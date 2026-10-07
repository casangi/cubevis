"""
test_baseline_combine.py
========================
How a raster cell combines several baselines (2026-10-07, HRS H4): the
all-baseline Time x Channel waterfall, the AIPS FTFLG view.

Location in repository:
    cubevis/tests/manual/visplot/test_baseline_combine.py

Run:
    pytest cubevis/tests/manual/visplot/test_baseline_combine.py -v

Sections
--------
1. The option                 names, default, SelectionSpec
2. The reductions             against numpy, numpy- and dask-backed
3. Real backends              equals per-baseline waterfalls combined
4. Real raster                per panel, title, re-query
5. Real plotter               control, help, Plot request, preset, flags
6. Generated task layers      the argument reached them

Sections 1-2 need no data.  3-5 build a small simulated MSv2 with
xarray-ms's simulator (and its MSv4 zarr twin).

What is NOT covered: anything that only happens in a browser.
"""
from __future__ import annotations

import asyncio
import pathlib
import re
import warnings

import numpy as np
import pytest

from cubevis_test_paths import ensure_cubevis_importable
ensure_cubevis_importable()

xr = pytest.importorskip("xarray")

from cubevis.toolbox.visplot.axes import Axis                         # noqa: E402
from cubevis.toolbox.visplot import selection as sel_mod              # noqa: E402
from cubevis.toolbox.visplot.selection import SelectionSpec           # noqa: E402
from cubevis.toolbox.visplot.data import _raster_average as ra        # noqa: E402

warnings.filterwarnings("ignore")


# ---------------------------------------------------------------------------
# 1. The option
# ---------------------------------------------------------------------------

class TestOption:

    def test_names_and_default(self):
        assert sel_mod.BASELINE_COMBINES == ("mean", "max", "coherent")
        assert sel_mod.DEFAULT_BASELINE_COMBINE == "mean"
        assert SelectionSpec().baseline_combine == "mean"

    def test_normalize(self):
        n = sel_mod.normalize_baseline_combine
        assert n(None) == "mean" and n("") == "mean"
        assert n(" Max ") == "max" and n("Maximum") == "max"
        assert n("coherent") == "coherent" and n("vector") == "coherent"
        with pytest.raises(ValueError, match="baseline_combine"):
            n("median")

    def test_copy_keeps_it_and_it_is_not_a_row_constraint(self):
        s = SelectionSpec(baseline_combine="max")
        assert s.copy().baseline_combine == "max"
        assert s.is_empty()

    def test_plotter_default_is_the_package_default(self):
        # The constructor default has to be a literal (sync_layers copies
        # it into the generated task layers).
        import inspect
        from cubevis.toolbox.visplot import VisibilityPlotter
        p = inspect.signature(VisibilityPlotter.__init__).parameters
        assert p["baseline_combine"].default == sel_mod.DEFAULT_BASELINE_COMBINE


# ---------------------------------------------------------------------------
# 2. The reductions
# ---------------------------------------------------------------------------

def _cube(seed=3, nt=5, nb=4, nf=6, dask=False, flag_frac=0.15):
    rng = np.random.default_rng(seed)
    v = (rng.normal(size=(nt, nb, nf)) + 1j * rng.normal(size=(nt, nb, nf))) \
        * rng.uniform(0.5, 4.0, (1, nb, 1))
    f = rng.random((nt, nb, nf)) < flag_frac
    dims = ("time", "baseline_id", "frequency")
    vis = xr.DataArray(v, dims=dims)
    flag = xr.DataArray(f, dims=dims)
    if dask:
        vis, flag = vis.chunk({"time": 2, "baseline_id": 3}), flag.chunk(
            {"time": 2, "baseline_id": 3})
    return vis, flag, np.where(f, np.nan, v)


def _nanmean(a, axis):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return np.nanmean(a, axis=axis)


def _nanmax(a, axis):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return np.nanmax(a, axis=axis)


