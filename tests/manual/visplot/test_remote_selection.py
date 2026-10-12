"""
test_remote_selection.py
========================
The features added in H8 (2026-10-11) through a real remote worker: the
selection fields that travel to the kernel (``uv_range``, ``autocorr``,
``time_range``, ``scan``), the "rows with data" refresh behind stepping,
the plot summary and its reproducing call, UTC / elapsed time labels,
``panels=[...]``, the integration time in the metadata, and the version
check between the local plotter and the kernel.

Every remote result is compared with the same call made locally.

Data and kernel: the same conventions as ``test_remote_flagging.py``
(``MS`` / ``PS`` with ``CUBEVIS_TEST_KERNEL_MS`` / ``_PS``, else a small
simulated MSv2 with autocorrelations; ``CUBEVIS_TEST_KERNEL``, default the
local ``python3`` kernel).  See ``REMOTE_TESTING.md``.

    pytest test_remote_selection.py
    MS=sis14_twhya_calibrated_flagged.ms pytest test_remote_selection.py
"""
from __future__ import annotations

import asyncio
import os
import re
import warnings

import numpy as np
import pytest

from cubevis.toolbox.visplot.axes import Axis
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
        # Known flags (the simulator's own are random, ~half): only the
        # first integration is flagged.
        dims, vis = data["DATA"]
        rng = np.random.default_rng(7 + int(desc.chunk_id))
        v = (1.0 + 0.1 * rng.standard_normal(vis.shape)) + 1j * 0.1 * rng.standard_normal(vis.shape)
        data["DATA"] = (dims, v.astype(np.complex64))
        fdims, _ = data["FLAG"]
        f = np.zeros(vis.shape, dtype=bool)
        f[0] = True
        data["FLAG"] = (fdims, f)
        return data

    sim.MSStructureSimulator(
        ntime=10, nantenna=5, auto_corrs=True,
        data_description=[(8, ["XX", "YY"]), (4, ["XX", "YY"])],
        simulate_data=True, transform_data=transform).simulate_ms(path)
    return path


@pytest.fixture(scope="module", params=_kinds())
def data(request, tmp_path_factory):
    """(local_path, remote_path, kind, field) for one data set."""
    kind = request.param
    if kind == "simulated":
        if not _LOCAL_KERNEL:
            pytest.skip("simulated data needs a kernel on this host")
        path = _simulate(str(tmp_path_factory.mktemp("rsel") / "sim.ms"))
        return path, path, "msv2", None
    local_env, remote_env = (("MS", "CUBEVIS_TEST_KERNEL_MS") if kind == "msv2"
                             else ("PS", "CUBEVIS_TEST_KERNEL_PS"))
    local = os.environ[local_env]
    return local, os.environ.get(remote_env, local), kind, \
        os.environ.get("CUBEVIS_TEST_FIELD", "3c279")


def _backend(kind, path):
    if kind == "msv2":
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend as B
    else:
        from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend as B
    return B(path)


@pytest.fixture(scope="module")
def local(data):
    from cubevis.toolbox.visplot.local_visibility_reader import LocalVisibilityReader
    b = _backend(data[2], data[0])
    b.open()
    yield LocalVisibilityReader(b)
    b.close()


@pytest.fixture(scope="module")
def remote(data):
    from cubevis.toolbox.visplot.remote_reduction_context import RemoteReductionContext
    ctx = RemoteReductionContext(data[1], KERNEL_NAME, backend_kind=data[2],
                                 call_timeout=300.0)
    yield ctx
    ctx.close()


def _base(data, local):
    """The selection the cases narrow: the field under test, the first
    half of its time range."""
    field = data[3]
    fsel = SelectionSpec(field_names=[field] if field else None)
    # The field's own time span (a field may cover a small part of the
    # observation: 3c279 in TW Hya).
    _agg, _x, (t0, t1), _d = local.query_raster(Axis.TIME, Axis.BASELINE, Axis.FLAG,
                                                fsel, polarization=None)
    return SelectionSpec(field_names=fsel.field_names,
                         time_range=(t0, t0 + 0.5 * (t1 - t0)))


def _cases(base, meta):
    import dataclasses as dc
    out = {
        "uv metres": dc.replace(base, uv_range=[[None, 300.0, "m"]]),
        "uv wavelengths": dc.replace(base, uv_range=[[100.0, None, "lambda"]]),
        "uv two ranges": dc.replace(base, uv_range=[[None, 100.0, "m"], [300.0, None, "m"]]),
        "autocorr exclude": dc.replace(base, autocorr="exclude"),
        "autocorr only": dc.replace(base, autocorr="only"),
        "time range": base,
    }
    scans = meta.get("scan_names") or []
    if scans:
        out["scan"] = dc.replace(base, scan=[scans[0]])
    return out


def _same_raster(a, b):
    ga, gb = np.asarray(a[0], dtype=float), np.asarray(b[0], dtype=float)
    assert ga.shape == gb.shape
    assert np.array_equal(np.isnan(ga), np.isnan(gb))
    np.testing.assert_allclose(ga[~np.isnan(ga)], gb[~np.isnan(gb)], rtol=1e-6)


