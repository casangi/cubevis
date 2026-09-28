"""flag_export.py
=================
Pure translators from pending ``FlagDelta``s to external forms.

* ``to_flagdata_lines`` -- CASA ``flagdata`` list-mode commands (one per
  line, apply with ``flagdata(vis=..., mode='list', inpfile=...)``).
* ``to_jsonl``          -- JSON Lines with full provenance (audit).

Fidelity rules
--------------
* **Region deltas** export as one selection each.  Extend options are
  folded into the selection itself (``extend_corr`` omits the correlation
  constraint, ``extend_chan`` the channels, ``extend_spw`` the spectral
  window, ``extend_scan`` replaces the time range by the scans) -- which is
  exactly what ``mode='extend'`` would do for such a selection, without a
  second pass.
* **Sample-set deltas** (anything a value condition or filter decided) are
  *materialized*: exported as explicit ``manual`` selections that name the
  exact samples, grouped into channel runs and merged across baselines and
  correlations where they coincide.  They are never exported as ``clip``
  or as a filter re-run, because re-evaluating a value condition against
  the MS (a different data column, other flags, another reference
  population) could select different samples than the user reviewed.
* **Time** is exported from the store's time scale (UNIX or MJD seconds,
  ``FlagDelta.time_format``) as a UTC calendar range padded by 1 ms, so the
  float round trip cannot drop an integration at either end.
* **Spectral windows**: a numeric SPW id is used when the store provides
  one (``SpwKey.kind == "spw"``) or the caller maps it (``spw_ids``).
  Otherwise the window is selected **by frequency** (``'*:f0~f1Hz'``),
  padded by a quarter channel so neighbouring channel centres are never
  included.  A DATA_DESC_ID is never written as an SPW id.
* **Order matters.**  Deltas are emitted in application order, flags as
  ``mode='manual'`` and unflags as ``mode='unflag'``.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/flag_export.py``
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Iterable, Mapping, Optional

import numpy as np

from .flag_model import FlagDelta, SpwKey, time_to_datetime

_TIME_PAD = 1e-3   # seconds


def casa_time(t: float, time_format: str = "unix") -> str:
    dt = time_to_datetime(t, time_format)
    return dt.strftime("%Y/%m/%d/%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def casa_timerange(t0: float, t1: float, time_format: str = "unix") -> str:
    return (f"{casa_time(t0 - _TIME_PAD, time_format)}~"
            f"{casa_time(t1 + _TIME_PAD, time_format)}")


def _quote(v: str) -> str:
    return "'" + str(v).replace("'", "\\'") + "'"


def _spw_id(key: SpwKey, spw_ids: Optional[Mapping]) -> Optional[str]:
    if spw_ids:
        for k in (key, str(key.ident)):
            try:
                if k in spw_ids:
                    return str(spw_ids[k])
            except TypeError:
                pass
    if key.kind == "spw":
        return str(key.ident)
    return None


def _channel_width(key: SpwKey) -> float:
    if key.n_chan > 1:
        return abs(key.freq_max - key.freq_min) / (key.n_chan - 1)
    return 0.0


def _freq_spec(f0: float, f1: float, pad: float) -> str:
    return f"{f0 - pad:.6f}~{f1 + pad:.6f}Hz"


def _spw_expr(key: SpwKey, chans: Optional[tuple], freqs: Optional[tuple],
              spw_ids: Optional[Mapping]) -> str:
    """One ``spw=`` term for window *key*, optionally limited to a channel
    run (``chans``) with its centre frequencies (``freqs``).

    Preference: numeric SPW id with channel indices; else the window *name*
    with a frequency range (``name:f0~f1Hz``); else a frequency range in
    every window (``*:f0~f1Hz``), which is only exact when no other window
    overlaps -- ``ambiguous_spws`` reports when it is not.
    """
    sid = _spw_id(key, spw_ids)
    pad = 0.25 * _channel_width(key)
    if sid is not None:
        if chans is None:
            return sid
        return f"{sid}:{int(chans[0])}~{int(chans[1])}"
    if freqs is None:
        freqs = (key.freq_min, key.freq_max)
    prefix = "*"
    if key.kind == "name" and str(key.ident) and not str(key.ident).startswith("<"):
        prefix = str(key.ident)
    return f"{prefix}:" + _freq_spec(min(freqs), max(freqs), pad)


def ambiguous_spws(keys: Iterable[SpwKey], spw_ids: Optional[Mapping] = None) -> list:
    """Windows that the exported ``spw=`` text cannot single out: no numeric
    id, and another window shares the name or (if unnamed) overlaps in
    frequency."""
    keys = list(keys)
    out = []
    for k in keys:
        if _spw_id(k, spw_ids) is not None:
            continue
        named = k.kind == "name" and str(k.ident) and not str(k.ident).startswith("<")
        for o in keys:
            if o is k:
                continue
            if named:
                clash = str(o.ident) == str(k.ident)
            else:
                clash = o.freq_min <= k.freq_max and k.freq_min <= o.freq_max
            if clash:
                out.append(k)
                break
    return out


def _antenna_expr(d: FlagDelta) -> Optional[str]:
    if d.baseline_ids is not None:
        return ";".join(f"{a}&{b}" for a, b in d.baseline_ids)
    if d.antenna_names is not None:
        return ",".join(str(a) for a in d.antenna_names)
    return None


def _mode(d: FlagDelta) -> str:
    return "manual" if d.flag else "unflag"


def _line(fields: dict) -> str:
    return " ".join(f"{k}={_quote(v)}" for k, v in fields.items() if v not in (None, ""))


def region_lines(d: FlagDelta, spw_ids: Optional[Mapping] = None,
                 reason: str = "") -> list:
    """``flagdata`` lines for a *region* delta (usually exactly one)."""
    base = {"mode": _mode(d)}
    if d.extend_scan and d.scan_names:
        base["scan"] = ",".join(d.scan_names)
    else:
        if d.time_range is not None:
            base["timerange"] = casa_timerange(*d.time_range, d.time_format)
        if d.scan_names is not None:
            base["scan"] = ",".join(d.scan_names)
    if d.field_names is not None:
        base["field"] = ",".join(d.field_names)
    ant = _antenna_expr(d)
    if ant:
        base["antenna"] = ant
    if d.correlation is not None and not d.extend_corr:
        base["correlation"] = ",".join(d.correlation)

    spw_terms = []
    if not d.extend_spw:
        if d.spw_channels is not None and not d.extend_chan:
            freqs_of = {}
            for sc in d.spw_channels:
                cw = _channel_width(sc.spw)
                f0 = sc.spw.freq_min + sc.chan_lo * cw if cw else sc.spw.freq_min
                f1 = sc.spw.freq_min + sc.chan_hi * cw if cw else sc.spw.freq_max
                spw_terms.append(_spw_expr(sc.spw, (sc.chan_lo, sc.chan_hi), (f0, f1), spw_ids))
        elif d.spw is not None:
            for k in d.spw:
                if d.freq_range is not None and not d.extend_chan:
                    lo = max(d.freq_range[0], k.freq_min)
                    hi = min(d.freq_range[1], k.freq_max)
                    if lo > hi:
                        continue
                    sid = _spw_id(k, spw_ids)
                    spec = _freq_spec(lo, hi, 0.25 * _channel_width(k))
                    spw_terms.append(f"{sid}:{spec}" if sid is not None else f"*:{spec}")
                elif d.channel_range is not None and not d.extend_chan:
                    spw_terms.append(_spw_expr(k, d.channel_range, None, spw_ids))
                else:
                    spw_terms.append(_spw_expr(k, None, None, spw_ids))
        elif d.freq_range is not None and not d.extend_chan:
            spw_terms.append("*:" + _freq_spec(d.freq_range[0], d.freq_range[1], 0.0))
    if spw_terms:
        base["spw"] = ",".join(spw_terms)
    if reason:
        base["reason"] = reason
    return [_line(base)]


def _runs(sorted_idx: np.ndarray) -> list:
    """Contiguous runs of integer positions: [(first, last), ...]."""
    if sorted_idx.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(sorted_idx) != 1)
    starts = np.concatenate([[0], breaks + 1])
    ends = np.concatenate([breaks, [sorted_idx.size - 1]])
    return [(int(sorted_idx[s]), int(sorted_idx[e])) for s, e in zip(starts, ends)]


def sample_lines(d: FlagDelta, spw_ids: Optional[Mapping] = None,
                 reason: str = "") -> list:
    """Materialized ``flagdata`` lines for a sample-set delta."""
    out = []
    for blk in d.samples:
        grid = blk.dense()
        if d.extend_chan:
            grid = np.broadcast_to(grid.any(axis=2, keepdims=True), grid.shape)
        if d.extend_corr:
            grid = np.broadcast_to(grid.any(axis=3, keepdims=True), grid.shape)
        # (time, pol-set, channel-run) -> [baselines]
        groups = defaultdict(list)
        nt, nb, nf, npol = grid.shape
        for it in range(nt):
            for ib in range(nb):
                g = grid[it, ib]
                if not g.any():
                    continue
                # per channel run, which correlations
                col_key = {}
                for ip in range(npol):
                    for run in _runs(np.flatnonzero(g[:, ip])):
                        col_key.setdefault(run, []).append(blk.pols[ip])
                for run, pols in col_key.items():
                    groups[(it, tuple(pols), run)].append(ib)
        for (it, pols, run), bls in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][2])):
            t = float(blk.times[it])
            fields = {"mode": _mode(d),
                      "timerange": casa_timerange(t, t, d.time_format),
                      "antenna": ";".join(f"{blk.ant1[b]}&{blk.ant2[b]}" for b in bls)}
            if not d.extend_chan:
                c0, c1 = run
                fields["spw"] = _spw_expr(blk.spw, (int(blk.chans[c0]), int(blk.chans[c1])),
                                          (float(blk.freqs[c0]), float(blk.freqs[c1])), spw_ids)
            else:
                fields["spw"] = _spw_expr(blk.spw, None, None, spw_ids)
            if pols is not None and not d.extend_corr:
                fields["correlation"] = ",".join(pols)
            if reason:
                fields["reason"] = reason
            out.append(_line(fields))
    return out


def _delta_spws(d: FlagDelta) -> list:
    keys = []
    if d.samples is not None:
        keys += [b.spw for b in d.samples]
    if d.spw is not None:
        keys += list(d.spw)
    if d.spw_channels is not None:
        keys += [sc.spw for sc in d.spw_channels]
    return keys


def to_flagdata_lines(deltas: Iterable[FlagDelta], *, spw_ids: Optional[Mapping] = None,
                      reason: str = "", comments: bool = True,
                      all_spws: Optional[Iterable[SpwKey]] = None) -> list:
    """All deltas as ordered ``flagdata`` list-mode lines.

    *spw_ids* optionally maps ``SpwKey`` (or ``str(SpwKey.ident)``) to the
    numeric CASA SPW id.  With ``comments=True`` each delta is preceded by a
    ``#`` line describing it (``flagdata`` list files accept comments).
    """
    deltas = list(deltas)
    lines = []
    if all_spws is not None:
        amb = ambiguous_spws(all_spws, spw_ids)
        used = [k for d in deltas for k in _delta_spws(d)]
        amb = [k for k in amb if any(k.matches(u) for u in used)]
        for k in amb:
            lines.append(f"# WARNING: spectral window {k.ident!s} ({k.n_chan} ch, "
                         f"{k.freq_min:.6g}-{k.freq_max:.6g} Hz) cannot be identified "
                         "uniquely without its SPW id; these lines may also select "
                         "other windows")
    for d in deltas:
        if comments:
            desc = [f"seq {d.seq}", d.verb]
            if d.source:
                desc.append(d.source)
            if d.filter is not None:
                desc.append(f"filter {d.filter.describe()}")
            for vr in d.value_ranges:
                desc.append(f"{vr.axis} in [{vr.lo:.6g}, {vr.hi:.6g}]"
                            + (f" ({vr.polarization})" if vr.polarization else ""))
            if d.provenance:
                desc.append(" -> ".join(d.provenance))
            lines.append("# " + "; ".join(desc))
        if d.is_sample_set:
            lines.extend(sample_lines(d, spw_ids, reason))
        else:
            lines.extend(region_lines(d, spw_ids, reason))
    return lines


def to_jsonl(deltas: Iterable[FlagDelta], header: Optional[dict] = None) -> str:
    from .flag_db import JSONL_FORMAT, JSONL_VERSION
    lines = [json.dumps({"format": JSONL_FORMAT, "version": JSONL_VERSION, **(header or {})})]
    lines += [json.dumps(d.to_dict(json_safe=True)) for d in deltas]
    return "\n".join(lines) + "\n"
