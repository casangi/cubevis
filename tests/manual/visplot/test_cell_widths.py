"""
test_cell_widths.py
===================
Issue #38: a lone channel or integration was drawn as wide as the gap to
its neighbour.  ``raster_grid.cell_edges`` takes a cell's width from its
nearer neighbour; a cell joined to neither neighbour has its nearer
neighbour across a gap, so it reached halfway across that gap (a single
integration between two scans 400 s away: a cell about 400 s wide).  The
reader now sends the true widths (integration time, channel width) with
the raster aggregate, and such a cell is drawn its own width -- by the
image, the readout and the flag engine alike.

Location in repository:
    cubevis/tests/manual/visplot/test_cell_widths.py

Run:
    pytest cubevis/tests/manual/visplot/test_cell_widths.py -v

Sections
--------
1. cell_edges / TimeAxis with widths    the rule (no data)
2. Reader helpers and the merge          partition widths, merge, channel index
3. Real backends                         the widths ride with the aggregate
4. Real raster                           image, readout, probe
5. Real plotter                          a flag box selects what is drawn

1-2 need no data; 3-5 build a small simulated MSv2 (and its MSv4 zarr
twin) with one integration alone between two pauses.

Every test in ``TestRealRaster`` and
``TestRealPlotter.test_box_in_the_gap_beside_it_flags_nothing`` FAIL on
``main`` at ``30560f08`` (the ``debug-0003`` merge).  Times here are unix
seconds (~1.7e9): compare them with an *absolute* tolerance --
``pytest.approx``'s default relative one is about 1700 s.

What is NOT covered: anything only a browser does; a lone channel on real
data (the simulator's channel widths do not match its frequency spacing,
and its one-channel window has width 0) -- that case is covered by the
rule and merge tests in sections 1-2.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import warnings

import numpy as np
import pytest

from cubevis_test_paths import ensure_cubevis_importable
ensure_cubevis_importable()

xr = pytest.importorskip("xarray")

from cubevis.toolbox.gridplot import raster_grid as rg               # noqa: E402
from cubevis.toolbox.visplot.axes import Axis                         # noqa: E402
from cubevis.toolbox.visplot.selection import SelectionSpec           # noqa: E402

warnings.filterwarnings("ignore")


def _edges(lo, hi):
    return [(float(a), float(b)) for a, b in zip(lo, hi)]


# ---------------------------------------------------------------------------
# 1. The rule
# ---------------------------------------------------------------------------

class TestCellEdges:

    T = [0.0, 6.0, 12.0, 18.0, 3000.0]

    def test_without_widths_nothing_changes(self):
        # The old rule, kept for a reader that sends no widths: the lone
        # cell reaches halfway to its neighbour (the bug, by design here).
        assert _edges(*rg.cell_edges(self.T))[-1] == (1509.0, 4491.0)

    def test_a_lone_cell_is_its_own_width(self):
        e = _edges(*rg.cell_edges(self.T, width=6.0))
        assert e[-1] == (2997.0, 3003.0)
        assert e[:4] == [(-3.0, 3.0), (3.0, 9.0), (9.0, 15.0), (15.0, 21.0)]

    def test_any_order_and_one_width_per_cell(self):
        t = [3000.0, 0.0, 6.0, 12.0, 18.0]
        assert _edges(*rg.cell_edges(t, width=[6.0] * 5))[0] == (2997.0, 3003.0)

    def test_a_single_cell_has_its_width(self):
        assert _edges(*rg.cell_edges([5.0], width=6.0)) == [(2.0, 8.0)]
        assert _edges(*rg.cell_edges([5.0])) == [(5.0, 5.0)]   # unknown: as before

    def test_a_decimated_run_is_a_run(self):
        # Every 4th 6 s integration: the reader sends 24 s widths
        # (scale_cell_widths), and the cells are 24 s, side by side.
        e = _edges(*rg.cell_edges([0.0, 24.0, 48.0, 3000.0], width=24.0))
        assert e[:3] == [(-12.0, 12.0), (12.0, 36.0), (36.0, 60.0)]
        assert e[3] == (2988.0, 3012.0)

    def test_two_lone_cells_do_not_join(self):
        # Before #38 each took its width from the other and they were
        # drawn as one 100 s band.
        assert _edges(*rg.cell_edges([0.0, 100.0])) == [(-50.0, 50.0), (50.0, 150.0)]
        assert _edges(*rg.cell_edges([0.0, 100.0], width=6.0)) == [
            (-3.0, 3.0), (97.0, 103.0)]

    def test_spacing_a_little_wider_than_the_width_is_still_a_run(self):
        # Channel width 0.875 of the spacing (the simulator's): no stripes.
        f = np.arange(8.0)
        lo, hi = rg.cell_edges(f, width=0.875)
        assert np.allclose(hi[:-1], lo[1:])

    def test_never_wider_than_half_way_to_the_neighbour(self):
        # A width that would overlap the neighbour is capped.
        lo, hi = rg.cell_edges([0.0, 6.0, 12.0, 40.0], width=[6.0, 6.0, 6.0, 100.0])
        assert hi[2] <= lo[3]
        assert (float(lo[3]), float(hi[3])) == (26.0, 54.0)

    def test_unknown_and_zero_widths_fall_back(self):
        t = [0.0, 6.0, 12.0, 3000.0]
        old = _edges(*rg.cell_edges(t))
        assert _edges(*rg.cell_edges(t, width=[6, 6, 6, np.nan])) == old
        assert _edges(*rg.cell_edges(t, width=0.0)) == old

    def test_a_negative_width_is_a_magnitude(self):
        # A lower-sideband window's channel width is negative.
        assert _edges(*rg.cell_edges([5.0], width=-2.0)) == [(4.0, 6.0)]

    def test_spectral_windows_of_different_widths(self):
        # Window A: 4 channels 1 MHz apart; a one-channel window B 2 MHz
        # wide far away; window C: one channel 0.5 MHz wide.
        f = np.array([100.0, 101.0, 102.0, 103.0, 200.0, 300.0])
        w = np.array([1.0, 1.0, 1.0, 1.0, 2.0, 0.5])
        e = _edges(*rg.cell_edges(f, width=w))
        assert e[4] == (199.0, 201.0) and e[5] == (299.75, 300.25)
        assert e[0] == (99.5, 100.5)

    def test_half_wins_over_width(self):
        assert _edges(*rg.cell_edges([0.0, 1.0, 7.0], half=0.5, width=99.0)) == [
            (-0.5, 0.5), (0.5, 1.5), (6.5, 7.5)]

    def test_wrong_number_of_widths(self):
        with pytest.raises(ValueError):
            rg.cell_edges([0.0, 1.0, 2.0], width=[1.0, 1.0])

    def test_helpers_pass_the_width_on(self):
        t, w = self.T, 6.0
        assert rg.extent(t, width=w) == (-3.0, 3003.0)
        assert rg.locate(t, 3010.0, width=w) == -1                 # in the gap now
        assert rg.locate(t, 3010.0) == 4                           # before #38
        assert rg.locate(t, 3001.0, width=w) == 4
        assert not rg.overlapping(t, 2000.0, 2990.0, width=w)[4]
        assert rg.overlapping(t, 2000.0, 2990.0)[4]

    def test_resample_draws_the_lone_cell_narrow(self):
        t = np.array(self.T)
        vals = np.arange(5.0).reshape(1, 5)
        out = rg.resample(vals, [0.0], t, (-3.0, 3200.0), (-0.5, 0.5),
                          3203, 1, y_half=0.5, x_width=6.0)
        px = np.arange(3203) - 3.0 + 0.5             # pixel centres, 1 s each
        drawn = np.isfinite(out[0])
        assert drawn[(px > 2997) & (px < 3003)].all()
        assert not drawn[(px > 2000) & (px < 2996)].any()
        assert not drawn[px > 3004].any()

    def test_cell_widths_reads_the_coordinate(self):
        a = xr.DataArray(np.zeros((2, 3)), dims=("time", "baseline_id"),
                         coords={"time": [0.0, 8.0], "baseline_id": [0, 1, 2],
                                 rg.cell_width_name("time"): ("time", [8.0, 8.0])})
        assert list(rg.cell_widths(a, "time")) == [8.0, 8.0]
        assert rg.cell_widths(a, "baseline_id") is None
        assert rg.cell_width_name("frequency") == "frequency_cell_width"


class TestTimeAxisWithWidths:

    T = [0.0, 8.0, 16.0, 400.0, 700.0, 708.0]

    def test_a_lone_integration_spans_its_own_time(self):
        ax = rg.TimeAxis.build(self.T, width=8.0)
        i = ax.times.index(400.0)
        assert (ax.lo[i], ax.hi[i]) == (396.0, 404.0)
        assert ax.positions == (0.0, 1.0, 2.0, 4.0, 6.0, 7.0)
        assert ax.real_span(3.6, 4.4) == (396.0, 404.0)

    def test_without_widths_as_before(self):
        ax = rg.TimeAxis.build(self.T)
        i = ax.times.index(400.0)
        assert ax.hi[i] - ax.lo[i] >= 300

    def test_repeated_times_take_the_larger_width(self):
        ax = rg.TimeAxis.build([0.0, 8.0, 400.0, 400.0], width=[8.0, 8.0, 4.0, 8.0])
        assert (ax.lo[-1], ax.hi[-1]) == (396.0, 404.0)


# ---------------------------------------------------------------------------
# 2. Reader helpers and the merge
# ---------------------------------------------------------------------------

def _load(rel):
    """A module from the source tree on its own (no package import)."""
    src = os.environ.get("CUBEVIS_SRC") or os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "cubevis")
    path = os.path.join(src, "toolbox", "visplot", rel)
    spec = importlib.util.spec_from_file_location(os.path.basename(rel)[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _part(times, freqs, value, tw=None, fw=None):
    coords = {"time": np.asarray(times, float), "frequency": np.asarray(freqs, float)}
    if tw is not None:
        coords["time_cell_width"] = ("time", np.full(len(times), tw))
    if fw is not None:
        coords["frequency_cell_width"] = ("frequency", np.full(len(freqs), fw))
    return xr.DataArray(np.full((len(times), len(freqs)), value, float),
                        dims=("time", "frequency"), coords=coords)


class TestMerge:

    def test_the_standalone_name_rule_matches_raster_grid(self):
        m = _load("data/_raster_merge.py")
        assert m.cell_width_name("time") == rg.cell_width_name("time")

    def test_widths_are_placed_on_the_union_axes(self):
        from cubevis.toolbox.visplot.data._raster_merge import merge_raster_partitions
        a = _part([0, 8, 16], [100, 101], 1.0, tw=8.0, fw=1.0)
        b = _part([0, 8, 16], [300], 2.0, tw=8.0, fw=2.0)
        m = merge_raster_partitions([a, b], "time", "frequency")
        assert list(m.coords["frequency"].values) == [100, 101, 300]
        assert list(rg.cell_widths(m, "frequency")) == [1.0, 1.0, 2.0]
        assert list(rg.cell_widths(m, "time")) == [8.0, 8.0, 8.0]

    def test_a_shared_coordinate_takes_the_larger_and_unknown_is_nan(self):
        from cubevis.toolbox.visplot.data._raster_merge import merge_raster_partitions
        a = _part([0, 8], [100], 1.0, tw=4.0)
        b = _part([8, 400], [100], 2.0, tw=8.0)
        c = _part([900], [100], 3.0)                  # says nothing
        m = merge_raster_partitions([a, b, c], "time", "frequency", rule="wmean")
        w = rg.cell_widths(m, "time")
        assert list(w[:3]) == [4.0, 8.0, 8.0] and np.isnan(w[3])
        assert rg.cell_widths(m, "frequency") is None

    def test_no_widths_no_coordinate(self):
        from cubevis.toolbox.visplot.data._raster_merge import merge_raster_partitions
        m = merge_raster_partitions([_part([0], [1], 1.0), _part([8], [1], 2.0)],
                                    "time", "frequency")
        assert set(m.coords) == {"time", "frequency"}


class TestReaderHelpers:

    def _ds(self, it=None, cw=None):
        t = xr.DataArray([0.0, 8.0], dims="time",
                         attrs={} if it is None else {"integration_time": it})
        f = xr.DataArray([1e9], dims="frequency",
                         attrs={} if cw is None else {"channel_width": cw})
        return xr.Dataset(coords={"time": t, "frequency": f})

    def test_partition_cell_width(self):
        from cubevis.toolbox.visplot.data.reader import partition_cell_width as pw
        ds = self._ds({"attrs": {"units": "s"}, "data": 8.0}, -2e6)
        assert pw(ds, "time") == 8.0 and pw(ds, "frequency") == 2e6
        assert pw(self._ds(), "time") is None
        assert pw(self._ds(0.0, "x"), "time") is None
        assert pw(self._ds(0.0, "x"), "frequency") is None
        assert pw(ds, "baseline_id") is None

    def test_with_cell_widths(self):
        from cubevis.toolbox.visplot.data.reader import with_cell_widths
        arr = xr.DataArray(np.zeros((2, 1)), dims=("time", "frequency"),
                           coords={"time": [0.0, 8.0], "frequency": [1e9]})
        out = with_cell_widths(arr, self._ds(8.0, 1e6))
        assert list(rg.cell_widths(out, "time")) == [8.0, 8.0]
        assert list(rg.cell_widths(out, "frequency")) == [1e6]
        assert set(with_cell_widths(arr, self._ds()).coords) == {"time", "frequency"}
        assert with_cell_widths(None, self._ds()) is None

    def test_a_channel_index_is_one_channel_wide(self):
        from cubevis.toolbox.visplot.data.reader import to_channel_index
        ref = np.array([1e9, 1.1e9, 1.2e9, 1.3e9])
        a = xr.DataArray(np.zeros((1, 2)), dims=("time", "frequency"),
                         coords={"time": [0.0], "frequency": ref[[0, 3]],
                                 "frequency_cell_width": ("frequency", [1e8, 1e8])})
        out = to_channel_index(a, "x", ref)
        assert list(out.coords["frequency"].values) == [0, 3]
        assert list(rg.cell_widths(out, "frequency")) == [1.0, 1.0]

    def test_decimation_scales_the_widths(self):
        from cubevis.toolbox.visplot.data.reader import scale_cell_widths
        a = _part([0, 8, 16, 24], [1e9, 2e9], 0.0, tw=8.0, fw=1e6)
        out = scale_cell_widths(a.isel(time=slice(None, None, 2)), {"time": 2, "frequency": 1})
        assert list(rg.cell_widths(out, "time")) == [16.0, 16.0]
        assert list(rg.cell_widths(out, "frequency")) == [1e6, 1e6]

    def test_a_decimated_channel_index_is_stride_channels_wide(self):
        from cubevis.toolbox.visplot.data.reader import to_channel_index
        ref = 1e9 + 1e6 * np.arange(16)
        a = xr.DataArray(np.zeros((1, 4)), dims=("time", "frequency"),
                         coords={"time": [0.0], "frequency": ref[::4],
                                 "frequency_cell_width": ("frequency", [4e6] * 4)})
        out = to_channel_index(a, "x", ref)
        assert list(rg.cell_widths(out, "frequency")) == [4.0] * 4

    def test_cell_bounds_takes_the_width(self):
        from cubevis.toolbox.visplot.data.reader import _cell_bounds
        t = np.array([0.0, 8.0, 16.0, 416.0])
        assert _cell_bounds(t, 3, width=8.0) == (412.0, 420.0)
        assert _cell_bounds(t, 3)[0] < 300                         # before #38


# ---------------------------------------------------------------------------
# Simulated data: integration 10 alone between two pauses
# ---------------------------------------------------------------------------

NTIME, NANT, NCHAN, DUMP = 16, 5, 8, 8.0
T_START = 5.0e9
# integrations 0-9, a 400 s pause, integration 10 alone, a 300 s pause, 11-15
GAPS = ((10, 400.0), (11, 300.0))
LONE = 10


def _transform(desc, data):
    dims, vis = data["DATA"]
    tdims, t = data["TIME"]
    t = np.asarray(t, dtype=np.float64).copy()
    idx = np.floor((t - T_START) / DUMP + 1e-6).astype(int)
    shift = np.zeros_like(t)
    for first, pause in GAPS:
        shift += np.where(idx >= first, pause, 0.0)
    data["TIME"] = (tdims, t + shift)
    if "TIME_CENTROID" in data:
        cd, c = data["TIME_CENTROID"]
        data["TIME_CENTROID"] = (cd, np.asarray(c, dtype=np.float64) + shift)
    amp = (10.0 * (1.0 + idx)).reshape((-1,) + (1,) * (vis.ndim - 1)) * np.ones(vis.shape)
    data["DATA"] = (dims, amp.astype(np.complex64))
    fdims, _ = data["FLAG"]
    data["FLAG"] = (fdims, np.zeros(vis.shape, dtype=bool))
    return data


@pytest.fixture(scope="module")
def sim_ms(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("cellw") / "lone.ms")
    sim.MSStructureSimulator(
        ntime=NTIME, time_chunks=NTIME, dump_rate=DUMP, time_start=T_START,
        nantenna=NANT, auto_corrs=False,
        data_description=[(NCHAN, ["XX", "YY"])],
        simulate_data=True, transform_data=_transform).simulate_ms(path)
    return path


@pytest.fixture(scope="module")
def sim_ps(sim_ms, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("cellwps") / "lone.ps.zarr")
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


def _times(agg):
    return np.asarray(agg.coords["time"].values, dtype=np.float64)


# ---------------------------------------------------------------------------
# 3. Real backends
# ---------------------------------------------------------------------------

class TestRealBackends:

    def test_the_lone_integration_is_there(self, backend):
        agg, *_ = backend.query_raster(Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE,
                                       SelectionSpec(), polarization="XX")
        d = np.diff(_times(agg))
        assert len(d) == NTIME - 1
        assert np.isclose(d[LONE - 1], DUMP + 400) and np.isclose(d[LONE], DUMP + 300)

    @pytest.mark.parametrize("axes", [(Axis.TIME, Axis.BASELINE),
                                      (Axis.TIME, Axis.FREQUENCY),
                                      (Axis.FREQUENCY, Axis.TIME)])
    def test_the_aggregate_carries_the_integration_time(self, backend, axes):
        agg, *_ = backend.query_raster(*axes, Axis.AMPLITUDE, SelectionSpec(),
                                       polarization="XX")
        w = rg.cell_widths(agg, "time")
        assert w is not None and np.allclose(w, DUMP)

    def test_the_channel_axis_is_one_channel_wide(self, backend):
        agg, *_ = backend.query_raster(Axis.TIME, Axis.CHANNEL, Axis.AMPLITUDE,
                                       SelectionSpec(), polarization="XX")
        w = rg.cell_widths(agg, agg.dims[1])
        assert w is not None and np.allclose(w, 1.0)

    def test_the_widths_survive_the_remote_wire_format(self, backend):
        from bokeh.core.serialization import Serializer, Deserializer
        from cubevis.toolbox.visplot import _wire_types as wt
        agg, *_ = backend.query_raster(Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE,
                                       SelectionSpec(), polarization="XX")
        enc = wt._encode_dataarray(agg, Serializer())
        assert "time_cell_width" in enc["coords"]
        back = wt._decode_dataarray(enc, Deserializer())
        assert np.allclose(rg.cell_widths(back, "time"), DUMP)


# ---------------------------------------------------------------------------
# 4. Real raster
# ---------------------------------------------------------------------------

W, H = 200, 1000


def _raster(backend, **kw):
    from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster
    args = dict(backend=backend, selection=SelectionSpec(), polarization="XX",
                y_dim=Axis.TIME, x_dim=Axis.BASELINE, quantity=Axis.AMPLITUDE,
                width=W, height=H, headless=True, scaling="linear")
    args.update(kw)
    return VisibilityRaster(**args)


def _rows(vr):
    """Each image row's time, and whether anything is drawn in it."""
    d = vr._image_source.data
    img = np.asarray(d["image"][0])
    h = img.shape[0]
    y = d["y"][0] + (np.arange(h) + 0.5) * d["dh"][0] / h
    alpha = (img.view(np.uint8).reshape(img.shape + (4,))[..., 3]
             if img.dtype != np.uint8 else img)
    return y, (alpha > 0).any(axis=1)


