"""
test_02_selection.py — Data selection patterns via xarray-ms.

Tests the selection patterns that MSv2Backend._apply_selection() will need to
support.  All selections use the xarray .sel() / .where() / .isel() API on the
DataTree returned by xarray-ms — no casatools involved.

Tests:
  - Partition selection (pick specific DATA_DESC_ID / SPW)
  - Field selection via field_name non-index coordinate
  - Scan selection via scan_name non-index coordinate
  - Time range selection via .sel(time=slice(...))
  - Baseline selection via antenna name coordinates
  - Frequency channel range selection
  - Polarization selection
  - Compound selection (field + spw + time range)
  - SelectionSpec-style helper that mirrors MSv2Backend._apply_selection

Usage:
    MS=sis14_twhya_calibrated_flagged.ms python test_02_selection.py
"""

import os
import numpy as np
import xarray
import xarray_ms  # noqa: F401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_ms():
    path = os.environ.get("MS", "sis14_twhya_calibrated_flagged.ms")
    if not os.path.isdir(path):
        from xarray_ms.testing.simulator import simulate
        print(f"WARNING: {path!r} not found — using simulated MS")
        path = simulate(
            "test_sim.ms",
            data_description=[
                (48, ("XX", "YY")),
                (48, ("XX", "YY")),
            ],
        )
    return path


def open_ms(ms_path):
    return xarray.open_datatree(
        ms_path,
        engine="xarray-ms:msv2",
        partition_schema=["DATA_DESC_ID", "OBSERVATION_ID"],
        chunks={"time": 200, "baseline_id": 100},
    )


def _first_partition(dt):
    """Return the Dataset of the first visibility partition."""
    for node in dt.children.values():
        ds = node.ds
        if ds.dims.get("time", 0) > 0:
            return ds
    raise RuntimeError("No visibility partition found")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_partition_selection():
    """Picking a specific partition by partition name."""
    ms = _get_ms()
    dt = open_ms(ms)
    partitions = list(dt.children.keys())
    p0 = dt[partitions[0]].ds
    print(f"  Selected partition {partitions[0]!r}: dims={dict(p0.dims)}")
    assert "VISIBILITY" in p0.data_vars


def test_field_selection():
    """Select a single field by its field_name coordinate value."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)

    # field_name is a non-index coordinate on the time dimension
    assert "field_name" in ds.coords, "field_name coordinate missing"
    unique_fields = np.unique(ds.coords["field_name"].values)
    print(f"  Available fields: {unique_fields}")

    target_field = unique_fields[0]
    mask = ds.coords["field_name"] == target_field
    ds_field = ds.isel(time=mask.values)
    print(f"  Field {target_field!r}: {ds_field.dims['time']} integrations")
    assert ds_field.dims["time"] > 0, "Field selection returned no integrations"


def test_scan_selection():
    """Select a single scan by scan_name coordinate."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)

    if "scan_name" not in ds.coords:
        print("  SKIP: scan_name coord not present in this dataset")
        return

    unique_scans = np.unique(ds.coords["scan_name"].values)
    print(f"  Available scans: {unique_scans[:5]} ...")
    target_scan = unique_scans[0]
    mask = ds.coords["scan_name"] == target_scan
    ds_scan = ds.isel(time=mask.values)
    print(f"  Scan {target_scan!r}: {ds_scan.dims['time']} integrations")
    assert ds_scan.dims["time"] > 0


def test_time_range_selection():
    """Select a time range using .sel(time=slice(t0, t1))."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)

    times = ds.coords["time"].values
    t_start = times[0]
    t_end = times[min(len(times) - 1, 29)]  # first 30 integrations

    ds_tsel = ds.sel(time=slice(t_start, t_end))
    n = ds_tsel.dims["time"]
    print(f"  Time range [{t_start:.1f}, {t_end:.1f}]: {n} integrations")
    assert n > 0, "Time range selection returned nothing"
    assert n <= ds.dims["time"], "Time selection returned more rows than original"


def test_baseline_selection_by_antenna_name():
    """Select baselines involving a specific antenna by name."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)

    ant1 = ds.coords["baseline_antenna1_name"].values
    ant2 = ds.coords["baseline_antenna2_name"].values
    all_antennas = np.unique(np.concatenate([ant1, ant2]))
    print(f"  Total antennas: {len(all_antennas)}, first few: {all_antennas[:4]}")

    target_ant = all_antennas[0]
    mask = (ant1 == target_ant) | (ant2 == target_ant)
    ds_bl = ds.isel(baseline_id=mask)
    print(f"  Baselines involving {target_ant!r}: {ds_bl.dims['baseline_id']}")
    assert ds_bl.dims["baseline_id"] > 0


