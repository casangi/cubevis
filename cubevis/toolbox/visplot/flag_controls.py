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
import uuid
from dataclasses import dataclass, field as dc_field
from typing import Any, Mapping, Optional

from .axes import Axis
from .flag_db import FlagDB
from .flag_filters import FilterRegistry, FlagFilter
from .flag_model import FlagCounts, FlagDelta, SpwKey, time_to_datetime

log = logging.getLogger(__name__)

NOTIFY_OK = "#a6e3a1"
NOTIFY_WARN = "#f38ba8"
PROPOSAL_COLOR = "#fab387"       # orange: proposal under review
DEFAULT_PENDING_COLOR = "#ff00ff"
DISPLAY_MODES = ("hide", "color")
DEFAULT_FLAGGED_COLOR = "#7f849c"   # grey: flagged data shown for unflagging


@dataclass
class Proposal:
    """A resolved flag request awaiting acceptance (not in the DB)."""
    delta: FlagDelta
    counts: FlagCounts
    warnings: list
    kind: str
    db_version: int
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
                 flagged_color: str = DEFAULT_FLAGGED_COLOR) -> None:
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
        self.proposal: Optional[Proposal] = None
        self.state_version = 0
        self._widgets: dict = {}
        self.db.add_listener(lambda _v: self.push_state())

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

    def push_state(self) -> None:
        """Send the pending state to the backend, re-stamp every panel's
        selection and mark every panel stale (re-queried on next render)."""
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
            prov = (f"raster box {x_ax.name} [{req['x0']:.6g}, {req['x1']:.6g}] x "
                    f"{y_ax.name} [{req['y0']:.6g}, {req['y1']:.6g}] "
                    f"({req['polarization']})")
        else:
            layers = scatter_layer_entries(panel)
            req.update(x_axis=x_ax.name, layers=layers)
            prov = (f"scatter box {x_ax.name} [{req['x0']:.6g}, {req['x1']:.6g}] x "
                    f"[{req['y0']:.6g}, {req['y1']:.6g}] on "
                    + ", ".join(f"{l['y_axis']}({l['polarization']})" for l in layers))
        req["provenance"] = [prov]
        req["comment"] = prov
        return req

    def evaluate(self, req: dict) -> Proposal:
        result = self.reader.evaluate_flag_request(req)
        counts = FlagCounts.from_dict(result.get("counts") or {})
        d = result.get("delta")
        delta = FlagDelta.from_dict(d) if isinstance(d, dict) else d
        return Proposal(delta=delta, counts=counts, warnings=list(result.get("warnings") or ()),
                        kind=req.get("kind", ""), db_version=self.db.version)

    async def handle_box(self, msg: dict, kind: str, panel) -> dict:
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
            if action in ("export_flagdata", "export_jsonl"):
                path = self.export(fmt="flagdata" if action == "export_flagdata" else "jsonl")
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
        if "display" in msg and msg["display"] in DISPLAY_MODES and msg["display"] != self.display:
            self.display = msg["display"]
            refresh = True
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
            self.push_state()
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
        if d.extend_corr or d.extend_chan:
            rows.append(("Extend", ", ".join(x for x, on in (("all correlations", d.extend_corr),
                                                              ("all channels", d.extend_chan)) if on)))
        rows.append(("Provenance", html.escape(" → ".join(d.provenance))))
        if prop.warnings:
            rows.append(("Notes", html.escape("; ".join(prop.warnings))))
        body = "".join(f"<tr><td style='padding-right:10px;color:#a6adc8'>{k}</td>"
                       f"<td>{v}</td></tr>" for k, v in rows)
        return (f"<div style='font-family:monospace;font-size:12px'>"
                f"<b style='color:{PROPOSAL_COLOR}'>Flag proposal</b>"
                f"<table>{body}</table></div>")

    # ================================================================== #
    # Export / Python API                                                  #
    # ================================================================== #

    def export(self, path: Optional[str] = None, fmt: str = "flagdata") -> str:
        from .flag_export import to_flagdata_lines, to_jsonl
        src = getattr(self._plotter, "_source_path", "") or "visplot"
        base = os.path.basename(os.path.normpath(src)) or "visplot"
        if path is None:
            path = base + (".flagcmd.txt" if fmt == "flagdata" else ".flags.jsonl")
        deltas = self.db.deltas()
        if fmt == "flagdata":
            spw_ids = {}
            all_spws = None
            try:
                spw_ids = self.reader.spw_casa_ids()
                all_spws = [SpwKey.from_dict(k) for k in self.reader.flag_spw_table()]
            except Exception:
                log.debug("spw id lookup unavailable", exc_info=True)
            lines = [f"# visplot pending flags for {src}; apply in order with "
                     f"flagdata(vis=..., mode='list', inpfile='{os.path.basename(path)}')"]
            lines += to_flagdata_lines(deltas, spw_ids=spw_ids, all_spws=all_spws)
            text = "\n".join(lines) + "\n"
        elif fmt == "jsonl":
            text = to_jsonl(deltas, {"source": src, "exported": time.time()})
        else:
            raise ValueError(f"unknown export format {fmt!r}")
        with open(path, "w") as fh:
            fh.write(text)
        return os.path.abspath(path)

    # ================================================================== #
    # FlagDB report (the "Describe pending flags" page)                    #
    # ================================================================== #

    def report_html(self) -> str:
        """A standalone HTML page describing the pending flags.

        Same look as the InfoTool page.  Lists every accepted operation in
        application order -- what it addresses (region or explicit
        samples), the filter and its parameters (with the code hash of the
        function), the value conditions, extend options, provenance and the
        exact ``flagdata`` commands it exports to -- plus the review/display
        settings and the undo/redo state.  Nothing here reads visibilities;
        sample counts are those computed when each operation was proposed.
        """
        from .flag_export import to_flagdata_lines, ambiguous_spws
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
            row("Extend", ", ".join(x for x, on in (("all correlations", self.extend_corr),
                                                    ("all channels", self.extend_chan)) if on)
                or "none"),
            row("Committed to disk", "no -- pending flags live only in this session "
                                     "until exported or committed"),
        ])
        parts = [f"<table class='cv-tbl'>{summary}</table>"]
        if all_spws is not None:
            amb = ambiguous_spws(all_spws, spw_ids)
            if amb:
                parts.append("<p class='cv-rect'>⚠ Spectral window(s) "
                             + ", ".join(esc(str(k.ident)) for k in amb)
                             + " cannot be identified uniquely without an SPW id; "
                               "exported selections may also match other windows.</p>")
        if not deltas:
            parts.append("<p>No pending flag operations.</p>")
        for d in deltas:
            rows = [row("Action", esc(d.verb)),
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
                rows.append(row("Time", f"{utc(d.time_range[0], d.time_format)} – "
                                        f"{utc(d.time_range[1], d.time_format)} UTC"))
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
                    f"SPW {sc.spw.ident} ({sc.spw.n_chan} ch): {sc.chan_lo}–{sc.chan_hi}"
                    for sc in d.spw_channels))))
            elif d.spw is not None:
                rows.append(row("Spectral windows", esc(", ".join(str(k.ident) for k in d.spw))))
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
                blk = "; ".join(f"SPW {b.spw.ident}: {b.count:,} samples in {len(b.times)} "
                                f"integrations × {len(b.ant1)} baselines × {len(b.freqs)} "
                                f"channels × {len(b.pols)} correlations"
                                for b in d.samples)
                rows.append(row("Samples by window", esc(blk)))
            if d.data_column:
                rows.append(row("Data column", esc(d.data_column)))
            if d.provenance:
                rows.append(row("Provenance", esc(" → ".join(d.provenance))))
            rows.append(row("Created", esc(time.strftime("%Y-%m-%d %H:%M:%S",
                                                          time.localtime(d.created)))))
            lines = to_flagdata_lines([d], spw_ids=spw_ids, comments=False)
            shown = lines[:200]
            cmd = esc("\n".join(shown)) + (f"\n… ({len(lines) - 200} more lines)"
                                            if len(lines) > 200 else "")
            parts.append(f"<h3>#{d.seq} — {esc(d.describe())}</h3>"
                         f"<table class='cv-tbl'>{''.join(rows)}</table>"
                         f"<details><summary class='cv-rect'>flagdata commands "
                         f"({len(lines)} line{'s' if len(lines) != 1 else ''})</summary>"
                         f"<pre class='cv-rect' style='white-space:pre-wrap'>{cmd}</pre>"
                         f"</details>")
        return VisibilityScatter._probe_region_page(
            f"Pending flags — {os.path.basename(os.path.normpath(src)) or 'visplot'}",
            "".join(parts))

    # ================================================================== #
    # GUI                                                                  #
    # ================================================================== #

    def build_widgets(self, comm, msg_id: str, section=None, width: int = 260,
                      stylesheet=None):
        """Flag section of the sidebar.  Returns a ``column``.

        *stylesheet* is a factory returning the sidebar's themed
        ``InlineStyleSheet`` (``VisibilityPlotter._dark``); every widget gets
        its own copy as ``stylesheets[0]`` so the Light/Dark toggle can
        restyle it like the rest of the sidebar (``themed_widgets()``).
        """
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
                if spec.help and "description" in w.properties():
                    w.description = spec.help      # (Checkbox has no tooltip)
                ws.append(w)
            param_widgets[n] = ws
            desc = Div(text=f"<span style='color:#a6adc8;font-size:11px'>"
                            f"{html.escape(f.description)}</span>", width=width)
            param_cols[n] = column(desc, *ws, visible=(n == self.filter_name))
        preview_cb = Checkbox(label="Preview each proposal", active=self.preview)
        ext_corr = Checkbox(label="Extend to all correlations", active=self.extend_corr)
        ext_chan = Checkbox(label="Extend to all channels", active=self.extend_chan)
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
        exp_fd = Button(label="Export flagdata", width=width // 2 - 4)
        exp_js = Button(label="Export JSONL", width=width // 2 - 4)

        cfg_js = CustomJS(args=dict(comm=comm, msg_id=msg_id, filt_sel=filt_sel,
                                    param_widgets=param_widgets, param_cols=param_cols,
                                    preview_cb=preview_cb, ext_corr=ext_corr,
                                    ext_chan=ext_chan, display=display, color=color,
                                    show_flagged=show_flagged, flagged_color=flagged_color,
                                    **self._response_args()),
                          code=_FLAG_RESPONSE_JS + _CONFIG_JS)
        for w in [filt_sel, preview_cb, ext_corr, ext_chan, display, color,
                  show_flagged, flagged_color] + \
                 [w for ws in param_widgets.values() for w in ws]:
            prop = {"Select": "value", "NumericInput": "value", "Checkbox": "active",
                    "RadioButtonGroup": "active", "ColorPicker": "color"}[type(w).__name__]
            w.js_on_change(prop, cfg_js)
        for key, b in list(btns.items()) + [("export_flagdata", exp_fd), ("export_jsonl", exp_js)]:
            b.js_on_click(CustomJS(args=dict(comm=comm, msg_id=msg_id, action=key,
                                             **self._response_args()),
                                   code=_FLAG_RESPONSE_JS + _ACTION_JS))
        report_btn.js_on_click(CustomJS(args=dict(comm=comm, msg_id=msg_id,
                                                  **self._response_args()),
                                        code=_FLAG_RESPONSE_JS + _REPORT_JS))
        self._widgets.update(info=info)
        themed = ([filt_sel, preview_cb, ext_corr, ext_chan, display, color, show_flagged,
                   flagged_color, exp_fd, exp_js,
                   report_btn] + list(btns.values())
                  + [w for ws in param_widgets.values() for w in ws])
        if stylesheet is not None:
            for w in themed:
                w.stylesheets = [stylesheet()] + list(w.stylesheets or [])
        self._themed = themed
        kids = ([section] if section is not None else []) + [
            filt_sel, *param_cols.values(), preview_cb, ext_corr, ext_chan,
            display, color, show_flagged, flagged_color, row(*btns.values()), row(exp_fd, exp_js), report_btn, info]
        return column(*kids, width=width)

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
        box = column(div, row(acc, rej), visible=False, sizing_mode="stretch_width",
                     styles={"background": "#313244", "padding": "6px 10px",
                             "border": f"2px dashed {PROPOSAL_COLOR}"})
        self._widgets.update(preview_div=div, preview_box=box)
        for b, action in ((acc, "accept"), (rej, "reject")):
            b.js_on_click(CustomJS(args=dict(comm=comm, msg_id=msg_id, action=action,
                                             **self._response_args()),
                                   code=_FLAG_RESPONSE_JS + _ACTION_JS))
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
                        code=_FLAG_RESPONSE_JS + "cvApplyFlagResponse(cb_data.response);")


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
    if (window.__cvSetBusy) window.__cvSetBusy(false);
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
    for (const i of idx) {
        const r = ranges[i];
        if (r) r.properties.end.change.emit();
    }
}
"""

_ACTION_JS = r"""
if (window.__cvSetBusy) window.__cvSetBusy(true);
const payload = {action: action};
if (preview_box && preview_box.tags && preview_box.tags.length) payload.id = preview_box.tags[0];
comm.send(msg_id, payload, cvApplyFlagResponse);
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
comm.send(msg_id, {action: 'config', filter: name, params: params,
                   preview: !!preview_cb.active, extend_corr: !!ext_corr.active,
                   extend_chan: !!ext_chan.active,
                   display: display.active === 1 ? 'color' : 'hide',
                   color: color.color, show_flagged: !!show_flagged.active,
                   flagged_color: flagged_color.color}, cvApplyFlagResponse);
"""

_REPORT_JS = r"""
// Open the tab synchronously inside the click (popup blockers only allow
// window.open from the user gesture itself), then fill it when Python
// answers -- the same pattern as the InfoTool page.
let w = null;
try { w = window.open("", "_blank"); } catch (e) { console.warn("[Flagging] window.open failed", e); }
if (w) w.document.write("<html><body style='background:#1e1e2e;color:#cdd6f4;" +
                        "font-family:sans-serif'><p>Building pending-flag report…</p></body></html>");
if (window.__cvSetBusy) window.__cvSetBusy(true);
comm.send(msg_id, {action: 'report'}, (resp) => {
    cvApplyFlagResponse(resp);
    if (!resp || resp.report_html == null) return;
    if (w && !w.closed) { w.document.open(); w.document.write(resp.report_html); w.document.close(); }
    else if (notify_div) { notify_div.text = "⚠ Pop-up blocked: allow pop-ups to see the report."; }
});
"""
