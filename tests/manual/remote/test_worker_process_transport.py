"""
Chunk 1b, Task 1 definition-of-done: `WorkerProcessTransport`/
`WorkerCommTransport` complete a request/response round trip AND a push
round trip against a REAL subprocess (not a loopback double, not
mocked) -- matching Chunk 1's own bar for `KernelClientTransport`.

Chunk 1b, Task 3 definition-of-done: a deliberately-crashing toy
worker's failure is diagnosable from the supervisor's log output in a
test, not just "it died."
"""
import asyncio
import logging
from uuid import uuid4

import pytest

from cubevis.bokeh.transport import CommMgr
from cubevis.remote import request
from cubevis.remote._worker_transport import WorkerProcessTransport


async def _spawn_supervisor_side():
    """Shared setup: a ROLE_MIRROR CommMgr wired to a real worker
    subprocess via WorkerProcessTransport, run() pumping in the
    background. Returns (mgr, comm, transport, run_task) -- caller is
    responsible for tearing down."""
    mgr = CommMgr(role=CommMgr.ROLE_MIRROR, transport_type='remote_kernel')
    transport = WorkerProcessTransport(str(uuid4()))
    await mgr.initialize(transport=transport)
    run_task = asyncio.ensure_future(transport.run())
    comm = mgr.open("worker")
    return mgr, comm, transport, run_task


async def _teardown(transport, run_task):
    run_task.cancel()
    try:
        await run_task
    except asyncio.CancelledError:
        pass
    await transport.close()


@pytest.mark.asyncio
async def test_request_response_round_trip_against_real_subprocess():
    mgr, comm, transport, run_task = await _spawn_supervisor_side()
    try:
        reply = await asyncio.wait_for(request(comm, "ping", {}), timeout=15)
        assert reply["pong"] is True
        # pid must differ from this test's own pid -- proves the command
        # actually executed in the subprocess, not in-process.
        import os
        assert reply["pid"] != os.getpid()

        reply = await asyncio.wait_for(request(comm, "add", {"a": 19, "b": 23}), timeout=15)
        assert reply == {"sum": 42}
    finally:
        await _teardown(transport, run_task)

    assert transport.returncode == 0, (
        "worker should exit cleanly (returncode 0) once stdin is closed"
    )


@pytest.mark.asyncio
async def test_push_round_trip_against_real_subprocess():
    """The *worker* initiates traffic (not just replying to what it's
    asked) -- registers a handler for the worker's unsolicited push,
    confirms it arrives, independent of and before the request it was
    triggered by returns its own reply."""
    mgr, comm, transport, run_task = await _spawn_supervisor_side()
    try:
        received = []
        push_seen = asyncio.Event()

        def on_worker_event(msg):
            received.append(msg)
            push_seen.set()
            return {"ack": True}

        comm.register("worker_event", on_worker_event)

        reply = await asyncio.wait_for(
            request(comm, "trigger_push", {"note": "hello"}), timeout=15
        )
        assert reply == {"triggered": True}

        await asyncio.wait_for(push_seen.wait(), timeout=15)
        assert len(received) == 1
        assert received[0]["note"] == "hello"
        import os
        assert received[0]["pid"] != os.getpid()
    finally:
        await _teardown(transport, run_task)


@pytest.mark.asyncio
async def test_worker_death_is_diagnosable_from_supervisor_log(caplog):
    """A deliberately-crashing command doesn't kill the whole worker
    process (the crash happens inside a registered handler, caught by
    CommMgr._handle_request same as any other exception) -- so this
    proves the *reply* carries the real error, not just silence."""
    mgr, comm, transport, run_task = await _spawn_supervisor_side()
    try:
        reply = await asyncio.wait_for(
            request(comm, "crash", {"reason": "task 3 diagnosability test"}), timeout=15
        )
        assert "error" in reply
        assert "task 3 diagnosability test" in reply["error"]
    finally:
        await _teardown(transport, run_task)


@pytest.mark.asyncio
async def test_worker_startup_failure_is_diagnosable_via_stderr_relay(caplog):
    """The real Task 3 case: the worker process itself fails to even
    start up (not a handler raising inside a running worker) -- the
    ONLY way to find out why is the relayed stderr, since there's no
    CommMgr reply channel to carry an error at all yet. Verify
    concretely that the supervisor's log shows *why* it died, not just
    that it died (the exact failure Chunk 1's sshpyk-diagnostics lesson
    was about)."""
    caplog.set_level(logging.WARNING, logger="cubevis.remote._worker_transport")

    mgr = CommMgr(role=CommMgr.ROLE_MIRROR, transport_type='remote_kernel')
    # A worker module that doesn't exist -- guaranteed startup failure,
    # standing in for "the worker raised before it could ever open a comm".
    transport = WorkerProcessTransport(str(uuid4()),
                                        worker_module="cubevis.remote._nonexistent_worker_module")
    await mgr.initialize(transport=transport)
    run_task = asyncio.ensure_future(transport.run())

    # Give the doomed process a moment to actually fail and flush stderr.
    await asyncio.sleep(2.0)

    run_task.cancel()
    try:
        await run_task
    except asyncio.CancelledError:
        pass
    await transport.close()

    assert transport.returncode not in (0, None), "the process should have exited non-zero"

    relayed = "\n".join(r.message for r in caplog.records)
    assert "No module named" in relayed or "ModuleNotFoundError" in relayed, (
        "the supervisor's log must show *why* the worker died (the traceback "
        "from python -m failing to find the module), not just that it died -- "
        f"got:\n{relayed}"
    )
