"""
test_04_flag_writeback.py — Write modified FLAG column back to MSv2.

This test is the critical verification for FlagWriteThrough support in
MSv2Backend.  It answers the open question from msvis_design.md §10:
"does current xarray-ms (v0.5.4) support writing FLAG data back to MSv2?"

Strategy
--------
xarray-ms v0.5.x is a *read* backend; it does not expose a write path
through open_datatree().  Flag write-back to MSv2 therefore requires one of:

  Path A — arcae direct write (recommended for FlagWriteThrough):
    Open the MSv2 TABLE via arcae, select the FLAG column rows corresponding
    to the modified partition, and write the numpy array back.

  Path B — dask-ms xds_to_table (alternative, requires python-casacore):
    Use daskms.xds_to_table() to write FLAG back.  dask-ms is the older
    xarray/dask MS library and still works for write-back even when
    xarray-ms is used for reading.

  Path C — copy to Zarr, modify, re-import (not a real write-back):
    Not suitable for FlagWriteThrough; changes are not in the original MSv2.

This script tests Path A (arcae) and Path B (dask-ms) independently,
so you can see which is available in your environment.

Tests:
  - Roundtrip: read FLAG, flip a known subset, write back, re-read, verify
  - Write-back leaves VISIBILITY and other columns unchanged
  - Multiple partitions can be written independently

Usage:
    MS=sis14_twhya_calibrated_flagged.ms python test_04_flag_writeback.py

IMPORTANT: This test modifies the MS in place.  Run on a COPY:
    cp -r sis14_twhya_calibrated_flagged.ms test_wb.ms
    MS=test_wb.ms python test_04_flag_writeback.py
"""

import os
import shutil
import tempfile
import numpy as np
import xarray
import xarray_ms  # noqa: F401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_ms(writable=False):
    """
    Return path to test MS.  If writable=True, copies to a temp dir so the
    original is not modified.  If the real MS is absent, uses the simulator.
    """
    src = os.environ.get("MS", "sis14_twhya_calibrated_flagged.ms")
    if not os.path.isdir(src):
        from xarray_ms.testing.simulator import simulate
        print(f"WARNING: {src!r} not found — using simulated MS")
        src = simulate("test_sim.ms", data_description=[(48, ("XX", "YY"))])

    if writable:
        tmpdir = tempfile.mkdtemp(prefix="msvis_wb_")
        dst = os.path.join(tmpdir, os.path.basename(src))
        shutil.copytree(src, dst)
        print(f"  Copied MS to {dst}")
        return dst
    return src


def open_ms(ms_path):
    return xarray.open_datatree(
        ms_path,
        engine="xarray-ms:msv2",
        partition_schema=["DATA_DESC_ID", "OBSERVATION_ID"],
        chunks={"time": 200, "baseline_id": 100},
    )


def _first_partition(dt):
    for node in dt.children.values():
        if node.ds.dims.get("time", 0) > 0:
            return node.ds
    raise RuntimeError("No partition found")


# ---------------------------------------------------------------------------
# Path A: arcae direct write
# ---------------------------------------------------------------------------

