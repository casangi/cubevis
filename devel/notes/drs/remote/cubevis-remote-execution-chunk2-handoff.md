# Chunk 2 handoff — `visplot` remote data path (MSv2/MSv4 reading + Datashader rendering as a remote object)

**Start here, not from prior chat history.** This document is
self-contained: it names the exact files and call sites to touch, what's
already confirmed by reading the actual code, and what's still open.
Read alongside `cubevis-remote-execution-design.md` (§2f, for *why* the
execution-context/object model is shaped the way it is) and
`cubevis-remote-execution-implementation.md` (for Chunk 1c's exact wire
schemas — `create_context`, `create_object`, `call_method`,
`dispatch_async`/`job_status`). `cubevis-remote-execution-developer-guide.md`
covers the framework's general usage patterns and pitfalls; this document
assumes that guide and layers `visplot`-specific detail on top.

**Status: designed, not yet implemented.** Nothing in this chunk's own
code exists yet — `_make_remote_context()` (below) still raises
`NotImplementedError`. This is intentionally the *next* chunk to
implement: it's the smallest real validation of Chunk 1c's stack end to
end (raster needs no new wire contract at all), and `visplot` is under
active development, so remote execution is valuable to its own developers
now — unlike `iclean` (Chunk 3), already released, lower near-term
appetite for change.

---

## 1. Where this plugs in — the exact integration point

`visibility_plotter.py` already has the factory dispatch this chunk fills
in. `open_ms`/`open_ps` resolve a backend via `_resolve_context_msv2`/
`_resolve_context_msv4`, both of which already have a `backend='remote'`
branch requiring `remote_endpoint`:

```python
def _make_remote_context(path: str, endpoint: str) -> ReductionContext:
    raise NotImplementedError(
        f"RemoteReductionContext is not yet implemented (preview release). "
        f"endpoint={endpoint!r} path={path!r}"
    )
```

This is the literal function this chunk replaces. `remote_endpoint`'s
shape isn't pinned down elsewhere in the codebase yet — deciding what it
is (a kernel name string for `sshpyk`? a `persistent_file`/manifest
label? a small config dict?) is part of this chunk's own scope, not
something already decided for you. The rest of `open_ms`/`open_ps` and
every backend-selection branch around it (`casa6`, `radps`, `null`)
needs no changes — this chunk only has to make the one `remote` branch
real.

## 2. What `RemoteReductionContext` must implement

`VisibilityReader` (`visibility_reader.py`) is a `@runtime_checkable
Protocol` with exactly four methods — `query_raster`, `query_columns`,
`probe_raster_pixel`, `probe_scatter_pixel` — confirmed by reading the
protocol definition directly, not inferred. `VisibilityPlot` never
touches the MS itself; it only calls through `self._backend`, typed as
`VisibilityReader` (`visibility_plot.py:85,266`), so nothing about
`VisibilityPlot`/`VisibilityPlotter` needs restructuring — they already
treat the reader as swappable.

Two more methods are needed beyond the formal protocol, evidenced by
`LocalVisibilityReader` (`local_visibility_reader.py`), and
`RemoteReductionContext` must implement both:

- **`metadata()`** — used by `open_ms`/`open_ps` to build
  `ObservationMetadata`.
- **`axis_info()`** — used for axis labeling. Its own comment warns that
  a missing implementation produces "correct-looking output, wrong
  label" — silent, not loud, so don't defer this one as an
  afterthought.

`reduction_context.py`'s own docstring already names
`RemoteReductionContext` as satisfying **both** `ReductionContext` and
`VisibilityReader` at once — read that docstring before starting; it's
the intended shape, not something to redesign from scratch.

