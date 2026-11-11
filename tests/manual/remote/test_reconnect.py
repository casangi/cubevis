"""
Scenario A: a session through a change of network (remote session handoff
2026-11-04, section (b)1; notes section 82).

The real thing -- an ssh route that dies and another that works -- is
exercised on a rig (``tests/manual/remote/rig_scenario_a.py``, root and
sshd needed).  These tests cover each piece without one:

* the watch (``RemoteObjectConnection._watched``) with a heartbeat that goes
  quiet and comes back: reconnecting is started, a read waiting through the
  gap is sent again, a write fails as interrupted, a short blip changes
  nothing, a call that ran out of time during the gap is held and resent,
  the budget ends a call with the reason, the next call reconnects first,
  and the state listener hears it all;
* sshpyk's control-socket check off the event loop (``make_nonblocking``);
* on a kernel on this computer: ``CommMgr.abandon_inflight`` frees the
  link's comm from a request whose reply never comes, and
  ``KernelClientTransport.renew_client`` replaces the client with calls
  still answered.

    CUBEVIS_TEST_KERNEL_PYTHON is not needed: a local ``python3`` kernel.
"""
import asyncio
import os
import time

import pytest

from cubevis.remote import _remote_object as R
from cubevis.remote import _reconnect as C


# ---- fakes ------------------------------------------------------------------

class _HB:
    def __init__(self):
        self.beating = True

    def is_beating(self):
        return self.beating


class _Mgr:
    def __init__(self):
        self.abandoned = 0

    async def abandon_inflight(self):
        self.abandoned += 1
        return 1


class _Transport:
    def __init__(self, hb):
        self._client = type("C", (), {})()
        self._client.hb_channel = hb
        self.renewed = 0

    async def renew_client(self):
        self.renewed += 1


class _Link:
    def __init__(self, hb):
        self.transport = _Transport(hb)
        self.mgr = _Mgr()


class _KM:
    async def is_alive(self):
        return True


class _Reconnector:
    host = "zuul06"

    def __init__(self, hb=None, heals=True):
        self.hb, self.heals, self.attempts = hb, heals, 0
        self.last_error = ""

    async def reconnect(self):
        self.attempts += 1
        if self.heals and self.hb is not None:
            self.hb.beating = True
            return True
        self.last_error = "ssh to zuul06 failed: Connection timed out"
        return False


def _conn(hb, reconnector=None, repeatable=()):
    c = object.__new__(R.RemoteObjectConnection)
    c._km, c._link = _KM(), _Link(hb)
    c._kernel_name, c._app = "cubevis-zuul06", "visplot"
    c._reconnector = reconnector
    c._repeatable = frozenset(repeatable)
    c._listeners = []
    c._state = "connected"
    c._link_down = False
    c._renewed_at = 0.0
    c._renew_lock = None
    return c


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setenv("CUBEVIS_REMOTE_LIVENESS", "0.1")
    monkeypatch.setenv("CUBEVIS_REMOTE_RECONNECT", "3")
    monkeypatch.setattr(R, "RECONNECT_AFTER", 0.3)
    monkeypatch.setattr(R, "RESEND_SETTLE", 0.3)
    monkeypatch.setattr(R, "HEARTBEAT_GRACE", 1.0)


async def _forever():
    await asyncio.sleep(3600)


# ---- the watch ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_read_waiting_through_a_gap_is_sent_again(fast):
    hb = _HB()
    rc = _Reconnector(hb)
    c = _conn(hb, rc, repeatable={"query_columns"})
    sent = []

    async def again():
        sent.append(1)
        return {"answer": 42}

    states = []
    c.add_state_listener(lambda s, d: states.append((s, d["host"])))
    hb.beating = False                        # the network goes away under the call
    t0 = time.monotonic()
    assert await c._watched(_forever(), method="query_columns", resend=again) == {"answer": 42}
    assert sent == [1] and rc.attempts >= 1
    assert c._link.transport.renewed == 1     # a new kernel client once the heartbeat is back
    assert c._link.mgr.abandoned == 1         # the lost request no longer holds the comm
    assert time.monotonic() - t0 < 3
    assert ("reconnecting", "zuul06") in states and states[-1][0] == "connected"


