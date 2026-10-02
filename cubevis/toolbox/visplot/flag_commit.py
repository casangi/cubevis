"""flag_commit.py
=================
Making pending flags permanent (FlagDB v2), and the export formats.

Runs where the data are (in-process, or in the remote worker through
``VisplotRemoteBackend``).  The plotter-side menu is in ``flag_controls``.

What each data format offers
----------------------------
=======================  =========  =========
action                   MSv2       MSv4
=======================  =========  =========
save flags as JSON        yes        yes      (P_local; ``FlagDB.to_jsonl``)
write flags to the data   arcae      zarr     (exact final state, verified)
load JSON as pending      yes        yes
restore a commit backup   yes        yes
=======================  =========  =========

MSv2: exact final-state write with arcae
-----------------------------------------
See the section note at ``commit_msv2_arcae``.  CASA ``flagdata`` writes
(and exported flagdata scripts) were removed on 2026-09-30: through CASA's
selection language a 52,624-sample operation on TW Hya came out 2,689
samples short, so they could silently differ from what visplot showed.
When casatools works, a CASA flag version is still saved before writing so
CASA users can restore with ``flagmanager``.

MSv4: zarr, with a side-file backup
-----------------------------------
MSv4 has no flag versions.  Only the samples whose state changes are
written (zarr coordinate writes into each partition's flag variable, in the
data group in use).  Their previous values are first saved in a side file
next to the store (``<store>.visplot_flag_backup_<timestamp>.npz``) --
exactly the changed samples, restorable with ``restore_msv4_backup``.
Integer (bit-field) flag variables keep their other bits: flagging sets
bit 0 only if the sample was unflagged; unflagging clears the value.
"""

from __future__ import annotations

import json
import logging
import os
import time as _time
from typing import Optional

import numpy as np

from .flag_model import FlagDelta

#: Commands per ``flagdata(mode='list')`` call.
FLAGDATA_CHUNK = 500

log = logging.getLogger(__name__)


# ======================================================================
# Capabilities
# ======================================================================

def data_format(backend) -> str:
    name = type(backend).__name__
    if "MSv4" in name:
        return "msv4"
    return "msv2"


def casatools_available() -> tuple:
    """``(ok, reason)``: can this process run ``casatasks.flagdata``?"""
    try:
        import casatasks  # noqa: F401
        from casatasks import flagdata, flagmanager  # noqa: F401
        return True, ""
    except Exception as exc:          # ImportError, or a broken binary install
        return False, f"casatools/casatasks not usable here ({type(exc).__name__}: {exc})"


def capabilities(backend) -> dict:
    """What the Export / commit menu may offer for this data.

    ``{"format": "msv2"|"msv4", "write": bool, "write_reason": str,
    "script": bool}`` -- ``write_reason`` explains a disabled write.
    """
    fmt = data_format(backend)
    path = getattr(backend, "_path", "") or ""
    if fmt == "msv2":
        writable = not path or os.access(path, os.W_OK)
        try:
            from arcae.lib.arrow_tables import Table  # noqa: F401
            ok, why = writable, ("" if writable else f"no write permission for {path}")
        except Exception as exc:
            ok, why = False, f"arcae not available ({exc})"
        from .flag_casa import casa_detected
        cok, cwhy = casa_detected()                  # cheap: no casatools import
        if cok and not writable:
            cok, cwhy = False, f"no write permission for {path}"
        return {"format": fmt, "write": ok, "write_reason": why,
                "casa_version": cok, "casa": cok, "casa_reason": cwhy}
    ok, why = True, ""
    try:
        import zarr  # noqa: F401
    except Exception as exc:
        ok, why = False, f"zarr not available ({exc})"
    if ok and path and not os.access(path, os.W_OK):
        ok, why = False, f"no write permission for {path}"
    return {"format": fmt, "write": ok, "write_reason": why, "script": False}


# ======================================================================
# Expected result (the same fold the display uses)
# ======================================================================

