"""
test_freq_gaps.py
=================
Issue #36 part 3: frequency gaps between spectral windows.

A raster's Frequency axis (Channel drawn in Hz when the windows differ)
and a scatter's X = Frequency can have the gaps between spectral windows
closed, as a Time axis can (``raster_grid.GapAxis``): every gap removed,
the ticks reading real frequencies, and the cursor readout, flag boxes and
zoomed (finer) queries converting back to Hz.  A scatter's canvas along
X = Frequency is capped at one column per channel (``scatter_freq_step``),
as it is per integration along X = Time.

Location in repository:
    cubevis/tests/manual/visplot/test_freq_gaps.py

Run:
    pytest cubevis/tests/manual/visplot/test_freq_gaps.py -v

Data: a simulated MSv2 (and its MSv4 zarr twin) with two spectral windows
of 16 channels, 1.00-1.15 GHz and 2.00-2.15 GHz.  What is NOT covered: a
real MS with many windows (yours), and anything only a browser shows.
"""
from __future__ import annotations

import asyncio
import warnings

import numpy as np
import pytest

from cubevis_test_paths import ensure_cubevis_importable
ensure_cubevis_importable()

xr = pytest.importorskip("xarray")

from cubevis.toolbox.gridplot.raster_grid import GapAxis                  # noqa: E402
from cubevis.toolbox.visplot.axes import Axis                             # noqa: E402
from cubevis.toolbox.visplot.selection import SelectionSpec               # noqa: E402

warnings.filterwarnings("ignore")

NCHAN, DF = 16, 10.0e6
F_A = 1.00e9 + DF * np.arange(NCHAN)
F_B = 2.00e9 + DF * np.arange(NCHAN)


def _transform(desc, data):
    data["FLAG"] = (data["FLAG"][0], np.zeros_like(data["FLAG"][1]))
    return data


@pytest.fixture(scope="module")
def sim_ms(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("fgaps") / "fgaps.ms")
    sim.MSStructureSimulator(
        ntime=8, time_chunks=8, nfield=1, nantenna=4, auto_corrs=False,
        data_description=[(F_A, ["XX", "YY"]), (F_B, ["XX", "YY"])],
        simulate_data=True, transform_data=_transform).simulate_ms(path)
    return path


@pytest.fixture(scope="module")
def sim_ps(sim_ms, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("fgapsps") / "fgaps.ps.zarr")
    xr.open_datatree(sim_ms, engine="xarray-ms:msv2").to_zarr(out, mode="w", compute=True)
    return out


@pytest.fixture(params=["msv2", "msv4"])
def backend(request, sim_ms, sim_ps):
    if request.param == "msv2":
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
        b = MSv2Backend(sim_ms)
    else:
        from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
        b = MSv4Backend(sim_ps)
    b.open()
    yield b
    b.close()


ALL = SelectionSpec()


def _raster(backend, **kw):
    from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster
    args = dict(backend=backend, selection=ALL, polarization="XX",
                y_dim=Axis.TIME, x_dim=Axis.FREQUENCY, quantity=Axis.AMPLITUDE,
                width=200, height=400, headless=True, scaling="linear")
    args.update(kw)
    return VisibilityRaster(**args)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

class TestReader:

    def test_two_windows_two_runs(self, backend):
        runs = backend.axis_runs(ALL, "frequency")
        assert len(runs) == 2
        (a0, a1), (b0, b1) = runs
        assert a0 < F_A[0] < F_A[-1] < a1 < b0 < F_B[0] < F_B[-1] < b1

    def test_scatter_freq_step(self, backend):
        step = backend.scatter_freq_step(ALL)
        assert step is not None and 0.5 * DF < step <= DF
        assert backend.scatter_freq_step(SelectionSpec(avg_chan=4)) == pytest.approx(4 * step)
        assert backend.scatter_freq_step(SelectionSpec(avg_chan="all")) is None
        assert backend.scatter_x_step(ALL, Axis.FREQUENCY) == pytest.approx(step)
        assert backend.scatter_x_step(ALL, Axis.TIME) == backend.scatter_time_step(ALL)
        assert backend.scatter_x_step(ALL, Axis.UVDIST) is None

    def test_scatter_chan_step(self, backend):
        assert backend.scatter_x_step(ALL, Axis.CHANNEL) == 1.0
        assert backend.scatter_chan_step(SelectionSpec(avg_chan=4)) == 4.0
        assert backend.scatter_chan_step(SelectionSpec(avg_chan="all")) is None


