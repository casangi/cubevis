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
########################################################################
from __future__ import annotations

import asyncio
from queue import Empty
from typing import Any, Dict, List, Optional, Tuple

from cubevis.bokeh.transport import CommMgr
from ._bridge import request, SyncBridge
from ._kernel_transport import KernelClientTransport, DEFAULT_TARGET_NAME
from ._supervisor import build_worker_process_delegate, DEFAULT_WORKER_MODULE

DEFAULT_WORKER_TARGET_NAME = "cubevis-remote-worker"


async def open_remote_kernel_link(
    kernel_manager,
    target_name: str = DEFAULT_TARGET_NAME,
    timeout: float = 30.0,
) -> Tuple[CommMgr, KernelClientTransport]:
    """
    P_local's side, one call: construct a `ROLE_MIRROR` `CommMgr`, wire a
    `KernelClientTransport` to it, and connect -- discovering whichever
    worker's `comm_open` shows up first on ``target_name`` (the kernel
    is assumed to have already been bootstrapped via
    `ensure_remote_worker()` before this is called; see Chunk 1's
    two-step demo script for the ordering this depends on).
    """
    mgr = CommMgr(role=CommMgr.ROLE_MIRROR, transport_type='remote_kernel')
    transport = KernelClientTransport(None, kernel_manager, target_name=target_name,
                                       timeout=timeout)
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
from cubevis.remote._supervisor import build_worker_process_delegate

_comm_mgr_id = ensure_remote_worker(
    build_worker_process_delegate({worker_module!r}),
    target_name={target_name!r},
)
print("COMM_MGR_ID=" + _comm_mgr_id)
"""


class RemoteAppLink:
    """
    P_local's handle on a supervisor-managed compute worker for one app
    instance. Owns ``(mgr, transport, sync_bridge, worker_target_name)``
    as one unit -- `mgr`/`transport` are the P_local<->supervisor leg
    (Chunk 1, unchanged), `sync_bridge` lets synchronous application code
    drive `request()` calls against it, and `worker_target_name` is the
    comm target this link's worker-delegate was bootstrapped under.
    """

    def __init__(self, mgr: CommMgr, transport: KernelClientTransport,
                 sync_bridge: SyncBridge, worker_target_name: str):
        self.mgr = mgr
        self.transport = transport
        self.sync_bridge = sync_bridge
        self.worker_target_name = worker_target_name
        self._comm = mgr.open("worker")
        self._closed = False

    @classmethod
    async def open(
        cls,
        kernel_manager,
        worker_target_name: str = DEFAULT_WORKER_TARGET_NAME,
        worker_module: str = DEFAULT_WORKER_MODULE,
        timeout: float = 30.0,
    ) -> "RemoteAppLink":
        setup_client = kernel_manager.client()
        setup_client.start_channels()
        try:
            await setup_client.wait_for_ready(timeout=timeout)
            await _execute_and_collect(
                setup_client,
                _BOOTSTRAP_TEMPLATE.format(worker_module=worker_module,
                                            target_name=worker_target_name),
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
        # Found this the hard way -- see the writeup accompanying this
        # chunk for the repro.
        asyncio.ensure_future(transport.run())

        # `sync_bridge` is kept on the link per the kickoff doc's suggested
        # shape, for a *future* caller with no ambient loop at all (e.g.
        # Chunk 2/3's next(gclean)) to drive request() calls against this
        # same mgr. That's NOT yet wired up here: doing it safely requires
        # either routing such calls through asyncio.run_coroutine_threadsafe
        # against *this* ambient loop (not the bridge's own loop), or
        # constructing this entire link from inside bridge.run() in the
        # first place so everything shares one loop from the start. Left
        # unresolved rather than shipping a version that looks like it
        # works and doesn't -- flagging this explicitly for the next chunk
        # rather than glossing over it.
        bridge = SyncBridge(name=f"remote-app-link-{mgr.comm_mgr_id}")
        bridge.start()

        return cls(mgr, transport, bridge, worker_target_name)

    async def close(self, timeout: float = 20.0) -> Optional[Dict[str, Any]]:
        """Confirms the worker subprocess actually exits (via the
        supervisor's own `shutdown_worker` handler, which awaits
        `WorkerProcessTransport.close()` before replying) -- not just
        that P_local's references to it are dropped."""
        if self._closed:
            return None

        result: Optional[Dict[str, Any]] = None
        try:
            result = await asyncio.wait_for(
                request(self._comm, "shutdown_worker", {}), timeout=timeout
            )
        except Exception:
            # Supervisor unreachable (kernel gone, connection lost) --
            # nothing more we can confirm from P_local's side.
            pass

        await self.transport.close()
        self.sync_bridge.stop()
        self._closed = True
        return result
