########################################################################
# Python Transport Implementations
# 
# Complete implementations of Colab Comms and Jupyter Comms transports
# for use with CommMgr.
########################################################################

import os
import asyncio
import time
import logging
import importlib
import traceback
from enum import Enum
from abc import ABC, abstractmethod
from bokeh.models import Div, CustomJS
from typing import Optional, Callable, Dict, Any
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

logger = logging.getLogger(__name__)

__all__ = [
    'TransportBase',
    'WebSocketTransport',
    'ColabCommsTransport',
    'JupyterCommsTransport',
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
        logger.debug('------------------------------------------------------------------------------------------------------------------------')
        logger.debug ('WebSocketTransport.__init__', stack_info=True)
        logger.debug('------------------------------------------------------------------------------------------------------------------------')
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
# Colab Comms Transport
# ============================================================================
import asyncio
from typing import Dict, Any, Optional, Callable

class ColabCommsTransport(TransportBase):
    """Transport for Google Colab using google.colab.kernel.comms."""

    def __init__(self, comm_mgr_id: str, abort: Optional[Callable] = None):
        super().__init__(comm_mgr_id, abort)
        self._comm = None
        self._message_callback = None
        self._is_connected = False
        self._loop = asyncio.get_event_loop()

    async def connect(self) -> None:
        try:
            import google.colab
            from IPython.display import display, Javascript
        except ImportError:
            raise RuntimeError("Google Colab environment not detected.")

        # 1. Register the target and EXIT. Do not wait for the flag here.
        try:
            google.colab.kernel.comms.register_target(self._comm_mgr_id, self._on_comm_open)
        except AttributeError:
            shell = get_ipython()
            shell.kernel.comm_manager.register_target(self._comm_mgr_id, self._on_comm_open)

        # 2. This JS won't execute until this cell COMPLETES.
        display(Javascript(f'''
            (async () => {{
                console.log("JS: Handshake starting for {self._comm_mgr_id}...");
                try {{
                    const channel = await google.colab.kernel.comms.open('{self._comm_mgr_id}', {{}});
                    window._colab_comm_{self._comm_mgr_id} = channel;
                    console.log("JS: Handshake SUCCESS.");
                }} catch (e) {{
                    console.error("JS: Handshake FAILED", e);
                }}
            }})();
        '''))
        print(f"📡 Registered {self._comm_mgr_id}. Cell finished. Ready for JS connection.")

    def _on_comm_open(self, comm, msg):
        # This will show up in the cell output area
        print(f"🔥 KERNEL: Comm opened for {self._comm_mgr_id}")
        self._comm = comm
        self._is_connected = True
        
        def _recv(msg):
            data = msg.get('content', {}).get('data', {})
            # Print to both places for absolute certainty
            print(f"📩 KERNEL RECEIVED: {data}")
            if self._message_callback:
                self._loop.call_soon_threadsafe(self._message_callback, data)

        self._comm.on_msg(_recv)

    async def send_message(self, message: Dict[str, Any]) -> None:
        """Send message from Python to Colab JS."""
        if self._comm:
            self._comm.send(message)

    def set_message_callback(self, callback: Callable[[Dict[str, Any]], None]):
        self._message_callback = callback

    async def run(self) -> None:
        """Keeps the transport alive until disconnected."""
        while self._is_connected:
            await asyncio.sleep(1)

    async def close(self) -> None:
        if self._comm:
            self._comm.close()
        self._is_connected = False

    def is_connected(self) -> bool:
        return self._is_connected

# ============================================================================
# Jupyter Comms Transport
# ============================================================================
class JupyterCommsTransport(TransportBase):
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
        transport = JupyterCommsTransport(comm_mgr_id="my_pipe")
        transport.set_message_callback(handler)
        transport.display_bridge()   # synchronous – must run inside a cell
        # ---- same or next cell ----
        await transport.connect()    # waits for JS handshake; raises on timeout
    """

    def __init__(self, comm_mgr_id: str, abort: Optional[Callable] = None):
        super().__init__(comm_mgr_id, abort)
        self._bridge = None
        self._comm = None
        self._callback: Optional[Callable] = None
        self._connected = False
        self._debug = "CUBEVIS_DEBUG" in os.environ
        self._conn_event: Optional[asyncio.Event] = None

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
        Called each time any JS context opens a comm to our target_name.

        On JupyterLab: called once, from the widget's render() function.
        On Colab: called once per iframe that calls comms.open(targetId).
          - First call: the widget render() in the bridge iframe.
          - Subsequent calls: any %%javascript cell or app iframe that
            independently opens a channel to communicate with Python.
            Each gets its own comm and its own _recv wired up, so messages
            from all of them reach the Python callback.
            The LAST opener's comm is stored as self._comm for Python->JS
            sends (send_message). On Colab, Python->JS is sent to ALL open
            comms via _colab_comms so every listener receives it.
        """
        logger.debug(f"JupyterCommsTransport._on_comm_open: comm opened for {self._comm_mgr_id}")

        # Track all open comms for Python->JS broadcast (Colab needs this
        # because each iframe opener is a separate channel)
        if not hasattr(self, '_colab_comms'):
            self._colab_comms = []
        self._colab_comms.append(comm)
        self._comm = comm   # also keep last for the JupyterLab single-comm case
        self._connected = True

        def _recv(msg):
            data = msg.get("content", {}).get("data", {})
            if data.get("type") == "js_ready":
                return
            logger.debug(f"JupyterCommsTransport._recv: {data}")
            if self._callback:
                self._callback(data)

        comm.on_msg(_recv)

        # Unblock connect() on first open
        if self._conn_event is not None and not self._conn_event.is_set():
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.call_soon_threadsafe(self._conn_event.set)
                else:
                    self._conn_event.set()
            except RuntimeError:
                self._conn_event.set()

    # ------------------------------------------------------------------
    # Phase 1: synchronous – must run inside the cell output context
    # ------------------------------------------------------------------
    def display_bridge(self) -> None:
        """
        Build the anywidget bridge, register the comm target, and display it.

        MUST be the last statement of a notebook cell (or called before the
        cell finishes) so that IPython's output context is still open when
        display() is called and the widget renders.

        In Colab, also enables the custom widget manager automatically.
        """
        # Colab: enable CDN widget manager before any widget is displayed
        if self._is_colab():
            try:
                from google.colab import output as _colab_out
                _colab_out.enable_custom_widget_manager()
                logger.debug("JupyterCommsTransport: Colab custom widget manager enabled")
            except Exception as e:
                logger.warning(f"JupyterCommsTransport: could not enable Colab widget manager: {e}")

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
        #     The %%javascript verification cell accesses window["comm_"+id]
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
                }

                function attachComm(comm) {
                    window["comm_" + targetId] = comm;
                    if (isDebug) {
                        el.innerHTML = `<div style="padding:5px;background:#ccf;border:1px solid #2196f3">` +
                                       `✅ Bridge Connected (${targetId})</div>`;
                        console.log("CUBEVIS DEBUG: Comm stored for", targetId);
                    }
                    model.send({ type: "js_ready", target_id: targetId });
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
                // JS->Python: channel.send({}, data) goes to Python _recv via
                //   the kernel comm that _on_comm_open registered.
                //
                // Python->JS: Python calls send_message() which broadcasts to all
                //   open comms including this channel; messages arrive in the
                //   channel.messages async iterator and are pumped to onMsg.
                //
                // Cross-iframe access: other Colab cell iframes cannot reach
                //   window["comm_"+id] since each cell has its own sandboxed window.
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
                            send(data) { channel.send({}, data); },
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

        # Register the Python-side comm target before display() so it is ready
        # the moment JS render() fires and calls comm.open().
        comm_class_name, _ = _get_comm_class()
        if comm_class_name == "create_comm":
            try:
                import comm as _comm_pkg
                _comm_pkg.get_comm_manager().register_target(
                    self._comm_mgr_id, self._on_comm_open
                )
            except Exception as e:
                logger.warning(f"JupyterCommsTransport: comm target registration failed: {e}")
        else:
            try:
                from IPython import get_ipython
                shell = get_ipython()
                if shell is not None:
                    shell.kernel.comm_manager.register_target(
                        self._comm_mgr_id, self._on_comm_open
                    )
            except Exception as e:
                logger.warning(f"JupyterCommsTransport: comm target registration failed: {e}")

        display(self._bridge)
        logger.debug(f"JupyterCommsTransport.display_bridge: widget displayed for {self._comm_mgr_id}")

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
        if self._bridge is None:
            raise RuntimeError(
                "display_bridge() must be called before connect()."
            )
        if self._connected:
            logger.debug("JupyterCommsTransport.connect: already connected")
            return

        self._conn_event = asyncio.Event()
        logger.debug(f"JupyterCommsTransport.connect: waiting for handshake (timeout={timeout}s)")
        try:
            await asyncio.wait_for(self._conn_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"JupyterCommsTransport: JS handshake timed out after {timeout}s. "
                "Ensure the bridge widget rendered (CUBEVIS_DEBUG will show it) "
                "and that anywidget is installed."
            )
        logger.debug("JupyterCommsTransport.connect: handshake complete")

    # ------------------------------------------------------------------
    # TransportBase interface
    # ------------------------------------------------------------------
    def is_connected(self) -> bool:
        return self._connected

    def set_message_callback(self, callback: Callable) -> None:
        self._callback = callback

    async def send_message(self, message: Dict[str, Any]) -> None:
        if not self._connected:
            raise RuntimeError("JupyterCommsTransport: not connected")
        colab_comms = getattr(self, '_colab_comms', None)
        if colab_comms:
            # Colab: broadcast to every iframe that has opened a channel.
            # This ensures the app iframe, the widget iframe, and any test
            # cell that opened its own comm all receive Python->JS messages.
            for c in list(colab_comms):
                try:
                    c.send(message)
                except Exception as e:
                    logger.warning(f"JupyterCommsTransport.send_message: comm send failed: {e}")
        elif self._comm is not None:
            # JupyterLab: single kernel comm
            self._comm.send(message)
        else:
            raise RuntimeError("JupyterCommsTransport: not connected (no comm available)")

    async def run(self) -> None:
        """Keep the transport alive until disconnected."""
        while self._connected:
            await asyncio.sleep(0.1)

    async def close(self) -> None:
        for c in getattr(self, '_colab_comms', []):
            try:
                c.close()
            except Exception:
                pass
        self._colab_comms = []
        self._comm = None
        self._connected = False
        self._bridge = None
