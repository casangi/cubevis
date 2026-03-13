"""
Jupyter Comm transport for DataPipe.

This transport uses Jupyter's comm system for communication, enabling
DataPipe to work in Jupyter environments and support remote kernels.
"""

import logging
import asyncio
import threading
import time
from typing import Dict, Optional, Callable, Any

from .transport import DataPipeTransport, TransportMessage

logger = logging.getLogger(__name__)

class JupyterCommTransport(DataPipeTransport):
    """
    Transport that uses Jupyter kernel comm system.
    
    This allows DataPipe to communicate through Jupyter's existing
    infrastructure, which provides:
    - Authentication via Jupyter
    - Automatic proxy through JupyterHub
    - Support for remote kernels
    - Persistence across browser reconnections
    
    Attributes
    ----------
    session_id : str
        Unique identifier for this comm session
    comm_manager : CommManager
        Jupyter comm manager from the kernel
    """
    
    def __init__(
        self,
        session_id: str,
        comm_manager: Any = None,
        abort: Optional[Callable] = None
    ):
        """
        Initialize Jupyter comm transport.
        
        Parameters
        ----------
        session_id : str
            Unique session identifier
        comm_manager : CommManager, optional
            Jupyter comm manager. If None, will try to get from IPython
        abort : callable, optional
            Function to call on fatal errors
        """
        self.session_id = session_id
        self._abort = abort
        self._pipes: Dict[str, 'DataPipe'] = {}
        self._pipe_lock = threading.Lock()
        
        # Get comm manager
        if comm_manager is None:
            comm_manager = self._get_comm_manager()
        self.comm_manager = comm_manager
        
        # Comm instance (created on first use)
        self._comm = None
        self._comm_id = f'iclean_{session_id}'
        self._connected = False
        
        # Message queue for before comm is fully initialized
        self._message_queue = []
        self._comm_ready = False
    
    def _get_comm_manager(self):
        """Get the comm manager from IPython kernel"""
        try:
            from IPython import get_ipython
            ipython = get_ipython()
            
            if ipython is None:
                raise RuntimeError("Not running in IPython environment")
            
            if not hasattr(ipython, 'kernel'):
                raise RuntimeError("IPython kernel not available")
            
            return ipython.kernel.comm_manager
        except ImportError:
            raise RuntimeError("IPython not available")
    
    def register_pipe(self, pipe_id: str, pipe: 'DataPipe') -> None:
        """Register a DataPipe with this transport"""
        with self._pipe_lock:
            self._pipes[pipe_id] = pipe
    
    def unregister_pipe(self, pipe_id: str) -> None:
        """Unregister a DataPipe from this transport"""
        with self._pipe_lock:
            if pipe_id in self._pipes:
                del self._pipes[pipe_id]
    
    async def send(self, pipe_id: str, message: dict) -> None:
        """
        Send a message from a DataPipe to frontend via Jupyter comm.
        
        Parameters
        ----------
        pipe_id : str
            ID of the DataPipe sending this message
        message : dict
            Message payload
        """
        if not self._comm_ready:
            # Queue message until comm is ready
            self._message_queue.append((pipe_id, message))
            return
        
        # Wrap message with pipe_id for multiplexing
        transport_msg = {
            'session_id': self.session_id,
            'pipe_id': pipe_id,
            'payload': message
        }
        
        # Send via comm
        if self._comm is not None:
            self._comm.send(transport_msg)
    
    async def start(self) -> None:
        """Initialize the Jupyter comm"""
        if self._comm is not None:
            return
        
        # Register comm target handler
        self.comm_manager.register_target(
            'iclean_datapipe',
            self._handle_comm_open
        )
        
        # Create or get existing comm
        try:
            # Try to get existing comm
            if self._comm_id in self.comm_manager.comms:
                self._comm = self.comm_manager.comms[self._comm_id]
            else:
                # Create new comm
                self._comm = self.comm_manager.new_comm(
                    target_name='iclean_datapipe',
                    comm_id=self._comm_id,
                    data={
                        'session_id': self.session_id,
                        'action': 'init'
                    }
                )
            
            # Register message handler
            self._comm.on_msg(self._handle_comm_message)
            self._comm.on_close(self._handle_comm_close)
            
            self._comm_ready = True
            self._connected = True
            
            # Send any queued messages
            await self._flush_message_queue()
            
            # Register session in extension
            self._register_session()
            
        except Exception as e:
            print(f"Error initializing Jupyter comm: {e}")
            if self._abort:
                self._abort(e)
            raise
    
    async def stop(self) -> None:
        """Close the Jupyter comm"""
        if self._comm is not None:
            try:
                self._comm.close()
            except Exception as e:
                print(f"Error closing comm: {e}")
            finally:
                self._comm = None
                self._comm_ready = False
                self._connected = False
    
    def is_connected(self) -> bool:
        """Check if transport is connected"""
        return self._connected and self._comm_ready
    
    def _handle_comm_open(self, comm, open_msg):
        """
        Handle new comm connection from frontend.
        
        This is called when a frontend initiates a comm connection.
        """
        data = open_msg.get('content', {}).get('data', {})
        session_id = data.get('session_id')
        
        if session_id == self.session_id:
            self._comm = comm
            self._comm.on_msg(self._handle_comm_message)
            self._comm.on_close(self._handle_comm_close)
            self._comm_ready = True
            self._connected = True
            
            # Flush queued messages
            asyncio.create_task(self._flush_message_queue())
    
    def _handle_comm_message(self, msg):
        """
        Handle incoming message from frontend via comm.
        
        Parameters
        ----------
        msg : dict
            Jupyter comm message
        """
        try:
            content = msg.get('content', {})
            data = content.get('data', {})
            
            # Extract routing info
            pipe_id = data.get('pipe_id')
            payload = data.get('payload', {})
            
            if not pipe_id:
                logger.warning( f"Comm message missing pipe_id: {data}" )
                return
            
            # Route to appropriate DataPipe
            asyncio.create_task(self._route_message(pipe_id, payload))
            
        except Exception as e:
            print(f"Error handling comm message: {e}")
            import traceback
            traceback.print_exc()
    
    def _handle_comm_close(self, msg):
        """Handle comm close event"""
        print(f"Comm closed for session {self.session_id}")
        self._comm_ready = False
        self._connected = False
    
    async def _route_message(self, pipe_id: str, message: dict) -> None:
        """
        Route a message to the appropriate DataPipe.
        
        Parameters
        ----------
        pipe_id : str
            ID of target DataPipe
        message : dict
            Message to route
        """
        with self._pipe_lock:
            if pipe_id not in self._pipes:
                print(f"No pipe registered with ID {pipe_id}\nfor message: {message}")
                return
            
            pipe = self._pipes[pipe_id]
        
        # Let the DataPipe handle its message
        await pipe._handle_transport_message(message)
    
    async def _flush_message_queue(self) -> None:
        """Send any messages that were queued before comm was ready"""
        while self._message_queue:
            pipe_id, message = self._message_queue.pop(0)
            await self.send(pipe_id, message)
    
    def _register_session(self) -> None:
        """
        Register this session with the Jupyter extension.
        
        This allows the extension to route WebSocket connections
        to the correct kernel/comm.
        """
        try:
            from IPython import get_ipython
            ipython = get_ipython()
            
            if ipython is not None and hasattr(ipython, 'kernel'):
                kernel_id = ipython.kernel.session.session
                
                # Import and use session registry
                try:
                    from iclean.jupyter_extension.session_registry import register_session
                    register_session(self.session_id, kernel_id)
                except ImportError:
                    # Extension not installed - that's okay for pure Jupyter mode
                    pass
        except Exception as e:
            logger.warning( f"Could not register session: {e}" )


class JupyterCommMultiplexer:
    """
    Helper class to manage multiple comms in a single kernel.
    
    This is useful when you have multiple independent DataPipe sessions
    in the same kernel (e.g., multiple notebooks connected to same kernel).
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._transports: Dict[str, JupyterCommTransport] = {}
        return cls._instance
    
    def get_or_create_transport(
        self,
        session_id: str,
        comm_manager: Any = None,
        abort: Optional[Callable] = None
    ) -> JupyterCommTransport:
        """
        Get or create a Jupyter comm transport for a session.
        
        Parameters
        ----------
        session_id : str
            Unique session identifier
        comm_manager : CommManager, optional
            Jupyter comm manager
        abort : callable, optional
            Error handler
            
        Returns
        -------
        JupyterCommTransport
            Transport instance for this session
        """
        if session_id not in self._transports:
            self._transports[session_id] = JupyterCommTransport(
                session_id=session_id,
                comm_manager=comm_manager,
                abort=abort
            )
        
        return self._transports[session_id]
    
    def remove_transport(self, session_id: str) -> None:
        """Remove a transport instance"""
        if session_id in self._transports:
            del self._transports[session_id]
