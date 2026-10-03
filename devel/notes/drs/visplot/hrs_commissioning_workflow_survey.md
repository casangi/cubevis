# HRS: commissioning and flagging workflow survey, mapped to visplot

*Written 2026-10-02 against `casangi/cubevis` `main` at `3b5eb82`.
Companion: `hrs_visplot_plan.md` (the plan this survey feeds).*

Purpose: the HRS requirement (CASR-385) names AIPS tasks and a "phase rms"
plot without definitions, and the stakeholders cannot be asked in detail.
This note records what those AIPS tasks actually do, what commissioning
users of a VLBI-style array routinely look at, and how each item maps onto
visplot today. It ends with a gap list that the plan turns into milestones.

Confidence markers used below:

- **[doc]** taken from the AIPS CookBook (31DEC26 HTML edition) or the AIPS
  change log; section numbers given.
- **[code]** read directly from the visplot source at `3b5eb82`.
- **[recall]** from general knowledge of AIPS/CASA practice, not re-checked
  against a primary source in this session. Treat as likely but confirm
  before designing against the detail.

Sources: AIPS CookBook §4.3.5 (general flagging), §5.5.1-5.5.2 (editing
tools, EDITR), §O.1.5-O.1.6 (LISTR/UVFLG, TVFLG menu), Appendix C.7 (VLBA
calibration); AIPS `CHANGED.13D` (FTFLG introduction). The CookBook defers
to "AIPS Memo 127" for the current TVFLG/SPFLG details; that memo was not
read and is the first thing to consult if a menu detail matters.

---

## 1. The three AIPS editors named in the requirement

All three are the same program pattern: build a gridded copy of the selected
data (the "master grid"), show a 2-D grey-scale image of it, let the user
mark regions, accumulate flag *commands* in a list that can be listed and
undone, and write them to the data set's flag table only on exit. They
differ only in which two axes are displayed.

| Task | Image axes | What one image covers | Typical use |
|---|---|---|---|
| TVFLG | baseline (x) by time (y) | one IF, one Stokes, one channel (or channel average) | bad antennas, bad baselines, bad time ranges; continuum |
| SPFLG | channel, all IFs side by side (x) by time (y) | one baseline, one Stokes | channel-dependent problems and RFI, per baseline |
| FTFLG | channel/IF (x) by time (y) | all baselines combined, one Stokes | fast survey for RFI common to all baselines |

**[doc]** §4.3.5 for the three coverage statements; §O.1.6 for TVFLG's
axes; §O.1.7.2 for SPFLG showing "spectral channels for all IFs on the
horizontal axis, one baseline at a time".

### 1.1 TVFLG in detail **[doc §O.1.6]**

Display quantities (menu column 3):

- amplitude; phase
- rms of amplitude; rms/mean of amplitude
- vector rms; vector rms / vector average
- **AMP V DIFF**: amplitude of the vector difference between a sample and a
  running vector "scan average" around it
- **AMPL DIFF**: |amplitude minus running scalar-average amplitude|
- **PHASE DIFF**: |phase minus phase of the running vector scan average|

Two time parameters are user-set: the *smooth time* (averaging used to form
the displayed image) and the *scan time* (length of the running average used
by the DIFF modes; a rolling buffer centred on each sample). The CookBook's
advice: amplitude finds long-lived problems, AMP V DIFF finds short
excursions and is sensitive to both amplitude and phase problems; the rms
modes need averaging and so blur flagging in time; for phase use a circular
colour table.

Baseline axis: ordered by baseline number, switchable interactively to
**sort by length**. An input option shows each baseline twice (1-2 and 2-1)
so that all baselines to an antenna form a contiguous block.

Flag operations (menu column 4):

