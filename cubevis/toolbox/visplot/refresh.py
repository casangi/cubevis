"""
refresh.py
==========
How much work a change actually requires.

The problem
-----------
Not every change needs the same amount of recomputation, but without a
vocabulary for saying so, code tends to reach for the most expensive
option because it is the one that is certainly correct.  A palette
change is the clearest case: it alters no data, no aggregation and no
extent, yet the obvious implementation re-queries 30 million points.

``RefreshLevel`` names the rungs so a caller can ask for the minimum, and
so a reviewer can see at a glance whether a handler is doing more than it
needs to.

The ladder
----------
Ordered by cost, cheapest first.  Each level implies every cheaper one:
re-aggregating necessarily re-shades, re-querying necessarily
re-aggregates.

``CHROME``
    Nothing about the pixels changes.  Titles, axis labels, theme
    colours, tick formatting.  In the GUI this is usually pure
    JavaScript and never reaches Python at all.

``SHADE``
    The pixels change but the aggregation does not.  Colormap, transfer
    function (``scaling``/``alpha``/``gamma``/``vmin``/``vmax``), layer
    opacity, ``color_mode``, and panning or zooming *within* the cached
    extent.  Runs ``tf.shade`` over the cached ``agg`` (raster) or
    re-composites the cached layer aggregations (scatter).

``AGGREGATE``
    The aggregation changes but the underlying rows do not.  Canvas
    resize, and for scatter a viewport change that alters the binning of
    the cached DataFrames.  Re-runs ``Canvas.points`` over cached data.
    Meaningful only for scatter -- raster's ``query_raster`` fuses the
    query and the aggregation, so a raster ``AGGREGATE`` is a ``QUERY``.

``QUERY``
    New rows are needed: axis, quantity, polarisation, data column, or
    any ``SelectionSpec`` change.  The only level that touches the
    backend.

Why this exists as a module
---------------------------
It was written when the Light/Dark toggle needed to change palettes.
The first design left the plot stale and asked the user to press Plot,
on the assumption that the intermediate state was merely suboptimal.  It
is not: a theme change inverts the relationship between ramp and
background and costs about 2.5x contrast (see ``palettes.py``), so the
plot is *unreadable* until re-shaded.  Asking a user to press a button to
make an illegible plot legible is not a reasonable state to leave them
in.

The fix was not "re-query on toggle" but "notice that a palette change is
a ``SHADE``".  That reasoning generalises, hence the ladder rather than a
one-off fast path.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/refresh.py``
"""

from __future__ import annotations

from enum import IntEnum


class RefreshLevel(IntEnum):
    """How much recomputation a change requires; ordered by cost."""

    CHROME    = 0
    SHADE     = 1
    AGGREGATE = 2
    QUERY     = 3

    @property
    def touches_backend(self) -> bool:
        """``True`` only for ``QUERY`` — the level that can be slow."""
        return self is RefreshLevel.QUERY

    def __repr__(self) -> str:              # pragma: no cover
        return f"RefreshLevel.{self.name}"


# What each changeable thing costs.  Keys are the parameter names used by
# the plot classes and the j2p message fields, so a handler can look up
# its own field names directly.
#
# When adding a control, add it here.  An unknown key resolves to QUERY,
# which is safe -- it recomputes more than necessary rather than showing
# stale pixels -- but it is a silent performance cost, so a missing entry
# should be treated as an oversight rather than a default.
_LEVELS: dict[str, RefreshLevel] = {
    # -- chrome: no pixel changes ------------------------------------- #
    "theme_chrome":   RefreshLevel.CHROME,
    "title":          RefreshLevel.CHROME,
    "x_label":        RefreshLevel.CHROME,
    "y_label":        RefreshLevel.CHROME,
    "compact_toolbar": RefreshLevel.CHROME,

    # -- shade: same aggregation, different colours ------------------- #
    "cmap":           RefreshLevel.SHADE,
    "layer_cmaps":    RefreshLevel.SHADE,
    "raster_cmap":    RefreshLevel.SHADE,
    "scatter_cmap":   RefreshLevel.SHADE,
    "theme":          RefreshLevel.SHADE,   # via the palettes it selects
    "scaling":        RefreshLevel.SHADE,
    "scaling_alpha":  RefreshLevel.SHADE,
    "scaling_gamma":  RefreshLevel.SHADE,
    "scaling_vmin":   RefreshLevel.SHADE,
    "scaling_vmax":   RefreshLevel.SHADE,
    "alpha":          RefreshLevel.SHADE,
    "color_mode":     RefreshLevel.SHADE,
    "viewport":       RefreshLevel.SHADE,

    # -- aggregate: same rows, different binning ---------------------- #
    "width":          RefreshLevel.AGGREGATE,
    "height":         RefreshLevel.AGGREGATE,
    "raster_interpolate": RefreshLevel.AGGREGATE,

    # -- query: new rows ---------------------------------------------- #
    "x_axis":         RefreshLevel.QUERY,
    "y_axis":         RefreshLevel.QUERY,
    "quantity":       RefreshLevel.QUERY,
    "polarization":   RefreshLevel.QUERY,
    "layers":         RefreshLevel.QUERY,
    "selection":      RefreshLevel.QUERY,
    "data_column":    RefreshLevel.QUERY,
    "spw":            RefreshLevel.QUERY,
    "field":          RefreshLevel.QUERY,
    "correlation":    RefreshLevel.QUERY,
    "time_range":     RefreshLevel.QUERY,
    "freq_range":     RefreshLevel.QUERY,
    "channel_range":  RefreshLevel.QUERY,
}


def level_for(*changed: str) -> RefreshLevel:
    """Cheapest level that covers every named change.

    Unknown names resolve to ``QUERY``: recomputing too much is a
    performance cost, showing stale pixels is a correctness one, and only
    the second is a bug.
    """
    if not changed:
        return RefreshLevel.CHROME
    return max(_LEVELS.get(name, RefreshLevel.QUERY) for name in changed)


def known_changes() -> tuple[str, ...]:
    """Every change name the table covers, for tests and diagnostics."""
    return tuple(sorted(_LEVELS))
