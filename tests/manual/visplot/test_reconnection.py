########################################################################
# Reconnection tests for cubevis/bokeh/transport
#
# These exercise CommMgr against a real `websockets` server and a real
# websocket client standing in for the JavaScript frontend. No Bokeh
# document, no browser, no BokehAppContext -- CommMgr is driven directly.
#
# The scenario that matters is a laptop suspending: the TCP connection is
# aborted without a close frame, so neither side sees a clean shutdown.
# `transport.abort()` on the client reproduces that exactly.
#
#     pytest -v tests/manual/reconnect/test_reconnection.py
#
# or standalone:
#
#     python tests/manual/reconnect/test_reconnection.py
########################################################################

import asyncio
import logging
import sys

import pytest
import websockets

from cubevis.bokeh import BokehInit
from cubevis.bokeh.transport import CommMgr
from cubevis.bokeh.transport import _comm_mgr as _comm_mgr_module
from cubevis.bokeh.transport._comm_mgr import AppState, ShutdownReason
from cubevis.utils._conversion import serialize, deserialize

HOST = "127.0.0.1"

# Every test below completes at least one real WebSocket handshake, and
# _low_level_transport.py's connect() calls BokehInit.get_app_context()
# as part of that handshake -- a process-global cache (BokehInit is a
# plain class-level list, not scoped per CommMgr/test at all: see
# cubevis/bokeh/__init__.py), lazily creating one BokehAppContext the
# first time anything calls it, then handing back that SAME object to
# every caller for the rest of the process. That context's
# `frontend_id` is exactly the state the "multiple tabs" check in
# _low_level_transport.py compares against on every future handshake.
#
# No test here ever tears this down -- each test's `finally` only
# closes its own websocket server, never the app context -- so
# whichever test runs first "wins" the shared context, and its
# `frontend_id` leaks into every later test in this file. This went
# unnoticed because every test using the `Frontend` class hardcodes the
# same "test-frontend" id, so reusing the leaked context never actually
# produces a mismatch -- until a test connects with a different id
# (test_heartbeat_is_answered_and_hidden_from_handlers uses "fe"
# directly, not via `Frontend`), which is exactly what surfaces this:
# the stale "test-frontend" from an earlier test collides with "fe" and
# the transport replies with {"type": "warning"} ("Multiple tabs
# detected!") instead of {"type": "initialized"}.
#
# This mirrors test_close_kinds.py's original serialization bug in one
# respect worth calling out explicitly: it is not a cubevis defect
# either. `BokehInit.get_app_context()`'s process-wide caching is
# exactly right for real usage (one BokehAppContext per running
# kernel/notebook, for the lifetime of that kernel) -- it only causes
# leakage here because this test file creates many independent,
# logically-unrelated sessions inside one long-lived process, which
# production never does. Confirmed against the real
# cubevis/bokeh/__init__.py source (BokehInit.clear_app_context(ctx)
# removes exactly the given object from that class-level list; the next
# get_app_context() call then finds the list empty and creates a
# genuinely fresh context) rather than assumed from usage sites alone.
@pytest.fixture(autouse=True)
def _reset_bokeh_app_context():
    yield
    BokehInit.clear_app_context(BokehInit.get_app_context())


# Ports are per-test so a lingering socket from a failed run can't cross-talk.
_next_port = [8790]


def _port():
    _next_port[0] += 1
    return _next_port[0]


def _new_mgr(port, **kw):
    """A CommMgr wired for websocket transport, with no app context involved."""
    on_shutdown = kw.pop("on_shutdown", None)
    mgr = CommMgr(transport_type="websocket", on_shutdown=on_shutdown)
    mgr.address = (HOST, port)
    for key, value in kw.items():
        setattr(mgr, key, value)
    return mgr


