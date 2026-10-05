"""Phase-stability statistics for raster cells: Phase RMS and Coherence.

What these are for
------------------
Both answer "how stable is the phase across the samples this cell
covers?", which is the first thing looked at when commissioning an
interferometer (HRS requirement: "plot phase rms vs time and
frequency").  A raster cell covers the samples along the dimensions that
are *not* displayed, so the two standard views fall out of the raster's
existing axis choices:

* Baseline x Time     -- each cell is one integration on one baseline;
  the statistic is taken across the band.  "Phase rms vs time."
* Baseline x Channel  -- each cell is one channel on one baseline; the
  statistic is taken across the selected time range.  "Phase rms vs
  frequency."

``Axis.PHASE_RMS``
    RMS, in degrees, of each sample's phase about the cell's mean phase
    direction.  Every unflagged sample counts equally (unit phasors), so
    it measures phase scatter, not amplitude.  The differences are
    wrapped into (-180, 180] before squaring, so a cell straddling the
    +/-180 deg wrap is handled correctly.  0 for perfectly stable phase;
    about 104 deg (180/sqrt(3)) for pure noise, where every phase is
    equally likely.

``Axis.COHERENCE``
    ``|mean(V)| / mean(|V|)`` -- vector-averaged over scalar-averaged
    amplitude, in [0, 1].  1 when the samples add coherently; near
    ``1/sqrt(N)`` for noise.  For small phase noise ``sigma`` (radians)
    it is about ``exp(-sigma**2 / 2)``, so it carries the same
    information as Phase RMS, expressed as the fraction of signal that
    survives averaging.  This is the quantity AIPS exposes as EDITR's
    coherence display and IBLED's decorrelation index.

Slope removal (``detrend``)
---------------------------
Before fringe fitting, VLBI-style data have a residual *delay* (phase
linear in frequency) and *rate* (phase linear in time).  Left in, that
slope dominates both statistics and hides the scatter the user is
looking for: two turns of phase across the band give ~104 deg and zero
coherence with no noise at all.  With ``detrend=True`` (the default) a
linear phase slope is estimated and removed, per cell, along each
reduced dimension that is time or frequency, before the statistic is
taken.  With ``detrend=False`` the statistic is of the data as they are,
which is the right thing to look at when the question is "is there a
delay?".

The slope is estimated in stages.  First coarsely, from mean lag-m
products ``angle(sum(z[k] * conj(z[k-m]))) / m`` (m = 1, 4, 16, ... up to a
third of the window, each refining the last), which need no unwrapping and are unbiased across the
+/-180 deg wrap; then refined by a least-squares straight-line fit to
the residual phases, which by then are small enough not to wrap.  In
testing this recovers the true scatter to within a few percent for
per-sample phase noise up to about 45 deg rms.  It is not a fringe fit: it
needs enough signal per sample for adjacent phases to be related, and
cannot recover a slope steeper than half a turn per sample.  On pure
noise there is no slope to find and the result is the noise value
either way.  Only pairs of samples that really are m steps apart by
their coordinate are used, so gaps (between scans) do not corrupt the
estimate, and the correction is applied using the real coordinate, so it
is right across those gaps too.

Cost and laziness
-----------------
The statistic is computed by a numpy kernel applied block by block
(``xr.apply_ufunc(..., dask="parallelized")``), so the result stays lazy
on dask-backed input and the MSv4 backend's fused compute and the MSv2
backend's decimate-then-compute keep working.  Sums only: no medians, no
sorting.

One consequence to know about: each block has to hold a cell's whole
window, so the reduced dimensions are rechunked into a single chunk.
For Baseline x Time (reduced over frequency) that is cheap.  For
Baseline x Channel (reduced over time) every block spans the full
selected time range, so memory grows with the time range selected -- the
same requirement Z-Score's per-baseline median already has.

A first version expressed the slope search as lazy xarray operations
(shifted products per lag).  It was correct but 15-25x the cost of an
Amplitude raster, because every lag re-chunked the data; the kernel
form measured about 4x.

HRS milestone H2, slice 1 (``devel/notes/drs/visplot/hrs_visplot_plan.md``).
"""
from __future__ import annotations

import warnings
from typing import Optional, Sequence

import numpy as np
import xarray as xr

from ..axes import Axis

_RAD2DEG = 180.0 / np.pi

#: Reduced dimensions along which a linear phase slope is meaningful.
_SLOPE_DIMS = ("frequency", "time")

STAT_QUANTITIES = (Axis.PHASE_RMS, Axis.COHERENCE)
"""Raster quantities computed by :func:`reduce_phase_stat`."""


