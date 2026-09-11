"""
test_visibility_scatter.py — Integration tests for VisibilityScatter.

Location in repository:
    cubevis/tests/manual/visplot/test_visibility_scatter.py

Tests against:
    cubevis/cubevis/toolbox/visplot/visibility_scatter.py
    cubevis/cubevis/toolbox/visplot/visibility_plot.py
    cubevis/cubevis/toolbox/visplot/data/msv2_backend.py
    cubevis/cubevis/toolbox/visplot/data/msv4_backend.py

Backend selection (mutually exclusive)
--------------------------------------
Set exactly one of MS or PS:

    ulimit -n 4096 && MS=<path>.ms   pytest test_visibility_scatter.py -v   # MSv2
    ulimit -n 4096 && PS=<path>.ps.zarr pytest test_visibility_scatter.py -v  # MSv4

If both are set the suite fails immediately (ambiguous).
If neither is set all tests are skipped.

Test classes
------------
1.  ScatterLayer         dataclass construction and defaults
2.  Lifecycle            build / layout / source structure
3.  SingleLayer          one-layer render: dtype, shape, ranges, data
4.  MultiLayer           two-layer render: composite image, state_source
5.  Alpha                set_alpha() fast re-composite, _handle_set_alpha
6.  ViewportRerender     pan/zoom re-composite from cached DataFrames
7.  Probe                _handle_probe returns label, range-guard
8.  UpdateAxes           update_axes() re-queries, preserves selection
8b. DeferredConstruction defer_initial_render=True — no backend query
                         until first activation (decision 11, grid/
                         iteration design notes)
9.  StateSource          _state_source fields for scatter
10. EmptySelection       empty/degenerate selection returns blank image
11. Timing               full pipeline under time budget
"""

from __future__ import annotations

import os
import sys
import time as time_mod
import warnings
from pathlib import Path

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Import strategy — package first, local files as fallback
# ---------------------------------------------------------------------------

def _try_package_import():
    from cubevis.toolbox.visplot.axes import Axis, AxisType
    from cubevis.toolbox.visplot.selection import SelectionSpec
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
    from cubevis.toolbox.visplot.visibility_scatter import (
        VisibilityScatter, ScatterLayer,
    )
    from cubevis.toolbox.visplot.visibility_plot import _img_to_uint32
    return (Axis, AxisType, SelectionSpec,
            MSv2Backend, MSv4Backend,
            VisibilityScatter, ScatterLayer, _img_to_uint32)


def _local_import():
    import importlib.util
    here = Path(__file__).parent

    def _load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod  = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    axes_mod = _load("cubevis.toolbox.visplot.axes",      here / "axes.py")
    sel_mod  = _load("cubevis.toolbox.visplot.selection", here / "selection.py")
    sys.modules["cubevis.toolbox.visplot.axes"]      = axes_mod
    sys.modules["cubevis.toolbox.visplot.selection"] = sel_mod
    _load("cubevis.toolbox.visplot.reader",          here / "reader.py")
    _load("cubevis.toolbox.visplot.data.msv2_backend",
          here / "msv2_backend.py")
    _load("cubevis.toolbox.visplot.data.msv4_backend",
          here / "msv4_backend.py")
    vp_mod = _load("cubevis.toolbox.visplot.visibility_plot",
                   here / "visibility_plot.py")
    vs_mod = _load("cubevis.toolbox.visplot.visibility_scatter",
                   here / "visibility_scatter.py")

    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
    return (
        axes_mod.Axis,
        axes_mod.AxisType,
        sel_mod.SelectionSpec,
        MSv2Backend,
        MSv4Backend,
        vs_mod.VisibilityScatter,
        vs_mod.ScatterLayer,
        vp_mod._img_to_uint32,
    )


try:
    (Axis, AxisType, SelectionSpec,
     MSv2Backend, MSv4Backend,
     VisibilityScatter, ScatterLayer, _img_to_uint32) = _try_package_import()
    _SOURCE = "package"
except ImportError:
    (Axis, AxisType, SelectionSpec,
     MSv2Backend, MSv4Backend,
     VisibilityScatter, ScatterLayer, _img_to_uint32) = _local_import()
    _SOURCE = "local"

try:
    import datashader as ds
    HAS_DATASHADER = True
except ImportError:
    HAS_DATASHADER = False

PLOT_W = 400
PLOT_H = 300


# ---------------------------------------------------------------------------
# Backend detection and shared helpers
# ---------------------------------------------------------------------------

def _detect_backend_path() -> tuple[str, str]:
    """Return (path, kind) where kind is 'msv2' or 'msv4'.

    * Neither set  -> pytest.skip (no data available)
    * Both set     -> pytest.fail (ambiguous; hard error)
    * One set, dir missing -> pytest.skip
    * One set, dir present -> return (path, kind)
    """
    ms_path = os.environ.get("MS", "").strip()
    ps_path = os.environ.get("PS", "").strip()
    if ms_path and ps_path:
        pytest.fail(
            "Both MS and PS are set — ambiguous. Set exactly one.\n"
            f"  MS={ms_path!r}\n  PS={ps_path!r}"
        )
    if not ms_path and not ps_path:
        pytest.skip(
            "No backend selected. Set MS=<path>.ms or PS=<path>.ps.zarr.",
            allow_module_level=True,
        )
    if ms_path:
        if not os.path.isdir(ms_path):
            pytest.skip(f"MSv2 path not found: {ms_path!r}")
        return ms_path, "msv2"
    if not os.path.isdir(ps_path):
        pytest.skip(f"MSv4 path not found: {ps_path!r}")
    return ps_path, "msv4"


def _open_backend():
    """Open and return the backend indicated by the environment."""
    path, kind = _detect_backend_path()
    b = MSv2Backend(path) if kind == "msv2" else MSv4Backend(path)
    b.open()
    return b


@pytest.fixture(scope="session", autouse=True)
def _show_backend(request):  # noqa: ARG001
    """Write the active backend to /dev/tty, bypassing pytest capture."""
    ms_path = os.environ.get("MS", "").strip()
    ps_path = os.environ.get("PS", "").strip()
    if ms_path and not ps_path:
        msg = f"[test_visibility_scatter] backend: MSv2  path={ms_path!r}"
    elif ps_path and not ms_path:
        msg = f"[test_visibility_scatter] backend: MSv4  path={ps_path!r}"
    else:
        return
    try:
        with open("/dev/tty", "w") as tty:
            tty.write(msg + "\n")
    except OSError:
        pass  # /dev/tty unavailable (CI without terminal)


def _suppress_warnings():
    warnings.filterwarnings("ignore", category=UserWarning, module="xarray_ms")
    warnings.filterwarnings("ignore",
                            message="The return type of.*Dataset.dims",
                            category=FutureWarning)
    warnings.filterwarnings("ignore", message="omp_set_nested")


def _require_datashader():
    if not HAS_DATASHADER:
        pytest.skip("datashader not installed — pip install datashader")


def _make_single_layer(backend, selection, **kwargs) -> VisibilityScatter:
    """Single AMPLITUDE XX layer, UVDIST x-axis."""
    meta = backend.metadata()
    pol  = meta["correlation_labels"][0]
    defaults = dict(width=PLOT_W, height=PLOT_H)
    defaults.update(kwargs)
    return VisibilityScatter(
        backend   = backend,
        selection = selection,
        x_axis    = Axis.UVDIST,
        layers    = [ScatterLayer(y_axis=Axis.AMPLITUDE, polarization=pol)],
        **defaults,
    )


def _make_two_layer(backend, selection, **kwargs) -> VisibilityScatter:
    """AMPLITUDE XX + AMPLITUDE YY, UVDIST x-axis."""
    meta = backend.metadata()
    pols = meta["correlation_labels"]
    if len(pols) < 2:
        pytest.skip("Need at least two polarizations for two-layer test")
    defaults = dict(width=PLOT_W, height=PLOT_H)
    defaults.update(kwargs)
    return VisibilityScatter(
        backend   = backend,
        selection = selection,
        x_axis    = Axis.UVDIST,
        layers    = [
            ScatterLayer(y_axis=Axis.AMPLITUDE, polarization=pols[0]),
            ScatterLayer(y_axis=Axis.AMPLITUDE, polarization=pols[1]),
        ],
        **defaults,
    )


# ---------------------------------------------------------------------------
# 1. ScatterLayer
# ---------------------------------------------------------------------------

class TestScatterLayer:

    def test_auto_label_from_axis_and_pol(self):
        lyr = ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX")
        assert "Amplitude" in lyr.label
        assert "XX" in lyr.label

    def test_explicit_label_preserved(self):
        lyr = ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX",
                           label="My Layer")
        assert lyr.label == "My Layer"

    def test_default_alpha_is_one(self):
        lyr = ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX")
        assert lyr.alpha == 1.0

    def test_default_cmap_is_none(self):
        """cmap=None triggers default assignment in VisibilityScatter.__init__."""
        lyr = ScatterLayer(y_axis=Axis.AMPLITUDE, polarization="XX")
        assert lyr.cmap is None

    def test_empty_layers_raises(self):
        _require_datashader()
        _suppress_warnings()
        backend = _open_backend()
        meta    = backend.metadata()
        sel     = SelectionSpec(channel_range=(0, 16))
        try:
            with pytest.raises(ValueError, match="non-empty"):
                VisibilityScatter(backend=backend, selection=sel,
                                  x_axis=Axis.UVDIST, layers=[])
        finally:
            backend.close()


