"""Read a CASA calibration table into an ``xarray.DataTree`` with arcae.

One node per spectral window, ``spw_<id>``, each an xarray Dataset in the
shape of xradio's draft calibration schema.  This module reads the rows
(``read_raw``); ``_build.build_tree`` makes the tree of them, as it does
for every other source; the names, dimensions and units are ``_schema``'s.

A node's ``time`` is the union of that window's solution times, and its
``antenna_name`` every antenna with a solution anywhere in the table.  A
time and antenna with no row is NaN with ``FLAG`` set and ``SOURCE_ROW``
-1, so "no solution" (``SOURCE_ROW < 0``) and "flagged" can be told apart.

Everything is read at once (cal tables are small).  No Bokeh, no CASA.
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Optional

import numpy as np

from .. import _arcae_util as au
from ._build import MAIN_COLUMNS, RawCalTable, SpwCells, build_tree


class NotACalTable(ValueError):
    """The path is not a CASA calibration table."""


def caltab_info(path: str) -> dict[str, Any]:
    """What kind of table *path* is, without reading its data.

    ``{"path", "type", "subType", "keywords", "subtables", "nrow",
    "columns"}``.  Raises ``NotACalTable`` unless ``table.info`` says
    ``Calibration``.
    """
    path = os.path.abspath(os.path.expanduser(str(path))).rstrip(os.sep)
    info = au.table_info(path)
    if not au.is_table(path) or info.get("type") != "Calibration":
        raise NotACalTable(f"{path}: not a CASA calibration table "
                           f"(table.info type is {info.get('type')!r})")
    t = au.open_table(path)
    try:
        plain, subtables = au.keywords(t)
        return {"path": path, "type": info["type"], "subType": info.get("subType") or plain.get("VisCal"),
                "keywords": plain, "subtables": subtables, "nrow": int(t.nrow()),
                "columns": list(t.columns())}
    finally:
        t.close()


def _names(path: str, subtable: str) -> list[str]:
    t = au.open_subtable(path, subtable)
    try:
        return [str(n) for n in t.getcol("NAME")] if t.nrow() else []
    finally:
        t.close()


def read_raw(path: str, *, spw: Optional[Iterable[int]] = None) -> RawCalTable:
    """The rows of the cal table at *path* (every window, or those of *spw*)."""
    info = caltab_info(path)
    path = info["path"]
    pcol = "CPARAM" if "CPARAM" in info["columns"] else "FPARAM"
    if pcol not in info["columns"]:
        raise NotACalTable(f"{path}: no CPARAM or FPARAM column")
    if info["nrow"] == 0:
        raise ValueError(f"{path}: the calibration table has no rows")

    t = au.open_table(path)
    try:
        main = {c: np.asarray(t.getcol(c)) for c in MAIN_COLUMNS}
        raw = RawCalTable(source=path, subtype=info["subType"] or "", keywords=info["keywords"],
                          antenna_names=_names(path, "ANTENNA"), field_names=_names(path, "FIELD"),
                          spw_names=_names(path, "SPECTRAL_WINDOW"), main=main,
                          time_scale=au.epoch_scale(t) or "utc")
        present = [int(s) for s in np.unique(main["SPECTRAL_WINDOW_ID"])]
        if spw is None:
            ids = present
        else:
            ids = [int(s) for s in ([spw] if np.isscalar(spw) else spw)]
            missing = [s for s in ids if s not in present]
            if missing:
                raise ValueError(f"spectral window(s) {missing} have no solutions; the table has {present}")
        spw_tab = au.open_subtable(path, "SPECTRAL_WINDOW")
        try:
            has_width = "CHAN_WIDTH" in spw_tab.columns()
            for s in ids:
                # a window's rows on their own: channel counts may differ by window
                rows = np.flatnonzero(main["SPECTRAL_WINDOW_ID"] == s)
                raw.cells[s] = SpwCells(
                    rows=rows,
                    par=au.getcol_rows(t, pcol, rows),               # (row, chan, par)
                    err=au.getcol_rows(t, "PARAMERR", rows),
                    snr=au.getcol_rows(t, "SNR", rows),
                    flag=au.as_bool(au.getcol_rows(t, "FLAG", rows)),
                    chan_freq=au.getcol_rows(spw_tab, "CHAN_FREQ", [s])[0].astype(np.float64),
                    chan_width=(au.getcol_rows(spw_tab, "CHAN_WIDTH", [s])[0].astype(np.float64)
                                if has_width else None))
        finally:
            spw_tab.close()
    finally:
        t.close()
    return raw


def open_caltab(path: str, *, spw: Optional[Iterable[int]] = None,
                receptor_labels: Optional[list[str]] = None):
    """A CASA calibration table as an ``xarray.DataTree``.

    Parameters
    ----------
    path
        The table.
    spw
        Spectral window id(s) to read; default: every window with rows.
    receptor_labels
        Labels for the receptor axis, when the caller knows them (the
        table's ``PolBasis`` keyword is ``unknown`` in CASA 6.7's tables;
        without it the labels are ``pol_0``, ``pol_1``).
    """
    return build_tree(read_raw(path, spw=spw), receptor_labels=receptor_labels)
