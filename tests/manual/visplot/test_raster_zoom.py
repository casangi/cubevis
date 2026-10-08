"""
test_raster_zoom.py
===================
Zooming a raster that had to be decimated (2026-10-07): the zoomed
region is queried again at higher resolution and held *beside* the full
aggregate.

Location in repository:
    cubevis/tests/manual/visplot/test_raster_zoom.py

Run:
    pytest cubevis/tests/manual/visplot/test_raster_zoom.py -v

Before this, the re-query replaced the full aggregate.  On ``main`` at
``50b414f``:

* on a Channel axis it raised (channel numbers were passed as Hz):
  ``test_channel_axis_zoom_does_not_raise``;
* zooming back out showed the zoomed region alone on a blank plot:
  ``test_zoom_out_draws_the_whole_raster_again``;
* the readout's time origin moved to the zoomed region:
  ``test_ranges_and_time_origin_survive_a_zoom``.

Those three use only what existed then and fail there.  (They set
``_is_decimated`` by hand, because of a fourth defect: the backends
reported decimation only from their final pass, so a store with one
partition came back "not decimated" however much had been strided out,
and the raster never asked for detail at all.
``test_backends_report_per_partition_decimation`` covers that.)

Sections
--------
1. Real raster     zoom in / pan / zoom out, both backends, three axes
2. Real plotter    flags and overlays while zoomed

Everything runs on a small simulated MSv2 (and its MSv4 zarr twin) with
``max_cells`` set low enough to force decimation.  TW Hya is never
decimated at the default budget, so there is no real-data section.
"""
from __future__ import annotations

import asyncio
import warnings

import numpy as np
import pytest

from cubevis_test_paths import ensure_cubevis_importable
ensure_cubevis_importable()

xr = pytest.importorskip("xarray")

from cubevis.toolbox.visplot.axes import Axis                         # noqa: E402
from cubevis.toolbox.visplot.selection import SelectionSpec           # noqa: E402

warnings.filterwarnings("ignore")

NTIME, NANT, NCHAN, DUMP = 60, 5, 32, 8.0
T_START = 5.0e9
GAP_AT, GAP = 40, 600.0            # a pause before integration 40
NBL = NANT * (NANT - 1) // 2
MAX_CELLS = 200                    # 60 x 32 = 1920 cells: decimated


def _transform(desc, data):
    dims, vis = data["DATA"]
    tdims, t = data["TIME"]
    t = np.asarray(t, dtype=np.float64).copy()
    idx = np.floor((t - t.min()) / DUMP + 1e-6).astype(int) + int(
        np.floor((t.min() - T_START) / DUMP + 1e-6))
    shift = np.where(idx >= GAP_AT, GAP, 0.0)
    data["TIME"] = (tdims, t + shift)
    if "TIME_CENTROID" in data:
        cd, c = data["TIME_CENTROID"]
        data["TIME_CENTROID"] = (cd, np.asarray(c, dtype=np.float64) + shift)
    # amplitude = 1000 * integration + 10 * channel + baseline code: every
    # sample of a baseline is distinct, so a cell's value says exactly
    # which integration and channel it came from.
    a1 = np.asarray(data["ANTENNA1"][1]).astype(float)
    a2 = np.asarray(data["ANTENNA2"][1]).astype(float)
    nchan = vis.shape[1]
    amp = (1000.0 * idx + (a1 * 2 + a2) * 0.1).reshape(-1, 1, 1) \
        + 10.0 * np.arange(nchan).reshape(1, -1, 1) + np.zeros(vis.shape)
    data["DATA"] = (dims, (amp + 1.0).astype(np.complex64))
    fdims, _ = data["FLAG"]
    data["FLAG"] = (fdims, np.zeros(vis.shape, dtype=bool))
    return data


