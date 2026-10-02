"""
test_flag_commit.py
===================
Export / commit of pending flags (FlagDB v2, ``flag_commit.py``):

* MSv4: zarr writes of exactly the changed samples, side-file backup,
  verification against the display's own fold, restore -- end to end
  through the plotter's Export / commit menu.
* MSv2: exact final-state write with arcae (default and only write), side-
  file backup, FLAG_ROW consistency, verification, restore.
* JSON export -> load round trip, spectral-window check on load.

Needs no MS: builds a small simulated MSv2 (and converts it to MSv4 when
xradio is available).
"""
import asyncio
import json
import os
import shutil
import sys
import types
import warnings

import numpy as np
import pytest

from cubevis.toolbox.visplot.flag_model import FlagDelta
from cubevis.toolbox.visplot.selection import SelectionSpec
from cubevis.toolbox.visplot import flag_commit as fc

warnings.filterwarnings("ignore")


def _transform(desc, data):
    ddid = int(desc.DATA_DESC_ID.item())
    rng = np.random.default_rng(1000 + ddid * 17 + int(desc.chunk_id))
    dims, vis = data["DATA"]
    v = (1.0 + 0.1 * rng.standard_normal(vis.shape)) + 1j * (0.1 * rng.standard_normal(vis.shape))
    for k in range(0, vis.shape[0], 7):
        v[k, k % vis.shape[1], 0] = 50.0
    data["DATA"] = (dims, v.astype(np.complex64))
    fdims, _ = data["FLAG"]
    f = np.zeros(vis.shape, dtype=bool)
    f[0, :, 0] = True
    data["FLAG"] = (fdims, f)
    return data


@pytest.fixture(scope="module")
def sim_ms(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("commit") / "c.ms")
    sim.MSStructureSimulator(
        ntime=10, nantenna=5, auto_corrs=False,
        data_description=[(8, ["XX", "XY", "YX", "YY"]), (4, ["XX", "YY"])],
        simulate_data=True, transform_data=_transform).simulate_ms(path)
    return path


@pytest.fixture(scope="module")
def sim_ps(sim_ms, tmp_path_factory):
    """The simulated MS as an MSv4-shaped zarr store (the same conversion as
    create_test_msv4.py: xarray-ms DataTree -> to_zarr)."""
    import xarray as xr
    out = str(tmp_path_factory.mktemp("commitps") / "c.ps.zarr")
    dt = xr.open_datatree(sim_ms, engine="xarray-ms:msv2",
                          partition_schema=["FIELD_ID"])
    dt.to_zarr(out, mode="w", compute=True)
    return out


def _deltas(backend):
    parts = list(backend._iter_visibility_partitions(None))
    t = parts[0].time.values
    pol = str(parts[0].polarization.values[0])

    def ev(**r):
        r.setdefault("selection", SelectionSpec()); r.setdefault("flag", True)
        x = backend.evaluate_flag_request(r)
        return FlagDelta.from_dict(x["delta"]) if x["delta"] else None
    out = [ev(kind="raster", x_axis="TIME", x0=t[2], x1=t[4], y_axis="BASELINE",
              y0=0.6, y1=2.4, polarization=pol),
           ev(kind="scatter", x_axis="TIME", x0=t[0] - 1, x1=t[-1] + 1, y0=10, y1=1e9,
              layers=[{"y_axis": "AMPLITUDE", "polarization": pol}]),
           ev(flag=False, kind="raster", x_axis="TIME", x0=t[0], x1=t[0], y_axis="BASELINE",
              y0=-0.5, y1=99, polarization=pol)]
    return [d for d in out if d is not None]


def _disk(backend):
    return [np.asarray(backend._disk_flag_mask(p).values).copy()
            for p in backend._iter_visibility_partitions(None)]


# ---------------------------------------------------------------------- #
# MSv4                                                                     #
# ---------------------------------------------------------------------- #

