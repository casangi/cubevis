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
import sys

import pytest
import websockets

from cubevis.bokeh import BokehInit
from cubevis.bokeh.transport import CommMgr
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

    def __init__(self, mgr, port, reply=True):
        self._mgr = mgr
        self._port = port
        self._reply = reply
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
            "frontend_id": "test-frontend",
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
