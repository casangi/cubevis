# visplot colorize-by-axis — Part 5a notes

Increment after `visplot-colorize-by-axis-notes-part4d.md`. Companion to
`visplot-colorize-by-axis-design.md`. Written 2026-09-23.

## 1. What this increment is

A review of the categorical ("colorize by axis") output against CASA6 PlotMS
and against the performance cost of the remote-render architecture, followed
by the first tranche of changes it justified ("step 1"). **Step 1 delivers:**

1. A vectorized categorical path (the render-side cost fix).
2. Fully opaque categorical layers.
3. A user-selectable **draw priority** per layer: `"rarest"` (default) or
   `"majority"`.
4. The live legend labelled by layer whenever the plot has more than one
   layer, plus a one-line caption saying how overlapping categories were
   resolved (also carried into exported PNGs).

**Not in step 1** (still open, see §9): building only the columns a layer
needs, the backend frame cache, Field, Baseline + highlight mode.

## 2. Decisions taken

| Question | Decision | Why |
|---|---|---|
| Draw priority | User-selectable per layer; **rarest-on-top is the default** | Costs nothing extra; answers "what is present?" where majority answers "what dominates?"; the reason to colorize is usually to find the odd one out. |
| Categorical opacity | **Fully opaque**; only the user's layer alpha applies | Density alpha comes from a pixel's *total* count, so it drew a lone sample of a rare category faintest — the opposite of what "rarest" is for. Density is the continuous mode's job. Matches PlotMS. |
| Colorize scope | Stays **per layer**, staged until Plot | Already how the gear tab works (one gear tab per plot, `layer_select` inside it). PlotMS has one `coloraxis` per plot; per-layer is a superset. |
| Stable colors per ID | **Dropped** | Conflicts with dynamic binning (>20 categories). Pinning a color to a value belongs to X-3 (user-chosen colors) and highlight mode. |
| Field + Baseline | **Yes**, sequenced after step 1; Baseline together with highlight mode | See §9. |

## 3. Changes by file

