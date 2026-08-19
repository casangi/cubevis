# `VisibilityPlotter` grid / iteration support — design notes (draft)

Status: **draft — pending confirmation**, written to be merged into the master
implementation plan. Captures decisions from the July 28 2026 design
discussion on supporting a PlotMS-style grid of plots (e.g. "Gain Phase vs
Time" per antenna, in a fixed N×M page with iteration through antennas).

## Context

PlotMS supports laying out a grid of related plots — same axes, different
iteration value per cell (per antenna, per SPW, per field, per scan) — with
paged navigation through the full iteration set. `VisibilityPlotter`
currently supports exactly one raster panel and one scatter panel (side by
side or over/under). This note captures the decisions needed to generalize
that to a grid, consistent with the existing no-Bokeh-server, comm-driven
architecture (each panel is a real interactive Bokeh object — its own
`ColumnDataSource`s, hover/probe tools, and comm registrations — not a static
image).

**Terminology note (added July 29 2026):** "iteration mode" is probably a
better name than "grid mode" — it names the property that actually does
the design work in decisions 4–8 (shared axes across cells, enabling
uniform comparison), whereas "grid" only describes the paginated N×M
visual layout sitting on top of it, and it matches PlotMS's own
terminology. Accepted in principle; the full rename across this document is
a deferred follow-up, not done yet — "grid mode"/"grid" below still means
the same thing.

## For the master roadmap: phase reordering (stakeholder feedback, July 28 2026)

A stakeholder flagged that export/scripting and performance benchmarking
were both sitting in a final "polish/completeness" phase, and suggested
moving them earlier so benchmarking against plotMS happens before grid-mode
design commitments are locked in. **Agreed** — see decision 10's
"Sequencing revised" note below for the reasoning. Net effect on this note's
ordered plan: a functional (not fully polished) slice of export/scripting —
generator/iteration API + one working PNG path, enough to benchmark
honestly — now sits as step 3, immediately after duo mode stabilizes and
before any grid-mode step. Whatever phase currently holds "export/scripting"
and "performance benchmarking" in the master roadmap should be split: the
functional/benchmarking slice moves to sit right after duo-mode work, full
completeness (`.ipynb` packaging ergonomics, pixel-perfect labeling, etc.)
stays in the later polish phase.

## Preview release scope (settled July 29 2026)

Promised a few turns back and not actually written down until now — the
gear/tabs interaction (decision 9) and the PNG rendering-path prototype
(decision 10) have both moved from rough idea to concrete since then, which
is what makes this worth pinning down now rather than later.

**In preview:**

- The `.data`/`change.emit()` ordering fix and the raster axis-conflict
  guard (already implemented).
- `compact_toolbar`/autohide, defaulting on for duo mode as well as grid
  mode (decision 1) — **implemented and tested working** (July 29 2026);
  the static toolbar-space margin is expected upstream Bokeh behavior, not
  an issue (see decision 1).
- The gear tool + tabbed sidebar config for duo mode, full interaction flow
  as specified in decision 9 — settled, not just a rough idea, as of the
  last several turns.
- **Headless PNG export via matplotlib** (decision 10) — moved from
  "internal benchmarking tool, your call" to **in preview**, on the
  strength of today's prototype: it works with a dependency this domain
  already assumes (matplotlib), unlike the webdriver path, which failed
  outright in a pipeline-like test environment. Ships as the functional
  slice described in decision 10's "Sequencing revised" note — a working
  labeled PNG path, not full completeness (the `eq_hist` colorbar fidelity
  question stays a known follow-up, not a preview blocker).
- The generator/iteration API over duo mode that makes scripted export
  possible at all (decision 10) — doesn't depend on grid infrastructure,
  needed for the export feature above to be usable from a script/pipeline
  rather than only interactively.

**Not in preview:** everything grid/iteration-mode (decisions 2–8) — still
correctly deferred, no UI decided, real open questions remaining (grid size
caps, comm namespacing against the actual transport layer). Cross-cell
sync (decisions 6/7) and grid-scoped export granularity stay out for the
same reason.

## Decisions

### 1. Toolbar auto-hide on hover — implemented and confirmed working

**Decision (revised July 28 2026):** expose `compact_toolbar: bool = True`
on the shared panel base (`VisibilityPlot`), implemented via Bokeh's
built-in `figure.toolbar.autohide = True` (no custom JS needed). **Default
changed to `True`, applying to duo mode as well as grid mode** — deliberate
early UX validation, not just a grid-mode setting: same logic as pulling
benchmarking earlier (decision 10) — surface a grid-motivated assumption on
the simple 2-panel case, cheaply, before grid depends on it. If hover-reveal
turns out to be disliked, better to learn that now. No explicit "always show
toolbar" preference toggle for now — same defer-until-requested pattern used
throughout this doc; add one only if feedback actually asks for it.

Independent of everything else below — can land at any time.

**Implemented (July 29 2026):** `compact_toolbar` threaded through
`VisibilityPlot.__init__` (sets `self._fig.toolbar.autohide`) and
`VisibilityPlotter.__init__` (passed to both panel constructions);
`VisibilityRaster`/`VisibilityScatter` needed no changes since both already
forward `**kwargs` to the base class.

**Known behavior, not a bug — accepted as-is:** the toolbar's reserved
layout space (the `toolbar_location="right"` column) does **not** collapse
when hidden; only the drawn content toggles, leaving a static margin.
Confirmed as intentional upstream design, not a quirk of this
implementation — from the original Bokeh feature discussion
(bokeh/bokeh#8284): *"the toolbar does not need to be in the center layout,
it just needs to allocate space as if it were going to draw, but then not
actually draw."* Reasoning: collapsing the space on hide/show would reflow
the layout right at the plot's edge on every hover-boundary crossing, which
risks shifting plot content under the cursor and flickering the toolbar in
and out. Reclaiming the space for real would mean not using Bokeh's native
toolbar system at all — a custom CSS-overlay toolbar with our own
`CustomAction`-based tools, reimplementing something Bokeh already provides
— disproportionate custom/fragile UI work for a ~30–40px margin, and cuts
against reusing what Bokeh already provides. Decision: accept the static
margin; the actual goal (less visual clutter) is achieved regardless.
Revisit only if this becomes an actual complaint, not preemptively.

### 2. Preset max grid size, not dynamic/shared-renderer

**Decision:** grid cells remain real, individually-interactive
`VisibilityRaster`/`VisibilityScatter` instances (own `ColumnDataSource`s,
comm registrations, flag tools). A shared-renderer-to-static-image approach
was considered and rejected: flagging depends on live click-drag selection
per panel, which a static raster can't support, and hover/probe
functionality would need to be reinvented per cell regardless.

Grid dimensions are bounded by a configurable max (proposed default page
size 3×3, hard cap ~6×6) rather than unbounded/dynamic, since there is no
Bokeh server to coordinate object lifecycle for an open-ended object pool.

### 3. Paginate, don't scroll

**Decision:** fixed-page grid with next/prev navigation through the
iteration set (antenna/SPW/field/scan), matching PlotMS convention. Turning
the page is a **selection change** on already-existing per-cell panels
(reusing the existing `update_axes()` / `_render()` recompute path with a
different iteration value), not a rebuild.

Rejected: continuous scrolling, either because (a) it forces every page's
worth of panels to exist and be computed simultaneously, or (b) it requires
virtualized/lazy rendering on scroll, a materially larger lift than paging
for a preview tool.

### 4. Uniform axes/mode per grid *by default*, not a structural limit

**Decision, phase 1 UI scope:** the grid has one global `mode`
(raster-only / scatter-only / both) and one set of axis controls
(`ry_sel`/`rx_sel`/`rq_sel`/`sx_sel`/`sy_sel`), applied identically to every
cell. What varies per cell is the iteration value only (e.g. antenna),
mirroring your screenshot: same "Gain Phase vs Time" axes in every cell,
different antenna per cell.

New control needed: an "Iterate by" `Select` (Antenna / SPW / Field / Scan)
plus rows × cols (bounded per #2).

This is a **UI scope** decision, not an architectural one — see decision 8.
Per-cell heterogeneous configuration (different plot kind and/or axes per
cell) is out of scope for the phase-1 UI, but the underlying object model
is deliberately not closed off from it.

### 5. Object count: only allocate what `mode` needs, sized to the page

**Decision:** N×M `VisibilityRaster` and/or N×M `VisibilityScatter`
instances are created only for the panel type(s) the current `mode`
actually uses (`mode="both"` → both; `mode="raster"` → raster only, zero
scatter instances), and sized to the **page**, not the full iteration count.
Page turns re-select existing panels rather than destroying/recreating them.

Each cell's panel object is independent and individually configurable via
its own `update_axes()` call — this independence is what makes decision 8
(below) possible without a rewrite.

### 6. Cross-cell pan/zoom sync (added July 28 2026)

**Decision:** a toggle ("Sync") broadcasts viewport range changes across
sibling grid cells of the same panel type (raster↔raster, scatter↔scatter —
never raster↔scatter, since their axes aren't comparable). Both X and Y
sync together; no per-axis granularity in phase 1. Bundled with the
crosshair position sync in decision 7 under the same toggle — one "linked
cells" concept, one control.

Motivation: grids like the per-antenna Gain Phase example exist
specifically to compare behavior across antennas at the same point in time,
and having to manually replicate a zoom across N cells defeats that purpose.

**Implementation approach:** reuses the existing per-panel viewport hook
(`js_on_change("end", rerender_js)` on each figure's `x_range`/`y_range`,
already debounced) rather than a new mechanism. When sync is on, a range
change on one cell's figure sets the same `start`/`end` on every sibling
figure of the same panel type; that in turn fires *that* cell's own
existing `rerender_js`, so each cell independently re-shades/re-queries
through the already-built single-panel path. Implemented as a broadcast
(each cell listening and re-setting siblings), not by literally sharing one
`Range1d` object across figures — more robust to toggling the sync on/off
at runtime, and avoids Bokeh range-reparenting edge cases. Needs a
reentrancy guard (a "sync in progress" flag) to prevent cell A's broadcast
to cell B from bouncing back to cell A.

Sync-eligibility between two cells is computed by checking whether their
*actual current axes match*, not by "both cells belong to this grid" — see
decision 8 for why that distinction matters.

**Caveat to watch for, not a blocker:** raster rerender re-queries the
backend (not just a local reshade) once zoom crosses the existing
decimation threshold (`needs_requery`/`is_decimated`). With sync on, one
zoom gesture on a deep raster grid triggers that backend query on every
raster cell at once — a burst of N concurrent queries instead of one. Not a
concern for `LocalVisibilityReader`; worth revisiting if/when a remote
`ReductionContext` is in play.

### 7. Cross-cell crosshair position sync (added July 28 2026)

**Decision:** in scope, bundled under the decision-6 "Sync" toggle. Split by
lift, not by concept:

- **Crosshair line *position*** — accepted into scope. Because grid cells
  of the same panel type share identical axes by construction (decision 4,
  default case), this is exactly the case Bokeh's built-in linked-crosshair
  pattern handles natively: multiple figures sharing the same `Span`
  objects as their `CrosshairTool` overlay stay in sync automatically, no
  custom JS. This is a materially smaller lift than the raster↔scatter
  crosshair link (which needed custom axis-translation logic precisely
  *because* those two panels' axes differ) — the grid case has no
  translation to do.
- **Hover *probe/tooltip value* sync** (the actual `probe_raster_pixel`/
  `probe_scatter_pixel` readout, not just the line) — **stays deferred**.
  Syncing this would mean firing a probe comm round-trip on every sibling
  cell on every mouse-move, which is real traffic across a fast-firing
  event and potentially dozens of cells. Revisit only if requested.

### 8. One grid architecture, not a fork for future heterogeneity (added July 28 2026)

**Decision:** do not build a separate system for a hypothetical future
"mixed raster/scatter/per-cell-different-axes" grid. Decision 5 already
commits to independent, individually-configurable `VisibilityRaster`/
`VisibilityScatter` objects per cell (each with its own `update_axes()`
call) — nothing about that requires uniform axes across cells. Uniformity
(decision 4) is a **UI scope** choice (one set of axis Selects broadcasts
the same config to every cell's constructor), not an architectural
constraint of the panel objects themselves.

Concretely: the per-cell config/descriptor (architecture piece 1, already
needed for the per-cell selection override) should carry an optional
`axes_override` field from the start, even though nothing sets it until a
per-cell customization UI exists. v1 ships with only the global axis
controls (matches decision 4, minimal UI); a later per-cell override becomes
an additive UI change against an already-general data model, not a rewrite
of the grid container, pagination, comm namespacing, or sync machinery.

**Consequence for sync (decisions 6 and 7):** sync-eligibility between two
cells is computed by checking whether their *actual current axes match*,
not by "both cells belong to this grid." With uniform axes (v1) that's
always true, so no visible behavior change now — but it means sync keeps
working correctly with zero changes if/when a per-cell axes override is
added later, rather than needing to be revisited.

### 9. Per-slot plot-kind switching for the 2-panel layout (added July 28 2026)

**Decision:** support switching either of the current two panel slots
between raster and scatter independently (e.g. two scatter panels side by
side), for the existing 2-panel layout only — not for grid mode (see
below). Motivated as much by proof-of-concept validation of decision 8 as
by the functionality itself; this was also specific demo feedback.

**Grid mode stays restricted to uniform type (decision 4), deliberately —
this is not being revisited as unnecessary caution.** Two independent
reasons, not just "be careful":
- The natural cheap implementation of per-slot switching (see below) is
  pre-building both panel kinds and toggling visibility. At 2 slots that's
  4 objects; at grid scale (up to 6×6 per decision 2) that's 2×N×M —
  up to ~72 live panel objects, each with its own comm registrations, hover
  tools, and image sources, mostly invisible at any given time. The
  cost/benefit is genuinely different at the two scales, not just "more
  cautious."
- Iteration mode's purpose is comparing the *same* plot across antennas/
  SPWs/fields/scans. Per-cell type divergence works against the thing the
  feature exists for, independent of implementation cost.

**Implementation approach — reuse the existing mode-toggle trick, don't
build dynamic construction/teardown.** The current `mode` toggle
(raster-only/scatter-only/both) already works by pre-building *both* a
`VisibilityRaster` and a `VisibilityScatter` and toggling container
`.visible` — not by constructing/destroying objects. Extend that same
pattern per slot: each of the two slots pre-builds one raster panel and
one scatter panel; a per-slot toggle picks which is shown. This avoids
needing any new comm lifecycle management (construct/destroy has no clean
story without a Bokeh server) and is the cheapest way to validate decision
8's premise before ever deciding whether it holds up at grid scale.

**Switch affordance — a single "gear" tool, revealing a sidebar panel, not
a floating popup (revised again July 28 2026).** Originally two separate
pieces (a dedicated switch icon plus a slot-keyed sidebar redesign), then
consolidated into one gear tool opening a *floating popup* local to the
plot. Revised once more: the popup becomes a **sidebar panel** instead —
"Panel A" / "Panel B" — each starting with a Kind selector (Raster/Scatter)
that reshapes the fields below it (axes, quantity, colour scaling — the
raster/scatter colormap controls are already panel-kind-specific in the
sidebar today, so they belong here too).

**Presented as tabs, not stacked sections (revised again July 28 2026).**
Two full sections (kind + axes + quantity + colour scaling each) stacked
simultaneously is a lot of sidebar height. Bokeh's native `Tabs`/
`TabPanel` widget solves this directly: "Panel A" and "Panel B" as two
tabs, only one visible at a time, but — crucially — Bokeh's `Tabs` already
preserves each tab's widget state while it's inactive, so switching tabs
doesn't discard whatever was being edited on the other one. That means we
only need to manage *one* level of show/hide ourselves (whether the whole
tab widget is present or hidden at baseline), not two independent
per-section toggles — genuinely less to build than the stacked-sections
version, not just a visual preference.

Reasoning for the sidebar-over-popup change is unchanged from before: the
floating-popup choice was justified by avoiding sidebar bloat, but that
concern is really about the N×M *grid* case — duo mode is fixed at exactly
2 slots, forever, and gear never appears in grid mode at all (see below),
so the scaling concern that motivated a popup doesn't actually apply here.

**Full interaction flow (settled July 28 2026, revised for tabs):**

1. The tab widget starts hidden entirely (baseline state).
2. User hovers the plot → `compact_toolbar`/autohide (decision 1) reveals
   that panel's toolbar, including the gear icon.
3. User clicks gear → the tab widget becomes visible (expands the sidebar
   first if it's collapsed), with that slot's tab active. Clicking gear on
   the *other* slot while the widget is already visible **switches the
   active tab** rather than adding a second simultaneously-visible section
   — but doesn't discard whatever was already changed on the first tab
   (Bokeh `Tabs` preserves inactive-tab widget state), so tweaking both
   before applying still works, just via tab-switching instead of scrolling
   past a stacked section.
4. User adjusts the active tab's controls (Kind, axes, quantity, colour
   scaling).
5. User presses the **existing global Plot ▶** — no new per-panel apply
   button. The per-panel change-detection that already exists (the same
   mechanism behind `raster_axes_changed`, extended to cover per-slot kind
   rather than fixed raster/scatter categories) is what makes "click one
   global button, only the touched panel(s) re-render" work correctly
   without a second update mechanism.
6. **On success**, the panel(s) update and the whole tab widget auto-hides
   again, returning to the baseline state.
   **On a validation error** for either slot (e.g. an axis conflict, same
   pattern as the raster Y/X guard built earlier in this project) — the
   tab widget stays open, and **auto-switches to whichever tab has the
   error** if it isn't already active, so the user immediately sees why
   nothing updated rather than wondering why the plot didn't change.

This keeps the sidebar clean most of the time (the property a floating
popup was originally trying to achieve) while staying embedded in the
sidebar (the property that made the popup unnecessary in the first place)
and compact even when open (the property tabs add over stacked sections) —
and it means the whole interaction follows one consistent "reveal on
demand, hide when done" idea end to end, rather than stacking multiple UI
philosophies.

- Needs nothing new architecturally beyond Bokeh's built-in `Tabs`/
  `TabPanel` widget plus one show/hide toggle for the whole widget — the
  same visibility-toggle pattern already used elsewhere in
  `visibility_plotter.py`, and simpler than either the popup or the
  stacked-sections version since Bokeh handles the inter-tab state
  preservation for free.

**Resolved (July 29 2026): `mode`/`layout` merge into one permanent "Layout"
sidebar control, replacing both.** The open loose end above is resolved by
separating concerns cleanly rather than reconciling `mode`'s existing
three-way (raster/scatter/both) semantics with per-slot Kind: `mode_rbg`
(both/raster/scatter) and the existing separate `layout_rbg` (side/over) —
two overlapping radio button groups today — collapse into **one** permanent
Layout selector: **One / Side by Side / Over-Under / Grid (future,
disabled/absent until grid mode ships)**. Number of visible slots follows
directly from Layout (one→1 slot, side/over→2 slots, grid→N slots later);
gear+Tabs (unchanged from above) governs each *visible* slot's Kind
(Raster/Scatter) and configuration. "One panel" becomes a first-class layout
state rather than today's "two slots, one hidden."

This is a better resolution than a `mode`-survives-as-convenience compromise
would have been, because it also directly realizes decision 8 in the actual
UI rather than only at the data-model level: duo mode and grid mode stop
being two separate systems and become points on the *same* Layout selector,
not a special case plus a future generalization bolted on separately.

Implementation note: all slots (up to whatever Layout currently implies)
still pre-build both Raster and Scatter panel objects regardless of current
Layout selection, per the established no-Bokeh-server pattern (decisions 5
and 9) — Layout toggles slot-level container visibility one level above
where Kind toggles which-of-that-slot's-two-panel-objects is visible; no
dynamic construction/teardown either way.

**Future item, explicitly postponed (added July 29 2026):** a combined
"screenshot of the whole panel layout, preserving arrangement" export is a
real gap — Bokeh's built-in Save tool only grabs one figure, and decision
10's matplotlib export preserves individual panel fidelity, not the
multi-panel arrangement around it. Distinct problem (composing N
already-rendered images into one, roughly) from either existing mechanism.
Not designed now; noted so it isn't lost.

**Considered and declined: switching duo mode's Bokeh layout widget to a
grid-shaped container now (added July 28 2026).** Raised as "should duo
mode use whatever Bokeh layout mechanism is most amenable to grid mode,
proactively?" The part of this worth doing is already done — decision 8
already unifies the per-cell/per-slot *config model* so duo mode's two
slots and grid mode's N×M cells share one logical shape. What's left is
narrower: should the *Bokeh container widget* itself switch from the
current manual `Row`/`Column` + visibility-toggle containers to
`gridplot()`/`GridBox` now, ahead of actually needing it for grid mode?
Declined for now: duo mode's side-by-side/over-under toggle currently works
via the same container-visibility-swap trick reused throughout this doc
(decisions 5 and 9), and switching to `gridplot()` would mean reworking
that working mechanism for a benefit that doesn't land on the actually hard
parts of grid mode — pagination, comm-id namespacing, and object pooling
(architecture pieces 3–6), none of which get easier because duo mode's
figures sit in a `GridBox` instead of a `Row`. Worth reconsidering only if,
once grid-mode implementation actually starts, `gridplot()` turns out to
meaningfully simplify something concrete — not speculatively now.

**Stage 1b implemented (July 30 2026) — data model only, deliberately
scoped short of a full slot-indexing rework. Confirmed against real data
the same day:** `_activate_slot_kind("A", "scatter")` on a live
`VisibilityPlotter` correctly triggered a real render (~2s) and populated
`_slot_a_scatter._layer_aggs`/`_layer_dfs` from all-`None` to real
per-polarization `xarray.DataArray`s — the query→render→activate path
works end to end, not just against the synthetic tests from decision 11.** Tracing the ~60 references
to `self._raster`/`self._scatter` throughout `visibility_plotter.py`
(hover probes, flag tool registration, crosshair sync, `doPlot`'s JS args)
showed they're all deeply coupled to "raster and scatter are two fixed,
permanent objects" — too large and too interdependent a rewrite to do
safely as one untestable pass. Key realization that unblocked a smaller,
real slice of it: **nothing can trigger a kind switch yet**, since gear/Tabs
doesn't exist. That means `self._raster`/`self._scatter` can become
*properties* resolving to "whichever object is active per slot," and every
one of those ~60 references keeps working completely unchanged — they'll
resolve to the same fixed object for the entire session, identically to
today, because nothing ever calls the thing that would change which object
is active.

What actually got built: four panel objects (slot A raster + scatter, slot
B raster + scatter) instead of two, using `defer_initial_render` (decision
11) for each slot's inactive kind; `self._raster`/`self._scatter` as
compatibility properties; and `_activate_slot_kind(slot, kind)` as real,
testable Python-level infrastructure that correctly switches state and
performs a first-time render via the same `update_axes()` mechanism decision
11 already established — but isn't wired to any UI trigger yet.

**Named explicitly rather than glossed over: this shim doesn't solve the
deeper problem, and that's Stage 1c, not later polish.** Once kind-switching
is actually wired up, both slots could independently hold the same kind
(two rasters, two scatters) or neither could — "the raster panel" stops
being well-defined, and the property's fallback behavior in that case is
arbitrary (documented in code, not silently hidden). Stage 1c is: build
gear/Tabs itself, and rework all ~60 references from kind-indexed to
properly slot-indexed. Not attempted now — deliberately deferred until
there's a UI trigger to build it against, so it's verifiable rather than
speculative.

**Grounding note (added July 31 2026):** researched PlotMS's and msview's
own precedent for the mode-A/mode-B split underlying this whole area (the
"a few individually-configured plots" vs. "uniform grid, iterate the
value" distinction — see decisions 2–8 for mode B). PlotMS itself already
implements this as two separate mechanisms rather than generalizing one
into the other: `iteraxis`+`gridrows`/`gridcols` for uniform-per-cell
iteration (no hard cap on grid size, just a legibility caveat), versus
`rowindex`/`colindex`/`plotindex`+`clearplots=False` for manual,
individually-configured panels built one `plotms()` call at a time.
Independent confirmation the split is the right shape, not an artifact of
this project's own constraints. Real antenna counts (ALMA 66, VLA 27)
confirm that iteration-driven grids are naturally an order of magnitude
past anything reasonably hand-configured, which is why mode A's ceiling is
inherently small — 2 today, 4 a plausible later extension, not a number
that will keep creeping toward grid-sized N. Separately: msview (which
this project replaces for MS-level interactive raster/flagging) is itself
already deprecated and removed from Mac packages in favor of CARTA for
image display — CARTA does not cover MS-level interactive raster/flagging,
so this is a real gap being filled, not a nice-to-have.

**Considered and declined: shared retargeted config drawer (added July 31
2026).** Raised as an alternative to a fixed `TabPanel`-per-slot `Tabs`
widget: one reusable drawer whose controls get repointed (via a small
JS-side registry) to whichever panel's gear was last clicked, rather than
one `TabPanel` pre-built per slot — motivated by not wanting the widget's
shape to hard-depend on slot count if mode A grows past 2. **Declined:**
mode A's actual use case is comparing several in-progress panel
configurations before committing via Plot ▶ (per the "helps decide what's
worth flagging" motivation in this doc's Context section) — not editing
one panel at a time. A user switching from configuring Panel A to Panel B
needs to come back to A later and see exactly what they'd already set, not
a drawer that's been repointed and forgotten it. Bokeh's `Tabs` gives this
for free (inactive-tab widget state is preserved natively); a shared
drawer would have to reconstruct that state management itself, for a
benefit (bounded widget count) that doesn't actually materialize in the
regime mode A operates in — see the grounding note above for why mode A
stays small-N by nature. One `TabPanel` per slot, sized to whatever mode
A's max turns out to be, stands as designed.

The retargeted-drawer *mechanism* isn't wasted, though — it's a better
fit for decision 8's deferred per-cell `axes_override` editing inside grid
mode, a genuinely different workflow (an occasional exception layered on
an otherwise-uniform grid, not several in-progress comparisons held open
at once, so nothing is lost by a drawer that only remembers the one cell
currently being edited). Noted as a design option for when that UI is
actually built, not decided now.

**Panel-state refactor: named attributes → an iterable slot record (added
July 31 2026), a.k.a. "Stage 1b.5" — implemented and tested July 31
2026.** Stage 1b's storage —
`self._slot_a_raster`/`_slot_a_scatter`/`_slot_b_raster`/`_slot_b_scatter`
plus `_slot_a_kind`/`_slot_b_kind`, six separate named attributes — is
workable at exactly 2 fixed slots, but every one of the ~60 references
Stage 1c needs to rework would have to be rewritten *again* if mode A's
slot count ever changes (e.g. 2 → 4 per the grounding note above), since
"loop over however many slots currently exist" isn't expressible against
fixed attribute names. **Decided:** replace the six named attributes with
a small per-slot record (e.g. a `_PanelSlot` dataclass — `id`, `kind`,
`raster`, `scatter`, plus an `.active` property resolving to whichever of
`raster`/`scatter` matches `kind`) held in an ordered `self._slots` list.
`self._raster`/`self._scatter` (the compatibility properties above) and
`_activate_slot_kind()` become thin wrappers over the same fallback logic
already documented above, just sourced by iterating `self._slots` instead
of two hand-written `if` branches on named attributes — no behavior
change, existing Stage 1b tests should pass unchanged against it.

This is a pure data-structure refactor, independent of the gear/Tabs UI
itself. Sequenced as its own step, **before** the gear/Tabs widget build
and the ~60-reference rework (both of which benefit from being written
once against "iterate `self._slots`" rather than against fixed names that
might need revisiting) — cheaper to land this first than to discover
partway through Stage 1c that the fixed-attribute shape needs changing
out from under an in-progress rework.

**Scope note:** slot ids stay simple strings (`"A"`, `"B"`, ...) for mode
A; grid mode's per-cell identifiers (architecture piece 4's
`ids[f"plot_{row}_{col}"]`-style comm namespacing) are a different id
shape (row/col pairs) and a different, larger structure (bounded pool,
pagination-aware) — this refactor touches mode A's fixed slots only, not a
preemptive grid data model. Whether grid mode's per-cell record ends up
sharing the `_PanelSlot` shape or needs its own is left open for when
architecture piece 2 (per-cell config/descriptor) is actually built.

**Stage 1c increment 1 — gear/Tabs skeleton — implemented and tested July
31 2026, `visibility_plotter.py` only, iterated in three passes.** What
shipped:

- One `TabPanel` per slot (built by iterating `self._slots`), inside a
  single `Tabs` widget that starts genuinely empty (`tabs=[]`) rather than
  pre-populated with both slots. Each slot's gear `CustomAction` (dark-mode
  hand-authored gear SVG, no third-party icon dependency) injects only its
  own `TabPanel` into `tabs.tabs` on click — a slot never appears in the
  header strip until its own gear has actually been used. Re-clicking an
  already-open gear brings it to front without rebuilding/duplicating it.
  (First version pre-populated both `TabPanel`s up front; revised after
  testing showed this defeated the "only reveal what's needed" goal — see
  the declined-shared-drawer note above for the related design reasoning.)
- On gear click, the target figure's title is fully replaced (not
  prefixed) with a plain "Panel A"/"Panel B" label in
  `_EDIT_TITLE_COLOR` (`#f38ba8`, reusing the existing raster
  axis-conflict accent rather than a new ad hoc color) — signals which
  on-screen plot a given tab belongs to and that it's in an unfinished
  state. (First version prefixed the real title with `"[Panel X] "`;
  revised after testing showed this collided with the app's own existing
  bracket convention for axis info, e.g. `"...[Time vs Channel]..."`, and
  wasn't attention-grabbing enough on its own.)
- Each `TabPanel` contains a Cancel button that restores that slot's
  captured pre-edit title text+color and removes only that slot's tab —
  "as if the gear had never been clicked" — hiding the whole widget if it
  was the last one open. Per-slot original text+color are held in a small
  `ColumnDataSource` (`self._panel_title_state[slot.id]`), written once by
  the gear click and read by both Cancel and the Plot-success handler —
  same cross-callback-shared-state idiom this file already uses elsewhere
  (e.g. `_state_source`), needed because a plain Python dict would not be
  shared live across separately-serialized `CustomJS` callbacks.
- A successful Plot ▶/Reload ↺ hides and fully empties `tabs.tabs`
  (`resp.status === 'ok'`, a field that already existed on every
  `_handle_plot()` response but was previously unused client-side), and
  restores title color for whichever slot(s) were actually open (gated on
  `tabs.tabs` membership, so an untouched slot's color is never reset).
  Title *text* needs no special handling on success — Python already
  resends fresh `resp.raster_title`/`resp.scatter_title` on every
  successful plot, which naturally supersedes whatever the gear had put
  there.
- **Not implemented:** decision 9's "validation error auto-switches to
  the offending tab." `resp.status === 'error'` correctly leaves the
  widget alone (open tabs stay open, still in their edited state), but
  nothing switches focus to whichever tab caused the failure —
  `_handle_plot()` doesn't currently tag `status_text` with which
  slot/axis failed, so there's no signal to switch on. Needs its own
  increment (a small `_handle_plot()` change) before it's buildable.

**Bug found and fixed while building this — a real Bokeh rendering gotcha,
worth remembering for any future widget work in this codebase, not just
this button.** Cancel's title-text restore was silently not appearing
on-screen (title stayed on the red "Panel A" placeholder) despite the
underlying model property being provably set correctly (`console.log`
confirmed the right value was being assigned) and despite explicit
`change.emit()` calls at both the `Title`-object and figure-object level —
neither made any difference. It self-corrected the moment the mouse
entered the plot, which was the tell: the value was right, the paint just
wasn't happening yet. Root cause (best-supported theory, not fully
confirmed from outside a live browser): Cancel's title change is followed,
in the *same synchronous tick*, by removing an item from `tabs.tabs` — a
heavier `Tabs`-view rebuild than Gear's `tabs.tabs.concat(...)` (adding an
item), which renders fine. If Bokeh batches pending layout-invalidation
requests at the document level rather than per-widget, the heavier
removal-driven layout pass can starve or overwrite the figure's own
pending title-layout invalidation queued in the same tick — and any
later browser event (mouse movement) simply gives it a chance to catch up
on whatever got dropped. **Fix:** defer the `tabs.tabs` removal itself by
one tick via `setTimeout(fn, 0)`, so it no longer competes with the
title's own layout update in the same synchronous block. Confirmed this
resolves it. **Takeaway for future work in this file:** a property change
that affects a figure's *layout* (title text — which affects title
height — unlike title *color*, which doesn't) is at risk of being
silently dropped if it shares a synchronous tick with another
layout-invalidating change elsewhere in the same document, particularly a
widget *removal* (heavier than addition). Grid mode's pagination/removal
work (architecture piece 3) is a plausible place this could resurface —
worth checking for the same symptom (a value is correctly set but doesn't
visibly update until some unrelated later interaction) before assuming a
data bug.

**Groups 1+2 rework (position-indexed display, all-four-panel construction
wiring) — implemented and tested July 31 2026, `visibility_plotter.py`
only.** Confirmed against live app: no observable behavior change, as
expected for a behavior-preserving refactor.
Prompted by a direct question before starting: is a Kind selector actually
buildable yet? No — `_build_plot_area()` places `self._raster.layout`/
`self._scatter.layout` at fixed screen positions, correct today only
because slot A defaults to raster and slot B to scatter. That's the real
prerequisite gap, not a parallel task, so this landed before any Kind
selector work.

Surveyed all ~65 `self._raster`/`self._scatter` references and split them
into three groups, not one uniform "kind → slot" rename:

- **Group 1 (positional — screen position, not kind):**
  `_build_plot_area()`'s container placement/visibility/sizing, the
  toolbar drag/scroll sync between whichever two figures are currently
  displayed, and — extended beyond the original two-item scope, for
  consistency — `layout_js` (the One/Side/Over control) and the preset
  buttons' (vplot/radplot/waterfall) visibility+sizing logic, since
  leaving those kind-indexed while `_build_plot_area()`'s init became
  positional would have been a real inconsistency, not just an
  aesthetic one.
- **Group 2 (construction-time setup — all four panel objects, not just
  the two currently kind-active):** `sizing_mode` (figure + layout),
  `toolbar_location`, dark-mode initial styling, the dark/light toggle's
  live re-theming, flag-tool `notify_div`/`status_div` wiring, and —
  the one with real latent-bug consequences — `register_select_callback`.
  Previously only the two active objects got any of this; the other two
  (already constructed per decision 9/11, just not yet displayed) would
  have appeared with Bokeh's raw default styling and *no* box-select
  wired for flagging the moment a future Kind selector activated them.
- **Group 3 (deliberately untouched, correctly deferred):** everything
  that depends on the still-unmade per-slot-axis-controls design
  decision — `_handle_plot()`, `doPlot()`'s response handling, the
  cursor-span crosshair correlation logic (genuinely kind-based: it
  matches raster's axis labels against scatter's, which is meaningful
  regardless of screen position, so it correctly stayed
  `self._raster`/`self._scatter` rather than becoming positional), and
  the sidebar's raster/scatter axis-control sections and colormap
  widgets.

**New infrastructure added:** `self._pos0`/`self._pos1` properties
(position-indexed, via `self._slot_display_order`) as the deliberate
counterpart to the existing kind-indexed `self._raster`/`self._scatter` —
not aliases, two genuinely different axes now that display order and
kind are decoupled. `self._all_panels` (flat list of all four objects)
for Group 2's uniform setup. `self._slot_display_order = [0, 1]` itself —
see the swap-groundwork note below.

**Bonus fix, found while doing this (not scope creep — directly enabled
by the same infrastructure):** the Stage 1c increment 1 gear-tool loop
had been paired as `(self._slots[0], self._raster), (self._slots[1],
self._scatter)`, documented at the time as a "positional shim" that
"stops being correct the moment kind-switching is wired up." That shim is
gone — `self._slots[i].active` (Stage 1b.5) already gives "whichever
object *this specific slot* currently shows," which is what gear/Cancel
actually need. Genuinely correct now, not a shim scoped to today's fixed
defaults.

**Zero-recompute panel swap — groundwork laid, trigger not built.**
Raised as a question before this rework started: given RGBA content is
expensive to compute but each slot already caches both kinds' rendered
output regardless of which is currently displayed, should "moving" Panel
A and Panel B's screen positions be supported without recomputing?
**Decided: yes**, and confirmed to cost nothing extra given this rework
was happening anyway — a swap is a pure layout reorder (reverse
`self._slot_display_order`, reassign the container's `.children` array
client-side, same whole-array-reassignment pattern already proven
correct for `tabs.tabs`), not a data operation. Agreed explicitly:
**not gated by Plot ▶** — since it's genuinely free (no comm round-trip
needed at all), staging it behind Plot ▶ would mix a zero-cost operation
in with expensive ones for no benefit. **"Panel A"/"Panel B" stays bound
to slot identity, not screen position** — settled by implementation
convenience rather than as a tiebreaker: everything built in Stage 1c
increment 1 (`_panel_title_state["A"]`, `_panel_tabpanels["A"]`, each
gear's own `"Configure Panel A"` description) already treats "Panel A"
as a stable identity; making it mean "whatever's currently on the left"
would require rewriting all of that on every swap, and would need this
exact same slot↔position indirection anyway, just used to actively
relabel instead of left alone. **Not built yet:** the actual swap
trigger (a UI placement question — toolbar icon vs. per-tab control —
deliberately left open, its own small follow-on) and the swap operation's
JS itself. **Flagged explicitly:** a swap is itself a layout-invalidating
operation, structurally similar to the `tabs.tabs` removal that caused
the Cancel same-tick collision bug above — when the swap trigger is
actually built, check for the same symptom before assuming it's safe
just because it's "just a reorder."

**Group 3 piece 1 — per-slot config panels + Raster/Scatter switch —
implemented and tested July 31 2026, `visibility_plotter.py` only.**
Landed after the per-slot-axis-controls design conversation settled on:
one raster config panel and one scatter config panel per slot (mirroring
decision 11's own "structural cost paid regardless of visibility"
precedent for the plot objects themselves, one layer up), a
`RadioButtonGroup` switch per tab matching the Layout control's own
visual language, and — confirmed explicitly — the switch is pure
client-side navigation, not a trigger: it only toggles which panel is
visible; the actual kind-switch is read and applied later, at Plot ▶
time, by `_handle_plot()`'s rewrite (piece 3, not yet built). No separate
"pending kind" tracking needed — the switch's own `.active` value at
Plot-press time *is* the pending kind, which falls out for free
specifically because switching was designed as batched rather than
immediate.

Two new builder methods, `_build_raster_config_panel(slot, dark)` and
`_build_scatter_config_panel(slot, dark)` — kept separate rather than one
kind-parameterized builder, since raster and scatter genuinely have
different fields (min/max via `colormap_controls()` on raster, absent on
scatter, was the concrete example that settled this). Both called in a
loop over `self._slots`, and storage follows the same `slot.id`-keyed
dict convention as `self._panel_title_state`/`self._panel_tabpanels`
(`self._panel_axis_widgets[slot.id][kind]`,
`self._panel_kind_switch[slot.id]`) rather than named attributes —
explicit design goal confirmed before starting: **build for N, ship for
2** — the config-panel mechanism itself should make a third slot a
non-event, even though the surrounding duo-mode scaffolding
(`self._pos0`/`self._pos1`, the fixed Layout control, swap groundwork
above) stays intentionally 2-slot-specific for now.

The per-slot raster Y/X axis-conflict check also had to move from the
single shared `self._notify_div` to an inline `Div` scoped to each panel
— flagged when the design was discussed, not an afterthought: a shared
message can't disambiguate which tab it's about now that both tabs can
be open with independent raster configs at once.

**Not yet done, expected:** the old global `self._raster_axis_section`/
`self._scatter_axis_section` sections are still present and still what
actually drives `_handle_plot()`/`doPlot()` — piece 1 is purely additive,
so the sidebar temporarily shows both the new per-tab panels and the old
global sections until piece 2 (retiring the global sections) lands.

**Bug found and fixed while building this — a second real Shadow DOM
gotcha, distinct from the same-tick layout-invalidation bug above, worth
remembering for the same reason.** Two follow-on UX issues were reported
after piece 1 landed: (1) clicking a gear left the newly-revealed tab
practically invisible if the sidebar was scrolled elsewhere — nothing
scrolled it into view; (2) switching Raster↔Scatter caused an
unrequested scroll, because the raster panel (with min/max) is taller
than the scatter panel, so toggling between them changes total sidebar
content height, and the browser clamps `scrollTop` to the new max on its
own when that happens near the bottom — not code calling `scrollTo()`,
an incidental side effect of the height change. Both fixes were written
using `document.querySelector('.cv-sidebar')` to locate the sidebar's
DOM element (for scroll-into-view on gear click, and capture/restore
`scrollTop` around the kind-switch toggle) — and **both silently did
nothing** when tested. Root cause: Bokeh 3.x commonly renders widget
content inside a Shadow DOM for style encapsulation, and a plain
top-level `document.querySelector()` cannot see across a shadow
boundary — it returns `null` rather than erroring, so the `if
(sidebarEl)` guards in both fixes simply skipped their bodies with no
visible sign anything had gone wrong. Confirmed via a hand-built mock DOM
with the target element nested two shadow-root levels deep: a plain
`querySelector` failed to find it, while a small recursive helper
(`__cvFindEl`, searches the light DOM first, then recurses into every
`shadowRoot` it finds) succeeded. Fixed by replacing the plain selector
call with this helper in both places; confirmed working live afterward.
**Takeaway for future work in this file:** *any* CustomJS that needs to
reach a specific rendered DOM element by CSS selector — not just this
one — should assume it may be inside a Shadow DOM and use a
shadow-piercing search rather than a plain `document.querySelector()`,
which fails silently (no console error) rather than loudly. This is a
different failure mode from the same-tick layout-invalidation bug above
(that one was about *when* a correctly-targeted change gets painted;
this one is about *whether the target is even found at all*) — both are
now documented so future DOM-touching JS in this file doesn't rediscover
either the slow way.

**Follow-on refinement (added same day):** the scroll-restore fix above
initially always restored the exact prior `scrollTop` regardless of
direction. Reported as still wrong one way: switching Scatter→Raster
(shorter to taller — raster has min/max, scatter doesn't) left newly
revealed lower fields off-screen, since restoring the old position
doesn't account for content that didn't exist yet when that position was
captured. Fixed by comparing `scrollHeight` before/after the toggle
rather than restoring unconditionally: on shrink, restore prior position
(unchanged, confirmed working); on grow, scroll to the bottom instead —
same idea as the gear-click fix, revealing what just appeared rather
than pinning to where the view was before it existed. Compares against
the actual measured `scrollHeight`, not which kind was selected, so it
stays correct if the two panels' relative heights ever change.

**Group 3 piece 3, Chunk 1 — per-slot request/response reshaping,
`_handle_plot()`/`doPlot()` rewritten — implemented July 31 2026,
`visibility_plotter.py` only, implemented and tested July 31 2026.**
Confirmed against the live app: the old global raster/scatter controls
no longer have any effect (expected — nothing reads them anymore, per
the presets/`layout_js` fixes below) and the gear-driven per-slot panels
correctly drive Plot ▶ through the new per-slot request/response path.
Prompted by a
correction to the originally-proposed piece 2/piece 3 ordering:
`doPlot()`'s request-building JS reads directly off the global
`ry_sel`/`rx_sel`/etc. widgets, so retiring those sections (the
originally-planned piece 2) before rewriting the request/response shape
would have broken Plot ▶ outright in the gap between the two — not an
independently-testable increment. Piece 3 has to land first; the doc's
piece numbering below reflects this.

Also prompted by a scope correction on the rewrite's actual goal: the
original framing (retire the global sections once per-slot panels exist)
undersold why per-slot configurability matters. The real point, per
direct clarification: **the only reason to want independent
configurability is to have two rasters or two scatters open at once** —
without that, per-slot config only changes *where* a plot appears, which
isn't worth the doubled-widget-count cost accepted for Group 3 piece 1.
So this rewrite targets genuine same-kind-on-both-slots support, not
just "read from the new widgets instead of the old ones."

That support turned out to require more than `_handle_plot()` alone —
raster and scatter are different Bokeh figure objects (different glyphs,
different axes), not one figure with swappable data, so an actual
kind-switch needs the *layout object occupying a screen position* to
change too, which `doPlot()`'s response handler doesn't yet do (it only
updates data on figures whose identity is fixed at construction).
Broken into two chunks for testability, given the size:

- **Chunk 1 (this one):** reshape request+response+`_handle_plot()` to
  be per-slot, but **explicitly reject an actual kind-switch attempt**
  with a clear error (`"⚠ Panel {id}: switching to {kind} isn't
  available yet in this build."`) rather than attempting a half-built
  one. Behavior-preserving for the untouched default configuration (slot
  A raster, slot B scatter) — the same testing standard as Stage 1b.5:
  confirm nothing changed for a user who never touches the Raster/Scatter
  switch.
- **Chunk 2 (not started):** the layout-swap mechanism itself (both a
  slot's raster layout and scatter layout present in the container,
  visibility toggled by what the response actually rendered — same
  whole-array-reassignment pattern already proven correct for
  `tabs.tabs`) plus relaxing Chunk 1's guard to actually call
  `_activate_slot_kind()` instead of rejecting. This is the piece that
  delivers the real capability.

**What Chunk 1 touched, concretely:**
- `_handle_plot()`: reads `msg["panels"][slot.id]` instead of flat
  `raster_y`/`raster_x`/`scatter_x`/`scatter_y` message keys; loops over
  `self._slots` for both the kind-mismatch guard and the per-slot raster
  Y/X conflict check (previously one global check); builds
  `panels_response` keyed by `slot.id` instead of `raster_*`/`scatter_*`
  response fields. **Bonus fix, not scope creep — directly required by
  the same change:** selection (`panel._selection`) is now applied to
  all four panel objects (`self._all_panels`, Group 2 pattern) rather
  than just the two currently active ones, since a panel Chunk 2 later
  activates needs current selection already applied, not whatever was
  current when it was last constructed or active.
- `doPlot()`'s request-building: reads each slot's kind switch +
  both-kind widget values (`panel0_kind_switch`, `panel0_ry_sel`, etc. —
  named `panel0`/`panel1` for `self._slots[0]`/`self._slots[1]`, same
  positional convention as `panel_a_tab`/`panel_b_tab` elsewhere) and
  sends `panels: {id: {kind, ...}}` instead of flat axis fields. Sends
  the *actual* requested kind honestly even when Chunk 1 will reject it
  server-side, rather than silently coercing it — keeps the client
  truthful and means Chunk 2 won't need to revisit this part.
- `doPlot()`'s response-handling: applies `resp.panels[panel0_id]`/
  `resp.panels[panel1_id]` to `r_fig`/`s_fig` — still valid, unrenamed
  variable names in this chunk, since Chunk 1's guard means kind can't
  actually change yet, so "slot A's figure" is still always the raster
  one.

**Two follow-on breaks found and fixed while making this change, neither
of which `_handle_plot()`/`doPlot()` alone would have caught:**
- **Presets** set the old global `ry_sel`/`rx_sel`/etc. widgets directly
  before calling `doPlot()` — once `doPlot()` stopped reading those
  widgets, presets would have silently done nothing. Fixed to set the
  per-slot widgets instead (`panel0_ry_sel`, etc., matching presets'
  existing fixed raster-on-slot-A/scatter-on-slot-B assumption), and to
  explicitly reset each slot's kind switch to match in case the user had
  switched either away before clicking a preset.
- **`layout_js`'s preference-persistence logic** (remembers side-vs-over
  per axis combination) also read the old global widgets, to build its
  cache key. Once nothing writes to those widgets anymore, this would
  have silently built preference keys from stale, frozen values instead
  of whatever's actually configured — not a crash, a quiet correctness
  bug. Fixed to read the per-slot widgets instead.

**Validation, given no live-MS access:** full syntax check on all four
files; all three touched JS blocks (`_do_plot_js`, the preset JS,
`layout_js`) extracted verbatim from the file and `node --check`'d; a
client-side simulation of the request-building logic (`buildPanelPayload`/
`rasterConflict`) across normal, conflict, and kind-switch-attempted
scenarios; a Python-level simulation of `_handle_plot()`'s two new guard
loops against stub slots, covering the normal case, a kind mismatch, a
raster Y/X conflict, and confirming scatter's own `x==y` correctly does
*not* trip the raster-only conflict check. **Confirmed live** (see the
header above): old global controls correctly inert, gear-driven per-slot
panels correctly drive Plot ▶ end to end.

**Group 3 piece 2 — retired the global raster/scatter sections —
implemented July 31 2026, `visibility_plotter.py` only.** Safe now that
Chunk 1 made them genuinely dead (confirmed live before removal, not
assumed). Removed: `self._ry_select`/`_rx_select`/`_rq_select`/
`_sx_select`/`_sy_select`, the old global raster Y/X conflict check
(`raster_axis_conflict_js`), the old global `colormap_controls()` calls
and `self._raster_cmap_widgets`/`_scatter_cmap_widgets`,
`self._raster_axis_section`/`_scatter_axis_section` themselves, and the
sidebar's "Axes" header (nothing visibly under it anymore — axis
controls live inside each tab now, and `self._gear_tabs` is hidden by
default). Also removed the resulting dead `raster_sec`/`scatter_sec`
references from `layout_js` and the preset JS.

Two things needed fixing as a direct consequence, not incidental
cleanup: `layout_js`'s "one" mode no longer needs to toggle a
`scatter_sec.visible` that doesn't exist (removed); and the dark/light
toggle's `widgets` recolor list, which explicitly named the old global
widgets, needed rebuilding from all four panels' per-slot widgets
instead — the toggle mutates each listed widget's own
`stylesheets[0].css` directly, so anything not explicitly enumerated
wouldn't be re-themed regardless of whether it happens to share the
same underlying `InlineStyleSheet` instance others do. Built as
`self._all_axis_widgets`, flattening all four (slot × kind) widget sets
— 3 raster selects + colormap widgets, 2 scatter selects + colormap
widgets, per slot — rather than assumed-safe sharing.

Also worth noting as a side benefit, not something separately
engineered: the old global `self._raster.colormap_controls()`/
`self._scatter.colormap_controls(layer_index=0)` calls were redundant
with Group 3 piece 1's own per-slot calls on the exact same underlying
objects (`self._raster` *is* `self._slots[0].raster`) — removing the
global call means `colormap_controls()` is now called exactly once per
object instead of twice, closing a latent duplicate-registration risk
that existed (harmlessly, as far as tested) since piece 1 landed.

**Validation:** full syntax check on all four files; all four touched
JS blocks (`layout_js`, the preset JS, the dark/light toggle — content
unchanged there, args only — and a residual scan confirming zero
remaining references to any removed attribute outside of explanatory
comments) extracted verbatim and `node --check`'d; a Python-level
simulation of the new `self._all_axis_widgets`-building loop against
stub per-slot widget dicts, confirming all 16 widgets across both slots
(3+2 selects and their colormap widgets, ×2 kinds, ×2 slots) are
correctly collected. **Confirmed live**: sidebar and Plot ▶/Reload ↺/
presets/dark-light-toggle all work correctly with the global sections
gone. Also confirmed, as an incidental cross-check of Chunk 1's guard
still working correctly on top of piece 2's removal: switching both
slots to the same kind (e.g. two scatter) correctly produces the
"switching to X isn't available yet" error rather than silently doing
nothing or half-rendering — expected, and a good sign the guard and the
now-slimmer sidebar aren't interacting badly.

**Group 3 piece 3, Chunk 2 — the layout-swap mechanism, same-kind-on-
both-slots actually delivered — implemented July 31 2026,
`visibility_plotter.py` only, not yet live-tested.** This is the piece
that delivers the actual point of the whole Group 3 rework: two rasters
or two scatters open at once.

**Design revision made before writing any code, worth recording:**
originally described (to the person, in conversation) as "the same
whole-array-reassignment pattern already proven correct for
`tabs.tabs`." On closer inspection, a simpler mechanism was available
and preferred: Bokeh's `row`/`column` already excludes `.visible=false`
children from the flex layout — proven by this exact file's own "One"
mode, which has relied on `pos1_layout.visible = false` taking zero
space since the Group 1 rework. So instead of swapping which object
occupies a container position, `_build_plot_area()` now builds
`side_container`/`over_container` with **all four** layout objects (both
kinds, both slots) as children from construction, and `doPlot()`'s
response handler toggles `.visible` on the pair matching each slot's
current kind. No `container.children` reassignment needed — `tabs.tabs`
needed that pattern specifically because `Tabs` doesn't have a `.visible`
per-tab concept the way row/column children do; that constraint doesn't
apply here, so the simpler, already-proven mechanism was preferred over
copying a pattern that solved a different problem.

**What changed, concretely:**
- `_build_plot_area()`: container children are now `pos0_slot.raster.layout,
  pos0_slot.scatter.layout, pos1_slot.raster.layout, pos1_slot.scatter.layout`
  (four, not two), each `.visible` set by whether its kind matches that
  slot's current `.kind`. `self._slot_display_order` still decides which
  *slot's pair* is in position 0 vs 1 (unchanged from Group 1) —
  orthogonal to which *kind* each slot is currently showing.
- `_handle_plot()`: Chunk 1's reject-on-mismatch guard is now a real
  switch — on a kind mismatch, calls `self._activate_slot_kind(slot.id,
  requested_kind)` (built back in Stage 1b, never had a caller until
  now), then proceeds with the same axes-changed-checked render logic
  already in place, using the request's actual axis values.
- `doPlot()`'s args: the old fixed `r_fig`/`s_fig`/`r_img_src`/
  `s_img_src`/`r_state`/`s_state` (which assumed slot 0 is always raster,
  slot 1 always scatter) are replaced with both-kind sets per slot
  (`panel0_raster_fig`, `panel0_scatter_fig`, etc. — 16 args total,
  systematic, not hand-picked).
- `doPlot()`'s response handler: for each slot, reads `resp.panels[id].kind`
  (what `_handle_plot()` says actually rendered this round — not assumed
  from a fixed binding), picks the matching fig/img_src/state, applies
  the response data to it, and toggles `.visible` on that slot's raster
  and scatter layout objects to match.

**Double-render tradeoff, decided rather than engineered around:**
`_activate_slot_kind()`, when a panel has genuinely never been rendered,
force-renders using that panel's *stored* (construction-time) axis
defaults — not this request's values. If the request also specifies
different axes for that same first activation, the subsequent
axes-changed check (already in place from Chunk 1) correctly re-renders
with the real values — meaning a first-time activation with
simultaneously-different axes renders twice. Decided to accept this as a
narrow, first-activation-only cost rather than complicate
`_activate_slot_kind()`'s contract to avoid it — confirmed via
simulation (below) that a *repeated* switch back to an already-rendered
kind correctly triggers zero extra renders, reusing the cached result
via the same axes-changed check, which is the case that actually matters
for the "expensive to recompute" concern this whole rework started from.

**Three follow-on gaps found and explicitly flagged, not fixed in this
chunk** — all three share the same root cause: something bound once, at
construction time, to "whichever object is active," which goes stale the
instant a kind switch actually happens:
- **`layout_js`/preset figure-sizing** (`pos0_fig`/`pos1_fig` in the
  Layout radio and preset buttons) still reference construction-time-fixed
  figures. Clicking "Side by Side"/"Over-Under" or a preset after an
  actual kind switch will resize the wrong (now-hidden) figure, leaving
  the visible one unaffected.
- **Gear tool's red-title targeting** (`slot.active.figure` resolved once
  at `_build_sidebar()` construction time) will label the wrong (hidden)
  figure's title after a kind switch, not the currently-visible one.
- **Cursor-span crosshair linking** (`self._raster`/`self._scatter`,
  correctly left kind-indexed per the comment in `_build_plot_area()`)
  degrades once both slots can hold the same kind — `self._raster`'s
  fallback-to-first-match behavior (documented since Stage 1b) picks an
  arbitrary one of two rasters rather than a well-defined "the" raster.
  **Resolved same day — see the "Generalized cursor-span tracking" note
  below.**

**Generalized cursor-span tracking to N panels — implemented July 31
2026, `visibility_plotter.py` only, not yet live-tested.** Closes the
cursor-span gap flagged just above. Live-tested confirmation of Chunk 2
surfaced this directly: after switching a slot's kind, cursor tracking
was lost entirely in the newly-active panel, and cross-panel tracking
stopped working altogether.

**Design correction made explicit in conversation, not just inferred:**
the old rule was "raster syncs with scatter" — fixed two-role, and
really just a specific case of a more general rule that only had two
roles to apply to. Revised: **any panel whose cursor moved syncs with
every *other* panel that shares at least one matching axis dimension** —
X-vs-X, X-vs-Y, Y-vs-X, Y-vs-Y all checked independently, so a panel
matching on both dimensions gets both its vertical and horizontal span
set, not just the first match found (confirmed as the intended behavior,
not assumed).

**What changed:** every panel object (`self._all_panels`, all four, not
just the two previously kind-active) now gets its own vertical+
horizontal span pair unconditionally at construction — same "structural
cost paid regardless of visibility" pattern as decision 11/Group 2 (flag
tools, `register_select_callback`, dark styling). The old raster/scatter
2-branch JS (four hardcoded span variables, duplicated logic per
direction) is replaced with one generic function taking a list of
`{id, fig, vspan, hspan}` descriptors and looping: reset all, find which
panel's `vr_id` matches the cursor source's `fig` field, always show
that panel's own spans, then for every *other* panel independently check
its X and Y axis labels against the source's X and Y labels.

The underlying mouse→`cursor_source` wiring needed no changes — already
set up per-instance in `VisibilityPlot._add_comm_hover()` (the shared
base class), called on every panel object regardless of active/visible
status, using each panel's own unique `vr_id`. Only the span-*display*
side (which panels have spans, and the matching logic) needed
generalizing — confirmed by reading the base class before assuming
anything needed touching there.

**Validation, given no live-MS access:** full syntax check on all four
files; confirmed via a real Bokeh install that `CustomJS.args` accepts a
list of dicts containing model references (the `panels` arg shape) as a
single value; the rewritten JS extracted verbatim and `node --check`'d;
a JS-level simulation across two scenarios — four panels with a mix of
matching/non-matching axes (confirming X-vs-X, X-vs-Y matches all
resolve correctly and non-matching panels stay untouched) and a panel
matching the source on *both* axes (confirming both its spans get set
independently, not just the first). **Not yet live-tested.** (This
closes out the Chunk 2 validation record above too, which was
interrupted mid-sentence by this insertion — restored below for
continuity: Chunk 2's own validation continued with the
`row`/`column`-with-four-children construction validated against a real
Bokeh install; the complete rewritten `_do_plot_js` extracted verbatim
and `node --check`'d; a JS-level simulation of the response handler
across two scenarios — switching both slots to raster simultaneously
and switching back to scatter — confirming the correct figure is
picked, the correct `.visible` pair is toggled, and an untouched slot's
response absence correctly leaves it alone; a Python-level simulation of
`_handle_plot()`'s switch logic confirming both the single-render and
double-render outcomes occur exactly as designed.)

**Four bugs found and fixed during live testing of Chunk 2 + cursor-span
generalization — August 2 2026, `visibility_plotter.py` only, all
confirmed live.** Surfaced by actually exercising same-kind-on-both-slots
for the first time; all four share a root cause with the three gaps
already flagged above — something computed once, either at construction
or from an incomplete signal, going stale or wrong once kind can
genuinely change at runtime.

1. **Gear tool missing entirely after a kind switch** (not just
   mistargeted — actually blocking further testing, since there was no
   way to reopen config once switched away from a slot's original kind).
   Root cause: gear was attached to `slot.active.figure`, evaluated once
   at construction — the figure that becomes visible after a switch
   never had a gear added to its toolbar at all. Fixed: one gear per
   *kind* per slot (two, not one), each bound to its own figure. Since
   only the currently-visible figure's toolbar is ever clickable, this
   also incidentally closed the "gear-tool title targeting" gap flagged
   under Chunk 2 above — there's no wrong figure to target anymore, each
   gear only ever fires from the one it's permanently attached to.
2. **Stale red title when the kind switch was changed *within* an open
   gear session before Plot was pressed.** The success handler used to
   restore color on `p0_fig`/`p1_fig` — whichever kind the *response*
   said rendered — not whichever figure was actually gear-clicked. Fixed
   by having `gear_click_js` record which kind it represents into
   `orig_source.data['kind']` at capture time; both the success handler
   and Cancel now read that directly instead of inferring from the
   response or from layout visibility.
3. **Client crash (`Range1d.start` given `null`) when both slots
   switched to raster in the same request.** `self._last_raster_selection`
   was a single attribute shared across the whole app; slot A's
   processing (first in `_handle_plot()`'s per-slot loop) would update
   it, corrupting slot B's own "did selection change" check later in the
   same call. Compounded by `_activate_slot_kind()`'s force-render using
   each panel's *stored* construction-time axis defaults — since both
   slots' raster objects were originally constructed with the same
   global defaults, a newly-activated panel's axes often already matched
   the incoming request, so even the Y/X/qty comparison alone could miss
   it. Fixed with two changes: `self._last_raster_selection_by_slot`
   (per-slot, not shared) and an explicit `switched_kind_this_round` set
   — a direct guarantee that any slot whose kind actually changed this
   round always sends full range data, not solely inferred from the
   axis/selection comparisons happening to catch it.
4. **Title text stuck on the gear placeholder ("Panel A") even after a
   successful Plot, with color correctly restored.** The success
   handler's text restoration was only ever a side effect of the
   unconditional `if (p0.title != null) p0_fig.title.text = p0.title`
   line — which only fires when that panel's axes actually changed. If a
   gear tab was open but that panel's own axes genuinely didn't change
   before Plot was pressed, no fresh title arrives, yet color still
   resets unconditionally. Fixed by falling back to `orig_source`'s
   captured original text whenever a fresh title wasn't actually applied
   to the edited figure this round — condition generalized to
   `p_title_applied = (p && p.title != null && p_kind === edited_kind)`,
   which also correctly covers bug #2's kind-switch scenario for text,
   not just color.

All four validated via targeted JS/Python simulations reproducing the
exact reported symptom before and after the fix (not just "should work"
reasoning) — see the conversation record for each simulation's output.
**Confirmed live** by the person for the full sequence: two-raster
switch, two-scatter switch, gear-driven config on both panels
simultaneously, Plot success, Cancel, and the text/color restore path.

**`layout_js`/preset figure-sizing gap closed — August 2 2026,
`visibility_plotter.py` only, live-confirmed.** Closes the last of the
three gaps originally flagged when Chunk 2 landed. Turned out to be two
related bugs, not one, both with the same root cause as the four bugs
fixed earlier the same day: a figure/layout reference captured once at
construction (`self._pos0.figure`/`.layout`), going stale the instant an
actual kind switch happens.

1. **`layout_js` (the One/Side/Over control)**: sizing was wrong after a
   kind switch, same as flagged. Fixed by giving it both kinds' fig/
   layout per position and resolving dynamically at click time — checking
   which of a position's two layout objects is currently `.visible`, the
   same signal Chunk 2's response handler and Cancel already treat as
   ground truth for "what's actually showing."
2. **Presets**: a related but more subtle bug found while fixing #1, not
   originally flagged. Presets always force a fixed target
   (pos0=raster, pos1=scatter) via `panel0_kind_switch.active = 0` etc.,
   so they don't need `layout_js`'s dynamic resolution — but the old code
   only set the *target* layout's `.visible = true` without explicitly
   hiding whichever kind had actually been showing before. If a slot had
   been switched away from the preset's assumed kind, both could appear
   stacked simultaneously for one frame, until `doPlot()`'s async
   response settled the correct final state a moment later. Fixed by
   setting the complete correct end-state (target visible AND other kind
   hidden) synchronously, for both positions, rather than leaving half of
   it to resolve asynchronously.

Both validated with simulations reproducing the exact previously-broken
scenario: `layout_js` against a slot that had been switched away from its
construction-time kind (confirmed it now resizes the actually-visible
figure, and leaves the stale one completely untouched); presets against
the same switched-away scenario (confirmed the old code produces the
transient double-visible state and the new code sets the complete correct
end-state with no window for it). **Not yet live-tested** — worth
confirming One/Side/Over and all three presets still behave correctly
after a real kind switch, both fixes.

**Scatter's always-recompute inefficiency fixed — August 2 2026,
`visibility_plotter.py` only, confirmed live (indirectly, via the panel 1
corruption fix below — that bug only appeared because this fix started
correctly sending `null` range fields for an unchanged scatter panel, and
Panel B correctly displaying its cached, untouched content once the
corruption was fixed confirms the skip-recompute path itself is working
end to end).** Explicitly requested
as the first of several flagged-but-optional remaining issues, given its
direct connection to the recompute-cost concern that motivated this whole
rework in the first place: two scatter panels can now exist
simultaneously, and until this fix each one fully recomputed on *every*
Plot press regardless of whether anything about it actually changed —
doubling the wasted work the original single-scatter version already had
(flagged as a pre-existing asymmetry when Chunk 1 first touched this
code, not fixed at the time).

Mirrors raster's `axes_changed` gating exactly, adapted for scatter's
different internal shape: scatter has no single `_y_dim` attribute (its Y
axis lives inside each `ScatterLayer`, one per polarization, all sharing
the same `y_axis` per `_make_scatter_layers()`), so the comparison reads
it off the first existing layer. `never_rendered` mirrors
`_activate_slot_kind()`'s own never-rendered check (all `layer_dfs`
`None`) for the same reason raster needed it: a never-rendered panel's
stored dims could otherwise coincidentally match the request and wrongly
skip its first real render — exactly the class of bug fixed for raster
earlier the same day. New `self._last_scatter_selection_by_slot`,
per-slot from the start (no shared-attribute version ever existed for
scatter to begin with, unlike raster's). Response fields
(`x0`/`x1`/`y0`/`y1`/labels/title/state) are now conditionally `None`
when unchanged, same as raster — required no changes to `doPlot()`'s JS
response handler, which was already fully kind-agnostic (generic
`!= null` guards throughout, never assumed scatter's fields were
unconditionally present) — including the text-restore fallback fix from
earlier the same day, which now also correctly applies to scatter.

Validated with a Python-level simulation extracted from the real logic,
covering five scenarios: nothing changed (correctly skips — the actual
point of the fix), Y axis changed, correlation selection changed, never
rendered, and kind switched this round with axes that happen to
coincidentally match stored state (correctly still forces a render,
avoiding the same null-range bug class fixed for raster).

**Panel 1 image corruption on unchanged scatter — found and fixed August
3 2026, `visibility_plotter.py` only, confirmed live.** Directly
exposed by the scatter recompute-gating fix above, though the underlying
defect predates it. `doPlot()`'s response handler has separately-written,
near-identical blocks for panel 0 and panel 1; panel 0's has always
correctly split the always-sent `image` update from the conditionally-
sent range (`x0`/`x1`/`y0`/`y1`) update into two separate checks. Panel
1's combined them into a single `if (p1.image != null)` — silently
harmless as long as scatter always re-rendered unconditionally (so `x0`
was never actually `null`), but the moment scatter gained its own
change-detection, an unchanged scatter panel correctly started sending
`x0: null` — and panel 1's combined check applied that `null` straight
to the image's position/size data (`x`, `dw`, `dh` — `null - null`
coerces to `0` in JS, not `NaN`), zeroing its width/height and making it
disappear. Reported live: changing only Panel A's (raster) quantity and
pressing Plot correctly re-rendered Panel A but made Panel B (unchanged
scatter) go blank. Fixed by splitting panel 1's block to match panel 0's
already-correct structure. Validated with a simulation reproducing the
exact scenario — confirmed the old handler zeroes Panel B's image
dimensions, the new one correctly leaves its existing valid data
untouched. **Confirmed live** — the person reported the blanking issue
resolved.

**Swap feature — designed and implemented August 3 2026,
`visibility_plotter.py` only, not yet live-tested.** Delivers the
zero-recompute panel-position swap discussed since Group 3's earliest
design conversations, when the recompute-cost concern first came up.

**Design history, worth preserving — this went through several real
revisions in conversation, not a single settled proposal:**

1. Original proposal: a per-tab "Move to" dropdown, options being every
   *other* currently-open panel, with conflict prevention (once two
   panels are paired, they're removed from other panels' option lists).
2. Revised: conflict prevention dropped once swaps were confirmed to be
   automatic and instant (not staged behind Plot ▶) — a swap is its own
   inverse, so there's nothing to protect against.
3. Labeling question, worked through in stages:
   - Considered labeling dropdown options by content (each panel's
     current title) — rejected once it was pointed out two panels could
     have genuinely identical titles (e.g. two rasters with the same
     axes), making options ambiguous.
   - Considered `numpy`-style coordinate labels (`[i,j]`) instead —
     matches this team's actual working vocabulary. Initially proposed
     as a fixed `(2,1)` column-vector shape regardless of Layout mode —
     **corrected**: the valid coordinate set genuinely depends on which
     Layout mode is active (`[0,0]`/`[0,1]` in Side-by-side — row fixed,
     column varies; `[0,0]`/`[1,0]` in Over/Under — column fixed, row
     varies; `[1,1]` invalid in duo mode either way, "one panel short of
     2×2"). Confirmed against four worked examples before being accepted
     as correct. "One" mode's coordinate was settled as simply `[0,0]`
     always (trivial, no ambiguity) with the *other*, hidden position's
     control disabled rather than guessing a convention for it.
4. **Final simplification**: since duo mode only ever has exactly one
   possible swap target, all of the above (coordinate labels, Layout-
   mode-dependent recomputation, per-tab option lists) collapses to
   "there is only one action, so offer a single button that does it,"
   labeled plainly "Swap." Explicitly placed *inside* each tab (not a
   toolbar icon) specifically because a future N-panel version of this
   control (the coordinate/dropdown scheme from step 3, not thrown away)
   has to live there — different panels need different available targets,
   which is inherently a per-panel concern.

**What's actually N-ready versus duo-specific, worth being precise
about:** the underlying mechanism — `self._display_order_source`, a
reorderable *list* (not a boolean), and the container-children-rebuild
logic — is exactly what a future coordinate/dropdown scheme reuses
directly. Only the *trigger widget* (one button, unconditional swap) is
duo-specific and would be replaced by the dropdown scheme once N>2 —
same category of "duo-only UI over N-ready plumbing" as the Layout
control itself, not a new kind of throwaway work.

**Implementation:**
- `self._display_order_source` (`ColumnDataSource`, `{"order": [[0,
  1]]}`) — the live, client-side-mutable tracker. Deliberately separate
  from `self._slot_display_order` (Python-side, fixed, read only at
  construction) — the swap never touches the Python attribute, since
  nothing at runtime reads it; only the JS-side tracker and the actual
  container children need to change, keeping the swap genuinely
  comm-free.
- One `Button` + shared `swap_js` `CustomJS` per tab (two Button
  instances, one shared code string — mirrors how `gear_click_js`/
  `cancel_click_js` are already single strings reused across instances).
  Reverses `display_order_source.data['order']`, then rebuilds
  `side_container`/`over_container`'s children from the two slots' full
  (raster, scatter) pairs in the new order — whole-array reassignment,
  the same pattern already proven for `tabs.tabs`.
- **"One" mode sizing gap closed** (flagged as a known dependency of this
  feature since it was first discussed): `layout_js` now resolves which
  slot is primary by reading `display_order_source` at click time,
  instead of the construction-time-fixed `_pos0_slot`/`_pos1_slot` it
  used before. Presets are correctly untouched — they never use "One"
  mode (only Side/Over, which size both positions identically regardless
  of which slot is where) and always force their own fixed slot0=raster/
  slot1=scatter assignment independent of any swap, so nothing about
  them needed to change.
- Gear tools, Cancel, `doPlot()`'s response handler: confirmed to need
  zero changes — all three were already slot-identity-based (`self._slots[0]`/
  `self._slots[1]` directly, or looping `for slot in self._slots`), a
  deliberate choice made when gear/Tabs was first built ("Panel A stays
  bound to slot identity, not screen position," settled by
  implementation convenience at the time) that happens to also make them
  automatically swap-safe now, for free.

Researched before committing to the button/dropdown approach at all:
confirmed no built-in Bokeh mechanism or small known add-on exists for
drag-and-drop reordering of entire layout panels (the one drag-related
capability found in the Bokeh ecosystem is for repositioning annotations
*within* a single plot's canvas — a different feature entirely).

**Validated, given no live-MS access:** full syntax check; `swap_js` and
the updated `layout_js` extracted verbatim and `node --check`'d; a
simulation running a full cycle — swap, confirm the container children
reorder correctly and "One" mode's resolved primary figure correctly
follows to the new slot (the specific gap this closes), swap again,
confirm full reversibility back to the original state. **Not yet
live-tested.**

**Tab row layout + tooltips — August 3 2026, `visibility_plotter.py`
only, not yet live-tested.** Two small polish items reported after
confirming the swap feature works. Row overflow was arithmetic — the
combined widget widths (120+80+60=260px) exactly equal `_SIDEBAR_WIDTH`
with no margin for borders/padding, so a single row was never going to
fit once Swap was added. Split into two rows: mode selection (Raster/
Scatter switch) on its own, Cancel + Swap together below. Tooltips added
by promoting the toolbar's existing local `_tt()` helper (previously a
nested function only reachable inside `_build_toolbar()`) to a proper
`self._tt()` method, so `_build_sidebar()` could wrap the tab's three
widgets with `Tip(...)` using the exact same convention already proven
throughout the toolbar, rather than a second copy of the same helper.
**Not yet live-tested.**

**`colormap_controls()` confirmed non-functional in this app's actual
architecture — found August 3 2026, not fixed (explicitly deferred to
the reference-testing phase by the person, not an oversight).**
`min`/`max` `TextInput`s (both `visibility_raster.py` and
`visibility_scatter.py`) are constructed and placed in the layout but
never given any callback at all — pure visual mockup. The "Color
scaling" `Select` and alpha/gamma inputs are worse: they use Bokeh's
native `.on_change()` server-side property-change callback, which can
only ever fire with a live Bokeh server running the Python event
loop — an architecture this project deliberately doesn't have (the
entire `CommMgr`/comm design exists specifically because there is no
Bokeh server). A fully correct, properly registered comm handler already
exists for this (`_handle_update_scaling_raster`/`..._scatter`,
`self._comm.register(self._msg_update_scaling, ...)`) but nothing on the
client side ever sends the message that would invoke it — likely built
and tested at some point against a `bokeh serve` environment, where
`.on_change()` would have appeared to work, before the app's real
deployment model solidified around comm-only. Real fix needed:
`js_on_change` + comm wiring to replace `.on_change()` for
scaling/alpha/gamma, plus building `min`/`max` from nothing. Deliberately
not attempted now — this is exactly the kind of thing the person's
planned reference-testing phase (comparing against known PlotMS/msview
results) exists to catch systematically, rather than patching piecemeal
each time something looks suspicious.

**Two light-mode gaps fixed — August 3 2026, `visibility_plotter.py`
only, not yet live-tested.** Both found by the person, neither covered by
any existing part of the dark/light toggle mechanism, for two different
underlying reasons: (1) the seven config-field hint divs
(`self._hint_field`/`_scan`/`_antenna`/etc.) use plain `Div.styles` with
a hardcoded dark-only color, never reachable by the generic recolor loop
(which only touches widgets using `.stylesheets`) — fixed by extending
the same `recolor_div()` pattern already proven for `status_div`/
`notify_div`; (2) the source file path's color is baked into an inline
HTML `<span style=...>` *inside* `path_div.text` itself, which no
property-based mechanism can reach at all — fixed by rebuilding the text
on toggle instead of restyling it (black in light mode per the specific
request, green preserved in dark mode). Validated with a simulation
running a full dark→light→dark cycle. **Not yet live-tested.**

**Updated design for the future N-panel swap trigger — August 3 2026,
recorded, not built.** Supersedes the coordinate-labeled dropdown scheme
from the "Swap feature" note above as the leading presentation-layer
design, proposed by the person: a small grid of buttons mirroring the
*actual on-screen arrangement* (two side by side in Side-by-side mode,
two stacked in Over/Under), with each tab's own current-position button
disabled and every other button moving that panel there on click.
Genuinely more intuitive than parsing coordinate labels — and the
"current position disabled" convention elegantly encodes "no swap" for
free, without needing an explicit option for it the way the dropdown
scheme did. Shares the same open dependency the coordinate scheme had,
just shifted to a different representation: needs mode A's eventual
small-N *geometric* layout convention decided first (a future 3- or
4-panel arrangement — row? L-shape? 2×2 with one empty? — not yet
designed). Scoped explicitly to mode A's future N-panel extension, not
grid/iteration mode (decisions 2–8), which has a genuinely different
uniform-config model and may not need a per-cell "swap" concept in the
same sense. The underlying mechanism
(`self._display_order_source`-style reorderable list) is unchanged by
this — only the trigger widget's presentation would differ from what's
built today.

**Validation-error auto-switch-to-tab — implemented August 3 2026,
`visibility_plotter.py` only, not yet live-tested.** The last piece of
decision 9's originally-settled gear/Tabs design, built once the
question "is it actually easy to know which panel failed?" was checked
against the real code rather than assumed — it was: every one of
`_handle_plot()`'s four current error paths (the kind-mismatch guard,
the raster Y/X conflict check, and both kinds' `except Exception`
catches around `update_axes()`) already runs inside a `for slot in
self._slots:` loop, so `slot.id` was already in scope at every failure
site. Added `"failed_slot": slot.id` to all four; `doPlot()`'s response
handler now switches `gear_tabs.active` to that slot's tab on failure,
opening it first if it wasn't already among the open tabs and expanding
the sidebar if collapsed — deliberately does not hide/reset the tabs the
way a successful Plot does, matching decision 9's design that a failure
leaves things open exactly as the user left them, just focused
correctly. One acknowledged limit, not a gap: a malformed *global*
selection value (field/SPW/scan/antenna/time/UV-range) isn't tied to any
single panel, so this can't help in that case — orthogonal to the
feature, not a shortfall in it. Validated with three simulated
scenarios: switching to an already-open tab, opening a not-yet-open tab
plus expanding a collapsed sidebar, and a graceful no-op when there's no
`failed_slot` (the global-failure case).

**Extended to cover the client-side raster Y/X conflict guard too —
found and fixed same day, during the very first live test.** The person
tested by triggering the conflict on one tab, then switching to the
*other* tab and pressing Plot — nothing switched back, though (correctly)
nothing was sent to Python either. Root cause: `doPlot()` has its own
earlier client-side guard that refuses to send a conflicting request at
all, before `ctrl.send()` is ever called — so this specific error never
reaches `_handle_plot()`, meaning the server-side `failed_slot`
mechanism above never gets a chance to run for it. Same underlying UX
problem the feature was built for, just reached via a different,
client-only path that doesn't produce any server response to react to.
Fixed by factoring the tab-opening/focusing logic out into a shared
`switchToTab()` helper, called from both the client-side guard
(immediately, before any request is sent) and the server-error response
handler (still needed for the kind-mismatch guard and both kinds'
exception handlers, which do reach `_handle_plot()`). Also corrected a
comment from the initial implementation that implied all four
`_handle_plot()` error paths equally reach the client via a server
response — not true for the raster Y/X conflict specifically, now noted
accurately at the check itself. Validated by reproducing the person's
exact scenario in simulation: viewing panel A's tab while panel B has an
unresolved conflict, confirming Plot is still correctly blocked and now
correctly switches focus to panel B. **Not yet live-tested** — the
person's report described the gap, this fix responds to it; still needs
their confirmation it actually works.

With this, every piece of decision 9's original gear/Tabs design is
built. The only remaining open item for the whole duo-mode 2-panel
implementation is `colormap_controls()` (min/max never wired at all;
Color scaling/alpha/gamma wired to a Bokeh-server-only mechanism that
can't fire in this app's actual comm-based architecture — see the note
above), explicitly deferred to the reference-testing phase rather than
patched piecemeal.

### 10. Reserve headless/pipeline export in the parameter surface (revised July 28 2026)

**Decision:** actual implementation is far-future / explicitly out of scope
for now, but the public parameter surface of `VisibilityPlotter`/`visplot()`
should be shaped today — while nothing has shipped and everything is free
to add or remove — so that headless export (duo mode or grid mode) is
possible later without a breaking signature change. Motivated directly by
the PlotMS precedent (scripted PNG export is a core plotms workflow) and
this codebase's existing scripted/task-layer orientation (`sync_layers`,
the CASA task wrapper).

**Output must be fully labeled** (axes, ticks, title, colorbar) — matching
the interactive appearance, not just the raw Datashader-composited pixels.
This resolves last round's "raw vs. labeled" fork in favor of labeled, and
commits any future raster (PNG) implementation to one of: (a) driving
Bokeh's own `export_png` via a headless browser (Selenium/geckodriver —
real dependency + startup cost), (b) a second, independent rendering path
(e.g. matplotlib) that must be kept visually consistent with the
interactive appearance indefinitely, or (c) hand-compositing labels via PIL
around the Datashader raster. Still an implementation-time decision, not
resolved now — flagged so it isn't mistaken for a small addition later.

**Format prioritization — HTML, `.ipynb`, and PNG are not equally hard.**
Three output formats are in view: a labeled raster (PNG), a standalone
printable HTML file, and a Jupyter notebook (`.ipynb`). These are **not**
comparably difficult:

- **HTML is close to free.** The interactive tool's normal output already
  *is* a standalone HTML file (every screenshot in this design discussion
  is one, opened via `file://`). Headless HTML export is close to "save the
  file already being generated to a caller-chosen path," not new rendering.
- **`.ipynb` is moderate.** A notebook embedding story already exists (the
  `anywidget`/Colab comm bridge, `cubevis[notebook]`) for showing the
  interactive widget inline. Producing a `.ipynb` is mostly packaging a
  cell around that existing bridge, not building new rendering.
- **PNG-labeled is the genuinely hard one** — the only format of the three
  requiring a browser-free rendering decision at all (see above).

Given that gap, HTML is the natural first concrete target if/when this work
starts — it validates the config/delivery separation below at close to zero
rendering cost, ahead of the harder PNG decision.

**Iteration: a generator API backed by one open backend, not one
`VisibilityPlotter` per iteration value.** Re-constructing
`VisibilityPlotter(ms=..., antenna=...)` fresh per iteration value would
likely re-open the MS/backend from scratch each time — real I/O and
metadata-parsing cost repeated N times. Instead: open the backend once, and
expose a generator that performs lightweight per-value re-selection —
exactly the same mechanism decision 3 already committed to for interactive
grid pagination (reusing existing panel objects across page turns via
`update_axes()`/selection swap, not rebuilding them). Headless iteration and
interactive grid pagination are the same underlying mechanism with a
different delivery target (yield-to-generator-and-export-a-file vs.
push-to-browser-via-comm), not two things to build separately.

**Scoping note:** this generator does not depend on grid mode existing.
"Iterate a single panel through N selection values, exporting one labeled
file per value" is a headless loop over the *existing* duo-mode render
path — none of decisions 2–8's grid machinery (rows×cols, comm-id
namespacing, page pooling) is required for it. Whether to sequence it ahead
of grid mode is an open scheduling question, not an architectural one.

