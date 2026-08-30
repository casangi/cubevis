########################################################################
# P_local-side convenience wiring.
#
# Every caller building the P_local <-> remote-kernel link ends up doing
# the same handful of steps: construct a mirrored CommMgr, construct a
# KernelClientTransport, wire them together, connect, mark running. The
# Chunk 1 tests originally did this by hand at each call site (reaching
# into CommMgr's private `_transport`/`_initialized` attributes directly)
# -- this module exists so that only ONE place in cubevis.remote does
# that, instead of every future caller (Chunk 2, Chunk 3, tests) needing
# to know CommMgr's internals to use this link at all.
#
# It turns out CommMgr already has close to the right method for this:
# `initialize()` already handles "transport was constructed and assigned
# externally, connect it, mark running" for its 'colab'/'jupyter' branch
# -- see _comm_mgr.py. That branch was extended (additively, no change to
# existing behavior) to also accept 'remote_kernel', so this helper can
# use the public `initialize()` entry point rather than duplicating its
# state transitions.
#
# Chunk 1c change (Task 2): `RemoteAppLink.open()` used to bootstrap AND
# spawn a single worker subprocess in one call (Chunk 1b). It no longer
# spawns anything itself -- it connects to the kernel's one Layer-1
# worker (the execution-context *pool*, from _supervisor.py), which is
# bootstrapped exactly as idempotently as Chunk 1b's single worker was,
# and stops there. `RemoteAppLink.create_context(worker_module=...,
# config=...)` is the new entry point for actually spawning a worker
# subprocess -- it wraps the `create_context` wire operation and returns
# an `ExecutionContext`, the light P_local-side handle application code
# interacts with. `RemoteAppLink.close()` tears down *every* context it
# created (via the pool's own `shutdown_context`/`shutdown_all`), not
# just one -- the same "confirm actual subprocess exit" standard Chunk
# 1b held `shutdown_worker` to, now applied per context.
########################################################################
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from uuid import uuid4

from cubevis.bokeh.transport import CommMgr
from ._bridge import request, SyncBridge
from ._kernel_transport import KernelClientTransport
from ._worker import DEFAULT_TARGET_NAME
from ._supervisor import build_worker_pool, DEFAULT_WORKER_MODULE

if TYPE_CHECKING:
    from jupyter_client.manager import AsyncKernelManager

DEFAULT_WORKER_TARGET_NAME = "cubevis-remote-worker"

# How long dispatch_fast()/job_status()/shutdown_context()/list_contexts()/
# supervisor_info() wait for the supervisor's own reply before giving up.
# These are all meant to return promptly (that's dispatch_fast's whole
# contract -- see _async_dispatch.py for dispatch_async's very different,
# unbounded-duration shape), so 30s is a generous ceiling for a hung/
# unreachable supervisor, not a budget anything here is expected to need.
_DEFAULT_CALL_TIMEOUT = 30.0

# create_context() specifically gets its own, much larger default --
# found necessary against a real sshpyk-provisioned cluster kernel
# (this sandbox's local-kernel testing never caught it): create_context
# includes the *supervisor's own* _CONFIGURE_TIMEOUT-bounded wait
# (_supervisor.py) for the freshly spawned worker's opening `configure`
# round trip, all nested inside this client-side wait. A client-side
# default equal to (or smaller than) the server's own internal budget
# is unsafe by construction -- it can legitimately fire while the server
# is still doing real, un-hung work (subprocess spawn + fresh
# interpreter startup, slower over a real network/shared filesystem
# than any local sandbox), misreporting a slow-but-succeeding spawn as a
# failure. Kept comfortably above _supervisor.py's own
# _CONFIGURE_TIMEOUT (90s) plus real round-trip overhead on an
# SSH-tunneled connection, rather than assumed equal to it.
_CREATE_CONTEXT_TIMEOUT = 180.0


