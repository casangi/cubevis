"""
test_scatter_average.py
=======================
Averaged scatter points (2026-10-09, HRS H6 slice 1): one point per
baseline and correlation per time window (off / scan / seconds) and
channel window (off / N / all), vector or scalar; flagged samples left
out; a flag box takes the samples behind the points it encloses.

Location in repository:
    cubevis/tests/manual/visplot/test_scatter_average.py

Run:
    pytest cubevis/tests/manual/visplot/test_scatter_average.py -v

Sections
--------
1. Grouping and averaging   on hand-made frames, against numpy
2. Backends                 averaged frames of a simulated MS (and its
                            MSv4 twin) against the visibilities read
                            directly; raw frames are not read again when
                            the averaging changes
3. Flagging                 flagged samples left out; a box on averaged
                            points flags the samples behind them; other
                            filters refused
4. Plotter                  constructor arguments, gear-tab controls,
                            a Plot message, title, help

The simulated data: 5 antennas, 24 integrations of 8 s in three scans of
8, 16 channels, XX and YY; V = A * exp(i * phi) with A and phi changing
with integration, channel and baseline, so vector and scalar averages
differ.

What is NOT covered: anything that only happens in a browser.
"""
from __future__ import annotations

import asyncio
import dataclasses

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data import _scatter_average as sa
from cubevis.toolbox.visplot.selection import SelectionSpec
from cubevis.toolbox.visplot import flag_engine as fe

NTIME, NANT, NCHAN, DUMP, PER_SCAN = 24, 5, 16, 8.0, 8


# ---------------------------------------------------------------------------
# 1. Grouping and averaging
# ---------------------------------------------------------------------------

def _frame(nt=6, nch=4, nbl=2, scans=None, t0=100.0, dt=8.0):
    rows = []
    for k in range(nt):
        for b in range(nbl):
            for c in range(nch):
                rows.append(dict(time=t0 + dt * k, baseline_id=b, __spw=0, __chan=c,
                                 baseline_antenna1_name="A", baseline_antenna2_name=f"B{b}",
                                 baseline_name=f"A&B{b}", frequency=1e9 + 1e6 * c,
                                 x=t0 + dt * k, y=0.0,
                                 **({"scan_name": str(scans[k])} if scans is not None else {})))
    return pd.DataFrame(rows)


