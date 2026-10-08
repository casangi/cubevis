"""flag_casa.py
================
OPTIONAL CASA-native flag writing (FlagDB v2): pending operations applied to
an MSv2 with ``casatasks.flagdata`` in list mode, after a ``flagmanager``
save -- for users who want flags written "the CASA way".

This is a SECONDARY path.  The default write (``flag_commit``, arcae) sets
exactly the samples visplot shows.  Through CASA's selection language the
same exact selections were applied incompletely in testing (TW Hya: 2,689 of
52,624 samples of one scattered operation were not flagged; reproducible with
plain ``flagdata``), so this path is:

* offered only when casatasks is importable (cheap detection at start-up --
  visplot never needs CASA otherwise);
* verified exactly like the default path: differences from what visplot
  showed are reported, never hidden;
* backed up both ways: a CASA flag version (``flagmanager``) and visplot's
  own side file of the rows the operations target.

The command generator below (moved here from ``flag_export``) emits exact
selections: each command is a cross product (time list x baselines x one
channel run x correlations) that contains only selected samples
(``test_flag_commit.py`` checks this under MSSelection semantics).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import time as _time
from collections import defaultdict
from typing import Iterable, Mapping, Optional

import numpy as np

from .flag_model import FlagDelta, SpwKey, time_to_datetime

log = logging.getLogger(__name__)

#: Commands per ``flagdata(mode='list')`` call (CASA combines a list's
#: selections into one data selection before applying it).
FLAGDATA_CHUNK = 500
#: Above this many commands the confirmation dialog warns explicitly.
LARGE_COMMAND_COUNT = 1000


def casa_detected() -> tuple:
    """``(ok, reason)`` from a CHEAP check (no import of casatools, which is
    slow and may be broken): are casatasks and casatools installed?"""
    for mod in ("casatasks", "casatools"):
        try:
            if importlib.util.find_spec(mod) is None:
                return False, f"{mod} is not installed"
        except Exception as exc:
            return False, f"{mod} cannot be located ({exc})"
    return True, ""


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
                 reason: str = "", own_reason: bool = True) -> list:
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
    # The user's own reason for this flag (HRS H5) wins over the
    # caller's blanket one.
    if own_reason:
        reason = getattr(d, "reason", "") or reason
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


#: Upper bound on time ranges per command line (keeps each MSSelection
#: expression a manageable size).
MAX_TIMES_PER_LINE = 200


def sample_lines(d: FlagDelta, spw_ids: Optional[Mapping] = None,
                 reason: str = "", own_reason: bool = True) -> list:
    """Materialized ``flagdata`` lines for a sample-set delta -- exact.

    Each command selects a cross product (times x baselines x channels x
    correlations), so samples are grouped such that every cross product is
    exactly a set of selected samples:

    1. per baseline and correlation set, contiguous runs of ACTUAL channel
       numbers (2026-09-30 fix: runs used to be taken over the trimmed grid,
       whose neighbouring columns need not be neighbouring channels, so a
       run could also flag the channels in between);
    2. the times sharing (baseline, correlations, channel run) become one
       comma-separated ``timerange`` list;
    3. baselines with identical (correlations, channel run, times) share one
       ``antenna`` list.

    The number of commands is then set by the structure of the selection,
    not by the number of samples (one command per sample before).
    """
    if own_reason:
        reason = getattr(d, "reason", "") or reason
    out = []
    for blk in d.samples:
        grid = blk.dense()
        if d.extend_chan:
            grid = np.broadcast_to(grid.any(axis=2, keepdims=True), grid.shape)
        if d.extend_corr:
            grid = np.broadcast_to(grid.any(axis=3, keepdims=True), grid.shape)
        chans = np.asarray(blk.chans, dtype=np.int64)
        freq_of = {int(c): float(f) for c, f in zip(chans, blk.freqs)}
        nt, nb, nf, npol = grid.shape
        # (baseline, pols, run) -> [time indices]
        per_bl = defaultdict(list)
        for it in range(nt):
            for ib in range(nb):
                g = grid[it, ib]
                if not g.any():
                    continue
                runs_for = defaultdict(list)                     # run -> pols
                for ip in range(npol):
                    sel = np.sort(chans[np.flatnonzero(g[:, ip])])
                    for run in _runs(sel):
                        runs_for[run].append(blk.pols[ip])
                for run, pols in runs_for.items():
                    per_bl[(ib, tuple(pols), run)].append(it)
        # (pols, run, times) -> [baselines]
        merged = defaultdict(list)
        for (ib, pols, run), its in per_bl.items():
            merged[(pols, run, tuple(its))].append(ib)
        for (pols, run, its), bls in sorted(merged.items(),
                                             key=lambda kv: (kv[0][2][0], kv[0][1])):
            for k in range(0, len(its), MAX_TIMES_PER_LINE):
                chunk = its[k:k + MAX_TIMES_PER_LINE]
                fields = {"mode": _mode(d),
                          "timerange": ",".join(
                              casa_timerange(float(blk.times[i]), float(blk.times[i]),
                                             d.time_format) for i in chunk),
                          "antenna": ";".join(f"{blk.ant1[b]}&{blk.ant2[b]}" for b in bls)}
                if not d.extend_chan:
                    c0, c1 = run
                    fields["spw"] = _spw_expr(blk.spw, (c0, c1),
                                              (freq_of.get(c0, blk.spw.freq_min),
                                               freq_of.get(c1, blk.spw.freq_max)), spw_ids)
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
                      all_spws: Optional[Iterable[SpwKey]] = None,
                      delta_reasons: bool = True) -> list:
    """All deltas as ordered ``flagdata`` list-mode lines.

    *spw_ids* optionally maps ``SpwKey`` (or ``str(SpwKey.ident)``) to the
    numeric CASA SPW id.  With ``comments=True`` each delta is preceded by a
    ``#`` line describing it (``flagdata`` list files accept comments).

    Each delta's own ``reason`` (what the user typed in the Flagging
    panel) is written as ``reason='...'`` unless *delta_reasons* is
    false; it takes precedence over the blanket *reason*.
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
            lines.extend(sample_lines(d, spw_ids, reason, delta_reasons))
        else:
            lines.extend(region_lines(d, spw_ids, reason, delta_reasons))
    return lines


