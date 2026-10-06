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

Windows
-------
By default a cell's statistic is taken over the samples it covers: the
dimensions that are not displayed.  Two settings refine that
(``SelectionSpec.stat_time_window`` / ``stat_chan_window``):

* Along a **displayed** axis a window groups neighbouring cells: the
  statistic is taken within each window and every cell in it shows that
  value.  The grid, its coordinates and everything downstream of the
  raster (flag overlay, probing, flagging by box) are unchanged; the
  picture simply becomes blocky at the window size.  This is what makes
  the statistics available on a single-baseline Time x Channel
  waterfall, where a cell is otherwise one sample.
* **Baselines** that are reduced (a Time x Channel raster with more than
  one baseline selected) are always pooled one by one: every baseline
  has its own phase, so the scatter is measured per baseline and the
  baselines combined.  The result is the phase stability of the selected
  baselines as a set -- one antenna's baselines, or the whole array.
* Along a **reduced** axis the windows are *pooled*: each window's
  scatter is measured about its own mean phase (and slope) and the
  windows' sums of squares and degrees of freedom are added.  So for
  Baseline x Channel, reduced over time in per-scan windows, phase jumps
  between scans and changes of source do not count as scatter.

Time windows are ``"scan"`` (a contiguous run of integrations) or a
length in seconds, and never span a gap; channel windows are a channel
count.  ``"auto"``, the default for time, means no window where time is
displayed and per-scan where it is reduced.

Cost and laziness
-----------------
The statistic is computed by a numpy kernel applied block by block
(``xr.apply_ufunc(..., dask="parallelized")``), so the result stays lazy
on dask-backed input and the MSv4 backend's fused compute and the MSv2
backend's decimate-then-compute keep working.  Sums only: no medians, no
sorting.  Windows are a Python loop inside the kernel, one iteration per
window (about half a millisecond each in testing).

Each block has to hold a cell's whole window, so the time / frequency
dimensions involved are rechunked into single chunks, and the remaining
(batch) dimensions are rechunked so that a block stays under
``_MAX_BLOCK_SAMPLES``.  Memory therefore does not grow with the number
of baselines, but one baseline's worth of the windowed dimensions must
fit: for Baseline x Channel that is the full selected time range times
the channels.

A first version expressed the slope search as lazy xarray operations
(shifted products per lag).  It was correct but 15-25x the cost of an
Amplitude raster, because every lag re-chunked the data; the kernel
form costs about the same as an Amplitude raster.

HRS milestone H2, slices 1-2 (``devel/notes/drs/visplot/hrs_visplot_plan.md``).
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

#: Reduced dimensions whose members are separate populations: the
#: statistic is taken for each member and the members pooled.
_POOL_DIMS = ("baseline_id",)

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


def _cell_sums(z: np.ndarray, n_core: int, want_rms: bool,
               slopes: tuple) -> tuple:
    """Sufficient statistics of one window: reduce the last *n_core* axes.

    Returns ``(a, b)``, both shaped like *z* without its core axes, such
    that the statistic of the window is ``sqrt(a / b)`` in radians for
    Phase RMS (sum of squared residuals over degrees of freedom) and
    ``a / b`` for Coherence (magnitude of the vector sum over the scalar
    sum).  Kept as sums so that several windows can be *pooled* into one
    cell by adding their ``a`` and their ``b`` -- see
    ``_phase_stat_kernel``.  A window with too few samples contributes
    zero to both.

    *slopes* is a tuple of ``(core_position, k)``: which of the core axes
    to remove a slope along, and the sample positions there.
    """
    core = tuple(range(z.ndim - n_core, z.ndim))
    amp = np.abs(z)
    if want_rms:
        # Unit phasors: every sample counts equally.  Zero-amplitude
        # samples have no phase and drop out.
        u = np.where(amp > 0, z / np.where(amp > 0, amp, 1.0), np.nan)
    else:
        u = z
    n_fit = 1                # parameters fitted per window: the mean phase
    for pos, k in slopes:
        if u.shape[z.ndim - n_core + pos] >= 3:
            u = _remove_slope_np(u, z.ndim - n_core + pos, k, core)
            n_fit += 1       # ... plus one slope
    n = np.sum(~np.isnan(u), axis=core)
    mean = np.nanmean(u, axis=core, keepdims=True)
    if not want_rms:
        ok = n >= 2
        vec = np.abs(mean.reshape(n.shape)) * n          # |sum of V|
        sca = np.nansum(amp, axis=core)                  # sum of |V|
        return np.where(ok, vec, 0.0), np.where(ok, sca, 0.0)
    resid = np.angle(u * np.conj(mean))
    # Sample RMS: divide by (n - fitted parameters), not n.  The mean
    # phase, and each slope removed, is fitted to the same samples and
    # absorbs some of their scatter; dividing by n would read low,
    # noticeably so for short windows (8 channels with a slope removed:
    # 13% low).
    dof = n - n_fit
    ok = dof > 0
    ss = np.nansum(resid * resid, axis=core)
    return np.where(ok, ss, 0.0), np.where(ok, dof, 0).astype(np.float64)