async def open_remote_kernel_link(
    kernel_manager: "AsyncKernelManager",
    target_name: str = DEFAULT_TARGET_NAME,
    comm_mgr_id: Optional[str] = None,
    ready_timeout: float = 60.0,
) -> Tuple[CommMgr, KernelClientTransport]:
    """
    Build and connect P_local's kernel-facing link in one call.

    Constructs a `CommMgr(role=CommMgr.ROLE_MIRROR)` and a
    `KernelClientTransport` bound to `kernel_manager`, wires them
    together, and calls `CommMgr.initialize()` (via the `'remote_kernel'`
    transport_type) to connect and mark the manager RUNNING -- the same
    sequence `_build_comm()` already uses for any local app, just against
    a pre-built transport instead of one `initialize()` constructs
    itself.

    Returns `(mgr, transport)`. The caller still owns scheduling
    `transport.run()` -- deliberately not started here, since *how* to
    run it (a bare `asyncio.ensure_future(...)` in an already-async
    context, vs. `SyncBridge.run_background(...)` when the caller has one
    -- see Chunk 2's construction-time case in `_bridge.py`'s docstring)
    is a choice this helper shouldn't make on the caller's behalf::

        mgr, transport = await open_remote_kernel_link(km)
        run_task = asyncio.ensure_future(transport.run())
        # ... later, on teardown:
        run_task.cancel()
        await transport.close()

    `comm_mgr_id`, if given, fixes the mirrored CommMgr's own id (purely
    internal bookkeeping -- routing is keyed on `target_name`, not this)
    rather than letting `CommMgr` generate one; mainly useful for tests
    that want a predictable id to assert against.
    """
    mgr = CommMgr(role=CommMgr.ROLE_MIRROR, **({"comm_mgr_id": comm_mgr_id} if comm_mgr_id else {}))
    transport = KernelClientTransport(
        mgr.comm_mgr_id, kernel_manager, target_name=target_name, ready_timeout=ready_timeout
    )
    mgr.transport_type = "remote_kernel"
    await mgr.initialize(transport=transport)
    return mgr, transport

async def _execute_and_collect(client, code: str, timeout: float = 30.0) -> List[str]:
    """Minimal execute-and-collect-stdout helper, same shape as Chunk
    1's own demo/test scripts use for running a bootstrap cell."""
    client.execute(code)
    lines: List[str] = []
    while True:
        msg = await client.get_iopub_msg(timeout=timeout)
        if msg["msg_type"] == "stream":
            lines.extend(msg["content"]["text"].splitlines())
        elif msg["msg_type"] == "error":
            tb = "\n".join(msg["content"].get("traceback", []))
            raise RuntimeError(f"remote bootstrap cell raised:\n{tb}")
        elif msg["msg_type"] == "status" and msg["content"]["execution_state"] == "idle":
            await client.get_shell_msg(timeout=timeout)
            break
    return lines


_BOOTSTRAP_TEMPLATE = """
from cubevis.remote import ensure_remote_worker
from cubevis.remote._supervisor import build_worker_pool

_comm_mgr_id = ensure_remote_worker(
    build_worker_pool(),
    target_name={target_name!r},
)
print("COMM_MGR_ID=" + _comm_mgr_id)
"""


