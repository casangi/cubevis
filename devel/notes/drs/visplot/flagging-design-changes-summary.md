# Design & Deliverable Changes — Flagging Tool Session (July 2026)

Scope note: this summarizes **design and deliverable** changes only —
what changed about the product's behavior, UI, or documented architecture.
Bug fixes made along the way (coordinate-space errors, toolbar exclusivity,
stale state-sync, etc.) are omitted; they don't change what was designed,
only whether it worked correctly.

---

## 1. Changes to `visibility_plotter_preview.md`

### 1.1 The flagging tool itself was redesigned — not a Box Select tool anymore

**Current doc says:** flagging happens via the standard Bokeh **Box Select**
tool (skeleton diagram: `[□ Box Select] [⚑ Flag†] [⟲ Undo†]`; §7 toolbar
table: *"Box Select — Activates Bokeh box-select tool; on box close
immediately adds `FlagDelta`..."*).

**What was actually built:** a custom `FlagTool` / `FlagTool(flag=False)`
("Unflag") tool pair, replacing Box Select and the disabled Flag/Undo
buttons entirely. Behavior:

- **Combines click and drag on one tool.** A click (no drag) zooms the
  local panel to 1:1 pixel resolution. A drag draws a rubber-band box
  (same dotted-border, translucent-fill visual language as Bokeh's own
  Box Zoom).
- **"Not unselectable."** Re-clicking the tool while it's already the
  active drag tool does not deactivate it — it re-triggers the 1:1 zoom
  instead. This was necessary so the tool can serve as both the
  persistent flagging mode and the standard way to reach flagging
  resolution, without a separate "off" state that would silently break
  flagging.
- **Two icons, one mechanism.** Solid flag = flag; outline flag = unflag.
  Both are the same `FlagTool` class parameterized by a `flag: bool`.
- **Undo is gone, replaced conceptually by Unflag.** Undo implied a
  temporal stack (reverse the last action); Unflag is a spatial region
  operation (draw a box, remove flags in that region) — judged more
  natural for radio astronomy workflows and not limited to reversing
  in order. *(This also affects the implementation plan — see §2.1 below.)*

**Doc sections needing an update:** the toolbar skeleton diagram, §7
toolbar table, and the "Flag ⚑ / Undo ⟲ disabled with explanation" row in
the "Spec items — delivered as specified" appendix table (this item is
now obsolete — those buttons no longer exist at all, disabled or
otherwise).

### 1.2 New constraint: flagging is gated on 1:1 pixel resolution

Not present in the current doc at all. Flagging/unflagging only records a
`FlagDelta` when the view is zoomed to at least one screen pixel per
underlying data cell. Below that resolution, the box still draws (visual
feedback, so the interaction doesn't feel broken) but is rejected with an
explanatory message rather than silently doing nothing. Rationale:
flagging against Datashader-averaged/aggregated bins would flag the wrong
thing — aggregated bins, not individual visibilities.

This applies differently to raster vs. scatter (see §1.5 below) — worth
capturing as a named concept in the spec since it's now central to how
flagging behaves in both panels.

### 1.3 New public constructor parameter: `enable_flagging`

Not present in the "Astronomer-facing API" example (preview doc §
"Construction approach"). Add:

```python
plotter = VisibilityPlotter(
    ...,
    enable_flagging = True,   # False → no flag/unflag tools at all
)
```

Rationale: an astronomer who only wants to inspect data (no flagging
workflow) can build a plotter with neither `FlagTool` instance present on
either panel's toolbar — nothing to disable, nothing in the way. Threads
straight through to `VisibilityRaster`/`VisibilityScatter` as well.

### 1.4 Status bar is now two widgets sharing one row, not a separate notify area

**Current doc** (§9 "Status bar" + the "Notification div" item under
"Items added beyond spec") describes the status `Div` and a separate red
notification `Div` as two independent elements, with the notification
described as sitting *above* the status bar and used only for "Python-side
warnings and errors."

**What changed:** they now share a single status-bar row — the dataset/
config summary (green) occupies the left half, flagging feedback and
other notifications (red) occupy the right half, via a shared `row()`
layout. Sidebar-field hints (which need the full row width) hide both
halves together rather than just the left one.

The red half's purpose has also expanded beyond generic errors: it now
reports the outcome of every flag/unflag attempt — a rejection message
when below 1:1 resolution, and a confirmation (including a running flag
count, see §1.6) on success.

**Doc sections needing an update:** §9 "Status bar", and the
"Notification div" bullet under "Items added beyond the original spec."

### 1.5 Scatter's "1:1" criterion is a deliberate approximation, not literal

Worth documenting as a known design compromise: scatter points are
plotted at exact positions (not binned/decimated the way raster is), so
"1:1 pixel resolution" doesn't map onto scatter the same way. The
implemented proxy reuses the existing sparse-data canvas-shrink logic
scatter already had for visual density boosting, evaluated at the full
data extent — in effect, "zoomed in enough that the full-extent view's
overplot-driven canvas shrink no longer applies," which is a reasonable
but inexact proxy for "not looking at an ambiguously overplotted
cluster." Confirmed via testing to still be somewhat imprecise even after
zooming — individual points do become visually separable, but not with
raster's cleanliness. Judged an acceptable compromise for this release
rather than something to invest further precision into right now.

### 1.6 Flag count now surfaces to the user (status bar + success message)

Not present in the current doc. After a successful flag/unflag, both the
green status half (as "Flag count: N") and the green confirmation message
itself report the current pending count from `FlagDB`. ("Pending" was
deliberately avoided as terminology — see note in §2.2 below on why.)

### 1.7 `Reload ↺` now behaves differently from `Plot ▶`

**Current doc** (§7 toolbar table) says *"Reload ↺ — Same as Plot in the
preview,"* i.e. no distinction. That's no longer accurate: `Reload ↺` now
also clears the pending `FlagDB` (in-memory only — nothing has ever been
written anywhere, so this is a safe reset), while `Plot ▶` does not. This
matches the reasonable expectation that starting over should discard
in-progress, uncommitted flag state.

### 1.8 No-server transport note: flagging traffic uses its own dedicated `Comm`

Minor, but relevant to the "No-server constraint" section, which
describes the CommMgr/Comm j2p/p2j transport generically. Flag/unflag
traffic was moved onto a separate `Comm` channel (opened via
`CommMgr.open()`, `squash_queue=False`) rather than sharing each panel's
existing general-purpose comm (`squash_queue=True`, used for hover/probe
traffic). Reasoning: a queue that silently squashes/replaces pending
messages is correct for hover tracking (only the latest cursor position
matters) but would be a real correctness risk for flagging (a rapid
second flag-box could squash an earlier one out of the queue before
Python ever saw it). Worth a line in the transport section if it
discusses per-panel comm channels at that level of detail.

---

## 2. Changes to `visibility_plotter_implementation_plan.md`

### 2.1 §4.5 "Flagging tools" — Undo is dropped; Unflag is the replacement

**Current doc lists**, among others:
> **Undo** — pop last `FlagDelta` from `FlagDB` and re-render; works
> freely until disk write

This item should be **removed or reframed**. The design decision made
this session was to drop the temporal-undo-stack concept entirely in
favor of **Unflag as a first-class tool** (spatial, region-based — draw a
box over previously-flagged data to remove flags in that region), using
the exact same mechanism as Flag (same `FlagTool` class, `flag=False`).
This is a more fundamental change than a UI relabel: it changes the
underlying interaction model from "reverse my last action" to "mark this
region as not-flagged," which doesn't require flags to be undone in
strict reverse order.

**Also worth reconciling:** §2.3's interactive flag loop (steps 4–7)
doesn't mention an unflag step at all, and would benefit from one now
that it's a first-class tool rather than an undo mechanism.

### 2.2 §4.5 — "Box select" tool description should reference `FlagTool`, not plain Bokeh Box Select

**Current doc:**
> **Box select** (default) — draw rectangle in data space; on box close
> immediately adds `FlagDelta` to `FlagDB` and re-renders flagged overlay
> in red; no button press required

This describes the *old* mechanism (plain Bokeh `BoxSelectTool`, always
active by default, competing with Box Zoom for the same drag gesture —
which was in fact one of the problems that motivated the redesign; Box
Zoom and Box Select can't both be the active drag tool at once in
standard Bokeh). The delivered mechanism is the `FlagTool`/`Unflag` pair
described in §1.1 above — worth updating this bullet to match, including
the "no button press required" framing, which is no longer quite right:
the *tool* still has to be selected/armed first (it's not always-active
the way this bullet implies), though once armed, drawing a box does not
require an additional button press.

**Terminology note:** the codebase and status-bar messaging now say
"Flag count" rather than "pending flags." This was a deliberate choice
because the underlying `FlagDB` is expected to *rationalize* entries —
unflagging something removes it from the DB rather than adding an
inverse entry, so "pending" (implying a queue awaiting some future
action) is a less accurate word than "count" (a plain measure of current
DB state). Worth keeping this terminology consistent if the master docs
use "pending" anywhere in flagging-related text.

### 2.3 §F-7/F-8 — the box-select j2p handler task description is stale

> F-7: Box-select j2p handler in `VisibilityRaster`: JS box-select tool
> callback sends data-space `(x0,x1,y0,y1)` → Python adds `FlagDelta`...
> F-8: Box-select j2p handler in `VisibilityScatter`

These describe **per-widget-class** handlers built on plain Bokeh Box
Select. What was actually delivered is a **single shared mechanism** in
the common `VisibilityPlot` base class (`_add_flag_tools()`), used
identically by both `VisibilityRaster` and `VisibilityScatter` — not two
separate implementations. The custom `FlagTool` (in
`cubevis.bokeh.tools._flag_tool`) is the thing that sends the j2p
message, not a callback wired directly to Bokeh's own box-select
geometry event. Worth deciding whether to rewrite F-7/F-8 as a single
shared task, or leave them as historical record and add a note pointing
to the actual delivered architecture.

### 2.4 Open design question surfaced, not yet answered: how does the red flag overlay actually work?

Raised during this session, genuinely unresolved: F-9/F-10 currently
state the red overlay flatly —

> F-9: "Show flagged" overlay in `VisibilityRaster`: red RGBA layer
> composited on top of existing image...
> F-10: "Show flagged" overlay in `VisibilityScatter`: semi-transparent
> red layer from flagged data points

— as if the mechanism is straightforward, but it isn't obviously so given
the Datashader-based rendering pipeline for both panels: raster pixels
are aggregated bins, not 1:1 with underlying MS rows, and scatter's
compositing already does its own Porter-Duff layering. A concrete open
question raised but not resolved: **should this be implemented as a
second, toggleable image layer representing the `FlagDB` state**, shown/
hidden independently of the main data image, rather than literally
recoloring pixels of the existing raster/scatter image? This deserves
explicit design attention before F-9/F-10 are scheduled — right now
they're written as if the "how" is settled, and it isn't.

---

## 3. Not changed, but worth reconfirming as still-accurate

- **radplot's raster x-axis is BASELINE × TIME, not BASELINE × UVDIST** —
  already correctly documented as a known limitation (§"Known
  limitations," item 3) and confirmed still accurate/expected during this
  session's testing. No doc change needed, just noting it came up and
  held up under scrutiny.
- Everything under "What the preview explicitly omits" remains
  accurate — none of it was touched this session.
