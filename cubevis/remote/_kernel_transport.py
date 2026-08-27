########################################################################
# Kernel-facing transports for remote execution (Chunk 1, Task 5).
#
# Two TransportBase implementations, mirroring the browser leg's
# WebSocketTransport / CommsTransport split, but for the new
# P_local <-> remote-kernel leg described in the design doc §2-3:
#
#   KernelClientTransport -- runs in P_local. Plays the *frontend* role
#       toward the remote kernel, using plain jupyter_client (whatever
#       actually starts/connects the kernel -- e.g. an sshpyk-provisioned
#       AsyncKernelManager -- is the caller's concern; this transport just
#       needs an AsyncKernelManager to drive). Pairs with a CommMgr in
#       CommMgr.ROLE_MIRROR.
#
#   KernelCommTransport -- runs inside the remote kernel process itself.
#       Plays the same role CommsTransport plays for the browser leg
#       (register a comm target, relay send()/on_msg() through it), but
#       with none of CommsTransport's anywidget/Colab/browser-bridge
#       machinery -- see its docstring below for why that machinery
#       specifically cannot be reused here. Pairs with a CommMgr in
#       CommMgr.ROLE_DEFAULT, run the same way _build_comm() already
#       constructs one for any local app.
#
# Both were checked against the real jupyter_client (8.9.1) / comm /
# ipykernel APIs, not assumed -- see cubevis-remote-execution-design.md's
# Chunk 1 addendum for what was verified and how.
#
# --- On this module's dependencies on cubevis (isolation note) --------
# This subpackage (cubevis.remote) is scoped to eventually be usable
# outside cubevis. Its dependency on cubevis.bokeh.transport's *public*
# surface (CommMgr, TransportBase) is fundamental and permanent -- that's
# the thing being bridged to a remote kernel, not an incidental coupling.
# Two narrower dependencies remain, both on non-underscore-prefixed-only
# modules, called out explicitly rather than silently relied on:
#   - cubevis.utils.serialize/deserialize: the wire-format encoding used
#     by CommsTransport/WebSocketTransport too, so staying consistent
#     with it (rather than rolling a separate JSON encoding here) matters
#     more than avoiding the import. Public module, low risk.
#   - cubevis.bokeh.transport._environment.get_ipython_kernel_shell: a
#     small, generic "am I running inside a Jupyter kernel" check that
#     happens to live in a *private* (underscore-prefixed) module inside
#     bokeh.transport, despite not being Bokeh-specific at all. This is
#     the one real seam: if cubevis.remote is ever extracted to stand
#     alone, this helper should be promoted to a public location (or
#     vendored) rather than reaching into bokeh.transport's private
#     internals from outside the package.
########################################################################
from __future__ import annotations

import asyncio
import logging
from queue import Empty
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional
from uuid import uuid4

from cubevis.bokeh.transport import TransportBase
from cubevis.utils import deserialize, serialize

if TYPE_CHECKING:
    from jupyter_client.asynchronous.client import AsyncKernelClient
    from jupyter_client.manager import AsyncKernelManager

logger = logging.getLogger(__name__)

__all__ = ["KernelClientTransport", "KernelCommTransport"]


