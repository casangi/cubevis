# Reference: rflag/tfcrop prior art and Dask feasibility for statistical colorization

**Companion to:** `visplot-colorize-by-axis-design.md` §7 (Part 6 —
corrected 2026-09 from a wrong citation of §9, which is that document's
changelog; also renamed from "Part 5" to "Part 6" at the same time, to
stop colliding with the unrelated, already-shipped "Part 5"/"Part 5a"
feature — see that document's naming note). This
document holds the detail Part 6 leans on but doesn't need to restate
in full — read it when implementing Part 6's backend, not before.

---

## 1. What CASA's `rflag`/`tfcrop` actually do

Both are modes of the `flagdata` task (CASAdocs); both write directly to
the MS's `FLAG` column when run in `action='apply'` mode (confirmed —
this is genuinely a flagging algorithm, not a display tool, which is
exactly why Part 6 is scoped as an *approximation* rather than a
wrapper around it — see §7.2 of the design doc).

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
moderate departures). This is the direct precedent for Part 6's
confirmed modified z-score (§7.4 of the design doc).

**`tfcrop`** takes a different mechanism to a similar end: it fits a
robust piecewise polynomial to the average bandpass shape (up to 5
iterations, each re-fitting after excluding points beyond N-stddev from
the previous fit) and flags departures from that fit. Useful reference
if Part 6's backend investigation finds windowed-RMS awkward for a
particular axis shape, but not the primary target — `rflag`'s two-step
time/frequency structure is the closer match to raster's existing
Time×Channel (`waterfall` preset) and Baseline×Time (`vplot`/`radplot`)
grids.

