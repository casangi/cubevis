"""flag_controls.py
===================
Plotter-side half of FlagDB v2: everything ``VisibilityPlotter`` needs to
turn a drawn box into accepted pending flags, show them, and let the user
undo, redo, clear, review and export them.

Pipeline (see ``flag_engine`` for the data side)::

    FlagTool box  ->  FlagController.handle_box()
                        -> request (panel axes, visible layers, filter, extend)
                        -> reader.evaluate_flag_request()   [where the data are]
                        -> Proposal (delta + exact counts)
                        -> reviewer: auto-accept, or the preview dialog
                        -> FlagDB.add()  ->  push_state()  ->  panels refresh

Display of pending flags (user selectable, ``flag_display=``)
------------------------------------------------------------
``"hide"`` (default)
    Pending flags are applied like on-disk flags: flagged points disappear,
    and every statistic (colour scaling, Z-Score reference, Flag fraction)
    is recomputed without them -- the AIPS flag-then-look-again loop.
``"color"``
    The data are drawn from the on-disk flags only and the samples whose
    state the pending flags change are painted on top in a user-selected
    colour (``flag_color=``).

While a proposal is under review (preview on) the samples it would change
are painted in the proposal colour (orange) in either mode.

The data backend is told which view a query wants through
``SelectionSpec.flag_view`` (``"effective"``, ``"disk"``, ``"pending"``,
``"proposal"``); ``SelectionSpec.pending_version`` carries a counter that
changes with every pending-state change so cached frames are never reused
across states.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/flag_controls.py``
"""

from __future__ import annotations

import asyncio
import dataclasses
import html
import logging
import os
import time
import threading
import uuid
from dataclasses import dataclass, field as dc_field
from typing import Any, Mapping, Optional

from .axes import Axis
from .flag_db import FlagDB
from .visibility_plot import _CV_SET_BUSY_JS
from .flag_filters import FilterRegistry, FlagFilter
from .flag_model import FlagCounts, FlagDelta, SpwKey, time_to_datetime

log = logging.getLogger(__name__)

NOTIFY_OK = "#a6e3a1"
NOTIFY_WARN = "#f38ba8"
PROPOSAL_COLOR = "#fab387"       # orange: proposal under review
DEFAULT_PENDING_COLOR = "#ff00ff"
DISPLAY_MODES = ("hide", "color")
DEFAULT_FLAGGED_COLOR = "#7f849c"   # grey: flagged data shown for unflagging


# "Flag reaches" > Baselines: value -> label (HRS H5, 2026-10-07).
REACH_BASELINES = {
    "drawn":    "As drawn",
    "shared":   "All to the antenna they share",
    "antennas": "All to every antenna drawn",
    "all":      "All baselines",
}
# ...and in a sentence ("Each box also takes: ...").
REACH_BASELINE_WORDS = {
    "shared":   "all baselines to the antenna the drawn ones share",
    "antennas": "all baselines to every antenna drawn",
    "all":      "all baselines",
}
REACH_WARN_COLOR = "#f9a825"      # amber: readable on dark and light
REACH_QUIET_COLOR = "#8c8fa1"
REASON_MAX = 80


# ``flag_reach="..."`` words (constructor / task argument) -> setting.
_REACH_WORDS = {
    "shared-antenna": ("baselines", "shared"),
    "antennas":       ("baselines", "antennas"),
    "all-baselines":  ("baselines", "all"),
    "channels":       ("channels", True),
    "spw":            ("spw", True),
    "scan":           ("scan", True),
    "fields":         ("field", True),
    "correlations":   ("correlations", True),
}


def parse_reach(text) -> dict:
    """``flag_reach`` as given to the constructor -> ``set_reach`` mapping.

    A comma-separated list of the words in ``_REACH_WORDS`` (empty: as
    drawn), or a mapping, which is passed through.
    """
    if not text:
        return {}
    if isinstance(text, Mapping):
        return dict(text)
    out = {}
    for word in str(text).replace(";", ",").split(","):
        w = word.strip().lower().replace("_", "-").replace(" ", "-")
        if not w:
            continue
        if w not in _REACH_WORDS:
            raise ValueError(f"flag_reach: unknown word {word.strip()!r}; "
                             f"known: {', '.join(_REACH_WORDS)}")
        k, v = _REACH_WORDS[w]
        if k == "baselines" and "baselines" in out and out[k] != v:
            raise ValueError("flag_reach: give one of shared-antenna, antennas, "
                             "all-baselines")
        out[k] = v
    return out


@dataclass
class Proposal:
    """A resolved flag request awaiting acceptance (not in the DB)."""
    delta: FlagDelta
    counts: FlagCounts
    warnings: list
    kind: str
    db_version: int
    reach: tuple = ()       # what the box was widened to, in words (H5)
    proposal_id: str = dc_field(default_factory=lambda: uuid.uuid4().hex)
    created: float = dc_field(default_factory=time.time)


def _hex_to_rgba(color: str, alpha: float = 0.85) -> tuple:
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    return (r, g, b, int(round(255 * alpha)))


