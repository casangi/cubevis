"""XArrayReader — abstract base class and MSv4 backend.

``XArrayReader`` is the single data-access abstraction used by all
``visplot`` source classes.  It wraps either ``xarray-ms`` (for MSv2
files) or ``xradio`` (for MSv4 Zarr), and presents an identical
MSv4-structured DataTree interface to every layer above.

Key design principles
---------------------
* **No Bokeh dependency** — pure Python / xarray / Dask.
* **Accepts ``Axis`` members**, not bare strings, for all axis
  arguments; uses ``Axis.axis_type`` to dispatch the correct xarray
  operation (coordinate passthrough vs. derived computation).
* **Read-only** — flag persistence is entirely the ``FlagDB``'s
  responsibility; no write methods here.
* **Always includes metadata labels** — ``query_columns`` returns
  ``scan_name``, ``field_name``, ``baseline_antenna1_name``, and
  ``baseline_antenna2_name`` alongside the requested data so that
  layers above can annotate plots without a second round-trip.
* **Lazy / Dask-backed** — nothing is materialised until Datashader
  calls ``.compute()`` implicitly during aggregation.

Module layout
-------------
``XArrayReader``     — abstract base class (this file)
``MSv4Backend``      — xradio implementation (this file)
``MSv2Backend``      — xarray-ms/arcae implementation (msv2_backend.py)

References
----------
msvis_design.md §4.2 (XArrayReader), §4.6 (MSv4 coordinate model),
§6 (MSv2 I/O)
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import Optional, Union

import numpy as np
import pandas as pd
import xarray as xr

from ..axes import Axis, AxisInfo, AxisType
from ..selection import SelectionSpec

log = logging.getLogger(__name__)


# ======================================================================
# Scatter render DTOs (2026-09)
# ======================================================================
#
# Replace the old dict[(Axis,pol), pd.DataFrame] query_columns() contract.
# See ScatterRenderResult's docstring for why: that contract shipped up
# to ~30,913,392 raw rows over the wire for a remote session (confirmed
# against a real MS), which outran dispatch_fast's 30s default timeout
# well before any bug in the encode/decode logic itself. Binning and
# shading now happen wherever query_columns() itself runs -- in-process
# for LocalVisibilityReader, in the worker subprocess for a remote
# session -- so only a small, bounded per-layer result crosses a
# process or wire boundary either way.

# ----------------------------------------------------------------------
# Colorize-by-axis (Part 3, 2026-09): axis -> per-row column name
# ----------------------------------------------------------------------
# Source of truth requested by visplot-colorize-by-axis-handoff-part3.md
# ("Build the Axis -> column name lookup table ... it doesn't exist yet
# -- the [What landed] table is the source of truth for what to put in
# it"). Mirrors that table exactly; do not hand-derive column names from
# an Axis elsewhere -- go through this dict (or ``colorizable_axes()``
# below) so a future column rename only has to happen here.
COLORIZE_AXIS_COLUMNS: dict[Axis, str] = {
    Axis.SCAN:        "scan_name",
    Axis.ANTENNA1:    "baseline_antenna1_name",
    Axis.ANTENNA2:    "baseline_antenna2_name",
    Axis.CORRELATION: "polarization",
    Axis.SPW:         "spw",
}

DEGENERATE_COLORIZE_AXES: frozenset = frozenset({Axis.CORRELATION})
"""Colorizable axes that can never show more than one category on a
single layer.