@pytest.mark.parametrize("dask", [False, True])
class TestReductions:

    def test_amplitude_over_baselines_only(self, dask):
        vis, flag, v = _cube(dask=dask)
        amp = np.abs(v)                                 # (t, b, f)
        for avg in ("vector", "scalar"):
            got = ra.reduce_amp_phase_baselines(
                vis, flag, Axis.AMPLITUDE, ["baseline_id"], avg, "mean").values
            assert np.allclose(got, _nanmean(amp, 1), equal_nan=True)
            got = ra.reduce_amp_phase_baselines(
                vis, flag, Axis.AMPLITUDE, ["baseline_id"], avg, "max").values
            assert np.allclose(got, _nanmax(amp, 1), equal_nan=True)
        got = ra.reduce_amp_phase_baselines(
            vis, flag, Axis.AMPLITUDE, ["baseline_id"], "vector", "coherent").values
        assert np.allclose(got, np.abs(_nanmean(v, 1)), equal_nan=True)

    def test_amplitude_averaging_applies_within_each_baseline(self, dask):
        vis, flag, v = _cube(dask=dask)
        # Time x (nothing): reduce over frequency inside each baseline,
        # then combine the baselines.
        per_vec = np.abs(_nanmean(v, 2))                # (t, b)
        per_sca = _nanmean(np.abs(v), 2)
        rd = ["baseline_id", "frequency"]
        for avg, per in (("vector", per_vec), ("scalar", per_sca)):
            for comb, fn in (("mean", _nanmean), ("max", _nanmax)):
                got = ra.reduce_amp_phase_baselines(
                    vis, flag, Axis.AMPLITUDE, rd, avg, comb).values
                assert np.allclose(got, fn(per, 1), equal_nan=True), (avg, comb)
        assert not np.allclose(_nanmean(per_vec, 1), _nanmean(per_sca, 1))

    def test_coherent_is_the_old_reduction(self, dask):
        vis, flag, _v = _cube(dask=dask)
        rd = ["baseline_id", "frequency"]
        for q in (Axis.AMPLITUDE, Axis.PHASE):
            for avg in ("vector", "scalar"):
                a = ra.reduce_amp_phase_baselines(vis, flag, q, rd, avg, "coherent").values
                b = ra.reduce_amp_phase(vis, flag, q, rd, avg).values
                assert np.array_equal(a, b, equal_nan=True)

    def test_without_baselines_to_combine_nothing_changes(self, dask):
        vis, flag, _v = _cube(dask=dask)
        for comb in ("mean", "max", "coherent"):
            a = ra.reduce_amp_phase_baselines(
                vis, flag, Axis.AMPLITUDE, ["frequency"], "vector", comb).values
            b = ra.reduce_amp_phase(vis, flag, Axis.AMPLITUDE, ["frequency"], "vector").values
            assert np.array_equal(a, b, equal_nan=True)

    def test_phase_is_the_mean_direction_of_the_baselines(self, dask):
        vis, flag, v = _cube(dask=dask)
        unit = v / np.abs(v)
        want = np.degrees(np.angle(_nanmean(unit, 1)))
        for comb in ("mean", "max"):                    # no maximum of a direction
            got = ra.reduce_amp_phase_baselines(
                vis, flag, Axis.PHASE, ["baseline_id"], "vector", comb).values
            assert np.allclose(got, want, equal_nan=True)
        # with something to average inside each baseline first
        per = _nanmean(v, 2)
        want = np.degrees(np.angle(_nanmean(per / np.abs(per), 1)))
        got = ra.reduce_amp_phase_baselines(
            vis, flag, Axis.PHASE, ["baseline_id", "frequency"], "vector", "mean").values
        assert np.allclose(got, want, equal_nan=True)

    def test_phase_across_the_wrap(self, dask):
        ph = np.deg2rad([[179.0, -179.0, 178.0, -178.0]])           # (t=1, b=4)
        v = (np.array([1.0, 50.0, 1.0, 50.0]) * np.exp(1j * ph))[:, :, None]
        vis = xr.DataArray(v, dims=("time", "baseline_id", "frequency"))
        flag = xr.zeros_like(vis, dtype=bool)
        if dask:
            vis, flag = vis.chunk({"baseline_id": 2}), flag.chunk({"baseline_id": 2})
        got = ra.reduce_amp_phase_baselines(
            vis, flag, Axis.PHASE, ["baseline_id"], "vector", "mean").values
        assert abs(abs(got[0, 0]) - 180.0) < 1e-9       # not 0, and not amplitude-weighted

    def test_a_fully_flagged_cell_is_blank(self, dask):
        vis, flag, _v = _cube(dask=dask)
        flag = flag.copy()
        flag.loc[dict(time=1, frequency=2)] = True
        for comb in ("mean", "max"):
            got = ra.reduce_amp_phase_baselines(
                vis, flag, Axis.AMPLITUDE, ["baseline_id"], "vector", comb).values
            assert np.isnan(got[1, 2]) and np.isfinite(got[0, 0])

    def test_plain_quantities(self, dask):
        vis, flag, v = _cube(dask=dask)
        q = np.abs(vis).where(~flag)                    # stands in for a DIFF magnitude
        rd = ["baseline_id", "frequency"]
        per = _nanmean(np.abs(v), 2)
        got = ra.reduce_plain_baselines(q, Axis.AMP_VDIFF, rd, "max").values
        assert np.allclose(got, _nanmax(per, 1), equal_nan=True)
        for comb in ("mean", "coherent"):
            got = ra.reduce_plain_baselines(q, Axis.AMP_VDIFF, rd, comb).values
            assert np.allclose(got, _nanmean(np.abs(v), (1, 2)), equal_nan=True)
        # a signed quantity has no meaningful maximum: always the mean
        r = vis.real.where(~flag)
        got = ra.reduce_plain_baselines(r, Axis.REAL, rd, "max").values
        assert np.allclose(got, _nanmean(v.real, (1, 2)), equal_nan=True)

    def test_bad_arguments(self, dask):
        vis, flag, _v = _cube(dask=dask)
        with pytest.raises(ValueError, match="baseline_combine"):
            ra.reduce_amp_phase_baselines(vis, flag, Axis.AMPLITUDE,
                                          ["baseline_id"], "vector", "median")
        with pytest.raises(ValueError, match="averaging"):
            ra.reduce_amp_phase_baselines(vis, flag, Axis.AMPLITUDE,
                                          ["baseline_id"], "rms", "mean")


