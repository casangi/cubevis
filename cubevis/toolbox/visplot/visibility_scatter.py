"""
visibility_scatter.py
=====================
Datashader scatter plot for visibility data with multiple overlaid layers.

Each layer is a ``ScatterLayer`` specifying a y-axis quantity, polarization,
color map, and alpha value.  All layers share the same x-axis and
``SelectionSpec``.

Rendering pipeline (2026-09 redesign)
--------------------------------------
Binning and shading now happen inside ``backend.query_columns()``
itself (``MSv2Backend``/``MSv4Backend``, in-process for a local
session or in the worker subprocess for a remote one) -- see
``ScatterRenderResult``'s docstring in ``data/reader.py`` for the full
rationale (in short: the pre-redesign contract shipped raw DataFrames
that could reach tens of millions of rows, which was fine in-process
but unworkable over the wire for a remote session). For each layer,
the backend returns a small ``ScatterLayerRender``: an already-shaded
RGBA image plus a handful of scalars/small arrays (``n_in_view``, a
histogram, a colorbar mapping curve) -- never raw rows.

What still happens here, in ``_collapse_and_composite``::

    layer_alpha = auto_alpha(n_in_view, canvas_pixels) * layer.alpha
    <collapse the returned image's alpha channel to layer_alpha>

    tf.stack(*images, how="over")  [reimplemented in numpy on uint32 ARGB]
        -> single composite image

This split is what keeps ``set_alpha()`` a free, no-backend-call
operation: it only needs ``n_in_view`` (returned) and the live
``layer.alpha``, never the raw data. Everything else that used to be
"free" (pan/zoom, ``update_scaling``, ``set_color_mode``,
``set_layer_cmaps``) is now a backend round trip, because it changes
something the backend's shading step depends on -- see ``_rerender``.

The composite is pushed to a single Bokeh ``image_rgba`` glyph via
``_image_source``, as before.

Axis switching
--------------
``update_axes(x_dim=, layers=)`` re-queries all layers and composites.
Passing new ``layers`` replaces the layer list entirely.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/visibility_scatter.py``
"""

from __future__ import annotations

import logging
import math
import os
import time
import asyncio
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING
from uuid import uuid4
from html import escape as _html_escape

import numpy as np
import xarray as xr

from bokeh.models import ColumnDataSource

from .visibility_plot import (
    VisibilityPlot, _img_to_uint32, _json_num,
)
from .panel_spec import ColorBand, PanelSpec
from . import colormap_scaling as _cms
from .data.reader import ScatterLayerSpec
from cubevis.bokeh.tools._info_tool import InfoTool

if TYPE_CHECKING:
    import pandas as pd
    from .visibility_reader import VisibilityReader
    from .selection import SelectionSpec
    from .axes import Axis

log = logging.getLogger(__name__)

try:
    import datashader as ds
    import datashader.reductions as ds_agg
    import datashader.transfer_functions as tf
    HAS_DATASHADER = True
except ImportError:
    HAS_DATASHADER = False

_DEFAULT_SCALING = "eq_hist"

# Default color maps for successive layers
_MIN_ALPHA = 90
"""Alpha floor for the sparsest populated pixel, of 255.

Datashader defaults to 40, which makes a single-point pixel nearly
invisible against either background -- the low-end complaint that
``palettes.condition`` could only half-address, because conditioning
moves the *colour* while alpha decides how much of it survives.

Measured against the dark ground with the conditioned ``polar[0]`` ramp,
distance from background at the sparse end:

    min_alpha    40 ->  23.7
                 60 ->  35.6
                 90 ->  53.3      <- chosen
                120 ->  71.1
                160 ->  94.8

Higher is not better: dense pixels stay at ~330 regardless, so raising
the floor compresses the dynamic range.  At 160 a nearly-empty pixel is
already a third of the way to a full one and genuine density structure
flattens out.  90 roughly doubles sparse visibility while leaving the
low quarter of the range distinguishable.

Applies to scatter only.  A raster cell is opaque and has no sparse end.
"""

_LAYER_CMAPS = [
    # Plasma
    ["#0d0887","#46039f","#7201a8","#9c179e","#bd3786",
     "#d8576b","#ed7953","#fb9f3a","#fdcb26","#f0f921"],
    # Inferno
    ["#000004","#1b0c41","#4a0c4e","#781c6d","#a52c60",
     "#cf4446","#ed6925","#fb9b06","#f7d13d","#fcffa4"],
    # Viridis
    ["#440154","#482878","#3e4989","#31688e","#26828e",
     "#1f9e89","#35b779","#6ece58","#b5de2b","#fde725"],
    # Magma
    ["#000004","#180f3d","#440f76","#721f81","#9f2f7f",
     "#cd4071","#f1605d","#fd9668","#feca8d","#fcfdbf"],
]


# ---------------------------------------------------------------------------
# ScatterLayer dataclass
# ---------------------------------------------------------------------------

@dataclass
class ScatterLayer:
    """Specification for one scatter plot layer.

    Parameters
    ----------
    y_axis : Axis
        The y-axis quantity (e.g. ``Axis.AMPLITUDE``, ``Axis.PHASE``).
    polarization : str
        Correlation product label (e.g. ``"XX"``).
    cmap : list[str] | None
        Color map hex strings.  ``None`` → assigned from ``_LAYER_CMAPS``
        cycle based on layer index.
    alpha : float
        Opacity in [0.0, 1.0].  0.0 = transparent (hidden), 1.0 = opaque.
    label : str
        Human-readable label for legend / toggle widgets.  Auto-generated
        from ``y_axis.label`` and ``polarization`` if empty.
    scaling : str
        Value-to-color transfer function for this layer.  One of
        ``colormap_scaling.ALL_SCALINGS``.  Defaults to ``"eq_hist"``
        (histogram equalization), which resolves the low-amplitude
        saturation seen under linear scaling on real visibility data —
        see ``colormap_scaling`` module docstring for the full
        rationale.
    scaling_alpha : float
        Parameter for ``"log"`` and ``"power"`` scalings.
    scaling_gamma : float
        Parameter for ``"gamma"`` scaling.
    scaling_vmin, scaling_vmax : float | None
        Manual value-domain clip range, overriding the automatic
        ``color_mode``-based range once both are set. ``None`` (default)
        means automatic. Clips the input (rather than setting a
        Datashader ``span=``) for ``"eq_hist"`` scaling.
    coloring : str
        ``"continuous"`` (default) or ``"categorical"`` — mirrors
        ``data.reader.ScatterLayerSpec.coloring`` (Part 3); see that
        dataclass's docstring for the full rationale (a string, not a
        bool, so a future third mode doesn't need a rework). Widget-side
        state only — ``_render_all_layers`` copies it into the
        ``ScatterLayerSpec`` sent to the backend on every render.
    colorize_axis : Axis | None
        Which axis to colorize by when ``coloring == "categorical"``.
        Must be one of ``data.reader.colorizable_axes()``; ``None`` is
        only valid when ``coloring == "continuous"`` — validated eagerly
        in ``__post_init__``, mirroring ``ScatterLayerSpec``'s own
        validation exactly (this class is the widget-side twin of that
        one, so the two must never accept a combination the other
        rejects).
    """
    y_axis:      "Axis"
    polarization: str        = "XX"
    cmap:         Optional[list] = None
    alpha:        float      = 1.0
    label:        str        = ""
    scaling:        str   = _DEFAULT_SCALING
    scaling_alpha:  float = 10.0
    scaling_gamma:  float = 1.0
    scaling_vmin:   Optional[float] = None  # manual override; None = auto
    scaling_vmax:   Optional[float] = None  # (see update_scaling, _shade_all_layers)
    coloring:       str = "continuous"
    colorize_axis:  Optional["Axis"] = None
    excluded_categories: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.label:
            self.label = f"{self.y_axis.label} {self.polarization}"
        if self.coloring not in ("continuous", "categorical"):
            raise ValueError(
                "ScatterLayer.coloring must be 'continuous' or "
                f"'categorical', got {self.coloring!r}"
            )
        if self.coloring == "categorical":
            if self.colorize_axis is None:
                raise ValueError(
                    "ScatterLayer.coloring='categorical' requires "
                    "colorize_axis to be set"
                )
        else:
            if self.colorize_axis is not None:
                raise ValueError(
                    "ScatterLayer.colorize_axis is only valid when "
                    "coloring='categorical'"
                )
            if self.excluded_categories:
                raise ValueError(
                    "ScatterLayer.excluded_categories is only valid when "
                    "coloring='categorical'"
                )


# ---------------------------------------------------------------------------
# VisibilityScatter
# ---------------------------------------------------------------------------

