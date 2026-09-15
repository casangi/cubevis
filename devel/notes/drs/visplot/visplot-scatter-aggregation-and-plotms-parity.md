# visplot Scatter Aggregation & PlotMS Feature Parity — Working Notes

**Context:** A stakeholder claimed the `VidaVis` prototype (hvPlot-based precursor to
`visplot`) let users choose the *aggregation function* used to color scatter points
(max, min, etc.), and separately that color could be driven by point metadata
(e.g. baseline) as a kind of third dimension. This raised the question of whether
`visplot`'s current scatter coloring is missing something VidaVis already solved.
These notes capture what was actually verified in both codebases, where the
stakeholder's memory likely came from instead, and a broader PlotMS-parity pass
that came out of the same conversation.

---

## 1. What visplot's scatter coloring actually does today

Source: `_scatter_render.py`, `render_layer()`.

```python
agg = cvs.points(df, "x", "y", ds_agg.mean("y"))
```

For each **screen pixel**, Datashader averages the `y` value of every raw sample
landing in that pixel. That per-pixel scalar is what feeds the scaling pipeline
(linear / log / eq_hist / explicit) to pick a color.

This is **not** count-based. The original hypothesis ("color reflects how many
points are coincident") does not match the current implementation — it's a
value-based statistic (mean of `y`) per screen pixel, not an occurrence count.
"Coincidence" here is a screen-space rounding artifact of canvas resolution, not
points that are semantically at the same (x, y).

---

## 2. What VidaVis (`github.com/casangi/vidavis`) actually contains

Verified directly against the repo source (not just the README).

### No scatter application ships in the package
`src/vidavis/apps/` contains only `_ms_raster.py`. The only scatter-related code
anywhere in the repo is a rough, non-integrated prototype:

- `devel/notes/ph/vis_scatter.py` — a personal dev-notes script, not part of the
  library. It uses plain `ds.count()` with **no** selectable aggregator — i.e.
  the literal "count of occurrences" behavior originally suspected, but attached
  to a throwaway prototype, not to anything a stakeholder would have used
  day-to-day.

### The real "aggregator" feature exists — but on `MsRaster`, not scatter
Confirmed in `_ms_plot_constants.py`:

```python
AGGREGATOR_OPTIONS = ['None', 'max', 'mean', 'median', 'min', 'std', 'sum', 'var']
```

Selected via `aggregator=` + `agg_axis=` on `MsRaster.plot()`. Critically, this is
**not** a Datashader per-pixel reduction. `_ps_raster_data.py`'s `aggregate_data()`
does plain xarray reduction on the raw dataset *before* any canvas/rendering step:

```python
if aggregator == 'max':
    agg_xds = xds.max(dim=apply_agg_axis, keep_attrs=True)
elif aggregator == 'mean':
    agg_xds = xds.mean(dim=apply_agg_axis, keep_attrs=True)
# ... median, min, std, sum, var
```

`agg_axis` must be a genuine, non-plotted data dimension (checked in
`_check_raster_inputs.py`) — e.g. reducing over `frequency` while plotting
`time` vs `baseline`. This only makes sense because there's a real, hidden third
axis producing multiple raw values at one (x, y) cell — not because Datashader
happened to bin two different points into the same screen pixel.

### No categorical/metadata coloring found
Grepped the whole repo for anything resembling Datashader's categorical coloring
(`count_cat`, `color_key`, "color by baseline as a category"). Nothing found.
The "color determined by metadata, e.g. baseline" recollection is **not** backed
by anything in VidaVis's source.

### visplot already has a raster-side precedent for this — just not user-facing
`msv2_backend.py` / `msv4_backend.py`'s raster path already collapses non-plotted
dimensions before gridding:

```python
reduce_dims = [d for d in q.dims if d not in (y_name, x_name)]
if reduce_dims:
    q = q.mean(dim=reduce_dims, skipna=True)
```

(Documented in `reader.py`'s `query_raster` docstring: "reduces... by averaging
over all dimensions not in (y_dim, x_dim)".) So the structural concept VidaVis
exposes as a user choice already exists in visplot's raster path — it's just
hardcoded to `mean` rather than selectable.

**Conclusion:** the stakeholder's "choose an aggregator" memory is real and
traceable to VidaVis, but belongs to raster, not scatter. Scatter's own
Datashader pixel-binning (`ds_agg.mean("y")`) could trivially be swapped for
`max`/`min`/`count`, but that would be a different (though still legitimate)
kind of aggregation choice than what VidaVis's `MsRaster.aggregator` does.

---

## 3. The "metadata coloring" idea → traced to CASA PlotMS, not VidaVis

Stakeholders confirmed they were conflating VidaVis's raster aggregator
flexibility with PlotMS's **"colorize by axis"** feature (color points by
baseline / antenna / spw / scan / correlation, etc.) — described as one of the
more useful PlotMS features. This is a real, well-known PlotMS capability that
predates VidaVis and is exactly the kind of thing visplot is meant to eventually
supersede. (Not verified against PlotMS source directly — this is general
domain knowledge — but it fits the stakeholder's description far better than
anything actually present in VidaVis.)

---

## 4. `dynspread` — the "inverse-density point sizing" question

Stakeholders separately recalled seeing a Datashader argument that (intuitively)
sized plotted pixels inversely to local point density, to draw attention to
outliers. This matches **`datashader.transfer_functions.dynspread`**
(also exposed directly in hvPlot as a plain `dynspread=True` kwarg — notable
since hvPlot is the layer VidaVis actually rendered through).

**Mechanism:** a post-processing dilation step on the already-shaded image.
Grows rendered pixels outward (up to `max_px`) when the overall plot is sparse;
leaves dense regions alone. It computes **one global spread radius for the whole
image** via a density heuristic (fraction of pixels with non-empty neighbors vs.
a `threshold`), then applies that radius uniformly — it is not a per-pixel
adaptive size based on the density at each specific location.

**Verified:**
- Not called anywhere in VidaVis's own source (grepped the repo — no hits).
  Likely seen directly in hvPlot's public docs/API rather than in VidaVis's code.
- Not currently used anywhere in visplot's pipeline either.
- Datashader's own project has an open, acknowledged issue describing
  `dynspread`'s single-global-radius heuristic as poorly suited to images with
  mixed sparse/dense regions in the same view.

**Applied to a real visplot screenshot (Amplitude XX/YY vs. UV Distance):**
the plot has a dense, near-saturated noise floor across most of the UV-distance
range *and* a sparse scatter of outlier points at higher amplitude — the exact
heterogeneous-density case `dynspread` handles badly. Any single global radius
is a compromise: big enough to help the outliers pop also blurs the dense floor;
small enough to preserve the dense floor does nothing for the outliers.

**Conclusion:** not recommended for the general/overview scatter view.
visplot's existing elevated per-pixel `min_alpha` (`_MIN_ALPHA = 90` in
`_scatter_render.py`, chosen specifically to help sparse visibility "without
flattening dense-region contrast") already addresses the underlying goal in a
way that's per-pixel rather than image-global, and doesn't have the same
mixed-density failure mode.

**Worth keeping in mind:** once a user zooms into a sufficiently narrow,
roughly-homogeneous sub-region (e.g. just the outlier band, or deep inside one
dense stripe), `dynspread`'s single-regime heuristic has a much easier problem
to solve and could plausibly help there. Revisit for zoomed views specifically,
not as a global default.

---

## 5. PlotMS feature-parity audit

A pass through visplot's own source to separate "already covered," "genuine
gap, worth adding," and "doesn't map well to the Datashader rendering model."

### Already implemented (don't re-add)

| Feature | Where |
|---|---|
| Flag versions (save/restore/list) | `reduction_context.py`: `save_flag_version`, `restore_flag_version`, `list_flag_versions` |
| Flag extension (corr/chan/spw/scan) | `reduction_context.py`: `extend_corr`, `extend_chan`, `extend_spw`, `extend_scan` — explicitly commented as mirroring "the plotms flag extension parameters" |
| Interactive box-select flag/unflag + undo | `flag_db.py` |
| UV range selection | `uvrange` string param, GUI text input + query plumbing in `visibility_plotter.py` |
| Per-band/layer legend (incl. peak-density annotation) | `panel_spec.py`, `visibility_scatter.py` |
| Iteration (Field / SPW) with animate Prev/Next | `iteration_step.py` |
| M×N grid export, blank-but-framed empty cells | `png_export.py` |
| Broad axis vocabulary (uvdist-λ, az/el/hour-angle/parallactic-angle, weight/weight-spectrum, calibration axes like Tsys/SNR/delay/gain amp-phase) | `axes.py` |
| Flagged-data visual distinction | Red overlay composited via the Datashader pipeline, not a separate marker shape (`flag_db.py` docstring) — arguably better suited to a density-image renderer than literal flagged-marker styling would be |

### Genuine gaps — worth adding

1. **Plot-time averaging** (time / channel / baseline / scan; vector vs. scalar).
   The only "averaging" currently in the codebase is an offline split/mstransform-style
   operation producing a *new* MS (`reduction_context.py`), not a live
   "average across channel" toggle on the current view. **Highest-value gap** —
   heavily used in PlotMS, and architecturally compatible: it's the same shape as
   the raster path's existing `q.mean(dim=reduce_dims)` reduction-over-a-hidden-axis,
   just needing to (a) become user-selectable and (b) apply before the scatter
   dataframe is built, not only in the raster path.

2. **Colorize by axis** (categorical coloring by baseline/antenna/corr/scan/spw).
   Confirmed absent (zero hits for "colorize" anywhere in visplot). Maps cleanly
   onto Datashader via `ds.by()` / `count_cat()` with a `color_key` — a genuinely
   different, additional code path from the current `mean("y")` coloring, not a
   replacement for it. No architectural fight required.

3. **Broader iteration axis** (scan / baseline / antenna / correlation, beyond
   today's Field/SPW). Pure control-flow extension — `iteration_step.py`'s own
   docstring already anticipates "future axes" as forward-compatible. Lower
   effort than the two above.

4. **Data-column overlay** (DATA vs. CORRECTED vs. MODEL as simultaneous layers).
   `SelectionSpec.data_column` is singular today, but the multi-layer/band
   compositing system already used for polarization overlays (e.g. XX+YY) is the
   same machinery this would need.

### Doesn't map well — advise against a literal port

Both stem from the same root cause: PlotMS pre-averages down to a bounded
number of markers and draws each at a fixed size, so "one marker" reliably
means "a modest, human-scale amount of underlying data." Datashader's per-pixel
density image has no equivalent guarantee.

- **Symbol size as a user control.** No equivalent concept exists in a
  density-image renderer — there's no discrete "symbol" to resize. The nearest
  mechanism, `dynspread`, is a different thing (post-hoc image dilation, not a
  size setting) and has the mixed-density failure mode documented in §4.
  Recommendation: don't attempt to simulate this.

- **Literal "locate → table of underlying rows."** Works in PlotMS because one
  marker = a bounded number of averaged rows, so the popup table is short and
  readable. In visplot, a screen pixel can represent anywhere from one to
  millions of raw samples, since there's no pre-averaging shrinking the count
  first. This has already been solved differently, not left undone: the
  hover-probe's coarse id-grid (min/max of time/baseline/frequency per cell —
  see `_scatter_render.py`'s `id_grid_*` fields) is the adapted answer to the
  same underlying need, without pretending a pixel has a short, enumerable row
  list behind it. Treat that as the intentional replacement, not a gap.

- **ATM/Tsys curve overlay** (atmospheric transmission curve on spectral plots).
  Confirmed absent. Technically compositable (just another line glyph over the
  image, similar to the flag overlay), but needs atmosphere-model data plumbing
  that doesn't currently exist in the architecture. Lower priority — a new
  dependency more than a rendering-model mismatch.

**Dependency note:** adding plot-time averaging (gap #1) would strengthen the
case for revisiting symbol-size and locate-table later — fewer, statistically
real points per pixel narrows the gap between visplot's density model and
PlotMS's marker model. It still wouldn't make literal symbol sizing meaningful,
but it would make a locate table more reasonable in size in the common case.

---

## 6. Suggested priority order (if acting on the gaps)

1. Plot-time averaging — highest value, clear architectural precedent.
2. Colorize by axis — clean Datashader fit, directly resolves the original
   stakeholder ask (once correctly attributed to PlotMS rather than VidaVis).
3. Broader iteration axis — smallest lift, extends existing forward-compatible code.
4. Data-column overlay — nice-to-have, reuses existing layer machinery.
5. `dynspread` for zoomed/homogeneous sub-views only — not a global default.
6. ATM/Tsys overlay — defer; needs new data dependency, narrower audience.

## 7. Open items for the stakeholder conversation

- Confirm the "aggregator" recollection was about raster, not scatter.
- Confirm "colorize by axis" is remembered from PlotMS, not VidaVis (they
  already agreed on this in discussion — worth stating back for the record).
- Decide whether plot-time averaging should support both scalar and vector
  averaging (PlotMS distinguishes these for complex visibility data) or start
  with one.