def test_combines_baselines():
    c = ra.combines_baselines
    assert c(Axis.AMPLITUDE, ["baseline_id"], "mean")
    assert c(Axis.PHASE, ["baseline_id", "time"], "coherent")
    assert c(Axis.AMP_VDIFF, ["baseline_id"], "max")
    assert not c(Axis.AMPLITUDE, ["frequency"], "max")
    assert not c(Axis.FLAG, ["baseline_id"], "max")
    assert not c(Axis.PHASE_RMS, ["baseline_id"], "max")
    assert not c(Axis.Z_SCORE, ["baseline_id"], "max")


# ---------------------------------------------------------------------------
# Simulated data: every baseline with its own amplitude and phase
# ---------------------------------------------------------------------------

NTIME, NANT, NCHAN = 12, 5, 8
NBL = NANT * (NANT - 1) // 2


def _transform(desc, data):
    rng = np.random.default_rng(77 + int(desc.chunk_id))
    dims, vis = data["DATA"]
    a1 = np.asarray(data["ANTENNA1"][1]).astype(float)
    a2 = np.asarray(data["ANTENNA2"][1]).astype(float)
    bl = (a1 * 7 + a2 * 3).reshape((-1,) + (1,) * (vis.ndim - 1))
    amp = (1.0 + 0.5 * bl) * (1.0 + 0.2 * rng.standard_normal(vis.shape))
    ph = 0.9 * bl + 0.3 * rng.standard_normal(vis.shape)     # differs per baseline
    data["DATA"] = (dims, (amp * np.exp(1j * ph)).astype(np.complex64))
    fdims, _ = data["FLAG"]
    data["FLAG"] = (fdims, rng.random(vis.shape) < 0.05)
    return data