def test_msv4_commit_verify_restore(sim_ps, tmp_path):
    from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
    ps = str(tmp_path / "w.ps.zarr")
    shutil.copytree(sim_ps, ps)
    b = MSv4Backend(ps); b.open()
    try:
        assert fc.capabilities(b)["write"] and fc.capabilities(b)["format"] == "msv4"
        deltas = _deltas(b)
        before = _disk(b)
        rep = fc.commit(b, deltas)
        assert rep["verified"] and rep["mismatches"] == 0 and rep["written"] > 0
        after = _disk(b)
        assert sum(int((x != y).sum()) for x, y in zip(before, after)) == rep["written"]
        assert os.path.exists(rep["backup"])
        fc.restore_msv4_backup(b, rep["backup"])
        assert all(np.array_equal(x, y) for x, y in zip(before, _disk(b)))
    finally:
        b.close()


def test_msv4_commit_through_the_plotter_menu(sim_ps, tmp_path):
    from cubevis.toolbox.visplot import VisibilityPlotter
    ps = str(tmp_path / "p.ps.zarr")
    shutil.copytree(sim_ps, ps)
    vp = VisibilityPlotter(ps=ps, layout="side", correlation="XX,YY")
    try:
        f = vp.flags
        opts = dict(f.export_options())
        assert set(opts) == {"json", "commit", "load", "restore"}     # MSv4: no CASA option
        b = vp._reader._backend
        for d in _deltas(b):
            f.db.add(d)
        before_gen = vp._cache_generation
        resp = asyncio.run(f.handle_action({"action": "export", "kind": "commit"}))
        assert "preview" in resp and "modifies the data set" in resp["preview"]["html"]
        resp = asyncio.run(f.handle_action({"action": "accept", "id": resp["preview"]["id"]}))
        assert resp["notify_text"].startswith("✓ Wrote") and "verified" in resp["notify_text"]
        # cached frames were updated in place (no re-read); the generation
        # only moves when that was not possible
        assert len(f.db) == 0 and vp._cache_generation in (before_gen, before_gen + 1)
        # cancel path writes nothing
        f.db.add(_deltas(b)[0])
        resp = asyncio.run(f.handle_action({"action": "export", "kind": "commit"}))
        resp = asyncio.run(f.handle_action({"action": "reject"}))
        assert "cancelled" in resp["notify_text"] and len(f.db) == 1
    finally:
        vp.close()


# ---------------------------------------------------------------------- #
# MSv2                                                                     #
# ---------------------------------------------------------------------- #










def test_msv2_write_needs_no_casatools(sim_ms, monkeypatch):
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    from cubevis.toolbox.visplot import flag_casa
    monkeypatch.setattr(fc, "casatools_available", lambda: (False, "casatools missing"))
    monkeypatch.setattr(flag_casa, "casa_detected", lambda: (False, "casatools missing"))
    b = MSv2Backend(sim_ms); b.open()
    try:
        caps = fc.capabilities(b)
        assert caps["write"] is True and caps["casa"] is False
    finally:
        b.close()





# ---------------------------------------------------------------------- #
# JSON round trip                                                          #
# ---------------------------------------------------------------------- #

