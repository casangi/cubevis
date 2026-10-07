# HRS H4, slice 2: the all-baseline waterfall (FTFLG view)

*2026-10-07. Built against `main` at `fa052b4`. Plan:
`hrs_visplot_plan.md` (closes H4). Slice 1: `hrs_h4_raster_axes.md`.*

## Why

AIPS FTFLG is SPFLG with every baseline in one image: a quick survey for
interference common to many baselines, instead of one image per
baseline. visplot could already draw it (Time x Channel with no single
baseline ticked reduces over the selected baselines, and a flag box
there applies to all of them), but two things were wrong for that use:

- The cell was the panel's *Averaging* applied across baselines. With
  the default, Vector, different baselines were added as complex
  numbers. Their phases agree only on a calibrated point source at the
  phase centre; on anything else they cancel. On TW Hya, all fields, the
  all-baseline amplitude read 1.4 where the baselines' amplitudes
  average 11.3.
- Nothing said, before a flag was accepted, that it reached every
  baseline. The AIPS documentation warns about exactly this view: "a few
  bad baselines can make it look like all are bad and cause you to flag
  too much".

## What the user sees

- **Baselines combined** (new control in each raster's gear tab;
  constructor and task argument `baseline_combine`). It matters only
  where Baseline is not one of the raster's axes and more than one
  baseline is selected. *Averaging* applies to the samples of each
  baseline; this says what is done with the baselines:

  | Choice | Cell shows | Use |
  |---|---|---|
  | **Mean** (default) | the average of the baselines' amplitudes | the general picture; a problem on a few baselines is diluted |
  | **Maximum** | the largest baseline | interference or a bad antenna shows even if one baseline has it |
  | **Coherent** | amplitude of the baselines added as complex numbers | a calibrated point source only: baselines agree and noise averages down |

  Help for it appears in the status area while the pointer is over the
  control, like the other gear-tab controls.
- **The title names it**: `Amplitude (max of baselines)  [Time vs
  Channel]`, `Amplitude (vector, baselines added coherently)`. Nothing
  is added where the raster has a Baseline axis or one baseline is
  selected.
- **All-BL** preset button (constructor `preset="waterfall-all"`): Time
  x Channel amplitude over Amplitude vs Channel, and the button sets
  Baselines combined to Maximum. The *Waterfall* button sets it to Mean.
  As with `zscore`, a constructor-time preset sets axes only: pass
  `baseline_combine="max"` with it.
- **Flag messages say how many baselines**: `✓ Flagged: 135 samples on
  15 baselines.` and, with review on, `Review the proposal: 30 samples
  on 15 baselines.` (Any flag that reaches more than one baseline says
  so, on any plot.)

## Behaviour change

A raster without a Baseline axis, several baselines selected, quantity
Amplitude or Phase: the default is now Mean where it was, in effect,
Coherent. Amplitudes on such plots are larger than before (TW Hya: 11.3
against 1.4). `baseline_combine="coherent"` gives the old picture.

## What each quantity does

| Quantity | Mean | Maximum | Coherent |
|---|---|---|---|
| Amplitude | mean of per-baseline amplitudes | largest per-baseline amplitude | amplitude of the summed visibilities (as *Averaging* says) |
| Phase | mean direction of the baselines' phases, each counted equally | same as Mean (a direction has no maximum) | phase of the summed visibilities |
| Amp V Diff, Phase Diff | mean, as before | largest baseline | same as Mean |
| Real, Imaginary | mean, as before (linear, so Mean = Coherent) | same as Mean (signed) | mean |
| Phase RMS, Coherence | each baseline measured by itself, pooled (unchanged) | | |
| Z-Score | always the maximum (unchanged) | | |
| Flag | always the fraction (unchanged) | | |

With two displayed axes out of time, baseline and frequency, a cell that
covers several baselines covers exactly one sample of each, so
*Averaging* has nothing to do within a baseline and is left out of the
title for Mean and Maximum. The code handles the general case (it will
matter for H7, antenna by antenna, where time and frequency are both
reduced).

## How it is built

- `selection.py`: `BASELINE_COMBINES`, `DEFAULT_BASELINE_COMBINE`,
  `normalize_baseline_combine`, `SelectionSpec.baseline_combine`
  (transport, stamped per panel at query time, like `averaging`).
- `data/_raster_average.py`: `reduce_amp_phase_baselines` (within each
  baseline by `averaging`, then across baselines),
  `reduce_plain_baselines`, `combines_baselines`. Lazy, sums and one
  max. Both backends' `_raster_2d` call them in place of
  `reduce_amp_phase` and the plain mean.
- `visibility_raster.py`: per-panel `baseline_combine` (constructor,
  property, `update_axes`), `n_baselines_combined`, the title.
- `visibility_plotter.py`: the control (`bcombine_sel`), its hint
  (`_STATIC_HINTS["bcombine"]`), the Plot request, `_PRESET_SETS`, the
  preset and its button.
- `flag_controls._count_text`: baselines in the count.
- No TypeScript change.

## Verified

- `test_baseline_combine.py`, 69 tests:
  - the reductions against numpy on numpy- and dask-backed arrays, with
    flags, across the phase wrap, with and without something to average
    inside each baseline;
  - both backends on simulated data where every baseline has its own
    amplitude and phase: Mean equals the mean of the per-baseline
    waterfalls, each queried by itself (the plan's exit test); Maximum
    equals their maximum; Coherent reads under half of Mean; one
    baseline selected is that baseline in every mode; nothing changes
    where Baseline is an axis or for Flag, Z-Score, Real; MSv2 = MSv4;
  - a raster: per panel, shared selection untouched, re-query on
    change, titles;
  - a plotter: the control and its options, its help wired to the hover
    wrapper, the Plot request (changed, unchanged, absent, junk), the
    preset table and button JS, the constructor preset; a box on the
    all-baseline waterfall flags those cells on every baseline and the
    message gives the number; review states it before accepting; a box
    with one baseline selected does not mention baselines;
  - the generated task layers carry the argument.
- Three modes on TW Hya, both backends: identical numbers (mean 11.31,
  max 32.16, coherent 1.44).
- `scripts/sync_layers --check` clean after regeneration.
- Headless Chromium on simulated data (no kernel): page loads without
  script errors, the All-BL button is in the toolbar, the title reads
  "Amplitude (max of baselines)", each raster gear tab has the control.

## Not verified

In a browser with a kernel: that the control replots, that its help
shows on hover (the gear tab could not be opened headlessly), that the
All-BL button sets the control when its tab has never been opened (the
assignment is wrapped in `try`, as for the other presets).

## Not built

- Median across baselines (robust, but needs a sort; the reductions so
  far are streaming sums).
- A count of baselines in the title.
- The same choice for scatter (H6, with the averaging controls).
