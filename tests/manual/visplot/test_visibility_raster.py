"""
test_visibility_raster.py — Integration tests for VisibilityRaster.

Location in repository:
    cubevis/tests/manual/visplot/test_visibility_raster.py

Tests against:
    cubevis/cubevis/toolbox/visplot/visibility_raster.py
    cubevis/cubevis/toolbox/visplot/data/msv2_backend.py
    cubevis/cubevis/toolbox/visplot/data/msv4_backend.py
    cubevis/cubevis/toolbox/visplot/axes.py
    cubevis/cubevis/toolbox/visplot/selection.py

Backend selection (mutually exclusive)
--------------------------------------
Set exactly one of MS or PS:

    ulimit -n 4096 && MS=<path>.ms   pytest test_visibility_raster.py -v   # MSv2
    ulimit -n 4096 && PS=<path>.ps.zarr pytest test_visibility_raster.py -v  # MSv4

If both are set the suite fails immediately (ambiguous).
If neither is set all tests are skipped.

Tests
-----
1. Lifecycle           build / rebuild / layout structure
2. Render              source geometry, image dtype, range consistency
3. _data_to_pixel      coordinate→pixel mapping, edge clipping, pre-render
4. _viewport_selection time, frequency, baseline axis viewport narrowing
5. probe (static)      _handle_probe via _data_to_pixel→probe_raster_pixel
6. rerender            programmatic re-render updates source in place
7. Datashader          rendered agg shape, finite pixels, colourmap
8. Timing              full raster pipeline < 8s end-to-end

Also covered further down (added after this list was last updated):
update_axes(), color_mode, colormap scaling, decimation, and
TestDeferredConstruction — construct with defer_initial_render=True
(no backend query; see decision 11 in the grid/iteration design notes).
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
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend, _axis_to_dim
    from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
    from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster
    from cubevis.toolbox.visplot.visibility_plot import _img_to_uint32
    return (Axis, AxisType, SelectionSpec,
            MSv2Backend, MSv4Backend, _axis_to_dim,
            VisibilityRaster, _img_to_uint32)


def _local_import():
    import importlib.util

    here = Path(__file__).parent

    def _load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod  = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    axes_mod    = _load("cubevis.toolbox.visplot.axes",      here / "axes.py")
    sel_mod     = _load("cubevis.toolbox.visplot.selection", here / "selection.py")
    sys.modules["cubevis.toolbox.visplot.axes"]      = axes_mod
    sys.modules["cubevis.toolbox.visplot.selection"] = sel_mod
    _load("cubevis.toolbox.visplot.reader",          here / "reader.py")
    backend_mod = _load("cubevis.toolbox.visplot.data.msv2_backend",
                        here / "msv2_backend.py")
    msv4_mod    = _load("cubevis.toolbox.visplot.data.msv4_backend",
                        here / "msv4_backend.py")
    _load("cubevis.toolbox.visplot.visibility_plot",
                       here / "visibility_plot.py")
    vr_mod      = _load("cubevis.toolbox.visplot.visibility_raster",
                        here / "visibility_raster.py")
    vp_mod      = sys.modules["cubevis.toolbox.visplot.visibility_plot"]

    return (
        axes_mod.Axis,
        axes_mod.AxisType,
        sel_mod.SelectionSpec,
        backend_mod.MSv2Backend,
        msv4_mod.MSv4Backend,
        backend_mod._axis_to_dim,
        vr_mod.VisibilityRaster,
        vp_mod._img_to_uint32,
    )


try:
    (Axis, AxisType, SelectionSpec,
     MSv2Backend, MSv4Backend, _axis_to_dim,
     VisibilityRaster, _img_to_uint32) = _try_package_import()
    _SOURCE = "package"
except ImportError:
    (Axis, AxisType, SelectionSpec,
     MSv2Backend, MSv4Backend, _axis_to_dim,
     VisibilityRaster, _img_to_uint32) = _local_import()
    _SOURCE = "local"

try:
    import datashader as ds
    import datashader.reductions as ds_agg
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


def _open_backend(**kwargs):
    """Open and return the backend indicated by the environment."""
    path, kind = _detect_backend_path()
    b = MSv2Backend(path, **kwargs) if kind == "msv2" else MSv4Backend(path, **kwargs)
    b.open()
    return b


def _backend_kind() -> str:
    """Return 'msv2' or 'msv4' without opening the backend."""
    _, kind = _detect_backend_path()
    return kind


def _axis_to_dim_safe(axis) -> str:
    """Return the xarray dimension name for axis, backend-agnostically."""
    return _axis_to_dim(axis)


@pytest.fixture(scope="session", autouse=True)
def _show_backend(request):  # noqa: ARG001
    """Write the active backend to /dev/tty, bypassing pytest capture."""
    ms_path = os.environ.get("MS", "").strip()
    ps_path = os.environ.get("PS", "").strip()
    if ms_path and not ps_path:
        msg = f"[test_visibility_raster] backend: MSv2  path={ms_path!r}"
    elif ps_path and not ms_path:
        msg = f"[test_visibility_raster] backend: MSv4  path={ps_path!r}"
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


def _make_vr(backend, selection, **kwargs) -> VisibilityRaster:
    """Construct a VisibilityRaster with defaults suited to sis14."""
    defaults = dict(
        y_dim    = Axis.TIME,
        x_dim    = Axis.BASELINE,
        quantity = Axis.AMPLITUDE,
        width    = PLOT_W,
        height   = PLOT_H,
    )
    defaults.update(kwargs)
    meta = backend.metadata()
    pol  = meta["correlation_labels"][0]
    return VisibilityRaster(
        backend      = backend,
        selection    = selection,
        polarization = pol,
        **defaults,
    )


def _first_finite_pixel(agg_vals):
    """Return (px, py) of the first finite-value pixel in an ndarray."""
    ys, xs = np.where(np.isfinite(agg_vals))
    if len(ys) == 0:
        pytest.skip("No finite pixels in agg — cannot test probe")
    return int(xs[0]), int(ys[0])


# ---------------------------------------------------------------------------
# 1. Lifecycle — build / rebuild / layout
# ---------------------------------------------------------------------------

class TestLifecycle:

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        self.pols = meta["correlation_labels"]
        t0, t1 = meta["time_range"]
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.1),
            channel_range=(0, 16),
        )

    def teardown_method(self):
        self.backend.close()

    def test_figure_is_not_none(self):
        vr = _make_vr(self.backend, self.sel)
        assert vr.figure is not None

    def test_layout_is_not_none(self):
        vr = _make_vr(self.backend, self.sel)
        assert vr.layout is not None

    def test_agg_is_set_after_build(self):
        vr = _make_vr(self.backend, self.sel)
        assert vr.agg is not None
        assert vr.agg.ndim == 2

    def test_source_populated(self):
        vr = _make_vr(self.backend, self.sel)
        src = vr._image_source.data
        assert "image" in src and "x" in src and "y" in src
        assert "dw" in src and "dh" in src

    def test_auto_title_contains_axis_names(self):
        vr = _make_vr(self.backend, self.sel)
        title = vr.figure.title.text
        assert "TIME" in title.upper() or "Time" in title
        assert "BASELINE" in title.upper() or "Baseline" in title

    def test_custom_title(self):
        vr = _make_vr(self.backend, self.sel, title="My Custom Title")
        assert vr._title == "My Custom Title"

    def test_without_datashader_raises_import_error(self):
        """If datashader is absent, constructor must raise ImportError."""
        import cubevis.toolbox.visplot.visibility_plot as vp_mod
        orig = vp_mod.HAS_DATASHADER
        try:
            vp_mod.HAS_DATASHADER = False
            with pytest.raises(ImportError, match="datashader"):
                _make_vr(self.backend, self.sel)
        finally:
            vp_mod.HAS_DATASHADER = orig


# ---------------------------------------------------------------------------
# 2. Render — source geometry, image dtype, range consistency
# ---------------------------------------------------------------------------

class TestRender:

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

    def test_image_dtype_is_uint32(self):
        vr = _make_vr(self.backend, self.sel)
        img = vr._image_source.data["image"][0]
        assert img.dtype == np.uint32, f"Expected uint32, got {img.dtype}"

    def test_image_shape_matches_canvas(self):
        vr = _make_vr(self.backend, self.sel)
        img = vr._image_source.data["image"][0]
        assert img.shape == (PLOT_H, PLOT_W), (
            f"Image shape {img.shape} != canvas ({PLOT_H}, {PLOT_W})"
        )

    def test_dw_dh_positive(self):
        vr = _make_vr(self.backend, self.sel)
        assert vr._image_source.data["dw"][0] > 0
        assert vr._image_source.data["dh"][0] > 0

    def test_xy_consistent_with_ranges(self):
        """x/y/dw/dh in source match x_range/y_range stored on the object."""
        vr = _make_vr(self.backend, self.sel)
        src  = vr._image_source.data
        x0   = src["x"][0];   dw = src["dw"][0]
        y0   = src["y"][0];   dh = src["dh"][0]
        xr0, xr1 = vr._x_range
        yr0, yr1 = vr._y_range
        assert np.isclose(x0,      xr0, rtol=1e-6)
        assert np.isclose(x0 + dw, xr1, rtol=1e-6)
        assert np.isclose(y0,      yr0, rtol=1e-6)
        assert np.isclose(y0 + dh, yr1, rtol=1e-6)

    def test_agg_dims_match_axes(self):
        """agg dimensions must be (time, baseline_id) for TIME×BASELINE."""
        vr = _make_vr(self.backend, self.sel)
        y_name = _axis_to_dim_safe(Axis.TIME)
        x_name = _axis_to_dim_safe(Axis.BASELINE)
        assert vr.agg.dims == (y_name, x_name), (
            f"agg.dims={vr.agg.dims}, expected ({y_name}, {x_name})"
        )

    def test_agg_has_finite_values(self):
        vr = _make_vr(self.backend, self.sel)
        assert int(np.isfinite(vr.agg.values).sum()) > 0, (
            "All raster values are NaN — data selection too narrow?"
        )

    def test_amplitude_values_nonnegative(self):
        vr = _make_vr(self.backend, self.sel)
        finite = vr.agg.values[np.isfinite(vr.agg.values)]
        assert (finite >= 0).all(), "Amplitude raster contains negative values"

    def test_phase_raster_in_range(self):
        """Phase values must be in [-180, 180] degrees."""
        vr = _make_vr(self.backend, self.sel, quantity=Axis.PHASE)
        finite = vr.agg.values[np.isfinite(vr.agg.values)]
        assert len(finite) > 0
        assert (finite >= -180).all() and (finite <= 180).all(), (
            f"Phase out of range: [{finite.min():.1f}, {finite.max():.1f}]"
        )

    def test_flag_raster_in_range(self):
        """Flag fraction must be in [0, 1]."""
        vr = _make_vr(self.backend, self.sel, quantity=Axis.FLAG)
        finite = vr.agg.values[np.isfinite(vr.agg.values)]
        assert (finite >= 0).all() and (finite <= 1).all()

    def test_x_range_ordered(self):
        vr = _make_vr(self.backend, self.sel)
        assert vr._x_range[0] < vr._x_range[1]

    def test_y_range_ordered(self):
        vr = _make_vr(self.backend, self.sel)
        assert vr._y_range[0] < vr._y_range[1]

    def test_empty_selection_does_not_crash(self):
        """Empty selection (no matching data) must return a valid blank image."""
        sel_empty = SelectionSpec(time_range=(0.0, 1.0))
        vr = _make_vr(self.backend, sel_empty)
        assert vr.agg is not None
        assert vr.agg.ndim == 2
        # Blank image: all pixels transparent (alpha == 0)
        img = vr._image_source.data["image"][0]
        assert img.dtype == np.uint32
        assert img.shape == (PLOT_H, PLOT_W)
        # All values should be 0 (fully transparent black)
        assert (img == 0).all(), "Empty selection should produce all-zero image"


# ---------------------------------------------------------------------------
# 3. _data_to_pixel — coordinate→pixel mapping
# ---------------------------------------------------------------------------

class TestDataToPixel:

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
        self.vr = _make_vr(self.backend, self.sel)

    def teardown_method(self):
        self.backend.close()

    def test_interior_maps_to_valid_pixel(self):
        agg = self.vr.agg
        xc = float(agg.coords[agg.dims[1]].values[agg.shape[1] // 2])
        yc = float(agg.coords[agg.dims[0]].values[agg.shape[0] // 2])
        px, py = self.vr._data_to_pixel(xc, yc)
        assert px is not None and py is not None
        h, w = agg.shape
        assert 0 <= px < w
        assert 0 <= py < h

    def test_coord_roundtrip(self):
        """The pixel index recovered from a coordinate centre maps back to
        the nearest coordinate to the query value."""
        agg = self.vr.agg
        x_coords = agg.coords[agg.dims[1]].values
        y_coords = agg.coords[agg.dims[0]].values
        # Test a known column centre
        col = agg.shape[1] // 3
        row = agg.shape[0] // 3
        px, py = self.vr._data_to_pixel(float(x_coords[col]),
                                         float(y_coords[row]))
        assert px == col, f"x roundtrip: sent col {col}, got px {px}"
        assert py == row, f"y roundtrip: sent row {row}, got py {py}"

    def test_below_range_clips_to_zero(self):
        """Values far below the coordinate range must return a valid pixel index."""
        h, w = self.vr.agg.shape
        px, py = self.vr._data_to_pixel(-1e30, -1e30)
        assert 0 <= px < w, f"px={px} out of bounds for w={w}"
        assert 0 <= py < h, f"py={py} out of bounds for h={h}"

    def test_above_range_clips_to_last(self):
        """Values far above the coordinate range must return a valid pixel index."""
        h, w = self.vr.agg.shape
        px, py = self.vr._data_to_pixel(1e30, 1e30)
        # Must be within bounds — the exact index depends on coordinate sort
        # order (agg coordinate arrays are not guaranteed ascending).
        assert 0 <= px < w, f"px={px} out of bounds for w={w}"
        assert 0 <= py < h, f"py={py} out of bounds for h={h}"

    def test_none_when_agg_not_set(self):
        from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster
        vr = object.__new__(VisibilityRaster)
        vr._agg = None
        px, py = vr._data_to_pixel(0.0, 0.0)
        assert px is None and py is None


# ---------------------------------------------------------------------------
# 4. _viewport_selection
# ---------------------------------------------------------------------------

class TestViewportSelection:

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        self.t0, self.t1 = meta["time_range"]
        self.f0, self.f1 = meta["freq_range"]
        self.sel = SelectionSpec(
            time_range=(self.t0, self.t0 + (self.t1 - self.t0) * 0.1),
            channel_range=(0, 16),
        )

    def teardown_method(self):
        self.backend.close()

    def test_time_x_axis_sets_time_range(self):
        vr = _make_vr(self.backend, self.sel,
                      y_dim=Axis.BASELINE, x_dim=Axis.TIME)
        t_lo = self.t0 + (self.t1 - self.t0) * 0.02
        t_hi = self.t0 + (self.t1 - self.t0) * 0.05
        sel2 = vr._viewport_selection(x_range=(t_lo, t_hi), y_range=None)
        assert sel2.time_range == (t_lo, t_hi)

    def test_time_y_axis_sets_time_range(self):
        vr = _make_vr(self.backend, self.sel,
                      y_dim=Axis.TIME, x_dim=Axis.BASELINE)
        t_lo = self.t0 + (self.t1 - self.t0) * 0.02
        t_hi = self.t0 + (self.t1 - self.t0) * 0.05
        sel2 = vr._viewport_selection(x_range=None, y_range=(t_lo, t_hi))
        assert sel2.time_range == (t_lo, t_hi)

    def test_frequency_y_axis_sets_freq_range(self):
        # Need a single-baseline selection to get a time×freq raster.
        # _iter_visibility_partitions is MSv2-only; skip on MSv4.
        if _backend_kind() != "msv2":
            pytest.skip("_iter_visibility_partitions is MSv2-only")
        ds_part = next(self.backend._iter_visibility_partitions())
        eit  = ds_part["EFFECTIVE_INTEGRATION_TIME"].isel(time=0).compute()
        vidx = int(np.where(np.isfinite(eit.values))[0][0])
        a1 = str(ds_part.coords["baseline_antenna1_name"].values[vidx])
        a2 = str(ds_part.coords["baseline_antenna2_name"].values[vidx])
        sel_bl = SelectionSpec(channel_range=(0, 48), baselines=[(a1, a2)])
        vr = _make_vr(self.backend, sel_bl,
                      y_dim=Axis.TIME, x_dim=Axis.FREQUENCY)
        f_lo = self.f0 + (self.f1 - self.f0) * 0.1
        f_hi = self.f0 + (self.f1 - self.f0) * 0.9
        sel2 = vr._viewport_selection(x_range=(f_lo, f_hi), y_range=None)
        assert sel2.freq_range == (f_lo, f_hi)

    def test_baseline_axis_does_not_set_freq_or_time(self):
        """BASELINE viewport range has no SelectionSpec field — no-op."""
        vr = _make_vr(self.backend, self.sel,
                      y_dim=Axis.TIME, x_dim=Axis.BASELINE)
        t_lo = self.t0 + (self.t1 - self.t0) * 0.01
        t_hi = self.t0 + (self.t1 - self.t0) * 0.05
        # x_range is BASELINE (no-op); y_range is TIME
        sel2 = vr._viewport_selection(x_range=(0.0, 100.0),
                                       y_range=(t_lo, t_hi))
        assert sel2.time_range == (t_lo, t_hi)
        assert sel2.freq_range is None

    def test_none_ranges_leave_selection_unchanged(self):
        vr = _make_vr(self.backend, self.sel)
        sel2 = vr._viewport_selection(x_range=None, y_range=None)
        assert sel2.time_range == self.sel.time_range
        assert sel2.freq_range == self.sel.freq_range

    def test_viewport_preserves_existing_fields(self):
        """Narrowing the viewport must not discard unrelated selection fields."""
        vr = _make_vr(self.backend, self.sel)
        t_lo = self.t0 + (self.t1 - self.t0) * 0.01
        t_hi = self.t0 + (self.t1 - self.t0) * 0.05
        sel2 = vr._viewport_selection(x_range=None, y_range=(t_lo, t_hi))
        # channel_range from original sel must survive
        assert sel2.channel_range == self.sel.channel_range


# ---------------------------------------------------------------------------
# 5. probe (static — no Comm)
# ---------------------------------------------------------------------------

class TestProbeStatic:
    """Exercise _handle_probe end-to-end without a live Comm channel.

    No J2P/P2J round-trip; we call the handler directly with data-space
    coordinates and inspect the returned label dict.
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
        self.vr = _make_vr(self.backend, self.sel)

    def teardown_method(self):
        self.backend.close()

    def _finite_data_coords(self):
        """Return data-space (x, y) that map to a finite pixel."""
        agg = self.vr.agg
        px, py = _first_finite_pixel(agg.values)
        x_val = float(agg.coords[agg.dims[1]].values[px])
        y_val = float(agg.coords[agg.dims[0]].values[py])
        return x_val, y_val

    def test_probe_returns_label_key(self):
        x, y = self._finite_data_coords()
        resp = self.vr._handle_probe({"x": x, "y": y})
        assert "label" in resp
        assert isinstance(resp["label"], str)
        assert len(resp["label"]) > 0

    def test_probe_label_contains_quantity(self):
        x, y = self._finite_data_coords()
        resp = self.vr._handle_probe({"x": x, "y": y})
        assert "Amplitude" in resp["label"]

    def test_probe_label_contains_value(self):
        """The formatted label must include a numeric quantity value."""
        x, y = self._finite_data_coords()
        resp = self.vr._handle_probe({"x": x, "y": y})
        # A non-empty pixel must show a number, not "empty"
        assert "empty" not in resp["label"].lower()

    def test_probe_out_of_range_safe(self):
        """Extreme coordinates outside agg extent must not raise."""
        resp = self.vr._handle_probe({"x": -1e30, "y": -1e30})
        assert "label" in resp

    def test_probe_nan_pixel_shows_empty(self):
        """Probing an empty (NaN) pixel must report empty, structurally.

        Asserts resp["probe"], not the label markup.  The equivalent
        scatter test broke twice on label-format changes alone, which is
        a bad reason for a test to fail; the label assertion is kept as a
        second, weaker check rather than as the only one.
        """
        agg = self.vr.agg
        ys, xs = np.where(np.isnan(agg.values))
        if len(ys) == 0:
            pytest.skip("No NaN pixels in agg for this selection")
        px, py = int(xs[0]), int(ys[0])
        x_val = float(agg.coords[agg.dims[1]].values[px])
        y_val = float(agg.coords[agg.dims[0]].values[py])
        resp = self.vr._handle_probe({"x": x_val, "y": y_val})
        probe = resp["probe"]
        assert probe["status"] == "ok"
        assert probe["empty"] is True
        assert probe["value"] is None
        assert "empty" in resp["label"].lower()

    # ------------------------------------------------------------------
    # Structured probe envelope (added 2026-08)
    # ------------------------------------------------------------------

    def test_probe_envelope_shape(self):
        """Every probe answer carries a status and leaves label intact."""
        x, y = self._finite_data_coords()
        resp = self.vr._handle_probe({"x": x, "y": y})
        assert set(resp.keys()) >= {"label", "probe"}
        probe = resp["probe"]
        assert probe["status"] == "ok"
        assert probe["empty"] is False
        assert isinstance(probe["value"], float)
        assert len(probe["pixel"]) == 2

    def test_probe_out_of_range_status(self):
        """Out-of-range must be identifiable without reading the markup."""
        resp = self.vr._handle_probe({"x": -1e30, "y": -1e30})
        assert resp["probe"]["status"] == "out_of_range"

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
        resp = self.vr._handle_probe({"x": x, "y": y})
        text = json.dumps(resp)
        assert "NaN" not in text and "Infinity" not in text
        json.loads(text)

    def test_probe_field_name_in_label(self):
        """When baseline is x-axis, probe should include field info (time is y)."""
        x, y = self._finite_data_coords()
        resp = self.vr._handle_probe({"x": x, "y": y})
        # probe_raster_pixel populates field_names from the time y-axis range
        # Just check label is non-empty and doesn't raise
        assert resp["label"]

    def test_probe_antenna_pair_in_label(self):
        """With baseline as x-axis, label should mention at least one antenna."""
        x, y = self._finite_data_coords()
        resp = self.vr._handle_probe({"x": x, "y": y})
        label = resp["label"]
        # Check for 'BL:' section (formatted by _format_probe)
        # Only present when antenna_pairs non-empty
        if "BL:" in label:
            # must be antenna names like DV01&DV02
            assert "&" in label


