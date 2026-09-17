# Handoff to Part 3: Rendering Pipeline

**From:** Part 2 (backend data plumbing)
**To:** Part 3 (categorical aggregation, dataclass fields, categorical palette)
**Companion documents:** `visplot-colorize-by-axis-design.md` (updated —
read §3 in full, including the 2026-09 performance-regression note, before
this doc), `test_msv2_backend.py`/`test_msv4_backend.py`'s
`TestColorizeByAxisColumns` classes (the permanent regression coverage —
see "Verification" below), `verify_colorize_axis_part2.py` (the original
standalone script; still runnable, now largely superseded by the above).

---

## What landed

Five per-row columns, populated in both `MSv2Backend` and `MSv4Backend`,
verified against real `sis14_twhya_calibrated_flagged` MSv2 and MSv4 data
(not synthetic fixtures):

| Column | Axis | Value form | Always present? |
|---|---|---|---|
| `scan_name` | Scan | scan number, as a string (e.g. `"12"`) | conditional — present iff `"scan_name" in ds.coords` (true on all real data seen; required by MSv4 schema v4.0.0) |
| `baseline_antenna1_name` | Antenna1 | antenna name (e.g. `"DA42"`) | conditional, same as above |
| `baseline_antenna2_name` | Antenna2 | antenna name (e.g. `"DA44"`) | conditional, same as above |
| `polarization` | Correlation | the layer's own fixed pol string (e.g. `"XX"`) | **always** — every `(axis, pol)` key already carries its own pol |
| `spw` | SPW | `_partition_spw_ident(ds)`'s return: an `int` (SPW/DDID) or `str` (spectral window name) depending on what the store provides — **not normalized to one dtype** | conditional — omitted for a partition that declares no SPW identity at all |

