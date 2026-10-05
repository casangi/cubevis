"""
test_raster_averaging.py
==========================
Tests for HRS milestone H1 (2026-10): Amplitude and Phase raster cells
are reduced from the complex visibility, with ``SelectionSpec.averaging``
choosing scalar (default) or vector averaging.

Location in repository:
    cubevis/tests/manual/visplot/test_raster_averaging.py

Run:
    pytest cubevis/tests/manual/visplot/test_raster_averaging.py -v

What is pinned here:

* the bug: Phase used to be the arithmetic mean of wrapped per-sample
  phases, so samples straddling +/-180 deg averaged to ~0 deg.  Both
  modes now give ~180 deg;
* scalar Amplitude is unchanged (mean of |V|), so existing plots and
  tests that rely on it keep their values;
* vector Amplitude is |mean(V)| and falls for incoherent samples
  (noise, a delay slope across the averaged channels);
* flagged samples are excluded in every mode; a fully flagged cell is
  NaN;
* Real / Imaginary / Z-Score / Flag do not depend on the mode;
* with nothing to reduce the per-sample value is returned;
* MSv2Backend and MSv4Backend agree;
* the option travels on SelectionSpec (default, copy, validation).

All synthetic -- no real MS/PS needed for any test in this file.
"""
from __future__ import annotations

import dataclasses

import dask.array as da
import numpy as np
import pytest
import xarray as xr

from cubevis.toolbox.visplot.axes import Axis
from cubevis.toolbox.visplot.data._raster_average import reduce_amp_phase
from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
from cubevis.toolbox.visplot.selection import (
    AVERAGING_MODES, SelectionSpec, normalize_averaging,
)

DIMS = ("time", "baseline_id", "frequency", "polarization")


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


