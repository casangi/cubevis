# HRS H1: vector / scalar averaging in raster cells

*2026-10-03; revised 2026-10-05 (per-panel control, vector default). Built
against `main` at `e46febc`. Plan: `hrs_visplot_plan.md` (milestone H1).*

## What was wrong

`_raster_2d()` in both backends reduced every quantity except Z-Score as
`mean(per-sample quantity)`. For Phase that is the arithmetic mean of
wrapped angles in degrees, which is not a phase: samples at +179 and -179
degrees (2 degrees apart) averaged to 0 instead of 180. Any Phase raster
cell covering samples on both sides of the wrap was wrong, most visibly on
Time x Baseline and Frequency x Baseline, where a whole dimension is
averaged. Amplitude was scalar-averaged only, so it could not show loss of
coherence.

## Definitions

| Mode | Amplitude | Phase |
|---|---|---|
| `vector` (default) | abs of mean(V) | angle of mean(V): amplitude weighted |
| `scalar` | mean of abs(V): what every Amplitude raster was before 2026-10 | circular mean: direction of the mean unit phasor, each sample weighted equally |

Real and Imaginary are linear, so they are the same in both modes. Flag
fraction and Z-Score ignore the mode. Flagged samples and NaN padding are
excluded; a cell with no usable sample is NaN. When the reduced dimensions
all have size one (single-baseline waterfall) the per-sample value is
shown, as before.

## Where it lives

- **Per raster panel.** `VisibilityRaster(averaging=...)`, the read-only
  `averaging` property, and `update_axes(averaging=...)`. Two rasters can
  show the same selection averaged differently; vector next to scalar
  Amplitude is a direct coherence check.
- **GUI:** an *Averaging* Select in each slot's raster gear tab, under
  *Raster quantity*. Like the Y/X/quantity pickers it is read when Plot is
  pressed and travels in that slot's part of the plot request
  (`panels[slot].averaging`). The sidebar control added on 2026-10-03 is
  gone.
- **Constructor / task:** `averaging=` on `VisibilityPlotter` and
  `visplot()` sets the initial value for every raster panel.
- **Transport:** `SelectionSpec.averaging`. The panel stamps its own mode
  onto a *copy* of the selection just before `query_raster`; the selection
  the plotter shares between panels is never changed by it, so scatter
  frames are no longer invalidated by a mode change (the 2026-10-03
  version did that). No reader-protocol or wire signatures changed.
- **Backends:** `data/_raster_average.py::reduce_amp_phase()`, called from
  `_raster_2d()` in both.

## Default: vector (changed 2026-10-05)

`selection.DEFAULT_AVERAGING = "vector"` is the single definition; every
default refers to it except `VisibilityPlotter.__init__`, which must spell
the literal because `sync_layers` copies it into the generated task
layers (a test pins the two together).

Reason: HRS commissioning users come from AIPS, and AIPS and plotms
average visibilities vectorially unless told otherwise.

**Consequence: every Amplitude raster that averages more than one sample
per cell changes.** Time x Baseline and Frequency x Baseline always do;
Time x Channel for a single baseline does not. Well-calibrated data on a
detected source barely moves. Amplitude falls, toward zero in the limit,
where the averaged samples are incoherent: noise-dominated data,
uncalibrated phases, residual delay across the averaged channels, residual
rate across the averaged times. Z-Score rasters and scatter plots are
untouched. To get the old picture: Averaging = Scalar in the gear tab, or
`averaging="scalar"`.

## Verified

- `test_raster_averaging.py`: 121 passed. Adds, since 2026-10-03: the
  default is vector everywhere; a raster panel stamps its own mode on a
  copy and leaves the shared selection alone; two panels on one selection
  query with different modes; an unchanged mode does not re-query.
- Whole `tests/manual/visplot` directory with and without these changes,
  same environment: identical (1000 passed; the same 11 failures and 3
  collection errors in reconnection/remote/lifecycle tests that need
  packages or data absent there; 679 skipped for lack of a real MS).
- No test in that directory compares raster Amplitude values against a
  scalar mean, so the skipped real-data tests are not expected to fail on
  the default switch; they have not been run.
- `scripts/sync_layers --check` clean after regeneration.

## Verified in the browser (Darrell, 2026-10-05)

Two raster panels on TW Hya, Time x Baseline, Amplitude, one Vector and
one Scalar, set from each slot's gear tab: the calibrator scans agree and
the target scans are clearly darker in Vector.

## Title

For Amplitude and Phase the default raster title names the mode,
"Amplitude (vector)  [Time vs Baseline]  pol=XX", so two panels that
differ only in averaging can be told apart in a screenshot. Other
quantities are titled as before; a custom title still wins.

## Not verified

- The title change in a browser (the title is sent with the same response
  that already updates it on a quantity change).
- The real-data tests, the MSv4 format on real data, and the remote path.

## Known remaining issue (H1b)

Averaging is right inside the backend, but two later stages still treat
the Phase image as an ordinary scalar field:

1. the Level-1 Datashader `Canvas.raster()` resample, which averages agg
   cells into screen pixels when zoomed out;
2. linear interpolation when upsampling (`raster_interpolate="linear"`).

Either can blend +179 and -179 degree cells into values near 0. Stride
decimation (`_decimate_agg`) is unaffected: it picks cells, it does not
average them. The fix is to carry cos/sin planes through the resample and
take the angle afterwards; it is scheduled with the phase waterfall preset
and cyclic colormap in H3.
