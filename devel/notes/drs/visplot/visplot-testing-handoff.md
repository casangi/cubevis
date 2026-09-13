# `visplot` testing handoff

Status: duo-mode (two-panel) implementation is functionally complete and
has been tested extensively during development, but that testing was
interactive and turn-by-turn — this document is for anyone picking the
code up fresh to verify it systematically, or re-verify after future
changes.

See `visplot-implementation-plan.md` for the design/architecture context
behind *why* things work the way they do. This document is about *what*
to test and *how*.

---

## Before you start

**Test file:** `sis14_twhya_calibrated_flagged.ms` (ALMA Band 7, TW Hydra,
4 SPWs, 43 antennas, 48 channels, XX/YY polarizations, pre-flagged).
Download: `https://casa.nrao.edu/download/devel/casavis/data/sis14_twhya_calibrated_flagged.ms.tar.gz`
(~230 MB). This is the file every round of testing during development used.

**Environment note:** open the exported HTML in a real browser with dev
tools open. Several bugs found during development were only visible via
`console.log` output or console errors/warnings (`console.warn` calls are
deliberately left in several places as permanent diagnostics, not
temporary debug cruft — see "Diagnostics already in place" below).

**No Bokeh server.** This is a core architectural constraint, not
incidental — everything Python↔JS is comm-message-based
(`ctrl.send(...)`/registered handlers), never Bokeh's native
`.on_change()`. If you're extending this code and reach for
`widget.on_change(...)`, stop — it will silently never fire in the actual
deployment (see the `colormap_controls()` finding below for a real
example of this exact mistake already in the codebase).

**Correctness vs. structural testing — these are different questions.**
Everything below tests whether the *UI behaves correctly* (does clicking
X do what X should do). It does **not** test whether the *values are
scientifically correct* (does the raster actually show the right amplitude
for the right baseline/time). That's a separate, harder verification —
see "Reference testing" in the implementation plan, not yet started.

---

## Diagnostics already in place

A few `console.log`/`console.warn` calls were deliberately left in the
code (not cleaned up) because they were useful during debugging and cost
nothing to leave:

- `doPlot()` logs the outgoing request and the incoming response:
  `[visplot doPlot] sending panels: ...` / `[visplot doPlot] received
  status: ... panels: ...`. Check this first if a Plot press seems to do
  the wrong thing — it tells you immediately whether the problem is in
  what was sent, what came back, or the client-side handling of the
  response.
- The cursor-span matching loop logs computed axis labels and resulting
  span locations per panel per hover event
  (`[visplot cursor-span] <panel_id> p_x_label: ... vspan.location: ...`).
  Verbose — expect to filter/search console output when using this.
- `console.warn('panel 0/1 update failed:', e)` in `doPlot()`'s response
  handler — if you ever see this fire, it means an exception was thrown
  while applying a response to a figure; treat it as a real bug report,
  not noise.
- Server-side `log.warning`/`log.error` calls throughout `_handle_plot()`
  — check terminal/server output alongside the browser console; several
  bugs during development were only fully diagnosed by cross-referencing
  both.

---

## GUI elements to test

### Toolbar (top)

