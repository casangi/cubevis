"""
Chunk 1, Task 6 -- start-vs-reattach against a real remote kernel process.
Unchanged by Chunk 1c.

Simulates: P_local session #1 starts a remote kernel and bootstraps the
cubevis worker in it (constructing a counter-backed "worker" object and
noting its identity). P_local session #1 then disconnects. P_local
session #2 -- a fresh KernelClientTransport, standing in for a brand new
P_local process -- reattaches to the SAME still-running kernel and calls
`ensure_remote_worker` again. This must:

  * NOT construct a second worker (build_worker's counter must still read 1)
  * hand back a comm target session #2 can immediately use for a normal
    request/response round trip

The remote kernel process itself staying alive across both "sessions" is
this test's stand-in for sshpyk's `existing=`/`persistent=` process-level
reattachment (real sshpyk usage needs SSH/cluster access this sandbox
doesn't have -- see the design doc addendum). What this test *does*
validate for real is the layer this codebase is actually responsible for:
correct behaviour once jupyter_client is reconnected to a kernel that
never went away.
"""
import asyncio
import os

import pytest

pytest.importorskip("jupyter_client")
pytest.importorskip("ipykernel")

from jupyter_client import AsyncKernelManager

from cubevis.bokeh.transport import CommMgr, AppState
from cubevis.remote import KernelClientTransport
from cubevis.remote import DEFAULT_TARGET_NAME


BOOTSTRAP_CODE = """
import sys
sys.path.insert(0, {src_path!r})
from cubevis.remote import ensure_remote_worker

def build_worker(mgr):
    global _BUILD_COUNT
    _BUILD_COUNT = globals().get('_BUILD_COUNT', 0) + 1
    return {{'build_count': _BUILD_COUNT}}

comm_mgr_id = ensure_remote_worker(build_worker, target_name={target_name!r})
print("COMM_MGR_ID=" + comm_mgr_id)
print("BUILD_COUNT=" + str(_BUILD_COUNT))
"""


async def _execute_and_collect(client, code, timeout=15):
    client.execute(code)
    stdout_lines = []
    while True:
        msg = await client.get_iopub_msg(timeout=timeout)
        if msg["msg_type"] == "stream":
            stdout_lines.extend(msg["content"]["text"].splitlines())
        elif msg["msg_type"] == "error":
            raise RuntimeError(f"remote cell raised: {msg['content']}")
        elif msg["msg_type"] == "status" and msg["content"]["execution_state"] == "idle":
            await client.get_shell_msg(timeout=timeout)
            break
    return stdout_lines


def _parse(lines, key):
    for line in lines:
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"{key} not found in remote output: {lines}")


@pytest.mark.asyncio
async def test_reattach_does_not_rebuild_worker_on_real_kernel():
    # tests/ -> remote/ -> cubevis/ -> src/ (three levels up from this file)
    src_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    target_name = DEFAULT_TARGET_NAME

    km = AsyncKernelManager(kernel_name="python3")
    await km.start_kernel(env={**os.environ, "PYTHONPATH": src_path})
    try:
        # --- "P_local session #1": first bootstrap -----------------------
        client1 = km.client()
        client1.start_channels()
        await client1.wait_for_ready(timeout=30)

        lines1 = await _execute_and_collect(
            client1, BOOTSTRAP_CODE.format(src_path=src_path, target_name=target_name)
        )
        comm_mgr_id_1 = _parse(lines1, "COMM_MGR_ID")
        build_count_1 = _parse(lines1, "BUILD_COUNT")
        assert build_count_1 == "1"

        # Prove session #1 can actually talk to the worker it just built.
        local_mgr_1 = CommMgr(role=CommMgr.ROLE_MIRROR)
        kct1 = KernelClientTransport(comm_mgr_id_1, km, target_name=target_name)
        local_mgr_1._transport = kct1
        kct1.set_message_callback(local_mgr_1._route_message)
        await kct1.connect()
        local_mgr_1.state = AppState.RUNNING
        run_task_1 = asyncio.ensure_future(kct1.run())

        # session #1 "disconnects" -- close its comm and its jupyter_client
        # channels, but NOT the kernel itself (the kernel process staying
        # alive is the whole point of a reattach scenario).
        run_task_1.cancel()
        try:
            await run_task_1
        except asyncio.CancelledError:
            pass
        await kct1.close()
        client1.stop_channels()

        # --- "P_local session #2": reattach, same live kernel ------------
        # A genuinely fresh jupyter_client client -- not the one session
        # #1 used and closed -- standing in for a brand new P_local
        # process reattaching, not the same process resuming.
        client1b = km.client()
        client1b.start_channels()
        await client1b.wait_for_ready(timeout=30)

        lines2 = await _execute_and_collect(
            client1b, BOOTSTRAP_CODE.format(src_path=src_path, target_name=target_name)
        )
        client1b.stop_channels()
        comm_mgr_id_2 = _parse(lines2, "COMM_MGR_ID")
        build_count_2 = _parse(lines2, "BUILD_COUNT")

        # The whole point: build_worker must NOT have run again, and the
        # bootstrap must hand back the *same* comm target as before.
        assert build_count_2 == "1", (
            "reattaching re-ran build_worker -- this would discard live "
            "worker state (an opened MS, an in-progress gclean run, etc.) "
            "on every reconnect, exactly the failure Task 6 exists to avoid"
        )
        assert comm_mgr_id_2 == comm_mgr_id_1, (
            "reattach should hand back the SAME worker identity, not mint "
            "a new one"
        )

        client2 = km.client()
        client2.start_channels()
        await client2.wait_for_ready(timeout=30)

        local_mgr_2 = CommMgr(role=CommMgr.ROLE_MIRROR)
        kct2 = KernelClientTransport(comm_mgr_id_2, km, target_name=target_name)
        local_mgr_2._transport = kct2
        kct2.set_message_callback(local_mgr_2._route_message)
        await kct2.connect()
        assert kct2.is_connected()
        local_mgr_2.state = AppState.RUNNING
        run_task_2 = asyncio.ensure_future(kct2.run())
        try:
            # Session #2's reattached comm target should be immediately
            # usable, exactly like session #1's was.
            local_comm_2 = local_mgr_2.open("post-reattach")
        finally:
            run_task_2.cancel()
            try:
                await run_task_2
            except asyncio.CancelledError:
                pass
            await kct2.close()
            client2.stop_channels()
    finally:
        await km.shutdown_kernel()
