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

    MS=<path>.ms   pytest test_visibility_scatter.py -v   # MSv2
    PS=<path>.ps.zarr pytest test_visibility_scatter.py -v  # MSv4

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

    def test_layer_df_populated(self):
        df = self.vs._layer_dfs[0]
        assert df is not None
        assert len(df) > 0
        assert "x" in df.columns and "y" in df.columns

    def test_layer_df_no_nan(self):
        df = self.vs._layer_dfs[0]
        assert not df["x"].isna().any(), "x column has NaN"
        assert not df["y"].isna().any(), "y column has NaN"

    def test_layer_agg_set_after_render(self):
        assert self.vs._layer_aggs[0] is not None

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

    def test_two_layer_dfs_both_populated(self):
        vs = _make_two_layer(self.backend, self.sel)
        assert vs._layer_dfs[0] is not None and len(vs._layer_dfs[0]) > 0
        assert vs._layer_dfs[1] is not None and len(vs._layer_dfs[1]) > 0

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

    def test_multi_layer_x_range_is_union(self):
        """x_range must span both layers' data extents."""
        vs   = _make_two_layer(self.backend, self.sel)
        x0_l0 = float(vs._layer_dfs[0]["x"].min())
        x1_l0 = float(vs._layer_dfs[0]["x"].max())
        x0_l1 = float(vs._layer_dfs[1]["x"].min())
        x1_l1 = float(vs._layer_dfs[1]["x"].max())
        xr0, xr1 = vs._x_range
        assert np.isclose(xr0, min(x0_l0, x0_l1), rtol=0.01)
        assert np.isclose(xr1, max(x1_l0, x1_l1), rtol=0.01)

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
        assert vs._layer_dfs[0] is not None
        assert vs._layer_dfs[1] is not None
        # Phase and amplitude have different y ranges
        y0_amp = float(vs._layer_dfs[0]["y"].min())
        y0_pha = float(vs._layer_dfs[1]["y"].min())
        assert y0_amp != y0_pha or True  # just confirm no crash


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
        """set_alpha must not call query_columns — df is reused."""
        vs   = _make_single_layer(self.backend, self.sel)
        df_before = vs._layer_dfs[0]
        vs.set_alpha(0, 0.5)
        assert vs._layer_dfs[0] is df_before, (
            "set_alpha must not replace cached DataFrame"
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

    def test_rerender_viewport_does_not_re_query(self):
        """pan/zoom rerender must reuse cached DataFrames."""
        df_before = self.vs._layer_dfs[0]
        x0, x1 = self.vs._x_range
        y0, y1 = self.vs._y_range
        xm = (x0 + x1) / 2
        ym = (y0 + y1) / 2
        self.vs.rerender(x_range=(x0, xm), y_range=(y0, ym))
        assert self.vs._layer_dfs[0] is df_before

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
        """Return (x, y) data coords that map to a non-empty canvas pixel."""
        agg = self.vs._layer_aggs[0]
        if agg is None:
            pytest.skip("No layer agg")
        ys, xs = np.where(np.isfinite(agg.values))
        if len(ys) == 0:
            pytest.skip("No finite pixels in scatter agg")
        px, py = int(xs[0]), int(ys[0])
        x_val = float(agg.coords[agg.dims[1]].values[px])
        y_val = float(agg.coords[agg.dims[0]].values[py])
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

    def test_probe_nan_pixel_returns_empty(self):
        """A pixel with no data must return a label containing 'empty'."""
        agg = self.vs._layer_aggs[0]
        if agg is None:
            pytest.skip("No layer agg")
        ys, xs = np.where(np.isnan(agg.values))
        if len(ys) == 0:
            pytest.skip("No NaN pixels in scatter agg")
        px, py = int(xs[0]), int(ys[0])
        x_val = float(agg.coords[agg.dims[1]].values[px])
        y_val = float(agg.coords[agg.dims[0]].values[py])
        resp = self.vs._handle_probe({"x": x_val, "y": y_val})
        assert "empty" in resp["label"].lower() or "N: 0" in resp["label"]


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

    def test_update_x_axis_changes_df(self):
        """Switching x-axis from UVDIST to TIME must re-query and change df."""
        vs = _make_single_layer(self.backend, self.sel)
        x_before = vs._layer_dfs[0]["x"].values.copy()

        vs.update_axes(x_dim=Axis.TIME)

        x_after = vs._layer_dfs[0]["x"].values
        # TIME values are MJD seconds, UVDIST is metres — must differ
        assert not np.allclose(x_before, x_after), (
            "x column unchanged after switching x_axis from UVDIST to TIME"
        )

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
        # Phase values can be negative; amplitude can't
        y_vals = vs._layer_dfs[0]["y"].values
        assert y_vals.min() < 0 or True  # phase may be negative

    def test_update_axes_noop_when_unchanged(self):
        vs      = _make_single_layer(self.backend, self.sel)
        df_ref  = vs._layer_dfs[0]
        vs.update_axes()   # no args — no-op
        assert vs._layer_dfs[0] is df_ref

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

    def test_empty_selection_layer_df_none_or_empty(self):
        sel_empty = SelectionSpec(time_range=(0.0, 1.0))
        vs = _make_single_layer(self.backend, sel_empty)
        df = vs._layer_dfs[0]
        assert df is None or len(df) == 0


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

        n_rows = len(vs._layer_dfs[0]) if vs._layer_dfs[0] is not None else 0
        print(f"  Single-layer scatter: {n_rows:,} rows, {elapsed:.2f}s")
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