class Frontend:
    """
    Minimal stand-in for the JavaScript CommMgr.

    Performs the initialize/initialized handshake, answers the transport-level
    __ping__ heartbeat, and optionally replies to p2j requests.
    """

    def __init__(self, mgr, port, reply=True, frontend_id="test-frontend", claim=False):
        self._claim = claim
        self._mgr = mgr
        self._port = port
        self._reply = reply
        self._frontend_id = frontend_id
        self.notices = []
        self.ws = None
        self.received = []
        self.handshaked = asyncio.Event()
        self.got_request = asyncio.Event()
        self._pump = None

    async def connect(self):
        # NOTE: this handshake -- and every message on this transport,
        # below -- must go through serialize()/deserialize()
        # (cubevis.utils._conversion), not raw json.dumps()/json.loads().
        # _low_level_transport.py runs every incoming message through
        # deserialize(), which expects serialize()'s own Bokeh-derived
        # wire format (every dict wrapped as e.g. {"type":"map",
        # "entries":[...]}), not plain JSON. A raw dict fails as soon as
        # any of its own keys collide with a name Bokeh's wire format
        # reserves for its outer envelope ("id" for a Ref, "type" for
        # its own structural tags) -- this handshake's "id" key is
        # exactly that collision, raising
        # bokeh.core.serialization.UnknownReferenceError, not a sign of
        # anything actually wrong in cubevis.
        self.ws = await websockets.connect(f"ws://{HOST}:{self._port}")
        await self.ws.send(serialize({
            "id": "initialize",
            "direction": "j2p",
            "frontend_id": self._frontend_id,
            "claim": self._claim,
            "backend_id": None,
            "comm_mgr_id": self._mgr.comm_mgr_id,
        }))
        ack = deserialize(await self.ws.recv())
        assert ack.get("type") == "initialized", f"unexpected handshake reply: {ack}"
        self.handshaked.set()
        self._pump = asyncio.create_task(self._run())
        return self

    async def _run(self):
        try:
            async for raw in self.ws:
                msg = deserialize(raw)

                if msg.get("type") == "__ping__":
                    await self.ws.send(serialize({
                        "type": "__pong__", "seq": msg.get("seq"),
                    }))
                    continue
                if msg.get("type") == "__superseded__":
                    self.notices.append(msg)
                    continue

                if msg.get("direction") == "p2j":
                    self.received.append(msg)
                    self.got_request.set()
                    if self._reply:
                        await self.ws.send(serialize({
                            "comm_id": msg["comm_id"],
                            "message_id": msg["message_id"],
                            "request_id": msg["request_id"],
                            "direction": "p2j",
                            "message": {"ok": True},
                        }))
        except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
            pass

    async def suspend(self):
        """Kill the TCP connection outright -- no close frame, as on sleep."""
        if self._pump:
            self._pump.cancel()
        try:
            self.ws.transport.abort()
        except AttributeError:          # older/newer websockets internals
            await self.ws.close()

    async def close(self):
        if self._pump:
            self._pump.cancel()
        try:
            await self.ws.close()
        except Exception:
            pass


# ======================================================================
# The regression itself
# ======================================================================

