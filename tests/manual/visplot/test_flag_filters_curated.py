"""
test_flag_filters_curated.py
============================
The curated flag filters of the Flagging panel (2026-10-09, HRS H5 slice
2): Value range on the displayed quantity, Outlier from neighbours
(running median along time or channel), and Grow around flags; the
reference-population choice hidden; the old Amplitude range kept for
scripts but not listed.

Location in repository:
    cubevis/tests/manual/visplot/test_flag_filters_curated.py

Run:
    pytest cubevis/tests/manual/visplot/test_flag_filters_curated.py -v

Sections
--------
1. Filters alone           on hand-made filter datasets (no data files)
2. Panel                   the curated list, controls and help
3. Plotter, both backends  boxes with each filter on a simulated MS with
                           planted spikes; what is flagged is read back
                           from the flags themselves

The simulated data: 5 antennas, 24 integrations of 8 s in three scans of
8, 16 channels, XX and YY.  Amplitude = 10 + 0.02 * integration (a
slow gain drift a neighbour test must NOT take) + 0.01 * baseline number
+ noise of 0.05 rms, with spikes of +40 at known (integration, baseline,
channel) places.

What is NOT covered: anything that only happens in a browser.
"""
from __future__ import annotations

import asyncio

import numpy as np
import pytest
import xarray as xr

from cubevis.toolbox.visplot import flag_filters as ff
from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot import flag_engine as fe

NTIME, NANT, NCHAN, DUMP, PER_SCAN = 24, 5, 16, 8.0, 8
# (integration, antenna1, antenna2, channel) of the planted spikes
SPIKES = ((5, 0, 1, 3), (13, 1, 2, 10), (20, 2, 4, 7))
SPIKE = 40.0


# ---------------------------------------------------------------------------
# 1. Filters alone
# ---------------------------------------------------------------------------

def _ds(amp, flag=None, scans=None, phase_deg=0.0):
    """Filter-contract dataset: one baseline, one correlation."""
    amp = np.asarray(amp, dtype=float)
    nt, nf = amp.shape
    vis = (amp * np.exp(1j * np.radians(phase_deg)))[:, None, :, None]
    flag = np.zeros(vis.shape, bool) if flag is None else np.asarray(flag)[:, None, :, None]
    coords = {"time": ("time", 100.0 + 8.0 * np.arange(nt)),
              "baseline_id": ("baseline_id", [0]),
              "baseline_antenna1_name": ("baseline_id", ["A"]),
              "baseline_antenna2_name": ("baseline_id", ["B"]),
              "frequency": ("frequency", 1e9 + 1e6 * np.arange(nf)),
              "channel": ("frequency", np.arange(nf)),
              "polarization": ("polarization", ["XX"])}
    if scans is not None:
        coords["scan_name"] = ("time", np.asarray(scans).astype(str))
    dims = ("time", "baseline_id", "frequency", "polarization")
    return xr.Dataset({"vis": (dims, vis), "amp": (dims, np.abs(vis)),
                       "phase": (dims, np.degrees(np.angle(vis))),
                       "real": (dims, vis.real), "imag": (dims, vis.imag),
                       "flag": (dims, flag),
                       "valid": (("time", "baseline_id"), np.ones((nt, 1), bool))},
                      coords=coords)


