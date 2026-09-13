# visplot PNG Export — Running Handoff

**Purpose:** capture decisions, rationale, and defects for folding into
`visibility_plotter_implementation_plan.md` and `visibility_plotter_preview.md`.
Written as we go rather than reconstructed at the end — several decisions below
have reasoning that is not recoverable from the diffs.

**Last updated:** 2026-08-19 (rev 48 — restyle body fully isolated)

---

## 0. Start here

The original goal — export a plot to PNG — is **done**, in both directions:

```python
# GUI: the Export PNG button writes the current view, zoom included
# Headless: __call__ is the terminal verb, and iteration is repeated calls
vp = VisibilityPlotter(ms='...', headless=True, plot_width=1400, plot_height=700)
vp(plotfile='amp.png', theme='light')
for spw in (0, 1, 2, 3):
    vp(plotfile=f'amp_spw{spw}.png', spw=[spw])
```

Work since then has been correctness and UI, not export.

### The five rules this document keeps re-deriving

Each was learned from a defect that **looked like success**. They are the most
transferable content here.

1. **A Python-side model change is invisible without a Bokeh server.** Any
   handler that alters what is drawn must *return* the new data for the client
   to install. Assigning `ColumnDataSource.data` succeeds, changes nothing, and
   logs nothing. (§8.16, §8.7)
2. **In plot code, `Axis.label` is never the right source for displayed text.**
   Only `AxisInfo` knows what was actually plotted. (§8.7, §8.26)
3. **A value fixed at construction will not follow a later mechanism** unless
   something explicitly re-pushes it. Four occurrences: startup chrome,
   re-shaded images, axis labels, sidebar section headings. Anything visual
   added to the sidebar must be added to `_THEME_RESTYLE_JS` *in the same
   change*. (§8.14, §8.16, §8.26, §8.35)
4. **The two backends diverge.** Four fixes have landed in one and not the
   other. Shared logic belongs in `reader.py`; `METADATA_KEYS` and the
   parameterised backend tests exist to catch the rest. (§8.7, §8.11, §8.27,
   §8.33)
5. **Assert on structure, not presentation**, and **assert in the same units
   you compute in**. A check that measures luminance while the code conditions
   on RGB distance silently passes what it was written to catch. (§8.15, §7)

### Verification that does not need an MS

* 90 compositor tests (`test_png_export.py`) — no MS, no bokeh, no display
* 34 SPW tests (`test_spw_selection.py`) — pure functions over metadata
* JS/Python tick parity under node — 1500 fuzzed cases across five SI scales
* `_THEME_RESTYLE_JS` syntax-checked under node with every arg stubbed

### What the test dataset cannot show

`sis14_twhya_calibrated_flagged.ms` has **one spectral window**, identified by
**name only**. So SPW *selection* is unexercisable by it (selecting the one
window changes nothing), and CASA-form `spw=N` output is unreachable. Both paths
have already failed silently once each under exactly these conditions. A
multi-window MS is the only real check.

---

## 1. The central architectural finding

**Bokeh contributes no pixels to the data area.**

`VisibilityRaster._render` produces `img32` via
`query_raster → ds.Canvas.raster → _shade_agg → _img_to_uint32`.
`VisibilityScatter._shade_all_layers` produces `img32` via
`cvs.points → tf.shade → Porter-Duff composite`. Both push the result into a
`ColumnDataSource` consumed by a single `image_rgba` glyph. Bokeh draws only the
*chrome* — title, axis labels, ticks, tick formatter, toolbar, hover.

This reframes the sync problem. The matplotlib export path is **not** a parallel
rendering implementation; it is a parallel **chrome** implementation over an
identical `(H, W) uint32` array. "Keep two renderers in agreement" (hard,
open-ended) becomes "keep two frame-drawers in agreement" (bounded, ~10 fields).

Consequence for the fidelity claim, in tiers:

| Tier | Claim | How it is tested |
|------|-------|------------------|
| 1 | The RGBA array is byte-identical | Hash `RenderedPanel.image`; guaranteed only at matched canvas size |
| 2 | Ranges, tick label *strings*, titles, labels identical | String comparison; JS/Python parity harness |
| 3 | Fonts, chrome pixel positions, grid styling | Best effort. **Do not image-diff whole figures.** |

Theme is a deliberate tier-3 exception: the GUI is dark, exports default to
light, because a PNG is usually headed for a paper.

---

## 2. Decisions taken

### 2.1 One signature, two consumers — no second scraped template

`VisibilityPlotter.__init__` holds the entire configuration vocabulary.
`show()` → Bokeh GUI; a new terminal method → matplotlib PNG. The generated
`visplot` shim dispatches on whether an output-file parameter was supplied.

Rationale:
- The scraper already reads `__init__`. Export parameters added there are picked
  up free: `inp()`, `_arg_default`, `tget`/`tput`, validators.
- A second scraped class would mean two constructors that must agree on ~30
  selection/axis parameters forever — precisely the drift being avoided.
- Matches plotms: `plotms(vis=..., plotfile='x.png')`.
- `hidden_args` handles export-only parameters that would clutter `inp()`.

### 2.2 Grid mode is one configuration plus an iteration axis

Grid mode has "one plot configuration panel controlling all plots," which is
plotms's `iteraxis`/`gridrows`/`gridcols` model, not per-cell configuration.
The constructor needs roughly `iteraxis`, `gridrows`, `gridcols`, `iterrange` —
everything else stays the existing flat scalars, which keeps the CASA task
validator happy (no nested structures).

**Known gap, unrelated to export:** the GUI supports per-slot raster↔scatter
kind switching (P-5b), but the constructor only has
`layout="one"|"side"|"over"` with slot A hardcoded raster and slot B scatter.
Two rasters side by side is reachable in the GUI and unreachable from the API.
This needs fixing regardless and is the natural place to start making the API
more expressive.

For genuinely heterogeneous panels beyond that, keep a documented `panels=`
escape hatch (list of dicts of primitives — still tput-safe).

### 2.3 Duo mode and M×N grid are the same call

The compositor takes `(nrows, ncols)` plus an ordered panel list. Callers
translate their own layout vocabulary *before* calling:

| Source | → |
|---|---|
| `layout="one"` | (1,1) |
| `layout="side"` | (1,2) |
| `layout="over"` | (2,1) |
| `gridrows=M, gridcols=N` | (M,N) |
| display mode raster-only / scatter-only | (1,1), one panel |

No layout strings cross the wire from the GUI: JS translates its radio-button
index to rows/cols before sending, keeping that vocabulary in one place.

### 2.4 Empty cells are blank but framed

A grid position with no data still gets its axes, title, and a note. Reflowing
to fill gaps would make cell position meaningless across a sequence of exported
files, which matters when flipping through `out_ant00.png … out_ant42.png`.
Revisit only if users ask.

Three distinct causes must stay distinguishable:
- selection genuinely has no rows (normal),
- everything flagged (normal),
- the query or shade raised (**a failure — must not be silently swallowed by a
  pipeline emitting 43 PNGs**).

`VisibilityScatter._layer_skip_reason` already distinguishes these. `_render` in
the raster collapses deferred / sub-2-cell / all-NaN / zero-width into one
`_degenerate` boolean; `_panel_spec()` currently re-derives the cause from
`self._agg`. **Recommended follow-up:** have `_render` record which condition
tripped, rather than inferring it later.

### 2.5 Flag overlay is out of scope for export

Keeps `FlagDB` out of the headless path entirely, so "headless" means something
clean.

### 2.6 Export must reflect GUI state, not pending widget values

A user can change a sidebar dropdown without pressing Plot. Export renders
**what is on screen**. Read Python-side render state; never widget values.

---

## 3. The export seam

### 3.1 `panel_spec.py` (new)

```
ColorBand    — one value→colour mapping (raster: 1; scatter: 1 per layer)
PanelSpec    — everything about a panel except its pixels
RenderedPanel— PanelSpec + image + optional viewport
```

`_state_data()` is now **derived from** `PanelSpec.to_state_data()` rather than
assembled alongside it. This was the one structural change worth insisting on: a
field added for the browser becomes visible to the exporter automatically, and
neither can drift without the other noticing. `_state_data_extra()` is gone,
replaced by an abstract `_panel_spec()`.

`PanelSpec` is deliberately a *superset* of `_state_data()`: it also carries band
cmaps, labels, `status`, `note`, and mappings, which the JS chrome has never
needed. `to_state_data()` emits only the historical key set, including the
raster/scatter asymmetry (unprefixed `scaling` vs `layer_scaling_{i}`) that
`flag_tool.ts` and the CustomJS callbacks depend on.

**Verification:** 192 raster cases × 6 scatter cases comparing old assembly
against new derivation — dicts identical in every case.

### 3.2 Cost discipline

`_panel_spec()` runs on **every** `_state_source` push, so it must be cheap —
plain attribute reads only. Building a `ScalarMapping` costs a histogram plus
~512 interpolation samples: negligible beside a shade, far too much per push.
Mappings are therefore attached in `render_result()` via
`_bands_with_mappings()`, not in `_panel_spec()`.

### 3.3 `_shade_for_export(viewport)`

Re-shades from cached state; **never re-queries the backend**. This is what makes
a GUI export instant and guarantees it shows what the user is looking at.

`VisibilityScatter._shade_for_export` **snapshots and restores**
`_layer_aggs`/`_layer_skip_reason`. `_shade_all_layers` mutates both, and
`_handle_probe` indexes those aggs with browser-viewport coordinates — exporting
at any other viewport would silently corrupt the next hover. This is probe
defect (2) arriving by a new route.

---

## 4. `tick_format.py` (new) — two-runtime parity

With no Bokeh server, tick labels are produced in the browser by a
`CustomJSTickFormatter`; the export must produce the same strings from Python.
Neither can be derived from the other. What *can* be done:

- `_JS_CORE` is the **single copy** of the algorithm as JavaScript.
- `TICK_FORMATTER_JS` wraps it with Bokeh's `_state_source` lookups.
- The test harness wraps the **same string** in a bare function and runs it under
  `node` against `GOLDEN_CASES`.
- `test_visibility_plot_uses_the_shared_string` greps the source to catch anyone
  inlining a copy back into `_build()`, which would let the shipped formatter
  drift while parity tests kept passing.

### 4.1 `toFixed` semantics are not portable

JS `toFixed` operates on `|x|` and rounds ties **away from zero**; Python's
`format(x, '.Nf')` rounds **half-to-even**:

| value | JS | Python |
|---|---|---|
| `(2.5).toFixed(0)` | `"3"` | `"2"` |
| `(0.25).toFixed(1)` | `"0.3"` | `"0.2"` |
| `(0.125).toFixed(2)` | `"0.13"` | `"0.12"` |

Python matches JS (the browser is what users see), via `_to_fixed()` using
`Decimal` on the float's exact binary value so near-ties like `1.005` (really
`1.00499…`) are correctly not treated as ties. A guard test asserts `_to_fixed`
**differs** from the f-string, so a future "simplification" fails loudly.

### 4.2 Behaviour changes to existing GUI labels

Both change what users already see:

1. **`1m 60s` fixed.** `Math.round(|elapsed| % 60)` could round the remainder to
   a full minute with nowhere to carry; 119.6 s rendered as `1m 60s`. Now rounds
   to whole seconds before splitting.
2. **Trailing zeros trimmed** on non-time axes: `0.0000` → `0`, `10.0000` → `10`.
   Guarded so a non-zero value never collapses to `"0"` — it falls back to nine
   decimals (`1.2e-6` → `0.0000012`).

Not changed, deliberately: no hour rollover, so 3600 s is `60m 00s`. Changing it
would alter existing labels and should be a decision, not a drive-by.

**Verification:** 2243 fuzzed cases under node, weighted toward tie values and
the trim/fallback zones — JS and Python identical.

---

## 5. `colormap_scaling.ScalarMapping` (new)

`apply_explicit_scaling` and `equalize_histogram` transform an array and discard
the curve. A colorbar needs the curve itself. `ScalarMapping` captures it as a
monotonic LUT with `forward`/`inverse`/`ticks`.

One LUT covers every scaling including `eq_hist`, built by **sampling the shade's
own curve** rather than duplicating the math — so a new entry in
`_SCALING_FUNCS` is automatically supported. Strict monotonicity is enforced by
dropping CDF plateaus (value ranges with no samples), which would otherwise make
the inverse ambiguous.

Directly usable as `matplotlib.colors.FuncNorm((forward, inverse))`, which is
what gives an exported colorbar correctly-placed ticks in data units under a
non-linear scaling.

**Verification:** all seven scalings strictly monotonic; round-trip error ≤ 9e-14;
`FuncNorm` accepts it; `reference=` correctly makes global and local differ.

---

## 6. `png_export.py` (new) — the compositor

### 6.1 Pixel fidelity

```python
ax.imshow(rgba, extent=(x0, x1, y0, y1),
          origin="lower", aspect="auto", interpolation="nearest")
```

Three of those are load-bearing:
- **`origin="lower"`** — Bokeh's `image_rgba` puts array row 0 at the bottom;
  matplotlib defaults to the opposite. Getting this wrong mirrors every plot
  vertically, which on a time axis is very easy not to notice.
- **`aspect="auto"`** — time vs channel, amplitude vs uvdist; a 1:1 data aspect
  would be meaningless.
- **`interpolation="nearest"`** — the array is already at final resolution;
  resampling would reintroduce the question `_resample_method` exists to answer.

**Layout is explicit per-cell pixel rects, not GridSpec.** GridSpec's
`wspace`/`hspace` are ratios of average axes width, which makes exact pixel
control awkward. Verified via `ax.get_window_extent()` across 3 panel sizes × 4
dpi values: the data area is exactly the agg's pixel dimensions in every case.

**Chrome margins are in points; the data area is in pixels.** So raising dpi
renders text with more pixels and leaves the Datashader image at 1:1. An earlier
pixel-based version had this backwards (output size constant, fonts shrinking).

**Two measurement traps, both encountered:**
- A pixel-counting check of the data area comes up one row and one column short,
  because the **axes spine overdraws the boundary pixels**. That is a frame drawn
  over the image, not data loss. *Assert on the bbox, not on pixel counts.*
- Do not "correct" the shortfall by padding the axes rect. A 900.5-pixel-wide
  axes forces a resample of a 900-pixel image — the exact thing the layout exists
  to prevent. (This mistake was made and reverted.)

### 6.2 Two-pass extent inheritance

Pass one collects extents; pass two draws. Empty cells inherit so axes stay put
across a sequence of exported files.

- Inheritance is **keyed on the `(x_label, y_label)` pair**, not global. Grid mode
  shares one configuration so it never matters there, but a duo can pair a raster
  against a scatter, and inheriting across them draws axes that are confidently
  mislabelled.
- An empty cell must **never** fall back to its own spec ranges: the degenerate
  branch leaves `(0.0, 1.0)`, which passes every "is this valid" test while being
  fiction. `0.0000`–`1.0000` ticks look like data.
- Inherited extents carry an inherited **tick origin**. Formatting an inherited
  MJD-seconds extent against the empty panel's own `(0.0, 1.0)` origin printed
  `4800001750.0000` instead of `29m 10s`.
- Only when *nothing* in the grid is drawable are ticks suppressed entirely.

### 6.3 Legends

Never drawn over data; space is reserved. matplotlib's default
`loc="upper right"` lands on exactly the corner an amplitude-vs-uvdistance
scatter tends to occupy, and no "find the empty corner" heuristic survives real
data.

`legend="auto" | "figure" | "panel" | "none"`. `"auto"` compares visible band
sets across populated cells: identical → one shared figure legend (grid mode's
single-configuration model guarantees this, and N identical legends are N−1 too
many); heterogeneous → per-panel, in a reserved band above each axes.

