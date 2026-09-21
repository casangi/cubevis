# Handoff to Part 4: UI Wiring

**From:** Part 3 (rendering pipeline: categorical aggregation, dataclass
fields, categorical palette)
**To:** Part 4 (axis picker, mutual-exclusivity logic, category legend
widget, export swatches)
**Companion documents:** `visplot-colorize-by-axis-design.md` (updated —
read §4.2 in full; it was rewritten this session, not just appended to),
`test_colorize_by_axis_render.py` (the permanent regression coverage for
everything below — see "Verification").

---

## What landed

Three files changed, all within Part 3's declared scope
(`_scatter_render.py`, `reader.py`, `palettes.py` — no backend or UI files
touched):

**`reader.py`**
- `ScatterLayerSpec.coloring: str = "continuous"` and
  `colorize_axis: Optional[Axis] = None`, with `__post_init__` validation
  (raises `ValueError` immediately for an inconsistent combination — see
  the class docstring for the exact rules). `coloring` is a string, not a
  bool, on purpose: Part 5 will want a third value for its color-source-
  column capability (§7.6/§7.7 of the design doc), and a two-state boolean
  would need a real rework to grow a third state later.
- `cmap` is REUSED for categorical mode, not duplicated into a second
  field: an ordered, already-theme-conditioned discrete color set (from
  `palettes.categorical_cmap(...)`) rather than a gradient, assigned to
  categories in sorted order and cycling modulo its length. This mirrors
  the existing division of labour — the widget resolves a palette *name*
  to concrete colors, the backend/render path just consumes them —
  `_scatter_render.py` never imports `palettes.py`.
- `COLORIZE_AXIS_COLUMNS: dict[Axis, str]` — the axis → per-row-column
  lookup table the Part 3 handoff asked for, built directly from Part 2's
  "What landed" table. `colorizable_axes()` returns its keys as a tuple,
  for the axis-picker's option list.
- `DEGENERATE_COLORIZE_AXES: frozenset` — currently just
  `{Axis.CORRELATION}`. Real, correctly-populated column; just never more
  than one category for a single layer (a layer already fixes one
  polarization). **Part 4's call, not resolved here:** whether to offer it
  in the axis picker at all, gray it out, or show it with a note. Nothing
  in the rendering path refuses it — it renders a valid one-entry legend.
- `ScatterLayerRender` gained three fields, populated together on a
  successful categorical render and all `None` for a continuous one or a
  skipped/empty categorical one:
  - `categories: Optional[tuple[str, ...]]` — the DISPLAY categories
    (post-binning; see below), in the same order `category_colors` and
    `category_members` use.
  - `category_colors: Optional[dict[str, str]]` — display category → hex
    color actually used in `image`. This is the "categorical palette"
    artifact Part 3 was scoped to produce.
  - `category_members: Optional[dict[str, tuple[str, ...]]]` — display
    category → the real, raw underlying value(s) it represents. **Always**
    a 1-tuple of itself in the common (unbucketed) case — see next
    section for why this matters to how Part 4 should read it.

**`_scatter_render.py`**
- `render_layer` branches cleanly on `lyr.coloring`. A categorical layer
  gets `categories`/`category_colors`/`category_members` populated and
  `peak_value`/`hist_counts`/`hist_edges`/`mapping_x`/`mapping_u` left
  `None` (no continuous colorbar/histogram concept applies — matches
  design doc §4.3's mutual-exclusivity). The hover-probe id grid is
  unaffected either way.
- `_resolve_categories(df, column, axis_label, cap)` → `(mask, categories,
  category_members, skip_reason)`. Only two skip reasons remain, both
  "no data at all" cases (the column is missing from `df` entirely, or
  present but 100% NaN for this selection) — **there is no longer a
  cardinality-cap skip reason.** See `_bin_categories` and `CATEGORY_CAP`'s
  docstring for why (real measurement showed refusing would make the
  feature nearly unusable at ngVLA's antenna count; full numbers in the
  design doc §4.2).
- `_bin_categories(distinct, cap)` — groups real distinct values into at
  most `cap` contiguous, near-equal buckets when there are more than
  `cap` of them; returns them unbucketed (each its own singleton) when
  there aren't. A multi-value bucket is labeled `"{first}–{last}"` (en
  dash, matching `VisibilityScatter._rect_title`'s existing range-format
  convention); a singleton bucket is labeled as the value itself.
- `_argmax_shade(agg, categories, cmap, min_alpha)` — winner-take-all
  categorical shading, replacing Datashader's own
  `tf.shade(agg, color_key=...)` blend. Every non-transparent pixel is
  **exactly** one of `cmap`'s colors (verified directly, not just
  typically) — no pixel can render a color absent from the legend. Alpha
  channel reuses `colormap_scaling.equalize_histogram` on total per-pixel
  count, same visual language as continuous eq_hist coloring.
