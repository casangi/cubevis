import asyncio, json, logging, sys
import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
sys.path.insert(0, ".")
from cubevis.bokeh.transport import CommMgr
from cubevis.bokeh.transport._comm_mgr import AppState
from cubevis.utils._conversion import serialize, deserialize

HOST = "127.0.0.1"
r = {}


class _RecordCapture(logging.Handler):
    """Captures log messages from one logger for a direct assertion,
    rather than only inferring "no uncaught exception happened" from
    the absence of a crash -- _low_level_transport.py's outer message
    loop swallows any exception a handler raises (see its own run()
    docstring), so a regression here would NOT crash this test; it
    would only reappear as an unwanted "Error processing message" log
    record, exactly like the field report's own duplicate lines.
    """
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())

# NOTE: this handshake payload must go through serialize()/deserialize()
# (cubevis.utils._conversion), not raw json.dumps()/json.loads() --
# _low_level_transport.py runs every incoming message through
# deserialize(), which expects serialize()'s own wire format, not
# plain JSON. A bare dict with a top-level "id" key (as this handshake
# has) collides with Bokeh's own reserved reference shape ({"id": ...}
# means "look up a previously-registered object by this id" in
# Bokeh's serialization protocol) and fails with
# bokeh.core.serialization.UnknownReferenceError, NOT because
# anything in cubevis itself is broken.
async def hs(ws, mgr):
    await ws.send(serialize({"id":"initialize","direction":"j2p","frontend_id":"fe",
                              "backend_id":None,"comm_mgr_id":mgr.comm_mgr_id}))
    assert deserialize(await ws.recv())["type"] == "initialized"

async def scenario(port, kind, grace, timeout, reconnect_after=None):
    """kind: 'tab_close' (clean 1001) | 'suspend' (RST, no close frame)"""
    shut = []
    mgr = CommMgr(transport_type="websocket",
                  on_shutdown=lambda reason=None, description="": shut.append(description))
    mgr.address = (HOST, port)
    mgr.reconnect_grace_period = grace
    mgr.reconnect_timeout = timeout
    mgr.open("d")
    srv = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)

    ws = await websockets.connect(f"ws://{HOST}:{port}")
    await hs(ws, mgr)
    await asyncio.sleep(0.3)

    if kind == "tab_close":
        await ws.close(code=1001, reason="going away")   # what a browser sends
    else:
        ws.transport.abort()                             # suspended laptop
    await asyncio.sleep(0.4)

    if reconnect_after is not None:
        await asyncio.sleep(reconnect_after)
        ws2 = await websockets.connect(f"ws://{HOST}:{port}")
        await hs(ws2, mgr)
        await asyncio.sleep(0.3)

    return mgr, shut, srv


async def _scenario_reply_races_with_close(port, *, handler_raises):
    """A handler's reply-send races the client having already gone.

    Deterministic, not timing-dependent: the handler itself closes the
    client side (from inside the handler, before returning/raising), so
    by the time _handle_request tries to send back a reply, the
    connection is guaranteed already closed -- every run, not just
    sometimes. handler_raises=False reproduces the exact bug from the
    2026-09 field report (a successful handler whose reply-send raced a
    tab close); handler_raises=True covers the adjacent case -- a
    genuine handler bug whose *error* reply also races the same close.

    Returns (mgr, transport_log_records) -- the latter captured from
    _low_level_transport's logger specifically to catch the field
    report's second symptom (a "received 1001..." exception escaping
    _handle_request uncaught and resurfacing there as "Error processing
    message"), which would NOT crash this test on its own -- see
    _RecordCapture's docstring.
    """
    mgr = CommMgr(transport_type="websocket")
    mgr.address = (HOST, port)
    client_ws = {}

    async def handle(msg):
        await client_ws["ws"].close(code=1001, reason="going away")
        await asyncio.sleep(0.05)  # let the close frame land server-side
        if handler_raises:
            raise ValueError("a genuine handler bug")
        return {"ok": True}

    comm = mgr.open("d")
    comm.register("go", handle)
    srv = await websockets.serve(mgr.process_messages, HOST, port, ping_interval=None)

    ws = await websockets.connect(f"ws://{HOST}:{port}")
    client_ws["ws"] = ws
    await hs(ws, mgr)

    transport_logger = logging.getLogger("cubevis.bokeh.transport._low_level_transport")
    cap = _RecordCapture()
    transport_logger.addHandler(cap)
    try:
        await ws.send(serialize({
            "comm_id": comm.comm_id, "message_id": "go", "request_id": "r1",
            "message": {}, "direction": "j2p",
        }))
        await asyncio.sleep(0.3)
    finally:
        transport_logger.removeHandler(cap)

    srv.close(); await srv.wait_closed()
    return mgr, cap.records

