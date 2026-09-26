# visplot Scatter "Colorize by Axis" — Design Document

**Status:** v9 — Parts 1–4 complete (design, backend plumbing, rendering
pipeline, and UI wiring), plus a same-lineage Part 5/5a already shipped
(category-exclusion checklist and rarest/majority priority selection —
**unrelated to this document's Part 6**, see the naming note below).
Part 6 (statistical/rflag-style colorization, formerly numbered "Part 5"
in this document — see below) scoped at the design level, **Slices 1+2
fully specified with no remaining open pre-implementation questions**
(§7.10) — metric verified against the literature, raster+scatter
synergy factored in, coloring unified into a third `coloring` mode
(§7.4/§7.6/§7.10) — Slice 3 deferred pending user validation of 1+2.
pending user validation of 1+2 — see §7.
**This is a living document.** Update it as later parts surface new
information or force a decision to change; don't create a competing copy.
See the changelog at the bottom for revision history.

**Naming note (2026-09):** this document originally called the
statistical/rflag-style colorization feature in §7 "Part 5." By the time
that work was actually taken up, the numbers "Part 5"/"Part 5a" had
independently been used, in real shipped code, for a *different* feature
(the category-exclusion checklist and rarest/majority priority selection
for colorize-by-axis — see `colorize_controls()`'s own docstring in
`visibility_scatter.py`). To avoid two different features answering to
"Part 5" in code comments, docstrings, and handoff documents, every
reference to the statistical/rflag-style colorization feature has been
renumbered "Part 6" throughout this document and its two companions
(`visplot-rflag-colorization-reference.md`,
`visplot-statistics-dataflow-notes.md`). Nothing about the feature's
scope or design changed — this is a rename only.

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
worth Part 3/4 (and Part 6's own backend work, §7.5's cost-tiering
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

### 4.2 Cardinality cap — *firm, revised in Part 3 (see v5 changelog)*
**Superseded design, kept for history:** the original plan below was to cap
at ~20 categories and *refuse* a selection that resolves to more, asking the
user to narrow it. Part 3 replaced the refusal with automatic binning before
writing any rendering code, once real measurement showed refusal would make
the feature routinely unusable on large modern arrays (ngVLA: up to 263
antennas) — see immediately below for what replaced it and why, and
`visplot-colorize-by-axis-handoff-part4.md` for the full numeric evidence.

**What actually landed:** the cap is still ~20 (`_scatter_render.
CATEGORY_CAP`), but exceeding it now means *auto-binning* into at most 20
contiguous, near-equal groups (`_scatter_render._bin_categories`) — e.g. an
ngVLA-scale Antenna1 selection with 263 real antennas renders as 20 buckets
of ~13 antennas each, labelled by range (`"DA05–DA19"`), never a refusal.
Real per-point identity is unaffected by bucketing: it's resolved through the
existing exact hover-probe/click-to-region mechanism (`IdentityTables`,
`probe_scatter_region`), which was never derived from a rendered pixel's
color in the first place — bucketing only limits how many colors are shown
side by side in one glance.

**Why the cap is ~20 at all, now that refusal is gone:** two independent
constraints that happen to land near the same number, not one:

1. **Legibility.** More than ~20 simultaneous legend swatches is hard for a
   person to actually use, independent of how many real categories exist
   underneath. This doesn't move as antenna counts grow — it's a human-
   perception ceiling, not a data-size one. (`palettes.categorical_cmap()`
   supplies exactly 20 colors, matching Bokeh's Category20, for the same
   reason.)
2. **Rendering cost.** Measured directly (400×300 canvas, 1M rows,
   JIT-warmed, best-of-3): the categorical aggregation itself
   (`ds_agg.by`) is cheap (~0.14ms/category), but shading it dominates and
   scales linearly in category count. The original implementation used
   Datashader's own `tf.shade(agg, color_key=...)`, measured at
   ~1.5ms/category — extrapolated to a 1920×1080 canvas at K=263 (ngVLA's
   real antenna count), that's **~6.8 seconds**, and the backing
   aggregation array alone (`4 bytes × pixels × K`, confirmed exact) is
   **~2.2 GB, transient, per layer**. Both costs are driven by
   `canvas_pixels × K`, confirmed independent of how much data is actually
   plotted (50K vs. 8M rows changed shading time under 5%), so a sparse
   selection gets no discount. Binning down to a constant cap makes cost
   independent of true antenna count instead: a 26-antenna MS and an
   ngVLA 263-antenna one now cost the same to render.

This was never really a wire/message-overhead limit, despite an earlier
framing of it that way in discussion — the returned `image` is a flat H×W
array regardless of category count, and `categories`/`category_colors`/
`category_members` stay tiny (a few hundred bytes even at K=263) since none
of what crosses a process or wire boundary scales with this number. See
`_scatter_render.CATEGORY_CAP`'s docstring for the full numbers this section
summarizes.

**Also landed alongside binning, for the same cost reason:** categorical
shading itself switched from Datashader's `tf.shade(color_key=...)` (a
per-pixel color *blend* across whichever categories land there) to a
hand-rolled winner-take-all (`_scatter_render._argmax_shade`) — measured
4–9× faster at this cardinality range (the speedup *grows* with category
count), and, independently of speed, the only approach where a legend
actually means something: a blended pixel's color generally matches no
single swatch exactly, which is a real defect once Part 4 puts a legend on
screen to compare against. Every pixel `_argmax_shade` produces is exactly
one of the assigned colors, verified by direct test, never a blend.

**Part 2/3 finding — real cardinality, `sis14_twhya_calibrated_flagged` (a
modest 26-antenna, single-SPW, single-observation ALMA test MS):**

| Axis | Distinct values | vs. ~20 cap |
|---|---|---|
| Scan | 17 | under, but not by much |
| Antenna1 | 20 | **at the cap** (unbinned — exactly 20 real antennas) |
| Antenna2 | 20 | **at the cap** (unbinned) |
| Correlation | 2 | well under (but degenerate per layer, see §4.1) |
| SPW | 1 | untested — this MS has only one SPW |

Antenna1/Antenna2 sitting exactly at the cap on this modest 26-antenna array
no longer risks the "refuse and ask to narrow" experience the original plan
would have hit routinely — it renders 20 real (unbucketed) antenna colors
today, and would transparently bucket a larger array (full ALMA at 43–50+
antennas, ngVLA at up to 263) rather than degrade to a refusal.

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
| 3 | Rendering pipeline: categorical aggregation, new dataclass fields, categorical palette | `_scatter_render.py`, `reader.py`, `palettes.py` | **Done**, including a same-session revision replacing the cardinality-cap refusal with auto-binning and switching categorical shading to winner-take-all — see §4.2 and the Part 4 handoff |
| 4 | UI wiring: axis picker, mutual-exclusivity logic, category legend widget, export swatches | `visibility_plotter.py`, `panel_spec.py`, `png_export.py` | **Done** (verified directly against the source, 2026-09 — `colorize_controls()`/`_build_scatter_config_panel` in `visibility_scatter.py`, `_colorize_key_from_layer`/`_colorize_key_from_override` in `visibility_plotter.py`), plus the category-exclusion checklist and rarest/majority priority selection beyond this document's original scope (shipped as "Part 5"/"Part 5a" in the code — see the naming note above; unrelated to this document's Part 6) — see §7.7 for the Part 6 touchpoint (N-way coloring-mode switch) this already puts in place |
| 6 | Statistical/rflag-style colorization (raster + scatter) — see §7 | `msv2_backend.py`, `msv4_backend.py`, `_scatter_render.py`, `reader.py`, `axes.py`, `visibility_plotter.py`, `visibility_scatter.py` | **Scoped, Slices 1+2 approved for implementation** (2026-09) — Slice 3 (global reference, opt-in, cluster-relevant) deferred pending user validation of Slices 1+2; see §7.3/§7.5 for the slice breakdown and §7.10 for the approval record |

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

