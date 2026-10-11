# HRS H6, slice 2: Spectrum and Time series presets

*2026-10-09 (third session). Built against `main` at `7c026d8`. Plan:
`hrs_visplot_plan.md` (H6). Slice 1 (scatter averaging) is
`hrs_h6_scatter_average.md`. Also in this delivery: the busy-indicator
fix (section 5) and two display fixes found while checking the presets
(section 4).*

## 1. What they are for

The two line plots a commissioning astronomer reads one baseline at a
time:

- **Spectrum**: amplitude above phase against frequency, each scan
  averaged. AIPS POSSM, plotms "amp / phase vs frequency, averaged in
  time". Shows the bandpass shape, edge channels, narrow-band
  interference, and a phase slope across the band (a delay error).
- **Time series**: amplitude above phase against time, all channels
  averaged. AIPS VPLOT, plotms "vs time, averaged over channels". Shows
  phase drifts and jumps, amplitude dropouts and how steady the gains
  are from scan to scan.

## 2. What the user sees

Two toolbar buttons after All-BL: **Spectrum** and **Time series**. Each
sets:

- both panels to scatter, Over / Under, the same X (Frequency or Time);
- Amplitude in the top panel (slot A), Phase in the bottom one (slot B);
- in both gear tabs, Average over time Scan / Off and Average over
  channels Off / All (Vector or Scalar is left as it is);
- a Z-Score (Statistical) colouring back to Continuous, as every preset
  does.

The selection is left alone. Over every baseline at once the views are
a cloud. They are meant to be read one baseline at a time: tick one, or
step with Baseline ◀ ▶. Antenna ◀ ▶ shows every baseline to one
antenna. Both work with a preset showing, because Prev / Next re-plots
with whatever the panels are set to. The pan / zoom X link (slice 1)
keeps the two panels together.

`preset="spectrum"` / `"timeseries"` in the constructor and the task do
the same. `scatter_avg_time` / `scatter_avg_chan` override the preset's
averaging when given (anything but `"off"`).

Hover help for both buttons in the status area.

## 3. Autocorrelations

H6 asked to confirm that autocorrelations can be selected and plotted.
**They could not, from an MSv2.** xarray-ms leaves them out unless
asked (`auto_corrs=False` by default) and nothing in visplot asked, so
they were silently missing: not in the Baseline table and not plotted.