@pytest.mark.asyncio
async def test_a_write_waiting_through_a_gap_is_interrupted(fast):
    hb = _HB()
    c = _conn(hb, _Reconnector(hb), repeatable={"query_columns"})

    async def again():
        raise AssertionError("a write must not be sent twice")

    hb.beating = False
    with pytest.raises(R.RemoteCallInterruptedError, match="commit_pending_flags.*not known"):
        await c._watched(_forever(), method="commit_pending_flags", resend=again)
    assert c._link.mgr.abandoned == 1
    assert issubclass(R.RemoteCallInterruptedError, R.RemoteKernelLostError)


@pytest.mark.asyncio
async def test_a_missed_beat_or_two_changes_nothing(fast):
    hb = _HB()
    rc = _Reconnector(hb)
    c = _conn(hb, rc, repeatable={"query_columns"})

    async def answer():
        await asyncio.sleep(0.15)
        hb.beating = False                    # one quiet sample, shorter than RECONNECT_AFTER
        await asyncio.sleep(0.15)
        hb.beating = True
        await asyncio.sleep(0.6)
        return "first"

    async def again():
        return "second"

    assert await c._watched(answer(), method="query_columns", resend=again) == "first"
    assert rc.attempts == 0 and c._link.transport.renewed == 0


@pytest.mark.asyncio
async def test_a_call_whose_time_ran_out_in_the_gap_is_held_and_sent_again(fast):
    hb = _HB()
    c = _conn(hb, _Reconnector(hb, heals=False), repeatable={"axis_info"})

    async def times_out():
        await asyncio.sleep(0.15)
        raise asyncio.TimeoutError()

    async def again():
        return "after"

    async def heal_later():
        await asyncio.sleep(0.8)
        hb.beating = True

    hb.beating = False
    healer = asyncio.ensure_future(heal_later())
    assert await c._watched(times_out(), method="axis_info", resend=again) == "after"
    await healer


@pytest.mark.asyncio
async def test_the_budget_ends_the_call_with_the_reason_and_the_next_call_reconnects(fast):
    hb = _HB()
    rc = _Reconnector(hb, heals=False)
    c = _conn(hb, rc)
    hb.beating = False
    t0 = time.monotonic()
    with pytest.raises(R.RemoteKernelLostError) as info:
        await c._watched(_forever(), method="query_raster")
    assert 2.5 < time.monotonic() - t0 < 6
    msg = str(info.value)
    assert "Connection timed out" in msg and "next action tries to reconnect" in msg
    assert "Close this" not in msg
    assert c.state == "lost" and c._link_down
    # the next call: reconnect first, then send
    rc.heals = True
    c._ctx = type("X", (), {})()
    calls = []

    async def dispatch_fast(message_id, payload=None, timeout=None):
        calls.append(payload["method"])
        return {"value": 7}
    c._ctx.dispatch_fast = dispatch_fast
    c._handle, c._closed, c._worker_dead = "h", False, False
    c._call_timeout, c._data_timeout, c._data_methods = 30.0, 600.0, frozenset()
    assert await c.acall("metadata") == {"value": 7}
    assert calls == ["metadata"] and c.state == "connected" and not c._link_down


@pytest.mark.asyncio
async def test_a_kernel_without_ssh_keeps_the_old_grace_and_message(fast):
    hb = _HB()
    c = _conn(hb, None)
    hb.beating = False
    with pytest.raises(R.RemoteKernelLostError, match="connection to it was lost.*Close this"):
        await c._watched(_forever())


# ---- sshpyk's check off the loop ------------------------------------------------

class SSHKernelProvisioner:                    # the name make_nonblocking looks for
    def __init__(self):
        self.ssh_base = ["false"]               # `false -O check host`: no control socket
        self.ssh_base_help = ["ssh"]
        self.all_hosts = ["hop", "node"]
        self.launch_timeout = 5
        self.log, self.log_prefix, self.independent_local_processes = None, "", False
        self.messages = []

    def ld(self, m, *a, **k): self.messages.append(m)
    def le(self, m, *a, **k): self.messages.append(m)
    def lw(self, m, *a, **k): self.messages.append(m)

    async def has_control_socket(self, alias, log_error=False):
        raise AssertionError("replaced")

    async def check_control_sockets(self, raise_on_failed_open=False):
        raise AssertionError("replaced")