def _expected_changes(backend, deltas) -> list:
    """Per touched partition: ``(ds_sub, base_da, base, eff, valid, info)``.

    Only the (time, baseline) rectangle the operations touch is read
    (``ds_sub``; ``info`` holds the indices into the full partition and its
    full time/frequency coordinates, to find it again after reopening).
    Partitions no operation touches are skipped from coordinates alone.
    2026-10-02: a few-sample commit on all of TW Hya took ~18 s, most of it
    reading and folding the FLAG column of whole partitions (twice, with
    the verification).  Arrays are in the FLAG variable's own dim order."""
    from .flag_engine import apply_pending, valid_mask, _bdim, block_coords
    from .flag_model import delta_mask
    out = []
    bd = _bdim(backend)
    for ds in backend._iter_visibility_partitions(None):
        try:
            bc = block_coords(backend, ds)
            um = None
            for d in deltas:
                m = delta_mask(d, bc)
                if m is None or not np.any(m):
                    continue
                tb = np.asarray(m).any(axis=(2, 3))
                um = tb if um is None else (um | tb)
        except Exception:
            um = np.ones((ds.sizes["time"], ds.sizes[bd]), dtype=bool)   # be safe
        if um is None or not um.any():
            continue
        ti = np.flatnonzero(um.any(axis=1))
        bi = np.flatnonzero(um.any(axis=0))
        sub = ds.isel({"time": ti, bd: bi})
        base_da = backend._disk_flag_mask(sub)
        eff_da = apply_pending(backend, sub, base_da, tuple(deltas))
        base = np.asarray(base_da.values, dtype=bool)
        eff = np.asarray(eff_da.values, dtype=bool)
        if np.array_equal(base, eff):
            continue
        v = valid_mask(backend, sub)
        if v is not None:
            import xarray as xr
            valid = np.asarray(xr.DataArray(np.asarray(v, dtype=bool), dims=("time", bd))
                               .broadcast_like(base_da).transpose(*base_da.dims).values)
        else:
            valid = np.ones(base.shape, dtype=bool)
        info = {"ti": ti, "bi": bi, "bdim": bd,
                "full_time": np.asarray(ds.coords["time"].values),
                "full_freq": np.asarray(ds.coords["frequency"].values)}
        out.append((sub, base_da, base, eff, valid, info))
    return out


def _same_partition(ds, info) -> bool:
    return (ds.sizes.get("time") == info["full_time"].size
            and np.array_equal(ds.coords["time"].values, info["full_time"])
            and np.array_equal(ds.coords["frequency"].values, info["full_freq"]))


def _verify(backend, expected) -> dict:
    """Compare the flags now on disk with the expected effective flags
    (only the touched rectangle of each touched partition is read)."""
    wrong = checked = 0
    by_kind = {"should_be_flagged": 0, "should_be_unflagged": 0, "collateral": 0}
    parts = list(backend._iter_visibility_partitions(None))
    for _sub, bda, base, eff, valid, info in expected:
        match = next((ds for ds in parts if _same_partition(ds, info)), None)
        if match is None:
            raise RuntimeError("verification: a partition could not be matched after reopen")
        sub = match.isel({"time": info["ti"], info["bdim"]: info["bi"]})
        now = np.asarray(backend._disk_flag_mask(sub).transpose(*bda.dims).values, dtype=bool)
        diff = (now != eff) & valid
        checked += int(valid.sum())
        wrong += int(diff.sum())
        by_kind["should_be_flagged"] += int((diff & eff).sum())
        by_kind["should_be_unflagged"] += int((diff & ~eff & (base != eff)).sum())
        by_kind["collateral"] += int((diff & (base == eff)).sum())
    return {"verified": wrong == 0, "mismatches": wrong, "checked": checked,
            "mismatch_kinds": by_kind}


# ======================================================================
# MSv2 -- exact final-state write with arcae (no CASA selection syntax)
# ======================================================================
#
# 2026-09-30: writing through flagdata left 2,689 of 52,624 samples of one
# operation unflagged on TW Hya (caught by the verification), although the
# exported selections are exact under MSSelection semantics (emulator test).
# This path avoids the selection language altogether: the final flag of
# every changed sample is computed by the same engine that drives the
# display and written into exactly its MS row/channel/correlation with
# arcae (casacore underneath), after saving the previous values to a side
# file.  FLAG_ROW is kept consistent (True iff every flag of the row is).