@pytest.fixture(scope="module")
def sim_ms(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("bcomb") / "c.ms")
    sim.MSStructureSimulator(
        ntime=NTIME, time_chunks=NTIME, nantenna=NANT, auto_corrs=False,
        data_description=[(NCHAN, ["XX", "YY"])],
        simulate_data=True, transform_data=_transform).simulate_ms(path)
    return path


@pytest.fixture(scope="module")
def sim_ps(sim_ms, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("bcombps") / "c.ps.zarr")
    dt = xr.open_datatree(sim_ms, engine="xarray-ms:msv2",
                          partition_schema=["FIELD_ID"])
    dt.to_zarr(out, mode="w", compute=True)
    return out


def _open(kind, sim_ms, sim_ps):
    if kind == "msv2":
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
        b = MSv2Backend(sim_ms)
    else:
        from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
        b = MSv4Backend(sim_ps)
    b.open()
    return b


@pytest.fixture(params=["msv2", "msv4"])
def backend(request, sim_ms, sim_ps):
    b = _open(request.param, sim_ms, sim_ps)
    yield b
    b.close()


def _q(backend, quantity=Axis.AMPLITUDE, y=Axis.TIME, x=Axis.CHANNEL, **sel):
    agg, *_ = backend.query_raster(y, x, quantity, SelectionSpec(**sel),
                                   polarization="XX")
    return agg


# ---------------------------------------------------------------------------
# 3. Real backends
# ---------------------------------------------------------------------------

class TestRealBackends:

    def _per_baseline(self, backend, quantity=Axis.AMPLITUDE):
        """One Time x Channel waterfall per baseline, each queried alone."""
        out = []
        for _bid, a1, a2 in backend.metadata()["baselines"]:
            out.append(_q(backend, quantity, baselines=[(a1, a2)]).values)
        assert len(out) == NBL
        return np.stack(out)                               # (b, t, f)

    def test_mean_is_the_mean_of_the_per_baseline_waterfalls(self, backend):
        per = self._per_baseline(backend)
        got = _q(backend, baseline_combine="mean").values
        assert got.shape == (NTIME, NCHAN)
        assert np.allclose(got, _nanmean(per, 0), equal_nan=True, rtol=1e-5)
        # and it is the default
        assert np.array_equal(got, _q(backend).values, equal_nan=True)

    def test_max_is_the_largest_per_baseline_waterfall(self, backend):
        per = self._per_baseline(backend)
        got = _q(backend, baseline_combine="max").values
        assert np.allclose(got, _nanmax(per, 0), equal_nan=True, rtol=1e-5)
        assert (got >= _q(backend, baseline_combine="mean").values - 1e-6).all()

    def test_coherent_cancels_where_baselines_disagree(self, backend):
        coh = _q(backend, baseline_combine="coherent").values
        mean = _q(backend, baseline_combine="mean").values
        # every baseline has its own phase in this data set
        assert np.nanmean(coh) < 0.5 * np.nanmean(mean)
        # with scalar averaging "coherent" adds amplitudes, i.e. the mean
        sca = _q(backend, baseline_combine="coherent", averaging="scalar").values
        assert np.allclose(sca, mean, equal_nan=True, rtol=1e-5)

    def test_phase_mean_direction(self, backend):
        per = np.deg2rad(self._per_baseline(backend, Axis.PHASE))
        want = np.degrees(np.angle(_nanmean(np.exp(1j * per), 0)))
        got = _q(backend, Axis.PHASE, baseline_combine="mean").values
        d = np.abs(((got - want) + 180.0) % 360.0 - 180.0)
        assert np.nanmax(d) < 1e-3

    def test_one_baseline_selected_is_that_baseline_in_every_mode(self, backend):
        b = backend.metadata()["baselines"][3]
        ref = _q(backend, baselines=[(b[1], b[2])], baseline_combine="mean").values
        for comb in ("max", "coherent"):
            got = _q(backend, baselines=[(b[1], b[2])], baseline_combine=comb).values
            assert np.allclose(got, ref, equal_nan=True, rtol=1e-6)

    def test_ignored_where_baseline_is_an_axis(self, backend):
        ref = _q(backend, y=Axis.TIME, x=Axis.BASELINE).values
        for comb in ("max", "coherent"):
            got = _q(backend, y=Axis.TIME, x=Axis.BASELINE, baseline_combine=comb).values
            assert np.array_equal(got, ref, equal_nan=True)

    def test_quantities_that_do_not_depend_on_it(self, backend):
        for q in (Axis.FLAG, Axis.Z_SCORE, Axis.REAL):
            ref = _q(backend, q, baseline_combine="mean").values
            got = _q(backend, q, baseline_combine="max").values
            assert np.allclose(got, ref, equal_nan=True), q.name

    def test_backends_agree(self, sim_ms, sim_ps):
        a, b = _open("msv2", sim_ms, sim_ps), _open("msv4", sim_ms, sim_ps)
        try:
            for q in (Axis.AMPLITUDE, Axis.PHASE, Axis.AMP_VDIFF):
                for comb in ("mean", "max", "coherent"):
                    x = _q(a, q, baseline_combine=comb).values
                    y = _q(b, q, baseline_combine=comb).values
                    assert np.allclose(x, y, equal_nan=True, rtol=1e-5, atol=1e-5), (q, comb)
        finally:
            a.close(); b.close()


