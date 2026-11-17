(cap-views)=

# Standard views

**Status:** available.

## What it is

Each standard view sets up the plots for one common inspection task in
a single step, using the axes, quantity and averaging that the task
needs. Every setting can be changed afterwards. Each view has a short
description that the window shows on request.

## What it is for

These views are the usual first pass over a new observation. Each
corresponds to a display that AIPS or CASA users already know:

| View | Python `preset=` | Shows | Like |
|---|---|---|---|
| Amplitude survey | `"vplot"` | Baseline × Time amplitude raster, with amplitude against time | AIPS VPLOT, TVFLG |
| Radial plot | `"radplot"` | the same raster, with amplitude against UV distance | AIPS RADPLOT, `plotms` amp vs uvdist |
| Waterfall | `"waterfall"` | Time × Channel amplitude, one baseline | AIPS SPFLG |
| Phase waterfall | `"phase-waterfall"` | Time × Channel phase, one baseline | AIPS SPFLG in phase |
| All-baseline waterfall | `"waterfall-all"` | every selected baseline in one Time × Channel image, each cell its largest amplitude | AIPS FTFLG |
| Z-Score survey | `"zscore"` | Baseline × Time colored by [Z-Score](z_score.md) | |
| Phase stability | `"phaserms-time"`, `"phaserms-freq"`, `"phaserms-uvdist"` | [Phase RMS](phase_stability.md) against time, channel or UV distance | |
| Antenna matrix | `"antmatrix"` | Antenna × Antenna, one cell per baseline, averaged over the selection | AIPS LISTR matrix |
| Spectrum | `"spectrum"` | amplitude above phase against frequency, each scan averaged | AIPS POSSM, `plotms` bandpass views |
| Time series | `"timeseries"` | amplitude above phase against time, all channels averaged | AIPS VPLOT |

## How it behaves

- **A suggested order for commissioning:**
  1. *Survey* with the antenna matrix, Z-Score, phase stability and
     amplitude views. In the antenna matrix, an antenna that is bad on
     every baseline is a whole row and column.
  2. *Confirm* on single baselines with the waterfalls, the spectrum and
     the time series, stepping through baselines.
  3. *Flag* on whichever view shows the problem best.

  The steps are described in *A commissioning workflow* in
  [](../visplot.rst).
- A view normally switches to the layout that suits it. The layout can
  be pinned, so that views change only the plots and not their
  arrangement.

## From Python

```python
visplot(vis="obs.ms", preset="antmatrix")
visplot(vis="obs.ms", preset="spectrum", antenna="EA01&EA02")
visplot(vis="obs.ms", preset="vplot", pin_layout=True, layout="one")
```

(cap-views-limits)=

## Limits

- Flag boxes are refused on the antenna matrix. Find the antenna there,
  then flag it on a Baseline × Time view.

---

Coming from plotms or AIPS? See {ref}`cap-from-plotms` and {ref}`cap-from-aips`.
