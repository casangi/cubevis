"""
Transport abstraction layer for DataPipe communication.

This module provides a pluggable transport system that allows DataPipe
to use different communication mechanisms:
- Direct WebSocket (original implementation)
- Multiplexed WebSocket (multiple DataPipes over one connection)
- Jupyter Comm (communication via Jupyter kernel comm system)
"""

from abc import ABC, abstractmethod
from typing import Dict, Tuple, Optional, Callable, Any
import asyncio
import threading
import time
import uuid
from ....utils import serialize, deserialize


class DataPipeTransport(ABC):
    """
    Abstract base class for DataPipe transport mechanisms.
    
    A transport handles the actual communication between Python backend
    and JavaScript frontend, allowing DataPipe to be agnostic about
    the underlying mechanism.
    """
    
    @abstractmethod
    async def send(self, pipe_id: str, message: dict) -> None:
        """
        Send a message to the frontend.
        
        Parameters
        ----------
        pipe_id : str
            Unique identifier for the DataPipe sending this message
        message : dict
            Message payload to send
        """
        pass
    
    @abstractmethod
    def register_pipe(self, pipe_id: str, pipe: 'DataPipe') -> None:
        """
        Register a DataPipe instance with this transport.
        
        Parameters
        ----------
        pipe_id : str
            Unique identifier for this DataPipe
        pipe : DataPipe
            The DataPipe instance to register
        """
        pass
    
    @abstractmethod
    def unregister_pipe(self, pipe_id: str) -> None:
        """
        Unregister a DataPipe instance from this transport.
        
        Parameters
        ----------
        pipe_id : str
            Unique identifier for the DataPipe to unregister
        """
        pass
    
    @abstractmethod
    async def start(self) -> None:
        """Start the transport (establish connections, etc.)"""
        pass
    
    @abstractmethod
    async def stop(self) -> None:
        """Stop the transport (close connections, cleanup)"""
        pass
    
    @abstractmethod
    def is_connected(self) -> bool:
        """Check if transport is currently connected"""
        pass


class TransportManager:
    """
    Singleton manager for transport instances.
    
    This ensures that multiple DataPipes can share the same transport
    when appropriate (e.g., multiplexed WebSocket or Jupyter comm).
    """
    
    _instance: Optional['TransportManager'] = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._transports: Dict[str, DataPipeTransport] = {}
                    cls._instance._transport_locks: Dict[str, asyncio.Lock] = {}
        return cls._instance
    
    def get_transport(
        self,
        transport_key: str,
        transport_type: str,
        create_func: Optional[Callable[[], DataPipeTransport]] = None,
        validate_address: bool = True,
        address: Optional[Tuple[str, int]] = None,
        **kwargs
    ) -> DataPipeTransport:
        """
        Get or create a transport instance.
        
        Parameters
        ----------
        transport_key : str
            Unique key identifying this transport instance
        transport_type : str
            Type of transport ('direct', 'multiplexed', 'jupyter')
        create_func : callable, optional
            Function to create transport if it doesn't exist
        validate_address : bool
            If True, validate that address matches for shared transports
        address : tuple of (str, int), optional
            Network address - used for validation
        **kwargs
            Additional arguments for transport creation
            
        Returns
        -------
        DataPipeTransport
            The transport instance
            
        Raises
        ------
        ValueError
            If address validation fails for shared transport
        """
        # Check if transport already exists
        if transport_key in self._transports:
            existing_transport = self._transports[transport_key]
            
            # Validate address if requested
            if validate_address and address is not None:
                # Check if transport has an address attribute
                if hasattr(existing_transport, 'address'):
                    existing_address = existing_transport.address
                    if existing_address != address:
                        raise ValueError(
                            f"Transport key '{transport_key}' already exists with "
                            f"address {existing_address}, but new DataPipe requests "
                            f"address {address}.\n\n"
                            f"When sharing transports (same transport_key), all DataPipes "
                            f"must use the same address.\n\n"
                            f"Solutions:\n"
                            f"  1. Use the same address: address={existing_address}\n"
                            f"  2. Use a different transport_key for different addresses\n"
                            f"  3. Let transport_key auto-generate from address (omit transport_key)\n"
                            f"  4. Disable validation: get_transport(..., validate_address=False)"
                        )
            
            return existing_transport
        
        # Create new transport
        if create_func is not None:
            transport = create_func(**kwargs)
        else:
            transport = self._create_transport(transport_type, address=address, **kwargs)
        
        self._transports[transport_key] = transport
        self._transport_locks[transport_key] = asyncio.Lock()
        
        return transport
    
    def _create_transport(self, transport_type: str, **kwargs) -> DataPipeTransport:
        """Create a transport based on type string"""
        if transport_type == 'direct':
            from .direct_websocket import DirectWebSocketTransport
            return DirectWebSocketTransport(**kwargs)
        elif transport_type == 'multiplexed':
            from .multiplexed_transport import MultiplexedWebSocketTransport
            return MultiplexedWebSocketTransport(**kwargs)
        elif transport_type == 'jupyter':
            from .jupyter_comm import JupyterCommTransport
            return JupyterCommTransport(**kwargs)
        else:
            raise ValueError(f"Unknown transport type: {transport_type}")
    
    def remove_transport(self, transport_key: str) -> None:
        """Remove a transport instance"""
        if transport_key in self._transports:
            del self._transports[transport_key]
            del self._transport_locks[transport_key]
    
    def get_lock(self, transport_key: str) -> asyncio.Lock:
        """Get the lock for a specific transport"""
        if transport_key not in self._transport_locks:
            self._transport_locks[transport_key] = asyncio.Lock()
        return self._transport_locks[transport_key]