def test_json_export_load_round_trip_and_spw_check(sim_ms, tmp_path):
    from cubevis.toolbox.visplot import VisibilityPlotter
    vp = VisibilityPlotter(ms=sim_ms, layout="side", correlation="XX,YY")
    try:
        f = vp.flags
        deltas = _deltas(vp._reader._backend)
        for d in deltas:
            f.db.add(d)
        p = str(tmp_path / "flags.jsonl")
        resp = asyncio.run(f.handle_action({"action": "export", "kind": "json", "path": p}))
        assert resp["notify_text"].startswith("Wrote")
        hdr = json.loads(open(p).readline())
        assert hdr["spw_table"] and hdr["data_format"] == "msv2"
        f.db.clear(record=False)
        resp = asyncio.run(f.handle_action({"action": "export", "kind": "load", "path": p}))
        assert f"Loaded {len(deltas)}" in resp["notify_text"]
        assert [d.to_dict() for d in f.db.deltas()] == \
               [dict(d.to_dict(), seq=g.seq) for d, g in zip(deltas, f.db.deltas())]
        # a file referring to a window this data does not have is refused
        lines = open(p).read().splitlines()
        body = [json.loads(x) for x in lines[1:]]
        for d in body:
            for blk in d.get("samples") or []:
                blk["spw"]["n_chan"] = 999
            for sc in d.get("spw_channels") or []:
                sc["spw"]["n_chan"] = 999
        bad = str(tmp_path / "bad.jsonl")
        open(bad, "w").write("\n".join([lines[0]] + [json.dumps(x) for x in body]) + "\n")
        f.db.clear(record=False)
        resp = asyncio.run(f.handle_action({"action": "export", "kind": "load", "path": bad}))
        assert "failed" in resp["notify_text"] and "not in this data" in resp["notify_text"]
        assert len(f.db) == 0
    finally:
        vp.close()


@pytest.mark.parametrize("kind", ["msv4", "msv2"])
def test_remote_commit_runs_in_the_worker(sim_ps, sim_ms, tmp_path, kind):
    """Remote sessions commit where the data are.  Local kernel only (the
    store is a local temporary copy)."""
    pytest.importorskip("jupyter_client")
    kernel = os.environ.get("CUBEVIS_TEST_KERNEL", "python3")
    if kernel != "python3":
        pytest.skip("expected without a shared filesystem: this test commits into a local "
                    "temporary copy, which only a kernel on this host can see")
    from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    from cubevis.toolbox.visplot.remote_reduction_context import RemoteReductionContext
    Backend = MSv4Backend if kind == "msv4" else MSv2Backend
    ps = str(tmp_path / ("remote.ps.zarr" if kind == "msv4" else "remote.ms"))
    shutil.copytree(sim_ps if kind == "msv4" else sim_ms, ps)
    local = Backend(ps); local.open()
    deltas = _deltas(local)
    before = _disk(local)
    local.close()
    try:
        rc = RemoteReductionContext(ps, kernel, backend_kind=kind)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"cannot start kernel {kernel!r}: {exc}")
    try:
        caps = rc.flag_commit_capabilities()
        assert caps["write"] and caps["format"] == kind
        rep = rc.commit_pending_flags(deltas)
        assert rep["verified"] and rep["written"] > 0
        rc.restore_flag_backup(rep["backup"])
    finally:
        rc.close()
    local = Backend(ps); local.open()
    try:
        assert all(np.array_equal(x, y) for x, y in zip(before, _disk(local)))
    finally:
        local.close()


def test_exports_never_overwrite(sim_ms, tmp_path, monkeypatch):
    from cubevis.toolbox.visplot import VisibilityPlotter
    monkeypatch.chdir(tmp_path)
    vp = VisibilityPlotter(ms=sim_ms, layout="side", correlation="XX,YY")
    try:
        f = vp.flags
        f.db.add(_deltas(vp._reader._backend)[0])
        a = asyncio.run(f.handle_action({"action": "export", "kind": "json"}))["notify_text"]
        b = asyncio.run(f.handle_action({"action": "export", "kind": "json"}))["notify_text"]
        files = sorted(p.name for p in tmp_path.glob("*.flags.*.jsonl"))
        assert len(files) == 2 and files[0] != files[1]          # time-stamped, unique
        # an explicit existing name is refused, and the file is untouched
        target = tmp_path / files[0]
        before = target.read_text()
        r = asyncio.run(f.handle_action({"action": "export", "kind": "json",
                                         "path": str(target)}))["notify_text"]
        assert "already exists" in r and target.read_text() == before
    finally:
        vp.close()


# ---------------------------------------------------------------------- #
# Export exactness under MSSelection semantics                              #
# ---------------------------------------------------------------------- #










