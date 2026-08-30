"""
Chunk 1b, Task 4 definition-of-done.

Note on what "liveness" means here: once a command is dispatched to a
worker, that worker process is legitimately busy until it replies (it's
one Python process -- GIL-holding C++ work, or here a literal
`time.sleep()`, blocks *everything* that worker does, including
answering an unrelated concurrent request; that's expected and correct,
not a bug to fix). The actual liveness requirement is that the
*supervisor's own* handling of a status check never itself waits on the
worker -- `JobRegistry.status()` is a synchronous dict lookup, so it
stays fast no matter how busy or how long-running the dispatched job is.
That's what these tests prove: `dispatch()` returns immediately, and
`status()` stays instant throughout, while a real subprocess is
genuinely blocked processing the job in the background.

Chunk 1c note (otherwise unchanged): see test_worker_process_transport.py's
module docstring -- `_spawn_supervisor_side` here sends the same opening
`configure` handshake before returning, since these tests also talk to
`WorkerProcessTransport` directly, below `_supervisor.py`'s pool.
"""
import asyncio
import os
import time
from uuid import uuid4

import pytest

from cubevis.bokeh.transport import CommMgr
from cubevis.remote import request
from cubevis.remote._worker_transport import WorkerProcessTransport
from cubevis.remote._async_dispatch import JobRegistry, JobStatus


async def _spawn_supervisor_side():
    mgr = CommMgr(role=CommMgr.ROLE_MIRROR, transport_type='remote_kernel')
    transport = WorkerProcessTransport(str(uuid4()))
    await mgr.initialize(transport=transport)
    run_task = asyncio.ensure_future(transport.run())
    comm = mgr.open("worker")
    # Chunk 1c: opening configuration handshake -- see this file's
    # module docstring.
    await asyncio.wait_for(request(comm, "configure", {}), timeout=15)
    return mgr, comm, transport, run_task


async def _teardown(transport, run_task):
    run_task.cancel()
    try:
        await run_task
    except asyncio.CancelledError:
        pass
    await transport.close()


@pytest.mark.asyncio
async def test_dispatch_returns_immediately_and_status_stays_instant_while_working():
    mgr, comm, transport, run_task = await _spawn_supervisor_side()
    try:
        registry = JobRegistry(is_worker_alive=lambda: transport.is_connected())

        t0 = time.monotonic()
        registry.dispatch(comm, "slow_echo", {"duration": 2.5, "value": "hi"}, job_id="job-1")
        dispatch_elapsed = time.monotonic() - t0
        assert dispatch_elapsed < 0.1, (
            f"dispatch() took {dispatch_elapsed:.3f}s -- it must schedule the "
            "worker call in the background and return immediately, never "
            "await the worker directly"
        )

        # Poll several times while the worker is genuinely still busy
        # (real subprocess, real time.sleep() inside it) -- every poll
        # must stay fast, proving the supervisor's own status-check
        # handling never itself blocks on the worker.
        for _ in range(4):
            t = time.monotonic()
            status = registry.status("job-1")
            poll_elapsed = time.monotonic() - t
            assert poll_elapsed < 0.05, (
                f"status() took {poll_elapsed:.3f}s while the worker was busy -- "
                "a status poll must never block on the worker"
            )
            assert status["status"] == JobStatus.WORKING.value
            await asyncio.sleep(0.3)

        # Now actually wait for it to finish and confirm the transition.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            status = registry.status("job-1")
            if status["status"] == JobStatus.COMPLETED.value:
                break
            await asyncio.sleep(0.1)

        assert status["status"] == JobStatus.COMPLETED.value
        assert status["result"] == {"echo": "hi", "slept": 2.5}
    finally:
        await _teardown(transport, run_task)


@pytest.mark.asyncio
async def test_died_status_when_worker_process_is_killed_mid_job():
    """The other half of Task 4's demonstration requirement: "died",
    end-to-end against a real subprocess -- kill it out from under an
    in-flight job and confirm status() reports DIED rather than hanging
    forever waiting on a reply that will never come."""
    mgr, comm, transport, run_task = await _spawn_supervisor_side()
    try:
        registry = JobRegistry(is_worker_alive=lambda: transport.is_connected())

        registry.dispatch(comm, "slow_echo", {"duration": 30.0}, job_id="job-doomed")
        # Let the worker actually start processing it.
        await asyncio.sleep(0.3)
        assert registry.status("job-doomed")["status"] == JobStatus.WORKING.value

        # Kill the worker process directly -- not close(), which would
        # try to negotiate a clean shutdown; this simulates the process
        # simply dying.
        transport._proc.kill()
        await transport._proc.wait()

        # is_connected() only flips False once run()'s read loop notices
        # EOF on stdout; give that a moment.
        deadline = time.monotonic() + 5.0
        status = None
        while time.monotonic() < deadline:
            status = registry.status("job-doomed")
            if status["status"] == JobStatus.DIED.value:
                break
            await asyncio.sleep(0.1)

        assert status["status"] == JobStatus.DIED.value, (
            f"expected DIED after killing the worker mid-job, got {status}"
        )
    finally:
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        transport._connected = False


@pytest.mark.asyncio
async def test_stuck_is_a_time_since_update_heuristic_not_a_proof():
    """Documents exactly what "stuck" means here: purely elapsed time
    with no cooperation from the worker. A fast-completing job with a
    tiny stuck_after threshold reports STUCK even though it's about to
    finish normally -- proving this is a heuristic on the clock, not a
    real detection of the worker being wedged (which, per the kickoff
    doc, isn't provable without cooperation from whatever's running
    inside the worker)."""
    mgr, comm, transport, run_task = await _spawn_supervisor_side()
    try:
        registry = JobRegistry(stuck_after=0.05, is_worker_alive=lambda: transport.is_connected())
        registry.dispatch(comm, "slow_echo", {"duration": 1.0}, job_id="job-2")

        await asyncio.sleep(0.3)
        status = registry.status("job-2")
        assert status["status"] == JobStatus.STUCK.value, (
            "with a tiny stuck_after, a perfectly healthy still-running job "
            "must be reported STUCK -- this is the heuristic being honest "
            "about what it actually measures (elapsed time), not a real "
            "liveness proof"
        )

        # ...and it still completes normally once the worker actually replies.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            status = registry.status("job-2")
            if status["status"] == JobStatus.COMPLETED.value:
                break
            await asyncio.sleep(0.05)
        assert status["status"] == JobStatus.COMPLETED.value
    finally:
        await _teardown(transport, run_task)
