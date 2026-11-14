"""vis_grid_source.py -- visplot's ``GridSource``: one cell per baseline,
antenna, SPW, field, scan or correlation, each drawn as the template panel
would draw it alone.

Stage G2 of ``devel/docs/gridplot/grid_and_calplot_plan.md`` (sections 3.2,
3.7, 11).  The grid view (``toolbox/gridplot/grid_view.py``) asks this for
labels, an extent, cells' images and probe text; this answers from a
headless copy of the template panel per cell:

* **the template** is a raster panel -- the plotter's grid panel (slot G,
  gridplot G3: ``grid_panel=``, or a copy of Panel A), which is never drawn
  itself: its axes, quantity, correlation, averaging, scaling and colormap
  are copied into every cell;
* **the selection** is the plotter's, narrowed per cell by the iteration
  value -- the grid iterates *within* the current selection (plan 1.3);
* **each cell is its own headless ``VisibilityRaster``**, kept (up to
  *keep* cells) so a zoom re-shades from what it holds (about 10 ms) and the
  probe reads the cell under the cursor, as a full panel's probe does.

Raster only for now: a scatter cell costs about 1.7 s through today's path
(G0, plan section 11), so scatter grids wait for read-once-and-split (G3).
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import replace
from typing import Optional, Sequence, Tuple

import numpy as np

log = logging.getLogger(__name__)

#: what a grid can iterate over, in the order the GUI offers them
ITERATE_BY = ("baseline", "antenna", "spw", "field", "scan", "correlation")


def _axis_kind(spec, axis: str) -> tuple:
    """What an axis shows, without its units or SPW: ``("Channel", False)``
    for "Channel (spw0)", ``("Frequency", False)`` for "Frequency [GHz]"."""
    label = spec.x_label if axis == "x" else spec.y_label
    is_time = spec.x_is_time if axis == "x" else spec.y_is_time
    return (str(label).split(" [")[0].split(" (")[0].strip(), bool(is_time))


class GridTemplateError(ValueError):
    """The template panel cannot be drawn as a grid (e.g. a scatter)."""


def normalize_iterate_by(value) -> str:
    v = str(value or "baseline").strip().lower()
    aliases = {"baselines": "baseline", "antennas": "antenna", "ant": "antenna",
               "spws": "spw", "fields": "field", "scans": "scan",
               "corr": "correlation", "correlations": "correlation",
               "pol": "correlation", "polarization": "correlation"}
    v = aliases.get(v, v)
    if v not in ITERATE_BY:
        raise ValueError(f"iterate_by must be one of {ITERATE_BY}; got {value!r}")
    return v


#: color scopes: each cell its own range; the page's cells one range; all
#: the grid's cells one range (found in the background, cell by cell)
COLOR_SCOPES = ("cell", "page", "grid")

#: values kept of each cell for a shared range (plus its exact min and
#: max): enough for an eq_hist curve, small enough for a big grid
_SAMPLE_MAX, _SAMPLE_MIN, _SAMPLE_TOTAL = 20000, 2000, 2_000_000


def _agg_sample(cell, size: int) -> Optional[np.ndarray]:
    """Up to *size* of *cell*'s finite aggregated values, evenly strided,
    with its exact minimum and maximum: ``None`` if it has none."""
    agg = getattr(cell, "_agg", None)
    if agg is None:
        return None
    v = np.asarray(agg.values, dtype=np.float64).ravel()
    v = v[np.isfinite(v)]
    if v.size == 0:
        return None
    if v.size > size:
        v = np.concatenate([v[:: -(-v.size // size)], [v.min(), v.max()]])
    return v


class VisGridSource:
    """``GridSource`` over a ``VisibilityPlotter``.

    Parameters
    ----------
    plotter : VisibilityPlotter
        Supplies the reader, the metadata and (by default) the selection
        and the template panel.
    iterate_by : one of ``ITERATE_BY``
    keep : how many cells' panels are kept (least recently drawn dropped)
    """

    COLOR_SCOPES = COLOR_SCOPES

    def __init__(self, plotter, iterate_by: str = "baseline", *, keep: int = 128) -> None:
        self._p = plotter
        self._keep = max(1, int(keep))
        self._lock = threading.RLock()
        self._cells: "OrderedDict[int, object]" = OrderedDict()
        self._iterate_by = normalize_iterate_by(iterate_by)
        self._template = None
        self._selection = None
        self._values: list = []
        self._labels: list = []
        self._extent: Optional[tuple] = None
        self._names = ("", "")
        self._union = None
        # shared color ranges: each cell's sample, the whole grid's pool,
        # and the background pass that fills them (stopped by a new gen)
        self._samples: dict = {}
        self._grid_ref: Optional[np.ndarray] = None
        self._gen = 0
        self._bg: Optional[threading.Thread] = None
        #: what the reader holds in memory for the cells (``_preload``)
        self.preload_info = {"key": None, "mb": 0.0, "reason": "", "s": 0.0}

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------
    @property
    def iterate_by(self) -> str:
        return self._iterate_by

    def configure(self, *, template=None, iterate_by=None, selection=None) -> "VisGridSource":
        """Take the template panel and selection as they are now, and list
        the values to iterate over.  Called before a grid is drawn (Plot in
        the Grid layout, or a headless export); cells drawn before are
        dropped."""
        from .visibility_raster import VisibilityRaster
        tmpl = template
        if tmpl is None:
            gs = getattr(self._p, "_grid_slot", None)
            tmpl = gs.active if gs is not None else self._p._pos0
        if not isinstance(tmpl, VisibilityRaster):
            raise GridTemplateError(
                "the Grid layout draws raster panels for now (a scatter cell costs "
                "about 1.7 s; scatter grids come with read-once-and-split, gridplot G3): "
                "set the grid's panel to Raster")
        with self._lock:
            if iterate_by is not None:
                self._iterate_by = normalize_iterate_by(iterate_by)
            self._template = tmpl
            self._selection = (selection if selection is not None
                               else self._p._selection).copy()
            self._values, self._labels = self._iteration_values()
            self._cells.clear()
            self._extent = None
            self._union = None
            self._samples.clear()
            self._grid_ref = None
            self._gen += 1
            self._names = (tmpl._panel_spec().axis_label("x"),
                           tmpl._panel_spec().axis_label("y"))
            self._preload()
        return self

    def _preload(self) -> None:
        """Read once, split (gridplot G3, 2026-11-13): the reader holds the
        grid's rows in memory, and every cell's selection (copied from
        ``self._selection``) carries the key, so each cell is a selection
        of rows already in memory instead of a read of its own.  Too big,
        or a reader without it: the cells read one by one, as before.
        ``CUBEVIS_VISPLOT_GRID_PRELOAD=0`` turns it off."""
        import os
        import time
        self._selection.preload = None
        self.preload_info = {"key": None, "mb": 0.0, "reason": "off", "s": 0.0}
        if os.environ.get("CUBEVIS_VISPLOT_GRID_PRELOAD", "1").strip() in ("0", "off", "no"):
            return
        reader = self._p._reader
        if not hasattr(reader, "preload") or not self._values:
            return
        t0 = time.perf_counter()
        try:
            info = dict(reader.preload(self._selection) or {})
        except Exception as exc:                            # noqa: BLE001 -- read one by one
            log.warning("grid: rows not held in memory (%s); the cells read one by one", exc)
            return
        info["s"] = round(time.perf_counter() - t0, 2)
        self.preload_info = info
        self._selection.preload = info.get("key")

    def _pairs_with_rows(self) -> list:
        """Baselines in the selection that have rows, in the metadata's
        order; all the metadata's baselines when the reader cannot tell.
        The selection's baselines or antennas narrow either list: the
        reader's answer does not apply them (seen 2026-11-12: stepping
        the Antenna stepper to DA49 left all 210 TW Hya baselines)."""
        meta = self._p._meta
        try:
            have = self._p._reader.baselines_with_rows(self._selection)
        except Exception:                                   # noqa: BLE001
            log.debug("baselines_with_rows failed", exc_info=True)
            have = None
        order = [tuple(b.pair) for b in (getattr(meta, "baselines", ()) or ())]
        if have is None:
            pairs = order
            auto = (self._selection.autocorr or "exclude")
            if auto == "exclude":
                pairs = [p for p in pairs if p[0] != p[1]]
            elif auto == "only":
                pairs = [p for p in pairs if p[0] == p[1]]
        else:
            have = {tuple(p) for p in have}
            known = [p for p in order if p in have]
            pairs = known + sorted(have - set(known))
        if self._selection.baselines:
            want = {tuple(b) for b in self._selection.baselines}
            pairs = [p for p in pairs if p in want or p[::-1] in want]
        elif self._selection.antenna_names:
            want = set(self._selection.antenna_names)
            pairs = [p for p in pairs if p[0] in want or p[1] in want]
        return pairs

    def _iteration_values(self) -> Tuple[list, list]:
        """``(values, labels)``: each value is a dict of selection fields
        the cell sets."""
        meta, sel, by = self._p._meta, self._selection, self._iterate_by
        if by == "baseline":
            pairs = self._pairs_with_rows()
            return ([{"baselines": [p], "antenna_names": None} for p in pairs],
                     [f"{a}-{b}" for a, b in pairs])
        if by == "antenna":
            pairs = self._pairs_with_rows()
            present = {a for p in pairs for a in p}
            names = [a.name for a in meta.antennas if a.name in present]
            if sel.antenna_names and not sel.baselines:
                names = [n for n in names if n in set(sel.antenna_names)]
            vals = []
            for n in names:
                mine = [p for p in pairs if n in p]
                vals.append({"baselines": mine, "antenna_names": None})
            return vals, names
        if by == "spw":
            ids = list(sel.spw) if sel.spw else [s.spw_id for s in meta.spws]
            return [{"spw": [i]} for i in ids], [f"SPW {i}" for i in ids]
        if by == "field":
            names = list(sel.field_names) if sel.field_names else [f.name for f in meta.fields]
            return [{"field_names": [n]} for n in names], list(names)
        if by == "scan":
            if sel.scan:
                scans = list(sel.scan)
            else:
                scans, seen = [], set()
                for s in meta.scans:
                    if sel.field_names and s.field_name not in set(sel.field_names):
                        continue
                    name = str(s.name)
                    if name not in seen:
                        seen.add(name)
                        scans.append(name)
            return [{"scan": [s]} for s in scans], [f"scan {s}" for s in scans]
        # correlation
        if sel.correlation:
            corrs = list(sel.correlation)
        else:
            from .visibility_plotter import _data_correlations
            corrs = list(_data_correlations(meta))
        return [{"correlation": [c], "_polarization": c} for c in corrs], list(corrs)

    # ------------------------------------------------------------------
    # GridSource
    # ------------------------------------------------------------------
    def labels(self) -> Sequence[str]:
        return list(self._labels)

    def values(self) -> list:
        return [dict(v) for v in self._values]

    def axis_names(self) -> Tuple[str, str]:
        return self._names

    def extent(self) -> Tuple[float, float, float, float]:
        """The cells' common extent: the whole selection drawn once by the
        template (one query) -- except along a Baseline axis when the grid
        splits the baselines (iterating by baseline or antenna), where it
        is the widest of the cells drawn so far."""
        with self._lock:
            if self._extent is None:
                self._extent = self._union_extent()
            return self._extent

    def _union_extent(self) -> tuple:
        """The template's extent (the selection drawn once), corrected per
        axis where a cell's axis is not the template's:

        * a Baseline axis the grid splits (iterating by baseline or
          antenna): the cell with the most baselines decides;
        * an axis of another kind -- a one-SPW cell draws Channel where the
          template over several SPWs draws Frequency: the cells' own
          extents, all of them (they are kept for drawing anyway; at most
          ``keep``)."""
        if not self._values:
            return (0.0, 1.0, 0.0, 1.0)
        from .axes import Axis
        # The template drawn with this selection is the union already (a
        # panel's Plot, or the plotter's first render); else draw it once.
        spec = self._template._panel_spec() if self._template is not None else None
        if spec is None or spec.status != "ok" or not self._template_current():
            spec = self._union_panel()._panel_spec()
        ext = {"x": list(map(float, spec.x_range)), "y": list(map(float, spec.y_range))}
        first = self._cell(0)._panel_spec()
        t = self._template
        dims = {"x": t._x_dim, "y": t._y_dim}
        for axis in ("x", "y"):
            if first.status == "ok" and _axis_kind(first, axis) != _axis_kind(spec, axis):
                rngs = []
                for i in range(min(len(self._values), self._keep)):
                    s = self._cell(i)._panel_spec()
                    if s.status == "ok":
                        rngs.append(s.x_range if axis == "x" else s.y_range)
                if len(self._values) > self._keep:
                    log.info("grid extent: the first %d of %d cells set the %s axis",
                             self._keep, len(self._values), axis)
                if rngs:
                    ext[axis] = [min(r[0] for r in rngs), max(r[1] for r in rngs)]
            elif dims[axis] == Axis.BASELINE and self._iterate_by in ("baseline", "antenna"):
                widest = max(range(len(self._values)),
                             key=lambda i: len(self._values[i].get("baselines") or ()))
                s = self._cell(widest)._panel_spec()
                ext[axis] = list(map(float, s.x_range if axis == "x" else s.y_range))
        return (ext["x"][0], ext["x"][1], ext["y"][0], ext["y"][1])

    def _template_current(self) -> bool:
        """Whether the template was last drawn with this selection's rows
        (so its extent is the cells' union)."""
        t = getattr(self._template, "_selection", None)
        if t is None:
            return False
        keys = ("field_names", "scan", "spw", "time_range", "uv_range", "baselines",
                "antenna_names", "freq_range", "channel_range", "correlation",
                "data_column", "autocorr")
        return all(getattr(t, k, None) == getattr(self._selection, k, None) for k in keys)

    def drill_target(self, index: int) -> dict:
        """What a drill-down selects in the full panel for cell *index*:
        ``{"baselines": [[a, b], ...]}``, ``{"antenna": name}``,
        ``{"spw": id}``, ``{"field": name}``, ``{"scan": name}`` or
        ``{"correlation": c}``."""
        v, by = self._values[index], self._iterate_by
        if by == "baseline":
            return {"baselines": [list(p) for p in v["baselines"]]}
        if by == "antenna":
            if self._selection.baselines:
                return {"baselines": [list(p) for p in v["baselines"]]}
            return {"antenna": self._labels[index]}
        if by == "spw":
            return {"spw": v["spw"][0]}
        if by == "field":
            return {"field": v["field_names"][0]}
        if by == "scan":
            return {"scan": v["scan"][0]}
        return {"correlation": v["correlation"][0]}

    def _union_panel(self):
        if self._union is None:
            from .refresh import RefreshLevel
            sel = self._selection.copy()
            if self._iterate_by in ("baseline", "antenna"):
                pairs = sorted({p for v in self._values for p in v["baselines"]})
                sel.baselines, sel.antenna_names = pairs or sel.baselines, None
            u = self._clone(sel, None)
            u.apply_refresh(RefreshLevel.QUERY)
            self._union = u
        return self._union

    def render(self, indices, viewport, size, *, color: str = "cell",
               page_indices=None):
        """The cells *indices*, drawn at *viewport* (or each its own) and
        *size*.  *color* (``COLOR_SCOPES``): ``"cell"``, each its own
        color range; ``"page"``, one range over *page_indices* (default
        *indices*); ``"grid"``, one over every cell, once the background
        pass has seen them all (until then, the page's: see
        ``color_state``)."""
        from ..gridplot.panel_spec import RenderedPanel
        w, h = int(size[0]), int(size[1])
        out = []
        with self._lock:
            shared = self._shared_reference(color, indices if page_indices is None
                                            else page_indices)
            for i in indices:
                i = int(i)
                if not (0 <= i < len(self._values)):
                    out.append(RenderedPanel(spec=self._empty_spec("", "no such cell")))
                    continue
                label = self._labels[i]
                try:
                    cell = self._cell(i)
                    cell._shared_reference = shared
                    cell._width, cell._height = w, h
                    if viewport is not None and getattr(cell, "_is_decimated", False):
                        try:
                            cell._ensure_detail(*viewport)
                        except Exception:                   # noqa: BLE001
                            log.debug("grid cell detail failed", exc_info=True)
                    rp = cell.render_result(tuple(viewport) if viewport is not None else None)
                    spec = replace(rp.spec, title=label)
                    if spec.status != "ok" and not spec.note:
                        spec = replace(spec, note="no data")
                    out.append(RenderedPanel(spec=spec, image=rp.image, viewport=rp.viewport))
                except Exception as exc:                    # noqa: BLE001 -- a cell, not the page
                    log.warning("grid cell %s failed: %s", label, exc)
                    out.append(RenderedPanel(spec=self._empty_spec(label, str(exc), "error")))
            if out and out[0].spec.status == "ok":
                self._names = (out[0].spec.axis_label("x"), out[0].spec.axis_label("y"))
        return out

    # ------------------------------------------------------------------
    # shared color ranges
    # ------------------------------------------------------------------
    def color_scope(self, color: str) -> str:
        """*color* checked against ``COLOR_SCOPES``."""
        color = str(color or "cell").lower()
        if color not in COLOR_SCOPES:
            raise ValueError(f"color must be one of {COLOR_SCOPES}, not {color!r}")
        return color

    def color_state(self, color: str) -> dict:
        """For the browser: ``pending`` (the whole grid's range is still
        being found) and ``note`` (how far it has got)."""
        if self.color_scope(color) != "grid" or self._grid_ref is not None:
            return {"pending": False, "note": ""}
        n = len(self._values)
        return {"pending": True,
                "note": f"grid color range: {len(self._samples)}/{n} cells"}

    def _sample_size(self) -> int:
        n = max(1, len(self._values))
        return int(min(_SAMPLE_MAX, max(_SAMPLE_MIN, _SAMPLE_TOTAL // n)))

    def _sample(self, i: int, cell=None) -> Optional[np.ndarray]:
        """Cell *i*'s sample, kept; *cell* if it is at hand, else the kept
        cell, else one queried for this and dropped (lock held)."""
        if i in self._samples:
            return self._samples[i]
        if cell is None:
            cell = self._cells.get(i)
        if cell is None:
            from .refresh import RefreshLevel
            sel = self._selection.copy()
            pol = None
            for k, v in self._values[i].items():
                if k == "_polarization":
                    pol = v
                else:
                    setattr(sel, k, list(v) if isinstance(v, list) else v)
            cell = self._clone(sel, pol)
            cell._values_only = True          # only its values are pooled
            cell.apply_refresh(RefreshLevel.QUERY)
        smp = _agg_sample(cell, self._sample_size())
        self._samples[i] = smp
        return smp

    @staticmethod
    def _pool(samples) -> Optional[np.ndarray]:
        parts = [s for s in samples if s is not None]
        return np.concatenate(parts) if parts else None

    def _shared_reference(self, color: str, page_indices) -> Optional[np.ndarray]:
        """The values the cells' colors are scaled against together (lock
        held): ``None`` for each cell its own."""
        color = self.color_scope(color)
        if color == "cell":
            return None
        if color == "grid":
            if self._grid_ref is None and len(self._samples) >= len(self._values):
                self._grid_ref = self._pool(self._samples.get(i) for i in
                                            range(len(self._values)))
            if self._grid_ref is not None:
                return self._grid_ref
            self._start_grid_pass()
        page = []
        for i in page_indices:
            i = int(i)
            if 0 <= i < len(self._values):
                try:
                    page.append(self._sample(i, self._cell(i)))
                except Exception:                           # noqa: BLE001 -- drawn as an error
                    log.debug("grid cell %d sample failed", i, exc_info=True)
        return self._pool(page)

    def _start_grid_pass(self) -> None:
        """Find every cell's sample in a thread, a cell at a time, so the
        page requests in between are not held up for long."""
        if self._bg is not None and self._bg.is_alive():
            return
        gen = self._gen

        def run():
            for i in range(len(self._values)):
                with self._lock:
                    if self._gen != gen:
                        return
                    if i in self._samples:
                        continue
                    try:
                        self._sample(i)
                    except Exception:                       # noqa: BLE001
                        log.debug("grid color pass: cell %d failed", i, exc_info=True)
                        self._samples[i] = None
            with self._lock:
                if self._gen == gen and self._grid_ref is None:
                    self._grid_ref = self._pool(self._samples.get(i) for i in
                                                range(len(self._values)))
        self._bg = threading.Thread(target=run, name="grid color range", daemon=True)
        self._bg.start()

    def wait_for_grid_range(self, timeout: Optional[float] = None) -> bool:
        """Block until the whole grid's range is known (headless use)."""
        with self._lock:
            if self._grid_ref is None:
                self._start_grid_pass()
            bg = self._bg
        if bg is not None:
            bg.join(timeout)
        return self._grid_ref is not None

    def probe(self, index: int, x: float, y: float) -> str:
        with self._lock:
            if not (0 <= index < len(self._values)):
                return ""
            label = self._labels[index]
            cell = self._cells.get(index)
            if cell is None:
                return f"<b>{label}</b>"
            env = cell._handle_probe({"x": x, "y": y})
        return f"<b>{label}</b> &nbsp;|&nbsp; {env.get('label', '')}"

    # ------------------------------------------------------------------
    # cells
    # ------------------------------------------------------------------
    def _cell(self, i: int):
        """Cell *i*'s panel, queried when first asked for."""
        from .refresh import RefreshLevel
        cell = self._cells.get(i)
        if cell is not None:
            self._cells.move_to_end(i)
            return cell
        sel = self._selection.copy()
        pol = None
        for k, v in self._values[i].items():
            if k == "_polarization":
                pol = v
            else:
                setattr(sel, k, list(v) if isinstance(v, list) else v)
        cell = self._clone(sel, pol)
        cell.apply_refresh(RefreshLevel.QUERY)
        self._cells[i] = cell
        while len(self._cells) > self._keep:
            self._cells.popitem(last=False)
        return cell

    def _clone(self, selection, polarization):
        """A headless raster drawn as the template is."""
        from .visibility_raster import VisibilityRaster
        t = self._template
        r = VisibilityRaster(
            backend=t._backend, selection=selection, y_dim=t._y_dim, x_dim=t._x_dim,
            quantity=t._quantity, polarization=polarization or t._polarization,
            width=t._width, height=t._height, cmap=t._cmap, max_cells=t._max_cells,
            color_mode=t._color_mode, scaling=t._scaling, scaling_alpha=t._scaling_alpha,
            scaling_gamma=t._scaling_gamma, raster_interpolate=t._raster_interpolate,
            averaging=t._averaging, detrend=t._detrend,
            stat_time_window=t._stat_time_window, stat_chan_window=t._stat_chan_window,
            baseline_order=t._baseline_order, baseline_combine=t._baseline_combine,
            headless=True, defer_initial_render=True)
        r._theme = getattr(t, "_theme", "dark")
        r._time_labels = getattr(t, "_time_labels", "utc")
        r._corr_choice = getattr(t, "_corr_choice", "auto")
        if t._scaling_vmin is not None or t._scaling_vmax is not None:
            try:
                r.update_scaling(vmin=t._scaling_vmin, vmax=t._scaling_vmax)
            except Exception:                               # noqa: BLE001
                log.debug("grid cell scaling copy failed", exc_info=True)
        return r

    def _empty_spec(self, title: str, note: str, status: str = "empty"):
        from ..gridplot.panel_spec import PanelSpec
        x0, x1, y0, y1 = self._extent or (0.0, 1.0, 0.0, 1.0)
        return PanelSpec(kind="raster", title=title, x_label=self._names[0],
                         y_label=self._names[1], x_range=(x0, x1), y_range=(y0, y1),
                         x_is_time=False, y_is_time=False, agg_n_x=0, agg_n_y=0,
                         color_mode="global", status=status, note=note,
                         theme=getattr(self._template, "_theme", "dark"))

    def set_theme(self, theme: str, ramp) -> None:
        """The plotter's theme changed: the kept cells (and the union) take
        the new palette -- a re-shade, no query."""
        with self._lock:
            for cell in list(self._cells.values()) + ([self._union] if self._union else []):
                cell._theme = theme
                try:
                    cell.set_cmap(ramp)
                except Exception:                           # noqa: BLE001
                    log.debug("grid cell re-color failed", exc_info=True)

    def close(self) -> None:
        with self._lock:
            key = getattr(self._selection, "preload", None) if self._selection else None
            if key and hasattr(self._p._reader, "drop_preload"):
                try:
                    self._p._reader.drop_preload(key)
                except Exception:                           # noqa: BLE001
                    log.debug("grid: drop_preload failed", exc_info=True)
            self._gen += 1
            self._samples.clear()
            self._grid_ref = None
            self._cells.clear()
            self._union = None