- single pixel (with or without confirmation); rectangular area
- a time, or a time range, for **all baselines**
- a time range for **all baselines to one antenna** (ANTENNA-DT)
- all times for one baseline; a time range for one baseline
- clip: by typed limits, interactively, or "by form" (re-apply a previous
  clip's quantity/averaging/limits to other channels/IFs/Stokes)

Flag *scope* switches that apply to subsequent commands: which Stokes are
flagged (need not be the displayed one); this channel or all channels; this
IF, a range, or all IFs; this source or all sources.

Bookkeeping: LIST FLAGS, UNDO FLAGS (by command number), REDO FLAGS, SET
REASON (a string stored with each flag command), flags applied to the data
only on EXIT after a yes/no. The command list is saved as it grows, so an
abrupt exit does not lose the session.

### 1.2 SPFLG **[doc §5.5.1, §O.1.7.2; menu details recall]**

Same program as TVFLG with channel on the x axis and baselines stepped
through one at a time. The CookBook calls it "the ultimate tool" for wide
band data but notes the cost: every baseline is a separate image, so with
many baselines users look at a few short and one or two long baselines to
find problems and then flag more generally. **[recall]** The flag operations
mirror TVFLG's with channel replacing baseline (flag a channel for all
times, a channel range for a time range, and so on), and the flag-scope
switch controls whether a flag applies to the displayed baseline, all
baselines to one of its antennas, or all baselines.

### 1.3 FTFLG **[doc §4.3.5, §5.5.1, CHANGED.13D]**

Introduced April 2013 as "a version of SPFLG that puts all baselines in a
single plane". Consequences stated in the documentation: it is a much faster
way to look for global RFI; every flag it generates applies to all
baselines; because it averages baselines, "a few bad baselines can make it
look like all are bad and cause you to flag too much", so channels that look
bad in FTFLG should be checked elsewhere before flagging.

### 1.4 The editor VLBI users actually reach for: EDITR **[doc §5.5.2]**

Not named in the requirement, but the CookBook recommends it over TVFLG for
arrays with a modest number of antennas (VLBI, MERLIN), and Appendix C.7
lists it first among the tools VLBA users inspect data with. It is
line-plot based, not image based:

- one *main antenna*; a stack of value-vs-time plots for up to 11 baselines
  to that antenna, the bottom one being the edit window; NEXT BASELINE and
  NEXT ANTENNA step through
- quantities: amplitude, phase, amplitude of the difference from a running
  vector mean, and a coherence parameter (scalar-average amplitude over
  vector-average amplitude, minus one, over the scan length)
- a second observable for the edit baseline can be stacked above it (for
  example phase above amplitude)
- flag a time, a time range, a box in value-time, everything above or below
  a value, or point by point
- scope rotates between **one baseline / all baselines to the main antenna /
  all baselines**; one or both polarizations; one, some or all IFs; this
  source or all sources
- value-based flags are converted to value-independent commands (time,
  antenna, IF, polarization) at the moment they are made, and it is those
  that are listed, undone and redone
- optional second data set (for example residuals) overplotted for
  comparison

---

## 2. "Phase rms vs time and frequency"

A correction to what I said when this work was scoped: I claimed AIPS's
TVFLG/SPFLG rms displays settle the definition. They do not, quite. The rms
modes in TVFLG are rms of **amplitude** and a **vector** rms **[doc
§O.1.6]**; the phase-specific mode is PHASE DIFF. No AIPS display is
literally "rms of phase". Likewise CASA's plotms has no phase-rms axis
**[recall]**; CASA users get phase scatter from `visstat` or by eye from
phase-vs-time plots.

What the AIPS precedent does establish is the *shape* of the computation: a
statistic of the samples inside a user-set window (smooth time), optionally
relative to a running vector average (scan time), shown on the same
time-baseline and time-channel images used for flagging.

What commissioning staff use phase scatter for **[recall]**:

1. **Phase stability in time, per baseline.** Scatter of phase over a short
   window (seconds to a few minutes) on a strong calibrator. Shows
   atmospheric and LO/maser instability and tells you the coherence time.
   Plotted vs time it shows when conditions or equipment changed.
2. **Phase stability vs baseline length.** The same number plotted against
   baseline length; rising scatter with length is atmosphere, a single
   discrepant antenna is equipment. This is the standard array phase
   stability plot.
3. **Phase scatter across the band, per baseline.** Scatter of phase over
   channels after removing a linear slope. A residual slope is a delay
   error; scatter about the slope is bandpass phase ripple or low
   signal-to-noise. Plotted vs frequency (per channel, scatter over time) it
   shows which parts of the band are unstable or contaminated.
4. **Coherence loss.** The ratio of vector-averaged to scalar-averaged
   amplitude over a window. For small phase noise this is exp(-sigma^2/2), so
   it carries the same information as phase rms and is what EDITR's
   coherence display and IBLED's "decorrelation index" **[doc §5.5.1]**
   expose. VLBI users may well mean this when they say phase rms.

