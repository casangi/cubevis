# visplot colorize-by-axis — Part 4d: info blocks, checkbox checklists, busy overlay

| | |
|---|---|
| **Document** | `visplot-colorize-by-axis-notes-part4d.md` (identify it by this title if the file is renamed) |
| **Written** | 2026-09-21, updated 2026-09-22 (two addenda) |
| **Ships in** | `visplot-colorize-by-axis-part4d.zip` |
| **Follows** | `visplot-colorize-by-axis-handoff-part4b.md` (the "HANDOFF" written at the end of the Part 5 session) |
| **Earlier names** | `PART5_STAGED_COLORIZE_NOTES.md` = `visplot-colorize-by-axis-notes-part4b.md`; `COMM_CONCURRENCY_NOTES.md` = `visplot-colorize-by-axis-notes-part4c.md` |

## Vocabulary (as used in this session)

* **configuration panel** — the whole left-hand area (`VisibilityPlotter._sidebar_col`).
* **gear tool tab** — the *transient* tab a plot's gear icon opens inside the
  configuration panel. There is one per (slot, kind): A-raster, A-scatter,
  B-raster, B-scatter.
* **info display** — cursor readout / legend / colorbar for one panel.

## What changed (original pass)

| Request | Now |
|---|---|
| Select one, both or none of cursor/legend/colorbar | Per-panel selectors (redesigned again in Addendum 2 below) |
| Reorder | A rotate control (redesigned in Addendum 2) |
| Scrollbar when taller than the window | Reverted in Addendum 2 — see below |
| Checkboxes for every categorical axis | The per-axis colorize checklists are `CheckboxGroup`s (scrolling wrapper + All/None buttons), not `DataTable`s. Raw values ride in `tags`; `doPlot()` reads `active`/`tags`. No SlickGrid left in any gear tab. |
| Busy cursor lost over Tabs etc. | `cvSetBusy(on)`: a top-most transparent overlay with `cursor: progress` (plus the body cursor for the margin), one stored give-up timer. |
| (new) colorbar | HTML gradient bar(s) from the same `ColorBand`/`ScalarMapping` the PNG export uses. Scatter bars are labelled "Amplitude XX: Density (eq_hist)"; the layer is named whenever the panel has more than one layer. |

## Addendum 1: reverted the sidebar move

An earlier version of this pass moved cursor tracking / legend / colorbar into
`VisibilityPlotter`'s permanent sidebar, one block per (slot, kind). **That was
reverted** after live review, for three reasons:

* Doesn't scale past two panels.
* Separates the colorbar/cursor readout from the plot they describe — requires
  knowing which panel is "Panel A" despite panels being swappable.
* The sidebar isn't wide enough for a comfortably-read colorbar.

Also reported at the time: six `WARNING:bokeh.core.validation.check:W-1005
(FIXED_SIZING_MODE)` console lines at construction/embed time. Root cause
confirmed directly: the reverted sidebar-block code set `sizing_mode="fixed"`
while explicitly nulling `height` on the cursor/legend/colorbar Divs — exactly
what Bokeh's validator flags. Gone as a side effect of the revert; a test
(`test_document_has_no_fixed_sizing_mode_validation_warnings`) pins it directly
by capturing Bokeh's own validation logger.

What came back with the revert: cursor readout / legend / colorbar under each
panel's own figure (`VisibilityPlot._build()`), in that panel's own layout —
never moved, never shared, no per-(slot,kind) registry needed in
`VisibilityPlotter` (the sidebar-block machinery, `_info_blocks`,
`_sync_info_blocks()`, `detach_info()`, was deleted, not just unused).

## Addendum 2: live-review fixes — Cancel/✕, cursor-height regression,
## colorbar width, sidebar gap, and a checklist+rotate redesign of the selectors

Feedback from an actual browser session surfaced four bugs and one design
change to the info display from Addendum 1. All are in this zip.

### 1. Cancel button → "✕"

Cosmetic, on request: the gear tab's Cancel button is now labelled "✕"
(echoing a window's own close control) instead of "Cancel". The tooltip was
also corrected while touching this: `cancel_click_js` only restores the
edited figure title and closes the tab — it never discarded any staged
widget value — so "Discard changes..." was always inaccurate. Now: "Close
this panel's configuration tab".

### 2. Cursor readout pushing the status bar off screen — a real regression, found and fixed

Addendum 1's rewrite of `VisibilityPlot._build()` dropped the ORIGINAL
design's explicit height cap on the cursor-readout Div (the pre-session code
always had `self._info_div.height = _STRIP_HEIGHT` with `overflow-y: auto`).
Combined with a generous, effectively-uncapped-in-practice 40vh outer
wrapper, a long hover readout (many scan numbers / baselines, wrapping to
3-4 lines) could grow the panel tall enough to push the page's status bar out
of the viewport — exactly as reported.

