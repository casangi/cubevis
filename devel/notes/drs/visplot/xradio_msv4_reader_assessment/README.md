# MSv4 reading through XRadio: assessment and plan

*Status: assessment complete, 2026-11-03. Decision: **XRadio becomes the
required, primary MSv4 reader**, behind a thin adapter module (§9).
Implementation deferred. Run the real-store confirmation (§6.0) and the
performance runs (§6.1) before merging. Reading MSv2 through XRadio is
recorded as a possible follow-on, deferred until after this change (§10).*

## 1. The request

Stakeholder feedback: visplot's MSv4 reader (`MSv4Backend`) should read
Processing Sets through **XRadio** instead of using xarray + zarr directly.
The word "(at least)" in the request suggests they may also want MSv2 to go
through XRadio eventually.

## 2. Short answer

**Make the change. XRadio becomes the one MSv4 reader, required by the
`visplot` extra, and all XRadio use goes through a thin adapter.**

* XRadio's read path *is* xarray + zarr. In xradio 1.3.0 (current, 2026-10-16),
  `open_processing_set()` is `xr.open_datatree(path, engine="zarr", chunks={})`
  plus the `xr_ps` / `xr_ms` accessors (and s3 support). The arrays are the
  same dask-backed zarr arrays. So speed is no reason to avoid it, and the
  measurements in §4 agree.
* There are essentially no real MSv4 datasets written by xarray-ms (§5.1), so
  there's no need to keep a second, hand-written metadata path for them. With
  one metadata path, the same store can't show different metadata in
  different environments.
* XRadio has no public API for writing into an existing store, so the MSv4
  flag commit (`flag_commit.commit_msv4`, zarr `vindex` writes plus an `.npz`
  backup) keeps writing zarr directly into the store XRadio opened. That is
  plumbing, not a second reader.
* Separately, revisit the `{"time": 100, "baseline_id": 100}` chunk override.
  It was the biggest performance factor measured here (§4.1), and the fix is
  worth doing on its own, whatever else happens.

## 3. Why xarray + zarr directly?

### 3.1 Confirmed answer: a prototyping artifact

Confirmed 2026-11-02 by asking the development session that made the choice.
It wasn't a deliberate decision about speed or about completeness of access:

* Darrell had asked to keep using `xarray-ms` for reading, for consistency
  with the MSv2 work.
* `xarray-ms` (0.5.6 then, and still true today) registers only an MSv2
  engine, `xarray-ms:msv2`. It has no Zarr/MSv4 reader.
* The nearest layer that returned the same kind of object was xarray's own
  `engine="zarr"`. A Processing Set is a Zarr-backed DataTree, and
  `xr.open_datatree(engine="zarr")` gives the same Datasets `MSv2Backend`
  works with. So `MSv4Backend` could reuse the same selection, Dask and query
  patterns. The speed work (consolidated metadata, partition cache) was then
  built on top of that choice.

That session also acknowledged where it had overstated things, and what this
assessment has since checked:

| Earlier claim | Status now |
|---|---|
| Zarr path is "exactly what xradio does internally" | Never verified then. **Now verified** from xradio 1.3.0 source: `open_processing_set` = `xr.open_datatree(engine="zarr", chunks={})` + accessors (+ s3). Same arrays. |
| Backend reads xradio stores correctly | Was a hypothesis: all real-data testing used an xarray-ms `to_zarr()` store. **Now partly tested**: the unmodified backend reads output from xradio 1.3's own converter with bit-identical rasters (§4, §4.2). That converter run used a synthetic MS, so a real xradio- or RADPS-produced store is still untested (§6.0). |
| xradio-native layout = `correlated_xds` child per partition | **Inferred from schema docs, never validated, and wrong for xradio 1.x.** That is the pre-1.0 layout. 1.x puts the data on the MSv4 node itself (§4.2). The synthetic `correlated_xds` test in `test_msv4_backend.py` tests a layout xradio no longer writes. |
| xradio would be "the safest route for flag write-back" | **Doesn't apply:** xradio has no public API for writing into an existing store (§2). Write-back stays on zarr regardless. |
| xradio offers "planned support for other storage formats" | Partly confirmed: the 1.3 converter takes `storage_backend: 'zarr' \| 'netcdf'`. `open_processing_set` itself still opens zarr only. |
| Importing xradio warned that MSv2 conversion was unavailable without casatools/casacore | Older behavior. In 1.3 the casacore-table layer defaults to **arcae** (the library xarray-ms already uses). No such warning appeared in our environment with arcae installed. |