class TestFiltersAlone:

    def test_value_range_bounds(self):
        ds = _ds(np.arange(10.0)[:, None] * np.ones((1, 2)))
        f = ff.BUILTIN_FILTERS["value_range"]
        def m(**p):
            return f.mask(ds, None, f.resolve_params(p))[:, 0, 0, 0]
        assert m(low=7.0).tolist() == [False] * 7 + [True] * 3
        assert m(high=2.0).tolist() == [True] * 3 + [False] * 7
        assert m(low=3.0, high=5.0).tolist() == [False] * 3 + [True] * 3 + [False] * 4
        assert m().all()                                   # no bounds: everything

    def test_value_range_on_phase_is_in_degrees(self):
        ds = _ds(np.ones((4, 3)), phase_deg=120.0)
        f = ff.BUILTIN_FILTERS["value_range"]
        p = dict(f.resolve_params({"low": 100.0}), quantity="PHASE")
        assert f.mask(ds, None, p).all()
        p = dict(f.resolve_params({"low": 130.0}), quantity="PHASE")
        assert not f.mask(ds, None, p).any()

    def test_value_range_refuses_a_derived_quantity(self):
        f = ff.BUILTIN_FILTERS["value_range"]
        p = dict(f.resolve_params({"low": 1.0}), quantity="PHASE_RMS")
        with pytest.raises(ValueError, match="Amplitude, Phase, Real or Imaginary"):
            f.mask(_ds(np.ones((3, 3))), None, p)

    def test_outlier_takes_a_spike_not_a_drift(self):
        # a drift of 0.6 noise rms per integration, faster than real gains
        a = 10.0 + 0.03 * np.arange(24)[:, None] * np.ones((1, 8))
        a += np.random.default_rng(1).normal(0, 0.05, a.shape)
        a[11, 4] += 5.0
        ds = _ds(a, scans=np.repeat([1, 2, 3], 8))
        f = ff.BUILTIN_FILTERS["outlier"]
        p = f.resolve_params({"along": "time"})
        got = f.mask(ds, f.prepare([ds], p), p)[:, 0, :, 0]
        assert np.argwhere(got).tolist() == [[11, 4]]

    def test_outlier_along_channel(self):
        a = 10.0 + 0.03 * np.arange(32)[None, :] * np.ones((6, 1))  # a sloping band
        a += np.random.default_rng(2).normal(0, 0.05, a.shape)
        a[2, 17] += 4.0
        ds = _ds(a)
        f = ff.BUILTIN_FILTERS["outlier"]
        p = f.resolve_params({"along": "channel"})
        got = f.mask(ds, f.prepare([ds], p), p)[:, 0, :, 0]
        assert np.argwhere(got).tolist() == [[2, 17]]

    def test_outlier_ignores_flagged_samples(self):
        a = 10.0 + np.random.default_rng(3).normal(0, 0.05, (24, 4))
        a[6, 1] += 8.0
        fl = np.zeros(a.shape, bool); fl[6, 1] = True
        ds = _ds(a, flag=fl)
        f = ff.BUILTIN_FILTERS["outlier"]
        p = f.resolve_params({})
        assert not f.mask(ds, f.prepare([ds], p), p).any()

    def test_neighbours_stop_at_a_scan_boundary(self):
        segs = ff._segments(_ds(np.ones((6, 2)), scans=[1, 1, 2, 2, 2, 3]), "time")
        assert [s.tolist() for s in segs] == [[0, 1], [2, 3, 4], [5]]
        assert [s.tolist() for s in ff._segments(_ds(np.ones((3, 4))), "frequency")] == [[0, 1, 2, 3]]

    def test_grow(self):
        fl = np.zeros((10, 8), bool); fl[4, 3] = True
        ds = _ds(np.ones(fl.shape), flag=fl, scans=[1] * 5 + [2] * 5)
        f = ff.BUILTIN_FILTERS["grow"]
        def g(**kw):
            p = f.resolve_params(kw)
            return np.argwhere(f.mask(ds, f.prepare([ds], p), p)[:, 0, :, 0]).tolist()
        assert g(along="time") == [[3, 3]]                 # 5 is in the next scan
        assert g(along="channel") == [[4, 2], [4, 4]]
        assert g(along="both") == [[3, 3], [4, 2], [4, 4]]
        assert g(along="channel", width=2) == [[4, 1], [4, 2], [4, 4], [4, 5]]

    def test_neighbours_outside_the_box_are_used(self):
        # the box holds one integration; its neighbours come from the
        # reference (the whole selection)
        a = 10.0 + np.random.default_rng(4).normal(0, 0.05, (24, 4))
        a[9, 2] += 6.0
        whole = _ds(a)
        box = whole.isel(time=[9])
        f = ff.BUILTIN_FILTERS["outlier"]
        p = f.resolve_params({})
        got = f.mask(box, f.prepare([whole], p), p)[:, 0, :, 0]
        assert np.argwhere(got).tolist() == [[0, 2]]

    def test_at_most_two_controls_and_no_reference_choice(self):
        r = ff.FilterRegistry()
        for n in r.gui_names():
            shown = [s for s in r.get(n).params if s.gui]
            assert len(shown) <= 2, n
            assert "reference" not in [s.name for s in shown], n

    def test_curated_list_and_the_old_filter_kept(self):
        r = ff.FilterRegistry({"mine": lambda ds, level=1.0: ds["amp"].values > level})
        assert r.gui_names() == ["all", "value_range", "outlier", "zscore", "amplitude_mad",
                                 "phase_deviation", "grow", "mine"]
        assert "amplitude_range" in r and not r.get("amplitude_range").gui