_STOKES = {1: "I", 2: "Q", 3: "U", 4: "V", 5: "RR", 6: "RL", 7: "LR", 8: "LL",
           9: "XX", 10: "XY", 11: "YX", 12: "YY", 13: "RX", 14: "RY", 15: "LX",
           16: "LY", 17: "XR", 18: "XL", 19: "YR", 20: "YL", 21: "PP", 22: "PQ",
           23: "QP", 24: "QQ"}
_MJD_UNIX_OFFSET = 40587.0 * 86400.0


def _ms_tables(ms: str) -> dict:
    from arcae.lib.arrow_tables import Table
    ant = [str(n) for n in Table.from_filename(f"{ms}::ANTENNA").to_arrow(columns=["NAME"])["NAME"].to_pylist()]
    dd = Table.from_filename(f"{ms}::DATA_DESCRIPTION").to_arrow(
        columns=["SPECTRAL_WINDOW_ID", "POLARIZATION_ID"])
    pol = Table.from_filename(f"{ms}::POLARIZATION").to_arrow(columns=["CORR_TYPE"])
    corr = [[_STOKES.get(int(c), str(c)) for c in row] for row in pol["CORR_TYPE"].to_pylist()]
    ddid = [(int(s), corr[int(p)]) for s, p in zip(dd["SPECTRAL_WINDOW_ID"].to_pylist(),
                                                    dd["POLARIZATION_ID"].to_pylist())]
    return {"antenna": {n: i for i, n in enumerate(ant)}, "ddid": ddid}


def _row_lookup(main) -> dict:
    """``{(ddid, time_us, ant1, ant2): [rows]}`` for the whole main table."""
    t = np.asarray(main.getcol("TIME"), dtype=np.float64)
    a1 = np.asarray(main.getcol("ANTENNA1"), dtype=np.int64)
    a2 = np.asarray(main.getcol("ANTENNA2"), dtype=np.int64)
    dd = np.asarray(main.getcol("DATA_DESC_ID"), dtype=np.int64)
    tus = np.rint(t * 1e6).astype(np.int64)
    lut: dict = {}
    for r, key in enumerate(zip(dd.tolist(), tus.tolist(), a1.tolist(), a2.tolist())):
        lut.setdefault(key, []).append(r)
    return lut


def _plan_msv2(backend, expected, tables, lut) -> dict:
    """``{row: [(chan, corr_index, new_flag)]}`` for every changed sample."""
    from .flag_engine import block_coords
    from .flag_model import TIME_TOL
    spw_ids = backend.spw_casa_ids()
    plan: dict = {}
    for ds, bda, base, eff, valid, _info in expected:
        bc = block_coords(backend, ds)
        dims = list(bda.dims)
        change = (base != eff) & valid
        if not change.any():
            continue
        sid = spw_ids.get(bc.spw)
        if sid is None:
            raise RuntimeError(f"arcae commit: spectral window {bc.spw.ident} has no unique "
                               "SPECTRAL_WINDOW row")
        pols = [str(p) for p in bc.pols]
        cands = [k for k, (s_, corr) in enumerate(tables["ddid"])
                 if s_ == sid and all(p in corr for p in pols)]
        if not cands:
            raise RuntimeError(f"arcae commit: no DATA_DESCRIPTION for spw {sid} with {pols}")
        idx = np.nonzero(change)
        coord = {d: idx[k] for k, d in enumerate(dims)}
        it, ib = coord["time"], coord[[d for d in dims if d not in
                                       ("time", "frequency", "polarization")][0]]
        ifr, ip = coord["frequency"], coord["polarization"]
        tfmt = str(ds.coords["time"].attrs.get("format", "unix")).lower()
        new = eff[idx]
        ant = tables["antenna"]
        for k in range(it.size):
            t = float(bc.times[it[k]])
            t_mjd = t if "mjd" in tfmt else t + _MJD_UNIX_OFFSET
            a1, a2 = ant[str(bc.ant1[ib[k]])], ant[str(bc.ant2[ib[k]])]
            rows = None
            for dd in cands:
                for key in ((dd, int(round(t_mjd * 1e6)), a1, a2),
                            (dd, int(round(t_mjd * 1e6)), a2, a1)):
                    rows = lut.get(key)
                    if rows:
                        corr = tables["ddid"][dd][1]
                        break
                if rows:
                    break
            if not rows:
                raise RuntimeError("arcae commit: no MS row for a changed sample "
                                   f"(time {t_mjd}, {bc.ant1[ib[k]]}&{bc.ant2[ib[k]]}, spw {sid})")
            if len(rows) != 1:
                raise RuntimeError(f"arcae commit: {len(rows)} MS rows share one sample's "
                                   "time/baseline/data description -- refusing to guess")
            c = corr.index(pols[ip[k]])
            plan.setdefault(rows[0], []).append((int(bc.chans[ifr[k]]), c, bool(new[k])))
    return plan


