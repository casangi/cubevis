# visplot Scatter "Colorize by Axis" — Design Document

**Status:** v4 — Parts 1–2 complete (design + backend plumbing, plus a
performance follow-up); Part 5 (statistical/rflag-style colorization)
scoped at the design level; Parts 3–4 not started.
**This is a living document.** Update it as later parts surface new
information or force a decision to change; don't create a competing copy.
See the changelog at the bottom for revision history.

---

## 1. Background

Originated from a stakeholder claim that `VidaVis` supported choosing an
aggregation function for scatter point coloring, plus metadata-based ("third
dimension") coloring. Investigation (see prior session notes /
`visplot-scatter-aggregation-and-plotms-parity.md`) traced these to two
separate, real things:

- VidaVis's actual aggregator feature (`max`/`mean`/`median`/`min`/`std`/`sum`/`var`)
  belongs to `MsRaster`, not scatter, and reduces a genuine non-plotted xarray
  dimension before any rendering step.
- The "color by metadata" recollection was confirmed by the stakeholder to
  actually be CASA **PlotMS**'s "colorize by axis" feature — coloring scatter
  points by a categorical axis (baseline, antenna, correlation, scan, SPW,
  etc.), one of PlotMS's more valued features.

Of everything reviewed in the PlotMS parity pass, **colorize-by-axis is the
only aggregation-adjacent gap with both a confirmed need and a concrete,
understood implementation mechanism** (Datashader's categorical aggregation).
This document scopes that feature.

---

## 2. Feature definition

For a given scatter layer, the user selects a **categorical axis** (e.g.
Correlation, Scan, SPW, Antenna1) instead of the current continuous
value-based coloring. Each rendered pixel is colored by the dominant category
among the raw samples landing there (Datashader `ds.by()` / `count_cat()`),
with a legend mapping category → color. This **replaces** that layer's
continuous `mean(y)`-based coloring and colorbar — it does not run alongside it.

---

## 3. Current state (what colorize-by-axis has to fit into)

Verified directly against the source; this is the baseline every later part
should build from rather than re-derive.

**Today's per-pixel coloring** (`_scatter_render.py`):
```python
agg = cvs.points(df, "x", "y", ds_agg.mean("y"))
```
One continuous scalar per pixel, run through `linear`/`log`/`eq_hist`/explicit
scaling to a colormap gradient.

**`ScatterLayerSpec`** (`reader.py`) — all fields are continuous-coloring
concepts; nothing categorical exists today:
```python
y_axis, polarization, cmap (tuple[str,...] — a gradient),
alpha, scaling ("eq_hist" default), scaling_alpha, scaling_gamma,
scaling_vmin, scaling_vmax
```

**`ScatterLayerRender`** (`reader.py`) — same story: `peak_value`,
`hist_counts`/`hist_edges`, `mapping_x`/`mapping_u` all exist specifically to
drive a continuous colorbar. The `id_grid_*` fields (min/max of
time/baseline_id/frequency per coarse bin) are range-based, for the
hover-probe — not categorical either.

**Metadata already in the per-row dataframe:** `time`, `baseline_id`,
`frequency` are *conditionally* present today, populated only for the
hover-probe id-grid (`_scatter_render.py`'s `id_cols` check), not for coloring.
No other discrete axis (correlation, scan, SPW, antenna, observation, intent)
is currently carried per-row.

**Palettes** (`palettes.py`): only continuous sequential ramps exist today,
theme-aware (light/dark), keyed as a "family" for multi-polarization layers.
No categorical/distinguishable-color-set palette exists.

**Legends** (`panel_spec.py`, `png_export.py`): today's legend is per-band
(polarization), with peak density folded in — a single swatch per layer, not
per category.

**Backend symmetry constraint:** `query_columns()` is documented as
"implemented identically by both `MSv2Backend` and `MSv4Backend`" — any new
column-plumbing work has to land in both and stay in sync. **Part 2
correction:** since the 2026-09 `query_columns()` redesign, `query_columns()`
itself no longer returns a per-row DataFrame at all — it bins/shades
internally now. The per-row target for this kind of work is
`_query_columns_raw()` → `_query_partition_scatter()`. The "implemented
identically" claim also isn't quite true below that level: MSv4Backend has
an additional cross-partition-fused path, `_query_all_partitions_scatter_fused`
(OPT-B, for parallel Zarr reads), that MSv2Backend has no equivalent of —
it's an independent code path, not a caller of `_query_partition_scatter`,
so anything landing in one has to be separately landed in the other too
(Part 2 found this the hard way — see the Part 3 handoff).

**Performance regression and fix (2026-09 follow-up).** Part 2's original
`scan_name`/`baseline_antenna1_name`/`baseline_antenna2_name` plumbing
broadcast each coordinate across the full per-partition sample grid, the
same mechanism already used for `time`/`baseline_id`/`frequency`. Measured
on real data, this added ~1.45s of near-identical fixed overhead to *both*
the fused and serial pipelines — small in absolute terms, but enough to
drop `TestQueryColumnsRendered.test_fused_faster_than_serial` (a
pre-existing regression test, unrelated to this feature until this) from
its calibrated ≥2.0x speedup down to ~1.8x, since a cost added equally to
both sides of a ratio shrinks the ratio itself. Root-caused and fixed in
two stages, both genuinely worth knowing about before touching this code
again:

1. **The broadcast itself was the wrong shape.** Replaced by cached
   per-MS/per-partition lookup tables — `_PartitionIdentity`,
   `_PartitionScanLookup`, `_scan_time_index`, `_antenna_lookup_table`, all
   new shared (non-abstract) methods on `XArrayReader` in `reader.py`, used
   identically by both backends rather than duplicated. Only a cheap
   integer index is ever broadcast across the full grid now (same class as
   `baseline_id`); the actual string values are attached via a fast
   fancy-index lookup against the small cached table, at the end, touching
   only the already-filtered output rows. `_antenna_lookup_table` is
   MS-wide (built once, matching `identity_tables()`'s own existing
   assumption that `baseline_id → antenna names` is consistent across
   every partition); `_scan_lookup_for_partition` is per-partition, keyed
   by a hashable `_PartitionIdentity` since `_iter_visibility_partitions()`
   yields a fresh `Dataset` wrapper object on every call even though the
   underlying data doesn't change.
2. **A second, genuinely surprising cost, found only by direct
   benchmarking:** pandas 3.0's `future.infer_string` option (on by
   default) silently upgrades a plain object-dtype array of many distinct
   strings to its newer `str` dtype on column assignment — and that
   conversion measured ~5x more expensive than constructing the column as
   an explicit `dtype=object` `pandas.Series` first (~245ms vs. ~49ms at
   4M rows). This turned out to be the *larger* of the two costs, well
   past the lookup computation itself. Fixed locally
   (`XArrayReader._as_object_column`, used only for the columns that need
   it), deliberately not via a global `pd.set_option(...)` — changing
   pandas's string-dtype behavior for the entire process is a much bigger
   decision than this one lookup, and doesn't belong buried in a
   data-reading method.

Net result, measured back-to-back against a pre-Part-2 baseline of ~2.5x,
on two different machines: ~2.0x and ~1.5x. Real, substantial recovery —
not a full one. `test_fused_faster_than_serial`'s threshold was lowered to
1.2x accordingly (see the test's own docstring for the full rationale);
chasing the remaining gap further hit clearly diminishing returns and was
deliberately not pursued past this point. Two new permanent regression
tests guard the specific findings above: one asserting `scan_name`/antenna
columns stay `dtype=object` (so the pandas conversion cost can't silently
creep back in unnoticed), one confirming the internal `"__scan_time_idx"`
bookkeeping column never leaks into user-facing output.

This cached-lookup pattern is now real, reusable shared infrastructure —
worth Part 3/4 (and Part 5's own backend work, §7.5's cost-tiering
discussion in particular) checking for before building a parallel
mechanism for any future per-row categorical/derived column with many
distinct string values.

---

## 4. Scope decisions

Each item below is either a **firm architectural choice** (low ambiguity,
stated with rationale) or a **proposed default** (a real judgment call —
flagged explicitly, revise freely).

### 4.1 Which axes are colorizable — *updated after Part 2 (see below)*
Start with the bounded-cardinality `NATIVE_DISCRETE` axes already defined in
`axes.py`: **Correlation, Scan, SPW, Antenna1, Antenna2, Observation, Intent.**

**Part 2 finding — Observation and Intent deferred.** Verified against real
MSv2 and MSv4 data (both via direct inspection and against `xradio`'s
installed schema module, `measurement_set/schema.py`):
- **Observation** has no surfaced identity at all in either backend — not a
  coordinate, not in `ds.attrs`. `ObservationInfoDict` (the MSv4 schema's
  required `observation_info` attr) carries observer/project/UID fields but
  no numeric ID. Getting it would mean bypassing xarray-ms/xradio entirely
  and reading MAIN's `OBSERVATION_ID` column directly.
- **Intent** is only exposed as `scan_name.attrs["scan_intents"]` — a
  **partition-level union list** (confirmed on real data: a partition
  spanning 2 scans reported the combined 3 intents across both), not a
  per-row or even per-scan value. The schema's own docstring confirms this
  is literally the MSv2 STATE table's comma-separated `OBS_MODE`, collapsed
  to one list per partition. A genuine per-row Intent needs a `STATE_ID` →
  `OBS_MODE` join neither backend does today.

Both are dropped from the in-scope list rather than force-fit. See
`visplot-colorize-by-axis-handoff-part3.md` for the full evidence trail.

**Part 2 finding — Correlation is degenerate.** `ScatterLayerSpec.polarization`
is a single scalar (not a set) — a scatter layer already plots exactly one
polarization. So a per-row "Correlation" column is always exactly one
category for any given layer; colorizing by it would never show more than a
single color. The column is still plumbed (harmless, keeps the mechanism
uniform, and costs nothing extra), but Part 3/4 should weigh whether it's
worth exposing as a *selectable* colorize axis in the UI given it can never
do anything visually for a single layer. Revisit if `ScatterLayerSpec` ever
grows multi-polarization-per-layer support for an unrelated reason.

**Confirmed in scope, verified against real data: Scan, SPW, Antenna1,
Antenna2.**

**Baseline is proposed as excluded** (or at least deprioritized): a typical
array has enough baselines that per-category colors stop being visually
distinguishable, and this matches a known usability limitation of PlotMS's
own baseline colorization. Worth confirming against your own typical dataset
sizes before treating this as final.

### 4.2 Cardinality cap — *proposed default, now with real data behind it*
Cap at **~20 categories** (comparable to a standard categorical palette like
Bokeh's Category20). If the current selection resolves to more distinct
values than the cap for the chosen axis, refuse with a message asking the
user to narrow the selection, rather than silently bucketing extras into an
"other" category. Auto-bucketing is a reasonable future enhancement, but
adds complexity (legend semantics for "other") not needed for a first version.

**Part 2 finding — real cardinality, `sis14_twhya_calibrated_flagged` (a
modest 26-antenna, single-SPW, single-observation ALMA test MS):**

| Axis | Distinct values | vs. ~20 cap |
|---|---|---|
| Scan | 17 | under, but not by much |
| Antenna1 | 20 | **at the cap** |
| Antenna2 | 20 | **at the cap** |
| Correlation | 2 | well under (but degenerate per layer, see §4.1) |
| SPW | 1 | untested — this MS has only one SPW |

Antenna1/Antenna2 sitting exactly at the proposed cap on a fairly small
26-antenna array is a real signal, not a hypothetical: full ALMA
configurations run 43–50+ antennas, and VLA runs 27. Antenna colorization
will hit the "refuse and ask to narrow" path routinely in normal use, not
as an edge case — worth deciding now whether that's acceptable UX or
whether Antenna1/Antenna2 need a higher or axis-specific cap (open question
carried to §8).

### 4.3 Interaction with existing scaling controls — *firm*
Enabling colorize-by-axis on a layer disables/hides that layer's continuous
scaling controls (colormap picker, span/vmin/vmax, eq_hist toggle) and swaps
its colorbar for the new categorical legend. The two coloring modes are
mutually exclusive per layer, not combinable.

### 4.4 Per-layer vs. plot-wide — *proposed default*
Colorize-by-axis is a **per-layer** setting (consistent with the existing
`ScatterLayerSpec` model — e.g. today's independent XX/YY continuous
coloring). A layer could be colorized while a sibling layer stays continuous.
Flagging this as worth a sanity check: it's the architecturally natural
choice, but confirm it's actually the UX you want before Part 4 builds to it.

### 4.5 Data availability gates the axis list — *resolved by Part 2*
Whichever axes are chosen for §4.1 must actually be plumbed as per-row
columns before Part 3 can do anything with them — this was Part 2's job.
Resolved: Scan, SPW, Antenna1, Antenna2 plumbed and verified against real
data in both backends; Observation and Intent dropped (see §4.1); Correlation
plumbed but flagged degenerate (see §4.1). Column naming convention Part 2
settled on: the MS-native coordinate/attribute name where one exists
(`scan_name`, `baseline_antenna1_name`, `baseline_antenna2_name`,
`polarization`), and the establish `SelectionSpec`/axes.py vocabulary word
where it doesn't (`spw`) — see
`visplot-colorize-by-axis-handoff-part3.md` for the full column-to-axis
mapping Part 3 needs.

---

## 5. Roadmap / part breakdown

| Part | Scope | Primary files | Status |
|---|---|---|---|
| 1 | Design + this document + Part 2 handoff | — | **This session** |
| 2 | Backend metadata plumbing | `msv2_backend.py`, `msv4_backend.py`, `reader.py` | **Done**, including a 2026-09 performance follow-up — see Part 3 handoff and §3 |
| 3 | Rendering pipeline: categorical aggregation, new dataclass fields, categorical palette | `_scatter_render.py`, `reader.py`, `palettes.py` | Not started — see §7.7 for a Part 5 touchpoint worth building in now |
| 4 | UI wiring: axis picker, mutual-exclusivity logic, category legend widget, export swatches | `visibility_plotter.py`, `panel_spec.py`, `png_export.py` | Not started — see §7.7 for a Part 5 touchpoint worth building in now |
| 5 | Statistical/rflag-style colorization (raster + scatter) — see §7 | `msv2_backend.py`, `msv4_backend.py`, `_scatter_render.py`, `reader.py`, `axes.py`, `visibility_plotter.py` | **Scoped (design only), this session** — likely needs its own backend/render/UI sub-passes once started, the same way Parts 2–4 broke up the original feature |

Each part produces a handoff document for the next (scoped work order +
open questions), and updates this design document if it forces a decision to
change. This document is the durable "why," the handoffs are the disposable
"what's next."

---

## 6. Explicit non-goals for this feature

Carried over from the broader PlotMS parity review — not part of
colorize-by-axis, don't let scope creep pull them in:

- Plot-time averaging (separate gap, separate design needed)
- Broader iteration axes beyond Field/SPW
- Data-column (DATA/CORRECTED/MODEL) overlay
- `dynspread` for zoomed views
- ATM/Tsys curve overlay

---

## 7. Part 5 — Statistical / rflag-style colorization (raster + scatter)

### 7.1 Motivation and origin

This started from a real astronomer workflow, not a hypothetical: bad
data is routinely diagnosed by connecting it back to the receiver(s)
that produced it (RFI, hardware malfunction, gain problems). It's
already documented as a target use case —
`visibility_plotter_implementation_plan.md`'s astronomer-workflow table
lists "Bad antenna: all baselines to one antenna deviant" with the
prescribed plot as "Scatter: amp vs time, colour-by-baseline, iterate
by antenna." That's a manual, eyeball-driven process today. The idea
that came out of discussion: automate the "does this look wrong"
judgment with a computed statistical-deviation coloring, so the
deviant pattern is visually immediate rather than something the
astronomer has to notice by comparing many plots.

That idea maps closely onto CASA's own `rflag`/`tfcrop` algorithms —
real, long-established, widely-used automated flagging tools (both
still actively referenced in current ALMA/VLA pipeline documentation).
Anchoring Part 5 on "an approximation of rflag" rather than an invented
metric is a deliberate choice: it gives astronomers something they
already have intuition for, and gives a known quantity to compare
against. See `visplot-rflag-colorization-reference.md` for the
algorithm detail this section summarizes.

### 7.2 What this is (and isn't) — firm

**This is a rendering/coloring feature, not a flagging feature.**
`rflag`/`tfcrop` write directly to the MS's `FLAG` column; Part 5 does
not write anything — it computes a score and colors with it, the same
way today's continuous `mean(y)` coloring or the new categorical
colorize-by-axis coloring do. A user who spots a deviant region still
flags it manually via the existing `FlagTool`/`UnflagTool` — this
*helps them find where to look*, it doesn't replace the decision or
the mechanism.

**This is an approximation in statistical philosophy, not a
reimplementation.** The goal is the same instinct `rflag` uses
(robust, median-based local statistics catching departures from a
slowly-varying "typical" signal), not bit-exact reproduction of CASA's
specific procedural algorithm (iterative fit convergence, threshold
auto-calculation, `extendflags` logic, and so on). Chasing exact
parity would be a much larger, different project.

### 7.3 The reference-group decision — open, for Part 5's own backend pass to resolve

Carried forward from discussion: "distance from typical" is
underspecified until the comparison population is fixed, and the
choice determines what gets diagnosed:

| Reference group | Diagnoses | Relationship to `rflag` |
|---|---|---|
| Per (baseline, SPW, correlation), across time/frequency | Localized RFI, transient spikes | Directly matches `rflag`'s own scope |
| Per antenna, aggregating across its baselines | A consistently deviant antenna → likely receiver/hardware fault — **the originating ask** | A derived aggregation on top of the per-baseline score, not a separate computation — conceptually the same decomposition antenna-based gain calibration (`gaincal`/self-cal) already relies on: a bad baseline could implicate either antenna or the pair specifically, and only aggregating across many different partners disambiguates |
| Per (time, frequency) bin, across baselines | Array-wide/correlator-wide RFI | New relative to `rflag`, which is inherently per-baseline |

Recommendation carried from discussion: implement the per-baseline
(rflag-shaped) score first, since it's directly reusable, then build
per-antenna aggregation on top of it rather than as an independent
computation.

### 7.4 Metric — proposed default

Modified/robust z-score: `0.6745 × (x − median(x)) / MAD(x)`, where
`MAD = median(|x − median(x)|)`. Computed on **real and imaginary
parts separately**, not on amplitude directly, matching `rflag`'s own
choice — visibility amplitude isn't Gaussian at low SNR (closer to
Rician/Rayleigh), while real/imaginary parts, noise-dominated, are.
The statistics *basis* (real/imag) and the *displayed* axis (amplitude,
phase, whatever the layer currently plots) are independent decisions —
this proposes always computing on real/imag regardless of what's shown.

### 7.5 Cost tiers — firm, carried from discussion

Two genuinely different costs, matching the "sometimes I'll pay for
extended computation" framing this started from:

- **Cheap (local/windowed, within a partition):** close to free
  relative to the per-partition compute already happening — arguably
  usable without an explicit opt-in.
- **Expensive (global reference, e.g. per-antenna across a whole
  selection):** a genuine two-pass computation — reduce across every
  selected partition for the reference statistic, then score every row
  against it. This is the tier that needs the explicit gate. Within
  it, mean/variance-based scores reduce cheaply and distribute
  naturally (associative, tree-reduction, scales to a cluster with no
  drama); median/MAD-based scores (the more robust, `rflag`-faithful
  choice) do not — exact computation needs a full sort/selection at
  scale, so distributed systems default to approximate quantile
  sketches (t-digest) instead. See
  `visplot-rflag-colorization-reference.md` §3 for the full technical
  treatment, and `visplot-statistics-dataflow-notes.md` for the
  related (but separate — see below) question of whether any of this
  can ride "for free" on the existing data load.

**Explicitly out of scope for Part 5 itself:** the "harvest statistics
during loading" idea from `visplot-statistics-dataflow-notes.md`. That
document is background rationale for whichever future backend pass
implements the expensive tier — not an instruction folded into Part 5
now. The conclusion already reached: any harvesting should ride behind
the same opt-in gate as the feature consuming it, never computed
speculatively.

### 7.6 Raster and scatter integration

**Proposed mechanism: a new `Axis` member** (name TBD — `Axis.DEVIATION`
is a placeholder) under `AxisType.DERIVED`, alongside `AMPLITUDE`/
`PHASE`/`REAL`/`IMAGINARY`. This is the single most leveraged decision
in this section: `AxisType.DERIVED` axes are already usable everywhere
those are — raster Y, raster X, raster Quantity, scatter X, scatter Y —
with no bespoke UI. Framed this way, "plot the deviation score
directly" (e.g. a waterfall with Quantity=Deviation instead of
Quantity=Amplitude) needs **no new UI mechanism**, only backend support
for computing the value — it falls through the same dropdowns
`FLAG_FRACTION`/`WEIGHT` are already waiting to be added to per the
implementation plan's §4.3.

**Existing presets already give this a home, exactly as guessed:**
- **`waterfall`** (Time × Channel raster, colored by Quantity) is
  structurally identical to the 2D grid `rflag`'s time-analysis and
  spectral-analysis steps operate on — sliding down a column is the
  time analysis, along a row is the spectral analysis. This is the
  most direct `rflag`-shaped target and the natural first
  implementation case.
- **`vplot`/`radplot`** (Baseline × Time raster, colored by Quantity)
  combined with the existing per-antenna iteration (§4.8) is the exact
  realization of the implementation plan's "Bad antenna" workflow —
  swap the coloring Quantity from Amplitude to Deviation in that exact
  configuration and the deviant-antenna pattern that currently requires
  eyeballing several iterations becomes visually immediate.

One capability beyond "plot the score as its own axis" is worth keeping
in scope, since it's closer to the original ask: **coloring an existing
amplitude/phase layer *by* the score**, rather than replacing the
plotted quantity with it (e.g. `vplot`'s scatter stays Time-vs-Amplitude,
but point color comes from Deviation). Today's continuous coloring
(`_scatter_render.py`) aggregates the same column being plotted
(`ds_agg.mean("y")`); this needs the aggregation source decoupled from
the plotted Y column (`ds_agg.mean("<deviation column>")` against a
`df` that carries both) — a `ScatterLayerSpec` field for "color source
column," distinct from `y_axis`, and the deviation value present as an
extra per-row column (computed, not just copied — a materially
different backend task than Part 2's metadata plumbing, worth not
conflating with it).

**Scatter and raster are complementary here, not redundant:** raster
necessarily bins into pixels before showing anything; scatter can (at
lower zoom/sample counts) show the true per-sample score before any
spatial binning smooths it out. Both views are worth having rather than
picking one.

### 7.7 Touchpoints for Parts 3 and 4 — cheap now, expensive to retrofit later

Two things worth building into Parts 3/4 *now*, even though Part 5
hasn't started, because they're nearly free as part of work already
planned and materially more expensive to unwind after the fact:

- **Part 3:** whatever field distinguishes today's continuous coloring
  from the new categorical colorize-by-axis coloring should be an
  extensible mode value (e.g. a string/enum: `"continuous"` /
  `"categorical"`), not a boolean. Part 5 will want a third mode
  (`"computed"` or similar) for the color-source-column capability in
  §7.6 — cheap to leave room for now, a real (if small) rework to
  retrofit onto a two-state boolean later.
- **Part 4:** the mutual-exclusivity logic that hides a layer's
  continuous scaling controls when colorize-by-axis is enabled should
  be written as an N-way mode switch, not an if/else pair, for the
  same reason.

**Good news reducing everything else:** the named-preset mechanism
(`_PRESETS`, `_preset_js`) and the per-slot gear-tab sidebar config
(P-5a, already shipped) are both already fully generic over axis/
Quantity choices — Part 5 needs **no new preset or sidebar
infrastructure**, just new values flowing through what's there
(a new `Axis` member, and eventually new gear-tab controls for window
size/threshold, using the exact pattern existing controls already use).

### 7.8 Non-goals for Part 5 — firm

- No flag-writing of any kind — visualization only (§7.2).
- No bit-exact reproduction of CASA's `rflag`/`tfcrop` procedural
  algorithm — statistical philosophy, not the algorithm itself (§7.2).
- No default-on global/expensive statistics tier — opt-in only (§7.5).
- No speculative "harvest during load" implementation — background
  notes only, gated behind a real consumer (§7.5,
  `visplot-statistics-dataflow-notes.md`).
- No new preset or sidebar mechanism — reuse what Parts 4/5a already
  shipped (§7.7).

### 7.9 Open questions

- [ ] Which reference group (§7.3) is the actual first target — or is
      per-baseline-then-per-antenna-aggregation (the recommended order)
      confirmed?
- [ ] Naming for the new `Axis` member (`DEVIATION` is a placeholder).
- [ ] Is real/imaginary the right statistics basis by default, or
      should it be configurable per layer (§7.4)?
- [ ] Worth pursuing real `flagdata(action='calculate')` as an MSv2-side
      validation oracle (`visplot-rflag-colorization-reference.md` §2)?
      Blocked on confirming whether it supports MSv4 Processing Sets at
      all — needs Darrell's own CASA6 knowledge, not further guessing.
- [ ] Will Part 5, once started, need its own design→backend→render→UI
      breakdown the way the original feature used Parts 2–4? (Current
      guess: yes, once scoping firms up further.)

---

## 8. Open questions log

Quick-scan list of everything not yet confirmed. Move resolved items to §4
with the decision recorded; add new ones as they surface.

- [ ] Is Baseline really out of scope, or is there a workflow where it's
      still wanted despite high cardinality (§4.1)?
- [x] ~~Is 20 categories the right cap, or should it vary by axis (§4.2)?~~
      Not resolved, but now backed by real numbers: Antenna1/Antenna2 sit
      *at* 20 on a modest 26-antenna array (§4.2) — Part 3/4 should decide
      whether antenna axes get a higher/separate cap before this becomes a
      routine "selection refused" experience.
- [ ] Per-layer colorization confirmed as the desired UX, not plot-wide (§4.4)?
- [x] ~~Any axis in §4.1 that Part 2 finds materially harder to plumb than
      the others — should it be dropped or deferred to a v2?~~ Yes:
      Observation and Intent, both dropped (§4.1). Correlation plumbed but
      found degenerate for a single-polarization layer (§4.1) — worth a
      decision on whether it's worth exposing in the Part 4 UI at all.
- [ ] New: is a single-dtype (all-`str` or all-`int`) `spw` column worth
      normalizing to at the Part 3 boundary, given `_partition_spw_ident`
      can return either depending on what the store provides? Part 2 left
      the native type as-is (mixed dtype possible across partitions) rather
      than force a cast nothing downstream needed yet.

---

## 9. Changelog

- **v1** — Initial design, written end of Part 1. Scope decisions in §4 are
  proposals pending confirmation; roadmap and non-goals are considered firm.
- **v2** — Part 2 complete. Scan/SPW/Antenna1/Antenna2 plumbed and verified
  against real MSv2 + MSv4 data in both backends (including a real, MSv4-only
  cross-partition path that needed its own fix — see §3's backend-symmetry
  correction). Observation and Intent dropped from scope (§4.1) — neither is
  exposed by the current xarray-ms/xradio schema on real data. Correlation
  plumbed but found degenerate for a single-polarization layer (§4.1). Real
  cardinality numbers added to §4.2. See
  `visplot-colorize-by-axis-handoff-part3.md` for the full evidence and
  what Part 3 needs to know about where the columns live.
- **v3** — Part 5 scoped (design only): statistical/`rflag`-style
  colorization for both raster and scatter (§7), originating from a
  discussion about connecting anomalous data back to the antenna/receiver
  that produced it. Anchored on CASA's real `rflag`/`tfcrop` algorithms as
  known, widely-used prior art — visualization only, no flag-writing, an
  approximation in statistical philosophy rather than a literal
  reimplementation. Identified two concrete touchpoints worth building into
  Parts 3/4 now, before they start, since they're cheap now and expensive
  to retrofit later (§7.7). Two satellite documents split off the deeper
  material: `visplot-rflag-colorization-reference.md` (CASA algorithm
  detail, real-`flagdata`-as-validation-oracle idea, Dask/distributed
  feasibility) and `visplot-statistics-dataflow-notes.md` (background
  rationale on which statistics can ride "for free" on the existing data
  load and which can't — explicitly not an active goal of any current part).
- **v4** — Part 2 performance follow-up (§3): the original scan/antenna
  broadcast measurably regressed a pre-existing timing test; root-caused
  and fixed in two stages (a cached per-MS/per-partition lookup mechanism,
  new shared infrastructure on `XArrayReader`; then a pandas 3.0
  string-dtype conversion cost found by direct benchmarking, the larger of
  the two). Recovered most, not all, of the regression — accepted as a
  real, understood residual cost rather than chased further. This is the
  note several code comments already pointed to before it existed here;
  written now to close that gap.
