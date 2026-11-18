The visplot window
==================

.. currentmodule:: applications

.. warning::

   This page is a working draft for the ``visplot`` test release.  The
   control-by-control description will be rewritten after the planned
   reorganisation of the configuration panel; the sections a tester
   needs first -- *A commissioning workflow*, *Reporting a problem* and
   *Scope, explicitly* -- are current.

``visplot`` is the ``cubevis`` replacement for CASA6's
``plotms`` (visibility scatter/line plots) and ``msview`` (2-D raster
display of visibility data). It unifies both plot styles into a single
tool that reads MSv2 measurement sets and MSv4 / Processing Set data
and renders them interactively, backed by Datashader for large-dataset
aggregation.

This page is aimed at astronomers testing the release. It assumes no
familiarity with the ``cubevis`` internals.

Status of this document
------------------------

*Updated 2026-11-17.*  Current: the *Grid* layout, *Large selections*, *A commissioning
workflow*, *Reporting a problem* and *Scope, explicitly*.  Installing,
launching ``visplot`` and its arguments are in :doc:`visplot_starting`.  The *Layout tour* still describes the controls in
outline only; every control explains itself in the status area at the
bottom of the window while the pointer is over it.

.. note::

   **Jupyter notebooks are not yet supported.** Run ``visplot`` from a
   plain interactive Python or CASA6 session (``ipython``, or a ``casa``
   prompt).  ``visplot.notebook()`` exists but raises
   ``NotImplementedError`` saying so.

Layout tour
------------

The preview window is divided into three regions:

.. code-block:: text

   ┌──────────────────────────────────────────────────────────────┐
   │  Toolbar: Plot, Reload, layout, height, presets, Export PNG   │
   ├──────────────┬───────────────────────────────────────────────┤
   │  Sidebar     │  Panel A │ Panel B   (One / Side by Side /     │
   │  (collapsible)│          │            Over / Under)           │
   │              │  each panel: a raster or a scatter             │
   ├──────────────┴───────────────────────────────────────────────┤
   │  Status line, and help for the control under the pointer      │
   └──────────────────────────────────────────────────────────────┘

Toolbar
```````

* **Plot ▶** — run the query against the selected data and (re)render.
  It turns **orange** when something has been changed (a selection, an
  axis, a gear-tab setting) that is not drawn yet, and green again once
  the plot shows it
* **Reload ↺** — force re-read from disk (equivalent in spirit to
  ``plotms``'s Reload checkbox, used when the MS has changed on disk)
* **⟨ Sidebar** — collapse/expand the control sidebar
* Presets — one button each; a preset sets both panels and plots:

  * *vplot* — Baseline vs Time amplitude raster, Amplitude vs Time
    scatter
  * *radplot* — the same raster, Amplitude vs UV Distance scatter
  * *Waterfall* — Time vs Channel amplitude, panels one above the other
  * *All-BL* — the same axes with every selected baseline in one image
    and each cell showing the largest amplitude among them (the AIPS
    FTFLG view), with Amplitude vs Channel below: a quick survey for
    interference
  * *Z-Score* — Baseline vs Time colored by Z-Score, to spot outliers
  * *φrms·t*, *φrms·ν*, *φrms·uv* — Phase RMS against time, against
    frequency (channel) and against UV distance. The raster shows which
    baseline and when (or at which channel); the scatter shows the same
    values for all baselines. These three also put the *Phase slope*
    and window controls back to their defaults
  * *φ Wfall* — Time vs Channel phase waterfall
  * *Ant×Ant* — the antenna × antenna matrix (AIPS LISTR's): one cell
    per baseline, averaged over time and channels, mirrored so an antenna
    that is bad on every baseline shows as a whole row and column; the
    diagonal holds the autocorrelations when *Autocorrelations* is *Include*. Hover a
    cell for its baseline. Flag on Baseline × Time once the antenna is
    found (a flag box on the matrix is refused)
  * *Spectrum* — two scatter panels, Amplitude above Phase against
    frequency, each scan averaged (AIPS POSSM): the bandpass shape, edge
    channels and a phase slope across the band. Read one baseline at a
    time: tick one, or step with Baseline ◀ ▶ (which, like Antenna
    ◀ ▶, skips baselines with no data in the selected field and SPWs;
    the tables show those dimmed, marked "(no data)")
  * *Time series* — the same pair against time, all channels averaged
    (AIPS VPLOT): phase drifts and jumps, amplitude dropouts. Read one
    baseline at a time, as for *Spectrum*

* Layout — *One*, *Side by Side*, *Over / Under*, *Grid* (the first
  panel repeated for each baseline, antenna, SPW, field, scan or
  correlation, page by page; ``layout="grid"``, ``iterate_by=``).  Each preset switches
  to the arrangement that suits it unless *Pin layout* (configuration
  panel, section *Panels*; ``pin_layout=True``) is ticked
* Height — the height of each row of plots: *default* (the layout's),
  *fit* (share the window), or a size in pixels (``row_height``). Applies at once and
  stays when the layout or preset changes
* Gear (each panel's toolbar) — opens that panel's configuration tab
  in the sidebar; changes there apply when *Plot ▶* is pressed. A
  successful Plot closes the tab unless its *Pin* checkbox (left of the
  tab's ✕) is ticked, which keeps it open to try one setting after
  another
* Help — holding the pointer over any toolbar or sidebar control shows
  what it does in the status area at the bottom
* Pan / Zoom / Reset (shared across both panels when both are shown).
  Wheel (scroll) zoom is off until its toolbar button is clicked, so a
  trackpad or mouse-wheel scroll over a plot scrolls the page
* Describe this plot (each figure's toolbar, the page icon beside the
  gear) — opens a page describing that plot and the ``visplot(...)``
  call that reproduces it; see *Reporting a problem*
* Flag / Unflag boxes (each figure's toolbar) — draw a box to propose
  flags; the Flagging section of the configuration panel holds the
  filters, reaches, undo / redo, the report and writing to the MS

Sidebar
```````