@pytest.mark.asyncio
async def test_disconnect_does_not_shut_down_the_application():
    """
    A dropped connection must not invoke on_shutdown.

    This is the regression: on_shutdown resolves __result_future, which exits
    the `async with websockets.serve(...)` block and closes the listening
    socket the frontend is about to reconnect to.
    """
    port = _port()
    shutdowns = []
    closes = []

    mgr = _new_mgr(port, on_shutdown=lambda reason=None, description="": shutdowns.append(reason))
    mgr.set_connection_closed_callback(lambda r, d: closes.append((r, d)))
    mgr.open("data")

    server = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)
    try:
        fe = await Frontend(mgr, port).connect()
        await asyncio.sleep(0.2)
        assert mgr.state == AppState.RUNNING

        await fe.suspend()
        await asyncio.sleep(1.5)

        assert shutdowns == [], "on_shutdown fired on a transient disconnect"
        assert len(closes) == 1, "connection-closed callback did not fire"
        assert mgr.state != AppState.STOPPED
        assert server.is_serving(), "listener closed; nothing left to reconnect to"
        assert mgr.connection_generation == 1
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_inflight_request_is_replayed_after_reconnect():
    """
    A request that was in flight when the socket died is re-sent once the
    frontend returns, and its callback fires exactly once.
    """
    port = _port()
    callbacks = []

    mgr = _new_mgr(port)
    comm = mgr.open("data")

    server = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)
    try:
        # Frontend that receives but never replies -> request stays in flight.
        fe1 = await Frontend(mgr, port, reply=False).connect()
        await asyncio.sleep(0.2)

        await comm.send("update", {"payload": 1}, callback=callbacks.append)
        await asyncio.wait_for(fe1.got_request.wait(), timeout=5)

        assert len(mgr._pending_requests) == 1
        assert "data" in mgr._pending

        await fe1.suspend()
        await asyncio.sleep(1.5)

        assert not mgr._pending, "comm left wedged in pending state"
        assert not mgr._pending_requests
        assert len(mgr._send_queue["data"]) == 1, "in-flight request was not requeued"
        assert mgr._send_queue["data"][0][1] == {"payload": 1}
        assert callbacks == [], "callback fired before the reply arrived"

        # Reconnect with a frontend that does reply.
        fe2 = await Frontend(mgr, port, reply=True).connect()
        await asyncio.sleep(0.6)

        assert mgr.state == AppState.RUNNING
        assert len(fe2.received) == 1, "replayed request did not arrive"
        assert fe2.received[0]["message"] == {"payload": 1}
        assert callbacks == [{"ok": True}], "callback did not fire exactly once"
        assert len(mgr._send_queue["data"]) == 0

        await fe2.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_resend_disabled_notifies_callback_instead():
    """With resend_inflight_on_reconnect False the callback gets an error."""
    port = _port()
    callbacks = []

    mgr = _new_mgr(port, resend_inflight_on_reconnect=False)
    comm = mgr.open("data")

    server = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)
    try:
        fe = await Frontend(mgr, port, reply=False).connect()
        await asyncio.sleep(0.2)
        await comm.send("update", {"payload": 9}, callback=callbacks.append)
        await asyncio.wait_for(fe.got_request.wait(), timeout=5)

        await fe.suspend()
        await asyncio.sleep(1.5)

        assert len(callbacks) == 1 and "error" in callbacks[0]
        assert len(mgr._send_queue["data"]) == 0, "message was replayed despite opt-out"
    finally:
        server.close()
        await server.wait_closed()


# ======================================================================
# Transport-level keepalive
# ======================================================================

@pytest.mark.asyncio
async def test_heartbeat_is_answered_and_hidden_from_handlers():
    """
    __ping__ is answered with __pong__ by the transport and never reaches the
    application. The frontend needs this because browsers cannot send
    WebSocket control frames from JavaScript.
    """
    port = _port()
    seen = []

    mgr = _new_mgr(port)
    comm = mgr.open("data")
    comm.register("noop", lambda m: seen.append(m) or {})

    server = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)
    try:
        ws = await websockets.connect(f"ws://{HOST}:{port}")
        await ws.send(serialize({
            "id": "initialize", "direction": "j2p",
            "frontend_id": "fe", "backend_id": None,
            "comm_mgr_id": mgr.comm_mgr_id,
        }))
        assert deserialize(await ws.recv())["type"] == "initialized"
        await asyncio.sleep(0.2)

        await ws.send(serialize({"type": "__ping__", "seq": 42}))
        pong = deserialize(await asyncio.wait_for(ws.recv(), timeout=5))

        assert pong["type"] == "__pong__"
        assert pong["seq"] == 42
        assert seen == [], "heartbeat leaked into the application"
        assert mgr.state == AppState.RUNNING

        await ws.close()
    finally:
        server.close()
        await server.wait_closed()


# ======================================================================
# Reconnect watchdog (opt-in)
# ======================================================================