**Why real/imaginary, not amplitude — verified, with one correction
(2026-09):** visibility noise is standard circularly symmetric complex
Gaussian noise — real and imaginary parts i.i.d., equal variance
(Thompson, Moran & Swenson, *Interferometry and Synthesis in Radio
Astronomy* — the field's standard reference; confirmed directly in
current literature, e.g. Kolopanis et al. 2023, arXiv:2211.13576 §3).
Amplitude is not Gaussian-distributed at low SNR (closer to
Rician/Rayleigh) precisely because it's a magnitude taken over that
same circularly symmetric noise. **Correction:** this is *not*,
however, `rflag`'s own default — CASA's `flagdata` `correlation`
parameter, which selects what `rflag`/`tfcrop`/`clip` actually operate
on, defaults to `ABS_ALL` (amplitude); `REAL_ALL`/`IMAG_ALL` are
options a user selects explicitly, confirmed directly against
CASAdocs (`flagdata`'s `correlation` parameter documentation — see §4).
The Gaussianity argument for preferring real/imaginary over amplitude
stands on its own statistical merit regardless of what `rflag`
defaults to; Part 6's default should follow it, same conclusion as
before — the *display* axis and the *statistics basis* remain
independent choices — just not on the grounds that it matches `rflag`'s
own default behavior.

**Combining real and imaginary into one score — resolved 2026-09, not
independently as the basis-only framing above might suggest.** Because
real and imaginary noise are i.i.d. with *equal* variance (the same
fact behind preferring them over amplitude), the correct generalization
of a robust z-score to two dimensions is one joint, rotation-invariant
radial statistic, not two independent per-part z-scores combined
afterward — the same "robust distance, then a modified z-score on the
distance" pattern already standard for multivariate outlier detection,
specialized to the isotropic case. Independent per-part z-scores would
implicitly model an anisotropic noise source that doesn't match the
physics, and would score the same physical anomaly differently
depending on its arbitrary phase/calibration convention. See
`visplot-colorize-by-axis-design.md` §7.4 for the full formula,
including a derived (not yet simulation-verified) replacement for the
standard modified z-score's `0.6745` calibration constant, needed
because a 2D radial deviation follows a Rayleigh distribution rather
than a symmetric 1D one. The primary sources are honestly ambiguous on
whether `rflag`/AIPS itself computes this jointly or per-part
internally (the AIPS `RFLAG` help text describes an *optional* mode
computing real and imaginary statistics "individually"; CASAdocs'
prose — "local rms of real and imag visibilities" — reads more like a
single combined quantity, but isn't fully explicit either way) — this
does not change the recommendation, since Part 6 is already scoped as
an approximation in statistical philosophy, not a bit-exact
reimplementation (§7.2 of the design doc).

**Displaying magnitude, not a signed value — confirmed, and it's the
literal convention for this exact statistic.** The modified z-score is
Iglewicz & Hoaglin's own statistic; their recommended outlier rule is
stated as an absolute value, `|Mᵢ| > 3.5` (Iglewicz, B. and Hoaglin,
D.C., 1993, *How to Detect and Handle Outliers*, ASQC Basic References
in Quality Control) — universal across every application of this
formula found during this review, not a UX preference layered on top.
A radial joint statistic (above) is non-negative by construction, which
settles this in the same step: there is no sign left to decide what to
do with.

**CASA already has a preview-only mode.** `flagdata(..., action='calculate',
display='both')` computes rflag's statistics and shows a (static,
non-interactive) plot of what would be flagged, without writing
anything. This is real, established practice — VLA/ALMA reduction
guides routinely recommend running `action='calculate'` before
`action='apply'`. Part 6 is best understood as bringing this same
"compute and show, don't commit" mode into an interactive, modern tool,
not as introducing a new concept to the workflow.

## 2. Using real `flagdata` as a validation oracle

Because `rflag`/`tfcrop` write real, well-defined output, running
`flagdata(mode='rflag', action='calculate')` against the same MS and
comparing its computed thresholds/candidate-flags to Part 6's own score
would give a genuine correctness check — the same role real
`casacore`/`arcae` ground truth played in Part 2's verification.

**Resolved 2026-09: `flagdata` does not operate on MSv4 Processing
Sets** (confirmed directly, not the "every example used `vis='....ms'`,
no evidence either way" inference this section originally relied on).
That confirms the argument this section already anticipated: Part 6
builds its own computation as the actual deliverable, symmetric across
both backends, consistent with everything else this project has cared
about — real `flagdata` is, at most, an MSv2-side verification
technique, and even then must stay a one-off, offline comparison
script outside this project's own dependency chain, never a runtime
one: Part 6 is confirmed independent of `casatools`/`python-casacore`
entirely (`arcae` remains available where a lower-level MSv2 read
genuinely needs it, since it's already a dependency of this codebase's
own MSv2 path via `xarray-ms`, but `casatools`/`python-casacore`
specifically are not to be imported or called from anywhere in Part
6's runtime path, including this validation-oracle idea if it's ever
picked up).

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
acceptable asymmetry or a reason to scope Part 6's expensive tier
(**Slice 3**, deferred — see `visplot-colorize-by-axis-design.md` §7.5/
§7.10) to MSv4 initially, whenever that tier is actually taken up.

## 4. Citations

- CASAdocs, `flagdata` task reference (rflag/tfcrop algorithm
  descriptions, and the `correlation` parameter's `ABS_ALL` default for
  rflag/tfcrop/clip — §1's correction above):
  https://casadocs.readthedocs.io/en/v6.2.0/api/tt/casatasks.flagging.flagdata.html
- CASA Guides, VLA Flagging (rflag `action='calculate'` preview
  workflow): https://casaguides.nrao.edu/index.php?title=VLA_CASA_Flagging-CASA6.5.4
- `tfcrop`/`rflag` algorithm walkthrough (R. Urvashi):
  https://www.aoc.nrao.edu/~rurvashi/TFCrop/
- AIPS `RFLAG` help (the "individually"-computed real/imaginary option
  §1's correction above weighs against a purely joint reading of the
  CASA prose): http://www.aips.nrao.edu/cgi-bin/ZXHLP2.PL?RFLAG
- Thompson, A.R., Moran, J.M., & Swenson, G.W., *Interferometry and
  Synthesis in Radio Astronomy* — standard reference for the circularly
  symmetric complex Gaussian visibility-noise model behind §1's
  real/imaginary joint-statistic correction.
- Kolopanis et al. 2023, "Why and When to Expect Gaussian Error
  Distributions in Epoch of Reionization 21-cm Power Spectrum
  Measurements," confirming the same noise model directly:
  https://arxiv.org/pdf/2211.13576 (§3)
- Iglewicz, B. and Hoaglin, D.C., 1993, *How to Detect and Handle
  Outliers*, ASQC Basic References in Quality Control: Statistical
  Techniques — the modified z-score's own source, including its
  `|Mᵢ| > 3.5` absolute-value convention (§1's magnitude confirmation
  above).
- Dask, `dask.dataframe.DataFrame.quantile` (tdigest method):
  https://docs.dask.org/en/latest/generated/dask.dataframe.DataFrame.quantile.html
- Coiled, "Faster Xarray Quantile Computations with Dask" (Dec 2024,
  Dask 2024.11.2 speedup): https://docs.coiled.io/blog/array-quantile.html
