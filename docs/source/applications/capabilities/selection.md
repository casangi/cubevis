# Selecting data

**Status:** available.

## What it is

Which samples are plotted is set by a data selection. As in CASA tasks,
it can be given by:

- field (one, several, ranges or wildcards)
- spectral window
- correlation
- antenna and baseline
- scan
- time range (UTC)
- UV range
- whether autocorrelations are included

The text forms follow CASA syntax.

## What it is for

The selection narrows the data to one stretch of time, one group of
baselines or one band before plotting. Being able to step through
fields, spectral windows, antennas and baselines one at a time is the
way to confirm, baseline by baseline, a problem found in a survey view.

## How it behaves

- **Antennas and baselines** can be chosen from lists or typed as text.
  Typed text only sets the list's choices, so the two can never
  disagree. Typed text helps on arrays with many antennas.
- **Stepping.** The selection can be stepped forward and back through
  fields, spectral windows, antennas (all the baselines to one antenna)
  and baselines. Stepping skips entries that have no data in the current
  selection, and those entries are marked as having none.
- **Autocorrelations are excluded by default.** On data that are not
  yet amplitude calibrated, such as VLBI data, the autocorrelations can be
  hundreds of times larger than the cross-correlations and would set the
  scale of every plot. They can be included, or plotted on their own.
- **Scan, time range and UV range** use CASA syntax (examples below). The
  UV range is in metres or wavelengths. Wavelengths are taken at each
  spectral window's centre frequency, and a baseline at one time is
  either wholly in or wholly out.
- **Nothing is read until a plot is asked for.** The selection can be
  narrowed first, and the window shows when what is drawn no longer
  matches the current settings.
- The status line lists the selection in force.

## From Python

```python
visplot(vis="obs.ms",
        field="3C286", spw="0,1", correlation="RR,LL",
        antenna="!EA05",                    # exclude an antenna
        scan="1~7", timerange="07:37:00~08:00",
        uvrange=">200m", autocorr="exclude")

visplot(vis="obs.ms", antenna="DA44&DV19")  # one baseline
```

`time_range=(start, end)` (UNIX seconds, UTC) and `uvdist_range=(min,
max)` (metres) are numeric alternatives to `timerange` and `uvrange`.

## Limits

- One time range at a time.
- The antenna syntax is a subset of CASA's MSSelection. Names, IDs,
  exclusions with `!`, and baselines as `NAME&NAME` are supported.