Hidden bands are omitted — an entry for a layer the user turned off claims
something is on the plot that is not.

### 6.4 Colorbars

**`colorbar` and `colorbar_side` are separate parameters.** Count and placement
are independent; fusing them into one enum makes some combinations inexpressible
(per-plot bars gathered at the figure edge) while inventing meaningless ones, and
puts `none` inside a position enum where it also has to mean global-off.

```
colorbar      = "auto" | "shared" | "each" | "none"
colorbar_side = "right" | "left" | "bottom"
```

`"auto"` resolution:
- **Raster** — shared when mappings agree (`color_mode="global"` means one
  reference distribution, hence one curve), one each when they do not
  (`color_mode="local"`: each panel equalizes to its own data).
- **Scatter** — **none**. A scatter ramp is points per pixel; two plotted
  quantities mean two bars per cell; in a grid that is a great deal of chrome for
  a number the legend already reports.

Sameness for density bands additionally requires **matching bin area**.
`_compute_canvas_size` is adaptive and shrinks the canvas for sparse layers by
more than an order of magnitude, so "count = 10" can mean different things in two
cells of the same grid. Note this is **orthogonal to `color_mode`**: global vs
local governs zoom *within* a panel, not comparability *across* panels.

Forcing `"shared"` on divergent mappings **logs and falls back to `"each"`**. A
quietly wrong colorbar in a paper is worse than an unexpected layout.

Bars are drawn into **reserved space as their own axes**, not via
`make_axes_locatable`, which steals width from the parent axes and would shrink
the data area below 1:1.

### 6.5 Labelling — two misreadings actively prevented

`ColorBand.kind` is `"value"` (raster) or `"density"` (scatter).

- **Quantity.** Labelling a scatter bar `Amplitude XX` — the layer label, which
  the *legend* correctly uses — states the ramp is an amplitude scale when it is
  a density scale. `bar_label()` returns `Density (eq_hist)` for density bands.
- **Scaling.** Every reader's prior, from plotms and the CASA viewer, is a linear
  ramp. Under `eq_hist` ticks bunch where data is dense — useful information, but
  only to a reader who knows to expect it. The scaling is named in the label.

`legend_label()` folds peak density into the scatter legend entry:
`Amplitude XX  (≤337 pts/px)`. One number answers "how overplotted is this?",
survives grid mode without scaling, and is why scatter colorbars default off.

### 6.6 Provenance footer

A PNG has no status bar and no sidebar. Derive the footer from
`VisibilityPlotter._status_text()` so the two summaries cannot diverge —
but `_plain_text()` must flatten it first: `_status_text()` is written for a
Bokeh `Div` and arrives as `<b>…</b>` / `<br>` markup, which matplotlib draws
literally.

---

## 7. Defects found and fixed

| # | Defect | Where | Notes |
|---|--------|-------|-------|
| 1 | `_img_to_uint32` PIL branch wrote **B,G,R,A** — R/B transposed vs what `image_rgba` consumes | `visibility_plot.py` | Test-only path, so nothing user-visible was wrong, but a test was pinning the bug in place and the docstring described the wrong layout |
| 2 | `_layers = list(layers)` in `update_axes` skipped cmap defaulting | `visibility_scatter.py` | `_handle_update_axes_scatter` builds `ScatterLayer` without `cmap` (the browser has no reason to send one) → `tf.shade(cmap=None)` → **panel blanks after any sidebar axis/layer change**. Fixed by `_with_default_cmaps()` applied wherever `_layers` is set |
| 3 | Probe answers were bare `{"label": html}` | both plots | Tests could only assert on markup; two label-format changes broke them. Now `{"label", "probe"}` with `status` / per-layer records |
| 4 | ~~Raster `_do_viewport_rerender` never stored its viewport~~ | `visibility_raster.py` | **FIXED 2026-08-17** — §8.22 |
| 4a | ~~`test_defer_is_much_faster_than_real_render`~~ **DONE 2026-08-17** §8.20 | `test_visibility_scatter.py` | Real construction is no longer query-dominated (MSv2 real ~0.216s, deferred ~0.096s), so fixed Bokeh/comm setup cost fails the `<real/10` arm. Replace with a backend call-counter; `test_defer_leaves_all_layer_dfs_none` already covers the intent structurally |
| 5 | Cell titles clipped off the top edge | `png_export.py` | Title band reserved in figure height but not offset in layout |
| 6 | Footer rendered raw HTML | `png_export.py` | Found only by exporting real data |
| 7 | Scatter density bars appeared under `colorbar="auto"` in a mixed duo | `png_export.py` | "Are *all* bands density?" fails when a raster is present. Density bands now filtered out of the auto path entirely |
| 8 | Panel legend collided with the cell title | `png_export.py` | Both wanted the band immediately above the axes |
| 9 | `SelectionSpec.spw` populated but never read by either backend | `msv2_backend.py`, `msv4_backend.py` | **Silent data-selection failure**: `spw='0'` plotted all SPWs. See §8.4a |
| 10 | Probe `Time:` prints raw MJD seconds while the axis prints elapsed | `visibility_plot.py` | §8.4d.2 — **fixed** 2026-08-14 |
| 11 | Probe `Channel:` duplicates and mislabels `Freq:` | `visibility_plot.py` | §8.4d.1 — open, folded into `axis_info` |
| 12 | Probe frequency range collapses at `:.6g` | `visibility_plot.py` | §8.4d.3 — **fixed** 2026-08-14 |

### 7.1 The empirical fact worth recording

**Datashader emits RGBA in memory order** (numerically `0xAABBGGRR` on
little-endian). Verified by shading `cmap=["#FF0000", "#0000FF"]`, which yields
bytes `[255, 0, 0, 255]` and `[0, 0, 255, 255]`.

Consequences: `RenderedPanel.rgba()` needs **no channel permutation**; reason
about this in **memory order, not numerically** (the same bytes read as
`0xRRGGBBAA` big-endian, so a hex assertion is an endianness trap).

### 7.2 Test coverage gaps noticed

- `TestProbe` in `test_visibility_scatter.py` uses `_make_single_layer`, so the
  **entire probe test class exercises only one layer**. Defect (1) of the
  PB-series — multi-layer consultation, the bulk of the 47.7% false-empty rate —
  has no coverage. A `TestProbeMultiLayer` class would close it and un-skip
  `test_probe_hidden_layer_reports_no_value`.
- An unreproducible `test_visibility_raster.py` failure was observed once. If it
  recurs, capture the traceback; deterministic causes would repeat, so genuine
  flakiness points at ordering between test classes sharing a backend.

---

## 8. Open: CHANNEL vs FREQUENCY (highest priority)

**This is the only outstanding item that produces a *wrong* plot rather than a
missing feature.**

### 8.1 The defect

`_axis_to_dim` maps **both** `Axis.FREQUENCY` and `Axis.CHANNEL` to the dimension
`"frequency"` (both MSv2 and MSv4 backends). `query_raster` then takes its extent
from `ds.coords["frequency"]` — actual Hz values. The channel *index*
`_compute_axis_values(Axis.CHANNEL)` carefully constructs (`np.arange(n)`,
`units=""`) is never consulted on that path.

Result: an axis **labelled "Channel" with ticks at 372.55–372.76 GHz**. sis14 has
48 channels; `0`–`47` would need no tick formatting work at all. The picture is
correct and the label is wrong, which is the worst combination — the plot looks
fine.

### 8.2 Why the backends do this

MSv4 partitions are per-SPW by default (`DATA_DESC_ID`), each carrying its own
`frequency` coordinate and fully self-describing. Channel index is unique **per
partition, not across them** — sis14 has four SPWs, so there are four channels
numbered 5. `query_raster` concatenates partitions, at which point the index
stops being a usable coordinate. Frequency has no such problem: globally unique,
monotonic, and it orders partitions correctly.

**There is no global channel index available from `xarray-ms`**, and MSv4
deliberately has no notion of a global spectral axis (SPWs may differ in channel
count, width, and may overlap). A synthetic global index would be a visplot
invention that breaks when `partition_schema` or a selection changes.

### 8.3 Flagging does not need a global index

Worth recording before it drives a design decision. To flag, you need plot
coordinates → `(partition, local channel index)`. **Frequency inverts to that
exactly**: each channel centre belongs to exactly one partition, and finding it
is a lookup in that partition's `frequency` coordinate. `_cell_bounds` already
gives the tolerance.

A synthetic global index would have to be inverted through the same lookup
anyway, adding a layer that can desynchronise. And `FlagDB` already stores
**coordinate-range deltas**, not index ranges: a frequency range survives a
repartition, a reordering, and an SPW subselection; a global index survives none
of them.

### 8.4 Resolution

Rejected: `Channel (GHz)` — channels are not measured in GHz; it reads as a units
error to exactly the audience most likely to notice, and hides that the axis has
silently stopped being channel-indexed.

**Adopted rule, selection-dependent:**

| Condition | Values | Axis label |
|---|---|---|
| One partition after selection | local index `0…N−1` | `Channel` |
| Multiple partitions | `frequency` coordinate | `Frequency [GHz]` |

Single-SPW is the common case when channel numbers actually matter (bandpass
structure, chasing an RFI spike), so the useful axis appears exactly when wanted.

**"One SPW" must mean one *partition after selection*, not
`len(selection.spw) == 1`.** A non-default `partition_schema` splitting by scan
or field yields several partitions within one SPW; their frequency coordinates
would be identical, which is fine for concat but means the count has to come from
the partition iteration, not the selection spec.

### 8.4a SPW selection existed but was silently ignored — FIXED 2026-08-14

**Correction to an earlier draft of this section**, which claimed
`SelectionSpec` had no `spw` field. It does. The earlier claim came from
grepping which `sel.` attributes the *backends* read, which shows only what they
consume — a bad way to establish what exists.

What was actually true, and worse:

| Layer | State |
|---|---|
| `VisibilityPlotter.__init__` | accepts `spw: str = ""` ✔ |
| `_parse_spw_string` | converts to `list[int]` ✔ |
| `_build_selection` | populates `SelectionSpec.spw` ✔ |
| `SelectionSpec.spw` | `Optional[list[int]]`, in `is_empty()` and `copy()` ✔ |
| Plot button (`msg["spw"]` → `_spw_str`) | replot path wired ✔ |
| **MSv2 / MSv4 backends** | **`sel.spw` read nowhere** ✘ |

So `visplot(ms=..., spw='0')` plotted every SPW, with no error and no warning —
a silent data-selection failure, a more serious category than the mislabelled
axis. The parameter looked implemented at every layer except the last.

**Fix.** `_iter_visibility_partitions(selection=None)` in both backends now skips
partitions whose SPW is not selected, via two small helpers:

- `_partition_spw_id(ds)` — MSv4 reads `ds.attrs["spectral_window_id"]`; MSv2
  tries `spectral_window_id` then `DATA_DESC_ID`. **This asymmetry is
  deliberate**: each mirrors its own backend's `metadata()` collection. Making
  them agree would risk a filter that matches ids `metadata()` never reports, so
  the sidebar would not offer them.
- `_spw_selected(ds, selection)` — a partition declaring **no** SPW id is
  **kept**. Refusing to plot because a store omits an optional attribute is a
  worse failure than plotting slightly more than asked; `metadata()` has the same
  tolerance.

Filtering happens at partition iteration, not in `_apply_selection`, because SPW
is a *partition* property (MSv4 partitions per-SPW by default; MSv2's default
schema is `["DATA_DESC_ID", "OBSERVATION_ID"]`) — skipping avoids reading the
partition at all.

Call sites split cleanly: `metadata()` and MSv2's `open()` must see the whole
store and pass no selection; the other five per backend (`query_columns`,
`query_raster`, `query_uv_coverage`, `samples_per_pixel`, `probe_raster_pixel`)
already had `selection` in scope.

An SPW selection matching **no** partitions logs a warning. Callers handle "no
data" gracefully, but silence would be indistinguishable from an empty range.

**Consequence for §8.4:** the single-partition branch of the CHANNEL rule is now
reachable. `axis_info` is unblocked.

### 8.4c Other constructor parameters that do nothing

`antenna`, `scan`, `timerange`, and `uvrange` are stored on `self._*_str` and
rendered through `_stub_input` in the sidebar — explicit, honestly-marked
placeholders, not silent drops. `_build_selection` does not consume them.
Distinct from the `spw` case above, where every layer *looked* implemented.
Worth listing in the preview spec as known-unimplemented so users are not
surprised.

### 8.4b Related: `channel_range` is per-partition

`channel_range` applies `isel(frequency=slice(...))` **inside each partition**.
With four SPWs, `channel_range=(0, 10)` selects the first ten channels of *each*
SPW and concatenates four disjoint frequency bands onto one axis.

This is arguably correct — it matches plotms's `spw='*:0~10'` semantics — but
combined with the §8.1 label defect it is genuinely confusing: the user asks for
channels, gets an axis labelled "Channel" showing frequency, spanning four bands
with gaps and no visual indication the gaps exist. (Gapped frequency axes are
presumably related to the earlier cell-bounds defect where a global-average
formula inflated integration windows on gapped axes.)

Once SPW selection exists, `channel_range` against a single SPW is unambiguous —
a further argument for doing it first.

### 8.4d The status bar disagrees with the axis (observed 2026-08-14)

A live probe readout, with the raster's y axis reading `6m 20s` at the same time:

```
Amplitude: 11.4412  |  Channel: 3.72764e+11  |  Time: 1.35331e+09  |
Freq: 372.764–372.764 GHz  |  Field: J0522-364  |  Scan: 4
```

Three defects, all the same family as §8.1 — coordinate rendering computed in
several places with nothing forcing agreement.

**1. `Channel:` duplicates `Freq:` and mislabels it.** `_format_probe` prints
`x_centre` raw under `self._x_dim.label`, then separately prints
`info["freq_range_ghz"]` with a unit. The backend already returns the frequency
in GHz, so unit-aware rendering partly exists — hardcoded for one field. Once
`axis_info` lands, `x_centre`/`y_centre` route through it and the `Freq:` special
case folds into the general path instead of sitting beside it.

**2. `Time:` disagrees with the y axis.** The axis renders elapsed time via
`tick_format`; the probe prints raw MJD seconds for the same coordinate. Fix is
small and independent of `axis_info`:

```python
format_tick(yc, self._y_dim == Axis.TIME, self._y_range[0])
```

reusing the function the axis and the export already share, so all three agree by
construction rather than by discipline.

**3. `Freq: 372.764–372.764 GHz` is a collapsed range.** `:.6g` cannot resolve
one channel width — sis14's ~15.6 MHz on a 372 GHz centre is the seventh digit.
Either widen precision for that field or print a single value when the endpoints
render identically; a range whose ends are equal reads as a bug even when the
numbers are fine. **Check against `_cell_bounds` first**: if the endpoints are
genuinely equal rather than merely rounding the same, it is the degenerate
cell-bounds path and a different problem.

**Scope consequence.** `_format_probe` should become the single place a
coordinate is rendered — value, unit, and elapsed-time handling — for every axis,
rather than one general branch plus a frequency special case plus an axis
formatter that disagrees with both. `axis_info` supplies the label and unit it
needs.

### 8.5 API — IMPLEMENTED 2026-08-14 (backend half)

`Axis` **already carries `.unit`** (`TIME: "s"`, `FREQUENCY: "Hz"`,
`UVDIST: "m"`, `CHANNEL: ""`). The unit table planned for `reader.py` was
therefore unnecessary — `AxisInfo` only needs to supply what is
*selection-dependent*: which axis actually got plotted.

