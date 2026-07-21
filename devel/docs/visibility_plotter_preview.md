# VisibilityPlotter — Preview Specification

**Purpose:** A minimal working preview that demonstrates the combined
`VisibilityRaster` + `VisibilityScatter` display in a single Bokeh layout,
gives astronomers a feel for the flagging workflow, and establishes the GUI
skeleton that the full implementation will fill in.

---

## Guiding principle

The preview should be **real, not mocked**. Every control that appears in
the layout either works or is visibly disabled with a tooltip explaining
why. Placeholder buttons labelled "Coming soon" breed distrust; a smaller
set of controls that all do something real breeds confidence.

---

## Layout architecture

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
│  [□ Box Select] [⚑ Flag†] [⟲ Undo†]                                 │
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
† Disabled in preview
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
is recorded.  The source is accessible from Python via CommMgr if
cross-session JSON file persistence is added later.

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
- Y axis (single layer): AMPLITUDE (working)
- Multi-layer and colour-by-axis: absent

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
| **Reload ↺** | Same as Plot in the preview |
| **[ Both \| Raster \| Scatter ]** | JS: sets visibility, resizes, updates sidebar |
| **[ Side by Side \| Over / Under ]** | JS: flips layout, resizes figures |
| **[vplot] [radplot] [Waterfall]** | JS: switches to Both, sets axes and layout |
| **Box Select** | Activates Bokeh box-select tool; on box close immediately adds `FlagDelta` to `FlagDB` and re-renders flagged overlay in red — no button press required |
| **Flag ⚑** | Disabled; tooltip: *"Write flags to disk — full release"* |
| **Undo ⟲** | Disabled; tooltip: *"Undo — full release"* |

Iteration (Prev/Next), Locate, Save plot, and Copy flagdata are absent
from the toolbar — no stubs, to keep the toolbar uncluttered.

### 8. Linked axis behaviour (working)

When both panels share the same x-axis dimension (e.g. both show TIME),
a shared Bokeh `Range1d` links their x-axes.  Panning or zooming one
panel moves the other in sync.  Hidden figures remain linked — switching
back to Both mode restores the synchronised view correctly.

### 9. Status bar

A `Div` updated on every Plot press and every mode/layout change:

```
Ready — sis14_twhya_calibrated_flagged.ms  |  Mode: Both  |  Layout: Side by Side
       Field: 0637-752  |  SPW: 0,1,2,3  |  Col: DATA
```

In single-panel modes the status bar omits the axis summary for the
hidden panel.

---

## What the preview explicitly omits

Absent with no stubs:

- Writing flags to disk (FlagDB accumulation and red overlay work; disk write — full release)
- Flag versions, flag extend
- Locate / hover probe (Bokeh hover tool active but no custom handler)
- Synchronized cross-panel cursor (Tier 1 same-axis Span and Tier 2 cross-axis
  row-level highlight — both full release; see main plan §4.7)
- Averaging controls
- Iteration (Prev/Next antenna/baseline)
- Calibration sidebar section
- Colour-by-metadata axis in scatter
- PNG export
- Multi-layer scatter
- Draggable panel divider

---

## Construction approach

### Astronomer-facing API

`VisibilityPlotter` is an end-user application, not a composable
programmer component.  Its constructor accepts strings, numbers, and
lists — no internal objects.  The same call works in the preview and in
the full release; no API changes will be needed later.

```python
from cubevis.toolbox.visplot import VisibilityPlotter

plotter = VisibilityPlotter(
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

    # Initial display configuration
    mode        = "both",           # "both", "raster", "scatter"
    layout      = "side",           # "side", "over"
    preset      = None,             # "vplot", "radplot", "waterfall"

    # Initial zoom / axis ranges — optional
    time_range   = None,        # (start, end) as ISO strings or MJD floats
    freq_range   = None,        # (start, end) in Hz
    uvdist_range = None,        # (min, max) in metres
)

plotter.show()  # returns a Bokeh layout for notebook embedding
```

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
context-selection matrix.

### What VisibilityPlotter does internally