* **Data selection** — data column, field, SPW, correlation,
  autocorrelations (*Exclude* by default), scan, antennas and baselines,
  time range, UV range
* **Panels** — *Pin layout*, *Time labels* (UTC or elapsed), and each
  panel's configuration tab (opened with its gear)

Each panel's configuration tab (its gear) holds the controls below; they
apply when *Plot ▶* is pressed.

* **Raster** — X/Y axis choice, quantity, *Correlation shown* (first
  ticked, the parallel hands averaged, or one product: two panels on RR
  and LL show a problem in one hand only), and averaging (vector or
  scalar). Besides amplitude, phase,
  real, imaginary, flag and Z-Score, the quantity can be *Phase RMS*
  (scatter of phase, in degrees, over the samples each cell covers) or
  *Coherence* (vector over scalar averaged amplitude, 0 to 1). On
  Baseline vs Time these show phase stability across the band for each
  integration; on Baseline vs Channel, stability over time for each
  channel (scan by scan, by default). The *Time window* and *Channel
  window* controls set what each cell's statistic is taken within; with
  a window set, the two quantities are also available on a Time vs
  Channel waterfall, where the selected baselines are measured one by
  one and combined.

  Where Baseline is not one of the raster's axes and several baselines
  are selected, each cell covers all of them. *Averaging* then applies
  to the samples of each baseline, and *Baselines combined* says what
  is done with the baselines: *Mean* (of their amplitudes), *Median*
  (what most of them share; a feature in *Maximum* but not in *Median*
  is on only a few baselines), *Maximum*
  (the largest; a problem on a few baselines stays visible) or
  *Coherent* (added as complex numbers first, which is only meaningful
  for a calibrated point source). The title names the choice.

  Raster cells are drawn where their coordinates are. A gap in time
  (between scans) or in frequency (between spectral windows) is left
  blank, and the cursor readout says so there. A Baseline axis shows
  the baselines that have
  data side by side, in number order or, with *Baseline order* set to
  *By length*, shortest first with the ticks reading the length. Sorted
  by length, trouble that grows along the axis is about distance
  (atmosphere, a resolved source); trouble in scattered columns is
  about particular antennas. The readout names the baseline and gives
  its length either way.

  *Amp V Diff* and *Phase Diff* show how far each sample is from the
  mean of the other samples in the same time window (one scan unless the
  *Time window* control gives a length): the amplitude of the vector
  difference, and the absolute phase difference in degrees. Anything
  steady subtracts out, so short-lived changes (an interference burst,
  a phase jump, one bad integration) stand out. Raster only.

  *Phase* is drawn in a cyclic colormap on a fixed -180 to 180 degree
  linear scale, so the wrap between -180 and +180 is not an edge in the
  image.
* **Scatter Y axis** — also offers *Phase RMS* and *Coherence*. Against
  Time each point is one baseline's scatter across the band in one
  integration; against Frequency or Channel, one baseline's scatter over
  a scan in one channel; against UV distance, one baseline's scatter
  over a scan.
* **Scatter axis controls** — X axis, plus one or more overplotted
  layers (quantity, correlation, color, opacity) for the scatter panel
* **Colormap controls** — scaling (default: histogram-equalized) and
  palette, for both panels

