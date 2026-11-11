"""What a grid shows: one plot definition repeated over an iteration axis.

A ``GridSource`` is the only thing an application gives the grid view
(``grid_view.GridView``): visplot's iterates a panel over baselines,
antennas, SPWs ...; calplot's over a calibration table's antennas.  The
view asks it for the cells of a page and draws them; it never sees data.
Plan section 3.2 (``devel/docs/gridplot/grid_and_calplot_plan.md``).

``SyntheticGridSource`` makes cells from arithmetic -- for tests, the demo,
and the grid view's own development -- with an optional cost per cell to
stand in for a real render.
"""
from __future__ import annotations

import itertools
import math
import time
from typing import Optional, Protocol, Sequence, Tuple, runtime_checkable

import numpy as np

from .panel_spec import PanelSpec, RenderedPanel

#: (x0, x1, y0, y1) in data coordinates
Viewport = Tuple[float, float, float, float]


@runtime_checkable
class GridSource(Protocol):
    """The cells of a grid, one per value of an iteration axis.

    Cells are addressed by *index*, 0 .. ``len(labels()) - 1``, in the
    order the grid fills them (row by row, page by page)."""

    def labels(self) -> Sequence[str]:
        """One title per cell ("ea01-ea02", "ea05", "spw 3" ...)."""
        ...

    def axis_names(self) -> Tuple[str, str]:
        """(x name, y name), the same for every cell (drawn once)."""
        ...

    def extent(self) -> Viewport:
        """The common extent: what a cell shows at a common scale before
        any zoom (the union of the cells' own extents)."""
        ...

    def render(self, indices: Sequence[int], viewport: Optional[Viewport],
               size: Tuple[int, int]) -> Sequence[RenderedPanel]:
        """The cells *indices*, each *size* (width, height) pixels.

        *viewport* None: each cell at its own full extent -- or, at a
        common scale, the view passes ``extent()``.  A ``RenderedPanel``
        whose ``image`` is None is drawn as empty, with its ``spec.note``
        as the reason."""
        ...

    def probe(self, index: int, x: float, y: float) -> str:
        """What the cursor is over in cell *index* (one line)."""
        ...


class SyntheticGridSource:
    """Cells from arithmetic: a waterfall-like raster or a scatter-like
    cloud per cell, different for each, deterministic.

    Parameters
    ----------
    n : number of cells (default 45: the baselines of 10 antennas)
    kind : "raster" or "scatter"
    cost : seconds each cell's render takes (``time.sleep``), to stand in
        for a real query; 0 by default
    """

    def __init__(self, n: int = 45, kind: str = "raster", cost: float = 0.0) -> None:
        if kind not in ("raster", "scatter"):
            raise ValueError(f"kind must be 'raster' or 'scatter', not {kind!r}")
        self.kind = kind
        self.cost = float(cost)
        nant = max(2, int(math.ceil((1 + math.sqrt(1 + 8 * n)) / 2)))   # nant(nant-1)/2 >= n
        ants = [f"ea{i:02d}" for i in range(1, nant + 1)]
        self._labels = [f"{a}-{b}" for a, b in itertools.combinations(ants, 2)][:n]
        self.renders = 0                     # how many cells rendered (tests)

    # -- GridSource ------------------------------------------------------
    def labels(self) -> Sequence[str]:
        return list(self._labels)

    def axis_names(self) -> Tuple[str, str]:
        return ("Time (s)", "Channel") if self.kind == "raster" else ("Time (s)", "Amplitude (Jy)")

    def extent(self) -> Viewport:
        return (0.0, 600.0, 0.0, 64.0) if self.kind == "raster" else (0.0, 600.0, 0.0, 2.0)

    def _value(self, i: int, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        rng = np.random.default_rng(1000 + i)
        a, b, c = rng.uniform(0.5, 2.0, 3)
        v = np.sin(x / (40 * a) + i) * np.cos(y / (6 * b)) + 0.3 * np.sin((x + y * c) / 25)
        if i % 7 == 3:                                       # an odd one out
            v = v + 1.5 * (np.abs(y - 20 - i % 30) < 2)
        return v

    def render(self, indices: Sequence[int], viewport: Optional[Viewport],
               size: Tuple[int, int]) -> Sequence[RenderedPanel]:
        w, h = max(2, int(size[0])), max(2, int(size[1]))
        out = []
        for i in indices:
            if self.cost:
                time.sleep(self.cost)
            self.renders += 1
            x0, x1, y0, y1 = viewport or self.extent()
            if not (0 <= i < len(self._labels)):
                out.append(RenderedPanel(spec=self._spec(i, x0, x1, y0, y1, "empty",
                                                         "no such cell")))
                continue
            xs = np.linspace(x0, x1, w)
            ys = np.linspace(y0, y1, h)
            if self.kind == "raster":
                v = self._value(i, xs[None, :], ys[:, None])
                img = _shade(v, -1.6, 2.8)
            else:
                rng = np.random.default_rng(i)
                t = rng.uniform(0, 600, 4000)
                amp = 1.0 + 0.25 * np.sin(t / 50 + i) + rng.normal(0, 0.08, t.size)
                if i % 7 == 3:
                    amp[(t > 300) & (t < 340)] += 0.6
                img = np.zeros((h, w), np.uint32)
                ok = (t >= x0) & (t <= x1) & (amp >= y0) & (amp <= y1)
                px = ((t[ok] - x0) / max(x1 - x0, 1e-9) * (w - 1)).astype(int)
                py = ((amp[ok] - y0) / max(y1 - y0, 1e-9) * (h - 1)).astype(int)
                img[py, px] = 0xFFB4772F                     # opaque, matplotlib blue (ABGR)
            out.append(RenderedPanel(spec=self._spec(i, x0, x1, y0, y1, "ok", None),
                                     image=img, viewport=(x0, x1, y0, y1)))
        return out

    def probe(self, index: int, x: float, y: float) -> str:
        if not (0 <= index < len(self._labels)):
            return ""
        xn, yn = self.axis_names()
        if self.kind == "raster":
            v = float(self._value(index, np.array(x), np.array(y)))
            return f"{self._labels[index]}  {xn} {x:.1f}  {yn} {y:.1f}  value {v:.3f}"
        return f"{self._labels[index]}  {xn} {x:.1f}  {yn} {y:.3f}"

    # -- helpers ----------------------------------------------------------
    def _spec(self, i, x0, x1, y0, y1, status, note) -> PanelSpec:
        xn, yn = self.axis_names()
        title = self._labels[i] if 0 <= i < len(self._labels) else ""
        return PanelSpec(kind=self.kind, title=title, x_label=xn, y_label=yn,
                         x_range=(x0, x1), y_range=(y0, y1), x_is_time=False, y_is_time=False,
                         agg_n_x=0, agg_n_y=0, color_mode="value", status=status, note=note,
                         theme="light")


def _shade(v: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """*v* through a small viridis-like ramp to opaque RGBA (uint32, ABGR)."""
    stops = np.array([[68, 1, 84], [59, 82, 139], [33, 145, 140], [94, 201, 98], [253, 231, 37]],
                     float)
    t = np.clip((v - lo) / (hi - lo), 0, 1) * (len(stops) - 1)
    k = np.minimum(t.astype(int), len(stops) - 2)
    f = (t - k)[..., None]
    rgb = (stops[k] * (1 - f) + stops[k + 1] * f).astype(np.uint32)
    return (0xFF000000 | (rgb[..., 2] << 16) | (rgb[..., 1] << 8) | rgb[..., 0]).astype(np.uint32)