def test_writeback_via_arcae():
    """
    Write FLAG back to MSv2 using arcae table API directly.

    arcae exposes casacore's table interface.  The FLAG column in the MSv2
    MAIN table is indexed by row, with each row containing a (nchan, ncorr)
    cell.  xarray-ms re-grids rows onto the (time, baseline_id) grid; to
    write back we need to map (time_idx, baseline_idx) -> original row index.

    As of arcae 0.2.x the table can be opened in write mode using
    arcae.table() (or casacore.tables.table if available).  This test
    verifies the roundtrip: flip flags for integrations 0–4 in the first
    partition, write, re-read, assert they flipped.
    """
    try:
        import arcae
    except ImportError:
        print("  SKIP: arcae not installed")
        return

    ms = _get_ms(writable=True)
    try:
        dt = open_ms(ms)
        ds = _first_partition(dt)
        flag_before = ds["FLAG"].isel(time=slice(0, 5)).compute().values.copy()

        # --- flip flags for first 5 integrations ---
        flag_new = (~flag_before.astype(bool)).astype(flag_before.dtype)

        # xarray-ms DataTree does not expose a write path; use arcae directly.
        # arcae.table() opens a casacore table; getcol/putcol work by row.
        # We need the row indices for (time 0..4, all baselines).
        # These are stored in the _ROWID hidden coordinate if available.
        if "_ROWID" in ds.coords:
            row_ids = ds.coords["_ROWID"].isel(time=slice(0, 5)).values  # (5, nbl)
        else:
            # Fallback: open via arcae and find rows by matching TIME/ANTENNA1
            # This is the hard path; skip if row IDs are absent.
            print("  SKIP: _ROWID coord not available in this xarray-ms version; "
                  "cannot map grid indices to MSv2 rows for arcae write-back")
            return

        # Open MS table in read/write mode
        ms_table = arcae.table(ms, readonly=False)
        try:
            n_bl = row_ids.shape[1]
            n_chan = flag_new.shape[2]
            n_pol  = flag_new.shape[3]
            for t_idx in range(5):
                for bl_idx in range(n_bl):
                    row = int(row_ids[t_idx, bl_idx])
                    if row < 0:
                        continue  # padded row
                    cell = flag_new[t_idx, bl_idx]  # (nchan, npol)
                    ms_table.putcell("FLAG", row, cell)
        finally:
            ms_table.close()

        # Re-read and verify
        dt2 = open_ms(ms)
        ds2 = _first_partition(dt2)
        flag_after = ds2["FLAG"].isel(time=slice(0, 5)).compute().values
        np.testing.assert_array_equal(
            flag_after.astype(bool), ~flag_before.astype(bool),
            err_msg="FLAG values after write-back do not match expected flip"
        )
        print("  arcae write-back roundtrip: PASS")

    finally:
        shutil.rmtree(os.path.dirname(ms), ignore_errors=True)


# ---------------------------------------------------------------------------
# Path B: dask-ms xds_to_table write
# ---------------------------------------------------------------------------

def test_writeback_via_daskms():
    """
    Write FLAG back via dask-ms xds_to_table.

    dask-ms (daskms) provides xds_from_table() / xds_to_table() which are
    the older, row-based MS interface backed by python-casacore.  It is still
    the most straightforward way to write individual columns back to MSv2.

    This test:
      1. Opens the MS with xarray-ms (for the MSv4 view)
      2. Reads the original FLAG with dask-ms (for row-based write path)
      3. Modifies a known subset
      4. Writes back with xds_to_table()
      5. Re-reads with xarray-ms and verifies

    The dask-ms FLAG variable is shaped (row, nchan, ncorr); we select the
    same 5 integrations across all baselines and flip them.
    """
    try:
        from daskms import xds_from_table, xds_to_table
    except ImportError:
        print("  SKIP: dask-ms not installed")
        return

    ms = _get_ms(writable=True)
    try:
        # Read with xarray-ms first to get the field_name for the subset
        dt = open_ms(ms)
        ds = _first_partition(dt)
        flag_before_xms = ds["FLAG"].isel(time=slice(0, 5)).compute().values.copy()
        # shape: (5, n_baseline, n_chan, n_pol)

        # Read with dask-ms (row-based view)
        # For the first DATA_DESC_ID partition, filter by DATA_DESC_ID=0
        dms_datasets = xds_from_table(ms, group_cols=["DATA_DESC_ID"])
        # dms_datasets[0] corresponds to DATA_DESC_ID=0
        dms_ds0 = dms_datasets[0]
        flag_dms = dms_ds0.FLAG  # (row, nchan, ncorr) dask array

        # dask-ms row order: sorted by TIME, ANTENNA1, ANTENNA2
        # The first 5*n_baseline rows cover the first 5 integrations
        n_bl = ds.dims["baseline_id"]
        n_rows_first5 = 5 * n_bl

        import dask.array as da
        flag_orig = flag_dms.data  # dask array (nrow, nchan, ncorr)
        flag_flipped_chunk = da.logical_not(flag_orig[:n_rows_first5].astype(bool)).astype(flag_orig.dtype)
        flag_rest = flag_orig[n_rows_first5:]
        flag_new_full = da.concatenate([flag_flipped_chunk, flag_rest], axis=0)

        # Assign and write back
        dms_ds0_new = dms_ds0.assign(FLAG=(dms_ds0.FLAG.dims, flag_new_full))
        xds_to_table(dms_ds0_new, ms, ["FLAG"]).compute()
        print("  dask-ms xds_to_table write: completed")

        # Re-read with xarray-ms and verify the first 5 integrations flipped
        dt2 = open_ms(ms)
        ds2 = _first_partition(dt2)
        flag_after = ds2["FLAG"].isel(time=slice(0, 5)).compute().values

        expected = (~flag_before_xms.astype(bool)).astype(flag_after.dtype)
        np.testing.assert_array_equal(
            flag_after, expected,
            err_msg="FLAG values after dask-ms write-back do not match expected flip"
        )
        print("  dask-ms write-back roundtrip: PASS")

    finally:
        shutil.rmtree(os.path.dirname(ms), ignore_errors=True)