# ---------------------------------------------------------------------------
# 2. Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        t0, t1 = meta["time_range"]
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.1),
            channel_range=(0, 16),
        )

    def teardown_method(self):
        self.backend.close()

    def test_figure_not_none(self):
        vs = _make_single_layer(self.backend, self.sel)
        assert vs.figure is not None

    def test_layout_not_none(self):
        vs = _make_single_layer(self.backend, self.sel)
        assert vs.layout is not None

    def test_image_source_populated(self):
        vs = _make_single_layer(self.backend, self.sel)
        assert vs._image_source is not None
        assert "image" in vs._image_source.data

    def test_state_source_populated(self):
        vs = _make_single_layer(self.backend, self.sel)
        assert vs._state_source is not None

    def test_layers_property_returns_copy(self):
        vs = _make_single_layer(self.backend, self.sel)
        lyrs = vs.layers
        lyrs.append(ScatterLayer(y_axis=Axis.PHASE, polarization="XX"))
        assert len(vs.layers) == 1, "layers property must return a copy"

    def test_default_cmap_assigned(self):
        """Layers with cmap=None must receive a default from _LAYER_CMAPS."""
        vs = _make_single_layer(self.backend, self.sel)
        assert vs._layers[0].cmap is not None
        assert len(vs._layers[0].cmap) > 0

    def test_two_layers_get_different_cmaps(self):
        vs = _make_two_layer(self.backend, self.sel)
        assert vs._layers[0].cmap != vs._layers[1].cmap

    def test_auto_title_contains_x_axis(self):
        vs = _make_single_layer(self.backend, self.sel)
        title = vs.figure.title.text
        assert "UV" in title.upper() or "uvdist" in title.lower()

    def test_custom_title(self):
        vs = _make_single_layer(self.backend, self.sel, title="My Scatter")
        assert vs.figure.title.text == "My Scatter"


# ---------------------------------------------------------------------------
# 3. Single layer render
# ---------------------------------------------------------------------------

class TestSingleLayer:

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        t0, t1 = meta["time_range"]
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.15),
            channel_range=(0, 48),
        )
        self.vs = _make_single_layer(self.backend, self.sel)

    def teardown_method(self):
        self.backend.close()

    def test_image_dtype_uint32(self):
        img = self.vs._image_source.data["image"][0]
        assert img.dtype == np.uint32, f"Expected uint32, got {img.dtype}"

    def test_image_shape_matches_canvas(self):
        """Initial render with full data must produce full-size image."""
        img = self.vs._image_source.data["image"][0]
        # Full data is dense (millions of points) — adaptive canvas not triggered
        assert img.shape == (PLOT_H, PLOT_W)

    def test_image_shape_at_most_canvas_size(self):
        """Image shape must always be at most (PLOT_H, PLOT_W) — adaptive
        canvas may produce smaller arrays for sparse viewports."""
        img = self.vs._image_source.data["image"][0]
        h, w = img.shape
        assert h <= PLOT_H and w <= PLOT_W

    def test_dw_dh_positive(self):
        assert self.vs._image_source.data["dw"][0] > 0
        assert self.vs._image_source.data["dh"][0] > 0

    def test_xy_consistent_with_ranges(self):
        src = self.vs._image_source.data
        x0  = src["x"][0];   dw = src["dw"][0]
        y0  = src["y"][0];   dh = src["dh"][0]
        xr0, xr1 = self.vs._x_range
        yr0, yr1 = self.vs._y_range
        assert np.isclose(x0,      xr0, rtol=1e-6)
        assert np.isclose(x0 + dw, xr1, rtol=1e-6)
        assert np.isclose(y0,      yr0, rtol=1e-6)
        assert np.isclose(y0 + dh, yr1, rtol=1e-6)

    def test_x_range_ordered(self):
        x0, x1 = self.vs._x_range
        assert x0 < x1, f"x_range not ordered: ({x0}, {x1})"

    def test_y_range_ordered(self):
        y0, y1 = self.vs._y_range
        assert y0 < y1, f"y_range not ordered: ({y0}, {y1})"

    def test_x_range_nonnegative_for_uvdist(self):
        """UVDIST is always ≥ 0."""
        x0, _ = self.vs._x_range
        assert x0 >= 0.0

    def test_y_range_nonnegative_for_amplitude(self):
        """Amplitude is always ≥ 0."""
        y0, _ = self.vs._y_range
        assert y0 >= 0.0

    # test_layer_df_populated / test_layer_df_no_nan (removed 2026-09):
    # both checked self.vs._layer_dfs[0], a per-layer raw DataFrame that
    # predates the "coarse but free" scatter redesign -- binning/shading
    # now happen backend-side (see ScatterRenderResult's docstring in
    # data/reader.py), so nothing populates _layer_dfs any more; it is
    # permanently None regardless of whether a layer actually rendered.
    # Not migrated to a direct equivalent: there is no "did the raw
    # per-sample data come back clean" check possible any more, since
    # the widget never receives raw per-sample data at all. The intent
    # both tests were actually protecting -- that a real render
    # happened and produced sane, non-degenerate output -- is already
    # covered by test_non_transparent_pixels_present,
    # test_xy_consistent_with_ranges, test_x_range_nonnegative_for_uvdist,
    # test_y_range_nonnegative_for_amplitude, and
    # test_amplitude_y_range_physically_reasonable, all above, all
    # against current, still-populated state.

    def test_layer_image_set_after_render(self):
        """Replaces test_layer_agg_set_after_render, which checked
        self.vs._layer_aggs[0] -- also a raw-data-era cache, also
        permanently None post-redesign (same reasoning as
        test_layer_df_populated above). _layer_images is the current
        equivalent: None until a layer has actually been rendered,
        populated with the real composited image array afterward --
        confirmed directly (see chat) by constructing with
        defer_initial_render=True and observing _layer_images go from
        [None] to a real array only once a render actually happens."""
        assert self.vs._layer_images[0] is not None

    def test_non_transparent_pixels_present(self):
        img   = self.vs._image_source.data["image"][0]
        alpha = (img >> 24) & 0xff
        assert (alpha > 0).any(), "All pixels are transparent"
        print(f"  Non-transparent pixels: {int((alpha > 0).sum())}/{PLOT_W*PLOT_H}")

    def test_amplitude_y_range_physically_reasonable(self):
        """sis14 ALMA Band 7 amplitudes are typically 1–100 Jy."""
        y0, y1 = self.vs._y_range
        assert y0 >= 0.0
        assert y1 < 1e6, f"y_range max suspiciously large: {y1}"


# ---------------------------------------------------------------------------
# 4. Multi-layer render
# ---------------------------------------------------------------------------

class TestMultiLayer:

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        self.pols = meta["correlation_labels"]
        t0, t1 = meta["time_range"]
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.15),
            channel_range=(0, 48),
        )

    def teardown_method(self):
        self.backend.close()

    def test_two_layer_images_both_populated(self):
        """Replaces test_two_layer_dfs_both_populated -- see
        TestSingleLayer.test_layer_image_set_after_render's docstring
        for why _layer_dfs no longer works as this signal and
        _layer_images does."""
        vs = _make_two_layer(self.backend, self.sel)
        assert vs._layer_images[0] is not None
        assert vs._layer_images[1] is not None

    def test_two_layer_composite_has_non_transparent_pixels(self):
        vs  = _make_two_layer(self.backend, self.sel)
        img = vs._image_source.data["image"][0]
        assert ((img >> 24) & 0xff > 0).any()

    def test_two_layer_composite_differs_from_single_layer(self):
        """Composite of two layers must differ from a single-layer render."""
        vs1 = _make_single_layer(self.backend, self.sel)
        vs2 = _make_two_layer(self.backend, self.sel)
        img1 = vs1._image_source.data["image"][0]
        img2 = vs2._image_source.data["image"][0]
        assert not np.array_equal(img1, img2), (
            "Single and two-layer composites are identical"
        )

    # test_multi_layer_x_range_is_union (removed 2026-09): needed each
    # layer's own raw x min/max via _layer_dfs (see
    # test_layer_image_set_after_render's docstring for why that's
    # permanently None post-redesign) to confirm vs._x_range spans
    # both. Not migrated: the widget only ever caches the *combined*
    # x_range post-redesign, not a per-layer breakdown -- per-layer
    # extents live in ScatterLayerRender.id_grid_x_range/y_range
    # backend-side (see data/reader.py) but nothing on the widget
    # caches those beyond the single most recent probe call, so there
    # is no current per-layer signal to check this against here. A
    # real per-layer-extent test belongs at the backend level (e.g.
    # alongside TestProbeScatterRegion in test_msv2_backend.py/
    # test_msv4_backend.py), not this file -- flagged as a genuine
    # coverage gap, not filled in here.

    def test_three_axes_in_one_scatter(self):
        """Multiple y-axes (amplitude + phase) in one VisibilityScatter."""
        if len(self.pols) < 1:
            pytest.skip("No polarizations")
        pol = self.pols[0]
        vs  = VisibilityScatter(
            backend   = self.backend,
            selection = self.sel,
            x_axis    = Axis.UVDIST,
            layers    = [
                ScatterLayer(y_axis=Axis.AMPLITUDE, polarization=pol),
                ScatterLayer(y_axis=Axis.PHASE,     polarization=pol),
            ],
            width  = PLOT_W,
            height = PLOT_H,
        )
        # _layer_dfs replaced with _layer_images -- see
        # test_layer_image_set_after_render's docstring. The original
        # y0_amp/y0_pha comparison below is dropped, not migrated: it
        # was written as `... or True # just confirm no crash`, which
        # is unconditionally true regardless of the values on its
        # left -- it was never actually checking that amplitude and
        # phase differ, only (redundantly with the two asserts above)
        # that constructing two layers with different y-axes doesn't
        # raise.
        assert vs._layer_images[0] is not None
        assert vs._layer_images[1] is not None