# ---------------------------------------------------------------------- #
# MSv2 -- exact arcae write (default)                                      #
# ---------------------------------------------------------------------- #

def test_msv2_arcae_commit_verify_restore(sim_ms, tmp_path, monkeypatch):
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    monkeypatch.setattr(fc, "casatools_available", lambda: (False, "test"))
    ms = str(tmp_path / "a.ms")
    shutil.copytree(sim_ms, ms)
    b = MSv2Backend(ms); b.open()
    try:
        deltas = _deltas(b)
        before = _disk(b)
        rep = fc.commit(b, deltas)
        assert rep["method"] == "arcae" and rep["verified"] and rep["mismatches"] == 0
        after = _disk(b)
        assert sum(int((x != y).sum()) for x, y in zip(before, after)) == rep["written"] > 0
        # FLAG_ROW consistent with FLAG for every written row
        from arcae.lib.arrow_tables import Table
        t = Table.from_filename(ms)
        dd = np.asarray(t.getcol("DATA_DESC_ID"))
        for d in np.unique(dd):
            rows = np.flatnonzero(dd == d)
            f = np.asarray(t.getcol("FLAG", index=(rows,))).astype(bool)
            fr = np.asarray(t.getcol("FLAG_ROW", index=(rows,))).astype(bool)
            assert np.array_equal(fr, f.reshape(len(rows), -1).all(axis=1))
        t.close()
        fc.restore_backup(b, rep["backup"])
        assert all(np.array_equal(x, y) for x, y in zip(before, _disk(b)))
    finally:
        b.close()


def test_msv2_arcae_commit_through_the_menu(sim_ms, tmp_path, monkeypatch):
    from cubevis.toolbox.visplot import VisibilityPlotter
    monkeypatch.setattr(fc, "casatools_available", lambda: (False, "test"))
    ms = str(tmp_path / "m.ms")
    shutil.copytree(sim_ms, ms)
    vp = VisibilityPlotter(ms=ms, layout="side", correlation="XX,YY")
    try:
        f = vp.flags
        opts = dict(f.export_options())
        assert set(opts) - {"commit_casa"} == {"json", "commit", "load", "restore"}
        assert opts["commit"] == "Write flags to the MS"
        for d in _deltas(vp._reader._backend):
            f.db.add(d)
        resp = asyncio.run(f.handle_action({"action": "export", "kind": "commit"}))
        assert "arcae" in resp["preview"]["html"]
        resp = asyncio.run(f.handle_action({"action": "accept", "id": resp["preview"]["id"]}))
        assert resp["notify_text"].startswith("✓ Wrote") and "verified" in resp["notify_text"]
        assert len(f.db) == 0
    finally:
        vp.close()


def test_restore_picker_confirmation_and_history(sim_ms, tmp_path):
    """Restore without a path offers the newest backup; restoring asks for
    confirmation; commit and restore each add one HISTORY row."""
    from cubevis.toolbox.visplot import VisibilityPlotter
    from arcae.lib.arrow_tables import Table
    ms = str(tmp_path / "r.ms")
    shutil.copytree(sim_ms, ms)

    def nhist():
        t = Table.from_filename(f"{ms}::HISTORY")
        n = t.nrow(); t.close()
        return n
    vp = VisibilityPlotter(ms=ms, layout="side", correlation="XX,YY")
    try:
        f = vp.flags
        before = _disk(vp._reader._backend)
        h0 = nhist()
        for d in _deltas(vp._reader._backend):
            f.db.add(d)
        r = asyncio.run(f.handle_action({"action": "export", "kind": "commit"}))
        r = asyncio.run(f.handle_action({"action": "accept", "id": r["preview"]["id"]}))
        assert "HISTORY entry" in r["notify_text"] and nhist() == h0 + 1
        r = asyncio.run(f.handle_action({"action": "export", "kind": "restore"}))
        assert r["export_path"].endswith(".npz") and "1 backup(s)" in r["notify_text"]
        r = asyncio.run(f.handle_action({"action": "export", "kind": "restore",
                                         "path": r["export_path"]}))
        assert "Restore flags from this backup?" in r["preview"]["html"]
        r = asyncio.run(f.handle_action({"action": "accept", "id": r["preview"]["id"]}))
        assert r["notify_text"].startswith("Restored") and nhist() == h0 + 2
        assert all(np.array_equal(x, y) for x, y in zip(before, _disk(vp._reader._backend)))
    finally:
        vp.close()


