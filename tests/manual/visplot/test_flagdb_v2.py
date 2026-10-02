"""
test_flagdb_v2.py
=================
FlagDB v2: model, evaluator, filters, FlagDB history, exporters, and the
backend integration (pending flags applied in ``_flag_mask``; box/filter
requests resolved by ``evaluate_flag_request``).

Runs without a real observation: the backend tests build a small,
deterministic MSv2 with xarray-ms's own simulator (two spectral windows of
8 and 4 channels, both *named* ``<Unknown>`` -- the name-only, non-unique
identity case), then drive the real ``MSv2Backend``.  Every result is
checked against an independent numpy computation on the raw arrays.

Assumes ``cubevis`` is importable (installed or on ``PYTHONPATH``); it
needs no MS/PS environment variables and no remote kernel.

    pytest -q test_flagdb_v2.py
"""
import json
import math
import warnings

import numpy as np
import pytest

from cubevis.toolbox.visplot.flag_model import (
    BlockCoords, FlagCounts, FlagDelta, SampleBlock, SpwChannels, SpwKey,
    fold_deltas, delta_mask,
)
from cubevis.toolbox.visplot.flag_db import FlagDB
from cubevis.toolbox.visplot import flag_filters as ff

warnings.filterwarnings("ignore")

K8 = SpwKey("w", "name", 1.0e9, 1.7e9, 8)
K4 = SpwKey("w", "name", 1.0e9, 1.3e9, 4)


def _bc(nt=5, nb=3, nf=8, pols=("XX", "YY"), key=K8):
    times = 100.0 + 10.0 * np.arange(nt)
    ants = [("A", "B"), ("A", "C"), ("B", "C")][:nb]
    freqs = np.linspace(key.freq_min, key.freq_max, key.n_chan)[:nf]
    return BlockCoords(times, np.array([a for a, _ in ants]), np.array([b for _, b in ants]),
                       freqs, np.array(pols), key, np.arange(nf),
                       np.array(["1"] * 2 + ["2"] * (nt - 2)), np.array(["F"] * nt))


# ====================================================================== #
# Model / evaluator                                                        #
# ====================================================================== #

def test_region_mask_matches_numpy_reference():
    bc = _bc()
    d = FlagDelta(time_range=(110, 130), baseline_ids=[("C", "A")], correlation=["YY"],
                  spw_channels=[SpwChannels(K8, 2, 4)])
    m = delta_mask(d, bc)
    ref = np.zeros(bc.shape, bool)
    ref[1:4, 1, 2:5, 1] = True          # (A,C) given reversed must still match
    assert np.array_equal(m, ref)


def test_region_spw_key_is_exact_not_name_only():
    bc4 = _bc(nf=4, key=K4)
    assert delta_mask(FlagDelta(spw=[K8]), bc4) is None
    assert delta_mask(FlagDelta(spw=[K4]), bc4).all()


def test_ordered_fold_later_unflag_wins_and_padding_untouched():
    bc = _bc()
    base = np.zeros(bc.shape, bool)
    base[0] = True                                  # committed flags
    valid = np.ones(bc.shape[:2], bool)
    valid[4, 2] = False                             # padding slot
    base[4, 2] = True
    deltas = [FlagDelta(flag=True, time_range=(110, 140)),
              FlagDelta(flag=False, time_range=(100, 120), correlation=["XX"]),
              FlagDelta(flag=True, time_range=(120, 120))]
    out = fold_deltas(deltas, bc, base, valid)
    ref = base.copy()
    t = bc.times
    for d in deltas:
        mt = (t >= d.time_range[0]) & (t <= d.time_range[1])
        mp = np.ones(2, bool) if d.correlation is None else (bc.pols == "XX")
        m = mt[:, None, None, None] & mp[None, None, None, :]
        m = m & valid[:, :, None, None]
        ref[np.broadcast_to(m, ref.shape)] = d.flag
    assert np.array_equal(out, ref)
    assert out[4, 2].all()            # padding keeps its (flagged) base value
    assert not out[0, :, :, 0].any()  # unflag of committed XX flags at t=100


def test_extend_options_on_region():
    bc = _bc()
    d = FlagDelta(correlation=["XX"], spw_channels=[SpwChannels(K8, 0, 0)],
                  extend_corr=True, extend_chan=True, time_range=(100, 100))
    m = delta_mask(d, bc)
    assert m[0].all() and not m[1:].any()


def test_sample_block_roundtrip_both_encodings_and_chunk_independence():
    k64 = SpwKey("w", "name", 1.0e9, 1.7e9, 64)
    bc = _bc(nt=64, nf=64, key=k64)
    rng = np.random.default_rng(3)
    diag = np.zeros(bc.shape, bool)
    for i in range(64):
        diag[i, i % 3, i, i % 2] = True          # sparse even after trimming
    for mask, enc in ((diag, "indices"), (rng.random(bc.shape) < 0.6, "bits")):
        blk = SampleBlock.from_mask(k64, bc.times, bc.ant1, bc.ant2, bc.freqs, bc.chans, bc.pols, mask)
        assert blk.encoding == enc
        d = FlagDelta(samples=[blk])
        assert np.array_equal(delta_mask(d, bc), mask)
        # any chunking of the data gives the same answer
        pieces = np.zeros_like(mask)
        for st in (slice(0, 20), slice(20, 64)):
            for sf in (slice(0, 30), slice(30, 64)):
                sub = bc.sub(st, slice(None), sf, slice(None))
                m = delta_mask(d, sub)
                if m is not None:
                    pieces[st, :, sf, :] = m
        assert np.array_equal(pieces, mask)
        # JSON round trip
        d2 = FlagDelta.from_dict(json.loads(json.dumps(d.to_dict())))
        assert np.array_equal(delta_mask(d2, bc), mask)


def test_sample_block_extend_chan_corr():
    bc = _bc()
    mask = np.zeros(bc.shape, bool)
    mask[2, 1, 5, 0] = True
    blk = SampleBlock.from_mask(K8, bc.times, bc.ant1, bc.ant2, bc.freqs, bc.chans, bc.pols, mask)
    d = FlagDelta(samples=[blk], extend_chan=True, extend_corr=True)
    m = delta_mask(d, bc)
    assert m[2, 1].all() and m.sum() == bc.shape[2] * bc.shape[3]


def test_v1_keyword_construction_still_works():
    d = FlagDelta(flag=True, time_range=(2.0, 1.0), freq_range=(5.0, 3.0),
                  correlation=["XX"], source="raster_box_flag", comment="c")
    assert d.time_range == (1.0, 2.0) and d.freq_range == (3.0, 5.0)
    assert d.correlation == ("XX",)


