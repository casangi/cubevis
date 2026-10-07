# HRS H3 / H4, slice 1: raster cells drawn where their axes say

*2026-10-07. Built against `main` at `693c99c`. Plan:
`hrs_visplot_plan.md` (closes H3; prepares H4 and H5).*

## What was wrong

The raster image did not line up with its own axes whenever an axis had
gaps.

`datashader.Canvas.raster` places an aggregate's rows and columns
*evenly* between the first and last coordinate, whatever the coordinate
values are. The axis ticks, the cursor readout and flag boxes all work
from the real coordinate values. The two agree only on an axis without
gaps.

Measured on the TW Hya test data, Baseline x Time, all fields (410
integrations of 6 s spread over 94 minutes):

| On the axis | Row actually drawn there | Row the readout / a flag box addressed |
|---|---|---|
| t + 710 s | t + 1173 s | t + 598 s |
| t + 1650 s | t + 1932 s | t + 1659 s |
| t + 4471 s | t + 4245 s | t + 4500 s |

The same on the Baseline axis as soon as the selection left holes in the
baseline numbers: with antenna DA44 ticked the remaining baselines are
numbers 1, 25, 49-71, and the column drawn at "25" was baseline 55.

So a box drawn round a visible feature could flag other data than it
enclosed. It was exact only for one scan with every baseline selected.
`tests/manual/visplot/test_raster_grid.py` has two tests
(`test_image_is_blank_in_a_time_gap`,
`test_drawn_rows_are_the_rows_the_axis_says`) that use only what existed
before and fail on `693c99c`.

## What the user sees now

- **Time and frequency axes are true.** Each integration is drawn at its
  own time. A gap between scans (or between spectral windows on a
  Frequency axis) is blank. The cursor readout in a gap says "no data
  here (a gap in the data)"; a flag box drawn only over a gap flags
  nothing. On TW Hya with every field selected about half the height of
  a Baseline x Time raster is now blank, because about half of that
  time range holds no data; zooming in works as before.
- **The Baseline axis has no holes.** The baselines that have data are
  drawn side by side. With one antenna ticked the raster is that
  antenna's baselines filling the width, not a few columns among blank
  ones. Baselines that were never observed (xarray-ms lays the axis out
  as every antenna pair; TW Hya has data on 210 of 325) are no longer
  drawn as blank columns.
- **Baseline order** (new control in each raster's gear tab; constructor
  and task argument `baseline_order`): *By number* (default) or *By
  length*, shortest first, from the antenna positions.
  - By number the ticks read the baseline number (the one the sidebar's
    Baseline table shows). They are no longer evenly spaced numbers when
    baselines are missing: tick "49" may sit next to tick "25".
  - By length the ticks read the length ("15.1 m", "1.24 km") and the
    axis is labelled "Baseline (by length)".
  - What it is for: trouble that grows along a length-ordered axis is
    about distance (atmosphere, a resolved source); trouble in scattered
    columns is about particular antennas. In number order the baselines
    to one antenna sit together instead.
- **Readout** on a Baseline axis: `Baseline: <number> | ... | BL:
  DA44&DV20 | Length: 107 m`, the same in either order.