class VisibilityScatter(VisibilityPlot):
    """Multi-layer Datashader scatter plot for visibility data.

    Parameters
    ----------
    backend : VisibilityReader
        Opened reader (``LocalVisibilityReader`` wrapping an
        ``MSv2Backend`` or ``MSv4Backend``, or a
        ``RemoteReductionContext`` for remote sessions).
    selection : SelectionSpec
        Data selection.
    x_axis : Axis
        The x-axis (e.g. ``Axis.UVDIST``, ``Axis.TIME``).
    layers : list[ScatterLayer]
        One or more scatter layers.  Each specifies a y-axis quantity,
        polarization, color map, and alpha.
    width, height : int
        Canvas dimensions in pixels.
    title : str | None
        Figure title; ``None`` → auto-generated.
    comm_mgr :
        ``CommMgr`` from the active ``BokehAppContext``.
    probe_slop_px : float
        How far, in *screen* pixels, the hover probe looks beyond the
        hovered bin for a populated one.  Converted to a bin radius per
        render using the current adaptive canvas size, so the tolerance
        feels the same at every zoom level.  Default ``6.0``; when a bin
        is already wider than this the radius resolves to 0 and only the
        exact bin is consulted.
    probe_search_radius : int | None
        Fixed radius in canvas bins, overriding ``probe_slop_px``.
        ``0`` forces strict exact-bin lookup.  Default ``None`` (derive
        from ``probe_slop_px``).
    probe_debug : bool
        Log one INFO line per hover describing what each layer's agg
        returned.  Also enabled by setting ``VISPLOT_PROBE_DEBUG=1``.
    enable_info_tool : bool
        Whether to add the scatter-only "i" InfoTool (hover-probe
        redesign piece 3, click-to-exact) to the figure toolbar.
        Independent of ``enable_flagging`` -- a quick-look session with
        flagging disabled may still want exact metadata lookup. Default
        ``True``.
    probe_region_max_samples : int
        Per-layer budget on exact matches the InfoTool's backend lookup
        will fully resolve before reporting "too many points, narrow
        your selection" instead. See
        ``XArrayReader.probe_scatter_region``'s docstring for why this
        guard exists. Default ``200_000``.
    """

    def __init__(
        self,
        backend: "VisibilityReader",
        selection: "SelectionSpec",
        x_axis: "Axis",
        layers: list[ScatterLayer],
        layer_cmaps: Optional[list] = None,
        width: int  = 900,
        height: int = 600,
        title: Optional[str] = None,
        comm_mgr=None,
        color_mode: str = "global",
        probe_slop_px: float = 6.0,
        probe_search_radius: Optional[int] = None,
        probe_debug: bool = False,
        probe_grid_max_cells: int = 3072,
        enable_info_tool: bool = True,
        probe_region_max_samples: int = 200_000,
        **kwargs,
    ) -> None:
        if not layers:
            raise ValueError("VisibilityScatter: layers must be non-empty")

        # Assign default color maps by layer index.  The family is an
        # instance attribute, not the module constant, because it is
        # theme-dependent: VisibilityPlotter resolves it through
        # ``palettes`` and passes it in.  A layer arriving from a j2p
        # payload has no cmap (the browser has no reason to send one) and
        # is filled from here -- see _with_default_cmaps.
        self._layer_cmaps = list(layer_cmaps or _LAYER_CMAPS)
        self._layers: list[ScatterLayer] = self._with_default_cmaps(layers)

        # Per-layer render state, cached from the last backend
        # query_columns() call (2026-09 redesign -- see
        # ScatterRenderResult's docstring in data/reader.py). The
        # backend now bins and shades; what's cached here is its
        # bounded result, not raw data.
        n = len(layers)
        self._layer_images:      list[Optional[np.ndarray]] = [None] * n
        self._layer_n_in_view:   list[int]                  = [0] * n
        self._layer_peak:        list[Optional[float]]      = [None] * n
        self._layer_hist_counts: list[Optional[np.ndarray]] = [None] * n
        self._layer_hist_edges:  list[Optional[np.ndarray]] = [None] * n
        self._layer_mapping:     list[Optional["_cms.ScalarMapping"]] = [None] * n
        self._layer_skip_reason: list[Optional[str]]        = [None] * n
        # Colorize-by-axis (Part 4, 2026-09): populated together on a
        # successful categorical render, all None for a continuous or
        # skipped/empty categorical one -- mirrors
        # ScatterLayerRender.categories/category_colors/category_members
        # in data/reader.py exactly (these ARE that data, just cached
        # client-side the same way peak/hist/mapping already are).
        # Read by colorize_controls()'s legend Div and by _panel_spec()
        # (which copies them onto ColorBand for png_export.py).
        self._layer_categories:        list[Optional[tuple]] = [None] * n
        self._layer_category_colors:   list[Optional[dict]]  = [None] * n
        self._layer_category_members:  list[Optional[dict]]  = [None] * n
        # Cache of each layer's continuous cmap, keyed by layer index,
        # populated only once a layer actually switches to categorical
        # (see update_colorize) -- lets switching back restore the
        # original ramp instead of leaving the categorical palette
        # assigned to a continuous render.
        self._layer_continuous_cmap_backup: dict[int, tuple] = {}
        self._canvas_width, self._canvas_height           = width, height
        self._full_canvas_width, self._full_canvas_height = width, height

        # Hover-probe redesign piece 2 (2026-09): coarse per-bin
        # native-coordinate-range + value grid, one entry per layer,
        # cached from the same query_columns() response as everything
        # above. Each entry is a dict with keys "value", "t_lo", "t_hi",
        # "bl_lo", "bl_hi", "f_lo", "f_hi", "x_range", "y_range" (any of
        # the six range keys may be absent if that native coordinate
        # wasn't available -- see ScatterLayerRender.id_grid_*'s
        # docstring), or None for a layer with no data at all
        # (skip_reason set). This is what lets _handle_probe resolve
        # hover locally -- no backend call -- for both an approximate
        # value AND field/scan/antenna/spw identity together, matching
        # (coarsely) everything the pre-redesign per-hover backend call
        # used to provide. probe_scatter_pixel (click-to-exact, piece 3)
        # remains the source of an exact reading on demand.
        self._layer_id_grid: list[Optional[dict]] = [None] * n
        self._probe_grid_max_cells: int = int(probe_grid_max_cells)

        # Vestigial -- kept ONLY so any remaining code path that still
        # checks these guards degrades gracefully instead of an
        # AttributeError. Never populated with real data since the
        # 2026-09 redesign moved binning/shading server-side --
        # _handle_probe itself no longer reads these (see
        # _layer_id_grid above and the piece-2 rewrite of
        # _handle_probe), but _layer_extents in particular is still
        # referenced by debug logging below.
        self._layer_dfs:     list[Optional["pd.DataFrame"]] = [None] * n
        self._layer_aggs:    list[Optional["xr.DataArray"]] = [None] * n
        self._layer_extents: list[Optional[tuple]]          = [None] * n

        # Hover-probe tuning.  ``probe_slop_px`` is how far, in *screen*
        # pixels, the probe will look beyond the hovered bin for a
        # populated one.  Screen pixels rather than bins because the
        # adaptive canvas changes bin size by more than an order of
        # magnitude: a bins-based radius of 1 is 3x3 screen px on a full
        # 900x600 canvas but 108x67 once the canvas shrinks to 25x27,
        # which is generous in exactly the case where the drawn mark is
        # already a whole bin wide and needs no help at all.
        #
        # At the default 6 px this means: single-pixel marks on a dense
        # canvas get a few pixels of forgiveness, while a shrunken
        # canvas whose bins are bigger than the budget resolves to
        # radius 0 -- exact-bin lookup, which already covers the entire
        # visible mark.
        #
        # ``probe_search_radius`` overrides the computation with a fixed
        # radius in bins; None (default) means derive it.  Setting 0
        # forces strict exact-bin behaviour, useful for isolating
        # whether a reported miss is a slop problem or a layer problem.
        #
        # ``probe_debug`` logs one line per hover at INFO with each
        # layer's agg shape, bin size, hovered and matched pixel, and
        # hit distance; it also honours the VISPLOT_PROBE_DEBUG
        # environment variable so it can be switched on without
        # touching a notebook cell.
        self._probe_slop_px: float = float(probe_slop_px)
        self._probe_search_radius: Optional[int] = (
            None if probe_search_radius is None else int(probe_search_radius)
        )
        self._probe_debug: bool = bool(probe_debug) or bool(
            os.environ.get("VISPLOT_PROBE_DEBUG")
        )

        # Current viewport — updated on every pan/zoom rerender so that
        # set_alpha / set_color_mode re-composite over the correct region.
        self._current_viewport: Optional[tuple[float,float,float,float]] = None

        # x_axis is stored as y_dim placeholder; base class uses _x_dim / _y_dim
        # for viewport narrowing.  For scatter, _y_dim is unused but must be set.
        # We use the first layer's y_axis as the canonical _y_dim for the base.
        self._color_mode       = color_mode
        self._msg_update_axes  = str(uuid4())
        self._msg_set_alpha    = str(uuid4())
        self._msg_color_mode   = str(uuid4())
        self._msg_update_scaling = str(uuid4())
        # No self._msg_colorize (Part 5, 2026-09): colorize is staged,
        # not live -- see colorize_controls()'s docstring. Removed
        # rather than left allocated-but-unregistered, since an unused
        # message id with no handler is exactly the kind of thing that
        # looks like a bug on the next read-through.

        # Hover-probe redesign piece 3 (2026-09), "click-to-exact":
        # scatter-only InfoTool (drag tool, click -> point, drag -> box)
        # backed by a dedicated Comm -- see _add_info_tool(). Set here
        # (before super().__init__()) so they exist by the time _build()
        # runs, matching the _msg_* convention above. _info_comm/
        # _info_tool themselves can't be created yet -- they need
        # self._comm_mgr, which super().__init__() sets up -- so those
        # stay None until _add_info_tool() runs from this class's own
        # _build() override, after super()._build() returns.
        self._msg_probe_region        = str(uuid4())
        self._enable_info_tool        = bool(enable_info_tool)
        self._probe_region_max_samples = int(probe_region_max_samples)
        self._info_comm = None
        self._info_tool = None

        super().__init__(
            backend   = backend,
            selection = selection,
            y_dim     = layers[0].y_axis,
            x_dim     = x_axis,
            width     = width,
            height    = height,
            title     = title,
            comm_mgr  = comm_mgr,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Public API extensions
    # ------------------------------------------------------------------

    @property
    def layers(self) -> list[ScatterLayer]:
        """The current scatter layer list."""
        return list(self._layers)

    def set_alpha(self, layer_index: int, alpha: float) -> None:
        """Set the alpha for a single layer and re-composite.

        Does NOT re-query the backend — only re-runs shade + stack.

        Parameters
        ----------
        layer_index : int
            Zero-based index into ``self.layers``.
        alpha : float
            New opacity in [0.0, 1.0].
        """
        if not (0 <= layer_index < len(self._layers)):
            raise IndexError(f"layer_index {layer_index} out of range")
        lyr = self._layers[layer_index]
        self._layers[layer_index] = ScatterLayer(
            y_axis        = lyr.y_axis,
            polarization  = lyr.polarization,
            cmap          = lyr.cmap,
            alpha         = max(0.0, min(1.0, alpha)),
            label         = lyr.label,
            scaling       = lyr.scaling,
            scaling_alpha = lyr.scaling_alpha,
            scaling_gamma = lyr.scaling_gamma,
            scaling_vmin  = lyr.scaling_vmin,
            scaling_vmax  = lyr.scaling_vmax,
            coloring      = lyr.coloring,
            colorize_axis = lyr.colorize_axis,
            excluded_categories = lyr.excluded_categories,
        )
        self._recomposite()
        self._update_state_source()

    def update_scaling(
        self,
        layer_index: int,
        scaling: Optional[str] = None,
        alpha: Optional[float] = None,
        gamma: Optional[float] = None,
        vmin: Optional[float] = None,
        vmax: Optional[float] = None,
        reset_range: bool = False,
    ) -> None:
        """Change one layer's value-to-color transfer function and re-render.

        POST-2026-09: this now issues a backend ``query_columns()`` call
        (see ``_rerender``) rather than only re-running shade + stack
        locally -- scaling/vmin/vmax are ``tf.shade()``/eq_hist
        parameters, and that step now runs backend-side (see
        ``ScatterRenderResult``'s docstring in ``data/reader.py``).
        Unlike ``set_alpha()``, this is no longer a free operation.

        Parameters
        ----------
        layer_index : int
            Zero-based index into ``self.layers``.
        scaling : str | None
            One of ``colormap_scaling.ALL_SCALINGS``.  ``None`` keeps the
            current value.
        alpha : float | None
            Parameter for ``"log"`` / ``"power"`` scalings.  ``None``
            keeps the current value.
        gamma : float | None
            Parameter for ``"gamma"`` scaling.  ``None`` keeps the
            current value.
        vmin, vmax : float | None
            Manual value-domain clip range, overriding the automatic
            ``color_mode``-based range (see
            ``data._scatter_render.render_layer``) once set. ``None``
            keeps the current value on its own — use
            ``reset_range=True`` to clear a previously-set override
            (``colormap_controls()``'s reset button). Clips the
            reference population (rather than setting a Datashader
            ``span=``) for ``"eq_hist"`` scaling.
        reset_range : bool
            If ``True``, clears both ``vmin`` and ``vmax`` back to
            ``None`` for this layer, applied before any ``vmin``/``vmax``
            passed in the same call.
        """
        if not (0 <= layer_index < len(self._layers)):
            raise IndexError(f"layer_index {layer_index} out of range")
        if scaling is not None and scaling not in _cms.ALL_SCALINGS:
            raise ValueError(
                f"scaling must be one of {_cms.ALL_SCALINGS}, got {scaling!r}"
            )
        lyr = self._layers[layer_index]
        new_vmin = None if reset_range else lyr.scaling_vmin
        new_vmax = None if reset_range else lyr.scaling_vmax
        self._layers[layer_index] = ScatterLayer(
            y_axis        = lyr.y_axis,
            polarization  = lyr.polarization,
            cmap          = lyr.cmap,
            alpha         = lyr.alpha,
            label         = lyr.label,
            scaling       = scaling if scaling is not None else lyr.scaling,
            scaling_alpha = alpha if alpha is not None else lyr.scaling_alpha,
            scaling_gamma = gamma if gamma is not None else lyr.scaling_gamma,
            scaling_vmin  = vmin if vmin is not None else new_vmin,
            scaling_vmax  = vmax if vmax is not None else new_vmax,
            coloring      = lyr.coloring,
            colorize_axis = lyr.colorize_axis,
            excluded_categories = lyr.excluded_categories,
        )
        self._rerender()
        self._update_state_source()

    def update_colorize(
        self,
        layer_index: int,
        coloring: Optional[str] = None,
        colorize_axis=None,
        excluded_categories=None,
    ) -> None:
        """Change one layer's colorize-by-axis mode and re-render.

        Part 5 (2026-09): retained as the underlying state-mutation
        method (still the one place cmap swap-in/out and axis
        validation happen), but no longer reachable live from the
        browser -- see ``colorize_controls()``'s docstring for why the
        UI moved to a staged model. Still directly callable
        programmatically.

        Parameters
        ----------
        layer_index : int
            Zero-based index into ``self.layers``.
        coloring : str | None
            ``"continuous"`` or ``"categorical"``.  ``None`` keeps the
            layer's current value.
        colorize_axis : Axis | str | None
            The axis to colorize by.  Accepts an ``Axis`` member or its
            ``.name`` string (j2p messages carry the string form —
            mirrors ``_parse_axis``'s convention elsewhere in this
            file).  ``None`` keeps the layer's current axis *unless*
            the effective mode is ``"categorical"`` and the layer has
            never had one, in which case the first non-degenerate
            colorizable axis is chosen — the same default
            ``colorize_controls()``'s picker itself opens on, so a
            layer switched straight to categorical (no explicit axis
            picked yet) renders something instead of raising.
        excluded_categories : tuple[str, ...] | None
            Raw category values (see ``ScatterLayerSpec.excluded_categories``'s
            docstring) to leave out of the render. ``None`` keeps the
            layer's current value when staying categorical; always
            reset to ``()`` when switching to (or staying) continuous,
            mirroring how *colorize_axis* itself is dropped in that
            case.

        Raises
        ------
        IndexError
            *layer_index* out of range.
        ValueError
            An unresolvable mode/axis combination, or *colorize_axis*
            names an axis outside ``data.reader.colorizable_axes()``.
        """
        from .axes import Axis
        from .data.reader import colorizable_axes, DEGENERATE_COLORIZE_AXES

        if not (0 <= layer_index < len(self._layers)):
            raise IndexError(f"layer_index {layer_index} out of range")
        lyr = self._layers[layer_index]

        new_coloring = coloring if coloring is not None else lyr.coloring
        if new_coloring not in ("continuous", "categorical"):
            raise ValueError(
                f"coloring must be 'continuous' or 'categorical', "
                f"got {new_coloring!r}"
            )

        if isinstance(colorize_axis, str):
            try:
                colorize_axis = Axis[colorize_axis]
            except KeyError:
                raise ValueError(f"unknown axis {colorize_axis!r}") from None

        if new_coloring == "continuous":
            # Mirrors ScatterLayerSpec.__post_init__: an axis is only
            # ever valid alongside categorical mode -- silently dropped
            # here rather than left dangling from a previous categorical
            # selection, exactly as switching back to continuous should
            # behave from the user's point of view (the axis picker
            # itself is hidden in this mode -- see colorize_controls()).
            new_axis = None
            new_excluded = ()
        else:
            new_axis = colorize_axis if colorize_axis is not None else lyr.colorize_axis
            if new_axis is None:
                candidates = [
                    a for a in colorizable_axes()
                    if a not in DEGENERATE_COLORIZE_AXES
                ]
                if not candidates:
                    raise ValueError("no colorizable axes available")
                new_axis = candidates[0]
            elif new_axis not in colorizable_axes():
                raise ValueError(
                    f"Axis.{new_axis.name} is not a colorizable axis -- "
                    "see data.reader.COLORIZE_AXIS_COLUMNS"
                )
            new_excluded = (tuple(excluded_categories) if excluded_categories is not None
                            else lyr.excluded_categories)

        # cmap is REUSED for categorical mode (Part 3 convention -- see
        # ScatterLayerSpec's docstring): swap in a categorical palette
        # when entering it, and restore whatever continuous cmap the
        # layer had before when leaving it, rather than leaving a
        # discrete category palette assigned to a continuous ramp (would
        # render, just not with the intended gradient). The pre-
        # categorical cmap is cached per layer index the first time a
        # layer goes categorical -- __init__ doesn't need to populate
        # this, only entries that have actually made the switch exist.
        new_cmap = lyr.cmap
        if new_coloring == "categorical" and lyr.coloring != "categorical":
            self._layer_continuous_cmap_backup[layer_index] = lyr.cmap
            from . import palettes
            new_cmap = tuple(palettes.categorical_cmap(theme=self._theme_hint()))
        elif new_coloring == "continuous" and lyr.coloring == "categorical":
            new_cmap = self._layer_continuous_cmap_backup.pop(
                layer_index, self._layer_cmaps[layer_index % len(self._layer_cmaps)],
            )

        self._layers[layer_index] = ScatterLayer(
            y_axis        = lyr.y_axis,
            polarization  = lyr.polarization,
            cmap          = new_cmap,
            alpha         = lyr.alpha,
            label         = lyr.label,
            scaling       = lyr.scaling,
            scaling_alpha = lyr.scaling_alpha,
            scaling_gamma = lyr.scaling_gamma,
            scaling_vmin  = lyr.scaling_vmin,
            scaling_vmax  = lyr.scaling_vmax,
            coloring      = new_coloring,
            colorize_axis = new_axis,
            excluded_categories = new_excluded,
        )
        self._rerender()
        self._update_state_source()

    def histogram(
        self, layer_index: int, bins: int = 254,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(counts, bin_edges)`` for one layer, from the backend.

        POST-2026-09: computed server-side, against the true per-sample
        reference population, and cached from the last render (see
        ``ScatterLayerRender.hist_counts``/``hist_edges`` in
        ``data/reader.py``) -- not rebinned here from a local agg, which
        no longer exists client-side.

        Used by ``colormap_controls()`` for the histogram display
        alongside the scaling controls.  Returns empty arrays if the
        layer hasn't been rendered yet.

        ``bins`` is honored only in that the backend always computes
        254 bins (matching this method's own default); a caller asking
        for a different count gets a warning and the cached result
        anyway, rather than an exact rebin the true per-sample values
        aren't available here to produce.
        """
        if not (0 <= layer_index < len(self._layer_hist_counts)):
            raise IndexError(f"layer_index {layer_index} out of range")
        counts = self._layer_hist_counts[layer_index]
        edges  = self._layer_hist_edges[layer_index]
        if counts is None or edges is None:
            return np.array([]), np.array([])
        if bins != len(counts):
            log.warning(
                "histogram(layer_index=%d, bins=%d): backend always "
                "computes %d bins; returning that instead of an exact "
                "rebin (the per-sample values aren't available here)",
                layer_index, bins, len(counts),
            )
        return counts, edges

    def colormap_controls(self, layer_index: int = 0):
        """Return a Bokeh widget column for one layer's scaling controls.

        ``VisibilityPlotter``'s sidebar embeds one of these per layer —
        see Phase 0 CM-4 in the implementation plan. Mirrors
        ``VisibilityRaster.colormap_controls()``; see that docstring for
        the full rationale on the ``CustomJS``/``comm.send()`` wiring
        pattern, the histogram+``EditSpan`` design (modeled on
        ``iclean``'s ``colormap_adjust()``), and the "rebuild on Plot
        press, not on every pan/zoom" scope decision. Scaling is
        per-layer here since each layer has its own colormap and
        quantity — ``layer_index`` is baked into every outgoing message
        so the response updates the right layer.

        This used to be missing min/max fields entirely (unlike
        ``VisibilityRaster``'s, which existed but weren't wired) —
        added here to bring the two into parity.

        Parameters
        ----------
        layer_index : int
            Which layer's controls to build.  Defaults to the first
            layer.
        """
        from bokeh.layouts import column, row
        from bokeh.models import Select, TextInput, Div, CustomJS, Button, BuiltinIcon, InlineStyleSheet, Spacer
        from bokeh.plotting import figure
        from bokeh.events import ValueSubmit
        from cubevis.bokeh.models._edit_span import EditSpan

        if not (0 <= layer_index < len(self._layers)):
            raise IndexError(f"layer_index {layer_index} out of range")
        lyr = self._layers[layer_index]

        equation = Div(text=_cms.scaling_equation_label(lyr.scaling))
        scaling_select = Select(
            title=f"Color scaling — {lyr.label}",
            value=lyr.scaling,
            options=list(_cms.ALL_SCALINGS),
        )
        alpha_input = TextInput(
            title="alpha", value=str(lyr.scaling_alpha),
            visible=lyr.scaling in ("log", "power"),
        )
        gamma_input = TextInput(
            title="gamma", value=str(lyr.scaling_gamma),
            visible=lyr.scaling == "gamma",
        )

        # --- Histogram + draggable min/max span pair ------------------
        counts, edges = self.histogram(layer_index)
        if counts.size == 0:
            edges = np.array([0.0, 1.0])
            counts = np.array([0])
        hist_lo, hist_hi = float(edges[0]), float(edges[-1])
        min_loc = lyr.scaling_vmin if lyr.scaling_vmin is not None else hist_lo
        max_loc = lyr.scaling_vmax if lyr.scaling_vmax is not None else hist_hi

        hist_source = ColumnDataSource(data={
            "left":   edges[:-1],
            "right":  edges[1:],
            "top":    counts,
            "bottom": np.zeros_like(counts),
        })
        hist_fig = figure(
            height=100, width=260,
            # REVERTED -- see VisibilityRaster.colormap_controls'
            # identical block: the toolbar-restoration hypothesis made
            # click-count worse (2->3), confirmed by testing. Real root
            # cause was unrelated -- see the EditSpan `dragging`
            # property fix and its wiring below.
            toolbar_location=None, tools="",
            y_axis_label=None,
        )
        hist_fig.quad(
            left="left", right="right", top="top", bottom="bottom",
            source=hist_source, fill_color="#89b4fa", line_color=None,
            fill_alpha=0.8,
        )
        hist_fig.yaxis.visible = False
        hist_fig.ygrid.visible = False
        # See VisibilityRaster.colormap_controls' identical block for the
        # full rationale (dim-but-legible dark default; now collected
        # by VisibilityPlotter._style_cmap_column for toggle support).
        hist_fig.background_fill_color = "#1e1e2e"
        hist_fig.border_fill_color     = "#1e1e2e"
        hist_fig.xaxis.axis_line_color        = "#45475a"
        hist_fig.xaxis.major_tick_line_color  = "#45475a"
        hist_fig.xaxis.minor_tick_line_color  = "#45475a"
        hist_fig.xaxis.major_label_text_color = "#cdd6f4"
        hist_fig.xgrid.grid_line_color        = "#45475a"
        hist_fig.xgrid.grid_line_alpha        = 0.3
        hist_fig.outline_line_color           = "#45475a"

        # See VisibilityRaster.colormap_controls' identical block for
        # the rationale (line_width also sets Bokeh's pan hit-test
        # tolerance; the previous 2 was floored to a fixed 2.5px zone).
        min_span = EditSpan(
            location=min_loc, dimension="height", editable=True,
            line_color="#f38ba8", line_dash="dashed", line_width=2,
        )
        max_span = EditSpan(
            location=max_loc, dimension="height", editable=True,
            line_color="#f38ba8", line_dash="dashed", line_width=2,
        )
        hist_fig.add_layout(min_span)
        hist_fig.add_layout(max_span)
        # See VisibilityRaster.colormap_controls' identical block for
        # the rationale.
        min_span.sibling = max_span
        max_span.sibling = min_span

        min_input = TextInput(title="min", value=f"{min_loc:.6g}", width=110)
        max_input = TextInput(title="max", value=f"{max_loc:.6g}", width=110)
        # See VisibilityRaster.colormap_controls' identical block for
        # the icon/styling rationale (colors come from a toggle-managed
        # stylesheet prepended by VisibilityPlotter._style_cmap_column,
        # not from here -- only non-color properties belong in this
        # button's own stylesheet).
        reset_button = Button(
            icon=BuiltinIcon(icon_name="reset", size="1.1em", color="#cdd6f4"),
            label="", width=36, height=36, button_type="default",
            stylesheets=[InlineStyleSheet(css="""
                :host(.bk-btn), .bk-btn {
                    padding: 2px;
                }
            """)],
        )
        # See VisibilityRaster.colormap_controls' identical block for
        # the rationale (align="end" alone isn't enough since the
        # button has no label above it, unlike the TextInputs).
        reset_button_col = column(Spacer(height=19), reset_button)

        controls = column(
            scaling_select,
            equation,
            row(alpha_input, gamma_input),
            hist_fig,
            row(reset_button_col, min_input, max_input),
        )

        if self._comm is None:
            # No comm channel -- controls render but are inert, matching
            # VisibilityRaster.colormap_controls' identical convention.
            return controls

        comm               = self._comm
        image_source       = self._image_source
        msg_update_scaling = self._msg_update_scaling
        equations          = {s: _cms.scaling_equation_label(s) for s in _cms.ALL_SCALINGS}

        _apply_image_js = """
    console.log('[visplot colormap] update_scaling response:', resp);
    if (!resp || resp.status !== 'ok') {
        console.warn('[visplot colormap] update_scaling failed or no response:', resp);
        return;
    }
    if (resp.image != null) {
        image_source.data['image'] = [resp.image];
        image_source.data['x']     = [resp.x0];
        image_source.data['y']     = [resp.y0];
        image_source.data['dw']    = [resp.x1 - resp.x0];
        image_source.data['dh']    = [resp.y1 - resp.y0];
        image_source.change.emit();
    } else {
        console.warn('[visplot colormap] response had no image:', resp);
    }
"""

        scaling_js = CustomJS(
            args={
                "comm": comm, "image_source": image_source,
                "equation": equation, "alpha_input": alpha_input,
                "gamma_input": gamma_input, "equations": equations,
                "layer_index": layer_index,
            },
            code=f"""
const s = cb_obj.value;
alpha_input.visible = (s === 'log' || s === 'power');
gamma_input.visible = (s === 'gamma');
equation.text = equations[s] || s;
console.log('[visplot colormap] sending:', {{layer_index: layer_index, scaling: s}});
comm.send('{msg_update_scaling}', {{layer_index: layer_index, scaling: s}}, function(resp) {{
{_apply_image_js}
}});
""",
        )
        scaling_select.js_on_change("value", scaling_js)

        def _numeric_submit_js(field_key: str) -> "CustomJS":
            return CustomJS(
                args={"comm": comm, "image_source": image_source,
                      "layer_index": layer_index},
                code=f"""
const v = parseFloat(cb_obj.value);
if (isNaN(v)) return;
console.log('[visplot colormap] sending:', {{layer_index: layer_index, {field_key}: v}});
comm.send('{msg_update_scaling}', {{layer_index: layer_index, {field_key}: v}}, function(resp) {{
{_apply_image_js}
}});
""",
            )

        alpha_input.js_on_event(ValueSubmit, _numeric_submit_js("alpha"))
        gamma_input.js_on_event(ValueSubmit, _numeric_submit_js("gamma"))

        # --- min/max <-> span bidirectional wiring ---------------------
        def _span_drag_visual_js(paired_input) -> "CustomJS":
            """See VisibilityRaster.colormap_controls' identical function
            for the null-guard rationale (undefined.toFixed() crash on a
            dead first drag attempt)."""
            return CustomJS(
                args={"paired_input": paired_input},
                code="""
if (cb_obj.location == null || isNaN(cb_obj.location)) return;
paired_input.value = cb_obj.location.toFixed(6);
""",
            )

        def _span_release_js(field_key: str, paired_input) -> "CustomJS":
            """See VisibilityRaster.colormap_controls' identical function
            for the full rationale (dragging property replacing the
            LODEnd PlotEvent approach, which never dispatched at all
            from a non-Plot origin)."""
            return CustomJS(
                args={"comm": comm, "image_source": image_source,
                      "layer_index": layer_index, "paired_input": paired_input},
                code=f"""
if (cb_obj.dragging) return;  // only act when the drag just ENDED
const v = cb_obj.location;
if (v == null || isNaN(v)) return;
paired_input.value = v.toFixed(6);
console.log('[visplot colormap] sending (span release):', {{layer_index: layer_index, {field_key}: v}});
comm.send('{msg_update_scaling}', {{layer_index: layer_index, {field_key}: v}}, function(resp) {{
{_apply_image_js}
}});
""",
            )

        min_span.js_on_change("location", _span_drag_visual_js(min_input))
        max_span.js_on_change("location", _span_drag_visual_js(max_input))
        min_span.js_on_change("dragging", _span_release_js("vmin", min_input))
        max_span.js_on_change("dragging", _span_release_js("vmax", max_input))

        def _range_submit_js(field_key: str, paired_span) -> "CustomJS":
            return CustomJS(
                args={"comm": comm, "image_source": image_source,
                      "layer_index": layer_index, "paired_span": paired_span},
                code=f"""
const v = parseFloat(cb_obj.value);
if (isNaN(v)) return;
paired_span.location = v;
console.log('[visplot colormap] sending (field submit):', {{layer_index: layer_index, {field_key}: v}});
comm.send('{msg_update_scaling}', {{layer_index: layer_index, {field_key}: v}}, function(resp) {{
{_apply_image_js}
}});
""",
            )

        min_input.js_on_event(ValueSubmit, _range_submit_js("vmin", min_span))
        max_input.js_on_event(ValueSubmit, _range_submit_js("vmax", max_span))

        reset_js = CustomJS(
            args={"comm": comm, "image_source": image_source,
                  "layer_index": layer_index,
                  "min_span": min_span, "max_span": max_span,
                  "min_input": min_input, "max_input": max_input,
                  "hist_lo": hist_lo, "hist_hi": hist_hi},
            code=f"""
min_span.location = hist_lo;
max_span.location = hist_hi;
min_input.value = hist_lo.toFixed(6);
max_input.value = hist_hi.toFixed(6);
console.log('[visplot colormap] sending (reset):', {{layer_index: layer_index, reset_range: true}});
comm.send('{msg_update_scaling}', {{layer_index: layer_index, reset_range: true}}, function(resp) {{
{_apply_image_js}
}});
""",
        )
        reset_button.js_on_click(reset_js)

        return controls

    def update_axes(
        self,
        x_dim: Optional["Axis"] = None,
        layers: Optional[list[ScatterLayer]] = None,
        title: Optional[str] = None,
    ) -> None:
        """Change the x-axis or layer list and re-render.

        Parameters
        ----------
        x_dim : Axis | None
            New x-axis.  ``None`` keeps the current value.
        layers : list[ScatterLayer] | None
            Replacement layer list.  ``None`` keeps existing layers.
        title : str | None
            New figure title.
        """
        changed = False
        if x_dim is not None and x_dim != self._x_dim:
            self._x_dim = x_dim;  changed = True
        if layers is not None:
            # _with_default_cmaps, not list(): a j2p layers payload
            # carries no cmap field, and an unfilled None reaches
            # tf.shade and blanks the panel.
            self._layers = self._with_default_cmaps(layers)
            n = len(layers)
            self._layer_images      = [None] * n
            self._layer_n_in_view   = [0] * n
            self._layer_peak        = [None] * n
            self._layer_hist_counts = [None] * n
            self._layer_hist_edges  = [None] * n
            self._layer_mapping     = [None] * n
            self._layer_skip_reason = [None] * n
            self._layer_id_grid     = [None] * n
            self._layer_categories       = [None] * n
            self._layer_category_colors  = [None] * n
            self._layer_category_members = [None] * n
            self._layer_dfs         = [None] * n   # vestigial -- see __init__
            self._layer_aggs        = [None] * n   # vestigial -- see __init__
            self._layer_extents     = [None] * n   # vestigial -- see __init__
            # Layer list replaced wholesale -- any cached continuous-cmap
            # backup is keyed by an index that may now name a different
            # layer entirely (or not exist), so drop it rather than let
            # a future update_colorize() restore the wrong ramp.
            self._layer_continuous_cmap_backup = {}
            changed = True
        if title is not None:
            self._title = title;  changed = True

        # A never-yet-rendered (defer_initial_render=True) panel must
        # render on its first update_axes() call regardless of what else
        # changed — same rationale as VisibilityRaster's identical guard.
        # Scatter has no single self._agg; "never rendered" here means
        # every layer's cached image is still None (set that way by
        # _render(defer=True), and by the layers-replacement branch above,
        # which is why this check must come after it).
        if all(img is None for img in self._layer_images):
            changed = True

        if not changed:
            return

        self._render(self._selection)
        self._notify_axes_changed()

    # ------------------------------------------------------------------
    # VisibilityPlot abstract interface
    # ------------------------------------------------------------------

    def _comm_description(self) -> str:
        return "visibility scatter"

    def _effective_title(self) -> str:
        if self._title:
            return self._title
        labels = ", ".join(lyr.label for lyr in self._layers)
        # _x_info.label, not _x_dim.label: the title must name the axis
        # that was actually plotted, for the same reason the axis label
        # must.  Bare name, no unit suffix -- the axis label carries that.
        return f"{labels}  vs  {self._x_info.label}"

    def _panel_spec(self) -> PanelSpec:
        """Describe this scatter: one colour band per ScatterLayer.

        One band per layer regardless of visibility, so band indices stay
        stable — the same invariant the probe envelope's ``layers`` list
        keeps, and for the same reason: a consumer indexing by position
        must not have entries shift under it when the user hides a layer.

        ``status`` is ``"empty"`` when no layer has data to draw.  The
        per-layer ``_layer_skip_reason`` strings are joined into ``note``
        because "never queried", "query returned 0 rows" and "0 samples
        in this viewport" are genuinely different events and only the
        last is routine — an exported cell that says which one applies is
        far more useful than a blank square.
        """
        x_is_time, y_is_time = self._axis_flags()

        bands = tuple(
            ColorBand(
                label         = lyr.label,
                cmap          = tuple(lyr.cmap or ()),
                scaling       = lyr.scaling,
                scaling_alpha = lyr.scaling_alpha,
                scaling_gamma = lyr.scaling_gamma,
                vmin          = lyr.scaling_vmin,
                vmax          = lyr.scaling_vmax,
                alpha         = lyr.alpha,
                visible       = lyr.alpha > 0.0,
                # Part 4: categories/colors/members are cheap cached
                # reads (unlike mapping/peak_density below, which cost a
                # histogram + interpolation and are only attached late,
                # at export time, by _bands_with_mappings) -- set here
                # directly rather than waiting for that step. kind is
                # set here too, not just for export: it is what
                # colorize_controls()/_legend_html() would need to
                # branch on if they read ColorBand instead of the raw
                # per-layer caches (they don't today, but PanelSpec is
                # meant to be the one seam both paths agree on -- see
                # this module's own docstring).
                kind = ("categorical" if lyr.coloring == "categorical"
                        else "value"),
                categories       = self._layer_categories[i]
                    if i < len(self._layer_categories) else None,
                category_colors  = self._layer_category_colors[i]
                    if i < len(self._layer_category_colors) else None,
                category_members = self._layer_category_members[i]
                    if i < len(self._layer_category_members) else None,
            )
            for i, lyr in enumerate(self._layers)
        )

        # agg_n_x/agg_n_y: canvas resolution at the *full* data extent --
        # same field names as VisibilityRaster so FlagTool's existing
        # zoom-to-1:1 math (flag_tool.ts) works unchanged here too. Unlike
        # raster, scatter points are exact (not binned/decimated), so this
        # isn't about resolving averaged data -- it's the same
        # sparse-data canvas-shrink logic the backend's
        # compute_canvas_size applies (see data/_scatter_render.py),
        # which means "1:1" for scatter effectively means "zoomed in
        # enough that the full-extent view's overplot-driven canvas
        # shrink no longer applies" -- a reasonable proxy for "not
        # looking at an overplotted, ambiguous cluster."
        #
        # POST-2026-09: no longer recomputed here -- the backend
        # computes it as part of every full-extent query_columns()
        # call, and _render_all_layers caches it in
        # _full_canvas_width/height specifically (as opposed to
        # _canvas_width/height, which tracks whatever the *last* call
        # used, full-extent or viewport).
        full_x0, full_x1 = self._x_range
        full_y0, full_y1 = self._y_range
        agg_n_x, agg_n_y = self._full_canvas_width, self._full_canvas_height

        drawable = any(
            img is not None and self._effective_skip_reason(i) is None
            for i, img in enumerate(self._layer_images)
        )
        status, note = "ok", None
        if not drawable:
            status = "empty"
            reasons = [
                f"{lyr.label}: {self._effective_skip_reason(i)}"
                for i, lyr in enumerate(self._layers)
                if self._effective_skip_reason(i)
            ]
            note = "; ".join(reasons) if reasons else "no layers with data"

        return PanelSpec(
            kind       = "scatter",
            title      = self._effective_title(),
            x_label    = self.x_label,
            y_label    = self.y_label,
            x_range    = (float(full_x0), float(full_x1)),
            y_range    = (float(full_y0), float(full_y1)),
            x_is_time  = x_is_time,
            y_is_time  = y_is_time,
            agg_n_x    = agg_n_x,
            agg_n_y    = agg_n_y,
            color_mode = self._color_mode,
            bands      = bands,
            status     = status,
            note       = note,
            theme      = self._theme_hint(),
            x_unit     = self._x_info.unit,
            y_unit     = self._y_info.unit,
        )

    def _bands_with_mappings(self, spec, viewport=None):
        """One mapping per layer, plus each layer's peak per-pixel value.

        These bands are ``kind="density"`` -- which is what stops the
        compositor labelling the ramp with the layer's amplitude label.
        ``peak_density`` feeds the legend annotation that scatter panels
        show instead of a colorbar.

        POST-2026-09: ``mapping``/``peak`` now come straight from the
        backend's last render (``ScatterLayerRender.mapping_x``/
        ``mapping_u``/``peak_value`` in ``data/reader.py``), computed
        server-side against the true per-sample reference population,
        rather than being rebuilt here from a locally-cached agg (which
        no longer exists client-side) -- see that docstring for why. A
        layer with no cached mapping (never rendered, or currently
        skipped) keeps ``mapping=None``, which the compositor reads as
        "no bar for this band" -- unchanged.

        Part 4: a ``kind="categorical"`` band is passed through
        untouched -- it never has a ``mapping``/``peak_density`` (no
        ramp, no colorbar; see ``ColorBand``'s docstring), and forcing
        ``kind="density"`` on it the way every other band gets here
        would make ``png_export.py`` draw it as a (nonexistent) density
        ramp instead of the category legend its ``categories``/
        ``category_colors`` already carry.
        """
        from dataclasses import replace

        out = []
        for i, band in enumerate(spec.bands):
            if band.kind == "categorical":
                out.append(band)
                continue
            mapping = (self._layer_mapping[i]
                       if i < len(self._layer_mapping) else None)
            if mapping is None or not band.visible:
                out.append(replace(band, kind="density"))
                continue
            peak = self._layer_peak[i] if i < len(self._layer_peak) else None
            out.append(replace(band, kind="density", mapping=mapping,
                               peak_density=peak))
        return tuple(out)

    def _shade_for_export(self, viewport=None) -> Optional[np.ndarray]:
        """Render a composite at *viewport* without disturbing the live
        widget's cached render state.

        POST-2026-09: rendering is now a backend round trip (see
        ``_rerender``), so this can no longer "peek" at a locally-cached
        agg the way the pre-redesign version did -- it issues its own
        backend query at the requested viewport and restores the
        previous cached render state afterward, so an export at some
        other viewport doesn't alter what's on screen (or what
        ``_panel_spec``/``colormap_controls()`` would report next).
        Snapshot and restore rather than relying on the export viewport
        happening to match the live one.
        """
        saved = (
            self._layer_images, self._layer_n_in_view, self._layer_peak,
            self._layer_hist_counts, self._layer_hist_edges,
            self._layer_mapping, self._layer_skip_reason,
            self._canvas_width, self._canvas_height,
            # Part 4: these three must ride along with everything else
            # here -- an export at a different viewport still calls
            # _render_all_layers(), which reassigns them same as the
            # live-state fields above. Omitting them would leave the
            # live widget's legend/category state silently overwritten
            # by whatever the export viewport happened to render.
            self._layer_categories, self._layer_category_colors,
            self._layer_category_members,
        )
        try:
            if viewport is None:
                xr, yr = self._current_render_range()
            else:
                x0, x1, y0, y1 = viewport
                xr, yr = (x0, x1), (y0, y1)
            self._render_all_layers(self._selection, x_range=xr, y_range=yr)
            return self._collapse_and_composite()
        finally:
            (self._layer_images, self._layer_n_in_view, self._layer_peak,
             self._layer_hist_counts, self._layer_hist_edges,
             self._layer_mapping, self._layer_skip_reason,
             self._canvas_width, self._canvas_height,
             self._layer_categories, self._layer_category_colors,
             self._layer_category_members) = saved

    def _build_glyphs(self) -> None:
        """Add the single composite image_rgba glyph."""
        self._fig.image_rgba(
            source = self._image_source,
            image  = "image",
            x = "x", y = "y", dw = "dw", dh = "dh",
        )

    def _build(self) -> None:
        """Extend the base build with the scatter-only InfoTool.

        Overridden (rather than adding a hook to ``VisibilityPlot``)
        specifically to keep this feature isolated to ``VisibilityScatter``
        -- ``VisibilityRaster``'s ``_build()`` is untouched, and nothing
        about the shared base class changes. See ``_add_info_tool``.
        """
        super()._build()
        if self._headless or self._fig is None:
            return
        self._add_info_tool()

    def _add_info_tool(self) -> None:
        """Add the "i" InfoTool (hover-probe redesign piece 3,
        click-to-exact) to this figure only.

        Gated on ``enable_info_tool`` (default ``True``) and on a
        ``CommMgr`` being available at all -- same guard shape
        ``VisibilityPlot._add_flag_tools`` uses, but deliberately
        independent of ``enable_flagging``: a quick-look session with
        flagging disabled may still want exact metadata lookup, and
        conversely a flagging session may not want the extra toolbar
        button -- the two are unrelated concerns that happen to both be
        drag tools.

        Uses its own dedicated Comm (``squash_queue=False``), the same
        reasoning as ``VisibilityPlot``'s ``_flag_comm``: this now
        triggers a real, sometimes-slow backend round trip (unlike the
        squash-queue-appropriate high-frequency hover traffic on
        ``self._comm``), and a rapid second click should never silently
        squash an in-flight first one before Python has answered it.
        """
        if not self._enable_info_tool or self._comm_mgr is None:
            return
        try:
            self._info_comm = self._comm_mgr.open(
                description=f"{self._comm_description()} info probe",
                squash_queue=False,
            )
        except Exception as exc:
            log.warning("%s: could not open info-probe Comm: %s",
                        type(self).__name__, exc)
            self._info_comm = None
            return
        self._info_comm.register(self._msg_probe_region,
                                  self._handle_probe_region)
        self._info_tool = InfoTool(
            comm=self._info_comm,
            msg_id=self._msg_probe_region,
        )
        self._fig.add_tools(self._info_tool)

    def _render(self, selection: "SelectionSpec", defer: bool = False, **kwargs) -> None:
        """Query+bin+shade all layers via the backend and push the composite.

        Parameters
        ----------
        defer : bool
            If ``True``, skip the backend query entirely and leave all
            layers empty with the same placeholder ``(0.0, 1.0)`` ranges
            used for genuinely empty data — same purpose and pattern as
            ``VisibilityRaster._render``'s ``defer``; see decision 11 in
            the grid/iteration design notes.
        """
        # Re-resolve axis labels before anything reads them: this is the
        # one place that knows both the current axes and the current
        # selection, and every axis- or selection-changing path funnels
        # through it.  Resolving here rather than in _panel_spec()
        # matters -- the backend counts partitions to decide whether
        # Axis.CHANNEL is unique, and _panel_spec() runs on every push.
        self._refresh_axis_info(selection)
        t0 = time.perf_counter()
        self._current_viewport = None   # reset — new data covers full range
        if defer:
            n = len(self._layers)
            self._layer_images      = [None] * n
            self._layer_n_in_view   = [0] * n
            self._layer_peak        = [None] * n
            self._layer_hist_counts = [None] * n
            self._layer_hist_edges  = [None] * n
            self._layer_mapping     = [None] * n
            self._layer_skip_reason = ["deferred (never rendered)"] * n
            self._layer_id_grid     = [None] * n
            self._layer_categories       = [None] * n
            self._layer_category_colors  = [None] * n
            self._layer_category_members = [None] * n
            self._layer_dfs         = [None] * n   # vestigial -- see __init__
            self._layer_aggs        = [None] * n   # vestigial -- see __init__
            self._layer_extents     = [None] * n   # vestigial -- see __init__
            self._x_range    = (0.0, 1.0)
            self._y_range    = (0.0, 1.0)
            self._canvas_width, self._canvas_height = self._width, self._height
            self._full_canvas_width  = self._width
            self._full_canvas_height = self._height
            img32 = np.zeros((self._height, self._width), dtype=np.uint32)
            self._push_image(img32, self._x_range, self._y_range)
        else:
            self._rerender(x_range=None, y_range=None)
        log.debug("VisibilityScatter._render: %.3fs", time.perf_counter() - t0)
        self._update_state_source()

    def _do_viewport_rerender(
        self, x0: float, x1: float, y0: float, y1: float
    ) -> dict:
        """Re-render at the new viewport via a fresh backend call.

        POST-2026-09: no longer a local recomposite of a cached
        DataFrame -- binning and shading both happen backend-side now
        (see ``ScatterRenderResult``'s docstring in ``data/reader.py``),
        so every pan/zoom now costs a full ``query_columns()`` round
        trip. This applies to LOCAL sessions too, not just remote ones:
        the backend re-reads the selected data from disk on every call:
        nothing is cached between calls the way the widget-side
        DataFrame used to be. A known, deliberate cost for this pass —
        see the scatter remote-execution design notes' discussion of
        debouncing / a stale-while-revalidate placeholder / an
        overscan margin as possible later mitigations, none implemented
        yet.
        """
        # Normalise — Bokeh box-zoom can produce start > end
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        self._current_viewport = (x0, x1, y0, y1)
        img32 = self._rerender(x_range=(x0, x1), y_range=(y0, y1))
        return {
            "image": img32,
            "x0": x0, "x1": x1,
            "y0": y0, "y1": y1,
        }

    # ------------------------------------------------------------------
    # Hover probe
    # ------------------------------------------------------------------
    #
    # DEFECT HISTORY (2026-08 probe-miss investigation)
    # -------------------------------------------------
    # The previous implementation had three independent defects that all
    # produced the same user-visible symptom: hovering a point that is
    # clearly painted in the composite image reports "<i>empty</i>",
    # while a neighbouring point in the same zoomed region reports a
    # value.
    #
    #   (1) SINGLE-LAYER PROBE.  It computed (px, py) from the *first*
    #       non-None layer agg, then looped over layers and returned on
    #       the first call that did not *raise* -- which is always layer
    #       0.  Layers 1..N-1 were therefore never consulted.  But the
    #       displayed image is a Porter-Duff composite of *every* layer
    #       (see _shade_all_layers), so any pixel painted only by, say,
    #       the YY layer reads back as NaN from the XX agg.  With two
    #       polarizations overlaid this alternates hit/miss point by
    #       point with no spatial pattern -- exactly the reported
    #       symptom.  Note the old loop returned even when
    #       info["value"] is None, so "found a layer" and "found data"
    #       were conflated.
    #
    #   (2) STALE AGGS.  _shade_all_layers only ever *assigns*
    #       self._layer_aggs[i]; it never clears it.  A layer skipped on
    #       the current pass (alpha == 0, zero points in view, shade
    #       exception, or the whole-canvas degenerate early return) kept
    #       its agg from a *previous viewport*, with different bin
    #       centres and possibly a different shape.  Indexing that with
    #       (px, py) derived from the current viewport silently returns
    #       a value from the wrong place, or raises IndexError.  Fixed
    #       at the top of _shade_all_layers; also defended here by
    #       deriving (px, py) per layer from that layer's own coords.
    #
    #   (3) NO SLOP.  cvs.points() marks exactly one bin per sample,
    #       but at deep zoom _compute_canvas_size shrinks the canvas so
    #       each bin is drawn as a large block, and the hover geometry
    #       is continuous.  A hover landing one bin off the mark reads
    #       NaN even though the user is visually on the point.  Now
    #       handled by _nearest_populated_bin with a configurable
    #       search radius.
    #
    # The probe now examines every visible layer, prefers an exact-bin
    # hit over a near hit and a near hit over nothing, and reports which
    # layer answered (via the layer's own label) plus how far off the
    # matched bin was.

    def _agg_pixel(
        self, agg: "xr.DataArray", x: float, y: float
    ) -> Optional[tuple[int, int]]:
        """Map data-space (x, y) to (px, py) in *this* agg's own coords.

        Derived per layer rather than once from a representative agg:
        layers are normally shaded at a shared canvas size against a
        shared viewport, but a layer whose agg is stale (see defect 2
        above) will have different coords, and using another layer's
        indices against it produces silent misattribution.
        """
        try:
            x_coords = agg.coords[agg.dims[1]].values
            y_coords = agg.coords[agg.dims[0]].values
        except (KeyError, IndexError):
            return None
        if x_coords.size == 0 or y_coords.size == 0:
            return None
        h, w = agg.shape
        px = max(0, min(int(np.argmin(np.abs(x_coords - x))), w - 1))
        py = max(0, min(int(np.argmin(np.abs(y_coords - y))), h - 1))
        return px, py

    # VESTIGIAL as of the 2026-09 hover-probe redesign's second pass --
    # _populated_mask/_bin_screen_size/_search_radius_bins/
    # _nearest_populated_bin (the next four methods) are no longer
    # called by _handle_probe, which now does exact-coarse-cell lookup
    # only -- see that method's docstring for why the neighbor-search
    # tolerance these implement doesn't belong on a coarse grid. Kept
    # rather than deleted on the chance something else (tests, a future
    # caller) still references them; genuinely dead from
    # _handle_probe's own perspective.
    @staticmethod
    def _populated_mask(values: np.ndarray) -> np.ndarray:
        """Boolean mask of bins that actually contain samples.

        ``ds_agg.mean()`` (what _shade_all_layers uses) leaves empty
        bins NaN, but ``count()``/``any()`` aggs use 0/False instead.
        Testing for emptiness with np.isnan alone silently treats every
        bin of an integer agg as populated, so branch on dtype.
        """
        if np.issubdtype(values.dtype, np.floating):
            return np.isfinite(values)
        if np.issubdtype(values.dtype, np.bool_):
            return values
        return values != 0

    def _bin_screen_size(self, agg: "xr.DataArray") -> tuple[float, float]:
        """Size of one agg bin in *screen* pixels, (width, height).

        The adaptive canvas (_compute_canvas_size) can shrink the agg to
        a small fraction of the figure, in which case the image glyph
        stretches each bin over many screen pixels -- on sparse zoomed
        data a bin is routinely 30-40 px across, i.e. the drawn mark IS
        one bin.  Everything the probe expresses in bins therefore has
        to be converted before it means anything to the user.
        """
        h, w = agg.shape
        if w <= 0 or h <= 0:
            return 1.0, 1.0
        return self._width / float(w), self._height / float(h)

    def _search_radius_bins(self, agg: "xr.DataArray") -> int:
        """Search radius in bins that yields ``_probe_slop_px`` of slop.

        A radius fixed in *bins* is the wrong unit: the same value gives
        3x3 screen pixels of tolerance on a full-resolution canvas and
        over 100x67 on a canvas the adaptive shrink has taken down to
        25x27.  It is most forgiving exactly where the marks are already
        biggest and least forgiving where they are single pixels.

        Converting from a screen-pixel budget inverts that.  When a bin
        is already larger than the budget -- the shrunken-canvas case,
        where the drawn mark and the bin coincide -- this returns 0, so
        an exact-bin lookup covers the whole visible mark and nothing
        beyond it.
        """
        if self._probe_search_radius is not None:
            return int(self._probe_search_radius)
        bin_w, bin_h = self._bin_screen_size(agg)
        smallest = min(bin_w, bin_h)
        if smallest >= self._probe_slop_px:
            return 0
        return int(math.ceil(self._probe_slop_px / smallest))

    def _nearest_populated_bin(
        self, agg: "xr.DataArray", px: int, py: int, radius: int,
        bin_w: float = 1.0, bin_h: float = 1.0,
    ) -> Optional[tuple[float, int, int]]:
        """Nearest populated bin to (px, py) within *radius* bins.

        Returns ``(distance_in_screen_px, px, py)`` -- distance 0.0 when
        the hovered bin itself is populated -- or ``None`` when the whole
        search window is empty.

        Distances are weighted by ``bin_w``/``bin_h`` so "nearest" means
        nearest *on screen*.  Agg bins are not square in screen space
        (36x22 px in the case that prompted this), so an unweighted
        hypot in bin units can prefer a bin the user's eye reads as
        farther away.  It is also what makes the reported distance
        honest: bins are the wrong unit to show in a status bar.

        A single pass over the (2r+1)² window finds the true nearest
        within it; ring-by-ring expansion would not (a hit at Chebyshev
        radius r can be farther in Euclidean terms than one at r+1).
        """
        values = agg.values
        h, w   = values.shape
        if not (0 <= px < w and 0 <= py < h):
            return None
        mask = self._populated_mask(values)
        if mask[py, px]:
            return 0.0, px, py
        if radius <= 0:
            return None
        y0, y1 = max(0, py - radius), min(h, py + radius + 1)
        x0, x1 = max(0, px - radius), min(w, px + radius + 1)
        window = mask[y0:y1, x0:x1]
        if not window.any():
            return None
        ys, xs = np.nonzero(window)
        dist   = np.hypot(((ys + y0) - py) * bin_h, ((xs + x0) - px) * bin_w)
        k      = int(np.argmin(dist))
        return float(dist[k]), int(xs[k] + x0), int(ys[k] + y0)

    def _handle_probe(self, message: dict) -> dict:
        """Probe every visible layer at the hover coordinates.

        POST-2026-09 (hover-probe redesign piece 2): resolved entirely
        locally now, against each layer's cached coarse id grid (see
        ``_layer_id_grid``, ``ScatterLayerRender.id_grid_*`` in
        ``data/reader.py``) -- no backend call. Identity comes from
        ``VisibilityPlot._match_identity`` (shared with
        ``VisibilityRaster``; ``polarization=None`` here rather than a
        specific layer's, since this hover can win on any layer and the
        coarse grid's whole point is an approximate range anyway --
        exact, polarization-scoped identity is what click-to-exact,
        piece 3, is for).

        CORRECTION (2026-09, second pass): the pre-redesign "search a
        few screen pixels further for a barely-missed point" tolerance
        (``_search_radius_bins``/``_nearest_populated_bin``, inherited
        unchanged from the full-resolution design) is NOT used here
        anymore -- exact-cell lookup only. That tolerance was
        calibrated for a full-resolution canvas where each bin is one
        or a few screen pixels and a single-pixel mark could be barely
        missed; it does not belong on a DELIBERATELY coarse grid, where
        each cell already covers a wide screen area on its own. Worse,
        it actively misled: confirmed in practice (a real hover
        reporting data ~250-300 screen pixels away, from a visually
        unrelated region of the plot, with no indication of direction
        or distance meaning to the user) that stacking "search
        neighbors" on top of an already-coarse grid can span an
        unbounded, confusing distance rather than a small forgiveness
        margin. An empty coarse cell now means exactly what it looks
        like: no data reported for that hover, full stop -- matching
        what a coarse grid can honestly promise.

        Reported value/identity are still coarse -- see
        ``ScatterLayerRender.id_grid_*``'s docstring for that
        (unrelated, intentional) trade-off. ``n_samples`` is not
        available from the coarse grid (no count reduction computed
        there) and reports ``None`` here; that's a known, deliberate
        gap piece 3 fills, not an oversight.
        """
        x = float(message.get("x", 0.0))
        y = float(message.get("y", 0.0))

        # Range-check against what is actually on screen.  See
        # pre-redesign comment (unchanged): checking the full data
        # extent instead would let an out-of-view hover clamp onto an
        # edge bin and report it as though it were under the cursor.
        if self._current_viewport is not None:
            vx0, vx1, vy0, vy1 = self._current_viewport
        else:
            vx0, vx1 = self._x_range
            vy0, vy1 = self._y_range
        if not (min(vx0, vx1) <= x <= max(vx0, vx1) and
                min(vy0, vy1) <= y <= max(vy0, vy1)):
            return self._probe_envelope(
                "out_of_range", "<i>out of range</i>", x=x, y=y, layers=[],
            )

        # Gather a candidate from every layer that actually has data in
        # the exact coarse cell under the cursor -- no neighbor search,
        # see this method's docstring for why.
        candidates: list[tuple[int, int, int]] = []   # (layer_index, px, py)
        debug_rows: list[str] = []

        for i, (id_grid, lyr) in enumerate(zip(self._layer_id_grid, self._layers)):
            if id_grid is None:
                reason = (self._layer_skip_reason[i]
                          if i < len(self._layer_skip_reason) else None)
                debug_rows.append(f"L{i}[{lyr.label}]:skip({reason or 'no id grid'})")
                continue
            h, w = id_grid["value"].shape
            gx0, gx1 = id_grid["x_range"]
            gy0, gy1 = id_grid["y_range"]
            # A lightweight xr.DataArray wrapper -- lets this reuse
            # _agg_pixel unchanged (it only ever touches
            # .shape/.values/.coords[.dims[...]]) rather than
            # duplicating that mapping for a plain-array + explicit-
            # range representation. Regularly spaced by construction
            # (the coarse grid is a uniform Canvas.points() binning),
            # unlike raster's raw_grid coordinates -- linspace is exact
            # here, not an approximation.
            agg = xr.DataArray(
                id_grid["value"], dims=("y", "x"),
                coords={"x": np.linspace(gx0, gx1, w),
                        "y": np.linspace(gy0, gy1, h)},
            )
            idx = self._agg_pixel(agg, x, y)
            if idx is None:
                debug_rows.append(f"L{i}:skip(no coords)")
                continue
            px, py = idx
            if np.isfinite(id_grid["value"][py, px]):
                candidates.append((i, px, py))
                debug_rows.append(f"L{i}[{lyr.label}]:hit@({px},{py}) shape={agg.shape}")
            else:
                debug_rows.append(f"L{i}[{lyr.label}]:empty@({px},{py}) shape={agg.shape}")

        if self._probe_debug:
            log.info(
                "[probe] x=%.9g y=%.9g viewport=(%.6g,%.6g,%.6g,%.6g) | %s",
                x, y, vx0, vx1, vy0, vy1, "  ".join(debug_rows),
            )

        if not candidates:
            return self._probe_envelope(
                "no_data", "<i>no data</i>", x=x, y=y, layers=[],
            )

        # Prefer the lowest layer index so the answer is stable as the
        # cursor moves across a region where multiple layers overlap.
        layer_i, px, py = min(candidates, key=lambda c: c[0])

        lyr = self._layers[layer_i]
        id_grid = self._layer_id_grid[layer_i]

        def _cell_range(lo_key: str, hi_key: str) -> Optional[tuple[float, float]]:
            if lo_key not in id_grid:
                return None
            lo, hi = id_grid[lo_key][py, px], id_grid[hi_key][py, px]
            if not (np.isfinite(lo) and np.isfinite(hi)):
                return None
            return float(lo), float(hi)

        t_range    = _cell_range("t_lo", "t_hi")
        bl_range   = _cell_range("bl_lo", "bl_hi")
        freq_range = _cell_range("f_lo", "f_hi")

        identity = self._match_identity(
            t_range=t_range, bl_range=bl_range, freq_range=freq_range,
            polarization=None,
        )

        raw_value = float(id_grid["value"][py, px])
        value = _json_num(raw_value) if np.isfinite(raw_value) else None

        # Cell-space centre/bounds, for the status bar and _flag_key --
        # exact (not approximate) since the coarse grid is uniformly
        # spaced by construction; only the CONTENTS of each cell
        # (identity, value) are coarse, not its geometry.
        cell_w, cell_h = (
            (id_grid["x_range"][1] - id_grid["x_range"][0]) / id_grid["value"].shape[1],
            (id_grid["y_range"][1] - id_grid["y_range"][0]) / id_grid["value"].shape[0],
        )
        cell_x_range = (id_grid["x_range"][0] + px * cell_w,
                        id_grid["x_range"][0] + (px + 1) * cell_w)
        cell_y_range = (id_grid["y_range"][0] + py * cell_h,
                         id_grid["y_range"][0] + (py + 1) * cell_h)
        x_centre = (cell_x_range[0] + cell_x_range[1]) / 2.0
        y_centre = (cell_y_range[0] + cell_y_range[1]) / 2.0

        info = {
            "value": raw_value if np.isfinite(raw_value) else None,
            "x_range": cell_x_range, "y_range": cell_y_range,
            **identity,
        }

        # Report *every* layer, not just the one that answered -- see
        # the pre-redesign comment (unchanged rationale): a status bar
        # showing only the winning layer cannot distinguish "XX has no
        # data here" from "you weren't told about XX". Other layers'
        # values are read straight from their own id grids at the same
        # hovered coarse pixel -- all layers share the same grid shape
        # (same probe_grid_max_cells, same viewport), so (px,py) from
        # one layer's lookup is a valid index into another's.
        hits = {i: (hpx, hpy) for i, hpx, hpy in candidates}
        layer_results: list[dict] = []
        for i, other in enumerate(self._layers):
            entry = {
                "index":       i,
                "label":       other.label,
                "visible":     other.alpha > 0.0,
                "value":       None,
                "skip_reason": (self._layer_skip_reason[i]
                                if i < len(self._layer_skip_reason) else None),
            }
            hit = hits.get(i)
            other_grid = self._layer_id_grid[i]
            if entry["visible"] and hit is not None and other_grid is not None:
                hx, hy = hit
                try:
                    val = _json_num(float(other_grid["value"][hy, hx]))
                except (IndexError, TypeError, ValueError):
                    val = None
                if val is not None:
                    entry["value"] = val
            layer_results.append(entry)

        readings = [
            self._layer_reading_html(e) for e in layer_results if e["visible"]
        ]

        _, _, remainder = self._format_probe(
            info, lyr.label
        ).partition(self._PROBE_SEP)

        parts = readings + ([remainder] if remainder else [])
        label = self._PROBE_SEP.join(parts)

        return self._probe_envelope(
            "ok", label,
            x = x, y = y,
            winner      = int(layer_i),
            layers      = layer_results,
            n_samples   = None,   # see this method's docstring
            x_centre    = _json_num(x_centre),
            y_centre    = _json_num(y_centre),
            flag_key    = self._flag_key(info) if identity else {},
        )

    # ------------------------------------------------------------------
    # Hover-probe redesign piece 3 (2026-09): click-to-exact
    # ------------------------------------------------------------------

    async def _handle_probe_region(self, message: dict) -> dict:
        """Handle an InfoTool click or drag: exact identity for a rectangle.

        Unlike ``_handle_probe`` (resolved entirely from the cached
        coarse ``_layer_id_grid``, no backend call), this always makes a
        real ``self._backend.probe_scatter_region(...)`` round trip --
        see that method's docstring for why click-to-exact fundamentally
        can't be answered locally the way raster's hover can (piece 1):
        there is no cached per-sample data client-side to consult: that
        cache is exactly what the "coarse but free" redesign removed.

        Message contract (from ``InfoTool``/``info_tool.ts``)
        -------------------------------------------------------
        Click:  ``{"tool": "info_click", "x": float, "y": float}``
        Drag:   ``{"tool": "info_box", "x0", "x1", "y0", "y1": float}``

        Returns
        -------
        dict with a single ``"info_html"`` key -- a complete, standalone
        HTML document (title, minimal inline styling, one section per
        visible layer) -- plus ``"status"``. ``info_tool.ts`` writes
        ``info_html`` verbatim into a brand-new browser tab it opens for
        every click/drag (see that file for why the tab is opened
        synchronously at click/drag-release time rather than from this
        method's (async) response, and for why it's a new tab each time
        rather than one reused tab -- multiple open tabs are meant to
        let several selections be compared side by side). Each page's
        title includes the clicked rectangle so a pile of tabs stays
        identifiable.
        """
        x_range = y_range = None
        try:
            tool = message.get("tool")
            if tool == "info_box":
                x_range = (float(message["x0"]), float(message["x1"]))
                y_range = (float(message["y0"]), float(message["y1"]))
            else:
                x = float(message.get("x", 0.0))
                y = float(message.get("y", 0.0))
                x_range, y_range = self._click_window(x, y)
                if x_range is None:
                    return {
                        "status": "out_of_range",
                        "info_html": self._probe_region_page(
                            f"Out of range ({x:.4g}, {y:.4g})",
                            "<p>That point is outside the current view.</p>",
                        ),
                    }

            visible = [lyr for lyr in self._layers if lyr.alpha > 0.0]
            if not visible:
                return {
                    "status": "no_layers",
                    "info_html": self._probe_region_page(
                        f"No visible layers "
                        f"{self._rect_title(x_range, y_range)}",
                        "<p>Every layer is currently hidden "
                        "(alpha = 0).</p>",
                    ),
                }

            yaxes = [(lyr.y_axis, lyr.polarization) for lyr in visible]
            # Async + to_thread + this instance's own _render_lock
            # (2026-09, same treatment as the render handlers below):
            # probe_scatter_region() is a real backend round trip (see
            # this method's own docstring), long enough to starve this
            # connection's ping/pong if run inline. The lock matters
            # here for a second reason too, not just liveness: this
            # reads self._x_dim/self._selection/self._layers, the same
            # instance state a concurrent colorize/scaling/axis-change
            # handler mutates -- without it, a probe could see a
            # torn mix of pre- and post-change state.
            async with self._render_lock:
                results = await asyncio.to_thread(
                    self._backend.probe_scatter_region,
                    self._x_dim, yaxes, self._selection,
                    x_range, y_range,
                    max_samples=self._probe_region_max_samples,
                )

            sections = [
                self._probe_region_layer_html(lyr, results.get(
                    (lyr.y_axis, lyr.polarization),
                    {"status": "no_data", "n_samples": 0},
                ))
                for lyr in visible
            ]
            body = (
                f"<p class='cv-rect'><b>x:</b> "
                f"{x_range[0]:.6g}&ndash;{x_range[1]:.6g} &nbsp; "
                f"<b>y:</b> {y_range[0]:.6g}&ndash;{y_range[1]:.6g}</p>"
                + "".join(sections)
            )
            return {
                "status": "ok",
                "info_html": self._probe_region_page(
                    f"Exact identity {self._rect_title(x_range, y_range)}",
                    body,
                ),
            }
        except Exception as exc:
            log.warning("_handle_probe_region failed: %s", exc, exc_info=True)
            return {
                "status": "error",
                "info_html": self._probe_region_page(
                    f"Error {self._rect_title(x_range, y_range)}",
                    f"<p>Could not resolve exact identity: "
                    f"{_html_escape(str(exc))}</p>",
                ),
            }

    @staticmethod
    def _rect_title(
        x_range: Optional[tuple[float, float]],
        y_range: Optional[tuple[float, float]],
    ) -> str:
        """Short ``"(x0-x1, y0-y1)"`` tag for a tab title.

        Since ``InfoTool`` now opens a brand-new tab per click/drag
        rather than reusing one (multiple open tabs are meant to be
        compared side by side), each tab's title needs something to
        tell it apart from the others in the browser's tab strip --
        this is that something. Returns ``""`` (nothing appended) if
        either range is unavailable, e.g. an error raised before the
        rectangle was computed.
        """
        if x_range is None or y_range is None:
            return ""
        return (f"({x_range[0]:.4g}\u2013{x_range[1]:.4g}, "
                f"{y_range[0]:.4g}\u2013{y_range[1]:.4g})")

    def _click_window(
        self, x: float, y: float,
    ) -> tuple[Optional[tuple[float, float]], Optional[tuple[float, float]]]:
        """Collapse a single click point to a small data-space rectangle.

        About one *displayed* canvas pixel wide/tall, derived from the
        current viewport and the last-rendered canvas size -- the same
        "what does one screen pixel cover in data units" question
        ``VisibilityPlot._add_flag_tools``'s 1:1-zoom math answers for a
        different purpose. Falls back to a tiny fraction of the full
        data extent if the viewport/canvas size can't produce a sane
        (finite, positive) pixel size -- e.g. a degenerate single-value
        axis -- rather than passing a zero-width window through to the
        backend, which would match nothing by exact float equality.

        Returns ``(None, None)`` if the click landed outside the current
        viewport.
        """
        if self._current_viewport is not None:
            vx0, vx1, vy0, vy1 = self._current_viewport
        else:
            vx0, vx1 = self._x_range
            vy0, vy1 = self._y_range
        if not (min(vx0, vx1) <= x <= max(vx0, vx1) and
                min(vy0, vy1) <= y <= max(vy0, vy1)):
            return None, None

        cw = max(int(self._canvas_width), 1)
        ch = max(int(self._canvas_height), 1)
        dx = abs(vx1 - vx0) / cw
        dy = abs(vy1 - vy0) / ch
        if not (math.isfinite(dx) and dx > 0):
            full_dx = abs(self._x_range[1] - self._x_range[0])
            dx = full_dx / 1000.0 if full_dx > 0 else 1e-6
        if not (math.isfinite(dy) and dy > 0):
            full_dy = abs(self._y_range[1] - self._y_range[0])
            dy = full_dy / 1000.0 if full_dy > 0 else 1e-6

        return (x - dx / 2.0, x + dx / 2.0), (y - dy / 2.0, y + dy / 2.0)

    def _probe_region_layer_html(self, lyr: "ScatterLayer", r: dict) -> str:
        """Render one layer's ``probe_scatter_region`` result as an HTML
        section, resolving identity via ``_match_identity`` when exact
        matches were found.

        Deliberately scopes ``_match_identity``'s cache lookup to this
        layer's own polarization (unlike ``_handle_probe``'s hover,
        which passes ``polarization=None`` since a coarse hover can win
        on any layer) -- an exact click already knows exactly which
        layer it's reporting on, so there's no reason to include a
        partition that doesn't carry this layer's polarization.
        """
        status = r.get("status", "no_data")
        heading = f"<h3>{_html_escape(lyr.label)}</h3>"

        if status == "no_data":
            return heading + "<p><i>No data in this region.</i></p>"
        if status == "too_many_points":
            n = r.get("n_samples", 0)
            return heading + (
                f"<p>At least <b>{n:,}</b> points in this region "
                f"&mdash; narrow your selection (click, or drag a "
                f"smaller box) to see exact identity.</p>"
            )

        identity = self._match_identity(
            t_range=r.get("t_range"),
            bl_range=r.get("bl_range"),
            bl_ids=r.get("bl_ids"),
            freq_range=r.get("freq_range"),
            polarization=lyr.polarization,
        )

        rows = [("N samples", f"{r.get('n_samples', 0):,}")]

        fields = identity.get("field_names") or []
        if fields:
            rows.append(("Field" + ("s" if len(fields) > 1 else ""),
                         ", ".join(_html_escape(f) for f in fields)))

        scans = identity.get("scan_names") or []
        if scans:
            rows.append(("Scan" + ("s" if len(scans) > 1 else ""),
                         ", ".join(_html_escape(s) for s in scans)))

        pairs = identity.get("antenna_pairs") or []
        if pairs:
            rows.append((
                f"Antenna pairs ({len(pairs)})",
                ", ".join(f"{_html_escape(a)}&amp;{_html_escape(b)}"
                          for a, b in pairs),
            ))

        fg = identity.get("freq_range_ghz")
        if fg is not None:
            lo, hi = float(fg[0]), float(fg[1])
            rows.append(("Frequency",
                         f"{lo:.9g} GHz" if lo == hi
                         else f"{lo:.9g}\u2013{hi:.9g} GHz"))

        spw_channels = identity.get("spw_channels") or {}
        if spw_channels:
            cw = identity.get("channel_width_hz")
            spw_str = "; ".join(
                f"spw {k}: chan {v[0]}~{v[1]}"
                for k, v in sorted(spw_channels.items(), key=lambda kv: str(kv[0]))
            )
            if cw:
                spw_str += f" (channel width {cw:.6g} Hz)"
            rows.append(("SPW/channels", _html_escape(spw_str)))

        table_rows = "".join(
            f"<tr><td class='cv-k'>{k}</td><td class='cv-v'>{v}</td></tr>"
            for k, v in rows
        )
        return heading + f"<table class='cv-tbl'>{table_rows}</table>"

    @staticmethod
    def _probe_region_page(title: str, body_html: str) -> str:
        """Wrap a body fragment as a complete, standalone HTML document.

        Built entirely in Python -- ``info_tool.ts`` only ever writes
        this string verbatim into the tab it opens, the same "Python
        formats, JS applies" split ``_format_probe``/the status-bar
        ``label`` already use elsewhere in this class. Plain enough
        (a light table, no JS) to select and paste cleanly into an
        email or ticket, which was the point of using a browser tab
        for this instead of squeezing it into the status bar.
        """
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{_html_escape(title)}</title>
<style>
  body {{ background:#1e1e2e; color:#cdd6f4; font-family: -apple-system,
          Helvetica, Arial, sans-serif; margin: 16px 24px; }}
  h2 {{ color:#cdd6f4; border-bottom: 1px solid #45475a; padding-bottom: 6px; }}
  h3 {{ color:#89b4fa; margin-top: 22px; margin-bottom: 6px; }}
  p  {{ line-height: 1.5; }}
  .cv-rect {{ color:#a6adc8; font-family: monospace; font-size: 13px; }}
  table.cv-tbl {{ border-collapse: collapse; margin: 4px 0 12px 0; }}
  table.cv-tbl td {{ padding: 3px 12px 3px 0; vertical-align: top;
                      font-size: 13px; }}
  td.cv-k {{ color:#a6adc8; white-space: nowrap; }}
  td.cv-v {{ color:#cdd6f4; }}
</style></head>
<body><h2>{_html_escape(title)}</h2>{body_html}</body></html>"""

    @staticmethod
    def _layer_reading_html(entry: dict) -> str:
        """Render one layer record from ``layer_results`` as a status cell.

        A pure function of the record, so the status-bar wording can
        change without touching probe logic.  The em dash means
        "consulted, nothing here" — see the reasoning above the readings
        loop for why silence is not an acceptable substitute.  Hidden
        layers are filtered out before this is called: a hidden layer was
        never consulted, so any reading for it would be a lie.

        REMOVED (2026-09, hover-probe redesign piece 2 second pass): the
        "(~Npx)" distance annotation. That described how far a
        neighbor-search had to reach on the old full-resolution
        design -- now that lookups are exact-coarse-cell-only (see
        _handle_probe's docstring), there is no search distance left to
        report; every reading here is either present (this exact coarse
        cell has data) or absent (it doesn't).
        """
        if entry["value"] is None:
            return f"<b>{entry['label']}:</b> <i>&mdash;</i>"
        return f"<b>{entry['label']}:</b> {entry['value']:.6g}"

    def _register_extra_comm_handlers(self) -> None:
        self._comm.register(self._msg_set_alpha,   self._handle_set_alpha)
        self._comm.register(self._msg_color_mode,  self._handle_set_color_mode)
        self._comm.register(self._msg_update_axes, self._handle_update_axes_scatter)
        self._comm.register(self._msg_update_scaling, self._handle_update_scaling)
        # No _msg_colorize registration (Part 5, 2026-09): colorize is
        # staged now, not live -- see colorize_controls()'s docstring.
        # Nothing sends this message anymore; update_colorize() itself
        # (the underlying state mutation) is unaffected and still
        # directly callable, and its actual trigger point is now
        # _handle_update_axes_scatter's own update_axes() call, via
        # whatever coloring/colorize_axis/excluded_categories a fresh
        # ScatterLayer carries in from doPlot's payload.

    # ------------------------------------------------------------------
    # Scatter-specific internals
    # ------------------------------------------------------------------

    def _render_all_layers(
        self,
        selection: "SelectionSpec",
        x_range: Optional[tuple[float, float]] = None,
        y_range: Optional[tuple[float, float]] = None,
    ) -> None:
        """Query, bin, and shade all layers via the backend.

        POST-2026-09: replaces the pre-redesign ``_query_all_layers`` +
        ``_shade_all_layers`` split. A single ``backend.query_columns()``
        call now does the querying *and* the binning/shading (see
        ``ScatterRenderResult``'s docstring in ``data/reader.py``), so
        this one method replaces both -- updating ``_x_range``/
        ``_y_range`` (the full data extent) exactly as
        ``_query_all_layers`` did, plus the new per-layer render-state
        caches that used to be rebuilt locally in ``_shade_all_layers``.

        Parameters
        ----------
        x_range, y_range :
            Viewport to bin/shade at. ``None`` (both) -> full data
            extent -- see ``MSv2Backend.query_columns``.
        """
        layer_specs = [
            ScatterLayerSpec(
                y_axis        = lyr.y_axis,
                polarization  = lyr.polarization,
                cmap          = tuple(lyr.cmap or ()),
                alpha         = lyr.alpha,
                scaling       = lyr.scaling,
                scaling_alpha = lyr.scaling_alpha,
                scaling_gamma = lyr.scaling_gamma,
                scaling_vmin  = lyr.scaling_vmin,
                scaling_vmax  = lyr.scaling_vmax,
                coloring      = lyr.coloring,
                colorize_axis = lyr.colorize_axis,
                excluded_categories = lyr.excluded_categories,
            )
            for lyr in self._layers
        ]

        result = self._backend.query_columns(
            self._x_dim, layer_specs, selection,
            x_range=x_range, y_range=y_range, color_mode=self._color_mode,
            width=self._width, height=self._height,
            probe_grid_max_cells=self._probe_grid_max_cells,
        )

        if len(result.layers) != len(self._layers):
            # Positional correspondence is the whole contract here (no
            # (Axis,pol)-keyed dict to fall back on the way the
            # pre-redesign version had) -- a length mismatch means
            # something upstream reordered or dropped a layer, and
            # zipping the mismatched lists below would silently pair
            # each remaining layer with the wrong render.
            raise RuntimeError(
                f"query_columns returned {len(result.layers)} layer "
                f"results for {len(self._layers)} requested layers"
            )

        self._x_range = result.x_range
        self._y_range = result.y_range
        self._canvas_width  = result.canvas_width
        self._canvas_height = result.canvas_height
        if x_range is None and y_range is None:
            # Only a full-extent call updates the reference size
            # _panel_spec's agg_n_x/agg_n_y reports -- see that
            # method's docstring for why this must stay independent of
            # whatever sub-range a pan/zoom last rendered.
            self._full_canvas_width  = result.canvas_width
            self._full_canvas_height = result.canvas_height

        self._layer_images      = []
        self._layer_n_in_view   = []
        self._layer_peak        = []
        self._layer_hist_counts = []
        self._layer_hist_edges  = []
        self._layer_mapping     = []
        self._layer_skip_reason = []
        self._layer_id_grid     = []
        self._layer_categories       = []
        self._layer_category_colors  = []
        self._layer_category_members = []
        for lyr, rendered in zip(self._layers, result.layers):
            self._layer_images.append(rendered.image)
            self._layer_n_in_view.append(rendered.n_in_view)
            self._layer_peak.append(rendered.peak_value)
            self._layer_hist_counts.append(rendered.hist_counts)
            self._layer_hist_edges.append(rendered.hist_edges)
            mapping = None
            if rendered.mapping_x is not None and rendered.mapping_u is not None:
                mapping = _cms.ScalarMapping(
                    rendered.mapping_x, rendered.mapping_u, lyr.scaling,
                )
            self._layer_mapping.append(mapping)
            self._layer_skip_reason.append(rendered.skip_reason)
            # Colorize-by-axis (Part 4): all three None together for a
            # continuous layer or a skipped/empty categorical one --
            # ScatterLayerRender's own contract (data/reader.py), passed
            # straight through with no reinterpretation needed here.
            self._layer_categories.append(rendered.categories)
            self._layer_category_colors.append(rendered.category_colors)
            self._layer_category_members.append(rendered.category_members)

            # Hover-probe redesign piece 2: coarse id grid, one dict per
            # layer, or None for a layer with no data at all (matches
            # the skip_reason-set case exactly -- there is nothing to
            # grid). See __init__'s docstring for the dict shape.
            if rendered.id_grid_value is not None:
                id_grid: dict = {
                    "value":   rendered.id_grid_value,
                    "x_range": rendered.id_grid_x_range,
                    "y_range": rendered.id_grid_y_range,
                }
                if rendered.id_grid_t_lo is not None:
                    id_grid["t_lo"] = rendered.id_grid_t_lo
                    id_grid["t_hi"] = rendered.id_grid_t_hi
                if rendered.id_grid_bl_lo is not None:
                    id_grid["bl_lo"] = rendered.id_grid_bl_lo
                    id_grid["bl_hi"] = rendered.id_grid_bl_hi
                if rendered.id_grid_freq_lo is not None:
                    id_grid["f_lo"] = rendered.id_grid_freq_lo
                    id_grid["f_hi"] = rendered.id_grid_freq_hi
                self._layer_id_grid.append(id_grid)
            else:
                self._layer_id_grid.append(None)

        # Vestigial -- kept only so _handle_probe's existing guards
        # degrade gracefully. See __init__'s comment on these fields.
        n = len(self._layers)
        self._layer_dfs     = [None] * n
        self._layer_aggs    = [None] * n
        self._layer_extents = [None] * n

        if self._probe_debug:
            for i, (lyr, rendered) in enumerate(zip(self._layers, result.layers)):
                log.info(
                    "[query] L%d[%s]: n_in_view=%d skip=%s",
                    i, lyr.label, rendered.n_in_view, rendered.skip_reason,
                )

    def _current_render_range(
        self,
    ) -> tuple[Optional[tuple[float, float]], Optional[tuple[float, float]]]:
        """(x_range, y_range) to re-query at: the pan/zoom viewport if
        one is active, else ``(None, None)``.

        BUG FIX (2026-09): this used to return ``self._x_range``/
        ``self._y_range`` (the cached full extent) instead of ``(None,
        None)`` when no viewport was active. That's wrong: those two
        fields can be stale -- most importantly, right after an axis
        change, before the query this very call is about to make has
        had a chance to refresh them. Passing the *stale* extent
        through to ``query_columns()`` as an explicit ``x_range``/
        ``y_range`` treats it as a binning constraint, not a "this is
        roughly where things are" hint -- so a fresh axis (e.g.
        switching X to ``Axis.TIME``, whose real values sit nowhere
        near whatever the previous axis's range was) would have every
        sample fall outside it, giving ``n_in_view=0`` for every layer
        and a blank render. The very same call's response then
        correctly refreshes ``self._x_range``/``self._y_range`` from
        the backend's real answer (``ScatterRenderResult.x_range``/
        ``y_range`` are always the true full extent, independent of
        whatever viewport was requested) -- which is exactly why a
        second, identical Plot press "fixed itself": the second call's
        stale values happened to already be correct, left over from
        the first call's response.

        Returning ``(None, None)`` here instead lets that ``None``
        propagate all the way to ``query_columns()`` unresolved, which
        is what actually triggers the backend to compute and report
        the full extent fresh on *this* call -- not the next one.
        """
        if self._current_viewport is not None:
            vx0, vx1, vy0, vy1 = self._current_viewport
            return (vx0, vx1), (vy0, vy1)
        return None, None

    def _effective_skip_reason(self, i: int) -> Optional[str]:
        """skip_reason for layer *i*, accounting for a live ``alpha``
        change that ``set_alpha()``'s free fast path doesn't re-derive
        server-side.

        ``self._layer_skip_reason[i]`` reflects whatever the backend
        said at the *last actual render* -- it can go stale the moment
        ``set_alpha()`` changes ``lyr.alpha`` without a backend call
        (see ``_collapse_and_composite``'s docstring). Checking
        ``lyr.alpha == 0.0`` fresh here, ahead of the cached reason,
        is what keeps ``_panel_spec``/probe reporting consistent with
        what ``set_alpha()`` actually did.
        """
        if i < len(self._layers) and self._layers[i].alpha == 0.0:
            return "hidden (alpha=0)"
        if i < len(self._layer_skip_reason):
            return self._layer_skip_reason[i]
        return None

    def _collapse_and_composite(self) -> np.ndarray:
        """Alpha-collapse + Porter-Duff composite the last rendered images.

        POST-2026-09: binning and shading themselves now happen
        backend-side (see ``ScatterRenderResult``'s docstring in
        ``data/reader.py``); this is the only rendering math still done
        here -- applying each layer's density-derived opacity
        (``auto_alpha * lyr.alpha``) to its cached image, then stacking.
        Ported from the second half of the pre-redesign
        ``_shade_all_layers`` (the part after ``tf.shade()``), unchanged
        in the actual math.

        Needs only ``n_in_view``/canvas pixel count -- no raw data --
        which is what keeps ``set_alpha()`` free of a backend round
        trip. A layer is excluded here if either the backend marked it
        skipped at render time, OR its *current* ``alpha`` is 0 --
        see ``_effective_skip_reason``; the second check is what lets
        ``set_alpha(i, 0.0)`` actually hide a layer without a fresh
        render.
        """
        canvas_pixels = max(1, self._canvas_width * self._canvas_height)
        shaded = []
        for i, lyr in enumerate(self._layers):
            img = self._layer_images[i] if i < len(self._layer_images) else None
            if img is None or self._effective_skip_reason(i) is not None:
                continue
            n_in_view = self._layer_n_in_view[i]

            img_arr = img.copy()
            ratio       = max(1.0, n_in_view / canvas_pixels)
            auto_alpha  = int(255.0 / math.log1p(ratio))
            auto_alpha  = max(80, min(255, auto_alpha))
            layer_alpha = max(0, min(255, int(auto_alpha * lyr.alpha)))
            if layer_alpha > 0:
                nonempty = (img_arr >> 24) > 0
                img_arr[nonempty] = (
                    (img_arr[nonempty] & 0x00FFFFFF)
                    | (np.uint32(layer_alpha) << np.uint32(24))
                )
            shaded.append(img_arr)

        if not shaded:
            return np.zeros(
                (self._canvas_height, self._canvas_width), dtype=np.uint32)
        if len(shaded) == 1:
            return shaded[0]

        # Porter-Duff "over" compositing in numpy on uint32 ARGB arrays.
        # For each pixel: result = src + dst * (1 - src_alpha/255).
        # This is equivalent to tf.stack(..., how="over") but works on
        # plain ndarray so we don't need Datashader Image objects.
        composite = shaded[0].copy()
        for layer_arr in shaded[1:]:
            src_a = ((layer_arr >> 24) & 0xFF).astype(np.float32) / 255.0
            dst_a = ((composite  >> 24) & 0xFF).astype(np.float32) / 255.0
            out_a = src_a + dst_a * (1.0 - src_a)

            # Blend each channel
            for shift in (16, 8, 0):   # R, G, B
                src_c = ((layer_arr >> shift) & 0xFF).astype(np.float32)
                dst_c = ((composite  >> shift) & 0xFF).astype(np.float32)
                with np.errstate(invalid="ignore", divide="ignore"):
                    out_c = np.where(
                        out_a > 0,
                        (src_c * src_a + dst_c * dst_a * (1.0 - src_a)) / out_a,
                        0.0,
                    )
                mask = np.uint32(0xFF) << np.uint32(shift)
                composite = (composite & ~mask) | \
                            (out_c.astype(np.uint32) << np.uint32(shift))

            out_a_u8 = np.clip(out_a * 255, 0, 255).astype(np.uint32)
            composite = (composite & 0x00FFFFFF) | (out_a_u8 << 24)

        return composite

    def _push_image(
        self,
        img32: np.ndarray,
        x_range: tuple[float, float],
        y_range: tuple[float, float],
    ) -> None:
        """Push a composite image into ``_image_source`` at the given range."""
        x0, x1 = x_range
        y0, y1 = y_range
        new_data = {
            "image": [img32],
            "x":     [x0],
            "y":     [y0],
            "dw":    [x1 - x0],
            "dh":    [y1 - y0],
        }
        if self._image_source is None:
            self._image_source = ColumnDataSource(data=new_data)
        else:
            self._image_source.data = new_data

    def _rerender(
        self,
        x_range: Optional[tuple[float, float]] = None,
        y_range: Optional[tuple[float, float]] = None,
    ) -> np.ndarray:
        """Full backend round trip: query+bin+shade, then collapse+composite.

        POST-2026-09: every call here costs a ``query_columns()`` round
        trip -- axis/layer changes, pan/zoom, ``color_mode``/scaling/cmap
        changes all end up here now, because binning and shading happen
        backend-side (see ``ScatterRenderResult``'s docstring in
        ``data/reader.py``). Only ``set_alpha()`` avoids this, via
        ``_recomposite()`` reusing the last render's cached per-layer
        images.

        Parameters
        ----------
        x_range, y_range :
            Viewport to render at. ``None`` (both) -> the current
            pan/zoom viewport if set, else ``(None, None)``, meaning
            "let the backend resolve and report the true full extent
            fresh on this call" -- see ``_current_render_range``'s
            docstring for why this must stay ``None`` here rather than
            being pre-resolved to a possibly-stale cached value.
        """
        if x_range is None and y_range is None:
            x_range, y_range = self._current_render_range()
        self._render_all_layers(self._selection, x_range=x_range, y_range=y_range)
        # Permanent per-panel legend (Part 5): kept in sync with every
        # LIVE render this way, not from inside _render_all_layers()
        # itself -- that method is also called by _shade_for_export(),
        # whose whole point is to render at a different viewport
        # WITHOUT disturbing live state, and the legend widget is a
        # live Bokeh UI object, not part of that method's own
        # save/restore tuple. Calling this here instead means an export
        # never touches it, by construction, rather than needing yet
        # another field added to that restore list.
        self._update_legend()
        img32 = self._collapse_and_composite()
        # _render_all_layers() just refreshed self._x_range/self._y_range
        # from the backend's real answer when x_range/y_range were None
        # (full-extent request) -- push THOSE, now correct, for display
        # positioning. An explicit viewport (pan/zoom or a preserved one
        # from _current_render_range) is already a real, non-None range
        # and gets pushed as-is.
        push_x_range = x_range if x_range is not None else self._x_range
        push_y_range = y_range if y_range is not None else self._y_range
        self._push_image(img32, push_x_range, push_y_range)
        return img32

    def _recomposite(self) -> None:
        """Alpha-only fast path: no backend call, reuses the last
        render's cached per-layer images. Used by ``set_alpha()``.

        BUGFIX (2026-09, found running this file's real-data test suite
        for the first time): ``_current_render_range()`` deliberately
        returns ``(None, None)`` when there's no active pan/zoom
        viewport -- see its own docstring -- on the assumption that the
        caller is about to make a fresh backend call and will use
        *that* call's just-refreshed ``self._x_range``/``self._y_range``
        instead. ``_rerender()`` satisfies that assumption (it always
        queries the backend); this method is explicitly the path that
        does not (that's the whole point of the "alpha-only fast path"),
        so passing ``(None, None)`` straight through to ``_push_image()``
        crashed on every call made before any viewport had been set --
        i.e. right after construction, before any pan/zoom, which is
        the common case, not an edge case (confirmed: reproduced on a
        freshly-constructed real ``VisibilityScatter`` before any other
        interaction). Since this path makes no backend call, nothing
        has changed since the last real render that would make the
        cached ``self._x_range``/``self._y_range`` stale -- unlike
        ``_current_render_range()``'s own docstring concern, which is
        specifically about staleness *around* a fresh query, not about
        a no-query call like this one. So falling back to them directly
        here is correct, not just convenient.
        """
        img32 = self._collapse_and_composite()
        xr, yr = self._current_render_range()
        if xr is None:
            xr = self._x_range
        if yr is None:
            yr = self._y_range
        self._push_image(img32, xr, yr)

    # ------------------------------------------------------------------
    # j2p handlers (scatter-specific)
    # ------------------------------------------------------------------

    def set_layer_cmaps(self, cmaps) -> None:
        """Swap every layer's colormap and re-render.

        A ``SHADE``-level change (``refresh.py``).  Assigns by layer
        index modulo the family length, matching how the family is
        applied at construction, so a scatter with more layers than the
        family has entries still cycles rather than failing.
        """
        from dataclasses import replace
        self._layer_cmaps = list(cmaps)
        self._layers = [
            replace(lyr, cmap=self._layer_cmaps[i % len(self._layer_cmaps)])
            for i, lyr in enumerate(self._layers)
        ]
        self._reshade()

    def _reshade(self) -> None:
        """Re-render from the backend with the current layer params.

        POST-2026-09: despite the name (kept for call-site continuity
        with ``set_layer_cmaps``), this is now a full backend round
        trip, not a local-only recomposite -- ``cmap`` is a
        ``tf.shade()`` parameter, which runs backend-side now (see
        ``ScatterRenderResult``'s docstring in ``data/reader.py``).
        No-ops if the panel has never been rendered (deferred
        construction, axes not yet chosen).
        """
        if all(img is None for img in self._layer_images):
            return
        self._rerender()

    def set_color_mode(self, mode: str) -> None:
        """Toggle color mode and re-render.

        POST-2026-09: this now issues a backend ``query_columns()``
        call (see ``_rerender``) -- ``color_mode`` selects the eq_hist/
        span reference population, which is resolved backend-side now
        (see ``ScatterRenderResult``'s docstring in ``data/reader.py``).
        No longer free the way it was before that redesign.

        Parameters
        ----------
        mode : ``"global"`` | ``"local"``
            ``"global"`` (default) — ``linear`` shading with ``span`` anchored
            to the full data y_range.  A 50 Jy point always maps to the same
            color regardless of zoom level.  Recommended for flagging.
            ``"local"`` — ``linear`` shading with ``span`` derived from the
            amplitude range of the data visible in the current viewport.  The
            full Plasma palette spans whatever is on screen, so zooming into
            a narrow amplitude range uses the full color range for that
            region.  Colors change on zoom (expected).  Best for exploring
            structure and low-contrast features within a region.
        """
        if mode not in ("global", "local"):
            raise ValueError(
                f"color_mode must be 'global' or 'local', got {mode!r}"
            )
        self._color_mode = mode
        self._rerender()
        self._update_state_source()

    def set_probe_grid_resolution(self, max_cells: int) -> None:
        """Adjust the coarse hover-identity grid's resolution and re-render.

        Hover-probe redesign piece 2 (2026-09) -- see
        ``ScatterLayerRender.id_grid_*``'s docstring in ``data/reader.py``
        for what this grid is and why it exists.  Same shape as
        ``update_scaling``/``set_color_mode``: this changes something the
        backend computes as part of the same ``query_columns()`` call
        that produces everything else, so a resolution change costs a
        fresh backend round trip regardless -- there is no free,
        client-side way to change it after the fact.

        A higher ``max_cells`` narrows a hover's reported
        scan/antenna/SPW range at the cost of a modestly larger
        response payload (still tens of KB even at fairly high
        resolution -- six-to-seven scalar reductions per coarse bin,
        not raw data).  ``probe_scatter_pixel`` (click-to-exact, piece
        3) remains the way to get an exact reading regardless of this
        setting.

        Parameters
        ----------
        max_cells : int
            Upper bound on the coarse grid's total cell count
            (width x height) -- see ``_scatter_render._id_grid_size``.
            Must be positive.
        """
        max_cells = int(max_cells)
        if max_cells <= 0:
            raise ValueError(f"max_cells must be positive, got {max_cells}")
        self._probe_grid_max_cells = max_cells
        self._rerender()

    def _image_response(self, status: str = "ok", **extra) -> dict:
        """Build a j2p response dict containing the current image and viewport.

        All j2p handlers that update the composite image use this so the JS
        callback can correctly reposition the image glyph at the current
        viewport extents rather than the full data range.
        """
        src = self._image_source.data
        return {
            "status": status,
            "image":  src["image"][0],
            "x0":     src["x"][0],
            "x1":     src["x"][0] + src["dw"][0],
            "y0":     src["y"][0],
            "y1":     src["y"][0] + src["dh"][0],
            **extra,
        }

    async def _handle_set_color_mode(self, message: dict) -> dict:
        """Handle j2p message to toggle color mode: {mode: "global"|"local"}.

        Returns the new composite image so the JS callback can update
        image_source.data directly — Python-side model property changes
        don't propagate to the browser in static HTML mode.

        Async + to_thread (2026-09): set_color_mode() re-renders every
        layer (_rerender() -> _render_all_layers() -> a real backend
        query), which can take long enough to starve this connection's
        own ping/pong keepalive if run inline on the event loop -- see
        CommMgr._handle_request's per-comm Lock (this instance's
        self._render_lock, shared with self._flag_comm) for what keeps
        this safe now that it can interleave with other handlers.
        """
        mode = message.get("mode", "global")
        try:
            await asyncio.to_thread(self.set_color_mode, mode)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        return self._image_response("ok", color_mode=self._color_mode)

    async def _handle_set_alpha(self, message: dict) -> dict:
        """Handle j2p 'vs_set_alpha': {layer_index: int, alpha: float}.

        Async + to_thread -- see _handle_set_color_mode's docstring;
        set_alpha() also ends in a real re-render.
        """
        idx   = int(message.get("layer_index", 0))
        alpha = float(message.get("alpha", 1.0))
        try:
            await asyncio.to_thread(self.set_alpha, idx, alpha)
        except (IndexError, ValueError) as exc:
            return {"status": "error", "message": str(exc)}
        return self._image_response("ok")

    async def _handle_update_scaling(self, message: dict) -> dict:
        """Handle j2p 'vs_update_scaling': {layer_index, scaling, alpha, gamma, vmin, vmax, reset_range}.

        All fields except layer_index are optional; omitted fields keep
        their current per-layer value. Uses _image_response so JS can
        update image_source directly, mirroring _handle_set_alpha.

        Async + to_thread -- see _handle_set_color_mode's docstring;
        update_scaling() also ends in a real re-render.
        """
        idx = int(message.get("layer_index", 0))
        try:
            await asyncio.to_thread(
                self.update_scaling,
                idx,
                scaling     = message.get("scaling"),
                alpha       = message.get("alpha"),
                gamma       = message.get("gamma"),
                vmin        = message.get("vmin"),
                vmax        = message.get("vmax"),
                reset_range = bool(message.get("reset_range", False)),
            )
        except (IndexError, ValueError) as exc:
            return {"status": "error", "message": str(exc)}
        lyr = self._layers[idx]
        return self._image_response(
            "ok",
            layer_index   = idx,
            scaling       = lyr.scaling,
            scaling_alpha = lyr.scaling_alpha,
            scaling_gamma = lyr.scaling_gamma,
            scaling_vmin  = lyr.scaling_vmin,
            scaling_vmax  = lyr.scaling_vmax,
        )

    def _legend_html(self, layer_index: int) -> str:
        """Render one layer's categorical legend as an HTML swatch list.

        Empty (well, a small italic placeholder) when the layer isn't
        categorical yet, hasn't been rendered yet, or rendered with no
        categories -- ``colorize_controls()``'s legend ``Div`` stays
        unpopulated in all of those cases, matching how
        ``colormap_controls()``'s histogram shows nothing for a
        never-rendered layer.

        Built from ``category_members``, not just ``categories`` --
        per visplot-colorize-by-axis-handoff-part4.md's "What this
        means for Part 4's legend widget": a bucketed category
        (``len(category_members[cat]) > 1``, e.g. an antenna range
        binned past ``CATEGORY_CAP``) gets every real value it covers
        listed in a native HTML ``title`` attribute, so hovering the
        swatch shows the full membership at essentially no extra
        cost -- no separate Bokeh tooltip model, just an attribute on
        the ``Div``'s own HTML.  An unbucketed category has
        ``category_members[cat] == (cat,)`` (see
        ``ScatterLayerRender.category_members``'s docstring), so it
        gets no ``title`` at all -- nothing more to say than the label
        already shows.
        """
        if not (0 <= layer_index < len(self._layers)):
            return ""
        lyr = self._layers[layer_index]
        if lyr.coloring != "categorical":
            return ""
        categories = (self._layer_categories[layer_index]
                      if layer_index < len(self._layer_categories) else None)
        colors = (self._layer_category_colors[layer_index]
                  if layer_index < len(self._layer_category_colors) else None)
        if not categories or not colors:
            return ("<i style='color:#a6adc8;font-size:11px'>"
                     "no categories in current selection</i>")
        members = (self._layer_category_members[layer_index]
                   if layer_index < len(self._layer_category_members) else None) or {}

        rows = []
        for cat in categories:
            color = colors.get(cat, "#888888")
            cat_members = members.get(cat, (cat,))
            title_attr = ""
            if len(cat_members) > 1:
                joined = ", ".join(str(m) for m in cat_members)
                title_attr = f' title="{_html_escape(joined)}"'
            rows.append(
                f"<div{title_attr} style='display:flex;align-items:center;"
                f"gap:6px;margin:2px 0;cursor:default;break-inside:avoid;"
                f"-webkit-column-break-inside:avoid'>"
                f"<span style='display:inline-block;width:10px;"
                f"height:10px;border-radius:2px;flex-shrink:0;"
                f"background:{_html_escape(str(color))}'></span>"
                f"<span style='color:#cdd6f4;font-size:11px;"
                f"overflow:hidden;text-overflow:ellipsis;"
                f"white-space:nowrap'>{_html_escape(str(cat))}</span>"
                f"</div>"
            )
        # Multi-column (2026-09, reported: a long category list -- e.g.
        # every scan number -- pushed the cursor-tracking status bar
        # below off screen in a single vertical list). column-width
        # (not a fixed column-count) lets the browser pick however many
        # ~110px columns actually fit this Div's own width, rather than
        # a number tuned for one sidebar width that would either
        # overflow a narrower one or waste space in a wider one. No
        # inner max-height/overflow-y here -- the OUTER legend_content
        # Div (see VisibilityPlot._build()) already provides that
        # boundary; nesting a second independent scroll region inside
        # it would be one scrollbar too many.
        return ("<div style='column-width:110px;column-gap:14px;"
                 "margin-top:4px'>" + "".join(rows) + "</div>")

    def _full_legend_html(self) -> str:
        """Combine every categorical layer's legend into one HTML block
        for the permanent per-panel legend (see ``_update_legend()``).
        Empty string if no layer is currently categorical (or none
        have rendered categories yet) -- the caller uses that to hide
        the whole legend wrapper, not leave an empty box visible.

        Layer labels prefix each layer's own swatch block only when
        more than one layer is categorical at once -- the same "don't
        clutter the single-layer case, disambiguate the multi-layer
        one" rule already used for ``png_export.py``'s own categorical
        legend (``_legend_handles``), applied here to the live browser
        legend instead of the static export.
        """
        categorical_indices = [
            i for i, lyr in enumerate(self._layers) if lyr.coloring == "categorical"
        ]
        if not categorical_indices:
            return ""
        multi = len(categorical_indices) > 1
        blocks = []
        for i in categorical_indices:
            html = self._legend_html(i)
            if not html:
                continue
            if multi:
                lyr = self._layers[i]
                blocks.append(
                    f"<div style='color:#a6adc8;font-size:11px;"
                    f"font-weight:bold;margin-top:6px'>"
                    f"{_html_escape(lyr.label)}</div>" + html
                )
            else:
                blocks.append(html)
        return "".join(blocks)

    def _update_legend(self) -> None:
        """Push the current combined categorical legend to the
        permanent per-panel info strip (``VisibilityPlot._build()``'s
        ``_legend_content``/``_legend_toggle``/``_cursor_toggle``/
        ``_info_div``).

        Deliberately independent of ``colorize_controls()``'s own
        widgets -- called after every real render (from
        ``_render_all_layers``, so every code path that ends in one --
        ``update_colorize``, ``update_scaling``, ``update_axes``, a
        ``doPlot`` press -- keeps this in sync automatically, with no
        caller needing to remember to call it separately. This is
        exactly what Part 4's live, gear-tab-scoped legend did NOT do:
        it only ever reflected the last live round trip, going stale
        (or blank) the moment the tab was closed and reopened even
        though the actual plot was still genuinely categorical. This
        one only ever reflects the ACTUAL rendered state, because it is
        rebuilt from that state every single time it changes.

        Toggle visibility only, never the active view (Part 5 addendum,
        2026-09): becoming categorical makes the "Legend" button appear
        so the user notices it's there, but does NOT switch away from
        whichever of cursor-tracking/legend they're currently looking
        at -- cursor-tracking is more likely to be what's actively in
        use (it updates on every hover), so auto-switching to the
        legend the moment new content exists would fight against that.
        Losing the content (switched back to continuous) DOES force the
        view back to cursor-tracking, though -- there's nothing left to
        show, so leaving the legend both selected and invisible would
        strand the user looking at nothing with no visible way back
        (the "Cursor" button is still there, but there's no reason to
        make them find it).

        No-op in headless mode, and during the very first render inside
        ``_build()`` (called before ``_legend_content`` even exists as
        an attribute -- ``_build()``'s figure/glyphs/legend-widget
        construction all happen AFTER its own initial ``_render()``
        call) -- ``getattr`` rather than a plain attribute check, so
        this degrades safely in both cases rather than raising.
        """
        if getattr(self, "_legend_content", None) is None:
            return
        html = self._full_legend_html()
        self._legend_content.text = html
        self._legend_toggle.visible = bool(html)
        if not html and self._legend_content.visible:
            # The legend was the active view and just lost its content
            # (switched back to continuous) -- force back to
            # cursor-tracking rather than leaving the user looking at a
            # blank pane with no legend button left to click back from.
            self._legend_content.visible = False
            self._info_div.visible = True
            self._cursor_toggle.button_type = "primary"
            self._legend_toggle.button_type = "default"

    def _colorize_category_values(self, axis, polarization: str) -> list[str]:
        """All possible raw category values for *axis*, from cheap,
        already-cached ``IdentityTables`` metadata -- no backend round
        trip. The widget layer's own "similar to SPW" enumeration (see
        ``VisibilityPlotter``'s permanent SPW ``DataTable``, populated
        from ``meta.spws``): the same "cheap static metadata, not a
        live query" pattern, one level down, per colorizable axis
        instead of per-MS.

        Returns raw values (individual antenna names, scan names, ...),
        not post-binning display labels -- matching
        ``ScatterLayerSpec.excluded_categories``'s own contract (see
        that field's docstring for why the checklist this feeds must
        deal in raw values, never buckets a render hasn't computed
        yet).
        """
        from .axes import Axis
        tables = self._ensure_identity_tables(polarization)
        if axis is Axis.SCAN:
            values = {s.scan_name for s in tables.scans}
        elif axis is Axis.ANTENNA1:
            values = {a1 for a1, _a2 in tables.baseline_antennas.values()}
        elif axis is Axis.ANTENNA2:
            values = {a2 for _a1, a2 in tables.baseline_antennas.values()}
        elif axis is Axis.SPW:
            values = {str(s.spw_id) for s in tables.spws}
        else:
            values = set()

        def _sort_key(v):
            # Numeric-looking values (scan numbers) sort numerically;
            # everything else (antenna/SPW names) falls back to plain
            # string order. Cosmetic only -- exclusion itself doesn't
            # depend on this order, just the checklist's own display.
            try:
                return (0, int(v))
            except ValueError:
                return (1, v)

        return sorted(values, key=_sort_key)

    def colorize_controls(self, layer_index: int = 0):
        """Return a Bokeh widget column for one layer's colorize-by-axis
        controls, plus the widget handles ``doPlot()``'s own
        payload-building JS needs to read their staged values from.

        Part 5 (2026-09) REDESIGN: staged, not live. Every OTHER control
        in the gear tab (axis pickers, Field, SPW, Correlation, ...)
        only takes effect when the user presses Plot -- ``doPlot()``
        reads their current values at that moment and sends them
        together. Part 4's original version of this method broke that
        pattern: it sent a live ``comm.send()`` on every change,
        immediately re-rendering. That produced a real, user-visible
        inconsistency: the categorical legend was live and correct
        while this tab stayed open, but empty again on reopening it
        later even though the plot itself was still genuinely
        categorical -- nothing about reopening this tab replayed that
        one-off response.

        This version holds no ``Comm`` reference and sends nothing.
        Every widget here only tracks its own current value;
        ``doPlot()``'s payload-building JS reads them the same way it
        already reads ``sx_sel.value``/``sy_sel.value``. The actual
        categorical render happens exactly once, when Plot is pressed,
        inside ``_handle_update_axes_scatter``'s ``update_axes()`` call
        -- not here. ``update_colorize()`` (the underlying state
        mutation) is unaffected and still directly callable; only the
        live comm trigger (``_handle_colorize``/``_msg_colorize``) was
        removed, since nothing sends it anymore.

        Category checklist (Part 5): one ``DataTable`` per colorizable
        axis, pre-built here from ``_colorize_category_values()`` --
        "build for N, ship visible 1", the same precedent already used
        for the per-layer columns in ``_build_scatter_config_panel``,
        applied one level down (per-axis instead of per-layer). Only
        the checklist matching ``axis_select``'s current value is
        visible; switching axes is a pure client-side visibility swap,
        same as switching layers -- no round trip needed, since every
        axis's possible values are already known up front.

        State preservation (2026-09, explicit design decision, not an
        oversight): the axis currently in effect for this layer
        (``lyr.colorize_axis``) starts with ``lyr.excluded_categories``
        unchecked and everything else checked, so reopening this tab
        shows what is actually plotted right now -- letting the user
        judge whether a replot is even needed before touching anything.
        Any OTHER axis -- one the user hasn't switched to since opening
        this tab, with no rendered state of its own to reflect -- always
        starts fully checked.

        Styling gap (known, deliberate for this pass): each checklist's
        ``DataTable`` uses Bokeh's own defaults rather than the
        sidebar's bespoke dark/light table CSS (``visibility_plotter.py``'s
        ``_DARK_TABLE_CSS``/``_LIGHT_TABLE_CSS``, used by the permanent
        SPW table) -- pulling those in here would need either a new
        constructor parameter threaded from ``_build_scatter_config_panel``
        or an upward import from this (widget) layer into the app layer,
        and matching that theme exactly is a smaller gap than everything
        else landed in this pass. Worth a follow-up, not a blocker.

        The axis picker excludes ``DEGENERATE_COLORIZE_AXES``
        (currently just ``Axis.CORRELATION``) entirely, rather than
        including it disabled or annotated -- Bokeh's ``Select`` has no
        way to disable one option, and an axis that can never produce
        more than a single-entry legend for a single-polarization layer
        is not a meaningful choice to offer. Filtered by frozenset
        membership, not a hardcoded axis name, so this stays correct if
        that frozenset's membership ever changes (see its docstring in
        ``data/reader.py``).

        Parameters
        ----------
        layer_index : int
            Which layer's controls to build. Defaults to the first
            layer.

        Returns
        -------
        controls : Bokeh column
            The widget tree for the sidebar.
        handles : dict
            ``{"mode_group": RadioButtonGroup, "axis_select": Select,
            "checklists": {axis_name_str: (DataTable, ColumnDataSource)}}`` --
            for ``doPlot()``'s payload-building JS to read current
            values from directly, the same way it already holds
            ``sx_sel``/``sy_sel``.
        """
        from bokeh.layouts import column
        from bokeh.models import (Select, RadioButtonGroup, Div, CustomJS,
                                   DataTable, TableColumn, ColumnDataSource)
        from .data.reader import colorizable_axes, DEGENERATE_COLORIZE_AXES
        from .axes import Axis

        if not (0 <= layer_index < len(self._layers)):
            raise IndexError(f"layer_index {layer_index} out of range")
        lyr = self._layers[layer_index]

        axis_options = [
            (axis.name, axis.label) for axis in colorizable_axes()
            if axis not in DEGENERATE_COLORIZE_AXES
        ]
        if not axis_options:
            # Defensive only -- today's single-entry DEGENERATE_COLORIZE_AXES
            # can never exhaust colorizable_axes() on its own. Falling
            # back to the unfiltered list rather than shipping an empty
            # (and therefore broken) Select if that ever changes.
            axis_options = [(axis.name, axis.label) for axis in colorizable_axes()]
        colorizable = [Axis[name] for name, _ in axis_options]

        is_categorical = lyr.coloring == "categorical"
        current_axis = (lyr.colorize_axis if lyr.colorize_axis is not None
                         else colorizable[0])

        section = Div(
            text=f"<span style='color:#a6adc8;font-size:11px'>"
                 f"Colorize \u2014 {_html_escape(lyr.label)}</span>",
        )
        mode_group = RadioButtonGroup(
            labels=["Continuous", "Categorical"],
            active=(1 if is_categorical else 0),
        )
        axis_select = Select(
            value=current_axis.name,
            options=axis_options,
            visible=is_categorical,
        )

        checklists: dict = {}
        checklist_tables = []
        for axis in colorizable:
            try:
                values = self._colorize_category_values(axis, lyr.polarization)
            except Exception as exc:
                log.warning("colorize_controls: could not enumerate %s: %s",
                            axis.name, exc)
                values = []
            source = ColumnDataSource(data=dict(value=values))
            if axis is current_axis and is_categorical:
                excluded = set(lyr.excluded_categories)
                source.selected.indices = [
                    i for i, v in enumerate(values) if v not in excluded
                ]
            else:
                source.selected.indices = list(range(len(values)))
            table = DataTable(
                source=source,
                columns=[TableColumn(field="value", title=axis.label)],
                selectable="checkbox",
                index_position=None,
                width=240,
                height=min(max(len(values), 1) * 26 + 30, 170),
                visible=(is_categorical and axis is current_axis),
            )
            checklists[axis.name] = (table, source)
            checklist_tables.append(table)

        controls = column(section, mode_group, axis_select, *checklist_tables)

        # Staged, not live (see this method's own docstring): both
        # callbacks below only manage visibility, locally, of this
        # method's own widgets -- neither touches self._comm, and
        # neither exists if self._comm is None, matching
        # colormap_controls()'s own "no comm -> inert" convention (the
        # difference here is that inert is now this method's ONLY mode
        # regardless of comm state, so the two are unconditional).
        checklist_by_axis_name = {name: t for name, (t, _s) in checklists.items()}
        mode_js = CustomJS(
            args={"axis_select": axis_select,
                  "checklist_by_axis": checklist_by_axis_name},
            code="""
const categorical = (cb_obj.active === 1);
axis_select.visible = categorical;
for (const name in checklist_by_axis) {
    checklist_by_axis[name].visible = categorical && (name === axis_select.value);
}
""",
        )
        mode_group.js_on_change("active", mode_js)

        axis_js = CustomJS(
            args={"checklist_by_axis": checklist_by_axis_name},
            code="""
for (const name in checklist_by_axis) {
    checklist_by_axis[name].visible = (name === cb_obj.value);
}
""",
        )
        axis_select.js_on_change("value", axis_js)

        return controls, {
            "mode_group": mode_group,
            "axis_select": axis_select,
            # Keyed by axis .name string (e.g. "ANTENNA1"), NOT the Axis
            # enum member itself -- these handles end up inside a
            # CustomJS args dict once doPlot()'s own args are built (see
            # _build_sidebar), which needs JSON-compatible dict keys/
            # values throughout; an Enum member is neither a Bokeh Model
            # (which CustomJS args does know how to reference) nor a
            # JSON primitive, so it would fail to serialize at that
            # point. String keys also match axis_select.value's own
            # type directly, which is what doPlot()'s JS actually reads
            # to know which checklist is the active one.
            "checklists": checklists,
        }

    def _with_default_cmaps(self, layers) -> list:
        """Return *layers* with any missing ``cmap`` filled in by index.

        Must be applied wherever ``self._layers`` is set, not only in
        ``__init__``.  ``_handle_update_axes_scatter`` constructs
        ``ScatterLayer`` objects from a j2p payload without a ``cmap``
        field -- the browser has no reason to send one -- so they arrive
        with ``cmap=None``.  Before this helper existed, ``update_axes``
        assigned that list straight to ``self._layers``, and every
        subsequent ``tf.shade(cmap=None)`` failed with "Expected `cmap`
        of ...; got: <class 'NoneType'>", blanking the panel after any
        sidebar axis or layer change.

        Part 4: a categorical layer with no cmap gets a categorical
        palette here, not the continuous ``_layer_cmaps`` cycle -- the
        same cmap-is-reused-for-categorical convention
        ``update_colorize`` follows (see ``ScatterLayerSpec``'s
        docstring in ``data/reader.py``). Filling it with a continuous
        gradient would still satisfy the "non-empty cmap" validation but
        render nonsense (a 10-stop sequential ramp cycled as if it were
        20 discrete category colors).
        """
        out = []
        for i, lyr in enumerate(layers):
            if lyr.cmap is None:
                if lyr.coloring == "categorical":
                    from . import palettes
                    cmap = tuple(palettes.categorical_cmap(theme=self._theme_hint()))
                else:
                    cmap = self._layer_cmaps[i % len(self._layer_cmaps)]
                lyr = ScatterLayer(
                    y_axis        = lyr.y_axis,
                    polarization  = lyr.polarization,
                    cmap          = cmap,
                    alpha         = lyr.alpha,
                    label         = lyr.label,
                    scaling       = lyr.scaling,
                    scaling_alpha = lyr.scaling_alpha,
                    scaling_gamma = lyr.scaling_gamma,
                    scaling_vmin  = lyr.scaling_vmin,
                    scaling_vmax  = lyr.scaling_vmax,
                    coloring      = lyr.coloring,
                    colorize_axis = lyr.colorize_axis,
                    excluded_categories = lyr.excluded_categories,
                )
            out.append(lyr)
        return out

    async def _handle_update_axes_scatter(self, message: dict) -> dict:
        """Handle j2p 'vs_update_axes' with scatter-specific fields.

        Async + to_thread for the actual update_axes() call -- see
        _handle_set_color_mode's docstring; this is the handler behind
        "change the X axis and replot", which ends in a real re-render
        exactly like the others here.
        """
        from .axes import Axis
        new_layers = None
        if "layers" in message:
            try:
                new_layers = [
                    ScatterLayer(
                        y_axis        = Axis[entry["y_axis"]],
                        polarization  = entry.get("polarization", "XX"),
                        alpha         = float(entry.get("alpha", 1.0)),
                        label         = entry.get("label", ""),
                        scaling       = entry.get("scaling", _DEFAULT_SCALING),
                        scaling_alpha = float(entry.get("scaling_alpha", 10.0)),
                        scaling_gamma = float(entry.get("scaling_gamma", 1.0)),
                        # Part 4: parsed defensively like every other
                        # field here even though nothing currently sends
                        # them on an axis-change message -- an axis
                        # change is a fresh ScatterLayer either way (this
                        # branch already drops scaling_vmin/vmax the same
                        # way), so a layer switched to categorical simply
                        # starts over in continuous mode unless a future
                        # caller starts including these.
                        coloring      = entry.get("coloring", "continuous"),
                        colorize_axis = (Axis[entry["colorize_axis"]]
                                         if entry.get("colorize_axis") else None),
                        # Part 5: doPlot's own payload-building JS is
                        # what will actually populate this (see
                        # colorize_controls()'s per-axis checklists) --
                        # parsed defensively here regardless, matching
                        # every other field on this line.
                        excluded_categories = tuple(entry.get("excluded_categories", ())),
                    )
                    for entry in message["layers"]
                ]
            except Exception as exc:
                log.warning("_handle_update_axes_scatter: bad layers: %s", exc)

        await asyncio.to_thread(
            self.update_axes,
            x_dim  = self._parse_axis(message, "x_dim"),
            layers = new_layers,
            title  = message.get("title"),
        )
        return {"status": "ok"}
