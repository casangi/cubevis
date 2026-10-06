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
  the title;
* windows (slice 2): along a displayed axis every sample shows its
  window's value and the grid is unchanged; along a reduced axis the
  windows are pooled, each about its own mean phase; windows never span
  a gap between scans; "auto" means off where time is displayed and
  per-scan where it is reduced; the single-baseline waterfall works once
  a window is given;
* scatter (slice 3): the statistic as a per-sample quantity -- every
  sample carries its window's value, per baseline, with the window
  chosen by the x axis; through the real backends and a real plotter on
  simulated data.

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
    STAT_QUANTITIES, _lag_ladder, _runs, _split, _step_index, chan_blocks,
    describe_windows, normalize_chan_window, normalize_time_window,
    ScatterStatSpec, paint_phase_stat, reduce_phase_stat,
    resolve_time_window, scatter_stat_spec, time_blocks,
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
        # Scatter too, since slice 3 (the x axis decides the window).
        ynames = [n for n, _ in vp._SCATTER_Y_OPTIONS]
        assert "PHASE_RMS" in ynames and "COHERENCE" in ynames
        sig = inspect.signature(vp.VisibilityPlotter.__init__)
        assert sig.parameters["detrend"].default is True

    def test_axis_members(self):
        assert Axis.PHASE_RMS.label == "Phase RMS"
        assert Axis.COHERENCE.label == "Coherence"
        assert set(STAT_QUANTITIES) == {Axis.PHASE_RMS, Axis.COHERENCE}


# ---------------------------------------------------------------------------
# 9. Windows (slice 2)
# ---------------------------------------------------------------------------

def _three_scans(nb=1, nf=64, seed=30):
    """Three 40-integration scans, 2 s sampling, gaps between them; phase
    noise 10 / 30 / 10 deg and a different phase offset in each scan."""
    t = np.concatenate([np.arange(40) * 2.0,
                        300.0 + np.arange(40) * 2.0,
                        700.0 + np.arange(40) * 2.0]) + 5.0e9
    sig = np.repeat([10.0, 30.0, 10.0], 40)
    off = np.repeat([0.0, 120.0, -90.0], 40)
    rng = np.random.default_rng(seed)
    ph = rng.normal(0, 1, (120, nb, nf)) * sig[:, None, None] + off[:, None, None]
    return _phasor(ph), t


WF = (Axis.TIME, Axis.FREQUENCY)          # single-baseline waterfall


class TestWindowHelpers:

    @pytest.mark.parametrize("given, want", [
        (None, "auto"), ("", "auto"), ("AUTO", "auto"), ("off", "off"),
        ("Scan", "scan"), ("60", 60.0), (30, 30.0), (2.5, 2.5),
    ])
    def test_normalize_time(self, given, want):
        assert normalize_time_window(given) == want

    @pytest.mark.parametrize("bad", ["soon", 0, -5, "0", float("nan")])
    def test_normalize_time_rejects(self, bad):
        with pytest.raises(ValueError):
            normalize_time_window(bad)

    @pytest.mark.parametrize("given, want", [
        (None, "off"), ("", "off"), ("off", "off"), (0, "off"), (1, "off"),
        ("16", 16), (8, 8), (8.0, 8),
    ])
    def test_normalize_chan(self, given, want):
        assert normalize_chan_window(given) == want

    @pytest.mark.parametrize("bad", ["wide", -2])
    def test_normalize_chan_rejects(self, bad):
        with pytest.raises(ValueError):
            normalize_chan_window(bad)

    def test_auto_resolution(self):
        assert resolve_time_window("auto", time_displayed=True) == "off"
        assert resolve_time_window("auto", time_displayed=False) == "scan"
        assert resolve_time_window("off", time_displayed=False) == "off"
        assert resolve_time_window(60, time_displayed=True) == 60.0

    def test_runs_split_at_gaps(self):
        k = np.array([0, 1, 2, 3, 53, 54, 55, 200.0])
        assert _runs(k) == [(0, 4), (4, 7), (7, 8)]

    def test_split_merges_a_short_tail(self):
        # A tail shorter than half a window joins the window before it...
        assert _split(0, 9, 4) == [(0, 4), (4, 9)]
        # ...one of half a window or more stands alone.
        assert _split(0, 10, 4) == [(0, 4), (4, 8), (8, 10)]
        assert _split(0, 11, 4) == [(0, 4), (4, 8), (8, 11)]
        assert _split(0, 3, 8) == [(0, 3)]

    def test_time_blocks(self):
        _, t = _three_scans()
        c = xr.DataArray(t, dims=("time",))
        assert time_blocks(c, "off") == [(0, 120)]
        assert time_blocks(c, "scan") == [(0, 40), (40, 80), (80, 120)]
        # 20 s at 2 s sampling: 10 integrations, never across a gap.
        b = time_blocks(c, 20.0)
        assert len(b) == 12 and all(e - a == 10 for a, e in b)
        assert (40, 50) in b and not any(a < 40 < e for a, e in b)

    def test_chan_blocks(self):
        assert chan_blocks(64, "off") == [(0, 64)]
        assert chan_blocks(64, 16) == [(0, 16), (16, 32), (32, 48), (48, 64)]

    @pytest.mark.parametrize("tw, cw, td, cd, want", [
        ("auto", "off", True, False, ""),
        ("auto", "off", False, True, "per scan"),
        ("off", "off", False, True, ""),
        (60, 16, True, True, "60 s x 16 ch"),
        ("scan", 8, True, False, "per scan x 8 ch"),
    ])
    def test_describe(self, tw, cw, td, cd, want):
        assert describe_windows(tw, cw, td, cd) == want


