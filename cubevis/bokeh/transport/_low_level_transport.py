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

class ShutdownReason(Enum):
    REQUESTED = "shutdown_requested"      # User called requestShutdown()
    TRANSPORT_CLOSED = "transport_closed" # WebSocket/conn closed
    ERROR = "error"                       # Fatal error occurred
    NORMAL = "normal"                     # Normal exit


logger = logging.getLogger(__name__)


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
    This transport now handles:
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
            logger.info(f"WebSocket waiting for initialization (comm_mgr_id={self._comm_mgr_id})")
            
            # Wait for initialization message
            init_message = await self.websocket.recv()
            msg = deserialize(init_message)
            
            if msg.get('id') == 'initialize' and msg.get('direction') == 'j2p':
                # Extract connection info
                frontend_id = msg.get('frontend_id')
                backend_id = msg.get('backend_id')
                received_comm_mgr_id = msg.get('comm_mgr_id')
                
                logger.info(
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
                            logger.info(f"Set frontend_id: {frontend_id}")
                
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
                logger.info(f"WebSocket initialized for comm_mgr_id={self._comm_mgr_id}")
                
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
        
        Iterates over incoming messages and calls the message callback.
        Blocks until connection closes or error occurs.
        """
        if not self._initialized:
            raise RuntimeError("Must call connect() before run()")
        
        if not self._message_callback:
            raise RuntimeError("Must call set_message_callback() before run()")
        
        from ...utils import deserialize
        
        try:
            logger.info(f"WebSocket event loop starting for {self._comm_mgr_id}")
            
            # Iterate over incoming messages
            async for message in self.websocket:
                try:
                    msg = deserialize(message)
                    
                    # Call the callback (set by CommMgr)
                    await self._message_callback(msg)
                    
                except Exception as e:
                    logger.error(f"Error processing WebSocket message: {e}")
                    if self.abort:
                        self.abort(e)
                    # Continue processing other messages
            
            logger.info(f"WebSocket event loop ended for {self._comm_mgr_id}")
            
        except Exception as e:
            logger.error(f"WebSocket event loop error: {e}")
            if self.abort:
                self.abort(e)
            raise
        finally:
            self._connected = False
    
    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self._connected:
            try:
                await self.websocket.close()
                logger.info(f"Closed WebSocket for {self._comm_mgr_id}")
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
    
    Uses google.colab.kernel.comms for efficient bidirectional communication.
    Leverages Bokeh's serialization for numpy arrays and complex data structures.
    
    Key features:
    - Uses Colab's native comm protocol (not eval_js)
    - Handles large data efficiently (images, arrays)
    - Automatic serialization via Bokeh
    - Callback-based message reception
    
    Usage:
        transport = ColabCommsTransport('app_comms', abort=error_handler)
        transport.set_message_callback(route_message)
        await transport._register_comm()
        await transport.send_message({'data': large_array})
    """
    
    def __init__(self, comm_mgr_id: str, abort: Optional[Callable] = None):
        super().__init__(comm_mgr_id, abort)
        self._comm = None
        self._registered = False
        self._message_callback: Optional[Callable] = None
        self._target_name = f'cubevis_datapipe_{comm_mgr_id}'
        
    async def connect(self) -> None:
        """Register comm target."""
        # Existing _register_comm logic
        pass
    
    async def run(self) -> None:
        """Keep event loop alive for callbacks."""
        # Keep alive until shutdown
        while self._registered:
            await asyncio.sleep(0.1)
    
    def set_message_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """
        Set callback for incoming messages.
        
        The callback will be called with deserialized message data.
        It should be an async function that handles routing.
        
        Args:
            callback: Async function to call when messages arrive
        """
        self._message_callback = callback
        logger.debug(f"Message callback set for {self._target_name}")
        
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
            await self._register_comm()
        
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
    
    async def _register_comm(self):
        """
        Register the comm target for Colab comms.
        
        This sets up bidirectional communication with the frontend.
        The frontend will open a comm to this target to establish connection.
        """
        try:
            # Import here to fail gracefully if not in Colab
            from google.colab import output
            
            # Define the handler that will be called when frontend sends messages
            def comm_target_handler(comm, open_msg):
                """
                Handler called when frontend opens a comm to this target.
                
                Args:
                    comm: The comm object for bidirectional communication
                    open_msg: The initial message from frontend
                """
                logger.info(
                    f"Frontend opened Colab comm to {self._target_name}"
                )
                
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
                            
                            if msg_type == 'heartbeat':
                                logger.debug(f"Heartbeat received for {self._comm_mgr_id}")
                                return
                            
                            if msg_type == 'comm_opened':
                                # Frontend acknowledging connection
                                logger.info('Frontend acknowledged comm connection')
                                return
                        
                        # Call the message callback if set (by CommMgr)
                        if self._message_callback is not None:
                            # Bridge sync callback to async
                            asyncio.create_task(self._message_callback(data))
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
                        except:
                            pass
                
                # Register the message handler
                comm.on_msg(on_msg)
                
                # Register close handler
                def on_close(msg):
                    """Handle comm close."""
                    logger.info(f"Colab comm closed for {self._target_name}")
                    self._registered = False
                    self._comm = None
                    if self.abort:
                        self.abort(
                            RuntimeError(f"Colab comm closed for {self._comm_mgr_id}")
                        )
                
                comm.on_close(on_close)
                
                # Send acknowledgment back to frontend
                from ...utils import serialize
                comm.send({'data': serialize({
                    'type': 'comm_opened',
                    'comm_mgr_id': self._comm_mgr_id,
                    'backend_ready': True,
                    'message': 'Colab comm established successfully'
                })})
            
            # Register the target with Colab's comm system
            # This makes the backend ready to receive comm connections
            output.register_comm_target(self._target_name, comm_target_handler)
            
            self._registered = True
            logger.info(
                f"Registered Colab comm target: {self._target_name}"
            )
            
        except ImportError:
            raise RuntimeError(
                "ColabCommsTransport requires google.colab package"
            )
        except Exception as e:
            logger.error(f"Error registering Colab comm target: {e}")
            raise
    
    async def close(self) -> None:
        """Close the Colab Comms connection."""
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
                logger.info(f"Closed Colab comm for {self._target_name}")
            except Exception as e:
                logger.error(f"Error closing Colab comm: {e}")
            finally:
                self._comm = None
        
        self._registered = False
    
    def is_connected(self) -> bool:
        """Check if Colab Comms is registered and has active comm."""
        return self._registered and self._comm is not None


# ============================================================================
# Jupyter Comms Transport
# ============================================================================
class JupyterCommsTransport(TransportBase):
    """
    Jupyter Comms-based transport for remote kernel execution.
    
    This transport enables:
    - Remote kernel execution without a notebook UI
    - Session persistence across disconnects/reconnects
    - Multi-client connection to the same kernel
    - Stateful communication that survives browser sessions
    
    Uses ipykernel.comm.Comm for bidirectional communication.
    
    Key features:
    - True Jupyter comm protocol (not websockets)
    - Kernel can outlive browser connections
    - Supports reconnection to running kernel
    - Multiple frontends can connect
    - Efficient array serialization via Bokeh
    
    Usage:
        transport = JupyterCommsTransport('app_comms', abort=error_handler)
        transport.set_message_callback(route_message)
        await transport._create_comm()
        await transport.send_message({'data': computation_result})
    """
    
    def __init__(self, comm_mgr_id: str, abort: Optional[Callable] = None):
        super().__init__(comm_mgr_id, abort)
        self._comm = None
        self._message_callback: Optional[Callable] = None
        self._comm_manager = None
        self._target_name = f'cubevis_datapipe_{comm_mgr_id}'
        self._is_open = False
        
    async def connect(self) -> None:
        """Create comm."""
        # Existing _create_comm logic
        pass
    
    async def run(self) -> None:
        """Keep event loop alive for callbacks."""
        # Keep alive until shutdown
        while self._is_open:
            await asyncio.sleep(0.1)
    
    def set_message_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """
        Set callback for incoming messages (used by CommMgr).
        
        Args:
            callback: Async function to call when messages arrive
        """
        self._message_callback = callback
        logger.debug(f"Message callback set for {self._target_name}")
        
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
            
            # Serialize using Bokeh's serialization
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
    
    async def _create_comm(self):
        """
        Create a Jupyter comm target.
        
        This initializes the comm on the kernel side. The frontend must
        open a matching comm with the same target_name to establish
        bidirectional communication.
        """
        try:
            from ipykernel.comm import Comm
            
            # Get the comm manager from the current kernel
            # This requires running in an IPython kernel context
            try:
                from IPython import get_ipython
                ipython = get_ipython()
                if ipython is None:
                    raise RuntimeError(
                        "Not running in IPython kernel - "
                        "JupyterCommsTransport requires an active kernel"
                    )
                    
                # Get the kernel's comm manager
                if hasattr(ipython, 'kernel'):
                    self._comm_manager = ipython.kernel.comm_manager
                else:
                    raise RuntimeError(
                        "IPython instance has no kernel - "
                        "cannot access comm manager"
                    )
            except ImportError:
                raise RuntimeError(
                    "IPython not available - "
                    "JupyterCommsTransport requires IPython kernel"
                )
            
            # Create the comm
            # Note: We create it on the kernel side, frontend will open it
            self._comm = Comm(
                target_name=self._target_name,
                data={
                    'comm_mgr_id': self._comm_mgr_id,
                    'type': 'initialization',
                    'backend_ready': True
                },
                comm_manager=self._comm_manager
            )
            
            # Register message handler
            self._comm.on_msg(self._handle_jupyter_message_sync)
            
            # Register close handler
            self._comm.on_close(self._handle_comm_close)
            
            self._is_open = True
            
            logger.info(
                f"Created Jupyter comm: {self._target_name} "
                f"(comm_id: {self._comm.comm_id})"
            )
            
            # Also register a comm_open handler for reconnections
            # This allows frontend to reconnect to existing kernel
            self._comm_manager.register_target(
                self._target_name,
                self._handle_comm_open
            )
            
        except ImportError as e:
            raise RuntimeError(
                f"JupyterCommsTransport requires ipykernel package: {e}"
            )
        except Exception as e:
            logger.error(f"Error creating Jupyter comm: {e}")
            raise
    
    def _handle_comm_open(self, comm, open_msg):
        """
        Handle when frontend opens a comm to this target.
        
        This is called when a frontend connects or reconnects to the kernel.
        
        Args:
            comm: The Comm object created by the frontend
            open_msg: The comm_open message from frontend
        """
        logger.info(
            f"Frontend opened comm to {self._target_name} "
            f"(comm_id: {comm.comm_id})"
        )
        
        # If we already have a comm, close the old one
        if self._comm is not None and self._comm.comm_id != comm.comm_id:
            logger.info(
                f"Replacing old comm {self._comm.comm_id} "
                f"with new comm {comm.comm_id}"
            )
            try:
                self._comm.close()
            except:
                pass
        
        # Use the new comm
        self._comm = comm
        self._is_open = True
        
        # Register handlers on the new comm
        self._comm.on_msg(self._handle_jupyter_message_sync)
        self._comm.on_close(self._handle_comm_close)
        
        # Send acknowledgment
        from ...utils import serialize
        self._comm.send({
            'type': 'cubevis_message',
            'comm_mgr_id': self._comm_mgr_id,
            'data': serialize({
                'type': 'comm_opened',
                'comm_mgr_id': self._comm_mgr_id,
                'backend_ready': True,
                'message': 'Comm established successfully'
            })
        })
    
    def _handle_jupyter_message_sync(self, msg: Dict[str, Any]):
        """
        Handle incoming message from Jupyter comm.
        
        Jupyter's on_msg expects a sync function, but we need to call
        async methods. This bridges the gap.
        
        Args:
            msg: Jupyter comm message (has specific structure)
        """
        try:
            from ...utils import deserialize
            
            # Jupyter comm messages have this structure:
            # {
            #   'content': {
            #     'data': <our actual data>,
            #     'comm_id': '...',
            #     ...
            #   },
            #   'header': {...},
            #   'metadata': {...},
            #   ...
            # }
            
            # Extract our data
            content = msg.get('content', {})
            data_wrapper = content.get('data', {})
            
            # Get the serialized data
            serialized_data = data_wrapper.get('data')
            
            if serialized_data is None:
                # Check if it's wrapped differently
                if 'type' in data_wrapper:
                    # Direct message (not wrapped)
                    data = data_wrapper
                else:
                    logger.warning(f"Jupyter comm message missing 'data' field: {data_wrapper}")
                    return
            else:
                # Deserialize using Bokeh's deserializer
                if isinstance(serialized_data, str):
                    data = deserialize(serialized_data)
                else:
                    data = serialized_data
            
            logger.debug(f"Received Jupyter comm message: {data.get('type', 'unknown')}")
            
            # Handle special message types
            msg_type = data.get('type') if isinstance(data, dict) else None
            
            if msg_type == 'ping':
                # Respond to ping with pong
                if self._comm:
                    from ...utils import serialize
                    self._comm.send({
                        'type': 'cubevis_message',
                        'comm_mgr_id': self._comm_mgr_id,
                        'data': serialize({
                            'type': 'pong',
                            'comm_mgr_id': self._comm_mgr_id,
                            'timestamp': time.time()
                        })
                    })
                return
            
            elif msg_type == 'heartbeat':
                # Update timestamp for keep-alive
                logger.debug(f"Heartbeat received for {self._comm_mgr_id}")
                return
            
            elif msg_type == 'comm_opened':
                # Frontend acknowledging our connection
                logger.info('Frontend acknowledged comm connection')
                return
            
            # Call the message callback if set (by CommMgr)
            if self._message_callback is not None:
                # Bridge sync callback to async
                # Get the current event loop or create one
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    # No event loop in current thread
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                
                # Schedule the async callback
                asyncio.create_task(self._message_callback(data))
            else:
                logger.warning(
                    f"Received message but no callback set for {self._comm_mgr_id}: "
                    f"{data}"
                )
                
        except Exception as e:
            logger.error(f"Error handling Jupyter comm message: {e}")
            traceback.print_exc()
            if self.abort:
                self.abort(e)
    
    def _handle_comm_close(self, msg: Dict[str, Any]):
        """
        Handle when the comm is closed.
        
        Args:
            msg: The comm_close message
        """
        logger.info(f"Jupyter comm closed for {self._target_name}")
        self._is_open = False
        
        # Notify via abort callback
        if self.abort:
            self.abort(RuntimeError(f"Jupyter comm closed for {self._comm_mgr_id}"))
    
    async def close(self) -> None:
        """Close the Jupyter Comm."""
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
                logger.info(f"Closed Jupyter comm for {self._target_name}")
            except Exception as e:
                logger.error(f"Error closing Jupyter comm: {e}")
            finally:
                self._comm = None
                self._is_open = False
        
        # Unregister the target
        if self._comm_manager is not None and self._target_name in self._comm_manager.targets:
            try:
                # Note: ipykernel doesn't provide a clean unregister method
                # We just remove it from the dict
                del self._comm_manager.targets[self._target_name]
                logger.debug(f"Unregistered comm target {self._target_name}")
            except Exception as e:
                logger.error(f"Error unregistering comm target: {e}")
    
    def is_connected(self) -> bool:
        """Check if Jupyter Comm is open."""
        return self._comm is not None and self._is_open
    
    def get_comm_id(self) -> Optional[str]:
        """
        Get the current comm ID.
        
        Useful for debugging and reconnection scenarios.
        
        Returns:
            The comm_id string or None if no comm exists
        """
        if self._comm is not None:
            return self._comm.comm_id
        return None
