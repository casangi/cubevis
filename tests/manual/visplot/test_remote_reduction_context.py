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

**Against a real (non-local) ``CUBEVIS_TEST_KERNEL``, the data must be
reachable at potentially two different paths.** MS/PS is opened
directly by this process for the ``local_reader`` fixture (the same
variable every other test file in a full run also reads), while
``RemoteReductionContext`` opens its own copy inside a worker
subprocess on the *kernel's* host, in that subprocess's own working
directory. Those are trivially the same path for local-kernel testing
(same host) -- but for a real remote kernel they're frequently not
(different mount layout, different home directory), and running the
whole suite in one invocation makes picking a single MS/PS value that
resolves correctly on both hosts hard to arrange.

``CUBEVIS_TEST_KERNEL_MS``/``CUBEVIS_TEST_KERNEL_PS`` -- optional, read only by this file -- override the
path handed to ``RemoteReductionContext`` specifically, without
touching what MS/PS means for ``local_reader`` or for any other test
file in the run. Each pairs with its same-kind local variable (``CUBEVIS_TEST_KERNEL_MS``
with ``MS``, ``CUBEVIS_TEST_KERNEL_PS`` with ``PS``) and falls back to that variable's own
value when unset -- so plain ``MS=...``/``PS=...`` (today's usage, and
every other test file's only option) keeps working unchanged for
local-kernel testing; only set ``CUBEVIS_TEST_KERNEL_MS``/``CUBEVIS_TEST_KERNEL_PS`` when the
remote-resolvable path genuinely differs from the local one. That gap
is exactly what caused the first real ``zuul06`` run of this suite
(2026-09) to fail at ``create_object()`` with a plain remote
``FileNotFoundError``: the local MS/PS value doesn't exist on zuul06's
own filesystem at that path, and there was no way at the time to tell
this file to use a different one for the remote leg specifically.

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
Note that ``test_call_timeout_constructor_default_applies_everywhere``'s
own tiny timeout only has something to time out once construction gets
past ``create_object()`` -- with a still-wrong remote path (CUBEVIS_TEST_KERNEL_MS/CUBEVIS_TEST_KERNEL_PS
unset or itself wrong) it never gets that far, and reports the same
``RemoteBackendError`` as everything else in this file rather than the
``TimeoutError`` it's actually checking for. Not a flaw in the timeout
mechanism -- see the paragraph above.

``test_query_columns_matches_local``/``test_probe_scatter_region_matches_local``
(added after a Chunk 2 status review found the gap): every other test
in this file exercises ``query_raster`` only -- scatter's remote wire
path (``query_columns``'s bounded, server-side-rendered
``ScatterRenderResult``, and ``probe_scatter_region``'s click-to-exact
identity computation) had never been called through
``RemoteReductionContext`` at all before these two, against either a
local kernel or real ``zuul06``. Both compare remote output to the
equivalent local call field-by-field. One representation wrinkle,
not a correctness issue: tuple-typed fields nested inside a dataclass
or a plain dict (``x_range``, ``id_grid_x_range``, ``t_range``, etc.)
come back as lists after a real wire round trip -- generic
dataclass/dict wire serialization has no separate tuple type -- so
those fields are compared element-wise (``tuple(...) ==
tuple(...)``) rather than with a type-sensitive ``==``. Dict *keys*
that are themselves tuples (``(Axis, polarization)`` in
``probe_scatter_region``'s return) were checked and do NOT have this
problem -- confirmed by running the test, not assumed.

Example Execution:

ulimit -n 8096 && MS=sis14_twhya_calibrated_flagged.ms CUBEVIS_TEST_KERNEL=cvpost106_python312 CUBEVIS_TEST_KERNEL_MS=/home/zuul06-2/dschieb/casa/visplot/sis14_twhya_calibrated_flagged.ms pytest test_remote_reduction_context.py
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
from cubevis.toolbox.visplot.data.reader import ScatterLayerSpec

KERNEL_NAME = os.environ.get("CUBEVIS_TEST_KERNEL", "python3")


def _backend_paths_and_kind():
    """Resolve (local_path, remote_path, kind).

    local_path is what ``local_reader`` opens directly, in this
    process. remote_path is what gets handed to
    ``RemoteReductionContext``, which opens it in a worker subprocess
    on the kernel's own host -- see module docstring for why these can
    differ, and for CUBEVIS_TEST_KERNEL_MS/CUBEVIS_TEST_KERNEL_PS.
    """
    ms = os.environ.get("MS")
    ps = os.environ.get("PS")
    if ms and ps:
        pytest.fail("Both MS and PS are set -- ambiguous, unset one")
    if ms:
        kind, local_path, remote_env = "msv2", ms, "CUBEVIS_TEST_KERNEL_MS"
    elif ps:
        kind, local_path, remote_env = "msv4", ps, "CUBEVIS_TEST_KERNEL_PS"
    else:
        pytest.skip("Neither MS nor PS set")

    remote_path = os.environ.get(remote_env, local_path)
    return local_path, remote_path, kind


@pytest.fixture(scope="module")
def backend_paths_and_kind():
    return _backend_paths_and_kind()


@pytest.fixture
def remote_ctx(backend_paths_and_kind):
    _local_path, remote_path, kind = backend_paths_and_kind
    ctx = RemoteReductionContext(remote_path, KERNEL_NAME, backend_kind=kind)
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture
def local_reader(backend_paths_and_kind):
    local_path, _remote_path, kind = backend_paths_and_kind
    if kind == "msv2":
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend as Backend
    else:
        from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend as Backend
    backend = Backend(local_path)
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


def test_query_columns_matches_local(remote_ctx, local_reader):
    """Scatter's remote wire path: ``ScatterLayerSpec`` out,
    ``ScatterRenderResult`` back -- an RGBA image plus the coarse
    per-layer identity-grid arrays ``InfoTool``'s hover path relies on
    (see the Chunk 2c section of the implementation doc). Nothing in
    this file exercised this before -- every other test here only calls
    ``query_raster``, and scatter's aggregation shape (server-side
    binning into a bounded, already-rendered result -- see that same
    doc section for how this differs from raster) had never been
    confirmed over the actual remote wire at all.
    """
    meta = local_reader.metadata()
    t0, t1 = meta["time_range"]
    selection = SelectionSpec(
        time_range=(t0, t0 + (t1 - t0) * 0.15), channel_range=(0, 48),
    )
    layers = [
        ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX", cmap=("black", "white")),
        ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="YY", cmap=("black", "white")),
    ]
    remote_result = remote_ctx.query_columns(
        xaxis=Axis.TIME, layers=layers, selection=selection, width=200, height=150,
    )
    local_result = local_reader.query_columns(
        xaxis=Axis.TIME, layers=layers, selection=selection, width=200, height=150,
    )

    # x_range/y_range are tuple fields on a dataclass; after a real wire
    # round trip they come back as lists (generic dataclass/JSON-shaped
    # serialization has no separate tuple type) -- a representation
    # difference, not a computational one, so compare element-wise
    # rather than with a type-sensitive ==.
    assert tuple(remote_result.x_range) == tuple(local_result.x_range)
    assert tuple(remote_result.y_range) == tuple(local_result.y_range)
    assert remote_result.canvas_width == local_result.canvas_width
    assert remote_result.canvas_height == local_result.canvas_height
    assert len(remote_result.layers) == len(local_result.layers)

    array_fields = (
        "hist_counts", "hist_edges", "mapping_x", "mapping_u",
        "id_grid_t_lo", "id_grid_t_hi", "id_grid_bl_lo", "id_grid_bl_hi",
        "id_grid_freq_lo", "id_grid_freq_hi", "id_grid_value",
    )
    for r_lyr, l_lyr in zip(remote_result.layers, local_result.layers):
        assert np.array_equal(r_lyr.image, l_lyr.image)
        assert r_lyr.n_in_view == l_lyr.n_in_view
        assert r_lyr.skip_reason == l_lyr.skip_reason
        if l_lyr.peak_value is None:
            assert r_lyr.peak_value is None
        else:
            assert r_lyr.peak_value == pytest.approx(l_lyr.peak_value)
        for field in array_fields:
            r_val, l_val = getattr(r_lyr, field), getattr(l_lyr, field)
            if l_val is None:
                assert r_val is None
            else:
                assert np.allclose(r_val, l_val, equal_nan=True)
        assert tuple(r_lyr.id_grid_x_range) == tuple(l_lyr.id_grid_x_range)
        assert tuple(r_lyr.id_grid_y_range) == tuple(l_lyr.id_grid_y_range)


def test_probe_scatter_region_matches_local(remote_ctx, local_reader):
    """``InfoTool``'s backend computation (click-to-exact identity) over
    the remote wire -- the other real gap alongside ``query_columns``
    above: this always makes a real backend round trip (see
    ``RemoteReductionContext.probe_scatter_region``'s own docstring on
    why it needs no ``_wire_types`` registration), so it's exactly the
    kind of call a remote session actually issues per click/drag, not
    just per pan/zoom.

    Uses a deliberately large rectangle (60% of the full extent), same
    reasoning as ``TestProbeRegion`` in ``test_visibility_scatter.py``:
    a click-sized window too often lands on an empty cell to reliably
    exercise the "found data" path.
    """
    meta = local_reader.metadata()
    t0, t1 = meta["time_range"]
    selection = SelectionSpec(
        time_range=(t0, t0 + (t1 - t0) * 0.5), channel_range=(0, 48),
    )
    extent = local_reader.query_columns(
        xaxis=Axis.TIME,
        layers=[ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                                  cmap=("black", "white"))],
        selection=selection, width=64, height=48,
    )
    x0, x1 = extent.x_range
    y0, y1 = extent.y_range
    rx0, rx1 = x0 + (x1 - x0) * 0.2, x0 + (x1 - x0) * 0.8
    ry0, ry1 = y0 + (y1 - y0) * 0.2, y0 + (y1 - y0) * 0.8

    yaxes = [(Axis.AMPLITUDE, "XX"), (Axis.AMPLITUDE, "YY")]
    remote_results = remote_ctx.probe_scatter_region(
        x_axis=Axis.TIME, yaxes=yaxes, selection=selection,
        x_range=(rx0, rx1), y_range=(ry0, ry1), max_samples=200_000,
    )
    local_results = local_reader.probe_scatter_region(
        x_axis=Axis.TIME, yaxes=yaxes, selection=selection,
        x_range=(rx0, rx1), y_range=(ry0, ry1), max_samples=200_000,
    )

    assert set(remote_results.keys()) == set(local_results.keys())
    for key in local_results:
        r, l = remote_results[key], local_results[key]
        assert r["status"] == l["status"]
        assert r["n_samples"] == l["n_samples"]
        # tuple fields inside a plain dict survive the wire round trip
        # as lists, same reasoning as x_range/y_range above -- compare
        # element-wise, None-safe.
        for field in ("t_range", "bl_range", "freq_range"):
            r_val, l_val = r[field], l[field]
            assert (tuple(r_val) if r_val is not None else None) == (
                tuple(l_val) if l_val is not None else None)
        assert (sorted(r["bl_ids"]) if r["bl_ids"] else r["bl_ids"]) == (
            sorted(l["bl_ids"]) if l["bl_ids"] else l["bl_ids"]
        )


def test_call_timeout_constructor_default_applies_everywhere(backend_paths_and_kind):
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
    _local_path, remote_path, kind = backend_paths_and_kind
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        RemoteReductionContext(remote_path, KERNEL_NAME, backend_kind=kind,
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
