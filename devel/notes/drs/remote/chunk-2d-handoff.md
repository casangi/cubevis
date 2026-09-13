# Chunk 2d handoff

Chunk 2c's original scope was piece 3 of the hover-probe redesign
("click-to-exact" for scatter) — that shipped and is confirmed working.
Getting it verified against real data pulled in a much larger pass: the
pre-existing `visplot` pytest suite (backend and widget) turned out to
predate the September "coarse but free" redesign in large part, and
running it for the first time against real MS/PS data surfaced several
independent, real bugs unrelated to piece 3 itself. This document
covers all of it — what shipped, what broke and got fixed along the
way, and what's still open.

## What shipped (piece 3 itself)

- **`InfoTool`** (`cubevis/bokeh/tools/_info_tool.py` + `info_tool.ts`):
  a scatter-only drag tool — click for a point, drag for a box — that
  opens a new browser tab per use (not a reused one; several open tabs
  are meant to be compared side by side) with exact field/scan/antenna/
  SPW identity. Deliberately doesn't extend `FlagTool` or touch
  `DragTool`.
- **`probe_scatter_region()`** (both backends): the actual click-to-exact
  computation — masks the real per-sample x/y for one layer against a
  data-space rectangle, reduces to native-coordinate spans, with a
  per-layer `max_samples` budget and independent per-layer
  `too_many_points` reporting.