def _append_history(ms: str, message: str, commands=(), params=()) -> bool:
    """One HISTORY row (MSv2): a small audit entry, nothing else grows."""
    try:
        from arcae.lib.arrow_tables import Table
        h = Table.from_filename(f"{ms}::HISTORY", readonly=False)
        try:
            n = h.nrow()
            h.addrows(1)
            idx = (np.array([n]),)
            h.putcol("TIME", np.array([_time.time() + _MJD_UNIX_OFFSET]), index=idx)
            h.putcol("OBSERVATION_ID", np.array([0], dtype=np.int32), index=idx)
            h.putcol("MESSAGE", np.array([message]), index=idx)
            h.putcol("PRIORITY", np.array(["NORMAL"]), index=idx)
            h.putcol("ORIGIN", np.array(["cubevis.visplot"]), index=idx)
            h.putcol("APPLICATION", np.array(["visplot"]), index=idx)
            h.putcol("CLI_COMMAND", np.array([list(commands) or [""]]), index=idx)
            h.putcol("APP_PARAMS", np.array([list(params) or [""]]), index=idx)
        finally:
            h.close()
        return True
    except Exception as exc:             # the flags are written; history is best effort
        log.warning("could not add a HISTORY row to %s: %s", ms, exc)
        return False


def _cubevis_version() -> str:
    try:
        import cubevis
        return str(getattr(cubevis, "__version__", "") or "unknown")
    except Exception:
        return "unknown"


