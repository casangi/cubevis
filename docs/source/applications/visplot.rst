visplot
=======

.. currentmodule:: applications

.. warning::

   This page documents the **preview** release of ``visplot``.
   The preview supports interactive **display** of visibility data only.
   Flagging (box-select, ⚑ Flag, ⟲ Undo, and writing flags back to the MS)
   is implemented under the hood but is **disabled in the
   GUI** and not yet documented here. A flagging section will be added
   once that workflow is user-facing.

``visplot`` is the ``cubevis`` replacement for CASA6's
``plotms`` (visibility scatter/line plots) and ``msview`` (2-D raster
display of visibility data). It unifies both plot styles into a single
tool that reads MSv2 measurement sets and MSv4 / Processing Set data
and renders them interactively, backed by Datashader for large-dataset
aggregation.

This page is aimed at astronomers evaluating the preview release. It
assumes no familiarity with the ``cubevis`` internals.

Status of this document
------------------------

This is a working draft. It currently covers:

* Launching ``visplot`` and reading its output
* The layout and controls available in the preview
* A basic "getting oriented" workflow using a real, previously-published
  dataset, with side-by-side comparison against the equivalent
  ``plotms``/``msview`` output

It intentionally does **not** yet cover flagging, iteration, averaging,
calibration on-the-fly, or export — none of those are enabled in the
preview.

.. note::

   **Jupyter notebooks are not yet supported in this preview.** Run
   ``visplot`` from a plain interactive Python or CASA6 session
   instead (e.g. ``ipython``, or a ``casa`` prompt). Once notebook
   support lands, the expected workflow will be:

   .. code-block:: python

      from cubevis import visplot
      vp = visplot.notebook(ms="...", ...)
      vp.show()

   This page will be updated to describe that workflow once it's
   available; everything below uses the plain task call, which is
   what the preview supports today.

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

   This is a different test MS than the one used in earlier
   ``cubevis``/``VisibilityRaster``/``VisibilityScatter`` unit tests
   (``sis14_twhya_calibrated_flagged.ms``, an ALMA Band 7 dataset with
   4 SPWs and pre-existing flags). The two should not be confused when
   comparing screenshots or reported behavior.

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
     - MSSelection antenna string. **Accepted and validated, but not
       yet applied to the query in the preview** — see the caveat
       below.
   * - ``scan``
     - str
     - MSSelection scan string. **Not yet wired** — same caveat.
   * - ``timerange``
     - str
     - MSSelection time-range string. **Not yet wired** — same
       caveat.
   * - ``uvrange``
     - str
     - UV range selection string. **Not yet wired** — same caveat.
   * - ``correlation``
     - str
     - Comma-separated correlation labels, e.g. ``"XX,YY"``. Default:
       all.
   * - ``datacolumn``
     - str
     - ``"data"`` (default), ``"corrected"``, or ``"model"``,
       depending on what columns are present in the MS.
   * - ``mode``
     - str
     - ``"both"``, ``"raster"``, or ``"scatter"`` — initial display
       mode (also togglable live in the GUI).
   * - ``layout``
     - str
     - ``"side"`` (side-by-side) or ``"over"`` (stacked) — initial
       panel layout (also togglable live in the GUI).
   * - ``preset``
     - str, optional
     - Named axis preset: ``"vplot"``, ``"radplot"``, or
       ``"waterfall"``. Corresponds to the toolbar preset buttons.
   * - ``time_range``
     - tuple/list of 2 floats, optional
     - ``(start, end)`` as MJD floats — a numeric plot-range hint,
       distinct from the string-based ``timerange`` selection above.
   * - ``freq_range``
     - tuple/list of 2 floats, optional
     - ``(start, end)`` in Hz.
   * - ``uvdist_range``
     - tuple/list of 2 floats, optional
     - ``(min, max)`` in metres.

.. caution::

   ``antenna``, ``scan``, ``timerange``, and ``uvrange`` are accepted
   by ``visplot`` and pass type validation, but are **stored without
   effect** in the preview — they do not currently filter what gets
   plotted. Passing a value for one of these will not raise an error,
   which can be misleading. Use ``field``, ``spw``, and ``correlation``
   for selection in this release.

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
* Display mode — Both / Raster only / Scatter only
* Layout — Side by Side / Over Under
* Presets — quick axis presets (e.g. amp-vs-time style, amp-vs-uvdist
  style, waterfall)
* Pan / Zoom / Reset (shared across both panels when both are shown)
* Box Select — draws a selection region on either panel (currently
  feeds only the disabled flagging path — see the warning at the top
  of this document)
* Flag ⚑ / Undo ⟲ — **present but disabled** in the preview

Sidebar
```````

* **Data selection** — field, SPW, correlation, data column
* **Raster axis controls** — X/Y axis choice and quantity for the
  raster panel
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

A basic verification workflow
-------------------------------

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

4. **Toggle Both / Raster only / Scatter only, and Side by Side / Over
   Under.** Purely a preview-mechanics check — confirms the CustomJS
   toggles are wiring up the right Bokeh containers.

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
reference). The preview's sidebar dropdowns are not a 1:1 match yet
(fewer axes are exposed), but the concepts line up as follows:

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
     - *not yet exposed*
     - planned
   * - ``amp`` / ``amplitude``
     - Amplitude
     - 
   * - ``phase``
     - Phase
     - 
   * - ``real`` / ``imag``
     - *not yet exposed*
     - planned
   * - ``uvdist``
     - UVdist
     - 
   * - ``corr`` / ``correlation``
     - Correlation (selection, not yet a plottable/colorize axis)
     - 
   * - ``flag``
     - *n/a in preview*
     - flagging is disabled; no flag-state axis yet
   * - ``coloraxis``
     - *not yet exposed*
     - colorize-by-axis is planned; preview colors by layer only

Preview scope, explicitly
---------------------------

Present and working:

* Raster and scatter display, MSv2 and MSv4/Processing Set backends
* Data/axis/colormap selection sidebar
* Display-mode and layout toggles
* Shared pan/zoom/reset, linked X range when applicable
* Session-scoped layout preference memory

Not yet available (tracked for the full release):

* Flagging: box-select currently accumulates into ``FlagDB`` internally
  and drives a red-overlay re-render, but the Flag/Undo toolbar buttons
  are disabled and nothing is written back to the MS
* ``antenna``, ``scan``, ``timerange``, and ``uvrange`` selection: the
  ``visplot`` task accepts and validates these arguments, but they are
  not yet applied to the query — only ``field``, ``spw``, and
  ``correlation`` currently filter what's plotted
* Iteration (Prev/Next by antenna, baseline, scan, etc.)
* Locate
* Save plot / image export
* Copy-flagdata-command export
* Averaging controls (channel, time, baseline)
* On-the-fly calibration
* Colorize-by-axis, multiple simultaneous Y axes
* Jupyter notebook support (``visplot.notebook(...)``) — not available
  in this preview; see the note near the top of this document

Feedback
---------

If something in this preview looks visibly wrong compared to the
``plotms``/``msview`` reference plots above — inverted axes, obviously
wrong amplitude scaling, missing correlations, channels that don't
match SPW metadata, etc. — that's exactly the kind of thing this
verification pass is meant to catch. Note the discrepancy against the
relevant screenshot pair in this document and file it against the
``visplot`` preview implementation.
