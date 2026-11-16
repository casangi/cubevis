"""A calibration table's rows, and the ``xarray.DataTree`` made of them.

``RawCalTable`` is a cal table as CASA lays it out: one row per solution
(a time, an antenna, a spectral window), with a (channel, parameter) cell
of parameters, errors, SNR and flags, in CASA's units and channel order.
Every source fills one in -- the arcae reader (``_reader``) from a table
on disk, the synthetic source (``_synthetic``) from numbers -- and
``build_tree`` turns it into the tree.  One builder is what makes every
source's tree the same shape.

``tree_to_raw`` goes the other way (for ``_write``, and so for tests).

What the builder changes on the way, and ``tree_to_raw`` changes back:
  * a known type's float parameters become float64 in the schema's units
    (a delay: CASA's ns to seconds);
  * a window whose frequencies decrease is reversed, so ``frequency``
    increases, and its node says ``casa_channel_order = "reversed"``.

The names, dimensions and units come from ``_schema``.  No Bokeh, no
CASA, no arcae.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

import numpy as np

from .. import _arcae_util as au
from . import _schema as S

log = logging.getLogger("cubevis.xarray.caltab")

#: the per-row columns a source provides (CASA's names and meanings)
MAIN_COLUMNS = ("TIME", "FIELD_ID", "SPECTRAL_WINDOW_ID", "ANTENNA1", "ANTENNA2",
                "INTERVAL", "SCAN_NUMBER", "OBSERVATION_ID")

#: root attribute <- table keyword
KEYWORD_ATTRS = (("MSName", "ms_name"), ("PolBasis", "pol_basis"),
                 ("CASA_Version", "casa_version"), ("ParType", "par_type"))


@dataclass
class SpwCells:
    """One spectral window's solutions, as CASA stores them.  The arrays
    are (row, chan, par), in the order of ``rows``."""
    rows: np.ndarray               # row numbers in the table
    par: np.ndarray                # complex or float, CASA's units
    err: np.ndarray
    snr: np.ndarray
    flag: np.ndarray               # bool
    chan_freq: np.ndarray          # Hz, one per channel, in the table's order
    chan_width: Optional[np.ndarray] = None    # Hz, one per channel (CASA's CHAN_WIDTH)


@dataclass
class RawCalTable:
    """A cal table as rows: what ``build_tree`` needs, and no more."""
    source: str                    # the table's path, or what made it
    subtype: str                   # CASA's: "G Jones", "B TSYS", ...
    keywords: dict[str, Any]       # ParType, PolBasis, MSName, CASA_Version
    antenna_names: list[str]       # by antenna id
    field_names: list[str]         # by field id
    spw_names: list[str]           # by spectral window id
    main: dict[str, np.ndarray]    # MAIN_COLUMNS, one value per row; TIME in MJD seconds
    cells: dict[int, SpwCells] = field(default_factory=dict)   # by spectral window id
    time_scale: str = "utc"
    created: Optional[str] = None  # ISO 8601: when the table was made (the tree's creation_date)


def _per_time(values: np.ndarray, t_index: np.ndarray, ntime: int, what: str, source: str) -> np.ndarray:
    """One value per time from a per-row column: the first row's, with a
    warning when a time's rows disagree (not seen in any table so far)."""
    out = np.zeros(ntime, dtype=values.dtype)
    out[t_index[::-1]] = values[::-1]                 # the first row at each time wins
    if (out[t_index] != values).any():
        log.warning("%s: %s differs between antennas at one time; the first is used", source, what)
    return out


def _name(names: list[str], i: int, what: str) -> str:
    """The name of id *i*: "" for none (-1), a made-up one for an id the
    subtable does not have."""
    if 0 <= i < len(names):
        return names[i]
    return "" if i < 0 else f"{what}_{i}"


def _creator() -> dict[str, str]:
    try:
        from importlib.metadata import version
        return {"software_name": S.CREATOR, "version": version("cubevis")}
    except Exception:                                # run from a checkout that is not installed
        return {"software_name": S.CREATOR, "version": "unknown"}


