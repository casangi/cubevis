"""
test_flag_commit_real.py
========================
Committing pending flags on REAL data -- the checks that close "commit on
real data / on the remote hosts" (FlagDB v2, flag_commit.py).

Local (needs ``MS`` and/or ``PS``; the data are COPIED to a temporary
directory first, the originals are never written):
  * an arcae (MSv2) / zarr (MSv4) commit of a raster box, a scatter box and
    an unflag of on-disk flags is verified sample by sample;
  * "Hide flagged" before the commit draws exactly what a fresh open of the
    committed data draws;
  * MSv2: one HISTORY row per commit and per restore;
  * restoring the backup gives back the original flags.

Remote (needs ``CUBEVIS_TEST_KERNEL`` and ``CUBEVIS_TEST_KERNEL_MS`` and/or
``CUBEVIS_TEST_KERNEL_PS`` -- the same variables as the other remote tests).
The worker COPIES that data set into a fresh temporary directory on the
kernel host (``tempfile.mkdtemp``, i.e. /tmp), the session is re-pointed at
the copy, and only the copy is written; it is deleted at the end:
  * the commit runs in the worker, is verified there, and the restore
    returns the original flags.

    MS=sis14_twhya_calibrated_flagged.ms PS=sis14_twhya_calibrated_flagged.ps.zarr \\
        pytest -q test_flag_commit_real.py
    CUBEVIS_TEST_KERNEL=cvpost140_python312 \\
        CUBEVIS_TEST_KERNEL_MS=/path/on/host/sis14_twhya_calibrated_flagged.ms \\
        pytest -q test_flag_commit_real.py
"""
import os
import shutil
import warnings

import numpy as np
import pytest

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.flag_model import FlagDelta
from cubevis.toolbox.visplot.selection import SelectionSpec
from cubevis.toolbox.visplot import flag_commit as fc

warnings.filterwarnings("ignore")
KERNEL = os.environ.get("CUBEVIS_TEST_KERNEL")


def _kinds():
    out = []
    if os.environ.get("MS"):
        out.append(("msv2", os.environ["MS"]))
    if os.environ.get("PS"):
        out.append(("msv4", os.environ["PS"]))
    return out or [pytest.param(None, None, marks=pytest.mark.skip(reason="set MS and/or PS"))]


def _backend(kind, path):
    if kind == "msv2":
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend as B
    else:
        from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend as B
    b = B(path)
    b.open()
    return b


def _smallest_field(reader):
    sizes = {}
    for p in reader._iter_visibility_partitions(None):
        for f in set(str(x) for x in np.asarray(p.field_name.values)):
            sizes[f] = sizes.get(f, 0) + int(p.sizes["time"])
    return min(sizes, key=sizes.get)


