# Handoff: visplot colorize-by-axis + info-panel redesign

Written at the end of a long session covering Part 4 (colorize-by-axis
UI wiring), a CommMgr concurrency/transport fix, and Part 5 (a full
staged-colorize redesign with three rounds of live-usage bug fixes).
This document exists so a fresh chat can pick up cleanly rather than
re-deriving everything below.

**Attached alongside this document**: the current state of every file
touched this session (`visibility_plot.py`, `visibility_scatter.py`,
`visibility_raster.py`, `visibility_plotter.py`, `reader.py`,
`_scatter_render.py`, `_comm_mgr.py`, `_low_level_transport.py`,
`_cube.py`, `_image_pipe.py`), plus `COMM_CONCURRENCY_NOTES.md` and
`PART5_STAGED_COLORIZE_NOTES.md` (the detailed, chronological build log
for everything summarized here). All compile cleanly as of this
handoff. Read this document first for orientation, then dip into
`PART5_STAGED_COLORIZE_NOTES.md` for the full detail on any specific
piece.

## Where things stand, in one paragraph

Colorize-by-axis (pick a categorical axis — scan, antenna, SPW — and
color scatter points by category) is fully staged now, matching every
other gear-tab control: nothing sends live; `doPlot()` reads all the
staged widgets' current values at Plot-press time. A per-axis category
checklist lets the user exclude specific categories before rendering,
built from cheap cached metadata (`IdentityTables`), no round trip.
Backend support (`ScatterLayerSpec.excluded_categories`,
`_resolve_categories()`'s raw-value filtering) is solid and verified
against real MS data. The permanent legend (as opposed to a stale,
tab-scoped one that was Part 4's original bug) is now a real, working
piece of the UI, currently sharing a fixed-height "info strip" with
cursor-tracking below each plot. **This last part — the info strip's
location and behavior — is what the user is now asking to redesign
again**, and is where the next session should start.

## Part-by-part summary

### Part 4 (earlier this session, before compaction)
Colorize-by-axis first landed: axis picker, categorical legend,
mutual-exclusivity with continuous scaling controls, PNG export legend
support. Full detail was in the pre-compaction transcript; the
compaction summary at the top of this conversation covers it.

### CommMgr concurrency fix (also pre-compaction)
Fixed a real bug where a slow handler blocked the WebSocket's own
ping/pong loop, causing false "connection is stale" disconnects during
normal use. Non-blocking dispatch via `asyncio.create_task`, per-object
locks so related handlers serialize correctly without blocking
unrelated ones. Fully detailed in `COMM_CONCURRENCY_NOTES.md`.

### Part 5: staged colorize redesign (this session)
The core problem: Part 4's colorize controls sent live `comm.send()`
calls, breaking the "everything stages until Plot" pattern the rest of
the gear tab uses. Symptom: the legend worked live but went blank on
reopening the tab even though the plot was still genuinely categorical.

Full redesign: `colorize_controls()` holds no `Comm` reference, sends
nothing. A per-axis category checklist (`DataTable`, "build for N, ship
visible 1") lets the user exclude categories before rendering, with
state preservation (reopening the tab reflects what's actually
plotted). `doPlot()`'s JS reads all of this at Plot-press time. Backend
gained `ScatterLayerSpec.excluded_categories` (raw values, filtered
before binning). A permanent per-panel legend, decoupled from the gear
tab entirely, gets pushed through `doPlot()`'s response (this app has
no live Bokeh server, so Python-side widget mutations need explicit
client-side application — this cost real debugging time to realize).

**Full detail, including two real bugs caught during implementation
(an `Axis`-enum-keyed dict that wouldn't have serialized; a missing
`axes_changed` check that would have silently dropped colorize-only
Plot presses) is in `PART5_STAGED_COLORIZE_NOTES.md`.**

### Part 5 addendum: three rounds of live-usage bugs (this session)

Real usage of the built feature surfaced issues across three rounds of
feedback. All are detailed in `PART5_STAGED_COLORIZE_NOTES.md`'s two
addenda; summarized here:

**Round 1** — dark mode missing on checklist tables (fixed:
`_style_cmap_column` now handles `DataTable` explicitly), legend pushed
cursor-tracking off screen with a long category list (fixed: CSS
multi-column layout), no busy feedback during a slow Plot press (fixed:
button disable + cursor change + 30s safety timeout), and the first
SlickGrid crash reports.

**Round 2** — the collapsible-legend-below-cursor-tracking design still
summed to too much height even without a `Tabs` widget. Redesigned to a
fixed-height (90px) slot shared between cursor-tracking and the legend,
switched by two small toggle buttons, never both stacked.

**Round 3 (most recent, this message)** — a *third* SlickGrid crash
variant, and critically: the two-button toggle "takes up as much room
as tabs" (didn't actually solve the space problem), and clicking the
Legend button didn't appear to work. **This is the open problem the
next session should address — see below.**

## The SlickGrid saga — read this before touching `DataTable` again

Three separate, real crashes were found in this session, all the same
underlying class of bug, all traced precisely against Bokeh 3.10's
actual shipped `bokeh-tables.js` (not guessed at):

1. `"Cannot read properties of undefined (reading 'setSelectedRows')"`
   — `DataTableView.prototype.updateSelection()` calls
   `this.grid.setSelectedRows(...)` with no existence check.
2. `"SlickGrid Cannot find stylesheet."` —
   `SlickGrid.prototype.getColumnCssRules()` looks up a `<style>`
   element via a shadow-root reference that's stale on an orphaned
   view.
3. `"SlickGrid requires a valid container, undefined does not exist in
   the DOM."` — `DataTableView.prototype._render_table()` constructs
   `new SlickGrid(this.wrapper_el, ...)`, and `this.wrapper_el` is
   undefined on an orphaned view (`_after_render()`, a separate Bokeh
   lifecycle hook, calls `_render_table()` without `render()` having
   completed on that instance).

All three are the same "orphaned twin" problem already known from Part
4 (`Select`/`RadioButtonGroup` had the identical issue, fixed the same
way) — Bokeh's own no-live-server view-building process can end up
constructing two views for one model the first time it's added to a
dynamically-shown container, and the orphaned one never finishes
initializing but still receives property-change callbacks. All three
are now patched via the same `__cvInstallSelectViewGuard` mechanism
(now covering four widget types across six guarded methods total,
defined identically in two places — search
`__cvInstallSelectViewGuard` in `visibility_plotter.py`).

**Honest assessment for the next session**: three variants of the same
bug class, found one at a time across three rounds of real usage,
should be read as a signal, not just three isolated bugs. The
"pre-build N `DataTable`s per layer, most hidden via `.visible=False`"
approach in `colorize_controls()` is what's repeatedly triggering this
— it's this app's first use of `DataTable` inside a dynamically-shown
gear tab (the permanent SPW table never has this problem, since it's
never inside one). Reactive patching has worked each time a new variant
surfaced, but there's no guarantee a fourth variant won't exist
somewhere else in SlickGrid's internals. **Worth seriously considering
whether the checklist should use a different widget entirely** — a
`CheckboxGroup` (simpler internals, no SlickGrid/shadow-DOM machinery)
is the most obvious candidate, at the cost of losing `DataTable`'s
built-in scrolling for a long category list (which would need to be
handled manually, e.g. wrapping the `CheckboxGroup` in a fixed-height
scrollable `Div`/container). This wasn't attempted this session because
the guard-patching approach kept appearing to work — but "kept
appearing to work" is exactly what happened before rounds 2 and 3 too.

## The new request (this message) — what's being asked, and open questions

Verbatim requirements from the user's most recent message:

1. Configure the cursor-tracking/legend/colorbar display as a
   **permanent element within the gear tool panel** at the left (not
   below the plot).
2. Allow the user to select **one, both, or none** of these to be
   shown — implying multiple can be visible simultaneously, not
   mutually exclusive like the current toggle-button design.
3. Allow the user to **reorder** the display (colorbar/legend on top
   vs. cursor-tracking on top).
4. **Automatically add a scroll bar** if the display extends beyond the
   browser window.

### My interpretation (needs confirming with the user, not assumed)

"The gear tool panel at the left" most likely means the **permanent**
part of the left sidebar (as distinct from the **transient** gear-tab
portion that appears/disappears with the gear icon and holds
`colorize_controls()`'s own checklist) — this distinction was
established explicitly earlier in this same session (see
`PART5_STAGED_COLORIZE_NOTES.md`'s discussion of "similar to SPW... in
the permanent part of the side panel," which was about styling, not
placement, but established the vocabulary). This matters a lot:
cursor-tracking needs to be visible **whenever the user is looking at
the plot**, not just while the gear tool happens to be open — so if
"gear tool panel" meant the transient tab specifically, cursor-tracking
would disappear the moment Plot is pressed and the tab closes, which
can't be the intent. **Confirm this reading before building anything.**

If that reading is right, this is a bigger architectural move than it
first sounds: cursor-tracking/legend/(future) colorbar currently live
inside `VisibilityPlot._build()` (the shared base class for
`VisibilityRaster`/`VisibilityScatter`), i.e. they're **owned by each
panel's own layout**. Moving them into the permanent sidebar means they
become owned by `VisibilityPlotter` instead, which needs to either (a)
show one shared display that reflects whichever panel was last
hovered, or (b) show one block per panel (matching how the gear tool
already has separate "Panel A"/"Panel B" tabs). Given there are two
panels with independent hover state today, **(b) seems more consistent
with the existing architecture, but this should be confirmed, not
assumed.**

"Automatically add plot surface scroll bar" is also worth confirming
precisely — most likely this means the *sidebar itself* should scroll
if its content (now including this new info section) exceeds available
height, rather than the whole page. This app has apparently avoided
needing any scrollbar until now, so introducing one — even a scoped,
automatic one — is a real change in a previously-held constraint,
worth being deliberate about.

### What already exists that's relevant

- `VisibilityPlot._build()` in `visibility_plot.py`: the current
  cursor-tracking (`_info_div`) / legend (`_legend_content`,
  `_legend_toggle`) / toggle (`_cursor_toggle`) widgets — the pieces
  being reconsidered. `_STRIP_HEIGHT = 90` is the current fixed height.
- `VisibilityScatter._update_legend()` /`_full_legend_html()` in
  `visibility_scatter.py`: builds the legend content from real rendered
  state (categories/colors/members) — this logic (what the legend
  *contains*) is almost certainly still correct and reusable regardless
  of where the widget ends up living; it's the *placement and
  interaction model* that's being redesigned, not the content
  generation.
- `_handle_plot`'s response fields (`legend_html`, `legend_visible`) and
  `doPlot()`'s JS response handling in `visibility_plotter.py`: the
  "Python computes it, JS has to apply it explicitly" pattern (no live
  Bokeh server) will still apply to whatever the new design looks like.
- No live colorbar exists anywhere yet — only the gear tab's own
  histogram (`colormap_controls()`), which is a different, unrelated
  thing. A real colorbar for continuous layers would be new work, not
  a relocation of something that already exists.

## File map

```
cubevis/toolbox/visplot/visibility_plot.py       (shared base: figure, info strip)
cubevis/toolbox/visplot/visibility_scatter.py    (colorize_controls, legend logic, checklist)
cubevis/toolbox/visplot/visibility_raster.py     (raster panel — no colorize, shares info strip)
cubevis/toolbox/visplot/visibility_plotter.py    (sidebar, doPlot, all the JS, __cvInstallSelectViewGuard)
cubevis/toolbox/visplot/data/reader.py           (ScatterLayerSpec, colorizable_axes)
cubevis/toolbox/visplot/data/_scatter_render.py  (_resolve_categories, the actual aggregation)
cubevis/bokeh/transport/_comm_mgr.py             (concurrency fix)
cubevis/bokeh/transport/_low_level_transport.py  (concurrency + connection-closed logging fix)
```

Test MS used throughout: `sis14_twhya_calibrated_flagged.ms` (26
antennas, ALMA Band 7, 4 SPWs).

## Suggested first steps for the next session

1. Confirm the two interpretation questions above with the user before
   writing any code — the placement question in particular changes
   which Python class owns the new widgets.
2. Decide on the `DataTable` vs. `CheckboxGroup` question for the
   category checklist, given the three-strikes SlickGrid history. If
   sticking with `DataTable`, at minimum carry forward the six existing
   guards (`__cvInstallSelectViewGuard`) — don't drop them.
3. Design the "select one/both/none + reorder" mechanism for the new
   permanent panel — this is a genuinely new piece of UI, not a
   relocation of an existing checkbox/toggle.
4. Design the scoped auto-scroll behavior for the sidebar.
5. Everything about *what the legend contains* and *how excluded
   categories get communicated to the backend* should carry forward
   unchanged — that machinery is solid and tested; only the container
   it lives in and how the user selects/orders it is being redesigned.