def test_autocorrelation_vs_crosscorrelation():
    """Separate autocorrelations (ant1==ant2) from cross-correlations."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)

    ant1 = ds.coords["baseline_antenna1_name"].values
    ant2 = ds.coords["baseline_antenna2_name"].values
    is_auto = ant1 == ant2
    n_auto = is_auto.sum()
    n_cross = (~is_auto).sum()
    print(f"  Autocorrelations: {n_auto}, Cross-correlations: {n_cross}")
    # For a well-formed interferometric MS, most baselines are cross-corr
    assert n_cross > 0, "No cross-correlation baselines found"


def test_frequency_selection():
    """Select a channel range by frequency index."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)

    n_chan = ds.dims["frequency"]
    ch0, ch1 = n_chan // 4, 3 * n_chan // 4  # middle half
    ds_freq = ds.isel(frequency=slice(ch0, ch1))
    print(f"  Channel range [{ch0}, {ch1}): {ds_freq.dims['frequency']} channels")
    freqs_Hz = ds_freq.coords["frequency"].values
    print(f"  Frequency range: {freqs_Hz[0]/1e9:.4f} – {freqs_Hz[-1]/1e9:.4f} GHz")
    assert ds_freq.dims["frequency"] == ch1 - ch0


def test_polarization_selection():
    """Select a single polarization."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)

    pols = ds.coords["polarization"].values
    print(f"  Polarizations available: {pols}")
    target_pol = pols[0]
    ds_pol = ds.sel(polarization=[target_pol])
    print(f"  Selected {target_pol!r}: shape={ds_pol['VISIBILITY'].shape}")
    assert ds_pol.dims["polarization"] == 1


def test_compound_selection():
    """Combined field + frequency + polarization selection."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)

    # Pick first field
    if "field_name" in ds.coords:
        target_field = np.unique(ds.coords["field_name"].values)[0]
        time_mask = (ds.coords["field_name"] == target_field).values
        ds = ds.isel(time=time_mask)

    # Channel 4 to 44 (trim edges)
    n_chan = ds.dims["frequency"]
    ds = ds.isel(frequency=slice(4, min(44, n_chan - 4)))

    # First polarization
    pols = ds.coords["polarization"].values
    ds = ds.sel(polarization=[pols[0]])

    print(
        f"  Compound selection result: time={ds.dims['time']}, "
        f"freq={ds.dims['frequency']}, pol={ds.dims['polarization']}"
    )
    assert ds.dims["time"] > 0
    assert ds.dims["frequency"] > 0
    assert ds.dims["polarization"] == 1


def test_where_based_flagging_mask():
    """Use .where() to mask flagged data — the core msvis pattern."""
    ms = _get_ms()
    dt = open_ms(ms)
    ds = _first_partition(dt)

    flag = ds["FLAG"]          # uint8, (time, baseline_id, frequency, polarization)
    vis  = ds["VISIBILITY"]

    # Mask flagged visibilities with NaN
    # FLAG is uint8 in xarray-ms v0.5.x; cast to bool first
    flag_bool = flag.astype(bool)
    vis_unflagged = vis.where(~flag_bool)

    # Trigger compute on a small slice to verify the chain works
    small = vis_unflagged.isel(time=slice(0, 5), frequency=slice(0, 4))
    result = small.compute()
    flag_fraction = flag_bool.isel(time=slice(0, 5), frequency=slice(0, 4)).mean().compute().item()
    print(f"  Flagged fraction (first 5 integrations, 4 channels): {flag_fraction:.3f}")
    print(f"  Unflagged visibility sample (time=0, bl=0, f=0): {result.values[0, 0, 0, :]}")


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_partition_selection,
        test_field_selection,
        test_scan_selection,
        test_time_range_selection,
        test_baseline_selection_by_antenna_name,
        test_autocorrelation_vs_crosscorrelation,
        test_frequency_selection,
        test_polarization_selection,
        test_compound_selection,
        test_where_based_flagging_mask,
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