class TransportMessage:
    """
    Standard message format for transport layer.
    
    This provides a consistent message structure across all transport types.
    """
    
    def __init__(
        self,
        pipe_id: str,
        msg_id: str,
        direction: str,
        message: dict,
        session_id: Optional[str] = None
    ):
        self.pipe_id = pipe_id
        self.msg_id = msg_id
        self.direction = direction  # 'p2j' or 'j2p'
        self.message = message
        self.session_id = session_id
        self.timestamp = time.time()
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization"""
        return {
            'pipe_id': self.pipe_id,
            'id': self.msg_id,
            'direction': self.direction,
            'message': self.message,
            'session': self.session_id,
            'timestamp': self.timestamp
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'TransportMessage':
        """Create from dictionary"""
        return cls(
            pipe_id=data.get('pipe_id', ''),
            msg_id=data.get('id', ''),
            direction=data.get('direction', ''),
            message=data.get('message', {}),
            session_id=data.get('session')
        )
    
    def serialize(self) -> str:
        """Serialize message for transmission"""
        return serialize(self.to_dict())
    
    @classmethod
    def deserialize(cls, data: str) -> 'TransportMessage':
        """Deserialize message from transmission"""
        return cls.from_dict(deserialize(data))


def generate_transport_key(
    transport_type: str,
    address: Optional[Tuple[str, int]] = None,
    session_id: Optional[str] = None,
    user_key: Optional[str] = None,
    **kwargs
) -> str:
    """
    Generate a unique key for a transport instance.
    
    If user_key is provided, it's used directly. Otherwise, key is
    auto-generated from address and other parameters to enable
    automatic transport sharing.
    
    Parameters
    ----------
    transport_type : str
        Type of transport
    address : tuple of (str, int), optional
        Network address (host, port)
    session_id : str, optional
        Session identifier
    user_key : str, optional
        User-provided explicit key (takes precedence)
    **kwargs
        Additional key components
        
    Returns
    -------
    str
        Unique transport key
    """
    # If user provided explicit key, use it
    if user_key:
        return user_key
    
    components = [transport_type]
    
    # For WebSocket-based transports, include address
    # This enables automatic sharing when same address is used
    if address is not None and transport_type in ('direct', 'multiplexed'):
        components.append(f"{address[0]}_{address[1]}")
    
    # For Jupyter transport, use session_id
    if session_id is not None and transport_type == 'jupyter':
        components.append(session_id)
    
    # Include any additional distinguishing factors
    for key, value in sorted(kwargs.items()):
        components.append(f"{key}_{value}")
    
    return "__".join(components)