Proposed definition for visplot (decision recorded in the plan):

- Statistic: circular standard deviation of the unflagged samples in the
  window, in degrees, computed from unit phasors so it is correct across
  the +/-180 degree wrap. Reported alongside, from the same sums, the
  coherence ratio |vector mean| / scalar mean.
- Window: a time length and a channel count, both user-set, default "one
  scan" and "one SPW" **[open: sensible defaults need real HRS data]**.
- Detrending option, default on for the frequency direction: remove the
  phase of the vector mean (always) and optionally a linear phase slope
  across the window (delay in frequency, rate in time) before taking the
  scatter. Without slope removal an uncorrected delay dominates the number
  and hides what the user is looking for.
- Views: (a) raster quantity on Time x Baseline and Time x Frequency;
  (b) scatter of windowed rms vs time and vs frequency, the literal
  requirement; (c) scatter of rms vs baseline length.

---

## 3. Other plots commissioning users expect

**[recall]** unless marked. This is the working set for bringing up a VLBI
array; it is what AIPS VLBI procedures and tutorials use at each step.

| Plot | AIPS task | What it answers |
|---|---|---|
| Amplitude and phase vs frequency, per baseline, vector-averaged over a scan, all IFs across the page | POSSM | Are there fringes? Residual delay (phase slope), bandpass shape, dead IFs, RFI |
| Autocorrelation spectra per antenna | POSSM | Bandpass shape, RFI, sampler/IF problems, independent of fringes |
| Amplitude and phase vs time, per baseline | VPLOT | Phase winding (residual rate), dropouts, late-on-source |
| Amplitude vs uv distance; uv coverage | UVPLT | Calibration consistency; coverage |
| Calibration solutions vs time per antenna: delay, rate, phase, amplitude, Tsys | SNPLT | Clock/LO behaviour, weather, pointing |
| Fringe-rate / delay spectra | FRPLT | Detailed fringe search |
| Scan-averaged amplitude and rms matrices, antenna by antenna | LISTR 'MATX' **[doc §O.1.5]** | A whole bad antenna shows as a row and column |

The antenna-by-antenna matrix is worth noting: it is the oldest "find the
bad antenna" tool and is exactly a raster of ANTENNA1 x ANTENNA2 with an
amplitude or rms quantity.

A point that affects several of these: VLBI data before fringe fitting have
residual delay and rate, so **vector averaging** over the chosen time and
channel range is what users expect; scalar averaging of amplitude hides
decorrelation and averaging phases arithmetically is meaningless across a
wrap. AIPS exposes the choice everywhere (POSSM, LISTR, TVFLG's scalar and
vector rms pairs).

---

## 4. Mapping onto visplot at `3b5eb82`

### 4.1 Views

| Need | visplot today | Evidence | Gap |
|---|---|---|---|
| TVFLG image (baseline x time) | Time x Baseline raster; `vplot` preset | **[code]** `query_raster` docstring, presets | Baseline order is by id only; no sort by length (no such code found) |
| SPFLG image (channel x time, one baseline) | Time x Channel raster needs a single baseline through `selection.baselines`; `waterfall` preset | **[code]** `msv2_backend.query_raster` | No baseline Prev/Next: iteration handlers exist for Field, SPW and Antenna only. Multi-SPW side by side: channel axis is per SPW |
| FTFLG image (channel x time, all baselines) | Not offered: Time x Frequency is restricted to one baseline | **[code]** same | Allow baseline to be a reduced dimension |
| LISTR matrix (antenna x antenna) | `ANTENNA1`, `ANTENNA2` axes exist in the vocabulary | **[code]** `axes.py` | Raster support for that pair not verified; assume absent |
| POSSM (amp/phase vs frequency) | Scatter with Frequency/Channel x, per-sample | **[code]** axes | No averaging, so no scan-averaged spectrum; no stacked amp+phase pair as one preset |
| VPLOT (amp/phase vs time) | Scatter, `vplot` preset pairs it with the raster | **[code]** | Same averaging gap |
| Autocorrelations | Unknown | not checked | Verify selection can include/exclude autos and that they plot |
| Calibration tables (SNPLT) | Axis names reserved (`GAIN_PHASE`, `DELAY`, `TSYS`...) but the reader has no implementation beyond a dispatch stub | **[code]** `reader.py` | Out of HRS scope as written; note for later |

