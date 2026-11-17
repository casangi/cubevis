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

## From Python

```python
visplot(vis="obs.ms", raster_qty="AMP_VDIFF", scatter_y="PHASE")
```

Raster values for `raster_qty`: `AMPLITUDE`, `PHASE`, `REAL`,
`IMAGINARY`, `PHASE_RMS`, `COHERENCE`, `AMP_VDIFF`, `PHASE_DIFF`, `FLAG`,
`Z_SCORE`. Scatter values for `scatter_y`: `AMPLITUDE`, `PHASE`, `REAL`,
`IMAGINARY`, `U`, `V`, `PHASE_RMS`, `COHERENCE`, `Z_SCORE`.

## Limits

- Weights, and calibration quantities (gains, delays, Tsys), cannot be
  plotted yet.
- There is no amplitude RMS quantity (see [](phase_stability.md)).
