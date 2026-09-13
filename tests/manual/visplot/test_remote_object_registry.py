"""
Regression coverage for the Chunk 1c generalized remote object
creation/invocation path: create_object/call_method/dispose_object,
including a real numpy array round-tripping through
``cubevis.utils.serialize``/``deserialize`` (Bokeh's own Serializer)
over the actual wire.

Promoted 2026-09 (Chunk 2d) from ``try_remote_object.py``'s demo --
first pytest coverage of this path. Runs against a local kernel by
default; override with ``CUBEVIS_TEST_KERNEL`` for a real sshpyk kernel.
"""
from __future__ import annotations

import os

import numpy as np
import pytest
import pytest_asyncio
from jupyter_client import AsyncKernelManager

from cubevis.remote import RemoteAppLink, DEFAULT_WORKER_TARGET_NAME

KERNEL_NAME = os.environ.get("CUBEVIS_TEST_KERNEL", "python3")
REGISTER_FUNCTION = "cubevis.remote._test_registrations:register_basic"


@pytest_asyncio.fixture
async def obj_ctx():
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
async def test_counter_state_persists_across_calls(obj_ctx):
    """Real, mutating state in the worker's own memory -- not a
    stateless RPC that happens to look stateful."""
    handle = await obj_ctx.create_object("Counter", args=[10])
    try:
        v1 = await obj_ctx.call_method(handle, "increment", args=[5])
        v2 = await obj_ctx.call_method(handle, "increment", kwargs={"by": 1})
        assert v1 == 15
        assert v2 == 16
    finally:
        await obj_ctx.dispose_object(handle)


@pytest.mark.asyncio
async def test_disposed_handle_errors_cleanly_not_a_crash(obj_ctx):
    handle = await obj_ctx.create_object("Counter", args=[0])
    assert await obj_ctx.dispose_object(handle) is True
    reply = await obj_ctx.dispatch_fast(
        "call_method",
        {"handle": handle, "method": "increment", "args": [], "kwargs": {}},
    )
    assert isinstance(reply, dict) and "error" in reply


@pytest.mark.asyncio
async def test_numpy_array_round_trips_through_the_real_wire(obj_ctx):
    handle = await obj_ctx.create_object("NumpyEcho")
    try:
        original = np.arange(6, dtype="float64").reshape(2, 3)
        doubled = await obj_ctx.call_method(handle, "double", args=[original])
        assert np.array_equal(np.asarray(doubled), original * 2)
    finally:
        await obj_ctx.dispose_object(handle)


@pytest.mark.asyncio
async def test_close_reports_clean_worker_exit():
    """close() must confirm the worker subprocess actually exits, not
    just that the request/reply plumbing returned something -- needs
    its own kernel (not the shared fixture) since it exercises close()
    itself rather than teardown after a test."""
    km = AsyncKernelManager(kernel_name=KERNEL_NAME)
    await km.start_kernel()
    try:
        link = await RemoteAppLink.open(
            km, worker_target_name=DEFAULT_WORKER_TARGET_NAME, timeout=60.0)
        await link.create_context(
            config={"register_function": REGISTER_FUNCTION}, timeout=180.0)
        results = await link.close()
        assert len(results) == 1
        (result,) = results.values()
        assert result["closed"] is True
        assert result["returncode"] == 0
    finally:
        await km.shutdown_kernel()
