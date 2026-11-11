# Iteration grids for `visplot` and a first `calplot`: plan

*Written 2026-11-10 against `casangi/cubevis` `main` at `a42cab5`; kept in
`devel/docs/gridplot`. G0 results: section 11. Builds on, and in one
place supersedes, `devel/notes/drs/visplot/visplot-grid-iteration-notes.md`
(July 2026, "the July notes" below).*

Confidence markers, as in `hrs_commissioning_workflow_survey.md`:

- **[doc]** from the documentation of the tool named (sources at the end).
- **[code]** read in this repository (or in `casangi/xradio`) at the commit
  named.
- **[recall]** general knowledge of CASA/AIPS practice, not re-checked in
  this session. Check before designing against the detail.

---

## 0. Short answer

1. **Grid plotting is a real, standard workflow, and calibration tables are
   its main use.** Every calibration plotter in the lineage (AIPS SNPLT,
   CASA plotcal, plotms, plotbandpass, difmap corplot) shows solutions one
   antenna per small plot, several per page, paged through the antennas.
   Visibility plotters do the same per baseline (AIPS VPLOT, difmap vplot,
   Miriad uvplt). "Channel maps" are the image-domain equivalent (a grid of
   cube planes). That is CARTA's job, not visplot's (section 1).
2. **Yes, `calplot`'s primary mode is the grid.** It is "iterate over
   antenna" with an optional overlay ("colour by antenna/SPW/polarization")
   as the second idiom. Both are needed. The second already exists in
   visplot as colour-by-axis.
