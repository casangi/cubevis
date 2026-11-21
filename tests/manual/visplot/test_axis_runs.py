"""
test_axis_runs.py
=================
Issue #36 part 1: the closed axis comes from the selection's runs.

The reader's ``axis_runs(selection, dim)`` gives the stretches of Time
(or Frequency) with data at full resolution; a raster with its time gaps
closed lays out ``GapAxis.from_runs`` of them, so a decimated full view
and a finer query for a zoomed viewport share one closed axis -- zooming
in with the gaps closed now re-queries, where debug-0003 magnified the
full aggregate.

Location in repository:
    cubevis/tests/manual/visplot/test_axis_runs.py

Run:
    pytest cubevis/tests/manual/visplot/test_axis_runs.py -v

Data: a simulated MSv2 (and its MSv4 zarr twin): FIELD-0 observed for 32
integrations of 8 s in two 16-integration scans 600 s apart, FIELD-1 in
between.  What is NOT covered: a real MS (TW Hya runs on your side), and
anything only a browser shows.
"""
from __future__ import annotations

import warnings

import numpy as np
import pytest

from cubevis_test_paths import ensure_cubevis_importable
ensure_cubevis_importable()

xr = pytest.importorskip("xarray")

from cubevis.toolbox.gridplot.raster_grid import GapAxis                  # noqa: E402
from cubevis.toolbox.visplot.axes import Axis                             # noqa: E402
from cubevis.toolbox.visplot.selection import SelectionSpec               # noqa: E402

warnings.filterwarnings("ignore")

T0, DUMP, NTIME, NCHAN = 5.0e9, 8.0, 32, 16
PAUSE = 600.0


def _transform(desc, data):
    """FIELD-0: integrations 0-15 (scan 1) and, after a 600 s pause,
    16-31 (scan 3); FIELD-1 (scan 2) sits in the pause."""
    t = np.asarray(data["TIME"][1], dtype=np.float64)
    idx = np.floor((t - T0) / DUMP + 1e-6).astype(int)
    f = int(np.asarray(desc.FIELD_ID).ravel()[0])
    if f == 0:
        new = t + np.where(idx >= 16, PAUSE, 0.0)
        scan = np.where(idx >= 16, 3, 1)
    else:
        new = T0 + 16 * DUMP + 100.0 + idx * DUMP           # in the pause
        scan = np.full(idx.shape, 2)
    data["TIME"] = (data["TIME"][0], new)
    if "TIME_CENTROID" in data:
        data["TIME_CENTROID"] = (data["TIME_CENTROID"][0], new)
    data["SCAN_NUMBER"] = (data["TIME"][0], scan.astype(np.int32))
    data["FLAG"] = (data["FLAG"][0], np.zeros_like(data["FLAG"][1]))
    return data


@pytest.fixture(scope="module")
def sim_ms(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("runs") / "runs.ms")
    sim.MSStructureSimulator(
        ntime=NTIME, time_chunks=NTIME, dump_rate=DUMP, time_start=T0,
        nfield=2, nantenna=4, auto_corrs=False,
        data_description=[(NCHAN, ["XX", "YY"])],
        simulate_data=True, transform_data=_transform).simulate_ms(path)
    return path


@pytest.fixture(scope="module")
def sim_ps(sim_ms, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("runsps") / "runs.ps.zarr")
    dt = xr.open_datatree(sim_ms, engine="xarray-ms:msv2", partition_schema=["FIELD_ID"])
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


F0 = SelectionSpec(field_names=["FIELD-0"])


def _times(b, sel):
    out = []
    for ds in b._iter_visibility_partitions(sel):
        out.extend(np.asarray(b._apply_selection(ds, sel).coords["time"].values).tolist())
    return np.unique(out)


class TestReader:

    def test_two_scans_two_runs(self, backend):
        runs = backend.axis_runs(F0, "time")
        t = _times(backend, F0)
        assert len(runs) == 2
        assert runs[0] == pytest.approx([t[0] - DUMP / 2, t[15] + DUMP / 2], rel=0, abs=1e-6)
        assert runs[1] == pytest.approx([t[16] - DUMP / 2, t[31] + DUMP / 2], rel=0, abs=1e-6)

    def test_both_fields_fill_the_pause(self, backend):
        runs = backend.axis_runs(SelectionSpec(), "time")
        # FIELD-1 sits 100 s after scan 1 and ends before scan 3
        assert len(runs) == 3

    def test_the_scan_and_time_range(self, backend):
        assert len(backend.axis_runs(SelectionSpec(field_names=["FIELD-0"], scan=["3"]), "time")) == 1
        t = _times(backend, F0)
        runs = backend.axis_runs(SelectionSpec(field_names=["FIELD-0"],
                                               time_range=(t[2], t[5])), "time")
        assert len(runs) == 1
        assert runs[0] == pytest.approx([t[2] - DUMP / 2, t[5] + DUMP / 2], rel=0, abs=1e-6)

    def test_antennas_are_ignored_and_cached(self, backend):
        a = backend.axis_runs(F0, "time")
        n = len(backend._axis_runs_cache)
        b = backend.axis_runs(SelectionSpec(field_names=["FIELD-0"],
                                            antenna_names=["ANTENNA-0"]), "time")
        assert a == b and len(backend._axis_runs_cache) == n

    def test_frequency(self, backend):
        runs = backend.axis_runs(F0, "frequency")
        assert len(runs) == 1 and runs[0][1] > runs[0][0]

    def test_nothing_selected_and_other_dims(self, backend):
        assert backend.axis_runs(SelectionSpec(field_names=["NO-SUCH"]), "time") == []
        assert backend.axis_runs(F0, "baseline_id") is None

    def test_local_reader_passes_it_through(self, backend):
        from cubevis.toolbox.visplot.local_visibility_reader import LocalVisibilityReader
        r = LocalVisibilityReader.__new__(LocalVisibilityReader)
        r._backend = backend
        assert r.axis_runs(F0, "time") == backend.axis_runs(F0, "time")