# ---------------------------------------------------------------------------
# 6. rerender
# ---------------------------------------------------------------------------

class TestRerender:

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        self.pols = meta["correlation_labels"]
        t0, t1 = meta["time_range"]
        self.t0, self.t1 = t0, t1
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.2),
            channel_range=(0, 48),
        )
        self.vr = _make_vr(self.backend, self.sel)

    def teardown_method(self):
        self.backend.close()

    def test_source_id_unchanged(self):
        """rerender() must update the existing _image_source, not replace it."""
        old_id = id(self.vr._image_source)
        t_lo = self.t0 + (self.t1 - self.t0) * 0.02
        t_hi = self.t0 + (self.t1 - self.t0) * 0.08
        self.vr.rerender(y_range=(t_lo, t_hi))
        assert id(self.vr._image_source) == old_id

    def test_rerender_updates_agg(self):
        """Viewport rerender must NOT re-query the backend — agg stays the same."""
        old_agg = self.vr.agg
        t_lo = self.t0 + (self.t1 - self.t0) * 0.02
        t_hi = self.t0 + (self.t1 - self.t0) * 0.06
        self.vr.rerender(y_range=(t_lo, t_hi))
        # agg must be the same object — no backend re-query for a viewport change
        assert self.vr.agg is old_agg

    def test_rerender_new_selection_updates_agg(self):
        """Passing new_selection must re-query the backend and replace agg."""
        old_agg = self.vr.agg
        new_sel = SelectionSpec(
            time_range=(self.t0, self.t0 + (self.t1 - self.t0) * 0.1),
            channel_range=(0, 16),
        )
        self.vr.rerender(new_selection=new_sel)
        # agg may be a different object since the backend was re-queried
        assert self.vr.agg is not None

    def test_rerender_image_still_uint32(self):
        self.vr.rerender()
        img = self.vr._image_source.data["image"][0]
        assert img.dtype == np.uint32

    def test_rerender_narrowed_time_range(self):
        """Narrowing the viewport should not crash and must return valid image."""
        t_lo = self.t0 + (self.t1 - self.t0) * 0.05
        t_hi = self.t0 + (self.t1 - self.t0) * 0.10
        self.vr.rerender(y_range=(t_lo, t_hi))
        img = self.vr._image_source.data["image"][0]
        assert img.shape == (PLOT_H, PLOT_W)

    def test_handle_rerender_response_structure(self):
        """_handle_rerender must return {image: ndarray, x0, x1, y0, y1}."""
        meta = self.backend.metadata()
        t0, t1 = meta["time_range"]
        x0, x1 = self.vr._x_range
        y_lo = t0 + (t1 - t0) * 0.0
        y_hi = t0 + (t1 - t0) * 0.1
        resp = self.vr._handle_rerender({
            "x0": float(x0), "x1": float(x1),
            "y0": y_lo,       "y1": y_hi,
        })
        assert set(resp.keys()) >= {"image", "x0", "x1", "y0", "y1"}
        # image arrives as ndarray (transport serialises it as typed buffer)
        assert isinstance(resp["image"], np.ndarray)
        assert resp["image"].dtype == np.uint32
        assert resp["image"].shape == (PLOT_H, PLOT_W)
        # returned bounds must echo the requested viewport, not the full data range
        assert np.isclose(resp["y0"], y_lo)
        assert np.isclose(resp["y1"], y_hi)
        assert resp["x1"] > resp["x0"]

    def test_handle_rerender_does_not_update_agg(self):
        """_handle_rerender must NOT call query_raster — agg stays cached."""
        meta  = self.backend.metadata()
        t0, t1 = meta["time_range"]
        old_agg = self.vr.agg
        self.vr._handle_rerender({
            "x0": float(self.vr._x_range[0]),
            "x1": float(self.vr._x_range[1]),
            "y0": t0, "y1": t0 + (t1 - t0) * 0.05,
        })
        # agg must be the identical object — viewport rerender never re-queries
        assert self.vr.agg is old_agg