class ExecutionContext:
    """
    P_local-side handle for one execution context (Chunk 1c, Task 2) --
    the object application code actually interacts with. Holds
    `context_id` plus a reference back to the owning `RemoteAppLink` for
    issuing further calls; carries no transport/process state of its
    own (that lives in the supervisor's `WorkerDelegate`, one layer
    away, per the design doc §2f -- Layer 2 is process isolation on the
    *supervisor* side, not something duplicated here).
    """

    def __init__(self, link: "RemoteAppLink", context_id: str, pid: Optional[int] = None):
        self._link = link
        self.context_id = context_id
        self.pid = pid

    async def dispatch_fast(self, message_id: str, payload: Optional[Dict[str, Any]] = None,
                             timeout: float = _DEFAULT_CALL_TIMEOUT) -> Any:
        return await asyncio.wait_for(
            request(self._link._comm, "dispatch_fast",
                    {"context_id": self.context_id, "message_id": message_id,
                     "payload": payload or {}}),
            timeout=timeout,
        )

    async def dispatch_async(self, message_id: str, payload: Optional[Dict[str, Any]] = None,
                              timeout: float = _DEFAULT_CALL_TIMEOUT) -> str:
        reply = await asyncio.wait_for(
            request(self._link._comm, "dispatch_async",
                    {"context_id": self.context_id, "message_id": message_id,
                     "payload": payload or {}}),
            timeout=timeout,
        )
        return reply["job_id"]

    async def job_status(self, job_id: str, timeout: float = _DEFAULT_CALL_TIMEOUT) -> Dict[str, Any]:
        return await asyncio.wait_for(
            request(self._link._comm, "job_status",
                    {"context_id": self.context_id, "job_id": job_id}),
            timeout=timeout,
        )

    # -- object creation/invocation (Task 4), via dispatch_fast ---------
    async def create_object(self, class_name: str, args: Optional[List[Any]] = None,
                             kwargs: Optional[Dict[str, Any]] = None) -> str:
        reply = await self.dispatch_fast(
            "create_object", {"class_name": class_name, "args": args or [], "kwargs": kwargs or {}}
        )
        return reply["handle"]

    async def call_method(self, handle: str, method: str, args: Optional[List[Any]] = None,
                           kwargs: Optional[Dict[str, Any]] = None) -> Any:
        return await self.dispatch_fast(
            "call_method",
            {"handle": handle, "method": method, "args": args or [], "kwargs": kwargs or {}},
        )

    async def dispose_object(self, handle: str) -> bool:
        reply = await self.dispatch_fast("dispose_object", {"handle": handle})
        return bool(reply.get("disposed"))

    async def eval_code(self, code: str) -> Any:
        return await self.dispatch_fast("eval_code", {"code": code})

    async def exec_code(self, code: str) -> Any:
        return await self.dispatch_fast("exec_code", {"code": code})

    async def worker_info(self) -> Dict[str, Any]:
        return await self.dispatch_fast("worker_info", {})

    async def shutdown(self, timeout: float = 20.0) -> Optional[Dict[str, Any]]:
        return await asyncio.wait_for(
            request(self._link._comm, "shutdown_context", {"context_id": self.context_id}),
            timeout=timeout,
        )


