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

from typing import TYPE_CHECKING, Optional, Tuple

from cubevis.bokeh.transport import CommMgr

from ._kernel_transport import KernelClientTransport
from ._worker import DEFAULT_TARGET_NAME

if TYPE_CHECKING:
    from jupyter_client.manager import AsyncKernelManager

__all__ = ["open_remote_kernel_link"]


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