def test_selection_fields_reach_the_worker(data, local, remote):
    """uv_range / autocorr / time_range / scan: the same raster and the
    same "rows with data" remotely as locally."""
    meta = local._backend.metadata()
    for name, sel in _cases(_base(data, local), meta).items():
        a = local.query_raster(Axis.BASELINE, Axis.TIME, Axis.AMPLITUDE, sel,
                               polarization="XX")
        b = remote.query_raster(Axis.BASELINE, Axis.TIME, Axis.AMPLITUDE, sel,
                                polarization="XX")
        _same_raster(a, b)
        assert sorted(map(tuple, local.baselines_with_rows(sel) or [])) == \
            sorted(map(tuple, remote.baselines_with_rows(sel) or [])), name


def test_uv_range_narrows_remotely(data, local, remote):
    """The UV range really removes rows in the worker (not just accepted):
    fewer raster cells have data."""
    import dataclasses as dc
    base = _base(data, local)

    def cells(sel):
        g = np.asarray(remote.query_raster(Axis.BASELINE, Axis.TIME, Axis.AMPLITUDE,
                                           sel, polarization="XX")[0], float)
        return int(np.isfinite(g).sum())
    every = cells(base)
    short = cells(dc.replace(base, uv_range=[[None, 150.0, "m"]]))
    assert 0 < short < every


def test_flag_box_under_uv_range_matches_local(data, local, remote):
    """A raster box under a UV range is stored as samples in the worker too,
    with the same counts."""
    import dataclasses as dc
    from cubevis.toolbox.visplot.flag_model import FlagDelta
    sel = dc.replace(_base(data, local), uv_range=[[None, 300.0, "m"]], correlation=["XX"])
    t0, t1 = sel.time_range
    req = {"flag": True, "selection": sel, "kind": "raster",
           "x_axis": "TIME", "x0": t0 - 1, "x1": t1 + 1,
           "y_axis": "BASELINE", "y0": -1.0, "y1": 1e6, "polarization": "XX"}
    a = local.evaluate_flag_request(dict(req))
    b = remote.evaluate_flag_request(dict(req))
    assert a["counts"]["n_changed"] == b["counts"]["n_changed"] > 0
    assert FlagDelta.from_dict(b["delta"]).is_sample_set
    assert any("UV range" in w for w in b["warnings"])


def test_metadata_carries_integration_time(data, local, remote):
    assert remote.metadata()["integration_time"] == \
        pytest.approx(local._backend.metadata()["integration_time"])
    assert remote.metadata()["integration_time"] > 0


def test_worker_software_versions(remote):
    from cubevis.toolbox.visplot.plot_summary import software_versions, version_mismatch
    kv = dict(remote.software_versions())
    assert "cubevis" in kv and "python" in kv
    if _LOCAL_KERNEL:
        # Same installation on both sides: no warning.
        assert version_mismatch(software_versions(), list(kv.items())) is None


def test_timeout_message_and_recovery(data, local, remote):
    """A call that runs out of time says which call, how long, and what to
    do; the next call still works (2026-10-12: it was a bare TimeoutError)."""
    base = _base(data, local)
    with pytest.raises(TimeoutError) as ei:
        remote._call("query_raster", timeout=1e-3, y_dim=Axis.BASELINE,
                     x_dim=Axis.TIME, quantity=Axis.AMPLITUDE, selection=base,
                     polarization=None, max_cells=2_000_000)
    msg = str(ei.value)
    assert "query_raster" in msg and KERNEL_NAME in msg and "call_timeout" in msg
    # The worker finishes the abandoned call; the next one is answered.
    assert remote.metadata()["integration_time"] > 0
    g = remote.query_raster(Axis.BASELINE, Axis.TIME, Axis.AMPLITUDE, base)[0]
    assert np.isfinite(np.asarray(g, float)).any()


# ---------------------------------------------------------------------- #
# Plotter level: local and remote headless plotters, same arguments        #
# ---------------------------------------------------------------------- #

def _plotter(data, remote_side, **kw):
    from cubevis.toolbox.visplot import VisibilityPlotter
    path = data[1] if remote_side else data[0]
    extra = dict(backend="remote", kernel_name=KERNEL_NAME) if remote_side else {}
    if data[3]:
        extra["field"] = data[3]
    src = {"ms": path} if data[2] == "msv2" else {"ps": path}
    return VisibilityPlotter(headless=True, layout="over", **src, **extra, **kw)


@pytest.fixture(scope="module")
def plotters(data):
    kw = dict(uvrange="<300m", autocorr="include", time_labels="elapsed",
              panels=[dict(kind="raster", raster_y="TIME", raster_x="CHANNEL",
                           raster_qty="PHASE"),
                      dict(kind="scatter", scatter_x="CHANNEL",
                           scatter_y="AMPLITUDE", scatter_avg_time="scan")])
    lp, rp = _plotter(data, False, **kw), _plotter(data, True, **kw)
    yield lp, rp
    lp.close()
    rp.close()