**Near-term implications for parameter design (do now, cheaply):**

- Keep "what to plot" (MS, selection, axes, mode, and — down the road — duo
  vs. grid layout, iterate-by, grid dims) structurally separate from "how to
  deliver it" (interactive browser session, generator/iteration, or a
  specific static format). Headless export should consume the same
  configuration through a different delivery path, not a second, parallel
  set of arguments.
- Reserve an `output_file`/`output_format`-shaped parameter (or a distinct
  generator-returning method) on `VisibilityPlotter`/`visplot()` now — `None`
  preserves today's interactive-only behavior exactly; the reserved shape
  can raise `NotImplementedError` until it exists. Costs nothing today,
  avoids an API break later.
- Protect, don't extend: the numeric rendering pipeline (`_render()`/
  `_shade_agg()`) already produces a plain numpy RGBA array via Datashader
  before anything Bokeh-specific touches it, which is what makes headless
  rendering plausible at all. The risk to watch for is future interactive
  features (gear tool, sync broadcast, crosshair) leaking assumptions into
  that numeric path that only make sense with a live browser session — no
  code change needed now, just a design invariant to keep in mind while
  building decisions 6, 7, and 9.

**Prototype findings (added July 29 2026) — real evidence, narrows but
doesn't fully close the rendering-path fork.** A quick standalone prototype
(synthetic labeled figure mimicking a raster panel — title, axis labels,
colorbar — not yet fed a real `query_raster`-composited image):

