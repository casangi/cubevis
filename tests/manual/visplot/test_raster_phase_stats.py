"""
test_raster_phase_stats.py
============================
Tests for HRS milestone H2, slice 1 (2026-10): the Phase RMS and
Coherence raster quantities (``Axis.PHASE_RMS``, ``Axis.COHERENCE``) and
their slope-removal option (``detrend``).

Location in repository:
    cubevis/tests/manual/visplot/test_raster_phase_stats.py

Run:
    pytest cubevis/tests/manual/visplot/test_raster_phase_stats.py -v

What is pinned here:

* definitions, against known answers: phase noise of sigma degrees reads
  sigma; coherence reads exp(-sigma^2/2); stable phase reads 0 and 1;
  pure noise reads ~104 deg and ~1/sqrt(N);
* the +/-180 deg wrap does not matter;
* slope removal: a residual delay (frequency) or rate (time) is taken
  out when detrend is on and dominates when it is off, including steep
  slopes, time gaps between scans, descending frequency and datetime64
  time coordinates; removing a slope that is not there changes nothing;
* flagged samples and NaN padding are excluded; too few samples is NaN;
* the result does not depend on how the data are chunked, and is lazy;
* MSv2Backend and MSv4Backend agree;
* the option is per raster panel, travels on SelectionSpec, and shows in
  the title.

All synthetic -- no real MS/PS needed for any test in this file.
"""
from __future__ import annotations

import inspect

import dask.array as da
import numpy as np
import pytest
import xarray as xr

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data._raster_stats import (
    STAT_QUANTITIES, _lag_ladder, _step_index, reduce_phase_stat,
)
from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
from cubevis.toolbox.visplot.selection import SelectionSpec

DIMS = ("time", "baseline_id", "frequency", "polarization")
NOISE_RMS_DEG = 180.0 / np.sqrt(3.0)      # uniform phase: 103.92 deg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _msv2():
    b = MSv2Backend.__new__(MSv2Backend)
    b._datatree = object()
    b._path = "synthetic"
    return b


def _msv4():
    b = MSv4Backend.__new__(MSv4Backend)
    b._datatree = object()
    b._resolved_mode = "interferometer"
    return b


BACKENDS = {"msv2": _msv2, "msv4": _msv4}


