# Chunk 2c Handoff — Completing Remote Execution for VisibilityPlotter

Chunk 2a built raster's remote execution path. Chunk 2b (implicit in
the work so far — not formally named at the time) built scatter's
remote binning/shading redesign and hover-probe piece 1 (raster).
**Chunk 2c** is this handoff's scope: finish scatter to parity with
raster and get the whole `cubevis.remote` branch merge-ready.

---

## 1. Status, itemized

### Raster remote execution — complete
- Full remote query path: confirmed end-to-end against a real ALMA MS
  on `zuul06`.
- Hover-probe piece 1 (static identity tables, local matching): built,
  delivered, **confirmed working via live GUI test** ("Piece 1 works
  as expected").

### Scatter remote execution (binning/shading redesign) — mostly complete
- `MSv2Backend.query_columns`: confirmed against real data (the
  original ~495MB/layer problem this redesign solves — see §3 — was
  diagnosed and fixed against a real MS).
- `MSv4Backend.query_columns`: implemented as a mechanical mirror of
  MSv2's transformation. **Not separately confirmed against real data
  via GUI** as far as this handoff can attest — worth an explicit
  check early in the next session rather than assuming parity holds.
- Wire protocol split (browser-facing `serialize`/`deserialize` vs.
  the P_local↔supervisor↔worker `remote_serialize`/
  `remote_deserialize`): done, tested at the protocol level.

### Hover-probe piece 2 (scatter's coarse identity grid) — delivered, confirmed
- Built, then went through two real post-delivery bugs before landing:
  a missing relay parameter (`probe_grid_max_cells` wasn't threaded
  through all seven places `query_columns` is defined/relayed — see
  §5), and an aspect-ratio bug that let hover report data from
  unrelated, distant regions of the plot (see §5 and §3).
- **Confirmed working** as of the most recent fix
  (`cubevis-probe-hover-precision-fix.zip`): "the worst of the
  tracking problems are fixed."

### Hover-probe piece 3 (scatter click-to-exact) — not started
Fully specified from the original three-piece design discussion, just
not built. See §4 for what it needs to do and why it's the right
complement to piece 2, not a redundant effort.

### Merge readiness (local unaffected, remote optional)
Reasoned confidence based on the shape of the changes (backend
selection via one constructor argument; local path never touches the
`cubevis.remote` subpackage), but **not explicitly re-verified with a
clean local-only regression pass** since the most recent round of
fixes. Recommend this as an explicit checklist item (§6) rather than
asserting it's done.

---

## 2. Is this ready for GUI testing?

Yes, with one thing worth saying plainly rather than glossing over:
Piece 2 needed two rounds of real-GUI-driven bug fixes before it
worked — neither issue was caught by synthetic testing alone. That's
not a reason to hold off further testing; it's the reason testing
matters. Treat this as "ready for its next clean pass," not "should
already be flawless."

Also worth setting expectations correctly: raster's hover is *exact*
(cheap enough to do a real per-hover lookup — see §3 for why). Scatter
piece 2's hover is *coarse by design* and will stay that way even once
working correctly — "scatter at parity with raster" for the probe
specifically means "coarse hover (done) + exact click available on
demand (piece 3, not yet built)," not that scatter's hover will ever
feel identical to raster's.

---

## 3. What and why: the coarse scatter identity grid

This section is meant to be usable on its own if you need to justify
this design to someone else.

### The problem this exists to solve

A scatter plot's X/Y axes (say, UV Distance vs. Amplitude, or Time vs.
Amplitude) are **continuous, derived quantities** computed from raw
visibility data — they are not a fixed grid the way a raster image's
pixels are. A single observation's visibility table has one row per
(time, baseline, channel, polarization) combination; a modest ALMA
scan set easily reaches tens of millions of such rows.

The original (pre-redesign) scatter implementation sent every selected
row to the browser as a raw (x, y) pair, so the client could bin and
shade it locally. Diagnosed directly against a real MS: a default
selection returned **30,913,392 raw rows, ~495MB for a single
layer**. Real sessions typically show two or more layers (e.g. XX and
YY polarizations overlaid) simultaneously, and this cost repeats on
every axis change, selection change, or even a pan/zoom, since the
old design treated those as needing a fresh full dataset. This is
fundamentally unlike raster: a raster image is bounded by
construction (a fixed pixel grid, capped cell count) no matter how
large the underlying data is. Scatter's raw-row approach had no such
bound — cost scaled directly with how much data was selected, with no
ceiling.

### The fix already in place for the display image

The redesign moved binning and shading (Datashader `Canvas.points()`
+ `tf.shade()`) onto the remote worker, so only a small, bounded RGBA
image (plus a few scalar summary numbers) crosses the wire — a few
hundred KB regardless of whether the selection is one thousand or
thirty million rows. This requires one full pass over the selected
data on the remote side (Datashader has to visit every sample once to
compute the per-pixel-bin aggregate), but that pass happens once per
render, not once per row shipped.

