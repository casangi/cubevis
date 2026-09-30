"""
test_remote_flagging.py
=======================
FlagDB v2 through a real remote worker: every flag call made against a
``RemoteReductionContext`` must give exactly what the same call gives
against a local backend, and a remote ``VisibilityPlotter`` must flag,
undo, display and export exactly like a local one.

What runs where (the requirement being tested): box resolution, filters,
reference statistics, pending-flag application, the flag views used by
the display overlays and the InfoTool box probe all run in the WORKER; only
small deltas, counts and images cross the wire.  User-supplied Python
filters are refused remotely (``UserFilterNotRemoteError``).

Data
----
* ``MS`` (and/or ``PS``) set: that data set, opened locally from
  ``MS``/``PS`` and remotely from ``CUBEVIS_TEST_KERNEL_MS``/
  ``CUBEVIS_TEST_KERNEL_PS`` (falling back to ``MS``/``PS``) -- the same
  convention as ``test_remote_reduction_context.py``.  Queries are
  restricted to a short time window so a real MS stays fast.
* Neither set: a small simulated MSv2 (xarray-ms simulator) in a temp
  directory.  Only possible with a kernel on THIS host (the default
  ``python3`` kernel), since the worker must be able to open the file.

Kernel: ``CUBEVIS_TEST_KERNEL`` (default ``python3``).

Examples::

    pytest test_remote_flagging.py                                  # simulated MS, local kernel
    MS=sis14_twhya_calibrated_flagged.ms pytest test_remote_flagging.py
    MS=sis14_twhya_calibrated_flagged.ms CUBEVIS_TEST_KERNEL=cvpost106_python312 \\
        CUBEVIS_TEST_KERNEL_MS=/home/zuul06-2/dschieb/casa/visplot/sis14_twhya_calibrated_flagged.ms \\
        pytest test_remote_flagging.py
"""
from __future__ import annotations

import asyncio
import os
import warnings

import numpy as np
import pytest

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.flag_model import FlagDelta
from cubevis.toolbox.visplot.selection import SelectionSpec

warnings.filterwarnings("ignore")

KERNEL_NAME = os.environ.get("CUBEVIS_TEST_KERNEL", "python3")
_LOCAL_KERNEL = "CUBEVIS_TEST_KERNEL" not in os.environ or KERNEL_NAME == "python3"


def _kinds():
    kinds = []
    if os.environ.get("MS"):
        kinds.append("msv2")
    if os.environ.get("PS"):
        kinds.append("msv4")
    return kinds or ["simulated"]


def _simulate(path):
    sim = pytest.importorskip("xarray_ms.testing.simulator")

    def transform(desc, data):
        ddid = int(desc.DATA_DESC_ID.item())
        rng = np.random.default_rng(1000 + ddid * 17 + int(desc.chunk_id))
        dims, vis = data["DATA"]
        v = (1.0 + 0.1 * rng.standard_normal(vis.shape)) + 1j * 0.1 * rng.standard_normal(vis.shape)
        for k in range(0, vis.shape[0], 7):
            v[k, k % vis.shape[1], 0] = 50.0
        data["DATA"] = (dims, v.astype(np.complex64))
        fdims, _ = data["FLAG"]
        f = np.zeros(vis.shape, dtype=bool)
        f[0, :, 0] = True
        data["FLAG"] = (fdims, f)
        return data

    sim.MSStructureSimulator(
        ntime=10, nantenna=5, auto_corrs=False,
        data_description=[(8, ["XX", "XY", "YX", "YY"]), (4, ["XX", "YY"])],
        simulate_data=True, transform_data=transform).simulate_ms(path)
    return path


@pytest.fixture(scope="module", params=_kinds())
def data(request, tmp_path_factory):
    """(local_path, remote_path, backend_kind, selection) for one data set."""
    kind = request.param
    if kind == "simulated":
        if not _LOCAL_KERNEL:
            pytest.skip("simulated data needs a kernel on this host; set MS/PS for "
                        "a remote CUBEVIS_TEST_KERNEL")
        path = _simulate(str(tmp_path_factory.mktemp("rflag") / "sim.ms"))
        return path, path, "msv2", SelectionSpec()
    local_env, remote_env = ("MS", "CUBEVIS_TEST_KERNEL_MS") if kind == "msv2" \
        else ("PS", "CUBEVIS_TEST_KERNEL_PS")
    local = os.environ[local_env]
    remote = os.environ.get(remote_env, local)
    return local, remote, kind, None