- **Webdriver `export_png` failed completely**, not just "needed a
  dependency." `selenium` installs fine via pip, but selenium itself needs
  an actual browser, and in the test container both Chromium and Firefox's
  default apt packages are snap-only transitional stubs — `snapd` itself
  doesn't function in that environment. This is a reasonable proxy for a
  minimal pipeline/compute-node environment (no desktop session, likely no
  systemd/snapd), which is exactly the deployment context this feature is
  motivated by. Tested in one sandbox, not NRAO's actual cluster
  infrastructure — evidence, not proof — but a concrete risk, not a
  hypothetical one.
- **Matplotlib duplicate-render path worked immediately**, zero extra
  system dependencies (already installed, no browser, no snap). A
  synthetic labeled figure rendered in 0.2s; a 20-panel loop averaged
  **~150ms/panel**. Caveats: synthetic random-noise data, not an actual
  eq_hist-composited image from `query_raster`; no fidelity check yet
  against the interactive Bokeh appearance (fonts, the `6m 20s`-style time
  tick formatting, etc.).

Net effect: matplotlib moves from "one of three equally-weighted options"
to the leading candidate — not because PIL compositing was ruled out, but
because the webdriver option's risk turned out to be more concrete than
"adds a dependency." Worth validating against a real Datashader-composited
image (not synthetic noise) before treating this as settled, and worth
retesting the webdriver path specifically against NRAO's actual target
deployment environment before fully ruling it out — this container's
snap/systemd limitation may not hold everywhere.