- **A single baseline** can be drawn on a Baseline axis (it was "too
  small to draw").
- **Axis ranges** run to the cells' edges, so the first and last rows
  and columns are whole. Elapsed-time labels still count from the first
  integration's centre, as on the scatter panel.
- **Flag records** name baselines, not axis positions: `raster box
  BASELINE [#14 ANTENNA-4&ANTENNA-5 .. #6 ANTENNA-1&ANTENNA-3, 3
  baselines by length] x TIME [...]`.
- **Ticks on a Baseline axis** stay on whole positions when zoomed in
  (set when the figure is built, and by `cvIntegerTicks` in the Plot
  response handler when the axes change).

## The cell rule (one definition, `raster_grid.py`)

Each coordinate is the centre of its cell.

- Neighbouring cells share an edge when they are as close as their own
  widths say they should be. A cell's width comes from its *nearer*
  neighbour, so a run of 6 s integrations has 6 s cells whatever lies
  beyond the run.
- Where the next coordinate is more than 1.5 times further than that
  (`GAP_FACTOR`), there is a gap: each cell keeps its own width and the
  space between belongs to no cell. One missing integration in a run is
  a gap.
- A Baseline axis is drawn in whole positions 0, 1, 2 ... (`BaselineAxis`
  maps positions to baseline numbers, in number or length order).

Before, a cell reached halfway to each neighbour however far away, so
the integration beside a 150 s pause "owned" 75 s of it for the readout
and for flag boxes, while the image showed something else again.

## Where it is used

| Consumer | Before | Now |
|---|---|---|
| Image (`VisibilityRaster._resample`) | `Canvas.raster`: even spacing | `raster_grid.resample`: each cell at its edges; mean where several cells share a pixel; no interpolation |
| Phase image | cos / sin through `Canvas.raster` (H1b) | cos / sin through `raster_grid.resample`; still a circular mean |
| Cursor readout (`_data_to_pixel`, `_cell_bounds`) | nearest centre; midpoint bounds | containing cell or "no data"; `cell_edges` |
| Flag box (`flag_engine._overlap`) | midpoint bounds on baseline numbers | `cell_edges`; Baseline through the request's `baseline_order` (the ids in display order, sent by the panel) |
| Flag overlays (`_apply_flag_overlays`) | `Canvas.raster(agg="min")` | `raster_grid.resample(how="any")`, on the panel's own baseline positions |
| Ticks, browser and PNG | numeric | `PanelSpec.x_ticks / y_ticks` (per-position text) through `tick_format`, one implementation in JS and one in Python as before; state keys `x_cat`, `y_cat`, `x_t0`, `y_t0` added |
| 1:1 zoom (`agg_n_x`, `agg_n_y`) | number of cells | number of *typical* cells the range would hold (on a gapped axis the number of cells gave 13.8 s per "cell" for 6 s integrations) |

Baseline lengths: `IdentityTables.baseline_lengths` (baseline number to
metres), filled by both backends from the `antenna_xds` nodes
(`XArrayReader.antenna_positions`). They ride with the identity tables a
panel already fetches once per selection, so the remote path needs no
extra round trip and no new wire type.

`raster_interpolate` is still accepted; the image no longer depends on
it. What is gone is linear *interpolation* between cells, which was only
reachable by forcing `raster_interpolate="linear"`.

No TypeScript change; the committed cubevisjs bundles are untouched.

## Choices made, open to change

1. **Real time with blank gaps, not contiguous time slots.** msview
   draws time as "time slots" with no gaps, which is what the image was
   effectively doing. Real time keeps the raster comparable with the
   scatter panel beside it and makes "when" readable off the axis. The
   cost is screen space on sparse schedules. A compact option could be
   added through the same per-position tick labels the Baseline axis
   now uses, if users want it.
2. **Never-observed baselines are left out** of a Baseline axis.
   Fully-flagged baselines are kept (they have rows), and show blank.
3. **`GAP_FACTOR = 1.5`.** 1 would call timing jitter a gap, 2 would
   hide one missing integration.
4. **Baseline order is per raster panel**, like averaging.
5. **Length is the distance between the antennas** (3-D, from the
   antenna positions), not the projected length, which changes with
   time. Without antenna positions the order falls back to number.

## Verified

- `test_raster_grid.py`, 86 tests, both backends:
  - the cell rule; `resample` against a brute-force pixel-by-pixel
    reference on 150 random gapped grids; `BaselineAxis`; tick labels in
    Python and the shipped JS under node;
  - on a simulated MS with two pauses in time (and its MSv4 twin):
    blank image in a pause; each drawn row is the integration the axis
    says; every pixel shows the cell under it in both orders; columns
    are the baselines the mapping names (against a direct backend
    query); length order matches lengths computed independently from
    the antenna table; readout names the baseline and gives the right
    value and time; readout in a pause;
  - on a real `VisibilityPlotter`: a box flags exactly the baselines
    and integrations it encloses in length order and in number order
    (checked on the flags themselves); a box over a pause only flags
    nothing; a box from a pause into data flags only what it touches;
    the pending-flag overlay is drawn inside the box and fills it; a
    Plot request changes the order and re-queries, and an unchanged or
    absent order does not;
  - TW Hya: gaps blank and rows at their times; 210 of 325 baselines
    drawn; DA44's baselines side by side, sorted, with the readout
    naming each.
- `test_hrs_presets_diff_phase.py`: the three H1b resample tests
  rewritten for the new call (same assertions).
- `test_visibility_raster.py`: two tests that equated `agg_n_y` with the
  number of rows now state the new definition (typical cells across the
  range); the rest unchanged, 212 pass.
- Whole `tests/manual/visplot`, one file per process, with the TW Hya MS
  and its MSv4 twin present, `693c99c` against this tree: identical
  except `test_raster_grid` (new, 86 pass) and `test_raster_phase_stats`
  (2 failures on `693c99c` fixed, see "Restored" below). Unchanged on
  both: `test_zscore_colorization` 4 setup errors.
- **Could not be run in the cloud sandbox on either tree** (they build a
  full plotter on TW Hya and exceed its 8 GB):
  `test_colorize_by_axis_part5d_legend_status`, `test_frame_cache`,
  `test_info_block_integration`, `test_visibility_scatter`. Run these
  locally.
- `scripts/sync_layers --check` clean after regeneration.
- Headless Chromium on the simulated data (no kernel): the page loads
  without script errors, the raster shows the two pauses blank, the
  Baseline axis reads lengths and "Baseline (by length)", each raster
  gear tab has the Baseline order control, and zoomed to two baselines
  the axis has one tick per baseline.
- Static PNG of TW Hya from both trees, side by side (scans as bands
  with blank gaps; DA44's 20 baselines filling the width).

## Restored

`693c99c` had lost the "one hover region for heading and table" change
of `dc58f0e` (SPW, Antenna, Baseline tables and the Correlation boxes):
the previous delivery was built on `4869088`, the merge that lacked it,
so unzipping it over `0a88721` took it out again, and
`TestStatusAreaHelp::test_every_non_text_control_with_help_is_wrapped`
failed on `main`. The four hunks and the matching paragraph of
`hrs_h2_phase_statistics.md` are back in this delivery.

## Not verified

Anything that only happens in a browser with a live kernel: that the
Baseline order control replots, that ticks read sensibly while zooming
(between whole positions a tick has no label), that flag boxes land
where drawn.

## Found, not changed

Both on the zoom path for a *decimated* raster (more cells than
`max_cells`, 2 million; TW Hya never is, HRS-sized data will be), both
present at `693c99c`:

1. **Channel axis:** zooming in until a re-query is needed raises
   ("zero-size array to reduction operation minimum").
   `VisibilityPlot._viewport_selection` puts channel *numbers* into
   `freq_range`, which is in Hz, so nothing matches. Converting is not
   enough: the re-queried aggregate numbers its channels from 0 again.
2. **Any axis:** after such a re-query the panel holds only the zoomed
   region, and zooming back out shows that region alone on an otherwise
   blank plot (0.1 % of the image drawn in a test); the readout's
   elapsed-time origin also moves to the zoomed region while the axis
   keeps the old one.

These need the two-level zoom reworked (keep the full aggregate, hold
the detail separately) and deserve their own slice with tests on a
large simulated data set.

Smaller:

- The sidebar's Baseline table still lists every antenna pair, observed
  or not, and has no length column.
- Zoomed far into a Baseline axis, ticks between whole positions are
  drawn without labels.
- `_wire_types._encode_dataframe` appends to
  `/tmp/cubevis_dataframe_debug.log` on every call (left-over debugging).
- Drawing a 2-million-cell aggregate takes about 0.1 s with the new
  resampler against 0.015 s for datashader's compiled one; typical
  rasters (a few hundred by a few hundred cells) take about 10 ms.