@pytest.fixture(scope="module")
def local(data):
    local_path, _r, kind, _s = data
    from cubevis.toolbox.visplot.local_visibility_reader import LocalVisibilityReader
    if kind == "msv2":
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend as B
    else:
        from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend as B
    b = B(local_path)
    b.open()
    yield LocalVisibilityReader(b)
    b.close()


@pytest.fixture(scope="module")
def remote(data):
    from cubevis.toolbox.visplot.remote_reduction_context import RemoteReductionContext
    _l, remote_path, kind, _s = data
    ctx = RemoteReductionContext(remote_path, KERNEL_NAME, backend_kind=kind,
                                 call_timeout=300.0)
    yield ctx
    ctx.close()


@pytest.fixture(scope="module")
def sel(data, local):
    """The selection used everywhere: whole simulated MS, or the first
    minute of the first field of a real one (keeps full-selection filters
    and scatter boxes fast)."""
    s = data[3]
    if s is not None:
        return s
    field = local._backend.metadata()["field_names"][0]
    agg, _x, (t0, _t1), _d = local.query_raster(Axis.TIME, Axis.BASELINE, Axis.FLAG,
                                                SelectionSpec(field_names=[field]),
                                                polarization=None)
    return SelectionSpec(field_names=[field], time_range=(t0, t0 + 60.0))


def _pol(reader, sel):
    return str(reader.metadata()["correlations"][0]) \
        if "correlations" in reader.metadata() else "XX"


def _strip(d):
    """Delta dict without identity/time-of-creation fields."""
    d = FlagDelta.from_dict(d).to_dict(json_safe=True)
    for k in ("delta_id", "created", "seq"):
        d.pop(k, None)
    return d


def _raster_box(reader, sel, pol):
    agg, *_ = reader.query_raster(Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, sel,
                                  polarization=pol)
    t = agg.coords["time"].values
    # baselines that actually have data (a real MS pads missing ones)
    good = np.flatnonzero(np.isfinite(np.asarray(agg.transpose("time", "baseline_id").values))
                          .any(axis=0))
    b = agg.coords["baseline_id"].values[good]
    return dict(kind="raster", x_axis="BASELINE", y_axis="TIME",
                x0=float(b[0]) - 0.5, x1=float(b[min(2, b.size - 1)]) + 0.5,
                y0=float(t[min(1, t.size - 1)]), y1=float(t[min(3, t.size - 1)]),
                polarization=pol)


@pytest.fixture(scope="module")
def pol(local, sel):
    agg, *_ = local.query_raster(Axis.TIME, Axis.BASELINE, Axis.FLAG, sel, polarization=None)
    ds = next(iter(local._backend._iter_visibility_partitions(sel)))
    return str(ds.coords["polarization"].values[0])


# ---------------------------------------------------------------------- #
# Reader-level parity                                                      #
# ---------------------------------------------------------------------- #

def test_spw_tables_match(local, remote):
    assert remote.flag_spw_table() == local.flag_spw_table()
    assert {k.to_dict()["n_chan"]: v for k, v in remote.spw_casa_ids().items()} == \
        {k.to_dict()["n_chan"]: v for k, v in local.spw_casa_ids().items()}


@pytest.mark.parametrize("filt", [
    {"name": "all", "params": {}},
    {"name": "amplitude_range", "params": {"low": 20.0}},
    {"name": "zscore", "params": {"cutoff": 3.0, "reference": "selection"}},
    {"name": "amplitude_mad", "params": {"nsigma": 4.0}},
])
def test_raster_request_matches_local(local, remote, sel, pol, filt):
    req = dict(_raster_box(local, sel, pol), flag=True, selection=sel, filter=filt)
    a = local.evaluate_flag_request(dict(req))
    b = remote.evaluate_flag_request(dict(req))
    assert (a["delta"] is None) == (b["delta"] is None)
    assert a["counts"]["n_changed"] == b["counts"]["n_changed"]
    assert a["counts"]["n_selected"] == b["counts"]["n_selected"]
    if a["delta"] is not None:
        assert _strip(a["delta"]) == _strip(b["delta"])


