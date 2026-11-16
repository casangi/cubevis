"""Write a tree as a CASA calibration table, with arcae alone.

``write_caltab(tree, path)`` makes a table that ``open_caltab`` reads back
as the same tree, and that casatools opens: CASA's columns, keywords,
``table.info`` and subtables.  It is here for tests -- a table on disk
without CASA, for the reader and for flag commits -- and it is the start
of a path from a calibration set back to CASA.

The subtables are valid MeasurementSet subtables (CASA refuses a cal
table otherwise), but they hold only what a tree knows -- names and
frequencies: antenna positions and field directions are zero.

No Bokeh, no CASA.  arcae is imported when a table is written.
"""
from __future__ import annotations

import os
import shutil
from typing import Any, Optional

import numpy as np

from .. import _arcae_util as au
from ._build import RawCalTable, tree_to_raw


def _column(value_type: str, array: bool = False, keywords: Optional[dict] = None) -> dict[str, Any]:
    """A column description as casacore records it (what arcae's
    ``tabledesc`` returns for CASA's own cal tables)."""
    desc = {"valueType": value_type, "dataManagerType": "StandardStMan", "dataManagerGroup": "MSMTAB",
            "option": 0 if array else 5, "maxlen": 0, "comment": "", "keywords": keywords or {}}
    if array:
        desc["ndim"] = -1                                            # any shape, cell by cell
    return desc


def _main_desc(pcol: str) -> dict[str, Any]:
    return {
        "TIME": _column("double", keywords={"QuantumUnits": ["s"],
                                            "MEASINFO": {"type": "epoch", "Ref": "UTC"}}),
        "FIELD_ID": _column("int"), "SPECTRAL_WINDOW_ID": _column("int"),
        "ANTENNA1": _column("int"), "ANTENNA2": _column("int"),
        "INTERVAL": _column("double", keywords={"QuantumUnits": ["s"]}),
        "SCAN_NUMBER": _column("int"), "OBSERVATION_ID": _column("int"),
        pcol: _column("complex" if pcol == "CPARAM" else "float", array=True),
        "PARAMERR": _column("float", array=True), "FLAG": _column("boolean", array=True),
        "SNR": _column("float", array=True),
        "WEIGHT": _column("float", array=True),                      # no cells, as CASA leaves it
    }


def _table_info(path: str, type_: str, subtype: str = "") -> None:
    with open(os.path.join(path, "table.info"), "w") as f:
        f.write(f"Type = {type_}\nSubType = {subtype}\n\n")


def _subtable(parent: str, name: str, columns: dict[str, Any], nrow: int) -> str:
    """The subtable *name* with the MeasurementSet's required columns (CASA
    refuses a cal table whose subtables are not valid MS subtables) and
    *columns* filled in: an array for a whole column, a list for a cell at
    a time."""
    from arcae.lib.arrow_tables import Table, ms_descriptor
    path = os.path.join(parent, name)
    t = Table.from_descriptor(path, table_desc=ms_descriptor(name, complete=False), nrow=nrow)
    try:
        for col, values in columns.items():
            if isinstance(values, list):                             # a cell at a time: shapes differ
                for row, cell in enumerate(values):
                    t.putcol(col, np.asarray(cell)[None], index=(np.array([row]),))
            elif nrow:
                t.putcol(col, values)
    finally:
        t.close()
    return path


def _strings(values) -> np.ndarray:
    return np.array([str(v) for v in values], dtype=object)


