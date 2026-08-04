# `visplot` implementation plan

Status: living planning document, feeding the master `cubevis` roadmap.
Renamed from `visplot-grid-iteration-notes.md` (August 4 2026) — that name
stopped fitting once duo-mode/gear-tabs work, which this document also
now fully covers, grew into the majority of its content.

This document is intentionally a **plan**, not a chat log. The detailed
turn-by-turn engineering history (every bug found, every simulation run
to validate a fix) lives in the conversation record and in inline code
comments throughout `visibility_plotter.py`/`visibility_raster.py`/
`visibility_scatter.py` — search those files for dated comments
(`added 2026-0X-XX`, `Fixed 2026-0X-XX`) to find the reasoning behind any
specific piece of code. This document keeps the *decisions* and their
rationale, at a level useful for planning what comes next, and trims the
narrative of how each was reached.

For verifying the current implementation, see the companion
**`visplot-testing-handoff.md`**. For the two next major bodies of work,
see **`visplot-development-handoff.md`** (N-panel duo mode, iteration/grid
mode) and **`visplot-headless-export-handoff.md`** (matplotlib-based
PNG/HTML export).

---

## Current status (as of August 4 2026)

**Duo mode (two-panel layout) is functionally complete.** Every panel can
independently be raster or scatter, independently configured, with
zero-recompute position swapping, full dark/light theming, and validation
errors that focus the correct tab automatically. This was the "Preview
release" scope described below, and it's done.

**What's confirmed working**, verified live against `sis14_twhya_calibrated_flagged.ms` across many rounds of testing:

