########################################################################
# Communications Manager with Per-Category (Comm) Isolation
########################################################################
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING
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
if TYPE_CHECKING:
    from .. import BokehAppContext

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


### Direction/role tags used to tell "a message I originated" apart from
### "a message the peer originated" when routing an incoming message.
###
### Historically these were hardcoded literals ('p2j' for anything this
### CommMgr sent, 'j2p' for anything it received and had to reply to) --
### safe only because the browser leg is genuinely asymmetric: Python
### always originates 'p2j', JS always originates 'j2p'. Two unmodified
### CommMgr instances talking to each other (e.g. P_local's kernel-facing
### side and a kernel-side CommMgr, both Python) would both tag their own
### outgoing traffic 'p2j' and both be unable to tell their own replies
### apart from the peer's fresh requests -- see the design doc, and
### test_task1_bug_reproduction.py for the concrete failure this caused.
###
### 'default' reproduces today's literals exactly, so the browser-facing
### path (and anything else already constructing a plain `CommMgr()`) is
### unaffected. Give the *other* end of a Python<->Python link 'mirror'
### so the two sides no longer collide.
###
### Exposed as named constants (module-level, and mirrored as CommMgr
### class attributes below) rather than leaving 'default'/'mirror' as
### magic strings, so callers outside this module -- notably the
### cubevis.remote subpackage -- don't have to hardcode them.
ROLE_DEFAULT = 'default'
ROLE_MIRROR = 'mirror'

_ROLE_DIRECTION_TAGS = {
    #        (self_direction, peer_direction)
    ROLE_DEFAULT: ('p2j', 'j2p'),
    ROLE_MIRROR:  ('j2p', 'p2j'),
}


