# Remote raster and scatter viewport responsiveness — assessment

**Status:** research/assessment, not a design decision. Written 2026-09,
after Chunk 2d's remote-path validation, in response to a direct question
about what (if anything) should change to keep panning/zooming responsive
once raster and scatter are actually driven remotely. Every claim about
current behavior below is sourced from reading the real code (file/line
references given), not from memory or the design documents' original
plans — several of those plans turned out not to match what shipped (see
the implementation doc's Chunk 2 section for the specific correction).
Where something is a judgment call rather than a fact, it's marked as
such.

---

## 1. Summary

Raster and scatter handle pan/zoom completely differently today, and the
difference isn't cosmetic: raster keeps almost all interaction local and
free; scatter re-executes its full pipeline, over the wire, on every
single viewport change. Locally this is a real but bounded cost (roughly
2–4 seconds per re-render on real data, measured — §3a). Remotely, that
same cost gets network round-trip latency, and potentially real
storage-network latency, added on top of it, every time — and nothing
today distinguishes "the user is dragging the plot" from "the user asked
for one new render," so every intermediate frame of a drag would, as
built today, be its own full round trip.

This isn't a hypothetical: the redesign that made scatter's server-side
binning correct (Chunk 2b/2c) already noticed this cost and already named
three mitigations — debouncing, a stale-while-revalidate placeholder, and
an overscan margin — in its own source comments, without implementing any
of them. This document picks that thread back up with the added context
of what a *remote* session specifically changes, and lays out options
rather than prescribing one, since the right choice depends on real
measurements this document doesn't have (§7).

**If one change had to be picked first:** debouncing the wire call until
a drag/zoom gesture settles is the smallest, lowest-risk change, benefits
raster and scatter equally, and doesn't require deciding anything about
overscan or caching architecture first. See §6 for why it's ranked above
the others.

---

## 2. Current state, verified against the real code

### 2a. Raster: two-level, mostly free

`VisibilityRaster._do_viewport_rerender` (`visibility_raster.py`) checks,
on every pan/zoom, whether the cached aggregation (`self._agg`, from the
last real `query_raster()` call) still has enough resolution for the new
viewport:

```python
needs_requery = (
    self._is_decimated
    and (x1 - x0) / self._width  < agg_cell_w
    and (y1 - y0) / self._height < agg_cell_h
)
```

- **If not** (the common case): `_shade_viewport` crops and re-shades
  the *cached* agg at the new extent — a local Datashader re-shade, no
  backend call, no network round trip. This covers all panning and any
  zoom-out, and any zoom-in that doesn't ask for more detail than the
  cache already has.
- **If so** (only possible when the original query was decimated —
  `self._is_decimated`, meaning the true dataset didn't fit under
  `max_cells` at the original extent — and only when zooming in far
  enough to need finer resolution than the cache holds): exactly one
  re-query, at `max_cells * 4`, for the *new, smaller* viewport extent
  (`_viewport_selection`, `visibility_plot.py:1131`). After that
  re-query, `self._current_viewport` resets to `None` and the newly
  cached (now higher-resolution, for that region) agg becomes the new
  baseline for further free local panning.

**Important nuance, confirmed by reading `_viewport_selection`
directly:** the re-query fetches *exactly* the new viewport, with no
padding/margin. So a raster that just re-queried can still immediately
need a second re-query if the user pans right past the edge of what was
just fetched — "mostly free" is not "always free," it's "free unless
you're actively exploring past the edge of your last fetch's resolution
envelope." True overscan (fetching a deliberately wider-than-viewport
region so a pan has somewhere to go before hitting cache edge) is **not**
implemented for raster today either — worth stating plainly since it
would be easy to assume from "raster is fine" that this was already
solved.

If the original query was **not** decimated (the whole dataset fit under
`max_cells`), `needs_requery` is always `False` — the cached agg already
has full fidelity everywhere, and pan/zoom is unconditionally free no
matter how far you zoom in.

### 2b. Scatter: zero-level, always a round trip

`VisibilityScatter._do_viewport_rerender`/`_rerender` (`visibility_scatter.py`)
has no equivalent tier. Its own docstring says so directly:

> POST-2026-09: no longer a local recomposite of a cached DataFrame —
> binning and shading both happen backend-side now... so every pan/zoom
> now costs a full `query_columns()` round trip. This applies to LOCAL
> sessions too, not just remote ones: the backend re-reads the selected
> data from disk on every call... A known, deliberate cost for this
> pass — see the scatter remote-execution design notes' discussion of
> debouncing / a stale-while-revalidate placeholder / an overscan margin
> as possible later mitigations, none implemented yet.

