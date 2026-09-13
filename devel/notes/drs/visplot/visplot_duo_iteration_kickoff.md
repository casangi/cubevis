# Kickoff — Duo-mode Iteration (Phase 2.5 / I-1)

**Status:** Ready to schedule — the single-selector-vs-independent-buttons design
question is resolved (confirmed against `msview`'s own documentation, August 2026).
**Scope of this session:** I-1 only. I-2 (Polarization) and I-3 (Antenna/Baseline/
Scan/Time) are explicitly out of scope — see §6.
**Primary reference:** `visibility_plotter_implementation_plan.md`, Phase 2.5 (between
Phase 2 and Phase 3) and Appendix C.8. This document summarizes what's needed to start;
the plan is the authoritative source if anything here is unclear or looks stale.

---

## 1. What "done" looks like

- One "Animate: Field | SPW" selector (radio or small dropdown) plus one Prev/Next
  button pair, placed in the toolbar or sidebar.
- Pressing Next/Prev advances through the currently-selected subset of whichever axis
  is chosen, re-plots both panels synchronously, and either wraps at the ends or clamps
  — pick one and say so in the PR, don't leave it undecided.
- The non-animated axis stays exactly as currently selected. This needs no new "hold
  fixed" mechanism — see §3.
- The status bar / title reflects current position, e.g. `Field 3/7: 0637-752` or
  `SPW 2/4: 1`.
- Tests added per §5.
- The plan updated per §7 — this is not optional; undocumented delivered work is
  exactly the kind of drift this project's maintenance conventions exist to prevent.

---

## 2. Why this design — don't relitigate, the evidence is in Appendix C.8

`msview` (the tool this replaces) treats the MS as a five-axis array (Time, Baseline,
Polarization, Channel, Spectral Window). The user picks two axes for the raster, then
explicitly assigns **exactly one** of the three remaining axes to be the Animator; the
other two are pinned via sliders. That's the documented precedent for building this as
a single selector, not independent Prev/Next per axis. Source: CASAdocs, "2-D
Visualization and Flagging of Visibility Data (viewer/msview)."

Field and SPW were chosen for this first cut — not Antenna, Baseline, Scan, or Time —
because they're the only two selection axes already wired end-to-end from sidebar to
backend query. The others aren't (tracked separately; see §6).

---

## 3. Mechanism — reuse existing plumbing, don't rebuild it

Confirmed by reading `visibility_plotter.py` directly (not inferred):

- `_build_selection()` (~L2762) already turns `self._field_str` / `self._spw_ids` into
  `SelectionSpec`. No changes needed here.
- `_handle_plot()` (~L2333) already accepts `field` and `spw_ids` in an incoming
  message and updates instance state before rebuilding the selection. Prev/Next should
  send this exact message shape — don't invent a parallel one.
- **Field** is a plain `Select` (`self._field_select`). Its `options` list has an
  `("", "All fields")` sentinel as entry 0 — skip it when computing next/prev; iterate
  `self._meta.fields` (a stable ordered tuple) instead.
- **SPW** is *not* a dropdown — it's a `DataTable`/`ColumnDataSource`
  (`self._spw_source`) using row-selection (`selected.indices`), because SPW
  identities can be non-contiguous ids or bare names. Stepping SPW means setting
  `selected.indices = [i]` for exactly one row, walking `self._meta.spws` (also a
  stable ordered tuple) — the same mechanism a manual single-SPW pick already uses
  correctly.
- The `doPlot()` CustomJS — already shared across Plot ▶, Reload ↺, and every preset
  button (see the "Shared plot-send logic" comment block, ~L3982) — already reads
  `spw_src.selected.indices` and `field_sel.value` and sends via `ctrl.send()`.
  Prev/Next's handler should call into this same function (extract/parameterize it if
  needed) rather than duplicate the send logic a fourth time.
- Holding the non-animated axis fixed needs **no new mechanism**: `_handle_plot()`
  only updates keys present in the incoming message, so simply omit the non-animated
  axis's key from the Prev/Next payload.

---

## 4. Architecture rules that apply (§3.1c of the plan — do not violate)

- **Rule 1 (no-server constraint).** Any handler that changes what's drawn must
  *return* new data for the client to install — assigning `ColumnDataSource.data` in
  Python silently does nothing. Prev/Next's re-plot must go through the same
  request/response round trip as Plot ▶, not a Python-side direct source mutation.
- **Rule 3 (the restyle body is one unit with its construction site).** If the new
  "Animate:" selector or Prev/Next buttons are added to the sidebar/toolbar, they must
  also be added to `_THEME_RESTYLE_JS` in the same commit, or they won't follow a
  later theme toggle. Run the existing `node`-based syntax check on
  `_THEME_RESTYLE_JS` after editing it.
- Skim the other three rules in §3.1c before starting; confirm none apply to the
  DataTable row-selection or `Select`-value mechanisms this touches.

---

## 5. Testing expectations

Match the project's established discipline (see "Export testing discipline," carried
forward from E-2 in the plan):

- Pure step-computation logic (given current field/SPW index and count, compute
  next/prev, handle wraparound, skip the sentinel) should be covered by standalone
  tests needing no MS, no Bokeh, no display — these should run in seconds.
- Any new/changed JS (button handlers, the `doPlot()` refactor) gets the same
  `node`-based syntax check already used for `_THEME_RESTYLE_JS`.
- Manual verification against `sis14_twhya_calibrated_flagged.ms` — but note its
  single-SPW limitation (documented elsewhere in this project) makes SPW *iteration*
  effectively untestable beyond "does the mechanism not crash." Field iteration is the
  one axis on this dataset that can be meaningfully verified end-to-end, since sis14
  has multiple fields.

---

## 6. Explicitly out of scope for this session

- **I-2** (Polarization/Correlation iteration) — separate follow-on, only after I-1
  ships and only if Polarization can be exposed as single-value-at-a-time (it's
  currently a `CheckboxGroup` for multi-select display).
- **I-3** (Antenna/Baseline/Scan/Time) — blocked on `antenna=`/`scan=`/`timerange=`
  selection wiring, tracked separately (Appendix C.8, reference-testing phase). If
  implementing I-1 makes this look tempting to just also wire up, don't — flag it back
  to the plan instead of scope-creeping.
- **Auto-play** — `msview` supports continuous animation beyond single-step Prev/Next;
  that's not requested here. Manual stepping is the full scope.
- **Averaging-aware iteration** (Phase 3, V-8/V-9) — out of scope. This is strictly
  discrete re-selection, no binning, no backend averaging changes.

---

## 7. When finished

- Update `visibility_plotter_implementation_plan.md`: mark I-1 ✅ Done with date, note
  any mechanism divergence from §3 above, update Appendix B's file inventory if new
  files were created.
- Update `visibility_plotter_preview.md`: move the iteration bullet from "what the
  preview explicitly omits" to the appropriate "what the preview includes" section.
- Report anything that surprised you relative to §3/§4 — that's exactly the kind of
  thing that becomes a new Appendix C item, or, if it's a defect pattern rather than a
  one-off, a candidate sixth architecture rule.