class TestRealRaster:

    def test_lone_integration_is_drawn_its_own_width(self, backend):
        vr = _raster(backend)
        t = _times(vr.agg)[LONE]
        y, drawn = _rows(vr)
        inside = (y > t - DUMP / 2 + 1) & (y < t + DUMP / 2 - 1)
        assert inside.any() and drawn[inside].all()
        # rows a little beyond its own width are blank
        assert not drawn[(y > t + DUMP) & (y < t + 150)].any()

    def test_gap_beside_it_is_blank(self, backend):
        vr = _raster(backend)
        tt = _times(vr.agg)
        y, drawn = _rows(vr)
        before = (y > tt[LONE - 1] + DUMP) & (y < tt[LONE] - DUMP)
        after = (y > tt[LONE] + DUMP) & (y < tt[LONE + 1] - DUMP)
        assert before.any() and not drawn[before].any()
        assert after.any() and not drawn[after].any()

    def test_readout_in_the_gap_beside_it_is_no_data(self, backend):
        vr = _raster(backend)
        t = _times(vr.agg)[LONE]
        assert vr._data_to_pixel(0.0, t + 20.0) == (None, None)
        assert vr._data_to_pixel(0.0, t + 1.0)[1] == LONE

    def test_probe_names_its_own_span(self, backend):
        vr = _raster(backend)
        t = _times(vr.agg)[LONE]
        info = vr._probe_raster_pixel_local(0, LONE)
        assert info["y_range"] == pytest.approx((t - DUMP / 2, t + DUMP / 2), rel=0, abs=1e-3)

    def test_closed_gaps_keep_its_real_span(self, backend):
        vr = _raster(backend)
        t = _times(vr.agg)[LONE]
        vr.update_axes(time_gaps="closed")
        ax = vr._time_axis
        i = ax.times.index(t)
        assert (ax.lo[i], ax.hi[i]) == pytest.approx((t - DUMP / 2, t + DUMP / 2),
                                                     rel=0, abs=1e-3)
        assert ax.positions[i] - ax.positions[i - 1] == 2       # one blank each side
        assert ax.positions[i + 1] - ax.positions[i] == 2