def write_caltab(tree, path: str, *, overwrite: bool = False) -> str:
    """Write *tree* (an ``open_caltab`` or ``synthetic_caltab`` tree, or a
    ``RawCalTable``) as a CASA calibration table at *path*; returns the
    absolute path.

    Read back, the table gives the same tree, apart from ``source`` and
    ``SOURCE_ROW``'s numbering when rows were left out.  The antennas and
    fields are those of the tree, numbered in its order (see
    ``_build.tree_to_raw``).
    """
    from arcae.lib.arrow_tables import Table

    raw = tree if isinstance(tree, RawCalTable) else tree_to_raw(tree)
    path = os.path.abspath(os.path.expanduser(str(path)))
    if os.path.exists(path):
        if not overwrite:
            raise FileExistsError(f"{path} exists (overwrite=True replaces it)")
        if not (au.is_table(path) or not os.listdir(path)):
            raise FileExistsError(f"{path} exists and is not a CASA table; not replaced")
        shutil.rmtree(path)

    partype = raw.keywords.get("ParType") or (
        "Complex" if np.iscomplexobj(next(iter(raw.cells.values())).par) else "Float")
    pcol = "CPARAM" if partype == "Complex" else "FPARAM"
    nrow = len(raw.main["TIME"])
    t = Table.from_descriptor(path, table_desc=_main_desc(pcol), nrow=nrow)
    try:
        for col in ("TIME", "INTERVAL"):
            t.putcol(col, np.asarray(raw.main[col], dtype=np.float64))
        for col in ("FIELD_ID", "SPECTRAL_WINDOW_ID", "ANTENNA1", "ANTENNA2", "SCAN_NUMBER",
                    "OBSERVATION_ID"):
            t.putcol(col, np.asarray(raw.main[col], dtype=np.int32))
        for c in raw.cells.values():                                 # a window at a time: shapes differ
            index = (np.asarray(c.rows, dtype=np.int64),)
            t.putcol(pcol, np.asarray(c.par, dtype=np.complex64 if pcol == "CPARAM" else np.float32),
                     index=index)
            t.putcol("PARAMERR", np.asarray(c.err, dtype=np.float32), index=index)
            t.putcol("SNR", np.asarray(c.snr, dtype=np.float32), index=index)
            t.putcol("FLAG", np.asarray(c.flag).astype(np.uint8), index=index)

        nspw = max(max(raw.cells) + 1, len(raw.spw_names))
        chan_freq = [np.asarray(raw.cells[s].chan_freq, dtype=np.float64) if s in raw.cells
                     else np.zeros(1) for s in range(nspw)]
        # a channel's width: the tree's, or else the spacing
        width = [np.asarray(raw.cells[s].chan_width, dtype=np.float64)
                 if s in raw.cells and raw.cells[s].chan_width is not None
                 else np.full(len(f), abs(f[1] - f[0]) if len(f) > 1 else 1.0)
                 for s, f in enumerate(chan_freq)]
        nant, nfield = len(raw.antenna_names), len(raw.field_names)
        subtables = {
            "ANTENNA": _subtable(path, "ANTENNA", {
                "NAME": _strings(raw.antenna_names), "STATION": _strings(raw.antenna_names),
                "TYPE": _strings(["GROUND-BASED"] * nant), "MOUNT": _strings(["ALT-AZ"] * nant),
                "POSITION": np.zeros((nant, 3)), "OFFSET": np.zeros((nant, 3)),
                "DISH_DIAMETER": np.full(nant, 25.0)}, nant),
            "FIELD": _subtable(path, "FIELD", {
                "NAME": _strings(raw.field_names), "CODE": _strings([""] * nfield),
                "NUM_POLY": np.zeros(nfield, dtype=np.int32),
                "SOURCE_ID": np.full(nfield, -1, dtype=np.int32),
                **{c: np.zeros((nfield, 1, 2)) for c in ("DELAY_DIR", "PHASE_DIR", "REFERENCE_DIR")}},
                nfield),
            "SPECTRAL_WINDOW": _subtable(path, "SPECTRAL_WINDOW", {
                "NAME": _strings((list(raw.spw_names) + [""] * nspw)[:nspw]),
                "NUM_CHAN": np.array([len(f) for f in chan_freq], dtype=np.int32),
                "REF_FREQUENCY": np.array([f[0] for f in chan_freq], dtype=np.float64),
                "TOTAL_BANDWIDTH": np.array([np.abs(w).sum() for w in width], dtype=np.float64),
                "MEAS_FREQ_REF": np.full(nspw, 5, dtype=np.int32),          # TOPO
                "FREQ_GROUP_NAME": _strings([""] * nspw),
                "CHAN_FREQ": chan_freq, "CHAN_WIDTH": width, "EFFECTIVE_BW": width,
                "RESOLUTION": width}, nspw),
            "OBSERVATION": _subtable(path, "OBSERVATION", {
                "TELESCOPE_NAME": _strings([str(raw.keywords.get("telescope", "synthetic"))]),
                "OBSERVER": _strings([""]), "PROJECT": _strings([""]), "SCHEDULE_TYPE": _strings([""]),
                "TIME_RANGE": np.array([[raw.main["TIME"].min(), raw.main["TIME"].max()]])}, 1),
            "HISTORY": _subtable(path, "HISTORY", {}, 0),
        }
        keywords = {"ParType": partype, "MSName": str(raw.keywords.get("MSName", "")),
                    "VisCal": raw.subtype, "PolBasis": str(raw.keywords.get("PolBasis", "unknown")),
                    "CASA_Version": str(raw.keywords.get("CASA_Version", ""))}
        keywords.update({name: f"Table: {sub}" for name, sub in subtables.items()})
        t.putkeywords(keywords)
    finally:
        t.close()
    _table_info(path, "Calibration", raw.subtype)
    for name in subtables:
        _table_info(os.path.join(path, name), "", "")
    return path
