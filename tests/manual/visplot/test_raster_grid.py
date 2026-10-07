"""
test_raster_grid.py
===================
Where a raster's cells are (2026-10-07, HRS H3 / H4): the image, the
cursor readout, flag boxes and flag overlays must all agree with the
axis, on axes that have gaps (time between scans) and on a Baseline axis
that shows a subset of baselines or is ordered by length.

Location in repository:
    cubevis/tests/manual/visplot/test_raster_grid.py

Run:
    pytest cubevis/tests/manual/visplot/test_raster_grid.py -v

Sections
--------
1. cell_edges / locate / overlapping   the cell rule
2. resample                            against a brute-force reference
3. BaselineAxis                        ordering, labels, lookups
4. Tick labels                         Python, and the shipped JS under node
5. Real backends                       baseline lengths (sim MS / PS)
6. Real raster                         image vs axis vs readout, gapped time
7. Real plotter                        flag boxes, overlays, Baseline order
8. TW Hya                              the measured case (needs the MS)

Sections 1-4 need no data.  5-7 build a small simulated MSv2 *with gaps
in time* using xarray-ms's simulator (and its MSv4 zarr twin).  Section
8 runs only when ``MS`` points at the TW Hya test data.

``test_image_is_blank_in_a_time_gap`` and
``test_drawn_rows_are_the_rows_the_axis_says`` use only what existed
before this work (the image source and the aggregate) and FAIL on
``main`` at ``693c99c``: there the image was drawn with rows evenly
spaced whatever their times.

What is NOT covered: anything that only happens in a browser.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import warnings

import numpy as np
import pytest

from cubevis_test_paths import ensure_cubevis_importable
ensure_cubevis_importable()

xr = pytest.importorskip("xarray")

from cubevis.toolbox.visplot import raster_grid as rg                 # noqa: E402
from cubevis.toolbox.visplot.axes import Axis                         # noqa: E402
from cubevis.toolbox.visplot.selection import SelectionSpec           # noqa: E402

warnings.filterwarnings("ignore")


# ---------------------------------------------------------------------------
# 1. The cell rule
# ---------------------------------------------------------------------------

class TestCellEdges:

    def test_uniform_run_is_contiguous_midpoints(self):
        lo, hi = rg.cell_edges([0.0, 6.0, 12.0, 18.0])
        assert lo.tolist() == [-3, 3, 9, 15]
        assert hi.tolist() == [3, 9, 15, 21]

    def test_a_gap_belongs_to_no_cell(self):
        t = np.array([0, 6, 12, 18, 100, 106, 112.0])
        lo, hi = rg.cell_edges(t)
        assert hi[3] == 21 and lo[4] == 97            # not 59, the midpoint
        assert rg.locate(t, 50.0) == -1
        assert rg.locate(t, 20.9) == 3 and rg.locate(t, 97.1) == 4

    def test_one_missing_integration_is_a_gap(self):
        lo, hi = rg.cell_edges([0.0, 6, 12, 24, 30])
        assert hi[2] == 15 and lo[3] == 21

    def test_jitter_is_not_a_gap(self):
        t = np.array([0.0, 6.05, 11.97, 18.02, 24.0])
        lo, hi = rg.cell_edges(t)
        assert np.allclose(hi[:-1], lo[1:])

    def test_change_of_integration_time_leaves_no_sliver(self):
        # 6 s integrations followed, without a pause, by 2 s ones.
        t = np.array([0.0, 6, 12, 16, 18, 20])
        lo, hi = rg.cell_edges(t)
        assert np.allclose(hi[:-1], lo[1:])

    def test_unsorted_input_keeps_its_order(self):
        t = np.array([12.0, 0.0, 6.0])
        lo, hi = rg.cell_edges(t)
        assert lo.tolist() == [9, -3, 3] and hi.tolist() == [15, 3, 9]

    def test_single_and_empty(self):
        lo, hi = rg.cell_edges([5.0])
        assert lo.tolist() == [5.0] and hi.tolist() == [5.0]
        lo, hi = rg.cell_edges([5.0], half=0.5)
        assert lo.tolist() == [4.5] and hi.tolist() == [5.5]
        assert rg.cell_edges([])[0].size == 0
        assert rg.extent([]) is None
        assert rg.extent([0.0, 1.0, 2.0], half=0.5) == (-0.5, 2.5)

    def test_index_axis_half(self):
        lo, hi = rg.cell_edges([0, 1, 2, 7], half=0.5)
        assert lo.tolist() == [-0.5, 0.5, 1.5, 6.5]

    def test_overlapping(self):
        t = np.array([0, 6, 12, 18, 100, 106.0])
        assert not rg.overlapping(t, 30, 90).any()           # only the gap
        assert rg.overlapping(t, 20.9, 97.1).tolist() == [0, 0, 0, 1, 1, 0]
        assert rg.overlapping(t, 21.1, 96.9).sum() == 0
        assert rg.overlapping(t, 100, 5).tolist() == [0, 1, 1, 1, 1, 0]   # reversed
        assert rg.overlapping([], 0, 1).size == 0

    def test_locate_on_a_shared_edge_is_a_cell(self):
        assert rg.locate([0.0, 6, 12], 3.0) in (0, 1)
        assert rg.locate([0.0, 6, 12], -3.0) == 0
        assert rg.locate([0.0, 6, 12], -3.01) == -1


# ---------------------------------------------------------------------------
# 2. resample
# ---------------------------------------------------------------------------

def _brute(v, yc, xc, xr_, yr_, w, h, yh=None, xh=None):
    """Pixel by pixel, with Python loops: the definition, not the method."""
    ylo, yhi = rg.cell_edges(yc, yh)
    xlo, xhi = rg.cell_edges(xc, xh)

    def cells(lo, hi, c, r0, r1, n):
        e = np.linspace(r0, r1, n + 1)
        out = []
        for p in range(n):
            a, b = e[p], e[p + 1]
            inn = [i for i in range(len(c))
                   if (a <= c[i] < b) or (p == n - 1 and c[i] == b)]
            if not inn:
                m = (a + b) / 2
                inn = [i for i in range(len(c)) if lo[i] <= m <= hi[i]][:1]
            out.append(inn)
        return out
    cy, cx = cells(ylo, yhi, yc, *yr_, h), cells(xlo, xhi, xc, *xr_, w)
    out = np.full((h, w), np.nan)
    for i in range(h):
        for j in range(w):
            if cy[i] and cx[j]:
                sub = v[np.ix_(cy[i], cx[j])]
                if np.isfinite(sub).any():
                    out[i, j] = np.nanmean(sub)
    return out


class TestResample:

    def test_matches_brute_force_on_random_gapped_grids(self):
        rng = np.random.default_rng(7)
        for _ in range(150):
            ny, nx = rng.integers(1, 25), rng.integers(1, 25)
            yc = np.cumsum(rng.choice([1, 1, 1, 5, 20], ny)).astype(float)
            xc = np.cumsum(rng.choice([2, 2, 7], nx)).astype(float)
            v = rng.normal(size=(ny, nx))
            v[rng.random((ny, nx)) < 0.2] = np.nan
            w, h = int(rng.integers(1, 50)), int(rng.integers(1, 50))
            xr_ = (xc[0] - rng.uniform(0, 5), xc[-1] + rng.uniform(0.1, 5))
            yr_ = (yc[0] - rng.uniform(0, 5), yc[-1] + rng.uniform(0.1, 5))
            got = rg.resample(v, yc, xc, xr_, yr_, w, h)
            assert np.allclose(got, _brute(v, yc, xc, xr_, yr_, w, h),
                               equal_nan=True, atol=1e-9)

    def test_rows_are_drawn_at_their_times_and_gaps_are_blank(self):
        t = np.array([0.0, 10, 20, 100, 110])            # gap 25..95
        v = np.arange(5, dtype=float)[:, None] * np.ones((1, 3))
        img = rg.resample(v, t, [0.0, 1, 2], (-0.5, 2.5), (-5, 115), 3, 120,
                          x_half=0.5)
        ys = np.linspace(-5, 115, 121)
        ys = (ys[:-1] + ys[1:]) / 2
        for y, row in zip(ys, img):
            k = rg.locate(t, y)
            if k < 0:
                assert np.isnan(row).all(), y
            else:
                assert (row == k).all(), (y, k, row)
        assert np.isnan(img[40:80]).all()                # the gap

    def test_upsampling_copies_values_exactly(self):
        v = np.array([[0.1, 0.2], [0.3, 1e9 + 0.7]])
        img = rg.resample(v, [0.0, 1], [0.0, 1], (-0.5, 1.5), (-0.5, 1.5),
                          40, 40, x_half=0.5, y_half=0.5)
        assert set(np.unique(img).tolist()) == set(v.ravel().tolist())

    def test_downsampling_is_the_mean_of_the_finite_cells(self):
        v = np.array([[1.0, 3.0, np.nan, 5.0]])
        img = rg.resample(v, [0.0], [0.0, 1, 2, 3], (-0.5, 3.5), (-0.5, 0.5),
                          2, 1, x_half=0.5, y_half=0.5)
        assert img.tolist() == [[2.0, 5.0]]

    def test_any_mode_never_loses_a_marked_cell(self):
        v = np.zeros((1, 100))
        v[0, 37] = 1.0
        img = rg.resample(v, [0.0], np.arange(100.0), (-0.5, 99.5), (-0.5, 0.5),
                          10, 1, x_half=0.5, y_half=0.5, how="any")
        assert img.tolist() == [[0, 0, 0, 1, 0, 0, 0, 0, 0, 0]]

    def test_single_column_is_drawable_with_a_half_width(self):
        v = np.array([[1.0], [2.0]])
        img = rg.resample(v, [0.0, 6], [0.0], (-0.5, 0.5), (-3, 9), 4, 2,
                          x_half=0.5)
        assert img.tolist() == [[1, 1, 1, 1], [2, 2, 2, 2]]

    def test_flipped_ranges(self):
        v = np.array([[1.0, 2.0]])
        a = rg.resample(v, [0.0], [0.0, 1], (1.5, -0.5), (-0.5, 0.5), 2, 1,
                        x_half=0.5, y_half=0.5)
        assert a.tolist() == [[2.0, 1.0]]

    def test_bad_input(self):
        with pytest.raises(ValueError):
            rg.resample(np.zeros((2, 2)), [0.0], [0.0, 1], (0, 1), (0, 1), 2, 2)
        with pytest.raises(ValueError):
            rg.resample(np.zeros((1, 1)), [0.0], [0.0], (0, 1), (0, 1), 2, 2,
                        how="max")
        assert np.isnan(rg.resample(np.zeros((0, 0)), [], [], (0, 1), (0, 1),
                                    3, 2)).all()


# ---------------------------------------------------------------------------
# 3. BaselineAxis
# ---------------------------------------------------------------------------

class TestBaselineAxis:

    LEN = {1: 30.0, 3: 10.0, 5: None, 9: 2000.0, 12: 10.0}
    NAMES = {1: "A&B", 3: "A&C", 9: "B&D", 12: "C&D"}

    def test_number_order(self):
        ax = rg.BaselineAxis.build([9, 1, 3], "number", self.LEN, self.NAMES)
        assert ax.ids == (1, 3, 9)
        assert ax.tick_labels() == ("1", "3", "9")
        assert ax.positions_of([9, 1, 7]).tolist() == [2, 0, -1]

    def test_length_order_shortest_first_unknown_last_ties_by_number(self):
        ax = rg.BaselineAxis.build([5, 1, 3, 9, 12], "length", self.LEN, self.NAMES)
        assert ax.ids == (3, 12, 1, 9, 5)
        assert ax.tick_labels() == ("10 m", "10 m", "30 m", "2 km", "#5")

    def test_length_order_without_lengths_is_number_order(self):
        ax = rg.BaselineAxis.build([9, 1, 3], "length")
        assert ax.ids == (1, 3, 9)

    def test_lookups(self):
        ax = rg.BaselineAxis.build([1, 3, 9], "length", self.LEN, self.NAMES)
        assert ax.ids == (3, 1, 9)
        assert ax.id_at(0.4) == 3 and ax.id_at(0.6) == 1 and ax.id_at(2.49) == 9
        assert ax.id_at(-0.6) is None and ax.id_at(2.6) is None
        assert ax.ids_in(0.6, 1.4) == [1]
        assert ax.ids_in(0.4, 1.6) == [3, 1, 9]
        assert ax.ids_in(5, 9) == []
        assert ax.describe(0) == "A&C (#3, 10 m)"

    def test_normalize(self):
        assert rg.normalize_baseline_order(None) == "number"
        assert rg.normalize_baseline_order(" Length ") == "length"
        assert rg.normalize_baseline_order("id") == "number"
        with pytest.raises(ValueError):
            rg.normalize_baseline_order("uv")

    def test_format_length(self):
        assert rg.format_length(15.06) == "15.1 m"
        assert rg.format_length(999.2) == "999 m"
        assert rg.format_length(1240.0) == "1.24 km"
        assert rg.format_length(8611e3) == "8611 km"
        assert rg.format_length(None) == "" and rg.format_length(float("nan")) == ""


# ---------------------------------------------------------------------------
# 4. Tick labels
# ---------------------------------------------------------------------------

LABEL_CASES = [
    (0.0, "15.1 m"), (1.0, "21.4 m"), (2.0, "#7"), (1.0000001, "21.4 m"),
    (0.5, ""), (-1.0, ""), (3.0, ""), (2.4, ""),
]
LABELS = ["15.1 m", "21.4 m", "#7"]


class TestTickLabels:

    def test_python(self):
        from cubevis.toolbox.visplot.tick_format import format_tick
        for tick, want in LABEL_CASES:
            assert format_tick(tick, False, 0.0, 1.0, LABELS) == want, tick
        # labels win over a time axis; no labels = unchanged behaviour
        assert format_tick(1.0, True, 0.0, 1.0, LABELS) == "21.4 m"
        assert format_tick(125.0, True, 0.0) == "2m 05s"

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
    def test_shipped_javascript(self, tmp_path):
        from cubevis.toolbox.visplot.tick_format import _JS_CORE, TICK_FORMATTER_JS
        script = (
            "function core(tick, is_time, t0, scale, labels) {" + _JS_CORE + "}\n"
            # the Bokeh wrapper, with a state source as the browser has it
            "function wrapped(tick, state, axis_key, t0_key, scale_key, cat_key) {"
            + TICK_FORMATTER_JS + "}\n"
            f"const cases = {json.dumps(LABEL_CASES)};\n"
            f"const labels = {json.dumps(LABELS)};\n"
            "const out = cases.map(c => core(c[0], false, 0, 1, labels));\n"
            "const st = {data: {x_is_time: [0], x_t0: [0], x_scale: [1], x_cat: [labels],"
            "                   y_is_time: [1], y_t0: [100], full_y0: [97], y_scale: [1], y_cat: [null]}};\n"
            "const w = [wrapped(1, st, 'x_is_time', 'x_t0', 'x_scale', 'x_cat'),"
            "           wrapped(225, st, 'y_is_time', 'y_t0', 'y_scale', 'y_cat')];\n"
            "console.log(JSON.stringify({out, w}));\n")
        f = tmp_path / "t.js"
        f.write_text(script)
        res = subprocess.run(["node", str(f)], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        got = json.loads(res.stdout)
        assert got["out"] == [c[1] for c in LABEL_CASES]
        assert got["w"] == ["21.4 m", "2m 05s"]

    def test_panel_spec_state(self):
        from cubevis.toolbox.visplot.panel_spec import PanelSpec
        base = dict(kind="raster", title="", x_label="Baseline (by length)",
                    y_label="Time", x_range=(-0.5, 2.5), y_range=(97.0, 200.0),
                    x_is_time=False, y_is_time=True, agg_n_x=3, agg_n_y=10,
                    color_mode="global")
        s = PanelSpec(**base, y_origin=100.0, x_ticks=tuple(LABELS))
        d = s.to_state_data()
        assert d["y_t0"] == [100.0] and d["x_t0"] == [-0.5]
        assert d["x_cat"] == [LABELS] and d["y_cat"] == [None]
        assert d["full_y0"] == [97.0]
        assert s.axis_label("x") == "Baseline (by length)"
        # defaults reproduce the old behaviour: origin = low end of range
        d = PanelSpec(**base).to_state_data()
        assert d["y_t0"] == [97.0] and d["x_cat"] == [None]


# ---------------------------------------------------------------------------
# Simulated data with gaps in time
# ---------------------------------------------------------------------------

NTIME, NANT, NCHAN, DUMP = 24, 6, 16, 8.0
T_START = 5.0e9
# integrations 0-9, then a 400 s pause, 10-17, a 200 s pause, 18-23
GAPS = ((10, 400.0), (18, 200.0))


def _transform(desc, data):
    dims, vis = data["DATA"]
    tdims, t = data["TIME"]
    t = np.asarray(t, dtype=np.float64).copy()
    idx = np.floor((t - t.min()) / DUMP + 1e-6).astype(int) + int(
        np.floor((t.min() - T_START) / DUMP + 1e-6))
    shift = np.zeros_like(t)
    for first, pause in GAPS:
        shift += np.where(idx >= first, pause, 0.0)
    data["TIME"] = (tdims, t + shift)
    if "TIME_CENTROID" in data:
        cd, c = data["TIME_CENTROID"]
        data["TIME_CENTROID"] = (cd, np.asarray(c, dtype=np.float64) + shift)
    # amplitude = 10 * (1 + integration number) + 0.01 * (ANTENNA1 * 10 + ANTENNA2)
    a1 = np.asarray(data["ANTENNA1"][1]).astype(float)
    a2 = np.asarray(data["ANTENNA2"][1]).astype(float)
    amp = 10.0 * (1.0 + idx) + 0.01 * (a1 * 10 + a2)
    amp = amp.reshape((-1,) + (1,) * (vis.ndim - 1)) * np.ones(vis.shape)
    data["DATA"] = (dims, amp.astype(np.complex64))
    fdims, _ = data["FLAG"]
    data["FLAG"] = (fdims, np.zeros(vis.shape, dtype=bool))
    return data


@pytest.fixture(scope="module")
def sim_ms(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("rgrid") / "g.ms")
    sim.MSStructureSimulator(
        ntime=NTIME, time_chunks=NTIME, dump_rate=DUMP, time_start=T_START,
        nantenna=NANT, auto_corrs=False,
        data_description=[(NCHAN, ["XX", "YY"])],
        simulate_data=True, transform_data=_transform).simulate_ms(path)
    return path


@pytest.fixture(scope="module")
def sim_ps(sim_ms, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("rgridps") / "g.ps.zarr")
    dt = xr.open_datatree(sim_ms, engine="xarray-ms:msv2",
                          partition_schema=["FIELD_ID"])
    dt.to_zarr(out, mode="w", compute=True)
    return out


def _open(kind, sim_ms, sim_ps):
    if kind == "msv2":
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
        b = MSv2Backend(sim_ms)
    else:
        from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
        b = MSv4Backend(sim_ps)
    b.open()
    return b


@pytest.fixture(params=["msv2", "msv4"])
def backend(request, sim_ms, sim_ps):
    b = _open(request.param, sim_ms, sim_ps)
    yield b
    b.close()


def _antenna_positions(sim_ms):
    """Straight from the data tree, not through the code under test."""
    dt = xr.open_datatree(sim_ms, engine="xarray-ms:msv2",
                          partition_schema=["FIELD_ID"])
    for node in dt.subtree:
        if "ANTENNA_POSITION" in node.ds.data_vars:
            a = node.ds
            return {str(n): np.asarray(p, dtype=float) for n, p in zip(
                a.antenna_name.values, a.ANTENNA_POSITION.values)}
    raise AssertionError("no antenna positions in the simulated MS")


# ---------------------------------------------------------------------------
# 5. Real backends
# ---------------------------------------------------------------------------

class TestRealBackends:

    def test_the_simulated_time_axis_has_the_gaps(self, backend):
        agg, *_ = backend.query_raster(Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE,
                                       SelectionSpec(), polarization="XX")
        d = np.diff(agg.coords["time"].values)
        assert len(d) == NTIME - 1
        assert np.isclose(d[9], DUMP + 400) and np.isclose(d[17], DUMP + 200)
        assert np.allclose(np.delete(d, [9, 17]), DUMP)

    def test_identity_tables_carry_lengths_from_antenna_positions(self, backend, sim_ms):
        pos = _antenna_positions(sim_ms)
        tables = backend.identity_tables(SelectionSpec(), polarization="XX")
        assert tables.baseline_lengths is not None
        assert set(tables.baseline_lengths) == set(tables.baseline_antennas)
        for bid, (a1, a2) in tables.baseline_antennas.items():
            want = float(np.linalg.norm(pos[a1] - pos[a2]))
            assert np.isclose(tables.baseline_lengths[bid], want), (bid, a1, a2)

    def test_antenna_positions_are_cached_and_dropped_on_close(self, backend):
        p = backend.antenna_positions()
        assert len(p) == NANT and backend.antenna_positions() is p

    def test_backends_agree(self, sim_ms, sim_ps):
        a, b = _open("msv2", sim_ms, sim_ps), _open("msv4", sim_ms, sim_ps)
        try:
            ta = a.identity_tables(SelectionSpec(), polarization="XX")
            tb = b.identity_tables(SelectionSpec(), polarization="XX")
            assert ta.baseline_antennas == tb.baseline_antennas
            assert ta.baseline_lengths.keys() == tb.baseline_lengths.keys()
            for k in ta.baseline_lengths:
                assert np.isclose(ta.baseline_lengths[k], tb.baseline_lengths[k])
        finally:
            a.close(); b.close()


# ---------------------------------------------------------------------------
# 6. Real raster
# ---------------------------------------------------------------------------

W, H = 300, 480


def _raster(backend, **kw):
    from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster
    args = dict(backend=backend, selection=SelectionSpec(), polarization="XX",
                y_dim=Axis.TIME, x_dim=Axis.BASELINE, quantity=Axis.AMPLITUDE,
                width=W, height=H, headless=True, scaling="linear")
    args.update(kw)
    return VisibilityRaster(**args)


def _image(vr):
    """The drawn RGBA image and the data coordinate of each pixel centre."""
    d = vr._image_source.data
    img = np.asarray(d["image"][0])
    h, w = img.shape
    x = d["x"][0] + (np.arange(w) + 0.5) * d["dw"][0] / w
    y = d["y"][0] + (np.arange(h) + 0.5) * d["dh"][0] / h
    return img, x, y


_T0 = {}


@pytest.fixture(autouse=True)
def _first_time(request):
    """The first integration's time as the backends report it (the MS
    holds MJD seconds; xarray-ms presents unix seconds)."""
    if "sim_ms" not in request.fixturenames and not any(
            f in request.fixturenames for f in ("backend", "plotter")):
        return
    if "t0" not in _T0:
        sim_ms = request.getfixturevalue("sim_ms")
        dt = xr.open_datatree(sim_ms, engine="xarray-ms:msv2",
                              partition_schema=["FIELD_ID"])
        for node in dt.subtree:
            if "VISIBILITY" in node.ds.data_vars:
                _T0["t0"] = float(node.ds.time.values.min())
                break


def _true_times():
    """Integration centres: DUMP apart, with the pauses in GAPS."""
    t = _T0["t0"] + DUMP * np.arange(NTIME)
    for first, pause in GAPS:
        t = t + np.where(np.arange(NTIME) >= first, pause, 0.0)
    return t


class TestRealRaster:

    # -- these two fail on main at 693c99c ---------------------------------

    def test_image_is_blank_in_a_time_gap(self, backend):
        vr = _raster(backend)
        img, _x, y = _image(vr)
        t = vr.agg.coords["time"].values
        # well inside the first pause (10 % .. 90 % of it)
        g0, g1 = t[9] + DUMP / 2, t[10] - DUMP / 2
        rows = (y > g0 + 0.1 * (g1 - g0)) & (y < g1 - 0.1 * (g1 - g0))
        assert rows.sum() > 20
        alpha = (img.view(np.uint8).reshape(img.shape + (4,)))[..., 3]
        assert (alpha[rows] == 0).all(), "data drawn where none was taken"
        # ...and something IS drawn on an integration
        on = np.abs(y - t[3]) < DUMP / 4
        assert on.any() and (alpha[on] > 0).all()

    def test_drawn_rows_are_the_rows_the_axis_says(self, backend):
        # Amplitude rises by 1 per integration, so with linear scaling
        # the colour of a row identifies its integration.
        vr = _raster(backend)
        img, _x, y = _image(vr)
        t = vr.agg.coords["time"].values
        col = img[:, img.shape[1] // 2]
        at_centre = [col[np.argmin(np.abs(y - tk))] for tk in t]
        assert len(set(at_centre)) == NTIME          # all distinguishable
        for k, tk in enumerate(t):
            inside = np.abs(y - tk) < DUMP * 0.4     # pixels within the cell
            assert inside.any()
            assert (col[inside] == at_centre[k]).all(), k

    # ----------------------------------------------------------------------

    def test_axis_ranges_reach_the_cell_edges(self, backend):
        vr = _raster(backend)
        t = _true_times()
        assert np.allclose(vr.agg.coords["time"].values, t)
        assert np.isclose(vr._y_range[0], t[0] - DUMP / 2)
        assert np.isclose(vr._y_range[1], t[-1] + DUMP / 2)
        assert vr._x_range == (-0.5, 14.5)
        # elapsed time still counts from the first integration's centre
        assert np.isclose(vr._y_origin, t[0])
        spec = vr._panel_spec()
        assert np.isclose(spec.axis_origin("y"), t[0])
        assert spec.to_state_data()["y_t0"] == [float(t[0])]

    def test_every_pixel_shows_the_cell_under_it(self, backend):
        for order in ("number", "length"):
            vr = _raster(backend, baseline_order=order)
            agg = vr.agg
            img = vr._resample(agg, vr._x_range, vr._y_range)
            xs, ys = img.coords["baseline_id"].values, img.coords["time"].values
            n_gap = 0
            for j in range(0, H, 5):
                for i in range(0, W, 7):
                    px, py = vr._data_to_pixel(xs[i], ys[j])
                    if px is None:
                        n_gap += 1
                        assert np.isnan(img.values[j, i])
                    else:
                        assert img.values[j, i] == agg.values[py, px]
            assert n_gap > 0

    def test_columns_are_the_baselines_the_mapping_names(self, backend):
        raw, *_ = backend.query_raster(Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE,
                                       SelectionSpec(), polarization="XX")
        for order in ("number", "length"):
            vr = _raster(backend, baseline_order=order)
            ax = vr.baseline_axis
            assert ax.order == order and len(ax) == 15
            assert vr.agg.coords["baseline_id"].values.tolist() == list(range(15))
            for p, bid in enumerate(ax.ids):
                want = raw.sel(baseline_id=bid).values
                assert np.array_equal(vr.agg.values[:, p], want, equal_nan=True)

    def test_length_order_matches_the_antenna_positions(self, backend, sim_ms):
        pos = _antenna_positions(sim_ms)
        vr = _raster(backend, baseline_order="length")
        ax = vr.baseline_axis
        lengths = []
        for name in ax.names:
            a1, a2 = name.split("&")
            lengths.append(float(np.linalg.norm(pos[a1] - pos[a2])))
        assert np.allclose(lengths, ax.lengths)
        assert lengths == sorted(lengths)
        assert sorted(ax.ids) == list(_raster(backend).baseline_axis.ids)
        spec = vr._panel_spec()
        assert spec.axis_label("x") == "Baseline (by length)"
        assert spec.x_ticks == ax.tick_labels() and spec.y_ticks is None
        assert all(lbl.endswith(" m") or lbl.endswith(" km") for lbl in spec.x_ticks)

    def test_number_order_ticks_are_baseline_numbers(self, backend):
        vr = _raster(backend)
        spec = vr._panel_spec()
        assert spec.axis_label("x") == "Baseline"
        assert spec.x_ticks == tuple(str(i) for i in vr.baseline_axis.ids)

    def test_a_subset_is_drawn_side_by_side(self, backend):
        names = backend.metadata()["antenna_names"]
        vr = _raster(backend, selection=SelectionSpec(antenna_names=[names[2]]))
        ax = vr.baseline_axis
        assert len(ax) == NANT - 1
        assert all(names[2] in n.split("&") for n in ax.names)
        assert vr._x_range == (-0.5, NANT - 1.5)
        assert np.isfinite(vr.agg.values).all()        # no empty columns

    def test_one_baseline_is_drawable(self, backend):
        b = backend.metadata()["baselines"][4]
        vr = _raster(backend, selection=SelectionSpec(baselines=[(b[1], b[2])]))
        assert vr.agg.shape == (NTIME, 1)
        assert vr._degenerate_reason is None
        assert vr._x_range == (-0.5, 0.5)
        img, _x, _y = _image(vr)
        assert (img != 0).any()

    def test_readout_names_the_baseline_under_the_cursor(self, backend):
        t = _true_times()
        for order in ("number", "length"):
            vr = _raster(backend, baseline_order=order)
            ax = vr.baseline_axis
            for p in (0, 6, 14):
                r = vr._handle_probe({"x": p + 0.3, "y": t[12] + 1.0})
                pr = r["probe"]
                assert pr["status"] == "ok"
                a1, a2 = ax.names[p].split("&")
                assert pr["flag_key"]["antenna_pairs"] == [(a1, a2)]
                assert f"<b>Baseline:</b> {ax.ids[p]}" in r["label"]
                assert f"<b>BL:</b> {ax.names[p]}" in r["label"]
                assert "<b>Length:</b>" in r["label"]
                # the value is that baseline's, at that integration
                i1, i2 = (int(a.split("-")[1]) for a in (a1, a2))
                assert np.isclose(pr["value"], 10 * (1 + 12) + 0.01 * (i1 * 10 + i2))
                tr = pr["flag_key"]["time_range"]
                assert np.isclose(tr[0], t[12] - DUMP / 2) and np.isclose(tr[1], t[12] + DUMP / 2)

    def test_readout_in_a_gap_says_so(self, backend):
        t = _true_times()
        vr = _raster(backend)
        r = vr._handle_probe({"x": 3.0, "y": (t[9] + t[10]) / 2})
        assert r["probe"]["status"] == "no_data"
        assert "gap" in r["label"]
        # the cell beside the gap does not reach into it
        r = vr._handle_probe({"x": 3.0, "y": t[9] + DUMP / 2 + 1.0})
        assert r["probe"]["status"] == "no_data"
        r = vr._handle_probe({"x": 3.0, "y": t[9] + DUMP / 2 - 1.0})
        assert r["probe"]["status"] == "ok"

    def test_update_axes_changes_the_order(self, backend):
        vr = _raster(backend)
        first = vr.baseline_axis.ids
        vr.update_axes(baseline_order="length")
        assert vr.baseline_order == "length"
        assert vr.baseline_axis.ids != first and sorted(vr.baseline_axis.ids) == sorted(first)
        with pytest.raises(ValueError):
            vr.update_axes(baseline_order="sideways")

    def test_no_baseline_axis_no_mapping(self, backend):
        vr = _raster(backend, x_dim=Axis.CHANNEL)
        assert vr.baseline_axis is None
        spec = vr._panel_spec()
        assert spec.x_ticks is None and spec.y_ticks is None
        assert vr._x_range == (-0.5, NCHAN - 0.5)

    def test_baseline_on_y(self, backend):
        vr = _raster(backend, y_dim=Axis.BASELINE, x_dim=Axis.CHANNEL,
                     baseline_order="length")
        assert vr._y_range == (-0.5, 14.5)
        spec = vr._panel_spec()
        assert spec.y_ticks == vr.baseline_axis.tick_labels() and spec.x_ticks is None
        assert spec.axis_label("y") == "Baseline (by length)"

    def test_png_export_uses_the_same_labels(self, backend):
        pytest.importorskip("matplotlib")
        from cubevis.toolbox.visplot.tick_format import mpl_formatter
        vr = _raster(backend, baseline_order="length")
        spec = vr._panel_spec()
        f = mpl_formatter(spec.x_is_time, spec.axis_origin("x"),
                          spec.axis_scale("x")[0], spec.axis_ticks("x"))
        assert f(2.0, 0) == spec.x_ticks[2] and f(2.5, 0) == ""


# ---------------------------------------------------------------------------
# 7. Real plotter
# ---------------------------------------------------------------------------

def _run(c):
    return asyncio.run(c)


@pytest.fixture(params=["msv2", "msv4"])
def plotter(request, sim_ms, sim_ps):
    from cubevis.toolbox.visplot import VisibilityPlotter
    kw = dict(ms=sim_ms) if request.param == "msv2" else dict(ps=sim_ps)
    vp = VisibilityPlotter(layout="side", correlation="XX,YY",
                           raster_y="TIME", raster_x="BASELINE",
                           baseline_order="length", **kw)
    yield vp
    vp.close()


def _flags_by_time_baseline(vp):
    """{(time index, 'A&B')} currently flagged in XX, all channels."""
    b = vp._reader._backend
    out = set()
    t_all = _true_times()
    for part in b._iter_visibility_partitions(None):
        f = b._flag_mask(part).transpose(
            "time", "baseline_id", "frequency", "polarization").values
        ip = list(part.polarization.values).index("XX")
        a1 = part.baseline_antenna1_name.values.astype(str)
        a2 = part.baseline_antenna2_name.values.astype(str)
        for ti, bi in zip(*np.nonzero(f[:, :, :, ip].all(axis=2))):
            k = int(np.argmin(np.abs(t_all - part.time.values[ti])))
            out.add((k, f"{a1[bi]}&{a2[bi]}"))
    return out


class TestRealPlotter:

    def test_constructor_sets_every_raster_and_its_control(self, plotter):
        for slot in plotter._slots:
            assert slot.raster.baseline_order == "length"
            sel = plotter._panel_axis_widgets[slot.id]["raster"]["border_sel"]
            assert sel.value == "length"
            assert [o[0] for o in sel.options] == ["number", "length"]
        assert plotter._hint_border.text.startswith("<b>Baseline order</b>")

    def test_baseline_axis_ticks_stay_on_whole_positions(self, plotter):
        r = plotter._slots[0].raster                  # Baseline across, Time up
        assert r._fig.xaxis[0].ticker.min_interval == 1
        assert r._fig.yaxis[0].ticker.min_interval == 0

    @pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
    def test_shipped_tick_spacing_javascript(self, plotter, tmp_path):
        fn = _js_function(plotter._do_plot_js, "cvIntegerTicks")
        script = fn + """
