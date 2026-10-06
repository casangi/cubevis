"""Collect the list of baselines in a data set, for metadata().

Why this exists
---------------
``SelectionSpec.baselines`` selects exact ``(antenna1, antenna2)`` pairs,
and both backends match them as *ordered* pairs against the data's own
``baseline_antenna1_name`` / ``baseline_antenna2_name`` coordinates.  A
GUI that offers baselines for selection therefore needs the pairs exactly
as the data spell them, in the data's orientation -- deriving them from
the antenna list would guess the orientation and offer pairs with no
data.  Both backends call this from ``metadata()`` so the list reaches
``ObservationMetadata.baselines`` the same way on either format, and
over the remote path (it is plain lists in the metadata dict).

Added 2026-10 for the sidebar's Baseline selection table (HRS H3).
"""
from __future__ import annotations

import numpy as np
import xarray as xr

_BL_DIM = "baseline_id"
_ANT1 = "baseline_antenna1_name"
_ANT2 = "baseline_antenna2_name"


def collect_baselines(ds: xr.Dataset, target: dict) -> None:
    """Add this partition's baselines to *target*.

    *target* maps ``(antenna1_name, antenna2_name)`` to the baseline's id
    -- the value of the ``baseline_id`` coordinate, which is the number
    the Baseline axis of a raster shows, or the position along that
    dimension when it carries no coordinate.  The first id seen for a
    pair is kept: partitions of one data set share a baseline numbering
    (the raster merge already relies on that).

    Does nothing for a partition without baseline name coordinates
    (single-dish data, metadata sub-tables).  Reads coordinates only.
    """
    if _ANT1 not in ds.coords or _ANT2 not in ds.coords:
        return
    a1 = np.asarray(ds.coords[_ANT1].values).ravel()
    a2 = np.asarray(ds.coords[_ANT2].values).ravel()
    if a1.size != a2.size:
        return
    if _BL_DIM in ds.coords:
        ids = np.asarray(ds.coords[_BL_DIM].values).ravel()
    else:
        ids = np.arange(a1.size)
    if ids.size != a1.size:
        ids = np.arange(a1.size)
    for i, n1, n2 in zip(ids, a1, a2):
        # Padded slots can carry empty names; they are not baselines.
        if n1 is None or n2 is None:
            continue
        s1, s2 = str(n1), str(n2)
        if not s1 or not s2 or s1 == "nan" or s2 == "nan":
            continue
        try:
            bid = int(i)
        except (TypeError, ValueError):
            bid = len(target)
        target.setdefault((s1, s2), bid)


def baselines_to_meta(target: dict) -> list:
    """*target* as the metadata dict's ``"baselines"`` value:
    ``[[baseline_id, antenna1_name, antenna2_name], ...]`` in id order."""
    return [[bid, a1, a2]
            for (a1, a2), bid in sorted(target.items(),
                                        key=lambda kv: (kv[1], kv[0]))]
