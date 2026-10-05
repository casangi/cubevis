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

``"scalar"``
    Amplitude: mean of ``|V|`` (unchanged from before).
    Phase: circular mean -- the direction of the mean *unit* phasor, so
    every unflagged sample counts equally whatever its amplitude.

``"vector"``
    Average the complex visibility, then take amplitude / phase.
    Amplitude falls where the samples are incoherent (noise, residual
    delay across the averaged channels, residual rate across the
    averaged times).  This is the average AIPS and plotms users expect.

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
    averaging: str = "scalar",
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
