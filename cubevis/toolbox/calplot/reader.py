"""``CalTableReader``: the one thing calplot asks about a calibration table.

    reader = CalTableReader.open("tl016b.gff")
    info = reader.info()                    # what it is, and what can be asked of it
    s = reader.read("delay", antenna=["BR", "KP"], spw=[1])
    s.time, s.y, s.flag, s.antenna          # one entry per solution: plain arrays

Everything above this class -- the grid cells, the controls, the flag
list -- sees only ``CalInfo`` and ``CalSamples``: names, numbers and
flat arrays.  It never sees the tree a table is read into, and so it
does not depend on how that tree is arranged.  That is what keeps
calplot independent of the reader (plan section 9.6): when xradio's
calibration layout settles, what changes is how this class walks the
data, not what it returns.

It is thin on purpose.  The data come from ``cubevis.xarray.caltab``
(a CASA table read with arcae, a synthetic table, or a tree saved to
Zarr); this class adds

* **selection** -- antenna, window, field, scan, receptor, time,
  channel -- by the names a person uses;
* **quantities** -- amplitude and phase of a complex gain, or a
  parameter by name -- with their units;
* **evaluation** -- a K table's delays as the phase they apply across
  the band (plan section 9.4, item 1);
* **flags** -- set by the same selection, then written with
  ``cubevis.xarray.caltab.commit_flags``.

No Bokeh, no CASA.  xarray is imported when a table is opened.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any, Optional, Sequence, Union

import numpy as np

from ...xarray.caltab import _schema as S

Names = Union[None, str, int, Sequence[Union[str, int]]]
Range = Optional[tuple[float, float]]

#: quantities of a complex parameter: name -> (function, units or None for the parameter's own)
_COMPLEX = {
    "amplitude": (np.abs, None),
    "phase": (np.angle, "rad"),
    "real": (np.real, None),
    "imag": (np.imag, None),
}

#: what ``evaluate`` offers, by ``caltype``
_EVALUATIONS = {"K": ("phase",)}


@dataclass(frozen=True)
class QuantityInfo:
    """Something ``read`` can return."""
    name: str                      # what to ask for: "amplitude", "delay", "snr" ...
    label: str                     # for an axis: "Gain amplitude"
    units: str                     # "" when there are none, or none are known
    parameter: str                 # the stored parameter it comes from ("GAIN"); "" for snr


@dataclass(frozen=True)
class SpwInfo:
    id: int
    name: str
    nchan: int
    frequency: tuple[float, float]     # first and last channel, Hz
    bandwidth: float                   # Hz: the channels' widths added up (0 if not known)
    ntime: int


@dataclass(frozen=True)
class CalInfo:
    """What a calibration table is.  Names are in the order of the numbers
    ``CalSamples`` uses for them."""
    source: str
    source_format: str
    caltype: str                       # "G", "B", "K", "Fringe", "Tsys" ... or CASA's subtype
    casa_subtype: str
    antennas: tuple[str, ...]
    receptors: tuple[str, ...]         # labels; for an unknown type, the parameter numbers
    spws: tuple[SpwInfo, ...]
    fields: tuple[str, ...]
    scans: tuple[str, ...]
    time_range: tuple[float, float]    # Unix seconds
    reference_antenna: Optional[str]   # a name, None (there is none), or "varies"
    quantities: tuple[QuantityInfo, ...]
    evaluations: tuple[str, ...]       # what ``evaluate`` offers ("phase" for a K table)
    nsolutions: int                    # solutions (time x antenna x channel x receptor) with a row
    nflagged: int                      # of those, flagged
    can_write_flags: bool


@dataclass(frozen=True, eq=False)
class CalSamples:
    """Solutions as flat arrays: one entry per solution, all the same length.

    ``antenna``, ``receptor``, ``field`` and ``scan`` are numbers into the
    tuples of names; ``spw`` is the window's id.  Only solutions that
    exist are here: a time and antenna with no solution has no entry.
    """
    quantity: str
    label: str
    units: str
    y: np.ndarray
    flag: np.ndarray                   # bool
    time: np.ndarray                   # Unix seconds
    frequency: np.ndarray              # Hz
    channel: np.ndarray                # channel number in its window
    antenna: np.ndarray
    spw: np.ndarray
    receptor: np.ndarray
    field: np.ndarray
    scan: np.ndarray
    error: Optional[np.ndarray]        # the parameter's error, where it has a meaning; else None
    antennas: tuple[str, ...] = ()
    receptors: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    scans: tuple[str, ...] = ()
    evaluated: bool = False            # from ``evaluate``: computed, not stored

    _COLUMNS = ("y", "flag", "time", "frequency", "channel", "antenna", "spw", "receptor",
                "field", "scan", "error")

    def __len__(self) -> int:
        return int(self.y.shape[0])

    def where(self, mask) -> "CalSamples":
        """The entries where *mask* is true (a grid cell: ``s.where(s.antenna == 3)``)."""
        mask = np.asarray(mask, dtype=bool)
        return replace(self, **{c: getattr(self, c)[mask] for c in self._COLUMNS
                                if getattr(self, c) is not None})


@dataclass(frozen=True)
class _Selection:
    antenna: Names = None
    spw: Names = None
    field: Names = None
    scan: Names = None
    receptor: Names = None
    time: Range = None                 # (first, last), Unix seconds, inclusive
    channel: Range = None              # (first, last) channel numbers, inclusive
    frequency: Range = None            # (lowest, highest), Hz, inclusive


def _as_list(value: Names) -> Optional[list]:
    if value is None:
        return None
    if isinstance(value, (str, int, np.integer)):
        return [value]
    return list(value)


def _pick(value: Names, names: Sequence[str], what: str) -> Optional[np.ndarray]:
    """Numbers into *names* for a selection by name or by number; None: all."""
    wanted = _as_list(value)
    if wanted is None:
        return None
    out = []
    for v in wanted:
        if isinstance(v, (int, np.integer)) and not isinstance(v, bool):
            if not 0 <= int(v) < len(names):
                raise ValueError(f"{what} {int(v)}: there are {len(names)} (0 to {len(names) - 1})")
            out.append(int(v))
        elif str(v) in names:
            out.append(list(names).index(str(v)))
        else:
            raise ValueError(f"no {what} {v!r}; there are: {', '.join(names)}")
    return np.array(sorted(set(out)), dtype=np.int64)


def _in_range(values: np.ndarray, rng: Range) -> np.ndarray:
    if rng is None:
        return np.ones(values.shape, dtype=bool)
    lo, hi = float(rng[0]), float(rng[1])
    return (values >= lo) & (values <= hi)


class CalTableReader:
    """A calibration table, as calplot uses one.  Make it with ``open``."""

    def __init__(self, tree) -> None:
        self._tree = tree
        self._nodes = {int(n.attrs["spectral_window_id"]): n for n in tree.children.values()}
        if not self._nodes:
            raise ValueError("the calibration table has no spectral windows")
        self._info: Optional[CalInfo] = None
        first = next(iter(self._nodes.values())).ds
        self._generic = S.RECEPTOR not in first.dims
        self._last = S.PARAMETER if self._generic else S.RECEPTOR

    # ---- opening -----------------------------------------------------------------------

    @classmethod
    def open(cls, source, **kwargs) -> "CalTableReader":
        """A reader for *source*: a CASA calibration table, a tree saved to
        Zarr, or a tree already in hand (``synthetic_caltab(...)``).

        *kwargs* go to the opener (``spw=``, ``receptor_labels=`` for a
        CASA table).
        """
        if hasattr(source, "children") and hasattr(source, "attrs"):         # a DataTree
            return cls(source)
        import xarray as xr
        from ...xarray import _arcae_util as au
        from ...xarray.caltab import open_caltab
        path = os.path.abspath(os.path.expanduser(str(source)))
        if au.is_table(path):
            return cls(open_caltab(path, **kwargs))                  # NotACalTable if it is another kind
        if os.path.isdir(path) and (os.path.exists(os.path.join(path, "zarr.json"))
                                    or os.path.exists(os.path.join(path, ".zgroup"))):
            tree = xr.open_datatree(path, engine="zarr").load()
            if tree.attrs.get("type") != S.TREE_TYPE:
                raise ValueError(f"{path}: a Zarr store, but not a calibration set")
            tree.attrs["source"], tree.attrs["source_format"] = path, "zarr"
            return cls(tree)
        raise ValueError(f"{path}: not a CASA calibration table or a calibration set in Zarr")

    def to_datatree(self):
        """The data as an ``xarray.DataTree`` (for saving or for other
        tools; calplot itself does not use it)."""
        return self._tree

    # ---- what it is ---------------------------------------------------------------------

    def _names(self, coord: str) -> tuple[str, ...]:
        """Every value of a per-time coordinate, over the windows, in order of first use."""
        seen: dict[str, None] = {}
        for s in sorted(self._nodes):
            ds = self._nodes[s].ds
            times = (ds[S.SOURCE_ROW] >= 0).any(S.ANTENNA).values
            for v in ds[coord].values[times]:
                seen.setdefault(str(v))
        return tuple(seen)

    def _quantities(self) -> tuple[QuantityInfo, ...]:
        ds = next(iter(self._nodes.values())).ds
        out: list[QuantityInfo] = []
        # the type's own order of parameters, not the store's (Zarr gives them back sorted)
        known = S.CAL_TYPES.get(str(self._tree.attrs.get("casa_subtype", "")))
        order = [f"{S.PARAM_PREFIX}_{p.name}" for p in known.params] if known else []
        names = sorted(ds.data_vars, key=lambda v: (order.index(v) if v in order else len(order), str(v)))
        for var in names:
            if var == S.PARAM_PREFIX:                                # the generic form: one per number
                out.append(QuantityInfo("value", str(ds[var].attrs.get("long_name", "Parameter")),
                                        str(ds[var].attrs.get("units", "")), ""))
            elif var.startswith(S.PARAM_PREFIX + "_"):
                name = var[len(S.PARAM_PREFIX) + 1:]
                long_name = str(ds[var].attrs.get("long_name", name.title()))
                units = str(ds[var].attrs.get("units", ""))
                if np.iscomplexobj(ds[var].values):
                    base = "Gain" if name == "GAIN" else long_name
                    for q, (_, q_units) in _COMPLEX.items():
                        own = "" if units == "dimensionless" else units
                        out.append(QuantityInfo(q, f"{base} {q}" if q in ("amplitude", "phase")
                                                else f"{base} ({q})", own if q_units is None else q_units,
                                                name))
                else:
                    out.append(QuantityInfo(name.lower(), long_name,
                                            "" if units == "dimensionless" else units, name))
        out.append(QuantityInfo("snr", "Signal to noise ratio", "", ""))
        return tuple(out)

    def info(self) -> CalInfo:
        """What the table is: its type, antennas, windows, fields, scans,
        and what ``read`` and ``evaluate`` can return."""
        if self._info is not None:
            return self._info
        tree = self._tree
        antennas: dict[str, None] = {}
        spws, t_lo, t_hi, nsol, nflag = [], np.inf, -np.inf, 0, 0
        refs: set[Optional[str]] = set()
        for s in sorted(self._nodes):
            ds = self._nodes[s].ds
            for a in ds[S.ANTENNA].values:
                antennas.setdefault(str(a))
            freq = ds[S.FREQUENCY].values
            width = (float(np.abs(ds[S.CHANNEL_WIDTH].values).sum())
                     if S.CHANNEL_WIDTH in ds.coords else 0.0)
            spws.append(SpwInfo(s, str(ds.attrs.get("spectral_window_name", "")), int(freq.size),
                                (float(freq[0]), float(freq[-1])), width, int(ds.sizes[S.TIME])))
            t_lo, t_hi = min(t_lo, float(ds[S.TIME].values.min())), max(t_hi, float(ds[S.TIME].values.max()))
            solved = (ds[S.SOURCE_ROW] >= 0).values
            flag = ds[S.FLAG].values[0][solved]
            nsol, nflag = nsol + int(flag.size), nflag + int(flag.sum())
            if S.REFERENCE_ANTENNA in ds:
                refs.add("varies")
            else:
                refs.add(ds.attrs.get("reference_antenna"))
        last = next(iter(self._nodes.values())).ds[self._last].values
        reference = "varies" if "varies" in refs or len(refs) > 1 else next(iter(refs))
        from ...xarray.caltab._flags import COMMITTERS
        source, fmt = str(tree.attrs.get("source", "")), str(tree.attrs.get("source_format", ""))
        caltype = str(tree.attrs.get("caltype", ""))
        self._info = CalInfo(
            source=source, source_format=fmt, caltype=caltype,
            casa_subtype=str(tree.attrs.get("casa_subtype", "")),
            antennas=tuple(antennas), receptors=tuple(str(v) for v in last), spws=tuple(spws),
            fields=self._names("field_name"), scans=self._names("scan_name"),
            time_range=(t_lo, t_hi), reference_antenna=reference,
            quantities=self._quantities(), evaluations=_EVALUATIONS.get(caltype, ()),
            nsolutions=nsol, nflagged=nflag,
            can_write_flags=fmt in COMMITTERS and os.path.isdir(source))
        return self._info

    # ---- selection ------------------------------------------------------------------------

    def _masks(self, sel: _Selection):
        """For each selected window: (id, dataset, mask over (time, antenna, chan, last))."""
        info = self.info()
        spw_ids = [w.id for w in info.spws]
        wanted = _as_list(sel.spw)
        if wanted is not None:
            wanted = [int(v) for v in wanted]
            missing = [v for v in wanted if v not in spw_ids]
            if missing:
                raise ValueError(f"no spectral window {missing}; there are: {spw_ids}")
        ant = _pick(sel.antenna, info.antennas, "antenna")
        rec = _pick(sel.receptor, info.receptors, "parameter" if self._generic else "receptor")
        fields = _as_list(sel.field)
        if fields is not None:
            fields = [info.fields[int(f)] if isinstance(f, (int, np.integer)) else str(f) for f in fields]
            unknown = [f for f in fields if f not in info.fields]
            if unknown:
                raise ValueError(f"no field {unknown}; there are: {', '.join(info.fields)}")
        scans = None if sel.scan is None else [str(v) for v in _as_list(sel.scan)]
        if scans is not None and [v for v in scans if v not in info.scans]:
            raise ValueError(f"no scan {[v for v in scans if v not in info.scans]}; "
                             f"there are: {', '.join(info.scans)}")
        for s in (spw_ids if wanted is None else wanted):
            ds = self._nodes[s].ds
            t = _in_range(ds[S.TIME].values, sel.time)
            if fields is not None:
                t &= np.isin(ds.field_name.values.astype(str), fields)
            if scans is not None:
                t &= np.isin(ds.scan_name.values.astype(str), scans)
            names = ds[S.ANTENNA].values.astype(str)
            a_number = np.array([info.antennas.index(n) for n in names])
            a = np.ones(names.size, dtype=bool) if ant is None else np.isin(a_number, ant)
            nchan = ds.sizes[S.FREQUENCY]
            c = _in_range(np.arange(nchan), sel.channel) & _in_range(ds[S.FREQUENCY].values, sel.frequency)
            nlast = ds.sizes[self._last]
            r = np.ones(nlast, dtype=bool) if rec is None else np.isin(np.arange(nlast), rec)
            solved = (ds[S.SOURCE_ROW] >= 0).values
            mask = (solved[:, :, None, None] & t[:, None, None, None] & a[None, :, None, None]
                    & c[None, None, :, None] & r[None, None, None, :])
            yield s, ds, mask, a_number

    # ---- reading ----------------------------------------------------------------------------

    def _quantity(self, name: str) -> QuantityInfo:
        for q in self.info().quantities:
            if q.name == name:
                return q
        raise ValueError(f"no quantity {name!r} in a {self.info().caltype} table; there are: "
                         f"{', '.join(q.name for q in self.info().quantities)}")

    def _columns(self, s: int, ds, idx, a_number) -> dict[str, np.ndarray]:
        info = self.info()
        ti, ai, ci, ri = idx
        return {
            "time": ds[S.TIME].values[ti], "frequency": ds[S.FREQUENCY].values[ci],
            "channel": ci.astype(np.int64), "antenna": a_number[ai].astype(np.int64),
            "spw": np.full(ti.size, s, dtype=np.int64), "receptor": ri.astype(np.int64),
            "field": np.array([info.fields.index(str(v)) for v in ds.field_name.values], dtype=np.int64)[ti]
            if ti.size else np.zeros(0, dtype=np.int64),
            "scan": np.array([info.scans.index(str(v)) for v in ds.scan_name.values], dtype=np.int64)[ti]
            if ti.size else np.zeros(0, dtype=np.int64),
        }

    def _samples(self, q: QuantityInfo, parts: list[dict[str, np.ndarray]], evaluated=False) -> CalSamples:
        info = self.info()
        def cat(key, dtype=None):
            arrays = [p[key] for p in parts]
            if not arrays:
                return np.zeros(0, dtype=dtype or np.float64)
            return np.concatenate(arrays)
        has_error = bool(parts) and all(p.get("error") is not None for p in parts)
        return CalSamples(
            quantity=q.name, label=q.label, units=q.units, y=cat("y"), flag=cat("flag", bool),
            time=cat("time"), frequency=cat("frequency"), channel=cat("channel", np.int64),
            antenna=cat("antenna", np.int64), spw=cat("spw", np.int64), receptor=cat("receptor", np.int64),
            field=cat("field", np.int64), scan=cat("scan", np.int64),
            error=cat("error") if has_error else None,
            antennas=info.antennas, receptors=info.receptors, fields=info.fields, scans=info.scans,
            evaluated=evaluated)

    def read(self, quantity: str, *, antenna: Names = None, spw: Names = None, field: Names = None,
             scan: Names = None, receptor: Names = None, time: Range = None, channel: Range = None,
             frequency: Range = None, include_flagged: bool = True) -> CalSamples:
        """The solutions of a selection, as flat arrays.

        Parameters
        ----------
        quantity
            One of ``info().quantities``: ``"amplitude"``, ``"phase"``,
            ``"real"``, ``"imag"`` of a complex gain; a parameter by name
            (``"delay"``, ``"rate"``, ``"tsys"``); ``"snr"``; or
            ``"value"`` for a type the schema does not know.
        antenna, field, scan, receptor
            Names, or numbers into ``info()``'s tuples; one or several.
            Default: all.
        spw
            Spectral window id(s).
        time, frequency
            (first, last), inclusive: Unix seconds, Hz.
        channel
            (first, last) channel numbers, inclusive.
        include_flagged
            False leaves flagged solutions out; by default they are
            returned, with ``flag`` set.
        """
        q = self._quantity(quantity)
        sel = _Selection(antenna, spw, field, scan, receptor, time, channel, frequency)
        parts = []
        for s, ds, mask, a_number in self._masks(sel):
            flag_all = ds[S.FLAG].values[0]
            if not include_flagged:
                mask = mask & ~flag_all
            idx = np.nonzero(mask)
            part = self._columns(s, ds, idx, a_number)
            part["flag"] = flag_all[idx]
            if q.name == "snr":
                part["y"], part["error"] = ds[S.SNR].values[0][idx].astype(np.float64), None
            elif q.name == "value":
                part["y"] = ds[S.PARAM_PREFIX].values[0][idx].astype(np.float64)
                part["error"] = ds[S.ERROR_PREFIX].values[0][idx].astype(np.float64)
            else:
                values = ds[f"{S.PARAM_PREFIX}_{q.parameter}"].values[0][idx]
                if q.name in _COMPLEX and np.iscomplexobj(values):
                    part["y"], part["error"] = _COMPLEX[q.name][0](values).astype(np.float64), None
                else:
                    part["y"] = values.astype(np.float64)
                    part["error"] = ds[f"{S.ERROR_PREFIX}_{q.parameter}"].values[0][idx].astype(np.float64)
            parts.append(part)
        return self._samples(q, parts)

    # ---- evaluation ---------------------------------------------------------------------------

    def evaluate(self, quantity: str = "phase", *, nfreq: int = 64,
                 frequencies: Optional[dict[int, Sequence[float]]] = None, wrap: bool = True,
                 **selection) -> CalSamples:
        """What a parameterised table does, where ``read`` gives what it stores.

        For a K (delay) table, ``"phase"``: the phase each delay applies
        across its window,

            phase = 2 pi x delay x (frequency - the table's frequency)

        with the delay in seconds.  CASA references the phase to the
        frequency the table gives the solution (the middle of the band it
        was solved over); checked against ``applycal`` on a simulated MS.

        Parameters
        ----------
        nfreq
            Frequencies per window, spread over the band the solution
            stands for (the channel's width).
        frequencies
            Instead: ``{spw id: frequencies in Hz}``, for example an MS's
            channels.
        wrap
            Phases in (-pi, pi] (default), or unwrapped.
        selection
            As ``read``'s: antenna, spw, field, scan, receptor, time.

        Each solution becomes one entry per frequency; ``channel`` numbers
        the frequencies, and ``evaluated`` is True.
        """
        info = self.info()
        if quantity not in info.evaluations:
            raise ValueError(f"a {info.caltype} table has nothing to evaluate as {quantity!r}"
                             + (f"; it offers: {', '.join(info.evaluations)}" if info.evaluations else ""))
        include_flagged = selection.pop("include_flagged", True)
        sel = _Selection(**selection)
        if sel.channel is not None or sel.frequency is not None:
            raise ValueError("evaluate takes nfreq= or frequencies=, not a channel or frequency selection")
        q = QuantityInfo("phase", "Phase from delay", "rad", "DELAY")
        to_seconds = {"ns": 1e-9, "s": 1.0}
        parts = []
        for s, ds, mask, a_number in self._masks(sel):
            delay = ds[f"{S.PARAM_PREFIX}_DELAY"]
            scale = to_seconds.get(str(delay.attrs.get("units", "ns")))
            if scale is None:
                raise ValueError(f"delay in {delay.attrs.get('units')!r}: units not known")
            if ds.sizes[S.FREQUENCY] != 1:
                raise ValueError("a delay per channel cannot be evaluated across a band")
            flag_all = ds[S.FLAG].values[0]
            if not include_flagged:
                mask = mask & ~flag_all
            ti, ai, ci, ri = np.nonzero(mask)
            f0 = float(ds[S.FREQUENCY].values[0])
            if frequencies is not None and s in frequencies:
                freq = np.asarray(frequencies[s], dtype=np.float64)
            else:
                width = (float(abs(ds[S.CHANNEL_WIDTH].values[0])) if S.CHANNEL_WIDTH in ds.coords else 0.0)
                if width <= 0:
                    raise ValueError(f"spectral window {s}: the band's width is not known; "
                                     "give frequencies={spw: [...]}")
                freq = f0 + np.linspace(-0.5, 0.5, int(nfreq)) * width
            tau = delay.values[0][ti, ai, ci, ri].astype(np.float64) * scale
            phase = 2 * np.pi * tau[:, None] * (freq[None, :] - f0)          # (solution, frequency)
            if wrap:
                phase = np.angle(np.exp(1j * phase))
            n, m = phase.shape
            rep = lambda v: np.repeat(v, m)                                   # noqa: E731
            part = {k: rep(v) for k, v in self._columns(s, ds, (ti, ai, ci, ri), a_number).items()}
            part["frequency"] = np.tile(freq, n)
            part["channel"] = np.tile(np.arange(m, dtype=np.int64), n)
            part["y"], part["flag"], part["error"] = phase.ravel(), rep(flag_all[ti, ai, ci, ri]), None
            parts.append(part)
        return self._samples(q, parts, evaluated=True)

    # ---- flags -----------------------------------------------------------------------------------

    def set_flags(self, flagged: bool = True, *, antenna: Names = None, spw: Names = None,
                  field: Names = None, scan: Names = None, receptor: Names = None, time: Range = None,
                  channel: Range = None, frequency: Range = None) -> int:
        """Flag (or, with ``flagged=False``, unflag) the solutions of a
        selection, in memory; returns how many changed.

        Nothing is written until ``commit_flags``.  ``read`` shows the
        new flags at once.
        """
        sel = _Selection(antenna, spw, field, scan, receptor, time, channel, frequency)
        changed = 0
        for s, ds, mask, _ in self._masks(sel):
            values = self._nodes[s][S.FLAG].data                              # the tree's own array
            differ = mask & (values[0] != bool(flagged))
            changed += int(differ.sum())
            values[0][differ] = bool(flagged)
        self._info = None                                                     # nflagged has changed
        return changed

    def flag_changes(self) -> dict[str, Any]:
        """What ``commit_flags`` would write, without writing it."""
        from ...xarray.caltab import commit_flags
        return commit_flags(self._tree, dry_run=True)

    def commit_flags(self, **kwargs) -> dict[str, Any]:
        """Write the flags to the table (``cubevis.xarray.caltab.commit_flags``:
        checked, backed up, read back).  Returns its report."""
        from ...xarray.caltab import commit_flags
        return commit_flags(self._tree, **kwargs)

    def restore_flags(self, backup_path: str) -> dict[str, Any]:
        """Put back what a commit replaced, in the table and in this reader."""
        from ...xarray.caltab import restore_flags
        report = restore_flags(backup_path, self.info().source)
        self.reload_flags()
        return report

    def flag_backups(self) -> list[dict[str, Any]]:
        from ...xarray.caltab import list_flag_backups
        return list_flag_backups(self.info().source)

    def reload_flags(self) -> int:
        """Take the flags from the store again, dropping changes not yet
        committed; returns how many solutions' flags changed here."""
        info = self.info()
        fresh = CalTableReader.open(info.source)
        changed = 0
        for s, node in self._nodes.items():
            if s not in fresh._nodes:
                continue
            new = fresh._nodes[s].ds[S.FLAG]
            if new.shape != node.ds[S.FLAG].shape:
                raise ValueError(f"{info.source} has changed since it was read; open it again")
            changed += int((new.values != node.ds[S.FLAG].values).sum())
            node[S.FLAG].data[...] = new.values
        self._info = None
        return changed