class TestGrouping:

    def test_normalizers(self):
        assert sa.normalize_avg_time(None) == "off"
        assert sa.normalize_avg_time("Scan") == "scan"
        assert sa.normalize_avg_time("30") == 30.0
        with pytest.raises(ValueError):
            sa.normalize_avg_time(-2)
        with pytest.raises(ValueError):
            sa.normalize_avg_time("often")
        assert sa.normalize_avg_chan("ALL") == "all"
        assert sa.normalize_avg_chan("8") == 8
        assert sa.normalize_avg_chan(1) == "off"
        with pytest.raises(ValueError):
            sa.normalize_avg_chan("some")

    def test_inactive_unless_asked(self):
        assert sa.scatter_average_of(SelectionSpec()) is None
        s = sa.scatter_average_of(SelectionSpec(avg_time="scan", averaging="scalar"))
        assert s == sa.ScatterAverage("scan", "off", "scalar")
        assert s.describe() == "scalar avg: scan"

    def test_counts_per_window(self):
        df = _frame(scans=[1, 1, 1, 2, 2, 2])
        def n_points(**kw):
            return int(sa.group_rows(df, sa.ScatterAverage(**kw)).max()) + 1
        assert n_points(time="scan") == 2 * 2 * 4          # scans x baselines x channels
        assert n_points(chan="all") == 6 * 2               # integrations x baselines
        assert n_points(chan=2) == 6 * 2 * 2
        assert n_points(time="scan", chan="all") == 2 * 2
        assert n_points(time=16.0) == 2 * 2 * 2 * 4         # 2 windows of 16 s per scan

    def test_windows_start_at_each_scan_and_never_span_two(self):
        df = _frame(nt=6, nch=1, nbl=1, scans=[1, 1, 1, 2, 2, 2])
        codes = sa.group_rows(df, sa.ScatterAverage(time=1000.0))
        assert codes.tolist() == [0, 0, 0, 1, 1, 1]
        codes = sa.group_rows(df, sa.ScatterAverage(time=16.0))
        assert codes.tolist() == [0, 0, 1, 2, 2, 3]

    def test_without_scans_a_gap_ends_a_window(self):
        df = _frame(nt=6, nch=1, nbl=1)
        df.loc[3:, "time"] += 500.0
        codes = sa.group_rows(df, sa.ScatterAverage(time=1e6))
        assert codes.tolist() == [0, 0, 0, 1, 1, 1]

    def test_vector_and_scalar_against_numpy(self):
        rng = np.random.default_rng(0)
        df = _frame(scans=[1, 1, 1, 2, 2, 2])
        v = rng.normal(5, 1, len(df)) * np.exp(1j * rng.uniform(-3, 3, len(df)))
        spec_v = sa.ScatterAverage("scan", "all", "vector")
        spec_s = sa.ScatterAverage("scan", "all", "scalar")
        codes = sa.group_rows(df, spec_v)
        for q in sa.AVERAGED_QUANTITIES:
            gv = sa.average_frame(q, df, v.real, v.imag, codes, spec_v)
            gs = sa.average_frame(q, df, v.real, v.imag, codes, spec_s)
            for g in range(codes.max() + 1):
                m = v[codes == g]
                want_v = {Axis.AMPLITUDE: abs(m.mean()), Axis.PHASE: np.degrees(np.angle(m.mean())),
                          Axis.REAL: m.real.mean(), Axis.IMAGINARY: m.imag.mean()}[q]
                want_s = {Axis.AMPLITUDE: np.abs(m).mean(),
                          Axis.PHASE: np.degrees(np.angle((m / np.abs(m)).mean())),
                          Axis.REAL: m.real.mean(), Axis.IMAGINARY: m.imag.mean()}[q]
                assert gv["y"][g] == pytest.approx(want_v, abs=1e-9)
                assert gs["y"][g] == pytest.approx(want_s, abs=1e-9)
                assert gv["avg_n"][g] == m.size
                assert gv["x"][g] == pytest.approx(df["x"].to_numpy()[codes == g].mean())

    def test_alignment_check(self):
        a = _frame()
        assert sa.aligned(a, a.copy())
        b = a.copy(); b.loc[0, "__chan"] = 3
        assert not sa.aligned(a, b)
        assert not sa.aligned(a, a.iloc[1:])


# ---------------------------------------------------------------------------
# Simulated data
# ---------------------------------------------------------------------------

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


