# visplot Documentation Handoff

**Date:** August 2026  
**From:** cubevis development session (Claude / Darrell Schiebel)  
**To:** visplot project maintainers

This document describes the two living design documents produced during the
`VisibilityPlotter` development session, explains what each one is for, how
they have been maintained, and what the maintainer needs to know to keep them
accurate going forward.

---

## 1. The two documents

### `visibility_plotter_implementation_plan.md`

**What it is:** The engineering reference for the full `VisibilityPlotter`
application. It contains the architecture (layer diagram, class boundaries,
data model), the capability set (§4.1 through §4.12), the full punch list
organized by phase, and three appendices.

**Who reads it:** Developers picking up new tasks, reviewers assessing scope,
anyone asking "why was X designed this way."

**What it is not:** A delivery log or a user guide. Items are written as
forward-looking design intent, not as post-hoc descriptions of what shipped.
Delivered items are marked ✅ Done with a date; the design rationale stays in
place.

**Structure:**
- §1–§2: Background and requirements (CASR-385, three-layer architecture)
- §3: Architecture (layer diagram, `PanelLayout`, five architecture rules)
- §4: Capability set — one sub-section per major feature group
- §4.12: Astronomer-facing constructor (the public API)
- Phase 0 through Phase 4: punch lists, with the Reference-testing phase and
  Phase 1.5 (export/benchmarking) inserted between Phase 0 and Phase 1
- Appendix A: API stubs
- Appendix B: File inventory
- Appendix C: Items for further research (out-of-scope, tracked so they are
  not lost)

---

### `visibility_plotter_preview.md`

**What it is:** The specification for the initial preview release, updated to
reflect what was actually delivered. It contains the layout architecture,
operating modes, what the preview includes/excludes, the constructor API, and
an implementation appendix recording delivery vs spec.

**Who reads it:** Stakeholders receiving the preview, developers understanding
what the preview was supposed to do vs what it did, anyone filing feedback
(see `visplot_preview_feedback.md`).

**What it is not:** A specification for the full release — that lives in the
implementation plan.

**Key sections:**
- Layout architecture (no-server constraint, dual-container toggle, screen
  real estate)
- §1–§9: What the preview includes, with working/disabled status per feature
- Construction approach: astronomer-facing API and headless export API
- Success criteria: acceptance tests a reviewer can run
- Implementation appendix: what was delivered as specified, what diverged, what
  was added beyond spec, known limitations

---

## 2. How the documents have been maintained

### Conventions

**Status markers in the plan:**
- `✅ Done (Month Year)` — delivered and verified
- `⬜ reference test pending` — code complete, correctness against a reference
  tool not yet checked
- No marker — open, not yet started

**"Open design question" rows** — used where the mechanism is not settled. Do
not schedule these tasks until the question is resolved; the current F-9/F-10
flag-overlay rows are examples.

**Appendix C** — for items that are out of scope but worth tracking. Use this
rather than deleting requirements that "won't happen soon." Each entry has a
"What would make it actionable" field to prevent them becoming permanent stubs.

**The five architecture rules** (§3.1c) — learned from defects that looked
like successes. Add a new rule when a new class of silent failure is
discovered; do not remove existing ones.

### What triggers a plan update

- A punch-list item is delivered → mark ✅ Done, add date, update file
  inventory in Appendix B if new files appeared
- A design decision is made that changes a forward-looking item → rewrite that
  item's description; add rationale if the decision is non-obvious
- A new requirement arrives → add to the appropriate phase punch list, or to
  Appendix C if out of scope; do not add to the capability set (§4) unless the
  requirement changes the *design*, not just the backlog
- A bug is found that reveals an architecture gap → consider adding a rule to
  §3.1c; add a reference-testing item if the fix needs validation against a
  reference tool
- A new "open design question" is surfaced → convert the relevant punch-list
  item to the OPEN DESIGN QUESTION format used by F-9/F-10

### What triggers a preview update

- A feature previously listed as absent is now working → move it from
  "explicitly omits" to the appropriate "what the preview includes" section
- A feature's behavior diverged from spec → update the relevant section and
  add a row to the "spec items modified" appendix table
- A feature was added beyond the original spec → add to the "items added
  beyond spec" appendix table
- A known limitation is resolved → remove from §"known limitations"
- A new known limitation is discovered → add to §"known limitations"

### The PB-series and other "found while doing X" work

Work found and fixed incidentally (PB-1 through PB-9 were all found while
tracing the export or probe path, not while specifically hunting for them) goes
into the plan under the phase it was fixed in, not the phase it was intended
for. The PB table sits in Phase 0 because the fixes touched Phase 0
infrastructure, even though the work happened during the Phase 1.5 period.
Keep this convention — it preserves the history of which infrastructure was
actually stable when work began on each phase.

---

## 3. The feedback document

`visplot_preview_feedback.md` is the lightweight companion for the preview
release. It is not a maintained design document — it is a living issue tracker
for early users. Once the project has a proper issue tracker (GitHub Issues or
JIRA), the feedback document can be retired and issues migrated there.

