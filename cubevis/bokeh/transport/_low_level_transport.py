########################################################################
# Python Transport Implementations
# 
# Complete implementations of Colab Comms and Jupyter Comms transports
# for use with CommMgr.
########################################################################

import asyncio
import time
import logging
import traceback
from enum import Enum
from abc import ABC, abstractmethod
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
class ColabCommsTransport(TransportBase):
    """
    Colab Comms-based transport for Google Colab environment.
    
    Uses google.colab.output.register_comm_target for efficient bidirectional
    communication.  Leverages Bokeh's serialization for numpy arrays and
    complex data structures.

    Key features:
    - Uses Colab's native comm protocol (not eval_js)
    - Handles large data efficiently (images, arrays)
    - Automatic serialization via Bokeh
    - Callback-based message reception
    
    Usage:
        transport = ColabCommsTransport('app_comms', abort=error_handler)
        transport.set_message_callback(route_message)
        await transport.connect()
        await transport.run()
        await transport.send_message({'data': large_array})
    """
    
    def __init__(self, comm_mgr_id: str, abort: Optional[Callable] = None):
        super().__init__(comm_mgr_id, abort)
        self._comm = None
        self._registered = False
        self._should_run = False
        self._message_callback: Optional[Callable] = None
        self._target_name = f'cubevis_comm_mgr_{comm_mgr_id}'

    # ------------------------------------------------------------------
    # Public TransportBase interface
    # ------------------------------------------------------------------
    def set_message_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """Set callback for incoming messages (used by CommMgr).

        Args:
            callback: Async function to call when messages arrive.
        """
        self._message_callback = callback
        logger.debug(f"Message callback set for {self._target_name}")

    async def connect(self) -> None:
        """
        Connect and register Colab comm.
        """
        logger.info(f"Colab Comms connecting for comm_mgr: {self._comm_mgr_id}")
        await self._register_comm()

    async def send_message(self, message: Dict[str, Any]) -> None:
        """
        Send a message through Colab Comms.
        
        The message is serialized using Bokeh's serialization which efficiently
        handles numpy arrays and complex data structures.
        
        Args:
            message: Dictionary message to send (may contain numpy arrays)
            
        Raises:
            RuntimeError: If comm is not registered
        """
        if not self._registered or self._comm is None:
            raise RuntimeError(
                f"Colab Comm not connected for {self._comm_mgr_id}. "
                "Call connect() first."
            )

        try:
            from ...utils import serialize
            
            # Serialize the message using Bokeh's serialization
            # This handles numpy arrays efficiently
            serialized = serialize(message)
            
            # Send the serialized string via comm
            # Colab comms expect a dict, so wrap in data field
            self._comm.send({'data': serialized})
            
            logger.debug(
                f"Sent message via Colab comm {self._target_name} "
                f"({len(serialized)} bytes)"
            )
        except Exception as e:
            logger.error(f"Error sending message via Colab comm: {e}")
            if self.abort:
                self.abort(e)
            raise
    
    async def run(self) -> None:
        """Keep event loop alive for Colab comm callbacks."""
        self._should_run = True
        logger.debug(f"Colab Comms event loop starting for {self._comm_mgr_id}")
        while self._should_run and self._registered:
            await asyncio.sleep(0.1)
        logger.debug(f"Colab Comms event loop ended for {self._comm_mgr_id}")

    async def close(self) -> None:
        """Close the Colab Comms connection."""
        self._should_run = False

        if self._comm is not None:
            try:
                from ...utils import serialize
                
                # Send closing notification
                self._comm.send({'data': serialize({
                    'type': 'closing',
                    'comm_mgr_id': self._comm_mgr_id,
                    'message': 'Backend closing comm'
                })})

                # Close the comm
                self._comm.close()
                logger.debug(f"Closed Colab comm for {self._target_name}")
            except Exception as e:
                logger.error(f"Error closing Colab comm: {e}")
            finally:
                self._comm = None
        
        self._registered = False
    
    def is_connected(self) -> bool:
        """Check if Colab Comms is registered and has an active comm."""
        return self._registered and self._comm is not None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _register_comm(self):
        """
        Register the comm target using the correct Colab API.

        This sets up bidirectional communication with the frontend.
        The frontend will open a comm to this target to establish connection.
        """
        try:
            # Import here to fail gracefully if not in Colab
            from google.colab import output
        except ImportError:
            raise RuntimeError(
                "ColabCommsTransport requires google.colab. "
                "This transport can only be used inside Google Colab."
            )

        # Define the handler that will be called when frontend sends messages
        def comm_target_handler(comm, open_msg):
            """
            Handler called when frontend opens a comm to this target.

            Args:
                comm: The comm object for bidirectional communication
                open_msg: The initial message from frontend
            """
            logger.debug(f"Frontend opened Colab comm to {self._target_name}")

            # Store the comm object
            self._comm = comm

            # Register message handler for incoming messages
            def on_msg(msg):
                """Handle incoming messages from frontend."""
                try:
                    from ...utils import deserialize

                    # Extract the data from the message
                    # Colab sends messages as {'data': <content>}
                    msg_data = msg.get('data', msg)

                    # If it's a string, deserialize it using Bokeh's deserializer
                    if isinstance(msg_data, str):
                        data = deserialize(msg_data)
                    else:
                        # Already deserialized (shouldn't normally happen)
                        data = msg_data

                    logger.debug(
                        f"Received Colab comm message: "
                        f"{data.get('type', 'unknown') if isinstance(data, dict) else type(data)}"
                    )

                    # Handle special message types
                    if isinstance(data, dict):
                        msg_type = data.get('type')

                        if msg_type == 'ping':
                            # Respond to ping
                            from ...utils import serialize
                            comm.send({'data': serialize({
                                'type': 'pong',
                                'comm_mgr_id': self._comm_mgr_id,
                                'timestamp': time.time()
                            })})
                            return

                        if msg_type in ('heartbeat', 'comm_opened', 'closing'):
                            logger.debug(f"Received {msg_type} message")
                            return

                    # Call the message callback if set (by CommMgr)
                    if self._message_callback is not None:
                        import inspect
                        if inspect.iscoroutinefunction(self._message_callback):
                            # Schedule on the running event loop
                            try:
                                loop = asyncio.get_event_loop()
                                asyncio.run_coroutine_threadsafe(
                                    self._message_callback(data), loop
                                )
                            except RuntimeError:
                                # No running loop — create a new one (last resort)
                                asyncio.run(self._message_callback(data))
                        else:
                            self._message_callback(data)
                    else:
                        logger.warning(
                            f"Received message but no callback set: {data}"
                        )

                except Exception as e:
                    logger.error(f"Error handling Colab comm message: {e}")
                    traceback.print_exc()
                    # Try to send error back to frontend
                    try:
                        from ...utils import serialize
                        comm.send({'data': serialize({
                            'type': 'error',
                            'error': str(e),
                            'traceback': traceback.format_exc()
                        })})
                    except Exception:
                        pass

            # Register close handler
            def on_close(msg):
                """Handle comm close from frontend."""
                logger.debug(f"Colab comm closed for {self._target_name}")
                self._registered = False
                self._should_run = False
                self._comm = None
                if self.abort:
                    self.abort(
                        RuntimeError(f"Colab comm closed for {self._comm_mgr_id}")
                    )

            comm.on_msg(on_msg)
            comm.on_close(on_close)

            # Send acknowledgment back to frontend
            from ...utils import serialize
            comm.send({'data': serialize({
                'type': 'comm_opened',
                'comm_mgr_id': self._comm_mgr_id,
                'backend_ready': True,
                'message': 'Colab comm established successfully'
            })})

        try:
            # Register the target with Colab's comm system
            # This makes the backend ready to receive comm connections
            output.register_comm_target(self._target_name, comm_target_handler)

            self._registered = True
            logger.debug(f"Registered Colab comm target: {self._target_name}")
        except Exception as e:
            logger.error(f"Error registering Colab comm target: {e}")
            raise

