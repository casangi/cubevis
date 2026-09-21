# Part 5 — staged colorize-by-axis + permanent legend

Companion to `PART4_IMPLEMENTATION_NOTES.md` and `COMM_CONCURRENCY_NOTES.md`.
Covers a full redesign of Part 4's colorize-by-axis UI, triggered by a real
bug report: the legend rendered correctly while `colorize_controls()`'s
gear tab stayed open, but came back empty on reopening it later even
though the plot itself was still genuinely categorical.

## Root cause of the Part 4 bug

Every other control in the gear tab (axis pickers, Field, SPW,
Correlation, ...) is *staged* — it only takes effect when the user
presses Plot, which reads every widget's current value at that moment.
Part 4's `colorize_controls()` broke that pattern: it sent a live
`comm.send()` on every change, re-rendering immediately. That produced a
one-off response applied to a `Div` that lived only inside the gear tab —
nothing about reopening the tab later replayed that response, so it
started blank regardless of the plot's actual state.

## The redesign, in one paragraph

Colorize becomes staged, like everything else. `colorize_controls()`
holds no `Comm` reference and sends nothing; its widgets just track their
own current value. A new per-axis category checklist (one `DataTable` per
colorizable axis, "build for N, ship visible 1", populated from cheap,
already-cached `IdentityTables` metadata — no round trip) lets the user
pick which categories to exclude before ever rendering. `doPlot()`'s own
payload-building JS reads all of this — mode, axis, checklist selections —
at Plot-press time, the same way it already reads `sx_sel.value`. The
actual categorical render, and the real category-to-color assignment the
legend needs, happens exactly once, server-side, when Plot is pressed. A
new, separate, *permanent* legend — attached to the plot itself, not the
gear tab — is rebuilt from the real rendered state after every render and
pushed to the browser in `doPlot()`'s own response, so it can never go
stale independent of what the gear tab is doing.

## What changed, by file

### `reader.py` / `_scatter_render.py` (backend)
- `ScatterLayerSpec` gained `excluded_categories: tuple[str, ...] = ()`,
  validated the same way `colorize_axis` is (rejected on a continuous
  layer).
- `_resolve_categories()` filters on **raw values, before binning** —
  not post-binning display labels. This went through one real revision
  during implementation: it was first built against display labels,
  then corrected once it became clear the checklist (enumerated from
  `IdentityTables`) only ever knows individual real values, never how a
  given render will eventually bucket them.
- Verified against real MS data: excluding specific antennas removes
  exactly those from the render; excluding everything produces a clean
  `skip_reason` instead of crashing.

### `visibility_scatter.py`
- `ScatterLayer` gained the matching `excluded_categories` field,
  threaded through every reconstruction site (`set_alpha`,
  `update_scaling`, `update_colorize`, `_with_default_cmaps`,
  `_handle_update_axes_scatter`) and into `_render_all_layers`'s
  `ScatterLayerSpec` construction.
- `update_colorize()` gained an `excluded_categories` parameter. Kept as
  a still-useful, directly-callable method — only its live comm trigger
  was removed.
- Removed the dead live-send mechanism entirely: `_msg_colorize`, its
  comm registration, and `_handle_colorize`.
- **`colorize_controls()` fully rewritten**: no `Comm` reference, sends
  nothing. New `_colorize_category_values()` helper enumerates raw
  categories per axis from `_ensure_identity_tables()`. One `DataTable`
  checklist pre-built per colorizable axis, toggled client-side on both
  mode and axis changes. State preservation: the axis currently in
  effect starts with its real `excluded_categories` unchecked; any other
  axis starts fully checked. Returns `(controls, handles_dict)` instead
  of just `controls` — a breaking signature change, propagated to its
  one caller.
  - **Bug caught before it went further**: the returned `checklists`
    dict was initially keyed by `Axis` enum members. Once this needed to
    cross into a `CustomJS` args dict (for `doPlot()` to read), that
    would have failed — Bokeh needs JSON-compatible keys, and an Enum
    member is neither a Bokeh Model nor a JSON primitive. Fixed to key
    by `axis.name` string, matching what the internal toggle JS already
    needed anyway.
  - Known, deliberate styling gap: each checklist's `DataTable` uses
    Bokeh's own defaults rather than the sidebar's bespoke dark/light
    table CSS (`visibility_plotter.py`'s `_DARK_TABLE_CSS`/
    `_LIGHT_TABLE_CSS`, used by the permanent SPW table). Fixing this
    needs either a new constructor parameter threaded from
    `_build_scatter_config_panel` or an upward import from the widget
    layer into the app layer — flagged as a follow-up, not fixed here.
