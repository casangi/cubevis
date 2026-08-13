"""
visibility_raster.py
====================
Datashader-rendered raster plot for visibility data.

Derives from ``VisibilityPlot`` which owns all CommMgr/Comm wiring,
``_state_source`` management, figure construction, tick formatters, hover
tool, and pan/zoom rerender trigger.

Raster-specific additions
--------------------------
* ``query_raster`` backend call → 2D float64 agg
* Two-level pan/zoom: Level-1 Datashader ``Canvas.raster()`` resample
  (fast, no backend query) vs Level-2 backend re-query when zoomed past
  agg resolution and ``is_decimated=True``
* ``interpolate='nearest'`` when upsampling (canvas pixel < one agg cell)
  to show crisp block boundaries rather than bilinear blur
* ``_state_source`` extra fields: ``agg_n_x``, ``agg_n_y`` for the 1:1
  zoom button
* ``update_axes(quantity=, polarization=)`` extends the base signature
* ``_data_to_pixel()`` converts hover data-space coordinates to agg indices

Package location
----------------
``cubevis/cubevis/toolbox/visplot/visibility_raster.py``
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional, TYPE_CHECKING
from uuid import uuid4

import numpy as np

from bokeh.models import ColumnDataSource

from .visibility_plot import (
    VisibilityPlot, _img_to_uint32, _axis_label, _json_num,
)
from .panel_spec import ColorBand, PanelSpec
from . import colormap_scaling as _cms

if TYPE_CHECKING:
    import xarray as xr
    from .visibility_reader import VisibilityReader
    from .selection import SelectionSpec
    from .axes import Axis

log = logging.getLogger(__name__)

try:
    import datashader as ds
    import datashader.transfer_functions as tf
    HAS_DATASHADER = True
except ImportError:
    HAS_DATASHADER = False

_DEFAULT_CMAP = [
    "#0d0887", "#46039f", "#7201a8", "#9c179e", "#bd3786",
    "#d8576b", "#ed7953", "#fb9f3a", "#fdcb26", "#f0f921",
]

_DEFAULT_SCALING = "eq_hist"


def _auto_title(quantity: "Axis", y_dim: "Axis", x_dim: "Axis",
                polarization: str) -> str:
    return (
        f"{quantity.label}  "
        f"[{y_dim.label} vs {x_dim.label}]"
        f"  pol={polarization}"
    )


class VisibilityRaster(VisibilityPlot):
    """Datashader raster plot for a single visibility quantity.

    Parameters
    ----------
    backend : VisibilityReader
        Opened reader (``LocalVisibilityReader`` wrapping an
        ``MSv2Backend`` or ``MSv4Backend``, or a
        ``RemoteReductionContext`` for remote sessions).
    selection : SelectionSpec
        Data selection.
    y_dim : Axis
        Native axis for the raster y dimension (rows).
    x_dim : Axis
        Native axis for the raster x dimension (columns).
    quantity : Axis
        Derived quantity rendered as color (AMPLITUDE, PHASE, etc.).
    polarization : str
        Correlation product label, e.g. ``"XX"``.
    width, height : int
        Canvas dimensions in pixels.
    title : str | None
        Figure title; ``None`` → auto-generated.
    comm_mgr :
        ``CommMgr`` from the active ``BokehAppContext``.
    cmap : list[str] | None
        Color map hex strings.  Defaults to Plasma.
    max_cells : int
        Agg cell budget for ``query_raster`` decimation.
    scaling : str
        Value-to-color transfer function.  One of
        ``colormap_scaling.ALL_SCALINGS``:
        ``"linear"``, ``"log"``, ``"eq_hist"`` (default), ``"sqrt"``,
        ``"square"``, ``"gamma"``, ``"power"``.  ``"eq_hist"``
        (histogram equalization) is the default because linear scaling
        saturates badly on real visibility data — a small high-amplitude
        population dominates the colormap while the populous
        low-amplitude region collapses to a featureless gradient.
    scaling_alpha : float
        Parameter for ``"log"`` and ``"power"`` scalings.
    scaling_gamma : float
        Parameter for ``"gamma"`` scaling.
    """

    def __init__(
        self,
        backend: "VisibilityReader",
        selection: "SelectionSpec",
        y_dim: "Axis",
        x_dim: "Axis",
        quantity: "Axis",
        polarization: str = "XX",
        width: int  = 900,
        height: int = 600,
        title: Optional[str] = None,
        comm_mgr=None,
        cmap: Optional[list] = None,
        max_cells: int = 2_000_000,
        color_mode: str = "global",
        scaling: str = _DEFAULT_SCALING,
        scaling_alpha: float = 10.0,
        scaling_gamma: float = 1.0,
        raster_interpolate: str = "auto",
        probe_debug: bool = False,
        **kwargs,
    ) -> None:
        self._quantity     = quantity
        self._polarization = polarization
        self._cmap         = cmap or _DEFAULT_CMAP
        self._max_cells    = max_cells
        self._color_mode   = color_mode
        self._scaling       = scaling if scaling in _cms.ALL_SCALINGS else _DEFAULT_SCALING
        self._scaling_alpha = scaling_alpha
        self._scaling_gamma = scaling_gamma
        self._scaling_vmin: Optional[float] = None  # manual override; None = auto
        self._scaling_vmax: Optional[float] = None  # (see update_scaling, _shade_agg)

        # Raster-specific state (set by _render)
        self._agg:          Optional["xr.DataArray"] = None
        self._is_decimated: bool                     = False

        # Upsample method for Canvas.raster().  "auto" (default) picks
        # "nearest" whenever either axis is upsampling and "linear"
        # otherwise -- see _resample_method for why linear is wrong on
        # a categorical baseline axis and a gapped time axis.  Force
        # "nearest" or "linear" to override; "nearest" unconditionally
        # is defensible for this display class and is the setting to
        # use when comparing against a reference tool that does no
        # interpolation of its own.
        if raster_interpolate not in ("auto", "nearest", "linear"):
            raise ValueError(
                f"raster_interpolate must be 'auto', 'nearest', or "
                f"'linear'; got {raster_interpolate!r}"
            )
        self._raster_interpolate: str = raster_interpolate

        # Log one INFO line per hover with the resolved grid indices,
        # agg shape, and the cell window the backend derived.  Also
        # honours VISPLOT_PROBE_DEBUG so it can be enabled without
        # editing a notebook cell.  Mirrors VisibilityScatter.
        self._probe_debug: bool = bool(probe_debug) or bool(
            os.environ.get("VISPLOT_PROBE_DEBUG")
        )

        # Per-instance uuid for the update_axes message
        self._msg_update_axes = str(uuid4())
        self._msg_color_mode  = str(uuid4())
        self._msg_update_scaling = str(uuid4())

        super().__init__(
            backend   = backend,
            selection = selection,
            y_dim     = y_dim,
            x_dim     = x_dim,
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
    def agg(self) -> Optional["xr.DataArray"]:
        """Cached float64 2D DataArray from the last ``query_raster`` call."""
        return self._agg

    def update_axes(
        self,
        y_dim: Optional["Axis"]  = None,
        x_dim: Optional["Axis"]  = None,
        quantity: Optional["Axis"] = None,
        polarization: Optional[str] = None,
        title: Optional[str] = None,
    ) -> None:
        """Change axes, quantity, or polarization and re-render in place.

        Extends the base ``update_axes`` with ``quantity`` and
        ``polarization`` parameters specific to raster mode.
        """
        changed = False
        if quantity is not None and quantity != self._quantity:
            self._quantity = quantity;  changed = True
        if polarization is not None and polarization != self._polarization:
            self._polarization = polarization;  changed = True

        # A never-yet-rendered (defer_initial_render=True) panel must
        # render on its first update_axes() call regardless of what else
        # changed — see the same guard and rationale in the base class's
        # update_axes(). Without it, activating a deferred panel by
        # calling update_axes() with no args, or with only unrelated
        # kwargs, would silently stay blank.
        if self._agg is None:
            changed = True

        # Delegate y_dim / x_dim / title changes to base; it calls _render
        # and _notify_axes_changed.  If only quantity/polarization changed
        # (or the panel has never rendered) we must trigger those ourselves.
        if y_dim is not None or x_dim is not None or title is not None or changed:
            if y_dim is not None and y_dim != self._y_dim:
                self._y_dim = y_dim
            if x_dim is not None and x_dim != self._x_dim:
                self._x_dim = x_dim
            if title is not None:
                self._title = title
            self._render(self._selection)
            self._notify_axes_changed()

    # ------------------------------------------------------------------
    # VisibilityPlot abstract interface
    # ------------------------------------------------------------------

    def _comm_description(self) -> str:
        return "visibility raster"

    def _effective_title(self) -> str:
        return self._title or _auto_title(
            self._quantity, self._y_dim, self._x_dim, self._polarization
        )

    def set_color_mode(self, mode: str) -> None:
        """Toggle color mode and re-render.

        Parameters
        ----------
        mode : ``"global"`` | ``"local"``
            ``"global"`` — value-domain anchor = full cached agg's value
            range; colors stable on zoom.
            ``"local"`` — value-domain anchor = current viewport crop's
            value range; reveals detail.
        """
        if mode not in ("global", "local"):
            raise ValueError(f"color_mode must be 'global' or 'local', got {mode!r}")
        self._color_mode = mode
        self._render(self._selection)

    def update_scaling(
        self,
        scaling: Optional[str] = None,
        alpha: Optional[float] = None,
        gamma: Optional[float] = None,
        vmin: Optional[float] = None,
        vmax: Optional[float] = None,
        reset_range: bool = False,
    ) -> None:
        """Change the value-to-color transfer function and re-shade.

        Does NOT re-query the backend — operates on the cached ``agg``
        array, mirroring the fast re-composite pattern used by
        ``VisibilityScatter.set_alpha()``.

        Parameters
        ----------
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
            ``color_mode``-based range (see ``_shade_agg``) once set.
            ``None`` keeps the current value — matching ``alpha``/
            ``gamma``'s convention, this does NOT clear a
            previously-set override on its own; use ``reset_range=True``
            for that (``colormap_controls()``'s reset button). Clips the
            input (rather than setting a Datashader ``span=``) for
            ``"eq_hist"`` scaling — see ``_shade_agg``.
        reset_range : bool
            If ``True``, clears both ``vmin`` and ``vmax`` back to
            ``None`` (automatic ranging resumes), applied before any
            ``vmin``/``vmax`` passed in the same call — in practice
            they're never passed together (the reset button sends only
            ``reset_range``), but this ordering means an explicit
            ``vmin``/``vmax`` would still win if it somehow were.
        """
        if scaling is not None:
            if scaling not in _cms.ALL_SCALINGS:
                raise ValueError(
                    f"scaling must be one of {_cms.ALL_SCALINGS}, got {scaling!r}"
                )
            self._scaling = scaling
        if alpha is not None:
            self._scaling_alpha = alpha
        if gamma is not None:
            self._scaling_gamma = gamma
        if reset_range:
            self._scaling_vmin = None
            self._scaling_vmax = None
        if vmin is not None:
            self._scaling_vmin = vmin
        if vmax is not None:
            self._scaling_vmax = vmax

        if self._agg is None:
            return  # nothing rendered yet — new scaling takes effect on next _render

        img32 = self._shade_viewport(self._x_range, self._y_range)
        new_data = {
            "image": [img32],
            "x":     [self._x_range[0]],
            "y":     [self._y_range[0]],
            "dw":    [self._x_range[1] - self._x_range[0]],
            "dh":    [self._y_range[1] - self._y_range[0]],
        }
        if self._image_source is None:
            self._image_source = ColumnDataSource(data=new_data)
        else:
            self._image_source.data = new_data
        self._update_state_source()

    def histogram(self, bins: int = 254) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(counts, bin_edges)`` for the current ``agg`` values.

        Used by ``colormap_controls()`` for an eventual histogram display
        alongside the scaling controls.  Returns empty arrays if nothing
        has been rendered yet.
        """
        if self._agg is None:
            return np.array([]), np.array([])
        finite = self._agg.values[np.isfinite(self._agg.values)]
        if finite.size == 0:
            return np.array([]), np.array([])
        counts, edges = np.histogram(finite, bins=bins)
        return counts, edges

    def colormap_controls(self):
        """Return a Bokeh widget column for scaling / range controls.

        ``VisibilityPlotter``'s sidebar embeds this directly — see Phase 0
        CM-4 in the implementation plan.

        Every control is wired via ``CustomJS``/``js_on_change``/
        ``js_on_event`` calling ``comm.send(self._msg_update_scaling,
        ...)`` directly — the same pattern already used for hover/probe
        and pan-zoom rerender in ``VisibilityPlot._add_comm_hover``/
        ``_add_rerender_trigger``. This function used to wire the
        dropdown and alpha/gamma inputs via plain Bokeh
        ``widget.on_change()`` Python callbacks, which never fire
        without a Bokeh server (this deployment has none — see the
        testing handoff's core architectural constraint); min/max had
        no wiring attempted at all. Fixed here for all five controls.

        Layout, in order:
            - Scaling dropdown (linear, log, eq_hist, sqrt, square,
              gamma, power)
            - Equation label (``Div``, updates with scaling choice —
              client-side only, no round-trip, via a small static
              scaling->equation-string map passed in as CustomJS args)
            - Alpha numeric input (visible for log / power)
            - Gamma numeric input (visible for gamma)
            - Histogram of the current cached agg's values (via
              ``histogram()``), with a draggable ``EditSpan`` pair
              marking the min/max clip range, modeled on ``iclean``'s
              ``colormap_adjust()`` (``_cube.py``) — see that
              function's own docstring in this codebase's history for
              the reference design. Dragging fires a comm round-trip
              only on release (via the EditSpan ``dragging`` property,
              not the ``location`` change itself), not continuously, so
              dragging doesn't flood the comm channel; intermediate
              drag positions update only the paired numeric field,
              client-side.
            - Min / max numeric range inputs, kept in sync with the
              spans in both directions: dragging a span updates its
              paired field's displayed value; submitting a field moves
              its paired span. Either one submits the same
              ``vmin``/``vmax`` update.

        By design (per team decision), the histogram is built fresh
        each time this function is called and is NOT kept live during
        pan/zoom — it reflects ``self._agg``, the full cached
        aggregation, which itself only changes on a real re-query (a
        Plot/Reload press), not a viewport crop. Rebuilding it on every
        such press is a separate integration piece in
        ``VisibilityPlotter``'s Plot-button flow (``_handle_plot()`` /
        ``doPlot()``), not implemented here — see conversation history;
        this function always reflects whatever ``self._agg`` holds at
        the moment it's called.

        Text inputs fire on ``ValueSubmit`` (Enter / blur), not on
        every keystroke, to avoid a comm round-trip per character.

        The widget construction itself (Bokeh `Select`, `TextInput`,
        `Div`, and the `column`/`row` layout) is implemented inline here
        rather than factored further, since both display classes need an
        essentially identical control set and any divergence is better
        caught early than abstracted prematurely.
        """
        from bokeh.layouts import column, row
        from bokeh.models import Select, TextInput, Div, CustomJS, Button, BuiltinIcon, InlineStyleSheet, Spacer
        from bokeh.plotting import figure
        from bokeh.events import ValueSubmit
        from cubevis.bokeh.models._edit_span import EditSpan

        equation = Div(text=_cms.scaling_equation_label(self._scaling))
        scaling_select = Select(
            title="Color scaling",
            value=self._scaling,
            options=list(_cms.ALL_SCALINGS),
        )
        alpha_input = TextInput(
            title="alpha", value=str(self._scaling_alpha),
            visible=self._scaling in ("log", "power"),
        )
        gamma_input = TextInput(
            title="gamma", value=str(self._scaling_gamma),
            visible=self._scaling == "gamma",
        )

        # --- Histogram + draggable min/max span pair ------------------
        counts, edges = self.histogram()
        if counts.size == 0:
            # Nothing rendered yet -- placeholder range so the figure
            # and spans aren't degenerate; real data appears once
            # colormap_controls() is next called after a render.
            edges = np.array([0.0, 1.0])
            counts = np.array([0])
        hist_lo, hist_hi = float(edges[0]), float(edges[-1])
        min_loc = self._scaling_vmin if self._scaling_vmin is not None else hist_lo
        max_loc = self._scaling_vmax if self._scaling_vmax is not None else hist_hi

        hist_source = ColumnDataSource(data={
            "left":   edges[:-1],
            "right":  edges[1:],
            "top":    counts,
            "bottom": np.zeros_like(counts),
        })
        hist_fig = figure(
            height=100, width=260,
            # REVERTED: previously tried toolbar_location="right",
            # tools="pan,wheel_zoom,reset" here as a hypothesis for the
            # multi-click drag issue (modeled on iclean's histogram,
            # which has a full toolbar). Live testing showed this made
            # it WORSE (2 clicks -> 3), so reverted back to no toolbar.
            # The actual root cause turned out to be unrelated -- see
            # the EditSpan `dragging` property fix (edit_span.ts,
            # _edit_span.py) and its wiring below.
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
        # Dim background so the (usually white-by-default) histogram
        # doesn't glare against the app's dark theme, while staying
        # light enough that the blue bars stay legible against it.
        # Reuses the border_fill_color tone already established for the
        # main panel figures elsewhere (VisibilityPlotter, dark-mode
        # construction-time styling) for palette consistency. This is
        # just the initial (dark-mode-default) value, matching how the
        # main figures are styled dark by default without waiting for
        # the user to press the dark/light toggle -- VisibilityPlotter.
        # _style_cmap_column() collects this figure (and the reset
        # button below) so the toggle's dedicated cmap_figs/cmap_icons
        # JS block can re-color them on demand, using a deliberately
        # dimmer light-mode background than the main panels' stark
        # white for the same "keep the distribution legible" reason.
        hist_fig.background_fill_color = "#1e1e2e"
        hist_fig.border_fill_color     = "#1e1e2e"
        hist_fig.xaxis.axis_line_color        = "#45475a"
        hist_fig.xaxis.major_tick_line_color  = "#45475a"
        hist_fig.xaxis.minor_tick_line_color  = "#45475a"
        hist_fig.xaxis.major_label_text_color = "#cdd6f4"
        hist_fig.xgrid.grid_line_color        = "#45475a"
        hist_fig.xgrid.grid_line_alpha        = 0.3
        hist_fig.outline_line_color           = "#45475a"

        # line_width also sets the pan hit-test tolerance in Bokeh's
        # own SpanView (tolerance = max(2.5px, line_width/2)) -- tried
        # widening this to 8 (4px tolerance) as an experiment, per
        # earlier conversation history. Confirmed (live testing, then
        # explained by reading ui_events.ts's actual pan dispatch
        # algorithm) that this was the wrong direction: Bokeh's
        # UIEventBus.hit_test_renderers() collects EVERY renderer whose
        # interactive_hit() succeeds at the click point, and __trigger()
        # hands the gesture to the FIRST candidate (in reversed
        # add-order, i.e. most-recently-added first) whose
        # on_pan_start() succeeds -- there is no distance comparison at
        # all. Since max_span is added after min_span, it always wins
        # any tie when both spans' tolerance zones overlap at the click
        # point, regardless of which one is actually closer. Widening
        # line_width only makes that overlap MORE likely. Reverted to 2
        # (floored to the fixed 2.5px tolerance either way) -- see the
        # `sibling`-aware interactive_hit override in edit_span.ts for
        # the actual fix attempt.
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
        # See edit_span.ts's interactive_hit override / EditSpan.sibling
        # in _edit_span.py for the full rationale: without this,
        # whichever span is added last (max_span here) always wins any
        # click that falls within both spans' overlapping hit-test
        # tolerance zones, regardless of which one is actually closer.
        min_span.sibling = max_span
        max_span.sibling = min_span

        # Narrower than Bokeh's stretch-to-fill default -- these hold a
        # handful of digits, not a sentence.
        min_input = TextInput(title="min", value=f"{min_loc:.6g}", width=110)
        max_input = TextInput(title="max", value=f"{max_loc:.6g}", width=110)
        # Icon-only button using Bokeh's own built-in "reset" icon (the
        # same one the toolbar's ResetTool uses). Colors are NOT set
        # here beyond this initial dark-mode default -- VisibilityPlotter
        # ._style_cmap_column() PREPENDS a shared, toggle-managed
        # stylesheet ahead of this one (see that method's docstring),
        # so only non-color properties (padding) belong in this
        # button's own stylesheet, or they'd permanently override the
        # toggle's colors regardless of light/dark state.
        reset_button = Button(
            icon=BuiltinIcon(icon_name="reset", size="1.1em", color="#cdd6f4"),
            label="", width=36, height=36, button_type="default",
            stylesheets=[InlineStyleSheet(css="""
                :host(.bk-btn), .bk-btn {
                    padding: 2px;
                }
            """)],
        )
        # align="end" alone bottom-aligns the row's children, but the
        # button (no label above it) is intrinsically shorter than a
        # TextInput (title label + box), so bottom-aligning still
        # leaves the button's TOP sitting higher than the input boxes'
        # tops -- reads as "too high" even though the bottoms match.
        # A spacer approximating the label row's height fixes this by
        # giving the button the same total height budget. 19px is a
        # rough estimate of Bokeh's default TextInput title height +
        # margin; may need a pixel or two of adjustment once actually
        # rendered.
        reset_button_col = column(Spacer(height=19), reset_button)

        controls = column(
            scaling_select,
            equation,
            row(alpha_input, gamma_input),
            hist_fig,
            row(reset_button_col, min_input, max_input),
        )

        if self._comm is None:
            # No comm channel (e.g. static/offline construction) --
            # controls render but are inert, matching the rest of
            # VisibilityPlot's convention of gating comm-only wiring
            # behind `self._comm is not None` (see _add_rerender_trigger
            # and friends).
            return controls

        comm                = self._comm
        image_source        = self._image_source
        msg_update_scaling  = self._msg_update_scaling
        equations           = {s: _cms.scaling_equation_label(s) for s in _cms.ALL_SCALINGS}

        # Shared response handler: apply the returned image to
        # image_source, mirroring _add_rerender_trigger's callback body
        # exactly so both the pan/zoom path and this one keep the image
        # in sync the same way. Logs the response, matching doPlot()'s
        # own left-in diagnostic-logging convention (see testing
        # handoff) -- added specifically to make it possible to tell,
        # from the console, whether a min/max update actually reached
        # the server and came back with a changed image, or failed
        # silently somewhere, without more guessing from this end.
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
            },
            code=f"""
const s = cb_obj.value;
alpha_input.visible = (s === 'log' || s === 'power');
gamma_input.visible = (s === 'gamma');
equation.text = equations[s] || s;
console.log('[visplot colormap] sending:', {{scaling: s}});
comm.send('{msg_update_scaling}', {{scaling: s}}, function(resp) {{
{_apply_image_js}
}});
""",
        )
        scaling_select.js_on_change("value", scaling_js)

        def _numeric_submit_js(field_key: str) -> "CustomJS":
            """CustomJS for a TextInput whose ValueSubmit sends one
            numeric field (alpha/gamma) to update_scaling.
            Non-numeric input is silently ignored (matches the old
            Python callbacks' try/except ValueError: pass behaviour)."""
            return CustomJS(
                args={"comm": comm, "image_source": image_source},
                code=f"""
const v = parseFloat(cb_obj.value);
if (isNaN(v)) return;
console.log('[visplot colormap] sending:', {{{field_key}: v}});
comm.send('{msg_update_scaling}', {{{field_key}: v}}, function(resp) {{
{_apply_image_js}
}});
""",
            )

        alpha_input.js_on_event(ValueSubmit, _numeric_submit_js("alpha"))
        gamma_input.js_on_event(ValueSubmit, _numeric_submit_js("gamma"))

        # --- min/max <-> span bidirectional wiring ---------------------
        def _span_drag_visual_js(paired_input) -> "CustomJS":
            """Span 'location' change (fires continuously during drag):
            update the paired numeric field's displayed value only.
            No comm round-trip here -- see _span_release_js below.

            Guards against cb_obj.location being null/undefined: a
            "dead" first pan gesture (mousedown+release with no actual
            movement) still fires this callback but with location not
            yet set, and `undefined.toFixed()` throws uncaught -- see
            conversation history for the console-log evidence that
            diagnosed this."""
            return CustomJS(
                args={"paired_input": paired_input},
                code="""
if (cb_obj.location == null || isNaN(cb_obj.location)) return;
paired_input.value = cb_obj.location.toFixed(6);
""",
            )

        def _span_release_js(field_key: str, paired_input) -> "CustomJS":
            """Span 'dragging' property change (fires on both drag-start,
            dragging=True, and drag-end, dragging=False) -- acts only on
            the latter.

            Replaces a former LODEnd-based approach: LODEnd is a
            documented Plot-scoped event (bokeh.events.LODEnd ->
            PlotEvent) and never dispatched to any js_on_event listener
            when triggered from a non-Plot origin (this Span), confirmed
            by two rounds of live console output with zero LOD-related
            entries at all, whether listened for on the span or on the
            containing figure. `dragging` is a genuine EditSpan property
            (added to edit_span.ts/_edit_span.py) using the same
            property-change dispatch already proven reliable for
            `location` above."""
            return CustomJS(
                args={"comm": comm, "image_source": image_source,
                      "paired_input": paired_input},
                code=f"""
if (cb_obj.dragging) return;  // only act when the drag just ENDED
const v = cb_obj.location;
if (v == null || isNaN(v)) return;
paired_input.value = v.toFixed(6);
console.log('[visplot colormap] sending (span release):', {{{field_key}: v}});
comm.send('{msg_update_scaling}', {{{field_key}: v}}, function(resp) {{
{_apply_image_js}
}});
""",
            )

        def _range_submit_js(field_key: str, paired_span) -> "CustomJS":
            """Numeric field ValueSubmit: move the paired span visually,
            then send the same update_scaling round-trip as dragging.
            Unaffected by the LODEnd/dragging issue above -- ValueSubmit
            is a standard widget event, already confirmed reachable."""
            return CustomJS(
                args={"comm": comm, "image_source": image_source,
                      "paired_span": paired_span},
                code=f"""
const v = parseFloat(cb_obj.value);
if (isNaN(v)) return;
paired_span.location = v;
console.log('[visplot colormap] sending (field submit):', {{{field_key}: v}});
comm.send('{msg_update_scaling}', {{{field_key}: v}}, function(resp) {{
{_apply_image_js}
}});
""",
            )

        min_span.js_on_change("location", _span_drag_visual_js(min_input))
        max_span.js_on_change("location", _span_drag_visual_js(max_input))
        min_span.js_on_change("dragging", _span_release_js("vmin", min_input))
        max_span.js_on_change("dragging", _span_release_js("vmax", max_input))
        min_input.js_on_event(ValueSubmit, _range_submit_js("vmin", min_span))
        max_input.js_on_event(ValueSubmit, _range_submit_js("vmax", max_span))

        # Reset: move both spans/fields back to the histogram's own
        # edges, client-side, and tell the server to clear the override
        # entirely (self._scaling_vmin/_vmax back to None, not just set
        # to the full range -- see update_scaling(reset_range=)) so
        # subsequent auto (global/local) ranging resumes.
        reset_js = CustomJS(
            args={"comm": comm, "image_source": image_source,
                  "min_span": min_span, "max_span": max_span,
                  "min_input": min_input, "max_input": max_input,
                  "hist_lo": hist_lo, "hist_hi": hist_hi},
            code=f"""
min_span.location = hist_lo;
max_span.location = hist_hi;
min_input.value = hist_lo.toFixed(6);
max_input.value = hist_hi.toFixed(6);
console.log('[visplot colormap] sending (reset):', {{reset_range: true}});
comm.send('{msg_update_scaling}', {{reset_range: true}}, function(resp) {{
{_apply_image_js}
}});
""",
        )
        reset_button.js_on_click(reset_js)

        return controls

    def _panel_spec(self) -> PanelSpec:
        """Describe this raster: one colour band, the quantity.

        ``status`` is ``"empty"`` whenever ``_render()`` took its
        degenerate branch — a deferred panel, a sub-2-cell agg, an
        all-NaN agg, or a zero-width range.  The browser ignores it (the
        blank image already says as much next to a live sidebar), but an
        exported PNG has no sidebar, so the compositor needs to know to
        draw a framed cell with a note instead of a black rectangle.
        """
        agg = self._agg
        n_x = agg.shape[1] if agg is not None and agg.ndim == 2 else 1
        n_y = agg.shape[0] if agg is not None and agg.ndim == 2 else 1
        x_is_time, y_is_time = self._axis_flags()

        band = ColorBand(
            label         = self._quantity.label,
            cmap          = tuple(self._cmap),
            scaling       = self._scaling,
            scaling_alpha = self._scaling_alpha,
            scaling_gamma = self._scaling_gamma,
            vmin          = self._scaling_vmin,
            vmax          = self._scaling_vmax,
            alpha         = 1.0,
            visible       = True,
        )

        status, note = "ok", None
        if agg is None:
            status, note = "empty", "not rendered yet"
        elif agg.ndim != 2 or agg.shape[0] < 2 or agg.shape[1] < 2:
            status, note = "empty", f"aggregation too small ({agg.shape})"
        elif not np.isfinite(agg.values).any():
            status, note = "empty", "no finite values in selection"
        elif self._x_range[0] == self._x_range[1] or \
             self._y_range[0] == self._y_range[1]:
            status, note = "empty", "zero-width axis range"

        return PanelSpec(
            kind       = "raster",
            title      = self._effective_title(),
            x_label    = _axis_label(self._x_dim),
            y_label    = _axis_label(self._y_dim),
            x_range    = (float(self._x_range[0]), float(self._x_range[1])),
            y_range    = (float(self._y_range[0]), float(self._y_range[1])),
            x_is_time  = x_is_time,
            y_is_time  = y_is_time,
            agg_n_x    = n_x,
            agg_n_y    = n_y,
            color_mode = self._color_mode,
            bands      = (band,),
            status     = status,
            note       = note,
        )

    def _shade_for_export(self, viewport=None) -> Optional[np.ndarray]:
        """Re-shade from the cached agg at *viewport*; no backend query."""
        if self._agg is None:
            return None
        if viewport is None:
            xr, yr = self._x_range, self._y_range
        else:
            x0, x1, y0, y1 = viewport
            xr, yr = (x0, x1), (y0, y1)
        return self._shade_viewport(xr, yr)

    def _build_glyphs(self) -> None:
        """Add the single image_rgba glyph for the raster."""
        self._fig.image_rgba(
            source = self._image_source,
            image  = "image",
            x = "x", y = "y", dw = "dw", dh = "dh",
        )

    def _render(
        self,
        selection: "SelectionSpec",
        max_cells: Optional[int] = None,
        defer: bool = False,
    ) -> None:
        """Run query_raster → shade → update _image_source.

        Parameters
        ----------
        defer : bool
            If ``True``, skip the backend query entirely and populate a
            blank placeholder image with sane (non-degenerate) axis
            ranges, reusing the existing degenerate-agg fallback path
            below rather than a new code path. Used to construct a panel
            object — e.g. a slot's inactive kind — without paying its
            query/render cost until it actually becomes active; see
            decision 11 in the grid/iteration design notes.
            ``self._agg`` is left ``None`` (an already-handled state
            elsewhere — see ``_shade_viewport``'s own ``agg is None``
            check), so a later real ``_render()`` call is not mistaken
            for a redundant one.
        """
        t0     = time.perf_counter()
        budget = max_cells if max_cells is not None else self._max_cells

        if defer:
            agg, x_range, y_range, is_decimated = None, (0.0, 1.0), (0.0, 1.0), False
        else:
            agg, x_range, y_range, is_decimated = self._backend.query_raster(
                y_dim        = self._y_dim,
                x_dim        = self._x_dim,
                quantity     = self._quantity,
                selection    = selection,
                polarization = self._polarization,
                max_cells    = budget,
            )
            log.debug(
                "query_raster: agg=%s  x=%s  y=%s  decimated=%s  (%.3fs)",
                agg.shape, x_range, y_range, is_decimated,
                time.perf_counter() - t0,
            )

        self._agg          = agg
        self._x_range      = x_range
        self._y_range      = y_range
        self._is_decimated = is_decimated

        x0, x1 = x_range
        y0, y1 = y_range

        # `defer` first so the rest short-circuits — agg is None in that
        # case and must never be touched (no agg.shape access below).
        _degenerate = (
            defer
            or agg.shape[0] < 2
            or agg.shape[1] < 2
            or not np.isfinite(agg.values).any()
            or x0 == x1
            or y0 == y1
        )

        if _degenerate:
            log.debug("_render: degenerate agg — blank image")
            img32 = np.zeros((self._height, self._width), dtype=np.uint32)
        else:
            # Same resample rule as _shade_viewport.  Previously this
            # call was bare, taking Datashader's "linear" default, so
            # the initial full-extent view was interpolated while every
            # post-zoom view was not.  See _resample_method.
            interpolate = self._resample_method(agg, (x0, x1), (y0, y1))
            cvs    = ds.Canvas(
                plot_width  = self._width,
                plot_height = self._height,
                x_range     = (x0, x1),
                y_range     = (y0, y1),
            )
            ds_agg = cvs.raster(agg, interpolate=interpolate)
            shaded = self._shade_agg(ds_agg)
            img32  = _img_to_uint32(shaded)

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

        # Keep _state_source in sync (no-op before _state_source exists)
        self._update_state_source()

    def _do_viewport_rerender(
        self, x0: float, x1: float, y0: float, y1: float
    ) -> dict:
        """Two-level pan/zoom: Level-1 Datashader resample or Level-2 re-query."""
        agg = self._agg
        if agg is not None and agg.shape[0] >= 2 and agg.shape[1] >= 2:
            agg_cell_w = (self._x_range[1] - self._x_range[0]) / agg.shape[1]
            agg_cell_h = (self._y_range[1] - self._y_range[0]) / agg.shape[0]
            needs_requery = (
                self._is_decimated
                and (x1 - x0) / self._width  < agg_cell_w
                and (y1 - y0) / self._height < agg_cell_h
            )
        else:
            needs_requery = False

        if needs_requery:
            log.debug("_do_viewport_rerender: Level-2 re-query")
            sub_sel = self._viewport_selection((x0, x1), (y0, y1))
            self._render(sub_sel, max_cells=self._max_cells * 4)
            img32 = self._shade_viewport(self._x_range, self._y_range)
            out_x0, out_x1 = self._x_range
            out_y0, out_y1 = self._y_range
        else:
            log.debug("_do_viewport_rerender: Level-1 resample")
            img32 = self._shade_viewport((x0, x1), (y0, y1))
            out_x0, out_x1 = x0, x1
            out_y0, out_y1 = y0, y1

        return {
            "image": img32,
            "x0": out_x0, "x1": out_x1,
            "y0": out_y0, "y1": out_y1,
        }

    def _handle_probe(self, message: dict) -> dict:
        x = float(message.get("x", 0.0))
        y = float(message.get("y", 0.0))

        x0, x1 = self._x_range
        y0, y1 = self._y_range
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            return self._probe_envelope(
                "out_of_range", "<i>out of range</i>", x=x, y=y,
            )

        px, py = self._data_to_pixel(x, y)
        if px is None:
            return self._probe_envelope(
                "out_of_range", "<i>out of range</i>", x=x, y=y,
            )
        try:
            info  = self._backend.probe_raster_pixel(
                self._agg, px, py, self._selection
            )
            label = self._format_probe(info, self._quantity.label)
            if self._probe_debug:
                log.info(
                    "[probe raster] x=%.9g y=%.9g -> (%d,%d) shape=%s "
                    "value=%r x_range=%r y_range=%r",
                    x, y, px, py, self._agg.shape,
                    info.get("value"), info.get("x_range"),
                    info.get("y_range"),
                )
        except Exception as exc:
            log.warning("probe_raster_pixel failed: %s", exc)
            return self._probe_envelope(
                "error",
                f"<span style='color:#f38ba8'>probe error: {exc}</span>",
                x=x, y=y, error=str(exc),
            )

        # `empty` is carried explicitly rather than left to be inferred
        # from `value is None`: "the backend found no sample here" and
        # "the sample's value was non-finite and _json_num dropped it"
        # collapse to the same None, and only the first is routine.
        value = _json_num(info.get("value"))
        return self._probe_envelope(
            "ok", label,
            x = x, y = y,
            value = value,
            empty = info.get("value") is None,
            pixel = [int(px), int(py)],
        )

    # ------------------------------------------------------------------
    # Raster-specific internals
    # ------------------------------------------------------------------

    def _shade_agg(
        self,
        ds_agg: "xr.DataArray",
    ) -> "object":
        """Shade a Datashader canvas agg using the current scaling state.

        Single call site for the value-to-color transform so that every
        re-shade — whether triggered by ``_render``, ``_shade_viewport``,
        or ``update_scaling`` — reads ``self._scaling`` rather than a
        hardcoded default (Phase 0 CM-6).

        ``color_mode`` behaviour, uniform across every scaling family:
        in ``"global"`` mode, the value-domain anchor (``vmin``/``vmax``)
        is derived from ``self._agg`` (the full cached aggregation)'s
        own finite value range and passed explicitly to every branch —
        never from the y-axis coordinate range. The two are unrelated
        whenever the y-axis differs from the rendered quantity (e.g. a
        TIME vs. CHANNEL raster colored by AMPLITUDE), which is the
        common case. In ``"local"`` mode, ``vmin``/``vmax`` are left
        ``None`` so each branch derives its own range from ``ds_agg``
        (the current viewport crop) — Datashader's native auto-ranging
        for ``linear``/``log``, ``apply_explicit_scaling()``'s own
        finite-range derivation for the explicit-transform scalings.
        Either way, a manual override set via ``update_scaling(vmin=,
        vmax=)`` (``colormap_controls()``'s min/max fields) takes
        precedence over both the ``"global"`` and ``"local"``
        derivation above once set. ``"eq_hist"`` additionally clips its
        input values (and, in ``"global"`` mode, the reference array) to
        the override range before equalizing, rather than using it as a
        Datashader ``span=`` directly — see the ``"eq_hist"`` branch
        below.

        * ``"linear"``, ``"log"`` — Datashader-native ``how=``
          reductions. In ``"global"`` mode, ``vmin``/``vmax`` are passed
          straight through as Datashader's own ``span=``; in ``"local"``
          mode ``span=`` is omitted entirely, letting Datashader
          auto-range from ``ds_agg``. Either way, no manual numpy
          pre-transform is needed for these two scalings.
        * ``"eq_hist"`` — Datashader's native ``how="eq_hist"`` rejects
          ``span=`` outright (raises ``ValueError``), so there is no way
          to anchor it to an external range through the public API.
          Implemented here as an explicit pre-transform via
          ``colormap_scaling.equalize_histogram()`` instead, which
          reimplements Datashader's own CDF-based algorithm but accepts
          a separate *reference* array to build the equalization curve
          from. ``"global"`` passes ``self._agg`` as that reference, so
          colors stay anchored to the full data's distribution
          regardless of zoom level — useful when zoomed in and wanting
          flagging-stable colors. ``"local"`` passes ``None`` (equalize
          against the crop itself), matching Datashader's native
          behaviour and auto-revealing whatever structure is currently
          visible.
        * ``"sqrt"``, ``"square"``, ``"gamma"``, ``"power"`` (explicit
          pre-transform) — ``vmin``/``vmax`` are passed straight to
          ``apply_explicit_scaling()`` as the clip range for the
          transform; in ``"local"`` mode that means passing ``None``,
          letting ``apply_explicit_scaling()`` derive the range from
          ``ds_agg`` itself, same as it always has.

        HISTORY: this function used to be inconsistent — the
        ``"linear"``/``"log"`` branch anchored ``"global"`` mode's
        ``span=`` to a y-axis coordinate range passed in by the caller
        instead of a value range. That was a bug: it silently clipped
        every agg value to a single bin whenever the y-axis range didn't
        numerically overlap the data-value range, producing a flat image
        regardless of scaling. The other five scalings already derived
        their ``"global"``-mode anchor correctly from ``self._agg``;
        this makes ``"linear"``/``"log"`` consistent with them, and
        removes the y-axis-range ``span`` parameter from this call path
        entirely so the two concepts (Datashader's ``span=`` meaning
        "value domain" vs. this codebase's old ``span`` meaning "y-axis
        range") can't collide again. ``"local"`` mode's behaviour is
        unchanged for every scaling — it was never affected by this bug.

        Returns the raw Datashader ``Image`` (not yet converted to
        ``uint32``) — callers apply ``_img_to_uint32`` themselves.
        """
        if self._color_mode == "global" and self._agg is not None:
            finite = self._agg.values[np.isfinite(self._agg.values)]
            vmin = float(finite.min()) if finite.size else None
            vmax = float(finite.max()) if finite.size else None
        else:
            vmin = None  # local mode: let apply_explicit_scaling / Datashader
            vmax = None  # derive their own range from ds_agg, as before

        # Manual override (colormap_controls' min/max fields) takes
        # precedence over the automatic global/local derivation above,
        # in either color_mode.
        if self._scaling_vmin is not None:
            vmin = self._scaling_vmin
        if self._scaling_vmax is not None:
            vmax = self._scaling_vmax

        if self._scaling in _cms.DATASHADER_HOW:
            shade_kw = dict(
                cmap=self._cmap,
                how=_cms.DATASHADER_HOW[self._scaling],
            )
            if vmin is not None and vmax is not None:
                shade_kw["span"] = [vmin, vmax]
            return tf.shade(ds_agg, **shade_kw)

        if self._scaling == "eq_hist":
            reference = (
                self._agg.values
                if self._color_mode == "global" and self._agg is not None
                else None
            )
            # Manual vmin/vmax override applies to eq_hist too, by
            # restricting the REFERENCE population (the array
            # equalize_histogram() builds its CDF from) to the in-range
            # subset, rather than clipping ds_agg's values in place.
            # eq_hist is rank-based, so clipping values without also
            # restricting the reference has ~no effect on interior color
            # resolution — pinning outliers to the boundary doesn't
            # change the interior rank order, verified empirically
            # before landing this. Restricting the reference population
            # does work: equalize_histogram()'s own np.interp(...,
            # left=cdf[0], right=cdf[-1]) automatically pins out-of-range
            # values to the extreme colors, so ds_agg.values itself is
            # passed through unmodified — only `reference` narrows.
            if self._scaling_vmin is not None or self._scaling_vmax is not None:
                pool = reference if reference is not None else ds_agg.values
                pool_finite = pool[np.isfinite(pool)]
                lo = self._scaling_vmin if self._scaling_vmin is not None else (
                    float(pool_finite.min()) if pool_finite.size else None)
                hi = self._scaling_vmax if self._scaling_vmax is not None else (
                    float(pool_finite.max()) if pool_finite.size else None)
                if lo is not None and hi is not None and hi > lo:
                    in_band = pool_finite[(pool_finite >= lo) & (pool_finite <= hi)]
                    if in_band.size > 0:
                        reference = in_band
            transformed = _cms.equalize_histogram(
                ds_agg.values, reference=reference,
            )
            scaled_agg = ds_agg.copy(data=transformed)
            return tf.shade(scaled_agg, cmap=self._cmap, how="linear", span=[0.0, 1.0])

        transformed = _cms.apply_explicit_scaling(
            ds_agg.values,
            self._scaling,
            alpha=self._scaling_alpha,
            gamma=self._scaling_gamma,
            vmin=vmin,
            vmax=vmax,
        )
        scaled_agg = ds_agg.copy(data=transformed)
        return tf.shade(scaled_agg, cmap=self._cmap, how="linear", span=[0.0, 1.0])

    def _resample_method(
        self,
        agg: "xr.DataArray",
        x_range: tuple[float, float],
        y_range: tuple[float, float],
    ) -> str:
        """Datashader upsample method for rendering *agg* into *x/y_range*.

        Returns ``"nearest"`` when either display axis is being
        **upsampled** — that is, when one agg cell spans more than one
        screen pixel — and ``"linear"`` otherwise.

        Why this matters, and why it is shared
        --------------------------------------
        ``Canvas.raster()`` defaults to ``upsample_method="linear"``.
        Linear interpolation presumes both axes are continua, and on a
        visibility raster neither is:

        * ``baseline_id`` is a categorical index.  Adjacent IDs are not
          physically adjacent baselines, so a value interpolated between
          baseline 40 and 41 corresponds to no baseline at all.
        * ``time`` has inter-scan gaps.  Interpolating across one invents
          data spanning minutes during which nothing was observed.  (The
          same non-uniformity that produced the ``_cell_bounds`` defect
          in the probe path.)

        It also dilutes exactly the features this tool exists to find.
        A single bad integration at amplitude 90 against a background of
        10 renders at 82.1 (8.8% low) at a 2.5x upsample ratio and 85.9
        (4.6% low) at 5x -- and the loss is *worst* at low ratios, where
        screen pixels straddle sample points rather than landing near
        them.  Ratios of 2-5x are the common regime for a few hundred
        timestamps on a 500 px panel.  A marginal outlier diluted below
        visual threshold in the full-extent view is one the astronomer
        never zooms in on.

        Finally, ``_data_to_pixel`` maps hover coordinates to the **agg
        grid**, so the probe reports true cell values while a linearly
        interpolated image shows fabricated intermediate ones.  On a
        smoothed gradient the displayed colour varies while every probe
        returns the same number, and the probe is the one that is right.

        This was previously computed only in ``_shade_viewport``;
        ``_render`` called ``cvs.raster(agg)`` bare and so took the
        ``"linear"`` default.  Since the initial full-extent view is
        routinely upsampling in time (a few tens of timestamps stretched
        over ~500 screen rows), the first image the user saw was
        interpolated and every image after the first zoom was not.
        Extracted here so the two paths cannot diverge again.

        Note that the test is an ``or`` across axes: if *either* axis
        upsamples, ``"nearest"`` is used for both.  Datashader takes a
        single upsample method for the whole resample, and preserving
        true sample values is the safer failure direction for a tool
        whose purpose is spotting bad data.
        """
        if self._raster_interpolate != "auto":
            return self._raster_interpolate

        x0, x1 = x_range
        y0, y1 = y_range
        agg_cell_w = (self._x_range[1] - self._x_range[0]) / agg.shape[1]
        agg_cell_h = (self._y_range[1] - self._y_range[0]) / agg.shape[0]
        upsampling = (
            (x1 - x0) / self._width  < agg_cell_w
            or (y1 - y0) / self._height < agg_cell_h
        )
        return "nearest" if upsampling else "linear"

    def _shade_viewport(
        self,
        x_range: tuple[float, float],
        y_range: tuple[float, float],
    ) -> np.ndarray:
        """Resample cached agg; use nearest-neighbour when upsampling."""
        agg = self._agg
        x0, x1 = x_range
        y0, y1 = y_range

        if (
            agg is None
            or agg.shape[0] < 2
            or agg.shape[1] < 2
            or x0 == x1
            or y0 == y1
        ):
            return np.zeros((self._height, self._width), dtype=np.uint32)

        interpolate = self._resample_method(agg, x_range, y_range)

        cvs    = ds.Canvas(
            plot_width  = self._width,
            plot_height = self._height,
            x_range     = (x0, x1),
            y_range     = (y0, y1),
        )
        ds_agg = cvs.raster(agg, interpolate=interpolate)
        shaded = self._shade_agg(ds_agg)
        return _img_to_uint32(shaded)

    def _data_to_pixel(
        self, x: float, y: float
    ) -> tuple[Optional[int], Optional[int]]:
        """Map data-space (x, y) to agg grid indices (px, py)."""
        if self._agg is None:
            return None, None
        agg        = self._agg
        y_dim_name = agg.dims[0]
        x_dim_name = agg.dims[1]
        if x_dim_name not in agg.coords or y_dim_name not in agg.coords:
            return None, None
        x_coords = agg.coords[x_dim_name].values
        y_coords = agg.coords[y_dim_name].values
        if len(x_coords) == 0 or len(y_coords) == 0:
            return None, None
        px = int(np.argmin(np.abs(x_coords - x)))
        py = int(np.argmin(np.abs(y_coords - y)))
        h, w = agg.shape
        return max(0, min(px, w - 1)), max(0, min(py, h - 1))

    def _register_extra_comm_handlers(self) -> None:
        self._comm.register(self._msg_update_axes, self._handle_update_axes_raster)
        self._comm.register(self._msg_color_mode,  self._handle_set_color_mode_raster)
        self._comm.register(self._msg_update_scaling, self._handle_update_scaling_raster)

    def _handle_set_color_mode_raster(self, message: dict) -> dict:
        """Handle j2p message to toggle color mode: {mode: "global"|"local"}."""
        mode = message.get("mode", "global")
        try:
            self.set_color_mode(mode)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        # Return the new image so JS can update image_source directly
        src = self._image_source.data
        return {
            "status":     "ok",
            "color_mode": self._color_mode,
            "image":      src["image"][0],
            "x0":         src["x"][0],
            "x1":         src["x"][0] + src["dw"][0],
            "y0":         src["y"][0],
            "y1":         src["y"][0] + src["dh"][0],
        }

    def _handle_update_scaling_raster(self, message: dict) -> dict:
        """Handle j2p 'vr_update_scaling': {scaling, alpha, gamma, vmin, vmax, reset_range}.

        All fields optional; omitted fields keep their current value.
        Returns the re-shaded image so JS can update image_source
        directly, mirroring _handle_set_color_mode_raster.
        """
        try:
            self.update_scaling(
                scaling     = message.get("scaling"),
                alpha       = message.get("alpha"),
                gamma       = message.get("gamma"),
                vmin        = message.get("vmin"),
                vmax        = message.get("vmax"),
                reset_range = bool(message.get("reset_range", False)),
            )
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}
        if self._image_source is None:
            return {"status": "ok", "scaling": self._scaling}
        src = self._image_source.data
        return {
            "status":        "ok",
            "scaling":       self._scaling,
            "scaling_alpha": self._scaling_alpha,
            "scaling_gamma": self._scaling_gamma,
            "scaling_vmin":  self._scaling_vmin,
            "scaling_vmax":  self._scaling_vmax,
            "image":         src["image"][0],
            "x0":            src["x"][0],
            "x1":            src["x"][0] + src["dw"][0],
            "y0":            src["y"][0],
            "y1":            src["y"][0] + src["dh"][0],
        }

    def _handle_update_axes_raster(self, message: dict) -> dict:
        """Handle j2p 'vr_update_axes' with raster-specific fields."""
        self.update_axes(
            y_dim        = self._parse_axis(message, "y_dim"),
            x_dim        = self._parse_axis(message, "x_dim"),
            quantity     = self._parse_axis(message, "quantity"),
            polarization = message.get("polarization"),
            title        = message.get("title"),
        )
        return {"status": "ok"}