Panels
``````

The raster panel renders a 2-D image (e.g. amplitude as a function of
time and channel) using Datashader aggregation, with pan/zoom that
resamples the cached aggregate rather than re-querying the backend.
The scatter panel renders point clouds (e.g. amplitude vs. UV
distance), also Datashader-aggregated, with per-layer alpha blending
when multiple quantities or correlations are overplotted.

When both panels share the same X quantity (e.g. both are plotted
against time), their X ranges are linked, so panning one pans the
other.

Grid
````

*Grid* (the fourth Layout button, ``layout="grid"``) draws one plot many
times over: once for each baseline, antenna, SPW, field, scan or
correlation in the selection, as small plots on a page, paged through --
plotms's iteration (``iteraxis``, ``gridrows``, ``gridcols``), AIPS
VPLOT and POSSM, difmap's vplot.  The point is to compare: the one
baseline or antenna that looks unlike its neighbours.

* **What is drawn** is the grid's own panel, *⚙ Grid panel* in the
  sidebar's Grid section (or the gear in the grid's toolbar): the same
  choices as a panel's gear tab, raster or scatter.  It starts as a copy
  of the first panel; a preset pressed in Grid sets it.
  ``grid_panel=`` sets it from the start.
* **Iterate by** chooses the axis, and *then* a second one (Antenna, then
  SPW: each antenna's SPWs in turn).  The plots are of the current
  selection only: select five antennas and iterate by antenna for five
  plots.  *Rows* and *Cols* set a page's size; ⏮ ◀ ▶ ⏭ in the toolbar
  page through, and *Play* steps through the pages (*Pages/s*, *Loop*).
  When everything fits on one page, *Play* steps the sidebar's stepper
  instead (the next antenna, say).
* **Scale**: *common* (default) gives every plot the same axes, drawn
  once along the top row and down the left column, and a zoom in one
  plot zooms them all; *per cell* gives each plot its own axes and zoom.
  **Color** (rasters): one color range for the page (default), for the
  whole grid, or per plot.  A scatter plot is shaded by its own density.
* **Hover** gives that plot's value under the cursor, as in a panel.
* **Tap a plot** to open it in the One layout with the sidebar set to its
  selection.  Flag there as usual.  *Grid* again brings back the grid
  and its selection; flags added or written meanwhile show at once, with
  no need to press *Plot*.  Flagging is not done in the grid's plots
  themselves.
* **Export PNG** in the toolbar writes the page shown;
  ``plotfile=`` with ``layout="grid"`` writes pages without a window
  (``exprange``).

The selection is read once for all the plots of a grid and held in
memory, within the frame cache (see *Large selections*).  A grid too big
for that still works: each plot then reads its own rows, more slowly.

Large selections
-----------------

A scatter panel holds every selected sample while it draws.  A selection
estimated to need more than 70 % of the memory available is loaded
anyway (it may be slow while the system swaps) and noted in the log.
Where swapping is not wanted -- a shared cluster node, a machine without
swap -- ``CUBEVIS_VISPLOT_MEMORY_GUARD=<MB>`` sets a limit: a scatter
estimated to need more is refused before anything is read, with a
message saying what to narrow.  (``CUBEVIS_VISPLOT_FRAME_CACHE_MB`` is a
different setting: how much read data is kept for redrawing.)

The Grid layout holds the rows of its selection in memory, counted in that
frame cache: while it holds them, the cache may run past its size by a
quarter, no more (``CUBEVIS_VISPLOT_GRID_OVERFLOW=<percent>`` changes the
share; ``0`` keeps it within the size).  A grid whose rows do not fit is
drawn plot by plot instead, reading each plot's own rows.

A commissioning workflow
-------------------------

A first pass over a new observation, in the order the views are useful.
Each step says what the view is *for*; the preset buttons set both
panels at once.

1. **Survey.** Which antennas, baselines and times are wrong?

   * *Ant×Ant* -- one cell per baseline, averaged over the selection. An
     antenna that is bad on every baseline is a whole row and column.
   * *Z-Score* -- Baseline × Time colored by how unusual each sample is
     for its own baseline; isolated bright cells are outliers in time.
   * *φrms·t* / *φrms·uv* -- phase stability per baseline over time, and
     against baseline length: a rise with length is the atmosphere, one
     high baseline is an antenna.
   * *vplot* -- amplitude per baseline over time: dropouts, bad scans.

   On circular-feed (RR / LL) or linear-feed data, a problem in one hand
   only shows when the raster shows that hand: set a raster's
   *Correlation shown* to *Parallel hands* for a first look at both, or
   put RR and LL side by side.

   On data that are not yet amplitude calibrated, keep
   *Autocorrelations* at *Exclude* (the default); otherwise they set the
   scale and every cross-correlation reads as zero.

2. **Confirm** on one baseline, stepping with Baseline ◀ ▶ (or Antenna
   ◀ ▶ for all baselines to one antenna):

   * *Waterfall* and *φ Wfall* -- time × channel amplitude and phase.
     Narrow features in frequency or short ones in time are interference;
     in phase, stripes across frequency are a residual delay and stripes
     in time a rate.
   * *Spectrum* (scan-averaged, like AIPS POSSM) and *Time series*
     (channel-averaged, like VPLOT) -- the bandpass, edge channels, phase
     slopes, jumps and drifts.
   * *All-BL* -- every selected baseline in one waterfall (AIPS FTFLG),
     showing the largest amplitude: interference common to many
     baselines.

   Narrow the data with *Scan*, *Time range* and *UV range* (Data
   section) to look closely at one stretch of time or baseline length;
   the status line lists whatever is in force.

3. **Flag** on the view that shows the problem best, with a box from the
   panel's toolbar. *Flag reaches* (Flagging section) widen each box to
   all baselines of the antenna, all channels, all correlations, all
   spectral windows, the whole scan or all fields, as AIPS TVFLG's
   scopes do; *Preview each proposal* shows what a box will flag before
   it is added. Undo, redo and *Describe pending flags* work on the
   list of pending operations.

4. **Commit.** Nothing is written until *Write to MS*, which first saves
   a backup of the flags it changes. Export the pending flags as JSON to
   keep a record or apply them elsewhere.

Times are UTC throughout (axes, readout, Time range box); the *Time
labels* switch (Panels section) changes the axes to elapsed time.

Reporting a problem
--------------------

Each plot's toolbar has a **Describe this plot** button (a page icon,
beside the gear). It opens a page with everything that went into that
plot: the data set and how it was opened (local or remote kernel), the
selection in full, the axes and every setting that shaped the image, the
color scaling, the displayed range and zoom, the number of pending
flags, the versions of cubevis (with its git commit when run from a
checkout) and the main libraries, and the ``visplot(...)`` call that
reproduces the view, with a *Copy* button. The same page is available
from Python as ``VisibilityPlotter.describe(slot="A", html=True)``.