**Naming convention** (for Part 3/4's axis→column lookup): MS-native
coordinate/attribute name where one exists (`scan_name`,
`baseline_antenna1_name`, `baseline_antenna2_name`, `polarization` — matches
`ds.coords` naming throughout the codebase), and the established
`SelectionSpec`/`axes.py` vocabulary word where no native coordinate exists
(`spw`, matching `SelectionSpec.spw`). There is no separate
`Axis → column name` mapping table anywhere yet — Part 3 will need to build
one; the above table is the source of truth for what to put in it.

## Where the columns are attached

Three places, all in both `msv2_backend.py` and `msv4_backend.py`:

1. **`_query_partition_scatter`** — the main per-partition path. `spw` and
   `polarization` are attached as a scalar assignment (`df["col"] = value`)
   after the per-key DataFrames exist, since both are constant across every
   row in a partition/key and need no dask array or broadcast at all.

   `scan_name`/`baseline_antenna1_name`/`baseline_antenna2_name` **do not**
   ride the broadcast mechanism `time`/`baseline_id`/`frequency` use —
   that was the original Part 2 approach, and it measurably regressed a
   pre-existing timing test (~1.45s of near-identical added cost in both
   the fused and serial pipelines; see design doc §3's performance-
   regression note for the full story, including a second, larger cost
   from pandas 3.0's default string-dtype conversion found only by direct
   benchmarking). Current mechanism, all new shared (non-abstract) methods
   on `XArrayReader` in `reader.py`, used identically by both backends:
   - `_antenna_lookup_table()` — MS-wide, built once per open backend,
     memoized. `baseline_id` (already cheap, already broadcast) gets
     fancy-indexed against it at the end.
   - `_scan_lookup_for_partition(raw_ds)` / `_scan_time_index(lookup, ds)`
     — per-partition, keyed by a hashable `_PartitionIdentity` (needed
     because `_iter_visibility_partitions()` yields a fresh `Dataset`
     wrapper object every call even though the underlying data doesn't
     change). Only a cheap integer position is ever broadcast across the
     full grid; the actual `scan_name` values are attached via fancy-index
     at the end, same as antenna.
   - `_as_object_column(values, index)` — wraps the fancy-indexed result
     as an explicit `dtype=object` `pandas.Series` before assignment,
     working around the pandas 3.0 cost mentioned above. This one matters
     for *any* future column with many distinct string values, not just
     these three.

   `scan_lookup` is computed by the caller (`_query_columns_raw`, from the
   *raw* partition, before `_apply_selection`) and passed into
   `_query_partition_scatter` as a parameter — it can't be reconstructed
   from the already-selected `ds` this method receives, since a
   `time_range` selection narrows exactly the span `_PartitionIdentity`
   needs to stay stable.

2. **`MSv4Backend._query_all_partitions_scatter_fused`** (OPT-B) — **a
   real, independent code path**, not a caller of `_query_partition_scatter`.
   MSv2Backend has no equivalent; this only exists on the MSv4 side, for
   parallelized cross-partition Zarr reads. **This path carried zero id
   columns before Part 2** — not even the pre-existing hover-probe
   `time`/`baseline_id`/`frequency` — so any selection wide enough to span
   multiple partitions and cross `_THRESH_FUSED` (routing here instead of
   the per-partition path — a common case, not an edge case) was silently
   dropping them. Fixed alongside the new columns, since fixing one without
   the other wasn't really separable (same per-partition metadata reads,
   same layout-tracking mechanism). If Part 3 changes anything about how
   columns are threaded through `_query_partition_scatter`, **check this
   method too** — nothing enforces the two stay in sync short of remembering
   to.

   Uses the same cached-lookup mechanism as item 1 above (not the original
   broadcast), which meant a real interface change: `_query_columns_raw`'s
   `selected` list is now `list[tuple[Dataset, Optional[_PartitionScanLookup]]]`,
   not a bare list of datasets — the scan lookup for each partition is
   computed once, while the caller still has the raw (pre-selection)
   partition in scope, and threaded through from there. `_antenna_lookup_table()`
   needs no such threading (it's MS-wide, fetched directly wherever needed).
   If Part 3/4/5 ever touches this `selected` list's shape, both the tuple
   unpacking here and in the per-partition branch of `_query_columns_raw`
   need to move together.

3. **`reader.py`'s `_compute_axis_values`** — NOT touched, and worth knowing
   why: this function references `ds.attrs.get("spectral_window_id")` /
   `ds.attrs.get("observation_id")` / `ds.attrs.get("intent")`, none of
   which exist in real xarray-ms 0.5.6 output (confirmed directly). It's
   imported by both backends but, as far as grep shows, never actually
   called by either — dead code, pre-dating (or diverging from) the real
   per-row column mechanism. Left alone since it's outside Part 2's declared
   scope and touching shared `reader.py` wasn't necessary for anything here,
   but it's misleading if anyone reads it looking for how SPW/Observation/
   Intent actually work. Candidate for a follow-up cleanup/removal.

## Axes dropped or deferred, and why

**Observation** — dropped. No surfaced identity anywhere: not a coordinate,
not in `ds.attrs`, in either backend, on real data. Confirmed by opening the
real MSv2 file directly via `xarray-ms` (`xr.open_datatree(..., engine=
"xarray-ms:msv2")`) and the real `.ps.zarr` via plain `zarr`/`xarray` — full
`ds.attrs` dump for both, no key resembling an observation ID. The MSv4
schema's `ObservationInfoDict` (`xradio.measurement_set.schema`) confirms
this isn't a gap in what got inspected — the schema itself only defines
observer/project/UID/release-date fields, no numeric ID. Getting one would
mean bypassing xarray-ms/xradio and reading MAIN's `OBSERVATION_ID` column
directly via `arcae` — a genuinely separate mechanism from everything else
here, not a small extension of it.

**Intent** — dropped. The only surfaced form is
`ds.coords["scan_name"].attrs["scan_intents"]`, confirmed on real data to be
a **partition-level union list**: a partition covering scans 4 and 33
reported `['CALIBRATE_BANDPASS#ON_SOURCE', 'CALIBRATE_PHASE#ON_SOURCE',
'CALIBRATE_WVR#ON_SOURCE']` — three intents for two scans, i.e. not
resolvable to one value per row or even per scan from this attribute alone.
`xradio`'s schema docstring for this field confirms it's literally the MSv2
STATE table's comma-separated `OBS_MODE`, collapsed once per partition at
conversion time. The real MS's own STATE table has 20 distinct `STATE_ID`s
with individually varying `OBS_MODE`, referenced per-row from MAIN — a
correct per-row Intent needs that join, which neither backend does today.

**Correlation** — plumbed, but flagged degenerate, not dropped.
`ScatterLayerSpec.polarization: str` is a scalar — a layer already plots
exactly one polarization, so its `polarization` column is always exactly one
category. The plumbing costs nothing (it's the same value the caller already
selected), so it's there and correct, but colorizing by it can never produce
more than one color for a single layer. Part 3/4's call on whether it's
worth surfacing as a *selectable* UI option given that.

## Real cardinality findings

Measured directly against `sis14_twhya_calibrated_flagged` (26 antennas,
1 SPW, 1 observation, 2 correlations, 17 scans) — see design doc §4.2 for
the table. Headline: **Antenna1/Antenna2 already sit at the proposed ~20
cap on this fairly modest array.** A larger ALMA config (43–50+ antennas) or
even a longer VLA track will exceed it routinely. Worth deciding before
Part 4 whether antenna axes get a different (higher, or auto-bucketed) cap,
or whether "refuse and ask to narrow" is genuinely fine as the common-case
UX for those two axes specifically.

SPW and Observation cardinality could not be stress-tested — this MS only
has one of each. No specific concern raised by that, just noting the gap in
what could be verified.

## Verification

Two layers now, not one:

**`test_msv2_backend.py`/`test_msv4_backend.py`'s `TestColorizeByAxisColumns`**
classes are the permanent home for this coverage (folded in from the
standalone script after Part 2 wrapped). Between the two files, against
real MSv2 and MSv4 data:

- All five columns present in `_query_columns_raw()`'s output.
- Spot-checked values match raw MAIN/ANTENNA/SCAN_NUMBER ground truth via
  `arcae` (MSv2 only — `arcae` is what visplot actually depends on, not
  `python-casacore`).
- MSv2's fused vs. serial paths agree, including the new columns (and,
  since the 2026-09 follow-up, the `scan_lookup` parameter both branches
  now require).