def test_counts_breakdown():
    bc = _bc()
    m = np.zeros(bc.shape, bool)
    m[1, 0, :, 1] = True
    c = FlagCounts.from_mask(m, bc, n_selected=100, n_changed=8)
    assert c.n_matched == 8 and c.by_baseline == {"A&B": 8}
    assert c.by_antenna == {"A": 8, "B": 8} and c.by_pol == {"YY": 8}
    assert c.time_span == (110.0, 110.0) and c.by_scan == {"1": 8}


# ====================================================================== #
# FlagDB                                                                   #
# ====================================================================== #

def test_flagdb_undo_redo_clear_and_versions():
    db = FlagDB()
    seen = []
    db.add_listener(seen.append)
    a = db.add(FlagDelta(time_range=(0, 1)))
    b = db.add(FlagDelta(flag=False, time_range=(0, 1)))
    assert [d.seq for d in db.deltas()] == [1, 2]
    db.undo()
    assert db.deltas() == (a,)
    db.redo()
    assert [d.delta_id for d in db.deltas()] == [a.delta_id, b.delta_id]
    db.clear()
    assert len(db) == 0
    db.undo()
    assert len(db) == 2
    db.redo(); db.undo()
    db.add(FlagDelta())
    assert not db.can_redo()
    assert seen == sorted(seen) and len(set(seen)) == len(seen)


def test_flagdb_jsonl_roundtrip():
    db = FlagDB()
    bc = _bc()
    m = np.zeros(bc.shape, bool); m[0, 0, 0, 0] = True
    blk = SampleBlock.from_mask(K8, bc.times, bc.ant1, bc.ant2, bc.freqs, bc.chans, bc.pols, m)
    db.add(FlagDelta(samples=[blk], filter=ff.ZSCORE.record(ff.ZSCORE.resolve_params({}))))
    db.add(FlagDelta(flag=False, spw=[K8], baseline_ids=[("A", "B")]))
    db2 = FlagDB.from_jsonl(db.to_jsonl({"ms": "x"}))
    for x, y in zip(db.deltas(), db2.deltas()):
        assert x.to_dict() == y.to_dict()


def test_flagdb_commit_clears_only_after_success():
    class Bad:
        def commit_flags(self, deltas):
            raise RuntimeError("nope")

    class Good:
        def commit_flags(self, deltas):
            self.got = deltas
            return "summary"
    db = FlagDB(); db.add(FlagDelta())
    with pytest.raises(RuntimeError):
        db.commit(Bad())
    assert len(db) == 1
    g = Good()
    assert db.commit(g) == "summary" and len(db) == 0 and len(g.got) == 1


# ====================================================================== #
# Filters                                                                  #
# ====================================================================== #

def test_zscore_cell_cutoff_matches_scatter_render():
    from cubevis.toolbox.visplot.data._scatter_render import zscore_cell_cutoff as ref
    for n in (1, 2, 10, 384, 1e5):
        for c in (3.0, 3.5, 5.0):
            assert math.isclose(ff.zscore_cell_cutoff(n, c), ref(n, c))


def test_registry_rejects_builtin_names_and_wraps_callables():
    reg = ff.FilterRegistry({"high_amp": lambda ds, level=2.0: ds["amp"] > level})
    assert "high_amp" in reg and reg.get("high_amp").builtin is False
    with pytest.raises(ValueError):
        ff.FilterRegistry({"zscore": lambda ds: True})
    with pytest.raises(ValueError):
        ff.ZSCORE.resolve_params({"cutoff": -1})
    with pytest.raises(ValueError):
        ff.ZSCORE.resolve_params({"nonsense": 1})


def test_param_specs_describe_for_gui():
    d = ff.AMPLITUDE_RANGE.describe()
    assert [p["name"] for p in d["params"]] == ["low", "high", "mode"]
    assert [p["name"] for p in ff.ZSCORE.describe()["params"] if p["gui"]] == \
        ["cutoff", "granularity", "reference"]


# ====================================================================== #
# Export                                                                   #
# ====================================================================== #

def test_jsonl_is_the_only_export(tmp_path):
    """flagdata command export was removed (2026-09-30): only JSON Lines."""
    from cubevis.toolbox.visplot import flag_export
    assert not hasattr(flag_export, "to_flagdata_lines")
    db = FlagDB()
    db.add(FlagDelta(antenna_names=["A"], time_range=(1.0, 2.0)))
    text = flag_export.to_jsonl(db.deltas(), {"source": "x"})
    header, deltas = FlagDB.parse_jsonl(text)
    assert header["source"] == "x" and len(deltas) == 1

# ====================================================================== #
# Backend integration (real MSv2Backend on a simulated MS)                  #
# ====================================================================== #

OUTLIER_AMP = 50.0


def _transform(desc, data):
    ddid = int(desc.DATA_DESC_ID.item())
    rng = np.random.default_rng(1000 + ddid * 17 + int(desc.chunk_id))
    dims, vis = data["DATA"]
    shape = vis.shape
    v = (1.0 + 0.1 * rng.standard_normal(shape)) + 1j * (0.1 * rng.standard_normal(shape))
    for k in range(0, shape[0], 7):
        v[k, k % shape[1], 0] = OUTLIER_AMP
    data["DATA"] = (dims, v.astype(np.complex64))
    fdims, _ = data["FLAG"]
    f = np.zeros(shape, dtype=bool)
    f[0, :, 0] = True
    data["FLAG"] = (fdims, f)
    return data


@pytest.fixture(scope="module")
def backend(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("ms") / "flag_test.ms")
    sim.MSStructureSimulator(
        ntime=10, nantenna=5, auto_corrs=False,
        data_description=[(8, ["XX", "XY", "YX", "YY"]), (4, ["XX", "YY"])],
        simulate_data=True, transform_data=_transform).simulate_ms(path)
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    b = MSv2Backend(path)
    b.open()
    yield b
    b.close()


def _parts(b):
    return list(b._iter_visibility_partitions())


def _raw(ds):
    return (np.asarray(ds["VISIBILITY"].transpose("time", "baseline_id", "frequency", "polarization").values),
            np.asarray(ds["FLAG"].transpose("time", "baseline_id", "frequency", "polarization").values, bool))


def _eval(b, **req):
    from cubevis.toolbox.visplot.selection import SelectionSpec
    req.setdefault("selection", SelectionSpec())
    req.setdefault("flag", True)
    r = b.evaluate_flag_request(req)
    d = FlagDelta.from_dict(r["delta"]) if r["delta"] is not None else None
    return d, r