## 7. Part 6 — Statistical / rflag-style colorization (raster + scatter)

### 7.1 Motivation and origin

This started from a real astronomer workflow, not a hypothetical: bad
data is routinely diagnosed by connecting it back to the receiver(s)
that produced it (RFI, hardware malfunction, gain problems). It's
already documented as a target use case —
`visibility_plotter_implementation_plan.md`'s astronomer-workflow table
lists "Bad antenna: all baselines to one antenna deviant" with the
prescribed plot as "Scatter: amp vs time, colour-by-baseline, iterate
by antenna." That's a manual, eyeball-driven process today. The idea
that came out of discussion: surface a computed statistical-deviation
score as a coloring, so the deviant pattern is visually immediate
rather than something the astronomer has to notice by comparing many
plots — the tool surfaces the statistic, the astronomer still makes
the judgment (made explicit as a firm design constraint in §7.2 and
§7.3: no automated "this antenna is bad" assessment, ever).

That idea maps closely onto CASA's own `rflag`/`tfcrop` algorithms —
real, long-established, widely-used automated flagging tools (both
still actively referenced in current ALMA/VLA pipeline documentation).
Anchoring Part 6 on "an approximation of rflag" rather than an invented
metric is a deliberate choice: it gives astronomers something they
already have intuition for, and gives a known quantity to compare
against. See `visplot-rflag-colorization-reference.md` for the
algorithm detail this section summarizes.

### 7.2 What this is (and isn't) — firm

**This is a rendering/coloring feature, not a flagging feature.**
`rflag`/`tfcrop` write directly to the MS's `FLAG` column; Part 6 does
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

**This presents statistics; it never renders a verdict.** Confirmed
2026-09 (see §7.10): any per-group aggregation (per-antenna or
otherwise) surfaces the underlying numbers — the score itself, and
whichever summary statistics the reference-group computation produces
— never a qualitative judgment like "this antenna is bad." The
astronomer decides what a given score means for their data; the tool's
job stops at making the number, and its pattern across the plot,
visible. This applies to every reference group in §7.3, not just the
per-antenna one the motivating example in §7.1 happens to use.

