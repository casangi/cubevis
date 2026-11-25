"""Re-ask instead of a deadline (issue #35, 2026-11-23).

A request that has had no reply for ``_reask_after`` s is sent again with the
same request_id and ``reask: True``.  The peer answers from its RP-1 record:
the stored reply (finished), ``__running__`` (still running: wait again), or
by running it (never had it).  A request is forgotten -- callback called with
an error, the comm's queue moves on -- only when a re-ask goes unanswered
while the peer shows it is alive (frames from it since the re-ask).  A silent
peer (blocked, asleep) or no connection: it waits, however long.

Also here: ``CommMgr.forget`` itself (by request_id or callback, in flight or
queued), the order of a comm's requests after a forget, RK-1
(``remote._bridge.request`` forgets on its timeout) and the event-loop lag
monitor (HB-1).

Uses the fake frontend of test_reconnection.py.
"""
import asyncio
import logging
import time

import pytest
import websockets

from cubevis.utils import serialize, deserialize
import cubevis.bokeh.transport._comm_mgr as _cm

from test_reconnection import Frontend, _new_mgr, _port, HOST

FAST = dict(_reask_after=0.4, _reask_check_interval=0.1, _reask_liveness_ticks=2)


class Peer(Frontend):
    """The fake frontend with a say in how each p2j request is answered:
    ``policy(msg)`` returns "reply", "drop" or "running".  ``ping_every``
    makes it send __ping__ frames (what the real page does every 15 s), so
    Python sees it is alive."""

    def __init__(self, mgr, port, policy, ping_every=None, **kw):
        super().__init__(mgr, port, reply=False, **kw)
        self._policy = policy
        self._ping_every = ping_every
        self._pinger = None
        self.answered = []

    async def connect(self):
        await super().connect()
        if self._ping_every:
            self._pinger = asyncio.create_task(self._ping_loop())
        return self

    async def _ping_loop(self):
        seq = 0
        try:
            while True:
                await asyncio.sleep(self._ping_every)
                seq += 1
                await self.ws.send(serialize({"type": "__ping__", "seq": seq}))
        except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
            pass

    def stop_pinging(self):
        if self._pinger:
            self._pinger.cancel()

    async def _run(self):
        try:
            async for raw in self.ws:
                msg = deserialize(raw)
                if msg.get("direction") != "p2j" or "request_id" not in msg:
                    continue
                self.received.append(msg)
                self.got_request.set()
                action = self._policy(msg)
                if action == "reply":
                    self.answered.append(msg["request_id"])
                    await self.ws.send(serialize({
                        "comm_id": msg["comm_id"], "message_id": msg["message_id"],
                        "request_id": msg["request_id"], "direction": "p2j",
                        "message": {"ok": True, "n": len(self.answered)}}))
                elif action == "running":
                    await self.ws.send(serialize({
                        "type": "__running__", "comm_id": msg["comm_id"],
                        "message_id": msg["message_id"],
                        "request_id": msg["request_id"], "direction": "j2p"}))
        except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
            pass

    async def close(self):
        self.stop_pinging()
        await super().close()


async def _serve(mgr, port):
    return await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)


async def _until(cond, timeout=5.0):
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > timeout:
            return False
        await asyncio.sleep(0.05)
    return True


# ----------------------------------------------------------------------
# The sending side (Python asks, the fake page answers)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_lost_reply_is_recovered_by_the_reask():
    port = _port()
    mgr = _new_mgr(port, **FAST)
    comm = mgr.open("data")
    server = await _serve(mgr, port)
    try:
        # The first copy's reply is "lost"; the re-ask is answered.
        peer = await Peer(mgr, port, lambda m: "reply" if m.get("reask") else "drop").connect()
        got = []
        await comm.send("update", {"x": 1}, callback=got.append)
        assert await _until(lambda: got, 3), "the re-ask did not recover the reply"
        assert got[0].get("ok") is True
        assert [m.get("reask", False) for m in peer.received] == [False, True]
        assert len({m["request_id"] for m in peer.received}) == 1, "a re-ask keeps the request_id"
        assert not mgr._pending_requests
        await peer.close()
    finally:
        server.close(); await server.wait_closed()


