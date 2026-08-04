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
import time
from typing import Optional, TYPE_CHECKING
from uuid import uuid4

import numpy as np

from bokeh.models import ColumnDataSource

from .visibility_plot import VisibilityPlot, _img_to_uint32, _axis_label
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

        # Raster-specific state (set by _render)
        self._agg:          Optional["xr.DataArray"] = None
        self._is_decimated: bool                     = False

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
            ``"global"`` — span = full data y_range; colors stable on zoom.
            ``"local"`` — Datashader normalises to viewport; reveals detail.
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
        CM-4 in the implementation plan.  Scaling changes call
        ``update_scaling()`` via the existing CommMgr j2p path
        (``self._msg_update_scaling``); the dropdown and alpha/gamma
        inputs themselves are plain Bokeh widgets with no bespoke
        per-instance JS beyond what ``VisibilityPlot`` already wires for
        other controls.

        Layout, in order:
            - Scaling dropdown (linear, log, eq_hist, sqrt, square,
              gamma, power)
            - Equation label (``Div``, updates with scaling choice)
            - Alpha numeric input (visible for log / power)
            - Gamma numeric input (visible for gamma)
            - Min / max numeric range inputs

        The widget construction itself (Bokeh `Select`, `TextInput`,
        `Div`, and the `column`/`row` layout) is implemented inline here
        rather than factored further, since both display classes need an
        essentially identical control set and any divergence is better
        caught early than abstracted prematurely.
        """
        from bokeh.layouts import column, row
        from bokeh.models import Select, TextInput, Div

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
        min_input = TextInput(title="min", value="")
        max_input = TextInput(title="max", value="")

        def _on_scaling_change(attr, old, new):
            alpha_input.visible = new in ("log", "power")
            gamma_input.visible = new == "gamma"
            equation.text = _cms.scaling_equation_label(new)
            self.update_scaling(scaling=new)

        def _on_alpha_change(attr, old, new):
            try:
                self.update_scaling(alpha=float(new))
            except ValueError:
                pass

        def _on_gamma_change(attr, old, new):
            try:
                self.update_scaling(gamma=float(new))
            except ValueError:
                pass

        scaling_select.on_change("value", _on_scaling_change)
        alpha_input.on_change("value", _on_alpha_change)
        gamma_input.on_change("value", _on_gamma_change)

        return column(
            scaling_select,
            equation,
            row(alpha_input, gamma_input),
            row(min_input, max_input),
        )

    def _state_data_extra(self) -> dict:
        """Add agg shape, color_mode, and colormap scaling to _state_source."""
        agg = self._agg
        n_x = agg.shape[1] if agg is not None and agg.ndim == 2 else 1
        n_y = agg.shape[0] if agg is not None and agg.ndim == 2 else 1
        return {
            "agg_n_x":    [n_x],
            "agg_n_y":    [n_y],
            "color_mode": [self._color_mode],
            "scaling":        [self._scaling],
            "scaling_alpha":  [self._scaling_alpha],
            "scaling_gamma":  [self._scaling_gamma],
        }

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
            cvs    = ds.Canvas(
                plot_width  = self._width,
                plot_height = self._height,
                x_range     = (x0, x1),
                y_range     = (y0, y1),
            )
            ds_agg = cvs.raster(agg)
            span = [float(y0), float(y1)] if self._color_mode == "global" else None
            shaded = self._shade_agg(ds_agg, span=span)
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
            return {"label": "<i>out of range</i>"}

        px, py = self._data_to_pixel(x, y)
        if px is None:
            return {"label": "<i>out of range</i>"}
        try:
            info  = self._backend.probe_raster_pixel(
                self._agg, px, py, self._selection
            )
            label = self._format_probe(info, self._quantity.label)
        except Exception as exc:
            log.warning("probe_raster_pixel failed: %s", exc)
            label = f"<span style='color:#f38ba8'>probe error: {exc}</span>"
        return {"label": label}

    # ------------------------------------------------------------------
    # Raster-specific internals
    # ------------------------------------------------------------------

    def _shade_agg(
        self,
        ds_agg: "xr.DataArray",
        span: Optional[list] = None,
    ) -> "object":
        """Shade a Datashader canvas agg using the current scaling state.

        Single call site for the value-to-color transform so that every
        re-shade — whether triggered by ``_render``, ``_shade_viewport``,
        or ``update_scaling`` — reads ``self._scaling`` rather than a
        hardcoded default (Phase 0 CM-6).

        ``color_mode`` behaviour by scaling family:

        * ``"linear"``, ``"log"`` — Datashader-native ``how=`` reductions
          that accept ``span=``. ``span`` (the y-axis range passed in by
          the caller; this raster's long-standing ``"global"``
          convention) directly anchors the color domain. ``"global"``
          keeps colors stable across pan/zoom; ``"local"``
          (``span=None``) lets Datashader auto-range to whatever is in
          ``ds_agg``.
        * ``"eq_hist"`` — Datashader's native ``how="eq_hist"`` rejects
          ``span=`` outright (raises ``ValueError``), so there is no way
          to anchor it to an external range through the public API.
          Implemented here as an explicit pre-transform via
          ``colormap_scaling.equalize_histogram()`` instead, which
          reimplements Datashader's own CDF-based algorithm but accepts
          a separate *reference* array to build the equalization curve
          from. ``"global"`` passes ``self._agg`` (the full cached
          aggregation) as the reference, so colors stay anchored to the
          full data's distribution regardless of zoom level — useful
          when zoomed in and wanting flagging-stable colors. ``"local"``
          passes ``None`` (equalize against the crop itself), matching
          Datashader's native behaviour and auto-revealing whatever
          structure is currently visible.
        * ``"sqrt"``, ``"square"``, ``"gamma"``, ``"power"`` (explicit
          pre-transform) — ``vmin``/``vmax`` for the transform are
          derived from ``self._agg``'s own finite value range in
          ``"global"`` mode, or from ``ds_agg`` (the current viewport
          crop)'s own finite value range in ``"local"`` mode. Same
          global/local intent as the eq_hist case above, via a clip
          range rather than a full CDF.

          IMPORTANT: none of the explicit-pre-transform branches derive
          their value-domain anchor from the ``span`` parameter, which
          carries the y-axis coordinate range (e.g. TIME in MJD seconds)
          in this raster's ``"global"`` calling convention — unrelated
          to the *value* domain of the rendered quantity (e.g.
          amplitude) whenever the y-axis differs from the rendered
          quantity, which is the common case. Using ``span`` for this
          was an earlier bug: it silently clipped every agg value to a
          single bin whenever the y-axis range didn't numerically
          overlap the data-value range, producing a flat image
          regardless of which scaling was selected.

        Returns the raw Datashader ``Image`` (not yet converted to
        ``uint32``) — callers apply ``_img_to_uint32`` themselves.
        """
        if self._scaling in _cms.DATASHADER_HOW:
            shade_kw = dict(
                cmap=self._cmap,
                how=_cms.DATASHADER_HOW[self._scaling],
            )
            if span is not None:
                shade_kw["span"] = span
            return tf.shade(ds_agg, **shade_kw)

        if self._scaling == "eq_hist":
            reference = (
                self._agg.values
                if self._color_mode == "global" and self._agg is not None
                else None
            )
            transformed = _cms.equalize_histogram(
                ds_agg.values, reference=reference,
            )
            scaled_agg = ds_agg.copy(data=transformed)
            return tf.shade(scaled_agg, cmap=self._cmap, how="linear", span=[0.0, 1.0])

        if self._color_mode == "global" and self._agg is not None:
            finite = self._agg.values[np.isfinite(self._agg.values)]
            vmin = float(finite.min()) if finite.size else None
            vmax = float(finite.max()) if finite.size else None
        else:
            vmin = None  # local mode: apply_explicit_scaling derives
            vmax = None  # from ds_agg's own (cropped) finite range

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

        agg_cell_w = (self._x_range[1] - self._x_range[0]) / agg.shape[1]
        agg_cell_h = (self._y_range[1] - self._y_range[0]) / agg.shape[0]
        upsampling  = (
            (x1 - x0) / self._width  < agg_cell_w
            or (y1 - y0) / self._height < agg_cell_h
        )
        interpolate = "nearest" if upsampling else "linear"

        cvs    = ds.Canvas(
            plot_width  = self._width,
            plot_height = self._height,
            x_range     = (x0, x1),
            y_range     = (y0, y1),
        )
        ds_agg = cvs.raster(agg, interpolate=interpolate)
        span = (
            [float(self._y_range[0]), float(self._y_range[1])]
            if self._color_mode == "global" else None
        )
        shaded = self._shade_agg(ds_agg, span=span)
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
        """Handle j2p 'vr_update_scaling': {scaling, alpha, gamma}.

        All fields optional; omitted fields keep their current value.
        Returns the re-shaded image so JS can update image_source
        directly, mirroring _handle_set_color_mode_raster.
        """
        try:
            self.update_scaling(
                scaling = message.get("scaling"),
                alpha   = message.get("alpha"),
                gamma   = message.get("gamma"),
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
