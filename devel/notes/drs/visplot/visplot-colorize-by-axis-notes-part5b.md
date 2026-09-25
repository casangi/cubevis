# visplot colorize-by-axis — Part 5b notes

Increment after `visplot-colorize-by-axis-notes-part5a.md`. Written 2026-09-24.
Read 5a first for the PlotMS ground truth, the decisions on draw priority and
opacity, and the measurement methodology.

## 1. What this increment is

The two axes the PlotMS review and the project's own workflow table called for,
plus the piece that makes one of them usable:

1. **Field** as a colorize axis.
2. **Baseline** as a colorize axis.
3. **Highlight mode** (`excluded_display`): unchecked values are either hidden
   (what unchecking always did) or drawn as neutral gray context.
4. **Baseline picking by antenna**, and sensible defaults for high-cardinality
   axes.

Why Baseline needed 3 and 4: PlotMS colored by baseline is a rainbow speckle
with a clipped legend (5a §5), and this MS has 210 baselines against a 20-color
cap. Coloring them all can only be binned into contiguous groups whose labels
mean nothing. What the "bad antenna" workflow needs is to color the few you care
about and still see where everything else lies.

## 2. Decisions taken

| Question | Decision | Why |
|---|---|---|
| Field/Baseline per-row representation | `pandas.Categorical` (small integer codes + one shared category list), **not** per-row strings | scan/antenna string columns cost ~0.17 s per 4M rows *each*, paid on every render. Two more would grow that. Measured cost of these two: **+0.06 s (3%) and +12 MB per 4M rows** (§5). |
| Where Field data comes from | Rides on the existing scan lookup (`_PartitionScanLookup.field_codes`) | Field, like scan, is a per-`time` coordinate, so the *same* `__scan_time_idx` array indexes both. No new lazy column, argument or tuple element in either backend. |
| Field category = name or id | Name | It is what the hover line shows ("Field: 3c279"). Fields sharing a name share a category, which is the right reading of "color by field". |
| Baseline label | `"ant1&ant2"` | The hover line's spelling (`BL: DA42&DA48`). |
| Gray group | An ordinary **last category** with a reserved label (`"Other (not selected)"`), fixed gray, ~47% alpha | Legend, hover-title membership and PNG export then work with zero new plumbing. Only the shading knows it always loses to a real category. |
| Gray vs. highlight priority | A pixel holding *any* highlighted sample shows it, however many gray samples share the pixel, in **both** draw priorities | Gray is context, not a competitor; it cannot be one more channel of the argmax/rarity order. |
| Default for a high-cardinality axis | ~~Nothing highlighted; selecting the axis also switches "Unselected values" to gray~~ **Superseded (§11): all checked, unselected values hidden** | The first draft left a blank canvas on a single layer. |
| Threshold | `HIGH_CARDINALITY_THRESHOLD = 20`, pinned equal to `CATEGORY_CAP` by a test | The GUI's "too many to color one by one" must be the renderer's binning cap. |

## 3. Changes by file

`data/reader.py`
- `COLORIZE_AXIS_COLUMNS`: `Axis.FIELD → "field_name"`, `Axis.BASELINE →
  "baseline_name"`. Order is the picker's order (Scan stays first/default;
  Field after Scan; Baseline after the antenna axes).
- `EXCLUDED_DISPLAYS = ("hide", "gray")`, `DEFAULT_EXCLUDED_DISPLAY`,
  `OTHER_CATEGORY_LABEL/COLOR/ALPHA`, `HIGH_CARDINALITY_THRESHOLD`.
- `ScatterLayerSpec.excluded_display` (validated by value only, like
  `category_priority`).
- `_PartitionScanLookup.field_codes` (optional, defaults `None`).
- `XArrayReader`: `_field_categories()` (MS-wide sorted names, cached),
  `_baseline_table()` (`code_of_bid`, unique labels, cached),
  `_identity_categoricals()` (builds both per-row columns for one partition's
  filtered rows). `_scan_lookup_for_partition` now fills `field_codes`.

`data/msv2_backend.py`, `data/msv4_backend.py`
- All **three** frame-assembly sites (MSv2 `_query_partition_scatter`; MSv4
  `_query_partition_scatter` and the fused `_query_all_partitions_scatter_fused`)
  attach `field_name` / `baseline_name`, reusing the scan index array they
  already computed. The MSv4 edits are textually identical to MSv2's.