def commit_msv2_arcae(backend, deltas, backup_path: Optional[str] = None,
                      verify: bool = True) -> dict:
    """Write *deltas* into the MS with arcae (see the section note)."""
    from arcae.lib.arrow_tables import Table
    deltas = list(deltas)
    ms = backend._path
    stamp = _time.strftime("%Y%m%d_%H%M%S")
    backup_path = backup_path or f"{os.path.normpath(ms)}.visplot_flag_backup_{stamp}.npz"
    T = {}
    t0 = _time.perf_counter()
    expected = _expected_changes(backend, deltas)
    T["expected"] = _time.perf_counter() - t0
    tables = _ms_tables(ms)
    t0 = _time.perf_counter()
    ro = Table.from_filename(ms)                     # plan while the backend is open
    try:
        lut = _row_lookup(ro)
    finally:
        ro.close()
    plan = _plan_msv2(backend, expected, tables, lut)
    rows = np.array(sorted(plan), dtype=np.int64)
    T["rows"] = _time.perf_counter() - t0
    n = 0
    version_name = None
    snapshot = _snapshot_frames(backend)
    t0 = _time.perf_counter()
    backend.close()                     # release the read handle before writing
    try:
        ok, _why = casatools_available()
        if ok and rows.size:
            # Also a CASA flag version, so CASA users can restore with
            # flagmanager; the side file is the authoritative backup.
            try:
                from casatasks import flagmanager
                version_name = f"visplot_{stamp}"
                flagmanager(vis=ms, mode="save", versionname=version_name,
                            comment="before visplot pending flags (arcae write)",
                            merge="replace")
            except Exception as exc:  # pragma: no cover - environment dependent
                log.warning("flagmanager save failed (%s); side-file backup only", exc)
                version_name = None
        main = Table.from_filename(ms, readonly=False)
        try:
            # FLAG is variably shaped across data descriptions: read, edit and
            # write one data description (one shape) at a time.
            dd_all = np.asarray(main.getcol("DATA_DESC_ID", index=(rows,)), dtype=np.int64) \
                if rows.size else np.zeros(0, np.int64)
            backup = {}
            groups = sorted(set(dd_all.tolist()))
            for g, dd in enumerate(groups):
                rr = rows[dd_all == dd]
                flags = np.asarray(main.getcol("FLAG", index=(rr,))).copy()
                frow = np.asarray(main.getcol("FLAG_ROW", index=(rr,))).copy()
                backup[f"rows_{g}"], backup[f"flag_{g}"], backup[f"flag_row_{g}"] = \
                    rr, flags.copy(), frow.copy()
                for k, r in enumerate(rr.tolist()):
                    for ch, c, v in plan[r]:
                        flags[k, ch, c] = v
                        n += 1
                main.putcol("FLAG", flags, index=(rr,))
                new_frow = flags.reshape(flags.shape[0], -1).all(axis=1).astype(frow.dtype)
                main.putcol("FLAG_ROW", new_frow, index=(rr,))
                if g == 0:
                    pass
            if groups:
                np.savez_compressed(
                    backup_path,
                    manifest=np.array(json.dumps({
                        "format": "cubevis.visplot.flag_backup.msv2", "version": 1, "ms": ms,
                        "created": _time.time(), "groups": len(groups),
                        "operations": [d.delta_id for d in deltas]})),
                    **backup)
        finally:
            main.close()
    finally:
        backend.open()
        backend._clear_lookup_caches()
    history = False
    if n:
        ops = [d.describe() for d in deltas]
        history = _append_history(
            ms, f"visplot wrote {n} flag change(s) in {rows.size} row(s) from "
                f"{len(deltas)} pending operation(s); previous flags saved in "
                f"{os.path.basename(backup_path)}",
            commands=ops[:50] + ([f"... {len(ops) - 50} more"] if len(ops) > 50 else []),
            params=[f"cubevis={_cubevis_version()}", f"backup={backup_path}",
                    f"operations={len(deltas)}", f"samples={n}"])
    T["write+reopen"] = _time.perf_counter() - t0
    if not n:                      # nothing changed: no write, no backup file
        backup_path = None
    report = {"format": "msv2", "method": "arcae", "backup": backup_path,
              "version_name": version_name, "history": history,
              "operations": len(deltas), "written": n, "rows": int(rows.size),
              "expected_changes": n}
    t0 = _time.perf_counter()
    if verify:
        report.update(_verify(backend, expected))
    T["verify"] = _time.perf_counter() - t0
    t0 = _time.perf_counter()
    if n and report.get("verified", False):
        report["frames_refreshed"] = refresh_cached_frames(backend, deltas, snapshot)
    T["refresh_frames"] = _time.perf_counter() - t0
    report["timing"] = {k: round(v, 3) for k, v in T.items()}
    log.info("visplot commit (arcae): %s", report["timing"])
    return report


def restore_msv2_backup(backend, backup_path: str) -> dict:
    from arcae.lib.arrow_tables import Table
    with np.load(backup_path, allow_pickle=False) as z:
        ng = int(json.loads(str(z["manifest"])).get("groups", 0))
        parts = [(z[f"rows_{g}"], z[f"flag_{g}"], z[f"flag_row_{g}"]) for g in range(ng)]
    backend.close()
    n_rows = 0
    try:
        main = Table.from_filename(backend._path, readonly=False)
        try:
            for rows, flag, frow in parts:
                main.putcol("FLAG", flag, index=(rows,))
                main.putcol("FLAG_ROW", frow, index=(rows,))
                n_rows += int(rows.size)
        finally:
            main.close()
    finally:
        backend.open()
        backend._clear_lookup_caches()
    _append_history(backend._path, f"visplot restored the flags of {n_rows} row(s) from "
                                   f"{os.path.basename(backup_path)}",
                    params=[f"cubevis={_cubevis_version()}", f"backup={backup_path}"])
    return {"restored_rows": n_rows, "backup": backup_path}


# ======================================================================
# MSv4 -- zarr writes with a side-file backup
# ======================================================================

def _partition_nodes(backend) -> list:
    """``[(zarr_group_path, flag_var_name, ds)]`` for every visibility
    partition, matched to the backend's cached partition datasets."""
    dt = backend._require_open() if hasattr(backend, "_require_open") else backend._datatree
    out = []
    for node in dt.subtree:
        if not node.has_data:
            continue
        ds = node.to_dataset()
        group = backend._get_data_group(ds) if hasattr(backend, "_get_data_group") else None
        fname = group["flag"] if group and "flag" in group else "FLAG"
        if fname in ds.data_vars and ("VISIBILITY" in ds.data_vars
                                      or ds.attrs.get("type") == "visibility"
                                      or (group and group.get("correlated_data") in ds.data_vars)):
            out.append((node.path.strip("/"), fname, ds))
    return out


