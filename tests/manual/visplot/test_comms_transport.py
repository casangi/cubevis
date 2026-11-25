"""The JupyterLab comm transport (CommsTransport) with fake kernel comms
(transport review JC-1, JC-2, JC-4; issue #35, 2026-11-24).

What was seen in JupyterLab 4.6 (headless Chromium, 2026-11-24): every
rendering of the bridge opens a kernel comm -- a page reload, a second view of
the output -- but only a page whose CommMgr sends `comm_opened` on it has an
app behind it (after a reload the visplot output does not come back while the
bridge does).  Until this change Python sent everything on the newest comm:
after a reload its messages went to a page with no app, and with a second
view open the replies to the first view's requests went to the second one.

No kernel needed: the comms are fakes with the ipykernel Comm surface used
(send, on_msg, on_close).
"""
import asyncio

import pytest
import pytest_asyncio

import cubevis.bokeh.transport._low_level_transport as llt
from cubevis.bokeh.transport._low_level_transport import CommsTransport, TransportNotConnectedError
from cubevis.utils import serialize, deserialize


class FakeComm:
    def __init__(self, name):
        self.name = name
        self.sent = []
        self._on_msg = None
        self._on_close = None
        self.closed = False

    def on_msg(self, f):
        self._on_msg = f

    def on_close(self, f):
        self._on_close = f

    def send(self, data):
        if self.closed:
            raise RuntimeError("comm closed")
        self.sent.append(deserialize(data["data"]))

    # --- the page side
    def page_sends(self, payload):
        self._on_msg({"content": {"data": {"type": "cubevis_message", "comm_mgr_id": "m",
                                           "data": serialize(payload)}}})

    def handshake(self):
        self.page_sends({"type": "comm_opened", "comm_mgr_id": "m", "frontend_ready": True})

    def close_from_page(self):
        self.closed = True
        self._on_close({})


class _Ctx:
    def add_preflight_callable(self, f):
        pass


class FakeCommManager:
    def __init__(self):
        self.targets = {}

    def register_target(self, name, f):
        self.targets[name] = f

    def unregister_target(self, name, f):
        return self.targets.pop(name)


@pytest_asyncio.fixture
async def transport(monkeypatch):
    # Async, so the transport's IOLoop.current() is the test's running loop.
    from cubevis.bokeh import BokehInit
    monkeypatch.setattr(BokehInit, "get_app_context", staticmethod(lambda: _Ctx()))
    t = CommsTransport("m")
    t._bridge = object()             # display_bridge() is not run here
    events = []
    t.on_frontend = events.append
    t.events = events
    received = []

    async def cb(msg):
        received.append(msg)

    t.set_message_callback(cb)
    t.received = received
    return t


def _open(t, name):
    c = FakeComm(name)
    t._on_comm_open(c, {})
    return c


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_comm_without_a_page_handshake_is_not_used(transport):
    a = _open(transport, "bridge only")
    assert not transport.is_connected()
    with pytest.raises(TransportNotConnectedError):
        await transport.send_message({"comm_id": "c", "message_id": "x", "request_id": "r1",
                                      "direction": "p2j", "message": {}})
    assert a.sent == []
    a.handshake()
    await _settle()
    assert transport.is_connected() and transport.events[-1] == "ready"
    await transport.send_message({"comm_id": "c", "message_id": "x", "request_id": "r1",
                                  "direction": "p2j", "message": {}})
    assert [m["request_id"] for m in a.sent] == ["r1"]
    assert transport.received == [], "the handshake is not an application message"


@pytest.mark.asyncio
async def test_a_new_comm_unconfirms_the_active_one_until_its_page_shakes_hands(transport):
    a = _open(transport, "first page")
    a.handshake()
    await _settle()
    b = _open(transport, "reload: bridge only")        # e.g. a reload
    await _settle()
    assert transport.events[-1] == "lost"
    assert not transport.is_connected(), "messages would go to a page that may be gone"
    b.handshake()                                       # (a second view would)
    await _settle()
    assert transport.events[-1] == "ready"
    await transport.send_message({"comm_id": "c", "message_id": "x", "request_id": "p1",
                                  "direction": "p2j", "message": {}})
    assert [m["request_id"] for m in b.sent] == ["p1"] and a.sent == []