def _step_index(coord: xr.DataArray) -> Optional[np.ndarray]:
    """Position of each sample along *coord*, in units of the median step.

    Returns a float array (0 at the first sample), or ``None`` when the
    coordinate cannot support a slope (fewer than 3 samples, non-numeric,
    or no usable median step).  Using the real coordinate rather than
    the sample index is what makes slope removal correct across gaps
    (between scans) and for unevenly sampled data.
    """
    vals = np.asarray(coord.values)
    if vals.ndim != 1 or vals.size < 3:
        return None
    if np.issubdtype(vals.dtype, np.datetime64):
        vals = vals.astype("datetime64[ns]").astype(np.int64) / 1e9
    elif not np.issubdtype(vals.dtype, np.number):
        return None
    vals = vals.astype(np.float64)
    diffs = np.diff(vals)
    finite = diffs[np.isfinite(diffs) & (diffs != 0)]
    if finite.size == 0:
        return None
    # Signed median: frequency can run downwards (lower sideband).
    step = float(np.median(finite))
    if not np.isfinite(step) or step == 0.0:
        return None
    return (vals - vals[0]) / step


def _lag_ladder(n: int) -> list:
    """Lags used for the coarse slope search in a window of *n* samples:
    1, 4, 16, ... up to a third of the window."""
    lags, m = [], 1
    while m <= max(1, n // 3):
        lags.append(m)
        m *= 4
    if n >= 6 and lags[-1] < n // 3:
        lags.append(n // 3)
    return lags


def _remove_slope_np(u: np.ndarray, axis: int, k: np.ndarray,
                     core: tuple) -> np.ndarray:
    """Remove, per cell, the linear phase slope of *u* along *axis*.

    *u* is complex with NaN for unusable samples; *core* are the axes a
    cell is reduced over (*axis* is one of them); *k* is the position of
    each sample along *axis* in median steps.

    Stage 1, coarse and wrap-proof -- a ladder of mean lag-m products.
    ``angle(sum(u[i] * conj(u[i-m])))`` is m times the slope, with no
    unwrapping.  Lag 1 can never alias (short of half a turn per sample)
    but is imprecise; a lag of m is m times more precise but aliases
    unless the slope is already known to better than half a turn per m
    samples.  So climb: each lag refines the estimate from the one
    before, m growing by 4 up to a third of the window.  (Lag 1 followed
    directly by the long lag fails for noisy samples in long windows --
    45 deg of noise over 256 channels read 61 deg -- because lag 1 alone
    is then too imprecise to keep the long lag from aliasing.)  Each
    rung needs only the raw lag-m sum: the slope known so far is taken
    out of that one number per cell, not out of the data.  Pairs are
    used only where the two samples really are m steps apart, so gaps do
    not corrupt the estimate; a window too broken up to offer any such
    pair skips that rung.

    Stage 2, least squares.  After stage 1 the residual phases are small
    enough not to wrap, so an ordinary straight-line fit to them is
    valid, uses every sample, and removes what tilt is left.  (With lag
    1 alone, 20 deg of true noise read ~24 deg.)
    """
    n = u.shape[axis]
    shape = [1] * u.ndim
    shape[axis] = n
    kb = k.reshape(shape)

    def sl(a, b):
        idx = [slice(None)] * u.ndim
        idx[axis] = slice(a, b)
        return tuple(idx)

    slope = None
    for m in _lag_ladder(n):
        ok = np.abs((k[m:] - k[:-m]) - m) < 0.5
        if not ok.any():
            continue
        p = u[sl(m, None)] * np.conj(u[sl(None, n - m)])
        okb = ok.reshape([n - m if i == axis else 1 for i in range(u.ndim)])
        p = np.where(okb, p, np.nan)
        total = np.nanmean(p, axis=core, keepdims=True)
        if slope is not None:
            total = total * np.exp(-1j * slope * m)
        step = np.angle(total) / m
        step = np.where(np.isfinite(step), step, 0.0)   # no usable pair
        slope = step if slope is None else slope + step
    if slope is None:
        return u
    u = u * np.exp(-1j * slope * kb)

    mean = np.nanmean(u, axis=core, keepdims=True)
    resid = np.angle(u * np.conj(mean))
    kk = np.where(np.isnan(resid), np.nan, kb)
    k_mean = np.nanmean(kk, axis=core, keepdims=True)
    r_mean = np.nanmean(resid, axis=core, keepdims=True)
    cov = np.nanmean(kk * resid, axis=core, keepdims=True) - k_mean * r_mean
    var = np.nanmean(kk * kk, axis=core, keepdims=True) - k_mean * k_mean
    fine = np.where(var > 0, cov / np.where(var > 0, var, 1.0), 0.0)
    fine = np.where(np.isfinite(fine), fine, 0.0)
    return u * np.exp(-1j * fine * kb)


def _phase_stat_kernel(z: np.ndarray, *, n_core: int, want_rms: bool,
                       slopes: tuple) -> np.ndarray:
    """Numpy kernel: reduce the last *n_core* axes of *z* to the statistic.

    *z* is complex with NaN where a sample is flagged or missing.
    *slopes* is a tuple of ``(core_position, k)`` -- which of the core
    axes to remove a slope along, and the sample positions there.
    """
    z = np.asarray(z)
    if not np.iscomplexobj(z):
        z = z.astype(np.complex128)
    core = tuple(range(z.ndim - n_core, z.ndim))
    with warnings.catch_warnings(), np.errstate(invalid="ignore", divide="ignore"):
        # All-NaN cells are expected (fully flagged, padded baselines).
        warnings.simplefilter("ignore", RuntimeWarning)
        amp = np.abs(z)
        if want_rms:
            # Unit phasors: every sample counts equally.  Zero-amplitude
            # samples have no phase and drop out.
            u = np.where(amp > 0, z / np.where(amp > 0, amp, 1.0), np.nan)
        else:
            u = z
        n_fit = 1            # parameters fitted per cell: the mean phase
        for pos, k in slopes:
            u = _remove_slope_np(u, z.ndim - n_core + pos, k, core)
            n_fit += 1       # ... plus one slope
        n = np.sum(~np.isnan(u), axis=core)
        mean = np.nanmean(u, axis=core, keepdims=True)
        if not want_rms:
            denom = np.nanmean(amp, axis=core)
            coh = np.abs(mean.reshape(n.shape)) / np.where(denom > 0, denom, np.nan)
            return np.where(n >= 2, coh, np.nan)
        resid = np.angle(u * np.conj(mean))
        # Sample RMS: divide by (n - fitted parameters), not n.  The
        # mean phase, and each slope removed, is fitted to the same
        # samples and absorbs some of their scatter; dividing by n would
        # read low, noticeably so for short windows (8 channels with a
        # slope removed: 13% low).
        dof = n - n_fit
        ss = np.nansum(resid * resid, axis=core)
        rms = np.sqrt(ss / np.where(dof > 0, dof, 1)) * _RAD2DEG
        return np.where(dof > 0, rms, np.nan)


def reduce_phase_stat(
    vis: xr.DataArray,
    flag: xr.DataArray,
    quantity: Axis,
    reduce_dims: Sequence[str],
    detrend: bool = True,
) -> xr.DataArray:
    """Reduce *vis* over *reduce_dims* to Phase RMS (deg) or Coherence.

    Parameters
    ----------
    vis :
        Visibility for one polarization.
    flag :
        Boolean mask aligned with *vis*; ``True`` samples are excluded.
    quantity :
        ``Axis.PHASE_RMS`` or ``Axis.COHERENCE``.
    reduce_dims :
        The dimensions the statistic is taken over (the cell's window).
        Must be non-empty.
    detrend :
        Remove a linear phase slope along each reduced time / frequency
        dimension first (see the module docstring).

    Returns
    -------
    xr.DataArray
        Lazy if the inputs are.  NaN where a cell has too few usable
        samples: Coherence needs two; Phase RMS needs more samples than
        fitted parameters (the mean phase, plus one per slope removed).
    """
    if quantity not in STAT_QUANTITIES:
        raise ValueError(f"reduce_phase_stat: unsupported quantity {quantity}")
    reduce_dims = [d for d in vis.dims if d in set(reduce_dims)]
    if not reduce_dims:
        raise ValueError("reduce_phase_stat: reduce_dims is empty")

    slopes = []
    if detrend:
        # Frequency before time: the delay is usually the larger slope.
        for dim in _SLOPE_DIMS:
            if dim in reduce_dims and dim in vis.coords:
                k = _step_index(vis.coords[dim])
                if k is not None:
                    slopes.append((reduce_dims.index(dim), k))

    z = vis.where(~flag)
    with warnings.catch_warnings():
        # While building the graph dask infers the output's meta by
        # casting the complex input's meta to float64, and numpy warns
        # that the imaginary part is discarded.  Nothing is discarded:
        # the kernel returns real values.  (Passing meta explicitly is
        # not possible through apply_ufunc alongside output_dtypes.)
        warnings.filterwarnings(
            "ignore", message="Casting complex values to real")
        return xr.apply_ufunc(
            _phase_stat_kernel, z,
            input_core_dims=[reduce_dims],
            kwargs=dict(n_core=len(reduce_dims),
                        want_rms=(quantity == Axis.PHASE_RMS),
                        slopes=tuple(slopes)),
            dask="parallelized",
            output_dtypes=[np.float64],
            dask_gufunc_kwargs=dict(allow_rechunk=True),
        )