@pytest.mark.asyncio
async def test_watchdog_shuts_down_when_nobody_returns():
    port = _port()
    shutdowns = []

    mgr = _new_mgr(port, reconnect_timeout=1.0,
                   on_shutdown=lambda reason=None, description="": shutdowns.append(reason))
    mgr.open("data")

    server = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)
    try:
        fe = await Frontend(mgr, port).connect()
        await asyncio.sleep(0.2)
        await fe.suspend()

        # transport.abort() sends a TCP RST, so the server sees the drop
        # essentially at once and the 1.0s watchdog starts right away.
        # Sample well inside that window, then well outside it.
        await asyncio.sleep(0.5)
        assert shutdowns == [], "watchdog fired early"

        await asyncio.sleep(1.5)
        assert len(shutdowns) == 1, "watchdog never fired"
        assert mgr.state == AppState.STOPPED
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_watchdog_stands_down_on_reconnect():
    port = _port()
    shutdowns = []

    mgr = _new_mgr(port, reconnect_timeout=1.5,
                   on_shutdown=lambda reason=None, description="": shutdowns.append(reason))
    mgr.open("data")

    server = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)
    try:
        fe1 = await Frontend(mgr, port).connect()
        await asyncio.sleep(0.2)
        await fe1.suspend()

        await asyncio.sleep(0.8)
        fe2 = await Frontend(mgr, port).connect()
        await asyncio.sleep(2.5)                # past the original deadline

        assert shutdowns == [], "watchdog fired despite a successful reconnect"
        assert mgr.state == AppState.RUNNING
        await fe2.close()
    finally:
        server.close()
        await server.wait_closed()


# ======================================================================
# Real shutdown still works
# ======================================================================

@pytest.mark.asyncio
async def test_requested_shutdown_still_invokes_on_shutdown():
    port = _port()
    shutdowns = []

    mgr = _new_mgr(port, on_shutdown=lambda reason=None, description="": shutdowns.append(reason))
    mgr.open("data")

    server = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)
    try:
        fe = await Frontend(mgr, port).connect()
        await asyncio.sleep(0.2)

        mgr.request_shutdown("user pressed done")
        mgr._shutdown_event.set()
        await asyncio.sleep(0.8)

        assert len(shutdowns) == 1, "on_shutdown was not called for a real shutdown"
        assert shutdowns[0] == ShutdownReason.REQUESTED
        assert mgr.state == AppState.STOPPED

        await fe.close()
    finally:
        server.close()
        await server.wait_closed()


# ======================================================================
# A failed handshake is that attempt's failure, not the session's
# ======================================================================
#
# visplot user testing, 2026-11-20: a browser attempt closed while still
# CONNECTING (ConnectionClosedOK 1001, "closed before the connection is
# established") made process_messages report a fatal error and shut the
# session down -- the reader was closed, and the window, which went on
# working on its next attempt, got "MSv2Backend is not open" for every
# later request.  Transport review 2026-11-16, WS-1 / WS-2 / WS-3.

async def _abandoned_attempt(port, *, say=None):
    """Connect, optionally send *say*, then close without completing the
    handshake -- what a browser does when it abandons an attempt."""
    ws = await websockets.connect(f"ws://{HOST}:{port}")
    if say is not None:
        await ws.send(say)
    await ws.close()


@pytest.mark.asyncio
async def test_abandoned_handshake_does_not_shut_down():
    port = _port()
    shutdowns = []
    mgr = _new_mgr(port, on_shutdown=lambda reason=None, description="": shutdowns.append(reason))
    mgr.open("data")
    server = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)
    try:
        await _abandoned_attempt(port)                       # closed before any message
        await _abandoned_attempt(port, say=serialize({"id": "hello", "direction": "j2p"}))
        await asyncio.sleep(0.5)
        assert shutdowns == [], "a failed handshake shut the session down"
        assert mgr.state not in (AppState.ERROR, AppState.STOPPED)

        # ... and a proper frontend still gets in afterwards.
        fe = await Frontend(mgr, port).connect()
        await asyncio.sleep(0.2)
        assert mgr.state == AppState.RUNNING
        await fe.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_failed_attempt_leaves_the_live_connection_alone():
    """A stray attempt must not retire the working transport (WS-2)."""
    port = _port()
    shutdowns = []
    mgr = _new_mgr(port, on_shutdown=lambda reason=None, description="": shutdowns.append(reason))
    comm = mgr.open("data")
    server = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)
    try:
        fe = await Frontend(mgr, port).connect()
        await asyncio.sleep(0.2)
        live = mgr._transport
        generation = mgr.connection_generation

        await _abandoned_attempt(port)
        await asyncio.sleep(0.5)

        assert shutdowns == []
        assert mgr._transport is live, "the live transport was replaced by a failed attempt"
        assert mgr.connection_generation == generation
        assert mgr.state == AppState.RUNNING

        # The live connection still carries requests.
        await comm.send("update", {"hello": 1}, callback=lambda reply: None)
        await asyncio.wait_for(fe.got_request.wait(), 3)
        await fe.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_silent_peer_times_out_of_the_handshake():
    """A client that connects and says nothing cannot hold the slot."""
    port = _port()
    shutdowns = []
    mgr = _new_mgr(port, on_shutdown=lambda reason=None, description="": shutdowns.append(reason))
    mgr._handshake_timeout = 0.5
    mgr.open("data")
    server = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)
    try:
        silent = await websockets.connect(f"ws://{HOST}:{port}")
        await asyncio.sleep(1.0)
        assert shutdowns == [] and mgr._transport is None
        fe = await Frontend(mgr, port).connect()
        await asyncio.sleep(0.2)
        assert mgr.state == AppState.RUNNING
        await fe.close()
        await silent.close()
    finally:
        server.close()
        await server.wait_closed()


