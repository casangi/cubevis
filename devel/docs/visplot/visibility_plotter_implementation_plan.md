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
9. [Appendix C — Items for further research](#appendix-c--items-for-further-research)

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

### 3.1b Panel ownership model

`VisibilityPlotter` holds panels as a list rather than named attributes, and
layout as a separate descriptor:

```python
self._panels: list[VisibilityPlot]   # N panels (VisibilityRaster or VisibilityScatter)
self._layout: PanelLayout            # how to arrange them
```

```python
@dataclass
class PanelLayout:
    mode:    str   = "split"  # "split" | "grid" | "deck"
    rows:    int   = 1        # for split / grid
    cols:    int   = 2        # for split / grid
    window:  int   = 1        # for deck: panels visible at once
    animate: bool  = False    # deck auto-advance
    shared_selection: bool = True  # False → each panel has its own SelectionSpec
```

The **preview uses `PanelLayout(mode="split", cols=2)`** — one raster and one
scatter side by side, shared selection — which is identical in behaviour to
the current hardcoded design. The refactor (A-10) switches the internal
representation without changing any visible behaviour or public API. The
payoff comes in the subsequent work:

| Layout | `mode` | Use case |
|---|---|---|
| Split (current) | `"split"` | One raster + one scatter, correlated views |
| Grid | `"grid"` | Caltable antenna panels (plotms screenshot); SPFLG tiled raster |
| Deck | `"deck"` | Sequential iteration with K simultaneous panels (`window` > 1 shows K at once) |

**Shared vs independent selection.** The default `shared_selection=True` covers
the current two-panel flagging case — both panels always show the same field/SPW.
`shared_selection=False` is required for the caltable antenna grid (each panel
shows a different antenna's solutions) and for any future multi-SPW tiled view.

### 3.1c Five architecture rules (learned from defects)

Each was re-derived from a defect that **looked like success** during the
export and reference-testing phases. They apply throughout the codebase.

1. **A Python-side model change is invisible without a Bokeh server.** Any
   handler that alters what is drawn must *return* the new data for the client
   to install. Assigning `ColumnDataSource.data` in Python succeeds, changes
   nothing on screen, and logs nothing.
2. **`Axis.label` is never the right source for displayed text.** Only
   `AxisInfo` knows what was actually plotted — e.g. the SI-prefixed string
   and the actual frequency range used.
3. **A value fixed at construction will not follow a later mechanism** unless
   explicitly re-pushed. Anything visual added to the sidebar must be added to
   `_THEME_RESTYLE_JS` in the same change — the two are one unit.
4. **The two backends diverge.** Fixes have landed in one and not the other
   multiple times. Shared logic belongs in `reader.py`; `METADATA_KEYS` and
   parameterised backend tests catch the rest.
5. **Assert on structure, not presentation**, and **assert in the same units
   you compute in**. A check that measures luminance while the code conditions
   on RGB distance silently passes what it was written to catch.

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
- Y: TIME, BASELINE (working in UI); FREQUENCY, CHANNEL, CORRELATION (backend-implemented, not yet in GUI dropdown); ANTENNA1, UVDIST_LAMBDA (planned)
- X: CHANNEL, TIME, FREQUENCY (working in UI); CORRELATION (same backend status); UVDIST_LAMBDA (planned)
- Quantity (colour): AMPLITUDE, PHASE, REAL, IMAGINARY (working); FLAG_FRACTION (planned); WEIGHT (planned)

**Scatter:**
- X: TIME, UVDIST, UVDIST_LAMBDA, FREQUENCY, CHANNEL, U, V (working — CHANNEL and UVDIST_LAMBDA added to GUI this session); W, BASELINE, ANTENNA1 (planned)
- Y (per layer): AMPLITUDE, PHASE, REAL, IMAGINARY, U, V (working — U/V added this session; unmasked by flags: UV-coverage shows sampling coverage); WEIGHT (planned)

### 4.4 Averaging

- Channel averaging — N channels binned to one
- Time averaging — N seconds binned to one
- Baseline averaging — average across all baselines
- Scalar vs vector averaging

Flags on averaged data propagate to unaveraged rows via `FlagDelta` — this is
a `ReductionContext.commit_flags()` responsibility, not a display responsibility.

### 4.5 Flagging tools

- **`FlagTool`** — custom Bokeh drag tool (not plain `BoxSelectTool`). A drag
  draws a rubber-band box in data space and, on release, adds a `FlagDelta` to
  `FlagDB` immediately; no separate button press required. A click (no drag) zooms
  the panel to 1:1 pixel resolution (one screen pixel per underlying data cell),
  which is the minimum resolution at which flagging is permitted. The tool is "not
  unselectable" — re-clicking while already active re-triggers the 1:1 zoom rather
  than deactivating, so flagging mode is never accidentally left behind. Implemented
  in `cubevis.bokeh.tools._flag_tool`; shared via `VisibilityPlot._add_flag_tools()`
  rather than per-subclass.
- **`UnflagTool`** — same `FlagTool` class parameterized by `flag=False`. Draws a
  box over data in the current `FlagDB` and removes matching deltas. This is a
  **spatial region operation**, not a temporal undo stack: it removes flags in the
  drawn region regardless of what order they were added. `Undo` (temporal reversal
  of the last action) is dropped from the design entirely.
- **1:1 resolution gate** — flagging/unflagging only records a `FlagDelta` when
  the view is zoomed to at least one screen pixel per underlying data cell. Below
  that resolution the box still draws (visual feedback, so the interaction doesn't
  feel broken) but is rejected with an explanatory message in the status bar. For
  raster this is exact; for scatter it uses a proxy based on the sparse-data
  canvas-shrink logic (acknowledged approximation — see §2.4 for the scatter
  flagging open question).
- **Nearest-point flag** — click to flag the point closest to cursor (difmap-style);
  same `FlagTool` mechanism, `flag=True`, applied at point granularity
- **Flag extend** — per-delta controls: all correlations, all channels, all SPWs, all times in scan
- **Flag ⚑** — write accumulated `FlagDB` entries to disk via `ReductionContext.commit_flags()`;
  the only operation that touches the MS or Processing Set
- **Flag version** — save / restore named disk states via `ReductionContext`
- **Flag count** — running count of `FlagDB` entries shown in status bar after each
  flag/unflag; "Flag count" preferred over "pending flags" since `FlagDB` rationalises
  entries (unflagging removes deltas rather than adding inverse entries, so "pending"
  is inaccurate)
- **Flag summary** — fraction flagged per SPW and per antenna, updated after each disk write

**Transport note.** Flag/unflag traffic runs on a dedicated `Comm` channel
(opened with `squash_queue=False`) rather than sharing each panel's general-purpose
hover/probe comm (`squash_queue=True`). A queue that silently replaces pending
messages is correct for hover tracking but is a correctness risk for flagging — a
rapid second flag-box could squash an earlier one before Python sees it.

**`enable_flagging` constructor parameter.** Passing `enable_flagging=False` omits
both `FlagTool` and `UnflagTool` from every panel toolbar, for sessions where the
user only wants to inspect data. Threads through to `VisibilityRaster` /
`VisibilityScatter` directly.

### 4.6 Locate / Hover

- Hover: multi-layer probe reports every visible layer; em dash (—) for any layer with no data at that location (hidden layers omitted entirely). Stable field order so status bar does not shift as cursor moves. Prefers exact-bin hit; near-miss search uses screen-pixel budget (`probe_slop_px` = 6 px), bin-aspect-weighted distances, lowest-layer-index tiebreak.
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

- **Save plot / Export PNG** — ✅ Done: GUI button writes the current view (zoom included) server-side; absolute path reported in status bar. No browser download (JupyterLab-over-SSH means Python process may be on a different machine than the browser; base64-over-comm deferred by decision).
- **Headless API** — ✅ Done: `VisibilityPlotter(ms=..., headless=True); vp(plotfile="out.png")`. See E-2.
- **Copy flagdata command** — generate equivalent `flagdata()` call for current `FlagDB` state
- **Python API** — `VisibilityPlotter(ms=..., field=..., preset=...)` usable in Jupyter
- **Reload ↺ vs Plot ▶** — `Plot ▶` re-queries and re-renders, preserving `FlagDB` state; `Reload ↺` re-queries, re-renders, and **clears `FlagDB`** (safe reset) — matches the expectation that "start over" discards in-progress uncommitted flag state

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

    # Explicit axis override (precedence: explicit > preset > hardcoded default)
    # Validated against GUI dropdown lists; ValueError with valid-options list if invalid
    raster_y   = None,          # e.g. "Time", "Baseline"
    raster_x   = None,          # e.g. "Channel", "Frequency"
    raster_qty = None,          # e.g. "Amplitude", "Phase"
    scatter_x  = None,          # e.g. "UVDist", "U"
    scatter_y  = None,          # e.g. "Amplitude", "V"

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
| CM-4 | `colormap_controls()` method: histogram figure with draggable `EditSpan` min/max handles (sibling-aware `interactive_hit` override fixing Bokeh\'s distance-unaware pan dispatch), `js_on_change` + comm-send replacing `.on_change()`, working alpha/gamma inputs, reset button, full dark/light support. ✅ **Done (August 2026).** | `visibility_raster.py`, `visibility_scatter.py` |
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
- Draggable `EditSpan` overlay and min/max wiring — ✅ **Done as part of CM-4** (August 2026)


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
| A-10 | Panel-list model and `_PanelSlot` data structure. ✅ **Done (July 31 2026).** `VisibilityPlotter` holds `self._slots: list[_PanelSlot]` (not fixed named attributes). Each `_PanelSlot` owns both a `VisibilityRaster` and a `VisibilityScatter` instance; `.kind` tracks which is currently active; the inactive kind's first `_render()` is deferred until it is switched to ("defer render, not construction"). Compatibility properties `self._raster`/`self._scatter` resolve through `self._slots[0]`/`self._slots[1]` for pre-slot code. Extending to N panels is "change how many entries are in this list." `panel_layout.py` as a separate file was superseded by this in-class design. `test_visibility_raster.py`/`test_visibility_scatter.py` pass unchanged — pure storage refactor with no observable behavior change. | `visibility_plotter.py` |

> **Phase 0 is fully closed out.** All A-series and CM-series items are
> complete and verified against real sis14 data (MSv2 and MSv4). The next
> session begins at Phase 1 (Flagging foundations).


---

**Hover-probe defect fixes (PB-series).** Fixed August 10 2026; verified against sis14.
Three independent defects produced the same symptom (hover returning empty for plainly-drawn points).
Primary cause was in `VisibilityScatter._handle_probe`. Measured impact: 47.7% of painted bins
reported empty under the old algorithm; 0% under the fix.

| ID | Task | Status | Files affected |
|---|---|---|---|
| PB-1 | Multi-layer hover probe: consult every visible layer; derive `(px,py)` per layer from that layer's own agg; prefer exact-bin hit, tiebreak by lowest layer index | ✅ Done | `visibility_scatter.py` |
| PB-2 | Agg invalidation at top of each shade pass; matching resets in `update_axes` and deferred-render paths | ✅ Done | `visibility_scatter.py` |
| PB-3 | Screen-pixel budget (`probe_slop_px` = 6.0 px) replacing fixed bin-radius; bin-aspect-weighted distances; resolves to 0 radius when bin is already larger than budget | ✅ Done | `visibility_scatter.py` |
| PB-4 | Shared probe geometry helpers (`_cell_bounds`, `_widen_if_degenerate`, `_bin_membership`, `_agg_value`) extracted to `reader.py`; both backends converted | ✅ Done | `reader.py`, `msv2_backend.py`, `msv4_backend.py` |
| PB-5 | Local cell bounds for non-uniform MS axes (previous global average 32× too wide on gapped time/frequency axes, sweeping in rows from neighbouring scans). Verified synthetically; **reference testing against real multi-scan/multi-SPW data pending** | ✅ Done (synthetic); ⬜ reference test pending | `reader.py`, `msv2_backend.py`, `msv4_backend.py` |
| PB-6 | Multi-layer status-bar readout; em dash for layers with no data; `_PROBE_SEP` extracted to base class | ✅ Done | `visibility_scatter.py`, `visibility_plot.py` |
| PB-7 | Probe diagnostics: `probe_debug` / `VISPLOT_PROBE_DEBUG`; per-layer skip reasons; extent logging; duplicate-layer-key warnings | ✅ Done | `visibility_scatter.py`, `visibility_raster.py` |
| PB-8 | `test_probe_fix.py` — 11 tests, standalone, AST-extracted from shipped sources | ✅ Done | `test_probe_fix.py` *(new)* |
| PB-9 | Unify `Canvas.raster()` upsample method between `_render` and `_shade_viewport` via shared `_resample_method()`; add `raster_interpolate` constructor parameter (`"auto"` default: nearest when either axis upsamples, linear when downsampling; `"nearest"` and `"linear"` force the choice). Previous inconsistency: `_shade_viewport` chose nearest-when-upsampling correctly, but `_render` always took Datashader's linear default — so the first image seen was interpolated and every pan/zoom thereafter was not. Linear upsampling is wrong for both raster axes: `baseline_id` is categorical (interpolating between adjacent IDs invents a baseline that does not exist); `time` has inter-scan gaps (interpolating across a gap invents observations). Measured peak dilution at 5× upsample: 4.6% under linear, 0% under nearest. Linear also fabricated 12 distinct levels where the true data had 2. `test_raster_resample.py` (8 tests, standalone, AST-extracted) added; `test_probe_fix.py` unaffected. `raster_interpolate="nearest"` recommended when comparing against reference tools that do not interpolate. | ✅ Done | `visibility_raster.py`, `test_raster_resample.py` *(new)* |
| PB-9 | Unify `Canvas.raster()` upsample method between `_render` and `_shade_viewport` via shared `_resample_method()`; add `raster_interpolate` constructor parameter (`"auto"` default applies nearest when either axis upsamples, linear when downsampling; `"nearest"` and `"linear"` are explicit overrides). Previous inconsistency: `_shade_viewport` already chose nearest-when-upsampling correctly, but `_render` always passed `Canvas.raster()` bare and took Datashader's linear default — so the first image seen by the user was interpolated and every subsequent pan/zoom image was not. Linear upsampling is wrong for both axes: `baseline_id` is categorical (interpolation between adjacent IDs invents a baseline that does not exist), and `time` has inter-scan gaps (interpolation across a gap invents observations). Measured peak dilution at a 5× upsample ratio: 4.6% under linear vs 0% under nearest. Verified: `test_raster_resample.py` (8 tests, standalone, AST-extracted) passes; existing `test_probe_fix.py` unaffected. Note: `raster_interpolate="nearest"` is recommended when comparing against reference tools that do not interpolate. | ✅ Done | `visibility_raster.py`, `test_raster_resample.py` *(new)* |

**Export testing discipline (from E-2 work, carries forward):**
- Compositor tests need no MS, no Bokeh, no display — 76 tests run in seconds; preserve that property
- Cross-runtime parity needs a harness, not discipline: `node` is available; use it
- Assert on structure not presentation; assert on bboxes not pixel counts
- Synthetic fixtures miss things: defects were found only by exporting real data
- Golden tables over ad-hoc assertions for anything two implementations must agree on

**Forward links:** F-11 (nearest-point flag) needs the same `_nearest_populated_bin` primitive — differing only in returning a row identifier; do not reimplement. F-10 (scatter flag overlay) interacts with PB-1: a flagged layer would appear in the multi-layer status bar — whether it *should* is a design question to settle when F-10 is scheduled.

---

### Reference-testing phase (between duo-mode stabilization and Phase 1)

*Not yet started. Deliberately deferred — verifying that the values the UI
produces are correct (not just that the UI behaves sensibly) requires known-good
comparison points.*

The plan is to validate against PlotMS and msview tutorial results for:

- Selection controls (antenna=, scan=, timerange=, uvrange=) — not yet confirmed to constrain plotted data; concrete consequence: Time-vs-Channel raster comparison against msview cannot run fairly (msview requires single-baseline selection; antenna= not wired)
- PB-5 raster hover probe metadata attribution against a real multi-scan/multi-SPW MS — local-cell-bounds fix verified synthetically, not yet validated against known-good plotms values
- Raster rendering fidelity at full extent (PB-9) — visplot now uses nearest-neighbour whenever either axis upsamples. Reference comparisons should note which tool interpolates; `raster_interpolate="nearest"` should be set when comparing against a tool that does not interpolate, since resampling-method differences are a plausible source of visual discrepancy in side-by-side comparisons
- Raster rendering fidelity at full extent — visplot now uses nearest-neighbour whenever either axis upsamples (PB-9). Reference comparisons should note which tool interpolates; `raster_interpolate="nearest"` should be set explicitly when comparing against a tool that does not interpolate, since visual discrepancy from resampling choices is a plausible source of false disagreement in side-by-side comparisons
- Whether selection controls (scan range, antenna, time range, UV range) actually
  constrain the plotted data, and correctly — not yet confirmed either way
- `colormap_controls()` correctness, once the `.on_change()` → `js_on_change` +
  comm-send fix is applied (CM-4 above)
- General cross-validation of axis values, flagging behaviour, and aggregation
  against a trusted reference implementation

See `visplot-testing-handoff.md` for structural (does-the-UI-work) testing
already completed; this phase is the separate, harder question of value
correctness against ground truth.

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
| F-7/F-8 | Flag/unflag j2p handler — shared implementation in `VisibilityPlot` base class (`_add_flag_tools()`), used identically by both `VisibilityRaster` and `VisibilityScatter`. Custom `FlagTool` sends data-space `(x0,x1,y0,y1)` to Python via a dedicated `Comm` channel (`squash_queue=False`); `UnflagTool` (`flag=False`) is the spatial unflag. Flagging gated on 1:1 pixel resolution. *(Preview sketched the gesture UI; this item delivers the `FlagDB` accumulation and overlay wiring.)* | `visibility_plot.py`, `cubevis/bokeh/tools/_flag_tool.py` |
| F-9 | **OPEN DESIGN QUESTION — resolve before scheduling.** "Show flagged" overlay in `VisibilityRaster`. Two candidate mechanisms: (A) re-query the backend with `FlagDB` state applied, shade flagged cells a distinct colour, and composite as a second `image_rgba` layer toggled by `figure.visible` — clean separation, independent show/hide, user-selectable colour, but requires a backend round-trip on every flag/unflag; (B) post-process the existing rendered `uint32` image in Python/NumPy, recolouring pixels that correspond to flagged data-space cells — cheaper but tightly couples flagging display to the rendering pipeline and loses independent show/hide. Complication: raster pixels are Datashader-aggregated bins, not 1:1 with MS rows, so identifying which pixels to recolour from a `FlagDelta` coordinate range is non-trivial. A second overlay image (option A) is architecturally cleaner and maps better to the "show/hide flagged data" UX goal. | `visibility_raster.py` |
| F-10 | **OPEN DESIGN QUESTION — resolve before scheduling.** "Show flagged" overlay in `VisibilityScatter`. Same two-option framing as F-9. Scatter's existing Porter-Duff compositing pipeline already layers multiple `ScatterLayer` images; a third "flagged data" layer using a distinct colour and rendered from `FlagDB` coordinate ranges is a natural fit (option A equivalent). Complication: scatter's `FlagTool` 1:1 resolution criterion is an acknowledged approximation — the overlay must be visually consistent with what was actually flagged. | `visibility_scatter.py` |
| F-11 | Nearest-point flag tool in `VisibilityScatter` (difmap-style): given screen coordinate, find closest data point, add `FlagDelta` | `visibility_scatter.py` |

---

### Phase 1.5 — Functional export slice and benchmarking
*(Moved earlier per stakeholder feedback, July 28 2026. A functional but not fully
polished export/benchmarking slice sits immediately after duo-mode stabilization,
before any iteration/grid-mode work, so benchmark numbers inform grid-mode sizing
decisions rather than guessing.)*

| ID | Task | Files affected |
|---|---|---|
| E-1 | Generator/iteration API: `__call__` is the terminal verb; iteration is repeated calls (`vp(plotfile=f"amp_spw{spw}.png", spw=[spw])` in a loop). No separate generator class needed — the existing flat constructor vocabulary handles per-call re-selection. ✅ **Done (August 2026).** | `visibility_plotter.py` |
| E-2 | Headless PNG export. ✅ **Done (August 2026).** GUI `Export PNG` button writes current view including zoom; headless path via `VisibilityPlotter(ms=..., headless=True)` skips Bokeh Figure/toolbar/tick-formatter construction. Matplotlib chrome over the byte-identical `(H, W) uint32` array already produced by Datashader — not a parallel renderer, a parallel *chrome* over the same pixels. Fidelity in three tiers: Tier 1 RGBA byte-identical at matched canvas size (hash-verified); Tier 2 ranges, tick label strings, titles, labels identical (JS/Python parity harness, 2243 fuzzed cases); Tier 3 fonts/chrome pixel positions best-effort. Theme is a deliberate Tier-3 exception: GUI defaults dark, PNG export defaults light (headed for a paper). SPW `DataTable` rework (§6a) done as part of this work. Key architectural finding: Bokeh contributes no data-area pixels — only chrome (title, axis labels, ticks, toolbar, hover). Export is not "keep two renderers in sync" but "keep two chrome-drawers in sync over an identical array." | `visibility_plotter.py`, `visibility_raster.py`, `visibility_scatter.py`, `png_export.py` *(new)*, `panel_spec.py` *(new)*, `tick_format.py` *(new)*, `palettes.py` *(new)*, `refresh.py` *(new)* |
| E-2a | **Known gap from export work:** constructor API only exposes `layout="one"|"side"|"over"` with slot A hardcoded raster and slot B scatter. Two rasters side by side is reachable from the GUI (P-5b) but unreachable from the API. Fixing this — adding per-slot kind control to the constructor, likely via a `panels=` escape hatch (list of dicts of primitives) — is independent of export but was identified during it and is the natural entry point to a more expressive constructor API. |
| E-3 | GUI colorbar (`ColorBar`). **Partially done.** `plot_left`/`plot_right` placement works in both GUI and PNG. Display-scope colorbar (a separate narrow figure, mapper/theme/range kept in sync on every `update_scaling()` and viewport change) done in PNG; GUI version deferred until someone requests it — better than shipping a GUI option that silently means something different from the PNG option. Under `eq_hist` in local mode the mapper must be rebuilt on every `update_scaling()` and viewport change. GUI colorbars should default **off** (gear panel histogram already conveys value distribution); PNG colorbars default **on** (no gear panel). | `colormap_scaling.py` (`ScalarMapping` added), GUI colorbar checkbox |
| E-4 | Real query→render→export timing benchmarks vs. PlotMS — data-backed decisions for grid-mode sizing (default/cap grid dimensions). Still open. | benchmarking scripts |
| E-5 | `query_columns` result cache keyed on `(x_axis, y_axes, selection)` — accepted alternative to affine remap; helps every axis toggle. Still open. | `visibility_scatter.py` |
| E-6 | SPW name→ID resolution through SPECTRAL_WINDOW subtable — blocks CASA-form `spw=N` output in export filenames. Still open. The sis14 test dataset has one SPW identified by name only, so both SPW *selection* (selecting the one window changes nothing) and CASA-form `spw=N` output are untestable with sis14; a multi-window MS is needed. | `msv2_backend.py`, `msv4_backend.py` |

---

### Phase 2 — VisibilityPlotter shell
*Combined layout with working selection, axis controls, flag accumulation and overlay, and disk write. No averaging or iteration yet.*

| ID | Task | Files affected |
|---|---|---|
| P-1 | `VisibilityPlotter` class skeleton: astronomer-facing constructor (see §4.12); internally calls `open_ms()`/`open_ps()`, constructs `VisibilityRaster`, `VisibilityScatter`, `FlagDB`, sidebar, toolbar, and preference `ColumnDataSource`; no internal objects exposed as public attributes | `visibility_plotter.py` *(new)* |
| P-2 | Sidebar widget set — accordion layout with Data, Axes, Display, Flagging sections; Bokeh `Select`, `MultiSelect`, `TextInput`, `CheckboxGroup`, `Slider` | `visibility_plotter.py` |
| P-3 | Toolbar — Plot, Reload, `FlagTool`, `UnflagTool`, Flag ⚑ (write to disk), Locate, Save Plot, Copy flagdata. `enable_flagging=False` omits flag tools entirely. | `visibility_plotter.py` |
| P-4 | Display mode toggle — Scatter / Raster / Both; dynamically show/hide panels | `visibility_plotter.py` |
| P-4a | **Auto-hide per-plot toolbars** — `compact_toolbar: bool = True` on `VisibilityPlot` base class; implemented via `figure.toolbar.autohide = True` (no custom JS). ✅ **Done (July 29 2026), default=True for all panels including duo mode.** Known accepted behavior: the toolbar's reserved layout space does not collapse when hidden (only drawn content toggles), consistent with upstream Bokeh design (bokeh/bokeh#8284) — collapsing the space would reflow the layout on every hover-boundary crossing. Accepted as-is; revisit only if this becomes an actual user complaint. | `visibility_plot.py`, `visibility_raster.py`, `visibility_scatter.py`, `visibility_plotter.py` |
| P-5 | Named view presets (vplot, radplot, Waterfall) — configure axes and layout per slot. ✅ **Done (August 2026).** Presets integrated with per-slot gear/Tabs config; preset application triggers per-slot `_handle_plot()`. | `visibility_plotter.py` |
| P-5a | **Per-slot gear tool + tabbed sidebar config.** ✅ **Done (July–August 2026).** A `CustomAction` tool in each panel's toolbar reveals a Bokeh `Tabs` widget, expanding the sidebar if collapsed. One gear per (slot, kind) pair — 2×2 = 4 gear instances for duo mode — because the kind that becomes active via a kind-switch would otherwise have no gear. Each slot pre-builds both a raster panel and a scatter panel; the tab's Kind selector picks which is shown. Independent per-slot editing state preserved (editing two panels' configs independently before committing either). | `visibility_plotter.py` |
| P-5b | **Per-slot raster ↔ scatter kind switching, same-kind-on-both-slots.** ✅ **Done (August 2026).** Any slot can independently be raster or scatter; two rasters or two scatters simultaneously is fully supported. All four layout objects (both kinds, both slots) always children of the display container, `.visible` toggled to show only the active kind per slot. A kind switch is implemented as a visibility toggle (no rebuild); scatter is recompute-gated (does not run `_render()` while invisible) to avoid wasted backend queries. | `visibility_plotter.py` |
| P-5c | **Zero-recompute panel-position swap.** ✅ **Done (August 3 2026).** One swap button per tab; `self._display_order_source` is the underlying reorderable-list tracker. Swap is a visibility toggle across all four layout objects simultaneously — no recompute. One/Side-by-Side/Over-Under all three layout modes confirmed working. | `visibility_plotter.py` |
| P-5d | **Validation-error auto-focus.** ✅ **Done (August 3 2026).** On a validation error (e.g. raster Y/X axis conflict), the sidebar automatically focuses the offending slot's tab. Client-side raster-Y/X-conflict guard fires before the server is ever reached. | `visibility_plotter.py` |
| P-6 | `SelectionSpec` UV range — add `uv_range` field (metres) for baseline selection | `selection.py` |
| P-7 | `Casa6ReductionContext` metadata methods: `list_fields()`, `list_spws()`, `list_antennas()`, `list_scans()`, `list_data_columns()` | `casa6_reduction_context.py` |
| P-8 | `open_ms()` / `open_ps()` factory with full `ReductionBackend` context-selection matrix (see §4.12) | ✅ Done (subsequent session) | `factory.py` |
| P-10 | Async plot with loading indicator: spinner `Div` overlay on figures while backend query is in flight; Cancel button sets a threading `Event` checked by the backend; Plot button disabled during query | `visibility_plotter.py` |

---

### Phase 2.5 — Iteration (duo mode)
*Per-axis Prev/Next beside each axis's own sidebar control, reusing duo
mode's existing selection/render plumbing rather than introducing a new
iteration engine. Scoped by `visplot_duo_iteration_kickoff.md`; see
Appendix C.8 for the msview precedent this design started from and
departed from (below).*

**Departure from the msview precedent (August 20 2026):** Appendix C.8's
"assign exactly one animator" design is msview's own documented model, and
was I-1's starting point (kickoff §2: "That's the documented precedent for
building this as a single selector, not independent Prev/Next per axis").
Live-MS testing revisited that: msview's constraint stems from its own
architecture — continuous slider-driven autoplay, one animation loop at a
time — which duo mode's discrete, click-to-step request/response mechanism
does not share. Each axis's `SelectionSpec` field (`field_names`, `spw`, …)
is already independent, and stepping one only ever touches its own message
key — nothing about the mechanism *requires* a single global "current
animator." Decentralizing removes a constraint that turned out to be
inherited from a different tool's different constraints rather than
re-derived for this one.

| ID | Task | Files affected |
|---|---|---|
| I-1 | Prev/Next buttons beside each iterable axis's own sidebar control — `self._field_prev_btn`/`_field_next_btn` next to the Field `Select`, `self._spw_prev_btn`/`_spw_next_btn` next to the SPW `DataTable`'s heading. Each pair mutates its own widget (`field_sel.value` or `spw_src.selected.indices`) and calls the existing `doPlot()` — no new message shape, no server-side "hold axis fixed" flag; the *other* axis is simply resent unchanged, which is what makes "which axis is iterating" not a mode that needs tracking or displaying anywhere. Wraps at the ends (chosen over clamping, per the kickoff's explicit request to pick one; `iteration_step.py`'s `wrap` parameter also supports clamp for a future mode that wants it, e.g. grid-mode paging). Index arithmetic: JS `stepIterationIndex`, parity-tested under `node` against Python `step_index` in new `iteration_step.py` (the JS copy is what actually runs — see that module's docstring for why a tested Python copy exists regardless). Status bar shows position, e.g. "Field 3/7: 0637-752" / "SPW 2/4: 1", via `_field_iteration_position()` / `_spw_iteration_position()` — computed from current selection state, not tracked iteration-mode state, so it's correct regardless of whether Prev/Next or a manual pick produced it. Each pair is **disabled at construction** (not just guarded at click time) when its axis has ≤1 item — a real single-SPW MS, found in live testing, made "visibly disabled" clearly the better choice over "silently does nothing." ✅ **Done (August 20 2026)**, superseding two earlier same-day iterations documented in full in this session's chat history: (1) a toolbar `RadioButtonGroup` "Animate: Field \| SPW" selector, (2) a toolbar `Select` version of the same. Both were replaced, not layered on — live-MS testing showed the *spatial* separation between a centralized toolbar control and the sidebar widgets it actually drives was itself the problem (toolbar width growing with every axis; "which axis is active" a mode with no visible indicator of its own). Per-axis Prev/Next removes the toolbar's iteration footprint to zero, permanently — I-2/I-3 each just need their own pair beside their own sidebar control, no toolbar change at all. **Consistency across panels**, raised during this design discussion: guaranteed by construction, not by convention — `_handle_plot()` builds exactly one `SelectionSpec` per request (`self._selection = self._build_selection()`) and assigns the *same object* to every panel (`self._all_panels`) before either re-renders, so Field/SPW are not per-panel state and a Prev/Next press cannot move one panel without the other, raster or scatter, regardless of which kind is active in which slot. **Bug found and fixed in the same session, not an I-1 defect but exposed by it:** `ufunc 'minimum' did not contain a loop with signature matching types (dtype('<U…')…)` on a Field change. Root cause: `msv2_backend.py` / `msv4_backend.py`'s `_raster_2d()` copies every coordinate off the source partition (`coords={k: v for k, v in vis_pol.coords.items()}`), including auxiliary display-label coordinates (`baseline_antenna1_name`/`baseline_antenna2_name`, string dtype) that a `.mean(dim=...)` reduction does not drop because they don't sit along the reduced dimensions. Those then survive into `query_raster()`'s `xr.concat(..., join="outer")` across partitions, which for different fields commonly disagree on which baselines are present — forcing a string coordinate through a categorical-dimension reconciliation that isn't expecting one. Fixed in both backends with a new `_drop_non_raster_coords()` helper (separate copies, per §3.1c Rule 4's convention of allowed divergence, landed in lockstep since it's the same defect in both) called at both of `_raster_2d`'s return points. The probe/hover path is unaffected — it re-reads these names from the source partition directly, never from `_raster_2d`'s output. Could not reproduce the exact failure live against xarray 2026.7.0 / datashader 0.19.1 in several attempts (see chat); the fix is justified independently (removing unused string metadata from a numeric-only pipeline is correct regardless), tested directly (`test_raster_coord_stripping.py`), but not confirmed end-to-end against the reporting MS. **Update (August 21 2026, UI polish from live screenshots):** four rounds of feedback against a real rendered sidebar fixed, in order: oversized buttons and excess whitespace (`Tip` is its own `LayoutDOM` with independent default sizing — unset width/height/margin on the Tip wrapper itself, not the button's own CSS, was the actual cause); light/dark theming (a themed sidebar `Select`/`Button` needs its color stylesheet swapped on toggle the way a toolbar control never did, since the toolbar area itself is never restyled — missed because I-1's own buttons started in the toolbar before moving to the sidebar); horizontal misalignment between Field's and SPW's button pairs (194px vs 200px, independently chosen, silently different); and vertical crowding of SPW against Field (a wrapper column's margin zeroed to fix the row's *internal* horizontal alignment also zeroed the gap to the *next* sidebar section, restored with an explicit bottom-only margin). All four were extracted into a new `_IterButtons` dataclass (module level, beside `_PanelSlot`) plus a small `_iter_guard_js()` helper for the one JS piece worth sharing (the "nothing to iterate" guard text) — Field and SPW were rewritten to use both, and both are now the intended starting point for I-2/I-3 rather than something to re-derive by hand a third time. See `_IterButtons`'s own docstring for the full per-round detail. | `visibility_plotter.py`, `msv2_backend.py`, `msv4_backend.py`, `iteration_step.py` *(new)*, `test_iteration_step.py` *(new)*, `test_visibility_plotter_iteration.py` *(new)*, `test_raster_coord_stripping.py` *(new)* |
| I-2 | Polarization/Correlation iteration. Blocked: Polarization is currently a `CheckboxGroup` for multi-select display; needs to be exposable as single-value-at-a-time — a real widget change, not just positioning, so `_IterButtons` doesn't remove this blocker, only what comes after it. Once rebuilt as a single-line widget, construction is `_IterButtons(axis_label="polarization", control=<the new widget>, count=len(all_corrs), dark=dark, icon_btn_css=self._icon_btn_css, tt=self._tt)` plus a `doIteratePolarization()` JS body using `_iter_guard_js("...", "polarization")` — same shape as Field's or SPW's block, whichever the new widget resembles. Not started — explicitly out of scope for the I-1 session. | `visibility_plotter.py` |
| I-3 | Antenna/Baseline/Scan/Time iteration. Blocked on `antenna=`/`scan=`/`timerange=` selection wiring (Appendix C.8, reference-testing phase). Each is currently an `EvTextInput` with `title=` — the exact shape Field's `Select` had before its title was moved out to an external `_section()` heading to fix a row-alignment defect (see I-1's August 21 update) — so that same restructuring (drop `title=`, add `_section(name)` above, `_IterButtons(axis_label=name, control=<the retitled input>, ...)` in a row below, `vertical_nudge` value TBD and worth actually checking against a real browser rather than assumed to match Field's 2px) is the concrete starting point once each is wired, not a pattern to re-derive from scratch. Not started — explicitly out of scope for the I-1 session. | `visibility_plotter.py` |

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
| C-1 | `CalibrationView` panel: gain/phase vs time per antenna from a calibration table. In the tiled multi-antenna grid (as shown in the plotms caltable screenshot, e.g. 3×3 antenna panels), each cell is a `VisibilityScatter` with `autohide_toolbar=True` (P-4a) and `PanelLayout(mode="grid")` (A-10). Flagging bad solution intervals directly in the calibration view shares the same `FlagDB` as the visibility view. | `calibration_view.py` *(new)* |
| C-2 | Calibration sidebar section in `VisibilityPlotter`: bandpass / gaincal / applycal buttons; enabled when `context.supports_calibration()` | `visibility_plotter.py` |
| C-3 | `Casa6ReductionContext` calibration methods: `bandpass()`, `gaincal()`, `fluxscale()`, `applycal()` | `casa6_reduction_context.py` |
| C-4 | `RadpsReductionContext` — flag operations + data queries | `radps_reduction_context.py` *(new)* |
| G-1 | Shared x-axis range linking between panels when x-axis dimension matches | `visibility_plotter.py` |
| G-2 | Locate sidebar: `DataTable` below plot area populated by locate handler | `visibility_plotter.py` |
| G-3 | Flag summary bar: fraction flagged per SPW and antenna after each commit | `visibility_plotter.py` |
| G-4 | Copy `flagdata` command: format pending flags as `flagdata()` call string | `visibility_plotter.py` |
| G-5 | PNG export via Bokeh `export_png` | `visibility_plotter.py` |
| G-6 | `Axis.CLOSURE_PHASE` enum value and backend query path (triangle sum of phase over antenna triples) | `axes.py`, `msv2_backend.py`, `msv4_backend.py` |
| G-7 | Synchronized cursor, Tier 1 (same-axis Span crosshair): cursor-span sync observed working live in all panel-kind combinations in duo mode (raster+scatter, raster+raster, scatter+scatter). ✅ **Substantially done** — implemented earlier, confirmed live August 2026; documents were stale. Generalized to N panels. | `visibility_plotter.py` |
| G-8 | Synchronized cursor, Tier 2 (cross-axis row-level highlight): distinct from G-7's Span sync; requires j2p round-trip to resolve which MS row a hovered pixel corresponds to, then highlight the matching point in the other panel's axis space. **Not yet built.** PB-series probe machinery and F-11's `_nearest_populated_bin` are the prerequisites. | `visibility_plotter.py`, `visibility_raster.py`, `visibility_scatter.py` |
| G-9 | `uint16`/`uint32` image bit depth option, deferred from Phase 0 CM-series — internal encoding refinement on top of `eq_hist` scaling | `visibility_raster.py`, `visibility_scatter.py` |
| G-10 | User-selectable render resolution/quality tradeoff (relevant for high-latency deployments e.g. Colab) | `visibility_plotter.py` |
| G-11a | Colormap widget built: histogram figure, draggable `EditSpan` min/max handles, scaling dropdown, reset button, dark/light support including histogram. ✅ **Done (CM-4, August 2026).** | `visibility_raster.py`, `visibility_scatter.py` |
| G-11b | Extract colormap widget to shared `cubevis/toolbox/colormap_widget.py`; wire `interactive_clean` to import from there. **Still open.** | `colormap_widget.py` *(new)*; `iclean` |
| T-1 | `data_group` test cases for `MSv4Backend` | `tests/` |
| T-2 | Synthetic xradio-native DataTree structure tests | `tests/` |
| T-3 | Single-dish test coverage | `tests/` |
| T-4 | `RemoteReductionContext` skeleton and transport protocol | `remote_reduction_context.py` *(new)* |
| X-1 | **CASR-385** SPFLG-style **grid mode**: paginated N×M grid of panels sharing the same axes, iterating through a selection value (antenna, SPW, field, scan) per cell. Each cell is a real interactive `VisibilityRaster`/`VisibilityScatter` instance (own `ColumnDataSource`s, comm registrations, flag tools) — not a static image. Object pool sized to the page, not the full iteration count; page turns re-select via existing `update_axes()` path, never rebuild. Depends on A-10 (`_PanelSlot` list, ✅ done) and P-4a (autohide, ✅ done). Grid/iteration layout is a sibling value on the same `layout_rbg` control as duo mode — switching between duo and iteration mid-session is supported by construction. Default/cap grid dimensions (proposed 3×3/6×6) should be set from E-4 benchmark numbers, not guessed. Key design decisions already settled: real interactive panels per cell; bounded grid size; paginate not scroll; uniform axes/mode per grid by default (data model supports heterogeneous, UI does not expose it in phase 1); object count sized to page; cross-cell pan/zoom sync gated by toggle; cross-cell crosshair position sync (reuses Bokeh native linked-crosshair, cheaper than the raster↔scatter crosshair link in duo mode). **Open questions:** exact default/max dimensions; compound "Iterate by" (single axis vs. antenna+SPW simultaneously); cross-cell flagging propagation; pan/zoom per-axis granularity; concurrent-backend-query burst mitigation; N-panel swap-trigger UI (coordinate dropdown vs. spatial button grid). Full detail in `visplot-development-handoff.md`. | `visibility_plotter.py`, `visibility_raster.py`, `visibility_scatter.py` |
| X-2 | **CASR-385** `Axis.PHASE_RMS`: phase RMS vs time or frequency as a scatter y-axis quantity; backend computes `std(angle(visibility))` across the baseline axis per time/channel cell; same Phase 4 bucket as `CLOSURE_PHASE` | `axes.py`, `msv2_backend.py`, `msv4_backend.py` |
| X-3 | **CASR-385** User-specified colours in colour-by-metadata mode: colour picker widget per category value (SPW, antenna, baseline); supplements the automatic categorical palette in S-1 | `visibility_plotter.py`, `visibility_scatter.py` |
| X-4 | **CASR-385** Performance benchmarks: explicit test cases at ngVLA scale (200+ antennas, 8000 channels, 1-second integrations, 10 SPWs) and ALMA/VLA scale (50+ antennas, 1000 channels); used to validate Datashader pipeline and `is_decimated` gate | `tests/` |
| X-5 | **CASR-385** Elevate `RemoteReductionContext` from stub to working implementation: serialise `ReductionOperation`, dispatch to remote worker (Dask or HTTP), return real `Future`; required for ngVLA TB-scale datasets where data cannot be local | `remote_reduction_context.py` |
| Y-1 | **CASR-385** Autoflag "calculate+display" mode: run an autoflag algorithm (e.g. `flagdata(mode='tfcrop', action='calculate')`), receive proposed flags, display them as a distinct overlay colour (e.g. orange, distinct from the committed-flag red), allow the astronomer to accept or reject before any disk write. Requires a new `ReductionContext.calculate_flags()` method returning proposed `FlagDelta` entries that flow into a separate "proposed" layer in `FlagDB` without entering the pending-commit queue. | `reduction_context.py`, `flag_db.py`, `visibility_raster.py`, `visibility_scatter.py`, `visibility_plotter.py` |
| Y-2 | **CASR-385** `Axis.RESIDUAL`: DATA − MODEL and CORRECTED − MODEL as displayable quantities in both raster and scatter, computed by the backend from the existing data column infrastructure | `axes.py`, `msv2_backend.py`, `msv4_backend.py` |
| Y-3 | **CASR-385** Frequency frame conversion (topo → LSRK/BARY/etc.) and velocity axis: display-time approximation analogous to plotms `transform` / `freqframe` / `restfreq` / `veldef` parameters; uses casacore `VelocityMachine` for MSv2 and equivalent MSv4 machinery. New `TransformSpec` dataclass to carry `freqframe`, `restfreq`, `veldef` alongside `SelectionSpec`. Note: on-the-fly frame conversion at display time is not as accurate as `cvel`/`mstransform` regridding; this limitation should be documented in the UI. | `axes.py` (`Axis.VELOCITY`), `msv2_backend.py`, `msv4_backend.py`, new `transform_spec.py` |
| Y-4 | **CASR-385** Histogram view panel: standalone amplitude or flag-fraction distribution display as an optional third panel in `VisibilityPlotter`, sharing the `histogram()` data already computed by `VisibilityRaster`/`VisibilityScatter`; candidate view type raised in requirements but no confirmed use case yet — see Appendix C | `visibility_plotter.py` |

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
| `iteration_step.py` | I-1: pure Prev/Next step-index arithmetic — `step_index()` in Python, JS-parity `STEP_INDEX_JS` embedded by `visibility_plotter.py`'s Prev/Next `CustomJS` |
| `test_iteration_step.py` | I-1: `iteration_step.py` unit tests + JS/node parity harness (mirrors `test_tick_format.py`'s pattern) |
| `test_visibility_plotter_iteration.py` | I-1: AST-lifted tests for `_field_iteration_position()` / `_spw_iteration_position()` (mirrors `test_spw_selection.py`'s pattern) |
| `test_raster_coord_stripping.py` | I-1 follow-on: regression tests for `_drop_non_raster_coords()` in both backends (the `ufunc 'minimum'` fix) — AST-lifted, parameterised over `msv2_backend.py`/`msv4_backend.py` per §3.1c Rule 4 |

### Modified files

| File | Change |
|---|---|
| `visibility_raster.py` | A-3: `backend: XArrayReader` → `backend: VisibilityReader`; F-9: flagged overlay (open design question); R-1 – R-4 |
| `visibility_scatter.py` | A-3: same type annotation change; F-10: flagged overlay (open design question); F-11: nearest-point flag; S-1 – S-4 |
| `visibility_plot.py` | F-7/F-8: `_add_flag_tools()` shared `FlagTool`/`UnflagTool` mechanism (delivered in preview) |
| `cubevis/bokeh/tools/_flag_tool.py` *(new)* | Custom `FlagTool` Bokeh tool class |
| `msv2_backend.py` | V-2, V-3: averaging; R-1: FLAG_FRACTION; S-1: colour-by-metadata; G-6: closure phase; I-1: `_drop_non_raster_coords()` fix for the `ufunc 'minimum'` defect |
| `msv4_backend.py` | V-4, V-5: averaging; R-1: FLAG_FRACTION; S-1: colour-by-metadata; G-6: closure phase; I-1: `_drop_non_raster_coords()` fix for the `ufunc 'minimum'` defect |
| `reader.py` | No changes required. `XArrayReader` ABC is unchanged. |
| `flag_db.py` | F-1 – F-6: coordinate resolvers, undo, extend, wire to `ReductionContext` |
| `selection.py` | P-6: add `uv_range` field |
| `axes.py` | G-6: `Axis.CLOSURE_PHASE` |
| `visibility_plotter.py` | I-1: "Animate: Field \| SPW" toolbar controls; `_field_iteration_position()` / `_spw_iteration_position()`; `_status_text()` iteration-position display. This appendix predates `visibility_plotter.py`'s own creation (hence its appearance under "New files" above rather than here) and has otherwise not been kept in sync with the file's actual history — see the note this session's kickoff response left about that. |

### Unchanged files

| File | Reason |
|---|---|
| `reader.py` | `XArrayReader` ABC and `_compute_axis_values` are correct as-is |
| `visibility_plot.py` | Base class is stable; no changes needed for Phase 0 |
| `axes.py` | Stable until Phase 4 (closure phase) |
| `selection.py` | Stable until Phase 2 (UV range) |

---

## Appendix C — Items for Further Research

This appendix captures requirements and ideas that are **out of scope for the
current implementation** but are worth tracking so they are not lost. Each
item notes the source and what would be needed to make it actionable.

### C.1 Histogram view panel

**Source:** CASR-385 requirements document ("Histograms? Any use-case?")

**Description:** A standalone amplitude or flag-fraction distribution panel,
displayed alongside or instead of the raster/scatter panels. Amplitude
histograms could help identify RFI thresholds before flagging; flag-fraction
histograms could show how flagging is distributed across SPWs or antennas.

**Current status:** The `histogram()` method on `VisibilityRaster` and
`VisibilityScatter` (Phase 0, CM-5) already computes the binned aggregation
data. The colormap widget (G-11) will display this as a small histogram
alongside the scaling controls. A *standalone* histogram panel is a separate,
larger UI element. No confirmed use case from astronomers yet.

**What would make it actionable:** A concrete astronomer workflow that the
raster/scatter view does not serve well, where a standalone distribution view
is the natural tool. The `histogram()` data is already available; the question
is whether the panel is worth the layout real estate.

---

### C.2 3D visibility views

**Source:** CASR-385 requirements document ("3D views? Any use-case?")

**Description:** Three-dimensional visualisation of visibility data — e.g. UV
coverage plotted in 3D, or amplitude as a function of two spatial/frequency
axes simultaneously.

**Current status:** Not planned and no confirmed use case has been identified.
The current architecture (Datashader → 2D RGBA image → Bokeh `image_rgba`)
is inherently 2D. A 3D view would require a different rendering stack (e.g.
three.js, plotly, or vispy).

**What would make it actionable:** A specific science question that cannot be
answered by any combination of the existing 2D raster and scatter views; a
volunteer to design and implement the rendering layer; and a decision about
whether the 3D view lives in `VisibilityPlotter` or as a separate application.

---

### C.3 Standalone application (Electron or equivalent)

**Source:** CASR-385 requirements document (front-end options)

**Description:** A packaged desktop application for rendering and user
interaction, independent of a Jupyter notebook session.

**Current status:** Not planned. `VisibilityPlotter` runs in a Jupyter
notebook or browser tab via the existing iclean/casagui infrastructure.
The architecture does not preclude this — the Bokeh layout is already
self-contained — but packaging it as an Electron app (or similar) is a
separate engineering effort.

**What would make it actionable:** A decision that notebook/browser delivery
is insufficient for a significant user segment; a resource allocation for the
packaging and distribution work; and a resolution of the CommMgr/Python kernel
communication model for a non-notebook environment (the j2p/p2j transport
currently assumes a live IPython kernel).

---

### C.4 matplotlib / scriptable rendering back-end

**Source:** CASR-385 requirements document ("Scriptability to include a
matplotlib rendering option")

**Description:** Non-interactive PNG/PDF output generated by a matplotlib
back-end rather than Bokeh, for pipeline or scripting use where no browser is
available. Matplotlib output is more easily customizable by astronomers than
Bokeh's `export_png`.

**Current status:** G-5 covers PNG export via Bokeh's own `export_png`, which
requires a headless browser (via selenium or playwright). A true matplotlib
back-end would require a parallel rendering path through the same backends
(`query_raster`, `query_columns`) but a different output layer.

**What would make it actionable:** A concrete pipeline use case where Bokeh's
`export_png` is insufficient (e.g. publication-quality figure customization,
environments without a headless browser); and a design decision about whether
the matplotlib path shares the `VisibilityRaster`/`VisibilityScatter` classes
or is a separate thin layer on top of the backend query API.

---

### C.5 Vector averaging vs scalar averaging — documentation gap

**Source:** CASR-385 requirements document ("vector averaging")

**Description:** The distinction between scalar averaging (average |V|) and
vector averaging (average Re, Im, then take |V|) is captured in the `scalar`
field of `AveragingSpec` (V-1) but is not explicitly documented anywhere in
the user-facing interface design. The default in plotms is vector averaging;
scalar averaging is important for detecting faint sources.

**Current status:** The field exists in the data model but the UI design
(sidebar averaging controls) does not yet specify how this is presented to
the astronomer, and the docstrings in `AveragingSpec` should note the
scientific implication of each choice.

**What would make it actionable:** Add a "scalar averaging" checkbox to the
averaging sidebar section with a tooltip explaining the distinction; update
the `AveragingSpec` docstring. Small effort — candidate for Phase 3 polish
rather than a new punch list item.

---

### C.6 Phase shift / phase center shift

**Source:** CASR-385 requirements document (derived quantities: "phaseshift")

**Description:** plotms supports an approximate phase center shift (`shift`
parameter, in arcsec). This is useful for re-centering a source before
inspecting visibilities. An approximate shift is a multiplication of the
visibilities by a phase ramp; an accurate shift requires `mstransform`.

**Current status:** Not in the plan. The `ReductionContext` interface has a
`split()` method that could wrap `mstransform` for accurate shifting, but an
approximate display-time shift (like plotms provides) has no current analog.

**What would make it actionable:** A confirmed astronomer use case for
display-time phase shifting during flagging inspection (as opposed to
permanent shifting via `mstransform`); and a decision about approximate vs
accurate implementation, noting the same plotms caveat about approximation
accuracy.

---

### C.7 Weighted visibilities and statistical quantities

**Source:** CASR-385 requirements document (derived quantities: "weighted
visibilities, hour-angle and elevation")

**Description:** Weighted visibilities (data × weight), hour-angle, and
elevation as displayable axes. Hour-angle and elevation are particularly useful
for diagnosing elevation-dependent gain issues and shadowing.

**Current status:** `Axis.WEIGHT` and `Axis.WEIGHT_SPECTRUM` exist in the
axes enum. `Axis.HOUR_ANGLE`, `Axis.AZIMUTH`, `Axis.ELEVATION`, and
`Axis.PARALLACTIC_ANGLE` also exist. The backend query path (`query_columns`)
would need to compute these from the MS antenna positions and observation
times, which requires casacore coordinate machinery for MSv2 or equivalent
for MSv4.

**What would make it actionable:** Backend implementation of the coordinate
computation (a moderate effort, casacore-dependent for MSv2); and confirmation
that these axes are needed for the flagging workflow rather than just for
diagnostic inspection (which could be served by listobs output).

---

### C.8 `msview` animator vs `visplot` averager

**Source:** reference-testing session (Time-vs-Channel raster test, August 2026)

**Description:** `msview` picks 2 display axes; the remaining axis becomes an Animator
stepped one frame at a time — never averaged. `visplot` averages over whatever isn't
displayed. These are different reductions; comparison for axis pairs beyond Time-vs-Baseline
is not apples-to-apples. Phase 3 averaging work (V-1 through V-9) may unblock fair comparison.

**What would make it actionable:** wire `antenna=` for single-baseline selection (§4.2),
or confirm whether msview has an averaging option.

---

### C.9 `MSv4Backend` field-ID resolution

**Source:** reference-testing session

**Description:** `_parse_field_string` positional-index fix was applied to MSv2Backend;
MSv4Backend has the same bug. Processing Sets have no FIELD subtable — no authoritative
name→FIELD_ID source found. `reduction_context.py` fallback keeps old behavior for any
backend without a real `field_ids` source: no crash, but wrong results for non-contiguous field IDs.

**What would make it actionable:** find where field IDs live in MSv4/Processing Set structure.

---

### C.10 Y-axis auto-ranging may not recompute per quantity

**Source:** reference-testing session (Real/Imaginary test)

**Description:** Imaginary scatter appeared empty ("no data") at Y range 49–53, squarely
within Real's range (Imaginary's actual range is −45 to +45). Not confirmed — could be
reused-browser-tab stale-state artifact.

**What would make it actionable:** clean repro attempt on a fresh browser tab.

---

### C.11 No mechanism to relocate a specific data point across views

**Source:** reference-testing session (Real² + Imaginary² = Amplitude² check)

**Description:** UV distance hover resolves to 3 decimal places but there is no way to
pin a point in one visplot launch and find it in another. Worked around by reading raw
visibilities directly from the MS.

**What would make it actionable:** a "copy hover coordinates" feature or session-persistent
hover log; confirmed user demand beyond testing workflows.

---

### C.12 SPW name-to-ID resolution gap

**Source:** export work (E-2 / E-6, August 2026)

**Description:** SPW selection via CASA-form `spw=N` (integer ID) in export
filenames is blocked because there is no reliable path from SPW name → integer
ID through the SPECTRAL_WINDOW subtable. The sis14 test dataset has exactly one
SPW identified by name only, so both SPW *selection* testing (selecting the one
window changes nothing observable) and `spw=N` output are untestable with it. A
multi-window MS is needed.

**What would make it actionable:** subtable access for SPECTRAL_WINDOW; a
multi-SPW test dataset; or confirmation of an alternative ID source.

---

### C.13 GUI export path vs headless state split

**Source:** export work (§9.2, August 2026)

**Description:** With no Bokeh server, `CustomJS`-set model properties never
propagate back to Python. GUI state splits into: (a) Python already knows
(axes, quantity, selection, scaling, cached agg/dfs); (b) browser-only, must
ship in the export payload (viewport per figure, layout radio, display-mode
radio, panel order); (c) deliberately different (theme). The export button's
`CustomJS` handler collects and ships (b). This is already implemented, but the
boundary is worth documenting for anyone extending the export path.