const mk = (v) => ({ticker: {min_interval: v}});
const fig = {below: [mk(0), {ticker: 'auto'}, {}], above: [], left: [mk(1)], right: [mk(0)]};
cvIntegerTicks(fig, {data: {x_cat: [['15 m', '20 m']], y_cat: [null]}});
const a = [fig.below[0].ticker.min_interval, fig.left[0].ticker.min_interval,
           fig.right[0].ticker.min_interval, fig.below[1].ticker];
cvIntegerTicks(fig, {data: {x_cat: [null], y_cat: [['1', '2']]}});
const b = [fig.below[0].ticker.min_interval, fig.left[0].ticker.min_interval,
           fig.right[0].ticker.min_interval];
cvIntegerTicks({}, {data: {}});                 // nothing to do, no throw
cvIntegerTicks(fig, {data: {}});                // an older state: all free
const c = [fig.below[0].ticker.min_interval, fig.left[0].ticker.min_interval];
console.log(JSON.stringify({a, b, c}));
"""
        f = tmp_path / "ticks.js"
        f.write_text(script)
        res = subprocess.run(["node", str(f)], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
        got = json.loads(res.stdout)
        assert got == {"a": [1, 0, 0, "auto"], "b": [0, 1, 1], "c": [0, 0]}

    def test_bad_order_fails_at_construction(self, sim_ms):
        from cubevis.toolbox.visplot import VisibilityPlotter
        with pytest.raises(ValueError):
            VisibilityPlotter(ms=sim_ms, baseline_order="uv")

    def test_box_flags_the_baselines_it_encloses_in_length_order(self, plotter):
        vp = plotter
        vp.flag_db.clear(record=False)
        r = vp._slots[0].raster
        assert (r._y_dim, r._x_dim) == (Axis.TIME, Axis.BASELINE)
        ax, t = r.baseline_axis, _true_times()
        # positions 4..6, integrations 11..13
        box = dict(x0=3.7, x1=6.2, y0=t[11] - 1, y1=t[13] + 1, flag=True)
        resp = _run(vp._handle_box_select(box, "raster", r))
        assert resp["notify_text"].startswith("✓"), resp["notify_text"]
        want = {(k, ax.names[p]) for k in (11, 12, 13) for p in (4, 5, 6)}
        assert _flags_by_time_baseline(vp) == want
        d = vp.flag_db.deltas()[-1]
        assert {f"{a}&{b}" for a, b in d.baseline_ids} == {ax.names[p] for p in (4, 5, 6)}
        # the record names baselines, not positions
        assert ax.names[4] in d.comment and "by length" in d.comment
        vp.flag_db.clear(record=False)

    def test_box_in_number_order_selects_different_baselines(self, plotter):
        vp = plotter
        vp.flag_db.clear(record=False)
        r = vp._slots[0].raster
        r.update_axes(baseline_order="number")
        try:
            ax, t = r.baseline_axis, _true_times()
            assert ax.order == "number"
            box = dict(x0=3.7, x1=6.2, y0=t[2] - 1, y1=t[2] + 1, flag=True)
            _run(vp._handle_box_select(box, "raster", r))
            assert _flags_by_time_baseline(vp) == {(2, ax.names[p]) for p in (4, 5, 6)}
        finally:
            vp.flag_db.clear(record=False)
            r.update_axes(baseline_order="length")

    def test_box_over_a_gap_only_flags_nothing(self, plotter):
        vp = plotter
        vp.flag_db.clear(record=False)
        r, t = vp._slots[0].raster, _true_times()
        g0, g1 = t[9] + DUMP / 2, t[10] - DUMP / 2
        box = dict(x0=0.0, x1=14.0, y0=g0 + 5, y1=g1 - 5, flag=True)
        resp = _run(vp._handle_box_select(box, "raster", r))
        assert resp["notify_text"].startswith("⚠ Nothing to flag"), resp["notify_text"]
        assert len(vp.flag_db) == 0 and _flags_by_time_baseline(vp) == set()

    def test_box_from_a_gap_into_data_flags_only_what_it_touches(self, plotter):
        vp = plotter
        vp.flag_db.clear(record=False)
        r, t, ax = vp._slots[0].raster, _true_times(), vp._slots[0].raster.baseline_axis
        g0 = t[9] + DUMP / 2
        # from mid-pause up to just inside integration 10
        box = dict(x0=0.0, x1=0.2, y0=g0 + 100, y1=t[10] - DUMP / 2 + 1, flag=True)
        _run(vp._handle_box_select(box, "raster", r))
        assert _flags_by_time_baseline(vp) == {(10, ax.names[0])}
        vp.flag_db.clear(record=False)

    def test_pending_flag_overlay_is_drawn_inside_the_box(self, plotter):
        vp = plotter
        vp.flag_db.clear(record=False)
        r, t = vp._slots[0].raster, _true_times()
        box = dict(x0=3.7, x1=6.2, y0=t[11] - 1, y1=t[13] + 1, flag=True)
        _run(vp._handle_box_select(box, "raster", r))
        try:
            _run(vp.flags.handle_action({"action": "config", "display": "color"}))
            r._render(r._selection)
            assert r._overlay_aggs, "no pending overlay"
            img = np.zeros((H, W), dtype=np.uint32)
            before = img.copy()
            r._apply_flag_overlays(img, r._x_range, r._y_range)
            painted = img != before
            assert painted.any()
            ys = r._y_range[0] + (np.arange(H) + 0.5) * (r._y_range[1] - r._y_range[0]) / H
            xs = r._x_range[0] + (np.arange(W) + 0.5) * (r._x_range[1] - r._x_range[0]) / W
            jj, ii = np.nonzero(painted)
            assert ys[jj].min() >= t[11] - DUMP / 2 - 1 and ys[jj].max() <= t[13] + DUMP / 2 + 1
            assert xs[ii].min() >= 3.5 - 0.05 and xs[ii].max() <= 6.5 + 0.05
            # ...and fills it: the three cells' centres are painted
            for k in (11, 12, 13):
                for p in (4, 5, 6):
                    assert painted[np.argmin(np.abs(ys - t[k])), np.argmin(np.abs(xs - p))]
        finally:
            vp.flag_db.clear(record=False)
            _run(vp.flags.handle_action({"action": "config", "display": "hide"}))

    def test_plot_request_changes_the_order_and_requeries(self, plotter):
        vp = plotter
        r = vp._slots[0].raster
        assert r.baseline_order == "length"
        seen = []
        cls = type(r)
        orig = cls.update_axes

        def spy(self, **kw):
            if self is r:
                seen.append(kw)
            return orig(self, **kw)
        cls.update_axes = spy
        try:
            msg = _plot_message(vp)
            msg["panels"][vp._slots[0].id]["baseline_order"] = "number"
            resp = _run(vp._handle_plot(msg))
            assert resp.get("status") != "error", resp
            assert seen and seen[-1]["baseline_order"] == "number"
            assert r.baseline_order == "number"
            p = resp["panels"][vp._slots[0].id]
            assert p["x_label"] == "Baseline" and p["state"]["x_cat"][0][0] == str(r.baseline_axis.ids[0])
            # unchanged order: no re-query
            seen.clear()
            _run(vp._handle_plot(msg))
            assert not seen
            # absent (an older client): keeps what the panel has
            del msg["panels"][vp._slots[0].id]["baseline_order"]
            _run(vp._handle_plot(msg))
            assert r.baseline_order == "number" and not seen
        finally:
            cls.update_axes = orig
            r.update_axes(baseline_order="length")


def _js_function(code: str, name: str) -> str:
    """The text of ``function <name>(...) {...}`` inside *code*."""
    start = code.index(f"function {name}(")
    i = code.index("{", start)
    depth = 0
    for j in range(i, len(code)):
        if code[j] == "{":
            depth += 1
        elif code[j] == "}":
            depth -= 1
            if depth == 0:
                return code[start:j + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def _plot_message(vp):
    """A Plot request that changes nothing, as the browser would send it."""
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


# ---------------------------------------------------------------------------
# 8. TW Hya
# ---------------------------------------------------------------------------

_MS = os.environ.get("MS", "sis14_twhya_calibrated_flagged.ms")
needs_twhya = pytest.mark.skipif(not os.path.isdir(_MS),
                                 reason="TW Hya MS not available (set MS=)")


@needs_twhya
class TestTWHya:

    @pytest.fixture(scope="class")
    def tw(self):
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
        b = MSv2Backend(_MS)
        b.open()
        yield b
        b.close()

    def test_gaps_between_scans_are_blank_and_rows_sit_at_their_times(self, tw):
        vr = _raster(tw, height=600, width=400)
        agg = vr.agg
        t = agg.coords["time"].values
        assert np.diff(t).max() > 20 * np.median(np.diff(t))      # it has gaps
        img = vr._resample(agg, vr._x_range, vr._y_range)
        ys = img.coords["time"].values
        half_px = (ys[1] - ys[0]) / 2
        lo, hi = rg.cell_edges(t)
        # a pixel row may show data only if an integration overlaps it
        covered = ((ys[:, None] + half_px >= lo[None, :])
                   & (ys[:, None] - half_px <= hi[None, :])).any(axis=1)
        drawn = np.isfinite(img.values).any(axis=1)
        assert not (drawn & ~covered).any()
        assert 0.3 < drawn.mean() < 0.7                            # about half is gap
        # and a row that IS drawn shows the integrations at its own time:
        # compare with the mean of the cells whose centres it contains
        for j in np.flatnonzero(drawn)[::17]:
            inside = (t >= ys[j] - half_px) & (t < ys[j] + half_px)
            if inside.sum() != 1:
                continue
            k = int(np.flatnonzero(inside)[0])
            a = agg.values[k]
            ok = np.isfinite(a)
            xs = img.coords["baseline_id"].values
            cols = np.clip(np.round(xs).astype(int), 0, agg.shape[1] - 1)
            assert np.allclose(img.values[j], a[cols], equal_nan=True), (j, k)

    def test_baselines_without_rows_are_not_drawn(self, tw):
        vr = _raster(tw)
        tables = tw.identity_tables(SelectionSpec(), polarization="XX")
        assert len(vr.baseline_axis) == len(tables.baselines_with_data) < len(tables.baseline_antennas)
        assert vr._x_range == (-0.5, len(vr.baseline_axis) - 0.5)

    def test_one_antennas_baselines_side_by_side_by_length(self, tw):
        vr = _raster(tw, selection=SelectionSpec(antenna_names=["DA44"]),
                     baseline_order="length")
        ax = vr.baseline_axis
        assert all("DA44" in n.split("&") for n in ax.names)
        assert list(ax.lengths) == sorted(ax.lengths)
        assert 10 < ax.lengths[0] < ax.lengths[-1] < 1000          # ALMA compact, metres
        # the readout at each position names that baseline
        t = vr.agg.coords["time"].values
        for p in (0, len(ax) - 1):
            r = vr._handle_probe({"x": float(p), "y": float(t[5])})
            assert f"<b>BL:</b> {ax.names[p]}" in r["label"]
