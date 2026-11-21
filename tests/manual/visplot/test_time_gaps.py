"""test_time_gaps.py -- a raster's Time axis with its gaps closed (debug-0003).

Pre-preview feedback: a field observed in short scans with long gaps in
between (other fields observed then) drew as thin bands in mostly blank
space.  The gaps are real -- the time axis is to scale, so that it lines
up with a scatter of X = Time and a box covers the times it seems to --
and now, optionally, they can be closed (``raster_grid.TimeAxis``, a
``GapAxis``): since #36 (debug-0004) each run of data stays to scale with
a small fixed blank between runs, the runs coming from the reader at
full resolution (``axis_runs``), so a zoomed detail query shares the
axis; ticks read the real times, the readout and flag boxes convert
back, and a faint line marks each blank.

Run:  python -m pytest test_time_gaps.py -q
"""
from __future__ import annotations

import numpy as np
import pytest

from cubevis_test_paths import ensure_cubevis_importable

ensure_cubevis_importable()

from cubevis.toolbox.gridplot.raster_grid import TimeAxis, normalize_time_gaps  # noqa: E402


class TestTimeAxis:
    """The runs-based closed axis (#36, debug-0004): runs to scale, a
    blank of min(2 %, 10 % / gaps) of their total between them."""
    T = [0, 8, 16, 60, 68, 76, 300]

    def _ax(self, t=None):
        return TimeAxis.build(self.T if t is None else t, width=8.0)

    def test_runs_starts_and_blank(self):
        ax = self._ax(self.T[::-1] + [8])                # any order, repeats
        assert ax.lo == (-4.0, 56.0, 296.0) and ax.hi == (20.0, 80.0, 304.0)
        # 56 s of data, two gaps: 2 % each (10 % / 2 would be more)
        assert ax.gap == pytest.approx(1.12)
        assert ax.starts == pytest.approx((0.0, 25.12, 50.24))
        assert ax.extent == pytest.approx((0.0, 58.24))
        assert ax.separators() == pytest.approx((24.56, 49.68))

    def test_many_gaps_share_ten_percent(self):
        t = [100.0 * i for i in range(11)]               # 11 lone cells, 10 gaps
        ax = TimeAxis.build(t, width=8.0)
        assert ax.gap == pytest.approx(0.10 * 88.0 / 10)
        assert 10 * ax.gap / (ax.extent[1] - ax.extent[0]) == pytest.approx(0.10 / 1.10)

    def test_positions_of_and_back(self):
        ax = self._ax()
        p = ax.positions_of([8, 60, 4, 30, 302])
        assert p[0] == pytest.approx(12.0) and p[1] == pytest.approx(29.12)
        assert p[2] == pytest.approx(8.0)
        assert np.isnan(p[3])                            # in the gap: not shown
        assert p[4] == pytest.approx(56.24)
        back = ax.real_of(p[~np.isnan(p)])
        assert back == pytest.approx([8, 60, 4, 302])
        assert np.isnan(ax.real_of([24.56])[0])           # the blank: no time
        assert ax.time_at(29.12) == pytest.approx(60.0) and ax.time_at(24.56) is None

    def test_real_span(self):
        ax = self._ax()
        assert ax.real_span(12.0, 29.12) == pytest.approx((8.0, 60.0))
        assert ax.real_span(24.2, 25.0) is None          # only the blank
        assert ax.real_span(-10.0, 100.0) == pytest.approx((-4.0, 304.0))

    def test_from_runs_merges(self):
        ax = TimeAxis.from_runs([(10, 20), (0, 12), (30, 40), (40, 45)])
        assert ax.lo == (0.0, 30.0) and ax.hi == (20.0, 45.0)
        assert TimeAxis.from_runs([(5, 9)]).gap == 0.0

    def test_cells_stay_in_their_run(self):
        from cubevis.toolbox.gridplot.raster_grid import cell_edges
        ax = self._ax()
        u = ax.positions_of(self.T)
        # widths that would reach across the 1.12 blank
        lo, hi = cell_edges(u, width=40.0, runs=ax.run_bounds())
        st, en = ax.run_bounds()
        assert hi[2] <= en[0] + 1e-9 and lo[3] >= st[1] - 1e-9
        assert lo[6] >= st[2] - 1e-9 and hi[6] <= en[2] + 1e-9

    def test_normalize(self):
        assert normalize_time_gaps(None) == "shown"
        assert normalize_time_gaps("Closed") == "closed"
        with pytest.raises(ValueError):
            normalize_time_gaps("squeezed")


