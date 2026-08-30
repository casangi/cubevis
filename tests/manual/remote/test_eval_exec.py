"""
Chunk 1c, Task 4 (second use case) definition-of-done: generalized
remote Python eval, jupyter-console-like -- `eval_code` for single
expressions, `exec_code` for multi-statement code using the documented
`_result`-variable convention (see worker_main.py's `handle_exec_code`
docstring for why this was chosen over mirroring a notebook cell's
last-expression display hook). End-to-end against a real worker
subprocess.
"""
import os

import pytest

pytest.importorskip("jupyter_client")
pytest.importorskip("ipykernel")

from jupyter_client import AsyncKernelManager

from cubevis.remote import RemoteAppLink


@pytest.mark.asyncio
async def test_eval_code_single_expression():
    src_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    km = AsyncKernelManager(kernel_name="python3")
    await km.start_kernel(env={**os.environ, "PYTHONPATH": src_path})
    try:
        link = await RemoteAppLink.open(km, worker_target_name="eval-exec-test", timeout=30)
        try:
            ctx = await link.create_context()
            assert await ctx.eval_code("1 + 2") == 3
            assert await ctx.eval_code("'hello ' + 'world'") == "hello world"
            assert await ctx.eval_code("[x * x for x in range(5)]") == [0, 1, 4, 9, 16]
        finally:
            await link.close(timeout=20)
    finally:
        await km.shutdown_kernel()


@pytest.mark.asyncio
async def test_exec_code_uses_result_variable_convention():
    src_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    km = AsyncKernelManager(kernel_name="python3")
    await km.start_kernel(env={**os.environ, "PYTHONPATH": src_path})
    try:
        link = await RemoteAppLink.open(km, worker_target_name="eval-exec-test-2", timeout=30)
        try:
            ctx = await link.create_context()

            # Multi-statement exec with no _result assigned -> None, not
            # an error and not "the last expression's value" (exec() has
            # no such concept at all -- this pins down that we do NOT
            # try to emulate one).
            result = await ctx.exec_code("x = 1\ny = 2\nz = x + y")
            assert result is None

            # The documented convention: assign _result explicitly.
            result = await ctx.exec_code("a = 10\nb = 32\n_result = a + b")
            assert result == 42

            # _result must be cleared between calls -- a later exec_code
            # that doesn't set it must not see a stale value from an
            # earlier call on the same (persistent) worker namespace.
            result = await ctx.exec_code("noop = True")
            assert result is None, (
                "_result from a previous exec_code call leaked into one that "
                "never set it -- worker_main.py's handler must clear _result "
                "before every exec_code call"
            )

            # The namespace persists across calls on the same execution
            # context (one worker process, one persistent dict) -- proves
            # this isn't re-creating a fresh namespace per call.
            await ctx.exec_code("counter = 0")
            await ctx.exec_code("counter += 1")
            await ctx.exec_code("counter += 1")
            assert await ctx.eval_code("counter") == 2
        finally:
            await link.close(timeout=20)
    finally:
        await km.shutdown_kernel()


@pytest.mark.asyncio
async def test_eval_exec_can_reach_registry_created_objects():
    """The eval/exec namespace is seeded with `_registry` (see
    worker_main.py's `_amain`) specifically so ad hoc eval/exec snippets
    can reach objects created via `create_object` -- the two Task 4 use
    cases (generic object invocation, generic eval) are meant to
    compose, not live in separate silos."""
    src_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    km = AsyncKernelManager(kernel_name="python3")
    await km.start_kernel(env={**os.environ, "PYTHONPATH": src_path})
    try:
        link = await RemoteAppLink.open(km, worker_target_name="eval-exec-registry-test", timeout=30)
        try:
            ctx = await link.create_context(
                config={"register_function": "cubevis.remote._test_registrations:register_basic"}
            )
            handle = await ctx.create_object("Counter", args=[100])
            value = await ctx.eval_code(f"_registry.get_object({handle!r}).value")
            assert value == 100

            result = await ctx.exec_code(
                f"_obj = _registry.get_object({handle!r})\n"
                f"_obj.increment(5)\n"
                f"_result = _obj.value"
            )
            assert result == 105
        finally:
            await link.close(timeout=20)
    finally:
        await km.shutdown_kernel()
