########################################################################
# Python Transport Implementations
# 
# Complete implementations of Colab Comms and Jupyter Comms transports
# for use with CommMgr.
########################################################################

import os
import asyncio
import threading
import time
import logging
import importlib
from enum import Enum
from functools import wraps
from abc import ABC, abstractmethod
from bokeh.models import Div, CustomJS
from typing import Optional, Callable, Dict, Any
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

logger = logging.getLogger(__name__)

__all__ = [
    'TransportBase',
    'WebSocketTransport',
    'CommsTransport',
]

# ============================================================================
# Helper: resolve the correct Comm class for the current environment
# ============================================================================
def _get_comm_class():
    """
    Return an appropriate Comm constructor for the running kernel environment.

    Resolution order (newest / most standards-compliant first):
      1. `comm.create_comm`  — standalone `comm` package (ipykernel ≥ 6.15,
                               JupyterLab 4.x, recommended going forward).
      2. `ipykernel.comm.Comm` — bundled comm in older ipykernel (< 6.15,
                                  JupyterLab 3.x, Classic Notebook 6.x).
      3. `ipywidgets` shim — last-resort fallback for very old environments.

    Returns a callable ``factory(target_name, data)`` that creates and opens
    a new Comm, matching the interface used below.
    """
    # Option 1: standalone comm package (preferred, JupyterLab 4 / ipykernel ≥ 6.15)
    try:
        from comm import create_comm  # noqa: F401
        logger.debug("Using comm.create_comm (standalone comm package)")
        return ("create_comm", create_comm)
    except ImportError:
        pass

    # Option 2: ipykernel bundled Comm (JupyterLab 3 / Classic Notebook 6)
    try:
        from ipykernel.comm import Comm  # noqa: F401
        logger.debug("Using ipykernel.comm.Comm")
        return ("Comm", Comm)
    except ImportError:
        pass

    # Option 3: ipywidgets shim (very old environments)
    try:
        from ipywidgets.widgets.widget_comm import Comm  # noqa: F401
        logger.debug("Using ipywidgets.widgets.widget_comm.Comm (fallback)")
        return ("Comm", Comm)
    except ImportError:
        pass

    raise RuntimeError(
        "Could not find a Comm implementation. "
        "Install 'comm' (pip install comm) or 'ipykernel'."
    )