**Rendering path chosen: matplotlib (revised July 29 2026).** Two reasons
beyond the prototype passing: it's not a *new* category of dependency for
this domain — CASA/astropy already assume matplotlib is present almost
universally, unlike Selenium+browser, which would be genuinely novel for a
CASA installation to carry. And matplotlib **subsumes** the standalone PIL-
compositing option from the original three-way fork — "hand-roll label/
tick/colorbar layout without a browser" is exactly what matplotlib already
does, via a mature library instead of custom layout code. Dropping PIL
compositing as a distinct option; down to one live path, not three.

**Integration approach to protect the numeric pipeline (not yet built,
recorded now):** feed matplotlib the **already-composited RGBA array** —
the same object the interactive Bokeh `image_rgba` glyph consumes — via
`imshow`, rather than having matplotlib re-derive colors from raw data.
This guarantees the actual pixels are byte-identical between interactive
and headless output and keeps matplotlib's job narrowly "add chrome around
already-correct pixels," consistent with decision 10's original
"protect, don't extend the numeric path" note.

**One genuinely tricky piece, not solved by the above:** an accurate
**colorbar** needs the scalar-to-color mapping function, not just the
pre-colored pixels — and `eq_hist` (histogram-equalized, nonlinear) scaling
isn't a standard linear/log norm matplotlib already knows how to draw.
Reproducing an honest colorbar means either re-deriving the same eq_hist
mapping Datashader used as a custom `matplotlib.colors.Normalize`, or
shipping with an approximate one and being upfront about it. Flagged now so
it isn't discovered mid-implementation.

