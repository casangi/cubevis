# FlagDB v2: design and specification

Status: design only; nothing in this document is implemented. It records the
decisions reached in discussion (2026-09), what the code does today, and a plan
whose first steps are pure Python with no GUI. Written so it can be picked up in a
fresh chat.

Suggested first message for a new chat:
> Starting FlagDB v2. Read FLAGDB_DESIGN.md. Begin with milestone F1 in section 12.

Related: `HRS_VISPLOT_PLAN.md` (why flagging matters; milestones M4/M5),
`VIEW_STATE_DESIGN.md` (pending flags become a `data`-scope state unit),
`ZSCORE_OPTIMIZATION_HANDOFF.md` (the Z-Score filter and its reference cache).

---

## 1. Scope

**In:** how flags are *created, held, previewed, displayed, undone and exported*
while pending: the delta model, ordering semantics, filters, an optional preview
step, display states, export to text/JSON. Flagging is the primary use of visplot,
so getting this right matters more than any single view.

**Out (deliberately, for now):** what finally happens to committed flags. Write-back
to the MS/zarr, `flagdata` execution and flag versions sit behind the existing
`ReductionContext.commit_flags()` seam. Everything here is designed so that either
outcome (write flags, or emit CASA flag commands) is a translation of the same
pending state.

## 2. What exists today (findings from reading the code)

- `flag_db.py::FlagDB`: an ordered list of `FlagDelta`s, `append/pop` (undo only),
  `clear`, `commit(context)` (drains via `context.commit_flags(list)`),
  `overlay_deltas()` (copy of the list), `peek`. In memory only; never writes until
  commit. The module docstring mentions a `flag_selection()` that does not exist;
  the docstring should be fixed.
- `reduction_context.py::FlagDelta` (frozen): `flag: bool`; optional
  `time_range` (MJD s), `freq_range` (Hz), `channel_range` (ints), `baseline_ids`
  (name pairs), `antenna_names`, `scan_names`, `field_names`, `correlation`;
  extend flags (`extend_corr/chan/spw/scan`); `source`, `comment`. **No SPW field.**
  `FlagSummary`: `n_flagged`, `fraction_flagged`, `by_spw`, `by_antenna`, `message`.
- `VisibilityPlotter` box-select handler: builds one delta from a drawn box, appends
  it, calls `_render_flag_overlay()` and reports "preview only, stored, not yet
  committed". Pending flags are cleared on data reload.
- **The overlay is a stub** (`_render_flag_overlay` is a TODO).
- **The handler keeps only the x-extent.** For a raster box only `time_range` or
  `freq_range` (from the x axis) goes into the delta; the y-extent (the baseline
  range in a Baseline x Time raster) appears only in the comment. For a scatter box
  the amplitude extent is dropped too. As built, a box means "these times, all
  baselines / all samples".
- When the raster x axis is Channel, the handler appears to put x values (channel
  indices) into `freq_range` (documented as Hz) **(verify whether a conversion happens
  elsewhere)**.
- Flagged samples are excluded from rendered quantities (`q.where(~flag)`), so
  already-flagged data is currently invisible and cannot be seen or unflagged from a
  view. The `Flag` raster quantity shows a flagged *fraction*.
- `FlagTool` (client) drags a box and fires a message; reportedly flags only at 1:1
  pixel resolution **(verify)**. `enable_flagging` turns the tools off.
- `commit_flags` is an interface; the null context raises. Which contexts implement it
  for MSv2/MSv4 data is **unverified**.

## 3. Core model

One pipeline for every way of making flags:

```
selection -> filter -> PROPOSAL -> reviewer -> accept -> Flag DB delta(s)
```

- **Selection**: what the user drew or chose (a box in a raster or scatter, an
  antenna, a time range), expressed in *data space*, never pixels.
- **Filter**: a function of the selected samples returning which ones to keep. The
  default filter is "keep everything". So *immediate AIPS-style flagging* and
  *filtered flagging* are the same path; they differ only in the filter.
- **Proposal**: the frozen result (selection + filter + parameters + resulting
  samples or region + provenance chain). Ephemeral, editable, cancellable; it is
  **not** in the Flag DB.