def _strip_location(call: str) -> str:
    """The reproducing call without what legitimately differs: the data
    path and the remote arguments."""
    keep = [l for l in call.splitlines()
            if not re.match(r"\s+(ms|ps|backend|kernel_name)=", l)]
    return "\n".join(keep)


def test_plotters_render_the_same(plotters):
    lp, rp = plotters
    for a, b in zip(lp._slots, rp._slots):
        assert a.kind == b.kind
        if a.kind == "raster":
            ga = np.asarray(a.raster._agg, float)
            gb = np.asarray(b.raster._agg, float)
            assert ga.shape == gb.shape
            assert np.array_equal(np.isnan(ga), np.isnan(gb))


def test_plotter_gets_the_data_timeout(plotters):
    """The plotter passes no call_timeout=: data calls get the long budget
    (600 s, or CUBEVIS_REMOTE_TIMEOUT), small calls keep 30 s."""
    from cubevis.toolbox.visplot import remote_reduction_context as rrc
    _lp, rp = plotters
    ctx = next(o for o in (rp._context, rp._reader)
               if type(o).__name__ == "RemoteReductionContext")
    assert ctx._call_timeout == rrc.DEFAULT_CALL_TIMEOUT
    assert ctx._data_call_timeout == rrc._data_call_timeout()
    assert {"query_raster", "query_columns", "evaluate_flag_request"} <= rrc.DATA_METHODS


def test_reproducing_call_names_the_kernel(plotters):
    lp, rp = plotters
    lc, rc = lp.reproducing_call(), rp.reproducing_call()
    assert "backend='remote'" in rc and f"kernel_name={KERNEL_NAME!r}" in rc
    assert "backend=" not in lc
    assert _strip_location(lc) == _strip_location(rc)


def test_summary_page_remote(plotters):
    _lp, rp = plotters
    d = rp.describe("A")
    sections = dict(d["sections"])
    assert dict(sections["Data set"])["Execution"] == "remote kernel"
    assert dict(sections["Data set"])["Kernel"] == KERNEL_NAME
    assert "Software on the kernel" in sections
    assert dict(sections["Selection"])["UV range"] == "<300m"
    html = rp.describe("B", html=True)
    assert "Software on the kernel" in html


def test_time_labels_identical(plotters):
    lp, rp = plotters
    for mode in ("elapsed", "utc"):
        labels = []
        for vp in (lp, rp):
            vp.set_time_labels(mode)
            spec = vp._slots[0].raster._panel_spec()
            labels.append((spec.axis_label("y"), spec.to_state_data()["time_labels"]))
        assert labels[0] == labels[1]


def test_rows_refresh_identical(plotters):
    """The Data-section refresh behind stepping (``_handle_rows``)."""
    lp, rp = plotters
    for msg in ({}, {"uvrange": "<100m"}, {"autocorr": "only"}, {"uvrange": "bad unit~x"}):
        a = asyncio.run(lp._handle_rows(dict(msg)))
        b = asyncio.run(rp._handle_rows(dict(msg)))
        assert a == b


def test_single_time_uses_integration_time_remotely(plotters):
    lp, rp = plotters
    assert rp._integration_half() == pytest.approx(lp._integration_half())
    assert rp._integration_half() != 0.5 or lp._integration_half() == 0.5


def test_version_check_quiet_when_same(plotters):
    _lp, rp = plotters
    assert rp._kernel_versions
    if _LOCAL_KERNEL:
        assert rp._version_warning is None


# ---------------------------------------------------------------------- #
# The version comparison itself (no kernel)                                #
# ---------------------------------------------------------------------- #

@pytest.mark.parametrize("local_v,remote_v,warn", [
    ("1.0.92.dev37+g5c0d466 (git 5c0d46683b)", "1.0.92.dev37+g5c0d466", None),
    ("1.0.92.dev37+g5c0d466 (git 5c0d46683b)", "1.0.80 (git 5c0d46683b)", None),
    ("1.0.92.dev37+g5c0d466 (git c89335a1b2)", "1.0.92.dev37+g5c0d466", "differs"),
    ("1.0.92", "1.0.92", None),
    ("1.0.92", "1.0.91", "differs"),
    ("1.0.92 (git c89335a1b2+dirty)", "1.0.92 (git c89335a1b2)", "uncommitted"),
    ("1.0.92", "unknown (older than 2026-10-12)", "unknown"),
    ("1.0.92 (git c89335a1b2+dirty)", "1.0.92 (git c89335a1b2+dirty)", None),
])
def test_version_mismatch(local_v, remote_v, warn):
    from cubevis.toolbox.visplot.plot_summary import version_mismatch
    got = version_mismatch([("cubevis", local_v)], [("cubevis", remote_v)])
    if warn is None:
        assert got is None
    else:
        assert got is not None and warn in got
