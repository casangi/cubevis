"""Antenna x antenna raster (HRS H7, 2026-10-11).

AIPS LISTR's 'MATX' view: one cell per baseline, the quantity reduced over
time and frequency, laid out as a matrix of antennas.  The matrix is
mirrored (cell (A, B) and cell (B, A) hold the same baseline), so an
antenna that is bad on every baseline shows as a whole row AND column --
the cross that makes it stand out.  The diagonal holds the
autocorrelations when the data have them.

The backends reduce each partition over every dimension but the baseline
(``_raster_2d`` with both raster dimensions set to the baseline), and
this module turns that per-baseline vector into the matrix.  Partitions
(spectral windows, scan intents) then combine cell by cell like any
other raster (``_raster_merge``), and ``compact`` drops the antennas
with no data.

The matrix axes are positions 0 .. N-1; ``attrs["antenna_labels"]`` names
them (the raster's tick labels and readout).
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np
import xarray as xr

#: Raster dimension names of the two antenna axes.
ANTENNA_DIMS = {"ANTENNA1": "antenna1", "ANTENNA2": "antenna2"}
LABELS_ATTR = "antenna_labels"


def _name(axis) -> str:
    return getattr(axis, "name", str(axis))


def is_antenna_matrix(y_dim, x_dim) -> bool:
    """Both raster axes are antenna axes (one each)."""
    return {_name(y_dim), _name(x_dim)} == set(ANTENNA_DIMS)


def check_axes(y_dim, x_dim) -> None:
    """An antenna axis only pairs with the other one: anything else
    (Antenna 1 x Time ...) would need a rule for which baselines go in a
    row, which nothing asks for yet."""
    names = {_name(y_dim), _name(x_dim)}
    if names & set(ANTENNA_DIMS) and names != set(ANTENNA_DIMS):
        raise NotImplementedError(
            "Antenna 1 goes with Antenna 2: together they make the antenna "
            "× antenna matrix (each baseline averaged over time and "
            "frequency). Pair Time, Channel or Baseline with each other.")


def to_matrix(vec: xr.DataArray, ds: xr.Dataset, antennas: Sequence[str],
              y_name: str, x_name: str, bdim: str = "baseline_id") -> Optional[xr.DataArray]:
    """The per-baseline values *vec* (dim *bdim*) of partition *ds* as a
    mirrored N x N antenna matrix, N = ``len(antennas)``."""
    if bdim not in vec.dims or "baseline_antenna1_name" not in ds.coords:
        return None
    bl = np.asarray(ds.coords[bdim].values)
    a1 = np.asarray(ds.coords["baseline_antenna1_name"].values).astype(str)
    a2 = np.asarray(ds.coords["baseline_antenna2_name"].values).astype(str)
    pos = {b: k for k, b in enumerate(bl.tolist())}
    idx = {str(a): k for k, a in enumerate(antennas)}
    n = len(antennas)
    out = np.full((n, n), np.nan)
    vals = np.asarray(vec.values, dtype=np.float64)
    for b, v in zip(np.asarray(vec.coords[bdim].values).tolist(), vals.tolist()):
        k = pos.get(b)
        if k is None:
            continue
        i, j = idx.get(a1[k]), idx.get(a2[k])
        if i is None or j is None:
            continue
        out[i, j] = v
        out[j, i] = v
    coord = np.arange(n, dtype=np.float64)
    return xr.DataArray(out, dims=(y_name, x_name),
                        coords={y_name: coord, x_name: coord},
                        attrs=dict(vec.attrs))


def compact(agg: xr.DataArray, antennas: Sequence[str]) -> xr.DataArray:
    """Leave out antennas with no value in their row or column; number the
    rest 0 .. K-1 and name them in ``attrs["antenna_labels"]``."""
    y_name, x_name = agg.dims
    vals = np.asarray(agg.values, dtype=np.float64)
    has = np.isfinite(vals).any(axis=0) | np.isfinite(vals).any(axis=1)
    keep_pos = np.asarray(agg.coords[y_name].values)[has].astype(int)
    keep = np.flatnonzero(has)
    if keep.size == 0:
        keep = np.arange(min(1, vals.shape[0]))
        keep_pos = keep
    sub = vals[np.ix_(keep, keep)]
    coord = np.arange(keep.size, dtype=np.float64)
    labels = [str(antennas[p]) if 0 <= p < len(antennas) else str(p) for p in keep_pos]
    return xr.DataArray(sub, dims=(y_name, x_name),
                        coords={y_name: coord, x_name: coord},
                        attrs={**dict(agg.attrs), LABELS_ATTR: labels})


def labels_of(agg) -> Optional[list]:
    """The antenna names of a compacted matrix, or ``None``."""
    try:
        lab = agg.attrs.get(LABELS_ATTR)
        return list(lab) if lab is not None else None
    except Exception:
        return None
