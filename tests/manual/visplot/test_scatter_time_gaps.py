"""
test_scatter_time_gaps.py
=========================
Issue #36 part 2: a scatter with X = Time can close its time gaps too.

The scatter asks the reader for the selection's runs (``axis_runs``) and
sends them with ``query_columns`` (``x_runs=``); the kernel puts every
sample's ``x`` on the closed axis they make
(``_scatter_render.close_x_gaps``) before binning, so the image, the
extents, the two-level reference and a zoomed re-render are all in
closed coordinates -- the same closed axis a raster of the selection
has.  What names a real time converts back: the tick labels and the
linked cursor (the runs ride in the panel state, as for the raster), the
hover readout, the exact-identity box and a flag box.

Location in repository:
    cubevis/tests/manual/visplot/test_scatter_time_gaps.py

Run:
    pytest cubevis/tests/manual/visplot/test_scatter_time_gaps.py -v

Data: the simulated MS of ``test_time_gaps.py`` -- FIELD-1 observed in
four 3-integration scans, 60 s apart.  What is NOT covered: anything
only a browser shows (ticks drawn, the cursor moving).
"""
from __future__ import annotations

import asyncio
import warnings

import numpy as np
import pytest

from cubevis_test_paths import ensure_cubevis_importable
ensure_cubevis_importable()

pd = pytest.importorskip("pandas")

from cubevis.toolbox.gridplot.raster_grid import GapAxis               # noqa: E402
from cubevis.toolbox.visplot.axes import Axis                          # noqa: E402
from cubevis.toolbox.visplot.data import _scatter_render as sr         # noqa: E402

warnings.filterwarnings("ignore")


# ---------------------------------------------------------------------------
# The kernel step
# ---------------------------------------------------------------------------

class TestCloseXGaps:
    RUNS = [[-4.0, 20.0], [56.0, 80.0]]

    def _frames(self):
        df = pd.DataFrame({"x": [0.0, 8.0, 60.0, 76.0, 30.0], "y": [1, 2, 3, 4, 5],
                           "time": [0.0, 8.0, 60.0, 76.0, 30.0]})
        return {(Axis.AMPLITUDE, "XX"): df, (Axis.PHASE, "XX"): None}

    def test_x_goes_on_the_closed_axis(self):
        frames = self._frames()
        out = sr.close_x_gaps(frames, self.RUNS)
        ax = GapAxis.from_runs(self.RUNS)
        d = out[(Axis.AMPLITUDE, "XX")]
        assert list(d["x"]) == pytest.approx(list(ax.positions_of([0.0, 8.0, 60.0, 76.0])))
        assert list(d["time"]) == [0.0, 8.0, 60.0, 76.0]     # identity stays real
        assert list(d["y"]) == [1, 2, 3, 4]                   # 30 s: no run, dropped
        assert out[(Axis.PHASE, "XX")] is None

    def test_the_cached_frames_are_left_alone(self):
        frames = self._frames()
        sr.close_x_gaps(frames, self.RUNS)
        assert list(frames[(Axis.AMPLITUDE, "XX")]["x"]) == [0.0, 8.0, 60.0, 76.0, 30.0]

    def test_a_memoized_extent_is_not_carried_over(self):
        frames = self._frames()
        frames[(Axis.AMPLITUDE, "XX")].attrs["extent"] = (0.0, 76.0, 1.0, 5.0)
        d = sr.close_x_gaps(frames, self.RUNS)[(Axis.AMPLITUDE, "XX")]
        assert "extent" not in d.attrs

    def test_no_runs_no_change(self):
        frames = self._frames()
        assert sr.close_x_gaps(frames, None) is frames
        assert sr.close_x_gaps(frames, []) is frames


class TestRemoteRelay:

    def _ctx(self, calls):
        from cubevis.toolbox.visplot.remote_reduction_context import RemoteReductionContext
        ctx = RemoteReductionContext.__new__(RemoteReductionContext)
        ctx._call = lambda method, **kw: calls.append(kw) or "ok"
        return ctx

    def test_x_runs_sent_only_when_closed(self):
        from cubevis.toolbox.visplot.selection import SelectionSpec
        calls = []
        ctx = self._ctx(calls)
        ctx.query_columns(Axis.TIME, [], SelectionSpec())
        ctx.query_columns(Axis.TIME, [], SelectionSpec(), x_runs=[(1, 2)])
        assert "x_runs" not in calls[0]
        assert calls[1]["x_runs"] == [[1.0, 2.0]]

    def test_registration_and_local_reader_accept_it(self):
        import inspect
        from cubevis.toolbox.visplot import remote_registrations as rr
        from cubevis.toolbox.visplot.local_visibility_reader import LocalVisibilityReader
        cls = next(c for c in vars(rr).values()
                   if isinstance(c, type) and "query_columns" in vars(c))
        assert "x_runs" in inspect.signature(cls.query_columns).parameters
        assert "x_runs" in inspect.signature(LocalVisibilityReader.query_columns).parameters