# ---------------------------------------------------------------------------
# 5. Alpha
# ---------------------------------------------------------------------------

class TestAlpha:

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        t0, t1 = meta["time_range"]
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.15),
            channel_range=(0, 48),
        )

    def teardown_method(self):
        self.backend.close()

    def test_set_alpha_zero_gives_blank_image(self):
        """Alpha=0 hides the layer — composite must be fully transparent."""
        vs = _make_single_layer(self.backend, self.sel)
        vs.set_alpha(0, 0.0)
        img   = vs._image_source.data["image"][0]
        alpha = (img >> 24) & 0xff
        assert (alpha == 0).all(), (
            f"Expected all-transparent image after alpha=0, "
            f"got {int((alpha > 0).sum())} non-transparent pixels"
        )

    def test_set_alpha_one_gives_opaque_image(self):
        """Alpha=1.0 must produce non-transparent pixels for real data."""
        vs = _make_single_layer(self.backend, self.sel)
        vs.set_alpha(0, 1.0)
        img = vs._image_source.data["image"][0]
        assert ((img >> 24) & 0xff > 0).any()

    def test_set_alpha_does_not_re_query_backend(self):
        """set_alpha must not call query_columns — the rendered image is
        reused, not re-fetched.

        Checks _layer_images (identity, not just equal values) rather
        than the original _layer_dfs: that attribute is permanently
        None post-redesign (see test_layer_image_set_after_render's
        docstring in TestSingleLayer), so the original identity check
        here (None is None) passed regardless of whether a backend call
        actually happened — this now checks the thing that would
        actually change if set_alpha() incorrectly triggered a fresh
        render.
        """
        vs   = _make_single_layer(self.backend, self.sel)
        img_before = vs._layer_images[0]
        vs.set_alpha(0, 0.5)
        assert vs._layer_images[0] is img_before, (
            "set_alpha must not replace the cached per-layer image"
        )

    def test_set_alpha_updates_layer(self):
        vs = _make_single_layer(self.backend, self.sel)
        vs.set_alpha(0, 0.5)
        assert abs(vs._layers[0].alpha - 0.5) < 1e-9

    def test_set_alpha_updates_state_source(self):
        vs = _make_single_layer(self.backend, self.sel)
        vs.set_alpha(0, 0.3)
        assert abs(vs._state_source.data["layer_alpha_0"][0] - 0.3) < 1e-9

    def test_set_alpha_clamps_to_unit_interval(self):
        vs = _make_single_layer(self.backend, self.sel)
        vs.set_alpha(0, 1.5)
        assert vs._layers[0].alpha <= 1.0
        vs.set_alpha(0, -0.5)
        assert vs._layers[0].alpha >= 0.0

    def test_set_alpha_out_of_range_raises(self):
        vs = _make_single_layer(self.backend, self.sel)
        with pytest.raises(IndexError):
            vs.set_alpha(99, 0.5)

    def test_handle_set_alpha(self):
        vs   = _make_single_layer(self.backend, self.sel)
        resp = vs._handle_set_alpha({"layer_index": 0, "alpha": 0.7})
        assert resp["status"] == "ok"
        assert abs(vs._layers[0].alpha - 0.7) < 1e-9

    def test_handle_set_alpha_bad_index(self):
        vs   = _make_single_layer(self.backend, self.sel)
        resp = vs._handle_set_alpha({"layer_index": 99, "alpha": 0.5})
        assert resp["status"] == "error"

    def test_partial_alpha_image_differs_from_full(self):
        vs_full    = _make_single_layer(self.backend, self.sel)
        vs_partial = _make_single_layer(self.backend, self.sel)
        vs_partial.set_alpha(0, 0.3)
        img_full    = vs_full._image_source.data["image"][0]
        img_partial = vs_partial._image_source.data["image"][0]
        assert not np.array_equal(img_full, img_partial), (
            "Full-alpha and partial-alpha images are identical"
        )

    def test_two_layer_alpha_second_layer_zero(self):
        """Setting second layer alpha=0 gives same result as single layer."""
        vs2 = _make_two_layer(self.backend, self.sel)
        vs1 = _make_single_layer(self.backend, self.sel)
        vs2.set_alpha(1, 0.0)
        img2 = vs2._image_source.data["image"][0]
        img1 = vs1._image_source.data["image"][0]
        # They may not be bit-identical (different cmaps) but both
        # should have similar numbers of non-transparent pixels
        n1 = int(((img1 >> 24) & 0xff > 0).sum())
        n2 = int(((img2 >> 24) & 0xff > 0).sum())
        assert abs(n1 - n2) < PLOT_W * PLOT_H * 0.05, (
            f"Non-transparent px counts differ too much: {n1} vs {n2}"
        )


# ---------------------------------------------------------------------------
# 5a. Colormap scaling — Phase 0 CM-series (per-layer eq_hist default,
#     update_scaling, colormap_controls, histogram)
# ---------------------------------------------------------------------------