When reporting a problem, please include:

* the summary page of the panel concerned (save it, or paste its text),
* a screenshot, or a PNG from *Export PNG*,
* what you expected to see, and -- if you have it -- the ``plotms`` or
  AIPS plot of the same data that shows it.

The summary lists what the call does not reproduce (zoom, pending flags,
scatter color settings, theme), so say if any of those matter.

Checking against plotms
------------------------

The goal here isn't astronomical analysis — it's confirming that
``visplot`` is reading the MS correctly and rendering
something that resembles what ``plotms``/``msview`` would show for the
same selection. A reasonable sequence:

1. **Amplitude vs. time, all correlations.** This is ``plotms``'s
   default view (Amp vs. Time) and a good first sanity check — you
   should see the self-calibrated TW Hya continuum visibilities with
   no obvious dropouts or striping that would indicate a reader bug.

   Equivalent ``plotms`` call for comparison:

   .. code-block:: python

      plotms(vis='twhya_selfcal_contsub_ms', xaxis='time', yaxis='amp',
             field='5', correlation='XX,YY')

2. **Amplitude vs. UV distance.** Useful because the expected shape
   (a roughly declining amplitude envelope with baseline length, for a
   resolved but compact source like TW Hya) is easy to eyeball.

   .. code-block:: python

      plotms(vis='twhya_selfcal_contsub_ms', xaxis='uvdist', yaxis='amp',
             field='5', correlation='XX,YY')

3. **Raster: amplitude over time × channel.** Compare against
   ``msview``'s 2-D raster display, or a ``plotms`` iteration over
   channel. Flat, uniformly-colored channels with no residual line
   structure would be consistent with the continuum subtraction having
   worked (the line was removed upstream, in ``uvcontsub``, before this
   MS was written).

.. figure:: _static/vp_amp_vs_time.png
   :alt: visplot amp vs time
   :width: 100%

   TODO(screenshot): Amp vs. Time, both correlations, TW Hya field 5.

.. figure:: _static/plotms_amp_vs_time.png
   :alt: plotms amp vs time for comparison
   :width: 100%

   TODO(screenshot): Equivalent ``plotms`` view, for side-by-side
   comparison.

.. figure:: _static/vp_amp_vs_uvdist.png
   :alt: visplot amp vs uvdist
   :width: 100%

   TODO(screenshot): Amp vs. UVdist, both correlations.