- Per-slot gear/Tabs configuration (open a panel's config, independent of
  the other panel's)
- Raster ↔ Scatter kind switching per panel, including two rasters or two
  scatters simultaneously
- Cursor-span crosshair tracking generalized to N panels, matching on any
  shared axis dimension (not just a fixed raster/scatter pairing)
- Zero-recompute panel-position swap (One/Side-by-Side/Over-Under, all
  three Layout modes)
- Validation-error auto-focus to the offending tab, including the
  client-side raster-Y/X-conflict guard (which never reaches the server)
- Dark/light theming, including the two gaps found in the last testing
  round (config-field hints, source-file-path color)
- Tooltips throughout the toolbar and per-tab controls

**Known, deliberately-not-fixed gap:** `colormap_controls()` (the Color
scaling dropdown, alpha/gamma, and the min/max fields) is not actually
wired up correctly for this app's real architecture — see
"`colormap_controls()` is non-functional" below. Explicitly deferred to
the reference-testing phase rather than patched piecemeal.

**Not yet started:** everything in `visplot-development-handoff.md`
(N-panel duo mode, iteration/grid mode) and
`visplot-headless-export-handoff.md` (matplotlib export) — both fully
designed below, neither implemented.

---

## Terminology

**"Duo mode"** — today's two-panel layout (One / Side-by-Side / Over-Under),
each panel individually configurable as raster or scatter. The subject of
all the "Preview release" work described below.

**"Iteration mode"** (previously called "grid mode" in earlier notes —
the rename is now complete throughout this document) — a PlotMS-style
paginated grid of panels sharing the same axes, iterating through a
selection value (antenna, SPW, field, scan) per cell. A distinct feature
from duo mode's N-panel expansion — see decision 8 below for why they
share an architecture without being the same UI.

---

## Preview release scope (settled July 29 2026, delivered August 3 2026)

**Shipped:**

- `compact_toolbar`/autohide, defaulting on for duo mode (decision 1)
- The gear tool + tabbed sidebar config for duo mode, full interaction
  flow per decision 9 — **complete**, see current-status summary above
- Headless PNG export via matplotlib was *scoped* for preview (decision
  10) but is **not yet built** — see `visplot-headless-export-handoff.md`
- The generator/iteration API over duo mode was *scoped* for preview but
  is **not yet built** — same handoff document

**Correctly still deferred:** everything iteration/grid-mode (decisions
2–8) — no UI built, real open questions remain (grid size caps, comm
namespacing against the transport layer). See
`visplot-development-handoff.md`.

---

## Decisions: duo mode / gear-tabs architecture

These decisions are **implemented and confirmed working** (see Current
Status above). Kept here because they explain *why* the code is shaped
the way it is — genuinely useful when extending it to N panels.

### Toolbar auto-hide on hover

Implemented and tested working (July 29 2026). The toolbar's reserved
static space when hidden is expected upstream Bokeh behavior, not a bug.

### Per-slot data model (`self._slots`, `_PanelSlot`)

Each duo-mode slot ("Panel A", "Panel B") is a `_PanelSlot` record holding
both a `VisibilityRaster` and a `VisibilityScatter` instance, plus which
one is currently `.kind`-active. Both objects **construct** at startup
(Bokeh `Figure`, `ColumnDataSource`s, comm registrations, hover/flag
tools) — only the *inactive* one's first `_render()` is deferred until
it's actually switched to (see "Defer render, not construction" below).
This is the foundational pattern everything else in duo mode builds on,
and the one N-panel expansion needs to generalize from 2 slots to N.

`self._slots: list[_PanelSlot]` (not fixed named attributes like
`self._slot_a`/`self._slot_b`) — this was a deliberate refactor
specifically so a later change to slot *count* wouldn't require touching
construction logic throughout the file. N-panel expansion should mostly
be "change how many entries are in this list," not a rewrite.

`self._raster`/`self._scatter` exist as compatibility properties
resolving through `self._slots[0]`/`self._slots[1]` for code that predates
the per-slot model — worth knowing about if extending to N panels, since
these properties become genuinely ambiguous once more than one slot can
hold the same kind (documented in their own docstrings).

### Gear tool + tabbed sidebar config

A `CustomAction` tool in each panel's toolbar reveals a Bokeh `Tabs`
widget, expanding the sidebar first if collapsed. **Critical detail for
N-panel expansion:** each *kind* needs its own gear instance attached to
its own figure — not one gear per slot. A slot's gear tool was originally
built as one-per-slot, bound to whichever kind was active at construction
time; this caused the kind that becomes active later (via a kind switch)
to have no gear at all, since nothing was ever attached to its figure.
Fixed by building one gear per (slot, kind) pair. This detail generalizes
directly to N slots, each still needing two gears (raster-kind, scatter-
kind) — N slots means 2N gear instances, not N.

Each slot pre-builds one raster panel and one scatter panel; the tab's
Kind selector picks which is shown. Considered and declined: a single
shared config drawer retargeted per gear-click — loses each slot's held-
open editing state, which defeats the actual comparison use case (editing
two panels' configs independently before committing either).

### Per-slot kind switching, same-kind-on-both-slots

Any slot can independently be raster or scatter, including two rasters or
two scatters simultaneously — this was the actual point of the whole
rework, not just a byproduct. The generalized architecture:

- **Container structure:** all four layout objects (both kinds, both
  slots) are always children of the display container, with `.visible`
  toggled to show only the currently-active kind per slot. This works
  because Bokeh's row/column layout already excludes `.visible=false`
  children from the flex layout — proven by "One" mode's existing
  behavior before this was even built, not new machinery.
- **Response handling:** `doPlot()`'s JS resolves which figure/data to
  update *dynamically*, from what the server response says actually
  rendered (`resp.panels[id].kind`), never from a binding fixed at
  construction time. This pattern — resolve dynamically, don't bind once
  — recurs throughout the fixes below and is the single most important
  lesson for extending this to N panels or to the swap feature.
- **Change-detection gating:** both raster and scatter now compare
  requested axes/selection against their own last-rendered state,
  per-slot, skipping recompute when nothing changed. (Scatter didn't have
  this until August 2 2026 — see "Scatter recompute-gating" below.)

### Zero-recompute panel-position swap

A "Swap" button in each tab exchanges which screen position shows which
panel's content — genuinely zero-recompute (no comm round-trip, no
re-render), purely a client-side container-children reassignment.
Underlying mechanism: `self._display_order_source`, a `ColumnDataSource`
holding an ordered list of slot indices — deliberately a reorderable
*list*, not a boolean, so the same mechanism extends directly to an
eventual N-panel version.

**Design history worth preserving**, since it went through several real
revisions before landing on the simple version actually built: a
coordinate-labeled ("`[row,col]`", numpy-style) dropdown scheme was fully
designed — including the discovery that valid coordinates depend on which
Layout mode is active (Side-by-side grows across columns, Over/Under
grows down rows) — before being set aside once it became clear duo mode
only ever has exactly one possible swap target, collapsing the whole
design to a single "Swap" button. **The coordinate scheme is not wasted
design work** — it's the leading candidate for the eventual N-panel
trigger, along with a newer alternative (a small grid of buttons mirroring
the actual on-screen arrangement, proposed during testing) that may be
even more intuitive. See `visplot-development-handoff.md` for the full
design record on both options.

### Validation-error auto-switch-to-tab

A failed Plot ▶ focuses whichever tab's configuration actually caused the
failure, rather than leaving the user to guess. `_handle_plot()` tags
every error response with `"failed_slot": slot.id`; the client switches
`gear_tabs.active` accordingly, opening the tab first if it wasn't already
open. **Important nuance:** the raster Y/X axis conflict — the most
common error a user will actually hit — is caught entirely client-side,
before any request reaches `_handle_plot()` at all (a deliberate "refuse
to send rather than let the server round-trip reject it" design). The
tab-switching logic had to be duplicated at that earlier client-side guard
too (factored into a shared `switchToTab()` helper), not just at the
server-response level, or it would only ever fire for the *less common*
error types (kind-mismatch, exceptions during render).

### Defer render, not construction, for inactive panels

Every panel object *constructs* at startup regardless of whether it's
currently active (structural cost, unavoidable without a riskier
construction-lifecycle-after-load model). Only the *first render* —
`_render()`, which triggers the actual backend query and Datashader
compositing, where the real cost lives — is deferred until a panel
actually becomes active. No new mechanism needed: `update_axes()`/
`_render()` already means "recompute because the displayed content
changed," the same trigger a kind switch needs, just fired from a
different event.

**Honest limit:** this addresses compute cost specifically, not the
structural cost (comm registrations, DOM weight) of every constructed-but-
inactive object. Real for up to N slots even with zero data behind them —
exactly what the headless-export benchmarking work (see the separate
handoff doc) should measure before N-panel or iteration-mode sizing
decisions get made from real numbers rather than a guess.

### `colormap_controls()` is non-functional

Found during testing, August 3 2026, **not fixed** — deliberately
deferred to the reference-testing phase rather than patched piecemeal.
Two distinct problems in `visibility_raster.py`/`visibility_scatter.py`'s
`colormap_controls()` (both files, same pattern):

- `min`/`max` `TextInput`s are constructed and placed in the layout but
  never given any callback at all — pure visual mockup.
- The "Color scaling" dropdown and alpha/gamma inputs use Bokeh's native
  `.on_change()` — a server-side property-change callback that can only
  ever fire with a live Bokeh server running the Python event loop. This
  architecture deliberately has none (the entire `CommMgr`/comm design
  exists because there is no Bokeh server). A fully correct, properly
  registered comm handler already exists for this
  (`_handle_update_scaling_raster`/`..._scatter`,
  `self._comm.register(...)`) but nothing on the client side ever sends
  the message that would invoke it — most likely built and tested at some
  point against a `bokeh serve` environment, where `.on_change()` would
  have appeared to work, before the app's real deployment model
  solidified around comm-only.

**Real fix needed:** replace `.on_change()` with `js_on_change` + comm
send for scaling/alpha/gamma; build `min`/`max` from nothing. Not
attempted — exactly the kind of thing the reference-testing phase (next
section) exists to catch systematically.

---

## Reference testing phase (not yet started)

Explicitly deferred by design, not an oversight: verifying that the
*values* the UI produces are actually correct — not just that the UI
behaves sensibly — needs known-good comparison points. The plan is to
compare against results from PlotMS and msview tutorials available
online, particularly for:

- Whether selection controls (scan range, antenna, time range, UV range)
  actually constrain the plotted data, and correctly — not yet confirmed
  either way.
- `colormap_controls()`, once fixed (see above) — whether the resulting
  scaling actually matches expectations.
- General cross-validation of axis values, flagging behavior, and
  aggregation against a trusted reference implementation.

See `visplot-testing-handoff.md` for the structural (does-the-UI-work)
testing that's already been done; this phase is the separate, harder
question of correctness against ground truth.

---

## Decisions: iteration/grid mode (design only — not built)

These are fully preserved from the original design discussion. Full
detail, including the "New architecture pieces required" and grid-specific
open questions, is in `visplot-development-handoff.md` — kept here only
as a summary index.

1. **Real interactive panels per cell, not a shared-renderer/static-image
   grid** — each cell is a genuine `VisibilityRaster`/`VisibilityScatter`
   instance (own flag tools, hover probes), not a static composited image.
2. **Bounded grid size** (proposed default 3×3, cap ~6×6 — not yet
   confirmed, should be set from real benchmark numbers) — no Bokeh
   server means no mechanism to coordinate an open-ended object pool.
3. **Paginate, don't scroll** — page turns re-select existing panels via
   the existing `update_axes()` path, not a rebuild.
4. **Uniform axes/mode per grid by default** — a UI-scope choice, not an
   architectural limit; the underlying per-cell object model supports
   heterogeneous configuration even though phase-1 UI doesn't expose it.
5. **Object count sized to the page, not the full iteration count** —
   page turns re-select, never destroy/recreate.
6. **Cross-cell pan/zoom sync**, gated by a toggle, same-panel-type only.
7. **Cross-cell crosshair position sync** — in scope, reuses Bokeh's
   native linked-crosshair support (cells share axes by construction, so
   no custom axis-translation is needed, unlike the raster↔scatter
   crosshair link in duo mode). Hover *probe/tooltip value* sync stays
   deferred (real per-mouse-move traffic concern).
8. **One grid architecture, not a fork for future heterogeneity** — the
   per-cell config/descriptor model is general from the start (an unused
   `axes_override` field reserved even though phase-1 doesn't set it).

Full detail on all eight, plus the "New architecture pieces required" list
and grid-specific open questions, is in `visplot-development-handoff.md`.

---

## Decisions: headless/pipeline export (design only — not built)

Fully preserved and expanded in `visplot-headless-export-handoff.md` —
kept here only as a summary index.

- Output must be fully labeled (axes, ticks, title, colorbar), matching
  the interactive appearance.
- **Rendering path chosen: matplotlib** (July 29 2026, based on prototype
  evidence — webdriver `export_png` failed outright in a pipeline-like
  test environment; matplotlib worked immediately with a dependency this
  domain already assumes).
- Feed matplotlib the **already-composited RGBA array** (the same object
  the interactive Bokeh glyph consumes) via `imshow`, guaranteeing
  byte-identical pixels between interactive and headless output.
- One genuinely hard piece: an accurate **colorbar** needs the eq_hist
  scalar-to-color mapping function itself, not just pre-colored pixels —
  not a standard matplotlib norm.
- **Iteration: a generator over one open backend**, not one
  `VisibilityPlotter` per iteration value — reuses the same
  lightweight-reselection mechanism grid-mode pagination needs, just with
  a different delivery target.
- **Dict-driven, decoupled from `VisibilityPlotter`** — headless export
  constructs fresh panel objects directly from a plain serializable dict,
  never touching a live comm session. A new `headless: bool` constructor
  flag should skip Bokeh `Figure`/toolbar/tick-formatter construction
  entirely for this path (not yet built).
- Full detail — sequencing rationale, prototype findings, deferred
  questions — in `visplot-headless-export-handoff.md`.

---

## Explicitly deferred (all phases)

- Per-cell heterogeneous raster/scatter and/or per-cell distinct axes *in
  the UI* for iteration mode — the data model supports it, no UI ships
  for it in phase 1.
- Cross-cell hover probe/tooltip value sync (distinct from crosshair
  position sync, which is in scope).
- True (virtualized) scrolling as an alternative to grid pagination.

## Open questions not yet resolved

- Exact default/max grid dimensions for iteration mode (proposed 3×3/6×6
  — not confirmed; should come from headless-export benchmark numbers).
- Whether "Iterate by" should support compound iteration (antenna *and*
  SPW simultaneously, as PlotMS supports) or a single axis only in phase 1.
- Whether flagging in one grid cell should have cross-cell effect or stay
  strictly per-cell.
- Whether pan/zoom sync needs per-axis granularity or syncing both
  together is sufficient.
- Whether the "burst of concurrent backend queries on sync" caveat
  (decision 6) needs mitigation before shipping, or only if it proves to
  matter against a real non-local backend.
- Headless export: full fidelity validation against real (not synthetic)
  eq_hist-composited data; generator output granularity (per-panel vs.
  per-composed-page); exact generator/method shape.
- The N-panel swap-trigger UI (coordinate dropdown vs. spatial button
  grid) — both designed, neither built, no final choice made. See
  `visplot-development-handoff.md`.
- Whether an eventual N-panel mode A extension needs a `headless`-style
  "no gear tools" flag for cases where panels shouldn't be individually
  adjustable — raised during this handoff, not yet resolved. See
  `visplot-development-handoff.md`.
