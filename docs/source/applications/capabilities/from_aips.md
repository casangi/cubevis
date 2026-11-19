(cap-from-aips)=

# Coming from AIPS

This page goes through the AIPS tasks that `visplot` takes the place of
for inspecting and editing visibility data: TVFLG, SPFLG, FTFLG and
EDITR for editing, and VPLOT, UVPLT, POSSM and LISTR's matrix listing
for display. For each display mode, option and menu item it gives the
`visplot` equivalent: the Python argument, the value, or the setting in
the window.

`visplot` does not copy the AIPS menus or keyboard use. It is designed
for a web window, so one AIPS menu item may correspond to a setting
plus a mouse action.

:::{admonition} Review status
:class: note
This table was compiled on 2026-11-16 from AIPS Memo 127 (*Interactive
flagging in AIPS*, 2025), the AIPS CookBook and the `visplot` source,
and **no row has been reviewed yet**. When a tester confirms a row, the
*Reviewed* column gets their initials and the date. Corrections are
welcome, especially for rows marked *Different*.
:::

**Status** means:

- **Same**: works the same way.
- **Different**: available, but works differently; the note says how.
- **Partial**: some of it is available.
- **Not yet**: not available.
- **N/A**: does not apply to how `visplot` works.

## TVFLG: what is shown

:::{container} crosswalk
| AIPS | visplot | Status | Notes | See | Reviewed |
|---|---|---|---|---|---|
| Baseline × time display | `preset="vplot"`, or `raster_y="TIME"`, `raster_x="BASELINE"` | Same | Either axis can be on either side | {ref}`Plots <cap-plots>` | |
| SORT BY LENGTH / SORT BY BASELINE | `baseline_order="length"` / `"number"` | Same | | {ref}`Plots <cap-plots>` | |
| DPARM(3): show both n−m and m−n | | N/A | Each baseline is shown once | | |
| ENTER CHANNEL, NCHAV (one channel, or channels averaged) | `spw` selection | Different | A Baseline × Time cell averages every selected channel; single channels cannot be selected yet | {ref}`Averaging <cap-averaging>` | |
| ENTER IF, LOAD NEXT IF/ST | `spw`, stepping through spectral windows | Same | | {ref}`Selection <cap-selection>` | |
| DISPLAY STOKES | `raster_corr` | Partial | One correlation, or the parallel hands averaged; no conversion to Stokes | {ref}`Plots <cap-plots>` | |
| AMPLITUDE, PHASE | `raster_qty="AMPLITUDE"`, `"PHASE"` | Same | Vector averaged by default; scalar available | {ref}`Quantities <cap-quantities>` | |
| RMS, RMS/MEAN (scalar amplitude) | | Not yet | Requested | {ref}`Phase stability <cap-phasestab-limits>` | |
| VECT RMS, VRMS/VAVG | `PHASE_RMS`, `COHERENCE` | Different | `visplot` measures the steadiness of *phase*: the RMS of phase, and vector over scalar amplitude | {ref}`Phase stability <cap-phasestab-calc>` | |
| AMP V DIFF | `raster_qty="AMP_VDIFF"` | Different | Compared with the mean of the *other* samples in the window (the sample itself is left out); the windows are fixed blocks | {ref}`Difference <cap-vdiff-calc>` | |
| PHASE DIFF | `raster_qty="PHASE_DIFF"` | Different | As for AMP V DIFF | {ref}`Difference <cap-vdiff-calc>` | |
| AMPL DIFF (scalar amplitude difference) | | Not yet | | {ref}`Difference <cap-vdiff-limits>` | |
| DPARM(4): divide by the source's flux density | | Not yet | | | |
:::

## TVFLG: averaging

:::{container} crosswalk
| AIPS | visplot | Status | Notes | See | Reviewed |
|---|---|---|---|---|---|
| DPARM(6): master grid interval | | N/A | Rasters are drawn at the data's own integrations | {ref}`Averaging <cap-averaging>` | |
| ENTER SMOOTH TIME (in multiples of DPARM(6)) | | Not yet | Requested: averaging by a number of integrations. Raster cells cannot yet be averaged in time | {ref}`Averaging <cap-averaging-limits>` | |
| Averages weighted by the data weights | | Different | `visplot` does not use the weights: every unflagged sample counts equally | {ref}`Averaging <cap-averaging-calc>` | |
| ENTER SCAN TIME (window for the DIFF displays) | `stat_time_window` | Different | A scan (the default) or a length in seconds, not in integrations | {ref}`Difference <cap-vdiff-calc>` | |
:::

## TVFLG: flagging

The *reaches* widen a box the way TVFLG's flagging switches do. The
baseline, spectral-window, scan and field reaches apply to a raster box
with the *All selected* filter; with other filters only the channel and
correlation reaches apply (see {ref}`cap-flagging-calc`).

