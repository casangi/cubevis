# visplot — Preview Specification

**Repository:** https://github.com/casangi/cubevis/blob/main/devel/docs/visplot/visibility_plotter_preview.md

> **Release staging note (August 2026).** This document specs the single, eventual
> **preview** release for general external users — the filename is accurate as-is. What
> has actually gone out so far is **pre-preview**: an internal, team-members-only
> staging period (in progress since August 2026) where team members exercise the
> current build and feed back before this document's full scope is met. Sections below
> marked as working reflect what pre-preview reviewers can currently exercise; sections
> still open (e.g. Duo-mode iteration — see the implementation plan's Phase 2.5) are
> requirements this document must still satisfy before `preview` itself ships.
> Pre-preview feedback is tracked in an internal document; feedback after `preview`
> ships to general external users is expected to move to a separate, external-facing
> mechanism (e.g. GitHub tickets — not yet decided).

**Purpose:** A minimal working preview that demonstrates the combined
`VisibilityRaster` + `VisibilityScatter` display in a single Bokeh layout,
gives astronomers a feel for the flagging workflow, and establishes the GUI
skeleton that the full implementation will fill in.

`visplot` is what astronomers run (`from cubevis import visplot; visplot(ms=...)`) —
this document uses `visplot` throughout for what the astronomer experiences.
`VisibilityPlotter` is the underlying Python class `visplot()` constructs and
returns; it appears here only where the distinction actually matters (mainly
"Construction approach" below). See the implementation plan §1 for the full
relationship between the two.

---

## Guiding principle

The preview should be **real, not mocked**. Every control that appears in
the layout either works or is visibly disabled with a tooltip explaining
why. Placeholder buttons labelled "Coming soon" breed distrust; a smaller
set of controls that all do something real breeds confidence.

---

## Layout architecture

> **Note:** this section (Display modes, Layout toggle, Skeleton) predates
> the unified `layout=` control and per-panel `kind=` described in
> "Layout and `kind`" under Construction approach below, which is
> authoritative for current constructor behavior. Flagged 2026-08-31,
> not yet rewritten — see that section's doc note for detail.

### No-server constraint

`VisibilityPlotter` uses the same CommMgr/Comm j2p/p2j transport as
`VisibilityRaster` and `VisibilityScatter` — there is no Bokeh server.
The entire layout is serialised to JavaScript once when `show()` is called.
After that point Python can update `ColumnDataSource` data and Bokeh model
properties (including `figure.width`, `figure.height`, and `figure.visible`),
but **cannot add, remove, or rearrange layout nodes**.

All layout rearrangements — the scatter/raster/both toggle, the
side-by-side ↔ over/under toggle, and figure resizing — are pure
JavaScript executed in the browser via `CustomJS` callbacks, with no
Python round-trip.

### Display modes

Three modes control which panels are visible:

| Mode | Raster | Scatter | Layout toggle |
|---|---|---|---|
| **Both** (default) | Visible | Visible | Active |
| **Raster only** | Visible | Hidden | Disabled |
| **Scatter only** | Hidden | Visible | Disabled |

"Hidden" means `figure.visible = false` — the figure remains in the Bokeh
document and continues to participate in shared `Range1d` axis linking, but
does not render.  The visible figure's `width` or `height` is expanded to
fill the vacated space using the same JS property-update mechanism as the
layout toggle.

The layout toggle `RadioButtonGroup` is **disabled** (not hidden) in
single-panel modes, with a tooltip *"Layout — available in Both mode"*.
Disabling avoids a toolbar reflow that would otherwise require layout
reconstruction.

### Layout toggle

A second `RadioButtonGroup` in the toolbar:

```
[ Side by Side | Over / Under ]
```

Fires a `CustomJS` callback that flips the plot area flex container
direction and resizes both figures:

```javascript
const overUnder = cb_obj.active === 1;
plotContainer.style.flexDirection = overUnder ? 'column' : 'row';
rasterFigure.width   = overUnder ? fullWidth  : halfWidth;
rasterFigure.height  = overUnder ? halfHeight : fullHeight;
scatterFigure.width  = overUnder ? fullWidth  : halfWidth;
scatterFigure.height = overUnder ? halfHeight : fullHeight;
```

No Python handler needed. Bokeh propagates `width`/`height` changes
through the existing document sync mechanism.

### Screen real estate

A typical high-resolution laptop (MacBook Pro 14/16", Dell XPS 15,
Framework 13) delivers roughly **1280×800 – 1440×900 CSS pixels** after
HiDPI scaling. After browser chrome (~90px) and Jupyter notebook
overhead (~200px), the usable plot area is approximately **500–600px
tall** and **900–1100px wide** (sidebar consuming ~280px).

At 50/50 over/under this yields ~240px per panel — workable for scatter
but cramped for raster. **Side by side is therefore the default for Both
mode**: each panel gets the full ~500px of height and ~400–450px of
width. Over/under is offered as a user toggle and is set automatically
by presets where vertical axis alignment adds analytical value.

In single-panel modes the visible figure expands to the full plot area
width (side-by-side equivalent) regardless of the disabled layout toggle
setting.

### Skeleton

```
┌─────────────────────────────────────────────────────────────────────┐
│  Toolbar                                                            │
│  [Plot ▶] [Reload ↺]  |  [● Both ○ Raster ○ Scatter]                │
│  [○ Side by Side ● Over/Under]  |  [vplot] [radplot] [Waterfall]    │
│  [🚩 FlagTool] [🏳 UnflagTool] [⚑ Flag†]                             │
├──────────────────┬──────────────────────────────────────────────────┤
│  Sidebar         │  ┌──────────────────┐  ┌──────────────────────┐  │
│  (~280px)        │  │  Raster panel    │  │  Scatter panel       │  │
│  [Data]          │  │                  │  │                      │  │
│  [Raster axes]   │  │  (side by side   │  │  default; toggled    │  │
│  [Scatter axes]  │  │   by default)    │  │  to over/under or    │  │
│                  │  │                  │  │  hidden by mode)     │  │
│                  │  └──────────────────┘  └──────────────────────┘  │
├──────────────────┴──────────────────────────────────────────────────┤
│  Status bar                                                         │
└─────────────────────────────────────────────────────────────────────┘
† Write-to-disk & flag accumulation — full release; preview demonstrates the selection gesture only
```

---

## Operating modes

### Independent mode (default)

The user controls raster axes and scatter axes separately via their own
sidebar sections. Any combination is valid. The panels are autonomous —
linked axis behaviour activates opportunistically when both x-axes show
the same dimension, but is not required.

Sidebar axis sections reflect the active display mode: in Raster only
mode the scatter axis section is hidden (sidebar `Div.visible = false`);
in Scatter only mode the raster axis section is hidden.  These are simple
Bokeh model property updates fired by the same JS callback as the display
mode toggle.

### Preset mode

A named preset takes ownership of **both** panels simultaneously, setting
axes and layout in one JS action. Presets always switch to **Both** mode
before applying their configuration — a preset never applies to a
single-panel view.

| Preset | Raster | Scatter | Layout |
|---|---|---|---|
| **vplot** | TIME × BASELINE, Amplitude | Amp vs Time | Side by side |
| **radplot** | BASELINE × UVDIST, Amplitude | Amp vs UVdist | Side by side |
| **Waterfall** | TIME × CHANNEL, Amplitude | Amp vs Time | Over / under |

After a preset is applied the user can adjust either panel's controls
independently. The preset label clears (toolbar shows "Custom") as soon
as any axis control diverges from the preset values.

---

## What the preview includes

### 1. Display mode toggle (working)

A `RadioButtonGroup` in the toolbar:

```
[ ● Both  ○ Raster only  ○ Scatter only ]
```

`CustomJS` callback:
- Sets `figure.visible` on the appropriate figure
- Resizes the remaining visible figure to fill the plot area
- Enables or disables the layout toggle
- Shows or hides the corresponding sidebar axis section

### 2. Layout toggle (working, Both mode only)

```
[ Side by Side | Over / Under ]
```

`CustomJS` callback flips the flex container direction and resizes both
figures.  Disabled with tooltip in single-panel modes.

### 3. Session-scoped layout preference memory (working)

A `ColumnDataSource` serialised at initialisation holds a JSON string
mapping axis-combination keys to preferred layout modes:

```
{ "TIME:BASELINE:AMPLITUDE:UVDIST:AMPLITUDE":  "side",
  "TIME:CHANNEL:AMPLITUDE:TIME:AMPLITUDE":      "over",
  "TIME:BASELINE:AMPLITUDE:_:_":               "side"  }
```

The `_:_` sentinel is used when one panel is hidden.  When the user
manually toggles the layout or display mode, the JS callback writes the
current key and chosen mode into the source.  When axes change and Plot
is pressed, JS reads the source and restores the previously chosen layout
for that combination, falling back to the preset default if no preference
is recorded.

> This section originally noted the source "is accessible from Python via
> CommMgr if cross-session JSON file persistence is added later" — that
> need is now a settled requirement, not a maybe: see "Save/load layout
> configuration" below and §3.1e of the implementation plan. The two
> mechanisms stay distinct rather than merging, though: this one is an
> automatic, silent, per-axis-combination *sticky preference*, reset every
> page load; `config=`/Save Layout is an explicit, user-triggered,
> durable *file*, save-then-share-then-reload. Same underlying idea
> (remember an arrangement choice), different persistence layer and
> different trigger — worth keeping separate rather than trying to make
> one subsume the other.

### 4. Data selection (sidebar — working)

| Control | Works in preview |
|---|---|
| MS / PS path (read-only `Div`, set at construction) | ✓ |
| Data column (`Select`: DATA / CORRECTED / MODEL) | ✓ |
| Field (`Select`, populated from `ObservationMetadata`) | ✓ |
| SPW (`MultiSelect`, populated from metadata) | ✓ |
| Polarization checkboxes | ✓ |
| Scan, antenna, time, UV range text inputs | Present; not yet wired to backend |

Unwired fields accept and retain input but do not filter the backend
query.  A note beneath each reads *"Full selection — full release"*.

### 5. Axis controls (sidebar — partially working)

**Raster** (hidden in Scatter only mode):
- Y axis: TIME, BASELINE (working)
- X axis: CHANNEL, TIME (working)
- Quantity: AMPLITUDE, PHASE (working)

**Scatter** (hidden in Raster only mode):
- X axis: UVDIST, TIME, FREQUENCY (working)
- Y axis: AMPLITUDE, PHASE, REAL, IMAGINARY, U, V (working — one ScatterLayer per selected polarisation, XX and YY by default, composited by Datashader; U/V added this session, deliberately unmasked by flags)
- Multi-layer *controls* (user-facing add/remove layer UI): absent
- Colour-by-metadata axis: absent

Axis labels use SI-prefix formatting (e.g. "GHz" rather than raw Hz) derived from `AxisInfo`, which records what was actually plotted rather than relying on `Axis.label` (the enum display name, which does not know about data ranges or units chosen at query time). This parity is enforced by a JS/Python harness.

### 6. Colormap controls (sidebar — working)

Both panels default to Datashader's `eq_hist` (histogram equalization)
reduction rather than linear scaling, which resolves the low-amplitude
saturation seen in early scatter testing (dense low-value data collapsing
to a uniform dark color under linear mapping).

Each panel embeds its own `colormap_controls()` widget (a small column
from `VisibilityRaster` / `VisibilityScatter` — see Phase 0 CM-series
in the implementation plan):

- Scaling dropdown: linear, log, sqrt, square, gamma, eq_hist (default)
- Alpha / gamma numeric input (shown conditionally per scaling choice)
- Min / max numeric range inputs

Changing scaling re-shades from the cached aggregation — no backend
re-query — so this is fast even on the preview's modest sis14 dataset and
will remain fast at full scale.

### 7. Toolbar summary

| Control | Behaviour |
|---|---|
| **Plot ▶** | Re-queries both active backends; re-renders; updates preference store |
| **Reload ↺** | Same as Plot ▶ in the preview (no `FlagDB` state to preserve or clear) |
| **[ Both \| Raster \| Scatter ]** | JS: sets visibility, resizes, updates sidebar |
| **[ Side by Side \| Over / Under ]** | JS: flips layout, resizes figures |
| **[vplot] [radplot] [Waterfall]** | JS: switches to Both, sets axes and layout |
| **FlagTool 🚩** | Custom drag tool demonstrating the flagging gesture. Drag → draws rubber-band selection box; status bar confirms the drawn region (gated on 1:1 pixel resolution; rejected with message otherwise). Click → zooms to 1:1. "Not unselectable" — re-click re-triggers zoom. No `FlagDB` or `FlagDelta` in the preview. |
| **UnflagTool 🏳** | Same `FlagTool`, `flag=False`. Demonstrates the unflag selection gesture; no `FlagDB` in the preview. |
| **Export PNG 📷** | Writes the current view (zoom included) to a server-side path; absolute path reported in the status bar. GUI defaults to light theme for PNG output regardless of current GUI theme. |
| **Flag ⚑** | `TipButton`; tooltip: *"Write flags to disk — full release"* |


Iteration (Prev/Next), Locate, Save plot, and Copy flagdata are absent
from the toolbar — no stubs, to keep the toolbar uncluttered.

### 8. Linked axis behaviour (working)

When both panels share the same x-axis dimension (e.g. both show TIME),
a shared Bokeh `Range1d` links their x-axes.  Panning or zooming one
panel moves the other in sync.  Hidden figures remain linked — switching
back to Both mode restores the synchronised view correctly.

### 9. Status bar

A single `row()` layout with two halves, updated on every Plot press and
every mode/layout change:

```
[green: Ready — sis14_twhya.ms | Mode: Both | Layout: Side by Side]
[red:   Flagged 12 cells (or rejection: "Zoom to 1:1 to flag")              ]
```

The **green left half** shows the dataset/config summary. The **red right half**
shows flagging tool feedback: a rejection message when a draw is below 1:1
resolution, or confirmation that a selection region was drawn. On sidebar
widget hover, both halves are replaced by the context-sensitive hint.
In single-panel modes the green half omits the axis summary for the hidden panel.

The hover probe reports all visible layers in the status bar (one reading per layer, stable index order so fields do not shift as the cursor moves). An em dash (—) appears for any layer with no data at the hovered location — this is intentional ("no data here"), not a bug. Hidden layers (alpha = 0) are omitted entirely. At high zoom the probe uses a screen-pixel search budget so a hover slightly off a drawn mark still resolves correctly.

---

## What the preview explicitly omits

Absent with no stubs:

- Flag accumulation (`FlagDB`, `FlagDelta`) and write-to-disk — full release; preview demonstrates the selection gesture only
- Flag versions, flag extend
- Full Locate sidebar (cursor tracking info divs work; locate results table — full release)
- Synchronized cross-panel cursor, Tier 1 (same-axis Span crosshair) — ✅ working in duo mode for all panel-kind combinations; documents were stale. Tier 2 (cross-axis row-level highlight via CommMgr probe) — not yet built; see §4.7 of the implementation plan
- Averaging controls
- Iteration (Prev/Next antenna/baseline) — absent from this (pre-preview) release, but now required for the general-user `preview` release; a scoped Field/SPW MVP is planned as Phase 2.5 (`I-series`) in the implementation plan, ahead of the full antenna/baseline/scan/time iteration engine in Phase 3
- Save/load layout configuration (`config=` constructor parameter, Save Layout toolbar action) — absent from this (pre-preview) release, but now required for the general-user `preview` release. Schema and round-trip design settled 2026-08-31 (see "Saving and loading layout" below and §3.1e of the implementation plan); not yet implemented — tracked as P-12
- Calibration sidebar section
- Colour-by-metadata axis in scatter
- Colorbar (partial — `plot_left`/`plot_right` placement works; display-scope GUI colorbar deferred)
- Multi-layer scatter *controls* (the rendering machinery and probe both handle multiple layers; the absent piece is user-facing UI to add/remove layers)
- Draggable panel divider

---

## Construction approach

### Astronomer-facing API

`visplot` is the end-user application, not a composable programmer
component. Its signature accepts strings, numbers, and lists — no internal
objects. It forwards these to construct a `VisibilityPlotter` instance and
returns it. The same call works in the preview and in the full release; no
API changes will be needed later.

```python
from cubevis import visplot

plotter = visplot(
    # Data source — exactly one of ms or ps
    ms   = "sis14_twhya_calibrated_flagged.ms",   # MSv2
    # ps = "sis14_twhya_calibrated_flagged.ps.zarr", # MSv4

    # Reduction backend — selects which ReductionContext is constructed
    backend         = "auto",   # "auto" | "casa6" | "radps" | "remote" | "null"
    remote_endpoint = None,     # required only when backend="remote"

    # Initial selection — all optional strings or numbers
    field       = "0637-752",   # name or index; default: first field
    spw         = "0,1,2,3",    # MSSelection string; default: all
    antenna     = "",           # MSSelection string; default: all
    scan        = "",           # MSSelection string; default: all
    timerange   = "",           # MSSelection string; default: all
    uvrange     = "",           # e.g. "0~50klambda"; default: all
    correlation = "XX,YY",      # default: all available
    datacolumn  = "data",       # "data", "corrected", "model"

    # Explicit axis override (precedence: explicit > preset > hardcoded default)
    # Validated against the same lists that drive the GUI dropdowns
    raster_y   = None,          # e.g. "Time", "Baseline"
    raster_x   = None,          # e.g. "Channel", "Frequency"
    raster_qty = None,          # e.g. "Amplitude", "Phase"
    scatter_x  = None,          # e.g. "UVDist", "U"
    scatter_y  = None,          # e.g. "Amplitude", "V"

    # Initial display configuration
    layout      = "side",           # "one", "side", "over" — or "raster"/"scatter"
                                     # as shorthand for layout="one", kind=<that>
    kind        = None,             # "raster" (default) or "scatter" — which panel
                                     # kind leads; see "Layout and kind" below
    config      = None,             # path to a saved layout JSON file — see
                                     # "Saving and loading layout" below
    preset      = None,             # "vplot", "radplot", "waterfall"

    # Flagging
    enable_flagging = True,     # False → no FlagTool/UnflagTool at all (inspect-only)

    # Initial zoom / axis ranges — optional
    time_range   = None,        # (start, end) as ISO strings or MJD floats
    freq_range   = None,        # (start, end) in Hz
    uvdist_range = None,        # (min, max) in metres
)

plotter.show()  # returns a Bokeh layout for notebook embedding

# Headless scripted export — no Bokeh figure, no browser needed
vp = visplot(ms="sis14.ms", headless=True,
             plot_width=1400, plot_height=700)
vp(plotfile="amp.png", theme="light")               # full-extent view
for spw in (0, 1, 2, 3):
    vp(plotfile=f"amp_spw{spw}.png", spw=[spw])     # iterate over SPWs
```

### Layout and `kind`

`layout=` controls arrangement only: `"one"` (single panel), `"side"`
(both panels, side by side — default), or `"over"` (both panels, one
above the other). `kind=` (`"raster"` or `"scatter"`, default `"raster"`)
controls which panel kind leads, and is independent of arrangement:

* With `layout="one"`, `kind` is the single visible panel's kind.
* With `layout="side"`/`"over"`, both panels are always shown, one
  raster and one scatter, exactly as today — `kind` only decides which
  one starts in the primary/first position on screen; the other always
  takes the complementary kind.

`layout="raster"` and `layout="scatter"` are also accepted, as shorthand
for `layout="one", kind="raster"`/`"scatter"` — so a single scatter
launch can be written either way:

```python
visplot(ms="sis14.ms", layout="one", kind="scatter")
visplot(ms="sis14.ms", layout="scatter")           # equivalent shorthand
```

Supplying both the shorthand and a conflicting explicit `kind=` (e.g.
`layout="scatter", kind="raster"`) raises `ValueError` immediately.
Either kind can still be switched at runtime from the sidebar's
per-panel Raster/Scatter toggle after launch, same as today —
`layout=`/`kind=` only set the *initial* state.

> **Doc note (2026-08-31).** The "Layout architecture" section above
> (Display modes, the separate Layout toggle, and the Skeleton diagram)
> describes an earlier design — a standalone `mode=` toggle
> (Both/Raster only/Scatter only) plus a separate Side-by-Side/Over-Under
> toggle. The shipped toolbar has since unified those into the single
> layout control described here and in the implementation plan's §3.1b;
> `mode=` no longer exists as a constructor parameter. That section is
> flagged as stale rather than rewritten in this pass — treat this
> subsection and the implementation plan as authoritative for
> `layout=`/`kind=` until the fuller rewrite happens.

### Saving and loading layout

**Status: design settled 2026-08-31 (implementation plan §3.1e); not yet
implemented. Required for the general-user `preview` release** — see the
"What the preview explicitly omits" entry above.

A `config=` constructor parameter loads a previously saved arrangement:

```python
visplot(ms="sis14.ms", config="my_layout.json")            # GUI
visplot(ms="sis14.ms", headless=True, config="grid.json")  # PNG export
```

**`config` deliberately captures layout, not data.** It never contains
which MS/PS was open, which field/SPW/correlation/data-column was
selected, the current pan/zoom viewport, or a colormap's manual clip
range (`vmin`/`vmax`) — all of that is specific to the dataset that was
open when it was saved, and would not necessarily make sense against a
different one. The line is drawn precisely: a field is included if its
valid values come from a fixed vocabulary baked into the code (an axis
dimension name, a palette name, a scaling mode) — the same static
per-role options lists `raster_y=`/`scatter_x=`/etc. are already
validated against — and excluded if its valid values depend on what is
actually in the loaded MS/PS (an identifier, or a numeric range
reflecting real data). This is what makes a saved file safe to hand to a
colleague and load against an entirely different observation: every
value in it is guaranteed valid for *any* MS, because none of it was
ever looked up against one.

**GUI and headless PNG output use two different, but closely related,
schemas — not one shape with unused fields.** The GUI needs both panel
kinds saved for every panel, in case you flip Raster/Scatter after
loading; a batch PNG grid never switches anything, so saving an unused
second kind per cell would just be waste, and waste that scales with
however large the grid is. What the two schemas *do* share exactly is
the description of a single axis configuration (which axes, which
colormap, which scaling) — only how panels are addressed and paired
around that shared piece differs. `config=` stays one parameter either
way; the file declares which kind it is, and loading a `"png"` file into
a GUI session (or vice versa) fails immediately with a clear message,
not a confusing error partway through.

**GUI schema:**

```json
{
  "schema_version": 1,
  "target": "gui",
  "layout": "side",
  "theme": "dark",
  "panels": [
    {
      "slot": "A",
      "kind": "raster",
      "raster":  { "y": "BASELINE", "x": "TIME", "quantity": "AMPLITUDE",
                   "cmap": "inferno", "scaling": "eq_hist" },
      "scatter": { "x": "TIME", "y": "AMPLITUDE",
                   "cmap": "viridis", "scaling": "linear" }
    },
    {
      "slot": "B",
      "kind": "scatter",
      "raster":  { "y": "TIME", "x": "CHANNEL", "quantity": "AMPLITUDE",
                   "cmap": "inferno", "scaling": "eq_hist" },
      "scatter": { "x": "UVDIST", "y": "AMPLITUDE",
                   "cmap": "viridis", "scaling": "linear" }
    }
  ]
}
```

`"slot": "A"` means *whichever panel is currently on the left, beside
the sidebar* — not whichever panel started there. Panels can be swapped
on screen after launch, and that swap only lives in the browser, so
Save always re-checks which one is actually on the left at the moment
you save, rather than assuming it's still the original one. Loading is
simpler: a fresh launch always starts with `"A"` on the left, so this
takes care of itself on the way back in.

Each slot always carries both its `raster` and `scatter` sub-configs —
matching how the GUI already works internally, where both kinds are
always built and only one is shown — so toggling kind after loading
lands on a sensibly-configured panel rather than a hardcoded default.
`panels` is two entries today regardless of `layout` (even `"one"`
saves the hidden second slot, since it is still live underneath, just
not shown); it grows unchanged if a future larger split-panel mode
ships.

**PNG schema** (for `headless=True` / batch export — e.g. a grid of
per-antenna raster plots, no GUI ever built):

```json
{
  "schema_version": 1,
  "target": "png",
  "rows": 2,
  "cols": 2,
  "panels": [
    [
      { "kind": "raster",  "raster":  { "y": "BASELINE", "x": "TIME", "quantity": "AMPLITUDE",
                                          "cmap": "inferno", "scaling": "eq_hist" } },
      { "kind": "scatter", "scatter": { "x": "TIME", "y": "AMPLITUDE",
                                          "cmap": "viridis", "scaling": "linear" } }
    ],
    [
      { "kind": "scatter", "scatter": { "x": "UVDIST", "y": "AMPLITUDE",
                                          "cmap": "viridis", "scaling": "linear" } },
      { "kind": "raster",  "raster":  { "y": "TIME", "x": "CHANNEL", "quantity": "AMPLITUDE",
                                          "cmap": "inferno", "scaling": "eq_hist" } }
    ]
  ]
}
```

Read row by row, left to right, matching how the grid actually lays
out; each cell has exactly the one kind it will render, nothing
unused. This is display configuration only, same as the GUI schema —
which antenna/SPW/etc. each cell iterates over is data selection, still
supplied fresh per call the ordinary way, not stored in the file.

**Precedence**, extending the rule already used for axes (`explicit
argument > preset > hardcoded default`): `explicit kwarg > config file >
preset > hardcoded default`. `visplot(ms=..., config="saved.json",
correlation="XX")` loads everything from the file except correlation,
which the file never contained.

**Save** works the same way as **Export PNG**, and for the same reason:
in this no-server architecture, Python's own panel objects already stay
current for almost everything (`self._raster_y`, `self._cmap`,
`self._scaling`, etc. are refreshed on every Plot press and every
cmap/scaling widget submit) — the client-side state Python never learns
on its own is the layout arrangement and which panel is currently on
which side, since neither the layout toggle nor the panel-swap button
ever calls back to Python. So the Save Layout action needs exactly one
small comm round-trip, gathering only that gap — arrangement, current
left/right assignment, and each slot's current kind (re-derived from
live state at the moment of the click, not assumed from the last Plot
press, mirroring Export PNG's own "determine at click time" fix for the
same class of staleness) — and lets Python build the rest of the JSON
from the panel objects' own already-current attributes before writing
the file.

