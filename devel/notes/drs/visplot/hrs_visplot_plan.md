# HRS requirements for plotting: expanded, with a visplot gap analysis and plan

Customer: USNO/NRAO High Sensitivity Subarray (HRS project). Source
requirement (CASR-385, "Plotting tool improvements"), verbatim:

- Faster and more reliable plotting tool than plotms
- Waterfall plot
- Ability to plot phase rms vs time and frequency
- Flagging tool similar to AIPS TVFLG, SPFLG, and FTFLG

The source is four lines with no numbers, datasets or acceptance criteria. This
document (1) turns each line into testable requirements, (2) says what visplot
already does and what is missing, (3) proposes a phased plan, and (4) lists the
questions that must be answered by the HRS/HSA side before the estimates below are
trustworthy. Everything about visplot's current state is from the code and GUI
sessions to date; items I could not verify are marked **(verify)**.

## 0. Summary and recommendations

| Requirement | Status today | Main gap | Rough effort* |
|---|---|---|---|
| Faster / more reliable than plotms | Substantial performance machinery exists; **no plotms comparison has ever been measured**; reliability work ongoing | A benchmark harness with agreed datasets and targets; a soak/regression story | M |
| Waterfall plot | A `Waterfall` preset exists (Time x Channel raster, Over/Under layout) | Per-baseline selection/iteration, averaging controls, multi-SPW/IF handling, phase view | M |
| Phase rms vs time and frequency | **Not present** (no phase-rms quantity) | New statistic (circular rms) in both backends; raster + scatter forms; definition agreed with users | M-L |
| Flagging like TVFLG / SPFLG / FTFLG | Scaffolding exists (box-select tool, pending-flag store with undo, overlay, commit interface) **(verify how much is wired end to end)** | AIPS-style workflows, flagging of decimated cells, threshold/"flag above" tools, flag versions, command export, persistence for the target data format | L |

\*S < 1 day, M = 1-3 days, L = 1-2 weeks of focused work in the style of this
project's sessions (design + both backends + tests). These are rough and exclude
waiting on datasets and stakeholder answers.

**Recommendations.**
1. **Benchmark against plotms first.** "Faster than plotms" is the only
   requirement that is fully measurable and we have no numbers. It also gives the
   Z-Score optimization work a target (see `ZSCORE_OPTIMIZATION_HANDOFF.md`).
2. **Do not wait for all feature testing before touching flagging.** See section 5:
   I recommend a flagging *design spike and non-destructive slice* in parallel with
   the phase-rms work, and disk persistence after GUI testing. Reasons are in
   section 5; the short version: flagging is the point of the other three, and
   its requirements constrain decisions we would otherwise make blind
   (decimated cells, cache invalidation, flag semantics).
3. **Get the phase-rms definition agreed before coding.** "Phase rms" has at least
   three reasonable meanings (section 3.3); building the wrong one is the largest
   avoidable rework in this plan.
4. **Obtain a real HSA-sized dataset early.** Everything about "faster" and
   "reliable" is dataset-dependent, and today's tests use one small MS.
5. **Build view save/restore and JSON "plot modes" before the waterfall work** (new
   milestone M1b; design in `VIEW_STATE_DESIGN.md`). Internal users could then
   author and ship view requirements as files instead of waiting for code changes.
   Limits: modes compose only what already exists (per-baseline waterfall
   selection/averaging, phase rms and flagging still need code), and the useful
   version depends on the browser-side restore path, which is the real cost.

## 1. What is unknown (ask HRS/HSA)

Answers change scope, so they gate the estimates.
- Data: format (MS v2? processing set / zarr?), size (antennas, channels/SPWs,
  integration time, total GB), typical selection they plot, where it lives (local
  disk, shared filesystem, remote kernel).
- "Phase rms": over what (channels, time window, baselines), about what (vector
  mean, linear fit / delay-rate removal), wrapped or unwrapped, per baseline or
  per antenna, absolute degrees or noise-normalized.
- "Waterfall": one baseline at a time (as I assume) or averaged; which
  quantities (amp, phase, both); time/channel averaging; multiple SPWs stacked?