### 3.2 What the history shows (for the record)

The git history and docs:

| Date | Evidence |
|---|---|
| design (`devel/docs/msvis_design.md` §2.3, §7, §9) | MSv4 backend specified as **xradio**. Still says so. |
| 2026-06-15 `ac6933a` | First `MSv4Backend` (then in `reader.py`) used `xradio.measurement_set.open_processing_set(path, chunks=...)`. |
| 2026-06-28 `10112fa` | That version removed ("old version of MSv4Backend"). New `msv4_backend.py` docstring: *"xarray's own Zarr engine — no xradio or xarray-ms dependency is required in the read path."* |
| `devel/slides/visplot/make_slides.py` | "MSv4Backend — reads and flags: xarray / Zarr, no xradio" |

Before §3.1 was confirmed, this assessment inferred three possible reasons.
They weren't the cause, but each still matters for implementation:

1. **The June xradio call couldn't have worked.** It passed `chunks=` to
   `open_processing_set`, which had no such parameter by xradio 1.2.0
   (2026-05). Implementation note: chunking can't be set through xradio's
   opener. It has to be applied afterwards with `ds.chunk(...)`.
2. **The test data was written by xarray-ms.** XRadio's accessors refuse
   that store (§4.3), so the test store has to be re-converted with XRadio's
   converter (§9.4).
3. **Dependency weight.** `xradio` alone requires only `xarray`, but
   `open_processing_set` imports `s3fs` unconditionally, and XRadio's
   documented install set is `xradio[zarr]`: toolviper (→ dask_jobqueue,
   distributed ≥ 2026.3, cerberus, rich, itables, …), s3fs, scipy, psutil,
   numcodecs, `zarr>=3,<4`. That matters for the `pip` extras and for the
   casa6 + xarray pixi environment (§5.1).

## 4. What was measured here (indicative only)

