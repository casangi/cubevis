#!/usr/bin/env python3
"""
Step-by-step, hand-run demo of the Chunk 1b remote-execution machinery:
`RemoteAppLink.open()` (bootstrap the execution-context pool + connect
in one call), a fast command, a push message, a background/async
command with status polling, and a clean `close()` that confirms the
worker subprocess actually exited -- not just that this script's
references to it were dropped.

Chunk 1c change from the original version of this demo: `open()` no
longer spawns a worker subprocess by itself -- it only connects to the
kernel's Layer-1 execution-context pool (`_supervisor.py`). Getting an
actual worker subprocess to talk to now takes one more explicit call,
`link.create_context()`, which is Step 3 below. See
`cubevis-remote-execution-implementation.md`'s Chunk 1c section for the
full three-layer id model (comm_mgr_id -> execution_context_id ->
object handle) this reflects.

Run it against a local kernel first (no SSH, no cluster, fast iteration):

    python try_worker_process.py --kernel-name python3

Then against a real sshpyk-provisioned remote kernel -- literally the
same script, only the kernel name changes, for exactly the reason
Chunk 1's own demo works this way: this code only ever talks to the
standard `AsyncKernelManager`/`AsyncKernelClient` API, and sshpyk
registers itself as a normal jupyter_client provisioner underneath it:

    python3 try_worker_process.py --kernel-name python3
    python3 try_worker_process.py --kernel-name zuul06_python312

`jupyter kernelspec list` shows which names are available.

What this demonstrates that Chunk 1's own demo doesn't:

  - `RemoteAppLink.open()`/`.create_context()`/`.close()` -- bootstrap
    the pool, spawn a worker subprocess inside it, and a teardown that
    confirms actual subprocess exit for every context the link created.
  - A *third* process in the picture, not just two: this script
    (P_local) drives the supervisor *kernel* (Chunk 1's remote-kernel
    side), which in turn spawns and drives its own *worker subprocess*
    (Chunk 1b/1c) inside one execution context -- three distinct pids,
    potentially two distinct hosts once run against a real remote
    kernel name.
  - Two different dispatch shapes, and why both exist: `dispatch_fast`
    awaits the worker directly and returns inline -- fine for anything
    that answers promptly. `dispatch_async` returns a `job_id`
    immediately and never blocks the supervisor's own receive loop, so
    `job_status` polls stay fast even while the worker is genuinely
    still busy inside a long call -- see the Chunk 1b addendum for why
    the first approach alone would have made status checks queue up
    behind whatever's currently running.

What this deliberately does NOT demonstrate: a worker-initiated push
reaching P_local through `RemoteAppLink`. `WorkerProcessTransport`'s own
push capability is proven directly against the supervisor
(`test_worker_process_transport.py::test_push_round_trip_against_real_subprocess`),
but relaying an arbitrary push on to P_local requires a handler
registered for that *specific* message_id on the supervisor's
worker-facing comm -- `CommMgr` has no wildcard/catch-all handler, and
registering one for a particular push type is exactly the kind of
app-specific wiring (a `visplot` progress update, an `iclean`
convergence update) that's Chunk 2/3's job, not this chunk's generic
`_supervisor.py`. Found by trying to demo it here rather than assumed.

Prerequisites: same as Chunk 1's demo -- `cubevis` must be importable in
BOTH this process's environment AND whatever environment the supervisor
kernel uses. The worker subprocess is spawned with `sys.executable`
*inside* the supervisor kernel's own process, so if `cubevis` is
importable there, it's importable for the worker automatically -- no
separate remote-worker install step.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from jupyter_client import AsyncKernelManager

from cubevis.remote import RemoteAppLink, DEFAULT_WORKER_TARGET_NAME


def _banner(step: int, title: str) -> None:
    print()
    print(f"--- Step {step}: {title} " + "-" * max(0, 50 - len(title)))


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--kernel-name",
        default="python3",
        help="Kernel name from `jupyter kernelspec list`. Default: python3 (local). "
             "Pass an sshpyk-provisioned name (e.g. zuul06_python312) for the real target.",
    )
    parser.add_argument(
        "--worker-target-name",
        default=DEFAULT_WORKER_TARGET_NAME,
        help=f"Comm target the supervisor's execution-context pool bootstraps under. "
             f"Default: {DEFAULT_WORKER_TARGET_NAME!r}.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Seconds to wait for kernel readiness / bootstrap / connect. Real "
             "sshpyk-provisioned kernels pay an SSH-connect cost here; local ones "
             "don't need nearly this long. Default: 60.",
    )
    args = parser.parse_args()

    print("=" * 70)
    print(f"cubevis.remote worker-process demo (Chunk 1b/1c) -- kernel_name={args.kernel_name!r}")
    print("=" * 70)

    # ------------------------------------------------------------------
    _banner(1, "construct AsyncKernelManager (the supervisor kernel)")
    # Same decision point as Chunk 1's demo: this line alone is the
    # entire choice between local and real-remote-via-sshpyk -- nothing
    # past it knows or cares which.
    km = AsyncKernelManager(kernel_name=args.kernel_name)
    print(f"    AsyncKernelManager(kernel_name={args.kernel_name!r}) constructed")
    print(f"    this script's own pid: {os.getpid()}")
    print(f"    (compare against the worker subprocess's pid reported in Step 4 --")
    print(f"     they must differ, and with a real remote kernel name, so may the host)")

    try:
        # ----------------------------------------------------------
        _banner(2, "RemoteAppLink.open() -- bootstrap the execution-context pool AND connect")
        print("    starting kernel (an sshpyk kernel pays the SSH-connect cost here)...")
        await km.start_kernel()
        print("    kernel process started; RemoteAppLink.open() will now:")
        print("      (a) run a bootstrap cell inside the supervisor kernel that")
        print("          constructs the Layer-1 execution-context pool and registers")
        print("          its proxy handlers (ensure_remote_worker + build_worker_pool --")
        print("          idempotent: a second open() against the same still-running")
        print("          kernel would reattach to the SAME pool, contexts and all,")
        print("          instead of building a fresh empty one)")
        print("      (b) connect P_local's own CommMgr to the supervisor")
        print("          (Chunk 1's request()/KernelClientTransport, unchanged)")
        print("    note: this alone does NOT spawn any worker subprocess yet --")
        print("    that's Step 3's job (Chunk 1c: the pool always exists at Layer 1;")
        print("    an execution context is only created at Layer 2, explicitly).")
        link = await RemoteAppLink.open(
            km, worker_target_name=args.worker_target_name, timeout=args.timeout
        )
        print(f"    link.mgr.role                 = {link.mgr.role!r}  (should be 'mirror')")
        print(f"    link.transport.is_connected() = {link.transport.is_connected()}")

        try:
            # ----------------------------------------------------------
            _banner(3, "create_context() -- spawn a worker subprocess inside the pool")
            supervisor_info = await link.supervisor_info()
            print(f"    supervisor kernel process: {supervisor_info}")

            ctx = await link.create_context()
            print(f"    execution_context_id = {ctx.context_id}")
            print(f"    worker subprocess pid = {ctx.pid}")

            # ----------------------------------------------------------
            _banner(4, "fast dispatch: ping (direct request/response, no job registry)")
            reply = await ctx.dispatch_fast("ping", {})
            print(f"    reply: {reply}")
            print(f"    -> pid={reply['pid']} is the WORKER SUBPROCESS's pid: a child of")
            print(f"       the supervisor kernel (pid={supervisor_info['pid']}), itself a")
            print(f"       separate process from this script (pid={os.getpid()}) -- three")
            print(f"       processes, confirmed distinct")

            # ----------------------------------------------------------
            _banner(5, "fast dispatch: add")
            reply = await ctx.dispatch_fast("add", {"a": 19, "b": 23})
            print(f"    reply: {reply}")

            # ----------------------------------------------------------
            _banner(6, "async dispatch: slow_echo -- returns a job_id immediately")
            t0 = time.monotonic()
            job_id = await ctx.dispatch_async(
                "slow_echo", {"duration": 3.0, "value": "background job"}
            )
            print(f"    dispatch_async returned in {time.monotonic() - t0:.3f}s -- job_id={job_id}")
            print(f"    (that's the whole point: it did NOT wait 3 seconds for the worker)")

            # ----------------------------------------------------------
            _banner(7, "poll job_status while the worker is genuinely still busy")
            status = {}
            while True:
                poll_t0 = time.monotonic()
                status = await ctx.job_status(job_id)
                poll_elapsed = time.monotonic() - poll_t0
                print(f"    [t={time.monotonic() - t0:5.1f}s] status={status['status']!r} "
                      f"(this poll itself took {poll_elapsed * 1000:.0f}ms)")
                if status["status"] == "completed":
                    break
                await asyncio.sleep(0.5)
            print(f"    result: {status['result']}")
            print(f"    -> every poll above stayed fast (milliseconds), even while the")
            print(f"       worker was still inside a 3-second blocking call -- the")
            print(f"       supervisor never had to choose between answering this poll")
            print(f"       and waiting on the worker")

        finally:
            # ----------------------------------------------------------
            _banner(8, "close() -- confirms every execution context's worker actually exits")
            results = await link.close()
            print(f"    close() results (by context_id): {results}")
            print(f"    -> 'closed': True and a real returncode per context, not just")
            print(f"       P_local dropping its own references to the transport")

    finally:
        _banner(9, "shut down the supervisor kernel")
        await km.shutdown_kernel()
        print("    kernel shut down")

    print()
    print("=" * 70)
    print("done")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
