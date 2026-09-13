"""
Regression coverage for the Chunk 1 remote-execution machinery: start a
kernel, bootstrap a (toy) worker in it via ``ensure_remote_worker``,
open a ``CommMgr`` mirror link against it, and round-trip real commands.

Promoted 2026-09 (Chunk 2d) from ``try_local_or_remote_kernel.py``'s
hand-run demo -- first pytest coverage of this path; previously only
ever exercised by hand. Runs against a local kernel by default (the
"python3" kernelspec) -- see that script's own docstring for why this
is a faithful stand-in for a real sshpyk-provisioned remote kernel:
nothing past ``AsyncKernelManager`` construction knows or cares whether
the kernel is local or remote. Override with the ``CUBEVIS_TEST_KERNEL``
env var to point this at a real sshpyk-registered kernel name instead.
"""
from __future__ import annotations

import asyncio
import os
import socket

import pytest
import pytest_asyncio
from jupyter_client import AsyncKernelManager

from cubevis.bokeh.transport import CommMgr
from cubevis.remote import DEFAULT_TARGET_NAME, open_remote_kernel_link, request

KERNEL_NAME = os.environ.get("CUBEVIS_TEST_KERNEL", "python3")

BOOTSTRAP_CODE = """
from cubevis.remote import ensure_remote_worker

def build_worker(mgr):
    import os, socket
    comm = mgr.open("demo")

    def handle_ping(msg):
        return {{"pong": True, "pid": os.getpid(), "hostname": socket.gethostname()}}

    def handle_add(msg):
        a, b = msg["a"], msg["b"]
        return {{"sum": a + b, "computed_on_host": socket.gethostname()}}

    comm.register("ping", handle_ping)
    comm.register("add", handle_add)
    return {{"demo_worker": True, "pid": os.getpid(), "hostname": socket.gethostname()}}

comm_mgr_id = ensure_remote_worker(build_worker, target_name={target_name!r})
print("BOOTSTRAP_OK comm_mgr_id=" + comm_mgr_id)
""".format(target_name=DEFAULT_TARGET_NAME)


async def _run_bootstrap(client, code: str, timeout: float = 60.0) -> None:
    client.execute(code)
    while True:
        msg = await client.get_iopub_msg(timeout=timeout)
        msg_type = msg.get("msg_type")
        if msg_type == "error":
            tb = "\n".join(msg["content"].get("traceback", []))
            raise RuntimeError(f"remote bootstrap cell raised:\n{tb}")
        elif msg_type == "status" and msg["content"]["execution_state"] == "idle":
            await client.get_shell_msg(timeout=timeout)
            return


@pytest_asyncio.fixture
async def kernel_mgr():
    km = AsyncKernelManager(kernel_name=KERNEL_NAME)
    await km.start_kernel()
    try:
        yield km
    finally:
        await km.shutdown_kernel()


@pytest_asyncio.fixture
async def bootstrapped_link(kernel_mgr):
    """A kernel with the demo worker bootstrapped and a CommMgr mirror
    link open against it -- the state every test in this file wants.

    Function-scoped (fresh kernel per test) rather than shared across
    tests, matching this suite's existing convention elsewhere
    (test_visibility_scatter.py's setup_method/teardown_method) of
    paying for isolation rather than risking cross-test state bleed.
    """
    setup_client = kernel_mgr.client()
    setup_client.start_channels()
    await setup_client.wait_for_ready(timeout=90)
    await _run_bootstrap(setup_client, BOOTSTRAP_CODE)
    setup_client.stop_channels()

    mgr, transport = await open_remote_kernel_link(kernel_mgr)
    run_task = asyncio.ensure_future(transport.run())
    try:
        yield mgr, transport
    finally:
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        await transport.close()


@pytest.mark.asyncio
async def test_link_role_and_transport_type(bootstrapped_link):
    mgr, transport = bootstrapped_link
    assert mgr.role == CommMgr.ROLE_MIRROR
    assert mgr.transport_type == "remote_kernel"
    assert transport.is_connected()


@pytest.mark.asyncio
async def test_ping_executes_on_the_kernel_process_not_here(bootstrapped_link):
    """The whole point of this path: the command must genuinely run in
    the kernel's own process, not be silently short-circuited locally.

    Confirmed for real (2026-09) against a real sshpyk-provisioned
    zuul06 kernel -- and that same run is exactly why the strict
    ``hostname == our own hostname`` assertion this test used to make
    was wrong in general: it came back ``"zuul06"``, correctly distinct
    from the laptop running pytest, which is precisely the point of
    this test and precisely why that assertion failed. It only ever
    held for local-kernel testing (same host by construction) -- kept
    below, but scoped to that case specifically. ``pid`` differing
    from our own is the universal proof of "ran elsewhere," local or
    real; a non-empty ``hostname`` coming back at all is the weaker,
    always-true half of that same check.
    """
    mgr, _ = bootstrapped_link
    comm = mgr.open("demo")
    reply = await request(comm, "ping", {})
    assert reply["pong"] is True
    assert reply["pid"] != os.getpid()
    assert reply["hostname"]
    if KERNEL_NAME == "python3":
        assert reply["hostname"] == socket.gethostname()


@pytest.mark.asyncio
async def test_add_computes_remotely(bootstrapped_link):
    mgr, _ = bootstrapped_link
    comm = mgr.open("demo")
    reply = await request(comm, "add", {"a": 19, "b": 23})
    assert reply["sum"] == 42
