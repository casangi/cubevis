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