def build_tree(raw: RawCalTable, *, spw: Optional[Iterable[int]] = None,
               polarization_type: Optional[Sequence[str]] = None):
    """The ``xarray.DataTree`` of a ``RawCalTable``: one node per spectral
    window (``spw_<id>``), in the shape ``_schema`` describes.

    *polarization_type* says what the receptors are (``["R", "L"]``) when
    the table does not.
    """
    import xarray as xr

    main = raw.main
    present = sorted(raw.cells)
    if spw is None:
        ids = present
    else:
        ids = [int(s) for s in ([spw] if np.isscalar(spw) else spw)]
        missing = [s for s in ids if s not in present]
        if missing:
            raise ValueError(f"spectral window(s) {missing} have no solutions; the table has {present}")

    ant_ids = np.unique(main["ANTENNA1"])                            # table-wide, sorted
    ant_coord = np.array([_name(raw.antenna_names, int(a), "antenna") for a in ant_ids])
    ref = np.unique(main["ANTENNA2"])
    one_ref = len(ref) == 1
    ref_name = _name(raw.antenna_names, int(ref[0]), "antenna") if one_ref and ref[0] >= 0 else None
    partype = raw.keywords.get("ParType")
    creator = _creator()

    nodes: dict[str, Any] = {}
    caltype_attr = None
    for s in ids:
        c = raw.cells[s]
        rows, par, err, snr, flag = c.rows, c.par, c.err, c.snr, au.as_bool(c.flag)
        nrow, nchan, npar = par.shape

        freq = np.asarray(c.chan_freq, dtype=np.float64)
        width = (np.asarray(c.chan_width, dtype=np.float64)
                 if c.chan_width is not None and len(c.chan_width) == nchan else None)
        if len(freq) != nchan:
            log.warning("%s: spectral window %d has %d channels of solutions but %d frequencies; "
                        "channel numbers are used", raw.source, s, nchan, len(freq))
            freq, width = np.arange(nchan, dtype=np.float64), None
        # xradio's convention: frequency increases.  A window stored the other
        # way is reversed here, and tree_to_raw and the flag commit reverse it back
        reversed_ = bool(nchan > 1 and np.all(np.diff(freq) < 0))
        if reversed_:
            freq = freq[::-1]
            par, err, snr, flag = (v[:, ::-1] for v in (par, err, snr, flag))
        elif nchan > 1 and not np.all(np.diff(freq) > 0):
            log.warning("%s: the frequencies of spectral window %d neither increase nor decrease; "
                        "they are left in the table's order", raw.source, s)

        # MSv4's frequency attributes.  The width is one number, as there: the
        # nominal channel width, positive
        freq_attrs: dict[str, Any] = {"type": "spectral_coord", "long_name": "Frequency", "units": "Hz"}
        if width is not None and nchan:
            w = float(abs(width[0]))
            if not np.allclose(np.abs(width), w, rtol=1e-9, atol=0.0):
                log.warning("%s: the channels of spectral window %d differ in width; the first's "
                            "(%g Hz) is recorded", raw.source, s, w)
            freq_attrs[S.CHANNEL_WIDTH] = S.quantity(w, "Hz")

        times, t_index = np.unique(main["TIME"][rows], return_inverse=True)
        a_index = np.searchsorted(ant_ids, main["ANTENNA1"][rows])
        ntime, nant = len(times), len(ant_ids)
        cell = np.full((ntime, nant), -1, dtype=np.int64)
        cell[t_index, a_index] = rows
        if (cell >= 0).sum() != nrow:
            raise ValueError(f"{raw.source}: spectral window {s} has more than one row for a "
                             "time and antenna; that layout is not supported")

        ct = S.lookup(raw.subtype, partype, npar)
        caltype_attr = ct.caltype if ct else (raw.subtype or "unknown")
        lead = (S.DIRECTION, S.TIME, S.ANTENNA, S.FREQUENCY)
        data_vars: dict[str, Any] = {}
        coords_extra: dict[str, Any] = {}

        def place(values: np.ndarray) -> np.ndarray:
            """(row, chan, ...) -> (1, time, antenna, chan, ...), NaN where there is no row."""
            out = np.full((1, ntime, nant) + values.shape[1:], np.nan, dtype=values.dtype)
            out[0, t_index, a_index] = values
            return out

        if ct is not None:
            nparam = len(ct.params)
            nrec = npar // nparam
            last, last_coord = S.RECEPTOR, S.receptor_labels(nrec, one_for_all=ct.one_for_all)
            types = S.polarization_types(raw.keywords.get("PolBasis"), nrec, polarization_type,
                                         one_for_all=ct.one_for_all)
            if types is not None:                                    # the same for every antenna:
                coords_extra[S.POLARIZATION_TYPE] = (                # a cal table cannot say otherwise
                    (S.ANTENNA, S.RECEPTOR), np.tile(np.array(types), (nant, 1)),
                    {"long_name": "Polarization type of each receptor"})
            shape = (nrow, nchan, nrec, nparam)                      # receptor-major packing
            par4, err4, snr4, flag4 = (v.reshape(shape) for v in (par, err, snr, flag))
            dims = lead + (last,)
            for i, p in enumerate(ct.params):
                attrs = {"long_name": p.long_name}
                if p.units is not None:
                    attrs["units"] = p.units
                values, errors = par4[..., i], err4[..., i]
                if not np.iscomplexobj(values):                      # the draft's float64, in its units
                    values = values.astype(np.float64) * p.scale
                    errors = errors.astype(np.float64) * p.scale
                data_vars[f"{S.PARAM_PREFIX}_{p.name}"] = (dims, place(values), attrs)
                data_vars[f"{S.ERROR_PREFIX}_{p.name}"] = (
                    dims, place(errors), {**attrs, "long_name": f"{p.long_name} error"})
            # one SNR and one FLAG per receptor: CASA writes the same value for
            # each parameter it solved (and SNR 0 for one it did not)
            first = snr4[..., :1]
            if ((snr4 != first) & (snr4 != 0)).any():
                log.warning("%s: SNR differs between the parameters of one receptor; "
                            "the first parameter's is kept", raw.source)
            data_vars[S.SNR] = (dims, place(snr4[..., 0]), {"long_name": "Signal to noise ratio",
                                                            "units": "dimensionless"})
            flag_cells = flag4.any(axis=-1)
        else:
            last, last_coord = S.PARAMETER, np.arange(npar)
            dims = lead + (last,)
            data_vars[S.PARAM_PREFIX] = (dims, place(par), {"long_name": f"{raw.subtype} parameters"})
            data_vars[S.ERROR_PREFIX] = (dims, place(err), {"long_name": f"{raw.subtype} parameter errors"})
            data_vars[S.SNR] = (dims, place(snr), {"long_name": "Signal to noise ratio",
                                                   "units": "dimensionless"})
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
            names[t_index, a_index] = [_name(raw.antenna_names, int(a), "antenna")
                                       for a in main["ANTENNA2"][rows]]
            data_vars[S.REFERENCE_ANTENNA] = ((S.TIME, S.ANTENNA), names.astype(str),
                                              {"long_name": "Reference antenna of each solution"})

        field_id = _per_time(main["FIELD_ID"][rows], t_index, ntime, "FIELD_ID", raw.source)
        scan = _per_time(main["SCAN_NUMBER"][rows], t_index, ntime, "SCAN_NUMBER", raw.source)
        coords = {
            S.DIRECTION: [S.DIRECTION_LABEL],
            S.TIME: (S.TIME, times - au.MJD_UNIX_OFFSET_S,
                     {"type": "time", "units": "s", "scale": raw.time_scale, "format": "unix"}),
            S.ANTENNA: (S.ANTENNA, ant_coord.astype(str)),
            S.FREQUENCY: (S.FREQUENCY, freq, freq_attrs),
            last: (last, np.asarray(last_coord)),
            **coords_extra,
            # "" for field -1: no field (a gain curve)
            "field_name": (S.TIME, np.array([_name(raw.field_names, int(f), "field")
                                             for f in field_id]).astype(str)),
            "scan_name": (S.TIME, np.array([str(v) for v in scan])),
        }
        attrs = {
            "type": S.DATASET_TYPE,
            "caltype": caltype_attr,
            "casa_subtype": raw.subtype or "",
            "schema_version": S.SCHEMA_VERSION,
            "type_version": S.TYPE_VERSION,
            "creator": dict(creator),
            "spectral_window_id": int(s),
            "spectral_window_name": _name(raw.spw_names, int(s), "spw"),
        }
        if ref_name is not None:
            attrs["reference_antenna"] = ref_name
        if reversed_:
            attrs[S.CHANNEL_ORDER] = S.REVERSED
        nodes[f"spw_{s}"] = xr.Dataset(data_vars, coords=coords, attrs=attrs)

    root_attrs = {
        "type": S.TREE_TYPE,
        "caltype": caltype_attr or (raw.subtype or "unknown"),
        "casa_subtype": raw.subtype or "",
        "source": raw.source,
        "source_format": "casa-caltab",
        "schema_version": S.SCHEMA_VERSION,
        "schema_draft": S.SCHEMA_DRAFT,
        "schema_deviations": list(S.DEVIATIONS),
        "creator": dict(creator),
    }
    if raw.created:
        root_attrs["creation_date"] = raw.created
    for key, attr in KEYWORD_ATTRS:
        if key in raw.keywords:
            root_attrs[attr] = str(raw.keywords[key])
    tree = {"/": xr.Dataset(attrs=root_attrs)}
    tree.update({f"/{name}": ds for name, ds in nodes.items()})
    return xr.DataTree.from_dict(tree)


