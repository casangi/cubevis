"""What the arcae-based backends share: a table's type, keywords and subtables.

No Bokeh, no CASA.  arcae is imported inside the functions, so importing
this module costs nothing.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import numpy as np

# CASA's epochs are seconds from MJD 0; xradio's are Unix seconds.
# MJD 40587 is 1970-01-01, so the shift is a whole number: exact in float64.
MJD_UNIX_OFFSET_S = 40587 * 86400.0


def table_info(path: str) -> dict[str, str]:
    """The ``Type`` and ``SubType`` of ``path/table.info`` (casacore's text
    file), as ``{"type": ..., "subType": ...}``; ``{}`` if there is none."""
    info: dict[str, str] = {}
    try:
        with open(os.path.join(path, "table.info"), "r", errors="replace") as f:
            for line in f:
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                key = key.strip().lower()
                if key == "type":
                    info["type"] = value.strip()
                elif key == "subtype":
                    info["subType"] = value.strip()
    except OSError:
        pass
    return info


def is_table(path: Any) -> bool:
    """Is *path* a casacore table directory?"""
    try:
        return os.path.isdir(path) and os.path.isfile(os.path.join(path, "table.dat"))
    except TypeError:
        return False


def open_table(path: str, readonly: bool = True):
    """Open a table (or ``path::SUBTABLE``) with one arcae instance.

    One instance is what a write needs, and enough for the small tables
    read here.  Close it with ``.close()``.
    """
    from arcae.lib.arrow_tables import Table
    return Table.from_filename(path, ninstances=1, readonly=readonly)


def open_subtable(path: str, name: str, readonly: bool = True):
    """Open the subtable *name* of the table at *path*."""
    return open_table(f"{path}::{name}", readonly=readonly)


def keywords(table) -> tuple[dict[str, Any], list[str]]:
    """A table's keywords, split into plain values and subtable names.

    casacore stores a subtable as a keyword whose value is
    ``"Table: <path>"``.  The path is where the table was written, so it is
    not kept: a subtable is opened by name (``open_subtable``).
    """
    plain: dict[str, Any] = {}
    subtables: list[str] = []
    for key, value in table.getkeywords().items():
        if isinstance(value, str) and value.startswith("Table: "):
            subtables.append(key)
        else:
            plain[key] = value
    return plain, subtables


def epoch_scale(table, column: str = "TIME") -> Optional[str]:
    """The time scale of an epoch column in xradio's spelling (``"utc"``),
    or None when the column does not say."""
    try:
        ref = table.getcolkeywords(column).get("MEASINFO", {}).get("Ref")
    except Exception:
        return None
    return str(ref).lower() if ref else None


def as_bool(values) -> np.ndarray:
    """arcae returns a Bool column as ``uint8``; ``~`` on that gives 254 and
    255, not False and True.  Every flag column goes through here."""
    return np.asarray(values).astype(bool)


def getcol_rows(table, column: str, rows: np.ndarray) -> np.ndarray:
    """*column* for the given rows.

    Reading by row is what lets a column whose cells differ in shape from
    one group of rows to the next (a B table over spectral windows with
    different channel counts) be read one group at a time.
    """
    rows = np.asarray(rows, dtype=np.int64)
    return np.asarray(table.getcol(column, index=(rows,)))