3. **Build one shared grid component, with lightweight cells, used by both
   applications.** A cell is a plain Bokeh figure showing a `RenderedPanel`
   image produced by the existing headless panel path. One toolbar serves
   the whole grid. This **supersedes July decision 2** ("each cell is a full
   `VisibilityRaster`/`VisibilityScatter`"). Your "one set of tools for all
   grid plots" framing is the right one, and the code built since July
   (`RenderedPanel`, `png_export`'s M×N grid, `headless=True` panels) makes
   it cheap (section 3).
4. **Raster or scatter does not matter to the grid.** A cell is an RGBA image
   either way, so both come at no extra cost. visplot needs both (a grid of
   per-baseline waterfalls is the SPFLG problem solved). calplot needs
   mostly scatter.
5. **Flagging stays in the full panels at first.** A cell click opens that
   cell's configuration in Panel A ("drill down"). Box-flagging directly in
   cells is a later, separable step.
6. **Is it the next most important thing?** It is the most valuable *new
   capability*, but two things compete for the slot: HRS consolidation (GUI
   refactor, astronomer testing, due spring/summer 2027) and remote
   Scenario A (a network change hangs the GUI for 600 s). My recommendation
   is in section 5: take a small benchmark slice plus the shared component
   now, the visplot Grid layout before the GUI refactor, and calplot after.
   Your call.
7. **xradio has no calibration format to read yet.** The
   `calibration_set` schema is an unmerged draft branch that defines only a
   fringe-fit dataset. calplot should start on CASA cal tables behind a
   reader interface that an xradio backend can implement later
   (section 4.3).

---

## 1. How astronomers use grids of small plots

### 1.1 The pattern

Every tool below does the same thing. **One plot definition** (same axes,
same quantity) is repeated per value of an **iteration axis**, with **N per
page** and **next/previous page**. The cells are comparable because their
axes match. The point is to spot the one antenna, baseline or SPW that
looks different from its neighbours.

| Tool | Domain | Iterates over | Page control | Notes |
|---|---|---|---|---|
| plotms | MS and cal tables | `iteraxis` (scan, field, spw, baseline, antenna, time, corr) **[doc]/[recall]** | `gridrows`, `gridcols`; `exprange` exports one file per page **[doc]** | `xselfscale`/`yselfscale` give the iterated plots a common range; `xsharedaxis`/`ysharedaxis` draw one external axis per column/row **[doc]** |
| plotcal (removed in CASA 6.2) | cal tables | antenna, time, spw, field; several at once, fixed order **[doc]** | `subplot`, Next button **[doc]** | The canonical example in its docs is one bandpass per plot, six plots per page, iterating over antennas **[doc]** |
| plotbandpass | B and Tsys tables | antenna, spw, time | `subplot` rows×cols, default 2×2 **[doc]** | `overlay` = antenna/time/spw/baseband puts several in one frame in different colours **[doc]** |
| AIPS SNPLT | SN/CL tables | antenna | `NCOUNT` plots per page **[doc]** | The standard way to inspect gain solutions **[doc]** |
| AIPS VPLOT / POSSM | visibilities | baseline | `NPLOTS` per page **[doc]** | VPLOT: one baseline per plot, many per page, many pages **[doc]** |
| difmap vplot | visibilities | baselines of one telescope | `nplots` per page; `n`/`p` keys page **[doc]** | Groups baselines by telescope so a telescope-based problem shows up, then edits **[doc]** |
| difmap corplot | self-cal corrections | telescope (one per page) | keys | Flag/unflag corrections by clicking **[doc]** |
| Miriad uvplt | visibilities | baseline | `nxy` | One baseline per sub-plot by default **[doc]** |
| ragavi-gains | CASA gain tables (Bokeh + datashader) | (overlay; antenna selection) | — | Bokeh prior art: falls back to static images above ~30,000 points because interactive HTML becomes unresponsive **[doc]** |
| CARTA channel map | image cubes | channel | start channel; up to 100 panels **[doc]** | Image domain; raster only in 5.x **[doc]** |

The VLA pipeline weblog shows cal solutions as per-antenna plots. Its
published troubleshooting guide diagnoses pointing failures and phase jumps
from those gain-table plots **[doc]**. Pipeline users therefore already
expect this view.

### 1.2 What this means for cubevis

- **Calibration tables are where grids matter most.** A cal table is
  antenna-based, with tens of antennas, so "one antenna per cell" fits on
  one to three pages (VLA 27, VLBA 10, ALMA up to ~66; HRS size not yet
  known, per the survey). This is the
  default view in every tool above, not an option.
- **For visibilities, grids are one view among several.** visplot already
  covers the "image of everything" views (TVFLG/FTFLG, antenna matrix) that
  replace much per-baseline paging. A grid adds two things the HRS survey
  identified:
  (a) SPFLG's "look at a few short and a few long baselines before flagging
  generally" — a page of waterfalls rather than Prev/Next one at a time;
  (b) VPLOT/POSSM/EDITR-style per-baseline time series and spectra, grouped
  by antenna.
- **Channel maps are out of scope for visplot.** They belong to a future
  image viewer, if cubevis ever has one beyond iclean. CARTA has had them
  since 5.0. The shared component in section 3 would serve that case
  unchanged (a cell is an image).
- **Two comparison modes matter, and both should be in calplot from the
  start.** Grid (one cell per antenna, shared scales) shows the shape of
  each solution. Overlay (all antennas in one frame, coloured) shows the
  outlier against the crowd. plotbandpass offers both for this reason.

### 1.3 Behaviours to carry over from plotms

These are the features users will look for first. All are cheap in the
design below:

- Common scale across cells by default (`selfscale`), with "each cell its
  own scale" as an option.
- External shared axes: tick labels only on the outer row/column, which
  saves a lot of space on a 4×4 page.
- Export one PNG per page, for a page range (`exprange`). `png_export`
  already composites an M×N page with a shared colorbar and legend
  **[code]**.
- Iterating over the iteration axis *within* the current selection.
  Selecting antennas 1–5 gives five cells.

---

## 2. What we have now [code, `a42cab5`]

- **Two fully configurable panels** (`VisibilityPlotter.MAX_PANELS = 2`),
  each slot holding a raster and a scatter, with gear/Tabs configuration,
  swap, presets and per-panel arguments (`panels=[...]`).
- **A headless panel path.** `VisibilityPlot(headless=True)` builds no
  figure and no comm, and renders to a `RenderedPanel`: a
  `PanelSpec` plus a `(H, W) uint32` RGBA image, the same pixels the Bokeh
  `image_rgba` glyph shows. `plotfile=` uses it for every preset.
- **`png_export.export_png(panels, nrows, ncols, ...)`** composites an M×N
  grid. It already supports shared or per-cell colorbars, a figure-level
  legend when cells agree, blank-but-framed empty cells, and per-row/column
  sizes. Its docstring already names grid mode and "out_ant00.png …
  out_ant42.png" as the use case.
- **Iteration steppers**: Field / SPW / Antenna / Baseline Prev/Next
  (`iteration_step.py`, `antenna_baseline_select.py`). A grid page is
  "N consecutive steps shown at once".
- **Calibration placeholders**: `Axis.GAIN_AMPLITUDE`, `GAIN_PHASE`,
  `DELAY`, `TSYS`, `SNR`, `OPACITY` exist with `AxisType.CALIBRATION`. The
  reader raises `NotImplementedError("… requires a CalTableReader")`. The
  reduction context carries `list_caltables()` and
  `bandpass`/`gaincal`/`fluxscale`/`applycal` parameter classes, unused so
  far.
- **The July grid design** (decisions 2–8, never built) assumed each cell
  was a full interactive panel with its own comm registrations and flag
  tool, a 3×3 default and 6×6 cap, and cross-cell sync done by broadcasting
  range changes. Its sequencing rule (benchmark one panel first, then set
  the cap) still stands.

---

## 3. Design: one shared grid component

### 3.1 Change from the July notes

**Supersede July decision 2.** Grid cells are *not*
`VisibilityRaster`/`VisibilityScatter` instances. A cell is a lightweight
Bokeh figure holding one `image_rgba` glyph (plus overlays), fed a
`RenderedPanel` from a headless panel on the Python side.

Why the July reasoning no longer holds:

| July concern | Now |
|---|---|
| "Flagging needs live per-panel selection" | Flagging moves to drill-down (3.6): a click puts the cell into Panel A, which has the full tool set. Box-flag in cells stays possible later, because one shared tool can report which cell it was used in. |
| "Hover/probe would need reinventing per cell" | One probe handler for the grid. The message carries the cell index and Python asks that cell's headless panel. That is one comm registration, not N. |
| Cost of N×M live panels unknown | Lightweight cells cost a figure and an image each. No comm, flag tool or gear tab per cell, and no 2×N×M raster+scatter pre-build. The 6×6 cap becomes a legibility choice, not a resource limit. |
| One toolbar per cell | One merged toolbar for the grid (your requirement). Bokeh's grid layouts can merge per-figure tools into a single toolbar **[recall: `gridplot(merge_tools=True)`; check against Bokeh 3.10 and our custom tools]**. |

What survives from July: pagination (decision 3); uniform axes per grid by
default (4); pre-build the pool, render only the visible page (11);
`axes_override` reserved in the cell descriptor (8); crosshair-position
linking (7); Grid as a fourth Layout option (architecture piece 3).

### 3.2 Pieces

```
            ┌─────────────── shared (new: cubevis/toolbox/_grid/ or similar) ───────────────┐
 Python     │ GridSpec      : template PanelSpec + iterate_by + values + rows×cols + scaling │
            │ GridSource    : protocol  page(i) -> list[RenderedPanel]; probe(cell, x, y)     │
            │ GridController: paging, comm handlers (page, probe, rerender, export)          │
            │ export_png    : (moved from visplot) one file per page, exprange               │
            └───────────────────────────────────────────────────────────────────────────────┘
 Browser      GridView: rows×cols lightweight figures, one merged toolbar, page nav,
              shared ranges when selfscale, external axes, cell-click event
            ┌──────────── visplot ────────────┐     ┌──────────────── calplot ────────────────┐
            │ VisGridSource: headless          │     │ CalGridSource: headless cal panels       │
            │ VisibilityRaster/Scatter per cell│     │ over a CalTableReader (CASA; xradio later)│
            └──────────────────────────────────┘     └──────────────────────────────────────────┘
```

- **`GridSource` is the only thing each application provides.** visplot's
  source turns "template panel config + iteration value" into a selection
  and renders headlessly. calplot's does the same over a cal-table reader.
- **What moves out of `toolbox/visplot/`** so calplot can use it:
  `panel_spec.py`, `png_export.py`, `tick_format.py`, `palettes.py`,
  `colormap_scaling.py`, `panel_sizes.py`, and probably `raster_grid.py`
  and the scatter shading in `data/_scatter_render.py`. This is a mechanical
  move with re-exports left behind; it is the first refactor of the plan.

### 3.3 Ranges, zoom and the one toolbar

- **Common scale (default).** All cells of a kind share one `x_range` and one
  `y_range` object, so a pan or zoom in one cell moves them all with no
  sync code. This replaces July decision 6's broadcast. Each cell gets its
  own ranges when "each cell its own scale" is chosen.
- **Re-render on zoom.** A zoomed cell shows a stretched image until the end
  of the gesture. Then one `rerender(page, viewport)` message asks Python
  for the whole page at the new range. That is one request answered with N
  images, not N concurrent requests (this closes July's "burst of N
  queries" concern by construction).
- **Toolbar contents:** pan, box-zoom, reset, hover-probe, crosshair (shared
  `Span`s, July decision 7), save-page, a "drill down" tap tool, and later
  the flag tool.

### 3.4 Data path and performance

The risk is not the browser any more. It is rendering N headless panels per
page. Measured so far: headless export is 5–8 s per preset on 3c279, and
some VLBA presets are much slower (`hrs_visplot_plan.md` section 7).

- **G0 measures this before anything is built** (July's benchmark-first
  rule, now actually runnable): time a 3×3 and a 4×4 page of per-baseline
  waterfalls and per-antenna time series through the existing headless path.
- **Expected fix: read once, split by the iteration axis.** For visibilities,
  one frame read for the page's union selection is grouped by baseline or
  antenna, and the cells are rendered from the groups. The frame cache and
  averaged-points memo already work this way within a panel.
- **Cal tables are small** (antennas × solutions × channels × pols). calplot
  can read a whole table into memory and render cells from it. A page
  should be sub-second.

### 3.5 Export

- Page → `export_png(cells, nrows, ncols, colorbar="auto", legend="auto")`.
- Task argument `plotfile=` with `{page}`/`{value}` substitution and an
  `exprange` equivalent (`"all"` or a page range). This mirrors plotms
  users' scripts.
- The GUI's save-page toolbar button uses the same call. That also closes
  July's postponed "screenshot of the whole layout" item for grids.

### 3.6 Flagging

- **v1: drill-down.** Tapping a cell loads its configuration (template plus
  iteration value) into Panel A and switches Layout to One or Side. All
  existing flagging works there unchanged. This is how difmap vplot is used
  too: survey grouped plots, find the bad telescope, then edit it.
- **v2 (later, optional): flag in cells.** One shared box tool, whose message
  carries `(cell, box)`. Python turns it into a `FlagDelta` with the cell's
  iteration value added to the selection. "Flag reaches" applies as it
  does today. This needs no per-cell comm; it is a small extension of the
  existing flag message.
- **calplot flags solutions, not visibilities.** That is a write to the cal
  table's `FLAG` column **[recall]**, which is simpler than an MS commit, but
  it needs its own `commit`/backup in the reader. FlagDB's command list,
  undo and preview should be reused; the commit target differs.

### 3.7 Grid controls (visplot)

- Layout: One / Side / Over / **Grid**.
- Grid reveals: *Iterate by* (Antenna, Baseline, SPW, Field, Scan,
  Correlation; a compound such as "Antenna, then SPW" later), Rows,
  Cols, Scale (common / per cell), page nav, page "k of n".
- **The template is the Panel A configuration**, edited with the existing
  gear tab. No new configuration UI is needed. "Plot ▶" renders page 1.
- Presets: "Waterfalls by baseline", "Time series by baseline (grouped by
  antenna)", "Spectra by baseline".

---

## 4. `calplot`

### 4.1 Separate application, shared core

**Recommendation: `calplot` is its own application,** with `iclean` and
`visplot` in the same architecture: `cubevis.setup` kernels, remote
execution, `Showable`, a `casatasks`-style wrapper. It is not a
`caltable=` mode of visplot. Reasons:

- The selection model differs: antenna, not baseline; no data column, no
  UV range, no averaging at first; parameters depend on the cal type.
- The flag target differs (section 3.6).
- `visibility_plotter.py` is 10.6k lines. plotms's own docs admit that its
  caltable mode leaves irrelevant options visible in the GUI **[doc]**,
  which is the failure to avoid.
- Remote matters as much as for visplot: cal tables live next to the MS on
  the cluster.

Shared: the grid component, `PanelSpec`/`RenderedPanel`/export, the `Axis`
enum (calibration members already exist), palettes and tick formats, the
remote framework, FlagDB's command list.

### 4.2 Scope

| Cal type | Default grid | Y quantities | X |
|---|---|---|---|
| G / T (gain) | per antenna | amp, phase, SNR | time |
| B (bandpass) | per antenna (× SPW) | amp, phase, SNR | channel / frequency |
| K (delay) | per antenna, or one plot of delay vs antenna | delay | time / antenna |
| Fringe (VLBI fringe fit) | per antenna | phase, delay, rate, SNR | time |
| Tsys | per antenna (× SPW) | Tsys | frequency / time |
| Parameterised (antpos, opac, gaincurve) | one table | the stored parameters | antenna |

Plus: reference antenna named in titles; flagged solutions shown in a
different colour or hidden; polarization overlay or ratio ("/" in plotms,
**[doc]**); overlay mode (colour by antenna/SPW/pol, using the existing
colour-by machinery); PARAMERR as error bars, which the scatter
renderer would need to learn.

**HRS relevance:** the survey lists SNPLT-style views (delay, rate, phase,
amplitude, Tsys per antenna vs time) as standard VLBI commissioning plots,
noted as out of scope "for later". Fringe and Tsys tables would be calplot's
first real users if HRS adopts it.

### 4.3 Reading cal tables

- **CASA cal tables (first backend).** A cal table is a CASA table
  **[doc]**. Its main table has, roughly, `TIME`, `FIELD_ID`,
  `SPECTRAL_WINDOW_ID`, `ANTENNA1`, `ANTENNA2` (reference antenna), `SCAN_NUMBER`,
  `CPARAM` or `FPARAM`, `PARAMERR`, `FLAG`, `SNR`, `WEIGHT`, and table
  keywords giving the type (`VisCal`, e.g. "G Jones", "B Jones") and
  parameter type **[recall: confirm column and keyword names on real tables
  before writing the reader]**. It can be read with python-casacore or
  casatools `table` into an xarray dataset shaped like xradio's draft
  (below), so the second backend is a swap.
- **xradio (second backend, later).** `casangi/xradio` `main` has no
  `calibration_set` package. Branch `545-calset-schema` (PR #578, last
  commit 2026-08-06) defines a schema with dims
  `(direction, time, antenna_name, frequency, receptor_label)`, but only one
  concrete dataset (`FringeJonesXds`: `CALPARAM_PHASE/DELAY/RATE`, errors,
  SNR). The baseline-based form is commented out as "obsolete … not ready to
  delete" **[code]**. No converter from CASA cal tables was found on that
  branch **[code]**. **Decision:** design calplot's internal dataset to
  follow that draft's dimension names, so an xradio reader is cheap when the
  schema lands. Do not wait for it.
- **MSv4 sub-datasets** (`system_calibration_xds`, `gain_curve_xds`,
  `phase_calibration_xds`) are not cal *solutions*. They are a natural
  later addition for Tsys **[doc]**. visplot's MSv4 backend does not read
  them today (no reference found at `a42cab5`) **[code]**. Note that xradio has open issues on
  `system_calibration_xds` frequency handling (#649, #685) **[doc]**.

---

## 5. Is this the next most important thing?

What is competing for the slot, from the handoffs:

| Item | Why it matters | Source |
|---|---|---|
| HRS consolidation: GUI refactor, then astronomer testing | The customer deadline is spring/summer 2027. The rule is that anything adding controls lands *before* the refactor. | `hrs_visplot_plan.md` section 8 |
| Remote Scenario A | A network change under a running session hangs the GUI for 600 s with no message. "More reliable than plotms" is a stated requirement. | `hrs_handoff_2026-11-04_sessions.md` |
| Leftover kernels, reattach, Session object | Robustness and ergonomics of the remote work | same |
| Grid (visplot) | SPFLG/VPLOT/EDITR-style survey views for commissioning. Not in CASR-385 as written. | this note |
| calplot | New capability. Covers the HRS survey's SNPLT item and replaces plotms's weakest mode. | this note |

**Recommendation.**

1. Do **Scenario A first** if HRS testing will use remote kernels on
   laptops. It is a hang with no message, the kind of failure that makes
   testers stop trusting the tool. If testing is local or on-site, it can
   wait.
2. Then **G0–G2** (benchmark, shared component, visplot Grid layout) before
   the GUI refactor. Grid adds a Layout option and controls, so under the
   consolidation rule it has to land before the refactor or wait for the
   round after.
3. **calplot (C0–C2) after**, in parallel with astronomer testing. It does
   not touch visplot's GUI, so it does not disturb what is being tested.

If calplot has a near-term stakeholder (HRS fringe/Tsys tables, a pipeline
group), swap 2 and 3. calplot alone also proves the shared component, and
visplot's grid can follow. G0–G1 are needed either way.

---

## 6. Staged plan

Sizes as in the HRS plan: S under a day, M one to three days, L one to two
weeks of sessions. Each stage has an exit test you run in the browser.

| Stage | What | Size | Exit |
|---|---|---|---|
| **G0** | Benchmark: page of 9/16 headless cells (waterfall per baseline; time series per baseline; one cal-like scatter), read-per-cell vs read-once-and-split. Record in this note. **Done 2026-11-10: section 11** (synthetic data; the cal-like scatter waits for C0; retake on real data) | S | Numbers; decide whether "read once, split" is needed in G2 or later |
| **G1a** | Move `panel_spec`, `png_export`, `tick_format`, `palettes`, `colormap_scaling`, `panel_sizes` (and shading helpers as needed) to a shared module; re-export from old paths **Done 2026-11-10** (`devel/notes/drs/gridplot/gridplot_notes.md` section 1) | S–M | Test suite unchanged; `plotfile=` output byte-identical |
| **G1b** | `GridView` (Bokeh) + `GridController` + `GridSource` protocol; a synthetic source for tests; paging; common/per-cell scale; external axes; one probe; crosshair; save-page; tap event | M–L | Demo notebook with a synthetic 4×4 source, both themes, local and remote |
| **G2** | visplot: Layout "Grid", Iterate by, Rows/Cols, template = Panel A; `VisGridSource`; drill-down tap; `plotfile` per page + range; three presets | M–L | Per-baseline waterfall grid on TW Hya and the VLBA subset; drill-down then flag works; PNG pages match the GUI |
| **G3** | visplot: read-once-and-split (if G0 says so); compound iteration; `axes_override` hook only | M | Page time inside the G0 target |
| **C0** | `CalTableReader` interface + CASA backend (G, B, K, Fringe, Tsys) into an xradio-draft-shaped dataset; tests on small generated tables | M | Round-trip of values and flags against casatools reads |
| **C1** | calplot app: selection (antenna, SPW, field, pol, time), grid per antenna, overlay mode, flagged solutions shown, refant in titles, PNG pages; remote kernels via the existing setup | L | Gain, bandpass and fringe tables from a real VLA/VLBA reduction |
| **C2** | Solution flagging: box in drill-down panel, FlagDB list/undo, commit to `FLAG` with a backup copy | M | Flag, undo, commit, reopen in plotms and see the same flags |
| G4 / C3 (optional) | Flag-in-cell with the shared tool (3.6 v2) | M | — |
| C4 (when xradio merges) | xradio calset backend | S–M | Parity with the CASA backend on a converted table |

---

## 7. Decisions (Darrell, 2026-11-10)

1. **July decision 2 is superseded.** Cells are lightweight RGBA images,
   with one toolbar for the grid and drill-down for flagging.
2. **calplot is its own application** sharing a core with visplot.
3. **Order as recommended**: Scenario A, then G0–G2, then calplot.
4. **First cal types**: the set that spans the implementation (section 9.5).
   **The stakeholder is xradio/RADPS**, so reader independence is a hard
   requirement: the xradio calibration format does not exist yet.
5. **Shared module: `cubevis/toolbox/gridplot`.** It holds the grid
   component and the plot core moved out of visplot (G1a).

Follow-up questions and answers are in section 9.

## 8. Things not established in this session

- Bokeh 3.10 merged-toolbar behaviour with cubevis's custom tools (flag,
  gear, `EvHover`) in a grid layout. G1b's first task.
- Real headless page cost (G0).
- Exact CASA cal-table column and keyword set per type, especially Fringe
  and the parameterised types.
- Whether the CASA team plans its own cal-table plotter for the PlotMS
  replacement. CASA docs say only that MS view and PlotMS are being migrated
  to a new unified MS plotting utility **[doc]**. Worth asking.

## 9. Follow-up questions (2026-11-10)

### 9.1 Playback ("tapedeck") mode

**Yes. It belongs in the plan, and it is a view of the same `GridSource`,
not a separate feature.** A grid shows values *k…k+N−1* side by side.
Playback shows value *k* in one large cell and advances *k* on a timer.
Same template, same iteration axis, same `page(i)` call with a page size of
one, and the same toolbar. The CASA viewer's tapedeck animated cube
channels. Here the useful axes are different:

| Application | Play through | Looking for |
|---|---|---|
| visplot | scan, time chunk (a new "time window" iteration axis), SPW | RFI that comes and goes; a baseline that degrades through the run |
| visplot | baseline, antenna | "flip book" survey at a size where detail is visible |
| calplot | solution time (a bandpass or Tsys spectrum per solution interval) | bandpass evolution, Tsys spikes |
| calplot | antenna | the same comparison as the grid, one at a time and larger |

Design points:

- **Scale is locked during playback.** A common scale is required, or the
  colour and Y range jump every frame and change becomes unreadable. Play
  uses the grid's common-scale limits, computed over the whole iteration
  range, not per frame.
- **Prefetch on the client.** Frames are small (a 600×400 cell is about
  1 MB RGBA, much less as PNG). The controller renders ahead by a few frames
  and the browser keeps a ring buffer, so the frame rate is set by a JS
  timer, not by round trips. This matters on remote kernels.
- **Controls**: play/pause, step ±1, loop, speed, a slider for the value,
  and "bounce" (forward then back). Space bar and arrow keys through
  `casalib.hotkeys`, scoped to the grid, as iclean does.
- **Grid + playback together.** Advancing the *page* on a timer is the
  same mechanism. It is useful for paging a 4×4 grid of waterfalls scan by
  scan.
- **Export**: one PNG per frame, through the same per-page export. An
  animated GIF/MP4 is a small step from there (matplotlib's animation
  writers) if anyone asks.
- **Staging**: add playback to G1b as a view mode. It needs no new
  Python-side machinery beyond prefetch, and it is a good early test of the
  component because it exercises paging hardest.

### 9.2 One toolbar for the whole grid: no pushback, with three conditions

It is the right decision. Three things must hold for it to work, and G1b
should prove them first:

1. **Every tool knows which cell it was used in.** Probe, tap/drill-down
   and (later) the flag box get the cell from the figure the event came
   from, and send `(cell, …)`. No tool may assume one figure.
2. **Bokeh merging must not break custom tools.** `gridplot(merge_tools=True)`
   merges tools of the same type across figures into one toolbar button
   **[recall]**. It is unverified for cubevis's `CustomAction`s and the
   flag tool. If merging misbehaves, the fallback is one toolbar of our own
   (`CustomAction`s that act on whichever cell is active, set on hover).
   That is still one toolbar from the user's side.
3. **The toolbar does not autohide.** `compact_toolbar` suits a panel.
   For a grid, the toolbar belongs outside the cells, always visible, with
   the page controls next to it.

One behaviour to settle in G1b: box-zoom with a **per-cell** scale zooms
only that cell; with a **common** scale it zooms all cells, because they
share ranges. That is the natural result of the design and matches what a
user would expect from each mode.

*Proved in the G1b prototype (2026-11-11, `devel/notes/drs/gridplot/gridplot_notes.md`
section 2): all three hold, with tools identical in every cell and the cell
taken from the event's origin; Bokeh's merge does work with cubevis's
`InfoTool` and `FlagTool` when their arguments are the same in every cell.*

### 9.3 Maximum rows and columns: yes, two limits

- **A legibility floor, not a fixed shape.** Rows and columns are refused
  if a cell's data area would fall below about 120×90 px at the current
  window size. On a typical laptop that allows about 6×6. On a large
  monitor it allows more.
- **A hard cap of 8×8 (64 cells) for the interactive grid.** Without a Bokeh
  server, every cell figure is built at construction time and shown or
  hidden. The cap is the pool size. 64 hidden lightweight figures should be
  cheap, but G1b measures construction time and page-switch time at 64
  before the number is fixed. For comparison, ALMA's 66 antennas fit in two
  pages of 6×6. CARTA caps its channel map at 100 panels **[doc]**.
- *Prototype (2026-11-11): the pool costs about 55 ms a cell at start, so
  it defaults to 6 x 6, with up to 8 x 8 as an argument; rows x cols change
  by re-assigning the grid's children (70-760 ms), never by hiding cells
  (8 s).*
- **No cap for headless export** beyond memory, as in plotms **[doc]**:
  a PNG page of 12×6 is reasonable on paper even if not on screen.

### 9.4 Features current tools lack (and users have needed)

From the tools' own documentation, gaps calplot and the grid can fill:

1. **Effective values of parameterised tables.** plotms only plots the
   stored parameters of delay, antpos, gaincurve and opacity tables, not the
   amplitudes and phases they produce at observed times and frequencies
   **[doc]**. calplot can evaluate them: show a K table as phase vs
   frequency, or a gain curve as gain vs elevation. Evaluation lives in the
   reader, so it works for any backend.
2. **Averaging on cal tables.** Unsupported in plotms for cal tables
   **[doc]**. Averaging over time or channels (vector or scalar) already
   exists in visplot's scatter path and can be reused.
3. **Comparing two tables.** plotbandpass overlays a second and third table
   **[doc]**. plotms has no equivalent. calplot can overlay *and* show the
   difference or ratio of two tables of the same type: self-cal round *n*
   against *n−1*, or one pipeline run against another. RADPS will produce
   many such iterations.
4. **VLBI tables.** CASA kept plotcal for some VLBI cases after plotms
   took over, and then removed plotcal in 6.2 **[doc]**. Fringe-fit tables
   (phase, delay, rate) are a gap. They are also xradio's only concrete
   calibration schema so far.
5. **Interactivity at volume.** ragavi falls back to static images above
   about 30,000 points **[doc]**. datashader-shaded cells do not have that
   limit.
6. **A GUI that only shows what applies.** plotms leaves options that are
   irrelevant to cal tables visible in its GUI **[doc]**. A separate app
   avoids this.

New ideas of mine, which none of the tools surveyed offers. They are cheap
because every cell already has its data on the Python side:

7. **Order cells by a statistic.** "Worst first": sort the iteration values
   by phase RMS, flag fraction, SNR or Z-Score instead of by antenna number,
   so page 1 shows the problems. One metric per cell plus a sort key.
8. **Cell badges and outlier outlines.** A small per-cell number (rms, %
   flagged, median SNR) in the corner, and a coloured outline on cells
   whose value is far from the median of the page. It is the small-
   multiples version of the TVFLG/LISTR "find the bad antenna" matrix.
9. **Links between calplot and visplot.** From a solution, open visplot on
   the visibilities it was solved from (same antenna, time interval, SPW,
   field). Flagged solutions could be offered as visibility flags (what
   `applycal` does with flagged solutions **[recall]**) and previewed in
   visplot before writing.

Proposed placement: 7 and 8 with G2 (and in calplot C1); 1 and 4 in C0/C1;
3 as C3; 2 and 9 later.

### 9.5 First calibration types, chosen to span the implementation

| Type | What it exercises |
|---|---|
| **G** (complex gain) | complex parameters; time axis; per-antenna grid; reference antenna |
| **B** (bandpass) | complex parameters; frequency axis; × SPW iteration; solution-time playback |
| **Fringe** (phase, delay, rate) | several float parameters per solution; matches xradio's `FringeJonesXds`, the only concrete schema to test against |
| **K** (delay) | float, parameterised; effective-value evaluation (9.4 item 1) |
| **Tsys** | float; time *and* frequency; later a second source (MSv4 `system_calibration_xds`) for the same view |

Between them these cover complex/float parameters, time and frequency axes,
single- and multi-parameter solutions, parameterised evaluation, and two
sources for the same quantity. Polarization (D-terms, X-Y phase) and
baseline-based tables come later.

### 9.6 Reader independence (xradio/RADPS)

The reader interface is the contract, and nothing above it sees a backend:

- `CalTableReader` (abstract): `open(path)`, `info()` (type, parameters,
  units, reference antenna, antennas, SPWs, fields, times),
  `read(selection) -> xarray.Dataset`, `evaluate(selection, at=...)` for
  parameterised types, and `flags`/`commit_flags`/`backup`.
- **The internal dataset follows the xradio draft's names**:
  dims `(time, antenna_name, frequency, receptor_label)`, data variables
  `CALPARAM_*`, `CALERROR_*`, `SNR`, `FLAG`, and attrs `type`,
  `reference_antenna`. Each backend converts to that. When the schema
  merges, an xradio backend is mostly a rename-free open.
- **Backends**: `casa_caltable` first (C0); `xradio_calset` when the schema
  lands (C4); a **synthetic** backend from day one, built on the draft
  schema, for tests and for exercising cases no real table gives us yet.
- **Detection** goes in `data_format.py` beside MS/PS detection, which
  already reads a CASA table's `table.info` `Type =` line **[code]**
  (cal tables carry their own type there **[recall]**).
- **Remote**: the reader runs in the kernel, like visplot's backends, so
  calplot inherits the remote framework without changes to the reader.
- Because the format is unsettled, keep a short "schema assumptions" list
  in the calplot notes and check it against PR #578 at each stage.

### 9.7 Channel maps

Agreed and recorded: no image-domain grids unless a cubevis application
needs one for its own workflow, as iclean does. The grid component would
support it unchanged if that ever happens.

### 9.8 Plan changes from this section

- G1a: the shared module is `cubevis/toolbox/gridplot`.
- G1b: adds the playback view and client prefetch; first proves the three
  toolbar conditions (9.2); measures a 64-cell pool (9.3).
- G2: adds the legibility floor and the 8×8 cap; cell badges, outliers and
  sort-by-statistic (9.4 items 7–8).
- C0: abstract `CalTableReader`, CASA and synthetic backends, the xradio
  draft's dataset names; G, B, Fringe, K, Tsys; effective values for K.
- C3 (new): two-table overlay, difference and ratio.

## 10. Reading CASA cal tables with arcae, as an xarray backend (2026-11-10)

Darrell's suggestion: read CASA cal tables with `arcae`, and make the
reader an xarray backend (working name `xarray-casa-caltab`), the cal-table
analogue of xarray-ms. It sits alongside the arcae-based xarray store for
CASA images that the iclean imaging layer already plans
(`iclean_imaging_layer_design.md` section 2.3).

### 10.1 Why this fits [code]

- **No new dependency.** `cubevis[visplot]` already requires xarray-ms,
  which requires `arcae>=0.5.4,<0.6.0` (`pyproject.toml`). visplot's
  MSv2 backend is arcae-only, and calplot can be too: no casatools in the
  kernel.
- **Flag commits are already solved.** `flag_commit.commit_msv2_arcae`
  writes the exact final flag state with arcae, after saving the old values
  and reading them back to verify. It replaced `flagdata` on 2026-09-30
  because `flagdata` writes were inexact. A cal table's `FLAG` column is
  the same problem, smaller.
- **arcae's API covers what a cal table needs** (arcae 0.5.8, 2026-10-20):
  `getcol`/`putcol` with numpy-style indexing, `getkeywords` and
  `getcolkeywords` (the cal type and parameter type are table keywords
  **[recall]**), `tabledesc`, `getcoldesc`, `nrow`, and subtables opened by
  path. Writes need `ninstances=1`. Reads can use several instances.
- **Reader independence becomes structural.** If the CASA reader is an
  xarray backend producing xradio-draft-shaped datasets, then *every*
  backend's contract is "open → `xarray.DataTree` in that shape". calplot
  above the reader never sees casacore. A future xradio calset is an
  `xr.open_datatree(...)` on Zarr, so most of C4 disappears.
- **It is useful outside cubevis.** RADPS needs a CASA → calset path, and
  the xradio branch has no converter **[code]**. An xarray backend gives
  that almost for free (`open_datatree(...).to_zarr(...)`). It is the
  natural thing to offer the xradio team, the way xarray-ms serves MSv2.

### 10.2 Shape

```python
dt = xr.open_datatree("cal.G", engine="casa-caltab")   # entry point, like xarray-ms
dt["spw_0"]                                            # one node per SPW (frequency axis per SPW)
#  dims:   time, antenna_name, frequency, receptor_label   (xradio draft names)
#  vars:   CALPARAM_<name>, CALERROR_<name>, SNR, FLAG
#  coords: field_name(time), scan_name(time)
#  attrs:  type ("antenna_calibration"), caltype ("G", "B", "K", "Fringe", "Tsys"),
#          reference_antenna, source table path, schema assumptions version
```

- **Parameter splitting is the backend's job.** `CPARAM` becomes one complex
  `CALPARAM_GAIN`. A fringe table's packed `FPARAM` becomes
  `CALPARAM_PHASE`/`_DELAY`/`_RATE`, matching `FringeJonesXds`. The
  packing layout per type is **[recall]** and is C0's first check against
  real tables.
- **Each value's source row** is kept as a hidden integer variable, so a
  flag commit can map `(time, antenna, spw)` back to table rows without
  searching again.
- **Lazy only where it pays.** Most tables fit in memory, so the backend
  reads eagerly by default. It also provides a lazy `BackendArray` for
  large B and Tsys tables (many channels × antennas × solutions).
- **Read-only, like all xarray backends.** Writing is a separate
  `commit_flags(path, deltas)` beside it, reusing `commit_msv2_arcae`'s
  backup and verify steps.

### 10.3 Where it lives

Follow the `cubevis.imaging` rule: no Bokeh and no CASA at import, so a
remote worker, RADPS or a test can use it without the GUI.

```
cubevis/xarray/                  arcae-based xarray backends (no Bokeh, no casatools)
    _arcae_util.py               table.info type, keyword JSON, subtable open (shared)
    caltab/                      engine "casa-caltab": backend, per-type parameter maps, commit_flags
    (casaimage/ later)           engine "casa-image": the store iclean's ImageStore section plans
cubevis/toolbox/gridplot/        the shared grid component
cubevis/toolbox/calplot/         the application
```

The entry point is registered under `[project.entry-points."xarray.backends"]`
in `pyproject.toml`. It stays inside cubevis until the schema and the
backend settle. Splitting it into its own package (`xarray-casa-caltab`)
is then a move, not a rewrite. When to split is a question for the
xradio/RADPS team: they may prefer it inside xradio as their converter.

The existing `cubevis/data/casaimage/` (image utilities from 2023–24) is
unrelated. The new name avoids confusion with it.

### 10.4 Plan changes

- **C0 becomes:** the `casa-caltab` xarray backend on arcae (G, B, Fringe,
  K, Tsys), the synthetic backend built on the same shape, `commit_flags`
  with backup and verify, and a parity test against casatools reads where
  casatools is available. The `CalTableReader` of 9.6 shrinks to
  selection, effective-value evaluation, and the flag-commit hook over a
  `DataTree`.
- **C4 becomes:** open xradio calset Zarr with the same code path, once
  PR #578 merges, plus a converter check (`casa-caltab` → Zarr → reopen →
  identical).
- **New, optional, later:** the `casa-image` backend for iclean's
  `ImageStore`, sharing `_arcae_util.py`. It is not part of this plan's
  stages, but building `caltab` first leaves the pattern in place.

### 10.5 To verify in C0

- Cal-table keyword names and the `FPARAM`/`CPARAM` packing per type, on
  real VLA, VLBA and ALMA tables.
- How arcae returns complex and variably shaped columns (its README: complex
  is a nested pair of floats; `getcol` reinterprets to numpy
  transparently) and `TpRecord` keywords, which arcae does not handle yet.
- Lock behaviour when calplot holds a table open while CASA writes a new
  version of it (RADPS iteration): `lockoptions`.

## 11. G0 results: what a page costs today (2026-11-10, base `089df90`)

`devel/tools/gridplot_bench/bench_g0.py`.  One headless `VisibilityPlotter`
holds the open data, as a `GridSource` will; each cell is the same panel
re-selected to one baseline and rendered to a `RenderedPanel`; the page is
composited with `export_png`.  **Data**: a synthetic MSv2 from the quick
tests' generator at VLBA size (10 antennas, 45 baselines, 1 SPW of 64
channels, 600 times, 2 correlations; `--make`).  **Machine**: the cloud
sandbox, 2 cores, 8 GB.  Not real data: the numbers that decide G2 should
be retaken on TW Hya and the VLBA TL016B subset (`--ms`).

