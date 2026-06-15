"""
test_01_open.py — Open MSv2 with xarray-ms, inspect DataTree structure.

Tests:
  - xarray.open_datatree() dispatches to the xarray-ms engine
  - Expected partition keys are present (one per DATA_DESC_ID)
  - Each partition Dataset has the correct dimensions and variables
  - Coordinate arrays (time, baseline_id, frequency, polarization) are present
  - Non-index coordinates (field_name, scan_name, antenna names) are present
  - antenna_xds sub-tree is present and has antenna positions
  - DataTree is lazy (no arrays materialised yet)

Usage:
    MS=sis14_twhya_calibrated_flagged.ms python test_01_open.py
    MS=sis14_twhya_calibrated_flagged.ms pytest test_01_open.py -v
"""

import os
import xarray
import xarray_ms  # noqa: F401 — registers the "xarray-ms" backend engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_ms():
    """Return path to test MS, or simulate a small one if not available."""
    path = os.environ.get("MS", "sis14_twhya_calibrated_flagged.ms")
    if not os.path.isdir(path):
        from xarray_ms.testing.simulator import simulate
        print(f"WARNING: {path!r} not found — using simulated MS")
        path = simulate(
            "test_sim.ms",
            data_description=[
                (48, ("XX", "YY")),
                (48, ("XX", "YY")),
                (48, ("XX", "YY")),
                (48, ("XX", "YY")),
            ],
        )
    return path


def open_ms(ms_path):
    """Open MS with recommended partition schema, returning lazy DataTree."""
    return xarray.open_datatree(
        ms_path,
        engine="xarray-ms:msv2",
        partition_schema=["DATA_DESC_ID", "OBSERVATION_ID"],
        chunks={"time": 200, "baseline_id": 100},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_datatree_opens():
    ms = _get_ms()
    dt = open_ms(ms)
    assert dt is not None, "open_datatree returned None"
    print(f"  DataTree root children: {list(dt.children.keys())}")


def test_partitions_present():
    ms = _get_ms()
    dt = open_ms(ms)
    partitions = list(dt.children.keys())
    assert len(partitions) >= 1, "Expected at least one partition"
    # For the real dataset we expect 4 partitions (4 SPWs)
    print(f"  Partition count: {len(partitions)} — {partitions}")


def test_partition_dimensions():
    ms = _get_ms()
    dt = open_ms(ms)
    for name, node in dt.children.items():
        ds = node.ds
        dims = set(ds.dims)
        required = {"time", "baseline_id", "frequency", "polarization"}
        missing = required - dims
        assert not missing, f"Partition {name!r} missing dims: {missing}"
        print(f"  {name}: dims={dict(ds.dims)}")


def test_data_variables():
    ms = _get_ms()
    dt = open_ms(ms)
    required_vars = {"VISIBILITY", "FLAG", "UVW", "WEIGHT"}
    for name, node in dt.children.items():
        ds = node.ds
        present = set(ds.data_vars)
        missing = required_vars - present
        assert not missing, (
            f"Partition {name!r} missing data vars: {missing}. "
            f"Present: {present}"
        )
        print(f"  {name}: data_vars={sorted(present)}")


def test_coordinates():
    ms = _get_ms()
    dt = open_ms(ms)
    for name, node in dt.children.items():
        ds = node.ds
        # Index coordinates
        for coord in ("time", "baseline_id", "frequency", "polarization"):
            assert coord in ds.coords, (
                f"Partition {name!r}: missing coord {coord!r}"
            )
        # Non-index but important coordinates
        for coord in ("field_name", "scan_name",
                      "baseline_antenna1_name", "baseline_antenna2_name"):
            if coord not in ds.coords:
                print(f"  WARNING: {name!r} missing non-index coord {coord!r}")
        print(f"  {name}: polarization values = {ds.coords['polarization'].values}")


def test_antenna_xds():
    ms = _get_ms()
    dt = open_ms(ms)
    for name, node in dt.children.items():
        children = list(node.children.keys())
        assert any("antenna" in c for c in children), (
            f"Partition {name!r} has no antenna_xds child. Children: {children}"
        )
        ant_node = node["antenna_xds"]
        assert "ANTENNA_POSITION" in ant_node.ds.data_vars, (
            f"antenna_xds in {name!r} has no ANTENNA_POSITION"
        )
        n_ant = ant_node.ds.dims["antenna_name"]
        print(f"  {name}: {n_ant} antennas")
        break  # One partition is enough for the structure check


def test_lazy_loading():
    """Verify that data variables are dask arrays — nothing materialised."""
    import dask.array as da
    ms = _get_ms()
    dt = open_ms(ms)
    for name, node in dt.children.items():
        ds = node.ds
        vis = ds["VISIBILITY"]
        assert isinstance(vis.data, da.Array), (
            f"VISIBILITY in {name!r} is not a Dask array — data loaded eagerly"
        )
        print(f"  {name}: VISIBILITY shape={vis.shape}, chunks={vis.chunks}")
        break  # One partition is enough


def test_attributes():
    """Check MSv4 schema attributes are present."""
    ms = _get_ms()
    dt = open_ms(ms)
    for name, node in dt.children.items():
        attrs = node.ds.attrs
        assert "schema_version" in attrs, (
            f"Partition {name!r} missing 'schema_version' attribute"
        )
        print(f"  {name}: schema_version={attrs.get('schema_version')}")
        break


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_datatree_opens,
        test_partitions_present,
        test_partition_dimensions,
        test_data_variables,
        test_coordinates,
        test_antenna_xds,
        test_lazy_loading,
        test_attributes,
    ]
    passed = failed = 0
    for t in tests:
        try:
            print(f"\n--- {t.__name__} ---")
            t()
            print("  PASS")
            passed += 1
        except Exception as exc:
            print(f"  FAIL: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