- Flagging: which of TVFLG/SPFLG/FTFLG workflows do users actually rely on
  (keyboard-driven? threshold flagging? "flag all antenna X at time T"?), must flags
  be written to the MS `FLAG` column, do they need versioning/undo across
  sessions, and must the operations be reproducible as a command list?
- Comparison targets: what plotms operations are slow or crash for them today
  (concrete cases beat generic targets), and what "faster" means (seconds to first
  image? interactive re-plot?).
- Users/environment: how many concurrent users, remote vs local browser, OS.
- Which plot modes/views they would want to author or receive as files (feeds M1b).
- Deadline/priority order among the four bullets.

## 2. Current visplot capability relevant to HRS

Already built (see the Part 6 handoffs for detail):
- Raster (Time x Baseline, Frequency x Baseline, Time x Frequency/Channel) and scatter
  panels; quantities Amplitude, Phase, Real, Imaginary, Flag fraction, Z-Score.
- Presets: vplot, radplot, Waterfall, Z-Score; side-by-side and over/under layouts;
  linked cursors and shared x ranges; PNG export.
- Two-level rendering (fast local resample, backend re-query when zoomed past the
  aggregation), decimation to a cell budget, byte-budgeted frame cache with request
  coalescing, fused multi-partition reads (MSv4 "OPT-B"), async rendering with a busy
  indicator.
- Selection: Field, SPW, correlation, antenna (with Prev/Next iteration), scan,
  time/UV range strings. Colorize-by-axis (categorical/statistical), per-antenna Z
  readout.
- Two backends (MSv2 and MSv4/processing set) with cross-backend parity tests;
  remote-kernel execution path.
- A save/restore framework (`view_state.py`, `scaling_memory.py`): a registry of
  state units, currently holding per-quantity raster scaling; not yet wired to
  the GUI (see `VIEW_STATE_DESIGN.md`).
- Flag scaffolding **(verify)**: `FlagTool` (box drag; flags only at 1:1 pixel
  resolution), `FlagDB` (pending deltas, undo stack, red overlay through the
  render pipeline, commit on the Flag button), `ReductionContext.commit_flags()` as
  the persistence interface (documented to call `flagdata()` for a casatasks
  context and RADPS/AstroVIPER equivalents for others; a null context raises).

## 3. Requirement-by-requirement plan

### 3.1 Faster and more reliable than plotms

**Make it testable.** Proposed acceptance criteria (numbers to be agreed with HRS):
- *Speed:* for a defined dataset/selection set, median time to first image and to
  re-plot after an axis change is <= 0.5x plotms (cold) and interactive (< 1 s) for
  pan/zoom/recolor (warm). Memory no worse than plotms on the largest dataset.
- *Reliability:* a scripted soak (>= 200 randomized interactions: axis/selection
  changes, presets, iteration, colorize, zoom) completes with zero unhandled
  exceptions, no hangs (watchdog), and stable memory (frame-cache budget honored,
  no growth across repeated plots).

**Work items**
1. Benchmark harness (`bench/`): datasets x operations x {visplot MSv2, visplot
   MSv4, plotms}, cold/warm, median of N, recording wall time, peak RSS, and a
   phase split (read/compute/shade/wire). Include the Z-Score cases. Emit a table
   suitable for the HRS report. Fairness rules (same selection, same averaging,
   plotms defaults documented).
