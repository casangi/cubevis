# Handoff: grid plots in visplot, G2 done, G3 next (2026-11-13)

> **Update, later on 2026-11-13: G3 item 1, the grid's own panel, is
> done.**  See `gridplot_notes.md` section 7 and plan section 15.  It was
> delivered as `gridplot-g3a_b694c8ad.zip`.  Next is G3 item 2, read once
> and split.  The test counts below are now 34 (`test_vis_grid.py`) and 24
> (`visplot_grid_check.py`).  Section 8 adds the grid's gear; section 9 makes Play play the stepper when every cell is on one page (27).  Section 10 is G3 item 2, read once and split (test_vis_grid 47); next is item 3, scatter grids.

The plan is `devel/docs/gridplot/grid_and_calplot_plan.md`, mirrored to
the claude.ai project doc `claude/grid_and_calplot_plan.md`.  Sections 13
and 14 cover what was built; the stage table is section 6.  The record
of each slice is `gridplot_notes.md` in this folder, sections 1-6.  The
next section is **7**.

## Where things stand

* **Branch `gridplot` at `fa907501`**, plus the zip from the final
  clean-up if Darrell has committed it (`ref_pngs.py`, the browser check
  tidied, this file).  Check `git log origin/gridplot`.
* **Done:** G0 (benchmark), G1a (shared core in `cubevis/toolbox/gridplot`),
  G1b (the grid view), G2 (visplot's Grid layout) and the G2 follow-ups:
  * the grid's controls are in the sidebar, the toolbar and the readout
    row, so the plots get the whole plot area;
  * ✱ on the Field / SPW / Antenna steppers;
  * Color scope: per cell / this page (the default) / whole grid, with
    shared normalisation, so the cells share one eq_hist curve;
  * the frame cache budget is set from the host's free memory;
  * the selection narrows a baseline grid;
  * busy stays on until the grid's page is drawn;
  * the grid shrinks to fit when every cell fits on one page.
* **Works:** raster grids, local and remote (a kernel on the same host),
  in both themes.  Cells are iterated by baseline, antenna, SPW, field,
  scan or correlation.  Paging and Play work.  Common and per-cell scales
  work, with outer axes drawn once.  Probe, drill-down and Back to Grid
  work.  Export PNG writes the page, and `plotfile=` writes pages with
  `gridrows`, `gridcols`, `gridcolor` and `exprange`.
* **Not yet:** scatter grids (G3); a grid template of its own (G3); real
  data timings.  Darrell has used it on TW Hya
  (`sis14_twhya_calibrated_flagged.ms`, 26 antennas, 210 baselines).  The
  VLBA TL016B subset is untested.

## Working conventions (Darrell)

* Deliver a zip of complete files, unzipped at the repo root and named
  with the base commit, e.g. `gridplot-g2f_2edbe0ca.zip`.  List any files
  to delete separately; a zip cannot delete them.
* Never commit or push.  Darrell commits to `gridplot` himself.
  `git fetch origin gridplot` before each slice, then
  `git reset --mixed origin/gridplot` once the tree matches his commit.
  This clone is read-only for him.
* Add one notes section per slice to `gridplot_notes.md`.  When the plan
  changes, read it in full and then `project_write` it with `local_path`.
  The tool refuses the write unless this session has read or written the
  doc in full.
* Answer design questions with a recommendation and wait for his "go"
  when it changes the plan.  Small fixes he reports from the browser are
  just fixed, with a check that would have caught them.

## Code map

| Where | What |
|---|---|
| `cubevis/toolbox/gridplot/grid_view.py` | `GridView`: pool of figures and the JS API `window.__cvGrid[key]` (reload, refit, redraw, restyle, state, save).  `embedded=True` hands back `.widgets` for the app to place.  `fitShape`/`regrid`/`S.settle` handle fit-to-cells; `color` scope comes from the source's `COLOR_SCOPES`. |
| `cubevis/toolbox/gridplot/grid_source.py`, `grid_app.py` | `GridSource` protocol, synthetic source, stand-alone `GridApp`. |
| `cubevis/toolbox/visplot/vis_grid_source.py` | `VisGridSource`: one headless clone of the template per cell (`_cell`, `_clone`); `_iteration_values` / `_pairs_with_rows` decide the cells from the selection; color scope (`_shared_reference`, `_sample`, background `_start_grid_pass`); `drill_target`. |
| `cubevis/toolbox/visplot/visibility_raster.py` | `_shared_reference`, honoured in `_shade_agg` and `_bands_with_mappings`. |
| `cubevis/toolbox/visplot/visibility_plotter.py` | `_build_grid`, `_place_grid_controls`, `_wire_grid`, `_grid_drill`, `_export_grid_png`, `_GRID_LAYOUT_JS`, `_GRID_DRILL_JS`, `fit_grid_shape`, `parse_exprange`.  doPlot's callback reloads the grid and releases busy when page 1 is drawn.  `_handle_plot` → `_handle_plot_panels`, which re-renders only the template in Grid. |
| `cubevis/toolbox/visplot/data/reader.py` | `_frame_cache_budget` (host rule in notes section 5). |
| `scripts/sync_layers` | Regenerates the casatasks/casashell `visplot` wrappers from the constructor.  Run it after adding an argument, then `--check`. |

## Test kit

All of these passed at the end of this session (2026-11-13):

```bash
# a VLBA-sized synthetic MS (10 antennas, 45 baselines, 64 chan, 600 times)
python devel/tools/gridplot_bench/bench_g0.py --make /tmp/g0.ms

python -m pytest -q tests/manual/gridplot/test_vis_grid.py          # 29
python -m pytest -q tests/manual/visplot/test_frame_cache.py        # 31
python tests/manual/gridplot/visplot_grid_check.py --ms /tmp/g0.ms --out /tmp/vgc   # 20 of 20
python tests/manual/gridplot/visplot_grid_check.py --ms /tmp/g0.ms --out /tmp/vgc --start grid
python tests/manual/gridplot/visplot_grid_check.py --ms /tmp/g0.ms --out /tmp/vgc --kernel python3
python tests/manual/gridplot/grid_browser_check.py --out /tmp/gbc   # 13 of 13 (stand-alone GridView)
python tests/ci/run.py                                              # 8 of 8
python3 -m scripts.sync_layers --check
python devel/tools/gridplot_bench/ref_pngs.py /tmp/ref > before.txt # 40 hashes; diff after a refactor
```

Whole visplot suite: `CUBEVIS_SRC=$PWD python -m pytest -q -n 8
tests/manual/visplot tests/manual/gridplot`.  Under `-n 8` about ten tests
fail by timing; rerun the failures serially before believing them.  The
failures that are real at `fa907501` are listed under **Suite baseline**
below.

The browser check (Playwright, headless Chromium) has a few helpers worth
reusing:
* a kernel-side delay of grid pages, switched on by a flag file and run in
  the page's worker thread (the *busy* check);
* `wait_drawn()`, which prints the grid's status, the console and
  `state()` when a page never draws;
* screenshots of each step in `--out`.

## Lessons (save the next session the time)

* **Bokeh `GridPlot` children.**  Putting back figures that were taken out
  makes the next layout pass throw `parent_style of undefined` in
  `GridBoxView._update_layout` until Bokeh has rebuilt their views.  Size
  figures that are out of the grid silently (`setv(..., {silent: true})`),
  reassign `children` only when they change, and wait two frames
  (`S.settle`) before any other change.  The stack trace hides the caller.
* **Busy is reference counted** (`window.__cvSetBusy`, `__cvBusyN`).
  Release exactly what you hold.  A hidden panel's range re-render also
  holds busy and can mask a missing hold.
* **A cubevis message sent while the websocket opens kills the session.**
  The grid waits for `pipe._mgr.transport.isConnected()`.  The transport
  itself still needs fixing (not done).
* **The reader's `baselines_with_rows(selection)` ignores the antenna and
  baseline selection**, so callers must narrow the result themselves.
* **Bokeh does not lay out a grid nested inside visplot's plot area.**
  The grid has its own `_grid_area` and is attached the first time it is
  used (a hidden grid costs 1.4 s at load).
* Patch scripts: visplot's JS contains no-break spaces; match them
  explicitly.

## Next: G3 (agreed)

In order of value:

1. **The grid's own template panel** (decided 2026-11-12, notes section 5).
   * A headless template with its own gear tab, including Raster /
     Scatter, shown in the sidebar only in Grid.
   * It starts as a copy of Panel A the first time Grid is entered.
   * Drill-down copies the cell into Panel A.  The data selection stays
     shared.
   * Presets, `plotfile=` and the reproducing call must say which
     configuration they apply to.
   * It removes the hidden Panel A re-render on every Plot in Grid.
2. **Read once, split by the iteration axis** (plan section 11, finding 3):
   * one frame read for the page's union selection, grouped by baseline
     or antenna;
   * scatter grids need it (1.7 s a cell today against 0.11 s split);
   * it makes "whole grid" color cheap, and then that becomes the
     default scope.
3. **Scatter grids**: Raster / Scatter in the grid template; cells from the
   scatter renderer; the color scope for scatter density.
4. Compound iteration (antenna, then SPW) and the `axes_override` hook
   only.

Then C0 (the arcae `casa-caltab` xarray backend) and calplot (plan
sections 4, 9 and 10).

## Open

* **Stale status line** after Back to Grid: it says "One panel" until the
  next Plot (notes section 4).
* **Default color scope**: "this page" until G3, then "whole grid".
* **The cubevis transport** drops the session on an early message (see
  Lessons).
* **Real-data timings** for G2 and G3 on TW Hya and the VLBA subset,
  measured with `bench_g0.py --ms`.

## Suite baseline at `fa907501`

`CUBEVIS_SRC=$PWD pytest -n 8 tests/manual/visplot tests/manual/gridplot`
gave 2279 passed, 695 skipped, 15 failed and 1 error in 17 min.  All 15
failures pass when rerun serially, so they are timing under `-n 8`.  The
one real failure is a collection error in `tests/manual/visplot/test_info_panel.py`
("attempted relative import beyond top-level package").  It was already
there at `fc3ea3b8` and is not grid work.  The 40 reference PNG hashes
are unchanged since `fc3ea3b8`, rebuilt from fresh MS sets with
`ref_pngs.py`.