@pytest.fixture(scope="module")
def sim_ms(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("rzoom") / "z.ms")
    sim.MSStructureSimulator(
        ntime=NTIME, time_chunks=NTIME, dump_rate=DUMP, time_start=T_START,
        nantenna=NANT, auto_corrs=False,
        data_description=[(NCHAN, ["XX", "YY"])],
        simulate_data=True, transform_data=_transform).simulate_ms(path)
    return path


@pytest.fixture(scope="module")
def sim_ps(sim_ms, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("rzoomps") / "z.ps.zarr")
    dt = xr.open_datatree(sim_ms, engine="xarray-ms:msv2",
                          partition_schema=["FIELD_ID"])
    dt.to_zarr(out, mode="w", compute=True)
    return out


class _Counting:
    """A backend that counts its raster queries."""

    def __init__(self, backend):
        self._b = backend
        self.calls = []

    def __getattr__(self, name):
        return getattr(self._b, name)

    def query_raster(self, *a, **k):
        self.calls.append(k.get("selection"))
        return self._b.query_raster(*a, **k)


@pytest.fixture(params=["msv2", "msv4"])
def backend(request, sim_ms, sim_ps):
    if request.param == "msv2":
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
        b = MSv2Backend(sim_ms)
    else:
        from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
        b = MSv4Backend(sim_ps)
    b.open()
    yield _Counting(b)
    b.close()


def _one_baseline(backend):
    b = backend.metadata()["baselines"][3]
    return (b[1], b[2])


def _raster(backend, x_dim=Axis.CHANNEL, y_dim=Axis.TIME, max_cells=MAX_CELLS,
            selection=None, **kw):
    from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster
    if selection is None:
        selection = SelectionSpec(baselines=[_one_baseline(backend)])
    vr = VisibilityRaster(
        backend=backend, selection=selection, polarization="XX",
        y_dim=y_dim, x_dim=x_dim,
        width=240, height=200, headless=True, scaling="linear",
        max_cells=max_cells, **{"quantity": Axis.AMPLITUDE, **kw})
    backend.calls.clear()
    return vr


def _truth(backend, x_dim=Axis.CHANNEL, y_dim=Axis.TIME, **sel):
    """The same raster, never decimated."""
    if "baselines" not in sel and "antenna_names" not in sel:
        sel["baselines"] = [_one_baseline(backend)]
    agg, *_ = backend._b.query_raster(y_dim, x_dim, Axis.AMPLITUDE,
                                      SelectionSpec(**sel), polarization="XX",
                                      max_cells=10_000_000)
    return agg


def _zoom_box(vr, fx=(0.30, 0.42), fy=(0.05, 0.20)):
    """A viewport as fractions of the full ranges."""
    X, Y = vr._x_range, vr._y_range
    return (X[0] + fx[0] * (X[1] - X[0]), X[0] + fx[1] * (X[1] - X[0]),
            Y[0] + fy[0] * (Y[1] - Y[0]), Y[0] + fy[1] * (Y[1] - Y[0]))


# ---------------------------------------------------------------------------
# 1. Real raster
# ---------------------------------------------------------------------------

