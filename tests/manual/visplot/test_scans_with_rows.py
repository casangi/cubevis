"""
test_scans_with_rows.py
=======================
Issue #37: Scan Prev / Next skip scans with no data in the selection.

The reader's ``scans_with_rows`` names the scans with rows (finite UVW)
in the chosen field / SPWs / time range / UV range / autocorrelations,
ignoring the scan box and the Antenna / Baseline ticks (as
``baselines_with_rows`` ignores the antenna choice).  The plotter sends
it with the first page, every Plot reply and the Data inputs' "has data"
refresh; the JavaScript side is in ``test_scan_iteration.py``.

Location in repository:
    cubevis/tests/manual/visplot/test_scans_with_rows.py

Run:
    pytest cubevis/tests/manual/visplot/test_scans_with_rows.py -v

Data: a small simulated MSv2 (and its MSv4 zarr twin) with two fields --
scans 1-4 on FIELD-0, 11-14 on FIELD-1, 1000 s later -- and scan 3's rows
absent (UVW NaN, as xarray-ms pads rows that are not there).

What is NOT covered here: the remote kernel path end to end (it needs
``jupyter_client``; ``test_remote_reduction_context.py`` has that case),
and a real MS whose SPWs cover different scans (the simulator's windows
have no usable ids).
"""
from __future__ import annotations

import asyncio
import warnings

import numpy as np
import pytest

from cubevis_test_paths import ensure_cubevis_importable
ensure_cubevis_importable()

xr = pytest.importorskip("xarray")

from cubevis.toolbox.visplot.selection import SelectionSpec          # noqa: E402

warnings.filterwarnings("ignore")

T0, DUMP, NTIME = 5.0e9, 8.0, 16
ALL = ["1", "2", "4", "11", "12", "13", "14"]          # scan 3 has no rows


def _transform(desc, data):
    t = np.asarray(data["TIME"][1], dtype=np.float64)
    idx = np.floor((t - T0) / DUMP + 1e-6).astype(int)
    f = int(np.asarray(desc.FIELD_ID).ravel()[0])
    scan = 10 * f + 1 + idx // 4
    data["SCAN_NUMBER"] = (data["TIME"][0], scan.astype(np.int32))
    data["TIME"] = (data["TIME"][0], t + 1000.0 * f)
    if "TIME_CENTROID" in data:
        cd, c = data["TIME_CENTROID"]
        data["TIME_CENTROID"] = (cd, np.asarray(c, dtype=np.float64) + 1000.0 * f)
    ud, u = data["UVW"]
    u = np.asarray(u, dtype=np.float64).copy()
    u[scan == 3] = np.nan
    data["UVW"] = (ud, u)
    return data


@pytest.fixture(scope="module")
def sim_ms(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("scans") / "scans.ms")
    sim.MSStructureSimulator(
        ntime=NTIME, time_chunks=NTIME, dump_rate=DUMP, time_start=T0,
        nfield=2, nantenna=4, auto_corrs=False,
        data_description=[(4, ["XX", "YY"])],
        simulate_data=True, transform_data=_transform).simulate_ms(path)
    return path


@pytest.fixture(scope="module")
def sim_ps(sim_ms, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("scansps") / "scans.ps.zarr")
    dt = xr.open_datatree(sim_ms, engine="xarray-ms:msv2",
                          partition_schema=["FIELD_ID"])
    dt.to_zarr(out, mode="w", compute=True)
    return out


@pytest.fixture(params=["msv2", "msv4"])
def backend(request, sim_ms, sim_ps):
    if request.param == "msv2":
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
        b = MSv2Backend(sim_ms)
    else:
        from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
        b = MSv4Backend(sim_ps)
    b.open()
    yield b
    b.close()


def _scan_times(b, scan):
    """(first, last) time of *scan* (unix seconds, as the reader has them)."""
    ts = []
    for ds in b._iter_visibility_partitions(SelectionSpec()):
        m = np.asarray(ds.coords["scan_name"].values).astype(str) == scan
        ts.extend(np.asarray(ds.coords["time"].values, dtype=np.float64)[m].tolist())
    return min(ts), max(ts)


class TestReader:

    def test_scans_without_rows_are_left_out(self, backend):
        assert backend.scans_with_rows(SelectionSpec()) == ALL
        assert backend.scans_with_rows(None) == ALL

    def test_in_scan_order(self, backend):
        # "11" sorts after "4" (numbers numerically), not between 1 and 2.
        out = backend.scans_with_rows(SelectionSpec())
        assert out.index("4") < out.index("11")

    def test_the_field(self, backend):
        f1 = SelectionSpec(field_names=["FIELD-1"])
        assert backend.scans_with_rows(f1) == ["11", "12", "13", "14"]

    def test_the_time_range(self, backend):
        t0, _ = _scan_times(backend, "1")
        _, t1 = _scan_times(backend, "4")
        assert backend.scans_with_rows(SelectionSpec(time_range=(t0, t1))) == ["1", "2", "4"]
        _, t2 = _scan_times(backend, "2")
        assert backend.scans_with_rows(SelectionSpec(time_range=(t0, t2))) == ["1", "2"]

    def test_scan_and_antenna_choice_are_ignored(self, backend):
        sel = SelectionSpec(scan=["2"], antenna_names=["ANTENNA-0"],
                            baselines=[("ANTENNA-1", "ANTENNA-2")])
        assert backend.scans_with_rows(sel) == ALL

    def test_cached_per_data_selection(self, backend):
        backend.scans_with_rows(SelectionSpec())
        n = len(backend._scan_rows_cache)
        for s in ("1", "2", "4"):                 # stepping: no new reads
            backend.scans_with_rows(SelectionSpec(scan=[s]))
        assert len(backend._scan_rows_cache) == n

    def test_nothing_matched_is_empty_not_unknown(self, backend):
        assert backend.scans_with_rows(SelectionSpec(field_names=["NO-SUCH"])) == []

    def test_cannot_tell_is_none(self, backend):
        class _NoUVW:
            def __init__(self, inner):
                self._i = inner

            def __getattr__(self, k):
                return getattr(self._i, k)

        from cubevis.toolbox.visplot.data.reader import XArrayReader
        fake = _NoUVW(backend)
        fake._iter_visibility_partitions = (
            lambda sel: (ds.drop_vars("UVW") for ds in backend._iter_visibility_partitions(sel)))
        fake.__dict__["_scan_rows_cache"] = {}
        assert XArrayReader.scans_with_rows(fake, SelectionSpec()) is None

    def test_local_reader_passes_it_through(self, backend):
        from cubevis.toolbox.visplot.local_visibility_reader import LocalVisibilityReader
        r = LocalVisibilityReader.__new__(LocalVisibilityReader)
        r._backend = backend
        assert r.scans_with_rows(SelectionSpec()) == ALL