:::{container} crosswalk
| AIPS | visplot | Status | Notes | See | Reviewed |
|---|---|---|---|---|---|
| FLAG PIXEL, FLAG AREA, FLAG BASELINE-DT | flag box | Same | A box of any size | {ref}`Flagging <cap-flagging>` | |
| FLAG TIME, FLAG TIME RANGE (all baselines) | flag box with the *all baselines* reach, or a full-width box | Same | | {ref}`Flagging <cap-flagging>` | |
| FLAG ANTENNA-DT (all baselines to an antenna) | flag box with the *shared antenna* reach (`flag_reach="shared-antenna"`) | Same | Drag across two or more baselines of the antenna | {ref}`Flagging <cap-flagging>` | |
| FLAG BASELINE (all times) | full-height flag box | Same | | {ref}`Flagging <cap-flagging>` | |
| FLAG/CONFIRM | `flag_preview=True` | Different | Each box is shown as a proposal, with counts and highlighted samples, to accept or reject | {ref}`Flagging <cap-flagging>` | |
| CLIP BY SET #S, CLIP BY FORM | *Value range* flag filter | Partial | Only the samples within the box are clipped | {ref}`Flagging <cap-flagging>` | |
| CLIP INTERACTIV | | Not yet | | | |
| SWITCH ALL-CH FLAG | `flag_reach="channels"` | Same | | {ref}`Flagging <cap-flagging>` | |
| SWITCH ALL-IF FLAG | `flag_reach="spw"` | Partial | All *selected* spectral windows; no IF range | {ref}`Flagging <cap-flagging>` | |
| SWITCH SOURCE FLAG | `flag_reach="fields"` | Same | | {ref}`Flagging <cap-flagging>` | |
| ENTER STOKES FLAG | `flag_reach="correlations"` | Partial | All correlations, or the one shown | {ref}`Flagging <cap-flagging>` | |
| | `flag_reach="scan"` | (more) | Every integration of the scans a box touches | {ref}`Flagging <cap-flagging>` | |
| LIST FLAGS | flag report | Same | | {ref}`Flagging <cap-flagging>` | |
| UNDO FLAGS, REDO FLAGS | undo, redo | Same | | {ref}`Flagging <cap-flagging>` | |
| SET REASON | `flag_reason` | Same | | {ref}`Flagging <cap-flagging>` | |
| FC table (work survives an interruption) | JSON export of pending flags | Different | Pending flags are kept in memory until written; export them to keep a record | {ref}`Flagging <cap-flagging>` | |
| EXIT: copy the FG table, then add flags | write to the MS | Same | A backup of the flags it changes is saved first | {ref}`Flagging <cap-flagging>` | |
| DPARM(5): extend flags in time | | N/A | Flags apply to exactly the samples selected | | |
:::

## TVFLG: display options

:::{container} crosswalk
| AIPS | visplot | Status | Notes | See | Reviewed |
|---|---|---|---|---|---|
| OFFCOLOR, TVPSEUDO, TVPHLAME, OFMCOLOR | `raster_cmap` | Partial | Seven colormaps. The circular (cyclic) scheme is used automatically for Phase and cannot be chosen by name | {ref}`Colormaps <cap-colormaps>` | |
| Cookbook advice: circular pseudo-coloring for phase | | Same | Automatic for Phase | {ref}`Colormaps <cap-colormaps>` | |
| TVTRANSF; LOAD LOG, SQRT, LOG2, LIN | color scaling | Same | Also histogram-equalized (the default), square, gamma or power, threshold | {ref}`Colormaps <cap-colormaps>` | |
| ENTER AMP / PHS / RMS PIXRANGE | color range | Same | | {ref}`Colormaps <cap-colormaps>` | |
| TVZOOM, AUTO Y ZOOM, ENTER BLC / TRC | zoom and pan | Same | Zoom re-samples the data already read | {ref}`Plots <cap-plots>` | |
| LOAD NEXT / LAST PIECE | `timerange`, `scan` | Different | Narrow the selection; there is no piece-by-piece loading. A raster over 2 million cells skips rows and columns until the selection is narrowed | {ref}`Plots <cap-plots-calc>` | |
:::

## SPFLG

Everything above for TVFLG applies, with these differences:

:::{container} crosswalk
| AIPS | visplot | Status | Notes | See | Reviewed |
|---|---|---|---|---|---|
| Channel × time display, one baseline | `preset="waterfall"` (amplitude), `"phase-waterfall"` | Same | | {ref}`Standard views <cap-views>` | |
| ENTER BASELINE, LOAD NEXT / LAST BASELINE, LIST BASLS | stepping through baselines; baseline list | Same | Stepping skips baselines with no data | {ref}`Selection <cap-selection>` | |
| ENTER CH SMOOTH (Gaussian, in channels) | | Not yet | Channel averaging is for scatter plots only | {ref}`Averaging <cap-averaging-limits>` | |
| DPARM(2): cross, auto or both | `autocorr` | Same | `"exclude"`, `"include"` or `"only"` | {ref}`Selection <cap-selection>` | |
| SWITCH BASLIN FLAG (n−m, n−\*, m−\*, \*−\*) | baselines reach | Same | As drawn, all baselines to the shared antenna, to every antenna drawn, or all | {ref}`Flagging <cap-flagging>` | |
| FLAG A CHANNEL, FLAG CHANNEL RANGE, FLAG CHANNEL-DT | flag box (full height for all times) | Same | | {ref}`Flagging <cap-flagging>` | |
| TVPSEUDO: rainbow, circular, step | | Partial | See the TVFLG display options | {ref}`Colormaps <cap-colormaps>` | |
| DOCAT=2 / IN2SEQ: build the master grid now, use it later | | N/A | There is no master grid | | |
:::