def _phase_stat_kernel(z: np.ndarray, *, n_pool: int, n_other: int,
                       want_rms: bool, axes: tuple) -> np.ndarray:
    """Numpy kernel behind :func:`reduce_phase_stat`.

    *z* is complex, NaN where a sample is flagged or missing, with its
    core axes last, in this order: *n_pool* axes whose members are each
    their own population and are pooled (baseline); *n_other* axes that
    are simply reduced together (no windows, no slope); then one axis
    per entry of *axes*.

    Each entry of *axes* describes a time or frequency axis::

        (k, blocks, displayed, detrend)

    ``k``         sample positions in median steps, or ``None`` if the
                  axis cannot support a slope;
    ``blocks``    list of ``(start, stop)`` windows covering the axis;
    ``displayed`` True if the axis is also an output axis (each sample
                  shows its window's value), False if it is reduced
                  (the windows are pooled into the one cell);
    ``detrend``   remove a slope along this axis within each window.

    The statistic is computed window by window and then either painted
    back over the window's own samples (displayed axis) or pooled
    (reduced axis).  Pooling adds the windows' sufficient statistics, so
    the pooled Phase RMS is the RMS about each window's *own* mean phase
    and slope: scan-to-scan phase jumps and source changes do not count
    as scatter.  The output has one axis per displayed entry, in order.
    """
    z = np.asarray(z)
    if not np.iscomplexobj(z):
        z = z.astype(np.complex128)
    n_ax = len(axes)
    lead = z.shape[:z.ndim - n_pool - n_other - n_ax]
    pool_axes = tuple(range(len(lead), len(lead) + n_pool))
    out_shape = lead + tuple(z.shape[z.ndim - n_ax + i] if axes[i][2] else 1
                             for i in range(n_ax))
    acc_a = np.zeros(out_shape)
    acc_b = np.zeros(out_shape)
    n_core = n_other + n_ax

    def walk(i, src_idx, dst_idx, slopes):
        if i == n_ax:
            sub = z[(Ellipsis,) + tuple(src_idx)]
            a, b = _cell_sums(sub, n_core, want_rms, tuple(slopes))
            if pool_axes:
                # One statistic per member (per baseline), then pooled:
                # each baseline's scatter is about its own mean phase.
                a, b = a.sum(axis=pool_axes), b.sum(axis=pool_axes)
            tgt = (Ellipsis,) + tuple(dst_idx)
            expand = (Ellipsis,) + (None,) * n_ax
            acc_a[tgt] += a[expand]
            acc_b[tgt] += b[expand]
            return
        k, blocks, displayed, detrend = axes[i]
        for start, stop in blocks:
            sl = slice(start, stop)
            more = ([(n_other + i, k[start:stop])]
                    if (detrend and k is not None) else [])
            walk(i + 1, src_idx + [sl],
                 dst_idx + [sl if displayed else slice(0, 1)],
                 slopes + more)

    with warnings.catch_warnings(), np.errstate(invalid="ignore", divide="ignore"):
        # All-NaN windows are expected (fully flagged, padded baselines).
        warnings.simplefilter("ignore", RuntimeWarning)
        walk(0, [slice(None)] * n_other, [], [])
        ratio = acc_a / np.where(acc_b > 0, acc_b, 1.0)
        val = np.sqrt(ratio) * _RAD2DEG if want_rms else ratio
        val = np.where(acc_b > 0, val, np.nan)
    keep = tuple(i for i in range(n_ax) if axes[i][2])
    drop = tuple(len(lead) + i for i in range(n_ax) if i not in keep)
    return val.squeeze(axis=drop) if drop else val


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

#: Largest block handed to the kernel, in samples (complex128: 16 bytes
#: each, and the kernel makes a few temporaries of a window's size).
_MAX_BLOCK_SAMPLES = 8_000_000


def normalize_time_window(value):
    """Canonical form of a time-window setting.

    Returns ``"auto"``, ``"off"``, ``"scan"``, or a positive float
    (seconds).  ``None`` / empty means ``"auto"``.  Raises ``ValueError``
    for anything else.
    """
    if value is None or value == "":
        return "auto"
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("auto", "off", "scan"):
            return v
        try:
            value = float(v)
        except ValueError:
            raise ValueError(
                "time window must be 'auto', 'off', 'scan' or a number of "
                f"seconds; got {value!r}") from None
    value = float(value)
    if not np.isfinite(value) or value <= 0:
        raise ValueError(f"time window must be positive; got {value!r}")
    return value