class TestRemoteMemo:
    """The remote context memoises without the scan / antenna choice, so
    stepping through the scans is no round trip (no kernel needed)."""

    def test_stepping_is_one_call(self):
        from cubevis.toolbox.visplot.remote_reduction_context import (
            RemoteReductionContext, DATA_METHODS, REPEATABLE_METHODS)
        assert "scans_with_rows" in DATA_METHODS and "scans_with_rows" in REPEATABLE_METHODS
        ctx = RemoteReductionContext.__new__(RemoteReductionContext)
        calls = []

        def _call(method, **kw):
            calls.append((method, kw["selection"]))
            return ["1", "2"]

        ctx._call = _call
        for s in ("1", "2", "4"):
            out = ctx.scans_with_rows(SelectionSpec(scan=[s], antenna_names=["A"]))
        assert out == ["1", "2"] and len(calls) == 1
        sent = calls[0][1]
        assert sent.scan is None and sent.antenna_names is None and sent.baselines is None
        ctx.scans_with_rows(SelectionSpec(field_names=["X"]))
        assert len(calls) == 2

    def test_the_registration_answers_strings(self):
        from cubevis.toolbox.visplot import remote_registrations as rr
        cls = next(c for c in vars(rr).values()
                   if isinstance(c, type) and "scans_with_rows" in vars(c))
        obj = cls.__new__(cls)
        obj._reader = type("R", (), {"scans_with_rows": lambda self, sel: [3, "7"]})()
        assert obj.scans_with_rows(None) == ["3", "7"]
        obj._reader = type("R", (), {"scans_with_rows": lambda self, sel: None})()
        assert obj.scans_with_rows(None) is None


# ---------------------------------------------------------------------------
# The plotter: first page, rows refresh, Plot reply
# ---------------------------------------------------------------------------

@pytest.fixture(params=["msv2", "msv4"])
def plotter(request, sim_ms, sim_ps):
    from cubevis.toolbox.visplot import VisibilityPlotter
    kw = dict(ms=sim_ms) if request.param == "msv2" else dict(ps=sim_ps)
    vp = VisibilityPlotter(layout="side", correlation="XX,YY", **kw)
    yield vp
    vp.close()


class TestPlotter:

    def test_first_page_carries_the_scans_with_data(self, plotter):
        assert plotter._scan_tab["has"] == ALL
        assert plotter._scan_tab["nos"] == [1, 2, 3, 4, 11, 12, 13, 14]

    def test_rows_refresh_answers_them(self, plotter):
        resp = asyncio.run(plotter._handle_rows({"field": "FIELD-1", "scan": "12"}))
        assert resp["status"] == "ok"
        assert resp["scan_has_data"] == ["11", "12", "13", "14"]

    def test_plot_reply_carries_them(self, plotter):
        resp = asyncio.run(plotter._handle_plot(_plot_message(plotter)))
        assert resp.get("status") != "error", resp
        assert resp.get("scan_has_data") == ALL

    def test_unknown_is_none(self, plotter, monkeypatch):
        def boom(sel):
            raise RuntimeError("old kernel")
        monkeypatch.setattr(plotter._reader, "scans_with_rows", boom)
        assert plotter._scans_with_data() is None


def _plot_message(vp):
    """A Plot request that changes nothing, as the browser would send it
    (as in test_raster_grid.py)."""
    W = vp._panel_axis_widgets
    return {"field": "", "correlation": "XX,YY", "datacolumn": "data",
            "reload": False,
            "spw_ids": [s.spw_id for s in vp._meta.spws],
            "antenna_names": [], "baselines": [], "both_ends_antennas": 0,
            "panels": {
                "A": {"kind": "raster",
                      "y": W["A"]["raster"]["y_sel"].value,
                      "x": W["A"]["raster"]["x_sel"].value,
                      "qty": W["A"]["raster"]["q_sel"].value,
                      "baseline_order": W["A"]["raster"]["border_sel"].value},
                "B": {"kind": "scatter",
                      "x": W["B"]["scatter"]["x_sel"].value,
                      "y": W["B"]["scatter"]["y_sel"].value,
                      "colorize": [None, None]}}}