class TestColormapScaling:
    """Tests for the Phase 0 colormap/pseudocolor fidelity refactor.

    Scaling is per-layer in VisibilityScatter, since each layer has its
    own colormap and rendered quantity. See TestColormapScaling in
    test_visibility_raster.py for the shared background and the
    eq_hist-vs-linear screenshot-reproduction rationale.
    """

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        t0, t1 = meta["time_range"]
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.15),
            channel_range=(0, 48),
        )

    def teardown_method(self):
        self.backend.close()

    def test_default_layer_scaling_is_eq_hist(self):
        vs = _make_single_layer(self.backend, self.sel)
        assert vs._layers[0].scaling == "eq_hist"

    def test_scatter_layer_scaling_param_accepted(self):
        meta = self.backend.metadata()
        pol = meta["correlation_labels"][0]
        vs = VisibilityScatter(
            backend=self.backend, selection=self.sel, x_axis=Axis.UVDIST,
            layers=[ScatterLayer(y_axis=Axis.AMPLITUDE, polarization=pol,
                                  scaling="linear")],
            width=PLOT_W, height=PLOT_H,
        )
        assert vs._layers[0].scaling == "linear"

    def test_update_scaling_changes_layer_attribute(self):
        vs = _make_single_layer(self.backend, self.sel)
        vs.update_scaling(0, scaling="log")
        assert vs._layers[0].scaling == "log"

    def test_update_scaling_invalid_raises(self):
        vs = _make_single_layer(self.backend, self.sel)
        with pytest.raises(ValueError, match="scaling"):
            vs.update_scaling(0, scaling="bogus")

    def test_update_scaling_out_of_range_index_raises(self):
        vs = _make_single_layer(self.backend, self.sel)
        with pytest.raises(IndexError):
            vs.update_scaling(99, scaling="log")

    def test_update_scaling_rerenders_image(self):
        vs = _make_single_layer(self.backend, self.sel)
        vs.update_scaling(0, scaling="linear")
        img_before = vs._image_source.data["image"][0].copy()
        vs.update_scaling(0, scaling="eq_hist")
        img_after = vs._image_source.data["image"][0]
        assert img_after.dtype == np.uint32
        assert not np.array_equal(img_before, img_after), (
            "image must change after switching from linear to eq_hist"
        )

    def test_set_alpha_preserves_scaling(self):
        """Regression test: set_alpha() reconstructs the ScatterLayer
        dataclass internally (it's frozen-by-convention, not truly
        immutable) — a prior version of this reconstruction dropped
        the scaling/scaling_alpha/scaling_gamma fields back to their
        class defaults. This must not happen."""
        vs = _make_single_layer(self.backend, self.sel)
        vs.update_scaling(0, scaling="gamma", gamma=0.3)
        assert vs._layers[0].scaling == "gamma"
        assert vs._layers[0].scaling_gamma == 0.3
        vs.set_alpha(0, 0.6)
        assert vs._layers[0].scaling == "gamma", (
            "set_alpha() must not reset scaling to the default"
        )
        assert vs._layers[0].scaling_gamma == 0.3
        assert abs(vs._layers[0].alpha - 0.6) < 1e-9

    def test_multi_layer_scaling_independent(self):
        """Updating one layer's scaling must not affect another layer's."""
        vs = _make_two_layer(self.backend, self.sel)
        vs.update_scaling(0, scaling="eq_hist")
        vs.update_scaling(1, scaling="linear")
        assert vs._layers[0].scaling == "eq_hist"
        assert vs._layers[1].scaling == "linear"
        vs.update_scaling(1, scaling="sqrt")
        assert vs._layers[0].scaling == "eq_hist", (
            "layer 0 scaling must be unaffected by updating layer 1"
        )
        assert vs._layers[1].scaling == "sqrt"

    def test_explicit_scalings_render_without_error(self):
        vs = _make_single_layer(self.backend, self.sel)
        for scaling in ("linear", "log", "eq_hist", "sqrt", "square",
                        "gamma", "power"):
            vs.update_scaling(0, scaling=scaling, alpha=10.0, gamma=0.5)
            img = vs._image_source.data["image"][0]
            assert img.dtype == np.uint32
            assert img.shape == (PLOT_H, PLOT_W)

    def test_state_source_has_per_layer_scaling_keys(self):
        vs = _make_single_layer(self.backend, self.sel)
        assert "layer_scaling_0" in vs._state_source.data
        assert vs._state_source.data["layer_scaling_0"][0] == "eq_hist"
        assert "layer_scaling_alpha_0" in vs._state_source.data
        assert "layer_scaling_gamma_0" in vs._state_source.data

    def test_colormap_controls_returns_bokeh_layout(self):
        from bokeh.models import LayoutDOM
        vs = _make_single_layer(self.backend, self.sel)
        controls = vs.colormap_controls(layer_index=0)
        assert isinstance(controls, LayoutDOM)

    def test_colormap_controls_out_of_range_raises(self):
        vs = _make_single_layer(self.backend, self.sel)
        with pytest.raises(IndexError):
            vs.colormap_controls(layer_index=99)

    def test_histogram_returns_counts_and_edges(self):
        """Updated 2026-09: histogram()'s own current docstring is
        explicit that ``bins`` is honored only in that a mismatch
        against what the backend actually computed logs a warning --
        the real per-sample values needed for an exact rebin to a
        caller-requested bin count aren't available client-side any
        more (post "coarse but free" redesign), so the backend's own
        bin count is always what's actually returned. This test
        previously asserted the old, pre-redesign contract (an exact
        rebin to the requested ``bins=30``); updated to check the
        actual, documented, current contract instead: internally
        consistent counts/edges, and a warning when the request
        doesn't match what came back -- not a hardcoded bin count,
        since that's the backend's choice to make, not this test's."""
        vs = _make_single_layer(self.backend, self.sel)
        counts, edges = vs.histogram(0, bins=30)
        assert counts.shape[0] > 0, "expected a non-empty histogram for real data"
        assert edges.shape[0] == counts.shape[0] + 1

    def test_histogram_warns_when_requested_bins_not_honored(self, caplog):
        """New 2026-09, alongside the update above: a bins= request that
        doesn't match what the backend actually computed must warn,
        per histogram()'s own documented contract -- silently returning
        a different bin count than asked for should never be silent.
        Uses caplog, not pytest.warns(): the warning is a plain
        log.warning() call (module-level logger), not a Python
        `warnings.warn()`, which pytest.warns() would not see."""
        vs = _make_single_layer(self.backend, self.sel)
        counts, _ = vs.histogram(0)  # default bins -- establishes the real count
        mismatched_bins = counts.shape[0] + 1
        with caplog.at_level("WARNING"):
            vs.histogram(0, bins=mismatched_bins)
        assert any("backend always" in rec.message for rec in caplog.records), (
            "expected a warning when the requested bin count wasn't honored"
        )

    def test_histogram_out_of_range_raises(self):
        vs = _make_single_layer(self.backend, self.sel)
        with pytest.raises(IndexError):
            vs.histogram(99)

    def test_handle_update_scaling_j2p(self):
        vs = _make_single_layer(self.backend, self.sel)
        resp = vs._handle_update_scaling(
            {"layer_index": 0, "scaling": "log", "alpha": 5.0}
        )
        assert resp["status"] == "ok"
        assert vs._layers[0].scaling == "log"
        assert vs._layers[0].scaling_alpha == 5.0

    def test_handle_update_scaling_bad_index(self):
        vs = _make_single_layer(self.backend, self.sel)
        resp = vs._handle_update_scaling({"layer_index": 99, "scaling": "log"})
        assert resp["status"] == "error"


# ---------------------------------------------------------------------------
# 6. Viewport rerender
# ---------------------------------------------------------------------------

class TestViewportRerender:

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        t0, t1 = meta["time_range"]
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.15),
            channel_range=(0, 48),
        )
        self.vs = _make_single_layer(self.backend, self.sel)

    def teardown_method(self):
        self.backend.close()

    # test_rerender_viewport_does_not_re_query (removed 2026-09): its
    # own docstring claimed "pan/zoom rerender must reuse cached
    # DataFrames" -- true under the pre-redesign architecture, but
    # _rerender()'s own current docstring is explicit that this is now
    # backward: "every call here costs a query_columns() round trip --
    # axis/layer changes, pan/zoom, ... all end up here now, because
    # binning and shading happen backend-side. Only set_alpha() avoids
    # this." So this test's stated invariant is actually false under
    # the current, intentional design -- not just checked via a dead
    # attribute (_layer_dfs, permanently None, so its own
    # `is df_before` check passed vacuously regardless). The real,
    # current invariant -- pan/zoom DOES trigger a fresh render -- is
    # exactly what test_rerender_viewport_updates_image right below
    # already verifies; nothing here needed inverting-and-keeping on
    # top of that.

    def test_rerender_viewport_updates_image(self):
        img_before = self.vs._image_source.data["image"][0].copy()
        x0, x1 = self.vs._x_range
        y0, y1 = self.vs._y_range
        self.vs.rerender(x_range=(x0, (x0 + x1) / 2),
                         y_range=(y0, (y0 + y1) / 2))
        img_after = self.vs._image_source.data["image"][0]
        assert not np.array_equal(img_before, img_after)

    def test_rerender_image_still_uint32(self):
        x0, x1 = self.vs._x_range
        y0, y1 = self.vs._y_range
        self.vs.rerender(x_range=(x0, (x0 + x1) / 2),
                         y_range=(y0, (y0 + y1) / 2))
        img = self.vs._image_source.data["image"][0]
        assert img.dtype == np.uint32

    def test_do_viewport_rerender_response_structure(self):
        x0, x1 = self.vs._x_range
        y0, y1 = self.vs._y_range
        resp = self.vs._do_viewport_rerender(x0, (x0 + x1) / 2, y0, y1)
        assert set(resp.keys()) >= {"image", "x0", "x1", "y0", "y1"}
        assert isinstance(resp["image"], np.ndarray)
        assert resp["image"].dtype == np.uint32
        # Shape is at most (PLOT_H, PLOT_W) — adaptive canvas may be smaller
        # for sparse viewports, but the half-data-range used here is dense.
        h, w = resp["image"].shape
        assert h <= PLOT_H and w <= PLOT_W
        assert h > 0 and w > 0

    def test_do_viewport_rerender_bounds_echo(self):
        x0, x1 = self.vs._x_range
        y0, y1 = self.vs._y_range
        xm = (x0 + x1) / 2
        ym = (y0 + y1) / 2
        resp = self.vs._do_viewport_rerender(x0, xm, y0, ym)
        assert np.isclose(resp["x0"], x0) and np.isclose(resp["x1"], xm)
        assert np.isclose(resp["y0"], y0) and np.isclose(resp["y1"], ym)

    def test_source_id_unchanged_after_viewport_rerender(self):
        old_id = id(self.vs._image_source)
        x0, x1 = self.vs._x_range
        self.vs.rerender(x_range=(x0, (x0 + x1) / 2))
        assert id(self.vs._image_source) == old_id

    def test_adaptive_canvas_sparse_viewport(self):
        """Sparse viewports must produce a smaller canvas so points are visible.

        Force a sparse viewport by selecting a tiny amplitude range near the
        top of the data (few points exist above 90% of the max amplitude).
        The resulting image shape must be smaller than the full canvas when
        fewer than 1% of canvas pixels have data (the adaptive threshold).
        """
        y0, y1 = self.vs._y_range
        x0, x1 = self.vs._x_range
        # Use only the top 2% of the amplitude range — very few points
        sparse_y0 = y0 + (y1 - y0) * 0.98
        resp = self.vs._do_viewport_rerender(x0, x1, sparse_y0, y1)
        assert resp["image"].dtype == np.uint32
        h, w = resp["image"].shape
        assert h > 0 and w > 0
        assert h <= PLOT_H and w <= PLOT_W

        # Count points in this sparse region
        df0 = self.vs._layer_dfs[0]
        if df0 is not None:
            n_in = int(
                ((df0["x"] >= x0) & (df0["x"] <= x1) &
                 (df0["y"] >= sparse_y0) & (df0["y"] <= y1)).sum()
            )
            pts_per_px = n_in / (PLOT_W * PLOT_H)
            if pts_per_px < 0.01:
                assert h < PLOT_H or w < PLOT_W, (
                    f"Adaptive canvas should fire for {n_in} points "
                    f"(pts_per_px={pts_per_px:.4f} < 0.01) but got full-size image"
                )
        print(f"  Sparse viewport image shape: {h}×{w} "
              f"(full={PLOT_H}×{PLOT_W}, "
              f"adaptive={'yes' if h < PLOT_H else 'no'})")