def _scans_transform(pd, data):
    """Two fields, four 3-integration scans each, interleaved in time."""
    f = int(pd.FIELD_ID[0])
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
    path = str(tmp_path_factory.mktemp("tgaps") / "scans.ms")
    sim.MSStructureSimulator(
        ntime=12, time_chunks=12, nantenna=4, nfield=2, auto_corrs=False,
        data_description=[(8, ["XX", "YY"])],
        transform_data=_scans_transform).simulate_ms(path)
    return path


@pytest.fixture
def plotter(scans_ms):
    from cubevis.toolbox.visplot import VisibilityPlotter
    vp = VisibilityPlotter(ms=scans_ms, field="FIELD-1", layout="side",
                           enable_flagging=True)
    yield vp
    vp.close()


def _raster(vp):
    return next(s.raster for s in vp._slots if getattr(s, "raster", None) is not None)


def _real_times(r):
    """The real times of the closed raster's cells."""
    return r._time_axis.real_of(r._agg.coords["time"].values)


class TestClosedRaster:
    def test_shown_by_default_and_closed_on_request(self, plotter):
        r = _raster(plotter)
        assert r.time_gaps == "shown" and r._time_axis is None
        y_shown = r._y_range
        real = np.asarray(r._agg.coords["time"].values, dtype=np.float64)
        r.update_axes(time_gaps="closed")
        ta = r._time_axis
        assert r._agg.dims[0] == "time"
        assert len(ta) == 4                              # FIELD-1's four scans
        assert _real_times(r) == pytest.approx(real)
        # to scale within a scan: 8 s apart
        assert np.diff(r._agg.coords["time"].values)[:2] == pytest.approx([8.0, 8.0])
        assert r._y_range == pytest.approx(ta.extent)
        spec = r._panel_spec()
        assert spec.axis_label("y").startswith("Time (gaps closed) [")
        assert spec.y_ticks is None and spec.y_runs == ta.state()
        r.update_axes(time_gaps="shown")
        assert r._time_axis is None and r._y_range == y_shown

    def test_runs_come_from_the_reader(self, plotter):
        r = _raster(plotter)
        r.update_axes(time_gaps="closed")
        runs = plotter._reader.axis_runs(r._selection, "time")
        assert [list(x) for x in zip(r._time_axis.lo, r._time_axis.hi)] == runs

    def test_readout_is_in_real_time(self, plotter):
        r = _raster(plotter)
        r.update_axes(time_gaps="closed")
        info = r._probe_raster_pixel_local(2, 4)       # 5th cell: 2nd of scan 4
        assert info["y_centre"] == pytest.approx(_real_times(r)[4])
        assert info["y_range"][1] - info["y_range"][0] == pytest.approx(8.0)
        assert info.get("scan_names") == ["4"]

    def test_flag_box_becomes_real_times(self, plotter):
        r = _raster(plotter)
        r.update_axes(time_gaps="closed")
        ta = r._time_axis
        u = r._agg.coords["time"].values
        box = {"x0": -0.5, "x1": 7.5, "y0": float(u[2]) - 1, "y1": float(u[3]) + 1}
        req = plotter._flags.build_request(r, "raster", box, True)
        assert (req["y0"], req["y1"]) == pytest.approx(ta.real_span(box["y0"], box["y1"]))
        sep = ta.separators()[0]
        with pytest.raises(ValueError, match="only a gap"):
            plotter._flags.build_request(
                r, "raster", {"x0": -0.5, "x1": 7.5, "y0": sep - 0.01, "y1": sep + 0.01}, True)

    def test_zoom_queries_detail_when_closed(self, plotter):
        """#36: the closed axis comes from the runs, so a finer query
        shares it -- zooming in re-queries instead of magnifying."""
        r = _raster(plotter)
        r.update_axes(time_gaps="closed")
        ta = r._time_axis
        r._is_decimated = True
        u0, u1 = ta.starts[1], ta.starts[1] + 10.0         # 10 s of scan 4
        x0, x1 = r._x_range
        r._ensure_detail(x0, x1, u0, u1)
        d = r._detail
        assert d is not None
        du = np.asarray(d.agg.coords["time"].values, dtype=np.float64)
        assert du.size and np.isfinite(du).all()
        st, en = ta.run_bounds()
        assert ((du >= st[1] - 1e-6) & (du <= en[1] + 1e-6)).all()
        # the same cells as the full aggregate, at the same closed times
        full_u = np.asarray(r._agg.coords["time"].values)
        assert np.isin(np.round(du, 6), np.round(full_u, 6)).all()

    def test_separators_are_drawn(self, plotter):
        r = _raster(plotter)
        r.update_axes(time_gaps="closed")
        img = np.asarray(r._image_source.data["image"][0])
        rgba = img.view(np.uint8).reshape(img.shape + (4,))
        h = img.shape[0]
        y0, y1 = r._y_range
        for s in r._time_axis.separators():
            row = int(np.floor((s - y0) / (y1 - y0) * h))
            assert (rgba[row, :, 3] > 0).all()           # the faint line
            assert tuple(rgba[row, 0]) == (128, 128, 128, 110)