def normalize_chan_window(value):
    """Canonical form of a channel-window setting: ``"off"`` or an int
    >= 2 (channels).  ``None`` / empty / 0 / 1 means ``"off"``."""
    if value is None or value == "":
        return "off"
    if isinstance(value, str):
        v = value.strip().lower()
        if v == "off":
            return "off"
        try:
            value = int(float(v))
        except ValueError:
            raise ValueError(
                "channel window must be 'off' or a number of channels; "
                f"got {value!r}") from None
    value = int(value)
    if value < 0:
        raise ValueError(f"channel window must not be negative; got {value!r}")
    return "off" if value < 2 else value


def resolve_time_window(window, time_displayed: bool):
    """What ``"auto"`` means for this raster.

    * Time is a displayed axis: ``"off"`` -- each integration is its own
      cell, as the axis says.
    * Time is reduced: ``"scan"`` -- the statistic is taken within each
      scan and the scans pooled, so that phase jumps between scans and
      changes of source do not read as scatter.

    Anything other than ``"auto"`` is returned unchanged.
    """
    window = normalize_time_window(window)
    if window == "auto":
        return "off" if time_displayed else "scan"
    return window


def _runs(k: np.ndarray) -> list:
    """Contiguous runs of an axis: split wherever consecutive samples are
    more than 1.5 median steps apart (a gap between scans)."""
    n = len(k)
    if n == 0:
        return []
    breaks = np.nonzero(np.abs(np.diff(k)) > 1.5)[0] + 1
    edges = [0, *breaks.tolist(), n]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]


def _split(start: int, stop: int, size: int) -> list:
    """Cut ``[start, stop)`` into windows of *size* samples.  A final
    piece shorter than half a window is merged into the one before it."""
    size = max(1, int(size))
    cuts = list(range(start, stop, size)) + [stop]
    out = [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)]
    if len(out) >= 2 and (out[-1][1] - out[-1][0]) * 2 < size:
        out[-2:] = [(out[-2][0], out[-1][1])]
    return out


def time_blocks(coord: xr.DataArray, window) -> Optional[list]:
    """Windows along a time coordinate for a *resolved* window setting.

    ``"off"``   one window, the whole axis (``None`` is returned when the
                caller should not window at all -- see
                ``reduce_phase_stat``);
    ``"scan"``  one window per contiguous run;
    seconds     each run cut into windows of that length.

    Windows never span a gap.
    """
    n = int(coord.size)
    if window == "off":
        return [(0, n)]
    k = _step_index(coord)
    if k is None:                       # too short / not numeric
        return [(0, n)]
    runs = _runs(k)
    if window == "scan":
        return runs
    vals = np.asarray(coord.values)
    if np.issubdtype(vals.dtype, np.datetime64):
        vals = vals.astype("datetime64[ns]").astype(np.int64) / 1e9
    step = abs(float(np.median(np.diff(vals.astype(np.float64)))))
    size = max(1, int(round(float(window) / step))) if step > 0 else n
    out = []
    for a, b in runs:
        out.extend(_split(a, b, size))
    return out


def chan_blocks(n: int, window) -> list:
    """Windows along a channel axis of *n* samples: ``"off"`` is one
    window; an integer cuts it into windows of that many channels."""
    if window == "off":
        return [(0, n)]
    return _split(0, n, int(window))


def describe_windows(time_window, chan_window, time_displayed: bool,
                     chan_displayed: bool) -> str:
    """Short text for a title: how the statistic's window is built.

    Empty when the window is simply "the samples the cell covers".
    """
    tw = resolve_time_window(time_window, time_displayed)
    cw = normalize_chan_window(chan_window)
    parts = []
    if tw == "scan":
        parts.append("per scan")
    elif tw != "off":
        parts.append(f"{tw:g} s")
    if cw != "off":
        parts.append(f"{cw} ch")
    return " x ".join(parts)


