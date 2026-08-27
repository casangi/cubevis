"""
Chunk 1, Task 4 + definition-of-done bullet 3.

`request()` and `SyncBridge` both need at least one test exercising the
"no running loop" call path -- that's the path most likely to have
subtle bugs (thread lifecycle, shutdown ordering) and the one the
mirrored-role CommMgr tests (which already run inside an event loop)
don't naturally cover.
"""
import asyncio
import threading

import pytest

from cubevis.bokeh.transport import CommMgr
from cubevis.remote import request, SyncBridge
from cubevis.remote.testing import wire_loopback_pair


# ----------------------------------------------------------------------
# request(): running-loop call path
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_request_running_loop_success():
    side_a = CommMgr(role=CommMgr.ROLE_MIRROR)   # P_local's kernel-facing side
    side_b = CommMgr(role=CommMgr.ROLE_DEFAULT)  # kernel-side
    wire_loopback_pair(side_a, side_b)

    comm_a = side_a.open("query")
    comm_b = side_b.open("query")
    comm_b.register("axis_info", lambda msg: {"axis": "frequency", "unit": "Hz"})

    result = await request(comm_a, "axis_info", {})
    assert result == {"axis": "frequency", "unit": "Hz"}


@pytest.mark.asyncio
async def test_request_running_loop_timeout():
    side_a = CommMgr(role=CommMgr.ROLE_MIRROR)
    side_b = CommMgr(role=CommMgr.ROLE_DEFAULT)
    wire_loopback_pair(side_a, side_b)

    comm_a = side_a.open("query")
    comm_b = side_b.open("query")
    # No handler registered on side_b -- side_b will reply with an error
    # dict (its own "no handler" convention), not silence. To exercise a
    # genuine timeout we need the reply to never come at all: close the
    # transport out from under the request after it's sent so no reply
    # can arrive.
    comm_b.register("slow_op", lambda msg: asyncio.sleep(999))  # never resolves in test time

    with pytest.raises(asyncio.TimeoutError):
        await request(comm_a, "slow_op", {}, timeout=0.2)


# ----------------------------------------------------------------------
# SyncBridge: the "no running loop" call path
# ----------------------------------------------------------------------
def test_sync_bridge_runs_coroutine_from_a_thread_with_no_loop():
    """
    Mirrors the exact shape `next(gclean)` / metadata()/axis_info() need:
    called from plain synchronous code (no `asyncio.get_running_loop()`
    available), yet still needs to `await` something.
    """
    # Sanity check: this thread genuinely has no running loop, matching
    # the construction-time call sites this primitive exists for.
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()

    bridge = SyncBridge(name="test-bridge")
    bridge.start()
    try:
        async def _compute():
            await asyncio.sleep(0.01)
            return 41 + 1

        result = bridge.run(_compute())
        assert result == 42
    finally:
        bridge.stop()

    assert not bridge.is_running


def test_sync_bridge_request_against_mirrored_comm_mgrs_no_loop():
    """
    The realistic version of the above: a synchronous caller (no running
    loop) using SyncBridge.run(request(...)) against two mirrored-role
    CommMgrs wired over the loopback double -- the same shape Chunk 2's
    RemoteReductionContext.metadata()/axis_info() will use against a real
    KernelClientTransport.
    """
    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()

    bridge = SyncBridge(name="test-bridge-2")
    bridge.start()
    try:
        async def _setup():
            side_a = CommMgr(role=CommMgr.ROLE_MIRROR)
            side_b = CommMgr(role=CommMgr.ROLE_DEFAULT)
            wire_loopback_pair(side_a, side_b)
            comm_a = side_a.open("meta")
            comm_b = side_b.open("meta")
            comm_b.register("metadata", lambda msg: {"n_baselines": 12})
            return comm_a

        comm_a = bridge.run(_setup())

        # This call is made from the *test's* thread, which has no loop --
        # exactly the next(gclean)-shaped case. request()'s own
        # `asyncio.get_running_loop()` call must resolve against the
        # bridge's loop, not this thread, which is why it has to be
        # wrapped in bridge.run(...) rather than called bare.
        result = bridge.run(request(comm_a, "metadata", {}))
        assert result == {"n_baselines": 12}
    finally:
        bridge.stop()


def test_sync_bridge_start_stop_idempotent_and_reusable():
    bridge = SyncBridge(name="test-bridge-3")
    bridge.start()
    bridge.start()  # idempotent, should not raise or spawn a second thread
    first_loop = bridge.loop
    bridge.run(asyncio.sleep(0))
    bridge.stop()
    bridge.stop()  # idempotent
    assert bridge.loop is None
    assert not bridge.is_running

    # Restartable after a clean stop.
    bridge.start()
    try:
        assert bridge.loop is not None
        assert bridge.loop is not first_loop
        assert bridge.run(asyncio.sleep(0, result="ok")) == "ok"
    finally:
        bridge.stop()


def test_sync_bridge_run_background_keeps_transport_alive_for_run():
    """
    Exercises `run_background()`, which is how a transport's `run()`
    read-loop and construction-time `request()`/`SyncBridge.run()` calls
    end up sharing one loop instead of racing two independent ones (see
    the SyncBridge docstring).
    """
    bridge = SyncBridge(name="test-bridge-4")
    bridge.start()
    try:
        events = []

        async def _background_loop():
            while True:
                events.append("tick")
                await asyncio.sleep(0.05)

        bridge.run_background(_background_loop())

        async def _wait_a_bit():
            await asyncio.sleep(0.18)
            return len(events)

        n = bridge.run(_wait_a_bit())
        assert n >= 2, "background task should have ticked at least twice by now"
    finally:
        bridge.stop()
