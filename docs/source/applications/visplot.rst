visplot
=======

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

*Updated 2026-10-11.*  Current: launching ``visplot``, the task
arguments, *A commissioning workflow*, *Reporting a problem* and
*Scope, explicitly*.  The *Layout tour* still describes the controls in
outline only; every control explains itself in the status area at the
bottom of the window while the pointer is over it.

.. note::

   **Jupyter notebooks are not yet supported.** Run ``visplot`` from a
   plain interactive Python or CASA6 session (``ipython``, or a ``casa``
   prompt).  ``visplot.notebook()`` exists but raises
   ``NotImplementedError`` saying so.

Prerequisites
-------------

* A working ``cubevis`` environment with the ``visplot`` task available
  (``from cubevis import visplot``)
* An interactive Python or CASA6 session — **not** a Jupyter notebook
  (see the note above)
* A measurement set. This guide uses ``twhya_selfcal_contsub_ms``,
  described below.

The test dataset
-----------------

The dataset used throughout this guide is the continuum-subtracted,
self-calibrated TW Hydra measurement set produced at the end of the
NRAO CASA Guides imaging sequence:

* :xref:`casaguide_imaging`
* :xref:`casaguide_selfcal`
* :xref:`casaguide_lineimaging`

Briefly: the raw TW Hya data are calibrated, imaged, and phase (then
amplitude) self-calibrated in four rounds against an ALMA continuum
model of field 5 (refant ``DV22``). The resulting ``twhya_selfcal.ms``
is then continuum-subtracted (``uvcontsub``) against the N2H+ line
present in the observed spectral window, producing the MS used here.

Because this dataset has a well-documented processing history and
published-looking plots in the CASA Guides (phase-vs-time solutions,
amp-vs-uvdist continuum visibilities, etc.), it's a reasonable dataset
against which to eyeball whether ``visplot`` is producing
sane output — even without deep ALMA expertise.

Download:

.. code-block:: text

   https://casa.nrao.edu/download/devel/casavis/data/twhya_selfcal_contsub_ms.tar.gz

.. note::

   This is a different test MS than the one used in the ``visplot``
   regression tests (``sis14_twhya_calibrated_flagged.ms``, ALMA Band 7,
   one spectral window of 384 channels, 26 antennas with data, linear
   feeds, pre-existing flags). The two should not be confused when
   comparing screenshots or reported behavior.  VLBI behaviour is checked
   against a subset of the VLBA tutorial data set TL016B (10 stations,
   four 128-channel spectral windows at 5 GHz, RR/LL, uncalibrated).

Launching visplot
------------------

``visplot`` is invoked as a CASA6-style task, from an interactive
Python or CASA6 session:

.. code-block:: python

   from cubevis import visplot

   visplot(
       ms          = "twhya_selfcal_contsub_ms",
       field       = "5",
       spw         = "0",
       correlation = "XX,YY",
       datacolumn  = "data",
       mode        = "both",
       layout      = "side",
   )

The call above does everything: it builds the app, displays the Bokeh
UI, and runs it — there's no separate step to show the plot.

