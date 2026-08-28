"""
Chunk 1b, Task 6 definition-of-done: `BokehAppContext` integration
tested without requiring a full Bokeh GUI/browser (construct
`BokehAppContext` instances directly) -- confirms `remote_link` is
per-instance, not shared globally, and that the teardown cascade
actually runs.
"""
import asyncio
import os

import pytest

pytest.importorskip("jupyter_client")
pytest.importorskip("ipykernel")

from jupyter_client import AsyncKernelManager

from cubevis.bokeh.models import BokehAppContext
from cubevis.remote import RemoteAppLink
from cubevis.remote._bokeh_integration import (
    new_comm_mgr_with_remote_teardown,
    attach_remote_link,
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.asyncio
async def test_remote_link_is_per_instance_with_two_concurrent_worker_subprocesses():
    """No GUI required -- constructs two BokehAppContext instances
    directly, each with its own RemoteAppLink talking to its own
    supervisor kernel, and confirms two independent worker subprocesses
    are genuinely running at once (not structurally separate attributes
    that happen to share state)."""
    src_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

    km_a = AsyncKernelManager(kernel_name="python3")
    km_b = AsyncKernelManager(kernel_name="python3")
    await km_a.start_kernel(env={**os.environ, "PYTHONPATH": src_path})
    await km_b.start_kernel(env={**os.environ, "PYTHONPATH": src_path})
    try:
        mgr_a, holder_a = new_comm_mgr_with_remote_teardown()
        mgr_b, holder_b = new_comm_mgr_with_remote_teardown()
        ctx_a = BokehAppContext(comm_mgr=mgr_a)
        ctx_b = BokehAppContext(comm_mgr=mgr_b)

        assert ctx_a.app_id != ctx_b.app_id
        assert ctx_a.remote_link is None and ctx_b.remote_link is None

        link_a = await RemoteAppLink.open(km_a, worker_target_name="app-a")
        link_b = await RemoteAppLink.open(km_b, worker_target_name="app-b")
        attach_remote_link(ctx_a, link_a, holder_a)
        attach_remote_link(ctx_b, link_b, holder_b)

        # Per-instance: setting B's link must not affect A's.
        assert ctx_a.remote_link is link_a
        assert ctx_b.remote_link is link_b
        assert ctx_a.remote_link is not ctx_b.remote_link

        from cubevis.remote import request

        comm_a = ctx_a.remote_link.mgr.open("worker")
        comm_b = ctx_b.remote_link.mgr.open("worker")
        reply_a = await asyncio.wait_for(
            request(comm_a, "dispatch_fast", {"message_id": "ping", "payload": {}}),
            timeout=15,
        )
        reply_b = await asyncio.wait_for(
            request(comm_b, "dispatch_fast", {"message_id": "ping", "payload": {}}),
            timeout=15,
        )
        pid_a, pid_b = reply_a["pid"], reply_b["pid"]
        assert pid_a != pid_b, "the two apps' worker subprocesses must be genuinely distinct"
        assert _pid_alive(pid_a) and _pid_alive(pid_b), (
            "both worker subprocesses should be alive concurrently"
        )
    finally:
        if ctx_a.remote_link is not None:
            await ctx_a.remote_link.close()
        if ctx_b.remote_link is not None:
            await ctx_b.remote_link.close()
        await km_a.shutdown_kernel()
        await km_b.shutdown_kernel()


@pytest.mark.asyncio
async def test_comm_mgr_shutdown_cascades_into_remote_link_close():
    """The teardown-hook finding: CommMgr.shutdown() -- not
    BokehAppContext.show()'s clear_app_context() call, which is
    unrelated -- is what must cascade into remote_link.close(). Proves
    it end to end: calling shutdown() on the app's own (browser-facing,
    never actually connected to a browser here) comm_mgr results in the
    worker subprocess actually exiting, with no GUI involved at all."""
    src_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    km = AsyncKernelManager(kernel_name="python3")
    await km.start_kernel(env={**os.environ, "PYTHONPATH": src_path})
    try:
        shutdown_calls = []

        def _on_shutdown(reason=None, description=""):
            shutdown_calls.append((reason, description))

        mgr, holder = new_comm_mgr_with_remote_teardown(on_shutdown=_on_shutdown)
        ctx = BokehAppContext(comm_mgr=mgr)

        link = await RemoteAppLink.open(km, worker_target_name="shutdown-cascade-test")
        attach_remote_link(ctx, link, holder)

        from cubevis.remote import request
        comm = ctx.remote_link.mgr.open("worker")
        reply = await asyncio.wait_for(
            request(comm, "dispatch_fast", {"message_id": "ping", "payload": {}}),
            timeout=15,
        )
        worker_pid = reply["pid"]
        assert _pid_alive(worker_pid)

        # This is the hook under test: BokehAppContext itself is never
        # touched again -- only the browser-facing comm_mgr's shutdown(),
        # exactly like a tab-close/reconnect-timeout would trigger for
        # real, without a browser ever being involved.
        await ctx.comm_mgr.shutdown()

        assert len(shutdown_calls) == 1, "the user's own on_shutdown must still fire exactly once"

        # The cascade schedules remote_link.close() as a background task
        # rather than awaiting it inline (on_shutdown is a sync callback;
        # see the module docstring for why) -- give it a moment to
        # actually finish confirming the subprocess exit.
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline and _pid_alive(worker_pid):
            await asyncio.sleep(0.1)

        assert not _pid_alive(worker_pid), (
            f"worker subprocess (pid={worker_pid}) is still alive after "
            f"comm_mgr.shutdown() -- the on_shutdown -> remote_link.close() "
            f"cascade did not actually run"
        )
    finally:
        await km.shutdown_kernel()
