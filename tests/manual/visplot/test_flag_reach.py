"""
test_flag_reach.py
==================
"Flag reaches" (2026-10-07, HRS H5 slice 1): how far a flag box goes
beyond what is drawn -- to all baselines of an antenna, all selected
spectral windows, the whole scan, all fields -- the reason stored with
each flag, the held-key full-height / full-width box, and the
status-area help of the Flagging panel.

Location in repository:
    cubevis/tests/manual/visplot/test_flag_reach.py

Run:
    pytest cubevis/tests/manual/visplot/test_flag_reach.py -v

Sections
--------
1. Settings            parse_reach, set_reach, reach words (no data)
2. Records             FlagDelta.reason round trip, flagdata lines
3. Engine / plotter    each reach on a simulated MS and its MSv4 twin:
                       what is flagged is read back from the flags
                       themselves, not from the record
4. GUI                 controls, their start-up values, help wrappers
5. Flag tool           the shipped bundle carries the held-key code

The simulated data: 5 antennas (10 baselines), 24 integrations of 8 s in
three scans of 8, two spectral windows of 16 and 8 channels, XX and YY.

What is NOT covered: anything that only happens in a browser -- that
Shift / Alt stretch the box while dragging, that the line under the
controls turns amber, that the help shows on hover.
"""
from __future__ import annotations

import asyncio
import glob
import os

import numpy as np
import pytest
import xarray as xr

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot import flag_engine as fe
from cubevis.toolbox.visplot.flag_controls import (
    FlagController, REACH_BASELINES, parse_reach)
from cubevis.toolbox.visplot.flag_model import FlagDelta, SpwChannels, SpwKey

NTIME, NANT, DUMP, PER_SCAN = 24, 5, 8.0, 8
NCHAN = (16, 8)


# ---------------------------------------------------------------------------
# 1. Settings
# ---------------------------------------------------------------------------

class TestSettings:

    def test_parse_reach_words(self):
        assert parse_reach("") == {} and parse_reach(None) == {}
        assert parse_reach("shared-antenna, scan") == {"baselines": "shared", "scan": True}
        assert parse_reach("All_Baselines;channels,spw,fields,correlations") == {
            "baselines": "all", "channels": True, "spw": True, "field": True,
            "correlations": True}
        assert parse_reach({"scan": True}) == {"scan": True}

    def test_parse_reach_refuses_what_it_does_not_know(self):
        with pytest.raises(ValueError, match="unknown word"):
            parse_reach("everything")
        with pytest.raises(ValueError, match="one of"):
            parse_reach("antennas, all-baselines")

    def _ctl(self, **kw):
        class _P:                      # the controller only keeps it
            _reader = None
        return FlagController(_P(), **kw)

    def test_defaults_are_as_drawn(self):
        c = self._ctl()
        assert c.scope == {"baselines": "drawn", "spw": False, "scan": False,
                           "field": False}
        assert not c.extend_chan and not c.extend_corr and c.reason == ""
        assert c.reach_words() == [] and c.reach_text() == "Flags cover what is drawn."

    def test_set_reach_keeps_what_is_not_named(self):
        c = self._ctl(reach="antennas, channels")
        assert c.scope["baselines"] == "antennas" and c.extend_chan
        c.set_reach({"scan": True})
        assert c.scope["baselines"] == "antennas" and c.scope["scan"] and c.extend_chan
        assert c.reach_words() == ["all baselines to every antenna drawn",
                                   "all channels", "the whole scan"]
        assert c.reach_text().startswith("⚠ Each box also takes: ")
        with pytest.raises(ValueError):
            c.set_reach({"baselines": "some"})
        with pytest.raises(ValueError):
            c.set_reach({"time": True})

    def test_reason_is_one_plain_line(self):
        c = self._ctl(reason="  RFI\n at 'edge'  ")
        assert c.reason == "RFI at edge"
        c.set_reason("x" * 500)
        assert len(c.reason) == 80
        c.set_reason(None)
        assert c.reason == ""

    def test_configure_message(self):
        c = self._ctl()
        c.push_state = lambda *a, **k: None
        c.response = lambda *a, **k: {}
        c.configure({"reach": {"baselines": "all", "spw": True, "bogus": 1},
                     "reason": "bad weather", "extend_chan": True})
        assert c.scope == {"baselines": "all", "spw": True, "scan": False, "field": False}
        assert c.extend_chan and c.reason == "bad weather"

    def test_scope_helpers(self):
        assert not fe.scope_is_wide(None) and not fe.scope_is_wide({})
        assert fe.scope_is_wide({"scan": True})
        assert fe.scope_is_wide({"baselines": "all"})
        with pytest.raises(ValueError):
            fe.normalize_scope({"baselines": "nearby"})