**`AxisInfo`** (frozen dataclass, in `axes.py` — pure, no reader dependency):

| Field | Meaning |
|---|---|
| `axis` | the axis whose values are on the plot; `label`/`unit` read from here |
| `requested` | what the caller asked for; equal to `axis` in the normal case |
| `dim` | dimension the values live along |
| `is_index` | positional index rather than physical quantity — takes no unit |
| `note` | why a substitution happened, for `PanelSpec.note` and the status bar |

Constructors `AxisInfo.direct()` / `AxisInfo.substituted()`, plus
`display_label(unit_override=None)` — the override is where the SI prefix lands
(`Frequency [GHz]`), because `CustomJSTickFormatter` runs per tick and has no
slot for a shared multiplier annotation.

**Key property:** label and unit are read from `axis`, never from `requested`.
A backend that substitutes *cannot* fail to relabel — the divergence is
unrepresentable rather than merely discouraged.

**`XArrayReader.axis_info(axis, selection)`** — concrete default returns the axis
unchanged with `is_index` set for `CHANNEL`/`ROW`. Both backends override for
`Axis.CHANNEL`: one partition after selection → `AxisInfo.direct(CHANNEL,
is_index=True)`; more → `AxisInfo.substituted(CHANNEL, FREQUENCY, note=...)`.

Partition count comes from `_iter_visibility_partitions(selection)`, **not**
`len(selection.spw)` — a non-default partition schema splitting by scan or field
yields several partitions within one SPW with identical frequency coordinates.