# ---------------------------------------------------------------------------
# Simulated data
# ---------------------------------------------------------------------------

def _transform(desc, data):
    dims, vis = data["DATA"]
    td, t = data["TIME"]
    t = np.asarray(t, dtype=float)
    k = np.round((t - 5.0e9) / DUMP).astype(int)
    a1 = np.asarray(data["ANTENNA1"][1]).astype(int)
    a2 = np.asarray(data["ANTENNA2"][1]).astype(int)
    amp = (10.0 + 0.02 * k + 0.01 * (a1 * 10 + a2))[:, None, None] * np.ones(vis.shape)
    seed = int(desc.DATA_DESC_ID[0]) * 1000 + int(desc.FIELD_ID[0])
    amp = amp + np.random.default_rng(seed).normal(0.0, 0.05, vis.shape)
    for ki, i, j, c in SPIKES:
        rows = (k == ki) & (a1 == i) & (a2 == j)
        amp[rows, c, :] += SPIKE
    data["DATA"] = (dims, amp.astype(np.complex64))
    fd, _ = data["FLAG"]
    data["FLAG"] = (fd, np.zeros(vis.shape, bool))
    return data


@pytest.fixture(scope="module")
def sim_ms(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    from arcae.lib.arrow_tables import Table
    path = str(tmp_path_factory.mktemp("curated") / "c.ms")
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
    out = str(tmp_path_factory.mktemp("curatedps") / "c.ps.zarr")
    dt = xr.open_datatree(sim_ms, engine="xarray-ms:msv2", partition_schema=["FIELD_ID"])
    dt.to_zarr(out, mode="w", compute=True)
    return out


@pytest.fixture(params=["msv2", "msv4"])
def plotter(request, sim_ms, sim_ps):
    from cubevis.toolbox.visplot import VisibilityPlotter
    kw = dict(ms=sim_ms) if request.param == "msv2" else dict(ps=sim_ps)
    vp = VisibilityPlotter(layout="side", correlation="XX,YY",
                           raster_y="TIME", raster_x="CHANNEL", **kw)
    yield vp
    vp.flag_db.clear(record=False)
    vp.close()


def _run(c):
    return asyncio.run(c)


def _times(vp):
    b = vp._reader._backend
    for part in b._iter_visibility_partitions(None):
        return np.sort(np.asarray(part.time.values, dtype=float))


def _flagged(vp):
    """{(integration, 'A&B', channel, correlation)} flagged by the pending flags."""
    b = vp._reader._backend
    t_all = _times(vp)
    out = set()
    for part in b._iter_visibility_partitions(None):
        base = b._flag_mask(part)
        eff = fe.apply_pending(b, part, base, vp.flag_db.deltas())
        f = np.asarray(eff.transpose("time", "baseline_id", "frequency", "polarization").values, bool)
        a1 = part.baseline_antenna1_name.values.astype(str)
        a2 = part.baseline_antenna2_name.values.astype(str)
        pols = [str(p) for p in part.polarization.values]
        order = np.argsort(np.asarray(part.frequency.values, float))
        rank = np.empty_like(order); rank[order] = np.arange(order.size)
        for ti, bi, fi, pi in zip(*np.nonzero(f)):
            k = int(np.argmin(np.abs(t_all - float(part.time.values[ti]))))
            out.add((k, f"{a1[bi]}&{a2[bi]}", int(rank[fi]), pols[pi]))
    return out


def _spikes(pol="XX"):
    return {(k, f"ANTENNA-{i}&ANTENNA-{j}", c, pol) for k, i, j, c in SPIKES}


def _use(vp, name, **params):
    vp.flag_db.clear(record=False)
    vp._flags.filter_name = name
    vp._flags.filter_params[name] = params


def _box(vp, panel, k0, k1, c0=None, c1=None):
    t = _times(vp)
    r = vp._slots[0].raster if panel == "raster" else vp._slots[1].scatter
    if panel == "raster":
        c0 = -0.4 if c0 is None else c0 - 0.4
        c1 = NCHAN - 0.6 if c1 is None else c1 + 0.4
        msg = dict(x0=c0, x1=c1, y0=t[k0] - 1, y1=t[k1] + 1, flag=True)
    else:
        f = r._fig
        msg = dict(x0=f.x_range.start, x1=f.x_range.end, y0=f.y_range.start,
                   y1=f.y_range.end, flag=True)
    return _run(vp._handle_box_select(msg, panel, r))


# ---------------------------------------------------------------------------
# 3. Plotter, both backends
# ---------------------------------------------------------------------------

class TestOnData:

    def test_raster_shows_time_by_channel(self, plotter):
        r = plotter._slots[0].raster
        assert (r._y_dim, r._x_dim) == (Axis.TIME, Axis.CHANNEL)

    def test_outlier_along_time_takes_the_spikes_and_nothing_else(self, plotter):
        vp = plotter
        _use(vp, "outlier", along="time", nsigma=5.0)
        resp = _box(vp, "raster", 0, NTIME - 1)
        assert resp["notify_text"].startswith("✓"), resp["notify_text"]
        assert _flagged(vp) == _spikes("XX")        # the panel shows XX
        d = vp.flag_db.deltas()[-1]
        assert d.filter is not None and d.filter.name == "outlier"

    def test_outlier_along_channel(self, plotter):
        vp = plotter
        _use(vp, "outlier", along="channel", nsigma=5.0)
        _box(vp, "raster", 0, NTIME - 1)
        assert _flagged(vp) == _spikes("XX")

    def test_outlier_box_on_one_integration_uses_its_neighbours(self, plotter):
        vp = plotter
        _use(vp, "outlier", along="time")
        k = SPIKES[1][0]
        resp = _box(vp, "raster", k, k)
        assert resp["notify_text"].startswith("✓"), resp["notify_text"]
        assert _flagged(vp) == {s for s in _spikes("XX") if s[0] == k}

    def test_value_range_in_the_units_shown(self, plotter):
        vp = plotter
        _use(vp, "value_range", low=SPIKE)          # only spikes reach 40+
        _box(vp, "raster", 0, NTIME - 1)
        assert _flagged(vp) == _spikes("XX")
        _use(vp, "value_range", low=SPIKE, high=SPIKE + 100.0)
        _box(vp, "raster", 0, NTIME - 1)
        assert _flagged(vp) == _spikes("XX")
        _use(vp, "value_range", high=5.0)           # every amplitude is about 10
        resp = _box(vp, "raster", 0, NTIME - 1)
        assert resp["notify_text"].startswith("⚠ Nothing to flag"), resp["notify_text"]

    def test_value_range_refused_on_a_derived_quantity(self, plotter):
        vp = plotter
        r = vp._slots[0].raster
        r.update_axes(quantity=Axis.PHASE_RMS)
        try:
            _use(vp, "value_range", low=1.0)
            resp = _box(vp, "raster", 0, NTIME - 1)
            assert "Amplitude, Phase, Real or Imaginary" in resp["notify_text"]
            assert len(vp.flag_db) == 0
        finally:
            r.update_axes(quantity=Axis.AMPLITUDE)

    def test_value_range_on_the_scatter(self, plotter):
        vp = plotter
        _use(vp, "value_range", low=SPIKE)
        resp = _box(vp, "scatter", 0, NTIME - 1)
        assert resp["notify_text"].startswith("✓"), resp["notify_text"]
        got = _flagged(vp)
        assert got == _spikes("XX") | _spikes("YY")

    def test_grow_after_flagging_the_spikes(self, plotter):
        vp = plotter
        _use(vp, "outlier", along="time")
        _box(vp, "raster", 0, NTIME - 1)
        vp._flags.filter_name = "grow"
        vp._flags.filter_params["grow"] = {"along": "channel", "width": 1}
        resp = _box(vp, "raster", 0, NTIME - 1)
        assert resp["notify_text"].startswith("✓"), resp["notify_text"]
        want = set(_spikes("XX"))
        for k, b, c, p in _spikes("XX"):
            want |= {(k, b, c - 1, p), (k, b, c + 1, p)}
        assert _flagged(vp) == want
        assert len(vp.flag_db) == 2
        # undo takes back only the grown samples
        vp.flag_db.undo()
        assert _flagged(vp) == _spikes("XX")

    def test_grow_stays_in_the_box(self, plotter):
        vp = plotter
        _use(vp, "outlier", along="time")
        _box(vp, "raster", 0, NTIME - 1)
        vp._flags.filter_name = "grow"
        vp._flags.filter_params["grow"] = {"along": "time", "width": 1}
        k, i, j, c = SPIKES[0]
        _box(vp, "raster", k + 1, k + 1)            # only the integration after
        assert _flagged(vp) == _spikes("XX") | {(k + 1, f"ANTENNA-{i}&ANTENNA-{j}", c, "XX")}


# ---------------------------------------------------------------------------
# 2. Panel
# ---------------------------------------------------------------------------

class TestPanel:

    @pytest.fixture
    def vp(self, sim_ms):
        from cubevis.toolbox.visplot import VisibilityPlotter
        vp = VisibilityPlotter(ms=sim_ms)
        vp._build_layout()
        yield vp
        vp.close()

    def test_filter_list_is_the_curated_one(self, vp):
        sel = vp._flags._widgets["filter"]
        assert [o[0] for o in sel.options] == ["all", "value_range", "outlier", "zscore",
                                               "amplitude_mad", "phase_deviation", "grow"]

    def test_no_reference_control_and_bounds_start_empty(self, vp):
        from bokeh.models import NumericInput, Select
        titles = [getattr(m, "title", None) for m in vp._sidebar_col.references()
                  if isinstance(m, (NumericInput, Select))]
        assert "Reference population" not in titles
        lows = [m for m in vp._sidebar_col.references()
                if isinstance(m, NumericInput) and list(m.tags or [])[:1] in (["low"], ["high"])
                and m.title in ("Low", "High")]
        assert {m.title for m in lows} == {"Low", "High"} and all(m.value is None for m in lows)

    def test_each_filter_has_status_area_help(self, vp):
        for n in vp._flags.registry.gui_names():
            h = getattr(vp, f"_hint_flagf_{n}").text
            assert h.startswith("<b>"), n
        assert "running median" in vp._hint_flagf_outlier.text
        assert "CASA" in vp._hint_flagf_grow.text
        assert "empty for no lower" in vp._hint_flagf_value_range.text