| View | Page | All four panels re-rendered (today's `plotfile=` path) | Shown panel only | Page's baselines in one render | Compose (PNG) |
|---|---|---|---|---|---|
| Waterfall (Time × Channel raster) | 3×3 | 15.1 s (1.7 s/cell) | **2.1 s** (0.23 s/cell) | 0.24 s | 0.8 s |
| | 4×4 | 30.4 s | **3.6 s** (0.22 s/cell) | 0.22 s | 1.0 s |
| Time series (Amplitude vs Time scatter) | 3×3 | 26.4 s (3.0 s/cell) | **15.3 s** (1.8 s/cell) | 1.4 s | 0.5 s |
| | 4×4 | 44.7 s | **27.9 s** (1.7 s/cell) | 1.8 s | 1.0 s |

A second pass over the same page costs the same as the first: the cost is
per query, not the data read.

**Findings**

1. **Re-selecting re-rendered all four panel objects** (both kinds in both
   slots, shown or not): in the waterfall run, 5.0 s of 7.2 s went to
   hidden scatters.  Fixed in this slice for headless export:
   `_apply_selection_overrides(..., panels=)` re-queries only what the file
   shows and marks the rest stale; `_export_png` brings a stale panel up to
   date when a later call shows it (checked identical to a full refresh).
   Nine `plotfile=` calls of the waterfall preset (Side layout, two panels
   drawn): 23.3 s → 17.3 s.  A grid source must likewise re-render only
   its template panel.
2. **Raster grids are workable through today's path**: 0.22 s a cell,
   a 4×4 page in under 4 s plus a second to compose.  With playback's
   prefetch (9.1) that is usable for G2 as it is.
3. **Scatter grids are not**: 1.7 s a cell, 28 s for 4×4, while the same
   16 baselines in *one* render take 1.8 s.  The cost is the scatter
   pipeline per query (column query, binning, histogram equalisation), not
   the bytes.  **Read once, split by the iteration axis is required before
   scatter grids ship** -- G3's first item moves ahead of G2's scatter
   presets.  Rasters can ship first.
4. **Composing is not the bottleneck**: under a second for 16 cells,
   though it grows with the page's pixel size (4754×1513 px for a 4×4
   waterfall page).
