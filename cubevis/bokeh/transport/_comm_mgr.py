########################################################################
# Communications Manager with Per-Category (Comm) Isolation
########################################################################
from __future__ import annotations

from enum import Enum
from typing import Optional, Callable, Dict, Any, List, Tuple
import asyncio
import inspect
import traceback
import logging
from uuid import uuid4

from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from bokeh.model import Model
from bokeh.models import CustomJS
from bokeh.core.properties import List
from bokeh.core.properties import String, Bool, Tuple, Int, Nullable, Instance
from ...utils import find_ws_address
from .. import BokehInit

class ShutdownReason(Enum):
    """Reason for shutdown"""
    REQUESTED = "shutdown_requested"      # User called requestShutdown()
    TRANSPORT_CLOSED = "transport_closed" # Connection closed normally
    ERROR = "error"                       # Fatal error occurred

logger = logging.getLogger(__name__)


class AppState(Enum):
    """Application lifecycle states."""
    CONSTRUCTED = "constructed"
    INITIALIZING = "initializing"
    RUNNING = "running"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"
    ERROR = "error"


class HandlerContext:
    """
    Context object passed to message handlers.
    Provides handlers with ability to affect application state.
    """

    def __init__(self, comm_mgr: 'CommMgr'):
        self._comm_mgr = comm_mgr

    def request_shutdown(self, reason: str = "Handler requested shutdown"):
        """Request graceful application shutdown."""
        logger.debug(f"HandlerContext.request_shutdown: reason='{reason}'")
        self._comm_mgr.request_shutdown(reason)

    def report_error(self, error: Exception, fatal: bool = False):
        """Report an error from a handler."""
        self._comm_mgr.report_error(error, fatal)

    def get_state(self) -> AppState:
        """Get current application state."""
        return self._comm_mgr.state

    def set_shared_state(self, key: str, value: Any):
        """Set shared state that other handlers can access."""
        self._comm_mgr.set_shared_state(key, value)

    def get_shared_state(self, key: str, default: Any = None) -> Any:
        """Get shared state."""
        return self._comm_mgr.get_shared_state(key, default)


class Comm( Model ):
    """
    Represents a communication category/channel.

    Each Comm has its own queue and pending state, allowing
    different message categories to be processed concurrently.

    Example usage:
        image_comm = mgr.open('image_updates')
        image_comm.register('update', handle_image_update)
        await image_comm.send('update', {'data': image}, callback)
    """

    comm_id = String( help="unique id for each Comm instance" )
    description = String( default='', help="string that describes this Comm object" )
    comm_mgr_id = String( help="comm_mgr that this comm channel is assocated with" )
    squash_queue = Bool( default=False,
                         help='''Existing messages with the same message id should be removed from
                                 the waiting message queue when a new message is added''' )

    _mgr = None

    def add_init_script(self, code, description='', args=None):
        """
        Helper to append a CustomJS script to a model's init_scripts list.

        Args:
            model: The Bokeh model containing the init_scripts property.
        script_code (str): The JS code for the CustomJS instance.
        args (dict, optional): Mapping of names to Bokeh models for the JS.
        """
        # Create the new CustomJS instance
        new_script = CustomJS(code=code, args=args or {})

        # 1. Access current scripts (default to empty list if None)
        current_scripts = list(self._mgr.init_scripts) if self._mgr.init_scripts else []

        # 2. Append the new script
        new_entry = (new_script, self.comm_id, description)
        current_scripts.append(new_entry)

        # 3. REASSIGN to trigger synchronization
        self._mgr.init_scripts = current_scripts

    def __init__( self, *args, comm_mgr: Optional[CommMgr] = None, **kwargs ):
        if 'comm_id' not in kwargs:
            kwargs['comm_id'] = str(uuid4( ))
        if 'description' not in kwargs:
            kwargs['description']
        if 'comm_mgr' and 'comm_mgr_id' not in kwargs:
            kwargs['comm_mgr_id'] = comm_mgr.comm_mgr_id
        super( ).__init__(*args, **kwargs)

        self._mgr = comm_mgr
        if comm_mgr:
            comm_mgr._register_comm(self)

    @property
    def mgr(self) -> CommMgr:
        """Get the CommMgr (lazy load from shared state if needed)"""
        if self._mgr is None:
            app = BokehInit.get_app_context()
            if app and app.comm_mgr:
                self._mgr = app.comm_mgr
        return self._mgr

    def register(self, message_id: str, callback: Callable) -> None:
        """Register a handler for messages with this ID."""
        if self._mgr:
            self._mgr.register(self, message_id, callback)

    def unregister(self, message_id: str) -> None:
        """Unregister a handler."""
        if self._mgr:
            self._mgr.unregister(self, message_id)

    async def send(self, message_id: str, message: Dict[str, Any],
                   callback: Optional[Callable] = None) -> None:
        """Send a message through this comm."""
        if self._mgr:
            await self._mgr.send(self, message_id, message, callback)


