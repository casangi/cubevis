"""Read a CASA calibration table into an ``xarray.DataTree`` with arcae.

One node per spectral window, ``spw_<id>``, each an xarray Dataset in the
shape of xradio's draft calibration schema.  The names, dimensions and
units come from ``_schema``; this module only moves values.

A node's ``time`` is the union of that window's solution times, and its
``antenna_name`` every antenna with a solution anywhere in the table.  A
time and antenna with no row is NaN with ``FLAG`` set and ``SOURCE_ROW``
-1, so "no solution" (``SOURCE_ROW < 0``) and "flagged" can be told apart.

Everything is read at once (cal tables are small).  No Bokeh, no CASA.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Optional

import numpy as np

from .. import _arcae_util as au
from . import _schema as S

log = logging.getLogger("cubevis.xarray.caltab")

MAIN_COLUMNS = ("TIME", "FIELD_ID", "SPECTRAL_WINDOW_ID", "ANTENNA1", "ANTENNA2",
                "INTERVAL", "SCAN_NUMBER", "OBSERVATION_ID")


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


def _spw_ids(spw, select: Optional[Iterable[int]]) -> list[int]:
    present = [int(s) for s in np.unique(spw)]
    if select is None:
        return present
    wanted = [int(s) for s in ([select] if np.isscalar(select) else select)]
    missing = [s for s in wanted if s not in present]
    if missing:
        raise ValueError(f"spectral window(s) {missing} have no solutions; the table has {present}")
    return wanted


def _per_time(values: np.ndarray, t_index: np.ndarray, ntime: int, what: str, path: str) -> np.ndarray:
    """One value per time from a per-row column: the first row's, with a
    warning when a time's rows disagree (not seen in any table so far)."""
    out = np.zeros(ntime, dtype=values.dtype)
    out[t_index[::-1]] = values[::-1]                 # the first row at each time wins
    if (out[t_index] != values).any():
        log.warning("%s: %s differs between antennas at one time; the first is used", path, what)
    return out