def test_spw_keys_distinguish_same_named_windows(backend):
    from cubevis.toolbox.visplot import flag_engine as fe
    keys = [k for k, _ in fe.spw_table(backend)]
    assert len(keys) == 2 and {k.n_chan for k in keys} == {8, 4}
    ids = backend.spw_casa_ids()
    assert sorted(ids.values()) == [0, 1]


def test_raster_box_is_exact_region_and_applies_everywhere(backend):
    p0, p1 = _parts(backend)
    t = p0.time.values
    d, r = _eval(backend, kind="raster", x_axis="TIME", x0=t[2] - 1, x1=t[4] + 1,
                 y_axis="BASELINE", y0=0.6, y1=2.4, polarization="XX")
    assert not d.is_sample_set and d.correlation == ("XX",)
    assert d.time_range == (t[2], t[4])
    # reference: rows 2..4, baseline ids 1..2, all channels, XX, both windows
    tot = 0
    for p in (p0, p1):
        _v, f0 = _raw(p)
        ref = np.zeros(f0.shape, bool)
        pols = list(p.polarization.values)
        ref[2:5, 1:3, :, pols.index("XX")] = True
        backend.set_pending_flags([d], 1)
        eff = backend._flag_mask(p).transpose("time", "baseline_id", "frequency", "polarization").values
        assert np.array_equal(eff, f0 | ref)
        tot += int((ref & ~f0).sum())
    assert r["counts"]["n_changed"] == tot
    backend.set_pending_flags([], 0)


def test_raster_channel_box_uses_spw_channels(backend):
    d, _ = _eval(backend, kind="raster", x_axis="CHANNEL", x0=1.6, x1=3.4,
                 y_axis="BASELINE", y0=-0.5, y1=9.5, polarization="YY")
    assert not d.is_sample_set
    assert sorted((sc.spw.n_chan, sc.chan_lo, sc.chan_hi) for sc in d.spw_channels) == \
        [(4, 2, 3), (8, 2, 3)]


def test_pending_state_reaches_raster_query_and_unflag_overrides(backend):
    from cubevis.toolbox.visplot.axes import Axis
    from cubevis.toolbox.visplot.selection import SelectionSpec
    p0 = _parts(backend)[0]
    t = p0.time.values
    flag_d, _ = _eval(backend, kind="raster", x_axis="TIME", x0=t[0], x1=t[-1],
                      y_axis="BASELINE", y0=-0.5, y1=9.5, polarization="XX")
    backend.set_pending_flags([flag_d], 7)
    agg, *_ = backend.query_raster(Axis.BASELINE, Axis.TIME, Axis.FLAG,
                                   SelectionSpec(correlation=["XX"], pending_version=7))
    assert np.nanmin(agg.values) == 1.0
    unflag_d, _ = _eval(backend, flag=False, kind="raster", x_axis="TIME", x0=t[0], x1=t[-1],
                        y_axis="BASELINE", y0=-0.5, y1=9.5, polarization="XX")
    backend.set_pending_flags([flag_d, unflag_d], 8)
    agg, *_ = backend.query_raster(Axis.BASELINE, Axis.TIME, Axis.FLAG,
                                   SelectionSpec(correlation=["XX"], pending_version=8))
    assert np.nanmax(agg.values) == 0.0     # the committed XX flags were unflagged too
    backend.set_pending_flags([], 0)


def test_scatter_amplitude_box_matches_numpy(backend):
    t = _parts(backend)[0].time.values
    d, r = _eval(backend, kind="scatter", x_axis="TIME", x0=t[0] - 1, x1=t[-1] + 1,
                 y0=10, y1=100, layers=[{"y_axis": "AMPLITUDE", "polarization": "XX"}])
    assert d.is_sample_set and d.value_ranges[1].axis == "AMPLITUDE"
    backend.set_pending_flags([d], 2)
    for p in _parts(backend):
        v, f0 = _raw(p)
        ip = list(p.polarization.values).index("XX")
        ref = np.zeros(f0.shape, bool)
        ref[..., ip] = (np.abs(v[..., ip]) >= 10) & ~f0[..., ip]
        eff = backend._flag_mask(p).transpose("time", "baseline_id", "frequency", "polarization").values
        assert np.array_equal(eff, f0 | ref)
    backend.set_pending_flags([], 0)
    assert r["counts"]["n_matched"] == 28


def test_scatter_box_respects_hidden_categories(backend):
    t = _parts(backend)[0].time.values
    d, r = _eval(backend, kind="scatter", x_axis="TIME", x0=t[0] - 1, x1=t[-1] + 1,
                 y0=10, y1=100,
                 layers=[{"y_axis": "AMPLITUDE", "polarization": "XX",
                          "hide_axis": "ANTENNA1", "hide_values": ["ANTENNA-0"]}])
    assert d is not None
    assert all(a != "ANTENNA-0" for blk in d.samples for a in blk.ant1)


def test_zscore_filter_matches_reference_formula(backend):
    from cubevis.toolbox.visplot.data._scatter_render import compute_baseline_zscore
    parts = _parts(backend)
    t = parts[0].time.values
    cutoff = 6.0
    d, r = _eval(backend, kind="raster", x_axis="TIME", x0=t[0], x1=t[-1],
                 y_axis="BASELINE", y0=-0.5, y1=9.5, polarization="XX",
                 filter={"name": "zscore", "params": {"cutoff": cutoff, "reference": "selection"}})
    backend.set_pending_flags([d], 3)
    # independent: scatter-style reference over the whole selection, per baseline, XX
    re, im, grp, where = [], [], [], []
    for pi, p in enumerate(parts):
        v, f0 = _raw(p)
        ip = list(p.polarization.values).index("XX")
        a1 = p.baseline_antenna1_name.values; a2 = p.baseline_antenna2_name.values
        for it, ib, ic in zip(*np.nonzero(~f0[..., ip])):
            re.append(v[it, ib, ic, ip].real); im.append(v[it, ib, ic, ip].imag)
            grp.append(f"{a1[ib]}&{a2[ib]}"); where.append((pi, it, ib, ic, ip))
    z = compute_baseline_zscore(np.array(re), np.array(im), np.array(grp))
    for pi, p in enumerate(parts):
        _v, f0 = _raw(p)
        ref = f0.copy()
        for (qi, it, ib, ic, ip), zz in zip(where, z):
            if qi == pi and zz > cutoff:
                ref[it, ib, ic, ip] = True
        eff = backend._flag_mask(p).transpose("time", "baseline_id", "frequency", "polarization").values
        assert np.array_equal(eff, ref)
    assert d.filter.name == "zscore"
    backend.set_pending_flags([], 0)