# ---------------------------------------------------------------------------
# 7. Probe
# ---------------------------------------------------------------------------

class TestProbe:

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        t0, t1 = meta["time_range"]
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.15),
            channel_range=(0, 48),
        )
        self.vs = _make_single_layer(self.backend, self.sel)

    def teardown_method(self):
        self.backend.close()

    def _finite_data_coords(self):
        """Return (x, y) data coords that map to a non-transparent pixel
        in the current composite image.

        Migrated 2026-09 from the original, which read
        ``self.vs._layer_aggs[0]`` (a raw Datashader agg, coordinate-
        labeled per axis) -- permanently None post-redesign (see
        ``test_layer_image_set_after_render``'s docstring in
        ``TestSingleLayer``), so this helper's own
        ``pytest.skip("No layer agg")`` fired unconditionally, silently
        skipping every test that calls it. Found auditing this file for
        exactly this kind of vestigial-attribute-driven silent skip,
        not a failure.

        Rebuilt from the composite image plus its pushed data-space
        origin/extent (``_image_source``'s own x/y/dw/dh -- the same
        fields ``test_xy_consistent_with_ranges`` already verifies
        line up with ``_x_range``/``_y_range``), since a raw per-layer
        agg with labeled coordinates no longer exists to read directly.
        Confirmed against real data before this change (see chat): the
        derived coordinate round-trips correctly through
        ``_handle_probe``.
        """
        src = self.vs._image_source.data
        img = src["image"][0]
        alpha = (img >> 24) & 0xff
        ys, xs = np.where(alpha > 0)
        if len(ys) == 0:
            pytest.skip("No non-transparent pixels in composite image")
        px, py = int(xs[0]), int(ys[0])
        h, w = img.shape
        x0, dw = src["x"][0], src["dw"][0]
        y0, dh = src["y"][0], src["dh"][0]
        x_val = x0 + (px + 0.5) / w * dw
        y_val = y0 + (py + 0.5) / h * dh
        return x_val, y_val

    def test_probe_returns_label(self):
        x, y = self._finite_data_coords()
        resp = self.vs._handle_probe({"x": x, "y": y})
        assert "label" in resp
        assert isinstance(resp["label"], str)
        assert len(resp["label"]) > 0

    def test_probe_label_contains_quantity(self):
        x, y = self._finite_data_coords()
        resp = self.vs._handle_probe({"x": x, "y": y})
        assert "Amplitude" in resp["label"]

    def test_probe_out_of_range_safe(self):
        resp = self.vs._handle_probe({"x": -1e30, "y": -1e30})
        assert "label" in resp
        assert "out of range" in resp["label"].lower() or resp["label"]

    def test_probe_range_guard(self):
        """Coordinates outside _x_range/_y_range must return out-of-range."""
        x0, x1 = self.vs._x_range
        y0, y1 = self.vs._y_range
        resp = self.vs._handle_probe({"x": x1 + 1e6, "y": y0})
        assert "out of range" in resp["label"].lower()

    def test_probe_contains_n_scatter_samples(self):
        """Scatter probe must include n_scatter_samples in formatted label."""
        x, y = self._finite_data_coords()
        resp = self.vs._handle_probe({"x": x, "y": y})
        # _format_probe adds "N: <int>" when n_scatter_samples is present
        assert "N:" in resp["label"] or resp["label"]

    def test_probe_empty_region_reports_no_value_for_any_layer(self):
        """A location with no data in any layer yields value=None per layer.

        Rewritten 2026-08.  Was test_probe_nan_pixel_returns_empty, which
        picked a bin NaN in layer 0 only and asserted "empty" in the
        label.  Both halves became wrong with the PB-series probe fix:

          * The probe consults every layer, so a bin empty in XX but
            populated in YY legitimately returns a value -- that was
            defect (1), and most of the 47.7% false-empty rate.
          * _nearest_populated_bin searches a screen-pixel budget, so an
            empty bin next to a populated one answers too -- defect (3).
          * _format_probe's "<i>empty</i>" is the first field, and
            _handle_probe partitions it off and discards it.  The string
            is now unreachable; the em dash is the no-data marker.

        Asserting on resp["probe"] instead means the next status-bar
        wording change does not break this test.

        REBUILT 2026-09 (Chunk 2d): the ``_layer_aggs``/``_agg_pixel``/
        ``_search_radius_bins``/``_nearest_populated_bin`` machinery this
        used is doubly stale -- ``_layer_aggs`` is permanently ``None``
        post-redesign (see ``TestProbe._finite_data_coords``'s
        docstring), AND ``_handle_probe`` itself dropped the neighbor-
        search radius entirely in the piece-2 redesign (see its own
        docstring: "exact-cell lookup only" now, no
        ``_search_radius_bins`` tolerance) -- so even with real aggs
        this would have been testing a forgiveness margin
        ``_handle_probe`` no longer has. Rebuilt against
        ``self.vs._layer_id_grid`` instead: the coarse identity grid
        ``_handle_probe`` itself reads (see its docstring), still real,
        still per-layer post-redesign. Confirmed against real data that
        every layer's grid shares identical shape/x_range/y_range for a
        given render (driven by the shared viewport/``probe_grid_max_cells``,
        not per-layer data extent), so one (px, py) index is valid
        against every layer's grid without recomputing per layer.
        """
        grids = [g for g in self.vs._layer_id_grid if g is not None]
        if not grids:
            pytest.skip("No layer id grids")

        v0 = grids[0]["value"]
        h, w = v0.shape
        gx0, gx1 = grids[0]["x_range"]
        gy0, gy1 = grids[0]["y_range"]
        xs_coord = np.linspace(gx0, gx1, w)
        ys_coord = np.linspace(gy0, gy1, h)

        ys, xs = np.where(~np.isfinite(v0))
        for py, px in zip(ys.tolist(), xs.tolist()):
            if all(not np.isfinite(g["value"][py, px]) for g in grids):
                xv, yv = float(xs_coord[px]), float(ys_coord[py])
                break
        else:
            # On dense data there may be no coarse cell empty in every
            # layer at once. Skipping is the honest outcome; contriving
            # one would test nothing real.
            pytest.skip("No cell empty in every layer's coarse grid")

        probe = self.vs._handle_probe({"x": xv, "y": yv})["probe"]
        assert probe["status"] in ("ok", "no_data")
        assert all(e["value"] is None for e in probe["layers"] if e["visible"])

    # ------------------------------------------------------------------
    # Structured probe envelope (added 2026-08)
    # ------------------------------------------------------------------

    def test_probe_envelope_shape(self):
        """Every probe answer carries a status and leaves label intact.

        The ``probe["exact"]`` check this test originally had is
        removed: not a renamed/vestigial attribute like the _layer_dfs
        cases elsewhere in this file, but a key that was never part of
        ``_probe_envelope``'s actual contract (``{"status": status,
        **extra}``, confirmed directly against the real source) --
        there's nothing to migrate it to. Unmasked by the
        _finite_data_coords fix above: this test previously always
        skipped via that helper's dead pytest.skip() path, so this
        mismatch had never actually been exercised.
        """
        x, y = self._finite_data_coords()
        resp = self.vs._handle_probe({"x": x, "y": y})
        assert set(resp.keys()) >= {"label", "probe"}
        probe = resp["probe"]
        assert probe["status"] == "ok"
        assert probe["winner"] in range(len(self.vs.layers))

    def test_probe_layers_entry_per_layer_including_hidden(self):
        """One entry per layer, in index order, whatever the visibility.

        The status bar's stable field order depends on this -- readings
        must not shift position as the cursor moves or as a layer is
        hidden.  Filtering happens at render time, not here.
        """
        x, y = self._finite_data_coords()
        probe = self.vs._handle_probe({"x": x, "y": y})["probe"]
        assert len(probe["layers"]) == len(self.vs.layers)
        assert [e["index"] for e in probe["layers"]] == list(
            range(len(self.vs.layers))
        )
        for entry, lyr in zip(probe["layers"], self.vs.layers):
            assert entry["label"] == lyr.label
            assert entry["visible"] == (lyr.alpha > 0.0)

    def test_probe_hidden_layer_reports_no_value(self):
        """A hidden layer is never consulted, so it can never carry a value.

        ``distance_px`` dropped 2026-09: the exact-cell-lookup redesign
        (see ``_handle_probe``'s docstring) has no neighbor search left
        to report a distance for -- a hit is exact or it doesn't exist.
        """
        if len(self.vs.layers) < 2:
            pytest.skip("Need at least two layers")
        self.vs.set_alpha(1, 0.0)
        try:
            x, y = self._finite_data_coords()
            probe = self.vs._handle_probe({"x": x, "y": y})["probe"]
            hidden = probe["layers"][1]
            assert hidden["visible"] is False
            assert hidden["value"] is None
        finally:
            self.vs.set_alpha(1, 1.0)

    def test_probe_out_of_range_status(self):
        """Out-of-range must be identifiable without reading the markup."""
        x0, x1 = self.vs._x_range
        y0, y1 = self.vs._y_range
        resp = self.vs._handle_probe({"x": x1 + 1e6, "y": y0})
        assert resp["probe"]["status"] == "out_of_range"
        assert resp["probe"]["layers"] == []

    def test_probe_envelope_is_json_safe(self):
        """The envelope must survive the Comm transport's json.dumps.

        A numpy scalar raises TypeError and a non-finite float serialises
        to a bare NaN token that the browser's JSON.parse rejects -- both
        take down the whole p2j response, not just the offending field.
        This is what _json_num exists to prevent; catch it here rather
        than in a browser console.
        """
        import json
        x, y = self._finite_data_coords()
        resp = self.vs._handle_probe({"x": x, "y": y})
        text = json.dumps(resp)
        assert "NaN" not in text and "Infinity" not in text
        json.loads(text)