def commit_msv4(backend, deltas, backup_path: Optional[str] = None,
                verify: bool = True) -> dict:
    """Write *deltas* into the PS (see module docstring)."""
    import zarr
    deltas = list(deltas)
    store = backend._path
    stamp = _time.strftime("%Y%m%d_%H%M%S")
    backup_path = backup_path or f"{os.path.normpath(store)}.visplot_flag_backup_{stamp}.npz"
    expected = _expected_changes(backend, deltas)
    nodes = _partition_nodes(backend)
    plan = []                                          # (group, var, idx tuple, new raw)
    backup = {}
    for _sub, bda, base, eff, valid, info in expected:
        node = next(((g, f, d) for g, f, d in nodes if _same_partition(d, info)), None)
        if node is None:
            raise RuntimeError("commit: could not locate a partition's zarr group")
        gpath, fname, ds_n = node
        change = (base != eff) & valid
        if not change.any():
            continue
        dims = list(bda.dims)
        idx_sub = list(np.nonzero(change))
        full = list(idx_sub)
        full[dims.index("time")] = info["ti"][idx_sub[dims.index("time")]]
        full[dims.index(info["bdim"])] = info["bi"][idx_sub[dims.index(info["bdim"])]]
        new_vals = eff[tuple(idx_sub)]
        raw_dims = list(ds_n[fname].dims)
        idx = tuple(full[dims.index(d)] for d in raw_dims)
        plan.append((gpath, fname, idx, new_vals))
    if not plan:                   # nothing changes: no write, no backup file
        report = {"format": "msv4", "backup": None, "operations": len(deltas),
                  "written": 0, "expected_changes": 0}
        if verify:
            report.update(_verify(backend, expected))
        return report
    root = zarr.open_group(store, mode="r+")
    # 1. backup (previous raw values of exactly the samples that change)
    arrays = {}
    manifest = {"format": "cubevis.visplot.flag_backup", "version": 1, "store": store,
                "created": _time.time(), "operations": [d.delta_id for d in deltas],
                "entries": []}
    for k, (gpath, fname, idx, _new) in enumerate(plan):
        arr = root[f"{gpath}/{fname}" if gpath else fname]
        prev = np.asarray(arr.vindex[idx])
        arrays[f"idx_{k}"] = np.stack(idx).astype(np.int64)
        arrays[f"prev_{k}"] = prev
        manifest["entries"].append({"group": gpath, "var": fname, "dtype": str(prev.dtype),
                                    "n": int(prev.size)})
    np.savez_compressed(backup_path, manifest=np.array(json.dumps(manifest)), **arrays)
    # 2. write
    n = 0
    for k, (gpath, fname, idx, new) in enumerate(plan):
        arr = root[f"{gpath}/{fname}" if gpath else fname]
        prev = arrays[f"prev_{k}"]
        if prev.dtype == bool:
            vals = new.astype(bool)
        else:
            vals = np.where(new, np.where(prev != 0, prev, 1), 0).astype(prev.dtype)
        arr.vindex[idx] = vals
        n += int(np.asarray(new).size)
    snapshot = _snapshot_frames(backend)
    backend.close()
    backend.open()
    backend._clear_lookup_caches()
    report = {"format": "msv4", "backup": backup_path, "operations": len(deltas),
              "written": n, "expected_changes": n}
    if verify:
        report.update(_verify(backend, expected))
    if n and report.get("verified", False):
        report["frames_refreshed"] = refresh_cached_frames(backend, deltas, snapshot)
    return report


def restore_backup(backend, backup_path: str) -> dict:
    """Undo an arcae (MSv2) or zarr (MSv4) commit from its side file."""
    with np.load(backup_path, allow_pickle=False) as z:
        fmt = json.loads(str(z["manifest"])).get("format", "")
    if fmt.endswith(".msv2"):
        return restore_msv2_backup(backend, backup_path)
    return restore_msv4_backup(backend, backup_path)


