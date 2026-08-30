"""
Chunk 1b, Task 5 support -- restructured by Chunk 1c, Task 1 around an
execution-context pool.

Reuses Chunk 1's `ensure_remote_worker()` idempotent start-vs-reattach
machinery for this hop too, one level deeper, rather than inventing a
second bootstrap mechanism: `build_worker_pool()` returns a plain
`build_worker` callable, so the exact same
`ensure_remote_worker(build_worker, target_name=...)` call Chunk 1
already uses now also constructs the pool the first time it runs, and
does nothing on a later reattach (the marker Chunk 1 already maintains
prevents `build_worker` from running twice) -- the pool object itself,
and every execution context inside it, simply keeps living across a
`P_local` reconnect, exactly as Chunk 1b's single worker already did.

Chunk 1c change: Chunk 1b's `build_worker_process_delegate()`/
`_spawn_and_wire_worker()` built exactly one `WorkerProcessTransport`
-backed subprocess per bootstrap. `ExecutionContextPool` replaces that
with a `Dict[execution_context_id, WorkerDelegate]`, owned by the
(still singular) Layer-1 `mgr`, and registers new P_local-facing
handlers on it:

  - `create_context(worker_module=..., config=...) -> {"context_id": ...,
    "pid": ...}` -- spawns a fresh `WorkerProcessTransport`, generates a
    UUID `context_id`, adds a `WorkerDelegate` to the pool. `config` is
    not interpreted here at all (per the kickoff doc) -- it is handed
    unopened to the spawned worker as its opening `configure` message
    (see worker_main.py), which is always sent (with an empty dict if no
    `config` was given) so the worker's own `configure` handler can fall
    back to its Chunk-1b-compatible toy-handler default.
  - `dispatch_fast`/`dispatch_async`/`job_status`/`shutdown_context` --
    Chunk 1b's existing four operations, each now taking a `context_id`
    and routing to the matching pool entry instead of a single fixed
    worker. Each pool entry has its own `JobRegistry` (not shared).
  - `list_contexts()` -- for introspection/debugging; not required for
    any reconnection scenario in scope, but cheap given the pool already
    exists, and useful for the demo/tests.
  - `supervisor_info()` -- cheap, generic introspection (this supervisor
    kernel process's own pid/hostname), so a caller/demo can show
    "process, kernel, host" information for all three tiers (P_local,
    supervisor kernel, worker) from one place -- not a task the kickoff
    doc names explicitly, but the same "cheap given X already exists"
    reasoning as `list_contexts()`.

`ensure_remote_worker()` itself is otherwise untouched -- this module
only changes what `build_worker` constructs, not Chunk 1's bootstrap
mechanism.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any, Dict, Optional
from uuid import uuid4

from cubevis.bokeh.transport import CommMgr
from ._worker_transport import WorkerProcessTransport
from ._async_dispatch import JobRegistry
from ._bridge import request

logger = logging.getLogger(__name__)

DEFAULT_WORKER_MODULE = "cubevis.remote.worker_main"

# Timeout for the opening `configure` round trip create_context() makes
# to a freshly spawned worker before returning -- generous, since a real
# `register_function` may need to import a heavy backend module (an
# MSv2/MSv4 reader stack, say), not just register a couple of toy
# handlers, and because on a real sshpyk-provisioned remote host, even
# spawning the bare subprocess and starting a fresh Python interpreter
# can cost real time (network filesystem home directories, cold import
# caches) that this sandbox's local-kernel testing never exercised.
#
# MUST stay comfortably below whatever timeout the P_local-side caller
# uses for its own create_context() wait (see _link.py's
# _CREATE_CONTEXT_TIMEOUT) -- create_context()'s reply is only sent
# after this configure round trip completes, so if the two budgets were
# equal (an actual bug found against a real cluster kernel, fixed here),
# the client's own wait_for could legitimately expire at the same
# instant the server is still doing real, un-hung work, misreporting a
# slow-but-succeeding spawn as a failure.
_CONFIGURE_TIMEOUT = 90.0


class WorkerDelegate:
    """Everything the supervisor side needs to keep reachable for one
    execution context's worker subprocess: one pool entry."""

    def __init__(self, worker_mgr: CommMgr, comm, transport: WorkerProcessTransport,
                 registry: JobRegistry, worker_module: str, config: Optional[Dict[str, Any]]):
        self.worker_mgr = worker_mgr
        self.comm = comm
        self.transport = transport
        self.registry = registry
        self.worker_module = worker_module
        self.config = config


class UnknownContextError(KeyError):
    """Raised (and turned into an `{'error': ...}` wire reply by
    `CommMgr._handle_request`, same as any other handler exception) when
    a `context_id` doesn't name a currently-live pool entry."""