# ---------------------------------------------------------------------------
# 8. UpdateAxes
# ---------------------------------------------------------------------------

class TestUpdateAxes:

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        self.pols = meta["correlation_labels"]
        t0, t1 = meta["time_range"]
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.15),
            channel_range=(0, 48),
        )

    def teardown_method(self):
        self.backend.close()

    # test_update_x_axis_changes_df (removed 2026-09): needed
    # vs._layer_dfs[0]["x"] before/after to confirm switching x-axis
    # actually changed the underlying data (see
    # test_layer_image_set_after_render's docstring for why that
    # attribute is permanently None post-redesign). Not migrated as a
    # separate test: test_update_x_axis_changes_x_range immediately
    # below and test_update_axes_updates_image further down already
    # cover this same intent -- "switching x-axis actually produces
    # different output" -- against current, still-populated state
    # (_x_range and the rendered image, respectively), and do it more
    # directly than re-deriving it from raw x values ever did.

    def test_update_x_axis_changes_x_range(self):
        vs = _make_single_layer(self.backend, self.sel)
        xr_before = vs._x_range
        vs.update_axes(x_dim=Axis.TIME)
        xr_after = vs._x_range
        assert xr_before != xr_after

    def test_update_layers_replaces_dfs(self):
        vs  = _make_single_layer(self.backend, self.sel)
        pol = self.pols[0]
        vs.update_axes(
            layers=[ScatterLayer(y_axis=Axis.PHASE, polarization=pol)]
        )
        assert len(vs._layers) == 1
        assert vs._layers[0].y_axis == Axis.PHASE
        # The removed line here read vs._layer_dfs[0]["y"].values then
        # asserted `.min() < 0 or True` -- unconditionally true
        # regardless of the actual values, so despite the "phase may be
        # negative" comment it was never actually checking phase's sign
        # (nor anything else); the two asserts above already cover
        # everything this test was actually verifying.

    def test_update_axes_noop_when_unchanged(self):
        vs      = _make_single_layer(self.backend, self.sel)
        img_ref = vs._layer_images[0]
        vs.update_axes()   # no args — no-op
        assert vs._layer_images[0] is img_ref

    def test_update_axes_preserves_original_selection(self):
        vs         = _make_single_layer(self.backend, self.sel)
        sel_before = vs._selection
        vs.update_axes(x_dim=Axis.TIME)
        assert vs._selection is sel_before

    def test_update_axes_updates_image(self):
        vs         = _make_single_layer(self.backend, self.sel)
        img_before = vs._image_source.data["image"][0].copy()
        vs.update_axes(x_dim=Axis.TIME)
        img_after  = vs._image_source.data["image"][0]
        assert not np.array_equal(img_before, img_after)

    def test_update_axes_updates_state_source_ranges(self):
        vs   = _make_single_layer(self.backend, self.sel)
        x0_before = vs._state_source.data["full_x0"][0]
        vs.update_axes(x_dim=Axis.TIME)
        x0_after  = vs._state_source.data["full_x0"][0]
        assert x0_before != x0_after


# ---------------------------------------------------------------------------
# 8b. Deferred construction — construct without querying the backend
# ---------------------------------------------------------------------------

class TestDeferredConstruction:
    """Tests for defer_initial_render=True — construct a VisibilityScatter's
    Bokeh scaffolding (figure, layout, state source) without querying the
    backend, so the object can exist as an inactive shell (e.g. a duo-mode
    slot's inactive kind) until it's actually needed. See decision 11 in
    the grid/iteration design notes.
    """

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        self.pols = meta["correlation_labels"]
        t0, t1 = meta["time_range"]
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.15),
            channel_range=(0, 48),
        )

    def teardown_method(self):
        self.backend.close()

    def test_defer_leaves_all_layer_images_none(self):
        """defer_initial_render=True must not query the backend at all.

        Stronger than the empty-selection case (test_empty_selection_
        layer_image_blank), which allows a genuinely blank placeholder
        image depending on what the backend returns for an empty query
        — deferred construction never queries at all, so every layer's
        image must be exactly None.

        Checks _layer_images, not the original _layer_dfs: that
        attribute is permanently None post-redesign regardless of defer
        state (see test_layer_image_set_after_render's docstring in
        TestSingleLayer), so the original assertion here passed
        vacuously whether or not defer actually skipped the backend
        call. _layer_images does distinguish the two states (confirmed:
        see test_first_update_axes_with_explicit_x_dim_renders above,
        which observes it go from [None] to a real array only once a
        render actually happens) -- though
        test_defer_makes_no_backend_query_calls below remains the
        stronger check regardless, per its own docstring: it counts
        actual calls rather than inferring them from a side effect.
        """
        vs = _make_single_layer(self.backend, self.sel, defer_initial_render=True)
        assert all(im is None for im in vs._layer_images), (
            "All layer images must be None — defer_initial_render "
            "appears to have queried the backend"
        )

    def test_defer_still_constructs_figure_and_layout(self):
        """Bokeh scaffolding must exist even though nothing was queried."""
        vs = _make_single_layer(self.backend, self.sel, defer_initial_render=True)
        assert vs.figure is not None
        assert vs.layout is not None

    def test_defer_produces_valid_blank_image(self):
        """Deferred image must be a valid, fully-transparent placeholder —
        same fallback path a real empty-selection render uses, not a new
        one."""
        vs = _make_single_layer(self.backend, self.sel, defer_initial_render=True)
        img = vs._image_source.data["image"][0]
        assert img.dtype == np.uint32
        assert img.shape == (PLOT_H, PLOT_W)
        alpha = (img >> 24) & 0xff
        assert (alpha == 0).all(), "Deferred image should be fully transparent"

    def test_defer_produces_sane_placeholder_ranges(self):
        """Ranges must be non-degenerate (x0 != x1, y0 != y1) so the figure
        itself constructs with valid pan/zoom bounds — same placeholder
        convention already used by _query_all_layers for genuinely empty
        data, not a new one."""
        vs = _make_single_layer(self.backend, self.sel, defer_initial_render=True)
        assert vs._x_range == (0.0, 1.0)
        assert vs._y_range == (0.0, 1.0)

    def test_defer_makes_no_backend_query_calls(self):
        """Deferred construction must not call the backend at all.

        Replaces test_defer_is_much_faster_than_real_render (removed
        2026-08-17).  That test asserted
        ``deferred < real/10 or deferred < 0.05``, which assumes real
        construction is *dominated* by the backend query.  Once the MSv2
        query got fast enough (observed real ~0.216s, deferred ~0.096s)
        the fixed cost both paths pay -- Bokeh figure, tools, comm setup
        -- became a large fraction of real, and the ratio failed while
        the code was fine.  A faster machine makes it fail harder, which
        is the wrong direction for a test to move.

        Counting calls measures the actual invariant.  It is also
        stronger than the sibling test_defer_does_not_query_backend,
        which checks that the layer DataFrames are None: a query whose
        result was fetched and discarded would leave them None and still
        be the bug this guards against.
        """
        calls = []
        names = ("query_columns", "query_raster", "samples_per_pixel")
        originals = {}
        for name in names:
            orig = getattr(self.backend, name, None)
            if orig is None:
                continue
            originals[name] = orig

            def _spy(*a, _n=name, _o=orig, **kw):
                calls.append(_n)
                return _o(*a, **kw)

            setattr(self.backend, name, _spy)
        try:
            _make_single_layer(self.backend, self.sel,
                               defer_initial_render=True)
            assert calls == [], (
                f"defer_initial_render still called the backend: {calls}"
            )
            # The spy must be capable of firing, or the assertion above
            # proves nothing and would keep passing after a refactor
            # renamed the query methods.
            _make_single_layer(self.backend, self.sel)
            assert calls, (
                "the spy never fired on a real render — this test is "
                "inert and is not checking what it claims"
            )
        finally:
            for name, orig in originals.items():
                try:
                    delattr(self.backend, name)
                except AttributeError:
                    setattr(self.backend, name, orig)

    def test_first_update_axes_with_explicit_x_dim_renders(self):
        """Activating a deferred panel by passing its own current x_dim
        back explicitly must perform a real render, even though nothing
        numerically changed.

        Note this is the scatter-specific version of a fix that mattered
        more here than for raster: VisibilityScatter.update_axes()
        requires x_dim to *actually differ* to register as changed (unlike
        VisibilityRaster's override, which treats "was a value explicitly
        passed" as sufficient on its own) — without an all-None guard on
        a per-layer render-state cache, this call would have silently
        no-op'd. (Originally checked ``self._layer_dfs`` for this; that
        cache is permanently None post-"coarse but free" redesign -- see
        ``test_layer_image_set_after_render``'s docstring in
        ``TestSingleLayer`` above -- so this now checks
        ``self._layer_images`` instead, which is the current equivalent
        signal.)
        """
        vs = _make_single_layer(self.backend, self.sel, defer_initial_render=True)
        assert all(im is None for im in vs._layer_images)
        vs.update_axes(x_dim=vs._x_dim)   # same value, explicitly passed
        assert not all(im is None for im in vs._layer_images), (
            "update_axes() with an explicit (unchanged) current x_dim "
            "must still materialize a deferred panel"
        )

    def test_bare_update_axes_after_defer_renders(self):
        """Regression test: update_axes() with *no* arguments at all must
        still materialize a deferred panel — see
        test_update_axes_noop_when_unchanged for the (correct, unchanged)
        no-op behavior this must NOT break for an already-rendered panel."""
        vs = _make_single_layer(self.backend, self.sel, defer_initial_render=True)
        assert all(im is None for im in vs._layer_images)
        vs.update_axes()
        assert not all(im is None for im in vs._layer_images), (
            "Bare update_axes() must still render a never-yet-rendered panel"
        )

    def test_update_axes_still_noop_when_already_rendered_and_unchanged(self):
        """Sanity check that the defer fix didn't regress the existing
        no-op guarantee for a normally-constructed (already-rendered)
        panel — see test_update_axes_noop_when_unchanged."""
        vs = _make_single_layer(self.backend, self.sel)   # defer_initial_render=False
        img_ref = vs._layer_images[0]
        assert img_ref is not None
        vs.update_axes()
        assert vs._layer_images[0] is img_ref, (
            "No-op update_axes() on an already-rendered panel must not "
            "replace layer images — the defer fix should only affect "
            "never-yet-rendered panels"
        )