# ======================================================================
# A replayed request does not run its handler twice (review RP-1)
# ======================================================================
#
# The browser replays the request it had in flight when a connection
# ended, with the same request_id.  Python may already have run it (only
# the reply was lost) or still be running it.

class _Caller:
    """A frontend that sends j2p requests and collects the replies."""

    def __init__(self, mgr, port):
        self._mgr, self._port = mgr, port
        self.ws = None
        self.replies = []
        self._pump = None
        self.got = asyncio.Event()

    async def connect(self):
        self.ws = await websockets.connect(f"ws://{HOST}:{self._port}")
        await self.ws.send(serialize({
            "id": "initialize", "direction": "j2p", "frontend_id": "test-frontend",
            "backend_id": None, "comm_mgr_id": self._mgr.comm_mgr_id,
        }))
        assert deserialize(await self.ws.recv()).get("type") == "initialized"
        self._pump = asyncio.create_task(self._run())
        return self

    async def _run(self):
        try:
            async for raw in self.ws:
                msg = deserialize(raw)
                if msg.get("type") == "__ping__":
                    await self.ws.send(serialize({"type": "__pong__", "seq": msg.get("seq")}))
                elif msg.get("request_id"):
                    self.replies.append(msg)
                    self.got.set()
        except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
            pass

    async def request(self, request_id, message):
        self.got.clear()
        await self.ws.send(serialize({
            "comm_id": "data", "message_id": "act", "request_id": request_id,
            "direction": "j2p", "message": message,
        }))

    async def suspend(self):
        self._pump.cancel()
        try:
            self.ws.transport.abort()
        except AttributeError:
            await self.ws.close()

    async def close(self):
        if self._pump:
            self._pump.cancel()
        try:
            await self.ws.close()
        except Exception:
            pass


def _counting_mgr(port, *, delay=0.0, gate=None):
    mgr = _new_mgr(port)
    calls = []

    async def act(message):
        calls.append(message)
        if gate is not None:
            await gate.wait()
        if delay:
            await asyncio.sleep(delay)
        return {"n": len(calls), "echo": message}

    mgr.open("data").register("act", act)
    return mgr, calls


