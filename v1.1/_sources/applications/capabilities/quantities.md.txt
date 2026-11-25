(cap-quantities)=

# Plotted quantities

**Status:** available.

## What it is

The value plotted, shown as color on a raster or as the Y value on a
scatter, can be:

| Quantity | Raster | Scatter | Meaning |
|---|---|---|---|
| Amplitude, Phase, Real, Imaginary | yes | yes | the complex visibility, averaged as [](averaging.md) describes |
| Flag | yes | | the fraction of each cell that is flagged |
| [Phase RMS, Coherence](phase_stability.md) | yes | yes | how steady the phase is |
| [Amp V Diff, Phase Diff](vector_difference.md) | yes | | how far each sample is from the mean of its neighbours in time |
| [Z-Score](z_score.md) | yes | yes | how unusual each sample is for its baseline |
| U, V | | yes | baseline coordinates, for UV coverage |

Phase is in degrees, from −180 to 180.

## What it is for

Amplitude and phase show what the data *are*. The derived quantities
show particular kinds of trouble:

- **Phase RMS and Coherence**: phase instability (atmosphere, a bad
  antenna).
- **Amp V Diff and Phase Diff**: short-lived changes (interference, a
  jump, one bad integration).
- **Z-Score**: outliers, judged against each baseline's own behaviour.
- **Flag**: where flags already are.

The AIPS cookbook gives similar advice for TVFLG: amplitude finds
problems lasting a scan or more, and the vector difference finds short
excursions.

(cap-quantities-calc)=

## How it is calculated

**Which samples count.** Only unflagged samples enter any value. Every
unflagged sample counts equally: **the data weights (`WEIGHT`,
`WEIGHT_SPECTRUM`) are not used** in any average or statistic. AIPS and
CASA normally weight their averages, so on data whose weights vary,
`visplot`'s averages can differ from theirs.

**Parallel hands.** When a raster shows the parallel hands averaged, the
visibility used is $V = (V_{RR} + V_{LL})/2$ (or XX and YY). A sample is
treated as flagged if either hand is flagged.

**A raster cell.** A raster cell covers a set $S$ of unflagged samples:
those that fall in it along the dimensions not shown, such as all
selected channels on a Baseline × Time raster. With $N$ samples in $S$:

| Quantity | Vector averaging (default) | Scalar averaging |
|---|---|---|
| Amplitude | $\left\lvert \frac{1}{N}\sum_S V \right\rvert$ | $\frac{1}{N}\sum_S \lvert V\rvert$ |
| Phase | $\arg \sum_S V$ | $\arg \sum_S V/\lvert V\rvert$ (each sample counts equally, whatever its amplitude) |
| Real, Imaginary | $\frac{1}{N}\sum_S \mathrm{Re}\,V$, $\frac{1}{N}\sum_S \mathrm{Im}\,V$ | the same |

- **Phase** is in degrees, in (−180°, 180°].
- **Flag** is the fraction of the cell's samples that are flagged,
  counted over all the selected correlations. Padding (time slots a
  baseline does not have) is not counted.
- **A cell with no unflagged samples** is left blank.
- **A cell covering several baselines** (any raster without a Baseline
  axis) is described under {ref}`cap-averaging-calc`.
- **The derived quantities** are described on their own pages:
  {ref}`cap-phasestab-calc`, {ref}`cap-vdiff-calc` and
  {ref}`cap-zscore-calc`.

**A scatter point** is one sample, unless scatter averaging is on (see
{ref}`cap-averaging-calc`).

## From Python

```python
visplot(vis="obs.ms", raster_qty="AMP_VDIFF", scatter_y="PHASE")
```

Raster values for `raster_qty`: `AMPLITUDE`, `PHASE`, `REAL`,
`IMAGINARY`, `PHASE_RMS`, `COHERENCE`, `AMP_VDIFF`, `PHASE_DIFF`, `FLAG`,
`Z_SCORE`. Scatter values for `scatter_y`: `AMPLITUDE`, `PHASE`, `REAL`,
`IMAGINARY`, `U`, `V`, `PHASE_RMS`, `COHERENCE`, `Z_SCORE`.

(cap-quantities-limits)=

## Limits

- Weights, and calibration quantities (gains, delays, Tsys), cannot be
  plotted yet.
- There is no amplitude RMS quantity (see [](phase_stability.md)).

---

Coming from plotms or AIPS? See {ref}`cap-from-plotms` and {ref}`cap-from-aips`.
