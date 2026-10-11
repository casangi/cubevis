# HRS H7: antenna x antenna matrix, and raster cells several partitions share

*2026-10-11 (third session). Built against `main` at `f37392c`. Plan:
`hrs_visplot_plan.md` (H4 known issue, H7). Two pieces, the first a
prerequisite for the second, plus a long-standing display bug found on
the way.*

## 1. Raster cells several partitions cover

**Problem.** `query_raster` reduces each partition (MSv2: one per data
description and scan intent; MSv4: one per intent and spectral window)
to its own 2D image and merges them on the union of both axes
(`_raster_merge.merge_raster_partitions`). Where two partitions covered
the same cell, the first one's value won. Partitions that tile the grid
(different times, different channels) were fine. Partitions that
overlap were not:

- **Baseline x Time on a multi-SPW MS** showed the first spectral
  window only. This was a known issue since H4. It is the normal case
  for VLBA-like data.
- **Baseline x Channel across scan intents** (the same baselines and
  channels, different times) showed the first intent only.
- **An antenna x antenna matrix** has every partition covering every
  cell, so it would have shown one partition.

**Fix.** Shared cells are combined, weighted by the number of samples
each partition reduced there (`combine_rule`, `merge_raster_partitions`
`rule=` / `weights=`):

| Quantity | Across partitions |
|---|---|
| Real, Imaginary, Flag fraction | weighted mean (exact) |
| Amplitude | weighted mean of each partition's (vector or scalar) amplitude; Maximum / Median when "Baselines combined" says so |
| Phase | weighted mean direction |
| Phase RMS, Coherence, Amp V Diff, Phase Diff | weighted mean (Maximum for the DIFFs when chosen) |
| Z-Score | maximum |

The rule is the one the user already chose for baselines: "averaging
applies within each, then they are combined". A *coherent* combination
across SPWs would need the complex sums, which the partitions do not
return; it combines by weighted mean.

Weights are the unflagged samples of the shown correlation, or for the
Flag quantity every recorded (not padded) sample. They are computed in
a second, flags-only pass, and only when a shared cell exists, so
partitions that tile the grid cost nothing extra.
`XArrayReader._raster_weight`; the weights follow a partition that was
decimated after they were made.

## 2. Antenna x antenna matrix (H7)

**What it is for.** AIPS LISTR's 'MATX' listing (survey, section 2): the
oldest "find the bad antenna" view. There is one cell per baseline, the
quantity reduced over time and frequency. The matrix is mirrored, (A, B)
and (B, A) being the same baseline, so an antenna that is bad on every
baseline shows as a whole row and column. The diagonal holds the
autocorrelations when the data have them (section 3 of
`hrs_h6_scatter_presets.md`).

**What the user sees.**

- Raster axes "Antenna 1" and "Antenna 2" in the gear tab lists. They
  only go together; Antenna 1 x Time is refused with a message saying so.
- The **Ant×Ant** preset: the matrix (Amplitude) beside radplot's
  scatter (Amplitude vs UV Distance), side by side, with hover help.
  `preset="antmatrix"` in the constructor and task.
- Antennas with no data in the selection are left out. Every antenna is
  named on both axes (up to 30; more are labelled sparsely).
- Title "Amplitude (vector) [Antenna × Antenna, each baseline over time
  and channels]". No "mean of baselines": a cell is one baseline.
- Readout: the value and the baseline ("BL: DA44&DA45").
- A flag box on the matrix is refused, with a message: find the antenna
  here, flag it on Baseline × Time (or tick it and use Flag reaches).
  Flagging whole baselines from the matrix could be added if asked.

On TW Hya 3c279, 19 of 26 antennas have data. DV08, DV16 and DA48 show
as low rows and columns.

**How.** In each backend's `_raster_2d`, both raster dimensions are set
to the baseline, so everything else is reduced by the existing
per-quantity code. `data/_antenna_matrix.to_matrix` then lays the
per-baseline values out on antenna positions (the data's antenna order),
and after the merge `compact` drops antennas without data and puts their
names in `attrs["antenna_labels"]`. The raster panel turns those into
tick labels and readout names. The names cross the remote wire with the
array.

## 3. The clipped fragment above a two-line title

Seen by Darrell on 2026-10-06; never reproduced headlessly until the
Ant×Ant preset changed a raster title from one line to two. A strip with
the bottom of a line of text was left above the new title, and every
repaint drew it again until the panels round the plot were laid out
anew. A real window resize cleared it, which is why it looked
intermittent.

Measured pixel by pixel in headless Chromium:

| Tried | Result |
|---|---|
| Holding the font size constant | still appears: the trigger is the line count |
| Window 'resize' event | does not clear it |
| `invalidate_layout()`, `request_paint()` | does not clear it |
| Hiding the title for a frame | clears it until the next repaint |
| Range change | clears it |
| `min_border_top` +1 and back | clears it for good |

Fix: when a Plot response changes a panel's title line count, nudge
`min_border_top` 300 ms and 1.5 s later. Checked both ways (one line to
two and back) and through repaints.

Also: categorical axes with at most 30 labels (the matrix; a small
array's Baseline axis) get one tick per label (`desired_num_ticks`).

## 4. Verified

- `test_raster_partition_combine.py` (14): the merge rules on hand-made
  partitions; both backends, Baseline x Time on a simulated two-SPW MS
  with flags, against numpy (Real exact, vector Amplitude and Phase,
  Maximum, Flag fraction).
- `test_antenna_matrix.py` (21): both backends on a simulated MS with
  autocorrelations and two SPWs (values against numpy, symmetry, labels,
  diagonal, either axis order, Flag, Antenna x Time refused), `compact`;
  TW Hya 3c279 plotter (19 antennas, title, ticks, readout, flag box
  refused, preset, the JS for title and ticks).
- `test_remote_reduction_context.py`: the matrix, remote against local,
  both backends.
- Existing raster suites unchanged (`test_visibility_raster`,
  `test_raster_phase_stats`, `test_baseline_combine`,
  `test_raster_averaging`, the backend suites).
- Live: Ant×Ant on TW Hya 3c279 (screenshots), hover, flag box message,
  the title fragment before and after.

## 5. Pinned configuration tabs (after Darrell's test)

*2026-10-11, against `2c2e174` (H7 committed). Darrell: "This seems like
great work"; asked for a pin on the configuration tabs, to make it
easier to go through the Ant×Ant quantities.*

Each panel's configuration (gear) tab has a **Pin** checkbox left of its
✕. Ticked, a successful Plot leaves that tab open; unticked tabs close
as before. While the tab stays open the panel shows its real title (not
the red "Panel A" shown while editing), and ✕ then keeps the panel as
plotted. Status-area help on hover. Per tab, not ticked at start, and it
stays as set when the tab is closed and opened again. A failed Plot
leaves every tab open, as before.

`self._panel_pin`; the doPlot success branch (`keepPinned` updates the
tab's saved title, colour and kind). `test_gear_tab_pin.py` runs that
branch under node with stand-in models (pinned / not / both / switched to
Scatter while pinned). Live: Ant×Ant with Panel A pinned and Panel B
not; Phase, then Real; A stayed open, B closed; ✕ left "Real".

## 6. Not done

- Flagging from the matrix (whole baselines over the selection).
- Coherent combination across SPWs (needs complex sums from the
  partitions).
- Antenna 1 x Time and the like (which baselines make a row is a choice
  nobody has asked for).
