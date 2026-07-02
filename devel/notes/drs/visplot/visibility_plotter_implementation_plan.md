# VisibilityPlotter — Implementation Plan

**Project:** cubevis / casangi  
**Status:** Pre-implementation — architecture settled, no code written  
**Last updated:** 2025-06

---

## Table of Contents

1. [Background and motivation](#1-background-and-motivation)
2. [Astronomer flagging workflow](#2-astronomer-flagging-workflow)
3. [Architecture overview](#3-architecture-overview)
4. [VisibilityPlotter capability set](#4-visibilityplotter-capability-set)
5. [GUI layout](#5-gui-layout)
6. [Implementation phases and punch list](#6-implementation-phases-and-punch-list)
7. [Appendix A — API stubs](#appendix-a--api-stubs)
8. [Appendix B — File inventory and change summary](#appendix-b--file-inventory-and-change-summary)

---

## 1. Background and Motivation

`VisibilityPlotter` is a replacement for `plotms` and `msview` in CASA6, targeting both
MSv2 and MSv4 / Processing Set data as used by NRAO (ALMA, VLA), RADPS/AstroVIPER, and
future ngVLA pipelines. It combines two already-implemented display classes:

- **`VisibilityRaster`** — Datashader-rendered 2D heatmap of a visibility quantity
  (amplitude, phase, flag fraction, etc.) over two native axes (time × channel, baseline × time, etc.)
- **`VisibilityScatter`** — Multi-layer Datashader scatter plot of one or more y-axis
  quantities vs a free x-axis

Both classes share the same `CommMgr`/`Comm` j2p/p2j transport, `_state_source` pattern,
and two-level pan/zoom architecture. `VisibilityPlotter` wraps them in a single application
with a shared selection panel, flagging toolbar, and the `ReductionContext` abstraction for
calibration and flag commit.

Requirements from JIRA ticket **CASR-385** (Plotting tool improvements, 12-month horizon)
inform the Phase 4 punch list items prefixed **X-**:

- Faster and more reliable than `plotms` — addressed by architecture (Datashader, two-level
  pan/zoom, clean read/write separation)
- Waterfall plot and flagging similar to AIPS TVFLG, SPFLG, FTFLG — TVFLG covered by
  `VisibilityRaster` TIME × CHANNEL with immediate `FlagDB` accumulation and red overlay
  on box close; SPFLG requires multi-panel tiled raster (X-1); FTFLG covered by
  baseline-averaged TIME × CHANNEL configuration
- Phase RMS vs time and frequency — new derived axis `Axis.PHASE_RMS` (X-2)
- ngVLA TB-scale datasets — remote execution requirement confirmed; `RemoteReductionContext`
  elevated in priority (X-4, X-5)

### Key advantages over plotms / msview

| Capability | plotms | msview | VisibilityPlotter |
|---|---|---|---|
| Raster display | ✗ | ✓ | ✓ |
| Scatter display | ✓ | ✗ | ✓ |
| Simultaneous raster + scatter | ✗ | ✗ | ✓ |
| MSv4 / Processing Set | ✗ | ✗ | ✓ |
| Jupyter / notebook embedding | Limited | ✗ | ✓ |
| Remote cluster execution | ✗ | ✗ | ✓ (via `RemoteReductionContext`) |
| Closure phase display | ✗ | ✗ | Planned (Phase 4) |
| Model overlay | Limited | ✗ | ✓ (scatter layer) |
| Difmap-style vplot mode | ✗ | ✗ | ✓ (named preset) |

---

## 2. Astronomer Flagging Workflow

Understanding the flagging workflow is the primary driver of the feature set.
Flagging is not a single step — it recurs throughout the reduction cycle:

```
Import (ASDM → MS / Processing Set)
    ↓
Pre-calibration inspection & flagging       ← primary VisibilityPlotter use case
    ↓
Calibration (bandpass → gaincal → fluxscale → applycal)
    ↓
Post-calibration inspection & flagging      ← secondary use case (CORRECTED column)
    ↓
Imaging / self-calibration loop
    ↓
Final image analysis
```

### 2.1 What the astronomer is looking for

| Problem type | Signature | Best plot configuration |
|---|---|---|
| RFI (narrowband) | Amplitude spike at specific channel(s) | Raster: time × channel |
| RFI (broadband/transient) | Spike at specific time, all channels | Scatter: amp vs time |
| Bad antenna | All baselines to one antenna deviant | Scatter: amp vs time, colour-by-baseline, iterate by antenna |
| Bad baseline | One pair consistently deviant | Scatter: amp vs UVdist |
| Phase decorrelation | Rapidly varying phase for a scan | Scatter: phase vs time |
| Shadowing | Zero amplitude at short baselines | Scatter: amp vs time (automated) |
| Edge channels | Rolloff at band edges | Raster or scatter: amp vs channel |
| Quack (settle time) | Bad data in first N seconds of each scan | Scatter: amp vs time per scan |

### 2.2 Lessons from difmap

Difmap remains popular outside NRAO specifically because of features absent from
CASA tools. The following difmap capabilities inform this design:

- **`vplot` mode** — amplitude/phase vs time per baseline and IF, colour-coded by flag
  state (green = unflagged, yellow = flagged, blue = selfcal-flagged, red = antenna flagged).
  Planned as a named view preset in `VisibilityPlotter`.
- **`radplot` mode** — amplitude/phase vs UV-radius; single-click nearest-point flagging
  (no box required). Planned as an additional flagging tool mode.
- **`corplot`** — accumulated self-cal corrections vs time per antenna; identifies
  periods of poor phase stability. Maps to the future `CalibrationView` panel.
- **`cpplot`** — interactive closure phase display. Planned as a new `Axis.CLOSURE_PHASE`
  value (Phase 4); requires a new backend query path.
- **`projplot`** — amplitude/phase along a projected UV cut; reveals source structure
  anisotropy. Achievable as a named scatter configuration.
- **Model overlay** — observed vs model visibilities on the same plot; immediate visual
  residual. Supported via `VisibilityScatter` multi-layer with the MODEL data column.

### 2.3 The interactive flag loop

The core pattern, repeated many times per session:

1. **Select** — field, SPW, scan, baseline/antenna subset, polarization
2. **Plot** — amplitude and/or phase vs time or frequency
3. **Identify** — hover/locate to determine coordinates of suspect data
4. **Flag** — draw a box or click nearest point; entry added to `FlagDB`
5. **Extend** — propagate flags to all correlations / all channels / all SPWs
6. **Verify** — re-plot with flagged data overlaid in red
7. **Checkpoint** — save flag version before next iteration

---

## 3. Architecture Overview

### 3.1 Layer diagram

```
┌─────────────────────────────────────────────────────────┐
│                   VisibilityPlotter                     │
│  (owns layout, sidebar, toolbar, iteration engine)      │
│                                                         │
│  ┌─────────────────┐   ┌─────────────────────────────┐  │
│  │ VisibilityRaster│   │    VisibilityScatter        │  │
│  │ (Datashader     │   │    (multi-layer Datashader  │  │
│  │  raster panel)  │   │     scatter panel)          │  │
│  └────────┬────────┘   └──────────────┬──────────────┘  │
│           │  VisibilityReader (Protocol)  │             │
└───────────┼──────────────────────────────┼──────────────┘
            │                              │
  ┌─────────▼──────────────────────────────▼──────────┐
  │              VisibilityReader (Protocol)          │
  │  .query_raster()  .query_columns()                │
  │  .probe_raster_pixel()  .probe_scatter_pixel()    │
  └───────────────────┬───────────────────────────────┘
                      │ implements
          ┌───────────┴───────────────────────┐
          │                                   │
  LocalVisibilityReader          RemoteReductionContext
  (wraps XArrayReader,           (future; implements BOTH
   used when data is local)       VisibilityReader AND
          │                       ReductionContext via RPC)
          │ wraps
  ┌───────┴───────────────────┐
  │       XArrayReader (ABC)  │
  │  .open()  .close()        │
  │  .metadata()              │
  └───────────────────────────┘
          │ concrete subclasses
    ┌─────┴──────┐
    │            │
MSv2Backend  MSv4Backend
(xarray-ms)  (Zarr/xradio)

  ┌────────────────────────────────────────────┐
  │          ReductionContext (ABC)            │
  │  commit_flags()  save/restore_flag_version │
  │  bandpass()  gaincal()  applycal() ...     │
  │  submit() → Future                         │
  └────────────┬───────────────────────────────┘
               │ concrete subclasses
    ┌──────────┬──────────────┐
    │          │              │
NullContext  Casa6Context  RadpsContext  RemoteContext(future)

  ┌──────────────────────────────────────────────────────┐
  │                   FlagDB                             │
  │  (append-only JSONL, coordinate-range FlagDeltas)    │
  │  .commit() → ReductionContext.commit_flags()         │
  └──────────────────────────────────────────────────────┘

  ┌──────────────────────────────────────────────────────┐
  │                ObservationMetadata                   │
  │  (frozen dataclass; produced at open time;           │
  │   drives VisibilityPlotter sidebar dropdowns)        │
  └──────────────────────────────────────────────────────┘
```

### 3.2 The VisibilityReader boundary

`VisibilityRaster` and `VisibilityScatter` depend **only** on `VisibilityReader`.
They do not import `XArrayReader`, `MSv2Backend`, `MSv4Backend`, or
`ReductionContext`. This boundary is what makes remote execution transparent:
both `LocalVisibilityReader` and `RemoteReductionContext` satisfy the four-method
`VisibilityReader` protocol, and the widgets never know the difference.

For local sessions:

```python
reader  = LocalVisibilityReader(MSv2Backend("/data/obs.ms"))
context = Casa6ReductionContext("/data/obs.ms")
plotter = VisibilityPlotter(metadata, reader, context, flag_db)
```

For remote cluster sessions (TB-scale MS on a compute node):

```python
remote  = RemoteReductionContext(endpoint="slurm://cluster.nrao.edu/data/obs.ms")
plotter = VisibilityPlotter(metadata, remote, remote, flag_db)
```

The same `VisibilityPlotter` code runs in both cases.

### 3.3 Why the backends stay below LocalVisibilityReader

`MSv2Backend` and `MSv4Backend` are **not** modified to implement
`VisibilityReader`. They continue to implement `XArrayReader` directly, which
gives them:

- Lifecycle methods (`open`, `close`, context manager)
- `metadata()` for `ObservationMetadata` construction
- `available_axes()` for axis selector population

These methods are not part of the widget-facing protocol. `LocalVisibilityReader`
wraps the backend and presents only the four protocol methods to the widgets.
`VisibilityPlotter` calls `metadata()` directly on the `LocalVisibilityReader`
wrapper (which forwards to the backend) during construction.

### 3.4 ObservationMetadata

`ObservationMetadata` is a frozen dataclass produced once at open time by the
`open_ms()` / `open_ps()` factory functions. It holds the field/SPW/antenna/scan
inventory needed to populate the sidebar dropdowns and is passed directly to
`VisibilityPlotter`. Neither `XArrayReader` nor `ReductionContext` holds it.

---

## 4. VisibilityPlotter Capability Set

### 4.1 Display modes

| Mode | Description |
|---|---|
| **Scatter only** | One or more `VisibilityScatter` layers |
| **Raster only** | One `VisibilityRaster` |
| **Linked** | Raster (top) + Scatter (bottom), shared selection and FlagDB |

Linked mode is the primary differentiator. A box drawn in either panel marks
the same rows in `FlagDB`.

### 4.2 Data selection

- MS / Processing Set path (file picker or text entry)
- Field — dropdown from `ObservationMetadata.fields`
- SPW — multi-select; label shows centre frequency and bandwidth
- Scan — range or list (`1~5,8,10~12`)
- Antenna / Baseline — MSSelection string or picker
- Correlation — checkboxes: XX, YY, XY, YX (or RR, LL, RL, LR)
- Time range — ISO or scan-relative
- UV range — metres (baseline selection in image plane)
- Data column — DATA, CORRECTED, MODEL (where available)

### 4.3 Axis controls

**Raster:**
- Y: TIME, CHANNEL, BASELINE, ANTENNA1, UVDIST_LAMBDA
- X: CHANNEL, TIME, UVDIST_LAMBDA
- Quantity (colour): AMPLITUDE, PHASE, REAL, IMAGINARY, WEIGHT, FLAG_FRACTION

**Scatter:**
- X: TIME, CHANNEL, UVDIST, UVDIST_LAMBDA, FREQUENCY, U, V, W, BASELINE, ANTENNA1
- Y (per layer): AMPLITUDE, PHASE, REAL, IMAGINARY, WEIGHT

### 4.4 Averaging

- Channel averaging — N channels binned to one
- Time averaging — N seconds binned to one
- Baseline averaging — average across all baselines
- Scalar vs vector averaging

Flags on averaged data propagate to unaveraged rows via `FlagDelta` — this is
a `ReductionContext.commit_flags()` responsibility, not a display responsibility.

### 4.5 Flagging tools

- **Box select** (default) — draw rectangle in data space; on box close immediately adds
  `FlagDelta` to `FlagDB` and re-renders flagged overlay in red; no button press required
- **Nearest-point flag** — click to flag the point closest to cursor (difmap-style);
  same immediate `FlagDB` accumulation and overlay behaviour as box select
- **Unflag** — same box-select flow with `FlagDelta.flag = False`
- **Flag extend** — per-delta controls: all correlations, all channels, all SPWs, all times in scan
- **Undo** — pop last `FlagDelta` from `FlagDB` and re-render; works freely until disk write
- **Flag ⚑** — write accumulated `FlagDB` entries to disk via `ReductionContext.commit_flags()`;
  the only operation that touches the MS or Processing Set
- **Flag version** — save / restore named disk states via `ReductionContext`
- **Flag summary** — fraction flagged per SPW and per antenna, updated after each disk write

### 4.6 Locate / Hover

- Hover: show probe result (value, coordinates, metadata) in a tooltip
- **Locate** button: for a drawn region, list all matching rows in a sidebar table

### 4.7 Synchronized cursor (cross-panel)

When both Raster and Scatter panels are visible, hovering in one panel
highlights the corresponding visibility sample(s) in the other. Two tiers
of fidelity, increasing in cost:

**Tier 1 — same-axis cursor sync (pure JS, no Python round-trip).**
When both panels share an x-axis dimension (the same case where the
`Range1d` axis link in §3.2 applies), a `CustomJS` callback on each
figure's `MouseMove` event reads the x-coordinate and positions a
vertical `Span` annotation at that x position on both figures. Zero
backend cost, zero latency. Only applies when axes correspond.

**Tier 2 — cross-axis row-level sync (CommMgr round-trip).** When the
panels show unrelated axis pairs (e.g. raster TIME × CHANNEL, scatter
AMP vs UVDIST), translating a hovered raster pixel into a scatter
highlight requires resolving the underlying MS row(s) that pixel
represents — exactly what `probe_raster_pixel` already computes (§3.4,
`VisibilityReader`). Mechanism:

1. JS `MouseMove` listener, throttled to ~10–15 Hz, sends hovered pixel
   coordinates to Python via a new CommMgr j2p message
2. Python calls `probe_raster_pixel` (or `probe_scatter_pixel` for the
   reverse direction) to resolve matching row(s)
3. Python pushes the corresponding coordinates in the *other* panel's
   axis space back via p2j
4. JS updates a small `ColumnDataSource` driving one or more highlight
   marker glyphs already present (but empty) in the target figure

No new backend capability is required — both probe methods already exist
on `VisibilityReader`.  The new pieces are the j2p/p2j message pair, the
JS throttle, and the highlight-marker glyph and its `ColumnDataSource`.

This is a genuinely useful diagnostic: hovering an anomalous raster pixel
and seeing where that sample falls in amplitude-vs-baseline-length space
immediately shows whether an anomaly is isolated to one baseline or
affects the whole array at that time/channel — a different and
complementary view from axis-range linking, which only works when both
panels happen to share an axis.

### 4.8 Iteration (difmap-style)

- Iterate over: antenna, baseline, field, SPW, scan, time
- Prev / Next buttons step through values
- Both panels update synchronously
- Title appends current iteration value

### 4.9 Named view presets (difmap-inspired)

- **vplot mode** — amplitude vs time, colour-by-baseline, flag-state overlay,
  one-per-antenna iteration
- **radplot mode** — amplitude vs UVdist, nearest-point flag tool active
- **projplot mode** — amplitude vs projected UV cut (selectable position angle)

### 4.10 Calibration integration (loose coupling)

`VisibilityPlotter` accepts an optional `ReductionContext`. When
`context.supports_calibration()` returns `True`, the sidebar gains a
**Calibration** accordion section:

- Run bandpass / gaincal / applycal buttons
- Calibration solution view panel (future `CalibrationView`, Phase 4)
- After `applycal`, the data column selector automatically switches to CORRECTED

When `ReductionContext` is `NullReductionContext` (no backend available, e.g.
RADPS without a CASA6 session), calibration buttons are hidden.

### 4.11 Export / scripting

- **Save plot** — PNG export of current view
- **Copy flagdata command** — generate equivalent `flagdata()` call for pending flags
- **Python API** — `VisibilityPlotter(ms=..., field=..., preset=...)` usable in Jupyter

### 4.12 Astronomer-facing constructor

`VisibilityPlotter` is an end-user application, not a composable
programmer component.  Its public constructor accepts only strings,
numbers, and lists — no internal objects (`VisibilityReader`,
`ReductionContext`, `ObservationMetadata`, `SelectionSpec`).

```python
from cubevis.toolbox.visplot import VisibilityPlotter

plotter = VisibilityPlotter(
    # Data source — exactly one of ms or ps (ValueError if both or neither)
    ms   = "sis14_twhya_calibrated_flagged.ms",
    # ps = "sis14_twhya_calibrated_flagged.ps.zarr",

    # Reduction backend — controls which ReductionContext is constructed
    backend         = "auto",   # "auto" | "casa6" | "radps" | "remote" | "null"
    remote_endpoint = None,     # required when backend="remote"

    # Initial selection — all optional strings or numbers
    field       = "0637-752",   # name or int index; default: first field
    spw         = "0,1,2,3",    # MSSelection string; default: all
    antenna     = "",           # MSSelection string; default: all
    scan        = "",           # MSSelection string; default: all
    timerange   = "",           # MSSelection string; default: all
    uvrange     = "",           # e.g. "0~50klambda"; default: all
    correlation = "XX,YY",      # default: all available
    datacolumn  = "data",       # "data", "corrected", "model"

    # Initial display configuration
    mode    = "both",           # "both", "raster", "scatter"
    layout  = "side",           # "side", "over"
    preset  = None,             # "vplot", "radplot", "waterfall"

    # Initial zoom / axis ranges — optional
    time_range   = None,        # (start, end) as ISO strings or MJD floats
    freq_range   = None,        # (start, end) in Hz
    uvdist_range = None,        # (min, max) in metres
)

plotter.show()  # returns a Bokeh layout for notebook embedding
```

### The `backend=` parameter and `ReductionBackend`

`backend` accepts a plain string or a `ReductionBackend` enum value
(defined in `reduction_context.py`; the `str` mixin makes them
identical at every call site):

| Value | Behaviour |
|---|---|
| `"auto"` (default) | Probe in priority order: `casatasks` → RADPS → `NullReductionContext`. For MSv4/PS, `casatasks` is never probed (CASA6 has no MSv4 write path). During the preview, `AUTO` falls through to `NullReductionContext` with a warning log since `Casa6ReductionContext` and `RadpsReductionContext` are not yet implemented — display-only use works cleanly in any environment. |
| `"casa6"` | Require `casatasks`; raise `RuntimeError` if not importable. **Only valid for `ms=`** — raises `ValueError` immediately if `ps=` is supplied. |
| `"radps"` | Require RADPS / AstroVIPER; raise `RuntimeError` if not available. Valid for both `ms=` and `ps=`. |
| `"remote"` | Use `RemoteReductionContext`; requires `remote_endpoint`. Valid for both. Not yet implemented in the preview — raises `NotImplementedError`. |
| `"null"` | Explicitly construct `NullReductionContext` without probing. Useful for display-only sessions or to suppress the `AUTO` warning log. |

### Role of `factory.py`

`open_ms()` and `open_ps()` in `factory.py` are **internal
implementation details** of `VisibilityPlotter.__init__`, not public
API. This resolves the apparent tension in §4.12: the constructor
accepts only primitive types, yet somehow produces a `ReductionContext`.
The factory is where all `ReductionBackend` selection logic lives — it
receives `path` and `backend` (a `ReductionBackend` value), resolves the
correct `ReductionContext` via the context-selection matrix above, and
returns the `(ObservationMetadata, LocalVisibilityReader, ReductionContext)`
triple that `__init__` stores in private attributes.

The factory is not imported directly by astronomer-facing code; it is an
implementation layer that could be replaced (e.g. for testing with a mock
context) without changing the `VisibilityPlotter` constructor signature.

**Composable layer for developers.**  `VisibilityRaster`,
`VisibilityScatter`, `LocalVisibilityReader`, and `ReductionContext`
remain fully accessible as independent programmer-facing components for
embedding in pipelines or custom tools.  `VisibilityPlotter` does not
replace them — it wraps them behind an astronomer-friendly interface.
Developers who want to build their own application shell can do so using
these lower-level classes directly, optionally naming their own class
`VisibilityWidget` or similar to signal its composable nature.

---

## 5. GUI Layout

### 5.1 Overall structure

```
┌──────────────────────────────────────────────────────────────────────┐
│  Toolbar (top)                                                       │
├────────────────────┬─────────────────────────────────────────────────┤
│  Control sidebar   │  Plot area                                      │
│  (~320px, left)    │                                                 │
│                    │  ┌───────────────────────────────────────────┐  │
│  [Data]            │  │  Raster panel (optional, resizable)       │  │
│  [Axes]            │  │                                           │  │
│  [Averaging]       │  └───────────────────────────────────────────┘  │
│  [Display]         │  ┌───────────────────────────────────────────┐  │
│  [Flagging]        │  │  Scatter panel (optional, resizable)      │  │
│  [Calibration]     │  │                                           │  │
│                    │  └───────────────────────────────────────────┘  │
├────────────────────┴─────────────────────────────────────────────────┤
│  Status / Locate results (collapsible)                               │
└──────────────────────────────────────────────────────────────────────┘
```

### 5.2 Toolbar

```
[Plot ▶]  [Reload ↺]  |  [◀ Prev]  Iter: antenna  [ea05 ▼]  [Next ▶]
  |  [□ Box Select]  [· Point Flag]  [⚑ Flag]  [⚐ Unflag]  [⟲ Undo]  [✦ Locate]
  |  [💾 Save plot]  [📋 Copy flagdata]
```

### 5.3 Sidebar accordion sections

**Data**
```
MS path:   [/path/to/data.ms          ] [Browse]
Column:    [DATA ▼]
Field:     [0637-752 (0) ▼]
SPW:       [☑ 0  ☑ 1  ☑ 2  ☐ 3]
Scan:      [________________]
Antenna:   [________________]
UV range:  [______] – [______] m
Time:      [________________]
```

**Axes**
```
Display mode:  [○ Scatter  ○ Raster  ● Both]

── Raster ────────────────────────────────
  Y axis:   [Time      ▼]
  X axis:   [Channel   ▼]
  Quantity: [Amplitude ▼]

── Scatter ───────────────────────────────
  X axis:   [UVdist    ▼]
  Layers:
    [Amplitude  XX  ████  α [━━●━] 1.0]  [−]
    [Phase      XX  ████  α [━━━●] 1.0]  [−]
  [+ Add layer]

── Presets ───────────────────────────────
  [vplot]  [radplot]  [projplot]
```

**Averaging**
```
Channel avg:   [1      ] channels
Time avg:      [0      ] seconds  [☐ scalar]
☐ Avg baselines   ☐ Avg SPWs
```

**Display**
```
Color mode: [● Global  ○ Local]
☑ Show flagged data (red overlay)
☐ Show flag fraction heatmap
Polarization: [☑ XX  ☑ YY  ☐ XY  ☐ YX]
```

**Flagging**
```
Flag extend:
  ☐ All correlations
  ☐ All channels in SPW
  ☐ All SPWs
  ☐ All times in scan

Flag versions:
  [Save current…]  [Restore…]
  Current: "before_plotms"
```

**Calibration** *(hidden when NullReductionContext)*
```
[Run bandpass…]  [Run gaincal…]  [Apply cal…]
Caltables: [cal.B0 ▼]
```

### 5.4 Status / Locate bar

```
┌──────────────────────────────────────────────────────────────────────┐
│ Locate results  [×]                                                  │
│ Scan  Field    Baseline    Time              Chan  SPW  Amp    Phase │
│ 3     0637-752 ea01–ea04   2024-03-01 12:00  32    1    12.4J  47°   │
│ …                                                                    │
│ Flag fraction: SPW0: 12%  SPW1: 0%  SPW2: 3%  SPW3: 18%              │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 6. Implementation Phases and Punch List

### Phase 0 — Architecture foundations ✅ COMPLETE

**Colormap/pseudocolor fidelity (CM-series).** Linear value-to-color
mapping in both display classes was found to saturate badly on
real visibility data — a small high-amplitude population dominates the
colormap while the populous low-amplitude region collapses to a
featureless dark gradient (observed directly during scatter testing:
amplitude vs UVdist rendered as a near-solid purple field below
amplitude ~40, with structure visible only near the top of the range).
This is a Datashader reduction-function problem, not a bit-depth problem —
`uint16` would saturate identically under a linear map.

| ID | Task | Status | Files affected |
|---|---|---|---|
| CM-1 | Switch default Datashader reduction from linear to `eq_hist` (histogram equalization); add `scaling: str = "eq_hist"` constructor parameter to both classes. `eq_hist` implemented as an explicit pre-transform (`colormap_scaling.equalize_histogram()`, mirroring Datashader's own CDF algorithm) rather than via `ds.tf.shade(how="eq_hist")`, because Datashader rejects `span=` for that `how=` value — the explicit version supports `color_mode` (global/local) which the native one cannot | ✅ Done | `visibility_raster.py`, `visibility_scatter.py`, `colormap_scaling.py` |
| CM-2 | `update_scaling(scaling, **kwargs)` fast re-shade path: re-shades the cached aggregation array without a backend re-query, mirroring the existing `set_alpha()` pattern | ✅ Done | `visibility_raster.py`, `visibility_scatter.py` |
| CM-3 | Port `quantize()`-style scaling functions (log, sqrt, square, gamma, power) from interactive_clean, adapted to operate on a generic Datashader aggregation array rather than a 2D image plane | ✅ Done | `colormap_scaling.py` *(new, shared)* |
| CM-4 | `colormap_controls()` method on both classes returning a Bokeh widget column: scaling dropdown, conditional alpha/gamma numeric input, min/max numeric range inputs (min/max wiring deferred — see note below) | ✅ Done | `visibility_raster.py`, `visibility_scatter.py` |
| CM-5 | `histogram()` method on both classes returning binned aggregation values | ✅ Done | `visibility_raster.py`, `visibility_scatter.py` |
| CM-6 | Ensure every re-shade reads `self._scaling` (instance state) rather than a hardcoded default; unified into a single `_shade_agg()` call site in `VisibilityRaster` used by both `_render()` and `_shade_viewport()` | ✅ Done | `visibility_raster.py`, `visibility_scatter.py` |
| CM-7 | Verification against the saturation scenario | ✅ Done — see verification notes below | manual + automated |
| CM-8 | New test classes for `update_scaling()`, `colormap_controls()`, `histogram()` | ✅ Done — `TestColormapScaling` added to both files (16 tests raster, 16 tests scatter); one pre-existing `TestColorMode` test fixed after real-data pytest run surfaced a `color_mode`/`eq_hist` interaction gap — see verification notes below | `test_visibility_raster.py`, `test_visibility_scatter.py` |

**Verification notes (CM-7).** No MS/PS test data was available in the
verification environment, so confirmation was done in two layers:

1. *Isolated transform check* — `colormap_scaling.apply_explicit_scaling()`
   tested standalone against a synthetic right-skewed amplitude
   distribution, a controlled monotonic ramp (to verify each scaling
   curve's direction independent of sample skew), and edge cases
   (all-NaN, degenerate range, negative values, unknown-scaling
   fallback). All passed.
2. *End-to-end through the real classes* — the actual `VisibilityRaster`
   and `VisibilityScatter` classes (not extracted logic) were
   instantiated against a mock `VisibilityReader` returning a
   synthetic agg shaped like the screenshot's saturation problem
   (exponential low-amplitude bulk + sparse high-amplitude stripe).
   Through the real `_render()` → `_shade_agg()` path: **linear
   scaling produced exactly 1 distinct color** (total saturation,
   reproducing the screenshot at its worst) while **`eq_hist` produced
   609 distinct colors** on the same data. This is the direct,
   mechanistic confirmation that the CM-1 default change fixes the
   reported problem.

**Bug found and fixed during verification.** The end-to-end check above
surfaced a genuine defect in the first CM-1/CM-6 implementation: the
explicit-scaling branch (`sqrt`/`square`/`gamma`/`power`) in
`VisibilityRaster._shade_agg()` was deriving its `vmin`/`vmax` clip range
from the `span` parameter — which is the **y-axis coordinate range**
(e.g. TIME in MJD seconds, ~5×10⁹) in this raster's `"global"`
color-mode convention, not the **value range** of the rendered quantity
(e.g. AMPLITUDE, ~0–100). Whenever the y-axis differs from the rendered
quantity — the common case (TIME or BASELINE on y, AMPLITUDE as colour)
— every agg value silently clipped to a single bin, collapsing the image
to one flat color regardless of which explicit scaling was selected.
Fixed by deriving `vmin`/`vmax` from the agg's own finite value range
instead of `span`. `VisibilityScatter`'s equivalent code path was
checked and found *not* to have this bug, because in that class the
y-axis and the rendered quantity are definitionally the same column, so
`span` already carries the correct semantics there. A regression test
(`test_explicit_scaling_value_domain_is_agg_values_not_axis_range`) was
added to `TestColormapScaling` in `test_visibility_raster.py` and
confirmed to fail against the buggy code and pass against the fix.

**Package-layout import bugs found and fixed.** Verification also
required actually importing the generated files into a real package
tree, which surfaced two stale relative-import paths left over from
earlier in this session: `visibility_plot.py`'s `LocalVisibilityReader`
auto-wrap and `local_visibility_reader.py` itself both wrote
`from .reader import XArrayReader`, assuming `reader.py` sits directly
in `visplot/`. The established convention (confirmed against the
`test_visibility_raster.py`/`test_visibility_scatter.py` module loaders
written earlier in this session) places `reader.py` in
`visplot/data/reader.py` alongside `msv2_backend.py` and
`msv4_backend.py`. Both files corrected to `from .data.reader import
XArrayReader`; a stale docstring example in `local_visibility_reader.py`
(`from cubevis.toolbox.visplot.msv2_backend import MSv2Backend`) was
also corrected to the `data.` path.

**Second bug found and fixed — this time by the real pytest suite
against real data.** After the above verification, the maintainer ran
the actual test suite against the sis14 dataset and reported one
failure: `TestColorMode.test_shade_viewport_global_local_differ_in_subrange`,
a pre-CM test asserting that `color_mode="global"` and `color_mode="local"`
must always render visibly different images for a sub-range viewport.
This is the case neither the synthetic mock testing nor the isolated
transform testing exercised, because both used `scaling="eq_hist"` only
incidentally and never specifically checked `color_mode` interaction
across the full scaling matrix.

Root cause: `_shade_agg()`'s `span` parameter is only honoured by the
Datashader-native `how="linear"` path; `"log"` and `"eq_hist"` compute
their colour mapping purely from the values present in the supplied agg,
with no span-like external anchor — so `color_mode` has **no effect at
all** for those two scalings, by construction of how Datashader
implements them. Since `eq_hist` is now the CM-1 default, the
pre-existing test's implicit assumption ("global/local always differ")
silently stopped holding the moment the default scaling changed,
without anyone touching the test or its assumption directly.

A second, narrower defect was found and fixed in the same pass: the
explicit-scaling branch (`sqrt`/`square`/`gamma`/`power`) was *not*
honouring `color_mode` at all — `vmin`/`vmax` were always derived from
the current viewport crop regardless of `"global"`/`"local"`, making
`"global"` a silent no-op for those four scalings too. Fixed by deriving
`vmin`/`vmax` from `self._agg` (the full cached aggregation) in
`"global"` mode, and from the current viewport crop in `"local"` mode —
the explicit-scaling equivalent of the linear case's `span` behaviour.

Resolution:

* `test_shade_viewport_global_local_differ_in_subrange` pinned to
  `scaling="linear"` explicitly — preserving its original, still-valid
  intent (verify the linear-scaling span behaviour), rather than relying
  on whatever the current default happens to be.
* `test_color_mode_has_no_effect_for_log` and (after the follow-up fix
  below) `test_color_mode_changes_eq_hist_output` added, turning the
  per-scaling `color_mode` contract into explicit, seed-verified tests
  rather than an implicit assumption that could silently break again.
* `test_explicit_scaling_color_mode_changes_value_domain` added,
  verifying the global/local distinction at the **transformed-array**
  level rather than the rendered pixel level. This was necessary because
  whether a given `vmin`/`vmax` difference survives 8-bit colour
  quantization into a *visibly* different image turned out to be
  data- and viewport-dependent — a sweep across 30 random seeds found
  cases (e.g. `sqrt` when the crop's max happened to land within ~0.4%
  of the full data's max) where the underlying transform legitimately
  produced a sub-float64-epsilon difference, which is correct behaviour,
  not a bug. The test was tightened to require a relative threshold
  (1% of the data range) before asserting that output must differ,
  rather than flagging any numerical non-identity.

**Follow-up: `eq_hist` global/local restored, not just documented as
absent.** After the fix above shipped, the maintainer raised a fair
question: `color_mode` (global/local) is a useful feature when zoomed
in — locking colours to the full data range avoids them shifting under
the user as they pan, while local mode reveals fine structure in
whatever's currently visible. Since `eq_hist` is the new CM-1 default,
having global/local silently do nothing for it was a real feature loss,
not just a documented limitation. Confirmed via direct test that
Datashader's `tf.shade(..., how="eq_hist", span=...)` actively raises
`ValueError: span is not (yet) valid to use with eq_hist` — there is no
way to opt into a fixed-anchor `eq_hist` through Datashader's public API.

Fixed by reimplementing histogram equalization as an explicit
pre-transform — `colormap_scaling.equalize_histogram()` — rather than
relying on Datashader's native `how="eq_hist"`. The reimplementation
mirrors Datashader's own algorithm exactly (histogram → cumulative
distribution → `np.interp`; confirmed bit-for-bit identical to
`datashader.transfer_functions.eq_hist()` in the no-reference case) but
adds a `reference` parameter: the array whose distribution defines the
equalization curve, separate from the array being mapped. `"global"`
mode passes the full cached aggregation as the reference (curve fixed
regardless of zoom level); `"local"` mode passes `None` (curve built
from the crop itself, matching Datashader's native behaviour).

`eq_hist` moved from `DATASHADER_HOW` to `EXPLICIT_SCALINGS` in
`colormap_scaling.py` as a result — this is purely an internal
classification change; nothing in the public API (`ALL_SCALINGS`,
`scaling_equation_label()`, `update_scaling()`, `colormap_controls()`)
changed shape. `log` was checked and confirmed to already accept
`span=` correctly via Datashader's native path, so it was left as-is;
only `eq_hist` needed the explicit-pre-transform treatment.

`VisibilityScatter`'s equivalent shade dispatch received the same fix.
Its case is simpler than the raster's: because the scatter's y-axis is
definitionally the same column as the rendered quantity, the per-layer
cached DataFrame's `df["y"]` column is directly usable as the `"global"`
reference array — no separate full-agg cache concept was needed, unlike
the raster where `self._agg` already serves that role.

Verified across 15 random seeds for both classes: `eq_hist` global and
local now produce genuinely different output in every case, restoring
the zoomed-in colour-stability/detail-reveal choice for the default
scaling. `test_color_mode_has_no_effect_for_eq_hist_and_log` (the test
added in the prior fix, asserting `eq_hist` and `log` are *identical*
across modes) was replaced with two separate tests:
`test_color_mode_has_no_effect_for_log` (renamed; log's behaviour is
unchanged and still differs correctly between modes, so the name was
always slightly misleading — kept for now, candidate for a clearer
rename later) and `test_color_mode_changes_eq_hist_output` (new,
asserting the restored behaviour).

**Deferred, not part of this phase** — tracked as Phase 4 items instead,
since neither changes the `colormap_controls()` API shape or sidebar
layout that Phase 2 depends on:

- `uint16`/`uint32` image bit depth — internal encoding refinement only
- User-selectable render resolution/quality (Colab network-cost tradeoff) —
  a `VisibilityPlotter`-level deployment concern, not a display-class API concern
- Draggable histogram `Span` overlay (interactive_clean's red-line drag
  interaction) — visual polish on top of the CM-4 widget, same API
- Manual min/max clip range wiring in `colormap_controls()` — the
  `TextInput` widgets exist in the layout but are not yet connected to
  `update_scaling()`'s `vmin`/`vmax`; currently min/max always derive
  from the data's own range

**Architecture foundations (A-series).**

| ID | Task | Status | Files affected |
|---|---|---|---|
| A-1 | Define `VisibilityReader` protocol | ✅ Done | `visibility_reader.py` |
| A-2 | Implement `LocalVisibilityReader` wrapping `XArrayReader` | ✅ Done | `local_visibility_reader.py` |
| A-3 | Update `VisibilityRaster` and `VisibilityScatter` type annotation: `backend: XArrayReader` → `backend: VisibilityReader` | ✅ Done | `visibility_raster.py`, `visibility_scatter.py` |
| A-4 | Define `ObservationMetadata` frozen dataclass and `from_backend_metadata()` factory | ✅ Done | `reduction_context.py` |
| A-5 | Define all DTOs and `ReductionBackend` enum: `FieldInfo`, `SpwInfo`, `AntennaInfo`, `ScanInfo`, `CaltableInfo`, `FlagDelta`, `FlagSummary`, `FlagVersionInfo`, `BandpassParams`, `GaincalParams`, `FluxscaleParams`, `ApplycalParams`, `SplitParams`, `ReductionOperation`, `ReductionResult`; plus `ReductionBackend(str, Enum)` with members `AUTO`, `CASA6`, `RADPS`, `REMOTE`, `NULL` — the `str` mixin allows plain strings to be passed to `VisibilityPlotter`'s `backend=` parameter | ✅ Done | `reduction_context.py` |
| A-6 | Define `ReductionContext` ABC | ✅ Done | `reduction_context.py` |
| A-7 | Implement `NullReductionContext` | ✅ Done | `reduction_context.py` |
| A-8 | Implement `open_ms()` and `open_ps()` factory functions: accept `path`, `backend: ReductionBackend\|str = "auto"`, and `remote_endpoint`; resolve the correct `ReductionContext` via the context-selection matrix (see §4.12); return `(ObservationMetadata, LocalVisibilityReader, ReductionContext)`. These are **internal implementation details** of `VisibilityPlotter.__init__` — not public API. The factory is where `ReductionBackend` selection logic lives; `VisibilityPlotter.__init__` calls them and discards the triple into private attributes. | ✅ Done (subsequent session) | `factory.py` *(new)* |
| A-9 | Verify existing test suite passes with `LocalVisibilityReader` wrapper in place of direct backend references | ✅ Done — `VisibilityPlot.__init__` auto-wraps any bare `XArrayReader`, so existing tests passing `MSv2Backend`/`MSv4Backend` directly require no changes | test suite |

> **Phase 0 is fully closed out.** All A-series and CM-series items are
> complete and verified against real sis14 data (MSv2 and MSv4). The next
> session begins at Phase 1 (Flagging foundations).


---

### Phase 1 — Flagging foundations
*End-to-end flag accumulation and commit with the two existing widget classes.*

| ID | Task | Files affected |
|---|---|---|
| F-1 | `FlagDelta` raster coordinate resolver: map `(time_range, channel_range)` or `(baseline_id, channel_range)` from a raster box to MS row indices | `flag_db.py` |
| F-2 | `FlagDelta` scatter coordinate resolver: map `(x_range, y_range)` in the scatter axis space to MS row indices | `flag_db.py` |
| F-3 | `FlagDB.undo()` — pop last pending `FlagDelta` before commit | `flag_db.py` |
| F-4 | `FlagDelta` extend logic: apply `extend_corr`, `extend_chan`, `extend_spw`, `extend_scan` before building row set | `flag_db.py` |
| F-5 | `Casa6ReductionContext` — flag operations only: `commit_flags()` calling `flagdata()`, `save_flag_version()`, `restore_flag_version()`, `list_flag_versions()` | `casa6_reduction_context.py` *(new)* |
| F-6 | Wire `FlagDB.commit()` to call `ReductionContext.commit_flags()` | `flag_db.py` |
| F-7 | Box-select j2p handler in `VisibilityRaster`: JS box-select tool callback sends data-space `(x0,x1,y0,y1)` → Python adds `FlagDelta` to `FlagDB` and immediately triggers flagged overlay re-render | `visibility_raster.py` |
| F-8 | Box-select j2p handler in `VisibilityScatter` | `visibility_scatter.py` |
| F-9 | "Show flagged" overlay in `VisibilityRaster`: red RGBA layer composited on top of existing image; re-rendered on every `FlagDB` accumulation (box close or undo), not only on disk write | `visibility_raster.py` |
| F-10 | "Show flagged" overlay in `VisibilityScatter`: semi-transparent red layer from flagged data points | `visibility_scatter.py` |
| F-11 | Nearest-point flag tool in `VisibilityScatter` (difmap-style): given screen coordinate, find closest data point, add `FlagDelta` | `visibility_scatter.py` |

---

### Phase 2 — VisibilityPlotter shell
*Combined layout with working selection, axis controls, flag accumulation and overlay, and disk write. No averaging or iteration yet.*

| ID | Task | Files affected |
|---|---|---|
| P-1 | `VisibilityPlotter` class skeleton: astronomer-facing constructor (see §4.12); internally calls `open_ms()`/`open_ps()`, constructs `VisibilityRaster`, `VisibilityScatter`, `FlagDB`, sidebar, toolbar, and preference `ColumnDataSource`; no internal objects exposed as public attributes | `visibility_plotter.py` *(new)* |
| P-2 | Sidebar widget set — accordion layout with Data, Axes, Display, Flagging sections; Bokeh `Select`, `MultiSelect`, `TextInput`, `CheckboxGroup`, `Slider` | `visibility_plotter.py` |
| P-3 | Toolbar — Plot, Reload, Box Select, Point Flag, Flag, Unflag, Undo, Locate, Save Plot, Copy flagdata | `visibility_plotter.py` |
| P-4 | Display mode toggle — Scatter / Raster / Both; dynamically show/hide panels | `visibility_plotter.py` |
| P-5 | Named view presets — vplot, radplot, projplot buttons configure axes and tool | `visibility_plotter.py` |
| P-6 | `SelectionSpec` UV range — add `uv_range` field (metres) for baseline selection | `selection.py` |
| P-7 | `Casa6ReductionContext` metadata methods: `list_fields()`, `list_spws()`, `list_antennas()`, `list_scans()`, `list_data_columns()` | `casa6_reduction_context.py` |
| P-8 | `open_ms()` / `open_ps()` factory with full `ReductionBackend` context-selection matrix (see §4.12) | ✅ Done (subsequent session) | `factory.py` |
| P-10 | Async plot with loading indicator: spinner `Div` overlay on figures while backend query is in flight; Cancel button sets a threading `Event` checked by the backend; Plot button disabled during query | `visibility_plotter.py` |

---

### Phase 3 — Averaging and iteration
*Backend changes required before either feature can be correctly implemented.*

| ID | Task | Files affected |
|---|---|---|
| V-1 | `AveragingSpec` dataclass: `n_chan`, `time_bin_s`, `avg_baselines`, `scalar` | `averaging.py` *(new)* |
| V-2 | Channel and time averaging in `MSv2Backend.query_raster()` | `msv2_backend.py` |
| V-3 | Channel and time averaging in `MSv2Backend.query_columns()` | `msv2_backend.py` |
| V-4 | Channel and time averaging in `MSv4Backend.query_raster()` | `msv4_backend.py` |
| V-5 | Channel and time averaging in `MSv4Backend.query_columns()` | `msv4_backend.py` |
| V-6 | Averaging in `LocalVisibilityReader` pass-through (update signatures) | `local_visibility_reader.py` |
| V-7 | Averaging in `VisibilityReader` protocol (update signatures) | `visibility_reader.py` |
| V-8 | Iteration engine in `VisibilityPlotter`: state machine over (antenna, baseline, field, SPW, scan, time); Prev / Next buttons | `visibility_plotter.py` |
| V-9 | Iteration updates both panels synchronously; title appends current iteration value | `visibility_plotter.py` |

---

### Phase 4 — Polish and completeness

| ID | Task | Files affected |
|---|---|---|
| R-1 | `FLAG_FRACTION` quantity in `VisibilityRaster`: new aggregation mode in `query_raster` | `msv2_backend.py`, `msv4_backend.py`, `visibility_raster.py` |
| R-2 | 1:1 zoom button in `VisibilityRaster`: JS button fires viewport reset using `agg_n_x`/`agg_n_y` from `_state_source` | `visibility_raster.py` |
| R-3 | Colorbar in `VisibilityRaster`: Bokeh `ColorBar` linked to shade span | `visibility_raster.py` |
| R-4 | Axis label units: CHANNEL shows frequency (GHz) secondary label; TIME shows ISO or elapsed | `visibility_raster.py`, `visibility_scatter.py` |
| S-1 | Colour-by-metadata axis in `VisibilityScatter`: categorical palette per SPW / antenna / scan / baseline | `visibility_scatter.py`, `msv2_backend.py`, `msv4_backend.py` |
| S-2 | Per-layer legend widget in `VisibilityScatter`: toggle (alpha=0) per layer from legend | `visibility_scatter.py` |
| S-3 | Auto-alpha review: expose sensitivity slider or improve perceptual model | `visibility_scatter.py` |
| S-4 | Multi-layer probe: `_handle_probe` returns a row per layer for hovered pixel | `visibility_scatter.py` |
| C-1 | `CalibrationView` panel: gain/phase vs time per antenna from a calibration table | `calibration_view.py` *(new)* |
| C-2 | Calibration sidebar section in `VisibilityPlotter`: bandpass / gaincal / applycal buttons; enabled when `context.supports_calibration()` | `visibility_plotter.py` |
| C-3 | `Casa6ReductionContext` calibration methods: `bandpass()`, `gaincal()`, `fluxscale()`, `applycal()` | `casa6_reduction_context.py` |
| C-4 | `RadpsReductionContext` — flag operations + data queries | `radps_reduction_context.py` *(new)* |
| G-1 | Shared x-axis range linking between panels when x-axis dimension matches | `visibility_plotter.py` |
| G-2 | Locate sidebar: `DataTable` below plot area populated by locate handler | `visibility_plotter.py` |
| G-3 | Flag summary bar: fraction flagged per SPW and antenna after each commit | `visibility_plotter.py` |
| G-4 | Copy `flagdata` command: format pending flags as `flagdata()` call string | `visibility_plotter.py` |
| G-5 | PNG export via Bokeh `export_png` | `visibility_plotter.py` |
| G-6 | `Axis.CLOSURE_PHASE` enum value and backend query path (triangle sum of phase over antenna triples) | `axes.py`, `msv2_backend.py`, `msv4_backend.py` |
| G-7 | Synchronized cursor, Tier 1 (same-axis): `CustomJS` `MouseMove` callback drawing a linked `Span` annotation on both figures when x-axis dimensions match | `visibility_plotter.py` |
| G-8 | Synchronized cursor, Tier 2 (cross-axis): throttled JS `MouseMove` → CommMgr j2p message → `probe_raster_pixel`/`probe_scatter_pixel` row resolution → p2j coordinate push → highlight-marker `ColumnDataSource` update in the other panel | `visibility_plotter.py`, `visibility_raster.py`, `visibility_scatter.py` |
| G-9 | `uint16`/`uint32` image bit depth option, deferred from Phase 0 CM-series — internal encoding refinement on top of `eq_hist` scaling | `visibility_raster.py`, `visibility_scatter.py` |
| G-10 | User-selectable render resolution/quality tradeoff (relevant for high-latency deployments e.g. Colab) | `visibility_plotter.py` |
| G-11 | Draggable histogram `Span` overlay for `colormap_controls()` (interactive_clean-style red-line drag), on top of the CM-4 numeric-input widget | `visibility_raster.py`, `visibility_scatter.py` |
| T-1 | `data_group` test cases for `MSv4Backend` | `tests/` |
| T-2 | Synthetic xradio-native DataTree structure tests | `tests/` |
| T-3 | Single-dish test coverage | `tests/` |
| T-4 | `RemoteReductionContext` skeleton and transport protocol | `remote_reduction_context.py` *(new)* |
| X-1 | **CASR-385** SPFLG-style multi-panel raster: tiled `VisibilityRaster` panels, one per baseline, rendered simultaneously in a scrollable grid; required for AIPS SPFLG workflow parity | `visibility_plotter.py`, `visibility_raster.py` |
| X-2 | **CASR-385** `Axis.PHASE_RMS`: phase RMS vs time or frequency as a scatter y-axis quantity; backend computes `std(angle(visibility))` across the baseline axis per time/channel cell; same Phase 4 bucket as `CLOSURE_PHASE` | `axes.py`, `msv2_backend.py`, `msv4_backend.py` |
| X-3 | **CASR-385** User-specified colours in colour-by-metadata mode: colour picker widget per category value (SPW, antenna, baseline); supplements the automatic categorical palette in S-1 | `visibility_plotter.py`, `visibility_scatter.py` |
| X-4 | **CASR-385** Performance benchmarks: explicit test cases at ngVLA scale (200+ antennas, 8000 channels, 1-second integrations, 10 SPWs) and ALMA/VLA scale (50+ antennas, 1000 channels); used to validate Datashader pipeline and `is_decimated` gate | `tests/` |
| X-5 | **CASR-385** Elevate `RemoteReductionContext` from stub to working implementation: serialise `ReductionOperation`, dispatch to remote worker (Dask or HTTP), return real `Future`; required for ngVLA TB-scale datasets where data cannot be local | `remote_reduction_context.py` |

---

## Appendix A — API Stubs

The following files were written as part of this planning session and are
checked into the repository. They define the interfaces but contain no
operational implementation beyond `NullReductionContext`.

### A.1 `visibility_reader.py` — `VisibilityReader` Protocol

```python
@runtime_checkable
class VisibilityReader(Protocol):
    def query_raster(
        self,
        y_dim: Axis,
        x_dim: Axis,
        quantity: Axis,
        selection: SelectionSpec,
        polarization: Optional[str] = None,
        max_cells: int = 2_000_000,
    ) -> tuple[xr.DataArray, tuple[float, float], tuple[float, float], bool]: ...

    def query_columns(
        self,
        xaxis: Axis,
        yaxes: list[tuple[Axis, str]],
        selection: SelectionSpec,
        *,
        canvas_width: int = 800,
        canvas_height: int = 600,
    ) -> dict[tuple[Axis, str], pd.DataFrame]: ...

    def probe_raster_pixel(
        self,
        raw_grid: xr.DataArray,
        gx: int,
        gy: int,
        selection: SelectionSpec,
    ) -> dict: ...

    def probe_scatter_pixel(
        self,
        canvas_agg: xr.DataArray,
        px: int,
        py: int,
        selection: SelectionSpec,
        scatter_df: pd.DataFrame,
    ) -> dict: ...
```

### A.2 `local_visibility_reader.py` — `LocalVisibilityReader`

Pure delegation adapter. Wraps any `XArrayReader` subclass and exposes the
four `VisibilityReader` methods. Also forwards `metadata()` and
`available_axes()` for use by `open_ms()` / `open_ps()` factories.

```python
class LocalVisibilityReader:
    def __init__(self, backend: XArrayReader) -> None: ...
    def open(self) -> None: ...
    def close(self) -> None: ...
    # VisibilityReader protocol:
    def query_raster(self, ...) -> ...: ...
    def query_columns(self, ...) -> ...: ...
    def probe_raster_pixel(self, ...) -> dict: ...
    def probe_scatter_pixel(self, ...) -> dict: ...
    # Pass-through for ObservationMetadata construction:
    def metadata(self) -> dict: ...
    def available_axes(self) -> list[Axis]: ...
```

### A.3 `reduction_context.py` — DTOs and `ReductionContext` ABC

#### Key DTOs

| DTO | Purpose |
|---|---|
| `ObservationMetadata` | Frozen dataclass; populates `VisibilityPlotter` sidebar |
| `FieldInfo` | Single field metadata |
| `SpwInfo` | Single SPW metadata |
| `AntennaInfo` | Single antenna metadata |
| `ScanInfo` | Single scan metadata |
| `CaltableInfo` | Calibration table on disk |
| `FlagDelta` | Pending flag operation from `FlagDB` |
| `FlagSummary` | Result of `commit_flags()` |
| `FlagVersionInfo` | Named saved flag version |
| `BandpassParams` | Parameters for `bandpass()` |
| `GaincalParams` | Parameters for `gaincal()` |
| `FluxscaleParams` | Parameters for `fluxscale()` |
| `ApplycalParams` | Parameters for `applycal()` |
| `SplitParams` | Parameters for `split()` |
| `ReductionOperation` | Serialisable task for remote dispatch |
| `ReductionResult` | Result of a completed remote operation |

#### `ReductionContext` ABC — method groups

| Group | Methods |
|---|---|
| Metadata | `list_fields()`, `list_spws()`, `list_antennas()`, `list_scans()`, `list_data_columns()`, `list_caltables()` |
| Flagging | `commit_flags()`, `save_flag_version()`, `restore_flag_version()`, `list_flag_versions()` |
| Calibration | `bandpass()`, `gaincal()`, `fluxscale()`, `applycal()` |
| Data manipulation | `split()` |
| Remote execution | `submit() → Future[ReductionResult]` |
| Introspection | `supports_calibration()`, `supports_remote_execution()` |

#### `NullReductionContext`

All metadata methods return empty lists. All mutation methods raise
`NotImplementedError`. `supports_calibration()` returns `False`.

### A.4 Future stubs (not yet written)

**`factory.py`** — `open_ms()` and `open_ps()`:

```python
def open_ms(
    path: str,
    context: Optional[ReductionContext] = None,
) -> tuple[ObservationMetadata, LocalVisibilityReader, ReductionContext]:
    """Open an MSv2 or MSv4 Processing Set.

    Returns (metadata, reader, context).  If context is None:
    - Casa6ReductionContext if casatasks is importable
    - NullReductionContext otherwise
    """
```

**`casa6_reduction_context.py`** — `Casa6ReductionContext`:

```python
class Casa6ReductionContext(ReductionContext):
    """Wraps casatasks for local CASA6 sessions.

    commit_flags() calls flagdata().
    bandpass() calls casatasks.bandpass().
    list_fields() etc. delegate to the held XArrayReader.metadata() result.
    """
```

**`radps_reduction_context.py`** — `RadpsReductionContext`:

```python
class RadpsReductionContext(ReductionContext):
    """RADPS / AstroVIPER backend.

    commit_flags() writes to MSv4 Zarr flag arrays.
    Calibration methods call RADPS task equivalents.
    """
```

**`remote_reduction_context.py`** — `RemoteReductionContext`:

```python
class RemoteReductionContext(ReductionContext):
    """Remote cluster backend. Implements BOTH ReductionContext AND
    VisibilityReader so that a single object can be passed for both
    reader and context in VisibilityPlotter when data is remote.

    query_raster() / query_columns() serialise the query, dispatch to
    the remote worker, and return the small agg result over the wire.

    submit() returns a real Future that resolves when the remote job
    completes.
    """
```

---

## Appendix B — File Inventory and Change Summary

### New files

| File | Contents |
|---|---|
| `visibility_reader.py` | `VisibilityReader` protocol *(written)* |
| `local_visibility_reader.py` | `LocalVisibilityReader` adapter *(written)* |
| `reduction_context.py` | All DTOs, `ReductionContext` ABC, `NullReductionContext` *(written)* |
| `factory.py` | `open_ms()`, `open_ps()` factory functions |
| `visibility_plotter.py` | `VisibilityPlotter` combined application class |
| `averaging.py` | `AveragingSpec` dataclass |
| `casa6_reduction_context.py` | `Casa6ReductionContext` |
| `radps_reduction_context.py` | `RadpsReductionContext` |
| `remote_reduction_context.py` | `RemoteReductionContext` |
| `calibration_view.py` | `CalibrationView` panel (Phase 4) |

### Modified files

| File | Change |
|---|---|
| `visibility_raster.py` | A-3: `backend: XArrayReader` → `backend: VisibilityReader`; F-7: box-select j2p handler; F-9: flagged overlay; R-1 – R-4 |
| `visibility_scatter.py` | A-3: same type annotation change; F-8: box-select handler; F-10: flagged overlay; F-11: nearest-point flag; S-1 – S-4 |
| `msv2_backend.py` | V-2, V-3: averaging; R-1: FLAG_FRACTION; S-1: colour-by-metadata; G-6: closure phase |
| `msv4_backend.py` | V-4, V-5: averaging; R-1: FLAG_FRACTION; S-1: colour-by-metadata; G-6: closure phase |
| `reader.py` | No changes required. `XArrayReader` ABC is unchanged. |
| `flag_db.py` | F-1 – F-6: coordinate resolvers, undo, extend, wire to `ReductionContext` |
| `selection.py` | P-6: add `uv_range` field |
| `axes.py` | G-6: `Axis.CLOSURE_PHASE` |

### Unchanged files

| File | Reason |
|---|---|
| `reader.py` | `XArrayReader` ABC and `_compute_axis_values` are correct as-is |
| `visibility_plot.py` | Base class is stable; no changes needed for Phase 0 |
| `axes.py` | Stable until Phase 4 (closure phase) |
| `selection.py` | Stable until Phase 2 (UV range) |
