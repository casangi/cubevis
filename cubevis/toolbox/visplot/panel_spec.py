"""
panel_spec.py
=============
The shared description of a rendered panel — the seam between the Bokeh
display path and the matplotlib export path.

Why this exists
---------------
Bokeh contributes no pixels to the data area of a ``VisibilityRaster`` or
``VisibilityScatter``.  Both classes produce an ``(H, W) uint32`` RGBA
array via Datashader and hand it to a single ``image_rgba`` glyph; Bokeh
draws the *chrome* around it — title, axis labels, ticks, tick
formatters, toolbar.  A matplotlib export path therefore does not
reimplement rendering, it reimplements chrome over an identical array.

That makes the sync problem bounded, but only if both chrome-drawers read
the same description.  ``PanelSpec`` is that description, and
``_state_data()`` — the ``ColumnDataSource`` the ``CustomJS`` chrome
already reads — is now *derived* from it rather than assembled
alongside it.  A field added for the browser is therefore automatically
visible to the exporter, and a field the exporter needs cannot be added
without the browser's copy seeing it too.

``PanelSpec`` is deliberately a superset of ``_state_data()``: it also
carries per-band colormaps and band labels, which the JS chrome has never
needed but a colorbar or legend does.  ``to_state_data()`` emits only the
historical key set, so this module can grow without perturbing the
browser.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/panel_spec.py``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np


# ---------------------------------------------------------------------------
# ColorBand
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ColorBand:
    """One value-to-color mapping contributing to a panel's image.

    A raster has exactly one band (its quantity).  A scatter has one per
    ``ScatterLayer``.  Unifying them here is what lets the export
    compositor draw a colorbar or a legend without branching on panel
    kind — it iterates ``PanelSpec.bands`` either way, and the only
    difference is how many there are.

    Attributes
    ----------
    label : str
        Human-readable name.  The quantity label for a raster
        (``"Amplitude"``); the layer label for a scatter
        (``"Amplitude XX"``).
    cmap : tuple[str, ...]
        Colormap as hex strings, low value first.  A tuple rather than a
        list so the dataclass stays hashable and cannot be mutated by a
        consumer.
    scaling : str
        One of ``colormap_scaling.ALL_SCALINGS``.
    scaling_alpha, scaling_gamma : float
        Parameters for the ``"log"``/``"power"`` and ``"gamma"``
        scalings respectively.  Carried unconditionally — which one is
        live depends on ``scaling``.
    vmin, vmax : float | None
        Manual value-domain clip, or ``None`` for automatic ranging.
        Note these are the *override* values, not the effective range:
        the effective range depends on ``PanelSpec.color_mode`` and on
        the data, and is resolved at shade time.  An exporter drawing a
        colorbar needs the effective range and must obtain it from the
        scalar mapping, not from here.
    alpha : float
        Layer opacity in [0, 1].  Always 1.0 for a raster.
    kind : {"value", "density"}
        What the ramp encodes.  ``"value"`` for a raster, whose ramp is
        the quantity; ``"density"`` for a scatter layer, whose ramp is
        points per pixel.  Drives ``bar_label()`` — see there.
    mapping : ScalarMapping | None
        The value-to-colour-position curve, from
        ``colormap_scaling.ScalarMapping``.  ``None`` on a spec built for
        ``_state_data()``, which never needs it; populated by
        ``render_result()``, where a colorbar might.  Building it costs a
        histogram and 512 interpolation samples, which is nothing beside
        a shade but too much to pay on every state push.
    peak_density : float | None
        Highest per-pixel count in a scatter layer's aggregation, for the
        legend annotation.  ``None`` for a raster.
    visible : bool
        ``False`` when the user has hidden this band (``alpha == 0``).
        Kept in the list rather than filtered out so band indices stay
        stable — the same reason the probe envelope keeps an entry for
        every layer.
    """

    label:         str
    cmap:          tuple[str, ...]
    scaling:       str
    scaling_alpha: float = 10.0
    scaling_gamma: float = 1.0
    vmin:          Optional[float] = None
    vmax:          Optional[float] = None
    alpha:         float = 1.0
    visible:       bool  = True
    kind:          str   = "value"
    mapping:       Optional[object] = None
    peak_density:  Optional[float]  = None

    # ------------------------------------------------------------------
    # Labelling
    # ------------------------------------------------------------------

    def bar_label(self) -> str:
        """Axis label for this band's colorbar.

        Two things this must get right, both of which are misreadings
        waiting to happen in a published figure:

        *Quantity.*  A raster's ramp encodes the quantity itself; a
        scatter's encodes *points per pixel*.  Labelling a scatter bar
        "Amplitude XX" — the layer label, which is what the legend
        correctly uses — states that the ramp is an amplitude scale when
        it is a density scale.  ``kind`` distinguishes them so the
        compositor never has to guess.

        *Scaling.*  Every reader's prior, from plotms and the CASA
        viewer, is a linear ramp.  Under ``eq_hist`` the ticks bunch
        where the data is dense, which is the information the scaling
        exists to expose — but only to a reader who knows to expect it.
        Naming the scaling is the difference between a figure that
        documents its processing and one that quietly misrepresents it.
        """
        base = "Density" if self.kind == "density" else self.label
        if self.scaling and self.scaling != "linear":
            return f"{base} ({self.scaling})"
        return base

    def legend_label(self) -> str:
        """Legend entry, with peak density folded in for scatter bands.

        What an astronomer wants from a density ramp is usually one
        number — "how overplotted is this?" — not a ramp.  Folding it
        into the legend answers that in one line, survives grid mode
        without scaling, and is why scatter colorbars default to off.
        """
        if self.kind == "density" and self.peak_density:
            return f"{self.label}  (\u2264{self.peak_density:.0f} pts/px)"
        return self.label

    def bin_area(self, spec: "PanelSpec") -> tuple[float, float]:
        """Data-units size of one aggregation bin, for comparability.

        Two scatter panels only have comparable densities if their bins
        cover the same area — ``_compute_canvas_size`` is adaptive and
        shrinks the canvas for sparse layers by more than an order of
        magnitude, so "count = 10" can mean quite different things in two
        cells of the same grid.  Note this is orthogonal to
        ``color_mode``: global vs local governs zoom *within* a panel,
        not comparability *across* panels.
        """
        return (
            (spec.x_range[1] - spec.x_range[0]) / max(spec.agg_n_x, 1),
            (spec.y_range[1] - spec.y_range[0]) / max(spec.agg_n_y, 1),
        )


# ---------------------------------------------------------------------------
# PanelSpec
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PanelSpec:
    """Everything about a rendered panel except its pixels.

    Cheap to build — every field is a plain attribute read or a value
    already computed during ``_render()`` — so ``_state_data()`` can
    derive from it on every state push without a re-shade.  The pixels
    live in ``RenderedPanel``, which is built only when something
    actually wants an image.

    Attributes
    ----------
    kind : str
        ``"raster"`` or ``"scatter"``.  Drives the per-band key naming in
        ``to_state_data()`` — raster emits unprefixed ``scaling``/
        ``scaling_alpha``/..., scatter emits ``layer_scaling_{i}``/... —
        which is a historical asymmetry in the ``_state_source`` schema
        that the JS reads, not a design choice worth relitigating here.
    title : str
        Panel title, from ``_effective_title()``.
    x_label, y_label : str
        Axis labels, already formatted with units by ``_axis_label()``.
    x_range, y_range : tuple[float, float]
        Full data extent.  Not the current viewport — see
        ``RenderedPanel.viewport`` for that.
    x_is_time, y_is_time : bool
        Whether that axis is ``Axis.TIME``, which selects elapsed-time
        tick formatting.  Both chrome paths need this and must agree on
        the resulting tick *strings*; see the tick-format parity note in
        the export design.
    agg_n_x, agg_n_y : int
        Aggregation grid resolution at full extent.  Raster takes these
        from ``agg.shape``; scatter from ``_compute_canvas_size()``.
        Same field names in both because ``flag_tool.ts``'s zoom-to-1:1
        math reads them without knowing the panel kind.
    color_mode : str
        ``"global"`` or ``"local"``.
    bands : tuple[ColorBand, ...]
        One entry per value-to-color mapping; see ``ColorBand``.
    status : str
        ``"ok"``, ``"empty"``, or ``"error"``.  Mirrors the probe
        envelope's vocabulary deliberately.  ``"empty"`` is a normal
        result (no rows selected, everything flagged); ``"error"`` is
        not, and an export must render it differently so a pipeline
        emitting 43 PNGs cannot silently swallow a failure.
    theme : {"dark", "light"}
        The theme the pixels were **shaded for**.  Carried because a
        palette is baked into the image before ``png_export`` ever sees
        it, and ramps are conditioned against a specific background
        (``palettes.condition``).  Drawing dark-conditioned pixels on a
        light ground, or the reverse, costs about 2.5x contrast.

        ``export_png`` defaults its chrome theme to this, so the mismatch
        cannot happen by forgetting to pass the theme twice -- which is
        exactly how it happened in the first headless export.
    note : str | None
        Human-readable detail for a non-``"ok"`` status, e.g. scatter's
        ``_layer_skip_reason`` text or an exception string.  Rendered
        into an empty export cell; unused by the browser.
    """

    kind:       str
    title:      str
    x_label:    str
    y_label:    str
    x_range:    tuple[float, float]
    y_range:    tuple[float, float]
    x_is_time:  bool
    y_is_time:  bool
    agg_n_x:    int
    agg_n_y:    int
    color_mode: str
    bands:      tuple[ColorBand, ...] = field(default_factory=tuple)
    status:     str           = "ok"
    note:       Optional[str] = None
    theme:      str           = "dark"
    x_unit:     str           = ""
    y_unit:     str           = ""

    # ------------------------------------------------------------------
    # Bokeh interop
    # ------------------------------------------------------------------

    def axis_scale(self, axis: str = "x") -> tuple[float, str]:
        """``(divisor, prefixed_unit)`` for one axis.

        Chosen from the *full extent* rather than the viewport, so units
        do not flip from GHz to MHz partway through a zoom -- and so the
        axis label can be computed once instead of on every pan.

        Time axes are excluded: elapsed formatting is already
        human-scaled, and dividing MJD seconds by 1e9 would be nonsense.
        """
        from .tick_format import si_prefix
        is_time = self.x_is_time if axis == "x" else self.y_is_time
        unit    = self.x_unit if axis == "x" else self.y_unit
        if is_time:
            return 1.0, unit
        lo, hi = self.x_range if axis == "x" else self.y_range
        return si_prefix(lo, hi, unit)

    def axis_label(self, axis: str = "x") -> str:
        """Axis label with the SI-prefixed unit, e.g. ``Frequency [GHz]``.

        The prefix goes in the *label*, not on each tick: Bokeh's
        ``CustomJSTickFormatter`` runs per tick and has no slot for a
        shared multiplier annotation, so scaling the label is the one
        approach that works identically in both runtimes.
        """
        base = self.x_label if axis == "x" else self.y_label
        base = base.split(" [")[0]      # drop any unit already present
        _, unit = self.axis_scale(axis)
        return base + (f" [{unit}]" if unit else "")

    def with_bands(self, bands) -> "PanelSpec":
        """Copy with *bands* replaced — used to attach mappings late."""
        from dataclasses import replace
        return replace(self, bands=tuple(bands))

    def to_state_data(self) -> dict:
        """Render as a ``ColumnDataSource.data`` dict.

        Emits exactly the historical ``_state_source`` key set and no
        more.  ``PanelSpec`` carries strictly more than this — band
        labels and cmaps, ``status``, ``note`` — because the export path
        needs them and the JS chrome does not.  Adding a field to
        ``PanelSpec`` must not silently change what the browser sees, so
        new keys are added here only deliberately.

        Every value is wrapped in a single-element list, per Bokeh's
        column format.
        """
        data: dict = {
            "full_x0":   [float(self.x_range[0])],
            "full_x1":   [float(self.x_range[1])],
            "full_y0":   [float(self.y_range[0])],
            "full_y1":   [float(self.y_range[1])],
            "y_is_time": [int(self.y_is_time)],
            "x_is_time": [int(self.x_is_time)],
            "x_label":   [self.axis_label("x")],
            "y_label":   [self.axis_label("y")],
            "agg_n_x":   [self.agg_n_x],
            "agg_n_y":   [self.agg_n_y],
            "color_mode": [self.color_mode],
            # Divisors for the tick formatter.  Fixed from the full
            # extent, so a pan or zoom never changes the axis units.
            "x_scale":   [self.axis_scale("x")[0]],
            "y_scale":   [self.axis_scale("y")[0]],
        }

        if self.kind == "scatter":
            for i, band in enumerate(self.bands):
                data[f"layer_alpha_{i}"]         = [band.alpha]
                data[f"layer_label_{i}"]         = [band.label]
                data[f"layer_scaling_{i}"]       = [band.scaling]
                data[f"layer_scaling_alpha_{i}"] = [band.scaling_alpha]
                data[f"layer_scaling_gamma_{i}"] = [band.scaling_gamma]
                data[f"layer_scaling_vmin_{i}"]  = [band.vmin]
                data[f"layer_scaling_vmax_{i}"]  = [band.vmax]
            data["n_layers"] = [len(self.bands)]
        else:
            # Raster: exactly one band, emitted unprefixed.  Guarded
            # rather than indexed blindly because a deferred panel has
            # rendered nothing yet and may legitimately have no band.
            band = self.bands[0] if self.bands else None
            data["scaling"]       = [band.scaling if band else ""]
            data["scaling_alpha"] = [band.scaling_alpha if band else 0.0]
            data["scaling_gamma"] = [band.scaling_gamma if band else 0.0]
            data["scaling_vmin"]  = [band.vmin if band else None]
            data["scaling_vmax"]  = [band.vmax if band else None]

        return data


# ---------------------------------------------------------------------------
# RenderedPanel
# ---------------------------------------------------------------------------

@dataclass(frozen=True, eq=False)
class RenderedPanel:
    """A ``PanelSpec`` plus the pixels that go with it.

    This is the unit the export compositor consumes.  It knows nothing
    about where it came from: the same shape is produced by a headless
    ``VisibilityPlotter`` render and by the GUI's Export button handler
    (which supplies the browser's current viewport).  Two producers, one
    consumer.

    ``eq=False`` because a numpy array field makes the generated
    ``__eq__`` raise on ambiguous truth value; compare ``spec`` and
    ``image`` explicitly if you need to.

    Attributes
    ----------
    spec : PanelSpec
        The description.  ``spec.status`` says whether ``image`` is
        meaningful.
    image : np.ndarray | None
        ``(H, W) uint32``, bytes R,G,B,A in memory order — see
        ``visibility_plot._img_to_uint32``.  ``None`` when
        ``spec.status`` is not ``"ok"``, i.e. an empty or failed cell,
        which the compositor renders as a framed cell with a title and
        ``spec.note`` rather than skipping.
    viewport : tuple[float, float, float, float] | None
        ``(x0, x1, y0, y1)`` actually rendered.  ``None`` means the full
        extent in ``spec.x_range``/``y_range``.  Present because the
        GUI's viewport lives only in the browser — Bokeh model
        properties set from ``CustomJS`` never propagate back — so an
        export triggered from the GUI must be told what is on screen.
    """

    spec:     PanelSpec
    image:    Optional[np.ndarray] = None
    viewport: Optional[tuple[float, float, float, float]] = None

    @property
    def extent(self) -> tuple[float, float, float, float]:
        """``(x0, x1, y0, y1)`` for ``imshow(extent=...)``.

        The rendered viewport when there is one, else the full extent.
        Matplotlib wants this in ``(left, right, bottom, top)`` order,
        which is what both sources already are.
        """
        if self.viewport is not None:
            return self.viewport
        return (self.spec.x_range[0], self.spec.x_range[1],
                self.spec.y_range[0], self.spec.y_range[1])

    def rgba(self) -> Optional[np.ndarray]:
        """``(H, W, 4)`` uint8 RGBA view of ``image``, or ``None``.

        No channel permutation: Datashader emits RGBA in memory order,
        verified empirically (shading ``cmap=["#FF0000", "#0000FF"]``
        yields bytes ``[255,0,0,255]`` and ``[0,0,255,255]``), and
        ``_img_to_uint32`` guarantees the result is C-contiguous so this
        view is always legal.
        """
        if self.image is None:
            return None
        h, w = self.image.shape
        return self.image.view(np.uint8).reshape(h, w, 4)