class CommMgr( Model, BokehInit ):
    """
    Single communications manager for entire application.

    Provides per-category (Comm) message handling with concurrent processing.
    Different Comm categories can process messages in parallel, while
    messages within a category are serialized.
    """
    transport_type = String( default='auto',
                             help="which type of low level transport to use" )
    address = Nullable( Tuple(String, Int), default=None,
                        help="the address (IP,port) when websockets low level transport is used" )
    comm_mgr_id = String( help="unique identifier for this communications manager" )
    init_scripts = List(
        Tuple(Instance(CustomJS), String, String),
        default=[],
        help="initialization scripts with associated metadata set from Comm object"
    )

    def __init__( self, *args,
                  on_shutdown: Optional[Callable] = None,
                  on_error: Optional[Callable[[Exception], None]] = None,
                  **kwargs ):
        if 'comm_mgr_id' not in kwargs:
            kwargs['comm_mgr_id'] = str(uuid4( ))

        logger.debug(f"CommMgr.__init__: {args}, on_shutdown={on_shutdown}, on_error={on_error}, {kwargs}", stack_info=True)

        super( ).__init__( *args, **kwargs )

        # Auto-detect transport type (sync operation)
        if self.transport_type == 'auto':
            self.transport_type = self._detect_transport()

        # Transport and messaging
        self._transport: Optional['TransportBase'] = None
        self._comms: Dict[str, Comm] = {}                                                       # comm_id => Comm
        self._handlers: Dict[str, Dict[str, Callable]] = {}                                     # comm_id => {message_id => handler}
        self._pending: Dict[str, str] = {}                                                      # comm_id => request_id (currently pending)
        self._pending_tasks: Dict[str, asyncio.Task] = {}                                       # comm_id => Task (for cancellation)
        self._pending_requests: Dict[str, Tuple[str, str, Callable]] = {}                       # request_id => (comm_id, message_id, callback)
        self._send_queue: Dict[str, List[Tuple[str, Dict[str, Any], Optional[Callable]]]] = {}  # comm_id => [(message_id, message, callback)]
        self._lock = asyncio.Lock()

        # State management
        self._state = AppState.CONSTRUCTED
        self._state_lock = asyncio.Lock()                 # REMOVED
        self._shutdown_requested = False
        self._pending_user_shutdown_reason = ''
        self._pending_user_shutdown = False               # Mark that shutdown is underway
                                                          # but not yet complete.
        self._shutdown_reason: Optional[str] = None       # REMOVED
        self._shutdown_callback_called = False            # Track if callback was called
        self._errors: List[Exception] = []                # REMOVED
        self._shared_state: Dict[str, Any] = {}

        # Lifecycle callbacks
        self._on_shutdown = on_shutdown                   # REMOVED
        self._on_error = on_error                         # REMOVED
        self._shutdown_event = asyncio.Event()
        self._on_connection_closed = None

        # Handler context (shared across all handlers)
        self._context = HandlerContext(self)

        self._initialized = False

        # Websocket address management (if not set with parameters)
        if self.transport_type == 'websocket':
            if not self.address:
                self.address = find_ws_address( )
                logger.debug( f"CommMgr.__init__: websocket address initialized to '{self.address}'" )
        else:
            if self.address:
                raise RuntimeError( 'CommMgr.__init__: address set for non-websocket transport' )

        logger.debug(f"Communications manager created: {self.comm_mgr_id}")

    @property
    def state(self) -> AppState:
        """Get current application state."""
        return self._state

    @state.setter
    def state(self, new_state: AppState):
        """Set application state."""
        old_state = self._state
        self._state = new_state
        logger.debug(f"Application state: {old_state.value} -> {new_state.value}")

    def open(self, comm_id: Optional[str] = None, squash_queue: bool = False, description: Optional[str] = '' ) -> Comm:
        """
        Open a new Comm (communication category).

        Args:
            comm_id: Unique identifier for this comm
            squash_queue: If True, only keep most recent queued message per message_id

        Returns:
            Comm object for this category

        Example:
            # For mouse tracking (needs squashing)
            mouse_comm = mgr.open('mouse_tracking', squash_queue=True)

            # For critical data (no squashing)
            data_comm = mgr.open('data_transfer', squash_queue=False)
        """
        if comm_id is None:
            comm_id = str(uuid4( ))
        if comm_id in self._comms:
            logger.warning(f"Comm '{comm_id}' already exists, returning existing")
            return self._comms[comm_id]

        logger.debug(f"CommMgr.open: {description}")
        comm = Comm(comm_id=comm_id, comm_mgr_id=self.comm_mgr_id, squash_queue=squash_queue, description=description)
        comm._mgr = self  # Set internal reference
        self._comms[comm_id] = comm
        self._handlers[comm_id] = {}
        self._send_queue[comm_id] = []

        logger.debug(f"Opened comm: {comm_id} (squash_queue={squash_queue})")
        return comm

    def _register_comm(self, comm: 'Comm'):
        """Internal method called by Comm.__init__"""
        if comm.comm_id not in self._comms:
            self._comms[comm.comm_id] = comm
            self._handlers[comm.comm_id] = {}
            self._send_queue[comm.comm_id] = []

    def close(self, comm: Comm):
        """Close a comm and clean up its resources."""
        comm_id = comm.comm_id

        # Cancel any pending task
        if comm_id in self._pending_tasks:
            self._pending_tasks[comm_id].cancel()
            del self._pending_tasks[comm_id]

        # Clean up pending request
        if comm_id in self._pending:
            request_id = self._pending[comm_id]
            if request_id in self._pending_requests:
                del self._pending_requests[request_id]
            del self._pending[comm_id]

        # Clean up handlers and queue
        if comm_id in self._handlers:
            del self._handlers[comm_id]
        if comm_id in self._send_queue:
            del self._send_queue[comm_id]
        if comm_id in self._comms:
            del self._comms[comm_id]

        logger.debug(f"Closed comm: {comm_id}")

    def request_shutdown(self, reason: str = "Shutdown requested"):
        """Request graceful shutdown of the application."""
        if not self._shutdown_requested:
            self._shutdown_requested = True
            self._shutdown_reason = reason
            ### shutdown must be postponed long enough for
            ### the result of the callback which called this
            ### request_shutdown to return a result before
            ### ending communications
            self._pending_user_shutdown = True
            self._pending_user_shutdown_reason = reason
            logger.debug(f"CommMgr.request_shutdown: '{reason}'")


    def report_error(self, error: Exception, fatal: bool = False):
        """Report an error."""
        self._errors.append(error)
        logger.error(f"Error reported ({'fatal' if fatal else 'non-fatal'}): {error}")

        if self._on_error:
            try:
                self._on_error(error)
            except Exception as e:
                logger.error(f"Error in error callback: {e}")

        if fatal:
            self.state = AppState.ERROR
            self.request_shutdown(f"Fatal error: {error}")

    def set_shared_state(self, key: str, value: Any):
        """Set shared state accessible to all handlers."""
        self._shared_state[key] = value
        logger.debug(f"Shared state set: {key} = {value}")

    def get_shared_state(self, key: str, default: Any = None) -> Any:
        """Get shared state."""
        return self._shared_state.get(key, default)

    def register(self, comm: Comm, message_id: str, callback: Callable) -> None:
        """
        Register a handler for messages.

        Internal method - users should call comm.register() instead.
        """
        comm_id = comm.comm_id
        if comm_id not in self._handlers:
            self._handlers[comm_id] = {}

        if message_id in self._handlers[comm_id]:
            logger.warning(f"Replacing handler for {comm_id}.{message_id}")

        self._handlers[comm_id][message_id] = callback
        logger.debug(f"Registered handler: {comm_id}.{message_id}")

    def unregister(self, comm: Comm, message_id: str) -> None:
        """Unregister a handler."""
        comm_id = comm.comm_id

        if comm_id in self._handlers and message_id in self._handlers[comm_id]:
            del self._handlers[comm_id][message_id]
            logger.debug(f"Unregistered handler: {comm_id}.{message_id}")

    async def send(self, comm: Comm, message_id: str, message: Dict[str, Any],
                   callback: Optional[Callable] = None):
        """
        Send a message through a comm.

        Internal method - users should call comm.send() instead.

        This implements per-comm queuing: if this comm already has a pending
        request, the new message is queued. Other comms can still send.
        """
        comm_id = comm.comm_id
        request_id = str(uuid4())

        msg = {
            'comm_id': comm_id,
            'message_id': message_id,
            'message': message,
            'direction': 'p2j',
            'request_id': request_id
        }

        async with self._lock:
            # Check if this comm has a pending request
            if comm_id in self._pending:
                # This comm is waiting for a response - queue this message
                if comm.squash_queue:
                    # Squash mode: replace any queued message with same message_id
                    self._send_queue[comm_id] = [
                        (mid, m, cb) for mid, m, cb in self._send_queue[comm_id]
                        if mid != message_id
                    ]

                # Add to queue
                self._send_queue[comm_id].append((message_id, message, callback))
                logger.debug(
                    f"Queued message for {comm_id}.{message_id} "
                    f"(queue size: {len(self._send_queue[comm_id])})"
                )
            else:
                # No pending request for this comm - send immediately
                await self._send_immediate(comm_id, message_id, message, request_id, callback)

    async def _send_immediate(self, comm_id: str, message_id: str, message: Dict[str, Any],
                              request_id: str, callback: Optional[Callable]):
        """Send a message immediately (assumes lock is held or not needed)."""
        msg = {
            'comm_id': comm_id,
            'message_id': message_id,
            'message': message,
            'direction': 'p2j',
            'request_id': request_id
        }

        # Mark this comm as having a pending request
        self._pending[comm_id] = request_id
        self._pending_requests[request_id] = (comm_id, message_id, callback)

        # Send through transport
        if self._transport and self._transport.is_connected():
            await self._transport.send_message(msg)
            logger.debug(f"Sent message: {comm_id}.{message_id} (request_id={request_id})")
        else:
            logger.warning(f"Transport not ready, cannot send {comm_id}.{message_id}")

    async def _process_next_queued(self, comm_id: str):
        """
        Process next queued message for a comm after current request completes.

        This is called when a response is received, allowing the queue to proceed.
        """
        async with self._lock:
            if comm_id not in self._send_queue or not self._send_queue[comm_id]:
                # No queued messages for this comm
                return

            # Get next message from queue
            message_id, message, callback = self._send_queue[comm_id].pop(0)
            request_id = str(uuid4())

            logger.debug(
                f"Processing queued message for {comm_id}.{message_id} "
                f"({len(self._send_queue[comm_id])} remaining in queue)"
            )

            # Send it
            await self._send_immediate(comm_id, message_id, message, request_id, callback)

    async def initialize(self):
        """Initialize the transport (for Colab/Jupyter)."""
        if self._initialized and self.transport_type != 'auto':
            return  # Already done

        self.state = AppState.INITIALIZING

        # Auto-detect transport if needed
        if self.transport_type == 'auto':
            self.transport_type = self._detect_transport()
            logger.debug(f"Auto-detected transport type: {self.transport_type}")

        # Create transport with error handler
        def transport_abort(error):
            self.report_error(error, fatal=True)

        logger.debug(f"CommMgr.initialize: {self.transport_type}")
        from ._low_level_transport import ColabCommsTransport, JupyterCommsTransport

        # Create and initialize non-WebSocket transports
        if self.transport_type == 'colab':
            self._transport = ColabCommsTransport(self.comm_mgr_id, abort=transport_abort)
            self._transport.set_message_callback(self._route_message)
            await self._transport.connect()
            self._initialized = True
            self.state = AppState.RUNNING
        elif self.transport_type == 'jupyter':
            self._transport = JupyterCommsTransport(self.comm_mgr_id, abort=transport_abort)
            self._transport.set_message_callback(self._route_message)
            await self._transport.connect()
            self._initialized = True
            self.state = AppState.RUNNING
        elif self.transport_type == 'websocket':
            # WebSocket transport created in process_messages() when we have the websocket
            logger.debug("WebSocket transport will be initialized in process_messages()")
        else:
            raise ValueError(f"Unknown transport type: {self.transport_type}")

        logger.debug(f"Communications manager initialized with {self.transport_type}")

    async def process_messages(self, websocket=None):
        """
        Process messages from transport.

        For WebSocket: Called by websockets.serve() for each connection.
        On normal close, cleans up and returns (allows reconnection).
        Only shuts down CommMgr for user request or fatal errors.
        """
        logger.debug("************************************************************************************************************************")
        logger.debug(f"CommMgr.process_messages  starting ({self.comm_mgr_id})")
        logger.debug(f"CommMgr.process_messages: transport_type {self.transport_type}")
        logger.debug("************************************************************************************************************************")

        # Determine why we stopped
        shutdown_reason = None
        shutdown_description = ""
        should_shutdown = False

        try:
            if self.transport_type == 'websocket':
                if not websocket:
                    raise ValueError("WebSocket transport requires websocket parameter")

                from ._low_level_transport import WebSocketTransport

                def transport_abort(error):
                    # Only report truly fatal errors
                    logger.error(f"Transport abort: {error}")
                    self.report_error(error, fatal=True)

                # Create WebSocket transport
                self._transport = WebSocketTransport(
                    self.comm_mgr_id,
                    websocket,
                    abort=transport_abort
                )

                # Set callback
                self._transport.set_message_callback(self._route_message)

                # Connect (performs handshake)
                await self._transport.connect()

                self._initialized = True
                self.state = AppState.RUNNING

            elif self.transport_type in ('colab', 'jupyter'):
                # Already initialized
                if self.state == AppState.CONSTRUCTED:
                    await self.initialize( )

            # Flush any queued messages
            await self._flush_all_queues()

            # Run transport event loop alongside shutdown monitor
            transport_task = asyncio.create_task(self._transport.run())
            shutdown_task = asyncio.create_task(self._shutdown_event.wait())

            # Wait for either transport to complete or shutdown
            done, pending = await asyncio.wait(
                [transport_task, shutdown_task],
                return_when=asyncio.FIRST_COMPLETED
            )

            # Determine why we stopped
            if shutdown_task in done:
                # User requested shutdown
                shutdown_reason = ShutdownReason.REQUESTED
                shutdown_description = "Shutdown requested by user"
                should_shutdown = True
                logger.debug("Shutdown was requested by user")
            else:
                # Transport closed - check if it was an error
                try:
                    # Get exception from transport task if any
                    transport_task.result()

                    # No exception - normal close
                    shutdown_reason = ShutdownReason.TRANSPORT_CLOSED
                    shutdown_description = "Connection closed normally"
                    should_shutdown = False
                    logger.debug("Connection closed normally, ready for reconnection")

                except ConnectionClosedError as e:
                    # WebSocket connection closed (laptop sleep, browser refresh, etc.)
                    # This is NORMAL - allow reconnection
                    shutdown_reason = ShutdownReason.TRANSPORT_CLOSED
                    shutdown_description = f"WebSocket closed: {e}"
                    should_shutdown = False
                    logger.debug(f"WebSocket connection closed: {e} - ready for reconnection")

                except Exception as e:
                    # Some other error in transport
                    shutdown_reason = ShutdownReason.ERROR
                    shutdown_description = f"Transport error: {e}"
                    should_shutdown = True
                    logger.error(f"Fatal transport error: {e}")
                    traceback.print_exc()

            # Cancel remaining tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"Error cancelling task: {e}")

        except Exception as e:
            # Exception during setup or processing
            shutdown_description = f"Error in message processing: {e}"
            logger.error(shutdown_description)
            traceback.print_exc()
            self.report_error(e, fatal=True)
            shutdown_reason = ShutdownReason.ERROR
            should_shutdown = True

        finally:
            # Clean up transport (make sure it's async)
            if self._transport:
                try:
                    await self._transport.close()
                except Exception as e:
                    logger.error(f"Error closing transport: {e}")

            # Only shutdown CommMgr for certain reasons
            if should_shutdown:
                logger.debug(f"Shutting down CommMgr (reason: {shutdown_reason})")
                await self.shutdown(reason=shutdown_reason, description=shutdown_description)
            else:
                # Just clean up this connection - ready for next one
                logger.debug(f"Connection ended (reason: {shutdown_reason}), ready for reconnection")

                # Reset connection-specific state
                self._transport = None

                # Clear pending requests for this connection
                self._pending.clear()
                self._pending_requests.clear()

                # Call connection closed callback if set
                if self._on_connection_closed:
                    try:
                        self._on_connection_closed(shutdown_reason, shutdown_description)
                    except Exception as e:
                        logger.error(f"Error in on_connection_closed callback: {e}")

    async def shutdown(self, reason: Optional[ShutdownReason] = None, description: str = ""):
        """Shut down the communications manager."""
        if self.state == AppState.STOPPED:
            return

        logger.debug(f"CommMgr.shutdown: {reason.value if reason else 'unknown'})")

        # Call shutdown callback
        if self._on_shutdown and not self._shutdown_callback_called:
            try:
                # Pass the enum value for better type safety
                self._on_shutdown(reason=reason, description=description)
                self._shutdown_callback_called = True
            except Exception as e:
                logger.error(f"Error in shutdown callback: {e}")
                traceback.print_exc()

        self.state = AppState.SHUTTING_DOWN

        # Close transport if still connected
        if self._transport:
            try:
                await self._transport.close()
            except Exception as e:
                logger.error(f"Error closing transport during shutdown: {e}")

        # Clear all state
        self._handlers.clear()
        self._pending.clear()
        self._pending_requests.clear()
        self._send_queue.clear()
        self._comms.clear()

        self.state = AppState.STOPPED
        logger.debug("Communications shutdown complete")

    async def _route_message(self, msg: Dict[str, Any]):
        """
        Route incoming message to appropriate handler.

        This handles both responses (p2j) and requests (j2p).
        """
        direction = msg.get('direction')

        if direction == 'p2j':
            # Response to our request
            await self._handle_response(msg)

        elif direction == 'j2p':
            # Request from frontend
            await self._handle_request(msg)

    async def _handle_response(self, msg: Dict[str, Any]):
        """Handle response from frontend to our request."""
        request_id = msg.get('request_id')

        if not request_id or request_id not in self._pending_requests:
            logger.warning(f"Received response for unknown request: {request_id}")
            return

        # Get request info
        comm_id, message_id, callback = self._pending_requests.pop(request_id)

        # Clear pending state for this comm
        if comm_id in self._pending and self._pending[comm_id] == request_id:
            del self._pending[comm_id]

        # Call the callback
        try:
            if callback:
                if inspect.iscoroutinefunction(callback):
                    await callback(msg['message'])
                else:
                    callback(msg['message'])
        except Exception as e:
            logger.error(f"Error in response callback for {comm_id}.{message_id}: {e}")
            self.report_error(e, fatal=False)

        # Process next queued message for this comm (if any)
        await self._process_next_queued(comm_id)

    async def _handle_request(self, msg: Dict[str, Any]):
        """Handle request from frontend."""
        comm_id = msg.get('comm_id')
        message_id = msg.get('message_id')
        request_id = msg.get('request_id')

        if not comm_id or not message_id:
            logger.warning(f"Request missing comm_id or message_id: {msg}")
            return

        # Find handler
        if comm_id not in self._handlers or message_id not in self._handlers[comm_id]:
            logger.warning(f"No handler for {comm_id}.{message_id}")

            # Send error response if request_id present
            if request_id and self._transport:
                await self._transport.send_message({
                    'comm_id': comm_id,
                    'message_id': message_id,
                    'request_id': request_id,
                    'message': {'error': f'No handler for {comm_id}.{message_id}'},
                    'direction': 'j2p'
                })
            return

        # Call handler
        handler = self._handlers[comm_id][message_id]

        try:
            # Check if handler expects context parameter
            sig = inspect.signature(handler)
            if len(sig.parameters) >= 2:
                try:
                    # Handler accepts (message, context)
                    result = handler(msg['message'], context=self._context)
                except Exception as e:
                    raise RuntimeError(
                        f"Handler {handler.__name__!r} failed with {type(e).__name__}: {e}"
                    ) from None
            else:
                # Handler only accepts message
                result = handler(msg['message'])

            if inspect.isawaitable(result):
                result = await result

            # Send response if there's a request_id
            if request_id and self._transport:
                reply = {
                    'comm_id': comm_id,
                    'message_id': message_id,
                    'request_id': request_id,
                    'message': result,
                    'direction': 'j2p'
                }

                if getattr(self, '_pending_user_shutdown', False):
                    ### transport_control is used to manage transport
                    reply['transport_control'] = 'SHUTDOWN-NOW'

                await self._transport.send_message(reply)

        except Exception as e:
            logger.error(f"Error in handler {comm_id}.{message_id}: {e}")
            traceback.print_exc()
            self.report_error(e, fatal=False)

            if request_id and self._transport:
                await self._transport.send_message({
                    'comm_id': comm_id,
                    'message_id': message_id,
                    'request_id': request_id,
                    'message': {
                        'error': str(e),
                        'traceback': traceback.format_exc()
                    },
                    'direction': 'j2p'
                })

        # Fire any shutdown that was requested during handler execution,
        # now that the response (if any) has been sent.
        if getattr(self, '_pending_user_shutdown', False):

            logger.debug(f"CommMgr._handle_request: user shutdown pending, message: {msg}")

            self._pending_user_shutdown = False

            # Signal the shutdown event
            try:
                loop = asyncio.get_running_loop()
                logger.debug(f"CommMgr._handle_request effectuate shutdown")
                loop.call_soon_threadsafe(self._shutdown_event.set)
            except RuntimeError:
                # No event loop running
                logger.debug(f"CommMgr._handle_request effectuate shutdown without loop")
                self._shutdown_event.set

    async def _flush_all_queues(self):
        """Flush all queued messages on startup."""
        for comm_id in list(self._send_queue.keys()):
            if self._send_queue[comm_id] and comm_id not in self._pending:
                # This comm has queued messages and no pending request
                await self._process_next_queued(comm_id)

    def _detect_transport(self) -> str:
        """Auto-detect appropriate transport (sync operation)."""
        try:
            import google.colab
            return 'colab'
        except ImportError:
            pass

        try:
            from IPython import get_ipython
            if get_ipython() is not None:
                return 'jupyter'
        except ImportError:
            pass

        return 'websocket'