# ---------------------------------------------------------------------------
# 5. Real plotter: a flag box selects what is drawn
# ---------------------------------------------------------------------------

def _run(c):
    return asyncio.run(c)


@pytest.fixture(params=["msv2", "msv4"])
def plotter(request, sim_ms, sim_ps):
    from cubevis.toolbox.visplot import VisibilityPlotter
    kw = dict(ms=sim_ms) if request.param == "msv2" else dict(ps=sim_ps)
    vp = VisibilityPlotter(layout="side", correlation="XX,YY",
                           raster_y="TIME", raster_x="BASELINE", **kw)
    yield vp
    vp.close()


def _flagged_times(vp):
    """The integration times with anything flagged in XX."""
    b = vp._reader._backend
    out = set()
    for part in b._iter_visibility_partitions(None):
        f = b._flag_mask(part).transpose(
            "time", "baseline_id", "frequency", "polarization").values
        ip = list(part.polarization.values).index("XX")
        for ti in np.nonzero(f[:, :, :, ip].any(axis=(1, 2)))[0]:
            out.add(float(part.time.values[ti]))
    return out


class TestRealPlotter:

    def test_box_in_the_gap_beside_it_flags_nothing(self, plotter):
        vp = plotter
        vp.flag_db.clear(record=False)
        r = vp._slots[0].raster
        t = _times(r.agg)[LONE]
        box = dict(x0=-0.5, x1=20.0, y0=t + 20.0, y1=t + 100.0, flag=True)
        resp = _run(vp._handle_box_select(box, "raster", r))
        assert resp["notify_text"].startswith("⚠ Nothing to flag"), resp["notify_text"]
        assert len(vp.flag_db) == 0 and _flagged_times(vp) == set()

    def test_box_touching_it_flags_it_alone(self, plotter):
        vp = plotter
        vp.flag_db.clear(record=False)
        r = vp._slots[0].raster
        t = _times(r.agg)[LONE]
        box = dict(x0=-0.5, x1=20.0, y0=t + 1.0, y1=t + 100.0, flag=True)
        _run(vp._handle_box_select(box, "raster", r))
        got = _flagged_times(vp)
        assert len(got) == 1 and abs(next(iter(got)) - t) < 1e-3
        vp.flag_db.clear(record=False)
