"""
png_export.py
=============
Matplotlib compositor: turns ``RenderedPanel`` objects into a PNG.

Scope
-----
This module draws *chrome* — figure, axes, ticks, titles, legends,
footer.  It produces no data pixels of its own: those arrive already
shaded, as the same ``(H, W) uint32`` RGBA array the Bokeh
``image_rgba`` glyph displays.  Bokeh and matplotlib are two
chrome-drawers over one Datashader render, which is what makes keeping
the GUI and the export in agreement a bounded problem rather than an
open-ended one.

It also knows nothing about where its panels came from.  The same call
serves a headless ``visplot(ms=..., plotfile=...)`` render and the GUI's
Export button (which supplies the browser's current viewport, since with
no Bokeh server a pan or zoom never reaches Python).  Two producers, one
consumer.

Grid model
----------
Duo mode and M x N grid mode are the same call.  ``layout="side"``
becomes ``(1, 2)``, ``"over"`` becomes ``(2, 1)``, ``"one"`` becomes
``(1, 1)``, and grid mode passes its own ``gridrows``/``gridcols``.  No
layout vocabulary reaches this module; the caller translates first.

Cells are **blank but framed**: a grid position with no data still gets
its axes, its title, and a note saying why it is empty.  Reflowing to
fill the gap would make cell position meaningless across a sequence of
exported files, which matters when someone is flipping through
``out_ant00.png`` ... ``out_ant42.png``.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/png_export.py``
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from .panel_spec import ColorBand, PanelSpec, RenderedPanel
from .tick_format import mpl_formatter

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Theme:
    """Colours for the chrome.

    The GUI is dark; a publication PNG is almost always light.  That is a
    deliberate difference between the two outputs, not a fidelity bug —
    "matches the GUI" is a claim about data pixels, ranges, and tick
    label text, not about background colour.
    """
    figure:    str
    axes:      str
    text:      str
    muted:     str
    spine:     str
    grid:      str
    error:     str


THEMES = {
    "light": Theme(figure="#ffffff", axes="#ffffff", text="#1a1a1a",
                   muted="#6a6a6a", spine="#9a9a9a", grid="#e0e0e0",
                   error="#b4232a"),
    # Catppuccin Mocha, matching the GUI's palette so a dark export looks
    # like a screenshot of the app rather than an unrelated styling.
    "dark":  Theme(figure="#1e1e2e", axes="#181825", text="#cdd6f4",
                   muted="#a6adc8", spine="#585b70", grid="#313244",
                   error="#f38ba8"),
}

# Chrome sizes in POINTS (1/72 inch), scaled to device pixels by dpi.
#
# Points rather than pixels so raising dpi does what people expect: the
# layout keeps its proportions, text is rendered with more pixels, and the
# file gets bigger.  The data area is the one thing held in pixels -- it is
# exactly agg_n_x by agg_n_y device pixels at every dpi, so the Datashader
# render is never resampled.  That is the basis of the fidelity claim, and
# also why byte-identity with the GUI holds only when the export uses the
# panel's own canvas size.
_MARGIN_L_PT   = 66.0
_MARGIN_R_PT   = 17.0
_MARGIN_T_PT   = 12.0
_MARGIN_B_PT   = 45.0
_CELL_TITLE_PT = 19.0
_WSPACE_PT     = 42.0
_HSPACE_PT     = 50.0
_FOOTER_PT     = 25.0
_LEGEND_PT     = 16.0
_CBAR_W_PT     = 9.0        # bar thickness
_CBAR_PAD_PT   = 8.0        # gap between plot and bar
_CBAR_LBL_PT   = 52.0       # tick labels + rotated axis label beside a bar
# Sized so a per-cell bar's axis label clears the *next* cell's y-label:
# in "each" mode the two are only _WSPACE_PT apart, and at 34pt they
# overlapped into an unreadable "Amplitude (eq_hist)Time".

# NOTE on measuring fidelity: matplotlib places the axes bbox at exactly
# the requested device-pixel size, and imshow with interpolation="nearest"
# fills it without resampling -- verified via ax.get_window_extent().  A
# naive check that counts coloured pixels in the output will come up one
# row and one column short, because the axes spine overdraws the boundary
# pixels.  That is a frame drawn over the image, not data loss; assert on
# the bbox, not on pixel counts.  Do not "correct" it by padding the axes
# rect: a 900.5-pixel-wide axes forces a resample of a 900-pixel image,
# which is the very thing this layout exists to avoid.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _plain_text(text: str) -> str:
    """Flatten Bokeh ``Div`` markup to something a PNG can render.

    ``VisibilityPlotter._status_text()`` is written for a ``Div``, so it
    arrives as ``<b>name</b> | Layout: ...<br>Field: all | ...``.
    matplotlib draws that literally, tags and all.  Deriving the footer
    from ``_status_text()`` is still right — one summary, not two that
    drift — so the flattening happens here instead.
    """
    import html
    import re
    if not text:
        return text
    text = re.sub(r"<\s*br\s*/?\s*>", "   |   ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _is_degenerate(rng: tuple[float, float]) -> bool:
    """True when a range cannot be drawn (zero width or non-finite)."""
    lo, hi = rng
    return not (np.isfinite(lo) and np.isfinite(hi)) or lo == hi


def _union(extents):
    """Bounding box of a list of ``(x0, x1, y0, y1)``, or ``None``."""
    if not extents:
        return None
    xs = [v for e in extents for v in e[:2]]
    ys = [v for e in extents for v in e[2:]]
    return (min(xs), max(xs), min(ys), max(ys))


class _ExtentIndex:
    """Pass one of the two-pass render: what empty cells should inherit.

    Empty cells inherit an extent so their axes stay put across a
    sequence of exported files — flipping through per-antenna PNGs should
    not make the axes jump whenever an antenna happens to have no data.

    Inheritance is keyed on the axis label pair, not taken globally.  In
    grid mode every cell shares one configuration so the distinction is
    moot, but a duo export can pair a raster against a scatter, and
    letting an empty ``Amplitude vs UV Distance`` cell inherit a
    neighbour's ``Time vs Channel`` range would draw axes that are not
    merely wrong but confidently mislabelled.  The global union is kept
    only as a last resort for a panel whose labels match nothing.
    """

    def __init__(self, panels: Sequence[Optional[RenderedPanel]]):
        groups: dict[tuple[str, str], list] = {}
        donors: dict[tuple[str, str], PanelSpec] = {}
        for p in panels:
            if p is None or p.image is None or p.spec is None:
                continue
            x0, x1, y0, y1 = p.extent
            if _is_degenerate((x0, x1)) or _is_degenerate((y0, y1)):
                continue
            key = (p.spec.x_label, p.spec.y_label)
            groups.setdefault(key, []).append((x0, x1, y0, y1))
            donors.setdefault(key, p.spec)
        self._by_labels = {k: _union(v) for k, v in groups.items()}
        self._global    = _union([e for v in groups.values() for e in v])
        self._donors    = donors
        self._donor_any = next(iter(donors.values()), None)

    def lookup(self, spec: Optional[PanelSpec]):
        """``(extent, donor_spec)`` for an empty cell described by *spec*.

        The donor matters as much as the extent.  Elapsed-time ticks are
        measured from the axis's ``full_x0``/``full_y0``, and an empty
        panel's own range is whatever the degenerate branch left behind —
        ``(0.0, 1.0)`` — so formatting its inherited MJD-second extent
        against its own origin prints raw epoch seconds
        (``4800001750.0000``) instead of ``29m 10s``.  Borrow the
        neighbour's origin along with its range.
        """
        if spec is not None:
            key = (spec.x_label, spec.y_label)
            if key in self._by_labels:
                return self._by_labels[key], self._donors.get(key)
        return self._global, self._donor_any


def _band_swatch(band: ColorBand) -> str:
    """Representative colour for a legend entry.

    The high end of the band's colormap: scatter layers are told apart by
    hue, and the top of each layer's ramp is where that hue is most
    saturated.  Falls back to grey for a band with no cmap.
    """
    return band.cmap[-1] if band.cmap else "#888888"


# ---------------------------------------------------------------------------
# Cell rendering
# ---------------------------------------------------------------------------

def _style_axes(ax, theme: Theme, spec: Optional[PanelSpec],
                extent, show_ticks: bool,
                tick_src: Optional[PanelSpec] = None) -> None:
    """Apply theme, labels, and tick formatters to one cell's axes.

    *spec* supplies the axis label text; *tick_src* supplies the
    time-axis flags and origins, and defaults to *spec*.  They differ
    only for an empty cell, which keeps its own labels but borrows a
    populated neighbour's tick origin.
    """
    ax.set_facecolor(theme.axes)
    for side in ("top", "right", "bottom", "left"):
        ax.spines[side].set_color(theme.spine)
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors=theme.muted, labelsize=8, length=3, width=0.8)

    if not show_ticks or extent is None:
        # Nothing anywhere in the grid had a drawable range, so any tick
        # values would be the (0, 1) degenerate fallback -- numbers that
        # look like data and are not.  Draw the frame and nothing else.
        ax.set_xticks([])
        ax.set_yticks([])
        return

    x0, x1, y0, y1 = extent
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)

    if spec is not None:
        ax.set_xlabel(spec.x_label, color=theme.text, fontsize=9)
        ax.set_ylabel(spec.y_label, color=theme.text, fontsize=9)
    src = tick_src if tick_src is not None else spec
    if src is not None:
        # Elapsed-time labels are relative to the FULL extent's origin,
        # not the viewport's -- so a zoomed export keeps the same tick
        # vocabulary as the unzoomed one, exactly as the browser does.
        ax.xaxis.set_major_formatter(
            mpl_formatter(src.x_is_time, src.x_range[0]))
        ax.yaxis.set_major_formatter(
            mpl_formatter(src.y_is_time, src.y_range[0]))
    ax.grid(True, color=theme.grid, linewidth=0.5, alpha=0.6)
    ax.set_axisbelow(True)


def _legend_handles(bands, theme: Theme):
    """Swatch handles for the visible bands, or ``[]`` if fewer than two.

    Hidden bands are omitted: an entry for a layer the user turned off
    would claim something is on the plot that is not.  A single band
    needs no legend — the title already names the quantity.
    """
    from matplotlib.lines import Line2D

    visible = [b for b in bands if b.visible]
    if len(visible) < 2:
        return []
    return [
        Line2D([], [], marker="s", linestyle="none", markersize=6,
               markerfacecolor=_band_swatch(b), markeredgecolor="none",
               label=b.legend_label())
        for b in visible
    ]


def _style_legend(leg, theme: Theme) -> None:
    for txt in leg.get_texts():
        txt.set_color(theme.text)


def _draw_panel_legend(ax, spec: PanelSpec, theme: Theme) -> None:
    """Legend in the reserved band above the axes.

    Deliberately outside the data area.  An inside-the-axes legend
    (matplotlib's default ``loc="upper right"``) sits on top of exactly
    the corner a scatter of amplitude against uv-distance tends to
    occupy, and there is no "empty corner" heuristic that survives real
    data.  ``frameon=False`` because outside the frame there is nothing
    to separate it from.
    """
    handles = _legend_handles(spec.bands, theme)
    if not handles:
        return
    leg = ax.legend(handles=handles, loc="lower right",
                    bbox_to_anchor=(0.0, 1.0, 1.0, 0.0), ncol=len(handles),
                    frameon=False, fontsize=7.5, handletextpad=0.4,
                    columnspacing=1.2, borderaxespad=0.15)
    _style_legend(leg, theme)


def _draw_figure_legend(fig, bands, theme: Theme, y: float) -> None:
    """One legend for the whole figure, at figure fraction *y*.

    Used when every populated cell carries the same bands, which is the
    normal case in grid mode: one configuration iterated over an axis
    means N identical legends, and N-1 of them are noise.
    """
    handles = _legend_handles(bands, theme)
    if not handles:
        return
    leg = fig.legend(handles=handles, loc="upper center",
                     bbox_to_anchor=(0.5, y), ncol=len(handles),
                     frameon=False, fontsize=8.5, handletextpad=0.4,
                     columnspacing=1.6)
    _style_legend(leg, theme)


def _band_key(spec: Optional[PanelSpec]):
    """Identity of a panel's visible band set, for sameness testing."""
    if spec is None:
        return None
    return tuple((b.label, b.cmap) for b in spec.bands if b.visible)


def _resolve_legend(mode: str, cells) -> str:
    """Turn ``legend="auto"`` into ``"figure"``, ``"panel"``, or ``"none"``.

    ``"figure"`` when more than one populated cell shares one band set —
    grid mode's single-configuration model guarantees this, and it is
    also true of a duo showing the same scatter twice.  ``"panel"`` when
    cells differ, which is what a heterogeneous duo needs.
    """
    if mode != "auto":
        return mode
    keys = [_band_key(c.spec) for c in cells
            if c is not None and c.image is not None]
    keys = [k for k in keys if k]
    if not keys:
        return "none"
    if len(keys) > 1 and all(k == keys[0] for k in keys):
        return "figure"
    return "panel"


def _cbar_mappings(cells, include_density: bool = True):
    """Every drawable ``(spec, band)`` pair that could carry a colorbar.

    *include_density* is ``False`` under ``colorbar="auto"``, which is
    what keeps scatter density ramps out of a mixed duo.  Testing "are
    *all* bands density?" is not enough: a raster paired with a scatter
    has both, so the raster would drag the scatter's two density bars
    onto the figure alongside it.
    """
    out = []
    for c in cells:
        if c is None or c.image is None or c.spec is None:
            continue
        for b in c.spec.bands:
            if not b.visible or b.mapping is None:
                continue
            if not include_density and b.kind == "density":
                continue
            out.append((c.spec, b))
    return out


def _mappings_agree(pairs) -> bool:
    """True when one colorbar would be correct for every pair.

    For a raster this is ``color_mode="global"``: one reference
    distribution, so one curve.  For a scatter it additionally requires
    matching bin geometry, because density counts are only comparable
    between panels whose bins cover the same area.
    """
    if len(pairs) < 2:
        return True
    def key(sb):
        spec, b = sb
        k = (b.kind, b.cmap, b.scaling,
             round(float(b.mapping.vmin), 12),
             round(float(b.mapping.vmax), 12))
        if b.kind == "density":
            ax, ay = b.bin_area(spec)
            k += (round(ax, 9), round(ay, 9))
        return k
    first = key(pairs[0])
    return all(key(p) == first for p in pairs[1:])


def _resolve_colorbar(mode: str, cells) -> str:
    """Turn ``colorbar="auto"`` into ``"shared"``, ``"each"``, or ``"none"``.

    ``"auto"`` defaults scatter bands to ``"none"``: a scatter ramp is
    points per pixel, two plotted quantities mean two bars per cell, and
    in a grid that is a great deal of chrome for a number the legend
    already reports.  Raster bands share one bar when their mappings
    agree and take one each when they do not.

    Forcing ``"shared"`` on mappings that disagree is refused rather than
    obeyed: a single bar would be right for one cell and wrong for the
    rest, and a quietly wrong colorbar in a paper is worse than an
    unexpected layout.
    """
    pairs = _cbar_mappings(cells, include_density=(mode != "auto"))
    if not pairs:
        return "none"
    if mode == "auto":
        return "shared" if _mappings_agree(pairs) else "each"
    if mode == "shared" and not _mappings_agree(pairs):
        log.warning(
            "export_png: colorbar='shared' requested but panel mappings "
            "differ (scaling, value range, or bin area); falling back to "
            "'each' rather than drawing a bar that is wrong for most cells"
        )
        return "each"
    return mode


def _bar_bands(spec: Optional[PanelSpec], mode: str,
               include_density: bool = True):
    """Bands of *spec* that should get their own bar under *mode*."""
    if spec is None or mode != "each":
        return []
    return [b for b in spec.bands
            if b.visible and b.mapping is not None
            and (include_density or b.kind != "density")]


def _draw_colorbar(fig, rect, band: ColorBand, theme: Theme,
                   side: str, show_label: bool = True) -> None:
    """Draw one colorbar into figure-fraction *rect*.

    Placed in reserved space as its own axes rather than via
    ``make_axes_locatable``, which steals width from the parent axes and
    would break the 1:1 pixel mapping the whole layout is built around.

    ``FuncNorm`` over the band's ``ScalarMapping`` is what puts ticks at
    real data values under a non-linear scaling: ``eq_hist`` is a
    monotonic CDF interpolation, so it is invertible and the bar's ticks
    bunch where the data is dense — the information the scaling exists to
    show.
    """
    from matplotlib.colors import ListedColormap, FuncNorm
    import matplotlib.cm as cm

    m = band.mapping
    cax = fig.add_axes(rect)
    norm = FuncNorm((m.forward, m.inverse), vmin=m.vmin, vmax=m.vmax)
    sm = cm.ScalarMappable(norm=norm,
                           cmap=ListedColormap(list(band.cmap) or ["#888"]))
    orientation = "horizontal" if side == "bottom" else "vertical"
    cb = fig.colorbar(sm, cax=cax, orientation=orientation)
    try:
        cb.set_ticks(list(m.ticks(6)))
    except Exception as exc:                       # pragma: no cover
        log.debug("colorbar tick placement failed: %s", exc)
    if show_label:
        cb.set_label(band.bar_label(), color=theme.text, fontsize=8)
    cb.ax.tick_params(colors=theme.muted, labelsize=7, length=2.5, width=0.7)
    cb.outline.set_edgecolor(theme.spine)
    cb.outline.set_linewidth(0.7)
    if side == "left":
        cax.yaxis.set_ticks_position("left")
        cax.yaxis.set_label_position("left")


def _draw_cell(ax, panel: Optional[RenderedPanel], theme: Theme,
               extents: _ExtentIndex, legend_mode: str = "panel") -> None:
    """Render one grid cell — with data, empty, failed, or absent."""
    spec = panel.spec if panel is not None else None

    # A drawable panel uses its own extent.  Anything else inherits, and
    # must NOT fall back to its own spec ranges: VisibilityRaster's
    # degenerate branch leaves those at (0.0, 1.0), which passes every
    # "is this a valid range" test while being pure fiction.  Drawing
    # 0.0000-1.0000 ticks on an empty cell is worse than drawing none,
    # because they look like data.
    if panel is not None and panel.image is not None:
        extent, donor = panel.extent, spec
    else:
        extent, donor = extents.lookup(spec)

    _style_axes(ax, theme, spec, extent, show_ticks=extent is not None,
                tick_src=donor)

    if spec is not None and spec.title:
        # A panel legend occupies the band immediately above the axes, so
        # the title has to clear it -- at the default pad the two were
        # drawn on top of each other.
        pad = 6.0
        if legend_mode == "panel" and len(_legend_handles(
                spec.bands if spec else (), theme)) > 1:
            pad += _LEGEND_PT
        ax.set_title(spec.title, color=theme.text, fontsize=9.5, pad=pad)

    if panel is None:
        return

    if panel.image is not None:
        rgba = panel.rgba()
        # origin="lower": Bokeh's image_rgba puts array row 0 at the
        # bottom of the frame, and matplotlib defaults to the opposite.
        # Getting this wrong mirrors every plot vertically, which on a
        # time axis is very easy not to notice.
        # aspect="auto": the axes are time vs channel, amplitude vs
        # uvdist -- a 1:1 data aspect would be meaningless.
        # interpolation="nearest": the array is already at final
        # resolution; resampling would reintroduce the very question
        # _resample_method exists to answer.
        ax.imshow(rgba, extent=extent, origin="lower", aspect="auto",
                  interpolation="nearest", zorder=2)
        if legend_mode == "panel":
            _draw_panel_legend(ax, spec, theme)
        return

    # Empty or failed.  "No rows selected" and "the shade raised" are
    # different events and only the first is routine -- a pipeline
    # emitting 43 PNGs must not swallow the second silently, so failures
    # are coloured and labelled as failures.
    is_error = spec is not None and spec.status == "error"
    msg = (spec.note if spec is not None and spec.note else
           ("error" if is_error else "no data"))
    if is_error:
        msg = f"ERROR: {msg}"
    ax.text(0.5, 0.5, _wrap(msg, 34), transform=ax.transAxes,
            ha="center", va="center", fontsize=8,
            color=theme.error if is_error else theme.muted,
            style="normal" if is_error else "italic", zorder=3)


def _wrap(text: str, width: int) -> str:
    """Soft-wrap an annotation so a long skip-reason stays inside its cell."""
    import textwrap
    return "\n".join(textwrap.wrap(text, width)[:4])


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def export_png(
    panels: Sequence[Optional[RenderedPanel]],
    path: str,
    *,
    nrows: int = 1,
    ncols: int = 1,
    footer: Optional[str] = None,
    theme: str = "light",
    dpi: int = 100,
    cell_size: Optional[tuple[int, int]] = None,
    legend: str = "auto",
    colorbar: str = "auto",
    colorbar_side: str = "right",
) -> str:
    """Composite *panels* into a PNG at *path*; return the absolute path.

    Parameters
    ----------
    panels : sequence of RenderedPanel or None
        Row-major.  ``None`` means a grid position with no panel at all
        (an M x N grid larger than the iteration range); a
        ``RenderedPanel`` whose ``spec.status`` is not ``"ok"`` means a
        panel that exists but has nothing to draw.  Both get a framed
        cell; only the second gets a title and a note.
    nrows, ncols : int
        Grid shape.  Callers translate their own layout vocabulary before
        calling — ``layout="side"`` is ``(1, 2)``, and grid mode passes
        ``gridrows``/``gridcols``.
    footer : str
        Provenance line: dataset, selection, data column.  A PNG has no
        status bar and no sidebar, so anything the reader needs six
        months from now has to be drawn on.  Derive it from
        ``VisibilityPlotter._status_text()`` so the two cannot diverge.
    theme : {"light", "dark"}
        ``"light"`` by default: the GUI is dark, but a PNG is usually
        headed for a paper.
    dpi : int
        Scales the chrome only.  Panel images stay 1:1 with output
        pixels, so a panel's data area is exactly its own width and
        height in device pixels at any dpi -- which is also why
        byte-identity with the GUI holds only at matched canvas size.
        Raising dpi makes labels crisper and the data area physically
        smaller on the page, not softer.
    cell_size : (int, int)
        Override the per-cell data-area size in pixels.  Defaults to the
        first drawable panel's image shape.  Overriding does NOT rescale
        the image: it is drawn into whatever box is provided, so a size
        other than the panel's own breaks 1:1.
    legend : {"auto", "figure", "panel", "none"}
        Where to put the band legend.  ``"auto"`` uses one shared figure
        legend when every populated cell carries the same bands — the
        normal case in grid mode, where one configuration iterated over
        an axis would otherwise produce N identical legends — and falls
        back to per-panel for a heterogeneous grid.  Legends are never
        drawn over the data; space is reserved for them.
    colorbar : {"auto", "shared", "each", "none"}
        How many colorbars.  Deliberately separate from
        ``colorbar_side``: count and placement are independent choices,
        and fusing them into one enum makes some combinations
        inexpressible (per-plot bars gathered at the figure edge) while
        inventing meaningless ones.  ``"auto"`` gives raster panels one
        shared bar when their mappings agree and one each when they do
        not, and gives scatter panels none — see ``_resolve_colorbar``.
    colorbar_side : {"right", "left", "bottom"}
        Which edge.  For ``"shared"`` this is the figure edge; for
        ``"each"`` it is each plot's own edge, and ``"bottom"`` puts a
        horizontal bar under every cell.

    Returns
    -------
    str
        Absolute path written.
    """
    import matplotlib
    matplotlib.use("Agg")            # headless; no display, no browser
    import matplotlib.pyplot as plt

    if nrows < 1 or ncols < 1:
        raise ValueError(f"export_png: bad grid shape {nrows}x{ncols}")
    cells = list(panels)
    if len(cells) > nrows * ncols:
        raise ValueError(
            f"export_png: {len(cells)} panels exceeds {nrows}x{ncols} grid"
        )
    cells += [None] * (nrows * ncols - len(cells))

    th = THEMES.get(theme)
    if th is None:
        raise ValueError(f"export_png: unknown theme {theme!r}; "
                         f"expected one of {sorted(THEMES)}")

    # --- pass 1: geometry ------------------------------------------------
    extents = _ExtentIndex(cells)

    if cell_size is not None:
        cw, ch = cell_size
    else:
        first = next((p for p in cells if p is not None and p.image is not None),
                     None)
        ch, cw = (first.image.shape if first is not None else (480, 640))
    cw, ch = int(cw), int(ch)

    px = dpi / 72.0                       # points -> device pixels
    legend_mode = _resolve_legend(legend, cells)
    multi_band  = any(
        p is not None and p.spec is not None
        and sum(1 for b in p.spec.bands if b.visible) > 1
        for p in cells
    )
    if not multi_band:
        legend_mode = "none"
    m_l   = _MARGIN_L_PT * px
    m_r   = _MARGIN_R_PT * px
    m_t   = _MARGIN_T_PT * px
    m_b   = (_MARGIN_B_PT + (_FOOTER_PT if footer else 0.0)) * px
    title = _CELL_TITLE_PT * px
    wsp   = _WSPACE_PT * px
    hsp   = (_HSPACE_PT + _CELL_TITLE_PT) * px
    # Legend space is *reserved*, not overlaid: a band above each axes for
    # per-panel legends, or one band below the top margin for a shared
    # figure legend.  Either way the data area stays cw x ch.
    lg_panel = (_LEGEND_PT * px * 1.6) if legend_mode == "panel" else 0.0
    lg_fig   = (_LEGEND_PT * px * 1.4) if legend_mode == "figure" else 0.0

    # Colorbar space is reserved the same way legend space is.  Using
    # make_axes_locatable instead would take the width out of the parent
    # axes, shrinking the data area below cw x ch and forcing a resample
    # of the Datashader image.
    cbar_mode = _resolve_colorbar(colorbar, cells)
    if colorbar_side not in ("right", "left", "bottom"):
        raise ValueError(
            f"export_png: unknown colorbar_side {colorbar_side!r}; "
            f"expected 'right', 'left', or 'bottom'"
        )
    want_density = colorbar != "auto"
    n_bars_each = max(
        (len(_bar_bands(c.spec if c else None, cbar_mode, want_density))
         for c in cells),
        default=0,
    )
    bar_w   = _CBAR_W_PT * px
    bar_pad = _CBAR_PAD_PT * px
    bar_lbl = _CBAR_LBL_PT * px
    horiz   = colorbar_side == "bottom"

    if cbar_mode == "each" and n_bars_each:
        if horiz:
            cb_cell_w, cb_cell_h = 0.0, n_bars_each * (bar_pad + bar_w) + bar_lbl
        else:
            cb_cell_w, cb_cell_h = n_bars_each * (bar_pad + bar_w + bar_lbl), 0.0
    else:
        cb_cell_w = cb_cell_h = 0.0

    if cbar_mode == "shared":
        cb_fig_w = 0.0 if horiz else (bar_pad + bar_w + bar_lbl)
        cb_fig_h = (bar_pad + bar_w + bar_lbl * 0.6) if horiz else 0.0
    else:
        cb_fig_w = cb_fig_h = 0.0

    block  = lg_panel + ch + cb_cell_h
    # Rounded to whole pixels so the saved canvas size and the coordinate
    # space the axes rects are computed against are the same number.
    cell_w = cw + cb_cell_w
    fig_w  = float(round(m_l + ncols * cell_w + (ncols - 1) * wsp
                         + m_r + cb_fig_w))
    fig_h  = float(round(m_t + lg_fig + title
                         + nrows * block + (nrows - 1) * hsp
                         + m_b + cb_fig_h))

    fig = plt.figure(figsize=(fig_w / dpi, fig_h / dpi), dpi=dpi,
                     facecolor=th.figure)

    # --- pass 2: draw ----------------------------------------------------
    # Explicit per-cell rects rather than GridSpec: its wspace/hspace are
    # ratios of the average axes width, which makes exact pixel control
    # awkward, and exactness is the point -- the data area must be cw x ch
    # device pixels so the Datashader image is placed, not resampled.
    for i, panel in enumerate(cells):
        r, c = divmod(i, ncols)
        # Left-side bars sit between the margin and the plot, so the plot
        # starts further right; right-side bars sit after it.
        x_cell = m_l + c * (cell_w + wsp)
        x0 = x_cell + (cb_cell_w if colorbar_side == "left" else 0.0)
        y0 = fig_h - (m_t + lg_fig + title
                      + r * (block + hsp) + lg_panel + ch)
        ax = fig.add_axes([x0 / fig_w, y0 / fig_h, cw / fig_w, ch / fig_h])
        _draw_cell(ax, panel, th, extents, legend_mode)

        for j, band in enumerate(_bar_bands(
                panel.spec if panel else None, cbar_mode, want_density)):
            if horiz:
                by = y0 - bar_pad - (j + 1) * (bar_w + bar_pad)
                rect = [x0 / fig_w, by / fig_h, cw / fig_w, bar_w / fig_h]
            elif colorbar_side == "left":
                bx = x_cell + j * (bar_pad + bar_w + bar_lbl) + bar_lbl
                rect = [bx / fig_w, y0 / fig_h, bar_w / fig_w, ch / fig_h]
            else:
                bx = x0 + cw + bar_pad + j * (bar_pad + bar_w + bar_lbl)
                rect = [bx / fig_w, y0 / fig_h, bar_w / fig_w, ch / fig_h]
            _draw_colorbar(fig, rect, band, th, colorbar_side)

    if cbar_mode == "shared":
        pairs = _cbar_mappings(cells, include_density=want_density)
        band = pairs[0][1]
        if horiz:
            rect = [m_l / fig_w, (m_b * 0.62) / fig_h,
                    (ncols * cell_w + (ncols - 1) * wsp) / fig_w,
                    bar_w / fig_h]
        else:
            bx = fig_w - m_r - cb_fig_w + bar_pad
            top = m_t + lg_fig + title
            hgt = fig_h - top - m_b
            rect = [bx / fig_w, m_b / fig_h, bar_w / fig_w, hgt / fig_h]
        _draw_colorbar(fig, rect, band, th, colorbar_side)

    if legend_mode == "figure":
        shared = next((c.spec.bands for c in cells
                       if c is not None and c.image is not None
                       and _band_key(c.spec)), ())
        _draw_figure_legend(fig, shared, th,
                            1.0 - (m_t * 0.4) / fig_h)

    footer = _plain_text(footer) if footer else footer
    if footer:
        fig.text(m_l / fig_w, (_MARGIN_B_PT * 0.30 * px) / fig_h, footer,
                 color=th.muted, fontsize=7.5, ha="left", va="bottom")

    path = os.path.abspath(os.path.expanduser(path))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fig.savefig(path, dpi=dpi, facecolor=th.figure)
    plt.close(fig)
    log.info("export_png: wrote %s (%dx%d px, %dx%d grid)",
             path, fig_w, fig_h, nrows, ncols)
    return path