`__init__` calls `open_ms()` or `open_ps()` from `factory.py` (an
internal implementation detail, not public API), passing `path`,
`backend`, and `remote_endpoint`.  The factory resolves the appropriate
`ReductionContext` and returns an `(ObservationMetadata,
LocalVisibilityReader, ReductionContext)` triple that `__init__` stores
in private attributes.  It then constructs one `VisibilityRaster` and
one `VisibilityScatter` from those objects, wraps their figures in a CSS
flex container `Div`, builds the sidebar and toolbar as Bokeh widget
columns, initialises the preference `ColumnDataSource` with an empty
dict, and attaches `CustomJS` callbacks for the display mode toggle,
layout toggle, and presets.  The CommMgr transport used by the two
display classes is reused without modification.

The box-select j2p handler always adds a `FlagDelta` to `FlagDB` and
immediately re-renders the flagged overlay in red — no button press
required.  This matches the AIPS TVFLG immediate-feedback workflow.
The Flag ⚑ button writes the accumulated `FlagDB` entries to disk via
`ReductionContext.commit_flags()` and is the only step that touches the
MS or Processing Set.  Undo ⟲ pops the last `FlagDelta` from `FlagDB`
and re-renders; it works freely until Flag is pressed.

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
11. Draw a box-select region; flagged data immediately appears in red in both panels without pressing Flag.
12. Press Undo; the red overlay clears for that region.
13. Read the status bar and know which dataset, mode, layout, and selection is active.

That is sufficient to validate the no-server layout approach, communicate
the design intent for both independent and preset operating modes, and
gather feedback before flagging and iteration work begins.

---

## Appendix: Preview implementation status (July 2026)

This appendix records which items from the preview specification were
delivered, which were modified in scope, and what was added beyond the
original spec. It is intended to serve as a reference for the first
round of stakeholder feedback.

---

### Spec items — delivered as specified

| § | Item | Notes |
|---|---|---|
| 1 | Display mode toggle (Both / Raster only / Scatter only) | `CustomJS`; sidebar axis sections hide/show correctly |
| 2 | Layout toggle (Side by Side / Over Under) | Dual-container approach (row + column, one hidden); avoids flex-direction mutation |
| 3 | Session-scoped layout preference memory | `ColumnDataSource` JSON store; written by layout JS, read on Plot |
| 4 | Sidebar — data selection | Field (with "All fields" sentinel), SPW, Correlation, Data column |
| 4 | Sidebar — axis controls | Raster Y/X/Qty, Scatter X/Y, with colormap controls |
| 5 | Presets (vplot, radplot, Waterfall) | Replot fires automatically on preset press |
| 6 | Toolbar skeleton | Plot ▶, Reload ↺, mode, layout, presets, Flag ⚑†, Undo ⟲† |
| 7 | Flag ⚑ / Undo ⟲ disabled with explanation | Replaced with `TipButton`; tooltip explains preview limitation |
| 8 | Linked x-axis behaviour | Shared `Range1d` when raster x == scatter x; restored on return to Both |
| 9 | Status bar | Updated on every Plot press and mode/layout change |

---

### Spec items — modified in scope or implementation

| § | Item | Difference from spec |
|---|---|---|
| Layout | Dual-container rather than flex-direction | Spec described CSS flex-direction mutation; implemented as two Bokeh containers (row + column) toggled via `.visible`. More reliable in Bokeh's no-server architecture. |
| 11 | Box-select → red overlay | Box-select j2p fires and `FlagDelta` accumulates in `FlagDB`. Red overlay re-render is a **stub** (Phase 1 F-9/F-10) — no visual feedback yet. Spec said "immediately appears in red". |
| 12 | Undo | `FlagDB.pop()` / `FlagDB.undo()` implemented in Python; UI button disabled. |
| radplot preset | Raster x axis | Spec has `BASELINE × UVDIST`; implemented as `BASELINE × TIME` since UVDIST is not a native raster MS dimension (`MSv2Backend` does not support it as a raster y/x). Scatter x = UVDIST as specified. |
| Preference key sentinel | `_:_` for hidden panel | Implemented; layout preference restored on axes change. |
| "Custom" label after diverging from preset | Not implemented | JS does not track whether current axes match a preset and relabel the toolbar. Low priority; deferred. |

---

### Items added beyond the original spec