@pytest.mark.asyncio
async def test_running_keeps_a_long_request_waiting_with_no_error():
    port = _port()
    mgr = _new_mgr(port, **FAST)
    comm = mgr.open("data")
    server = await _serve(mgr, port)
    try:
        finish = asyncio.Event()
        peer = await Peer(mgr, port,
                          lambda m: ("reply" if finish.is_set() else "running") if m.get("reask") else "drop",
                          ping_every=0.05).connect()
        got = []
        await comm.send("update", {"x": 1}, callback=got.append)
        await asyncio.sleep(2.5)                      # six re-ask periods, the page alive throughout
        assert got == [], f"a running request was failed: {got}"
        assert sum(1 for m in peer.received if m.get("reask")) >= 3
        finish.set()
        assert await _until(lambda: got, 3)
        assert got[0].get("ok") is True
        await peer.close()
    finally:
        server.close(); await server.wait_closed()


@pytest.mark.asyncio
async def test_unanswered_reask_with_the_page_alive_is_forgotten_and_the_queue_moves_on():
    port = _port()
    mgr = _new_mgr(port, **FAST)
    comm = mgr.open("data")
    server = await _serve(mgr, port)
    try:
        # Never answers "update" (not even the re-ask), answers "next".
        peer = await Peer(mgr, port, lambda m: "reply" if m["message_id"] == "next" else "drop",
                          ping_every=0.05).connect()
        first, second = [], []
        await comm.send("update", {"x": 1}, callback=first.append)
        await comm.send("next", {"x": 2}, callback=second.append)      # queued behind it
        assert await _until(lambda: first, 4), "the lost request was never given up on"
        assert "error" in first[0]
        assert await _until(lambda: second, 3), "the queue did not move on after the forget"
        assert second[0].get("ok") is True
        assert [m["message_id"] for m in peer.received if not m.get("reask")] == ["update", "next"]
        await peer.close()
    finally:
        server.close(); await server.wait_closed()


@pytest.mark.asyncio
async def test_a_silent_page_is_waited_for_not_given_up_on():
    # No frames from the page after the re-ask (its loop blocked, or asleep
    # with the socket still up): no evidence the request was lost.
    port = _port()
    mgr = _new_mgr(port, **FAST)
    comm = mgr.open("data")
    server = await _serve(mgr, port)
    try:
        peer = await Peer(mgr, port, lambda m: "drop").connect()      # no pings
        got = []
        await comm.send("update", {"x": 1}, callback=got.append)
        await asyncio.sleep(2.0)
        assert got == [], f"a silent page's request was failed: {got}"
        assert any(m.get("reask") for m in peer.received)
        assert mgr._pending_requests
        await peer.close()
    finally:
        server.close(); await server.wait_closed()


@pytest.mark.asyncio
async def test_no_connection_means_no_reask_and_no_forget():
    # iclean left running with the laptop unplugged: the request waits.
    port = _port()
    mgr = _new_mgr(port, **FAST)
    comm = mgr.open("data")
    server = await _serve(mgr, port)
    try:
        peer = await Peer(mgr, port, lambda m: "drop", ping_every=0.05).connect()
        got = []
        await comm.send("update", {"x": 1}, callback=got.append)
        await asyncio.sleep(0.1)
        await peer.suspend()                          # gone, no close frame
        await asyncio.sleep(2.0)
        assert got == []
        # It comes back: the request is replayed (RP-1) and answered.
        peer = await Peer(mgr, port, lambda m: "reply", ping_every=0.05).connect()
        assert await _until(lambda: got, 3)
        assert got[0].get("ok") is True
        await peer.close()
    finally:
        server.close(); await server.wait_closed()


# ----------------------------------------------------------------------
# The answering side (the fake page asks, Python answers)
# ----------------------------------------------------------------------

async def _ask(ws, request_id, message_id="slow", reask=False):
    msg = {"comm_id": "data", "message_id": message_id, "request_id": request_id,
           "direction": "j2p", "message": {}}
    if reask:
        msg["reask"] = True
    await ws.send(serialize(msg))