**Deferred to actual implementation time, not decided now:**

- Full fidelity validation of the matplotlib path against real
  eq_hist-composited raster data and the interactive Bokeh appearance
  (ticks, fonts) — the prototype above used synthetic data only.
- Generator output granularity — one panel per yielded item vs. one
  composed grid-page image per yielded item — the same per-page-vs-per-cell
  question already open for grid export in general (see open questions).
- Exact generator/method shape (e.g. yielding renderable results the caller
  explicitly exports, vs. a convenience method that iterates and writes
  files directly).

**Sequencing revised (added July 28 2026, stakeholder feedback):** a
*functional* (not fully polished) slice of this — the generator/iteration
API plus one working PNG export path — should move earlier in the roadmap,
right after duo mode (step 2) stabilizes and *before* any grid-mode work
(steps 3–7) begins, rather than sitting in a final "polish/completeness"
phase. Two concrete reasons, not just general prudence:

- It's the only practical way to get **real numbers** behind decisions 2
  and 5, both currently flagged as unconfirmed estimates (the 3×3/6×6 grid
  cap, and whether N×M live objects is viable at scale). Benchmarking one
  panel's query→render→export cost lets that cost be modeled at N×M
  *before* committing to build the grid, rather than discovering a
  performance problem after decisions 2–8 are already implemented.