# ============================================================================
# Jupyter Comms Transport
# ============================================================================
class JupyterCommsTransport(TransportBase):
    def __init__(self, comm_mgr_id: str, abort: Optional[Callable] = None):
        logger.debug('------------------------------------------------------------------------------------------------------------------------')
        logger.debug ('JupyterCommsTransport.__init__')
        logger.debug('------------------------------------------------------------------------------------------------------------------------')
        super().__init__(comm_mgr_id, abort)
        self._comm: Optional[Comm] = None
        self._callback: Optional[Callable] = None
        self._connected = asyncio.Event()

    async def connect(self) -> None:
        logger.debug('------------------------------------------------------------------------------------------------------------------------')
        logger.debug ('JupyterCommsTransport.connect')
        logger.debug('------------------------------------------------------------------------------------------------------------------------')
        # 1. Register the target handler in the Python Kernel
        def _target_func(comm, open_msg):
            self._comm = comm
            @self._comm.on_msg
            def _recv(msg):
                if self._callback:
                    self._callback(msg['content']['data'])

            self._connected.set()
            self._comm.send({'status': 'connected'})

        get_ipython().kernel.comm_manager.register_target(self._comm_mgr_id, _target_func)

        # 2. Inject the JS to find the kernel and open the Comm from the frontend
        # (This uses your specific logic to "find" the kernel in JupyterLab)
        display(Javascript(f"""
            console.log("We're off to see the wizard!")
            (async () => {{
                const poll = setInterval(async () => {{
                    const panelEl = document.querySelector('.jp-NotebookPanel');
                    const syms = Object.getOwnPropertySymbols(panelEl ?? document.body);
                    let kernel = null;
                    for (const sym of syms) {{
                        const val = panelEl?.[sym];
                        if (val?.sessionContext?.session?.kernel) {{
                            kernel = val.sessionContext.session.kernel;
                            break;
                        }}
                    }}
                    if (!kernel) return;
                    clearInterval(poll);

                    const comm = kernel.createComm('{self._comm_mgr_id}');
                    comm.onMsg = (msg) => console.log('JS Received:', msg.content.data);
                    comm.open({{}});
                    window["comm_{self._comm_mgr_id}"] = comm;
                }}, 200);
            }})();
        """))

        # Wait for the JS to actually hit our _target_func
        await self._connected.wait()

    async def send_message(self, message: Dict[str, Any]) -> None:
        logger.debug('------------------------------------------------------------------------------------------------------------------------')
        logger.debug ('JupyterCommsTransport.send_message')
        logger.debug('------------------------------------------------------------------------------------------------------------------------')
        if self._comm:
            self._comm.send(message)

    def set_message_callback(self, callback: Callable[[Dict[str, Any]], None]):
        self._callback = callback

    async def run(self) -> None:
        """Keep the transport alive. In Jupyter, this is just a wait loop."""
        logger.debug('------------------------------------------------------------------------------------------------------------------------')
        logger.debug ('JupyterCommsTransport.run')
        logger.debug('------------------------------------------------------------------------------------------------------------------------')
        while self.is_connected():
            await asyncio.sleep(1)

    async def close(self) -> None:
        if self._comm:
            self._comm.close()
            self._comm = None
        self._connected.clear()

    def is_connected(self) -> bool:
        return self._comm is not None

