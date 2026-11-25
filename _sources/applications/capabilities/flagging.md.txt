(cap-flagging)=

# Flagging

**Status:** available.

## What it is

Interactive editing in the style of AIPS TVFLG / SPFLG / FTFLG, designed
for a web window rather than for keyboard commands:

- **Boxes.** A flag or unflag box can be drawn on any raster or scatter
  plot (except the antenna matrix).
- **Filters** decide which of the samples inside a box are flagged:
  - all of them (the default, as in TVFLG)
  - a value range on the plotted quantity
  - outliers from their neighbours (a running median along time or
    channel)
  - [Z-Score](z_score.md)
  - amplitude outliers (by MAD)
  - phase deviation
  - growing around existing flags
- **Reaches** widen a box beyond what is drawn, like the TVFLG flagging
  scopes. A box can extend to:
  - all baselines to the antenna that the drawn baselines share, to
    every antenna drawn, or to all baselines
  - all channels
  - all selected spectral windows
  - the whole scan
  - all fields
  - all correlations

  Reaches stay set until changed, and the window warns whenever any
  reach is wider than the box. The baseline, spectral-window, scan and
  field reaches apply to a raster box with the *All selected* filter.
  With any other filter, or on a scatter plot, only the channel and
  correlation reaches apply. A single box can also be extended to the
  full height or width of the plot without changing the reaches.
- **Preview.** A box can be shown as a proposal first: how many samples
  it would flag, on which baselines, antennas, windows and scans, with
  those samples highlighted. The proposal is then accepted or rejected.
- **Pending flags** form a list, with undo and redo, a report, and an
  optional reason stored with each flag.
- **Commit.** Nothing is written to the data until it is committed, and
  committing first saves a backup of the flags it changes. Pending flags
  can also be exported as JSON, as a record or to apply elsewhere.

## What it is for

The goal is to find a problem on the view that shows it best and flag
it there, at the scope it really has. Use the reaches for a bad antenna
or a bad integration across the band. Use the filters to take only the
outliers out of a busy region.

## How it behaves

- Pending flags are either hidden or drawn in a color. Flagged data can
  also be shown, so that an unflag box can select them.
- A box on an averaged plot, or on a cell that covers several baselines,
  flags every sample behind what it encloses.
- Flags are applied at the full time and frequency resolution of the
  data, whatever the averaging used to display it.

(cap-flagging-calc)=

## How the filters decide

A filter is applied to the samples inside the box as drawn. What it
takes is then limited to samples whose state the action can change:
unflagged samples for a flag box, flagged ones for an unflag box. The
channel and correlation reaches then extend each sample taken to all
channels or all correlations. The baseline, spectral-window, scan and
field reaches apply only to a raster box with *All selected*. Statistics are always taken from unflagged samples
only, and the data weights are not used.

| Filter | Takes a sample when | Default |
|---|---|---|
| All selected | always (AIPS-style immediate flagging) | |
| Value range | the value of the quantity the panel shows (Amplitude, Phase, Real or Imaginary) is between Low and High; either bound may be left empty | |
| Outlier from neighbours | the amplitude differs from the median of its neighbours by more than the cutoff times $\sigma$ (details below) | 5 σ, along time |
| Z-Score | its {ref}`Z-Score <cap-zscore-calc>` is above the cutoff; or, matching per cell, the largest Z-Score in its raster cell is above the cell cutoff | 3.5 |
| Amplitude outlier (MAD) | $\lvert A - \operatorname{median}(A)\rvert \,/\, (1.4826\,\mathrm{MAD}) >$ the cutoff | 5 |
| Phase deviation | its phase is further than the limit from the reference's median direction, $\arg(\operatorname{median}\mathrm{Re} + i\,\operatorname{median}\mathrm{Im})$ | 45° |
| Grow around flags | it is unflagged and within *width* samples (in time, channel, or both) of a flagged sample, on disk or pending | 1 sample, both |

**Outlier from neighbours, in detail:**

- **Comparison.** Each sample's amplitude is compared with the median of
  its 9 neighbours along time (or along channel), *not including
  itself*. Near the ends of a run, the 9-sample window is moved inward
  rather than cut short.
- **Runs.** Runs are cut at scan boundaries.
- **Scatter.** $\sigma$ is $1.4826 \times$ the median absolute difference
  from those running medians, for the baseline and correlation, in each
  spectral window.
- **Too few neighbours.** A sample with fewer than 3 unflagged
  neighbours gets no verdict.
- **Samples outside the box.** It needs them, so it is worked out on the
  whole selection, and the box only says which of the results to take.
  Grow around flags works the same way.

**Reference populations.** Z-Score, Amplitude outlier and Phase
deviation judge each sample against one baseline and one correlation:

- on a scatter plot, over the whole current selection;
- on a raster, in each spectral window separately.

This matches what the panel shows.

## From Python

```python
visplot(vis="obs.ms", flag_preview=True,
        flag_reach="shared-antenna,channels", flag_reason="RFI, scan 4")
```

Custom filters can be added with `flag_filters={name: function}`; they
run only with local data.

(cap-flagging-limits)=

## Limits

- Flag boxes are refused on the antenna × antenna matrix.
- AIPS keyboard shortcuts are deliberately not reproduced.
- There is no `flagdata` command export; JSON export and writing to the
  MS replace it.

---

Coming from plotms or AIPS? See {ref}`cap-from-plotms` and {ref}`cap-from-aips`.
