"""
Chunk 1, Task 1 -- reproduce the bug described in the design doc (Section 3)
before touching any working code.

Scenario: two `CommMgr` instances, both running in today's only mode (no
role/direction parameter exists yet), wired directly together with a
loopback transport double -- standing in for "kernel-side CommMgr" and
"P_local's kernel-facing CommMgr" from the design doc, with no real
`sshpyk`/websocket involved.

Today, `CommMgr.send()`/`_send_immediate()` hardcode the outgoing
`'direction'` tag to `'p2j'` regardless of which side is sending, and
`_route_message()` treats *any* incoming `'p2j'`-tagged message as "a
response to a request I made" -- looking it up in `_pending_requests` by
`request_id`. A push has no corresponding pending request on the
*receiving* side (it wasn't asked for), so it is logged as "unknown
request" and silently dropped.

This test pins that failure mode down concretely: side B sends an
unsolicited push to side A; side A's registered handler for that
message_id never fires.
"""
import asyncio
import logging

import pytest

from cubevis.bokeh.transport import CommMgr, Comm
from cubevis.remote.testing import wire_loopback_pair


@pytest.mark.asyncio
async def test_unsolicited_push_is_misrouted_and_dropped(caplog):
    # side_a: stand-in for P_local's kernel-facing CommMgr (today: no way
    #         to distinguish it from side_b -- that's the bug).
    # side_b: stand-in for the kernel-side CommMgr, sending an unsolicited
    #         progress push with no prior request from side_a.
    side_a = CommMgr()
    side_b = CommMgr()
    wire_loopback_pair(side_a, side_b, name_a="P_local(kernel-facing)", name_b="kernel-side")

    comm_a = side_a.open("progress")
    comm_b = side_b.open("progress")
    assert comm_a.comm_id == comm_b.comm_id, (
        "the loopback pair test helper wires same-named comms 1:1; if this "
        "assert ever fires, the test setup (not CommMgr) is the problem"
    )

    received = []

    def on_progress(msg):
        received.append(msg)

    # side_a registers a handler, exactly as P_local would to receive
    # progress pushes forwarded from the kernel side.
    comm_a.register("progress_update", on_progress)

    # side_b (the "kernel") pushes an unsolicited progress update. Nothing
    # on side_b's end is waiting for a *reply* to this -- it's a push, not
    # a request side_b itself made.
    with caplog.at_level(logging.WARNING):
        await comm_b.send("progress_update", {"percent": 42})
        # Give the loopback delivery (scheduled via call_soon) a chance to run.
        await asyncio.sleep(0.05)

    # The bug: the handler never fires, because the message arrived tagged
    # 'p2j' (side_b's own hardcoded outgoing tag) and side_a's
    # _route_message() treats incoming 'p2j' as a response to ITS OWN
    # pending request -- there isn't one, so it's dropped.
    assert received == [], (
        "if this fails, the misrouting bug described in the design doc "
        "has disappeared without anyone changing CommMgr -- investigate "
        "before assuming it's fixed"
    )

    # Confirm it was dropped for exactly the documented reason, not for
    # some unrelated cause (wrong comm_id, transport not connected, etc.)
    assert any(
        "Received response for unknown request" in rec.message
        for rec in caplog.records
    ), "expected the 'unknown request' warning that marks this specific failure mode"

    # Secondary consequence, also worth pinning down: side_b's own
    # `_pending` bookkeeping for this comm is now permanently stuck,
    # because no reply will ever arrive to clear it (side_a never even
    # saw a request to reply to). Every subsequent send on this comm
    # queues behind a request that can never complete.
    assert comm_b.comm_id in side_b._pending, (
        "side_b's pending slot for this comm should still be occupied -- "
        "this is what 'wedges' the comm for all future traffic"
    )
