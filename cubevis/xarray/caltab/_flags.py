"""Write a tree's flags back to where it was read from.

xarray backends are read-only, so writing is this module's::

    dt = open_caltab("cal.G")
    dt["spw_0"]["FLAG"].loc[dict(antenna_name="ea05")] = True     # edit the tree
    report = commit_flags(dt)                                      # make the table say the same

The tree is the statement of what the flags should be.  ``commit_flags``
finds where the store differs from it, writes exactly those cells, and
reads them back.  The steps are those of visplot's
``flag_commit.commit_msv2_arcae``:

1. **check** that the store is still the one the tree was read from
   (every row's time, antenna and window), or write nothing;
2. **plan** from a fresh read of the store's flags, so a cell the tree does
   not change is never written;
3. **back up** the previous flags of the rows to be written, in a side
   file next to the store (``<store>.caltab_flag_backup_<stamp>.npz``);
4. **write** those rows;
5. **read back** and compare with the plan (``verified``).

``restore_flags`` puts a backup's flags back; ``list_flag_backups`` finds
them.

Which store
-----------
The work is done by a *committer*, chosen by the tree's ``source_format``
attribute (``COMMITTERS``).  There is one, for CASA cal tables, written
with arcae.  An xradio calibration set will need its own -- a Zarr write,
with no table rows -- and registers here beside it: nothing above this
module should know which it got.  What a committer must do is the five
steps above and the report below.

CASA tables: one flag per receptor
----------------------------------
A row's FLAG cell has a value per parameter; the tree has one per
receptor (``_build``).  A changed tree cell sets all of that receptor's
parameters in the row; an unchanged one leaves them as they are, even if
they differ among themselves.  A time and antenna with no row
(``SOURCE_ROW`` -1) has nothing to write: it is counted
(``no_solution``) and stays flagged.

No Bokeh, no CASA.  arcae is imported when a table is written.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import time as _time
from typing import Any, Callable, Optional

import numpy as np

from .. import _arcae_util as au
from . import _schema as S

log = logging.getLogger("cubevis.xarray.caltab")

BACKUP_FORMAT = "cubevis.caltab.flag_backup"
BACKUP_TAG = ".caltab_flag_backup_"


class FlagCommitError(RuntimeError):
    """The flags were not written (the store is as it was)."""


# --- the CASA committer ----------------------------------------------------------------

def _stamp() -> str:
    return _time.strftime("%Y%m%d_%H%M%S")


def _backup_name(path: str) -> str:
    """A side file next to the table that does not exist yet."""
    base = f"{os.path.normpath(path)}{BACKUP_TAG}{_stamp()}"
    name, n = f"{base}.npz", 1
    while os.path.exists(name):                                      # two commits in one second
        n += 1
        name = f"{base}_{n}.npz"
    return name


def _nodes(tree) -> dict[int, Any]:
    return {int(n.attrs["spectral_window_id"]): n.to_dataset(inherit=False)
            for n in tree.children.values()}


def _check_rows(table, path: str, s: int, ds, rows: np.ndarray, ti: np.ndarray, ai: np.ndarray,
                antenna_names: list[str]) -> None:
    """The rows the tree points at are still that time, antenna and window."""
    if rows.size and rows.max() >= table.nrow():
        raise FlagCommitError(f"{path}: the table has {table.nrow()} rows but the tree points at row "
                              f"{int(rows.max())}; it is not the table the tree was read from")
    time = au.getcol_rows(table, "TIME", rows)
    ant = au.getcol_rows(table, "ANTENNA1", rows)
    spw = au.getcol_rows(table, "SPECTRAL_WINDOW_ID", rows)
    names = np.array([antenna_names[a] if 0 <= a < len(antenna_names) else f"antenna_{a}" for a in ant])
    same = ((time == ds[S.TIME].values[ti] + au.MJD_UNIX_OFFSET_S) & (spw == s)
            & (names == ds[S.ANTENNA].values[ai].astype(str)))
    if not same.all():
        raise FlagCommitError(
            f"{path}: {int((~same).sum())} row(s) of spectral window {s} are not the time and antenna "
            "the tree has for them; the table has changed since it was read.  Nothing was written: "
            "read it again and reapply the flags")


def _plan_casa(table, path: str, tree, antenna_names: list[str]) -> dict[str, Any]:
    """Where the table differs from the tree: per window, the rows to write
    and their flags before and after."""
    groups = []
    counts = {"flagged": 0, "unflagged": 0, "no_solution": 0, "cells": 0}
    for s, ds in sorted(_nodes(tree).items()):
        src = ds[S.SOURCE_ROW].values
        want = ds[S.FLAG].values[0]                                  # (time, antenna, chan, last)
        counts["no_solution"] += int((~want[src < 0]).sum())         # unflagged where there is no row
        ti, ai = np.nonzero(src >= 0)
        rows = src[ti, ai].astype(np.int64)
        if not rows.size:
            continue
        _check_rows(table, path, s, ds, rows, ti, ai, antenna_names)
        before = au.as_bool(au.getcol_rows(table, "FLAG", rows))     # (row, chan, par)
        want = want[ti, ai].astype(bool)                             # (row, chan, last)
        nchan, npar = before.shape[1:]
        if want.shape[1] != nchan:
            raise FlagCommitError(f"{path}: spectral window {s} has {nchan} channels in the table and "
                                  f"{want.shape[1]} in the tree")
        if S.RECEPTOR in ds.dims:                                    # one flag per receptor
            nrec = want.shape[2]
            if npar % nrec:
                raise FlagCommitError(f"{path}: {npar} parameters do not divide among {nrec} receptors")
            per = before.reshape(before.shape[0], nchan, nrec, npar // nrec)
            change = want != per.any(axis=-1)
            after = np.where(change[..., None], want[..., None], per).reshape(before.shape)
        else:
            if want.shape[2] != npar:
                raise FlagCommitError(f"{path}: {npar} parameters in the table, {want.shape[2]} in the tree")
            change = want != before
            after = want
        counts["flagged"] += int((change & want).sum())
        counts["unflagged"] += int((change & ~want).sum())
        touched = change.reshape(change.shape[0], -1).any(axis=1)
        if touched.any():
            counts["cells"] += int((after[touched] != before[touched]).sum())
            groups.append({"spw": s, "rows": rows[touched], "before": before[touched],
                           "after": after[touched]})
    return {"groups": groups, **counts}


def _append_history(path: str, message: str, params=()) -> bool:
    """One HISTORY row: an audit entry.  Best effort: the flags are written."""
    try:
        h = au.open_subtable(path, "HISTORY", readonly=False)
        try:
            n = h.nrow()
            h.addrows(1)
            idx = (np.array([n]),)
            h.putcol("TIME", np.array([_time.time() + au.MJD_UNIX_OFFSET_S]), index=idx)
            h.putcol("OBSERVATION_ID", np.array([0], dtype=np.int32), index=idx)
            h.putcol("MESSAGE", np.array([message]), index=idx)
            h.putcol("PRIORITY", np.array(["NORMAL"]), index=idx)
            h.putcol("ORIGIN", np.array(["cubevis.xarray.caltab"]), index=idx)
            h.putcol("APPLICATION", np.array(["cubevis"]), index=idx)
            h.putcol("CLI_COMMAND", np.array([[""]]), index=idx)
            h.putcol("APP_PARAMS", np.array([list(params) or [""]]), index=idx)
        finally:
            h.close()
        return True
    except Exception as exc:
        log.warning("could not add a HISTORY row to %s: %s", path, exc)
        return False


def _write_groups(path: str, groups: list[dict[str, Any]], key: str) -> int:
    """Write each group's ``key`` flags and read them back; the number of
    cells that do not read back as written."""
    mismatched = 0
    t = au.open_table(path, readonly=False)
    try:
        for g in groups:
            t.putcol("FLAG", g[key].astype(np.uint8), index=(g["rows"],))
    finally:
        t.close()
    t = au.open_table(path)                                          # a fresh handle: from disk
    try:
        for g in groups:
            got = au.as_bool(au.getcol_rows(t, "FLAG", g["rows"]))
            mismatched += int((got != g[key]).sum())
    finally:
        t.close()
    return mismatched


def _commit_casa(tree, path: str, *, backup: bool, backup_path: Optional[str], verify: bool,
                 dry_run: bool) -> dict[str, Any]:
    from ._reader import caltab_info                                 # raises unless a cal table
    info = caltab_info(path)
    path = info["path"]
    T: dict[str, float] = {}
    t0 = _time.perf_counter()
    ant = au.open_subtable(path, "ANTENNA")
    try:
        antenna_names = [str(n) for n in ant.getcol("NAME")] if ant.nrow() else []
    finally:
        ant.close()
    t = au.open_table(path)
    try:
        plan = _plan_casa(t, path, tree, antenna_names)
    finally:
        t.close()
    T["plan"] = _time.perf_counter() - t0
    groups = plan.pop("groups")
    nrows = int(sum(g["rows"].size for g in groups))
    report = {"format": "casa-caltab", "path": path, "dry_run": bool(dry_run),
              "flagged": plan["flagged"], "unflagged": plan["unflagged"],
              "no_solution": plan["no_solution"], "written": 0 if dry_run else plan["cells"],
              "to_write": plan["cells"], "rows": nrows, "backup": None, "history": False}
    if dry_run or not groups:                                        # nothing to do: no write, no backup
        report["timing"] = {k: round(v, 3) for k, v in T.items()}
        return report

    t0 = _time.perf_counter()
    if backup:
        backup_path = backup_path or _backup_name(path)
        arrays = {}
        for i, g in enumerate(groups):
            arrays[f"rows_{i}"], arrays[f"flag_{i}"] = g["rows"], g["before"]
        np.savez_compressed(backup_path, manifest=np.array(json.dumps({
            "format": BACKUP_FORMAT, "version": 1, "table": path, "created": _time.time(),
            "groups": len(groups), "spw": [g["spw"] for g in groups], "rows": nrows,
            "flagged": plan["flagged"], "unflagged": plan["unflagged"],
            "nrow": info["nrow"], "subtype": info["subType"]})), **arrays)
        report["backup"] = backup_path
    T["backup"] = _time.perf_counter() - t0

    t0 = _time.perf_counter()
    mismatched = _write_groups(path, groups, "after")
    T["write"] = _time.perf_counter() - t0
    if verify:
        report["verified"] = mismatched == 0
        report["mismatched"] = mismatched
    report["history"] = _append_history(
        path, f"cubevis wrote {plan['cells']} flag value(s) in {nrows} row(s): {plan['flagged']} "
              f"solution(s) flagged, {plan['unflagged']} unflagged"
              + (f"; previous flags saved in {os.path.basename(backup_path)}" if backup else ""),
        params=[f"backup={backup_path}" if backup else "backup=none",
                f"flagged={plan['flagged']}", f"unflagged={plan['unflagged']}"])
    report["timing"] = {k: round(v, 3) for k, v in T.items()}
    log.debug("caltab flag commit: %s", report)
    return report


def _restore_casa(path: str, backup_path: str) -> dict[str, Any]:
    from ._reader import caltab_info
    info = caltab_info(path)
    path = info["path"]
    with np.load(backup_path, allow_pickle=False) as z:
        manifest = json.loads(str(z["manifest"]))
        if manifest.get("format") != BACKUP_FORMAT:
            raise FlagCommitError(f"{backup_path}: not a caltab flag backup")
        groups = [{"rows": z[f"rows_{i}"], "before": z[f"flag_{i}"].astype(bool)}
                  for i in range(int(manifest["groups"]))]
    if manifest.get("nrow") != info["nrow"] or manifest.get("subtype") != info["subType"]:
        raise FlagCommitError(
            f"{backup_path} is of a {manifest.get('subtype')} table with {manifest.get('nrow')} rows; "
            f"{path} is a {info['subType']} table with {info['nrow']}.  Nothing was written")
    mismatched = _write_groups(path, groups, "before")
    nrows = int(sum(g["rows"].size for g in groups))
    history = _append_history(path, f"cubevis restored the flags of {nrows} row(s) from "
                                    f"{os.path.basename(backup_path)}", params=[f"backup={backup_path}"])
    return {"format": "casa-caltab", "path": path, "backup": backup_path, "restored_rows": nrows,
            "verified": mismatched == 0, "mismatched": mismatched, "history": history}


# --- which store ---------------------------------------------------------------------------

#: ``source_format`` -> (commit, restore).  A committer is
#: ``commit(tree, path, *, backup, backup_path, verify, dry_run) -> report``;
#: its report has at least ``format``, ``path``, ``flagged``, ``unflagged``,
#: ``no_solution``, ``written``, ``backup`` and (when asked) ``verified``.
COMMITTERS: dict[str, tuple[Callable[..., dict], Callable[..., dict]]] = {
    "casa-caltab": (_commit_casa, _restore_casa),
}


def _committer(source_format: Optional[str]):
    try:
        return COMMITTERS[source_format or ""]
    except KeyError:
        raise FlagCommitError(f"flags cannot be written to a {source_format!r} source "
                              f"(known: {', '.join(sorted(COMMITTERS))})") from None


def commit_flags(tree, path: Optional[str] = None, *, backup: bool = True,
                 backup_path: Optional[str] = None, verify: bool = True,
                 dry_run: bool = False) -> dict[str, Any]:
    """Make the store's flags those of *tree*.

    Parameters
    ----------
    tree
        A tree from ``open_caltab`` (or ``xr.open_datatree``) whose
        ``FLAG`` variables say what the flags should be.
    path
        The store; default: where the tree was read from (its ``source``).
    backup, backup_path
        Save the previous flags of the rows written in a side file next
        to the store (default), or at *backup_path*.
    verify
        Read the written rows back and compare (default).
    dry_run
        Report what would be written, and write nothing.

    Returns a report: ``flagged`` and ``unflagged`` (solutions of the
    tree whose flag changes), ``no_solution`` (cells unflagged in the
    tree where there is no solution: not written), ``written`` (values
    written to the store), ``rows``, ``backup`` (the side file, or None
    when nothing changed), ``verified``, ``history`` and ``timing``.

    Raises ``FlagCommitError``, with the store untouched, when the store
    is not the one the tree was read from.
    """
    source = path or tree.attrs.get("source")
    if not source or (path is None and str(source).startswith("synthetic:")):
        raise FlagCommitError("the tree was not read from a store; give the path to write to")
    commit, _ = _committer(tree.attrs.get("source_format"))
    return commit(tree, str(source), backup=backup, backup_path=backup_path, verify=verify,
                  dry_run=dry_run)


def restore_flags(backup_path: str, path: Optional[str] = None) -> dict[str, Any]:
    """Put back the flags a commit replaced, from its backup file.

    *path* defaults to the store the backup was made of.  Only the rows
    that commit wrote are restored, to what they were before it; a later
    commit's changes to other rows stay.
    """
    with np.load(backup_path, allow_pickle=False) as z:
        manifest = json.loads(str(z["manifest"]))
    _, restore = _committer("casa-caltab" if manifest.get("format") == BACKUP_FORMAT else None)
    return restore(str(path or manifest["table"]), backup_path)


def list_flag_backups(path: str) -> list[dict[str, Any]]:
    """The commit backups next to *path*, newest first:
    ``[{"path", "created", "rows", "flagged", "unflagged"}]``."""
    base = os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))
    out = []
    for p in glob.glob(f"{glob.escape(base)}{BACKUP_TAG}*.npz"):
        try:
            with np.load(p, allow_pickle=False) as z:
                m = json.loads(str(z["manifest"]))
            if m.get("format") != BACKUP_FORMAT:
                continue
            out.append({"path": p, "created": float(m.get("created", os.path.getmtime(p))),
                        "rows": int(m.get("rows", 0)), "flagged": int(m.get("flagged", 0)),
                        "unflagged": int(m.get("unflagged", 0))})
        except Exception:
            continue
    return sorted(out, key=lambda e: -e["created"])