2. Baseline numbers on sis14 now; HSA-sized dataset when available.
3. Reliability: real-MS regression suite in CI (today several real-MS tests only run
   on the developer's machine); a randomized interaction soak driven through the
   plotter's message API; keep the deadlock/race lessons as regression tests
   (frame-cache finalizer, scaling race, force-change-to-None pattern).
4. Failure behavior: every backend error must surface as a message in the UI, never
   a silent blank panel or a hung busy indicator (a give-up timer exists for the
   latter); OOM guards for very large selections (cell budget exists; add a
   pre-flight size estimate and warning).
5. Fix known fragilities: a rare full-suite hang was observed once and traced to
   the cache/finalizer deadlock (fixed); keep watching for others.
6. Performance work items from the Z-Score handoff (subsampled reference, shared
   reference cache) feed this requirement.

**Risks:** no HSA data yet; plotms behavior on huge data may be poor for reasons
unrelated to visplot, making "faster" easy on some cases and unreachable on others
(disk-bound reads).

### 3.2 Waterfall plot

**Today:** the `Waterfall` preset shows a Time x Channel raster of Amplitude in
Over/Under layout, with a scatter below. As I understand the raster rules, a
Time x Frequency raster of a *single* baseline needs an explicit baseline
selection; without one the raster averages over baselines (the screenshot shows
that averaged form).

**Gaps and work**
1. **Per-baseline waterfall with iteration.** A baseline picker with Prev/Next (same
   pattern as the antenna iteration), accepting antenna pairs; status line shows
   "Baseline N/M: A1&A2". (M)
2. **Quantity choice:** Amplitude/Phase/Real/Imag/Flag already exist as raster
   quantities; make Phase waterfall a first-class preset with a cyclic colormap.
3. **Averaging controls:** time and channel averaging factors (with the
   decimation logic accounting for them); important for large channel counts.
4. **Frequency axis:** channel index vs frequency (GHz); multiple SPWs/IFs (the
   channel axis is only well defined for one SPW today, and is relabelled in that
   case), stacked or iterated per SPW.
5. **Flag overlay** on the waterfall (uses the Flag quantity / overlay path), needed
   for the flagging workflow.
6. **Mode variants:** waterfall flavors that only recombine existing behavior
   (quantity, layout, colormap/scaling, averaging once it exists) should be
   authored as modes (M1b) rather than new presets in code.
7. **Acceptance:** for a chosen baseline/SPW on the HSA dataset, the waterfall renders
   within the speed target; iteration through all baselines is fast because of the
   frame cache; values match a direct numpy computation on that baseline.

### 3.3 Phase rms vs time and frequency

**Nothing exists.** The interpretations, in order of what I would build first:
- **A. Rms across channels, per time (plotted vs time)** and **rms across time,
  per channel (plotted vs frequency)**, per baseline. This is what the existing
  raster axis combinations give almost for free once the quantity exists:
  Time x Baseline reduces over frequency; Frequency x Baseline reduces over time.
- **B. Rms across baselines/antennas, per (time, channel) cell**, a Time x Frequency
  map of phase scatter across the array. The current raster rule (Time x
  Frequency requires a single baseline) would need to allow multi-baseline
  reduction for this quantity.
- **C. Local-window rms** (sliding window in time and/or frequency).

**Statistic.** Phase is circular, so a plain standard deviation of wrapped phase is
wrong near +/-180 deg. Proposed default: rms of the wrapped residual about the
vector-mean phase, computed in two cheap passes (mean unit phasor per group, then
mean squared wrapped residual); equivalently a circular standard deviation from
the mean resultant length. Both are streaming reductions (no medians), so this
should be **as fast as Amplitude**, unlike Z-Score. Options to decide with users:
optional linear-trend (delay/rate) removal before the rms; amplitude weighting;
absolute degrees vs normalization by the expected thermal phase noise (1/SNR),
which makes values comparable across baselines with different sensitivity.

**Work items**
1. Define and document the statistic (section 1 answers).
2. `Axis.PHASE_RMS`: axis metadata; a `_raster_2d` branch in both backends
   (custom reduction instead of mean/max); flag-aware; parity test MSv2 == MSv4;
   tests against numpy on synthetic data including wrapped phases straddling
   +/-180 deg and noise-only data.
3. Scatter form (points of rms vs time, and rms vs frequency): the scatter pipeline
   is per-sample, so this needs an aggregated frame (precedent: the Z-Score staging
   and `_finalize_zscore_frame`); simplest is to flatten the raster result into
   (x, baseline, value) rows.
4. Presets/UI: `phaserms-time` and `phaserms-freq` presets; colorbar in degrees;
   a threshold-scaling default consistent with the Z-Score work.
5. Interaction with Z-Score: consider separate amplitude-Z and phase-Z (Z-Score
   backlog item 6), since phase problems and gain problems have different causes.
6. **Acceptance:** results agree with an independent numpy/CASA computation on a
   reference dataset to a stated tolerance; runtime within 1.5x Amplitude.

### 3.4 Flagging like AIPS TVFLG, SPFLG, FTFLG

*(The user plans to add flagging after GUI feature testing; my recommendation on
timing is in section 5.)* My understanding of the AIPS tools, **to be confirmed with
HRS users**: TVFLG is a time x baseline grid display for editing; SPFLG is a
channel x time display for editing one baseline's spectra; FTFLG is a
frequency/time-oriented editor (details to confirm). visplot's existing views
already correspond: Baseline x Time raster (TVFLG-like), Time x Channel waterfall
per baseline (SPFLG-like), Frequency x Baseline / Time x Frequency (FTFLG-like,
pending confirmation).

**What to build (gap list)**
1. **Flag scope semantics.** A raster cell at a decimated zoom stands for many
   samples. Today's tool only flags at 1:1 pixel resolution, which is safe but
   awkward for TVFLG-style editing of a whole baseline-time cell. Define exactly
   what a drawn box means at any zoom (cells x channels x pols), show the number of
   samples that would be flagged before applying, and allow "flag cell across
   all channels" style operations.
2. **Scopes and targets:** displayed polarization vs all polarizations; single
   baseline vs all baselines of an antenna at a time; channel across all times; time
   across all baselines.
3. **Tools driven by statistics:** "flag everything above the cutoff" using
   Z-Score (the n-aware cutoff) or an amplitude threshold, with preview, per-baseline
   or global scope, and undo. This is where Z-Score and phase-rms views pay off.
4. **Undo/redo and versions:** pending-delta undo exists; add flag versions
   (equivalent of CASA flagmanager save/restore) so a session can be rolled back
   after commit.
5. **Reproducibility:** export the applied operations as a flag command list
   (flagdata-style), with a dry-run summary (samples newly flagged, percent of
   data). Important for pipelines and for auditing.
6. **Persistence:** define and implement `commit_flags` per context and data format
   (MSv2 -> `FLAG` column; MSv4/zarr -> equivalent), always operable on a copy for
   testing; concurrency/locking; the Z-Score/frame caches must be invalidated
   (`cache_generation`) after a commit since flags change the reference population.
7. **Workflow polish:** keyboard-driven operation if HRS users need it (AIPS style),
   linked panels updating after each flag, iteration through baselines while
   flagging.
8. **Safety:** confirmation for destructive commits, never write during tests
   except on scratch copies.
9. **Acceptance:** scripted flagging operations produce exactly the expected flag
   array (compared to an independent computation), survive undo/redo and
   versioning, and the resulting MS is readable by CASA/AIPS-side tools.

## 4. Phased roadmap

| # | Milestone | Depends on | Effort | Exit criteria |
|---|---|---|---|---|
| M0 | Requirements clarification + datasets | - | S (mostly waiting) | Section 1 answered; an HSA-sized dataset available |
| M1 | Benchmark harness + baseline numbers vs plotms | M0 (partly) | M | Table of speed/memory results; targets agreed |
| M1b | View save/restore + JSON plot modes (`VIEW_STATE_DESIGN.md` V2-V4): server-held units, browser-side restore path, mode loader with validation, the four presets converted to modes | M0 (mode wishlist) | M-L | A mode file reproduces each existing preset exactly (parity test); internal users can author, save and load a mode without code changes |
| M2 | Waterfall completion (3.2) | M1b (framework); per-baseline selection/averaging are code, not modes | M | Per-baseline iteration, averaging, phase waterfall, tests; composable variants authored as modes |
| M3 | Phase rms (3.3) | M0 (definition) | M-L | Raster + scatter, both backends, parity + numpy tests |
| M4 | Flagging design spike + non-destructive slice (section 5) | M2 | M | Written flag-semantics spec; overlay/undo/threshold flag on decimated cells, in memory |
| M5 | Flagging persistence + AIPS-style workflows (3.4) | M4, M0 | L | Commit to scratch copies, versions, command export |
| M6 | Hardening and release | all | M | Soak test, real-MS CI, user guide, performance targets met |

Z-Score optimization (`ZSCORE_OPTIMIZATION_HANDOFF.md`) slots in after M1 (it
needs the same benchmark data) and can run alongside M2-M3.

Suggested order rationale: M1 first (cheap, decision-relevant); then M1b, because
every later view requirement gets cheaper once modes can be authored as files and
because it proves the framework on real use before anything depends on it; then M2
and M3 (what users look at while flagging); M4 starts as soon as M2's waterfall
exists. Flagging (M4/M5) stays code: modes can describe the *views* a flagging
workflow uses, not the flagging operations.

## 5. When to add flagging

I recommend starting it **earlier than "after all feature testing"**, in two
stages:
- **Stage 1 (early, in parallel with M3): design and a non-destructive slice.**
  Reasons: (a) it is the actual purpose of the other three bullets, so feedback from
  HRS users will be far more informative once a view-to-flag loop exists, even
  in memory; (b) its requirements constrain earlier decisions: how decimated
  cells map to samples, how caches are invalidated, whether Z-Score references
  must be recomputed after flagging; discovering these late means rework in the
  raster/backends; (c) much of the scaffolding already exists, so the risk is
  design, not volume; (d) reproducibility (command export) is easier to design in
  than to retrofit.
- **Stage 2 (after GUI feature testing): disk persistence.** Keeps the destructive
  part behind evidence that the views and semantics are right, as you planned.
  Until then flags exist only as pending deltas and overlays, and nothing is
  written.

## 6. Cross-cutting

- **Documentation:** a user guide with the HRS workflows (spot with Z-Score /
  phase-rms, confirm on waterfall, flag), and architecture notes for maintainers
  (the hazards list in the Z-Score handoff is a starting point).
- **Testing:** keep the synthetic suite fast; add real-MS CI; mutation-check new
  tests; drive `update_axes` the way the plotter does.
- **Compatibility:** MSv2 and MSv4 parity on every new quantity; remote-kernel path
  tested for anything that adds array attributes or new backend methods.
- **Parity with plotms features HRS actually uses:** collect from users; do not
  assume.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Wrong phase-rms definition | Agree the statistic (3.3) before coding; ship the general reduction machinery so variants are cheap |
| No large dataset; performance claims unproven | M0/M1 first; synthetic benchmarks are only indicative |
| Flag persistence bugs corrupt data | Scratch copies only in testing; commit dry-run; versions/undo before enabling on originals |
| Decimated-cell flagging ambiguity | Explicit semantics + sample-count preview (section 3.4 item 1) |
| Real-MS tests not in CI | Add real-MS CI (M1/M6) |
| M1b delays the first visible HRS feature by roughly the cost of the browser-side restore path | Do the small server-held units first (quick wins), time-box the browser-side path, keep M2's code-only items (per-baseline selection, averaging) independent of it so they can start in parallel |
| Mode files become a public contract once internal users author them | Freeze unit keys/schema before authoring starts; saved-file fixtures per released schema; `requires` capability check so a mode fails cleanly on a build lacking a feature |
| Users author invalid or dataset-specific modes | Validation with clear messages; templates ("automatic", rules) instead of hardcoded antennas/SPWs (`VIEW_STATE_DESIGN.md` 7A) |
| Requirements grow after first demo | Written acceptance criteria per requirement, agreed up front |

## 8. Immediate next steps

1. Send section 1 to the HRS contact.
2. Time the two rasters (Amplitude vs Z-Score) on the real MS and record plotms
   equivalents (M1 starter; see the Z-Score handoff section 3).
3. Ask the HRS contact which plot modes they would want to author or receive (feeds
   M1b's scope), and decide whether to start M1b's small server-held units now.
4. Decide whether M2's code-only items (per-baseline selection, averaging) start in
   parallel with M1b; they do not depend on it.
