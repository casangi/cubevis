#!/usr/bin/env python3
"""
Step-by-step, hand-run demo of the Chunk 1 remote-execution machinery:
start a kernel, bootstrap a (toy) worker in it, run a couple of commands
against that worker, shut everything down.

Run it against a local kernel first (no SSH, no cluster, fast iteration):

    python try_local_or_remote_kernel.py --kernel-name python3

Then against a real sshpyk-provisioned remote kernel -- literally the
same script, only the kernel name changes, because sshpyk registers
itself as a normal jupyter_client provisioner and this code only ever
talks to the standard AsyncKernelManager/AsyncKernelClient API (see
`cubevis.remote._kernel_transport.KernelClientTransport`, which is what
actually does the work below):

    python3 try_local_or_remote_kernel.py --kernel-name python3
    python3 try_local_or_remote_kernel.py --kernel-name zuul06_python312

`jupyter kernelspec list` shows which names are available.

Prerequisites: `cubevis` must be importable in BOTH the environment this
script runs in AND whatever environment the target kernel uses -- for a
local kernel that's normally automatic (same Python env); for a real
remote sshpyk kernel, cubevis needs to be installed on the remote host
too. If bootstrap fails with `ModuleNotFoundError: cubevis`, that's what
to check first. (For sandbox/development testing where cubevis isn't
actually installed anywhere, `--extra-sys-path` can inject a path into
the remote bootstrap code as a workaround -- not something you should
need in a real environment.)
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from jupyter_client import AsyncKernelManager

from cubevis.bokeh.transport import CommMgr
from cubevis.remote import DEFAULT_TARGET_NAME, open_remote_kernel_link, request


BOOTSTRAP_CODE = """
{extra_sys_path}
from cubevis.remote import ensure_remote_worker

def build_worker(mgr):
    # A real backend (Chunk 2/3) would open an MS / build a
    # ReductionContext or gclean instance here, and register the actual
    # query/progress handlers those need. This demo worker is a stand-in:
    # two trivial commands, registered the same way a real one would be.
    import os, socket

    comm = mgr.open("demo")

    def handle_ping(msg):
        return {{
            "pong": True,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
        }}

    def handle_add(msg):
        a, b = msg["a"], msg["b"]
        return {{"sum": a + b, "computed_on_host": socket.gethostname()}}

    comm.register("ping", handle_ping)
    comm.register("add", handle_add)

    return {{"demo_worker": True, "pid": os.getpid(), "hostname": socket.gethostname()}}

comm_mgr_id = ensure_remote_worker(build_worker, target_name={target_name!r})
print("BOOTSTRAP_OK comm_mgr_id=" + comm_mgr_id)
"""


def _banner(step: int, title: str) -> None:
    print()
    print(f"--- Step {step}: {title} " + "-" * max(0, 50 - len(title)))


async def _run_bootstrap_and_stream_output(client, code: str, timeout: float = 60.0) -> None:
    """
    Executes `code` in the remote kernel and prints its stdout live, so
    you can watch the bootstrap happen instead of it disappearing into a
    black box. Raises if the remote cell itself raises.
    """
    client.execute(code)
    while True:
        msg = await client.get_iopub_msg(timeout=timeout)
        msg_type = msg.get("msg_type")
        if msg_type == "stream":
            for line in msg["content"]["text"].splitlines():
                print(f"    [remote] {line}")
        elif msg_type == "error":
            tb = "\n".join(msg["content"].get("traceback", []))
            raise RuntimeError(f"remote bootstrap cell raised:\n{tb}")
        elif msg_type == "status" and msg["content"]["execution_state"] == "idle":
            await client.get_shell_msg(timeout=timeout)  # drain the execute_reply
            return


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--kernel-name",
        default="python3",
        help="Kernel name from `jupyter kernelspec list`. Default: python3 (local). "
             "Pass an sshpyk-provisioned name (e.g. zuul06_python312) for the real target.",
    )
    parser.add_argument(
        "--target-name",
        default=DEFAULT_TARGET_NAME,
        help=f"Comm target name both sides agree on. Default: {DEFAULT_TARGET_NAME!r}.",
    )
    parser.add_argument(
        "--extra-sys-path",
        default=None,
        help="Development-only: a path to sys.path.insert(0, ...) inside the remote "
             "kernel before importing cubevis. Not needed in a real environment where "
             "cubevis is properly installed on both ends.",
    )
    args = parser.parse_args()

    extra_sys_path = (
        f"import sys; sys.path.insert(0, {args.extra_sys_path!r})"
        if args.extra_sys_path
        else "# (no --extra-sys-path given; assuming cubevis is already importable here)"
    )

    print("=" * 70)
    print(f"cubevis.remote demo -- kernel_name={args.kernel_name!r}")
    print("=" * 70)

    # ------------------------------------------------------------------
    _banner(1, "construct AsyncKernelManager (standard jupyter_client, no cubevis/sshpyk-specific code)")
    # This is the ENTIRE decision of whether the kernel that ends up
    # running is local or a real sshpyk-tunneled remote process --
    # nothing past this line knows or cares which.
    km = AsyncKernelManager(kernel_name=args.kernel_name)
    print(f"    AsyncKernelManager(kernel_name={args.kernel_name!r}) constructed")

    try:
        # --------------------------------------------------------------
        _banner(2, "start the kernel and bootstrap a (toy) worker in it")
        print("    starting kernel "
              "(this is where an sshpyk kernel actually pays the SSH-connect cost)...")
        await km.start_kernel()
        setup_client = km.client()
        setup_client.start_channels()
        await setup_client.wait_for_ready(timeout=90)
        print("    kernel ready")

        await _run_bootstrap_and_stream_output(
            setup_client,
            BOOTSTRAP_CODE.format(extra_sys_path=extra_sys_path, target_name=args.target_name),
        )
        setup_client.stop_channels()

        # --------------------------------------------------------------
        _banner(3, "open_remote_kernel_link() -- P_local's side, one call")
        mgr, transport = await open_remote_kernel_link(km, target_name=args.target_name)
        print(f"    mgr.role            = {mgr.role!r}  (should be {CommMgr.ROLE_MIRROR!r})")
        print(f"    mgr.transport_type  = {mgr.transport_type!r}  (should be 'remote_kernel')")
        print(f"    transport.is_connected() = {transport.is_connected()}")

        run_task = asyncio.ensure_future(transport.run())
        try:
            # ----------------------------------------------------------
            _banner(4, "run a command: ping")
            comm = mgr.open("demo")
            reply = await request(comm, "ping", {})
            print(f"    reply: {reply}")
            print(f"    -> that pid/hostname is where the command actually executed;")
            print(f"       compare against `hostname`/`echo $$` run locally to confirm")
            print(f"       this really happened on the kernel side, not in this process.")

            # ----------------------------------------------------------
            _banner(5, "run a command: add")
            reply = await request(comm, "add", {"a": 19, "b": 23})
            print(f"    reply: {reply}")

        finally:
            _banner(6, "tear down")
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass
            await transport.close()
            print("    transport closed")

    finally:
        await km.shutdown_kernel()
        print("    kernel shut down")

    print()
    print("=" * 70)
    print("done")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