Passing both `ms` and `ps` raises a `ValueError` immediately with a
clear message.  Omitting both also raises `ValueError`.  All other
parameters are optional; defaults are chosen by inspecting the data
after opening (e.g. first field, all SPWs, all available polarizations).

The `backend=` parameter accepts a plain string or a `ReductionBackend`
enum value.  In the preview `"auto"` falls through to
`NullReductionContext` with a warning log (since `Casa6ReductionContext`
and `RadpsReductionContext` are not yet implemented), so display-only use
works cleanly in any environment.  Pass `"null"` explicitly to suppress
the warning.  See §4.12 of the implementation plan for the full
context-selection matrix, and for how to reach `VisibilityPlotter` directly
if you want the class rather than `visplot()`.

### What VisibilityPlotter does internally

`__init__` calls `open_ms()` or `open_ps()`, defined directly in
`visibility_plotter.py` (an internal implementation detail, not public
API — see the implementation plan's "Role of `open_ms()` / `open_ps()`"
section; no separate `factory.py` exists), passing `path`,
`backend`, and `remote_endpoint`.  These resolve the appropriate
`ReductionContext` and return an `(ObservationMetadata,
LocalVisibilityReader, ReductionContext)` triple that `__init__` stores
in private attributes.  It then constructs one `VisibilityRaster` and
one `VisibilityScatter` from those objects, wraps their figures in a CSS
flex container `Div`, builds the sidebar and toolbar as Bokeh widget
columns, initialises the preference `ColumnDataSource` with an empty
dict, and attaches `CustomJS` callbacks for the display mode toggle,
layout toggle, and presets.  The CommMgr transport used by the two
display classes is reused without modification.