def test_user_filter_and_amplitude_filter(backend):
    t = _parts(backend)[0].time.values
    user = ff.make_flag_filter(lambda ds, level=20.0: ds["amp"] > level, name="loud",
                               params=[ff.ParamSpec("level", "float", 20.0)])
    d1, _ = _eval(backend, kind="raster", x_axis="TIME", x0=t[0], x1=t[-1], y_axis="BASELINE",
                  y0=-0.5, y1=9.5, polarization="XX", filter_obj=user)
    d2, _ = _eval(backend, kind="raster", x_axis="TIME", x0=t[0], x1=t[-1], y_axis="BASELINE",
                  y0=-0.5, y1=9.5, polarization="XX",
                  filter={"name": "amplitude_range", "params": {"low": 20.0}})
    for a, b in zip(d1.samples, d2.samples):
        assert np.array_equal(a.dense(), b.dense())
    assert d1.filter.builtin is False and d1.filter.code_hash


def test_unflag_filter_only_touches_flagged(backend):
    t = _parts(backend)[0].time.values
    d, r = _eval(backend, flag=False, kind="raster", x_axis="TIME", x0=t[0], x1=t[0],
                 y_axis="BASELINE", y0=-0.5, y1=9.5, polarization="XX",
                 filter={"name": "amplitude_range", "params": {"low": 0.0}})
    # only the committed spectrum (row 0 = time 0, baseline 0, corr 0) was flagged
    assert r["counts"]["n_matched"] == 8 + 4


def test_frame_cache_keyed_on_pending_version(backend):
    from cubevis.toolbox.visplot.axes import Axis
    from cubevis.toolbox.visplot.selection import SelectionSpec
    from cubevis.toolbox.visplot.data.reader import _selection_fingerprint
    a = SelectionSpec(pending_version=1)
    b = SelectionSpec(pending_version=2)
    assert _selection_fingerprint(a) == _selection_fingerprint(b)


# ====================================================================== #
# GUI path: VisibilityPlotter box handler, review, display, history        #
# (drives the real handler; no browser needed)                             #
# ====================================================================== #

import asyncio


@pytest.fixture(scope="module")
def plotter(backend):
    from cubevis.toolbox.visplot import VisibilityPlotter
    vp = VisibilityPlotter(
        ms=backend._path, layout="side", correlation="XX,YY",
        flag_filters={"loud": lambda ds, level=20.0: ds["amp"] > level})
    yield vp
    vp.close()


def _run(c):
    return asyncio.run(c)


def _eff(vp, part):
    b = vp._reader._backend
    return b._flag_mask(part).transpose("time", "baseline_id", "frequency", "polarization").values


def test_handler_raster_box_flags_exactly_the_covered_cells(plotter, backend):
    vp = plotter
    vp.flag_db.clear(record=False)
    r = vp._slots[0].raster
    agg = r._agg
    ydim, xdim = agg.dims
    ys, xs = agg.coords[ydim].values, agg.coords[xdim].values
    # a box strictly inside cells [2..4] (y) x [3..5] (x)
    box = dict(x0=float(xs[3]), x1=float(xs[5]), y0=float(ys[2]), y1=float(ys[4]), flag=True)
    resp = _run(vp._handle_box_select(box, "raster", r))
    assert resp["notify_text"].startswith("✓"), resp["notify_text"]
    d = vp.flag_db.deltas()[-1]
    assert not d.is_sample_set and d.correlation == (r._polarization,)
    # numpy reference: every sample whose time and frequency are cell
    # centres inside [x0,x1] x [y0,y1] (overlap semantics add the half-
    # cells on either side, so compare against the box widened by half a
    # cell on the raster's own grids)
    for p in _parts(backend):
        ok_t = np.isin(p.time.values, ys[2:5]) if ydim == "time" else None
        freqs = p.frequency.values
        in_f = (freqs >= xs[3] - 1) & (freqs <= xs[5] + 1)
        _v, f0 = _raw(p)
        ref = f0.copy()
        ip = list(p.polarization.values).index(r._polarization)
        ref[np.ix_(ok_t, np.ones(f0.shape[1], bool), in_f, [ip])] = True
        # padding never changes
        eit = np.isfinite(p.EFFECTIVE_INTEGRATION_TIME.values)
        ref = np.where(eit[:, :, None, None], ref, f0)
        assert np.array_equal(_eff(vp, p), ref)
    assert r._flag_stale and vp._slots[1].scatter._flag_stale


def test_handler_scatter_box_and_review_accept_reject(plotter):
    vp = plotter
    vp.flag_db.clear(record=False)
    sc = vp._slots[1].scatter
    x0, x1 = sc._x_range
    _run(vp.flags.handle_action({"action": "config", "preview": True}))
    resp = _run(vp._handle_box_select(dict(x0=x0, x1=x1, y0=10, y1=100, flag=True), "scatter", sc))
    assert "preview" in resp and len(vp.flag_db) == 0
    assert sc._flag_overlays and sc._flag_overlays[-1][0] == "proposal"
    before = vp.flag_db.version
    resp = _run(vp.flags.handle_action({"action": "reject"}))
    assert resp.get("preview_closed") and len(vp.flag_db) == 0 and vp.flag_db.version == before
    _run(vp._handle_box_select(dict(x0=x0, x1=x1, y0=10, y1=100, flag=True), "scatter", sc))
    resp = _run(vp.flags.handle_action({"action": "accept"}))
    assert len(vp.flag_db) == 1 and vp.flag_db.deltas()[0].is_sample_set
    assert vp.flag_db.deltas()[0].n_samples == 28          # both XX outliers, both windows
    _run(vp.flags.handle_action({"action": "config", "preview": False}))


def test_handler_user_filter_undo_redo_clear(plotter):
    vp = plotter
    vp.flag_db.clear(record=False)
    r = vp._slots[0].raster
    x0, x1 = r._x_range; y0, y1 = r._y_range
    _run(vp.flags.handle_action({"action": "config", "filter": "loud", "params": {"level": 20.0}}))
    _run(vp._handle_box_select(dict(x0=x0, x1=x1, y0=y0, y1=y1, flag=True), "raster", r))
    d = vp.flag_db.deltas()[0]
    assert d.filter.name == "loud" and not d.filter.builtin
    assert d.n_samples == 28                                # XX outliers, both windows
    for action, n in (("undo", 0), ("redo", 1), ("clear", 0), ("undo", 1)):
        _run(vp.flags.handle_action({"action": action}))
        assert len(vp.flag_db) == n
    _run(vp.flags.handle_action({"action": "config", "filter": "all"}))