# ---------------------------------------------------------------------------
# 4. Real raster
# ---------------------------------------------------------------------------

def _raster(backend, **kw):
    from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster
    args = dict(backend=backend, selection=SelectionSpec(), polarization="XX",
                y_dim=Axis.TIME, x_dim=Axis.CHANNEL, quantity=Axis.AMPLITUDE,
                width=200, height=160, headless=True)
    args.update(kw)
    return VisibilityRaster(**args)


class TestRealRaster:

    def test_two_panels_combine_the_same_data_differently(self, backend):
        sel = SelectionSpec()
        a = _raster(backend, selection=sel, baseline_combine="mean")
        b = _raster(backend, selection=sel, baseline_combine="max")
        assert a.baseline_combine == "mean" and b.baseline_combine == "max"
        assert np.nanmean(b.agg.values) > np.nanmean(a.agg.values)
        assert sel.baseline_combine == "mean"            # the shared selection is untouched
        assert np.allclose(a.agg.values, _q(backend, baseline_combine="mean").values,
                           equal_nan=True)

    def test_default(self, backend):
        assert _raster(backend).baseline_combine == "mean"
        with pytest.raises(ValueError):
            _raster(backend, baseline_combine="median")

    def test_update_axes_requeries(self, backend):
        vr = _raster(backend)
        before = vr.agg.values.copy()
        vr.update_axes(baseline_combine="max")
        assert vr.baseline_combine == "max"
        assert not np.allclose(before, vr.agg.values, equal_nan=True)
        with pytest.raises(ValueError):
            vr.update_axes(baseline_combine="sum")

    def test_title_names_the_combination(self, backend):
        t = lambda **kw: _raster(backend, **kw)._effective_title().replace("\n", " ")
        assert t().startswith("Amplitude (mean of baselines)  [Time vs Channel]")
        assert t(baseline_combine="max").startswith("Amplitude (max of baselines)")
        assert t(baseline_combine="coherent").startswith(
            "Amplitude (vector, baselines added coherently)")
        assert t(baseline_combine="coherent", averaging="scalar").startswith(
            "Amplitude (scalar, baselines added coherently)")
        # Phase has no maximum: say what was actually done
        assert t(quantity=Axis.PHASE, baseline_combine="max").startswith(
            "Phase (mean of baselines)")
        # the same with the axes the other way round
        assert t(x_dim=Axis.TIME, y_dim=Axis.CHANNEL).startswith(
            "Amplitude (mean of baselines)  [Channel vs Time]")
        # Diff quantities: only Maximum is named
        assert "baselines" not in t(quantity=Axis.AMP_VDIFF)
        assert "max of baselines" in t(quantity=Axis.AMP_VDIFF, baseline_combine="max")
        # quantities that do not depend on it
        for q in (Axis.FLAG, Axis.Z_SCORE, Axis.REAL):
            assert "baselines" not in t(quantity=q, baseline_combine="max")

    def test_title_is_unchanged_where_nothing_is_combined(self, backend):
        vr = _raster(backend, x_dim=Axis.BASELINE, baseline_combine="max")
        assert vr._effective_title().startswith("Amplitude (vector)  [Time vs Baseline]")
        assert vr.n_baselines_combined is None
        b = backend.metadata()["baselines"][2]
        one = _raster(backend, selection=SelectionSpec(baselines=[(b[1], b[2])]),
                      baseline_combine="max")
        assert one.n_baselines_combined == 1
        assert one._effective_title().startswith("Amplitude (vector)  [Time vs Channel]")

    def test_number_of_baselines_combined(self, backend):
        assert _raster(backend).n_baselines_combined == NBL
        names = backend.metadata()["antenna_names"]
        vr = _raster(backend, selection=SelectionSpec(antenna_names=[names[0]]))
        assert vr.n_baselines_combined == NANT - 1