def test_wired_in_the_gear_tab_and_plot():
    import inspect
    from cubevis.toolbox.visplot import visibility_plotter as vp
    src = inspect.getsource(vp)
    assert '"tgaps_sel": tg_sel' in src
    assert "time_gaps: tg_sel ? tg_sel.value : null" in src
    assert "time_gaps    = tgaps," in src
    assert "tgaps != panel.time_gaps or" in src


def test_state_carries_the_runs(plotter):
    r = _raster(plotter)
    assert r._state_data()["y_runs"] == [None]
    r.update_axes(time_gaps="closed")
    d = r._state_data()
    assert d["x_runs"] == [None]
    assert d["y_runs"][0] == r._time_axis.state()


def test_linked_cursor_converts_through_real_time(plotter):
    """The cursor sync (VisibilityPlotter._build_layout) maps a closed
    axis's coordinate to real time for the other panels, and back."""
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    cb = plotter._cursor_source.js_property_callbacks["change:data"][0]
    code = cb.code
    # Raster: Time (gaps closed) on Y; runs -4..20 at 0..24, 56..80 at 25..49.
    runs = [[0, 25], [-4, 56], [20, 80]]
    script = (
        "const span = () => ({location: NaN});\n"
        "const fig = (xl, yl) => ({below: [{axis_label: xl}], left: [{axis_label: yl}]});\n"
        "const raster = {id: 'R', fig: fig('Channel', 'Time (gaps closed) [UTC, d]'), vspan: span(), hspan: span(),\n"
        f"  state: {{data: {{x_runs: [null], y_runs: [{json.dumps(runs)}]}}}}}};\n"
        "const scatter = {id: 'S', fig: fig('Time [UTC, d]', 'Amplitude'), vspan: span(), hspan: span(),\n"
        "  state: {data: {x_runs: [null], y_runs: [null]}}};\n"
        "const panels = [raster, scatter];\n"
        "const run = (x, y, f) => new Function('cursor_src', 'panels', CODE)("
        "{data: {x: [x], y: [y], fig: [f]}}, panels);\n"
        "run(3, 10, 'R'); const a = scatter.vspan.location;\n"
        "run(3, 24.5, 'R'); const b = scatter.vspan.location;\n"
        "run(62, 0.5, 'S'); const c = raster.hspan.location;\n"
        "run(40, 0.5, 'S'); const d = raster.hspan.location;\n"
        "console.log(JSON.stringify([a, b, c, d]));"
    ).replace("CODE", json.dumps(code))
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    a, b, c, d = json.loads(out.stdout.strip())
    assert a == pytest.approx(6.0)         # closed 10: 10 s into the run from -4
    assert b is None                       # the blank: no time (NaN)
    assert c == pytest.approx(31.0)        # 62 s: 6 s into the run starting at 25
    assert d is None                       # 40 s: in the closed gap
