"""
test_flagdb_remote.py
=====================
FlagDB v2 through a real Jupyter kernel worker (``RemoteReductionContext``)
compared against the same calls on a local backend.

Every flag computation -- box resolution, filters, pending-flag application,
flag views, the InfoTool box probe -- must give the same answer in the
worker as in-process, and the plotter must drive it end to end with
``kernel_name=``.

Two data modes:

* **Real MS on a remote host** -- set ``MS`` (the path on this machine) and
  ``CUBEVIS_TEST_KERNEL_MS`` (the path of the SAME MS on the kernel's host).
  The local and remote backends then read the same data from their own
  paths, and every assertion compares remote with local (no data-specific
  counts).  Requests are restricted to the smallest field (override with
  ``CUBEVIS_TEST_FIELD``) to keep the run short.
* **Simulated MS** (neither variable set) -- builds the same simulated MSv2
  as ``test_flagdb_v2.py`` in a local temporary directory.  This only works
  with a kernel on THIS machine (e.g. ``python3``): a remote kernel cannot
  see a local temporary path, which shows up as "cannot start kernel ...
  create_object" skips.

Uses the kernel named by ``CUBEVIS_TEST_KERNEL`` (default ``python3``); skips
if ``jupyter_client`` is unavailable.  The kernel environment must be able to
import ``cubevis`` and ``xarray_ms``.

    pytest -q test_flagdb_remote.py                                   # local kernel
    MS=... CUBEVIS_TEST_KERNEL=cvpost140_python312 \
        CUBEVIS_TEST_KERNEL_MS=/path/on/host/... pytest -q test_flagdb_remote.py
"""
import asyncio
import dataclasses
import os
import warnings

import numpy as np
import pytest

pytest.importorskip("jupyter_client")

from cubevis.toolbox.visplot.flag_model import FlagDelta
from cubevis.toolbox.visplot.selection import SelectionSpec
from cubevis.toolbox.visplot.axes import Axis

warnings.filterwarnings("ignore")
KERNEL_NAME = os.environ.get("CUBEVIS_TEST_KERNEL", "python3")
CANON = ("time", "baseline_id", "frequency", "polarization")


def _transform(desc, data):
    ddid = int(desc.DATA_DESC_ID.item())
    rng = np.random.default_rng(1000 + ddid * 17 + int(desc.chunk_id))
    dims, vis = data["DATA"]
    shape = vis.shape
    v = (1.0 + 0.1 * rng.standard_normal(shape)) + 1j * (0.1 * rng.standard_normal(shape))
    for k in range(0, shape[0], 7):
        v[k, k % shape[1], 0] = 50.0
    data["DATA"] = (dims, v.astype(np.complex64))
    fdims, _ = data["FLAG"]
    f = np.zeros(shape, dtype=bool)
    f[0, :, 0] = True
    data["FLAG"] = (fdims, f)
    return data


@pytest.fixture(scope="module")
def ms_paths(tmp_path_factory):
    """``(local_path, kernel_path, simulated)``."""
    local_ms = os.environ.get("MS")
    kernel_ms = os.environ.get("CUBEVIS_TEST_KERNEL_MS")
    if local_ms and kernel_ms:
        if not os.path.exists(local_ms):
            pytest.skip(f"MS={local_ms!r} does not exist on this machine")
        return local_ms, kernel_ms, False
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("rms") / "flag_remote.ms")
    sim.MSStructureSimulator(
        ntime=10, nantenna=5, auto_corrs=False,
        data_description=[(8, ["XX", "XY", "YX", "YY"]), (4, ["XX", "YY"])],
        simulate_data=True, transform_data=_transform).simulate_ms(path)
    return path, path, True


@pytest.fixture(scope="module")
def ms_path(ms_paths):
    """Path as the KERNEL sees it (what remote calls are given)."""
    return ms_paths[1]


@pytest.fixture(scope="module")
def simulated(ms_paths):
    return ms_paths[2]


@pytest.fixture(scope="module")
def local(ms_paths):
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    from cubevis.toolbox.visplot.local_visibility_reader import LocalVisibilityReader
    b = MSv2Backend(ms_paths[0])
    b.open()
    yield LocalVisibilityReader(b)
    b.close()


def _kernel_skip(exc, ms_paths):
    """Skip with the reason FIRST (pytest -v truncates long reasons)."""
    local_ms, kernel_ms, simulated = ms_paths
    if simulated and KERNEL_NAME != "python3":
        pytest.skip(
            "expected without a shared filesystem: the simulated MS is created in a "
            f"local temporary directory that kernel {KERNEL_NAME!r} cannot see. Set MS "
            "(path here) and CUBEVIS_TEST_KERNEL_MS (path on the kernel host) to test "
            f"against a real MS present on both hosts. [{exc}]")
    if not simulated:
        pytest.skip(f"kernel {KERNEL_NAME!r} could not open CUBEVIS_TEST_KERNEL_MS="
                    f"{kernel_ms!r} -- check the path exists on the kernel host. [{exc}]")
    pytest.skip(f"cannot start kernel {KERNEL_NAME!r}: {exc}")