class TestWindowsOnDisplayedAxes:

    def test_waterfall_blank_without_a_window(self, backend_name):
        vis, t = _three_scans()
        out = _raster(backend_name, _dataset(vis, time=t), *WF, Axis.PHASE_RMS)
        assert out.shape == (120, 64) and np.isnan(out).all()

    def test_waterfall_per_scan(self, backend_name):
        vis, t = _three_scans()
        out = _raster(backend_name, _dataset(vis, time=t), *WF, Axis.PHASE_RMS,
                      time_window="scan")
        # Grid unchanged; each channel shows its scan's scatter over time.
        assert out.shape == (120, 64)
        assert out[:40].mean() == pytest.approx(10.0, rel=0.06)
        assert out[40:80].mean() == pytest.approx(30.0, rel=0.06)
        assert out[80:].mean() == pytest.approx(10.0, rel=0.06)
        # Constant along time within a scan, different between channels.
        assert np.allclose(out[:40], out[0])
        assert not np.allclose(out[0], out[0, 0])

    def test_waterfall_time_and_channel_blocks(self, backend_name):
        vis, t = _three_scans()
        out = _raster(backend_name, _dataset(vis, time=t), *WF, Axis.PHASE_RMS,
                      time_window=20, chan_window=16)
        assert out.shape == (120, 64)
        # One value per 10-integration x 16-channel block.
        blk = out[:10, :16]
        assert np.allclose(blk, blk[0, 0])
        assert not np.isclose(out[0, 0], out[10, 0])
        assert not np.isclose(out[0, 0], out[0, 16])
        assert out[:40].mean() == pytest.approx(10.0, rel=0.08)
        assert out[40:80].mean() == pytest.approx(30.0, rel=0.08)

    def test_window_does_not_span_the_gap(self, backend_name):
        # A 1000 s window is longer than any scan: it must still stop at
        # the gaps, so the 30-deg scan cannot contaminate its neighbours.
        vis, t = _three_scans()
        out = _raster(backend_name, _dataset(vis, time=t), *WF, Axis.PHASE_RMS,
                      time_window=1000)
        assert out[:40].mean() == pytest.approx(10.0, rel=0.06)
        assert out[80:].mean() == pytest.approx(10.0, rel=0.06)

    def test_baseline_time_default_is_per_integration(self, backend_name):
        vis, t = _three_scans(nb=3)
        ds = _dataset(vis, time=t)
        auto = _raster(backend_name, ds, *TB, Axis.PHASE_RMS)
        off = _raster(backend_name, ds, *TB, Axis.PHASE_RMS, time_window="off")
        assert np.allclose(auto, off, equal_nan=True)
        assert len(np.unique(auto[:40, 0].round(9))) == 40    # not blocked

    def test_baseline_time_per_scan(self, backend_name):
        vis, t = _three_scans(nb=3)
        out = _raster(backend_name, _dataset(vis, time=t), *TB, Axis.PHASE_RMS,
                      time_window="scan")
        assert out.shape == (120, 3)
        assert np.allclose(out[:40], out[0])
        assert out[:40].mean() == pytest.approx(10.0, rel=0.04)
        assert out[40:80].mean() == pytest.approx(30.0, rel=0.04)

    def test_coherence_windows(self, backend_name):
        vis, t = _three_scans(nb=2)
        out = _raster(backend_name, _dataset(vis, time=t), *TB, Axis.COHERENCE,
                      time_window="scan", chan_window=16)
        assert out[:40].mean() == pytest.approx(np.exp(-np.deg2rad(10) ** 2 / 2), rel=0.01)
        assert out[40:80].mean() == pytest.approx(np.exp(-np.deg2rad(30) ** 2 / 2), rel=0.02)

    def test_slope_removed_within_each_window(self, backend_name):
        # A rate that differs from scan to scan: only per-window slope
        # removal can take it out.
        vis, t = _three_scans(seed=31)
        rate = np.repeat([2.0, -5.0, 9.0], 40) * np.tile(np.arange(40) * 2.0, 3)
        vis = vis * _phasor(rate)[:, None, None]
        ds = _dataset(vis, time=t)
        on = _raster(backend_name, ds, *WF, Axis.PHASE_RMS, time_window="scan")
        off = _raster(backend_name, ds, *WF, Axis.PHASE_RMS, time_window="scan",
                      detrend=False)
        assert on[:40].mean() == pytest.approx(10.0, rel=0.08)
        assert on[80:].mean() == pytest.approx(10.0, rel=0.08)
        assert off[80:].mean() > 60.0

    def test_flagged_samples_in_a_window(self, backend_name):
        vis, t = _three_scans()
        flag = np.zeros(vis.shape, dtype=bool)
        vis[5:15] = 99.0 * _phasor(77.0)
        flag[5:15] = True
        out = _raster(backend_name, _dataset(vis, flag, time=t), *WF,
                      Axis.PHASE_RMS, time_window="scan")
        assert out[:40].mean() == pytest.approx(10.0, rel=0.08)

    def test_fully_flagged_window_is_nan_others_fine(self, backend_name):
        vis, t = _three_scans()
        flag = np.zeros(vis.shape, dtype=bool)
        flag[40:80] = True
        out = _raster(backend_name, _dataset(vis, flag, time=t), *WF,
                      Axis.PHASE_RMS, time_window="scan")
        assert np.isnan(out[40:80]).all()
        assert np.isfinite(out[:40]).all() and np.isfinite(out[80:]).all()