class ExecutionContextPool:
    """
    The object `build_worker_pool()`'s `build_worker(mgr)` constructs and
    returns -- opaque to `ensure_remote_worker()`, kept alive across
    reattaches by the same namespace-marker mechanism Chunk 1 already
    has. Owns every execution context spawned under this one supervisor
    kernel and registers the pool's P_local-facing wire handlers on
    `mgr`.
    """

    def __init__(self, mgr: CommMgr):
        self._mgr = mgr
        self._contexts: Dict[str, WorkerDelegate] = {}
        self._comm = mgr.open("worker")
        self._register_handlers()

    def _register_handlers(self) -> None:
        c = self._comm
        c.register("create_context", self._handle_create_context)
        c.register("dispatch_fast", self._handle_dispatch_fast)
        c.register("dispatch_async", self._handle_dispatch_async)
        c.register("job_status", self._handle_job_status)
        c.register("shutdown_context", self._handle_shutdown_context)
        c.register("list_contexts", self._handle_list_contexts)
        c.register("supervisor_info", self._handle_supervisor_info)

    def _get(self, context_id: str) -> WorkerDelegate:
        try:
            return self._contexts[context_id]
        except KeyError:
            raise UnknownContextError(f"unknown execution_context_id: {context_id!r}") from None

    def _handle_supervisor_info(self, msg):
        return {"pid": os.getpid(), "hostname": socket.gethostname()}

    async def _handle_create_context(self, msg):
        worker_module = msg.get("worker_module") or DEFAULT_WORKER_MODULE
        config = msg.get("config")

        worker_mgr = CommMgr(role=CommMgr.ROLE_MIRROR, transport_type="remote_kernel")
        transport = WorkerProcessTransport(str(uuid4()), worker_module=worker_module)
        await worker_mgr.initialize(transport=transport)
        asyncio.ensure_future(transport.run())

        comm = worker_mgr.open("worker")
        registry = JobRegistry(is_worker_alive=transport.is_connected)

        # The opening configuration message (Task 3): always sent, even
        # when the caller passed no `config`, so the worker's own
        # `configure` handler can fall back to its Chunk-1b-compatible
        # toy-handler default. Awaiting the reply (rather than firing
        # this and returning immediately) is what guarantees ordering --
        # this is genuinely the *first* message the worker processes,
        # not merely the first one intended to be.
        configure_reply = await asyncio.wait_for(
            request(comm, "configure", config or {}), timeout=_CONFIGURE_TIMEOUT
        )

        context_id = str(uuid4())
        self._contexts[context_id] = WorkerDelegate(
            worker_mgr, comm, transport, registry, worker_module, config
        )
        logger.debug(
            f"ExecutionContextPool: created context_id={context_id} "
            f"pid={transport.pid} worker_module={worker_module!r} "
            f"register_function={configure_reply.get('register_function')!r}"
        )
        return {"context_id": context_id, "pid": transport.pid}

    async def _handle_dispatch_fast(self, msg):
        """Short commands: direct await, result returned inline. Only
        appropriate for calls that return promptly -- see
        _async_dispatch.py's module docstring for why a long command
        here would stall this CommMgr's own receive loop."""
        delegate = self._get(msg["context_id"])
        return await request(delegate.comm, msg["message_id"], msg.get("payload", {}))

    async def _handle_dispatch_async(self, msg):
        delegate = self._get(msg["context_id"])
        job_id = str(uuid4())
        delegate.registry.dispatch(delegate.comm, msg["message_id"], msg.get("payload", {}), job_id)
        return {"job_id": job_id}

    def _handle_job_status(self, msg):
        delegate = self._get(msg["context_id"])
        return delegate.registry.status(msg["job_id"])

    async def _handle_shutdown_context(self, msg):
        context_id = msg["context_id"]
        delegate = self._contexts.pop(context_id, None)
        if delegate is None:
            return {"closed": False, "error": f"unknown execution_context_id: {context_id!r}"}
        await delegate.transport.close()
        return {"closed": True, "returncode": delegate.transport.returncode,
                "context_id": context_id}

    def _handle_list_contexts(self, msg):
        return {"contexts": [
            {
                "context_id": context_id,
                "pid": delegate.transport.pid,
                "alive": delegate.transport.is_connected(),
                "worker_module": delegate.worker_module,
            }
            for context_id, delegate in self._contexts.items()
        ]}

    async def shutdown_all(self) -> Dict[str, Dict[str, Any]]:
        """Tears down every still-live context, confirming each
        subprocess's actual exit -- used by `RemoteAppLink.close()`
        (Task 2) so a link's teardown doesn't leave any context's worker
        subprocess dangling."""
        results: Dict[str, Dict[str, Any]] = {}
        for context_id in list(self._contexts):
            delegate = self._contexts.pop(context_id)
            await delegate.transport.close()
            results[context_id] = {
                "closed": True,
                "returncode": delegate.transport.returncode,
            }
        return results


def build_worker_pool():
    """Returns a `build_worker` callable suitable for
    `ensure_remote_worker(build_worker, target_name=...)`. Replaces
    Chunk 1b's `build_worker_process_delegate()` -- the pool itself is
    what gets kept alive as "the worker" under `ensure_remote_worker`'s
    namespace marker, so a reattaching `P_local` finds the same pool
    (and every execution context still in it), not a fresh empty one."""

    def build_worker(mgr: CommMgr):
        return ExecutionContextPool(mgr)

    return build_worker