Fixed by giving cursor/legend/colorbar each their OWN small fixed height
again (60px / 90px / 100px — see `info_panel.ITEM_HEIGHTS`), each with its
own small internal `overflow-y: auto` (the same bounded-box pattern the
original cursor strip always used), and removing the 40vh outer wrapper
entirely. Total panel height is now bounded by construction (sum of
whichever items are checked), not by hoping a viewport percentage fits.
Concretely: with everything checked, worst case is figure height + 160px
(raster, cursor+colorbar) or + 250px (scatter, all three) — taller than the
pre-session baseline of figure + 90px, but now opt-in and bounded, where
before it was open-ended. If figure(550) + 250 = 800px doesn't comfortably
fit your screen with everything checked, unchecking Legend/Colorbar by
default, or lowering `ITEM_HEIGHTS`, are both easy follow-ups.

### 3. Colorbar width not matching the plot — best-effort fix, **unverified**

No code path was found that treats the colorbar Div differently from cursor
or legend (all three used the identical `sizing_mode="stretch_width"`
pattern), so the leading theory is that it was the 40vh wrapper's own
`overflow-y: auto` interacting with Bokeh's flex width-stretch — a
documented-ish CSS quirk where `overflow` on a flex item can affect its
cross-axis sizing in some browsers — and that this affected all three
Divs, but was only visually obvious on the colorbar's high-contrast
gradient bar (the cursor/legend Divs' plain backgrounds would show the same
narrowing far less noticeably). Removing that wrapper (done for #2 above)
should fix this as a side effect. Also removed, defensively: the explicit
`width=self._width` numeric hint on all three Divs (now unset, relying
purely on `sizing_mode="stretch_width"`), in case that pixel value was
itself going stale relative to a figure resized by a later layout-mode
switch. **Neither explanation is confirmed** — there was no code path that
obviously treated the colorbar differently, so this is inference from the
CSS mechanism most likely to produce a WIDTH-only symptom, not a traced
root cause. Please confirm with a fresh screenshot.

### 4. Sidebar not reaching the status bar (blank gap) — reasoned fix, **unverified**

Root cause found directly: the sidebar's `max-height` was a hardcoded
`f"{_PANEL_HEIGHT + 60}px"` (610px) constant that never accounted for the
plot column growing taller (exactly the panels described in #2/#3). Once
the plot column exceeded 610px, the sidebar — capped at 610px — fell
visibly short of it. Fixed by replacing the fixed cap with
`"align-self": "stretch", "height": "100%"`, letting the sidebar track
whatever height the plot-area column actually ends up at (the row's default
flex cross-axis stretch), with `overflow-y: auto` still present as a
fallback if the sidebar's OWN content (an open gear tab) is ever taller
than that. Bokeh's actual default `align-items` for its row layout wasn't
confirmed in source, hence the explicit `align-self` rather than relying on
an assumed default.

### 5. Info-display selectors redesigned: checklist + rotate, replacing the radios

Per your description: an "All" `Checkbox`, a `CheckboxGroup` with one entry
per available item ("Cursor tracking" / "Legend" — scatter only — /
"Colorbar"), and one small rotate button ("↻") that cycles display order by
one step per click (reading each Div's current CSS `order` and computing
the next permutation — no separate order model needed). Checking "All"
checks every item; unchecking it unchecks every item; checking/unchecking
an individual item updates "All" to reflect whether all are currently
checked. The two directions are guarded against bouncing off each other by
a `window.__cvInfoSyncBusy` flag namespaced per `(slot, kind)` (`sync_id`),
the same "global, namespaced flag" pattern this app already uses elsewhere
(`cvSetBusy`, `__cvInstallSelectViewGuard`).

This also **removed the mutual exclusivity** between legend and colorbar
(the old Auto/Legend/Colorbar/None radio): independent checkboxes let both
show simultaneously, which is actually correct for the mixed-layer case
(one categorical layer, one continuous) — the old radio could never show
both at once even though that's a legitimate state. A raster's checklist
has no "Legend" entry at all (rather than an always-inert one), since
raster panels never produce legend content.

`info_panel.py`'s public surface changed accordingly:
- Removed: `KEY_MODE_LABELS`, `ORDER_LABELS`, `resolve_key`,
  `info_wrapper_styles`; the old `InfoSelectors` fields
  (`show_cursor`/`key_mode`/`order`); `wire_info_display`/
  `apply_info_defaults`'s old three-positional-Div signatures.
- Added: `ITEM_LABELS`, `ITEM_KEYS`, `ITEM_HEIGHTS`; the new
  `InfoSelectors` fields (`all_cb`/`items`/`rotate_btn`/`item_keys`);
  `build_info_selectors(kind, sync_id, *, width, stylesheets=None)` (note
  the new required `sync_id`); `wire_info_display(divs, sel)` /
  `apply_info_defaults(divs, sel)`, now taking a `{key: Div}` dict rather
  than three positional cursor/legend/colorbar arguments; `ROTATE_JS`.

### Verified this round

- Real MS build + `file_html()` embed: zero `FIXED_SIZING_MODE` warnings
  (direct regression test), close button confirmed present in the
  serialized page, "Cancel" confirmed absent, sidebar's `max-height`
  confirmed absent / `align-self`+`height` confirmed present.
- Node-level tests (against the shipped strings directly) for
  `INFO_APPLY_JS`, `ROTATE_JS`, and the All↔checklist sync, including:
  re-entrancy actually blocked (simulated nested call), two panels' sync
  guards confirmed independent (namespacing works), a full N=2 and N=3
  rotation cycle returns to the original order, and JS/Python parity for
  visibility over the whole (item_keys × checked-combination ×
  legend-text × colorbar-text) state space for both kinds.
- All 59 new/updated tests pass; existing scatter/raster/colorize suites
  give **identical failure sets** to the unmodified code (same 11
  pre-existing async-handler failures, same 4 raster/colorize failures);
  all 212 non-MS existing tests pass; all 174 `CustomJS` bodies in the
  built document parse under node.

### Not verified — needs a browser

Items 3 and 4 above (width fix, sidebar stretch) are reasoned from code
inspection, not confirmed visually. The checklist/rotate UI's actual
appearance and click behaviour (only the underlying JS logic was tested,
not real DOM interaction). Whether the new worst-case panel heights
(figure + 160–250px) comfortably fit your screen with everything checked.

## Manual browser checklist

1. Load; confirm each panel shows a cursor readout and (if continuous) a
   colorbar directly under its own figure, and that the colorbar's gradient
   bar spans the full width of the plot above it.
2. Confirm the sidebar's bottom edge reaches the status bar with no gap.
3. Open a gear tab → find "Info display": an "All" checkbox, a checklist
   (Cursor tracking / Legend / Colorbar), a "↻" button. Uncheck "All" — all
   three hide. Check one item individually — "All" should NOT be checked
   (since not all are). Check the remaining items — "All" becomes checked.
4. Click "↻" a few times; confirm the three items' vertical order rotates
   (bottom → top) each click.
5. Colorize a scatter layer categorically (legend) while the sibling layer
   stays continuous (colorbar) — confirm BOTH the legend and the colorbar
   can be checked/shown simultaneously (this was impossible under the old
   radio).
6. Click the "✕" button on an open gear tab; confirm the tab closes and any
   title edit is discarded, same as "Cancel" did before.
7. Hover the scatter plot somewhere with many scans/baselines selected;
   confirm the cursor-readout box does NOT grow past its fixed height (it
   should show a small internal scrollbar instead) and the status bar stays
   in place.
8. Resize the browser window narrower/shorter; re-check 1–2.
9. Toggle light/dark with a gear tab open.

## Known limitations

* In "local" colour mode the raster/scatter colorbar tracks the last Plot /
  scaling change, not pan/zoom.
* With "All" checked on a scatter panel with a long legend, worst-case panel
  height is figure + 250px — noticeably taller than the pre-session 90px
  baseline. This is opt-in (uncheck items you don't need) but the *default*
  is "All checked", so a first-run scatter panel with real categorical data
  will be taller than before by default.

## File map (paths relative to the package root)

```
cubevis/toolbox/visplot/info_panel.py           blocks, selectors, INFO_APPLY_JS, ROTATE_JS, colorbar_html()
cubevis/toolbox/visplot/visibility_plot.py      info widgets built here, per-item fixed heights
cubevis/toolbox/visplot/visibility_scatter.py   CheckboxGroup checklists; _update_legend(); colorbar in responses
cubevis/toolbox/visplot/visibility_raster.py    colorbar in responses
cubevis/toolbox/visplot/visibility_plotter.py   selector wiring; cvSetBusy; CheckboxGroup guard; ✕ button; sidebar stretch
cubevis/bokeh/transport/_comm_mgr.py            warning on abrupt disconnect with no timeout
cubevis/tests/manual/visplot/test_info_panel.py             pure + node parity + rotate/sync JS checks
cubevis/tests/manual/visplot/test_checkbox_guard.py         node, model of Bokeh's view
cubevis/tests/manual/visplot/test_info_block_integration.py needs MS=<path>.ms
cubevis/tests/manual/visplot/test_reconnection.py           +3 tests (warning)
patches/*.diff                                        vs the project-knowledge copies (patch -p1)
```

## Addendum 3 (same day): sidebar scroll clarified, rotate now reorders the
## checklist too, checklist is now context-sensitive, colorbar width
## attempt #2, and the close button moved + shrunk

Your "visible area only" comment turned out to be about the sidebar's own
scroll behaviour specifically, not the per-panel info display — Addendum
2's sidebar fix (`align-self: stretch`) had it backwards. Sorted out below,
along with four other items from this round's screenshots.

### 1. Sidebar scrollbar — reverted the Addendum-2 "fix", restored its own bound

What actually happened: making the sidebar *stretch to match the plot
column's height* meant that whenever a gear tab's own content was tall
(now including the info-display checklist), the sidebar grew right along
with it — pushing the **whole page** taller and forcing the **browser's
own page-level scrollbar** to reach the status bar. That's the scrollbar
you meant to rule out, visible at the far right of the browser window in
your first screenshot, and it's exactly what this app has always avoided.

Reverted to the sidebar having its **own** bound, independent of the plot
column: `max-height: min(610px, calc(100vh - 16px))` with its own
`overflow-y: auto`. This is closer to what the sidebar had before this
whole info-display round (a `_PANEL_HEIGHT`-derived constant), but now
capped by the viewport too, so a short window degrades to the sidebar's
own internal scrollbar rather than either overflowing silently (the
original "blank rectangle" bug from Addendum 1) or forcing a page scroll
(Addendum 2's mistake).

### 2. Rotate now reorders the checklist too

The rotate button already reordered the Divs under the plot; now it also
reorders the checklist's own rows to match, so "Legend" (say) appears at
the top of the checklist exactly when it's at the top under the plot.
This needed real care: a `CheckboxGroup`'s `active` is index-based, so
reordering `labels` without correctly remapping `active` would silently
check the wrong rows. The fix tracks checked state by **key**, not
position, through the reorder — verified directly (a checklist with only
"Legend" checked stays showing only Legend, at whatever new row position
it lands, after rotating).

### 3. Checklist is now context-sensitive

"(colorbar and tracking for raster), (colorbar and tracking for
Continuous), (colorbar, tracking and legend for Categorical)" — done. The
checklist now shows exactly two rows (Cursor tracking, Colorbar) until the
panel actually has legend content, at which point "Legend" appears as a
third row, checked by default; it disappears again the moment the panel
goes back to continuous. This is driven by the same "does the Legend Div
have text" check the visibility logic already used, so no new signal was
needed — just reacting to it in one more place. A raster's checklist
still never grows a "Legend" row at all (raster panels never produce
legend content).

Where a newly-appearing "Legend" row lands, order-wise: appended after
whatever already has an explicit position, so it shows up at the bottom
rather than jumping to the top and disrupting a rotation you'd already
done. Verified directly (rotate once, let Legend appear, confirm the
already-rotated cursor/colorbar order is undisturbed and Legend lands
after both).

Found and fixed along the way: the Python-side twin of this logic
(`apply_info_defaults`) had a real bug where a Div that became
*inapplicable* was never explicitly hidden — it just kept Bokeh's own
default `visible=True`, which happened not to matter in the live app
(the real Divs are always constructed with `visible=False` already) but
would have been wrong in any context that didn't set that explicitly.
Fixed to match the client-side script's own explicit-hide step.

### 4. Colorbar width — second attempt

The first attempt (removing an *outer* wrapper's `overflow-y: auto`)
didn't fix it, per your screenshot. On reflection that's not surprising:
each item still has its **own** `overflow-y: auto` (needed for the
bounded-height fix), so if that CSS property really is the mechanism,
removing it from one place while it remained on another was never going
to help. Added, directly on cursor/legend/colorbar: explicit
`width: 100%` and `box-sizing: border-box` — a more forceful, CSS-level
instruction than `sizing_mode="stretch_width"` alone, which should hold
regardless of whether the `overflow` theory is exactly right. **Still not
confirmed** — there is no code path found that treats the colorbar
differently from the other two, so this remains inference toward the
most likely CSS mechanism, not a traced root cause. Please check again;
if this still doesn't fix it, the next step would be inspecting the
actual rendered DOM/computed styles in a browser's dev tools rather than
guessing further from source.

### 5. Close button: padding and position

(a) The oversized border was Bokeh's own default button padding, sized
for text labels, around a single glyph. This codebase already has a
compact icon-button stylesheet (`_icon_btn_css`) built for exactly this,
used by the existing Field/SPW prev/next buttons — reused it here rather
than inventing new CSS, so it now matches those buttons' look exactly
(24x24, no default padding).

(b) Moved onto the same row as Raster/Scatter, pushed to the right edge
via `justify-content: space-between` (no extra Spacer widget needed).
That row was previously split into two specifically because
`120 + 80 + 60 = 260px` (the width of Raster/Scatter + the old "Cancel" +
Swap) exactly filled `_SIDEBAR_WIDTH` with nothing left for
borders/padding; with the close button now icon-sized, the combined width
is comfortably smaller, so Swap gets its own row alone below rather than
crowding back in — you only asked for the close button to move, so Swap
stayed put.

### Verified this round

- Node-level: 29 checks covering the dynamic checklist (legend
  appearing/disappearing/reappearing — including that it comes back
  *checked* rather than lingering in whatever unchecked state it had
  before disappearing), rotate reordering both the Divs and the
  checklist together (by key identity, not position), a 2-item
  (raster-shaped) rotate cycle, and the order-append-at-the-bottom
  behaviour when Legend first appears after a rotation.
- Python: a real construction + a real categorical `_handle_plot` press
  + a real reversion to continuous, confirming the checklist narrows to
  2 items, widens to 3, and narrows back to 2 — using real legend HTML
  from a real colorize call, not a synthetic stand-in.
- Direct inspection of a real built `VisibilityPlotter`: sidebar's
  `max-height` is viewport-relative (not `align-self`/`height:100%`);
  all four panels' checklists start at 2 items (this MS subset's default
  view is continuous); the close button sits in the kind-switch row,
  24x24, with the shared icon stylesheet; every item Div carries
  `width:100%`/`box-sizing:border-box`; cursor/colorbar carry initial
  order 1/2.
- All 65 new/updated tests pass; existing scatter/raster/colorize suites
  give **identical failure sets** to the unmodified code; all 212
  non-MS existing tests pass; all 174 `CustomJS` bodies parse under
  node; zero `FIXED_SIZING_MODE` warnings, reconfirmed.

### Not verified — needs a browser

Item 4 (colorbar width) above, most importantly. Also: the sidebar's own
scrollbar actually appearing/behaving correctly at a short window height;
the close button's visual alignment on the kind-switch row; the
checklist visually growing/shrinking/reordering in real time as you
interact with it (only the underlying JS logic was tested, not real DOM
interaction).

## Addendum 4 (same day): checklist race fixed, colorbar width root cause
## found, multi-column legend was already there, page-scroll restructured

Five more items from the latest round of screenshots: a checklist bug that
made the feature nearly unusable, a third attempt at the colorbar width
(this one backed by evidence, not a guess), a multi-column legend request
that turned out to already be implemented, the recurring blank-rectangle
gap, and a restructuring of how the page scrolls per your latest
description.

### 1. Checking/unchecking one item was clearing all of them — root cause and fix

Confirmed and reproduced (as a logic-level test, not literally in a
browser): the original design had **two separate listeners** — the
checklist's own change wrote to "All", and "All"'s own change wrote back
to the checklist — each guarded by a shared busy-flag meant to stop them
bouncing off each other. That guard's correctness depends on Bokeh
firing property-change callbacks strictly synchronously and re-entrantly;
if that assumption doesn't hold exactly as expected, unchecking one item
sets "All" to unchecked, which (if the guard doesn't block it) fires the
"All → checklist" listener and clears everything.

Rewritten so there is only **one** listener in each direction, and
neither is guarded because neither needs to be:
- Clicking "All" runs one small script that sets the checklist's checked
  set directly (only when it would actually differ).
- Checking or unchecking an individual item runs the same script that
  already handles everything else (visibility, the dynamic Legend
  row) — computing "should All now read as checked" is just one more
  thing that script does, as a plain computed value, not a second
  independent listener reacting to a first one's output.

With no second listener on either side, there's nothing left for the two
directions to race against. Verified directly: unchecking one of two
checked items leaves the other one checked and "All" correctly shows
unchecked; clicking "All" checks everything and settles without
oscillating.

### 2. Colorbar width — found via Bokeh's own source this time

Traced directly into Bokeh 3.10's compiled JavaScript
(`bokeh-widgets.js`, `MarkupView.render()`): every `Div` widget wraps its
HTML content in an internal element it builds for itself —
`div({class: 'bk-clearfix', style: {display: 'inline-block'}})` — and
that `inline-block` sizing is hardcoded there. It sits *inside* the
element that the model's own `styles` property reaches, so nothing set
through `styles` (including both previous attempts: removing an outer
wrapper's scroll property, then adding an explicit width) could ever
touch it. `inline-block` shrinks an element to fit its content
regardless of the width of anything around it — exactly this symptom.

Fixed with a stylesheet rule targeting that class directly, attached to
each Div's own `stylesheets` (a different property from `styles` —
`stylesheets` reaches the widget's own shadow root as a full CSS rule,
confirmed by reading Bokeh's `_user_stylesheets()`):

```css
.bk-clearfix { display: block !important; width: 100% !important; box-sizing: border-box !important; }
```

The `!important` matters specifically: Bokeh's own inline
`style="display: inline-block"` has no `!important`, and a stylesheet
rule with `!important` always wins against a plain inline style — this
is a guarantee from the CSS specification, not a browser-specific
behavior, which is why this attempt carries far more confidence than the
previous two guesses.

### 3. Multi-column legend — already implemented, blocked by the same bug

Checked the actual legend-generating code before writing anything new:
the multi-column layout (`column-width:110px`, wrapping each swatch row
so it won't split across columns) was already there from an earlier
session. It never worked because CSS multi-column layout needs its
container to have a real, definite width to compute how many columns
fit — and the container in question was exactly the `inline-block`
element from item 2 above, which had no definite width of its own.
Fixing item 2 fixes this too, confirmed by rebuilding a real categorized
legend and checking the generated HTML still contains the multi-column
CSS, now sitting inside a Div with an actual resolved width.

### 4. Blank rectangle below the sidebar — recurring cause, now removed

Same underlying issue as Addendum 2's version of this bug: the sidebar
and the plot area were using **different** height formulas (the plot
area, until now, had no bound at all), so whichever one was shorter
stopped short of the other's bottom edge, leaving the gap.

Fixed by giving both the *exact same* formula — a shared function,
`_viewport_bound_css()` — so their bottoms always align. Each still
scrolls independently.

### 5. Page-scroll restructuring, per your latest description

Read as: sidebar and status bar (and toolbar) permanently visible, and
the plot area — not the whole page — is what scrolls when its content
runs long. This is what item 4's fix delivers directly: the plot area
now has the same bounded height and its own `overflow-y: auto` the
sidebar already had, and the toolbar/status bar sit outside that bound
entirely (they're siblings of the sidebar+plot-area row, not inside it),
so neither can be pushed off screen by how tall a panel's info display
gets.

One honest caveat on the bound itself: it's a **documented estimate**
(`_CHROME_HEIGHT_ESTIMATE = 90` px for the combined toolbar+status-bar
height), not a measured exact value — there's no clean way to read
actual rendered heights from Python in this no-Bokeh-server app, the
same reason `_PANEL_HEIGHT = 550` has always been a "typical screen"
estimate rather than a computed one. If 90px under- or over-estimates
your actual toolbar+status-bar height, the columns will be very
slightly too tall or too short relative to true available space. It
should be close, and is a specific, checkable number if it needs
adjusting after you see it.

### 6. Cursor tracking freezing after colorizing — investigated further, one related bug fixed, not resolved

While tracing hover-related code for the width investigation, found and
fixed a real, unrelated bug: the hover throttle was a single timestamp
shared across **both** panels (`window._cvLastProbe`), rather than one
per panel, so rapid movement involving either panel could suppress the
other's next hover update for up to 120ms. Namespaced it per panel. I
cannot confirm this explains what you reported — the server-side probe
handler was already directly tested (Addendum 3) and returns correct
data regardless of colorize state, so the actual cause is somewhere in
client-side territory I have no way to exercise without a browser.

### Verified this round

- Node-level: the exact reported scenario (uncheck one of two checked
  items) reproduced and confirmed fixed; "All" click settles without
  oscillating; JS/Python parity re-verified over the whole state space
  with "All"'s computed value included this time (this caught a real
  bug in my own Python fix — a stale checked-key was leaking through
  after becoming inapplicable, corrupting the "all checked" count —
  found and corrected before shipping).
- Direct inspection of a real built plotter: sidebar and plot-area
  height bounds are identical; every info Div carries the clearfix
  override with `!important`; "All" and the checklist each have exactly
  one listener (not two); a real categorized legend still emits the
  multi-column CSS, now inside a Div with a real width.
- The real, server-side hover-probe handler directly exercised again,
  confirming it returns correct data before and after colorizing.
- All 70 new/updated tests pass; existing scatter/raster/colorize suites
  give **identical failure sets** to the unmodified code; all 212 non-MS
  existing tests pass; all 170 `CustomJS` bodies in the built document
  parse under node; zero `FIXED_SIZING_MODE` warnings.

### Not verified — needs a browser

The colorbar width fix, despite the much higher confidence behind it
this time, is still unconfirmed visually. The exact page-scroll
behavior (whether 90px is a close enough chrome-height estimate) needs
your screen to check. The cursor-tracking freeze remains unresolved.

## Addendum 5 (same day): the checklist bug had a second, distinct cause;
## cursor-tracking freeze remains unresolved, ruled out further

Confirmed working from your latest screenshots: the colorbar width fix
and the multi-column legend (both from Addendum 4). Two items remained.

### 1. Checking/unchecking an individual item was STILL clearing everything

Addendum 4's fix (eliminating the symmetric two-listener design) did not
fully resolve this, per your report. Found the actual remaining cause by
building a materially different kind of test -- one that WIRES the two
scripts together as real, chained event listeners on mock models, rather
than calling each script in isolation with hand-supplied inputs (which is
what every previous test, including Addendum 4's, had done). That
distinction turned out to matter: calling each script by itself can never
exercise a chain reaction between them, and there was one.

`INFO_APPLY_JS` (wired to the checklist's own change) recomputes "All"
and writes `all_cb.active` as a side effect, "only when it actually
differs." That write is still a real property change, and it
**synchronously fires all_cb's own listener** -- the "All -> items"
script -- exactly as if the user had clicked "All" themselves. That
listener, unable to tell the difference, rebuilt the checklist from
scratch based on all_cb's new (reflective) value, clearing everything
whenever the reflected value happened to be "unchecked" -- which is
precisely the case right after unchecking any one item.

Fixed with a guard scoped specifically to this one write:
`window.__cvAllCbReflecting`, keyed by `all_cb.id` (a real Bokeh model
id -- no extra parameter needed), set immediately before that reflective
write and cleared immediately after, both synchronously. The "All ->
items" listener checks it and does nothing when it's set, so a
programmatic reflection of reality is never confused with an actual
click.

Verified with a new class of test built specifically to catch this
(`test_info_panel.py`'s wired-listener tests): real mock models with
genuine getter/setter properties (so a plain `obj.prop = val` -- exactly
what every script here actually writes -- fires listeners the same way a
real Bokeh model does, not just an explicit `.set()` call), listeners
registered exactly as the real code registers them, and the reported
scenario (uncheck one of two checked items) driven through that real
chain. Confirmed the new tests fail against the pre-fix code (reproducing
the exact clearing behavior) and pass against the fix.

### 2. Cursor tracking freezing after colorizing -- ruled out further, still unresolved

Directly inspected a real built panel's `HoverTool` before and after a
categorical colorize press: `hover.renderers` is the **literal same list
object** as the figure's own `renderers` (not a copy, not reassigned) in
both cases -- there is exactly one persistent `image_rgba` glyph for the
whole life of a panel, and colorizing never adds, removes, or replaces
it. This rules out "the hover tool is watching a stale renderer" as
cleanly as a theory can be ruled out without a browser.

Combined with Addendum 3's direct test of `_handle_probe` (returns
correct, fresh data regardless of colorize state) and Addendum 4's fix
to the previously-global hover throttle, I have now checked every
server-side and Python-inspectable piece of this path and found nothing
wrong in any of them. Whatever is happening is genuinely client-side,
in code I have no way to execute or observe without a real browser.

**What would help most:** open the browser's DevTools console before
reproducing, then colorize and hover. If there's a red error at the
moment tracking stops, that single line would very likely point straight
at the cause -- I've exhausted what static reading of the source can
tell me here.

### Verified this round

- The exact reported checklist scenario, reproduced and confirmed fixed
  via genuinely wired (not isolated) listeners -- 5 new tests covering
  single-item uncheck, middle-of-three uncheck, "All" click, re-checking
  back to fully checked, and two sequential unchecks in a row.
- Confirmed those same tests correctly FAIL against the pre-fix code
  (proving they would have caught this before it shipped).
- `HoverTool.renderers is figure.renderers` (literal identity) directly
  confirmed both before and after colorizing, on a real built panel.
- All 74 new/updated tests pass; existing scatter/raster/colorize suites
  give **identical failure sets** to the unmodified code; all 170
  `CustomJS` bodies parse under node; zero `FIXED_SIZING_MODE` warnings.

### Not resolved

The cursor-tracking freeze. I don't have a next static-analysis step for
this one -- it needs a browser console.

## Addendum 6 (same day): status bar now truly pinned to the bottom;
## busy cursor added to pan/zoom re-renders too

Two requests this round: fix the window-resize/status-bar gap properly
this time, and add the busy cursor to ordinary pan/zoom re-renders —
which you correctly suspected might explain the "cursor tracking
freeze" that Addenda 3–5 never managed to pin down.

### 1. Status bar not reaching the true bottom on a resized window

The previous fix (Addendum 4) gave the sidebar and plot area a shared
`height: min(670px, calc(100vh - 90px))` — bounded so they'd never force
a page scroll, but only ever a **cap**, never something that could
*grow*. Resize the window taller than that cap and the excess space had
nothing in the layout claiming it: the status bar sat right below the
now-too-short plot area/sidebar row, with a persistent blank gap filling
the rest of the window beneath it.

Fixed properly this time with an actual grow-to-fill mechanism rather
than a bigger guess at the cap. Both the root layout and the row holding
the sidebar and plot area now carry a CSS class
(`cv-root-shell`, `cv-body-row`), and a script run once at page load
finds those exact elements — recursively through Bokeh's shadow DOM,
the same lookup this app's kind-switch scroll fix already uses — and
sets, directly on them: the root shell to `height: 100vh`, and the body
row to `flex: 1 1 auto; min-height: 0`, so it fills *however much space
is actually left* after the toolbar and status bar take their own
natural height, on any window size. The sidebar and plot area's own
`height` changed from that fixed formula to a plain `100%`, which now
resolves against the row's real, computed height instead of a guess.

This goes through direct inline-style assignment on the real elements
rather than through this app's usual `styles=` dict on the Bokeh model,
for a specific reason: Bokeh computes its own `flex`/`align-self` for
every layout child from that child's `sizing_mode`, and a plain
`styles=` entry can't be certain of outranking Bokeh's own generated
stylesheet. An inline style set directly on the element after Bokeh has
already rendered it always wins, by the CSS specification — not
something that needed testing to trust.

### 2. Busy cursor added to pan/zoom re-renders

You raised a real gap: the busy cursor only ever showed up around the
main Plot/Reload buttons — an ordinary pan or zoom, which also triggers
a re-render over the network, showed nothing at all while that request
was in flight. That's a very plausible explanation for the "cursor
tracking has frozen" reports from Addenda 3–5: the panel wasn't broken,
it was just mid-request with zero visual indication of that, indistinguishable
from actually being stuck.

Pulled the busy-cursor logic out of its previous single home into a
small shared, page-global piece both the Plot button's own script and
each panel's pan/zoom handler now call — so panning or zooming shows the
exact same overlay Plot already did, for as long as that specific
request is outstanding. Raster and scatter share one underlying
mechanism, so this covers both, in every panel, in one change.

### Verified this round

- Direct inspection of a real built plotter: `cv-root-shell` and
  `cv-body-row` classes present on the correct elements; the init
  script that finds and sets their inline styles ships in the page;
  the global `html, body { height: 100% }` rule is present; sidebar and
  plot area both resolve to `100%`.
- Every panel's pan/zoom handler (all four: both panels × raster and
  scatter, since they share one base-class method) carries the shared
  busy-cursor script and calls it before sending and inside the
  response callback; the Plot button's own script still disables
  Plot/Reload specifically, on top of the same shared overlay.
- All 76 new/updated tests pass; existing scatter/raster/colorize
  suites give **identical failure sets** to the unmodified code; all
  170 `CustomJS` bodies parse under node; zero `FIXED_SIZING_MODE`
  warnings.

### Not verified — needs a browser

Both of these are logic/structure checks, not a rendered screen: the
gap-free resize behavior itself, and how the busy cursor actually looks
during a live pan or zoom. If item 2 turns out to be the real
explanation for the cursor-tracking reports all along, that would be
good news — nothing left to chase there.

## Addendum 7 (same day): the status-bar fix was a real regression —
## found the actual cause with a headless browser this time, not guessing

You reported, correctly, that Addendum 6's status-bar fix was worse than
what it replaced: a single page-level scrollbar for the whole app, the
status bar not visible at startup, and a gear tab's content pushing it
further down. This addendum explains what was actually wrong and how it
was confirmed this time — not inferred from reading source, but watched
happening in a real browser.

### What was actually happening

Addendum 6's fix found the root layout and the sidebar/plot-area row by
CSS class and set their `height`/`flex` directly via JavaScript, once,
at page load. That looked reasonable from the source, but it never had
a chance: **Bokeh's own layout engine recomputes and re-applies its own
inline `style.height` on these elements continuously** — on initial
load, on every window resize, and on any DOM change such as a tab's
content becoming visible. A one-time external override, however it's
applied, is silently overwritten the next time Bokeh's own layout pass
runs — and both of your reported triggers (resizing the window, opening
a gear tab) are exactly the events that make it run again. That's why
it didn't just fail to help — it actively fought Bokeh's own sizing on
every one of those events, which is a materially worse position than
not touching it at all.

### How this was actually confirmed this time

Built a real headless-browser test harness for this specific question,
rather than continue reasoning from source (which is what produced both
the original bug and Addendum 6's failed fix): Playwright driving a
Chrome-for-Testing binary already cached in this environment, loading
the real page rendered with Bokeh's `INLINE` resources (so no network
access is needed — this environment's network is restricted and can't
reach Bokeh's CDN). That let the actual computed layout be inspected
directly — `getBoundingClientRect()`, computed styles, real resize
events — instead of inferred from reading Bokeh's compiled JS, which is
what led to guessing wrong twice.

That harness reproduced the regression exactly (confirmed a page-level
scroll appearing, the override being silently reset) and was then used
to test the actual fix before writing a single line of the real
codebase: **`sizing_mode="stretch_both"`** on the root layout, the
sidebar/plot-area row, and the plot area itself, with
`sizing_mode="stretch_height"` on the sidebar — Bokeh's own native
mechanism for "fill the remaining space", which Bokeh itself keeps
correct on every layout pass, rather than something set once from
outside that Bokeh doesn't know about and will undo.

Verified in that harness, using a plain-Bokeh-widgets reconstruction of
the exact same column/row nesting the real app uses (the real app's own
custom widgets need a separate compiled JS bundle to render standalone,
which is a different problem from this layout question and wasn't
pursued — the layout mechanism itself is what regressed, and that's
what this isolates):

- **Three window sizes** (900px, resized to 1400px taller, resized to
  500px shorter than the content needs): the status bar's bottom edge
  tracked the true bottom of the window every time, with **zero**
  page-level scroll at any size.
- **A simulated gear-tab-content-growth** (20 extra rows added to a
  sidebar column, matching what opening a real gear tab does): the
  status bar's position was completely unaffected, and the page's
  scroll height did not change at all.

The previous CSS-class/init-script mechanism was removed entirely
(`cv-root-shell`/`cv-body-row` classes, the add_init_script block that
searched for them) — nothing is fighting Bokeh's own layout system
anymore, so there is nothing left for it to silently undo.

### Verified this round

- The exact regression reproduced and the exact fix confirmed working,
  in a real headless browser, across three window sizes and a
  simulated dynamic-content-growth scenario — not inferred from source.
- Direct inspection of a real built plotter: root, body row, and plot
  area all carry `sizing_mode="stretch_both"`; the sidebar carries
  `sizing_mode="stretch_height"`; no leftover height/flex overrides or
  the abandoned CSS-class mechanism remain anywhere.
- All 75 new/updated tests pass; existing scatter/raster/colorize
  suites give **identical failure sets** to the unmodified code; all
  170 `CustomJS` bodies parse under node; zero `FIXED_SIZING_MODE`
  warnings.

### Not verified

The real app's own rendering, with its custom Tip/EditSpan/comm widgets
— those need a separate, compiled JS bundle to render outside a live
session, which is why this round's verification used a plain-Bokeh
reconstruction of the same layout structure instead. The layout
mechanism itself (what actually regressed) is what that isolates and
confirms; the custom widgets are a separate concern, unaffected by this
change, but not independently re-verified visually this round.
