"""_scatter_render.py
=====================
Shared scatter binning + shading pipeline for ``MSv2Backend`` and
``MSv4Backend``.

Relocated here (2026-09) from ``VisibilityScatter._shade_all_layers`` /
``histogram`` / ``_bands_with_mappings`` almost verbatim -- see
``ScatterRenderResult``'s docstring in ``reader.py`` for the full
rationale. In short: query_columns() was shipping up to ~30M raw rows
over the wire for a remote session, because binning and shading lived
widget-side and needed the raw DataFrame to do it. Moving both here
means only a small, bounded per-layer result crosses a process or wire
boundary -- for local and remote sessions alike, since
``LocalVisibilityReader`` and ``VisplotRemoteBackend`` both just
delegate to this same backend method either way.

Deliberately a standalone module rather than folded into
``msv2_backend.py``/``msv4_backend.py`` directly, and NOT duplicated
between them the way ``_decimate_agg`` is: this pipeline is roughly 5x
``_decimate_agg``'s size, touches no backend-specific partition/Dask
internals (pure numpy/pandas/Datashader operating on an already-built
DataFrame), and has much higher drift risk as two independently-edited
copies. Flagging this as a deliberate deviation from the
``_decimate_agg`` precedent, not an oversight -- happy to duplicate
instead if consistency with that precedent matters more than the drift
risk.

What stays client-side (``VisibilityScatter``), and why
---------------------------------------------------------
* **Per-pixel alpha collapse** (``layer_alpha = auto_alpha *
  lyr.alpha``) -- needs only ``ScatterLayerRender.n_in_view`` and the
  canvas pixel count, both tiny. Keeping this client-side is what
  keeps ``VisibilityScatter.set_alpha()`` a free, no-requery operation,
  exactly as it is today.  (Part 5a: a *categorical* layer is exempt from
  the density-based ``auto_alpha`` -- its occupied pixels are opaque and
  only the user's ``lyr.alpha`` applies.  See ``_priority_shade``.)
* **Porter-Duff compositing across layers** -- pure image-space math
  over the returned RGBA arrays; needs no raw data.

Package location
-----------------
``cubevis/cubevis/toolbox/visplot/data/_scatter_render.py``
"""

from __future__ import annotations

import math
from typing import NamedTuple, Optional

import numpy as np
import pandas as pd

try:
    import datashader as ds
    import datashader.reductions as ds_agg
    import datashader.transfer_functions as tf
    HAS_DATASHADER = True
except ImportError:
    HAS_DATASHADER = False

from .. import colormap_scaling as _cms
from .reader import (ScatterLayerSpec, ScatterLayerRender, COLORIZE_AXIS_COLUMNS,
                     OTHER_CATEGORY_LABEL, OTHER_CATEGORY_COLOR, OTHER_CATEGORY_ALPHA)

# Mirrors VisibilityScatter._MIN_ALPHA -- see that module's docstring
# for the measured rationale (Datashader's min_alpha=40 default makes a
# single-point pixel nearly invisible; 90 roughly doubles sparse
# visibility without flattening dense-region contrast).  CONTINUOUS layers
# only since Part 5a: categorical layers are fully opaque (see
# ``_priority_shade``) and no longer use a density alpha floor.
_MIN_ALPHA = 90

# ---------------------------------------------------------------------------
# Colorize-by-axis (Part 3, 2026-09)
# ---------------------------------------------------------------------------

CATEGORY_CAP = 20
"""Maximum distinct COLORS a colorize-by-axis layer shows at once.

Revised (2026-09, Part 3 second pass) after real measurement showed the
original "refuse over cap, ask the user to narrow" design doc §4.2
default would make the feature nearly unusable on large modern arrays
(ngVLA: up to 263 antennas) -- see ``_bin_categories`` for what replaced
it. What follows is why the number itself stays 20 even though the
FAILURE MODE changed completely.

This was never really a wire/message-overhead limit, despite how it may
have read -- the returned ``image`` is a flat H x W array regardless of
category count, and ``categories``/``category_colors``/
``category_members`` are tiny (a few hundred bytes even at K=263), so
none of what actually crosses a process or wire boundary scales with
this number. It measures two DIFFERENT things that both happen to
plateau around the same value:

1. **Legibility.** A legend with more than ~20 simultaneous swatches
   stops being something a person can actually use to tell colors
   apart -- this is a human-perception ceiling, not a data-size one,
   and does not move just because an array has more antennas.
   ``len(palettes.categorical_cmap())`` == 20 for the same reason
   (Bokeh's Category20).
2. **Rendering cost.** Measured directly (400x300 canvas, 1M rows,
   JIT-warmed, best-of-3): the categorical aggregation
   (``ds_agg.by``) costs ``~0.14ms/category`` and is cheap; shading it
   is the real cost and, with Datashader's own ``tf.shade(...,
   color_key=...)``, scaled at ``~1.5ms/category`` -- extrapolated to
   a 1920x1080 canvas at K=263 (ngVLA's real antenna count), that is
   **~6.8 SECONDS**, and the backing aggregation array alone
   (``4 bytes x pixels x K``, confirmed exactly this via measurement)
   is **~2.2 GB**, transient, PER LAYER. Both costs are driven
   entirely by ``canvas_pixels x K`` -- confirmed independent of how
   much data is actually being plotted (50K vs. 8M rows changed
   shading time by under 5%) -- so a sparse selection gets no discount
   just because it looks like it should be cheap.

``_bin_categories`` below bounds real cardinality down to at most this
many groups before anything is aggregated or shaded, which is what
actually keeps cost bounded now -- not a refusal. Since cost is driven
by K, not by which K values happen to be present, capping K at a
constant makes cost independent of true antenna count: a 26-antenna MS
and an ngVLA 263-antenna one cost the same to render, once binned. See
``_priority_shade`` for the other half of the cost fix (replacing
Datashader's per-category blend with a single-pass one-color-per-pixel
pick, measured 4-9x faster at this same cardinality range,
independently of the binning decision).
"""


