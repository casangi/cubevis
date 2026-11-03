"""
A worker that dies (round 3 of wrapping up setup, notes section 56).

Before: the supervisor awaited the worker's reply with no regard for the
worker process, so a worker killed mid-request (the system's memory limit
during a big scatter, Darrell's first cvpost106 run) left the request
unanswered, and the plotter waited out its whole data budget (600 s)
before a timeout said nothing of why.  Now the supervisor races every
relayed request against the worker's exit and answers at once with the
reason -- the signal, and the worker's last stderr lines.

    CUBEVIS_TEST_KERNEL_PYTHON is not needed: a local ``python3`` kernel.
"""
import asyncio
import os
import signal
import time

import pytest

pytest.importorskip("jupyter_client")
pytest.importorskip("ipykernel")

from jupyter_client import AsyncKernelManager

from cubevis.remote import RemoteAppLink


async def _link(name):
    src_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    km = AsyncKernelManager(kernel_name="python3")
    await km.start_kernel(env={**os.environ, "PYTHONPATH": src_path})
    link = await RemoteAppLink.open(km, worker_target_name=name, timeout=30)
    return km, link


@pytest.mark.asyncio
async def test_a_worker_killed_mid_request_is_reported_at_once():
    km, link = await _link("test-worker-death-1")
    try:
        ctx = await link.create_context()
        assert (await ctx.dispatch_fast("ping", {}))["pong"]
        req = asyncio.ensure_future(ctx.dispatch_fast("slow_echo", {"duration": 60}, timeout=120))
        await asyncio.sleep(1.0)                       # the worker is in the call
        os.kill(ctx.pid, signal.SIGKILL)               # as the out-of-memory killer does
        t0 = time.monotonic()
        reply = await req
        took = time.monotonic() - t0
        assert took < 10, f"reported after {took:.1f} s"
        assert isinstance(reply, dict) and "error" in reply, reply
        assert "WorkerDiedError" in reply.get("traceback", "")
        assert "SIGKILL" in reply["error"] and "memory" in reply["error"], reply["error"]
        assert f"pid {ctx.pid}" in reply["error"]
        # And every later request to it: at once, the same reason.
        t0 = time.monotonic()
        again = await ctx.dispatch_fast("ping", {}, timeout=120)
        assert time.monotonic() - t0 < 5 and "SIGKILL" in again["error"]
    finally:
        await link.close(timeout=20)
        await km.shutdown_kernel(now=True)


@pytest.mark.asyncio
async def test_a_worker_that_exits_says_its_status_and_last_words():
    km, link = await _link("test-worker-death-2")
    try:
        ctx = await link.create_context()
        await ctx.exec_code("import sys; sys.stderr.write('about to go\\n'); sys.stderr.flush()")
        req = asyncio.ensure_future(ctx.exec_code("import os; os._exit(3)"))
        reply = await asyncio.wait_for(req, 30)
        assert "exited with status 3" in reply["error"], reply
        assert "about to go" in reply["error"]
    finally:
        await link.close(timeout=20)
        await km.shutdown_kernel(now=True)


# ---------------------------------------------------------------------------
# The plotter's side (RemoteReductionContext): its liveness watch and the
# new worker after a dead one -- without an MS (the methods under test do
# not touch the data), on an object made without its constructor.
# ---------------------------------------------------------------------------

from cubevis.toolbox.visplot import remote_reduction_context as R


def _bare(km=None, link=None):
    rrc = R.RemoteReductionContext.__new__(R.RemoteReductionContext)
    rrc._km, rrc._link, rrc._kernel_name = km, link, "test-kernel"
    rrc._path, rrc._backend_kind = "x.ms", "msv2"
    rrc._context_config = {}
    return rrc


@pytest.mark.asyncio
async def test_a_kernel_killed_mid_call_ends_the_wait(monkeypatch):
    monkeypatch.setenv("CUBEVIS_REMOTE_LIVENESS", "0.5")
    km, link = await _link("test-worker-death-3")
    try:
        ctx = await link.create_context()
        rrc = _bare(km, link)
        call = asyncio.ensure_future(rrc._watched(
            ctx.dispatch_fast("slow_echo", {"duration": 60}, timeout=120)))
        await asyncio.sleep(1.0)
        os.kill(km.provisioner.process.pid, signal.SIGKILL)       # the kernel itself
        t0 = time.monotonic()
        with pytest.raises(R.RemoteKernelLostError, match="has stopped"):
            await call
        assert time.monotonic() - t0 < 10
    finally:
        try:
            await link.close(timeout=5)
        except Exception:
            pass
        await km.shutdown_kernel(now=True)


