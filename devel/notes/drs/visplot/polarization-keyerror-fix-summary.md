# Raster/scatter polarization KeyError — fix summary

**Ticket:** Raster `KeyError` on `.sel(polarization=...)` when opening `3c84scan1.ms`
**Status:** Fixed, smoke-tested against the real MS, three real project test suites passing.

## Root cause

`3c84scan1.ms` splits one nominal spectral window (`EVLA_C#A0C0#0`) across
multiple DDIDs with different correlation products — one partition is
RR‑only, another LL‑only, another RR+LL, another full‑pol. The backend's
`metadata()` collects polarization labels as a single **global union**
across all partitions (`msv2_backend.py: metadata()`, mirrored in
`reduction_context.py: SpwInfo.polarizations`), so
`visibility_plotter.py` picks `first_pol = sorted(pols)[0]` — `'LL'` for
this MS — and passes that one scalar to every partition's
`.sel(polarization=first_pol)`. Partitions that don't locally carry
`'LL'` raise `KeyError`. The identical unguarded pattern existed in four
independent call sites (raster + scatter, msv2 + msv4), not just the one
in the traceback.

## Fix design

A partition that doesn't locally carry the panel's displayed
polarization now contributes nothing to that render — exactly like a
partition outside the requested time/baseline/SPW range — rather than
raising. This was the deliberate choice over "fall back to a different
local polarization," because `VisibilityPlot._flag_key()` already
documents `self._polarization` as a single, panel-wide, truthful value
("the polarisation actually displayed; a raster shows one at a time and
flagging the others would be wrong") — silently substituting a different
polarization per partition would make that value a lie and corrupt flag
write-back.

**Confirmed non-goal violated: nothing becomes unreachable.** Every
polarization a partition actually has can still be selected (e.g. via
the sidebar Correlation control, which drives `first_pol` reactively)
and will render normally — verified directly against the real MS (see
Verification below).

Scope was expanded twice during this ticket, both by request:
1. Probing (`probe_raster_pixel`) — discovered to have the same identity
   inconsistency risk once the render path started skipping partitions.
2. Box-select flagging (`_handle_box_select`) — discovered to never set
   `FlagDelta.correlation` at all, contradicting `_flag_key()`'s own
   documented contract.

## Files changed

| File | Change |
|---|---|
| `msv2_backend.py` | `_raster_2d`: presence guard before `.sel(polarization=...)`, returns `None` on absence (existing "skip partition" contract). `_query_partition_scatter`: filters `yaxes` to locally-present pols before building lazy arrays. `probe_raster_pixel`: new `polarization: Optional[str] = None` parameter; when given, excludes pol-absent partitions from field/scan/antenna/spw identity gathering. |
| `msv4_backend.py` | Same three fixes, mirrored exactly. Additionally: `_query_all_partitions_scatter_fused` (the OPT-B fully-fused scatter path, independently reachable and independently unguarded) gets the same presence filtering — this also fixed a latent bug where its x-axis broadcast template always used `yaxes[0]`'s polarization even when a given partition didn't carry it. |
| `reader.py` | Abstract `probe_raster_pixel` signature + docstring updated with the new `polarization` parameter (default `None`, so this is backward compatible for any implementer that doesn't pass it). |
| `local_visibility_reader.py` | `probe_raster_pixel` delegate threads `polarization` through to the backend. |
| `visibility_raster.py` | `_handle_probe` now calls `probe_raster_pixel(..., polarization=self._polarization)`. |
| `visibility_plotter.py` | `_handle_box_select` now sets `FlagDelta.correlation`: `[self._raster._polarization]` for raster, `[lyr.polarization for lyr in self._scatter._layers if lyr.alpha > 0.0]` (visible layers only) for scatter. Sourced via the existing `self._raster`/`self._scatter` compatibility-shim properties, deliberately matching the same "correct today only because only one of this kind can fire a select" simplification already relied on by `self._raster_x`/`self._scatter_x` in that method — not attempting to solve per-slot select-source identity (that's the pre-existing, separately-tracked "Group 3" work). |

## Verification

**Direct backend smoke test against the real `3c84scan1.ms`** (assembled
a minimal real package tree — actual `msv2_backend.py`/`msv4_backend.py`/
`reader.py`/`selection.py` plus a hand-verified `axes.py` — and ran
`arcae`/`xarray-ms` against the uploaded MS):

1. `query_raster(polarization='LL')` — the exact original crash —
   now returns real finite data (3900 cells) instead of raising `KeyError`.
2. Every polarization the MS actually has (RR, LL, RL, LR) independently
   renders real, non-empty data — confirms nothing became unreachable.
3. `query_columns()` (scatter path) handles a mixed-polarization `yaxes`
   list without crashing, with row counts correctly tracking which
   partitions carry which pols (RR/LL: 582,400 rows each; RL/LR: 83,200
   rows each, matching that only one of the three DDID groups is
   full-pol).
4. `probe_raster_pixel()` works both with the new `polarization=`
   argument and with the old `None` default (back-compat confirmed).

**Real project test suites, run unmodified against the patched files:**

| Suite | Result |
|---|---|
| `test_spw_selection.py` | 34/34 passed |
| `test_visibility_plotter_iteration.py` | 14/14 passed |
| `test_tick_format.py` | 18/19 passed — the one failure is a pre-existing environment gap (see below), not a regression |

**Not completed:** `test_visibility_raster.py` (the comprehensive raster
suite, which exercises the fix through the real `VisibilityRaster`/
`_handle_probe` path rather than the backend directly) was attempted
against the live MS but blocked by cascading missing infrastructure not
present in the project files handed to this chat: `cubevis.bokeh.tools.
_flag_tool.FlagTool` (a compiled-TypeScript Bokeh tool), `cubevis.
toolbox.visplot.visibility_reader.VisibilityReader` (a Protocol), and —
one level further in — `AxisInfo.display_label()` on the real `axes.py`,
which I don't have and had only approximated. I stopped there rather
than keep fabricating unseen parts of the codebase to force a green
suite; the direct-backend smoke test above already exercises the actual
crash and fix at the point it occurred, but this suite's ~90 additional
assertions (image dtype, state-source contents, decimation, colormap
scaling, etc.) were **not** re-verified against this patch. If this
matters before shipping, the fastest path is running it in an
environment that already has the real `axes.py`/`visibility_reader.py`/
`cubevis.bokeh` modules.

`test_visibility_scatter.py` was not attempted at all, for the same
reason — it would need the same missing infrastructure. The scatter
backend logic (`_query_partition_scatter`, `_query_all_partitions_
scatter_fused`) was only verified via the direct backend smoke test
(item 3 above), not through the full `VisibilityScatter` widget layer.

## Known gap: `RemoteReductionContext`

`VisibilityRaster._handle_probe` now calls `self._backend.
probe_raster_pixel(..., polarization=self._polarization)`. `self._backend`
can be a `LocalVisibilityReader` (fixed in this ticket) **or** a
`RemoteReductionContext`, for remote sessions. You mentioned
`RemoteReductionContext` hasn't been written yet — **when it is, its
`probe_raster_pixel` implementation needs to accept this new
`polarization` keyword argument**, or a remote hover probe will raise
`TypeError: unexpected keyword argument 'polarization'`. Worth adding to
whatever tracks that class's implementation checklist.

## Suggested Known Issues table update

| Bug | Status | Root cause | Fix |
|---|---|---|---|
| Raster/scatter `KeyError` on polarization `.sel()` (3c84scan1.ms) | **Fixed** | MSv2/MSv4 backends assumed one global polarization set MS-wide; some MSs split one SPW across DDIDs with different correlation products (RR-only / LL-only / RR+LL / full-pol on this MS). Four independent `.sel(polarization=...)` call sites (raster + scatter, msv2 + msv4) had no per-partition presence check. | Partition lacking the panel's displayed polarization now contributes nothing to that render, matching the existing "outside selection range" skip pattern rather than raising. Extended to `probe_raster_pixel` (polarization-aware identity lookup) and `_handle_box_select` (flags now scope to `FlagDelta.correlation`) for pixel-to-data traceability during flagging. See `polarization-keyerror-fix-summary.md`. |

## Not in scope for this ticket (left as-is, deliberately)

- The "default polarization is alphabetically-first-of-ticked-corrs" UX
  quality issue (e.g. this MS defaults to `'LL'`, which happens to leave
  out one partition by default) — not a correctness bug, just a
  discoverability nuance; flagged during design discussion, not
  requested in scope.
- `_handle_box_select`'s reliance on plotter-level `self._raster_x`/
  `self._scatter_x`/`self._raster`/`self._scatter` instead of true
  per-slot select-source identity — pre-existing, self-documented
  limitation ("becomes Group 3's problem"), deliberately not touched.