class RemoteAppLink:
    """
    P_local's handle on a supervisor-managed execution-context pool for
    one app instance. Owns ``(mgr, transport, sync_bridge)`` -- `mgr`/
    `transport` are the P_local<->supervisor leg (Chunk 1, unchanged),
    and `sync_bridge` lets synchronous application code drive `request()`
    calls against it.

    Chunk 1c: `open()` no longer spawns a worker subprocess itself --
    see `create_context()`. A link with no contexts created against it
    is a perfectly normal, if useless, state (mirroring the pool always
    existing at Layer 1 regardless of how many Layer-2 contexts have
    been created inside it -- see the design doc §2f).
    """

    def __init__(self, mgr: CommMgr, transport: KernelClientTransport, sync_bridge: SyncBridge):
        self.mgr = mgr
        self.transport = transport
        self.sync_bridge = sync_bridge
        self._comm = mgr.open("worker")
        self._contexts: Dict[str, ExecutionContext] = {}
        self._closed = False

    @classmethod
    async def open(
        cls,
        kernel_manager,
        worker_target_name: str = DEFAULT_WORKER_TARGET_NAME,
        timeout: float = 30.0,
    ) -> "RemoteAppLink":
        setup_client = kernel_manager.client()
        setup_client.start_channels()
        try:
            await setup_client.wait_for_ready(timeout=timeout)
            await _execute_and_collect(
                setup_client,
                _BOOTSTRAP_TEMPLATE.format(target_name=worker_target_name),
                timeout=timeout,
            )
        finally:
            setup_client.stop_channels()

        mgr, transport = await open_remote_kernel_link(
            kernel_manager, target_name=worker_target_name, ready_timeout=timeout
        )

        # transport.run() MUST run on the same (ambient) loop mgr/transport
        # were constructed on -- mgr's internal asyncio.Lock and Futures are
        # bound to that loop, and driving them from a different loop/thread
        # (e.g. a SyncBridge's own dedicated loop) is a real cross-loop
        # hazard, not just a style choice: it deadlocks silently rather
        # than raising, since asyncio.Lock isn't thread-safe across loops.
        # Found this the hard way during Chunk 1b -- see the implementation
        # doc's writeup for the repro.
        asyncio.ensure_future(transport.run())

        # `sync_bridge` is kept on the link per the original kickoff doc's
        # suggested shape, for a *future* caller with no ambient loop at
        # all (e.g. Chunk 2/3's next(gclean)) to drive request() calls
        # against this same mgr. That's NOT yet wired up here (unchanged
        # from Chunk 1b): doing it safely requires either routing such
        # calls through asyncio.run_coroutine_threadsafe against *this*
        # ambient loop (not the bridge's own loop), or constructing this
        # entire link from inside bridge.run() in the first place so
        # everything shares one loop from the start. Left unresolved
        # rather than shipping a version that looks like it works and
        # doesn't -- still flagged for whichever chunk needs a
        # synchronous call site against a RemoteAppLink-backed mgr first.
        bridge = SyncBridge(name=f"remote-app-link-{mgr.comm_mgr_id}")
        bridge.start()

        return cls(mgr, transport, bridge)

    async def create_context(self, worker_module: str = DEFAULT_WORKER_MODULE,
                              config: Optional[Dict[str, Any]] = None,
                              timeout: float = _CREATE_CONTEXT_TIMEOUT) -> ExecutionContext:
        """Spawns a fresh execution-context worker subprocess under this
        link's supervisor kernel and returns a handle to it. `config`,
        if given, is handed unopened to the worker as its `configure`
        payload (see worker_main.py); the most common shape is
        `{"register_function": "some.module:register"}`.

        `timeout` defaults to `_CREATE_CONTEXT_TIMEOUT` (see module
        docstring for why this needs its own, larger default than the
        other operations here) -- pass a larger value explicitly for a
        `register_function` that imports something even heavier than
        this default already assumes, or for a cluster host known to be
        slow to spawn on. A `TimeoutError` here does NOT necessarily mean
        the spawn failed -- the worker subprocess may simply still be
        starting up on the remote host when this gives up. Any actual
        failure (a bad `register_function` import, the worker module
        itself failing to start) is logged by the *supervisor kernel
        process*, not this caller -- for a real sshpyk-provisioned
        kernel, that means the remote kernel's own log, which this
        process cannot see; there is no local stderr to check the way
        there is for this sandbox's local-kernel tests, since the
        supervisor kernel is a separate process on a separate host.
        """
        reply = await asyncio.wait_for(
            request(self._comm, "create_context",
                    {"worker_module": worker_module, "config": config}),
            timeout=timeout,
        )
        ctx = ExecutionContext(self, reply["context_id"], pid=reply.get("pid"))
        self._contexts[ctx.context_id] = ctx
        return ctx

    async def list_contexts(self, timeout: float = _DEFAULT_CALL_TIMEOUT) -> List[Dict[str, Any]]:
        reply = await asyncio.wait_for(
            request(self._comm, "list_contexts", {}), timeout=timeout
        )
        return reply["contexts"]

    async def supervisor_info(self, timeout: float = _DEFAULT_CALL_TIMEOUT) -> Dict[str, Any]:
        return await asyncio.wait_for(
            request(self._comm, "supervisor_info", {}), timeout=timeout
        )

    async def close(self, timeout: float = 20.0) -> Dict[str, Any]:
        """Tears down *every* execution context this link created --
        Chunk 1b's single-worker `close()` standard (confirm actual
        subprocess exit, not just that P_local's references were
        dropped), now applied per context rather than once."""
        if self._closed:
            return {}

        results: Dict[str, Any] = {}
        for context_id, ctx in list(self._contexts.items()):
            try:
                results[context_id] = await ctx.shutdown(timeout=timeout)
            except Exception as e:
                # Supervisor unreachable (kernel gone, connection lost) --
                # nothing more we can confirm from P_local's side for
                # this context; still proceed to tear down the others
                # and this link's own transport.
                results[context_id] = {"closed": False, "error": str(e)}
        self._contexts.clear()

        await self.transport.close()
        self.sync_bridge.stop()
        self._closed = True
        return results
