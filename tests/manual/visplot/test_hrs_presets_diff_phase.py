"""
test_hrs_presets_diff_phase.py
==============================
HRS slice of 2026-10-07, which closes H2 and most of H3:

1. ``AMP_VDIFF`` / ``PHASE_DIFF`` raster quantities
   (``data/_raster_diff.py``): difference from the mean of the other
   samples in the same time window.
2. Phase display: cyclic colormap, fixed linear -180..180 scaling, and
   a resample that takes a circular mean (H1b).
3. Presets ``phaserms-time`` / ``phaserms-freq`` / ``phaserms-uvdist``
   and ``phase-waterfall``, and the table-driven toolbar buttons.
4. Status-area help for the remaining gear-tab controls and the toolbar
   (which had Bokeh tooltips: a second kind of help).

Location in repository:
    cubevis/tests/manual/visplot/test_hrs_presets_diff_phase.py
"""
from __future__ import annotations

import asyncio
import warnings

import numpy as np
import pytest
import xarray as xr

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot import palettes
from cubevis.toolbox.visplot.data import _raster_diff as rd
from cubevis.toolbox.visplot.data._raster_diff import (
    DIFF_QUANTITIES, describe_diff_window, diff_from_window_mean,
    resolve_diff_window,
)


# ---------------------------------------------------------------------------
# 1. Difference from the window mean
# ---------------------------------------------------------------------------

def _da(z, flag=None, scan=None, dt=1.0):
    """(time, baseline_id, frequency) complex DataArray + flag."""
    z = np.asarray(z, dtype=np.complex128)
    nt = z.shape[0]
    coords = {"time": np.arange(nt) * dt}
    if scan is not None:
        coords["scan_name"] = ("time", np.asarray(scan))
    vis = xr.DataArray(z, dims=("time", "baseline_id", "frequency"),
                       coords=coords)
    f = np.zeros(z.shape, bool) if flag is None else np.asarray(flag, bool)
    return vis, xr.DataArray(f, dims=vis.dims, coords=coords)


class TestDiffDefinition:

    def test_quantities(self):
        assert DIFF_QUANTITIES == (Axis.AMP_VDIFF, Axis.PHASE_DIFF)
        assert Axis.AMP_VDIFF.label == "Amp V Diff"
        assert Axis.PHASE_DIFF.unit == "deg"

    def test_constant_signal_reads_zero(self):
        z = np.full((10, 2, 3), 2.0 * np.exp(0.7j))
        vis, flag = _da(z)
        for q in DIFF_QUANTITIES:
            out = diff_from_window_mean(vis, flag, q).values
            assert np.allclose(out, 0.0, atol=1e-9)

    def test_one_outlier_reads_its_full_size(self):
        # Nine samples of 1 and one of 4: the outlier is compared with
        # the mean of the OTHER nine (1), so it reads 3, not 2.7.
        z = np.ones((10, 1, 1), complex)
        z[4] = 4.0
        vis, flag = _da(z)
        out = diff_from_window_mean(vis, flag, Axis.AMP_VDIFF).values[:, 0, 0]
        assert out[4] == pytest.approx(3.0)
        # ...and the others are each pulled by it: mean of the other
        # nine is (8 + 4) / 9.
        assert out[0] == pytest.approx(12.0 / 9.0 - 1.0)

    def test_phase_jump_reads_its_angle(self):
        z = np.ones((9, 1, 1), complex)
        z[3] = np.exp(1j * np.deg2rad(60.0))
        vis, flag = _da(z)
        out = diff_from_window_mean(vis, flag, Axis.PHASE_DIFF).values[:, 0, 0]
        assert out[3] == pytest.approx(60.0)
        assert np.all(out >= 0)

    def test_phase_diff_across_the_wrap(self):
        # +170 against a reference at -170: 20 deg apart, not 340.
        z = np.full((6, 1, 1), np.exp(1j * np.deg2rad(-170.0)))
        z[0] = np.exp(1j * np.deg2rad(170.0))
        vis, flag = _da(z)
        out = diff_from_window_mean(vis, flag, Axis.PHASE_DIFF).values[:, 0, 0]
        assert out[0] == pytest.approx(20.0)

    def test_matches_a_direct_computation(self):
        rng = np.random.default_rng(3)
        z = rng.standard_normal((12, 3, 5)) + 1j * rng.standard_normal((12, 3, 5))
        vis, flag = _da(z)
        amp = diff_from_window_mean(vis, flag, Axis.AMP_VDIFF).values
        ph = diff_from_window_mean(vis, flag, Axis.PHASE_DIFF).values
        for i in range(12):
            ref = np.delete(z, i, axis=0).mean(axis=0)
            assert np.allclose(amp[i], np.abs(z[i] - ref))
            assert np.allclose(ph[i], np.abs(np.rad2deg(np.angle(z[i] * np.conj(ref)))))

    def test_each_baseline_and_channel_has_its_own_mean(self):
        z = np.ones((8, 2, 2), complex)
        z[:, 1, :] = 5.0          # a different, equally steady, baseline
        vis, flag = _da(z)
        out = diff_from_window_mean(vis, flag, Axis.AMP_VDIFF).values
        assert np.allclose(out, 0.0)