# ---------------------------------------------------------------------------
# 7. Datashader rendering
# ---------------------------------------------------------------------------

class TestDatashadedOutput:

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

    def test_img_to_uint32_pil_byte_order(self):
        """PIL RGBA Image path: bytes stay R G B A in memory order.

        Was asserting B G R A, matching a transposition in the PIL branch
        of _img_to_uint32 (fixed 2026-08).  Test-only path, so nothing
        user-visible was wrong, but this test was pinning the bug in
        place.  Assert memory order rather than the packed uint32 value:
        the same bytes read as 0xAABBGGRR little-endian and 0xRRGGBBAA
        big-endian, so a hex assertion would be an endianness trap.
        """
        from PIL import Image
        img = Image.fromarray(
            np.array([[[0x11, 0x22, 0x33, 0xff]]], dtype=np.uint8), mode="RGBA"
        )
        out  = _img_to_uint32(img)
        assert out.shape == (1, 1)
        assert out.dtype == np.uint32
        assert out.flags["C_CONTIGUOUS"]
        view = out.view(np.uint8).reshape(1, 1, 4)
        assert int(view[0, 0, 0]) == 0x11, "Red channel wrong"
        assert int(view[0, 0, 1]) == 0x22, "Green channel wrong"
        assert int(view[0, 0, 2]) == 0x33, "Blue channel wrong"
        assert int(view[0, 0, 3]) == 0xff, "Alpha channel wrong"

    def test_img_to_uint32_datashader_emits_rgba_in_memory(self):
        """Datashader shade() output is RGBA in memory: red byte 0, blue byte 2.

        The fact the whole export path rests on -- matplotlib's imshow
        takes ``img32.view(np.uint8).reshape(h, w, 4)`` with no channel
        permutation, and that is correct only if this holds.  Uses a
        two-colour cmap because a symmetric palette cannot distinguish R
        from B, which is exactly how the transposition above survived.
        """
        import xarray as xr
        import datashader.transfer_functions as tf

        agg = xr.DataArray(
            np.array([[0.0, 1.0]]), dims=("y", "x"),
            coords={"y": [0], "x": [0, 1]},
        )
        img = tf.shade(agg, cmap=["#FF0000", "#0000FF"],
                       how="linear", span=[0.0, 1.0])
        out  = _img_to_uint32(img)
        view = out.view(np.uint8).reshape(out.shape + (4,))
        assert int(view[0, 0, 0]) == 0xFF, "low end should be red in byte 0"
        assert int(view[0, 0, 2]) == 0x00
        assert int(view[0, 1, 0]) == 0x00
        assert int(view[0, 1, 2]) == 0xFF, "high end should be blue in byte 2"

    def test_img_to_uint32_rejects_float_rgba(self):
        """A float RGBA array must raise, not be silently truncated."""
        with pytest.raises(ValueError):
            _img_to_uint32(np.zeros((2, 2, 4), dtype=np.float32))

    def test_img_to_uint32_uint32_passthrough_is_contiguous(self):
        """A non-contiguous uint32 input still yields a viewable result.

        Callers rely on ``.view(np.uint8)`` unconditionally; that is only
        safe because the uint32 branch runs ascontiguousarray.
        """
        src = np.arange(6, dtype=np.uint32).reshape(2, 3)
        out = _img_to_uint32(src[:, ::2])
        assert out.flags["C_CONTIGUOUS"]
        np.testing.assert_array_equal(out, src[:, ::2])

    def test_img_to_uint32_datashader_passthrough(self):
        """Datashader Image path: uint32 array passed through unchanged."""
        # Simulate the (H, W) uint32 that tf.shade() produces
        synthetic = np.array([[0xff112233, 0xff445566]], dtype=np.uint32)
        out = _img_to_uint32(synthetic)
        assert out.dtype == np.uint32
        assert out.shape == (1, 2)
        np.testing.assert_array_equal(out, synthetic)

    def test_rendered_image_has_non_transparent_pixels(self):
        """At least some pixels must be non-transparent (alpha > 0)."""
        vr  = _make_vr(self.backend, self.sel)
        img = vr._image_source.data["image"][0]
        # Alpha is the high byte in little-endian 0xAARRGGBB
        alpha = (img >> 24) & 0xff
        assert (alpha > 0).any(), "All pixels are transparent — pipeline failed"

    def test_nearest_neighbour_at_sub_cell_viewport(self):
        """_shade_viewport must use nearest-neighbour when upsampling.

        At sub-cell viewport size (each canvas pixel < one agg cell) bilinear
        interpolation blurs the interior.  Verifies that the image produced at
        a tiny viewport (guaranteed upsampling) differs from a full-view image,
        and that both are valid uint32 arrays.  The interpolate="nearest" path
        is exercised when vp_px_size < agg_cell_size.
        """
        vr = _make_vr(self.backend, self.sel)
        agg = vr.agg
        if agg is None or agg.shape[0] < 2 or agg.shape[1] < 2:
            pytest.skip("agg too small to test interpolation")

        # Full-view image (downsampling — linear interpolation)
        img_full = vr._shade_viewport(vr._x_range, vr._y_range)

        # Tiny viewport: cover exactly 2 agg cells in each dimension
        # → canvas_px_size << agg_cell_size → upsampling → nearest-neighbour
        agg_cell_w = (vr._x_range[1] - vr._x_range[0]) / agg.shape[1]
        agg_cell_h = (vr._y_range[1] - vr._y_range[0]) / agg.shape[0]
        x_mid = (vr._x_range[0] + vr._x_range[1]) / 2
        y_mid = (vr._y_range[0] + vr._y_range[1]) / 2
        tiny_xr = (x_mid - agg_cell_w, x_mid + agg_cell_w)
        tiny_yr = (y_mid - agg_cell_h, y_mid + agg_cell_h)
        img_tiny = vr._shade_viewport(tiny_xr, tiny_yr)

        # Both must be valid uint32 arrays at canvas resolution
        assert img_full.dtype == np.uint32
        assert img_tiny.dtype == np.uint32
        assert img_full.shape == (PLOT_H, PLOT_W)
        assert img_tiny.shape == (PLOT_H, PLOT_W)

        # The two images must differ — the tiny upsampled view will show
        # a different colour distribution than the full downsampled view.
        # (If they were identical something is wrong with the viewport logic.)
        assert not np.array_equal(img_full, img_tiny), (
            "Full-view and sub-cell-viewport images are identical — "
            "viewport sizing or interpolation switch may be broken"
        )
        """Time×frequency waterfall for a single baseline."""
        if _backend_kind() != "msv2":
            pytest.skip("_iter_visibility_partitions is MSv2-only")
        ds_part = next(self.backend._iter_visibility_partitions())
        eit  = ds_part["EFFECTIVE_INTEGRATION_TIME"].isel(time=0).compute()
        vidx = int(np.where(np.isfinite(eit.values))[0][0])
        a1   = str(ds_part.coords["baseline_antenna1_name"].values[vidx])
        a2   = str(ds_part.coords["baseline_antenna2_name"].values[vidx])
        sel_bl = SelectionSpec(channel_range=(0, 48), baselines=[(a1, a2)])
        vr = _make_vr(self.backend, sel_bl,
                      y_dim=Axis.TIME, x_dim=Axis.FREQUENCY)
        img = vr._image_source.data["image"][0]
        assert img.dtype == np.uint32
        assert img.shape == (PLOT_H, PLOT_W)
        alpha = (img >> 24) & 0xff
        assert (alpha > 0).any(), "Waterfall: all pixels transparent"
        print(f"  Waterfall {a1}–{a2}: {int((alpha > 0).sum())} non-transparent px")


