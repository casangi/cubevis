"""vis_grid_source.py -- visplot's ``GridSource``: one cell per baseline,
antenna, SPW, field, scan or correlation, each drawn as the template panel
would draw it alone.

Stage G2 of ``devel/docs/gridplot/grid_and_calplot_plan.md`` (sections 3.2,
3.7, 11).  The grid view (``toolbox/gridplot/grid_view.py``) asks this for
labels, an extent, cells' images and probe text; this answers from a
headless copy of the template panel per cell:

* **the template** is a raster or scatter panel -- the plotter's grid
  panel (slot G, gridplot G3: ``grid_panel=``, or a copy of Panel A), which
  is never drawn itself: its axes, quantity or layers, correlation,
  averaging, scaling and colormap are copied into every cell;
* **the selection** is the plotter's, narrowed per cell by the iteration
  value -- the grid iterates *within* the current selection (plan 1.3);
* **each cell is its own headless ``VisibilityRaster`` or
  ``VisibilityScatter``**, kept (up to *keep* cells) so a zoom re-shades
  from what it holds and the probe reads the cell under the cursor, as a
  full panel's probe does;
* **the rows are read once** (G3): the reader holds the grid's rows in
  memory, and a scatter cell's samples are cut from the union's frames.

Color scopes (one range per page or grid) are a raster's: a scatter cell
is shaded by its own density (eq_hist of its counts).

**Compound iteration** (G3 item 4): ``iterate_by="antenna,spw"`` is a cell
per antenna and SPW, the outer axis's values in turn and, within each,
the inner axis's values found in the selection narrowed to it (a field's
scans; a SPW's baselines with rows).

**Axes override** (G3 item 4, a hook: nothing in the GUI sets it yet):
``set_axes_override(fn)`` lets a cell draw other axes than the template
(July notes decision 8).  Such a cell is drawn at its own scale, and the
grid view leaves it out of the common scale's zoom.
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


_ITERATE_ALIASES = {"baselines": "baseline", "antennas": "antenna", "ant": "antenna",
                    "spws": "spw", "fields": "field", "scans": "scan",
                    "corr": "correlation", "correlations": "correlation",
                    "pol": "correlation", "polarization": "correlation"}


def iterate_axes(value) -> tuple:
    """``iterate_by`` as a tuple of one or two axes, outer first:
    ``"antenna"``, ``"antenna,spw"``, ``"antenna then spw"``,
    ``("antenna", "spw")`` (compound iteration, gridplot G3 item 4,
    2026-11-14: a cell per antenna and SPW, the SPWs of each antenna in
    turn).  Two axes at most, not the same twice, and not both Baseline
    and Antenna (a baseline is already one antenna's).  Raises
    ``ValueError``."""
    if value is None or (isinstance(value, str) and not value.strip()):
        parts = ["baseline"]
    elif isinstance(value, (list, tuple)):
        parts = [str(v) for v in value if str(v).strip()]
    else:
        v = str(value).lower()
        for sep in (" then ", ">", "/", "+", ";"):
            v = v.replace(sep, ",")
        parts = [p for p in v.split(",") if p.strip()]
    axes = []
    for p in parts:
        a = p.strip().lower()
        a = _ITERATE_ALIASES.get(a, a)
        if a not in ITERATE_BY:
            raise ValueError(f"iterate_by must be one or two of {ITERATE_BY} "
                             f"(e.g. 'antenna,spw'); got {value!r}")
        axes.append(a)
    if not axes:
        axes = ["baseline"]
    if len(axes) > 2:
        raise ValueError(f"iterate_by: two axes at most (outer, inner); got {value!r}")
    if len(axes) == 2 and axes[0] == axes[1]:
        raise ValueError(f"iterate_by: {axes[0]} twice in {value!r}")
    if set(axes) == {"baseline", "antenna"}:
        raise ValueError("iterate_by: baseline and antenna cannot be combined "
                         "(each baseline is already one antenna's)")
    return tuple(axes)


def normalize_iterate_by(value) -> str:
    """``iterate_by`` checked and spelled one way: ``"antenna"``, or two
    axes joined by a comma, outer first (``"antenna,spw"``)."""
    return ",".join(iterate_axes(value))


#: what a cell's axes override may change (``VisGridSource.set_axes_override``)
AXES_OVERRIDE_KEYS = {"raster": ("x_dim", "y_dim", "quantity"),
                      "scatter": ("x_axis", "y_axis")}


def _as_axis(value):
    """An ``Axis`` from an ``Axis``, its name (``"TIME"``) or its label
    (``"Time"``)."""
    from .axes import Axis
    if isinstance(value, Axis):
        return value
    v = str(value).strip()
    try:
        return Axis[v.upper().replace(" ", "_")]
    except KeyError:
        pass
    for a in Axis:
        if str(a.label).lower() == v.lower():
            return a
    raise ValueError(f"axes override: no axis {value!r}")


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
        #: each cell's label per iteration axis, outer first
        self._parts: list = []
        #: ``fn(index, label, value) -> dict | None`` (``set_axes_override``)
        self._axes_override = None
        #: each cell's axes override, checked (None: the template's)
        self._axes: list = []
        self._template = None
        self._selection = None
        self._values: list = []
        self._labels: list = []
        self._extent: Optional[tuple] = None
        self._names = ("", "")
        self._union = None
        self._kind = "raster"          # the template's: "raster" or "scatter"
        # shared color ranges: each cell's sample, the whole grid's pool,
        # and the background pass that fills them (stopped by a new gen)
        self._samples: dict = {}
        self._grid_ref: Optional[np.ndarray] = None
        self._gen = 0
        self._bg: Optional[threading.Thread] = None
        #: what the reader holds in memory for the cells (``_preload``)
        self.preload_info = {"key": None, "mb": 0.0, "reason": "", "s": 0.0}
        #: changes whenever drawn cells are out of date (``configure``,
        #: ``sync``): the browser's page cache is only good for one version
        self.version = 0

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
        from .visibility_scatter import VisibilityScatter
        tmpl = template
        if tmpl is None:
            gs = getattr(self._p, "_grid_slot", None)
            tmpl = gs.active if gs is not None else self._p._pos0
        if not isinstance(tmpl, (VisibilityRaster, VisibilityScatter)):
            raise GridTemplateError(
                f"the Grid layout draws raster or scatter panels, not {type(tmpl).__name__}")
        with self._lock:
            # Scatter grids (gridplot G3 item 3, 2026-11-13): their cells
            # read from the rows held in memory, cut from the union's frames
            # (``XArrayReader._split_from_union``).
            self._kind = "raster" if isinstance(tmpl, VisibilityRaster) else "scatter"
            if iterate_by is not None:
                self._iterate_by = normalize_iterate_by(iterate_by)
            self._template = tmpl
            self._selection = self._fresh((selection if selection is not None
                                           else self._p._selection).copy())
            self._values, self._labels, self._parts = self._iteration_values()
            self._axes = self._cell_axes()
            self._cells.clear()
            self._extent = None
            self._union = None
            self._samples.clear()
            self._grid_ref = None
            self._gen += 1
            self.version += 1
            self._names = (tmpl._panel_spec().axis_label("x"),
                           tmpl._panel_spec().axis_label("y"))
            self._preload()
        return self

    # ------------------------------------------------------------------
    # flags: the cells follow the plotter's pending flags and commits
    # ------------------------------------------------------------------
    def _freshness(self) -> tuple:
        """The plotter's ``(pending_version, flag_view, cache_generation)``
        now: what the cells' selection must carry to show the flags as the
        panels do (FlagController's ``stamp_selection``; a commit's
        ``_after_disk_change``)."""
        p = self._p
        sel = getattr(p, "_selection", None)
        flags = getattr(p, "_flags", None)
        pv = getattr(flags, "state_version", None)
        if pv is None:
            pv = getattr(sel, "pending_version", 0)
        view = getattr(sel, "flag_view", None)
        if flags is not None and hasattr(flags, "main_view"):
            try:
                view = flags.main_view()
            except Exception:                               # noqa: BLE001
                log.debug("grid: flag view unknown", exc_info=True)
        gen = getattr(p, "_cache_generation", None)
        if gen is None:
            gen = getattr(sel, "cache_generation", 0)
        return int(pv or 0), view, int(gen or 0)

    def _fresh(self, sel):
        """*sel* with the plotter's flag state (``_freshness``)."""
        pv, view, gen = self._freshness()
        changes = {"pending_version": pv, "cache_generation": gen}
        if view is not None and hasattr(sel, "flag_view"):
            changes["flag_view"] = view
        return replace(sel, **changes)

    def sync(self) -> bool:
        """Bring the cells up to the plotter's flags: after a flag is added
        or undone (in a drilled-down cell, say), or a commit, the cells kept
        and the shared color ranges are dropped, and after a commit the rows
        held in memory are read again.  True if anything changed (the
        grid's pages must be drawn again).  Found 2026-11-17: without it the
        grid showed the flags of its last Plot until the next Plot in Grid."""
        with self._lock:
            sel = self._selection
            if sel is None:
                return False
            new = self._fresh(sel)
            old_key = (sel.pending_version, getattr(sel, "flag_view", None),
                       sel.cache_generation)
            new_key = (new.pending_version, getattr(new, "flag_view", None),
                       new.cache_generation)
            if old_key == new_key:
                return False
            self._selection = new
            self._cells.clear()
            self._union = None
            self._samples.clear()
            self._grid_ref = None
            self._gen += 1
            self.version += 1
            if new.cache_generation != sel.cache_generation:
                # the flags on disk changed: the rows held are stale
                self._preload()
            return True

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

    def _pairs_with_rows(self, sel=None) -> list:
        """Baselines in the selection that have rows, in the metadata's
        order; all the metadata's baselines when the reader cannot tell.
        The selection's baselines or antennas narrow either list: the
        reader's answer does not apply them (seen 2026-11-12: stepping
        the Antenna stepper to DA49 left all 210 TW Hya baselines)."""
        meta = self._p._meta
        sel = self._selection if sel is None else sel
        try:
            have = self._p._reader.baselines_with_rows(sel)
        except Exception:                                   # noqa: BLE001
            log.debug("baselines_with_rows failed", exc_info=True)
            have = None
        order = [tuple(b.pair) for b in (getattr(meta, "baselines", ()) or ())]
        if have is None:
            pairs = order
            auto = (sel.autocorr or "exclude")
            if auto == "exclude":
                pairs = [p for p in pairs if p[0] != p[1]]
            elif auto == "only":
                pairs = [p for p in pairs if p[0] == p[1]]
        else:
            have = {tuple(p) for p in have}
            known = [p for p in order if p in have]
            pairs = known + sorted(have - set(known))
        if sel.baselines:
            want = {tuple(b) for b in sel.baselines}
            pairs = [p for p in pairs if p in want or p[::-1] in want]
        elif sel.antenna_names:
            want = set(sel.antenna_names)
            pairs = [p for p in pairs if p[0] in want or p[1] in want]
        return pairs

    @property
    def axes(self) -> tuple:
        """The iteration axes, outer first."""
        return iterate_axes(self._iterate_by)

    @property
    def splits_baselines(self) -> bool:
        """Whether the cells split the baselines (iterating by baseline or
        antenna, alone or in a compound)."""
        return bool({"baseline", "antenna"} & set(self.axes))

    @staticmethod
    def _narrowed(sel, value: dict):
        """*sel* narrowed to an iteration value."""
        out = sel.copy()
        for k, v in value.items():
            if not k.startswith("_"):
                setattr(out, k, list(v) if isinstance(v, list) else v)
        return out

    def _iteration_values(self) -> Tuple[list, list, list]:
        """``(values, labels, parts)``: each value is a dict of selection
        fields the cell sets; *parts* is each cell's label per axis.  For
        two axes the inner one's values are found in the selection
        narrowed to each outer value, so a field's cells are its scans,
        and a SPW's its baselines with rows."""
        axes = self.axes
        vals, labs = self._axis_values(axes[0], self._selection)
        if len(axes) == 1:
            return vals, labs, [(lab,) for lab in labs]
        values, labels, parts = [], [], []
        for v, lab in zip(vals, labs):
            iv, il = self._axis_values(axes[1], self._narrowed(self._selection, v))
            for v2, lab2 in zip(iv, il):
                values.append({**v, **v2})
                labels.append(f"{lab} · {lab2}")
                parts.append((lab, lab2))
        return values, labels, parts

    def _axis_values(self, by: str, sel) -> Tuple[list, list]:
        """One axis's ``(values, labels)`` within *sel*."""
        meta = self._p._meta
        if by == "baseline":
            pairs = self._pairs_with_rows(sel)
            return ([{"baselines": [p], "antenna_names": None} for p in pairs],
                     [f"{a}-{b}" for a, b in pairs])
        if by == "antenna":
            pairs = self._pairs_with_rows(sel)
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
        # cells with their own axes (an override) have their own extents
        common = [i for i in range(len(self._values)) if not self.own_scale(i)]
        if not common:
            return (ext["x"][0], ext["x"][1], ext["y"][0], ext["y"][1])
        first = self._cell(common[0])._panel_spec()
        t = self._template
        dims = ({"x": t._x_dim, "y": t._y_dim} if self._kind == "raster"
                else {"x": t._x_dim, "y": t._layers[0].y_axis})
        for axis in ("x", "y"):
            if first.status == "ok" and _axis_kind(first, axis) != _axis_kind(spec, axis):
                rngs = []
                for i in common[:self._keep]:
                    s = self._cell(i)._panel_spec()
                    if s.status == "ok":
                        rngs.append(s.x_range if axis == "x" else s.y_range)
                if len(common) > self._keep:
                    log.info("grid extent: the first %d of %d cells set the %s axis",
                             self._keep, len(self._values), axis)
                if rngs:
                    ext[axis] = [min(r[0] for r in rngs), max(r[1] for r in rngs)]
            elif dims[axis] == Axis.BASELINE and self.splits_baselines:
                widest = max(common,
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
        ``{"correlation": c}`` -- one key per iteration axis."""
        v, out = self._values[index], {}
        for by, label in zip(self.axes, self._parts[index]):
            if by == "baseline":
                out["baselines"] = [list(p) for p in v["baselines"]]
            elif by == "antenna":
                if self._selection.baselines:
                    out["baselines"] = [list(p) for p in v["baselines"]]
                else:
                    out["antenna"] = label
            elif by == "spw":
                out["spw"] = v["spw"][0]
            elif by == "field":
                out["field"] = v["field_names"][0]
            elif by == "scan":
                out["scan"] = v["scan"][0]
            else:
                out["correlation"] = v["correlation"][0]
        return out

    def _union_panel(self):
        if self._union is None:
            from .refresh import RefreshLevel
            sel = self._selection.copy()
            # A scatter's union is the selection as it is: the same frames
            # the cells are cut from (``_split_from_union``), read once.
            if self.splits_baselines and self._kind == "raster":
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
            self.sync()
            shared = self._shared_reference(color, indices if page_indices is None
                                            else page_indices)
            for i in indices:
                i = int(i)
                if not (0 <= i < len(self._values)):
                    out.append(RenderedPanel(spec=self._empty_spec("", "no such cell")))
                    continue
                label = self._labels[i]
                own = self.own_scale(i)
                vp = None if own else viewport
                try:
                    cell = self._cell(i)
                    # a cell with its own axes has its own color range too
                    cell._shared_reference = None if own else shared
                    cell._width, cell._height = w, h
                    if vp is not None and getattr(cell, "_is_decimated", False):
                        try:
                            cell._ensure_detail(*vp)
                        except Exception:                   # noqa: BLE001
                            log.debug("grid cell detail failed", exc_info=True)
                    rp = cell.render_result(tuple(vp) if vp is not None else None)
                    if self._kind == "scatter" and vp is not None:
                        # the probe's range check is against what is on
                        # screen: the grid's extent, not the cell's own
                        cell._current_viewport = tuple(map(float, vp))
                    spec = replace(rp.spec, title=label)
                    if spec.status != "ok" and not spec.note:
                        spec = replace(spec, note="no data")
                    out.append(RenderedPanel(spec=spec, image=rp.image, viewport=rp.viewport,
                                             own_scale=own))
                except Exception as exc:                    # noqa: BLE001 -- a cell, not the page
                    log.warning("grid cell %s failed: %s", label, exc)
                    out.append(RenderedPanel(spec=self._empty_spec(label, str(exc), "error")))
            first = next((rp for i, rp in zip(indices, out)
                          if rp.spec.status == "ok" and not self.own_scale(int(i))), None)
            if first is not None:
                self._names = (first.spec.axis_label("x"), first.spec.axis_label("y"))
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
        if self._kind == "scatter" and self.color_scope(color) != "cell":
            return {"pending": False,
                    "note": "scatter: each cell its own density scale"}
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
            cell = self._new_cell(i)
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
        if color == "cell" or self._kind == "scatter":
            # a scatter cell's colors are its own density scale (eq_hist of
            # its counts); one scale across cells is a raster's
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
        cell = self._new_cell(i)
        cell.apply_refresh(RefreshLevel.QUERY)
        self._cells[i] = cell
        while len(self._cells) > self._keep:
            self._cells.popitem(last=False)
        return cell

    def _new_cell(self, i: int):
        """Cell *i*'s panel, not yet queried: the template's, narrowed to
        the cell's value, with its axes override if it has one."""
        pol = self._values[i].get("_polarization")
        axes = self._axes[i] if i < len(self._axes) else None
        return self._clone(self._narrowed(self._selection, self._values[i]), pol, axes)

    # ------------------------------------------------------------------
    # axes override (a hook, G3 item 4)
    # ------------------------------------------------------------------
    def set_axes_override(self, fn) -> None:
        """Let cells draw other axes than the template (July notes
        decision 8; no GUI sets it yet).  *fn* is ``fn(index, label,
        value) -> dict | None``, or a dict of label to override; an
        override sets some of ``AXES_OVERRIDE_KEYS`` (a raster's
        ``x_dim``, ``y_dim``, ``quantity``; a scatter's ``x_axis``,
        ``y_axis``), each an ``Axis`` or its name.  ``None`` clears it.
        Takes effect at the next ``configure``.  A cell with an override
        is drawn at its own scale (``own_scale``)."""
        if fn is not None and not callable(fn):
            table = dict(fn)
            fn = lambda i, label, value: table.get(label)      # noqa: E731
        with self._lock:
            self._axes_override = fn

    def _cell_axes(self) -> list:
        """Each cell's override, checked; None where it draws the
        template's axes."""
        fn = self._axes_override
        if fn is None:
            return [None] * len(self._values)
        allowed = AXES_OVERRIDE_KEYS[self._kind]
        out = []
        for i, (lab, v) in enumerate(zip(self._labels, self._values)):
            o = fn(i, lab, dict(v))
            if not o:
                out.append(None)
                continue
            bad = set(o) - set(allowed)
            if bad:
                raise ValueError(f"axes override for {lab}: {sorted(bad)} not one of "
                                 f"{allowed} (a {self._kind} grid)")
            o = {k: _as_axis(a) for k, a in o.items()}
            mine = self._template_axes()
            out.append(o if any(mine.get(k) != a for k, a in o.items()) else None)
        return out

    def _template_axes(self) -> dict:
        t = self._template
        if self._kind == "raster":
            return {"x_dim": t._x_dim, "y_dim": t._y_dim, "quantity": t._quantity}
        return {"x_axis": t._x_dim, "y_axis": t._layers[0].y_axis}

    def own_scale(self, index: int) -> bool:
        """Whether cell *index* draws its own axes (an override), so it
        has its own scale even when the grid's is common."""
        return 0 <= index < len(self._axes) and self._axes[index] is not None

    def _clone(self, selection, polarization, axes=None):
        """A headless panel drawn as the template is (with *axes*, an
        override of its axes)."""
        if self._kind == "scatter":
            return self._clone_scatter(selection, polarization, axes)
        from .visibility_raster import VisibilityRaster
        t = self._template
        a = dict(self._template_axes(), **(axes or {}))
        r = VisibilityRaster(
            backend=t._backend, selection=selection, y_dim=a["y_dim"], x_dim=a["x_dim"],
            quantity=a["quantity"], polarization=polarization or t._polarization,
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

    def _clone_scatter(self, selection, polarization, axes=None):
        """A headless scatter drawn as the template is: its X axis, its
        layers (each Y quantity and correlation, with its colorize and
        scaling settings), averaging and Phase RMS / Coherence settings.
        A correlation cell keeps the layers of that correlation (one made
        from the first layer if the template has none)."""
        from dataclasses import replace as _replace
        from .visibility_scatter import VisibilityScatter
        t = self._template
        layers = [_replace(lyr) for lyr in t._layers]
        if polarization:
            mine = [lyr for lyr in layers if lyr.polarization == polarization]
            if not mine:
                l0 = layers[0]
                mine = [_replace(l0, polarization=polarization,
                                 label=str(l0.label).replace(str(l0.polarization),
                                                             polarization))]
            layers = mine
        if axes and "y_axis" in axes:
            layers = [_replace(lyr, y_axis=axes["y_axis"]) for lyr in layers]
        x_axis = (axes or {}).get("x_axis", t._x_dim)
        sc = VisibilityScatter(
            backend=t._backend, selection=selection, x_axis=x_axis, layers=layers,
            layer_cmaps=list(t._layer_cmaps), width=t._width, height=t._height,
            color_mode=t._color_mode, avg_time=t._avg_time, avg_chan=t._avg_chan,
            averaging=t._averaging, detrend=t._detrend,
            stat_time_window=t._stat_time_window, stat_chan_window=t._stat_chan_window,
            enable_info_tool=False, headless=True, defer_initial_render=True)
        sc._theme = getattr(t, "_theme", "dark")
        sc._time_labels = getattr(t, "_time_labels", "utc")
        if getattr(t, "_statistical_cmap", None):
            sc._statistical_cmap = t._statistical_cmap
        return sc

    def _empty_spec(self, title: str, note: str, status: str = "empty"):
        from ..gridplot.panel_spec import PanelSpec
        x0, x1, y0, y1 = self._extent or (0.0, 1.0, 0.0, 1.0)
        return PanelSpec(kind=self._kind, title=title, x_label=self._names[0],
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
                    if self._kind == "scatter":
                        cell.set_layer_cmaps(list(self._template._layer_cmaps),
                                             statistical_cmap=ramp)
                    else:
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