async def main():
    # 1. tab closed for good -> shuts down after the grace period
    mgr, shut, srv = await scenario(8850, "tab_close", grace=1.0, timeout=None)
    await asyncio.sleep(0.4)
    r["tab close: still alive inside grace"] = (shut == [] and mgr.state != AppState.STOPPED)
    await asyncio.sleep(1.2)
    r["tab close: shut down after grace"] = (len(shut) == 1 and mgr.state == AppState.STOPPED)
    r["tab close: reason mentions close"] = (shut and "closed the connection" in shut[0])
    srv.close(); await srv.wait_closed()

    # 2. tab reloaded -> comes back inside the grace period, session survives
    mgr, shut, srv = await scenario(8851, "tab_close", grace=2.0, timeout=None,
                                    reconnect_after=0.5)
    await asyncio.sleep(2.5)
    r["tab reload: survived"] = (shut == [] and mgr.state == AppState.RUNNING)
    r["tab reload: generation bumped"] = (mgr.connection_generation == 1)
    srv.close(); await srv.wait_closed()

    # 3. laptop suspend -> NOT subject to the grace period, waits indefinitely
    mgr, shut, srv = await scenario(8852, "suspend", grace=1.0, timeout=None)
    await asyncio.sleep(2.5)
    r["suspend: ignores grace period"] = (shut == [] and mgr.state != AppState.STOPPED)
    srv.close(); await srv.wait_closed()

    # 4. suspend with a finite reconnect_timeout -> uses that, not the grace
    mgr, shut, srv = await scenario(8853, "suspend", grace=0.5, timeout=2.0)
    await asyncio.sleep(1.0)
    r["suspend: alive past grace"] = (shut == [])
    await asyncio.sleep(2.0)
    r["suspend: shut down at reconnect_timeout"] = (len(shut) == 1)
    srv.close(); await srv.wait_closed()

    # 5. suspend then wake inside the window -> survives
    mgr, shut, srv = await scenario(8854, "suspend", grace=0.5, timeout=3.0,
                                    reconnect_after=1.0)
    await asyncio.sleep(3.0)
    r["suspend+wake: survived"] = (shut == [] and mgr.state == AppState.RUNNING)
    srv.close(); await srv.wait_closed()

    # 6. reply races with the client already gone (2026-09 field report):
    # a handler SUCCEEDS, but by the time _handle_request sends the
    # reply the tab has already closed. Must be treated as the benign
    # "peer went away" case _send_request already handles this way, not
    # reported as an error, and must not leak an uncaught exception up
    # to _low_level_transport's message loop either.
    mgr, transport_records = await _scenario_reply_races_with_close(
        8855, handler_raises=False)
    r["reply races with close: not reported as an error"] = not any(
        isinstance(e, (ConnectionClosedError, ConnectionClosedOK))
        for e in mgr._errors
    )
    r["reply races with close: no uncaught exception in transport loop"] = not any(
        "Error processing message" in msg for msg in transport_records
    )

    # 7. a genuine handler bug whose ERROR reply also races the client
    # already being gone. The real bug must still be recorded (exactly
    # once) -- but the failed attempt to deliver that error to an
    # already-gone client must not itself raise uncaught or add a
    # second, unrelated entry to self._errors.
    mgr, transport_records = await _scenario_reply_races_with_close(
        8856, handler_raises=True)
    r["error-reply races with close: real bug recorded exactly once"] = (
        len(mgr._errors) == 1 and isinstance(mgr._errors[0], ValueError)
    )
    r["error-reply races with close: no uncaught exception in transport loop"] = not any(
        "Error processing message" in msg for msg in transport_records
    )

    for k, v in r.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    return all(r.values())

if __name__ == "__main__":
    print("=== close classification: tab close vs reload vs suspend ===")
    sys.exit(0 if asyncio.run(main()) else 1)