# ---------------------------------------------------------------------------
# 5. Real plotter
# ---------------------------------------------------------------------------

def _run(c):
    return asyncio.run(c)


@pytest.fixture(params=["msv2", "msv4"])
def plotter(request, sim_ms, sim_ps):
    from cubevis.toolbox.visplot import VisibilityPlotter
    kw = dict(ms=sim_ms) if request.param == "msv2" else dict(ps=sim_ps)
    vp = VisibilityPlotter(layout="side", correlation="XX,YY",
                           raster_y="TIME", raster_x="CHANNEL",
                           baseline_combine="max", **kw)
    yield vp
    vp.close()


def _plot_message(vp):
    W = vp._panel_axis_widgets
    return {"field": "", "correlation": "XX,YY", "datacolumn": "data",
            "reload": False,
            "spw_ids": [s.spw_id for s in vp._meta.spws],
            "antenna_names": [], "baselines": [], "both_ends_antennas": 0,
            "panels": {
                "A": {"kind": "raster",
                      "y": W["A"]["raster"]["y_sel"].value,
                      "x": W["A"]["raster"]["x_sel"].value,
                      "qty": W["A"]["raster"]["q_sel"].value,
                      "baseline_combine": W["A"]["raster"]["bcombine_sel"].value},
                "B": {"kind": "scatter",
                      "x": W["B"]["scatter"]["x_sel"].value,
                      "y": W["B"]["scatter"]["y_sel"].value,
                      "colorize": [None, None]}}}