# ---------------------------------------------------------------------------
# Raster
# ---------------------------------------------------------------------------

class TestRaster:

    def test_shown_by_default(self, backend):
        vr = _raster(backend)
        assert vr.freq_gaps == "shown" and vr._freq_axis is None
        f = np.asarray(vr.agg.coords["frequency"].values)
        assert f.min() > 0.9e9

    def test_closed_is_the_readers_runs(self, backend):
        vr = _raster(backend)
        vr.update_axes(freq_gaps="closed")
        ax = vr._freq_axis
        assert [list(r) for r in zip(ax.lo, ax.hi)] == backend.axis_runs(ALL, "frequency")
        u = np.asarray(vr.agg.coords["frequency"].values, dtype=np.float64)
        assert u.size == 2 * NCHAN
        assert u.min() >= 0 and u.max() <= ax.extent[1]
        # the windows meet: no blank between them
        assert ax.ends[0] == pytest.approx(ax.starts[1])
        # the range is the closed axis
        assert vr._x_range[1] - vr._x_range[0] < 2 * NCHAN * DF * 1.01
        # and the time axis is untouched
        assert vr._time_axis is None

    def test_spec_label_runs_and_unit(self, backend):
        vr = _raster(backend)
        vr.update_axes(freq_gaps="closed")
        spec = vr._panel_spec()
        assert spec.x_runs == vr._freq_axis.state() and spec.y_runs is None
        assert "(gaps closed)" in spec.x_label
        # the prefix comes from the real frequencies, not the closed range
        assert spec.axis_scale("x")[1] == "GHz"

    def test_ticks_read_real_frequencies(self, backend):
        from cubevis.toolbox.gridplot.tick_format import format_tick
        vr = _raster(backend)
        vr.update_axes(freq_gaps="closed")
        ax = vr._freq_axis
        spec = vr._panel_spec()
        scale = spec.axis_scale("x")[0]
        s = format_tick(ax.starts[1], False, 0.0, scale, runs=ax.state())
        assert float(s) == pytest.approx(ax.lo[1] / scale, abs=1e-4)

    def test_both_axes_closed(self, backend):
        vr = _raster(backend)
        vr.update_axes(freq_gaps="closed", time_gaps="closed")
        spec = vr._panel_spec()
        assert spec.x_runs is not None and spec.y_runs is not None

    def test_probe_names_real_frequencies(self, backend):
        vr = _raster(backend)
        vr.update_axes(freq_gaps="closed")
        ax = vr._freq_axis
        agg = vr.agg
        gx = int(np.argmin(np.abs(agg.coords["frequency"].values - (ax.starts[1] + DF / 2))))
        info = vr._probe_raster_pixel_local(gx, 0)
        assert F_B[0] - DF <= info["x_centre"] <= F_B[-1] + DF
        assert info["x_range"][0] > 1.5e9

    def test_detail_selection_is_in_hz(self, backend):
        vr = _raster(backend)
        vr.update_axes(freq_gaps="closed")
        ax = vr._freq_axis
        sel = vr._detail_selection((ax.starts[1], ax.starts[1] + 4 * DF), vr._y_range)
        assert sel is not None
        lo, hi = sel.freq_range
        assert F_B[0] - DF <= lo < hi <= F_B[0] + 5 * DF
        e = ax.extent[1]
        assert vr._detail_selection((e + DF, e + 2 * DF), vr._y_range) is None

    def test_a_channel_index_has_no_gaps(self, backend):
        """An aggregate relabelled as a channel index (one window, or
        windows that share channels: data.reader.to_channel_index) is
        left alone, and the panel then has no closed frequency axis."""
        from cubevis.toolbox.visplot.data.reader import CHANNEL_AXIS_ATTR
        vr = _raster(backend)
        vr.update_axes(freq_gaps="closed")
        agg = vr.agg.assign_coords(frequency=np.arange(vr.agg.sizes["frequency"]))
        agg.attrs = {CHANNEL_AXIS_ATTR: "x"}
        out = vr._place_freqs(agg, primary=True)
        assert out is agg and vr._freq_axis is None

    def test_channel_across_windows_is_in_hz_and_closes(self, backend):
        from cubevis.toolbox.visplot.data.reader import CHANNEL_AXIS_ATTR
        vr = _raster(backend, x_dim=Axis.CHANNEL)
        vr.update_axes(freq_gaps="closed")
        assert CHANNEL_AXIS_ATTR not in vr.agg.attrs
        assert vr._freq_axis is not None and len(vr._freq_axis) == 2