def _dataset(vis, flag=None, chunked=True):
    """(time, baseline_id, frequency, polarization) dataset, one pol."""
    vis = np.asarray(vis, dtype=np.complex128)
    if vis.ndim == 3:
        vis = vis[..., None]
    if flag is None:
        flag = np.zeros(vis.shape, dtype=bool)
    else:
        flag = np.asarray(flag, dtype=bool)
        if flag.ndim == 3:
            flag = flag[..., None]
    nt, nb, nf, _ = vis.shape
    if chunked:
        vis = da.from_array(vis, chunks=(max(1, nt // 2), nb, max(1, nf // 2), 1))
        flag = da.from_array(flag, chunks=(max(1, nt // 2), nb, max(1, nf // 2), 1))
    return xr.Dataset(
        data_vars={"VISIBILITY": (DIMS, vis), "FLAG": (DIMS, flag)},
        coords={"time": np.arange(nt, dtype=np.float64),
                "baseline_id": np.arange(nb),
                "frequency": np.linspace(1e9, 1.1e9, nf),
                "polarization": ["XX"]},
    )


def _raster(backend_name, ds, y, x, qty, averaging=None):
    """Run _raster_2d and return a computed (y, x) numpy array."""
    b = BACKENDS[backend_name]()
    kw = {} if averaging is None else {"averaging": averaging}
    arr = b._raster_2d(ds, y, x, qty, "XX", **kw)
    assert arr is not None
    return np.asarray(arr.compute().values)


def _phasor(deg):
    return np.exp(1j * np.deg2rad(deg))


def _wrap_dist(a, b):
    """Smallest absolute angular distance between a and b, degrees."""
    return np.abs((np.asarray(a) - np.asarray(b) + 180.0) % 360.0 - 180.0)


@pytest.fixture(params=sorted(BACKENDS))
def backend_name(request):
    return request.param


@pytest.fixture(params=AVERAGING_MODES)
def mode(request):
    return request.param


# ---------------------------------------------------------------------------
# 1. The bug: phase across the +/-180 deg wrap
# ---------------------------------------------------------------------------

class TestPhaseWrap:

    def _wrap_ds(self):
        # 4 times x 3 baselines x 6 channels; every sample has unit
        # amplitude and phase alternating +179 / -179 deg along frequency.
        ph = np.where(np.arange(6) % 2 == 0, 179.0, -179.0)
        vis = np.broadcast_to(_phasor(ph), (4, 3, 6)).copy()
        return _dataset(vis)

    def test_wrap_gives_180_not_0(self, backend_name, mode):
        out = _raster(backend_name, self._wrap_ds(),
                      Axis.TIME, Axis.BASELINE, Axis.PHASE, mode)
        assert out.shape == (4, 3)
        assert np.all(_wrap_dist(out, 180.0) < 1e-9)

    def test_arithmetic_mean_would_have_been_zero(self):
        # Documents what the old code returned, so the test above is
        # known to distinguish the two behaviours.
        ph = np.where(np.arange(6) % 2 == 0, 179.0, -179.0)
        assert abs(ph.mean()) < 1e-12

    def test_default_mode_is_fixed_too(self, backend_name):
        out = _raster(backend_name, self._wrap_ds(),
                      Axis.TIME, Axis.BASELINE, Axis.PHASE)   # no averaging kw
        assert np.all(_wrap_dist(out, 180.0) < 1e-9)

    def test_range_is_minus180_to_180(self, backend_name, mode):
        rng = np.random.default_rng(3)
        vis = _phasor(rng.uniform(-180, 180, (5, 4, 7)))
        out = _raster(backend_name, _dataset(vis),
                      Axis.TIME, Axis.BASELINE, Axis.PHASE, mode)
        assert np.all(out > -180.0 - 1e-9) and np.all(out <= 180.0 + 1e-9)

    def test_constant_phase_is_returned_exactly(self, backend_name, mode):
        vis = np.full((3, 2, 5), 2.5 * _phasor(37.0))
        out = _raster(backend_name, _dataset(vis),
                      Axis.FREQUENCY, Axis.BASELINE, Axis.PHASE, mode)
        assert out.shape == (5, 2)
        assert np.allclose(out, 37.0, atol=1e-9)


# ---------------------------------------------------------------------------
# 2. Scalar vs vector: definitions against direct numpy
# ---------------------------------------------------------------------------

class TestAgainstNumpy:

    def _random(self, seed=11, shape=(6, 4, 9)):
        rng = np.random.default_rng(seed)
        vis = (rng.normal(1.0, 1.0, shape) + 1j * rng.normal(0.3, 1.0, shape))
        flag = rng.random(shape) < 0.2
        return vis, flag

    def test_scalar_amplitude_is_mean_of_abs(self, backend_name):
        vis, flag = self._random()
        out = _raster(backend_name, _dataset(vis, flag),
                      Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, "scalar")
        want = np.nanmean(np.where(flag, np.nan, np.abs(vis)), axis=2)
        assert np.allclose(out, want, rtol=1e-12, atol=1e-12)

    def test_vector_amplitude_is_abs_of_mean(self, backend_name):
        vis, flag = self._random()
        out = _raster(backend_name, _dataset(vis, flag),
                      Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, "vector")
        m = np.nanmean(np.where(flag, np.nan, vis.real), axis=2) \
            + 1j * np.nanmean(np.where(flag, np.nan, vis.imag), axis=2)
        assert np.allclose(out, np.abs(m), rtol=1e-12, atol=1e-12)

    def test_vector_phase_is_angle_of_mean(self, backend_name):
        vis, flag = self._random()
        out = _raster(backend_name, _dataset(vis, flag),
                      Axis.FREQUENCY, Axis.BASELINE, Axis.PHASE, "vector")
        m = np.nanmean(np.where(flag, np.nan, vis.real), axis=0) \
            + 1j * np.nanmean(np.where(flag, np.nan, vis.imag), axis=0)
        want = np.degrees(np.angle(m)).T          # (frequency, baseline)
        assert np.all(_wrap_dist(out, want) < 1e-9)

    def test_scalar_phase_is_circular_mean(self, backend_name):
        vis, flag = self._random()
        out = _raster(backend_name, _dataset(vis, flag),
                      Axis.FREQUENCY, Axis.BASELINE, Axis.PHASE, "scalar")
        u = vis / np.abs(vis)
        m = np.nanmean(np.where(flag, np.nan, u.real), axis=0) \
            + 1j * np.nanmean(np.where(flag, np.nan, u.imag), axis=0)
        want = np.degrees(np.angle(m)).T
        assert np.all(_wrap_dist(out, want) < 1e-9)

    def test_scalar_and_vector_phase_differ_when_amplitudes_differ(self, backend_name):
        # One strong sample at 0 deg, three weak ones at 90 deg: vector
        # follows the strong one, scalar the majority.
        vis = np.zeros((1, 1, 4), dtype=complex)
        vis[0, 0, :] = [10.0, 0.1j, 0.1j, 0.1j]
        ds = _dataset(vis)
        v = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, Axis.PHASE, "vector")
        s = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, Axis.PHASE, "scalar")
        assert abs(v[0, 0]) < 5.0
        assert s[0, 0] > 60.0

    def test_vector_amplitude_never_exceeds_scalar(self, backend_name):
        vis, flag = self._random(seed=5)
        ds = _dataset(vis, flag)
        v = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, "vector")
        s = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, "scalar")
        assert np.all(v <= s + 1e-12)


# ---------------------------------------------------------------------------
# 3. Coherence: what vector averaging is for
# ---------------------------------------------------------------------------

class TestCoherence:

    def test_noise_only_vector_amplitude_is_far_below_scalar(self, backend_name):
        rng = np.random.default_rng(1)
        shape = (4, 3, 4096)
        vis = rng.normal(0, 1, shape) + 1j * rng.normal(0, 1, shape)
        ds = _dataset(vis)
        v = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, "vector")
        s = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, "scalar")
        # scalar -> Rayleigh mean sqrt(pi/2) ~ 1.2533; vector -> ~1/sqrt(N)
        assert np.allclose(s, np.sqrt(np.pi / 2), rtol=0.05)
        assert np.all(v < 0.1)

    def test_delay_slope_decorrelates_vector_not_scalar(self, backend_name):
        # Unit-amplitude signal whose phase winds exactly two turns
        # across the band (a residual delay): the vector average over
        # frequency vanishes, the scalar average stays 1.
        nf = 64
        ph = 720.0 * np.arange(nf) / nf
        vis = np.broadcast_to(_phasor(ph), (3, 2, nf)).copy()
        ds = _dataset(vis)
        v = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, "vector")
        s = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, "scalar")
        assert np.allclose(s, 1.0, atol=1e-12)
        assert np.all(v < 1e-12)

    def test_coherent_signal_same_in_both_modes(self, backend_name):
        vis = np.full((3, 2, 16), 3.0 * _phasor(-50.0))
        ds = _dataset(vis)
        v = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, "vector")
        s = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, "scalar")
        assert np.allclose(v, 3.0) and np.allclose(s, 3.0)