class TestWindowsPooledOnReducedAxes:

    def test_default_pools_per_scan(self, backend_name):
        # Frequency x Baseline, time reduced.  Per-scan pooling ignores
        # the 120 / -90 deg offsets between scans: pooled RMS is
        # sqrt((10^2 + 30^2 + 10^2) / 3) = 19.1.
        vis, t = _three_scans(nb=3)
        out = _raster(backend_name, _dataset(vis, time=t), *FB, Axis.PHASE_RMS)
        assert out.shape == (64, 3)
        assert out.mean() == pytest.approx(np.sqrt(1100.0 / 3.0), rel=0.04)

    def test_off_takes_the_whole_range(self, backend_name):
        # ...whereas the whole range as one window sees the offsets.
        vis, t = _three_scans(nb=3)
        out = _raster(backend_name, _dataset(vis, time=t), *FB, Axis.PHASE_RMS,
                      time_window="off")
        assert out.mean() > 50.0

    def test_scan_equals_auto_when_time_is_reduced(self, backend_name):
        vis, t = _three_scans(nb=2)
        ds = _dataset(vis, time=t)
        a = _raster(backend_name, ds, *FB, Axis.PHASE_RMS)
        b = _raster(backend_name, ds, *FB, Axis.PHASE_RMS, time_window="scan")
        assert np.allclose(a, b)

    def test_pooled_channel_windows(self, backend_name):
        # Baseline x Time, frequency reduced in 16-channel windows.  A
        # steep delay is removed window by window; with a phase step
        # between the two halves of the band, only windowing avoids
        # counting the step as scatter.
        nf = 64
        step = np.where(np.arange(nf) < 32, 0.0, 150.0)
        ph = _noisy((20, 2, nf), 10.0, seed=32) + step
        ds = _dataset(_phasor(ph))
        whole = _raster(backend_name, ds, *TB, Axis.PHASE_RMS, detrend=False)
        win = _raster(backend_name, ds, *TB, Axis.PHASE_RMS, detrend=False,
                      chan_window=16)
        assert whole.mean() > 60.0
        assert win.mean() == pytest.approx(10.0, rel=0.06)

    def test_single_scan_unaffected_by_scan_pooling(self, backend_name):
        ph = _noisy((60, 2, 16), 15.0, seed=33)
        ds = _dataset(_phasor(ph))
        a = _raster(backend_name, ds, *FB, Axis.PHASE_RMS, time_window="scan")
        b = _raster(backend_name, ds, *FB, Axis.PHASE_RMS, time_window="off")
        assert np.allclose(a, b)


