(cap-from-plotms)=

# Coming from plotms

This page goes through CASA `plotms`'s parameters in the order the CASA
documentation lists them, and gives the `visplot` equivalent of each:
the Python argument, the value, or the setting in the window.

:::{admonition} Review status
:class: note
This table was compiled on 2026-11-16 from the CASA `plotms`
documentation and the `visplot` source, and **no row has been
reviewed yet**. When a tester confirms a row, the *Reviewed* column
gets their initials and the date. Corrections are welcome, especially
for rows marked *Different*.
:::

**Status** means:

- **Same**: works the same way.
- **Different**: available, but works differently; the note says how.
- **Partial**: some of it is available.
- **Not yet**: not available.
- **N/A**: does not apply to how `visplot` draws.

`visplot`'s arguments are those of the `visplot(...)` task call; see
[](../visplot_starting.md).

## Data and selection

:::{container} crosswalk
| plotms | visplot | Status | Notes | See | Reviewed |
|---|---|---|---|---|---|
| `vis` | `vis` | Same | MSv2 or MSv4; the format is detected | {ref}`Data <cap-data>` | |
| `caltable` | | Not yet | Calibration tables cannot be plotted | {ref}`Data <cap-data-limits>` | |
| `xdatacolumn`, `ydatacolumn`: `data`, `corrected`, `model` | `datacolumn` | Different | One column for the whole plot, not one per axis | {ref}`Data <cap-data>` | |
| `xdatacolumn`, `ydatacolumn`: `float`, residuals (`corrected-model` ...), ratios | | Not yet | | {ref}`Data <cap-data-limits>` | |
| `field` | `field` | Same | | {ref}`Selection <cap-selection>` | |
| `spw` | `spw` | Partial | Spectral windows by index; no `spw:chan` channel ranges | {ref}`Selection <cap-selection-limits>` | |
| `timerange` | `timerange` | Same | One range | {ref}`Selection <cap-selection>` | |
| `uvrange` | `uvrange` | Same | | {ref}`Selection <cap-selection>` | |
| `antenna` | `antenna` | Partial | Names, IDs, `!` exclusion and `NAME&NAME` baselines | {ref}`Selection <cap-selection-limits>` | |
| `scan` | `scan` | Same | | {ref}`Selection <cap-selection>` | |
| `correlation` | `correlation` | Same | Stokes conversion is not offered | {ref}`Selection <cap-selection>` | |
| `array`, `observation`, `intent`, `feed`, `msselect` | | Not yet | | | |
| autocorrelations, selected through `antenna` | `autocorr` | Different | A separate setting: `"exclude"` (the default), `"include"` or `"only"` | {ref}`Selection <cap-selection>` | |
:::

## Averaging

:::{container} crosswalk
| plotms | visplot | Status | Notes | See | Reviewed |
|---|---|---|---|---|---|
| `avgtime` | `scatter_avg_time` | Different | Seconds or `"scan"`, per plot. Never across a scan boundary. A raster cell instead averages what it covers | {ref}`Averaging <cap-averaging>` | |
| `avgchannel` | `scatter_avg_chan` | Same | A number of channels, or `"all"` | {ref}`Averaging <cap-averaging>` | |
| `avgscan`, `avgfield` | | Not yet | Time averaging never crosses scans or fields | {ref}`Averaging <cap-averaging-limits>` | |
| `avgbaseline` | `baseline_combine` | Different | Rasters only: the baselines in a cell are combined as mean, median, maximum or coherent | {ref}`Averaging <cap-averaging>` | |
| `avgantenna`, `avgspw` | | Not yet | | {ref}`Averaging <cap-averaging-limits>` | |
| `scalar` | `averaging="scalar"` | Same | `"vector"` is the default, as in plotms | {ref}`Averaging <cap-averaging>` | |
:::

## Transformations