**Independent of `casatools`/`python-casacore` — firm, confirmed
2026-09.** `flagdata(action='calculate')` (§7.9's now-resolved
validation-oracle question) does not operate on MSv4 Processing Sets,
which settles what was previously open in §7.9 and in
`visplot-rflag-colorization-reference.md` §2: Part 6's own statistics
computation is the actual deliverable on both backends, not a wrapper
around real `flagdata` on either one, and nothing in Part 6's runtime
path may depend on `casatools` or `python-casacore`. `arcae` remains
available if a lower-level MSv2 read genuinely needs it (it's already
a dependency of this codebase's MSv2 path via `xarray-ms`), but
`casatools`/`python-casacore` specifically are out — including for the
MSv2-side `flagdata`-as-verification-technique idea in
`visplot-rflag-colorization-reference.md` §2, which, if pursued at
all, must stay a one-off, offline comparison script run outside this
project's own dependency chain, never something Part 6 imports or
calls at runtime.

### 7.3 The reference-group decision — resolved 2026-09 (slices approved; see §7.10)

Carried forward from discussion: "distance from typical" is
underspecified until the comparison population is fixed, and the
choice determines what gets diagnosed:

| Reference group | Diagnoses | Relationship to `rflag` |
|---|---|---|
| Per (baseline, SPW, correlation), across time/frequency | Localized RFI, transient spikes | Directly matches `rflag`'s own scope |
| Per antenna, aggregating across its baselines | Surfaces which antenna's baselines carry the largest scores, for the astronomer to assess — **the originating ask** | A derived aggregation on top of the per-baseline score, not a separate computation — conceptually the same decomposition antenna-based gain calibration (`gaincal`/self-cal) already relies on: a bad baseline could implicate either antenna or the pair specifically, and only aggregating across many different partners disambiguates |
| Per (time, frequency) bin, across baselines | Array-wide/correlator-wide RFI | New relative to `rflag`, which is inherently per-baseline |

**Resolved:** implement the per-baseline (rflag-shaped) score first,
then build per-antenna aggregation on top of it rather than as an
independent computation — confirming the recommendation this section
originally carried forward from discussion, now the approved plan
(§7.10).

**A distinction worth separating from the table above, surfaced during
scope review (2026-09): "per-antenna" is not by itself the line
between the cheap and expensive cost tiers in §7.5.** Aggregating an
antenna's baselines *within whatever selection/iteration is already
loaded* (the vplot/radplot + iterate-by-antenna workflow this section's
motivating example describes) is a reduction over data already
resident — cheap, same tier as the per-baseline score itself, no opt-in
needed. What actually crosses into the expensive tier is scoring
against a reference computed over a *larger population than what's on
screen* — e.g., an antenna's typical behavior across the whole selected
observation, not just the currently-windowed/iterated view. That's a
genuine two-pass computation regardless of which reference group it's
attached to. See §7.5 and §7.10 for how this reshapes the slice
breakdown.

### 7.4 Metric — resolved 2026-09, verified against the literature (see §7.10)

**Basis: real and imaginary parts, treated jointly, not amplitude.**
Visibility noise is standard circularly symmetric complex Gaussian
noise — real and imaginary parts i.i.d., equal variance (Thompson,
Moran & Swenson, *Interferometry and Synthesis in Radio Astronomy* —
the field's standard reference; confirmed directly in current
literature, e.g. Kolopanis et al. 2023, arXiv:2211.13576 §3). Amplitude
is not Gaussian at low SNR (Rician/Rayleigh instead), which is exactly
why real/imaginary is the better statistical choice — this part of the
original proposal holds up. **Correction to how this was previously
framed:** it is not, as earlier stated, "matching `rflag`'s own
choice" — CASA's `flagdata` `correlation` parameter, which controls
what `rflag`/`tfcrop`/`clip` actually operate on, defaults to
`ABS_ALL` (amplitude); real/imaginary is an option a user can select
(`REAL_ALL`/`IMAG_ALL`), not `rflag`'s built-in default (confirmed
directly against CASAdocs — see
`visplot-rflag-colorization-reference.md` §1's updated citation). The
Gaussianity argument for preferring real/imag stands on its own
statistical merit regardless; the doc just shouldn't credit it to
`rflag`'s own default behavior.

**Combination: a single joint, rotation-invariant statistic — not two
independent per-part z-scores.** Because real and imaginary noise are
i.i.d. with *equal* variance (the same physical fact that justifies
preferring them over amplitude), the correct generalization of a
robust z-score to two dimensions is a joint radial distance, not two
separate 1D statistics combined afterward — independent real/imag
z-scores would implicitly assume an anisotropic noise source that
doesn't match the physics, and would make the same real anomaly score
differently depending on its arbitrary phase/calibration convention.
This is the same "robust distance, then a modified z-score on the
distance" pattern already standard for multivariate outlier detection
generally, specialized to the isotropic case:

```
dr = real − median(real | reference population)
di = imag − median(imag | reference population)
r  = sqrt(dr² + di²)                      # radial deviation, one number per sample
scale = median(r | reference population)  # robust "typical radius"
score = r / scale × sqrt(2 × ln(2))       # ≈ r / scale × 1.1774
```

`score` is always ≥ 0 by construction (a radius), which also settles
§7.10's magnitude-vs-signed question below in the same step — there is
no separate sign to decide what to do with.

**The reference population excludes already-flagged data — confirmed
2026-09 (§7.10), firm.** A point flagged by a prior pass does not
contribute to `median(real | ...)`, `median(imag | ...)`, or
`median(r | ...)` above. It is still displayed and still scored (still
gets a `dr`/`di`/`r`/`score` of its own) — it just isn't counted toward
what "typical" means for everything else. Applies identically to
whichever reference population is in play (§7.5's windowed Slice 1+2
population, or a future Slice 3 global one).

**A calibration detail flagged for implementation, not fully resolved
here:** the standard modified z-score's `0.6745` constant calibrates
MAD to Gaussian σ for a symmetric 1D distribution. The radius of an
isotropic 2D Gaussian deviation instead follows a Rayleigh
distribution, whose median relates to its scale parameter as
`median = σ√(2 ln 2)` — hence `√(2 ln 2) ≈ 1.1774` replacing `0.6745`
above (a standard Rayleigh-distribution property, derived here, worth
confirming against a quick simulation during implementation rather
than taken purely on this derivation). The outlier *threshold* itself
(`rflag`'s `timedevscale`/`freqdevscale`, Iglewicz & Hoaglin's
recommended 3.5 for the 1D case) would need its own recalibration for
the same reason — treated as a policy choice either way, same as
`rflag`'s own user-adjustable thresholds, not something to lock down
in this document.

The statistics *basis* (real/imag, combined as above) and the
*displayed* axis (amplitude, phase, whatever the layer currently
plots) remain independent decisions, as originally proposed — this
computes on real/imag regardless of what's shown.

### 7.5 Cost tiers — firm, carried from discussion; slice mapping added 2026-09

Two genuinely different costs, matching the "sometimes I'll pay for
extended computation" framing this started from:

- **Cheap (local/windowed, within a partition, or a reduction over
  whatever selection/iteration is already loaded):** close to free
  relative to the per-partition compute already happening — usable
  without an explicit opt-in. Covers both the per-baseline score
  (§7.3's first row) **and** per-antenna aggregation of that score
  *within the current view* (§7.3's clarifying note) — these are
  **Slices 1 and 2**, approved for implementation (§7.10).
- **Expensive (global reference — a population larger than what's on
  screen, e.g. an antenna's typical behavior across the whole selected
  observation):** a genuine two-pass computation — reduce across every
  selected partition for the reference statistic, then score every row
  against it. This is the tier that needs the explicit gate, and is
  **Slice 3**, deferred pending user validation of Slices 1+2 (§7.10)
  rather than built now. Within it, mean/variance-based scores reduce
  cheaply and distribute naturally (associative, tree-reduction, scales
  to a cluster with no drama); median/MAD-based scores (the more
  robust, `rflag`-faithful choice) do not — exact computation needs a
  full sort/selection at scale, so distributed systems default to
  approximate quantile sketches (t-digest) instead. See
  `visplot-rflag-colorization-reference.md` §3 for the full technical
  treatment, and `visplot-statistics-dataflow-notes.md` for the
  related (but separate — see below) question of whether any of this
  can ride "for free" on the existing data load.

**Architectural note for Slices 1+2, adopted specifically to keep
Slice 3 a later addition rather than a rewrite (2026-09, see §7.10):**
the scoring function backing Slices 1+2 must take its reference
population (whatever the median/MAD — or mean/variance — is computed
against) as an explicit parameter, not assume it is always "whatever
is currently loaded." Slices 1+2 will always call it that way in
practice, but the function itself shouldn't know that. This is a
zero-cost discipline now — Slice 1's own reference population already
has to be *some* explicit array — and it's what makes Slice 3, if and
when it's taken up, a matter of computing a different (possibly
cluster-computed) reference and passing it into the *same* scoring
function, rather than a second, parallel implementation.

**Explicitly out of scope for Part 6 itself:** the "harvest statistics
during loading" idea from `visplot-statistics-dataflow-notes.md`. That
document is background rationale for whichever future backend pass
implements the expensive tier — not an instruction folded into Part 6
now. The conclusion already reached: any harvesting should ride behind
the same opt-in gate as the feature consuming it, never computed
speculatively.

### 7.6 Raster and scatter integration

**Mechanism: a new `Axis` member, `Axis.Z_SCORE`** (confirmed 2026-09,
see §7.10 — chosen over the `DEVIATION` placeholder as the name an
astronomer user is most likely to already have intuition for) under
`AxisType.DERIVED`, alongside `AMPLITUDE`/
`PHASE`/`REAL`/`IMAGINARY`. This is the single most leveraged decision
in this section: `AxisType.DERIVED` axes are already usable everywhere
those are — raster Y, raster X, raster Quantity, scatter X, scatter Y —
with no bespoke UI. Framed this way, "plot the deviation score
directly" (e.g. a waterfall with Quantity=Z-Score instead of
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
  swap the coloring Quantity from Amplitude to Z-Score in that exact
  configuration and the deviant-antenna pattern that currently requires
  eyeballing several iterations becomes visually immediate.

**Raster+scatter synergy, verified against the source (2026-09) — this
is where visplot's dual-panel design gives Part 6 something PlotMS has
no equivalent of, per your request to factor this in.** visplot already
links its raster and scatter panels in two concrete ways, neither built
for this feature but both directly useful to it:

- **Linked cursor.** `cursor_source` (`visibility_plotter.py`) is one
  shared `ColumnDataSource` passed to every raster and scatter panel
  instance in both slots; hovering any one of them updates it, and
  every other panel reacts. A Z-Score-colored raster's anomalous cell
  and its exact corresponding scatter point are already linked this
  way — "which raw sample is that anomaly, exactly" is answered by
  hovering, across panels, with no new wiring.
- **Linked x-range.** Confirmed directly in `_build_plot_area`:
  `self._scatter.figure.x_range = self._raster.figure.x_range`
  whenever the two panels share an X axis — true today for `vplot`
  and `waterfall`, both of which give raster and scatter the same
  Time axis. Panning or zooming one panel already pans/zooms the
  other. For Slices 1+2, whose reference population is "whatever's
  currently in view" (§7.5), two Z-Score-colored panels on a linked
  axis are automatically scoring against the *same* window — a real
  win for the trust concerns in the addition above (the same N, not
  two different ones the astronomer has to reconcile), not something
  to build separately.

**The recommended realization of this:** the workflow this feature
targets is naturally two-step — a Z-Score waterfall (raster) answers
"when, or at what channel, does something look wrong"; a scatter
colored by (or colorized by baseline, with Z-Score available via the
linked cursor), iterated by antenna, answers "which baseline or
antenna is responsible." Rather than a single-panel default, the new
preset this document already proposes (§7.7) should set up *both*
panels at once, on a linked axis, the same way `vplot`/`waterfall`
already do — turning "spot it, then confirm what it is" into one
synchronized view rather than two the astronomer has to configure and
keep in sync by hand. This dual-panel pairing is also where a
demonstration is most different from PlotMS, which has no equivalent.

**One backend implication worth recording now, extending §7.5's
swappable-reference-population note:** if both panels' queries resolve
to the same selection and window (likely, given the linking above),
computing the reference statistic (§7.4) once and reusing it for both
panels' scoring — rather than recomputing it twice — keeps the two
panels' displayed scores for the same underlying point identical, not
merely similar, as well as avoiding redundant work. Where exactly such
a cache should live (the existing Part 6/6b frame-cache infrastructure
is the obvious candidate) is an implementation detail for whoever picks
up Slice 1's backend work, not a decision needed here.

**Resolved 2026-09: "color an existing amplitude/phase layer *by* the
score" is a third `coloring` mode, not a bespoke field.** Originally
described here as a standalone "color source column" capability
needing its own decoupling mechanism; reframed, following the same
shape already proven by categorical colorize-by-axis, as a third value
of `ScatterLayerSpec.coloring` — `"continuous"` (today's `mean(y)`),
`"categorical"` (colorize-by-axis), and now **`"statistical"`**
(name proposed, not final) for Z-Score-sourced coloring. This is
exactly the extensible mode value §7.7's touchpoint asked Part 3 to
leave room for, now with a concrete third case. Consequences:

- The rendering pipeline gets a third branch alongside the existing
  continuous/categorical split (`render_layer` and, per the two-level
  rendering work, `build_layer_reference`/`resample_layer_reference`),
  computing the joint Z-Score (§7.4) against the plotted layer's own
  `df` rather than aggregating the plotted Y column — the "aggregation
  source decoupled from the plotted Y column" idea from the original
  framing, just slotted into the mode switch rather than a separate
  field.
- The gear-tab UI gets a third `mode_group` option, following
  `colorize_controls()`'s own established shape exactly: a
  mode-specific sub-panel (display style — see below — and whatever
  else this mode needs), staged the same way the categorical
  checklist already is.
- **Slice 2 needs no coloring work of its own.** Because this is a
  layer property, not a workflow-specific one, the iterate-by-antenna
  view (§7.3 row 2) simply shows the *same* `"statistical"`-mode
  per-baseline coloring already in place for Slice 1, narrowed by
  whatever selection the antenna iteration already applies — the same
  way colorize-by-axis today doesn't care whether iteration is active.
  This closes the open question from the previous revision (§7.9, now
  resolved below): no per-antenna color to design, only the
  already-approved per-antenna summary *readout* (§7.6's trust
  additions), which can show several statistics side by side rather
  than needing one aggregation formula to pick a color with.

**Resolved 2026-09: a threshold/highlight display, as a switchable
option, not a replacement for the continuous gradient.** Implementable
cheaply and with confidence, not just in principle — this reuses the
existing scaling dispatch (`"linear"`/`"log"`/`"eq_hist"`/etc., the
same mechanism the two-level rendering work's `resample_layer_reference`
already drives) with one more option, e.g. `"threshold"`: map the score
to a binary indicator (over the cutoff or not, reusing the existing
`scaling_vmin` field as the cutoff — no new field needed) and shade
with a two-color palette instead of a gradient, rather than any new
rendering architecture. Because it rides the same scaling dispatch,
it's available wherever continuous scaling already is — both for
Z-Score plotted directly as an axis/Quantity (ordinary continuous
coloring, `scaling="threshold"`) and as the `"statistical"` coloring
mode's own display-style choice (gradient vs. threshold), the same
switch serving both integration paths from earlier in this section.

**Scatter and raster are complementary here, not redundant:** raster
necessarily bins into pixels before showing anything; scatter can (at
lower zoom/sample counts) show the true per-sample score before any
spatial binning smooths it out. Both views are worth having rather than
picking one.

**Two additions to what gets displayed, approved 2026-09 specifically
to earn user trust (§7.10) — users are expected to be, reasonably,
suspicious of an automated score, so color alone is not enough:**

- **Reference-population size, visible, with a minimum-N floor.**
  Slices 1+2 score against whatever's currently loaded/windowed
  (§7.5), so the same point's score can shift as the astronomer pans
  or zooms — an honest property of "deviant from what," not a bug, but
  one that will read as broken if the astronomer can't see why. The
  sample count the reference was computed from must be visible
  somewhere (colorbar tooltip or legend), and below some minimum N the
  display should say so explicitly (e.g. grey out, "not enough samples
  in view") rather than show a median/MAD-based score that's actually
  too noisy to mean anything at that N.
- **A quantitative readout alongside Slice 2's per-antenna color, not
  color alone.** Something like sample count, median/typical score, and
  fraction over whatever threshold is in use, per antenna, in the
  existing legend/info-panel area. Gives the astronomer a number to
  note down (or put in a reduction log) rather than asking them to take
  a color on faith — cheap to add on top of infrastructure that's
  already there.

### 7.7 Touchpoints for Parts 3 and 4 — cheap now, expensive to retrofit later

Two things worth building into Parts 3/4 *now*, even though Part 6
hasn't started, because they're nearly free as part of work already
planned and materially more expensive to unwind after the fact:

- **Part 3:** whatever field distinguishes today's continuous coloring
  from the new categorical colorize-by-axis coloring should be an
  extensible mode value (e.g. a string/enum: `"continuous"` /
  `"categorical"`), not a boolean. Part 6 will want a third mode
  (`"statistical"` proposed, settled 2026-09 — see §7.6) for the
  color-source-column capability in §7.6 — cheap to leave room for now,
  a real (if small) rework to retrofit onto a two-state boolean later.
- **Part 4:** the mutual-exclusivity logic that hides a layer's
  continuous scaling controls when colorize-by-axis is enabled should
  be written as an N-way mode switch, not an if/else pair, for the
  same reason.

**Good news reducing everything else:** the named-preset mechanism
(`_PRESETS`, `_preset_js`) and the per-slot gear-tab sidebar config
(P-5a, already shipped — see the naming note at the top of this
document; unrelated to this document's Part 6 despite the similar
label) are both already fully generic over axis/
Quantity choices — Part 6 needs **no new preset or sidebar
infrastructure**, just new values flowing through what's there
(a new `Axis` member, and eventually new gear-tab controls for window
size/threshold, using the exact pattern existing controls already use).
Concretely, verified against the source (2026-09): `_RASTER_QTY_OPTIONS`
and `_SCATTER_Y_OPTIONS` in `visibility_plotter.py` are plain
`(name, label)` tuples — adding `Axis.Z_SCORE` to each is a one-line
change per list, no new mechanism. The "color an existing layer by the
score" capability's controls belong in the same per-layer gear-tab
panel `colorize_controls()` already builds for colorize-by-axis —
concretely, extending its existing `mode_group` (today a two-way
`RadioButtonGroup`: continuous vs. categorical) to a third,
Z-Score-sourced mode, with a parallel sub-panel the same way the
categorical mode's checklist is today.

### 7.8 Non-goals for Part 6 — firm

- No flag-writing of any kind — visualization only (§7.2).
- No bit-exact reproduction of CASA's `rflag`/`tfcrop` procedural
  algorithm — statistical philosophy, not the algorithm itself (§7.2).
- No automated qualitative assessment ("this antenna is bad") — scores
  and statistics only, the astronomer judges (§7.2, confirmed 2026-09).
- No dependency on `casatools`/`python-casacore` at runtime — confirmed
  2026-09, §7.2.
- No default-on global/expensive statistics tier — opt-in only (§7.5).
- Slice 3 (the global/expensive tier) itself is out of scope for this
  pass — deferred pending user validation of Slices 1+2 (§7.10).
- No speculative "harvest during load" implementation — background
  notes only, gated behind a real consumer (§7.5,
  `visplot-statistics-dataflow-notes.md`).
- No new preset or sidebar mechanism — reuse what Parts 4/5a already
  shipped (§7.7).

### 7.9 Open questions

- [x] ~~Which reference group (§7.3) is the actual first target — or is
      per-baseline-then-per-antenna-aggregation (the recommended order)
      confirmed?~~ Confirmed 2026-09 (§7.3, §7.10): per-baseline first
      (Slice 1), per-antenna-within-current-view second (Slice 2).
- [x] ~~Naming for the new `Axis` member (`DEVIATION` is a
      placeholder).~~ Resolved 2026-09: `Axis.Z_SCORE` (§7.6, §7.10).
- [x] ~~Is real/imaginary the right statistics basis by default, or
      should it be configurable per layer (§7.4)?~~ Confirmed 2026-09,
      verified against the standard interferometric noise model
      (Thompson, Moran & Swenson): real/imaginary, not configurable per
      layer for this pass, combined as a single joint radial statistic
      rather than two independent per-part z-scores (§7.4, §7.10) —
      correcting this document's earlier claim that real/imaginary
      "matches `rflag`'s own choice" (CASA's `flagdata` actually
      defaults to amplitude; real/imaginary is available, not default —
      §7.4).
- [x] ~~Worth pursuing real `flagdata(action='calculate')` as an MSv2-side
      validation oracle (`visplot-rflag-colorization-reference.md` §2)?~~
      Resolved 2026-09: `flagdata` does not operate on MSv4 Processing
      Sets (confirmed directly, not inferred). Part 6 builds its own
      computation on both backends; if the MSv2-side comparison is
      pursued at all, it stays a one-off offline script, never a
      runtime dependency (§7.2, §7.10).
- [ ] Will Part 6, once Slices 1+2 are validated with users, need its
      own design→backend→render→UI breakdown the way the original
      feature used Parts 2–4 for Slice 3? (Current guess: yes, once
      Slice 3 is actually taken up.)
- [x] ~~A threshold/highlight display mode (everything under a threshold
      rendered neutrally, everything over it in one unmissable color),
      as a first-class alternative to a continuous gradient~~ Resolved
      2026-09: yes, as a switchable option (not a replacement) — a new
      `"threshold"` scaling function alongside `linear`/`log`/`eq_hist`,
      reusing the existing `scaling_vmin` field as the cutoff. See §7.6.
- [x] ~~Whether already-flagged data should be excluded from the
      reference-population computation~~ Confirmed 2026-09: excluded.
      Median/MAD tolerate contamination well (up to ~50% breakdown), so
      this was never a fragile question, but the policy is now explicit
      rather than implicit (§7.10).
- [x] ~~Per-antenna aggregation (§7.3 row 2, Slice 2): ... does Slice 2
      need a distinct per-antenna color at all?~~ Resolved 2026-09: no.
      "Color by Z-Score" is a third `ScatterLayerSpec.coloring` mode
      (`"statistical"`, name proposed) parallel to today's
      `"continuous"`/`"categorical"`, following `colorize_controls()`'s
      own established shape — a layer property, not a workflow-specific
      one. Slice 2 therefore reuses Slice 1's per-baseline coloring
      as-is under whatever selection antenna iteration already applies;
      the only new Slice 2 work is the summary readout (§7.6/§7.10),
      which shows several statistics side by side rather than needing a
      single aggregation formula. See §7.6.

### 7.10 Slice approval / scope record — 2026-09

Recorded here as the durable decision trail, since §7.9's resolutions
above all trace back to this one round of scoping:

- **Approved for implementation now: Slices 1 and 2** — the per-baseline
  windowed score (§7.3 row 1) and per-antenna aggregation of it within
  whatever's already loaded/iterated (§7.3 row 2, the clarifying note).
  Both are cost-tier "cheap" (§7.5); neither needs the opt-in gate.
- **Deferred: Slice 3** — the global-reference, expensive, cluster-
  relevant tier (§7.5). Not rejected, deliberately not built yet:
  Slices 1+2 are to be validated and tested with real users first: the
  reference-group breakdown, the metric, and the per-antenna
  presentation could all still change in response to that feedback, and
  building Slice 3 before that would risk building the wrong version of
  it.
- **The one thing done now specifically to keep Slice 3 cheap later:**
  the §7.5 architectural note (explicit, swappable reference-population
  parameter on the scoring function) — a zero-cost discipline for
  Slices 1+2 that turns Slice 3 into "compute a different reference and
  pass it in" rather than a second implementation, if and when it's
  taken up.
- **Metric:** basis and combination rule both verified against the
  literature, not just carried forward as originally proposed — see
  §7.4 for the full derivation and citations. Real/imaginary confirmed
  as the right basis (matches the standard circularly-symmetric-complex-
  Gaussian interferometric noise model, Thompson/Moran/Swenson), but
  combined as one joint radial statistic rather than two independent
  per-part z-scores, and reported as a magnitude (always ≥ 0) rather
  than a signed value — the latter also matches Iglewicz & Hoaglin's
  own convention for the modified z-score they defined (thresholded on
  absolute value). One correction made to this document's own earlier
  framing in the same pass: real/imaginary is a well-motivated choice,
  but is not CASA's own default for `rflag` (that's amplitude,
  `ABS_ALL`) — see §7.4.
- **Naming:** `Axis.Z_SCORE`, chosen for being the term most likely to
  already mean something to an astronomer user, over the `DEVIATION`
  placeholder.
- **Per-antenna presentation:** statistics only, never a qualitative
  verdict — see §7.2's firm statement and §7.3's table wording.
- **User trust — two additions approved specifically because users are
  expected to be (rightly) suspicious of an automated score:**
  - The reference population's size must be visible to the user
    (colorbar/legend), and there must be a minimum-N floor below which
    the score is not shown as if it were reliable (grey out / an
    explicit "not enough samples in view" rather than a noisy median/
    MAD presented with false confidence) — see §7.6.
  - Slice 2's per-antenna view gets a quantitative readout (sample
    count, median/typical score, fraction over whatever threshold is in
    use) alongside the color, in the existing legend/info-panel area —
    not color alone. Astronomers get a number they can note down, not
    just a visual impression to take on faith — see §7.6.
- **Dependencies:** confirmed independent of `casatools`/
  `python-casacore` at runtime; `arcae` remains available if needed.
  `flagdata` confirmed not to support MSv4, closing that open question
  in the direction §3 of `visplot-rflag-colorization-reference.md`
  already anticipated.
- **Raster+scatter synergy:** confirmed and factored in, per explicit
  request — visplot's existing linked cursor and linked x-range
  (`cursor_source`, `_build_plot_area`'s x-range sharing — both
  verified directly against `visibility_plotter.py`, neither built for
  this feature) already give a Z-Score-colored raster+scatter pair most
  of the "spot it, then confirm what it is" workflow for free. The new
  preset (§7.6/§7.7) should set up both panels at once on a linked
  axis, not just one panel — see §7.6 for the full reasoning and the
  backend-caching implication this adds to §7.5's architectural note.
- **Reference population excludes already-flagged data.** Confirmed
  2026-09: a point already flagged by a prior pass does not contribute
  to the median/MAD (or joint-radial equivalent, §7.4) computed for
  Slices 1+2. Still shown in the display — just not counted toward
  what "typical" means. Robust statistics tolerate a fair amount of
  contamination on their own (median/MAD's ~50% breakdown point), so
  this was never a fragile question, but the policy is now explicit
  rather than left for an implementer to guess.
- **"Color by Z-Score" is a third `coloring` mode, `"statistical"`
  (proposed), not a bespoke field.** Confirmed 2026-09 — see §7.6 for
  the full reasoning. This dissolves the per-antenna color question
  below: Slice 2 needs no coloring work of its own, only the already-
  approved summary readout, since coloring is a layer property that
  Slice 1's mode already provides regardless of what selection or
  iteration is active.
- **A threshold/highlight display, switchable, not exclusive.**
  Confirmed 2026-09 — a new `"threshold"` scaling function, reusing the
  existing scaling dispatch and the `scaling_vmin` field, available
  both for Z-Score plotted directly and as the `"statistical"` mode's
  own display-style choice. See §7.6.
- **After this round, Slices 1+2 have no remaining open pre-
  implementation questions** — §7.9's one remaining item (Slice 3's own
  design→backend→render→UI breakdown) is explicitly deferred until
  Slice 3 is taken up, not a blocker for starting Slices 1+2 now.

---

## 8. Open questions log

Quick-scan list of everything not yet confirmed. Move resolved items to §4
with the decision recorded; add new ones as they surface.

- [ ] Is Baseline really out of scope, or is there a workflow where it's
      still wanted despite high cardinality (§4.1)?
- [x] ~~Is 20 categories the right cap, or should it vary by axis (§4.2)?~~
      Resolved in Part 3: the cap stays ~20 (a legibility ceiling, not an
      antenna-count-dependent one), but exceeding it now auto-bins rather
      than refuses (§4.2) — so no axis needs a higher or separate cap; a
      263-antenna ngVLA selection costs the same to render as a 26-antenna
      one.
- [ ] Per-layer colorization confirmed as the desired UX, not plot-wide (§4.4)?
- [x] ~~Any axis in §4.1 that Part 2 finds materially harder to plumb than
      the others — should it be dropped or deferred to a v2?~~ Yes:
      Observation and Intent, both dropped (§4.1). Correlation plumbed but
      found degenerate for a single-polarization layer (§4.1) — worth a
      decision on whether it's worth exposing in the Part 4 UI at all.
- [x] ~~New: is a single-dtype (all-`str` or all-`int`) `spw` column worth
      normalizing to at the Part 3 boundary, given `_partition_spw_ident`
      can return either depending on what the store provides?~~ Resolved
      in Part 3: `_scatter_render._resolve_categories` string-normalizes
      before comparison, so the same real SPW merges into one category
      regardless of which type a given partition reported it as. No
      backend-side change needed — the normalization lives entirely at the
      rendering boundary.

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
- **v3** — Part 6 scoped (design only): statistical/`rflag`-style
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
- **v5** — Part 3 complete: categorical aggregation, new `ScatterLayerSpec`/
  `ScatterLayerRender` fields, and a categorical palette (`_scatter_render.py`,
  `reader.py`, `palettes.py`), verified against real MSv2 and MSv4 data
  (including the MSv4-only OPT-B cross-partition path). Revised once in the
  same session, before any of it reached Part 4: the original §4.2
  "refuse over cap" plan was replaced with automatic contiguous binning
  (`_bin_categories`), and categorical shading switched from Datashader's
  blend to a hand-rolled winner-take-all (`_argmax_shade`), after direct
  measurement showed the original plan would make the feature nearly
  unusable at ngVLA's real antenna count (263) — both on cost grounds (a
  263-category blend at 1920×1080 measured out to ~6.8s and ~2.2GB
  transient, per layer) and, independently, on correctness grounds (a
  blended pixel color generally matches no single legend swatch, which
  defeats the point of a legend). See §4.2 for the full numbers and
  `visplot-colorize-by-axis-handoff-part4.md` for what this means for
  Part 4's legend widget. The `spw` mixed-dtype open question from §8 was
  also resolved in Part 3 (string-normalize at the rendering boundary, no
  backend change needed).
- **v4** — Part 2 performance follow-up (§3): the original scan/antenna
  broadcast measurably regressed a pre-existing timing test; root-caused
  and fixed in two stages (a cached per-MS/per-partition lookup mechanism,
  new shared infrastructure on `XArrayReader`; then a pandas 3.0
  string-dtype conversion cost found by direct benchmarking, the larger of
  the two). Recovered most, not all, of the regression — accepted as a
  real, understood residual cost rather than chased further. This is the
  note several code comments already pointed to before it existed here;
  written now to close that gap.
- **v6** — Housekeeping + Part 6 scope decisions, both 2026-09. (1)
  Renamed the statistical/rflag-style colorization feature from "Part 5"
  to "Part 6" throughout this document and its two companions: the
  numbers "Part 5"/"Part 5a" had, by this point, independently been used
  in real shipped code for the unrelated category-exclusion/priority-
  selection feature (see the naming note at the top of this document) —
  a rename only, no scope change. Also corrected this document's own
  stale status line (Part 4 was in fact complete, verified against the
  source, not "not started" as previously stated) and fixed an
  incorrect section citation in `visplot-rflag-colorization-reference.md`
  (§9 → §7). (2) Part 6 scoping decisions recorded in full in the new
  §7.10: Slices 1+2 (per-baseline score, per-antenna aggregation within
  the current view) approved for implementation; Slice 3 (global
  reference, cluster-relevant) deferred pending user validation of
  Slices 1+2; metric confirmed as originally proposed (§7.4); axis named
  `Axis.Z_SCORE`; per-antenna aggregation confirmed to present statistics
  only, never a qualitative verdict (§7.2, §7.3); confirmed independent
  of `casatools`/`python-casacore` at runtime, `arcae` permitted if
  needed (§7.2); `flagdata`-as-validation-oracle question resolved —
  does not support MSv4, so Part 6 builds its own computation on both
  backends. A new architectural note added to §7.5: the Slice 1+2
  scoring function takes its reference population as an explicit
  parameter rather than assuming "whatever's currently loaded," a
  zero-cost discipline now that keeps a later Slice 3 a matter of
  passing in a different reference rather than a second implementation.
- **v7** — Metric verified against the literature (2026-09), plus
  raster+scatter synergy factored in on request. The real/imaginary
  basis (§7.4) is confirmed correct, but its combination rule is
  revised: a single joint, rotation-invariant radial statistic, not two
  independent per-part z-scores, justified by the standard circularly-
  symmetric-complex-Gaussian interferometric noise model (Thompson,
  Moran & Swenson) rather than by intuition alone. Displaying the score
  as a magnitude (never signed) is confirmed as the literal convention
  for the modified z-score itself (Iglewicz & Hoaglin 1993), not a UX
  preference. One correction made to this document's own prior claim:
  real/imaginary does not "match `rflag`'s own choice" — CASA's
  `flagdata` correlation parameter defaults to amplitude (`ABS_ALL`)
  for rflag/tfcrop/clip; real/imaginary is a well-motivated option, not
  the default. Two trust-focused UI additions approved (§7.6, §7.10):
  visible reference-population size with a minimum-N floor, and a
  quantitative per-antenna readout alongside Slice 2's coloring, not
  color alone. Two more (a threshold/highlight display mode; excluding
  already-flagged data from the reference) were proposed but not yet
  decided — left open in §7.9 rather than assumed. Raster+scatter
  synergy (§7.6, §7.10): visplot's existing linked cursor and linked
  x-range (verified directly against `visibility_plotter.py`) already
  give a Z-Score-colored raster+scatter pair most of the "spot it, then
  confirm what it is" workflow for free; the new preset should set up
  both panels at once on a linked axis, and a shared reference-statistic
  cache across both panels' queries was added as a backend implication
  of §7.5's swappable-reference-population note.
- **v8** — Already-flagged data confirmed excluded from the reference
  population (§7.4, §7.10) — still displayed and scored, just not
  counted toward what "typical" means. One new open item surfaced while
  closing that one out: Slice 2's per-antenna aggregation (§7.3 row 2)
  has a "present statistics, not a verdict" principle but no decided
  formula, and it's not yet settled whether Slice 2 needs a distinct
  per-antenna *color* at all, versus keeping Slice 1's per-baseline
  color and adding only a summary readout — see §7.9.
- **v9** — The two items v8 left open are now resolved, and the second
  one dissolved the first rather than just answering it. "Color by
  Z-Score" is confirmed as a third `ScatterLayerSpec.coloring` mode
  (`"statistical"`, proposed name), parallel to today's `"continuous"`/
  `"categorical"` and built on `colorize_controls()`'s own established
  shape — which means Slice 2 needs no per-antenna coloring work at
  all, only the already-approved summary readout, since coloring is a
  layer property indifferent to whatever selection or iteration is
  active. A threshold/highlight display is confirmed as a new
  `"threshold"` scaling function alongside `linear`/`log`/`eq_hist` —
  switchable, not exclusive, reusing the existing scaling dispatch and
  `scaling_vmin` field, and available both for Z-Score plotted directly
  and as the new mode's own display-style choice. §7.6 rewritten
  accordingly; the "color source column" framing it previously used is
  retired in favor of the mode-based description. Slices 1+2 now have
  no remaining open pre-implementation questions (§7.10).