Setup: xradio 1.3.0, xarray-ms 0.5.9, current `cubevis` main, Python 3.13,
2 cores and 7 GB RAM, warm page cache (it can't be dropped without root).
Data: a synthetic MS from `xradio.testing.measurement_set.msv2_io.gen_test_ms`
(5 antennas, 256 channels, 2 pols, 4 DDIs × 10k rows). It was converted
(a) with `convert_msv2_to_processing_set` (72 MB) and (b) with xarray-ms +
`to_zarr` (16 MB). Script: `bench_msv4_xradio.py` (attached). It runs the
unmodified `MSv4Backend` and changes only the open step.

Median seconds over 3 runs, after a warm-up:

**(a) XRadio-written PS**

| mode | open + metadata | raster T×F amp (all) | raster (10% time) | raw Σ\|V\| | dask chunks |
|---|---|---|---|---|---|
| direct (as shipped, chunks 100/100) | 1.84 | 17.6 | 1.88 | 17.7 | (100, 10, 256, 2) |
| direct, native chunks | 0.37 | 1.45 | 0.39 | 1.32 | (3276, 10, 256, 2) |
| `open_processing_set` | 0.36 | 1.41 | 0.34 | 1.33 | (3276, 10, 256, 2) |

**(b) xarray-ms-written PS**

| mode | open + metadata | raster (all) | raster (10%) | raw Σ\|V\| |
|---|---|---|---|---|
| direct (as shipped) | 1.74 | 10.2 | 1.06 | 16.5 |
| direct, native chunks | 0.27 | 2.31 | 0.35 | 3.12 |
| `open_processing_set` | 0.26 | 2.25 | 0.39 | 2.96 |

Raster values were bit-identical across all three modes on both stores.

### 4.1 Reading

* **XRadio vs direct with the same chunks: no difference** (within noise),
  as the source code predicts.
* **The real factor is the chunk override.** `MSv4Backend._DEFAULT_CHUNKS =
  {"time": 100, "baseline_id": 100}` splits each stored zarr chunk
  into many dask tasks, and every one of them reads and decompresses the
  *whole* zarr chunk. On these stores that cost 3–12× on reads and 5–6× on
  open. The synthetic data is small and oddly shaped, so this needs confirming
  on real stores (sis14, and an HRS-scale PS). On large stores with huge time
  chunks, some rechunking may still help memory and parallelism, but it should
  be aligned to the stored chunks (whole multiples of them), not fixed at 100.

### 4.2 Compatibility of the current backend with XRadio output

The unmodified `MSv4Backend` opened and queried an XRadio-1.3-written PS
correctly. Note that its `correlated_xds` "xradio-native" branch in
`_collect_visibility_partitions` describes the **pre-1.0** XRadio layout. In
1.x the MSv4 node itself carries `type="visibility"` and the data variables,
with `antenna_xds`, `field_and_source_base_xds`, … as children.

### 4.3 XRadio accessors on an xarray-ms-written store

| call | XRadio-written | xarray-ms-written |
|---|---|---|
| `open_processing_set()` | OK | OK (it's just `open_datatree`) |
| `ps.xr_ps.summary()` / `get_combined_antenna_xds()` | OK | **fails**: root lacks `type="processing_set"` |
| `ms.xr_ms.get_partition_info()` | OK | **fails**: no `field_and_source_base_xds` child |
| `ms.xr_ms.sel(data_group_name=…)` | OK | OK |

## 5. Options and decision

**A. Push back (keep direct zarr).** Rejected. The performance argument
doesn't hold, because the read path is the same. The design doc already
specifies xradio. And we'd keep hand-maintaining MSv4 layout knowledge
(partition detection, data-group resolution, field-ID resolution — Appendix
C.9 — and SPW identity) that XRadio, the reference implementation of the
schema, already maintains. The one piece of that knowledge we did hand-write
for XRadio's own layout (`correlated_xds`) turned out to be wrong (§3.1).

**B. XRadio as the required, primary MSv4 reader (decided).**
1. `MSv4Backend` opens every Processing Set through the adapter (§9), which
   calls `open_processing_set()`. Chunking is applied afterwards by visplot,
   aligned to the stored chunks, only if §6.1 shows a benefit.
2. Partition discovery, data-group resolution, SPW, field/source and antenna
   metadata all come from XRadio's helpers through the adapter. The
   hand-written versions (`_collect_visibility_partitions`,
   `_get_data_group`, the SPW and field lookups) are removed.
3. A store that XRadio's helpers reject (non-conforming, or a pre-1.0 schema
   version) gets a clear error that names the problem and suggests
   re-converting with `convert_msv2_to_processing_set`. There is no silent
   fallback to a second metadata path.
4. Flag commit and backup restore keep writing zarr directly (no XRadio write
   API), using node paths that the adapter supplies.
5. Dependency: `xradio[zarr]` goes in the `xarray`/`visplot` extra, pinned
   `>=1.3,<1.4` (§5.1, §5.2).
6. Test data: re-convert sis14 with XRadio's converter and retire the
   xarray-ms-written copy (§9.4).

Benefits beyond satisfying the request: s3/cloud stores for free
(`open_processing_set` handles `s3fs`), schema evolution tracked upstream,
`scan_intents` filtering at open, and a path to the dropped Intent axis
(partition-level only, as found in Part 2).

Rough size: the adapter (§9) is roughly 200–300 lines plus tests, and most of
it replaces existing code in `msv4_backend.py` rather than adding to it. Add
re-baselining of tests and fixtures. A few days.

**C. MSv2 through XRadio as well.** Deferred to a possible follow-on, to be
considered only after B is complete, tested and in production. See §10.

### 5.1 Why required, not optional

*Revised 2026-11-03. An earlier draft (2026-11-02) considered making XRadio
optional, used when installed and when the store conforms, with today's zarr
path as a fallback.*

**The case for a fallback rested on xarray-ms-written stores, and there are
essentially none in real use.** The only one known is our own sis14 test
store. Without that population, an optional XRadio would cost a second metadata
path, tests that both paths agree, a CI matrix with and without XRadio, and a
record of which reader was used, all to protect against a case that doesn't
occur. It would also answer "use XRadio when available" rather than the
stakeholders' "use XRadio".

Facts from the optional-XRadio investigation that still matter:

* **xarray-ms is not an MSv4 reader.** It registers one engine,
  `xarray-ms:msv2`. Every `.ps.zarr` is opened by xarray's built-in
  `engine="zarr"`, which is also what `open_processing_set()` calls.
* **What reading actually needs** (tested in a clean environment): `xradio`
  plus `s3fs`. `open_processing_set()` imports s3fs unconditionally, and
  toolviper is never imported on the read path. `xr_ps.summary()` also needs
  `astropy`, already a `cubevis` core dependency. This subset is undeclared,
  though, so as a required dependency we declare `xradio[zarr]`, the set XRadio
  documents.
* **Side effect:** `import xradio` installs process-wide warning filters that
  hide zarr's "unstable specification" and "consolidated metadata" warnings,
  for all of visplot.

**Remaining exception: pre-1.0 XRadio stores.** The observation above covers
xarray-ms output, not stores written by XRadio before 1.0 (e.g. early RADPS
output, schema "4.0.-9987"). If any are still in use, they need re-converting.
The adapter checks the store's `schema_version` attribute and refuses
unsupported versions with a clear message (§9).

### 5.2 How stable is the XRadio API?

*Added 2026-11-02.* Compared from the code of eight releases (0.0.60, 1.0.0,
1.0.2, 1.1.0, 1.1.3, 1.2.0, 1.2.5, 1.3.0). The GitHub release notes were not
reachable from the analysis environment, so any published versioning policy
was not checked.

**On-disk schema: stable.** MSv4 schema 4.0.0 shipped with XRadio 1.0.0
(2025-10) and was unchanged through 1.2.5. 1.3.0 (2026-10-16) moved to 4.0.1,
whose only data change is a new `PHASED_ARRAY_ELEMENT_COUNT` field in
`PhasedArrayXds`. Nothing visplot reads was affected. (Some schema helper
classes moved to `xradio.schema.measures` between 1.0 and 1.2; internal code,
not layout.)

**Python API: settling, but has broken compatibility in a minor release.**

| Release | Changes to the public API |
|---|---|
| 0.0.60 → 1.0.0 | `open_processing_set(intents=)` → `scan_intents=`; `summary()` gained `first_columns` |
| 1.0 → 1.1.0 | **Breaking, no deprecation period:** `data_group=` → `data_group_name=` on `summary`, `get_combined_field_and_source_xds` and related methods; `add_data_group()` signature rewritten; converter `overwrite=` → `persistence_mode=` |
| 1.1 → 1.2.0 | Additions only: `array_backend=`, `partition_filter=`, `delete_data_variables`, `plot_antenna_positions_2d` |
| 1.2.0 → 1.2.5 | No change |
| 1.2.5 → 1.3.0 | No signature changes; new `open_msv2` (large new MSv2 code) |

About 15 releases in 13 months. Deprecation handling is uneven: the
converter's `use_table_iter` gets a proper `DeprecationWarning`, but the 1.1
renames simply broke old calls.

The calls visplot would use (`open_processing_set(path)`,
`xr_ms.sel(data_group_name=…)`, `get_partition_info()`,
`get_field_and_source_xds()`, `get_combined_antenna_xds()`) haven't changed
signature since 1.1.0, but `data_group_name` is exactly what 1.1.0 renamed.

**With no fallback, an XRadio release that breaks compatibility breaks
visplot's MSv4 support outright.** These safeguards therefore matter more,
not less:

* **Pin `xradio>=1.3,<1.4`** and widen deliberately after testing. Minor
  releases have broken things, so `<2` is not safe.
* **All XRadio calls in one adapter module** (§9), so a rename is fixed in one
  file, and a fix that depends on the XRadio version lives there too.
* **CI job against the latest XRadio**, so a break shows up as a failing test
  before anyone widens the pin.

## 6. Test plan (before merging)

### 6.0 Real-store confirmation (do this first)

Suggested by the session that wrote the backend. Originally the deciding
test between two readers; with XRadio decided, it is now a one-off check that
nothing visible changes. Take one real store produced by XRadio's converter
or by RADPS (not xarray-ms, not synthetic). Open it with today's backend and
with `open_processing_set()`. Compare, partition by partition:

* the set of visibility partitions found (names and count);
* dimensions and sizes, coordinates, and data variable names and dtypes;
* `data_groups` attrs and the variables each group resolves to;
* `metadata()` output (scans, fields, SPWs, antennas, baselines,
  correlations, time and frequency ranges);
* one raster and one scatter render, pixel for pixel.

Any difference is examined before merging. It means either today's
hand-written logic was wrong for that store (XRadio's answer is then the
correct one), or the adapter's translation into visplot's types needs fixing.

**Expected, not a bug:** a store written by XRadio's converter uses XRadio's
naming. Antenna names get the station appended
(`test_antenna_0_test_station_0`) and field names get the field id
(`NGC3031_0`). So re-converting sis14 with XRadio changes the labels from
what today's xarray-ms-written copy shows (§10.2). Flag JSON saved against the
old sis14 PS won't match the re-converted one by name.

### 6.1 Performance runs

Run `bench_msv4_xradio.py` (extend as needed) in the environment visplot
ships in: the pixi casa6 + xarray env, and the remote worker env.

**Datasets**
1. sis14 re-converted with `convert_msv2_to_processing_set` (default
   chunking, and one run with `main_chunksize` set small).
2. A large real PS from the HRS / VLA side (multi-SPW, many scans, TB-scale if
   available) on the storage it will really live on (lustre).
3. Optional: an s3-hosted PS, to check the new capability.

**Modes:** `direct` (as shipped), `direct-nat`, `xradio`, plus
`xradio + aligned rechunk` if §4's result holds.

**Operations:** open + metadata; full-range raster (T×F amplitude); zoomed
raster (10% time, one SPW); scatter `query_columns` amp-vs-time with
colorize-by-scan; the probe; flag commit + verify against an XRadio-opened
tree; the remote path (`test_remote_reduction_context.py` with `PS=`).

**Metrics:** wall time (median and min of ≥3); cold cache (fresh process,
cache dropped or a store larger than RAM, `--repeats 1 --no-warmup`); peak
RSS; and bit-identical results against `direct`.

**Acceptance:**
* `xradio` within ±5% of `direct-nat` on every operation, with identical
  outputs.
* The chunking choice set from data: whichever of native or aligned-rechunk
  wins on dataset 2 becomes the default.
* `xradio[zarr]` resolves together with casatasks in the pixi environment
  (verify toolviper's `dask>=2026.3` / `distributed` pins and conda-forge
  availability), and `pip install cubevis[visplot]` still works.
* Full `test_msv4_backend.py` passes against dataset 1, with the
  `correlated_xds` synthetic test replaced by an XRadio-converted fixture.
* A non-conforming store and a pre-1.0 store each produce the intended clear
  error.

## 7. Suggested reply to stakeholders (draft)

> We looked into it and agree. XRadio's `open_processing_set` is a thin
> wrapper over the same xarray/zarr open we use, so reading through it costs
> nothing, and it keeps us aligned with the reference schema implementation.
> visplot will read MSv4 only through XRadio, as a required dependency, with
> partition and metadata handling taken from XRadio rather than our own code.
> Flags will still be written directly to the zarr store, since XRadio has no
> in-place write API. We'll confirm on real data before merging; measurements
> so far show no cost, plus a separate chunking improvement we found along the
> way. We'll evaluate XRadio's new `open_msv2` for the MSv2 side afterwards.

## 8. Questions for Darrell

1. ~~Does §3 match your memory of why the June version was dropped?~~
   Answered: a prototyping artifact (§3.1).
2. ~~Required or optional?~~ Decided: required and primary (§5.1).
3. ~~Are there real PS stores in circulation written by xarray-ms?~~
   Answered: essentially none. **Still open:** are any pre-1.0 XRadio
   stores (e.g. early RADPS output) still in use? That decides whether the
   pre-1.0 refusal (§5.1) needs a migration note.
4. Should the chunk-default fix ship first, on its own, given it's
   independent and appears to be the bigger win?
5. ~~Must MSv2 and MSv4 display identically?~~ Answered 2026-11-03: no
   mandate. For MSv2, fidelity with CASA6 matters more than consistency with
   MSv4 (§10.1).

## Doc inconsistencies to fix (whatever is decided)

* `data/__init__.py`: says MSv4Backend opens via `xradio.read_processing_set()`.
  It doesn't (yet). Correct it to the adapter when the change lands.
* `data/reader.py` module and `XArrayReader` docstrings: "MSv4Backend —
  xradio implementation".
* `msvis_design.md` §2.3/§7/§9 and the implementation-plan diagram
  ("Zarr/xradio"), versus the slides ("no xradio"). The design doc becomes
  correct again once the change lands; the slides need updating.
* `msv4_backend.py`: the "xradio-native = `correlated_xds` child" description
  is the pre-1.0 layout, an inference that was never validated (§3.1).
  `test_msv4_backend.py` §11 builds its synthetic store on that layout.

## 9. Implementation: the XRadio adapter

*Added 2026-11-03.* All XRadio use goes through one thin module that
translates XRadio into visplot's own types. It sits below `MSv4Backend`;
`XArrayReader` remains visplot's independence layer above MSv2/MSv4.

### 9.1 Why have one

* **Contains XRadio's API changes** (§5.2). The next rename like `data_group=`
  → `data_group_name=` is fixed in one file. Fixes that depend on the XRadio
  version live there too, so the pin can be widened one release at a time.
* **Keeps visplot's own types in charge.** `MSv4Backend`, `metadata()`,
  `IdentityTables` and colorize-by-axis consume visplot dataclasses, not
  XRadio's `summary()` DataFrame columns or `partition_info` dictionary keys.
  Upstream changes in those shapes can't reach the display.
* **Testable without real stores.** Edge cases (missing SPW names, several
  data groups, single-dish partitions) can be tested with a fake adapter.
* **One place for policy:** the `schema_version` check (refusing pre-1.0
  stores), the import of XRadio and its missing-dependency message, and the
  process-wide warning filters XRadio installs.
* **Easy to follow for whoever maintains this next.** A short, well-named
  module is easier to pick up than XRadio calls scattered through a
  3,000-line backend.

### 9.2 What it must not do

* **Wrap the bulk data.** Partitions come back as plain lazy `xr.Dataset`s,
  the same zarr-backed arrays, handed straight to the existing selection and
  query code. Wrapping arrays adds overhead and a second place where values
  could diverge.
* **Pretend to support other MSv4 libraries.** XRadio is the reference
  implementation of the schema, and there's no second library to abstract
  over. That can be added if one ever appears.
* **Duplicate `XArrayReader`.** That already abstracts MSv2 vs MSv4 for the
  rest of visplot.

### 9.3 Rough shape (`data/_msv4_xradio.py`)

```python
@dataclass(frozen=True)
class Msv4Partition:
    name: str              # node path; also what flag_commit writes to
    ds: xr.Dataset         # lazy, zarr-backed; passed through untouched
    data_group: DataGroup  # visplot's resolved names: vis/flag/weight/uvw
    info: PartitionInfo    # spw name/id, scan intents, fields, sources

def open_ps(path, data_group=None) -> PsHandle      # schema check + open_processing_set
def partitions(h) -> list[Msv4Partition]
def antennas(h) -> AntennaTable
def field_and_source(h, part) -> FieldSourceTable   # may close Appendix C.9
def zarr_node_path(h, part) -> str                  # for commit_msv4 / restore
```

* **Chunking stays in visplot**, applied by one function after `open_ps`,
  aligned to the stored chunks (§4.1). XRadio's opener can't set chunking
  anyway (§3.2).
* **`flag_commit` asks the adapter for node paths** instead of walking the tree
  itself, so reading and writing share one definition of what a partition is.
* **Most of it replaces existing code** (`_collect_visibility_partitions`,
  `_get_data_group`, the SPW and field lookups) rather than adding to it.
  Roughly 200–300 lines plus tests.

### 9.4 Tests and fixtures

* **sis14 re-converted with XRadio's converter** becomes the standard MSv4
  fixture. The xarray-ms-written copy is retired, or kept only to assert the
  non-conforming-store error.
* **Replace the `correlated_xds` synthetic test** with small fixtures made by
  XRadio's real converter (e.g. from `xradio.testing.measurement_set.msv2_io.gen_test_ms`,
  as used for §4), covering several partitions, several data groups and
  single-dish.
