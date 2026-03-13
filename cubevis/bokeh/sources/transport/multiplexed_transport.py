"""
Multiplexed WebSocket transport for DataPipe.

This transport allows multiple DataPipe instances to share a single
WebSocket connection, reducing resource usage and simplifying network
configuration.
"""

import logging
import asyncio
import threading
import time
import traceback
import uuid
from typing import Dict, Optional, Set
import websockets
from websockets.server import serve, WebSocketServerProtocol

from .transport import DataPipeTransport, TransportMessage
from ....utils import serialize, deserialize

logger = logging.getLogger(__name__)

class MultiplexedWebSocketTransport(DataPipeTransport):
    """
    WebSocket transport that multiplexes multiple DataPipes over one connection.

    This transport:
    - Runs a WebSocket server on a specified address/port
    - Routes messages to/from DataPipes based on pipe_id
    - Handles connection lifecycle for all registered pipes
    - Provides session conflict detection across all pipes

    Attributes
    ----------
    address : tuple of (str, int)
        The (host, port) to bind the WebSocket server to
    """

    def __init__(self, address: tuple, abort: Optional[callable] = None):
        """
        Initialize multiplexed WebSocket transport.

        Parameters
        ----------
        address : tuple of (str, int)
            Network address (host, port) for WebSocket server
        abort : callable, optional
            Function to call on fatal errors
        """
        self.address = address
        self._abort = abort

        # DataPipe registry
        self._pipes: Dict[str, 'DataPipe'] = {}
        self._pipe_lock = threading.Lock()

        # WebSocket state
        self._websocket: Optional[WebSocketServerProtocol] = None
        self._server = None
        self._connected = False
        self._session_id: Optional[str] = None

        # Session tracking for conflict detection
        self._active_sessions: Dict[str, dict] = {}
        self._session_lock = threading.Lock()

        # Background task
        self._server_task: Optional[asyncio.Task] = None
        self._running = False

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
        Send a message from a specific DataPipe to frontend.

        Parameters
        ----------
        pipe_id : str
            ID of the DataPipe sending this message
        message : dict
            Message payload
        """
        if not self._connected or self._websocket is None:
            raise RuntimeError(f"Transport not connected for pipe {pipe_id}")

        # Wrap message with pipe_id for multiplexing
        transport_msg = TransportMessage(
            pipe_id=pipe_id,
            msg_id=message.get('id', ''),
            direction=message.get('direction', 'p2j'),
            message=message.get('message', {}),
            session_id=self._session_id
        )

        await self._websocket.send(transport_msg.serialize())

    async def start(self) -> None:
        """Start the WebSocket server"""
        print(f"Multiplexed WebSocket server start({self}): {self._running}")
        if self._running:
            return

        self._running = True

        # Start WebSocket server in background
        async def run_server():
            try:
                async with serve(
                    self._handle_connection,
                    self.address[0],
                    self.address[1]
                ) as server:
                    self._server = server
                    print(f"MultiplexedWebSocketTransport server started on {self.address}")

                    # Keep server running
                    while self._running:
                        await asyncio.sleep(0.1)
            except Exception as e:
                print(f"WebSocket server error: {e}")
                traceback.print_exc()
                if self._abort:
                    self._abort(e)

        # Create background task
        self._server_task = asyncio.create_task(run_server())

    async def stop(self) -> None:
        """Stop the WebSocket server"""
        self._running = False

        if self._websocket:
            await self._websocket.close()
            self._websocket = None

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        if self._server_task:
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass

        self._connected = False

    def is_connected(self) -> bool:
        """Check if transport is connected"""
        return self._connected

    async def _handle_connection(self, websocket: WebSocketServerProtocol):
        """
        Handle a WebSocket connection.

        This processes all messages for all registered DataPipes on this
        single connection.
        """
        connection_id = str(uuid.uuid4())  # Unique ID for this connection
        session_id = None

        try:
            async for message in websocket:
                msg_dict = deserialize(message)

                # Extract session info
                if 'session' not in msg_dict:
                    await self._send_error(
                        websocket,
                        'missing_session',
                        'Session ID not found in message'
                    )
                    await websocket.close()
                    return

                msg_session_id = msg_dict['session']

                # On first message, establish session for this connection
                if session_id is None:
                    session_id = msg_session_id

                    print( f"                               establish session session id {session_id}" )
                    print( f"                               establish session clean <PRE>{self._session_lock}" )
                    with self._session_lock:
                        # Clean up stale sessions
                        print( f"                               establish session clean <IN> {self._session_lock}" )
                        self._cleanup_dead_sessions()

                        # Check if another WebSocket connection already exists for this session
                        print( f"                               establish session active up  {self._session_lock}" )
                        if session_id in self._active_sessions:
                            existing_info = self._active_sessions[session_id]
                            print( f"                               establish session existing info {existing_info}" )

                            # Check if existing connection is alive
                            if not await self._handle_session_conflict(
                                websocket, existing_info, session_id
                            ):
                                print( f"                               establish session REJECTED {existing_info}" )
                                return  # Connection rejected
                            print( f"                               establish session passed check {existing_info}" )

                        # Register this WebSocket connection for this session
                        print( f"                               establish session register   {self._session_lock}" )
                        self._active_sessions[session_id] = {
                            'websocket': websocket,
                            'timestamp': time.time(),
                            'transport': self,
                            'connection_id': connection_id
                        }
                        self._session_id = session_id
                        self._connected = True

                        print(f"MultiplexedWebSocketTransport: Session {session_id} established (connection {connection_id})")

                # Validate session consistency for this connection
                elif session_id != msg_session_id:
                    print( f"MultiplexedWebSocketTransport: error w/ session id {msg_dict}" )
                    await self._send_error(
                        websocket,
                        'session_corruption',
                        f"Session mismatch: expected {session_id}, got {msg_session_id}"
                    )
                    await websocket.close()

                    with self._session_lock:
                        if session_id in self._active_sessions:
                            del self._active_sessions[session_id]

                    return

                # Route message to appropriate DataPipe
                pipe_id = msg_dict.get('pipe_id')

                # Handle initialization messages
                if msg_dict.get('id') == 'initialize':
                    print(f"MultiplexedWebSocketTransport: Pipe {pipe_id} initialized for session {session_id}")
                    # Just acknowledge - no special handling needed
                    # The pipe is already registered via register_pipe()
                    continue

                if not pipe_id:
                    # Legacy format without pipe_id - try to infer
                    pipe_id = self._infer_pipe_id(msg_dict)

                if pipe_id:
                    await self._route_message(pipe_id, msg_dict)
                else:
                    print(f"Warning: Could not determine pipe_id for message: {msg_dict.get('id')}")

        except websockets.exceptions.ConnectionClosed:
            print(f"WebSocket connection closed for session {session_id}")
        except Exception as e:
            print(f"Error in WebSocket handler: {e}")
            traceback.print_exc()
        finally:
            # Cleanup - remove this connection's session entry
            if session_id:
                with self._session_lock:
                    # Only remove if this is still the active connection for this session
                    if session_id in self._active_sessions:
                        if self._active_sessions[session_id].get('connection_id') == connection_id:
                            del self._active_sessions[session_id]
                            print(f"MultiplexedWebSocketTransport: Session {session_id} cleaned up")

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
                logger.warning( f"No pipe registered with ID {pipe_id}\nfor message: {message}" )
                return

            pipe = self._pipes[pipe_id]

        # Let the DataPipe handle its message
        await pipe._handle_transport_message(message)

    def _infer_pipe_id(self, message: dict) -> Optional[str]:
        """
        Infer pipe_id from message when not explicitly provided.

        This is for backward compatibility with non-multiplexed messages.
        """
        # If only one pipe registered, use it
        with self._pipe_lock:
            if len(self._pipes) == 1:
                return list(self._pipes.keys())[0]

        return None

    async def _send_error(
        self,
        websocket: WebSocketServerProtocol,
        error_type: str,
        error_message: str
    ) -> None:
        """Send an error message to the client"""
        error_msg = {
            'id': error_type,
            'message': {
                'type': error_type,
                'error': error_message
            },
            'direction': 'error'
        }
        await websocket.send(serialize(error_msg))

    def _cleanup_dead_sessions(self) -> None:
        """Remove stale sessions (older than 5 minutes)"""
        current_time = time.time()
        dead_sessions = []

        for session_id, info in self._active_sessions.items():
            if current_time - info['timestamp'] > 300:  # 5 minutes
                dead_sessions.append(session_id)

        for session_id in dead_sessions:
            del self._active_sessions[session_id]

    async def _is_websocket_alive(
        self,
        websocket: WebSocketServerProtocol
    ) -> bool:
        """Check if a websocket is still alive"""
        print( f"                               alive check ENTERING {websocket}" )
        try:
            await asyncio.wait_for(websocket.ping(), timeout=2.0)
            print( f"                               alive check IS ALIVE {websocket}" )
            return True
        except (asyncio.TimeoutError, Exception):
            print( f"                               alive check IS DEAD {websocket}" )
            return False

    async def _handle_session_conflict(
        self,
        new_websocket: WebSocketServerProtocol,
        existing_info: dict,
        session_id: str
    ) -> bool:
        """
        Handle session conflict when duplicate connection detected.

        Returns
        -------
        bool
            True if new connection should be accepted, False to reject
        """
        existing_ws = existing_info['websocket']

        print( f"                               conflict mediation ENTERING {existing_info}" )
        print( f"                               conflict mediation existing ws {existing_ws}" )
        print( f"                               conflict mediation new ws {new_websocket}" )
        # Check if existing connection is still alive
        if await self._is_websocket_alive(existing_ws):
            print( f"                               conflict mediation existing ws is ALIVE {existing_info}" )
            # Both connections alive - reject new one
            conflict_msg = {
                'id': 'session_conflict',
                'message': {
                    'type': 'session_conflict',
                    'error': 'Multiple windows/tabs detected. Please use only one browser window.',
                    'action': 'close_duplicate'
                },
                'direction': 'error'
            }

            try:
                await existing_ws.send(serialize(conflict_msg))
                await new_websocket.send(serialize(conflict_msg))
                await new_websocket.close(code=1008, reason='Session conflict')

                if self._abort:
                    err = RuntimeError(f"Session conflict: duplicate connection for {session_id}")
                    self._abort(err)

                return False
            except Exception as e:
                print(f"Error handling session conflict: {e}")

        # Existing connection dead - allow new one
        return True
