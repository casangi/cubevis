"""
Regression coverage for Chunk 2's ``RemoteReductionContext``: a real
MSv2/MSv4 backend running in a remote worker subprocess, driven through
the synchronous ``VisibilityReader``-shaped API ``SyncBridge`` exposes
-- unlike the other three ``test_remote_*`` files, this one's public
API is synchronous (no ``pytest.mark.asyncio`` needed), and it needs
real MS/PS data, not a toy fixture class.

Promoted 2026-09 (Chunk 2d) from ``try_remote_reduction_context.py``'s
demo -- first pytest coverage of this path. Requires the MS or PS
environment variable, exactly like test_msv2_backend.py/
test_msv4_backend.py -- skips if neither is set. Runs against a local
kernel by default; override with ``CUBEVIS_TEST_KERNEL`` for a real
sshpyk kernel.

``query_raster``'s selection in most tests here is deliberately
restricted (a small time/channel window, matching the rest of this
suite) rather than the demo script's unrestricted ``SelectionSpec()``:
an unrestricted query was observed to occasionally exceed
``dispatch_fast``'s 30s default on a cold worker (first MS open in a
fresh subprocess) even though the equivalent local call took ~2s --
retried warm, it passed in ~2.2s, matching local. That looks like the
same cold-start sensitivity ``TestTiming`` documents for the local
scatter pipeline, not a wire-serialization defect (a restricted query
never showed it, and the unrestricted query matched local exactly once
warm). ``RemoteReductionContext`` previously had no way to raise that
budget from its public API at all -- fixed 2026-09 (Chunk 2d) by adding
a ``call_timeout=`` constructor parameter plus a per-call ``timeout=``
on every ``_call``-routed method; see the two timeout-parameter tests
near the end of this file for direct coverage, in both directions.
"""
from __future__ import annotations

import asyncio
import os

import numpy as np
import pytest

from cubevis.toolbox.visplot.remote_reduction_context import (
    RemoteReductionContext, RemoteBackendError,
)
from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.selection import SelectionSpec
from cubevis.toolbox.visplot.local_visibility_reader import LocalVisibilityReader

KERNEL_NAME = os.environ.get("CUBEVIS_TEST_KERNEL", "python3")


def _backend_path_and_kind():
    ms = os.environ.get("MS")
    ps = os.environ.get("PS")
    if ms and ps:
        pytest.fail("Both MS and PS are set -- ambiguous, unset one")
    if ms:
        return ms, "msv2"
    if ps:
        return ps, "msv4"
    pytest.skip("Neither MS nor PS set")


@pytest.fixture(scope="module")
def backend_path_and_kind():
    return _backend_path_and_kind()


@pytest.fixture
def remote_ctx(backend_path_and_kind):
    path, kind = backend_path_and_kind
    ctx = RemoteReductionContext(path, KERNEL_NAME, backend_kind=kind)
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture
def local_reader(backend_path_and_kind):
    path, kind = backend_path_and_kind
    if kind == "msv2":
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend as Backend
    else:
        from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend as Backend
    backend = Backend(path)
    backend.open()
    reader = LocalVisibilityReader(backend)
    try:
        yield reader
    finally:
        backend.close()


def test_metadata_matches_local(remote_ctx, local_reader):
    assert remote_ctx.metadata() == local_reader.metadata()


def test_list_fields_and_spws_are_nonempty(remote_ctx):
    assert remote_ctx.list_fields()
    assert remote_ctx.list_spws()


def test_unknown_method_raises_remote_backend_error_with_traceback(remote_ctx):
    """dispatch_fast's explicit error-checking must surface a real
    remote exception as RemoteBackendError, not a raw dict or an
    unrelated KeyError (developer guide S3)."""
    with pytest.raises(RemoteBackendError) as exc_info:
        remote_ctx._call("this_method_does_not_exist")
    assert exc_info.value.remote_traceback


def test_query_raster_matches_local(remote_ctx, local_reader):
    """The actual wire-serialization test: Axis + SelectionSpec out,
    a real aggregation back. See module docstring for why the selection
    here is restricted rather than the demo's unrestricted one."""
    meta = local_reader.metadata()
    t0, t1 = meta["time_range"]
    selection = SelectionSpec(
        time_range=(t0, t0 + (t1 - t0) * 0.15), channel_range=(0, 48),
    )
    remote_agg, remote_xr, remote_yr, remote_dec = remote_ctx.query_raster(
        y_dim=Axis.BASELINE, x_dim=Axis.TIME, quantity=Axis.AMPLITUDE,
        selection=selection, max_cells=500_000,
    )
    local_agg, local_xr, local_yr, local_dec = local_reader.query_raster(
        y_dim=Axis.BASELINE, x_dim=Axis.TIME, quantity=Axis.AMPLITUDE,
        selection=selection, max_cells=500_000,
    )
    assert remote_agg.shape == local_agg.shape
    assert np.allclose(np.asarray(remote_agg), np.asarray(local_agg), equal_nan=True)
    assert remote_xr == local_xr
    assert remote_yr == local_yr
    assert remote_dec == local_dec


def test_call_timeout_constructor_default_applies_everywhere(backend_path_and_kind):
    """``call_timeout=`` at construction becomes the default budget for
    every ``_call``-routed method that doesn't override it -- including
    the constructor's own final metadata fetch.

    Proven here with a deliberately tiny value so an ordinarily-fast
    call reliably fails, rather than depending on the real cold/warm
    timing variance documented in the module docstring to exercise the
    mechanism -- that variance is real but not reliable enough to
    assert on.

    This also doubles as regression coverage for a real leak this same
    change fixed: that final metadata fetch used to sit outside
    __init__'s cleanup try/except, so a failure here used to leave the
    kernel and worker subprocess running. If that regresses, this test
    would still pass (it only asserts the exception) but a kernel
    process would leak every run -- see the constructor's own comment
    at the fixed call site for the direct process-level verification.
    """
    path, kind = backend_path_and_kind
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        RemoteReductionContext(path, KERNEL_NAME, backend_kind=kind,
                                call_timeout=0.0001)


def test_per_call_timeout_overrides_the_instance_default(remote_ctx):
    """A per-call ``timeout=`` replaces the instance's ``call_timeout``
    for that one call only -- lowering it here to prove the override
    actually reaches ``dispatch_fast`` rather than being silently
    accepted and ignored, then confirming the instance-wide default
    (unaffected by the call above) still works normally afterward."""
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        remote_ctx.metadata(timeout=0.0001)
    assert remote_ctx.metadata()