`data/_scatter_render.py`
- `_categorize()`: reads a `Categorical` column's codes directly (no hashing,
  no strings) and keeps only categories that have rows, so unused categories
  never reach the legend. New `show_excluded`: unchecked values become a last
  "Other (not selected)" category; it does not count toward the cap; missing
  values are never gray; nothing highlighted is a valid all-gray render in gray
  mode (but still the "all excluded" skip in hide mode).
- `_priority_shade()`: gray is always drawn under any real category.

`visibility_scatter.py`
- `ScatterLayer.excluded_display`, carried through every reconstruction site,
  `update_colorize(..., excluded_display=None)` (kept, not reset, on a switch to
  continuous), j2p handler, backend spec.
- `_collapse_and_composite`: a categorical layer's per-pixel alpha is now
  **scaled** by the layer alpha rather than overwritten, so the gray context
  stays dimmer than the highlight at any alpha (real categories are 255, so
  their result is unchanged from 5a).
- `_colorize_category_values`: Field (from `ScanInfo.field_name`) and Baseline
  (from `baseline_antennas`, `"a1&a2"`).
- `colorize_controls`: "Unselected values" dropdown; on the Baseline checklist a
  "Baselines with antenna" picker (pure JS: it filters the checklist's own
  `ant1&ant2` labels); a hint line on any axis over the threshold.
  (Superseded in §11: the first draft also started high-cardinality axes with
  nothing checked and switched the dropdown to gray on such an axis; both removed.)

`visibility_plotter.py` — `excluded_display` in `_make_scatter_layers`, the
`doPlot` payload and both change-detection key helpers (now 5-tuples; a
display-only change counts as a change, and the round-trip invariant from 5a
still holds).

## 4. What each axis costs to render

4M rows, 900×600, whole `render_layer` call (incl. the hover id grid):

| Layer | Time |
|---|---|
| Scan (object strings, reference) | 287 ms |
| **Field** (categorical) | **214 ms** |
| Antenna 1 (object strings, reference) | 336 ms |
| **Baseline** (categorical, 210 present → binned to 20) | **249 ms** |
| Baseline, DV18 highlighted (20), rest gray | 258 ms |
| Baseline, nothing highlighted (all gray) | 184 ms |

The categorical path is faster than the string path it sits next to, and gray
mode is essentially free.

## 5. Cost added to every render

`_query_columns_raw`, 4M rows, warm, two interleaved A/B runs against the 5a
code: **1.83 s → 1.89 s (+0.06 s, ~3%)**; frame **+12 MB** (shallow). These
columns are built on every plot whether or not any layer uses them; the
"build only the columns a layer needs" item (5a §9.1) would remove that and
the older string-column cost together.

## 6. Verification

**Verified (sandbox, these files overlaid on published cubevis 1.0.84):**
- Field per scan and baseline labels match **casacore ground truth** read
  independently of the code under test (scans 12/16 → TW Hya, 14 → J1037-295;
  the label for every baseline of scan 12).
- `test_colorize_by_axis_part5b_field_baseline.py`: 78 tests (62 for the axes and highlight mode, 16 added by the §10 follow-up).
  **Mutation-checked**: 8 deliberate breakages (gray beating a highlight, missing
  rows going gray, unused categories in the legend, alpha overwritten instead of
  scaled, widget never sending `excluded_display`, high-cardinality axis
  starting fully checked, baseline label reversed, change detection ignoring
  `excluded_display`) were each caught.
- Full run over the render / export / PNG / part5a / part5b /
  `test_visibility_scatter` suites: **417 passed, 9 skipped, 11 failed — the same
  11 failures as before any change** (tests call `async` handlers
  synchronously: `TestAlpha` ×2, `TestColormapScaling` ×2, `TestProbeRegion` ×7).
- Updated deliberately: `TestColorizeAxisColumnsTable` (the pinned axis table
  gained two entries; the five Part 2 entries are asserted unchanged in a new
  test) and the continuous change-detection key (now a 5-tuple).

