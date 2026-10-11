"""
test_antenna_matrix.py
======================
HRS H7 (2026-10-11): the antenna x antenna raster -- AIPS LISTR's 'MATX':
one cell per baseline, the quantity reduced over time and frequency,
mirrored so a bad antenna is a whole row and column; the diagonal holds
the autocorrelations.

1. Backends   a simulated MS with autocorrelations and two spectral
              windows, both backends, against numpy (Real exact; vector
              Amplitude per SPW combined by weight; the diagonal);
              antennas without data left out; Antenna x Time refused
2. TW Hya     3c279: 19 antennas with data, DA41 / DA42 left out
3. Plotter    preset, title, tick labels, readout, flag box refused, tick
              density, the two-line-title repaint fix (JS), upright x
              labels (2026-10-11, after Darrell's test: the names ran
              into each other)
"""
from __future__ import annotations

import asyncio
import os

import numpy as np
import pytest
import xarray as xr

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.selection import SelectionSpec

NTIME, NANT, NFLAG = 4, 4, 2
NCHANS = (8, 12)
MS = os.environ.get("MS", "")


def _transform(desc, data):
    dims, vis = data["DATA"]
    a1 = np.asarray(data["ANTENNA1"][1]).astype(float)
    a2 = np.asarray(data["ANTENNA2"][1]).astype(float)
    nch = vis.shape[1]
    spw = 0 if nch == NCHANS[0] else 1
    ch = np.arange(nch)[None, :, None]
    amp = (10 + 3 * a1 + a2)[:, None, None] + 0.5 * ch + 2 * spw
    ph = (0.3 * a1 - 0.2 * a2)[:, None, None] + 0.1 * ch + 0.4 * spw
    v = np.broadcast_to(amp * np.exp(1j * ph), vis.shape).astype(np.complex64)
    data["DATA"] = (dims, v)
    fd, _ = data["FLAG"]
    f = np.zeros(vis.shape, bool)
    f[:, :NFLAG, :] = True
    data["FLAG"] = (fd, f)
    return data


@pytest.fixture(scope="module")
def sim_ms(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("amat") / "a.ms")
    sim.MSStructureSimulator(
        ntime=NTIME, time_chunks=NTIME, dump_rate=8.0, time_start=5.0e9,
        nantenna=NANT, auto_corrs=True,
        data_description=[(n, ["XX", "YY"]) for n in NCHANS],
        simulate_data=True, transform_data=_transform).simulate_ms(path)
    return path


@pytest.fixture(scope="module")
def sim_ps(sim_ms, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("amatps") / "a.ps.zarr")
    xr.open_datatree(sim_ms, engine="xarray-ms:msv2", auto_corrs=True).to_zarr(
        out, mode="w", compute=True)
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


def _samples(a1, a2):
    out = []
    for spw, nch in enumerate(NCHANS):
        ch = np.arange(NFLAG, nch)
        amp = (10 + 3 * a1 + a2) + 0.5 * ch + 2 * spw
        ph = (0.3 * a1 - 0.2 * a2) + 0.1 * ch + 0.4 * spw
        out.append(amp * np.exp(1j * ph))
    return out


def _matrix(b, q, y=Axis.ANTENNA1, x=Axis.ANTENNA2):
    sel = SelectionSpec(correlation=["XX"], averaging="vector")
    agg, xr_, yr_, _ = b.query_raster(y, x, q, sel, polarization="XX")
    return agg, xr_, yr_


class TestBackends:

    def test_shape_labels_symmetry(self, backend):
        agg, xr_, yr_ = _matrix(backend, Axis.REAL)
        assert agg.dims == ("antenna1", "antenna2") and agg.shape == (NANT, NANT)
        assert agg.attrs["antenna_labels"] == [f"ANTENNA-{i}" for i in range(NANT)]
        assert np.allclose(agg.values, agg.values.T, equal_nan=True)
        assert xr_ == (0.0, NANT - 1.0) and yr_ == (0.0, NANT - 1.0)

    def test_real_is_the_mean_over_time_and_both_spws(self, backend):
        agg, _, _ = _matrix(backend, Axis.REAL)
        for i in range(NANT):
            for j in range(i, NANT):
                want = np.concatenate(_samples(i, j)).real.mean()
                assert agg.values[i, j] == pytest.approx(want, rel=1e-5)
                assert agg.values[j, i] == pytest.approx(want, rel=1e-5)

    def test_vector_amplitude_per_spw_then_weighted(self, backend):
        agg, _, _ = _matrix(backend, Axis.AMPLITUDE)
        w = np.array([n - NFLAG for n in NCHANS], float)
        for i in range(NANT):
            for j in range(i, NANT):
                want = (w * np.abs([s.mean() for s in _samples(i, j)])).sum() / w.sum()
                assert agg.values[i, j] == pytest.approx(want, rel=1e-5)

    def test_diagonal_is_the_autocorrelations(self, backend):
        agg, _, _ = _matrix(backend, Axis.AMPLITUDE)
        assert np.isfinite(np.diag(agg.values)).all()

    def test_axes_either_way_round(self, backend):
        a, _, _ = _matrix(backend, Axis.REAL)
        b, _, _ = _matrix(backend, Axis.REAL, y=Axis.ANTENNA2, x=Axis.ANTENNA1)
        assert b.dims == ("antenna2", "antenna1")
        assert np.allclose(a.values, b.values, equal_nan=True)

    def test_flag_fraction(self, backend):
        agg, _, _ = _matrix(backend, Axis.FLAG)
        assert agg.values == pytest.approx(2 * NFLAG / sum(NCHANS))

    def test_antenna_with_time_is_refused(self, backend):
        with pytest.raises(NotImplementedError, match="Antenna 1 goes with Antenna 2"):
            backend.query_raster(Axis.ANTENNA1, Axis.TIME, Axis.AMPLITUDE,
                                 SelectionSpec(correlation=["XX"]), polarization="XX")