The feedback document's §3 (missing features) should be kept roughly in sync
with the plan's punch list, but it does not need to be exhaustive — it should
list the items most likely to come up in a feedback conversation, not every
item in the plan.

---

## 4. Things the next maintainer needs to know

### The sis14 test dataset limitation

`sis14_twhya_calibrated_flagged.ms` has **one spectral window, identified by
name only**. This means:
- SPW *selection* is untestable (selecting the one window changes nothing)
- CASA-form `spw=N` integer output is unreachable
- Both have already failed silently under exactly these conditions

A multi-window MS is needed for SPW-related reference testing. This is
documented in Appendix C.12 and in the reference-testing phase section.

### The no-server constraint and its consequences

There is no Bokeh server. The entire layout is serialised to JavaScript once at
`show()` time. After that, Python can update `ColumnDataSource` data and Bokeh
model properties, but cannot add, remove, or rearrange layout nodes.

This has two recurring consequences:
1. Any handler that changes what is drawn must *return* the new data for the
   client to install — assigning `ColumnDataSource.data` in Python succeeds
   silently and does nothing. (Architecture rule 1, §3.1c)
2. Layout changes (panel additions, grid mode) must be pre-built at
   serialisation time; all cells are present in the DOM from the start, with
   visibility toggled by JS. This is why `_PanelSlot` pre-builds both a raster
   and scatter for each slot.

### The two-backends divergence problem

`MSv2Backend` and `MSv4Backend` have diverged repeatedly — fixes have landed
in one and not the other at least four times. The shared `reader.py` module is
the correct home for logic that must be identical in both. `METADATA_KEYS` and
the parameterised backend test pattern exist to catch drift. When adding
backend functionality, always check both backends and add a parameterised test.

### AxisInfo vs Axis.label

`Axis.label` is the enum display name (e.g. "Frequency"). `AxisInfo` records
what was *actually plotted* — the SI-prefixed string, the actual range in the
units used, and the data source. Axis labels in the UI and export must derive
from `AxisInfo`, not `Axis.label`. Mixing them up produces labels that are
technically correct but show the wrong units or wrong range. (Architecture rule
2, §3.1c)

### The restyle body is one unit with its construction site

Anything added to the sidebar or to the chart chrome must also be added to
`_THEME_RESTYLE_JS` in the same commit. A value fixed at construction time will
not follow a later theme toggle unless something explicitly re-pushes it.
(Architecture rule 3, §3.1c) The `_THEME_RESTYLE_JS` body is syntax-checked
under `node` — run that check after every edit to the JS string.

### Open design questions that block scheduling

These punch-list items are marked **OPEN DESIGN QUESTION** and should not be
scheduled until the mechanism is decided:
- **F-9** — flagged-data overlay on `VisibilityRaster` (how to overlay flagged
  bins on a Datashader-rendered image when pixels are not 1:1 with MS rows)
- **F-10** — flagged-data overlay on `VisibilityScatter` (same question;
  interaction with the multi-layer probe noted)

### Reference testing is a separate phase, not a task within a phase

The reference-testing phase sits between Phase 0 and Phase 1 in the plan. It
is the only phase where the question is not "does the UI behave correctly" but
"are the values correct." PB-5 (local cell bounds for non-uniform axes) is the
primary open item there; the Time-vs-Channel raster comparison against msview
is blocked until `antenna=` selection is wired.

---

## 5. Document locations and update cadence

| Document | Suggested location | Update cadence |
|---|---|---|
| `visibility_plotter_implementation_plan.md` | `cubevis/devel/docs/` | Every sprint that delivers, closes, or redesigns a punch-list item |
| `visibility_plotter_preview.md` | `cubevis/devel/docs/` | When the preview feature set changes or a known limitation is resolved |
| `visplot_preview_feedback.md` | `cubevis/devel/docs/` | As feedback is received; retire when migrated to a proper issue tracker |

The implementation plan and preview spec should be committed to the repository
alongside the code they describe, so diffs are traceable. The handoff documents
(this file, `visplot_probe_defect_handoff.md`, `visplot_export_handoff.md`,
etc.) are reference material and do not need to be updated after handoff —
their content has been folded into the plan.

---

## 6. Suggested first steps for the new maintainer

1. Read §3.1c (five architecture rules) — these will save time.
2. Read the reference-testing phase section and note the two open items
   (PB-5, Time-vs-Channel comparison).
3. Check that the test suite passes: `test_visibility_raster.py`,
   `test_visibility_scatter.py`, `test_probe_fix.py`, `test_raster_resample.py`,
   `test_png_export.py`, `test_spw_selection.py`, `test_tick_format.py`.
4. Obtain a multi-window MS for SPW-related testing (Appendix C.12).
5. Wire `antenna=` selection — this unblocks both the Time-vs-Channel reference
   test and the fair msview comparison.