- New `_full_legend_html()` — combines every categorical layer's legend
  into one HTML block, prefixing with the layer's own label only when
  more than one layer is categorical at once (the same rule already
  used in `png_export.py`'s static export legend).
- New `_update_legend()` — pushes `_full_legend_html()`'s result to the
  permanent legend widgets (see `visibility_plot.py` below). Called from
  `_rerender()`, **not** from inside `_render_all_layers()` — that
  method is also called by `_shade_for_export()`, whose whole point is
  rendering at a different viewport without disturbing live state, and
  the legend widget is a live Bokeh UI object that was never part of
  that method's save/restore tuple. Verified: an export at a different
  viewport does not touch the live legend at all.
  - A second real ordering bug caught here: `_build()`'s very first
    action is calling `_render()` (hence `_rerender()`), *before* the
    legend widgets are constructed later in the same method. Fixed with
    a `getattr(self, "_legend_content", None)` guard in
    `_update_legend()` rather than a plain attribute check, so the very
    first render degrades safely instead of raising `AttributeError`.

### `visibility_plot.py` (shared base class)
- Added the permanent legend's widgets to `_build()`'s shared layout,
  used by both raster and scatter: `_legend_toggle` (a small `Button`,
  collapsed by default), `_legend_content` (a `Div`, hidden until there's
  something to show), `_legend_wrapper` (the column holding both,
  `visible=False` until populated). A tiny `CustomJS` toggles
  `_legend_content.visible` on click. Raster panels get the same three
  widgets — they simply never receive content, since only
  `VisibilityScatter` calls `_update_legend()`.

### `visibility_plotter.py`
- `_build_scatter_config_panel` updated for `colorize_controls()`'s new
  `(controls, handles)` return signature; `colorize_handles` (one entry
  per layer) stored alongside `x_sel`/`y_sel`/etc. in the panel's
  widgets dict.
- `_make_scatter_layers()` gained a `colorize_overrides` parameter — one
  entry per polarization, either `None` (continuous) or a dict matching
  exactly what the checklist/JS produces. A categorical override gets a
  fresh categorical cmap in place of the continuous cycle, mirroring
  `update_colorize()`'s own cmap-swap convention.
- `_handle_plot`'s scatter branch: extracts `panel_msg.get("colorize")`,
  folds a colorize-state comparison into the existing `axes_changed`
  check (**a real gap, not just a nice-to-have** — without this, a
  Plot press where *only* colorize/exclusion changed would have been
  silently skipped entirely, since x/y/pols/selection would all still
  match), and passes the overrides through to `_make_scatter_layers`.
- `_handle_plot`'s scatter response gained `legend_html`/
  `legend_visible` fields, always sent (same reasoning as `image`).
  **This was a necessary addition, not optional polish**: this app has
  no live Bokeh server, so Python setting
  `panel._legend_content.text`/`panel._legend_wrapper.visible` (which
  happens automatically inside `update_axes()` → ... → `_update_legend()`)
  does nothing in the browser on its own — it has to be read back out
  and applied client-side, exactly like `image_source.data` already is.
  Realizing this before shipping avoided landing a legend that updated
  correctly in Python and never appeared in the browser at all.
- `_plot_js_args` gained `panel{0,1}_colorize_handles` and
  `panel{0,1}_scatter_legend_{wrapper,content,toggle}`.
- `buildPanelPayload`'s scatter branch (JS) now includes a `colorize`
  array, built by a new `buildColorizeArray()` reading each layer's
  staged `mode_group`/`axis_select`/checklist state.
- `doPlot()`'s response handler applies `legend_html`/`legend_visible`
  to the correct panel's legend widgets, mirroring how it already
  applies `image`/`state`.

## Verification status

**Executed and passing, against real MS data end to end:**
1. Backend `excluded_categories`, raw-value semantics, against real data.
2. `colorize_controls()`'s checklist construction, enumeration from real
   `IdentityTables`, and state preservation on reopening (both the
   currently-active axis reflecting real excluded categories, and an
   inactive axis starting fully checked) — re-verified after the
   Enum-key fix.
3. The full nested `colorize_handles` JS structure (list of dicts
   containing string-keyed dicts of model tuples) serializes cleanly
   through real `Document.validate()`/`json_item()`.
4. `_make_scatter_layers()` with `colorize_overrides` → `update_axes()`
   → a real mixed categorical/continuous render in one call, exclusions
   correctly applied — this is the actual path `_handle_plot` uses,
   exercised directly (by extracting and running the real function body
   against the real backend, working around this sandbox's inability to
   import the full, very heavy `visibility_plotter.py` module directly).
5. The permanent legend: hidden and empty initially; populates with
   real antenna names after a real categorical render; correctly hides
   and resets (`visible=False`, empty text, collapsed toggle label) on
   switching back to continuous; correctly combines multiple
   categorical layers with disambiguating label headers; **confirmed
   untouched by `_shade_for_export()`** at a different viewport.
6. The legend widgets (`_legend_wrapper`/`_legend_content`/
   `_legend_toggle`) serialize correctly through real Bokeh alongside
   the rest of a panel's `CustomJS` args, with real legend HTML content
   verified present in the serialized structure.

**Reviewed but not executed:**
- `doPlot()`'s JS itself (`buildColorizeArray`, the response handler's
  legend-application block) — verified to *serialize* correctly, and
  traced by hand against the exact data shapes involved, but its
  execution logic has not run in a real browser. Same limitation as
  every other piece of hand-written JS in this whole project — no
  browser available in this sandbox.