@pytest.fixture(scope="module")
def sim_ms(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    from arcae.lib.arrow_tables import Table
    path = str(tmp_path_factory.mktemp("savg") / "a.ms")
    sim.MSStructureSimulator(
        ntime=NTIME, time_chunks=NTIME, dump_rate=DUMP, time_start=5.0e9,
        nantenna=NANT, auto_corrs=False, data_description=[(NCHAN, ["XX", "YY"])],
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
def sim_ps(sim_ms, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("savgps") / "a.ps.zarr")
    dt = xr.open_datatree(sim_ms, engine="xarray-ms:msv2", partition_schema=["FIELD_ID"])
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


def _cached(b, x, key, sel, view="effective"):
    from cubevis.toolbox.visplot.data.reader import _FlagViewContext
    with _FlagViewContext(view):
        return b._query_columns_cached_raw(x, [key], sel, b._frame_cache_obj(), None)[key]


# ---------------------------------------------------------------------------
# 2. Backends
# ---------------------------------------------------------------------------

class TestBackends:

    @pytest.mark.parametrize("mode", ["vector", "scalar"])
    def test_spectrum_per_scan_matches_numpy(self, backend, mode):
        sel = SelectionSpec(correlation=["XX", "YY"], avg_time="scan", averaging=mode)
        df = _cached(backend, Axis.CHANNEL, (Axis.AMPLITUDE, "YY"), sel)
        d = _direct(backend, "YY")
        assert len(df) == 10 * 3 * NCHAN                 # baselines x scans x channels
        assert (df["avg_n"] == PER_SCAN).all()
        for _, r in df.sample(40, random_state=1).iterrows():
            bl, scan, ch = f"{r.baseline_antenna1_name}&{r.baseline_antenna2_name}", int(r.scan_name), int(r.x)
            m = np.array([d[(bl, scan, ch, k)] for k in range((scan - 1) * PER_SCAN, scan * PER_SCAN)])
            want = abs(m.mean()) if mode == "vector" else np.abs(m).mean()
            assert r.y == pytest.approx(want, rel=1e-5)

    def test_time_series_over_all_channels(self, backend):
        sel = SelectionSpec(correlation=["XX"], avg_chan="all")
        df = _cached(backend, Axis.TIME, (Axis.PHASE, "XX"), sel)
        d = _direct(backend, "XX")
        assert len(df) == 10 * NTIME and (df["avg_n"] == NCHAN).all()
        t0 = df["time"].min()
        for _, r in df.sample(30, random_state=2).iterrows():
            bl = f"{r.baseline_antenna1_name}&{r.baseline_antenna2_name}"
            k = int(round((r.time - t0) / DUMP))
            m = np.array([d[(bl, 1 + k // PER_SCAN, c, k)] for c in range(NCHAN)])
            assert r.y == pytest.approx(np.degrees(np.angle(m.mean())), abs=1e-3)

    def test_other_quantities_are_not_averaged(self, backend):
        sel = SelectionSpec(correlation=["XX"], avg_time="scan")
        df = _cached(backend, Axis.CHANNEL, (Axis.PHASE_RMS, "XX"), sel)
        assert len(df) == 10 * NTIME * NCHAN and "avg_n" not in df.columns

    def test_changing_the_averaging_reads_nothing(self, backend, monkeypatch):
        sel = SelectionSpec(correlation=["XX"])
        _cached(backend, Axis.CHANNEL, (Axis.AMPLITUDE, "XX"), dataclasses.replace(sel, avg_time="scan"))
        calls = []
        orig = type(backend)._query_columns_raw
        monkeypatch.setattr(type(backend), "_query_columns_raw",
                            lambda self, *a, **k: calls.append(a) or orig(self, *a, **k))
        for t, c, m in (("off", "off", "vector"), (30.0, 4, "scalar"), ("scan", "all", "vector")):
            _cached(backend, Axis.CHANNEL, (Axis.AMPLITUDE, "XX"),
                    dataclasses.replace(sel, avg_time=t, avg_chan=c, averaging=m))
        assert calls == []


# ---------------------------------------------------------------------------
# 3. Flagging and the plotter
# ---------------------------------------------------------------------------

def _run(c):
    return asyncio.run(c)


@pytest.fixture(params=["msv2", "msv4"])
def plotter(request, sim_ms, sim_ps):
    from cubevis.toolbox.visplot import VisibilityPlotter
    kw = dict(ms=sim_ms) if request.param == "msv2" else dict(ps=sim_ps)
    vp = VisibilityPlotter(layout="side", correlation="XX,YY", kind="scatter",
                           scatter_x="CHANNEL", scatter_avg_time="scan", **kw)
    yield vp
    vp.flag_db.clear(record=False)
    vp.close()


def _pending(vp):
    """{(a1&a2, integration, channel, pol)} the pending flags flag."""
    b = vp._reader._backend
    out = set()
    t_all = None
    for part in b._iter_visibility_partitions(None):
        f = np.asarray(fe.apply_pending(b, part, b._flag_mask(part), vp.flag_db.deltas())
                       .transpose("time", fe._bdim(b), "frequency", "polarization").values, bool)
        t = np.asarray(part.time.values, float)
        t_all = np.sort(t) if t_all is None else t_all
        a1 = part.baseline_antenna1_name.values.astype(str)
        a2 = part.baseline_antenna2_name.values.astype(str)
        pols = part.polarization.values.astype(str)
        for it, ib, ic, ip in zip(*np.nonzero(f)):
            out.add((f"{a1[ib]}&{a2[ib]}", int(round((t[it] - t_all[0]) / DUMP)), int(ic), str(pols[ip])))
    return out


class TestFlagging:

    def test_box_on_averaged_points_flags_the_samples_behind(self, plotter):
        vp = plotter
        sc = vp._slots[0].scatter
        assert sc.scatter_average() == sa.ScatterAverage("scan", "off", "vector")
        df = _cached(vp._reader._backend, Axis.CHANNEL, (Axis.AMPLITUDE, "XX"),
                     sc._with_stat_settings(sc._selection))
        pt = df.iloc[int(np.argmax(df["y"].to_numpy()))]          # the highest point
        msg = dict(x0=pt.x - 0.2, x1=pt.x + 0.2, y0=pt.y - 1e-3, y1=pt.y + 1e-3, flag=True)
        resp = _run(vp._handle_box_select(msg, "scatter", sc))
        assert resp["notify_text"].startswith("✓"), resp["notify_text"]
        bl = f"{pt.baseline_antenna1_name}&{pt.baseline_antenna2_name}"
        scan = int(pt.scan_name)
        want = {(bl, k, int(pt.x), "XX") for k in range((scan - 1) * PER_SCAN, scan * PER_SCAN)}
        got = _pending(vp)
        assert want <= got and {g for g in got if g[3] == "XX"} == want
        assert vp.flag_db.deltas()[-1].n_samples == len(got)

    def test_flagged_samples_are_left_out(self, plotter):
        vp = plotter
        sc = vp._slots[0].scatter
        b = vp._reader._backend
        sel = sc._with_stat_settings(sc._selection)
        before = _cached(b, Axis.CHANNEL, (Axis.AMPLITUDE, "XX"), sel)
        # flag one integration of everything with an unaveraged raster box
        r = vp._slots[0].raster
        r.update_axes(y_dim=Axis.TIME, x_dim=Axis.CHANNEL)
        t = np.sort(np.unique(before["time"]))
        tt = np.sort(np.unique(np.concatenate([np.asarray(p.time.values, float)
                                               for p in b._iter_visibility_partitions(None)])))
        _run(vp._handle_box_select(dict(x0=-0.5, x1=NCHAN - 0.5, y0=tt[2] - 1, y1=tt[2] + 1,
                                        flag=True), "raster", r))
        sel2 = sc._with_stat_settings(dataclasses.replace(
            sc._selection, pending_version=vp.flag_db.version))
        b._cv_pending = tuple(vp.flag_db.deltas())
        after = _cached(b, Axis.CHANNEL, (Axis.AMPLITUDE, "XX"), sel2)
        n_first = after.loc[after["scan_name"].astype(str) == "1", "avg_n"]
        n_other = after.loc[after["scan_name"].astype(str) != "1", "avg_n"]
        assert (n_first == PER_SCAN - 1).all() and (n_other == PER_SCAN).all()

    def test_other_filters_are_refused(self, plotter):
        vp = plotter
        sc = vp._slots[0].scatter
        vp._flags.filter_name = "value_range"
        vp._flags.filter_params["value_range"] = {"low": 1.0}
        try:
            f = sc._fig
            resp = _run(vp._handle_box_select(dict(x0=f.x_range.start, x1=f.x_range.end,
                                                   y0=f.y_range.start, y1=f.y_range.end,
                                                   flag=True), "scatter", sc))
            assert "only the All selected filter" in resp["notify_text"]
            assert len(vp.flag_db) == 0
        finally:
            vp._flags.filter_name = "all"

    def test_unaveraged_scatter_is_as_before(self, plotter):
        vp = plotter
        sc = vp._slots[0].scatter
        sc.update_axes(avg_time="off")
        assert sc.scatter_average() is None
        resp = _run(vp._handle_box_select(dict(x0=-1e9, x1=1e9, y0=-1e9, y1=1e9,
                                               flag=True), "scatter", sc))
        assert resp["notify_text"].startswith("✓"), resp["notify_text"]
        assert len(_pending(vp)) == 10 * NTIME * NCHAN * 2


class TestPlotter:

    def test_title_says_how(self, plotter):
        assert "(vector avg: scan)" in plotter._slots[0].scatter._effective_title()

    def test_gear_controls_and_help(self, plotter):
        vp = plotter
        vp._build_layout()
        w = vp._panel_axis_widgets[vp._slots[0].id]["scatter"]
        assert w["avg_time_sel"].value == "scan"
        assert w["avg_chan_sel"].value == "off"
        assert w["avg_mode_sel"].value == "vector"
        assert "all" in [o[0] for o in w["avg_chan_sel"].options]
        assert vp._hint_s_avg.text.startswith("<b>Averaging (scatter)</b>")
        js = vp._do_plot_js
        assert "avg_time: sat_sel ? sat_sel.value : null" in js
        assert "panel1_sat_sel, panel1_sac_sel, panel1_sam_sel," in js

    def test_plot_message_changes_the_averaging(self, plotter):
        vp = plotter
        sc = vp._slots[0].scatter
        msg = {"panels": {vp._slots[0].id: {"kind": "scatter", "x": "TIME", "y": "AMPLITUDE",
                                            "avg_time": "off", "avg_chan": "all",
                                            "averaging": "scalar"}}}
        _run(vp._handle_plot(msg))
        assert sc.scatter_average() == sa.ScatterAverage("off", "all", "scalar")
        assert "(scalar avg: all channels)" in sc._effective_title()

    def test_constructor_validates(self, sim_ms):
        from cubevis.toolbox.visplot import VisibilityPlotter
        with pytest.raises(ValueError):
            VisibilityPlotter(ms=sim_ms, scatter_avg_time="sometimes")


# ---------------------------------------------------------------------------
# 5. The X-axis link between the two panels (found on the way, 2026-10-09)
# ---------------------------------------------------------------------------

class TestXLink:
    """The scatter and raster used to share one X Range model when the
    constructor gave them the same X axis -- kept after Plot changed one
    of the axes, so the raster's Channel axis was set to time values and
    went blank.  The link is now a check made on every pan / zoom."""

    def test_no_figures_share_a_range(self, sim_ms):
        from cubevis.toolbox.visplot import VisibilityPlotter
        vp = VisibilityPlotter(ms=sim_ms, kind="scatter", scatter_x="CHANNEL",
                               raster_y="TIME", raster_x="CHANNEL")
        try:
            vp._build_layout()
            ranges = [pn.figure.x_range for pn in vp._all_panels]
            assert len({id(r) for r in ranges}) == len(ranges)
            for r in ranges:
                cbs = r.js_property_callbacks.get("change:start", [])
                assert any("__cvXSync" in cb.code for cb in cbs)
        finally:
            vp.close()

    @pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node not on PATH")
    def test_link_follows_only_matching_visible_panels(self, tmp_path):
        import json, subprocess
        from cubevis.toolbox.visplot.visibility_plotter import _X_SYNC_JS
        script = """
function Range(s, e) { this.start = s; this.end = e; this.setv = function (o) {
    this.start = o.start; this.end = o.end; }; }
function entry(label, visible) { return {fig: {x_range: new Range(0, 1)},
    state: {data: {x_label: [label]}}, layout: {visible: visible}}; }
const window = {};
function sync(entries, cb_obj) { %s }
const out = {};
let E = [entry('Channel', true), entry('Channel', true), entry('Channel', false), entry('Time [s]', true)];
E[0].fig.x_range.start = 3; E[0].fig.x_range.end = 7; sync(E, E[0].fig.x_range);
out.same = [E[1].fig.x_range.start, E[1].fig.x_range.end];
out.hidden = [E[2].fig.x_range.start, E[2].fig.x_range.end];
out.other = [E[3].fig.x_range.start, E[3].fig.x_range.end];
E[2].fig.x_range.start = 5; sync(E, E[2].fig.x_range);
out.from_hidden = [E[0].fig.x_range.start];
console.log(JSON.stringify(out));
""" % _X_SYNC_JS
        f = tmp_path / "xsync.js"
        f.write_text(script)
        res = subprocess.run(["node", str(f)], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        got = json.loads(res.stdout)
        assert got == {"same": [3, 7], "hidden": [0, 1], "other": [0, 1], "from_hidden": [3]}
