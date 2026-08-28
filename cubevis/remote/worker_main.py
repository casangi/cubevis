"""
Chunk 1b, Task 2.

`python -m cubevis.remote.worker_main` -- the worker subprocess's entry
point. Constructs a `CommMgr(role=CommMgr.ROLE_DEFAULT)` +
`WorkerCommTransport`, reading/writing its own stdin/stdout.

For this chat's tests, a toy worker registering a couple of trivial
handlers is enough -- real backend objects (an opened MS, a
`ReductionContext`, a `gclean` instance) are Chunk 2/3's job, dispatched
through exactly this same plumbing without touching it again.

CRITICAL: nothing here may write to stdout except the framed protocol
messages `WorkerCommTransport` sends -- stdout is the wire. Anything a
handler (or a library it calls) prints accidentally would corrupt the
frame stream from the supervisor's point of view. Logging is configured
to stderr only, which `WorkerProcessTransport` on the supervisor side
captures and relays.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import time

from cubevis.bokeh.transport import CommMgr, AppState
from cubevis.remote._worker_transport import WorkerCommTransport

logger = logging.getLogger("cubevis.remote.worker_main")


def _register_toy_handlers(comm) -> None:
    """Mirrors Chunk 1's demo_local_or_remote_kernel.py toy commands --
    not real backend objects, just enough surface to exercise the
    transport for real (request/response, push, a deliberately slow
    command for liveness tests, and a deliberate crash for stderr-relay
    tests)."""

    def handle_ping(msg):
        return {"pong": True, "pid": os.getpid(), "hostname": socket.gethostname()}

    def handle_add(msg):
        return {"sum": msg["a"] + msg["b"]}

    async def handle_slow_echo(msg):
        """A command that blocks the *worker's* asyncio loop for real
        (a CPU-bound sleep, standing in for a GIL-holding C++ call) --
        for exercising the supervisor's background-dispatch path against
        genuinely long-running work."""
        duration = msg.get("duration", 1.0)
        time.sleep(duration)  # deliberately synchronous/blocking, not asyncio.sleep
        return {"echo": msg.get("value"), "slept": duration}

    def handle_crash(msg):
        raise RuntimeError(f"deliberate crash for testing: {msg.get('reason', 'no reason given')}")

    async def handle_trigger_push(msg):
        """Worker-initiated push: sends an unsolicited message on this
        same comm *before* replying to this request, exercising the
        other direction of traffic (not just replying to what it's
        asked) against a real subprocess."""
        await comm.send("worker_event", {"note": msg.get("note", "hello from worker"),
                                          "pid": os.getpid()})
        return {"triggered": True}

    comm.register("trigger_push", handle_trigger_push)

    comm.register("ping", handle_ping)
    comm.register("add", handle_add)
    comm.register("slow_echo", handle_slow_echo)
    comm.register("crash", handle_crash)


async def _amain(comm_mgr_id: str) -> None:
    mgr = CommMgr(role=CommMgr.ROLE_DEFAULT, comm_mgr_id=comm_mgr_id,
                  transport_type="remote_kernel")
    comm = mgr.open("worker")
    _register_toy_handlers(comm)

    transport = WorkerCommTransport(comm_mgr_id)
    await mgr.initialize(transport=transport)
    mgr.state = AppState.RUNNING

    logger.info(f"worker_main: ready, pid={os.getpid()}, comm_mgr_id={comm_mgr_id}")
    await transport.run()
    logger.info("worker_main: transport closed, exiting")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--comm-mgr-id", required=True,
                         help="comm_mgr_id the supervisor expects this worker to use "
                              "(so both sides' role-direction tags line up)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    # Logging to stderr ONLY -- stdout is the wire protocol.
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    try:
        asyncio.run(_amain(args.comm_mgr_id))
    except Exception:
        logger.exception("worker_main: fatal error during startup/run")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