class TestWindowsChunkingParityPlumbing:

    @pytest.mark.parametrize("axes, kw", [
        (WF, dict(time_window=20, chan_window=16)),
        (TB, dict(time_window="scan")),
        (FB, dict()),
        (FB, dict(time_window=30, chan_window=8)),
    ])
    def test_independent_of_chunking_and_backend(self, axes, kw):
        vis, t = _three_scans(nb=1 if axes == WF else 3)
        ref = _raster("msv2", _dataset(vis, time=t, chunks=None), *axes,
                      Axis.PHASE_RMS, **kw)
        for name in sorted(BACKENDS):
            for chunks in [(120, vis.shape[1], 64, 1), (7, 1, 5, 1)]:
                out = _raster(name, _dataset(vis, time=t, chunks=chunks), *axes,
                              Axis.PHASE_RMS, **kw)
                assert np.allclose(out, ref, rtol=1e-9, atol=1e-9, equal_nan=True)

    def test_windowed_result_is_lazy_and_keeps_coords(self, backend_name):
        vis, t = _three_scans()
        arr = BACKENDS[backend_name]()._raster_2d(
            _dataset(vis, time=t), *WF, Axis.PHASE_RMS, "XX",
            time_window="scan", chan_window=16)
        assert isinstance(arr.data, da.Array)
        assert arr.dims == ("time", "frequency")
        assert np.array_equal(arr.coords["time"].values, t)

    def test_large_batch_is_rechunked_not_refused(self, monkeypatch):
        # Force a tiny block budget: the result must not change.
        from cubevis.toolbox.visplot.data import _raster_stats as rs
        vis, t = _three_scans(nb=6)
        ds = _dataset(vis, time=t, chunks=(120, 6, 64, 1))
        ref = _raster("msv2", ds, *FB, Axis.PHASE_RMS)
        monkeypatch.setattr(rs, "_MAX_BLOCK_SAMPLES", 300)
        out = _raster("msv2", ds, *FB, Axis.PHASE_RMS)
        assert np.allclose(out, ref, rtol=1e-9)

    def test_selection_fields(self):
        s = SelectionSpec()
        assert s.stat_time_window == "auto" and s.stat_chan_window == "off"
        c = SelectionSpec(stat_time_window=60.0, stat_chan_window=16).copy()
        assert c.stat_time_window == 60.0 and c.stat_chan_window == 16
        assert SelectionSpec(stat_time_window="scan").is_empty()

    @pytest.mark.parametrize("name", sorted(BACKENDS))
    def test_query_raster_reads_windows_from_selection(self, name):
        vis, t = _three_scans()
        ds = _dataset(vis, time=t)
        b = BACKENDS[name]()
        b._iter_visibility_partitions = lambda selection: iter([ds])
        b._apply_selection = lambda raw_ds, selection: raw_ds
        blank, *_ = b.query_raster(Axis.TIME, Axis.FREQUENCY, Axis.PHASE_RMS,
                                   SelectionSpec(baselines=[0]), polarization="XX")
        win, *_ = b.query_raster(
            Axis.TIME, Axis.FREQUENCY, Axis.PHASE_RMS,
            SelectionSpec(baselines=[0], stat_time_window="scan"),
            polarization="XX")
        assert np.isnan(blank.values).all()
        assert np.isfinite(win.values).all()


class _WinReader:
    def __init__(self):
        self.calls = []

    def query_raster(self, y_dim, x_dim, quantity, selection,
                     polarization=None, max_cells=2_000_000, **kw):
        self.calls.append((selection.stat_time_window, selection.stat_chan_window))
        agg = xr.DataArray(
            np.ones((4, 3)), dims=("time", "baseline_id"),
            coords={"time": np.arange(4.0), "baseline_id": np.arange(3)})
        return agg, (0.0, 2.0), (0.0, 3.0), False

    def identity_tables(self, *a, **kw):
        return {}