# ======================================================================
# CASA commit
# ======================================================================

def commit_msv2_casa(backend, deltas, version_name: Optional[str] = None,
                     backup_path: Optional[str] = None, verify: bool = True) -> dict:
    """Write *deltas* with casatasks: visplot side-file backup of the target
    rows, ``flagmanager`` save, one or more ``flagdata(mode='list')`` calls
    per operation IN ORDER, HISTORY row, verification.  Cached frames are
    refreshed in place only when the result verified; otherwise the caller
    re-reads (the frames must show what is really on disk)."""
    from . import flag_commit as fc
    try:
        from casatasks import flagdata, flagmanager
    except Exception as exc:
        raise RuntimeError(f"casatasks could not be imported: {type(exc).__name__}: {exc}")
    from arcae.lib.arrow_tables import Table
    deltas = list(deltas)
    vis = backend._path
    stamp = _time.strftime("%Y%m%d_%H%M%S")
    version_name = version_name or f"visplot_{stamp}"
    backup_path = backup_path or f"{os.path.normpath(vis)}.visplot_flag_backup_{stamp}.npz"
    spw_ids = backend.spw_casa_ids()
    expected = fc._expected_changes(backend, deltas)
    n_expected = int(sum(int(((e != b) & v).sum()) for _s, _bda, b, e, v, _i in expected))
    # Without the users' reasons: they select nothing, and this path
    # stays the command set that was verified against CASA.  They are in
    # the exported command file, the JSON and the report.
    commands = [(d, to_flagdata_lines([d], spw_ids=spw_ids, comments=False,
                                      delta_reasons=False)) for d in deltas]
    report = {"format": "msv2", "method": "casa", "version_name": None, "backup": None,
              "operations": len(deltas), "expected_changes": n_expected,
              "commands": int(sum(len(c) for _d, c in commands)), "flagdata_calls": 0,
              "written": n_expected}
    if not n_expected:
        report.update(fc._verify(backend, expected) if verify else {})
        report["written"] = 0
        return report
    # visplot's own backup of the rows the operations target (exact restore
    # of those rows; the CASA flag version covers anything else CASA touches)
    ro = Table.from_filename(vis)
    try:
        lut = fc._row_lookup(ro)
    finally:
        ro.close()
    plan = fc._plan_msv2(backend, expected, fc._ms_tables(vis), lut)
    rows = np.array(sorted(plan), dtype=np.int64)
    snapshot = fc._snapshot_frames(backend)
    calls = 0
    backend.close()                 # release the read handle before CASA writes
    try:
        ro = Table.from_filename(vis)
        try:
            dd_all = np.asarray(ro.getcol("DATA_DESC_ID", index=(rows,)), dtype=np.int64)
            backup = {}
            groups = sorted(set(dd_all.tolist()))
            for g, dd in enumerate(groups):
                rr = rows[dd_all == dd]
                backup[f"rows_{g}"] = rr
                backup[f"flag_{g}"] = np.asarray(ro.getcol("FLAG", index=(rr,)))
                backup[f"flag_row_{g}"] = np.asarray(ro.getcol("FLAG_ROW", index=(rr,)))
        finally:
            ro.close()
        np.savez_compressed(backup_path, manifest=np.array(json.dumps({
            "format": "cubevis.visplot.flag_backup.msv2", "version": 1, "ms": vis,
            "created": _time.time(), "groups": len(groups), "method": "casa",
            "operations": [d.delta_id for d in deltas]})), **backup)
        report["backup"] = backup_path
        flagmanager(vis=vis, mode="save", versionname=version_name,
                    comment="before visplot pending flags (CASA flagdata write)",
                    merge="replace")
        report["version_name"] = version_name
        for d, cmds in commands:
            for k in range(0, len(cmds), FLAGDATA_CHUNK):
                flagdata(vis=vis, mode="list", inpfile=cmds[k:k + FLAGDATA_CHUNK],
                         flagbackup=False, action="apply")
                calls += 1
    finally:
        backend.open()
        backend._clear_lookup_caches()
    report["flagdata_calls"] = calls
    report["history"] = fc._append_history(
        vis, f"visplot applied {len(deltas)} pending operation(s) with casatasks.flagdata "
             f"({report['commands']} command(s) in {calls} call(s)); previous flags saved as "
             f"flag version {version_name} and {os.path.basename(backup_path)}",
        commands=[d.describe() for d in deltas][:50],
        params=[f"cubevis={fc._cubevis_version()}", f"backup={backup_path}",
                f"flag_version={version_name}", f"operations={len(deltas)}"])
    if verify:
        report.update(fc._verify(backend, expected))
    if report.get("verified", False):
        report["frames_refreshed"] = fc.refresh_cached_frames(backend, deltas, snapshot)
    return report