@pytest.mark.asyncio
async def test_replay_of_an_answered_request_resends_the_reply():
    port = _port()
    mgr, calls = _counting_mgr(port)
    server = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)
    try:
        fe = await _Caller(mgr, port).connect()
        await fe.request("r1", {"step": "next"})
        await asyncio.wait_for(fe.got.wait(), 3)
        await fe.request("r1", {"step": "next"})            # the replay
        await asyncio.wait_for(fe.got.wait(), 3)
        assert len(calls) == 1, "the handler ran again for a replayed request"
        assert [r["message"] for r in fe.replies] == [{"n": 1, "echo": {"step": "next"}}] * 2

        await fe.request("r2", {"step": "next"})            # a new request runs
        await asyncio.wait_for(fe.got.wait(), 3)
        assert len(calls) == 2 and fe.replies[-1]["message"]["n"] == 2
        await fe.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_replay_while_the_original_runs_is_dropped():
    port = _port()
    gate = asyncio.Event()
    mgr, calls = _counting_mgr(port, gate=gate)
    server = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)
    try:
        fe = await _Caller(mgr, port).connect()
        await fe.request("r1", {"x": 1})
        await asyncio.sleep(0.2)
        await fe.request("r1", {"x": 1})                    # replay, original still running
        await asyncio.sleep(0.2)
        gate.set()
        await asyncio.wait_for(fe.got.wait(), 3)
        await asyncio.sleep(0.3)
        assert len(calls) == 1
        assert len(fe.replies) == 1, "the dropped copy got a reply of its own"
        await fe.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_reply_lost_on_a_drop_reaches_the_replay_once():
    """The reconnect case itself: the reply to r1 had nowhere to go; the
    page comes back, replays r1, and gets exactly one reply without the
    handler running again."""
    port = _port()
    mgr, calls = _counting_mgr(port, delay=0.4)
    server = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)
    try:
        fe1 = await _Caller(mgr, port).connect()
        await fe1.request("r1", {"flag": "apply"})
        await asyncio.sleep(0.1)
        await fe1.suspend()                                 # drops before the reply
        await asyncio.sleep(1.0)                            # handler finishes meanwhile

        fe2 = await _Caller(mgr, port).connect()
        await fe2.request("r1", {"flag": "apply"})
        await asyncio.wait_for(fe2.got.wait(), 3)
        await asyncio.sleep(0.3)
        assert len(calls) == 1, "flag apply ran twice"
        assert len(fe2.replies) == 1 and fe2.replies[0]["message"]["n"] == 1
        await fe2.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_cancelled_handler_is_not_mistaken_for_a_running_one():
    """A handler that never finished (cancelled) leaves no record, so the
    replay runs it instead of being dropped forever."""
    mgr, calls = _counting_mgr(_port(), gate=asyncio.Event())
    msg = {"comm_id": "data", "message_id": "act", "request_id": "r1",
           "direction": "j2p", "message": {}}
    task = asyncio.ensure_future(mgr._handle_request(msg))
    await asyncio.sleep(0.1)
    assert mgr._last_request["data"] == ["r1", None]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "data" not in mgr._last_request


@pytest.mark.asyncio
async def test_python_replay_keeps_its_request_id():
    """Python -> browser half of RP-1: the request replayed after a
    reconnect carries its first request_id, so the page can tell."""
    port = _port()
    mgr = _new_mgr(port)
    comm = mgr.open("data")
    server = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)
    try:
        fe1 = await Frontend(mgr, port, reply=False).connect()
        await comm.send("update", {"payload": 1}, callback=lambda r: None)
        await asyncio.wait_for(fe1.got_request.wait(), 5)
        first = fe1.received[0]["request_id"]
        await fe1.suspend()
        await asyncio.sleep(1.0)
        fe2 = await Frontend(mgr, port).connect()
        await asyncio.wait_for(fe2.got_request.wait(), 5)
        assert fe2.received[0]["request_id"] == first
        await fe2.close()
    finally:
        server.close()
        await server.wait_closed()


# ======================================================================
# Newest tab takes over (review WS-2)
# ======================================================================

@pytest.mark.asyncio
async def test_another_tab_supersedes_the_first():
    port = _port()
    shutdowns = []
    mgr = _new_mgr(port, on_shutdown=lambda reason=None, description="": shutdowns.append(reason))
    comm = mgr.open("data")
    server = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)
    try:
        tab1 = await Frontend(mgr, port, frontend_id="page-1").connect()
        await asyncio.sleep(0.2)
        tab2 = await Frontend(mgr, port, frontend_id="page-2").connect()
        await asyncio.sleep(0.5)

        assert [n["type"] for n in tab1.notices] == ["__superseded__"]
        assert tab1.ws.close_code == _comm_mgr_module.SUPERSEDED_CLOSE_CODE
        assert tab2.notices == []
        assert shutdowns == [] and mgr.state == AppState.RUNNING
        await comm.send("update", {"to": "tab2"}, callback=lambda r: None)
        await asyncio.wait_for(tab2.got_request.wait(), 3)
        assert tab1.received == []

        # The first page waking and reconnecting on its own is turned away:
        # the tabs must not trade the session back and forth.
        woke = await Frontend(mgr, port, frontend_id="page-1").connect()
        await asyncio.sleep(0.5)
        assert [n["type"] for n in woke.notices] == ["__superseded__"]
        assert tab2.notices == [] and mgr._transport is not None
        assert mgr._transport.frontend_id == "page-2"

        # "Use this tab" (claim): the first page takes it back.
        tab1b = await Frontend(mgr, port, frontend_id="page-1", claim=True).connect()
        await asyncio.sleep(0.5)
        assert [n["type"] for n in tab2.notices] == ["__superseded__"]
        assert tab1b.notices == [] and mgr._transport.frontend_id == "page-1"
        await tab1b.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_same_page_reconnecting_is_not_superseded():
    port = _port()
    mgr = _new_mgr(port)
    mgr.open("data")
    server = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)
    try:
        a = await Frontend(mgr, port, frontend_id="page-1").connect()
        await asyncio.sleep(0.2)
        b = await Frontend(mgr, port, frontend_id="page-1").connect()
        await asyncio.sleep(0.5)
        assert a.notices == [] and a.ws.close_code != _comm_mgr_module.SUPERSEDED_CLOSE_CODE
        await b.close()
    finally:
        server.close()
        await server.wait_closed()