The `FlagTool` j2p handler records the drawn region and updates the
status bar with confirmation or a rejection message. No `FlagDB` or
`FlagDelta` exists in the preview — flag accumulation, write-to-disk,
and undo/unflag persistence are Phase 1 deliverables (F-1 through F-10
in the implementation plan).

### Programmer-facing composable layer

Developers building their own tools or embedding individual display
classes use the lower-level API directly:

```python
from cubevis.toolbox.visplot import (
    MSv2Backend, LocalVisibilityReader,
    VisibilityRaster, VisibilityScatter,
    SelectionSpec, Axis,
)

backend = MSv2Backend("data.ms")
backend.open()
reader  = LocalVisibilityReader(backend)
sel     = SelectionSpec(field_name="0637-752", spw_ids=[0, 1])

raster  = VisibilityRaster(reader, sel, y_dim=Axis.TIME,
                           x_dim=Axis.CHANNEL, quantity=Axis.AMPLITUDE,
                           width=800, height=500)
```

`VisibilityPlotter` does not expose its internal `VisibilityRaster`,
`VisibilityScatter`, `LocalVisibilityReader`, or `ReductionContext`
instances as public attributes — these are implementation details.
The composable layer exists independently and is the right choice for
programmatic or pipeline use; `VisibilityPlotter` is the right choice
for interactive data inspection.

