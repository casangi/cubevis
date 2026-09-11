import asyncio, json, sys
import websockets
sys.path.insert(0, ".")
from cubevis.bokeh.transport import CommMgr
from cubevis.bokeh.transport._comm_mgr import AppState
from cubevis.utils._conversion import serialize, deserialize

HOST = "127.0.0.1"
r = {}

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

    for k, v in r.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    return all(r.values())

if __name__ == "__main__":
    print("=== close classification: tab close vs reload vs suspend ===")
    sys.exit(0 if asyncio.run(main()) else 1)