**Not verified — please check on your side:**
- **The GUI in a browser**: the "Unselected values" dropdown, the "Baselines
  with antenna" picker's JS, `axis_js`'s switch to gray on a high-cardinality
  axis, and `doPlot` sending `excluded_display`. Python-side wiring is tested;
  the JS is not executed anywhere in my environment.
- **MSv4** (user later confirmed display works on a `.ps.zarr`, 2026-09-24; the
  automated MSv4 tests remain unrun): the three MSv4 edits mirror MSv2's, but the MSv4 tests skip here
  (they need `sis14_twhya_calibrated_flagged.ps.zarr` from
  `create_test_msv4.py`, which is not in the project files).
- `test_msv2_backend.py`, `test_info_block_integration.py` (out of memory in the
  4 GB sandbox, on original and modified code alike).
- Remote execution end to end.

## 7. Known limits

- **Very large baseline lists.** Baseline's checklist is a plain
  `CheckboxGroup`. It is fine at 210 (this MS); an ALMA-scale array has ~2,100
  baselines and ngVLA ~34,000, which I have not measured and expect to be slow
  to build in the browser. Worth a filter or virtualized list if it matters.
- **The antenna picker replaces the selection** with the baselines involving
  one antenna. For several antennas, add the rest by hand or extend it.
- **Field rides on the scan lookup**: a partition with `field_name` but no
  `scan_name` coordinate would get no field column. Not seen in practice.
- **Field is by name.** PlotMS labels "Field 5: TW Hya" (id + name); two fields
  with one name merge here.
- **Gray legend swatch is opaque** while the gray pixels are dimmed (~47%).

## 8. Design-doc updates to fold in

- §4.1 axes: Field and Baseline are now in scope (the "Baseline out of scope?"
  question was answered in 5a).
- `excluded_display` / highlight mode; gray as a reserved last category.
- Field and Baseline are per-row `Categorical` columns; the reasoning.
- A categorical layer's alpha is scaled, not overwritten, by the layer alpha.

## 9. Next

1. **Build only the columns a layer needs** (now also covers `field_name` /
   `baseline_name`): removes ~0.7 s of a 1.8 s raw query and most of the frame's
   memory on plots that don't colorize.
2. **Backend frame cache** (5a §9.2): pan/zoom ~2.0 s → ~0.26 s (continuous).
3. Baseline at ALMA scale (see §7), multi-antenna picking.
4. Undecided: PlotMS-like symbol size (1 px vs ~2–3 px), layer-dropdown mode
   suffix ("XX (by Scan)"), user-chosen color per value (X-3).

## 10. Follow-up: the checklists offered values that had no data

Reported after trying the GUI: three baselines were ticked, **Plot** was pressed,
and the legend showed only "Other (not selected)". The plot was right and the
feedback was not.

**Cause.** The three were `DA41&DA45/48/50`, and **DA41 has no rows in this MS**.
The checklist is built from the identity tables' `baseline_antennas`, which
comes from the partitions' `baseline_id` *coordinate*. xarray-ms lays that out
as the full antenna-pair grid: **325 pairs for 26 antennas, of which 210 were
ever observed**. Five antennas (DA41, DV01, DV04, DV07, DV21) have no rows at
all. So the Baseline checklist, the Antenna 1 / Antenna 2 checklists and the new
"Baselines with antenna" picker all offered values that could never color
anything, and nothing said so. (Verified independently against the MS's
ANTENNA1/ANTENNA2 columns via casacore: 0 rows for each of the three.)

**Fix, in two layers.**
1. `IdentityTables.baselines_with_data` (sorted tuple, or `None` = unknown).
   Built in both backends' `identity_tables()` from `TIME_CENTROID`, which is NaN
   wherever the `(time, baseline)` grid has no row: a small non-visibility array
   (ms per partition) that reproduces the casacore answer exactly (210 of 325;
   **171 for scan 12 alone**, so it follows the selection). The widget's
   `_colorize_category_values` uses it for Baseline, Antenna 1 and Antenna 2, so
   the checklists and the antenna picker only offer real values. It is a
   **separate field on purpose**: `baseline_antennas` also feeds the hover probe,
   and its contents (still 325) are unchanged, so hover behaves exactly as before.
   `None` (backend without the variable, or any one partition unknowable)
   filters nothing.
