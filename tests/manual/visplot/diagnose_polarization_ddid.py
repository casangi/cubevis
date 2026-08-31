#!/usr/bin/env python
"""Standalone xarray-ms diagnostic — no cubevis import.

Purpose
-------
Check whether a KeyError like:

    KeyError: "not all values found in index 'polarization'. Try setting
    the `method` keyword argument (example: method='nearest')."

is caused by the MS having *different polarization products on different
DATA_DESC_IDs that share the same SPECTRAL_WINDOW_ID* (a data
characteristic, not a bug) versus a genuine xarray-ms indexing defect
(a coordinate value reported by ds.coords but not selectable via .sel()
on the very same partition).

Usage
-----
    python diagnose_polarization_ddid.py /path/to/3c84scan1.ms

Reads exactly like cubevis.MSv2Backend does: same engine name and
partition_schema, so the partitions you see here are the same ones
cubevis iterates.
"""
import sys

import xarray as xr

ENGINE = "xarray-ms:msv2"
PARTITION_SCHEMA = ["DATA_DESC_ID", "OBSERVATION_ID"]


def main(path: str) -> None:
    print(f"Opening {path!r} with engine={ENGINE!r}, "
          f"partition_schema={PARTITION_SCHEMA!r}\n")

    dt = xr.open_datatree(
        path,
        engine=ENGINE,
        partition_schema=PARTITION_SCHEMA,
    )

    partitions = []
    for node in dt.subtree:
        if not node.has_data:
            continue
        ds = node.ds
        if ds.sizes.get("time", 0) == 0:
            continue
        if not any(v in ds.data_vars for v in
                   ("VISIBILITY", "DATA", "CORRECTED_DATA", "MODEL_DATA")):
            continue
        partitions.append((node.path, ds))

    print(f"Found {len(partitions)} visibility partition(s)\n")

    # --- Per-partition identity + polarization contents ------------------
    all_pols_seen = set()
    per_partition_pols = []

    for path_str, ds in partitions:
        spw_id = ds.attrs.get("spectral_window_id")
        ddid = ds.attrs.get("DATA_DESC_ID")
        spw_name = None
        if "frequency" in ds.coords:
            spw_name = ds.coords["frequency"].attrs.get(
                "spectral_window_name")

        pols = None
        if "polarization" in ds.coords:
            pols = [str(p) for p in ds.coords["polarization"].values]
            all_pols_seen.update(pols)

        per_partition_pols.append(pols)

        print(f"partition: {path_str}")
        print(f"  attrs.spectral_window_id = {spw_id}")
        print(f"  attrs.DATA_DESC_ID       = {ddid}")
        print(f"  frequency spw name       = {spw_name}")
        print(f"  polarization coord       = {pols}")
        print()

    # --- Cross-partition comparison --------------------------------------
    distinct_pol_sets = {tuple(p) for p in per_partition_pols if p}
    print("=" * 70)
    print(f"Union of all polarization labels seen anywhere: "
          f"{sorted(all_pols_seen)}")
    print(f"Distinct per-partition polarization sets: {len(distinct_pol_sets)}")
    for s in distinct_pol_sets:
        print(f"  {s}")
    if len(distinct_pol_sets) > 1:
        print(
            "\n--> CONFIRMED: this MS has heterogeneous polarization sets "
            "across partitions (DDIDs). A single global 'first polarization' "
            "will not exist on every partition. This matches the cubevis "
            "hypothesis -- not an xarray-ms bug."
        )
    else:
        print(
            "\n--> All partitions report the SAME polarization set. If the "
            "original KeyError still reproduces here, that points at an "
            "xarray-ms indexing defect rather than a cubevis assumption bug."
        )
    print("=" * 70)
    print()

    # --- Direct .sel() probe: every (partition, pol-in-global-union) combo
    print("Per-partition .sel(polarization=<label>) probe "
          "(using every label seen anywhere in the MS):\n")
    for (path_str, ds), pols in zip(partitions, per_partition_pols):
        if "VISIBILITY" not in ds.data_vars and "DATA" not in ds.data_vars:
            continue
        vis = ds.get("VISIBILITY", ds.get("DATA"))
        for label in sorted(all_pols_seen):
            try:
                vis.sel(polarization=label)
                status = "OK"
            except KeyError as exc:
                status = f"KeyError: {exc}"
            local = "(local)" if pols and label in pols else "(NOT local)"
            print(f"  {path_str:30s} pol={label!r:6s} {local:12s} -> {status}")
        print()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} /path/to/ms", file=sys.stderr)
        sys.exit(1)
    main(sys.argv[1])