Verified against fakes for both backends: CHANNEL resolves to an index axis at 1
partition and substitutes to FREQUENCY at 4; FREQUENCY and TIME pass through
unchanged; the substituted case carries a note naming the fix ("Select a single
spw to plot channel number").

**Remaining (front-end half):** replace `_axis_label()` with `AxisInfo` at its
eight call sites — `visibility_plot._build`, `visibility_raster._panel_spec`,
`visibility_scatter._panel_spec`, and two pairs in `visibility_plotter`'s j2p
responses (lines ~1650, ~1730), which currently recompute labels independently of
the panel and are exactly the divergence this fixes. Panels should hold
`_x_info`/`_y_info` resolved at selection-change time rather than calling
`axis_info` from `_panel_spec()`, which runs on every state push.

### 8.6 Front-end half — IMPLEMENTED 2026-08-14

**Resolution point.** `_refresh_axis_info(selection)` runs at the top of each
subclass's `_render()` — the one place that knows both the current axes and the
current selection, and which all three axis-mutation sites already funnel
through. Resolving there rather than in `_panel_spec()` matters: the backend
counts partitions to decide whether `Axis.CHANNEL` is unique, and
`_panel_spec()` runs on every `_state_source` push. Degrades to
`AxisInfo.direct` when the backend has no `axis_info` (a remote reduction
context satisfies the reader protocol structurally and may not be updated); the
fallback loses only the substitution, which was the old behaviour anyway.

Panels expose `x_label` / `y_label` properties and `axis_notes()`. Figure
construction and both `_panel_spec()`s read them. `_axis_label()` survives only
for callers with no selection context (dropdown option text) and carries a
DEPRECATED note explaining why a bare `Axis` cannot label a rendered plot.

**`_format_coord` — one place a coordinate becomes text.** Three branches, in
order:

1. **Time** → `tick_format.format_tick`, the same function the axis formatter and
   the matplotlib export use. Fixes §8.4d.2 by construction rather than by
   discipline.
2. **Dimensioned** → `tick_format.si_scale`, giving `372.764 GHz` rather than
   `3.72764e+11`.
3. **Index / dimensionless** → plain.

`si_scale` is **Python-only and deliberately so**: the probe renders to HTML
server-side, so unlike `format_tick` it has no JS counterpart and needs no parity
harness. Axis *tick* text is what must match the browser; a status-bar readout is
not. It refuses to prefix compound (`m/s`) or already-dimensionless units, and
TIME branches away before it is reached — otherwise MJD seconds would render as
`1.3533 Gs`.

The redundant `Freq:` field is suppressed when either axis already reports
frequency, and retained when neither does (on a time-vs-baseline raster the
cell's frequency span is genuinely extra information). Precision widened to
`:.9g`, collapsing to a single value when endpoints are equal (§8.4d.3).

**Result** — observed readout before, and after:

```
before: Amplitude: 11.4412 | Channel: 3.72764e+11 | Time: 1.35331e+09 |
        Freq: 372.764-372.764 GHz | Field: J0522-364 | Scan: 4
after:  Amplitude: 11.4412 | Frequency: 372.764 GHz | Time: 6m 20s |
        Field: J0522-364 | Scan: 4
single: Amplitude: 11.4412 | Channel: 23 | Time: 6m 20s |
   SPW  Freq: 372.764 GHz | Field: J0522-364 | Scan: 4
```

**Note on relative import depth.** `axes.py` sits beside `visibility_plot.py` in
`cubevis/toolbox/visplot/`, so `from .axes import ...`. The backends and
`reader.py` are one level deeper and correctly use `..axes`. Copying the deeper
form into `visibility_plot.py` broke it; the same file already had
`from .axes import Axis` a few lines away, which was the answer.

### 8.7 Three silent failures found wiring it up (2026-08-14)

Each individually looked like success. Recorded because the *shapes* recur.

**1. The adapter did not forward the new method.**
`LocalVisibilityReader` deliberately narrows `XArrayReader` to the display
protocol, so every method the widgets need must be added there explicitly.
`axis_info` went onto the backends and not onto the adapter, and
`_refresh_axis_info`'s `getattr(backend, "axis_info", None)` guard quietly
produced the pre-AxisInfo behaviour. **The guard now logs once per reader
class, naming the consequence.** It exists for the future
`RemoteReductionContext`; a fallback indistinguishable from success is worse
than no fallback.

*Diagnostic that found it:* `type(panel._backend)` was the adapter, not the
backend — `panel._backend._backend` was `MSv2Backend`.

**2. `_axis_to_dim` has different arity in the two backends.**
MSv2's takes the axis alone; MSv4's also takes a baseline dim. Identical code
was inserted into both, so on MSv2 the `self._baseline_dim` lookup raised
`AttributeError` — swallowed by an over-broad `except (ValueError,
AttributeError)` into `dim=""`. The `except` is now `ValueError` only (a derived
axis genuinely has no dimension); a real mistake raises. **The backends have
diverged before** — this is the same lesson that hoisted the probe-geometry
helpers into `reader.py`.

**3. Resolved labels were never pushed to the live figure.**
`_build()` sets `x_axis_label` once from the seeded direct resolution, before the
backend is consulted; `_refresh_axis_info` updated only the Python object. The
figure title would have kept reading "Channel" while the status bar correctly
read "Frequency". `_sync_axis_labels()` now runs after every refresh, on both the
success and fallback paths.

**4. (Follow-on) Panel titles composed from bare `Axis` members.**
`_auto_title` and `VisibilityScatter._effective_title` used `_x_dim.label`, so a
title read `[Time vs Channel]` over an axis labelled `Frequency [Hz]` — the same
divergence one layer up. Both now take resolved names from `_x_info`/`_y_info`.
Bare names without unit suffix: `[Time vs Frequency]` reads better than
`[Time [s] vs Frequency [Hz]]`, and the axis labels carry units already.

**Rule that falls out:** in plot code, `Axis.label` is never the right source for
displayed text. Only `AxisInfo.label` knows what was plotted. `_axis_label()`
survives for dropdown option text, where there is no rendered axis to describe.

### 8.8 Channel index: kept, guarded, and qualified (2026-08-14)

**Question raised:** is a per-SPW channel index a useful thing to show, or an
internal index leading users down a blind alley?

**Answer: it is CASA's public selection key, not an internal index.** An RFI
spike found in the plot is acted on with `flagdata(vis=..., spw='0:137~139')`
or `split(vis=..., spw='0:10~50')` — syntax that takes channel numbers. Without
a channel axis the user converts by hand: divide by channel width, mind the
reference channel, hope the SPW is not reversed. That arithmetic is exactly what
produces off-by-one flagging errors.

Three more genuinely channel-domain uses: edge channels ("the first and last 5
are always bad" is a count, identical across SPWs regardless of their frequency
coverage); bandpass structure compared across SPWs or executions; and plotms
parity (`xaxis='channel'` is standard, and this is a plotms replacement).

**Where the concern was right:** the number is meaningless without its SPW.
"Channel 137" is ambiguous across four; `spw='0:137'` is actionable. Addressed by
`AxisInfo.context`, which appends a qualifier to the label — `Channel (spw 0)` —
so the number is never orphaned from the scope it indexes into. The status bar
already shows both: `Channel: 23 | Freq: 372.764 GHz`, which is better than
plotms manages and removes the blind-alley risk entirely.

`context` is omitted when a partition declares no SPW id, rather than emitting an
orphaned qualifier.

**Latent defect this caught, before it was tested.** `axis_info` decides the
*label*; `_axis_to_dim` still maps `Axis.CHANNEL` to the `"frequency"` dimension
and `query_raster` still takes its extent from `ds.coords["frequency"]`. The
single-partition branch would therefore have labelled the axis `Channel` while
the ticks carried Hz — **the original §8.1 defect, reasserted more
confidently** — whereas the multi-SPW path (already verified in the GUI) is
correct.

**Guard:** `_SUPPORTS_CHANNEL_INDEX = False` on both backends. The
single-partition branch checks it and otherwise substitutes to FREQUENCY with a
distinct note ("channel index is not yet plotted by this backend"). Flipping the
flag is the only change needed in `axis_info` once the values follow.

Verified across all five combinations on both backends: 4-spw substitutes
regardless; 1-spw substitutes while unsupported; 1-spw supported gives
`Channel (spw 2)` with `is_index=True` and no note; 1-spw with no id gives bare
`Channel`.

**To implement properly** (its own change, with its own tests): `query_raster`
assigns `np.arange(n_chan)` as the x coordinate when `x_dim is Axis.CHANNEL` and
exactly one partition survives selection. Touches the concat path, the extent
computation, and `probe_raster_pixel`'s inverse mapping, in both backends. Then
flagging needs an index→frequency conversion before reaching `FlagDB`, which
stores coordinate ranges — trivial to invert since the index is positional within
the one selected partition, but another place two representations must agree.

### 8.9 Correction: count distinct SPWs, not partitions (2026-08-16)

`axis_info` counted **partitions** to decide whether a channel index was
unambiguous. Wrong test, and wrong in the direction that suppresses a working
feature.

Observed on `sis14_twhya_calibrated_flagged.ms`: the diagnostic reported
`n_parts: 4`, but the SPW control offers only SPW 0 and the raster spans one
contiguous ~230 MHz band with no gaps. That is **one spectral window carried by
four partitions** — MSv2's default schema is
`["DATA_DESC_ID", "OBSERVATION_ID"]`, and a multi-field MS then yields several
partitions per SPW.

Channel numbering is a property of the *spectral window*: partitions sharing an
SPW share its frequency coordinate exactly, so their channel numbering is
identical. Several partitions carrying one SPW is completely unambiguous.

**Criterion is now the count of distinct SPW ids among surviving partitions.**
This hazard was written into §8.4 when the rule was designed ("one SPW may span
several partitions when the schema splits by scan or field") and then implemented
as a naive partition count anyway — a design note is not a test.

A third case fell out: **zero declared SPW ids**. Uniqueness is then unknowable,
so `axis_info` substitutes to frequency with its own note rather than claiming an
index that might span several windows. Note this is the opposite tolerance from
`_spw_selected`, where an undeclared partition is *kept* — in both places the
conservative choice is the one that avoids asserting something unverified, but
"conservative" points in different directions.

Verified on both backends across six shapes: 4 partitions of 1 SPW →
`Channel (spw 0)`; 1 partition of 1 SPW → `Channel (spw 2)`; 4 partitions of 4
SPWs, 6 of 2 SPWs, and 2 with no ids → all substitute to `Frequency [Hz]` with
distinguishable notes.

**Also clarified: `len(dt.groups)` is not the SPW count.** `DataTree.groups`
returns the path of every node — root, partitions, and subtable nodes beneath
them — so 13 is a node count. The authoritative source is
`metadata()["spw_ids"]`, or
`{_partition_spw_id(ds) for ds in _iter_visibility_partitions(None)}`.

**Repartitioning does not threaten the channel index.** Channel numbering derives
from the `SPECTRAL_WINDOW` frequency axis; partition schemas split *rows* — by
field, scan, observation — never channels. A channel index is therefore stable
under any partition schema, and MSv4 is no worse off than MSv2. The fragile
construct was only ever a *global* index across SPWs, which is why none is being
built.

### 8.5-old Proposed API (superseded)

The defect exists because values and label are produced in three places that
nothing forces to agree: `_compute_axis_values` builds values, `_axis_label`
builds the string, `_axis_to_dim` quietly decides which values get plotted.

Add to the `XArrayReader` ABC:

```python
def axis_info(self, axis: Axis, selection: SelectionSpec) -> AxisInfo:
    """Label, unit, and how values are resolved for *axis* under *selection*."""
```

returning e.g. `AxisInfo(label="Frequency", unit="Hz", dim="frequency",
is_index=False, fallback_from=Axis.CHANNEL)`. A backend falling back from channel
index to frequency **must** return the frequency label with it — the divergence
becomes unrepresentable rather than merely discouraged.

The `selection` argument is what lets the answer differ between "one SPW, channel
index is fine" and "four SPWs, falling back to frequency" — a distinction the
current code cannot express.

Unit table lives in `reader.py`, **not** in each backend (same lesson as the
shared probe-geometry helpers, which were hoisted there to stop the backends
diverging). Backends override only where the dataset knows better — MSv4 can read
`ds.frequency.attrs["units"]` rather than assume Hz. Note
`_compute_axis_values` already sets `attrs["units"]` on most axes
(`UVDIST: "m"`, `UVDIST_LAMBDA: "λ"`, `VELOCITY: "m/s"`, `PHASE: "rad"`,
`CHANNEL: ""`); the information exists and is simply never surfaced.

### 8.6 Surfacing the fallback

The fallback must be visible, not left to be inferred from tick magnitudes:

- `PanelSpec.note` carries it for export.
- **Status bar / hover readout names the axis and gives the value with units**,
  e.g. `Frequency: 372.647 GHz`; in the single-SPW case simply `Channel: 23`.
  This routes through `_format_probe`, which currently reports `x_centre` /
  `y_centre` as bare numbers — it needs the same unit-aware formatting.

### 8.7 SI prefixes follow from this

Once the unit is available, `PanelSpec` gains `x_unit`/`y_unit`, `_state_source`
carries them for the JS, and the formatter can scale.

**Fold the prefix into the axis label, not onto each tick** — label reads
`Frequency [GHz]`, ticks read `372.55`, `372.60`. This matters for a
Bokeh-specific reason: `CustomJSTickFormatter` runs per tick and has no slot for
a shared offset or multiplier annotation, which is why matplotlib's `+3.7255e11`
corner notation has no clean GUI counterpart. Scaling the label is the one
approach that works identically in both runtimes, keeping the parity harness
meaningful.

Wrinkle: the prefix depends on the visible range, so it changes on zoom. The
label already lives in `_state_source` as `x_label`/`y_label`, so it is reachable,
but it becomes a second thing the browser mutates.

**Never infer a unit from magnitude alone.** `format_tick` receives a bare float;
a magnitude-triggered "GHz" would confidently mislabel a long baseline in metres
as gigametres.

Rotating x tick labels on collision remains a useful compositor-only fallback for
axes with no unit to scale by — matplotlib can measure label widths after a draw,
so it can be automatic rather than a parameter.

---

## 8.10 SPW selection UI (design agreed 2026-08-16, not yet built)

### The label bug that started it

The dropdown read **`0 selected`**. Ambiguous in the worst possible way: SPW 0
exists, so it reads as "SPW 0 is selected" — which is exactly what a user would
conclude from a plot showing SPW 0's data. A count that looks like an id.

`none selected` removes that but leaves a second oddity: nothing selected, yet
everything plotted. Correct behaviour (it matches CASA's `spw=''`) but it
describes widget state rather than what the user is getting.

The decisive argument sits directly above it in the same panel: the Field control
says **`All fields`**. Two controls, same panel, same semantics, and one of them
said "0 selected".

### Agreed design

**Widget state is always explicit; no two states share an outcome.**

* **Default: every box checked.** `spw=''` from the constructor maps to
  all-checked at widget-init. WYSIWYG — the boxes always show what is plotted.
  Bare `ms=`/`ps=` therefore plots everything, as it must.
* **Scrollable checklist**, not a dropdown — the ALMA 30-SPW case makes a dropdown
  unusable, and the dropdown is what forced the all/none ambiguity in the first
  place.
* **"All" / "None" buttons** at the top. These genuinely differ, because:
* **Plot with nothing checked → do not render.** Show "select at least one
  spectral window." This is what makes None distinct from All, and it costs
  nothing *here* because the GUI has an explicit **Plot** button: unchecking all
  is a transient state on the way to checking two, and the empty case is only
  reachable by deliberately pressing Plot. In a live-updating GUI this would be
  annoying; here it is free.

**Translation at the boundary.** Widget state stays explicit; `_build_selection`
collapses it:

```python
spw_ids = None if all_checked else checked_ids
```

All-checked emits `SelectionSpec.spw = None`, which skips `_spw_selected`
entirely rather than testing membership against every id, and keeps the selection
portable if a different MS is loaded. Display is WYSIWYG; internal representation
stays unconstrained-means-all.

**Show more than the id.** With an ASDM-imported MS the ids are non-contiguous —
0, 1, 17, 19, 21, 23 — because WVR and channel-average windows sit alongside the
science ones. The id alone does not help a user choose:

```
☑ SPW 0    372.5-372.8 GHz   384 ch
☑ SPW 1    374.4-374.6 GHz   128 ch
☑ SPW 17     7.5-7.6 GHz       1 ch     <- WVR
```

`metadata()` already collects `spw_ids`; whether it carries per-SPW frequency
range and channel count needs checking — if not, a small addition to the same
loop.

### SPW numbering facts

The id is the row index into the `SPECTRAL_WINDOW` subtable — **persisted in the
file, not ordinal or recomputed on open**. Stable for a given MS and it is what
`spw='0,2'` refers to. **Not** stable across `split`/`mstransform`, which
renumber. Routinely non-contiguous. This is why the *id* is shown rather than a
position: "the third checkbox" is meaningless, "spw 17" is what gets retyped.

### Also outstanding

`_status_text` should distinguish "showing all because unconstrained" from
"explicitly selected all". On a single-SPW dataset these coincide, so a bug there
will not show on sis14.

## 8.11 DATA_DESC_ID is not a spectral window id — FIXED 2026-08-16

`MSv2Backend._partition_spw_id` fell back to `DATA_DESC_ID` when
`spectral_window_id` was absent. **These are different quantities.**
`DATA_DESC_ID` indexes `DATA_DESCRIPTION`, which maps to a *(spectral window,
polarization setup)* pair. They coincide when there is a single setup — which is
why sis14 shows no symptom — but on a mixed-setup MS, DDID 3 can be SPW 5.

The number reaches the axis label (`Channel (spw 3)`) and the SPW control, and a
user retypes it as `spw='3'` into `flagdata`. It would select something else.
A wrong-data bug wearing a correct-looking label — the category this whole
session has been clearing.

**Fix:** `_partition_spw_ident(ds) -> (id, kind)` with *kind* in
`{"spw", "ddid", "none"}`. `_partition_spw_id` still returns the bare id, so
*filtering* is unchanged and self-consistent (`metadata()` collects through the
same fallback). Anything that *displays* the number branches on the kind:
`axis_info` now emits `Channel (ddid 3)` rather than claiming an spw.

Verified: `spectral_window_id` present → `Channel (spw 5)`; only `DATA_DESC_ID` →
`Channel (ddid 3)`; mixed or absent → substitutes to frequency.

**Still TODO:** resolve DDID through `DATA_DESCRIPTION` to a true spw id. Needs
subtable access the backend does not currently have. Until then, reporting
honestly is the correct behaviour, and the SPW checklist must use the same
`_partition_spw_ident` kind so its labels do not re-introduce the claim.

## 8.12 Headless panels — VisibilityPlot half done (2026-08-16)

`VisibilityPlot.__init__` gained `headless: bool = False`. `_build()` returns
immediately after `_state_source`, leaving `self._fig = None`.

**Why the cut is there.** `_render()` and `_state_source` are the substrate
`render_result()` reads — `_render` fills `_x_range`/`_y_range`/`_image_source`,
and the state dict derives from `_panel_spec()`. Both are pure Python. Everything
after builds browser chrome (figure, glyphs, tick formatters, hover, rerender
trigger, axes-changed handler, flag tools), none of which contributes a pixel to
the data area.

Verified by AST walk that the return precedes every chrome call:

```
L809  _render
L815  ColumnDataSource      (_state_source)
L830  RETURN (headless)
L833  figure
L849  _build_glyphs
L890  _add_hover_tool ... L895 _add_flag_tools
```

`_build_glyphs` is the only place the subclasses touch `self._fig`
(`visibility_raster.py:891`, `visibility_scatter.py:905`), and it runs after the
return, so a headless panel never dereferences it. `_sync_axis_labels()` already
guards on `_fig is None`.

`comm_mgr` was **already** independently optional (guards at
`visibility_plot.py:307-318` and `:335`), so a headless panel is comm-free
without further change — a nicer starting position than expected.

### Remaining: the VisibilityPlotter half

`__init__` is 523 lines (675–1198) with a clean seam at **line 828**:

* **675–826 — always runs.** Validate, store args, `open_ms`/`open_ps`,
  `_build_selection()`, resolve axes and presets.
* **828–832 — cheap, keep.** `FlagDB()`, hotkey scope uuid.
* **834–~940 — GUI.** `BokehAppContext`, `CommMgr`, control pipe and handler
  registration, `_cursor_source`, `_display_order_source`.
* **~950–1190 — panels + handler registration.**
* **1197 — `_build_layout()`.**

Split into `_resolve_config()` (always) and `_build_gui()` (gated). Headless
builds panels with `headless=True, comm_mgr=None, cursor_source=None` and returns
without the layout. `defer_initial_render` is the existing precedent for a gate
of this shape.

### 8.12a Extraction map for the VisibilityPlotter half

Decided 2026-08-16: **full split** (real method extraction, not inline gates) and
**render only what is needed** (headless builds only the panels it will export).

**The two PNG paths do not constrain this choice.** They converge *after*
construction: the CLI path is `_resolve_config()` -> build panels ->
`render_result()` -> `export_png()`; the GUI Export button operates on the
already-constructed live panels, supplying the browser's viewport. Neither
touches the other's construction, so the split is purely code organisation.

`__init__` spans 675-1198. Existing section banners give the boundaries:

| Lines | Section | Destination |
|---|---|---|
| 705-736 | Validate; Store arguments | `_resolve_config()` — always |
| 738-782 | Open data source (`open_ms`/`open_ps`, `_build_selection`) | `_resolve_config()` — always |
| 784-826 | Preset / explicit axes | `_resolve_config()` — always |
| 829-832 | `FlagDB()`, hotkey scope uuid | always (cheap; leave in `__init__`) |
| 835-909 | Communication infrastructure; Reconnection behaviour | `_build_comm()` — **gated** |
| 919, 937 | `_cursor_source`, `_display_order_source` | `_build_panels()` — always (plain `ColumnDataSource`s, browser-free, and the panels need them) |
| ~950-1080 | Four panel constructions; `_slots`; per-slot selection caches; `_slot_display_order`; `_all_panels` | `_build_panels(headless)` — always |
| 1081-1126 | Per-panel figure styling | `_style_panel_figures()` — **gated**; dereferences `_panel.figure`, which is `None` headless |
| 1127-1167 | Toolbar sync between `_pos0`/`_pos1` | `_build_gui()` — **gated** |
| 1168-1194 | Flag/unflag callback wiring | `_build_gui()` — **gated** |
| 1195-1198 | `_build_layout()` | `_build_gui()` — **gated** |

**Coupling is minimal.** The panel-construction block references only
`self._comm_mgr` and `self._cursor_source` from GUI-created state — no pipe, no
`_app_context`, no message ids. `comm_mgr=None` is already supported by
`VisibilityPlot` (guards at `visibility_plot.py:307-318`, `:335`).

**Headless flow:** `_resolve_config()` -> FlagDB/hotkey -> `_build_panels(headless=True)` -> return.

**Render only what is needed.** `_build_panels` takes which `(slot, kind)` pairs
to construct. The GUI builds all four (2 slots x 2 kinds) so kind-switching is
instant; headless builds only the kind each slot will actually export, saving two
`_render()` calls — real backend queries, paid once per exported file, so 43
times in a 43-PNG iteration.

**Consequence to handle:** `_all_panels` will hold fewer than four entries
headless, and `_PanelSlot` will hold `None` for the unbuilt kind. Anything
indexing `_all_panels` positionally, or assuming both kinds exist (the swap and
kind-switch paths), must be checked. Those paths are GUI-only so headless never
reaches them, but the unbuilt kind should raise a clear error rather than an
`AttributeError` on `None`.

## 8.13 Palettes must follow the theme (2026-08-16)

### Diagnosis: it is alpha, not hue

Scatter density layers looked washed out on a light background — in the
exported PNG **and in the GUI's own Light mode**, which is what proved the PNG
was faithful and the defect upstream.

Datashader gives low-count pixels low alpha, so they blend toward whatever is
behind them. A dark-to-bright ramp works on a dark ground (sparse fades to
black, dense is bright) and inverts badly on white: sparse fades to white, and
the bright end is *also* near-white, so the panel collapses toward the
background. A light theme needs ramps running **light to dark**.

Measured, compositing density 0..1 with alpha rising against each background —
peak colour distance from the background:

| family | background | peak |
|---|---|---|
| `polar` | dark | 306–330 |
| `polar` | **light** | **143–193** |
| `polar_light` | **light** | **355–383** |
| `polar_light` | dark | 140–148 |

Matched theme and palette give roughly 2x the contrast, and the mismatch is
symmetric — a light family on a dark ground is equally bad, so this must track
the theme rather than being a fixed better choice.

Rasters are far less affected: a populated raster cell is opaque, so only empty
cells show the background. That is why the light GUI raster always looked
acceptable while the scatter did not.

### The architectural consequence

**A palette is a render-time choice, not a chrome choice.** It is applied before
`tf.shade()`, lives in `ColorBand.cmap`, and by the time `png_export` sees a
`RenderedPanel` the pixels are already coloured. `export_png(theme=...)`
therefore **cannot** fix a mismatched palette — it would have to re-shade, and it
deliberately has no backend access.

PNG dark mode itself already works (`export_png(theme="dark")`, tested); only the
palettes failed to follow.

**Restates the tier-1 fidelity claim** (§1): byte-identical to the GUI *at the
same theme*. A light-themed export was never going to match a dark GUI, because
different pixels were shaded. That is the honest form of the claim.

### Agreed design

* **`theme` and palette are separate arguments.** Independent axes — viridis on
  dark is a legitimate want. `theme` sets the *default* palette; an explicit
  palette argument overrides it.
* **Scatter selects a *family*, not a ramp.** `_LAYER_CMAPS` is indexed by layer,
  so a two-polarisation scatter draws two ramps that must stay distinguishable
  from each other as well as from the background.
  `scatter_cmap="polar"` names a set.
* **Sidebar controls for both**, taking effect on **replot**. This dissolves the
  cost concern: replot already re-queries and re-shades, so palette changes need
  no new machinery and no fast path.
* **The Light/Dark toggle marks the plot stale** — chrome flips instantly, with a
  hint to press Plot to update colours. Keeps the toggle free and makes the
  coupling visible, rather than auto-replotting (~2 s here) or silently leaving
  the pixels wrong.
* **Sticky override, per role.** Once the user picks a palette by hand, the theme
  stops driving *that role* for the session; the other role keeps tracking. An
  explicit constructor argument counts as user-set from the start — otherwise the
  first theme toggle would silently discard it.
* The stale hint should only appear when at least one role is still
  theme-driven; otherwise the toggle genuinely changes nothing but chrome.

### The rule is background *separation*, not ramp direction

The first cut of `palettes.py` asserted only that light ramps run light-to-dark.
Too weak: it caught burnout on white but not the mirror defect, **sparse pixels
vanishing into the dark ground**. Measured against the dark axes background
(luminance 0.098), *every* dark-theme ramp started within 0.1 — `polar[1]`
started at 0.09, a separation of 0.008.

Both failures are one constraint violated at opposite ends: **no ramp colour may
sit near the background luminance.** It applies to opaque rasters too — a
plasma-minimum cell (0.07) against the dark ground is indistinguishable from an
empty cell, so the lowest data value and "no data" look identical.

### Conditioning rather than hand-tuning

`condition(cmap, theme, min_gap)` re-samples a ramp over the longest contiguous
stretch that clears the background. Canonical ramps stay as authored; one
definition per ramp adapts to each theme, survives a background change, and works
on a user-supplied colormap the registry has never seen.

**Per-role margins**, because alpha changes what "enough" means:

| Role | Gap | Why |
|---|---|---|
| Raster | `RASTER_MIN_GAP = 0.06` | Opaque — only needs to be distinguishable. Trimming plasma at 0.06 keeps its purple (`#7804a6`); at 0.16 it would cut to `#a21d9a` and discard the whole deep-blue third. |
| Scatter | `SCATTER_MIN_GAP = 0.16` | Alpha-blended — low alpha scales contrast *down*, so 0.16 shows only ~0.03 at 20% alpha. That is the floor, not a margin. |

### Conditioning is necessary but not sufficient at the sparse end

Measured with Datashader's `min_alpha=40/255`, distance from background:

| ramp | sparse (raw → conditioned) | dense |
|---|---|---|
| `polar[0]` dark | 11.9 → **17.4** | 330 |
| `polar[1]` dark | 13.5 → **28.4** | 307 |

Roughly double, but 28 against a 441 maximum is still faint. **`min_alpha` is the
dominant lever for the sparse end** and no palette choice can lift it much
further — `tf.shade(min_alpha=...)` in the scatter shade path is the
complementary change, and the next thing to try if sparse data still disappears.
Documented on `SCATTER_MIN_GAP`.

### `palettes.py` — DONE

Registry keyed by role and name, with `_DEFAULTS` mapping theme to the default
name for each role. Raster: plasma, viridis, inferno, cividis, plasma_r,
viridis_r, gray_r. Scatter families: polar, warm, polar_light, gray_light.

Unknown names fall back to the theme default rather than raising — a palette name
is cosmetic, and failing a whole render over one is worse than drawing it in the
default colours.

`check_background_contrast()` asserts that after conditioning every ramp clears
its background by the role margin, *and* that direction is right so the sparse
end is the one that fades. **This defect is invisible in a swatch** — the colours
look fine alone and only the composited image shows it — so it needs a
programmatic check rather than review. Its tolerance allows 0.005 for 8-bit
quantisation in `_hex()`, without which it flags ramps that miss by 0.001.

### Remaining wiring

* `_DEFAULT_CMAP` (raster) and `_LAYER_CMAPS` (scatter) become lookups at
  construction rather than module constants.
* Constructor: `theme`, `raster_cmap`, `scatter_cmap`, plus the per-role
  user-set flags.
* Sidebar: two palette selectors; toggle sets the stale hint.
* `ColorBand.cmap` keeps carrying whatever was actually used, so the colorbar and
  legend follow automatically with no further change.

## 8.14 Two corrections from the first themed run (2026-08-17)

### Reversing the raster ramp was wrong

Light defaults used `plasma_r`. But **reversal only matters for
alpha-blended scatter**, where the *sparse* end must be the one that fades into
the background. A raster cell is opaque — nothing fades — so it needs only to
avoid the background, which `condition()` already handles by trimming whichever
end is at risk (near-black on dark, near-white on light).

`plasma` on white needs no trimming at all: its low end is far from white and its
high end clears the raster margin. So the raster default is now **the same ramp
in both themes**, and light-mode rasters stop reading inside-out.

`check_background_contrast()` correspondingly audits every raster ramp against
*both* backgrounds (they are offered in both themes) and no longer constrains
raster ramp *direction*, which is free for an opaque ramp.

### The GUI cannot yet start in light chrome — now gated, was broken

`theme="light"` selected light-conditioned palettes, but the GUI chrome stayed
dark: the Light/Dark toggle restyles via `CustomJS` on the `"active"` change
event, and **a change event does not fire at load**. Initialising the toggle to
`active=True` does not help — there is no change to react to.

The result was the worst combination: light ramps against a dark ground, which is
the mirror of the defect §8.13 was written to fix and costs about the same
contrast (~2.5x). The scatter rendered nearly black.

**Gated:** a GUI launch with `theme="light"` warns and falls back to dark
palettes, so chrome and pixels agree. Headless is unaffected —
`VisibilityPlotter(headless=True, theme="light")` and `export_png(theme="light")`
are fully supported, because there is no chrome to disagree with.
`_requested_theme` preserves what was asked for.

### The toggle reads as a state indicator, not an action

Observed 2026-08-17: the button shows `☀ Light` while the GUI is dark, and was
read as "the light scheme is active" rather than "click for light". It is an
*action* label by design — the CustomJS flips it to `🌙 Dark` after switching —
but it was misread repeatedly, which is better evidence about the design than the
intent behind it.

**Replace it with a two-segment `RadioButtonGroup` (Light / Dark)**, matching the
Layout control immediately beside it in the same toolbar, where the active
segment is highlighted. Same visual language as its neighbour, no label
inversion to decode, no emoji cue needed.

This also fits the fix below: a `RadioButtonGroup` that sends a p2j message on
change gives Python the theme, which is what all three blocked behaviours need.

**To lift the restriction**, one of:

1. **Theme-aware Python styling** — `_style_panel_figures`, `_style_cmap_column`
   and `_dark_stylesheet` take the theme instead of hardcoding dark. Most
   thorough, largest diff, and makes the toggle and the constructor share one
   code path.
2. **Document-ready hook** — `BokehAppContext` fires a `DocumentReady` CustomJS
   that flips the toggle when the constructor asked for light, reusing the
   existing restyle callback. Much smaller, but it needs a hook the app context
   does not currently expose, and there would be a brief dark flash at load.

(1) is the right end state; (2) is the cheap route if light-at-startup is wanted
before the styling refactor.

**This also blocks the agreed stale-hint behaviour** (§8.13): for a toggle to
mark the plot stale and have Plot re-resolve palettes, the toggle's state must
reach Python. It is currently pure JS, so that needs a p2j message regardless of
which option above is taken.

## 8.15 Luminance was the wrong metric (2026-08-17)

Conditioning trimmed plasma's low end from `#0d0887` to `#7804a6`, discarding
the deep blue that gives the ramp its depth — visible in both the GUI and the
export, and the reason the output "lost its sensory appeal".

**The trim was unnecessary.** `#0d0887` differs from the dark axes ground
`#181825` by only 0.026 in *luminance* but by **100 RGB units** — deep blue
versus dark navy-grey, obviously different to the eye. Luminance alone was
measuring the wrong thing: two colours of similar brightness and different hue
are perfectly distinguishable, especially for an *opaque* raster cell.

**Switched to Euclidean RGB distance** (0–441), with per-role thresholds:

| Constant | Value | Rationale |
|---|---|---|
| `RASTER_MIN_DIST` | 70 | Opaque; hue counts as much as brightness. Leaves plasma **completely untrimmed** in both themes. |
| `SCATTER_MIN_DIST` | 150 | Alpha-blended; Datashader's `min_alpha=40/255` scales separation down by 0.157, so 150 shows as ~24 on screen. |

Results — distance from background under simulated alpha blending:

| ramp | sparse (raw → conditioned) | dense |
|---|---|---|
| `polar[0]` dark | 11.9 → **23.7** | 330 |
| `polar[1]` dark | 13.5 → **23.6** | 307 |
| `polar_light[0]` light | 10.9 → **23.6** | 355 |
| `polar_light[1]` light | 18.2 → **23.7** | 383 |

Roughly double the sparse-end visibility, symmetric across themes, **and the
raster ramp fully restored**. The old luminance constants are retained under
their previous names with deprecation notes, since the reasoning is worth
keeping.

The lesson generalises: *"close to the background" is a perceptual claim, and
luminance captures only one dimension of it.* The audit was measuring in
luminance while the ramps were being conditioned in distance — the two must use
the same units or the check silently passes ramps it was written to catch.

## 8.16 The stale-hint design was wrong; `refresh.py` (2026-08-17)

### Why it was wrong

§8.13 agreed the Light/Dark toggle would flip chrome instantly and mark the plot
stale, on the assumption that the intermediate state was merely *suboptimal*.

Observed in practice: it is **unreadable**. A theme change inverts the
ramp/background relationship and costs ~2.5x contrast in the direction that
matters, so the scatter goes nearly black. Asking a user to press a button to
make an illegible plot legible is not a state to leave them in.

### But auto-replot was not the answer either

The original choice was framed as toggle-only / stale-hint / auto-replot, and all
three were wrong because they shared a false premise: that changing the palette
required recomputation. **It does not.** A palette alters no rows, no
aggregation and no extent — the cached `agg` and layer DataFrames are sufficient,
which is exactly what `update_scaling()`'s fast re-shade path already exploits.

### The general mechanism

`refresh.py` names the rungs so callers can ask for the minimum and reviewers can
see when a handler over-reaches:

| Level | Meaning | Examples |
|---|---|---|
| `CHROME` | no pixel change | titles, labels, theme colours, tick format |
| `SHADE` | same aggregation, different colours | cmap, scaling/alpha/gamma/vmin/vmax, layer alpha, `color_mode`, viewport within cache |
| `AGGREGATE` | same rows, different binning | canvas resize, `raster_interpolate` |
| `QUERY` | new rows | axes, quantity, polarisation, any `SelectionSpec` field |

`AGGREGATE` is meaningful only for scatter — raster's `query_raster` fuses query
and aggregation, so a raster `AGGREGATE` is a `QUERY`.

`level_for(*names)` returns the max over the named changes. **Unknown names
resolve to `QUERY`**: recomputing too much is a performance cost, showing stale
pixels is a correctness one, and only the second is a bug. A missing table entry
is still an oversight, not a default.

### Wiring

* `VisibilityPlot.apply_refresh(level)` — `CHROME` no-ops, `SHADE`/`AGGREGATE`
  delegate to `_reshade()`, `QUERY` re-renders. `_reshade()` defaults to a
  logged no-op so a subclass without a cheap path degrades to "nothing happens"
  rather than to a silent full re-query.
* `VisibilityRaster.set_cmap()` / `_reshade()` — re-shades the cached agg via
  `update_scaling()`.
* `VisibilityScatter.set_layer_cmaps()` / `_reshade()` — re-composites the cached
  layer DataFrames via `_composite_and_push()` at the current viewport.
* `VisibilityPlotter.set_theme()` — re-resolves palettes for roles that are not
  user-set, recomputes both ramps (a user-set *name* still resolves to different
  colours per theme, because `condition()` trims against the background), and
  pushes the new cmaps to all four panels.

### Wired to the toggle — DONE 2026-08-17

`_ids["theme"]` on the existing control pipe, `_handle_theme` registered beside
`_handle_plot`/`_handle_done`, and the restyle body's toggle caller appends a
`ctrl.send(ids['theme'], {theme: ...})` after flipping the chrome. `ctrl` and
`ids` are already bound in `_build_toolbar` (lines 3411–3412), well before the
toggle's args dict, so no new plumbing was needed.

The **startup** caller deliberately does not send: Python already knows the theme
at construction, and a message there would be a redundant round trip.

Symptom before this landed, worth recognising if it recurs: launching with
`theme="light"` and clicking Dark flipped the chrome and left the plot visibly
unchanged — light-conditioned ramps on a dark ground, which is the mismatch case.

### The re-shade must be *returned*, not assigned

First attempt re-shaded correctly and invisibly. `_composite_and_push()` assigns
`self._image_source.data`, and **with no Bokeh server a Python-side
`ColumnDataSource` assignment never reaches the browser** — the contract every
other image-updating handler already follows, stated in
`VisibilityScatter._handle_set_color_mode`'s docstring: *"Python-side model
property changes don't propagate to the browser in static HTML mode."*

The symptom was diagnostic: the **raster** appeared to update and the **scatter**
did not, because the raster's own scaling path pushes over the comm while the
theme path did not.

`_handle_theme` now returns `images`, and the toggle's response callback installs
each into its `image_source` and calls `change.emit()`. Alignment between the two
sides comes from `_theme_img_panels`, an index list built once at toolbar
construction: the args capture sources in that order and
`_panel_image_payloads()` emits in the same order, with `None` for a panel that
has no image so a deferred panel does not shift everything after it. The JS
guards on length and skips nulls.

**Verify `set_theme` never queries.** A structural check asserts its body
contains no `_render(`, `query_raster`, `query_columns` or `update_axes`. That
property is the whole reason a theme change can run on a click, and it would be
easy to lose by routing a future palette change through `update_axes`.

**Latency, measured in use (2026-08-17):** a slight delay, and the result is
legible in both directions. Acceptable as-is.

Note the "re-shade only the visible panels" optimisation is already implicit and
would gain nothing: `set_theme` loops all four, but a deferred panel's
`_reshade()` returns early on absent cached state (`_agg is None`, or all
`_layer_dfs` None), so only the two drawn panels do real work. The remaining
delay is Datashader's shade cost on 30M points, not overhead.

## 8.17 Export button — DONE 2026-08-17

The original goal, reachable at last: `visplot` can write a PNG of the current
view from the GUI, matching what is on screen including zoom.

**Payload is browser-only state, deliberately.** With no Bokeh server the
figures' `x_range`/`y_range`, the layout radio and the display order are all
`CustomJS`-mutated and never reach Python — so an export driven from
`self._pos0._x_range` would render the *unzoomed* extent in the default layout.
The message carries exactly those three things:

* `panels[].viewport` — `[x0, x1, y0, y1]` per visible figure
* `panels[].panel` — index into `_all_panels`, resolved client-side from
  `slotN_{raster,scatter}_layout.visible` and a `panel_index` map, so the
  handler indexes panels the same way the JS names them
* `nrows`/`ncols` — the JS translates the layout radio, so **no layout string
  crosses the wire**; the compositor only ever takes a grid shape (§2.3)

Everything else — axes, selection, scaling, palettes, cached aggregations —
Python already holds and is *not* taken from the message. A structural check
asserts the payload contains no `selection`/`scaling`/`cmap`/`axes` keys.

**Order comes from `_display_order_source`**, so a Swap is reflected in the
export rather than silently exporting in slot order.

**Never re-queries.** `render_result(viewport)` re-shades from cache, so the
export is fast and shows exactly what the user is looking at rather than a fresh
query that might differ. Asserted structurally: the handler contains no
`query_raster`/`query_columns`/`update_axes`/`_build_selection`.

**Where the file lands.** Server-side, defaulting to
`<ms-stem>_<YYYYmmdd-HHMMSS>.png` in the process's working directory, with the
resolved absolute path returned and shown in the notify div. There is no save
dialog without a Bokeh server, and under JupyterLab-over-SSH the Python process
is on a different machine from the browser, so "save where the user is" is not
available. Reporting the path is the honest substitute. Streaming bytes back as
a browser download remains possible via base64 over the comm but is a lot of
payload for a convenience (§9.2).

### Dimensions: the exported aspect must follow the GUI

First run exported the right *data* at the wrong *shape*: a full-width One-mode
panel came out square. The compositor sizes cells from the image, which is
Python's construction-time canvas — but the layout JS resizes the figures for
One / Side / Over-Under (`full_w`, `side_w`, `panel_h`, `over_h`) and, like the
viewport, those assignments never reach Python.

**Two changes:**

1. The payload now carries each figure's on-screen `size`, passed to
   `export_png(cell_size=...)`. That is *not* the canvas the image was shaded
   at, so matplotlib scales the image into the box — trading tier-1 1:1 pixels
   (§1) for matching the GUI, which is the right trade for an export whose
   purpose is to reproduce the view.
2. `VisibilityPlotter(plot_width=, plot_height=)` sets the per-panel canvas.
   `_PANEL_WIDTH_SIDE`/`_PANEL_HEIGHT` were module constants, so panel size was
   not configurable at all. Note this is the Datashader canvas as well as the
   Bokeh figure size, so it sets **aggregation resolution**, not just display
   scale — raising it genuinely resolves more detail.

Passing `plot_width`/`plot_height` to match the intended output gets both
matching aspect *and* 1:1 pixels.

## 8.18 `TestProbeMultiLayer` — DONE 2026-08-17

`TestProbe` builds its fixture with `_make_single_layer`, so the entire probe
test class exercised one layer — and defect (1) of the PB-series (consulting
*every* layer, the bulk of the 47.7% false-empty rate) had no coverage since it
was fixed.

Six tests on a two-layer fixture: one entry per layer in index order; a bin
populated in layer 1 but not layer 0 still reports a value (defect (1) exactly,
skipping when the polarisations happen to be populated identically);
`winner` indexes the nearest hit; a hidden layer reports no value while the
visible one still does; the label renders both readings in index order; and the
envelope is JSON-safe with twice as many numeric fields.

## 8.19 `__call__` is the terminal verb in both modes (2026-08-17)

### The colour bug that prompted it

The first headless export came out washed out. `VisibilityPlotter(headless=True)`
defaults to `theme="dark"` so the palettes were dark-conditioned, while
`export_png()` defaulted to `theme="light"` and drew them on white — the §8.13
mismatch again, arriving through the one door left open: **the caller had to
pass the theme twice**, and the obvious usage passed it neither time.

That is a design flaw, not a usage error. The pixels already know what they were
shaded for, so `PanelSpec.theme` now carries it and `export_png(theme=None)`
adopts it. An explicit `theme=` still wins. The mismatch is no longer reachable
by omission.

### The API shape

Manual `render_result()` + `export_png()` was clunky, and the only reason to
build a headless plotter is to produce files — so the terminal verb should do it.

**The two modes are disjoint**, which is what makes overloading `__call__`
honest rather than opaque: a headless plotter has no GUI to launch, and a GUI
plotter has no reason to be called for output.

```python
# GUI — unchanged
app_context, task = plotter(exec_context, app_id)

# Headless — returns the absolute path written
path = plotter(plotfile="amp_vs_uvdist.png")
```

**Iteration is repeated invocation**, which is the real payoff: the plotter holds
the opened MS, so each call reuses the open handle rather than reopening. For a
43-antenna sweep that is one open instead of 43.

```python
for spw in (0, 1, 2, 3):
    plotter(plotfile=f"amp_spw{spw}.png", spw=[spw])
```

Any `SelectionSpec` field may be passed. **Unknown field names raise** rather
than being ignored — a silently dropped `spw=[2]` would write a file that looks
right and contains the wrong data, the worst outcome available. Overrides
persist across calls, so a loop that narrows stays narrowed; pass the field again
to widen.

Selection changes go through `apply_refresh(RefreshLevel.QUERY)` — the one part
of a headless export that touches the backend. Calling repeatedly *without* a
selection change reuses the cached aggregations and costs only the shade.

`nrows`/`ncols` default from the layout (`one`→1x1, `side`→1x2, `over`→2x1) and
`layout=` overrides per call. Backward compatibility verified structurally:
`exec_context` and `task_id` remain the first two positional parameters and every
new option is keyword-only.

### One `theme`, one meaning

A follow-on report asked for an *error* when `theme` is passed alongside
`headless=True`, on the belief that it would not be honoured. It is honoured —
in headless mode the constructor's `theme` selects the **palettes**, which are
baked into the pixels at shade time.

The real wart was that two parameters shared a name and meant different things:
constructor `theme` chose palettes, `export_png(theme=)` chose chrome. Setting
them separately produced dark-conditioned ramps on a white ground — legible, but
fading toward the wrong background at the sparse end, and easy to miss because
plasma is now untrimmed in both themes (so the *raster* is theme-independent) and
the scatter degradation is mild rather than dramatic.

**Erroring would have been the wrong fix.** A call-time `theme` now re-resolves
the palettes as well, via `set_theme()` — a `SHADE` refresh, no re-query — so one
`theme` at either place does the whole job and the two cannot diverge.

### A documented contract with no implementation

`VisibilityPlot._theme_hint()` reads `self._theme` on the panel, and its
docstring said "set by `VisibilityPlotter`". Nothing set it. Every panel
therefore reported the class default `"dark"`, so
`VisibilityPlotter(theme="light", headless=True)` produced light-conditioned
ramps under dark chrome — the same mismatch, inverted.

`_build_panels` now stamps `panel._theme` at construction and `set_theme` keeps
it in step.

**Third occurrence of this shape** (after §8.7's unforwarded adapter method and
§8.16's Python-side CDS assignment): a contract stated in a docstring, unimplemented,
failing silently. `PanelSpec.theme` defaults to `"dark"`, so a producer that
never sets it looks correct in every dark-themed test and mislabels only the
light ones — and the light path was the newest and least covered.

`TestSpecThemeIsPopulated` now asserts the *contract* rather than a producer
(export follows a light spec; mixed specs take the first drawable), so it holds
for any future producer. **83 tests.**

### Remaining

`plotfile` is not yet a *constructor* argument, so the scraped
`visplot(ms=..., plotfile=...)` shim of §2.1 still needs its dispatch: when
`plotfile` is supplied, construct with `headless=True` and call once. That is now
a small shim rather than a design question.

## 8.20 `min_alpha` and the timing test (2026-08-17)

### `min_alpha` — the other half of the sparse-end problem

`palettes.condition` could only half-address faint sparse scatter: it moves the
*colour* away from the background, but **alpha decides how much of that colour
survives**. All three `tf.shade` branches omitted `min_alpha`, taking
Datashader's default of 40/255, which makes a single-point pixel nearly
invisible against either ground.

Measured on the dark background with the conditioned `polar[0]` ramp, distance
from background at the sparse end:

| `min_alpha` | sparse | 25th pct | dense |
|---|---|---|---|
| 40 (default) | 23.7 | 53.0 | 330 |
| 60 | 35.6 | 53.0 | 330 |
| **90 (chosen)** | **53.3** | **73.6** | 330 |
| 120 | 71.1 | 98.2 | 330 |
| 160 | 94.8 | 130.9 | 330 |

**Higher is not better.** Dense pixels stay at ~330 regardless, so raising the
floor compresses the dynamic range: at 160 a nearly-empty pixel is already a
third of the way to a full one and genuine density structure flattens. 90 roughly
doubles sparse visibility while keeping the low quarter of the range
distinguishable.

Scatter only — a raster cell is opaque and has no sparse end.

### Timing test replaced

`test_defer_is_much_faster_than_real_render` asserted
`deferred < real/10 or deferred < 0.05`, which assumes real construction is
*dominated* by the backend query. Once the MSv2 query got fast enough (observed
real ~0.216s, deferred ~0.096s) the fixed cost both paths pay — Bokeh figure,
tools, comm setup — became a large fraction of real, and the ratio failed while
the code was fine. **A faster machine makes it fail harder**, which is the wrong
direction for a test to move.

`test_defer_makes_no_backend_query_calls` spies on `query_columns`,
`query_raster` and `samples_per_pixel` and asserts zero calls. Stronger than the
sibling `test_defer_leaves_all_layer_dfs_none`: a query whose result was fetched
and discarded would leave those None and still be the bug. It also asserts the
spy *fires* on a real render, so a refactor that renamed the query methods cannot
leave the test silently inert.

## 8.21 SI prefixes in axis labels — DONE 2026-08-17

`372550000000` on a Frequency axis was unreadable in every plot and every
export. Ticks now read `372.55` under a `Frequency [GHz]` label.

**The prefix goes in the label, not on each tick.** Bokeh's
`CustomJSTickFormatter` runs per tick and has no slot for a shared multiplier
annotation, so matplotlib's `+3.7255e11` corner notation has no GUI counterpart.
Scaling the label is the one approach that works identically in both runtimes,
which keeps the parity harness meaningful.

**The scale comes from the full extent, not the viewport.** A unit that flips
from GHz to MHz partway through a zoom is more disorienting than a large number,
and holding it fixed means the label is computed once rather than on every pan.
`PanelSpec.axis_scale()` reads `x_range`/`y_range`; `_state_source` carries
`x_scale`/`y_scale` for the JS formatter.

**Time axes are excluded** — elapsed formatting is already human-scaled, and
dividing MJD seconds by 1e9 would be nonsense. So are compound and angular units:
`km/s` from a value in m/s would be right, `kdeg` from degrees would not.

The prefix set is deliberately coarser than `si_scale`'s (k, M, G and m, µ): an
axis reading `TB` or `fm` is more confusing than a slightly large number.

**Never inferred from magnitude.** The unit comes from `AxisInfo.unit`, which the
panels now put into `PanelSpec.x_unit`/`y_unit`. A magnitude-triggered "GHz"
would mislabel a long baseline in metres as gigametres.

**Parity:** golden table split into `GOLDEN_CASES` (25 unscaled, unchanged
four-tuples so existing harnesses need no edit) and `GOLDEN_SCALED` (10
five-tuples). **1500 fuzzed cases across five prefix scales — JS and Python
identical.** Seven new compositor tests, including that an existing `[m]` in a
label is not duplicated into `[m] [m]`. **90 tests.**

## 8.22 Two raster cleanups (2026-08-17)

### Raster now tracks its viewport (defect 4)

`_do_viewport_rerender` shaded at the requested range and never stored it, so
Python believed the raster was always showing its full extent while the browser
showed a zoom. Scatter has tracked `_current_viewport` all along.

Harmless while every consumer passed a viewport explicitly — but by the time
`set_theme` landed it was no longer harmless: a palette re-shade would have
snapped a zoomed raster back to full range, silently. It is set on both branches
(Level-1 resample records the range; Level-2 re-query clears it, because the
re-query re-cached at the new extent), cleared by `_render`, and honoured by
`_reshade`/`_shade_for_export`.

Worth noting the shape: an asymmetry that was *currently* harmless became a bug
as soon as a new caller appeared, and nothing would have flagged it.

### `_render` records *which* degenerate condition tripped

`_degenerate` collapsed deferred / sub-2-cell / all-NaN / zero-width into one
boolean. For the GUI a blank panel beside a live sidebar is self-explanatory; an
exported PNG has no sidebar, and "not rendered yet", "no unflagged data in this
selection" and "x axis has zero width" are things a reader needs to tell apart.

`_panel_spec()` had been re-deriving the cause from `self._agg` — duplicated
logic that could not distinguish `defer` at all, since a deferred panel's agg
looks the same as an empty one. It now reads `_degenerate_reason`, which only
`_render` can know.

## 8.23 Channel-index capability is per-query-path (2026-08-18)

### The divergence

Observed with `Channel` selected on both panels: the **scatter** plotted a real
channel index 0–383 labelled `Channel`, while the **raster** showed
`Frequency [GHz]`. Same requested axis, same backend, two answers.

Cause: `query_columns` (scatter) returns `_compute_axis_values(Axis.CHANNEL)` —
a genuine `arange` — while `query_raster` resolves through `_axis_to_dim` to the
frequency *coordinate*. **Capability is per-path, not per-backend**, and
`_SUPPORTS_CHANNEL_INDEX` as a single backend-level flag could not express that.

### Fix

`axis_info(axis, selection, query=)` takes `"columns"` or `"raster"`. Each plot
class declares `_QUERY_PATH` (`VisibilityPlot` defaults to `"columns"`;
`VisibilityRaster` overrides to `"raster"`) and `_refresh_axis_info` passes it.
The flag is renamed `_SUPPORTS_RASTER_CHANNEL_INDEX` so its scope is visible at
the definition, and the substitution note now says the *raster path* is what
lacks the capability and that scatter panels can plot channel number.

`LocalVisibilityReader.axis_info` forwards the new argument — the same adapter
gap as §8.7, caught this time by looking rather than by a symptom.

Verified on both backends: single SPW gives `Channel (spw 0)` on the columns path
and `Frequency [Hz]` on the raster path; multi-SPW substitutes on both.

### Switching Channel <-> Frequency should be CHROME, not QUERY

Within a single SPW, channel index and frequency are related by an **affine map**
(uniform channel width), and Datashader bins uniformly over the coordinate — so
the *same aggregation produces the same image* either way. Only the extent and
the labels differ.

That makes a Channel <-> Frequency switch a `CHROME` change in `refresh.py`
terms: no re-query, no re-shade, not even a re-composite. Push new ranges and
labels to the axes and the existing pixels are already correct.

Caveats to encode when implementing: it holds only for a **single SPW** (across
several, frequency is monotonic and channel index is not unique) and only for
**uniform channel width** — worth asserting from the frequency coordinate rather
than assuming, since a concatenated or irregular SPW would break the affinity
silently.

**Remaining for raster channel support:** `query_raster` assigning
`np.arange(n_chan)` as the x coordinate when one SPW survives, `probe_raster_pixel`'s
inverse mapping, and the index->frequency conversion before `FlagDB` (which
stores coordinate ranges). Then flip `_SUPPORTS_RASTER_CHANNEL_INDEX`.

## 8.24 The probe now carries a flagging identity (2026-08-18)

A pixel on screen is not a place in the MS: it is a Datashader cell over a
concatenated, selection-filtered view. `probe_raster_pixel` reported enough to
*describe* a cell but not enough to *address* it, so a `FlagOperation` could not
have been written from a probe result.

### What was missing

`spw` and channel indices. The probe returned a frequency range, but CASA
addresses channels as `spw='0:137~139'` — and with several SPWs concatenated onto
one frequency axis, a cell's frequency window can span more than one.

Both backends now record `spw_channels: {spw: [c_lo, c_hi]}` while iterating
partitions in the probe. Read from **each partition's own `frequency`
coordinate**, so it is exact rather than reconstructed from an average channel
width — which would be wrong on a concatenated or irregular SPW, and wrong
*silently*. Partitions declaring no SPW id contribute to neither
`spw_channels` nor `spw_ids`: an unidentified window cannot appear in a flag
command, so claiming otherwise would be worse than omitting it.

### `VisibilityPlot._flag_key(info)`

Assembles the full identity into the probe envelope under `flag_key`:

| field | why |
|---|---|
| `spw_channels` | the CASA addressing form; what gets retyped |
| `freq_range_ghz` | the physical equivalent; **survives repartition and SPW renumbering** (`split`/`mstransform` renumber; frequency does not), so `FlagDB` stores this and `spw_channels` is the convenience form |
| `time_range` | MJD seconds, from `_cell_bounds` — local neighbour spacing, so inter-scan gaps do not inflate it |
| `antenna_pairs`, `field_names`, `scan_names` | the remaining selection axes a flag command takes |
| `correlation` | a raster shows one polarisation at a time; flagging the others would be wrong |
| `data_column` | which column the flag applies to |

Time is taken from **whichever axis carries it** — a raster can plot time on
either — and the whole thing returns `{}` on an empty probe so callers can test
truthiness rather than inspecting fields. Verified JSON-safe, since it crosses
the comm.

Round-trip check: a probe of a cell yields
`spw='0:137~139' antenna='DA41&DV02' correlation='XX'` — directly usable.

### Still needed for a complete flag round trip

* **Scatter probes** carry no `flag_key` yet. A scatter pixel covers many
  visibilities and the inverse mapping is genuinely harder — worth doing
  deliberately rather than by analogy with the raster.
* **`FlagDB` write path** — this supplies the identity; converting it to a
  `FlagOperation` and applying it to the MS is the remaining half.
* **Uniform channel width** should be asserted from the frequency coordinate
  where the affine channel/frequency assumption is used (§8.23), not assumed.

## 8.25 There is no numeric SPW id in the xarray-ms view (2026-08-18)

Diagnosing an empty `spw_channels` produced a finding that invalidates an earlier
assumption. Dumping `ds.attrs` for every partition of a real MS
(xarray-ms 0.5.6):

* **No `spectral_window_id`. No `DATA_DESC_ID`.** Neither key exists.
* The only spectral identity is on the *frequency coordinate*:
  `ds.frequency.attrs["spectral_window_name"]` =
  `'ALMA_RB_07#BB_2#SW-01#FULL_RES'` -- a **name**, not the integer CASA's
  `spw=` takes.
* Also there, and useful: `channel_width` = 610351.5625 Hz, and
  `reference_frequency`.

### Consequences, including a retraction

`metadata()` reporting `spws=0` was **accurate**, not a lookup bug.

`_spw_selected` keeps every partition when no id is declared (the deliberate
"undeclared means keep" tolerance), so **SPW filtering has been a no-op on this
dataset all along** -- including when it was believed confirmed by a plot
(§8.4a). The extent change observed then came from something else. `_spw_selected`
now logs once when a store identifies windows by name, because silently ignoring
an spw selection is the exact defect that section was written to fix.

`axis_info` was taking its `n_spws == 0` branch, which is why CHANNEL substituted
with the "not declared" note rather than "not yet plotted".

### Fix

`_partition_spw_ident` gains a third lookup step and returns
`(identity, kind)` with *kind* in `{"spw", "ddid", "name", "none"}`. Identity may
now be a **string**; `chan_spans` and `spw_ids` accept it, and only "no identity
at all" is excluded -- an unnameable window cannot appear in a flag command.

`_partition_channel_width` reads the width so the affine channel/frequency
assumption (§8.23) can be *checked*. It is reported as `channel_width_hz`, both
in the backend's probe result and in `flag_key`.

**Verified live 2026-08-18**: probing grid cell `(60, 205)` on a `(410, 384)`
grid returns `spw_channels {'ALMA_RB_07#BB_2#SW-01#FULL_RES': [60, 60]}` — the
channel index matches `gx`, and all four partitions collapse into one entry
because they share a spectral window name.

### The design consequence for FlagDB

**Turning a name into `spw=N` needs a NAME -> row lookup in the MS's
SPECTRAL_WINDOW subtable, which the backend cannot reach.** So the numeric id is
not always obtainable, and `spw_channels` is a *best-effort convenience*.

This settles the earlier question in favour of the answer already argued in §8.3:
**FlagDB must key on frequency ranges.** They are exact, always available, and
survive the `split`/`mstransform` renumbering that would invalidate an id anyway.
`spw_channels` is the human-facing rendering, produced when the store makes it
possible.

## 8.26 The last `_axis_label` call sites — FIXED 2026-08-18

Symptom: after switching a scatter's x axis to Frequency, its **ticks** read
372.55–372.75 (correctly scaled) while its **label** read `Frequency [Hz]`. The
raster beside it read `Frequency [GHz]`.

The split is diagnostic. The raster was labelled at `_build()` time, which uses
`PanelSpec.axis_label()`. The scatter was *re-*labelled when its axis changed —
and that path runs through `_handle_update_axes`'s j2p response, which computed
`_axis_label(x)` from the bare `Axis` enum.

A bare `Axis` has **no selection context**, so it cannot know that `CHANNEL`
resolved to frequency, and **no range**, so it cannot carry the SI prefix. It
produced `Frequency [Hz]` under ticks the browser was already dividing by 1e9.

This is item 1c from §8.6, outstanding since the `AxisInfo` front-end work: the
two j2p sites were flagged then as "the last instance of the original
divergence" and not fixed. They already called `panel._effective_title()` and
`panel._state_data()` on the adjacent lines — the labels simply were not
converted with them.

Both now use `panel._panel_spec().axis_label(...)`, and the
`from .visibility_plot import _axis_label` import is removed from
`visibility_plotter.py` so it cannot be reached for again.

**Pattern, third instance:** a value derived from an `Axis` enum rather than
from the panel that rendered it. §8.7 established the rule -- *in plot code,
`Axis.label` is never the right source for displayed text* -- and this is where
it had not yet been applied.

## 8.27 Qualifier kind, and a measured re-query (2026-08-18)

### `Channel (ddid <name>)`

The scatter axis read `Channel (ddid ALMA_RB_07#BB_2#SW-01#FULL_RES)` — a
qualifier asserting the value is a DATA_DESC_ID when it is not a number at all.

`kind` was resolved by `kind = "spw" if kinds == {"spw"} else "ddid"`, a two-way
branch written before the `"name"` kind existed (§8.25). **MSv4 was worse**: it
had no branch at all, hardcoding `f"spw {spw_id}"` — the §8.11 mislabelling,
present there the whole time and only ever fixed in MSv2, on the assumption that
MSv4 always carries a numeric id. It does not.

Both now read the kind: a **name is self-describing and takes no prefix**; a
numeric id keeps one, because "137" alone says nothing about what it indexes;
partitions that disagree produce no qualifier rather than one that is wrong for
most of them.

Verified identical across both backends for all three identity shapes.

**Lesson repeated:** a fix applied to one backend and not the other. §8.7 already
recorded the backends diverging (`_axis_to_dim` arity) and the probe-geometry
helpers were hoisted into `reader.py` for the same reason. This block is now
duplicated in both and is a candidate for the same treatment.

### Frequency <-> Channel still re-queries: 1.656 s, measured

Confirmed from a live switch: changing the scatter's x axis from Channel to
Frequency ran a full `query_columns` — `n=30913392`, 1.656 s — for what is an
**affine relabelling of the same data** (§8.23).

For scatter the x column values genuinely differ (channel index vs Hz), so this
is not free the way the raster case is: it needs the cached DataFrame's x column
remapped, `x_chan = (x_freq - f0) / channel_width`, rather than re-fetched. That
makes it an `AGGREGATE` in `refresh.py` terms — re-bin cached points — not a
`QUERY`.

`_partition_channel_width` (§8.25) already supplies the divisor, and the
uniform-width check it enables is exactly the precondition.

### DECIDED 2026-08-18: do not implement the affine remap

Three reasons, in order of weight:

1. **It classifies on a different kind of predicate.** `refresh.py` decides by
   *what changed*. This would decide by what changed **and** the numeric
   relationship between the old and new values. That generalises to nothing else
   in the table, so it is a special case wearing the clothes of a general
   mechanism -- which is how the mechanism stops being trustworthy.
2. **The precondition is runtime data, not code.** Uniform channel width and a
   single spectral window must hold *at the moment of the switch*, so the slow
   path has to exist and be tested regardless. The optimisation adds a branch
   rather than replacing one.
3. **The failure mode is silent and severe.** A wrong remap yields a
   correct-looking plot with mislocated features -- the same shape as most
   defects in this document, reintroduced deliberately.

### The better path, if the 1.6 s ever matters

**Cache `query_columns` results keyed on `(x_axis, y_axes, selection)`.**

Toggling Channel -> Frequency -> Channel then hits the cache on the way back --
and so does *any* axis toggle: quantity, polarisation, y-axis. Not one pair.

No numeric assumptions, no precondition to verify at runtime, and the failure
mode is a **stale** plot rather than a **wrong** one, which is detectable. It
trades memory for latency instead of correctness for latency, and memory is the
quantity that can be bounded.

Invalidation is the whole design: the key must include everything
`query_columns` reads, so a `SelectionSpec` change or a `data_column` change
misses. Worth a cap on entries, since each holds a DataFrame of the full point
set (30M rows on the test dataset).

## 8.28 Raster channel index — DONE 2026-08-18

`Axis.CHANNEL` now plots a real channel index on the raster path, matching what
the scatter has done all along. `_SUPPORTS_RASTER_CHANNEL_INDEX` is `True` on
both backends.

### Shared, not duplicated

The helpers live in `reader.py`, not in each backend: per-backend copies of
spectral-window logic have diverged **three times** (`_axis_to_dim` arity §8.7,
the DDID qualifier §8.11, the kind resolution §8.27). The probe-geometry helpers
were hoisted for the same reason.

### `to_channel_index` — indices are positions, not `arange`

Applied to the assembled agg **after final decimation**. That ordering is the
whole subtlety: `_decimate_agg` may stride the axis, and after striding the
retained cells are channels 0, 4, 8 … not 0, 1, 2. Numbering them consecutively
would produce an axis that looks right and is wrong — and wrong *precisely on
zoomed-out views*, where decimation applies and where a user is least likely to
notice.

Verified: undecimated gives 0…383; strided by 4 gives 0, 4, 8, 12, 16 where
naive `arange` would give 0, 1, 2, 3, 4.

Index matching uses `searchsorted` plus a neighbour comparison rather than exact
float equality, because concat and decimation can perturb the last bit.

### `channel_axis_is_unambiguous` — compare coordinates, not window counts

Requires every partition to present the **same** frequency coordinate. Two
partitions of one window do (they differ by scan, field or observation, never by
channel); two different windows do not, and there is no global channel numbering
to fall back on.

Compared **by value** rather than by counting spectral windows, because the
window identity may be only a name (§8.25) while the coordinate is the thing that
actually has to match.

### The inverse, and why it is required

The reference frequencies ride in the agg's attrs (`CHANNEL_REF_ATTR`) so the
mapping is invertible. `probe_raster_pixel` compares its cell bounds against each
partition's `frequency` coordinate — with the axis relabelled those bounds are
*indices*, and comparing them to Hz **matches nothing, which looks like an empty
cell rather than an error**. `channel_range_to_freq` inverts first; on a
frequency-axis agg it returns `None` and the probe compares Hz as before.

Verified: cell bounds 59.5–60.5 invert to 372.569097–372.570318 GHz, which
matches channels 59, 60, 61 — so channel 60 is found and `flag_key` still
resolves through frequency, which is what `FlagDB` needs.

## 8.29 SPW metadata — the prerequisite for the checklist (2026-08-18)

`SpwInfo` already carried `name`, `centre_freq_hz`, `bandwidth_hz` and
`n_channels` — the exact fields §8.10's checklist needs — but zero-filled, with
the comment *"not available from raw metadata dict"*. And `metadata()` still used
the attrs-only SPW lookup superseded in §8.25, which is why it reported
`spws=0`: **the control had nothing to list.**

### Backend

`metadata()` now goes through `_partition_spw_ident` and collects per-window
detail while it is already iterating partitions and already reading the frequency
coordinate: `id`, `kind`, `name`, `n_channels`, `centre_freq_hz`,
`bandwidth_hz`, `channel_width_hz`, `freq_min_hz`, `freq_max_hz`. Exposed as
`meta["spws"]`; `spw_ids` remains, now sorted with a key that tolerates mixed
int/str identities.

Bandwidth spans channel **edges**, not centres — so a single-channel window has a
non-zero width rather than collapsing to 0. Verified against the real shape:
384 channels, 372.533–372.767 GHz, 234.375 MHz = 384 x 610351.5625 Hz.

### `SpwInfo`

Populated from `meta["spws"]` when present, falling back to the old zero-filled
form for a backend that has not been updated.

**`spw_id` is no longer necessarily an `int`** — it is the window *name* when
that is all the store provides (§8.25), so consumers must treat it as an opaque
key. The dataclass says so.

`SpwInfo.label()` renders a checklist row:

```
ALMA_RB_07#BB_2#SW-01#FULL_RES   372.53-372.77 GHz   384 ch
WVR#NOMINAL                        6.80-8.30 GHz       1 ch
```

The span and channel count are the point: they are what let a user pick the
science window out of the WVR and channel-average windows beside it in an
ASDM-imported MS. The id alone does not — and `1 ch` at 7 GHz is recognisable at
a glance.

### Still ahead for §8.10

The control itself. **Carry the §8.25 caveat into it:** on a name-only store
`SelectionSpec.spw` holds parsed integers and cannot match a name, so filtering
is a no-op and `_spw_selected` logs once. A checklist that appears to filter and
does not would be worse than the current dropdown — so either the control keys on
the same opaque identity `_partition_spw_ident` returns, or the name-only case
must be visibly disabled.

## 8.30 SPW selection keyed on the opaque identity (2026-08-18)

Decision: the checklist — and everything else — keys on whatever
`_partition_spw_ident` returns, rather than assuming an integer.

### `_spw_selected`

Compares identities directly, whatever kind they are. The previous version bailed
out on names because `SelectionSpec.spw` was assumed to hold parsed integers,
which made SPW filtering a **silent no-op on every xarray-ms store** (§8.25).

A stringified comparison is kept as a last resort, for a caller that resolved
against different metadata. Deliberately biased toward *keeping* the partition:
showing more data than asked for is the better failure direction — the user can
see it, whereas silently showing less looks like a correct plot of a smaller
dataset.

### `_parse_spw_string`

Now resolves against `meta.spws` and returns identities, in order:

1. exact match on the identity, stringified;
2. exact match on `name`;
3. case-insensitive **substring** of the name, so `spw='SW-01'` picks
   `ALMA_RB_07#BB_2#SW-01#FULL_RES` without typing the full ASDM name;
4. for a purely numeric token only, the **ordinal position** in `meta.spws`.

**Substring matching is skipped for numeric tokens.** `"0"` occurs inside
`ALMA_RB_07#BB_2#SW-01#FULL_RES`, so a bare digit matched a name by coincidence
and silently preempted step 4 — picking a window the user did not mean, with no
warning, because the match "succeeded". Caught by the test showing `spw='0'`
resolving without the positional warning it should have produced.

Step 4 is a last resort **and is logged**: `spw='0'` is what CASA habits produce,
and refusing it would be unhelpful, but a position is not a CASA `spw` id and on
a name-reporting store the real one is unrecoverable. Saying so beats silently
picking the wrong window.

An unmatched token is logged and skipped.

## 8.31 `test_spw_selection.py` — before building UI on this (2026-08-18)

**34 tests**, no MS, no backend, no datashader — all pure functions over
metadata, lifted by AST so the file runs in a bare environment and a failure
points at the logic rather than an import chain.

### Why, specifically

This logic has failed **silently twice**, and both times a plot appeared to
confirm it working:

* §8.4a — `SelectionSpec.spw` was populated by the constructor, the parser and
  the Plot button, and read by **no backend**.
* §8.25 — once the backends read it, they compared parsed *integers* against
  identities xarray-ms reports as *names*, so nothing matched and
  `_spw_selected` fell through to keeping every partition.

Neither was visible in a plot. Neither would have survived a test.

**And the test dataset cannot exercise it.** sis14 has one spectral window: with
one window, selecting it changes nothing and deselecting it is the empty case.
Fabricated multi-window metadata is the *only* check available short of another
MS — which is exactly why this had to be written before a UI is built on top.

### Coverage

`_partition_spw_ident` is parameterised over **both backends**, because the same
fix has landed in one and not the other three times (§8.7 arity, §8.11 DDID
qualifier, §8.27 kind resolution).

Selection matching pins the direction of the failure: an undeclared partition is
*kept*, because showing more data than asked for is visible while showing less
looks like a correct plot of a smaller dataset.

Parsing pins the numeric-substring hazard (§8.30): `"0"` occurs inside
`ALMA_RB_07#BB_2#SW-01#FULL_RES`, so a bare digit must not substring-match.

`SpwInfo.label()` pins that a single-channel WVR window renders as a real span
rather than collapsing — which is what makes it recognisable in the list.

### Note on package location

`_find_visplot()` walks *up* from the test file rather than indexing
`parents[n]`. The test tree's depth relative to the package is not a fact this
file should encode, and a wrong index produces a wall of `FileNotFoundError`
that says nothing about the actual problem — as it did on the first run here.

## 8.32 A `NameError` that compiled cleanly (2026-08-18)

`test_msv4_backend.py` and `test_visibility_raster.py` failed on an MSv4
processing set with `NameError: name 'freq_coords' is not defined` inside
`query_raster`.

**Cause, and it is mine.** The edit that added frequency-coordinate collection
(§8.28) did two things per backend: declare `freq_coords = []` and append to it.
The script asserted on MSv4's `_raster_2d` call site, which differs textually
from MSv2's, raised, and **never wrote MSv4**. I then patched the `append` into
MSv4 separately and did not notice the declaration had been lost with the
aborted write.

`py_compile` passed throughout: Python binds names at runtime, so a read-before-
assignment inside a method is invisible until that method runs. And it ran only
on the raster path, so the compositor suite and the scatter suite stayed green.

**Fixed** by adding the declaration. Verified structurally: in both backends
`freq_coords`' first textual appearance in `query_raster` is an assignment, and
`query_raster` reads no name it does not bind.

### Two process notes

**A failed multi-file edit leaves a partial state.** The script wrote MSv2, then
raised on MSv4. Nothing rolled back MSv2, and nothing flagged that MSv4 was now
half-edited by a *later* script. Multi-file edits should either write nothing on
failure or report which files were left untouched.

**Compilation is not a check for this class of error.** An AST scan for
names loaded but never bound within a function catches it. A general version
produces false positives on closure captures (nested functions reading enclosing
locals) and on module-level names assigned inside `try/except`, such as
`HAS_DASK` — so it is useful as a targeted check on a specific function rather
than as a blanket test.

## 8.33 Metadata key parity (2026-08-18)

`test_metadata_matches_msv2_keys` failed after `spws` was added to `metadata()`
(§8.29). Correct behaviour: the test asserts **exact** key equality between the
backends, and loosening it to a subset check would let a key go missing from one
of them unnoticed.

That assertion is one of the few cheap catches for backend divergence — the same
fix has landed in one backend and not the other three times (`_axis_to_dim`
arity §8.7, the DDID qualifier §8.11, the kind resolution §8.27). Its docstring
now says so, and says to add a key to both backends before updating the set.

Added alongside it: `test_spws_detail_is_consistent_with_spw_ids`. `spws` and
`spw_ids` are collected in the same loop but under different conditions —
`spw_ids` needs only an identity, `spws` also needs a frequency coordinate — so
they can diverge on a store where one partition lacks coordinates. The test pins
that the id lists match and that each window's numbers are self-consistent,
including that bandwidth spans channel *edges* so a single-channel window is
non-zero.

### The two suites disagreed about the contract

MSv2 *did* have an equivalent — and it used a **subset** check
(`required - keys`), which is why adding `spws` broke MSv4 and not MSv2. So the
suites disagreed about what the rule was, and **neither actually compared the
backends**: each compared one backend to its own hand-maintained list.

`reader.METADATA_KEYS` now holds the contract, and both suites assert against it.
Adding a key means editing the ABC — which is also where the key's meaning is
documented.

### A real asymmetry the strict check surfaced

MSv2 returns `field_ids`; MSv4 does not. Not drift — a **known gap**, with a
comment in `ObservationMetadata.from_backend_metadata` saying the positional
fallback is *wrong* whenever FIELD_IDs are non-contiguous, confirmed on a real
MS where alphabetically sorted `field_names` do not line up with FIELD_ID order.
Numeric `field=` selection against a Processing Set is unreliable until MSv4
grows an equivalent source.

Modelled as `METADATA_OPTIONAL_KEYS` rather than folded into the required set:
requiring it would fail MSv4 for a gap that cannot be closed here, and excluding
it would hide the asymmetry. A key belongs there **only while there is a
documented reason a backend cannot supply it** — so the set is a list of known
debts, not an escape hatch.

Verified by AST against both backends' `metadata()` returns: no missing required
keys, no undocumented extras.

## 8.34 SPW checklist — built 2026-08-18

§8.10's design, now implemented. `MultiSelect` replaced by a `DataTable`.

### Why a DataTable

An ASDM-imported MS carries 30 windows with non-contiguous ids, and **the id
alone does not say which is the science window**. The frequency span and channel
count do — a 1-channel window at 7 GHz is a WVR at a glance. `DataTable` also
provides scrolling and checkbox selection natively, where a `CheckboxGroup`
would need both hand-built.

Three columns: spectral window, GHz span, channel count.

### Identities are no longer round-tripped through text

The old path did `spw_sel.value.join(',')` and Python re-parsed it. The identity
is deliberately **opaque** — an int or a spectral-window name (§8.25) — so text
is the wrong carrier, and a name containing a comma would have broken it
outright.

The table's source holds `ident` in its own type, the Plot payload sends
`spw_ids` as a **list**, and `_build_selection` prefers those over parsing
`_spw_str`. The string form is kept for the constructor and older clients:
`spw=` records what was *asked for*, the list records what was *chosen*.

### No All / None buttons — the header checkbox already does it

§8.10 specified All and None buttons. Built, then **removed 2026-08-19**:
`DataTable`'s checkbox column puts a select-all toggle in the header row, which
does both jobs. The buttons duplicated it, and they were the one sidebar control
the theme restyle still did not reach — removing a redundant widget is a better
answer than styling one.

The distinction they were meant to carry survives regardless. **"None selected"
is a real state, not a synonym for "all"**: Plot refuses an empty selection
("Select at least one spectral window") rather than rendering. That costs nothing
because Plot is an explicit commit — unchecking everything is a transient step on
the way to checking two, so the empty case is only reachable by pressing Plot
deliberately.

Worth noting the design lesson: the buttons were specified before the widget was
chosen. Once the widget was a `DataTable` rather than a `CheckboxGroup`, half the
specified UI became redundant — and it took seeing it rendered to notice.

### Two references that needed retargeting

`self._spw_select` is now a `column` wrapping the label, the All/None row and
the table. Both existing consumers wanted a *widget*:

* `_focus_blur` attaches `MouseEnter`/`MouseLeave`, which a layout container does
  not emit — the hint would have silently never appeared.
* the theme restyle walks widgets, not containers.

Both now take `self._spw_table`. The sidebar layout still takes the column, which
is correct.

## 8.35 Two things the theme toggle never reached (2026-08-19)

Reported after the checklist landed: sidebar section headings (`Data`,
`Correlation`, `SPW`) read as disabled in **light** mode, and the new
`DataTable` stayed light-on-white in **dark** mode.

Same root cause, opposite directions: **styled once at construction with nothing
able to change it.** That is precisely the defect the restyle body's own comments
already describe, recurring in two new places.

### Section headings

Built as `Div(text="<span style='color:#cdd6f4;...'>")` — the colour baked into
inline HTML. Now created through a `_section()` helper that collects them into
`self._section_divs`; the restyle rewrites the inline colour with a regex against
`_SECTION_DARK`/`_SECTION_LIGHT`.

### `DataTable`

**Bokeh renders `DataTable` through SlickGrid's own DOM inside a shadow root**,
so none of the `.bk-input`-style rules that theme a `Select` or `TextInput`
apply. Adding `_spw_table` to the restyle's `widgets` list (§8.34) was therefore
not enough — it was never going to be.

`_DARK_TABLE_CSS` targets the SlickGrid classes directly
(`.slick-header-columns`, `.slick-viewport`, `.slick-row`, `.slick-cell`), and
the restyle swaps it in and out. `_LIGHT_TABLE_CSS` is deliberately **empty**:
SlickGrid's defaults are already a light theme, so light mode is the absence of
the override rather than a second sheet to maintain.

### The fix broke everything (2026-08-19)

The first attempt constructed the stylesheet in JS with
`new Bokeh.InlineStyleSheet({css: ...})`. **`Bokeh.InlineStyleSheet` is not a
constructor in that namespace**, and the resulting `TypeError` aborted the rest
of the restyle body — so the theme stopped changing *anywhere*, including the
toggle's own label. A one-line mistake looked like the whole feature had broken.

Two corrections:

**Both stylesheets are now constructed in Python** and passed in as models; the
callback only swaps which is attached. Nothing in the restyle body calls a Bokeh
JS constructor.

**Every block is wrapped in a `_step(name, fn)` helper** that catches and logs.
The body has grown by accretion — figures, info divs, sidebar, widgets, hint
divs, `path_div`, colormap histograms, icons, gear tabs, section headings, the
table — and every addition ran in the same try-less sequence, so any one of them
could take down all the others.

Completed 2026-08-19: six blocks now isolated (`section headings`, `spw table`,
`page background`, `figures`, `info divs`, `source path`). The five that already
had bare `try/catch` keep it, but **every catch now reports** under a
`[visplot theme] <block> failed:` prefix — one of them was
`catch(e) {}`, swallowing failures entirely, which is the same invisibility this
whole section is about.

Colour constants stay at top level, since every block reads them.

**The body is syntax-checked under node**, wrapped in a scope with every arg
stubbed. A brace error inside a Python string is otherwise invisible until the
toggle is clicked in a browser.

### The pattern, now four occurrences

A value fixed at construction that a later mechanism is expected to change:
§8.14 (chrome at startup), §8.16 (re-shaded images), §8.26 (axis labels), and now
these two. The restyle body is the single place that changes appearance after
construction, so **anything visual added to the sidebar has to be added to it in
the same change** — the two are one unit, and splitting them is what produces
this bug every time.

## 9. Remaining work

| # | Item | Blocked by | Notes |
|---|------|-----------|-------|
| 0 | ~~SPW partition filtering~~ | — | **DONE 2026-08-14** — see §8.4a |
| 1 | ~~`axis_info` backend half~~ | — | **DONE 2026-08-14** — see §8.5 |
| 1b | ~~`AxisInfo` front-end~~ | — | **DONE 2026-08-14** — see §8.6. *Except* `visibility_plotter.py` ~1650/~1730, below |
| 1c | ~~`visibility_plotter` j2p label responses~~ | — | **DONE 2026-08-18** — §8.26 |
| 1a | ~~`_format_probe` as the single coordinate renderer~~ | — | **DONE 2026-08-14** — §8.4d 1–3 all closed |
| 2 | ~~SI prefix in axis label~~ | — | **DONE 2026-08-17** — §8.21 |
| 2b | ~~Raise `tf.shade(min_alpha=...)`~~ | — | **DONE 2026-08-17** — §8.20, set to 90 |
| 2a | ~~Plot `Axis.CHANNEL` as a real index on the raster path~~ | — | **DONE 2026-08-18** — §8.28 |
| 2c | `query_columns` result cache keyed on `(x_axis, y_axes, selection)` | — | §8.27 — the accepted alternative to the affine remap; helps every axis toggle |
| 3 | Headless split of `VisibilityPlotter.__init__` | — | Plot half **done** (§8.12); plotter half remains. | Riskiest change; `__init__` opens `CommMgr`, opens a control channel, builds four panels, calls `_build_layout()`. Split into "resolve config + open data" (always) and "build GUI" (gated). `defer_initial_render` is the existing precedent |
| 4 | GUI colorbar checkbox + Bokeh `ColorBar` | — | See 9.1 |
| 5 | ~~Export button + viewport payload~~ | — | **DONE 2026-08-17** — §8.17 |
| 6 | Constructor per-slot kind (§2.2 gap) | — | Independent of export |
| 6a | ~~SPW checklist rework (`DataTable`)~~ | — | **DONE 2026-08-18** — §8.34 |
| 6d | ~~Theme-aware GUI chrome + p2j~~ | — | **DONE** §8.14/§8.16. Remaining: replace the Toggle with a Light/Dark `RadioButtonGroup` for readability |
| 6b | Resolve SPW *name* through SPECTRAL_WINDOW to an id | — | §8.25 — needs subtable access; blocks CASA-form `spw=N` output |
| 6e | MSv4 `field_ids` source | — | §8.33 — without it numeric `field=` against a Processing Set is unreliable |
| 6c | Multi-partition/single-SPW backend test case | — | The shape sis14 actually has; nothing pins it |
| 7 | ~~`_render` records degenerate cause~~ | — | **DONE 2026-08-17** — §8.22 |
| 8 | ~~Raster stores its viewport~~ | — | **DONE 2026-08-17** — §8.22 |
| 9 | ~~`TestProbeMultiLayer`~~ | — | **DONE 2026-08-17** — §8.18 |

### 9.1 GUI colorbar — asymmetry to know up front

In matplotlib all placements cost the same. In Bokeh they do not:

- `plot_left`/`plot_right` → `fig.add_layout(ColorBar(...), "left"|"right")`,
  trivial.
- Display-scope has **no Bokeh equivalent**: a separate narrow figure containing
  only a `ColorBar`, inserted into the layout, with its mapper, theme, and range
  kept in sync on every `update_scaling()` and every local-mode viewport change.

**Staging:** `plot_left`/`plot_right` in both GUI and PNG first; display-scope in
the PNG only; add to the GUI when someone asks. Better than shipping a GUI option
that silently means something different from the same-named export option.

Under `eq_hist` in local mode the mapper must be rebuilt on every
`update_scaling()` and viewport change, so the checkbox handler hooks the same
place `_composite_and_push()` already fires. The checkbox should be per-panel
state that `_panel_spec()` reports, so an export inherits the GUI setting.

Note the gear panel's histogram with the draggable `EditSpan` already conveys the
value distribution — most of what a colorbar says. The bar adds the ramp-to-value
mapping specifically. This argues for GUI colorbars defaulting **off** and PNG
defaulting **on**, since the PNG has no gear panel.

### 9.2 Export button — the state split

With no Bokeh server, `CustomJS`-set model properties never propagate back. GUI
state splits three ways:

- **Python already knows** (nothing to do): axes, quantity, polarization,
  selection, `_scaling*`, `_color_mode`, per-layer alpha, cached
  `_agg`/`_layer_dfs`.
- **Browser-only — must ship in the payload**: viewport per figure, layout radio,
  display-mode radio, panel order (`_display_order_source`).
- **Deliberately different**: theme.

The `CustomJS` handler should collect state and send it, not send a bare ping. It
already holds references to all four figures, so reading
`fig.x_range.start/end`, the two `RadioButtonGroup.active` values, and the order
source is a dozen lines.

**Which state wins:** render state, never pending widget values (§2.6).

**Where the file lands:** no Bokeh server means no save dialog, and in
JupyterLab-over-SSH the Python process is on a different machine than the browser.
Write server-side to a path from a toolbar `TextInput` (or derived from the MS
name) and report the resolved absolute path in the status bar. Streaming PNG
bytes back as a browser download is possible via base64 over the comm but is a
lot of payload for a convenience. Deferred by decision.

---

## 10. Files

### New
| File | Location |
|---|---|
| `panel_spec.py` | `cubevis/toolbox/visplot/` |
| `tick_format.py` | `cubevis/toolbox/visplot/` |
| `png_export.py` | `cubevis/toolbox/visplot/` |
| `test_tick_format.py` | `cubevis/tests/manual/visplot/` |
| `test_png_export.py` | `cubevis/tests/manual/visplot/` |

### Modified
| File | Change |
|---|---|
| `visibility_plot.py` | `_img_to_uint32` fix; `_json_num`; `_probe_envelope`; `_state_data` derives from `_panel_spec`; `_axis_flags`; `render_result`; `_bands_with_mappings` hook; imports `TICK_FORMATTER_JS` |
| `visibility_raster.py` | `_panel_spec`; `_shade_for_export`; `_crop_agg`; `_bands_with_mappings`; probe envelope |
| `visibility_scatter.py` | `_panel_spec`; `_shade_for_export` (with snapshot/restore); `_bands_with_mappings`; `_with_default_cmaps`; probe envelope; `_layer_reading_html` |
| `colormap_scaling.py` | `ScalarMapping` |
| `axes.py` | `AxisInfo` |
| `palettes.py` | **new** — theme-keyed colormap registry (§8.13) |
| `refresh.py` | **new** — RefreshLevel ladder (§8.16) |
| `reader.py` | `axis_info()` default on the ABC |
| `msv4_backend.py` | `_partition_spw_id`; `_spw_selected`; `_iter_visibility_partitions(selection)`; `axis_info()` |
| `local_visibility_reader.py` | forwards `axis_info()` |
| `msv2_backend.py` | same as MSv4 |
| `test_visibility_raster.py` | Byte-order assertions flipped; datashader/float/contiguity pins; probe envelope tests |
| `test_visibility_scatter.py` | Probe test rewritten against structure; envelope tests |

### Test counts
`test_png_export.py` 76 · `test_tick_format.py` (golden 25 + 2243 fuzzed under
node) · raster and scatter suites green.

---

## 11. Testing discipline worth carrying forward

0. **A Python-side model assignment is invisible without a Bokeh server.**
   Any handler that changes what is drawn must *return* the new data for the
   client to install. Assigning `ColumnDataSource.data` in Python succeeds,
   changes nothing on screen, and logs nothing — the most silent failure mode in
   this codebase, and it has now bitten twice (§8.7.3 axis labels, §8.16 theme
   re-shade).
1. **Assert on structure, not presentation.** The probe test broke twice on label
   format alone. Markup is not a contract.
2. **Assert on bboxes, not rendered pixel counts.** The spine overdraws
   boundaries; pixel counting produces false off-by-ones.
3. **The compositor needs no MS, no bokeh, no display.** It consumes a plain data
   structure, so its 76 tests run in seconds and cannot be blocked by a missing
   dataset. Preserve that property.
4. **Cross-runtime parity needs a harness, not discipline.** `node` is available;
   use it. 2243 fuzzed cases cost under a second.
5. **Synthetic fixtures miss things.** Defects 6, 7, and 8 were found only by
   exporting real data. A synthetic transparent corner was also misread as a UI
   element — fixtures should look like data, not like tests.
6. **Golden tables over ad-hoc assertions** for anything two implementations must
   agree on.