# ---------------------------------------------------------------------------
# 2. Records
# ---------------------------------------------------------------------------

class TestRecords:

    def test_reason_round_trips(self):
        d = FlagDelta(time_range=(1.0, 2.0), reason="RFI")
        for safe in (True, False):
            back = FlagDelta.from_dict(d.to_dict(json_safe=safe))
            assert back.reason == "RFI"
        old = d.to_dict()
        old.pop("reason")                         # a file saved before today
        assert FlagDelta.from_dict(old).reason == ""

    def test_flagdata_lines_carry_the_reason(self):
        from cubevis.toolbox.visplot.flag_casa import to_flagdata_lines
        d = FlagDelta(time_range=(5.0e9, 5.0e9 + 8), time_format="mjd",
                      antenna_names=("ANTENNA-1",), reason="antenna off source")
        e = FlagDelta(time_range=(5.0e9, 5.0e9 + 8), time_format="mjd")
        lines = to_flagdata_lines([d, e], comments=False)
        assert lines[0].endswith("reason='antenna off source'")
        assert "reason" not in lines[1]
        # a blanket reason fills in where the flag has none of its own
        lines = to_flagdata_lines([d, e], comments=False, reason="visplot")
        assert lines[0].endswith("reason='antenna off source'")
        assert lines[1].endswith("reason='visplot'")
        # the command set applied through CASA is as it was before
        lines = to_flagdata_lines([d, e], comments=False, delta_reasons=False)
        assert not any("reason" in l for l in lines)


# ---------------------------------------------------------------------------
# Simulated data
# ---------------------------------------------------------------------------

def _no_flags(desc, data):
    dims, f = data["FLAG"]
    data["FLAG"] = (dims, np.zeros(np.shape(f), dtype=bool))
    return data