def test_display_modes_switch_views_and_overlays(plotter):
    vp = plotter
    sc = vp._slots[1].scatter
    _run(vp.flags.handle_action({"action": "config", "display": "color", "color": "#00ff00"}))
    assert sc._selection.flag_view == "disk"
    assert sc._flag_overlays and sc._flag_overlays[0][0] == "pending"
    _run(vp.flags.handle_action({"action": "config", "display": "hide"}))
    assert sc._selection.flag_view == "effective" and not sc._flag_overlays


def test_flag_views_on_backend(backend):
    p0 = _parts(backend)[0]
    t = p0.time.values
    d, _ = _eval(backend, kind="raster", x_axis="TIME", x0=t[3], x1=t[3],
                 y_axis="BASELINE", y0=-0.5, y1=9.5, polarization="XX")
    backend.set_pending_flags([d], 11)
    _v, f0 = _raw(p0)
    with backend.flag_view("disk"):
        assert np.array_equal(backend._flag_mask(p0).transpose(*f0.dims if hasattr(f0, "dims") else ("time", "baseline_id", "frequency", "polarization")).values, f0)
    with backend.flag_view("pending"):
        shown = ~backend._flag_mask(p0).transpose("time", "baseline_id", "frequency", "polarization").values
    assert shown[3, :, :, 0].all() and shown.sum() == shown[3, :, :, 0].size
    backend.set_pending_flags([], 0)


def test_reload_discards_pending(plotter):
    vp = plotter
    vp.flag_db.add(FlagDelta(time_range=(0, 1)))
    vp.flags.reset()
    assert len(vp.flag_db) == 0 and not vp.flag_db.can_undo()


# ====================================================================== #
# InfoTool box == FlagTool box                                             #
# ====================================================================== #

def test_infotool_box_reports_exactly_what_flag_would_flag(plotter):
    vp = plotter
    vp.flag_db.clear(record=False)
    sc = vp._slots[1].scatter
    x0, x1 = sc._x_range
    box = dict(x0=x0, x1=x1, y0=10, y1=100)
    from cubevis.toolbox.visplot.flag_controls import scatter_layer_entries
    req = dict(selection=sc._selection, x_axis=sc._x_dim.name,
               layers=scatter_layer_entries(sc), **box)
    probe = vp._reader.probe_flag_region(req)
    per_layer = sum(v["n_samples"] for v in probe["layers"].values())
    resp = _run(vp._handle_box_select(dict(box, flag=True), "scatter", sc))
    d = vp.flag_db.deltas()[-1]
    assert probe["flag_n"] == per_layer == d.n_samples == 28
    # after flagging, the same box shows nothing to flag and everything to unflag
    probe2 = vp._reader.probe_flag_region(req)
    assert probe2["flag_n"] == 0 and probe2["unflag_n"] >= 28
    # the scatter InfoTool handler goes through the same path
    out = _run(sc._handle_probe_region(dict(tool="info_box", x0=x0, x1=x1, y0=10, y1=100)))
    assert "Flag box here:</b> 0" in out["info_html"]
    vp.flag_db.clear(record=False)


def test_flagdb_report_page(plotter):
    vp = plotter
    vp.flag_db.clear(record=False)
    r = vp._slots[0].raster
    x0, x1 = r._x_range; y0, y1 = r._y_range
    _run(vp._handle_box_select(dict(x0=x0, x1=x1, y0=y0, y1=y0 + (y1 - y0) * 0.3, flag=True),
                               "raster", r))
    _run(vp.flags.handle_action({"action": "config", "filter": "loud", "params": {"level": 20.0}}))
    _run(vp._handle_box_select(dict(x0=x0, x1=x1, y0=y0, y1=y1, flag=True), "raster", r))
    resp = _run(vp.flags.handle_action({"action": "report"}))
    page = resp["report_html"]
    assert page.startswith("<!DOCTYPE html>") and "Pending operations</td><td class='cv-v'>2" in page
    import re
    assert len(re.findall(r"<h3>#\d+ — ", page)) == 2 and "loud(level=20.0)" in page
    assert "user, code " in page and "flagdata" not in page
    # the sample-set operation (user filter) is described in detail
    assert "Samples per baseline" in page and "Samples per antenna" in page
    assert "integrations" in page and "channels " in page
    # time spans in UTC and as the MS stores them (TIME column, MJD seconds)
    assert "UTC time span" in page and "MS time span" in page
    assert "MJD seconds, MS TIME column" in page
    _run(vp.flags.handle_action({"action": "config", "filter": "all"}))
    vp.flag_db.clear(record=False)
    assert "No pending flag operations" in _run(vp.flags.handle_action({"action": "report"}))["report_html"]


def test_scatter_redraw_after_flag_needs_one_query(plotter):
    """Flagging outliers shrinks the scatter's data extent while the view
    stays put; the redraw must not pay a second backend query for the
    empty area outside the (full-extent) reference."""
    vp = plotter
    vp.flag_db.clear(record=False)
    sc = vp._slots[1].scatter
    view = dict(x0=sc._x_range[0], x1=sc._x_range[1], y0=0.0, y1=60.0)
    n = {"q": 0}
    orig = sc._backend.query_columns

    def spy(*a, **k):
        n["q"] += 1
        return orig(*a, **k)
    sc._backend.query_columns = spy
    try:
        _run(vp._handle_box_select(dict(view, y0=10, y1=100, flag=True), "scatter", sc))
        n["q"] = 0
        sc._handle_rerender(dict(view))
        assert n["q"] == 1
    finally:
        sc._backend.query_columns = orig
        vp.flag_db.clear(record=False)


# ====================================================================== #
# Cached raw scatter frames: row-wise flag views == a fresh read          #
# ====================================================================== #

def _xy(df):
    if len(df) == 0:
        return np.zeros((0, 2))
    cols = ["x", "y"] if "y" in df.columns else ["x", "zscore"] if "zscore" in df.columns else list(df.columns[:2])
    a = df[cols].to_numpy(dtype=np.float64)
    return a[np.lexsort(a.T[::-1])]