def _requests(rd, sel):
    """Raster box, scatter box (top 0.1% amplitudes) and an unflag of a
    region holding on-disk flags -- built from the data, not hard-coded."""
    b = getattr(rd, "_backend", rd)
    part = None
    for raw in b._iter_visibility_partitions(sel):
        part = b._apply_selection(raw, sel)
        if part.sizes.get("time", 0):
            break
    t = part.time.values
    pol = str(part.polarization.values[0])
    amp = np.abs(np.asarray(part.VISIBILITY.sel(polarization=pol).values))
    lo = float(np.nanquantile(amp[np.isfinite(amp)], 0.999))
    k = max(1, len(t) // 4)
    return [dict(flag=True, selection=sel, kind="raster", x_axis="TIME", x0=t[k], x1=t[2 * k],
                 y_axis="BASELINE", y0=0.6, y1=3.4, polarization=pol),
            dict(flag=True, selection=sel, kind="scatter", x_axis="TIME", x0=t.min() - 1,
                 x1=t.max() + 1, y0=lo, y1=1e30,
                 layers=[{"y_axis": "AMPLITUDE", "polarization": pol}]),
            dict(flag=False, selection=sel, kind="raster", x_axis="TIME", x0=t.min(),
                 x1=t.max(), y_axis="BASELINE", y0=-0.5, y1=1e6, polarization=pol)], pol


def _deltas(rd, reqs):
    out = []
    for r in reqs:
        d = rd.evaluate_flag_request(dict(r))["delta"]
        if d:
            out.append(FlagDelta.from_dict(d))
    return out


def _raster(rd, sel, pol, quantity):
    agg, *_ = rd.query_raster(y_dim=Axis.TIME, x_dim=Axis.CHANNEL, quantity=quantity,
                              selection=sel, polarization=pol)
    return np.asarray(agg.values)


@pytest.mark.parametrize("kind,path", _kinds())
def test_local_commit_on_real_data(kind, path, tmp_path):
    copy = str(tmp_path / os.path.basename(os.path.normpath(path)))
    shutil.copytree(path, copy)
    b = _backend(kind, copy)
    try:
        sel = SelectionSpec(field_names=[_smallest_field(b)])
        reqs, pol = _requests(b, sel)
        deltas = _deltas(b, reqs)
        assert deltas, "no flag operation matched any data"
        before = [np.asarray(b._disk_flag_mask(p).values).copy()
                  for p in b._iter_visibility_partitions(None)]
        # what "Hide flagged" shows with the operations pending
        b.set_pending_flags(deltas, 7)
        s7 = SelectionSpec(field_names=sel.field_names, pending_version=7)
        hide_amp = _raster(b, s7, pol, Axis.AMPLITUDE)
        b.set_pending_flags([], 0)
        n_hist = _history_rows(copy) if kind == "msv2" else None

        rep = fc.commit(b, deltas)
        assert rep["verified"] and rep["mismatches"] == 0, rep
        assert rep["written"] > 0 and rep["backup"], rep
        if kind == "msv2":
            assert rep["history"] and _history_rows(copy) == n_hist + 1
    finally:
        b.close()
    # a FRESH open of the committed data draws what "Hide flagged" drew
    b2 = _backend(kind, copy)
    try:
        fresh = _raster(b2, SelectionSpec(field_names=sel.field_names), pol, Axis.AMPLITUDE)
        assert np.allclose(fresh, hide_amp, equal_nan=True)
        fc.restore_backup(b2, rep["backup"])
        after = [np.asarray(b2._disk_flag_mask(p).values)
                 for p in b2._iter_visibility_partitions(None)]
        assert all(np.array_equal(x, y) for x, y in zip(before, after))
        if kind == "msv2":
            assert _history_rows(copy) == n_hist + 2
        assert any(e["path"] == rep["backup"] for e in fc.list_backups(b2))
    finally:
        b2.close()


def _history_rows(ms):
    from arcae.lib.arrow_tables import Table
    t = Table.from_filename(f"{ms}::HISTORY")
    try:
        return int(t.nrow())
    finally:
        t.close()


def _remote_data():
    out = []
    if os.environ.get("CUBEVIS_TEST_KERNEL_MS"):
        out.append(("msv2", os.environ["CUBEVIS_TEST_KERNEL_MS"]))
    if os.environ.get("CUBEVIS_TEST_KERNEL_PS"):
        out.append(("msv4", os.environ["CUBEVIS_TEST_KERNEL_PS"]))
    return out or [pytest.param(None, None, marks=pytest.mark.skip(
        reason="set CUBEVIS_TEST_KERNEL and CUBEVIS_TEST_KERNEL_MS and/or _PS "
               "(copied to a temporary directory on the kernel host; never written)"))]


@pytest.mark.parametrize("kind,path", _remote_data())
def test_remote_commit_on_real_data(kind, path):
    if not KERNEL:
        pytest.skip("set CUBEVIS_TEST_KERNEL")
    pytest.importorskip("jupyter_client")
    from cubevis.toolbox.visplot.remote_reduction_context import RemoteReductionContext
    rc = RemoteReductionContext(path, KERNEL, backend_kind=kind, call_timeout=3600)
    tmpdir = None
    try:
        # copy on the kernel host, then work on the copy only
        tmpdir = rc.eval_code("__import__('tempfile').mkdtemp(prefix='visplot_commit_test_')")
        scratch = rc.eval_code(f"__import__('os').path.join({tmpdir!r}, "
                               f"__import__('os').path.basename({path.rstrip('/')!r}))")
        rc.exec_code(f"import shutil\nshutil.copytree({path!r}, {scratch!r})\n_result = True")
        rc.reopen(scratch)
        _remote_commit_checks(rc, kind)
    finally:
        try:
            if tmpdir:
                rc.exec_code(f"import shutil\nshutil.rmtree({tmpdir!r}, ignore_errors=True)")
        finally:
            rc.close()


def _remote_commit_checks(rc, kind):
    caps = rc.flag_commit_capabilities()
    assert caps["write"], caps.get("write_reason")
    pol = (rc.metadata().get("correlation_labels") or ["XX"])[0]
    # the first field where the box changes some flags (a box over data that
    # is already flagged commits nothing -- and writes no backup)
    deltas, sel = [], SelectionSpec()
    for field in (rc.metadata().get("field_names") or [None]):
        sel = SelectionSpec(field_names=[field]) if field else SelectionSpec()
        # the box is built from the time coordinates the worker reports
        agg, *_ = rc.query_raster(y_dim=Axis.TIME, x_dim=Axis.CHANNEL, quantity=Axis.FLAG,
                                  selection=sel, polarization=pol)
        times = np.asarray(agg.coords["time"].values if "time" in agg.coords
                           else agg.coords[agg.dims[0]].values, dtype=float)
        if not times.size:
            continue
        t0, t1 = float(np.nanmin(times)), float(np.nanmax(times))
        req = dict(flag=True, selection=sel, kind="raster", x_axis="TIME",
                   x0=t0 + (t1 - t0) * 0.25, x1=t0 + (t1 - t0) * 0.5,
                   y_axis="BASELINE", y0=-0.5, y1=1e6, polarization=pol)
        res = rc.evaluate_flag_request(dict(req))
        if res["delta"] and res["counts"].get("n_changed", 0) > 0:
            deltas = [FlagDelta.from_dict(res["delta"])]
            break
    assert deltas, "no field where a flag box would change anything"
    before = _raster(rc, sel, pol, Axis.FLAG)
    rep = rc.commit_pending_flags(deltas)
    assert rep["verified"] and rep["mismatches"] == 0, rep
    assert rep["written"] > 0 and rep["backup"], rep
    rc.restore_flag_backup(rep["backup"])
    after = _raster(rc, SelectionSpec(field_names=sel.field_names, cache_generation=1),
                    pol, Axis.FLAG)
    assert np.allclose(before, after, equal_nan=True)
