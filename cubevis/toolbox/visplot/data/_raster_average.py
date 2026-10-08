"""Averaging of Amplitude and Phase into raster cells.

Why this exists
---------------
``_raster_2d()`` (both backends) reduces a partition to the two displayed
dimensions by averaging over the others.  Until 2026-10 every quantity
except Z-Score was reduced as ``mean(per-sample quantity)``.  That is
right for Real and Imaginary (linear in the visibility) and is one
legitimate choice for Amplitude (*scalar* averaging), but it is wrong
for Phase: the arithmetic mean of wrapped per-sample phases is not a
phase.  Two samples at +179 deg and -179 deg are 2 deg apart and average
to 180 deg; their arithmetic mean is 0 deg, the opposite direction.

This module provides the two reductions that are meaningful
(``SelectionSpec.averaging``):

``"vector"`` (the package default, ``selection.DEFAULT_AVERAGING``)
    Average the complex visibility, then take amplitude / phase.
    Amplitude falls where the samples are incoherent (noise, residual
    delay across the averaged channels, residual rate across the
    averaged times).  This is the average AIPS and plotms users expect.

``"scalar"``
    Amplitude: mean of ``|V|`` (the only behaviour before 2026-10).
    Phase: circular mean -- the direction of the mean *unit* phasor, so
    every unflagged sample counts equally whatever its amplitude.

Everything stays lazy: the inputs are dask-backed ``DataArray``s and the
result is too, so the MSv4 backend's single fused ``dask.compute()``
(OPT-B) and the MSv2 backend's per-partition decimate-then-compute both
keep working unchanged.  Only streaming sums are used (no medians, no
sorting), so the cost is that of the Amplitude raster it replaces: two
means instead of one.

HRS milestone H1 (``devel/notes/drs/visplot/hrs_visplot_plan.md``).
"""
from __future__ import annotations

from typing import Sequence

import numpy as np
import xarray as xr

from ..axes import Axis

_RAD2DEG = 180.0 / np.pi


def reduce_amp_phase(
    vis: xr.DataArray,
    flag: xr.DataArray,
    quantity: Axis,
    reduce_dims: Sequence[str],
    averaging: str = "vector",
) -> xr.DataArray:
    """Reduce *vis* over *reduce_dims* to Amplitude or Phase (degrees).

    Parameters
    ----------
    vis :
        Visibility for one polarization (complex; real single-dish
        spectra also work -- their imaginary part is zero).
    flag :
        Boolean mask aligned with *vis*; ``True`` samples are excluded.
    quantity :
        ``Axis.AMPLITUDE`` or ``Axis.PHASE``.
    reduce_dims :
        Dimensions to average over.  Must be non-empty: with nothing to
        reduce the per-sample value is already the answer and the caller
        keeps it.
    averaging :
        ``"scalar"`` or ``"vector"`` (see the module docstring).

    Returns
    -------
    xr.DataArray
        Lazy if the inputs are.  A cell with no unflagged, finite sample
        is NaN.  Phase is in degrees in (-180, 180].
    """
    if quantity not in (Axis.AMPLITUDE, Axis.PHASE):
        raise ValueError(f"reduce_amp_phase: unsupported quantity {quantity}")
    if averaging not in ("scalar", "vector"):
        raise ValueError(
            f"averaging must be 'scalar' or 'vector'; got {averaging!r}")
    reduce_dims = list(reduce_dims)
    if not reduce_dims:
        raise ValueError("reduce_amp_phase: reduce_dims is empty")

    good = ~flag
    re = vis.real.where(good)
    im = vis.imag.where(good)

    if quantity == Axis.AMPLITUDE and averaging == "scalar":
        return np.hypot(re, im).mean(dim=reduce_dims, skipna=True)

    if averaging == "vector":
        a = re.mean(dim=reduce_dims, skipna=True)
        b = im.mean(dim=reduce_dims, skipna=True)
    else:
        # Circular mean: average the unit phasors.  A sample of exactly
        # zero amplitude has no direction; it is left out (NaN) rather
        # than counted as 0 deg.
        amp = np.hypot(re, im)
        amp = amp.where(amp > 0)
        a = (re / amp).mean(dim=reduce_dims, skipna=True)
        b = (im / amp).mean(dim=reduce_dims, skipna=True)

    if quantity == Axis.AMPLITUDE:
        return np.hypot(a, b)
    # arctan2(NaN, NaN) is NaN, so empty cells stay NaN.
    return np.arctan2(b, a) * _RAD2DEG


# ---------------------------------------------------------------------- #
# Several baselines in one cell (HRS H4, 2026-10-07)                       #
# ---------------------------------------------------------------------- #

BASELINE_DIM = "baseline_id"

MAX_QUANTITIES = (Axis.AMPLITUDE, Axis.AMP_VDIFF, Axis.PHASE_DIFF)
"""Quantities for which "the largest of the baselines' values" means
something: all are magnitudes (zero is "nothing there")."""