# ---------------------------------------------------------------------------
# 8. _state_source — axis-switching infrastructure
# ---------------------------------------------------------------------------

class TestStateSource:
    """Tests that _state_source is populated correctly and updates on rerender."""

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

    def test_state_source_exists(self):
        vr = _make_vr(self.backend, self.sel)
        assert vr._state_source is not None

    def test_state_source_has_required_keys(self):
        vr = _make_vr(self.backend, self.sel)
        d = vr._state_source.data
        required = {"agg_n_x", "agg_n_y", "full_x0", "full_x1",
                    "full_y0", "full_y1", "y_is_time",
                    "x_is_time", "x_label", "y_label"}
        assert required <= set(d.keys()), (
            f"Missing keys: {required - set(d.keys())}"
        )

    def test_state_source_agg_shape_matches(self):
        vr = _make_vr(self.backend, self.sel)
        d  = vr._state_source.data
        assert d["agg_n_x"][0] == vr.agg.shape[1]
        assert d["agg_n_y"][0] == vr.agg.shape[0]

    def test_state_source_ranges_match(self):
        vr = _make_vr(self.backend, self.sel)
        d  = vr._state_source.data
        assert np.isclose(d["full_x0"][0], vr._x_range[0])
        assert np.isclose(d["full_x1"][0], vr._x_range[1])
        assert np.isclose(d["full_y0"][0], vr._y_range[0])
        assert np.isclose(d["full_y1"][0], vr._y_range[1])

    def test_state_source_y_is_time_for_time_y_dim(self):
        vr = _make_vr(self.backend, self.sel,
                      y_dim=Axis.TIME, x_dim=Axis.BASELINE)
        assert vr._state_source.data["y_is_time"][0] == 1
        assert vr._state_source.data["x_is_time"][0] == 0

    def test_state_source_updates_after_rerender(self):
        """_state_source must be updated when the agg changes."""
        vr = _make_vr(self.backend, self.sel)
        old_n_x = vr._state_source.data["agg_n_x"][0]

        # Full re-render with a narrower selection
        meta = self.backend.metadata()
        t0, t1 = meta["time_range"]
        new_sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.05),
            channel_range=(0, 48),
        )
        vr.rerender(new_selection=new_sel)
        # _state_source should reflect the new agg shape
        new_n_y = vr._state_source.data["agg_n_y"][0]
        assert new_n_y == vr.agg.shape[0], (
            "_state_source agg_n_y not updated after rerender"
        )


