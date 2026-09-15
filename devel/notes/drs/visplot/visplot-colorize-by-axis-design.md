# visplot Scatter "Colorize by Axis" — Design Document

**Status:** v1 — initial design (Part 1)
**This is a living document.** Update it as later parts surface new
information or force a decision to change; don't create a competing copy.
See the changelog at the bottom for revision history.

---

## 1. Background

Originated from a stakeholder claim that `VidaVis` supported choosing an
aggregation function for scatter point coloring, plus metadata-based ("third
dimension") coloring. Investigation (see prior session notes /
`visplot-scatter-aggregation-and-plotms-parity.md`) traced these to two
separate, real things:

- VidaVis's actual aggregator feature (`max`/`mean`/`median`/`min`/`std`/`sum`/`var`)
  belongs to `MsRaster`, not scatter, and reduces a genuine non-plotted xarray
  dimension before any rendering step.
- The "color by metadata" recollection was confirmed by the stakeholder to
  actually be CASA **PlotMS**'s "colorize by axis" feature — coloring scatter
  points by a categorical axis (baseline, antenna, correlation, scan, SPW,
  etc.), one of PlotMS's more valued features.

Of everything reviewed in the PlotMS parity pass, **colorize-by-axis is the
only aggregation-adjacent gap with both a confirmed need and a concrete,
understood implementation mechanism** (Datashader's categorical aggregation).
This document scopes that feature.

---

## 2. Feature definition

For a given scatter layer, the user selects a **categorical axis** (e.g.
Correlation, Scan, SPW, Antenna1) instead of the current continuous
value-based coloring. Each rendered pixel is colored by the dominant category
among the raw samples landing there (Datashader `ds.by()` / `count_cat()`),
with a legend mapping category → color. This **replaces** that layer's
continuous `mean(y)`-based coloring and colorbar — it does not run alongside it.

---

## 3. Current state (what colorize-by-axis has to fit into)

Verified directly against the source; this is the baseline every later part
should build from rather than re-derive.

**Today's per-pixel coloring** (`_scatter_render.py`):
```python
agg = cvs.points(df, "x", "y", ds_agg.mean("y"))
```
One continuous scalar per pixel, run through `linear`/`log`/`eq_hist`/explicit
scaling to a colormap gradient.

**`ScatterLayerSpec`** (`reader.py`) — all fields are continuous-coloring
concepts; nothing categorical exists today:
```python
y_axis, polarization, cmap (tuple[str,...] — a gradient),
alpha, scaling ("eq_hist" default), scaling_alpha, scaling_gamma,
scaling_vmin, scaling_vmax
```

**`ScatterLayerRender`** (`reader.py`) — same story: `peak_value`,
`hist_counts`/`hist_edges`, `mapping_x`/`mapping_u` all exist specifically to
drive a continuous colorbar. The `id_grid_*` fields (min/max of
time/baseline_id/frequency per coarse bin) are range-based, for the
hover-probe — not categorical either.

**Metadata already in the per-row dataframe:** `time`, `baseline_id`,
`frequency` are *conditionally* present today, populated only for the
hover-probe id-grid (`_scatter_render.py`'s `id_cols` check), not for coloring.
No other discrete axis (correlation, scan, SPW, antenna, observation, intent)
is currently carried per-row.

**Palettes** (`palettes.py`): only continuous sequential ramps exist today,
theme-aware (light/dark), keyed as a "family" for multi-polarization layers.
No categorical/distinguishable-color-set palette exists.

**Legends** (`panel_spec.py`, `png_export.py`): today's legend is per-band
(polarization), with peak density folded in — a single swatch per layer, not
per category.

**Backend symmetry constraint:** `query_columns()` is documented as
"implemented identically by both `MSv2Backend` and `MSv4Backend`" — any new
column-plumbing work has to land in both and stay in sync.

---

## 4. Scope decisions

Each item below is either a **firm architectural choice** (low ambiguity,
stated with rationale) or a **proposed default** (a real judgment call —
flagged explicitly, revise freely).

### 4.1 Which axes are colorizable — *proposed default*
Start with the bounded-cardinality `NATIVE_DISCRETE` axes already defined in
`axes.py`: **Correlation, Scan, SPW, Antenna1, Antenna2, Observation, Intent.**

**Baseline is proposed as excluded** (or at least deprioritized): a typical
array has enough baselines that per-category colors stop being visually
distinguishable, and this matches a known usability limitation of PlotMS's
own baseline colorization. Worth confirming against your own typical dataset
sizes before treating this as final.

### 4.2 Cardinality cap — *proposed default*
Cap at **~20 categories** (comparable to a standard categorical palette like
Bokeh's Category20). If the current selection resolves to more distinct
values than the cap for the chosen axis, refuse with a message asking the
user to narrow the selection, rather than silently bucketing extras into an
"other" category. Auto-bucketing is a reasonable future enhancement, but
adds complexity (legend semantics for "other") not needed for a first version.

### 4.3 Interaction with existing scaling controls — *firm*
Enabling colorize-by-axis on a layer disables/hides that layer's continuous
scaling controls (colormap picker, span/vmin/vmax, eq_hist toggle) and swaps
its colorbar for the new categorical legend. The two coloring modes are
mutually exclusive per layer, not combinable.

### 4.4 Per-layer vs. plot-wide — *proposed default*
Colorize-by-axis is a **per-layer** setting (consistent with the existing
`ScatterLayerSpec` model — e.g. today's independent XX/YY continuous
coloring). A layer could be colorized while a sibling layer stays continuous.
Flagging this as worth a sanity check: it's the architecturally natural
choice, but confirm it's actually the UX you want before Part 4 builds to it.

### 4.5 Data availability gates the axis list
Whichever axes are chosen for §4.1 must actually be plumbed as per-row
columns before Part 3 can do anything with them — this is Part 2's job (see
handoff document). If plumbing turns out to be materially harder for a
particular axis (e.g. Intent/Observation needing an extra join), that's a
legitimate reason to revise §4.1's list, and should come back to this
document rather than being quietly absorbed.

---

## 5. Roadmap / part breakdown

| Part | Scope | Primary files | Status |
|---|---|---|---|
| 1 | Design + this document + Part 2 handoff | — | **This session** |
| 2 | Backend metadata plumbing | `msv2_backend.py`, `msv4_backend.py` | Not started |
| 3 | Rendering pipeline: categorical aggregation, new dataclass fields, categorical palette | `_scatter_render.py`, `reader.py`, `palettes.py` | Not started |
| 4 | UI wiring: axis picker, mutual-exclusivity logic, category legend widget, export swatches | `visibility_plotter.py`, `panel_spec.py`, `png_export.py` | Not started |

Each part produces a handoff document for the next (scoped work order +
open questions), and updates this design document if it forces a decision to
change. This document is the durable "why," the handoffs are the disposable
"what's next."

---

## 6. Explicit non-goals for this feature

Carried over from the broader PlotMS parity review — not part of
colorize-by-axis, don't let scope creep pull them in:

- Plot-time averaging (separate gap, separate design needed)
- Broader iteration axes beyond Field/SPW
- Data-column (DATA/CORRECTED/MODEL) overlay
- `dynspread` for zoomed views
- ATM/Tsys curve overlay

---

## 7. Open questions log

Quick-scan list of everything not yet confirmed. Move resolved items to §4
with the decision recorded; add new ones as they surface.

- [ ] Is Baseline really out of scope, or is there a workflow where it's
      still wanted despite high cardinality (§4.1)?
- [ ] Is 20 categories the right cap, or should it vary by axis (§4.2)?
- [ ] Per-layer colorization confirmed as the desired UX, not plot-wide (§4.4)?
- [ ] Any axis in §4.1 that Part 2 finds materially harder to plumb than the
      others — should it be dropped or deferred to a v2?

---

## 8. Changelog

- **v1** — Initial design, written end of Part 1. Scope decisions in §4 are
  proposals pending confirmation; roadmap and non-goals are considered firm.