- **Reviewer**: decides whether the proposal is accepted. Default: auto-accept. With
  the preview option on: a dialog (section 6). This is the single hook for future
  interactive stages.
- **Flag DB**: holds only accepted deltas. Its undo stack therefore stays clean.

Even in immediate mode a proposal object is created and immediately accepted. That
costs almost nothing and is the extension point.

## 4. Delta model and semantics

**Two representations, one interface.**
1. **Region delta**: a coordinate region (time range, baseline set, SPW, channel or
   frequency range, correlations, scan/field, extend options). Cheap; exports as a
   plain `flagdata` selection. Produced by an immediate box with the identity filter.
2. **Sample-set delta**: an explicit set of samples, or a filter reference with a
   *frozen* selection, produced when a filter narrowed the region. Needed because a
   value condition (amplitude range, Z-Score above a cutoff) cannot be expressed as a
   coordinate region.

**Sample identity**: (time, baseline, SPW, channel, correlation). Units: time in MJD
seconds; frequency in Hz; channel as an index **within an SPW** (so an SPW field is
required whenever channels are named); baselines as antenna-name pairs.

**Required changes to `FlagDelta` (v2)**
- Add `spw` (ids/names). Channel ranges are meaningless without it.
- Add a baseline set that a raster y-extent can populate (baseline index range ->
  antenna-name pairs, via the current selection's baseline ordering).
- A value/predicate form: `(quantity, low, high)` and the filter form (section 5).
- An id and creation order (for redo, stepping, audit).
- Keep `source`/`comment`; add a provenance chain (e.g. "box -> Z-Score > 4.9 ->
  grow 2 channels").

**Ordering.** The effective flag state is the *sequential* application of accepted
deltas: a later unflag overrides an earlier flag on the same samples, and vice versa.
A single union-of-selections object cannot represent that, so the "effective mask"
is computed by folding the deltas in order. Define it precisely and test it.

**Selection is frozen at proposal time.** A per-baseline filter's result depends on
what is selected; freezing the selection makes preview and commit agree.

## 5. Filters

A filter chooses points; the action (flag, unflag, highlight) is separate.

**Contract.** Array in, boolean array out: the filter receives the selected samples
as a read-only labeled xarray Dataset (dims: time, baseline, frequency,
polarization; variables: `vis`, existing `flag`, coordinates, helpers such as `amp`
and `phase`) and returns a boolean mask of the same shape. The framework intersects
it with the selection and the unflagged data. A per-point boolean function is
accepted through an "elementwise" wrapper, with a speed warning.

**Declared scope** (data is processed in dask chunks, so a filter says what it needs):
- *local*: elementwise, chunk-safe (amplitude above x);
- *per baseline*: needs a whole-selection statistic first (Z-Score's median). Written
  as `prepare(ds) -> small stats` (cacheable; the shared Z-Score reference cache
  serves this) and `mask(ds, stats)` per chunk;
- *windowed*: needs neighboring time/channels; the framework supplies a halo;
- *global*: sees everything (rare, slow).

**Parameters.** Each filter declares a parameter spec (name, type, default, bounds)
so the GUI can draw a control and a saved view can store values.

**Registry.** `visplot(..., filters={"name": callable_or_spec})`; the GUI selects by
name at filter time. Built-in presets: Z-Score above cutoff (n-aware), amplitude range,
phase and noise-based cuts. User functions are supplied by the Python caller, never
typed into the GUI, and never loaded from saved files.

**Reproducibility.** A filter delta records the filter name, parameters and a hash of
the function's code. No user function has a CASA equivalent; at export it is
materialized to explicit samples (section 9). Saved views/modes store name and
parameters only; a mode naming a filter that is not registered fails cleanly through
its `requires` check (`VIEW_STATE_DESIGN.md` 7A).

**Cells vs samples.** Filters act on samples, not rendered cells. When a raster cell
aggregates samples, "matched" is a display choice (any / all / fraction of samples).

**Caveats to resolve.** With a remote kernel, a user function in the GUI process may
not reach the process holding the data; filters may need kernel-side registration
(check the remote path). With several SPWs of different channel counts, decide
whether a filter is called once per SPW/partition.

## 6. Preview (optional review step)

- **Option:** `preview` (off by default). Off: proposals are auto-accepted and go
  straight into the DB. On: the reviewer shows a dialog and waits for OK or Reject.
- **Why off is safe:** pending flags never touch disk and undo exists, so preview
  matters mainly for large or expensive results and user-supplied filters, and to keep
  accidental bulk proposals out of the DB. Optional guard (open decision): auto-show
  the dialog when a proposal exceeds N percent of the data or uses a user filter.
- **Dialog content:** samples to be added (new vs already flagged), a breakdown by
  baseline/antenna, SPW, polarization and time coverage, the filter and parameters,
  the selection it applied to, the provenance chain.
- **In the plot:** the proposed points are drawn in their own style (dashed orange)
  while the dialog is open.
- **Blocking:** block only *flagging* interactions while it is open; leave pan/zoom so
  the user can inspect (open decision).
- **Mechanics:** Bokeh has no native modal and there is no Bokeh server, so this is
  custom client UI plus a server round trip (the busy overlay is a precedent; not
  verified as reusable). Counts come from the pure evaluator (section 12, F1) on the
  frozen selection; large proposals should report progress and cap detail.
- **Staleness:** flags accepted between creating and accepting a proposal can make
  counts stale; recompute on accept or warn.
- **Reject:** leaves the DB and undo stack exactly as they were.

## 7. Display states and preview semantics

Distinct visual states (colors to be chosen with the palettes work):
1. unflagged (normal);
2. **committed flagged** (from the data; optional "show flagged", off by default;
   required for unflagging and for TVFLG-style editing);
3. **pending flag** (solid red overlay);
4. **pending unflag**;
5. **proposal under review** (dashed orange).

**Overlay vs apply-pending.** Two modes for the effect of pending flags on the
picture: *overlay only* (data unchanged, flagged samples tinted) and *apply pending*
(queries exclude pending-flagged samples, so rasters, colorbars and statistics such
as Z-Score are recomputed as if they were flagged). Apply-pending is what makes the
flag-then-re-look loop useful, but it makes cached frames depend on pending state.
Frame-cache keys must include a pending-state version (the existing
`cache_generation` covers reload only); Z-Score references must be recomputed.

**Decimation.** A rendered cell stands for many samples; the delta is in data space
so it is exact regardless of zoom. What a drawn box covers at a given zoom (whole
cells vs partial), and the pre-commit sample count, come from the evaluator.

## 8. Undo, redo, growth, threading, persistence

- Add **redo** (undo currently discards).
- **Merge/normalize** deltas so many box operations do not make overlay cost grow
  linearly (evaluate once to a mask per view; merge adjacent regions).
- `FlagDB` is not thread-safe; handlers are async. Either serialize behind the
  plotter's render lock or make it internally locked.
- **Pending persistence:** a `data`-scope unit in the view-state registry
  (`data.flags.pending`, order 70) so pending flags survive a restart. Reload
  currently clears them silently; decide whether to warn.

## 9. Export seam (pure functions, no backend needed)

Because region deltas resemble `flagdata` selections, export is a translator that can
be developed and tested with no data:

| Delta field | `flagdata` (list-mode line) |
|---|---|
| flag / unflag | `mode='manual'` / `mode='unflag'` |
| `time_range` (MJD s) | `timerange='YYYY/MM/DD/HH:MM:SS.s~...'` (converted) |
| `baseline_ids` | `antenna='A1&A2;A3&A4'` |
| `antenna_names` | `antenna='A1'` (all baselines with it) |
| `spw` + `channel_range` | `spw='id:c0~c1'` |
| `freq_range` | `spw='...'` via frequency ranges (or converted to channels) |
| `correlation` | `correlation='XX,YY'` |
| `scan_names`, `field_names` | `scan=`, `field=` |
| amplitude range (predicate) | `mode='clip'` with `clipminmax` (only for quantities `clip` supports) |
| extend flags | a following `mode='extend'` line |

Anything without an equivalent (a user filter, a Z-Score predicate) is **materialized**
to explicit samples first (recorded compactly) before export. Also export JSON Lines
with the full provenance chain for audit. Unresolved: whether the materialized form
should be many `manual` lines or a sample list a context writes directly.

## 10. Interactions with other work

- **Z-Score:** the Z-Score cutoff becomes the first built-in filter; its per-baseline
  reference is the "prepare" phase and should come from the shared reference cache
  (optimization handoff, option 4.2). The "candidate flag generator" backlog item is
  this filter.
- **View state:** pending flags are a `data` unit; a mode may include a *filter
  selection* (name and parameters) but never flags themselves.
- **HRS plan:** this is milestone M4 (in-memory slice), with M5 (persistence) left
  behind the export/commit seam.
- **Later interactive stages** (not built; the reviewer and proposal are the hooks):
  extend preview, threshold-from-histogram, candidate review (Prev/Next through
  proposed regions), drill-down (a detail view scoped to the selection, as in AIPS
  TVFLG then SPFLG), confirm-before-flagging plots, calculate-then-apply like rflag.
  Stages share one interface (proposal in, proposal out), so a new flow is a
  composition, not a new code path.

## 11. Testing strategy

- The evaluator is the reference: build a tiny synthetic dataset and compare every
  result against an independent numpy computation.
- Ordering: flag/unflag sequences, overlaps, idempotence, redo/undo round trips.
- Filters: each scope kind; chunk-boundary equivalence (same result at any chunking);
  per-baseline prepare/mask agreement with an in-memory reference; the Z-Score filter
  against `compute_baseline_zscore`.
- Export: golden files for `flagdata` lines and JSONL; round trip where possible.
- Reviewer: auto-accept adds exactly the proposed delta(s); reject changes nothing;
  stale-proposal handling.
- Handler tests must drive the real box-select path (the earlier y-extent loss went
  unnoticed because the delta was never checked against the box).
- Mutation-check new tests; GUI checks for anything client-side.

## 12. Milestones

| # | Milestone | Effort* | Exit criteria |
|---|---|---|---|
| F1 | Semantics + `FlagDelta` v2 + pure evaluator (region and predicate deltas to a boolean mask; ordering; counts) | M | Spec'd behavior passes against the numpy reference |
| F2 | Exporters: `flagdata` lines and JSONL, with materialization | S-M | Golden-file tests |
| F3 | Box handler fixed to carry the full extent (baseline range, values); redo; merge; locking | S-M | Handler tests drive the real path |
| F4 | Filter framework: contract, scopes, parameter specs, `filters=` registry, built-in presets (Z-Score, amplitude range) | M | Chunk-equivalence and Z-Score parity tests |
| F5 | Overlay rendering and display states (committed / pending / unflag), overlay vs apply-pending, cache keys | M-L | Live GUI check |
| F6 | Preview dialog and reviewer, pending persistence as a view-state unit | M | Accept/reject/stale behaviors |

\*S < 1 day, M 1-3 days, L 1-2 weeks; rough.

## 13. Open decisions

1. Filter scope per call: one baseline or SPW at a time, or the whole selection?
2. Preview blocking: block only flagging, or fully modal?
3. Auto-show guard (percent of data, user filters) or strictly manual?
4. Apply-pending vs overlay: default, and is it a per-panel or global switch?
5. Materialized export: `manual` lines, or a sample list written directly?
6. Cell matching rule for rasters (any / all / fraction) and its default.
7. Should reload warn before discarding pending flags?
8. Redo history size and merge policy.
9. Remote-kernel story for user-supplied filters.
10. Colors and styles for the five display states.

## 14. Hazards

- The box handler currently drops the second axis; do not build on its deltas until F3.
- Units: time MJD seconds, frequency Hz, channel index per SPW; check every conversion.
- Flags accumulate in Python; JavaScript only reports the box. Overlays must be
  rendered by the Python pipeline.
- Renders must be serialized with the panel render lock; pending-flag changes should
  not re-render from inside a handler without it.
- The plotter resets `_quantity`/`_x_dim`/`_y_dim` to `None` before `update_axes`; keep
  that in mind for anything that reacts to axis changes (see the Z-Score handoff).
- Real-MS tests cannot run in the sandbox; ask for full-suite runs.
