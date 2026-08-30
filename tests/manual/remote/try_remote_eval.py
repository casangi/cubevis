#!/usr/bin/env python3
"""
Chunk 1c, Task 4 (use case 2 of 2) -- step-by-step, hand-run demo of
generalized remote Python eval/exec, similar in spirit to
`jupyter-console` talking to a kernel -- except here the code runs
inside an `asyncio.subprocess` worker that the remote supervisor kernel
itself created and supervises (Chunk 1b/1c), not in the supervisor
kernel's own namespace.

Demonstrates, in order: single-expression `eval_code` (plain `eval()`
semantics), multi-statement `exec_code` using the documented `_result`
-variable convention (see worker_main.py's `handle_exec_code` docstring
for why this was chosen over mirroring a notebook cell's
last-expression display hook), the persistent worker-side namespace
across calls, and reaching an object created via `create_object`
directly from an eval/exec snippet through the `_registry` name that's
seeded into the namespace for exactly that purpose.

Also offers a genuine `--interactive` mode: a real read-eval-print loop
against the live remote worker, one line at a time, exactly like
`jupyter-console` -- each line is tried as `eval_code` first (so a bare
expression prints its value, matching console/REPL expectations) and
falls back to `exec_code` if it isn't a valid expression.

Run it against a local kernel first (no SSH, no cluster, fast
iteration):

    python try_remote_eval.py --kernel-name python3
    python try_remote_eval.py --kernel-name python3 --interactive
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from jupyter_client import AsyncKernelManager

from cubevis.remote import RemoteAppLink, DEFAULT_WORKER_TARGET_NAME

REGISTER_FUNCTION = "cubevis.remote._test_registrations:register_basic"


def _banner(step, title: str) -> None:
    print()
    print(f"--- Step {step}: {title} " + "-" * max(0, 50 - len(title)))


async def _run_scripted_demo(ctx) -> None:
    _banner(1, "eval_code: single expressions")
    for expr in ["1 + 2", "'hello ' + 'world'", "[x * x for x in range(5)]", "sum(range(101))"]:
        result = await ctx.eval_code(expr)
        print(f"    eval_code({expr!r}) -> {result!r}")

    _banner(2, "exec_code: multi-statement, no _result set -> None")
    result = await ctx.exec_code("x = 1\ny = 2\nz = x + y")
    print(f"    exec_code('x = 1; y = 2; z = x + y') -> {result!r}")
    print(f"    (exec() has no 'last expression value' concept -- unlike a notebook")
    print(f"     cell's display hook, which this deliberately does NOT emulate; see")
    print(f"     worker_main.py's handle_exec_code docstring)")

    _banner(3, "exec_code: the documented `_result` convention")
    result = await ctx.exec_code("a = 10\nb = 32\n_result = a + b")
    print(f"    exec_code('a = 10; b = 32; _result = a + b') -> {result!r}")

    _banner(4, "the worker-side namespace persists across calls")
    await ctx.exec_code("counter = 0")
    await ctx.exec_code("counter += 1")
    await ctx.exec_code("counter += 1")
    value = await ctx.eval_code("counter")
    print(f"    three exec_code calls incrementing 'counter' -> eval_code('counter') = {value}")

    _banner(5, "eval/exec reaching a create_object-created instance via `_registry`")
    handle = await ctx.create_object("Counter", args=[100])
    print(f"    created Counter(100) -> handle={handle}")
    value = await ctx.eval_code(f"_registry.get_object({handle!r}).value")
    print(f"    eval_code(\"_registry.get_object({handle!r}).value\") -> {value}")
    result = await ctx.exec_code(
        f"_obj = _registry.get_object({handle!r})\n_obj.increment(5)\n_result = _obj.value"
    )
    print(f"    exec_code (increment by 5 then read .value) -> {result}")
    await ctx.dispose_object(handle)


async def _run_interactive_repl(ctx) -> None:
    print()
    print("Interactive remote eval -- one line at a time, Ctrl-D/Ctrl-C to quit.")
    print("Each line is tried as an expression (eval_code) first; if that fails to")
    print("compile as an expression, it's run as a statement (exec_code) instead --")
    print("the same jupyter-console-like feel of typing at a live remote namespace.")
    loop = asyncio.get_running_loop()
    while True:
        try:
            line = await loop.run_in_executor(None, input, ">>> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line.strip():
            continue
        try:
            compile(line, "<remote-eval>", "eval")
            is_expression = True
        except SyntaxError:
            is_expression = False

        try:
            if is_expression:
                result = await ctx.eval_code(line)
                print(repr(result))
            else:
                result = await ctx.exec_code(line)
                if result is not None:
                    print(repr(result))
        except Exception as e:
            print(f"error: {e}")


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
    parser.add_argument("--interactive", action="store_true",
                         help="Drop into a real read-eval-print loop against the live "
                              "remote worker, instead of running the scripted demo steps.")
    parser.add_argument("--log-level", default="DEBUG",
                         help="Root log level for THIS process (P_local). Note: a "
                              "failure inside create_context()'s worker spawn is logged "
                              "by the *supervisor kernel process*, not here -- for a "
                              "real sshpyk-provisioned kernel that means the remote "
                              "kernel's own log, regardless of this setting.")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.DEBUG),
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    print("=" * 70)
    print(f"cubevis.remote generalized remote eval demo (Chunk 1c) -- "
          f"kernel_name={args.kernel_name!r}")
    print("=" * 70)

    _banner(0, "P_local (this script)'s own process/host info")
    print(f"    pid      = {os.getpid()}")
    print(f"    hostname = {os.uname().nodename}")
    print(f"    python   = {sys.executable}")

    km = AsyncKernelManager(kernel_name=args.kernel_name)

    try:
        await km.start_kernel()
        link = await RemoteAppLink.open(
            km, worker_target_name=args.worker_target_name, timeout=args.timeout
        )
        try:
            supervisor_info = await link.supervisor_info()
            print(f"    supervisor kernel: pid={supervisor_info['pid']} "
                  f"hostname={supervisor_info['hostname']}")

            ctx = await link.create_context(config={"register_function": REGISTER_FUNCTION},
                                             timeout=args.create_context_timeout)
            worker_info = await ctx.worker_info()
            print(f"    worker subprocess: pid={worker_info['pid']} "
                  f"hostname={worker_info['hostname']} executable={worker_info['executable']}")
            print()
            print("    three tiers now printed once, per the kickoff doc's requirement:")
            print(f"      P_local:    pid={os.getpid()}, host={os.uname().nodename}")
            print(f"      supervisor: pid={supervisor_info['pid']}, host={supervisor_info['hostname']}")
            print(f"      worker:     pid={worker_info['pid']}, host={worker_info['hostname']}")

            if args.interactive:
                await _run_interactive_repl(ctx)
            else:
                await _run_scripted_demo(ctx)

        finally:
            _banner("close", "close() -- confirms the worker subprocess actually exits")
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