### 4.2 Quantities and averaging

| Need | visplot today | Gap |
|---|---|---|
| Amplitude, phase, real, imaginary, flag fraction, Z-Score | Present in raster and scatter **[code]** | none |
| **Vector averaging** | Raster cells are the arithmetic **mean of per-sample amplitude** (scalar) and the arithmetic **mean of per-sample phase in degrees** **[code]** `_raster_2d`: `da.angle(...)` then `q.mean(...)` | **Phase cells are wrong wherever samples straddle +/-180 degrees, and amplitude cannot show coherent averages.** This is a correctness issue independent of HRS and the highest-value fix in this survey. MSv4 backend assumed to match (parity tests) but not read |
| Phase rms / coherence | Absent | New reduction (section 2) |
| Difference from running mean (AMP V DIFF, PHASE DIFF) | Absent as a display. The *Phase deviation* and *MAD* flag filters compute related statistics for flagging only **[code]** `flag_filters.py` | Display quantity sharing the windowed machinery |
| Time/channel averaging controls | Listed as absent in the plotter's module notes **[code]**; decimation by stride exists but is not averaging | Needed for POSSM/VPLOT style plots and for the smooth-time concept |
| Cyclic colormap for phase | Not checked | Verify; add if missing |

### 4.3 Flagging

| AIPS capability | visplot today | Gap |
|---|---|---|
| Box on image; point | Flag/Unflag boxes on raster and scatter | none |
| Command list, list, undo/redo, apply on exit | FlagDB: ordered, undo/redo/clear, preview, commit with backup and verification; HTML description of pending flags | none; visplot is ahead (unflag, restore from backup, JSON round trip) |
| Reason string per flag | `comment` and `source` fields exist on a delta **[code]** `flag_engine` | Confirm the GUI lets the user set it |
| Scope: all channels, all correlations | Extend checkboxes **[code]** `flag_controls.py` | none |
| Scope: all SPWs/IFs, whole scan | `extend_spw`, `extend_scan` exist in the model and engine **[code]** but no checkbox was found for them in the GUI | Expose |
| Scope: **all baselines to an antenna; all baselines** | Absent | New extend options; this is the core of TVFLG's ANTENNA-DT/TIME RANGE and of FTFLG |
| Scope: all sources/fields | Not found | Add with the others |
| Flag a whole row/column of the image (a time, a baseline, a channel) | Only by drawing a box that spans it | Convenience: click-to-select row or column |
| Clip above/below on the displayed quantity | Amplitude range, Z-Score, MAD, Phase deviation and user filters, with preview | Clip on whatever quantity is displayed (including the new rms/diff quantities); "clip by form" equals re-running a stored filter on another selection |
| Editing while stepping through baselines | No baseline iteration | As 4.1 |
| EDITR stacked baselines to one antenna | Antenna iteration plus colour-by-baseline scatter approximates it | Acceptable for now |

### 4.4 Gap list, in the order the plan takes them

1. Vector averaging for raster cells (and a vector/scalar choice), both
   backends. Fixes phase rasters.
2. Windowed-statistic machinery: circular phase rms, coherence ratio,
   difference from running vector mean; raster and scatter forms; rms vs
   baseline length.
3. Baseline iteration and baseline sort by length.
4. All-baselines Time x Frequency raster (FTFLG view).
5. Flag scope extensions: antenna, all baselines, SPW, scan, field; row and
   column selection; clip on displayed quantity.
6. Time and channel averaging controls; POSSM-style and VPLOT-style
   presets; autocorrelation check.
7. Antenna x antenna matrix view.
8. Static raster output for every interactive view (secondary).

---

## 5. Things I could not establish

- The current SPFLG/FTFLG menus in detail (AIPS Memo 127 not read; the
  on-line HELP gateway could not be queried per task).
- Whether HRS data will arrive fringe-fitted or raw. It decides whether
  slope removal in the phase-rms window is essential or merely useful.
- HRS array size, channel counts, and integration time; these set window
  defaults and whether the per-baseline waterfall is practical or the
  all-baseline view becomes the main one.
- Items marked "not checked" or "not verified" in section 4 need a short
  code-reading pass before the milestone that touches them.
