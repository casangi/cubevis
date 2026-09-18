# Part 4 (UI wiring) — implementation notes

Companion to `visplot-colorize-by-axis-handoff-part4.md`. Covers the four
modified files in this delivery plus one new test file. Written so a
future session (or a colleague) can pick this up without re-deriving the
reasoning from a diff.

## Decisions made this session

1. **Correlation excluded from the axis picker entirely**, filtered by
   membership in `DEGENERATE_COLORIZE_AXES` rather than a hardcoded axis
   name — if that frozenset's membership ever changes (e.g.
   `ScatterLayerSpec` grows multi-polarization-per-layer support, the one
   thing the design doc names as what would un-degenerate it), the picker
   adjusts with no code change. `update_colorize()` itself does **not**
   block a direct request for Correlation — only the picker omits it as
   an option — matching the design doc's own stance that it's a valid,
   just uninformative, choice.
2. **One layer selector governs both scaling and colorize controls
   together**, not two independent selection mechanisms. Every layer's
   combined column is built up front ("build for N, ship for 2", one
   level down from the existing per-slot config-panel precedent) and
   switching is a client-side visibility toggle — no comm round trip,
   since the controls inside each column already send their own backend
   messages independently of which column is currently shown.
3. **Bucketed-category tooltips via a plain HTML `title` attribute** on
   the legend rows, since the legend is already an HTML `Div` — full
   member list on hover at effectively no extra cost, no new Bokeh
   tooltip machinery.

## What changed, by file

### `visibility_scatter.py`
- `ScatterLayer` gained `coloring`/`colorize_axis` fields, validated in
  `__post_init__` exactly mirroring `ScatterLayerSpec` (Part 3, in
  `data/reader.py`).
- `update_colorize()` — new public method, mirrors `update_scaling()`'s
  shape. Handles mode/axis resolution (defaulting to the first
  non-degenerate colorizable axis when switching to categorical with no
  axis chosen yet) and swaps `cmap` in and out of a categorical palette
  on the transition, caching the prior continuous cmap per layer index
  so switching back restores it instead of leaving a discrete palette
  assigned to a continuous ramp.
- `colorize_controls(layer_index=0)` — new widget builder, same
  `CustomJS`/`comm.send()` pattern as `colormap_controls()`. Builds a
  Continuous/Categorical `RadioButtonGroup`, an axis `Select` (options
  from `colorizable_axes()`, minus `DEGENERATE_COLORIZE_AXES`), and a
  legend `Div`.
- `_legend_html(layer_index)` — new helper, renders the categorical
  legend as an HTML swatch list from `category_members` (not just
  `categories`), giving bucketed categories a `title` attribute listing
  every real value they cover.
- `_handle_colorize` — new comm handler (`vs_colorize`), registered in
  `_register_extra_comm_handlers`. Returns the composite image plus
  `legend_html` for the widget's `Div`.
- `_render_all_layers` now sends `coloring`/`colorize_axis` in every
  `ScatterLayerSpec` and caches `categories`/`category_colors`/
  `category_members` per layer from the backend's response.
- **Every other `ScatterLayer(...)` reconstruction site** (`set_alpha`,
  `update_scaling`, `_with_default_cmaps`, `_handle_update_axes_scatter`)
  updated to carry the two new fields through — this class reconstructs
  the whole dataclass on every mutation rather than using
  `dataclasses.replace`, so any site not updated would have silently
  reset a layer's colorize state on its next alpha or scaling tweak.
  `_with_default_cmaps` also had to special-case categorical layers
  (fill with a categorical palette, not the continuous `_layer_cmaps`
  cycle) since it runs whenever a layer arrives with `cmap=None`.
- `_shade_for_export`'s save/restore tuple extended with the three new
  per-layer caches — otherwise a PNG export at a different viewport
  would silently overwrite the live widget's legend state.
- Three per-layer-list init sites (`__init__`, `update_axes`, deferred
  `_render`) updated to size the new lists alongside the existing ones.
- `_panel_spec()` populates `ColorBand.kind`/`categories`/
  `category_colors`/`category_members` directly (cheap cached reads, no
  need to wait for the expensive `_bands_with_mappings` step the way
  `mapping`/`peak_density` do).
- `_bands_with_mappings()` now passes a `kind="categorical"` band through
  untouched instead of forcing `kind="density"` onto every band — that
  unconditional overwrite predates Part 4 and would otherwise make
  `png_export.py` draw a (nonexistent) density ramp on a categorical
  layer instead of its category legend.

### `panel_spec.py`
- `ColorBand` gained `categories: Optional[tuple[str, ...]]`,
  `category_colors: Optional[dict]`, `category_members: Optional[dict]`,
  and a new `kind="categorical"` value (alongside the existing
  `"value"`/`"density"`). All backward compatible — every new field
  defaults to `None`, and a `ColorBand` built exactly as before Part 4
  is unaffected (covered by
  `test_pre_part4_band_unaffected`/`test_plain_band_key_shape_unchanged`
  in the new test file).