- The choice among the three rendering-path options above is itself
  performance-sensitive — headless-browser startup cost per invocation is a
  known pain point for the Selenium/webdriver approach specifically — so
  the early benchmarking pass should double as the basis for *deciding*
  that fork, not just for producing a plotMS comparison number. Making that
  choice with data now avoids exactly the kind of late-stage rework the
  stakeholder is trying to head off.

This does **not** mean pulling all of Phase 4's export/scripting scope
forward — only enough to benchmark honestly (one working PNG path, not
pixel-perfect label positioning; the generator, not full `.ipynb` packaging
ergonomics). Full completeness stays a later-phase concern; only the
functional-and-measurable slice moves up.

### 11. Defer render, not construction, for inactive panels (added July 30 2026)

**Decision:** the "pre-build everything, toggle visibility" pattern used
throughout this doc (decisions 5, 9) bundles two genuinely separable costs
that should be treated differently:

1. **Construction** — the Bokeh objects themselves (`Figure`,
   `ColumnDataSource`, toolbar, comm registration, hover/flag tools).
   Structural, bounded, hard to avoid without introducing post-load
   construction lifecycle management (a real risk this doc has
   consistently avoided elsewhere).
2. **Render** — actually calling `_render()`, which triggers
   `query_raster()`/`query_columns()` against the backend and Datashader
   compositing. This is where the real cost lives — the thing decision 10's
   benchmarking exists to measure.