class CommMgr( Model, BokehInit ):
    """
    Single communications manager for entire application.

    Provides per-category (Comm) message handling with concurrent processing.
    Different Comm categories can process messages in parallel, while
    messages within a category are serialized.

    ``role`` selects which direction tag this instance uses for messages it
    originates, and which tag it therefore expects on incoming requests from
    its peer -- see ``_ROLE_DIRECTION_TAGS``. Two CommMgrs that talk directly
    to each other (as opposed to one Python CommMgr and one JS frontend) must
    use opposite roles: one ``ROLE_DEFAULT``, the other ``ROLE_MIRROR``.
    """

    # Convenience aliases so callers can write CommMgr.ROLE_MIRROR instead
    # of importing the module-level constants separately.
    ROLE_DEFAULT = ROLE_DEFAULT
    ROLE_MIRROR = ROLE_MIRROR

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
                  role: str = ROLE_DEFAULT,
                  **kwargs ):
        if 'comm_mgr_id' not in kwargs:
            kwargs['comm_mgr_id'] = str(uuid4( ))

        # JavaScript Comm object find their manager using this
        kwargs['name'] = kwargs['comm_mgr_id']
        logger.debug(f"CommMgr.__init__: {args}, on_shutdown={on_shutdown}, on_error={on_error}, role={role}, {kwargs}")

        super( ).__init__( *args, **kwargs )

        if role not in _ROLE_DIRECTION_TAGS:
            raise ValueError(
                f"CommMgr: unknown role {role!r}; expected one of {sorted(_ROLE_DIRECTION_TAGS)}"
            )
        self._role = role
        self._self_direction, self._peer_direction = _ROLE_DIRECTION_TAGS[role]

        # Auto-detect transport type (sync operation)
        if self.transport_type == 'auto':
            self.transport_type = self._detect_transport()

        # Transport and messaging
        self._transport: Optional['TransportBase'] = None
        self._comms: Dict[str, Comm] = {}                                                       # comm_id => Comm
        self._handlers: Dict[str, Dict[str, Callable]] = {}                                     # comm_id => {message_id => handler}
        self._pending: Dict[str, str] = {}                                                      # comm_id => request_id (currently pending)
        self._pending_tasks: Dict[str, asyncio.Task] = {}                                       # comm_id => Task (for cancellation)
        # request_id => (comm_id, message_id, message, callback)
        # The message body is retained so that a request which was in flight
        # when a connection dropped can be replayed after the frontend
        # reconnects. See _reset_for_reconnect( ).
        self._pending_requests: Dict[str, Tuple[str, str, Dict[str, Any], Callable]] = {}
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
        self._on_reconnect = None

        # Incremented each time a connection is torn down without shutting the
        # application down. Lets callers distinguish a reconnect from a fresh
        # start, and lets stale callbacks from a dead connection be recognised.
        self._connection_generation = 0

        # When True, a request that was in flight when the connection dropped
        # is pushed back onto the front of its comm's send queue and re-sent
        # once the frontend returns. See the resend_inflight_on_reconnect
        # property below for the trade-off involved.
        self._resend_inflight_on_reconnect = True

        # Seconds to wait for a reconnect before giving up and shutting down,
        # or None to wait indefinitely (the default). See the
        # reconnect_timeout property below.
        self._reconnect_timeout: Optional[float] = None
        self._reconnect_watchdog: Optional[asyncio.Task] = None

        # Seconds to wait when the frontend closed deliberately (tab closed or
        # reloaded). Much shorter than _reconnect_timeout -- see the
        # reconnect_grace_period property below.
        self._reconnect_grace_period: Optional[float] = 3.0

        # Handler context (shared across all handlers)
        self._context = HandlerContext(self)

        self._initialized = False

    def registered( self, context: BokehAppContext ) -> None:
        '''This is called when this CommMgr is registered with BokehAppContext.
        '''
        # Websocket address management (if not set with parameters)
        if self.transport_type == 'websocket':
            if not self.address:
                self.address = find_ws_address( )
                logger.debug( f"CommMgr.__init__: websocket address initialized to '{self.address}'" )
        else:
            if self.address:
                raise RuntimeError( 'CommMgr.__init__: address set for non-websocket transport' )
            if self.transport_type == 'colab' or self.transport_type == 'jupyter':
                from ._low_level_transport import CommsTransport
                # Create transport with error handler
                def transport_abort(error):
                    self.report_error(error, fatal=True)
                ### CommsTransport is created here to allow for registration of
                ### preflight callables which enable the anywidget bridge.
                self._transport = CommsTransport(self.comm_mgr_id, abort=transport_abort)

        logger.debug(f"Communications manager registered: {self.comm_mgr_id}")

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

    @property
    def role(self) -> str:
        """
        This CommMgr's direction/role tag ('default' or 'mirror').

        Read-only: fixed at construction, since changing it mid-session
        while requests are in flight would make in-progress ``request_id``
        bookkeeping ambiguous. See ``_ROLE_DIRECTION_TAGS``.
        """
        return self._role

    @property
    def connection_generation(self) -> int:
        """
        Number of connections that have been retired without shutting the
        application down. Zero until the first disconnect, so
        ``connection_generation > 0`` means "this is a reconnect".
        """
        return self._connection_generation

    @property
    def resend_inflight_on_reconnect(self) -> bool:
        """
        Whether p2j requests that were in flight when a connection dropped are
        replayed after the frontend reconnects (default ``True``).

        With ``True`` the message body is pushed back onto the front of its
        comm's send queue and re-sent, so its callback still fires once the
        frontend returns. This is the right default for the GUI-update traffic
        cubevis sends, which is idempotent: the reply never arrived, so the
        frontend either never saw the request or never finished acting on it.

        Set to ``False`` if any p2j message is unsafe to deliver twice. In that
        case the pending callback is instead invoked once with
        ``{'error': ...}`` so the caller is not left waiting forever.
        """
        return self._resend_inflight_on_reconnect

    @resend_inflight_on_reconnect.setter
    def resend_inflight_on_reconnect(self, value: bool) -> None:
        self._resend_inflight_on_reconnect = bool(value)
        logger.debug(f"resend_inflight_on_reconnect set to {self._resend_inflight_on_reconnect}")

    @property
    def reconnect_timeout(self) -> Optional[float]:
        """
        Seconds to wait for the frontend to come back after a connection is
        lost, or ``None`` (the default) to wait indefinitely.

        ``None`` restores the original behaviour: the backend survives any
        outage and the session ends only when the GUI explicitly requests
        shutdown. The cost is that closing the browser tab outright leaves the
        Python session waiting forever.

        Setting a value (e.g. ``1800`` for thirty minutes) arms a watchdog on
        each disconnect. If no new connection has arrived when it expires, the
        CommMgr shuts down as though the transport had closed for good. Make it
        comfortably longer than the longest outage you want to survive -- an
        overnight suspend needs many hours.
        """
        return self._reconnect_timeout

    @reconnect_timeout.setter
    def reconnect_timeout(self, value: Optional[float]) -> None:
        self._reconnect_timeout = None if value is None else float(value)
        logger.debug(f"reconnect_timeout set to {self._reconnect_timeout}")

    @property
    def reconnect_grace_period(self) -> Optional[float]:
        """
        Seconds to wait after the frontend closed the connection *deliberately*
        -- i.e. sent a WebSocket close frame -- before shutting down. Defaults
        to 3 seconds. ``None`` waits indefinitely, as ``reconnect_timeout``
        does; ``0`` shuts down at once and gives up on surviving reloads.

        A closed browser tab and a reloaded browser tab are indistinguishable
        at the moment they happen: both send close code 1001 ("going away"),
        and there is no way to tell which one it was from the close frame
        alone. Nor can the frontend tell us -- the browser only reveals that a
        load was a reload on the *next* page load, which is after we needed to
        decide. They separate themselves a moment later: a reload comes
        straight back, a closed tab never does. So the policy is to wait
        briefly and let the frontend prove which it was, and the floor on that
        wait is however long a reload takes to reconnect.

        Three seconds suits a local app, where the page is served from
        ``file://`` and the socket is on localhost, so a reload reconnects
        almost immediately. Raise it if the assets are ever served over a real
        network, where a cold-cache reload has to re-fetch the Bokeh and
        cubevis bundles before the handshake can even start -- a reload that
        overruns this becomes a spurious shutdown.

        This is deliberately much shorter than ``reconnect_timeout``, which
        governs the case where the peer vanished without a close frame (a
        suspended laptop, a dropped network). There, waiting indefinitely is
        the whole point; here, waiting indefinitely would leave a session
        pinned open after the user has visibly finished with it.
        """
        return self._reconnect_grace_period

    @reconnect_grace_period.setter
    def reconnect_grace_period(self, value: Optional[float]) -> None:
        self._reconnect_grace_period = None if value is None else float(value)
        logger.debug(f"reconnect_grace_period set to {self._reconnect_grace_period}")

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

        #logger.debug(f"CommMgr.open: {description}")
        comm = Comm(comm_id=comm_id, comm_mgr_id=self.comm_mgr_id, squash_queue=squash_queue, description=description)
        comm._mgr = self  # Set internal reference
        self._comms[comm_id] = comm
        self._handlers[comm_id] = {}
        self._send_queue[comm_id] = []

        #logger.debug(f"Opened comm: {comm_id} (squash_queue={squash_queue})")
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
        #logger.debug(f"Registered handler: {comm_id}.{message_id}")

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
            'direction': self._self_direction,
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
            'direction': self._self_direction,
            'request_id': request_id
        }

        if not (self._transport and self._transport.is_connected()):
            ### No live connection. Hold the message at the head of this comm's
            ### queue and leave the comm un-pending, so it is not wedged waiting
            ### for a reply that can never arrive. _flush_all_queues( ) picks it
            ### up when the frontend reconnects.
            self._send_queue.setdefault(comm_id, []).insert(0, (message_id, message, callback))
            logger.debug(
                f"Transport not ready; holding {comm_id}.{message_id} for reconnect "
                f"(queue size: {len(self._send_queue[comm_id])})"
            )
            return

        # Mark this comm as having a pending request
        self._pending[comm_id] = request_id
        self._pending_requests[request_id] = (comm_id, message_id, message, callback)

        # Send through transport
        try:
            await self._transport.send_message(msg)
            logger.debug(f"Sent message: {comm_id}.{message_id} (request_id={request_id})")
        except (ConnectionClosedError, ConnectionClosedOK) as e:
            ### The socket died between is_connected( ) and the write. Undo the
            ### pending marker and re-queue so the reconnect path replays it.
            logger.debug(f"Connection closed while sending {comm_id}.{message_id}: {e}")
            self._pending.pop(comm_id, None)
            self._pending_requests.pop(request_id, None)
            self._send_queue.setdefault(comm_id, []).insert(0, (message_id, message, callback))

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

    async def initialize(self, transport: Optional['TransportBase'] = None):
        """
        Initialize the transport (for Colab/Jupyter/remote_kernel).

        `transport`, if given, is assigned to `self._transport` before
        the usual checks run -- equivalent to setting `self._transport`
        directly beforehand (which is still what this does internally),
        but lets external callers such as cubevis.remote's
        `open_remote_kernel_link()` avoid reaching past the underscore
        for what is, for the 'colab'/'jupyter'/'remote_kernel' transport
        types, otherwise a required step.
        """
        if transport is not None:
            self._transport = transport

        if self._initialized and self.transport_type != 'auto':
            return  # Already done

        self.state = AppState.INITIALIZING

        # Auto-detect transport if needed
        if self.transport_type == 'auto':
            self.transport_type = self._detect_transport()
            logger.debug(f"Auto-detected transport type: {self.transport_type}")

        logger.debug(f"CommMgr.initialize: {self.transport_type}")

        # Create and initialize non-WebSocket transports.
        #
        # 'remote_kernel' is handled identically to 'colab'/'jupyter' here
        # (a transport constructed and assigned to self._transport by the
        # caller before initialize() is called -- the same expectation the
        # RuntimeError below already documents for 'colab'/'jupyter') --
        # it's a separate label purely so cubevis.remote's KernelClientTransport
        # doesn't have to masquerade as a notebook-embedded 'jupyter' transport
        # (which would also make transport_type=='auto' autodetection, used
        # elsewhere for the browser leg, ambiguous with this new leg).
        if self.transport_type in ('colab', 'jupyter', 'remote_kernel'):
            if not self._transport:
                raise RuntimeError(f"transport should be set earlier for {self.transport_type}")
            self._transport.set_message_callback(self._route_message)
            await self._transport.connect()
            self._initialized = True
            self.state = AppState.RUNNING
        elif self.transport_type == 'websocket':
            # WebSocket transport created in process_messages() when we have the websocket
            logger.debug("WebSocket transport will be initialized in process_messages()")
        else:
            raise ValueError(f"Unknown transport type: {self.transport_type}")

        #logger.debug(f"Communications manager initialized with {self.transport_type}")

    async def process_messages(self, websocket=None):
        """
        Process messages from transport.

        For WebSocket: Called by websockets.serve() for each connection.
        On normal close, cleans up and returns (allows reconnection).
        Only shuts down CommMgr for user request or fatal errors.
        """
        logger.debug(f"CommMgr.process_messages starting ({self.comm_mgr_id})")

        # Determine why we stopped
        shutdown_reason = None
        shutdown_description = ""
        should_shutdown = False

        try:
            if self.transport_type == 'websocket':
                if not websocket:
                    raise ValueError("WebSocket transport requires websocket parameter")

                from ._low_level_transport import WebSocketTransport

                ### A new connection can arrive while the previous handler is
                ### still unwinding. This is common on wake-from-sleep: the
                ### browser notices the socket is dead and reconnects before the
                ### server has finished tearing the old one down. Retire the old
                ### transport first so it is not orphaned and so its in-flight
                ### requests are re-queued rather than lost.
                if self._transport is not None:
                    logger.debug(
                        "New connection while a transport is still active; retiring the old one"
                    )
                    try:
                        await self._transport.close()
                    except Exception:
                        logger.exception("Error closing superseded transport")
                    self._reset_for_reconnect()

                ### The frontend is here, so the "never came back" timer no
                ### longer applies. This must come *after* the retire block
                ### above, because _reset_for_reconnect( ) arms a fresh one.
                self._cancel_reconnect_watchdog()

                is_reconnect = self._connection_generation > 0

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

                if is_reconnect and self._on_reconnect:
                    try:
                        self._on_reconnect(self._connection_generation)
                    except Exception:
                        logger.exception("Error in on_reconnect callback")

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
            ### Ask the transport how the connection ended *before* discarding
            ### it. A close frame means the browser deliberately went away (tab
            ### closed or reloaded); its absence means the peer vanished (sleep,
            ### network loss). The two want very different reconnect policies.
            clean_close = None
            if self._transport is not None:
                probe = getattr(self._transport, 'close_was_clean', None)
                if callable(probe):
                    try:
                        clean_close = probe()
                    except Exception:
                        logger.exception("Error querying transport close status")

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

                ###
                ### NOTE: self._on_shutdown is deliberately NOT called here.
                ###
                ### It tears down the application -- for interactive clean it
                ### resolves __result_future, which exits the
                ### `async with websockets.serve( ... )` block and closes the
                ### listening socket. Calling it for a transient disconnect
                ### (laptop sleep, brief network drop, tab reload) destroys the
                ### very server the frontend is about to reconnect to, which is
                ### how reconnection was lost. Only shutdown( ) -- reached via
                ### the should_shutdown branch above -- may call it.
                ###
                self._reset_for_reconnect(clean_close=clean_close)
                if self._on_connection_closed:
                    try:
                        self._on_connection_closed(shutdown_reason, shutdown_description)
                    except Exception as e:
                        logger.error(f"Error in on_connection_closed callback: {e}")

    ########################################################################
    # Reconnection support
    ########################################################################
    def _reset_for_reconnect(self, clean_close: Optional[bool] = None) -> None:
        """
        Discard per-connection state while keeping this CommMgr alive.

        Called when a connection ends for a reason that does NOT warrant
        shutting the application down: laptop sleep, a brief network drop, a
        browser tab reload, or a new connection superseding an old one.

        ``clean_close`` says how the connection ended, and selects how long we
        are willing to wait for the frontend to return:

          True  -- the peer sent a close frame, so the browser deliberately
                   went away (tab closed, reloaded, navigated). Wait only
                   ``reconnect_grace_period``.
          False -- the peer vanished with no close frame (suspended laptop,
                   dropped network). Wait ``reconnect_timeout``, which defaults
                   to forever.
          None  -- unknown; treated like False, so we wait rather than risk
                   killing a recoverable session.

        Everything that outlives a single connection -- registered handlers,
        Comm objects, shared state, the per-comm send queues -- is preserved.
        Only the transport and the in-flight request bookkeeping are reset.

        Requests that were in flight are pushed back onto the *front* of their
        comm's send queue when ``resend_inflight_on_reconnect`` is True, so
        their callbacks still fire once the frontend returns. When it is False
        the pending callback is invoked once with an error instead, so no
        caller is left waiting on a reply that can never arrive.
        """
        self._transport = None
        self._initialized = False
        self._connection_generation += 1

        if self.state not in ( AppState.STOPPED, AppState.ERROR, AppState.SHUTTING_DOWN ):
            self.state = AppState.INITIALIZING

        requeued = 0
        notified = 0

        for request_id, entry in list(self._pending_requests.items()):
            comm_id, message_id, message, callback = entry

            if self._resend_inflight_on_reconnect:
                self._send_queue.setdefault(comm_id, []).insert(
                    0, (message_id, message, callback)
                )
                requeued += 1
            elif callback is not None:
                ### Don't leave the caller waiting on a reply that will never come.
                try:
                    result = callback({ 'error': 'connection lost before a reply arrived' })
                    if inspect.isawaitable(result):
                        asyncio.ensure_future(result)
                    notified += 1
                except Exception:
                    logger.exception(
                        f"Error notifying {comm_id}.{message_id} of lost connection"
                    )

        self._pending.clear()
        self._pending_requests.clear()

        logger.debug(
            f"CommMgr reset for reconnect (generation={self._connection_generation}, "
            f"requeued={requeued}, notified={notified}, "
            f"close={'clean' if clean_close else 'abrupt'})"
        )

        if clean_close:
            self._arm_reconnect_watchdog(self._reconnect_grace_period, clean=True)
        else:
            self._arm_reconnect_watchdog(self._reconnect_timeout, clean=False)

    def _arm_reconnect_watchdog(self, timeout: Optional[float],
                                clean: bool = False) -> None:
        """
        Start the "frontend never came back" timer, if one is configured.

        Without a watchdog a closed browser tab is indistinguishable from a
        sleeping laptop, so the backend waits forever. With one, the session
        shuts down cleanly after ``timeout`` seconds of silence.
        """
        self._cancel_reconnect_watchdog()

        if timeout is None:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("No running loop; reconnect watchdog not armed")
            return

        logger.debug(
            f"Reconnect watchdog armed for {timeout}s "
            f"({'deliberate close' if clean else 'connection lost'})"
        )
        self._reconnect_watchdog = loop.create_task(
            self._reconnect_watchdog_coro(self._connection_generation, timeout, clean)
        )

    def _cancel_reconnect_watchdog(self) -> None:
        """Stand down the reconnect watchdog (called when a connection arrives)."""
        watchdog = self._reconnect_watchdog
        self._reconnect_watchdog = None
        if watchdog is not None and not watchdog.done():
            watchdog.cancel()

    async def _reconnect_watchdog_coro(self, generation: int, timeout: float,
                                       clean: bool = False) -> None:
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return

        ### A later disconnect superseded this watchdog, so it is stale.
        if self._connection_generation != generation:
            return

        ### The frontend came back.
        if self._transport is not None:
            return

        if clean:
            description = (
                f"frontend closed the connection and did not return "
                f"within {timeout}s"
            )
            logger.debug(
                f"Frontend closed deliberately and did not reconnect within "
                f"{timeout}s; shutting down"
            )
        else:
            description = f"frontend did not reconnect within {timeout}s"
            logger.warning(
                f"No reconnection within {timeout}s; shutting down communications"
            )

        ### Detach before calling shutdown( ). shutdown( ) cancels the watchdog,
        ### and this coroutine *is* the watchdog -- cancelling ourselves would
        ### raise CancelledError at the first await inside shutdown( ) and leave
        ### the teardown half finished.
        self._reconnect_watchdog = None

        await self.shutdown(
            reason=ShutdownReason.TRANSPORT_CLOSED,
            description=description
        )

    def set_connection_closed_callback(self, callback: Optional[Callable]) -> None:
        """
        Register a callback invoked when a connection ends but the application
        keeps running.

        Signature: ``callback(reason: ShutdownReason, description: str)``

        Use this to grey out the GUI or show a "reconnecting..." indicator.
        This is NOT a shutdown notification -- see the ``on_shutdown``
        constructor argument for that.
        """
        self._on_connection_closed = callback

    def set_reconnect_callback(self, callback: Optional[Callable]) -> None:
        """
        Register a callback invoked when a connection is established after the
        first one, i.e. when the frontend has successfully reconnected.

        Signature: ``callback(generation: int)``
        """
        self._on_reconnect = callback

    async def shutdown(self, reason: Optional[ShutdownReason] = None, description: str = ""):
        """Shut down the communications manager."""
        if self.state == AppState.STOPPED:
            return

        logger.debug(f"CommMgr.shutdown: {reason.value if reason else 'unknown'})")

        # No point waiting for a reconnection we are no longer interested in
        self._cancel_reconnect_watchdog()

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

        This handles both responses (tagged with this instance's own
        ``self._self_direction``) and requests (tagged with the peer's
        ``self._peer_direction``). For the default role these are the
        literal 'p2j'/'j2p' strings the browser leg has always used; see
        ``_ROLE_DIRECTION_TAGS`` for why a Python<->Python link needs one
        side to use 'mirror' instead.
        """
        direction = msg.get('direction')

        if direction == self._self_direction:
            # Response to a request we made ourselves.
            await self._handle_response(msg)

        elif direction == self._peer_direction:
            # Fresh request (or push) from the peer.
            await self._handle_request(msg)

        else:
            logger.warning(
                f"CommMgr[{self._role}]._route_message: unrecognized direction "
                f"{direction!r} (expected {self._self_direction!r} or "
                f"{self._peer_direction!r}); dropping message"
            )

    async def _handle_response(self, msg: Dict[str, Any]):
        """Handle response from frontend to our request."""
        request_id = msg.get('request_id')

        if not request_id or request_id not in self._pending_requests:
            logger.warning(f"Received response for unknown request: {request_id}")
            return

        # Get request info. The message body is retained only so the request can
        # be replayed after a reconnect; it is not needed once a reply arrives.
        comm_id, message_id, _message, callback = self._pending_requests.pop(request_id)

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
                    'direction': self._peer_direction
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
                    'direction': self._peer_direction
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
                    'direction': self._peer_direction
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

        # NOTE: `get_ipython() is not None` alone is NOT sufficient here —
        # it is also true in a plain terminal `ipython` REPL, which has no
        # kernel. is_jupyter_kernel() checks for the `.kernel` attribute
        # that only a real notebook/kernel session has. See _environment.py.
        from ._environment import is_jupyter_kernel
        if is_jupyter_kernel():
            return 'jupyter'

        return 'websocket'

