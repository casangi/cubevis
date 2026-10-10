"""
test_scatter_presets.py
=======================
HRS H6 slice 2 (2026-10-09): the Spectrum and Time series presets (two
scatter panels, Amplitude over Phase, averaged), autocorrelations, and
two display fixes found on the way: panels render at their figure's
current size, and sparse scatter points are drawn larger.

Location in repository:
    cubevis/tests/manual/visplot/test_scatter_presets.py

Run:
    pytest cubevis/tests/manual/visplot/test_scatter_presets.py -v

Sections
--------
1. Autocorrelations   found in an MSv2 and opened with it; listed in the
                      Baseline table; averaged like any baseline
2. Presets            constructor (both panels, axes, averaging, layout),
                      toolbar buttons and their JS, help
3. Preset values      a Plot as the buttons send it, one baseline (cross
                      and auto), both backends: the averaged values the
                      two panels draw against numpy
4. Display            set_pixel_size / the redraw message; spread_sparse

The simulated data: 4 antennas with autocorrelations, 16 integrations of
8 s in two scans of 8, 16 channels, XX and YY; V = A * exp(i * phi) with A
and phi changing with integration, channel and baseline.

What is NOT covered: anything that only happens in a browser (checked
live with devel/tools/visplot_headless: the buttons, Baseline Prev / Next
with a preset showing, the frame-size redraw after a layout change).
"""
from __future__ import annotations

import asyncio
import dataclasses

import numpy as np
import pytest
import xarray as xr

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data import _scatter_average as sa
from cubevis.toolbox.visplot.selection import SelectionSpec
from cubevis.toolbox.visplot import flag_engine as fe

NTIME, NANT, NCHAN, DUMP, PER_SCAN = 16, 4, 16, 8.0, 8
NBL = NANT * (NANT + 1) // 2           # with autocorrelations


def _transform(desc, data):
    dims, vis = data["DATA"]
    td, t = data["TIME"]
    k = np.round((np.asarray(t, float) - 5.0e9) / DUMP).astype(int)
    a1 = np.asarray(data["ANTENNA1"][1]).astype(float)
    a2 = np.asarray(data["ANTENNA2"][1]).astype(float)
    ch = np.arange(vis.shape[1])[None, :, None]
    amp = (10.0 + 0.3 * k + a1 + 0.5 * a2)[:, None, None] + 0.1 * ch
    ph = (0.2 * k + 0.4 * (a1 - a2))[:, None, None] + 0.25 * ch
    corr = np.arange(vis.shape[2])[None, None, :]
    v = amp * np.exp(1j * (ph + 0.5 * corr))
    data["DATA"] = (dims, v.astype(np.complex64))
    fd, _ = data["FLAG"]
    data["FLAG"] = (fd, np.zeros(vis.shape, bool))
    return data