# ---------------------------------------------------------------------------
# Scatter (through a plotter: flags, probes)
# ---------------------------------------------------------------------------

@pytest.fixture(params=["msv2", "msv4"])
def plotter(request, sim_ms, sim_ps):
    from cubevis.toolbox.visplot import VisibilityPlotter
    kw = dict(ms=sim_ms) if request.param == "msv2" else dict(ps=sim_ps)
    vp = VisibilityPlotter(layout="side", enable_flagging=True, **kw)
    yield vp
    vp.close()


def _scatter(vp, closed=True):
    s = next(s.scatter for s in vp._slots if s.kind == "scatter")
    s.update_axes(x_dim=Axis.FREQUENCY, freq_gaps="closed" if closed else "shown")
    return s


class TestScatter:

    def test_off_by_default_and_only_for_frequency(self, plotter):
        s = next(s.scatter for s in plotter._slots if s.kind == "scatter")
        assert s.freq_gaps == "shown" and s._x_gap_axis is None
        s.update_axes(x_dim=Axis.FREQUENCY)
        assert s._x_gap_axis is None
        s.update_axes(freq_gaps="closed")
        assert s._x_gap_axis is not None and s._time_axis is None
        s.update_axes(x_dim=Axis.TIME)          # time gaps still shown
        assert s._x_gap_axis is None
        s.update_axes(x_dim=Axis.UVDIST)
        assert s._x_gap_axis is None

    def test_closed_axis_and_range(self, plotter):
        s = _scatter(plotter, closed=False)
        real = s._x_range
        s.update_axes(freq_gaps="closed")
        ax = s._x_gap_axis
        assert len(ax) == 2
        assert real[1] - real[0] > 0.9e9                     # the 0.85 GHz gap shown
        assert s._x_range[1] - s._x_range[0] < 0.5e9         # closed
        assert s._x_range[0] >= ax.extent[0] - 1 and s._x_range[1] <= ax.extent[1] + 1

    def test_spec(self, plotter):
        s = _scatter(plotter)
        spec = s._panel_spec()
        assert spec.x_runs == s._x_gap_axis.state()
        assert "(gaps closed)" in spec.x_label
        assert spec.axis_scale("x")[1] == "GHz"

    def test_same_axis_as_a_raster(self, plotter, sim_ms):
        s = _scatter(plotter)
        r = _raster(s._backend)
        r.update_axes(freq_gaps="closed")
        assert r._freq_axis.state() == s._x_gap_axis.state()

    def test_flag_box_becomes_real_frequencies(self, plotter):
        s = _scatter(plotter)
        ax = s._x_gap_axis
        y0, y1 = s._y_range
        box = {"x0": ax.starts[1] + DF, "x1": ax.starts[1] + 3 * DF, "y0": y0, "y1": y1}
        req = plotter._flags.build_request(s, "scatter", box, True)
        assert (req["x0"], req["x1"]) == pytest.approx(ax.real_span(box["x0"], box["x1"]))
        e = ax.extent[1]
        with pytest.raises(ValueError, match="frequency axis"):
            plotter._flags.build_request(
                s, "scatter", {"x0": e + DF, "x1": e + 2 * DF, "y0": y0, "y1": y1}, True)

    def test_raster_flag_box_becomes_real_frequencies(self, plotter):
        r = next(sl.raster for sl in plotter._slots if sl.kind == "raster")
        r.update_axes(y_dim=Axis.TIME, x_dim=Axis.FREQUENCY, freq_gaps="closed")
        ax = r._freq_axis
        y0, y1 = r._y_range
        box = {"x0": ax.starts[1] + DF, "x1": ax.starts[1] + 3 * DF, "y0": y0, "y1": y1}
        req = plotter._flags.build_request(r, "raster", box, True)
        assert (req["x0"], req["x1"]) == pytest.approx(ax.real_span(box["x0"], box["x1"]))
        e = ax.extent[1]
        with pytest.raises(ValueError, match="frequency axis"):
            plotter._flags.build_request(
                r, "raster", {"x0": e + DF, "x1": e + 2 * DF, "y0": y0, "y1": y1}, True)

    def test_exact_identity_outside_the_data_says_so(self, plotter):
        s = _scatter(plotter)
        e = s._x_gap_axis.extent[1]
        y0, y1 = s._y_range
        resp = asyncio.run(s._handle_probe_region(
            {"tool": "info_box", "x0": e + DF, "x1": e + 2 * DF, "y0": y0, "y1": y1}))
        assert resp["status"] == "no_data"
        assert "frequency axis" in resp["info_html"]

    def test_zoom_into_the_second_window(self, plotter):
        s = _scatter(plotter)
        ax = s._x_gap_axis
        y0, y1 = s._y_range
        s._rerender(x_range=(ax.starts[1], ax.ends[1]), y_range=(y0, y1))
        assert any(n > 0 for n in s._layer_n_in_view)

    def test_canvas_capped_at_one_column_per_channel(self):
        from cubevis.toolbox.visplot.data._scatter_render import compute_canvas_size
        import pandas as pd
        from cubevis.toolbox.visplot.data.reader import ScatterLayerSpec
        x = np.repeat(np.arange(2 * NCHAN) * DF, 200)
        df = pd.DataFrame({"x": x, "y": np.tile(np.linspace(0.0, 1.0, 200), 2 * NCHAN)})
        layers = [ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                                   cmap=("#000", "#fff"))]
        w, _h = compute_canvas_size({(Axis.AMPLITUDE, "XX"): df}, layers,
                                    0.0, 2 * NCHAN * DF, 0.0, 1.0, 900, 400,
                                    x_step=DF)
        assert w == 2 * NCHAN


