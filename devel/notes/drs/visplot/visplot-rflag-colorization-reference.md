# Reference: rflag/tfcrop prior art and Dask feasibility for statistical colorization

**Companion to:** `visplot-colorize-by-axis-design.md` §9 (Part 5). This
document holds the detail Part 5 leans on but doesn't need to restate
in full — read it when implementing Part 5's backend, not before.

---

## 1. What CASA's `rflag`/`tfcrop` actually do

Both are modes of the `flagdata` task (CASAdocs); both write directly to
the MS's `FLAG` column when run in `action='apply'` mode (confirmed —
this is genuinely a flagging algorithm, not a display tool, which is
exactly why Part 5 is scoped as an *approximation* rather than a
wrapper around it — see §9.2 of the design doc).

**`rflag`** (ref. E. Greisen, AIPS, 2011), two independent steps run
per field/SPW/timerange/baseline:

- **Time analysis** (for each channel): calculate the local RMS of real
  and imaginary visibilities within a sliding time window; calculate
  the **median** RMS across time windows and the **median deviation**
  from it; flag if a window's local RMS exceeds
  `timedevscale × (medianRMS + medianDev)`.
- **Spectral analysis** (for each time): calculate the average and RMS
  of real/imaginary visibilities across channels; calculate each
  channel's deviation from that average and the median deviation; flag
  if a channel's deviation exceeds `freqdevscale × medianDev`.

Both steps use **median-based** statistics deliberately, not mean/stddev
— the whole point is robustness to the very outliers being searched for
(a handful of severe spikes inflate an ordinary stddev enough to mask
moderate departures). This is the direct precedent for Part 5's
proposed modified z-score (§9.4 of the design doc).

**`tfcrop`** takes a different mechanism to a similar end: it fits a
robust piecewise polynomial to the average bandpass shape (up to 5
iterations, each re-fitting after excluding points beyond N-stddev from
the previous fit) and flags departures from that fit. Useful reference
if Part 5's backend investigation finds windowed-RMS awkward for a
particular axis shape, but not the primary target — `rflag`'s two-step
time/frequency structure is the closer match to raster's existing
Time×Channel (`waterfall` preset) and Baseline×Time (`vplot`/`radplot`)
grids.

**Why real/imaginary, not amplitude:** visibility amplitude is not
Gaussian-distributed at low SNR (closer to Rician/Rayleigh); real and
imaginary parts, dominated by additive noise, are much closer to
Gaussian. `rflag` computes its statistics on real/imag for this reason.
Part 5's default should follow this rather than z-scoring whatever axis
happens to be plotted (amplitude, phase, ...) — the *display* axis and
the *statistics basis* are independent choices.

**CASA already has a preview-only mode.** `flagdata(..., action='calculate',
display='both')` computes rflag's statistics and shows a (static,
non-interactive) plot of what would be flagged, without writing
anything. This is real, established practice — VLA/ALMA reduction
guides routinely recommend running `action='calculate'` before
`action='apply'`. Part 5 is best understood as bringing this same
"compute and show, don't commit" mode into an interactive, modern tool,
not as introducing a new concept to the workflow.

## 2. Using real `flagdata` as a validation oracle

Because `rflag`/`tfcrop` write real, well-defined output, running
`flagdata(mode='rflag', action='calculate')` against the same MS and
comparing its computed thresholds/candidate-flags to Part 5's own score
would give a genuine correctness check — the same role real
`casacore`/`arcae` ground truth played in Part 2's verification.

**Open and unverified:** whether `flagdata` operates on MSv4 Processing
Sets, or is MSv2-table-only (every example found during Part 5's design
research used `vis='....ms'`; no evidence either way was found for
Processing Set support). If it's MSv2-only, that's a real argument for
building Part 5's own computation as the actual deliverable (works
symmetrically on both backends, consistent with everything this project
has cared about) while reserving the real-`flagdata` comparison as an
MSv2-side verification technique specifically — not a runtime
dependency. Worth Darrell confirming directly given his CASA6
maintainer role, rather than guessing further here.

## 3. Dask feasibility in more depth

**Associative statistics (mean, variance, count, sum, min/max) reduce
cheaply and distribute naturally.** Each chunk's partial contribution
(e.g. a partial sum, sum-of-squares, and count — Welford's algorithm is
the standard numerically-stable formulation) combines with every other
chunk's via simple arithmetic. Dask's own `.mean()`/`.var()` already do
exactly this as a tree reduction — low memory per node, no full-dataset
materialization, and this genuinely doesn't care whether the scheduler
is a laptop's threads or a hundred-node cluster.

**Median/MAD do not have this property.** An exact median needs a full
sort or selection algorithm over the whole population — expensive at MS
scale, and why every distributed system (Dask included) defaults to
*approximate* quantile algorithms for anything at scale. Concretely:
`dask.dataframe.quantile()` and `dask.array.quantile()` support a
`method='tdigest'` option (t-digest: a mergeable streaming sketch that
gives an ε-approximate quantile, with the same "can be updated
incrementally as data streams by" property mean/variance have — an
approximate median can be gathered "for free" in the same data-flow
sense as an exact mean, just with an accuracy trade-off an exact
computation doesn't have). Worth knowing: Dask's own multidimensional
quantile machinery got a substantial (~20x, per Coiled's write-up)
speedup as of Dask 2024.11.2 — a genuinely recent development, not
something to assume is still as slow as older documentation might
suggest.

**Distributed execution is close to transparent for this codebase.**
`dask.compute()` (already the mechanism throughout `msv2_backend.py`/
`msv4_backend.py`) picks up whatever the ambient default scheduler is.
Instantiating a `dask.distributed.Client()` once — a `LocalCluster` for
a laptop, `dask_jobqueue.SLURMCluster` or similar for an HPC/observatory
cluster — routes every existing `dask.compute()` call through it without
those call sites needing to change.

**A real asymmetry, though, echoing Part 2's own finding:** MSv4/Zarr
is a much more natural fit for distributed scale-out than MSv2. Zarr is
chunk-addressable and safe for many concurrent/distributed readers —
literally why OPT-B (`_query_all_partitions_scatter_fused`) already
exists and already leans on "Zarr is thread-safe." MSv2 goes through
`arcae`/casacore table locking, a much less natural fit for many
distributed worker processes hitting the same MS file over a shared
filesystem (lock contention is a known, not hypothetical, pain point).
A global-statistics feature that's genuinely useful on a cluster is a
better match for MSv4 first — worth deciding whether that's an
acceptable asymmetry or a reason to scope Part 5's expensive tier to
MSv4 initially.

## 4. Citations

- CASAdocs, `flagdata` task reference (rflag/tfcrop algorithm
  descriptions): https://casadocs.readthedocs.io/en/v6.2.0/api/tt/casatasks.flagging.flagdata.html
- CASA Guides, VLA Flagging (rflag `action='calculate'` preview
  workflow): https://casaguides.nrao.edu/index.php?title=VLA_CASA_Flagging-CASA6.5.4
- `tfcrop`/`rflag` algorithm walkthrough (R. Urvashi):
  https://www.aoc.nrao.edu/~rurvashi/TFCrop/
- Dask, `dask.dataframe.DataFrame.quantile` (tdigest method):
  https://docs.dask.org/en/latest/generated/dask.dataframe.DataFrame.quantile.html
- Coiled, "Faster Xarray Quantile Computations with Dask" (Dec 2024,
  Dask 2024.11.2 speedup): https://docs.coiled.io/blog/array-quantile.html
