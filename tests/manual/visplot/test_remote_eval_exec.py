"""
Regression coverage for the Chunk 1c generalized remote eval/exec path:
``RemoteAppLink`` -> ``create_context()`` -> ``eval_code``/``exec_code``
against a real worker subprocess.

Promoted 2026-09 (Chunk 2d) from ``try_remote_eval.py``'s scripted demo
-- first pytest coverage of this path. Runs against a local kernel by
default; override with ``CUBEVIS_TEST_KERNEL`` for a real sshpyk kernel.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio
from jupyter_client import AsyncKernelManager

from cubevis.remote import RemoteAppLink, DEFAULT_WORKER_TARGET_NAME

KERNEL_NAME = os.environ.get("CUBEVIS_TEST_KERNEL", "python3")
REGISTER_FUNCTION = "cubevis.remote._test_registrations:register_basic"


@pytest_asyncio.fixture
async def eval_ctx():
    """A live ExecutionContext against a fresh worker subprocess.

    Function-scoped -- fresh kernel + worker per test, same isolation
    tradeoff as test_remote_kernel_link.py's bootstrapped_link fixture.
    """
    km = AsyncKernelManager(kernel_name=KERNEL_NAME)
    await km.start_kernel()
    try:
        link = await RemoteAppLink.open(
            km, worker_target_name=DEFAULT_WORKER_TARGET_NAME, timeout=60.0)
        try:
            ctx = await link.create_context(
                config={"register_function": REGISTER_FUNCTION}, timeout=180.0)
            yield ctx
        finally:
            await link.close()
    finally:
        await km.shutdown_kernel()


@pytest.mark.asyncio
async def test_eval_code_simple_expressions(eval_ctx):
    assert await eval_ctx.eval_code("1 + 2") == 3
    assert await eval_ctx.eval_code("'hello ' + 'world'") == "hello world"
    assert await eval_ctx.eval_code("[x * x for x in range(5)]") == [0, 1, 4, 9, 16]
    assert await eval_ctx.eval_code("sum(range(101))") == 5050


@pytest.mark.asyncio
async def test_exec_code_without_result_returns_none(eval_ctx):
    """exec() has no 'last expression value' concept -- unlike a
    notebook cell's display hook, which this deliberately does not
    emulate; see worker_main.py's handle_exec_code docstring."""
    result = await eval_ctx.exec_code("x = 1\ny = 2\nz = x + y")
    assert result is None


@pytest.mark.asyncio
async def test_exec_code_result_convention(eval_ctx):
    result = await eval_ctx.exec_code("a = 10\nb = 32\n_result = a + b")
    assert result == 42


@pytest.mark.asyncio
async def test_namespace_persists_across_calls(eval_ctx):
    await eval_ctx.exec_code("counter = 0")
    await eval_ctx.exec_code("counter += 1")
    await eval_ctx.exec_code("counter += 1")
    assert await eval_ctx.eval_code("counter") == 2


@pytest.mark.asyncio
async def test_registry_reachable_from_eval_and_exec(eval_ctx):
    """A create_object-created instance must be reachable from eval/exec
    snippets via the `_registry` name seeded into the worker namespace."""
    handle = await eval_ctx.create_object("Counter", args=[100])
    try:
        value = await eval_ctx.eval_code(f"_registry.get_object({handle!r}).value")
        assert value == 100
        result = await eval_ctx.exec_code(
            f"_obj = _registry.get_object({handle!r})\n"
            f"_obj.increment(5)\n_result = _obj.value"
        )
        assert result == 105
    finally:
        await eval_ctx.dispose_object(handle)
