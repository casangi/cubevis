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
  reach is wider than the box. A single box can also be extended to the
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

## From Python

```python
visplot(vis="obs.ms", flag_preview=True,
        flag_reach="shared-antenna,channels", flag_reason="RFI, scan 4")
```

Custom filters can be added with `flag_filters={name: function}`; they
run only with local data.

## Limits

- Flag boxes are refused on the antenna × antenna matrix.
- AIPS keyboard shortcuts are deliberately not reproduced.
- There is no `flagdata` command export; JSON export and writing to the
  MS replace it.