2. A legend note, `VisibilityScatter._no_data_note`: "No data for: X, Y (+n
   more)" for any **checked** value that drew nothing. It covers what (1) cannot
   — e.g. a baseline whose every sample is flagged, or a backend that cannot
   report presence — and it is client-side only (identity tables + the render's
   `category_members`), so no backend round trip. It never raises.

**Verification.** 16 new tests, including a replay of
the exact reported case. Six deliberate breakages of the new logic: five caught;
the sixth (treating the gray group as "drawn") was an *equivalent mutant* — the
gray group's members are unchecked values, which the note already excludes — so
the dead guard was deleted instead of contriving a test for it.

**Worth knowing.** This was a *shared* limitation, not specific to Baseline: any
enumeration built from the identity tables' coordinate arrays lists what the grid
*could* hold. Antenna 1/2 had the same problem before Part 5b (DA41 was offered
there too). If another axis is enumerated from coordinates in future, check it
against rows.

**Scale (measured, 2026-09-24).** On a real 6 GB MS the presence check over all
partitions took **0.0 s** (2,162 baseline-partition hits; it also confirms the
variable exists on that MS and xarray-ms version, since a partition returning
`None` would have raised). The reason is structural: xarray-ms builds
`TIME_CENTROID` from the MS main table's per-row `TIME_CENTROID` column (8 bytes
per row) scattered into the `(time, baseline_id)` grid through its row map, with
missing cells padded as NaN. So the disk cost tracks the number of *rows*, not
rows × channels × polarizations -- three to four orders of magnitude below the
visibilities. (An earlier version of this note worried about ~160 MB for an
ALMA-sized partition; that is the transient padded grid in memory, not a disk
read, and no chunking is needed on this evidence.) MSv4 uses the same helper and
was confirmed to display correctly on a `.ps.zarr` by the user; if an MSv4
dataset lacks the variable the helper returns `None`, which degrades to the old
behavior, and the legend note still applies.

## 11. Follow-up 2: categorical layers on top; hide by default; all checked

Prompted by looking at the four-baseline highlight in a panel that also had a
continuous `YY` layer: the gray "Other (not selected)" context was mostly covered
by `YY` (79% of the gray pixels had a `YY` pixel on top in a reproduction), and
what remained washed out the density ramp.

**Compared four options** on the real MS (`layer_order_options.png`): today's
order with gray; categorical on top with unselected **hidden**; categorical on
top with gray over the continuous layer; and gray moved *under* everything. Hidden
was cleanest; gray over `YY` was muddiest; and gray-underneath added nothing,
because the continuous layer already draws all the data and so *is* the context.
The extra "gray goes underneath" split was therefore not built.