def _category_sort_key(value: str):
    """Sort key that orders numeric-looking strings numerically and
    falls back to plain lexicographic order otherwise.

    Every colorize-by-axis column is string-valued by the time this
    runs (see ``_resolve_categories``), including ones that are
    "numbers as strings" -- ``scan_name`` ("2", "10", "17") and ``spw``
    (str-normalized from a possibly-int identity, see that function's
    docstring). Plain string sorting would put "10" before "2"; this
    keeps scan/SPW legends in the order an astronomer expects while
    still doing something reasonable for genuinely non-numeric values
    (antenna names like "DA42", polarization labels like "XX") by
    falling back to string order for those. The ``(0, ...)``/``(1,
    ...)`` tuple prefix keeps every numeric-looking value sorted before
    every non-numeric one, rather than interleaving by coincidence of
    Python's cross-type comparison rules (which would raise anyway --
    float and str aren't orderable against each other).

    Also the ordering ``_bin_categories`` groups contiguously, so a
    bin's ``"lo\u2013hi"`` label is meaningful (a genuine contiguous
    range in this order) rather than an arbitrary pair of values that
    happened to land in the same bucket.
    """
    try:
        return (0, float(value))
    except (TypeError, ValueError):
        return (1, value)


def _bin_categories(distinct: list[str], cap: int) -> dict[str, tuple[str, ...]]:
    """Group *distinct* (already sorted by ``_category_sort_key``) into
    at most *cap* contiguous, near-equal-sized buckets.

    Returns an ordered ``{display_label: (raw_value, ...)}`` mapping --
    ordered because it's built by walking *distinct* once, front to
    back, so iterating it later reproduces sort order with no second
    sort needed. When ``len(distinct) <= cap`` every bucket is a
    trivial singleton (``{v: (v,)}`` for each *v*) -- callers should
    NOT special-case "was this binned?"; ``len(category_members[label])
    == 1`` already answers that per-category, and treating the trivial
    case as "zero buckets of size 1" rather than "no buckets" keeps one
    code path instead of two.

    A label is the single value itself for a singleton bucket, or
    ``"{first}\u2013{last}"`` (en dash, matching this codebase's own
    range-formatting convention -- see ``VisibilityScatter._rect_title``)
    for a multi-value one. Buckets are built by even integer division
    with the remainder spread across the FIRST few buckets (standard
    "as-equal-as-possible" partition of *n* items into *cap* groups) --
    given *distinct* has no duplicates (it's built from a Python
    ``set``) and ``len(distinct) > cap`` whenever this path actually
    runs, every bucket is guaranteed non-empty and every multi-value
    bucket's first and last members are genuinely distinct, so no
    label collision is possible.
    """
    n = len(distinct)
    if n <= cap:
        return {v: (v,) for v in distinct}

    base, extra = divmod(n, cap)
    members: dict[str, tuple[str, ...]] = {}
    start = 0
    for i in range(cap):
        size = base + (1 if i < extra else 0)
        group = tuple(distinct[start:start + size])
        start += size
        label = group[0] if len(group) == 1 else f"{group[0]}\u2013{group[-1]}"
        members[label] = group
    return members


class _Categorization(NamedTuple):
    """Everything ``render_layer`` needs to know about one layer's categories.

    ``bucket`` is the per-row display-category index (an ``int32`` array,
    ``len(df)`` long); ``-1`` marks a row that is not drawn -- either its
    value is missing (NaN/None) or the user excluded it.  Carrying integer
    codes instead of the per-row strings is the whole point of this type:
    every later step (shading, the legend, the rarity ordering) then works
    on small integer arrays, and the only place a string is ever touched
    is the handful of *distinct* values.

    ``population`` is the number of drawn rows per category over the WHOLE
    current selection -- deliberately not the viewport, so anything derived
    from it (the ``"rarest"`` draw order) cannot change when the user pans
    or zooms.  Exactly one of ``categories``/``skip_reason`` is ``None``.

    ``other_index`` (Part 5b): index into ``categories`` of the gray
    "Other (not selected)" group, or ``None`` when there isn't one.  When
    present it is always the LAST category, and ``members``/``population``
    have an entry for it like any other -- see ``EXCLUDED_DISPLAYS``.
    """
    bucket:      Optional[np.ndarray]
    categories:  Optional[list]
    members:     Optional[dict]
    population:  Optional[np.ndarray]
    skip_reason: Optional[str]
    other_index: Optional[int] = None


