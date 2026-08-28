"""
Chunk 1b, Task 5 definition-of-done: `RemoteAppLink.open()`/`.close()`
tested; the worker subprocess is confirmed gone (not just unreferenced)
after `close()`.
"""
import asyncio
import os

import pytest

pytest.importorskip("jupyter_client")
pytest.importorskip("ipykernel")

from jupyter_client import AsyncKernelManager

from cubevis.remote import RemoteAppLink, request


@pytest.mark.asyncio
async def test_open_close_confirms_worker_subprocess_actually_exits():
    src_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    km = AsyncKernelManager(kernel_name="python3")
    await km.start_kernel(env={**os.environ, "PYTHONPATH": src_path})
    try:
        link = await RemoteAppLink.open(km, worker_target_name="test-app-link", timeout=30)
        try:
            assert link.transport.is_connected()

            # Use the fast-dispatch proxy end to end -- proves this isn't
            # just a connected-but-idle link.
            comm = link.mgr.open("worker")
            reply = await asyncio.wait_for(
                request(comm, "dispatch_fast",
                        {"message_id": "add", "payload": {"a": 10, "b": 32}}),
                timeout=15,
            )
            assert reply == {"sum": 42}

            # Independently discover the worker's actual OS pid via the
            # kernel, so close()'s claim can be checked from *outside*
            # this process's own bookkeeping, not just by trusting
            # transport.returncode.
            reply = await asyncio.wait_for(
                request(comm, "dispatch_fast", {"message_id": "ping", "payload": {}}),
                timeout=15,
            )
            worker_pid = reply["pid"]
            assert _pid_alive(worker_pid), "worker subprocess should be alive before close()"

        finally:
            result = await link.close(timeout=20)

        assert result is not None and result.get("closed") is True, (
            f"close() should get back a confirmation from the supervisor's own "
            f"shutdown_worker handler, not just close its own side; got {result}"
        )
        assert result.get("returncode") is not None, (
            "the supervisor's confirmation must include the worker's actual exit code"
        )

        # The real proof: ask the OS directly, from this test process,
        # independent of anything cubevis reported.
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline and _pid_alive(worker_pid):
            await asyncio.sleep(0.1)
        assert not _pid_alive(worker_pid), (
            f"worker subprocess (pid={worker_pid}) is still alive after "
            f"RemoteAppLink.close() returned -- close() must confirm actual "
            f"process exit, not just that Python references were dropped"
        )
    finally:
        await km.shutdown_kernel()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours -- shouldn't happen here
    return True