# ---------------------------------------------------------------------------
# 4. Flags
# ---------------------------------------------------------------------------

class TestFlags:

    def test_flagged_samples_are_excluded(self, backend_name, mode):
        # Good samples: amplitude 1 at +10 deg.  One flagged sample per
        # cell: amplitude 1000 at -170 deg; it must not move the result.
        vis = np.full((3, 2, 8), _phasor(10.0))
        flag = np.zeros((3, 2, 8), dtype=bool)
        vis[:, :, 5] = 1000.0 * _phasor(-170.0)
        flag[:, :, 5] = True
        ds = _dataset(vis, flag)
        amp = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, mode)
        ph = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, Axis.PHASE, mode)
        assert np.allclose(amp, 1.0, atol=1e-9)
        assert np.allclose(ph, 10.0, atol=1e-9)

    def test_fully_flagged_cell_is_nan(self, backend_name, mode):
        vis = np.full((3, 2, 8), _phasor(10.0))
        flag = np.zeros((3, 2, 8), dtype=bool)
        flag[1, 0, :] = True
        ds = _dataset(vis, flag)
        for qty in (Axis.AMPLITUDE, Axis.PHASE):
            out = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, qty, mode)
            assert np.isnan(out[1, 0])
            assert np.isfinite(np.delete(out.ravel(), 2)).all()

    def test_nan_padded_slots_are_ignored(self, backend_name, mode):
        # xarray-ms pads missing (time, baseline) rows with NaN and marks
        # them flagged; an unflagged NaN must be harmless as well.
        vis = np.full((3, 2, 8), _phasor(25.0))
        vis[0, 1, 3] = np.nan + 1j * np.nan
        ds = _dataset(vis)
        ph = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, Axis.PHASE, mode)
        assert np.allclose(ph, 25.0, atol=1e-9)

    def test_zero_amplitude_sample_has_no_direction(self, backend_name):
        # Scalar phase: a zero sample is left out, not counted as 0 deg.
        vis = np.full((1, 1, 4), _phasor(120.0))
        vis[0, 0, 0] = 0.0
        out = _raster(backend_name, _dataset(vis),
                      Axis.TIME, Axis.BASELINE, Axis.PHASE, "scalar")
        assert np.allclose(out, 120.0, atol=1e-9)