### `png_export.py`
- `_legend_handles` now produces one swatch per **category** (not just
  per layer) for any visible `kind="categorical"` band, alongside the
  pre-existing per-layer identification swatches for plain bands.
- `_wants_legend`/`_categorical_bands` (new) replace the old
  `multi_band` gate — a single categorical layer now reserves legend
  space and gets one, where before Part 4 a single band never did.
- `_band_key` now includes `categories` in its identity tuple, so
  grid/iteration mode won't treat two cells with the *same* colorize
  axis but *different real categories* (e.g. different scan subsets) as
  sharing one figure legend — that would be correct for only one of
  them.
- `_legend_ncol`/`_legend_rows` (new) cap a categorical legend at 6
  columns and wrap the rest into additional rows; the reserved legend
  space (`lg_panel`/`lg_fig`) and per-cell title padding both scale with
  the resulting row count.
- **Two real bugs found and fixed while testing this against actual
  `PanelSpec`/`ColorBand` inputs** (not just by inspection):
  1. `_legend_ncol` originally sized columns from `len(bands)` (the
     *band* count) instead of the *handle* count — a single 20-category
     band produced `ncol=1` (one absurdly wide row) instead of wrapping
     at the intended cap of 6.
  2. `_legend_handles`/`_legend_handle_count`'s condition for drawing a
     plain band's identifying swatch was `len(plain) >= 2` — so one
     continuous layer overlaid with one categorical layer (2 visible
     bands total, only 1 of them "plain") silently dropped the
     continuous layer's own swatch. Fixed to gate on total visible-band
     count instead, matching the pre-Part-4 rule's actual intent ("a
     single band needs no legend, the title already names it").

  Both are covered as explicit regression tests
  (`test_single_categorical_band_ncol_bug_regression`,
  `test_mixed_plain_and_categorical_disambiguates_and_identifies_both`).

### `visibility_plotter.py`
- `_style_cmap_column` now styles `RadioButtonGroup` the same way as
  `Select`/`TextInput` (so `colorize_controls()`'s mode switch picks up
  the dark/light toggle for free), and no longer double-wraps the
  legend `Div`'s own HTML in a styling `<span>` (guarded by checking for
  `<div`/`<i` prefixes alongside the pre-existing `<span>` check).
- `_build_scatter_config_panel` rewritten: builds every layer's combined
  `colormap_controls()` + `colorize_controls()` column up front, adds a
  `layer_select` when there's more than one layer (client-side-only
  visibility toggle, mirroring the existing Raster/Scatter
  `kind_switch` pattern), and wires the scaling/colorize mutual
  exclusivity by finding the `RadioButtonGroup` among
  `_style_cmap_column`'s already-flattened widget list rather than
  changing `colorize_controls()`'s return contract.

## Verification status

**Executed and passing (25/25 tests, `test_colorize_by_axis_part4_export.py`):**
`panel_spec.py`'s new `ColorBand` fields, and every changed/new function
in `png_export.py` — including a full `export_png()` call with a
categorical band, a mixed plain+categorical export, and a two-plain-layer
export confirming no regression on the pre-existing path. Run with:

```
pytest cubevis/tests/manual/visplot/test_colorize_by_axis_part4_export.py -v
```

This is what surfaced both bugs above — they were not visible from
reading the code alone.

**Reviewed but not executed:** everything in `visibility_scatter.py` and
`visibility_plotter.py`. These pull in datashader, xarray, arcae/xarray-ms,
and Bokeh's real comm/`CustomJS` machinery, none of which are available
outside the real environment. All four `ScatterLayer(...)` reconstruction
sites were re-checked by hand for the same "dropped field" bug class the
two `png_export.py` bugs belong to, and `update_colorize`'s mode/axis/cmap
resolution was traced through by hand for each transition (continuous→
continuous, continuous→categorical, categorical→categorical,
categorical→continuous). Treat this as a solid starting point for review,
not as verified.

## Suggested next steps

1. Drop these four files into the real tree and run the existing
   `test_msv2_backend.py`/`test_msv4_backend.py`/
   `test_colorize_by_axis_render.py` suites to confirm nothing in Part 3's
   coverage regressed (nothing in this delivery touches `reader.py`/
   `_scatter_render.py`/`palettes.py`, so this should be a clean rerun,
   but hasn't been executed here).
2. A live smoke test against `sis14_twhya_calibrated_flagged.ms`: open
   the sidebar, switch a layer to categorical for each colorizable axis,
   confirm the legend renders and updates, toggle back to continuous and
   confirm the original cmap is restored (not a leftover categorical
   palette), and check the layer selector with a 2+ layer scatter.
3. Extend `test_colorize_by_axis_render.py` (or a new widget-level
   sibling) with real coverage for `ScatterLayer` validation,
   `update_colorize`'s transitions, and `_legend_html`'s output —
   the one gap this delivery could not close itself, for the dependency
   reasons above.
4. Not attempted: any change to `reader.py`, `_scatter_render.py`, or
   `palettes.py` — Part 3's declared scope was left untouched throughout.