async def _raw_page(mgr, port):
    ws = await websockets.connect(f"ws://{HOST}:{port}")
    await ws.send(serialize({"id": "initialize", "direction": "j2p", "frontend_id": "raw",
                             "claim": False, "backend_id": None, "comm_mgr_id": mgr.comm_mgr_id}))
    assert deserialize(await ws.recv()).get("type") == "initialized"
    return ws


async def _next(ws, timeout=3.0):
    return deserialize(await asyncio.wait_for(ws.recv(), timeout))


@pytest.mark.asyncio
async def test_reask_of_a_running_request_gets_running_then_the_reply():
    port = _port()
    mgr = _new_mgr(port)
    comm = mgr.open("data")
    release = asyncio.Event()
    runs = []

    async def slow(msg):
        runs.append(1)
        await release.wait()
        return {"done": True}

    comm.register("slow", slow)
    server = await _serve(mgr, port)
    try:
        ws = await _raw_page(mgr, port)
        await _ask(ws, "r1")
        await asyncio.sleep(0.2)
        await _ask(ws, "r1", reask=True)
        note = await _next(ws)
        assert note.get("type") == "__running__" and note.get("request_id") == "r1"
        release.set()
        reply = await _next(ws)
        assert reply.get("request_id") == "r1" and reply["message"] == {"done": True}
        await _ask(ws, "r1", reask=True)                 # finished: the reply again
        again = await _next(ws)
        assert again.get("request_id") == "r1" and again["message"] == {"done": True}
        assert runs == [1], "the handler ran more than once"
        await ws.close()
    finally:
        server.close(); await server.wait_closed()


@pytest.mark.asyncio
async def test_a_reconnect_replay_of_a_running_request_is_still_dropped_silently():
    # Without reask (a replay after a reconnect, or an older peer) nothing
    # new is sent: unchanged from RP-1.
    port = _port()
    mgr = _new_mgr(port)
    comm = mgr.open("data")
    release = asyncio.Event()

    async def slow(msg):
        await release.wait()
        return {"done": True}

    comm.register("slow", slow)
    server = await _serve(mgr, port)
    try:
        ws = await _raw_page(mgr, port)
        await _ask(ws, "r1")
        await asyncio.sleep(0.2)
        await _ask(ws, "r1")
        with pytest.raises(asyncio.TimeoutError):
            await _next(ws, timeout=0.5)
        release.set()
        assert (await _next(ws)).get("request_id") == "r1"
        await ws.close()
    finally:
        server.close(); await server.wait_closed()


@pytest.mark.asyncio
async def test_after_a_forget_the_next_request_runs_after_the_forgotten_one():
    # The sender gave up on r1 and sent r2 on the same comm; r1 is in fact
    # still running here.  The comm lock keeps the order: r2 starts only
    # once r1 has finished.
    port = _port()
    mgr = _new_mgr(port)
    comm = mgr.open("data")
    events = []

    async def slow(msg):
        events.append(("start", msg["n"]))
        await asyncio.sleep(0.5)
        events.append(("end", msg["n"]))
        return {"n": msg["n"]}

    comm.register("slow", slow)
    server = await _serve(mgr, port)
    try:
        ws = await _raw_page(mgr, port)
        for rid, n in (("r1", 1), ("r2", 2)):
            await ws.send(serialize({"comm_id": "data", "message_id": "slow", "request_id": rid,
                                     "direction": "j2p", "message": {"n": n}}))
            await asyncio.sleep(0.05)
        replies = [await _next(ws), await _next(ws)]
        assert [r["request_id"] for r in replies] == ["r1", "r2"]
        assert events == [("start", 1), ("end", 1), ("start", 2), ("end", 2)]
        await ws.close()
    finally:
        server.close(); await server.wait_closed()