def test_remote_sessions_autosave_and_recover(sim_ms, tmp_path, monkeypatch):
    """Pending flags of a REMOTE session are kept in a local JSON Lines
    autosave and can be recovered; local sessions write nothing."""
    from cubevis.toolbox.visplot import VisibilityPlotter
    from cubevis.toolbox.visplot.flag_controls import FlagController
    monkeypatch.setenv("HOME", str(tmp_path))
    vp = VisibilityPlotter(ms=sim_ms, layout="side", correlation="XX,YY")
    try:
        f = vp.flags
        assert not f.is_remote and f.autosaved() is None
        monkeypatch.setattr(FlagController, "is_remote", property(lambda self: True))
        deltas = _deltas(vp._reader._backend)
        for d in deltas:
            f.db.add(d)
        f._autosave_now()
        auto = f.autosaved()
        assert auto and auto["operations"] == len(deltas)
        assert auto["path"].startswith(str(tmp_path))
        assert "recover" in dict(f.export_options())
        f.db.clear(record=False)
        r = asyncio.run(f.handle_action({"action": "export", "kind": "recover"}))
        assert f"Recovered {len(deltas)}" in r["notify_text"] and len(f.db) == len(deltas)
        f.db.clear(record=False)
        f._autosave_now()                      # nothing pending -> autosave removed
        assert f.autosaved() is None
    finally:
        vp.close()


def test_backup_dropdown_lists_backups(sim_ms, tmp_path):
    from cubevis.toolbox.visplot import VisibilityPlotter
    ms = str(tmp_path / "d.ms")
    shutil.copytree(sim_ms, ms)
    vp = VisibilityPlotter(ms=ms, layout="side", correlation="XX,YY")
    try:
        f = vp.flags
        r = asyncio.run(f.handle_action({"action": "export", "kind": "list_backups"}))
        assert r["backups"] == [] and "No commit backups" in r["notify_text"]
        f.db.add(_deltas(vp._reader._backend)[0])
        p = asyncio.run(f.handle_action({"action": "export", "kind": "commit"}))
        asyncio.run(f.handle_action({"action": "accept", "id": p["preview"]["id"]}))
        r = asyncio.run(f.handle_action({"action": "export", "kind": "list_backups"}))
        assert len(r["backups"]) == 1 and r["backups"][0][0].endswith(".npz")
        assert "1 op(s)" in r["backups"][0][1]
    finally:
        vp.close()


@pytest.mark.parametrize("kind", ["msv2", "msv4"])
def test_cached_frames_after_commit_match_a_fresh_read(sim_ms, sim_ps, tmp_path, kind):
    """After a commit the cached scatter frames are updated in place (no
    re-read); what they draw must equal a fresh read of the written data."""
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
    from cubevis.toolbox.visplot.data.reader import ScatterLayerSpec
    from cubevis.toolbox.visplot.axes import Axis
    B = MSv2Backend if kind == "msv2" else MSv4Backend
    path = str(tmp_path / ("f.ms" if kind == "msv2" else "f.ps.zarr"))
    shutil.copytree(sim_ms if kind == "msv2" else sim_ps, path)
    pol = "XX"
    layers = [ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization=pol, cmap=("#000000", "#ffffff"))]
    kw = dict(width=120, height=90, ref_scale=None)
    b = B(path); b.open()
    try:
        b.query_columns(Axis.TIME, layers, SelectionSpec(), **kw)      # fill the cache
        reads = []
        orig = b._query_columns_raw
        b._query_columns_raw = lambda *a, **k: (reads.append(1), orig(*a, **k))[1]
        rep = fc.commit(b, _deltas(b))
        assert rep["verified"] and rep.get("frames_refreshed", 0) >= 1
        after = b.query_columns(Axis.TIME, layers, SelectionSpec(), **kw)
        assert reads == [], "the post-commit redraw re-read the data"
    finally:
        b.close()
    fresh_b = B(path); fresh_b.open()
    try:
        fresh = fresh_b.query_columns(Axis.TIME, layers, SelectionSpec(), **kw)
    finally:
        fresh_b.close()
    for a, f in zip(after.layers, fresh.layers):
        assert np.array_equal(a.image, f.image)