`data/reader.py`
- `CATEGORY_PRIORITIES = ("rarest", "majority")`, `DEFAULT_CATEGORY_PRIORITY`
  (documented at length in the constant's docstring).
- `ScatterLayerSpec.category_priority` (validated by value only; unlike
  `excluded_categories` it is *not* rejected on continuous layers because it
  carries a default in both modes).

`data/_scatter_render.py`
- `_categorize()` → `_Categorization(bucket, categories, members, population,
  skip_reason)`. `pd.factorize` hashes each row once; every later step runs
  over the K *distinct* values. `bucket` is an int32 code per row (-1 = not
  drawn); `population` is rows per category over the **whole selection**
  (never the viewport — that is what keeps "rarest" stable while zooming).
- `_resolve_categories()` kept as a thin wrapper (same return contract).
- `_priority_shade()` replaces `_argmax_shade()`: one color per pixel, alpha
  255, `"majority"` = `argmax` of counts, `"rarest"` = first-hit over the
  category axis reordered rarest-first. Empty pixels remain the all-zero value.
- `_shade_categorical()` builds the categorical column with
  `Categorical.from_codes` (no strings).
- `render_layer()` categorical branch rewired; docstrings updated.

`visibility_scatter.py`
- `ScatterLayer.category_priority`; carried through every reconstruction site
  (`set_alpha`, `update_scaling`, `update_colorize`, `_with_default_cmaps`,
  the j2p handler) and into the backend `ScatterLayerSpec`.
- `update_colorize(..., category_priority=None)`; not reset when switching to
  continuous.
- `_collapse_and_composite`: categorical layers skip the density-based
  `auto_alpha`; `layer_alpha = int(255 * lyr.alpha)`. `set_alpha()` stays a
  free, no-requery operation.
- `_full_legend_html`: layer label whenever `len(self._layers) > 1` (was: more
  than one *categorical* layer — the cause of the unlabelled legend beside a
  labelled colorbar in the original screenshot); per-block caption.
- `colorize_controls`: "Draw priority" `Select`, visible only in categorical
  mode; exposed as `handles["priority_select"]`.
- `_panel_spec`: bands carry `category_priority` (categorical layers only).

`visibility_plotter.py`
- `_make_scatter_layers` carries `category_priority`.
- `doPlot`'s `buildColorizeArray` sends `category_priority`.
- Change detection lifted out of `_handle_plot` into module-level
  `_colorize_key_from_override` / `_colorize_key_from_layer` so it is testable.
  A priority-only change counts as a real change; a continuous layer has no
  priority to compare.

`panel_spec.py` — `CATEGORY_PRIORITY_CAPTIONS`, `ColorBand.category_priority`
(added **last** in the field order), `ColorBand.priority_caption()`.

`png_export.py` — caption entry after each categorical band's swatches;
`_legend_handle_count` mirrors it; `_band_key` appends the priority **only when
a band has one**, so plain-band keys keep their pre-Part-5a shape (an existing
test pins that).

## 4. Measurements

All on `sis14_twhya_calibrated_flagged.ms`, a 4M-sample slice (scans 12+14),
one layer, **one core / 4 GB sandbox** — ratios are meaningful, absolute times
are pessimistic.

Where a render call spends time before this increment (`_query_columns_raw`
is paid on **every** call — plot, pan, zoom, recolor, export):

| Stage | Time |
|---|---|
| `_query_columns_raw`, all columns | 1.7–1.8 s |
| – scan/antenna string columns | 0.5–0.7 s |
| – polarization/spw string columns | 0.1 s |
| – time/baseline/frequency (hover grid inputs) | 0.15 s |
| – x, y only | 0.9 s |
| `render_layer`, continuous | 0.26 s (coarse id grid: 56 ms; 8% of the image payload) |
| `render_layer`, categorical (before) | 2.6–3.1 s |

The coarse hover grid is **not** the cost problem. `render_layer` (whole call
incl. id grid) categorical, before → after: scan 4.28 → 0.29 s, antenna1
3.39 → 0.35 s, antenna2 3.53 → 0.35 s, spw 5.42 → 0.65 s, correlation
3.66 → 0.45 s.

Priority cost (8M rows, 900×600): rarest-on-top shading 20 ms (K=3) / 39 ms
(K=20) vs 49 / 68 ms for the old shading step; the only new work is a
population count (~33 ms at 8M rows, once per selection, from row codes).

## 5. PlotMS ground truth

Obtained by running PlotMS (casaplotms 2.9.1 / casatools 6.7.6, Linux,
headless under Xvfb) on the same MS. **Linux build only; not the macOS GUI.**

- Fixed **10-color palette**; color = `palette[(id + 2) mod 10]`, keyed by the
  value's *ID* (verified on scans, ~35 baselines, all 5 fields, antenna 1).
  Slots: `#0066f0 #a868d8 #202020 #e00066 #e07600 #66d000 #ac6600 #0091a0
  #10e050 #6600e0`. Repeats every 10 IDs.
- Opaque per-point symbols, no density; last-drawn value wins (scan 16 covers
  12 and 14). Adjacent scans 12 (orange) / 14 (brown) are hard to tell apart.
- Legend labels carry ID and name ("Field 5: TW Hya", "Antenna1 5: DA48");
  long legends run off the canvas (baseline).
- Baseline coloring on this MS is a rainbow speckle with a clipped legend —
  confirms the design doc's "known PlotMS limitation" note.

Structural fact worth keeping: in an MS `ANTENNA1 < ANTENNA2` for every row
here (210 baselines, 21 antennas). The highest-numbered antenna never appears
as antenna 1 and the lowest never as antenna 2; for a middle antenna, coloring
by antenna 1 shows only ~49% of its data.

## 6. Verification status

**Verified (sandbox, overlaying these files on the published cubevis 1.0.84):**
- `"majority"` mode is pixel-identical to the previous implementation on 7
  synthetic and 7 real-data cases (colors, categories, members, skip reasons).
- `test_colorize_by_axis_part5a_priority.py`: 72 tests, including an
  independent NumPy reference for both modes, zoom stability, ties, exclusions,
  opacity, widget wiring, legend, export caption and change detection.
  **Mutation-checked**: 7 deliberate breakages of the implementation and 1 of
  the change-detection key were each caught.
- Full run over the render / export / PNG / part5a / `test_visibility_scatter`
  suites: 351 passed, 7 skipped, 11 failed — **the same 11 failures as before
  any change** (the tests call `async` handlers synchronously:
  `TestAlpha` ×2, `TestColormapScaling` ×2, `TestProbeRegion` ×7).
- 10 other test files and 48 script-style checks give identical results on the
  original and modified code.

**Not verified:**
- The dropdown and `doPlot` JS in a real browser.
- MSv4 (6 tests skip: need `sis14_twhya_calibrated_flagged.ps.zarr`).
- `test_msv2_backend.py`, `test_info_block_integration.py` (killed for memory in
  the 4 GB sandbox on both versions).
- Remote execution path end to end.

**Files I needed that are not in the project:** `create_test_msv4.py`,
`cubevis_test_paths.py` (stubbed), and `cubevis/bokeh/tools/_info_tool.py` /
`_flag_tool.py` (borrowed from published cubevis 1.0.84 — if your dev tree
differs, results could too).

Two things that looked like regressions and were not: a first-run timing
failure in `TestTiming` (cold disk cache after a sandbox restart; ~4.6 s on
both versions) and the 11 above (pre-existing).

## 7. Behavior changes to expect

- Categorical layers are opaque, so **stacking order now matters**: a
  categorical layer fully covers what is beneath it where they overlap. The
  existing per-layer alpha/hide controls are the tool; no reorder control.
- In `"rarest"` mode a color means "this category is present in this pixel",
  not "this is the commonest". A lone sample colors a whole pixel, which is the
  point, and may look speckled. Hence the caption and the `"majority"` option.
- The old test `test_alpha_respects_min_alpha_floor_for_any_populated_pixel`
  became `test_categorical_pixels_are_fully_opaque`.

## 8. Design-doc updates to fold in

- Baseline "out of scope?" open question: **answered — no.** PlotMS
  documentation and tutorials use it heavily and the project's own workflow
  table prescribes it; but plain coloring is not useful (§5), see §9.
- §4.3 / alpha: categorical layers are opaque; only `lyr.alpha` applies.
- Legend labelling rule now matches the colorbar's and the PNG export's.
- `category_priority` on `ScatterLayerSpec` / `ScatterLayer` / `ColorBand`.
- Color stability across selections was considered and rejected (conflicts with
  binning).

## 9. Next

1. **Build only the columns a layer needs** (perf item 2): scan/antenna/
   polarization/spw string columns cost ~0.65 s of a 1.7 s raw query and about
   half the frame's memory, and are paid on every plot. Must land in both
   backends (MSv4 has a separate fused path). `polarization` is a degenerate
   axis already.
2. **Backend frame cache** (perf item 3), keyed by axis, layers and selection,
   byte-budgeted LRU, invalidated on flag commit and `close()`, with scan/antenna
   codes derived lazily from cached `time`/`baseline_id`. Estimated pan/zoom
   ~2.0 s → ~0.26 s (continuous). `VisplotRemoteBackend` is constructed once per
   session, so a cache on it survives across calls locally and remotely; the
   wire contract is unchanged.
3. **Field** (small): `field_name` is already a per-`time` coordinate on every
   partition in both backends, the same shape as `scan_name`; add to
   `COLORIZE_AXIS_COLUMNS`, one branch in the checklist enumeration
   (`ScanInfo` carries field names), MSv4 parity, parametrize the scan tests.
4. **Baseline + highlight mode + antenna-based picking**: `baseline_id` is
   already in every frame, so plumbing is cheap; the work is semantics. 210
   baselines against a 20-color cap makes contiguous bins meaningless and a
   210-entry checklist unusable. Highlight mode (colored values, everything else
   gray) and "select baselines by antenna" (either end) answer the "bad
   antenna" workflow directly — something PlotMS cannot do.
5. Undecided/optional: PlotMS-like symbol size (our points are 1 px; PlotMS's
   ~2–3 px), layer-dropdown mode suffix ("XX (by Scan)"), user-chosen color per
   value (plan item X-3).
