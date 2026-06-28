"""create_test_msv4.py — Convert an MSv2 file to a ps.zarr Processing Set.

Creates the test dataset required by ``test_msv4_backend.py`` using
``xarray-ms`` (not xradio), keeping the entire toolchain within the
xarray-ms ecosystem.

Usage
-----
    MS=sis14_twhya_calibrated_flagged.ms python create_test_msv4.py

Output
------
    sis14_twhya_calibrated_flagged.ps.zarr   (in the current directory)

The script auto-detects the MS path from the ``MS`` environment variable
(defaulting to ``sis14_twhya_calibrated_flagged.ms`` in the working directory).

Why xarray-ms, not xradio?
--------------------------
The ``MSv4Backend`` reads Zarr stores with ``xr.open_datatree(engine="zarr")``,
which is xarray's own engine — no xradio dependency required.  Since
``xarray-ms`` already opens MSv2 files into a fully MSv4-compliant DataTree,
``DataTree.to_zarr()`` produces a store with the exact same dimension names,
coordinate names, and attribute schema that ``MSv4Backend`` expects.

Chunk sizes
-----------
The default chunks match ``MSv2Backend._DEFAULT_CHUNKS`` so that the Zarr
store layout is aligned with the read-back chunk specification.
"""

from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path


def main() -> None:
    ms_path = os.environ.get("MS", "sis14_twhya_calibrated_flagged.ms")
    if not os.path.isdir(ms_path):
        sys.exit(
            f"ERROR: MS not found at {ms_path!r}.\n"
            "Download from:\n"
            "  https://casa.nrao.edu/download/devel/casavis/data/"
            "sis14_twhya_calibrated_flagged.ms.tar.gz\n"
            "Or set the MS environment variable."
        )

    out_path = Path(ms_path).stem + ".ps.zarr"

    print(f"Input  : {ms_path}")
    print(f"Output : {out_path}")

    warnings.filterwarnings("ignore", category=UserWarning, module="xarray_ms")
    warnings.filterwarnings("ignore", message="The return type of.*Dataset.dims",
                            category=FutureWarning)
    warnings.filterwarnings("ignore", message="omp_set_nested")

    try:
        import xarray as xr
        import xarray_ms  # noqa: F401 — registers the xarray-ms:msv2 engine
    except ImportError as exc:
        sys.exit(f"ERROR: {exc}\nInstall: pip install xarray-ms")

    try:
        import zarr  # noqa: F401
    except ImportError as exc:
        sys.exit(f"ERROR: {exc}\nInstall: pip install 'zarr>=2.10'")

    chunks = {"time": 100, "baseline_id": 100}
    partition_schema = ["DATA_DESC_ID", "OBSERVATION_ID"]

    print(f"Opening MSv2 with partition_schema={partition_schema} ...")
    try:
        dt = xr.open_datatree(
            ms_path,
            engine="xarray-ms:msv2",
            partition_schema=partition_schema,
            chunks=chunks,
        )
    except Exception as exc:
        sys.exit(f"ERROR opening MSv2: {exc}")

    # Count visibility partitions for the status message
    n_parts = sum(
        1 for node in dt.subtree
        if node.has_data and node.ds.sizes.get("time", 0) > 0
        and any(v in node.ds.data_vars for v in ("VISIBILITY", "DATA"))
    )
    print(f"Found {n_parts} visibility partition(s).")

    if os.path.exists(out_path):
        print(f"Removing existing {out_path} ...")
        import shutil
        shutil.rmtree(out_path)

    print(f"Writing to {out_path} ...")
    try:
        dt.to_zarr(out_path, mode="w", compute=True)
    except Exception as exc:
        sys.exit(f"ERROR writing Zarr: {exc}")

    print(f"Done.  Test data written to: {out_path}")
    print()
    print("Run tests with:")
    print(f"  PS={out_path} pytest test_msv4_backend.py -v")


if __name__ == "__main__":
    main()