@pytest.fixture(scope="module")
def remote(ms_paths):
    from cubevis.toolbox.visplot.remote_reduction_context import RemoteReductionContext
    try:
        ctx = RemoteReductionContext(ms_paths[1], KERNEL_NAME, backend_kind="msv2")
    except Exception as exc:  # pragma: no cover - environment dependent
        _kernel_skip(exc, ms_paths)
    yield ctx
    ctx.close()


@pytest.fixture(scope="module")
def base_sel(local, simulated):
    """The selection every request uses: everything for the simulated MS,
    the smallest field (or ``CUBEVIS_TEST_FIELD``) for a real one."""
    if simulated:
        return SelectionSpec()
    field = os.environ.get("CUBEVIS_TEST_FIELD")
    if not field:
        sizes = {}
        for p in local._backend._iter_visibility_partitions():
            for f in set(str(x) for x in np.asarray(p.field_name.values)):
                sizes[f] = sizes.get(f, 0) + int(p.sizes["time"])
        field = min(sizes, key=sizes.get)
    return SelectionSpec(field_names=[field])


def _pol(local):
    p = next(iter(local._backend._iter_visibility_partitions()))
    pols = [str(x) for x in p.polarization.values]
    return "XX" if "XX" in pols else pols[0]


def _times(local, sel=None):
    b = local._backend
    for raw in b._iter_visibility_partitions(sel):
        p = b._apply_selection(raw, sel) if sel is not None else raw
        if p.sizes.get("time", 0):
            return p.time.values
    raise AssertionError("selection has no data")


def _eff(reader_backend, part):
    return reader_backend._flag_mask(part).transpose(*CANON).values


def _amp_threshold(local, sel, pol, simulated):
    """Lower amplitude edge for scatter boxes: the simulated outliers (10),
    or the 99.9th percentile of the selection's unflagged amplitudes."""
    if simulated:
        return 10.0
    vals = []
    for raw in local._backend._iter_visibility_partitions(sel):
        p = local._backend._apply_selection(raw, sel)
        if pol not in [str(x) for x in p.polarization.values]:
            continue
        v = np.abs(np.asarray(p.VISIBILITY.sel(polarization=pol).values))
        f = np.asarray(p.FLAG.sel(polarization=pol).values, dtype=bool)
        vals.append(v[~f & np.isfinite(v)])
    allv = np.concatenate(vals) if vals else np.array([1.0])
    return float(np.quantile(allv, 0.999))


def _reqs(local, sel, simulated):
    t = _times(local, sel)
    pol = _pol(local)
    lo = _amp_threshold(local, sel, pol, simulated)
    return {
        "raster": dict(flag=True, selection=sel, kind="raster", x_axis="TIME",
                       x0=t[min(2, len(t) - 1)], x1=t[min(4, len(t) - 1)],
                       y_axis="BASELINE", y0=0.6, y1=2.4, polarization=pol),
        "scatter": dict(flag=True, selection=sel, kind="scatter", x_axis="TIME",
                        x0=t.min() - 1, x1=t.max() + 1, y0=lo, y1=1e30,
                        layers=[{"y_axis": "AMPLITUDE", "polarization": pol}]),
        "zscore": dict(flag=True, selection=sel, kind="raster", x_axis="TIME",
                       x0=t.min(), x1=t.max(), y_axis="BASELINE", y0=-0.5, y1=1e6,
                       polarization=pol,
                       filter={"name": "zscore", "params": {"cutoff": 6.0}}),
    }


@pytest.mark.parametrize("which", ["raster", "scatter", "zscore"])
def test_evaluate_matches_local(local, remote, which, base_sel, simulated):
    req = _reqs(local, base_sel, simulated)[which]
    lr = local.evaluate_flag_request(dict(req))
    rr = remote.evaluate_flag_request(dict(req))
    assert rr["counts"]["n_matched"] == lr["counts"]["n_matched"]
    assert rr["counts"]["n_changed"] == lr["counts"]["n_changed"]
    assert (lr["delta"] is None) == (rr["delta"] is None)
    if lr["delta"] is None:          # e.g. nothing above the Z-Score cutoff in real data
        assert not simulated or which == "zscore"
        return
    ld, rd = FlagDelta.from_dict(lr["delta"]), FlagDelta.from_dict(rr["delta"])
    assert ld.is_sample_set == rd.is_sample_set
    # identical effect on every partition
    b = local._backend
    for raw in b._iter_visibility_partitions(base_sel):
        p = b._apply_selection(raw, base_sel)
        b.set_pending_flags([ld], 1)
        a = _eff(b, p)
        b.set_pending_flags([rd], 1)
        assert np.array_equal(a, _eff(b, p))
    b.set_pending_flags([], 0)