@pytest.mark.parametrize("yaxis", ["AMPLITUDE", "PHASE", "Z_SCORE"])
def test_raw_frame_views_match_fresh_read(backend, yaxis):
    from cubevis.toolbox.visplot.axes import Axis
    from cubevis.toolbox.visplot.selection import SelectionSpec
    from cubevis.toolbox.visplot.data.reader import _FlagViewContext
    t = _parts(backend)[0].time.values
    region, _ = _eval(backend, kind="raster", x_axis="TIME", x0=t[2], x1=t[5],
                      y_axis="BASELINE", y0=0.6, y1=3.4, polarization="XX")
    samples, _ = _eval(backend, kind="scatter", x_axis="TIME", x0=t[0] - 1, x1=t[-1] + 1,
                       y0=10, y1=100, layers=[{"y_axis": "AMPLITUDE", "polarization": "YY"},
                                              {"y_axis": "AMPLITUDE", "polarization": "XX"}])
    unflag, _ = _eval(backend, flag=False, kind="raster", x_axis="TIME", x0=t[0], x1=t[3],
                      y_axis="BASELINE", y0=-0.5, y1=9.5, polarization="XX")
    ext = FlagDelta(correlation=["YY"], time_range=(t[7], t[7]), extend_corr=True,
                    extend_chan=True, baseline_ids=[("ANTENNA-0", "ANTENNA-1")])
    keys = [(Axis[yaxis], "XX"), (Axis[yaxis], "YY")]
    states = [([], None), ([region], samples), ([region, samples, unflag], ext),
              ([region, samples, unflag, ext], None)]
    for i, (deltas, prop) in enumerate(states):
        backend.set_pending_flags(deltas, 100 + i, True, prop)
        sel = SelectionSpec(pending_version=100 + i)
        for view in ("effective", "disk", "pending", "proposal", "flagged"):
            with _FlagViewContext(view):
                got = backend._query_columns_cached(Axis.TIME, keys, sel)
                want = backend._query_columns_raw(Axis.TIME, keys, sel)
            for k in keys:
                assert "__disk_flag" not in got[k].columns
                g, w = _xy(got[k]), _xy(want[k])
                assert g.shape == w.shape, (yaxis, i, view, k, g.shape, w.shape)
                assert np.allclose(g, w, equal_nan=True), (yaxis, i, view, k)
    backend.set_pending_flags([], 0)


def test_raw_frames_are_not_reread_on_flag_change(backend):
    from cubevis.toolbox.visplot.axes import Axis
    from cubevis.toolbox.visplot.selection import SelectionSpec
    keys = [(Axis.AMPLITUDE, "XX")]
    backend._query_columns_cached(Axis.TIME, keys, SelectionSpec(pending_version=1))
    n = {"reads": 0}
    orig = backend._query_columns_raw

    def spy(*a, **k):
        n["reads"] += 1
        return orig(*a, **k)
    backend._query_columns_raw = spy
    try:
        t = _parts(backend)[0].time.values
        d, _ = _eval(backend, kind="raster", x_axis="TIME", x0=t[1], x1=t[2],
                     y_axis="BASELINE", y0=-0.5, y1=9.5, polarization="XX")
        backend.set_pending_flags([d], 2)
        backend._query_columns_cached(Axis.TIME, keys, SelectionSpec(pending_version=2))
        assert n["reads"] == 0
    finally:
        backend._query_columns_raw = orig
        backend.set_pending_flags([], 0)


def test_incremental_view_state_matches_uncached(backend):
    """The per-frame effective-state cache (prefix reuse for add / undo /
    redo) must give exactly what a from-scratch fold gives."""
    from cubevis.toolbox.visplot import flag_engine as fe
    from cubevis.toolbox.visplot.data.reader import ScatterLayerSpec
    t = _parts(backend)[0].time.values
    a, _ = _eval(backend, kind="raster", x_axis="TIME", x0=t[1], x1=t[3],
                 y_axis="BASELINE", y0=-0.5, y1=9.5, polarization="XX")
    b, _ = _eval(backend, kind="scatter", x_axis="TIME", x0=t[0] - 1, x1=t[-1] + 1,
                 y0=10, y1=100, layers=[{"y_axis": "AMPLITUDE", "polarization": "XX"}])
    c, _ = _eval(backend, flag=False, kind="raster", x_axis="TIME", x0=t[2], x1=t[2],
                 y_axis="BASELINE", y0=-0.5, y1=9.5, polarization="XX")
    from cubevis.toolbox.visplot.selection import SelectionSpec
    backend.set_pending_flags([], 0)
    layer = [ScatterLayerSpec(y_axis=Axis_AMP(), polarization="XX", cmap=("#000000", "#ffffff"))]
    backend.query_columns(Axis_TIME(), layer, SelectionSpec(), width=100, height=100)
    frames = [v for v in _raw_frames(backend)]
    assert frames, "no raw frame cached"
    df = frames[0]
    for i, seq in enumerate(([a], [a, b], [a], [a, b, c], [a, b], [])):
        backend.set_pending_flags(seq, 100 + i)
        got = fe.frame_keep_mask(backend, df, "XX", "effective")
        fe._FRAME_SLOTS.pop(id(df), None)                 # from scratch
        want = fe.frame_keep_mask(backend, df, "XX", "effective")
        assert np.array_equal(got, want), seq
    backend.set_pending_flags([], 0)


def Axis_AMP():
    from cubevis.toolbox.visplot.axes import Axis
    return Axis.AMPLITUDE


def Axis_TIME():
    from cubevis.toolbox.visplot.axes import Axis
    return Axis.TIME


def _raw_frames(backend):
    """Raw frames currently held by the backend's frame cache."""
    cache = backend._frame_cache_obj()
    store = getattr(cache, "_entries", None) or getattr(cache, "_d", None) or {}
    for v in list(store.values()):
        df = v[-1] if isinstance(v, tuple) else v
        if hasattr(df, "columns") and "__disk_flag" in df.columns:
            yield df


def test_shared_binning_is_bit_identical(backend):
    """render_layer() and build_layer_reference() share the id grid and, at
    equal resolution, the (x, y) mean aggregation: the result must be
    bit-identical to computing each separately."""
    from cubevis.toolbox.visplot.data import _scatter_render as sr
    from cubevis.toolbox.visplot.data.reader import ScatterLayerSpec
    from cubevis.toolbox.visplot.selection import SelectionSpec
    from cubevis.toolbox.visplot.axes import Axis
    layers = [ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization=p, cmap=("#000000", "#ffffff"))
              for p in ("XX", "YY")]
    kw = dict(width=200, height=150, probe_grid_max_cells=3072, color_mode="global")
    saved = sr._BIN_MEMO_MAX
    try:
        for rs in (1.0, 2.0):
            sr._BIN_MEMO.clear(); sr._BIN_MEMO_MAX = 0
            a = backend.query_columns(Axis.TIME, layers, SelectionSpec(), ref_scale=rs, **kw)
            sr._BIN_MEMO.clear(); sr._BIN_MEMO_MAX = 8
            b = backend.query_columns(Axis.TIME, layers, SelectionSpec(), ref_scale=rs, **kw)
            for x, y in zip(a.layers, b.layers):
                assert np.array_equal(x.image, y.image)
                assert np.array_equal(x.id_grid_value, y.id_grid_value, equal_nan=True)
                assert np.array_equal(np.asarray(x.reference.ref_agg), np.asarray(y.reference.ref_agg), equal_nan=True)
                assert np.array_equal(np.asarray(x.reference.ref_count), np.asarray(y.reference.ref_count))
    finally:
        sr._BIN_MEMO_MAX = saved