5. **Not yet measured**: remote kernels (each cell a round trip, plus the
   images on the wire: about 1 MB RGBA a cell), and real data sizes.

**Plan changes**

- G2 ships raster grids (waterfall, Time × Baseline per antenna, antenna
  matrix) first; scatter grids wait for read-once-and-split.
- G3's "read once, split" is required, and first in G3.
- The grid source re-renders its template panel only: no hidden panels.

## Sources

- plotms task reference (iteraxis, gridrows/gridcols, xselfscale,
  xsharedaxis, exprange, caltable support):
  https://casadocs.readthedocs.io/en/latest/api/tt/casaplotms.plotms.html ;
  https://casadocs.readthedocs.io/en/v4.7-v6.1/tasks540/plotms.xml.html
- plotcal (deprecated; iteration, subplot):
  https://casadocs.readthedocs.io/en/v6.2.0/api/tt/casatasks.visualization.plotcal.html
- plotbandpass (subplot, overlay):
  https://casadocs.readthedocs.io/en/latest/api/tt/casatasks.visualization.plotbandpass.html
- AIPS CookBook 6.3 (VPLOT) and 9.4 (POSSM NPLOTS):
  https://www.aips.nrao.edu/CookHTML/CookBookse35.html ;
  https://www.aips.nrao.edu/CookHTML/CookBookse58.html
