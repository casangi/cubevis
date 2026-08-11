# `visplot` reference-testing sources

Status: starting point for the reference-testing phase described in
`visibility_plotter_implementation_plan.md`. Sources here are deliberately
external to `cubevis`/`visplot`/`casagui` (with one exception, noted below,
kept because it's the actual design lineage document rather than a testing
reference). Companion to `visplot-testing-handoff.md` (structural/UI
correctness) — this document is about *value* correctness against
trusted reference tools.

---

## 1. Design lineage (context, not a test reference)

**`casangi/casagui` wiki — "Visibility Visualization"**
https://github.com/casangi/casagui/wiki/Visibility-Visualization

The original design doc for the tool that became `visplot`. Explicitly
frames raster and scatter as covering two legacy tools in one app:

> Raster displays of any pair of [Time, Frequency, Baseline, Correlation]...
> addresses the 'msview' use-case.
> Scatter plots with X/Y axis flexibility similar to current casaplotms...
> addresses the 'plotms' use-case.

This is the spec duo-mode's raster+scatter pairing is implementing —
worth rereading when deciding whether an observed `visplot` behavior is
"correct" (matches the legacy tool it's replacing) vs. a deliberate
`visplot`-specific improvement.

---

## 2. PlotMS (scatter-side reference)

**CASADocs — "Data Examination/Editing", Plot/Edit using plotms**
https://casadocs.readthedocs.io/en/stable/notebooks/data_examination.html

The single most useful page — full `plotms` parameter reference with
GUI-tab-by-GUI-tab correspondence. Key sections to test `visplot`'s
scatter mode against:

- **Axes tab** — full axis list (Metadata / Visibility values & flags /
  Observational geometry / Calibration / Ephemeris groups). `visplot`
  scatter should be checked against at minimum the Metadata and
  Visibility-values groups (Scan, Field, Time, Spw, Channel, Frequency,
  Corr, Antenna1/2, Baseline, Amp, Phase, Real, Imag, Wt) plus
  Observational geometry (UVdist, UVwave, U/V/W).
- **Averaging** — channel, time (+scan/field), all-baselines vs.
  per-antenna, all-spw, vector vs. scalar. `visplot`'s averaging
  implementation (Phase 3, not yet started per the implementation plan)
  should match this semantics when built — flagging this now as a
  forward reference.
- **Atmospheric/Tsky overlay** (`showatm`, `showtsky`, `showimage`) —
  this is almost certainly what you meant by "tsys over plots": an
  overlay curve (not a Tsys *axis*) requiring Channel or Frequency as
  x-axis, computed from weather subtables via the atmosphere tool. Not
  in the current `visplot` implementation plan at all — worth adding as
  a tracked future item alongside the F-9/F-10 flagged-overlay work,
  since it's architecturally similar (second overlay layer). There is
  also a distinct **Tsys axis** for Tsys calibration tables specifically
  — different feature, worth not conflating the two when scoping.
- **Iteration (Page tab)** — `iteraxis` options (scan, field, spw,
  baseline, antenna, time, corr, poln, antpos), `xselfscale`/`yselfscale`
  (global axis range) and shared-axis-on-grid options. Direct reference
  for `visplot`'s not-yet-built iteration/grid mode
  (`visplot-development-handoff.md`).
- **Interactive tools** — Locate, Mark/Subtract/Clear Regions, Flag/
  Unflag/Flag All, Hold Drawing. Useful cross-check for `visplot`'s
  flag-tool UX decisions (F-1 through F-11 in the implementation plan).
- **Colorize (`coloraxis`)** — scan, field, spw, antenna1/2, baseline,
  channel, corr, time, observation, intent, poln, antpos. `visplot`
  scatter's compositing/coloring should be checked against this list for
  parity, not just against what's currently implemented.

**"First Look at Imaging" CASA Guide** (multiple versions — CASA 6, 6.4,
4.4, CDE) — uses `sis14_twhya_calibrated_flagged.ms` directly, the same
file `visplot` testing already standardizes on.
https://casaguides.nrao.edu/index.php?title=First_Look_at_Imaging_CASA_6

Concrete, reproducible reference calls on our exact test MS:
- UV coverage: `plotms(vis='sis14_twhya_calibrated_flagged.ms', xaxis='u', yaxis='v', avgchannel='10000', avgspw=False, avgtime='1e9', avgscan=False, coloraxis="field")`
- Amp vs. UVdist, iterated by Field (Axes tab: X=UVDist, Data=Amp; Page tab: Iteration Axis=Field)

**"Inspecting Data" CASA Guide** — worked plotms + msview walkthrough on
a different MS, useful for the *sequence* of operations (see §4 below)
more than for axis reference, since it's a different dataset.
https://casaguides.nrao.edu/index.php/Inspecting_Data

---

## 3. msview (raster-side reference)

**CASADocs — "2-D Visualization and Flagging of Visibility Data (viewer/msview)"**
(current URL structure keeps changing — search "2-D Visualization and
Flagging of Visibility Data msview" if this 404s)
https://casa.nrao.edu/casadocs/casa-5.1.0/data-examination-and-editing/2-d-visualization-of-visibility-data-msview

Key facts extracted so far, to expand once fetched in full:

- **Selection parameters**: field, spectral window, time range, uv range,
  antenna, corr, scan, array, ms selection expression — same vocabulary
  as `plotms`, good cross-check that `visplot`'s selection controls
  (Field/SPW/Scan/Antenna/Time/UV-range per the testing handoff) cover
  the same set on both panel kinds.
- **Display**: Raster only for MeasurementSets ("similar to AIPS task
  TVFLG").
- **Display Axes roll-up**: data is internally a 5-axis array — **Time,
  Baseline, Polarization, Channel, Spectral Window**. Any two become the
  raster's X/Y; the remaining three get sliders/animator (i.e. msview's
  raster is inherently N-dimensional with a 2D slice shown, not
  restricted to a fixed axis pairing). Baseline axis can be ordered by
  antenna1-antenna2 (default) or unprojected baseline length — a
  specific, testable option worth checking whether `visplot` raster's
  axis-order semantics match.
- **Basic Settings (min/max/scaling)**: "Lowering the data maximum will
  help brighten weaker data values" — direct reference behavior for the
  currently-broken `colormap_controls()` min/max fields once wired up
  (see conversation history above for the confirmed bug).

**msview task reference (CASADocs, current)**
https://casadocs.readthedocs.io/en/stable/api/tt/casaviewer.msview.html

**"VLA CASA Flagging" CASA Guide** — uses both `plotms` and `msview` in
the same workflow, explicitly recommending both:
https://casaguides.nrao.edu/index.php/VLA_CASA_Flagging-CASA6.2.0

> Once the bulk of the RFI and corrupted data has been removed... it is
> worth having another look at the data with the visualisation tools,
> either plotms or msview.

Also documents msview's default axes (Time vs. Baseline) and confirms
the 5-axis set (time, baseline, channel, correlation, spectral window)
independently of the CASADocs chapter above.

---

## 4. Concrete synergy test scenarios

These are the actual cross-tool consistency checks, not just per-tool
structural correctness — the thing duo-mode raster+scatter is supposed
to let you do that neither legacy tool alone could:

1. **Same selection, both views, on `sis14_twhya_calibrated_flagged.ms`.**
   Duo-mode Raster + Scatter, identical Field/SPW/Scan/Antenna selection
   on both panels. Raster: Time × Channel (msview-equivalent, default
   axes per the VLA flagging guide). Scatter: Amp vs. UVdist (plotms
   reference figure already in hand from the First-Look guide). Check
   that structure visible in one (an outlier cluster in the scatter, a
   bright/dark region in the raster) traces to the same underlying rows
   in the other — this is the actual value of having both in one
   backend rather than two independent tools.
2. **Colorize-by parity.** plotms `coloraxis` vs. msview's baseline
   ordering — test whether `visplot` scatter's "colorize by" and
   raster's axis-order option produce visually consistent groupings for
   the same selection (e.g. colorize scatter by antenna1, compare
   against raster with baseline ordered by antenna).
3. **RFI-hunting workflow parity**, directly modeled on the "Inspecting
   Data" guide's sequence: start with default Amp vs. Time scatter,
   spot low-amplitude outliers, switch X axis to Baseline + colorize by
   Channel to isolate a bad antenna — then confirm the *same* antenna
   shows up as an anomalous region in a Time × Baseline raster of the
   same selection. This is a good one to run early since it's a
   documented, known-answer workflow (not just "does it look
   reasonable").
4. **UV coverage cross-check.** The First-Look guide's `u` vs. `v`
   scatter reference plot vs. a `visplot` raster of the same MS with
   comparable axes (if U/V are exposed as raster axis options at all —
   worth confirming whether they are, since msview's native 5-axis set
   doesn't include U/V directly).

---

## 5. Open gaps to fill in next

- Full msview "Data Display Options" chapter fetch (got summary/roll-up
  description above, not the complete Basic Settings parameter list —
  worth pulling in full before testing the colormap fix against it).
- A msview-specific worked example on `sis14_twhya_calibrated_flagged.ms`
  itself hasn't turned up yet — everything found so far uses this MS
  for `plotms`/imaging, not for `msview` raster specifically. May not
  exist; if not, the "Inspecting Data" or "VLA CASA Flagging" guides'
  worked examples on their own MS remain the best raster-side reference,
  used for workflow/behavior parity rather than exact-figure matching.
- `showatm`/`showtsky` atmospheric overlay: not yet in the implementation
  plan's punch list at all — flagging for a decision on whether it's in
  scope before doing reference testing against it.

---

## 6. Launching `visplot` for reference testing

`visplot` (the function users actually call — a thin execution-environment
wrapper around `VisibilityPlotter`, see `visplot.py`) is what every test
document below should specify as its launch command, not direct
`VisibilityPlotter(...)` construction, since that's what users will
actually run.

**Relevant parameters** (from `visplot.py`'s docstring):

| Parameter | Purpose | Status |
|---|---|---|
| `ms` / `ps` | MSv2 path / MSv4-Processing-Set path — exactly one required | functional |
| `backend` | `"auto"`, `"casa6"`, `"radps"`, `"remote"`, `"null"` | functional |
| `field` | field name or index string | functional |
| `spw` | comma-separated SPW indices | functional |
| `correlation` | comma-separated correlation labels | functional |
| `datacolumn` | `"data"`, `"corrected"`, `"model"` | functional |
| `layout` | `"one"`, `"side"`, `"over"` | functional |
| `preset` | `"vplot"`, `"radplot"`, `"waterfall"`, or `None` | functional |
| `antenna` | MSSelection antenna string | **stored, not yet wired** |
| `scan` | MSSelection scan string | **stored, not yet wired** |
| `timerange` | MSSelection time-range string | **stored, not wired** |
| `uvrange` | UV range string | **stored, not wired** |
| `time_range`/`freq_range`/`uvdist_range` | tuple/list range filters | (not separately confirmed — worth checking if a test needs these) |
| `compact_toolbar` | auto-hide per-figure toolbars | functional |

**Important discovered caveat**: `antenna`, `scan`, `timerange`, and
`uvrange` are explicitly documented in `visplot.py` itself as accepted-
but-inert at construction time. Any test wanting to pre-select one of
these at launch won't currently work — this is about the *construction
parameter* specifically; whether the corresponding GUI selector controls
(mentioned generally in the testing handoff) are separately functional is
a different, GUI-checklist-level question, not yet confirmed either way.

**Standard launch pattern for these test documents**: launch with just
`ms`/`backend` (defaults for everything else), then use the GUI itself
(Field selector, panel-kind switch, axis pickers, etc.) to reach the
tested state — this doubles as GUI-element exercise per the combined
testing approach, rather than construction-parameter shortcutting past
the controls we're also trying to test.

```python
visplot(ms='sis14_twhya_calibrated_flagged.ms')
```

---

## 7. MS provenance / calibration-state caveat (discovered during test 02)

**Resolved, decisively.** Three independently-sourced copies of
`sis14_twhya_calibrated_flagged.ms` (a `casavis` devel mirror, a
`canfar.net`/ARC copy, and a fresh pull from NRAO's own canonical
bulk-data link for this exact guide version) all show identical
structure: `DATA` only, no `CORRECTED_DATA`, 1 SPW. This ruled out "bad
ad hoc local copy" as the explanation for test 02's discrepancy.

The real explanation, confirmed via `setjy(..., standard='Butler-JPL-
Horizons 2012', usescratch=True)`: this MS's `DATA` column, despite
"calibrated" in the filename, was never brought onto an **absolute flux
scale**. `setjy` computes Ceres's true physical flux on this MS as 7.34
Jy — matching the CASA Guide's own reference figure (~7 Jy peak)
almost exactly — while `DATA`'s actual amplitude values run 5–20x
higher. The guide's author's reference figures were generated from data
at a calibration state (amplitude-gain-calibrated) that this delivered
MS was never brought to.

**Practical implication for all future tests against this MS**: this is
not just a test-02-specific issue. **Any comparison involving absolute
amplitude/flux values** against this guide's figures will show the same
kind of mismatch, for the same reason, regardless of which tool is
used — confirmed by `visplot` and real `plotms` independently agreeing
with each other on this file. This is not a `visplot` correctness
question. Structural/shape comparisons (does a trend exist, does a
control work, does the tool distinguish two known-different cases)
remain valid and are the right kind of test to run against this MS;
absolute-value comparisons need either a properly amplitude-calibrated
version of this MS (not currently available) or should be dropped in
favor of relative/shape checks.


---

## 8. Testing methodology, revised (post test-02 investigation)

**New default going forward**: pair a `plotms` command with an
equivalent `visplot` command, run both against the *same local MS in
the same session*, and compare their outputs directly — rather than
comparing a `visplot` run against a CASA Guide's static published
figure.

**Why**: test 02's investigation showed that guide figures may have
been generated at a different calibration state than what's in the
publicly delivered MS (§7) — a real confound that has nothing to do with
`visplot`'s correctness, and is expensive to rule out after the fact.
Comparing two tools against identical live data sidesteps that entire
class of problem. If `plotms` and `visplot` disagree with each other on
the same data, that's a real, actionable signal; if they agree with
each other but not with a guide figure, that points at the guide
figure's provenance, not at `visplot`.

**Trade-off, accepted deliberately**: this approach uses construction
parameters (`visplot(ms=..., field=..., preset=...)`) to reach the
tested state rather than GUI interaction, so it doesn't exercise GUI
elements as a byproduct the way earlier tests did. That coverage isn't
lost — it's tracked separately under the structural GUI checklist
(`visplot-testing-handoff.md`), which remains a to-do item on its own.

**Reachability via construction parameters alone**: full GUI-free setup
is only possible when the target axis combination matches one of the
three built-in presets:

```python
_PRESETS = {
    "vplot":     (Axis.BASELINE, Axis.TIME,    Axis.AMPLITUDE,
                  Axis.TIME,     Axis.AMPLITUDE, "side"),
    "radplot":   (Axis.BASELINE, Axis.TIME,    Axis.AMPLITUDE,
                  Axis.UVDIST,   Axis.AMPLITUDE, "side"),
    "waterfall": (Axis.TIME,     Axis.CHANNEL, Axis.AMPLITUDE,
                  Axis.TIME,     Axis.AMPLITUDE, "over"),
}
```

There's no generic construction-time axis-selection parameter — for any
axis combination outside these three presets, reaching the tested state
still needs at least one GUI step (setting the X/Y axis dropdowns)
even with this revised methodology.

**Standard test doc format going forward**:
- `plotms` command (exact, runnable)
- `visplot` command (exact, runnable — preset-based where possible)
- Prediction (what should be seen, ideally with independent physical
  grounding, not just "matches a screenshot")
- Pass criteria: do the two tools' outputs *agree with each other*

**Two things worth knowing that postdate this section, kept here as
written rather than rewritten in hindsight**:
- The "reachability" limitation above (presets only) was superseded by
  §11's explicit axis parameters — most scatter/raster axis
  combinations are fully GUI-free now, not just the three presets.
- This methodology was validated repeatedly for **scatter**
  (`plotms`/`visplot`, tests 01–03) but hit a real architectural
  blocker for **raster**/`msview` — see §13. The paired-command
  approach isn't a universal solution; it works when both tools reduce
  un-displayed axes the same way, which turned out not to be true for
  raster.

---

## 9. `field=` numeric-string gap — FIXED for `MSv2Backend` only

`visplot(ms=..., field='2', ...)` used to **not** mean `FIELD_ID=2`,
unlike `plotms`'s identical-looking `field='2'`. Traced to
`ObservationMetadata.from_backend_metadata()` in `reduction_context.py`
synthesizing `FieldInfo.field_id` via `enumerate()` over an
alphabetically-sorted name list, rather than reading the MS's real
`FIELD_ID` column — confirmed on this MS specifically (real `FIELD_ID`s:
0, 2, 3, 5, 6, non-contiguous): `field='2'` resolved to J0522-364, not
Ceres (the real FIELD_ID=2).

**Fixed, for `MSv2Backend`**: `msv2_backend.py` gained
`_field_id_map()`, reading the MS's `FIELD` subtable directly via
`arcae` (already a hard dependency — no new one added) — `FIELD_ID` is
the row index into that subtable, by CASA convention, the same
convention `plotms` itself relies on. Threaded through
`reduction_context.py` (uses the backend's real `field_ids` when
present) and `visibility_plotter.py`'s `_parse_field_string` (matches
against the real `field_id` now, returns `None` rather than a wrong
guess on no match). **Re-verified live**: `visplot(ms=..., field='2',
preset='radplot')` now correctly resolves to Ceres, confirmed via
sidebar, footer, and hover data all agreeing, and the resulting
scatter plot matching real `plotms`'s `field='2'` output in both shape
and rough Y-axis range.

**NOT fixed for `MSv4Backend` / Processing Sets.** Processing Sets
don't have a literal `FIELD` subtable the way MSv2 does, so this fix
doesn't apply there, and no separate investigation has been done yet.
`reduction_context.py`'s fallback preserves the old (wrong-on-non-
contiguous-IDs) positional-index behavior for any backend that doesn't
supply `field_ids` — currently every Processing Set falls into this.

**Practical rule for test documents, revised**: numeric `field=` is now
reliable for `visplot` when working with an MSv2 measurement set
(`ms=`), matching `plotms`'s convention directly. For a Processing Set
(`ps=`), field **names** remain the only reliable option until
`MSv4Backend` gets an equivalent fix — treat numeric `field=` against a
`.ps.zarr` set as unreliable/untested. `plotms` itself is of course
unaffected either way (it only ever operates on real MSv2 files, this
whole question is `visplot`-specific).

**Resolved as a byproduct, not independently re-confirmed**: whether
raster and scatter panels could independently disagree about which
field a shared `field=` resolved to (raised as an open question after
the original bug report, where only the raster panel's hover data was
visible). The live re-verification's raster panel correctly showed
Ceres throughout; the scatter panel doesn't have its own "Field:"
hover readout to check the same way, so this wasn't independently
confirmed for that panel specifically — but since the fix addresses
the single shared resolution point both panels read from, a
raster/scatter disagreement would be surprising at this point rather
than expected.

**Related risk**: `spw=` likely has a similar numeric-string path
(`_parse_spw_string`) — not yet confirmed whether it has the same kind
of gap, since SPW IDs in this MS happen to be contiguous (just SPW 0)
so it wouldn't have surfaced the same way. Worth keeping in mind for
any future MS with non-contiguous SPW IDs.

---

## 10. Axis-option audit — nine already-implemented options exposed

Prompted by test 01: checked `visplot`'s exposed axis dropdowns against
what the backend (`msv2_backend.py`) actually computes, not just against
`plotms`'s/`msview`'s full vocabularies. Found the gap was mostly **UI
exposure, not missing implementation**:

| | Backend-ready (`msv2_backend.py`) | Was exposed | Fixed: now exposed |
|---|---|---|---|
| Scatter X | TIME, UVDIST, UVDIST_LAMBDA, FREQUENCY, CHANNEL, U, V | UVDIST, TIME, FREQUENCY | + CHANNEL, UVDIST_LAMBDA, U |
| Scatter Y | AMPLITUDE, PHASE, REAL, IMAGINARY (+ U, V, new) | AMPLITUDE, PHASE | + REAL, IMAGINARY, U, V |
| Raster X/Y | TIME, BASELINE, FREQUENCY, CHANNEL, CORRELATION | TIME, BASELINE, CHANNEL | + CORRELATION |
| Raster quantity | FLAG, AMPLITUDE, PHASE, REAL, IMAGINARY | AMPLITUDE, PHASE | + REAL, IMAGINARY, FLAG |

`U`/`V` as scatter **Y**-axis values were the one genuine gap (not just
unexposed) — `_lazy_quantity()` only handled visibility-derived
quantities. Added, unmasked by flags (see the function's own updated
docstring for the reasoning). Verified against the real method
(`types.MethodType`-bound, not just read) for: correct values matching
`ds["UVW"]`, correct broadcast over frequency, no flag-masking even
with all data flagged, no regression on the pre-existing quantities,
and a clear `ValueError` (not a silent wrong answer) if `ds` is
missing.

**Not covered by this pass — still genuinely missing**:
- Colorize-by (`plotms`'s `coloraxis`) — no control exists at all.
- The rest of `plotms`'s vocabulary not touched here: Scan, Field, Corr,
  Antenna1, Antenna2, Baseline (as a scatter axis, distinct from
  raster's), Weight, W, Azimuth, Elevation, HourAngle, ParAngle.
- `MSv4Backend` — this audit only checked `MSv2Backend`; whether the
  same "already implemented, not exposed" pattern holds there too is
  unchecked.
- `msview`'s raster set is now fully covered (Time, Baseline, Channel,
  Correlation all exposed; Spectral Window remains selection-only, not
  a plottable raster axis, which seems like a reasonable design choice
  rather than a gap).

---

## 11. Explicit axis parameters — `raster_y`/`raster_x`/`raster_qty`/`scatter_x`/`scatter_y`

`VisibilityPlotter` (and `visplot`, generated from its docstring — see
below) now accepts explicit axis parameters, not just `preset=`:

```python
visplot(ms='sis14_twhya_calibrated_flagged.ms', scatter_x='U', scatter_y='V')
```

Reuses the exact mechanism `preset=` already used internally — no new
axis-representation scheme, no significant refactor. Precedence:
explicit argument > `preset=` > hardcoded default (Time vs. Channel
raster, UVDist vs. Amplitude scatter). Validated against the same
`_RASTER_AXIS_OPTIONS`/`_RASTER_QTY_OPTIONS`/`_SCATTER_X_OPTIONS`/
`_SCATTER_Y_OPTIONS` lists that drive the GUI dropdowns — single source
of truth, so the breadth available here is exactly whatever's in those
lists (see §10), no more. An invalid value raises `ValueError` listing
the valid options for that specific role.

**Practical effect for test documents**: any test whose target axis
combination isn't covered by `vplot`/`radplot`/`waterfall` no longer
needs a "this requires one manual GUI step" caveat — set the axes
directly in the launch command instead. Test 01 was the motivating case
(no preset covers U-vs-V) and has been updated accordingly.

**Docstring note**: `visplot.py` is generated by scraping
`VisibilityPlotter`'s own docstring/type hints in `visibility_plotter.py`
— parameter documentation belongs there, not hand-edited into
`visplot.py` directly (confirmed: a regeneration after fixing this
produced a `visplot.py` identical to an earlier hand-edited attempt,
so the docstring is the correct, durable place to maintain this).

---

## 12. Raster partition-concat ordering bug — FIXED for `MSv2Backend`

Found via test 04, the very first raster reference test — validates
prioritizing raster testing right after the axis audit closed out the
last scatter gap.

**Symptom**: a small dark/flagged "notch," visible in both `msview` and
`visplot`'s Time-vs-Baseline raster of the same MS, appeared at
*different relative Time positions* in the two tools. Both tools'
Y-axes independently confirmed to increase upward (ruled out a simple
axis-flip explanation) — a genuine ordering difference.

**Root cause**: `query_raster()` in `msv2_backend.py` concatenates this
MS's 4 partitions (split by intent/`OBS_MODE` — bandpass-cal,
amplitude-cal, phase-cal, target) via `xr.concat(..., dim=y_name, ...)`
in iteration order, with no `.sortby()` anywhere in the function. An
intent revisited at non-contiguous times (e.g. a phase calibrator
checked periodically through the observation) lands in one partition
spanning a non-contiguous time range; concatenating it as a single
contiguous block scrambles chronological order in the result whenever
that happens. `msview` reads the MS table directly, unaffected by this
intent-based partitioning, hence no discrepancy on its side.

**Fix**: `agg = agg.sortby(sort_dims)` immediately after the concat,
applied unconditionally. Verified against a synthetic scenario shaped
exactly like the real bug (4 partitions, one spanning a non-contiguous
time range, in non-chronological iteration order): confirmed monotonic
order restored, zero data loss.

**Scope**: `MSv2Backend` only, same as the `field=` fix in §9 — this
audit didn't check whether `MSv4Backend`'s raster path has an analogous
gap. Given the same underlying pattern (multiple partitions, no
explicit sort after combining them) showed up twice now in two
unrelated codepaths (`field_id` construction in §9, raster concat here),
worth treating "was this combined/concatenated result actually sorted"
as a standing question for any future backend code review, not just
these two instances.

**Confirmed fixed, live**: re-ran test 04's commands after the fix —
the notch now lands at the same relative Time position in both tools
(top of the range/latest times, left edge/lowest baseline, matching
exactly). Before the fix it was at the bottom of `visplot`'s range;
`msview`'s was always at the top. Not a false alarm.

---

## 13. `msview` animates, `visplot` averages — a real methodology blocker for raster/`msview` tests

Discovered setting up test 05 (Time vs. Channel), applies retroactively
to test 04 (Time vs. Baseline) too.

**The two tools handle axes beyond the two displayed ones completely
differently.** `msview`'s raster model: pick 2 display axes, the
remaining axis becomes an **Animator** — step through it one frame at a
time (e.g. one specific baseline, one specific channel), never
averaged. `visplot`'s raster model: pick 2 display axes,
**average** over whatever isn't displayed (confirmed directly in
`query_raster()`'s own docstring — "average over frequency (and pol)"
for Time×Baseline, etc.). These aren't two settings of the same
underlying capability — they're genuinely different reductions, and a
screenshot comparison between them was never strictly apples-to-apples,
even when it looks like one.

**Consequence for test 04**: the notch-position comparison that led to
the partition-ordering bug fix (§12) wasn't a rigorous one-to-one check
— `msview`'s screenshot was one arbitrary Channel/Correlation frame,
`visplot`'s was a full average. The bug fix itself remains fully valid
(confirmed via code-reading and synthetic reproduction, not reliant on
the screenshots matching), but test 04's document has been corrected to
not overclaim a "PASS" on raster/`msview` parity — that was never
actually established.

**Consequence for test 05**: closed without running the actual
comparison. Setting it up revealed `msview` genuinely has no
"average across baseline" option for this axis pair as far as
currently checked — Baseline becomes the Animator, full stop. Whether
an alternative exists (some `msview` averaging mode not yet found, or
a restore-file trick) is unconfirmed.

**Where this leaves raster/`msview` testing going forward**: paused,
not abandoned. Two things would unblock it: (a) `visplot` gaining
single-frame (not just averaged) raster rendering for the un-displayed
axis — ties directly to the already-known `antenna=` "not yet wired"
gap (§ — see `visplot.py`'s own docstring), since single-baseline
selection is exactly the kind of capability that would let `visplot`
match `msview`'s per-frame model when needed; or (b) confirming whether
`msview` has an averaging option this investigation didn't find. Until
one of those is true, further raster/`msview` axis-pair tests would
likely just re-demonstrate this same mismatch rather than produce a
meaningful pass/fail on structure.

---

## 14. Hover-probe sample count bug — `N: 0` on visibly-rendered pixels — FIXED

Found while spot-checking points for test 06's `Real² + Imaginary² =
Amplitude²` verification — not from a dedicated test, but a real,
reproducible bug caught along the way.

**Symptom**: hovering a visibly-rendered (colored) scatter point showed
`N: 0` in the hover readout, unchanged when moving around nearby
points.

**Root cause**: `probe_scatter_pixel()` in `msv2_backend.py` computed
the pixel's half-width for counting nearby raw samples as
`(x_coords[-1]-x_coords[0]) / (2*len(x_coords))` — dividing by the bin
*count* rather than the true center-to-center spacing
(`2*(len(x_coords)-1)`). Confirmed numerically: ~5% narrower than
Datashader's actual bin width in a synthetic reproduction, causing a
real rendered (non-NaN) pixel's independently-recomputed sample count
to come up empty. Same exact formula, copy-pasted, was also found in
`probe_raster_pixel()` (raster's hover tooltip Field/Scan/BL
attribution) — fixed there too, though without an equally direct
symptom to have caught it independently.

**Fix**: divide by `2*(len-1)` instead of `2*len` in both functions.
Verified against the real, patched `probe_scatter_pixel` (not a
standalone reimplementation): 13 rendered-but-`N=0` pixels in a
reproduction before the fix, 0 after, across 287 real rendered pixels.

**Also found and fixed in `MSv4Backend`**: checked proactively after
fixing `MSv2Backend` — the identical formula, in both
`probe_raster_pixel` and `probe_scatter_pixel`, same bug. Fixed the
same way, verified the same way (0/287 rendered-but-`N=0` pixels after
the fix, against the real patched function). Both backends now
consistent.

**Status: NOT resolved — the fix was real but insufficient.** Retested
live, at essentially the same point that originally showed the bug:
identical `N: 0` symptom persisted after the fix (file checksums
confirmed correctly loaded). The formula correction is verified correct
for what it addresses — but Datashader's own canvas (what
`probe_scatter_pixel` operates on) is uniformly gridded by
construction, so a pure coordinate-spacing error shouldn't be possible
there the way it plausibly still is for raster's partition-concatenated
grid. That points toward `canvas_agg` and `scatter_df` (the two things
`probe_scatter_pixel` compares) being out of sync with each other —
different underlying data entirely, not a formula problem. Added
targeted diagnostic logging to `_handle_probe` in `visibility_scatter.py`
to capture the dataframe's actual size/range on the next occurrence,
rather than guessing further. Open.

**Third instance of the same underlying pattern** this conversation has
now found: an independently-recomputed boundary/count/order derived
from a coordinate array, instead of reusing the true value already
computed elsewhere, silently diverging near edges or in aggregate. §9
(`field_id` via `enumerate()` over a sorted name list instead of the
real column), §12 (raster partition concat with no `.sortby()`), and
this one (bin half-width recomputed from coordinate spacing instead of
Datashader's own true edges) are all the same shape of bug. Worth
treating "is this boundary/order/count independently re-derived instead
of reused from source" as a standing question for any further backend
code review, not just these three instances.