def test_pending_flags_applied_in_worker(local, remote, base_sel, simulated):
    pol = _pol(local)
    d = FlagDelta.from_dict(local.evaluate_flag_request(
        dict(_reqs(local, base_sel, simulated)["raster"]))["delta"])
    sel = dataclasses.replace(base_sel, correlation=[pol], pending_version=5)
    for rd in (local, remote):
        rd.set_pending_flags([d], 5)
    kw = dict(y_dim=Axis.BASELINE, x_dim=Axis.TIME, quantity=Axis.FLAG, selection=sel,
              polarization=pol)
    la, *_ = local.query_raster(**kw)
    ra, *_ = remote.query_raster(**kw)
    assert np.allclose(la.values, ra.values, equal_nan=True)
    for view in ("disk", "pending"):
        v = dataclasses.replace(sel, flag_view=view)
        la, *_ = local.query_raster(**dict(kw, selection=v))
        ra, *_ = remote.query_raster(**dict(kw, selection=v))
        assert np.allclose(la.values, ra.values, equal_nan=True), view
    for rd in (local, remote):
        rd.set_pending_flags([], 0)


def test_probe_flag_region_matches_local(local, remote, base_sel, simulated):
    req = dict(_reqs(local, base_sel, simulated)["scatter"])
    lp = local.probe_flag_region(dict(req))
    rp = remote.probe_flag_region(dict(req))
    assert rp["flag_n"] == lp["flag_n"] and rp["unflag_n"] == lp["unflag_n"]
    assert lp["flag_n"] > 0
    if simulated:
        assert lp["flag_n"] == 28
    for k in lp["layers"]:
        assert rp["layers"][k]["n_samples"] == lp["layers"][k]["n_samples"]


def test_spw_ids_and_table(local, remote):
    assert remote.spw_casa_ids() == local.spw_casa_ids()
    assert remote.flag_spw_table() == local.flag_spw_table()


def test_user_filter_refused_remotely(local, remote, base_sel, simulated):
    from cubevis.toolbox.visplot.flag_filters import make_flag_filter
    req = dict(_reqs(local, base_sel, simulated)["raster"], filter_obj=make_flag_filter(lambda ds: ds["amp"] > 1,
                                                                  name="u"))
    with pytest.raises(RuntimeError, match="only with local data"):
        remote.evaluate_flag_request(req)


def test_call_stats_separate_worker_time(remote):
    remote.call_stats(reset=True)
    remote.flag_spw_table()
    st = remote.call_stats()
    assert st["client"]["flag_spw_table"][0] == 1
    assert st["worker"]["flag_spw_table"][0] == 1
    assert st["overhead"]["flag_spw_table"] >= 0.0


def test_plotter_with_kernel_end_to_end(ms_paths, local, base_sel, simulated):
    from cubevis.toolbox.visplot import VisibilityPlotter
    kw = {} if simulated else {"field": base_sel.field_names[0]}
    lo = _amp_threshold(local, base_sel, _pol(local), simulated)
    try:
        vp = VisibilityPlotter(ms=ms_paths[1], kernel_name=KERNEL_NAME, layout="side",
                               correlation="XX,YY", **kw)
        assert type(vp._reader).__name__ == "RemoteReductionContext", \
            "kernel_name= must select the remote backend"
    except Exception as exc:  # pragma: no cover
        _kernel_skip(exc, ms_paths)
    try:
        r = vp._slots[0].raster
        x0, x1 = r._x_range; y0, y1 = r._y_range
        resp = asyncio.run(vp._handle_box_select(
            dict(x0=x0, x1=x1, y0=y0, y1=y0 + (y1 - y0) * 0.3, flag=True), "raster", r))
        assert resp["notify_text"].startswith("✓"), resp["notify_text"]
        assert len(vp.flag_db) == 1
        out = r._handle_rerender(dict(x0=x0, x1=x1, y0=y0, y1=y1))
        assert out["image"] is not None and not r._flag_stale
        sc = vp._slots[1].scatter
        sx0, sx1 = sc._x_range
        resp = asyncio.run(vp._handle_box_select(
            dict(x0=sx0, x1=sx1, y0=lo, y1=1e30, flag=True), "scatter", sc))
        assert resp["notify_text"].startswith("✓"), resp["notify_text"]
        asyncio.run(vp.flags.handle_action({"action": "undo"}))
        assert len(vp.flag_db) == 1
        assert "Pending operations" in vp.flags.report_html()
    finally:
        vp.close()