def _no_categories(reason: str) -> _Categorization:
    return _Categorization(None, None, None, None, reason)


def _categorize(
    df: pd.DataFrame, column: str, axis_label: str, cap: int = CATEGORY_CAP,
    excluded: Optional[frozenset] = None, show_excluded: bool = False,
) -> _Categorization:
    """Resolve *column* into display categories, per-row codes and populations.

    Replaces the pre-Part-5a implementation, which ran ``str(v)``,
    ``astype(str)``, ``Series.map`` and ``pd.Categorical(strings)`` over
    every row of the DataFrame -- four Python-level passes measured at
    ~2.6 s per 4M rows (11-13x the whole continuous render), none of it
    Datashader.  Here ``pd.factorize`` hashes each row exactly once, in C,
    and every step after it (``str()``, exclusion, sorting, binning, the
    label lookup) runs over the *K distinct values*, not the N rows.  The
    result is identical -- same categories, same order, same colors, same
    pixels (checked image-for-image on real data, with and without
    exclusions) -- only cheaper.

    Behavior preserved deliberately:

    * A missing *column*, or one with no non-null value, yields a skip
      reason (same wording as before) rather than raising.
    * Rows whose value is missing are never drawn.  This is a correctness
      requirement, not tidiness: **Datashader's ``ds_agg.by()`` silently
      folds a NaN-coded category into the LAST real category's count**
      (confirmed directly on a 20-row synthetic column: counts came out
      ``[5, 15]`` instead of ``[5, 5]``), inflating that category's weight.
      Such rows get bucket ``-1`` and are filtered out before ``by()`` ever
      sees the column.
    * Values are string-normalized (``str(v)``) before comparison, which is
      also how a mixed ``int``/``str`` ``spw`` identity (an int DDID on one
      partition, a name on another) merges into one category instead of
      splitting: two distinct raw values with the same ``str()`` map to the
      same bucket here.
    * *excluded* (raw values, not post-binning labels) is applied BEFORE
      binning, so an excluded value never contributes to a bucket -- correct
      even when the exclusion changes which values share one.
    * Categories, members and colors come from the FULL per-layer *df* (the
      whole selection), never the viewport, so a category keeps its color
      while the user pans and zooms.
    * *show_excluded* (Part 5b, ``excluded_display == "gray"``): instead of
      dropping the excluded rows, gather them into one extra LAST category,
      ``OTHER_CATEGORY_LABEL``.  It exists only if some excluded value is
      actually present, is never binned into the real categories (exclusion
      still happens before binning), and does not count toward *cap*.  With
      nothing left highlighted the real categories are simply empty and the
      layer is all gray -- a valid, useful render, not the "all excluded"
      skip that hiding everything is.
    """
    if column not in df.columns:
        return _no_categories(f"no {axis_label} data for this selection")

    col = df[column]
    if isinstance(col.dtype, pd.CategoricalDtype):
        # Part 5b: Field and Baseline arrive as pandas Categoricals (small
        # integer codes + a shared category list; see
        # ``XArrayReader._identity_categoricals``).  Read the codes directly
        # -- no hashing, no strings -- and keep only the categories this
        # selection actually contains: a category that is in the shared list
        # but has no rows must not show up in the legend.
        cat_codes = col.cat.codes.to_numpy()                 # -1 == missing
        cats = col.cat.categories
        counts = np.bincount(cat_codes[cat_codes >= 0], minlength=len(cats))
        present = np.flatnonzero(counts)
        remap = np.full(len(cats) + 1, -1, dtype=np.int64)   # last slot <- code -1
        remap[present] = np.arange(len(present))
        codes = remap[cat_codes]
        uniques = cats.take(present)
    else:
        codes, uniques = pd.factorize(col.to_numpy(), use_na_sentinel=True)
    if len(uniques) == 0:
        return _no_categories(f"no {axis_label} data for this selection")

    u_str = [str(u) for u in uniques]                      # K values, not N rows
    keep = np.ones(len(u_str), dtype=bool)
    if excluded:
        excl = set(excluded)
        keep = np.array([s not in excl for s in u_str], dtype=bool)
        if not keep.any() and not show_excluded:
            return _no_categories(f"all {axis_label} categories excluded")

    distinct = sorted({s for s, k in zip(u_str, keep) if k},
                      key=_category_sort_key)
    members = _bin_categories(distinct, cap)
    categories = list(members)
    label_index = {label: i for i, label in enumerate(categories)}
    value_index = {v: label_index[label]
                   for label, group in members.items() for v in group}

    other_index = None
    if show_excluded and not keep.all():
        other_index = len(categories)
        categories.append(OTHER_CATEGORY_LABEL)
        members[OTHER_CATEGORY_LABEL] = tuple(sorted(
            (s for s, k in zip(u_str, keep) if not k), key=_category_sort_key))

    # Lookup table from factorize()'s code -> display-category index.  One
    # extra trailing slot so factorize's -1 (missing) indexes it too (Python
    # negative indexing lands on the last element) and comes out as -1.
    lut = np.full(len(u_str) + 1, -1, dtype=np.int32)
    for k, s in enumerate(u_str):
        if keep[k]:
            lut[k] = value_index[s]
        elif other_index is not None:
            lut[k] = other_index
    bucket = lut[codes]

    population = np.bincount(bucket[bucket >= 0], minlength=len(categories))
    return _Categorization(bucket, categories, members, population, None, other_index)