Always pay cost (1); never pay cost (2) speculatively. Construct every
object up front as before, but defer its first `_render()` call until the
panel is actually about to become visible/active. This is a genuinely
different, better answer than the binary "pre-build-and-render-all vs.
lazily-construct-on-first-use" framing this doc had been circling —
"no dynamic construction after initial load" stays fully intact (every
object exists from the start), and only the *expensive* part is deferred.

**No new mechanism needed.** `update_axes()`/`_render()` already means
"recompute because what should be shown changed" — that's the same trigger
a Kind switch or a grid page turn needs, just fired from a different UI
event than an axis `Select` change. Users already experience a brief
compute-then-display latency whenever they change axes today, so deferring
render to first-activation doesn't introduce new UX, just one more trigger
for a wait pattern that already exists.

**Application to decision 9 (Stage 1b, actionable now):** each slot's
default-active kind renders at construction (it's what's visible
immediately); its inactive counterpart constructs but stays an empty shell
until the first time it's actually switched to via gear+Tabs.

**Application to grid mode (later, resolves an open question from last
turn):** the full bounded pool constructs structurally up front, but only
the panels on the *currently displayed page* ever render — page turns
become the render trigger, reusing decision 3's pagination mechanism
rather than inventing a new one. This replaces the "pre-build vs. lazily
construct the grid pool" tension flagged previously — it was a false binary;
both halves of it can be true at once for the two different costs.

**Honest limit of this fix:** addresses the *compute* cost specifically,
not the *structural* one. Comm registrations and browser-side DOM weight
for up to N×M hidden panels are still real even with zero data behind them
— exactly what decision 10's benchmarking (already sequenced earlier in the
roadmap for this reason) needs to measure before decision 2's cap is set
from real numbers rather than a guess. This buys real room but doesn't
make the scaling question go away outright.

### 12. Dict-driven headless export, decoupled from `VisibilityPlotter` (added July 30 2026)

**Decision:** headless export (decision 10) becomes its own small,
independent function — e.g. `render_panels_from_spec(spec: dict, backend)`
— that constructs fresh `VisibilityRaster`/`VisibilityScatter` objects
directly from a plain serializable dict, never touching `VisibilityPlotter`
or a live Bokeh/comm session at all. `VisibilityPlotter`'s only
responsibility toward this is producing that dict from its current
interactive state (`to_spec()` or similar) — a genuinely separate,
much smaller concern than actually generating output.

This is the concrete realization of two things already decided rather than
a new direction: decision 8's per-cell config/descriptor model (originally
scoped to grid mode, recognized here as applying to duo mode too, not
grid-specific after all) and decision 10's "keep 'what to plot' structurally
separate from 'how to deliver it'" — a dict has zero coupling to whether a
live session exists, which is about as separate as those two concerns can
get.

**New enabling piece this surfaces — "no Bokeh rendering involved" isn't
true yet.** `_build()` unconditionally constructs a real Bokeh `Figure`,
toolbar, tick formatters, and hover tools regardless of whether a
`comm_mgr` is passed. `defer_initial_render` (decision 11) controls *when*
the numeric query happens, not *whether* Bokeh scaffolding gets built at
all. A second, orthogonal constructor flag — `headless: bool = False` —
should short-circuit `_build()` before `figure(...)`, `_build_glyphs()`,
tick formatters, and hover/flag tool wiring, leaving only `_render()` and
its resulting arrays. Same separation-of-concerns move as decision 11, on
the other axis: that one deferred the query, this one skips the figure
construction. Not yet implemented — recorded so it isn't rediscovered from
scratch when this work actually starts.

**Sketch of the dict shape** (illustrative, not a finalized schema):