Now `MSv2Backend.open` asks for them when the MS has them
(`has_autocorrelations`: ANTENNA1 == ANTENNA2 in the first or last 100k
rows). It does not ask otherwise, because xarray-ms asked on data
without autocorrelations adds an empty baseline per antenna. The probe
reads only the ends of the table: correlators write autocorrelations
with every integration, and reading the whole of ANTENNA1 / ANTENNA2 of
a large MS takes tens of seconds (0.2 s for TW Hya's 80k rows).

An MSv4 has whatever its conversion kept. A Processing Set made with
xarray-ms's defaults has none.

Autocorrelations are listed as "A&A (auto)" in the Baseline table (the
marking existed already), are averaged like any baseline, and commit
flags by antenna name like any baseline.

For HRS this matters more than for ALMA. VLBA-style amplitude
calibration (AIPS ACCOR) starts from the autocorrelations, and the
autocorrelation spectrum is the first look at a station's bandpass and
interference.

## 4. Display fixes found on the way

**Panels rendered at their construction size.** The datashader canvas of
a panel was sized from `plot_width` / `plot_height` when the panel was
built (500 x 550, side by side) and never followed the figure. In Over /
Under the figure is about 1020 x 280, so each bin was drawn twice as
wide and half as tall: scatter points became flat dashes and rasters
lost horizontal resolution. This affected every Over / Under view, not
only the new presets.

Now every Plot and redraw request carries the plot frame's size
(`inner_width` / `inner_height`: the image fills the frame, not the
axes), and the panel renders at it (`VisibilityPlot.set_pixel_size`).
A change of the figure's size (layout, presets, the sidebar toggle)
triggers a redraw of its own (`js_on_change("width" / "height")` on the
same debounced redraw). The browser skips that redraw while the figure
is not on screen, and Python skips it when the size has not changed
(`size_only`). A scatter drops its two-level references when the size
changes, because they were binned for the old canvas.

**Sparse scatter points were one or two screen pixels.** A one-baseline
averaged spectrum is a few thousand points on a canvas of a few hundred
thousand pixels. `spread_sparse` (`visibility_scatter.py`) grows each
point into its empty neighbours (3 x 3 bins) when under 2 % of the
image is drawn and a bin is under 3 screen pixels. This is like plotms's
"autoscaling" symbol, which draws larger points when there are few. It
changes the display only: hover readout, flag boxes and the data are as
before.

**Toolbar too wide for a laptop.** With two more buttons the toolbar no
longer fits a 1440 px window. The row now wraps (CSS `flex-wrap`), so
Export and the theme toggle move to a second line instead of being cut
off. At 1700 px it is still one line. If the second line is unwelcome,
shorter labels and a narrower layout switch would win back about 130 px.

## 5. The busy indicator (the open H6 slice 1 bug)

Cause: `window.__cvSetBusy` gave up after a fixed 30 s
(`GIVE_UP_MS`) whatever was still in flight, so a Plot taking longer
showed idle before it finished. Reproduced headlessly with a 40 s delay
on the Plot reply (`SLOW_PLOT=40`): idle at 30.1 s, image at 45.8 s.

Fix: the give-up became a watchdog. After 30 s, and every 5 s after
that, it asks the page's CommMgr(s) whether a request is still awaiting
a reply that can arrive (connected or reconnecting; in-flight requests
are replayed after a reconnection). If one is, busy stays. If none is,
the busy state cannot be cleared by anything (a response callback threw,
the transport shut down, reconnection paused), so it gives up as before.
cubevisjs `CommMgr` gained `inFlight()` and `canReply()` and a page
registry `window.__cvCommMgrs`; with an older bundle the watchdog falls
back to the fixed 30 s. Headless, with the same 40 s delay: busy held
to the final image at 47.8 s; a busy state with nothing in flight still
cleared at 30 s.

## 6. Verified

- `tests/manual/visplot/test_scatter_presets.py`, 23 tests, both
  backends where data are read:
  - autocorrelations: found in an MS (both ends probed), absent from one
    without and not invented there, listed and marked, averaged;
  - presets from the constructor: kinds, Y per panel, X, averaging,
    layout, titles; explicit averaging wins; the buttons' JS; hover help;
    each gear tab's Y select shows its own panel's Y;
  - values: a Plot as the buttons send it, one cross baseline and one
    autocorrelation (Spectrum) and one baseline (Time series), against
    numpy, amplitude and phase;
  - display: `set_pixel_size`, the redraw message (same size: nothing
    drawn; new size: an image of the new shape), the JS wiring,
    `spread_sparse`.
- `tests/manual/visplot/test_busy_watchdog.py`: the watchdog under node
  with a virtual clock (long request held, leak given up, no registry,
  no reply possible, a throwing manager).
- `test_baseline_combine.py`: its preset-table invariant now includes
  the two new presets.
- Live in headless Chromium against a running plotter (TW Hya, 3c279):
  Spectrum on DA44&DA45, Baseline ▶ to DA44&DA46, Time series; image
  shape against figure shape; toolbar at 1440 and 1700 px; the busy
  timeline above.

## 7. Not done / open

- ~~**The Baseline table lists every antenna pair, including those with no
  data.**~~ Done 2026-10-10, section 8. On TW Hya 3c279, DA41 and DA42 have no rows, so Baseline ▶
  from the top shows about 50 empty plots before the first one with
  data. Narrowing the tables (or the stepping) to baselines with data
  needs a "which baselines have rows" pass over the selection. xarray-ms
  pads missing (time, baseline) cells with NaN and flags, so it can be
  read from the data already cached. Not built: worth asking whether it
  bites on HRS data, which is likely to be complete.
- Autocorrelation detection reads the ends of the table only. An MS whose
  autocorrelations appear only in its middle would be opened without
  them.
- Spectrum over several SPWs: each SPW lies at its own frequencies along
  the X axis (Frequency is absolute), which is what the plan asks.
  Checked only on single-SPW data (TW Hya, the simulator).

## 8. Follow-up after Darrell's macOS test (2026-10-10, base `3d34624`)

**Busy cursor lasting after the plot, Plot enabled.** Darrell's sequence:
a preset, a long Plot, a two-finger scroll to reach the lower panel; the
plot under the pointer zoomed and a long redraw started. Not reproduced
headlessly (the overlay blocked the wheel there), so three causes were
dealt with:

- *Wheel zoom off by default.* Bokeh's wheel zoom acts on any scroll over
  a plot (there is no focus to lose), and trackpad momentum keeps sending
  scroll events after the fingers lift, after the overlay has gone. Now
  `active_scroll=None`: a scroll over a plot scrolls the page, and the
  wheel-zoom toolbar button turns zoom on (in both visible panels: the
  toolbar sync). Decided with Darrell.
- *The overlay follows the page.* It was placed once, in viewport
  coordinates; a page scroll, or a preset's layout change after busy
  started, could move a plot out from under it. It is now placed again
  every 200 ms and on any scroll or resize while it is up.
- *The watchdog no longer counts hover readouts.* It held busy while any
  request was in flight; hover readouts are requests, sent continuously
  while the pointer moves, so a leaked busy count could be held for as
  long as the user kept moving the mouse. It now holds only for a
  request that has waited at least 5 s (`CommMgr.oldestWaitMs()`). The
  request a real busy period waits on is always that old, because the
  30 s timer restarts at every busy change. No leak was found; this
  removes the way one would have shown as "busy forever".

**Pin layout.** Each preset already switched to its own layout (vplot
side by side, Spectrum and the waterfalls Over / Under). The layout
preference the Layout buttons record per axes (`pref_src`) is written
but never read. New: a "Pin layout" checkbox (sidebar, section "Panels",
with help) and `pin_layout=` argument. Ticked, presets keep the current
arrangement (One, Side by Side or Over / Under) and only set what each
panel shows; the Layout buttons do not pin. `pin_layout=True` with
`preset=` keeps the `layout=` argument.

**Stepping skips baselines and antennas without data.** On TW Hya 3c279,
Baseline ▶ from the top showed about 50 empty plots first. Every Plot
response now carries, per Antenna and Baseline table row, whether it has
rows in the selected field / scan / SPW / time range (the antenna and
baseline choice ignored). Prev / Next step over those only, and the
tables dim the others and mark them "(no data)". A ticked antenna
without data steps to its neighbour. Presence is finite UVW in the
partitions (xarray-ms pads absent rows with NaN), so a fully flagged
baseline still counts and is stepped through; that is why there is no
"All" checkbox (agreed with Darrell). `XArrayReader.baselines_with_rows`,
forwarded by `LocalVisibilityReader` and `RemoteReductionContext`
(memoised like `axis_info`), cached per field / scan / SPW / time range:
0.4 s for TW Hya 3c279 on first use.

**Verified.** `test_layout_pin_and_stepping.py` (13 tests: pin from the
constructor and in every preset's JS, rows with data on both backends
against the MS's own ANTENNA1/2 and the Plot response, the stepping under
node, wheel zoom off), `test_busy_watchdog.py` (a manager reporting
request age: an old request holds busy, short ones do not),
`test_remote_reduction_context.py` (`baselines_with_rows` remote equals
local, both backends). Live: Spectrum with Side by Side pinned stays
side by side; Baseline ▶ from nothing ticked lands on DA44&DA45 (the
first with data); dimmed rows; a wheel over a plot leaves the ranges
alone.

## 9. Busy long after Plot is enabled again (2026-10-11)

Darrell, after the section 8 delivery: after a long Plot (Time series,
All fields) the busy indicator always stays up well after Plot is
enabled again, and the plots look finished. It does end eventually.

**Cause: the redraws that follow a Plot re-read the data.** After a Plot
each panel redraws once more when the Plot's new ranges arrive, and once
more when a preset changed the figure's size. A redraw is meant to
resample the reference the Plot left (Level-1, no data read). Two things
sent them back to the data:

- The resize redraw (section 4) dropped the scatter's references, so it
  was always a full query.
- When a selection's frames are larger than the frame cache (TW Hya,
  field "TW Hya": a 993 MiB frame against an 803 MiB budget; All fields
  more), every query re-reads them, and the memo of averaged points only
  matched when the very same raw frame object came back. So every redraw
  of an averaged scatter re-read and re-averaged the whole selection.

