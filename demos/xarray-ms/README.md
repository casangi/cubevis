# msvis xarray-ms Test Suite

## Test Dataset

**`sis14_twhya_calibrated_flagged.ms`** — ALMA Band 7 observation of TW Hydrae

Download (~230 MB unpacked):
```bash
wget https://casa.nrao.edu/download/devel/casavis/data/sis14_twhya_calibrated_flagged.ms.tar.gz
tar zxf sis14_twhya_calibrated_flagged.ms.tar.gz
```

### Why this dataset

| Property | Value | Relevance |
|---|---|---|
| Telescope | ALMA (43 antennas) | Real-world interferometer with many baselines |
| Band | 7 (~372–374 GHz) | Spectral line + continuum content |
| SPWs | 4 (DATA_DESC_ID 0–3) | Tests multi-partition DataTree structure |
| Channels | 48 per SPW | Enough frequency axis to exercise selection |
| Integrations | ~270 per field | Enough time axis, not huge |
| Fields | 6 (calibrators + TW Hya field 5) | Tests field-based selection |
| Correlations | XX, YY | Two-polarization, tests pol axis |
| Existing flags | Yes (pre-applied shadow/online flags) | Tests flag read AND write-back |
| Packed size | ~230 MB | Fast download, fits easily in memory |

### Structure expected after `xarray.open_datatree`

```
/
├── sis14_twhya_calibrated_flagged_partition_000   # SPW 0
│   ├── antenna_xds
│   └── field_and_source_base_xds
├── sis14_twhya_calibrated_flagged_partition_001   # SPW 1
...
└── sis14_twhya_calibrated_flagged_partition_003   # SPW 3
```

Each partition dataset has dims `(time, baseline_id, frequency, polarization)` and
data variables `VISIBILITY`, `FLAG`, `UVW`, `WEIGHT`, `EFFECTIVE_INTEGRATION_TIME`.

## Test Scripts

| Script | What it covers |
|---|---|
| `test_01_open.py` | Open MS, inspect DataTree structure, validate coords |
| `test_02_selection.py` | Field/SPW/time/baseline selection via `.sel` / `.where` |
| `test_03_flag_read.py` | Read existing FLAG column, compute flag statistics |
| `test_04_flag_writeback.py` | Modify FLAG array, write back to MSv2, verify roundtrip |
| `test_05_axes.py` | Derive plottable quantities: amplitude, phase, uvdist, uvwave |
| `test_06_dask.py` | Dask chunking, compute amp/phase lazily, trigger `.compute()` |
| `test_07_dask_derived.py` | Dask computation of derived quantities for msvis |
| `test_08_datashader_scatter.py` | Datashader scatter plot pipeline for msvis |
| `test_09_datashader_raster.py` | Datashader 2D raster plot pipeline for msvis |
| `test_10_bokeh_display.py` | Render scatter and raster plots as Bokeh figures |
| `test_11_parallel_optimization.py` | Parallel access and optimization tests |

Run all:
```bash
MS=sis14_twhya_calibrated_flagged.ms pytest test_0*.py -v
```
or individually:
```bash
MS=sis14_twhya_calibrated_flagged.ms python test_01_open.py
```
All scripts also run standalone (they default to `MS` env var, falling back to a
`simulate()`-generated MS so the test suite works even without the real data).