class TestDiffWindows:

    def test_auto_and_off_mean_a_scan(self):
        assert resolve_diff_window("auto") == "scan"
        assert resolve_diff_window("off") == "scan"
        assert resolve_diff_window("scan") == "scan"
        assert resolve_diff_window(30) == 30
        assert describe_diff_window("auto") == "vs scan mean"
        assert describe_diff_window("60") == "vs 60 s mean"

    def test_mean_never_crosses_a_scan(self):
        # Two scans at different levels, each steady: nothing changed
        # WITHIN a scan, so the difference is zero everywhere.
        z = np.ones((10, 1, 1), complex)
        z[5:] = 3.0
        vis, flag = _da(z, scan=["a"] * 5 + ["b"] * 5)
        out = diff_from_window_mean(vis, flag, Axis.AMP_VDIFF).values
        assert np.allclose(out, 0.0)
        # Without scan labels and without a gap it is one window.
        vis, flag = _da(z)
        out = diff_from_window_mean(vis, flag, Axis.AMP_VDIFF).values
        assert np.all(out > 1.0)

    def test_window_in_seconds(self):
        z = np.ones((12, 1, 1), complex)
        z[6:] = 3.0
        vis, flag = _da(z, dt=10.0)
        out = diff_from_window_mean(vis, flag, Axis.AMP_VDIFF, 60).values
        assert np.allclose(out, 0.0)        # the step is on a window edge

    def test_flagged_samples_neither_count_nor_show(self):
        z = np.ones((8, 1, 1), complex)
        z[2] = 100.0
        f = np.zeros(z.shape, bool)
        f[2] = True
        vis, flag = _da(z, f)
        out = diff_from_window_mean(vis, flag, Axis.AMP_VDIFF).values[:, 0, 0]
        assert np.isnan(out[2])
        assert np.allclose(np.delete(out, 2), 0.0)

    def test_a_lone_sample_has_nothing_to_compare_with(self):
        z = np.ones((4, 1, 1), complex)
        vis, flag = _da(z, scan=["a", "b", "b", "b"])
        out = diff_from_window_mean(vis, flag, Axis.AMP_VDIFF).values[:, 0, 0]
        assert np.isnan(out[0]) and np.allclose(out[1:], 0.0)

    def test_lazy_and_chunking_independent(self):
        rng = np.random.default_rng(5)
        z = rng.standard_normal((16, 4, 6)) + 1j * rng.standard_normal((16, 4, 6))
        vis, flag = _da(z, scan=["a"] * 8 + ["b"] * 8)
        want = diff_from_window_mean(vis, flag, Axis.AMP_VDIFF).values
        lazy = diff_from_window_mean(
            vis.chunk({"time": 3, "baseline_id": 2}),
            flag.chunk({"time": 3, "baseline_id": 2}), Axis.AMP_VDIFF)
        assert lazy.chunks is not None
        assert lazy.dims == vis.dims
        assert np.allclose(lazy.values, want)

    def test_needs_time_and_a_diff_quantity(self):
        vis, flag = _da(np.ones((4, 1, 1)))
        with pytest.raises(ValueError):
            diff_from_window_mean(vis, flag, Axis.AMPLITUDE)
        with pytest.raises(ValueError):
            diff_from_window_mean(vis.isel(time=0), flag.isel(time=0),
                                  Axis.AMP_VDIFF)


# ---------------------------------------------------------------------------
# 2. Phase display
# ---------------------------------------------------------------------------