def _dataset(vis, flag=None, time=None, freq=None, chunks="auto"):
    vis = np.asarray(vis, dtype=np.complex128)[..., None]
    if flag is None:
        flag = np.zeros(vis.shape, dtype=bool)
    else:
        flag = np.asarray(flag, dtype=bool)[..., None]
    nt, nb, nf, _ = vis.shape
    if chunks is not None:
        if chunks == "auto":
            chunks = (max(1, nt // 3), nb, max(1, nf // 3), 1)
        vis = da.from_array(vis, chunks=chunks)
        flag = da.from_array(flag, chunks=chunks)
    return xr.Dataset(
        data_vars={"VISIBILITY": (DIMS, vis), "FLAG": (DIMS, flag)},
        coords={"time": np.arange(nt) * 2.0 + 5.0e9 if time is None else time,
                "baseline_id": np.arange(nb),
                "frequency": (np.linspace(1.0e9, 1.1e9, nf)
                              if freq is None else freq),
                "polarization": ["XX"]},
    )


def _raster(backend_name, ds, y, x, qty, **kw):
    arr = BACKENDS[backend_name]()._raster_2d(ds, y, x, qty, "XX", **kw)
    assert arr is not None
    return np.asarray(arr.compute().values)


def _phasor(deg):
    return np.exp(1j * np.deg2rad(deg))


def _noisy(shape, sigma_deg, seed=0):
    rng = np.random.default_rng(seed)
    return rng.normal(0.0, sigma_deg, shape)


@pytest.fixture(params=sorted(BACKENDS))
def backend_name(request):
    return request.param


# Baseline x Time: each cell is reduced over frequency.
TB = (Axis.TIME, Axis.BASELINE)
# Frequency x Baseline: each cell is reduced over time.
FB = (Axis.FREQUENCY, Axis.BASELINE)


# ---------------------------------------------------------------------------
# 1. Definitions against known answers
# ---------------------------------------------------------------------------

class TestKnownAnswers:

    @pytest.mark.parametrize("sigma", [5.0, 20.0, 45.0])
    @pytest.mark.parametrize("detrend", [True, False])
    def test_phase_rms_reads_the_noise(self, backend_name, sigma, detrend):
        ph = _noisy((30, 3, 256), sigma, seed=1)
        out = _raster(backend_name, _dataset(_phasor(ph)), *TB,
                      Axis.PHASE_RMS, detrend=detrend)
        assert out.shape == (30, 3)
        assert out.mean() == pytest.approx(sigma, rel=0.03)

    @pytest.mark.parametrize("sigma", [5.0, 20.0, 45.0])
    def test_coherence_is_exp_minus_half_sigma_squared(self, backend_name, sigma):
        ph = _noisy((30, 3, 256), sigma, seed=2)
        out = _raster(backend_name, _dataset(_phasor(ph)), *TB, Axis.COHERENCE)
        want = np.exp(-np.deg2rad(sigma) ** 2 / 2.0)
        assert out.mean() == pytest.approx(want, rel=0.02)

    def test_stable_phase(self, backend_name):
        vis = np.full((4, 2, 32), 3.0 * _phasor(-75.0))
        ds = _dataset(vis)
        assert np.allclose(_raster(backend_name, ds, *TB, Axis.PHASE_RMS), 0.0, atol=1e-6)
        assert np.allclose(_raster(backend_name, ds, *TB, Axis.COHERENCE), 1.0, atol=1e-9)

    def test_pure_noise(self, backend_name):
        rng = np.random.default_rng(3)
        shape = (20, 2, 1024)
        vis = rng.normal(size=shape) + 1j * rng.normal(size=shape)
        ds = _dataset(vis)
        rms = _raster(backend_name, ds, *TB, Axis.PHASE_RMS, detrend=False)
        coh = _raster(backend_name, ds, *TB, Axis.COHERENCE, detrend=False)
        assert rms.mean() == pytest.approx(NOISE_RMS_DEG, rel=0.03)
        # The vector mean of N noise samples shrinks as 1/sqrt(N) while
        # the scalar mean does not; the ratio is 1/sqrt(N) to within a
        # constant close to 1.
        assert coh.mean() == pytest.approx(1.0 / np.sqrt(1024), rel=0.25)
        assert np.all(coh < 0.15)

    def test_phase_rms_ignores_amplitude(self, backend_name):
        # Same phases, wildly different amplitudes: unit phasors.
        ph = _noisy((6, 2, 128), 15.0, seed=4)
        amp = np.random.default_rng(5).uniform(0.01, 100.0, ph.shape)
        a = _raster(backend_name, _dataset(_phasor(ph)), *TB, Axis.PHASE_RMS, detrend=False)
        b = _raster(backend_name, _dataset(amp * _phasor(ph)), *TB, Axis.PHASE_RMS, detrend=False)
        assert np.allclose(a, b, rtol=1e-9)

    def test_coherence_is_amplitude_weighted(self, backend_name):
        # One strong sample dominates the vector sum: coherence stays high
        # even though the weak samples point elsewhere.
        vis = np.zeros((1, 1, 5), dtype=complex)
        vis[0, 0] = [100.0, 1j, -1.0, -1j, 1.0]
        out = _raster(backend_name, _dataset(vis), *TB, Axis.COHERENCE, detrend=False)
        assert out[0, 0] == pytest.approx(100.0 / 104.0, rel=1e-9)

    def test_ranges(self, backend_name):
        rng = np.random.default_rng(6)
        shape = (10, 3, 40)
        vis = rng.normal(0.3, 1, shape) + 1j * rng.normal(0, 1, shape)
        ds = _dataset(vis)
        rms = _raster(backend_name, ds, *TB, Axis.PHASE_RMS)
        coh = _raster(backend_name, ds, *TB, Axis.COHERENCE)
        assert np.all(rms >= 0) and np.all(rms <= 180.0)
        assert np.all(coh >= 0) and np.all(coh <= 1.0 + 1e-12)


# ---------------------------------------------------------------------------
# 2. The wrap
# ---------------------------------------------------------------------------

class TestWrap:

    @pytest.mark.parametrize("centre", [0.0, 90.0, 180.0, -179.0])
    def test_rms_independent_of_mean_phase(self, backend_name, centre):
        ph = _noisy((8, 2, 256), 20.0, seed=7)
        out = _raster(backend_name, _dataset(_phasor(ph + centre)), *TB,
                      Axis.PHASE_RMS, detrend=False)
        ref = _raster(backend_name, _dataset(_phasor(ph)), *TB,
                      Axis.PHASE_RMS, detrend=False)
        assert np.allclose(out, ref, atol=1e-6)

    def test_two_samples_across_the_wrap(self, backend_name):
        # +179 and -179: 2 deg apart.  Sample RMS of {+1, -1} with one
        # fitted parameter is sqrt(2).
        vis = np.broadcast_to(_phasor(np.array([179.0, -179.0])), (2, 2, 2)).copy()
        out = _raster(backend_name, _dataset(vis), *TB, Axis.PHASE_RMS, detrend=False)
        assert np.allclose(out, np.sqrt(2.0), atol=1e-6)


# ---------------------------------------------------------------------------
# 3. Slope removal
# ---------------------------------------------------------------------------

class TestDetrend:

    @pytest.mark.parametrize("turns", [0.5, 2.0, 10.0, 40.0])
    def test_delay_removed(self, backend_name, turns):
        nf = 256
        slope = 360.0 * turns * np.arange(nf) / nf
        ph = _noisy((12, 3, nf), 10.0, seed=8) + slope
        ds = _dataset(_phasor(ph))
        on = _raster(backend_name, ds, *TB, Axis.PHASE_RMS, detrend=True)
        assert on.mean() == pytest.approx(10.0, rel=0.04)

    def test_delay_dominates_when_kept(self, backend_name):
        nf = 256
        slope = 360.0 * 3.0 * np.arange(nf) / nf            # 3 whole turns
        ds = _dataset(_phasor(np.broadcast_to(slope, (4, 2, nf))))
        rms = _raster(backend_name, ds, *TB, Axis.PHASE_RMS, detrend=False)
        coh = _raster(backend_name, ds, *TB, Axis.COHERENCE, detrend=False)
        assert np.allclose(rms, NOISE_RMS_DEG, rtol=0.02)
        assert np.all(coh < 1e-9)

    def test_clean_delay_removed_exactly(self, backend_name):
        nf = 64
        slope = 360.0 * 2.3 * np.arange(nf) / nf
        ds = _dataset(_phasor(np.broadcast_to(slope, (4, 2, nf))))
        assert np.allclose(_raster(backend_name, ds, *TB, Axis.PHASE_RMS), 0.0, atol=1e-5)
        assert np.allclose(_raster(backend_name, ds, *TB, Axis.COHERENCE), 1.0, atol=1e-9)

    def test_each_cell_gets_its_own_slope(self, backend_name):
        # Different delay on every (time, baseline) cell.
        nf = 128
        rng = np.random.default_rng(9)
        turns = rng.uniform(-8, 8, (6, 3, 1))
        ph = 360.0 * turns * np.arange(nf) / nf + _noisy((6, 3, nf), 8.0, seed=10)
        out = _raster(backend_name, _dataset(_phasor(ph)), *TB, Axis.PHASE_RMS)
        assert np.allclose(out, 8.0, rtol=0.25)
        assert out.mean() == pytest.approx(8.0, rel=0.05)

    def test_no_slope_present_changes_little(self, backend_name):
        ph = _noisy((12, 3, 256), 20.0, seed=11)
        ds = _dataset(_phasor(ph))
        on = _raster(backend_name, ds, *TB, Axis.PHASE_RMS, detrend=True)
        off = _raster(backend_name, ds, *TB, Axis.PHASE_RMS, detrend=False)
        assert np.allclose(on, off, rtol=0.03)

    def test_rate_removed_along_time(self, backend_name):
        # Frequency x Baseline: cells are reduced over time, so the slope
        # removed is the rate.
        nt = 120
        rate = 360.0 * 4.0 * np.arange(nt) / nt
        ph = _noisy((nt, 2, 8), 12.0, seed=12) + rate[:, None, None]
        ds = _dataset(_phasor(ph))
        on = _raster(backend_name, ds, *FB, Axis.PHASE_RMS, detrend=True)
        off = _raster(backend_name, ds, *FB, Axis.PHASE_RMS, detrend=False)
        assert on.shape == (8, 2)
        assert on.mean() == pytest.approx(12.0, rel=0.06)
        assert off.mean() > 90.0

    def test_rate_removed_across_a_scan_gap(self, backend_name):
        # Two scans 500 s apart; the phase keeps winding through the gap.
        t = np.concatenate([np.arange(30) * 2.0, 560.0 + np.arange(30) * 2.0]) + 5.0e9
        rate_deg_per_s = 3.0
        ph = (_noisy((60, 2, 8), 10.0, seed=13)
              + (rate_deg_per_s * (t - t[0]))[:, None, None])
        ds = _dataset(_phasor(ph), time=t)
        on = _raster(backend_name, ds, *FB, Axis.PHASE_RMS, detrend=True)
        assert on.mean() == pytest.approx(10.0, rel=0.08)

    def test_descending_frequency(self, backend_name):
        nf = 128
        freq = np.linspace(1.1e9, 1.0e9, nf)                 # lower sideband
        slope = 360.0 * 5.0 * np.arange(nf) / nf
        ph = _noisy((6, 2, nf), 10.0, seed=14) + slope
        out = _raster(backend_name, _dataset(_phasor(ph), freq=freq), *TB, Axis.PHASE_RMS)
        assert out.mean() == pytest.approx(10.0, rel=0.05)

    def test_datetime64_time_coordinate(self, backend_name):
        nt = 90
        t = (np.datetime64("2026-10-05T00:00:00", "ns")
             + (np.arange(nt) * 2_000_000_000).astype("timedelta64[ns]"))
        rate = 360.0 * 3.0 * np.arange(nt) / nt
        ph = _noisy((nt, 2, 4), 10.0, seed=15) + rate[:, None, None]
        out = _raster(backend_name, _dataset(_phasor(ph), time=t), *FB, Axis.PHASE_RMS)
        assert out.mean() == pytest.approx(10.0, rel=0.08)

    def test_short_window_is_not_biased_low(self, backend_name):
        # 8 channels with a slope removed: dividing by n would read 13%
        # low; the degrees-of-freedom correction should not.
        ph = _noisy((400, 3, 8), 20.0, seed=16)
        out = _raster(backend_name, _dataset(_phasor(ph)), *TB, Axis.PHASE_RMS)
        assert np.sqrt((out ** 2).mean()) == pytest.approx(20.0, rel=0.04)


# ---------------------------------------------------------------------------
# 4. Flags, padding, too few samples
# ---------------------------------------------------------------------------

class TestFlagsAndCounts:

    def test_flagged_samples_excluded(self, backend_name):
        ph = _noisy((6, 2, 128), 10.0, seed=17)
        vis = _phasor(ph)
        flag = np.zeros(vis.shape, dtype=bool)
        vis[:, :, 40:50] = 50.0 * _phasor(123.0)            # garbage block
        flag[:, :, 40:50] = True
        clean = np.delete(_phasor(ph), np.s_[40:50], axis=2)
        for qty in STAT_QUANTITIES:
            got = _raster(backend_name, _dataset(vis, flag), *TB, qty, detrend=False)
            want = _raster(backend_name, _dataset(clean), *TB, qty, detrend=False)
            assert np.allclose(got, want, rtol=1e-9)

    def test_flagged_gap_does_not_break_slope_removal(self, backend_name):
        nf = 256
        slope = 360.0 * 6.0 * np.arange(nf) / nf
        vis = _phasor(_noisy((6, 2, nf), 10.0, seed=18) + slope)
        flag = np.zeros(vis.shape, dtype=bool)
        flag[:, :, 100:140] = True
        out = _raster(backend_name, _dataset(vis, flag), *TB, Axis.PHASE_RMS)
        assert out.mean() == pytest.approx(10.0, rel=0.06)

    def test_fully_flagged_cell_is_nan(self, backend_name):
        vis = _phasor(_noisy((4, 2, 32), 10.0, seed=19))
        flag = np.zeros(vis.shape, dtype=bool)
        flag[1, 0] = True
        for qty in STAT_QUANTITIES:
            out = _raster(backend_name, _dataset(vis, flag), *TB, qty)
            assert np.isnan(out[1, 0])
            assert np.isfinite(np.delete(out.ravel(), 2)).all()

    def test_nan_padding_ignored(self, backend_name):
        vis = _phasor(_noisy((4, 2, 64), 10.0, seed=20))
        ref = _raster(backend_name, _dataset(np.delete(vis, 7, axis=2)), *TB,
                      Axis.PHASE_RMS, detrend=False)
        vis[:, :, 7] = np.nan + 1j * np.nan
        out = _raster(backend_name, _dataset(vis), *TB, Axis.PHASE_RMS, detrend=False)
        assert np.allclose(out, ref, rtol=1e-9)

    def test_one_sample_per_cell_is_nan(self, backend_name):
        # Single-baseline Time x Frequency: nothing is reduced, and the
        # scatter of one sample is undefined.
        vis = _phasor(_noisy((6, 1, 12), 10.0, seed=21))
        for qty in STAT_QUANTITIES:
            out = _raster(backend_name, _dataset(vis), Axis.TIME, Axis.FREQUENCY, qty)
            assert out.shape == (6, 12)
            assert np.isnan(out).all()

    def test_too_few_samples_for_the_fit_is_nan(self, backend_name):
        # 3 channels, slope removed: mean + slope = 2 parameters, 1
        # degree of freedom -> defined.  Flag one more: none -> NaN.
        vis = _phasor(_noisy((2, 1, 3), 10.0, seed=22))
        flag = np.zeros(vis.shape, dtype=bool)
        assert np.isfinite(_raster(backend_name, _dataset(vis), *TB, Axis.PHASE_RMS)).all()
        flag[0, 0, 0] = True
        out = _raster(backend_name, _dataset(vis, flag), *TB, Axis.PHASE_RMS)
        assert np.isnan(out[0, 0]) and np.isfinite(out[1, 0])

    def test_zero_amplitude_sample_dropped_from_rms(self, backend_name):
        vis = np.full((1, 1, 6), _phasor(40.0))
        vis[0, 0, 2] = 0.0
        out = _raster(backend_name, _dataset(vis), *TB, Axis.PHASE_RMS, detrend=False)
        assert np.allclose(out, 0.0, atol=1e-6)


# ---------------------------------------------------------------------------
# 5. Chunking, laziness, parity
# ---------------------------------------------------------------------------

class TestChunkingAndParity:

    @pytest.mark.parametrize("qty", STAT_QUANTITIES)
    @pytest.mark.parametrize("axes", [TB, FB])
    def test_independent_of_chunking(self, backend_name, qty, axes):
        nt, nb, nf = 24, 3, 48
        ph = (_noisy((nt, nb, nf), 15.0, seed=23)
              + 360.0 * 2.0 * np.arange(nf) / nf
              + (360.0 * 1.5 * np.arange(nt) / nt)[:, None, None])
        vis = _phasor(ph)
        ref = _raster(backend_name, _dataset(vis, chunks=None), *axes, qty)
        for chunks in [(nt, nb, nf, 1), (5, 1, 7, 1), (1, nb, 1, 1)]:
            out = _raster(backend_name, _dataset(vis, chunks=chunks), *axes, qty)
            assert np.allclose(out, ref, rtol=1e-9, atol=1e-9, equal_nan=True)

    @pytest.mark.parametrize("qty", STAT_QUANTITIES)
    def test_result_is_lazy(self, backend_name, qty):
        ds = _dataset(_phasor(_noisy((6, 2, 16), 10.0)))
        arr = BACKENDS[backend_name]()._raster_2d(ds, *TB, qty, "XX")
        assert isinstance(arr.data, da.Array)
        assert arr.dims == ("time", "baseline_id")
        assert set(arr.coords) == {"time", "baseline_id"}

    @pytest.mark.parametrize("qty", STAT_QUANTITIES)
    @pytest.mark.parametrize("axes", [TB, FB, (Axis.BASELINE, Axis.TIME)])
    @pytest.mark.parametrize("detrend", [True, False])
    def test_msv2_equals_msv4(self, qty, axes, detrend):
        rng = np.random.default_rng(24)
        shape = (10, 4, 20)
        vis = rng.normal(1.0, 0.5, shape) + 1j * rng.normal(0, 0.5, shape)
        flag = rng.random(shape) < 0.2
        ds = _dataset(vis, flag)
        a = _raster("msv2", ds, *axes, qty, detrend=detrend)
        b = _raster("msv4", ds, *axes, qty, detrend=detrend)
        assert a.shape == b.shape
        assert np.allclose(a, b, equal_nan=True, rtol=0, atol=1e-12)


# ---------------------------------------------------------------------------
# 6. Helper contracts
# ---------------------------------------------------------------------------

class TestHelpers:

    def _one(self):
        ds = _dataset(np.ones((2, 2, 4), dtype=complex))
        return (ds["VISIBILITY"].sel(polarization="XX"),
                ds["FLAG"].sel(polarization="XX"))

    def test_rejects_other_quantities(self):
        vis, flag = self._one()
        with pytest.raises(ValueError):
            reduce_phase_stat(vis, flag, Axis.AMPLITUDE, ["frequency"])

    def test_rejects_empty_reduce_dims(self):
        vis, flag = self._one()
        with pytest.raises(ValueError):
            reduce_phase_stat(vis, flag, Axis.PHASE_RMS, [])

    @pytest.mark.parametrize("n, want", [
        (1, [1]), (3, [1]), (6, [1, 2]), (16, [1, 4, 5]),
        (64, [1, 4, 16, 21]), (256, [1, 4, 16, 64, 85]),
    ])
    def test_lag_ladder(self, n, want):
        assert _lag_ladder(n) == want

    def test_step_index_uniform(self):
        c = xr.DataArray(np.linspace(1e9, 1.1e9, 11), dims=("frequency",))
        assert np.allclose(_step_index(c), np.arange(11))

    def test_step_index_with_gap(self):
        c = xr.DataArray(np.array([0.0, 2, 4, 6, 106, 108, 110]), dims=("time",))
        assert np.allclose(_step_index(c), [0, 1, 2, 3, 53, 54, 55])

    def test_step_index_refuses_short_or_nonnumeric(self):
        assert _step_index(xr.DataArray(np.arange(2.0), dims=("time",))) is None
        assert _step_index(xr.DataArray(np.array(["a", "b", "c"]), dims=("x",))) is None

    def test_backends_accept_detrend(self, backend_name):
        sig = inspect.signature(BACKENDS[backend_name]()._raster_2d)
        assert sig.parameters["detrend"].default is True


# ---------------------------------------------------------------------------
# 7. query_raster reads detrend from the selection
# ---------------------------------------------------------------------------

class TestQueryRasterUsesSelection:

    @pytest.mark.parametrize("name", sorted(BACKENDS))
    def test_detrend_from_selection(self, name):
        nf = 64
        slope = 360.0 * 3.0 * np.arange(nf) / nf
        ds = _dataset(_phasor(np.broadcast_to(slope, (3, 2, nf))))
        b = BACKENDS[name]()
        b._iter_visibility_partitions = lambda selection: iter([ds])
        b._apply_selection = lambda raw_ds, selection: raw_ds
        try:
            on, *_ = b.query_raster(Axis.TIME, Axis.BASELINE, Axis.COHERENCE,
                                    SelectionSpec(), polarization="XX")
            off, *_ = b.query_raster(Axis.TIME, Axis.BASELINE, Axis.COHERENCE,
                                     SelectionSpec(detrend=False), polarization="XX")
        except AttributeError as exc:           # backend needs more state
            pytest.skip(f"{name} query_raster not drivable bare: {exc}")
        assert np.allclose(on.values, 1.0, atol=1e-9)
        assert np.all(off.values < 1e-9)


# ---------------------------------------------------------------------------
# 8. Plumbing: SelectionSpec, the raster panel, the title, the plotter
# ---------------------------------------------------------------------------

class _RecordingReader:
    def __init__(self):
        self.calls = []

    def query_raster(self, y_dim, x_dim, quantity, selection,
                     polarization=None, max_cells=2_000_000, **kw):
        self.calls.append((quantity, selection.detrend))
        agg = xr.DataArray(
            np.ones((4, 3)), dims=("time", "baseline_id"),
            coords={"time": np.arange(4.0), "baseline_id": np.arange(3)})
        return agg, (0.0, 2.0), (0.0, 3.0), False

    def identity_tables(self, *a, **kw):
        return {}


class TestPlumbing:

    def _vr(self, reader, selection=None, **kw):
        pytest.importorskip("datashader")
        from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster
        kw.setdefault("quantity", Axis.PHASE_RMS)
        return VisibilityRaster(reader, selection or SelectionSpec(),
                                Axis.TIME, Axis.BASELINE, **kw)

    def test_selection_default_copy_and_constraint(self):
        assert SelectionSpec().detrend is True
        assert SelectionSpec(detrend=False).copy().detrend is False
        assert SelectionSpec(detrend=False).is_empty()

    def test_panel_default_and_backend_sees_it(self):
        r = _RecordingReader()
        vr = self._vr(r)
        assert vr.detrend is True and r.calls[-1] == (Axis.PHASE_RMS, True)

    def test_panel_value_reaches_backend_not_shared_selection(self):
        r, sel = _RecordingReader(), SelectionSpec()
        self._vr(r, sel, detrend=False)
        assert r.calls[-1][1] is False
        assert sel.detrend is True

    def test_update_axes_changes_and_requeries_once(self):
        r = _RecordingReader()
        vr = self._vr(r)
        n = len(r.calls)
        vr.update_axes(detrend=False)
        assert vr.detrend is False and len(r.calls) == n + 1
        vr.update_axes(detrend=False)
        assert len(r.calls) == n + 1

    def test_two_panels_differ(self):
        r, sel = _RecordingReader(), SelectionSpec()
        a = self._vr(r, sel, detrend=True)
        b = self._vr(r, sel, detrend=False)
        a.update_axes(quantity=Axis.COHERENCE)
        b.update_axes(quantity=Axis.COHERENCE)
        assert r.calls[-2:] == [(Axis.COHERENCE, True), (Axis.COHERENCE, False)]

    @pytest.mark.parametrize("qty", STAT_QUANTITIES)
    def test_title_says_whether_slope_was_removed(self, qty):
        r = _RecordingReader()
        on = self._vr(r, quantity=qty, detrend=True)._effective_title()
        off = self._vr(r, quantity=qty, detrend=False)._effective_title()
        assert f"{qty.label} (slope removed)" in on
        assert f"{qty.label} (slope kept)" in off

    @pytest.mark.parametrize("qty", [Axis.AMPLITUDE, Axis.PHASE, Axis.REAL,
                                     Axis.FLAG, Axis.Z_SCORE])
    def test_other_titles_do_not_mention_slope(self, qty):
        t = self._vr(_RecordingReader(), quantity=qty)._effective_title()
        assert "slope" not in t

    def test_plotter_offers_the_quantities_and_the_argument(self):
        from cubevis.toolbox.visplot import visibility_plotter as vp
        names = [n for n, _ in vp._RASTER_QTY_OPTIONS]
        assert "PHASE_RMS" in names and "COHERENCE" in names
        # Raster only: a scatter of per-sample values has no window.
        assert "PHASE_RMS" not in [n for n, _ in vp._SCATTER_Y_OPTIONS]
        sig = inspect.signature(vp.VisibilityPlotter.__init__)
        assert sig.parameters["detrend"].default is True

    def test_axis_members(self):
        assert Axis.PHASE_RMS.label == "Phase RMS"
        assert Axis.COHERENCE.label == "Coherence"
        assert set(STAT_QUANTITIES) == {Axis.PHASE_RMS, Axis.COHERENCE}