- SNPLT usage (ATCA AIPS guide):
  https://www.atnf.csiro.au/computing/software/atca_aips/node41.html
- difmap vplot, corplot, editing help:
  https://ftp.eso.org/scisoft/scisoft4/sources/difmap/uvf_difmap/help/vplot.hlp ;
  https://ftp.eso.org/scisoft/scisoft4/sources/difmap/uvf_difmap/help/corplot.hlp ;
  https://ftp.eso.org/scisoft/scisoft4/sources/difmap/uvf_difmap/help/editing.hlp
- Miriad uvplt: https://astro.uni-bonn.de/~sysstw/lfa_html/miriad/node54.html
- ragavi-gains (Bokeh/datashader prior art):
  https://ragavi.readthedocs.io/en/dev/gains.html ; https://github.com/ratt-ru/ragavi
- VLA pipeline problems from gain-table plots:
  https://casaguides.nrao.edu/index.php/Pipeline:_Frequent_VLA_problems
- CARTA channel map view: https://carta.readthedocs.io/en/5.1/channel_map_control.html ;
  https://carta.readthedocs.io/en/dev/appendix_a_version_history.html
- CASA on the PlotMS/msview migration: https://casadocs.readthedocs.io/en/v6.7.3/notebooks/carta.html
- arcae (README, API, HISTORY at 0.5.8): https://github.com/ratt-ru/arcae
- xradio calibration schema draft: https://github.com/casangi/xradio/pull/578
  (branch `545-calset-schema`); MSv4 sub-datasets:
  https://xradio.readthedocs.io/en/stable/measurement_set/overview.html