# ---------------------------------------------------------------------------
# Real data
# ---------------------------------------------------------------------------

def _scans_transform(pd_, data):
    """Two fields, four 3-integration scans each, interleaved in time
    (as test_time_gaps.py)."""
    f = int(pd_.FIELD_ID[0])
    t = data["TIME"][1]
    ut = np.unique(t)
    ti = np.searchsorted(ut, t)
    scan = 1 + 2 * (ti // 3) + f
    newt = ut[0] + (scan - 1) * 60.0 + (ti % 3) * 8.0
    data["TIME"] = (data["TIME"][0], newt)
    data["SCAN_NUMBER"] = (("row",), scan.astype(np.int32))
    if "TIME_CENTROID" in data:
        data["TIME_CENTROID"] = (data["TIME_CENTROID"][0], newt)
    data["FLAG"] = (data["FLAG"][0], np.zeros_like(data["FLAG"][1]))
    return data


@pytest.fixture(scope="module")
def scans_ms(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("stgaps") / "scans.ms")
    sim.MSStructureSimulator(
        ntime=12, time_chunks=12, nantenna=4, nfield=2, auto_corrs=False,
        data_description=[(8, ["XX", "YY"])],
        transform_data=_scans_transform).simulate_ms(path)
    return path


@pytest.fixture(scope="module")
def scans_ps(scans_ms, tmp_path_factory):
    import xarray as xr
    out = str(tmp_path_factory.mktemp("stgapsps") / "scans.ps.zarr")
    xr.open_datatree(scans_ms, engine="xarray-ms:msv2",
                     partition_schema=["FIELD_ID"]).to_zarr(out, mode="w", compute=True)
    return out


@pytest.fixture(params=["msv2", "msv4"])
def plotter(request, scans_ms, scans_ps):
    from cubevis.toolbox.visplot import VisibilityPlotter
    kw = dict(ms=scans_ms) if request.param == "msv2" else dict(ps=scans_ps)
    vp = VisibilityPlotter(field="FIELD-1", layout="side", enable_flagging=True, **kw)
    yield vp
    vp.close()


def _scatter(vp, closed=True):
    s = next(s.scatter for s in vp._slots if s.kind == "scatter")
    s.update_axes(x_dim=Axis.TIME, time_gaps="closed" if closed else "shown")
    return s


class TestBackend:

    def test_query_columns_on_the_closed_axis(self, plotter):
        s = _scatter(plotter, closed=False)
        real = s._x_range
        s.update_axes(time_gaps="closed")
        ta = s._time_axis
        assert len(ta) == 4                                   # FIELD-1's scans
        assert s._x_range[0] >= ta.extent[0] - 1e-6 and s._x_range[1] <= ta.extent[1] + 1e-6
        assert real[1] - real[0] > 150                        # real: 3 gaps of 36 s+
        assert s._x_range[1] - s._x_range[0] < 100            # closed: 4 runs, no gaps


class TestWidget:

    def test_off_by_default_and_only_for_time(self, plotter):
        s = next(s.scatter for s in plotter._slots if s.kind == "scatter")
        assert s.time_gaps == "shown" and s._time_axis is None
        s.update_axes(time_gaps="closed")
        if s._x_dim != Axis.TIME:
            assert s._time_axis is None
        s.update_axes(x_dim=Axis.TIME)
        assert s._time_axis is not None
        s.update_axes(x_dim=Axis.UVDIST)
        assert s._time_axis is None

    def test_spec_and_state(self, plotter):
        s = _scatter(plotter)
        spec = s._panel_spec()
        assert spec.x_runs == s._time_axis.state()
        assert "(gaps closed)" in spec.axis_label("x")
        assert s._state_data()["x_runs"] == [s._time_axis.state()]

    def test_same_axis_as_a_raster_of_the_selection(self, plotter):
        s = _scatter(plotter)
        r = next(sl.raster for sl in plotter._slots if sl.kind == "raster")
        r.update_axes(time_gaps="closed")
        assert r._time_axis.state() == s._time_axis.state()

    def test_scans_meet(self, plotter):
        """No blank between runs, as on the raster (2026-11-21)."""
        s = _scatter(plotter)
        ta = s._time_axis
        assert ta.ends[:-1] == pytest.approx(ta.starts[1:])

    def test_flag_box_becomes_real_times(self, plotter):
        s = _scatter(plotter)
        ta = s._time_axis
        y0, y1 = s._y_range
        box = {"x0": ta.starts[1] + 1.0, "x1": ta.starts[2] + 1.0, "y0": y0, "y1": y1}
        req = plotter._flags.build_request(s, "scatter", box, True)
        assert (req["x0"], req["x1"]) == pytest.approx(ta.real_span(box["x0"], box["x1"]))
        e = ta.extent[1]
        with pytest.raises(ValueError, match="outside the data"):
            plotter._flags.build_request(
                s, "scatter", {"x0": e + 5, "x1": e + 6, "y0": y0, "y1": y1}, True)

    def test_exact_identity_outside_the_data_says_so(self, plotter):
        s = _scatter(plotter)
        e = s._time_axis.extent[1]
        y0, y1 = s._y_range
        resp = asyncio.run(s._handle_probe_region(
            {"tool": "info_box", "x0": e + 5, "x1": e + 6, "y0": y0, "y1": y1}))
        assert resp["status"] == "no_data"

    def test_exact_identity_names_real_times(self, plotter):
        s = _scatter(plotter)
        ta = s._time_axis
        y0, y1 = s._y_range
        resp = asyncio.run(s._handle_probe_region(
            {"tool": "info_box", "x0": ta.starts[1], "x1": ta.ends[1], "y0": y0, "y1": y1}))
        assert resp["status"] == "ok"
        assert f"{ta.lo[1]:.6g}" in resp["info_html"]

    def test_zoom_re_render_stays_on_the_axis(self, plotter):
        s = _scatter(plotter)
        ta = s._time_axis
        y0, y1 = s._y_range
        s._rerender(x_range=(ta.starts[1], ta.ends[1]), y_range=(y0, y1))
        assert any(n > 0 for n in s._layer_n_in_view)

    def test_plot_message_closes_the_gaps(self, plotter):
        import inspect
        from cubevis.toolbox.visplot import visibility_plotter as vpm
        src = inspect.getsource(vpm)
        assert "time_gaps: stg_sel ? stg_sel.value : null" in src
        assert "s_tg != panel.time_gaps" in src
        assert "averaging=s_am, time_gaps=s_tg" in src
        w = plotter._panel_axis_widgets
        sid = next(sl.id for sl in plotter._slots if sl.kind == "scatter")
        assert w[sid]["scatter"]["tgaps_sel"].value == "shown"


# ---------------------------------------------------------------------------
# No more canvas columns than integrations (2026-11-21)
# ---------------------------------------------------------------------------

class TestCanvasStep:
    """X = Time: the canvas has no more columns than samples in view, so
    none is left empty between two integrations (stripes on a closed
    axis of 6.048 s integrations about 2.2 px apart, TW Hya)."""

    def _frames(self, n=1000, per=100):
        # dense enough that the sparse-data shrink stays out of it
        x = np.repeat(np.arange(n) * 6.048, per)
        df = pd.DataFrame({"x": x, "y": np.tile(np.linspace(0.0, 2.0, per), n)})
        return {(Axis.AMPLITUDE, "XX"): df}

    def _layers(self):
        from cubevis.toolbox.visplot.data.reader import ScatterLayerSpec
        return [ScatterLayerSpec(y_axis=Axis.AMPLITUDE, polarization="XX",
                                 cmap=("#000", "#fff"))]

    def test_capped_by_the_step(self):
        w, h = sr.compute_canvas_size(self._frames(), self._layers(),
                                      0.0, 2480.0, 0.0, 2.0, 900, 550, x_step=6.048)
        assert w == int(2480.0 / 6.048) and h == 550

    def test_no_step_no_cap(self):
        assert sr.compute_canvas_size(self._frames(), self._layers(),
                                      0.0, 2480.0, 0.0, 2.0, 900, 550) == (900, 550)

    def test_zoomed_into_a_few_integrations(self):
        w, _ = sr.compute_canvas_size(self._frames(), self._layers(),
                                      0.0, 20.0, 0.0, 2.0, 900, 550, x_step=6.048)
        assert w == 3

    def test_the_step_from_the_reader(self, scans_ms):
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
        from cubevis.toolbox.visplot.selection import SelectionSpec
        b = MSv2Backend(scans_ms)
        b.open()
        try:
            step = b.scatter_time_step(SelectionSpec())
            assert step == pytest.approx(8.0)            # the simulator's dump rate
            assert b.scatter_time_step(SelectionSpec(avg_time=30.0)) == pytest.approx(30.0)
            assert b.scatter_time_step(SelectionSpec(avg_time=2.0)) == pytest.approx(8.0)
            assert b.scatter_time_step(SelectionSpec(avg_time="scan")) is None
        finally:
            b.close()

    def test_closed_scatter_has_no_empty_columns(self, plotter):
        s = _scatter(plotter)
        img = s._collapse_and_composite()
        a = img.view(np.uint8).reshape(img.shape + (4,))[..., 3]
        assert (a > 0).any(axis=0).all()
        span = s._x_range[1] - s._x_range[0]
        assert s._canvas_width <= int(span / 8.0)