def test_scatter_request_matches_local(local, remote, sel, pol):
    req = dict(flag=True, selection=sel, kind="scatter", x_axis="UVDIST", x0=0.0, x1=1e9,
               y0=0.0, y1=1e9, layers=[{"y_axis": "AMPLITUDE", "polarization": pol}],
               filter={"name": "amplitude_range", "params": {"low": 5.0}})
    a = local.evaluate_flag_request(dict(req))
    b = remote.evaluate_flag_request(dict(req))
    assert a["counts"]["n_changed"] == b["counts"]["n_changed"]
    if a["delta"] is not None:
        assert _strip(a["delta"]) == _strip(b["delta"])


def test_pending_flags_and_views_render_identically(local, remote, sel, pol):
    req = dict(_raster_box(local, sel, pol), flag=True, selection=sel,
               filter={"name": "all", "params": {}})
    d = FlagDelta.from_dict(local.evaluate_flag_request(dict(req))["delta"])
    prop = FlagDelta.from_dict(local.evaluate_flag_request(dict(req, flag=False))["delta"])
    for rd in (local, remote):
        rd.set_pending_flags([d], 5, True, prop)
    try:
        for view in ("effective", "disk", "pending", "proposal"):
            s = SelectionSpec(**{**sel.__dict__, "pending_version": 5, "flag_view": view})
            la, *_ = local.query_raster(Axis.TIME, Axis.BASELINE, Axis.FLAG, s, polarization=pol)
            ra, *_ = remote.query_raster(Axis.TIME, Axis.BASELINE, Axis.FLAG, s, polarization=pol)
            np.testing.assert_array_equal(np.asarray(la.values), np.asarray(ra.values),
                                          err_msg=f"flag view {view}")
        eff = SelectionSpec(**{**sel.__dict__, "pending_version": 5})
        disk = SelectionSpec(**{**sel.__dict__, "pending_version": 5, "flag_view": "disk"})
        ea, *_ = remote.query_raster(Axis.TIME, Axis.BASELINE, Axis.FLAG, eff, polarization=pol)
        da, *_ = remote.query_raster(Axis.TIME, Axis.BASELINE, Axis.FLAG, disk, polarization=pol)
        assert np.nansum(ea.values) > np.nansum(da.values)      # pending flags applied remotely
    finally:
        for rd in (local, remote):
            rd.set_pending_flags([], 0)


def test_probe_flag_region_matches_local(local, remote, sel, pol):
    req = dict(selection=sel, x_axis="UVDIST", x0=0.0, x1=1e9, y0=5.0, y1=1e9,
               layers=[{"y_axis": "AMPLITUDE", "polarization": pol}])
    a = local.probe_flag_region(dict(req))
    b = remote.probe_flag_region(dict(req))
    assert (a["flag_n"], a["unflag_n"]) == (b["flag_n"], b["unflag_n"])
    for k in a["layers"]:
        assert a["layers"][k]["n_samples"] == b["layers"][k]["n_samples"]


def test_user_filter_is_refused_remotely(remote, local, sel, pol):
    from cubevis.toolbox.visplot.flag_filters import make_flag_filter
    from cubevis.toolbox.visplot.remote_reduction_context import UserFilterNotRemoteError
    f = make_flag_filter(lambda ds, level=1.0: ds["amp"] > level, name="loud")
    req = dict(_raster_box(local, sel, pol), flag=True, selection=sel,
               filter={"name": "loud", "params": {}}, filter_obj=f)
    with pytest.raises(UserFilterNotRemoteError):
        remote.evaluate_flag_request(req)


# ---------------------------------------------------------------------- #
# Plotter-level parity (headless: the GUI handlers without a browser)      #
# ---------------------------------------------------------------------- #

def _plotter(path, remote, kind="msv2", field=None, **extra):
    from cubevis.toolbox.visplot import VisibilityPlotter
    kw = dict(backend="remote", kernel_name=KERNEL_NAME) if remote else {}
    if field:
        kw["field"] = field
    data_kw = {"ms": path} if kind == "msv2" else {"ps": path}
    return VisibilityPlotter(headless=True, layout="side", **data_kw, **kw, **extra)


def _smallest_field(local):
    """Smallest field of a real data set (keeps plotter parity runs short);
    ``CUBEVIS_TEST_FIELD`` overrides."""
    if os.environ.get("CUBEVIS_TEST_FIELD"):
        return os.environ["CUBEVIS_TEST_FIELD"]
    sizes = {}
    for p in local._backend._iter_visibility_partitions(None):
        if "field_name" not in p.coords:
            continue
        for f in set(str(x) for x in np.asarray(p.field_name.values)):
            sizes[f] = sizes.get(f, 0) + int(p.sizes["time"])
    return min(sizes, key=sizes.get) if sizes else None