## FTFLG

:::{container} crosswalk
| AIPS | visplot | Status | Notes | See | Reviewed |
|---|---|---|---|---|---|
| Channel × time, all baselines vector averaged | `preset="waterfall-all"` with `baseline_combine="coherent"` | Different | The preset combines baselines by *maximum*, so interference on a few baselines is not diluted. Mean, median and coherent (the FTFLG vector average) are also available | {ref}`Averaging <cap-averaging>` | |
| Each flag applies to all baselines | flag box | Same | A box on such a plot flags every selected baseline | {ref}`Flagging <cap-flagging>` | |
:::

## EDITR

:::{container} crosswalk
| AIPS | visplot | Status | Notes | See | Reviewed |
|---|---|---|---|---|---|
| Baselines to one antenna against time | stepping through antennas, with a Baseline × Time raster or a time series | Different | | {ref}`Selection <cap-selection>` | |
| Second data set, or second observable (DOTWO) | two panels | Partial | Two panels show two observables of the *same* data set | {ref}`Plots <cap-plots>` | |
| SHOW COHERENCE | `COHERENCE` | Different | `visplot` gives vector over scalar amplitude, from 0 to 1 (1 is fully coherent) | {ref}`Phase stability <cap-phasestab-calc>` | |
:::

## Display tasks

:::{container} crosswalk
| AIPS | visplot | Status | Notes | See | Reviewed |
|---|---|---|---|---|---|
| VPLOT: a quantity against time, one baseline per plot | `preset="timeseries"`; `layout="grid"`, `iterate_by="baseline"` | Same | | {ref}`Standard views <cap-views>` | |
| UVPLT / VPLOT axes: amplitude, phase, real, imaginary, time, u, v, uv distance, frequency, channel | scatter axes | Same | | {ref}`Plots <cap-plots>` | |
| UVPLT / VPLOT axes: hour angle, elevation, azimuth, parallactic angle, position angle, w, log amplitude, weight | | Not yet | | {ref}`Plots <cap-plots-limits>` | |
| UVPLT: uv coverage | `scatter_x="U"`, `scatter_y="V"` | Same | | {ref}`Plots <cap-plots>` | |
| SOLINT (time averaging, in minutes) | `scatter_avg_time` | Different | In seconds, or `"scan"` | {ref}`Averaging <cap-averaging>` | |
| XINC (plot every n-th sample) | | N/A | Every sample is drawn, as density | {ref}`Plots <cap-plots>` | |
| DO3COL (separate IFs, channels, sources or times by color) | scatter layer coloring | Partial | By scan, field, antenna, baseline, spectral window or correlation; not by channel or time | {ref}`Plots <cap-plots>` | |
| POSSM: spectra averaged in time | `preset="spectrum"` | Partial | Each scan averaged, per baseline. Spectra averaged over baselines are not yet available on scatter plots | {ref}`Standard views <cap-views>` | |
| POSSM: plot bandpass and other calibration tables | | Not yet | | {ref}`Data <cap-data-limits>` | |
| LISTR OPTYPE='MATX': baseline matrix listing | `preset="antmatrix"` | Different | A raster image rather than a listing, averaged over the whole selection. Narrow by `scan` for one scan | {ref}`Standard views <cap-views>` | |
:::

## Automatic flagging tasks

:::{container} crosswalk
| AIPS | visplot | Status | Notes | See | Reviewed |
|---|---|---|---|---|---|
| RFLAG (statistical flagging) | Z-Score; the *Z-Score*, *Outlier from neighbours* and *Amplitude outlier* flag filters | Different | Interactive: the filters act within a box that is drawn | {ref}`Flagging <cap-flagging-calc>` | |
| CLIP (by limits) | *Value range* flag filter | Partial | Within a box | {ref}`Flagging <cap-flagging>` | |
| UVFLG (flags from a list) | | Not yet | Flags can be exported as JSON | {ref}`Flagging <cap-flagging>` | |
:::

## Sources

- E. W. Greisen, [*Interactive flagging in AIPS*](https://aips.nrao.edu/TEXT/PUBL/AIPSMEM127.PDF), AIPS Memo 127 (2025).
- The AIPS CookBook: [More editing of uv data](https://aips.nrao.edu/CookHTML/CookBookse30.html), [Plotting](https://aips.nrao.edu/CookHTML/CookBookse35.html), [Tools for data examination](https://www.aips.nrao.edu/CookHTML/CookBookse58.html).
