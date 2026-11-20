# What visplot can do

This section lists what `visplot` can do, one capability per page. Each
page says what the capability is, what it is for in a commissioning or
flagging workflow (with the AIPS or CASA tool it corresponds to), how it
behaves, how to set it from Python, and its current limits.

:::{note}
The arrangement of `visplot`'s window is still being settled with the
testers, so these pages describe *what* each capability does rather than
*where* its control is. In the window, holding the pointer over any
control shows what it does in the status area at the bottom. The Python
argument names given on each page are stable.
:::

*Updated 2026-11-16.*

## Coming from plotms or AIPS

Two pages go through the parameters of CASA `plotms` and the modes and
menu items of the AIPS editing and plotting tasks, one by one, and give
the `visplot` equivalent of each, with a link to the page that
describes it:

- {ref}`cap-from-plotms`
- {ref}`cap-from-aips`

Both are under review by the testers; corrections are welcome.

The pages on quantities, averaging and flagging each have a *How it is
calculated* section with the formulas, for comparing `visplot`'s numbers
with those of other tools. *How the picture is drawn* explains what
happens on screen when there are more cells than pixels, or more than a
raster's limit, and defines each axis; *How colors are assigned* gives
the color scalings.

## Capabilities

| Capability | Status | In short |
|---|---|---|
| [Data formats and access](data_access.md) | Available | MSv2 and MSv4 / Processing Sets, local or on another computer |
| [Selecting data](selection.md) | Available | Field, SPW, correlation, antenna, baseline, scan, time and UV range, in CASA syntax |
| [Rasters, scatter plots and layouts](plot_types.md) | Available | Two linked panels, or a grid of plots iterated over baseline, antenna, SPW, ... |
| [Standard views](standard_views.md) | Available | One-step equivalents of AIPS VPLOT, POSSM, FTFLG, LISTR's matrix and others |
| [Plotted quantities](quantities.md) | Available | Amplitude, phase, real, imaginary, flagged fraction, and the derived quantities below |
| [Phase stability: Phase RMS and Coherence](phase_stability.md) | Available | How steady the phase is, per baseline, in time or across the band |
| [Difference from the mean: Amp V Diff and Phase Diff](vector_difference.md) | Available (rasters) | AIPS TVFLG's vector-difference modes: short-lived changes stand out |
| [Z-Score](z_score.md) | Available | How unusual each sample is for its own baseline |
| [Averaging](averaging.md) | Available; by number of integrations requested | Vector or scalar; over time and channels; how baselines are combined |
| [Colormaps and color scaling](colormaps.md) | Available | Selectable colormaps, a cyclic colormap for phase, several scalings |
| [Flagging](flagging.md) | Available | Boxes, filters, reaches, preview, undo, export and writing to the MS |
| [Saving and describing plots](output.md) | Available | PNG export, and a page describing each plot with the call that reproduces it |

## Requested, not yet available

- **Averaging by number of integrations** instead of by seconds (AIPS
  counts in integrations). See [](averaging.md).
- **Amp V Diff and Phase Diff on scatter plots.** They are raster
  quantities only for now. See [](vector_difference.md).
- **An amplitude RMS quantity.** The RMS quantities measure phase only.
  See [](phase_stability.md).

## Not yet available

- Jupyter notebooks (`visplot.notebook()` raises `NotImplementedError`
  saying so).
- Correlation as a raster axis. Each raster shows one correlation or the
  parallel hands averaged.
- Elevation, hour angle, azimuth and parallactic angle as axes.
- On-the-fly calibration, and plotting calibration tables.
- Flagging on the antenna × antenna matrix.
- Weighted averages. The data weights are not used: every unflagged
  sample counts equally.

If a capability you need is missing, or works differently from the AIPS
or CASA tool you are used to, please say so; see *Reporting a problem*
in [](../visplot.rst).

```{toctree}
:hidden:

from_plotms
from_aips
data_access
selection
plot_types
standard_views
quantities
phase_stability
vector_difference
z_score
averaging
colormaps
flagging
output
```