.. figure:: _static/vp_raster.png
   :alt: visplot raster panel
   :width: 100%

   TODO(screenshot): Raster panel, amplitude over time × channel.

Axis vocabulary: visplot vs. plotms
-------------------------------------

``plotms`` axis names are the closest existing vocabulary an astronomer
will already know (see :xref:`casadocs_dataexam` for the full
reference). visplot exposes fewer axes, chosen for commissioning; the
concepts line up as follows:

.. list-table::
   :header-rows: 1
   :widths: 30 30 40

   * - plotms axis
     - visplot (preview)
     - Notes
   * - ``time``
     - Time
     - 
   * - ``chan`` / ``channel``
     - Channel
     - 
   * - ``freq`` / ``frequency``
     - Frequency
     - scatter X; a raster's Channel axis reads frequency when several
       spectral windows are shown
   * - ``amp`` / ``amplitude``
     - Amplitude
     - 
   * - ``phase``
     - Phase
     - 
   * - ``real`` / ``imag``
     - Real, Imaginary
     - 
   * - ``uvdist``
     - UVdist
     - 
   * - ``corr`` / ``correlation``
     - Correlation (selection, not yet a plottable/colorize axis)
     - 
   * - ``flag``
     - Flag (raster quantity: the flagged fraction of each cell)
     - flagged data can also be shown (Flagging, *Show flagged data*)
   * - ``coloraxis``
     - Colorize (scatter, per layer)
     - continuous, categorical (e.g. by antenna, baseline, scan, field)
       or statistical (Z-Score)
   * - ``elevation``, ``hourangle``, ``azimuth``, ``parang``
     - *not available*
     - add if testers ask (computable from time, positions and field
       direction)

Scope, explicitly
-----------------

*Updated 2026-10-11, before testing by astronomers.*

Present and working:

* Raster and scatter display, MSv2 and MSv4/Processing Set backends,
  local or in a remote kernel
* ``vis=`` for either format, as in CASA tasks (``ms=`` / ``ps=`` still work)
* Selection by data column, field (one, several, ranges, wildcards), spectral window, correlation,
  antenna and baseline (tables, or typed text); stepping through
  fields, spectral windows, antennas and baselines with ◀ ▶
* Raster quantities including Phase RMS, Coherence, Z-Score and the
  phase / amplitude differences; vector or scalar averaging; how a cell
  covering several baselines is combined; which correlation each raster
  shows, including the parallel hands averaged
* Scatter averaging over time and channels
* Presets: vplot, radplot, waterfalls (one baseline, all baselines,
  phase), Z-Score, phase RMS views, antenna × antenna matrix, Spectrum,
  Time series
* Colorize by axis, several correlations as layers
* Flagging: boxes on either panel with filters, preview, reaches
  (antennas, all baselines, channels, correlations, spectral windows,
  scan, field), reason, undo / redo, a report, JSON export, and writing
  to the MS with a backup
* Selection by scan, time range (UTC) and UV range, CASA syntax;
  autocorrelations excluded by default (*Autocorrelations* switch)
* UTC time axes, readout and Time range box (or elapsed time)
* The plot summary page and its reproducing call (*Reporting a problem*)
* Export PNG from the toolbar, from Python without a browser
  (``VisibilityPlotter(..., headless=True)(plotfile=...)``), and from
  the task (``visplot(..., plotfile="out.png")``)
* Shared pan/zoom/reset, linked X range when applicable; dark and light
* The Grid layout: one plot per baseline, antenna, SPW, field, scan or
  correlation (or two of them), raster or scatter, paged, with
  drill-down to a full panel and PNG pages (*Grid*)

Not yet available:

* Jupyter notebooks (``visplot.notebook()`` says so)
* Correlation as a raster axis (removed from the axis lists; each
  raster shows one correlation, or the parallel hands averaged, chosen
  in its gear tab and named in its title)
* Flagging on the antenna × antenna matrix (refused, with the reason)
* More than two panels (the Grid layout repeats one)
* Flagging in the Grid layout's plots (tap one and flag in the panel)
* Locate; on-the-fly calibration; plotting calibration tables
* A ``flagdata`` command export (removed; JSON and writing to the MS
  replace it)

See also :doc:`capabilities/index` for a capability-by-capability
description of what ``visplot`` can do.

Feedback
---------

If something looks wrong compared to ``plotms``, ``msview`` or AIPS --
inverted axes, wrong amplitude scaling, missing correlations, channels
that don't match the spectral window, times that don't match the
observing log -- that is exactly what testing is meant to catch. Report
it as described in *Reporting a problem*, with the plot summary page.
