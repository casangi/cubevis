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
        # Build and display the bridge immediately while the cell output
        # context is still open.  display_bridge() is intentionally NOT a
        # separate public call anymore — construction = display.
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

            data = msg.get("content", {}).get("data", {})

            # Handle the signal from JS asking Python to open the reply channel.
            # JS registered a target handler and now needs Python to initiate the
            # comm_open so Colab routes Python->JS messages to that handler.
            if data.get("type") == "cubevis_open_reply":
                reply_target = data.get("reply_target", self._comm_mgr_id + "_reply")
                logger.debug(f"CommsTransport._recv: opening reply channel on {reply_target}")
                self._open_reply_channel(reply_target)
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
    # Open the Python-initiated reply channel (Colab Python->JS)
    # ------------------------------------------------------------------
    def _open_reply_channel(self, reply_target: str) -> None:
        """
        Open a Python-initiated comm to reply_target so that Colab's JS
        registerTarget handler receives a channel whose .messages iterator
        reliably delivers Python's comm.send() calls.

        Called when _recv receives a "cubevis_open_reply" signal from JS.
        JS registered a target handler BEFORE sending that signal, so the
        handler is in place when this comm_open arrives.
        """
        from pathlib import Path
        comm_class_name, _ = _get_comm_class()
        try:
            if comm_class_name == "create_comm":
                from comm import create_comm
                reply_comm = create_comm(target_name=reply_target)
                reply_comm.open()
            else:
                from IPython import get_ipython
                shell = get_ipython()
                from ipykernel.comm import Comm
                reply_comm = Comm(target_name=reply_target)
                reply_comm.open()

            if not hasattr(self, '_reply_comms'):
                self._reply_comms = []
            self._reply_comms.append(reply_comm)
            logger.debug(f"CommsTransport._open_reply_channel: opened {reply_target}, "
                        f"total reply comms={len(self._reply_comms)}")
            with open(Path.home() / "debug.txt", "a") as f:
                f.write(f"<<reply_channel>> opened {reply_target}, comm_id={reply_comm.comm_id}\n")
        except Exception as e:
            logger.error(f"CommsTransport._open_reply_channel: failed: {e}")
            with open(Path.home() / "debug.txt", "a") as f:
                f.write(f"<<reply_channel>> FAILED: {e}\n")

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

        esm = r"""
            function render({ model, el }) {
                const isDebug  = """ + is_debug_js + r""";
                const targetId = model.get("target_id");

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

                        const channel = await colabComms.open(targetId, {});

                        // Wrap channel in a comm-shaped object so app code can use
                        // comm.send(data) and comm.onMsg = fn uniformly.
                        const comm = {
                            // JS -> Python: goes through the kernel comm (_recv fires)
                            send(data) { channel.send(data); },
                            // Python -> JS: set by app/notebook code to receive messages
                            onMsg: null,
                        };

                        // Pump Python->JS messages from channel.messages to comm.onMsg
                        (async () => {
                            for await (const message of channel.messages) {
                                if (typeof comm.onMsg === "function") {
                                    comm.onMsg({ content: { data: message.data || {} } });
                                }
                            }
                        })();

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
        # dynamically when JS signals it (cubevis_open_reply in _recv).
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
        from ...utils import serialize
        if not self._connected:
            raise RuntimeError("CommsTransport: not connected")

        envelope = {
            "type": "cubevis_message",
            "comm_mgr_id": self._comm_mgr_id,
            "data": serialize(message)          # Bokeh-serialize the payload
        }

        # On Colab: send via dedicated _reply_comms (Python->JS channels).
        # Each app iframe opened its own _reply channel; send to the most recent
        # one (the active app instance). Earlier ones may be stale page loads.
        # On JupyterLab: single bidirectional _comm_objs channel.
        reply_comms = getattr(self, '_reply_comms', None)
        if reply_comms:
            # Colab path: use the last registered reply comm (most recent opener)
            target_comm = reply_comms[-1]
            try:
                target_comm.send(envelope)
                with open(file_path, "a", encoding="utf-8") as f:
                    f.write(f"<<send_message>> sent OK via reply_comm: {envelope}\n")
            except Exception as e:
                with open(file_path, "a") as f:
                    f.write(f"<<send_message>> FAILED via reply_comm: {e}\n")
                logger.warning(f"CommsTransport.send_message: reply comm send failed: {e}")
        elif getattr(self, '_comm_objs', None):
            # JupyterLab path: single bidirectional comm
            for c in list(self._comm_objs):
                try:
                    c.send(envelope)
                    with open(file_path, "a", encoding="utf-8") as f:
                        f.write(f"<<send_message>> sent OK via comm_obj: {envelope}\n")
                except Exception as e:
                    with open(file_path, "a") as f:
                        f.write(f"<<send_message>> FAILED via comm_obj: {e}\n")
                    logger.warning(f"CommsTransport.send_message: comm send failed: {e}")
        else:
            raise RuntimeError("CommsTransport: not connected (no comm available)")

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
        for c in getattr(self, '_reply_comms', []):
            try:
                c.close()
            except Exception:
                pass
        self._comm_objs = []
        self._reply_comms = []
        self._connected = False
        self._bridge = None