# ---------------------------------------------------------------------------
# Plotter wiring
# ---------------------------------------------------------------------------

class TestPlotter:

    def test_controls_and_message(self, plotter):
        import inspect
        from cubevis.toolbox.visplot import visibility_plotter as vpm
        src = inspect.getsource(vpm)
        assert "freq_gaps: fg_sel ? fg_sel.value : null" in src
        assert "freq_gaps: sfg_sel ? sfg_sel.value : null" in src
        assert "fgaps != panel.freq_gaps" in src
        assert "s_fg != panel.freq_gaps" in src
        w = plotter._panel_axis_widgets
        for sl in plotter._slots:
            assert w[sl.id]["raster"]["fgaps_sel"].value == "shown"
            assert w[sl.id]["scatter"]["fgaps_sel"].value == "shown"

    def test_tooltips(self):
        from cubevis.toolbox.visplot import visibility_plotter as vpm
        tips = next(v for k, v in vars(vpm).items()
                    if isinstance(v, dict) and "tgaps" in v and "s_tgaps" in v)
        assert "fgaps" in tips and "s_fgaps" in tips


# ---------------------------------------------------------------------------
# The Level-1 reference keeps to the same columns (2026-11-22)
# ---------------------------------------------------------------------------

class TestReferenceStep:
    """X = Channel, 384 channels: a reference at twice the capped canvas
    had an empty column between channels, and a Level-1 redraw at a
    slightly different viewport (the range settling after a step) came
    out denser in one half than the other, with a seam between."""

    def _halves(self, x_step):
        import pandas as pd
        from cubevis.toolbox.visplot.data import _scatter_render as sr
        from cubevis.toolbox.visplot.data.reader import ScatterLayerSpec
        rng = np.random.default_rng(0)
        n, per = 384, 300
        df = pd.DataFrame({"x": np.repeat(np.arange(n, dtype=float), per),
                           "y": rng.gamma(3.0, 3.0, n * per)})
        lyr = ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                               cmap=("#000000", "#ffffff"))
        x0, x1, y0, y1 = -0.5, 383.5, 0.0, 45.0
        cw, ch = sr.compute_canvas_size({(Axis.AMPLITUDE, "XX"): df}, [lyr],
                                        x0, x1, y0, y1, 900, 400, x_step=1.0)
        rw, rh = sr.reference_size(cw, ch, 2.0, x0, x1, x_step)
        ref = sr.build_layer_reference(df, lyr, x0, x1, y0, y1, rw, rh, cw, ch, "global")
        img, _ = sr.resample_layer_reference(ref, lyr, None, 0.0, 383.0, y0, y1, cw, ch)
        occ = (np.asarray(img) != 0).sum(axis=0)
        return rw, occ[: cw // 2].mean(), occ[cw // 2:].mean()

    def test_reference_capped(self):
        from cubevis.toolbox.visplot.data import _scatter_render as sr
        assert sr.reference_size(384, 400, 2.0, -0.5, 383.5, 1.0) == (384, 800)
        assert sr.reference_size(384, 400, 2.0, -0.5, 383.5, None) == (768, 800)
        assert sr.reference_size(100, 400, 2.0, 0.0, 1000.0, 1.0) == (200, 800)

    def test_no_seam(self):
        rw, left, right = self._halves(1.0)
        assert rw == 384
        assert abs(left - right) < 0.05 * left

    def test_the_seam_without_the_cap(self):
        _rw, left, right = self._halves(None)
        assert abs(left - right) > 0.1 * left

    def _ref(self, x_step):
        import pandas as pd
        from cubevis.toolbox.visplot.data import _scatter_render as sr
        from cubevis.toolbox.visplot.data.reader import ScatterLayerSpec
        rng = np.random.default_rng(1)
        df = pd.DataFrame({"x": np.repeat(np.arange(384, dtype=float), 50),
                           "y": rng.gamma(3.0, 3.0, 384 * 50)})
        lyr = ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                               cmap=("#000000", "#ffffff"))
        x0, x1, y0, y1 = -0.5, 383.5, 0.0, 45.0
        rw, rh = sr.reference_size(384, 400, 2.0, x0, x1, x_step)
        return sr.build_layer_reference(df, lyr, x0, x1, y0, y1, rw, rh, 384, 400,
                                        "global", x_step=x_step)

    def test_reference_knows_its_step(self):
        assert self._ref(1.0).ref_x_step == 1.0
        assert self._ref(None).ref_x_step is None

    def test_x_zoom_stays_local(self):
        from cubevis.toolbox.visplot.data import _scatter_render as sr
        ref = self._ref(1.0)
        # a 4x zoom in X: nothing finer to fetch
        assert not sr.needs_level2_requery(ref, 100.0, 196.0, 0.0, 45.0, 384, 400)
        # a 2x zoom in Y is within the reference's 2x; 4x needs Level-2
        assert not sr.needs_level2_requery(ref, 100.0, 196.0, 0.0, 22.5, 384, 400)
        assert sr.needs_level2_requery(ref, 100.0, 196.0, 0.0, 11.0, 384, 400)
        # outside the reference: Level-2 as before
        assert sr.needs_level2_requery(ref, 300.0, 500.0, 0.0, 45.0, 384, 400)

    def test_without_a_step_x_zoom_still_requeries(self):
        from cubevis.toolbox.visplot.data import _scatter_render as sr
        ref = self._ref(None)            # 768 columns, 2x
        assert not sr.needs_level2_requery(ref, 100.0, 292.0, 0.0, 45.0, 384, 400)
        assert sr.needs_level2_requery(ref, 100.0, 196.0, 0.0, 45.0, 384, 400)