########################################################################
# P_local side: frontend role, backed by jupyter_client
########################################################################
class KernelClientTransport(TransportBase):
    """
    TransportBase for P_local's kernel-facing side.

    Plays the *frontend* role toward a single remote Jupyter kernel:
    opens one multiplexed comm (mirroring the browser leg's "one comm per
    kernel" discipline from the design doc), sends every outgoing message
    as a `comm_msg` on the shell channel, and reads incoming `comm_msg`
    traffic off iopub in `run()`.

    This transport does NOT itself decide *how* the kernel is
    started/reached -- that's the `AsyncKernelManager`'s (and its
    provisioner's) job. Against a real cluster node that provisioner is
    `sshpyk-provisioner` (config'd via a kernelspec's
    `metadata.kernel_provisioner` stanza, per sshpyk's own docs); for the
    Chunk 1 spike it is exercised against a real local `ipykernel`
    process using the *same* `AsyncKernelManager`/`AsyncKernelClient` API
    surface sshpyk sits underneath (see
    test_task5_kernel_transport_spike.py). What was NOT exercised there,
    for lack of cluster/SSH access in that environment: the actual SSH
    tunneling and the `existing=`/`persistent=` reattach config sshpyk
    layers on top -- that is real, already-implemented sshpyk
    functionality (see `_worker.py` / the design doc addendum), just not
    something that sandbox could dial an SSH connection to test.

    Construction does not start anything (matches WebSocketTransport,
    which is handed an already-live websocket, and CommsTransport, which
    relies on an already-running kernel) -- `connect()` does the work:
    starts the kernel manager if needed, waits for the kernel to be
    ready, and completes the `comm_open` handshake against whatever
    target name the kernel side's `KernelCommTransport` registered.

    Most callers should not construct this directly -- see
    `cubevis.remote.open_remote_kernel_link()`, which wires this together
    with a `CommMgr(role=CommMgr.ROLE_MIRROR)` via `CommMgr.initialize()`
    (using the `'remote_kernel'` transport_type) instead of requiring
    private-attribute access from calling code.
    """

    def __init__(
        self,
        comm_mgr_id: str,
        kernel_manager: "AsyncKernelManager",
        target_name: Optional[str] = None,
        abort: Optional[Callable] = None,
        ready_timeout: float = 60.0,
    ):
        super().__init__(comm_mgr_id, abort)
        self._km = kernel_manager
        self._target_name = target_name or comm_mgr_id
        self._ready_timeout = ready_timeout

        self._client: Optional["AsyncKernelClient"] = None
        self._comm_id = str(uuid4())
        self._callback: Optional[Callable] = None
        self._connected = False
        self._closed = False

    async def connect(self) -> None:
        if not self._km.has_kernel:
            logger.debug(f"KernelClientTransport.connect: starting kernel for {self._comm_mgr_id}")
            await self._km.start_kernel()
        else:
            logger.debug(
                f"KernelClientTransport.connect: reattaching to already-running "
                f"kernel for {self._comm_mgr_id}"
            )

        self._client = self._km.client()
        self._client.start_channels()
        await self._client.wait_for_ready(timeout=self._ready_timeout)

        msg = self._client.session.msg(
            "comm_open",
            content={"comm_id": self._comm_id, "target_name": self._target_name, "data": {}},
        )
        self._client.shell_channel.send(msg)
        self._connected = True
        logger.debug(
            f"KernelClientTransport.connect: comm_open sent "
            f"(comm_id={self._comm_id}, target_name={self._target_name})"
        )

    def set_message_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        self._callback = callback

    async def send_message(self, message: Dict[str, Any]) -> None:
        if not self.is_connected() or self._client is None:
            raise RuntimeError("KernelClientTransport: not connected")

        msg = self._client.session.msg(
            "comm_msg",
            content={"comm_id": self._comm_id, "data": {"envelope": serialize(message)}},
        )
        self._client.shell_channel.send(msg)

    async def run(self) -> None:
        """
        Poll iopub for comm_msg traffic addressed to our comm_id.

        Polls rather than blocking indefinitely so `close()` (which just
        sets `_closed`) can stop this loop promptly -- `get_iopub_msg`
        with a short timeout raises `queue.Empty` on the jupyter_client
        side rather than blocking forever, matching the same "keep the
        loop alive, check a flag" shape WebSocketTransport/CommsTransport
        already use.
        """
        if self._client is None:
            raise RuntimeError("KernelClientTransport.run: connect() was not called")

        while not self._closed:
            try:
                msg = await self._client.get_iopub_msg(timeout=0.5)
            except Empty:
                continue
            except Exception:
                logger.exception("KernelClientTransport.run: error reading iopub")
                continue

            if msg.get("msg_type") not in ("comm_msg", "comm_close"):
                continue
            content = msg.get("content", {})
            if content.get("comm_id") != self._comm_id:
                continue
            if msg["msg_type"] == "comm_close":
                logger.debug(f"KernelClientTransport.run: peer closed comm {self._comm_id}")
                self._connected = False
                continue

            envelope = content.get("data", {}).get("envelope")
            if envelope is None:
                logger.warning("KernelClientTransport.run: comm_msg with no envelope, dropping")
                continue

            try:
                inner = deserialize(envelope)
            except Exception:
                logger.exception("KernelClientTransport.run: deserialize failed")
                continue

            if self._callback is not None:
                await self._callback(inner)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self._client is not None:
            try:
                msg = self._client.session.msg(
                    "comm_close", content={"comm_id": self._comm_id, "data": {}}
                )
                self._client.shell_channel.send(msg)
            except Exception:
                logger.exception("KernelClientTransport.close: error sending comm_close")
            try:
                self._client.stop_channels()
            except Exception:
                logger.exception("KernelClientTransport.close: error stopping channels")

        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and not self._closed