# ----------------------------------------------------------------------
# forget() itself, RK-1, the lag monitor
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_forget_by_callback_removes_a_queued_request_and_keeps_the_order_of_the_rest():
    port = _port()
    mgr = _new_mgr(port)
    comm = mgr.open("data")
    server = await _serve(mgr, port)
    try:
        hold = asyncio.Event()
        peer = await Peer(mgr, port, lambda m: "reply" if hold.is_set() else "drop").connect()
        a, b, c = [], [], []
        await comm.send("a", {}, callback=a.append)       # in flight
        await comm.send("b", {}, callback=b.append)       # queued
        await comm.send("c", {}, callback=c.append)       # queued
        assert mgr.forget() == 0                           # names nothing
        cb_b = mgr._send_queue["data"][0][2]
        assert mgr.forget(callback=cb_b, error="dropped") == 1
        assert b == [{"error": "dropped"}]
        cb_a = mgr._pending_requests[mgr._pending["data"]][3]
        hold.set()
        assert mgr.forget(callback=cb_a) == 1              # in flight, no error callback
        assert await _until(lambda: c, 3)
        assert a == []
        assert [m["message_id"] for m in peer.received] == ["a", "c"]
        await peer.close()
    finally:
        server.close(); await server.wait_closed()


@pytest.mark.asyncio
async def test_rk1_a_timed_out_request_does_not_wedge_the_comm():
    from cubevis.remote._bridge import request
    port = _port()
    mgr = _new_mgr(port)
    comm = mgr.open("worker")
    server = await _serve(mgr, port)
    try:
        # The first reply is lost for good (iopub); later ones arrive.
        seen = []
        peer = await Peer(mgr, port, lambda m: seen.append(m["message_id"]) or
                          ("drop" if m["message_id"] == "lost" else "reply")).connect()
        with pytest.raises(asyncio.TimeoutError):
            await request(comm, "lost", {}, timeout=0.3)
        assert "worker" not in mgr._pending, "the timed-out request left the comm pending"
        reply = await request(comm, "next", {}, timeout=2.0)
        assert reply.get("ok") is True
        # Cancelled from outside (an outer wait_for) behaves the same.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(request(comm, "lost", {}), timeout=0.3)
        assert "worker" not in mgr._pending
        assert (await request(comm, "next", {}, timeout=2.0)).get("ok") is True
        await peer.close()
    finally:
        server.close(); await server.wait_closed()


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.mark.asyncio
async def test_loop_lag_monitor_names_the_blocking_handler():
    port = _port()
    mgr = _new_mgr(port)
    comm = mgr.open("data")

    def blocking(msg):
        time.sleep(2.0)                                   # holds the loop
        return {"ok": True}

    comm.register("blocker", blocking)
    cap = _Capture()
    old = _cm.logger.level
    _cm.logger.setLevel(logging.DEBUG)
    _cm.logger.addHandler(cap)
    server = await _serve(mgr, port)
    try:
        ws = await _raw_page(mgr, port)
        await _ask(ws, "r1", message_id="blocker")
        await _next(ws)
        await asyncio.sleep(0.8)
        lines = [m for m in cap.messages if "event loop blocked" in m]
        assert lines, cap.messages[-10:]
        assert "blocking (data.blocker)" in lines[0], lines
        await ws.close()
    finally:
        _cm.logger.removeHandler(cap)
        _cm.logger.setLevel(old)
        server.close(); await server.wait_closed()


@pytest.mark.asyncio
async def test_a_send_while_the_page_is_away_goes_after_the_replay():
    # A request in flight when the page went is put back at the head of its
    # queue (RP-1).  A message sent before the page returns used to be held
    # at the head as well -- in front of it -- and so went first (found
    # 2026-11-24; the same in the Jupyter comm transport).
    port = _port()
    mgr = _new_mgr(port)
    comm = mgr.open("data")
    server = await _serve(mgr, port)
    try:
        peer = await Peer(mgr, port, lambda m: "drop").connect()
        got = []
        await comm.send("first", {}, callback=got.append)
        await asyncio.sleep(0.2)
        await peer.suspend()
        await asyncio.sleep(0.3)
        await comm.send("second", {}, callback=got.append)
        peer = await Peer(mgr, port, lambda m: "reply").connect()
        assert await _until(lambda: len(got) == 2, 3)
        assert [m["message_id"] for m in peer.received] == ["first", "second"]
        await peer.close()
    finally:
        server.close(); await server.wait_closed()