* **Adapter unit tests** against those fixtures and against a fake adapter
  for edge cases.
* **Error tests:** non-conforming store, pre-1.0 `schema_version`, XRadio not
  installed.
* **CI job against the latest XRadio release** (§5.2), separate from the
  pinned job.
* **A real RADPS store** when one is available, for the §6.0 check.

## 10. Possible follow-on: reading MSv2 through XRadio

*Added 2026-11-03. **Deferred:** consider only after the MSv4 change (§5 B,
§9) is complete, tested and in production. Recorded here so the findings
aren't lost.*

XRadio 1.3.0 (2026-10-16) added `open_msv2()`: a lazy view of an MSv2 as the
Processing Set `convert_msv2_to_processing_set` would write, without converting
it. It reads through **arcae**, the same C++ layer xarray-ms uses
(`xradio._utils._casacore.backend` defaults to arcae). It is read-only by
default: its partition cache stays in memory, and it adds an `XRADIO_PARTITIONS`
sub-table to the MS only if explicitly asked to. It also registers its own
xarray engine, `xradio_msv2`.

### 10.1 Policy context

* **There is no mandate for MSv2 and MSv4 to display identically in visplot.**
* **For MSv2, stakeholders would likely prefer fidelity with CASA6** (plain
  antenna and field names, as `plotms`/`listobs` show them) over consistency
  with MSv4. xarray-ms's names match CASA6's. XRadio's don't (§10.2).