@pytest.fixture(scope="module")
def plotters(data, local):
    """Local and remote plotters on the same data: the simulated MS, or the
    real MS/PS given by MS/PS + CUBEVIS_TEST_KERNEL_MS/_PS (restricted to its
    smallest field)."""
    local_path, remote_path, kind, s = data
    field = None if s is not None else _smallest_field(local)
    lp = _plotter(local_path, False, kind, field)
    rp = _plotter(remote_path, True, kind, field)
    yield lp, rp
    lp.close()
    rp.close()


def _flag_both(plotters, kind, frac=(0.0, 1.0, 0.0, 0.3), **msg):
    out = []
    for vp in plotters:
        obj = vp._slots[0].raster if kind == "raster" else vp._slots[1].scatter
        x0, x1 = obj._x_range
        y0, y1 = obj._y_range
        box = dict(x0=x0 + (x1 - x0) * frac[0], x1=x0 + (x1 - x0) * frac[1],
                   y0=y0 + (y1 - y0) * frac[2], y1=y0 + (y1 - y0) * frac[3], flag=True)
        box.update(msg)
        out.append(asyncio.run(vp._handle_box_select(box, kind, obj)))
    return out


def test_remote_plotter_flags_undo_export_like_local(plotters, tmp_path):
    lp, rp = plotters
    assert type(rp._reader).__name__ == "RemoteReductionContext"
    a, b = _flag_both(plotters, "raster")
    assert a["notify_text"] == b["notify_text"] and a["notify_text"].startswith("✓")
    a, b = _flag_both(plotters, "scatter", frac=(0.0, 1.0, 0.1, 1.0))
    assert a["notify_text"] == b["notify_text"]
    for vp in plotters:
        asyncio.run(vp.flags.handle_action({"action": "config", "filter": "zscore",
                                            "params": {"cutoff": 3.0}}))
    a, b = _flag_both(plotters, "raster", frac=(0.0, 1.0, 0.0, 1.0))
    assert a["notify_text"] == b["notify_text"]
    for vp in plotters:
        asyncio.run(vp.flags.handle_action({"action": "undo"}))
        asyncio.run(vp.flags.handle_action({"action": "config", "filter": "all"}))
    # two or three operations were added (the Z-Score box may match nothing
    # on real data), one undone -- identically on both sides
    assert len(lp.flag_db) == len(rp.flag_db) >= 1
    lf = open(lp.export_flags(str(tmp_path / "l.txt"))).read().splitlines()[1:]
    rf = open(rp.export_flags(str(tmp_path / "r.txt"))).read().splitlines()[1:]
    assert lf == rf
    # display: a colour-mode re-render and the InfoTool box work remotely
    asyncio.run(rp.flags.handle_action({"action": "config", "display": "color"}))
    sc = rp._slots[1].scatter
    img = sc._handle_rerender(dict(x0=sc._x_range[0], x1=sc._x_range[1],
                                   y0=sc._y_range[0], y1=sc._y_range[1]))["image"]
    assert img.ndim == 2
    info = asyncio.run(sc._handle_probe_region(dict(tool="info_box", x0=sc._x_range[0],
                                                    x1=sc._x_range[1], y0=0, y1=1e9)))
    assert info["status"] == "ok" and "Flag box here" in info["info_html"]


def test_remote_plotter_refuses_user_filter_cleanly(data, local):
    local_path, remote_path, kind, s = data
    field = None if s is not None else _smallest_field(local)
    vp = _plotter(remote_path, True, kind, field,
                  flag_filters={"loud": lambda ds, level=1.0: ds["amp"] > level})
    try:
        asyncio.run(vp.flags.handle_action({"action": "config", "filter": "loud"}))
        r = vp._slots[0].raster
        resp = asyncio.run(vp._handle_box_select(
            dict(x0=r._x_range[0], x1=r._x_range[1], y0=r._y_range[0], y1=r._y_range[1],
                 flag=True), "raster", r))
        assert "local data" in resp["notify_text"] and len(vp.flag_db) == 0
    finally:
        vp.close()