def _simulate(path, auto_corrs):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    from arcae.lib.arrow_tables import Table
    sim.MSStructureSimulator(
        ntime=NTIME, time_chunks=NTIME, dump_rate=DUMP, time_start=5.0e9,
        nantenna=NANT, auto_corrs=auto_corrs,
        data_description=[(NCHAN, ["XX", "YY"])],
        simulate_data=True, transform_data=_transform).simulate_ms(path)
    tab = Table.from_filename(path, readonly=False)
    try:
        tm = np.asarray(tab.getcol("TIME"))
        idx = np.round((tm - tm.min()) / DUMP).astype(np.int32)
        tab.putcol("SCAN_NUMBER", (1 + idx // PER_SCAN).astype(np.int32))
    finally:
        tab.close()
    return path


@pytest.fixture(scope="module")
def sim_ms(tmp_path_factory):
    return _simulate(str(tmp_path_factory.mktemp("spre") / "a.ms"), True)


@pytest.fixture(scope="module")
def sim_ps(sim_ms, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("spreps") / "a.ps.zarr")
    dt = xr.open_datatree(sim_ms, engine="xarray-ms:msv2",
                          partition_schema=["FIELD_ID"], auto_corrs=True)
    dt.to_zarr(out, mode="w", compute=True)
    return out


def _kw(kind, sim_ms, sim_ps):
    return dict(ms=sim_ms) if kind == "msv2" else dict(ps=sim_ps)


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


def _direct(b, pol):
    """``{(a1&a2, scan, chan, k): complex}`` read straight from the partitions."""
    out = {}
    t_all = None
    for part in b._iter_visibility_partitions(None):
        v = b._resolve_vis(part).transpose("time", fe._bdim(b), "frequency", "polarization").values
        t = np.asarray(part.time.values, float)
        t_all = np.sort(t) if t_all is None else t_all
        a1 = part.baseline_antenna1_name.values.astype(str)
        a2 = part.baseline_antenna2_name.values.astype(str)
        ip = list(part.polarization.values.astype(str)).index(pol)
        for it in range(v.shape[0]):
            k = int(round((t[it] - t_all[0]) / DUMP))
            for ib in range(v.shape[1]):
                for ic in range(v.shape[2]):
                    out[(f"{a1[ib]}&{a2[ib]}", 1 + k // PER_SCAN, ic, k)] = v[it, ib, ic, ip]
    return out


def _cached(b, x, key, sel):
    from cubevis.toolbox.visplot.data.reader import _FlagViewContext
    with _FlagViewContext("effective"):
        return b._query_columns_cached_raw(x, [key], sel, b._frame_cache_obj(), None)[key]


def _run(c):
    return asyncio.run(c)


# ---------------------------------------------------------------------------
# 1. Autocorrelations
# ---------------------------------------------------------------------------

class TestAutocorrelations:

    def test_found_in_the_ms(self, sim_ms, tmp_path):
        from cubevis.toolbox.visplot.data.msv2_backend import has_autocorrelations
        assert has_autocorrelations(sim_ms)
        assert has_autocorrelations(sim_ms, probe_rows=3)       # both ends read
        plain = _simulate(str(tmp_path / "p.ms"), False)
        assert not has_autocorrelations(plain)
        assert not has_autocorrelations(str(tmp_path / "missing.ms"))

    def test_listed_and_marked(self, backend):
        from cubevis.toolbox.visplot import antenna_baseline_select as abs_
        from cubevis.toolbox.visplot.reduction_context import ObservationMetadata
        meta = ObservationMetadata.from_backend_metadata(backend.metadata())
        autos = [b for b in meta.baselines if b.is_auto]
        assert len(meta.baselines) == NBL and len(autos) == NANT
        names = abs_.baseline_table_data(meta)["name"]
        assert sum(n.endswith(" (auto)") for n in names) == NANT

    def test_an_ms_without_them_gains_no_empty_ones(self, tmp_path):
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
        b = MSv2Backend(_simulate(str(tmp_path / "p.ms"), False))
        b.open()
        from cubevis.toolbox.visplot.reduction_context import ObservationMetadata
        try:
            meta = ObservationMetadata.from_backend_metadata(b.metadata())
            assert meta.baselines and not any(x.is_auto for x in meta.baselines)
        finally:
            b.close()

    def test_averaged_like_any_baseline(self, backend):
        bl = ("ANTENNA-1", "ANTENNA-1")
        sel = SelectionSpec(correlation=["XX"], baselines=[bl], avg_time="scan")
        df = _cached(backend, Axis.FREQUENCY, (Axis.AMPLITUDE, "XX"), sel)
        d = _direct(backend, "XX")
        assert len(df) == 2 * NCHAN and (df["avg_n"] == PER_SCAN).all()
        assert set(zip(df.baseline_antenna1_name, df.baseline_antenna2_name)) == {bl}


# ---------------------------------------------------------------------------
# 2. Presets
# ---------------------------------------------------------------------------

@pytest.fixture(params=["msv2", "msv4"])
def kind(request):
    return request.param


class TestPresetConstruction:

    @pytest.mark.parametrize("preset, x, at, ac", [
        ("spectrum",   Axis.FREQUENCY, "scan", "off"),
        ("timeseries", Axis.TIME,      "off",  "all"),
    ])
    def test_both_panels_scatter(self, kind, sim_ms, sim_ps, preset, x, at, ac):
        from cubevis.toolbox.visplot import VisibilityPlotter
        vp = VisibilityPlotter(preset=preset, correlation="XX,YY", **_kw(kind, sim_ms, sim_ps))
        try:
            assert vp._layout == "over"
            assert [s.kind for s in vp._slots] == ["scatter", "scatter"]
            ys = [s.scatter.layers[0].y_axis for s in vp._slots]
            assert ys == [Axis.AMPLITUDE, Axis.PHASE]
            for s in vp._slots:
                assert s.scatter._x_dim == x
                assert s.scatter.scatter_average() == sa.ScatterAverage(at, ac, "vector")
            want = "(vector avg: scan)" if at == "scan" else "(vector avg: all channels)"
            assert want in vp._slots[0].scatter._effective_title()
            assert want in vp._slots[1].scatter._effective_title()
        finally:
            vp.close()

    def test_explicit_averaging_wins(self, sim_ms):
        from cubevis.toolbox.visplot import VisibilityPlotter
        vp = VisibilityPlotter(ms=sim_ms, preset="spectrum", scatter_avg_time="30",
                               scatter_avg_chan="4")
        try:
            assert vp._slots[1].scatter.scatter_average() == sa.ScatterAverage(30.0, 4, "vector")
        finally:
            vp.close()

    def test_toolbar_buttons(self, sim_ms):
        from cubevis.toolbox.visplot import VisibilityPlotter
        vp = VisibilityPlotter(ms=sim_ms)
        try:
            vp._build_layout()
            w0 = vp._panel_axis_widgets[vp._slots[0].id]["scatter"]
            w1 = vp._panel_axis_widgets[vp._slots[1].id]["scatter"]
            assert w0["y_sel"].value == w1["y_sel"].value          # unchanged default
            for name, x, at, ac in (("spectrum", "FREQUENCY", "scan", "off"),
                                    ("timeseries", "TIME", "off", "all")):
                assert name in vp._preset_buttons
                js = [cb for cb in vp._preset_buttons[name].js_event_callbacks["button_click"]][0].code
                for line in ("panel0_kind_switch.active = 1;", "panel1_kind_switch.active = 1;",
                             "layout_rbg.active = 2;", "pos0_scatter_layout.visible = true;",
                             "pos1_scatter_layout.visible = true;",
                             f"panel0_sx_sel.value = '{x}';", f"panel1_sx_sel.value = '{x}';",
                             "panel0_sy_sel.value = 'AMPLITUDE';", "panel1_sy_sel.value = 'PHASE';",
                             f"panel0_sat_sel.value = '{at}';", f"panel1_sac_sel.value = '{ac}';",
                             "doPlot();"):
                    assert line in js, (name, line)
                assert getattr(vp, f"_hint_preset_{name}").text.startswith("<b>")
        finally:
            vp.close()

    def test_panel_y_selects_follow_the_panels(self, sim_ms):
        from cubevis.toolbox.visplot import VisibilityPlotter
        vp = VisibilityPlotter(ms=sim_ms, preset="timeseries")
        try:
            vp._build_layout()
            ys = [vp._panel_axis_widgets[s.id]["scatter"]["y_sel"].value for s in vp._slots]
            assert ys == ["AMPLITUDE", "PHASE"]
        finally:
            vp.close()


# ---------------------------------------------------------------------------
# 3. Preset values against numpy
# ---------------------------------------------------------------------------

def _pair_plot_msg(vp, x, at, ac, baseline):
    panels = {}
    for slot, y in zip(vp._slots, ("AMPLITUDE", "PHASE")):
        panels[slot.id] = {"kind": "scatter", "x": x, "y": y, "avg_time": at,
                           "avg_chan": ac, "averaging": "vector", "w": 900, "h": 260}
    return {"panels": panels, "baselines": [list(baseline)]}


class TestPresetValues:

    @pytest.mark.parametrize("baseline", [("ANTENNA-0", "ANTENNA-2"),
                                          ("ANTENNA-2", "ANTENNA-2")])
    def test_spectrum(self, kind, sim_ms, sim_ps, baseline):
        from cubevis.toolbox.visplot import VisibilityPlotter
        vp = VisibilityPlotter(preset="spectrum", correlation="XX,YY", **_kw(kind, sim_ms, sim_ps))
        try:
            resp = _run(vp._handle_plot(_pair_plot_msg(vp, "FREQUENCY", "scan", "off", baseline)))
            assert resp.get("status") != "error", resp
            b = vp._reader._backend
            d = _direct(b, "YY")
            bl = "&".join(baseline)
            for slot, q in zip(vp._slots, (Axis.AMPLITUDE, Axis.PHASE)):
                sc = slot.scatter
                assert (sc._width, sc._height) == (900, 260)
                sel = sc._with_stat_settings(sc._selection)
                assert sel.baselines == [baseline]
                df = _cached(b, Axis.FREQUENCY, (q, "YY"), sel)
                assert len(df) == 2 * NCHAN and (df["avg_n"] == PER_SCAN).all()
                freqs = np.sort(df["x"].unique())
                for _, r in df.iterrows():
                    scan, ch = int(r.scan_name), int(np.searchsorted(freqs, r.x))
                    m = np.array([d[(bl, scan, ch, k)]
                                  for k in range((scan - 1) * PER_SCAN, scan * PER_SCAN)])
                    want = (abs(m.mean()) if q is Axis.AMPLITUDE
                            else np.degrees(np.angle(m.mean())))
                    assert r.y == pytest.approx(want, rel=1e-5, abs=1e-3)
        finally:
            vp.close()

    def test_time_series(self, kind, sim_ms, sim_ps):
        from cubevis.toolbox.visplot import VisibilityPlotter
        baseline = ("ANTENNA-1", "ANTENNA-3")
        vp = VisibilityPlotter(preset="timeseries", correlation="XX,YY", **_kw(kind, sim_ms, sim_ps))
        try:
            resp = _run(vp._handle_plot(_pair_plot_msg(vp, "TIME", "off", "all", baseline)))
            assert resp.get("status") != "error", resp
            b = vp._reader._backend
            d = _direct(b, "XX")
            for slot, q in zip(vp._slots, (Axis.AMPLITUDE, Axis.PHASE)):
                sc = slot.scatter
                df = _cached(b, Axis.TIME, (q, "XX"), sc._with_stat_settings(sc._selection))
                assert len(df) == NTIME and (df["avg_n"] == NCHAN).all()
                t0 = df["time"].min()
                for _, r in df.iterrows():
                    k = int(round((r.time - t0) / DUMP))
                    m = np.array([d[("&".join(baseline), 1 + k // PER_SCAN, c, k)]
                                  for c in range(NCHAN)])
                    want = (abs(m.mean()) if q is Axis.AMPLITUDE
                            else np.degrees(np.angle(m.mean())))
                    assert r.y == pytest.approx(want, rel=1e-5, abs=1e-3)
        finally:
            vp.close()


# ---------------------------------------------------------------------------
# 4. Display: render size and sparse points
# ---------------------------------------------------------------------------

class TestDisplay:

    def test_set_pixel_size(self, sim_ms):
        from cubevis.toolbox.visplot import VisibilityPlotter
        vp = VisibilityPlotter(ms=sim_ms, kind="scatter", scatter_x="CHANNEL")
        try:
            sc = vp._slots[0].scatter
            w, h = sc._width, sc._height
            assert not sc.set_pixel_size(None, 300)
            assert not sc.set_pixel_size("x", 300)
            assert not sc.set_pixel_size(5, 300)
            assert not sc.set_pixel_size(w, h)
            assert sc.set_pixel_size(1000, 250) and (sc._width, sc._height) == (1000, 250)
        finally:
            vp.close()

    def test_redraw_message_carries_the_size(self, sim_ms):
        from cubevis.toolbox.visplot import VisibilityPlotter
        vp = VisibilityPlotter(ms=sim_ms, kind="scatter", scatter_x="CHANNEL",
                               correlation="XX")
        try:
            sc = vp._slots[0].scatter
            x0, x1 = sc._x_range
            y0, y1 = sc._y_range
            msg = dict(x0=x0, x1=x1, y0=y0, y1=y1, w=sc._width, h=sc._height, size_only=True)
            assert sc._handle_rerender(msg) == {}          # same size: nothing to draw
            msg.update(w=1000, h=250)
            out = sc._handle_rerender(msg)
            assert (sc._width, sc._height) == (1000, 250)
            img = np.asarray(out["image"])
            # the canvas has the figure's shape (sparse data shrink it, by
            # the same factor in both directions)
            assert img.shape[1] / img.shape[0] == pytest.approx(1000 / 250, rel=0.15)
        finally:
            vp.close()

    def test_redraw_js(self, sim_ms):
        from cubevis.toolbox.visplot import VisibilityPlotter
        vp = VisibilityPlotter(ms=sim_ms, kind="scatter")
        try:
            vp._build_layout()
            fig = vp._slots[0].scatter.figure
            assert "change:width" in fig.js_property_callbacks
            assert "change:height" in fig.js_property_callbacks
            code = fig.js_property_callbacks["change:width"][0].code
            assert "size_only: sizeOnly" in code and "fig.inner_width" in code
            assert "e[0].w = f.inner_width" in vp._do_plot_js
        finally:
            vp.close()

    def test_spread_sparse(self):
        from cubevis.toolbox.visplot.visibility_scatter import spread_sparse
        a = np.zeros((50, 50), np.uint32)
        a[5, 5], a[5, 7], a[0, 0] = 0xFF0000FF, 0xFFFF0000, 0xFF00FF00
        b = a.copy()
        assert spread_sparse(b, 1.0)
        drawn = (b >> 24) > 0
        assert drawn[4:7, 4:9].all() and drawn[0:2, 0:2].all() and drawn.sum() == 15 + 4
        assert b[5, 5] == a[5, 5] and b[5, 7] == a[5, 7]       # points keep their colour
        assert b[4, 4] == a[5, 5] and b[4, 8] == a[5, 7]
        c = a.copy()
        assert not spread_sparse(c, 3.0) and (c == a).all()    # bins already big enough
        dense = np.full((10, 10), 0xFF000001, np.uint32)
        dense[0, 0] = 0
        assert not spread_sparse(dense, 1.0)                   # not sparse
        assert not spread_sparse(np.zeros((5, 5), np.uint32), 1.0)
