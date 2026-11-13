# gridplot: session notes

Development notes for the shared grid component (`cubevis/toolbox/gridplot`)
and calplot, one section per slice, as in `devel/notes/drs/visplot`.  The
design is `devel/docs/gridplot/grid_and_calplot_plan.md` ("the plan"); G0
(the benchmark) is its section 11.

## 1. G1a: the shared plot core moves to `cubevis/toolbox/gridplot` (base `27ab2b74`, 2026-11-10)

The plan's section 3.2: what calplot and the grid view need, out of visplot.
Branch `gridplot-g1a`.

**Moved** (`git mv`, so history follows; content unchanged but for each
docstring's "Package location" line):

| module | what it is |
|---|---|
| `panel_spec` | `PanelSpec`, `RenderedPanel`, `ColorBand`: a panel as data, and its RGBA image -- a grid cell |
| `png_export` | `export_png`: panels composited into one PNG, M x N |
| `tick_format` | tick labels, units, UTC times, the Bokeh tick formatter's JS (as text) |
| `palettes` | palettes and backgrounds per theme |
| `colormap_scaling` | colour scaling (equalised, threshold, statistical) |
| `panel_sizes` | size arguments, cell sizes |
| `raster_grid` | where a raster's cells are |

They import only each other, the standard library, numpy and matplotlib:
importing all seven loads neither Bokeh, visplot nor CASA (checked with a
clean interpreter).  `gridplot/__init__.py` imports nothing.

**Not moved**: the scatter shading (`data/_scatter_render.py`) -- it is
tied to the data backends' column queries; G1b moves what the grid view
needs once that is known, rather than guessing now.

**Old names stay**: each old module (`cubevis/toolbox/visplot/png_export.py`
and the six others) is a three-line alias that replaces itself in
`sys.modules` with the moved module, so `from cubevis.toolbox.visplot import
png_export`, `from cubevis.toolbox.visplot.panel_spec import RenderedPanel`
and a test's `monkeypatch.setattr(visplot.png_export, ...)` all reach the
same objects.  cubevis's own imports now name `gridplot` (36 lines in nine
visplot modules); tests and demos still use the old names, which is what
keeps the aliases exercised.

**Checks**

* `plotfile=` output byte-identical: all ten presets, both themes, on the
  quick tests' MS and a VLBA-sized synthetic one (40 PNGs, sha256 equal;
  generation checked deterministic first).
* The visplot tests that import these modules (19 files, 1270 tests), on
  the base (a worktree at `27ab2b74`) and on the branch: the same result,
  1154 passed, 86 failed and 12 errors, all the failures and errors tests
  that need the TW Hya data.  Note `test_raster_grid.py` needs
  `CUBEVIS_SRC` (the package directory) or it stops collection, which also
  stops every other file in the same pytest run.
* Quick suites: all pass (iclean-parity skipped: no CASA here).
* The wheel (pdm-backend) carries `cubevis/toolbox/gridplot/`, so setup's
  `pip install "cubevis[visplot]==VERSION"` on a remote host gets it.

**Fingerprint**: `k2-3fe9faaae4688b790820` -> `k2-c8c0a0db7db6ad63a548`.  The
kernel side's closure now names `gridplot.colormap_scaling`, `panel_sizes`,
`raster_grid` and `tick_format` (what the data backends use);
`png_export` and `panel_spec` stay on the client's side.  setup makes new
environments.

**Next: G1b** -- first the toolbar prototype (the plan's 9.2: each tool knows
its cell; Bokeh's merged toolbar with cubevis's custom tools; the toolbar
outside the cells) and a 64-cell pool's construction and page-switch time,
before the rest of the grid view is built on it.

## 2. G1b prototype: one toolbar for a grid of image cells (base `b1b8009a`, 2026-11-11)