# ============================================================================
# Transport Base Class
# ============================================================================
class TransportBase(ABC):
    """Abstract base class for communication transports."""
    
    def __init__(self, comm_mgr_id: str, abort: Optional[Callable] = None):
        self._comm_mgr_id = comm_mgr_id
        self.abort = abort
        
    @abstractmethod
    async def connect(self) -> None:
        """Connect and initialize the transport."""
        pass
    
    @abstractmethod
    async def send_message(self, message: Dict[str, Any]) -> None:
        """Send a message through this transport."""
        pass
    
    @abstractmethod
    def set_message_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """Set callback for incoming messages (used by all transports)."""
        pass
    
    @abstractmethod
    async def run(self) -> None:
        """
        Run the transport event loop.
        
        For WebSocket: iterates over incoming messages
        For Colab/Jupyter: keeps event loop alive for callbacks
        
        Blocks until shutdown or connection closes.
        """
        pass
    
    @abstractmethod
    async def close(self) -> None:
        """Close the transport connection."""
        pass
    
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if transport is currently connected."""
        pass
    
# ============================================================================
# WebSocket Transport
# ============================================================================
class WebSocketTransport(TransportBase):
    """WebSocket-based transport for standalone, Jupyter Lab, and classic Notebook.

    This transport handles:
    - Initial handshaking (validate frontend/backend)
    - Event loop (iterating over messages)
    - Connection lifecycle
    
    Usage (same as Colab/Jupyter):
        transport = WebSocketTransport(comm_mgr_id, websocket)
        transport.set_message_callback(route_message)
        await transport.connect()  # Performs handshake
        await transport.run()      # Runs until connection closes
    """
    
    def __init__(self, comm_mgr_id: str, websocket, abort: Optional[Callable] = None):
        logger.debug( f'WebSocketTransport.__init__: {comm_mgr_id}' )
        super().__init__(comm_mgr_id, abort)
        self.websocket = websocket
        self._message_callback: Optional[Callable] = None
        self._connected = False
        self._initialized = False
        self._should_run = False

    def set_message_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """Set callback for incoming messages."""
        self._message_callback = callback
        logger.debug(f"Message callback set for WebSocket {self._comm_mgr_id}")
    
    async def connect(self) -> None:
        """
        Perform WebSocket handshake.
        
        Waits for initialization message from frontend and validates it.
        Sends acknowledgment back.
        """
        from ...utils import deserialize, serialize
        
        try:
            logger.debug(f"WebSocket waiting for initialization (comm_mgr_id={self._comm_mgr_id})")

            # Wait for initialization message
            init_message = await self.websocket.recv()
            msg = deserialize(init_message)
            
            if msg.get('id') == 'initialize' and msg.get('direction') == 'j2p':
                frontend_id = msg.get('frontend_id')
                backend_id = msg.get('backend_id')
                received_comm_mgr_id = msg.get('comm_mgr_id')
                
                logger.debug(
                    f"WebSocket initialization received: "
                    f"frontend={frontend_id}, backend={backend_id}, "
                    f"comm_mgr={received_comm_mgr_id}"
                )
                
                # Validate against BokehAppContext
                from .. import BokehInit
                app = BokehInit.get_app_context()
                
                warnings = []
                
                if app:
                    # Check backend_id
                    if backend_id and backend_id != app.backend_id:
                        warning = (
                            f"Frontend connecting to wrong backend! "
                            f"Expected: {app.backend_id}, Got: {backend_id}"
                        )
                        logger.warning(warning)
                        warnings.append(warning)
                    
                    # Check for duplicate frontend (multi-tab detection)
                    if frontend_id:
                        if app.frontend_id and app.frontend_id != frontend_id:
                            warning = (
                                f"Multiple tabs detected! "
                                f"Existing: {app.frontend_id}, New: {frontend_id}"
                            )
                            logger.warning(warning)
                            warnings.append(warning)
                            
                            # Send warning message
                            await self.websocket.send(serialize({
                                'type': 'warning',
                                'message': 'Application already open in another tab',
                                'existing_frontend_id': app.frontend_id,
                                'new_frontend_id': frontend_id
                            }))
                        else:
                            # Set frontend_id
                            app.frontend_id = frontend_id
                            logger.debug(f"Set frontend_id: {frontend_id}")
                
                # Send acknowledgment
                await self.websocket.send(serialize({
                    'type': 'initialized',
                    'backend_id': app.backend_id if app else self._comm_mgr_id,
                    'comm_mgr_id': self._comm_mgr_id,
                    'message': 'WebSocket connection established',
                    'warnings': warnings
                }))
                
                self._connected = True
                self._initialized = True
                logger.debug(f"WebSocket initialized for comm_mgr_id={self._comm_mgr_id}")
                
            else:
                raise RuntimeError(
                    f"First message was not initialization: {msg.get('id')}"
                )
                
        except Exception as e:
            logger.error(f"Error during WebSocket initialization: {e}")
            if self.abort:
                self.abort(e)
            raise
    
    async def send_message(self, message: Dict[str, Any]) -> None:
        """Send a message through the WebSocket."""
        if not self._connected:
            raise RuntimeError("WebSocket not connected")
        
        from ...utils import serialize
        await self.websocket.send(serialize(message))
    
    async def run(self) -> None:
        """
        Run the WebSocket event loop.
        
        Listens for messages until connection closes.
        ConnectionClosedError can happen when laptop sleeps and is NOT re-raised.
        """
        if not self._initialized:
            raise RuntimeError("Must call connect() before run()")
        
        if not self._message_callback:
            raise RuntimeError("Must call set_message_callback() before run()")
        
        from ...utils import deserialize
        self._should_run = True
        logger.debug(f"WebSocket event loop starting for {self._comm_mgr_id}")

        try:
            # Iterate over incoming messages
            async for message in self.websocket:
                if not self._should_run:
                    break
                try:
                    msg = deserialize(message)
                    await self._message_callback(msg)
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    # Continue processing other messages

            logger.debug(f"WebSocket closed normally for {self._comm_mgr_id}")

        except (ConnectionClosedError, ConnectionClosedOK) as e:
            # Normal close - don't treat as error
            logger.debug(f"WebSocket connection closed: {e}")
            # Don't re-raise - this is expected when laptop sleeps
            
        except Exception as e:
            logger.error(f"WebSocket event loop error: {e}")
            if self.abort:
                self.abort(e)
            raise  # Re-raise unexpected errors

        finally:
            self._connected = False
            self._initialized = False
            self._should_run = False
    
    async def close(self) -> None:
        """Close the WebSocket connection."""
        self._should_run = False
        if self.websocket and self._connected:
            try:
                await self.websocket.close()
                logger.debug(f"Closed WebSocket for {self._comm_mgr_id}")
            except Exception as e:
                logger.error(f"Error closing WebSocket: {e}")
            finally:
                self._connected = False
                self._initialized = False

    def is_connected(self) -> bool:
        """Check if WebSocket is connected and initialized."""
        return self._connected and self._initialized

# ============================================================================
# Jupyter and Colab Comms Transport
# ============================================================================
class CommsTransport(TransportBase):
    """
    anywidget-based transport for JupyterLab 4 and Google Colab.

    Message flow
    ------------
    JS -> Python : JS calls comm.send(data)
                   kernel routes comm_msg to Python comm.on_msg(_recv)
                   _recv calls the user callback directly on the kernel thread
                   (ipywidgets Output capture works on that thread)

    Python -> JS : Python calls self._comm.send(msg)
                   kernel delivers to JS rawComm.onMsg handler
                   (set by notebook / application code after connect())

    Notebook usage
    --------------
        transport = CommsTransport(comm_mgr_id="my_pipe")
        transport.set_message_callback(handler)
        # The bridge widget is displayed automatically during __init__.
        # If CUBEVIS_DEBUG is set it shows connection status; otherwise it
        # is a zero-height invisible element with no visual footprint.
        # ---- same or next cell ----
        await transport.connect()    # waits for JS handshake; raises on timeout
    """

    def __init__(self, comm_mgr_id: str, abort: Optional[Callable] = None):
        from tornado.ioloop import IOLoop
        logger.debug( f'CommsTransport.__init__: {comm_mgr_id}' )
        super().__init__(comm_mgr_id, abort)
        self._main_ioloop = IOLoop.current()
        self._bridge = None
        self._bridge_started = { }                   # key is self._comm_mgr_id
        self._callback: Optional[Callable] = None
        self._connected = False
        self._debug = "CUBEVIS_DEBUG" in os.environ
        # _conn_event is set once Comm connection is established
        # This may be set and read in different threads/event loops
        # so asyncio.Event( ) will not work.
        self._conn_event = threading.Event()
        self._last_parent_header: dict = {}  # parent header of last received comm_msg
        self._colab_pending_reply: dict = {}  # pending reply for poll delivery
        # In Colab: display the bridge immediately from __init__.
        # CommsTransport is constructed during a cell's execution (the setup
        # cell), so that cell's output context is open. The bridge must render
        # in its OWN cell iframe — separate from the Bokeh app cell — because
        # BroadcastChannel does not deliver to the posting context itself.
        # If the bridge and CommsTransport share an iframe, bc_rx.postMessage()
        # in the bridge ESM never reaches CommsTransport's bc_rx.onmessage.
        #
        # In JupyterLab: use the preflight mechanism so the bridge renders
        # in the Bokeh app cell (single iframe, no BroadcastChannel needed).
        # Use the preflight mechanism for both JupyterLab and Colab.
        # display_bridge() must run in the same cell as ic.show() so that
        # Colab's CDN widget manager routes comm_msg to the bridge model
        # (Colab only routes comm_msg to widgets in the currently-executing
        # cell's output context). This ensures model.on("msg:custom") fires
        # when Python calls self._bridge.send().
        # The window["cubevis_rx_cb_..."] callback then delivers to CommsTransport
        # which shares the same window (same iframe, same cell output).
        from .. import BokehInit
        BokehInit.get_app_context().add_preflight_callable(self.display_bridge)

    # ------------------------------------------------------------------
    # Environment detection
    # ------------------------------------------------------------------
    @staticmethod
    def _is_colab() -> bool:
        try:
            import google.colab  # noqa: F401
            return True
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # Comm-open callback – called by the kernel when JS opens the comm
    # ------------------------------------------------------------------
    def _on_comm_open(self, comm, msg):
        """
        Called by the kernel when JS opens a comm to one of our registered targets.

        We register two targets in Colab:
          target_id        — JS->Python channel (one per opener, _recv wired on each)
          target_id+"_reply" — Python->JS channel (one per app iframe, stored for sends)

        On JupyterLab only one target is registered (target_id) and it handles both
        directions via the single kernel comm.
        """
        logger.debug(f"CommsTransport._on_comm_open: comm opened for {self._comm_mgr_id}")

        # JS->Python channel: wire _recv and track
        if not hasattr(self, '_comm_objs'):
            self._comm_objs = []
        self._comm_objs.append(comm)
        self._comm = comm

        def _invoke_callback(msg):
            if self._callback:
                # the comm.on_msg callback mechanism is synchronous by design. It expects
                # a standard function and does not await the result
                self._main_ioloop.add_callback(
                    lambda: asyncio.ensure_future(self._callback(msg))
                )
            else:
                logger.error(f"_recv: no callback is available")

        def _recv(msg):
            logger.debug(f"{self} CommsTransport._recv: {msg}")
            from pathlib import Path
            with open(Path.home() / "debug.txt", "a") as f:
                f.write(f"<<_recv>> raw msg keys: {list(msg.keys())}\n")
                content = msg.get("content", {})
                f.write(f"<<_recv>> content keys: {list(content.keys())}\n")
                data = content.get("data", {})
                f.write(f"<<_recv>> data type: {type(data).__name__}, value: {str(data)[:200]}\n")

            # Capture parent header only for non-poll messages.
            # Poll messages use the bridge iframe context (captured by Colab kernel automatically).
            _data_peek = msg.get("content", {}).get("data", {})
            _is_poll_msg = isinstance(_data_peek, dict) and _data_peek.get("type") == "cubevis_poll"
            if not _is_poll_msg:
                self._last_parent_header = msg.get("parent_header", {})
                if not self._last_parent_header:
                    try:
                        from IPython import get_ipython as _gip2
                        _ip3 = _gip2()
                        if _ip3 is not None and hasattr(_ip3, 'kernel'):
                            k = _ip3.kernel
                            if hasattr(k, '_parent_header'):
                                self._last_parent_header = k._parent_header
                                if hasattr(k, '_parent_ident'):
                                    self._last_parent_ident = k._parent_ident
                    except Exception:
                        pass

            data = msg.get("content", {}).get("data", {})

            # Handle poll from widget bridge: deliver pending reply via eval_js.
            # eval_js runs in the bridge iframe (different from Bokeh app iframe),
            # so BroadcastChannel delivers to bc_rx.onmessage in the app iframe. ✓
            if _is_poll_msg:
                _pending = getattr(self, "_colab_pending_reply", {})
                if _pending:
                    import json as _pj
                    import pathlib as _plib
                    _fp2 = _plib.Path.home() / "debug.txt"
                    try:
                        from google.colab import output as _co
                        _cb = f"cubevis_rx_cb_{self._comm_mgr_id}"
                        _bc = f"cubevis_rx_{self._comm_mgr_id}"
                        _del_fn = f"_cubevis_pollDelivered_{self._comm_mgr_id}"
                        _env_s = _pj.dumps(_pending)
                        _js = (f"(()=>{{const msg={_env_s};"
                               f"const cb=window[{_pj.dumps(_cb)}];"
                               f"if(typeof cb==='function'){{"
                               f"  console.log('CUBEVIS poll-deliver: window cb');cb(msg);"
                               f"}} else {{"
                               f"  console.log('CUBEVIS poll-deliver: posting bc');"
                               f"}}"
                               f"try{{const bc=new BroadcastChannel({_pj.dumps(_bc)});"
                               f"bc.postMessage(msg);bc.close();}}catch(e){{}}"
                               f"if(window[{_pj.dumps(_del_fn)}])window[{_pj.dumps(_del_fn)}]();"
                               f"}})();")
                        _co.eval_js(_js, ignore_result=True)
                        self._colab_pending_reply = {}
                        with open(_fp2, "a") as _df:
                            _df.write(f"<<poll>> delivered reply via eval_js\n")
                    except Exception as _pe:
                        with open(_fp2, "a") as _df:
                            _df.write(f"<<poll>> delivery failed: {_pe}\n")
                else:
                    # Nothing pending: tell JS to count this as an empty poll
                    import json as _pj2
                    _empty_fn = f"_cubevis_pollEmpty_{self._comm_mgr_id}"
                    try:
                        from google.colab import output as _co2
                        _js2 = (f"if(window[{_pj2.dumps(_empty_fn)}])"
                                f"window[{_pj2.dumps(_empty_fn)}]();")
                        _co2.eval_js(_js2, ignore_result=True)
                    except Exception:
                        pass
                return

            # first check if it is a CommsTransport message
            if data.get("type") == "cubevis_message":
                from ...utils import deserialize
                logger.debug(f"CommsTransport._recv: expected message {data}, {self._callback}")
                try:
                    raw = data.get("data", "{}")

                    if isinstance( raw, str ):
                        actual_message = deserialize(raw)
                        logger.debug(f"CommsTransport._recv app message: {actual_message}")
                        _invoke_callback(actual_message)
                    else:
                        logger.error(f"_recv: data does not seem to be in a serialized format")

                except Exception as e:
                    logger.warning(f"CommsTransport._recv: deserialize failed: {e}, raw={raw[:200]}")

            else:
                # when testing simple messages are sent directly using
                # the Jupyter/Colab comm object
                logger.debug(f"{self} CommsTransport._recv: UNEXPECTED message {data}, {self._callback}")
                from pathlib import Path
                file_path = Path.home() / "debug.txt"
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(f"<<001>> {self} calling a regular function with {data}\n")
                try:
                    _invoke_callback(data)
                except Exception as e:
                    from pathlib import Path
                    file_path = Path.home() / "debug.txt"
                    with open( file_path, "a", encoding="utf-8") as f:
                        f.write( f"<<002>> {self} error calling function: {e}\n")

                logger.debug(f"{self} CommsTransport._recv: where are we {data}, {self._callback}")
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(f"<<003>> {self} after calling a regular function with {data}\n")

        comm.on_msg(_recv)

        if not self._connected:
            self._connected = True
            self._conn_event.set()

    # ------------------------------------------------------------------
    # Phase 1: synchronous – must run inside the cell output context
    # ------------------------------------------------------------------
    def display_bridge(self) -> None:
        """
        Build the anywidget bridge widget, register the comm target, and
        display it in the current cell output.

        Called automatically by __init__ while the cell output context is
        still open.  Should not be called directly.

        When CUBEVIS_DEBUG is set the widget shows a visible connection-status
        indicator.  Otherwise it renders as a zero-height invisible element
        so it has no visual footprint in the notebook.

        In Colab, also enables the custom widget manager automatically.
        """
        if self._bridge_started.get( self._comm_mgr_id, False ):
            logger.debug( f"display_bridge: already started for {self._comm_mgr_id}" )
            return

        logger.debug( f"display_bridge: starting for {self._comm_mgr_id}" )

        # prevent restarting a particular Jupyter/Colab comm
        self._bridge_started[self._comm_mgr_id] = True

        # Colab: enable CDN widget manager before any widget is displayed
        if self._is_colab():
            try:
                from google.colab import output as _colab_out
                _colab_out.enable_custom_widget_manager()
                logger.debug("CommsTransport: Colab custom widget manager enabled")
            except Exception as e:
                logger.warning(f"CommsTransport: could not enable Colab widget manager: {e}")

        try:
            import anywidget
            import traitlets
            from IPython.display import display
        except ImportError:
            raise ImportError(
                "The 'anywidget' package is required. Install with: pip install anywidget"
            )

        is_debug_js = "true" if self._debug else "false"

        # ESM message flow:
        #
        #   JS -> Python
        #     comm.send(data) → kernel comm_msg → Python comm.on_msg(_recv)
        #     This is the standard kernel comm path and it works reliably when
        #     the comm is created from within the widget's own render() context.
        #     The %%javascript verification cell accesses window["cubevis_"+id]
        #     which is set in the same widget iframe, so the comm_id is consistent.
        #
        #   Python -> JS
        #     self._comm.send(msg) → kernel comm_msg → JS rawComm.onMsg(m)
        #     rawComm.onMsg is null by default; set it after connect() to receive.
        #
        #   Colab path:
        #     google.colab.kernel.comms.open(targetId) instead of createComm().
        #     For Colab JS->Python, comm.send() routes through model.send() because
        #     Colab's comm object doesn't have a direct kernel ZMQ back-channel.
        #     For Colab Python->JS, self._comm may be None; send_message() falls
        #     back to self._bridge.send() which delivers via the anywidget channel.

        import time as _time
        _esm_ts = str(int(_time.time()))
        from pathlib import Path as _Path
        with open(_Path.home() / "debug.txt", "a") as _dbf:
            _dbf.write(f"<<display_bridge>> called, esm_ts={_esm_ts}, is_colab={self._is_colab()}\n")
        esm = "// cubevis-esm:" + _esm_ts + "\n" + r"""
            function render({ model, el }) {
                const isDebug  = """ + is_debug_js + r""";
                const targetId = model.get("target_id");
                // ESM version marker — if this log appears, the new ESM is running
                console.log("CUBEVIS ESM v2 render() called for", targetId);

                if (isDebug) {
                    el.innerHTML = `<div style="padding:5px;background:#dfd;border:1px solid #4caf50">` +
                                   `📡 Bridge JS Loaded (${targetId})</div>`;
                    console.log("CUBEVIS DEBUG: Bridge JS Starting, target =", targetId);
                } else {
                    // Zero-height invisible element — no visual footprint.
                    el.style.cssText = "display:block;height:0;overflow:hidden;margin:0;padding:0";
                }

                function attachComm(comm) {

                    window["cubevis_" + targetId] = { comm, dbg_el: isDebug ? el : null };

                    if (isDebug) {
                        el.innerHTML = `<div style="padding:5px;background:#ccf;border:1px solid #2196f3">` +
                                       `✅ Bridge Connected (${targetId})</div>`;
                        console.log("CUBEVIS DEBUG: Comm stored for", targetId);
                        console.log("CUBEVIS DEBUG: Comm stored in", window);
                    }
                }

                // ── Path 1: JupyterLab 4 / classic Jupyter ────────────────────
                // kernel.createComm() gives a real comm with a stable comm_id.
                // JS->Python via comm.send() routes through the kernel ZMQ channel
                // to Python comm.on_msg(_recv).  This works because the comm was
                // created here in the widget render() context, so the comm_id that
                // CommManager registered in comm_open matches what comm.send() uses.
                function tryJupyterPath() {
                    const kernel = model.widget_manager && model.widget_manager.kernel;
                    if (!kernel || typeof kernel.createComm !== "function") return false;

                    const comm = kernel.createComm(targetId);
                    comm.onMsg = null;   // set by app/notebook code to receive Python->JS
                    comm.open({ status: "connected" });
                    attachComm(comm);
                    return true;
                }

                // ── Path 2: Google Colab ──────────────────────────────────────
                // Colab does not expose kernel.createComm().  We use
                // google.colab.kernel.comms.open(targetId) instead.
                //
                // comms.open() triggers _on_comm_open on the Python side, giving
                // Python a comm with on_msg(_recv) wired — exactly like JupyterLab.
                //
                // JS->Python: channel.send(data) goes to Python _recv via
                //   the kernel comm that _on_comm_open registered.
                //
                // Python->JS: Python calls send_message() which broadcasts to all
                //   open comms including this channel; messages arrive in the
                //   channel.messages async iterator and are pumped to onMsg.
                //
                // Cross-iframe access: other Colab cell iframes cannot reach
                //   window["cubevis_"+id] since each cell has its own sandboxed window.
                //   Those cells must call google.colab.kernel.comms.open(targetId)
                //   themselves to get their own channel — _on_comm_open fires again,
                //   _recv is wired to the new comm, and send_message() broadcasts
                //   Python->JS to all open comms including the new one.
                async function tryColabPath() {
                    try {
                        const colabComms = google?.colab?.kernel?.comms;
                        if (!colabComms || typeof colabComms.open !== "function") return false;

                        // The widget bridge iframe is the sole owner of the kernel comm.
                        // Colab's channel.messages does NOT deliver Python's comm.send()
                        // calls (proven by diagnostic testing). Instead we use two
                        // BroadcastChannels as a same-origin cross-iframe bus:
                        //
                        //   bc_tx ("cubevis_tx_<id>"): JS -> Python
                        //     Any iframe posts here; bridge receives and calls channel.send()
                        //
                        //   bc_rx ("cubevis_rx_<id>"): Python -> JS
                        //     Python calls self._bridge.send() → anywidget model →
                        //     model.on("msg:custom") here → bc_rx.postMessage()
                        //     Any iframe listening on bc_rx receives the reply.

                        const channel = await colabComms.open(targetId, {});

                        // TX bus: relay JS->Python from any iframe to the kernel
                        const bc_tx = new BroadcastChannel(`cubevis_tx_${targetId}`);
                        bc_tx.onmessage = (event) => {
                            if (isDebug) console.log("CUBEVIS DEBUG: bc_tx relay to kernel:", event.data);
                            channel.send(event.data);
                        };

                        // On-demand polling: JS sends a poll only when waiting for a reply.
                        // This avoids flooding the kernel with empty polls.
                        // bc_tx.onmessage (any JS->Python message) triggers a poll sequence.
                        // Python's _recv delivers the reply via eval_js in the bridge iframe
                        // context, then BroadcastChannel delivers to the Bokeh app iframe. ✓
                        let _pollActive = false;
                        let _pollTimer = null;
                        let _emptyPolls = 0;
                        const _MAX_EMPTY = 4; // stop after 4 consecutive empty polls (~200ms idle)

                        function _startPoll() {
                            _emptyPolls = 0; // reset idle counter on new request
                            if (_pollActive) return;
                            _pollActive = true;
                            function _doPoll() {
                                if (!_pollActive) return;
                                channel.send({ type: "cubevis_poll", target_id: targetId });
                                _pollTimer = setTimeout(_doPoll, 50);
                            }
                            _doPoll();
                        }

                        // Python calls these via eval_js to control the poll loop
                        function _onPollDelivered() {
                            _emptyPolls = 0; // reply delivered, reset idle counter
                        }
                        function _onPollEmpty() {
                            _emptyPolls += 1;
                            if (_emptyPolls >= _MAX_EMPTY) {
                                _pollActive = false;
                                if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
                            }
                        }
                        window[`_cubevis_pollDelivered_${targetId}`] = _onPollDelivered;
                        window[`_cubevis_pollEmpty_${targetId}`] = _onPollEmpty;

                        // Hook into bc_tx: start polling whenever JS sends a message to Python
                        const _origBcTxOnmessage = bc_tx.onmessage;
                        bc_tx.onmessage = (event) => {
                            _origBcTxOnmessage(event);
                            _startPoll();
                        };

                        // Register stop function for Python to call after delivering reply

                        // RX bus: Python->JS via anywidget model → deliver to listeners.
                        // Two delivery paths:
                        //   1. window["cubevis_rx_cb_"+id](msg) — for same-iframe delivery
                        //      (BroadcastChannel does NOT deliver to sender's own context)
                        //   2. bc_rx.postMessage(msg) — for cross-iframe delivery
                        const bc_rx = new BroadcastChannel(`cubevis_rx_${targetId}`);
                        model.on("msg:custom", (msg) => {
                            // This fires when Python calls self._bridge.send(envelope)
                            console.log("CUBEVIS: msg:custom fired!", msg);
                            // Same-iframe: call registered callback directly
                            const cb = window[`cubevis_rx_cb_${targetId}`];
                            console.log("CUBEVIS: window cb type=", typeof cb);
                            if (typeof cb === "function") {
                                console.log("CUBEVIS: calling window cb");
                                cb(msg);
                            }
                            // Cross-iframe: broadcast for other contexts
                            bc_rx.postMessage(msg);
                        });
                        console.log("CUBEVIS: msg:custom handler registered on model");

                        const comm = {
                            // Direct JS->Python from this iframe
                            send(data) { channel.send(data); },
                            onMsg: null,
                        };

                        attachComm(comm);
                        return true;
                    } catch (e) {
                        console.error("CUBEVIS: Colab comm path failed:", e);
                        return false;
                    }
                }

                // Try JupyterLab first, fall back to Colab
                (async () => {
                    if (!tryJupyterPath()) {
                        if (isDebug) console.log("CUBEVIS DEBUG: Jupyter path unavailable, trying Colab");
                        const ok = await tryColabPath();
                        if (!ok) {
                            console.error("CUBEVIS: No comm path succeeded for", targetId);
                            if (isDebug) {
                                el.innerHTML = `<div style="padding:5px;background:#fcc;border:1px solid red">` +
                                               `❌ No comm path for ${targetId}</div>`;
                            }
                        }
                    }
                })();
            }
            export default { render };
        """

        class CommBridge(anywidget.AnyWidget):
            _esm = esm
            target_id = traitlets.Unicode("").tag(sync=True)

        self._bridge = CommBridge(target_id=self._comm_mgr_id)

        # Register the JS->Python comm target. Python opens the reply channel
        # JS->Python only; Python->JS travels via anywidget model → bc_rx.
        comm_class_name, _ = _get_comm_class()
        if comm_class_name == "create_comm":
            try:
                import comm as _comm_pkg
                _comm_pkg.get_comm_manager().register_target(
                    self._comm_mgr_id, self._on_comm_open
                )
            except Exception as e:
                logger.warning(f"CommsTransport: comm target registration failed: {e}")
        else:
            try:
                from IPython import get_ipython
                shell = get_ipython()
                if shell is not None:
                    shell.kernel.comm_manager.register_target(
                        self._comm_mgr_id, self._on_comm_open
                    )
            except Exception as e:
                logger.warning(f"CommsTransport: comm target registration failed: {e}")

        # Wrap the bridge in an ipywidgets.Output so it renders in its own
        # sandboxed iframe even when display_bridge() is called from the same
        # cell as the Bokeh app. Each Output widget gets its own iframe in
        # Colab, giving the bridge a separate browsing context from CommsTransport.
        # This ensures BroadcastChannel messages from the bridge ESM are
        # delivered to CommsTransport's bc_rx.onmessage (different context).
        if self._is_colab():
            try:
                import ipywidgets as _ipyw
                _out = _ipyw.Output()
                with _out:
                    display(self._bridge)
                display(_out)
                logger.debug(f"CommsTransport.display_bridge: bridge wrapped in Output widget for {self._comm_mgr_id}")
            except ImportError:
                display(self._bridge)
                logger.debug(f"CommsTransport.display_bridge: bridge displayed directly (no ipywidgets) for {self._comm_mgr_id}")
        else:
            display(self._bridge)
        logger.debug(f"CommsTransport.display_bridge: widget displayed for {self._comm_mgr_id}")

    # ------------------------------------------------------------------
    # Phase 2: async – wait for the JS handshake to complete
    # ------------------------------------------------------------------
    async def connect(self, timeout: float = 30.0) -> None:
        """
        Wait for the JS side to complete the handshake.

        On JupyterLab: _on_comm_open fires when JS calls comm.open().
        On Colab: _on_comm_open fires when JS calls comms.open(targetId), setting _conn_event.

        Must be called after display_bridge(). Raises RuntimeError on timeout.
        """
        if self._connected: return

        if self._bridge is None:
            raise RuntimeError( "display_bridge() must be called before connect()." )

        deadline = asyncio.get_event_loop( ).time( ) + timeout
        while not self._conn_event.is_set( ):
            if asyncio.get_event_loop( ).time( ) > deadline:
                raise RuntimeError( f"CommsTransport: JS handshake timed out after {timeout}s" )
            await asyncio.sleep( 0.1 )

        logger.debug("CommsTransport.connect: handshake complete")

    # ------------------------------------------------------------------
    # TransportBase interface
    # ------------------------------------------------------------------
    def is_connected(self) -> bool:
        return self._connected

    def set_message_callback(self, callback: Callable) -> None:
        import inspect
        # Determine the execution style ONCE
        is_async = inspect.iscoroutinefunction(callback)

        @wraps(callback)
        async def wrapper(msg):
            from pathlib import Path
            file_path = Path.home() / "debug.txt"
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(f"<<004>> {self} in wrapper function with {msg}\n")
            try:
                if is_async:
                    with open(file_path, "a", encoding="utf-8") as f:
                        f.write(f"<<005>> {self} invoking coroutine with {msg}\n")
                    await callback(msg)
                else:
                    with open(file_path, "a", encoding="utf-8") as f:
                        f.write(f"<<006>> {self} invoking wrapped function with {msg}\n")
                    callback(msg)
            except Exception as e:
                logger.error(f"set_message_callback.wrapper error: {e}")
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(f"<<007>> {self} error encountered {e}\n")

        # Store the wrapper as the internal callback
        self._callback = wrapper

    async def send_message(self, message: Dict[str, Any]) -> None:
        from pathlib import Path
        file_path = Path.home() / "debug.txt"
        with open(file_path, "a") as f:
            f.write(f"<<send_message>> sending to {len(self._comm_objs)} comms: {str(message)[:100]}\n")
            f.write(f"<<send_message>> is_colab={self._is_colab()} bridge={self._bridge is not None}\n")
        from ...utils import serialize
        if not self._connected:
            raise RuntimeError("CommsTransport: not connected")

        envelope = {
            "type": "cubevis_message",
            "comm_mgr_id": self._comm_mgr_id,
            "data": serialize(message)          # Bokeh-serialize the payload
        }

        if self._is_colab():
            # Colab: Python->JS via google.colab.output.eval_js() (blocking/synchronous).
            # eval_js executes JS in the output context of whichever cell triggered the
            # current kernel execution. For spectrum clicks, that's the Bokeh app cell —
            # the same iframe where CommsTransport registered window["cubevis_rx_cb_..."].
            # The window callback delivers directly to colabComm.onMsg → handleJupyterMessage.
            # BroadcastChannel is also posted for any cross-iframe listeners.
            import json as _json
            # eval_js only works when called from the main IPython kernel thread.
            # When send_message is called from a Tornado IOLoop callback (as in the
            # full app), eval_js with ignore_result=True silently does nothing.
            # Fix: schedule the eval_js call on the kernel's main asyncio loop using
            # IPython's kernel.io_loop, which runs on the main thread where eval_js works.
            try:
                from google.colab import output as _colab_output
                cb_name = f"cubevis_rx_cb_{self._comm_mgr_id}"
                bc_name = f"cubevis_rx_{self._comm_mgr_id}"
                env_json = _json.dumps(envelope)
                js_code = (
                    f"(()=>{{"
                    f"const msg={env_json};"
                    f"const cb=window[{_json.dumps(cb_name)}];"
                    f"if(typeof cb==='function'){{"
                    f"  console.log('CUBEVIS eval_js: calling window cb');"
                    f"  cb(msg);"
                    f"}} else {{"
                    f"  console.log('CUBEVIS eval_js: no window cb, posting to bc');"
                    f"}}"
                    f"try{{const bc=new BroadcastChannel({_json.dumps(bc_name)});bc.postMessage(msg);bc.close();}}catch(e){{}}"
                    f"}})();"
                )

                _parent_header = getattr(self, '_last_parent_header', {})

                # Try Bokeh document approach: update a Bokeh model property.
                # The Bokeh session has its own always-active WebSocket to the browser.
                # Store reply for delivery via poll mechanism.
                # The widget bridge polls Python every 250ms via channel.send({type:"cubevis_poll"}).
                # Python's _recv handles the poll synchronously and calls eval_js there.
                # eval_js runs in the widget bridge iframe context (different from Bokeh app iframe),
                # so BroadcastChannel delivers to CommsTransport's bc_rx.onmessage. ✓
                self._colab_pending_reply = envelope
                with open(file_path, "a") as _f:
                    _f.write(f"<<send_message>> reply stored for poll delivery\n")

            except Exception as e:
                with open(file_path, "a") as f:
                    f.write(f"<<send_message>> eval_js FAILED: {type(e).__name__}: {e}\n")
                logger.warning(f"CommsTransport.send_message: eval_js failed: {e}")
        else:
            # JupyterLab: single bidirectional kernel comm
            comm_objs = getattr(self, '_comm_objs', None)
            if not comm_objs:
                raise RuntimeError("CommsTransport: not connected (no comm available)")
            try:
                comm_objs[0].send(envelope)
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(f"<<send_message>> sent via comm: {str(envelope)[:120]}\n")
            except Exception as e:
                with open(file_path, "a") as f:
                    f.write(f"<<send_message>> comm.send FAILED: {e}\n")
                logger.warning(f"CommsTransport.send_message: comm send failed: {e}")

    async def run(self) -> None:
        """Keep the transport alive until disconnected."""
        while self._connected:
            await asyncio.sleep(0.1)

    async def close(self) -> None:
        for c in getattr(self, '_comm_objs', []):
            try:
                c.close()
            except Exception:
                pass
        self._comm_objs = []
        self._connected = False
        self._bridge = None