class TestRealPlotter:

    def test_constructor_sets_every_raster_and_its_control(self, plotter):
        for slot in plotter._slots:
            assert slot.raster.baseline_combine == "max"
            sel = plotter._panel_axis_widgets[slot.id]["raster"]["bcombine_sel"]
            assert sel.title == "Baselines combined" and sel.value == "max"
            assert [o[0] for o in sel.options] == ["mean", "max", "coherent"]

    def test_bad_value_fails_at_construction(self, sim_ms):
        from cubevis.toolbox.visplot import VisibilityPlotter
        with pytest.raises(ValueError, match="baseline_combine"):
            VisibilityPlotter(ms=sim_ms, baseline_combine="median")

    def test_control_has_help_in_the_status_area(self, plotter):
        from cubevis.bokeh.models import EvHover
        hint = plotter._hint_bcombine
        text = hint.text
        assert text.startswith("<b>Baselines combined</b>")
        for word in ("<b>Mean</b>", "<b>Maximum</b>", "<b>Coherent</b>",
                     "every selected baseline"):
            assert word in text
        assert hint in plotter._hint_divs()
        # each raster gear tab's control sits in a hover wrapper that
        # shows this hint
        for slot in plotter._slots:
            sel = plotter._panel_axis_widgets[slot.id]["raster"]["bcombine_sel"]
            wraps = [m for m in plotter._sidebar_col.references()
                     if isinstance(m, EvHover) and m.child is sel]
            assert wraps, "control is not wrapped for help"
            from bokeh.events import MouseEnter, MouseLeave
            enter = wraps[0].js_event_callbacks.get(MouseEnter.event_name, [])
            leave = wraps[0].js_event_callbacks.get(MouseLeave.event_name, [])
            assert any(cb.args.get("hint") is hint and "hint.visible = true" in cb.code
                       for cb in enter)
            assert any(cb.args.get("hint") is hint and "hint.visible = false" in cb.code
                       for cb in leave)

    def test_control_is_in_the_plot_request(self, plotter):
        a = plotter._plot_js_args
        for n, slot in enumerate(plotter._slots):
            assert a[f"panel{n}_rm_sel"] is \
                plotter._panel_axis_widgets[slot.id]["raster"]["bcombine_sel"]
        code = plotter._do_plot_js
        assert "baseline_combine: rm_sel ? rm_sel.value : null" in code
        assert "panel0_rc_sel, panel0_rb_sel, panel0_rm_sel," in code
        assert "panel1_sd_sel, panel1_st_sel, panel1_sc_sel);" in code

    def test_plot_request_changes_it_and_requeries(self, plotter):
        vp = plotter
        r = vp._slots[0].raster
        cls = type(r)
        orig = cls.update_axes
        seen = []

        def spy(self, **kw):
            if self is r:
                seen.append(kw)
            return orig(self, **kw)
        cls.update_axes = spy
        try:
            msg = _plot_message(vp)
            msg["panels"]["A"]["baseline_combine"] = "mean"
            resp = _run(vp._handle_plot(msg))
            assert resp.get("status") != "error", resp
            assert seen and seen[-1]["baseline_combine"] == "mean"
            assert r.baseline_combine == "mean"
            assert "mean of baselines" in resp["panels"]["A"]["title"]
            seen.clear()
            _run(vp._handle_plot(msg))
            assert not seen                                    # unchanged: no re-query
            del msg["panels"]["A"]["baseline_combine"]         # an older client
            _run(vp._handle_plot(msg))
            assert r.baseline_combine == "mean" and not seen
            msg["panels"]["A"]["baseline_combine"] = "median"  # junk: kept, not fatal
            resp = _run(vp._handle_plot(msg))
            assert resp.get("status") != "error" and r.baseline_combine == "mean"
        finally:
            cls.update_axes = orig
            r.update_axes(baseline_combine="max")

    def test_preset_table_and_button(self, plotter):
        from cubevis.toolbox.visplot import visibility_plotter as vpm
        assert vpm._PRESETS["waterfall-all"] == (
            Axis.TIME, Axis.CHANNEL, Axis.AMPLITUDE,
            Axis.CHANNEL, Axis.AMPLITUDE, "over")
        names = [b[0] for b in vpm._PRESET_BUTTONS]
        assert names == list(vpm._PRESETS) and "waterfall-all" in names
        hint = getattr(plotter, "_hint_preset_waterfall-all")
        assert hint.text.startswith("<b>All-baseline waterfall</b>")
        assert "every selected baseline" in hint.text
        # the buttons' JS: All-BL sets Maximum, Waterfall sets Mean
        js = {n: o.code for n, o in zip(names, plotter._preset_js_objects)}
        assert "try { panel0_rm_sel.value = 'max'; } catch(e) {}" in js["waterfall-all"]
        assert "try { panel0_rm_sel.value = 'mean'; } catch(e) {}" in js["waterfall"]
        assert "panel0_rm_sel.value" not in js["vplot"]
        assert "panel0_rm_sel" in plotter._preset_js_objects[0].args

    def test_constructor_preset(self, sim_ms):
        from cubevis.toolbox.visplot import VisibilityPlotter
        vp = VisibilityPlotter(ms=sim_ms, correlation="XX", preset="waterfall-all",
                               baseline_combine="max")
        try:
            r = vp._slots[0].raster
            assert (r._y_dim, r._x_dim, r._quantity) == (
                Axis.TIME, Axis.CHANNEL, Axis.AMPLITUDE)
            assert r.baseline_combine == "max"
            assert "max of baselines" in r._effective_title()
        finally:
            vp.close()

    def test_box_flags_every_baseline_and_says_how_many(self, plotter):
        vp = plotter
        vp.flag_db.clear(record=False)
        r = vp._slots[0].raster
        assert Axis.BASELINE not in (r._y_dim, r._x_dim)
        t = r.agg.coords["time"].values
        b = vp._reader._backend

        def flagged():
            n = 0
            for part in b._iter_visibility_partitions(None):
                f = b._flag_mask(part).transpose(
                    "time", "baseline_id", "frequency", "polarization")
                ip = list(part.polarization.values).index("XX")
                n += int(f.values[:, :, :, ip].sum())
            return n
        before = flagged()
        # channels 2..4 (cells -0.5..0.5 wide), integrations 5..6
        box = dict(x0=1.7, x1=4.2, y0=float(t[5]) - 1, y1=float(t[6]) + 1, flag=True)
        resp = _run(vp._handle_box_select(box, "raster", r))
        try:
            assert resp["notify_text"].startswith("✓ Flagged:"), resp["notify_text"]
            assert f"on {NBL} baselines" in resp["notify_text"]
            # every selected baseline, in those cells: 2 x 3 x NBL samples
            # are flagged afterwards (some already were)
            now = 0
            for part in b._iter_visibility_partitions(None):
                f = b._flag_mask(part).transpose(
                    "time", "baseline_id", "frequency", "polarization").values
                ip = list(part.polarization.values).index("XX")
                tt = part.time.values
                rows = np.flatnonzero((tt >= t[5] - 1) & (tt <= t[6] + 1))
                assert f[np.ix_(rows, np.arange(f.shape[1]), [2, 3, 4], [ip])].all()
                now += int(f[:, :, :, ip].sum())
            added = now - before
            assert 0 < added <= 2 * 3 * NBL
            d = vp.flag_db.deltas()[-1]
            assert d.baseline_ids is None and d.antenna_names is None   # = all baselines
        finally:
            vp.flag_db.clear(record=False)

    def test_review_states_the_baselines_before_accepting(self, plotter):
        vp = plotter
        vp.flag_db.clear(record=False)
        r = vp._slots[0].raster
        t = r.agg.coords["time"].values
        _run(vp.flags.handle_action({"action": "config", "preview": True}))
        try:
            resp = _run(vp._handle_box_select(
                dict(x0=5.8, x1=6.2, y0=float(t[1]) - 1, y1=float(t[1]) + 1, flag=True),
                "raster", r))
            assert resp["notify_text"].startswith("Review the proposal:")
            assert f"on {NBL} baselines" in resp["notify_text"]
            assert len(vp.flag_db) == 0
            _run(vp.flags.handle_action({"action": "reject"}))
        finally:
            _run(vp.flags.handle_action({"action": "config", "preview": False}))
            vp.flag_db.clear(record=False)

    def test_one_baseline_box_does_not_mention_baselines(self, plotter):
        vp = plotter
        vp.flag_db.clear(record=False)
        bl = vp._meta.baselines[2]
        msg = _plot_message(vp)
        msg["baselines"] = [list(bl.pair)]
        _run(vp._handle_plot(msg))
        r = vp._slots[0].raster
        t = r.agg.coords["time"].values
        try:
            resp = _run(vp._handle_box_select(
                dict(x0=1.8, x1=2.2, y0=float(t[3]) - 1, y1=float(t[3]) + 1, flag=True),
                "raster", r))
            assert resp["notify_text"].startswith("✓ Flagged:")
            assert "baselines" not in resp["notify_text"]
        finally:
            vp.flag_db.clear(record=False)
            _run(vp._handle_plot(_plot_message(vp)))


# ---------------------------------------------------------------------------
# 6. Generated task layers
# ---------------------------------------------------------------------------

def test_task_layers_have_the_argument():
    import cubevis
    root = pathlib.Path(cubevis.__file__).parent / "private"
    for name in ("casatasks/visplot.py", "casashell/visplot.py"):
        src = (root / name).read_text()
        assert re.search(r"baseline_combine", src), name
        assert "'mean'" in src or '"mean"' in src
