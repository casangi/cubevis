# HRS H5, slice 2: a curated set of flag filters

*2026-10-09. Built against `main` at `3d04908`. Plan:
`hrs_visplot_plan.md` (H5). Agreed with Darrell: a short list of
statistical filters with at most two settings each rather than many
knobs; user-supplied Python filters stay as an expert feature and are
not extended; no chains of filters.*

## What a filter is for

A box says where to look; a filter says which samples in it to take.
With a filter a generous box can be drawn round a bad stretch and only
the bad samples in it are flagged. "All selected (immediate)" takes
everything (AIPS TVFLG style) and remains the default.

## The list in the Flagging panel

| Filter | Takes | Settings | For |
|---|---|---|---|
| All selected (immediate) | every sample in the box | — | ordinary flagging |
| **Value range** (new) | samples whose value of the quantity the panel shows lies in [Low, High] | Low, High (either may be empty) | dropouts (High only), strong interference (Low only); CASA `clip` |
| **Outlier from neighbours** (new) | amplitudes far from the running median of the 9 samples around them | Compare along (time / channel), Cutoff (sigma) | spikes in time, narrow-band interference; CASA `tfcrop` / AIPS FLAGR in spirit |
| Z-Score above cutoff | as before | Cutoff, Match per | what the Z-Score view shows bright |
| Amplitude outlier (MAD) | as before | Sigma | a baseline's samples off its usual level |
| Phase deviation | as before | Max deviation | calibrators, where phases should agree |
| **Grow around flags** (new) | unflagged samples next to flagged ones (on disk or pending) | Grow along (time / channel / both), Samples | the weak edges of interference a cutoff misses; CASA `extend` |

- **Value range** works on the panel's quantity, so the numbers typed
  are those of the colour bar or axis: Amplitude, Phase (degrees), Real
  or Imaginary. On a panel showing anything else (Phase RMS, Z-Score,
  ...) it refuses with a message. On a scatter panel the layers must
  show one quantity. It replaces "Amplitude range" in the list;
  `amplitude_range` stays registered (scripts, the remote API, records
  saved before) but is not listed.
- **Reference population** is no longer a control: "auto" (per
  spectral window on a raster, the whole selection on a scatter, as the
  Z-Score colouring does) is always used.

## How the neighbour filters work

Both need samples outside the box: a spike at the edge of a box is
judged against the samples beyond it, and a sample next to a flagged
one may be just outside. They are "reference" filters: `prepare()` gets
the whole current selection for the baselines and correlations the box
touches and works out the answer on that grid (per baseline,
correlation and spectral window); `mask()` looks the box's samples up.

Outlier from neighbours, per baseline and correlation:

1. Unflagged amplitudes only (flagged ones are not neighbours).
2. Runs along time are cut at scan boundaries; along channel the run is
   the window.
3. Each sample is compared with the median of the 9 samples centred on
   it, *itself left out*. Near the end of a run the window moves inward,
   keeping 9 samples.
4. The scatter is 1.4826 x the median |residual| over the baseline's
   whole selection; a sample is taken when |residual| > cutoff x scatter.
   At least 3 usable neighbours are needed for a verdict.

Two details found by measurement (pure noise, 20,000 runs):

- With the sample in its own window it is often the median; its
  residual is then 0 and the measured scatter reads low by about a
  third, so ordinary noise crossed a 5-sigma cutoff at 0.1-0.5 % of
  samples. Left out: about 1 per million.
- Windows cut short, or filled by reflection, at the ends of short
  scans scatter more than inner ones (0.05 % false alarms at the ends of
  8-integration scans). The inward-shifted window has none. It is off
  centre by up to 4 samples, which matters only if the level changes by
  a noise rms within that many integrations or channels.

Grow: flagged samples (on disk or pending; padding excluded) dilated by
the chosen number of samples along time (within a scan), channel, or
both; the unflagged ones reached are taken.

## Corrected after Darrell's first test (2026-10-09)

- *Describe pending flags could not be reached.* Hovering a control
  near the bottom of the sidebar showed its help in the status area,
  which then grew, shrank the sidebar above it and pushed that control
  out from under the pointer; the help vanished, everything moved back.
  Reproduced live (sidebar 722 px high, 688 px while the Export help
  showed). The status / help area now has a fixed height
  (`_STATUS_HEIGHT`, five lines of help) and a longer help text scrolls
  inside it; nothing above moves. The plots lose about 30 px of height
  for it.
- *A whole-raster and a whole-scatter Outlier box gave different
  counts* (11,786 and 18,748). Not a fault: the raster shows one
  correlation (XX) and the scatter both. Checked on all fields of TW Hya
  in the engine: the two take exactly the same 11,786 XX samples; the
  scatter's other 6,962 are YY.

## Verified

- `tests/manual/visplot/test_flag_filters_curated.py`, 32 tests:
  - filters alone on made-up data: bounds of Value range, Phase in
    degrees, refusal on a derived quantity; Outlier takes a spike and
    not a drift, along time and channel, ignores flagged samples, uses
    neighbours outside the box; scan segments; Grow along time, channel,
    both, and width 2; at most two controls, no reference control;
    the curated order with a user filter after it and Amplitude range
    hidden;
  - on a simulated MS with spikes at known places (and its MSv4 twin),
    checked on the flags themselves: Outlier along time and along
    channel takes exactly the spikes; a box on one integration still
    uses its neighbours; Value range with Low, Low+High, and a bound
    that takes nothing; refusal on a Phase RMS raster; Value range on
    the scatter; Grow after Outlier, then Undo takes back only the
    grown samples; Grow stays in the box;
  - the panel: the list, empty Low / High, status-area help for each
    filter.
- TW Hya 3c279, Time x Channel, whole raster: Outlier along time 22
  samples of 1.3 million in 1.3 s, along channel 16 in 1.5 s; Value
  range 0.5 s; Grow 0.9 s.
- Live in headless Chromium against a running plotter: the list, each
  filter's description and two controls, and an Outlier box flagging
  22 samples.

## Not verified

- How the Outlier cutoff feels on real HRS data, and whether 9 samples
  is the right window. Both are easy to change (`RUNNING_WINDOW`) if the
  first real data say otherwise.
