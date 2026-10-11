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
differ on.

Cells several partitions cover (2026-10-11)
-------------------------------------------
Until 2026-10-11 the first non-NaN value won.  That is wrong wherever
partitions overlap in a cell rather than tile the grid: Baseline x Time
over several spectral windows (same baselines, same times, each SPW its
own channels) showed the first SPW only; an Antenna x Antenna matrix
(HRS H7), reduced over time and frequency, would show the first
partition (one SPW, one scan intent) only.  Such cells are now combined
by ``rule`` (see ``combine_rule``), weighted by the number of samples
each partition reduced there:

* ``"wmean"``: weighted mean -- exact for Real, Imaginary and the flagged
  fraction; for scalar Amplitude, the DIFF quantities, Phase RMS and
  Coherence it is the mean of the per-partition values;
* ``"circmean"``: weighted mean direction, for Phase (degrees);
* ``"max"``: the largest (Z-Score; "Baselines combined: Maximum");
* ``"median"``: the median of the per-partition values;
* ``"first"``: the old behaviour (callers that do not say).

Partitions that tile the grid (no shared cell) are placed as before;
weights are only computed when a shared cell exists.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np
import xarray as xr


#: Above this many partition x cell values, "median" falls back to "wmean"
#: rather than stack every partition's grid in memory.
_MEDIAN_STACK_MAX = 20_000_000


def combine_rule(quantity, baseline_combine: str = "mean") -> str:
    """How cells several partitions cover are combined for *quantity*
    (an ``Axis``, or its name) under "Baselines combined" *baseline_combine*.

    The same choice the user made for baselines applies across spectral
    windows and scans: averaging within each partition, then combining.
    "coherent" combines by weighted mean (a vector sum across SPWs would
    need the complex sums, which the partitions do not return)."""
    name = getattr(quantity, "name", str(quantity))
    if name == "Z_SCORE":
        return "max"
    if name == "PHASE":
        return "circmean"
    if name in ("AMPLITUDE", "AMP_VDIFF", "PHASE_DIFF"):
        if baseline_combine == "max":
            return "max"
        if baseline_combine == "median" and name == "AMPLITUDE":
            return "median"
    return "wmean"


def merge_raster_partitions(
    parts: Sequence[xr.DataArray], y_name: str, x_name: str,
    rule: str = "first", weights=None,
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
    rule :
        How cells covered by more than one partition are combined (module
        docstring).  ``"first"``: the first non-NaN value.
    weights :
        For ``"wmean"`` / ``"circmean"``: one entry per partition, each a
        2D array of sample counts on that partition's own coordinates (an
        ``xr.DataArray``) or a callable returning one -- called only when
        a cell is actually shared.  ``None``: equal weights.

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
    shape = (y_union.size, x_union.size)

    placed = []
    cover = np.zeros(shape, dtype=np.int32)
    for p in parts:
        p = p.transpose(y_name, x_name)
        yi = np.searchsorted(y_union, np.asarray(p.coords[y_name].values))
        xi = np.searchsorted(x_union, np.asarray(p.coords[x_name].values))
        vals = np.asarray(p.values, dtype=np.float64)
        placed.append((yi, xi, vals))
        cover[np.ix_(yi, xi)] += ~np.isnan(vals)

    def _wrap(out):
        return xr.DataArray(
            out.astype(dtype, copy=False), dims=(y_name, x_name),
            coords={y_name: y_union, x_name: x_union},
            attrs=dict(parts[0].attrs),
        )

    shared = bool((cover > 1).any())
    if rule == "first" or not shared:
        out = np.full(shape, np.nan)
        for yi, xi, vals in placed:
            block = out[np.ix_(yi, xi)]
            # First non-NaN wins: only fill cells still empty.
            fill = np.isnan(block) & ~np.isnan(vals)
            block[fill] = vals[fill]
            out[np.ix_(yi, xi)] = block
        return _wrap(out)

    if rule == "median" and len(parts) * cover.size > _MEDIAN_STACK_MAX:
        rule = "wmean"
    if rule == "max":
        out = np.full(shape, -np.inf)
        for yi, xi, vals in placed:
            block = out[np.ix_(yi, xi)]
            out[np.ix_(yi, xi)] = np.fmax(block, vals)
        out[cover == 0] = np.nan
        return _wrap(out)
    if rule == "median":
        stack = np.full((len(placed),) + shape, np.nan)
        for k, (yi, xi, vals) in enumerate(placed):
            stack[k][np.ix_(yi, xi)] = vals
        with np.errstate(all="ignore"):
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                return _wrap(np.nanmedian(stack, axis=0))

    # weighted: "wmean" / "circmean"
    sw = np.zeros(shape)
    acc = np.zeros(shape, dtype=np.complex128 if rule == "circmean" else np.float64)
    for k, (yi, xi, vals) in enumerate(placed):
        w = None
        if weights is not None and k < len(weights) and weights[k] is not None:
            wk = weights[k]() if callable(weights[k]) else weights[k]
            w = _aligned_weights(wk, parts[k], y_name, x_name)
        if w is None:
            w = np.ones_like(vals)
        ok = ~np.isnan(vals) & np.isfinite(w) & (w > 0)
        wv = np.where(ok, w, 0.0)
        if rule == "circmean":
            term = wv * np.exp(1j * np.radians(np.where(ok, vals, 0.0)))
        else:
            term = wv * np.where(ok, vals, 0.0)
        sw[np.ix_(yi, xi)] += wv
        acc[np.ix_(yi, xi)] += term
    with np.errstate(invalid="ignore", divide="ignore"):
        if rule == "circmean":
            out = np.degrees(np.angle(acc))
        else:
            out = acc / sw
    out[sw <= 0] = np.nan
    return _wrap(out)


def _aligned_weights(w, part, y_name, x_name):
    """Weights *w* as a numpy array on *part*'s own (y, x) cells (the
    partition may have been decimated after its weights were made)."""
    try:
        if isinstance(w, xr.DataArray):
            w = w.transpose(y_name, x_name)
            w = w.sel({y_name: part.coords[y_name].values,
                       x_name: part.coords[x_name].values})
            return np.asarray(w.values, dtype=np.float64)
        w = np.asarray(w, dtype=np.float64)
        return w if w.shape == part.shape else None
    except Exception:
        return None


def coord_extent(values) -> tuple:
    """``(lo, hi)`` of a raster axis coordinate, as floats.

    Numeric coordinates: their min and max.  Anything else (the
    Correlation axis is strings -- "XX", "YY"): positions, 0 .. n-1.
    ``query_raster`` used to call ``float(values.min())`` directly, which
    raises for strings ("ufunc 'minimum' did not contain a loop..."), so
    a raster with Correlation on an axis failed outright (2026-10-06).
    """
    arr = np.asarray(values)
    if arr.size == 0:
        return (0.0, 0.0)
    if np.issubdtype(arr.dtype, np.number) or np.issubdtype(arr.dtype, np.bool_):
        return (float(arr.min()), float(arr.max()))
    if np.issubdtype(arr.dtype, np.datetime64):
        sec = arr.astype("datetime64[ns]").astype(np.int64) / 1e9
        return (float(sec.min()), float(sec.max()))
    return (0.0, float(arr.size - 1))