class JupyterCommsTransportORIG(TransportBase):
    """
    Jupyter Comms-based transport for Classic Notebook and JupyterLab.

    Uses the kernel comm protocol for bidirectional communication between
    the Python kernel and the JavaScript frontend.

    Key features:
    - Works with Classic Notebook 6.x and JupyterLab 3.x / 4.x
    - Kernel can outlive browser connections
    - Supports reconnection to running kernel
    - Multiple frontends can connect
    - Efficient array serialization via Bokeh

    Usage:
        transport = JupyterCommsTransport('app_comms', abort=error_handler)
        transport.set_message_callback(route_message)
        await transport.connect()
        await transport.run()
        await transport.send_message({'data': computation_result})
    """

    
    def __init__(self, comm_mgr_id: str, abort: Optional[Callable] = None):
        super().__init__(comm_mgr_id, abort)
        self._comm = None
        self._message_callback: Optional[Callable] = None
        self._comm_manager = None
        self._target_name = f'cubevis_comm_mgr_{comm_mgr_id}'
        self._is_open = False
        self._should_run = False
        try:
            self._loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_event_loop()
        except RuntimeError:
            self._loop = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Public TransportBase interface
    # ------------------------------------------------------------------
    def set_message_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """
        Set callback for incoming messages (used by CommMgr).
        
        Args:
            callback: Async function to call when messages arrive
        """
        self._message_callback = callback
        logger.debug(f"Message callback set for {self._target_name}")
        
    async def connect(self) -> None:
        """
        Connect and create Jupyter comm.

        Creates a comm using the best available kernel comm implementation
        for the current environment (JupyterLab 4 / Classic Notebook 6).
        """
        self._loop = asyncio.get_event_loop()

        logger.info(f"Jupyter Comms connecting for comm_mgr: {self._comm_mgr_id}")

        try:
            from IPython import get_ipython
            ipython = get_ipython()
            if not ipython:
                raise RuntimeError("IPython kernel not available")

            kernel = ipython.kernel

            # Obtain the comm_manager — attribute name varies by ipykernel version
            comm_manager = None
            for attr in ('comm_manager', 'comm_info'):
                if hasattr(kernel, attr):
                    comm_manager = getattr(kernel, attr)
                    break

            # Newer ipykernel (≥ 6.15) uses `kernel.comm_manager` but as a
            # CommManager instance from the standalone `comm` package.
            # Also try the IPython kernel's internal comm_manager.
            if comm_manager is None:
                # Last-ditch: ipykernel exposes comm manager on the shell
                if hasattr(ipython, 'comm_manager'):
                    comm_manager = ipython.comm_manager

            if comm_manager is None:
                raise RuntimeError(
                    "Kernel does not have comm_manager. "
                    "Ensure ipykernel ≥ 5.x is installed."
                )

            self._comm_manager = comm_manager

            # Register a target so the frontend can open a comm back to us
            # (reconnection path, or when frontend initiates).
            def target_func(comm, open_msg):
                logger.info(
                    f"Backend received comm open from frontend: {self._target_name}"
                )
                self._handle_comm_open(comm, open_msg)

            comm_manager.register_target(self._target_name, target_func)
            logger.info(f"Registered Jupyter comm target: {self._target_name}")

            kind, comm_factory = _get_comm_class()

            if kind == "create_comm":
                # New standalone `comm` package API
                self._comm = comm_factory(
                    target_name=self._target_name,
                    data={
                        'type': 'initialization',
                        'comm_mgr_id': self._comm_mgr_id,
                        'backend_ready': True,
                    }
                )
                # `create_comm` returns an already-open comm — no .open() needed
                # but we must still call open() to send the comm_open message.
                self._comm.open(data={
                    'type': 'initialization',
                    'comm_mgr_id': self._comm_mgr_id,
                    'backend_ready': True,
                })
            else:
                # Legacy ipykernel.comm.Comm API
                self._comm = comm_factory(
                    target_name=self._target_name,
                    data={
                        'type': 'initialization',
                        'comm_mgr_id': self._comm_mgr_id,
                        'backend_ready': True,
                    }
                )
                # MUST call open() to actually send the comm_open
                # message to the frontend.  Without this the frontend never
                # receives the comm and the connection silently fails.
                self._comm.open(data={
                    'type': 'initialization',
                    'comm_mgr_id': self._comm_mgr_id,
                    'backend_ready': True,
                })

            # Register message handler
            @self._comm.on_msg
            def _on_msg(msg):
                self._handle_jupyter_message_sync(msg)

            # Register close handler
            @self._comm.on_close
            def _on_close(msg):
                self._handle_comm_close(msg)

            self._is_open = True
            logger.info(f"Jupyter comm opened: {self._target_name}")

            # Start heartbeat
            self._start_heartbeat()

        except Exception as e:
            logger.error(f"Error connecting Jupyter comm: {e}")
            if self.abort:
                self.abort(e)
            raise

    async def send_message(self, message: Dict[str, Any]) -> None:
        """
        Send a message through Jupyter Comm.
        
        Args:
            message: Dictionary message to send
            
        Raises:
            RuntimeError: If comm is not initialized
        """
        if self._comm is None or not self._is_open:
            raise RuntimeError(
                f"Jupyter Comm not initialized or closed for {self._comm_mgr_id}"
            )
            
        try:
            from ...utils import serialize
            serialized = serialize(message)
            
            # Jupyter comms expect a dict, wrap serialized data
            comm_msg = {
                'type': 'cubevis_message',
                'comm_mgr_id': self._comm_mgr_id,
                'data': serialized
            }
            self._comm.send(comm_msg)

            logger.debug(
                f"Sent message via Jupyter Comm {self._target_name} "
                f"({len(serialized)} bytes)"
            )
        except Exception as e:
            logger.error(f"Error sending message via Jupyter Comm: {e}")
            if self.abort:
                self.abort(e)
            raise
    
    async def run(self) -> None:
        """Keep event loop alive for Jupyter Comm callbacks."""
        self._loop = asyncio.get_event_loop()
        self._should_run = True
        logger.debug(f"Jupyter Comms event loop starting for {self._comm_mgr_id}")
        while self._should_run and self._is_open:
            await asyncio.sleep(0.1)
        logger.debug(f"Jupyter Comms event loop ended for {self._comm_mgr_id}")

    async def close(self) -> None:
        """Close the Jupyter Comm."""
        self._should_run = False
        self._stop_heartbeat()

        if self._comm is not None and self._is_open:
            try:
                from ...utils import serialize
                
                # Send a close notification before actually closing
                self._comm.send({
                    'type': 'cubevis_message',
                    'comm_mgr_id': self._comm_mgr_id,
                    'data': serialize({
                        'type': 'closing',
                        'comm_mgr_id': self._comm_mgr_id,
                        'message': 'Backend closing comm'
                    })
                })
                
                # Close the comm
                self._comm.close()
                logger.debug(f"Closed Jupyter comm for {self._target_name}")
            except Exception as e:
                logger.error(f"Error closing Jupyter comm: {e}")
            finally:
                self._comm = None
                self._is_open = False
        
        # Unregister the target so it doesn't receive stale connections
        if self._comm_manager is not None:
            try:
                targets = getattr(self._comm_manager, 'targets', {})
                if self._target_name in targets:
                    del targets[self._target_name]
                    logger.debug(
                        f"Unregistered comm target {self._target_name}"
                    )
            except Exception as e:
                logger.error(f"Error unregistering comm target: {e}")

    def is_connected(self) -> bool:
        """Check if Jupyter Comm is open."""
        return self._comm is not None and self._is_open
    
    def get_comm_id(self) -> Optional[str]:
        """
        Get the current comm ID for debugging / reconnection.
        
        Returns:
            The comm_id string or None if no comm exists
        """
        if self._comm is not None:
            return getattr(self._comm, 'comm_id', None)
        return None

    # ------------------------------------------------------------------
    # Internal callbacks
    # ------------------------------------------------------------------
    def _handle_comm_open(self, comm, open_msg):
        """Handle when frontend opens a comm to us (reconnection path)."""
        logger.info("Frontend opened comm (replacing existing if any)")

        # Close existing comm if any
        if self._comm and self._comm != comm:
            try:
                self._comm.close()
            except Exception:
                pass

        # Use the new comm
        self._comm = comm
        self._is_open = True

        # Register handlers
        @comm.on_msg
        def _on_msg(msg):
            self._handle_jupyter_message_sync(msg)

        @comm.on_close
        def _on_close(msg):
            self._handle_comm_close(msg)

        # Send acknowledgment
        comm.send({
            'type': 'comm_opened',
            'comm_mgr_id': self._comm_mgr_id,
            'backend_ready': True
        })

    def _handle_jupyter_message_sync(self, msg):
        """
        Handle incoming message from Jupyter comm (sync callback context).
        """
        from ...utils import deserialize

        try:
            # Jupyter comm messages have nested structure:
            # msg = {'content': {'data': {...}}, ...}
            content = msg.get('content', {})
            data_wrapper = content.get('data', {})

            # Our wrapper puts the Bokeh-serialized bytes in data_wrapper['data']
            serialized_data = data_wrapper.get('data')

            if serialized_data:
                if isinstance(serialized_data, str):
                    data = deserialize(serialized_data)
                else:
                    data = serialized_data
            elif data_wrapper.get('type'):
                # Direct message (no inner serialization)
                data = data_wrapper
            else:
                logger.warning(f"Unexpected message structure: {msg}")
                return

            # Handle special messages
            msg_type = data.get('type') if isinstance(data, dict) else None

            if msg_type == 'ping':
                from ...utils import serialize
                if self._comm:
                    self._comm.send({
                        'type': 'cubevis_message',
                        'comm_mgr_id': self._comm_mgr_id,
                        'data': serialize({'type': 'pong', 'timestamp': time.time()})
                    })
                return

            if msg_type in ('heartbeat', 'comm_opened', 'closing'):
                logger.debug(f"Received {msg_type} message")
                return

            # Call the message callback
            if self._message_callback:
                import inspect
                if inspect.iscoroutinefunction(self._message_callback):
                    # FIX [5]: schedule on the captured event loop rather than
                    # using create_task() which requires the current thread to
                    # have a running loop.
                    loop = self._loop
                    if loop is not None and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self._message_callback(data), loop
                        )
                    else:
                        # Fallback: try get_event_loop() from this thread
                        try:
                            current_loop = asyncio.get_event_loop()
                            if current_loop.is_running():
                                asyncio.run_coroutine_threadsafe(
                                    self._message_callback(data), current_loop
                                )
                            else:
                                current_loop.run_until_complete(
                                    self._message_callback(data)
                                )
                        except RuntimeError as e:
                            logger.error(
                                f"Cannot schedule async callback — no running "
                                f"event loop: {e}"
                            )
                else:
                    self._message_callback(data)

        except Exception as e:
            logger.error(f"Error handling Jupyter message: {e}")
            traceback.print_exc()

    def _handle_comm_close(self, msg):
        """Handle comm close from frontend."""
        logger.info("Jupyter comm closed")
        self._is_open = False
        self._should_run = False
        self._stop_heartbeat()
        self._comm = None

    def _start_heartbeat(self, interval: float = 30.0):
        """Start sending periodic heartbeats to the frontend."""
        from ...utils import serialize

        async def heartbeat_loop():
            while self._is_open:
                try:
                    if self.is_connected():
                        self._comm.send({
                            'type': 'cubevis_message',
                            'comm_mgr_id': self._comm_mgr_id,
                            'data': serialize({
                                'type': 'heartbeat',
                                'timestamp': time.time()
                            })
                        })
                except Exception:
                    pass  # Heartbeat failure is non-fatal
                await asyncio.sleep(interval)

        self._heartbeat_task = asyncio.create_task(heartbeat_loop())

    def _stop_heartbeat(self):
        """Stop the heartbeat task."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
