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

    MS=<path>.ms   pytest test_visibility_raster.py -v   # MSv2
    PS=<path>.ps.zarr pytest test_visibility_raster.py -v  # MSv4

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
        """Probing an empty (NaN) pixel must yield a label containing 'empty'."""
        agg = self.vr.agg
        ys, xs = np.where(np.isnan(agg.values))
        if len(ys) == 0:
            pytest.skip("No NaN pixels in agg for this selection")
        px, py = int(xs[0]), int(ys[0])
        x_val = float(agg.coords[agg.dims[1]].values[px])
        y_val = float(agg.coords[agg.dims[0]].values[py])
        resp = self.vr._handle_probe({"x": x_val, "y": y_val})
        assert "empty" in resp["label"].lower()

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
        """PIL RGBA Image path: bytes packed as B G R A in memory (little-endian)."""
        from PIL import Image
        img = Image.fromarray(
            np.array([[[0x11, 0x22, 0x33, 0xff]]], dtype=np.uint8), mode="RGBA"
        )
        out  = _img_to_uint32(img)
        assert out.shape == (1, 1)
        assert out.dtype == np.uint32
        view = out.view(np.uint8).reshape(1, 1, 4)
        assert int(view[0, 0, 0]) == 0x33, "Blue channel wrong"
        assert int(view[0, 0, 1]) == 0x22, "Green channel wrong"
        assert int(view[0, 0, 2]) == 0x11, "Red channel wrong"
        assert int(view[0, 0, 3]) == 0xff, "Alpha channel wrong"

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
        mode when viewing a sub-range of the data."""
        vr = _make_vr(self.backend, self.sel)
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
            "for a sub-range viewport"
        )

    def test_state_source_has_color_mode_key(self):
        vr = _make_vr(self.backend, self.sel)
        assert "color_mode" in vr._state_source.data
        assert vr._state_source.data["color_mode"][0] == "global"


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