class TestWindowsOnThePanel:

    def _vr(self, reader, y=Axis.TIME, x=Axis.BASELINE, **kw):
        pytest.importorskip("datashader")
        from cubevis.toolbox.visplot.visibility_raster import VisibilityRaster
        kw.setdefault("quantity", Axis.PHASE_RMS)
        return VisibilityRaster(reader, SelectionSpec(), y, x, **kw)

    def test_defaults_reach_backend(self):
        r = _WinReader()
        vr = self._vr(r)
        assert (vr.stat_time_window, vr.stat_chan_window) == ("auto", "off")
        assert r.calls[-1] == ("auto", "off")

    def test_update_axes(self):
        r = _WinReader()
        vr = self._vr(r)
        n = len(r.calls)
        vr.update_axes(stat_time_window="60", stat_chan_window=16)
        assert r.calls[-1] == (60.0, 16) and len(r.calls) == n + 1
        vr.update_axes(stat_time_window=60, stat_chan_window="16")
        assert len(r.calls) == n + 1                       # unchanged
        vr.update_axes(quantity=Axis.COHERENCE)            # None keeps them
        assert r.calls[-1] == (60.0, 16)

    def test_rejects_bad_values(self):
        with pytest.raises(ValueError):
            self._vr(_WinReader(), stat_time_window="soon")
        vr = self._vr(_WinReader())
        with pytest.raises(ValueError):
            vr.update_axes(stat_chan_window=-4)

    def test_title_names_the_window(self):
        r = _WinReader()
        assert "(slope removed)" in self._vr(r)._effective_title()
        t = self._vr(r, stat_time_window=60, stat_chan_window=16)._effective_title()
        assert "Phase RMS (slope removed, 60 s x 16 ch)" in t
        # Time not displayed: auto means per scan, and the title says so.
        t = self._vr(r, y=Axis.BASELINE, x=Axis.CHANNEL)._effective_title()
        assert "Phase RMS (slope removed, per scan)" in t

    def test_plotter_arguments_and_options(self):
        from cubevis.toolbox.visplot import visibility_plotter as vp
        sig = inspect.signature(vp.VisibilityPlotter.__init__)
        assert sig.parameters["stat_time_window"].default == "auto"
        assert sig.parameters["stat_chan_window"].default == "off"
        # Every offered value is one the normalizers accept.
        for v, _ in vp._STAT_TIME_WINDOW_OPTIONS:
            normalize_time_window(v)
        for v, _ in vp._STAT_CHAN_WINDOW_OPTIONS:
            normalize_chan_window(v)
        # A constructor value outside the list is added so the Select
        # can show it.
        opts = vp._window_options(vp._STAT_TIME_WINDOW_OPTIONS, 45.0, "s")
        assert ("45", "45 s") in opts
        assert vp._window_value(45.0) == "45" and vp._window_value("scan") == "scan"


# ---------------------------------------------------------------------------
# 10. Several baselines on the waterfall: pooled one by one, never lumped
# ---------------------------------------------------------------------------

class TestBaselinesArePooled:
    """The GUI's antenna filter selects every baseline of the named
    antennas, so a Time x Channel raster normally has several baselines
    reduced into each cell.  Each baseline has its own phase; lumping
    them read ~80-90 deg however stable each one was (found 2026-10-05,
    before this reached a real data set)."""

    def _ds(self, nb=5, sigma=10.0, seed=40):
        rng = np.random.default_rng(seed)
        vis, t = _three_scans(nb=nb, seed=seed)
        # _three_scans gives 10/30/10 deg; use only the first scan's
        # level by rebuilding with a fixed sigma, plus a different phase
        # on every baseline.
        ph = (rng.normal(0, sigma, (120, nb, 64))
              + rng.uniform(-180, 180, (1, nb, 1)))
        return _dataset(_phasor(ph), time=t)

    def test_blank_without_a_window(self, backend_name):
        out = _raster(backend_name, self._ds(), *WF, Axis.PHASE_RMS)
        assert out.shape == (120, 64) and np.isnan(out).all()

    def test_per_scan_reads_each_baselines_own_scatter(self, backend_name):
        out = _raster(backend_name, self._ds(), *WF, Axis.PHASE_RMS,
                      time_window="scan")
        assert out.shape == (120, 64)
        assert np.nanmean(out) == pytest.approx(10.0, rel=0.05)

    def test_coherence_likewise(self, backend_name):
        out = _raster(backend_name, self._ds(), *WF, Axis.COHERENCE,
                      time_window=20, chan_window=8)
        assert np.nanmean(out) == pytest.approx(
            np.exp(-np.deg2rad(10.0) ** 2 / 2), rel=0.01)

    def test_one_noisy_baseline_raises_the_pooled_value(self, backend_name):
        # 4 baselines at 10 deg and one at 40: pooled RMS is
        # sqrt((4*100 + 1600) / 5) = 20.
        rng = np.random.default_rng(41)
        _, t = _three_scans()
        sig = np.array([10.0, 10.0, 10.0, 10.0, 40.0])[None, :, None]
        ph = rng.normal(0, 1, (120, 5, 64)) * sig + rng.uniform(-180, 180, (1, 5, 1))
        out = _raster(backend_name, _dataset(_phasor(ph), time=t), *WF,
                      Axis.PHASE_RMS, time_window="scan")
        assert np.nanmean(out) == pytest.approx(20.0, rel=0.06)

    def test_fully_flagged_baseline_does_not_count(self, backend_name):
        rng = np.random.default_rng(42)
        _, t = _three_scans()
        ph = rng.normal(0, 10.0, (120, 3, 64)) + rng.uniform(-180, 180, (1, 3, 1))
        vis = _phasor(ph)
        flag = np.zeros(vis.shape, dtype=bool)
        vis[:, 1] = 500.0
        flag[:, 1] = True
        out = _raster(backend_name, _dataset(vis, flag, time=t), *WF,
                      Axis.PHASE_RMS, time_window="scan")
        assert np.nanmean(out) == pytest.approx(10.0, rel=0.06)

    def test_single_baseline_unchanged(self, backend_name):
        vis, t = _three_scans(nb=1)
        out = _raster(backend_name, _dataset(vis, time=t), *WF, Axis.PHASE_RMS,
                      time_window="scan")
        assert out[:40].mean() == pytest.approx(10.0, rel=0.06)
        assert out[40:80].mean() == pytest.approx(30.0, rel=0.06)