# ---------------------------------------------------------------------- #
# Optional CASA flagdata write (secondary; only when CASA is installed)    #
# ---------------------------------------------------------------------- #

@pytest.fixture
def fake_casatasks(monkeypatch):
    """A recording stand-in for casatasks (writes nothing)."""
    import sys
    import types
    from cubevis.toolbox.visplot import flag_casa
    calls = []
    mod = types.ModuleType("casatasks")
    mod.flagmanager = lambda **kw: calls.append(("flagmanager", kw))
    mod.flagdata = lambda **kw: calls.append(("flagdata", kw))
    monkeypatch.setitem(sys.modules, "casatasks", mod)
    monkeypatch.setattr(flag_casa, "casa_detected", lambda: (True, ""))
    return calls


def test_casa_option_only_when_casa_is_installed(sim_ms, monkeypatch):
    from cubevis.toolbox.visplot import VisibilityPlotter, flag_casa
    for ok in (False, True):
        monkeypatch.setattr(flag_casa, "casa_detected",
                            lambda ok=ok: (ok, "" if ok else "casatasks is not installed"))
        vp = VisibilityPlotter(ms=sim_ms, layout="side", correlation="XX,YY")
        try:
            assert ("commit_casa" in dict(vp.flags.export_options())) is ok
        finally:
            vp.close()


def test_casa_write_calls_in_order_backs_up_and_verifies(sim_ms, tmp_path, fake_casatasks):
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    ms = str(tmp_path / "c.ms")
    shutil.copytree(sim_ms, ms)
    b = MSv2Backend(ms); b.open()
    try:
        deltas = _deltas(b)
        rep = fc.commit(b, deltas, method="casa", version_name="vtest")
        kinds = [c[0] for c in fake_casatasks]
        assert kinds[0] == "flagmanager" and set(kinds[1:]) == {"flagdata"}
        assert fake_casatasks[0][1]["versionname"] == "vtest"
        for _k, kw in fake_casatasks[1:]:
            assert kw["mode"] == "list" and kw["flagbackup"] is False and kw["inpfile"]
        # the stand-in wrote nothing: the verification must say so, and the
        # cached frames must NOT be refreshed to a state that is not on disk
        assert rep["verified"] is False and rep["mismatches"] == rep["expected_changes"] > 0
        assert "frames_refreshed" not in rep
        assert os.path.exists(rep["backup"]) and rep["history"]
    finally:
        b.close()


def test_casa_write_via_menu_warns_and_reports(sim_ms, tmp_path, fake_casatasks):
    from cubevis.toolbox.visplot import VisibilityPlotter
    ms = str(tmp_path / "m.ms")
    shutil.copytree(sim_ms, ms)
    vp = VisibilityPlotter(ms=ms, layout="side", correlation="XX,YY")
    try:
        f = vp.flags
        f._caps = None
        for d in _deltas(vp._reader._backend):
            f.db.add(d)
        r = asyncio.run(f.handle_action({"action": "export", "kind": "commit_casa"}))
        html_ = r["preview"]["html"]
        assert "CASA-like write" in html_ and "flagdata" in html_ and "command(s)" in html_
        r = asyncio.run(f.handle_action({"action": "accept", "id": r["preview"]["id"]}))
        assert r["notify_text"].startswith("⚠ Wrote") and "differ from what visplot showed" \
            in r["notify_text"]
    finally:
        vp.close()