class FlagController:
    """Owns the ``FlagDB``, the filter registry and the flag GUI state of
    one ``VisibilityPlotter``.

    Parameters
    ----------
    plotter :
        The owning ``VisibilityPlotter`` (used for its reader, selection,
        panels and status bar).
    filters :
        ``flag_filters=`` from the constructor: ``{name: callable | FlagFilter}``.
    preview :
        Review each proposal in a dialog before it enters the DB.
    display :
        ``"hide"`` or ``"color"`` -- see the module docstring.
    color :
        Colour for pending flags in ``"color"`` mode.
    """

    def __init__(self, plotter, *, filters: Optional[Mapping[str, Any]] = None,
                 preview: bool = False, display: str = "hide",
                 color: str = DEFAULT_PENDING_COLOR, show_flagged: bool = False,
                 flagged_color: str = DEFAULT_FLAGGED_COLOR,
                 reach: Optional[Mapping[str, Any]] = None, reason: str = "") -> None:
        if display not in DISPLAY_MODES:
            raise ValueError(f"flag_display must be one of {DISPLAY_MODES}; got {display!r}")
        self._plotter = plotter
        self.db = FlagDB()
        self.registry = FilterRegistry(filters)
        self.filter_name = "all"
        self.filter_params: dict = {}
        self.preview = bool(preview)
        self.display = display
        self.color = color or DEFAULT_PENDING_COLOR
        self.show_flagged = bool(show_flagged)
        self.flagged_color = flagged_color or DEFAULT_FLAGGED_COLOR
        self.extend_corr = False
        self.extend_chan = False
        # "Flag reaches" (HRS H5): how far beyond the drawn box a flag
        # goes.  Stays as set until changed, like AIPS's scope switches;
        # everything starts at "as drawn".  Channels and correlations
        # are the two extend_* switches above, the rest is ``scope``
        # (resolved by flag_engine.widen_region when a box is proposed).
        self.scope = {"baselines": "drawn", "spw": False, "scan": False, "field": False}
        self.reason = ""
        self.flag_tools: list = []      # FlagTools to outline in amber (set by the plotter)
        self.set_reach(parse_reach(reach))
        self.set_reason(reason)
        self.proposal: Optional[Proposal] = None
        self._commit_pending: Optional[dict] = None
        self._restore_pending: Optional[dict] = None
        self._caps: Optional[dict] = None
        self.state_version = 0
        self._widgets: dict = {}
        self.db.add_listener(lambda _v: self.push_state())
        # Remote sessions only: pending flags live in this process, but the
        # kernel/worker they depend on can die with the remote host or link.
        # Keep a JSON Lines copy on THIS machine after every change (in a
        # background thread, debounced), offered back by the Export /
        # commit menu.  Local sessions skip it (no overhead).
        self._autosave_timer = None
        self._autosave_lock = threading.Lock()
        if self.is_remote:
            self.db.add_listener(lambda _v: self._schedule_autosave())

    # ================================================================== #
    # State pushed to the data backend and the panels                     #
    # ================================================================== #

    @property
    def reader(self):
        return self._plotter._reader

    @property
    def is_remote(self) -> bool:
        """True when the data live in a remote worker."""
        return type(self.reader).__name__ == "RemoteReductionContext"

    def main_view(self) -> str:
        return "effective" if self.display == "hide" else "disk"

    def overlays(self) -> list:
        """``[(flag_view, rgba), ...]`` panels composite over their image."""
        out = []
        if self.show_flagged:
            # First, so pending / proposal colours are drawn on top of it.
            out.append(("flagged", _hex_to_rgba(self.flagged_color, 0.8)))
        if self.display == "color" and len(self.db):
            out.append(("pending", _hex_to_rgba(self.color)))
        if self.proposal is not None:
            out.append(("proposal", _hex_to_rgba(PROPOSAL_COLOR, 0.9)))
        return out

    def push_state(self, data_changed: bool = True) -> None:
        """Send the pending state to the backend, re-stamp every panel's
        selection and mark every panel stale (re-queried on next render).

        ``data_changed=False``: only the overlays changed (display colour,
        "Show flagged data", a display switch with nothing pending) -- the
        drawn data are the same, so panels only refresh their overlays."""
        if not data_changed:
            overlays = self.overlays()
            view = self.main_view()
            for panel in getattr(self._plotter, "_all_panels", ()):
                sel = getattr(panel, "_selection", None)
                if sel is not None and sel.flag_view != view:
                    panel._selection = dataclasses.replace(sel, flag_view=view)
                panel._flag_overlays = overlays
                panel._overlays_stale = True
            return
        self.state_version += 1
        deltas = self.db.deltas()
        prop = self.proposal.delta if self.proposal is not None else None
        setter = getattr(self.reader, "set_pending_flags", None)
        if setter is not None:
            try:
                setter(deltas, self.state_version, True, prop) if _accepts_proposal(setter) \
                    else setter(deltas, self.state_version, True)
            except Exception:
                log.exception("set_pending_flags failed")
        overlays = self.overlays()
        view = self.main_view()
        for panel in getattr(self._plotter, "_all_panels", ()):
            sel = getattr(panel, "_selection", None)
            if sel is not None:
                panel._selection = dataclasses.replace(
                    sel, pending_version=self.state_version, flag_view=view)
            panel._flag_overlays = overlays
            panel._flag_stale = True

    def init_panels(self) -> None:
        """Give freshly built panels the configured overlays (e.g.
        ``flag_show_flagged=True`` at construction) before their first
        render -- without marking them stale."""
        overlays = self.overlays()
        for panel in getattr(self._plotter, "_all_panels", ()):
            panel._flag_overlays = overlays
            # Panels render inside their constructors, before this runs:
            # redraw the ones already drawn so the initial page carries the
            # overlay (only when an overlay is configured at construction).
            if overlays and getattr(panel, "_selection", None) is not None \
                    and not getattr(panel, "_deferred", False) \
                    and (getattr(panel, "_agg", None) is not None
                         or any(r is not None for r in getattr(panel, "_layer_images", ()) or ())):
                try:
                    panel._render(panel._selection)
                except Exception:
                    log.debug("initial overlay render failed", exc_info=True)

    def stamp_selection(self, sel):
        """Apply the current pending version and view to a new selection."""
        return dataclasses.replace(sel, pending_version=self.state_version,
                                   flag_view=self.main_view())

    def reset(self) -> None:
        """Data reload: drop pending flags and history (nothing was written)."""
        self.proposal = None
        self.db.clear(record=False)

    # ================================================================== #
    # Requests                                                            #
    # ================================================================== #

    def _filter(self) -> FlagFilter:
        return self.registry.get(self.filter_name)

    # ------------------------------------------------------------------ #
    # Flag reaches / reason                                                #
    # ------------------------------------------------------------------ #
    def set_reach(self, reach: Optional[Mapping[str, Any]]) -> None:
        """Set how far a flag reaches beyond the drawn box.

        *reach* maps any of ``baselines`` (one of ``REACH_BASELINES``),
        ``channels``, ``correlations``, ``spw``, ``scan``, ``field``
        (booleans) to its value; keys left out keep their setting.
        """
        for k, v in dict(reach or {}).items():
            if k == "baselines":
                v = str(v).lower()
                if v not in REACH_BASELINES:
                    raise ValueError(f"flag_reach['baselines'] must be one of "
                                     f"{tuple(REACH_BASELINES)}; got {v!r}")
                self.scope["baselines"] = v
            elif k == "channels":
                self.extend_chan = bool(v)
            elif k == "correlations":
                self.extend_corr = bool(v)
            elif k in ("spw", "scan", "field"):
                self.scope[k] = bool(v)
            else:
                raise ValueError(f"flag_reach: unknown key {k!r}; known: baselines, "
                                 f"channels, correlations, spw, scan, field")

    def set_reason(self, reason) -> None:
        # One line, no quotes: it goes into flagdata's reason='...' and
        # into one row of a report.
        r = " ".join(str(reason or "").split())
        self.reason = r.replace("'", "").replace('"', "")[:REASON_MAX]

    def reach_words(self) -> list:
        """The settings wider than "as drawn", in words (empty if none)."""
        out = []
        b = self.scope["baselines"]
        if b != "drawn":
            out.append(REACH_BASELINE_WORDS[b])
        for on, text in ((self.extend_chan, "all channels"),
                         (self.scope["spw"], "all selected spectral windows"),
                         (self.scope["scan"], "the whole scan"),
                         (self.scope["field"], "all fields"),
                         (self.extend_corr, "all correlations")):
            if on:
                out.append(text)
        return out

    def reach_text(self) -> str:
        """The line under the "Flag reaches" controls."""
        w = self.reach_words()
        if not w:
            return "Flags cover what is drawn."
        return "⚠ Each box also takes: " + html.escape("; ".join(w)) + "."

    def build_request(self, panel, kind: str, msg: dict, flag: bool) -> dict:
        """The engine request for a box drawn on *panel* (raster or scatter)."""
        f = self._filter()
        params = dict(self.filter_params.get(f.name, {}))
        sel = panel._selection if getattr(panel, "_selection", None) is not None \
            else self._plotter._build_selection()
        req = {
            "flag": flag, "selection": sel, "kind": kind,
            "x0": float(msg.get("x0", 0.0)), "x1": float(msg.get("x1", 0.0)),
            "y0": float(msg.get("y0", 0.0)), "y1": float(msg.get("y1", 0.0)),
            "filter": {"name": f.name, "params": params},
            "filter_obj": f,
            "extend": {"extend_corr": self.extend_corr, "extend_chan": self.extend_chan},
            "scope": dict(self.scope),
            "reason": self.reason,
            "source": f"{kind}_box_{'flag' if flag else 'unflag'}",
            "data_column": getattr(sel, "data_column", ""),
        }
        # The axis actually plotted: a backend may substitute (e.g. Channel
        # -> Frequency when windows differ), and the box is in those units.
        x_ax = _plotted_axis(panel, "x")
        y_ax = _plotted_axis(panel, "y")
        if kind == "raster":
            req.update(x_axis=x_ax.name, y_axis=y_ax.name,
                       polarization=getattr(panel, "_polarization", None),
                       quantity=getattr(getattr(panel, "_quantity", None), "name", None))
            # A raster's Baseline axis is drawn in positions (side by
            # side, in number or length order), not baseline numbers.
            # The engine needs the same mapping to resolve the box; the
            # record names the baselines, since a position means nothing
            # once the plot is gone.
            bl_axis = getattr(panel, "_bl_axis", None)
            if bl_axis is not None and Axis.BASELINE in (x_ax, y_ax):
                req["baseline_order"] = [int(i) for i in bl_axis.ids]

            def _span(ax, a, b):
                if ax == Axis.BASELINE and bl_axis is not None:
                    return _baseline_span(bl_axis, a, b)
                return _rng(ax, a, b)

            prov = (f"raster box {x_ax.name} {_span(x_ax, req['x0'], req['x1'])} x "
                    f"{y_ax.name} {_span(y_ax, req['y0'], req['y1'])} "
                    f"({req['polarization']})")
        else:
            layers = scatter_layer_entries(panel)
            req.update(x_axis=x_ax.name, layers=layers)
            prov = (f"scatter box {x_ax.name} {_rng(x_ax, req['x0'], req['x1'])} x "
                    f"{_rng(None, req['y0'], req['y1'])} on "
                    + ", ".join(f"{l['y_axis']}({l['polarization']})" for l in layers))
        # Shift / Alt held while drawing: the tool stretched the box to
        # the plot's edges; say so, since the numbers alone do not.
        span = str(msg.get("span") or "")
        if span:
            prov += {"y": " [Shift: full height of the view]",
                     "x": " [Alt: full width of the view]",
                     "xy": " [Shift+Alt: the whole view]"}.get(span, "")
        req["provenance"] = [prov]
        req["comment"] = prov
        return req

    def evaluate(self, req: dict) -> Proposal:
        result = self.reader.evaluate_flag_request(req)
        counts = FlagCounts.from_dict(result.get("counts") or {})
        d = result.get("delta")
        delta = FlagDelta.from_dict(d) if isinstance(d, dict) else d
        return Proposal(delta=delta, counts=counts, warnings=list(result.get("warnings") or ()),
                        kind=req.get("kind", ""), db_version=self.db.version,
                        reach=tuple(result.get("reach") or ()))

    async def handle_box(self, msg: dict, kind: str, panel) -> dict:
        t0 = time.perf_counter()
        try:
            return await self._handle_box(msg, kind, panel)
        finally:
            log.info("visplot timing: %s %s box evaluated in %.2f s (the redraws follow)",
                     "flag" if msg.get("flag", True) else "unflag", kind,
                     time.perf_counter() - t0)

    async def _handle_box(self, msg: dict, kind: str, panel) -> dict:
        flag = bool(msg.get("flag", True))
        verb = "flag" if flag else "unflag"
        if panel is None:
            return self.response(f"⚠ No {kind} panel to {verb}.", NOTIFY_WARN, refresh=False)
        if self.proposal is not None:
            return self.response("⚠ Accept or reject the proposal under review first.",
                                 NOTIFY_WARN, refresh=False, preview=self._preview_payload())
        try:
            req = self.build_request(panel, kind, msg, flag)
            # Resolving a box reads data (possibly the whole selection for a
            # scatter box or a reference-population filter): run it off the
            # event loop so hover, pan/zoom and the other panel stay live.
            prop = await asyncio.to_thread(self.evaluate, req)
        except Exception as exc:
            if type(exc).__name__ == "UserFilterNotRemoteError":
                log.warning("%s", exc)          # expected, not a bug: no traceback
            else:
                log.exception("flag request failed")
            return self.response(f"⚠ {verb.capitalize()} failed: {html.escape(str(exc))}",
                                 NOTIFY_WARN, refresh=False)
        if prop.delta is None:
            why = "; ".join(prop.warnings) or "nothing matched"
            return self.response(f"⚠ Nothing to {verb}: {html.escape(why)}.", NOTIFY_WARN,
                                 refresh=False)
        if self.preview:
            self.proposal = prop
            self.push_state()
            return self.response(
                f"Review the proposal: {self._count_text(prop)}.", NOTIFY_OK,
                preview=self._preview_payload())
        return self.accept(prop)

    def accept(self, prop: Proposal) -> dict:
        stale = prop.db_version != self.db.version
        self.proposal = None
        self.db.add(prop.delta)       # listener pushes state
        verb = prop.delta.verb.capitalize() + "ged"
        text = f"✓ {verb}: {self._count_text(prop)}."
        if stale:
            text += " (the pending flags changed while this was under review; " \
                    "the reviewed samples were used as shown)"
        if prop.warnings:
            text += " " + html.escape("; ".join(prop.warnings))
        return self.response(text, NOTIFY_OK, preview_closed=True)

    def reject(self) -> dict:
        self.proposal = None
        self.push_state()
        return self.response("Proposal rejected; nothing changed.", NOTIFY_OK,
                             preview_closed=True)

    @staticmethod
    def _count_text(prop: Proposal) -> str:
        c = prop.counts
        n = c.n_changed if c.n_changed else c.n_matched
        s = f"{n:,} sample{'s' if n != 1 else ''}"
        if c.n_selected and c.n_selected != n:
            s += f" of {c.n_selected:,} selected"
        # How many baselines it reaches (HRS H4): a box on a plot without
        # a Baseline axis -- the all-baseline waterfall -- flags every
        # selected baseline, and that should be plain before it is
        # accepted or committed, not discovered afterwards.
        nb = len(c.by_baseline or {})
        if nb > 1:
            s += f" on {nb:,} baselines"
        # What the box was widened to (HRS H5), in words, in the same
        # sentence as the count it explains.
        d = prop.delta
        words = list(getattr(prop, "reach", ()) or ())
        if d is not None:
            words += [w for on, w in ((d.extend_chan, "all channels"),
                                      (d.extend_corr, "all correlations")) if on]
        if words:
            s += " \u2014 reaching " + html.escape("; ".join(words))
        return s

    # ================================================================== #
    # Actions from the Flag controls                                      #
    # ================================================================== #

    async def handle_action(self, msg: dict, context=None) -> dict:
        action = msg.get("action", "")
        try:
            if action == "config":
                return self.configure(msg)
            if action == "undo":
                op = self.db.undo()
                return self.response("Undone." if op else "Nothing to undo.", NOTIFY_OK,
                                     refresh=bool(op))
            if action == "redo":
                op = self.db.redo()
                return self.response("Redone." if op else "Nothing to redo.", NOTIFY_OK,
                                     refresh=bool(op))
            if action == "clear":
                n = self.db.clear()
                return self.response(f"Cleared {n} pending operation(s) (undo restores them).",
                                     NOTIFY_OK, refresh=bool(n))
            if action == "accept" and self._commit_pending is not None \
                    and msg.get("id") == self._commit_pending["id"]:
                return await self._do_commit()
            if action == "reject" and self._commit_pending is not None:
                self._commit_pending = None
                return self.response("Commit cancelled; nothing was written.", NOTIFY_OK,
                                     refresh=False, preview_closed=True)
            if action == "accept" and getattr(self, "_restore_pending", None) is not None \
                    and msg.get("id") == self._restore_pending["id"]:
                pend, self._restore_pending = self._restore_pending, None
                resp = await self._export_action({"kind": "restore", "path": pend["path"],
                                                  "confirmed": True})
                resp["preview_closed"] = True
                return resp
            if action == "reject" and getattr(self, "_restore_pending", None) is not None:
                self._restore_pending = None
                return self.response("Restore cancelled; nothing was changed.", NOTIFY_OK,
                                     refresh=False, preview_closed=True)
            if action == "export":
                return await self._export_action(msg)
            if action == "accept":
                if self.proposal is None or msg.get("id") not in (None, self.proposal.proposal_id):
                    return self.response("⚠ No such proposal.", NOTIFY_WARN, preview_closed=True)
                return self.accept(self.proposal)
            if action == "reject":
                return self.reject()
            if action == "report":
                out = self.response("", NOTIFY_OK, refresh=False)
                out["report_html"] = self.report_html()
                return out
            if action == "export_flagdata":
                return self.response("⚠ flagdata export was removed; use JSON or "
                                     "Write flags (Export / commit).", NOTIFY_WARN, refresh=False)
            if action == "export_jsonl":
                path = self.export(fmt="jsonl")
                return self.response(f"Wrote {html.escape(path)}", NOTIFY_OK, refresh=False)
        except Exception as exc:
            log.exception("flag action %r failed", action)
            return self.response(f"⚠ {html.escape(action)} failed: {html.escape(str(exc))}",
                                 NOTIFY_WARN, refresh=False)
        return self.response(f"⚠ Unknown flag action {html.escape(str(action))}",
                             NOTIFY_WARN, refresh=False)

    def configure(self, msg: dict) -> dict:
        refresh = False
        if "filter" in msg:
            name = msg["filter"] or "all"
            f = self.registry.get(name)
            params = dict(msg.get("params") or {})
            specs = f.param_specs()
            params = {k: v for k, v in params.items() if k in specs and specs[k].gui}
            f.resolve_params(params)            # validate now, report errors early
            self.filter_name = name
            self.filter_params[name] = params
        if "preview" in msg:
            self.preview = bool(msg["preview"])
        if "extend_corr" in msg:
            self.extend_corr = bool(msg["extend_corr"])
        if "extend_chan" in msg:
            self.extend_chan = bool(msg["extend_chan"])
        if isinstance(msg.get("reach"), dict):
            self.set_reach({k: v for k, v in msg["reach"].items()
                            if k in ("baselines", "spw", "scan", "field")})
        if "reason" in msg:
            self.set_reason(msg["reason"])
        data_changed = False
        if "display" in msg and msg["display"] in DISPLAY_MODES and msg["display"] != self.display:
            self.display = msg["display"]
            refresh = True
            # "hide" draws the effective flags, "color" the on-disk ones:
            # identical data unless something is pending.
            data_changed = bool(len(self.db)) or self.proposal is not None
        if "show_flagged" in msg and bool(msg["show_flagged"]) != self.show_flagged:
            self.show_flagged = bool(msg["show_flagged"])
            refresh = True
        if msg.get("flagged_color") and msg["flagged_color"] != self.flagged_color:
            self.flagged_color = msg["flagged_color"]
            refresh = refresh or self.show_flagged
        if "color" in msg and msg["color"] and msg["color"] != self.color:
            self.color = msg["color"]
            refresh = refresh or (self.display == "color")
        if refresh:
            self.push_state(data_changed=data_changed)
        return self.response("", NOTIFY_OK, refresh=refresh)

    # ================================================================== #
    # Responses                                                           #
    # ================================================================== #

    def info_text(self) -> str:
        n = len(self.db)
        samples = sum(d.n_samples or 0 for d in self.db.deltas())
        parts = [f"<b>Pending:</b> {n} operation{'s' if n != 1 else ''}"]
        if samples:
            parts.append(f"{samples:,} samples")
        parts.append(f"filter: {html.escape(self._filter().label)}")
        if self.proposal is not None:
            parts.append("<span style='color:%s'>proposal under review</span>" % PROPOSAL_COLOR)
        return " • ".join(parts)

    def _refresh_indices(self) -> list:
        p = self._plotter
        panels = list(getattr(p, "_all_panels", ()))
        slots = list(getattr(p, "_slots", ()))
        if getattr(p, "_layout", "side") == "one":
            slots = slots[:1]
        out = []
        for s in slots:
            act = s.active() if callable(getattr(s, "active", None)) else s.active
            if act in panels:
                out.append(panels.index(act))
        return out

    def response(self, text: str, color: str, *, refresh: bool = True,
                 preview: Optional[dict] = None, preview_closed: bool = False) -> dict:
        p = self._plotter
        if text:
            try:
                p._notify(text, color=color)
            except Exception:
                pass
        try:
            p._update_status_bar()
        except Exception:
            pass
        out = {
            "notify_text": text if text else None,
            "notify_color": color,
            "status_text": p._status_text() if hasattr(p, "_status_text") else None,
            "flag_info": self.info_text(),
            "can_undo": self.db.can_undo(), "can_redo": self.db.can_redo(),
            "refresh": self._refresh_indices() if refresh else [],
        }
        if preview is not None:
            out["preview"] = preview
        if preview_closed:
            out["preview_closed"] = True
        return out

    def _preview_payload(self) -> Optional[dict]:
        if self.proposal is None:
            return None
        return {"id": self.proposal.proposal_id, "html": self.proposal_html(self.proposal)}

    def proposal_html(self, prop: Proposal) -> str:
        d, c = prop.delta, prop.counts

        def top(dct, n=8):
            items = sorted(dct.items(), key=lambda kv: -kv[1])
            s = ", ".join(f"{html.escape(str(k))}: {v:,}" for k, v in items[:n])
            if len(items) > n:
                s += f", … ({len(items) - n} more)"
            return s or "—"
        rows = [
            ("Action", d.verb),
            ("Samples to change", f"{c.n_changed:,}"),
            ("Matched by filter", f"{c.n_matched:,}"),
            ("Selected by the box", f"{c.n_selected:,}"),
            ("Filter", html.escape(d.filter.describe()) if d.filter else "all selected"),
            ("Representation", "explicit samples" if d.is_sample_set else "coordinate region"),
            ("Baselines", top(c.by_baseline)),
            ("Antennas", top(c.by_antenna)),
            ("Correlations", top(c.by_pol)),
            ("Spectral windows", top(c.by_spw)),
            ("Scans", top(c.by_scan)),
        ]
        if c.time_span:
            t0 = time_to_datetime(c.time_span[0], d.time_format).strftime("%Y-%m-%d %H:%M:%S")
            t1 = time_to_datetime(c.time_span[1], d.time_format).strftime("%H:%M:%S")
            rows.append(("Time", f"{t0} – {t1} UTC ({c.n_times} integrations)"))
        reach = list(prop.reach) + [x for x, on in (("all channels", d.extend_chan),
                                                    ("all correlations", d.extend_corr)) if on]
        rows.append(("Reaches", html.escape("; ".join(reach)) if reach else "as drawn"))
        if d.reason:
            rows.append(("Reason", html.escape(d.reason)))
        rows.append(("Provenance", html.escape(" → ".join(d.provenance))))
        if prop.warnings:
            rows.append(("Notes", html.escape("; ".join(prop.warnings))))
        body = "".join(f"<tr><td style='padding:1px 14px 1px 0;opacity:0.75;"
                       f"white-space:nowrap;vertical-align:top'>{k}</td>"
                       f"<td style='padding:1px 0'>{v}</td></tr>" for k, v in rows)
        return (f"<div style='font-family:system-ui,sans-serif;line-height:1.35'>"
                f"<div style='font-size:14px;font-weight:600;margin-bottom:4px'>Flag proposal</div>"
                f"<table style='border-collapse:collapse'>{body}</table></div>")

    # ================================================================== #
    # Export / Python API                                                  #
    # ================================================================== #

    def export(self, path: Optional[str] = None, fmt: str = "jsonl") -> str:
        """Write the pending operations as JSON Lines; returns the path.
        (``fmt="flagdata"`` was removed on 2026-09-30 -- see flag_export.)"""
        from .flag_export import to_jsonl
        if fmt != "jsonl":
            raise ValueError(f"unknown export format {fmt!r}: only 'jsonl' is supported "
                             "(flagdata export was removed; write flags with the exact "
                             "Export / commit path instead)")
        src = getattr(self._plotter, "_source_path", "") or "visplot"
        path = path or self._default_path(".flags.jsonl")
        text = to_jsonl(self.db.deltas(), self._json_header(src))
        with open(path, "w") as fh:
            fh.write(text)
        return os.path.abspath(path)


    # ================================================================== #
    # Export / commit / load                                               #
    # ================================================================== #

    # ---------------- autosave (remote sessions) ----------------------- #

    AUTOSAVE_DELAY_S = 1.0

    def autosave_path(self) -> str:
        import hashlib
        src = os.path.normpath(getattr(self._plotter, "_source_path", "") or "visplot")
        tag = hashlib.sha1(src.encode()).hexdigest()[:10]
        root = os.path.join(os.path.expanduser("~"), ".cache", "cubevis", "visplot", "autosave")
        return os.path.join(root, f"{os.path.basename(src) or 'visplot'}-{tag}.flags.jsonl")

    def _schedule_autosave(self) -> None:
        with self._autosave_lock:
            if self._autosave_timer is not None:
                self._autosave_timer.cancel()
            self._autosave_timer = threading.Timer(self.AUTOSAVE_DELAY_S, self._autosave_now)
            self._autosave_timer.daemon = True
            self._autosave_timer.start()

    def _autosave_now(self) -> None:
        """Write (or, with nothing pending, remove) the autosave file."""
        from .flag_export import to_jsonl
        path = self.autosave_path()
        try:
            deltas = self.db.deltas()
            if not deltas:
                if os.path.exists(path):
                    os.remove(path)
                return
            os.makedirs(os.path.dirname(path), exist_ok=True)
            src = getattr(self._plotter, "_source_path", "") or ""
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                fh.write(to_jsonl(deltas, self._json_header(src)))
            os.replace(tmp, path)                 # atomic: never a half-written file
        except Exception:
            log.warning("pending-flag autosave failed", exc_info=True)

    def autosaved(self) -> Optional[dict]:
        """``{"path", "operations", "saved"}`` of a recoverable autosave."""
        path = self.autosave_path()
        if not (self.is_remote and os.path.exists(path)):
            return None
        try:
            with open(path) as fh:
                n = sum(1 for _ in fh) - 1
            return {"path": path, "operations": max(0, n), "saved": os.path.getmtime(path)} \
                if n > 0 else None
        except Exception:
            return None

    def capabilities(self) -> dict:
        """Cached ``flag_commit.capabilities`` of the data (remote-aware)."""
        if self._caps is None:
            fn = getattr(self.reader, "flag_commit_capabilities", None)
            try:
                self._caps = fn() if fn else {"format": "msv2", "write": False,
                                              "write_reason": "reader cannot commit"}
            except Exception as exc:
                self._caps = {"format": "?", "write": False,
                              "write_reason": str(exc)}
        return self._caps

    def export_options(self) -> list:
        """``[(value, label)]`` for the Export / commit menu of this data."""
        caps = self.capabilities()
        msv2 = caps.get("format") != "msv4"
        opts = [("json", "Save flags as JSON")]
        label = "Write flags to the MS" if msv2 else "Write flags to the PS"
        if not caps.get("write"):
            label += " -- unavailable"
        opts.append(("commit", label))
        if msv2 and caps.get("casa"):
            # Secondary, CASA-native path -- only when CASA is installed.
            opts.append(("commit_casa", "Write flags with CASA flagdata (CASA-like)"))
        opts.append(("load", "Load flags from JSON (as pending)"))
        auto = self.autosaved()
        if auto:
            opts.append(("recover", f"Recover autosaved flags ({auto['operations']} operation(s), "
                                    + time.strftime("%Y-%m-%d %H:%M",
                                                    time.localtime(auto["saved"])) + ")"))
        opts.append(("restore", "Restore flags from a commit backup (.npz)"))
        return opts

    def _json_header(self, src: str) -> dict:
        hdr = {"source": src, "exported": time.time(),
               "data_format": self.capabilities().get("format")}
        try:
            hdr["spw_table"] = self.reader.flag_spw_table()
        except Exception:
            pass
        sel = getattr(self._plotter, "_selection", None) or None
        dc = getattr(self._plotter, "_datacolumn", None)
        if dc:
            hdr["data_column"] = dc
        try:
            import cubevis
            hdr["cubevis_version"] = getattr(cubevis, "__version__", None)
        except Exception:
            pass
        return hdr

    def load_jsonl(self, path: str) -> int:
        """Add the operations of an exported JSON Lines file as pending
        flags (in order).  Refuses a file whose spectral windows do not
        exist in the open data."""
        with open(path) as fh:
            header, deltas = FlagDB.parse_jsonl(fh.read())
        current = [SpwKey.from_dict(k) for k in self.reader.flag_spw_table()]

        def known(k):
            return any(k.matches(c) for c in current)
        bad = set()
        for d in deltas:
            keys = []
            if d.samples is not None:
                keys += [b.spw for b in d.samples]
            if d.spw is not None:
                keys += list(d.spw)
            if d.spw_channels is not None:
                keys += [sc.spw for sc in d.spw_channels]
            bad.update(f"{k.ident} ({k.n_chan} ch)" for k in keys if not known(k))
        if bad:
            raise ValueError("the file refers to spectral windows not in this data: "
                             + ", ".join(sorted(bad)))
        have = {d.delta_id for d in self.db.deltas()}
        n = 0
        for d in deltas:
            if d.delta_id in have:
                continue
            self.db.add(d)
            n += 1
        return n

    def _default_path(self, suffix: str) -> str:
        """``<data name>.<kind>.<YYYYmmdd-HHMMSS>.<ext>`` -- unique per export
        (to the second) and sortable, so a later export never replaces an
        earlier one."""
        src = getattr(self._plotter, "_source_path", "") or "visplot"
        base = os.path.basename(os.path.normpath(src)) or "visplot"
        stem, ext = os.path.splitext(suffix)            # ".flags", ".jsonl"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = f"{base}{stem}.{stamp}{ext}"
        n = 2
        while os.path.exists(path):                      # same second
            path = f"{base}{stem}.{stamp}-{n}{ext}"
            n += 1
        return path

    @staticmethod
    def _refuse_existing(path: Optional[str]) -> Optional[str]:
        """Explicit file names are never overwritten silently."""
        if path and os.path.exists(path):
            return (f"⚠ {html.escape(os.path.abspath(path))} already exists; nothing was "
                    "written.  Choose another name, or leave the file box empty for a "
                    "new time-stamped name.")
        return None

    async def _export_action(self, msg: dict) -> dict:
        kind = msg.get("kind") or "json"
        path = (msg.get("path") or "").strip() or None
        deltas = self.db.deltas()
        caps = self.capabilities()
        if kind == "json":
            refusal = self._refuse_existing(path)
            if refusal:
                return self.response(refusal, NOTIFY_WARN, refresh=False)
        if kind == "json":
            p = self.export(path or self._default_path(".flags.jsonl"), fmt="jsonl")
            return self.response(f"Wrote {len(deltas)} operation(s) to {html.escape(p)}",
                                 NOTIFY_OK, refresh=False)
        if kind == "load":
            if not path:
                return self.response("⚠ Give the JSON file to load in the file box.",
                                     NOTIFY_WARN, refresh=False)
            n = self.load_jsonl(path)
            return self.response(f"Loaded {n} pending operation(s) from {html.escape(path)}",
                                 NOTIFY_OK)
        if kind == "list_backups":
            fn = getattr(self.reader, "list_flag_backups", None)
            found = (await asyncio.to_thread(fn)) if fn else []
            out = self.response("" if found else "No commit backups found next to the data.",
                                NOTIFY_OK, refresh=False)
            out["backups"] = [
                [e["path"], f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e['created']))}"
                            f" · {e['operations']} op(s) · {os.path.basename(e['path'])}"]
                for e in found]
            return out
        if kind == "recover":
            auto = self.autosaved()
            if not auto:
                return self.response("No autosaved flags to recover.", NOTIFY_OK, refresh=False)
            n = self.load_jsonl(auto["path"])
            return self.response(f"Recovered {n} pending operation(s) from the autosave.",
                                 NOTIFY_OK)
        if kind == "restore" and not path:
            fn = getattr(self.reader, "list_flag_backups", None)
            found = fn() if fn else []
            if not found:
                return self.response("No commit backups found next to the data.", NOTIFY_OK,
                                     refresh=False)
            lines = "; ".join(
                f"{os.path.basename(e['path'])} ({e['operations']} op(s), "
                f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(e['created']))})"
                for e in found[:5])
            resp = self.response(f"{len(found)} backup(s), newest first: {html.escape(lines)}. "
                                 "The newest is in the file box -- press Go to review it.",
                                 NOTIFY_OK, refresh=False)
            resp["export_path"] = found[0]["path"]
            return resp
        if kind == "restore" and not msg.get("confirmed"):
            if not os.path.exists(path) and not self.is_remote:
                return self.response(f"⚠ {html.escape(path)} does not exist.", NOTIFY_WARN,
                                     refresh=False)
            self._restore_pending = {"id": uuid.uuid4().hex, "path": path}
            body = (f"<div style='font-family:system-ui,sans-serif;line-height:1.35'>"
                    f"<div style='font-size:14px;font-weight:600;margin-bottom:4px'>"
                    f"Restore flags from this backup?</div>"
                    f"<div>{html.escape(path)}</div>"
                    f"<div style='margin-top:4px;opacity:0.85'>The flags the commit changed are "
                    f"set back to their previous values; pending operations are kept.</div>"
                    f"<div style='margin-top:6px;font-weight:600;color:#e64553'>"
                    f"This modifies the data set on disk.</div></div>")
            return self.response("Confirm restoring flags from the backup.", NOTIFY_OK,
                                 refresh=False,
                                 preview={"id": self._restore_pending["id"], "html": body})
        if kind == "restore":
            if not path:
                return self.response("⚠ Give the backup (.npz) file in the file box.",
                                     NOTIFY_WARN, refresh=False)
            if not os.path.exists(path):
                return self.response(f"⚠ {html.escape(path)} does not exist.", NOTIFY_WARN,
                                     refresh=False)
            rep = await asyncio.to_thread(self.reader.restore_flag_backup, path)
            self._after_disk_change()
            what = (f"{rep['restored']:,} sample flag(s)" if "restored" in rep
                    else f"the flags of {rep.get('restored_rows', 0):,} row(s)")
            return self.response(f"Restored {what} from {html.escape(path)}", NOTIFY_OK)
        if kind in ("commit", "commit_casa"):
            casa = kind == "commit_casa"
            if not caps.get("casa" if casa else "write"):
                return self.response("⚠ Writing flags is unavailable here: "
                                     + html.escape(caps.get("casa_reason" if casa else
                                                            "write_reason") or "unknown reason"),
                                     NOTIFY_WARN, refresh=False)
            if not deltas:
                return self.response("Nothing to write: no pending operations.", NOTIFY_OK,
                                     refresh=False)
            self._commit_pending = {"id": uuid.uuid4().hex, "deltas": deltas,
                                    "method": "casa" if casa else None}
            return self.response("Confirm writing the pending flags.", NOTIFY_OK,
                                 refresh=False,
                                 preview={"id": self._commit_pending["id"],
                                          "html": self._commit_html(deltas, caps,
                                                                    "casa" if casa else None)})
        return self.response(f"⚠ Unknown export kind {html.escape(str(kind))}", NOTIFY_WARN,
                             refresh=False)

    def _commit_html(self, deltas, caps, method=None) -> str:
        esc = html.escape
        src = getattr(self._plotter, "_source_path", "") or ""
        msv2 = caps.get("format") != "msv4"
        n_samples = sum(d.n_samples or 0 for d in deltas)
        casa_note = ""
        if method == "casa":
            n_cmd = self._casa_command_count(deltas)
            how = (f"casatasks.flagdata (list mode, {n_cmd:,} command(s), operations in order), "
                   "after saving a CASA flag version (flagmanager) and a visplot backup of the "
                   "target rows")
            casa_note = (
                "<div style='margin-top:6px;color:#df8e1d;font-weight:600'>CASA-like write: "
                "CASA may apply large or scattered selections incompletely (in testing, "
                "2,689 of 52,624 samples of one operation were missed). The result is "
                "verified and any difference reported; the exact default write is "
                "\u201cWrite flags to the MS\u201d.</div>")
            if n_cmd > self._casa_large():
                casa_note += ("<div style='color:#e64553;font-weight:600'>This is a large "
                              f"selection ({n_cmd:,} flagdata commands): expect it to be slow "
                              "and more likely to be incomplete.</div>")
        elif msv2:
            how = ("the final flag of exactly the changed samples is written to their MS "
                   "rows with arcae (FLAG_ROW kept consistent), after saving their previous "
                   "values to a backup file next to the MS"
                   + (" and a CASA flag version" if caps.get("casa_version") else ""))
        else:
            how = ("zarr writes of exactly the changed samples, after saving their previous "
                   "values to a backup file next to the store")
        rows = [("Write to", esc(src)), ("Operations", str(len(deltas))),
                ("Samples (as proposed)", f"{n_samples:,}"), ("Method", esc(how)),
                ("After writing", "the result is compared with what visplot showed; "
                                  "the pending list is emptied and the plots re-read the data")]
        body = "".join(f"<tr><td style='padding:1px 14px 1px 0;opacity:0.75;"
                       f"white-space:nowrap;vertical-align:top'>{k}</td>"
                       f"<td style='padding:1px 0'>{v}</td></tr>" for k, v in rows)
        return (f"<div style='font-family:system-ui,sans-serif;line-height:1.35'>"
                f"<div style='font-size:14px;font-weight:600;margin-bottom:4px'>"
                f"Write pending flags to the data?</div>"
                f"<table style='border-collapse:collapse'>{body}</table>"
                f"{casa_note}"
                f"<div style='margin-top:6px;font-weight:600;color:#e64553'>"
                f"This modifies the data set on disk.</div></div>")

    @staticmethod
    def _casa_large() -> int:
        from .flag_casa import LARGE_COMMAND_COUNT
        return LARGE_COMMAND_COUNT

    def _casa_command_count(self, deltas) -> int:
        """flagdata commands the CASA write would issue (generated locally
        from the deltas; the SPW ids come from the reader)."""
        try:
            from .flag_casa import to_flagdata_lines
            ids = self.reader.spw_casa_ids()
            return int(sum(len(to_flagdata_lines([d], spw_ids=ids, comments=False))
                           for d in deltas))
        except Exception:
            return 0

    async def _do_commit(self) -> dict:
        pend, self._commit_pending = self._commit_pending, None
        deltas = pend["deltas"]
        t0 = time.perf_counter()
        opts = {"method": pend["method"]} if pend.get("method") else {}
        rep = await asyncio.to_thread(self.reader.commit_pending_flags, list(deltas), **opts)
        log.info("visplot commit: %.2f s in the backend %s", time.perf_counter() - t0,
                 rep.get("timing", ""))
        self.db.clear(record=False)
        if rep.get("verified") and "frames_refreshed" in rep:
            # Cached frames were brought up to date in place (flag_commit.
            # refresh_cached_frames): the redraw only re-filters, like any
            # flag change -- no re-read of every cached selection.
            self.push_state()
        else:
            self._after_disk_change()
        parts = []
        if rep.get("backup"):
            parts.append(f"backup <b>{html.escape(str(rep.get('backup')))}</b>")
        if rep.get("version_name"):
            parts.append(f"CASA flag version <b>{html.escape(str(rep.get('version_name')))}</b>")
        if rep.get("history"):
            where_extra = " A HISTORY entry records the write."
        else:
            where_extra = ""
        where = " and ".join(parts) or "nowhere"
        if not rep.get("written", rep.get("expected_changes", 1)):
            return self.response(f"Nothing to write: the {len(deltas)} pending operation(s) "
                                 "change no flags on disk (already in that state).",
                                 NOTIFY_OK, preview_closed=True)
        if rep.get("verified", True):
            text = (f"✓ Wrote {len(deltas)} operation(s) "
                    f"({rep.get('expected_changes', 0):,} sample changes"
                    + (f" in {rep['rows']:,} MS row(s)" if rep.get("rows") is not None else "")
                    + (f" with CASA flagdata, {rep['commands']:,} command(s) in "
                       f"{rep['flagdata_calls']} call(s)" if rep.get("method") == "casa" else "")
                    + "); verified. "
                    f"Previous flags saved as {where}.{where_extra}")
            color = NOTIFY_OK
        else:
            k = rep.get("mismatch_kinds", {})
            text = (f"⚠ Wrote {len(deltas)} operation(s), but {rep.get('mismatches', 0):,} "
                    f"sample(s) differ from what visplot showed "
                    f"({k.get('should_be_flagged', 0):,} not flagged, "
                    f"{k.get('should_be_unflagged', 0):,} not unflagged, "
                    f"{k.get('collateral', 0):,} changed that should not have). "
                    f"Previous flags saved as {where} -- restore them if needed.")
            color = NOTIFY_WARN
        return self.response(text, color, preview_closed=True)

    def _after_disk_change(self) -> None:
        """On-disk flags changed: every cached frame is stale."""
        p = self._plotter
        p._cache_generation = getattr(p, "_cache_generation", 0) + 1
        for panel in getattr(p, "_all_panels", ()):
            sel = getattr(panel, "_selection", None)
            if sel is not None:
                panel._selection = dataclasses.replace(sel, cache_generation=p._cache_generation)
        self.push_state()

    # ================================================================== #
    # FlagDB report (the "Describe pending flags" page)                    #
    # ================================================================== #

    def report_html(self) -> str:
        """A standalone HTML page describing the pending flags.

        Same look as the InfoTool page.  Lists every accepted operation in
        application order -- what it addresses (region or explicit
        samples), the filter and its parameters (with the code hash of the
        function), the value conditions, extend options, provenance and the
        samples it addresses (times, baselines, antennas, channels,
        correlations and per-baseline / per-antenna counts) -- plus the review/display
        settings and the undo/redo state.  Nothing here reads visibilities;
        sample counts are those computed when each operation was proposed.
        """
        from .visibility_scatter import VisibilityScatter
        esc = html.escape
        deltas = self.db.deltas()
        src = getattr(self._plotter, "_source_path", "") or ""
        spw_ids, all_spws = {}, None
        try:
            spw_ids = self.reader.spw_casa_ids()
            all_spws = [SpwKey.from_dict(k) for k in self.reader.flag_spw_table()]
        except Exception:
            log.debug("report: spw ids unavailable", exc_info=True)

        def row(k, v):
            return f"<tr><td class='cv-k'>{esc(k)}</td><td class='cv-v'>{v}</td></tr>"

        def utc(t, fmt):
            return time_to_datetime(t, fmt).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        n_flag = sum(1 for d in deltas if d.flag)
        total = sum(d.n_samples or 0 for d in deltas)
        data_fmt = self.capabilities().get("format")
        f = self._filter()
        summary = "".join([
            row("Source", esc(src)),
            row("Pending operations", f"{len(deltas)} ({n_flag} flag, {len(deltas) - n_flag} unflag)"),
            row("Samples (as proposed)", f"{total:,}"),
            row("Undo / redo available", f"{'yes' if self.db.can_undo() else 'no'} / "
                                         f"{'yes' if self.db.can_redo() else 'no'}"),
            row("Display", esc(self.display) + (f" (colour {esc(self.color)})"
                                               if self.display == "color" else "")),
            row("Preview", "on" if self.preview else "off"),
            row("Show flagged data", ("on (colour " + esc(self.flagged_color) + ")")
                if self.show_flagged else "off"),
            row("Current filter", esc(f.label) + (" " + esc(str(self.filter_params.get(f.name)))
                                                  if self.filter_params.get(f.name) else "")),
            row("Flag reaches (now set)", esc("; ".join(self.reach_words()) or "as drawn")),
            row("Reason (now set)", esc(self.reason) or "—"),
            row("Committed to disk", "no -- pending flags live only in this session until "
                                     "written (Export / commit → Write flags) or saved "
                                     "as JSON"),
        ])
        parts = [f"<table class='cv-tbl'>{summary}</table>"]
        if not deltas:
            parts.append("<p>No pending flag operations.</p>")
        for d in deltas:
            rows = [row("Action", esc(d.verb)),
                    *([row("Reason", esc(d.reason))] if d.reason else []),
                    row("Representation", "explicit samples (frozen when proposed)"
                        if d.is_sample_set else "coordinate region"),
                    row("Samples (as proposed)", f"{d.n_samples:,}" if d.n_samples is not None
                        else "—")]
            if d.filter is not None:
                rows.append(row("Filter", esc(d.filter.describe())
                                + f" <span class='cv-rect'>[{'built-in' if d.filter.builtin else 'user'}, "
                                  f"code {esc(d.filter.code_hash)}]</span>"))
            for vr in d.value_ranges:
                rows.append(row("Value condition",
                                f"{esc(vr.axis)} in [{vr.lo:.6g}, {vr.hi:.6g}]"
                                + (f" ({esc(vr.polarization)})" if vr.polarization else "")))
            if d.time_range is not None:
                rows += _time_rows(row, d.time_range[0], d.time_range[1], d.time_format,
                                   data_fmt)
            if d.scan_names is not None:
                rows.append(row("Scans", esc(", ".join(d.scan_names))))
            if d.field_names is not None:
                rows.append(row("Fields", esc(", ".join(d.field_names))))
            if d.baseline_ids is not None:
                bl = [f"{a}&{b}" for a, b in d.baseline_ids]
                rows.append(row("Baselines", f"{len(bl)}: " + esc(", ".join(bl[:40]))
                                + (" …" if len(bl) > 40 else "")))
            if d.antenna_names is not None:
                rows.append(row("Antennas", esc(", ".join(d.antenna_names))))
            if d.spw_channels is not None:
                rows.append(row("Channels", esc("; ".join(
                    f"{_spw_label(sc.spw, spw_ids)}: channels {sc.chan_lo}–{sc.chan_hi}"
                    for sc in d.spw_channels))))
            elif d.spw is not None:
                rows.append(row("Spectral windows", esc(", ".join(_spw_label(k, spw_ids)
                                                                   for k in d.spw))))
            if d.freq_range is not None:
                rows.append(row("Frequency", f"{d.freq_range[0] / 1e9:.9g} – "
                                             f"{d.freq_range[1] / 1e9:.9g} GHz"))
            if d.correlation is not None:
                rows.append(row("Correlations", esc(", ".join(d.correlation))))
            ext = [x for x, on in (("correlations", d.extend_corr), ("channels", d.extend_chan),
                                   ("spectral windows", d.extend_spw), ("scans", d.extend_scan))
                   if on]
            if ext:
                rows.append(row("Extended to all", esc(", ".join(ext))))
            if d.is_sample_set:
                rows += _sample_set_rows(d, row, utc, spw_ids, data_fmt)
            if d.data_column:
                rows.append(row("Data column", esc(d.data_column)))
            if d.provenance:
                rows.append(row("Provenance", esc(" → ".join(d.provenance))))
            rows.append(row("Created", esc(time.strftime("%Y-%m-%d %H:%M:%S",
                                                          time.localtime(d.created)))))
            parts.append(f"<h3>#{d.seq} — {_title_with_times(d)}</h3>"
                         f"<table class='cv-tbl'>{''.join(rows)}</table>"
                         + (_sample_set_details(d, utc) if d.is_sample_set else
                            _region_details(d)))
        return VisibilityScatter._probe_region_page(
            f"Pending flags — {os.path.basename(os.path.normpath(src)) or 'visplot'}",
            "".join(parts))

    # ================================================================== #
    # GUI                                                                  #
    # ================================================================== #

    def build_widgets(self, comm, msg_id: str, section=None, width: int = 260,
                      stylesheet=None, hover=None):
        """Flag section of the sidebar.  Returns a ``column``.

        *stylesheet* is a factory returning the sidebar's themed
        ``InlineStyleSheet`` (``VisibilityPlotter._dark``); every widget gets
        its own copy as ``stylesheets[0]`` so the Light/Dark toggle can
        restyle it like the rest of the sidebar (``themed_widgets()``).

        *hover* is ``VisibilityPlotter._hover``: ``hover(widget, name)``
        returns the widget wrapped so that the status-area help
        ``_hint_<name>`` shows while the pointer is over it.  All help
        here goes that way (2026-10-07); no widget carries a tooltip.
        The names are the ``flag_*`` keys of the plotter's static hints
        plus ``flagf_<filter>`` per filter (``filter_hints()``).
        """
        if hover is None:
            def hover(widget, _name):
                return widget
        from bokeh.layouts import column, row
        from bokeh.models import (Button, Checkbox, ColorPicker, CustomJS, Div,
                                  NumericInput, RadioButtonGroup, Select)
        names = self.registry.names()
        filt_sel = Select(title="Filter", value=self.filter_name, width=width,
                          options=[(n, self.registry.get(n).label
                                    + ("" if self.registry.get(n).builtin
                                       else (" (user, local data only)" if self.is_remote
                                             else " (user)")))
                                   for n in names])
        param_cols, param_widgets = {}, {}
        for n in names:
            f = self.registry.get(n)
            ws = []
            for spec in f.params:
                if not spec.gui:
                    continue
                label = spec.label or spec.name
                if spec.kind in ("float", "int"):
                    w = NumericInput(title=label, value=spec.default, width=width,
                                     mode="float" if spec.kind == "float" else "int",
                                     low=spec.min, high=spec.max)
                elif spec.kind == "choice":
                    w = Select(title=label, value=str(spec.default), width=width,
                               options=[str(c) for c in spec.choices])
                elif spec.kind == "bool":
                    w = Checkbox(label=label, active=bool(spec.default), width=width)
                else:
                    continue
                w.tags = [spec.name, spec.kind]
                ws.append(w)
            param_widgets[n] = ws
            desc = Div(text=f"<span style='color:#a6adc8;font-size:11px'>"
                            f"{html.escape(f.description)}</span>", width=width)
            # The block and its help region are one thing to show and
            # hide: the wrapper (or the bare column without hover).
            param_cols[n] = hover(column(desc, *ws, width=width), f"flagf_{n}")
            param_cols[n].visible = (n == self.filter_name)
        preview_cb = Checkbox(label="Preview each proposal", active=self.preview)
        # ---- Flag reaches (HRS H5) ----------------------------------- #
        from bokeh.models import TextInput
        reach_head = Div(text="<b>Flag reaches</b>", width=width,
                         styles={"font-size": "12px", "margin-top": "4px",
                                 "color": "#a6adc8"})
        reach_bl = Select(title="Baselines", value=self.scope["baselines"], width=width,
                          options=list(REACH_BASELINES.items()))
        ext_chan = Checkbox(label="All channels", active=self.extend_chan)
        reach_spw = Checkbox(label="All selected spectral windows", active=self.scope["spw"])
        reach_scan = Checkbox(label="Whole scan", active=self.scope["scan"])
        reach_field = Checkbox(label="All fields", active=self.scope["field"])
        ext_corr = Checkbox(label="All correlations", active=self.extend_corr)
        wide = bool(self.reach_words())
        for t in self.flag_tools:
            t.reach_wide = wide
        reach_note = Div(text=self.reach_text(), width=width,
                         styles={"font-size": "11px",
                                 "color": REACH_WARN_COLOR if wide else REACH_QUIET_COLOR})
        reason_in = TextInput(title="Reason (stored with each flag)", value=self.reason,
                              placeholder="e.g. RFI, antenna off source", width=width,
                              max_length=REASON_MAX)
        self._widgets.update(reach_bl=reach_bl, ext_chan=ext_chan, ext_corr=ext_corr,
                             reach_spw=reach_spw, reach_scan=reach_scan,
                             reach_field=reach_field, reach_note=reach_note,
                             reason=reason_in, preview=preview_cb, filter=filt_sel)
        display = RadioButtonGroup(labels=["Hide flagged", "Show in colour"],
                                   active=DISPLAY_MODES.index(self.display), width=width)
        color = ColorPicker(title="Pending colour", color=self.color, width=width)
        show_flagged = Checkbox(label="Show flagged data (to unflag)", active=self.show_flagged)
        flagged_color = ColorPicker(title="Flagged colour", color=self.flagged_color, width=width)
        # mid-grey: readable on both the dark and the light sidebar
        info = Div(text=self.info_text(), width=width,
                   styles={"font-size": "11px", "color": "#8c8fa1"})
        self._widgets["info"] = info
        btns = {k: Button(label=l, width=width // 3 - 4, button_type="default")
                for k, l in (("undo", "Undo"), ("redo", "Redo"), ("clear", "Clear"))}
        report_btn = Button(label="Describe pending flags", width=width)
        exp_sel = Select(title="Export / commit", value="json", width=width,
                         options=self.export_options())
        exp_path = TextInput(title="File (optional; required to load / restore)",
                             placeholder="default: <data name>.<kind>.<date-time>.<ext>",
                             width=width)
        exp_go = Button(label="Go", width=width, button_type="primary")
        # Restore: a dropdown of the backups found next to the data, filled
        # when "Restore" is chosen; picking one puts its path in the file
        # box (which still accepts any typed path).
        exp_backups = Select(title="Existing backups (newest first)", value="",
                             options=[("", "—")], width=width, visible=False)

        cfg_js = CustomJS(args=dict(comm=comm, msg_id=msg_id, filt_sel=filt_sel,
                                    param_widgets=param_widgets, param_cols=param_cols,
                                    preview_cb=preview_cb, ext_corr=ext_corr,
                                    ext_chan=ext_chan, reach_bl=reach_bl,
                                    reach_spw=reach_spw, reach_scan=reach_scan,
                                    reach_field=reach_field, reach_note=reach_note,
                                    reason_in=reason_in,
                                    reach_labels=dict(REACH_BASELINE_WORDS),
                                    reach_warn=REACH_WARN_COLOR,
                                    reach_quiet=REACH_QUIET_COLOR,
                                    flag_tools=list(self.flag_tools),
                                    display=display, color=color,
                                    show_flagged=show_flagged, flagged_color=flagged_color,
                                    **self._response_args()),
                          code=_CV_SET_BUSY_JS + _FLAG_RESPONSE_JS + _CONFIG_JS)
        for w in [filt_sel, preview_cb, ext_corr, ext_chan, reach_bl, reach_spw,
                  reach_scan, reach_field, reason_in, display, color,
                  show_flagged, flagged_color] + \
                 [w for ws in param_widgets.values() for w in ws]:
            prop = {"Select": "value", "NumericInput": "value", "Checkbox": "active",
                    "RadioButtonGroup": "active", "ColorPicker": "color",
                    "TextInput": "value"}[type(w).__name__]
            w.js_on_change(prop, cfg_js)
        exp_go.js_on_click(CustomJS(args=dict(comm=comm, msg_id=msg_id, exp_sel=exp_sel,
                                              exp_path=exp_path, exp_backups=exp_backups,
                                              **self._response_args()),
                                    code=_CV_SET_BUSY_JS + _FLAG_RESPONSE_JS + _EXPORT_JS))
        exp_sel.js_on_change("value", CustomJS(
            args=dict(comm=comm, msg_id=msg_id, exp_sel=exp_sel, exp_path=exp_path,
                      exp_backups=exp_backups, **self._response_args()),
            code=_CV_SET_BUSY_JS + _FLAG_RESPONSE_JS + _BACKUP_LIST_JS))
        exp_backups.js_on_change("value", CustomJS(
            args=dict(exp_path=exp_path, exp_backups=exp_backups),
            code="if (exp_backups.value) exp_path.value = exp_backups.value;"))
        for key, b in list(btns.items()):
            b.js_on_click(CustomJS(args=dict(comm=comm, msg_id=msg_id, action=key,
                                             **self._response_args()),
                                   code=_CV_SET_BUSY_JS + _FLAG_RESPONSE_JS + _ACTION_JS))
        report_btn.js_on_click(CustomJS(args=dict(comm=comm, msg_id=msg_id,
                                                  **self._response_args()),
                                        code=_CV_SET_BUSY_JS + _FLAG_RESPONSE_JS + _REPORT_JS))
        self._widgets.update(info=info)
        themed = ([filt_sel, preview_cb, ext_corr, ext_chan, reach_bl, reach_spw,
                   reach_scan, reach_field, reason_in, display, color, show_flagged,
                   flagged_color, exp_sel, exp_backups, exp_path, exp_go,
                   report_btn] + list(btns.values())
                  + [w for ws in param_widgets.values() for w in ws])
        if stylesheet is not None:
            for w in themed:
                w.stylesheets = [stylesheet()] + list(w.stylesheets or [])
        self._themed = themed
        # One hover region per subject, so the help does not flicker
        # while the pointer moves between the parts of one group.
        kids = ([section] if section is not None else []) + [
            hover(filt_sel, "flag_filter"),
            *param_cols.values(),
            hover(preview_cb, "flag_preview"),
            hover(column(reach_head, reach_bl, ext_chan, reach_spw, reach_scan,
                         reach_field, ext_corr, reach_note, width=width), "flag_reach"),
            hover(reason_in, "flag_reason"),
            hover(column(display, color, width=width), "flag_display"),
            hover(column(show_flagged, flagged_color, width=width), "flag_shown"),
            hover(row(*btns.values(), width=width), "flag_undo"),
            hover(column(exp_sel, exp_backups, exp_path, exp_go, width=width), "flag_export"),
            hover(report_btn, "flag_report"), info]
        return column(*kids, width=width)

    def filter_hints(self) -> dict:
        """Status-area help per filter: ``{"flagf_<name>": html}``.

        What the filter picks out of a box and what each of its
        parameters does (the text that used to be tooltips).
        """
        out = {}
        esc = html.escape
        for n in self.registry.names():
            f = self.registry.get(n)
            pts = [f"<b>{esc(f.label)}</b> \u2014 {esc(f.description)}"]
            for spec in f.params:
                if spec.gui and spec.help:
                    pts.append(f"<b>{esc(spec.label or spec.name)}</b>: {esc(spec.help)}")
            out[f"flagf_{n}"] = "  | ".join(pts)
        return out

    def export_hint(self) -> str:
        """Status-area help for the Export / commit group."""
        caps = self.capabilities()
        pts = ["<b>Export / commit</b> \u2014 what to do with the pending flags; nothing "
               "is written until <b>Go</b>",
               "<b>Save as JSON</b>: the flags as a file that can be loaded again here, "
               "with their reasons",
               "<b>Write flags</b> to the MS / PS: exact, checked afterwards, with a backup"
               + ("" if caps.get("write") else
                  " (unavailable: " + html.escape(caps.get("write_reason") or "") + ")")]
        if caps.get("casa"):
            pts.append("<b>Write flags with CASA flagdata</b>: the CASA route; CASA may apply "
                       "large or scattered selections incompletely, so prefer Write flags "
                       "for many points. The result is checked either way")
        pts.append("<b>File</b>: where to write, or what to load or restore; empty takes a "
                   "name made from the data name and the time")
        return "  | ".join(pts)

    def themed_widgets(self) -> list:
        """Widgets the Light/Dark toggle must restyle (``stylesheets[0]``)."""
        return list(getattr(self, "_themed", ()))

    def build_preview_box(self, comm, msg_id: str):
        """Hidden review dialog (shown by a proposal response)."""
        from bokeh.layouts import column, row
        from bokeh.models import Button, CustomJS, Div
        div = Div(text="", sizing_mode="stretch_width")
        acc = Button(label="Accept", button_type="success", width=110)
        rej = Button(label="Reject", button_type="danger", width=110)
        # Readable in both themes: page colours via the info-strip CSS
        # variables (set by the Light/Dark toggle), full-strength text, a
        # solid accent border (2026-09-30: grey-on-grey was hard to read).
        div.styles = {"color": "var(--cv-info-fg, #cdd6f4)", "font-size": "13px"}
        box = column(div, row(acc, rej), visible=False, sizing_mode="stretch_width",
                     styles={"background": "var(--cv-info-bg, #1e1e2e)", "padding": "8px 12px",
                             "border": f"2px solid {PROPOSAL_COLOR}", "border-radius": "6px"})
        self._widgets.update(preview_div=div, preview_box=box)
        for b, action in ((acc, "accept"), (rej, "reject")):
            b.js_on_click(CustomJS(args=dict(comm=comm, msg_id=msg_id, action=action,
                                             **self._response_args()),
                                   code=_CV_SET_BUSY_JS + _FLAG_RESPONSE_JS + _ACTION_JS))
        return box

    def _response_args(self) -> dict:
        """Models the shared JS response handler touches."""
        p = self._plotter
        ranges = [getattr(getattr(obj, "_fig", None), "x_range", None)
                  for obj in getattr(p, "_all_panels", ())]
        return dict(notify_div=getattr(p, "_notify_div", None),
                    status_div=getattr(p, "_status_div", None),
                    info_div=self._widgets.get("info"),
                    preview_div=self._widgets.get("preview_div"),
                    preview_box=self._widgets.get("preview_box"),
                    ranges=[r for r in ranges if r is not None])

    def response_callback(self):
        """CustomJS for ``FlagTool.response_callback`` (``cb_data.response``)."""
        from bokeh.models import CustomJS
        return CustomJS(args=self._response_args(),
                        code=_CV_SET_BUSY_JS + _FLAG_RESPONSE_JS + "cvApplyFlagResponse(cb_data.response);")


def scatter_layer_entries(panel) -> list:
    """The visible layers of a scatter panel as engine request entries.

    Shared by the FlagTool (``FlagController.build_request``) and the
    InfoTool box probe so both address exactly the same samples: hidden
    layers (alpha 0) are skipped and categories hidden by a categorical
    colouring are excluded.
    """
    layers = []
    for lyr in getattr(panel, "_layers", ()):
        if getattr(lyr, "alpha", 1.0) <= 0.0:
            continue
        entry = {"y_axis": lyr.y_axis.name, "polarization": str(lyr.polarization)}
        if (getattr(lyr, "coloring", "") == "categorical"
                and getattr(lyr, "excluded_display", "") == "hide"
                and lyr.excluded_categories and lyr.colorize_axis is not None):
            entry["hide_axis"] = lyr.colorize_axis.name
            entry["hide_values"] = [str(v) for v in lyr.excluded_categories]
        layers.append(entry)
    return layers


def _plotted_axis(panel, which: str) -> Axis:
    """The axis whose units the drawn box is in.

    A raster Channel axis is only relabelled to channel indices when every
    partition shares one frequency axis (``data.reader.to_channel_index``
    marks the aggregate); otherwise the aggregate keeps frequencies in Hz
    even though the axis is still called Channel.  The aggregate is the
    truth for what the box coordinates mean.
    """
    info = getattr(panel, f"_{which}_info", None)
    ax = getattr(info, "axis", None)
    ax = ax if isinstance(ax, Axis) else getattr(panel, f"_{which}_dim")
    agg = getattr(panel, "_agg", None)
    if ax == Axis.CHANNEL and agg is not None and hasattr(agg, "attrs"):
        from .data.reader import CHANNEL_AXIS_ATTR
        if CHANNEL_AXIS_ATTR not in agg.attrs:
            return Axis.FREQUENCY
    return ax


def _accepts_proposal(fn) -> bool:
    import inspect
    try:
        return len(inspect.signature(fn).parameters) >= 4
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------- #
# Browser side (no Bokeh server: responses are applied explicitly)        #
# ---------------------------------------------------------------------- #

_FLAG_RESPONSE_JS = r"""
function cvApplyFlagResponse(resp) {
    if (!resp) return;
    if (resp.notify_text != null && notify_div) {
        notify_div.text = resp.notify_text;
        if (resp.notify_color != null)
            notify_div.styles = {...notify_div.styles, color: resp.notify_color};
    }
    if (resp.status_text != null && status_div) status_div.text = resp.status_text;
    if (resp.flag_info != null && info_div) info_div.text = resp.flag_info;
    if (resp.preview && preview_box) {
        preview_div.text = resp.preview.html;
        preview_box.tags = [resp.preview.id];
        preview_box.visible = true;
    }
    if (resp.preview_closed && preview_box) preview_box.visible = false;
    // Re-render the visible panels at their current viewport: emitting the
    // x-range 'end' change runs each figure's own pan/zoom re-render
    // callback, and the Python side re-queries a panel whose pending-flag
    // state changed before drawing it.
    const idx = resp.refresh || [];
    if (idx.length && window.__cvSetBusy) {
        // Bridge the pan/zoom re-render's 300 ms debounce: each panel's
        // re-render request sets busy itself only when it is actually sent,
        // so without this hold the cursor went idle between this reply and
        // the redraw.  Released once those requests are under way.
        window.__cvSetBusy(true);
        setTimeout(function() { window.__cvSetBusy(false); }, 450);
    }
    for (const i of idx) {
        const r = ranges[i];
        if (r) r.properties.end.change.emit();
    }
}
"""

_ACTION_JS = r"""
window.__cvSetBusy(true);
const payload = {action: action};
if (preview_box && preview_box.tags && preview_box.tags.length) payload.id = preview_box.tags[0];
comm.send(msg_id, payload, (resp) => { window.__cvSetBusy(false); cvApplyFlagResponse(resp); });
"""

_CONFIG_JS = r"""
const name = filt_sel.value;
for (const [n, col] of Object.entries(param_cols)) col.visible = (n === name);
const params = {};
for (const w of (param_widgets[name] || [])) {
    const [pname, kind] = w.tags;
    if (kind === 'bool') params[pname] = !!w.active;
    else if (w.value !== null && w.value !== undefined && w.value !== '') params[pname] = w.value;
}
// The line under "Flag reaches": plain when every setting is "as drawn",
// a warning when any is wider (the settings stay until changed, so the
// user must be able to see at a glance that the next box is not just a box).
{
    const wide = [];
    if (reach_bl.value !== 'drawn') wide.push(String(reach_labels[reach_bl.value] || ''));
    if (ext_chan.active) wide.push('all channels');
    if (reach_spw.active) wide.push('all selected spectral windows');
    if (reach_scan.active) wide.push('the whole scan');
    if (reach_field.active) wide.push('all fields');
    if (ext_corr.active) wide.push('all correlations');
    reach_note.text = wide.length ? '\u26a0 Each box also takes: ' + wide.join('; ') + '.'
                                  : 'Flags cover what is drawn.';
    reach_note.styles = {...reach_note.styles, color: wide.length ? reach_warn : reach_quiet};
    for (const t of flag_tools) t.reach_wide = wide.length > 0;
}
window.__cvSetBusy(true);
comm.send(msg_id, {action: 'config', filter: name, params: params,
                   preview: !!preview_cb.active, extend_corr: !!ext_corr.active,
                   extend_chan: !!ext_chan.active,
                   reach: {baselines: reach_bl.value, spw: !!reach_spw.active,
                           scan: !!reach_scan.active, field: !!reach_field.active},
                   reason: reason_in.value || '',
                   display: display.active === 1 ? 'color' : 'hide',
                   color: color.color, show_flagged: !!show_flagged.active,
                   flagged_color: flagged_color.color},
          (resp) => { window.__cvSetBusy(false); cvApplyFlagResponse(resp); });
"""

_REPORT_JS = r"""
// Open the tab synchronously inside the click (popup blockers only allow
// window.open from the user gesture itself), then fill it when Python
// answers -- the same pattern as the InfoTool page.
let w = null;
try { w = window.open("", "_blank"); } catch (e) { console.warn("[Flagging] window.open failed", e); }
if (w) w.document.write("<html><body style='background:#1e1e2e;color:#cdd6f4;" +
                        "font-family:sans-serif'><p>Building pending-flag report…</p></body></html>");
window.__cvSetBusy(true);
comm.send(msg_id, {action: 'report'}, (resp) => {
    window.__cvSetBusy(false);
    cvApplyFlagResponse(resp);
    if (!resp || resp.report_html == null) return;
    if (w && !w.closed) { w.document.open(); w.document.write(resp.report_html); w.document.close(); }
    else if (notify_div) { notify_div.text = "⚠ Pop-up blocked: allow pop-ups to see the report."; }
});
"""

_EXPORT_JS = r"""
window.__cvSetBusy(true);
comm.send(msg_id, {action: 'export', kind: exp_sel.value, path: exp_path.value || ''},
          (resp) => {
              window.__cvSetBusy(false);
              if (resp && resp.export_path != null) exp_path.value = resp.export_path;
              cvApplyFlagResponse(resp);
          });
"""


# ---------------------------------------------------------------------- #
# Report helpers (the "Describe pending flags" page)                       #
# ---------------------------------------------------------------------- #

def _spw_label(key, spw_ids) -> str:
    sid = (spw_ids or {}).get(key)
    if sid is None:          # keys rebuilt from JSON may differ in the last float bits
        for k, v in (spw_ids or {}).items():
            if hasattr(key, "matches") and key.matches(k):
                sid = v
                break
    head = f"SPW {sid}" if sid is not None else "SPW"
    return (f"{head} {key.ident} ({key.n_chan} ch, "
            f"{key.freq_min / 1e9:.6f}–{key.freq_max / 1e9:.6f} GHz)")


def _ranges(vals) -> str:
    """'3–7, 12, 20–21' from integers."""
    import numpy as np
    v = np.unique(np.asarray(vals, dtype=np.int64))
    if v.size == 0:
        return "—"
    out, start, prev = [], v[0], v[0]
    for x in v[1:]:
        if x == prev + 1:
            prev = x
            continue
        out.append(f"{start}" if start == prev else f"{start}–{prev}")
        start = prev = x
    out.append(f"{start}" if start == prev else f"{start}–{prev}")
    return ", ".join(out)


def _sample_set_rows(d, row, utc, spw_ids, data_format=None) -> list:
    """Summary rows for an explicit-sample operation."""
    import numpy as np
    esc = html.escape
    rows = []
    t_all = np.concatenate([np.asarray(b.times) for b in d.samples]) if d.samples else []
    if len(t_all):
        rows += _time_rows(row, float(np.min(t_all)), float(np.max(t_all)), d.time_format,
                           data_format)
    for b in d.samples:
        g = b.dense()
        used_t = int(g.any(axis=(1, 2, 3)).sum())
        used_b = int(g.any(axis=(0, 2, 3)).sum())
        chans = np.asarray(b.chans)[g.any(axis=(0, 1, 3))]
        pols = [p for p, on in zip(b.pols, g.any(axis=(0, 1, 2))) if on]
        rows.append(row(_spw_label(b.spw, spw_ids),
                        esc(f"{b.count:,} samples · {used_t} integrations · {used_b} baselines"
                            f" · channels {_ranges(chans)} · correlations {', '.join(pols)}")))
    return rows


def _sample_set_details(d, utc) -> str:
    """Collapsible per-baseline and per-antenna sample counts and the full
    list of integrations of an explicit-sample operation."""
    import numpy as np
    from collections import Counter
    esc = html.escape
    per_bl, per_ant, per_pol, times = Counter(), Counter(), Counter(), set()
    for b in d.samples:
        g = b.dense()
        nb = g.sum(axis=(0, 2, 3))
        for i in np.flatnonzero(nb):
            a1, a2 = str(b.ant1[i]), str(b.ant2[i])
            per_bl[f"{a1}&{a2}"] += int(nb[i])
            per_ant[a1] += int(nb[i])
            if a2 != a1:
                per_ant[a2] += int(nb[i])
        for p, n in zip(b.pols, g.sum(axis=(0, 1, 2))):
            if n:
                per_pol[str(p)] += int(n)
        for i in np.flatnonzero(g.any(axis=(1, 2, 3))):
            times.add(float(b.times[i]))

    def table(counter, head):
        items = counter.most_common()
        body = "".join(f"<tr><td class='cv-k'>{esc(k)}</td><td class='cv-v'>{v:,}</td></tr>"
                       for k, v in items)
        return (f"<details><summary class='cv-rect'>{head} ({len(items)})</summary>"
                f"<table class='cv-tbl'>{body}</table></details>")
    tl = sorted(times)
    tlist = ", ".join(utc(t, d.time_format)[11:] for t in tl[:500]) + (" …" if len(tl) > 500 else "")
    return (table(per_pol, "Samples per correlation")
            + table(per_ant, "Samples per antenna")
            + table(per_bl, "Samples per baseline")
            + f"<details><summary class='cv-rect'>Integrations ({len(tl)}, UTC)</summary>"
              f"<p class='cv-rect' style='white-space:normal'>{esc(tlist)}</p></details>")


def _region_details(d) -> str:
    """Collapsible full baseline list of a region operation."""
    esc = html.escape
    if not d.baseline_ids or len(d.baseline_ids) <= 40:
        return ""
    bl = ", ".join(f"{a}&{b}" for a, b in d.baseline_ids)
    return (f"<details><summary class='cv-rect'>All baselines ({len(d.baseline_ids)})</summary>"
            f"<p class='cv-rect' style='white-space:normal'>{esc(bl)}</p></details>")


# ---------------------------------------------------------------------- #
# Time ranges: raw values and UTC                                           #
# ---------------------------------------------------------------------- #

_MJD_UNIX = 40587.0 * 86400.0


def _baseline_span(bl_axis, a, b) -> str:
    """The baselines a box covers along a displayed Baseline axis, for a
    provenance string: ``[#3 DA41&DA45]`` or ``[#3 DA41&DA45 .. #71
    DA44&DV23, 12 baselines by length]``."""
    lo, hi = min(float(a), float(b)), max(float(a), float(b))
    ids = bl_axis.ids_in(lo, hi)
    if not ids:
        return "[none]"
    names = dict(zip(bl_axis.ids, bl_axis.names))

    def one(i):
        n = names.get(i, "")
        return f"#{i} {n}".strip()
    if len(ids) == 1:
        return f"[{one(ids[0])}]"
    return (f"[{one(ids[0])} .. {one(ids[-1])}, {len(ids)} baselines"
            f" by {bl_axis.order}]")


def _rng(axis, a, b) -> str:
    """``[a, b]`` for provenance strings.  TIME keeps every digit of the
    stored value (to the millisecond) -- %.6g turned 1353311129.52 into
    1.35331e+09, losing the integration -- other axes stay compact."""
    if axis is not None and getattr(axis, "name", "") == "TIME":
        return f"[{float(a):.3f}, {float(b):.3f}]"
    return f"[{float(a):.6g}, {float(b):.6g}]"


def _time_text(t0: float, t1: float, time_format: str = "unix") -> str:
    """A time range as the raw stored values, the MS TIME column values
    (MJD seconds), and UTC -- e.g. for cross-checking against the MS."""
    fmt = (time_format or "unix").lower()
    if "mjd" in fmt:
        mjd0, mjd1 = t0, t1
        unix0, unix1 = t0 - _MJD_UNIX, t1 - _MJD_UNIX
    else:
        unix0, unix1 = t0, t1
        mjd0, mjd1 = t0 + _MJD_UNIX, t1 + _MJD_UNIX
    u0 = time_to_datetime(t0, time_format).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    u1 = time_to_datetime(t1, time_format).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    if u0[:10] == u1[:10]:
        u1 = u1[11:]
    return (f"{u0} – {u1} UTC  ·  unix {unix0:.3f} – {unix1:.3f} s  ·  "
            f"MS TIME (MJD s) {mjd0:.3f} – {mjd1:.3f}")


def _time_rows(row, t0: float, t1: float, time_format: str, data_format) -> list:
    """Two rows: the span in UTC, and as the data set stores it -- the MS
    TIME column (MJD seconds) for MSv2, UNIX seconds for MSv4 (PS) --
    at full precision, for cross-checking against the data."""
    esc = html.escape
    mjd = "mjd" in (time_format or "unix").lower()
    unix0, unix1 = (t0 - _MJD_UNIX, t1 - _MJD_UNIX) if mjd else (t0, t1)
    u0 = time_to_datetime(t0, time_format).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    u1 = time_to_datetime(t1, time_format).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    if data_format == "msv4":
        raw = row("PS time span", esc(f"{unix0:.3f} – {unix1:.3f} s (UNIX seconds, as stored "
                                      "in the processing set)"))
    else:
        raw = row("MS time span", esc(f"{unix0 + _MJD_UNIX:.3f} – {unix1 + _MJD_UNIX:.3f} s "
                                      "(MJD seconds, MS TIME column)"))
    return [row("UTC time span", f"{u0} – {u1} UTC"), raw]


def _title_with_times(d) -> str:
    """The operation's one-line description with its TIME box shown as raw
    values and UTC (descriptions store the plotted values)."""
    import re
    esc = html.escape
    text = esc(d.describe())

    def repl(m):
        try:
            a, b = float(m.group(1)), float(m.group(2))
        except ValueError:
            return m.group(0)
        return f"TIME [{a:.3f}, {b:.3f}] ({_time_text(a, b, d.time_format).split('  ·  ')[0]})"
    return re.sub(r"TIME \[([-+0-9.eE]+), ([-+0-9.eE]+)\]", repl, text)

_BACKUP_LIST_JS = r"""
if (exp_sel.value !== 'restore') {
    exp_backups.visible = false;
} else {
    window.__cvSetBusy(true);
    comm.send(msg_id, {action: 'export', kind: 'list_backups', path: ''}, (resp) => {
        window.__cvSetBusy(false);
        const items = (resp && resp.backups) || [];
        exp_backups.options = items.length ? items : [['', '(no backups found)']];
        exp_backups.value = items.length ? items[0][0] : '';
        if (items.length) exp_path.value = items[0][0];
        exp_backups.visible = true;
        cvApplyFlagResponse(resp);
    });
}
"""