**A document this comment points to — "the scatter remote-execution
design notes" — is referenced twice in the actual source
(`visibility_scatter.py` and `data/msv2_backend.py`).** Not found in
project knowledge when this assessment was first written; since then,
searched directly against the actual repository
(`github.com/casangi/cubevis`, `devel/docs/` and `devel/notes/drs/`
in full, including the one file whose content plausibly overlapped —
`visplot-grid-iteration-notes.md`, which turned out to be about grid/
panel-sync debouncing, a different topic) and genuinely not found
anywhere in it. Two later documents did surface separately
(`cubevis-remote-execution-chunk2-handoff.md` and
`chunk2-completion-scatter-handoff.md`, folded into this assessment and
the implementation doc below) — neither is the referenced "design
notes" file specifically, and neither was in the repository search
either. Best current read: that document either was never committed, or
never existed under that name — treat this assessment as the first
real word on the subject, not a second one, until/unless it turns up.

The only viewport-adjacent operation that *is* free today: `set_alpha()`
(toggling a layer's visibility), via `_recomposite()`, which reuses the
last render's cached per-layer images (`self._layer_images` — see the
Chunk 2d test rebuild that relies on exactly this being real and
per-layer). Pan, zoom, axis changes, `color_mode`/scaling/colormap
changes — everything else — goes through `_rerender()`, unconditionally.