class TestRemoteMemo:

    def test_memo_per_dim_without_the_antenna_choice(self):
        from cubevis.toolbox.visplot.remote_reduction_context import (
            RemoteReductionContext, DATA_METHODS, REPEATABLE_METHODS)
        assert "axis_runs" in DATA_METHODS and "axis_runs" in REPEATABLE_METHODS
        ctx = RemoteReductionContext.__new__(RemoteReductionContext)
        calls = []

        def _call(method, **kw):
            calls.append((method, kw["dim"], kw["selection"]))
            return [[0, 1]]

        ctx._call = _call
        ctx.axis_runs(SelectionSpec(antenna_names=["A"]), "time")
        ctx.axis_runs(SelectionSpec(antenna_names=["B"]), "time")
        ctx.axis_runs(SelectionSpec(), "frequency")
        assert [c[1] for c in calls] == ["time", "frequency"]
        assert calls[0][2].antenna_names is None

    def test_the_registration(self):
        from cubevis.toolbox.visplot import remote_registrations as rr
        cls = next(c for c in vars(rr).values()
                   if isinstance(c, type) and "axis_runs" in vars(c))
        obj = cls.__new__(cls)
        obj._reader = type("R", (), {"axis_runs": lambda self, sel, dim: [(1, 2)]})()
        assert obj.axis_runs(None, "time") == [[1.0, 2.0]]


# ---------------------------------------------------------------------------
# The raster: a decimated full view, and a zoom that re-queries
# ---------------------------------------------------------------------------

def _raster(backend, **kw):
    from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster
    args = dict(backend=backend, selection=F0, polarization="XX",
                y_dim=Axis.TIME, x_dim=Axis.FREQUENCY, quantity=Axis.AMPLITUDE,
                width=200, height=400, headless=True, scaling="linear")
    args.update(kw)
    return VisibilityRaster(**args)


class TestRaster:

    def test_the_axis_is_the_readers_runs(self, backend):
        vr = _raster(backend)
        vr.update_axes(time_gaps="closed")
        ax = vr._time_axis
        runs = backend.axis_runs(F0, "time")
        assert [list(x) for x in zip(ax.lo, ax.hi)] == runs

    def test_decimated_full_view_then_a_finer_zoom_on_the_same_axis(self, backend):
        # Room for a quarter of the 32 x 16 cells: strided along time.
        vr = _raster(backend, max_cells=NTIME * NCHAN // 4)
        vr.update_axes(time_gaps="closed")
        ax = vr._time_axis
        full_u = np.asarray(vr.agg.coords["time"].values, dtype=np.float64)
        assert vr._is_decimated and full_u.size < NTIME
        # the decimated cells are on the full-resolution runs' axis
        assert len(ax) == 2 and np.isfinite(full_u).all()
        # zoom into the first 40 s of the second scan
        u0 = ax.starts[1]
        x0, x1 = vr._x_range
        vr._ensure_detail(x0, x1, u0, u0 + 40.0)
        d = vr._detail
        assert d is not None
        du = np.asarray(d.agg.coords["time"].values, dtype=np.float64)
        # every cell on one of the runs (the overscan margin may reach
        # back into the first)
        assert np.isfinite(ax.real_of(du)).all()
        # finer than the full view: consecutive 8 s integrations in view
        inview = du[(du >= u0) & (du <= u0 + 40.0)]
        assert inview.size >= 5 and np.diff(inview) == pytest.approx(DUMP)
        # the same real times as the data
        t = _times(backend, F0)
        assert np.isin(np.round(ax.real_of(du), 3), np.round(t, 3)).all()

    def test_a_zoom_into_the_blank_asks_for_nothing(self, backend):
        vr = _raster(backend, max_cells=NTIME * NCHAN // 4)
        vr.update_axes(time_gaps="closed")
        ax = vr._time_axis
        s = ax.blank_centres()[0]
        assert vr._detail_selection(vr._x_range, (s - ax.gap / 4, s + ax.gap / 4)) is None

    def test_old_kernel_falls_back_on_the_aggregate(self, backend, monkeypatch):
        def boom(*a, **k):
            raise AttributeError("no axis_runs")
        monkeypatch.setattr(backend, "axis_runs", boom, raising=False)
        vr = _raster(backend)
        vr.update_axes(time_gaps="closed")
        assert len(vr._time_axis) == 2