########################################################################
# Remote kernel side: default role, headless (no browser/anywidget)
########################################################################
class KernelCommTransport(TransportBase):
    """
    TransportBase for the CommMgr running *inside* the remote kernel
    process.

    `CommsTransport` (the only existing `'jupyter'` transport) cannot
    serve this role: its `__init__` unconditionally calls
    `BokehInit.get_app_context().add_preflight_callable(self.display_bridge)`,
    and `display_bridge()` builds an `anywidget.AnyWidget` and calls
    `display()`, then `connect()` blocks on `self._conn_event`, which is
    only ever set from `_on_comm_open()` fired by a *JS* comm handshake
    (JupyterLab's widget manager, or the Colab eval_js/BroadcastChannel
    path). None of that has a counterpart when the peer is
    `KernelClientTransport` speaking plain jupyter_client -- there is no
    browser, no widget manager, nothing to render the bridge for. This
    was flagged as an open question in the design doc's Task 3
    ("reuse CommMgr as-is, or a lighter base class") but is really a
    separate, transport-level finding: `CommMgr` itself (queueing,
    reconnect, role tagging) is fine to reuse via `role=CommMgr.ROLE_DEFAULT`;
    the *browser-coupled parts of CommsTransport specifically* are not, and
    a headless sibling is needed regardless of the CommMgr-reuse decision.

    What this class keeps from CommsTransport: registering a comm target
    directly on the kernel's own comm_manager. `register_target()` is
    already transport-agnostic -- it fires for ANY `comm_open` with a
    matching target_name, regardless of whether the opener is JS via
    anywidget or a raw `jupyter_client` frontend (which is exactly what
    `KernelClientTransport` is). What it drops: the anywidget bridge, the
    Colab eval_js/BroadcastChannel chunking path, and the JS-side
    handshake wait -- none of which apply when both ends are Python.

    `target_name` is deliberately decoupled from `comm_mgr_id` (mirroring
    `KernelClientTransport`'s constructor): the design doc's "exactly one
    Jupyter comm per kernel" discipline means there is only ever one
    worker per kernel process, so `_worker.py`'s start-vs-reattach
    mechanism (`ensure_remote_worker`) uses a single well-known constant
    target name rather than a freshly-generated `comm_mgr_id` -- letting
    a *new* `KernelClientTransport` in a reattaching P_local process dial
    in without first having to learn a previous session's
    randomly-generated id from anywhere.
    """

    def __init__(self, comm_mgr_id: str, abort: Optional[Callable] = None,
                 target_name: Optional[str] = None,
                 open_timeout: float = 60.0):
        super().__init__(comm_mgr_id, abort)
        self._target_name = target_name or comm_mgr_id
        self._callback: Optional[Callable] = None
        self._connected = False
        self._closed = False
        self._comm = None  # the comm.BaseComm/ipykernel.comm.Comm opened by the peer
        self._open_event = asyncio.Event()
        self._open_timeout = open_timeout

    async def connect(self) -> None:
        # See the module docstring's isolation note -- this is the one
        # private-module dependency this subpackage has.
        from cubevis.bokeh.transport._environment import get_ipython_kernel_shell

        shell = get_ipython_kernel_shell()
        if shell is None:
            raise RuntimeError(
                "KernelCommTransport requires a real Jupyter kernel session "
                "(is_jupyter_kernel() must be True). Unlike CommsTransport "
                "there is no JS-bridge fallback here -- this transport is "
                "for the headless remote-kernel leg only."
            )

        try:
            import comm as _comm_pkg
            _comm_pkg.get_comm_manager().register_target(self._target_name, self._on_comm_open)
            logger.debug(
                f"KernelCommTransport.connect: registered target "
                f"{self._target_name!r} via comm.create_comm"
            )
        except ImportError:
            shell.kernel.comm_manager.register_target(self._target_name, self._on_comm_open)
            logger.debug(
                f"KernelCommTransport.connect: registered target "
                f"{self._target_name!r} via ipykernel.comm.Comm (fallback)"
            )

        await asyncio.wait_for(self._open_event.wait(), timeout=self._open_timeout)
        self._connected = True

    def _on_comm_open(self, kernel_comm, open_msg: Dict[str, Any]) -> None:
        """Called by the kernel's comm_manager when the peer opens our target."""
        kernel_comm.on_msg(self._on_comm_msg)
        kernel_comm.on_close(self._on_comm_close)
        self._comm = kernel_comm
        self._open_event.set()
        logger.debug(f"KernelCommTransport._on_comm_open: comm opened for {self._comm_mgr_id}")

    def _on_comm_msg(self, msg: Dict[str, Any]) -> None:
        envelope = msg.get("content", {}).get("data", {}).get("envelope")
        if envelope is None:
            logger.warning("KernelCommTransport._on_comm_msg: no envelope, dropping")
            return
        try:
            inner = deserialize(envelope)
        except Exception:
            logger.exception("KernelCommTransport._on_comm_msg: deserialize failed")
            return
        if self._callback is not None:
            asyncio.ensure_future(self._callback(inner))

    def _on_comm_close(self, msg: Dict[str, Any]) -> None:
        logger.debug(f"KernelCommTransport._on_comm_close: peer closed {self._comm_mgr_id}")
        self._connected = False

    def set_message_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        self._callback = callback

    async def send_message(self, message: Dict[str, Any]) -> None:
        if self._comm is None:
            raise RuntimeError("KernelCommTransport: no comm open yet")
        self._comm.send({"envelope": serialize(message)})

    async def run(self) -> None:
        while not self._closed:
            await asyncio.sleep(0.1)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._comm is not None:
            try:
                self._comm.close()
            except Exception:
                logger.exception("KernelCommTransport.close: error closing comm")
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and not self._closed