Before section 5 the 30 s give-up hid this: the indicator went idle while
these redraws were still running.

Reproduced with field 3c279 and `CUBEVIS_VISPLOT_FRAME_CACHE_MB=1`
(nothing fits), Time series button: Plot 11 s, then the indicator stayed
up a further **7.4 s** (redraws of 4.7 s and 7.4 s) on `3d34624`.

Fixes:

- **A resize keeps the references.** The canvas is scaled to the new
  size and the redraw resamples (`VisibilityScatter._pixel_size_changed`);
  `_can_resample_locally` still sends it to the data when a reference is
  too coarse.
- **Averaged points come from the memo without the raw frames.** The
  state (frame token, selection, generation, pending-flag version, flag
  view, averaging) fixes the result, a commit clears the memo, and an
  averaged result is small, so it may outlive its raw frame
  (`_query_columns_cached_raw`). Averaged layers also read their frame
  once (with Real and Imaginary) instead of twice.
- **The status line follows the layout.** It said "Side by Side" under
  an Over / Under view (Darrell's screenshot): Plot now sends the layout
  shown, and Python's copy (status line, PNG export) follows it.

After: the same run stays busy **2.2 s** after Plot (two redraws of
1.8 s, which are numba compiling datashader's resampler on its first use
in the process); a second preset in the same session, **0.7 s** (redraws
of 0.15 s).

Tests: `test_layout_pin_and_stepping.py` section 5 (no read for a
repeated averaged query with nothing cached; a resize redraws without a
query; the status line).

Still open: the first-use numba compile (~2 to 3 s, once per session).
Warming it at start-up would hide it; not done.