Task arguments (preview)
`````````````````````````

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Argument
     - Type
     - Meaning
   * - ``ms``
     - str, optional
     - Path to an MSv2 measurement set. Exactly one of ``ms`` or ``ps``
       must be given.
   * - ``ps``
     - str, optional
     - Path to an MSv4 / Processing Set Zarr store. Exactly one of
       ``ms`` or ``ps`` must be given.
   * - ``backend``
     - str
     - Reduction backend: ``"auto"`` (default), ``"casa6"``,
       ``"radps"``, ``"remote"``, or ``"null"``. ``"remote"`` requires
       a ``remote_endpoint``, which is not yet user-exposed in the
       preview.
   * - ``field``
     - str
     - Field selection, CASA-style selection syntax (field name or
       ID). Default: first field.
   * - ``spw``
     - str
     - Spectral window selection, CASA-style (comma list, ``a~b``
       ranges, channel selection). Default: all.
   * - ``antenna``
     - str
     - Antennas and baselines to tick initially in the sidebar's
       *Antenna* and *Baseline* tables: names or numbers
       (``"DA41,DA43"``), ranges (``"DA41~DA46"``), exclusion
       (``"!DA42"``), and exact baselines (``"DA44&DV19"``), separated
       by ``,`` or ``;``. Ticked antennas select their baselines to
       every other antenna; ``!`` selects only baselines between the
       remaining antennas; ticked baselines select exactly those.
   * - ``scan``
     - str
     - Scans, CASA style: ``"1,3,7"``, ``"1~7"``, ``">30"``, ``"!5"``
       (all but 5). The sidebar's *Scan* box starts with it.
   * - ``timerange``
     - str
     - UTC time range, CASA style: full dates
       (``"2012/11/19/07:37:00~2012/11/19/08:00:00"``), times of day
       (``"07:37:00~08:00"``, on the day of the data), open ends
       (``">08:00"``, ``"<08:30"``), a start and a length
       (``"08:00:00+00:10:00"``), AIPS-style day numbers
       (``"1/00:10:00"``, as the time axis labels data that cross
       midnight), or one time (the integration around it). One range.
       The *Time range* box starts with it.
   * - ``uvrange``
     - str
     - UV-distance ranges, CASA style: ``"0~50klambda"``,
       ``"100~300m"``, ``">200m"``, ``"<1.5km"``; several with commas.
       Units m, km, lambda, klambda, Mlambda (metres if none);
       wavelengths are taken at each spectral window's centre
       frequency. Whole rows (one baseline at one time) are in or out.
       The *UV range* box starts with it.
   * - ``autocorr``
     - str
     - ``"exclude"`` (default; cross-correlations only), ``"include"``
       or ``"only"``. On VLBI data before amplitude calibration the
       autocorrelations are hundreds of times the cross-correlations and
       flatten every plot of all baselines. A baseline named in
       ``antenna`` (``"BR&BR"``) is plotted whatever this says.
   * - ``correlation``
     - str
     - Comma-separated correlation labels, e.g. ``"XX,YY"``. Default:
       all.
   * - ``datacolumn``
     - str
     - ``"data"`` (default), ``"corrected"``, or ``"model"``,
       depending on what columns are present in the MS.
   * - ``averaging``
     - str
     - How raster cells combine the samples they cover: ``"vector"``
       (default) averages the complex visibilities and then takes
       amplitude or phase, so amplitude drops where the samples are
       incoherent; ``"scalar"`` averages the amplitudes themselves.
       Sets the initial value for every raster panel; each panel's
       gear tab has its own *Averaging* control.
   * - ``baseline_combine``
     - str
     - How a raster cell that covers several baselines combines them
       (where Baseline is not a plot axis, e.g. Time vs Channel with no
       single baseline ticked): ``"mean"`` (default) of the baselines'
       amplitudes, ``"median"`` (what most baselines share, unmoved
       by a few bad ones), ``"max"`` (the largest, so interference on a few
       baselines is not diluted), or ``"coherent"`` (the visibilities
       are added first; for a calibrated point source). ``averaging``
       applies to the samples within each baseline. Initial value for
       every raster panel; each panel's gear tab has its own *Baselines
       combined* control.
   * - ``baseline_order``
     - str
     - How a raster lays out its Baseline axis: ``"number"`` (default),
       in the order of the sidebar's Baseline table, or ``"length"``,
       shortest first, with the ticks reading the length. Initial value
       for every raster panel; each panel's gear tab has its own
       *Baseline order* control.
   * - ``detrend``
     - bool
     - For the *Phase RMS* and *Coherence* raster quantities: remove a
       linear phase slope (residual delay or rate) before the statistic
       is taken. Default ``True``. Initial value for every raster panel;
       each panel's gear tab has its own *Phase slope* control.
   * - ``stat_time_window``
     - str or float
     - Time window the *Phase RMS* / *Coherence* statistic is taken
       within: ``"auto"`` (default), ``"off"``, ``"scan"``, or seconds.
       Where Time is a plot axis every integration shows its window's
       value; where it is not, the windows are pooled into each cell.
       ``"auto"`` is ``"off"`` in the first case and ``"scan"`` in the
       second. Windows never span a gap between scans.
   * - ``stat_chan_window``
     - str or int
     - Channel window for the same statistic: ``"off"`` (default) or a
       number of channels.
   * - ``layout``
     - str
     - ``"side"`` (side by side), ``"over"`` (stacked) or ``"one"`` --
       initial panel layout (also the *One / Side by Side / Over /
       Under* buttons). ``"raster"`` / ``"scatter"`` mean one panel of
       that kind.
   * - ``row_height``
     - int, str or list, optional
     - Height of each row of plots in pixels (the plot with its axes,
       not the readout and colour bars under it). Default: 550 for One
       and Side by Side, 280 for Over / Under. ``"fit"``: the rows share
       the window's height (never below 250 px; the plot area scrolls
       instead). A list gives one height per row, e.g. ``[250, 400]``
       for Over / Under. Also the *Height* menu beside the layout
       buttons; a height chosen there stays when the layout changes.
   * - ``column_width``
     - int, str or list, optional
     - Width of each column of plots in pixels. Default (or ``"fit"``):
       follow the window. A list gives one width per column (Side by
       Side has two). With ``plotfile`` there is no window: the default
       is 1020 for One and Over / Under, 500 per column for Side by Side.
   * - ``panels``
     - list of dicts, optional
     - Each panel's configuration, in screen order, one dict per panel
       (at most two for now). Keys: ``kind``, ``raster_y``, ``raster_x``,
       ``raster_qty``, ``averaging``, ``baseline_order``,
       ``baseline_combine``, ``detrend``, ``stat_time_window``,
       ``stat_chan_window``, ``scatter_x``, ``scatter_y``,
       ``scatter_avg_time``, ``scatter_avg_chan``, and for a raster
       ``scaling``, ``vmin``, ``vmax``. A key left out takes what the
       flat arguments or ``preset`` give that panel. This is the form the
       plot summary prints (see *Reporting a problem*).
   * - ``time_labels``
     - str
     - ``"utc"`` (default): time axes and the cursor readout give UTC
       time of day, with the date in the axis label; ``"elapsed"``:
       minutes and seconds from the first sample. Also the *Time labels*
       switch.
   * - ``plotfile``
     - str, optional
     - Write a PNG of the plot to this path instead of opening the
       window; the task returns the path. Each plot's data area is
       ``column_width`` by ``row_height``.
   * - ``preset``
     - str, optional
     - Named axis preset: ``"vplot"``, ``"radplot"``, ``"waterfall"``,
       ``"zscore"``, ``"phaserms-time"``, ``"phaserms-freq"``,
       ``"phaserms-uvdist"``, ``"phase-waterfall"``,
       ``"waterfall-all"``, ``"antmatrix"``, ``"spectrum"`` or
       ``"timeseries"``.
       Corresponds to the toolbar preset buttons.
   * - ``time_range``
     - tuple/list of 2 floats, optional
     - ``(start, end)`` in UNIX seconds (UTC): the numeric form of
       ``timerange``. Give one or the other.
   * - ``freq_range``
     - tuple/list of 2 floats, optional
     - ``(start, end)`` in Hz.
   * - ``uvdist_range``
     - tuple/list of 2 floats, optional
     - ``(min, max)`` UV distance in metres: the numeric form of
       ``uvrange``. Give one or the other.

Text in the selection arguments (and the sidebar boxes) that does not
parse, or matches nothing in the data, is refused with a message saying
what is accepted -- at construction for an argument, in the status line
for a box (and nothing is plotted until it is corrected).

All arguments are plain strings, numbers, or simple tuples/lists —
there's no need to construct any internal ``cubevis`` objects to get a
plot on screen.

.. figure:: _static/vp_initial_load.png
   :alt: visplot on first load
   :width: 100%

   TODO(screenshot): ``visplot`` immediately after the call renders,
   before pressing Plot.

Layout tour
------------

The preview window is divided into three regions:

.. code-block:: text

   ┌──────────────────────────────────────────────────────────────┐
   │  Toolbar                                                      │
   ├──────────────┬─────────────────────────────────────────────  │
   │  Sidebar     │  Raster panel  (visible in "both"/"raster")    │
   │  (collapsible)│  Scatter panel (visible in "both"/"scatter")  │
   │              │                                                │
   ├──────────────┴─────────────────────────────────────────────  │
   │  Status bar                                                   │
   └──────────────────────────────────────────────────────────────┘

Toolbar
```````

