"""
Chunk 1b, Task 5 support.

Reuses Chunk 1's `ensure_remote_worker()` idempotent start-vs-reattach
machinery for this hop too, one level deeper, rather than inventing a
second bootstrap mechanism: `build_worker_process_delegate()` returns a
plain `build_worker` callable, so the exact same
`ensure_remote_worker(build_worker, target_name=...)` call Chunk 1
already uses now also spawns and wires a real OS worker subprocess the
first time it runs, and does nothing on a later reattach (the marker
Chunk 1 already maintains prevents `build_worker` from running twice).

The P_local-facing handlers registered here are the "no new mechanism
needed" dispatch pattern: `dispatch_fast` awaits the worker directly for
short commands, `dispatch_async` schedules a background job (Task 4) and
returns a `job_id` immediately, `job_status` is a fast synchronous poll,
and `shutdown_worker` tears the subprocess down and confirms it actually
exited before replying.
"""
from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from cubevis.bokeh.transport import CommMgr
from ._worker_transport import WorkerProcessTransport
from ._async_dispatch import JobRegistry
from ._bridge import request

logger = logging.getLogger(__name__)

DEFAULT_WORKER_MODULE = "cubevis.remote.worker_main"


class WorkerDelegate:
    """Everything the supervisor side needs to keep reachable for a
    single delegated worker subprocess. Attached to the P_local-facing
    `CommMgr` as `mgr._remote_worker_delegate` so a later lookup (or a
    reattach) can find it without re-deriving anything."""

    def __init__(self, worker_mgr: CommMgr, transport: WorkerProcessTransport,
                 registry: JobRegistry):
        self.worker_mgr = worker_mgr
        self.transport = transport
        self.registry = registry


async def _spawn_and_wire_worker(mgr: CommMgr, worker_module: str) -> WorkerDelegate:
    worker_mgr = CommMgr(role=CommMgr.ROLE_MIRROR, transport_type='remote_kernel')
    transport = WorkerProcessTransport(str(uuid4()), worker_module=worker_module)
    await worker_mgr.initialize(transport=transport)
    asyncio.ensure_future(transport.run())

    worker_comm = worker_mgr.open("worker")
    registry = JobRegistry(is_worker_alive=transport.is_connected)
    delegate = WorkerDelegate(worker_mgr, transport, registry)

    proxy_comm = mgr.open("worker")

    async def handle_dispatch_fast(msg):
        """Short commands: direct await, result returned inline. Only
        appropriate for calls that return promptly -- see the module
        docstring in _async_dispatch.py for why a long command here
        would stall this CommMgr's own receive loop."""
        return await request(worker_comm, msg["message_id"], msg.get("payload", {}))

    async def handle_dispatch_async(msg):
        job_id = str(uuid4())
        registry.dispatch(worker_comm, msg["message_id"], msg.get("payload", {}), job_id)
        return {"job_id": job_id}

    def handle_job_status(msg):
        return registry.status(msg["job_id"])

    async def handle_shutdown_worker(msg):
        await transport.close()
        return {"closed": True, "returncode": transport.returncode}

    proxy_comm.register("dispatch_fast", handle_dispatch_fast)
    proxy_comm.register("dispatch_async", handle_dispatch_async)
    proxy_comm.register("job_status", handle_job_status)
    proxy_comm.register("shutdown_worker", handle_shutdown_worker)

    mgr._remote_worker_delegate = delegate
    return delegate


def build_worker_process_delegate(worker_module: str = DEFAULT_WORKER_MODULE):
    """Returns a `build_worker` callable suitable for
    `ensure_remote_worker(build_worker, target_name=...)`."""

    def build_worker(mgr: CommMgr):
        # Spawning is inherently async; ensure_remote_worker calls
        # build_worker synchronously (same reasoning as its own
        # KernelCommTransport wiring in _worker.py), so schedule it
        # rather than await it here.
        asyncio.ensure_future(_spawn_and_wire_worker(mgr, worker_module))
        return mgr

    return build_worker
