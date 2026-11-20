"""test_time_gaps.py -- a raster's Time axis with its gaps closed (debug-0003).

Pre-preview feedback: a field observed in short scans with long gaps in
between (other fields observed then) drew as thin bands in mostly blank
space.  The gaps are real -- the time axis is to scale, so that it lines
up with a scatter of X = Time and a box covers the times it seems to --
and now, optionally, they can be closed: one position per time cell, a
blank one at each break (``raster_grid.TimeAxis``), ticks reading the
times, the readout and flag boxes converted back to real time.

Run:  python -m pytest test_time_gaps.py -q
"""
from __future__ import annotations

import numpy as np
import pytest

from cubevis_test_paths import ensure_cubevis_importable

ensure_cubevis_importable()

from cubevis.toolbox.gridplot.raster_grid import TimeAxis, normalize_time_gaps  # noqa: E402


class TestTimeAxis:
    T = [0, 8, 16, 60, 68, 76, 300]

    def test_positions_with_one_blank_at_each_break(self):
        ax = TimeAxis.build(self.T[::-1] + [8])          # any order, repeats
        assert ax.positions == (0, 1, 2, 4, 5, 6, 8)
        assert ax.n_positions == 9

    def test_positions_of(self):
        ax = TimeAxis.build(self.T)
        p = ax.positions_of([8, 60, 4, 30, 302])
        assert p[0] == 1 and p[1] == 4 and p[2] == pytest.approx(0.5)
        assert np.isnan(p[3])                            # in the gap: not shown
        assert p[4] == pytest.approx(8.0, abs=0.05)

    def test_real_span_and_cells(self):
        ax = TimeAxis.build(self.T)
        assert ax.real_span(1.6, 4.4) == (12.0, 64.0)    # third of run 1 .. first of run 2
        assert ax.real_span(3.0, 3.2) is None            # only the blank row
        assert ax.time_at(4) == 60.0 and ax.time_at(3) is None
        assert ax.cell_at(1) == (4.0, 12.0)

    def test_tick_labels(self):
        ax = TimeAxis.build(self.T)
        assert ax.tick_labels(lambda t: f"{t:g}") == (
            "0", "8", "16", "", "60", "68", "76", "", "300")

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


class TestClosedRaster:
    def test_shown_by_default_and_closed_on_request(self, plotter):
        r = _raster(plotter)
        assert r.time_gaps == "shown" and r._time_axis is None
        y_shown = r._y_range
        r.update_axes(time_gaps="closed")
        assert r._agg.dims[0] == "time"
        assert list(r._agg.coords["time"].values) == [0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 14]
        assert r._y_range == (-0.5, 14.5)
        spec = r._panel_spec()
        assert spec.axis_label("y").startswith("Time (gaps closed) [")
        assert spec.y_ticks[3] == "" and spec.y_ticks[0] != ""
        r.update_axes(time_gaps="shown")
        assert r._time_axis is None and r._y_range == y_shown

    def test_readout_is_in_real_time(self, plotter):
        r = _raster(plotter)
        r.update_axes(time_gaps="closed")
        info = r._probe_raster_pixel_local(2, 4)       # 5th cell: 2nd of scan 4
        ta = r._time_axis
        assert info["y_centre"] == ta.times[4]
        assert info["y_range"][1] - info["y_range"][0] == pytest.approx(8.0)
        assert info.get("scan_names") == ["4"]

    def test_flag_box_becomes_real_times(self, plotter):
        r = _raster(plotter)
        r.update_axes(time_gaps="closed")
        ta = r._time_axis
        req = plotter._flags.build_request(
            r, "raster", {"x0": -0.5, "x1": 7.5, "y0": 1.6, "y1": 4.4}, True)
        assert (req["y0"], req["y1"]) == (ta.lo[2], ta.hi[3])
        with pytest.raises(ValueError, match="only a gap"):
            plotter._flags.build_request(
                r, "raster", {"x0": -0.5, "x1": 7.5, "y0": 3.0, "y1": 3.2}, True)

    def test_no_detail_query_when_closed(self, plotter):
        r = _raster(plotter)
        r.update_axes(time_gaps="closed")
        r._is_decimated = True
        r._ensure_detail(0, 1, 0, 1)
        assert r._detail is None


def test_wired_in_the_gear_tab_and_plot():
    import inspect
    from cubevis.toolbox.visplot import visibility_plotter as vp
    src = inspect.getsource(vp)
    assert '"tgaps_sel": tg_sel' in src
    assert "time_gaps: tg_sel ? tg_sel.value : null" in src
    assert "time_gaps    = tgaps," in src
    assert "tgaps != panel.time_gaps or" in src


def test_state_carries_the_cells(plotter):
    r = _raster(plotter)
    assert r._state_data()["y_tcells"] == [None]
    r.update_axes(time_gaps="closed")
    d = r._state_data()
    ta = r._time_axis
    assert d["x_tcells"] == [None]
    assert d["y_tcells"][0] == [list(ta.positions), list(ta.lo), list(ta.hi)]


def test_linked_cursor_converts_through_real_time(plotter):
    """The cursor sync (VisibilityPlotter._build_layout) maps a closed
    axis's position to real time for the other panels, and back."""
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    cb = plotter._cursor_source.js_property_callbacks["change:data"][0]
    code = cb.code
    # Raster: Time (gaps closed) on Y, cells 0,1,2 | 4 (times 0,8,16 | 60).
    cells = [[0, 1, 2, 4], [-4, 4, 12, 56], [4, 12, 20, 64]]
    script = (
        "const span = () => ({location: NaN});\n"
        "const fig = (xl, yl) => ({below: [{axis_label: xl}], left: [{axis_label: yl}]});\n"
        "const raster = {id: 'R', fig: fig('Channel', 'Time (gaps closed) [UTC, d]'), vspan: span(), hspan: span(),\n"
        f"  state: {{data: {{x_tcells: [null], y_tcells: [{json.dumps(cells)}]}}}}}};\n"
        "const scatter = {id: 'S', fig: fig('Time [UTC, d]', 'Amplitude'), vspan: span(), hspan: span(),\n"
        "  state: {data: {x_tcells: [null], y_tcells: [null]}}};\n"
        "const panels = [raster, scatter];\n"
        "const run = (x, y, f) => new Function('cursor_src', 'panels', CODE)("
        "{data: {x: [x], y: [y], fig: [f]}}, panels);\n"
        "run(3, 1.25, 'R'); const a = scatter.vspan.location;\n"
        "run(3, 3.0, 'R'); const b = scatter.vspan.location;\n"
        "run(62, 0.5, 'S'); const c = raster.hspan.location;\n"
        "run(40, 0.5, 'S'); const d = raster.hspan.location;\n"
        "console.log(JSON.stringify([a, b, c, d]));"
    ).replace("CODE", json.dumps(code))
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    a, b, c, d = json.loads(out.stdout.strip())
    assert a == pytest.approx(10.0)        # position 1.25: a quarter into the 8-s cell 4..12
    assert b is None                       # the blank row: no time (NaN)
    assert c == pytest.approx(4.25)        # 62 s: three quarters into the cell at position 4
    assert d is None                       # 40 s: in the closed gap