# ======================================================================
# Waiting forever must not be silent
# ======================================================================
#
# A browser tab that goes away without either a close frame or a
# __goodbye__ is indistinguishable from a suspended laptop, so with
# reconnect_timeout=None (what VisibilityPlotter sets) the session
# waits indefinitely -- by design.  What was wrong was that it did so
# silently: from the terminal, "tab closed, nothing happened" looked
# like a hang.  These pin the warning that now says why.

class _WarnCapture(logging.Handler):
    """Collects WARNING+ messages from CommMgr's own logger.  A plain
    handler rather than pytest's caplog so the standalone runner below
    (which has no fixtures) exercises these too."""
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


async def _drop_and_watch(kind, **mgr_kw):
    """Connect a frontend, end the connection as `kind`, and return every
    warning CommMgr logged while that played out."""
    port = _port()
    mgr = _new_mgr(port, **mgr_kw)
    mgr.open("data")
    cap = _WarnCapture()
    _comm_mgr_module.logger.addHandler(cap)
    server = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)
    try:
        fe = await Frontend(mgr, port).connect()
        await asyncio.sleep(0.2)
        if kind == "abrupt":
            await fe.suspend()                 # RST, no close frame, no goodbye
        else:
            await fe.close()                   # proper close frame
        await asyncio.sleep(1.0)
    finally:
        _comm_mgr_module.logger.removeHandler(cap)
        server.close()
        await server.wait_closed()
    return cap.messages


@pytest.mark.asyncio
async def test_abrupt_disconnect_with_no_timeout_says_it_is_waiting():
    msgs = await _drop_and_watch("abrupt")          # reconnect_timeout defaults to None
    hits = [m for m in msgs if "without a close message" in m]
    assert len(hits) == 1, f"expected exactly one warning, got {msgs!r}"
    assert "Ctrl-C" in hits[0] and "reconnect_timeout" in hits[0]


@pytest.mark.asyncio
async def test_abrupt_disconnect_with_a_timeout_does_not_warn():
    # A timeout means the session WILL end on its own; "waiting
    # indefinitely" would be untrue.
    msgs = await _drop_and_watch("abrupt", reconnect_timeout=30.0)
    assert not [m for m in msgs if "without a close message" in m], msgs


@pytest.mark.asyncio
async def test_clean_close_does_not_warn():
    # A close frame means the tab was deliberately closed; the grace
    # period ends the session shortly, so there is nothing to explain.
    msgs = await _drop_and_watch("clean", reconnect_grace_period=30.0)
    assert not [m for m in msgs if "without a close message" in m], msgs


# ----------------------------------------------------------------------
# Standalone runner, so this works without pytest-asyncio installed.
# ----------------------------------------------------------------------
if __name__ == "__main__":
    tests = [obj for name, obj in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            asyncio.run(test())
            print(f"  PASS  {test.__name__}")
        except Exception as exc:                                  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {test.__name__}: {exc}")
        finally:
            # Same cleanup the pytest fixture above does -- this
            # runner gets no pytest fixtures at all, so without this
            # the leaked BokehAppContext (see _reset_bokeh_app_context's
            # docstring) would accumulate across this loop exactly the
            # same way it does under pytest.
            BokehInit.clear_app_context(BokehInit.get_app_context())
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