- `_handle_plot`'s `axes_changed`/`colorize_changed` comparison logic —
  reviewed carefully (including the deliberate choice to only compare
  when `pols == current_pols`, since a pols mismatch already forces
  `axes_changed` via a fresh `_make_scatter_layers()` call), but not
  exercised against a live multi-panel `VisibilityPlotter` instance —
  same gap noted in the Part 4/comm-concurrency notes for that class of
  test.

## Suggested next steps

1. A live smoke test in the real app: open the gear tab, switch a layer
   to categorical, uncheck a few categories, press Plot, confirm the
   permanent legend appears below the plot (collapsed by default,
   expands on click) and matches what's actually rendered. Reopen the
   gear tab and confirm the checklist reflects the real excluded state.
   Switch back to continuous and confirm the legend collapses and
   clears.
2. The checklist `DataTable` dark/light theme styling gap noted above.
3. Consider whether the checklist's numeric-scan-number sort (a small
   local `_sort_key` in `_colorize_category_values`, not the backend's
   own `_category_sort_key`) is worth unifying with the backend's — a
   minor cosmetic-only inconsistency, not a correctness issue.

## Addendum: live feedback from the first real usage pass

Real usage of the checklist surfaced three bugs and two UX gaps, all
addressed:

**SlickGrid crashes when checking a category box — root-caused against
Bokeh's actual shipped JS, not guessed at.** Two errors:
`"Cannot read properties of undefined (reading 'setSelectedRows')"` and
`"SlickGrid Cannot find stylesheet."`. Both are the same orphaned-twin
problem already fixed for `Select`/`RadioButtonGroup` (Part 4), now
hitting `DataTable` — this app's first `DataTable` living inside a
dynamically-added gear tab (the permanent SPW table never has this
problem, since it's never inside one).

- `DataTableView.prototype.updateSelection()` (confirmed in Bokeh
  3.10's `bokeh-tables.js`) ends with `this.grid.setSelectedRows(...)`
  with no existence check. An orphaned twin's `render()` — where
  `this.grid = new SlickGrid(...)` happens — never runs, so a
  `ColumnDataSource.selected` change (checking a box) tries to sync a
  grid that doesn't exist.
- `SlickGrid.prototype.getColumnCssRules()` looks up a `<style>`
  element it injected via `(this._options.shadowRoot || document)`.
  An orphaned twin's shadow root gets discarded once the real view
  takes over; something (a layout/resize pass) still runs this lookup
  against the stale reference and throws.

Fixed by extending the same `__cvInstallSelectViewGuard` mechanism
(now four widget types, two independent flags added) — guard
`updateSelection` on `this.grid`, and wrap `getColumnCssRules` to
return a harmless empty rule object instead of throwing (frozen-column
border styling, which this app's tables never use anyway).

**Checklist tables didn't follow dark/light theme.** Previously flagged
as a known, deliberate gap; now actually fixed. `_style_cmap_column`
handles `DataTable` explicitly, applying the same
`[dark_stylesheet, self._table_css_dark]` pairing the permanent SPW
table already uses, and returns a new `styled_tables` list. Threaded
through the per-layer loop, the raster panel's widgets dict (empty list,
for a uniform key across both panel kinds), the cross-panel flattening
loop, and a new `_step('colorize tables', ...)` block in the theme
toggle mirroring the SPW table's own step exactly.

**Legend pushed the cursor-tracking status bar off screen for a long
category list.** `_legend_html()`'s swatch wrapper switched from a
single scrolling column (`max-height`/`overflow-y`) to CSS
`column-width` (not a fixed count, so it adapts to whatever width is
actually available) with `break-inside: avoid` per row. Removed the
now-redundant inner scroll region, since the outer `_legend_content`
Div (`visibility_plot.py`) already provides one — two independent
scrollbars nested inside each other was the wrong shape regardless of
the column count.

**No feedback during a slow categorical Plot press.** `doPlot()` now
disables both Plot and Reload and sets a busy cursor immediately before
sending, restored at the top of the response handler — plus a 30s
safety timeout that does the same reset unconditionally, so a dropped
connection mid-request (exactly the class of issue this session's
transport fixes addressed) can't leave the UI permanently stuck.

### Verification status (this addendum)

- The `DataTable` dark-styling fix verified in isolation: extracted and
  ran `_style_cmap_column`'s actual logic against a real `DataTable`,
  confirmed it receives exactly `[dark_stylesheet, self._table_css_dark]`.
- The multi-column legend verified against real categorical render
  output: confirmed `column-width`/`break-inside:avoid` present and the
  old single-column wrapper gone.
- **Not verified**: the SlickGrid guard extension and the busy-cursor
  JS — both are hand-written/hand-traced against Bokeh's actual shipped
  source (not guessed at), but neither has run in a real browser. This
  is the same limitation noted for every other piece of JS in this
  project; the SlickGrid diagnosis in particular would benefit from
  confirmation against the real reported repro before being considered
  fully closed.

### Open design question, not implemented: tabbed vs. collapsible area beneath the plot

Raised: should cursor-tracking, a (currently nonexistent) live colorbar,
and the categorical legend share tabbed space to save screen real
estate? My recommendation, reasoned through but not built:

- **Keep cursor-tracking as its own always-visible strip**, not behind a
  tab — it's the one piece of this trio that changes continuously while
  actively exploring a plot (every mouse move), and hiding it behind a
  tab switch would fight against how it's actually used.
- **A live colorbar and the categorical legend, by contrast, are both
  static per-render context** (set once per Plot press, not per mouse
  move) and — as you suspected — are genuinely mutually exclusive for
  any given layer (continuous wants a colorbar, categorical wants a
  legend). That's exactly what the collapsible legend area already
  built this session is suited for: no tab navigation needed at all,
  since "whichever is relevant" can simply be whatever content gets
  pushed into that one collapsible area, the same way the legend already
  works.
- Given the SlickGrid bugs above are a very concrete demonstration that
  *any* dynamically-shown content in this app carries real orphaned-view
  risk, I'd actively avoid introducing a new `Tabs` widget for this
  specific spot unless there's a reason the collapsible-area approach
  can't cover it — not because tabs are wrong in general, but because
  this particular codebase's relationship with dynamically-shown Bokeh
  widgets has now cost real debugging time twice.
- No live colorbar exists in the browser today (only the gear tab's own
  histogram, which is a different thing) — so this is really "design the
  future colorbar to land in the same collapsible slot the legend already
  occupies" rather than "restructure something that already exists."

## Addendum 2: the collapsible legend still summed to too much height

Real feedback: even without a `Tabs` widget, the collapsible legend
(stacked *below* an always-visible cursor-tracking strip, adding height
when expanded) still pushed the status line off a laptop screen —
`_info_div` + an independently-growing `_legend_content` can together
exceed available height even though each piece looks fine in isolation.
This app has otherwise avoided ever needing a page-level scrollbar, so
that mattered.

**Redesign**: cursor-tracking and the legend are now mutually exclusive
within *one fixed-height slot* (90px, `_STRIP_HEIGHT` in
`visibility_plot.py`), switched by two small buttons ("Cursor"/
"Legend") — the same behavior originally proposed as tabs, kept, but
implemented with the same plain `Button` + `.visible` pattern already
used successfully elsewhere in this session, not Bokeh's `Tabs` model
(deliberately — see Addendum 1's reasoning about avoiding new
dynamically-shown Bokeh widgets in this app right after the SlickGrid
bugs). `_legend_wrapper` is gone entirely; `_cursor_toggle` is new.

- The "Legend" button only appears when there's content
  (`_legend_toggle.visible`, mirroring the old wrapper's role) — a
  continuous-only panel looks exactly as it did before this whole
  feature existed.
- Becoming categorical does **not** steal the active view from
  cursor-tracking — the button appears, but cursor-tracking (more
  likely to be what's actively in use, since it updates on every hover)
  stays showing until the user explicitly clicks "Legend".
- Losing the legend's content while it *was* the active view (switched
  back to continuous) **does** force the view back to cursor-tracking —
  otherwise the user would be looking at a blank pane with no visible
  way back.
- Total layout height is now fixed and predictable regardless of how
  much legend content there is: figure + a small toggle-button row +
  exactly one 90px slot, never both stacked.

This touched three files: `visibility_plot.py` (the widgets themselves),
`visibility_scatter.py` (`_update_legend()`'s toggle-only-not-content
logic), and `visibility_plotter.py` (`_plot_js_args`'s widget references
and `doPlot()`'s response-handling JS, both updated from the old
wrapper-based fields to the new toggle-visibility + forced-switchback
fields).

**Verified against real data**: fresh layer starts with cursor-tracking
active and the Legend button hidden; a real categorical render makes
the button appear *without* switching the active view; simulating a
user click to the legend view, then switching back to continuous,
correctly forces the view back to cursor-tracking with both button
states updated. **Not verified**: the actual button click JS and the
`doPlot()` response-handling JS additions — same browser-less
limitation as everything else in this project.
