# Zooming a decimated raster

*2026-10-07. Built against `main` at `50b414f`. Follows from "Found, not
changed" in `hrs_h4_raster_axes.md`. Also in this delivery: Median for
"Baselines combined" (see `hrs_h4_baseline_combine.md`).*

## Why now

A raster with more cells than `max_cells` (2 million) is decimated: the
backend keeps every k-th row and column. The TW Hya test data never
reach that (410 x 384), so the path was hardly exercised. HRS-sized data
will live on it: 8 hours at 1 s by 2048 channels is 59 million cells for
one baseline. The flag scopes of H5 will be used from exactly these
views.

## What was wrong (all at `50b414f`)

| | Effect |
|---|---|
| 1 | Zooming in on a **Channel** axis until more detail was needed raised `zero-size array to reduction operation minimum`. The zoomed selection put channel *numbers* into `freq_range`, which is in Hz, so nothing matched |
| 2 | After any such re-query the panel held **only the zoomed region**. Zooming back out drew that region alone on an otherwise blank plot |
| 3 | The same re-query moved the panel's ranges, so the cursor readout counted **elapsed time from the zoomed region** while the axis went on counting from the start |
| 4 | Both backends reported "decimated" only from their final pass. A store with **one partition** (one spectral window, one field: the usual HRS case) was strided in the per-partition pass and came back "not decimated", so the raster **never asked for detail at all** and a zoom only magnified the strided cells |

Had 1 been fixed alone, a Channel axis would have come back renumbered
from 0 in the zoomed region.

## What happens now

The aggregate of the whole selection is never replaced. When it was
decimated and the viewport is finer than it can show, the zoomed region
(plus half its width again on each side) is queried at up to four times
the cell budget and held *beside* it (`VisibilityRaster._detail`).

- The picture, the flag overlays and the cursor readout come from the
  detail while it covers the viewport and is fine enough; otherwise
  from the full aggregate.
- A small pan, zooming back out, and zooming back in to the same place
  cost no query. A pan beyond the margin, or a deeper zoom into a detail
  that is itself still decimated, queries again.
- Axis ranges, the time origin, the colour scaling and the colour bar
  stay those of the whole selection, so colours do not shift between
  the two levels.
- A Channel axis keeps the channel numbers of the selection. The zoomed
  query is narrowed in frequency through the full aggregate's reference
  frequencies (or by moving `channel_range`, when the selection has
  one, since the backends give that precedence over `freq_range`), and
  the result is renumbered back.
- A new plot, a flag change or an overlay change drops the detail; the
  next draw asks again if it is still needed.
- A viewport in a gap holds no data and is simply blank. A detail query
  that fails logs a warning and the full aggregate draws instead.

## Limits, by choice

- **Only Amplitude, Phase, Real, Imaginary and Flag get detail.**
  Z-Score is scored against each baseline's whole selection, and Phase
  RMS, Coherence, Amp V Diff and Phase Diff are computed within time
  windows (scans). A query narrowed to the viewport would give
  different numbers from the picture it replaces, worst at its edges.
  Those stay on the full aggregate when zoomed: coarse, but the same
  numbers. Lifting this needs the narrowing to respect scan boundaries
  (and, for Z-Score, a reference carried over from the full selection).
- **A Baseline axis does not gain baselines on zooming.** It is laid out
  from the full aggregate, and the detail is asked for exactly the
  baselines already drawn in that stretch of the axis (a raster strided
  along Baseline as well would otherwise get back a different subset,
  possibly none of them on the axis). More baselines come only with
  fewer antennas selected. Not a limit for a few-antenna array.
- Decimation is still by striding (every k-th cell), not averaging, so
  the zoomed-out picture of a huge raster can miss a narrow feature that
  the zoomed-in one shows. Unchanged here; "Baselines combined:
  Maximum" does not help along time or channel. Worth its own look
  (max or mean over the strided block) when real HRS data arrive.

## How it is built

`visibility_raster.py` only, plus the one-line report fix in each
backend's `query_raster`:

- `_Detail` (aggregate in the full raster's coordinates, the extent
  asked for, whether it is itself decimated, its overlays).
- `_ensure_detail` decides; `_finer_than` is the test (along a
  narrowable axis, pixels smaller than the typical cell *and* the
  viewport under 40 % of what is held, so the viewport a detail was
  made for never asks for another); `_detail_selection` narrows;
  `_to_full_coordinates` renumbers channels and places baselines;
  `_query_detail` puts them together.
- `_shade_viewport` picks the level; `_shown_agg` gives the readout the
  aggregate the picture came from.
- `_do_viewport_rerender` always answers with the viewport asked for.
  It used to answer with the zoomed selection's own extent after a
  re-query.

No change to the reader protocol, the remote path or the TypeScript.

## Verified

`test_raster_zoom.py`, 48 tests, both backends, on a simulated MS with a
pause in time and `max_cells` forced low:

- twelve of them (the four defects above) fail on `50b414f`;
- the detail holds exactly the undecimated data of the region and its
  margin, on Channel and Frequency axes and on Time x Baseline in
  length order; channel numbers are those of the selection, with and
  without a `channel_range`;
- the readout reads the detail while zoomed and the full aggregate
  after zooming out;
- query counts: one on zooming in, none for a small pan, zooming out or
  returning, one more beyond the margin; none when not decimated, for a
  modest zoom, or for the quantities above;
- the answer is the viewport asked for; ranges, grid size, time origin
  and state are unchanged by a zoom;
- a viewport in a pause; a detail query that raises; a new render;
- on a plotter: a flag box drawn while zoomed flags what it encloses on
  every baseline, and after the redraw the pending-flag overlay is in
  the detail and inside the box.

`test_visibility_raster.py`: the one test that required the re-query
to *replace* the aggregate now requires the aggregate kept and a finer
detail held (and aims at a row that exists; the middle of TW Hya's time
range is a gap).

On TW Hya with `max_cells=4000`: Channel and Frequency axes zoom in with
one query, pan with none, and zoom out to the same picture as before.

## Not verified

In a browser: nothing here changes the page, but the behaviour is only
reachable with data beyond 2 million cells per raster (or `max_cells`
lowered), which the browser sessions so far have not had.