class TestCyclicColormap:

    def test_ends_meet(self):
        cm = palettes.cyclic_cmap()
        assert cm[0] == cm[-1]
        assert len(set(cm[:-1])) == len(cm) - 1

    def test_constant_lightness_clear_of_both_backgrounds(self):
        # _luminance is a plain luma, not perceptual lightness (which is
        # what the map holds constant), so this is a loose bound; plasma
        # spans about 0.8 on the same measure.
        lum = [palettes._luminance(c) for c in palettes.cyclic_cmap()]
        assert max(lum) - min(lum) < 0.2
        for theme in ("dark", "light"):
            bg = palettes._rgb(palettes.BACKGROUNDS[theme])
            for c in palettes.cyclic_cmap():
                assert palettes._dist(palettes._rgb(c), bg) > palettes.RASTER_MIN_DIST

    def test_not_offered_as_a_general_raster_colormap(self):
        assert "phase" not in palettes.raster_names()


class _Stub:
    """Enough of a VisibilityRaster for _shade_cmap / _resample."""
    from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster as _VR
    _shade_cmap = _VR._shade_cmap
    _resample = _VR._resample

    def __init__(self, quantity):
        self._quantity = quantity
        self._cmap = ["#000000", "#ffffff"]


class TestPhaseShading:

    def test_phase_uses_the_cyclic_ramp_and_nothing_else_does(self):
        assert tuple(_Stub(Axis.PHASE)._shade_cmap()) == palettes.cyclic_cmap()
        for q in (Axis.AMPLITUDE, Axis.PHASE_RMS, Axis.PHASE_DIFF, Axis.Z_SCORE):
            assert _Stub(q)._shade_cmap() == ["#000000", "#ffffff"]

    def _agg(self, cols):
        a = np.asarray(cols, dtype=np.float64)
        return xr.DataArray(a, dims=("y", "x"),
                            coords={"y": np.arange(a.shape[0], dtype=float),
                                    "x": np.arange(a.shape[1], dtype=float)})

    def test_downsampling_across_the_wrap_is_a_circular_mean(self):
        import datashader as ds
        # Columns alternate +179 / -179; four columns per pixel.
        row = np.tile([179.0, -179.0], 32)
        agg = self._agg(np.tile(row, (8, 1)))
        cvs = ds.Canvas(plot_width=16, plot_height=8,
                        x_range=(0, 63), y_range=(0, 7))
        plain = cvs.raster(agg, interpolate="linear").values
        safe = _Stub(Axis.PHASE)._resample(cvs, agg, "linear").values
        assert np.nanmax(np.abs(plain)) < 90          # the bug: reads ~0
        assert np.nanmin(np.abs(safe)) > 178.9        # 180, either sign

    def test_other_quantities_are_resampled_as_before(self):
        import datashader as ds
        agg = self._agg(np.arange(64.0).reshape(8, 8))
        cvs = ds.Canvas(plot_width=4, plot_height=4,
                        x_range=(0, 7), y_range=(0, 7))
        a = cvs.raster(agg, interpolate="linear").values
        b = _Stub(Axis.AMPLITUDE)._resample(cvs, agg, "linear").values
        assert np.array_equal(a, b, equal_nan=True)

    def test_values_and_blanks_survive(self):
        import datashader as ds
        a = np.full((6, 6), 45.0)
        a[2, 3] = np.nan
        agg = self._agg(a)
        cvs = ds.Canvas(plot_width=6, plot_height=6,
                        x_range=(0, 5), y_range=(0, 5))
        out = _Stub(Axis.PHASE)._resample(cvs, agg, "nearest").values
        assert np.isnan(out).sum() == 1
        assert np.allclose(out[np.isfinite(out)], 45.0)


# ---------------------------------------------------------------------------
# 3 + 4. Presets and help, on a real plotter over simulated data
# ---------------------------------------------------------------------------

def _sim_transform(desc, data):
    rng = np.random.default_rng(11 + int(desc.chunk_id))
    dims, vis = data["DATA"]
    ph = np.deg2rad(rng.normal(0, 10.0, vis.shape))
    data["DATA"] = (dims, np.exp(1j * ph).astype(np.complex64))
    fdims, _ = data["FLAG"]
    data["FLAG"] = (fdims, np.zeros(vis.shape, dtype=bool))
    return data