- `CATEGORY_CAP = 20` — unchanged value, completely different
  justification. It's a legibility ceiling (how many swatches a person
  can actually read in a legend) that happens to coincide with a
  rendering-cost ceiling once binning bounds `K` at a constant. It is
  **not**, and never really was, a wire/message-overhead limit — the
  returned `image` stays a flat `H×W` array regardless of category count,
  and `categories`/`category_colors`/`category_members` stay tiny (a few
  hundred bytes even at K=263) either way.

**`palettes.py`**
- `categorical_cmap(name=None, theme="dark", n=None)` — 20 colors (a
  plain copy of Bokeh's Category20, not a `bokeh` import, to keep this
  module dependency-free), each individually contrast-checked against the
  theme background (`_nudge_for_contrast`) rather than resampled as a
  ramp (there's no ramp here, just a flat set). `n` cycles modulo the base
  palette's length, matching `scatter_cmaps()`'s existing convention.
- `categorical_names()` for symmetry with `raster_names()`/
  `scatter_names()`.
- `check_background_contrast()` extended to audit the categorical palette
  too — zero complaints on both themes as of this handoff.

## Why the cap changed from "refuse" to "bin" mid-Part-3

This wasn't planned going in — Part 3 started implementing the design
doc's original §4.2 (refuse over ~20, ask the user to narrow), and the
concern about ngVLA's 263-antenna array (raised mid-session) is what
prompted actually measuring the real cost instead of assuming. Headline
numbers (400×300 canvas, 1M rows, JIT-warmed, best-of-3 — see
`_scatter_render.CATEGORY_CAP`'s docstring for the extrapolation to
larger canvases):

| | Datashader blend (original) | Winner-take-all (`_argmax_shade`) |
|---|---|---|
| Cost per category | ~1.5 ms | ~0.2 ms |
| Extrapolated, 1920×1080, K=263 | **~6.8 s** | **well under 1 s** |
| Aggregation memory, K=263 @ 1080p | ~2.2 GB (same either way — this is the `ds_agg.by` array itself, not a shading cost) | |

Both costs are driven by `canvas_pixels × K`, confirmed independent of
row count (50K vs. 8M rows changed shading time under 5%) — a sparse
selection gets no discount. A flat refusal at K=20 would have made
colorize-by-axis unusable for antenna axes on exactly the arrays (ngVLA,
and to a lesser extent full ALMA/VLA) where spotting a bad antenna is the
whole point of the feature (see the design doc §7.1's motivating
workflow). Binning down to a constant cap fixes this without needing a
resolution-aware or axis-specific cap: cost is bounded by the cap itself,
never by true cardinality, so a 263-antenna ngVLA selection costs the
same to render as a 26-antenna ALMA one.

Switching to winner-take-all shading was almost a separate decision that
happened to arrive at the same time — it's not just faster, it's the only
approach where "which legend entry is this pixel" has a real answer
(Datashader's blend can produce a color that matches no swatch at all).
Recommend keeping both changes together rather than reverting either in
isolation.

## What this means for Part 4's legend widget

**`category_members` is the field to build the legend from, not just
`categories`/`category_colors`.** For any display category:

- `len(category_members[cat]) == 1` → an ordinary, unbucketed category.
  Show it as-is (`cat` itself is the real value — e.g. `"DA42"`).
- `len(category_members[cat]) > 1` → a bucket. `cat` is already a
  human-readable range label (e.g. `"DA05–DA19"`) suitable for the
  legend row itself; `category_members[cat]` is the full list of real
  values it covers, suitable for a tooltip/expansion
  (`"DA05, DA06, DA07, ... DA19"`) if you want one. Nothing downstream
  needs to know which case it is before deciding what to draw — the
  length check is the only branch needed, and it's optional (a legend
  that just shows `cat` and ignores membership entirely is still
  correct, just less detailed).

**Bucketing doesn't reduce what a user can find out about any specific
point.** Exact antenna/scan/SPW identity for anything under the cursor or
inside a clicked region already goes through `IdentityTables`/
`probe_scatter_region` — a real per-row lookup, never a decode of the
rendered pixel color. A bucketed legend only limits how many colors are
shown side by side in one glance; narrowing the selection (e.g. an
`antenna_names` filter) to fewer real values than the cap removes
bucketing entirely for that narrower view, at full per-antenna color
resolution.

**Mutual exclusivity (design doc §4.3) is unchanged and still Part 4's
job**: enabling `coloring="categorical"` on a layer should hide/disable
that layer's continuous scaling controls (colormap picker, span/vmin/
vmax, eq_hist toggle) and show the categorical legend instead. `render_layer`
already enforces this at the data level (a categorical
`ScatterLayerRender` never carries `peak_value`/`hist_counts`/etc.) —
Part 4 just needs the widget-side control visibility to match.