| Item | Description |
|---|---|
| **Collapsible sidebar** | `⟨` / `⟩` toggle button collapses the left panel; figures expand via `sizing_mode="stretch_width"` |
| **Dark / Light mode** | Full theme toggle covering figures, sidebar, info divs, status bar, and page background |
| **Linked cursor spans** | Dashed `Span` lines in each figure track cursor position in the other figure when axes are compatible; axis-aware (vertical or horizontal) and orientation-correct |
| **Cursor tracking info divs** | `_info_div` below each figure shows Amplitude, Channel/Time/Frequency, Field, Scan, BL on hover |
| **Synchronised Bokeh toolbars** | Both figures keep their native Bokeh toolbars (pan, box zoom, wheel zoom, reset, save); tool activation synced via `js_on_change("active_drag")` |
| **Context-sensitive sidebar hints** | On mouse-enter, the status bar is replaced by an MS-specific hint for each sidebar widget (actual scan IDs, antenna names, observation time range, format examples) via `EvTextInput` + `MouseEnter`/`MouseLeave` |
| **Tooltips on all toolbar buttons** | `Tip` wraps all active buttons; `TipButton` replaces Flag and Undo with informative hover tooltips |
| **Multi-layer scatter** | One `ScatterLayer` per selected polarisation (XX and YY by default), composited by Datashader |
| **`ReductionBackend` enum** | `str` enum (`"auto"`, `"casa6"`, `"radps"`, `"remote"`, `"null"`) selects reduction context; accepted as plain string by constructor |
| **Widget/render consistency** | "All fields" sentinel in Field dropdown ensures sidebar initial state matches the initial render (`field_names=None`); `_last_raster_selection` prevents spurious raster re-renders when only scatter axes change |
| **Notification div** | Transient red notification area above the status bar for Python-side warnings and errors (e.g. unsupported axis combination) |

---

### Success criteria — assessment

| # | Criterion | Status |
|---|---|---|
| 1 | Open MSv2 / MSv4; both panels render | ✅ MSv2 confirmed; MSv4 path present |
| 2 | Change field or SPW; press Plot; both panels update | ✅ |
| 3 | Change raster quantity Amplitude → Phase | ✅ |
| 3a | Colormap scaling eq_hist ↔ linear | ✅ |
| 4 | Raster only: scatter hides, raster expands, layout disables | ✅ |
| 5 | Back to Both: scatter reappears, layout re-enables | ✅ |
| 6 | Over / Under: both panels reflow | ✅ |
| 7 | Waterfall preset: Both mode, axes set, Over/Under | ✅ |
| 8 | vplot preset: axes and layout update | ✅ |
| 9 | Manual layout override after preset; Plot remembers choice | ✅ |
| 10 | Pan shared TIME x-axis; scatter follows | ✅ |
| 11 | Box-select → red overlay immediately | ⚠️ Box-select fires j2p and accumulates in `FlagDB`; red overlay not yet rendered (Phase 1) |
| 12 | Undo → red overlay clears | ⚠️ `FlagDB.undo()` implemented; UI disabled in preview |
| 13 | Status bar shows dataset / mode / layout / selection | ✅ |

---

### Known limitations for stakeholder feedback

1. **Box-select flag overlay** — selection accumulates in `FlagDB` but no
   red visual feedback is rendered yet. Astronomers should be aware that
   box-select does work (j2p fires) but the AIPS TVFLG-style immediate
   red overlay is a Phase 1 deliverable.

2. **Scan / Antenna / Time range / UV range** — sidebar fields accept
   text input and sidebar hints show valid values, but these parameters
   are not yet wired to the backend query. Only Field, SPW, Correlation,
   and Data column filter the displayed data.

3. **radplot raster x-axis** — shows TIME rather than UVDIST on the raster
   panel. UVDIST is only available as a scatter x-axis; it cannot be used
   as a native raster dimension against MSv2.

4. **No iteration** — Prev/Next antenna/baseline/scan controls are absent.

5. **No PNG export** — Bokeh's built-in save tool is present in the
   toolbar but produces the default Bokeh PNG, not a publication-quality
   export.

6. **Preset "Custom" label** — the toolbar does not detect when axes have
   diverged from a preset and relabel accordingly.
