"""
test_flag_commit.py
===================
Export / commit of pending flags (FlagDB v2, ``flag_commit.py``):

* MSv4: zarr writes of exactly the changed samples, side-file backup,
  verification against the display's own fold, restore -- end to end
  through the plotter's Export / commit menu.
* MSv2: casatasks ``flagmanager`` save, then one ``flagdata(mode='list')``
  call per operation in order, then verification.  Checked with a
  recording stand-in for casatasks everywhere, and for real when casatools
  imports (the real test is skipped otherwise).
* JSON export -> load round trip, spectral-window check on load.
* The MSv2 flagdata script.

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
        assert "commit" in opts and "script" not in opts and "restore" in opts
        b = vp._reader._backend
        for d in _deltas(b):
            f.db.add(d)
        before_gen = vp._cache_generation
        resp = asyncio.run(f.handle_action({"action": "export", "kind": "commit"}))
        assert "preview" in resp and "modifies the data set" in resp["preview"]["html"]
        resp = asyncio.run(f.handle_action({"action": "accept", "id": resp["preview"]["id"]}))
        assert resp["notify_text"].startswith("✓ Wrote") and "verified" in resp["notify_text"]
        assert len(f.db) == 0 and vp._cache_generation == before_gen + 1
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

@pytest.fixture
def fake_casatasks(monkeypatch):
    """A recording stand-in for casatasks (writes nothing)."""
    calls = []
    mod = types.ModuleType("casatasks")
    mod.flagmanager = lambda **kw: calls.append(("flagmanager", kw))
    mod.flagdata = lambda **kw: calls.append(("flagdata", kw))
    monkeypatch.setitem(sys.modules, "casatasks", mod)
    return calls


def test_msv2_commit_calls_casa_in_order_and_verifies(sim_ms, tmp_path, fake_casatasks):
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    ms = str(tmp_path / "f.ms")
    shutil.copytree(sim_ms, ms)
    b = MSv2Backend(ms); b.open()
    try:
        deltas = _deltas(b)
        rep = fc.commit(b, deltas, method="casa", version_name="vtest")
        kinds = [c[0] for c in fake_casatasks]
        assert kinds[0] == "flagmanager" and kinds[1:] == ["flagdata"] * len(deltas)
        assert fake_casatasks[0][1]["mode"] == "save" and fake_casatasks[0][1]["versionname"] == "vtest"
        for (_k, kw), d in zip(fake_casatasks[1:], deltas):
            assert kw["mode"] == "list" and kw["flagbackup"] is False and kw["inpfile"]
            want = "mode='manual'" if d.flag else "mode='unflag'"
            assert all(want in line for line in kw["inpfile"])
        # the stand-in wrote nothing: verification must notice every change
        assert rep["verified"] is False and rep["mismatches"] == rep["expected_changes"] > 0
    finally:
        b.close()


def test_msv2_commit_real_casa(sim_ms, tmp_path):
    ok, why = fc.casatools_available()
    if not ok:
        pytest.skip(why)
    from casatasks import flagmanager
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    ms = str(tmp_path / "r.ms")
    shutil.copytree(sim_ms, ms)
    b = MSv2Backend(ms); b.open()
    try:
        before = _disk(b)
        rep = fc.commit(b, _deltas(b), method="casa", version_name="visplot_test")
        assert rep["verified"], rep                 # CASA wrote exactly what visplot showed
        b.close()
        flagmanager(vis=ms, mode="restore", versionname="visplot_test")
        b.open(); b._clear_lookup_caches()
        assert all(np.array_equal(x, y) for x, y in zip(before, _disk(b)))
    finally:
        b.close()


def test_msv2_capabilities_without_casatools(sim_ms, monkeypatch):
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    monkeypatch.setattr(fc, "casatools_available", lambda: (False, "casatools missing"))
    b = MSv2Backend(sim_ms); b.open()
    try:
        caps = fc.capabilities(b)
        # the exact arcae write needs no CASA; only the CASA write is disabled
        assert caps["write"] is True and caps["casa"] is False
        assert "casatools" in caps["casa_reason"] and caps["script"]
    finally:
        b.close()


def test_flagdata_script_is_python_and_ordered(sim_ms):
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    b = MSv2Backend(sim_ms); b.open()
    try:
        deltas = _deltas(b)
        text = b.flagdata_script(deltas)
    finally:
        b.close()
    compile(text, "script.py", "exec")
    assert text.index("flagmanager(vis=vis, mode='save'") < text.index("flagdata(vis=vis")
    assert text.count("flagdata(vis=vis, mode='list'") == len(deltas)
    assert "mode='unflag'" in text and f"vis = {sim_ms!r}" in text


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
        s = asyncio.run(f.handle_action({"action": "export", "kind": "script"}))["notify_text"]
        assert len(list(tmp_path.glob("*.flagdata.*.py"))) == 1
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

def _emulate_casa(lines, backend, spw_ids):
    """Samples selected by our manual/unflag lines, using MSSelection-style
    semantics: a command selects the cross product of its timerange list,
    antenna baseline list, spw:channel range and correlations."""
    import datetime as _dt
    import re
    epoch = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)

    def ts(s):
        d = _dt.datetime.strptime(s, "%Y/%m/%d/%H:%M:%S.%f").replace(tzinfo=_dt.timezone.utc)
        return (d - epoch).total_seconds()
    from cubevis.toolbox.visplot.flag_engine import block_coords
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
        for trs, pairs, s, c0, c1, corr in cmds:
            if s != sid:
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


def test_sample_set_export_is_exact_under_msselection(sim_ms):
    """Every exported sample-set command selects only samples of the set,
    and together they select all of them (no collateral, nothing missed)."""
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    from cubevis.toolbox.visplot.flag_export import to_flagdata_lines
    from cubevis.toolbox.visplot.flag_model import delta_mask
    b = MSv2Backend(sim_ms); b.open()
    try:
        ids = b.spw_casa_ids()
        t = next(iter(b._iter_visibility_partitions(None))).time.values
        for y0 in (10.0, 0.9):                       # outliers only / dense box
            r = b.evaluate_flag_request(dict(
                flag=True, selection=SelectionSpec(), kind="scatter", x_axis="TIME",
                x0=t[0] - 1, x1=t[-1] + 1, y0=y0, y1=1e9,
                layers=[{"y_axis": "AMPLITUDE", "polarization": "XX"},
                        {"y_axis": "PHASE", "polarization": "YY"}] if y0 < 1 else
                       [{"y_axis": "AMPLITUDE", "polarization": "XX"}]))
            d = FlagDelta.from_dict(r["delta"])
            lines = to_flagdata_lines([d], spw_ids=ids, comments=False)
            for bc, got in _emulate_casa(lines, b, ids):
                want = delta_mask(d, bc)
                want = np.zeros(bc.shape, bool) if want is None else want
                assert np.array_equal(got, want), (y0, int((want & ~got).sum()),
                                                   int((got & ~want).sum()))
    finally:
        b.close()


def test_sample_runs_use_actual_channel_numbers():
    """Samples on channels 1 and 3 (not 2) must not become '1~3'."""
    from cubevis.toolbox.visplot.flag_export import sample_lines
    from cubevis.toolbox.visplot.flag_model import SampleBlock, SpwKey
    key = SpwKey(0, "spw", 1e9, 1.007e9, 8)
    mask = np.zeros((1, 1, 2, 1), bool); mask[0, 0, :, 0] = True
    blk = SampleBlock.from_mask(key, np.array([1.6e9]), ["A"], ["B"],
                                np.array([1.001e9, 1.003e9]), np.array([1, 3]), ["XX"], mask)
    lines = sample_lines(FlagDelta(samples=[blk]), spw_ids={key: 0})
    spws = sorted(l.split("spw='")[1].split("'")[0] for l in lines)
    assert spws == ["0:1~1", "0:3~3"] or spws == ["0:1~1,0:3~3"], spws


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
        assert "exact, arcae" in opts["commit"] and "unavailable" in opts["commit_casa"]
        for d in _deltas(vp._reader._backend):
            f.db.add(d)
        resp = asyncio.run(f.handle_action({"action": "export", "kind": "commit"}))
        assert "arcae" in resp["preview"]["html"]
        resp = asyncio.run(f.handle_action({"action": "accept", "id": resp["preview"]["id"]}))
        assert resp["notify_text"].startswith("✓ Wrote") and "verified" in resp["notify_text"]
        assert len(f.db) == 0
    finally:
        vp.close()