## Real cardinality findings (confirmed, both backends)

On `sis14_twhya_calibrated_flagged` (26 antennas, 1 SPW, 1 observation, 2
correlations, 17 scans), within the same 15%-of-full-selection window
Part 2's own tests used:

| Axis | Categories returned | Binned? |
|---|---|---|
| Scan | 2 (within this narrow window; up to 17 across the full MS) | No |
| Antenna1 | 20 | No — exactly at the cap, real antenna names |
| Antenna2 | 20 | No — exactly at the cap, real antenna names |
| SPW | 1 (`"ALMA_RB_07#BB_2#SW-01#FULL_RES"` — a name, not an int, on this MS) | No |
| Correlation | 1 (`"XX"`) | No — degenerate, as expected |

Synthetic tests confirm binning itself: an ngVLA-scale 263-antenna column
renders successfully as exactly 20 buckets (263 = 20×13 + 3, so 3 buckets
of 14 and 17 of 13), covering all 263 real values, in well under a second
on a 400×300 canvas.

## Verification

`test_colorize_by_axis_render.py` (new file, `tests/manual/visplot/`) —
67 tests, all passing against real MSv2 and MSv4 data (including the
MSv4-only OPT-B cross-partition path) plus synthetic edge cases:

- `ScatterLayerSpec` validation (every valid/invalid `coloring`/
  `colorize_axis` combination).
- `COLORIZE_AXIS_COLUMNS`/`colorizable_axes`/`DEGENERATE_COLORIZE_AXES`
  match the Part 2 "What landed" table exactly.
- `_category_sort_key` numeric-aware ordering.
- `_bin_categories`: under/at/over cap, the ngVLA 263-antenna case
  specifically, contiguous coverage, en-dash labeling, remainder
  distribution.
- `_resolve_categories`: missing column, all-NaN column, over-cap no
  longer skips, mixed int/str `spw` merges into one category.
- `render_layer` categorical path: basic render, continuous/categorical
  field mutual exclusivity, the NaN-folding Datashader bug (regression
  guard — see `_resolve_categories`'s docstring for what this guards
  against), color stability across pan/zoom, cmap cycling, degenerate
  Correlation axis, the ngVLA-scale case end-to-end with a timing
  assertion, and — the property `_argmax_shade` exists for — every
  rendered pixel matches exactly one legend color, verified by decoding
  actual pixel bytes and checking against `category_colors`, not just
  spot-checked.
- `palettes.categorical_cmap`: color count, cycling, theme differences,
  unknown-name fallback, `check_background_contrast()` integration.
- End-to-end through `MSv2Backend`/`MSv4Backend.query_columns` for every
  colorizable axis, plus a mixed categorical+continuous multi-layer call.

Pre-existing suites re-run clean throughout this work with zero
regressions: `test_msv2_backend.py` (77 passed, 1 skipped — one test
excluded for local sandbox memory limits, unrelated to Part 3),
`test_msv4_backend.py` (127 passed, 1 skipped), `test_scatter_helpers.py`.

## Suggested first steps for Part 4

- Build the axis-picker option list from `colorizable_axes()`, not a
  hand-maintained list — keeps it impossible for the picker and the
  render path to drift apart. Decide what to do about
  `DEGENERATE_COLORIZE_AXES` (exclude, gray out, or show with a note).
- Wire `coloring`/`colorize_axis` into `VisibilityScatter`'s widget-side
  `ScatterLayer` → `ScatterLayerSpec` construction
  (`_render_all_layers` in `visibility_scatter.py`) — this file wasn't
  touched in Part 3 (out of its declared scope) but will need a new
  widget-side `colorize_axis`/`coloring` control analogous to the
  existing `scaling`/`cmap` ones.
- Legend widget: iterate `zip(result.categories, ...)`, pull color from
  `result.category_colors[cat]`, and optionally expand
  `result.category_members[cat]` for a bucket's tooltip — see "What this
  means for Part 4's legend widget" above.
- Export swatches (`png_export.py`): same `category_colors`/
  `category_members` pair covers this; no new backend data needed.
- Mutual exclusivity: hide/disable continuous scaling controls
  (`panel_spec.py`) when a layer's `coloring == "categorical"`.
- If Part 4 (or Part 5) ever needs a resolution-aware version of this
  (e.g. an even-higher cap for a very small canvas where the cost budget
  would allow it), the hooks are `CATEGORY_CAP` (currently a flat
  constant) and `_bin_categories`'s `cap` parameter — both are already
  parameters, not hardcoded into `_resolve_categories`'s call sites, so
  this would be a small, local change rather than a redesign. Not
  recommended pre-emptively; the flat cap's cost is already
  canvas-size-independent by construction, so there's no correctness
  pressure to do this, only a possible "could show a few more colors on
  a small canvas" nicety.