- [ ] Sidebar collapse/expand button (`⟨`/`⟩`)
- [ ] Plot ▶ — replots both panels using current configuration
- [ ] Reload ↺ — reloads data and replots, clears any pending flags
- [ ] Layout control: One / Side by Side / Over-Under — test all three,
  including switching between them after other state changes (a kind
  switch, a swap) to catch staleness bugs (see "Known-tricky
  interactions" below)
- [ ] Presets (vplot / radplot / Waterfall) — each should force both
  panels to specific configurations and Side/Over layout
- [ ] Dark/Light toggle — see "Dark/light theming" checklist below
- [ ] Tooltips on every toolbar element (hover, wait ~1.5s)

### Per-panel gear tab (click the gear icon on either figure's toolbar)

- [ ] Gear opens the correct tab, expanding the sidebar if collapsed
- [ ] Gear is present and functional on **both** kinds of a panel — click
  gear while a panel shows raster, switch to scatter via Plot, confirm
  gear is *still there* on the now-visible scatter figure (this exact
  case was a real, blocking bug during development — the gear tool used
  to only exist on whichever kind was active at page-load time)
- [ ] Title turns red with a plain "Panel A"/"Panel B" placeholder on
  gear-click; restores to the real title + original color on: (a)
  Cancel, (b) a successful Plot that didn't actually change that panel's
  axes, (c) a successful Plot after switching kind *within* the same open
  tab before pressing Plot. All three of these were separately broken and
  fixed during development — don't assume (b) or (c) work just because
  (a) does.
- [ ] Cancel — discards the open tab's changes, closes it, restores the
  original title/color
- [ ] Raster/Scatter switch — client-side only, doesn't itself trigger a
  replot; changes which config sub-panel is visible within the tab
- [ ] Swap — see "Swap feature" checklist below
- [ ] All four tab controls (Raster/Scatter switch, Cancel, Swap,
  tooltips) render in two rows, not one overflowing row
- [ ] Raster Y/X axis conflict (same dimension for both) — inline warning
  appears immediately in the tab; Plot press is silently refused
  client-side (confirm nothing appears in the `doPlot() sending panels`
  console log — it should not even attempt to send)
- [ ] **Validation-error auto-focus** — trigger the Y/X conflict on one
  tab, switch to the *other* tab, press Plot: focus should switch back to
  the tab with the actual conflict, not do nothing silently

### Two-panel kind combinations

Test all four, not just the default (raster+scatter):

- [ ] Raster + Scatter (default)
- [ ] Raster + Raster
- [ ] Scatter + Scatter
- [ ] Switching between combinations repeatedly, including switching back
  to a kind that was already rendered once (should reuse cached data, not
  recompute — see "Recompute-gating" below for how to verify this isn't
  just a UI illusion)

### Swap feature

- [ ] Swap while both panels are different kinds
- [ ] Swap while both panels are the same kind
- [ ] Swap, then re-open gear on the panel that moved — confirm it opens
  the correct tab for the content actually there
- [ ] Swap, then use "One" mode — confirm the panel that becomes primary
  is the one that actually swapped into that position (this specific
  interaction needed its own fix — the sizing logic used to hardcode
  which slot was "primary" regardless of any swap)
- [ ] Swap is reversible — swapping twice returns to the original
  arrangement

### Cursor-span crosshair tracking

- [ ] Hovering in one panel shows a crosshair in the other panel *only*
  when they share at least one matching axis dimension — not a fixed
  raster/scatter pairing
- [ ] A panel matching the hovered panel on **both** axes (e.g. one shows
  Time vs. Channel, the other shows Channel vs. Time) gets **both** its
  vertical and horizontal spans set, not just one
- [ ] No spans appear when there's no dimension match at all

### Dark/light theming

Toggle both directions (dark→light and light→dark) and check every
element gets recolored, not just the obviously visible ones:

- [ ] Figure backgrounds, axes, titles
- [ ] Sidebar widgets (selects, buttons)
- [ ] Status bar / notification text
- [ ] Config-field hint text (Field/SPW/Scan/Antenna/Time/UV-range hints)
  — this was missing entirely until found during testing; confirm it's
  actually fixed, not just present
- [ ] Source file path text — should be green in dark mode, **black** in
  light mode (a specific request, not an accessibility default) — the
  color used to be permanently green regardless of mode

### Recompute-gating (harder to verify — needs more than eyeballing)

Both raster and scatter now skip recomputation when nothing about a
panel's axes/selection actually changed. This is hard to confirm purely
visually since "nothing visibly changed" and "recompute was skipped" look
identical from the UI. Suggested approaches:

- Watch server-side logs/timing for a Plot press on an unchanged panel —
  should be near-instant, no backend query.
- If you have any way to instrument or time `update_axes()` calls, an
  unchanged-panel Plot press should not call it at all for that panel.
- At minimum, confirm the *response* still looks correct even when
  recompute is skipped — the cached image should still display, not go
  blank. (This exact failure mode — an unchanged panel going blank — was
  a real bug found and fixed; see "Known-tricky interactions" below.)

---

## Known-tricky interactions (worth deliberately exercising, not just incidentally covering)

These are combinations that specifically broke during development because
something was bound once at construction time instead of resolved
dynamically — the single most common bug pattern in this codebase's
history. If you're testing after a future change, these are the highest-
value places to check first:

1. **Layout mode + kind switch** — switch a panel's kind, then use
   One/Side/Over or a preset. (Fixed, but re-verify after any future
   change to `layout_js` or the preset builders.)
2. **Layout mode + swap** — swap panel positions, then use One/Side/Over,
   specifically "One" mode.
3. **Gear session spanning a kind switch** — open gear, change the
   Raster/Scatter switch *without* pressing Plot yet, then press Plot.
   Confirm the *previously* active kind's figure gets its title/color
   correctly restored, not left stuck red.
4. **An unchanged panel next to a changed one** — change only one panel's
   config, press Plot, confirm the *other*, untouched panel doesn't go
   blank or lose its displayed image. (This specific case — the "panel 1"
   response-handling code path had a bug the "panel 0" path didn't — was
   found via exactly this test.)
5. **Simultaneous requests on both panels** — modify both panels' configs
   via their gear tabs before pressing Plot once; confirm both actually
   update, not just the first one processed.

---

## Process suggestions

- **Use the browser console proactively, not reactively.** The
  `doPlot()` request/response logging exists specifically so you don't
  have to guess whether a problem is client-side or server-side — check
  it first, every time something looks wrong, before forming a hypothesis.
- **When something looks broken, try to reproduce the *exact* failure in
  isolation before reporting it** — several bugs during development
  turned out to depend on a specific *sequence* of actions (e.g. #3 above
  only happens if the kind switch happens *before* Plot, not after), and
  a vague "X doesn't work" report costs much more round-trip time than a
  precise repro.
- **A "looks fine" result on first try is not the same as "confirmed
  correct."** Several serious bugs (the panel-1 corruption, the shared
  selection-tracker crash) only appeared in *specific* combinations that
  a single straightforward test wouldn't hit. Working through the "Known-
  tricky interactions" list above catches most of these.
- **Re-run the full checklist after any change that touches shared JS
  state** — anything using `self._display_order_source`,
  `self._panel_kind_switch`, or the `_plot_js_args` dict is reachable
  from multiple UI paths, and a fix in one path has repeatedly turned out
  to need the identical fix in a sibling path (see "Known-tricky
  interactions" #4 for a concrete example of exactly this).