# ---------------------------------------------------------------------------
# 11. Scatter (slice 3): the statistic painted onto every sample
# ---------------------------------------------------------------------------

class TestScatterStatSpec:

    @pytest.mark.parametrize("x, want", [
        (Axis.TIME,          ("sample", "all")),
        (Axis.FREQUENCY,     ("scan", "sample")),
        (Axis.CHANNEL,       ("scan", "sample")),
        (Axis.UVDIST,        ("scan", "all")),
        (Axis.UVDIST_LAMBDA, ("scan", "all")),
        (Axis.U,             ("scan", "all")),
    ])
    def test_defaults_follow_the_x_axis(self, x, want):
        s = scatter_stat_spec(x)
        assert (s.time, s.chan) == want and s.detrend is True

    def test_off_means_no_sub_windows(self):
        # Single samples along the x axis's own dimension, the whole
        # extent along the other.
        assert scatter_stat_spec(Axis.TIME, "off", "off").time == "sample"
        assert scatter_stat_spec(Axis.CHANNEL, "off", "off").time == "all"
        assert scatter_stat_spec(Axis.CHANNEL, "off", "off").chan == "sample"
        assert scatter_stat_spec(Axis.UVDIST, "off", "off") == \
            ScatterStatSpec("all", "all", True)

    def test_explicit_windows_are_used_as_given(self):
        s = scatter_stat_spec(Axis.TIME, 60, 16, detrend=False)
        assert s == ScatterStatSpec(60.0, 16, False)
        assert scatter_stat_spec(Axis.CHANNEL, "scan", 8).chan == 8

    def test_spec_is_hashable(self):
        assert len({scatter_stat_spec(Axis.TIME), scatter_stat_spec(Axis.TIME)}) == 1


def _vis_flag(vis, flag=None, time=None):
    ds = _dataset(vis, flag, time=time)
    return (ds["VISIBILITY"].sel(polarization="XX"),
            ds["FLAG"].sel(polarization="XX"))