class TestRealRaster:

    # -- these three fail on main at 50b414f --------------------------------

    def test_backends_report_per_partition_decimation(self, backend):
        b = _one_baseline(backend)
        sel = SelectionSpec(baselines=[b])
        agg, _x, _y, dec = backend._b.query_raster(
            Axis.TIME, Axis.CHANNEL, Axis.AMPLITUDE, sel, polarization="XX",
            max_cells=MAX_CELLS)
        assert agg.size <= MAX_CELLS < NTIME * NCHAN and dec is True
        agg, _x, _y, dec = backend._b.query_raster(
            Axis.TIME, Axis.CHANNEL, Axis.AMPLITUDE, sel, polarization="XX",
            max_cells=10_000_000)
        assert agg.shape == (NTIME, NCHAN) and dec is False

    def test_channel_axis_zoom_does_not_raise(self, backend):
        vr = _raster(backend)
        vr._is_decimated = True                # see the module docstring
        out = vr._do_viewport_rerender(*_zoom_box(vr))
        assert (out["image"] != 0).any()

    @pytest.mark.parametrize("x_dim", [Axis.CHANNEL, Axis.FREQUENCY])
    def test_zoom_out_draws_the_whole_raster_again(self, backend, x_dim):
        vr = _raster(backend, x_dim=x_dim)
        vr._is_decimated = True
        X, Y = vr._x_range, vr._y_range
        before = vr._do_viewport_rerender(X[0], X[1], Y[0], Y[1])["image"]
        vr._do_viewport_rerender(*_zoom_box(vr))
        after = vr._do_viewport_rerender(X[0], X[1], Y[0], Y[1])["image"]
        assert (before != 0).mean() > 0.3          # the rest is the pause
        assert np.array_equal(before, after)

    @pytest.mark.parametrize("x_dim", [Axis.CHANNEL, Axis.FREQUENCY])
    def test_ranges_and_time_origin_survive_a_zoom(self, backend, x_dim):
        vr = _raster(backend, x_dim=x_dim)
        vr._is_decimated = True
        X, Y = vr._x_range, vr._y_range
        t = _truth(backend, x_dim=x_dim).coords["time"].values
        x0, x1, _y0, _y1 = _zoom_box(vr)

        def label():
            return vr._handle_probe({"x": (x0 + x1) / 2, "y": float(t[5])})["label"]
        want = label()
        vr._do_viewport_rerender(*_zoom_box(vr))
        assert (vr._x_range, vr._y_range) == (X, Y)
        got = label()
        tm = lambda s: s.split("<b>Time:</b>")[1].split("&nbsp;")[0].strip()
        assert tm(got) == tm(want) == "40.0 s"       # integration 5, 8 s apart

    # ----------------------------------------------------------------------

    @pytest.mark.parametrize("x_dim", [Axis.CHANNEL, Axis.FREQUENCY])
    def test_detail_is_the_undecimated_data_of_the_region(self, backend, x_dim):
        vr = _raster(backend, x_dim=x_dim)
        full = vr.agg
        truth = _truth(backend, x_dim=x_dim)
        assert full.size < truth.size
        x0, x1, y0, y1 = _zoom_box(vr)
        vr._do_viewport_rerender(x0, x1, y0, y1)
        assert len(backend.calls) == 1
        d = vr._detail
        assert d is not None and vr._detail_on and not d.is_decimated
        assert vr.agg is full                              # never replaced
        xd, yd = d.agg.dims[1], d.agg.dims[0]
        # every held cell is the true cell at that coordinate
        want = truth.sel({xd: d.agg.coords[xd].values, yd: d.agg.coords[yd].values})
        assert np.array_equal(d.agg.values, want.values, equal_nan=True)
        # ...and it holds everything in the viewport, plus the margin
        tx, ty = truth.coords[xd].values, truth.coords[yd].values
        in_view = truth.sel({xd: tx[(tx >= x0) & (tx <= x1)],
                             yd: ty[(ty >= y0) & (ty <= y1)]})
        assert in_view.size > 0
        got = d.agg.sel({xd: in_view.coords[xd].values, yd: in_view.coords[yd].values})
        assert np.array_equal(got.values, in_view.values)
        assert d.agg.sizes[xd] > in_view.sizes[xd]

    def test_channel_numbers_are_those_of_the_whole_selection(self, backend):
        vr = _raster(backend)
        vr._do_viewport_rerender(*_zoom_box(vr))
        d = vr._detail.agg
        chans = d.coords["frequency"].values
        assert chans.min() > 0 and chans.max() < NCHAN
        # amplitude = 1000 * integration + 10 * channel + ...: the tens
        # digit is the channel number
        got = np.round((d.values % 1000.0) / 10.0 - 0.1)
        assert np.array_equal(got, np.broadcast_to(chans[None, :], d.shape))

    def test_readout_follows_the_picture(self, backend):
        vr = _raster(backend)
        truth = _truth(backend)
        t = truth.coords["time"].values
        x0, x1, y0, y1 = _zoom_box(vr)
        vr._do_viewport_rerender(x0, x1, y0, y1)
        k = int(np.flatnonzero((t >= y0) & (t <= y1))[1])
        c = int(np.ceil(x0)) + 1
        r = vr._handle_probe({"x": float(c), "y": float(t[k])})
        assert r["probe"]["status"] == "ok"
        assert np.isclose(r["probe"]["value"], truth.values[k, c])
        assert f"<b>Channel:</b> {c}" in r["label"]
        # zoomed out again the readout reads the (coarser) full aggregate
        vr._do_viewport_rerender(*vr._x_range, *vr._y_range)
        assert not vr._detail_on
        r = vr._handle_probe({"x": float(c), "y": float(t[k])})
        assert r["probe"]["value"] in vr.agg.values

    def test_small_pan_and_zoom_out_cost_no_query(self, backend):
        vr = _raster(backend)
        x0, x1, y0, y1 = _zoom_box(vr)
        vr._do_viewport_rerender(x0, x1, y0, y1)
        assert len(backend.calls) == 1
        dx, dy = 0.2 * (x1 - x0), 0.2 * (y1 - y0)
        out = vr._do_viewport_rerender(x0 + dx, x1 + dx, y0 + dy, y1 + dy)
        assert len(backend.calls) == 1 and vr._detail_on
        assert (out["x0"], out["x1"], out["y0"], out["y1"]) == (
            x0 + dx, x1 + dx, y0 + dy, y1 + dy)
        vr._do_viewport_rerender(*vr._x_range, *vr._y_range)
        assert len(backend.calls) == 1 and not vr._detail_on
        # back in: the held detail still serves
        vr._do_viewport_rerender(x0, x1, y0, y1)
        assert len(backend.calls) == 1 and vr._detail_on

    def test_a_pan_beyond_the_margin_queries_again(self, backend):
        vr = _raster(backend)
        x0, x1, y0, y1 = _zoom_box(vr)
        vr._do_viewport_rerender(x0, x1, y0, y1)
        w = x1 - x0
        vr._do_viewport_rerender(x0 + 3 * w, x1 + 3 * w, y0, y1)
        assert len(backend.calls) == 2 and vr._detail_on

    def test_the_answer_is_the_viewport_that_was_asked_for(self, backend):
        vr = _raster(backend)
        box = _zoom_box(vr)
        out = vr._do_viewport_rerender(*box)
        assert (out["x0"], out["x1"], out["y0"], out["y1"]) == box
        assert vr._current_viewport == box
        assert out["image"].shape == (vr._height, vr._width)

    def test_colours_stay_anchored_to_the_whole_selection(self, backend):
        vr = _raster(backend)
        s0 = vr._panel_spec()
        vr._do_viewport_rerender(*_zoom_box(vr))
        s1 = vr._panel_spec()
        assert (s0.x_range, s0.y_range, s0.agg_n_x, s0.agg_n_y) == (
            s1.x_range, s1.y_range, s1.agg_n_x, s1.agg_n_y)
        assert vr._state_source.data["y_t0"] == [float(vr._y_origin)]

    def test_no_query_when_not_decimated(self, backend):
        vr = _raster(backend, max_cells=2_000_000)
        assert not vr._is_decimated
        vr._do_viewport_rerender(*_zoom_box(vr, (0.4, 0.42), (0.1, 0.12)))
        assert backend.calls == [] and vr._detail is None

    def test_no_query_for_a_modest_zoom(self, backend):
        # More than _DETAIL_ZOOM of the range: nothing to gain.
        vr = _raster(backend)
        vr._do_viewport_rerender(*_zoom_box(vr, (0.1, 0.9), (0.1, 0.9)))
        assert backend.calls == [] and vr._detail is None

    def test_quantities_that_look_beyond_the_cell_stay_on_the_full_aggregate(self, backend):
        for q in (Axis.Z_SCORE, Axis.PHASE_RMS, Axis.AMP_VDIFF):
            vr = _raster(backend, selection=SelectionSpec(), quantity=q)
            assert vr._is_decimated
            out = vr._do_viewport_rerender(*_zoom_box(vr))
            assert backend.calls == [] and vr._detail is None, q.name
            assert out["image"].shape == (vr._height, vr._width)

    def test_a_selection_with_a_channel_range(self, backend):
        sel = SelectionSpec(baselines=[_one_baseline(backend)], channel_range=(8, 28))
        vr = _raster(backend, selection=sel, max_cells=120)
        assert vr._is_decimated
        # 20 channels, numbered from 0 (strided, so the cells are wide)
        assert vr._x_range[0] < 0 and 19 <= vr._x_range[1] <= 20
        x0, x1, y0, y1 = _zoom_box(vr, (0.30, 0.50), (0.05, 0.20))
        vr._do_viewport_rerender(x0, x1, y0, y1)
        d = vr._detail
        assert d is not None and vr._detail_on
        # the narrowed query moved the channel range, not a frequency range
        q = backend.calls[-1]
        assert q.freq_range is None and q.channel_range[0] >= 8 and q.channel_range[1] <= 28
        chans = d.agg.coords["frequency"].values
        assert chans.min() >= 0 and chans.max() <= 19
        # displayed channel c is channel 8 + c of the data
        got = np.round((d.agg.values % 1000.0) / 10.0 - 0.1)
        assert np.array_equal(got, np.broadcast_to(chans[None, :] + 8, d.agg.shape))

    def test_time_by_baseline(self, backend):
        vr = _raster(backend, x_dim=Axis.BASELINE, selection=SelectionSpec(),
                     max_cells=150, baseline_order="length")
        assert vr._is_decimated
        ids = vr.baseline_axis.ids
        x0, x1, y0, y1 = _zoom_box(vr, (0.0, 1.0), (0.05, 0.20))
        vr._do_viewport_rerender(x0, x1, y0, y1)
        d = vr._detail
        assert d is not None and vr._detail_on
        assert vr.baseline_axis.ids == ids                # the layout did not move
        # the detail was asked for the baselines on the axis, by name
        names = [tuple(n.split("&")) for n in vr.baseline_axis.names]
        assert sorted(backend.calls[-1].baselines) == sorted(names)
        truth = _truth(backend, x_dim=Axis.BASELINE, antenna_names=None)
        for j, p in enumerate(d.agg.coords["baseline_id"].values.astype(int)):
            want = truth.sel(baseline_id=ids[p], time=d.agg.coords["time"].values).values
            assert np.array_equal(d.agg.values[:, j], want)
        # more integrations than the full aggregate has in that stretch
        t_full = vr.agg.coords["time"].values
        t_det = d.agg.coords["time"].values
        assert ((t_det >= y0) & (t_det <= y1)).sum() > ((t_full >= y0) & (t_full <= y1)).sum()

    def test_a_viewport_in_a_gap(self, backend):
        vr = _raster(backend)
        t = _truth(backend).coords["time"].values
        g0, g1 = t[GAP_AT - 1] + DUMP, t[GAP_AT] - DUMP
        assert g1 - g0 > 400
        X = vr._x_range
        out = vr._do_viewport_rerender(X[0] + 2, X[0] + 6, g0 + 100, g0 + 200)
        assert not (out["image"] != 0).any()              # nothing there, nothing drawn
        assert vr._detail is None and not vr._detail_on

    def test_a_failed_detail_query_still_draws(self, backend):
        vr = _raster(backend)
        orig = backend._b.query_raster

        def boom(*a, **k):
            raise RuntimeError("store went away")
        backend._b.query_raster = boom
        try:
            out = vr._do_viewport_rerender(*_zoom_box(vr))
        finally:
            backend._b.query_raster = orig
        assert vr._detail is None and (out["image"] != 0).any()

    def test_a_new_render_drops_the_detail(self, backend):
        vr = _raster(backend)
        vr._do_viewport_rerender(*_zoom_box(vr))
        assert vr._detail is not None
        vr.update_axes(quantity=Axis.PHASE)
        assert vr._detail is None and not vr._detail_on


