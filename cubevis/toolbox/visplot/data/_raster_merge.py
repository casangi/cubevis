"""Merge per-partition 2D raster arrays into one grid.

Why this exists
---------------
``query_raster()`` reduces each partition (one per SPW/DDID on MSv2; one
per intent/SPW on MSv4) to its own 2D ``(y, x)`` array and then has to
combine them.  It used to do::

    xr.concat(parts, dim=y_name, join="outer", ...)

which stacks the partitions along *y*.  That is only right when the
partitions are disjoint along *y*.  Partitions are routinely disjoint
along *x* instead, or share both axes' members, and then ``concat``
produces wrong grids:

* Baseline (y) x Time (x) -- the default vplot layout -- over partitions
  that are disjoint in time but observe the same baselines (one per
  scan/intent): every baseline appeared once *per partition*.  On
  sis14_twhya (26 antennas) that is 650 rows instead of 325, each
  baseline listed twice with its data split between the copies.
* Time (y) x Channel/Frequency (x) on a multi-SPW MS: every SPW has the
  same times but its own frequencies, so the SPWs were stacked as extra
  *time* rows (N_spw x N_time rows, each NaN outside its own channels).
  That produced SPWs offset from one another in time, ~(N_spw-1)/N_spw of
  the grid NaN (black gaps), and an inflated row count that tripped
  ``_decimate_agg``, which then strided away whole rows -- data only
  every few seconds.

(Time (y) x Baseline (x) happened to work, because there the partitions
really are disjoint along y.)

The merge here is placement by coordinate on the union of *both* axes,
so it is correct regardless of which axis (or both) the partitions
differ on.  Where two partitions populate the same cell, the first
non-NaN value wins (``combine_first`` semantics).
"""
from __future__ import annotations

from typing import Sequence

import numpy as np
import xarray as xr


def merge_raster_partitions(
    parts: Sequence[xr.DataArray], y_name: str, x_name: str,
) -> xr.DataArray:
    """Merge 2D ``(y_name, x_name)`` partitions on the union of both axes.

    Parameters
    ----------
    parts :
        Computed 2D arrays, each with 1-D coordinates on ``y_name`` and
        ``x_name`` (any other coordinates must already be stripped, see
        ``_drop_non_raster_coords``).
    y_name, x_name :
        The two dimension names.

    Returns
    -------
    xr.DataArray
        Dims ``(y_name, x_name)``, coordinates sorted ascending and
        unique, cells no partition covers left as NaN.  ``attrs`` come
        from the first partition (callers override what they need).
    """
    if len(parts) == 1:
        return parts[0]

    y_union = np.unique(np.concatenate(
        [np.asarray(p.coords[y_name].values) for p in parts]))
    x_union = np.unique(np.concatenate(
        [np.asarray(p.coords[x_name].values) for p in parts]))

    dtype = np.result_type(*[p.dtype for p in parts])
    if not np.issubdtype(dtype, np.floating):
        dtype = np.float64
    out = np.full((y_union.size, x_union.size), np.nan, dtype=dtype)

    for p in parts:
        p = p.transpose(y_name, x_name)
        yi = np.searchsorted(y_union, np.asarray(p.coords[y_name].values))
        xi = np.searchsorted(x_union, np.asarray(p.coords[x_name].values))
        vals = np.asarray(p.values, dtype=dtype)
        block = out[np.ix_(yi, xi)]
        # First non-NaN wins: only fill cells still empty.
        fill = np.isnan(block) & ~np.isnan(vals)
        block[fill] = vals[fill]
        out[np.ix_(yi, xi)] = block

    return xr.DataArray(
        out, dims=(y_name, x_name),
        coords={y_name: y_union, x_name: x_union},
        attrs=dict(parts[0].attrs),
    )