class TestPaintPhaseStat:

    def test_vs_time_one_value_per_baseline_per_integration(self):
        rng = np.random.default_rng(50)
        ph = _noisy((12, 3, 128), 10.0, seed=50) + rng.uniform(-180, 180, (12, 3, 1))
        v, f = _vis_flag(_phasor(ph))
        out = paint_phase_stat(v, f, Axis.PHASE_RMS, scatter_stat_spec(Axis.TIME))
        assert out.dims == v.dims and out.shape == v.shape
        vals = out.compute().values
        assert np.allclose(vals, vals[:, :, :1])            # same across the band
        assert len(np.unique(vals[:, :, 0].round(9))) == 36  # differs per (t, bl)
        assert vals.mean() == pytest.approx(10.0, rel=0.04)

    def test_matches_the_raster_reduction(self):
        # "Phase rms vs time" in a scatter is the Baseline x Time raster,
        # one point per cell.
        ph = _noisy((10, 4, 64), 15.0, seed=51)
        v, f = _vis_flag(_phasor(ph))
        painted = paint_phase_stat(v, f, Axis.PHASE_RMS,
                                   scatter_stat_spec(Axis.TIME)).compute().values
        raster = reduce_phase_stat(v, f, Axis.PHASE_RMS, ["frequency"]).compute().values
        assert np.allclose(painted[:, :, 0], raster, rtol=1e-9)

    def test_vs_channel_one_value_per_baseline_per_channel_per_scan(self):
        vis, t = _three_scans(nb=2)
        v, f = _vis_flag(vis, time=t)
        out = paint_phase_stat(v, f, Axis.PHASE_RMS,
                               scatter_stat_spec(Axis.CHANNEL)).compute().values
        assert out.shape == (120, 2, 64)
        assert np.allclose(out[:40], out[0])               # constant within a scan
        assert out[:40].mean() == pytest.approx(10.0, rel=0.06)
        assert out[40:80].mean() == pytest.approx(30.0, rel=0.06)

    def test_vs_uvdist_one_value_per_baseline_per_scan(self):
        vis, t = _three_scans(nb=3)
        v, f = _vis_flag(vis, time=t)
        out = paint_phase_stat(v, f, Axis.COHERENCE,
                               scatter_stat_spec(Axis.UVDIST)).compute().values
        assert len(np.unique(out[:40].round(9))) == 3       # 3 baselines, scan 1
        assert out[:40].mean() == pytest.approx(np.exp(-np.deg2rad(10) ** 2 / 2), rel=0.01)

    def test_baselines_are_never_mixed(self):
        # One noisy baseline must not raise its neighbours' values.
        rng = np.random.default_rng(52)
        sig = np.array([5.0, 40.0, 5.0])[None, :, None]
        ph = rng.normal(0, 1, (20, 3, 128)) * sig
        v, f = _vis_flag(_phasor(ph))
        out = paint_phase_stat(v, f, Axis.PHASE_RMS,
                               scatter_stat_spec(Axis.TIME)).compute().values
        assert out[:, 0].mean() == pytest.approx(5.0, rel=0.06)
        assert out[:, 1].mean() == pytest.approx(40.0, rel=0.06)

    def test_time_windows_in_seconds(self):
        vis, t = _three_scans(nb=1)
        v, f = _vis_flag(vis, time=t)
        out = paint_phase_stat(v, f, Axis.PHASE_RMS,
                               scatter_stat_spec(Axis.TIME, 20, "off")).compute().values
        assert np.allclose(out[:10], out[0]) and not np.isclose(out[0, 0, 0], out[10, 0, 0])

    def test_flagged_samples_do_not_contribute(self):
        ph = _noisy((8, 2, 64), 10.0, seed=53)
        vis = _phasor(ph)
        flag = np.zeros(vis.shape, dtype=bool)
        vis[:, :, :8] = 77.0
        flag[:, :, :8] = True
        v, f = _vis_flag(vis, flag)
        out = paint_phase_stat(v, f, Axis.PHASE_RMS,
                               scatter_stat_spec(Axis.TIME)).compute().values
        assert out.mean() == pytest.approx(10.0, rel=0.08)

    def test_lazy_and_chunk_independent(self):
        ph = _noisy((12, 3, 32), 10.0, seed=54)
        spec = scatter_stat_spec(Axis.CHANNEL)
        ref = None
        for chunks in [None, (12, 3, 32, 1), (5, 1, 7, 1)]:
            ds = _dataset(_phasor(ph), chunks=chunks)
            out = paint_phase_stat(ds["VISIBILITY"].sel(polarization="XX"),
                                   ds["FLAG"].sel(polarization="XX"),
                                   Axis.PHASE_RMS, spec)
            if chunks is not None:
                assert isinstance(out.data, da.Array)
            vals = np.asarray(out.compute().values)
            ref = vals if ref is None else ref
            assert np.allclose(vals, ref, rtol=1e-9)

    def test_rejects_nothing_to_measure_and_other_quantities(self):
        v, f = _vis_flag(_phasor(_noisy((4, 1, 4), 5.0)))
        with pytest.raises(ValueError):
            paint_phase_stat(v, f, Axis.PHASE_RMS, ScatterStatSpec("sample", "sample"))
        with pytest.raises(ValueError):
            paint_phase_stat(v, f, Axis.AMPLITUDE, scatter_stat_spec(Axis.TIME))


class TestLazyQuantityScatter:

    def test_backends_agree_and_mask_flags(self, backend_name):
        ph = _noisy((8, 2, 32), 10.0, seed=55)
        vis = _phasor(ph)
        flag = np.zeros(vis.shape, dtype=bool)
        flag[2, 1, 5] = True
        ds = _dataset(vis, flag)
        b = BACKENDS[backend_name]()
        q = b._lazy_quantity(ds["VISIBILITY"], ds["FLAG"], Axis.PHASE_RMS, "XX",
                             stat=scatter_stat_spec(Axis.TIME))
        vals = q.compute().values
        assert vals.shape == (8, 2, 32)
        assert np.isnan(vals[2, 1, 5]) and np.isfinite(np.delete(vals.ravel(), 2 * 64 + 32 + 5)).all()
        other = BACKENDS["msv4" if backend_name == "msv2" else "msv2"]()
        q2 = other._lazy_quantity(ds["VISIBILITY"], ds["FLAG"], Axis.PHASE_RMS, "XX",
                                  stat=scatter_stat_spec(Axis.TIME))
        assert np.allclose(vals, q2.compute().values, equal_nan=True)

    def test_needs_a_spec(self, backend_name):
        ds = _dataset(_phasor(_noisy((4, 1, 8), 5.0)))
        with pytest.raises(ValueError, match="stat="):
            BACKENDS[backend_name]()._lazy_quantity(
                ds["VISIBILITY"], ds["FLAG"], Axis.COHERENCE, "XX")