:::{container} crosswalk
| plotms | visplot | Status | Notes | See | Reviewed |
|---|---|---|---|---|---|
| `freqframe`, `restfreq`, `veldef` | | Not yet | There is no velocity axis | | |
| `phasecenter` | | Not yet | | | |
:::

## Flag extension

:::{container} crosswalk
| plotms | visplot | Status | Notes | See | Reviewed |
|---|---|---|---|---|---|
| `extendflag`, `extcorr` | `flag_reach="correlations"` | Same | | {ref}`Flagging <cap-flagging>` | |
| `extchannel` | `flag_reach="channels"` | Same | | {ref}`Flagging <cap-flagging>` | |
| | `flag_reach`: baselines, spectral windows, scan, fields | (more) | Reaches plotms does not have | {ref}`Flagging <cap-flagging>` | |
:::

## Iteration and grids

:::{container} crosswalk
| plotms | visplot | Status | Notes | See | Reviewed |
|---|---|---|---|---|---|
| `iteraxis`: `scan`, `field`, `spw`, `baseline`, `antenna`, `corr` | `layout="grid"`, `iterate_by` | Same | Two axes can be combined (`"antenna,spw"`) | {ref}`Plots <cap-plots>` | |
| `iteraxis="time"` | | Not yet | | {ref}`Plots <cap-plots-limits>` | |
| `gridrows`, `gridcols` | `gridrows`, `gridcols` | Different | For iteration only (1–8). Two different plots are two panels (`layout="side"` or `"over"`, `panels=`) | {ref}`Plots <cap-plots>` | |
| `rowindex`, `colindex`, `plotindex` | `panels=[{...}, {...}]` | Different | At most two independently configured panels | {ref}`Plots <cap-plots-limits>` | |
| `xselfscale`, `yselfscale`, `xsharedaxis`, `ysharedaxis` | | Different | A grid has a common or per-plot axis scale, set in the window; `gridcolor` does the same for the color range | {ref}`Plots <cap-plots>` | |
:::

## Axes

:::{container} crosswalk
| plotms | visplot | Status | Notes | See | Reviewed |
|---|---|---|---|---|---|
| `time` | `TIME` | Same | UTC by default, or elapsed (`time_labels`) | {ref}`Plots <cap-plots>` | |
| `chan`, `freq` | `CHANNEL`, `FREQUENCY` | Same | Frequency is a scatter X axis; a raster's Channel axis reads frequency across several spectral windows | {ref}`Plots <cap-plots>` | |
| `vel` | | Not yet | | | |
| `amp`, `phase`, `real`, `imag` | `AMPLITUDE`, `PHASE`, `REAL`, `IMAGINARY` | Same | | {ref}`Quantities <cap-quantities>` | |
| `uvdist`, `uvwave` | `UVDIST`, `UVDIST_LAMBDA` | Same | Scatter X axis | {ref}`Plots <cap-plots>` | |
| `u`, `v` | `U`, `V` | Same | U against V gives UV coverage | {ref}`Plots <cap-plots>` | |
| `w`, `uwave`, `vwave`, `wwave` | | Not yet | | | |
| `baseline` | `BASELINE` | Different | A raster axis, by number or by length (`baseline_order`) | {ref}`Plots <cap-plots>` | |
| `ant1`, `ant2` | `ANTENNA1` × `ANTENNA2` | Different | Together, as the antenna × antenna matrix raster | {ref}`Standard views <cap-views>` | |
| `corr` | | Not yet | Not an axis; each raster shows one correlation or the parallel hands averaged (`raster_corr`), and a scatter shows correlations as layers | {ref}`Plots <cap-plots-limits>` | |
| `scan`, `field`, `spw` as axes | | Not yet | Available for coloring and iteration instead | | |
| `flag` | `FLAG` (raster) | Different | The flagged fraction of each cell; flagged data can also be shown (`flag_show_flagged`) | {ref}`Quantities <cap-quantities>` | |
| `wt`, `wtamp`, `wtsp`, `sigma`, `sigmasp` | | Not yet | | {ref}`Quantities <cap-quantities-limits>` | |
| `interval`, `row`, `observation`, `intent`, `feed1`, `feed2`, `flagrow` | | Not yet | | | |
| `azimuth`, `elevation`, `hourang`, `parang` | | Not yet | | {ref}`Plots <cap-plots-limits>` | |
| antenna-based and calibration axes (`ant-azimuth`, `gainamp`, `delay`, `tsys`, ...) | | Not yet | | | |
| | `PHASE_RMS`, `COHERENCE`, `AMP_VDIFF`, `PHASE_DIFF`, `Z_SCORE` | (more) | Quantities plotms does not have | {ref}`Quantities <cap-quantities>` | |
:::