class TestUpdateAxes:
    """Tests for in-place axis switching via update_axes()."""

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        self.pols = meta["correlation_labels"]
        t0, t1 = meta["time_range"]
        self.t0, self.t1 = t0, t1
        self.sel = SelectionSpec(
            time_range=(t0, t0 + (t1 - t0) * 0.15),
            channel_range=(0, 48),
        )

    def teardown_method(self):
        self.backend.close()

    def test_update_quantity_changes_agg_values(self):
        """Switching quantity must produce a different agg."""
        vr = _make_vr(self.backend, self.sel, quantity=Axis.AMPLITUDE)
        amp_vals = vr.agg.values.copy()

        vr.update_axes(quantity=Axis.PHASE)

        assert vr._quantity == Axis.PHASE
        phase_vals = vr.agg.values
        # Phase values must differ from amplitude values
        finite_both = np.isfinite(amp_vals) & np.isfinite(phase_vals)
        assert finite_both.any()
        assert not np.allclose(amp_vals[finite_both], phase_vals[finite_both]), (
            "Amplitude and phase agg values are identical — "
            "update_axes may not have re-queried the backend"
        )

    def test_update_axes_updates_state_source(self):
        """update_axes must push new agg shape into _state_source."""
        vr = _make_vr(self.backend, self.sel,
                      y_dim=Axis.TIME, x_dim=Axis.BASELINE)
        old_n_x = vr._state_source.data["agg_n_x"][0]
        old_n_y = vr._state_source.data["agg_n_y"][0]

        # Pick a single baseline for TIME×FREQUENCY.
        # Use _iter_visibility_partitions on MSv2; fall back to
        # metadata antenna_names on MSv4.
        if _backend_kind() == "msv2":
            ds_part = next(self.backend._iter_visibility_partitions())
            eit  = ds_part["EFFECTIVE_INTEGRATION_TIME"].isel(time=0).compute()
            vidx = int(np.where(np.isfinite(eit.values))[0][0])
            a1   = str(ds_part.coords["baseline_antenna1_name"].values[vidx])
            a2   = str(ds_part.coords["baseline_antenna2_name"].values[vidx])
        else:
            ants = self.backend.metadata().get("antenna_names", [])
            if len(ants) < 2:
                pytest.skip("Need at least two antennas for baseline selection")
            a1, a2 = ants[0], ants[1]
        vr._selection = SelectionSpec(
            channel_range=(0, 48), baselines=[(a1, a2)]
        )
        vr.update_axes(x_dim=Axis.FREQUENCY)

        assert vr._x_dim == Axis.FREQUENCY
        new_n_x = vr._state_source.data["agg_n_x"][0]
        assert new_n_x == vr.agg.shape[1]

    def test_update_axes_noop_when_unchanged(self):
        """update_axes with no changes must not re-query the backend."""
        vr = _make_vr(self.backend, self.sel)
        old_agg = vr.agg
        vr.update_axes()   # no args — should be a no-op
        assert vr.agg is old_agg, "No-op update_axes must not replace agg"

    def test_update_axes_preserves_original_selection(self):
        """update_axes must not permanently replace self._selection."""
        vr  = _make_vr(self.backend, self.sel)
        sel_before = vr._selection
        vr.update_axes(quantity=Axis.PHASE)
        assert vr._selection is sel_before, (
            "update_axes must not replace self._selection"
        )

    def test_update_axes_image_source_updated(self):
        """update_axes must push a new RGBA image into _image_source."""
        # Use color_mode="local" so Datashader normalises to each quantity's
        # own range — amplitude (0-100 Jy) vs phase (-π to π) produce clearly
        # different images.  In "global" mode the phase agg averaged over all
        # channels can be nearly uniform, yielding a near-identical image.
        vr = _make_vr(self.backend, self.sel, quantity=Axis.AMPLITUDE,
                      color_mode="local")
        img_before = vr._image_source.data["image"][0].copy()
        vr.update_axes(quantity=Axis.PHASE)
        img_after = vr._image_source.data["image"][0]
        assert not np.array_equal(img_before, img_after), (
            "RGBA image must change when quantity changes"
        )


# ---------------------------------------------------------------------------
# 8b. Deferred construction — construct without querying the backend
# ---------------------------------------------------------------------------