def reduce_phase_stat(
    vis: xr.DataArray,
    flag: xr.DataArray,
    quantity: Axis,
    reduce_dims: Sequence[str],
    detrend: bool = True,
    time_window="auto",
    chan_window="off",
) -> xr.DataArray:
    """Reduce *vis* to Phase RMS (deg) or Coherence.

    Parameters
    ----------
    vis :
        Visibility for one polarization.
    flag :
        Boolean mask aligned with *vis*; ``True`` samples are excluded.
    quantity :
        ``Axis.PHASE_RMS`` or ``Axis.COHERENCE``.
    reduce_dims :
        The dimensions that are not displayed.  May be empty when a
        window along a displayed axis is given.
    detrend :
        Remove a linear phase slope along time / frequency within each
        window first (see the module docstring).
    time_window :
        ``"auto"`` (default), ``"off"``, ``"scan"``, or seconds.  See
        ``resolve_time_window`` and the module docstring.
    chan_window :
        ``"off"`` (default) or a number of channels.

    Returns
    -------
    xr.DataArray
        The dimensions of *vis* that are not in *reduce_dims*, same
        sizes and coordinates: windows along a displayed axis do not
        change the grid, every sample shows its window's value.  Lazy if
        the inputs are.  NaN where no window had enough usable samples:
        Coherence needs two; Phase RMS needs more samples than fitted
        parameters (the mean phase, plus one per slope removed).
    """
    if quantity not in STAT_QUANTITIES:
        raise ValueError(f"reduce_phase_stat: unsupported quantity {quantity}")
    reduce_set = set(reduce_dims)
    t_disp = "time" in vis.dims and "time" not in reduce_set
    f_disp = "frequency" in vis.dims and "frequency" not in reduce_set
    tw = resolve_time_window(time_window, t_disp)
    cw = normalize_chan_window(chan_window)

    # Time / frequency axes the kernel must see whole: every reduced one,
    # and a displayed one only when it has a window (otherwise each of
    # its samples is independent and stays an ordinary batch axis).
    axes, axis_dims, out_dims = [], [], []
    for dim, win, disp in (("time", tw, t_disp), ("frequency", cw, f_disp)):
        if dim not in vis.dims:
            continue
        if disp and win == "off":
            continue
        n = int(vis.sizes[dim])
        k = _step_index(vis.coords[dim]) if dim in vis.coords else None
        if dim == "time":
            blocks = (time_blocks(vis.coords[dim], win)
                      if dim in vis.coords else [(0, n)])
        else:
            blocks = chan_blocks(n, win)
        axes.append((k, blocks, disp, bool(detrend)))
        axis_dims.append(dim)
        if disp:
            out_dims.append(dim)
    # Reduced baselines are pooled, not lumped: every baseline has its
    # own phase, so the scatter is measured per baseline and combined.
    # (Lumping them reads ~100 deg for any set of baselines with
    # different phases, however stable each one is.)
    pool = [d for d in vis.dims if d in reduce_set and d in _POOL_DIMS]
    other = [d for d in vis.dims
             if d in reduce_set and d not in axis_dims and d not in pool]
    core = pool + other + axis_dims
    if not (other or axis_dims):
        raise ValueError(
            "reduce_phase_stat: nothing to take a statistic over (one "
            "sample per baseline per cell and no window)")
    if not core:
        raise ValueError(
            "reduce_phase_stat: nothing to take a statistic over (no "
            "reduced dimension and no window along a displayed one)")

    z = vis.where(~flag)

    # Keep kernel blocks to a sane size: the core dimensions must be whole
    # in each block, so limit the batch dimensions instead.
    if getattr(z, "chunks", None) is not None:
        per_cell = int(np.prod([z.sizes[d] for d in core]))
        budget = max(1, _MAX_BLOCK_SAMPLES // max(1, per_cell))
        rechunk = {}
        for d in z.dims:
            if d in core:
                continue
            size = max(1, min(int(z.sizes[d]), budget))
            rechunk[d] = size
            budget = max(1, budget // size)
        if rechunk:
            z = z.chunk(rechunk)

    with warnings.catch_warnings():
        # While building the graph dask infers the output's meta by
        # casting the complex input's meta to float64, and numpy warns
        # that the imaginary part is discarded.  Nothing is discarded:
        # the kernel returns real values.  (Passing meta explicitly is
        # not possible through apply_ufunc alongside output_dtypes.)
        warnings.filterwarnings(
            "ignore", message="Casting complex values to real")
        out = xr.apply_ufunc(
            _phase_stat_kernel, z,
            input_core_dims=[core],
            output_core_dims=[out_dims],
            kwargs=dict(n_pool=len(pool), n_other=len(other),
                        want_rms=(quantity == Axis.PHASE_RMS),
                        axes=tuple(axes)),
            dask="parallelized",
            output_dtypes=[np.float64],
            dask_gufunc_kwargs=dict(allow_rechunk=True),
        )
    # apply_ufunc puts output core dims last; restore the input's order.
    return out.transpose(*[d for d in vis.dims if d in out.dims])
