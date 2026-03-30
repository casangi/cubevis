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
        from google.colab import kernel
        from IPython.display import display, Javascript

        # 1. Register the target first so it's ready when JS calls
        kernel.comms.register_target(self._comm_mgr_id, self._on_comm_open)
        
        # 2. Inject the JS. NOTE: This won't run until this cell finishes!
        display(Javascript(f'''
            (async () => {{
                // This global check ensures we don't try to connect if already active
                if (window.colab_transport_{self._comm_mgr_id}) return;

                const kernel = google.colab.kernel;
                const channel = await kernel.comms.open('{self._comm_mgr_id}', {{}});
                window.colab_transport_{self._comm_mgr_id} = channel;

                (async () => {{
                    for await (const message of channel.messages) {{
                        // Dispatch to your TS implementation listeners
                        window.dispatchEvent(new CustomEvent('{self._comm_mgr_id}', {{ detail: message.data }}));
                    }}
                }})();
                console.log("Colab JS Transport initialized");
            }})();
        '''))
        
        # IMPORTANT: Do not 'while not self._is_connected' here if you want
        # the cell to finish and the JS to actually run.
        print(f"📡 Target '{self._comm_mgr_id}' registered. Awaiting JS handshake...")

    def _on_comm_open(self, comm, msg):
        """Callback triggered when the frontend opens the comm channel."""
        self._comm = comm
        self._is_connected = True
        
        @comm.on_msg
        def _recv(msg):
            data = msg['content']['data']
            if self._message_callback:
                self._loop.call_soon_threadsafe(self._message_callback, data)

        @comm.on_close
        def _close(msg):
            self._is_connected = False
            if self.abort:
                self.abort()

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
    def __init__(self, comm_mgr_id: str, abort: Optional[Callable] = None):
        super().__init__(comm_mgr_id, abort)
        self._bridge = None
        self._comm: Optional[Comm] = None
        self._callback: Optional[Callable] = None
        self._connected = False
        self._debug = "CUBEVIS_DEBUG" in os.environ

    def _ensure_anywidget(self):
        """Lazy-load anywidget only when needed."""
        if self._bridge is not None:
            return

        logger.debug("JupyterCommsTransport._ensure_anywidget: setting up bridge")
        try:
            import anywidget
            from IPython.display import display
        except ImportError:
            raise ImportError(
                "The 'anywidget' package is required for Jupyter transport. "
                "Please install it with: pip install anywidget"
            )

        # Define the Bridge class locally so it's only created if anywidget exists
        class CommBridge(anywidget.AnyWidget):
            _esm = """
                function render({ model, el }) {
                    const isDebug = """ + ("true" if "CUBEVIS_DEBUG" in os.environ else "false") + """;

                    if (isDebug) {
                        el.innerHTML = "<div style='padding:5px; background: #dfd; border: 1px solid #4caf50;'>📡 Bridge JS Loaded</div>";
                        console.log("CUBEVIS DEBUG: Bridge JS Starting");
                    }

                    model.on("msg:custom", (msg) => {
                        if (msg.method === "open_comm") {
                            const kernel = model.widget_manager.kernel;
                            const comm = kernel.createComm(msg.target_id);

                            comm.onMsg = (m) => model.send({ type: "comm_msg", data: m.content.data });
                            comm.open({ status: "connected" });

                            window["comm_" + msg.target_id] = comm;

                            if (isDebug) {
                                el.innerHTML = "<div style='padding:5px; background: #ccf; border: 1px solid #2196f3;'>✅ Bridge Connected</div>";
                                console.log("CUBEVIS DEBUG: Comm opened for " + msg.target_id);
                            }
                        }
                    });
                }
                export default { render };
                """

        self._bridge = CommBridge()
        self._display_func = display

    async def connect(self) -> None:
        from google.colab import kernel
        from IPython.display import display, Javascript

        # 1. Register the target so Python is ready to hear from JS
        kernel.comms.register_target(self._comm_mgr_id, self._on_comm_open)

        # 2. Inject JS. This ONLY runs once this cell finishes!
        # We add a small delay to ensure the backend is fully ready.
        display(Javascript(f'''
            (async () => {{
                const target = "{self._comm_mgr_id}";
                console.log("Starting JS Handshake for:", target);
                try {{
                    const channel = await google.colab.kernel.comms.open(target, {{}});
                    // Success! Now listen for messages
                    (async () => {{
                        for await (const message of channel.messages) {{
                            window.dispatchEvent(new CustomEvent(target, {{ detail: message.data }}));
                        }}
                    }})();
                    console.log("JS Handshake Complete.");
                }} catch (e) {{
                    console.error("JS Handshake Failed:", e);
                }}
            }})();
        '''))

        print(f"📡 Registered {self._comm_mgr_id}. Run the next cell to verify.")

    def is_connected(self) -> bool:
        return self._connected

    async def send_message(self, message: Dict[str, Any]) -> None:
        if self._comm:
            self._comm.send(message)

    def set_message_callback(self, callback: Callable):
        self._callback = callback

    async def run(self) -> None:
        while self.is_connected() or self._comm is None:
            await asyncio.sleep(0.1)

    async def close(self):
        if self._comm:
            self._comm.close()
        self._connected = False