def test_incremental_pending_sync(local, remote, base_sel, simulated):
    reqs = _reqs(local, base_sel, simulated)
    pol = _pol(local)
    a = FlagDelta.from_dict(local.evaluate_flag_request(dict(reqs["raster"]))["delta"])
    b = FlagDelta.from_dict(local.evaluate_flag_request(dict(reqs["scatter"]))["delta"])
    kw = dict(y_dim=Axis.BASELINE, x_dim=Axis.TIME, quantity=Axis.FLAG, polarization=pol)

    def same(version, deltas):
        sel = dataclasses.replace(base_sel, correlation=[pol], pending_version=version)
        local.set_pending_flags(deltas, version)
        la, *_ = local.query_raster(selection=sel, **kw)
        ra, *_ = remote.query_raster(selection=sel, **kw)
        return np.allclose(la.values, ra.values, equal_nan=True)

    remote.set_pending_flags([a], 11)                 # full (or first sync)
    assert same(11, [a])
    remote.call_stats(reset=True)
    remote.set_pending_flags([a, b], 12)              # only b travels
    st = remote.call_stats()
    assert "sync_pending_flags" in st["client"] and "set_pending_flags" not in st["client"]
    assert same(12, [a, b])
    remote.set_pending_flags([a], 13)                 # undo: ids only
    assert same(13, [a])
    remote.__dict__["_cv_sent_ids"] = {"bogus"}       # client/worker out of step
    remote.set_pending_flags([a, b], 14)              # unknown id -> full resend
    assert same(14, [a, b])
    for rd in (local, remote):
        rd.set_pending_flags([], 0)


def test_large_results_pass_through_intact(local, remote, base_sel):
    """Worker-encoded results travel through the kernel untouched (raw frame
    segments, then a Jupyter binary buffer) and must decode to exactly the
    local result -- images and reference grids alike."""
    from cubevis.toolbox.visplot.data.reader import ScatterLayerSpec
    sel = base_sel
    layers = [ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization=p, cmap=("#000000", "#ffffff"))
              for p in _two_pols(local)]
    kw = dict(width=300, height=200, ref_scale=2.0, color_mode="global")
    lr = local.query_columns(Axis.TIME, layers, sel, **kw)
    rr = remote.query_columns(Axis.TIME, layers, sel, **kw)
    for a, b in zip(lr.layers, rr.layers):
        assert np.array_equal(a.image, b.image)
        if a.reference is not None:
            assert np.array_equal(np.asarray(a.reference.ref_agg.values),
                                  np.asarray(b.reference.ref_agg.values), equal_nan=True)


def _two_pols(local):
    p = next(iter(local._backend._iter_visibility_partitions()))
    return [str(x) for x in p.polarization.values][:2]


def test_frame_raw_segments_round_trip():
    import asyncio as _a
    from cubevis.remote import _worker_transport as wt

    async def go():
        big = "x" * 300_000 + '"quoted"\\n'
        msg = {"type": "response", "message_id": "m1",
               "message": {"__cv_pre_encoded__": big}, "other": [1, 2, 3]}
        reader = _a.StreamReader()

        class W:
            def __init__(self):
                self.buf = bytearray()

            def write(self, b):
                self.buf += b

            async def drain(self):
                pass
        w = W()
        await wt._write_frame(w, msg)
        await wt._write_frame(w, {"type": "response", "message_id": "m2", "message": 7})
        reader.feed_data(bytes(w.buf))
        reader.feed_eof()
        a = await wt._read_frame(reader)
        b = await wt._read_frame(reader)
        return a, b
    a, b = _a.run(go())
    assert a["message"]["__cv_pre_encoded__"].endswith('"quoted"\\n') and len(a["message"]["__cv_pre_encoded__"]) == 300_010
    assert a["other"] == [1, 2, 3] and b["message"] == 7


def test_coordinate_queries_memoised_but_follow_reload(remote):
    """axis_info / identity_tables are coordinate-only: asked once per
    selection and data generation, independent of flags and flag view."""
    import dataclasses as _dc
    sel = SelectionSpec()
    remote.call_stats(reset=True)
    a = remote.identity_tables(sel)
    b = remote.identity_tables(_dc.replace(sel, pending_version=9, flag_view="disk"))
    remote.axis_info(Axis.TIME, sel); remote.axis_info(Axis.TIME, sel)
    st = remote.call_stats()["client"]
    assert st["identity_tables"][0] == 1 and st["axis_info"][0] == 1 and a is b
    remote.identity_tables(_dc.replace(sel, cache_generation=1))      # Reload
    assert remote.call_stats()["client"]["identity_tables"][0] == 2
