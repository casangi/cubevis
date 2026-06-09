# Visibility Data Visualization — Requirements & Design

> **Status:** Pre-alpha planning  
> **Date:** 2026-06  
> **Context:** This document summarizes requirements and architectural decisions reached during
> initial design discussions. It is intended as a living reference to guide implementation.

---

## 1. Background & Motivation

This project creates interactive plotting tools for radio astronomy measurement data, to be
developed as part of the [cubevis](https://github.com/casangi/cubevis) codebase. There are
two primary display modes exposed through a single unified plotter:

- **Scatter/line mode** — diagnostic scatter plots of visibility and calibration data,
  analogous in scope to CASA5 `PlotMS`
- **Raster mode** — pseudo-color 2D rasterized views of measurement data, analogous to
  CASA5 `msview`

The historical separation of these into two distinct tools (`PlotMS` and `msview`) reflects
those tools having been built at different times by different teams in different languages,
not a fundamental data model distinction. This implementation unifies them into a single
`VisibilityPlotter` whose rendering mode is determined by the user's axis selection.

A predecessor prototype, [vidavis](https://github.com/casangi/vidavis), explored the HoloViz
stack (hvPlot + Panel + Datashader) on top of Bokeh, and reached a working `MsRaster`
implementation. That work informs this design but the architecture here diverges
significantly: Panel and hvPlot are excluded in favor of direct control of the Bokeh layer,
consistent with the cubevis codebase and its existing transport infrastructure.

---

## 2. Requirements

### 2.1 Functional Requirements

#### Scatter/Line Mode (PlotMS analog)

Support plotting of any combination of axes drawn from the `Axis` enumeration (see
Appendix A). The complete axis vocabulary covers:

| Category | Axes |
|---|---|
| MS metadata | scan, field, time, interval, spw, channel, frequency, velocity, correlation, antenna1, antenna2, baseline, row, observation, intent |
| Visibility values | amplitude, phase, real, imaginary, weight, weightspectrum, flag |
| Observational geometry | uvdist (m), uvdist\_λ, u, v, w, azimuth, elevation, hour angle, parallactic angle |
| Calibration table axes | gain amplitude/phase, delay, Tsys, SNR, opacity |

Typical high-priority plot combinations (covering the majority of real-world use):
- Amplitude vs. Time
- Amplitude vs. Frequency
- Phase vs. Time
- Phase vs. Frequency
- Amplitude vs. UV-distance (meters or wavelengths)
- Real vs. Imaginary
- Gain amplitude/phase vs. Time or Frequency (calibration table inspection)

Additional capabilities:
- **Data column selection:** DATA, CORRECTED, MODEL columns (MSv2) or equivalent (XRadio)
- **Time and channel averaging** — handled by Datashader aggregation at display resolution;
  no pre-averaging in the reader is required for correctness
- **Data selection:** by field, SPW, scan, antenna, baseline, correlation (see Section 4.6
  for how scan and field are handled in the MSv4 data model)
- **Color-by axis:** color plotted points by any `Axis` member
- **Iteration axis:** page through plots by baseline, antenna, etc.
- **Flag overlay:** display existing flags and session flags in distinct colors
- **Interactive flagging:** draw a region on the plot to accumulate flag operations in the
  `FlagDB`; flags are not written to disk until explicitly committed

#### Raster Mode (msview analog)

- 2D pseudo-color displays with primary axes combinations:
  - Baseline × Time (colored by amplitude, phase, or flag)
  - Frequency × Time (per baseline — RFI waterfall)
  - UV-plane coverage: U × V (colored by amplitude)
- Viewport-driven data loading: only the visible region is rendered at any time
- Pan and zoom trigger re-render at the new viewport bounds
- Interactive region selection for flagging (accumulates into `FlagDB`)
- Zoom in raster mode captures the visible coordinate range as a `SelectionSpec` that can
  be transferred to scatter mode (see Section 4.5)

#### Mode selection

Rendering mode is determined automatically from the `Axis` members chosen for the two
plot axes (see Appendix A for the full classification):

- If either axis has `axis_type == AxisType.NATIVE_DISCRETE` (baseline, antenna,
  correlation, scan, field, SPW, etc.) → **raster mode**
- Otherwise (both axes are continuous or derived) → **scatter/line mode**

The plotter indicates clearly to the user when a mode switch occurs, because the
pan/zoom interaction model changes with it (see Section 4.3).

### 2.2 Data Scale Requirements

- Must remain responsive with datasets up to **several terabytes**
- Raw visibilities are **never transmitted to the browser**; all data must be reduced to
  pixel-resolution RGBA arrays server-side before transmission — in both modes
- Datashader handles aggregation to display resolution in both modes; no pre-averaging
  in the reader is needed
- The scatter/line mode must warn the user when the unaveraged selection would exceed a
  reasonable data volume threshold, and suggest tighter selection parameters

### 2.3 Data Format Requirements

Support two storage backends, both presenting an MSv4-structured xarray DataTree to the
layers above:

| Backend | Format | Library |
|---|---|---|
| MSv2 | CASA Measurement Set v2 | `xarray-ms` (xarray backend via `arcae` C++ bindings) |
| MSv4 | Zarr (`.ps.zarr`), xarray/dask | `xradio` |

`xarray-ms` presents a full MSv4 view over MSv2 files using `xarray.open_datatree()`,
with the same DataTree structure, dimension names (`time`, `baseline_id`, `frequency`,
`polarization`), and xarray/Dask access patterns as `xradio`. The two reader classes may
therefore collapse into a single `XArrayReader` parameterized by backend.

MSv2 datasets may also be explicitly converted to MSv4 Zarr by the user using
`xradio`'s converter, for maximum read performance on parallel storage.

### 2.4 Environment & Transport Requirements

- **Jupyter Lab** — via Jupyter Lab Comms
- **Google Colab** — via Colab Comms
- **Standalone browser** — via WebSockets
- Transport is managed by the existing `CommMgr`/`Comm` layer from cubevis (reused
  without modification if possible)
- GUI must be displayable via the existing `Showable` interface
- **No Bokeh Server** — preference is to avoid the Bokeh Server to simplify compute
  cluster execution and maintain consistency with the cubevis interactive clean workflow

### 2.5 Flagging Requirements

Flagging is managed through a `FlagDB` accumulation layer that separates the interactive
flagging experience from flag persistence. Key properties:

- Existing flags from the dataset are **mutable** — the user is free to unflag them
- Existing flags are loaded **lazily** from the reader as part of normal data queries,
  not materialized into the `FlagDB` at open time
- The `FlagDB` tracks only **deltas** from the reader's FLAG data: new flag operations
  and explicit unflag operations, stored as an append-only log
- All flag operations are stored as **coordinate-range or coordinate-polygon operations
  in native MS axes** (`Axis` members with `is_native == True`); derived axes
  (`Axis.is_derived == True`) are display-only and do not contribute to flag
  specifications
- Flags are **not written to disk** until the user explicitly commits them
- Three persistence backends are supported (see Section 5)
- **Three-color rendering** distinguishes: existing flags (reader-sourced), session flags
  (`FlagDB`-sourced), and normal data
- Flagging when both axes have `is_derived == True` (e.g., amplitude vs. UV-distance)
  is not supported; the plotter warns the user and suggests switching to a view with at
  least one native axis

---

## 3. Technology Stack

| Role | Technology | Notes |
|---|---|---|
| Rendering backend | [Bokeh](https://bokeh.org/) | Custom Models, no Bokeh Server |
| Server-side rasterization & aggregation | [Datashader](https://datashader.org/) | Used directly, not via hvPlot; handles downsampling in both modes |
| Large data structures | [Dask](https://dask.org/) + [xarray](https://xarray.dev/) | Native to both backends |
| Transport | `CommMgr`/`Comm` from cubevis | WebSocket / JupyterLab / Colab |
| Display integration | `Showable` from cubevis | Consistent notebook/browser display |
| MSv4 I/O | [xradio](https://xradio.readthedocs.io/) | Dask-backed DataTree of xarray Datasets |
| MSv2 I/O | [xarray-ms](https://xarray-ms.readthedocs.io/) | xarray MSv4 view over MSv2 via `arcae` C++ bindings; thread-safe multi-reader access |

### Excluded from this project

- **casatools / python-casacore** — no longer needed in the read path; `xarray-ms`
  handles MSv2 I/O via `arcae`. May appear in `FlagCommandFile` persistence backend
  if CASA6 flag command generation requires it.
- **Panel** — has its own server/comm model that conflicts with `CommMgr`
- **hvPlot** — useful reference (explored in vidavis prototype) but bypassed; Datashader
  is used directly
- **Bokeh Server** — avoided for cluster compatibility and workflow consistency

### Notes on Datashader as the averaging layer

Datashader's `Canvas.points()` and `Canvas.raster()` aggregate all input data to
pixel resolution, producing a fixed-size output array regardless of input volume. This
is the downsampling/averaging operation — no pre-averaging in the reader is required.
For both backends, Datashader consumes Dask-backed arrays directly, so the full
dataset is never materialized in memory. The Datashader pipeline replaces the need for
a bespoke averaging iterator (analogous to casacore's `AveragingVii2`).

### Note on Cubed as a future Dask alternative

[Cubed](https://cubed-dev.github.io/cubed/) is a Python library for scalable
out-of-core array processing that positions itself as a drop-in replacement for Dask's
array API. Its defining property is **bounded, predictable memory**: rather than
relying on Dask's scheduler to manage memory dynamically (which requires careful tuning
and can produce unpredictable OOM failures at scale), Cubed computes a conservative
upper bound on memory usage during the planning phase and raises an exception *before*
running if the budget would be exceeded. All inter-task data movement happens via
cloud storage (Zarr) rather than in-memory shuffle, making it inherently stateless and
well-suited to serverless and cluster execution environments.

For an interactive visualization tool operating on TB-scale datasets, Cubed's
predictable memory model is architecturally attractive — a viewer that crashes due to
an unexpected OOM mid-session is a poor user experience, and Cubed's pre-flight memory
check would surface this before the computation begins.

**Why Cubed is not used now.** Two gaps currently prevent adoption:

1. **Datashader has no Cubed backend.** Datashader's `Canvas.points()` dispatches
   through Dask explicitly; there is no equivalent dispatch path for Cubed arrays.
   Passing a Cubed-backed xarray Dataset to Datashader would either fail or silently
   materialize the full array, defeating the purpose entirely.

2. **`cubed-xarray` is not yet production-ready.** The `cubed-xarray` glue package
   that allows xarray to use Cubed as its chunked array backend is explicitly
   described as a proof of concept with known incomplete areas, including
   `xarray.map_blocks` not dispatching to `cubed.map_blocks` and certain reduction
   operations falling back to Dask or triggering immediate computation.

**Why the architecture accommodates it later.** The parallel compute backend is
entirely hidden inside `XArrayReader`. If Datashader gains a Cubed backend and
`cubed-xarray` matures, switching from Dask to Cubed requires no changes to the
source classes, `FlagDB`, plotter, or any other layer — it is a pure `XArrayReader`
implementation concern. This is tracked as a future consideration in Section 10.

---

## 4. Architecture

### 4.1 Layer Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    User / Notebook                          │
└────────────────────────┬────────────────────────────────────┘
                         │ instantiates
┌────────────────────────▼────────────────────────────────────┐
│                  VisibilityPlotter                          │
│   (figure + controls + layout + flag commit UI)             │
│   mode: 'scatter' | 'raster'  ← set by Axis.triggers_raster│
└──────┬──────────────────────────────────┬───────────────────┘
       │ owns (one active at a time)      │ owns
       │                                  │
┌──────▼──────────────┐  ┌───────────────▼───────────────────┐
│VisibilityLineSource │  │VisibilityRasterSource              │
│(Bokeh Model)        │  │(Bokeh Model)                       │
│- columnar query     │  │- viewport-driven 2D query          │
│- caches lazy Dataset│  │- always re-queries reader on       │
│- Datashader points()│  │  viewport change                   │
│- composites FlagDB  │  │- Datashader Canvas.raster()        │
│- RGBA uint8 →CommMgr│  │- composites FlagDB overlay         │
│                     │  │- RGBA uint8 → CommMgr              │
└──────┬──────────────┘  └──────────────┬────────────────────┘
       │                                │
       └──────────────┬─────────────────┘
                      │ both inherit from
           ┌──────────▼──────────────┐
           │   VisibilitySourceBase  │
           │   (Bokeh Model)         │
           │   - holds reader ref    │
           │   - holds FlagDB ref    │
           │   - metadata queries    │
           │   - CommMgr boilerplate │
           │   - RGBA uint8 contract │
           └──────┬──────────────────┘
                  │
       ┌──────────┴──────────────────┐
       │                             │
┌──────▼──────────────┐  ┌──────────▼──────────────────────  ┐
│      FlagDB         │  │        XArrayReader                │
│ (accumulation layer)│  │  (ABC or single parameterized      │
│ - append-only log   │  │   class, pure Python, no Bokeh)    │
│   of FlagOperations │  │  - wraps xarray-ms or xradio       │
│ - evaluate(coords)  │  │  - accepts Axis members for        │
│   → signed mask     │  │    xaxis/yaxis/axis1/axis2         │
│ - persist(backend)  │  │  - translates SelectionSpec to     │
│ - JSONL on disk     │  │    xarray operations               │
└──────┬──────────────┘  │  - always returns scan_name,       │
       │                 │    field_name, antenna labels       │
       │                 │    alongside data                   │
       │                 └──────┬───────────────┬─────────────┘
       │                        │               │
       │              ┌─────────▼───┐   ┌───────▼───────────┐
       │              │  MSv2Back-  │   │  MSv4Backend      │
       │              │  end        │   │  xradio            │
       │              │  xarray-ms  │   │                    │
       │              │  + arcae    │   │                    │
       │              └─────────────┘   └────────────────────┘
       │
┌──────▼────────────────────────────┐
│         Flag Persistence          │
├───────────────────────────────────┤
│ FlagWriteThrough  (xarray write)  │
│ FlagCommandFile   (CASA6 flagdata)│
│ FlagTable         (CASA flag tbl) │
└───────────────────────────────────┘
```

### 4.2 Layer Descriptions

#### `VisibilityPlotter` (user-facing toolbox class)

The single class the user instantiates. Assembles:
- A Bokeh `Figure` with an `ImageRGBA` glyph (Datashader output in both modes)
- GUI controls shared across modes: data selection (using human-readable scan names,
  field names, antenna names — see Section 4.6), data column, colormap, iteration
  controls; axis selectors populated from `Axis` members
- Flag commit UI: persist button, backend selector, session flag summary
- Mode-specific controls shown/hidden based on active mode
- The appropriate source class, switched when `Axis.triggers_raster` changes
- CommMgr callback wiring and `Showable` integration

Mode selection logic:

```python
def _select_mode(self, xaxis: Axis, yaxis: Axis) -> str:
    if xaxis.triggers_raster or yaxis.triggers_raster:
        return 'raster'
    return 'scatter'
```

Flagging validity check:

```python
def _can_flag(self, xaxis: Axis, yaxis: Axis) -> bool:
    return xaxis.is_native or yaxis.is_native
```

Multiple `VisibilityPlotter` instances may be created and run concurrently, each with
its own independent mode, colormap, query state, `FlagDB`, and comm channel.

#### `XArrayReader` (pure Python, no Bokeh dependency)

Wraps either `xarray-ms` (for MSv2) or `xradio` (for MSv4), presenting an identical
MSv4-structured DataTree interface to all layers above. Accepts `Axis` members for all
axis arguments; encapsulates all translation between the MSv4 data model and the
human-meaningful vocabulary astronomers use.

Responsibilities:
- **Data queries:** accept `Axis` members for `xaxis`, `yaxis`, `axis1`, `axis2`,
  `quantity`; return Dask-backed xarray Datasets for a given `SelectionSpec`, always
  including `scan_name`, `field_name`, `baseline_antenna1_name`, and
  `baseline_antenna2_name` alongside the requested data variables
- **Axis dispatch:** uses `Axis.axis_type` and `Axis.is_native` to determine the
  correct xarray operation for each requested axis (e.g., `Axis.AMPLITUDE` triggers
  `np.abs(ds.VISIBILITY)` computation; `Axis.TIME` uses the native time coordinate
  directly; `Axis.SCAN` uses `ds.scan_name` as a non-index coordinate)
- **Scan and field selection:** translate `SelectionSpec.scan` (string scan names) and
  `SelectionSpec.field` (field name strings) to `ds.where(ds.scan_name.isin(...))`
  operations — internal to the reader (see Section 4.6)
- **Metadata queries:** return available scan names, field names, antenna names, SPWs,
  time range, frequency range in human-readable form for populating GUI controls
- **Read-only:** no flag-write methods; flag persistence is entirely the `FlagDB`'s
  responsibility

Key interface methods (indicative, not final):

```python
class XArrayReader(ABC):

    def query_columns(self,
                      xaxis: Axis,
                      yaxis: Axis,
                      data_column: str,
                      selection: SelectionSpec) -> xr.Dataset:
        """Return lazy Dask-backed Dataset for scatter mode.
        Always includes scan_name, field_name, baseline_antenna1_name,
        baseline_antenna2_name alongside computed axis values and FLAG.
        Datashader consumes this directly; no pre-averaging."""

    def query_raster(self,
                     axis1: Axis,
                     axis2: Axis,
                     quantity: Axis,
                     bounds: tuple,
                     shape: tuple,
                     selection: SelectionSpec) -> xr.DataArray:
        """Return a 2D DataArray covering bounds at viewport pixel shape.
        axis1 and axis2 must have triggers_raster == True or be continuous
        native axes; quantity is the colormap value (typically DERIVED)."""

    def metadata(self) -> dict:
        """Scan names, field names, antenna names, SPWs, time range,
        freq range — all in human-readable form for GUI controls."""
```

#### `SelectionSpec` (shared data class)

An explicit, shareable representation of the current data selection in native MS
coordinate terms. Used by GUI controls, `XArrayReader`, `FlagOperation` records, and
transferred between modes on mode switch. All identifiers are human-readable strings
(antenna names, field names, scan name strings), not internal integer indices.

```python
@dataclass
class SelectionSpec:
    field: list[str] | None         # field name strings, or None = all
    scan: list[str] | None          # scan name strings, or None = all
    spw: list[int] | None           # SPW indices, or None = all
    time_range: tuple | None        # (t_min, t_max) MJD seconds
    baselines: list[tuple] | None   # [(ant1_name, ant2_name), ...] or None = all
    freq_range: tuple | None        # (f_min, f_max) Hz
    correlation: list[str] | None   # ['XX', 'YY', ...] or None = all
```

`XArrayReader` translates to xarray operations internally; `SelectionSpec` never
exposes internal MSv4 integer indices to the layers above.

#### `FlagDB` (flag accumulation layer)

Sits between user interactions and any persistence mechanism. Accumulates flag
operations as a compact, append-only delta log. Never materializes the full FLAG array.

Key properties:
- Each `FlagOperation` record contains: operation type (`flag`/`unflag`), a
  `SelectionSpec` giving the coordinate-range specification, `flag_type`
  (`'coordinate_range'` or `'coordinate_polygon'`), optional polygon vertices in
  native coordinate space for lasso selections, `xaxis: Axis` and `yaxis: Axis` at
  the time of flagging, `selection_shape` (`'rectangle'`/`'lasso'`), `origin_mode`
  (`'scatter'`/`'raster'`), timestamp, sequence number, optional annotation
- Only `Axis` members with `is_native == True` contribute to the coordinate
  specification; `is_derived == True` axes are ignored at flag-creation time
- **Undo** appends a compensating operation — log is always append-only, preserving
  full audit history
- `evaluate(coords) -> signed_mask`: produces `+1` (newly flagged), `-1` (unflagged),
  `0` (unchanged); composited onto the reader's FLAG array during rendering
- `persist(backend)`: computes net state (compensating operations cancel out) before
  passing to backend
- In-memory: list of `FlagOperation` dataclass instances — no DB manager needed
- Session persistence: JSONL format, written incrementally; no dependencies beyond
  Python standard library

#### Flag rendering — three-color overlay

The rendering pipeline composites three layers before colormapping:
1. **Reader FLAG array** — existing flags, distinct "existing flag" color
2. **FlagDB `+1` mask** — session flags, distinct "session flag" color
3. **FlagDB `-1` mask** — unflagged regions, cleared from existing flag layer

#### `VisibilitySourceBase` (Bokeh Model)

Shared base for the two source classes. Holds references to both an `XArrayReader`
and a `FlagDB`. Provides CommMgr boilerplate, common property definitions (including
`xaxis: Axis`, `yaxis: Axis` or `axis1: Axis`, `axis2: Axis`), and the contract that
all subclasses produce RGBA uint8 numpy arrays as output.

#### `VisibilityLineSource` (Bokeh Model)

Manages the columnar data pipeline for scatter/line mode.

- Bokeh Model properties include `xaxis: Axis`, `yaxis: Axis`, `data_column`,
  `color_axis: Axis`, `iter_axis: Axis`, `iter_index`, and `selection: SelectionSpec`
- On query change: calls `reader.query_columns(xaxis, yaxis, ...)` with the current
  `SelectionSpec`, caches the resulting lazy Dataset and its data extent, runs
  Datashader `Canvas.points()`, composites `FlagDB` overlay, sends RGBA uint8 via
  CommMgr
- On viewport change: re-runs Datashader on cached Dataset — reader not re-invoked
  unless viewport exceeds cached data extent
- On flag region received: checks `xaxis.is_native or yaxis.is_native`; if neither,
  warns user; otherwise projects selection onto native axes and adds `FlagOperation`
  to `FlagDB`

#### `VisibilityRasterSource` (Bokeh Model)

Manages the viewport-driven 2D pipeline for raster mode.

- Bokeh Model properties include `axis1: Axis`, `axis2: Axis`, `quantity: Axis`,
  and `selection: SelectionSpec`
- On viewport change: calls `reader.query_raster(axis1, axis2, quantity, ...)`;
  captures viewport bounds as an updated `SelectionSpec`; runs Datashader
  `Canvas.raster()`; composites `FlagDB` overlay; sends RGBA uint8
- On flag region received: translates rectangle or lasso boundary to native axis
  coordinate ranges/polygon; adds `FlagOperation` to `FlagDB`; re-renders

### 4.3 Rendering Pipeline

Both modes produce RGBA uint8 output via Datashader and transmit via CommMgr:

```
Storage (MSv2 via xarray-ms+arcae / MSv4 Zarr via xradio)
        │
        ▼
XArrayReader.query_*(xaxis: Axis, yaxis: Axis, selection: SelectionSpec)
  [applies SelectionSpec; dispatches axis computation via Axis.axis_type]
  → lazy Dask-backed xarray Dataset (never fully materialized)
  → always includes scan_name, field_name, antenna labels alongside data
        │
        ▼
Datashader Canvas
  → Canvas.points()  [scatter: re-run on viewport change, cached data]
  → Canvas.raster()  [raster:  re-run on viewport change, fresh data]
  → aggregate at viewport pixel resolution
        │
        ▼
FlagDB.evaluate(coords) → signed mask
  → composite: existing flags | session flags | unflagged regions
        │
        ▼
Server-side colormapping — three-color RGBA compositing
  → RGBA uint8 numpy array
        │
        ▼
CommMgr → BokehJS → Bokeh ImageRGBA glyph update
```

**Pan/zoom handling:**
- Both modes handle pan/zoom identically from the user's perspective
- Scatter mode: Datashader re-runs on cached data (fast); reader re-invoked only if
  viewport exceeds cached extent
- Raster mode: reader always re-invoked; latency depends on storage and data volume
- In both modes, Bokeh never renders individual data glyphs; overplotting is encoded
  in Datashader aggregation

### 4.4 JS-to-Python Callback Pattern (no Bokeh Server)

Without the Bokeh Server there are no automatically triggered Python callbacks on
property change. Instead, the cubevis `CustomJS`/CommMgr pattern is used throughout:

1. A Bokeh widget has a `CustomJS` callback attached to its relevant property
2. The `CustomJS` sends a typed message through the active comm channel
3. Python's CommMgr dispatch receives the message, runs the appropriate query or
   action, and pushes back a new RGBA array

This is the same mechanism already used in cubevis for channel navigation. Iteration
through baselines or antennas, viewport pan/zoom, flag region commits, flag persistence
operations, and mode switches all use this channel.

### 4.5 Raster-to-Scatter Mode Switch with Selection Transfer

A key workflow — identifying a suspicious region in the raster view, then switching to
a line plot of that region — is supported through `SelectionSpec` transfer on mode
switch:

1. User zooms into a region of the raster display
2. `VisibilityRasterSource` captures the visible coordinate bounds as a `SelectionSpec`
3. User switches to scatter mode by changing an axis to a `DERIVED` `Axis` member
4. `VisibilityPlotter` presents an axis-choice dialog: which raster axis becomes the
   line plot X axis (`axis1` or `axis2`), and which `DERIVED` `Axis` becomes Y
5. The `SelectionSpec` from the raster zoom initializes the scatter mode query
6. Flag operations on the line plot use the same `SelectionSpec` context and are
   visible as overlays when switching back to raster mode

This feature is planned for a second implementation increment. `SelectionSpec` is
designed from the start to support it.

### 4.6 MSv4 Coordinate Model and Scan/Field Handling

The MSv4 schema (used by both `xradio` and `xarray-ms`) represents scan and field
information differently from MSv2, with practical consequences for display labeling
and data selection.

**Key structural facts:**
- `scan_name` is a **non-index string coordinate on the time dimension** — attached to
  each time sample; supports `ds.where()` but not direct `.sel()` indexing
- `field_name` is similarly a **non-index coordinate on the time dimension**
- `baseline_id` is an integer index; human-readable antenna names are in
  `baseline_antenna1_name` and `baseline_antenna2_name` as non-index coordinates
- The Processing Set is partitioned by `(DATA_DESC_ID, OBS_MODE, OBSERVATION_ID)` —
  not by scan; multiple scans may appear within a single partition
- **Scan number is not and will not be a first-class axis** in the MSv4 schema;
  `Axis.SCAN` is classified `NATIVE_DISCRETE` for mode-selection purposes but its
  selection is implemented via boolean masking, not index lookup

**Consequences for `XArrayReader`:**
- `Axis.SCAN` and `Axis.FIELD` selection: `ds.where(ds.scan_name.isin(selection.scan))`
- `Axis.BASELINE` selection: `ds.where((ds.baseline_antenna1_name == ant1) &
  (ds.baseline_antenna2_name == ant2))`
- `query_columns()` always returns `scan_name`, `field_name`,
  `baseline_antenna1_name`, `baseline_antenna2_name` alongside requested data
- `metadata()` returns scan names and field names as strings from the Processing Set
  summary; these drive the selection GUI controls

**Consequences for plot labeling:**
- Time axes are labeled in MJD seconds natively; the plotter adds scan-boundary
  annotations and tick labels showing scan names at scan transition times
- `Axis.SCAN` is not used as a primary continuous axis — scan is an annotation on
  the time axis

**Consequences for `FlagDB` and `FlagCommandFile`:**
- `FlagOperation.selection.scan` stores scan name strings (e.g., `['3', '5']`)
- `FlagCommandFile` serializes as `flagdata(scan='3,5', ...)` — direct mapping

**Note on the vidavis experience:**
The predecessor prototype encountered difficulties with scan and field labeling when
working directly with the AstroViper/GraphViper API, which exposed raw MSv4 coordinate
structure without translation. The `XArrayReader` layer specifically absorbs all
MSv4-to-display-vocabulary translation so that layers above always work with
human-readable identifiers and `Axis` members.

### 4.7 Flagging Data Flow

```
User draws region on plot (either mode)
  → BokehJS captures display-coordinate bounds and flag/unflag intent
  → CommMgr sends to Python
  → Source class checks xaxis.is_native or yaxis.is_native:
      - if neither → warn user; no FlagOperation created
      - if at least one native axis → project selection onto native axes
        (derived axis extent ignored); create coordinate range/polygon
  → FlagOperation (with SelectionSpec, xaxis: Axis, yaxis: Axis) added to FlagDB
  → FlagDB.evaluate() generates signed mask for current query coordinates
  → Re-render with updated three-color overlay (no disk write)

User clicks "Commit flags"
  → FlagDB.persist(chosen_backend) called
  → Net state computed (compensating undo operations cancelled out)
  → Backend applies net operations (see Section 5)
  → FlagDB optionally cleared or retained for continued editing
```

---

## 5. Flag Persistence Backends

Flag persistence is a pluggable operation invoked explicitly by the user. The `FlagDB`
delta log is backend-agnostic; the same accumulated operations can be committed through
any of the following backends.

### 5.1 `FlagWriteThrough` — direct write to dataset

Applies the net flag delta directly to the FLAG array of the open dataset:
- For MSv4 Zarr (`xradio`): writes through xarray's Zarr I/O
- For MSv2 (`xarray-ms`): writes through `xarray-ms`'s write-back mechanism (to be
  confirmed; see Section 10)

Evaluates the delta against the actual FLAG column at commit time to prevent spurious
unflag writes to already-unflagged rows, which could corrupt CASA6 pipeline metadata.

### 5.2 `FlagCommandFile` — CASA6 flag command serialization

Serializes the net flag delta as `flagdata()`-compatible commands to a text file.
`FlagOperation.selection.scan` string lists map directly to `flagdata(scan='3,5', ...)`
specifications. Useful when the original data file must not be modified, or when flags
need to be applied to another dataset.

### 5.3 `FlagTable` — CASA flag table

Writes a structured CASA flag table applicable via
`flagdata(mode='apply', inpfile=...)`. Appropriate for observatory pipeline integration.

### 5.4 Session persistence (FlagDB delta log)

The `FlagDB` delta log is persisted to disk in JSONL format (one JSON record per
`FlagOperation`), written incrementally. Full audit history including undo
(compensating) operations is preserved. `persist(backend)` computes net state before
writing — the two operations are independent.

---

## 6. MSv2 I/O — `xarray-ms` and `arcae`

### 6.1 Why xarray-ms

`xarray-ms` presents a full MSv4-structured DataTree view over MSv2 files using
`xarray.open_datatree()`. Identical DataTree structure, dimension names, and
xarray/Dask access patterns as `xradio`. Current release: v0.5.4 (May 2026),
actively maintained by the RATT group (Simon Perkins). Key benefits:

- `XArrayReader` uses the same code path for both backends
- Datashader consumes both identically
- `arcae` C++ layer provides thread-safe casacore access; single-worker-thread
  constraint lifted for reads
- `casatools` and `python-casacore` removed from the read path

### 6.2 Performance expectations

Read performance bounded by casacore's `TiledStMan` disk access model. On a single
disk, sequential reads are comparable to prior casatools approaches; parallel reads
offer limited benefit. XRadio on Zarr remains the highest-performance path; Zarr's
chunked layout was designed for parallel random reads in a way casacore's format was
not. Users needing maximum performance on large MSv2 datasets should convert to MSv4
Zarr first.

### 6.3 Binary format specification (future work, out of scope)

A C/C++-independent specification of the casacore Table Data System binary format
would have long-term archival value. Prior work: `ms2zarr` C++ proof-of-concept and
casa-formats-specification Kaitai Struct descriptions. Out of scope for this project.

---

## 7. Key Design Decisions & Rationale

| Decision | Rationale |
|---|---|
| `Axis` Enum with `AxisType` classification | Single authoritative definition of the axis vocabulary; `axis_type` drives mode selection, flagging validity, and reader dispatch without string comparisons or branching on magic values |
| Mode selection via `Axis.triggers_raster` | Clean one-liner; automatically correct for any new axis added to the Enum |
| Flagging validity via `Axis.is_native` | Same property used by `FlagDB` and source classes; consistent semantics across both modes |
| Single `VisibilityPlotter` with two internal source classes | The historical PlotMS/msview split reflects implementation history, not data model distinction. Source classes remain separate because their update semantics are genuinely different. |
| Datashader as the averaging/downsampling layer | Aggregation to pixel resolution replaces pre-averaging iterators. Both modes produce RGBA uint8; no individual glyphs sent to browser. Scales to TB via Dask backend. |
| `XArrayReader` accepts `Axis` members, not strings | Type-safe axis dispatch; reader uses `Axis.axis_type` to determine the correct xarray operation for each axis |
| `XArrayReader` wrapping `xarray-ms` and `xradio` | Both backends present MSv4 DataTree; reader encapsulates all MSv4-to-display-vocabulary translation including scan/field/antenna label handling |
| `SelectionSpec` as an explicit shared data class | Portable between modes, GUI controls, reader queries, and `FlagOperation` records. All identifiers human-readable strings. Enables raster-to-scatter mode switch. |
| `FlagDB` accumulation layer | Separates interactive flagging from persistence; undo via compensating operations; multiple persistence backends; reader read-only; scales to TB by tracking only coordinate-range deltas |
| Flags stored as coordinate-range/polygon in native `Axis` members only | Derived axes are display-only. Matches PlotMS semantics. Only approach that scales to TB datasets — pixel-level provenance not feasible. |
| Lasso selections produce coordinate polygons, not row index sets | Compact, exact, directly expressible as `flagdata()` region parameters |
| Flagging on two `is_derived` axes unsupported | No meaningful coordinate-range flag can be inferred. User warned. |
| Existing flags mutable, loaded lazily | Users free to correct pipeline flags. Materializing existing flags impractical for TB. |
| Three-color flag rendering | Visual feedback on existing vs. session vs. unflagged without disk writes |
| Undo via compensating operations | Log append-only; full audit history preserved; `persist()` computes net state |
| In-memory FlagDB, JSONL session persistence | No DB manager needed; session log is kilobytes; JSONL has no dependencies, human-readable, naturally append-only |
| `FlagWriteThrough` evaluates delta at commit time | Prevents spurious unflag writes; protects CASA6 pipeline weight metadata |
| Scan as non-index time coordinate; `Axis.SCAN` is `NATIVE_DISCRETE` | MSv4 schema design; scan will not be first-class. Reader handles via `ds.where()`; plotter annotates time axis with scan boundaries |
| No Bokeh Server | Consistent with cubevis; simplifies cluster execution; Python session reusable between GUI invocations |
| `CustomJS` + CommMgr for all JS-to-Python callbacks | Proven pattern from cubevis; works across WebSocket, JupyterLab, Colab |
| GPU acceleration out of scope | Datashader cuDF/CuPy backends are drop-in at dataframe/array level; architecture accommodates without change |

---

## 8. Relationship to Existing Codebase

| This project | cubevis analog | Notes |
|---|---|---|
| `VisibilityPlotter` | `_cube.py` toolbox | Single unified tool; owns mode switching and flag commit UI |
| `VisibilityLineSource` | `ImageDataSource` | Lazy Dataset cache + Datashader points pipeline |
| `VisibilityRasterSource` | `ImageDataSource` | Viewport-driven; closest structural analog |
| `XArrayReader` | `ImagePipe` | Pure-Python I/O; wraps xarray-ms or xradio; encapsulates all MSv4 translation; accepts `Axis` members |
| `Axis` / `AxisType` | *(new)* | Enum-based axis vocabulary; no analog in cubevis |
| `SelectionSpec` | *(new)* | Shared selection data class; no analog in cubevis |
| `FlagDB` | *(new)* | Flag accumulation, undo, persistence; no analog in cubevis |
| `CommMgr` / `Comm` | `CommMgr` / `Comm` | **Reused directly** |
| `Showable` | `Showable` | **Reused directly** |
| Transport layer | `_low_level_transport.py` | **Reused directly** |

---

## 9. MSv2 vs. XRadio Reader Tradeoffs

| | MSv2 via `xarray-ms` | MSv4 via `xradio` |
|---|---|---|
| **Read performance** | Bounded by casacore `TiledStMan` I/O; parallel reads limited by storage topology | Fast — Zarr chunked I/O; Dask parallelism fully effective |
| **Flag writes** | Via `FlagWriteThrough` using `xarray-ms` write-back (to be confirmed) | Via `FlagWriteThrough` to Zarr FLAG array |
| **Data consistency** | Operating on original MSv2 | Operating on Zarr copy; original MSv2 unchanged |
| **Setup overhead** | None — `xarray.open_datatree()` on MSv2 directly | Conversion step required (`xradio` converter) |
| **Interface to reader** | MSv4 DataTree — identical to MSv4 path | MSv4 DataTree |
| **Scan/field access** | `scan_name`/`field_name` non-index coordinate model | Same model |
| **CASA6 pipeline integration** | Natural — MSv2 is the native format | Requires managing two copies |

---

## 10. Remaining Open Questions

- **`xarray-ms` write-back completeness:** does current `xarray-ms` (v0.5.4) support
  writing FLAG data back to MSv2 via `FlagWriteThrough`? Verify before implementing
  `FlagWriteThrough` for the MSv2 backend.

- **Scatter mode extent cache policy:** how much margin beyond the new viewport bounds
  should be fetched when the viewport moves outside the cached data extent? To be
  defined before implementation.

- **Concurrent plotter instances:** cubevis interactive clean has not been verified to
  support multiple concurrently live GUI objects sharing the same CommMgr. Verify
  before relying on `VisibilityPlotter` concurrency.

- **Raster-to-scatter mode switch (second increment):** the axis-choice dialog and
  `SelectionSpec` transfer mechanism are deferred to a second implementation increment.

- **Scan boundary annotation on time axis:** the specific approach for annotating scan
  boundaries and scan name labels on a continuous time axis needs UI design work —
  to be resolved during plotter implementation.

- **Cubed as a future Dask replacement:** Cubed's bounded-memory serverless model is
  architecturally attractive for TB-scale interactive visualization, and the design
  accommodates switching without changes above the `XArrayReader` layer. Monitor
  progress on (a) Datashader adding a Cubed backend and (b) `cubed-xarray` reaching
  production readiness. Revisit once both gaps are closed.

---

## Appendix A — `Axis` and `AxisType` Enumeration

The complete axis vocabulary is defined as a pair of Python enumerations. `AxisType`
classifies each axis for mode-selection and flagging logic. `Axis` is the single
authoritative definition of all supported plot axes.

```python
from enum import Enum, auto
from typing import Literal

class AxisType(Enum):
    """Classification of a plot axis.

    Used by VisibilityPlotter for mode selection and flagging validity,
    and by XArrayReader to dispatch the correct xarray operation.
    """
    NATIVE_CONTINUOUS = auto()
    # Exists as a real coordinate or column in the MSv4 DataTree.
    # Continuous-valued; does not trigger raster mode.
    # Examples: time, frequency, u, v, w, uvdist

    NATIVE_DISCRETE   = auto()
    # Exists as a real coordinate or column in the MSv4 DataTree.
    # Discrete-valued; triggers raster mode when used as a plot axis.
    # Note: SCAN and FIELD are NATIVE_DISCRETE for mode-selection purposes
    # but are implemented via ds.where(ds.scan_name.isin(...)) in the reader,
    # not via direct .sel() indexing (see Section 4.6).
    # Examples: baseline, antenna, correlation, scan, field, spw

    DERIVED           = auto()
    # Computed from data variables; not directly selectable in the DataTree.
    # Display-only for flagging purposes (Axis.is_native == False).
    # Examples: amplitude, phase, real, imaginary, uvdist_lambda

    CALIBRATION       = auto()
    # Valid only when the data source is a calibration table.
    # Examples: gain_amplitude, gain_phase, delay, tsys, snr, opacity


class Axis(Enum):
    """All supported plot axes.

    Each member carries a human-readable label, a unit string for axis
    tick formatting, and an AxisType classification.

    Usage in VisibilityPlotter:
        mode = 'raster' if xaxis.triggers_raster or yaxis.triggers_raster
               else 'scatter'
        can_flag = xaxis.is_native or yaxis.is_native

    Usage in XArrayReader:
        reader dispatches the correct xarray operation based on axis_type.
    """

    # ------------------------------------------------------------------ #
    # Native continuous                                                    #
    # ------------------------------------------------------------------ #
    TIME              = ("Time",             "s",     AxisType.NATIVE_CONTINUOUS)
    FREQUENCY         = ("Frequency",        "Hz",    AxisType.NATIVE_CONTINUOUS)
    CHANNEL           = ("Channel",          "",      AxisType.NATIVE_CONTINUOUS)
    VELOCITY          = ("Velocity",         "m/s",   AxisType.NATIVE_CONTINUOUS)
    UVDIST            = ("UV Distance",      "m",     AxisType.NATIVE_CONTINUOUS)
    U                 = ("U",               "m",     AxisType.NATIVE_CONTINUOUS)
    V                 = ("V",               "m",     AxisType.NATIVE_CONTINUOUS)
    W                 = ("W",               "m",     AxisType.NATIVE_CONTINUOUS)
    INTERVAL          = ("Interval",         "s",     AxisType.NATIVE_CONTINUOUS)
    ROW               = ("Row",              "",      AxisType.NATIVE_CONTINUOUS)

    # ------------------------------------------------------------------ #
    # Native discrete                                                      #
    # ------------------------------------------------------------------ #
    BASELINE          = ("Baseline",         "",      AxisType.NATIVE_DISCRETE)
    ANTENNA1          = ("Antenna 1",        "",      AxisType.NATIVE_DISCRETE)
    ANTENNA2          = ("Antenna 2",        "",      AxisType.NATIVE_DISCRETE)
    CORRELATION       = ("Correlation",      "",      AxisType.NATIVE_DISCRETE)
    SCAN              = ("Scan",             "",      AxisType.NATIVE_DISCRETE)
    FIELD             = ("Field",            "",      AxisType.NATIVE_DISCRETE)
    SPW               = ("SPW",              "",      AxisType.NATIVE_DISCRETE)
    OBSERVATION       = ("Observation",      "",      AxisType.NATIVE_DISCRETE)
    INTENT            = ("Intent",           "",      AxisType.NATIVE_DISCRETE)

    # ------------------------------------------------------------------ #
    # Derived                                                              #
    # ------------------------------------------------------------------ #
    AMPLITUDE         = ("Amplitude",        "",      AxisType.DERIVED)
    PHASE             = ("Phase",            "rad",   AxisType.DERIVED)
    REAL              = ("Real",             "",      AxisType.DERIVED)
    IMAGINARY         = ("Imaginary",        "",      AxisType.DERIVED)
    WEIGHT            = ("Weight",           "",      AxisType.DERIVED)
    WEIGHT_SPECTRUM   = ("Weight Spectrum",  "",      AxisType.DERIVED)
    FLAG              = ("Flag",             "",      AxisType.DERIVED)
    UVDIST_LAMBDA     = ("UV Distance",      "λ",     AxisType.DERIVED)
    AZIMUTH           = ("Azimuth",          "deg",   AxisType.DERIVED)
    ELEVATION         = ("Elevation",        "deg",   AxisType.DERIVED)
    HOUR_ANGLE        = ("Hour Angle",       "h",     AxisType.DERIVED)
    PARALLACTIC_ANGLE = ("Parallactic Angle","deg",   AxisType.DERIVED)

    # ------------------------------------------------------------------ #
    # Calibration table                                                    #
    # ------------------------------------------------------------------ #
    GAIN_AMPLITUDE    = ("Gain Amplitude",   "",      AxisType.CALIBRATION)
    GAIN_PHASE        = ("Gain Phase",       "rad",   AxisType.CALIBRATION)
    DELAY             = ("Delay",            "s",     AxisType.CALIBRATION)
    TSYS              = ("Tsys",             "K",     AxisType.CALIBRATION)
    SNR               = ("SNR",              "",      AxisType.CALIBRATION)
    OPACITY           = ("Opacity",          "",      AxisType.CALIBRATION)

    # ------------------------------------------------------------------ #
    # Constructor and properties                                           #
    # ------------------------------------------------------------------ #

    def __init__(self, label: str, unit: str, axis_type: AxisType):
        self.label = label          # human-readable display label
        self.unit = unit            # unit string for axis tick labels
        self.axis_type = axis_type  # classification

    @property
    def is_native(self) -> bool:
        """True if this axis exists as a real coordinate or column in the
        MSv4 DataTree and can contribute to a FlagOperation specification."""
        return self.axis_type in (
            AxisType.NATIVE_CONTINUOUS,
            AxisType.NATIVE_DISCRETE,
        )

    @property
    def is_derived(self) -> bool:
        """True if this axis is computed from data variables and is
        display-only for flagging purposes."""
        return self.axis_type == AxisType.DERIVED

    @property
    def triggers_raster(self) -> bool:
        """True if using this axis causes VisibilityPlotter to switch to
        raster mode."""
        return self.axis_type == AxisType.NATIVE_DISCRETE
```

### Notes on specific members

**`UVDIST` vs. `UVDIST_LAMBDA`:** `UVDIST` (metres) is classified `NATIVE_CONTINUOUS`
because u, v, w are stored directly in the DataTree as the `UVW` data variable —
the distance in metres is computed as `sqrt(u² + v²)` from native data. `UVDIST_LAMBDA`
(wavelengths) requires dividing by the wavelength derived from frequency, making it
fully derived and display-only for flagging.

**`SCAN` and `FIELD`:** classified `NATIVE_DISCRETE` for mode-selection purposes, but
their MSv4 representation as non-index time coordinates means selection is via
`ds.where()` boolean masking in the reader, not direct `.sel()` indexing. This is an
internal reader implementation detail; the Enum classification reflects their semantic
role, not their xarray access pattern.

**Calibration axes:** `AxisType.CALIBRATION` members are only valid when the
`XArrayReader` is opened against a calibration table rather than a visibility dataset.
The plotter should filter the axis selector to show only calibration axes when a cal
table is open, and exclude them otherwise.

---

*Document generated from design discussions. Update as decisions are refined.*