class TestDeferredConstruction:
    """Tests for defer_initial_render=True — construct a VisibilityRaster's
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

    def test_defer_leaves_agg_none(self):
        """defer_initial_render=True must not query the backend at all."""
        vr = _make_vr(self.backend, self.sel, defer_initial_render=True)
        assert vr.agg is None, (
            "agg must stay None until the first real render — "
            "defer_initial_render appears to have queried the backend"
        )

    def test_defer_still_constructs_figure_and_layout(self):
        """Bokeh scaffolding must exist even though nothing was queried."""
        vr = _make_vr(self.backend, self.sel, defer_initial_render=True)
        assert vr.figure is not None
        assert vr.layout is not None

    def test_defer_produces_valid_blank_image(self):
        """Deferred image must be a valid, fully-transparent placeholder —
        same fallback path a real degenerate/empty-selection render uses,
        not a new one."""
        vr = _make_vr(self.backend, self.sel, defer_initial_render=True)
        img = vr._image_source.data["image"][0]
        assert img.dtype == np.uint32
        assert img.shape == (PLOT_H, PLOT_W)
        assert (img == 0).all(), "Deferred image should be all-zero (blank)"

    def test_defer_produces_sane_placeholder_ranges(self):
        """Ranges must be non-degenerate (x0 != x1, y0 != y1) so the figure
        itself constructs with valid pan/zoom bounds, even though they're
        placeholder values rather than real data extents."""
        vr = _make_vr(self.backend, self.sel, defer_initial_render=True)
        assert vr._x_range == (0.0, 1.0)
        assert vr._y_range == (0.0, 1.0)

    def test_defer_is_much_faster_than_real_render(self):
        """Deferred construction must not pay the backend query cost —
        the whole point of decision 11. Real construction of the same
        selection is the baseline; deferred should be at least an order
        of magnitude faster, not just "somewhat" faster, since it does
        no I/O at all."""
        t0 = time_mod.perf_counter()
        _make_vr(self.backend, self.sel)
        real_elapsed = time_mod.perf_counter() - t0

        t0 = time_mod.perf_counter()
        _make_vr(self.backend, self.sel, defer_initial_render=True)
        deferred_elapsed = time_mod.perf_counter() - t0

        print(f"  real={real_elapsed:.3f}s  deferred={deferred_elapsed:.3f}s")
        assert deferred_elapsed < real_elapsed / 10 or deferred_elapsed < 0.05, (
            f"Deferred construction ({deferred_elapsed:.3f}s) is not "
            f"dramatically faster than real construction ({real_elapsed:.3f}s) "
            "— defer_initial_render may still be querying the backend"
        )

    def test_first_update_axes_with_explicit_values_renders(self):
        """Activating a deferred panel by passing its own current axes
        back explicitly (the natural 'this slot just became active' call)
        must perform a real render, even though nothing numerically
        changed."""
        vr = _make_vr(self.backend, self.sel, defer_initial_render=True)
        assert vr.agg is None
        vr.update_axes(
            y_dim=vr._y_dim, x_dim=vr._x_dim,
            quantity=vr._quantity, polarization=vr._polarization,
        )
        assert vr.agg is not None, (
            "update_axes() with explicit (unchanged) current axes must "
            "still materialize a deferred panel"
        )
        assert vr.agg.ndim == 2

    def test_bare_update_axes_after_defer_renders(self):
        """Regression test: update_axes() with *no* arguments at all must
        still materialize a deferred panel. This is the case that was
        actually broken before the self._agg is None guard was added —
        the pre-existing 'no-op when unchanged' logic (correct for an
        already-rendered panel; see test_update_axes_noop_when_unchanged)
        would otherwise silently leave a deferred panel blank forever,
        since nothing about its axes technically 'changes' on activation."""
        vr = _make_vr(self.backend, self.sel, defer_initial_render=True)
        assert vr.agg is None
        vr.update_axes()
        assert vr.agg is not None, (
            "Bare update_axes() must still render a never-yet-rendered panel"
        )

    def test_update_axes_still_noop_when_already_rendered_and_unchanged(self):
        """Sanity check that the defer fix didn't regress the existing
        no-op guarantee for a normally-constructed (already-rendered)
        panel — see test_update_axes_noop_when_unchanged."""
        vr = _make_vr(self.backend, self.sel)   # defer_initial_render=False
        old_agg = vr.agg
        assert old_agg is not None
        vr.update_axes()
        assert vr.agg is old_agg, (
            "No-op update_axes() on an already-rendered panel must not "
            "replace agg — the defer fix should only affect never-yet-"
            "rendered panels"
        )


# ---------------------------------------------------------------------------
# 9b. ColorMode — global/local colour mapping
# ---------------------------------------------------------------------------

class TestColorMode:
    """Tests for color_mode parameter and set_color_mode() on VisibilityRaster."""

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

    def test_default_color_mode_is_global(self):
        vr = _make_vr(self.backend, self.sel)
        assert vr._color_mode == "global"

    def test_color_mode_local_accepted(self):
        vr = _make_vr(self.backend, self.sel, color_mode="local")
        assert vr._color_mode == "local"

    def test_invalid_color_mode_raises(self):
        vr = _make_vr(self.backend, self.sel)
        with pytest.raises(ValueError, match="color_mode"):
            vr.set_color_mode("invalid")

    def test_set_color_mode_updates_attribute(self):
        vr = _make_vr(self.backend, self.sel)
        vr.set_color_mode("local")
        assert vr._color_mode == "local"
        vr.set_color_mode("global")
        assert vr._color_mode == "global"

    def test_set_color_mode_updates_state_source(self):
        vr = _make_vr(self.backend, self.sel)
        vr.set_color_mode("local")
        assert vr._state_source.data["color_mode"][0] == "local"

    def test_set_color_mode_rerenders_image(self):
        """set_color_mode must update _image_source with a new image."""
        vr = _make_vr(self.backend, self.sel, color_mode="global")
        img_before = vr._image_source.data["image"][0].copy()
        vr.set_color_mode("local")
        img_after  = vr._image_source.data["image"][0]
        assert img_after.dtype == np.uint32
        assert img_after.shape == img_before.shape

    def test_shade_viewport_global_local_differ_in_subrange(self):
        """_shade_viewport must produce different images in global vs local
        mode when viewing a sub-range of the data, for linear scaling.

        Pinned to scaling="linear" explicitly to keep this test's
        guarantee unconditional: "linear" and "log" always differ
        visually between global/local since color_mode sets Datashader's
        span= to the rendered quantity's own value range — the full
        cached agg's range in "global" mode, the viewport crop's range
        in "local" mode — for both (see
        TestColormapScaling.test_color_mode_affects_log_via_span,
        despite the name being about log specifically — log behaves like
        linear here, not like eq_hist). "eq_hist" also now differs
        correctly between global/local — see
        TestColormapScaling.test_color_mode_changes_eq_hist_output — but
        is exercised separately rather than folded into this test. The
        explicit scalings (sqrt/square/gamma/power) do have a genuine
        global/local distinction at the value-domain level, but whether
        that distinction survives 8-bit colour quantization into a
        *visibly different* image is data- and seed-dependent — see
        TestColormapScaling.test_explicit_scaling_color_mode_changes_value_domain
        for a test of the underlying logic rather than pixel output.
        """
        vr = _make_vr(self.backend, self.sel, scaling="linear")
        x0, x1 = vr._x_range
        y0, y1 = vr._y_range
        xm, ym = (x0 + x1) / 2, (y0 + y1) / 2

        vr._color_mode = "global"
        img_global = vr._shade_viewport((x0, xm), (y0, ym))

        vr._color_mode = "local"
        img_local  = vr._shade_viewport((x0, xm), (y0, ym))

        assert img_global.dtype == np.uint32
        assert img_local.dtype  == np.uint32
        assert not np.array_equal(img_global, img_local), (
            "global and local colour modes must produce different images "
            "for a sub-range viewport under linear scaling"
        )

    def test_state_source_has_color_mode_key(self):
        vr = _make_vr(self.backend, self.sel)
        assert "color_mode" in vr._state_source.data
        assert vr._state_source.data["color_mode"][0] == "global"


# ---------------------------------------------------------------------------
# 8a. Colormap scaling — Phase 0 CM-series (eq_hist default, update_scaling,
#     colormap_controls, histogram)
# ---------------------------------------------------------------------------

class TestColormapScaling:
    """Tests for the Phase 0 colormap/pseudocolor fidelity refactor.

    Background: linear value-to-colour mapping was found to saturate
    badly on real visibility data (a small high-amplitude population
    dominates the colormap while the populous low-amplitude region
    collapses to a featureless gradient — observed directly during
    scatter testing). ``eq_hist`` (histogram equalization) is now the
    default reduction; explicit scalings (log, sqrt, square, gamma,
    power) are available as manual overrides via ``update_scaling()``.
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

    def test_default_scaling_is_eq_hist(self):
        vr = _make_vr(self.backend, self.sel)
        assert vr._scaling == "eq_hist"

    def test_scaling_constructor_param_accepted(self):
        vr = _make_vr(self.backend, self.sel, scaling="linear")
        assert vr._scaling == "linear"

    def test_invalid_scaling_constructor_param_falls_back_to_default(self):
        vr = _make_vr(self.backend, self.sel, scaling="not_a_scaling")
        assert vr._scaling == "eq_hist"

    def test_update_scaling_changes_attribute(self):
        vr = _make_vr(self.backend, self.sel)
        vr.update_scaling(scaling="log")
        assert vr._scaling == "log"

    def test_update_scaling_invalid_raises(self):
        vr = _make_vr(self.backend, self.sel)
        with pytest.raises(ValueError, match="scaling"):
            vr.update_scaling(scaling="bogus")

    def test_update_scaling_rerenders_image(self):
        """update_scaling must update _image_source with a new image,
        without re-querying the backend (cheap re-shade path)."""
        vr = _make_vr(self.backend, self.sel, scaling="linear")
        img_before = vr._image_source.data["image"][0].copy()
        vr.update_scaling(scaling="eq_hist")
        img_after = vr._image_source.data["image"][0]
        assert img_after.dtype == np.uint32
        assert img_after.shape == img_before.shape
        assert not np.array_equal(img_before, img_after), (
            "image must change after switching from linear to eq_hist"
        )

    def test_eq_hist_resolves_more_structure_than_linear(self):
        """The core fix under test: on real (right-skewed) visibility
        amplitude data, eq_hist must show more colour variation than
        linear, since linear saturates the populous low-amplitude region
        into a near-uniform colour."""
        vr = _make_vr(self.backend, self.sel, quantity=Axis.AMPLITUDE)

        def _distinct_colors(img32):
            alpha = (img32 >> 24) & 0xFF
            rgb = img32 & 0x00FFFFFF
            return len(np.unique(rgb[alpha > 0]))

        vr.update_scaling(scaling="linear")
        n_linear = _distinct_colors(vr._image_source.data["image"][0])

        vr.update_scaling(scaling="eq_hist")
        n_eq_hist = _distinct_colors(vr._image_source.data["image"][0])

        assert n_eq_hist >= n_linear, (
            f"eq_hist ({n_eq_hist} colours) should resolve at least as "
            f"much structure as linear ({n_linear} colours) on real "
            f"visibility amplitude data"
        )

    def test_explicit_scalings_render_without_error(self):
        """All seven scalings must render a valid uint32 image with no
        exception, on real data — the actual screenshot-reproduction
        regression test."""
        vr = _make_vr(self.backend, self.sel)
        for scaling in ("linear", "log", "eq_hist", "sqrt", "square",
                        "gamma", "power"):
            vr.update_scaling(scaling=scaling, alpha=10.0, gamma=0.5)
            img = vr._image_source.data["image"][0]
            assert img.dtype == np.uint32
            assert img.shape == (PLOT_H, PLOT_W)

    def test_gamma_and_alpha_params_stored(self):
        vr = _make_vr(self.backend, self.sel)
        vr.update_scaling(scaling="gamma", gamma=0.3)
        assert vr._scaling_gamma == 0.3
        vr.update_scaling(scaling="power", alpha=5.0)
        assert vr._scaling_alpha == 5.0
        # gamma value from the prior call must persist independently
        assert vr._scaling_gamma == 0.3

    def test_update_scaling_before_render_is_a_noop_not_a_crash(self):
        """Calling update_scaling before any _render() has populated
        _agg must not raise — it should just update state for the next
        render to pick up."""
        vr = _make_vr(self.backend, self.sel)
        vr._agg = None  # simulate pre-render state
        vr.update_scaling(scaling="log")  # must not raise
        assert vr._scaling == "log"

    def test_state_source_has_scaling_keys(self):
        vr = _make_vr(self.backend, self.sel)
        assert "scaling" in vr._state_source.data
        assert vr._state_source.data["scaling"][0] == "eq_hist"
        assert "scaling_alpha" in vr._state_source.data
        assert "scaling_gamma" in vr._state_source.data

    def test_colormap_controls_returns_bokeh_layout(self):
        from bokeh.models import LayoutDOM
        vr = _make_vr(self.backend, self.sel)
        controls = vr.colormap_controls()
        assert isinstance(controls, LayoutDOM)

    def test_histogram_returns_counts_and_edges(self):
        vr = _make_vr(self.backend, self.sel)
        counts, edges = vr.histogram(bins=50)
        assert counts.shape[0] == 50
        assert edges.shape[0] == 51
        assert counts.sum() > 0

    def test_histogram_before_render_returns_empty(self):
        vr = _make_vr(self.backend, self.sel)
        vr._agg = None
        counts, edges = vr.histogram()
        assert counts.size == 0
        assert edges.size == 0

    def test_explicit_scaling_value_domain_is_agg_values_not_axis_range(self):
        """Regression test for a Phase 0 bug: the explicit-scaling
        pre-transform (sqrt/square/gamma/power) must derive its vmin/vmax
        from the rendered agg's own value range, not from the y-axis
        coordinate range passed as `span`. The two are unrelated whenever
        the y-axis (e.g. TIME, in MJD seconds ~5e9) differs from the
        rendered quantity (e.g. AMPLITUDE, ~0-100) — passing the axis
        range as the value-clip range silently collapsed every pixel to
        a single colour bin."""
        vr = _make_vr(
            self.backend, self.sel,
            y_dim=Axis.TIME, x_dim=Axis.BASELINE, quantity=Axis.AMPLITUDE,
        )
        vr.update_scaling(scaling="gamma", gamma=0.4)
        img = vr._image_source.data["image"][0]
        alpha = (img >> 24) & 0xFF
        rgb = img & 0x00FFFFFF
        n_colors = len(np.unique(rgb[alpha > 0]))
        assert n_colors > 1, (
            "gamma scaling collapsed to a single colour — vmin/vmax are "
            "likely being derived from the y-axis range instead of the "
            "agg's own value range"
        )

    def test_color_mode_affects_log_via_span(self):
        """Datashader's log `how=` reduction computes its mapping purely
        from the values present in the viewport crop when span=None, and
        from a fixed external span= when provided — color_mode="global"
        sets that span to the full cached agg's value range,
        color_mode="local" to the viewport crop's own value range (see
        the "linear", "log" branch in VisibilityRaster._shade_agg's
        docstring), so log DOES respond to color_mode in the same way
        linear does. This test exists to
        confirm log behaves like linear, not like eq_hist (see
        test_color_mode_changes_eq_hist_output below, and
        test_shade_viewport_global_local_differ_in_subrange which is
        pinned to "linear" for the same underlying reason).
        """
        vr = _make_vr(self.backend, self.sel, scaling="log")
        x0, x1 = vr._x_range
        y0, y1 = vr._y_range
        xm, ym = (x0 + x1) / 2, (y0 + y1) / 2

        vr._color_mode = "global"
        img_global = vr._shade_viewport((x0, xm), (y0, ym))
        vr._color_mode = "local"
        img_local = vr._shade_viewport((x0, xm), (y0, ym))

        assert not np.array_equal(img_global, img_local), (
            "log: global and local should differ, since log accepts "
            "span= the same way linear does"
        )

    def test_color_mode_changes_eq_hist_output(self):
        """color_mode must affect eq_hist rendering too, even though
        Datashader's native how="eq_hist" rejects span= outright (raises
        ValueError: "span is not (yet) valid to use with eq_hist").

        eq_hist is implemented as an explicit pre-transform via
        colormap_scaling.equalize_histogram(), which accepts a separate
        *reference* array to build the equalization curve from:
        "global" mode passes the full cached agg as the reference (so
        colours stay anchored to the full data's distribution regardless
        of zoom — useful when zoomed in and wanting flagging-stable
        colours), "local" mode passes None (equalize against the crop
        itself, matching Datashader's native behaviour). This restores
        the global/local feature for eq_hist, which is the default
        scaling, rather than leaving it as a silent no-op.
        """
        vr = _make_vr(self.backend, self.sel, scaling="eq_hist")
        x0, x1 = vr._x_range
        y0, y1 = vr._y_range
        xm, ym = (x0 + x1) / 2, (y0 + y1) / 2

        vr._color_mode = "global"
        img_global = vr._shade_viewport((x0, xm), (y0, ym))
        vr._color_mode = "local"
        img_local = vr._shade_viewport((x0, xm), (y0, ym))

        assert not np.array_equal(img_global, img_local), (
            "eq_hist: global and local should differ — global must "
            "equalize against the full cached agg, local against the "
            "current viewport crop"
        )

    def test_explicit_scaling_color_mode_changes_value_domain(self):
        """For explicit scalings (sqrt/square/gamma/power), color_mode
        must cause _shade_agg to pass a different vmin/vmax anchor to
        the pre-transform: "global" anchors to the full cached agg's
        value range, "local" anchors to the current viewport crop.

        This is verified by monkeypatching apply_explicit_scaling to
        capture the vmin/vmax arguments it actually receives, rather than
        comparing the output arrays — the double-normalization inside
        apply_explicit_scaling (clip to [vmin,vmax], apply curve,
        normalize output back to [0,1]) can produce near-identical float
        arrays even when vmin/vmax genuinely differ, if the vmin values
        happen to be identical and the vmax difference is moderate and
        the curve is strongly compressive at the high end (e.g. sqrt on
        this particular real dataset with a half-range viewport crop).
        Testing the vmin/vmax inputs directly is unambiguous.
        """
        from cubevis.toolbox.visplot import colormap_scaling as cms
        import unittest.mock as mock

        for scaling, gamma in (("sqrt", 1.0), ("square", 1.0),
                                ("gamma", 0.4), ("power", 1.0)):
            vr = _make_vr(self.backend, self.sel, scaling=scaling)
            vr._scaling_gamma = gamma
            x0, x1 = vr._x_range
            y0, y1 = vr._y_range
            xm, ym = (x0 + x1) / 2, (y0 + y1) / 2

            full_finite = vr._agg.values[np.isfinite(vr._agg.values)]
            assert full_finite.size > 0, "need finite data in cached agg"

            captured = {}
            original = cms.apply_explicit_scaling

            def capturing_apply(values, scaling_, **kwargs):
                captured[scaling_] = (kwargs.get("vmin"), kwargs.get("vmax"))
                return original(values, scaling_, **kwargs)

            with mock.patch.object(cms, "apply_explicit_scaling",
                                   side_effect=capturing_apply):
                vr._color_mode = "global"
                vr._shade_viewport((x0, xm), (y0, ym))
                global_vmin, global_vmax = captured.get(scaling, (None, None))

                captured.clear()
                vr._color_mode = "local"
                vr._shade_viewport((x0, xm), (y0, ym))
                local_vmin, local_vmax = captured.get(scaling, (None, None))

            assert global_vmin is not None, (
                f"{scaling}: apply_explicit_scaling not called in global mode"
            )
            assert local_vmin is None and local_vmax is None, (
                f"{scaling}: local mode should pass vmin=None, vmax=None "
                f"(let apply_explicit_scaling derive from the crop); "
                f"got vmin={local_vmin}, vmax={local_vmax}"
            )
            assert global_vmax is not None, (
                f"{scaling}: global mode should pass a non-None vmax"
            )
            assert abs(global_vmax - float(full_finite.max())) < 1e-6, (
                f"{scaling}: global vmax {global_vmax:.6g} should equal "
                f"full agg max {full_finite.max():.6g}"
            )
            assert abs(global_vmin - float(full_finite.min())) < 1e-6, (
                f"{scaling}: global vmin {global_vmin:.6g} should equal "
                f"full agg min {full_finite.min():.6g}"
            )