# ---------------------------------------------------------------------------
# 5. What must NOT depend on the mode
# ---------------------------------------------------------------------------

class TestModeIndependent:

    def _ds(self):
        rng = np.random.default_rng(21)
        shape = (5, 4, 6)
        vis = rng.normal(2, 1, shape) + 1j * rng.normal(-1, 1, shape)
        flag = rng.random(shape) < 0.15
        return vis, flag, _dataset(vis, flag)

    @pytest.mark.parametrize("qty", [Axis.REAL, Axis.IMAGINARY])
    def test_real_imag(self, backend_name, qty):
        vis, flag, ds = self._ds()
        s = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, qty, "scalar")
        v = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, qty, "vector")
        part = vis.real if qty is Axis.REAL else vis.imag
        want = np.nanmean(np.where(flag, np.nan, part), axis=2)
        assert np.allclose(s, want) and np.allclose(v, want)

    @pytest.mark.parametrize("qty", [Axis.Z_SCORE, Axis.FLAG])
    def test_zscore_and_flag(self, backend_name, qty):
        import warnings
        _, _, ds = self._ds()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            s = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, qty, "scalar")
            v = _raster(backend_name, ds, Axis.TIME, Axis.BASELINE, qty, "vector")
        assert np.array_equal(s, v, equal_nan=True)

    def test_nothing_to_reduce_returns_per_sample(self, backend_name, mode):
        # Single baseline, single pol, TIME x FREQUENCY: the only reduced
        # dimension has size 1, so each cell is one sample.
        rng = np.random.default_rng(8)
        vis = rng.normal(0, 1, (5, 1, 7)) + 1j * rng.normal(0, 1, (5, 1, 7))
        ds = _dataset(vis)
        amp = _raster(backend_name, ds, Axis.TIME, Axis.FREQUENCY, Axis.AMPLITUDE, mode)
        ph = _raster(backend_name, ds, Axis.TIME, Axis.FREQUENCY, Axis.PHASE, mode)
        assert np.allclose(amp, np.abs(vis[:, 0, :]))
        assert np.all(_wrap_dist(ph, np.degrees(np.angle(vis[:, 0, :]))) < 1e-9)


# ---------------------------------------------------------------------------
# 6. Backend parity and laziness
# ---------------------------------------------------------------------------

class TestParityAndLaziness:

    @pytest.mark.parametrize("qty", [Axis.AMPLITUDE, Axis.PHASE])
    @pytest.mark.parametrize("axes", [(Axis.TIME, Axis.BASELINE),
                                      (Axis.FREQUENCY, Axis.BASELINE),
                                      (Axis.BASELINE, Axis.TIME)])
    def test_msv2_equals_msv4(self, qty, axes, mode):
        rng = np.random.default_rng(42)
        shape = (6, 5, 8)
        vis = rng.normal(0.5, 1, shape) + 1j * rng.normal(0, 1, shape)
        flag = rng.random(shape) < 0.25
        ds = _dataset(vis, flag)
        a = _raster("msv2", ds, axes[0], axes[1], qty, mode)
        b = _raster("msv4", ds, axes[0], axes[1], qty, mode)
        assert a.shape == b.shape
        assert np.allclose(a, b, equal_nan=True, rtol=0, atol=1e-12)

    def test_result_is_lazy(self, backend_name, mode):
        # MSv4's OPT-B fuses every partition into one dask.compute();
        # MSv2 decimates before computing.  Both need a lazy result.
        vis = np.ones((4, 3, 6), dtype=complex)
        ds = _dataset(vis, chunked=True)
        arr = BACKENDS[backend_name]()._raster_2d(
            ds, Axis.TIME, Axis.BASELINE, Axis.PHASE, "XX", averaging=mode)
        assert isinstance(arr.data, da.Array)

    def test_dims_and_coords(self, backend_name, mode):
        vis = np.ones((4, 3, 6), dtype=complex)
        arr = BACKENDS[backend_name]()._raster_2d(
            _dataset(vis), Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE, "XX",
            averaging=mode)
        assert arr.dims == ("time", "baseline_id")
        assert set(arr.coords) == {"time", "baseline_id"}


