# Handoff to Part 2: Backend Metadata Plumbing

**From:** Part 1 (design)
**To:** Part 2 (backend data plumbing)
**Companion document:** `visplot-colorize-by-axis-design.md` — read that first
for the *why*; this document is the scoped *what*.

---

## Goal

Extend `query_columns()` in both `msv2_backend.py` and `msv4_backend.py` so
the scatter dataframe carries whichever per-row categorical columns
colorize-by-axis will need — **verified as correct on its own**, independent
of any rendering or UI change. Part 3 should be able to pick this up and
find real, correct data already sitting in the dataframe.

## In scope

Add per-row columns for the axes currently proposed in the design doc §4.1:

- Correlation
- Scan
- SPW
- Antenna1
- Antenna2
- Observation
- Intent

For each axis, confirm:
1. It can be attached per-row to the existing scatter dataframe (same
   dataframe `render_layer()` already receives — one row per raw sample).
2. The value is the same human-readable form used elsewhere in visplot
   (e.g. antenna *names*, not bare integer IDs — matching the convention
   already established in `SelectionSpec`, which is explicit about avoiding
   raw internal indices).
3. It's populated consistently for both `MSv2Backend` and `MSv4Backend` —
   `query_columns()` is documented as implemented identically by both; don't
   let the two drift.

## Follow the existing precedent

`time`/`baseline_id`/`frequency` are already conditionally attached today,
for the hover-probe id-grid (see `_scatter_render.py`'s `id_cols` check).
New columns should follow the **same conditional/tolerant pattern** —
populate when available, and let a caller that doesn't have a given column
still get a valid (non-categorical-colorized) render — rather than making
every column a hard requirement. This avoids breaking existing
configurations mid-transition, exactly as the existing pattern's comments
describe.

## Explicitly NOT in scope for Part 2

- No changes to `_scatter_render.py`'s `render_layer()` coloring logic.
- No `ScatterLayerSpec` / `ScatterLayerRender` dataclass changes.
- No UI changes.
- No categorical palette work.

Part 2 is data plumbing only. Resist the urge to sneak in a quick rendering
experiment even if it's tempting once the columns exist — keep the checkpoint
clean so Part 3 starts from a verified-correct, unopinionated dataset.

## Acceptance criteria

Part 2 is done when, for a representative MS and a known selection:
- Each in-scope column is present in the dataframe returned by
  `query_columns()`.
- Spot-checked values are correct against the same selection's known
  metadata (e.g. the antenna names for a couple of rows match what's
  expected for those baselines).
- Both backends return the same columns/shape for an equivalent selection.

A small standalone test or manual verification script is enough — this
doesn't need to touch the rendering path to be considered complete.

## Questions Part 2 should resolve or flag back to the design doc

- **Real cardinality check:** for a typical MS, what does the actual
  distinct-value count look like for each axis (Correlation, Scan, SPW,
  Antenna1/2, Observation, Intent)? This directly tests the design doc's
  §4.2 cardinality-cap assumption (~20) — report back if any "in scope" axis
  routinely blows past it even in normal use.
- **Any axis that's harder than expected?** If Observation or Intent (or any
  other) needs a nontrivial join/lookup that the others don't, flag it — that
  may be a reason to drop or defer it rather than block the rest of Part 2 on it.
- **Anything about Baseline** worth reporting even though it's out of scope
  per §4.1 — e.g. if it turns out to be nearly free to add alongside
  Antenna1/2 for some structural reason, worth a note for the design doc even
  if it stays excluded from the *user-facing* axis list.

## Handing off from Part 2

When Part 2 wraps, produce `visplot-colorize-by-axis-handoff-part3.md`
covering: which columns landed, any axis dropped/deferred and why, the real
cardinality findings, and anything Part 3 needs to know about how/where the
columns are attached (so Part 3 doesn't have to re-read the backend diff to
find them). Update the design document's §4.1/§4.2/§7 if the findings change
any of those decisions.