def tree_to_raw(tree) -> RawCalTable:
    """The rows of a tree: ``build_tree`` undone.

    Rows keep the order ``SOURCE_ROW`` gives them, renumbered from 0.
    Values go back to CASA's units and single precision, and a reversed
    window to its own channel order.  The tree holds names, not CASA's
    ids, so the antennas and fields are numbered here in the order they
    are met: a table read, then written from its tree, has the same
    solutions but may number an antenna or a field differently, and
    leaves out those with no solution.
    """
    subtype = tree.attrs.get("casa_subtype", "")
    keywords = {key: tree.attrs[attr] for key, attr in KEYWORD_ATTRS if attr in tree.attrs}
    nodes = {int(ds.attrs["spectral_window_id"]): ds
             for ds in (n.to_dataset(inherit=False) for n in tree.children.values())}
    if not nodes:
        raise ValueError("the tree has no spectral windows")
    known = S.CAL_TYPES.get(subtype)

    antenna_names: list[str] = []
    field_names: list[str] = []

    def ident(names: list[str], name: str) -> int:
        if name == "":
            return -1
        if name not in names:
            names.append(name)
        return names.index(name)

    for ds in nodes.values():                                        # one numbering for all windows
        for n in ds[S.ANTENNA].values:
            ident(antenna_names, str(n))

    order: list[tuple[int, int, int, int]] = []                      # (source row, spw, time, antenna)
    for s, ds in nodes.items():
        src = ds[S.SOURCE_ROW].values
        for ti, ai in zip(*np.nonzero(src >= 0)):
            order.append((int(src[ti, ai]), s, int(ti), int(ai)))
    order.sort()
    n = len(order)
    main = {"TIME": np.zeros(n), "INTERVAL": np.zeros(n),
            **{c: np.zeros(n, dtype=np.int32) for c in MAIN_COLUMNS if c not in ("TIME", "INTERVAL")}}
    per_spw: dict[int, list[tuple[int, int, int]]] = {s: [] for s in nodes}
    for row, (_, s, ti, ai) in enumerate(order):
        ds = nodes[s]
        main["TIME"][row] = ds[S.TIME].values[ti] + au.MJD_UNIX_OFFSET_S
        main["INTERVAL"][row] = ds[S.INTERVAL].values[ti, ai]
        main["SPECTRAL_WINDOW_ID"][row] = s
        main["ANTENNA1"][row] = ident(antenna_names, str(ds[S.ANTENNA].values[ai]))
        main["FIELD_ID"][row] = ident(field_names, str(ds.field_name.values[ti]))
        main["SCAN_NUMBER"][row] = int(ds.scan_name.values[ti])
        if S.REFERENCE_ANTENNA in ds:
            ref = str(ds[S.REFERENCE_ANTENNA].values[ti, ai])
        else:
            ref = ds.attrs.get("reference_antenna", "")
        main["ANTENNA2"][row] = ident(antenna_names, ref)
        per_spw[s].append((row, ti, ai))

    first = next(iter(nodes.values()))
    if S.POLARIZATION_TYPE in first.coords:                          # so the types read back
        types = first[S.POLARIZATION_TYPE].values
        if (types == types[:1]).all() and S.pol_basis_of(types[0]) != "unknown":
            keywords["PolBasis"] = S.pol_basis_of(types[0])
    cells: dict[int, SpwCells] = {}
    spw_names = [""] * (max(nodes) + 1)
    partype = keywords.get("ParType")
    for s, ds in nodes.items():
        spw_names[s] = str(ds.attrs.get("spectral_window_name", ""))
        rows = np.array([r for r, _, _ in per_spw[s]], dtype=np.int64)
        ti = np.array([t for _, t, _ in per_spw[s]], dtype=np.int64)
        ai = np.array([a for _, _, a in per_spw[s]], dtype=np.int64)

        def take(name: str) -> np.ndarray:                           # (row, chan, last)
            return ds[name].values[0][ti, ai]

        if S.PARAM_PREFIX in ds:                                     # the generic form
            par, err, snr, flag = (take(v) for v in (S.PARAM_PREFIX, S.ERROR_PREFIX, S.SNR, S.FLAG))
        else:
            if known is not None:                                    # the type's order, not the store's
                params = [p for p in known.params if f"{S.PARAM_PREFIX}_{p.name}" in ds]
            else:
                params = [S.Param(v[len(S.PARAM_PREFIX) + 1:], "", None) for v in ds.data_vars
                          if v.startswith(S.PARAM_PREFIX + "_")]
            nparam = len(params)

            def casa(prefix: str, p: S.Param) -> np.ndarray:         # back to CASA's units and precision
                v = take(f"{prefix}_{p.name}")
                return v if np.iscomplexobj(v) else (v / p.scale).astype(np.float32)

            def pack(per: list[np.ndarray]) -> np.ndarray:           # [(row, chan, rec)] -> (row, chan, par)
                stacked = np.stack(per, axis=-1)                     # (row, chan, rec, param)
                return stacked.reshape(stacked.shape[0], stacked.shape[1], -1)

            par = pack([casa(S.PARAM_PREFIX, p) for p in params])
            err = pack([casa(S.ERROR_PREFIX, p) for p in params])
            flag = pack([take(S.FLAG)] * nparam)
            # the receptor's SNR for each parameter; what CASA wrote for one it
            # did not solve (0) is not in the tree
            snr = pack([take(S.SNR)] * nparam)
        freq = np.asarray(ds[S.FREQUENCY].values, dtype=np.float64)
        w = S.quantity_value(ds[S.FREQUENCY].attrs.get(S.CHANNEL_WIDTH))
        width = None if w is None else np.full(freq.size, w)
        if ds.attrs.get(S.CHANNEL_ORDER) == S.REVERSED:              # back to the table's order,
            freq = freq[::-1]                                        # where CASA's widths are negative
            width = None if width is None else -width
            par, err, snr, flag = (v[:, ::-1] for v in (par, err, snr, flag))
        cells[s] = SpwCells(rows=rows, par=par, err=err, snr=snr, flag=flag.astype(bool),
                            chan_freq=freq, chan_width=width)
        if partype is None:
            partype = "Complex" if np.iscomplexobj(par) else "Float"
    keywords.setdefault("ParType", partype)
    scale = first[S.TIME].attrs.get("scale", "utc")
    return RawCalTable(source=str(tree.attrs.get("source", "")), subtype=subtype, keywords=keywords,
                       antenna_names=antenna_names, field_names=field_names, spw_names=spw_names,
                       main=main, cells=cells, time_scale=scale,
                       created=tree.attrs.get("creation_date"))