- **`_match_identity`'s `bl_ids` parameter** (`visibility_plot.py`):
  exact antenna-pair resolution by discrete matched `baseline_id`
  values, instead of `bl_range`'s contiguous-range scan — needed
  because baseline_id has no structural relationship to a scatter's
  axes, so a click's matched ids are commonly non-contiguous.
  `bl_ids=None` (pieces 1/2's only option) is unchanged, byte for
  byte — confirmed by test.
- **Old dead code removed**: `probe_raster_pixel`/`probe_scatter_pixel`,
  confirmed to have zero live application callers before deletion.

Icon is a generated placeholder (circle-i badge, matching
`flag-data.svg`'s illustrative style once that convention was seen) —
not a final asset.

## Real bugs found and fixed, independent of piece 3

Finding and fixing these needed the full backend/widget test suites
actually passing against real data, which is why they surfaced here
rather than earlier — none of them are piece-3 regressions.

### 1. `_query_partition_scatter`'s serial-path crash (both backends)

The non-fused (`< 500,000` samples — likely the *common* case, not an
edge case) branch built its DataFrame via
`xr.Dataset(...).stack(sample=...).to_dataframe()[keep_cols]`.
`.stack()` promotes any coordinate sharing a name with a stacked
dimension into the resulting MultiIndex rather than a column —
`time`/`baseline_id`/`frequency` always do, since piece 2's coarse
identity grid requests them unconditionally by exactly those names.
`KeyError` on every scatter query under the fused threshold.

Fixed by rebuilding the DataFrame the same way the (unaffected) fused
branch already does — ravel + direct construction, no
`stack()`/`to_dataframe()`. Verified independently on both backends
against real MS/PS data: reproduced the crash first, then confirmed
the fixed serial path produces **byte-identical** output to the fused
path for the same real partition (`pd.testing.assert_frame_equal`),
not just "no longer crashes."

### 2. `BokehInit.get_app_context()` double-registration (`cubevis/bokeh/__init__.py`)

The lazy-create path did:
```python
cls._app_context.append(BokehAppContext())
```
— but `BokehAppContext.__init__` already calls
`BokehInit.set_app_context(self)` as its last step, which itself
appends to the same list. Net effect: the first-ever call registers
the same object twice. `clear_app_context(ctx)` is a plain
`list.remove(ctx)` — removing only one occurrence — so a single clear
call always left one stale reference behind, and the next
`get_app_context()` call kept handing back the same poisoned object
(with, e.g., a stale WebSocket `frontend_id` from a previous session).

Fixed by removing the redundant `.append()` — constructing the context
is enough. Verified by direct simulation against the real source
before and after.

This one only bites when `get_app_context()`'s lazy-create path fires
*before* any `BokehAppContext` is explicitly constructed — real usage
always constructs one via `VisibilityPlotter` first, so this is latent
in normal operation, but real in anything that drives `CommMgr`
directly without a full app context (exactly `test_reconnection.py`'s
own design, and conceivably other future bare-transport tooling).

### 3. `_recomposite()` / `_push_image()` crash on the common case (`visibility_scatter.py`)

`_current_render_range()` deliberately returns `(None, None)` when
there's no active pan/zoom viewport — its own docstring explains this
is meant for callers that immediately make a fresh backend call and
use *that* call's just-refreshed `_x_range`/`_y_range` instead (which
`_rerender()` correctly does). `_recomposite()` — `set_alpha()`'s
no-backend-call fast path — doesn't make that call, and passed the
`None`s straight to `_push_image()`. Crashed on every `set_alpha()`
call made before any pan/zoom, i.e. right after construction — the
common case.

Fixed with the same fallback `_rerender()` already has:
`self._x_range`/`self._y_range` directly, since nothing has changed
since the last real render that would make them stale (no backend call
happened in between — that's the whole point of this fast path).
Reproduced against real MSv2 data first, then confirmed the fix
produces *correct* output (alpha=0 → fully transparent; alpha=1.0 →
real non-transparent pixels), not just non-crashing. This one fixed
10 of `test_visibility_scatter.py`'s 25 original real-data failures by
itself.

### 4. Two `serialize()`/`deserialize()` bugs in test files (not production code)

`test_close_kinds.py` and `test_reconnection.py`'s handshake helpers
built raw `json.dumps()` payloads instead of using
`cubevis.utils._conversion.serialize()`/`deserialize()`. A bare dict
with a top-level `"id"` key collides with Bokeh's own reserved
reference-shape wire syntax (`UnknownReferenceError`). Fixed
consistently across all message traffic in both files (handshake,
ping/pong, request/reply), not just the one line that happened to
reproduce the reported error — same wire format applies to all of it.
Both confirmed via direct round-trip tests against the real codec.

## Test suite modernization

The pre-existing suite predated the "coarse but free" redesign in
several places. Two different situations, handled differently:

**Mechanical migration** (`test_msv2_backend.py`, `test_msv4_backend.py`):
`_query_columns_raw` still exists, explicitly documented as unchanged
since before the redesign, with the exact `(xaxis, yaxes_as_tuples,
selection)` signature and `dict[(Axis,pol), DataFrame]` return shape
these tests already used. Every `.query_columns(` call site swapped to
`._query_columns_raw(` — confirmed no call site depended on any
new-contract-only keyword argument first.

**`TestProbePixel` → `TestIdentityTables` + `TestProbeScatterRegion`**
(both backend files): the old class tested `probe_raster_pixel`/
`probe_scatter_pixel` directly (removed per piece 3). Replaced with
two new classes testing the actual replacements
(`identity_tables()`, `probe_scatter_region()`) against **real MS/PS
data** — cross-checking `probe_scatter_region`'s exact sample count
against an independent `_query_columns_raw` pull, monotonicity on a
narrower rectangle, `bl_ids` mapping to real antenna pairs via
`identity_tables()`, and the `too_many_points` guard. This is new,
real-data coverage of piece 3's own work that didn't exist before
(the synthetic `test_probe_scatter_region.py` from earlier in this
chunk complements it, doesn't replace it).

**`test_raster_coord_stripping.py`**: two hardcoded paths
(`_PKG / request.param`) were missing a `data/` path segment — backends
live at `cubevis/toolbox/visplot/data/`, not directly under `visplot/`.
Mechanical fix, verified against the real backend files.

**`test_visibility_scatter.py`**: heavily built around `_layer_dfs`/
`_layer_aggs`, both permanently `None` post-redesign (you flagged
`_layer_dfs` as vestigial yourself, independently, earlier in this
chunk). Handled per-test based on what was actually being checked:
- Migrated to `_layer_images` (the current populated-after-render
  signal) where the underlying intent was still real and checkable —
  including three `TestDeferredConstruction` tests protecting a real,
  named feature (decision 11, grid/iteration design notes), and
  several tests that were passing *vacuously* (identity checks like
  `x is x` where both sides were always `None`, so the check never
  actually exercised anything) rather than failing outright.
- Retired outright where a currently-passing test already covers the
  same intent more directly (named explicitly in each removal comment).
- Deleted dead lines outright where the crash was in code whose own
  result was never meaningfully used (`... or True`-style asserts).
- Updated one test's expectation where the *architecture* changed on
  purpose and is documented as such (`histogram()`'s bin count is
  always whatever the backend computed, not the caller's request —
  added a companion test that the mismatch warning actually fires).
- Retired one (`test_multi_layer_x_range_is_union`) with no direct
  replacement, flagged explicitly as a real coverage gap rather than
  papered over: per-layer extent isn't cached anywhere on the widget
  post-redesign.
- **Fixed a helper that was silently skipping tests, not passing them**:
  `TestProbe._finite_data_coords()` read `_layer_aggs[0]`, always
  `None`, so its own `pytest.skip(...)` fired unconditionally — several
  tests (`test_probe_envelope_shape` among them) had never actually
  run. Rebuilt from the composite image's own pushed data-space
  origin/extent instead, confirmed against real data. Unblocking it
  surfaced one more real, minor issue: `test_probe_envelope_shape`
  checked a `probe["exact"]` key that was never part of
  `_probe_envelope`'s actual contract — removed, not migrated (nothing
  to migrate it to).

## Known gaps — explicitly deferred, not fixed

- **`TestProbe`/`TestProbeMultiLayer`'s deeper tests** (8, currently
  skipped): `test_probe_empty_region_reports_no_value_for_any_layer`,
  `test_bin_populated_only_in_layer_1_is_found`, and siblings need a
  coordinate-labeled 2D aggregation object per layer to find specific
  "populated in layer A but not B"-style pixels. That data structure
  doesn't exist anywhere client-side any more (that's the whole point
  of the redesign) — rebuilding this needs real design thought about
  what these regression tests should check instead, not a quick swap.
  Worth real attention: the docstrings on these describe a genuine,
  previously-fixed defect (a documented 47.7% false-empty rate) — this
  is protective coverage currently providing zero protection, silently.
