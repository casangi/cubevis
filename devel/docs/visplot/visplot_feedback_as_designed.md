# visplot pre-preview feedback: behaviour that is as designed

*Discussion document, 2026-11-18, `debug-0001`.  For the feedback review
meeting of 2026-11-19.*

Several items in the first round of pre-preview feedback describe
behaviour that is implemented on purpose and works, but that the tester
expected to work differently.  Those items are collected here, each with
what was reported, what visplot actually does (checked against the code
on `debug-0001`), and what is left to decide.  The aim is to settle, per
item, whether the answer is **documentation**, a **UI wording / layout
change**, or a **real change in behaviour**.

Bugs and small fixes from the same feedback are tracked separately (see
[Tracked elsewhere](#tracked-elsewhere) at the end) and are not discussed
here.

| # | Feedback item (paraphrased) | Topic | Proposed answer |
|---|---|---|---|
| 1 | "Baselines combined: Coherent and Averaging: Vector seem redundant" | [Averaging vs Baselines combined](#1-averaging-vs-baselines-combined) | Docs + wording; one real open question |
| 2 | "Time and Channel window could be more general, allow typed values" | [Statistic windows](#2-time-window-and-channel-window) | Docs; generalisation is topic 3 |
| 3 | "Phase RMS is very specific; prefer max / mean / min / std / sum / var aggregation" | [Phase RMS vs aggregation](#3-phase-rms-vs-a-general-aggregation) | Keep Phase RMS; aggregation is a separate feature request |
| 4 | "Correlation shown seems redundant with the correlation selection" | [Correlation shown](#4-correlation-shown-vs-the-data-sections-correlation-ticks) | Docs; one UI option to decide |
| 5 | "Data selection lacks channel selection" | [Channel selection](#5-channel-selection) | Partly exists (`freq_range=`); GUI support is a feature request |
| 6 | "A text field needs a focus change before Plot" | [Stage, then plot](#6-stage-then-plot-and-committing-a-text-field) | As designed; docs only |
| 7 | "Can Grow around be previewed?  What does a filter do with aggregated data in the box?" | [Flagging](#7-flagging-preview-filters-and-what-a-box-covers) | Works; docs |
| 8 | "Some raster options do not apply to scatter and vice versa" | [Raster vs scatter options](#8-raster-and-scatter-options) | As designed; docs |
| 9 | "Tooltips are too long, with no way to scroll" (framing only) | [Where help appears](#9-where-help-appears) | Explain the mechanism; the length fix is tracked elsewhere |

---

## 1. Averaging vs Baselines combined

**Reported.** *Baselines combined: Coherent* and *Averaging: Vector* look
related and possibly redundant: is one over time and one across
baselines?

**What visplot does.** Yes, they answer different questions, and the
code does them as two steps
(`data/_raster_average.py: reduce_amp_phase_baselines`):

1. **Averaging** (*Vector* / *Scalar*) combines the samples of **one
   baseline** inside a raster cell: the times and channels the cell
   covers.  *Vector* averages the complex visibilities and then takes
   amplitude/phase, so amplitude drops where the samples do not line up.
   *Scalar* averages the amplitudes, and for phase takes the circular
   mean.
2. **Baselines combined** (*Mean* / *Median* / *Maximum* / *Coherent*)
   applies only when a cell covers **several baselines**, i.e. a raster
   without Baseline on an axis (Time × Channel with no single baseline
   ticked, the *All-BL* preset).  It combines the per-baseline results
   from step 1.

If Baseline is a plot axis, *Baselines combined* has no effect at all.

| | Mean / Median / Maximum | Coherent |
|---|---|---|
| **Vector** | each baseline vector-averaged, then the baselines' amplitudes combined (baselines weighted equally) | every sample of every baseline added as complex numbers, then the amplitude taken (samples weighted equally) |
| **Scalar** | each baseline scalar-averaged, then combined (baselines weighted equally) | mean of \|V\| over every sample of every baseline (samples weighted equally) — **not coherent** |

For Phase, Mean, Median and Maximum all give the mean direction of the
baselines; "median" and "max" have no meaning for a direction.

**Open question for the meeting.** The feedback is partly right.
*Coherent* is implemented as "skip step 2 and run step 1 over the
baselines too" (`reduce_amp_phase` over every reduced dimension).  So
*Coherent* with *Scalar* averaging is a sample-weighted scalar mean, and
the label is misleading.  Options:

* (a) disable *Coherent* (or show a warning) while Averaging is *Scalar*;
* (b) rename it to describe what it does, e.g. *All samples together*,
  with the hint explaining that with Vector this is the coherent sum;
* (c) leave the behaviour and fix only the hint text.

**Also to fix (tracked elsewhere).** The Averaging hint says vector
averaging is "what AIPS and plotms do".  plotms offers scalar averaging
too; the hint should say vector is the *default* in both.

## 2. Time window and Channel window

**Reported.** The time and channel windows seem useful but limited to
certain modes; could they be more general, and accept typed values?

**What visplot does.** The windows are settings **of the Phase RMS,
Coherence and Diff quantities**, not of the raster in general
(`data/_raster_stats.py`, module docstring "Windows").  A Phase RMS value
needs a set of samples to measure scatter over, and the windows choose
that set:

* along a **displayed** axis, a window groups neighbouring cells, the
  statistic is taken within it, and every cell in the window shows that
  value.  The picture becomes blocky at the window size.  This is what
  makes Phase RMS possible on a single-baseline Time × Channel waterfall,
  where one cell is otherwise one sample;
* along a **reduced** axis, the windows are **pooled**: each window is
  measured about its own mean phase (and slope) and the results added.
  So for Baseline × Channel, per-scan windows stop phase jumps between
  scans from counting as scatter.

*Auto* (time) means no window where Time is displayed and one scan where
it is not.  Time windows never span a gap between scans.  For *Amp V
Diff* / *Phase Diff* the time window is the stretch the reference mean is
taken over.

For Amplitude, Real, Imaginary etc. there is no window: the cell itself
is the averaging unit, and the grid resolution sets its size.

**Typed values.** Any value already works from Python:
`stat_time_window=45` (seconds) or `stat_chan_window=12` (channels), and
the GUI dropdown then gains a "45 s" entry
(`visibility_plotter.py: _window_options`).  The GUI itself offers only
the fixed list.

**For discussion.** (a) Allow free entry in the GUI (an editable field
next to the dropdown)?  (b) Should the controls be hidden, rather than
labelled "(RMS / Coherence / Diff)", when the quantity does not use them?
Making windows apply to every quantity is the aggregation request in
topic 3.

## 3. Phase RMS vs a general aggregation

**Reported.** Phase RMS seems too specific to be a quantity of its own;
better to support it through quantity + aggregation (max, mean, min, std,
sum, var), as the MSraster prototype did.

**What visplot does.** Phase RMS is not "std of phase", which is why it
is its own quantity (`data/_raster_stats.py`):

* it is a **circular** statistic: deviations about the mean phase
  *direction*, wrapped into (−180°, 180°] before squaring, every unflagged
  sample as a unit phasor.  A plain std of phase is wrong for any cell
  straddling ±180°;
* by default (*Phase slope: Remove*) it first removes a linear phase
  slope (residual delay or rate), so un-fringe-fitted data show their
  scatter rather than their slope;
* it uses the pooled windows of topic 2.

*Coherence* (|mean V| / mean |V|) carries the same information as a
fraction (the AIPS EDITR coherence display).

**For discussion.** A general per-cell aggregation (`max`, `min`, `std`,
… of the chosen quantity) is a reasonable **feature request**, separate
from Phase RMS.  Part of it already exists for the case of several
baselines (Baselines combined: Median / Maximum).  The questions are
scope (raster only? scatter averaging too?) and what `std` of Phase
should mean (circular, presumably, so it would become Phase RMS without
slope removal).  This is a planning item, not a bug.

## 4. Correlation shown vs the Data section's correlation ticks

**Reported.** *Correlation shown* seems redundant with the correlation
selection, and *Parallel hands* seems redundant too.

**What visplot does.** They do different jobs:

* the **Data section's Correlation ticks** select what is read for the
  page.  For a **scatter**, each ticked correlation becomes its own layer
  with its own colors;
* a **raster** shows **one** correlation at a time, so each raster panel
  has its own *Correlation shown*:
  * *First ticked* (default) follows the Data ticks, and is named in the
    title;
  * *Parallel hands* is (XX+YY)/2 or (RR+LL)/2, a Stokes-I-like view.  A
    sample counts only where both hands are unflagged, and a flag box
    drawn on it flags both hands (`data/reader.py: select_polarization`);
  * a named product (e.g. LL) is read **whether or not it is ticked**:
    the raster replaces the selection's correlation list with its own
    (`correlation_for_query`).  So Panel A on RR and Panel B on LL works
    with only one hand ticked.

**For discussion.** The confusing part is the last point: a raster can
show a correlation the Data section says is not selected.  Options:
(a) document it (current behaviour); (b) limit the list to the ticked
correlations plus *Parallel hands*.  (b) is simpler to explain, but
showing RR and LL side by side would then need both ticked, which also
gives the scatter two layers.

## 5. Channel selection

**Reported.** The Data selection lacks channel selection.

**What visplot does.** A **frequency range** is supported, but only at
start-up: `visplot(..., freq_range=(f_lo, f_hi))` in Hz applies to every
SPW.  The data layer also has a `channel_range` (`selection.py`,
`SelectionSpec.channel_range`), but nothing in the plotter sets it, and
it is one zero-based range applied to every SPW.  There is no
GUI control, and no CASA-style `spw='2:10~50'` syntax.

**For discussion.** This is a real gap and a feature request.  The design
questions: per-SPW channel ranges (SPWs can have different channel
counts per partition), CASA `spw` syntax in the SPW text box, and keeping
flag write-back in absolute channel numbers.  It belongs in a planning
session.

## 6. Stage, then plot, and committing a text field

**Reported.** Changing a text field needs a focus change before pressing
Plot.

**What visplot does.** Sidebar controls **stage** their values and the
plot is redrawn only by Plot ▶, Reload or a Prev/Next button.  This is
deliberate: live redraws on every edit would mean an expensive round
trip per keystroke or click, and would hit the empty-selection guards in
the middle of an edit.  Plot ▶ turns orange when something is staged but
not drawn.  Color scaling is the exception and applies at once, because
it only re-shades the image it already has.

A text field commits its value on **Enter** or when it loses focus.
Clicking Plot ▶ itself takes the focus away, so typing a value and then
clicking Plot normally works without a separate step.

**For discussion.** If a tester can show a case where typing and then
clicking Plot ▶ draws the *old* value, that is a bug and should be
reported with the field's name.  Otherwise: docs only.

## 7. Flagging: preview, filters and what a box covers

**Reported.** Can options like *Grow around* be previewed?  Does the
flagging filter act mostly on the spatial data in the box?  How does it
treat the aggregated data in the box?

**What visplot does.** The pipeline is **box → filter → proposal →
review → pending flags** (`flag_filters.py`, `flag_controls.py`):

* **The box** selects every sample *behind* what is drawn: for a raster,
  every time, channel and baseline that the enclosed cells average over,
  not the cells' averaged values.  *Flag reaches* (Baselines, All
  channels, All selected SPWs, Whole scan, All fields, All correlations)
  extend that set beyond the box.  An amber outline and note say when
  any reach is wider than the box.  The Baselines, SPW, Scan and Field
  reaches are applied only to a **raster** box with the *All selected*
  filter; for any other box the panel says they were not applied.
* **The filter** then judges those **individual samples** and keeps only
  the ones that match: *Value range*, *Outlier from neighbours*,
  *Z-Score above cutoff*, *Amplitude outlier (MAD)*, *Phase deviation*,
  *Grow around flags*, or *All selected* (everything, the AIPS TVFLG
  style).  The one exception is *Z-Score*'s **Match per: cell** setting,
  which judges a raster cell as the Z-Score coloring shows it (the
  default *auto* does this on a Z-Score raster).
* **Preview each proposal** shows, before anything is recorded, what the
  box would change after the reaches **and** the filter: how many
  samples, on which baselines, antennas, windows and scans, painted in
  orange on both plots.  So *Grow around flags* can be previewed like any
  other filter.

*Grow around flags* is a filter, not a reach.  It takes unflagged
samples next to already-flagged ones (on disk or pending): flag the core
with one box, then draw a second box with this filter (CASA's
`extend`).

**For discussion.** Docs only, but this is the area where a short
end-user page (the pipeline in five lines, with one example) would save
the most confusion.

## 8. Raster and scatter options

**Reported.** Some raster options do not apply to scatter and vice versa.
Should options appear based on the plot type, or always appear for both?

**What visplot does.** They already appear by plot type.  Each panel's
configuration tab has a **Raster / Scatter** switch, and only the chosen
kind's options are shown (`visibility_plotter.py`, the `kind_switch`
wiring).  Each kind keeps its own settings while hidden, so switching a
panel back restores them.  The grid's own panel tab follows the same
rule.

The two kinds have different controls because they work differently:
a raster reduces everything onto a fixed grid of cells (Averaging,
Baselines combined, Baseline order, Correlation shown, Color scaling with
histogram).  A scatter draws one point per sample, or per averaged
sample (Average over time / channels, Vector / Scalar, Layer coloring).

**For discussion.** Is the switch visible enough?  Should the tab's
heading name the kind ("Panel A: raster")?

## 9. Where help appears

**Reported** (as part of a length complaint). Tooltips such as *Baselines
combined* are too long to fit, with no way to scroll.

**What visplot does.** These are not pop-up tooltips.  Help for the
control under the pointer appears in the fixed-height status area at the
bottom of the window (five lines), so showing help never moves the
layout.  Because it follows the pointer, a scrollbar cannot help: the
text changes as soon as the pointer leaves the control to scroll.

**Tracked elsewhere.** Several texts are much longer than five lines
(characters, without markup: Flag reaches 1206, Baselines combined 1148,
Averaging (scatter) 833, Raster axes 631).  The proposed fix is to cut
them to fit and add a test that enforces a length limit.  The
alternative, a click-to-pin help panel, is for discussion.

---

## Tracked elsewhere

The rest of the first feedback round is being handled as fixes on
`debug-0001` or as planning items, not discussed above:

* **Fixed on `debug-0001` (2026-11-18)**, checked in headless Chrome
  against a simulated MS:
  * the equation label follows dark / light mode;
  * the colormap histogram, the min/max lines and fields, and the
    scaling dropdown follow every Plot (they used to show the first
    quantity drawn), and Reset uses the current histogram
    (`colormap_sync.py`);
  * a scaling shown by a reply no longer echoes back to Python as a
    live change (this also affected the grid drill-down);
  * *threshold* shows one line and one field, titled "cutoff";
  * the Averaging hint no longer says vector is "what AIPS and plotms
    do": it is their default, and both offer scalar too;
  * no browser autofill suggestions in any text field;
  * Baselines combined: *Coherent* is now *Vector (coherent)* and is
    offered only with Vector averaging (topic 1, option a).
* **Not reproduced:** dragging a histogram line updated its field in
  every attempt (Chrome, including after closing and reopening the
  tab).  The Flagged / Pending color pickers are not re-rendered when
  the help text changes, the obvious cause of a closing popup; it needs
  the tester's browser and OS.  Spelling already fixed.
* **Agreed, not yet done:** over-long help texts, cut to five lines with
  "press ? for the full explanation" opening the long text in a new tab
  (one source for the in-app help and the user docs).
* **Agreed, not yet done:** SPW / Antenna / Baseline Prev / Next step
  through the ticked rows; the ticks stay the selection and the current
  row gets its own marker.
* **Feature requests for planning:** Scan and Correlation iteration;
  Time (integration) iteration; GUI channel selection; per-cell
  aggregation; the iterated value in the plot title.