**Decisions (user's proposal, agreed):**
1. **Categorical layers are drawn above continuous ones**, each group in layer
   order (`VisibilityScatter._stack_order()`, used by `_collapse_and_composite`,
   so the PNG export follows too). A fixed rule, not a control. It also fixes the
   original mixed-panel report where `YY` buried the scan colors.
2. **"Unselected values" is Hide everywhere by default** (the data-layer default
   already was; the GUI's auto-switch to gray on a high-cardinality axis is gone).
   Gray stays as an opt-in; it earns its place when a categorical layer stands
   alone or every layer is categorical, where stacking is moot.
3. **Every axis starts with every value checked**, including Baseline (its first
   view is the render's binned groups; narrow with "None" + a few checks, or the
   antenna picker). This removes the blank-canvas state, so no special design was
   needed for it. The hint under a high-cardinality checklist now says values
   share 20 colors and how to narrow them.
4. **Empty state**: unchecking everything now says "All <axis> values are
   unchecked — check some to plot" instead of the generic "no categories in
   current selection" (which reads like a data problem). The generic wording
   remains for a layer that really has no data.

**Behavior change to expect:** any mixed panel looks different (the categorical
layer is now above), and its points are opaque over the continuous layer. Per-layer
alpha/hide still work.

**Verification.** New tests: `TestStackingOrder` (the order for five layer mixes;
a real-pixel check that a highlighted point is never covered by the continuous
layer, in either layer order; a hidden categorical layer covers nothing; layers are
never reordered themselves), `TestEmptyState`, and updated `TestControls` (all
checked; hide default; no JS side effect on axis switch; the hint). Five deliberate
breakages (drawing by raw index; order inverted; empty start restored; empty-state
message dropped; auto-gray restored) were each caught. The one test that pinned the
old "starts empty" default was replaced.

## 12. Follow-up 3: legend width, the empty-plot warning, and the PNG legend

**Reported (2026-09-24):** with every baseline checked, the binned range labels
("DA42&DA44–DA42&DV13") were clipped to "DA42&DA44–DA…" in a fixed 110 px column;
and a categorical plot with nothing to plot should say so in the red line at the
bottom, consistent with the app's other warnings.

**1. Live legend column width follows the longest label.**
`_legend_column_width(labels)` (in `visibility_scatter.py`): `max(110, min(320,
longest × 7.4 + 24))` px, used for the CSS `column-width` in `_legend_html`. The
7.4 px/char was measured from the report (an uppercase-heavy label clipped at 110
px showed ~13 characters in the ~94 px left after swatch and gap). Short labels
(scan numbers) stay at 110 px; a label too long even for 320 px is ellipsized and
carries its full text in a `title`.

**2. Nothing-to-plot warning in the notification line.**
`VisibilityScatter.empty_categorical_warnings()` returns one plain-language line
per categorical layer whose backend skip reason is "all <axis> categories
excluded" (i.e. hide mode with every value unchecked; gray mode is a valid
all-gray render and correctly does not warn; a layer with no data has a different
skip reason). `_colorize_warning_text(slots)` in `visibility_plotter.py` collects
them from ACTIVE scatter slots only (an idle slot's stale state must not warn),
prefixed "⚠ Panel B —". `_handle_plot` then calls `_notify(text, color=...)` and
returns `notify_text` + `notify_color` — the same channel the other warnings use,
because with no Bokeh server the browser only sees what the response carries
(`doPlot`'s handler applies both). The color is one named constant,
`_NOTIFY_WARN_COLOR = "#f38ba8"`, asserted equal to the file's existing warning red
(`_EDIT_TITLE_COLOR`, the raster axis-conflict warning's color). The next Plot press
returns `notify_text: ""`, clearing it. The legend's own placeholder ("All <axis>
values are unchecked …") is kept as well.

**3. Found while checking (1): the exported PNG had the same flaw, worse.**
`png_export._legend_ncol` chose `min(n, 6)` columns regardless of label width, so 20
binned baseline labels were laid out six across and **ran off both edges of the
image** (the first column was cut off). Fixed: `_legend_ncol` / `_legend_rows` take
an optional `avail_pt` width budget and `mode` ("panel" or "figure", which carry
different font size and column spacing), and never use more columns than fit at the
longest label; `avail_pt=None` reproduces the old count-only behavior exactly. A
panel legend measures its own axes (`_axes_width_pt`); the shared figure legend is
given the width of the grid. The rows reserved above the axes and the columns
drawn are computed from the same budget (a spy test pins that), otherwise a legend
could be clipped or leave dead space. The column width is an estimate (~0.66 em per
character plus the handle and spacing), because no renderer exists while space is
still being reserved. On the reproduction the legend now fits in 3 columns × 7 rows
and the image is ~60 px taller.

**Verification.** `test_colorize_by_axis_part5d_legend_status.py` (44 tests):
the width helper; the legend against the real binned-baseline labels; the warning
text; the plotter collector (active vs idle slots, several panels); **end to end
through the real `VisibilityPlotter._handle_plot`** (response fields, the live
`_notify_div` text and color, clearing on the next press, survival of a repeated
press, gray mode and partial checking not warning, and the legend width in the
response); and the export legend, including a **measured bounding-box test** that
the old layout overflowed the figure and the new one fits. Eight deliberate
breakages were each caught.

**Recipe: a small MS for end-to-end plotter tests in a memory-limited
environment.** `test_info_block_integration.py` builds a real plotter on the whole
MS and exhausts a 4 GB machine (the full test MS is ~62M samples per layer).
`table(MS).query("SCAN_NUMBER==14").copy("scan14_small.ms", deep=True)` (casacore)
gives a 17 MB, 1,900-row MS that opens normally and runs the same code paths; point
`MS=` at it.

**Not verified:** the notification line's appearance in a real browser (the
response and the live div are tested, not the rendering); the export legend's width
estimate on fonts/labels very different from these (the bounding-box test uses the
real matplotlib renderer with this file's fonts).

## 13. Part 6: the backend frame cache (pan / zoom / recolor no longer re-read the MS)

**The problem (measured in the very first review).** Since binning and shading moved
backend-side, every `query_columns` re-read the selected data from disk. On a
4M-sample slice, one layer: the read is ~1.8 s and the render ~0.26 s, so a pan or
zoom cost ~2.0 s where the render alone is ~0.26 s. A recolor, a PNG export and
any other re-render paid the same. The read does not depend on the viewport.

**The design.** `XArrayReader._query_columns_cached` (shared base class, so MSv2
and MSv4 get it identically) keeps the per-layer frames the raw read returns:
- **One entry per layer** — key `(x axis, y axis, polarization, selection
  fingerprint)`. Adding or dropping a layer reuses the others; a request that is
  half cached reads only the missing layer(s) (together, so the fused read is
  still shared).
- **Byte-budgeted LRU.** Default budget: `$CUBEVIS_VISPLOT_FRAME_CACHE_MB`, else a
  **tenth** of physical memory clamped to [256 MiB, 4 GiB] (512 MiB if memory cannot
  be determined; macOS falls back to psutil then `sysctl hw.memsize`). `0`
  disables the cache entirely, which reproduces the old behavior exactly.
  `set_frame_cache_limit_mb()` changes it live; `frame_cache_stats()` reports
  entries / bytes / hits / misses / evictions. A frame larger than the whole
  budget is simply not stored.
- **Process-wide, keyed per backend.** There is ONE cache per process (a shared
  budget), and every key starts with a per-backend token. A per-backend budget of
  "a quarter of memory" multiplies by the number of backends — three plotters in one
  Jupyter kernel would claim three quarters of RAM, and a backend never closed would
  hold its frames until garbage collection (a first draft did exactly this; found
  when a multi-suite test run started swapping the timing tests). The token means a
  backend can never be served another's frames; `close()` drops exactly that
  backend's entries, and a `weakref.finalize` drops them if the backend is
  collected without being closed. It still lives on the backend side, which
  `VisplotRemoteBackend` constructs once per session, so it works identically
  locally and in a remote worker; the per-row frames never cross the wire (only
  the small render result does).
- **Frames are handed back as shallow copies**, so a caller adding or replacing a
  column cannot change what the cache holds, and rendering never writes into a
  frame (both pinned by tests). The frame's `(x, y)` extent is memoized in
  `df.attrs`, saving four O(N) passes per call.
- **Freshness: `SelectionSpec.cache_generation`.** A cached entry is valid only for
  the generation it was read under. The plotter bumps it on **Reload**, so the
  next query re-reads from disk and the stale entry is dropped, not accumulated.
  The generation rides on the selection because that object already travels to
  the backend on every call, locally or remotely, so **no new RPC** (and none of
  the four proxy layers) had to change. It is excluded from `is_empty()` and
  from the fingerprint, and preserved by `copy()` (which lists fields by hand and
  would otherwise have silently dropped it).
- **`close()` releases the frames** (via `_clear_lookup_caches`).

**A behavior change you should know about: Reload now reloads.** Previously the
Reload button only cleared the pending-flag preview and never re-rendered anything
by itself. With a cache it needs a real meaning, and PlotMS's own "Reload" is "read
the data again". So `_handle_plot` now (a) bumps the generation and (b) folds
`did_reload` into `axes_changed` for both the raster and scatter panels, so they
genuinely re-render from a fresh read. Consequence to be aware of: **pressing Plot
with nothing changed no longer re-reads the disk**; if the MS is modified by another
process (e.g. a CASA flagging task), press Reload. (Pending flags in this app are
preview-only and never written, so the app itself cannot make a cached frame stale.)

**Measured** (4M samples, 1 layer, 900×600, one core; the cold read figure below is
the same ~1.8 s as before):
| Call | Before | After |
|---|---|---|
| pan / zoom (continuous) | 1.89 s | **0.24 s** (7.9×) |
| back to full view | ~1.9 s | 0.26 s |
| recolor by Scan (same selection) | ~1.9 s + 2.6 s categorical | **0.63 s** |
Cached result is pixel-identical to the uncached one (image, ranges, canvas, id grid,
categories). The remaining 0.63 s on recolor is the object-string categorization —
the next item.

**Verification.** `test_frame_cache.py`: 64 tests — hits (pan, zoom, recolor,
half-cached, dropped layer, list-vs-tuple selection), misses (selection, x axis, y
quantity, polarization, generation), budget/LRU/eviction/`0`/oversize/lowering, budget
defaults (env, clamps, unknown memory), safety (copies, no mutation by rendering,
concurrent requests read once, unhashable selection degrades to uncached, extent),
the shared budget (two backends, isolation, close, collection, shared limit), the LRU on its own, the `cache_generation` field, and **end to end through the real
plotter** (a recolor press does zero reads; Reload bumps the generation and really
re-reads; the press after Reload is cached again). Sixteen deliberate breakages (serving
a stale generation, a missing backend token, leaking collected backends, ignoring the selection or polarization in the key, handing out the
cached object, no LRU recency, no budget, `copy()` dropping the field, Reload not
bumping / not re-rendering / not reaching the selection) were each caught.

**Not verified:** the **remote** path (a worker-side cache is the same code on the
same object, and `SelectionSpec` is serialized generically, but I could not run
`test_remote_reduction_context.py`); MSv4 (same base-class code; its tests need the
`.ps.zarr`); a real browser press of Reload.

**Trade-offs, honestly.** A cached frame holds ~110 bytes/row today (the string
columns are about half of it), so a 30M-sample layer is ~3 GB — within the default
budget only on a large machine; a frame over budget is not cached and behaves exactly
as before. Shrinking frames is the next item.

**Why a tenth, not a quarter (a finding, not a guess).** A read's transient memory
peaks at ~4× its final frame (1.7 GB RSS for a 0.44 GB frame, measured) and the cache
keeps *other* frames alive during it. The first draft used a quarter of RAM; on the
4 GB test machine a combined test run then swapped and failed a 10 s timing test that
passes alone in 4.2 s (cache on: 4.2 s, off: 4.4 s — no per-call cost). Capping the
budget at 256 MiB made the identical run pass, in 115 s instead of 200+ s. Hence a
tenth, with a test that pins "cache + one 4× read fits" across machine sizes. A frame
over budget is simply not cached, exactly the old behavior; raise it with
`CUBEVIS_VISPLOT_FRAME_CACHE_MB` on a big machine. **Not done:** evicting *before* a
read that is known to be large (the read size is not known up front without walking the
partitions first); the smaller default is the mitigation.

## 14. Part 6b: every identity column is a small-integer Categorical

**The problem.** scan / antenna1 / antenna2 were per-row `object` columns (~0.17 s each
per 4M rows to build, 8 B/row of pointers) and spw / polarization per-row pandas `str`
columns (38 and 10 B/row): ~80 of a frame's **111 B/row** and ~0.65 s of a 1.7 s read.
They were built on every read whether or not any layer colored by them — and, since
Part 6, every cached frame holds them. (Part 5b had already made Field and Baseline
categoricals for exactly this reason; this finishes the job.)

**The change.** `XArrayReader._identity_categoricals` now builds ALL of them in one call:
`scan_name`, `field_name`, `baseline_name`, `baseline_antenna1_name`,
`baseline_antenna2_name`, `spw`, `polarization`. Each is `Categorical.from_codes(int
codes, MS-wide category list)`: one fancy-index of a small int array plus a range check.
MS-wide lists (`_scan_categories`, `_spw_categories`, `_antenna_code_tables`,
`_field_categories`, `_baseline_table`) are what let per-partition frames `pd.concat`
without falling back to `object`. `_PartitionScanLookup` gained `scan_codes` (the scan
analogue of `field_codes`; `scan_names` is kept). The three backend assembly sites
(MSv2 per-partition; MSv4 per-partition and fused) each shrank to one call.
`_categorize` already read a Categorical's codes directly (Part 5b), so no rendering
change was needed — and a test pins that.

**Measured** (4M samples, 1 layer, warm, same slice as before):
| | Before | After |
|---|---|---|
| raw read (`_query_columns_raw`) | 1.56 s | **1.05 s** (−33%) |
| frame size | 111 B/row (445 MB) | **44 B/row (176 MB)** (−60%) |
| recolor by Scan (cached frame) | 0.63 s | **0.19 s** (3.3×) |
| recolor by Antenna 1 / Baseline / Field / SPW | (all similar, ~0.6 s+) | 0.22 / 0.22 / 0.19 / 0.18 s |
| pan / zoom (cached frame) | 0.24 s | 0.24 s |
Every axis now recolors as fast as a pan/zoom. The smaller frames also mean the frame
cache (Part 6) holds ~2.5× more per byte of budget.

**Correctness.** Rendering is unchanged: for every colorizable axis, `render_layer` on the
categorical frame and on the equivalent all-strings frame (what the code used to build)
gives identical categories, members, colors and pixels (a test). Values are checked
against casacore reading the MS directly (antenna names per scan, field per scan,
baseline label = the two antennas row-wise). One defensive addition: the SPW code uses
`searchsorted`, which returns an *insertion point* for a value not in the list — a wrong
label on every row if it ever happened — so it is verified with an exact match and the
column is omitted otherwise (the first version of that test used a single-SPW MS where
the bounds check alone hid the gap; it now includes an identity that sorts *before* a
real one).

**Tests.** `test_frame_columns.py`: 42 tests (dtype and no-missing for all seven columns, a
size regression guard, values vs casacore, shared categories and MS-wide lists, a
multi-partition selection staying categorical, render equivalence for every axis, and the
builder's edge cases: out-of-range ids, empty frames, unknown SPW, polarization needing a
row count). Seven deliberate breakages (scans mislabelled, antennas swapped, out-of-range ids
labelled, unknown SPW mislabelled, wrong polarization, a return to per-row strings, and the
SPW exact-match check removed) were each caught. Two existing tests changed *because the
representation changed*, not the behavior: `test_msv2_backend.py`'s dtype guard
(`dtype == object`, "avoid the slow pandas string conversion") now asserts categorical —
the guard's purpose is served better; and Part 5b's baseline-label test concatenated the
two antenna columns with `"&"`, which needs `.astype(str)` on categoricals.

**MSv4 confirmed** (2026-09-25, on the real `.ps.zarr`, by the user): `test_msv4_backend.py`
had one failure, `TestColorizeByAxisColumns::test_scan_and_antenna_columns_avoid_expensive_string_dtype`
-- the exact twin of `test_msv2_backend.py`'s guard, which I had updated for Part 6b but
missed its MSv4 copy. Fixed the same way (see that test's docstring in both files). I
verified the fix by installing `xradio`+`toolviper` here and converting the one-scan test MS
to its own `.ps.zarr`; against that, 125 passed, 1 skipped, and the SAME TWO failures as the
*completely unmodified original* `test_msv4_backend.py` + `msv4_backend.py`:
`TestApplySelection::test_time_range_reduces_time_size` and
`TestQueryRaster::test_max_cells_limits_output_size` -- both data-scale artifacts of the
1,900-cell (10 time x 190 baseline) one-scan grid, confirmed by inspecting the `.ps.zarr`
directly (`test_max_cells_limits_output_size` asserts decimation at a 10,000-cell cap, which
a 1,900-cell grid never reaches; `query_raster` is untouched by this session's edits, so
this could only be a test-vs-fixture-size mismatch, not a regression). `test_msv2_backend.py`
was run on the whole MS by the user: fully passing. `test_msv2_backend.py` was also run here
on the one-scan MS: 77 passed, 1 failed, that same `test_time_range_reduces_time_size` as on
the original sources.

**Still not verified:** the remote path.

**What remains in a frame (44 B/row).** The five numeric columns are 36 of the 44:
`x` f64 8, `y` f32 4, `time` f64 8, `baseline_id` int64 8, `frequency` f64 8. Roughly 2×
more is available (`baseline_id` as int16/int32; `time`/`frequency` as small indices into
their per-partition tables, keeping `x` full precision where it is time) but that is a
bigger change to the hover id-grid inputs and was not done.