```python
{
    "layout": "side",  # "one" | "side" | "over" | (future) "grid"
    "panels": [
        {"slot": "A", "kind": "raster", "y_dim": "TIME", "x_dim": "BASELINE",
         "quantity": "AMPLITUDE", "colour_scaling": {...}},
        {"slot": "B", "kind": "scatter", "x_dim": "UVDIST", "layers": [...]},
    ],
    "selection": {"field": "...", "spw": "...", "correlation": "...", "datacolumn": "..."},
}
```

**Two things deferred, not decided now, flagged so they aren't missed:**

- Validation must be shared, not duplicated. The raster axis-conflict
  guard built early in this project checks interactive `_handle_plot`
  requests — a hand-written or scripted dict needs the identical check,
  not a second copy that could drift from it.
- The dict is an *additional* representation, not a replacement for the
  kwarg-based `VisibilityPlotter(...)` constructor. Kwargs remain right for
  interactive/notebook ergonomics; the dict is right for reproducibility
  and headless/pipeline use. Both should exist, consuming the same
  underlying config.

**Sequencing:** the *consuming* side (dict → fresh objects → PNG) is
genuinely independent of Stage 1b — it only needs `VisibilityRaster`/
`VisibilityScatter`, already stable and tested. The *producing* side
(`VisibilityPlotter.to_spec()`) has a soft dependency on Stage 1b: today it
could only ever describe fixed roles, and becomes meaningfully more useful
once per-slot kind exists. Not a hard blocker, just less rich until then —
Stage 1b remains the immediate next step regardless.

## New architecture pieces required

1. **Per-panel "gear" tool + tabbed sidebar config (2-panel layout)** — a
   `CustomAction` tool in each panel's toolbar that reveals a Bokeh `Tabs`/
   `TabPanel` widget ("Panel A" / "Panel B"), expanding the sidebar first if
   collapsed and activating that slot's tab. The tab widget is hidden by
   default and auto-hides again after a successful Plot ▶, auto-switching
   to (and staying open on) whichever tab has a validation error (decision
   9, full interaction flow settled). Each slot pre-builds one raster panel
   and one scatter panel per the existing `mode`-toggle visibility trick;
   each tab's Kind selector picks which is shown and configures it. This is
   the "in the small" validation of the per-cell config model grid mode
   will eventually need. **Storage backing this (added July 31 2026):**
   built against `self._slots` (the `_PanelSlot`-record refactor, "Stage
   1b.5" under decision 9) rather than fixed per-slot named attributes, so
   the `Tabs` widget's `TabPanel` list is generated by iterating
   `self._slots` — a later change to mode A's slot count doesn't require
   touching this construction logic. Considered and declined: a single
   shared config drawer retargeted per gear-click instead of one
   `TabPanel` per slot (decision 9) — loses each slot's held-open editing
   state, which defeats mode A's actual comparison use case.
2. **Per-cell config/descriptor (grid)** — the shared field/SPW/correlation
   selection plus one iteration-axis/value pair layered on top, plus an
   optional (unused in v1) `axes_override` field per decision 8. This is
   the load-bearing piece for grid mode; everything else in grid mode
   depends on it existing first. Distinct from item 1 above (2-panel case)
   but the same underlying model.
3. **Grid container** — rows × cols (bounded), "Iterate by" select,
   next/prev page controls, and the bounded pool of panel objects for the
   current page. **UI shape resolved (added July 30 2026):** Grid becomes a
   fourth option on the Layout `RadioButtonGroup` (One/Side/Over/Grid,
   additive to the control built in Stage 1a — no rework of that control
   needed). Selecting it reveals a sibling cluster of grid-only controls —
   two bounded pickers (Rows 1–N, Cols 1–M, bounds from decision 2) plus
   the "Iterate by" select and page nav from this same list — rather than
   enumerating every rows×cols combination as flat options, which doesn't
   scale to whatever bound the benchmarking work in decision 10 eventually
   settles on. Same "reveal only what's relevant" pattern used for gear→Tabs
   elsewhere in this doc. Needs its own purpose-built container (not
   `side_container`/`over_container` reused) — consistent with, and
   unaffected by, decision 8's declined "switch duo mode to `gridplot()`"
   call.
4. **Comm-id namespacing per cell** — each cell still needs its own
   registered handlers (hover probes, flag tool, plot/reload), so the
   existing per-panel id scheme (`ids['plot']`, `ids['probe']`, etc.) needs
   to extend to something like `ids[f"plot_{row}_{col}"]`. Needs to be
   verified against the existing `CommMgr`/`_comm_mgr.py` registration
   pattern before the rest of the grid is built — this is the one piece
   most likely to surface an unexpected constraint from the transport
   layer.
5. **Page-change wiring** — "Iterate by" + next/prev trigger a page-change
   event that re-runs selection (not construction) on each cell's panel(s).
6. **Sync broadcast** — a client-side listener per cell, gated by the sync
   toggle, that (a) propagates `x_range`/`y_range` changes to sibling cells
   of the same panel type with matching axes, with a reentrancy guard
   (decision 6), and (b) wires shared `Span`/`CrosshairTool` overlays across
   those same sibling cells (decision 7). Builds on the existing per-panel
   `rerender_js` viewport hook plus Bokeh's native linked-crosshair support;
   no new server-side mechanism required for either half.

## Proposed ordered implementation plan

1. `compact_toolbar` (`toolbar.autohide`) on the shared base — independent,
   low-risk. **Implemented and tested (July 29 2026).**
2. Per-panel gear config tool for the existing 2-panel layout (item 1
   above) — validates decision 8's model in the small, doesn't depend on
   any grid infrastructure, and addresses the demo feedback directly.
   **Progress: Stage 1a (unified Layout control) implemented and tested.
   Stage 1b (per-slot data model — four objects, `defer_initial_render`
   per slot's inactive kind, `self._raster`/`self._scatter` as compatibility
   properties, `_activate_slot_kind()`) implemented July 30 2026 — see
   decision 9's Stage 1b note below. Stage 1b.5 (`self._slots` list of
   `_PanelSlot` records, replacing the six named per-slot attributes)
   implemented and tested July 31 2026 — `test_visibility_raster.py`/
   `test_visibility_scatter.py` pass unchanged and no UI-visible behavior
   changed, as expected for a pure storage refactor behind the
   `self._raster`/`self._scatter` compatibility properties. Stage 1c
   increment 1 (gear/Tabs skeleton — reveal-on-click tabs, red
   full-replacement title label, per-slot Cancel, success-path reset; see
   decision 9's "Stage 1c increment 1" note below for the full record,
   including a Bokeh same-tick layout-invalidation bug found and fixed
   along the way) implemented and tested July 31 2026. Groups 1+2 of the
   ~60-reference rework (positional display-order infrastructure —
   `self._pos0`/`self._pos1`/`self._slot_display_order`/`self._all_panels`
   — plus all-four-panel construction wiring; see decision 9's "Groups 1+2
   rework" note below for the full record, including the zero-recompute
   swap groundwork) implemented and tested July 31 2026 — confirmed
   against the live app (no observable behavior change, as expected), on
   top of the syntax/JS/simulation validation already done pre-test.
   **Group 3 piece 1** (per-slot config panels + Raster/Scatter switch —
   see decision 9's "Group 3 piece 1" note below for the full record,
   including a second Shadow DOM bug found and fixed along the way)
   implemented and tested July 31 2026, confirmed against the live app.
   **Group 3 piece 3, Chunk 1** (per-slot request/response reshaping,
   `_handle_plot()`/`doPlot()` rewritten, presets and `layout_js` fixed
   in the same pass — see decision 9's "Group 3 piece 3, Chunk 1" note
   below for the full record, including the corrected piece ordering:
   piece 3 has to precede piece 2, not follow it, since `doPlot()` reads
   directly off the widgets piece 2 would remove) implemented July 31
   2026 — confirmed against the live app (old global controls correctly
   inert, gear-driven per-slot panels correctly drive Plot ▶), on top of
   the syntax/JS/simulation validation already done pre-test.
   **Piece 2** (retired the global raster/scatter sections and their
   now-dead `layout_js`/preset references, rebuilt the dark/light
   toggle's widget list from all four per-slot widget sets — see
   decision 9's "Group 3 piece 2" note below for the full record)
   implemented July 31 2026 — **confirmed against the live app.**
   **Chunk 2** (the layout-swap mechanism — all four layout objects
   present in the container, visibility toggled by `.kind`; `_handle_plot()`
   relaxed to actually call `_activate_slot_kind()`) and **cursor-span
   tracking generalized to N panels** both implemented July 31 2026 and
   **now confirmed against the live app** (August 2 2026), after four
   bugs found during that live testing were fixed — see decision 9's
   "Four bugs found and fixed during live testing" note below for the
   full record. Group 3's core rework — same-kind-on-both-slots,
   genuinely working end to end — is complete and live-confirmed.
   Fixing bug #1 (missing gear tool) incidentally closed the
   "gear-tool title targeting" gap originally flagged under Chunk 2, so
   only **one** of the original three flagged gaps remained open after
   that: **`layout_js`/preset figure-sizing**. Fixed August 2 2026 — see
   decision 9's "`layout_js`/preset figure-sizing gap closed" note below
   for the full record, including a related, more subtle preset
   transient-visibility bug found (not originally flagged) while fixing
   the sizing gap. With this, all three originally-flagged Chunk 2 gaps
   are addressed and confirmed live (scatter recompute-gating and the
   panel-1 corruption bug it exposed were also found, fixed, and
   confirmed live in this same window — see those notes above).
   **Swap feature** (zero-recompute panel-position swap, one button per
   tab, `self._display_order_source` as the underlying reorderable-list
   tracker — see decision 9's "Swap feature" note below for the full
   multi-turn design history, including the "One" mode sizing gap closed
   as part of the same work) designed and implemented August 3 2026,
   **confirmed live**, plus two small tab-layout/tooltip and two
   light-mode polish items also confirmed live in the same window (see
   notes above). **Validation-error auto-switch-to-tab** implemented
   August 3 2026 — simulation-validated, **not yet live-tested**. With
   this, every piece of decision 9's originally-settled gear/Tabs design
   is built. Remaining, unrelated to Group 3's UI: only
   `colormap_controls()` (min/max never wired; Color scaling/alpha/gamma
   wired to a mechanism that can't fire in this app's actual
   architecture), explicitly deferred to the reference-testing phase
   rather than patched now.**
3. **(moved up per stakeholder feedback, decision 10)** Functional
   generator/iteration API over duo mode, plus one working PNG export
   path — enough to benchmark, not full Phase-4 polish. Requires step 2
   (duo mode stable) but nothing from grid mode. Deliverable: real
   query→render→export timing numbers vs. plotMS, and a data-backed choice
   among decision 10's three rendering-path options, both available
   *before* grid-mode work begins. **Rendering path chosen: matplotlib
   (July 29 2026) — see decision 10's prototype findings.**
4. Per-cell config/descriptor for grid mode, including the unused
   `axes_override` field (item 2 above).
5. Grid container: bounded rows×cols, "Iterate by" select, page nav, bounded
   object pool (item 3 above). Default/cap dimensions (open question below)
   should be set using the step-3 benchmark numbers rather than the
   currently-unconfirmed 3×3/6×6 guess.
6. Comm-id namespacing per grid cell (item 4 above) — confirm against
   existing transport before proceeding further.
7. Wire page-change to re-select (not rebuild) each cell.
8. Sync toggle: pan/zoom broadcast + crosshair position link (item 6 above)
   — natural follow-on once the grid and per-cell comm registrations exist;
   not required for a minimal working grid, but cheap given the existing
   viewport hook and Bokeh's native crosshair-sharing support.

## Explicitly deferred

- Per-cell heterogeneous raster/scatter and/or per-cell distinct axes *in
  the UI* — the data model is kept open to it (decision 8), but no UI for
  it ships in phase 1.
- Cross-cell hover **probe/tooltip value** sync (decision 7) — distinct
  from the crosshair *position* sync, which is in scope.
- True (virtualized) scrolling as an alternative to pagination.

## Open questions not yet resolved

- **Confirmed (July 30 2026): switching between duo mode (One/Side/Over)
  and Grid mid-session is supported by construction**, not a separate
  capability to build — Grid is a sibling value on the same `layout_rbg`
  control built in Stage 1a, not a construction-time-only choice. Switching
  back to duo from Grid preserves whatever duo-mode state existed before,
  since panels are hidden, not torn down, when Layout changes.
- **Resolved by decision 11:** the grid panel pool is pre-built structurally
  upfront (no post-initial-load construction, consistent with everywhere
  else in this doc) — what's actually deferred is *rendering* each panel,
  not constructing it, gated on whether that panel is on the currently
  displayed page. The binary framing from a previous turn was a false one.
- Exact default/max grid dimensions (proposed 3×3 default / 6×6 cap above —
  not yet confirmed).
- Whether "Iterate by" should support compound iteration (e.g. antenna *and*
  SPW simultaneously, as PlotMS supports) or a single axis only in phase 1.
- Whether flagging actions taken in one grid cell should have any
  cross-cell effect (e.g. flag propagates to the same baseline/time range
  visible in a sibling cell) or remain strictly per-cell.
- Whether pan/zoom sync needs per-axis (X-only/Y-only) granularity, or
  whether syncing both together (current proposal) is sufficient.
- Whether the burst-of-concurrent-backend-queries caveat under decision 6
  needs mitigation (e.g. staggering) before it ships, or only if it proves
  to matter in practice against a real (non-local) backend.
- Headless export (decision 10): rendering path is now **resolved to
  matplotlib** (see decision 10) — remaining open pieces are full fidelity
  validation against real (not synthetic) data, generator output
  granularity (per-panel vs. per-composed-page — see decision 12's sketch),
  and exact generator/method shape.