**The colormap/scaling case specifically was already flagged, before
this assessment, as its own named concern, not folded into "pan/zoom" by
this document for the first time.** The handoff that corrected
`query_columns()` to shade server-side, not just bin server-side (see
the implementation doc's Chunk 2b correction) named it explicitly at the
time: before that correction, recoloring a scatter plot was free (local,
mirroring raster); after it, every colormap or scaling adjustment costs
a full round trip against the real ~600-800ms floor (§3a below), the
same underlying cause as pan/zoom's cost but a distinct interaction a
user triggers separately and often more frequently while exploring
data — worth keeping as its own line item when deciding what to fix,
not assuming a pan/zoom fix automatically covers it. None of this
document's own §5 options distinguish the two triggers, but a debounce
interval or a render cache tuned for pan/zoom's typical gesture shape
may not be the
right tuning for a colormap dropdown's click-and-immediately-see-result
expectation — worth designing for explicitly rather than assuming
shared code means shared UX.

### 2c. Why the difference exists, and why it isn't just an oversight

Raster's cache-and-crop trick works because its output is a **numeric
grid** — cropping a wider grid to a smaller extent and re-shading is a
well-defined, cheap operation on that grid, independent of how the grid
was produced. Scatter's redesign moved binning and shading **behind**
the wire boundary specifically to fix a real memory-safety problem (an
unbounded row-materialization risk on a large, unrestricted selection —
see the implementation doc's Chunk 2b correction) — the result crossing
the wire is now a rendered RGBA image, not a numeric grid or a DataFrame.
An already-rendered image can be cropped (to zoom out / pan within it)
but **cannot be validly re-binned at a different data range** — there's
no way to "zoom into" part of a finished image and get a correctly-binned
result for the new, smaller extent; that requires re-running Datashader
against the underlying samples for that extent specifically. This is the
structural reason scatter's fix, correct on its own terms, removed the
option raster still has — not an implementation gap in the ordinary
sense.

### 2d. The coarse identity grid is a relevant, existing precedent

Chunk 2c's hover-probe redesign already established a working pattern
directly relevant here: `query_columns` computes, in the *same* backend
call as the display image, a second, much coarser grid
(`self._layer_id_grid`, ~64×48 cells by default,
`probe_grid_max_cells`) of native-coordinate ranges per bin — cheap to
compute alongside the main render, small on the wire (tens of KB), and
sufficient to answer "roughly what's near this point" without a new
round trip. That's a real, shipped example of "compute something a bit
more than what's strictly needed for *this* frame, once, so a class of
follow-up interactions can be answered locally" — the same shape of idea
as an overscan margin for panning, already proven out for a different
purpose in this exact codebase. Worth citing to whoever designs a
panning cache, as an existing pattern to extend rather than a foreign
concept to introduce.

---

## 3. What the three named factors change

### 3a. Datashader compute cost

Measured directly during Chunk 2d (`cProfile` against a real, ~1.9M
sample local scatter render, single CPU core, no `numba` installed):
roughly **2.0s in `dask.compute()`** (chunk reads off disk/zarr) and
roughly **1.9s in `_scatter_render.render_layer`** (the actual Datashader
aggregation), for one layer, one render. Two important caveats on this
number: it was measured cold-ish on a single-core sandbox with no
`numba` acceleration, and a separate cold-vs-warm timing investigation in
this same chunk found a large, unexplained first-touch slowdown pattern
(cold run ~11s, every subsequent warm run 2.5–4s, mechanism not pinned
down) — so treat "2–4s" as a real, observed order of magnitude on modest
hardware, not a tight, guaranteed bound. On the real host `visplot`
actually runs against (however many cores, whatever numba/threading
setup, whatever the true dataset size is), this could be meaningfully
faster or meaningfully slower — not measured here.

The relevant point for this document isn't the exact number, it's that
this cost is **paid on every single pan/zoom under the current design**,
because nothing caches or reuses it. Locally, that's a real but bounded
per-interaction cost. Remotely, it compounds with §3b and with plain
network round-trip time — **once a worker/session already exists**
(distinct from first-connect cost, corrected in the developer guide's
own §5 after a real bug there was found and fixed: single-digit to
low-double-digit seconds now, not the minutes an earlier version of
that section reported), steady-state per-call latency has a real,
separately measured floor: **roughly 600-800ms** (dispatch/round-trip/
backend-compute), from a real benchmark sweep during raster's own
remote-completion work, essentially independent of payload size within
the ranges tested (see the implementation doc's raster-completion
section). That floor doesn't go away just because scatter's redesigned
payload is small and bounded — it makes each render *bounded*, not
*fast*. This document's §3a compute-cost estimate is **on top of** that
600-800ms floor, not instead of it — a real remote scatter pan/zoom
under the current zero-caching design should be expected to cost at
least floor-plus-render, not just render alone.

### 3b. MSv4 scale and storage location

Two effects worth separating, since they call for different fixes:

- **Read volume.** A larger dataset, or a broader on-screen selection,
  means more rows read per `query_columns()` call — this is exactly
  the "adaptive pipeline" tiering `msv2_backend.py` already has
  (serial under 500K samples, fused `dask.compute()` 500K–5M, `+`
  thread-parallel Datashader passes above 5M — confirmed by direct grep,
  §3c). More data read is more time in the `dask.compute()` portion of
  §3a's measurement, scaling with selection size, independent of where
  the compute happens.
- **Storage latency.** If the worker process's own disk access is
  actually a SAN (or any networked filesystem) rather than local disk,
  every read inside that `dask.compute()` call pays whatever latency
  that network filesystem has — a cost that exists **only** on the
  remote worker's side, invisible to local-kernel testing entirely
  (Chunk 2d's local-kernel tests read from an ordinary local filesystem
  throughout; a real SAN-backed remote run was never part of what this
  project measured). This is a plausible, named-by-you concern that this
  assessment cannot confirm or size from the code alone — it needs an
  actual measurement against the real remote storage layout (§7).

Both effects make each individual `query_columns()` call slower on a
real, large, remotely-stored MSv4 than anything Chunk 2d's local-kernel
testing (against a small local `.ps.zarr`) would suggest — which raises
the stakes on *not* calling it once per intermediate drag frame, but
doesn't, by itself, change which architectural fix is right.

### 3c. `dask.distributed` availability

**Confirmed directly (grep across `msv2_backend.py`, `msv4_backend.py`,
`_scatter_render.py`): there is no `dask.distributed` wiring anywhere in
this codebase today** — no `Client(...)`, no `get_client()`, no
distributed scheduler configuration. The "+ parallel Datashader passes"
tier above 5M samples uses a plain `ThreadPoolExecutor` — thread-level
parallelism within one process, on one host. Whether that host is a
laptop or a beefy cluster node, today's code uses it identically: one
process, some threads, no multi-node spread. The Chunk 2 implementation
doc named cluster-wide distributed execution as a goal "independent of
the bandwidth concern" when the server-side-binning redesign was done
(a lazy Dask graph is exactly what `dask.distributed` could spread
across nodes) — but confirmed, now, as aspirational, not built.

This matters for the panning question specifically because it changes
where the leverage is: if a real multi-node cluster sits behind the
remote host and is never used, the biggest available win for a single
slow render might be wiring up `dask.distributed` for that one call, not
a client-side caching scheme at all. That's a separate, larger piece of
work from anything else in this document (it touches the backend's own
compute path, not `visplot`'s viewport-interaction logic), and isn't
assessed further here beyond flagging it as the thing worth sizing before
assuming caching/overscan is the highest-leverage fix — a render that's
already fast because it's spread across sixteen nodes needs panning
mitigations far less urgently than one that isn't.

---

## 4. Consistency between raster and scatter

Worth being explicit about what "consistency" could mean here, since it
cuts more than one way:

- **Consistent user experience** (pan feels similarly responsive on both
  plot types) — a real, worthwhile goal, and the one most likely meant.
- **Consistent implementation** (scatter adopts raster's exact
  cache-and-crop mechanism) — **not achievable as-is**, per §2c: scatter's
  output is a rendered image, not a re-croppable numeric grid, so
  raster's specific trick doesn't transfer directly. What *can* transfer
  is the general shape underneath it — "fetch more than exactly what's
  displayed once, so a range of nearby follow-up requests can be
  answered from that fetch without a new round trip" — realized
  differently for each (an overscan margin around the requested extent
  for scatter's *image*, the same margin already conceptually present in
  raster's *undecimated* case, or a wider grid crop in raster's
  decimated case).
- **Consistent policy** (both use the same overscan factor, the same
  debounce interval, the same cache-invalidation rule once one is built)
  — achievable and probably worth doing once either is designed in
  detail, so a user doesn't experience one plot type behaving
  noticeably differently from the other in the same session.

Recommendation on this specific point: aim for the first and third,
don't force the second.

---

## 5. Options considered

Presented as options, not a ranked list yet (ranking is §6) — several
are complementary rather than exclusive.

### 5a. Debouncing

Delay issuing the `query_columns()`/`query_raster()` call until the
viewport has stopped changing for some short interval (tens to a couple
hundred milliseconds), rather than firing on every intermediate frame of
a drag. Cheapest possible change: pure client-side (JS) timing logic
around the existing `CustomJS` callback that currently fires the wire
request, no backend/protocol change, no cache/staleness design needed.
Doesn't reduce the cost of any *individual* render; reduces how many are
issued during continuous interaction (a five-second drag becomes one
request at the end, not dozens along the way). Benefits raster's
Level-2 re-query path too (currently, nothing stops a fast zoom gesture
from triggering more than one Level-2 re-query in quick succession if it
crosses the cache-resolution boundary more than once).

### 5b. Stale-while-revalidate placeholder

While a fresh render is in flight, keep showing the *previous* frame
(possibly visibly scaled/cropped to approximate the new viewport) rather
than a blank or frozen plot, then swap in the real result when it
arrives. Improves perceived responsiveness without changing when or how
often a real render happens — complementary to debouncing (debouncing
reduces request count; this improves what the user sees while whichever
requests still happen are in flight) and to overscan (a placeholder
scaled from an overscanned previous frame is a much better visual
approximation than one scaled from an exact-viewport previous frame).
Pure client-side/presentation change again — no backend contract change
needed, since it's about what's shown while waiting, not about what's
requested.

### 5c. Overscan margin

Request and render a region **larger** than the current viewport (e.g.,
1.5–2× on each axis), cache the result client-side, and serve pan/zoom
within that margin locally by cropping — genuinely new for scatter
(§2c: needs the underlying samples re-binned at the wider extent, so
this is a real `query_columns()` call with a wider `x_range`/`y_range`,
not a client-side trick) and an *enhancement* to raster's existing
scheme (today's Level-2 re-query already fetches at higher resolution
when needed, but at the exact new viewport with no margin — see §2a's
"free unless you're actively exploring past the edge" nuance). Real
tradeoffs, not free: more data read/binned per request (directly
compounding with §3a/§3b's per-render cost, working against the memory-
safety reasoning that motivated moving scatter's binning server-side in
the first place — an overscan factor needs its own bound, not an
unbounded "fetch everything nearby"), and a cache-invalidation question
that raster's simpler "cropped from one big cached agg" design mostly
avoids by not needing per-layer cache-freshness tracking (scatter's
per-layer alpha/colormap/scaling state already interacts with cached
per-layer images via `_recomposite()` — an overscan cache needs to
compose with that correctly, not introduce a second, parallel notion of
"the currently cached render").

**If overscan is pursued for scatter, the coarse identity grid (§2d)
should almost certainly be sized and cached using the same overscan
extent as the display image**, not the exact viewport — otherwise a
pan within the cached image's margin would show data the hover/click
probe can't yet identify without a fresh call, reintroducing a
round-trip exactly where the image itself no longer needs one.

### 5d. A prefetch/background-refresh tier, modeled on raster's own two levels

A more direct analogy to raster's actual design than plain overscan:
render at the exact requested viewport (fast response), *and* kick off a
background re-render at a wider extent (or higher resolution) that
replaces the cache once it lands, so a *subsequent* pan/zoom has a
better chance of being served locally, without making the *current*
interaction wait for the wider render. More moving parts than 5c (two
outstanding requests to reason about instead of one, a real "which
result is newer" question), but avoids paying overscan's extra cost on
every single request — only on ones where the wider version isn't
already cached and fresh.

### 5e. Client-side render cache (LRU of recent viewports)

Independent of overscan: keep the last N rendered results (per layer,
per selection/color-mode/scaling state) and serve an exact repeat
viewport (zoom out then back in to the same spot, or an axis toggle and
back) from cache rather than re-rendering. Cheap to add on top of any of
the above, catches a specific, common interaction pattern (backtracking)
that none of 5a–5d directly address, and needs a cache-key design that
correctly includes every input `render_layer` actually depends on
(selection, `x_range`/`y_range`, `color_mode`, per-layer scaling/cmap —
already enumerated as `ScatterLayerSpec`'s fields, so the key is close to
"that dataclass plus the viewport tuple," not something to invent from
scratch).

### 5f. Wire up `dask.distributed` for the aggregation itself

Orthogonal to all of the above — makes each individual render faster by
spreading it across a real cluster (§3c), rather than reducing how often
a render has to happen or improving what's shown while waiting. Only
worth real investment if §3c's cluster-availability assumption is
confirmed true for the actual deployment target and a real render is
confirmed slow enough on that cluster's single-node path to justify it —
neither confirmed here (§7).

---

## 6. A rough prioritization, and why

This is a judgment call, offered for discussion, not a settled decision
— the honest answer is that §7's measurements should inform this more
than this document alone can.

1. **Debouncing (5a) first.** Smallest change, lowest risk, helps both
   plot types immediately, and doesn't require deciding anything about
   caching/overscan architecture first — it's very unlikely to be wrong
   to do regardless of what's decided about the rest.
2. **Stale-while-revalidate placeholder (5b) next**, for the same
   reasons (cheap, low-risk, presentation-only) — and it gets
   meaningfully better once an overscan margin exists to scale the
   placeholder from, so doing 5a/5b before committing to 5c/5d is a
   reasonable sequencing even though none strictly depends on another.
3. **Overscan (5c) or the prefetch tier (5d) for scatter specifically**
   is the real architectural decision, and the one most affected by §3's
   three factors — genuinely worth deferring until §7's measurements
   exist, since the right overscan factor (or whether prefetch's extra
   complexity is worth it over plain overscan) depends on real numbers
   this document doesn't have: how much slower is a real remote render
   actually, end to end, against real data at real scale.
4. **The render cache (5e)** can be added independently, at any point,
   with the least regret if priorities shift.
5. **`dask.distributed` (5f)** is its own project, not a viewport-
   responsiveness tweak — worth sizing (§3c) but shouldn't block or be
   blocked by 1–4.

---

## 7. What would need to be measured before committing further

Named plainly rather than assumed:

- **A real render's actual cost against real remote storage.** Partially
  answered since this was first written: a real ~600-800ms per-call
  floor is now confirmed (§3a) — but that's a floor for ordinary calls,
  not specifically for a *large* remote render against genuinely
  networked storage. This project's own compute-cost measurement (§3a)
  is still against a small local dataset on ordinary local disk. Nothing
  here says how much of a large real render's cost is I/O versus compute
  once the I/O is genuinely networked (SAN or otherwise), which directly
  determines whether overscan's "read more per request" tradeoff is
  worth it or actively harmful.
- **Whether a real multi-node cluster actually sits behind the intended
  remote host(s)**, and if so, whether `dask.distributed` is already
  available/configured there or would need its own setup work — changes
  whether §5f is a near-term option or a much longer-term one.
- **Real user interaction patterns** — how much backtracking (5e's
  target) versus continuous exploration (5a–5d's target) actually
  happens in practice, which this document has no data on.
- ~~Whether "the scatter remote-execution design notes" document
  mentioned in §2b actually exists somewhere outside what this
  assessment had access to~~ — resolved: searched the actual repository
  directly and confirmed it does not exist there under that name or any
  found equivalent (§2b has the full account). Struck through rather
  than deleted, per this document set's own convention for resolved
  open questions.