The plan's 9.2 (one toolbar: three conditions) and 9.3 (the pool), proved
before the grid view is built on them.
`devel/tools/gridplot_proto/proto_g1b.py`: a static page (no kernel: what is
in question is the browser's side) with a pool of 8 x 8 lightweight cells,
each a figure with one `image_rgba` glyph, put together with
`gridplot(merge_tools=True)` and driven in headless Chrome by Playwright
(`--chrome PATH`; here Chrome for Testing 153 at `/usr/bin/google-chrome`;
playwright's own browser download is not reachable from the sandbox).
Bokeh 3.10.

**Results**

| question | answer |
|---|---|
| one toolbar? | **Yes.** Ten tools x 64 cells became ten buttons above the grid (`ToolProxy` of 64 each): pan, wheel zoom, box zoom, reset, tap, hover, crosshair, and cubevis's own `InfoTool` and `FlagTool` (flag and unflag, two buttons because `flag` differs). |
| what that needs | Bokeh merges tools of one class whose properties are equal (`bokeh.layouts.group_tools`; `overlay` and `renderers` ignored).  **A tool with a per-cell argument does not merge** -- a `CustomJS` with the cell's index in its args, or `FlagTool`/`InfoTool` as visplot builds them today (each panel's `comm`, `image_source`, `state_source`) would give 64 buttons. |
| each tool knows its cell? | **Yes, from the event**: `js_on_event` per figure with one shared `CustomJS`, the cell from `cb_obj.origin.tags[0]`.  Real mouse: moves in cells 0, 9, 27, 63 and taps in 5, 42 each reported the right cell. |
| common scale | Cells sharing `x_range`/`y_range`: a box zoom in cell 12 moved all 64 (one range, 64 `rangesupdate` events).  No sync code. |
| one crosshair | **Yes**: one `CrosshairTool(overlay=[Span, Span])` instance added to every cell draws in all of them (checked on plain images; on noise images a thin line is invisible -- use a visible colour/width). |
| page switch (new RGBA in every cell, to paint) | 9 cells 55 ms, 16: 80 ms, 36: 200 ms, 64: 320 ms (writing the data: 2-11 ms; the rest is Bokeh's repaint).  The browser is not where a page's time goes (G0: Python is). |
| rows x cols in the browser, no server | **Re-assigning the grid's children** (`grid.children = [[fig, r, c], ...]`): 3x3 73 ms, 4x4 139 ms, 6x6 390 ms, 8x8 762 ms.  **Not** by hiding cells (`visible = false` on 55 figures: 8.4 s, a re-layout per figure). |
| the pool's cost | about 55 ms a cell in Python (37 ms building, 17 ms serialising), with 1x1 placeholder images: 16 cells 0.9 s / 1.0 MB of page, 36 cells 1.9 s / 2.2 MB, 64 cells 3.5 s / 3.8 MB. |

**Decisions this gives G1b**

1. **Tools are identical in every cell; the cell comes from the event.**
   Grid-level tool arguments only (one comm, one message id).  Events:
   `js_on_event` on each figure with one shared callback, cell =
   `origin.tags[0]` (or a per-figure lookup table).
2. **Flag-in-cell (G4) needs a grid variant of `FlagTool`** that reads its
   cell's sources from its own plot view (each figure still has its own
   tool instance inside the proxy, so `this.plot_view` is the cell) instead
   of per-panel arguments.  A TypeScript change; not in G1b (drill-down
   first, as planned).
3. **Rows x cols changes re-assign the grid's children**; a pool figure is
   never hidden.
4. **The pool defaults to 6 x 6** (about 2 s at start), with up to 8 x 8 as
   an argument (`grid_max=`), not 8 x 8 for everyone: the plan's 9.3 cap
   stays, the default pool is smaller.  Cells start with 1x1 placeholders.
5. **Outer axes need equal frames**: hiding inner cells' axes made the
   first column's frames narrower than the others' (visible in
   `pool_8x8.png`).  *Settled 2026-11-11*: cells are sized by
   `frame_width`/`frame_height`, which keeps the plot areas equal whichever
   axes show; X axis across the top of the top row, Y axis down the left
   of the first column, each axis name once (the plan's 1.3).

**Running it** (2026-11-11, after Darrell's first try): `--open` writes the page and
opens it in the default browser, no playwright -- hover and tap in cells,
use the toolbar, and press **Measure**: the page times page switches (9-64
cells) and rows x cols changes itself and shows them.  Without `--open`
playwright drives it unattended; `--chrome` takes an executable or a
macOS `.app` bundle (the program inside it), and without `--chrome` the
installed Google Chrome is used.  The page's own rows x cols times can read
lower (34-83 ms in one run) than the unattended run's: Bokeh's re-layout
may finish after the two animation frames it waits for.  Plan with the
table's.

**Not established here**: the comm path (images over the pipe, local and
remote) -- the next step builds `GridView` on the real comm; the
`InfoTool`'s own region probe in a merged proxy (it merged; its action was
not exercised).

## 3. G1b: the grid view (base `bdb2ec76`, 2026-11-11)

The prototype's toolbar design, built on the real comm path.  Three new
modules in `cubevis/toolbox/gridplot`, one export change, a browser check
and a demo notebook.  Nothing in visplot changes yet (that is G2).

**`grid_source.py`** -- the `GridSource` protocol a grid draws from:
`labels()`, `axis_names()`, `extent()`, `render(indices, viewport, size)`
-> `RenderedPanel`s, `probe(index, x, y)` -> one line of text.  visplot (G2)
and calplot (C1) each implement it; the view knows nothing else about
the data.  `SyntheticGridSource(n, kind="raster"|"scatter", cost=0.0)` is
the test source: every baseline of enough antennas for *n* cells, every
7th cell the odd one out, *cost* seconds of simulated render per cell.

**`grid_view.py`** -- `GridView(source, comm_mgr, rows, cols, pool, cell,
theme, on_drill)`.  A pool of figures (default 6 x 6, at most 8 x 8) built
once; each cell an `image_rgba` with its own source; one merged toolbar
(Pan, WheelZoom, BoxZoom, Reset, Tap, Hover for the probe, one shared
Crosshair).  Controls: page ⏮ ◀ ▶ ⏭, Rows, Cols, Scale (common / per
cell), Play with pages/s and Loop, Save PNG; a status line ("drawn in N
ms") and a probe line.  Two pipes: page/drill/save (in order) and probe
(squashed, so a fast mouse sends only the latest position).

- *Paging*: Python renders a page's cells and returns RGBA images; the
  browser caches pages by (rows, cols, scale, viewport) and prefetches the
  next two.  Responses for a superseded request are dropped.
- *Scale*: common -- a zoom in any cell sets one viewport for all, and the
  page is re-rendered at it; per cell -- only that cell re-renders.  Reset
  returns to the full extent.
- *Axes*: common -- X on the top row, Y on the first column, each name once
  (a Div above the grid, a rotated one beside it); per cell -- every cell
  has both.  Cells are sized by `frame_width`/`frame_height`, so plot areas
  stay equal either way.
- *Playback*: steps pages at the chosen rate; a tick is skipped while the
  previous page is still loading, so a slow source slows playback instead
  of losing pages.
- *Tap* drills: `on_drill(index, label)` is called in Python.  Flagging
  stays drill-down (decision 2 of section 2).
- *Save PNG*: the page as Python renders it (`png_export`), used rows only,
  downloaded by the browser.

**`grid_app.py`** -- `GridApp(source, title, **view_kw)`: the grid on its
own page, with the same protocol as visplot and iclean (`cubevis.exe`,
`show()` in a notebook).  calplot's application starts from it.

**`png_export.export_png(..., axes="each"|"outer")`** -- `"outer"` draws a
common-scale page the way the view does: X tick labels across the top of
the top row, Y down the first column, each axis name once, narrow gaps.
The grid's Save passes `"outer"` when the scale is common.  The default
(`"each"`) is unchanged: the 40 reference PNGs (both MSs, every preset,
both themes) are byte-identical to `bdb2ec76`'s.

**Checks**

- `tests/manual/gridplot/grid_browser_check.py`: serves a `GridApp` and
  drives it in Chrome (Playwright) with a real mouse -- first page, axes,
  paging, last page, reshape, probe, tap/drill, zoom common, axes per
  cell, zoom per cell, playback, save, console errors.  13 of 13 pass for
  raster and for scatter with `--cost 0.05`.  `--chrome` as in section 2.
- `tests/manual/gridplot/grid_demo.ipynb`: a 4 x 4 raster grid (light) and
  a 3 x 3 scatter grid (dark, slow source).  Executes without error under
  nbconvert; the interaction itself is for a person to try.
- `python tests/ci/run.py`: 8 of 8.

**Not established here**: a remote kernel (a synthetic source has no
remote meaning; G2 tests remote with visplot's real data), and page times
for real data (G0's numbers stand).

**Next: G2** -- visplot's Grid layout as a `GridSource` over its plot core:
raster presets first (waterfall, Time x Baseline per antenna, antenna
matrix); scatter grids after G3's read-once-and-split.

## 4. G2: visplot's Grid layout (base `fe6c0b0d`, 2026-11-11)

The grid view of section 3 inside visplot: Layout **Grid**, a fourth
button beside One / Side by Side / Over / Under, draws the first panel once
per baseline, antenna, SPW, field, scan or correlation of the selection.
Raster panels only (G0: a scatter cell costs ~1.7 s; scatter grids wait for
G3's read-once-and-split).

**`visplot/vis_grid_source.py`** -- `VisGridSource`, visplot's `GridSource`.
`configure(template, iterate_by, selection)` copies the template panel
(Panel A -- the panel shown first) and the plotter's selection, and lists
the iteration values *within* that selection (plan 1.3): baselines with
rows; antennas (each one's baselines); the selection's SPWs, fields,
scans, correlations.  Each cell is its own headless `VisibilityRaster`,
built as the template is (axes, quantity, correlation, averaging, scaling,
colormap, theme) and queried when first drawn; up to 128 are kept, so a
zoom re-shades (~10 ms) and the probe reads the cell under the cursor, with
the panels' own probe text.  The common extent is the template's (no extra
query), corrected per axis where a cell's axis differs: a Baseline axis the
grid splits (the cell with most baselines decides), or an axis of another
kind -- a one-SPW cell draws Channel where the template over two SPWs draws
Frequency (found by the tests: every cell was blank).

**In the GUI** (`visibility_plotter.py`):

- *Plot in Grid* updates only the template panel, then configures the
  source; the browser draws page 1.  The panels' layout (`_layout`) is
  kept: Grid is a mode (`_grid_on`), so a drill-down has One to go to.
- *Choosing Grid draws it*: the first time Grid is shown it plots, as Plot
  in Grid would (Darrell's first try, TW Hya: Grid showed empty cells until
  Plot was pressed).  Later switches show the page last drawn.
- *Cells fit the window* (Darrell, TW Hya): the grid measures the room its
  area has in the browser (`GridView.fit_to`) at every rows x cols, when
  shown and when the window is resized -- a 1 x 1 page fills it.
- *The cursor readout sits above the grid*, a fixed two lines: below it,
  it moved with the grid and was drawn over the cells after a reshape
  (Bokeh had not caught up with the grid's new height).
- *Iterate by* sits in the grid's control row; changing it plots again.
- *Tap* drills down: the sidebar is set to the cell's selection (as the
  Prev / Next steppers set it), Layout to One, and Plot runs.  Back to
  Grid restores the sidebar to the grid's own selection, so the next Plot
  iterates over that, not the one cell.
- *Export PNG* in Grid is the grid's Save PNG.
- *Presets in Grid keep Grid* (as if Pin layout were ticked): the preset
  sets the panel the grid repeats -- vplot, Waterfall, Antenna x antenna
  are the plan's grid presets.  A two-scatter preset (Spectrum, Time
  series) is refused with the raster-only message.
- *An axis whose cells name their positions differently* (a Baseline axis
  in a grid by antenna: each antenna's own baselines) is labelled on every
  cell, in the browser as in `png_export` -- else the first column's
  labels would stand for every column.
- *Light / Dark*: the grid's chrome at once, its cells redrawn in the new
  palette once Python has it (kept cells re-coloured, not re-queried).
- *Cells fill an area* (1020 x 640 px) at every rows x cols, with the
  panels' tick labels (UTC times, SI units, names along a Baseline axis:
  each cell carries its `PanelSpec` axis state for `TICK_FORMATTER_JS`).
- *Plot sends the grid's rows x cols*, so the plot summary's reproducing
  call has `layout="grid"`, `iterate_by`, `gridrows`, `gridcols`.

**Three Bokeh / cubevis traps, found in the browser:**

1. *The grid inside visplot's plot area was never laid out*: its cells'
   frames stayed 0 x 0 (white boxes).  Bisecting visplot's layout: any
   column around the body row (sidebar + plot area, both stretch) with
   the grid inside the plot area breaks it; beside the plot area it is
   laid out normally.  So the grid has its own area in the body row and
   `window.__cvGridLayout(on)` swaps the two.
2. *A hidden grid still costs at page load*: the browser builds views for
   hidden layouts -- 1.4 s more for every visplot page (4.8 -> 6.3 s).
   The grid area stays empty until Grid is first chosen (the models are in
   the document through `__cvGridLayout`'s arguments); page load is
   unchanged (4.5 s here).
3. *A message sent while the websocket is still opening kills the
   session*: cubevis queues it and reconnects, which aborts the opening
   connection; Python takes the aborted handshake as fatal and shuts down
   (`MSv2Backend is not open`).  Seen when visplot starts in Grid (page 1
   is requested at load).  GridView now waits for
   `pipe._mgr.transport.isConnected()` before its first page.  **The
   transport itself should not do this** -- a cubevis fix (queue without
   reconnecting while the first connect is under way) is worth making
   separately; anything else that sends at load will hit it.

Also: cells built with an empty title had no room for one (titles never
showed, also in G1b's GridApp); they start with a no-break space.

**`plotfile=`** -- `VisibilityPlotter(layout="grid", iterate_by=...,
gridrows=, gridcols=, exprange=, plotfile=...)` writes pages with
`png_export(axes="outer")`: `exprange` is `"current"` (page 1, as plotms),
`"all"` or page numbers (`"1-3,5"`); several pages get `_page01`, ... before
the extension unless the name has `{page}` (and `{value}`, the page's
first label).  `plotter(exprange=...)` per call.  Cell size: 300 x 200 px, or
`column_width` / `row_height` when they are numbers.  The task layers carry
the four new arguments (`scripts/sync_layers`).

**`png_export(axes="outer")` follows the labels**: margins sized from the
tick labels' text (long upright antenna names, UTC times), the names
beyond them; an axis whose cells name their positions differently (a
Baseline axis in a grid by antenna) gets tick labels on every cell; fewer
UTC ticks in a narrow cell.  `axes="each"` (every other export) is
unchanged: the 40 reference PNGs are byte-identical to `fe6c0b0d`'s.

**Fixed on the way:**

- *G1a's seven alias modules were never committed* (`visplot/png_export.py`,
  `panel_spec.py`, `tick_format.py`, `palettes.py`, `colormap_scaling.py`,
  `panel_sizes.py`, `raster_grid.py`: not in `b1b8009a`, `bdb2ec76` or
  `fe6c0b0d`).  Every old import (20 test files, and anyone's scripts)
  failed.  Recreated as section 1 describes.  `tests/manual/visplot`: 80
  failed + 105 errors -> 3 failed + 36 errors, 2035 passed; the rest pass
  with `CUBEVIS_SRC` set (they need it).
- *The CASA task template was behind the committed layer*: `6294aad0`
  (casatasks optional) edited `private/casatasks/visplot.py` by hand, so
  regenerating put the CASA logging import back (`test_casa_optional`
  failed).  The template now writes what was committed; `--check` is
  clean at `fe6c0b0d` and here.
- `test_antenna_iteration` reads `_handle_plot`'s source: it now reads
  `_handle_plot_panels` too (the panels' Plot moved there).

**Checks**

- `tests/manual/gridplot/test_vis_grid.py` (16, ~30 s, tiny MS): arguments,
  every iteration axis, common extent, drawing, zoom, probe, theme,
  scatter refused, `plotfile` pages, GUI models, Plot in Grid, summary.
- `tests/manual/gridplot/visplot_grid_check.py --ms DATA.ms [--start grid]
  [--kernel python3]`: 16 browser checks (load, Grid, Plot, axis names,
  ticks, Iterate by, probe, zoom, drill-down, back to Grid, theme, preset,
  readout, fit, export, console).  16 of 16: starting in Over, starting in Grid, and
  through a remote kernel (`python3` on this host).
- Remote headless: 9 cells through the kernel are pixel-identical to
  local (3.0 s vs 2.7 s).
- `grid_browser_check.py` (G1b) 13 of 13, raster and scatter; demo
  notebook runs; `tests/ci/run.py` 8 of 8.

**Not established here**: TW Hya and the VLBA subset (the exit test's data);
flagging after a drill-down in the browser (it is the ordinary panel path,
untouched); a real remote host.

**Open, for Darrell:**

1. *Colour scale across cells.*  Each cell scales its colours to its own
   data, as the template panel would alone; at a common axis scale a
   common colour scale may matter more for spotting the odd one out (a
   bright baseline is normalised away).  Options: per cell (now); one
   range for the grid -- from the template's own (whole-selection) render,
   or from the page.  plotms's iteration has no colour axis to follow.
2. *Scatter grids and one place to configure them* (asked 2026-11-11):
   the grid's configuration is Panel A's tab in the sidebar -- its
   Raster / Scatter switch included -- so no new control area is
   needed; scatter cells wait for G3's read-once-and-split.
3. *Status line after Back to Grid* says "One panel" until the next Plot
   (the layout buttons do not talk to Python).

## 5. G2 follow-ups: room for the plots, ✱, colour scope, frame cache (base `fc3ea3b8`, 2026-11-11)

Darrell's review of section 4: the grid's control row took the plots'
room; "All fields" after stepping left Plot orange (staged, as designed,
but surprising after the steppers plot at once); the colour range should be
a choice, defaulting to a common one; the frame cache evicted 1.5 GiB
frames at a 2.4 GiB budget.  Open item 1 of section 4 is settled by
**Colour scope** below; item 2 stands as agreed (Panel A's tab is the
grid's configuration); item 3 is still open.

**Grid controls placed as visplot's own** (`_place_grid_controls`).
`GridView(embedded=True)` builds only the plots and hands its widgets
back (`GridView.widgets`: `nav`, `play`, `shape`, `colour`, `status`,
`probe`, `save`); the application places them.  visplot puts:

- a **Grid** section at the top of the sidebar: Iterate by, Scale; Rows,
  Cols, Colour; Pages/s, Loop;
- paging and Play at the end of the toolbar;
- "drawn in" and the cursor readout in a row between the body and the
  status bar (clear of the cells, which now get the whole plot area).

Each is shown only in Grid (`grid_parts` in `__cvGridLayout`).  Save PNG
is not placed: Export PNG already writes the grid's page.  The stand-alone
`GridApp` keeps its control row (non-embedded, unchanged).

**✱ on the iteration steppers.**  Each stepper (Field, SPW, Antenna) is
◀ ✱ ▶; ✱ goes back to all (All fields, every SPW, no antenna or baseline
ticked) and plots at once, as ◀ / ▶ do.  Choosing "All fields" in the
dropdown still stages (Plot orange) like any other edit.

**Colour scope** (Grid section: *per cell*, *this page* (default), *whole
grid*; `gridcolour=` for scripts and `plotfile=`; in the reproducing call
when not "page").  A common colour scale needs common normalisation, not
only a common vmin/vmax: with `eq_hist` each cell equalised to its own
histogram makes any shared range meaningless.  So the cells share a
*reference population*:

- `VisibilityRaster._shared_reference` (default `None`): when set,
  `_shade_agg` takes vmin/vmax from it and `eq_hist` builds its curve from
  it; the colour bar mapping (`_bands_with_mappings`) uses the same, so a
  bar always describes the image.  A manual vmin/vmax still wins.
- `VisGridSource` keeps a sample of each cell's aggregated values (at most
  20000, at least 2000, about 2 M over the grid, evenly strided, plus the
  cell's exact min and max) and pools them: the page's cells for *this
  page*; every cell for *whole grid*.  `render(..., colour=,
  page_indices=)` sets the pool on each cell under the source's lock, so a
  prefetched page and a shown one do not mix ranges.  A cell drawn again
  alone (a per-cell zoom) keeps its page's range.
- *whole grid*: a background thread visits every cell (querying one not
  kept, then dropping it), a cell per lock hold, so page requests wait at
  most one cell.  Until it has finished a page is drawn in its own range
  and the response says `colour.pending`; the browser shows "grid colour
  range: k/n cells" and asks again every 1.5 s.  A new Plot (`configure`)
  forgets the samples and stops the pass.  Headless export with
  `gridcolour="grid"` waits for it (`wait_for_grid_range`).
- Cost: *this page* queries the page's cells before shading the first --
  the same work as before, in a different order.  *whole grid* reads
  every cell once; after G3 (read once, split) it becomes cheap enough to
  be the default.

**Frame cache budget** (`reader._frame_cache_budget`).  Remote hosts are
usually compute nodes, so free memory, not caution, decides:

| host | budget |
|---|---|
| < 12 GiB physical | 10 % of physical |
| ≥ 48 GiB free | min(64 GiB, 40 % of free) |
| otherwise | min(16 GiB, 25 % of physical), at most half of free |

Floor 256 MiB (512 MiB when memory cannot be read).  The choice and its
reason are logged at INFO; `CUBEVIS_VISPLOT_FRAME_CACHE_MB` (or
`frame_cache_mb=`) still sets it.  Examples: 8 GiB laptop 0.8 GiB; 24 GiB
workstation with 16 GiB free 6 GiB; 512 GiB shared host with 40 GiB free
16 GiB; 256 GiB node with 200 GiB free 64 GiB.

**Checks**

- `test_vis_grid.py` 19 (new: colour scopes, eq_hist shares one curve,
  `gridcolour` checked); `test_frame_cache.py` 31 (the host table above).
- `visplot_grid_check.py` 18 of 18 (new: *readout* clear of the grid with
  Rows in the sidebar; *all (✱)*; *colour*: default "this page", "per
  cell" redraws, "whole grid" ready), starting in Over, in Grid, and
  through a remote kernel.
- `grid_browser_check.py` 13 of 13 (non-embedded GridView).

**Fix (2026-11-12, base `78f7131e`): the selection narrows a baseline
grid.**  Stepping Antenna to DA49 on TW Hya with Iterate by = Baseline left
all 210 baselines (24 pages).  `_pairs_with_rows` applied the selection's
baselines / antennas only when the reader could not list the baselines
with rows; the reader's list ignores them.  Both lists are now narrowed, so
DA49 gives only the baselines to DA49 that have data.  Iterate by Antenna
already filtered.  `test_selection_narrows_the_cells`; the browser *all
(✱)* check now counts cells (Antenna ▶: fewer cells; ✱: all again).

**Fix (2026-11-12): busy until the grid's page is drawn.**  Stepping the
antennas in Grid, the cursor went idle before the grid changed: doPlot's
reply released the busy state (two frames, plus a 450 ms bridge for the
panels' re-render), but in Grid that reply only configures the source --
the page is a second request, made after it.  Now, in Grid, doPlot's busy
hold is released when `__cvGrid[key].reload()` (which returns `show()`'s
promise) has drawn page 1, two frames later; the 450 ms bridge is skipped.
The *busy* browser check delays each grid page by a second in the kernel
(a flag file, in the page's worker thread) and samples `__cvBusyN` every
5 ms: before the fix it fell to 0 for ~0.8 s of the draw; after, it holds
until the page is drawn and is 0 afterwards.

Seen on the way, not changed: each Plot in Grid also re-renders the hidden
template panel (the reply's new ranges set off its range re-render), which
competes with the grid's page for the kernel.  Skipping a hidden figure's
range re-render needs a "draw when shown" for its return from Grid.

**Grid fits its cells (2026-11-12, agreed with Darrell).**  Rows x Cols is
the most a page holds: when every cell fits on one page the grid shrinks
to them -- fewest rows, then fewest columns: one cell is one plot the size
of the grid; 2 are 1 x 2, 4 are 2 x 2, 5 are 2 x 3 (in 3 x 3).  A
baseline stepped to one cell no longer shows one plot among eight blank
ones.  It stays in Grid (no switch to One: paging and the grid's settings
stay; a tap still drills down to the full panel).  `GridView` keeps the
chosen shape (`state().rows/cols`) apart from the shown one
(`shownRows/shownCols`); `reload(why, nCells)` takes the count so the
first request is already the fitted shape (a source without the hint is
asked again at it).  Headless pages use the same rule
(`fit_grid_shape`).

Bokeh: putting back figures taken out of the `GridPlot` (1 x 1 back to
3 x 3) threw in `GridBoxView._update_layout` ("parent_style" of undefined)
on the next layout pass, before their views were rebuilt.  `regrid()` now
sizes figures outside the grid silently, re-assigns the children only when
they change, and then `show()` waits two frames (`S.settle`) before
anything else is laid out.

**Decided (2026-11-12): the grid gets its own panel, in G3.**  Editing the
gear tab in Grid changes Panel A today (the grid repeats Panel A's live
raster), each Plot in Grid re-renders hidden Panel A, and drill-down
overwrites it.  G3 gives the grid its own headless template with its own
gear tab (Raster / Scatter included), shown in the sidebar only in Grid,
started as a copy of Panel A the first time; drill-down copies the cell
into Panel A; the data selection (sidebar) stays shared.  Presets,
`plotfile=` and the reproducing call then name which configuration they
apply to.  The hidden Panel A re-render goes with it.

**Open, for Darrell:**

1. *Default colour scope.*  "this page" now; "whole grid" after G3.
   Paging under "this page" changes the colours from page to page --
   intended, and the reason for the whole-grid option.
2. *Status line after Back to Grid* (section 4, item 3), still.