# ---------------------------------------------------------------------------
# 7. query_raster reads the mode from the selection
# ---------------------------------------------------------------------------

class TestQueryRasterUsesSelection:

    def _backend(self, name, ds):
        b = BACKENDS[name]()
        b._iter_visibility_partitions = lambda selection: iter([ds])
        b._apply_selection = lambda raw_ds, selection: raw_ds
        return b

    def test_msv2_query_raster(self):
        nf = 32
        vis = np.broadcast_to(_phasor(360.0 * np.arange(nf) / nf), (3, 2, nf)).copy()
        b = self._backend("msv2", _dataset(vis))
        s, *_ = b.query_raster(Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE,
                               SelectionSpec(), polarization="XX")
        v, *_ = b.query_raster(Axis.TIME, Axis.BASELINE, Axis.AMPLITUDE,
                               SelectionSpec(averaging="vector"), polarization="XX")
        assert np.allclose(s.values, 1.0)
        assert np.all(np.abs(v.values) < 1e-12)


# ---------------------------------------------------------------------------
# 8. The helper's own contract
# ---------------------------------------------------------------------------

class TestHelperContract:

    def _one(self):
        ds = _dataset(np.ones((2, 2, 2), dtype=complex))
        return (ds["VISIBILITY"].sel(polarization="XX"),
                ds["FLAG"].sel(polarization="XX"))

    def test_rejects_other_quantities(self):
        vis, flag = self._one()
        with pytest.raises(ValueError):
            reduce_amp_phase(vis, flag, Axis.REAL, ["frequency"], "vector")

    def test_rejects_unknown_mode(self):
        vis, flag = self._one()
        with pytest.raises(ValueError):
            reduce_amp_phase(vis, flag, Axis.PHASE, ["frequency"], "median")

    def test_rejects_empty_reduce_dims(self):
        vis, flag = self._one()
        with pytest.raises(ValueError):
            reduce_amp_phase(vis, flag, Axis.PHASE, [], "vector")


# ---------------------------------------------------------------------------
# 9. SelectionSpec plumbing
# ---------------------------------------------------------------------------

class TestSelectionSpec:

    def test_default_is_scalar(self):
        assert SelectionSpec().averaging == "scalar"

    def test_copy_preserves(self):
        assert SelectionSpec(averaging="vector").copy().averaging == "vector"

    def test_replace_preserves(self):
        s = dataclasses.replace(SelectionSpec(averaging="vector"), flag_view="disk")
        assert s.averaging == "vector"

    def test_not_a_constraint(self):
        assert SelectionSpec(averaging="vector").is_empty()

    def test_in_cache_fingerprint(self):
        from cubevis.toolbox.visplot.data.reader import _selection_fingerprint
        assert (_selection_fingerprint(SelectionSpec(averaging="scalar"))
                != _selection_fingerprint(SelectionSpec(averaging="vector")))

    @pytest.mark.parametrize("given, want", [
        ("vector", "vector"), ("Vector", "vector"), (" SCALAR ", "scalar"),
        (None, "scalar"), ("", "scalar"),
    ])
    def test_normalize(self, given, want):
        assert normalize_averaging(given) == want

    def test_normalize_rejects_unknown(self):
        with pytest.raises(ValueError, match="scalar"):
            normalize_averaging("median")