# ---------------------------------------------------------------------------
# 2. Real plotter
# ---------------------------------------------------------------------------

def _run(c):
    return asyncio.run(c)


@pytest.fixture(params=["msv2", "msv4"])
def plotter(request, sim_ms, sim_ps):
    from cubevis.toolbox.visplot import VisibilityPlotter
    kw = dict(ms=sim_ms) if request.param == "msv2" else dict(ps=sim_ps)
    vp = VisibilityPlotter(layout="side", correlation="XX,YY",
                           raster_y="TIME", raster_x="CHANNEL", **kw)
    r = vp._slots[0].raster
    r._max_cells = MAX_CELLS
    r._render(r._selection)
    assert r._is_decimated
    yield vp
    vp.close()


class TestRealPlotter:

    def test_flagging_while_zoomed(self, plotter):
        vp = plotter
        vp.flag_db.clear(record=False)
        r = vp._slots[0].raster
        X, Y = r._x_range, r._y_range
        box_view = dict(x0=X[0] + 8, x1=X[0] + 14, y0=Y[0] + 30, y1=Y[0] + 110)
        r._handle_rerender(dict(box_view))
        assert r._detail_on
        t = r._detail.agg.coords["time"].values
        k = int(np.flatnonzero((t >= box_view["y0"]) & (t <= box_view["y1"]))[2])
        # one channel (10), one integration, as seen in the detail
        resp = _run(vp._handle_box_select(
            dict(x0=9.8, x1=10.2, y0=float(t[k]) - 1, y1=float(t[k]) + 1, flag=True),
            "raster", r))
        try:
            assert resp["notify_text"].startswith("✓ Flagged:"), resp["notify_text"]
            assert f"{NBL} samples on {NBL} baselines" in resp["notify_text"]
            _run(vp.flags.handle_action({"action": "config", "display": "color"}))
            # the redraw the browser asks for after a flag change
            out = r._handle_rerender(dict(box_view))
            assert r._detail_on and r._detail.overlays, "no overlay in the detail"
            img = np.zeros((r._height, r._width), dtype=np.uint32)
            r._apply_flag_overlays(img, (box_view["x0"], box_view["x1"]),
                                   (box_view["y0"], box_view["y1"]),
                                   overlays=r._detail.overlays)
            jj, ii = np.nonzero(img)
            assert jj.size
            ys = box_view["y0"] + (jj + 0.5) * (box_view["y1"] - box_view["y0"]) / r._height
            xs = box_view["x0"] + (ii + 0.5) * (box_view["x1"] - box_view["x0"]) / r._width
            assert xs.min() >= 9.5 - 0.05 and xs.max() <= 10.5 + 0.05
            assert ys.min() >= t[k] - DUMP / 2 - 0.5 and ys.max() <= t[k] + DUMP / 2 + 0.5
            assert out["image"].shape == (r._height, r._width)
        finally:
            vp.flag_db.clear(record=False)
            _run(vp.flags.handle_action({"action": "config", "display": "hide"}))

    def test_a_replot_starts_from_the_whole_selection(self, plotter):
        vp = plotter
        r = vp._slots[0].raster
        X, Y = r._x_range, r._y_range
        r._handle_rerender(dict(x0=X[0] + 8, x1=X[0] + 14, y0=Y[0] + 30, y1=Y[0] + 110))
        assert r._detail is not None
        r.update_axes(baseline_combine="max")
        try:
            assert r._detail is None and (r._x_range, r._y_range) == (X, Y)
        finally:
            r.update_axes(baseline_combine="mean")