---

## Corner cases handled by the JS callbacks

| Situation | Handling |
|---|---|
| Preset pressed in single-panel mode | Switch to Both first, then apply preset axes and layout |
| Layout toggle in single-panel mode | Button disabled; no callback fired |
| Shared `Range1d` when a panel is hidden | Range remains linked; restores correctly on return to Both |
| Preference key when a panel is hidden | Sentinel `_:_` used for absent panel axes |
| Status bar in single-panel mode | Omits axis summary for hidden panel |
| "Custom" label after diverging from preset | JS compares current axis values to preset table; clears preset highlight |

---

## Success criteria for the preview

A reviewer in a Jupyter notebook should be able to:

1. Open an MSv2 or MSv4 dataset and see both panels render side by side.
2. Change field or SPW and press Plot; both panels update.
3. Change the raster quantity from Amplitude to Phase and press Plot.
3a. Switch the scatter colormap scaling from eq_hist to linear and back;
    confirm the low-amplitude region goes from visually flat (linear) to
    showing structure (eq_hist).
4. Switch to Raster only; scatter hides, raster expands; layout toggle disables.
5. Switch back to Both; scatter reappears at correct size; layout toggle re-enables.
6. Toggle to Over / Under; both panels reflow and resize correctly.
7. Press the Waterfall preset; mode switches to Both, axes set, layout switches to Over / Under.
8. Press the vplot preset; axes and layout update; layout returns to Side by Side.
9. Manually override the layout after a preset; press Plot; the manual choice is remembered.
10. Pan the time axis in the raster when both show TIME on x; the scatter time axis follows.
11. Activate `FlagTool`; zoom to 1:1; draw a box; status bar confirms the selection region.
11a. Draw without zooming to 1:1; status bar shows rejection message.
11b. Hover a scatter point at high zoom; the status bar reports a value for each visible polarisation, with an em dash (—) for any layer with no data at that location.
12. Activate `UnflagTool`; draw a box; status bar confirms the unflag gesture.
13. Read the status bar and know which dataset, mode, layout, and selection is active.
14. Press Export PNG; a PNG file appears at the path shown in the status bar, with light-theme chrome regardless of the current GUI theme.

That is sufficient to validate the no-server layout approach, communicate
the design intent for both independent and preset operating modes, and
gather feedback before flagging and iteration work begins.
