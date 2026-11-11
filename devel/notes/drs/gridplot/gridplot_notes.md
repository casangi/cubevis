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