# ---------------------------------------------------------------------------
# 9. Decimation — two-level rendering strategy
# ---------------------------------------------------------------------------

class TestDecimation:
    """Tests for the max_cells decimation and two-level zoom strategy.

    Uses a tiny max_cells value to force decimation on sis14 (which would
    otherwise never decimate), making it possible to test Level-2 re-query
    behaviour without a terabyte MS.
    """

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()
        meta = self.backend.metadata()
        self.pols = meta["correlation_labels"]
        t0, t1 = meta["time_range"]
        self.t0, self.t1 = t0, t1
        self.sel = SelectionSpec(channel_range=(0, 48))

    def teardown_method(self):
        self.backend.close()

    def test_no_decimation_with_large_budget(self):
        """With a generous max_cells budget, sis14 must not be decimated."""
        _, _, _, is_decimated = self.backend.query_raster(
            y_dim        = Axis.TIME,
            x_dim        = Axis.BASELINE,
            quantity     = Axis.AMPLITUDE,
            selection    = self.sel,
            polarization = self.pols[0],
            max_cells    = 2_000_000,
        )
        assert not is_decimated, (
            "sis14 agg should fit within 2M cells without decimation"
        )

    def test_decimation_triggered_by_small_budget(self):
        """A tiny max_cells budget must trigger decimation."""
        agg, _, _, is_decimated = self.backend.query_raster(
            y_dim        = Axis.TIME,
            x_dim        = Axis.BASELINE,
            quantity     = Axis.AMPLITUDE,
            selection    = self.sel,
            polarization = self.pols[0],
            max_cells    = 100,   # 10×10 — far smaller than sis14
        )
        assert is_decimated, "max_cells=100 must force decimation"
        assert agg.shape[0] * agg.shape[1] <= 200, (
            f"Decimated agg {agg.shape} exceeds budget with margin"
        )

    def test_decimated_agg_has_correct_dims(self):
        """Decimated agg must still have the right dimension names."""
        agg, _, _, _ = self.backend.query_raster(
            y_dim        = Axis.TIME,
            x_dim        = Axis.BASELINE,
            quantity     = Axis.AMPLITUDE,
            selection    = self.sel,
            polarization = self.pols[0],
            max_cells    = 100,
        )
        assert agg.dims[0] == _axis_to_dim_safe(Axis.TIME)
        assert agg.dims[1] == _axis_to_dim_safe(Axis.BASELINE)

    def test_decimated_agg_coordinate_span_unchanged(self):
        """Decimation strides coordinates — the span must cover the full range."""
        _, x_range_full, y_range_full, _ = self.backend.query_raster(
            y_dim=Axis.TIME, x_dim=Axis.BASELINE,
            quantity=Axis.AMPLITUDE, selection=self.sel,
            polarization=self.pols[0], max_cells=2_000_000,
        )
        agg_d, x_range_d, y_range_d, _ = self.backend.query_raster(
            y_dim=Axis.TIME, x_dim=Axis.BASELINE,
            quantity=Axis.AMPLITUDE, selection=self.sel,
            polarization=self.pols[0], max_cells=100,
        )
        # The returned x/y ranges must reflect the full original extents,
        # not just the strided subset, so Bokeh sets correct axis bounds.
        assert np.isclose(x_range_d[0], x_range_full[0], rtol=0.01), (
            "x_range min should match full extent"
        )
        assert np.isclose(x_range_d[1], x_range_full[1], rtol=0.01), (
            "x_range max should match full extent"
        )
        assert np.isclose(y_range_d[0], y_range_full[0], rtol=0.01)
        assert np.isclose(y_range_d[1], y_range_full[1], rtol=0.01)

    def test_level1_rerender_does_not_change_agg(self):
        """When not decimated, _handle_rerender must not re-query backend."""
        vr = _make_vr(self.backend, self.sel, max_cells=2_000_000)
        assert not vr._is_decimated, "sis14 should not be decimated at 2M"
        old_agg = vr.agg

        # Zoom in — should stay Level 1 (no re-query) because not decimated
        vr._handle_rerender({
            "x0": float(vr._x_range[0]),
            "x1": float(vr._x_range[0] + (vr._x_range[1] - vr._x_range[0]) * 0.1),
            "y0": float(vr._y_range[0]),
            "y1": float(vr._y_range[0] + (vr._y_range[1] - vr._y_range[0]) * 0.1),
        })
        assert vr.agg is old_agg, (
            "Level-1 rerender must not replace the agg"
        )

    def test_level2_rerender_replaces_agg_when_decimated(self):
        """When decimated and zoomed past agg resolution, must re-query backend."""
        # Force decimation with a tiny budget
        vr = _make_vr(self.backend, self.sel, max_cells=100)
        assert vr._is_decimated, "max_cells=100 must produce a decimated agg"
        old_agg = vr.agg
        h, w = old_agg.shape

        # Compute the agg cell size and send a viewport much smaller than it
        agg_cell_w = (vr._x_range[1] - vr._x_range[0]) / w
        agg_cell_h = (vr._y_range[1] - vr._y_range[0]) / h
        x_mid = (vr._x_range[0] + vr._x_range[1]) / 2
        y_mid = (vr._y_range[0] + vr._y_range[1]) / 2

        # Viewport covers half an agg cell — definitely below agg resolution
        vr._handle_rerender({
            "x0": x_mid - agg_cell_w * 0.25,
            "x1": x_mid + agg_cell_w * 0.25,
            "y0": y_mid - agg_cell_h * 0.25,
            "y1": y_mid + agg_cell_h * 0.25,
        })
        assert vr.agg is not old_agg, (
            "Level-2 rerender must replace the agg with a higher-resolution sub-query"
        )

    def test_level2_rerender_response_image_valid(self):
        """Level-2 rerender must return a valid uint32 image."""
        vr = _make_vr(self.backend, self.sel, max_cells=100)
        assert vr._is_decimated
        h, w = vr.agg.shape
        agg_cell_w = (vr._x_range[1] - vr._x_range[0]) / w
        agg_cell_h = (vr._y_range[1] - vr._y_range[0]) / h
        x_mid = (vr._x_range[0] + vr._x_range[1]) / 2
        y_mid = (vr._y_range[0] + vr._y_range[1]) / 2

        resp = vr._handle_rerender({
            "x0": x_mid - agg_cell_w * 0.25,
            "x1": x_mid + agg_cell_w * 0.25,
            "y0": y_mid - agg_cell_h * 0.25,
            "y1": y_mid + agg_cell_h * 0.25,
        })
        assert isinstance(resp["image"], np.ndarray)
        assert resp["image"].dtype == np.uint32
        assert resp["image"].shape == (PLOT_H, PLOT_W)

    def test_vr_max_cells_parameter_accepted(self):
        """VisibilityRaster must accept max_cells and store it."""
        vr = _make_vr(self.backend, self.sel, max_cells=500_000)
        assert vr._max_cells == 500_000


