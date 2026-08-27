"""
Chunk 1, Task 5 + definition-of-done bullet 4.

`KernelClientTransport` connects to and exchanges a message with a real
remote kernel -- a genuine spike, not just a design sketch. "Remote" here
is a real, separate `ipykernel` subprocess reached purely through
`jupyter_client`'s normal `AsyncKernelManager`/`AsyncKernelClient` API (no
loopback double, no mocked client) -- the same API surface `sshpyk` sits
underneath as a kernel provisioner.

Scope, stated plainly: this environment has no SSH/cluster access, so the
actual SSH tunneling `sshpyk` layers on top was NOT exercised here -- only
the standard jupyter_client kernel-management/comm-protocol surface
`sshpyk`-provisioned kernels use identically to a local one. See the
design doc addendum for what that does and doesn't cover.

This test is slower and heavier than the rest of Chunk 1's suite (it
spawns a real Python subprocess) -- kept in its own file so it's easy to
skip/deselect (`-k "not spike"`) if that's ever inconvenient, without
touching the fast loopback-based suite.
"""
import asyncio
import os
import sys

import pytest

pytest.importorskip("jupyter_client")
pytest.importorskip("ipykernel")

from jupyter_client import AsyncKernelManager

from cubevis.bokeh.transport import CommMgr, AppState
from cubevis.remote import KernelClientTransport


SETUP_CODE = """
import sys
sys.path.insert(0, {src_path!r})
from cubevis.bokeh.transport import CommMgr, AppState
from cubevis.remote import KernelCommTransport

mgr = CommMgr(role=CommMgr.ROLE_DEFAULT)
transport = KernelCommTransport(mgr.comm_mgr_id)
mgr._transport = transport
transport.set_message_callback(mgr._route_message)

comm = mgr.open('spike')
def handle_ping(msg):
    return {{'pong': msg.get('n', 0) + 1, 'answered_in': 'remote-kernel'}}
comm.register('ping', handle_ping)

# Registering the target is synchronous; ipykernel's own shell-channel
# dispatch (independent of our code -- see KernelCommTransport's
# docstring) invokes _on_comm_open/_on_comm_msg once the client sends
# comm_open/comm_msg.
import comm as _comm_pkg
_comm_pkg.get_comm_manager().register_target(mgr.comm_mgr_id, transport._on_comm_open)
mgr.state = AppState.RUNNING
mgr._initialized = True
print("COMM_MGR_ID=" + mgr.comm_mgr_id)
"""


async def _run_setup_and_get_comm_mgr_id(client, src_path: str) -> str:
    client.execute(SETUP_CODE.format(src_path=src_path))
    comm_mgr_id = None
    while True:
        msg = await client.get_iopub_msg(timeout=15)
        if msg["msg_type"] == "stream":
            for line in msg["content"]["text"].splitlines():
                if line.startswith("COMM_MGR_ID="):
                    comm_mgr_id = line.split("=", 1)[1].strip()
        elif msg["msg_type"] == "error":
            raise RuntimeError(f"remote setup cell raised: {msg['content']}")
        elif msg["msg_type"] == "status" and msg["content"]["execution_state"] == "idle":
            await client.get_shell_msg(timeout=15)  # drain the execute_reply
            break
    assert comm_mgr_id is not None, "remote setup cell did not report a comm_mgr_id"
    return comm_mgr_id


@pytest.mark.asyncio
async def test_kernel_client_transport_round_trip_against_real_kernel():
    # tests/ -> remote/ -> cubevis/ -> src/ (three levels up from this file)
    src_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

    km = AsyncKernelManager(kernel_name="python3")
    await km.start_kernel(env={**os.environ, "PYTHONPATH": src_path})
    try:
        client = km.client()
        client.start_channels()
        await client.wait_for_ready(timeout=30)

        comm_mgr_id = await _run_setup_and_get_comm_mgr_id(client, src_path)

        # P_local's side: a mirrored CommMgr driving the real remote
        # kernel through KernelClientTransport.
        local_mgr = CommMgr(role=CommMgr.ROLE_MIRROR)
        kct = KernelClientTransport(comm_mgr_id, km, target_name=comm_mgr_id)
        local_mgr._transport = kct
        kct.set_message_callback(local_mgr._route_message)

        await kct.connect()
        assert kct.is_connected()
        local_mgr.state = AppState.RUNNING
        local_mgr._initialized = True

        run_task = asyncio.ensure_future(kct.run())
        try:
            local_comm = local_mgr.open("spike")
            reply_box = {}
            local_comm.register("ping", lambda msg: None)  # not expected to fire; request() side only

            def on_reply(msg):
                reply_box["reply"] = msg

            await local_comm.send("ping", {"n": 41}, callback=on_reply)

            for _ in range(100):
                if "reply" in reply_box:
                    break
                await asyncio.sleep(0.1)

            assert reply_box.get("reply") == {
                "pong": 42,
                "answered_in": "remote-kernel",
            }, "did not receive the expected reply from the real remote kernel process"

            # And bookkeeping cleared correctly on the P_local side too --
            # same check as the loopback round-trip tests, now against a
            # genuine remote process instead of an in-process double.
            assert local_mgr._pending == {}
        finally:
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass
            await kct.close()
            client.stop_channels()
    finally:
        await km.shutdown_kernel()