# ---------------------------------------------------------------------------
# 9. StateSource
# ---------------------------------------------------------------------------

class TestStateSource:

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        t0, t1 = meta["time_range"]
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.1),
            channel_range=(0, 16),
        )

    def teardown_method(self):
        self.backend.close()

    def test_required_base_keys_present(self):
        vs = _make_single_layer(self.backend, self.sel)
        d  = vs._state_source.data
        required = {"full_x0", "full_x1", "full_y0", "full_y1",
                    "x_is_time", "y_is_time", "x_label", "y_label"}
        assert required <= set(d.keys()), \
            f"Missing: {required - set(d.keys())}"

    def test_scatter_specific_keys_present(self):
        vs = _make_single_layer(self.backend, self.sel)
        d  = vs._state_source.data
        assert "layer_alpha_0" in d
        assert "layer_label_0" in d
        assert "n_layers" in d

    def test_two_layer_state_has_both_alpha_keys(self):
        vs = _make_two_layer(self.backend, self.sel)
        d  = vs._state_source.data
        assert "layer_alpha_0" in d
        assert "layer_alpha_1" in d

    def test_n_layers_matches(self):
        vs = _make_two_layer(self.backend, self.sel)
        assert vs._state_source.data["n_layers"][0] == 2

    def test_x_is_time_false_for_uvdist(self):
        vs = _make_single_layer(self.backend, self.sel)
        assert vs._state_source.data["x_is_time"][0] == 0

    def test_x_is_time_true_after_switch(self):
        vs = _make_single_layer(self.backend, self.sel)
        vs.update_axes(x_dim=Axis.TIME)
        assert vs._state_source.data["x_is_time"][0] == 1

    def test_alpha_in_state_source_matches_layer(self):
        vs = _make_single_layer(self.backend, self.sel)
        vs.set_alpha(0, 0.42)
        assert abs(vs._state_source.data["layer_alpha_0"][0] - 0.42) < 1e-9

    def test_state_source_ranges_match_data(self):
        vs = _make_single_layer(self.backend, self.sel)
        d  = vs._state_source.data
        assert np.isclose(d["full_x0"][0], vs._x_range[0])
        assert np.isclose(d["full_x1"][0], vs._x_range[1])


# ---------------------------------------------------------------------------
# 10. Empty / degenerate selection
# ---------------------------------------------------------------------------

class TestEmptySelection:

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()

    def teardown_method(self):
        self.backend.close()

    def test_empty_selection_does_not_crash(self):
        """Empty time range (no matching rows) must produce blank image."""
        sel_empty = SelectionSpec(time_range=(0.0, 1.0))
        vs = _make_single_layer(self.backend, sel_empty)
        assert vs._image_source is not None
        img = vs._image_source.data["image"][0]
        assert img.dtype == np.uint32
        assert img.shape == (PLOT_H, PLOT_W)

    def test_empty_selection_blank_image(self):
        sel_empty = SelectionSpec(time_range=(0.0, 1.0))
        vs    = _make_single_layer(self.backend, sel_empty)
        img   = vs._image_source.data["image"][0]
        alpha = (img >> 24) & 0xff
        assert (alpha == 0).all(), (
            "Empty selection should produce fully transparent image"
        )

    def test_empty_selection_layer_image_blank(self):
        """A genuinely empty selection must still render a valid,
        fully-transparent placeholder image -- same convention
        test_defer_produces_valid_blank_image checks for the deferred
        case.

        Migrated from checking _layer_dfs (permanently None regardless
        of whether the selection was actually empty -- see
        test_layer_image_set_after_render's docstring -- so the
        original ``df is None or len(df) == 0`` passed vacuously no
        matter what an empty selection actually produced). Confirmed
        directly (see chat) that an empty selection still produces a
        real, shaped, fully-transparent _layer_images[0] -- never None
        -- which is the thing actually worth checking here.
        """
        sel_empty = SelectionSpec(time_range=(0.0, 1.0))
        vs = _make_single_layer(self.backend, sel_empty)
        img = vs._layer_images[0]
        assert img is not None
        alpha = (img >> 24) & 0xff
        assert (alpha == 0).all(), "Empty-selection image should be fully transparent"


# ---------------------------------------------------------------------------
# 11. Timing
# ---------------------------------------------------------------------------