- **`test_single_layer_pipeline_under_10s`**: took 24.5–30.4s across
  three runs against real data, failed twice and passed once with no
  code changes in between — genuinely system-load-sensitive, not
  reliably over or under budget. Whether 10s is still a realistic
  budget given the new architecture's extra overhead (coarse identity
  grid, real rendering) versus a real performance regression worth
  profiling is a judgment call, deliberately left untouched.
- **`test_visibility_raster.py`'s module docstring** still lists
  `probe_raster_pixel` as covered test surface — stale reference, not
  causing any failure. Never received this file's actual source in
  this chunk, only that one docstring line quoted back in a test log —
  send it over if it's worth a look.
- **Scatter's remote path**, end to end, including hover, on a live
  kernel/worker session: never exercised in this entire chunk.
  Everything real-data has been local. This was open at the *start*
  of Chunk 2c's own handoff and is still open now.
- **`InfoTool` in an actual browser**: never clicked or dragged for
  real. Only the pure-logic pieces (`_click_window`, `_rect_title`,
  `_probe_region_page`) have real verification. The TS build step for
  `info_tool.ts` has never been confirmed to run.
- **Icon**: still the generated placeholder.

## Suggested Chunk 2d scope

In rough priority order, though this is a suggestion, not a
prescription:

1. Real-browser pass on `InfoTool` (click, drag, popup-vs-tab behavior
   across actual browsers, the TS build step) — the one piece of this
   whole chunk with zero real-environment verification.
2. Scatter's remote path end to end, including hover — carried over
   from Chunk 2c's original handoff, still untouched.
3. Rebuild the `TestProbe`/`TestProbeMultiLayer` multi-layer-defect
   regression coverage properly, given what it's protecting against.
4. Investigate `test_single_layer_pipeline_under_10s`'s budget with
   actual profiling, rather than continuing to eyeball elapsed-time
   prints.
5. MSv4-specific real-data pass — everything real-data in this chunk
   used both MSv2 and MSv4 test files, but hasn't been through a
   dedicated MSv4-focused review the way MSv2 implicitly got via
   `sis14_twhya`.
