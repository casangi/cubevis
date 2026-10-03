"""Disambiguate spectral windows whose NAMEs collide.

The problem
-----------
xarray-ms exposes no numeric SPW id on a partition, only the window
*name* (``frequency.attrs["spectral_window_name"]``), so
``_partition_spw_ident`` falls back to the name as the window's
identity.  That is only valid while names are unique.  They are not:
EVLA names its subbands ``Subband:0..7`` in *each* baseband pair, so a
16-window MS has 16 partitions but 8 distinct "identities".  Everything
keyed on the identity then collapses the windows in pairs --
``metadata()`` (so the SPW table and Prev/Next list 8, not 16), SPW
selection (choosing ``Subband:0`` silently selects two windows), and the
distinct-SPW count behind the Channel axis.

The fix
-------
When -- and only when -- a name occurs on more than one row of the MS's
SPECTRAL_WINDOW subtable, identify the partition by its row number,
which *is* CASA's ``spw=`` id.  The row is found by matching
``(name, reference frequency)``: the partition carries the window's
REF_FREQUENCY, which is what tells same-named windows apart.  Windows
with unique names keep the name identity exactly as before, so ALMA-style
stores and everything selecting by name are unaffected.
"""
from __future__ import annotations

import functools
import logging
from typing import Callable, Optional

import numpy as np

log = logging.getLogger(__name__)

_REF_TOL_HZ = 1.0   # REF_FREQUENCY is stored exactly; this only absorbs float repr


def build_ambiguous_spw_map(ms_path: str) -> dict:
    """``{(name, ref_freq_hz): spw_row}`` for names that are NOT unique.

    Empty when every name is unique, when the subtable cannot be read, or
    when it lacks NAME / REF_FREQUENCY -- in all of which the plain name
    identity is the right (or only available) answer.  Never raises.
    """
    try:
        from arcae import table as _arcae_table
        t = _arcae_table(f"{ms_path}/SPECTRAL_WINDOW")
        try:
            names = [str(n) for n in t.getcol("NAME")]
            refs = np.asarray(t.getcol("REF_FREQUENCY"), dtype=float).ravel()
        finally:
            t.close()
    except Exception:
        log.debug("could not read SPECTRAL_WINDOW at %s; spw names will be "
                  "used as identities as-is", ms_path, exc_info=True)
        return {}

    if len(names) != len(refs):
        return {}

    counts: dict = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1

    out: dict = {}
    for row, (n, r) in enumerate(zip(names, refs)):
        if counts[n] > 1:
            out[(n, round(float(r) / _REF_TOL_HZ))] = row
    return out


def _partition_ref_freq(ds) -> Optional[float]:
    """The partition's window reference frequency in Hz, or ``None``."""
    freq = ds.coords.get("frequency") if hasattr(ds, "coords") else None
    attrs = getattr(freq, "attrs", None) or {}
    rf = attrs.get("reference_frequency")
    if isinstance(rf, dict):          # xarray-ms: {"attrs": {...}, "data": Hz}
        rf = rf.get("data")
    try:
        return float(rf) if rf is not None else None
    except (TypeError, ValueError):
        return None


def _disambiguated(raw_ident: Callable, ambiguous: dict, ds):
    value, kind = raw_ident(ds)
    if kind != "name":
        return value, kind
    ref = _partition_ref_freq(ds)
    if ref is None:
        return value, kind
    row = ambiguous.get((str(value), round(ref / _REF_TOL_HZ)))
    if row is None:
        return value, kind
    return int(row), "spw"


def make_disambiguating_ident(raw_ident: Callable, ambiguous: dict) -> Callable:
    """Wrap ``raw_ident(ds) -> (ident, kind)`` with duplicate-name handling.

    ``raw_ident`` is the existing pure ``_partition_spw_ident`` (kept
    untouched and independently testable).  Returns it unchanged when no
    name is ambiguous, so the common case costs nothing.  A ``partial``
    over a module-level function, not a closure, so a backend holding it
    stays picklable.
    """
    if not ambiguous:
        return raw_ident
    return functools.partial(_disambiguated, raw_ident, ambiguous)