**Why this only became a *transfer* problem once remote execution
existed.** Pre-redesign, in local-only mode, the ~495MB DataFrame was
constructed and consumed within a single Python process — `P_local`
did the MS read and the widget's binding directly, so nothing was ever
serialized or sent over a network; the cost was real (memory,
construction time) but private to that one process. Introducing a
remote kernel didn't create new data volume — it exposed the same cost
as a *transfer* problem: the MS read now has to happen on the remote
worker (that's the point of remote execution), so the same ~495MB now
had to be serialized and shipped back across the SSH tunnel before the
client could do anything with it. That's the concrete scenario that
originally hit the wire-protocol call timeout early in this effort.

**Both local and remote sessions use the identical bounded/coarse-grid
code path today — deliberately one implementation, not two.**
`query_columns` (binning, shading, and the coarse identity grid) lives
entirely inside the backend classes (`MSv2Backend`/`MSv4Backend`).
`LocalVisibilityReader` and the remote relay chain
(`RemoteReductionContext`/`remote_registrations.py`) are both thin
pass-throughs to that same method — neither branches on "am I local or
remote." A local session gets the bounded, coarse behavior as a
byproduct of running the same code the remote path runs, not because
it specifically needs it: a local-only design, considered in
isolation, could in principle do an uncoarsened per-hover lookup the
way raster's does, since there's no wire boundary to protect. The
current design accepts a small, real cost for local sessions (a
Datashader summary pass whose bounded-payload benefit only remote
sessions actually need) in exchange for maintaining exactly one
`query_columns` implementation instead of two that could drift apart.
Worth having on hand if the local-mode cost is ever questioned.

### Why hover can't just repeat what raster's hover does

Raster's hover-probe (piece 1) works by a cheap, direct per-hover
lookup: raster's data already lives on a coordinate grid, so "what's
under this pixel" is an O(1) lookup with no aggregation needed. That
was always affordable per mouse-move event.

Scatter has no such direct lookup available. Knowing "what
scans/antennas/frequencies contributed to the data visible at this
screen position" requires knowing, per sample, which screen-space bin
it landed in — which is exactly the same binning operation the
display image itself required. Doing this as a *separate* query on
every hover event would mean a full (or partial) data pass, over a
network round trip to a remote worker, on every mouse movement during
a hover — impractical both for compute/I/O cost (the underlying data
is lazily-loaded via dask/xarray, so a fresh query means real disk/
network I/O, not just CPU) and for interactive responsiveness (hover
fires many times per second).

### The actual design: "coarse but free"

Rather than a separate per-hover query, the coarse identity grid piggy-
backs on the render pass that already has to happen. Datashader's
`summary()` aggregation computes multiple named reductions in a single
pass over the data — so alongside the "mean" reduction already needed
for shading, the render also computes six more reductions (min/max of
time, baseline ID, and frequency) at a **much coarser** resolution
(~3072 cells by default, roughly 64×48, versus a typical 800×600+
display canvas). This costs no additional data access — it's the same
single aggregation pass, just producing a modestly larger output
array (tens of KB, not megabytes).

The coarseness is deliberate, not a shortcut taken for lack of
budget: a grid at full display resolution would need seven float64
values per cell, and at ~480,000 cells (800×600) that's over 26MB per
layer, on every render — defeating the point of having a bounded
wire payload in the first place. At ~3072 cells, the same seven
fields cost roughly 170KB per layer — two orders of magnitude smaller,
negligible even over a constrained remote link.

### What's given up, and why that's an acceptable trade

The trade is precision: each coarse cell can span a meaningfully wide
range of the underlying native coordinates, so a hover reports a
*range* ("scan 10, sometime in this window, roughly these antennas")
rather than an exact match the way raster's hover does. This is why
piece 3 (click-to-exact, not yet built) exists as a complement, not a
redundancy: a deliberate user action (a click) can afford the cost of
a real, targeted, exact per-partition lookup that a hover firing
dozens of times a second cannot.

### The two bugs found so far, briefly, since they're instructive

1. A missing relay parameter (`probe_grid_max_cells`) — `query_columns`
   turned out to have seven separate definitions across the codebase
   (two real backends, an abstract protocol, and four relay/delegate
   layers for local and remote sessions), and only the two real
   backends were updated initially.
2. An aspect-ratio bug in how the coarse grid's own width/height were
   chosen — computed from raw data-value spans (e.g. seconds vs. Jy)
   instead of actual screen-pixel geometry, which produced wildly
   non-square coarse cells (confirmed: a 40x mismatch between a cell's
   width and height in screen pixels for a real Time-vs-Amplitude
   plot). Combined with a "search a little further" hover tolerance
   inherited from the pre-redesign full-resolution design, this let a
   hover report data from a visually unrelated, distant part of the
   plot. Both are fixed; the neighbor-search tolerance was removed
   entirely rather than re-tuned, since it doesn't belong on a
   deliberately coarse grid regardless of aspect ratio — see the
   `cubevis-probe-hover-precision-fix.zip` delivery notes for the full
   diagnosis.

---

## 4. Piece 3 spec (click-to-exact), for whoever builds it

Already fully specified from the original three-piece design
discussion:

- New comm handler, wired to a **click** event, separate from the
  existing hover handler.
- Reuses `probe_scatter_pixel(x_axis, y_axis, polarization, selection,
  x_range, y_range)` — a targeted, per-partition backend method that
  does an exact lookup: compute lazy x/y for the *one* layer clicked,
  build the in-pixel mask, reduce to per-dimension native-coordinate
  masks, harvest identity via the same shared helper
  (`VisibilityPlot._match_identity`) piece 1 and piece 2 both already
  use.
- This is the same design an earlier (pre-redesign) proposal already
  worked out, just demoted from "every hover" to "on click only" once
  the cost of a per-hover backend call became clear.

---

## 5. Lessons worth not re-learning

- **The "every widget gets two views the first time its tab opens"
  Bokeh behavior is expected, not a bug**, given this app's no-server,
  build-everything-upfront architecture (every panel/kind combination
  must exist from construction — there's no server to create them on
  demand later). Don't re-chase this as a timing race if it resurfaces
  elsewhere; the fix is a narrow guard on the one place it actually
  matters (`SelectView._update_value()`), not preventing the duplicate
  view.
- **Console-simulated Bokeh property mutations are not reliable
  stand-ins for a real UI-triggered action.** A `CustomAction`/tool
  click dispatches through Bokeh's own execution machinery
  differently than a script evaluated directly in devtools — cost real
  debugging time twice in this session before being identified.
  Prefer real-click testing (with `SelectView.prototype` instrumented
  to log directly) over inferring behavior from a console script when
  the two disagree.
- **Never derive a screen-space aspect ratio from raw data-value
  spans** when the two axes are different physical quantities — see
  §3's second bug.
- `query_columns`'s signature lives in seven places, not two — any
  future parameter addition needs to be threaded through the abstract
  protocol, both real backends, and all four relay/delegate layers
  (local and remote), or it'll surface as a `TypeError` in whichever
  path wasn't touched.

---

## 6. Concrete next-session checklist

1. Build and test hover-probe piece 3 (click-to-exact) — see §4.
2. Confirm `MSv4Backend`'s scatter path against real data via GUI, if
   not already done independently of this handoff.
3. Explicit local-only regression pass — no remote kernel configured
   at all — to confirm the merge-readiness assumption in §1 rather
   than continuing to reason about it from the shape of the code.
4. Confirm scatter's remote path end-to-end **including hover** on an
   actual remote session. All piece 2 testing so far has been local
   (that's what surfaced the `LocalVisibilityReader` relay bug) — the
   remote relay path (`RemoteReductionContext`/
   `remote_registrations.py`) has the fix applied but hasn't been
   exercised live yet as far as this handoff can confirm.
5. Once 1–4 are clean: this branch should be ready to merge.

---

## 7. File manifest (state as of this handoff)

Files touched during scatter-remote + hover-probe piece 2 work, all
delivered as zips over the course of this conversation — **not
reflected in Project Knowledge**, which has been stale against the
actual deployed tree for this entire effort:

- `cubevis/toolbox/visplot/data/reader.py` — `ScatterLayerSpec`,
  `ScatterLayerRender` (incl. `id_grid_*` fields), `ScatterRenderResult`,
  abstract `query_columns` signature.
- `cubevis/toolbox/visplot/data/_scatter_render.py` — shared
  bin+shade+coarse-grid pipeline (`render_layer`, `_id_grid_size`,
  `compute_canvas_size`).
- `cubevis/toolbox/visplot/data/msv2_backend.py`,
  `msv4_backend.py` — real `query_columns` implementations.
- `cubevis/toolbox/visplot/visibility_reader.py` — abstract protocol.
- `cubevis/toolbox/visplot/local_visibility_reader.py` — local relay.
- `cubevis/toolbox/visplot/remote_reduction_context.py`,
  `remote_registrations.py` — remote relays (P_local side and
  worker side).
- `cubevis/toolbox/visplot/visibility_scatter.py` — client-side
  widget: render-state caching, `_handle_probe` (local hover
  matching), `set_probe_grid_resolution`.
- `cubevis/toolbox/visplot/visibility_plotter.py` — the
  `SelectView._update_value()` guard (unrelated to scatter-remote
  specifically, but landed in this same conversation) and the earlier
  `_layer_dfs`/`_layer_aggs` → `_layer_images` fixes.
- `cubevis/utils/_conversion.py`, `cubevis/utils/__init__.py`,
  `cubevis/remote/_worker_transport.py`,
  `cubevis/remote/_kernel_transport.py` — the browser/remote wire
  serialization split.

Recommend whoever starts the next session pull the actual current
state of these files directly (via zip or a fresh read of the deployed
tree) rather than relying on Project Knowledge.