def _resolve_categories(
    df: pd.DataFrame, column: str, axis_label: str, cap: int = CATEGORY_CAP,
    excluded: Optional[frozenset] = None,
) -> tuple[Optional[np.ndarray], Optional[list[str]],
           Optional[dict[str, tuple[str, ...]]], Optional[str]]:
    """Boolean row-mask + display categories + raw-value membership for
    colorize-by-axis, or a skip reason if the data can't support it at all.

    Thin wrapper over ``_categorize`` (which carries the full reasoning and
    is what ``render_layer`` calls), kept because its return shape is the
    stable, tested contract: ``(mask, categories, category_members,
    skip_reason)`` -- exactly one of ``categories``/``skip_reason`` is not
    ``None``, and ``mask``/``category_members`` are ``None`` iff
    ``skip_reason`` is not.  ``categories`` is ``list(category_members)``;
    ``mask`` selects the drawn rows (present and not excluded).

    Exceeding *cap* is not a failure: the distinct values are grouped into
    at most *cap* contiguous buckets (see ``_bin_categories``).
    """
    cat = _categorize(df, column, axis_label, cap=cap, excluded=excluded)
    if cat.skip_reason is not None:
        return None, None, None, cat.skip_reason
    return cat.bucket >= 0, cat.categories, cat.members, None