# --- real backends and a real plotter, on simulated data -------------------

def _sim_transform(desc, data):
    ddid = int(desc.DATA_DESC_ID.item())
    rng = np.random.default_rng(2000 + ddid * 17 + int(desc.chunk_id))
    dims, vis = data["DATA"]
    # 10 deg of phase noise across the band; a random phase per row.
    ph = (np.deg2rad(rng.normal(0, 10.0, vis.shape))
          + rng.uniform(-3, 3, (vis.shape[0],) + (1,) * (vis.ndim - 1)))
    amp = 1.0 + 0.1 * rng.standard_normal(vis.shape)
    data["DATA"] = (dims, (amp * np.exp(1j * ph)).astype(np.complex64))
    fdims, _ = data["FLAG"]
    data["FLAG"] = (fdims, np.zeros(vis.shape, dtype=bool))
    return data


@pytest.fixture(scope="module")
def sim_paths(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    root = tmp_path_factory.mktemp("statscatter")
    ms = str(root / "s.ms")
    sim.MSStructureSimulator(
        ntime=24, nantenna=5, auto_corrs=False,
        data_description=[(64, ["XX", "YY"])],
        simulate_data=True, transform_data=_sim_transform).simulate_ms(ms)
    ps = str(root / "s.ps.zarr")
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        xr.open_datatree(ms, engine="xarray-ms:msv2",
                         partition_schema=["FIELD_ID"]).to_zarr(ps, mode="w", compute=True)
    return {"msv2": dict(ms=ms), "msv4": dict(ps=ps)}


@pytest.mark.parametrize("fmt", ["msv2", "msv4"])
@pytest.mark.parametrize("x, y, check", [
    ("TIME", "PHASE_RMS", lambda lo, hi: 5.0 < lo < 10.0 < hi < 16.0),
    ("TIME", "COHERENCE", lambda lo, hi: 0.95 < lo <= hi <= 1.0),
    # The simulated phase jumps at random between integrations, so a
    # per-scan window reads large scatter and low coherence: correct.
    ("CHANNEL", "PHASE_RMS", lambda lo, hi: lo > 50.0 and hi < 125.0),
    ("UVDIST", "COHERENCE", lambda lo, hi: 0.0 <= lo and hi < 0.7),
])
def test_scatter_through_a_real_plotter(sim_paths, fmt, x, y, check):
    import warnings
    warnings.filterwarnings("ignore")
    from cubevis.toolbox.visplot import VisibilityPlotter
    vp = VisibilityPlotter(layout="side", correlation="XX", scatter_x=x,
                           scatter_y=y, **sim_paths[fmt])
    try:
        sc = next(s.scatter for s in vp._slots if s.kind == "scatter")
        lo, hi = (float(v) for v in sc._y_range)
        assert check(lo, hi), (lo, hi)
        assert Axis[y].label in sc.figure.title.text
    finally:
        vp.close()


@pytest.mark.parametrize("fmt", ["msv2", "msv4"])
def test_constructor_settings_reach_the_scatter(sim_paths, fmt):
    # No scatter gear-tab controls yet: the constructor's values apply.
    import warnings
    warnings.filterwarnings("ignore")
    from cubevis.toolbox.visplot import VisibilityPlotter
    rng = {}
    for label, kw in (("band", {}), ("8ch", dict(stat_chan_window=8))):
        vp = VisibilityPlotter(layout="side", correlation="XX", scatter_x="TIME",
                               scatter_y="PHASE_RMS", **kw, **sim_paths[fmt])
        try:
            sel = vp._build_selection()
            assert sel.stat_chan_window == kw.get("stat_chan_window", "off")
            sc = next(s.scatter for s in vp._slots if s.kind == "scatter")
            rng[label] = tuple(float(v) for v in sc._y_range)
        finally:
            vp.close()
    # 8-channel windows scatter more than the whole 64-channel band.
    assert rng["8ch"][1] - rng["8ch"][0] > rng["band"][1] - rng["band"][0]