def restore_msv4_backup(backend, backup_path: str) -> dict:
    """Undo a ``commit_msv4``: write the saved previous values back."""
    import zarr
    with np.load(backup_path, allow_pickle=False) as z:
        manifest = json.loads(str(z["manifest"]))
        root = zarr.open_group(backend._path, mode="r+")
        n = 0
        for k, e in enumerate(manifest["entries"]):
            arr = root[f"{e['group']}/{e['var']}" if e["group"] else e["var"]]
            idx = tuple(z[f"idx_{k}"])
            arr.vindex[idx] = z[f"prev_{k}"]
            n += int(e["n"])
    backend.close()
    backend.open()
    backend._clear_lookup_caches()
    return {"restored": n, "backup": backup_path}


def commit(backend, deltas, method: Optional[str] = None, **kw) -> dict:
    """Dispatch on the data format: MSv2 -> arcae (default) or, with
    ``method="casa"``, the optional CASA flagdata path (``flag_casa``);
    MSv4 -> zarr."""
    fmt = data_format(backend)
    if fmt == "msv2" and method == "casa":
        from .flag_casa import commit_msv2_casa
        return commit_msv2_casa(backend, deltas, **{k: v for k, v in kw.items()
                                                   if k in ("version_name", "backup_path",
                                                            "verify")})
    if fmt == "msv2":
        return commit_msv2_arcae(backend, deltas, **{k: v for k, v in kw.items()
                                                     if k in ("backup_path", "verify")})
    return commit_msv4(backend, deltas, **{k: v for k, v in kw.items()
                                           if k in ("backup_path", "verify")})


def list_backups(backend) -> list:
    """Commit backups next to the data, newest first:
    ``[{"path", "created", "operations", "kind"}]``."""
    import glob
    base = os.path.normpath(backend._path)
    out = []
    for p in glob.glob(f"{glob.escape(base)}.visplot_flag_backup_*.npz"):
        try:
            with np.load(p, allow_pickle=False) as z:
                m = json.loads(str(z["manifest"]))
            out.append({"path": p, "created": float(m.get("created", os.path.getmtime(p))),
                        "operations": len(m.get("operations", [])),
                        "kind": "msv2" if m.get("format", "").endswith(".msv2") else "msv4"})
        except Exception:
            continue
    return sorted(out, key=lambda e: -e["created"])


def _snapshot_frames(backend) -> list:
    """``[(key, generation, raw frame)]`` held by *backend*'s frame cache --
    taken before a commit closes the backend (closing drops them)."""
    cache = backend._frame_cache_obj()
    with cache.lock:
        return [(k, e[0], e[2]) for k, e in cache._d.items()
                if hasattr(e[2], "columns") and "__disk_flag" in e[2].columns]


def refresh_cached_frames(backend, deltas, snapshot) -> int:
    """After a VERIFIED commit: put the raw scatter frames cached before the
    commit back, brought up to date, instead of re-reading them.

    Each raw frame row carries its on-disk flag (``__disk_flag``).  The new
    on-disk state of every row is the old one with the committed operations
    folded in -- the same fold ``flag_engine`` uses for the display, and the
    one the write was just verified against -- so that column is replaced
    and the frame's derived caches are dropped.  Without this the redraw
    after a commit re-read every cached selection from disk (seconds for a
    full data set, however few samples were written).  Returns the number
    of frames restored."""
    from . import flag_engine as fe
    cache = backend._frame_cache_obj()
    n = 0
    for key, gen, df in snapshot:
        slot = fe._FRAME_SLOTS.get(id(df))
        if slot is not None:                          # row identity stays valid;
            slot["eff"].clear()                       # effective states do not
        pol = key[3] if len(key) > 3 else None
        disk = df["__disk_flag"].to_numpy(dtype=bool)
        new = disk.copy()
        R = fe._rows_for(backend, df, pol)
        for d in deltas:
            new[fe.row_delta_mask(d, R)] = bool(d.flag)
        df["__disk_flag"] = new
        if slot is not None:
            slot["eff"].clear()
        with cache.lock:
            cache.put(key, gen, df)
        n += 1
    backend.__dict__.pop("_cv_view_memo", None)       # filtered views of the old state
    return n