* So the case for this follow-on would have to rest on other benefits (one
  adapter, XRadio's metadata helpers, the "(at least)" in the request), not on
  cross-format consistency, and it must not cost CASA6 fidelity for MSv2 users.

### 10.2 Same MS, two readers (synthetic MS, XRadio 1.3.0, xarray-ms 0.5.9)

| | xarray-ms | XRadio `open_msv2` |
|---|---|---|
| VISIBILITY, UVW, time, frequency, scan | identical | identical |
| Antenna names | `test_antenna_0` | `test_antenna_0_test_station_0` |
| Field names | `NGC3031` | `NGC3031_0` |
| FLAG on grid cells with no data | flagged (uint8) | unflagged (bool) |
| WEIGHT | all NaN | all 1.0 |
| Partition names | `synth_partition_000…` | `synth_0…` |
| Works with XRadio's helpers | no | yes |
| Full read of all VISIBILITY | 1.26 s | 1.13 s |

* **A Processing Set written by XRadio's converter matches `open_msv2`
  exactly**: same names, missing cells unflagged. XRadio is consistent with
  itself.
* **The naming is likely deliberate, motivated by VLBI.** Appending the station
  keeps names unique when the same antenna name appears at more than one
  station, and `_<id>` does the same for duplicate field names. So if MSv2 ever
  goes through XRadio, the choice is between XRadio's names and normalizing back
  to CASA6 names in the adapter, which may then be ambiguous for VLBI.
* **The WEIGHT difference may come from how the synthetic MS was generated.**
  Confirm it, and the missing-cell FLAG difference, on sis14 and a real VLA or
  HRS MS.
* **Consequences of switching:**
  - Saved flags: FlagDB records operations by human-readable names, so existing
    flag JSON saved from an MS would need a migration or a name map.
  - Statistics: flag fractions and statistics such as the Z-Score colorization
    could differ, because the readers disagree on missing grid cells and on
    weights.

### 10.3 Open cost grows with rows and partitions

Each open ran in a fresh process. MSes were synthetic, with 5 antennas and 8
channels, so only the row and partition counts vary. The machine had 2 cores.
Row and partition counts are approximate.

| MS | MAIN rows | partitions | xarray-ms first / repeat open | XRadio first / repeat open |
|---|---|---|---|---|
| r10k | 40k | 4 | 1.0 / 0.06 s | 1.0 / 0.6 s |
| r40k | 160k | 4 | 1.0 / 0.09 s | 1.0 / 0.9 s |
| r160k | 640k | 4 | 1.3 / 0.19 s | 2.0 / 1.8 s |
| r640k | 2.56M | 4 | 2.8 / 0.63 s | 6.1 / 7.4 s |
| s8 | 160k | 16 | 1.2 / 0.25 s | 2.3 / 2.0 s |
| s32 | 640k | 64 | 2.3 / 1.0 s | 8.4 / 9.1 s |

Where XRadio's time goes:

* **Rows (linear).** It reads MAIN's index columns for every row
  (DATA_DESC_ID, FIELD_ID, SCAN_NUMBER, STATE_ID, ANTENNA1/2, TIME) to work out
  partitions and each partition's time × baseline grid. xarray-ms does the same
  kind of work more cheaply, and caches it within the process.
* **Partitions (dominant, about 0.1–0.2 s each here).** Profiling the
  64-partition MS: building the partitions took 13.9 of 17.8 s, with
  `field_and_source_xds` alone 5.7 s and repeated subtable reads most of the
  rest. `antenna_xds`, `system_calibration_xds` and `pointing_xds` are rebuilt
  for every partition, even when identical. (The synthetic MS includes an
  ephemeris table, which may inflate the field/source cost.)
* **Not reused on a second open.** The in-memory partition cache saves only the
  partition list, so the sub-datasets are rebuilt on every open.

Extrapolating roughly: real ALMA or VLA data partitioned by SPW × intent ×
observation easily reaches a few hundred partitions. At 0.1–0.2 s each, that
is roughly 30 s to over a minute just to open. A remote worker reopens after
every flag commit.

**Why MSv4 doesn't have this problem:** for a Processing Set, the converter
builds the sub-datasets once and writes them to disk, so `open_processing_set`
just reads them. `open_msv2` repeats the conversion's metadata work on every
open.

Ways to reduce it, if pursued:

* **`partition_filter`:** open only the SPWs, intents or fields being plotted.
  That fits visplot's selection model, but means reopening when the selection
  widens.
* **`with_pointing=False`:** skip pointing, unless the Azimuth, Elevation or
  parallactic-angle axes need it.
* **Persisted partition cache** (`XRADIO_PARTITIONS` sub-table): saves only the
  partition list, not the per-partition build, and writes into the user's MS.
  That conflicts with treating MSv2 as read-only.
* **Ask the XRadio team** about reusing sub-datasets that are identical across
  partitions. That looks like the biggest single win, and it's their code.

### 10.4 Other risks

* **Very new:** `open_msv2` arrived in 1.3.0, a large body of new code.
* **Interaction with flag writes:** `open_msv2` raises `MSv2ChangedError` if the
  MS changes after opening. Its documentation says that covers rows and grid
  changes, not FLAG values. `commit_msv2_arcae` writes into the same MS, so
  this needs an explicit test.

### 10.5 What would decide it

Run on sis14 and a large, partition-heavy real MS (VLA or HRS):

1. **Fidelity with CASA6:** names, partitioning, FLAG and WEIGHT on missing
   cells, compared with what CASA6 `plotms`/`listobs` show. Decide the naming
   policy: XRadio's names, or normalized to CASA6 in the adapter, with the VLBI
   caveat.
2. **Open time:** cold and repeat, with and without `partition_filter` and
   `with_pointing=False`. If opening takes a minute or more, xarray-ms (or
   converting to MSv4 first) stays the better path for large MSv2 data.
3. **Flag commit:** `commit_msv2_arcae` against an XRadio-opened tree, then
   reading back without `MSv2ChangedError`.
4. **Saved flags:** loading existing flag JSON, and a migration or name map if
   names change.
5. **Read throughput and remote path:** the same operations as §6.1.

Scripts used for §10.2–10.3 (in the project): `msv2_reader_compare.py`
(two readers, same MS), `msv2_open_scaling.py` (open time per reader) and
`gen_scaling_ms.py` (synthetic MSes of a given size and SPW count).