``ScatterLayerSpec.polarization`` is a scalar -- a layer already plots
exactly one polarization -- so a per-row ``polarization`` column is
always exactly one category for that layer (see the design doc's §4.1
finding). The column is real and correctly populated (kept for
uniformity with the other axes, and it costs nothing), so rendering it
works fine; it just never shows more than one legend entry. Left as a
flag rather than removed from ``COLORIZE_AXIS_COLUMNS`` -- Part 4's
call whether a degenerate axis is worth excluding from the axis-picker
UI (a `ScatterLayerSpec` field is not the right place to hide it, since
"never useful for one layer" is a UI-plumbing statement, not a data
statement -- see visplot-colorize-by-axis-handoff-part3.md's "Axes
dropped or deferred" section).
"""


def colorizable_axes() -> tuple[Axis, ...]:
    """``Axis`` members with a real per-row column to colorize by.

    Convenience for Part 4's axis-picker UI -- built from
    ``COLORIZE_AXIS_COLUMNS`` so the picker's options and the render
    path's column lookup can never drift apart. Does not exclude
    ``DEGENERATE_COLORIZE_AXES``; a UI can consult that set separately
    if it wants to warn about or omit a degenerate choice.
    """
    return tuple(COLORIZE_AXIS_COLUMNS)


@dataclass(frozen=True)
class ScatterLayerSpec:
    """One layer's rendering parameters, as ``query_columns`` needs them.

    Deliberately NOT ``VisibilityScatter.ScatterLayer`` itself --
    ``XArrayReader`` subclasses live in the data layer and must not
    import upward from the widget layer. ``VisibilityScatter``
    constructs one of these per ``ScatterLayer`` at the call site
    (``label`` and other widget-only fields are dropped).

    A plain frozen dataclass of JSON-primitive/Enum/tuple fields --
    round-trips via ``cubevis.utils._conversion``'s existing generic
    dataclass wire support with no new registration needed.

    ``alpha`` is carried through even though the actual opacity blend
    happens client-side (see ``ScatterLayerRender``): it is used only
    by ``compute_canvas_size`` to exclude a hidden (``alpha <= 0``)
    layer from the shared adaptive canvas-size estimate, matching
    ``VisibilityScatter._compute_canvas_size``'s pre-redesign behavior.
    It does NOT skip shading a hidden layer (see
    ``_scatter_render.render_layer``'s 2026-09 correction note) --
    that would leave nothing cached for
    ``VisibilityScatter.set_alpha()`` to un-hide without a backend call.

    ``coloring``/``colorize_axis`` (Part 3, 2026-09): select
    colorize-by-axis. ``coloring`` is a string, not a bool, on purpose
    -- see the design doc's §7.7 touchpoint: Part 5's color-source-
    column capability will want a third value (``"computed"`` or
    similar) alongside ``"continuous"``/``"categorical"``, and a two-
    state boolean would need a real rework to grow a third state later.
    ``scaling``/``scaling_*``/``cmap`` keep their existing continuous-
    coloring meaning when ``coloring="continuous"`` (the default, so
    every pre-Part-3 caller is unaffected); the design doc's §4.3 keeps
    the two modes mutually exclusive per layer, not combinable, so
    nothing here reads ``scaling``/``scaling_*`` when
    ``coloring="categorical"``.

    ``cmap`` is reused, not duplicated, for the categorical case: an
    ordered, already-theme-conditioned set of discrete colors (e.g.
    ``palettes.categorical_cmap(...)``) rather than a gradient --
    assigned to categories in sorted order, cycling modulo its length
    exactly the way ``palettes.scatter_cmaps()`` already documents for
    per-layer ramp assignment. This mirrors the existing division of
    labour (the widget resolves a palette *name* to concrete hex
    colors and hands the backend/render path already-concrete colors;
    ``_scatter_render.py`` never imports ``palettes.py``) rather than
    adding a second, mode-specific palette field.

    ``colorize_axis`` must be one of ``COLORIZE_AXIS_COLUMNS`` and must
    (only) be set when ``coloring="categorical"`` -- validated eagerly
    in ``__post_init__`` so a Part 4 wiring bug surfaces at
    ``ScatterLayerSpec`` construction, not three calls later inside
    ``_scatter_render.render_layer``.

    ``excluded_categories`` (Part 5, 2026-09): raw per-sample values
    (e.g. individual antenna names -- not a post-binning display label
    like a bucketed range) to leave out of both the render and the
    returned ``categories``/``category_colors``/``category_members``.
    Raw, not display, values because the checklist that populates this
    is built from cheap, already-cached ``IdentityTables`` metadata
    (the widget layer's own "similar to SPW" enumeration -- see
    ``VisibilityScatter.colorize_controls()``), which only ever
    enumerates individual real values, never how a particular render
    will eventually bucket them (see ``_resolve_categories``'s
    docstring for the full reasoning). Only meaningful when
    ``coloring="categorical"`` -- validated the same way
    ``colorize_axis`` is, and for the same reason: a continuous layer
    has no categories to exclude, so a non-empty value here on one
    would silently do nothing rather than surface the caller's mistake.
    A tuple, not a ``frozenset``, so this round-trips over the same
    JSON-primitive wire support as every other field here; converted to
    a set only where membership testing actually happens
    (``_resolve_categories``).
    """
    y_axis:        Axis
    polarization:  str
    cmap:          tuple[str, ...]
    alpha:         float = 1.0
    scaling:       str   = "eq_hist"
    scaling_alpha: float = 10.0
    scaling_gamma: float = 1.0
    scaling_vmin:  Optional[float] = None
    scaling_vmax:  Optional[float] = None
    coloring:      str = "continuous"
    colorize_axis: Optional[Axis] = None
    excluded_categories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.coloring not in ("continuous", "categorical"):
            raise ValueError(
                "ScatterLayerSpec.coloring must be 'continuous' or "
                f"'categorical', got {self.coloring!r}"
            )
        if self.coloring == "categorical":
            if self.colorize_axis is None:
                raise ValueError(
                    "ScatterLayerSpec.coloring='categorical' requires "
                    "colorize_axis to be set"
                )
            if self.colorize_axis not in COLORIZE_AXIS_COLUMNS:
                raise ValueError(
                    f"Axis.{self.colorize_axis.name} is not a "
                    "colorizable axis -- see COLORIZE_AXIS_COLUMNS"
                )
            if not self.cmap:
                raise ValueError(
                    "ScatterLayerSpec.coloring='categorical' requires "
                    "a non-empty cmap (the per-category color set)"
                )
        else:
            if self.colorize_axis is not None:
                raise ValueError(
                    "ScatterLayerSpec.colorize_axis is only valid when "
                    "coloring='categorical'"
                )
            if self.excluded_categories:
                raise ValueError(
                    "ScatterLayerSpec.excluded_categories is only valid "
                    "when coloring='categorical'"
                )


@dataclass(frozen=True)
class ScatterLayerRender:
    """One layer's rendered result from ``query_columns``.

    ``image`` carries Datashader's own per-pixel occupancy alpha (via
    ``tf.shade(..., min_alpha=...)``), deliberately NOT yet collapsed
    to a single density-derived opacity value. That collapse
    (``layer_alpha = auto_alpha * lyr.alpha``, where ``auto_alpha``
    derives from ``n_in_view`` and the canvas pixel count) is cheap
    and stays client-side -- it's what keeps
    ``VisibilityScatter.set_alpha()`` a free, no-requery operation,
    exactly as it is today.

    ``hist_counts``/``hist_edges`` and ``mapping_x``/``mapping_u`` are
    computed against the *true* per-sample reference population (the
    real column values, filtered to the viewport for
    ``color_mode="local"``) -- not an approximation from the binned
    agg -- because this is the one place that population still exists.
    They feed ``VisibilityScatter.histogram()``/``colormap_controls()``
    and ``_bands_with_mappings()``'s ``ScalarMapping`` respectively;
    reconstruct the latter via
    ``ScalarMapping(mapping_x, mapping_u, lyr.scaling)``.
    """
    image:       np.ndarray             # HxW uint32 RGBA
    n_in_view:   int
    skip_reason: Optional[str]
    peak_value:  Optional[float]
    hist_counts: Optional[np.ndarray]
    hist_edges:  Optional[np.ndarray]
    mapping_x:   Optional[np.ndarray]
    mapping_u:   Optional[np.ndarray]

    # ---- hover-probe redesign piece 2 (2026-09) -------------------- #
    # A second, much coarser per-bin grid of native-coordinate ranges,
    # computed alongside `image` in the same render (see
    # `_scatter_render.render_layer`'s `probe_grid_max_cells` handling).
    # Six 2D float arrays, all shaped (id_grid_height, id_grid_width) --
    # min/max of (time, baseline_id, frequency) for whatever samples
    # landed in each coarse bin. `None` (all six) when this layer had no
    # samples at all (skip_reason set) -- there is nothing to grid.
    #
    # Deliberately a SEPARATE, coarser canvas from `image`, not a reuse
    # of the display resolution: the whole point (see
    # XArrayReader.query_columns' `probe_grid_max_cells` docstring) is
    # that this only has to narrow a hover to "roughly this range", so a
    # ~64x48 grid keeps the extra payload to tens of KB even when the
    # display canvas itself is much larger. `id_grid_x_range`/
    # `id_grid_y_range` are carried explicitly rather than assumed equal
    # to the layer's own display range, since a per-layer local
    # color_mode range can differ from what was actually binned here --
    # this grid is always binned over the SAME (x0,x1,y0,y1) the display
    # image used, but recording it explicitly avoids ever having to
    # assume that alignment holds if this ever changes.
    id_grid_t_lo:    Optional[np.ndarray] = None
    id_grid_t_hi:    Optional[np.ndarray] = None
    id_grid_bl_lo:   Optional[np.ndarray] = None
    id_grid_bl_hi:   Optional[np.ndarray] = None
    id_grid_freq_lo: Optional[np.ndarray] = None
    id_grid_freq_hi: Optional[np.ndarray] = None
    id_grid_x_range: Optional[tuple[float, float]] = None
    id_grid_y_range: Optional[tuple[float, float]] = None
    # Coarse per-bin mean value (same grid, same summary() pass as the
    # six ranges above) -- added so a local hover can report an
    # approximate reading alongside coarse identity, matching what the
    # pre-redesign per-hover backend call used to provide for both at
    # once. Deliberately the mean of the SAME quantity `image` shades
    # (lyr's y-axis quantity), not a separate concept -- "coarse but
    # free" for both value and identity together, with
    # probe_scatter_region (click-to-exact) remaining the source of an
    # exact reading, exactly as it already is for identity.
    id_grid_value:   Optional[np.ndarray] = None

    # ---- colorize-by-axis (Part 3, 2026-09) ------------------------- #
    # Populated only when this layer rendered with
    # ``ScatterLayerSpec.coloring == "categorical"`` (``None``, all
    # three, for a continuous layer or a skipped/empty categorical one
    # -- see ``_scatter_render.render_layer``'s no-data-available skip
    # path, which reports through the existing ``skip_reason``
    # mechanism rather than adding a second one here; there is no
    # longer a cardinality-cap skip reason -- see ``category_members``
    # below and ``_scatter_render.CATEGORY_CAP``'s docstring).
    #
    # ``categories`` is the full, sorted, DISPLAY set for this layer's
    # ``colorize_axis`` in the CURRENT SELECTION -- not narrowed to the
    # current viewport, and not necessarily the raw distinct values
    # themselves (see ``category_members``). Category-to-color
    # assignment (``category_colors``) is keyed off this same list, in
    # this same order, precisely so a pan/zoom re-render (a new
    # viewport, same selection) cannot reassign an already-shown
    # category to a different color -- see
    # ``_scatter_render._resolve_categories``'s docstring for why
    # viewport-scoping this would be a real (if subtle) correctness
    # bug, not a cosmetic one.
    #
    # ``category_colors`` maps each entry of ``categories`` to the hex
    # color ``image`` actually used for it -- Part 4's legend widget
    # and PNG-export swatches are the intended readers; this is the
    # "categorical palette" artifact the design doc's Part 3 scope
    # calls for, carried on the render result rather than recomputed
    # by the caller (which has no access to ``lyr.cmap``'s modulo-
    # cycling once a layer's category count exceeds its color count).
    #
    # ``category_members`` maps each entry of ``categories`` to the
    # tuple of RAW underlying values it represents -- always a 1-tuple
    # containing just that value when the real distinct-value count was
    # within ``CATEGORY_CAP`` (the common case), or several real values
    # grouped into one contiguous bucket (see
    # ``_scatter_render._bin_categories``) when it wasn't -- e.g. an
    # ngVLA-scale antenna axis with 263 real antennas renders at most
    # ``CATEGORY_CAP`` buckets like ``"DA05\u2013DA19"``, each mapping
    # to the ~13 real antenna names it covers. Always populated
    # alongside ``categories`` on a successful render -- deliberately
    # NOT ``None`` in the unbucketed case, so a consumer never needs to
    # branch on "was this binned?" before using it; checking
    # ``len(category_members[cat]) > 1`` per entry answers that already
    # where it matters (e.g. a legend tooltip listing real members).
    # Bucketing a bird's-eye view like this does not cost any real
    # per-point diagnostic power: identifying the actual antenna/scan/
    # SPW under the cursor for any given rendered point already goes
    # through the exact per-row identity mechanism (``IdentityTables``/
    # the hover-probe id grid above/``probe_scatter_region``), never
    # through decoding a pixel's color back into a category -- so a
    # bucketed color only limits how many colors are shown side by
    # side in one glance, not what can be found out about any one point.
    categories:       Optional[tuple[str, ...]] = None
    category_colors:  Optional[dict[str, str]] = None
    category_members: Optional[dict[str, tuple[str, ...]]] = None


@dataclass(frozen=True)
class ScatterRenderResult:
    """Return type of ``query_columns`` (see the DTOs above)."""
    x_range:       tuple[float, float]   # resolved full-data extent
    y_range:       tuple[float, float]
    canvas_width:  int                   # adaptive size actually used
    canvas_height: int
    layers:        tuple[ScatterLayerRender, ...]


@dataclass(frozen=True)
class ScanInfo:
    """One scan's time span and field, for local hover-probe identity
    matching (see ``VisibilityPlot._match_identity``)."""
    scan_name:  str
    field_name: str
    t_start:    float
    t_end:      float


@dataclass(frozen=True)
class SpwInfo:
    """One spectral window's per-channel frequency array, for local
    hover-probe frequency->channel matching.

    ``spw_id`` may be an ``int`` (a real SPW id) or a ``str`` (a
    spectral window name, when no numeric id is available -- see
    ``MSv2Backend._partition_spw_ident``). A partition with no
    identity at all (``None``) is excluded from
    ``IdentityTables.spws`` entirely -- an unidentifiable window
    cannot be addressed in a flag command, so carrying it here would
    only be reported later anyway.
    """
    spw_id:           object   # int | str
    frequencies:      np.ndarray   # Hz, per-channel, local index = position
    channel_width_hz: Optional[float]


@dataclass(frozen=True)
class IdentityTables:
    """Static per-(selection, polarization) identity tables.

    Built once from partition *coordinate* arrays only -- no
    VISIBILITY read, see ``MSv2Backend.identity_tables`` -- and cached
    client-side by ``VisibilityPlot._ensure_identity_tables``. Used by
    both ``VisibilityRaster`` and ``VisibilityScatter``'s hover probes
    to resolve field/scan/antenna/spw identity from native-coordinate
    ranges entirely locally, with no per-hover backend call -- see
    ``VisibilityPlot._match_identity``.

    Replaces the pre-2026-09 design where this same information was
    re-scanned from every selected partition on every single hover
    event (``probe_raster_pixel``'s original inline implementation).
    That scan touched no VISIBILITY data even then -- it was always
    this cheap -- it just ran far more often than the data underlying
    it ever changed (once per selection, not once per mouse-move).
    """
    scans:             tuple[ScanInfo, ...]
    baseline_antennas: dict   # baseline_id (int) -> (ant1_name, ant2_name)
    spws:              tuple[SpwInfo, ...]


@dataclass(frozen=True)
class _PartitionIdentity:
    """Stable, hashable identity for one raw (pre-selection) partition.

    ``_iter_visibility_partitions()`` yields a fresh ``Dataset`` wrapper
    object on every call even though the underlying data doesn't change
    -- ``id(ds)`` differs call to call, confirmed directly on real data
    -- so this exists to give repeated calls something to agree on for
    caching purposes (see ``XArrayReader._scan_lookup_for_partition``).
    Built from the partition's own SPW identity plus its raw time
    coordinate's span and length; verified stable across repeated
    ``_iter_visibility_partitions()`` calls and unique across every
    partition of a real multi-partition MS (including one where every
    partition shares the same single SPW, so SPW identity alone would
    not have been sufficient).

    Must be computed from the *raw* partition, before
    ``_apply_selection`` — a ``time_range`` selection narrows
    ``t_min``/``t_max``, so computing this from an already-selected
    ``Dataset`` would silently produce a different key for the same
    underlying partition queried under two different selections
    (missing the cache every time) or, worse, collide with a genuinely
    different partition's key.
    """
    spw_ident: object   # int | str | None -- see _partition_spw_ident
    t_min:     float
    t_max:     float
    t_size:    int


@dataclass(frozen=True)
class _PartitionScanLookup:
    """One partition's ``scan_name`` lookup, built once and cached for
    the life of the open backend.

    Added 2026-09 (colorize-by-axis Part 2 follow-up) to replace
    broadcasting ``scan_name`` across the full per-partition sample
    grid (measured at ~1.45s of near-identical fixed overhead in both
    the fused and serial ``_query_partition_scatter`` paths, on a
    modest test MS -- see ``visplot-colorize-by-axis-design.md``'s
    performance-regression note). ``time_values``/``scan_names`` are
    this partition's own raw ``time``/``scan_name`` coordinate values,
    stored **pre-sorted by time value** (sorted once here rather than
    on every ``_scan_time_index`` call, since this object is built once
    and reused for the life of the open backend).

    Applied via ``_scan_time_index`` + a fancy-index lookup at the
    caller, not a per-row value comparison -- see that method's
    docstring for why a first attempt at this (looking scan names up by
    the ``time`` *value* directly, at full output-row scale) turned out
    to still be expensive, and what replaced it.
    """
    identity:    _PartitionIdentity
    time_values: np.ndarray   # sorted ascending
    scan_names:  np.ndarray   # parallel to time_values


# ======================================================================
# Probe geometry helpers
# ======================================================================
#
# Shared by MSv2Backend and MSv4Backend so the two probe implementations
# cannot drift apart again.  Both previously carried copy-pasted cell
# geometry with the same defects; see the 2026-08 probe-miss notes in
# VisibilityScatter._handle_probe.

def _cell_bounds(coords: np.ndarray, idx: int) -> tuple[float, float]:
    """Data-space ``(lo, hi)`` bounds of cell *idx* in *coords*.

    Uses **local** neighbour spacing rather than a global
    ``(c[-1] - c[0]) / (N - 1)`` average.

    The global-average form is exact for a Datashader canvas agg, whose
    bins are uniform by construction, but it is wrong for a raw MS
    coordinate axis, which routinely is not:

    * ``time`` has large gaps between scans — an average half-width
      derived across those gaps makes every cell's window many times
      wider than the actual integration spacing, so the field/scan/
      antenna metadata lookup in ``probe_raster_pixel`` sweeps in rows
      belonging to *neighbouring scans* and reports them as though they
      were under the cursor.
    * ``frequency`` is non-uniform across concatenated spectral windows
      for the same reason.

    Local spacing degrades gracefully: on a uniform axis it reproduces
    the global answer exactly, and on a gapped axis it keeps each cell's
    window tied to its own neighbours.  Handles descending coordinate
    arrays and the degenerate single-element case.
    """
    n = len(coords)
    if n == 0:
        raise IndexError("empty coordinate array")
    if not (0 <= idx < n):
        raise IndexError(f"index {idx} out of range for {n} coordinates")

    centre = float(coords[idx])
    if n == 1:
        return centre, centre

    if idx > 0:
        half_lo = abs(centre - float(coords[idx - 1])) / 2.0
    else:
        half_lo = abs(float(coords[1]) - centre) / 2.0
    if idx < n - 1:
        half_hi = abs(float(coords[idx + 1]) - centre) / 2.0
    else:
        half_hi = abs(centre - float(coords[n - 2])) / 2.0

    return centre - half_lo, centre + half_hi


def _widen_if_degenerate(
    bounds: tuple[float, float], coords: np.ndarray
) -> tuple[float, float]:
    """Give a zero-width bin window a usable width.

    ``_cell_bounds`` returns ``(c, c)`` for a single-element coordinate
    axis, which the adaptive scatter canvas can produce at extreme zoom
    on sparse data.  A closed test against a zero-width window requires
    exact float equality, so the sample count comes back 0 even when the
    bin plainly contains points.

    The pad is sized to absorb float32 round-trip error rather than to
    guess a bin width: MS columns are frequently float32 while the agg
    coordinates are float64, so a sample and its bin centre can differ
    in the seventh significant figure while being the same number.  It
    deliberately does *not* widen far enough to capture genuinely
    different values — with a single-bin canvas there is no bin width to
    recover, and ``_compute_canvas_size`` clamps the canvas to at least
    10x10 anyway, so this is a guard against an unreachable state rather
    than a routine code path.
    """
    lo, hi = bounds
    if hi > lo:
        return bounds
    if coords.size > 1:
        pad = abs(float(coords[-1]) - float(coords[0])) / 2.0
    else:
        pad = abs(lo) * 1e-6 or 1e-9
    return lo - pad, hi + pad


def _bin_membership(
    series: "pd.Series",
    bounds: tuple[float, float],
    idx: int,
    n_bins: int,
):
    """Half-open ``[lo, hi)`` membership, closed on the final bin.

    Matches Datashader's own binning rule
    (``floor((v - v0) / (v1 - v0) * N)``).  The previous
    closed-on-both-ends test double-counted samples lying exactly on the
    edge shared by two adjacent bins.
    """
    lo, hi = bounds
    if idx >= n_bins - 1:
        return (series >= lo) & (series <= hi)
    return (series >= lo) & (series < hi)


def _agg_value(values: np.ndarray, iy: int, ix: int) -> Optional[float]:
    """Value at ``values[iy, ix]``, or ``None`` when that bin is empty.

    Empty-bin sentinels differ by reduction: ``mean``/``max``/``min``
    leave NaN, but ``count`` leaves integer ``0`` and ``any`` leaves
    ``False``.  A bare ``np.isnan`` test therefore reports every bin of
    an integer agg as populated, which would make a switch of reduction
    silently break the "is there data here?" readout.
    """
    raw = values[iy, ix]
    if np.issubdtype(values.dtype, np.floating):
        return None if np.isnan(raw) else float(raw)
    if np.issubdtype(values.dtype, np.bool_):
        return 1.0 if bool(raw) else None
    return None if raw == 0 else float(raw)


# ======================================================================
# Axis dispatch helpers
# ======================================================================

def _compute_axis_values(
    ds: xr.Dataset,
    axis: Axis,
    data_column: str = "DATA",
) -> xr.DataArray:
    """Compute the values for *axis* from *ds*.

    Returns a DataArray with dimensions drawn from the MSv4 DataTree
    schema (``time``, ``baseline_id``, ``frequency``, ``polarization``).

    Parameters
    ----------
    ds:
        An xarray Dataset from a single partition of the MSv4 DataTree.
        Expected data variables: ``VISIBILITY`` (or whatever column is
        selected), ``UVW``, ``FLAG``, ``WEIGHT``, ``WEIGHT_SPECTRUM``.
    axis:
        The axis to compute.
    data_column:
        One of ``'DATA'``, ``'CORRECTED'``, ``'MODEL'`` — selects which
        xarray variable holds the complex visibilities.  The MSv4 schema
        uses ``VISIBILITY`` for the primary data column; mapping from the
        MSv2 column names is done at open time in the backend.

    Raises
    ------
    NotImplementedError
        For calibration axes (handled by a future CalTableReader).
    ValueError
        For axis members that cannot be extracted from a visibility DS.
    """
    vis_var = _resolve_vis_variable(ds, data_column)

    match axis:
        # --- native continuous: return the coordinate directly ----------
        case Axis.TIME:
            return ds["time"]
        case Axis.FREQUENCY:
            return ds["frequency"]
        case Axis.CHANNEL:
            # channel index along the frequency dimension
            freq = ds["frequency"]
            chan = xr.DataArray(
                np.arange(freq.sizes["frequency"]),
                dims=["frequency"],
                attrs={"long_name": "Channel", "units": ""},
            )
            return chan
        case Axis.VELOCITY:
            # radio velocity — requires rest_frequency attribute on the
            # frequency coordinate; fall back gracefully
            freq = ds["frequency"]
            rest = float(freq.attrs.get("rest_frequency", np.nan))
            if np.isnan(rest):
                raise ValueError(
                    "Axis.VELOCITY requires rest_frequency to be set "
                    "on the 'frequency' coordinate."
                )
            c = 299_792_458.0  # m/s
            return (c * (rest - freq) / rest).assign_attrs(
                {"long_name": "Radio Velocity", "units": "m/s"}
            )
        case Axis.U:
            return ds["UVW"].sel(uvw_index=0).drop_vars("uvw_index")
        case Axis.V:
            return ds["UVW"].sel(uvw_index=1).drop_vars("uvw_index")
        case Axis.W:
            return ds["UVW"].sel(uvw_index=2).drop_vars("uvw_index")
        case Axis.UVDIST:
            u = ds["UVW"].sel(uvw_index=0)
            v = ds["UVW"].sel(uvw_index=1)
            return np.sqrt(u ** 2 + v ** 2).assign_attrs(
                {"long_name": "UV Distance", "units": "m"}
            )
        case Axis.INTERVAL:
            return ds["INTERVAL"]
        case Axis.ROW:
            # Row is MSv2 provenance info; in MSv4 we use a simple index
            n = ds.sizes.get("time", 1) * ds.sizes.get("baseline_id", 1)
            return xr.DataArray(
                np.arange(n),
                attrs={"long_name": "Row", "units": ""},
            )

        # --- native discrete: return non-index coordinate labels --------
        case Axis.BASELINE:
            # Return a composite label "ant1 & ant2" for display
            a1 = ds["baseline_antenna1_name"]
            a2 = ds["baseline_antenna2_name"]
            return (a1 + " & " + a2).assign_attrs({"long_name": "Baseline"})
        case Axis.ANTENNA1:
            return ds["baseline_antenna1_name"].assign_attrs(
                {"long_name": "Antenna 1"}
            )
        case Axis.ANTENNA2:
            return ds["baseline_antenna2_name"].assign_attrs(
                {"long_name": "Antenna 2"}
            )
        case Axis.CORRELATION:
            return ds["polarization"].assign_attrs({"long_name": "Correlation"})
        case Axis.SCAN:
            return ds["scan_name"].assign_attrs({"long_name": "Scan"})
        case Axis.FIELD:
            return ds["field_name"].assign_attrs({"long_name": "Field"})
        case Axis.SPW:
            # SPW is a partition-level scalar in MSv4; expose as a
            # broadcast scalar DataArray for consistency
            spw_id = int(ds.attrs.get("spectral_window_id", -1))
            return xr.DataArray(spw_id, attrs={"long_name": "SPW"})
        case Axis.OBSERVATION:
            obs_id = int(ds.attrs.get("observation_id", -1))
            return xr.DataArray(obs_id, attrs={"long_name": "Observation"})
        case Axis.INTENT:
            intent = str(ds.attrs.get("intent", ""))
            return xr.DataArray(intent, attrs={"long_name": "Intent"})

        # --- derived: compute from visibility data ----------------------
        case Axis.AMPLITUDE:
            return np.abs(ds[vis_var]).assign_attrs(
                {"long_name": "Amplitude", "units": ""}
            )
        case Axis.PHASE:
            return np.angle(ds[vis_var]).assign_attrs(
                {"long_name": "Phase", "units": "rad"}
            )
        case Axis.REAL:
            return ds[vis_var].real.assign_attrs(
                {"long_name": "Real", "units": ""}
            )
        case Axis.IMAGINARY:
            return ds[vis_var].imag.assign_attrs(
                {"long_name": "Imaginary", "units": ""}
            )
        case Axis.WEIGHT:
            return ds["WEIGHT"].assign_attrs({"long_name": "Weight"})
        case Axis.WEIGHT_SPECTRUM:
            return ds["WEIGHT_SPECTRUM"].assign_attrs(
                {"long_name": "Weight Spectrum"}
            )
        case Axis.FLAG:
            return ds["FLAG"].assign_attrs({"long_name": "Flag"})
        case Axis.UVDIST_LAMBDA:
            u = ds["UVW"].sel(uvw_index=0)
            v = ds["UVW"].sel(uvw_index=1)
            uvdist_m = np.sqrt(u ** 2 + v ** 2)
            freq = ds["frequency"]
            c = 299_792_458.0
            wavelength = c / freq  # broadcasts over (baseline_id, frequency)
            return (uvdist_m / wavelength).assign_attrs(
                {"long_name": "UV Distance", "units": "λ"}
            )
        case Axis.AZIMUTH | Axis.ELEVATION | Axis.HOUR_ANGLE | Axis.PARALLACTIC_ANGLE:
            # Observational geometry: requires POINTING subtable.
            # Stub — implementation deferred until POINTING integration.
            raise NotImplementedError(
                f"{axis.label} requires the POINTING subtable; "
                "not yet implemented."
            )

        # --- calibration table axes ------------------------------------
        case (
            Axis.GAIN_AMPLITUDE | Axis.GAIN_PHASE | Axis.DELAY
            | Axis.TSYS | Axis.SNR | Axis.OPACITY
        ):
            raise NotImplementedError(
                f"{axis.label} is a calibration-table axis and requires "
                "a CalTableReader; not supported on a visibility dataset."
            )

        case _:
            raise ValueError(f"Unknown Axis member: {axis!r}")


def _resolve_vis_variable(ds: xr.Dataset, data_column: str) -> str:
    """Map *data_column* name to the variable name in *ds*.

    MSv4 uses 'VISIBILITY' for the primary column; the backend may
    expose 'CORRECTED_DATA' and 'MODEL_DATA' as-is or under translated
    names.  This function probes the dataset and returns the first name
    that matches.
    """
    column_map = {
        "DATA": ["VISIBILITY", "DATA"],
        "CORRECTED": ["CORRECTED_DATA", "CORRECTED"],
        "MODEL": ["MODEL_DATA", "MODEL"],
    }
    candidates = column_map.get(data_column.upper(), [data_column])
    for name in candidates:
        if name in ds:
            return name
    raise KeyError(
        f"data_column={data_column!r} not found in dataset.  "
        f"Available variables: {list(ds.data_vars)}"
    )


# ======================================================================
# Abstract base class
# ======================================================================

# ---------------------------------------------------------------------------
# metadata() contract
# ---------------------------------------------------------------------------

METADATA_KEYS = frozenset({
    "scan_names",
    "field_names",
    "antenna_names",
    "spw_ids",
    "correlation_labels",
    "time_range",
    "freq_range",
    "n_baselines",
    "data_columns",
    "spws",
})
"""Exactly the keys every ``metadata()`` implementation must return.

Defined here rather than repeated in each backend's test suite, which is
how the two drifted: ``test_msv4_backend`` asserted **exact** equality
against one hardcoded list while ``test_msv2_backend`` asserted a
**subset** against another.  So the suites disagreed about what the
contract was, and neither actually compared the backends -- each compared
one backend to a list a human had to keep in sync.

Exact equality is the right rule in both directions.  A *missing* key
breaks whichever consumer reads it.  An *extra* key on one backend is a
feature that silently works against one store and not the other -- which
has happened three times (``_axis_to_dim`` arity, the DDID qualifier, the
kind resolution), each time surviving until someone ran the other
backend.

Adding a key means editing this set, which is also the place to say what
the key means:

``spw_ids``
    Window identities, as ``_partition_spw_ident`` reports them --
    numeric where the store provides ids, otherwise names.  **Not
    necessarily ints.**
``spws``
    Per-window detail: ``id``, ``kind``, ``name``, ``n_channels``,
    ``centre_freq_hz``, ``bandwidth_hz``, ``channel_width_hz``,
    ``freq_min_hz``, ``freq_max_hz``.  One entry per id in ``spw_ids``,
    in the same order.
"""

METADATA_OPTIONAL_KEYS = frozenset({
    "field_ids",
})
"""Keys a backend *may* return, with a known consumer and a known gap.

Separate from ``METADATA_KEYS`` because these represent real asymmetries
rather than drift, and collapsing the two would hide them.  A key belongs
here only while there is a documented reason a backend cannot supply it.

``field_ids``
    Authoritative FIELD_IDs, positionally aligned with ``field_names``.
    **MSv2 supplies these; MSv4 does not.**  Without them
    ``ObservationMetadata`` falls back to a positional index, which is
    *wrong* whenever the source FIELD_IDs are non-contiguous -- confirmed
    on a real MS, where alphabetically sorted ``field_names`` do not line
    up with FIELD_ID order at all.  Numeric ``field=`` selection against
    a Processing Set is therefore unreliable until MSv4 grows an
    equivalent source.  See ``ObservationMetadata.from_backend_metadata``.
"""


# ---------------------------------------------------------------------------
# Channel-index axis support (shared by both backends)
# ---------------------------------------------------------------------------
#
# Lives here rather than in each backend because per-backend copies of
# spectral-window logic have diverged three times already (``_axis_to_dim``
# arity, the DDID qualifier, the kind resolution).  The probe-geometry
# helpers were hoisted for the same reason.

CHANNEL_AXIS_ATTR = "cubevis_channel_axis"
"""Agg attribute naming which axis carries a channel index (``"x"``/``"y"``)."""

CHANNEL_REF_ATTR = "cubevis_channel_ref_hz"
"""Agg attribute holding the reference frequency coordinate, in Hz.

Kept alongside the index so the mapping is **invertible**: the probe must
turn a channel range back into a frequency range to look up fields,
scans and antennas, and ``FlagDB`` needs frequencies because they survive
the ``split``/``mstransform`` renumbering that invalidates ids.
"""


def channel_axis_is_unambiguous(freq_coords: "list[np.ndarray]") -> bool:
    """Whether a channel index is well defined across *freq_coords*.

    Requires that every partition present the **same** frequency
    coordinate.  Two partitions of one spectral window do (they differ by
    scan, field or observation, never by channel); two *different*
    windows do not, and there is no global channel numbering to fall back
    on -- MSv4 has no notion of one.

    Compared by value rather than by counting spectral windows, because
    the window identity may be only a name (see ``_partition_spw_ident``)
    and the coordinate is the thing that actually has to match.
    """
    if not freq_coords:
        return False
    first = np.asarray(freq_coords[0])
    for other in freq_coords[1:]:
        other = np.asarray(other)
        if other.shape != first.shape or not np.array_equal(other, first):
            return False
    return True


def to_channel_index(agg, axis: str, ref_freq: "np.ndarray"):
    """Relabel *agg*'s frequency coordinate as a channel index.

    Indices are positions in *ref_freq*, **not** ``arange(len(coord))``:
    ``_decimate_agg`` may have strided the axis, and after striding the
    retained cells are channels 0, 4, 8 ... not 0, 1, 2.  Numbering them
    consecutively would produce an axis that looks right and is wrong,
    and it would be wrong precisely on the zoomed-out views where
    decimation applies.

    The original frequencies are stored in ``CHANNEL_REF_ATTR`` so the
    mapping can be inverted; see ``channel_range_to_freq``.
    """
    dim = agg.dims[1] if axis == "x" else agg.dims[0]
    coord = np.asarray(agg.coords[dim].values, dtype=np.float64)
    ref = np.asarray(ref_freq, dtype=np.float64)

    # searchsorted + neighbour comparison rather than exact equality:
    # concat and decimation can perturb the last bit of a float.
    pos = np.searchsorted(ref, coord)
    pos = np.clip(pos, 0, len(ref) - 1)
    left = np.clip(pos - 1, 0, len(ref) - 1)
    take_left = np.abs(ref[left] - coord) < np.abs(ref[pos] - coord)
    idx = np.where(take_left, left, pos).astype(np.int64)

    out = agg.assign_coords({dim: idx})
    out.attrs = dict(agg.attrs)
    out.attrs[CHANNEL_AXIS_ATTR] = axis
    out.attrs[CHANNEL_REF_ATTR] = ref
    return out


def channel_range_to_freq(agg, lo: float, hi: float):
    """Invert a channel-index range on *agg* back to Hz, or ``None``.

    The probe compares its cell bounds against each partition's
    ``frequency`` coordinate.  When the axis has been relabelled those
    bounds are channel indices, and comparing them to Hz silently matches
    nothing -- which would look like an empty cell rather than an error.
    """
    ref = agg.attrs.get(CHANNEL_REF_ATTR) if hasattr(agg, "attrs") else None
    if ref is None:
        return None
    ref = np.asarray(ref, dtype=np.float64)
    if ref.size == 0:
        return None
    i_lo = int(np.clip(np.floor(lo), 0, ref.size - 1))
    i_hi = int(np.clip(np.ceil(hi), 0, ref.size - 1))
    f_a, f_b = float(ref[i_lo]), float(ref[i_hi])
    return (min(f_a, f_b), max(f_a, f_b))



class XArrayReader(abc.ABC):
    """Abstract base class for MSv2 and MSv4 data readers.

    Subclasses wrap either ``xarray-ms`` (MSv2 via ``arcae``) or
    ``xradio`` (MSv4 Zarr), presenting an identical MSv4-structured
    interface to the layers above.

    All public methods accept ``Axis`` members — never bare strings —
    for axis arguments.  Selection parameters are conveyed through
    ``SelectionSpec`` instances whose fields use human-readable string
    identifiers.

    Instances are **read-only**.  Flag persistence is the
    ``FlagDB``'s responsibility.
    """

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def open(self) -> None:
        """Open the underlying data source.

        Called once after construction.  Should be idempotent if called
        more than once.
        """

    @abc.abstractmethod
    def close(self) -> None:
        """Release resources held by the underlying data source."""

    def __enter__(self) -> "XArrayReader":
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Cached per-MS scan/antenna lookups (colorize-by-axis Part 2         #
    # follow-up, 2026-09)                                                 #
    # ------------------------------------------------------------------ #
    #
    # Shared here (concrete methods on the ABC, not abstract) rather than
    # duplicated per-backend, unlike e.g. _partition_spw_ident -- both
    # only ever need coordinates Part 2 already made uniform across both
    # backends (scan_name, baseline_id, baseline_antenna1_name/2), so
    # there's no backend-specific logic that would force a split. This
    # is also, deliberately, the same specific bug class this project
    # already hit once (OPT-B carrying its own copy of the id-cols logic
    # and quietly missing an update the other copy got) -- putting this
    # here instead means it structurally cannot recur for this piece.
    #
    # Cache storage is lazily attached to instances via getattr/setattr
    # rather than requiring an __init__ on this ABC (it doesn't have
    # one, and neither concrete backend's own __init__ needs to know
    # this exists). Each backend's close() clears both caches for
    # hygiene, but note neither holds anything but small coordinate
    # arrays/strings -- never VISIBILITY data -- so the memory cost of
    # not clearing them would be negligible even if a call site forgot.

    def _partition_identity(self, raw_ds: "xr.Dataset") -> _PartitionIdentity:
        """Stable identity for *raw_ds* -- see ``_PartitionIdentity``.

        Must be called with the raw (pre-``_apply_selection``) partition,
        never an already-selected one.
        """
        spw_ident, _kind = self._partition_spw_ident(raw_ds)
        t = raw_ds.coords["time"].values
        return _PartitionIdentity(
            spw_ident=spw_ident,
            t_min=float(t.min()) if t.size else 0.0,
            t_max=float(t.max()) if t.size else 0.0,
            t_size=int(t.size),
        )

    def _scan_lookup_for_partition(
        self, raw_ds: "xr.Dataset"
    ) -> Optional[_PartitionScanLookup]:
        """Return *raw_ds*'s scan lookup, building and caching it on
        first use; ``None`` if this partition has no ``scan_name``
        coordinate at all.

        Must be called with the raw partition (see
        ``_partition_identity``'s docstring) -- the returned lookup is
        applied afterward via ``_scan_time_index`` against the
        *selected* dataset's own (small) ``time`` coordinate, which is
        what makes correctness independent of which subset of the
        partition that particular query selected.
        """
        if "scan_name" not in raw_ds.coords or "time" not in raw_ds.coords:
            return None
        cache = getattr(self, "_scan_lookup_cache", None)
        if cache is None:
            cache = {}
            self._scan_lookup_cache = cache
        key = self._partition_identity(raw_ds)
        hit = cache.get(key)
        if hit is not None:
            return hit
        t = np.asarray(raw_ds.coords["time"].values)
        s = raw_ds.coords["scan_name"].values.astype(str)
        order = np.argsort(t)
        lookup = _PartitionScanLookup(
            identity=key, time_values=t[order], scan_names=s[order],
        )
        cache[key] = lookup
        return lookup

    def _scan_time_index(
        self,
        lookup: Optional[_PartitionScanLookup],
        ds: "xr.Dataset",
    ) -> Optional["xr.DataArray"]:
        """Small integer-position array mapping *ds*'s own ``time``
        coordinate values to their index in *lookup*'s sorted
        ``time_values`` -- e.g. shape ``(n_time,)`` for this partition,
        never the full sample grid.

        This exists because of a real, measured lesson from the first
        version of this fix: looking scan names up by *value* (via
        ``np.searchsorted`` against the millions of already-broadcast/
        raveled/filtered ``time`` values a query produces) still cost
        ~250ms per ~4M output rows in practice -- ``searchsorted``'s
        cost scales with the *query* array size, and the query array
        here is the full per-row output, not the small per-partition
        table. Antenna names never had this problem because
        ``baseline_id`` is already a small integer, cheap to fancy-index
        directly (~20ms for the same ~4M rows, confirmed by direct
        microbenchmark) -- this function gives scan_name the same
        property by doing the one *value*-based comparison exactly once,
        here, against ``ds``'s own small ``time`` coordinate (a few
        hundred elements, not millions), and returning a small integer
        array to broadcast/ravel/filter exactly like ``baseline_id``
        already is. The caller fancy-indexes ``lookup.scan_names`` with
        this (already broadcast, raveled, and filtered) index array at
        the end, rather than calling any per-row value-lookup at all.
        """
        if lookup is None or "time" not in ds.coords:
            return None
        t = np.asarray(ds.coords["time"].values)
        idx = np.searchsorted(lookup.time_values, t)
        idx = np.clip(idx, 0, len(lookup.time_values) - 1)
        return xr.DataArray(idx, dims=("time",), coords={"time": ds.coords["time"]})

    def _antenna_lookup_table(
        self,
    ) -> Optional[tuple[np.ndarray, np.ndarray]]:
        """MS-wide antenna-name lookup arrays, indexed directly by
        ``baseline_id`` (``ant1_names[bid]``, ``ant2_names[bid]``),
        built once and cached for the life of the open backend.
        ``None`` if no partition carries the needed coordinates at all.

        Matches ``identity_tables()``'s own existing assumption that
        this mapping is consistent across every partition of an open MS
        (first partition to report a given ``baseline_id`` wins) --
        unlike scan_name, this is not partition-scoped, so one MS-wide
        table suffices rather than a per-partition cache.
        """
        _unset = "_unset"
        cached = getattr(self, "_antenna_lookup", _unset)
        if cached is not _unset:
            return cached
        table: dict[int, tuple[str, str]] = {}
        for raw_ds in self._iter_visibility_partitions():
            if not ("baseline_antenna1_name" in raw_ds.coords
                    and "baseline_id" in raw_ds.coords):
                continue
            bl_ids = raw_ds.coords["baseline_id"].values.astype(np.int64)
            ant1 = raw_ds.coords["baseline_antenna1_name"].values.astype(str)
            ant2 = raw_ds.coords["baseline_antenna2_name"].values.astype(str)
            uniq_ids, first_idx = np.unique(bl_ids, return_index=True)
            for bid, idx in zip(uniq_ids, first_idx):
                bid_i = int(bid)
                if bid_i not in table:
                    table[bid_i] = (str(ant1[idx]), str(ant2[idx]))
        if not table:
            self._antenna_lookup = None
            return None
        max_bid = max(table)
        ant1_arr = np.full(max_bid + 1, "", dtype=object)
        ant2_arr = np.full(max_bid + 1, "", dtype=object)
        for bid, (a1, a2) in table.items():
            ant1_arr[bid] = a1
            ant2_arr[bid] = a2
        result = (ant1_arr, ant2_arr)
        self._antenna_lookup = result
        return result

    @staticmethod
    def _apply_antenna_lookup(
        lookup: Optional[tuple[np.ndarray, np.ndarray]],
        baseline_id_column: np.ndarray,
    ) -> Optional[tuple[np.ndarray, np.ndarray]]:
        """Vectorized map of *baseline_id_column* to (ant1, ant2) name
        arrays via *lookup* (from ``_antenna_lookup_table``)."""
        if lookup is None:
            return None
        ant1_arr, ant2_arr = lookup
        bid = baseline_id_column.astype(np.int64)
        # Out-of-range ids shouldn't occur, but this is a rendering hot
        # path -- fail soft (empty string) rather than raise.
        oob = (bid < 0) | (bid >= len(ant1_arr))
        safe = np.clip(bid, 0, len(ant1_arr) - 1)
        ant1 = np.where(oob, "", ant1_arr[safe])
        ant2 = np.where(oob, "", ant2_arr[safe])
        return ant1, ant2

    @staticmethod
    def _as_object_column(values: np.ndarray, index) -> pd.Series:
        """Wrap *values* (a numpy object-dtype array of many distinct
        strings) as a plain-``object``-dtype pandas Series, explicitly
        bypassing pandas 3.0's default string-dtype inference
        (``future.infer_string``, on by default).

        Real, measured finding, not a defensive guess: a bare
        ``df[col] = values`` assignment of an array of many distinct
        string values gets silently upgraded by pandas to its newer
        ``str`` dtype, and that conversion cost ~5x what constructing
        this Series explicitly does (~245ms vs. ~49ms, confirmed by
        direct benchmark at 4M rows) -- this turned out to be the
        single largest remaining cost in the scan/antenna lookup path,
        well past the lookup computation itself (a plain fancy-index,
        ~20ms at the same scale). Works identically whether the result
        is assigned onto an existing DataFrame (``_query_partition_scatter``)
        or passed as a dict value before a single ``pd.DataFrame(...)``
        call (OPT-B) -- both measured and confirmed.

        Deliberately local to these specific columns, not a global
        ``pd.set_option("future.infer_string", False)`` -- that would
        change pandas string-column behavior for the entire process,
        well beyond this one lookup, which is a call for the
        application to make deliberately, not something to reach for
        inside a data-reading method.

        NOT used for a *scalar* fill (``polarization``/``spw``): a
        direct ``df[col] = scalar`` assignment already uses a fast
        broadcast path despite also picking up the ``str`` dtype, and
        measured *more* expensive when wrapped this way instead --
        this helper is for the output of a fancy-index lookup (many
        distinct values), not a constant one.
        """
        return pd.Series(values, dtype=object, index=index)

    def _clear_lookup_caches(self) -> None:
        """Reset the scan/antenna lookup caches -- call from each
        backend's ``close()`` for hygiene (see this section's docstring
        for why this is a hygiene measure, not a correctness necessity)."""
        self._scan_lookup_cache = {}
        self._antenna_lookup = None


    # ------------------------------------------------------------------ #
    # Metadata                                                             #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def metadata(self) -> dict:
        """Return human-readable metadata for populating GUI controls.

        The returned dict has the following keys (all values are
        human-readable strings or Python scalars, never internal
        integer indices):

        ``scan_names`` : list[str]
            All scan name strings present in the dataset.
        ``field_names`` : list[str]
            All field name strings.
        ``field_ids`` : list[Optional[int]], optional
            Real MS/PS FIELD_ID for each entry in ``field_names``,
            aligned by position. Concrete backends SHOULD populate this
            from an authoritative source (e.g. the MS's ``FIELD``
            subtable row order for ``MSv2Backend``) rather than
            omitting it -- without it, ``ObservationMetadata`` falls
            back to a bare positional index, which silently gives wrong
            results whenever the real FIELD_IDs are non-contiguous
            (confirmed on a real MS: alphabetically-sorted field names
            do not line up with FIELD_ID order). Omit this key entirely
            (rather than returning wrong values) if no authoritative
            source is available yet.
        ``antenna_names`` : list[str]
            All antenna name strings (sorted).
        ``spw_ids`` : list[int]
            Spectral window indices present.
        ``correlation_labels`` : list[str]
            Polarization product labels, e.g. ``['XX', 'YY']``.
        ``time_range`` : tuple[float, float]
            ``(t_min, t_max)`` in MJD seconds.
        ``freq_range`` : tuple[float, float]
            ``(f_min, f_max)`` in Hz, across all SPWs.
        ``n_baselines`` : int
            Total number of unique baselines.
        ``data_columns`` : list[str]
            Available data columns, e.g. ``['DATA', 'CORRECTED']``.
        """

    # ------------------------------------------------------------------ #
    # Scatter / line mode query                                            #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def query_columns(
        self,
        xaxis: Axis,
        layers: list["ScatterLayerSpec"],
        selection: SelectionSpec,
        *,
        x_range: Optional[tuple[float, float]] = None,
        y_range: Optional[tuple[float, float]] = None,
        color_mode: str = "global",
        width: int = 800,
        height: int = 600,
        probe_grid_max_cells: int = 3072,
    ) -> "ScatterRenderResult":
        """Query, bin, and shade scatter layers; return a bounded result.

        See ``MSv2Backend.query_columns`` for the full contract and
        ``ScatterRenderResult``/``ScatterLayerRender`` for the return
        shape. Binning (Datashader ``Canvas.points()``) and shading
        (``tf.shade()``, plus the eq_hist/explicit-scaling transforms in
        ``colormap_scaling``) both happen inside this method now, not in
        ``VisibilityScatter`` -- see ``ScatterRenderResult``'s docstring
        for why (in short: the old raw-DataFrame contract shipped up to
        ~30M rows over the wire for a remote session).

        ``probe_grid_max_cells`` (2026-09, hover-probe redesign piece 2)
        controls the resolution of a second, much coarser per-bin
        native-coordinate-range grid computed alongside the display
        image -- see ``ScatterLayerRender.id_grid_*``'s docstring for
        what it carries and why it exists. Default ~3072 (about 64x48)
        is deliberately far coarser than the display canvas
        (``width``/``height``): the identity grid only has to narrow a
        hover to "roughly this range of scans/antennas/SPWs", not
        pinpoint a single sample -- that precision is what
        ``probe_scatter_region``'s click-to-exact path is for. Adjustable
        via ``VisibilityScatter.set_probe_grid_resolution()``.

        Implemented identically by both ``MSv2Backend`` and
        ``MSv4Backend`` (2026-09).
        """

    # ------------------------------------------------------------------ #
    # Raster mode query                                                    #
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def query_raster(
        self,
        y_dim: Axis,
        x_dim: Axis,
        quantity: Axis,
        selection: SelectionSpec,
        polarization: Optional[str] = None,
        max_cells: int = 2_000_000,
    ) -> tuple[xr.DataArray, tuple[float, float], tuple[float, float], bool]:
        """Return a computed 2D DataArray suitable for ``Canvas.raster()``.

        The backend reduces the selected data to a 2D float64 array by
        averaging over all dimensions not in ``(y_dim, x_dim)``, then
        decimates the result to at most ``max_cells`` cells by applying a
        uniform stride in each dimension.  The caller (``VisibilityRaster``)
        is responsible for Datashader resampling, colour mapping, and RGBA
        conversion.

        Two-level rendering contract
        ----------------------------
        The ``is_decimated`` return value drives ``VisibilityRaster``'s
        pan/zoom strategy:

        * ``is_decimated=False`` — the agg contains every data point that
          matched the selection.  Datashader resamples it for all zoom
          levels; no backend re-query is ever needed on zoom-in.
        * ``is_decimated=True`` — the agg was strided to fit within
          ``max_cells``.  Detail exists in the MS that is not in the agg.
          When the viewer zooms in past one agg cell (viewport pixel size <
          agg cell size in data units), ``VisibilityRaster`` should call
          ``query_raster`` again with a tightened ``SelectionSpec`` and a
          higher ``max_cells`` to fetch the sub-window at full resolution.

        Separating the data reduction (backend) from the canvas sizing and
        colour mapping (``VisibilityRaster``) keeps the backend free of
        canvas-size knowledge and lets the caller re-colour without
        re-reading data.

        Parameters
        ----------
        y_dim :
            Native axis for the y (row) dimension.  Must be one of
            ``Axis.TIME``, ``Axis.BASELINE``, ``Axis.FREQUENCY``,
            ``Axis.CHANNEL``.
        x_dim :
            Native axis for the x (column) dimension — same vocabulary.
        quantity :
            Derived axis to render as colour.  Must be one of
            ``Axis.AMPLITUDE``, ``Axis.PHASE``, ``Axis.REAL``,
            ``Axis.IMAGINARY``, ``Axis.FLAG``.
        selection :
            Data selection constraints (field, scan, SPW, time range,
            baselines, channel range, …).
        polarization :
            Polarization product label (e.g. ``"XX"``).  Required for
            visibility-derived quantities; ignored for ``Axis.FLAG``.
            If ``None`` and required, the backend logs a warning and
            uses the first available correlation.
        max_cells : int
            Maximum number of cells (rows × columns) in the returned agg.
            Defaults to 2,000,000 (≈16 MB at float64), which comfortably
            covers a 1000×600 canvas with ~3× oversampling.  For very
            large MSes the backend strides the reduced 2D grid to fit
            within this budget before calling ``.compute()``, so that
            only the strided rows/columns are read from disk via Dask.

        Returns
        -------
        agg : xr.DataArray
            Computed (not lazy) 2D float64 DataArray with named
            coordinates on both dimensions.  Shape is at most
            ``(n_y_cells, n_x_cells)`` where ``n_y_cells * n_x_cells
            <= max_cells``.  Passed directly to
            ``datashader.Canvas.raster()``.
        x_range : tuple[float, float]
            ``(x_min, x_max)`` — the full extent of the x coordinate in
            the *original unreduced* data (not the strided agg).  Used to
            set the Bokeh figure x_range so the axis reflects real data
            bounds even when the agg is decimated.
        y_range : tuple[float, float]
            ``(y_min, y_max)`` — the full extent of the y coordinate.
        is_decimated : bool
            ``True`` if a stride > 1 was applied in either dimension,
            meaning the agg does not contain every data point.  ``False``
            when the full reduced grid fit within ``max_cells``.
        """

    # ------------------------------------------------------------------ #
    # Pixel hover probe                                                    #
    # ------------------------------------------------------------------ #
    #
    # NOTE (2026-09, hover-probe redesign piece 3 / Chunk 2c): the
    # remote/backend forms of ``probe_raster_pixel`` and
    # ``probe_scatter_pixel`` that used to live here were removed.
    #
    # ``probe_raster_pixel`` (took a whole ``raw_grid`` DataArray and
    # shipped it P_local -> worker on every hover) was already dead:
    # piece 1 replaced raster's hover with a fully local lookup against
    # the already-cached agg (``VisibilityRaster._probe_raster_pixel_local``).
    # Nothing has called the backend/remote version since.
    #
    # ``probe_scatter_pixel`` (took a Datashader ``canvas_agg`` *and* the
    # raw per-sample ``scatter_df``) predates the "coarse but free"
    # scatter redesign and was never updated to match it -- since that
    # redesign, nothing keeps a ``scatter_df`` around to pass it (see
    # ``VisibilityScatter._layer_dfs``'s vestigial-list docstring), so
    # this method could not actually have been called successfully.
    # Confirmed dead by inspection: nothing outside this relay chain
    # ever called either method.
    #
    # ``probe_scatter_region`` below is piece 3's real replacement for
    # scatter -- see its docstring. Raster has no equivalent because it
    # never needed one: piece 1's local lookup already gives raster's
    # hover exact answers for free, and a click/drag on raster can reuse
    # the same local path (see ``VisibilityRaster._probe_raster_pixel_local``)
    # rather than a new backend method.
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def probe_scatter_region(
        self,
        x_axis: Axis,
        yaxes: list[tuple[Axis, str]],
        selection: SelectionSpec,
        x_range: tuple[float, float],
        y_range: tuple[float, float],
        max_samples: int = 200_000,
    ) -> dict[tuple[Axis, str], dict]:
        """Exact identity for every sample of each layer inside a data-space
        rectangle (hover-probe redesign piece 3, "click-to-exact").

        Complements the coarse per-bin identity grid ``query_columns``
        already computes alongside each render (see
        ``ScatterLayerRender.id_grid_*``): that grid is free (piggy-backed
        on the render pass) but coarse -- a hover reports a *range*, not
        an exact match. This method is the opposite trade: a real,
        targeted backend round trip that visits every sample actually
        inside the rectangle, in exchange for an exact answer. Meant to
        be called rarely (a user click or a drawn box), never on every
        mouse-move the way the coarse grid effectively is.

        A single clicked point and a drawn box are the same call here --
        the caller (``VisibilityScatter``) collapses a click to a tiny
        rectangle (about one canvas pixel wide, in data units) around
        the click location before calling this. There is no separate
        "point" contract to keep in sync with the "region" one.

        For each requested ``(y_axis, polarization)`` pair, this method:

        1. Computes the *exact* lazy x/y arrays for that one layer (the
           same ``_lazy_x_axis``/``_lazy_quantity`` machinery
           ``query_columns`` uses for the full render), never the
           already-binned Datashader agg.
        2. Builds a per-sample boolean mask: ``x_range[0] <= x <=
           x_range[1] and y_range[0] <= y <= y_range[1]``.
        3. Counts matches. If the running count exceeds ``max_samples``
           (checked per layer, independently -- one dense layer being
           over budget does not block a sparser overlaid layer from
           reporting normally), further partitions are skipped for that
           layer and it reports ``status="too_many_points"`` with a
           lower-bound count rather than paying to finish an answer
           nobody asked to see in full.
        4. Otherwise, reduces the mask along every dimension but one to
           get native-coordinate spans (``t_range`` over time,
           ``bl_range`` over baseline_id, ``freq_range`` over frequency
           -- Hz, not GHz) covering only the *matched* samples -- not
           the whole partition, and not a coordinate-value range test
           the way ``probe_raster_pixel`` used to do (scatter's axes are
           usually derived quantities like amplitude or UV distance, not
           native coordinates, so there is no shortcut range test to run
           against the coordinates directly -- the mask has to come from
           the actual x/y values).

        Deliberately does *not* resolve field/scan/antenna/SPW names
        itself -- the caller does that afterward via the same
        ``VisibilityPlot._match_identity`` helper pieces 1 and 2 already
        use, against ``IdentityTables`` it already has cached. That
        keeps identity-resolution logic in exactly one place.

        ``bl_range`` alone would inherit ``_match_identity``'s existing
        imprecision for pieces 1/2: matching every baseline_id
        *between* the observed min and max, not just the discrete ids
        actually present. That's an acceptable trade for a coarse hover,
        but wrong for a method whose entire purpose is being the exact
        counterpart to it -- a click whose matched baseline_ids happen
        to be non-contiguous would otherwise report antenna pairs that
        were never in the rectangle, and this is also the natural
        eventual input to a real flag command, where over-reporting
        means flagging visibilities that were never selected. So this
        method also returns ``bl_ids`` (see below) -- the literal set of
        matched baseline_ids, already sitting in the same reduced mask
        ``bl_range`` comes from, at no extra cost -- and the caller
        passes both to ``_match_identity``, which resolves antenna pairs
        by exact dict lookup when ``bl_ids`` is given instead of the
        range scan. Time -> scan/field and frequency -> SPW/channels are
        left on the existing range-based path: the same imprecision
        applies to them in principle, but a scan and an SPW are each
        already contiguous blocks, so it takes a click landing across a
        scan or SPW boundary to matter at all -- much lower probability
        and lower consequence than baseline_id, which has no such
        structure. Revisit if that assumption ever proves wrong in
        practice.

        Parameters
        ----------
        x_axis :
            Axis used for the x column of every layer (a scatter plot's
            x-axis is shared across all overlaid layers).
        yaxes :
            ``(Axis, polarization)`` pairs, one per layer to probe --
            same shape as ``query_columns``'s internal ``yaxes``. Pass
            every currently *visible* layer (``alpha > 0``); a hidden
            layer contributed nothing to what the user clicked on.
        selection :
            Data selection constraints (same selection the current
            render used).
        x_range, y_range :
            The rectangle, in data-space units for this plot's x/y axes.
            Order-independent (min/max is taken internally).
        max_samples :
            Per-layer budget on exact matches before giving up and
            reporting ``too_many_points`` instead of finishing the scan.
            Protects both the backend (a huge box can otherwise mean a
            near-full-selection scan) and the round trip (a "too many"
            answer is small regardless of how big the true count is).

        Returns
        -------
        dict keyed by ``(y_axis, polarization)``, one entry per
        requested layer, each a dict with keys:

        ``"status"`` : ``"ok"`` | ``"no_data"`` | ``"too_many_points"``
            ``"no_data"`` -- the layer has no partitions matching the
            selection, or none had a sample inside the rectangle.
        ``"n_samples"`` : int
            Exact count when ``status="ok"``; a lower bound (the count
            at the point iteration stopped) when ``"too_many_points"``;
            ``0`` for ``"no_data"``.
        ``"t_range"`` : tuple[float, float] or None
            MJD-seconds span of matched samples' ``time`` coordinate.
            ``None`` if the layer's partitions carry no ``time``
            coordinate, or ``status`` is not ``"ok"``.
        ``"bl_range"`` : tuple[float, float] or None
            ``baseline_id`` span of matched samples -- kept alongside
            ``bl_ids`` as a cheap display summary (e.g. "baselines
            12-47"). Same ``None`` conditions as ``t_range``.
        ``"bl_ids"`` : list[int] or None
            The literal, discrete set of matched ``baseline_id`` values
            (sorted), for exact antenna-pair resolution -- see the
            note above. ``None`` under the same conditions as
            ``bl_range``; empty list is possible only if ``bl_range``
            is also present but degenerate, which should not occur in
            practice since both come from the same non-empty mask.
        ``"freq_range"`` : tuple[float, float] or None
            Hz span of matched samples' ``frequency`` coordinate --
            deliberately Hz, not GHz, to match

            ``IdentityTables.spws.frequencies``'s units and
            ``VisibilityPlot._match_identity``'s ``freq_range``
            parameter directly. Same ``None`` conditions as ``t_range``.

        Implemented identically by both ``MSv2Backend`` and
        ``MSv4Backend``.
        """

    @abc.abstractmethod
    def identity_tables(
        self,
        selection: SelectionSpec,
        *,
        polarization: Optional[str] = None,
    ) -> IdentityTables:
        """Static per-selection identity tables for local hover-probe matching.

        Scans partition *coordinate* arrays only -- no VISIBILITY read,
        same access pattern ``probe_raster_pixel``'s identity lookup
        already used -- to build: every scan's (name, field, time
        span); every baseline's antenna-pair names; every SPW's
        per-channel frequency array. See ``IdentityTables``'s
        docstring for the full rationale.

        Parameters
        ----------
        selection :
            Data selection constraints.
        polarization :
            When given, a partition that doesn't locally carry this
            polarization is excluded entirely -- mirrors
            ``probe_raster_pixel``'s identical parameter and
            rationale: such a partition contributes no rendered
            pixels for that polarization, so its identity shouldn't
            be reported either. ``None`` (the default) includes every
            partition regardless of polarization -- the right default
            for a caller with no single displayed polarization to
            pass (e.g. a multi-polarization scatter overlay).

        Returns
        -------
        IdentityTables
        """

    # ------------------------------------------------------------------ #
    # Convenience                                                          #
    # ------------------------------------------------------------------ #

    # ``probe_raster_pixel`` result keys used for flagging
    # ---------------------------------------------------------------
    # ``spw_channels`` : dict[int, [c_lo, c_hi]]
    #     Channel span touched by the probed cell, per spectral window.
    #     Read from each partition's own ``frequency`` coordinate, so it
    #     is exact -- not reconstructed from an average channel width,
    #     which would be wrong on a concatenated or irregular SPW.
    #     A dict rather than a single range because one raster cell can
    #     span several concatenated SPWs.
    #
    #     This is the CASA addressing form (``spw='0:137~139'``) and is
    #     what makes a probe actionable: it is the string the astronomer
    #     retypes into ``flagdata``.
    #
    # ``spw_ids`` : list[int]
    #     The spectral windows touched, sorted.  Convenience view of
    #     ``spw_channels``' keys.
    #
    # Partitions that declare no SPW id contribute to neither, rather
    # than being reported under a sentinel key -- an unidentified window
    # cannot be addressed in a flag command, so claiming otherwise would
    # be worse than omitting it.

    def axis_info(
        self, axis: Axis, selection: Optional[SelectionSpec] = None,
        query: str = "columns",
    ) -> AxisInfo:
        """Resolve what will actually be plotted along *axis*.

        The single authority for an axis's label, unit, and dimension —
        callers must not derive any of those from the ``Axis`` enum
        directly, because a backend may substitute a different axis for
        the one requested and the label has to follow.  See ``AxisInfo``
        for why that divergence was previously possible.

        The *selection* argument matters: the answer can differ between
        "one spectral window selected, the channel index is unique" and
        "four selected, fall back to frequency".  A backend that cannot
        honour *axis* under *selection* returns
        ``AxisInfo.substituted(...)`` with a note explaining what would
        restore it.

        *query* names the path the caller will actually use --
        ``"columns"`` (scatter, via ``query_columns``) or ``"raster"``
        (via ``query_raster``).  It matters because **capability is
        per-path, not per-backend**: ``query_columns`` returns
        ``_compute_axis_values(Axis.CHANNEL)``, a real channel index,
        while ``query_raster`` resolves through ``_axis_to_dim`` to the
        frequency *coordinate*.  A single backend-level answer made the
        two panels of one plotter disagree about the same requested axis
        -- scatter labelled "Channel", raster "Frequency [GHz]".

        This default returns the axis unchanged, which is correct for
        every axis a backend does not special-case.  ``Axis.CHANNEL`` is
        marked as an index axis so it takes no unit suffix.
        """
        return AxisInfo.direct(
            axis,
            dim      = "",
            is_index = axis in (Axis.CHANNEL, Axis.ROW),
        )

    def available_axes(self) -> list[Axis]:
        """Return the subset of ``Axis`` members valid for this reader.

        Excludes ``CALIBRATION`` axes (only valid against cal tables)
        and any axes whose underlying data variables are absent from the
        open dataset.

        Override in subclasses to refine based on actual variable
        availability.
        """
        return [
            ax for ax in Axis
            if ax.axis_type is not AxisType.CALIBRATION
        ]
