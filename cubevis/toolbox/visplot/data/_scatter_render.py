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

try:
    from scipy.ndimage import minimum_filter, maximum_filter
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

from .. import colormap_scaling as _cms
from .reader import (ScatterLayerSpec, ScatterLayerRender, ScatterLayerReference,
                     AntennaZScoreSummary,
                     COLORIZE_AXIS_COLUMNS,
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


def _categorical_count_agg(
    df: pd.DataFrame, cat: _Categorization, cvs: "ds.Canvas",
):
    """Bin *df* into a per-category count cube at *cvs*'s resolution.

    Factored out of ``_shade_categorical`` (2026-09, two-level rendering)
    so ``build_layer_reference`` can run the identical binning step at a
    DIFFERENT (typically higher) resolution to build a cached reference
    cube, without duplicating the categorical-DataFrame construction.
    Pure aggregation, no shading -- see ``_priority_shade`` for that half.

    Only rows with ``bucket >= 0`` reach ``cvs.points()`` -- see
    ``_categorize`` for why a NaN-category row must never get to
    ``ds_agg.by()``.  The categorical column is built straight from the
    integer codes (``Categorical.from_codes``), never from strings.

    Returned dims are ``("y", "x", "__category__")`` -- Datashader's own
    ``ds_agg.by()`` output order.  This is NOT the dim order
    ``Canvas.raster()`` expects for a 3D array (band dimension first,
    per its own docstring) -- callers resampling this cube (see
    ``resample_layer_reference``) must transpose before and after, not
    assume it can be passed straight through.
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
    return cvs.points(df_cat, "x", "y", ds_agg.by("__category__", ds_agg.count()))


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
    """
    agg = _categorical_count_agg(df, cat, cvs)
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


def _compute_id_grid(
    df: pd.DataFrame, x0: float, x1: float, y0: float, y1: float,
    canvas_w: int, canvas_h: int, probe_grid_max_cells: int,
) -> dict:
    """The coarse per-bin native-coordinate-range grid (hover-probe
    redesign piece 2) -- see ``render_layer``'s docstring for the full
    rationale for why it's a separate, coarser pass from the display
    agg. Factored out of ``render_layer`` (2026-09, two-level rendering)
    so ``build_layer_reference`` can compute the SAME grid, at the SAME
    resolution, over a reference call's own extent, and additionally
    dilate it (``_dilate_bounds``) -- see that function's docstring for
    why the raw (undilated) grid is unsafe to resample later.

    Conditional per native-coordinate column, exactly as before this
    refactor: a caller (or an older DataFrame construction path mid
    transition) that hasn't populated one of "time"/"baseline_id"/
    "frequency" simply doesn't get that pair of keys in the returned
    dict, rather than failing the whole computation. The coarse mean
    reading (``"value"``) only needs x/y, which always exist, so it is
    always present.

    Returns a dict keyed ``"value"``, and any of ``"t_lo"``/``"t_hi"``,
    ``"bl_lo"``/``"bl_hi"``, ``"freq_lo"``/``"freq_hi"`` that applied --
    values are the raw ``id_agg["..."]`` ``xr.DataArray``s (NOT
    ``.values``), so a caller can either pull ``.values`` directly
    (``render_layer``'s own usage) or dilate-then-cache them as
    ``xr.DataArray``s (``build_layer_reference``'s usage, which needs
    the coordinate wrapper to resample via ``Canvas.raster()`` later).
    """
    id_cols = [c for c in ("time", "baseline_id", "frequency") if c in df.columns]
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
    # One aggregation pass computes all requested reductions together
    # (Datashader's ds.summary()), not one pass per reduction -- seven
    # reductions here cost the same single vectorized pass over df as a
    # one-reduction agg, just a bigger (still tiny, ~id_w*id_h*7
    # float64s) output.
    id_agg = id_cvs.points(df, "x", "y", ds_agg.summary(**summary_kwargs))
    out: dict = {"value": id_agg["val"]}
    if "time" in id_cols:
        out["t_lo"], out["t_hi"] = id_agg["t_lo"], id_agg["t_hi"]
    if "baseline_id" in id_cols:
        out["bl_lo"], out["bl_hi"] = id_agg["bl_lo"], id_agg["bl_hi"]
    if "frequency" in id_cols:
        out["freq_lo"], out["freq_hi"] = id_agg["f_lo"], id_agg["f_hi"]
    return out


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
        # Part 6 (2026-09): "statistical" coloring aggregates by a
        # DECOUPLED color column ("color", merged in by
        # MSv2Backend.query_columns from a separate Z-Score fetch --
        # see ScatterLayerSpec.coloring's own docstring) instead of the
        # plotted Y column itself -- position still comes from (x, y)
        # either way (a statistical layer still plots, say, Amplitude
        # vs. Time; only the COLOR changes), so only the AGGREGATION
        # source changes here, reusing every scaling/threshold/eq_hist
        # branch below completely unchanged rather than duplicating it
        # for a third mode.
        agg_column = "color" if lyr.coloring == "statistical" else "y"
        agg = cvs.points(df, "x", "y", ds_agg.mean(agg_column))

        if lyr.coloring == "statistical":
            # The color source's own range, NOT full_y_range (the
            # PLOTTED axis's range, e.g. Amplitude's -- unrelated to the
            # Z-Score's own scale). Computed fresh from df here rather
            # than threaded through as a new parameter, mirroring how
            # eq_reference below already recomputes its own reference
            # population from df rather than being passed one.
            color_vals = df[agg_column].to_numpy()
            color_finite = color_vals[np.isfinite(color_vals)]
            full_color_range = (
                (float(color_finite.min()), float(color_finite.max()))
                if color_finite.size else (0.0, 1.0)
            )
        else:
            full_color_range = full_y_range

        # Reference population for eq_hist / colorbar / histogram is the
        # TRUE per-sample values (of agg_column), not the binned agg --
        # running here, where the DataFrame still exists, is what makes
        # that possible without ever shipping them anywhere. Mirrors
        # VisibilityScatter._shade_all_layers' color_mode branch exactly.
        if color_mode == "local":
            visible_vals = df.loc[in_view, agg_column]
            if len(visible_vals) > 0:
                span = [float(visible_vals.min()), float(visible_vals.max())]
                eq_reference = visible_vals.to_numpy()
            else:
                span = [float(full_color_range[0]), float(full_color_range[1])]
                eq_reference = None
        else:  # "global"
            span = [float(full_color_range[0]), float(full_color_range[1])]
            eq_reference = df[agg_column].to_numpy()

        if lyr.scaling_vmin is not None and lyr.scaling_vmax is not None:
            span = [lyr.scaling_vmin, lyr.scaling_vmax]
        elif lyr.scaling == "threshold" and lyr.scaling_vmin is not None:
            # Part 6 (2026-09): unlike every other scaling above, whose
            # override needs BOTH vmin and vmax set together, a
            # threshold's cutoff needs only vmin -- it has one
            # meaningful boundary, not a range, and its upper bound is
            # irrelevant to its own classification (see
            # colormap_scaling.apply_explicit_scaling's threshold
            # branch). Keeps whichever upper bound span already
            # computed (the local/global auto-range above) rather than
            # requiring the caller to also supply a vmax that would
            # never actually be used.
            span = [lyr.scaling_vmin, span[1] if span is not None else lyr.scaling_vmin + 1.0]

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
    # See _compute_id_grid's docstring (factored out 2026-09, two-level
    # rendering, so build_layer_reference can compute the identical grid,
    # at the identical resolution, and dilate it -- see that function and
    # ScatterLayerReference.ref_id_t_lo's docstring).
    id_grid = _compute_id_grid(
        df, x0, x1, y0, y1, canvas_w, canvas_h, probe_grid_max_cells,
    )
    id_grid_value = id_grid["value"].values
    id_grid_t_lo    = id_grid["t_lo"].values    if "t_lo"    in id_grid else None
    id_grid_t_hi    = id_grid["t_hi"].values    if "t_hi"    in id_grid else None
    id_grid_bl_lo   = id_grid["bl_lo"].values   if "bl_lo"   in id_grid else None
    id_grid_bl_hi   = id_grid["bl_hi"].values   if "bl_hi"   in id_grid else None
    id_grid_freq_lo = id_grid["freq_lo"].values if "freq_lo" in id_grid else None
    id_grid_freq_hi = id_grid["freq_hi"].values if "freq_hi" in id_grid else None

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


# ---------------------------------------------------------------------------
# Two-level (Level-1/Level-2) scatter rendering (2026-09)
# ---------------------------------------------------------------------------
#
# Brings VisibilityRaster's existing two-level pan/zoom scheme
# (_do_viewport_rerender: a cheap local Datashader resample of an
# already-computed aggregation when the new viewport doesn't need finer
# resolution than what's cached, a real backend re-query only when it
# does) to VisibilityScatter, which until now paid a full query_columns()
# round trip on every single pan/zoom. See the scatter two-level
# rendering handoff notes (2026-09) for the full design, the measured
# numbers behind every decision below, and the correctness invariants
# this section exists to preserve.
#
# Split, deliberately, from render_layer() above rather than folded into
# it:
#   * build_layer_reference() runs alongside render_layer() on a full
#     render or a Level-2 re-query (never on the Level-1 hot path this
#     feature exists to avoid) -- see its own docstring for why a small
#     amount of duplicated aggregation cost there is the right trade
#     against ever touching render_layer()'s existing, tested behavior.
#   * resample_layer_reference()/resample_id_grid() run ONLY client-side
#     (VisibilityScatter, in whatever process it's running -- see the
#     handoff notes §1/§7 for why this must be client-side, not
#     backend-side, to actually remove the network hop in a remote
#     session) -- MSv2Backend/MSv4Backend never call these.
#   * needs_level2_requery() is the shared, pure gate both the widget
#     (deciding Level-1 vs. Level-2) and tests call.

def _dilate_bounds(
    lo: np.ndarray, hi: np.ndarray, radius: int = 1,
) -> tuple[np.ndarray, np.ndarray]:
    """Widen a (lo, hi) pair of per-cell extrema fields by *radius* cells
    on every side, so resampling them later can never report a tighter
    range than the true one.

    See the two-level rendering handoff notes §2.1 for the full
    derivation: a direct real-data test found 3 conservativeness
    violations out of 2919 valid cells without this (linearly
    interpolating -- or nearest-sampling across -- a cell boundary can
    silently pick the "wrong", tighter neighbor); zero violations with
    it, verified here again on a synthetic 300k-row/64x48-grid case
    (30 violations without the fix, 0 with it, matching the handoff
    notes' own order of magnitude) before this was wired into
    production.

    ``lo`` gets ``scipy.ndimage.minimum_filter``, ``hi`` gets
    ``maximum_filter`` -- each output cell becomes the min/max of its
    ``(2*radius+1)``-square neighborhood, giving a boundary cell a
    safety margin from its immediate neighbors instead of only ever
    point-sampling its single nearest reference cell (see
    ``Canvas.raster(interpolate="nearest")``).

    NaN-safe: a cell with no samples is NaN in both *lo* and *hi* (the
    same underlying "any rows landed in this bin" condition produces
    both), so ``empty = isnan(lo) | isnan(hi)`` identifies it once for
    both fields. An empty cell contributes no bound (filled with
    +/-inf, so a real neighbor's value always wins over it, never the
    reverse) but a neighborhood that is ENTIRELY empty stays NaN in the
    output -- there is nothing to dilate from. A dilated bound
    deliberately CAN spread into a cell adjacent to an empty one: that
    is the safety margin working as intended, not a claim that the
    empty cell itself has data.
    """
    if not HAS_SCIPY:
        raise ImportError(
            "scipy is required for the two-level scatter rendering "
            "hover-probe dilation fix.\nInstall: pip install scipy"
        )
    size = 2 * radius + 1
    lo_arr = np.asarray(lo, dtype=np.float64)
    hi_arr = np.asarray(hi, dtype=np.float64)
    empty = np.isnan(lo_arr) | np.isnan(hi_arr)
    lo_filled = np.where(empty, np.inf, lo_arr)
    hi_filled = np.where(empty, -np.inf, hi_arr)
    # mode="nearest" (edge-replicate): an edge cell's neighborhood should
    # widen using its own edge value for the missing side, never a
    # mirrored interior value that could tighten the margin instead of
    # widening it.
    lo_out = minimum_filter(lo_filled, size=size, mode="nearest")
    hi_out = maximum_filter(hi_filled, size=size, mode="nearest")
    all_empty_window = maximum_filter(
        (~empty).astype(np.uint8), size=size, mode="nearest",
    ) == 0
    lo_out = np.where(all_empty_window, np.nan, lo_out)
    hi_out = np.where(all_empty_window, np.nan, hi_out)
    return lo_out, hi_out


def build_layer_reference(
    df: Optional[pd.DataFrame], lyr: ScatterLayerSpec,
    x0: float, x1: float, y0: float, y1: float,
    ref_w: int, ref_h: int, canvas_w: int, canvas_h: int,
    color_mode: str, probe_grid_max_cells: int = 3072,
) -> ScatterLayerReference:
    """Build one layer's cached reference for later Level-1 resampling.

    Runs ALONGSIDE render_layer() on the same call (same *df*, same
    resolved viewport (x0,x1,y0,y1)) whenever ``query_columns(ref_scale=
    ...)`` was given a non-None ``ref_scale`` -- see that parameter's
    docstring. Independent of render_layer() rather than folded into it
    on purpose: this only ever runs once per backend call (a full render
    or a Level-2 re-query), never on the Level-1 hot path this whole
    feature exists to avoid, so its own bounded, small duplicated cost
    (one extra ``_categorize`` pass for a categorical layer, one extra
    id-grid pass -- both already proven cheap/bounded elsewhere in this
    module) is the right trade against ever touching or risking
    render_layer()'s existing, tested behavior.

    *ref_w*/*ref_h* -- the reference's own aggregation resolution
    (``REF_SCALE`` x the ADAPTIVE display canvas size -- i.e. *canvas_w*/
    *canvas_h*, the same adaptive shrink ``compute_canvas_size`` already
    applies for sparse data, kept proportional here too).

    *canvas_w*/*canvas_h* are ALSO needed on their own, separately from
    *ref_w*/*ref_h*: the hover-probe id grid's own resolution is
    deliberately independent of ``REF_SCALE`` (see
    ``ScatterLayerReference``'s docstring) and uses the exact same
    ``_id_grid_size(canvas_w, canvas_h, probe_grid_max_cells)`` formula
    render_layer() already does, via the shared ``_compute_id_grid``
    helper.

    See ``ScatterLayerReference`` for the full field-by-field contract
    of what this returns, and the scatter two-level rendering handoff
    notes for the design this implements.
    """
    if not HAS_DATASHADER:
        raise ImportError(
            "datashader is required for VisibilityScatter's rendering "
            "path.\nInstall: pip install datashader"
        )
    if df is None:
        return ScatterLayerReference(
            ref_x_range=(x0, x1), ref_y_range=(y0, y1),
            skip_reason="not queried",
        )
    if len(df) == 0:
        return ScatterLayerReference(
            ref_x_range=(x0, x1), ref_y_range=(y0, y1),
            skip_reason="query returned 0 rows",
        )

    in_view = (
        (df["x"] >= x0) & (df["x"] <= x1) &
        (df["y"] >= y0) & (df["y"] <= y1)
    )
    if int(in_view.sum()) == 0:
        return ScatterLayerReference(
            ref_x_range=(x0, x1), ref_y_range=(y0, y1),
            skip_reason=f"0 of {len(df)} samples in viewport",
        )

    # ---- hover-probe id grid: same call render_layer() makes, plus the
    # dilation safety fix (§2.1) on top of the three (lo, hi) pairs. Not
    # an extremum, so id_grid "value" (the coarse mean reading) is
    # carried through undilated.
    id_grid = _compute_id_grid(
        df, x0, x1, y0, y1, canvas_w, canvas_h, probe_grid_max_cells,
    )
    ref_id_t_lo = ref_id_t_hi = None
    ref_id_bl_lo = ref_id_bl_hi = None
    ref_id_freq_lo = ref_id_freq_hi = None
    if "t_lo" in id_grid:
        lo_v, hi_v = _dilate_bounds(id_grid["t_lo"].values, id_grid["t_hi"].values)
        ref_id_t_lo = id_grid["t_lo"].copy(data=lo_v)
        ref_id_t_hi = id_grid["t_hi"].copy(data=hi_v)
    if "bl_lo" in id_grid:
        lo_v, hi_v = _dilate_bounds(id_grid["bl_lo"].values, id_grid["bl_hi"].values)
        ref_id_bl_lo = id_grid["bl_lo"].copy(data=lo_v)
        ref_id_bl_hi = id_grid["bl_hi"].copy(data=hi_v)
    if "freq_lo" in id_grid:
        lo_v, hi_v = _dilate_bounds(id_grid["freq_lo"].values, id_grid["freq_hi"].values)
        ref_id_freq_lo = id_grid["freq_lo"].copy(data=lo_v)
        ref_id_freq_hi = id_grid["freq_hi"].copy(data=hi_v)
    ref_id_value = id_grid["value"]   # mean reading, not an extremum -- no dilation

    id_grid_kwargs = dict(
        ref_id_t_lo=ref_id_t_lo, ref_id_t_hi=ref_id_t_hi,
        ref_id_bl_lo=ref_id_bl_lo, ref_id_bl_hi=ref_id_bl_hi,
        ref_id_freq_lo=ref_id_freq_lo, ref_id_freq_hi=ref_id_freq_hi,
        ref_id_value=ref_id_value,
    )

    ref_cvs = ds.Canvas(
        plot_width=ref_w, plot_height=ref_h, x_range=(x0, x1), y_range=(y0, y1),
    )

    if lyr.coloring == "categorical":
        column = COLORIZE_AXIS_COLUMNS[lyr.colorize_axis]
        cat = _categorize(
            df, column, lyr.colorize_axis.label, cap=CATEGORY_CAP,
            excluded=frozenset(lyr.excluded_categories) or None,
            show_excluded=(lyr.excluded_display == "gray"),
        )
        if cat.skip_reason is not None:
            return ScatterLayerReference(
                ref_x_range=(x0, x1), ref_y_range=(y0, y1),
                skip_reason=cat.skip_reason, **id_grid_kwargs,
            )
        ref_cube = _categorical_count_agg(df, cat, ref_cvs)
        # float32, not the native ds_agg.count() uint32 -- see
        # resample_layer_reference's docstring for why: Canvas.raster()'s
        # default downsample reduction ("mean") floor-truncates a
        # fractional mean of small integer counts to 0 the instant it
        # lands back in an integer dtype, which silently erases sparse
        # category presence on any Level-1 view coarser than the
        # reference itself (confirmed directly: 82% of true presence
        # lost on a synthetic case). float32 exactly represents every
        # integer count this cube ever holds (categorical counts are
        # nowhere near float32's 2**24 exact-integer ceiling) while
        # letting a fractional mean survive the resample -- confirmed
        # directly to restore 100% presence AND 100% "majority" argmax
        # agreement simultaneously, with the default "mean" reduction,
        # no priority-dependent special-casing needed.
        ref_cube = ref_cube.astype(np.float32)
        return ScatterLayerReference(
            ref_x_range=(x0, x1), ref_y_range=(y0, y1),
            ref_cube=ref_cube,
            population=cat.population, other_index=cat.other_index,
            **id_grid_kwargs,
        )

    # continuous
    summary = ref_cvs.points(
        df, "x", "y", ds_agg.summary(mean=ds_agg.mean("y"), count=ds_agg.count()),
    )
    ref_agg = summary["mean"]
    ref_count = summary["count"]

    eq_curve = None
    if lyr.scaling == "eq_hist" and color_mode == "global":
        # Mirrors render_layer()'s own vmin/vmax-band-limited reference
        # population exactly (see that function's continuous/eq_hist
        # branch) -- a manual clip changes what the eq_hist curve is
        # built from, and that curve is exactly what gets cached here.
        eq_ref = df["y"].to_numpy()
        if lyr.scaling_vmin is not None or lyr.scaling_vmax is not None:
            pool_finite = eq_ref[np.isfinite(eq_ref)]
            lo = lyr.scaling_vmin if lyr.scaling_vmin is not None else (
                float(pool_finite.min()) if pool_finite.size else None)
            hi = lyr.scaling_vmax if lyr.scaling_vmax is not None else (
                float(pool_finite.max()) if pool_finite.size else None)
            if lo is not None and hi is not None and hi > lo:
                in_band = pool_finite[(pool_finite >= lo) & (pool_finite <= hi)]
                if in_band.size > 0:
                    eq_ref = in_band
        eq_curve = _cms.build_equalize_curve(eq_ref)

    return ScatterLayerReference(
        ref_x_range=(x0, x1), ref_y_range=(y0, y1),
        ref_agg=ref_agg, ref_count=ref_count, eq_curve=eq_curve,
        **id_grid_kwargs,
    )


def needs_level2_requery(
    ref: Optional[ScatterLayerReference],
    x0: float, x1: float, y0: float, y1: float,
    canvas_w: int, canvas_h: int,
    ref_covers_all_data: bool = False,
) -> bool:
    """``True`` when viewport (x0,x1,y0,y1) needs finer resolution than
    *ref* can safely serve at Level-1 -- i.e. Level-2 (a real backend
    re-query) is required.

    Implements the two-level rendering handoff notes §2.3's OR-gate::

        needs_requery = (x1-x0)/canvas_w < ref_cell_w  OR  (y1-y0)/canvas_h < ref_cell_h

    OR, not AND, deliberately: ``VisibilityRaster``'s own gate uses AND,
    which under-resolves whichever single axis was zoomed further when
    only one axis needs finer resolution than what's cached -- a
    considered improvement for this new implementation, not a claim
    that the existing raster gate is wrong (see the handoff notes for
    the full reasoning).

    Also ``True`` whenever *ref* is ``None``, is a skip_reason'd/empty
    reference, or the requested viewport reaches outside
    ``ref.ref_x_range``/``ref.ref_y_range`` -- in every one of these
    cases there is no aggregated data to resample from at all,
    regardless of cell size.
    """
    if ref is None or ref.skip_reason is not None:
        return True
    if x1 <= x0 or y1 <= y0 or canvas_w <= 0 or canvas_h <= 0:
        return True
    rx0, rx1 = ref.ref_x_range
    ry0, ry1 = ref.ref_y_range
    if (x0 < rx0 or x1 > rx1 or y0 < ry0 or y1 > ry1) and not ref_covers_all_data:
        # Outside the reference there may be data it never saw.  Not so
        # when the reference came from a FULL-extent render
        # (``ref_covers_all_data``): its range is the data's own extent,
        # so beyond it there is nothing to draw and Level-1 is exact.
        # (2026-09-29: after flagging outliers the data extent shrinks
        # while the user's view does not; every such redraw used to pay a
        # second backend query for an image that is empty outside.)
        return True
    agg = ref.ref_agg if ref.ref_agg is not None else ref.ref_cube
    if agg is None:
        return True
    h, w = agg.shape[0], agg.shape[1]
    if h < 1 or w < 1:
        return True
    ref_cell_w = (rx1 - rx0) / w
    ref_cell_h = (ry1 - ry0) / h
    return (
        (x1 - x0) / canvas_w < ref_cell_w or
        (y1 - y0) / canvas_h < ref_cell_h
    )


def _resample_interpolate(
    ref_agg, ref_x_range: tuple[float, float], ref_y_range: tuple[float, float],
    x_range: tuple[float, float], y_range: tuple[float, float],
    canvas_w: int, canvas_h: int,
) -> str:
    """"nearest" when either axis is upsampling past *ref_agg*'s own
    per-cell resolution, else "linear" -- mirrors
    ``VisibilityRaster._resample_method`` exactly (see that method's
    docstring for the "why nearest" rationale: an interpolated image
    shows fabricated intermediate values a probe cannot back up, while a
    smoothed gradient's displayed colour varies even though every probe
    returns the same underlying number).
    """
    x0, x1 = x_range
    y0, y1 = y_range
    h, w = ref_agg.shape[0], ref_agg.shape[1]
    ref_cell_w = (ref_x_range[1] - ref_x_range[0]) / max(w, 1)
    ref_cell_h = (ref_y_range[1] - ref_y_range[0]) / max(h, 1)
    upsampling = (
        (x1 - x0) / max(canvas_w, 1) < ref_cell_w or
        (y1 - y0) / max(canvas_h, 1) < ref_cell_h
    )
    return "nearest" if upsampling else "linear"


def _estimate_count_in_view(
    agg, x0: float, x1: float, y0: float, y1: float,
) -> int:
    """Approximate sample count within (x0,x1,y0,y1): crop *agg* (a
    count or count-cube reference, via coordinate slicing -- NOT a
    canvas-resolution resample, which would double-count under nearest
    upsampling) to the viewport and sum it.

    An ESTIMATE, not an exact raw-row count: cropping by the reference's
    own (coarser) cell boundaries can include a partial cell's full
    count when the true viewport edge cuts through it. Acceptable here
    specifically because this only feeds
    ``ScatterLayerRender.n_in_view``, which in turn only feeds the
    client's density-based ``auto_alpha`` (how vibrant a sparse layer
    looks) -- not a correctness invariant the way the id-grid extrema
    are (see ``ScatterLayerReference.ref_count``'s docstring).
    """
    if agg is None:
        return 0
    try:
        dims = agg.dims
        y_dim, x_dim = dims[0], dims[1]
        crop = agg.sel({
            x_dim: slice(min(x0, x1), max(x0, x1)),
            y_dim: slice(min(y0, y1), max(y0, y1)),
        })
        total = np.nansum(np.asarray(crop.values, dtype=np.float64))
        return int(total) if np.isfinite(total) else 0
    except Exception:
        log.debug("_estimate_count_in_view: crop failed, returning 0", exc_info=True)
        return 0


def resample_layer_reference(
    ref: ScatterLayerReference, lyr: ScatterLayerSpec,
    categories: Optional[tuple], x0: float, x1: float, y0: float, y1: float,
    canvas_w: int, canvas_h: int,
) -> tuple[np.ndarray, int]:
    """Level-1: shade one layer's cached reference at a new viewport --
    no raw rows touched, no backend call.

    Mirrors render_layer()'s own shading branch (continuous vs.
    categorical) but resamples an already-aggregated cube via
    ``Canvas.raster()`` instead of binning raw points via
    ``Canvas.points()``. See the two-level rendering handoff notes §1/§2
    for the full design.

    *categories* comes from the paired ``ScatterLayerRender.categories``
    the widget already cached from the same call that built *ref* (see
    ``ScatterLayerReference``'s docstring for why categories/colors
    aren't duplicated onto the reference itself); unused for a
    continuous layer. *lyr* supplies the live, current cmap/scaling/
    category_priority -- these can only change via a call that rebuilds
    the reference anyway (``update_scaling``, ``set_layer_cmaps``), so
    reading them live here rather than caching a copy on *ref* cannot
    go stale between Level-1 calls.

    Returns ``(image, n_in_view)`` -- see ``_estimate_count_in_view``
    for why the count is an estimate, not exact.
    """
    if ref.skip_reason is not None:
        return _empty_render(canvas_h, canvas_w, ref.skip_reason).image, 0

    cvs = ds.Canvas(
        plot_width=canvas_w, plot_height=canvas_h, x_range=(x0, x1), y_range=(y0, y1),
    )
    cmap = list(lyr.cmap or ())

    if ref.ref_cube is not None:
        # Canvas.raster() assumes a 3D array's LAST two dims are (y, x)
        # and resamples the FIRST dim's layers independently -- the
        # OPPOSITE of ds_agg.by()'s own (y, x, category) output order
        # (confirmed directly: passed straight through, plot_width/
        # plot_height silently resample the wrong two axes -- category
        # and x -- while leaving y untouched). Transpose to
        # (category, y, x) for the call and back to (y, x, category)
        # afterward so _priority_shade keeps receiving the same (H, W,
        # K) shape it always has. The category coordinate is reassigned
        # to a plain integer range for the call -- _priority_shade never
        # reads it, only positional order, which transpose/back
        # preserves exactly.
        cat_dim = ref.ref_cube.dims[-1]
        y_dim, x_dim = ref.ref_cube.dims[0], ref.ref_cube.dims[1]
        n_cat = ref.ref_cube.sizes[cat_dim]
        cube_t = ref.ref_cube.transpose(cat_dim, y_dim, x_dim).assign_coords(
            {cat_dim: np.arange(n_cat)},
        )
        # Default downsample reduction ("mean") is correct here for BOTH
        # priorities, and deliberately not overridden: "rarest" needs
        # PRESENCE (`> 0`) preserved for a possibly very sparse category,
        # and "majority" needs relative RANKING across categories
        # (`argmax`) preserved -- "mean" gives both, because every band
        # in a given output pixel is divided by the SAME merge-window
        # size, a monotonic per-pixel rescaling that cannot change which
        # band is largest (argmax(mean) == argmax(sum) exactly), and
        # because ref_cube is now float32 (see build_layer_reference),
        # a fractional mean survives instead of floor-truncating to 0
        # the way it did as a uint cube. Confirmed directly: 100%
        # presence agreement AND 100% "majority" argmax agreement
        # simultaneously on the same synthetic case that previously
        # forced a choice between the two (82% presence loss under
        # "mean"+uint, or a degraded 80% argmax match under "max"+uint).
        resampled_t = cvs.raster(cube_t, interpolate="nearest")
        ds_agg_cube = resampled_t.transpose(y_dim, x_dim, cat_dim)
        img_arr, _colors = _priority_shade(
            ds_agg_cube, list(categories or ()), tuple(cmap),
            lyr.category_priority, ref.population, ref.other_index,
        )
        n_in_view = _estimate_count_in_view(ref.ref_cube, x0, x1, y0, y1)
        return img_arr, n_in_view

    if ref.ref_agg is None:
        return _empty_render(canvas_h, canvas_w, "reference not built").image, 0

    interpolate = _resample_interpolate(
        ref.ref_agg, ref.ref_x_range, ref.ref_y_range, (x0, x1), (y0, y1),
        canvas_w, canvas_h,
    )
    ds_agg_resampled = cvs.raster(ref.ref_agg, interpolate=interpolate)
    values = ds_agg_resampled.values

    span = None
    if lyr.scaling_vmin is not None and lyr.scaling_vmax is not None:
        span = [lyr.scaling_vmin, lyr.scaling_vmax]
    elif lyr.scaling == "threshold" and lyr.scaling_vmin is not None:
        # Part 6 (2026-09): mirrors render_layer's identical branch
        # above -- see that one's comment for the full rationale (a
        # threshold's cutoff needs only vmin, unlike every other
        # scaling's vmin+vmax-together override).
        span = [lyr.scaling_vmin, span[1] if span is not None else lyr.scaling_vmin + 1.0]

    if lyr.scaling in _cms.DATASHADER_HOW:
        shade_kwargs = dict(
            cmap=cmap, how=_cms.DATASHADER_HOW[lyr.scaling], min_alpha=_MIN_ALPHA,
        )
        if span is not None:
            shade_kwargs["span"] = span
        img = tf.shade(ds_agg_resampled, **shade_kwargs)
    elif lyr.scaling == "eq_hist":
        transformed = _cms.apply_equalize_curve(values, ref.eq_curve)
        scaled_agg = ds_agg_resampled.copy(data=transformed)
        img = tf.shade(
            scaled_agg, cmap=cmap, how="linear", span=[0.0, 1.0], min_alpha=_MIN_ALPHA,
        )
    else:
        transformed = _cms.apply_explicit_scaling(
            values, lyr.scaling, alpha=lyr.scaling_alpha, gamma=lyr.scaling_gamma,
            vmin=span[0] if span is not None else None,
            vmax=span[1] if span is not None else None,
        )
        scaled_agg = ds_agg_resampled.copy(data=transformed)
        img = tf.shade(
            scaled_agg, cmap=cmap, how="linear", span=[0.0, 1.0], min_alpha=_MIN_ALPHA,
        )

    img_arr = np.array(img, dtype=np.uint32)
    n_in_view = _estimate_count_in_view(ref.ref_count, x0, x1, y0, y1)
    return img_arr, n_in_view


def resample_id_grid(
    ref: ScatterLayerReference, x0: float, x1: float, y0: float, y1: float,
    canvas_w: int, canvas_h: int, probe_grid_max_cells: int,
) -> Optional[dict]:
    """Level-1: resample the cached, already-dilated id-grid reference to
    the current viewport's own id-grid resolution.

    Returns the same dict SHAPE ``VisibilityScatter._render_all_layers``
    already builds from a live ``ScatterLayerRender`` (keys ``"value"``,
    ``"x_range"``, ``"y_range"``, and any of ``"t_lo"``/``"t_hi"``,
    ``"bl_lo"``/``"bl_hi"``, ``"f_lo"``/``"f_hi"`` the reference has), or
    ``None`` when the reference has no id-grid at all (a skip_reason'd
    layer -- nothing to hover).

    Always ``interpolate="nearest"`` for every field, range and
    value alike: this reference and the current viewport's own id-grid
    resolution are BOTH sized independently of ``REF_SCALE`` (see
    ``ScatterLayerReference``'s docstring) via the same
    ``_id_grid_size(canvas_w, canvas_h, probe_grid_max_cells)`` formula
    -- and ``needs_level2_requery`` (gated on the separate, always-finer
    data reference) already guarantees the requested viewport sits
    within ``ref``'s own extent whenever Level-1 is chosen. In practice
    this makes an id-grid Level-1 resample always an upsample (a smaller
    region drawn at the same ~probe_grid_max_cells budget); "nearest" is
    also the ONLY safe choice for the dilated lo/hi fields regardless
    (see ``_dilate_bounds``) -- the coarse mean "value" field could use
    "linear" like the display image does, but there is no correctness
    reason to special-case it, and using the one rule everywhere keeps
    this function simple.
    """
    if ref.ref_id_value is None:
        return None
    id_w, id_h = _id_grid_size(canvas_w, canvas_h, probe_grid_max_cells)
    cvs = ds.Canvas(
        plot_width=id_w, plot_height=id_h, x_range=(x0, x1), y_range=(y0, y1),
    )

    out: dict = {"x_range": (x0, x1), "y_range": (y0, y1)}
    out["value"] = cvs.raster(ref.ref_id_value, interpolate="nearest").values

    def _pair(lo_ref, hi_ref):
        return (
            cvs.raster(lo_ref, interpolate="nearest").values,
            cvs.raster(hi_ref, interpolate="nearest").values,
        )

    if ref.ref_id_t_lo is not None:
        out["t_lo"], out["t_hi"] = _pair(ref.ref_id_t_lo, ref.ref_id_t_hi)
    if ref.ref_id_bl_lo is not None:
        out["bl_lo"], out["bl_hi"] = _pair(ref.ref_id_bl_lo, ref.ref_id_bl_hi)
    if ref.ref_id_freq_lo is not None:
        out["f_lo"], out["f_hi"] = _pair(ref.ref_id_freq_lo, ref.ref_id_freq_hi)
    return out


# ---------------------------------------------------------------------------
# Part 6: statistical (Z-Score) colorization -- Slice 1
#
# Provenance, stated precisely: INSPIRED BY the robust-statistics approach
# of CASA/AIPS `rflag` (median-based statistics, real and imaginary
# parts, sigma-style thresholds), but NOT an implementation of the rflag
# algorithm, and it will not reproduce rflag's flags. rflag measures local
# RMS in sliding time windows per channel plus a per-time spectral pass,
# with thresholds scaled from noise estimates; this module scores each
# sample against ONE median per baseline over the whole selection. The
# statistic itself is the Iglewicz & Hoaglin modified z-score generalized
# to a joint radial distance. See ZSCORE_USER_GUIDE.md, "How this relates
# to CASA rflag".
# ---------------------------------------------------------------------------
#
# Per-baseline, windowed reference population (visplot-colorize-by-axis-
# design.md §7.3 row 1, §7.4, §7.5). See that document for the full
# design and the statistical justification: circularly symmetric complex
# Gaussian visibility noise (real and imaginary parts i.i.d., EQUAL
# variance -- Thompson, Moran & Swenson) makes a single joint radial
# distance the correct generalization of a robust z-score to two
# dimensions, not two independent per-part z-scores combined afterward.

_ZSCORE_RAYLEIGH_CONST = math.sqrt(2.0 * math.log(2.0))
"""Replaces the standard modified z-score's 0.6745 for this joint/radial
case. 0.6745 calibrates MAD to Gaussian sigma for a SYMMETRIC 1D
distribution (``median(|x - median(x)|) * 0.6745 ~= sigma``). The radius
of an isotropic 2D Gaussian deviation instead follows a Rayleigh
distribution, whose median relates to its own scale parameter as
``median = sigma * sqrt(2 * ln(2))`` -- so THIS constant (~1.1774), not
0.6745, is what recovers a sigma-comparable score here. See the design
doc §7.4 for the full derivation -- a derived, not yet simulation-
verified, calibration. The RELATIVE ordering of scores (which points are
most anomalous) does not depend on this constant at all, only the
absolute numbers a threshold gets compared against do.
"""


def compute_baseline_zscore(
    real: np.ndarray, imag: np.ndarray, group: np.ndarray,
) -> np.ndarray:
    """Robust joint Z-Score of (real, imag) against a per-group (typically
    per-baseline) reference population. Inspired by rflag's median-based
    statistics; not an implementation of the rflag algorithm (see the
    "Provenance" note above this section).

    Implements the design doc's §7.4 formula exactly::

        dr = real - median(real | group)
        di = imag - median(imag | group)
        r  = sqrt(dr**2 + di**2)
        scale = median(r | group)
        score = r / scale * sqrt(2 * ln(2))

    A single joint radial statistic, not two independent per-part
    z-scores -- see §7.4 for why (circularly symmetric complex Gaussian
    visibility noise: real and imaginary parts are i.i.d. with EQUAL
    variance, so a joint, rotation-invariant distance is the correct
    generalization, not an artifact of an arbitrary phase convention).
    Always >= 0 (a radius) -- there is no sign to report.

    Parameters
    ----------
    real, imag :
        Per-sample real/imaginary parts. Expected to already be
        flag-masked by the caller -- every entry here should be a
        genuine, unflagged sample. Excluding flagged data is NOT
        handled inside this function, by design: it mirrors how every
        other derived ``Axis`` already gets flag-masked upstream, in
        ``XArrayReader._lazy_quantity``'s ``q.where(~flag_pol)``, rather
        than inventing a special case here.
    group :
        Per-sample group key -- ``baseline_id`` for Slice 1's
        per-baseline reference population (§7.3 row 1). Any hashable
        per-row array works; a future per-antenna or per-(time,freq)-bin
        reference group (§7.3's other rows) would call this the same
        way with a different ``group`` array -- nothing in this
        function is baseline-specific.

    Returns
    -------
    np.ndarray
        Same shape as *real*/*imag*, float64, the Z-Score per sample.
        NaN wherever a group's reference population was degenerate (a
        single-member group has zero spread by construction -- scale
        would be exactly 0) -- callers should treat this the same way a
        ``skip_reason``'d layer is treated elsewhere in this module:
        nothing to show, not an error. No minimum-population floor
        beyond that single-member case is applied HERE -- see the
        design doc's "reference population size must be visible" trust
        requirement (§7.6, §7.10) for the UI-level floor; this function
        always computes something (or NaN for a truly degenerate
        group), and it is the caller's job to decide whether N was
        large enough to trust the result, not this function's.
    """
    real_arr  = np.asarray(real, dtype=np.float64)
    imag_arr  = np.asarray(imag, dtype=np.float64)
    group_arr = np.asarray(group)
    if real_arr.shape != imag_arr.shape or real_arr.shape != group_arr.shape:
        raise ValueError(
            "compute_baseline_zscore: real/imag/group must share one "
            f"shape; got {real_arr.shape}, {imag_arr.shape}, {group_arr.shape}"
        )
    if real_arr.size == 0:
        return np.zeros(0, dtype=np.float64)

    df = pd.DataFrame({"real": real_arr, "imag": imag_arr, "group": group_arr})
    # groupby + transform("median"): one pass, broadcasts each group's
    # own median back to every row in that group -- the standard,
    # efficient way to compare every row against its own group's
    # reference without a manual join.
    med_real = df.groupby("group")["real"].transform("median").to_numpy()
    med_imag = df.groupby("group")["imag"].transform("median").to_numpy()
    dr = real_arr - med_real
    di = imag_arr - med_imag
    r = np.sqrt(dr * dr + di * di)

    scale = (
        pd.Series(r, index=df.index)
        .groupby(df["group"])
        .transform("median")
        .to_numpy()
    )
    with np.errstate(invalid="ignore", divide="ignore"):
        score = np.where(
            scale > 0, r / scale * _ZSCORE_RAYLEIGH_CONST, np.nan,
        )
    return score


# ---------------------------------------------------------------------------
# Part 6 Slice 2: per-antenna quantitative readout
# ---------------------------------------------------------------------------

_DEFAULT_ZSCORE_THRESHOLD = 3.5
"""Iglewicz & Hoaglin's (1993) own convention for this exact modified
z-score statistic (``|M_i| > 3.5``) -- see the design doc's §7.4
citation, already used there to justify displaying the score as a
magnitude. Used as ``compute_antenna_zscore_summary``'s threshold
whenever the caller doesn't supply one from the layer's own
``scaling_vmin`` (which takes precedence when set, since it reflects
what the user is actually looking at on screen right now, not a fixed
literature default) -- see that function's own docstring.
"""


def zscore_cell_cutoff(
    n_samples,
    per_sample_cutoff: float = _DEFAULT_ZSCORE_THRESHOLD,
) -> float:
    """Cutoff for a cell that reports the MAX of *n_samples* Z-Scores.

    Under the null (no anomaly) a Z-Score is the radial amplitude of
    circularly symmetric complex Gaussian noise in units of its own
    scale, i.e. Rayleigh distributed with unit scale:
    ``P(Z > t) = exp(-t**2 / 2)`` (see ``_ZSCORE_RAYLEIGH_CONST``, which
    calibrates the robust scale so this holds).  *per_sample_cutoff* is
    the cutoff that is right for ONE sample (3.5, the Iglewicz & Hoaglin
    convention -- a per-sample false-alarm rate of ``p0 = exp(-3.5**2/2)``,
    about 0.2 %).

    A raster cell does not show one sample: it shows the maximum over
    every sample reduced into it (e.g. all channels of a Time x Baseline
    cell), and the max of *n* draws exceeds a fixed cutoff far more
    often -- with 384 channels, 3.5 flags ~58 % of cells that contain
    nothing but noise.  Holding the per-CELL false-alarm rate at ``p0``
    instead means solving ``1 - (1 - p)**n = p0`` for the per-sample
    tail probability ``p`` (Sidak correction) and taking
    ``t = sqrt(-2 ln p)``.  ``n = 1`` returns *per_sample_cutoff*
    exactly; ``n = 384`` gives ~4.9.  The dependence on *n* is
    logarithmic (``t**2 ~ c**2 + 2 ln n``), so the nominal count of
    samples reduced per cell is accurate enough -- flagging that removes
    a large share of them barely moves the result.

    Returns *per_sample_cutoff* unchanged for a missing, non-finite or
    ``< 1`` count, so a caller that cannot supply *n* degrades to the
    per-sample behaviour rather than failing.
    """
    try:
        n = float(n_samples)
    except (TypeError, ValueError):
        return float(per_sample_cutoff)
    if not math.isfinite(n) or n <= 1.0:
        return float(per_sample_cutoff)
    p0 = math.exp(-0.5 * per_sample_cutoff * per_sample_cutoff)
    # 1 - (1 - p0)**(1/n), computed stably for tiny p0 and large n.
    p = -math.expm1(math.log1p(-p0) / n)
    return math.sqrt(-2.0 * math.log(p))


def compute_antenna_zscore_summary(
    scores: np.ndarray, antenna_name: str,
    threshold: float = _DEFAULT_ZSCORE_THRESHOLD,
) -> AntennaZScoreSummary:
    """Slice 2's per-antenna quantitative readout (design doc §7.6,
    §7.10): sample count, median score, and fraction over threshold --
    statistics only, never a qualitative verdict (§7.2's own explicit
    requirement).

    A pure summarizing function, deliberately as simple as
    ``compute_baseline_zscore`` itself: *which* rows belong to the one
    antenna being summarized is entirely the caller's job (see
    ``MSv2Backend.query_columns``'s own antenna-summary handling,
    which passes in exactly one layer's own, already-selection-
    narrowed Z-Score column) -- this function only aggregates whatever
    array it's given.

    Parameters
    ----------
    scores :
        A layer's own Z-Score values (the "y" column directly for a
        layer plotting ``Axis.Z_SCORE``, or the merged-in "color"
        column for a ``coloring="statistical"`` layer -- see
        ``ScatterLayerSpec.coloring``'s own docstring for that
        distinction). Non-finite entries (already-excluded samples,
        degenerate single-baseline-member groups -- see
        ``compute_baseline_zscore``'s own NaN convention) are dropped
        before summarizing, not treated as zero or as "over threshold".
    antenna_name :
        Carried through unchanged into the result -- this function
        does not resolve or validate it against any metadata; that is
        the caller's job (``SelectionSpec.antenna_names`` naming
        exactly one antenna).
    threshold :
        The cutoff ``fraction_over_threshold`` is computed against.
        Callers should pass the layer's own ``scaling_vmin`` when set
        (the threshold actually driving what the user sees on screen,
        whether or not ``scaling == "threshold"`` specifically), and
        fall back to ``_DEFAULT_ZSCORE_THRESHOLD`` otherwise -- this
        function itself has no opinion and always uses exactly what
        it's given, so that choice stays visible at the call site
        rather than hidden in here.

    Returns
    -------
    AntennaZScoreSummary
        ``count=0`` and ``median_score=fraction_over_threshold=None``
        when *scores* has no finite entries at all -- a real, reportable
        state ("nothing to summarize"), not an error.
    """
    finite = np.asarray(scores, dtype=np.float64)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        return AntennaZScoreSummary(
            antenna_name=antenna_name, count=0,
            median_score=None, fraction_over_threshold=None,
            threshold=float(threshold),
        )
    return AntennaZScoreSummary(
        antenna_name=antenna_name,
        count=int(finite.size),
        median_score=float(np.median(finite)),
        fraction_over_threshold=float((finite > threshold).mean()),
        threshold=float(threshold),
    )
