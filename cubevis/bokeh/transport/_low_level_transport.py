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


def _dbg_write(msg: str) -> None:
    """Diagnostic helper. To enable, replace pass with a file write to ~/debug.txt."""
    pass

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
        self._closed = False

    def set_message_callback(self, callback: Callable[[Dict[str, Any]], None]):
        """Set callback for incoming messages."""
        self._message_callback = callback

    async def connect(self) -> None:
        """
        Perform WebSocket handshake.

        Waits for initialization message from frontend and validates it.
        Sends acknowledgment back.
        """
        from ...utils import deserialize, serialize

        try:
            logger.debug( "WebSocket waiting for initialization (comm_mgr_id=%s)", self._comm_mgr_id )

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
        if self._closed:
            return

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
        self._closed = True

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

    JS → Python:
                    JS calls channel.send(data) on the Colab comm object.
                    The bridge ESM relays it via bc_tx BroadcastChannel to
                    the kernel, which routes the comm_msg to Python _recv().
                    _recv() invokes the user callback on the main kernel thread.

    Python → JS (JupyterLab):
                    Python calls self._comm.send(msg).
                    The kernel delivers it directly to the JS rawComm.onMsg
                    handler registered by the application after connect().

    Python → JS (Colab):
                    Python calls google.colab.output.eval_js() to post
                    init/chunk/done messages to a session-specific
                    BroadcastChannel named 'cubevis_chunk_<comm_mgr_id>'.
                    All messages use this path regardless of size — small
                    messages produce a single chunk (total=1) while large
                    messages are split into multiple chunks of up to
                    colab_chunk_size bytes each (default 500KB).
                    Because BroadcastChannel is same-origin and cross-iframe,
                    delivery is independent of which iframe eval_js targets —
                    the _parents routing race that affects eval_js does not
                    affect BroadcastChannel delivery. The bridge ESM listens
                    on this channel, assembles chunks by index regardless of
                    arrival order, and once all chunks and the done signal
                    have arrived posts the complete message to a second
                    BroadcastChannel ('cubevis_rx_<comm_mgr_id>') which the
                    Bokeh app iframe receives via bc_rx.onmessage.

    Notebook usage
    --------------
        transport = CommsTransport(comm_mgr_id="my_pipe")
        transport.set_message_callback(handler)
        # The bridge widget is displayed automatically during __init__.
        # If CUBEVIS_DEBUG is set it shows connection status; otherwise it
        # is a zero-height invisible element with no visual footprint.
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
        self._closed = False
        self._debug = "CUBEVIS_DEBUG" in os.environ
        # _conn_event is set once Comm connection is established
        # This may be set and read in different threads/event loops
        # so asyncio.Event( ) will not work.
        self._conn_event = threading.Event()
        self._last_parent_header: dict = {}  # parent header of last received comm_msg
        self._colab_pending_replies: list = []  # FIFO queue of pending Colab replies

        # Threshold in bytes above which numpy arrays are sent as binary.
        # Set to a small value (e.g. 1024) to test chunking with small images.
        self.colab_binary_threshold: int = 65536  # 64KB (unused - kept for compat)
        # Max bytes per eval_js chunk. Lower for testing, raise if needed. This
        # defaults to 500KB but it is set low when debugging is enabled to ensure
        # that chunking is tested.
        self.colab_chunk_size: int = 5000 if "CUBEVIS_DEBUG" in os.environ else 500_000
        self._colab_inflight: int = 0  # count of requests received but not yet replied
        self._colab_bridge_parents: dict = {}  # parent header of the bridge cell
        # In Colab: display the bridge immediately from __init__.
        # CommsTransport is constructed during a cell's execution (the setup
        # cell), so that cell's output context is open. The bridge must render
        # in its OWN cell iframe — separate from the Bokeh app cell — because
        # BroadcastChannel does not deliver to the posting context itself.
        # If the bridge and CommsTransport share an iframe, bc_rx.postMessage()
        # in the bridge ESM never reaches CommsTransport's bc_rx.onmessage.
        #
        # In JupyterLab: use the preflight mechanism so the bridge renders
        # in the Bokeh app cell (single iframe, no BroadcastChannel needed).
        # Use the preflight mechanism for both JupyterLab and Colab.
        # display_bridge() must run in the same cell as ic.show() so that
        # Colab's CDN widget manager routes comm_msg to the bridge model
        # (Colab only routes comm_msg to widgets in the currently-executing
        # cell's output context). This ensures model.on("msg:custom") fires
        # when Python calls self._bridge.send().
        # The window["cubevis_rx_cb_..."] callback then delivers to CommsTransport
        # which shares the same window (same iframe, same cell output).
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
            from ...utils import LazySummarize
            import traceback
            from pathlib import Path

            if getattr(self, '_closed', False):
                return

            #logger.debug( "CommsTransport._recv: %s", LazySummarize(msg) )
            # Capture parent header only for non-poll messages.
            # Poll messages use the bridge iframe context (captured by Colab kernel automatically).
            _data_peek = msg.get("content", {}).get("data", {})
            _is_poll_msg = isinstance(_data_peek, dict) and _data_peek.get("type") == "cubevis_poll"
            if not _is_poll_msg:
                self._last_parent_header = msg.get("parent_header", {})
                if not self._last_parent_header:
                    try:
                        from IPython import get_ipython as _gip2
                        _ip3 = _gip2()
                        if _ip3 is not None and hasattr(_ip3, 'kernel'):
                            k = _ip3.kernel
                            if hasattr(k, '_parent_header'):
                                self._last_parent_header = k._parent_header
                                if hasattr(k, '_parent_ident'):
                                    self._last_parent_ident = k._parent_ident
                    except Exception:
                        #logger.exception( "CommsTransport._recv error encountered" )
                        pass

            data = msg.get("content", {}).get("data", {})

            # Handle poll from widget bridge.
            # Delivery is scheduled on the IOLoop so _recv returns immediately,
            # keeping the kernel free to process incoming messages.
            if _is_poll_msg:
                _pending = getattr(self, "_colab_pending_replies", [])
                if _pending:
                    import pathlib as _plib
                    import threading as _thr
                    import json as _pj
                    _snapshot = _pending.pop(0)  # take first reply, leave rest queued
                    _mgr_id = self._comm_mgr_id

                    def _deliver_in_thread(_snap=_snapshot, _mid=_mgr_id, _self=self):
                        """Deliver envelope via eval_js + BroadcastChannel.

                        Uses a session-specific BroadcastChannel so eval_js iframe
                        routing (_parents) is irrelevant — BroadcastChannel delivers
                        cross-iframe to the bridge ESM listener regardless of which
                        iframe eval_js targets.
                        """
                        if getattr(_self, '_closed', False):
                            return

                        try:
                            from google.colab import output as _co
                            import json as _pj
                            import uuid as _uuid_d

                            # Set _parents to the bridge cell context so eval_js runs
                            # in a valid execution context. The exact iframe doesn't
                            # matter for delivery — BroadcastChannel handles routing —
                            # but without a valid context Colab may drop eval_js calls.
                            _ph_d = getattr(_self, '_colab_bridge_parents', {})
                            try:
                                from IPython import get_ipython as _gip_d
                                _ip_d = _gip_d()
                                _k_d = _ip_d.kernel if (_ip_d and hasattr(_ip_d, 'kernel')) else None
                                if _k_d is not None and _ph_d and hasattr(_k_d, '_parents'):
                                    _k_d._parents.clear()
                                    _k_d._parents.update(_ph_d)
                            except Exception:
                                pass

                            _del_fn   = f"_cubevis_pollDelivered_{_mid}"
                            _stop_fn  = f"_cubevis_stopPoll_{_mid}"
                            _env_s    = _pj.dumps(_snap)
                            _inflight = getattr(_self, "_colab_inflight", 0)
                            _has_more = bool(getattr(_self, "_colab_pending_replies", [])) or _inflight > 0
                            _tok      = _uuid_d.uuid4().hex[:16]
                            _CHUNK    = getattr(_self, "colab_chunk_size", 500_000)
                            _chunks   = [_env_s[i:i+_CHUNK] for i in range(0, len(_env_s), _CHUNK)]
                            _chunk_bc = _pj.dumps(f"cubevis_chunk_{_mid}")

                            # Signal start of delivery
                            _co.eval_js(
                                #f"console.log('CUBEVIS init tok={_tok} total={len(_chunks)}');"
                                f"(new BroadcastChannel({_chunk_bc})).postMessage({{tok:'{_tok}',type:'init',total:{len(_chunks)}}});",
                                ignore_result=True
                            )

                            # Send each chunk with index for ordered reassembly
                            for _ci, _chunk in enumerate(_chunks):
                                _co.eval_js(
                                    #f"console.log('CUBEVIS chunk tok={_tok} idx={_ci}');"
                                    f"(new BroadcastChannel({_chunk_bc})).postMessage({{tok:'{_tok}',type:'chunk',idx:{_ci},data:{_pj.dumps(_chunk)}}});",
                                    ignore_result=True
                                )

                            # Signal completion — bridge ESM assembles and delivers
                            _co.eval_js(
                                #f"console.log('CUBEVIS done tok={_tok}');"
                                f"(new BroadcastChannel({_chunk_bc})).postMessage({{tok:'{_tok}',type:'done',"
                                f"del_fn:{_pj.dumps(_del_fn)},stop_fn:{_pj.dumps(_stop_fn) if not _has_more else 'null'}}});",
                                ignore_result=True
                            )
                            #logger.debug("<<poll>> delivered via %s chunk(s) (%s bytes)", len(_chunks), len(_env_s))

                        except Exception:
                            logger.exception("<<poll>> thread eval_js failed")

                    _thr.Thread(target=_deliver_in_thread, daemon=True).start()
                # nothing pending - JS manages idle timeout itself
                return

            # first check if it is a CommsTransport message
            if data.get("type") == "cubevis_message":
                # Poll is started by JS bc_tx.onmessage hook when the request was sent
                self._colab_inflight = getattr(self, "_colab_inflight", 0) + 1
                from ...utils import deserialize
                #logger.debug(f"CommsTransport._recv: expected message {data}, {self._callback}")
                try:
                    raw = data.get("data", "{}")

                    if isinstance( raw, str ):
                        actual_message = deserialize(raw)
                        #logger.debug(f"CommsTransport._recv app message: {actual_message}")
                        _invoke_callback(actual_message)
                    else:
                        logger.error(f"_recv: data does not seem to be in a serialized format")

                except Exception as e:
                    logger.warning(f"CommsTransport._recv: deserialize failed: {e}, raw={raw[:200]}")

            else:
                # when testing simple messages are sent directly using
                # the Jupyter/Colab comm object
                #logger.debug( "CommsTransport._recv: %s", LazySummarize(data) )

                # Skip transport-layer control messages — these are handled by
                # the transport itself and must not be forwarded to the
                # application callback.  Without this guard, comm_opened in
                # particular causes Python to send a response whose request_id
                # the JS CommMgr has never registered, producing a flood of
                # "Received response for unknown request" warnings.
                _msg_type = data.get("type", "") if isinstance(data, dict) else ""
                if _msg_type in ("comm_opened", "ping", "heartbeat", "closing"):
                    #logger.debug("CommsTransport._recv: skipping control message type=%s", _msg_type)
                    pass
                else:
                    try:
                        _invoke_callback(data)
                        logger.debug( "CommsTransport._recv: callback successful" )
                    except Exception:
                        logger.exception( "CommsTransport._recv: error invoking callback function" )

        comm.on_msg(_recv)

        # Refresh the bridge iframe parent header every time a comm opens.
        # _on_comm_open fires in the bridge cell's kernel execution context,
        # so kernel._parents here reflects the live bridge iframe — not the
        # stale context captured during display_bridge() in the setup cell.
        # This ensures that on a second (or later) GUI run the eval_js call
        # in _deliver_in_thread targets the correct bridge iframe, not the
        # one from the previous run.
        if self._is_colab():
            try:
                from IPython import get_ipython as _gip_open
                _ip_open = _gip_open()
                if _ip_open is not None and hasattr(_ip_open, 'kernel'):
                    _k_open = _ip_open.kernel
                    if hasattr(_k_open, '_parents') and isinstance(_k_open._parents, dict):
                        self._colab_bridge_parents = dict(_k_open._parents)

                        #from google.colab import output as _co
            except Exception:
                logger.exception("_on_comm_open: failed to refresh _colab_bridge_parents")

        if not self._connected:
            self._connected = True
            self._conn_event.set()

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
                #logger.debug("CommsTransport: Colab custom widget manager enabled")
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


        if self._is_colab():
            try:
                from IPython import get_ipython as _gip_br
                _ip_br = _gip_br()
                if _ip_br and hasattr(_ip_br, 'kernel'):
                    _k_br = _ip_br.kernel
                    if hasattr(_k_br, '_parents') and isinstance(_k_br._parents, dict):
                        # Using dict() creates a shallow copy, which is good practice here
                        self._colab_bridge_parents = dict(_k_br._parents)

            except Exception:
                # Automatically captures the stack trace and the error message
                logger.exception("<<display_bridge>> parent capture failed")

        import time as _time
        _esm_ts = str(int(_time.time()))
        esm = "// cubevis-esm:" + _esm_ts + "\n" + r"""
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
                        console.log("CUBEVIS DEBUG: Comm stored for", targetId, window);
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

                        // The widget bridge iframe is the sole owner of the kernel comm.
                        // Colab's channel.messages does NOT deliver Python's comm.send()
                        // calls (proven by diagnostic testing). Instead we use two
                        // BroadcastChannels as a same-origin cross-iframe bus:
                        //
                        //   bc_tx ("cubevis_tx_<id>"): JS -> Python
                        //     Any iframe posts here; bridge receives and calls channel.send()
                        //
                        //   bc_rx ("cubevis_rx_<id>"): Python -> JS
                        //     Python calls self._bridge.send() → anywidget model →
                        //     model.on("msg:custom") here → bc_rx.postMessage()
                        //     Any iframe listening on bc_rx receives the reply.

                        const channel = await colabComms.open(targetId, {});

                        // TX bus: relay JS->Python from any iframe to the kernel
                        const bc_tx = new BroadcastChannel(`cubevis_tx_${targetId}`);
                        bc_tx.onmessage = (event) => {
                            //if (isDebug) console.log("CUBEVIS DEBUG: bc_tx relay to kernel:", event.data);
                            channel.send(event.data);
                        };

                        // On-demand polling: JS sends a poll only when waiting for a reply.
                        // This avoids flooding the kernel with empty polls.
                        // bc_tx.onmessage (any JS->Python message) triggers a poll sequence.
                        // Python's _recv delivers the reply via eval_js in the bridge iframe
                        // context, then BroadcastChannel delivers to the Bokeh app iframe. ✓
                        let _pollActive = false;
                        let _pollTimer = null;
                        let _consecutiveEmpty = 0;

                        function _startPoll() {
                            _consecutiveEmpty = 0;
                            if (_pollActive) return;
                            _pollActive = true;
                            function _doPoll() {
                                if (!_pollActive) return;
                                channel.send({ type: "cubevis_poll", target_id: targetId });
                                // Backoff: 50ms → 500ms for consecutive empty polls
                                const _interval = Math.min(500, 50 * Math.pow(2, Math.min(_consecutiveEmpty, 3)));
                                _pollTimer = setTimeout(_doPoll, _interval);
                                _consecutiveEmpty++;
                            }
                            _doPoll();
                        }

                        function _stopPoll() {
                            _pollActive = false;
                            if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
                        }

                        // Python controls the poll loop via threaded eval_js (non-blocking).
                        // No idle timeout - Python explicitly stops when nothing is pending.
                        window[`_cubevis_startPoll_${targetId}`] = _startPoll;
                        window[`_cubevis_stopPoll_${targetId}`]  = _stopPoll;
                        // After delivery, reset backoff so next reply is fast
                        window[`_cubevis_pollDelivered_${targetId}`] = () => { _consecutiveEmpty = 0; };
                        // Chunk delivery via BroadcastChannel — routing-independent.
                        // Python's eval_js sends init/chunk/done messages on 'cubevis_chunk'.
                        // Because BroadcastChannel works cross-iframe on same origin,
                        // _parents routing of eval_js calls no longer affects delivery.
                        // Per-session chunk channel — named by targetId so multiple
                        // GUI sessions on the same page don't cross-deliver chunks.
                        const _chunkBc = new BroadcastChannel(`cubevis_chunk_${targetId}`);
                        const _chunkBufs = {};
                        _chunkBc.addEventListener('message', (event) => {
                            const {tok, type, data, idx, total, del_fn, stop_fn} = event.data;
                            if (!tok) return;
                            //console.log(`CUBEVIS chunkBc received: type=${type} tok=${tok} idx=${idx} total=${total}`);
                            if (type === 'init') {
                                _chunkBufs[tok] = {chunks: new Array(total), received: 0, total, done: false, del_fn, stop_fn};
                            } else if (type === 'chunk') {
                                const buf = _chunkBufs[tok];
                                if (buf && idx !== undefined) {
                                    buf.chunks[idx] = data;
                                    buf.received++;
                                }
                            } else if (type === 'done') {
                                // done may arrive before all chunks — store metadata and check
                                const buf = _chunkBufs[tok];
                                if (buf) {
                                    buf.done = true;
                                    buf.del_fn = del_fn;
                                    buf.stop_fn = stop_fn;
                                }
                            }
                            // Attempt assembly whenever a message arrives —
                            // proceeds only when done flag is set and all chunks received
                            const buf = _chunkBufs[tok];
                            if (buf && buf.done && buf.received === buf.total) {
                                delete _chunkBufs[tok];
                                try {
                                    const msg = JSON.parse(buf.chunks.join(''));
                                    const cb = window[`cubevis_rx_cb_${targetId}`];
                                    if (typeof cb === 'function') cb(msg);
                                    try {
                                        const bc = new BroadcastChannel(`cubevis_rx_${targetId}`);
                                        bc.postMessage(msg);
                                        bc.close();
                                    } catch(e) {}
                                    if (buf.del_fn && window[buf.del_fn]) window[buf.del_fn]();
                                    if (buf.stop_fn && window[buf.stop_fn]) window[buf.stop_fn]();
                                } catch(e) {
                                    console.error(`CUBEVIS: chunk assembly error tok=${tok}:`, e);
                                }
                            }
                        });

                        // Teardown hook
                        window[`_cubevis_teardown_${targetId}`] = () => {
                            _stopPoll()
                            // Clean up chunk channel and any pending buffers
                            Object.keys(_chunkBufs).forEach(k => delete _chunkBufs[k])
                            _chunkBc.close()
                            bc_tx.close()
                            bc_rx.close()
                            channel.close?.()
                        }

                        // Hook into bc_tx: start polling whenever JS sends a message to Python
                        const _origBcTxOnmessage = bc_tx.onmessage;
                        bc_tx.onmessage = (event) => {
                            _origBcTxOnmessage(event);
                            _startPoll();
                        };

                        // Register stop function for Python to call after delivering reply

                        // RX bus: Python->JS via anywidget model → deliver to listeners.
                        // Two delivery paths:
                        //   1. window["cubevis_rx_cb_"+id](msg) — for same-iframe delivery
                        //      (BroadcastChannel does NOT deliver to sender's own context)
                        //   2. bc_rx.postMessage(msg) — for cross-iframe delivery
                        const bc_rx = new BroadcastChannel(`cubevis_rx_${targetId}`);
                        model.on("msg:custom", (msg) => {
                            // This fires when Python calls self._bridge.send(payload)
                            // If it's a cubevis_reply, extract the envelope and route it.
                            // Otherwise treat msg as the envelope directly (legacy path).
                            const envelope = (msg && msg.type === "cubevis_reply")
                                ? msg.envelope : msg;
                            if (msg && msg.type === "cubevis_binary") {
                                // Binary array delivery: reconstruct typed array from buffer
                                // and store by token for substitution into pending envelopes.
                                try {
                                    const bufs = buffers || [];
                                    if (bufs.length > 0) {
                                        const buf = bufs[0];
                                        let typedArr;
                                        if (msg.dtype === "uint8" || msg.dtype === "bool") {
                                            typedArr = new Uint8Array(buf);
                                        } else if (msg.dtype === "float32") {
                                            typedArr = new Float32Array(buf);
                                        } else if (msg.dtype === "float64") {
                                            typedArr = new Float64Array(buf);
                                        } else if (msg.dtype === "int32") {
                                            typedArr = new Int32Array(buf);
                                        } else if (msg.dtype === "int64") {
                                            typedArr = new BigInt64Array(buf);
                                        } else {
                                            typedArr = new Uint8Array(buf);
                                        }
                                        // Store for substitution
                                        window[`_cubevis_bin_${msg.token}`] = {
                                            data: typedArr,
                                            dtype: msg.dtype,
                                            shape: msg.shape
                                        };
                                        // Notify CommsTransport that a binary token arrived
                                        const _arrivedCb = window[`cubevis_binary_arrived_${msg.comm_mgr_id}`];
                                        if (typeof _arrivedCb === "function") _arrivedCb();
                                    }
                                } catch(e) {
                                    console.log("CUBEVIS binary error: " + e);
                                }
                                return;
                            }
                    if (msg && msg.type === "cubevis_reply") {
                                console.log("CUBEVIS poll-deliver: bridge.send reply → bc_rx");
                                // Reset backoff after delivery
                                if (typeof _consecutiveEmpty !== 'undefined') _consecutiveEmpty = 0;
                            }
                            // Same-iframe: call registered callback directly
                            const cb = window[`cubevis_rx_cb_${targetId}`];
                            if (typeof cb === "function") {
                                cb(envelope);
                            }
                            // Cross-iframe: broadcast to Bokeh app iframe
                            try {
                                const bc = new BroadcastChannel(`cubevis_rx_${targetId}`);
                                bc.postMessage(envelope);
                                bc.close();
                            } catch(e) {}
                        });

                        const comm = {
                            // Direct JS->Python from this iframe
                            send(data) { channel.send(data); },
                            onMsg: null,
                        };

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
        # JS->Python only; Python->JS travels via anywidget model → bc_rx.
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

        # Wrap the bridge in an ipywidgets.Output so it renders in its own
        # sandboxed iframe even when display_bridge() is called from the same
        # cell as the Bokeh app. Each Output widget gets its own iframe in
        # Colab, giving the bridge a separate browsing context from CommsTransport.
        # This ensures BroadcastChannel messages from the bridge ESM are
        # delivered to CommsTransport's bc_rx.onmessage (different context).
        if self._is_colab():
            try:
                import ipywidgets as _ipyw
                _out = _ipyw.Output()
                with _out:
                    display(self._bridge)
                display(_out)
                logger.debug(f"CommsTransport.display_bridge: bridge wrapped in Output widget for {self._comm_mgr_id}")
            except ImportError:
                display(self._bridge)
                logger.debug(f"CommsTransport.display_bridge: bridge displayed directly (no ipywidgets) for {self._comm_mgr_id}")
        else:
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
        ####################################################################################################
        ### This is the last Python => JavaScript test to check for bi-directional communications over.  ###
        ### Colab Comm objects. It was successful so the next iteration will be to explore this a little ###
        ### further to see if the use of Colab's eval_js is not actually required.                       ###
        ####################################################################################################

        if self._connected: return

        if self._bridge is None:
            raise RuntimeError( "display_bridge() must be called before connect()." )

        deadline = asyncio.get_event_loop( ).time( ) + timeout
        while not self._conn_event.is_set( ):
            if asyncio.get_event_loop( ).time( ) > deadline:
                raise RuntimeError( f"CommsTransport: JS handshake timed out after {timeout}s" )
            await asyncio.sleep( 0.1 )


        # this is often never reached for Colab because self._connected is already True and
        # self._bridge is already set (see short circuits above)
        #logger.debug("CommsTransport.connect: handshake complete")

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
            from ...utils import LazySummarize
            logger.debug(
                "%s wrapper: invoking %s callback with %s",
                self,
                "async" if is_async else "sync",
                LazySummarize(msg)
            )

            try:
                if is_async:
                    await callback(msg)
                else:
                    callback(msg)
            except Exception:
                logger.exception("set_message_callback.wrapper error")

        # Store the wrapper as the internal callback
        self._callback = wrapper

    async def send_message(self, message: Dict[str, Any]) -> None:
        from ...utils import serialize
        # Use lazy formatting to avoid string building/summarizing unless DEBUG is on
        from ...utils import LazySummarize
        #logger.debug(
        #    "<<send_message>> to %d comms (is_colab=%s, bridge=%s): %s",
        #    len(self._comm_objs),
        #    self._is_colab(),
        #    self._bridge is not None,
        #    LazySummarize(message)
        #)

        if not self._connected:
            # Note: Log the error before raising if you want it in the persistent log,
            # otherwise the raised exception will be the only record.
            raise RuntimeError("CommsTransport: not connected")

        envelope = {
            "type": "cubevis_message",
            "comm_mgr_id": self._comm_mgr_id,
            "data": serialize(message)
        }

        if self._is_colab():
            # Colab: Python->JS via google.colab.output.eval_js() (blocking/synchronous).
            # eval_js executes JS in the output context of whichever cell triggered the
            # current kernel execution. For spectrum clicks, that's the Bokeh app cell —
            # the same iframe where CommsTransport registered window["cubevis_rx_cb_..."].
            # The window callback delivers directly to colabComm.onMsg → handleJupyterMessage.
            # BroadcastChannel is also posted for any cross-iframe listeners.
            import json as _json
            # eval_js only works when called from the main IPython kernel thread.
            # When send_message is called from a Tornado IOLoop callback (as in the
            # full app), eval_js with ignore_result=True silently does nothing.
            # Fix: schedule the eval_js call on the kernel's main asyncio loop using
            # IPython's kernel.io_loop, which runs on the main thread where eval_js works.
            try:
                from google.colab import output as _colab_output
                cb_name = f"cubevis_rx_cb_{self._comm_mgr_id}"
                bc_name = f"cubevis_rx_{self._comm_mgr_id}"
                env_json = _json.dumps(envelope)
                js_code = (
                    f"(()=>{{"
                    f"const msg={env_json};"
                    f"const cb=window[{_json.dumps(cb_name)}];"
                    f"if(typeof cb==='function'){{"
                    f"  cb(msg);"
                    f"}} else {{"
                    f"}}"
                    f"try{{const bc=new BroadcastChannel({_json.dumps(bc_name)});bc.postMessage(msg);bc.close();}}catch(e){{}}"
                    f"}})();"
                )

                _parent_header = getattr(self, '_last_parent_header', {})

                # Try Bokeh document approach: update a Bokeh model property.
                # The Bokeh session has its own always-active WebSocket to the browser.
                # Store reply for delivery via poll mechanism.
                # The widget bridge polls Python every 250ms via channel.send({type:"cubevis_poll"}).
                # Python's _recv handles the poll synchronously and calls eval_js there.
                # eval_js runs in the widget bridge iframe context (different from Bokeh app iframe),
                # so BroadcastChannel delivers to CommsTransport's bc_rx.onmessage. ✓
                if not hasattr(self, "_colab_pending_replies"):
                    self._colab_pending_replies = []
                self._colab_pending_replies.append(envelope)
                _if = max(0, getattr(self, "_colab_inflight", 1) - 1)
                self._colab_inflight = _if
                #logger.debug("<<send_message>> reply queued (depth=%d inflight=%d)",
                #             len(self._colab_pending_replies), _if)
                # If inflight==0, the request came via an external channel (not bc_tx),
                # so bc_tx.onmessage never fired and _startPoll was never called.
                # Start the poll now so this reply gets delivered.
                if _if == 0 and getattr(self, '_bridge', None) is not None:
                    # External channel path: deliver via bridge.send() which is a true
                    # fire-and-forget ZMQ push — no kernel round-trip, no blocking.
                    # bridge.send() works here because we ARE in the active kernel
                    # execution context (the async def body running in the user cell).
                    try:
                        _snap_d = self._colab_pending_replies.pop()
                        self._bridge.send({
                            "type": "cubevis_reply",
                            "comm_mgr_id": self._comm_mgr_id,
                            "envelope": _snap_d
                        })
                        #logger.debug("<<send_message>> direct bridge.send delivery (inflight=0 path)")
                    except Exception as _spe:
                        logger.exception("<<send_message>> direct delivery failed")

            except Exception as e:
                logger.warning("CommsTransport.send_message: eval_js failed: %s", e)
        else:
            # JupyterLab: single bidirectional kernel comm
            comm_objs = getattr(self, '_comm_objs', None)
            if not comm_objs:
                raise RuntimeError("CommsTransport: not connected (no comm available)")
            try:
                comm_objs[0].send(envelope)
                #logger.debug("<<send_message>> sent via comm")
            except Exception as e:
                logger.warning("CommsTransport.send_message: comm send failed: %s", e)

    async def run(self) -> None:
        """Keep the transport alive until disconnected."""
        while self._connected:
            await asyncio.sleep(0.1)


    async def close(self) -> None:
        # Close all Comms
        #logger.debug(f"CommsTransport.close: called for {self._comm_mgr_id} closed={self._closed}")
        if self._closed:
            return
        self._closed = True

        for c in getattr(self, '_comm_objs', []):
            try:
                c.close()
            except Exception:
                pass

        # Tear down the JS bridge — stops bc_tx polling which prevents
        # _parents contamination during subsequent GUI sessions
        if self._is_colab():
            import json as _json
            _ph_c  = getattr(self, '_colab_bridge_parents', {})
            _mid_c = self._comm_mgr_id
            teardown_fn = f"_cubevis_teardown_{_mid_c}"
            teardown_js = (
                f"if(window[{_json.dumps(teardown_fn)}])"
                f"  window[{_json.dumps(teardown_fn)}]();"
            )

            _done = threading.Event()

            def _do_teardown():
                try:
                    from google.colab import output as _co
                    from IPython import get_ipython as _gip_c
                    _ip_c = _gip_c()
                    _k_c  = _ip_c.kernel if (_ip_c and hasattr(_ip_c, 'kernel')) else None
                    if _k_c is not None and _ph_c and hasattr(_k_c, '_parents'):
                        _k_c._parents.clear()
                        _k_c._parents.update(_ph_c)
                    _co.eval_js(teardown_js, ignore_result=True)
                except Exception:
                    logger.exception("close: teardown eval_js failed")
                finally:
                    _done.set()

            self._main_ioloop.add_callback(_do_teardown)
            # Poll without blocking the event loop
            for _ in range(40):  # up to 2 seconds
                if _done.is_set():
                    break
                await asyncio.sleep(0.05)

            #logger.debug(f"CommsTransport.close: teardown done={_done.is_set()} for {self._comm_mgr_id}")

        self._comm_objs = []
        self._connected = False
        self._bridge = None