def _fill(shape: tuple[int, ...], dtype) -> np.ndarray:
    return np.full(shape, np.nan, dtype=dtype)


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
    import xarray as xr

    info = caltab_info(path)
    path = info["path"]
    kw = info["keywords"]
    subtype = info["subType"]
    partype = kw.get("ParType")
    pcol = "CPARAM" if "CPARAM" in info["columns"] else "FPARAM"
    if pcol not in info["columns"]:
        raise NotACalTable(f"{path}: no CPARAM or FPARAM column")

    t = au.open_table(path)
    try:
        if info["nrow"] == 0:
            raise ValueError(f"{path}: the calibration table has no rows")
        main = {c: np.asarray(t.getcol(c)) for c in MAIN_COLUMNS}
        scale = au.epoch_scale(t) or "utc"
        ant_tab = au.open_subtable(path, "ANTENNA")
        try:
            ant_names = [str(n) for n in ant_tab.getcol("NAME")]
        finally:
            ant_tab.close()
        fld_tab = au.open_subtable(path, "FIELD")
        try:
            field_names = np.array([str(n) for n in fld_tab.getcol("NAME")], dtype=object)
        finally:
            fld_tab.close()
        spw_tab = au.open_subtable(path, "SPECTRAL_WINDOW")
        try:
            spw_names = [str(n) for n in spw_tab.getcol("NAME")]
            ids = _spw_ids(main["SPECTRAL_WINDOW_ID"], spw)
            chan_freq = {s: au.getcol_rows(spw_tab, "CHAN_FREQ", [s])[0].astype(np.float64) for s in ids}
        finally:
            spw_tab.close()

        ant_ids = np.unique(main["ANTENNA1"])                       # table-wide, sorted
        ant_coord = np.array([ant_names[a] if 0 <= a < len(ant_names) else f"antenna_{a}"
                              for a in ant_ids], dtype=object)
        ref = np.unique(main["ANTENNA2"])
        one_ref = len(ref) == 1
        ref_name = (ant_names[int(ref[0])] if one_ref and 0 <= ref[0] < len(ant_names) else None)

        nodes: dict[str, Any] = {}
        caltype_attr = None
        for s in ids:
            rows = np.flatnonzero(main["SPECTRAL_WINDOW_ID"] == s)
            par = au.getcol_rows(t, pcol, rows)                      # (row, chan, par)
            err = au.getcol_rows(t, "PARAMERR", rows)
            snr = au.getcol_rows(t, "SNR", rows)
            flag = au.as_bool(au.getcol_rows(t, "FLAG", rows))
            nrow, nchan, npar = par.shape

            times, t_index = np.unique(main["TIME"][rows], return_inverse=True)
            a_index = np.searchsorted(ant_ids, main["ANTENNA1"][rows])
            ntime, nant = len(times), len(ant_ids)
            cell = np.full((ntime, nant), -1, dtype=np.int64)
            cell[t_index, a_index] = rows
            if (cell >= 0).sum() != nrow:
                raise ValueError(f"{path}: spectral window {s} has more than one row for a "
                                 "time and antenna; that layout is not supported")

            freq = chan_freq[s]
            if len(freq) != nchan:
                log.warning("%s: spectral window %d has %d channels of solutions but %d frequencies; "
                            "channel numbers are used", path, s, nchan, len(freq))
                freq = np.arange(nchan, dtype=np.float64)

            ct = S.lookup(subtype, partype, npar)
            caltype_attr = ct.caltype if ct else (subtype or "unknown")
            lead = (S.DIRECTION, S.TIME, S.ANTENNA, S.FREQUENCY)
            data_vars: dict[str, Any] = {}

            def place(values: np.ndarray, fill_dtype=None) -> np.ndarray:
                """(row, chan, ...) -> (1, time, antenna, chan, ...), NaN where there is no row."""
                out = _fill((1, ntime, nant) + values.shape[1:], fill_dtype or values.dtype)
                out[0, t_index, a_index] = values
                return out

            if ct is not None:
                nparam = len(ct.params)
                nrec = npar // nparam
                last, last_coord = S.RECEPTOR, S.receptor_labels(kw.get("PolBasis"), nrec, receptor_labels)
                shape = (nrow, nchan, nrec, nparam)                  # receptor-major packing
                par4, err4, snr4, flag4 = (v.reshape(shape) for v in (par, err, snr, flag))
                dims = lead + (last,)
                for i, p in enumerate(ct.params):
                    attrs = {"long_name": p.long_name}
                    if p.units is not None:
                        attrs["units"] = p.units
                    data_vars[f"{S.PARAM_PREFIX}_{p.name}"] = (dims, place(par4[..., i]), attrs)
                    data_vars[f"{S.ERROR_PREFIX}_{p.name}"] = (
                        dims, place(err4[..., i]), {**attrs, "long_name": f"{p.long_name} error"})
                # one SNR and one FLAG per receptor: CASA writes the same value for
                # each parameter it solved (and SNR 0 for one it did not)
                first = snr4[..., :1]
                if ((snr4 != first) & (snr4 != 0)).any():
                    log.warning("%s: SNR differs between the parameters of one receptor; "
                                "the first parameter's is kept", path)
                data_vars[S.SNR] = (dims, place(snr4[..., 0]), {"long_name": "Signal to noise ratio"})
                flag_cells = flag4.any(axis=-1)
            else:
                last, last_coord = S.PARAMETER, np.arange(npar)
                dims = lead + (last,)
                data_vars[S.PARAM_PREFIX] = (dims, place(par), {"long_name": f"{subtype} parameters"})
                data_vars[S.ERROR_PREFIX] = (dims, place(err), {"long_name": f"{subtype} parameter errors"})
                data_vars[S.SNR] = (dims, place(snr), {"long_name": "Signal to noise ratio"})
                flag_cells = flag

            flag_out = np.ones((1, ntime, nant) + flag_cells.shape[1:], dtype=bool)
            flag_out[0, t_index, a_index] = flag_cells
            data_vars[S.FLAG] = (dims, flag_out, {"long_name": "Calibration flags"})
            data_vars[S.SOURCE_ROW] = ((S.TIME, S.ANTENNA), cell,
                                       {"long_name": "Row of the CASA table; -1: no solution"})
            data_vars[S.INTERVAL] = ((S.TIME, S.ANTENNA), place(main["INTERVAL"][rows])[0],
                                     {"long_name": "Solution interval", "units": "s"})
            if not one_ref:
                names = np.full((ntime, nant), "", dtype=object)
                a2 = main["ANTENNA2"][rows]
                names[t_index, a_index] = [ant_names[a] if 0 <= a < len(ant_names) else "" for a in a2]
                data_vars[S.REFERENCE_ANTENNA] = ((S.TIME, S.ANTENNA), names.astype(str),
                                                  {"long_name": "Reference antenna of each solution"})

            field_id = _per_time(main["FIELD_ID"][rows], t_index, ntime, "FIELD_ID", path)
            scan = _per_time(main["SCAN_NUMBER"][rows], t_index, ntime, "SCAN_NUMBER", path)
            coords = {
                S.DIRECTION: [S.DIRECTION_LABEL],
                S.TIME: (S.TIME, times - au.MJD_UNIX_OFFSET_S,
                         {"type": "time", "units": "s", "scale": scale, "format": "unix"}),
                S.ANTENNA: (S.ANTENNA, ant_coord.astype(str)),
                S.FREQUENCY: (S.FREQUENCY, freq, {"type": "spectral_coord", "units": "Hz"}),
                last: (last, np.asarray(last_coord)),
                "field_name": (S.TIME, np.array(
                    [field_names[f] if 0 <= f < len(field_names) else ("" if f < 0 else f"field_{f}")
                     for f in field_id]).astype(str)),               # -1: no field (a gain curve)
                "scan_name": (S.TIME, np.array([str(v) for v in scan])),
            }
            attrs = {
                "type": S.DATASET_TYPE,
                "caltype": caltype_attr,
                "casa_subtype": subtype or "",
                "spectral_window_id": int(s),
                "spectral_window_name": spw_names[s] if 0 <= s < len(spw_names) else "",
            }
            if ref_name is not None:
                attrs["reference_antenna"] = ref_name
            nodes[f"spw_{s}"] = xr.Dataset(data_vars, coords=coords, attrs=attrs)
    finally:
        t.close()

    root_attrs = {
        "type": S.TREE_TYPE,
        "caltype": caltype_attr or (subtype or "unknown"),
        "casa_subtype": subtype or "",
        "source": path,
        "source_format": "casa-caltab",
        "schema_version": S.SCHEMA_VERSION,
        "schema_draft": S.SCHEMA_DRAFT,
        "schema_deviations": list(S.DEVIATIONS),
        "creator": {"software_name": "cubevis.xarray.caltab"},
    }
    for key, attr in (("MSName", "ms_name"), ("PolBasis", "pol_basis"),
                      ("CASA_Version", "casa_version"), ("ParType", "par_type")):
        if key in kw:
            root_attrs[attr] = str(kw[key])
    tree = {"/": xr.Dataset(attrs=root_attrs)}
    tree.update({f"/{name}": ds for name, ds in nodes.items()})
    return xr.DataTree.from_dict(tree)
