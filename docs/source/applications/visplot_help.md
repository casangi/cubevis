(vp-help)=

# visplot controls, in full

This page is the long form of the descriptions and warnings in the
`visplot` window's status area: what a **ⓘ** there leads to. Pressing
**?** in the window (or clicking a warning's ⓘ) opens this page at the
entry for what the status area is showing: see {ref}`vp-help-window`.
Some entries are elsewhere in the documentation, where the subject is
already explained, for example {ref}`vp-memory-guard`.

The pages under {ref}`cap-data` and the other capability pages describe
what each feature is for; this page describes each control.

*Updated 2026-11-19 (ⓘ and ?).*

```{contents}
:local:
:depth: 2
```

## Selection

(vp-antenna)=

### Antenna and Baseline

Tick rows to select.

* **Antennas ticked**: their baselines to every other antenna.
* **Baselines ticked**: exactly those; they override the antennas.
* **Nothing ticked**: all antennas, or all baselines.
* The text box under each table sets the ticks from text: for antennas,
  a name or number such as `DA41`, `DA43~DA46`, `0,3,5` or `!DA42`
  (all but); for baselines, two antennas joined by `&` (`DA41&DV01`),
  or the baseline's number (`12`, `10~20`, `!3`). **Enter** replaces
  the ticks and clears the box; if any part matches nothing, nothing
  changes. Then press **Plot**.
* **Prev / Next** (◀ ▶) step through the rows one at a time: see
  {ref}`vp-iteration`.

(vp-iteration)=

### Stepping through Field, SPW, Scan, Antenna and Baseline

Field, SPW, Scan, Antenna and Baseline each have **Prev**, **✱** and
**Next** beside their control (Scan: see {ref}`vp-scan`; it has a text
box rather than a table, and its ▶ is the line under the box). Each step plots straight away, and steps wrap
around at the ends.

* **The ▶ marks the row being shown.** It is that axis's *current
  row*, and the table scrolls to keep it in view. While a row is
  current, the status line gives the position, for example
  "Antenna 4/6: ANTENNA-3".
* **Two or more rows ticked:** Prev and Next step through only those
  rows. The ticks stay as they are.
* **One row ticked:** Prev and Next step through every row that has
  data, and the tick moves with the ▶.
* **No row ticked:** on Antenna and Baseline, none ticked means all, so
  the ▶ steps through every row with data and the ticks stay empty. On
  SPW the tick and the ▶ start at the first row and move together.
* **Stepping ends** when the ticks are changed by hand (a click, the text
  box, ✱, a drill-down from the grid) or when **Plot** is pressed.
  **Plot** always plots what is ticked.
* **The plot titles** name what is being shown when the selection holds
  one field, SPW, scan, antenna or baseline of several, for example
  "… — 1642+689, scan 7": an exported PNG then says which it is.
* **The grid's Play** steps, in order of preference: the axis with a ▶
  (the most specific one, if more than one has a ▶; a current scan
  counts, between Antenna and SPW); else an axis with
  exactly one row ticked; else the most specific axis with more than one
  row to step.

(vp-scan)=

### Scan

Scan numbers, applied by **Plot**. The status-area help lists the
data's scans.

* `1,3,7`: those scans.
* `1~7`: scans 1 to 7. A range is written with `~`, not `-`.
* `>30` and `<5`: scans above or below a number.
* `!5`: all scans but 5. `!` can go before any of the forms above.
* Several parts are separated by commas.
* Empty: all scans.

**Stepping through scans.** ◀ and ▶ beside the Scan heading plot one
scan at a time: the scans the box selects (every scan when it is empty),
and only those of the field shown when one field is chosen. A line under
the box says which, for example "▶ Scan 7 · 3 of 5"; the box itself is
left as it is. Typing in the box, **Plot**, or ✱ (all of the box's scans
again) ends it. Scans with no data for the chosen field, spectral
windows, time range or UV range are skipped, and are not counted in
"3 of 5". The Antenna and Baseline ticks do not count here: with only
some antennas ticked, a scan that none of them took part in can still
plot empty. See also {ref}`vp-iteration`.

The text is checked when **Enter** is pressed and when the box loses the
focus. A problem shows as a ⚠ warning in the status area, which stays
while other help is showing; text that selects no scan of the data is a
problem too.

(vp-time)=

### Time range

UTC, applied by **Plot**. The status-area help gives the data's time
range.

* `08:00:00~08:30:00`: times of day, on the day of the data.
* `2024-05-01/08:00:00~2024-05-01/08:30:00`: with the date.
* `>08:00` and `<08:30`: after or before a time.
* `08:00:00+00:10:00`: a start and a length.
* `1/00:10:00`: day/time, as the time axis labels data that cross
  midnight.
* One time alone: the integration around it.
* Empty: all times.

(vp-uvrange)=

### UV range

Projected baseline length, applied by **Plot**.

* `0~50klambda`, `100~300m`, `>200m`, `<1.5km`; several with commas.
* Units: m, km, lambda, klambda, Mlambda (kl and Ml for short); metres
  when none is given.
* Wavelengths are taken at each spectral window's centre frequency.
* Whole rows (one baseline at one time) are in or out.
* Empty: all lengths.

(vp-autocorr)=

### Autocorrelations

Baselines from an antenna to itself.

* **Exclude** (the default): cross-correlations only. Autocorrelations
  can be hundreds of times larger (VLBI data before amplitude
  calibration) and flatten every plot of all baselines.
* **Include**: both. The Antenna × antenna matrix then has its
  diagonal.
* **Only**: autocorrelations alone: the bandpass shape and the
  interference at each station.
* A baseline ticked by name (for example `BR&BR`) is plotted whatever is
  chosen here.
* Applied by **Plot**.

## Raster panels

(vp-r-axes)=

### Raster axes

What runs up (Y) and across (X) the image. Everything else is averaged
into each cell.

* **Baseline × Time**: all baselines at once, to find when and where
  something went wrong.
* **Time × Channel**: a waterfall. One baseline if one is ticked,
  otherwise the selected baselines combined (see {ref}`vp-bcombine`).
* **Baseline × Channel**: the band on every baseline.
* **Antenna 1 × Antenna 2**: one cell per baseline over time and
  channels, mirrored, so a bad antenna is a whole row and column (the
  {ref}`vp-preset-antmatrix` preset). The two Antenna axes only go
  together.
* Y and X must differ.
* Which correlation is shown is set by {ref}`vp-rcorr`.

(vp-rcorr)=

### Correlation shown

A raster shows one correlation at a time.

* **First ticked**: the first one ticked in the Data section, named in
  the title.
* **Parallel hands**: the mean of the two parallel hands, (XX+YY)/2 or
  (RR+LL)/2: the Stokes I-like view AIPS editors start from. A sample
  counts only where both hands are unflagged, and a flag box here flags
  both.
* **A product**: that one, whether it is ticked or not.
* Two panels, on RR and on LL, show a problem that is in one hand only.

**How it differs from the Data section's Correlation ticks.** The ticks
decide what is *read*: a scatter draws one layer for each ticked
correlation. Correlation shown decides what each *raster* draws, and a
raster set to a product reads that product itself, so it can show a
correlation the ticks leave out. That lets the two panels show RR and LL
side by side while a scatter keeps a single layer, or a scatter show
both hands while the raster shows one. With *First ticked*, the raster
simply follows the ticks.

(vp-averaging)=

### Averaging

How a raster cell combines the samples it covers (Amplitude and Phase
only).

* **Vector**: average the complex visibilities, then take amplitude and
  phase. The amplitude drops where the samples do not line up (noise, an
  uncalibrated phase, a delay across the averaged channels). This is the
  default in AIPS and plotms, which both offer scalar averaging too.
* **Scalar**: average the amplitudes themselves. Shows the signal plus the
  noise level, with no loss from decorrelation.
* Vector next to Scalar, on two panels, shows where coherence is lost.

See also {ref}`cap-averaging`.

(vp-bcombine)=

### Baselines combined

What a cell shows when it covers several baselines: any raster without
Baseline on an axis, such as Time × Channel with no single baseline
ticked. {ref}`vp-averaging` applies to the samples of each baseline;
this is what happens to the baselines.

* **Mean**: the average of the baselines' amplitudes. The general picture
  of the selected baselines; a problem on a few of them is diluted.
* **Median**: the middle baseline at each cell: what most baselines
  share, unmoved by a few bad ones. A feature that shows in Maximum but
  not in Median is on a few baselines (find and flag those); one that
  shows in both is on most of them.
* **Maximum**: the largest baseline at each cell. Interference or a bad
  antenna shows even if only one baseline has it; then tick baselines to
  find which.
* **Vector (coherent)**: the baselines are added as complex numbers
  first. Only for a calibrated point source, where all baselines agree
  and the noise averages down; on anything else they cancel and the plot
  reads low. Offered only with Vector averaging.
* Applies to Amplitude, Phase (Mean, Median and Maximum all give the mean
  direction), Amp V Diff and Phase Diff.
* A flag box on such a plot flags every selected baseline.

(vp-border)=

### Baseline order

How the Baseline axis of a raster is laid out. The baselines that have
data are drawn side by side either way.

* **By number**: in the order of the Baseline table; baselines to one
  antenna sit together.
* **By length**: shortest first, and the ticks read the length. Trouble
  that grows along the axis is then about distance (the atmosphere, a
  source that is resolved); trouble in scattered columns is about
  particular antennas.
* The cursor readout names the baseline and its length in both.

(vp-time-gaps)=

### Time gaps

How a Time axis treats stretches with no data, such as the time spent on
other fields between the scans of the one shown: a raster's, or a
scatter's with X = Time.

* **Shown** (the default): time is drawn to scale. A field observed in
  short scans with long gaps between them shows as thin bands in
  mostly blank space. The Time axis then lines up with a scatter of
  X = Time beside it.
* **Closed**: each stretch with data (a scan, or scans back to back)
  is kept to scale, and a long gap between stretches shrinks to a small
  blank, left empty as with the gaps shown. A blank is 2% of the axis,
  or less when there are many long gaps: all of them together take at
  most a tenth of it. A short gap (a slew between scans, a pause
  between subscans) keeps its real width, so closing never shows more
  breaks than the gaps shown do. With very many long gaps a blank can
  be narrower than a pixel until you zoom in. The ticks read the real
  times, and the axis
  label says "(gaps closed)". The cursor readout and flag boxes work in
  real time: a box drawn across a closed gap covers the scans on both
  sides, and the gap itself, which holds no data of the selection.
* A raster and a scatter of X = Time with their gaps closed share one
  closed axis, so they line up. With the gaps closed on one and shown on
  the other they do not, but the linked cursor still follows: over one
  it marks the same time on the other, and over a time in a closed gap
  it shows nothing.
* Zooming in reads finer cells, as with the gaps shown.
* Each panel has its own setting, in its gear tab (for a scatter, shown
  when X is Time). Applied by **Plot**.

(vp-twin)=

### Time window

The stretch of time each Phase RMS or Coherence value is measured
within.

* **Auto**: one integration where Time is a plot axis, one scan where it
  is not.
* **Off**: no windows.
* **Scan**, or a length: each scan, or pieces of that length; never
  across a gap.
* For **Amp V Diff** and **Phase Diff** it is the stretch the reference
  mean is taken over (Auto and Off mean one scan).
* Longer windows are steadier but blur changes in time. On the plot they
  show as blocks.
* **Custom…** opens a box under the list for any length: a number of
  seconds, or with a unit (`45`, `45s`, `2min`, `1.5h`). **Enter** adds it
  to the list and selects it. A value that is not a positive length is
  reported as a warning and changes nothing.
* A scatter panel has its own Time window, for its Phase RMS and
  Coherence points: *Auto* is one integration when X is Time, otherwise
  one scan.

(vp-cwin)=

### Channel window

How many channels each Phase RMS or Coherence value is measured within.

* **Off**: on a raster, the whole band where frequency is not a plot axis
  and single channels where it is; on a scatter, single channels when X is
  Frequency or Channel, otherwise the whole band.
* **A number**: blocks of that many channels.
* **Custom…** opens a box for any whole number of channels, 2 or more
  (`12`, `12ch`); **Enter** adds it to the list and selects it.
* On a Time × Channel waterfall, Phase RMS and Coherence need a channel
  window, a time window, or both: without one, each cell holds one sample.

(vp-scaling)=

### Color scaling

How values are spread over the colors. It changes the picture, never the
data.

* **eq_hist** uses every color equally and shows faint structure, but
  distances between colors mean nothing.
* **linear** is honest about size, and lets a few strong values hide the
  rest.
* **log** and **sqrt** sit between.
* **Min** and **max** (or a drag on the histogram) clip the range;
  **threshold** highlights everything above a cutoff.
* Phase is fixed at −180..180°, linear, with a cyclic colormap.

See also {ref}`cap-colormaps`.

## Scatter panels

(vp-s-avg)=

### Averaging (scatter)

One point per baseline and correlation for each stretch of time and block
of channels, instead of one per sample: what a spectrum or a time series
needs.

* **Average over time**: *Off* (every integration), *Scan* (a spectrum
  per scan, with X Frequency or Channel), or a length (from the start of
  each scan, never across a scan boundary).
* **Average over channels**: *Off*, a number of channels, or *All* (one
  point per spectral window: a time series, with X Time).
* **Vector**: average the complex visibilities, then take amplitude and
  phase. The amplitude drops where the samples do not add up (noise, an
  uncalibrated phase, a delay across the averaged channels).
* **Scalar**: average the amplitudes.
* Amplitude, Phase, Real and Imaginary only. Flagged samples are left
  out, and a flag box takes every sample behind the points it encloses.

## Presets

(vp-preset-waterfall-all)=

### All-baseline waterfall

Time × Channel with every selected baseline in one image, like AIPS
FTFLG.

* Each cell shows the largest amplitude among the baselines
  ({ref}`vp-bcombine` is set to Maximum).
* The scatter panel shows Amplitude vs Channel.
* A quick survey for interference: one picture instead of one per
  baseline. A flag box here flags **every selected baseline**, and the
  message says how many.
* What looks bad here may be on only a few baselines: tick antennas or
  baselines to find which before flagging them all.

(vp-preset-antmatrix)=

### Antenna × antenna

One cell per baseline, averaged over time and channels, like the matrix
of AIPS LISTR.

* The matrix is mirrored, so an antenna that is bad on every baseline
  shows as a whole row and column.
* Hover a cell for its baseline. The diagonal holds the autocorrelations
  when {ref}`vp-autocorr` is Include.
* The scatter panel shows Amplitude vs UV Distance, where that antenna's
  baselines stand apart.
* {ref}`vp-averaging` applies: vector averaging over a long stretch reads
  low where the phase is not calibrated yet. Use Scalar, or narrow the
  time range.
* Flag on Baseline × Time once the antenna is found.

## Flagging

See also {ref}`cap-flagging`.

(vp-flag-reach)=

### Flag reaches

How far each flag (or unflag) goes beyond the box that is drawn. The
settings stay until they are changed; the line underneath turns amber
while any is wider than the box, and the box is outlined in amber as it
is drawn.

* **Baselines**: *As drawn*; or all baselines *to the antenna they share*
  (drag across two or more baselines of the bad antenna; refused if they
  do not share exactly one); or all baselines *to every antenna drawn*;
  or *all baselines*.
* **All channels**: the drawn time range over the whole band (a bad
  integration).
* **All selected spectral windows**: the same channels and times in every
  selected window.
* **Whole scan**: every integration of the scans the box touches.
* **All fields**: matters only on a plot without a Time axis.
* **All correlations**: not just the one plotted.
* Baselines, windows, scans and fields are extended for a raster box with
  the {ref}`vp-flagf-all` filter; other boxes say so.
* **For one box only**, without changing these settings: **Shift** while
  dragging gives the full height of the plot as shown, **Alt** (**Option**
  on a Mac) the full width. A key held as the drag starts, or pressed
  during it, counts for the whole drag, even if it is let go first.
  **Esc** before letting go of the button drops the box.

(vp-flag-filters)=

### Flag filters

What a flag box takes from the samples it encloses.

A box from which the filter takes no sample adds nothing to the pending
flags, and the status area says **Nothing to flag** (or unflag), with the
reason: usually "no samples matched filter ...". The filter's cutoff is
then beyond every value in the box: lower it (for Value range, check the
numbers against the color bar or axis, whose units it uses), or draw the
box with **All selected**.

(vp-flagf-all)=

#### All selected (immediate)

Every sample in the box: AIPS-style immediate flagging. The other filters
take only some of the samples in the box, so a generous box can be drawn
round a bad stretch.

(vp-flagf-value-range)=

#### Value range

Samples whose value, of the quantity the panel shows (Amplitude, Phase,
Real or Imaginary), lies in the range. For dropouts (High only) and
strong interference (Low only); CASA's clip.

* **Low**: flag samples at or above this value; empty for no lower bound.
  In the units of the color bar or axis.
* **High**: flag samples at or below this value; empty for no upper
  bound. Low and High together take the samples between them.

(vp-flagf-outlier)=

#### Outlier from neighbors

Amplitudes far from the running median of the 9 samples around them, per
baseline and correlation, within each scan. Finds spikes that a smooth
change in gain or a bandpass does not explain; CASA's tfcrop or AIPS's
FLAGR in spirit.

* **Compare along**: *time*, each sample against the integrations before
  and after it (spikes in time, brief interference); *channel*, against
  the neighboring channels (narrow-band interference, a bad channel).
* **Cutoff (sigma)**: how far from its neighbors a sample must be, in
  robust standard deviations of the baseline's scatter about them. Lower
  takes more.

(vp-flagf-zscore)=

#### Z-Score above cutoff

The robust per-baseline joint (real, imaginary) Z-Score, as the Z-Score
coloring computes it: what the Z-Score view shows bright.

* **Cutoff**: the per-sample Z-Score cutoff (3.5, after Iglewicz and
  Hoaglin).
* **Match per**: *sample*, each sample against the cutoff; *cell*, a
  whole raster cell (the maximum over its reduced dimensions, with a
  Šidák-corrected cutoff), as the raster coloring shows it; *auto*,
  *cell* on a raster showing Z-Score, else *sample*.

See also {ref}`cap-zscore`.

(vp-flagf-amplitude-mad)=

#### Amplitude outlier (MAD)

|amp − median| / (1.4826 MAD) above the cutoff, per baseline over the
whole selection: a baseline's samples that are off its usual level.

* **Sigma**: how far from the baseline's median amplitude, in robust
  standard deviations.

(vp-flagf-phase-deviation)=

#### Phase deviation

Phase further than the limit from the per-baseline median direction. For
a calibrator, where the phases should agree.

* **Max deviation (deg)**: phases further than this from the baseline's
  median direction are taken.

(vp-flagf-grow)=

#### Grow around flags

Unflagged samples next to flagged ones (on disk or pending). Interference
has weaker edges that a cutoff misses: flag the core with another filter,
then draw a box with this one; CASA's extend.

* **Grow along**: *time*, the integrations before and after a flagged
  sample; *channel*, the channels either side; *both*, either.
* **Samples**: how many samples either side of a flagged one are taken.

(vp-flag-export)=

### Export / commit

What to do with the pending flags. Nothing is written until **Go**.

* **Save as JSON**: the flags as a file that can be loaded again here,
  with their reasons.
* **Write flags** to the MS or Processing Set: exact, checked afterwards,
  and with a backup.
* **Write flags with CASA flagdata** (when CASA is installed): the CASA
  route. CASA may apply large or scattered selections incompletely, so
  prefer Write flags for many points. The result is checked either way.
* **After writing**, visplot reads the flags back and compares them with
  what it showed. A difference is reported as a warning with the number
  of samples not flagged, not unflagged, or changed that should not have
  been; the flags as they were before are kept in the backup file it
  names (see the next point).
* **Restore flags from a commit backup (.npz)** puts back the flags a
  write replaced; give the backup file in **File**.
* **Writing flags is unavailable** when visplot cannot write to the data:
  no write permission for it, or a reader that cannot write. The warning
  gives the reason. Save the flags as JSON and apply them where the data
  can be written.
* **Load flags from JSON**: the flag files saved in the working directory
  or next to the data are listed, this data's first.
* **File**: where to write, or what to load or restore. Empty takes a name
  made from the data name and the time.