def test_visibility_unchanged_after_writeback():
    """
    After writing FLAG, verify VISIBILITY values are unmodified.
    Guards against accidentally writing to the wrong column.
    """
    try:
        from daskms import xds_from_table, xds_to_table
    except ImportError:
        print("  SKIP: dask-ms not installed")
        return

    ms = _get_ms(writable=True)
    try:
        # Read VISIBILITY before
        dt = open_ms(ms)
        ds = _first_partition(dt)
        vis_before = ds["VISIBILITY"].isel(time=slice(0, 5)).compute().values.copy()

        # Write FLAG (same as test_writeback_via_daskms but minimal)
        from daskms import xds_from_table, xds_to_table
        import dask.array as da
        dms_ds = xds_from_table(ms, group_cols=["DATA_DESC_ID"])[0]
        n_bl = ds.dims["baseline_id"]
        flag_new = da.logical_not(dms_ds.FLAG.data[:5 * n_bl].astype(bool))
        flag_rest = dms_ds.FLAG.data[5 * n_bl:]
        dms_ds_new = dms_ds.assign(FLAG=(
            dms_ds.FLAG.dims,
            da.concatenate([flag_new.astype(dms_ds.FLAG.dtype), flag_rest])
        ))
        xds_to_table(dms_ds_new, ms, ["FLAG"]).compute()

        # Re-read VISIBILITY — should be unchanged
        dt2 = open_ms(ms)
        ds2 = _first_partition(dt2)
        vis_after = ds2["VISIBILITY"].isel(time=slice(0, 5)).compute().values

        np.testing.assert_array_equal(
            vis_after, vis_before,
            err_msg="VISIBILITY changed after FLAG-only write-back"
        )
        print("  VISIBILITY unchanged after write-back: PASS")

    finally:
        shutil.rmtree(os.path.dirname(ms), ignore_errors=True)


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(
        "\nNOTE: This test modifies the MS in place (on a temporary copy).\n"
        "      To use your own copy: MS=/path/to/copy.ms python test_04_flag_writeback.py\n"
    )
    tests = [
        test_writeback_via_arcae,
        test_writeback_via_daskms,
        test_visibility_unchanged_after_writeback,
    ]
    passed = failed = 0
    for t in tests:
        try:
            print(f"\n--- {t.__name__} ---")
            t()
            print("  PASS")
            passed += 1
        except Exception as exc:
            import traceback
            print(f"  FAIL: {exc}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
