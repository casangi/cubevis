# Notes: statistics "harvested" during data loading

**Status: background rationale only.** This document captures an
insight from discussion, for whichever future part needs it. It does
not authorize or schedule any implementation — nothing here is an
active goal of Part 5 or any other current part. See
`visplot-colorize-by-axis-design.md` §9.5 for the one-paragraph summary
this expands on.

---

## The question

Could useful statistics be computed "for free" as a side effect of the
existing per-partition data loading, rather than as a dedicated,
separate pass later? The honest answer splits cleanly in two.

## What's genuinely free: additive statistics

Mean, variance, count, sum, min, and max are *associative* — each
chunk's partial contribution (a partial sum, sum-of-squares, and count;
Welford's algorithm is the standard numerically-stable formulation)
combines with every other chunk's via simple arithmetic, with no need
to revisit or hold the whole dataset. This is exactly why Dask's own
`.mean()`/`.var()` are single-pass and low-memory regardless of scale.

Concretely, in this codebase: `_query_all_partitions_scatter_fused`
(and `_query_partition_scatter`) already build one `all_lazy` list and
issue a single `dask.compute(*all_lazy)` over it. Appending
`.mean()`/`.var()`/`.count()` reductions on the *same underlying
VISIBILITY/FLAG dask arrays* into that same list costs no extra I/O —
Dask's task graph recognizes the shared chunk-read tasks and doesn't
re-read anything from Zarr/the MS to produce the extra summaries. If a
future part wants a cheap, always-available global mean/variance, this
is the mechanism: ride the existing load, don't schedule a second one.

## What isn't free: median and MAD

There's no partial-sum equivalent for "the middle value." An exact
median needs the full population (or a sort/selection algorithm over
it); Dask's approximate alternative (t-digest sketches) *can* be
updated incrementally in the same data-flow sense, but only as an
approximation, not an exact answer. See
`visplot-rflag-colorization-reference.md` §3 for the fuller technical
treatment (this is the same material, just there for Part 5's specific
context; here for general reference).

## The dependency that doesn't go away

Even granting all of the above, a point read early in a stream can't be
correctly scored against "the dataset's final, fully-informed typical
value" until the stream is done (or a running approximation has
converged). That's not an engineering gap to optimize away — it's
inherent to what "compare to the whole dataset" means.

What *can* be optimized is whether that dependency forces a second read
of storage. If per-partition data from "pass one" is cached in memory,
"pass two" (scoring against the now-finished statistic) is a CPU-only
recompute over data already resident — not a second hit against the
MS/Zarr store. If it's discarded, pass two means reading storage again.
This is a genuine, real memory-vs-I/O trade-off for whichever future
part actually builds a global-statistics feature — not something to
resolve here, just something to not have to rediscover later.

## The resolution already reached

Harvesting any of this unconditionally, on every load, "just in case"
some future feature wants it, would trade guaranteed cost for uncertain
benefit — most loads would pay for statistics nobody consumes. The
sane shape is the same one already agreed for the outlier-colorization
feature as a whole: **whatever gets computed should ride behind the
same opt-in flag as the feature that consumes it.** If the feature is
off, nothing is computed and there's no waste to worry about; if it's
on, the read is already being paid for, and folding an associative
reduction into that same pass is close to free by comparison. There is
no version of this that is "worth doing speculatively, before a
consumer exists" — the free ride only applies once something is asking
for the statistic.