class _Alive:
    def __init__(self, alive=True):
        self.alive = alive

    async def is_alive(self):
        return self.alive


class _HB:
    def __init__(self, beating):
        self.beating = beating

    def is_beating(self):
        return self.beating


class _Link:
    def __init__(self, beating=True):
        self.transport = type("T", (), {})()
        self.transport._client = type("C", (), {})()
        self.transport._client.hb_channel = _HB(beating)


@pytest.mark.asyncio
async def test_a_silent_heartbeat_counts_as_a_lost_connection(monkeypatch):
    monkeypatch.setenv("CUBEVIS_REMOTE_LIVENESS", "0.2")
    monkeypatch.setattr(R, "HEARTBEAT_GRACE", 1.0)
    rrc = _bare(_Alive(True), _Link(beating=False))
    with pytest.raises(R.RemoteKernelLostError, match="connection to it was lost"):
        await rrc._watched(asyncio.sleep(30))
    # A beating heartbeat and a running kernel: the call's own result.
    rrc = _bare(_Alive(True), _Link(beating=True))

    async def answer():
        await asyncio.sleep(0.7)
        return 42
    assert await rrc._watched(answer()) == 42


class _Ctx:
    """dispatch_fast replies, in turn; records what it was asked."""
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def dispatch_fast(self, message_id, payload=None, timeout=None):
        self.calls.append((message_id, payload))
        return self.replies.pop(0)

    async def shutdown(self, timeout=None):
        return {"closed": True}


@pytest.mark.asyncio
async def test_a_dead_worker_is_reported_and_the_next_call_starts_a_new_one():
    died = {"error": "the worker process (pid 7 on node) was killed (SIGKILL) -- most often "
                     "the system's memory limit", "traceback": "...WorkerDiedError: ..."}
    old = _Ctx([died])
    new = _Ctx([{"handle": "h2"}, {"answer": 1}])
    link = _Link()

    async def create_context(config=None, timeout=None):
        return new
    link.create_context = create_context
    rrc = _bare(_Alive(True), link)
    rrc._ctx, rrc._handle = old, "h1"
    with pytest.raises(R.RemoteWorkerDiedError, match="SIGKILL.*the next Plot starts a new one"):
        await rrc._acall("query_columns")
    assert rrc._worker_dead
    assert await rrc._acall("query_columns") == {"answer": 1}
    assert rrc._ctx is new and rrc._handle == "h2" and not rrc._worker_dead
    assert new.calls[0][0] == "create_object"
    assert new.calls[0][1]["kwargs"] == {"path": "x.ms", "backend_kind": "msv2"}
    assert new.calls[1][1]["handle"] == "h2"


def test_sshpyks_identityfile_hint_is_quiet_for_setups_kernels(monkeypatch, tmp_path):
    import logging
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from cubevis.setup import sshgen
    lg = logging.getLogger("test-sshpyk-hint")
    lg.filters.clear()
    spec = type("S", (), {"metadata": {"kernel_provisioner": {"config": {
        "ssh_config": str(sshgen.config_path())}}}})()
    km = type("K", (), {"kernel_spec": spec, "log": lg})()
    R._quiet_setup_kernel_hints(km)
    R._quiet_setup_kernel_hints(km)
    assert len(lg.filters) == 1
    rec = lambda m: logging.LogRecord("x", logging.WARNING, "", 0, m, None, None)
    f = lg.filters[0]
    assert not f.filter(rec("[SSHPYK640] [Local ssh config for cv-login] identityfile: Likely "
                            "missing in your ssh config. Multiple values: ['~/.ssh/id_rsa']"))
    assert f.filter(rec("[SSHPYK144] batchmode: Recommended to be 'yes'"))
    # Another kernel (not setup's ssh configuration): left alone.
    other = logging.getLogger("test-sshpyk-hint-2")
    other.filters.clear()
    spec2 = type("S", (), {"metadata": {"kernel_provisioner": {"config": {"ssh_config": "/x"}}}})()
    R._quiet_setup_kernel_hints(type("K", (), {"kernel_spec": spec2, "log": other})())
    assert not other.filters