* **Plot ▶** — run the query against the selected data and (re)render
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
  * *Z-Score* — Baseline vs Time coloured by Z-Score, to spot outliers
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

* Layout — *One*, *Side by Side*, *Over / Under*.  Each preset switches
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
* **Raster axis controls** — X/Y axis choice, quantity and averaging
  (vector or scalar) for the raster panel. Besides amplitude, phase,
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

A commissioning workflow
-------------------------

A first pass over a new observation, in the order the views are useful.
Each step says what the view is *for*; the preset buttons set both
panels at once.

1. **Survey.** Which antennas, baselines and times are wrong?

   * *Ant×Ant* -- one cell per baseline, averaged over the selection. An
     antenna that is bad on every baseline is a whole row and column.
   * *Z-Score* -- Baseline × Time coloured by how unusual each sample is
     for its own baseline; isolated bright cells are outliers in time.
   * *φrms·t* / *φrms·uv* -- phase stability per baseline over time, and
     against baseline length: a rise with length is the atmosphere, one
     high baseline is an antenna.
   * *vplot* -- amplitude per baseline over time: dropouts, bad scans.

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
colour scaling, the displayed range and zoom, the number of pending
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
scatter colour settings, theme), so say if any of those matter.

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
     - Colourize (scatter, per layer)
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
* Selection by data column, field, spectral window, correlation,
  antenna and baseline (tables, or typed text); stepping through
  fields, spectral windows, antennas and baselines with ◀ ▶
* Raster quantities including Phase RMS, Coherence, Z-Score and the
  phase / amplitude differences; vector or scalar averaging; how a cell
  covering several baselines is combined
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

Not yet available:

* Jupyter notebooks (``visplot.notebook()`` says so)
* Correlation as a raster axis (removed from the axis lists; a raster
  shows the first ticked correlation, named in its title -- untick the
  others to see a different one)
* Flagging on the antenna × antenna matrix (refused, with the reason)
* More than two panels
* Locate; on-the-fly calibration; plotting calibration tables
* A ``flagdata`` command export (removed; JSON and writing to the MS
  replace it)

Feedback
---------

If something looks wrong compared to ``plotms``, ``msview`` or AIPS --
inverted axes, wrong amplitude scaling, missing correlations, channels
that don't match the spectral window, times that don't match the
observing log -- that is exactly what testing is meant to catch. Report
it as described in *Reporting a problem*, with the plot summary page.