def _hex_to_rgb_uint8(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _priority_shade(
    agg, categories: list[str], cmap: tuple[str, ...], priority: str,
    population: Optional[np.ndarray], other_index: Optional[int] = None,
) -> tuple[np.ndarray, dict[str, str]]:
    """Shade a per-pixel category-count aggregation: one color per pixel,
    chosen by *priority*, fully opaque.

    Never a blend.  Every occupied pixel is exactly one of *cmap*'s colors
    (and every empty pixel is the all-zero, fully transparent value), which
    is what lets a legend say "this color means that category" and be right.
    Datashader's own categorical shading (``tf.shade(color_key=...)``) blends
    a mixed pixel proportionally, giving a color that matches no swatch, and
    is also 4-9x slower at real cardinalities -- see this module's history.

    *priority* -- ``"rarest"`` or ``"majority"``, see
    ``data.reader.CATEGORY_PRIORITIES``:

    * ``"majority"``: the category with the most samples in the pixel
      (``argmax`` of the counts; ties go to the lower-sorted category).
    * ``"rarest"``: the category with the smallest *population* -- rows over
      the whole selection, from ``_categorize`` -- among those present in the
      pixel.  Implemented as a first-hit over the category axis reordered
      rarest-first, so it is one boolean pass (about the cost of the argmax
      above; measured within a few percent either way).  Ties in population
      go to the lower-sorted category, so the result is deterministic.

    Opacity (Part 5a, 2026-09): occupied pixels are alpha 255, not the
    histogram-equalized density alpha this function used to compute.  That
    alpha came from the pixel's TOTAL count, so a lone sample of a rare
    category -- the very thing ``"rarest"`` exists to show -- was drawn
    faintest.  Density is the continuous mode's job ("how much"); a
    categorical layer answers "which".  The client applies the user's own
    layer alpha on top (``VisibilityScatter._collapse_and_composite``) and
    deliberately skips the density-based ``auto_alpha`` for these layers.

    *categories* must be in the same order used to build *agg*'s categorical
    dimension, and *population* (rows per category) in that same order.

    *other_index* (Part 5b): index of the gray "Other (not selected)"
    category, always last.  It is context, not a competitor: a pixel that
    holds ANY real category shows that category, however many gray samples
    share it, in BOTH priorities (the reason it cannot simply be one more
    channel of the argmax / rarity order).  A pixel with only gray samples
    is drawn in ``OTHER_CATEGORY_COLOR`` at ``OTHER_CATEGORY_ALPHA``, so the
    context recedes behind the opaque highlighted colors.
    """
    counts = agg.values                       # (H, W, K [+1 gray]), count dtype
    n_real = other_index if other_index is not None else counts.shape[-1]
    real = counts[..., :n_real]
    present = real > 0
    h, w = counts.shape[:2]
    nonempty = present.any(axis=-1) if n_real else np.zeros((h, w), dtype=bool)
    other_only = (
        (counts[..., other_index] > 0) & ~nonempty
        if other_index is not None else np.zeros((h, w), dtype=bool)
    )

    img = np.zeros((h, w), dtype=np.uint32)
    color_key = {cat: cmap[i % len(cmap)] for i, cat in enumerate(categories)}
    if other_index is not None:
        color_key[categories[other_index]] = OTHER_CATEGORY_COLOR

    if nonempty.any():
        if priority == "majority":
            pick = real.argmax(axis=-1)
        else:
            if population is None:            # defensive; _categorize always sets it
                population = real.reshape(-1, n_real).sum(axis=0)
            order = np.argsort(population[:n_real], kind="stable")   # rarest first
            pick = order[present[..., order].argmax(axis=-1)]        # first present

        palette_rgb = np.array(
            [_hex_to_rgb_uint8(cmap[i % len(cmap)]) for i in range(n_real)],
            dtype=np.uint32,
        )
        rgb = palette_rgb[pick[nonempty]]                    # occupied pixels only
        opaque = np.uint32(255) << np.uint32(24)
        img[nonempty] = (
            opaque | (rgb[:, 2] << np.uint32(16)) |
            (rgb[:, 1] << np.uint32(8)) | rgb[:, 0]
        )
    if other_only.any():
        r, g, b = _hex_to_rgb_uint8(OTHER_CATEGORY_COLOR)
        img[other_only] = (
            (np.uint32(OTHER_CATEGORY_ALPHA) << np.uint32(24))
            | (np.uint32(b) << np.uint32(16)) | (np.uint32(g) << np.uint32(8))
            | np.uint32(r)
        )
    return img, color_key


def _shade_categorical(
    df: pd.DataFrame, cat: _Categorization, cmap: tuple[str, ...],
    cvs: "ds.Canvas", priority: str,
) -> tuple[np.ndarray, dict[str, str]]:
    """Bin + shade one layer's colorize-by-axis image from its ``_Categorization``.

    Colors are assigned by cycling *cmap* modulo its length across
    ``cat.categories`` in their given (already-sorted, already-bucketed)
    order -- the same "index modulo" convention ``palettes.scatter_cmaps()``
    documents for per-layer ramp assignment, applied to categories, so a
    count exceeding the color set degrades to repeated colors rather than
    raising (in practice it never triggers: ``_bin_categories`` caps the
    count at ``CATEGORY_CAP`` and ``palettes.categorical_cmap()`` provides
    that many colors; kept as a safety net).

    Only rows with ``bucket >= 0`` reach ``cvs.points()`` -- see
    ``_categorize`` for why a NaN-category row must never get to
    ``ds_agg.by()``.  The categorical column is built straight from the
    integer codes (``Categorical.from_codes``), never from strings.
    """
    x = df["x"].to_numpy()
    y = df["y"].to_numpy()
    bucket = cat.bucket
    keep = bucket >= 0
    if not keep.all():                        # skip three full-length copies when unneeded
        x, y, bucket = x[keep], y[keep], bucket[keep]
    df_cat = pd.DataFrame({
        "x": x, "y": y,
        "__category__": pd.Categorical.from_codes(bucket, categories=cat.categories),
    })
    agg = cvs.points(df_cat, "x", "y", ds_agg.by("__category__", ds_agg.count()))
    return _priority_shade(agg, cat.categories, cmap, priority, cat.population,
                           cat.other_index)


def compute_canvas_size(
    dataframes: dict, layers: list[ScatterLayerSpec],
    x0: float, x1: float, y0: float, y1: float,
    width: int, height: int,
) -> tuple[int, int]:
    """Adaptive canvas size for sparse data.

    Ported verbatim from ``VisibilityScatter._compute_canvas_size`` --
    see that method's (pre-redesign) docstring for the ``pts_per_px``
    rationale. Excludes hidden (``alpha <= 0``) layers from the count,
    matching the original exactly.
    """
    total_in_view = 0
    for lyr in layers:
        if lyr.alpha <= 0.0:
            continue
        df = dataframes.get((lyr.y_axis, lyr.polarization))
        if df is None or len(df) == 0:
            continue
        total_in_view += int(
            ((df["x"] >= x0) & (df["x"] <= x1) &
             (df["y"] >= y0) & (df["y"] <= y1)).sum()
        )
    pts_per_px = total_in_view / (width * height)
    if pts_per_px < 0.01 and total_in_view > 0:
        scale = max(0.05, math.sqrt(
            total_in_view / (width * height * 0.01)
        ))
        return max(10, int(width * scale)), max(10, int(height * scale))
    return width, height


def _id_grid_size(
    canvas_w: int, canvas_h: int, max_cells: int,
) -> tuple[int, int]:
    """(width, height) for the coarse identity grid, bounded to at most
    ``max_cells`` total cells while matching the *display canvas's*
    screen aspect ratio.

    BUG FIX (2026-09): the first version of this function computed
    aspect ratio from the data's own (x1-x0)/(y1-y0) spans -- which is
    wrong whenever x and y are different physical quantities (e.g. Time
    in seconds vs. Amplitude in Jy, ratio in the thousands), since
    their raw numeric spans have nothing to do with the canvas's actual
    screen-pixel geometry. Confirmed in practice: a ~5400s Time span
    against a ~130 Jy Amplitude span produced a ~341x9 grid instead of
    anything resembling the roughly-square display canvas, making each
    coarse cell ~2.6 screen-px wide but ~106 screen-px tall -- a hover
    probe's "search a little further for a barely-missed point"
    tolerance (see _handle_probe -- REMOVED for the id grid specifically
    as of this same fix, see that method) then computed its search
    radius from the *smallest* bin dimension, letting a "miss" search
    hundreds of screen pixels in the tall direction and report data
    from a completely different, visually unrelated region as though it
    were "nearby". Using the canvas's own screen aspect ratio instead
    keeps id grid cells roughly square in screen space, matching what a
    user actually sees, regardless of what physical units x and y are
    in.
    """
    aspect = max(canvas_w, 1) / max(canvas_h, 1)
    h = max(1, int(round(math.sqrt(max_cells / max(aspect, 1e-9)))))
    w = max(1, int(round(max_cells / h)))
    return w, h


def _empty_render(canvas_h: int, canvas_w: int, reason: str) -> ScatterLayerRender:
    return ScatterLayerRender(
        image=np.zeros((canvas_h, canvas_w), dtype=np.uint32),
        n_in_view=0, skip_reason=reason, peak_value=None,
        hist_counts=None, hist_edges=None, mapping_x=None, mapping_u=None,
    )


def render_layer(
    df: Optional[pd.DataFrame], lyr: ScatterLayerSpec,
    x0: float, x1: float, y0: float, y1: float,
    canvas_w: int, canvas_h: int,
    color_mode: str, full_y_range: tuple[float, float],
    probe_grid_max_cells: int = 3072,
) -> ScatterLayerRender:
    """Bin + shade one layer.

    Ported from ``VisibilityScatter._shade_all_layers``'s per-layer
    body (pre-redesign) -- see that method for the line-by-line
    precedent this mirrors. Stops short of alpha-channel collapse and
    cross-layer compositing -- see this module's docstring.

    CORRECTION (2026-09, post-chunk-1): does NOT skip shading when
    ``lyr.alpha == 0.0``, unlike the very first version of this
    function. A hidden layer still needs a real cached image: toggling
    visibility back on happens via ``VisibilityScatter.set_alpha()``,
    which by design makes no backend call and can only work with
    whatever image is already cached -- there is nothing to un-hide if
    the backend never bothered to shade it. ``compute_canvas_size``
    still excludes ``alpha <= 0`` layers from its density estimate
    (that's a free, canvas-sizing-only decision with no such
    asymmetry). The widget now derives "hidden" purely from its own
    live ``lyr.alpha`` at composite time -- see
    ``VisibilityScatter._collapse_and_composite``.

    ADDITION (2026-09, hover-probe redesign piece 2): also computes a
    second, much coarser per-bin native-coordinate-range grid (see
    ``ScatterLayerRender.id_grid_*``'s docstring) via a SEPARATE
    ``Canvas.points()`` call at ``_id_grid_size(..., probe_grid_max_cells)``
    resolution, using Datashader's ``summary()`` to compute all six
    min/max reductions (time, baseline_id, frequency) in one aggregation
    pass rather than six separate ones. Requires ``df`` to carry
    "time"/"baseline_id"/"frequency" columns alongside "x"/"y" --
    conditional per-column, so a caller that only populates a subset
    (or none, e.g. during a transition) still gets a valid render, just
    with the corresponding ``id_grid_*`` fields left ``None``.

    ADDITION (2026-09, Part 3, colorize-by-axis): when
    ``lyr.coloring == "categorical"``, replaces the continuous
    mean(y)/eq_hist/colorbar pipeline above with categorical
    aggregation over ``lyr.colorize_axis``'s per-row column (see
    ``_categorize``/``_shade_categorical``/``_priority_shade``)
    and populates ``categories``/``category_colors``/
    ``category_members`` instead of ``peak_value``/``hist_counts``/
    ``hist_edges``/``mapping_x``/``mapping_u`` (left ``None``) -- the
    two modes are mutually exclusive per the design doc §4.3, not
    layered together. Real cardinality beyond ``CATEGORY_CAP`` is
    handled by grouping into contiguous buckets (see
    ``_bin_categories``), not by refusing -- there is no longer a
    "too many categories" skip reason; the hover-probe id grid below
    is unaffected either way, since it bins on
    "time"/"baseline_id"/"frequency"/mean("y"), none of which depend
    on the coloring mode.
    """
    if not HAS_DATASHADER:
        raise ImportError(
            "datashader is required for VisibilityScatter's rendering "
            "path.\nInstall: pip install datashader"
        )
    if df is None:
        return _empty_render(canvas_h, canvas_w, "not queried")
    if len(df) == 0:
        return _empty_render(canvas_h, canvas_w, "query returned 0 rows")

    in_view = (
        (df["x"] >= x0) & (df["x"] <= x1) &
        (df["y"] >= y0) & (df["y"] <= y1)
    )
    n_in_view = int(in_view.sum())
    if n_in_view == 0:
        return _empty_render(
            canvas_h, canvas_w, f"0 of {len(df)} samples in viewport")

    cvs = ds.Canvas(
        plot_width=canvas_w, plot_height=canvas_h,
        x_range=(x0, x1), y_range=(y0, y1),
    )

    # ---- colorize-by-axis (Part 3, 2026-09) ------------------------- #
    # A full mode split, not a tweak to the continuous path below: per
    # the design doc §4.3, the two coloring modes are mutually
    # exclusive per layer, and a categorical layer has no continuous
    # colorbar/histogram/eq_hist mapping to compute at all (those
    # fields stay None on the returned ScatterLayerRender -- Part 4's
    # categorical legend widget replaces them, it doesn't sit next to
    # them).
    if lyr.coloring == "categorical":
        column = COLORIZE_AXIS_COLUMNS[lyr.colorize_axis]
        cat = _categorize(
            df, column, lyr.colorize_axis.label,
            excluded=frozenset(lyr.excluded_categories) or None,
            show_excluded=(lyr.excluded_display == "gray"),
        )
        if cat.skip_reason is not None:
            return _empty_render(canvas_h, canvas_w, cat.skip_reason)
        img_arr, category_colors = _shade_categorical(
            df, cat, lyr.cmap, cvs, lyr.category_priority,
        )
        categories_out = tuple(cat.categories)
        category_members_out = cat.members
        peak_value = hist_counts = hist_edges = None
        mapping_x = mapping_u = None
    else:
        agg = cvs.points(df, "x", "y", ds_agg.mean("y"))

        # Reference population for eq_hist / colorbar / histogram is the
        # TRUE per-sample y-values, not the binned agg -- running here,
        # where the DataFrame still exists, is what makes that possible
        # without ever shipping them anywhere. Mirrors
        # VisibilityScatter._shade_all_layers' color_mode branch exactly.
        if color_mode == "local":
            visible_y = df.loc[in_view, "y"]
            if len(visible_y) > 0:
                span = [float(visible_y.min()), float(visible_y.max())]
                eq_reference = visible_y.to_numpy()
            else:
                span = [float(full_y_range[0]), float(full_y_range[1])]
                eq_reference = None
        else:  # "global"
            span = [float(full_y_range[0]), float(full_y_range[1])]
            eq_reference = df["y"].to_numpy()

        if lyr.scaling_vmin is not None and lyr.scaling_vmax is not None:
            span = [lyr.scaling_vmin, lyr.scaling_vmax]

        cmap = list(lyr.cmap)
        if lyr.scaling in _cms.DATASHADER_HOW:
            shade_kwargs = dict(
                cmap=cmap, how=_cms.DATASHADER_HOW[lyr.scaling],
                min_alpha=_MIN_ALPHA,
            )
            if span is not None:
                shade_kwargs["span"] = span
            img = tf.shade(agg, **shade_kwargs)
        elif lyr.scaling == "eq_hist":
            eq_ref = eq_reference
            if lyr.scaling_vmin is not None or lyr.scaling_vmax is not None:
                pool = eq_ref if eq_ref is not None else agg.values
                pool_finite = pool[np.isfinite(pool)]
                lo = lyr.scaling_vmin if lyr.scaling_vmin is not None else (
                    float(pool_finite.min()) if pool_finite.size else None)
                hi = lyr.scaling_vmax if lyr.scaling_vmax is not None else (
                    float(pool_finite.max()) if pool_finite.size else None)
                if lo is not None and hi is not None and hi > lo:
                    in_band = pool_finite[(pool_finite >= lo) & (pool_finite <= hi)]
                    if in_band.size > 0:
                        eq_ref = in_band
            transformed = _cms.equalize_histogram(agg.values, reference=eq_ref)
            scaled_agg = agg.copy(data=transformed)
            img = tf.shade(
                scaled_agg, cmap=cmap, how="linear",
                span=[0.0, 1.0], min_alpha=_MIN_ALPHA,
            )
        else:
            transformed = _cms.apply_explicit_scaling(
                agg.values, lyr.scaling, alpha=lyr.scaling_alpha,
                gamma=lyr.scaling_gamma,
                vmin=span[0] if span is not None else None,
                vmax=span[1] if span is not None else None,
            )
            scaled_agg = agg.copy(data=transformed)
            img = tf.shade(
                scaled_agg, cmap=cmap, how="linear",
                span=[0.0, 1.0], min_alpha=_MIN_ALPHA,
            )

        img_arr = np.array(img, dtype=np.uint32)

        finite_agg = agg.values[np.isfinite(agg.values)]
        peak_value = float(finite_agg.max()) if finite_agg.size else None

        hist_counts = hist_edges = None
        ref_for_hist = eq_reference if eq_reference is not None else agg.values
        ref_finite = np.asarray(ref_for_hist)
        ref_finite = ref_finite[np.isfinite(ref_finite)]
        if ref_finite.size:
            hist_counts, hist_edges = np.histogram(ref_finite, bins=254)

        mapping_x = mapping_u = None
        mapping = _cms.ScalarMapping.from_values(
            agg.values, lyr.scaling, reference=eq_reference,
            alpha=lyr.scaling_alpha, gamma=lyr.scaling_gamma,
            vmin=lyr.scaling_vmin, vmax=lyr.scaling_vmax,
        )
        if mapping is not None:
            mapping_x, mapping_u = mapping.curve

        categories_out = None
        category_colors = None
        category_members_out = None

    # ---- hover-probe redesign piece 2: coarse identity grid -------- #
    # A second, separate (much coarser) Canvas.points() pass -- see
    # this function's docstring and _id_grid_size for why it can't just
    # reuse the display `agg` above. Conditional per native-coordinate
    # column: a caller (or an older DataFrame construction path mid
    # transition) that hasn't populated one of "time"/"baseline_id"/
    # "frequency" simply doesn't get that pair of id_grid_* fields,
    # rather than failing the whole render.
    id_grid_t_lo = id_grid_t_hi = None
    id_grid_bl_lo = id_grid_bl_hi = None
    id_grid_freq_lo = id_grid_freq_hi = None
    id_grid_value = None
    id_cols = [c for c in ("time", "baseline_id", "frequency") if c in df.columns]
    if id_cols or True:
        # "or True": id_grid_value (the coarse mean reading) only needs
        # x/y, which always exist -- so the coarse grid is still worth
        # computing even when none of the three identity columns made
        # it into df (e.g. mid-transition), just with only the value
        # field populated and all six range fields left None.
        id_w, id_h = _id_grid_size(canvas_w, canvas_h, probe_grid_max_cells)
        id_cvs = ds.Canvas(
            plot_width=id_w, plot_height=id_h,
            x_range=(x0, x1), y_range=(y0, y1),
        )
        summary_kwargs = {"val": ds_agg.mean("y")}
        if "time" in id_cols:
            summary_kwargs["t_lo"] = ds_agg.min("time")
            summary_kwargs["t_hi"] = ds_agg.max("time")
        if "baseline_id" in id_cols:
            summary_kwargs["bl_lo"] = ds_agg.min("baseline_id")
            summary_kwargs["bl_hi"] = ds_agg.max("baseline_id")
        if "frequency" in id_cols:
            summary_kwargs["f_lo"] = ds_agg.min("frequency")
            summary_kwargs["f_hi"] = ds_agg.max("frequency")
        # One aggregation pass computes all requested reductions
        # together (Datashader's ds.summary()), not one pass per
        # reduction -- seven reductions here cost the same single
        # vectorized pass over df as the one-reduction display agg
        # above, just a bigger (still tiny, ~id_w*id_h*7 float64s) output.
        id_agg = id_cvs.points(df, "x", "y", ds_agg.summary(**summary_kwargs))
        id_grid_value = id_agg["val"].values
        if "time" in id_cols:
            id_grid_t_lo = id_agg["t_lo"].values
            id_grid_t_hi = id_agg["t_hi"].values
        if "baseline_id" in id_cols:
            id_grid_bl_lo = id_agg["bl_lo"].values
            id_grid_bl_hi = id_agg["bl_hi"].values
        if "frequency" in id_cols:
            id_grid_freq_lo = id_agg["f_lo"].values
            id_grid_freq_hi = id_agg["f_hi"].values

    return ScatterLayerRender(
        image=img_arr, n_in_view=n_in_view, skip_reason=None,
        peak_value=peak_value, hist_counts=hist_counts, hist_edges=hist_edges,
        mapping_x=mapping_x, mapping_u=mapping_u,
        id_grid_t_lo=id_grid_t_lo, id_grid_t_hi=id_grid_t_hi,
        id_grid_bl_lo=id_grid_bl_lo, id_grid_bl_hi=id_grid_bl_hi,
        id_grid_freq_lo=id_grid_freq_lo, id_grid_freq_hi=id_grid_freq_hi,
        id_grid_x_range=(x0, x1), id_grid_y_range=(y0, y1),
        id_grid_value=id_grid_value,
        categories=categories_out, category_colors=category_colors,
        category_members=category_members_out,
    )
