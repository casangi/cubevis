"""
visibility_scatter.py
=====================
Datashader scatter plot for visibility data with multiple overlaid layers.

Each layer is a ``ScatterLayer`` specifying a y-axis quantity, polarization,
color map, and alpha value.  All layers share the same x-axis and
``SelectionSpec``.

Rendering pipeline
------------------
For each layer::

    query_columns(x_axis, [(y_axis, pol)], selection)
        -> DataFrame with columns "x", "y"

    Canvas.points(df, "x", "y", agg=mean("y"))
        -> canvas-resolution float64 agg (H × W)

    tf.shade(agg, cmap=layer.cmap, alpha=int(layer.alpha * 255))
        -> Datashader Image (uint32, RGBA)

Then composite all layer images::

    tf.stack(*images, how="over")
        -> single composite Datashader Image

The composite is pushed to a single Bokeh ``image_rgba`` glyph via
``_image_source``.  This avoids painter's-order artefacts and lets
Datashader handle alpha compositing correctly in float space.

Alpha control
-------------
Each layer's ``alpha`` (0.0–1.0) is stored in ``_state_source`` as
``layer_alpha_N`` for JS widgets to read.  Changing alpha only re-runs
the shade + stack step (no re-query), so it is fast.

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
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING
from uuid import uuid4

import numpy as np

from bokeh.models import ColumnDataSource

from .visibility_plot import (
    VisibilityPlot, _img_to_uint32, _json_num,
)
from .panel_spec import ColorBand, PanelSpec
from . import colormap_scaling as _cms

if TYPE_CHECKING:
    import pandas as pd
    import xarray as xr
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

    def __post_init__(self):
        if not self.label:
            self.label = f"{self.y_axis.label} {self.polarization}"


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

        # Cached DataFrames and canvas aggs — one per layer
        self._layer_dfs:  list[Optional["pd.DataFrame"]] = [None] * len(layers)
        self._layer_aggs: list[Optional["xr.DataArray"]] = [None] * len(layers)
        self._layer_skip_reason: list[Optional[str]] = [None] * len(layers)
        self._layer_extents: list[Optional[tuple]] = [None] * len(layers)

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
        )
        self._composite_and_push()
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
        """Change one layer's value-to-color transfer function and re-composite.

        Does NOT re-query the backend — only re-runs shade + stack, mirroring
        ``set_alpha()``.

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
            ``color_mode``-based range (see ``_shade_all_layers``) once
            set. ``None`` keeps the current value on its own — use
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
        )
        self._composite_and_push()
        self._update_state_source()

    def histogram(
        self, layer_index: int, bins: int = 254,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(counts, bin_edges)`` for one layer's cached agg values.

        Used by ``colormap_controls()`` for an eventual histogram display
        alongside the scaling controls.  Returns empty arrays if the
        layer hasn't been rendered yet.
        """
        if not (0 <= layer_index < len(self._layer_aggs)):
            raise IndexError(f"layer_index {layer_index} out of range")
        agg = self._layer_aggs[layer_index]
        if agg is None:
            return np.array([]), np.array([])
        finite = agg.values[np.isfinite(agg.values)]
        if finite.size == 0:
            return np.array([]), np.array([])
        counts, edges = np.histogram(finite, bins=bins)
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
            self._layer_dfs  = [None] * len(layers)
            self._layer_aggs = [None] * len(layers)
            self._layer_skip_reason = [None] * len(layers)
            self._layer_extents = [None] * len(layers)
            changed = True
        if title is not None:
            self._title = title;  changed = True

        # A never-yet-rendered (defer_initial_render=True) panel must
        # render on its first update_axes() call regardless of what else
        # changed — same rationale as VisibilityRaster's identical guard.
        # Scatter has no single self._agg; "never rendered" here means
        # every layer's DataFrame is still None (set that way by
        # _render(defer=True), and by the layers-replacement branch above,
        # which is why this check must come after it).
        if all(df is None for df in self._layer_dfs):
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
            )
            for lyr in self._layers
        )

        # agg_n_x/agg_n_y: canvas resolution at the *full* data extent --
        # same field names as VisibilityRaster so FlagTool's existing
        # zoom-to-1:1 math (flag_tool.ts) works unchanged here too. Unlike
        # raster, scatter points are exact (not binned/decimated), so this
        # isn't about resolving averaged data -- it's the same
        # sparse-data canvas-shrink logic _shade_all_layers uses
        # (_compute_canvas_size), which means "1:1" for scatter
        # effectively means "zoomed in enough that the full-extent view's
        # overplot-driven canvas shrink no longer applies" -- a reasonable
        # proxy for "not looking at an overplotted, ambiguous cluster."
        full_x0, full_x1 = self._x_range
        full_y0, full_y1 = self._y_range
        agg_n_x, agg_n_y = self._compute_canvas_size(
            full_x0, full_x1, full_y0, full_y1)

        drawable = any(
            df is not None and len(df) > 0 and lyr.alpha > 0.0
            for lyr, df in zip(self._layers, self._layer_dfs)
        )
        status, note = "ok", None
        if not drawable:
            status = "empty"
            reasons = [
                f"{lyr.label}: {r}"
                for lyr, r in zip(self._layers, self._layer_skip_reason)
                if r
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
        """One mapping per layer, plus each layer's peak per-pixel count.

        A scatter agg is a *count*, so these bands are ``kind="density"``
        -- which is what stops the compositor labelling the ramp with the
        layer's amplitude label.  ``peak_density`` feeds the legend
        annotation that scatter panels show instead of a colorbar.

        Uses whatever ``_layer_aggs`` the last shade left, so the curve
        matches the pixels on screen.  A layer that was skipped has no
        agg and keeps ``mapping=None``, which the compositor reads as
        "no bar for this band".
        """
        from dataclasses import replace
        from .colormap_scaling import ScalarMapping

        out = []
        for i, band in enumerate(spec.bands):
            agg = (self._layer_aggs[i]
                   if i < len(self._layer_aggs) else None)
            if agg is None or not band.visible:
                out.append(replace(band, kind="density"))
                continue
            values = np.asarray(agg.values, dtype=np.float64)
            finite = values[np.isfinite(values)]
            peak = float(finite.max()) if finite.size else None
            lyr = self._layers[i]
            m = ScalarMapping.from_values(
                values, lyr.scaling,
                alpha = lyr.scaling_alpha,
                gamma = lyr.scaling_gamma,
                vmin  = lyr.scaling_vmin,
                vmax  = lyr.scaling_vmax,
            )
            out.append(replace(band, kind="density", mapping=m,
                               peak_density=peak))
        return tuple(out)

    def _shade_for_export(self, viewport=None) -> Optional[np.ndarray]:
        """Re-composite from cached DataFrames at *viewport*; no re-query.

        _shade_all_layers has side effects -- it rebuilds self._layer_aggs
        and self._layer_skip_reason -- and _handle_probe indexes those
        aggs with coordinates derived from whatever viewport the *browser*
        is showing.  An export at any other viewport would therefore
        silently corrupt the next hover, which is defect (2) in the probe
        notes above arriving by a new route.  Snapshot and restore rather
        than relying on the export viewport happening to match.
        """
        saved_aggs    = self._layer_aggs
        saved_reasons = self._layer_skip_reason
        try:
            if viewport is None:
                if self._current_viewport is not None:
                    vx0, vx1, vy0, vy1 = self._current_viewport
                    xr, yr = (vx0, vx1), (vy0, vy1)
                else:
                    xr, yr = self._x_range, self._y_range
            else:
                x0, x1, y0, y1 = viewport
                xr, yr = (x0, x1), (y0, y1)
            return self._shade_all_layers(xr, yr)
        finally:
            self._layer_aggs        = saved_aggs
            self._layer_skip_reason = saved_reasons

    def _build_glyphs(self) -> None:
        """Add the single composite image_rgba glyph."""
        self._fig.image_rgba(
            source = self._image_source,
            image  = "image",
            x = "x", y = "y", dw = "dw", dh = "dh",
        )

    def _render(self, selection: "SelectionSpec", defer: bool = False, **kwargs) -> None:
        """Query all layers and push the composite image.

        Parameters
        ----------
        defer : bool
            If ``True``, skip the backend query entirely and leave all
            layers empty with the same placeholder ``(0.0, 1.0)`` ranges
            ``_query_all_layers`` already uses for genuinely empty data —
            same purpose and pattern as ``VisibilityRaster._render``'s
            ``defer``; see decision 11 in the grid/iteration design notes.
        """
        # Re-resolve axis labels before anything reads them: this is the
        # one place that knows both the current axes and the current
        # selection, and every axis- or selection-changing path funnels
        # through it.  Resolving here rather than in _panel_spec()
        # matters -- the backend counts partitions to decide whether
        # Axis.CHANNEL is unique, and _panel_spec() runs on every push.
        self._refresh_axis_info(selection)
        t0 = time.perf_counter()
        if defer:
            self._layer_dfs  = [None] * len(self._layers)
            self._layer_aggs = [None] * len(self._layers)
            self._layer_skip_reason = (
                ["deferred (never rendered)"] * len(self._layers)
            )
            self._layer_extents = [None] * len(self._layers)
            self._x_range    = (0.0, 1.0)
            self._y_range    = (0.0, 1.0)
        else:
            self._query_all_layers(selection)
        self._current_viewport = None   # reset — new data covers full range
        self._composite_and_push()
        log.debug("VisibilityScatter._render: %.3fs", time.perf_counter() - t0)
        self._update_state_source()

    def _do_viewport_rerender(
        self, x0: float, x1: float, y0: float, y1: float
    ) -> dict:
        """Re-composite cached DataFrames over the new viewport."""
        # Normalise — Bokeh box-zoom can produce start > end
        x0, x1 = min(x0, x1), max(x0, x1)
        y0, y1 = min(y0, y1), max(y0, y1)
        self._current_viewport = (x0, x1, y0, y1)
        img32 = self._shade_all_layers(x_range=(x0, x1), y_range=(y0, y1))
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
        """Probe every visible layer at the hover coordinates."""
        x = float(message.get("x", 0.0))
        y = float(message.get("y", 0.0))

        # Range-check against what is actually on screen.  The old code
        # checked the *full* data extent, so after a zoom a hover well
        # outside the visible region still passed and then got clamped
        # by argmin onto an edge bin, reporting that edge bin's data as
        # though it were under the cursor.
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

        # Gather a candidate from every layer that is currently drawn.
        # (distance_in_screen_px, layer_index, px, py)
        candidates: list[tuple[float, int, int, int]] = []
        fallback:   Optional[tuple[int, int, int]] = None   # (layer, px, py)
        debug_rows: list[str] = []
        radius = 0

        for i, (agg, df, lyr) in enumerate(
            zip(self._layer_aggs, self._layer_dfs, self._layers)
        ):
            if agg is None or df is None or len(df) == 0:
                reason = (self._layer_skip_reason[i]
                          if i < len(self._layer_skip_reason) else None)
                ext = (self._layer_extents[i]
                       if i < len(self._layer_extents) else None)
                extent_s = ""
                if ext is not None:
                    _, xmin, xmax, ymin, ymax = ext
                    extent_s = (f" x=[{xmin:.6g},{xmax:.6g}]"
                                f" y=[{ymin:.6g},{ymax:.6g}]")
                debug_rows.append(
                    f"L{i}[{lyr.label}]:skip({reason or 'no agg/df'})"
                    f"{extent_s}"
                )
                continue
            idx = self._agg_pixel(agg, x, y)
            if idx is None:
                debug_rows.append(f"L{i}:skip(no coords)")
                continue
            px, py = idx
            if fallback is None:
                fallback = (i, px, py)
            bin_w, bin_h = self._bin_screen_size(agg)
            radius = self._search_radius_bins(agg)
            hit = self._nearest_populated_bin(
                agg, px, py, radius, bin_w, bin_h
            )
            if hit is None:
                debug_rows.append(
                    f"L{i}:miss@({px},{py}) shape={agg.shape} "
                    f"bin={bin_w:.1f}x{bin_h:.1f}px r={radius}"
                )
                continue
            dist, hpx, hpy = hit
            candidates.append((dist, i, hpx, hpy))
            # Log the hovered bin as well as the matched one; when they
            # differ, that difference is the whole story.
            debug_rows.append(
                f"L{i}[{lyr.label}]:hit@({hpx},{hpy}) from({px},{py}) "
                f"d={dist:.1f}px "
                f"shape={agg.shape} bin={bin_w:.1f}x{bin_h:.1f}px r={radius}"
            )

        if self._probe_debug:
            log.info(
                "[probe] x=%.9g y=%.9g viewport=(%.6g,%.6g,%.6g,%.6g) | %s",
                x, y, vx0, vx1, vy0, vy1, "  ".join(debug_rows),
            )

        if not candidates and fallback is None:
            return self._probe_envelope(
                "no_data", "<i>no data</i>", x=x, y=y, layers=[],
            )

        # Prefer an exact-bin hit; among equals prefer the lowest layer
        # index so the answer is stable as the cursor moves.
        if candidates:
            dist, layer_i, px, py = min(candidates, key=lambda c: (c[0], c[1]))
            exact = dist == 0.0
        else:
            layer_i, px, py = fallback          # type: ignore[misc]
            dist, exact     = 0.0, True

        lyr = self._layers[layer_i]
        df  = self._layer_dfs[layer_i]
        agg = self._layer_aggs[layer_i]
        try:
            info = self._backend.probe_scatter_pixel(
                agg, px, py, self._selection, df
            )
        except Exception as exc:
            log.warning(
                "probe_scatter_pixel layer %d at (%d,%d) failed: %s",
                layer_i, px, py, exc,
            )
            return self._probe_envelope(
                "error",
                f"<span style='color:#f38ba8'>probe error: {exc}</span>",
                x=x, y=y, layers=[], error=str(exc),
            )

        # Report *every* layer, not just the one that answered.
        #
        # A status bar showing only the winning layer cannot distinguish
        # "XX has no data here" from "you weren't told about XX", and
        # that distinction is the whole question when judging whether an
        # outlier is single-polarization or common to both.  An explicit
        # em dash for a layer with nothing at this location carries real
        # information; silence carries none.
        #
        # Values for the non-winning layers are read straight from their
        # aggs rather than by calling probe_scatter_pixel again: the
        # backend recounts samples against the full DataFrame, which is
        # a scan of >1e6 rows per call, and hover fires many times a
        # second.  The sample count N therefore describes the winning
        # layer only, which is why it stays adjacent to that layer's
        # reading in the field order below.
        # Built as records first, rendered to HTML second.  The two used
        # to happen in one pass, which meant the only machine-readable
        # form of "did this layer have data" was the presence of an em
        # dash in a markup string — see _probe_envelope for why that is a
        # bad thing for a test to depend on.  One entry per layer
        # regardless of visibility, so field order is stable for callers;
        # the HTML pass below is what filters hidden layers out.
        hits = {i: (d, hx, hy) for d, i, hx, hy in candidates}
        layer_results: list[dict] = []
        for i, other in enumerate(self._layers):
            entry = {
                "index":       i,
                "label":       other.label,
                "visible":     other.alpha > 0.0,
                "value":       None,
                "distance_px": None,
                "skip_reason": (self._layer_skip_reason[i]
                                if i < len(self._layer_skip_reason) else None),
            }
            hit = hits.get(i)
            if entry["visible"] and hit is not None:
                d_i, hx, hy = hit
                # Routing a non-finite value through _json_num is the one
                # deliberate label change in this refactor: the old loop
                # rendered a NaN bin as the literal text "nan", which
                # reads as a value in the status bar when it means the
                # opposite.  Unreachable in normal flow — _populated_mask
                # tests np.isfinite for float aggs, so
                # _nearest_populated_bin never returns a NaN bin — so
                # this only fires if mask and agg ever disagree, and an
                # em dash is the honest answer when they do.  Every other
                # input produces byte-identical HTML to the old loop.
                try:
                    val = _json_num(self._layer_aggs[i].values[hy, hx])
                except (IndexError, TypeError, ValueError):
                    val = None
                if val is not None:
                    entry["value"]       = val
                    entry["distance_px"] = float(d_i)
            layer_results.append(entry)

        readings = [
            self._layer_reading_html(e) for e in layer_results if e["visible"]
        ]

        # Everything after the winning layer's own value: the axis
        # coordinates, the sample count, and any backend extras.
        _, _, remainder = self._format_probe(
            info, lyr.label
        ).partition(self._PROBE_SEP)

        parts = readings + ([remainder] if remainder else [])
        label = self._PROBE_SEP.join(parts)
        if not exact:
            label += f"{self._PROBE_SEP}<i>nearest ({dist:.0f} px away)</i>"

        # Only known-safe scalars are promoted out of `info`; the raw
        # backend dict also holds tuples, numpy types, and the
        # field_names/antenna_pairs lists, none of which have ever
        # crossed the wire.  Widen this deliberately, not by splatting.
        return self._probe_envelope(
            "ok", label,
            x = x, y = y,
            winner      = int(layer_i),
            exact       = bool(exact),
            distance_px = float(dist),
            layers      = layer_results,
            n_samples   = _json_num(info.get("n_scatter_samples")),
            x_centre    = _json_num(info.get("x_centre")),
            y_centre    = _json_num(info.get("y_centre")),
        )

    @staticmethod
    def _layer_reading_html(entry: dict) -> str:
        """Render one layer record from ``layer_results`` as a status cell.

        A pure function of the record, so the status-bar wording can
        change without touching probe logic.  The em dash means
        "consulted, nothing here" — see the reasoning above the readings
        loop for why silence is not an acceptable substitute.  Hidden
        layers are filtered out before this is called: a hidden layer was
        never consulted, so any reading for it would be a lie.
        """
        if entry["value"] is None:
            return f"<b>{entry['label']}:</b> <i>&mdash;</i>"
        cell = f"<b>{entry['label']}:</b> {entry['value']:.6g}"
        if entry["distance_px"]:   # falsy covers both None and an exact hit
            cell += f" <i>(~{entry['distance_px']:.0f}px)</i>"
        return cell

    def _register_extra_comm_handlers(self) -> None:
        self._comm.register(self._msg_set_alpha,   self._handle_set_alpha)
        self._comm.register(self._msg_color_mode,  self._handle_set_color_mode)
        self._comm.register(self._msg_update_axes, self._handle_update_axes_scatter)
        self._comm.register(self._msg_update_scaling, self._handle_update_scaling)

    # ------------------------------------------------------------------
    # Scatter-specific internals
    # ------------------------------------------------------------------

    def _query_all_layers(self, selection: "SelectionSpec") -> None:
        """Query backend for all layers; update _layer_dfs."""
        # Build combined y-axes list for a single query_columns call
        y_axes = [(lyr.y_axis, lyr.polarization) for lyr in self._layers]
        result = self._backend.query_columns(
            self._x_dim, y_axes, selection
        )

        # A layer whose key is absent from the result is a different
        # failure from one whose frame is empty, and both are different
        # from a frame that arrived holding the wrong quantity.  Record
        # enough to tell them apart: the requested key, whether the
        # backend returned it, and the frame's actual data extent.
        missing = [
            (lyr.y_axis, lyr.polarization)
            for lyr in self._layers
            if (lyr.y_axis, lyr.polarization) not in result
        ]
        if missing:
            log.warning(
                "query_columns returned no frame for %d of %d requested "
                "keys: %s (requested %s, got %s)",
                len(missing), len(self._layers), missing,
                y_axes, list(result.keys()),
            )
        if len(set(y_axes)) != len(y_axes):
            # y_axes is a list but the backend keys its result by these
            # pairs, so duplicates silently collapse and two layers end
            # up sharing one frame.
            log.warning(
                "duplicate (y_axis, polarization) across layers: %s — "
                "layers sharing a key will share a DataFrame", y_axes,
            )

        x0_all, x1_all, y0_all, y1_all = [], [], [], []
        self._layer_extents = [None] * len(self._layers)
        for i, lyr in enumerate(self._layers):
            key = (lyr.y_axis, lyr.polarization)
            df  = result.get(key)
            self._layer_dfs[i] = df
            if df is not None and len(df) > 0:
                xmin, xmax = float(df["x"].min()), float(df["x"].max())
                ymin, ymax = float(df["y"].min()), float(df["y"].max())
                self._layer_extents[i] = (len(df), xmin, xmax, ymin, ymax)
                x0_all.append(xmin)
                x1_all.append(xmax)
                y0_all.append(ymin)
                y1_all.append(ymax)

        if self._probe_debug:
            for i, (lyr, ext) in enumerate(
                zip(self._layers, self._layer_extents)
            ):
                if ext is None:
                    log.info("[query] L%d[%s]: no rows", i, lyr.label)
                    continue
                n, xmin, xmax, ymin, ymax = ext
                log.info(
                    "[query] L%d[%s]: n=%d x=[%.6g, %.6g] y=[%.6g, %.6g]",
                    i, lyr.label, n, xmin, xmax, ymin, ymax,
                )

        if x0_all:
            self._x_range = (min(x0_all), max(x1_all))
            self._y_range = (min(y0_all), max(y1_all))
        else:
            self._x_range = (0.0, 1.0)
            self._y_range = (0.0, 1.0)

    def _compute_canvas_size(
        self, x0: float, x1: float, y0: float, y1: float,
    ) -> tuple[int, int]:
        """Canvas pixel dimensions used to render the given range.

        Normally just ``(self._width, self._height)``, but for sparse
        data (few points relative to canvas area) a smaller canvas is
        used to visually boost apparent point density — see the
        ``pts_per_px`` scaling below. Factored out of ``_shade_all_layers``
        so ``_panel_spec`` can also compute this for the *full*
        data extent (see ``agg_n_x``/``agg_n_y`` there), independent of
        whatever sub-range is currently in view.
        """
        total_in_view = sum(
            int(((df["x"] >= x0) & (df["x"] <= x1) &
                 (df["y"] >= y0) & (df["y"] <= y1)).sum())
            for lyr, df in zip(self._layers, self._layer_dfs)
            if df is not None and len(df) > 0 and lyr.alpha > 0.0
        )
        pts_per_px = total_in_view / (self._width * self._height)
        if pts_per_px < 0.01 and total_in_view > 0:
            scale = max(0.05, math.sqrt(
                total_in_view / (self._width * self._height * 0.01)
            ))
            shared_w = max(10, int(self._width  * scale))
            shared_h = max(10, int(self._height * scale))
        else:
            shared_w, shared_h = self._width, self._height
        return shared_w, shared_h

    def _shade_all_layers(
        self,
        x_range: Optional[tuple[float, float]] = None,
        y_range: Optional[tuple[float, float]] = None,
    ) -> np.ndarray:
        """Run cvs.points + shade for each layer; stack and return uint32.

        Parameters
        ----------
        x_range, y_range :
            Viewport extents.  ``None`` → use full data range.
        """
        xr = x_range or self._x_range
        yr = y_range or self._y_range
        # Normalise so x0 < x1 and y0 < y1 — Bokeh's box-zoom can produce
        # inverted ranges when the figure y-axis is flipped for image display.
        x0, x1 = (min(xr), max(xr))
        y0, y1 = (min(yr), max(yr))

        # Invalidate every cached agg up front.  Only layers that are
        # actually shaded below re-populate their slot, so a layer that
        # is skipped this pass (alpha == 0, no points in view, or a
        # shade exception) can no longer leave behind an agg built for a
        # *previous* viewport — which _handle_probe would then index
        # with current-viewport pixel coordinates.  See defect (2) in
        # the probe notes above.
        self._layer_aggs = [None] * len(self._layers)

        if x0 == x1 or y0 == y1:
            return np.zeros((self._height, self._width), dtype=np.uint32)

        # Global amplitude scale — anchor span to the full data y_range so
        # the same amplitude value always maps to the same color regardless
        # of zoom level.  This makes it possible to visually track features
        # across pan/zoom without the color shifting under the user's feet.
        span_arg = None
        if self._color_mode == "global":
            global_y0, global_y1 = self._y_range
            span_arg = [global_y0, global_y1]

        # Compute total points in viewport across all layers to determine
        # a single canvas size shared by all layers — this ensures the
        # Porter-Duff compositing loop always gets arrays of the same shape.
        shared_w, shared_h = self._compute_canvas_size(x0, x1, y0, y1)

        # Why each layer did or did not contribute to this composite.
        # Consumed by _handle_probe so a hover that finds no data can
        # say which of the several quite different causes applies --
        # "never queried", "query came back empty", "hidden by the user"
        # and "no samples in this viewport" are not the same event, and
        # only the last is routine.
        self._layer_skip_reason = [None] * len(self._layers)

        shaded_images = []
        for i, (lyr, df) in enumerate(zip(self._layers, self._layer_dfs)):
            if df is None:
                self._layer_skip_reason[i] = "not queried"
                continue
            if len(df) == 0:
                self._layer_skip_reason[i] = "query returned 0 rows"
                continue
            if lyr.alpha == 0.0:
                self._layer_skip_reason[i] = "hidden (alpha=0)"
                continue
            try:
                # Count points in viewport for this layer's auto-alpha.
                n_in_view = int(
                    ((df["x"] >= x0) & (df["x"] <= x1) &
                     (df["y"] >= y0) & (df["y"] <= y1)).sum()
                )
                if n_in_view == 0:
                    self._layer_skip_reason[i] = (
                        f"0 of {len(df)} samples in viewport"
                    )
                    continue

                cvs_layer = ds.Canvas(
                    plot_width  = shared_w,
                    plot_height = shared_h,
                    x_range     = (x0, x1),
                    y_range     = (y0, y1),
                )
                agg = cvs_layer.points(df, "x", "y", ds_agg.mean("y"))
                self._layer_aggs[i] = agg

                # Auto-scale opacity by overplot ratio
                # Use shared canvas pixels so alpha is consistent across layers
                canvas_pixels = shared_w * shared_h
                ratio        = max(1.0, n_in_view / canvas_pixels)
                auto_alpha   = int(255.0 / math.log1p(ratio))
                auto_alpha   = max(80, min(255, auto_alpha))
                layer_alpha  = max(0, min(255, int(auto_alpha * lyr.alpha)))

                # Color mapping controlled by color_mode:
                #   "global" → span=[full_y0, full_y1]
                #       Stable colors across all zoom levels — a 50 Jy point
                #       always maps to the same color.  Best for flagging.
                #   "local" → span=[viewport_y_min, viewport_y_max]
                #       Full palette spans whatever amplitudes are visible
                #       in the current viewport.  Colors change on zoom
                #       (expected).  Best for exploring structure.
                #
                # Within either color_mode, lyr.scaling selects the actual
                # value-to-color transform (Phase 0 CM-1).  "eq_hist" is
                # the default — see colormap_scaling module docstring for
                # why linear scaling saturates on real visibility data.
                #
                # Unlike VisibilityRaster (where the y-axis is often a
                # different quantity than the rendered color, e.g. TIME
                # vs AMPLITUDE), here the y-axis IS the rendered quantity,
                # so df["y"] directly gives the reference value array
                # needed for "global" eq_hist equalization — no separate
                # full-data cache is needed.
                if self._color_mode == "local":
                    visible_y = df.loc[
                        (df["x"] >= x0) & (df["x"] <= x1) &
                        (df["y"] >= y0) & (df["y"] <= y1),
                        "y"
                    ]
                    if len(visible_y) > 0:
                        span = [float(visible_y.min()), float(visible_y.max())]
                        eq_hist_reference = visible_y.to_numpy()
                    else:
                        span = [float(self._y_range[0]), float(self._y_range[1])]
                        eq_hist_reference = None
                else:  # "global"
                    span = span_arg
                    eq_hist_reference = df["y"].to_numpy()

                # Manual override (colormap_controls' min/max fields)
                # takes precedence over the automatic global/local span
                # above, in either color_mode. Applied to DATASHADER_HOW
                # scalings via `span` here; eq_hist gets an equivalent
                # clip applied directly to its inputs below instead,
                # since it doesn't take a span= (see VisibilityRaster.
                # _shade_agg's eq_hist branch for the identical
                # reasoning).
                if lyr.scaling_vmin is not None and lyr.scaling_vmax is not None:
                    span = [lyr.scaling_vmin, lyr.scaling_vmax]

                if lyr.scaling in _cms.DATASHADER_HOW:
                    shade_kwargs = dict(
                        cmap=lyr.cmap,
                        how=_cms.DATASHADER_HOW[lyr.scaling],
                        min_alpha=_MIN_ALPHA,
                    )
                    if span is not None:
                        shade_kwargs["span"] = span
                    img = tf.shade(agg, **shade_kwargs)
                elif lyr.scaling == "eq_hist":
                    eq_reference = eq_hist_reference
                    # See VisibilityRaster._shade_agg's eq_hist branch
                    # for the full rationale: restricting the reference
                    # population (not clipping agg.values in place) is
                    # what actually concentrates color resolution into
                    # the selected range, verified empirically.
                    if lyr.scaling_vmin is not None or lyr.scaling_vmax is not None:
                        pool = eq_reference if eq_reference is not None else agg.values
                        pool_finite = pool[np.isfinite(pool)]
                        lo = lyr.scaling_vmin if lyr.scaling_vmin is not None else (
                            float(pool_finite.min()) if pool_finite.size else None)
                        hi = lyr.scaling_vmax if lyr.scaling_vmax is not None else (
                            float(pool_finite.max()) if pool_finite.size else None)
                        if lo is not None and hi is not None and hi > lo:
                            in_band = pool_finite[(pool_finite >= lo) & (pool_finite <= hi)]
                            if in_band.size > 0:
                                eq_reference = in_band
                    transformed = _cms.equalize_histogram(
                        agg.values, reference=eq_reference,
                    )
                    scaled_agg = agg.copy(data=transformed)
                    img = tf.shade(
                        scaled_agg, cmap=lyr.cmap, how="linear",
                        span=[0.0, 1.0], min_alpha=_MIN_ALPHA,
                    )
                else:
                    transformed = _cms.apply_explicit_scaling(
                        agg.values,
                        lyr.scaling,
                        alpha=lyr.scaling_alpha,
                        gamma=lyr.scaling_gamma,
                        vmin=span[0] if span is not None else None,
                        vmax=span[1] if span is not None else None,
                    )
                    scaled_agg = agg.copy(data=transformed)
                    img = tf.shade(
                        scaled_agg, cmap=lyr.cmap, how="linear",
                        span=[0.0, 1.0], min_alpha=_MIN_ALPHA,
                    )
                img_arr = np.array(img, dtype=np.uint32)
                if layer_alpha > 0:
                    nonempty = (img_arr >> 24) > 0
                    img_arr[nonempty] = (
                        (img_arr[nonempty] & 0x00FFFFFF)
                        | (np.uint32(layer_alpha) << np.uint32(24))
                    )
                shaded_images.append(img_arr)
            except Exception as exc:
                self._layer_skip_reason[i] = f"shade failed: {exc}"
                log.warning("shade layer %d failed: %s", i, exc)

        if not shaded_images:
            return np.zeros((self._height, self._width), dtype=np.uint32)

        if len(shaded_images) == 1:
            return shaded_images[0]

        # Porter-Duff "over" compositing in numpy on uint32 ARGB arrays.
        # For each pixel: result = src + dst * (1 - src_alpha/255).
        # This is equivalent to tf.stack(..., how="over") but works on
        # plain ndarray so we don't need Datashader Image objects.
        composite = shaded_images[0].copy()
        for layer_arr in shaded_images[1:]:
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

    def _composite_and_push(
        self,
        x_range: Optional[tuple[float, float]] = None,
        y_range: Optional[tuple[float, float]] = None,
    ) -> None:
        """Shade + stack all layers; push result into _image_source.

        Uses _current_viewport when set (i.e. user has panned/zoomed) so
        that set_alpha / set_color_mode re-composite over the correct region
        rather than the full data range.
        """
        if x_range is None and y_range is None and self._current_viewport:
            vx0, vx1, vy0, vy1 = self._current_viewport
            xr = (vx0, vx1)
            yr = (vy0, vy1)
        else:
            xr = x_range or self._x_range
            yr = y_range or self._y_range
        x0, x1 = xr
        y0, y1 = yr

        img32    = self._shade_all_layers(xr, yr)
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

    # ------------------------------------------------------------------
    # j2p handlers (scatter-specific)
    # ------------------------------------------------------------------

    def set_layer_cmaps(self, cmaps) -> None:
        """Swap every layer's colormap and re-composite from cache.

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
        """Re-composite the cached layer DataFrames; no backend query."""
        if not any(df is not None for df in self._layer_dfs):
            return
        if self._current_viewport is not None:
            x0, x1, y0, y1 = self._current_viewport
            self._composite_and_push((x0, x1), (y0, y1))
        else:
            self._composite_and_push(self._x_range, self._y_range)

    def set_color_mode(self, mode: str) -> None:
        """Toggle color mode and re-composite without re-querying the backend.

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
        self._composite_and_push()
        self._update_state_source()

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

    def _handle_set_color_mode(self, message: dict) -> dict:
        """Handle j2p message to toggle color mode: {mode: "global"|"local"}.

        Returns the new composite image so the JS callback can update
        image_source.data directly — Python-side model property changes
        don't propagate to the browser in static HTML mode.
        """
        mode = message.get("mode", "global")
        try:
            self.set_color_mode(mode)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        return self._image_response("ok", color_mode=self._color_mode)

    def _handle_set_alpha(self, message: dict) -> dict:
        """Handle j2p 'vs_set_alpha': {layer_index: int, alpha: float}."""
        idx   = int(message.get("layer_index", 0))
        alpha = float(message.get("alpha", 1.0))
        try:
            self.set_alpha(idx, alpha)
        except (IndexError, ValueError) as exc:
            return {"status": "error", "message": str(exc)}
        return self._image_response("ok")

    def _handle_update_scaling(self, message: dict) -> dict:
        """Handle j2p 'vs_update_scaling': {layer_index, scaling, alpha, gamma, vmin, vmax, reset_range}.

        All fields except layer_index are optional; omitted fields keep
        their current per-layer value. Uses _image_response so JS can
        update image_source directly, mirroring _handle_set_alpha.
        """
        idx = int(message.get("layer_index", 0))
        try:
            self.update_scaling(
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
        """
        out = []
        for i, lyr in enumerate(layers):
            if lyr.cmap is None:
                lyr = ScatterLayer(
                    y_axis        = lyr.y_axis,
                    polarization  = lyr.polarization,
                    cmap          = self._layer_cmaps[i % len(self._layer_cmaps)],
                    alpha         = lyr.alpha,
                    label         = lyr.label,
                    scaling       = lyr.scaling,
                    scaling_alpha = lyr.scaling_alpha,
                    scaling_gamma = lyr.scaling_gamma,
                    scaling_vmin  = lyr.scaling_vmin,
                    scaling_vmax  = lyr.scaling_vmax,
                )
            out.append(lyr)
        return out

    def _handle_update_axes_scatter(self, message: dict) -> dict:
        """Handle j2p 'vs_update_axes' with scatter-specific fields."""
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
                    )
                    for entry in message["layers"]
                ]
            except Exception as exc:
                log.warning("_handle_update_axes_scatter: bad layers: %s", exc)

        self.update_axes(
            x_dim  = self._parse_axis(message, "x_dim"),
            layers = new_layers,
            title  = message.get("title"),
        )
        return {"status": "ok"}