def combines_baselines(quantity: Axis, reduce_dims: Sequence[str],
                       baseline_combine: str) -> bool:
    """Whether *baseline_combine* changes how *quantity* is reduced over
    *reduce_dims* -- i.e. whether a title or a readout should name it."""
    if BASELINE_DIM not in reduce_dims:
        return False
    if quantity in (Axis.AMPLITUDE, Axis.PHASE):
        return True
    return quantity in MAX_QUANTITIES


def _median_over_baselines(per: xr.DataArray) -> xr.DataArray:
    """Median of *per* over the baseline dimension, ignoring NaN.

    A median needs every baseline of a cell in one block, so a
    dask-backed array is rechunked to a single chunk along baseline
    (the other dimensions keep their chunks, so memory per block is one
    block's worth of all baselines).  An all-NaN cell stays NaN; the
    "All-NaN slice" warning numpy raises for it at compute time is not
    an error here and callers may see it.
    """
    if getattr(per, "chunks", None) is not None:
        per = per.chunk({BASELINE_DIM: -1})
    return per.median(dim=BASELINE_DIM, skipna=True)


def reduce_amp_phase_baselines(
    vis: xr.DataArray,
    flag: xr.DataArray,
    quantity: Axis,
    reduce_dims: Sequence[str],
    averaging: str = "vector",
    baseline_combine: str = "mean",
) -> xr.DataArray:
    """Reduce *vis* to Amplitude or Phase where the cell may cover
    several baselines.

    Two steps, because they are different questions.  The samples of ONE
    baseline in the cell (the times or channels reduced away) are
    averaged by *averaging*, as :func:`reduce_amp_phase` does.  The
    baselines are then combined by *baseline_combine* -- see
    ``SelectionSpec.baseline_combine``.  With ``"coherent"``, or when
    baseline is not among *reduce_dims*, this is :func:`reduce_amp_phase`
    over everything, unchanged.

    Lazy if the inputs are; sums and one max only.
    """
    reduce_dims = list(reduce_dims)
    if baseline_combine not in ("mean", "median", "max", "coherent"):
        raise ValueError(
            f"baseline_combine must be 'mean', 'median', 'max' or "
            f"'coherent'; got {baseline_combine!r}")
    if baseline_combine == "coherent" or BASELINE_DIM not in reduce_dims:
        return reduce_amp_phase(vis, flag, quantity, reduce_dims, averaging)
    if quantity not in (Axis.AMPLITUDE, Axis.PHASE):
        raise ValueError(
            f"reduce_amp_phase_baselines: unsupported quantity {quantity}")
    if averaging not in ("scalar", "vector"):
        raise ValueError(
            f"averaging must be 'scalar' or 'vector'; got {averaging!r}")
    inner = [d for d in reduce_dims if d != BASELINE_DIM]

    good = ~flag
    re = vis.real.where(good)
    im = vis.imag.where(good)

    if quantity == Axis.AMPLITUDE:
        per = (reduce_amp_phase(vis, flag, Axis.AMPLITUDE, inner, averaging)
               if inner else np.hypot(re, im))
        if baseline_combine == "max":
            return per.max(dim=BASELINE_DIM, skipna=True)
        if baseline_combine == "median":
            return _median_over_baselines(per)
        return per.mean(dim=BASELINE_DIM, skipna=True)

    # Phase: each baseline's direction, then the mean direction of the
    # baselines, every baseline counted equally.  ("max" and "median"
    # have no meaning for a direction and are combined the same way.)
    if inner:
        if averaging == "vector":
            a = re.mean(dim=inner, skipna=True)
            b = im.mean(dim=inner, skipna=True)
        else:
            amp = np.hypot(re, im)
            amp = amp.where(amp > 0)
            a = (re / amp).mean(dim=inner, skipna=True)
            b = (im / amp).mean(dim=inner, skipna=True)
    else:
        a, b = re, im
    mag = np.hypot(a, b)
    mag = mag.where(mag > 0)
    ua = (a / mag).mean(dim=BASELINE_DIM, skipna=True)
    ub = (b / mag).mean(dim=BASELINE_DIM, skipna=True)
    return np.arctan2(ub, ua) * _RAD2DEG


def reduce_plain_baselines(
    q: xr.DataArray,
    quantity: Axis,
    reduce_dims: Sequence[str],
    baseline_combine: str = "mean",
) -> xr.DataArray:
    """Reduce a per-sample quantity *q* (Real, Imaginary, Amp V Diff,
    Phase Diff) over *reduce_dims*.

    The mean over everything, as it always was -- except that with
    ``baseline_combine="max"`` or ``"median"`` the magnitudes in
    ``MAX_QUANTITIES`` are averaged within each baseline and the largest
    (or middle) baseline is shown.
    """
    reduce_dims = list(reduce_dims)
    if (baseline_combine in ("max", "median") and quantity in MAX_QUANTITIES
            and BASELINE_DIM in reduce_dims):
        inner = [d for d in reduce_dims if d != BASELINE_DIM]
        per = q.mean(dim=inner, skipna=True) if inner else q
        if baseline_combine == "median":
            return _median_over_baselines(per)
        return per.max(dim=BASELINE_DIM, skipna=True)
    return q.mean(dim=reduce_dims, skipna=True)