def test_compact_leaves_out_antennas_without_data():
    from cubevis.toolbox.visplot.data._antenna_matrix import compact
    m = np.full((4, 4), np.nan)
    m[0, 2] = m[2, 0] = 1.0
    m[2, 3] = m[3, 2] = 2.0
    agg = xr.DataArray(m, dims=("antenna1", "antenna2"),
                       coords={"antenna1": np.arange(4.0), "antenna2": np.arange(4.0)})
    out = compact(agg, ["A", "B", "C", "D"])
    assert out.attrs["antenna_labels"] == ["A", "C", "D"]
    assert out.values.tolist()[0][1] == 1.0 and out.coords["antenna1"].values.tolist() == [0, 1, 2]


# ---------------------------------------------------------------------------
# 2-3. TW Hya and the plotter
# ---------------------------------------------------------------------------

needs_ms = pytest.mark.skipif(not os.path.isdir(MS), reason="TW Hya MS not available (MS=)")


@pytest.fixture(scope="module")
def plotter():
    if not os.path.isdir(MS):
        pytest.skip("TW Hya MS not available (MS=)")
    from cubevis.toolbox.visplot import VisibilityPlotter
    vp = VisibilityPlotter(ms=MS, field="3c279", correlation="XX", preset="antmatrix")
    vp._build_layout()
    yield vp
    vp.close()


@needs_ms
class TestPlotter:

    def test_matrix(self, plotter):
        r = plotter._slots[0].raster
        agg = r._agg
        assert agg.dims == ("antenna1", "antenna2") and agg.shape == (19, 19)
        labels = agg.attrs["antenna_labels"]
        assert "DA41" not in labels and "DA42" not in labels and labels[0] == "DA44"

    def test_title_and_ticks(self, plotter):
        r = plotter._slots[0].raster
        title = r._effective_title()
        assert "Antenna × Antenna, each baseline over time and channels" in title
        assert "mean of baselines" not in title
        spec = r._panel_spec()
        assert spec.x_ticks == spec.y_ticks == tuple(r._agg.attrs["antenna_labels"])
        for ax in (r.figure.xaxis[0], r.figure.yaxis[0]):
            assert ax.ticker.desired_num_ticks == 19 and ax.ticker.min_interval == 1

    def test_readout_names_the_baseline(self, plotter):
        r = plotter._slots[0].raster
        labels = r._agg.attrs["antenna_labels"]
        text = r._format_probe(r._probe_raster_pixel_local(1, 0), "Amplitude")
        assert f"BL:</b> {labels[0]}&{labels[1]}" in text
        assert "Antenna 1:" not in text

    def test_flag_box_refused_with_a_reason(self, plotter):
        from cubevis.toolbox.visplot import flag_engine as fe
        req = {"flag": True, "kind": "raster", "selection": plotter._slots[0].raster._selection,
               "x_axis": "ANTENNA2", "x0": 0.0, "x1": 2.0,
               "y_axis": "ANTENNA1", "y0": 0.0, "y1": 2.0, "polarization": "XX"}
        with pytest.raises(ValueError, match="antenna × antenna matrix is not available"):
            fe.evaluate_request(plotter._reader._backend, req)

    def test_preset_button(self, plotter):
        from cubevis.toolbox.visplot.visibility_plotter import _PRESETS, _PRESET_BUTTONS
        assert _PRESETS["antmatrix"][:3] == (Axis.ANTENNA1, Axis.ANTENNA2, Axis.AMPLITUDE)
        assert "antmatrix" in [b[0] for b in _PRESET_BUTTONS]
        assert plotter._hint_preset_antmatrix.text.startswith("<b>Antenna × antenna</b>")

    def test_two_line_title_repaint_fix_and_tick_density_js(self, plotter):
        js = plotter._do_plot_js
        assert "_f.min_border_top = b + 1;" in js
        assert "const dn = (n > 0 && n <= 30) ? n : 6;" in js


def test_x_label_orientation_rule():
    from cubevis.toolbox.visplot.visibility_plot import x_label_orientation
    assert x_label_orientation(["DA44", "DA45"]) == "vertical"
    assert x_label_orientation(["DA44&DA45"] * 30) == "vertical"
    assert x_label_orientation(["DA44"] * 31) == "horizontal"     # sparse labels
    assert x_label_orientation(["A", "B", "C"]) == "horizontal"
    assert x_label_orientation(None) == x_label_orientation([]) == "horizontal"


@needs_ms
def test_matrix_x_labels_upright(plotter):
    r = plotter._slots[0].raster
    assert [a.major_label_orientation for a in r.figure.xaxis] == ["vertical"]
    assert [a.major_label_orientation for a in r.figure.yaxis] == ["horizontal"]
    js = plotter._do_plot_js
    assert "const o = up ? 'vertical' : 'horizontal';" in js
