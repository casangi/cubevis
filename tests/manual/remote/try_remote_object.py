#!/usr/bin/env python3
"""
Chunk 1c, Task 4 (use case 1 of 2) -- step-by-step, hand-run demo of
generalized remote object creation and invocation: an execution context
is configured with a real `register_function`
(`cubevis.remote._test_registrations:register_basic`), then driven
through `create_object` / `call_method` / `dispose_object`, including a
real numpy array argument and return value round-tripping through
`cubevis.utils.serialize`/`deserialize` (Bokeh's own serializer) over
the actual wire.

This is deliberately built on the same toy fixture classes the test
suite uses (`Counter`, `NumpyEcho` -- see
`cubevis/remote/_test_registrations.py`'s docstring for why they live
inside the installable package rather than under `tests/`), standing in
for what Chunk 2/3 would register instead: a real MSv2/MSv4 reader
instance, a Datashader render object, etc. Nothing about the mechanism
demonstrated here is toy-specific.

Run it against a local kernel first (no SSH, no cluster, fast
iteration):

    python try_remote_object.py --kernel-name python3

Then against a real sshpyk-provisioned remote kernel -- literally the
same script, only the kernel name changes (see try_worker_process.py's
own docstring for why):

    python3 try_remote_object.py --kernel-name zuul06_python312
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import numpy as np
from jupyter_client import AsyncKernelManager

from cubevis.remote import RemoteAppLink, DEFAULT_WORKER_TARGET_NAME

REGISTER_FUNCTION = "cubevis.remote._test_registrations:register_basic"


def _banner(step: int, title: str) -> None:
    print()
    print(f"--- Step {step}: {title} " + "-" * max(0, 50 - len(title)))


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kernel-name", default="python3",
                         help="Kernel name from `jupyter kernelspec list`. Default: python3 (local).")
    parser.add_argument("--worker-target-name", default=DEFAULT_WORKER_TARGET_NAME)
    parser.add_argument("--timeout", type=float, default=60.0,
                         help="Seconds to wait for kernel readiness/bootstrap/connect.")
    parser.add_argument("--create-context-timeout", type=float, default=180.0,
                         help="Seconds to wait for create_context() (worker subprocess "
                              "spawn + its opening configure round trip, on the "
                              "supervisor's own host). A real sshpyk-provisioned cluster "
                              "kernel can legitimately need much longer here than a local "
                              "one -- raise this before assuming a TimeoutError means the "
                              "spawn actually failed.")
    parser.add_argument("--log-level", default="INFO",
                         help="Root log level for THIS process (P_local). Note: a "
                              "failure inside create_context()'s worker spawn is logged "
                              "by the *supervisor kernel process*, not here -- for a "
                              "real sshpyk-provisioned kernel that means the remote "
                              "kernel's own log, regardless of this setting. Default: "
                              "INFO -- sshpyk's own breadcrumbs print, its much noisier "
                              "DEBUG-level ssh/ps-poll/bootstrap-line output does not; "
                              "pass --log-level DEBUG for that when diagnosing a real "
                              "hang or a slow remote host.")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.DEBUG),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    print("=" * 70)
    print(f"cubevis.remote generalized remote object demo (Chunk 1c) -- "
          f"kernel_name={args.kernel_name!r}")
    print("=" * 70)

    # ------------------------------------------------------------------
    # "Process, kernel and host information for where things run, once
    # at startup" -- P_local's own tier, printed immediately.
    # ------------------------------------------------------------------
    _banner(0, "P_local (this script)'s own process/host info")
    print(f"    pid      = {os.getpid()}")
    print(f"    hostname = {os.uname().nodename}")
    print(f"    python   = {sys.executable}")

    km = AsyncKernelManager(kernel_name=args.kernel_name)

    try:
        _banner(1, "start the supervisor kernel and connect the execution-context pool")
        await km.start_kernel()
        link = await RemoteAppLink.open(
            km, worker_target_name=args.worker_target_name, timeout=args.timeout
        )

        try:
            supervisor_info = await link.supervisor_info()
            print(f"    supervisor kernel: pid={supervisor_info['pid']} "
                  f"hostname={supervisor_info['hostname']}")

            _banner(2, f"create_context(config={{'register_function': {REGISTER_FUNCTION!r}}})")
            ctx = await link.create_context(config={"register_function": REGISTER_FUNCTION},
                                             timeout=args.create_context_timeout)
            worker_info = await ctx.worker_info()
            print(f"    worker subprocess: pid={worker_info['pid']} "
                  f"hostname={worker_info['hostname']} executable={worker_info['executable']}")
            print(f"    execution_context_id = {ctx.context_id}")
            print()
            print("    three tiers now printed once, per the kickoff doc's requirement:")
            print(f"      P_local:    pid={os.getpid()}, host={os.uname().nodename}")
            print(f"      supervisor: pid={supervisor_info['pid']}, host={supervisor_info['hostname']}")
            print(f"      worker:     pid={worker_info['pid']}, host={worker_info['hostname']}")

            _banner(3, "create_object('Counter', args=[10])")
            counter_handle = await ctx.create_object("Counter", args=[10])
            print(f"    handle = {counter_handle}")

            _banner(4, "call_method(handle, 'increment', args=[5]) x2 -- real, mutating state")
            v1 = await ctx.call_method(counter_handle, "increment", args=[5])
            v2 = await ctx.call_method(counter_handle, "increment", kwargs={"by": 1})
            print(f"    after +5:  {v1}")
            print(f"    after +1:  {v2}")
            print(f"    -> state genuinely persists across calls, in the worker's own memory")

            _banner(5, "dispose_object(handle)")
            disposed = await ctx.dispose_object(counter_handle)
            print(f"    disposed = {disposed}")
            reply = await ctx.dispatch_fast("call_method", {
                "handle": counter_handle, "method": "increment", "args": [], "kwargs": {}
            })
            print(f"    calling a disposed handle now returns: {reply}")

            _banner(6, "create_object('NumpyEcho') -- real numpy array through the real wire")
            echo_handle = await ctx.create_object("NumpyEcho")
            original = np.arange(6, dtype="float64").reshape(2, 3)
            print(f"    original array:\n{original}")

            doubled = await ctx.call_method(echo_handle, "double", args=[original])
            doubled = np.asarray(doubled)
            print(f"    doubled array (round-tripped through cubevis.utils.serialize/deserialize,")
            print(f"    Bokeh's real Serializer/Deserializer, over the actual wire):\n{doubled}")
            assert np.array_equal(doubled, original * 2)
            print(f"    -> round trip verified correct")

            await ctx.dispose_object(echo_handle)

        finally:
            _banner(7, "close() -- confirms the worker subprocess actually exits")
            results = await link.close()
            print(f"    close() results: {results}")

    finally:
        await km.shutdown_kernel()

    print()
    print("=" * 70)
    print("done")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