@pytest.mark.asyncio
async def test_a_reply_goes_back_on_the_comm_its_request_came_on(transport):
    a = _open(transport, "first view"); a.handshake()
    b = _open(transport, "second view"); b.handshake()
    await _settle()
    a.page_sends({"comm_id": "c", "message_id": "plot", "request_id": "j1", "direction": "j2p", "message": {}})
    await _settle()
    assert [m.get("request_id") for m in transport.received] == ["j1"]
    await transport.send_message({"comm_id": "c", "message_id": "plot", "request_id": "j1",
                                  "direction": "j2p", "message": {"ok": True}})
    assert [m["request_id"] for m in a.sent] == ["j1"], "the first view's reply went elsewhere"
    assert b.sent == []
    # Python's own requests go to the newest page.
    await transport.send_message({"comm_id": "c", "message_id": "x", "request_id": "p2",
                                  "direction": "p2j", "message": {}})
    assert [m["request_id"] for m in b.sent] == ["p2"]


@pytest.mark.asyncio
async def test_closing_the_active_comm_is_a_lost_page(transport):
    a = _open(transport, "page"); a.handshake()
    await _settle()
    a.close_from_page()
    await _settle()
    assert transport.events[-1] == "lost" and not transport.is_connected()


@pytest.mark.asyncio
async def test_a_failing_send_raises_not_connected(transport):
    a = _open(transport, "page"); a.handshake()
    await _settle()
    a.closed = True                                    # gone without a close message
    with pytest.raises(TransportNotConnectedError):
        await transport.send_message({"comm_id": "c", "message_id": "x", "request_id": "p3",
                                      "direction": "p2j", "message": {}})
    await _settle()
    assert not transport.is_connected() and transport.events[-1] == "lost"


@pytest.mark.asyncio
async def test_close_ends_run_and_unregisters_the_target(transport):
    mgr = FakeCommManager()
    mgr.register_target("m", transport._on_comm_open)
    transport._comm_manager = mgr
    runner = asyncio.ensure_future(transport.run())
    await asyncio.sleep(0.05)
    assert not runner.done()
    await transport.close()
    await asyncio.wait_for(runner, 1.0)
    assert "m" not in mgr.targets


@pytest.mark.asyncio
async def test_commmgr_waits_through_a_lost_page_and_replays_on_the_next(transport):
    """CommMgr + CommsTransport: a request in flight when the page goes is sent
    again (same request_id) to the next page that shakes hands; its reply
    reaches the callback."""
    from cubevis.bokeh.transport import CommMgr
    mgr = CommMgr(transport_type="jupyter")
    mgr._transport = transport
    a = _open(transport, "page 1")
    await mgr.initialize()
    a.handshake()
    await _settle()
    comm = mgr.open("data")
    got = []
    await comm.send("update", {"n": 1}, callback=got.append)
    sent_a = [m for m in a.sent if m.get("message_id") == "update"]
    assert len(sent_a) == 1
    rid = sent_a[0]["request_id"]

    b = _open(transport, "page 2")                      # page 1 reloads
    await _settle()
    assert "data" not in mgr._pending, "the request still waits on the old page"
    await comm.send("update", {"n": 2}, callback=got.append)   # held, not lost
    assert not [m for m in b.sent if m.get("message_id") == "update"]

    b.handshake()
    await _settle()
    sent_b = [m for m in b.sent if m.get("message_id") == "update"]
    assert [m["request_id"] for m in sent_b][:1] == [rid], "not replayed with its request_id"
    b.page_sends({"comm_id": "data", "message_id": "update", "request_id": rid,
                  "direction": "p2j", "message": {"ok": 1}})
    await _settle()
    sent_b = [m for m in b.sent if m.get("message_id") == "update"]
    assert len(sent_b) == 2, "the held request did not follow"
    b.page_sends({"comm_id": "data", "message_id": "update", "request_id": sent_b[1]["request_id"],
                  "direction": "p2j", "message": {"ok": 2}})
    await _settle()
    assert got == [{"ok": 1}, {"ok": 2}]
