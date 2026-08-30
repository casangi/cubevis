"""
Chunk 1, Task 2 + definition-of-done bullet 1. Unchanged by Chunk 1c.

With one CommMgr in the 'default' role and its peer in the 'mirror' role,
both a request/response round trip and an unsolicited push round trip
should complete correctly over a loopback test-double transport -- no
real sshpyk/cluster connection required to validate the multiplexing
core.
"""
import asyncio

import pytest

from cubevis.bokeh.transport import CommMgr
from cubevis.remote.testing import wire_loopback_pair


@pytest.mark.asyncio
async def test_request_response_round_trip_mirrored_roles():
    # side_a: P_local's kernel-facing CommMgr, mirrored.
    # side_b: kernel-side CommMgr, default (unchanged from today).
    side_a = CommMgr(role=CommMgr.ROLE_MIRROR)
    side_b = CommMgr(role=CommMgr.ROLE_DEFAULT)
    wire_loopback_pair(side_a, side_b)

    comm_a = side_a.open("query")
    comm_b = side_b.open("query")

    # side_b answers queries dispatched to it, exactly like a today's
    # browser-facing j2p handler would.
    def handle_query(message):
        return {"echo": message, "answered_by": "kernel"}

    comm_b.register("do_query", handle_query)

    reply_box = {}

    def on_reply(message):
        reply_box["reply"] = message

    await comm_a.send("do_query", {"x": 1}, callback=on_reply)
    await asyncio.sleep(0.1)

    assert reply_box.get("reply") == {"echo": {"x": 1}, "answered_by": "kernel"}
    # The request/response cycle should have cleared both sides' pending
    # bookkeeping -- nothing should be left wedged.
    assert side_a._pending == {}
    assert side_b._pending == {}


@pytest.mark.asyncio
async def test_unsolicited_push_round_trip_mirrored_roles():
    # Same role assignment as the request/response test, but this time
    # side_b (kernel-side, default role) pushes unprompted -- the exact
    # scenario that was silently dropped before Chunk 1's fix.
    side_a = CommMgr(role=CommMgr.ROLE_MIRROR)
    side_b = CommMgr(role=CommMgr.ROLE_DEFAULT)
    wire_loopback_pair(side_a, side_b)

    comm_a = side_a.open("progress")
    comm_b = side_b.open("progress")

    received = []
    comm_a.register("progress_update", lambda msg: received.append(msg))

    # side_a needs a handler too, since a "push" is implemented as a
    # request that expects *some* reply to release the sender's pending
    # slot (see _process_next_queued / the design doc's push description).
    comm_a.register("progress_update", lambda msg: received.append(msg))

    await comm_b.send("progress_update", {"percent": 42})
    await asyncio.sleep(0.1)

    assert received == [{"percent": 42}]

    # And the push's sender-side pending slot should have cleared too --
    # this is the second half of the original bug (a permanently wedged
    # comm), now fixed as a side effect of correct routing.
    assert side_b._pending == {}


@pytest.mark.asyncio
async def test_two_default_role_peers_still_collide_by_design():
    """
    Two same-role CommMgrs (e.g. two 'default's, or two 'mirror's) are
    NOT expected to interoperate correctly -- role mirroring only works
    when exactly one side is 'default' and the other is 'mirror'. This
    pins that down explicitly so nobody "fixes" it into silently working,
    which would just be the original bug with extra steps.
    """
    side_a = CommMgr(role=CommMgr.ROLE_MIRROR)
    side_b = CommMgr(role=CommMgr.ROLE_MIRROR)
    wire_loopback_pair(side_a, side_b)

    comm_a = side_a.open("progress")
    comm_b = side_b.open("progress")
    received = []
    comm_a.register("progress_update", lambda msg: received.append(msg))

    await comm_b.send("progress_update", {"percent": 1})
    await asyncio.sleep(0.05)

    assert received == []
    assert comm_b.comm_id in side_b._pending