def _effect(backend, d):
    backend.set_pending_flags([d], 4242)
    out = [backend._flag_mask(p).transpose("time", "baseline_id", "frequency", "polarization").values
           for p in _parts(backend)]
    backend.set_pending_flags([], 0)
    return out


@pytest.mark.parametrize("case", ["amp_both", "phase_xx", "hidden_ant", "unflag"])
def test_scatter_box_from_frames_matches_ms_path(backend, case):
    """The cached-frame scatter-box path must address exactly the samples the
    MS-reading path does, with the same counts."""
    t = _parts(backend)[0].time.values
    base = dict(kind="scatter", x_axis="TIME", x0=t[0] - 1, x1=t[-1] + 1)
    pre = None
    if case == "amp_both":
        req = dict(base, y0=0.9, y1=100, layers=[{"y_axis": "AMPLITUDE", "polarization": "XX"},
                                                 {"y_axis": "AMPLITUDE", "polarization": "YY"}])
    elif case == "phase_xx":
        req = dict(base, y0=-5, y1=5, layers=[{"y_axis": "PHASE", "polarization": "XX"}])
    elif case == "hidden_ant":
        req = dict(base, y0=0.9, y1=100, layers=[{"y_axis": "AMPLITUDE", "polarization": "XX",
                                                  "hide_axis": "ANTENNA1",
                                                  "hide_values": ["ANTENNA-0", "ANTENNA-2"]}])
    else:   # unflag part of an earlier flag, plus the committed spectrum
        pre, _ = _eval(backend, **dict(base, y0=10, y1=100,
                                       layers=[{"y_axis": "AMPLITUDE", "polarization": "XX"}]))
        req = dict(base, y0=0.0, y1=60, flag=False,
                   layers=[{"y_axis": "AMPLITUDE", "polarization": "XX"}])
    if pre is not None:
        backend.set_pending_flags([pre], 77)
    try:
        fast, rf = _eval(backend, **req)
        slow, rs = _eval(backend, **dict(req, force_ms=True))
    finally:
        backend.set_pending_flags([], 0)
    for k in ("n_matched", "n_changed", "by_baseline", "by_pol", "by_scan", "by_antenna"):
        assert rf["counts"][k] == rs["counts"][k], k
    assert rf["counts"]["time_span"] == rs["counts"]["time_span"]
    if pre is not None:
        seq_f = [pre, fast]; seq_s = [pre, slow]
        backend.set_pending_flags(seq_f, 78)
        a = [backend._flag_mask(p).values for p in _parts(backend)]
        backend.set_pending_flags(seq_s, 79)
        b = [backend._flag_mask(p).values for p in _parts(backend)]
        backend.set_pending_flags([], 0)
    else:
        a, b = _effect(backend, fast), _effect(backend, slow)
    assert all(np.array_equal(x, y) for x, y in zip(a, b))


def test_scatter_box_fast_path_reads_no_visibilities(backend, monkeypatch):
    from cubevis.toolbox.visplot import flag_engine as fe
    t = _parts(backend)[0].time.values
    req = dict(kind="scatter", x_axis="TIME", x0=t[0] - 1, x1=t[-1] + 1, y0=10, y1=100,
               layers=[{"y_axis": "AMPLITUDE", "polarization": "XX"}])
    _eval(backend, **req)                         # warm the frame cache

    def boom(*a, **k):
        raise AssertionError("MS path used")
    monkeypatch.setattr(fe, "_scatter_box", boom)
    monkeypatch.setattr(fe, "_filter_dataset", boom)
    d, r = _eval(backend, **req)
    assert d is not None and r["counts"]["n_matched"] == 28


def test_zoomed_redraw_after_flag_skips_full_extent_reference(plotter):
    """Zoomed in, the redraw after a flag is a single Level-2 query (it
    returns the new full-data extent and global scaling too).  The final
    image must equal what the previous full-then-zoomed path produced."""
    vp = plotter
    vp.flag_db.clear(record=False)
    sc = vp._slots[1].scatter
    fx0, fx1 = sc._x_range; fy0, fy1 = sc._y_range
    zoom = dict(x0=fx0 + (fx1 - fx0) * 0.4, x1=fx0 + (fx1 - fx0) * 0.6,
                y0=fy0, y1=fy0 + (fy1 - fy0) * 0.2)
    calls = []
    orig = sc._backend.query_columns

    def spy(*a, **k):
        calls.append(k.get("ref_scale"))
        return orig(*a, **k)
    sc._backend.query_columns = spy
    try:
        sc._handle_rerender(dict(zoom))
        box = dict(x0=fx0, x1=fx1, y0=10, y1=100, flag=True)
        _run(vp._handle_box_select(box, "scatter", sc))
        calls.clear()
        new = sc._handle_rerender(dict(zoom))["image"].copy()
        assert len(calls) == 1 and calls[0] is not None        # one Level-2 query only
        state_new = (tuple(sc._x_range), tuple(sc._y_range), sc.colorbar_html(),
                     [None if h is None else np.asarray(h).tolist() for h in sc._layer_hist_counts])
        # reference answer: same state, old behaviour (reference on the full re-read)
        sc._flag_stale = True
        sc._prepare_stale_render = lambda *a: False
        old = sc._handle_rerender(dict(zoom))["image"]
        assert np.array_equal(new, old)
        state_old = (tuple(sc._x_range), tuple(sc._y_range), sc.colorbar_html(),
                     [None if h is None else np.asarray(h).tolist() for h in sc._layer_hist_counts])
        assert state_new == state_old        # extent, colour bar, histograms
    finally:
        sc._backend.query_columns = orig
        sc.__dict__.pop("_prepare_stale_render", None)
        vp.flag_db.clear(record=False)