**All six methods are `SyncBridge`-wrapped `call_method` calls against a
backend object living in a dedicated execution context** — created once
via `create_object` (not per call, and not sharing a process with
anything else's work), matching `LocalVisibilityReader`'s own calling
convention exactly so `VisibilityPlot`/`VisibilityPlotter` never need to
know which implementation they're talking to. This is a corrected
understanding from an earlier draft of this design: a naive `async def`
implementation would have quietly broken the "reader is swappable"
property, since `VisibilityReader`'s four protocol methods are plain
synchronous calls, not coroutines — wrap with `SyncBridge.run(...)`, per
the developer guide's §6, rather than making the protocol itself async.

**One exception:** `ReductionContext.submit()` does not fit this
"straightforward `call_method`" pattern — its `Future`-bridge mechanism
is genuinely undesigned, not merely unimplemented. If `visplot`'s actual
usage needs `submit()` for this chunk (check current call sites before
assuming it's in scope), that's real, first-time design work, not a
mechanical application of the pattern above.

## 3. Raster (2a) — no wire-contract change needed

**Confirmed by reading the actual implementation, not assumed:**
`MSv2Backend._raster_2d` (`msv2_backend.py:1233-1310`) builds its
quantity array via dask-array primitives (`da.absolute`, `da.angle`) and
reduces over non-display dimensions while the array stays lazy;
`_decimate_agg` strides that still-lazy graph *before* any
materialization; the single `.compute()` call
(`msv2_backend.py:1134`) happens *after* striding, so Dask only ever
reads the strided cells regardless of the true dataset size. Output is
capped at `max_cells` (default 2,000,000 cells, "≈16 MB at float64" per
`reader.py:738`'s own docstring) — a fixed bound independent of dataset
size.

This means `query_raster()` is already safe to call remotely more or
less as-is: `RemoteReductionContext.query_raster()` can be close to a
mechanical relay — a `call_method` whose result is exactly what
`LocalVisibilityReader.query_raster()` already returns, marshaled
through Chunk 1c's existing serialization (`cubevis.utils.serialize`/
`deserialize`, the same one Bokeh itself uses — already verified against
a real numpy array round trip in Chunk 1c's own tests, not just
scalars). **The one tuning worth doing:** pass a smaller `max_cells` by
default when dispatching remotely than the local 2M default, trading
resolution for bandwidth — the interface already exposes this parameter
for exactly this purpose, no new mechanism needed.

**Do not route local recompositing through the wire.**
`VisibilityRaster._shade_viewport()` (`visibility_raster.py:1380-1409`)
operates purely on the cached `self._agg` with zero backend calls — pan/
zoom within the cached resolution, color-mode toggles, and scaling
changes all resolve locally today, for free. An earlier draft of this
design considered shipping a fully-rendered image over the wire on every
render call and rejected it: that would turn every currently-free pan/
zoom drag into a network round trip. The wire boundary is `query_raster()`
itself (already correctly bounded, above) — nothing about the render
path changes.

**Suggested first milestone:** get raster working end to end against a
real (or local-standin) MS before touching scatter at all. It validates
Chunk 1c's create_context → create_object → call_method path with a
contract that's already correct, with no new design work required — the
cleanest possible smoke test for the whole stack.

## 4. Scatter (2b) — the real gap, and it's a correctness fix, not just a bandwidth one

**This is the one place in Chunk 2 with actual unsolved design work, and
it matters independent of remote execution at all.**
`MSv2Backend.query_columns`'s "adaptive pipeline"
(`msv2_backend.py:843-846` — serial stack under 500K samples, fused
`dask.compute()`+numpy ravel from 500K–5M, parallel Datashader-ready
DataFrames above 5M) materializes every matching row at every tier — the
tiers change *how* it computes, never *whether* the full match set gets
pulled into memory. A broad scatter selection against a terabyte-scale
MSv4 could try to materialize the entire matched slice — **this is
already a memory-safety problem in today's local, non-remote code**, not
something remote execution introduces.

**The fix has a documented blueprint already in the codebase, just not
implemented.** `reader.py`'s abstract `query_columns` docstring (lines
651, 669–670 — predates the concrete implementation's deviation from it)
already specifies the correct shape: Datashader should consume the
Dataset directly via `Canvas.points()`, with no pre-averaging, and
`.compute()` called only inside Datashader itself, never materializing
the full array in Python first. `Canvas.points()` genuinely accepts a
Dask-backed input and performs the pixel-binning via Dask's own chunked
reduction, without fully materializing the input — this is standard
Dask/Datashader territory, not a novel mechanism to invent.

**No `max_cells`-style cap is needed here, unlike raster**, and this is
worth understanding precisely rather than copying raster's pattern by
habit: raster needs its stride because it has an intermediate reduction
stage (averaging over non-display dimensions into a 2D grid) that can be
enormous *before* canvas-resolution binning ever happens. Scatter has no
such intermediate — each sample maps directly to one `(x, y)` point, and
`Canvas.points()`'s binning *is* the reduction, not a second pass over an
already-reduced grid. Its output is always exactly
`canvas_width × canvas_height`, regardless of whether the selection
matches ten rows or ten billion — the wire payload is bounded
automatically, with no separate decimation/`is_decimated` concept to
design.

**On data fidelity — precise and worth preserving exactly:** this fix
drops no matching points from the result. Every sample within the
selection still contributes to whichever pixel-bin it lands in; the
bin's aggregate is genuine, not a subset. What changes is *where* the
binning happens (near the data, before the network hop) rather than
*where* it happens today (client-side, in
`VisibilityScatter._shade_all_layers`, against `self._layer_dfs` cached
from a `query_columns()` call that already shipped every raw row across
whatever transport is in use). This is explicitly **not** the same kind
of trade-off as raster's `max_cells` stride — that one is real, visible
decimation with a defined recovery path (`is_decimated` + re-query at
higher resolution on zoom-in); nothing here needs an analogous recovery
path because nothing is being dropped.

**This fix is also the enabler for genuine multi-node cluster
execution** of the aggregation itself, independent of the bandwidth
concern that motivates it here: a lazy Dask graph is exactly what a
`dask.distributed` scheduler can spread across multiple worker nodes,
each partition read and reduced in parallel, results combined into the
one bounded output. Today's eager `.compute()`-into-one-process's-pandas
implementation caps out at one process's memory regardless of how many
nodes sit behind it, so this fix is a prerequisite for that goal, not
merely compatible with it.

## 5. Open questions to resolve during implementation (not yet answered by this or the design doc)

- **`Canvas.points()` wants a Dask DataFrame, not an `xr.Dataset`
  directly** — some conversion (likely `.to_dask_dataframe()`) is the
  probable missing glue between what the backend currently produces and
  what Datashader consumes lazily. This is standard, well-supported
  Dask/xarray territory in general, but has **not been verified against
  this codebase's actual partition/backend code**
  (`_iter_visibility_partitions`, `_apply_selection`, etc., in
  `msv2_backend.py`) — it may not drop in cleanly. Check this early;
  it's the first real risk in implementing §4.
- **Whether/how this generalizes to `MSv4Backend`** — not reviewed as
  part of this design; `msv4_backend.py` exists in the source tree but
  its query_columns-equivalent path hasn't been read. Don't assume
  `MSv2Backend`'s fix transfers mechanically.
- **Local recompositing consistency for scatter.**
  `_shade_all_layers` currently rebins from cached raw rows on every
  viewport change. Once the remote path returns a bounded aggregate
  instead of raw rows, does the *local* path's probe logic (`_agg_pixel`,
  which indexes into `self._layer_aggs`, themselves derived from
  raw-row rebinning) need adjustment so local and remote sessions behave
  consistently? Not analyzed — worth resolving before shipping both
  paths side by side.
- **Which execution-context configuration `visplot`'s worker
  registration function should build** — what gets `create_object`'d
  eagerly at context-creation time (the opened MS/Processing Set itself,
  presumably) versus lazily on first use. Not worked through against
  this chunk's specific method set yet; write the `register_function`
  (per the developer guide's §3) with this question explicitly in mind.
- **`ReductionContext.submit()`'s `Future`-bridge** — still entirely
  undesigned (§2 above); resolve only if `visplot`'s actual current usage
  requires it for this chunk, rather than building it speculatively.

## 6. Suggested implementation order

1. Decide `remote_endpoint`'s shape (§1) — this blocks everything else.
2. Build the `visplot` worker registration function and get a backend
   object constructible via `create_object` in a dedicated execution
   context (developer guide §3).
3. Implement `RemoteReductionContext.metadata()`/`axis_info()` first —
   smallest surface, needed before anything else can open successfully.
4. Implement raster (§3) — mechanical, validates the whole stack,
   requires no new design.
5. Implement scatter (§4) — the real design work; resolve the
   `Canvas.points()`/Dask DataFrame question (§5) early, since it's the
   biggest unverified risk.
6. Only then, if needed: `submit()`'s `Future`-bridge.

## 7. Definition of done, suggested

- `RemoteReductionContext` satisfies `isinstance(obj, VisibilityReader)`
  (the `@runtime_checkable` protocol check) and implements `metadata()`/
  `axis_info()` alongside it.
- `open_ms(path, backend='remote', remote_endpoint=...)` returns a
  working reader/context pair, confirmed against a real MS (or
  Processing Set), not a mock.
- Raster: a real `query_raster()` round trip through a live execution
  context, output verified equal (or acceptably close, if a smaller
  remote `max_cells` is used) to the equivalent local call.
- Scatter: a real `query_columns()` round trip returning a bounded
  aggregate for a selection sized well beyond what today's local
  eager-materialization code could safely handle in one process — this
  is the test that actually proves the fix, not just that the new code
  path runs.
- Local recompositing (`_shade_viewport`, `_shade_all_layers`'s
  non-remote portions) confirmed unchanged and still free of backend
  calls.
- Whatever remains genuinely unresolved (per §5) stated plainly in an
  updated status document, not silently dropped.