## Display

:::{container} crosswalk
| plotms | visplot | Status | Notes | See | Reviewed |
|---|---|---|---|---|---|
| `coloraxis`: `scan`, `field`, `spw`, `antenna1`, `antenna2`, `baseline` | scatter layer coloring | Same | Also coloring by Z-Score | {ref}`Plots <cap-plots>` | |
| `coloraxis`: `corr` | scatter layers | Different | Each correlation is a layer in its own color | {ref}`Plots <cap-plots>` | |
| `coloraxis`: `channel`, `time`, `observation`, `intent` | | Not yet | | | |
| `clearplots=False` (overplot) | scatter layers | Different | Layers in one scatter plot | {ref}`Plots <cap-plots>` | |
| `customsymbol`, `symbolshape`, `symbolsize`, `symbolcolor`, ... | | N/A | Points are drawn as density; the colormap is selectable (`scatter_cmap`) | {ref}`Colormaps <cap-colormaps>` | |
| `customflaggedsymbol`, `flaggedsymbolcolor`, ... | `flag_show_flagged`, `flag_flagged_color` | Same | | {ref}`Flagging <cap-flagging>` | |
| `plotrange` | | Different | Zoom in the window; `time_range` / `uvrange` narrow the data instead | {ref}`Selection <cap-selection>` | |
| `title`, `xlabel`, `ylabel`, fonts, grid lines, `legendposition` | | Not yet | Titles and legends are generated | | |
| `xconnector`, `timeconnector` | | Not yet | | | |
| `showatm`, `showtsky`, `showimage` | | Not yet | | | |
:::

## Export and other

:::{container} crosswalk
| plotms | visplot | Status | Notes | See | Reviewed |
|---|---|---|---|---|---|
| `plotfile` (PNG) | `plotfile` | Same | Writes the PNG instead of opening the window | {ref}`Output <cap-output>` | |
| `expformat`: `jpg`, `pdf`, `ps`, `txt` | | Not yet | PNG only | {ref}`Output <cap-output>` | |
| `exprange` | `exprange` | Same | Also page numbers, such as `"1-3,5"` | {ref}`Output <cap-output>` | |
| `width`, `height`, `dpi`, `highres` | `row_height`, `column_width` | Different | Sizes per row and column of plots, in pixels | {ref}`Output <cap-output>` | |
| `showgui=False` | `plotfile` | Different | Giving `plotfile` implies no window | {ref}`Output <cap-output>` | |
| `headeritems` | plot description page | Different | Every setting of the plot, and the call that reproduces it | {ref}`Output <cap-output>` | |
| `callib` (on-the-fly calibration) | | Not yet | | | |
| Locate (GUI) | cursor readout | Partial | The readout names the baseline, time and value under the pointer; there is no list of selected points | | |
| Flag / Unflag (GUI) | flag and unflag boxes | Same | With filters, preview, undo and a backup before writing | {ref}`Flagging <cap-flagging>` | |
| Reload (GUI) | Reload | Same | Re-reads the data from disk | | |
:::

## Sources

- CASA documentation, [`plotms` task](https://casadocs.readthedocs.io/en/latest/api/tt/casaplotms.plotms.html).