@pytest.mark.parametrize("case", ["amp_both", "hidden_ant", "pending"])
def test_probe_from_frames_matches_ms_path(backend, case):
    t = _parts(backend)[0].time.values
    req = dict(selection=__import__("cubevis.toolbox.visplot.selection", fromlist=["x"]).SelectionSpec(),
               x_axis="TIME", x0=t[0] - 1, x1=t[-1] + 1, y0=0.9, y1=100)
    if case == "hidden_ant":
        req["layers"] = [{"y_axis": "AMPLITUDE", "polarization": "XX", "hide_axis": "ANTENNA1",
                          "hide_values": ["ANTENNA-0"]}]
    else:
        req["layers"] = [{"y_axis": "AMPLITUDE", "polarization": "XX"},
                         {"y_axis": "PHASE", "polarization": "XX"},
                         {"y_axis": "AMPLITUDE", "polarization": "YY"}]
    if case == "pending":
        d, _ = _eval(backend, kind="scatter", x_axis="TIME", x0=t[0] - 1, x1=t[-1] + 1,
                     y0=10, y1=100, layers=[{"y_axis": "AMPLITUDE", "polarization": "XX"}])
        backend.set_pending_flags([d], 91)
    try:
        fast = backend.probe_flag_region(dict(req))
        slow = backend.probe_flag_region(dict(req, force_ms=True))
    finally:
        backend.set_pending_flags([], 0)
    assert fast["flag_n"] == slow["flag_n"] and fast["unflag_n"] == slow["unflag_n"]
    for k in slow["layers"]:
        a, b = fast["layers"][k], slow["layers"][k]
        assert a["n_samples"] == b["n_samples"] and a["status"] == b["status"], k
        for f in ("t_range", "freq_range", "bl_ids", "bl_range"):
            av, bv = a[f], b[f]
            assert (av is None and bv is None) or list(av) == list(bv), (k, f)


def test_flagged_view_shows_flagged_valid_samples_only(backend):
    """The "flagged" view draws exactly the effectively flagged samples and
    never padding."""
    t = _parts(backend)[0].time.values
    d, _ = _eval(backend, kind="raster", x_axis="TIME", x0=t[4], x1=t[5],
                 y_axis="BASELINE", y0=-0.5, y1=9.5, polarization="XX")
    backend.set_pending_flags([d], 31)
    try:
        for p in _parts(backend):
            eff = backend._flag_mask(p).transpose("time", "baseline_id", "frequency", "polarization").values
            valid = np.isfinite(p.EFFECTIVE_INTEGRATION_TIME.values)[:, :, None, None]
            with backend.flag_view("flagged"):
                hidden = backend._flag_mask(p).transpose("time", "baseline_id", "frequency",
                                                         "polarization").values
            assert np.array_equal(~hidden, eff & valid)
    finally:
        backend.set_pending_flags([], 0)


def test_show_flagged_overlay_and_unflag_committed(backend):
    """Show flagged data at construction: the initial page carries the
    overlay; an Unflag box over the committed spectrum restores it."""
    from cubevis.toolbox.visplot import VisibilityPlotter
    vp = VisibilityPlotter(ms=backend._path, layout="side", correlation="XX,YY",
                           flag_show_flagged=True, flag_flagged_color="#102030")
    try:
        sc = vp._slots[1].scatter
        assert sc._flag_overlays and sc._flag_overlays[0][0] == "flagged"
        px = np.frombuffer(bytes([0x10, 0x20, 0x30, int(round(255 * 0.8))]), dtype=np.uint32)[0]
        assert (np.asarray(sc._image_source.data["image"][0]) == px).sum() > 0
        view = dict(x0=sc._x_range[0], x1=sc._x_range[1], y0=0.0, y1=60.0)
        resp = _run(vp._handle_box_select(dict(view, flag=False), "scatter", sc))
        assert resp["notify_text"].startswith("✓ Unflagged: 12 samples")
        img = sc._handle_rerender(dict(view))["image"]
        assert (img == px).sum() == 0
        _run(vp.flags.handle_action({"action": "config", "show_flagged": False}))
        assert not sc._flag_overlays
    finally:
        vp.close()


def test_display_toggles_do_not_reread_drawn_data(plotter):
    """Show flagged data / display switches with nothing pending change only
    overlays: panels must not re-read their main data."""
    from cubevis.toolbox.visplot.axes import Axis
    vp = plotter
    vp.flag_db.clear(record=False)
    r, sc = vp._slots[0].raster, vp._slots[1].scatter
    rv = dict(x0=r._x_range[0], x1=r._x_range[1], y0=r._y_range[0], y1=r._y_range[1])
    sv = dict(x0=sc._x_range[0], x1=sc._x_range[1], y0=sc._y_range[0], y1=sc._y_range[1])
    r._handle_rerender(dict(rv)); sc._handle_rerender(dict(sv))
    calls = []
    orig_r, orig_c = vp._reader.query_raster, vp._reader.query_columns

    def qr(*a, **k):
        calls.append(("raster", k.get("quantity")))
        return orig_r(*a, **k)

    def qc(*a, **k):
        calls.append(("columns", a[2].flag_view if len(a) > 2 else k["selection"].flag_view))
        return orig_c(*a, **k)
    for p in (r, sc):
        p._backend.query_raster, p._backend.query_columns = qr, qc
    try:
        for cfg in ({"show_flagged": True}, {"display": "color"}, {"display": "hide"},
                    {"show_flagged": False}):
            calls.clear()
            _run(vp.flags.handle_action(dict(action="config", **cfg)))
            r._handle_rerender(dict(rv)); sc._handle_rerender(dict(sv))
            main = [c for c in calls if c not in (("raster", Axis.FLAG),)
                    and not (c[0] == "columns" and c[1] in ("flagged", "pending", "proposal"))]
            assert main == [], (cfg, calls)
    finally:
        for p in (r, sc):
            p._backend.query_raster, p._backend.query_columns = orig_r, orig_c
        _run(vp.flags.handle_action(dict(action="config", show_flagged=False, display="hide")))


def test_rerender_comm_handler_runs_off_the_event_loop(plotter):
    import threading
    sc = plotter._slots[1].scatter
    seen = {}
    orig = sc._handle_rerender

    def spy(msg):
        seen["thread"] = threading.current_thread() is threading.main_thread()
        return orig(msg)
    sc._handle_rerender = spy
    try:
        _run(sc._handle_rerender_async(dict(x0=sc._x_range[0], x1=sc._x_range[1],
                                            y0=sc._y_range[0], y1=sc._y_range[1])))
    finally:
        del sc._handle_rerender
    assert seen["thread"] is False