- MSv4's OPT-B vs. per-partition paths agree, including the new columns —
  the check that would have caught the original OPT-B gap.
- `scan_name`/`baseline_antenna1_name`/`baseline_antenna2_name` stay
  `dtype=object` (guards the pandas 3.0 conversion cost from silently
  creeping back in) and the internal `"__scan_time_idx"` bookkeeping
  column never leaks into the returned DataFrame.

**`verify_colorize_axis_part2.py`** (delivered alongside the original
handoff) still runs and still passes — no `cubevis` install required,
imports the real package instead of assuming its own file location. It
predates the 2026-09 follow-up, so it doesn't check the two items above
specific to that (dtype, bookkeeping leak); the permanent suite is the
more complete reference now. Keeping it around is optional at this point.

All checks pass as of this update. Rerun with `MS=`/`PS=` pointing at any
other real MS/MSv4 pair to re-verify on different data.

## Suggested first steps for Part 3

- Build the `Axis → column name` lookup table from the "What landed" table
  above — it doesn't exist yet.
- Decide how to handle `spw`'s mixed possible dtype (int or str across
  partitions) before building a `pandas.Categorical` from it — see design
  doc §7's new open question.
- `_query_all_partitions_scatter_fused` is the one place a future change to
  `_query_partition_scatter` won't automatically propagate to — grep for it
  before assuming a MSv2/MSv4-symmetric edit is actually symmetric.
- If Part 3 (or Part 5) needs a new per-row column with many distinct
  string values, reuse the cached-lookup pattern on `XArrayReader`
  (`_PartitionIdentity`/`_PartitionScanLookup`/`_scan_time_index`/
  `_antenna_lookup_table`/`_as_object_column`) rather than a fresh
  `broadcast_like(template)` — that was Part 2's original approach for
  exactly these three columns, and it measurably regressed a pre-existing
  timing test (see design doc §3). The pandas 3.0 string-dtype cost
  `_as_object_column` works around is not specific to this feature — it'll
  bite any future column built the naive way, categorical or not.