@pytest.mark.asyncio
async def test_sshpyks_reconnect_no_longer_holds_the_event_loop(monkeypatch):
    pytest.importorskip("sshpyk")
    import sshpyk.utils
    opened = []

    def slow_verify(**kw):                       # sshpyk's blocking subprocess.run, 1.5 s
        time.sleep(1.5)
        opened.append(kw["host_alias"])
        return True, "Linux"
    monkeypatch.setattr(sshpyk.utils, "verify_ssh_connection", slow_verify)
    km = type("KM", (), {})()
    km.provisioner = SSHKernelProvisioner()
    assert C.make_nonblocking(km) and C.make_nonblocking(km)       # once; idempotent
    stalls, done = [], False

    async def ticker():
        last = time.monotonic()
        while not done:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            stalls.append(now - last)
            last = now
    t = asyncio.ensure_future(ticker())
    await km.provisioner.check_control_sockets()
    done = True
    await t
    assert opened == ["hop", "node"]
    assert max(stalls) < 0.5


def test_a_kernel_without_sshpyk_gets_nothing():
    km = type("KM", (), {"provisioner": object()})()
    assert not C.make_nonblocking(km) and C.SshReconnector.for_kernel(km) is None


# ---- on a kernel on this computer --------------------------------------------------

async def _context():
    pytest.importorskip("jupyter_client")
    pytest.importorskip("ipykernel")
    from jupyter_client import AsyncKernelManager
    from cubevis.remote import RemoteAppLink
    src = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    km = AsyncKernelManager(kernel_name="python3")
    await km.start_kernel(env={**os.environ, "PYTHONPATH": src})
    link = await RemoteAppLink.open(km, worker_target_name="test-reconnect", timeout=30)
    ctx = await link.create_context(config={
        "register_function": "cubevis.remote._test_registrations:register_basic"})
    reply = await ctx.dispatch_fast("create_object", {"class_name": "Counter", "args": [],
                                                      "kwargs": {"start": 5}})
    return km, link, ctx, reply["handle"]


async def _call(ctx, handle, method, timeout=30, **kwargs):
    return await ctx.dispatch_fast("call_method", {"handle": handle, "method": method,
                                                   "args": [], "kwargs": kwargs},
                                   timeout=timeout)


@pytest.mark.asyncio
async def test_a_request_whose_reply_never_comes_no_longer_holds_the_comm():
    km, link, ctx, h = await _context()
    try:
        assert await _call(ctx, h, "get") == 5
        stuck = asyncio.ensure_future(_call(ctx, h, "slow_get", seconds=30, timeout=60))
        await asyncio.sleep(0.5)
        # One request at a time per comm: without abandon this one waits ~30 s.
        queued = asyncio.ensure_future(_call(ctx, h, "get", timeout=60))
        await asyncio.sleep(0.5)
        assert not queued.done()
        assert await link.mgr.abandon_inflight() == 1
        stuck.cancel()
        # The worker answers once slow_get ends; what matters is the comm sent it.
        assert await asyncio.wait_for(queued, timeout=45) == 5
    finally:
        try:
            await link.close(timeout=5)
        except Exception:
            pass
        await km.shutdown_kernel(now=True)


@pytest.mark.asyncio
async def test_a_new_kernel_client_keeps_the_link_answering():
    km, link, ctx, h = await _context()
    try:
        old = link.transport._client
        assert await _call(ctx, h, "increment", by=2) == 7
        await link.transport.renew_client()
        assert link.transport._client is not old
        assert link.transport._client.session.session != old.session.session
        # the read loop survived the old client's channels stopping under it
        for i in range(3):
            assert await _call(ctx, h, "increment") == 8 + i
    finally:
        try:
            await link.close(timeout=5)
        except Exception:
            pass
        await km.shutdown_kernel(now=True)