def test_casa_write_reports_a_broken_casa_install(sim_ms, tmp_path, monkeypatch):
    import sys
    from cubevis.toolbox.visplot import flag_casa
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    monkeypatch.setitem(sys.modules, "casatasks", None)        # import fails
    ms = str(tmp_path / "b.ms")
    shutil.copytree(sim_ms, ms)
    b = MSv2Backend(ms); b.open()
    try:
        before = _disk(b)
        with pytest.raises(RuntimeError, match="casatasks could not be imported"):
            fc.commit(b, _deltas(b), method="casa")
        assert all(np.array_equal(x, y) for x, y in zip(before, _disk(b)))
    finally:
        b.close()


def _emulate_casa(lines, backend, spw_ids):
    """Samples selected by manual/unflag lines under MSSelection semantics:
    the cross product of the timerange list, baselines, spw:channels and
    correlations of each command."""
    import datetime as _dt
    import re
    from cubevis.toolbox.visplot.flag_engine import block_coords
    epoch = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)

    def ts(x):
        d = _dt.datetime.strptime(x, "%Y/%m/%d/%H:%M:%S.%f").replace(tzinfo=_dt.timezone.utc)
        return (d - epoch).total_seconds()
    cmds = []
    for line in lines:
        kv = dict(re.findall(r"(\w+)='([^']*)'", line))
        trs = [tuple(ts(y) for y in x.split("~")) for x in kv["timerange"].split(",")]
        pairs = set(tuple(p.split("&")) for p in kv["antenna"].split(";"))
        sid, ch = kv["spw"].split(":")
        c0, c1 = map(int, ch.split("~"))
        cmds.append((trs, pairs, int(sid), c0, c1, set(kv["correlation"].split(","))))
    out = []
    for p in backend._iter_visibility_partitions(None):
        bc = block_coords(backend, p)
        got = np.zeros(bc.shape, bool)
        sid = spw_ids.get(bc.spw)
        for trs, pairs, s_, c0, c1, corr in cmds:
            if s_ != sid:
                continue
            mt = np.zeros(len(bc.times), bool)
            for t0, t1 in trs:
                mt |= (bc.times >= t0) & (bc.times <= t1)
            mb = np.array([(a, c) in pairs or (c, a) in pairs for a, c in zip(bc.ant1, bc.ant2)])
            mf = (bc.chans >= c0) & (bc.chans <= c1)
            mp = np.isin(bc.pols, list(corr))
            got |= (mt[:, None, None, None] & mb[None, :, None, None]
                    & mf[None, None, :, None] & mp[None, None, None, :])
        out.append((bc, got))
    return out


def test_casa_commands_are_exact_under_msselection(sim_ms):
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    from cubevis.toolbox.visplot.flag_casa import to_flagdata_lines
    from cubevis.toolbox.visplot.flag_model import delta_mask
    b = MSv2Backend(sim_ms); b.open()
    try:
        ids = b.spw_casa_ids()
        t = next(iter(b._iter_visibility_partitions(None))).time.values
        for y0 in (10.0, 0.9):
            r = b.evaluate_flag_request(dict(
                flag=True, selection=SelectionSpec(), kind="scatter", x_axis="TIME",
                x0=t[0] - 1, x1=t[-1] + 1, y0=y0, y1=1e9,
                layers=[{"y_axis": "AMPLITUDE", "polarization": "XX"}]))
            d = FlagDelta.from_dict(r["delta"])
            lines = to_flagdata_lines([d], spw_ids=ids, comments=False)
            for bc, got in _emulate_casa(lines, b, ids):
                want = delta_mask(d, bc)
                want = np.zeros(bc.shape, bool) if want is None else want
                assert np.array_equal(got, want)
    finally:
        b.close()