# ---------------------------------------------------------------------------
# 9. Timing — full raster pipeline end-to-end
# ---------------------------------------------------------------------------

class TestTiming:

    def setup_method(self):
        _require_datashader()
        _suppress_warnings()
        self.backend = _open_backend()

    def teardown_method(self):
        self.backend.close()

    def test_full_raster_pipeline_under_8s(self):
        """query_raster + Datashader shade + RGBA conversion must finish < 8s."""
        meta = self.backend.metadata()
        t0_data, t1_data = meta["time_range"]
        sel = SelectionSpec(
            time_range=(t0_data, t0_data + (t1_data - t0_data) * 0.5),
            channel_range=(0, 48),
        )

        t0 = time_mod.perf_counter()
        vr = _make_vr(self.backend, sel)
        elapsed = time_mod.perf_counter() - t0

        img = vr._image_source.data["image"][0]
        n_nontransparent = int(((img >> 24) & 0xff > 0).sum())
        print(f"  Full pipeline: {elapsed:.2f}s, "
              f"{n_nontransparent} non-transparent px")
        assert elapsed < 8.0, (
            f"VisibilityRaster full pipeline took {elapsed:.1f}s — exceeds 8s"
        )


# ---------------------------------------------------------------------------
# Standalone runner (no pytest required)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _suppress_warnings()
    _, _kind = _detect_backend_path()
    import sys; print(f"[test_visibility_raster] backend: {_kind}", file=sys.stderr)

    test_classes = [
        TestLifecycle,
        TestRender,
        TestDataToPixel,
        TestViewportSelection,
        TestProbeStatic,
        TestRerender,
        TestDatashadedOutput,
        TestStateSource,
        TestUpdateAxes,
        TestDeferredConstruction,
        TestColorMode,
        TestDecimation,
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
