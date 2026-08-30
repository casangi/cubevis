"""
Chunk 1b, Task 5 definition-of-done: `RemoteAppLink.open()`/`.close()`
tested; the worker subprocess is confirmed gone (not just unreferenced)
after `close()`.

Chunk 1c rewrite: `RemoteAppLink.open()` no longer spawns a worker
subprocess itself (Task 2) -- it only bootstraps the kernel's Layer-1
execution-context *pool*. A worker subprocess now comes from
`link.create_context()`, which returns an `ExecutionContext` handle
(`ctx.context_id`, `ctx.pid`) that owns `dispatch_fast`/`dispatch_async`/
etc directly -- no more building a `{"message_id": ..., "payload": ...}`
envelope and sending it via `request(comm, "dispatch_fast", ...)` by
hand. `link.close()` now tears down *every* context the link created
and returns a `Dict[context_id, result]` rather than one bare result, so
the definition-of-done check (confirm actual OS-level process exit) now
reads that dict by `ctx.context_id`. The underlying guarantee this test
protects -- `close()` proves the subprocess is actually gone, not just
unreferenced -- is unchanged.
"""
import asyncio
import os

import pytest

pytest.importorskip("jupyter_client")
pytest.importorskip("ipykernel")

from jupyter_client import AsyncKernelManager

from cubevis.remote import RemoteAppLink


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours -- shouldn't happen here
    return True


@pytest.mark.asyncio
async def test_open_close_confirms_worker_subprocess_actually_exits():
    src_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    km = AsyncKernelManager(kernel_name="python3")
    await km.start_kernel(env={**os.environ, "PYTHONPATH": src_path})
    try:
        link = await RemoteAppLink.open(km, worker_target_name="test-app-link", timeout=30)
        try:
            assert link.transport.is_connected()

            # create_context() is the Chunk 1c entry point that actually
            # spawns a worker subprocess -- open() alone no longer does.
            ctx = await link.create_context()
            assert ctx.pid is not None

            # Use the fast-dispatch proxy end to end -- proves this isn't
            # just a connected-but-idle link.
            reply = await ctx.dispatch_fast("add", {"a": 10, "b": 32})
            assert reply == {"sum": 42}

            # Independently discover the worker's actual OS pid via the
            # kernel, so close()'s claim can be checked from *outside*
            # this process's own bookkeeping, not just by trusting
            # transport.returncode.
            reply = await ctx.dispatch_fast("ping", {})
            worker_pid = reply["pid"]
            assert worker_pid == ctx.pid, (
                "create_context()'s reported pid should match the pid the "
                "worker itself reports"
            )
            assert _pid_alive(worker_pid), "worker subprocess should be alive before close()"

        finally:
            results = await link.close(timeout=20)

        assert ctx.context_id in results, (
            "close() should report a result for every context it tore down"
        )
        result = results[ctx.context_id]
        assert result.get("closed") is True, (
            f"close() should get back a confirmation from the supervisor's own "
            f"shutdown_context handler, not just close its own side; got {result}"
        )
        assert result.get("returncode") is not None, (
            "the supervisor's confirmation must include the worker's actual exit code"
        )

        # The real proof: ask the OS directly, from this test process,
        # independent of anything cubevis reported.
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline and _pid_alive(worker_pid):
            await asyncio.sleep(0.1)
        assert not _pid_alive(worker_pid), (
            f"worker subprocess (pid={worker_pid}) is still alive after "
            f"RemoteAppLink.close() returned -- close() must confirm actual "
            f"process exit, not just that Python references were dropped"
        )
    finally:
        await km.shutdown_kernel()


@pytest.mark.asyncio
async def test_two_execution_contexts_are_genuinely_separate_subprocesses():
    """Chunk 1c, Task 1's core pool guarantee: at least two
    `execution_context_id`s can live concurrently under one supervisor
    kernel, each independently reachable, each a genuinely separate OS
    process."""
    src_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    km = AsyncKernelManager(kernel_name="python3")
    await km.start_kernel(env={**os.environ, "PYTHONPATH": src_path})
    try:
        link = await RemoteAppLink.open(km, worker_target_name="test-pool-link", timeout=30)
        try:
            ctx_a = await link.create_context()
            ctx_b = await link.create_context()
            assert ctx_a.context_id != ctx_b.context_id
            assert ctx_a.pid != ctx_b.pid

            reply_a = await ctx_a.dispatch_fast("ping", {})
            reply_b = await ctx_b.dispatch_fast("ping", {})
            assert reply_a["pid"] == ctx_a.pid
            assert reply_b["pid"] == ctx_b.pid
            assert _pid_alive(ctx_a.pid) and _pid_alive(ctx_b.pid)

            contexts = await link.list_contexts()
            assert {c["context_id"] for c in contexts} == {ctx_a.context_id, ctx_b.context_id}

            # Tear down just one context; the other must be unaffected.
            await ctx_a.shutdown()
            deadline = asyncio.get_running_loop().time() + 5.0
            while asyncio.get_running_loop().time() < deadline and _pid_alive(ctx_a.pid):
                await asyncio.sleep(0.1)
            assert not _pid_alive(ctx_a.pid)
            assert _pid_alive(ctx_b.pid), (
                "shutting down one execution context must not affect a sibling "
                "context in the same pool"
            )
        finally:
            await link.close(timeout=20)
    finally:
        await km.shutdown_kernel()