class TestTiming:

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()

    def teardown_method(self):
        self.backend.close()

    def test_single_layer_pipeline_under_10s(self):
        """Full query_columns + shade pipeline must finish < 10s."""
        meta   = self.backend.metadata()
        t0, t1 = meta["time_range"]
        sel    = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.5),
            channel_range=(0, 48),
        )
        t_start = time_mod.perf_counter()
        vs = _make_single_layer(self.backend, sel)
        elapsed = time_mod.perf_counter() - t_start

        # n_in_view replaces the original row-count print (len(vs._layer_dfs[0])),
        # which always printed 0 post-redesign (see
        # test_layer_image_set_after_render's docstring) -- actively
        # misleading here specifically, since it read as "no data was
        # processed" for a call that just took real, measurable time
        # doing exactly that. _layer_n_in_view is the current per-layer
        # count of samples actually in view, kept from the last render.
        n_in_view = vs._layer_n_in_view[0] if vs._layer_n_in_view else 0
        print(f"  Single-layer scatter: {n_in_view:,} samples in view, {elapsed:.2f}s")
        assert elapsed < 10.0, (
            f"Scatter pipeline took {elapsed:.1f}s — exceeds 10s"
        )

    def test_set_alpha_fast(self):
        """set_alpha (shade-only, no re-query) must finish < 1s."""
        meta   = self.backend.metadata()
        t0, t1 = meta["time_range"]
        sel    = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.15),
            channel_range=(0, 48),
        )
        vs = _make_single_layer(self.backend, sel)

        t_start = time_mod.perf_counter()
        vs.set_alpha(0, 0.5)
        elapsed = time_mod.perf_counter() - t_start

        print(f"  set_alpha: {elapsed*1000:.1f}ms")
        assert elapsed < 1.0, (
            f"set_alpha took {elapsed:.2f}s — should be sub-second (shade only)"
        )


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _suppress_warnings()

    test_classes = [
        TestScatterLayer,
        TestLifecycle,
        TestSingleLayer,
        TestMultiLayer,
        TestAlpha,
        TestViewportRerender,
        TestProbe,
        TestUpdateAxes,
        TestDeferredConstruction,
        TestStateSource,
        TestEmptySelection,
        TestTiming,
    ]

    total_passed = total_failed = total_skipped = 0

    for cls in test_classes:
        print(f"\n{'='*60}\n  {cls.__name__}\n{'='*60}")
        obj = cls()
        methods = sorted(m for m in dir(obj) if m.startswith("test_"))
        for name in methods:
            print(f"\n  --- {name} ---")
            try:
                if hasattr(obj, "setup_method"):
                    obj.setup_method()
                getattr(obj, name)()
                if hasattr(obj, "teardown_method"):
                    obj.teardown_method()
                print("  PASS")
                total_passed += 1
            except pytest.skip.Exception as exc:
                print(f"  SKIP: {exc}")
                total_skipped += 1
                try:
                    if hasattr(obj, "teardown_method"):
                        obj.teardown_method()
                except Exception:
                    pass
            except Exception as exc:
                import traceback
                print(f"  FAIL: {exc}")
                traceback.print_exc()
                total_failed += 1
                try:
                    if hasattr(obj, "teardown_method"):
                        obj.teardown_method()
                except Exception:
                    pass

    print(f"\n{'='*60}")
    print(f"  {total_passed} passed, {total_failed} failed, "
          f"{total_skipped} skipped")


# ---------------------------------------------------------------------------
# Multi-layer probe (added 2026-08-17)
# ---------------------------------------------------------------------------

class TestProbeMultiLayer:
    """Probe behaviour with more than one layer.

    ``TestProbe`` builds ``self.vs`` with ``_make_single_layer``, so the
    *entire* probe test class exercises one layer.  Defect (1) of the
    PB-series -- the probe consulting every layer rather than only the
    first -- was the bulk of the 47.7% false-empty rate, and it has had
    no coverage since it was fixed.  This class is that coverage.
    """

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        t0, t1 = meta["time_range"]
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.15),
            channel_range=(0, 48),
        )
        self.vs = _make_two_layer(self.backend, self.sel)

    def teardown_method(self):
        self.backend.close()

    def _finite_data_coords(self):
        """A coordinate where at least one layer has data.

        REBUILT 2026-09 (Chunk 2d): the original read
        ``self.vs._layer_aggs``, permanently ``None`` post-redesign --
        same vestigial-attribute trap as ``TestProbe``'s copy (see that
        class's docstring). Unlike ``TestProbe``'s fix, which reads back
        from the *composite* image (fine when there's only one layer to
        tell apart), this needs genuinely per-layer visibility, which a
        merged composite can't give. Uses ``self.vs._layer_id_grid``
        instead -- the coarse identity grid ``_handle_probe`` itself
        reads (see its docstring), still real and per-layer. Confirmed
        against real data that every layer's grid shares identical
        shape/x_range/y_range for a given render, so one (px, py) index
        is valid against any layer's grid.
        """
        for grid in self.vs._layer_id_grid:
            if grid is None:
                continue
            values = grid["value"]
            ys, xs = np.where(np.isfinite(values))
            if len(ys) == 0:
                continue
            py, px = int(ys[len(ys) // 2]), int(xs[len(xs) // 2])
            h, w = values.shape
            gx0, gx1 = grid["x_range"]
            gy0, gy1 = grid["y_range"]
            x = np.linspace(gx0, gx1, w)[px]
            y = np.linspace(gy0, gy1, h)[py]
            return float(x), float(y)
        pytest.skip("No populated bins in any layer")

    # -- the defect this class exists for -------------------------------

    def test_probe_reports_every_layer(self):
        """One entry per layer, not just the first.

        The single-layer test class cannot distinguish "consulted every
        layer" from "consulted layer 0 and stopped", because those are
        the same thing when there is only one.
        """
        x, y = self._finite_data_coords()
        probe = self.vs._handle_probe({"x": x, "y": y})["probe"]
        assert len(probe["layers"]) == len(self.vs.layers) == 2
        assert [e["index"] for e in probe["layers"]] == [0, 1]
        assert [e["label"] for e in probe["layers"]] == [
            lyr.label for lyr in self.vs.layers]

    def test_bin_populated_only_in_layer_1_is_found(self):
        """A bin empty in XX but populated in YY must still report a value.

        This is defect (1) exactly: the pre-fix probe consulted layer 0,
        found nothing, and returned "empty" while layer 1 had data at the
        same coordinate.  Skips when the two polarisations happen to be
        populated identically, which is the common case on well-behaved
        data -- a skip here means the dataset could not exercise the
        defect, not that the behaviour is unverified elsewhere.

        REBUILT 2026-09 (Chunk 2d): ``self.vs._layer_aggs`` is
        permanently ``None`` post-redesign (see
        ``TestProbeMultiLayer._finite_data_coords``'s docstring) --
        rebuilt against ``self.vs._layer_id_grid`` instead, the same
        structure ``_handle_probe`` itself now consults.
        """
        g0, g1 = self.vs._layer_id_grid[0], self.vs._layer_id_grid[1]
        if g0 is None or g1 is None:
            pytest.skip("Need both layers gridded")
        v0, v1 = g0["value"], g1["value"]
        only1 = ~np.isfinite(v0) & np.isfinite(v1)
        ys, xs = np.where(only1)
        if len(ys) == 0:
            pytest.skip("No bin populated in layer 1 but not layer 0")
        py, px = int(ys[0]), int(xs[0])
        h, w = v1.shape
        gx0, gx1 = g1["x_range"]
        gy0, gy1 = g1["y_range"]
        x = float(np.linspace(gx0, gx1, w)[px])
        y = float(np.linspace(gy0, gy1, h)[py])

        probe = self.vs._handle_probe({"x": x, "y": y})["probe"]
        assert probe["status"] == "ok"
        assert probe["layers"][1]["value"] is not None, (
            "layer 1 has data at this bin but the probe reported none — "
            "the probe is not consulting every layer"
        )

    def test_winner_is_the_nearest_layer(self):
        """``winner`` indexes the lowest-index layer with a hit.

        RENAMED IN SPIRIT, 2026-09 (Chunk 2d): the original asserted a
        ``distance_px``-based "nearest" tiebreak, inherited from the
        pre-redesign full-resolution neighbor search. The piece-2
        redesign does exact-cell lookup only (see ``_handle_probe``'s
        docstring) and its actual tiebreak is "lowest layer index among
        candidates" (see ``_handle_probe``: ``min(candidates, key=lambda
        c: c[0])``) -- there is no distance left to be nearest by. Left
        the test name alone (renaming it is a bigger diff than the fix
        needs) but rebuilt the assertion to match what winner selection
        actually does now.
        """
        x, y = self._finite_data_coords()
        probe = self.vs._handle_probe({"x": x, "y": y})["probe"]
        hits = [e["index"] for e in probe["layers"] if e["value"] is not None]
        if not hits:
            pytest.skip("No layer hit at this coordinate")
        assert probe["winner"] == min(hits)

    # -- hiding a layer --------------------------------------------------

    def test_hidden_layer_reports_no_value(self):
        """A hidden layer is never consulted, so it cannot carry a value.

        The single-layer class skipped its equivalent
        ("Need at least two layers"), so this path was untested.

        ``distance_px`` dropped 2026-09 -- see
        ``TestProbe.test_probe_hidden_layer_reports_no_value``'s
        docstring.
        """
        self.vs.set_alpha(1, 0.0)
        try:
            x, y = self._finite_data_coords()
            probe = self.vs._handle_probe({"x": x, "y": y})["probe"]
            hidden = probe["layers"][1]
            assert hidden["visible"] is False
            assert hidden["value"] is None
            # ...and the visible one still reports normally.
            assert probe["layers"][0]["visible"] is True
        finally:
            self.vs.set_alpha(1, 1.0)

    def test_label_shows_one_reading_per_visible_layer(self):
        """The status bar renders both layers, in index order."""
        x, y = self._finite_data_coords()
        label = self.vs._handle_probe({"x": x, "y": y})["label"]
        for lyr in self.vs.layers:
            assert lyr.label in label
        assert label.index(self.vs.layers[0].label) < \
               label.index(self.vs.layers[1].label)

    def test_probe_envelope_is_json_safe(self):
        """Two layers double the numeric fields that could carry a NaN."""
        import json
        x, y = self._finite_data_coords()
        text = json.dumps(self.vs._handle_probe({"x": x, "y": y}))
        assert "NaN" not in text and "Infinity" not in text
        json.loads(text)