@pytest.fixture(scope="module")
def sim_ms(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    from arcae.lib.arrow_tables import Table
    path = str(tmp_path_factory.mktemp("reach") / "r.ms")
    sim.MSStructureSimulator(
        ntime=NTIME, time_chunks=NTIME, dump_rate=DUMP, time_start=5.0e9,
        nantenna=NANT, auto_corrs=False,
        data_description=[(n, ["XX", "YY"]) for n in NCHAN],
        simulate_data=True, transform_data=_no_flags).simulate_ms(path)
    t = Table.from_filename(path, readonly=False)
    try:
        tm = np.asarray(t.getcol("TIME"))
        idx = np.round((tm - tm.min()) / DUMP).astype(np.int32)
        t.putcol("SCAN_NUMBER", (1 + idx // PER_SCAN).astype(np.int32))
    finally:
        t.close()
    return path


@pytest.fixture(scope="module")
def sim_ps(sim_ms, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("reachps") / "r.ps.zarr")
    dt = xr.open_datatree(sim_ms, engine="xarray-ms:msv2",
                          partition_schema=["FIELD_ID"])
    dt.to_zarr(out, mode="w", compute=True)
    return out


def _run(c):
    return asyncio.run(c)


@pytest.fixture(params=["msv2", "msv4"])
def plotter(request, sim_ms, sim_ps):
    from cubevis.toolbox.visplot import VisibilityPlotter
    kw = dict(ms=sim_ms) if request.param == "msv2" else dict(ps=sim_ps)
    vp = VisibilityPlotter(layout="side", correlation="XX,YY",
                           raster_y="TIME", raster_x="BASELINE", **kw)
    yield vp
    vp.flag_db.clear(record=False)
    vp.close()


def _reset(vp, **reach):
    vp.flag_db.clear(record=False)
    f = vp._flags
    f.scope.update(baselines="drawn", spw=False, scan=False, field=False)
    f.extend_chan = f.extend_corr = False
    f.reason = ""
    f.set_reach(reach)
    return vp._slots[0].raster


def _times(vp):
    b = vp._reader._backend
    for part in b._iter_visibility_partitions(None):
        return np.sort(np.asarray(part.time.values, dtype=float))


def _pending(vp):
    """What the pending flags flag, read from the effective flags:
    ``{(window channels, time index, 'A&B', correlation): n channels}``."""
    return _flagged(vp._reader._backend, vp.flag_db.deltas(), _times(vp))


def _flagged(b, deltas, t_all):
    out = {}
    for part in b._iter_visibility_partitions(None):
        base = b._flag_mask(part)
        eff = fe.apply_pending(b, part, base, deltas) if deltas else base
        f = np.asarray(eff.transpose("time", "baseline_id", "frequency",
                                     "polarization").values, bool)
        a1 = part.baseline_antenna1_name.values.astype(str)
        a2 = part.baseline_antenna2_name.values.astype(str)
        pols = [str(p) for p in part.polarization.values]
        nchan = f.shape[2]
        for ti, bi, pi in zip(*np.nonzero(f.any(axis=2))):
            k = int(np.argmin(np.abs(t_all - float(part.time.values[ti]))))
            out[(nchan, k, f"{a1[bi]}&{a2[bi]}", pols[pi])] = int(f[ti, bi, :, pi].sum())
    return out


def _box(vp, r, p0, p1, k0, k1, **more):
    t = _times(vp)
    msg = dict(x0=p0 - 0.3, x1=p1 + 0.3, y0=t[k0] - 1, y1=t[k1] + 1, flag=True, **more)
    return _run(vp._handle_box_select(msg, "raster", r))


def _ants(name):
    return set(name.split("&"))


# ---------------------------------------------------------------------------
# 3. Engine / plotter
# ---------------------------------------------------------------------------

class TestReach:

    def test_as_drawn_is_unchanged(self, plotter):
        vp = plotter
        r = _reset(vp)
        assert (r._y_dim, r._x_dim) == (Axis.TIME, Axis.BASELINE)
        ax = r.baseline_axis
        resp = _box(vp, r, 2, 3, 4, 5)
        assert resp["notify_text"].startswith("✓"), resp["notify_text"]
        assert "reaching" not in resp["notify_text"]
        got = _pending(vp)
        assert {(k, b) for (_n, k, b, _p) in got} == {
            (k, ax.names[p]) for k in (4, 5) for p in (2, 3)}
        d = vp.flag_db.deltas()[-1]
        assert d.antenna_names is None and not d.extend_scan
        assert not any(p.startswith("reaches") for p in d.provenance)

    def test_shared_antenna(self, plotter):
        vp = plotter
        r = _reset(vp, baselines="shared")
        ax = r.baseline_axis
        # two baselines that share exactly one antenna
        pairs = [(i, j) for i in range(len(ax.names)) for j in range(i + 1, len(ax.names))
                 if j == i + 1 and len(_ants(ax.names[i]) & _ants(ax.names[j])) == 1]
        i, j = pairs[0]
        ant = next(iter(_ants(ax.names[i]) & _ants(ax.names[j])))
        resp = _box(vp, r, i, j, 9, 9)
        assert resp["notify_text"].startswith("✓"), resp["notify_text"]
        assert f"all baselines to {ant}" in resp["notify_text"]
        got = _pending(vp)
        bls = {b for (_n, _k, b, _p) in got}
        assert bls == {n for n in ax.names if ant in _ants(n)} and len(bls) == NANT - 1
        assert {k for (_n, k, _b, _p) in got} == {9}
        d = vp.flag_db.deltas()[-1]
        assert d.antenna_names == (ant,) and d.baseline_ids is None
        assert d.provenance[-1] == f"reaches: all baselines to {ant}"
        # the count is of what is really flagged, not of the box
        assert d.n_samples == sum(got.values())

    def test_shared_antenna_refuses_to_guess(self, plotter):
        vp = plotter
        r = _reset(vp, baselines="shared")
        ax = r.baseline_axis
        resp = _box(vp, r, 0, 0, 3, 3)                 # one baseline: which end?
        assert resp["notify_text"].startswith("⚠ Nothing to flag"), resp["notify_text"]
        assert "two antennas" in resp["notify_text"]
        resp = _box(vp, r, 0, len(ax.names) - 1, 3, 3)  # everything: no one antenna
        assert "do not share exactly one antenna" in resp["notify_text"]
        assert len(vp.flag_db) == 0 and _pending(vp) == {}

    def test_every_antenna_of_the_drawn_baselines(self, plotter):
        vp = plotter
        r = _reset(vp, baselines="antennas")
        ax = r.baseline_axis
        ants = _ants(ax.names[0])
        resp = _box(vp, r, 0, 0, 3, 3)
        assert resp["notify_text"].startswith("✓"), resp["notify_text"]
        bls = {b for (_n, _k, b, _p) in _pending(vp)}
        assert bls == {n for n in ax.names if ants & _ants(n)}
        assert len(bls) == 2 * (NANT - 2) + 1
        assert set(vp.flag_db.deltas()[-1].antenna_names) == ants

    def test_all_baselines(self, plotter):
        vp = plotter
        r = _reset(vp, baselines="all")
        resp = _box(vp, r, 4, 4, 20, 21)
        assert "all baselines" in resp["notify_text"]
        got = _pending(vp)
        assert len({b for (_n, _k, b, _p) in got}) == NANT * (NANT - 1) // 2
        assert {k for (_n, k, _b, _p) in got} == {20, 21}
        d = vp.flag_db.deltas()[-1]
        assert d.baseline_ids is None and d.antenna_names is None

    def test_whole_scan(self, plotter):
        vp = plotter
        r = _reset(vp, scan=True)
        ax = r.baseline_axis
        resp = _box(vp, r, 1, 1, 10, 10)               # integration 10 is in scan 2
        assert "whole scan 2" in resp["notify_text"], resp["notify_text"]
        got = _pending(vp)
        assert {k for (_n, k, _b, _p) in got} == set(range(8, 16))
        assert {b for (_n, _k, b, _p) in got} == {ax.names[1]}
        d = vp.flag_db.deltas()[-1]
        assert d.scan_names == ("2",) and d.extend_scan
        # across a scan boundary: both scans, whole
        _reset(vp, scan=True)
        resp = _box(vp, r, 1, 1, 7, 8)
        assert "whole scans 1, 2" in resp["notify_text"], resp["notify_text"]
        assert {k for (_n, k, _b, _p) in _pending(vp)} == set(range(0, 16))

    def test_scan_and_antenna_together(self, plotter):
        vp = plotter
        r = _reset(vp, scan=True, baselines="antennas", correlations=True)
        ax = r.baseline_axis
        ants = _ants(ax.names[5])
        _box(vp, r, 5, 5, 17, 17)
        got = _pending(vp)
        assert {k for (_n, k, _b, _p) in got} == set(range(16, 24))
        assert {b for (_n, _k, b, _p) in got} == {n for n in ax.names if ants & _ants(n)}
        assert {p for (_n, _k, _b, p) in got} == {"XX", "YY"}
        # every channel of both windows (Channel is not an axis here)
        assert {n: c for (n, _k, _b, _p), c in got.items()} == {16: 16, 8: 8}

    def test_unflag_reaches_the_same_way(self, plotter):
        vp = plotter
        r = _reset(vp, baselines="all")
        _box(vp, r, 0, 0, 2, 2)
        assert _pending(vp)
        t = _times(vp)
        msg = dict(x0=-0.3, x1=0.3, y0=t[2] - 1, y1=t[2] + 1, flag=False)
        vp._flags.show_flagged = True
        try:
            resp = _run(vp._handle_box_select(msg, "raster", r))
        finally:
            vp._flags.show_flagged = False
        assert resp["notify_text"].startswith("✓ Unflagged"), resp["notify_text"]
        assert _pending(vp) == {}

    def test_count_includes_all_channels_and_correlations(self, plotter):
        # The box's own samples were counted; what it really changed with
        # All channels / All correlations was far more (2026-10-09).
        vp = plotter
        for kind in ("raster", "scatter"):
            for reach in ({"channels": True}, {"correlations": True},
                          {"channels": True, "correlations": True}):
                vp.flag_db.clear(record=False)
                _reset(vp, **reach)
                if kind == "raster":
                    _box(vp, vp._slots[0].raster, 2, 2, 4, 4)
                else:
                    sc = vp._slots[1].scatter
                    fg = sc._fig
                    msg = dict(x0=fg.x_range.start, x1=fg.x_range.end, y0=fg.y_range.start,
                               y1=fg.y_range.start + 0.2 * (fg.y_range.end - fg.y_range.start),
                               flag=True)
                    _run(vp._handle_box_select(msg, "scatter", sc))
                d = vp.flag_db.deltas()[-1]
                assert d.n_samples == sum(_pending(vp).values()), (kind, reach)

    def test_reason_is_stored_and_reported(self, plotter):
        vp = plotter
        r = _reset(vp)
        vp._flags.set_reason("RFI")
        _box(vp, r, 0, 0, 0, 0)
        vp._flags.set_reason("")
        _box(vp, r, 1, 1, 0, 0)
        a, b = vp.flag_db.deltas()[-2:]
        assert (a.reason, b.reason) == ("RFI", "")
        page = vp._flags.report_html()
        assert page.count(">Reason<") == 1 and ">RFI<" in page
        assert "Flag reaches (now set)" in page

    def test_held_key_box_is_named_in_the_record(self, plotter):
        vp = plotter
        r = _reset(vp)
        _box(vp, r, 0, 0, 0, 0, span="y")
        assert "[Shift: full height of the view]" in vp.flag_db.deltas()[-1].comment
        _box(vp, r, 1, 1, 0, 0, span="x")
        assert "[Alt: full width of the view]" in vp.flag_db.deltas()[-1].comment
        _box(vp, r, 2, 2, 0, 0)
        assert "full" not in vp.flag_db.deltas()[-1].comment

    def test_scatter_box_keeps_the_reason_and_says_what_was_not_widened(self, plotter):
        vp = plotter
        _reset(vp, scan=True)
        vp._flags.set_reason("scatter RFI")
        sc = vp._slots[1].scatter
        f = sc._fig
        msg = dict(x0=f.x_range.start, x1=f.x_range.end, y0=f.y_range.start,
                   y1=f.y_range.end, flag=True)
        resp = _run(vp._handle_box_select(msg, "scatter", sc))
        assert resp["notify_text"].startswith("✓"), resp["notify_text"]
        assert "channels and correlations only" in resp["notify_text"]
        assert vp.flag_db.deltas()[-1].reason == "scatter RFI"
        _reset(vp)
        resp = _run(vp._handle_box_select(msg, "scatter", sc))
        assert "only" not in resp["notify_text"]

    def test_preview_dialog_says_how_far(self, plotter):
        vp = plotter
        r = _reset(vp, baselines="all")
        vp._flags.set_reason("test")
        vp._flags.preview = True
        try:
            _box(vp, r, 0, 0, 6, 6)
            prop = vp._flags.proposal
            assert prop is not None and prop.reach == ("all baselines",)
            page = vp._flags.proposal_html(prop)
            assert "Reaches" in page and "all baselines" in page and "test" in page
            assert len(vp.flag_db) == 0
        finally:
            vp._flags.preview = False
            vp._flags.reject()


class TestLifecycle:
    """The plan's exit test for H5: a widened flag survives undo / redo,
    a JSON round trip and a commit, on a scratch copy."""

    @pytest.fixture(params=["msv2", "msv4"])
    def scratch(self, request, sim_ms, sim_ps, tmp_path):
        import shutil
        from cubevis.toolbox.visplot import VisibilityPlotter
        if request.param == "msv2":
            path = str(tmp_path / "s.ms")
            shutil.copytree(sim_ms, path)
            kw = dict(ms=path)
        else:
            path = str(tmp_path / "s.ps.zarr")
            shutil.copytree(sim_ps, path)
            kw = dict(ps=path)
        vp = VisibilityPlotter(layout="side", correlation="XX,YY", raster_y="TIME",
                               raster_x="BASELINE", flag_reach="shared-antenna, scan",
                               flag_reason="antenna stuck", **kw)
        yield vp
        vp.close()

    def test_undo_redo_json_commit(self, scratch, tmp_path):
        vp = scratch
        r = vp._slots[0].raster
        ax = r.baseline_axis
        i = next(i for i in range(len(ax.names) - 1)
                 if len(_ants(ax.names[i]) & _ants(ax.names[i + 1])) == 1)
        ant = next(iter(_ants(ax.names[i]) & _ants(ax.names[i + 1])))
        resp = _box(vp, r, i, i + 1, 12, 12)
        assert resp["notify_text"].startswith("✓"), resp["notify_text"]
        want = _pending(vp)
        # all baselines to the antenna, the whole of scan 2, the plotted
        # correlation, every channel of both windows
        assert {k for (_n, k, _b, _p) in want} == set(range(8, 16))
        assert {b for (_n, _k, b, _p) in want} == {n for n in ax.names if ant in _ants(n)}
        assert len(want) == 2 * 8 * (NANT - 1) and sum(want.values()) == 8 * (NANT - 1) * 24

        vp.flag_db.undo()
        assert _pending(vp) == {}
        vp.flag_db.redo()
        assert _pending(vp) == want

        path = vp._flags.export(str(tmp_path / "flags.jsonl"))
        vp.flag_db.clear(record=False)
        assert _pending(vp) == {}
        assert vp._flags.load_jsonl(path) == 1
        assert _pending(vp) == want
        d = vp.flag_db.deltas()[-1]
        assert d.reason == "antenna stuck"
        assert d.provenance[-1] == f"reaches: all baselines to {ant}; whole scan 2"

        page = vp._flags.report_html()
        assert "whole scan 2" in page and "antenna stuck" in page

        b = vp._reader._backend
        t_all = _times(vp)
        rep = b.commit_pending_flags(vp.flag_db.deltas())
        assert rep.get("written") == sum(want.values()), rep
        assert _flagged(b, [], t_all) == want          # now on disk


class TestSpectralWindows:
    """A Time x Channel waterfall of both windows.  They have different
    channel widths, so the panel plots Frequency; the two windows cover
    the same band with 16 and 8 channels."""

    @pytest.fixture(params=["msv2", "msv4"])
    def wf(self, request, sim_ms, sim_ps):
        from cubevis.toolbox.visplot import VisibilityPlotter
        kw = dict(ms=sim_ms) if request.param == "msv2" else dict(ps=sim_ps)
        vp = VisibilityPlotter(layout="side", correlation="XX,YY",
                               raster_y="TIME", raster_x="CHANNEL", **kw)
        yield vp
        vp.flag_db.clear(record=False)
        vp.close()

    def _freqs(self, vp):
        b = vp._reader._backend
        return {len(raw): np.asarray(raw, float) for _k, raw in fe.spw_table(b)}

    def _box(self, vp, r, f0, f1, k):
        t = _times(vp)
        msg = dict(x0=f0, x1=f1, y0=t[k] - 1, y1=t[k] + 1, flag=True)
        return _run(vp._handle_box_select(msg, "raster", r))

    def _channels(self, vp):
        """{window channels: sorted flagged channel numbers}"""
        b = vp._reader._backend
        out = {}
        for part in b._iter_visibility_partitions(None):
            eff = fe.apply_pending(b, part, b._flag_mask(part), vp.flag_db.deltas())
            f = np.asarray(eff.transpose("time", "baseline_id", "frequency",
                                         "polarization").values, bool)
            order = np.argsort(np.asarray(part.frequency.values, float))
            out[f.shape[2]] = [int(c) for c in np.flatnonzero(f.any(axis=(0, 1, 3))[order])]
        return out

    def test_as_drawn_takes_the_channels_under_the_box(self, wf):
        vp = wf
        r = _reset(vp)
        fq = self._freqs(vp)
        # round channels 2..4 of the 16-channel window; channel 1 of the
        # 8-channel window lies in the same stretch of frequency
        resp = self._box(vp, r, fq[16][2] - 1e6, fq[16][4] + 1e6, 3)
        assert resp["notify_text"].startswith("✓"), resp["notify_text"]
        assert self._channels(vp) == {16: [2, 3, 4], 8: [1]}

    def test_same_channel_numbers_in_every_selected_window(self, wf):
        vp = wf
        r = _reset(vp, spw=True)
        fq = self._freqs(vp)
        resp = self._box(vp, r, fq[16][2] - 1e6, fq[16][4] + 1e6, 3)
        assert resp["notify_text"].startswith("✓"), resp["notify_text"]
        assert "of all 2 selected spectral windows" in resp["notify_text"]
        # the channel numbers drawn (1 in one window, 2-4 in the other)
        # in both windows
        assert self._channels(vp) == {16: [1, 2, 3, 4], 8: [1, 2, 3, 4]}
        d = vp.flag_db.deltas()[-1]
        assert d.freq_range is None
        assert sorted((int(sc.spw.n_chan), sc.chan_lo, sc.chan_hi)
                      for sc in d.spw_channels) == [(8, 1, 4), (16, 1, 4)]
        assert {k for (_n, k, _b, _p) in _pending(vp)} == {3}

    def test_channels_beyond_a_narrow_window_are_clipped(self, wf):
        vp = wf
        r = _reset(vp, spw=True)
        fq = self._freqs(vp)
        self._box(vp, r, fq[16][10] - 1e6, fq[16][12] + 1e6, 3)
        got = self._channels(vp)
        assert got[16][-1] == 12 and got[8][-1] == 7
        assert got[8] == [c for c in got[16] if c <= 7]

    def test_all_channels_with_all_windows(self, wf):
        vp = wf
        r = _reset(vp, spw=True, channels=True)
        fq = self._freqs(vp)
        resp = self._box(vp, r, fq[16][2] - 1e6, fq[16][2] + 1e6, 3)
        assert resp["notify_text"].startswith("✓"), resp["notify_text"]
        assert self._channels(vp) == {16: list(range(16)), 8: list(range(8))}


# ---------------------------------------------------------------------------
# 4. GUI
# ---------------------------------------------------------------------------

class TestGui:

    @pytest.fixture
    def vp(self, sim_ms):
        from cubevis.toolbox.visplot import VisibilityPlotter
        vp = VisibilityPlotter(ms=sim_ms, flag_reach="shared-antenna, scan, channels",
                               flag_reason="RFI")
        vp._build_layout()
        yield vp
        vp.close()

    def test_constructor_sets_the_controls(self, vp):
        w = vp._flags._widgets
        assert w["reach_bl"].value == "shared"
        assert [o[0] for o in w["reach_bl"].options] == list(REACH_BASELINES)
        assert w["reach_scan"].active and w["ext_chan"].active
        assert not w["reach_spw"].active and not w["reach_field"].active
        assert not w["ext_corr"].active
        assert w["reason"].value == "RFI"
        assert w["reach_note"].text.startswith("⚠ Each box also takes: ")
        assert w["reach_note"].styles["color"] == "#f9a825"

    def test_flag_tools_are_outlined_when_the_reach_is_wide(self, vp, sim_ms):
        tools = vp._flags.flag_tools
        assert len(tools) >= 4 and all(t.reach_wide for t in tools)
        js = vp._flags._widgets["reach_bl"].js_property_callbacks["change:value"][0]
        assert list(js.args["flag_tools"]) == tools
        assert "t.reach_wide = wide.length > 0" in js.code
        from cubevis.toolbox.visplot import VisibilityPlotter
        plain = VisibilityPlotter(ms=sim_ms)
        try:
            plain._build_layout()
            assert plain._flags.flag_tools and not any(
                t.reach_wide for t in plain._flags.flag_tools)
        finally:
            plain.close()

    def test_no_sidebar_section_is_cut_to_the_window(self, vp):
        # Bokeh caps each child of a column at max-height 100%; a section
        # taller than the window was cut and the next one drawn over it.
        for child in vp._sidebar_col.children:
            assert (child.styles or {}).get("max-height") == "none", type(child).__name__

    def test_saved_flag_files_are_listed_for_loading(self, vp, tmp_path, monkeypatch):
        import json, time as _t
        monkeypatch.chdir(tmp_path)
        f = vp._flags
        mine = f.export(str(tmp_path / "a.flags.jsonl"))
        _t.sleep(0.05)
        other = tmp_path / "b.flags.jsonl"
        other.write_text(json.dumps({"format": "cubevis.visplot.flagdb", "version": 2,
                                     "source": "/x/other.ms"}) + "\n")
        (tmp_path / "c.jsonl").write_text('{"something": "else"}\n')
        (tmp_path / "d.jsonl").write_text("not json\n")
        got = f.list_flag_files()
        names = [os.path.basename(e["path"]) for e in got]
        assert names == ["a.flags.jsonl", "b.flags.jsonl"]      # this data first
        assert got[0]["this_data"] and not got[1]["this_data"]
        assert got[1]["source"] == "other.ms" and got[1]["operations"] == 0
        resp = _run(f._export_action({"kind": "list_json", "path": ""}))
        assert [r[0] for r in resp["backups"]] == [e["path"] for e in got]
        assert "(saved from other.ms)" in resp["backups"][1][1]

    def test_bad_reach_fails_at_construction(self, sim_ms):
        from cubevis.toolbox.visplot import VisibilityPlotter
        with pytest.raises(ValueError):
            VisibilityPlotter(ms=sim_ms, flag_reach="the lot")

    def test_no_flag_control_has_a_tooltip(self, vp):
        # One kind of help: the status area.
        for w in vp._flags.themed_widgets():
            assert getattr(w, "description", None) in (None, ""), type(w).__name__

    def test_every_flag_control_shows_help_in_the_status_area(self, vp):
        from cubevis.bokeh.models import EvHover
        wraps = [m for m in vp._sidebar_col.references() if isinstance(m, EvHover)]

        def inside(layout, target):
            if layout is target:
                return True
            kids = list(getattr(layout, "children", []) or [])
            if getattr(layout, "child", None) is not None:
                kids.append(layout.child)
            return any(inside(k, target) for k in kids if not isinstance(k, (tuple, str)))

        def hint_of(control):
            for m in wraps:
                if inside(m.child, control):
                    for cbs in m.js_event_callbacks.values():
                        for cb in cbs:
                            if "hint" in cb.args:
                                return cb.args["hint"]
            return None
        for w in vp._flags.themed_widgets():
            h = hint_of(w)
            assert h is not None, f"{type(w).__name__} {getattr(w, 'title', '') or getattr(w, 'label', '')!r} has no help"
            assert h.text.startswith("<b>")
        w = vp._flags._widgets
        assert hint_of(w["reach_bl"]) is vp._hint_flag_reach
        assert hint_of(w["reach_scan"]) is vp._hint_flag_reach
        assert hint_of(w["reason"]) is vp._hint_flag_reason
        assert hint_of(w["filter"]) is vp._hint_flag_filter
        for key in ("Shift", "Alt", "Whole scan", "stay until changed"):
            assert key in vp._hint_flag_reach.text

    def test_each_filter_has_its_own_help(self, vp):
        for n in vp._flags.registry.names():
            h = getattr(vp, f"_hint_flagf_{n}")
            f = vp._flags.registry.get(n)
            assert f.label in h.text.replace("&amp;", "&")
            for spec in f.params:
                if spec.gui and spec.help:
                    assert (spec.label or spec.name) in h.text


# ---------------------------------------------------------------------------
# 5. Flag tool
# ---------------------------------------------------------------------------

class TestFlagTool:

    def test_source_stretches_the_box_only_while_a_key_is_held(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))))
        src = os.path.join(root, "cubevisjs", "src", "bokeh", "tools", "flag_tool.ts")
        if not os.path.exists(src):
            pytest.skip("TypeScript source not present")
        s = open(src).read()
        assert "m.shift" in s and "m.alt" in s
        # Ctrl+click is the secondary click on macOS: not a modifier here
        assert "m.ctrl" not in s and "ctrlKey" not in s
        # the box sent is the one last drawn, not one recomputed from the
        # pointer-up event (which can lack its modifiers on macOS)
        end = s[s.index("override _pan_end"):s.index("comm.send(msg_id")]
        assert "_draw_box" not in end and "this._cancelled" in end
        assert '"Escape"' in s
        # a key held as the drag starts counts for the whole drag: no
        # keyup handling may shrink the box, and the page-wide key state
        # is consulted (a key pressed before the button, focus elsewhere)
        assert 'addEventListener("keyup", this._on_key' not in s
        assert "HELD.shift" in s and "this._shift = this._shift ||" in s
        # a press held still before moving must not lose the box: Bokeh's
        # press gesture is switched off while a flag tool is active
        assert "press_threshold" in s and "note_active(this.model.id" in s
        assert "flag, panel, at_pixel_res, span," in s
        assert "reach_wide" in s

    def test_every_shipped_bundle_has_it(self):
        import cubevis
        root = os.path.join(os.path.dirname(cubevis.__file__), "__js__")
        bundles = sorted(glob.glob(os.path.join(root, "bokeh-3.*", "cubevisjs.min.js")))
        assert len(bundles) >= 5
        texts = {open(b).read() for b in bundles}
        assert len(texts) == 1, "the bundles differ"
        t = texts.pop()
        assert "at_pixel_res" in t and "span" in t and ".shift" in t
        assert "reach_wide" in t and "Escape" in t
