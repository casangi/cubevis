"""Panel sizes: ``row_height`` and ``column_width`` (2026-10-13).

Every layout is a grid -- One is 1 x 1, Side by Side 1 x 2, Over / Under
2 x 1 -- and a panel's plot is its row's height by its column's width.
The size is the plot's figure (the plot with its axes, title and
toolbar), not the cursor readout and colour bars underneath it, so a
given ``row_height`` gives the same plot whatever is shown below.

Each argument is one of:

``None``
    The layout's default: heights 550 px (One, Side by Side) and 280 px
    (Over / Under); widths follow the window.
``"fit"``
    Share the window: rows divide the plot area's height (after the
    readouts and colour bars, never below ``FIT_MIN_HEIGHT``), columns
    its width.
a number of pixels
    Every row (column) that size.
a list of numbers
    One per row (column), in order; a short list repeats its last value.

Writing a file (``plotfile=``) has no window, so ``"fit"`` and ``None``
mean the file defaults (``FILE_WIDTH``); there the numbers are the size
of each plot's data area, as the GUI's Export PNG uses the on-screen
data area.
"""
from __future__ import annotations

from typing import Any, Optional, Union

Size = Union[None, str, int, list]

ROW_HEIGHT_DEFAULT = {"one": 550, "side": 550, "over": 280}
"""Row height by layout when ``row_height`` is not given (the sizes the
layout buttons have always used)."""

FILE_WIDTH = {"one": 1020, "side": 500, "over": 1020}
"""Column width for a file when ``column_width`` is ``None`` or ``"fit"``
(the GUI's widths at its usual window size)."""

FIT_MIN_HEIGHT = 250
"""``"fit"`` never makes a row shorter than this: the plot area scrolls
instead (Darrell, 2026-10-13: a fitted Over / Under with colour bars
would otherwise be too short to read on a laptop)."""

MIN_SIZE, MAX_SIZE = 100, 4000
MAX_ENTRIES = 2
"""Rows / columns a list may name: two panels for now (``MAX_PANELS``)."""

GRID = {"one": (1, 1), "side": (1, 2), "over": (2, 1)}


class SizeError(ValueError):
    pass


def _number(name: str, v: Any) -> int:
    if isinstance(v, bool):
        raise SizeError(f"{name}: {v!r} is not a size")
    if isinstance(v, str):
        t = v.strip().lower().removesuffix("px").strip()
        try:
            v = float(t)
        except ValueError:
            raise SizeError(f"{name}: {v!r} is not a size; give pixels, "
                            "a list of pixels, or 'fit'") from None
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        raise SizeError(f"{name}: {v!r} is not a size") from None
    if not MIN_SIZE <= n <= MAX_SIZE:
        raise SizeError(f"{name}: {n} px is outside {MIN_SIZE}-{MAX_SIZE} px")
    return n


def parse_size(name: str, value: Any) -> Size:
    """Normalise a ``row_height`` / ``column_width`` argument: ``None``,
    ``"fit"``, an int, or a list of ints.  Raises ``SizeError``."""
    if value is None:
        return None
    if isinstance(value, str):
        t = value.strip().lower()
        if t in ("", "default", "none"):
            return None
        if t == "fit":
            return "fit"
        if "," in t or t.startswith("["):
            parts = [p for p in t.strip("[]()").replace(" ", "").split(",") if p]
            return parse_size(name, parts)
        return _number(name, value)
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        if len(value) > MAX_ENTRIES:
            raise SizeError(f"{name}: at most {MAX_ENTRIES} values "
                            f"(one per row or column); got {len(value)}")
        vals = [_number(name, v) for v in value]
        return vals[0] if len(vals) == 1 else vals
    return _number(name, value)


def _per(spec: Size, n: int) -> list:
    """*spec* spread over *n* rows/columns (a short list repeats its last)."""
    if isinstance(spec, list):
        return [spec[min(i, len(spec) - 1)] for i in range(n)]
    return [spec] * n


def row_heights(spec: Size, layout: str) -> list:
    """Heights of the layout's rows: ints, or ``"fit"`` (the browser
    computes those)."""
    nrows = GRID.get(layout, (1, 2))[0]
    if spec is None:
        return [ROW_HEIGHT_DEFAULT.get(layout, 550)] * nrows
    return _per(spec, nrows)


def column_widths(spec: Size, layout: str, *, for_file: bool = False) -> list:
    """Widths of the layout's columns: ints, or ``None`` = follow the
    window (GUI).  For a file there is no window, so ``None`` / ``"fit"``
    give ``FILE_WIDTH``."""
    ncols = GRID.get(layout, (1, 2))[1]
    if spec in (None, "fit"):
        return [FILE_WIDTH.get(layout, 500) if for_file else None] * ncols
    return _per(spec, ncols)


def cell_sizes(row_spec: Size, col_spec: Size, layout: str) -> list:
    """``[(width, height), ...]`` for each panel position, in reading
    order, for a file (and the canvas a headless panel is drawn at)."""
    nrows, ncols = GRID.get(layout, (1, 2))
    hs = [h if isinstance(h, int) else ROW_HEIGHT_DEFAULT.get(layout, 550)
          for h in row_heights(row_spec, layout)]
    ws = column_widths(col_spec, layout, for_file=True)
    return [(ws[c], hs[r]) for r in range(nrows) for c in range(ncols)]


def size_value(spec: Size) -> Optional[Union[str, int, list]]:
    """The argument value that reproduces *spec* (``None`` = default)."""
    return list(spec) if isinstance(spec, list) else spec


def size_text(spec: Size) -> str:
    """Short text for the status line and summary page."""
    if spec is None:
        return "default"
    if spec == "fit":
        return "fit"
    if isinstance(spec, list):
        return ", ".join(f"{v} px" for v in spec)
    return f"{spec} px"
