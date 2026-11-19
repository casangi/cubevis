# visplot pre-preview feedback: feature requests for planning

*Planning document, `debug-0001`, 2026-11-18.*

The first round of pre-preview feedback produced bug reports, misreadings
of how a control works, and requests for things visplot does not do yet.
The bugs are fixed on `debug-0001`.  The misreadings were settled in
discussion, and the answers belong in the user documentation.  This
document covers only the third group: feature requests that need a design
decision before anyone builds them.

For each request it gives what was asked, what visplot does today (checked
against the code), the design questions, and an estimate of size.  The
order is roughly the order in which they could be taken up.

| # | Request | Size | Depends on |
|---|---|---|---|
| 1 | [Scan iteration](#1-scan-iteration) | medium | — |
| 2 | [Correlation iteration](#2-correlation-iteration) | medium | 3 |
| 3 | [Which correlations "Correlation shown" offers](#3-which-correlations-correlation-shown-offers) | small | — |
| 4 | [The iterated value in the plot title](#4-the-iterated-value-in-the-plot-title) | small | — |
| 5 | [Channel selection in the GUI](#5-channel-selection-in-the-gui) | large | — |
| 6 | [Time (integration) iteration](#6-time-integration-iteration) | large | 1 |
| 7 | [Per-cell aggregation](#7-per-cell-aggregation-max-min-std-) | large | — |
| 8 | [Statistic windows: more general, typed values](#8-statistic-windows-more-general-typed-values) | small to medium | 7 |
| 9 | [User-guide pages from the in-app help](#9-user-guide-pages-from-the-in-app-help) | done; rewrite open | sidebar reorganization |

## How iteration works now

Items 1, 2, 4 and 6 build on the iteration fixed on `debug-0001`, so its
rules are summarized here.  The code is in `iteration_subset.py` and
`iteration_step.py`.

* Field, SPW, Antenna and Baseline each have Prev / ✱ / Next beside their
  control.  Each step plots straight away.  Steps wrap around at the ends.
* **The ▶ always marks iteration.**  The row being shown is that axis's
  *current row*, marked ▶ in the table (the table scrolls to keep it in
  view).  Plot requests carry the current row instead of the ticks.
* **Two or more rows ticked:** Prev / Next step through only those rows.
  The ticks stay as they are.
* **One row ticked:** Prev / Next step through every row that has data,
  and the tick moves with the ▶.
* **No row ticked:** on Antenna and Baseline, none ticked means all, so
  the ▶ steps through every row with data and the ticks stay empty.  On
  SPW (where none ticked is refused at Plot) the tick and ▶ start at the
  first row and move together.
* **Iteration ends** when the ticks change by hand (a click, the text box,
  ✱, a drill-down from the grid) or when Plot ▶ is pressed.  Plot ▶ always
  plots what is ticked.  The stepper's own tick moves do not end it.
* While a row is current, the status line shows the position, for
  example "Antenna 4/6: ANTENNA-3".
* **The grid's Play** advances, in order: the axis with a ▶ (the most
  specific, if more than one); else an axis with exactly one row ticked;
  else the most specific axis with more than one row to step.

---

## 1. Scan iteration

**Requested.** Prev / Next for Scan, like the other axes.

**Today.** Scan is a text box (`scan_inp`), not a table.  It takes CASA's
syntax (`4, 7~9, >12, !5`, parsed by `selection_text.parse_scan_text`).
Every other iterable axis is a table or a dropdown, so there is no list of
scans in the sidebar to step through or to mark.

**Design questions.**

* **What to step through.** Without a table, the subset rule would be:
  the scans the text selects, or every scan when the box is empty.  The
  scan list is known when the sidebar is built (`meta.scans`; the Scan
  help shows the first eight).  It would have to be sent to the browser,
  as the antenna list is, and parsed there with the same syntax as
  `parse_scan_text`.
* **Scans with no data.** Prev / Next skip antennas and baselines with
  no data in the current selection (their `has_data` columns, refreshed
  when the Data selection changes).  Nothing tracks this for scans yet;
  that refresh would need a scan list too.
* **Where the marker goes.** A text box has no row to mark.  The options:
  * show the current scan beside the box ("Scan 7 · 3 of 5");
  * turn Scan into a table, like Antenna.  That is the consistent choice,
    but a table is tall: an MS can have hundreds of scans, and the
    sidebar is already long.
* **Scan intent.** Many users would rather step through the
  calibrator scans or the target scans.  CASA's `intent=` is a separate
  selection.  Is an intent filter in scope, or a separate request?
* **Grid Play.** Play would need a scan axis too.

**Size.** Medium if the scan stays a text box with a "current scan"
label.  Large if Scan becomes a table.

## 2. Correlation iteration

**Requested.** Prev / Next for Correlation.

**Today.** The Data section's Correlation control is a set of checkboxes,
`corr_cbg`.  It decides what is read: for a scatter, one layer per ticked
correlation.  A raster shows one correlation at a time, chosen by each
panel's own **Correlation shown** (First ticked, Parallel hands, or a
named product).  See item 3.

**Design questions.**

* **What a step changes.**
  * Stepping the Data checkboxes changes what is read. Scatters lose
    their other layers, and a raster on "First ticked" follows along.
  * Stepping each raster's Correlation shown leaves the scatters alone,
    but there are two panels.  Does one Prev / Next move both?
* **The subset rule** applies as it does to the tables: with two or more
  ticked, step through those; the marker goes on the checkbox label.
  "Parallel hands" is not a checkbox, so it would not be a step.
* **Small numbers.** With 2 or 4 correlations, a dropdown already gets
  to any of them in one click.  Iteration matters most with Play and
  with grid Play.

**Size.** Medium.  Settle item 3 first, since it decides what "the
correlation shown" means.

## 3. Which correlations "Correlation shown" offers

**Requested** (in discussion).  "Correlation shown" seemed redundant
with the Data section's correlation ticks.

**Today.** They do different jobs, as described in item 2.  The confusing
part is that a raster can show a correlation the Data section leaves
unticked: the raster replaces the selection's correlation list with its
own (`correlation_for_query`).

**Options.**

* (a) Keep this, and document it.
* (b) Offer only the ticked correlations plus "Parallel hands".  This is
  simpler to explain, but showing RR and LL side by side then needs both
  ticked, which also gives each scatter two layers.

**Size.** Small either way.

## 4. The iterated value in the plot title

**Requested.** Show the value being stepped through (e.g. "SPW 3") in
the plot title.

**Today.** The status line already shows the position while iterating:
"Antenna 4/6: ANTENNA-3", "Baseline 5/15: ANT0&ANT5".  Raster titles name
the quantity, the axes and the correlation.  Scatter titles name the
panel.

**Design questions.**

* **Space.** Titles are already long.  A panel 400 px wide fits about
  50 characters.  A baseline name is often 15 or more.
* **PNG export.** The exported PNG contains the titles and color bars,
  but not the status line.  So a sequence of PNGs saved while stepping
  cannot be told apart unless the title carries the value.  That is the
  strongest argument for this request.  An alternative is a footer line
  on the export only.
* **Which value.** Only the axis being iterated, or the whole selection
  when it is a single field, SPW, antenna or baseline?

**Size.** Small.  The title is already built server-side per panel
(`_effective_title`).

## 5. Channel selection in the GUI

**Requested.** The Data selection cannot select channels.

**Today.**

* `visplot(..., freq_range=(f_lo, f_hi))` selects a frequency range in
  Hz, applied to every SPW, at start-up only.
* The data layer has `SelectionSpec.channel_range`, but nothing in the
  plotter sets it.  It is a single zero-based range applied within every
  SPW.
* There is no GUI control, and no CASA-style `spw='2:10~50'`.

**Design questions.**

* **Per-SPW ranges.** SPWs can have different channel counts, even
  between partitions of one MS, so one range for all SPWs is wrong as the
  general case.  The selection needs a range per SPW.
* **Syntax.** Extend the SPW text box ("id, name, a~b, !id") with CASA's
  `spw:chan` (`2:10~50, 3:0~63;100~127`)?  Or add a channel column to the
  SPW table?  The text form is familiar to CASA users and takes little
  room.  A table column shows the selection without needing to remember
  it.
* **Flag write-back.** Flags must be written with absolute channel
  numbers.  A raster drawn over channels 10~50 must not turn into flags
  on channels 0~40.  This affects `FlagDelta`, both back ends, and the
  JSON flag files.
* **Display.** Should the channel axis show absolute channel numbers or
  positions within the selection?  Absolute is safer, and matches flags.
* **Interaction with channel averaging.** The scatter's Average over
  channels and the raster channel window would apply within the
  selection.
* **MSv4.** Frequency is a coordinate per partition, so selecting by
  frequency maps more naturally than selecting by channel index.  Offer
  both?

**Size.** Large: it touches selection, both back ends, flags and the GUI.

## 6. Time (integration) iteration

**Requested.** Prev / Next through time, one integration at a time.

**Today.** Time is selected with a text box (Time range).  Iteration
exists for Field, SPW, Antenna and Baseline.  Scan iteration (item 1)
would cover stepping in coarse time units.

**Design questions.**

* **What a step is.** One integration, a scan (item 1), or a window of a
  given length?  A window with a typed length ("step 30 s") covers the
  first and third.
* **What it means for each plot.**
  * Rasters with a Time axis (Baseline × Time, Time × Channel) would show
    a single row per step.  That is useful only as a cursor, which
    cursor tracking already gives.
  * Scatters with X = Time likewise.
  * The useful cases are plots without a time axis: Baseline × Channel,
    Antenna × Antenna, Amplitude vs UV distance.  Stepping there is a
    movie of the data through time.  Should the control be offered only
    when no visible plot has a Time axis?
* **Cost.** Each step is a new query.  An integration-by-integration
  movie over a long observation is thousands of round trips.  Per-step
  caching, or a Play that reads ahead, may be needed.
* **The carrier.** Steps could rewrite the Time range text box, which
  keeps "what is plotted" visible and editable.  Or they could keep a
  separate current time, like the current row of the tables.

**Size.** Large.  Build Scan iteration first and see whether a time
window is still wanted.

## 7. Per-cell aggregation (max, min, std, …)

**Requested.** Choose how samples are combined into a cell, as the
MSraster prototype allowed: max, mean, min, std, sum, var.  The request
came from a single tester.  Phase RMS seemed too specific to be its own
quantity.

**Today.**

* A raster cell averages its samples: **Averaging** chooses Vector or
  Scalar.
* When a cell covers several baselines, **Baselines combined** chooses
  Mean, Median, Maximum or Vector (coherent).
* Phase RMS and Coherence are separate quantities with their own
  estimators (`data/_raster_stats.py`).  Phase RMS is a circular
  statistic: deviations about the mean phase direction, with an optional
  linear phase-slope removal.  It is not "std of phase".

**Design questions.**

* **Scope.** Raster cells only, or the scatter's averaging too?  The
  scatter's Datashader reductions already offer max, min, std and var
  per pixel.
* **How it relates to Averaging.** Vector / Scalar describe how samples
  are averaged.  Max, min and std of Amplitude are naturally scalar.
  Would Aggregation replace Averaging (Vector mean, Scalar mean, Max,
  Min, Std, …), or sit beside it with Averaging greyed out?
* **Phase.** Max and min of a direction have no meaning; std must be
  circular.  With slope removal, "std of Phase" is Phase RMS.  So Phase
  would offer Mean and Std (= Phase RMS) only.
* **How it relates to Baselines combined.** Two stages: within a
  baseline (this request), then across baselines (Mean / Median / Max /
  Vector (coherent)).  "Max then Max" is the cell's largest sample.  The
  combinations need explaining, possibly in a table in the long help.
* **Flagging.** Unchanged: a flag box still takes every sample behind the
  cells, whatever statistic colors them.
* **Performance.** The raster reduction (`_raster_average.py`) is numpy.
  Max, min and sum cost the same as a mean; std and var need a second
  pass or Welford's method.

**Size.** Large, mostly for the GUI and its explanation.  The
arithmetic is small.

## 8. Statistic windows: more general, typed values

**Requested.** The Time window and Channel window seemed useful but
limited to certain quantities, and offer only a fixed list of values.

**Today.**

* The windows belong to Phase RMS, Coherence, Amp V Diff and Phase Diff
  (`data/_raster_stats.py`).
  * Along a displayed axis, a window groups neighboring cells.
  * Along a reduced axis, windows are pooled.
* Any value already works from Python, for example
  `stat_time_window=45` or `stat_chan_window=12`, and it then appears in
  the dropdown.  The GUI offers the fixed list only.

**Design questions.**

* **Typed values in the GUI.** An editable field next to the dropdown,
  or "Custom…" as a dropdown entry that opens one?
* **Hide or label?** Hide the controls when the quantity does not use
  them, instead of labelling them "(RMS / Coherence / Diff)"?
* **More general.** Windows for every quantity amount to the
  aggregation of item 7 over a block of cells, so design the two
  together.

**Size.** Small for typed values and hiding.  Applying windows to every
quantity is part of item 7.

## 9. User-guide pages from the in-app help

**Done** on `debug-0002` (2026-11-19), the other way round from the one
first planned: the long form of each help is written in the user guide,
[`visplot_help.md`](../../../docs/source/applications/visplot_help.md),
and the code only names it by a label (`help_text.py`).  Whatever the
status area shows that has an entry carries a ⓘ; pressing **?** anywhere
in the window (or clicking a message's ⓘ) opens the documentation copy
for the installed version at that entry.  Letters are not used (`h` was,
briefly): they stay free for keyboard navigation.  A test checks every label the
code uses exists in the documentation; the build writes a forwarding
page for each (`docs/source/extensions/vp_help.py`).  How labels are
kept is in `docs/source/contributing.md`.

**Still open.**

* **The control-by-control rewrite** of the user guide, after the
  planned sidebar reorganization.  The help page's entries can then
  move into it; their labels go with them, and the code does not change.
* **Long-form content.**  The entries are the old long hint texts, lightly
  edited.  Examples and figures can now be added like anywhere else in the
  documentation.
* **More entries.**  Controls whose help fits the status area have no
  entry yet (no ⓘ).  Messages with one so far: the Scan, Time range and UV
  range errors, a raster's axes, the memory guard, and from flagging a
  reach that does not fit the box, a box that takes nothing, writing
  unavailable, and a write that does not check out.  Others can be given
  one with `help_text.marker`; most status lines need none.