@pytest.fixture(scope="module")
def sim_paths(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    root = tmp_path_factory.mktemp("hrsslice")
    ms = str(root / "s.ms")
    sim.MSStructureSimulator(
        ntime=20, nantenna=5, auto_corrs=False,
        data_description=[(32, ["XX", "YY"])],
        simulate_data=True, transform_data=_sim_transform).simulate_ms(ms)
    ps = str(root / "s.ps.zarr")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        xr.open_datatree(ms, engine="xarray-ms:msv2",
                         partition_schema=["FIELD_ID"]).to_zarr(ps, mode="w", compute=True)
    return {"msv2": dict(ms=ms), "msv4": dict(ps=ps)}


def _plotter(paths, fmt, **kw):
    warnings.filterwarnings("ignore")
    from cubevis.toolbox.visplot import VisibilityPlotter
    return VisibilityPlotter(correlation="XX", **kw, **paths[fmt])


NEW_PRESETS = {
    "phaserms-time":   (Axis.BASELINE, Axis.TIME, Axis.PHASE_RMS, Axis.TIME, Axis.PHASE_RMS, "side"),
    "phaserms-freq":   (Axis.BASELINE, Axis.CHANNEL, Axis.PHASE_RMS, Axis.CHANNEL, Axis.PHASE_RMS, "side"),
    "phaserms-uvdist": (Axis.BASELINE, Axis.TIME, Axis.PHASE_RMS, Axis.UVDIST, Axis.PHASE_RMS, "side"),
    "phase-waterfall": (Axis.TIME, Axis.CHANNEL, Axis.PHASE, Axis.TIME, Axis.PHASE, "over"),
}


class TestPresetTable:

    def test_definitions(self):
        from cubevis.toolbox.visplot.visibility_plotter import _PRESETS
        for name, want in NEW_PRESETS.items():
            assert _PRESETS[name] == want

    def test_every_preset_has_exactly_one_button(self):
        from cubevis.toolbox.visplot.visibility_plotter import (
            _PRESETS, _PRESET_BUTTONS, _PRESETS_RESET_STAT)
        assert [b[0] for b in _PRESET_BUTTONS] == list(_PRESETS)
        assert set(_PRESETS_RESET_STAT) < set(_PRESETS)

    def test_options_exist_for_everything_a_preset_sets(self):
        from cubevis.toolbox.visplot import visibility_plotter as vp
        opts = lambda o: {k for k, _ in o}
        for ry, rx, rq, sx, sy, _ in vp._PRESETS.values():
            assert ry.name in opts(vp._RASTER_AXIS_OPTIONS)
            assert rx.name in opts(vp._RASTER_AXIS_OPTIONS)
            assert rq.name in opts(vp._RASTER_QTY_OPTIONS)
            assert sx.name in opts(vp._SCATTER_X_OPTIONS)
            assert sy.name in opts(vp._SCATTER_Y_OPTIONS)
        assert {"AMP_VDIFF", "PHASE_DIFF"} <= opts(vp._RASTER_QTY_OPTIONS)
        assert not {"AMP_VDIFF", "PHASE_DIFF"} & opts(vp._SCATTER_Y_OPTIONS)


@pytest.mark.parametrize("fmt", ["msv2", "msv4"])
class TestOnAPlotter:

    @pytest.mark.parametrize("name", list(NEW_PRESETS))
    def test_constructor_preset(self, sim_paths, fmt, name):
        vp = _plotter(sim_paths, fmt, preset=name.replace("-", "_"))
        try:
            ry, rx, rq, sx, sy, layout = NEW_PRESETS[name]
            r, s = vp._slots[0].raster, vp._slots[1].scatter
            assert (r._y_dim, r._x_dim, r._quantity) == (ry, rx, rq)
            assert s._x_dim == sx and s.layers[0].y_axis == sy
            assert vp._layout == layout
            assert r.agg is not None and np.isfinite(r.agg.values).any()
            assert rq.label in r.figure.title.text
        finally:
            vp.close()

    def test_phase_rms_values_are_the_simulated_noise(self, sim_paths, fmt):
        vp = _plotter(sim_paths, fmt, preset="phaserms-time")
        try:
            a = vp._slots[0].raster.agg.values
            assert 7.0 < np.nanmedian(a) < 13.0          # 10 deg put in
        finally:
            vp.close()

    def test_phase_panel_is_linear_over_the_circle(self, sim_paths, fmt):
        vp = _plotter(sim_paths, fmt, preset="phase-waterfall")
        try:
            r = vp._slots[0].raster
            assert (r._scaling, r._scaling_vmin, r._scaling_vmax) == ("linear", -180.0, 180.0)
            assert tuple(r._shade_cmap()) == palettes.cyclic_cmap()
            assert tuple(r._panel_spec().bands[0].cmap) == palettes.cyclic_cmap()
            # Leaving Phase gives the panel's own colormap and scaling back.
            r.update_axes(quantity=Axis.AMPLITUDE)
            assert r._scaling == "eq_hist" and r._scaling_vmin is None
            assert tuple(r._shade_cmap()) != palettes.cyclic_cmap()
        finally:
            vp.close()

    @pytest.mark.parametrize("q, lo, hi", [
        # 10 deg of noise on unit phasors: |V - M| ~ 0.17, angle ~ 8 deg.
        ("AMP_VDIFF", 0.08, 0.30), ("PHASE_DIFF", 4.0, 14.0)])
    @pytest.mark.parametrize("y, x", [("BASELINE", "TIME"), ("TIME", "CHANNEL"),
                                      ("BASELINE", "CHANNEL")])
    def test_diff_rasters(self, sim_paths, fmt, q, lo, hi, y, x):
        vp = _plotter(sim_paths, fmt, raster_y=y, raster_x=x, raster_qty=q)
        try:
            r = vp._slots[0].raster
            assert lo < float(np.nanmean(r.agg.values)) < hi
            assert f"{Axis[q].label} (vs scan mean)" in r.figure.title.text
        finally:
            vp.close()

    def test_diff_window_is_the_time_window_control(self, sim_paths, fmt):
        vp = _plotter(sim_paths, fmt, raster_y="BASELINE", raster_x="TIME",
                      raster_qty="AMP_VDIFF")
        try:
            r = vp._slots[0].raster
            r.update_axes(stat_time_window="60")
            assert "(vs 60 s mean)" in r._effective_title()
        finally:
            vp.close()

    def test_preset_buttons_and_their_js(self, sim_paths, fmt):
        from cubevis.toolbox.visplot.visibility_plotter import (
            _PRESETS, _PRESETS_RESET_STAT)
        vp = _plotter(sim_paths, fmt, layout="side")
        try:
            vp._build_layout()
            assert list(vp._preset_buttons) == list(_PRESETS)
            for (name, btn), js in zip(vp._preset_buttons.items(),
                                       vp._preset_js_objects):
                ry, rx, rq, sx, sy, _ = _PRESETS[name]
                assert f"panel0_rq_sel.value = '{rq.name}'" in js.code
                assert f"panel1_sy_sel.value = '{sy.name}'" in js.code
                assert f"panel1_sx_sel.value = '{sx.name}'" in js.code
                resets = "panel1_st_sel.value = 'auto'" in js.code
                assert resets == (name in _PRESETS_RESET_STAT)
                for arg in ("panel0_rd_sel", "panel0_rt_sel", "panel0_rc_sel",
                            "panel1_sd_sel", "panel1_st_sel", "panel1_sc_sel"):
                    assert arg in js.args
        finally:
            vp.close()

    def test_help_is_wired_everywhere(self, sim_paths, fmt):
        from bokeh.models import Div
        from cubevis.bokeh.models import EvHover, Tip
        from cubevis.toolbox.visplot.visibility_plotter import (
            _STATIC_HINTS, _PRESET_BUTTONS)
        vp = _plotter(sim_paths, fmt, layout="side")
        try:
            root = vp._build_layout()
            names = list(_STATIC_HINTS) + [f"preset_{b[0]}" for b in _PRESET_BUTTONS]
            hints = {n: getattr(vp, f"_hint_{n}") for n in names}
            for n, h in hints.items():
                assert isinstance(h, Div) and h.text and not h.visible, n
                assert h in vp._status_col.children, n
            # Every hint is the target of at least one hover wrapper.
            wired = set()
            for m in root.references():
                if isinstance(m, EvHover):
                    for cbs in m.js_event_callbacks.values():
                        for cb in cbs:
                            wired.add(cb.args["hint"].id)
            for n, h in hints.items():
                assert h.id in wired, n
            # The toolbar has one kind of help: no tooltips left on it.
            for btn in vp._preset_buttons.values():
                assert not any(isinstance(m, Tip) and m.child is btn
                               for m in root.references())
        finally:
            vp.close()

    def test_plot_request_with_a_diff_quantity(self, sim_paths, fmt):
        vp = _plotter(sim_paths, fmt, layout="side")
        try:
            msg = {"action": "plot", "datacolumn": "DATA", "field": "",
                   "spw": "", "correlation": "XX", "scan": "", "antenna": "",
                   "timerange": "", "uvrange": "",
                   "panels": {"A": {"kind": "raster", "y": "TIME", "x": "CHANNEL",
                                    "qty": "PHASE_DIFF", "averaging": "vector",
                                    "detrend": True, "twin": "auto", "cwin": "off"}}}
            resp = asyncio.run(vp._handle_plot(msg))
            assert "Phase Diff (vs scan mean)" in resp["panels"]["A"]["title"]
        finally:
            vp.close()
