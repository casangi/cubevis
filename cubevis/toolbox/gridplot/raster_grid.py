"""Where a raster's cells are: one definition, used by everything.

Why this exists
---------------
A visibility raster is a grid of cells whose coordinates are *not*
evenly spaced:

* **Time** has gaps between scans (and wherever integrations are
  missing).
* **Frequency** has gaps between spectral windows.
* **Baseline** is a list, not a continuum.  After a selection the
  baseline numbers that remain are an arbitrary subset (one antenna's
  baselines are numbers 1, 25, 49-71 of 325 on the TW Hya test data).

Until 2026-10-07 the image was drawn with ``datashader.Canvas.raster``,
which places an aggregate's rows and columns *evenly* between the first
and last coordinate, whatever the coordinate values are.  The axis
ticks, the cursor readout and flag boxes all worked from the real
coordinate values.  The two agree only on an axis without gaps; on the
TW Hya data, Baseline x Time over all fields, the row drawn at "t + 710
s" was the integration from t + 1173 s, while the readout and a flag box
at that height addressed t + 598 s.  A box drawn round a visible feature
could flag other data than it enclosed.

This module is the single place that says where a cell is.  The image
(:func:`resample`), the cursor readout (:func:`locate`), the flag engine
(:func:`overlapping`) and the flag overlays all use :func:`cell_edges`,
so they cannot disagree again.

The cell rule
-------------
Each coordinate is the centre of its cell.

* Neighbouring cells share an edge when they are as close as their own
  widths say they should be.  A cell's width is taken from its nearer
  neighbour, so a run of 6 s integrations has 6 s cells whatever lies
  beyond the run.
* Where the next coordinate is further away than that (more than
  ``GAP_FACTOR`` times the two half-widths), there is a **gap**: each
  cell keeps its own width and the space between belongs to no cell.  It
  is drawn blank, the readout says there is no data there, and a flag
  box that covers only the gap selects nothing.
* An *index* axis (Baseline position, Channel number) has cells of
  half-width 0.5 about each integer; pass ``half=0.5``.
* A cell with no neighbour close by (a lone integration between two
  gaps, a single channel, an axis of one cell) has nothing to take a
  width from: its nearer neighbour is across a gap, and until 2026-11-20
  (issue #38) such a cell was drawn as wide as that gap -- and two lone
  cells took their widths from each other and were drawn joined.  When
  the data says how wide a cell is (``width=``: the integration time,
  the channel width, times the stride after decimation), that width is
  the cell's own, in the gap test as well.  The widths travel with a
  raster aggregate as a coordinate on its dimension
  (:func:`cell_width_name`).

The baseline axis
-----------------
:class:`BaselineAxis` maps positions along a displayed Baseline axis
(0, 1, 2 ... with no holes) to baseline numbers, in number order or by
length.  The raster stores positions as the coordinate of its
``baseline_id`` dimension and converts back through this object wherever
a baseline has to be named: tick labels, the readout, flag requests.

HRS milestones H3 (ordering) and H4; see
``devel/notes/drs/visplot/hrs_h4_raster_axes.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

GAP_FACTOR = 1.5
"""Two neighbouring cells are separated by a gap when their centres are
more than this many times the sum of their half-widths apart.  1 would
call any timing jitter a gap; 2 would hide one missing integration in a
run.  1.5 sits between."""

BASELINE_ORDERS = ("number", "length")
"""Ways to order a displayed Baseline axis."""

DEFAULT_BASELINE_ORDER = "number"


def normalize_baseline_order(value) -> str:
    """Validate a baseline order; ``None`` gives the default."""
    if value is None:
        return DEFAULT_BASELINE_ORDER
    v = str(value).strip().lower()
    if v in ("id", "index"):
        v = "number"
    if v not in BASELINE_ORDERS:
        raise ValueError(
            f"baseline order must be one of {BASELINE_ORDERS}; got {value!r}")
    return v


# ---------------------------------------------------------------------- #
# Cells                                                                    #
# ---------------------------------------------------------------------- #

CELL_WIDTH_SUFFIX = "_cell_width"
"""A raster aggregate's true cell widths along dimension ``d`` ride as the
coordinate ``d + CELL_WIDTH_SUFFIX`` on ``d`` (issue #38)."""


def cell_width_name(dim: str) -> str:
    """The name of the cell-width coordinate on dimension *dim*."""
    return f"{dim}{CELL_WIDTH_SUFFIX}"


def cell_widths(agg, dim: str) -> Optional[np.ndarray]:
    """The true cell widths an aggregate carries along *dim* (aligned with
    its *dim* coordinate), or ``None`` when it carries none."""
    coords = getattr(agg, "coords", None)
    name = cell_width_name(dim)
    if coords is None or name not in coords:
        return None
    w = np.asarray(coords[name].values, dtype=np.float64).ravel()
    n = np.asarray(coords[dim].values).size if dim in coords else w.size
    return w if w.size == n else None


def _as_widths(width, n: int) -> Optional[np.ndarray]:
    """*width* (``None``, one number or one per cell) as *n* full widths;
    NaN where unknown."""
    if width is None:
        return None
    w = np.abs(np.asarray(width, dtype=np.float64).ravel())
    if w.size == 1:
        w = np.full(n, w[0])
    if w.size != n:
        raise ValueError(f"cell_edges: {w.size} widths for {n} coordinates")
    return w


def cell_edges(coords, half: Optional[float] = None,
               width=None) -> tuple[np.ndarray, np.ndarray]:
    """``(lo, hi)``: the edges of the cell centred on each of *coords*.

    *coords* may be in any order; the result is aligned with it.

    *half* fixes every cell's half-width (an index axis).

    *width* is how much of the axis each cell stands for -- the
    integration time or channel width, times the stride when the
    aggregate was decimated -- one number, or one per coordinate, NaN
    where unknown.  Where it is known it is the cell's own width: in the
    test for a gap, and for a side with no neighbour joined to it (see
    the module docstring).  Without it a cell's width is taken from its
    nearer neighbour, as before #38.  Joined cells share an edge as
    before, so a run whose spacing is up to ``GAP_FACTOR`` times the
    width is still drawn without stripes; a side with no joined
    neighbour is capped at half the distance to that neighbour, so cells
    never overlap.  A single coordinate with neither *half* nor *width*
    has zero width.
    """
    c = np.asarray(coords, dtype=np.float64).ravel()
    n = c.size
    if n == 0:
        return np.zeros(0), np.zeros(0)
    if half is not None:
        return c - half, c + half
    w = _as_widths(width, n)
    if n == 1:
        own = w[0] / 2.0 if w is not None else np.nan
        if np.isfinite(own) and own > 0:
            return c - own, c + own
        return c.copy(), c.copy()

    order = np.argsort(c, kind="stable")
    cs = c[order]
    d = np.diff(cs)
    # Half the distance to the nearer neighbour: the most a cell can
    # take without overlapping it, and its width when nothing says more.
    left = np.concatenate([[np.inf], d])
    right = np.concatenate([d, [np.inf]])
    h_near = np.minimum(left, right) / 2.0
    h = h_near
    if w is not None:
        own = w[order] / 2.0
        h = np.where(np.isfinite(own) & (own > 0), np.minimum(own, h_near), h_near)

    lo_s = cs - h
    hi_s = cs + h
    hsum = h[:-1] + h[1:]
    joined = d <= GAP_FACTOR * hsum
    # A shared edge divides the spacing in proportion to the two widths
    # (the midpoint when they are equal).
    with np.errstate(invalid="ignore", divide="ignore"):
        frac = np.where(hsum > 0, h[:-1] / hsum, 0.5)
    edge = cs[:-1] + d * frac
    hi_s[:-1] = np.where(joined, edge, hi_s[:-1])
    lo_s[1:] = np.where(joined, edge, lo_s[1:])

    lo = np.empty(n)
    hi = np.empty(n)
    lo[order] = lo_s
    hi[order] = hi_s
    return lo, hi


def extent(coords, half: Optional[float] = None,
           width=None) -> Optional[tuple[float, float]]:
    """Outer edges of the cells of *coords*, or ``None`` if there are none."""
    lo, hi = cell_edges(coords, half, width)
    if lo.size == 0:
        return None
    return float(lo.min()), float(hi.max())


def locate(coords, value: float, half: Optional[float] = None,
           width=None) -> int:
    """Index of the cell containing *value*, or ``-1`` (a gap, or outside)."""
    c = np.asarray(coords, dtype=np.float64).ravel()
    if c.size == 0:
        return -1
    lo, hi = cell_edges(c, half, width)
    inside = np.flatnonzero((lo <= value) & (value <= hi))
    if inside.size == 0:
        return -1
    if inside.size == 1:
        return int(inside[0])
    # On a shared edge: the nearer centre.
    return int(inside[np.argmin(np.abs(c[inside] - value))])


def overlapping(coords, lo: float, hi: float,
                half: Optional[float] = None, width=None) -> np.ndarray:
    """Mask of the cells of *coords* that overlap ``[lo, hi]``."""
    c = np.asarray(coords, dtype=np.float64).ravel()
    lo, hi = min(lo, hi), max(lo, hi)
    if c.size == 0:
        return np.zeros(0, dtype=bool)
    clo, chi = cell_edges(c, half, width)
    return (chi >= lo) & (clo <= hi)


# ---------------------------------------------------------------------- #
# Drawing                                                                  #
# ---------------------------------------------------------------------- #

def _pixel_cells(lo, hi, centres, r0: float, r1: float, n: int):
    """For *n* pixels across ``[r0, r1]``: the inclusive range of cells
    ``(first, last)`` each pixel shows; ``last < first`` means blank.

    A pixel shows the cells whose centres fall inside it (so several
    cells per pixel are averaged and none is counted twice), or, if none
    does, the one cell that contains the pixel's centre (so a cell
    larger than a pixel is drawn across its full width, with its true
    value).  Cells must be in ascending order.
    """
    edges = np.linspace(r0, r1, n + 1)
    # The last pixel includes its right edge.
    upper = edges[1:].copy()
    upper[-1] = np.nextafter(upper[-1], np.inf)
    first = np.searchsorted(centres, edges[:-1], side="left")
    last = np.searchsorted(centres, upper, side="left") - 1
    none = last < first

    mid = (edges[:-1] + edges[1:]) / 2.0
    m = len(lo)
    j = np.searchsorted(hi, mid, side="left")       # first cell ending at/after mid
    jc = np.minimum(j, m - 1)
    holds = (j < m) & (lo[jc] <= mid)
    first = np.where(none, np.where(holds, jc, 0), first)
    last = np.where(none, np.where(holds, jc, -1), last)
    return first.astype(np.intp), last.astype(np.intp)


def _reduce(sums: np.ndarray, counts: np.ndarray, first, last, axis: int):
    """Sum *sums* and *counts* over ``first..last`` along *axis*."""
    single = bool(np.all(last - first <= 0))
    if single:
        # One cell (or none) per pixel: copy, do not accumulate, so the
        # values drawn are exactly the values held.
        take = np.where(last >= first, first, 0)
        s = np.take(sums, take, axis=axis)
        c = np.take(counts, take, axis=axis)
        blank = last < first
        if blank.any():
            idx = [slice(None)] * sums.ndim
            idx[axis] = blank
            s[tuple(idx)] = 0.0
            c[tuple(idx)] = 0
        return s, c
    shape = list(sums.shape)
    shape[axis] += 1
    cs = np.zeros(shape, dtype=np.float64)
    cc = np.zeros(shape, dtype=np.int64)
    idx = [slice(None)] * sums.ndim
    idx[axis] = slice(1, None)
    np.cumsum(sums, axis=axis, out=cs[tuple(idx)])
    np.cumsum(counts, axis=axis, out=cc[tuple(idx)])
    hi_i = np.where(last >= first, last + 1, first)
    s = np.take(cs, hi_i, axis=axis) - np.take(cs, first, axis=axis)
    c = np.take(cc, hi_i, axis=axis) - np.take(cc, first, axis=axis)
    return s, c


def resample(values, y_coords, x_coords, x_range, y_range,
             width: int, height: int, *,
             y_half: Optional[float] = None, x_half: Optional[float] = None,
             y_width=None, x_width=None,
             how: str = "mean") -> np.ndarray:
    """Draw a grid of cells into a ``height x width`` array of pixels.

    Parameters
    ----------
    values :
        2-D, ``(len(y_coords), len(x_coords))``.  NaN is "no data".
    y_coords, x_coords :
        Cell centres, ascending.
    x_range, y_range :
        The data range the pixels span.  Row 0 of the result is the low
        end of *y_range*.
    y_half, x_half :
        Fixed half-width for an index axis (see :func:`cell_edges`).
    y_width, x_width :
        True cell widths (see :func:`cell_edges`).
    how :
        ``"mean"``: a pixel covering several cells shows the mean of
        their finite values.  ``"any"``: a pixel is 1.0 if any of its
        cells is non-zero and finite, else 0.0 (for masks; never NaN).

    Returns
    -------
    np.ndarray
        float64.  For ``"mean"``, NaN where a pixel falls in a gap,
        outside the grid, or only on cells without data.
    """
    v = np.asarray(values, dtype=np.float64)
    yc = np.asarray(y_coords, dtype=np.float64).ravel()
    xc = np.asarray(x_coords, dtype=np.float64).ravel()
    if v.ndim != 2 or v.shape != (yc.size, xc.size):
        raise ValueError(
            f"resample: values {v.shape} do not match coords "
            f"({yc.size}, {xc.size})")
    if how not in ("mean", "any"):
        raise ValueError(f"resample: how must be 'mean' or 'any'; got {how!r}")
    width, height = int(width), int(height)
    blank = np.zeros((height, width)) if how == "any" else np.full((height, width), np.nan)
    if yc.size == 0 or xc.size == 0 or width < 1 or height < 1:
        return blank
    x0, x1 = float(x_range[0]), float(x_range[1])
    y0, y1 = float(y_range[0]), float(y_range[1])
    flip_x, flip_y = x1 < x0, y1 < y0
    if flip_x:
        x0, x1 = x1, x0
    if flip_y:
        y0, y1 = y1, y0
    if x0 == x1 or y0 == y1:
        return blank

    ylo, yhi = cell_edges(yc, y_half, y_width)
    xlo, xhi = cell_edges(xc, x_half, x_width)
    fy, ly = _pixel_cells(ylo, yhi, yc, y0, y1, height)
    fx, lx = _pixel_cells(xlo, xhi, xc, x0, x1, width)

    finite = np.isfinite(v)
    if how == "any":
        sums = (finite & (v != 0)).astype(np.float64)
    else:
        sums = np.where(finite, v, 0.0)
    counts = finite.astype(np.int64)

    s, c = _reduce(sums, counts, fx, lx, axis=1)
    s, c = _reduce(s, c, fy, ly, axis=0)

    if how == "any":
        out = (s > 0).astype(np.float64)
    else:
        with np.errstate(invalid="ignore", divide="ignore"):
            out = np.where(c > 0, s / np.maximum(c, 1), np.nan)
    if flip_x:
        out = out[:, ::-1]
    if flip_y:
        out = out[::-1, :]
    return out


# ---------------------------------------------------------------------- #
# Baseline axis                                                            #
# ---------------------------------------------------------------------- #

def format_length(metres: Optional[float]) -> str:
    """A baseline length for a tick or a readout: ``"15.1 m"``,
    ``"1.24 km"``, ``"8611 km"``; ``""`` when unknown."""
    if metres is None:
        return ""
    try:
        m = float(metres)
    except (TypeError, ValueError):
        return ""
    if not np.isfinite(m):
        return ""
    if abs(m) < 999.5:
        return f"{m:.3g} m"
    return f"{m / 1000.0:.4g} km"


@dataclass(frozen=True)
class BaselineAxis:
    """Positions along a displayed Baseline axis.

    Position ``p`` (0-based, no holes) shows baseline number ``ids[p]``.

    Attributes
    ----------
    ids :
        Baseline numbers (the ``baseline_id`` the data and the sidebar's
        Baseline table use) in display order.
    order :
        ``"number"`` or ``"length"``: how *ids* were ordered.
    lengths :
        Length in metres of each displayed baseline (NaN if unknown),
        aligned with *ids*.
    names :
        ``"ANT1&ANT2"`` for each displayed baseline (``""`` if unknown).
    """
    ids:     tuple
    order:   str = DEFAULT_BASELINE_ORDER
    lengths: tuple = ()
    names:   tuple = ()

    HALF = 0.5
    """Half-width of a position's cell."""

    @classmethod
    def build(cls, present_ids, order: str = DEFAULT_BASELINE_ORDER,
              lengths: Optional[dict] = None,
              names: Optional[dict] = None) -> "BaselineAxis":
        """Order the baselines *present_ids*.

        *lengths* maps baseline number to metres and *names* to
        ``"A&B"``.  ``"length"`` puts the shortest first; baselines of
        unknown length go last, and ties (and everything, when no length
        is known) fall back to number order, so the result is always
        well defined.
        """
        order = normalize_baseline_order(order)
        ids = sorted({int(i) for i in np.asarray(present_ids).ravel().tolist()})
        lengths = lengths or {}
        names = names or {}

        def _len(i):
            v = lengths.get(i)
            try:
                v = float(v)
            except (TypeError, ValueError):
                return float("nan")
            return v

        if order == "length":
            ids.sort(key=lambda i: (not np.isfinite(_len(i)),
                                    _len(i) if np.isfinite(_len(i)) else 0.0, i))
        return cls(ids=tuple(ids), order=order,
                   lengths=tuple(_len(i) for i in ids),
                   names=tuple(str(names.get(i, "")) for i in ids))

    def __len__(self) -> int:
        return len(self.ids)

    @property
    def positions(self) -> np.ndarray:
        return np.arange(len(self.ids), dtype=np.float64)

    def positions_of(self, ids) -> np.ndarray:
        """Position of each baseline number in *ids*; ``-1`` if not shown."""
        return positions_of(self.ids, ids)

    def ids_in(self, lo: float, hi: float) -> list:
        """Baseline numbers whose cells overlap positions ``[lo, hi]``."""
        if not self.ids:
            return []
        m = overlapping(self.positions, lo, hi, half=self.HALF)
        return [self.ids[i] for i in np.flatnonzero(m)]

    def id_at(self, position: float) -> Optional[int]:
        """Baseline number shown at *position*, or ``None``."""
        if not self.ids:
            return None
        i = int(np.floor(float(position) + self.HALF))
        return self.ids[i] if 0 <= i < len(self.ids) else None

    def tick_labels(self) -> tuple:
        """What a tick at each position reads.

        Number order: the baseline number.  Length order: the length, so
        the axis reads as a (non-linear) scale of baseline length; the
        number where a length is unknown.
        """
        if self.order == "length":
            out = []
            for i, m in zip(self.ids, self.lengths):
                out.append(format_length(m) or f"#{i}")
            return tuple(out)
        return tuple(str(i) for i in self.ids)

    def describe(self, position: float) -> str:
        """``"DA41&DA42 (#0, 15.1 m)"`` for the readout and for messages."""
        i = int(np.floor(float(position) + self.HALF))
        if not (0 <= i < len(self.ids)):
            return ""
        name = self.names[i] if i < len(self.names) else ""
        length = format_length(self.lengths[i]) if i < len(self.lengths) else ""
        extra = ", ".join(x for x in (f"#{self.ids[i]}", length) if x)
        return f"{name} ({extra})" if name else extra


TIME_GAP_MODES = ("shown", "closed")
"""How a raster's Time axis treats stretches with no data (debug-0003)."""


def normalize_time_gaps(value) -> str:
    """``"shown"`` (the default: time is drawn to scale, a gap between
    scans is blank) or ``"closed"`` (:class:`TimeAxis`)."""
    v = str(value or "shown").strip().lower()
    if v in ("shown", "show", "linear", "false", "off", "no"):
        return "shown"
    if v in ("closed", "close", "true", "on", "yes"):
        return "closed"
    raise ValueError(f"time gaps must be 'shown' or 'closed'; got {value!r}")


@dataclass(frozen=True)
class TimeAxis:
    """A Time axis with its gaps closed (pre-preview feedback, debug-0003).

    A field observed for a few minutes at a time, with long stretches in
    between on other fields, fills a to-scale Time axis mostly with blank
    space (each scan a thin band).  Closed, the axis is laid out like a
    Baseline axis: one *position* per time cell, side by side, and a
    single blank position (:attr:`GAP`) where the cells had a gap
    between them (:func:`cell_edges`) -- AIPS's editors draw time the same
    way, one row per integration.  The coordinate of the raster's
    ``time`` dimension becomes the position, and whatever must name a
    time -- tick labels, the cursor readout, a flag box -- converts back
    through this object.

    Times that are not one of :attr:`times` (a flag overlay read at a
    slightly different time) are placed by interpolation within the run
    they fall in, and outside every run are not shown.

    Attributes
    ----------
    times :
        The cells' times (seconds, as the raster's ``time`` coordinate),
        ascending.
    lo, hi :
        Each cell's real edges (:func:`cell_edges` on *times*).
    positions :
        Each cell's position: ``0, 1, 2, ...``, plus :attr:`GAP` after
        each break.
    """
    times:     tuple
    lo:        tuple
    hi:        tuple
    positions: tuple

    HALF = 0.5
    """Half-width of a position's cell."""
    GAP = 1
    """Blank positions drawn where the cells had a gap."""

    @classmethod
    def build(cls, times, width=None) -> "TimeAxis":
        """*width*: the cells' true widths (one number, or one per entry
        of *times*), so a lone integration's real span is its own (#38)."""
        raw = np.asarray(times, dtype=np.float64).ravel()
        w = _as_widths(width, raw.size)
        ok = np.isfinite(raw)
        t, inv = np.unique(raw[ok], return_inverse=True)
        tw = None
        if w is not None:
            tw = np.full(t.size, np.nan)
            np.fmax.at(tw, inv, w[ok])
        lo, hi = cell_edges(t, width=tw)
        brk = np.zeros(t.size, dtype=np.int64)
        if t.size > 1:
            brk[1:] = (lo[1:] > hi[:-1]).astype(np.int64)
        pos = np.arange(t.size, dtype=np.float64) + cls.GAP * np.cumsum(brk)
        return cls(times=tuple(t.tolist()), lo=tuple(lo.tolist()), hi=tuple(hi.tolist()),
                   positions=tuple(pos.tolist()))

    def __len__(self) -> int:
        return len(self.times)

    @property
    def n_positions(self) -> int:
        """Positions along the axis, blank ones included."""
        return int(self.positions[-1]) + 1 if self.positions else 0

    def positions_of(self, times) -> np.ndarray:
        """Position of each of *times*; NaN where it lies in no cell.

        A time in a cell maps linearly across that cell's position
        (its centre to the cell's position), so a time between two
        neighbouring cells of a run lands between their positions."""
        t = np.asarray(times, dtype=np.float64).ravel()
        out = np.full(t.size, np.nan)
        if not self.times or t.size == 0:
            return out
        c = np.asarray(self.times)
        lo = np.asarray(self.lo)
        hi = np.asarray(self.hi)
        p = np.asarray(self.positions)
        k = np.clip(np.searchsorted(hi, t, side="left"), 0, c.size - 1)
        inside = (t >= lo[k]) & (t <= hi[k])
        # Within cell k: its centre is at p[k]; its edges at p[k] +- HALF.
        with np.errstate(invalid="ignore", divide="ignore"):
            left = np.where(t < c[k], (t - c[k]) / np.where(c[k] > lo[k], c[k] - lo[k], np.inf),
                            (t - c[k]) / np.where(hi[k] > c[k], hi[k] - c[k], np.inf))
        out[inside] = p[k][inside] + self.HALF * left[inside]
        return out

    def real_span(self, a: float, b: float) -> Optional[tuple]:
        """The real time range covered by the cells overlapping positions
        ``[a, b]``: ``(first cell's lower edge, last cell's upper edge)``,
        or ``None`` if the span holds no cell (all of it blank)."""
        if not self.times:
            return None
        a, b = min(a, b), max(a, b)
        m = overlapping(np.asarray(self.positions), a, b, half=self.HALF)
        idx = np.flatnonzero(m)
        if idx.size == 0:
            return None
        return float(self.lo[idx[0]]), float(self.hi[idx[-1]])

    def time_at(self, position: float) -> Optional[float]:
        """The time of the cell drawn at *position*, or ``None``."""
        i = self._index_at(position)
        return None if i is None else float(self.times[i])

    def cell_at(self, position: float) -> Optional[tuple]:
        """``(lo, hi)``: the real edges of the cell at *position*."""
        i = self._index_at(position)
        return None if i is None else (float(self.lo[i]), float(self.hi[i]))

    def _index_at(self, position: float) -> Optional[int]:
        if not self.times:
            return None
        p = np.asarray(self.positions)
        i = int(np.argmin(np.abs(p - float(position))))
        return i if abs(p[i] - float(position)) <= self.HALF + 1e-9 else None

    def tick_labels(self, fmt) -> tuple:
        """What a tick at each position (0 .. n_positions - 1) reads:
        ``fmt(time)`` at a cell, ``""`` at a blank position."""
        out = [""] * self.n_positions
        for t, p in zip(self.times, self.positions):
            out[int(p)] = fmt(t)
        return tuple(out)


def positions_of(order_ids: Sequence, ids) -> np.ndarray:
    """Position in *order_ids* of each of *ids* (``-1`` where absent).

    The flag engine uses this with the id list a flag request carries,
    so the box it resolves is in the same positions the image was drawn
    in.
    """
    order_ids = np.asarray(list(order_ids), dtype=np.int64).ravel()
    ids = np.asarray(ids).ravel()
    out = np.full(ids.size, -1, dtype=np.int64)
    if order_ids.size == 0 or ids.size == 0:
        return out
    try:
        ids_i = ids.astype(np.int64)
    except (TypeError, ValueError):
        return out
    sorter = np.argsort(order_ids, kind="stable")
    k = np.searchsorted(order_ids, ids_i, sorter=sorter)
    k = np.clip(k, 0, order_ids.size - 1)
    hit = order_ids[sorter[k]] == ids_i
    out[hit] = sorter[k][hit]
    return out
