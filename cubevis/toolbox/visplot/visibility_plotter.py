"""visibility_plotter.py
========================
``VisibilityPlotter`` — astronomer-facing combined raster + scatter
visibility inspection and flagging tool.

This is a cubevis *application* class in the sense of
``cubevis-app-startup.md``: it owns a ``BokehAppContext``, a ``CommMgr``,
and an async server coroutine, and exposes a ``show()`` entry point for
Jupyter notebook use.

Astronomer-facing API
---------------------
The constructor accepts only strings, numbers, and lists — no internal
objects.  The same call works in the preview and in the full release;
no API changes are required later.

::

    from cubevis.toolbox.visplot import VisibilityPlotter

    plotter = VisibilityPlotter(
        ms          = "sis14_twhya_calibrated_flagged.ms",
        field       = "0637-752",
        spw         = "0,1,2,3",
        correlation = "XX,YY",
        datacolumn  = "data",
        layout      = "side",
        enable_flagging = True,   # False -> quick-look, no Flag/Unflag tools
    )
    plotter.show()

Preview scope
-------------
* Layout control (unified, replaces the former separate mode + layout
  toggles as of July 29 2026): One / Side by Side / Over-Under (CustomJS)
* ``enable_flagging`` (default True): adds the FlagTool / Unflag drag
  tools to both panels' toolbars, replacing box-select. Set False for a
  quick-look instance with no flagging workflow at all — only
  pan/wheel-zoom/box-zoom/reset/save remain.
* Dual container approach for Side by Side / Over-Under — both Bokeh
  row/column containers always in the document, one hidden
* Collapsible sidebar with ⟨/⟩ toggle button
* Dark-mode sidebar widget styling via InlineStyleSheet
* Shared toolbar: pan, zoom, reset applied to both figures simultaneously;
  individual figure toolbars hidden (toolbar_location=None)
* Cursor tracking: raster._info_div and scatter._info_div surfaced via
  raster.layout / scatter.layout
* Session-scoped layout preference memory (ColumnDataSource JSON store)
* Sidebar: data selection (field, SPW, correlation, data column),
  raster axis controls, scatter axis controls, colormap controls
* Toolbar: Plot ▶, Reload ↺, ⟨Sidebar, display mode, layout, presets,
  pan/zoom/reset. Flag ⚑ / Unflag are per-figure drag tools (see
  ``enable_flagging`` above), not top-level buttons.
* Flag/Unflag box → FlagDB accumulation + red overlay re-render stub
* Linked x-axis Range1d when both panels share x dimension
* Status bar Div

Absent from the preview (Phase 2+):
* Writing flags to disk
* Locate, Save plot, Copy flagdata
* Averaging controls
* Calibration sidebar section

Iteration (I-1, Phase 2.5, added 2026-08-19): one "Animate: Field | SPW"
selector plus Prev/Next buttons in the toolbar, stepping through
``meta.fields`` / ``meta.spws`` with wrap-around at the ends. Antenna,
Baseline, Scan, and Time iteration (I-3) and grid-mode iteration (X-1)
remain out of scope — see ``visplot_duo_iteration_kickoff.md``.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/visibility_plotter.py``
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from dataclasses import dataclass
from typing import Callable, ClassVar, Optional
from uuid import uuid4

import websockets

from bokeh.events import MouseEnter, MouseLeave
from bokeh.layouts import column, row
from bokeh.models import (
    Button, CheckboxGroup, ColumnDataSource, CustomAction, CustomJS,
    DataTable, Div, InlineStyleSheet, MultiSelect, RadioButtonGroup,
    Select, TabPanel, Tabs, TableColumn, TextInput, Toggle,
)

from cubevis.bokeh import BokehInit
from cubevis.bokeh.models import BokehAppContext, Showable, EvTextInput, Tip
from bokeh.models import Tooltip
from bokeh.models.dom import HTML as BokehHTML
from cubevis.bokeh.transport import CommMgr
from cubevis import exe

from .axes import Axis
from .selection import SelectionSpec
from .visibility_raster import VisibilityRaster
from .visibility_scatter import VisibilityScatter, ScatterLayer, _LAYER_CMAPS
from . import palettes as _palettes
from .refresh import RefreshLevel as _RefreshLevel
from .flag_db import FlagDB
from .iteration_step import STEP_INDEX_JS
from .reduction_context import (
    FlagDelta,
    NullReductionContext,
    ObservationMetadata,
    ReductionBackend,
    ReductionContext,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

_PANEL_WIDTH_SIDE  = 500    # each panel in side-by-side mode
_PANEL_WIDTH_FULL  = 1020   # single-panel or over/under mode
_PANEL_HEIGHT      = 550
_PANEL_HEIGHT_OVER = 280    # each panel height in over/under mode
_SECTION_DARK      = "#cdd6f4"
_SECTION_LIGHT     = "#1e293b"
"""Sidebar section-heading colours.

Previously hardcoded inline at construction with nothing able to change
them, so the headings stayed dark-blue on a light sidebar and read as
disabled.  Named so the restyle callback can swap them.
"""

_DARK_TABLE_CSS = """
:host { --bk-table-bg: #1e1e2e; --bk-table-fg: #cdd6f4;
        --bk-table-hdr: #313244; --bk-table-line: #45475a;
        --bk-table-sel: #313244; }
.slick-header-columns, .slick-header-column {
    background: var(--bk-table-hdr) !important;
    color: var(--bk-table-fg) !important;
    border-color: var(--bk-table-line) !important; }
.slick-viewport, .grid-canvas, .slick-row, .slick-cell {
    background: var(--bk-table-bg) !important;
    color: var(--bk-table-fg) !important;
    border-color: var(--bk-table-line) !important; }
.slick-row.odd .slick-cell { background: #181825 !important; }
.slick-row.active .slick-cell, .slick-row.selected .slick-cell {
    background: var(--bk-table-sel) !important; }
"""
"""DataTable is SlickGrid, so a generic widget stylesheet never reaches it.

Bokeh renders DataTable through SlickGrid's own DOM inside a shadow root,
and none of the `.bk-input`-style rules that theme a Select or TextInput
apply.  The table therefore stayed light-on-white in dark mode while
every other sidebar widget followed the theme.
"""

_LIGHT_TABLE_CSS = ""
"""Empty: SlickGrid's own defaults are already a light theme."""

_SIDEBAR_WIDTH     = 260
_SIDEBAR_WIDTH_COL = 268    # column width including padding

# Preset definitions: (raster_y, raster_x, raster_qty, scatter_x, scatter_y, layout)
_PRESETS = {
    "vplot": (
        Axis.BASELINE, Axis.TIME,    Axis.AMPLITUDE,
        Axis.TIME,     Axis.AMPLITUDE,
        "side",
    ),
    "radplot": (
        # Raster: Amplitude vs Baseline (x=TIME is native; UVDIST is scatter-only)
        # Scatter: Amplitude vs UVDIST (the defining radplot axis)
        Axis.BASELINE, Axis.TIME,    Axis.AMPLITUDE,
        Axis.UVDIST,   Axis.AMPLITUDE,
        "side",
    ),
    "waterfall": (
        Axis.TIME,     Axis.CHANNEL, Axis.AMPLITUDE,
        Axis.TIME,     Axis.AMPLITUDE,
        "over",
    ),
}


def _resolve_axis_arg(value, options, role_name, default):
    """Resolve a user-supplied axis= constructor string to an Axis enum
    member.

    ``options`` is one of ``_RASTER_AXIS_OPTIONS``/``_RASTER_QTY_OPTIONS``/
    ``_SCATTER_X_OPTIONS``/``_SCATTER_Y_OPTIONS`` -- the exact same list
    that populates the corresponding GUI dropdown, so this validates
    against a single source of truth rather than a separately-maintained
    list: anything invalid here would also be unselectable in the GUI,
    and anything added to a dropdown's options becomes constructor-
    selectable for free, no separate update needed.

    Returns ``default`` unchanged if ``value`` is None (no override
    requested) -- callers pass the preset-or-hardcoded-default value
    computed so far as ``default``, so precedence is: explicit
    argument > preset > hardcoded default.
    """
    if value is None:
        return default
    name = value.strip().upper()
    valid_names = [opt[0] for opt in options]
    if name not in valid_names:
        raise ValueError(
            f"{role_name}={value!r} is not a valid axis for this role. "
            f"Valid options: {', '.join(valid_names)}"
        )
    return Axis[name]


# Both raster axes share the same dimension vocabulary. Selecting the
# same dimension for both Y and X is rejected — see the conflict guard
# wired up in _build_sidebar() (client-side) and _handle_plot()
# (server-side).
_RASTER_AXIS_OPTIONS = [("TIME",        "Time"),
                        ("BASELINE",    "Baseline"),
                        ("CHANNEL",     "Channel"),
                        ("CORRELATION", "Correlation")]
_RASTER_Y_OPTIONS   = _RASTER_AXIS_OPTIONS
_RASTER_X_OPTIONS   = _RASTER_AXIS_OPTIONS
_RASTER_QTY_OPTIONS = [("AMPLITUDE", "Amplitude"),
                       ("PHASE",     "Phase"),
                       ("REAL",      "Real"),
                       ("IMAGINARY", "Imaginary"),
                       ("FLAG",      "Flag")]
_SCATTER_X_OPTIONS  = [("UVDIST",        "UV Distance"),
                       ("UVDIST_LAMBDA", "UV Distance (wavelengths)"),
                       ("TIME",          "Time"),
                       ("FREQUENCY",     "Frequency"),
                       ("CHANNEL",       "Channel"),
                       ("U",             "U"),
                       ("V",             "V")]
_SCATTER_Y_OPTIONS  = [("AMPLITUDE", "Amplitude"),
                       ("PHASE",     "Phase"),
                       ("REAL",      "Real"),
                       ("IMAGINARY", "Imaginary"),
                       ("U",         "U"),
                       ("V",         "V")]

# Dark-mode CSS applied to all sidebar input widgets via InlineStyleSheet.
# Overrides Bokeh's default light component styles so widgets blend with
# the #1e1e2e sidebar background.
_DARK_WIDGET_CSS = """
:host { --bokeh-base-font: system-ui, sans-serif; }
.bk-input {
    background:   #313244 !important;
    color:        #cdd6f4 !important;
    border-color: #45475a !important;
}
select.bk-input option {
    background: #313244;
    color:      #cdd6f4;
}
.bk-input-group label,
.bk-label,
label {
    color: #cdd6f4 !important;
}
.bk-btn {
    background: #313244 !important;
    color:      #cdd6f4 !important;
    border-color: #45475a !important;
}
.bk-btn:hover {
    background: #45475a !important;
}
"""

# Light-mode counterpart -- previously existed ONLY as inline JS text
# inside the dark_btn toggle's CustomJS code (never promoted to a
# Python constant the way _DARK_WIDGET_CSS was), the exact kind of
# duplication that let _LIGHT_TABS_CSS go missing entirely for a while
# (see that constant's own history). Values match what was already
# working in the inline JS version; only the formatting changed, to
# match _DARK_WIDGET_CSS's style.
_LIGHT_WIDGET_CSS = """
:host { }
.bk-input {
    background:   #ffffff !important;
    color:        #222222 !important;
    border-color: #aaa !important;
}
select.bk-input option {
    background: #fff;
    color:      #222;
}
.bk-input-group label,
.bk-label,
label {
    color: #222222 !important;
}
.bk-btn {
    background: #f0f0f0 !important;
    color:      #222 !important;
    border-color: #aaa !important;
}
"""

# Compact Prev/Next iteration buttons (I-1, Phase 2.5 — sidebar redesign).
# Theme-*independent* on purpose, and deliberately a SEPARATE stylesheet
# from _DARK_WIDGET_CSS/_LIGHT_WIDGET_CSS rather than folded into them.
# The restyle callback's generic widgets loop does
# `w.stylesheets[0].css = widget_css` — it overwrites index 0 wholesale
# with whichever of the two constants above applies, on every toggle.
# Sizing rules living inside that same constant would therefore need
# duplicating into both _DARK_WIDGET_CSS and _LIGHT_WIDGET_CSS to
# survive a toggle at all — this way, sizing lives once, attached at
# stylesheets[1] (see _field_prev_btn / _spw_prev_btn's construction in
# _build_sidebar()), a slot the widgets loop never touches, while
# stylesheets[0] still gets the normal dark/light colour swap like every
# other themed sidebar widget. Same two-stylesheet shape as
# self._spw_table's [dark, table_css_dark] — that pairing exists for a
# different reason (SlickGrid needs its own sheet) but the mechanism is
# identical: index 0 is the generically-swapped one, index 1 is not.
_ICON_BTN_CSS = """
.bk-btn {
    padding:    0     !important;
    min-width:  22px  !important;
    min-height: 22px  !important;
    font-size:  11px  !important;
    line-height: 20px !important;
}
.bk-btn:disabled {
    opacity: 0.35     !important;
    cursor: not-allowed !important;
}
"""

# ---------------------------------------------------------------------------
# Stage 1c: gear tool icon + Tabs dark styling (added 2026-07-31)
# ---------------------------------------------------------------------------
#
# Hand-authored generic gear/cog glyph (circle hub + eight rotated tooth
# rectangles) rather than a path borrowed from any icon library, so there's
# no third-party icon licensing question. Rendered at a small fixed size —
# fine detail is unnecessary since it only needs to read as "settings" in a
# toolbar button.
_GEAR_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">
<g fill="#cdd6f4">
{teeth}
<circle cx="12" cy="12" r="7" />
</g>
<circle cx="12" cy="12" r="3" fill="#1e1e2e" />
</svg>""".format(teeth="\n".join(
    f'<rect x="10.5" y="0.5" width="3" height="5" '
    f'transform="rotate({angle} 12 12)" />'
    for angle in range(0, 360, 45)
))

_GEAR_ICON_DATA_URI = (
    "data:image/svg+xml;base64,"
    + base64.b64encode(_GEAR_ICON_SVG.encode("utf-8")).decode("ascii")
)

# Color a panel's figure title switches to while its gear/Tabs config is
# open (added 2026-07-31). Reuses the same red/pink accent this file
# already uses for the raster Y/X axis-conflict warning
# (raster_axis_conflict_msg's notify_div color) rather than introducing a
# new ad hoc "attention" color into the palette.
_EDIT_TITLE_COLOR = "#f38ba8"

# Bokeh's Tabs widget uses its own shadow-DOM CSS classes (not the
# .bk-input/.bk-btn ones _DARK_WIDGET_CSS targets), so it needs its own
# dark-theme rule set. Not yet visually verified against a live render —
# flagged for a look once this is tested against the real UI, same as
# everything else here.
_DARK_TABS_CSS = """
:host { --bokeh-base-font: system-ui, sans-serif; }
.bk-header {
    background:   #181825 !important;
    border-color: #45475a !important;
}
.bk-tab {
    color:      #a6adc8 !important;
    background: transparent !important;
}
.bk-tab.bk-active {
    color:        #cdd6f4 !important;
    border-color: #89b4fa !important;
}
.bk-tab:hover {
    color: #cdd6f4 !important;
}
"""

# Light-mode counterpart -- previously didn't exist at all, so the
# dark/light toggle had nothing to swap _gear_tabs' stylesheet to and
# the gear tab strip (e.g. "Panel A") stayed dark regardless of mode.
# Colors chosen to match the existing light widget CSS in the dark_btn
# toggle JS (#222222 text, #aaa/#ccc borders, light backgrounds) for
# consistency with the rest of the light theme.
_LIGHT_TABS_CSS = """
:host { }
.bk-header {
    background:   #f0f0f0 !important;
    border-color: #cccccc !important;
}
.bk-tab {
    color:      #555555 !important;
    background: transparent !important;
}
.bk-tab.bk-active {
    color:        #222222 !important;
    border-color: #4a90d9 !important;
}
.bk-tab:hover {
    color: #222222 !important;
}
"""


# ---------------------------------------------------------------------------
# MSSelection string parsers (preview-grade — full parser in Phase 2)
# ---------------------------------------------------------------------------

def _parse_spw_string(spw_str: str, meta: ObservationMetadata) -> list:
    """Resolve an ``spw=`` string to the identities the backend uses.

    Returns whatever ``_partition_spw_ident`` reports -- a numeric id
    where the store provides one, otherwise the spectral window *name*
    -- because that is what ``_spw_selected`` compares against.

    An earlier version kept only ``tok.isdigit()`` tokens and returned
    ints.  On an xarray-ms store, where windows are identified by name,
    that produced a list matching nothing, and SPW filtering became a
    silent no-op (see the handoff).

    Each token is matched, in order:

    1. exactly against a window's identity, stringified;
    2. exactly against its ``name``;
    3. as a case-insensitive substring of its name -- so
       ``spw='SW-01'`` picks ``ALMA_RB_07#BB_2#SW-01#FULL_RES`` without
       requiring the full ASDM name to be typed.  **Skipped for
       purely-numeric tokens**, which would otherwise match by
       coincidence (``"0"`` occurs in ``ALMA_RB_07#...``);
    4. for a purely numeric token only, against the *ordinal position*
       in ``meta.spws``.

    Step 4 is a deliberate last resort and is logged.  ``spw='0'`` is
    what a user reaching for CASA habits will type, and refusing it
    outright would be unhelpful -- but a position is **not** a CASA
    ``spw`` id, and on a store that reports names there is no way to
    recover the real one.  Saying so is better than either silently
    selecting the wrong window or silently selecting none.

    An unmatched token is logged and skipped.  Returning everything on a
    token that matched nothing would show *more* data than asked for
    without saying so.
    """
    all_ids = [s.spw_id for s in meta.spws]
    if not spw_str or spw_str.strip() == "":
        return all_ids

    result: list = []
    for tok in (t.strip() for t in spw_str.split(",")):
        if not tok:
            continue
        hit = None
        for s in meta.spws:
            if str(s.spw_id) == tok or (s.name and s.name == tok):
                hit = s.spw_id
                break
        if hit is None and not tok.isdigit():
            # Substring matching is skipped for purely-numeric tokens.
            # "0" occurs inside "ALMA_RB_07#BB_2#SW-01#FULL_RES", so a
            # bare digit would match a name by coincidence and silently
            # preempt the positional branch below -- picking a window the
            # user did not mean, with no warning, because the match
            # "succeeded".
            low = tok.lower()
            for s in meta.spws:
                if s.name and low in s.name.lower():
                    hit = s.spw_id
                    break
        if hit is None and tok.isdigit():
            pos = int(tok)
            if 0 <= pos < len(all_ids):
                hit = all_ids[pos]
                log.warning(
                    "spw=%r matched no spectral window id or name; using "
                    "position %d (%r).  This store does not expose CASA "
                    "spw numbers, so a positional match is a guess -- "
                    "name the window to be certain.", tok, pos, hit,
                )
        if hit is None:
            log.warning("spw=%r matched no spectral window; ignoring it "
                        "(available: %s)", tok,
                        ", ".join(str(s.spw_id) for s in meta.spws) or "none")
            continue
        if hit not in result:
            result.append(hit)

    return result or all_ids


def _parse_correlation_string(corr_str: str,
                               meta: ObservationMetadata) -> list[str]:
    if not corr_str or corr_str.strip() == "":
        if meta.spws:
            return list(meta.spws[0].polarizations)
        return ["XX", "YY"]
    return [c.strip().upper() for c in corr_str.split(",") if c.strip()]


def _parse_field_string(field_str: str,
                         meta: ObservationMetadata) -> Optional[str]:
    """Resolve a field= string to a field name.

    A purely-numeric string is matched against each field's real
    ``field_id`` (not a positional index into ``meta.fields`` -- that
    was the bug this replaced: silently wrong whenever source FIELD_IDs
    are non-contiguous, which they commonly are, e.g. after splitting
    out a subset of an MS's original fields. Confirmed on a real MS
    with FIELD_IDs 0, 2, 3, 5, 6 -- the old positional-index version
    resolved field='2' to the field at position 2 in an alphabetically-
    sorted name list, which was J0522-364, not the field whose real
    FIELD_ID is 2 (Ceres). ``meta.fields[i].field_id`` is now populated
    from an authoritative source where the backend provides one -- see
    ``ObservationMetadata.from_backend_metadata`` and
    ``MSv2Backend._field_id_map`` -- so a direct match against it gives
    the same answer plotms's field='N' MSSelection syntax would).

    Matches plotms's field= convention. Returns None (not a fallback
    guess) if no field has that FIELD_ID, so callers can tell "no such
    field" apart from a real match rather than silently degrading into
    matching the numeric string as a literal field name.
    """
    if not field_str or field_str.strip() == "":
        return None
    if field_str.strip().isdigit():
        fid = int(field_str.strip())
        for f in meta.fields:
            if f.field_id == fid:
                return f.name
        return None
    return field_str.strip() or None


# ---------------------------------------------------------------------------
# Iteration position helpers (I-1, Phase 2.5)
# ---------------------------------------------------------------------------
#
# Pure lookups, not stepping -- ``iteration_step.step_index`` computes
# *where Prev/Next should go next*; these compute *where the current
# selection already is*, for the status bar's "Field 3/7: 0637-752" /
# "SPW 2/4: 1" readout (visplot_duo_iteration_kickoff.md §1). Reusable
# regardless of whether the current position was reached via Prev/Next or
# a manual Select/DataTable pick -- no separate "was this an iteration
# step" state needs to be tracked anywhere.

def _field_iteration_position(field_str: str,
                              meta: ObservationMetadata) -> Optional[tuple]:
    """1-based ``(position, count)`` of the field ``field_str`` resolves
    to within ``meta.fields``, or ``None`` when nothing resolves to
    exactly one field.

    ``meta.fields`` is the same stable ordered tuple Prev/Next steps
    through client-side (see ``iteration_step.py`` and the "Animate:
    Field | SPW" toolbar controls in ``_build_toolbar``) -- excluding the
    Field ``Select``'s ``("", "All fields")`` sentinel entry, which is
    not itself a steppable position. Returns ``None`` for that sentinel
    (``field_str`` empty) and for a string that fails to resolve at all,
    both of which ``_parse_field_string`` already reports as ``None``.
    """
    name = _parse_field_string(field_str, meta)
    if name is None:
        return None
    names = [f.name for f in meta.fields]
    if name not in names:
        return None
    return names.index(name) + 1, len(names)


def _spw_iteration_position(spw_ids,
                            meta: ObservationMetadata) -> Optional[tuple]:
    """1-based ``(position, count)`` within ``meta.spws`` when *spw_ids*
    names exactly one spectral window, else ``None``.

    Unlike Field, SPW is a genuine multi-select (the sidebar's checkbox
    ``DataTable``) -- a manual multi-window pick is a normal, common
    state, not a sentinel, and correctly reports no position here (the
    existing ``self._spw_str or "all"`` fallback in ``_status_text()``
    covers it). Only Prev/Next -- which always narrows to exactly one row
    when stepping SPW, per the kickoff's §3 -- or a manual single-row
    pick produce a position.
    """
    if not spw_ids or len(spw_ids) != 1:
        return None
    ids = [s.spw_id for s in meta.spws]
    if spw_ids[0] not in ids:
        return None
    return ids.index(spw_ids[0]) + 1, len(ids)


# ---------------------------------------------------------------------------
# Backend probes and open_ms / open_ps factory functions
# ---------------------------------------------------------------------------

def _probe_casatasks() -> bool:
    try:
        import casatasks  # noqa: F401
        return True
    except ImportError:
        return False


def _probe_radps() -> bool:
    try:
        import radps  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        import astroviper  # noqa: F401
        return True
    except ImportError:
        return False


def _make_casa6_context(path: str) -> ReductionContext:
    raise NotImplementedError(
        "Casa6ReductionContext is not yet implemented. "
        "Pass backend='null' to suppress this error and use display-only mode."
    )


def _make_radps_context(path: str) -> ReductionContext:
    raise NotImplementedError(
        "RadpsReductionContext is not yet implemented. "
        "Pass backend='null' to suppress this error and use display-only mode."
    )


def _make_remote_context(path: str, endpoint: str) -> ReductionContext:
    raise NotImplementedError(
        f"RemoteReductionContext is not yet implemented (preview release). "
        f"endpoint={endpoint!r} path={path!r}"
    )


def _resolve_context_msv2(path, backend, remote_endpoint):
    if backend == ReductionBackend.NULL:
        return NullReductionContext()
    if backend == ReductionBackend.REMOTE:
        if not remote_endpoint:
            raise ValueError("backend='remote' requires remote_endpoint.")
        return _make_remote_context(path, remote_endpoint)
    if backend == ReductionBackend.CASA6:
        if not _probe_casatasks():
            raise RuntimeError("backend='casa6' requested but casatasks not importable.")
        return _make_casa6_context(path)
    if backend == ReductionBackend.RADPS:
        if not _probe_radps():
            raise RuntimeError("backend='radps' requested but RADPS not available.")
        return _make_radps_context(path)
    # AUTO
    if _probe_casatasks():
        try:
            return _make_casa6_context(path)
        except NotImplementedError:
            log.debug("open_ms (auto): Casa6ReductionContext not implemented; trying RADPS")
    if _probe_radps():
        try:
            return _make_radps_context(path)
        except NotImplementedError:
            log.debug("open_ms (auto): RadpsReductionContext not implemented; using Null")
    return NullReductionContext()


def _resolve_context_msv4(path, backend, remote_endpoint):
    if backend == ReductionBackend.CASA6:
        raise ValueError("backend='casa6' is not valid for MSv4/PS data.")
    if backend == ReductionBackend.NULL:
        return NullReductionContext()
    if backend == ReductionBackend.REMOTE:
        if not remote_endpoint:
            raise ValueError("backend='remote' requires remote_endpoint.")
        return _make_remote_context(path, remote_endpoint)
    if backend == ReductionBackend.RADPS:
        if not _probe_radps():
            raise RuntimeError("backend='radps' requested but RADPS not available.")
        return _make_radps_context(path)
    # AUTO
    if _probe_radps():
        try:
            return _make_radps_context(path)
        except NotImplementedError:
            log.debug("open_ps (auto): RadpsReductionContext not implemented; using Null")
    return NullReductionContext()


def open_ms(path, *, backend=ReductionBackend.AUTO, remote_endpoint=None):
    """Open an MSv2 measurement set; return (metadata, reader, context)."""
    from .local_visibility_reader import LocalVisibilityReader
    from .data.msv2_backend import MSv2Backend
    backend = ReductionBackend(backend)
    b = MSv2Backend(path)
    b.open()
    reader = LocalVisibilityReader(b)
    meta = ObservationMetadata.from_backend_metadata(
        reader.metadata(), source_path=path
    )
    context = _resolve_context_msv2(path, backend, remote_endpoint)
    log.debug("open_ms: fields=%d spws=%d context=%s",
              len(meta.fields), len(meta.spws), type(context).__name__)
    return meta, reader, context


def open_ps(path, *, backend=ReductionBackend.AUTO, remote_endpoint=None):
    """Open an MSv4 / Processing Set; return (metadata, reader, context)."""
    from .local_visibility_reader import LocalVisibilityReader
    from .data.msv4_backend import MSv4Backend
    backend = ReductionBackend(backend)
    b = MSv4Backend(path)
    b.open()
    reader = LocalVisibilityReader(b)
    meta = ObservationMetadata.from_backend_metadata(
        reader.metadata(), source_path=path
    )
    context = _resolve_context_msv4(path, backend, remote_endpoint)
    log.debug("open_ps: fields=%d spws=%d context=%s",
              len(meta.fields), len(meta.spws), type(context).__name__)
    return meta, reader, context


def _make_scatter_layers(
    y_axis: "Axis",
    polarizations: list[str],
    scaling_alpha: float = 50.0,
    cmaps: Optional[list] = None,
) -> list[ScatterLayer]:
    """Build one ``ScatterLayer`` per polarisation with assigned cmaps.

    ``VisibilityScatter.__init__`` assigns cmaps when ``lyr.cmap is None``,
    but ``update_axes`` with a fresh layer list does not.  This helper
    always assigns cmaps explicitly so the shade step never receives
    ``cmap=None`` regardless of which code path constructs the layers.

    *cmaps* is the theme-resolved family from ``palettes``; callers that
    have a plotter should pass ``self._scatter_ramps`` so the layers match
    the current theme.  ``None`` falls back to the module constant, which
    is the dark-theme family -- correct for the default theme and merely
    suboptimal otherwise, rather than a failure.
    """
    cmaps = list(cmaps or _LAYER_CMAPS)
    return [
        ScatterLayer(
            y_axis        = y_axis,
            polarization  = pol,
            cmap          = cmaps[i % len(cmaps)],
            scaling_alpha = scaling_alpha,
        )
        for i, pol in enumerate(polarizations)
    ]


# ---------------------------------------------------------------------------
# _PanelSlot — Stage 1b.5 (added 2026-07-31)
# ---------------------------------------------------------------------------

@dataclass
class _PanelSlot:
    """One configurable panel slot: a fixed identity holding both a raster
    and a scatter object, plus which of the two is currently active.

    Replaces Stage 1b's six separate named attributes
    (``self._slot_a_raster``/``_slot_a_scatter``/``_slot_b_raster``/
    ``_slot_b_scatter`` plus ``self._slot_a_kind``/``_slot_b_kind``) with a
    small record held in an ordered ``self._slots`` list, so code that needs
    "however many slots currently exist" can iterate rather than reference
    fixed attribute names. See decision 9's "Stage 1b.5" note in
    ``visplot-grid-iteration-notes.md`` for the full rationale — this is a
    pure data-structure refactor with no behavior change from Stage 1b.

    Attributes
    ----------
    id : str
        Slot identifier, e.g. ``"A"`` / ``"B"``. Mode-A-only for now — grid
        mode's per-cell identifiers are a different (row/col) shape and are
        not assumed to reuse this class; see the scope note in decision 9.
    kind : str
        ``"raster"`` or ``"scatter"`` — which of ``raster``/``scatter`` is
        currently the slot's active/visible object.
    raster : VisibilityRaster
        Always constructed, per decisions 5/9/11 (structural cost paid
        regardless of whether this slot is currently showing raster).
    scatter : VisibilityScatter
        Same as ``raster`` above, for the scatter kind.
    """

    id: str
    kind: str
    raster: "VisibilityRaster"
    scatter: "VisibilityScatter"

    @property
    def active(self):
        """Whichever of ``raster``/``scatter`` matches ``self.kind``."""
        return self.raster if self.kind == "raster" else self.scatter


def _iter_guard_js(count_var: str, axis_label: str) -> str:
    """JS snippet: if ``count_var`` <= 1, write a "nothing to iterate"
    message to ``notify_div`` and return -- shared verbatim by every
    ``doIterateX()`` function body (see ``_IterButtons`` below for why
    this can't just live inside that class).

    Backstop, not the primary guard, for every axis this is used on:
    ``_IterButtons`` already disables the buttons outright when count
    is <=1 at construction time (see its own docstring), so in normal
    use this branch is only reachable via some other path setting the
    widget's value/selection directly. Kept anyway on the same belt-
    and-suspenders principle this file already applies elsewhere (e.g.
    the raster Y/X conflict guard) -- the cost of checking is one
    comparison, and the alternative is a disabled-looking control that
    can still silently do nothing if something else does manage to
    trigger it.

    Parameters
    ----------
    count_var : str
        The JS variable/expression holding the item count, already in
        scope at the point this snippet is spliced in (e.g. ``"names.length"``
        or ``"idents.length"``).
    axis_label : str
        Singular noun for the message, e.g. ``"field"``, ``"spectral window"``
        -- pluralized with a trailing "s", which covers every axis this
        project has or has planned (I-2 Polarization, I-3 Antenna/Baseline/
        Scan/Time); revisit if a future axis's plural is irregular.
    """
    return f"""
    if ({count_var} <= 1) {{
        if (notify_div)
            notify_div.text = {count_var} === 0
                ? "<b>No {axis_label}s to iterate.</b>"
                : "<b>Only one {axis_label} in this dataset — nothing to iterate.</b>";
        return;
    }}"""


@dataclass
class _IterButtons:
    """One axis's Prev/Next button pair, plus the row it shares with
    that axis's own control -- sized, themed, tooltipped, and aligned
    uniformly across every iteration axis (I-1, Phase 2.5).

    Why this class exists
    ----------------------
    Field's and SPW's Prev/Next controls were each fixed, by hand, in
    three separate rounds of live-MS-testing feedback: oversized
    buttons and stray whitespace (Tip is its own ``LayoutDOM`` with
    independent default sizing -- unset margins on both the Button
    *and* its Tip wrapper, not the CSS, was the actual cause); light/
    dark theming (a themed sidebar Select needs its color stylesheet
    swapped on toggle the way a toolbar Button never did, since the
    toolbar itself is never restyled); horizontal misalignment between
    the two axes' button pairs (independently-chosen label widths, 194
    vs 200px, that only looked equal); and vertical crowding against
    the next sidebar section (a wrapper column's margin zeroed for one
    reason -- fixing the row's *internal* alignment -- silently zeroed
    a different thing, the gap to whatever comes *after* it, too).

    Every one of those was a Python/Bokeh-layout concern with no real
    per-axis content -- the actual stepping logic (what "current index"
    means for a ``Select`` vs. a ``DataTable``'s row selection, and
    likely something else again for a future free-text ``EvTextInput``
    axis) is genuinely different per widget type and stays out of this
    class, written directly in ``_build_toolbar()`` per axis. Folding
    that in too would trade three real, already-debugged fixes for one
    speculative one, guessed at ahead of I-2/I-3 actually needing it.

    Two-phase construction, matching this file's existing split between
    ``_build_sidebar()`` (widgets exist) and ``_build_toolbar()``
    (``self._do_plot_js``/``_plot_js_args`` exist): build the instance
    in ``_build_sidebar()`` -- buttons and the row are ready and can be
    placed immediately -- then call ``.wire()`` from ``_build_toolbar()``
    once there's a ``doPlot()`` to call into.

    Attributes
    ----------
    axis_label : str
        Singular noun used in tooltips and the disabled-state/"nothing
        to iterate" message, e.g. ``"field"``, ``"spectral window"``.
    control : UIElement
        What sits to the left of the buttons: either the axis's own
        single-line widget (Field's title-less ``Select``) or a heading
        ``Div`` beside a control that lives on its own row underneath
        (SPW's ``DataTable``). Either way, its width must already equal
        ``LABEL_WIDTH`` and its margin must already be ``(0,0,0,0)`` --
        this class positions the buttons beside it, not the control
        itself, the same division of responsibility ``_section()``
        already has from the widgets it labels.
    count : int
        Number of steppable items. ``<= 1`` disables both buttons
        outright (not just the click-time guard in ``_iter_guard_js``)
        and switches both tooltips to explain why, rather than leaving
        a control that looks enabled but silently does nothing.
    dark : InlineStyleSheet
        The sidebar's shared color stylesheet (from ``self._dark()``) --
        stylesheets[0] on both buttons, following the light/dark toggle
        exactly like every other themed sidebar widget (see
        ``_ICON_BTN_CSS``'s own comment for why sizing lives in a
        second, untouched slot instead).
    icon_btn_css : InlineStyleSheet
        ``self._icon_btn_css`` -- stylesheets[1] on both buttons.
    tt : Callable[[str], Tooltip]
        ``self._tt`` -- building each button's ``Tip`` tooltip needs the
        same ``Tooltip``-construction helper every other toolbar/sidebar
        tooltip in this file already uses.
    vertical_nudge : int
        Extra top margin (px) on both buttons, beyond the shared
        ``align="center"`` the row already applies. 0 for a ``Div``-
        backed control (SPW) -- a plain ``Div`` centers predictably
        against a ``Button`` at the same declared height. Field's
        ``Select`` needed 2: a ``<select>`` element's own intrinsic box
        metrics center a couple of pixels differently than a ``<div>``
        does even at an identical declared height, and that difference
        has to be supplied per control type -- there's no way to derive
        it structurally, only to name it once here instead of
        rediscovering it by hand for the next ``Select``-backed axis
        (Antenna/Scan/Time, I-3, all currently ``EvTextInput`` with a
        similar-enough box model to likely need the same nudge, but
        this is exactly the kind of thing worth actually checking
        against a real browser rather than assuming carries over).

    Not attributes, class-level instead, deliberately: ``BTN_DIMS`` and
    ``LABEL_WIDTH`` are shared across *all* axes, computed once here
    rather than passed in or recomputed per call site the way the first
    two rounds did (194 vs 200px was exactly that recomputation
    silently drifting) -- callers reference ``_IterButtons.LABEL_WIDTH``
    directly when sizing their own control/heading before constructing
    one of these.
    """

    axis_label:     str
    control:        "UIElement"
    count:          int
    dark:           InlineStyleSheet
    icon_btn_css:   InlineStyleSheet
    tt:             "Callable[[str], Tooltip]"
    vertical_nudge: int = 0

    BTN_DIMS: ClassVar[dict] = {"width": 24, "height": 24}
    # Two 24px buttons plus the 6px+2px gaps used between them and their
    # neighbour below -- _SIDEBAR_WIDTH minus exactly that footprint.
    LABEL_WIDTH: ClassVar[int] = _SIDEBAR_WIDTH - (2 * 24 + 6 + 2)

    def __post_init__(self):
        enabled = self.count > 1
        if enabled:
            prev_tip = f"Previous {self.axis_label} (wraps at the ends)"
            next_tip = f"Next {self.axis_label} (wraps at the ends)"
        else:
            why = (f"This dataset has only {self.count} "
                   f"{self.axis_label}{'s' if self.count != 1 else ''} "
                   "— nothing to iterate")
            prev_tip = next_tip = why

        self.prev_btn = Button(
            label="◀", button_type="default",
            stylesheets=[self.dark, self.icon_btn_css],
            disabled=not enabled,
            margin=(0, 0, 0, 0), **self.BTN_DIMS,
        )
        self.next_btn = Button(
            label="▶", button_type="default",
            stylesheets=[self.dark, self.icon_btn_css],
            disabled=not enabled,
            margin=(0, 0, 0, 0), **self.BTN_DIMS,
        )
        self.row = row(
            self.control,
            Tip(self.prev_btn, tooltip=self.tt(prev_tip),
                margin=(self.vertical_nudge, 0, 0, 6), **self.BTN_DIMS),
            Tip(self.next_btn, tooltip=self.tt(next_tip),
                margin=(self.vertical_nudge, 0, 0, 2), **self.BTN_DIMS),
            align="center", margin=(0, 0, 0, 0),
        )

    def wire(self, plot_js_args: dict, do_plot_js: str, step_js: str,
             fn_name: str) -> None:
        """Attach the Prev/Next click handlers. Call once, from
        ``_build_toolbar()``, after ``self._do_plot_js``/``_plot_js_args``
        exist -- see this class's own docstring for why construction and
        wiring are split across two methods/two builder passes at all.

        Parameters
        ----------
        plot_js_args : dict
            ``self._plot_js_args``, or that dict extended with whatever
            extra widgets *this axis's* stepping logic needs beyond the
            standard set (Field/SPW need nothing extra -- both read/
            write widgets already in ``_plot_js_args``).
        do_plot_js : str
            ``self._do_plot_js`` -- the shared ``doPlot()`` definition
            every Prev/Next reuses rather than duplicating the send
            logic again (kickoff §3).
        step_js : str
            The axis-specific ``function doIterate<Axis>(delta) {...}``
            body, e.g. built with ``STEP_INDEX_JS + "..." + _iter_guard_js(...) + "..."``.
            Not generated here -- see the class docstring for why the
            actual stepping logic per widget type stays out of this
            class and is written directly at each call site.
        fn_name : str
            The function name ``step_js`` defines, e.g.
            ``"doIterateField"`` -- called with ``-1``/``+1`` to build
            each button's full ``CustomJS`` code.
        """
        self.prev_btn.js_on_click(CustomJS(
            args=plot_js_args,
            code=step_js + do_plot_js + f"{fn_name}(-1);\ndoPlot(false);",
        ))
        self.next_btn.js_on_click(CustomJS(
            args=plot_js_args,
            code=step_js + do_plot_js + f"{fn_name}(1);\ndoPlot(false);",
        ))


# ---------------------------------------------------------------------------
# VisibilityPlotter
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Theme restyle — one JS body, two callers
# ---------------------------------------------------------------------------
#
# Extracted from the Light/Dark toggle's CustomJS so the same code can
# run at document load.  The toggle restyles on the "active" *change*
# event, and a change event does not fire at load -- so a constructor
# theme="light" built dark chrome that nothing ever restyled, while the
# palettes were resolved light.  That mismatch costs ~2.5x contrast (see
# palettes.py) and rendered the scatter nearly black.
#
# The body expects a boolean ``light`` already in scope and refers to the
# toggle nowhere, so the two callers differ only in how they obtain it:
#
#   toggle  : const light = cb_obj.active;   (+ label update after)
#   startup : const light = true;            (baked in by Python)
#
# One copy matters more than usual here: the body covers figures, info
# divs, sidebar, widgets, hint divs, path_div's regenerated HTML,
# colormap histograms, icons and the gear tab strip -- and every one of
# those was added after a *reported* light-mode gap.  A second copy would
# silently miss the next one.
_THEME_RESTYLE_JS = """
// Each block below is wrapped by _step() so that one failure cannot
// silently abort the rest.  This body has grown by accretion -- figures,
// info divs, sidebar, widgets, hint divs, path_div, colormap histograms,
// icons, gear tabs, section headings, the SPW table -- and every addition
// runs in the same try-less sequence.  A single TypeError in the middle
// (`Bokeh.InlineStyleSheet is not a constructor`, 2026-08-19) stopped the
// theme changing ANYWHERE, including the toggle's own label, which made
// a one-line mistake look like the whole feature had broken.
function _step(name, fn) {
    try { fn(); }
    catch (e) { console.error('[visplot theme] ' + name + ' failed:', e); }
}

// --- sidebar section headings ---------------------------------------
// Their colour was baked into the Div's inline style at construction, so
// nothing could change it and they stayed dark-blue on a light sidebar.
_step('section headings', () => {
    if (typeof section_divs === 'undefined' || !section_divs) return;
    const _sc = light ? section_light : section_dark;
    for (const d of section_divs) {
        if (!d || !d.text) continue;
        d.text = d.text.replace(/color:#[0-9a-fA-F]{3,8}/, 'color:' + _sc);
    }
});

// --- SPW DataTable ---------------------------------------------------
// DataTable renders through SlickGrid inside a shadow root, so the
// generic widget CSS above never reaches it: it needs its own sheet.
// Both sheets are constructed in Python and passed in; this only swaps
// which is attached.  Building one here would need
// `Bokeh.InlineStyleSheet`, which is not a constructor -- and the
// TypeError aborted every block after this one, so the whole theme
// silently stopped changing.
_step('spw table', () => {
    if (typeof spw_table === 'undefined' || !spw_table) return;
    spw_table.stylesheets = [sidebar_css,
                             light ? table_css_light : table_css_dark];
});

const bg_fig    = light ? 'white'   : 'black';
const bg_border = light ? '#ffffff' : '#1e1e2e';
const label_c   = light ? '#222222' : '#cdd6f4';
const grid_c    = light ? '#cccccc' : '#45475a';
const page_bg   = light ? '#ffffff' : '#181825';
const info_bg   = light ? '#ffffff' : '#1e1e2e';
const info_c    = light ? '#222222' : '#cdd6f4';
const status_c  = light ? '#155724' : '#a6e3a1';
const title_c   = light ? '#222222' : '#cdd6f4';
// Two new colors for the light-mode fixes above: hint_c stays visually
// distinct from status_c in light mode too (dark teal vs. dark green),
// matching the existing dark-mode intent ("cyan — distinct from status
// green") rather than just reusing status_c and losing that distinction.
// source_c is exactly label_c's value, reused under its own name since
// it's conceptually "make the source path readable, matching the rest
// of the sidebar's text" rather than tied to the same one label_c
// happens to serve elsewhere.
const hint_c    = light ? '#0c5460' : '#89dceb';
const source_c  = light ? '#222222' : '#a6e3a1';
// dark_css/light_css/dark_tabs_css/light_tabs_css are passed in via
// args (see the CustomJS args dict above) rather than defined here --
// single source of truth in _DARK_WIDGET_CSS/_LIGHT_WIDGET_CSS/
// _DARK_TABS_CSS/_LIGHT_TABS_CSS.
const widget_css = light ? light_css : dark_css;
const tabs_css = light ? light_tabs_css : dark_tabs_css;

_step('page background', () => {
    // Page background
    for (const el of [document.body, document.documentElement]) {
        try { el.style.background = page_bg; } catch(e) { console.error('[visplot theme] sidebar container failed:', e); }
    }
    for (const sel of ['.bk-root', '[data-root-id]', '.bk']) {
        try {
            document.querySelectorAll(sel).forEach(
                el => el.style.background = page_bg
            );
        } catch(e) { console.error('[visplot theme] sidebar container failed:', e); }
    }
});
_step('figures', () => {
    // Figures (all four panel objects — see args comment above)
    for (const fig of figs) {
        fig.background_fill_color = bg_fig;
        fig.border_fill_color     = bg_border;
        if (fig.title) fig.title.text_color = title_c;
        for (const ax of [...fig.below, ...fig.left, ...fig.right, ...fig.above]) {
            if (ax.axis_label_text_color  !== undefined) ax.axis_label_text_color  = label_c;
            if (ax.major_label_text_color !== undefined) ax.major_label_text_color = label_c;
            if (ax.axis_line_color        !== undefined) ax.axis_line_color        = label_c;
            if (ax.major_tick_line_color  !== undefined) ax.major_tick_line_color  = label_c;
            if (ax.minor_tick_line_color  !== undefined) ax.minor_tick_line_color  = label_c;
        }
        for (const g of fig.center) {
            if (g.grid_line_color !== undefined) g.grid_line_color = grid_c;
        }
    }
});
_step('info divs', () => {
    // Info divs and status bar
    function recolor_div(div, bg, fg) {
        try {
            const s = Object.assign({}, div.styles);
            s['background'] = bg;
            s['color']      = fg;
            div.styles = s;
        } catch(e) { console.error('[visplot theme] sidebar container failed:', e); }
    }
    for (const info_div of info_divs) {
        recolor_div(info_div, info_bg, info_c);
    }
    recolor_div(status_div, page_bg, status_c);
    recolor_div(notify_div, page_bg, light ? '#b02a37' : '#f38ba8');

    // Config-field hint divs (added 2026-08-03, fixing a reported gap) —
    // same page_bg as status_div, distinct text color from status_c to
    // preserve the dark-mode "cyan — distinct from status green" intent.
    for (const hint_div of hint_divs) {
        recolor_div(hint_div, page_bg, hint_c);
    }
});
_step('source path', () => {
    // Source path (added 2026-08-03, fixing a reported gap) — the color was
    // baked into an inline HTML <span style=...> inside .text itself, which
    // no property-based recolor mechanism can reach; rebuilt here instead of
    // restyled. label_c already correctly tracks light/dark for the
    // "Source:" label; source_c (== label_c's value, see definition above)
    // does the same for the path itself, replacing the old permanently-green
    // value per the specific request that light mode read black, not green.
    path_div.text = "<b style='color:" + label_c + "'>Source:</b> " +
        "<span style='font-family:monospace;font-size:11px;color:" +
        source_c + "'>" + source_basename + "</span>";
});
// Sidebar container background
try {
    const s = Object.assign({}, sidebar.styles);
    s['background']   = light ? '#f8f8f0' : '#1e1e2e';
    s['border-right'] = light ? '1px solid #ccc' : '1px solid #45475a';
    sidebar.styles = s;
} catch(e) { console.error('[visplot theme] sidebar container failed:', e); }

// Sidebar widgets — update InlineStyleSheet CSS directly on each widget.
// This is the only reliable way since InlineStyleSheet overrides all other CSS.
try {
    for (const w of widgets) {
        if (w.stylesheets && w.stylesheets.length > 0) {
            w.stylesheets[0].css = widget_css;
        }
    }
} catch(e) { console.error('[visplot theme] sidebar widgets failed:', e); }

// Gear tab strip ("Panel A"/"Panel B") — previously had no light-mode
// CSS defined anywhere and was never touched by this callback at all,
// so it stayed dark regardless of mode (reported directly). Same
// stylesheets[0].css swap pattern as the widgets loop above, just with
// dedicated tab CSS since Bokeh's Tabs widget uses different shadow-DOM
// classes (.bk-header/.bk-tab) than .bk-input/.bk-btn.
try {
    if (tabs.stylesheets && tabs.stylesheets.length > 0) {
        tabs.stylesheets[0].css = tabs_css;
    }
} catch(e) { console.error('[visplot theme] gear tabs failed:', e); }

// Colormap histogram figures (added to fix a reported light-mode gap —
// see _style_cmap_column's docstring). Deliberately dimmer than the
// main panels' stark black/white in BOTH modes — light mode reuses the
// sidebar's own light background tone (#f8f8f0) rather than pure
// white — so the plotted distribution stays legible either way, per
// the original design intent for this figure.
const cmap_bg = light ? '#f8f8f0' : '#1e1e2e';
try {
    for (const fig of cmap_figs) {
        fig.background_fill_color = cmap_bg;
        fig.border_fill_color     = cmap_bg;
        if (fig.outline_line_color !== undefined) fig.outline_line_color = grid_c;
        for (const ax of [...fig.below, ...fig.left, ...fig.right, ...fig.above]) {
            if (ax.axis_line_color        !== undefined) ax.axis_line_color        = label_c;
            if (ax.major_tick_line_color  !== undefined) ax.major_tick_line_color  = label_c;
            if (ax.minor_tick_line_color  !== undefined) ax.minor_tick_line_color  = label_c;
            if (ax.major_label_text_color !== undefined) ax.major_label_text_color = label_c;
        }
        for (const g of fig.center) {
            if (g.grid_line_color !== undefined) g.grid_line_color = grid_c;
        }
    }
} catch(e) { console.warn('colormap figure recolor failed:', e); }

// Reset button icons — BuiltinIcon.color isn't reachable via
// stylesheets at all, has to be set directly.
try {
    for (const icon of cmap_icons) {
        if (icon.color !== undefined) icon.color = label_c;
    }
} catch(e) { console.error('[visplot theme] reset icons failed:', e); }
"""


class VisibilityPlotter:
    """Combined raster + scatter visibility inspection and flagging tool.

    Parameters
    ----------
    ms : str | None
        Path to an MSv2 measurement set.  Exactly one of ``ms`` or ``ps``
        must be supplied.
    ps : str | None
        Path to an MSv4 / Processing Set Zarr store.
    backend : str
        Reduction backend: ``"auto"``, ``"casa6"``, ``"radps"``,
        ``"remote"``, or ``"null"``.  Default ``"auto"``.
    remote_endpoint : str | None
        Required only when ``backend="remote"``.
    field : str
        Field name or integer index string.  Default: first field.
    spw : str
        Comma-separated SPW indices (``"0,1,2,3"``).  Default: all.
    antenna : str
        MSSelection antenna string.  (Stored; not yet wired in preview.)
    scan : str
        MSSelection scan string.  (Stored; not yet wired.)
    timerange : str
        MSSelection time-range string.  (Stored; not wired.)
    uvrange : str
        UV range string.  (Stored; not wired.)
    correlation : str
        Comma-separated correlation labels (``"XX,YY"``).  Default: all.
    datacolumn : str
        Visibility column: ``"data"``, ``"corrected"``, or ``"model"``.
    layout : str
        Panel layout: ``"one"`` (single panel, raster by default in this
        preview — per-panel kind switching is a later addition),
        ``"side"`` (both panels, side by side), or ``"over"`` (both
        panels, one above the other). Default ``"side"``.
    preset : str | None
        Named preset: ``"vplot"``, ``"radplot"``, ``"waterfall"``, or ``None``.
    raster_y, raster_x : str | None
        Explicit raster Y/X axis, e.g. ``"TIME"``, ``"BASELINE"``,
        ``"CHANNEL"``, ``"CORRELATION"``. Takes precedence over
        ``preset``, which takes precedence over the default
        (Time vs. Channel). Validated against the same options the
        raster axis dropdowns expose in the GUI — an invalid value
        raises ``ValueError`` listing the valid options.
    raster_qty : str | None
        Explicit raster quantity (color axis), e.g. ``"AMPLITUDE"``,
        ``"PHASE"``, ``"REAL"``, ``"IMAGINARY"``, ``"FLAG"``. Same
        precedence and validation as ``raster_y``/``raster_x``.
    scatter_x, scatter_y : str | None
        Explicit scatter X/Y axis, e.g. ``"UVDIST"``, ``"TIME"``,
        ``"FREQUENCY"``, ``"CHANNEL"``, ``"UVDIST_LAMBDA"``, ``"U"``,
        ``"V"`` for X; ``"AMPLITUDE"``, ``"PHASE"``, ``"REAL"``,
        ``"IMAGINARY"``, ``"U"``, ``"V"`` for Y. Same precedence and
        validation. E.g. ``scatter_x="U", scatter_y="V"`` launches
        directly into a U-vs-V UV-coverage scatter plot with no manual
        axis-picker step needed.
    time_range : tuple[float, float] | list[float] | None
        ``(start, end)`` as MJD floats.
    freq_range : tuple[float, float] | list[float] | None
        ``(start, end)`` in Hz.
    uvdist_range : tuple[float, float] | list[float] | None
        ``(min, max)`` in metres.
    compact_toolbar : bool
        Whether each figure's toolbar auto-hides until the mouse is over
        that plot.  Defaults to ``True``.
    """

    def __init__(
        self,
        *,
        ms:               Optional[str] = None,
        ps:               Optional[str] = None,
        backend:          str           = "auto",
        remote_endpoint:  Optional[str] = None,
        field:            str           = "",
        spw:              str           = "",
        antenna:          str           = "",
        scan:             str           = "",
        timerange:        str           = "",
        uvrange:          str           = "",
        correlation:      str           = "",
        datacolumn:       str           = "data",
        layout:           str           = "side",
        preset:           Optional[str] = None,
        raster_y:         Optional[str] = None,
        raster_x:         Optional[str] = None,
        raster_qty:       Optional[str] = None,
        scatter_x:        Optional[str] = None,
        scatter_y:        Optional[str] = None,
        time_range:       tuple[float, float] | list[float] | None = None,
        freq_range:       tuple[float, float] | list[float] | None = None,
        uvdist_range:     tuple[float, float] | list[float] | None = None,
        enable_flagging:  bool           = True,
        compact_toolbar:  bool           = True,
        plot_width:       Optional[int]  = None,
        plot_height:      Optional[int]  = None,
        theme:            str            = "dark",
        raster_cmap:      Optional[str]  = None,
        scatter_cmap:     Optional[str]  = None,
        headless:         bool           = False,
    ) -> None:
        """Construct the plotter.

        Split into three phases so a headless export can stop after the
        first two.  ``headless=True`` resolves configuration and opens the
        data, builds only the panels it will actually export, and returns
        without any browser chrome -- no ``CommMgr``, no control pipe, no
        figure styling, no layout.

        See ``_resolve_config``, ``_build_panels`` and ``_build_gui``.
        """
        self._headless = bool(headless)

        self._resolve_config(
            ms=ms, ps=ps, backend=backend, remote_endpoint=remote_endpoint,
            field=field, spw=spw, antenna=antenna, scan=scan,
            timerange=timerange, uvrange=uvrange, correlation=correlation,
            datacolumn=datacolumn, layout=layout, preset=preset,
            raster_y=raster_y, raster_x=raster_x, raster_qty=raster_qty,
            scatter_x=scatter_x, scatter_y=scatter_y,
            time_range=time_range, freq_range=freq_range,
            uvdist_range=uvdist_range, enable_flagging=enable_flagging,
            compact_toolbar=compact_toolbar,
            theme=theme, raster_cmap=raster_cmap, scatter_cmap=scatter_cmap,
            plot_width=plot_width, plot_height=plot_height,
        )

        # ------------------------------------------------------------------ #
        # FlagDB + hotkey scope                                                #
        # ------------------------------------------------------------------ #
        self._flag_db         = FlagDB()
        self._hotkey_scope_id = str(uuid4())

        if self._headless:
            # No comm layer: VisibilityPlot already treats comm_mgr=None
            # as "no browser", so the panels are simply built without one.
            self._comm_mgr = None
            self._build_panels()
            return

        self._build_comm()
        self._build_panels()
        self._style_panel_figures()
        self._build_gui()

    def _resolve_config(
        self,
        *,
        ms, ps, backend, remote_endpoint, field, spw, antenna, scan,
        timerange, uvrange, correlation, datacolumn, layout, preset,
        raster_y, raster_x, raster_qty, scatter_x, scatter_y,
        time_range, freq_range, uvdist_range, enable_flagging,
        compact_toolbar, theme, raster_cmap, scatter_cmap,
        plot_width, plot_height,
    ) -> None:
        """Phase 1: validate, store arguments, open the data, resolve axes.

        Everything here runs in every mode -- it is what a headless export
        needs and nothing more.  Opens the MS/PS, builds the initial
        ``SelectionSpec``, and resolves the preset/explicit axis choices
        into ``_raster_x/_raster_y/_raster_qty/_scatter_x/_scatter_y``.

        No Bokeh models are created and no browser state is assumed.
        """
        # ------------------------------------------------------------------ #
        # Validate                                                             #
        # ------------------------------------------------------------------ #
        if ms is not None and ps is not None:
            raise ValueError("Supply exactly one of ms= or ps=, not both.")
        if ms is None and ps is None:
            raise ValueError("One of ms= or ps= must be supplied.")

        # ------------------------------------------------------------------ #
        # Store arguments                                                      #
        # ------------------------------------------------------------------ #
        self._ms_path       = ms
        self._ps_path       = ps
        self._source_path   = ms or ps
        self._field_str     = field
        self._spw_str       = spw
        # Identities chosen in the SPW table, or None to fall back to
        # parsing _spw_str.  Set by the Plot handler.
        self._spw_ids       = None
        self._antenna_str   = antenna
        self._scan_str      = scan
        self._timerange_str = timerange
        self._uvrange_str   = uvrange
        self._corr_str      = correlation
        self._datacolumn    = datacolumn.upper()
        self._layout        = layout.lower()
        self._preset        = preset.lower() if preset else None
        self._time_range    = time_range
        self._freq_range    = freq_range
        self._uvdist_range  = uvdist_range
        self._enable_flagging = enable_flagging
        self._compact_toolbar = compact_toolbar

        # --- theme and palettes -------------------------------------- #
        #
        # A palette is a *render-time* choice: it is applied before
        # tf.shade(), so it is baked into the pixels and cannot be
        # changed by the export's chrome theme.  See palettes.py.
        #
        # Sticky override, per role: `theme` supplies the default, and
        # once the user picks a palette by hand the theme stops driving
        # THAT role for the session while the other keeps tracking.  An
        # explicit constructor argument counts as user-set from the
        # start -- otherwise the first Light/Dark toggle would silently
        # discard it.
        # Per-panel canvas size in pixels.  This is the Datashader canvas
        # as well as the Bokeh figure size, so it sets the aggregation
        # resolution, not just the display scale -- see png_export's
        # `cell_size` note.  Defaults preserve the previous hardcoded
        # constants.
        self._plot_width  = int(plot_width or _PANEL_WIDTH_SIDE)
        self._plot_height = int(plot_height or _PANEL_HEIGHT)

        self._requested_theme = theme if theme in ("dark", "light") else "dark"
        self._theme = self._requested_theme

        # A light-themed GUI launch applies the same restyle the toggle
        # runs, once, at document load -- see _apply_startup_theme().  The
        # chrome and the palettes must agree: light-conditioned ramps on a
        # dark ground cost ~2.5x contrast (palettes.py) and render the
        # scatter nearly black, which is exactly what happened while this
        # was unwired.
        self._raster_cmap_name  = raster_cmap or _palettes.default_for(
            "raster", self._theme)
        self._scatter_cmap_name = scatter_cmap or _palettes.default_for(
            "scatter", self._theme)
        self._raster_cmap_user_set  = raster_cmap is not None
        self._scatter_cmap_user_set = scatter_cmap is not None

        if self._layout not in ("one", "side", "over"):
            raise ValueError(f"layout must be 'one', 'side', or 'over'; got {layout!r}")

        # ------------------------------------------------------------------ #
        # Open data source                                                     #
        # ------------------------------------------------------------------ #
        if ms is not None:
            self._meta, self._reader, self._context = open_ms(
                ms, backend=backend, remote_endpoint=remote_endpoint
            )
        else:
            self._meta, self._reader, self._context = open_ps(
                ps, backend=backend, remote_endpoint=remote_endpoint
            )

        self._selection = self._build_selection()
        # Group 3 piece 3, Chunk 2 bug fix (added 2026-08-02): per-slot,
        # not a single shared attribute. Was self._last_raster_selection
        # (one value for the whole app) — correct only as long as at most
        # one slot could ever be raster, which Chunk 2 finally allows to
        # not be true. With two slots both raster, processing slot A
        # first (in _handle_plot()'s per-slot loop) would set this shared
        # tracker to match self._selection; by the time slot B's own
        # axes-changed check ran in the *same call*, the "did selection
        # change" comparison read as false — even though slot B's raster
        # object had never had its range data sent to the client at
        # all — so no x0/x1/y0/y1/title got sent for it, and the browser
        # crashed trying to set a Bokeh Range1d.start to null. Keyed by
        # slot.id so each slot's own render history is tracked
        # independently. Starts empty — self._slots doesn't exist yet at
        # this point in __init__ — and an absent key is exactly the
        # right default anyway: .get(slot.id) returning None compares as
        # "changed" against any real selection, correctly forcing a first
        # render.
        self._last_raster_selection_by_slot: dict = {}

        # Recompute-cost fix (added 2026-08-02): scatter previously had no
        # change-detection at all — it re-rendered unconditionally on
        # every single _handle_plot() call, regardless of whether its
        # axes/selection actually changed (a pre-existing asymmetry,
        # documented but not fixed when Chunk 1 first touched this code).
        # Two scatter panels can now exist simultaneously, doubling the
        # wasted recompute on every Plot press — directly the kind of
        # cost this whole rework started from concern about. Mirrors
        # self._last_raster_selection_by_slot exactly: per-slot, starts
        # empty for the same __init__-ordering reason (self._slots
        # doesn't exist yet here), populated below once it does.
        self._last_scatter_selection_by_slot: dict = {}

        # ------------------------------------------------------------------ #
        # Preset / explicit axes                                               #
        # ------------------------------------------------------------------ #
        # NOTE: uses _resolved_* local names throughout, not raster_y/
        # raster_x/etc. directly -- those names now belong to the
        # incoming raster_y=/raster_x=/etc. constructor parameters
        # (added so a test or script can launch straight into a given
        # axis configuration, e.g. scatter_x='U', scatter_y='V', without
        # a manual GUI step -- see visplot-testing-handoff/reference
        # test 01 for the motivating case). Reusing those names for the
        # resolved Axis values would silently clobber the incoming
        # arguments before they're ever read.
        _resolved_raster_y   = Axis.TIME
        _resolved_raster_x   = Axis.CHANNEL
        _resolved_raster_qty = Axis.AMPLITUDE
        _resolved_scatter_x  = Axis.UVDIST
        _resolved_scatter_y  = Axis.AMPLITUDE

        if self._preset and self._preset in _PRESETS:
            ry, rx, rq, sx, sy, pl = _PRESETS[self._preset]
            _resolved_raster_y, _resolved_raster_x, _resolved_raster_qty = ry, rx, rq
            _resolved_scatter_x, _resolved_scatter_y                    = sx, sy
            self._layout                                                = pl

        # Explicit raster_y=/raster_x=/etc. arguments take precedence
        # over preset (which takes precedence over the hardcoded
        # default above). Validated against the same per-role OPTIONS
        # lists that drive the GUI dropdowns -- _RASTER_AXIS_OPTIONS/
        # _RASTER_QTY_OPTIONS/_SCATTER_X_OPTIONS/_SCATTER_Y_OPTIONS are
        # the single source of truth for "what's valid here" either
        # way, so this can never accept something the GUI itself
        # wouldn't, and anything added to the GUI's options becomes
        # usable here for free.
        _resolved_raster_y   = _resolve_axis_arg(raster_y,   _RASTER_AXIS_OPTIONS, "raster_y",   _resolved_raster_y)
        _resolved_raster_x   = _resolve_axis_arg(raster_x,   _RASTER_AXIS_OPTIONS, "raster_x",   _resolved_raster_x)
        _resolved_raster_qty = _resolve_axis_arg(raster_qty, _RASTER_QTY_OPTIONS,  "raster_qty", _resolved_raster_qty)
        _resolved_scatter_x  = _resolve_axis_arg(scatter_x,  _SCATTER_X_OPTIONS,   "scatter_x",  _resolved_scatter_x)
        _resolved_scatter_y  = _resolve_axis_arg(scatter_y,  _SCATTER_Y_OPTIONS,   "scatter_y",  _resolved_scatter_y)

        self._raster_y   = _resolved_raster_y
        self._raster_x   = _resolved_raster_x
        self._raster_qty = _resolved_raster_qty
        self._scatter_x  = _resolved_scatter_x
        self._scatter_y  = _resolved_scatter_y

    def _build_comm(self) -> None:
        """Phase 2a (GUI only): comm layer and reconnection handlers.

        Creates the ``BokehAppContext``/``CommMgr``, opens the control
        pipe, and registers the plot/done handlers.  Skipped entirely
        when headless -- there is no browser to talk to, and
        ``VisibilityPlot`` accepts ``comm_mgr=None``.
        """
        # ------------------------------------------------------------------ #
        # Communication infrastructure                                         #
        # ------------------------------------------------------------------ #
        def _shutdown_handler(reason, description):
            self._stop()
            BokehInit.clear_app_context(self._app_context)

        def _connection_closed_handler(reason, description):
            #
            # A connection ended but the session is still alive -- the laptop
            # slept, the network blipped, or the browser tab was reloaded.
            #
            # Do NOT call self._stop() here. It resolves _result_future, which
            # exits the `async with websockets.serve( ... )` block in
            # _task_server() and closes the listening socket that the frontend
            # is about to reconnect to.
            #
            log.debug(
                "VisibilityPlotter: connection lost (%s); awaiting reconnection",
                description,
            )

        def _reconnect_handler(generation):
            log.debug(
                "VisibilityPlotter: frontend reconnected (generation=%d)", generation
            )

        self._app_context = BokehAppContext(
            comm_mgr  = CommMgr(on_shutdown=_shutdown_handler),
            app_state = {
                "name":        "VisibilityPlotter",
                "initialized": True,
                "source_path": self._source_path,
                "layout":      self._layout,
            },
            title = f"VisibilityPlotter — {os.path.basename(self._source_path)}",
        )
        self._comm_mgr = self._app_context.comm_mgr

        # ------------------------------------------------------------------ #
        # Reconnection behaviour                                              #
        # ------------------------------------------------------------------ #
        # These fire for transient disconnects and are distinct from
        # on_shutdown, which ends the session for good.
        self._comm_mgr.set_connection_closed_callback(_connection_closed_handler)
        self._comm_mgr.set_reconnect_callback(_reconnect_handler)

        # Requests that were in flight when a connection dropped are replayed
        # once the frontend returns. All p2j traffic here is idempotent GUI
        # state (raster/scatter payloads, widget values), so redelivery is
        # safe. Set to False if that ever stops being true.
        self._comm_mgr.resend_inflight_on_reconnect = True

        # Wait indefinitely for the frontend to come back. Set to a number of
        # seconds to have the session shut itself down after an outage of that
        # length -- useful if a closed browser tab should not leave the Python
        # side running forever.
        self._comm_mgr.reconnect_timeout = None

        self._result_future = None

        self._pipe = {"control": None}
        self._pipe["control"] = self._comm_mgr.open(
            squash_queue=True,
            description="visibility plotter control",
        )

        # Message IDs — must be created before registering handlers
        self._ids = {
            "plot": str(uuid4()),
            "done": str(uuid4()),
            "theme": str(uuid4()),
            "export": str(uuid4()),
        }
        self._pipe["control"].register(self._ids["plot"], self._handle_plot)
        self._pipe["control"].register(self._ids["done"], self._handle_done)
        self._pipe["control"].register(self._ids["theme"], self._handle_theme)
        self._pipe["control"].register(self._ids["export"], self._handle_export)

    def _build_panels(self) -> None:
        """Phase 2b: cursor/order sources, panel objects, slot bookkeeping.

        Runs in every mode.  The two ``ColumnDataSource``s live here
        rather than with the comm layer because the panels need them and
        they are plain data models -- no browser required.

        Panels are constructed with ``headless=self._headless``, which
        makes each one build its Datashader substrate (``_render``,
        ``_state_source``) and skip its figure and tools.

        Palettes are resolved here rather than left to the plot classes'
        module constants: the ramp depends on the theme, and only the
        plotter knows it.  ``palettes.condition()`` also trims each ramp
        away from the theme background so the sparse end does not vanish
        into it -- see ``palettes.py``.

        "Render only what is needed" needs no special casing here: the
        existing ``defer_initial_render=(_slot_X_kind != "...")`` already
        means the inactive kind of each slot never queries the backend.
        Combined with ``headless``, an inactive panel builds neither a
        figure nor an aggregation, so all four objects still exist -- and
        ``_all_panels`` keeps its fixed length and positional meaning --
        while only the two exported panels cost anything.
        """
        raster_ramp        = _palettes.raster_cmap(self._raster_cmap_name,
                                                   self._theme)
        scatter_ramps      = _palettes.scatter_cmaps(self._scatter_cmap_name,
                                                     self._theme)
        # Kept so paths that rebuild layers later -- update_axes, the
        # polarisation checkboxes -- produce layers in the same palette
        # as the ones built here.
        self._raster_ramp  = raster_ramp
        self._scatter_ramps = scatter_ramps

        # ------------------------------------------------------------------ #
        # Construct display widgets                                            #
        # ------------------------------------------------------------------ #
        pols = self._selection.correlation or ["XX"]
        first_pol = pols[0]

        # Shared cursor ColumnDataSource for linked cursor across figures.
        # Created before widgets and passed via cursor_source= so the hover
        # CustomJS has the reference at _build() time.
        import math
        self._cursor_source = ColumnDataSource(
            data={"x": [math.nan], "y": [math.nan], "fig": [""]}
        )

        # Shared display-order tracker (added 2026-08-03, swap feature).
        # order[0] is a list of self._slots indices, in current screen-
        # position order — starts [0, 1], matching self._slot_display_order
        # (which stays fixed; this is the *live*, client-side-mutable
        # counterpart used by the actual swap trigger and by layout_js's
        # "One" mode sizing, which needs to know which slot is currently
        # primary). A ColumnDataSource, not a plain list, for the same
        # reason orig_source is one: needs to be read and written by
        # multiple independently-serialized CustomJS callbacks (both
        # slots' swap buttons, layout_js) sharing one live reference.
        # Deliberately a reorderable *list*, not a boolean — duo mode only
        # ever needs a 2-element swap today, but this is the same
        # structure a future N-panel "move to" scheme reuses directly,
        # rather than something to redesign later.
        self._display_order_source = ColumnDataSource(data={"order": [[0, 1]]})

        # Use stretch_width so figures fill available space responsively.
        # Fixed pixel widths are not set — Bokeh's CSS flexbox handles sizing.
        #
        # ---- Stage 1b: each slot pre-builds BOTH kinds ---------------------
        # Slot A defaults to raster, slot B defaults to scatter — preserves
        # today's exact default appearance and behavior. Only each slot's
        # default-active kind is actually rendered at construction (decision
        # 11); the inactive kind is a real, comm-registered Bokeh object
        # (structural cost paid regardless, per decision 9) that stays an
        # empty shell until first activated via _activate_slot_kind() below.
        # Nothing calls that method yet — gear/Tabs (a later stage) is what
        # will eventually wire a UI trigger to it — so for now this is
        # inert, testable infrastructure, not a behavior change.
        #
        # ---- Stage 1b.5: object construction order/kwargs are unchanged
        # from Stage 1b (still slot A raster, slot A scatter, slot B
        # scatter, slot B raster) — only the storage after construction
        # changes, from four named attributes to two _PanelSlot records in
        # self._slots. See decision 9's "Stage 1b.5" note in
        # visplot-grid-iteration-notes.md.
        _slot_a_kind = "raster"
        _slot_b_kind = "scatter"

        _slot_a_raster = VisibilityRaster(
            backend       = self._reader,
            selection     = self._selection,
            y_dim         = self._raster_y,
            x_dim         = self._raster_x,
            quantity      = self._raster_qty,
            polarization  = first_pol,
            width         = self._plot_width,
            height        = self._plot_height,
            comm_mgr      = self._comm_mgr,
            cursor_source = self._cursor_source,
            enable_flagging = self._enable_flagging,
            compact_toolbar = self._compact_toolbar,
            defer_initial_render = (_slot_a_kind != "raster"),
            headless             = self._headless,
            cmap                 = raster_ramp,
        )
        # One scatter layer per polarisation — multi-layer compositing
        # naturally boosts density and visibility vs a single layer. Slot
        # A's scatter has no user-configured axes yet (nothing can set them
        # until gear/Tabs exists), so it mirrors slot B's scatter defaults
        # as a sensible starting point rather than an arbitrary one.
        _slot_a_scatter = VisibilityScatter(
            backend       = self._reader,
            selection     = self._selection,
            x_axis        = self._scatter_x,
            layers        = _make_scatter_layers(
                self._scatter_y, pols, cmaps=scatter_ramps),
            width         = self._plot_width,
            height        = self._plot_height,
            comm_mgr      = self._comm_mgr,
            cursor_source = self._cursor_source,
            enable_flagging = self._enable_flagging,
            compact_toolbar = self._compact_toolbar,
            defer_initial_render = (_slot_a_kind != "scatter"),
            headless             = self._headless,
            layer_cmaps          = scatter_ramps,
        )

        _slot_b_scatter = VisibilityScatter(
            backend       = self._reader,
            selection     = self._selection,
            x_axis        = self._scatter_x,
            layers        = _make_scatter_layers(
                self._scatter_y, pols, cmaps=scatter_ramps),
            width         = self._plot_width,
            height        = self._plot_height,
            comm_mgr      = self._comm_mgr,
            cursor_source = self._cursor_source,
            enable_flagging = self._enable_flagging,
            compact_toolbar = self._compact_toolbar,
            defer_initial_render = (_slot_b_kind != "scatter"),
            headless             = self._headless,
            layer_cmaps          = scatter_ramps,
        )
        # Slot B's raster mirrors slot A's raster defaults — same reasoning
        # as slot A's scatter above.
        _slot_b_raster = VisibilityRaster(
            backend       = self._reader,
            selection     = self._selection,
            y_dim         = self._raster_y,
            x_dim         = self._raster_x,
            quantity      = self._raster_qty,
            polarization  = first_pol,
            width         = self._plot_width,
            height        = self._plot_height,
            comm_mgr      = self._comm_mgr,
            cursor_source = self._cursor_source,
            enable_flagging = self._enable_flagging,
            compact_toolbar = self._compact_toolbar,
            defer_initial_render = (_slot_b_kind != "raster"),
            headless             = self._headless,
            cmap                 = raster_ramp,
        )

        # Stage 1b.5: ordered slot records — self._slots[0] is "A",
        # self._slots[1] is "B". Order matches today's A/B layout
        # convention; nothing currently depends on list order beyond that.
        self._slots: list[_PanelSlot] = [
            _PanelSlot(id="A", kind=_slot_a_kind,
                       raster=_slot_a_raster, scatter=_slot_a_scatter),
            _PanelSlot(id="B", kind=_slot_b_kind,
                       raster=_slot_b_raster, scatter=_slot_b_scatter),
        ]

        # Now that self._slots exists, populate the per-slot selection
        # trackers declared earlier (had to start empty — self._slots
        # didn't exist yet at that point in __init__). Populating them
        # here with the initial selection, for every slot, avoids an
        # unnecessary first re-render if the user presses Plot without
        # having changed anything — for raster this matches what
        # self._last_raster_selection = self._selection used to do at
        # construction, just keyed per slot now instead of shared; for
        # scatter this is new (it previously had no change-detection to
        # preserve the behavior of), but the same reasoning applies.
        for _slot in self._slots:
            self._last_raster_selection_by_slot[_slot.id]  = self._selection
            self._last_scatter_selection_by_slot[_slot.id] = self._selection

        # Screen-position order, added 2026-07-31 alongside the Group 1/2
        # slot-indexed rework: self._slot_display_order[0] is whichever
        # self._slots index is currently drawn first/left/top,
        # [1] is second/right/bottom. Kept separate from slot *identity*
        # (self._slots' own order, self._panel_title_state/_tabpanels keyed
        # by slot.id) on purpose — a future zero-recompute "swap Panel A
        # and Panel B's screen positions" action (discussed, not yet
        # built) only needs to reverse this list; it doesn't touch slot
        # identity, kind, or any rendered content, since both panels'
        # RGBA data is already cached on their respective slot regardless
        # of where either is currently drawn. Currently always [0, 1];
        # nothing reorders it yet.
        self._slot_display_order: list[int] = [0, 1]

        # All four panel objects (both kinds, both slots) — added 2026-07-31
        # as part of the Group 1/2 rework. Previously several setup steps
        # below only touched self._raster/self._scatter (whichever two
        # objects happen to be kind-active right now, i.e. today always
        # slot A's raster + slot B's scatter) and silently left the other
        # two panel objects (slot A's scatter, slot B's raster — both
        # already constructed per decision 9/11, just not yet displayed)
        # without this setup applied at all. That was a latent bug, not
        # just an inconsistency: once a future Kind selector activates one
        # of those two, it would appear with Bokeh's raw default
        # sizing/toolbar/theme and *no* select-callback wired for
        # box-flagging, rather than picking up the same setup every other
        # panel already has. Fixed by applying setup uniformly to all four
        # here, at construction time, regardless of which is currently
        # displayed.
        # Stamp the resolved theme onto each panel.  PanelSpec.theme is
        # read from here (VisibilityPlot._theme_hint), and it is what
        # export_png defaults its chrome to -- so without this every
        # panel reports the class default "dark" and a light-themed
        # headless export draws light-conditioned ramps on a dark
        # ground.  The docstring claimed the plotter set this; it did
        # not, and nothing failed loudly when it was missing.
        for _p in [obj for slot in self._slots
                   for obj in (slot.raster, slot.scatter)]:
            _p._theme = self._theme

        self._all_panels = [obj for slot in self._slots
                            for obj in (slot.raster, slot.scatter)]

    def _style_panel_figures(self) -> None:
        """Phase 2c (GUI only): sizing mode and theme on each panel figure.

        Dereferences ``panel.figure``, which is ``None`` for a headless
        panel -- so this is gated rather than merely relocated.
        """
        # Use stretch_width so figures expand to fill available space when
        # the sidebar is collapsed or layout changes. Both .figure and
        # .layout (the wrapper column: figure + info div) need this — the
        # latter is applied here too (2026-07-31) rather than only in
        # _build_plot_area(), so it's already correct on all four panels,
        # not just whichever two are positioned on screen at any moment.
        for _panel in self._all_panels:
            _panel.figure.sizing_mode = "stretch_width"
            _panel.layout.sizing_mode = "stretch_width"

        # Toolbar sits on the right of each figure, providing all standard
        # tools (pan, wheel zoom, box zoom, reset, save, etc.) with correct
        # visual feedback. With compact_toolbar=True (default) it auto-hides
        # until the mouse is over that plot (bokeh.models.Toolbar.autohide) —
        # position and hide-behavior are independent Bokeh settings, not in
        # tension with each other. Tool synchronisation (below) is a
        # separate, position-based concern — see _pos0/_pos1.
        for _panel in self._all_panels:
            _panel.figure.toolbar_location = "right"

        # Apply dark mode styling at construction time so figures match the
        # dark sidebar/status bar without the user needing to press the
        # button — for all four, so an initially-inactive panel doesn't
        # appear in Bokeh's raw default theme the moment it's later shown.
        for _panel in self._all_panels:
            fig = _panel.figure
            fig.background_fill_color = "black"
            fig.border_fill_color     = "#1e1e2e"
            if fig.title:
                fig.title.text_color  = "#cdd6f4"
            _lc = "#cdd6f4"
            _gc = "#45475a"
            for ax in (*fig.below, *fig.left, *fig.right, *fig.above):
                if hasattr(ax, "axis_label_text_color"):
                    ax.axis_label_text_color      = _lc
                    ax.major_label_text_color     = _lc
                    ax.axis_line_color            = _lc
                    ax.major_tick_line_color      = _lc
                    ax.minor_tick_line_color      = _lc
            for g in fig.center:
                if hasattr(g, "grid_line_color"):
                    g.grid_line_color = _gc

    def set_theme(self, theme: str) -> None:
        """Adopt *theme* and re-shade; never re-queries.

        A theme change is a ``SHADE`` (``refresh.py``): it selects
        different palettes, and a palette alters no rows, no aggregation
        and no extent.  The cached ``agg`` and layer DataFrames are
        enough.

        This replaces an earlier design that flipped the chrome, marked
        the plot stale and asked the user to press Plot.  That assumed
        the intermediate state was merely suboptimal -- it is not.  A
        theme change inverts the ramp/background relationship and costs
        about 2.5x contrast (``palettes.py``), so the plot is
        *unreadable* until re-shaded, and asking someone to press a
        button to make an illegible plot legible is not a state to leave
        them in.

        Sticky override still holds: a role the user has set by hand
        keeps its palette and is not re-resolved.
        """
        from .refresh import RefreshLevel

        theme = theme if theme in ("dark", "light") else "dark"
        if theme == self._theme:
            return
        self._theme = theme

        changed = False
        if not self._raster_cmap_user_set:
            self._raster_cmap_name = _palettes.default_for("raster", theme)
            changed = True
        if not self._scatter_cmap_user_set:
            self._scatter_cmap_name = _palettes.default_for("scatter", theme)
            changed = True
        # Even a user-set *name* resolves to different colours per theme,
        # because condition() trims against the background -- so the
        # ramps are recomputed either way.
        self._raster_ramp = _palettes.raster_cmap(self._raster_cmap_name,
                                                  theme)
        self._scatter_ramps = _palettes.scatter_cmaps(
            self._scatter_cmap_name, theme)

        for panel in self._all_panels:
            panel._theme = theme          # keeps PanelSpec.theme honest
            try:
                if hasattr(panel, "set_layer_cmaps"):
                    panel.set_layer_cmaps(self._scatter_ramps)
                elif hasattr(panel, "set_cmap"):
                    panel.set_cmap(self._raster_ramp)
            except Exception as exc:
                log.warning("theme re-shade failed for %s: %s",
                            type(panel).__name__, exc)
        log.debug("set_theme(%r): %s re-shade of %d panels (defaults %s)",
                  theme, RefreshLevel.SHADE.name, len(self._all_panels),
                  "re-resolved" if changed else "kept (user-set)")

    def _apply_startup_theme(self, layout) -> None:
        """Run the theme restyle once at document load, if starting light.

        The Light/Dark toggle restyles on its ``"active"`` change event,
        which cannot fire at load, so a ``theme="light"`` launch would
        otherwise build dark chrome and never restyle it.  This attaches
        the *same* ``_THEME_RESTYLE_JS`` body to the document-ready
        event with ``light`` baked in, so there is still only one copy of
        the styling code.

        Dark needs no startup pass: it is what the widgets are
        constructed with.

        Failure here is non-fatal and logged.  A document-ready hook is
        the one part of this that depends on how the surrounding app
        context emits its Document, so if it is unavailable the plot
        still renders -- in dark chrome, with a warning, which is the
        old behaviour rather than a broken one.
        """
        if self._theme != "light":
            return
        try:
            from bokeh.events import DocumentReady
            from bokeh.io import curdoc
            from bokeh.models import CustomJS

            cb = CustomJS(args=dict(self._theme_restyle_args),
                          code="const light = true;\n" + _THEME_RESTYLE_JS)
            curdoc().js_on_event(DocumentReady, cb)
        except Exception as exc:
            log.warning(
                "could not apply theme='light' at startup (%s); the GUI "
                "will open in dark chrome -- use the Light/Dark toggle. "
                "Palettes were resolved for light, so contrast will be "
                "poor until you do.", exc,
            )

    def _build_gui(self) -> None:
        """Phase 3 (GUI only): toolbar sync, flag callbacks, layout."""
        # ------------------------------------------------------------------ #
        # Synchronise Bokeh toolbars between the two *currently displayed*  #
        # figures. js_on_change on the toolbar model property fires when    #
        # the user activates a tool via the Bokeh UI.                      #
        #                                                                    #
        # Position-based (self._pos0/self._pos1), not all-four: syncing pan #
        # tool state only makes sense between the two figures a user can    #
        # actually see/interact with side by side right now — the other    #
        # two panel objects have no visible toolbar to sync in the first    #
        # place. Uses self._slot_display_order indirectly via _pos0/_pos1,  #
        # so this keeps tracking "whatever's currently adjacent on screen"  #
        # if a future swap changes which slot is drawn where. (Kind         #
        # switching, if/when built, would need this re-wired per switch —   #
        # out of scope here, same as the rest of Group 3.)                  #
        # ------------------------------------------------------------------ #
        _sync_drag_code = """
const t = cb_obj.active_drag;
if (!t) { other.active_drag = null; return; }
for (const dt of other.tools) {
    if (dt.type === t.type) { other.active_drag = dt; return; }
}
"""
        _sync_scroll_code = """
const t = cb_obj.active_scroll;
if (!t) { other.active_scroll = null; return; }
for (const dt of other.tools) {
    if (dt.type === t.type) { other.active_scroll = dt; return; }
}
"""
        pos0_tb = self._pos0.figure.toolbar
        pos1_tb = self._pos1.figure.toolbar

        pos0_tb.js_on_change("active_drag",
            CustomJS(args={"other": pos1_tb}, code=_sync_drag_code))
        pos1_tb.js_on_change("active_drag",
            CustomJS(args={"other": pos0_tb}, code=_sync_drag_code))
        pos0_tb.js_on_change("active_scroll",
            CustomJS(args={"other": pos1_tb}, code=_sync_scroll_code))
        pos1_tb.js_on_change("active_scroll",
            CustomJS(args={"other": pos0_tb}, code=_sync_scroll_code))

        # ------------------------------------------------------------------ #
        # Wire flag/unflag callbacks on all four panel objects (not just    #
        # the two currently kind-active ones — same all-four reasoning as   #
        # above). Registering this is harmless even when                    #
        # enable_flagging=False — with no FlagTool present on a figure's    #
        # toolbar, nothing in the browser ever sends _msg_select for it, so #
        # these closures simply never fire. Dispatch is by each object's    #
        # own actual kind (two objects get the raster callback, two get     #
        # the scatter callback) — kind-based, not slot- or position-based,  #
        # since _handle_box_select's raster/scatter interpretation is       #
        # inherently a kind concern. Note: _handle_box_select() itself      #
        # still reads self._raster_x/self._scatter_x (today's single        #
        # global axis state) to interpret coordinates — correct today only  #
        # because only the two currently-displayed figures can actually    #
        # fire a select event from the browser; becomes Group 3's problem   #
        # once per-slot axis state exists.                                  #
        # ------------------------------------------------------------------ #
        async def _raster_select(msg, context=None, self=self):
            return await self._handle_box_select(msg, panel="raster")

        async def _scatter_select(msg, context=None, self=self):
            return await self._handle_box_select(msg, panel="scatter")

        for slot in self._slots:
            slot.raster.register_select_callback(_raster_select)
            slot.scatter.register_select_callback(_scatter_select)

        # ------------------------------------------------------------------ #
        # Build layout                                                         #
        # ------------------------------------------------------------------ #
        inner_layout = self._build_layout()
        self._app_context.ui = inner_layout

        # Last, so every model the restyle touches already exists.
        self._apply_startup_theme(inner_layout)

    # ====================================================================== #
    # Stage 1b.5 — slot lookup + Stage 1b compatibility properties           #
    # (properties are still TEMPORARY, see docstrings)                      #
    # ====================================================================== #

    def _slot_by_id(self, slot_id: str) -> "_PanelSlot":
        """Look up a ``_PanelSlot`` by its id (``"A"`` / ``"B"``).

        Small helper introduced with the Stage 1b.5 refactor — centralizes
        the id→record lookup so callers don't index ``self._slots``
        positionally. Raises ``ValueError`` on an unknown id, same
        validation ``_activate_slot_kind()`` did directly before this
        refactor.
        """
        for slot in self._slots:
            if slot.id == slot_id:
                return slot
        raise ValueError(f"slot must be one of "
                          f"{[s.id for s in self._slots]!r}; got {slot_id!r}")

    @property
    def _pos0(self):
        """Whichever panel object is currently drawn first/left/top.

        Added 2026-07-31 (Group 1 rework) as the *position*-indexed
        counterpart to the *kind*-indexed ``self._raster``/``self._scatter``
        below — deliberately separate properties, not aliases, because
        kind and screen position are independent axes now that
        ``self._slot_display_order`` exists. Layout/display code (which
        figure is drawn where, toolbar sync between whatever's currently
        adjacent on screen, cursor-span sync) should read this; code that
        cares about "the raster config" specifically regardless of where
        it's drawn (``doPlot``'s JS args, ``_handle_plot()``) should keep
        using ``self._raster``/``self._scatter`` — that split hasn't been
        touched by this rework and still depends on Group 3 (per-slot axis
        controls, not yet designed).
        """
        return self._slots[self._slot_display_order[0]].active

    @property
    def _pos1(self):
        """Whichever panel object is currently drawn second/right/bottom.

        See ``self._pos0`` above for the position-vs-kind distinction.
        """
        return self._slots[self._slot_display_order[1]].active

    @property
    def _raster(self) -> "VisibilityRaster":
        """Whichever slot currently holds the raster kind.

        Temporary compatibility shim, unchanged in behavior from Stage 1b —
        only its storage moved (Stage 1b.5: sourced from ``self._slots``
        instead of named ``self._slot_a_raster``/``self._slot_b_raster``
        attributes). Correct as long as at most one slot is raster at a
        time — true for the entire session today, since nothing calls
        ``_activate_slot_kind()`` yet (gear/Tabs, a later stage, is what
        will eventually wire a UI trigger to it) — so slot A stays raster /
        slot B stays scatter throughout. This property exists so the ~60
        existing references elsewhere in this class (hover probes, flag
        tools, crosshair sync, ``doPlot``'s JS args, etc.) keep working
        completely unchanged during Stage 1b/1b.5.

        It does **not** solve the deeper problem: once kind-switching is
        actually wired up, both slots could independently hold the same
        kind (two rasters, two scatters) or neither could — "the raster
        panel" stops being well-defined. At that point every one of those
        ~60 references needs to become properly slot-indexed instead of
        kind-indexed; this shim only buys time until that stage, it isn't
        the fix for it. Falls back to the first slot's (deferred,
        unrendered) raster object if no slot is currently raster.
        """
        for slot in self._slots:
            if slot.kind == "raster":
                return slot.raster
        return self._slots[0].raster

    @property
    def _scatter(self) -> "VisibilityScatter":
        """Whichever slot currently holds the scatter kind.

        Same temporary-shim caveats as ``self._raster`` above — see that
        docstring. Falls back to the last slot's (deferred, unrendered)
        scatter object if no slot is currently scatter — matches Stage 1b's
        original fallback-to-slot-B behavior, since slot B is
        ``self._slots[-1]`` in today's two-slot ordering.
        """
        for slot in self._slots:
            if slot.kind == "scatter":
                return slot.scatter
        return self._slots[-1].scatter

    def _activate_slot_kind(self, slot: str, kind: str) -> None:
        """Switch which kind (raster/scatter) is active for a slot.

        Real, testable Python-level infrastructure — not yet wired to any
        UI trigger. Updates the slot's active-kind state and, if the
        target object has never been rendered (``defer_initial_render``
        left it as an empty shell — see decision 11), performs its first
        real render now, reusing the same ``update_axes()`` mechanism
        already used for axis changes rather than a new one.

        Parameters
        ----------
        slot : ``"A"`` | ``"B"``
        kind : ``"raster"`` | ``"scatter"``

        Notes
        -----
        Deliberately does **not** attempt to keep ``self._raster``/
        ``self._scatter`` well-defined in every resulting state (e.g. after
        switching slot B to raster while slot A is already raster, both
        slots are raster and ``self._scatter`` falls back to the last
        slot's scatter object, which is what a caller would see even though
        that's not really "the" scatter panel in any deep sense). That
        ambiguity is real but out of scope for Stage 1b/1b.5 — nothing
        calls this method from the browser yet, so it's only reachable from
        direct Python/test code until gear/Tabs and the Stage 1c
        slot-indexing rework land.

        Stage 1b.5 change: mutates the looked-up ``_PanelSlot.kind`` field
        (via ``self._slot_by_id()``) instead of ``setattr`` on a
        ``f"_slot_{slot.lower()}_kind"`` attribute name — same effect,
        sourced from the new record instead of a named attribute.
        """
        panel_slot = self._slot_by_id(slot)  # raises ValueError on bad id
        if kind not in ("raster", "scatter"):
            raise ValueError(f"kind must be 'raster' or 'scatter'; got {kind!r}")

        panel_slot.kind = kind

        panel = panel_slot.raster if kind == "raster" else panel_slot.scatter
        if kind == "raster":
            never_rendered = panel.agg is None
        else:
            never_rendered = all(df is None for df in panel._layer_dfs)

        if never_rendered:
            # Force-activate via the same mechanism update_axes() already
            # uses for real axis changes (decision 11's self._agg-is-None /
            # all-layer-dfs-None guard).
            if kind == "raster":
                panel.update_axes(
                    y_dim=panel._y_dim, x_dim=panel._x_dim,
                    quantity=panel._quantity, polarization=panel._polarization,
                )
            else:
                panel.update_axes(x_dim=panel._x_dim)

    # ====================================================================== #
    # Public entry point                                                       #
    # ====================================================================== #

    def show(self) -> "Showable":
        """Display the plotter in a Jupyter notebook cell."""
        from bokeh.io.state import curstate
        if not curstate().notebook:
            from bokeh.io import output_notebook
            output_notebook()

        app_id  = uuid4()
        context = exe.Context(exe.Mode.THREAD)
        app_context, task = self(context, app_id)

        future_holder = [None]

        def startup():
            future_holder[0] = context.execute(task, app_id)

        def get_future():
            if future_holder[0] is None:
                raise RuntimeError("VisibilityPlotter has not been launched yet")
            return future_holder[0]

        return Showable(
            app_context, startup, get_future,
            name="visibility-plotter-jpy",
        )

    # ====================================================================== #
    # cubevis application protocol                                             #
    # ====================================================================== #

    def __call__(self, exec_context=None, task_id=None, *,
                 plotfile=None, nrows=None, ncols=None,
                 theme=None, dpi=100, layout=None, **selection):
        """Do the thing — display in a GUI, or write a PNG.

        The two modes are disjoint, so one verb serves both: a plotter
        built with ``headless=True`` has no GUI to launch, and one built
        without it has no reason to be called for output.

        **GUI mode** (``headless=False``) — unchanged::

            app_context, task = plotter(exec_context, app_id)

        **Headless mode** (``headless=True``) — writes a PNG and returns
        its absolute path::

            path = plotter(plotfile="amp_vs_uvdist.png")

        Iteration is repeated invocation.  The plotter holds the opened
        MS, so each call reuses the open handle rather than reopening --
        for a 43-antenna sweep that is one open instead of 43::

            for spw in (0, 1, 2, 3):
                plotter(plotfile=f"amp_spw{spw}.png", spw=[spw])

        Parameters
        ----------
        plotfile:
            Output path.  Defaults to a timestamped name derived from the
            MS, in the working directory.
        nrows, ncols:
            Grid shape.  Defaults follow ``layout``/the constructor's
            layout: ``"one"`` is 1x1, ``"side"`` 1x2, ``"over"`` 2x1.
        theme:
            ``"dark"`` or ``"light"``.  Sets **both** the palettes and
            the chrome, re-shading from cache if it differs from the
            constructor's -- so the two cannot diverge.  ``None``
            (default) keeps the constructor's theme.

            The constructor's ``theme`` *is* honoured in headless mode:
            it chooses the colormaps, which are applied at shade time.
            This parameter overrides it per call.
        dpi:
            Scales the chrome; the data area stays at the panel's canvas
            size.  See ``png_export``.
        layout:
            Override the constructor's layout for this call only.
        **selection:
            Any ``SelectionSpec`` field (``spw``, ``field_names``,
            ``scan``, ``time_range``, ``freq_range``, ``correlation``,
            ``antenna_names``, ``baselines``, ``channel_range``,
            ``data_column``).  Applied for this call and every subsequent
            one, so a loop that narrows once stays narrowed -- pass the
            field again to widen it back.
        """
        if not self._headless:
            if exec_context is None:
                raise TypeError(
                    "VisibilityPlotter(...) in GUI mode requires an "
                    "execution context; pass headless=True to write a PNG "
                    "instead"
                )
            self._open_channels()
            return self._app_context, exe.Task(self._task_server)

        return self._export_png(
            plotfile=plotfile, nrows=nrows, ncols=ncols,
            theme=theme, dpi=dpi, layout=layout, selection=selection,
        )

    def _export_png(self, *, plotfile=None, nrows=None, ncols=None,
                    theme=None, dpi=100, layout=None, selection=None):
        """Render the current configuration to a PNG; return its path.

        Re-queries only when *selection* actually changes something --
        a QUERY-level refresh in the ``refresh.py`` sense.  Calling this
        repeatedly with no selection change re-uses the cached
        aggregations and costs only the shade.
        """
        from .png_export import export_png

        if selection:
            self._apply_selection_overrides(selection)

        if theme is not None and theme != self._theme:
            # One `theme` means one thing.  The constructor's `theme`
            # selects *palettes* -- baked into the pixels before
            # png_export ever sees them -- while export_png's selects
            # *chrome*.  Passing them separately is how you get
            # dark-conditioned ramps on a white ground: legible, but
            # fading toward the wrong background at the sparse end.
            #
            # So a call-time theme re-resolves the palettes too.  This is
            # a SHADE (refresh.py): no re-query, just a re-composite from
            # the cached aggregations.
            self.set_theme(theme)

        lay = (layout or self._layout).lower()
        if nrows is None or ncols is None:
            nrows, ncols = {"one": (1, 1), "side": (1, 2),
                            "over": (2, 1)}.get(lay, (1, 2))
        n_panels = nrows * ncols

        panels = [self._pos0, self._pos1][:n_panels]
        rendered = [p.render_result() for p in panels]

        path = str(plotfile or "").strip() or self._default_plotfile()
        out = export_png(
            rendered, path,
            nrows=nrows, ncols=ncols,
            footer=self._status_text(),
            theme=theme, dpi=dpi,
        )
        log.info("wrote %s", out)
        return out

    def _apply_selection_overrides(self, overrides: dict) -> None:
        """Update ``_selection`` in place and re-render the panels.

        A ``QUERY``-level change (``refresh.py``): new rows are needed,
        so this is the one part of a headless export that touches the
        backend.  Unknown field names raise rather than being ignored --
        a silently dropped ``spw=[2]`` would write a file that looks
        right and contains the wrong data, which is the worst outcome
        available here.
        """
        from dataclasses import fields as _fields
        valid = {f.name for f in _fields(self._selection)}
        unknown = set(overrides) - valid
        if unknown:
            raise TypeError(
                f"unknown selection field(s): {sorted(unknown)}; "
                f"valid fields are {sorted(valid)}"
            )
        for key, value in overrides.items():
            setattr(self._selection, key, value)
        for panel in self._all_panels:
            try:
                panel.apply_refresh(_RefreshLevel.QUERY)
            except Exception as exc:
                log.warning("re-render failed for %s: %s",
                            type(panel).__name__, exc)

    async def _task_server(self):
        self._result_future = asyncio.Future()
        if self._comm_mgr.address:
            #
            # The serve() context manager must outlive any individual
            # connection. process_messages() is the *per-connection* handler
            # and returns every time a socket drops; keeping the listener open
            # across those returns is what makes reconnection possible. Only
            # _stop() -- via _shutdown_handler -- may resolve _result_future.
            #
            # The keepalive parameters let the server notice a dead peer in
            # ~30s instead of waiting out TCP timeouts. The frontend runs its
            # own application-level __ping__/__pong__ heartbeat, because
            # browsers cannot send WebSocket ping frames from JavaScript.
            #
            async with websockets.serve(
                self._comm_mgr.process_messages,
                self._comm_mgr.address[0],
                self._comm_mgr.address[1],
                ping_interval = 20,
                ping_timeout  = 10,
                close_timeout = 5,
            ):
                await self._result_future
        else:
            await self._comm_mgr.process_messages()
        return self.result()

    def _stop(self, _=None):
        if self._result_future is not None and not self._result_future.done():
            self._result_future.set_result(None)

    def result(self):
        if self._result_future is None or not self._result_future.done():
            return None
        return self._result_future.result()

    def _open_channels(self):
        # Channels opened eagerly in __init__; this is a no-op guard.
        pass

    # ====================================================================== #
    # j2p handlers                                                             #
    # ====================================================================== #

    async def _handle_export(self, msg, context=None):
        """Export the current view to a PNG.

        The browser supplies what only it knows.  With no Bokeh server, a
        pan or zoom performed in the browser never reaches Python -- the
        figures' ``x_range``/``y_range`` are ``CustomJS``-mutated model
        properties, and those do not propagate back -- so a viewport
        obtained from ``self._pos0._x_range`` would be the *unzoomed*
        extent.  The same applies to the layout radio and the panel
        order.  Everything else (axes, selection, scaling, palettes,
        cached aggregations) Python already holds and must NOT be taken
        from the message.

        Re-shades from cache at the supplied viewport; never re-queries,
        so this is fast and shows exactly what is on screen rather than a
        fresh query that might differ.
        """
        import os
        from .png_export import export_png

        panels_msg = msg.get("panels") or []
        nrows = int(msg.get("nrows", 1) or 1)
        ncols = int(msg.get("ncols", len(panels_msg) or 1) or 1)
        path  = str(msg.get("path") or "").strip() or self._default_plotfile()

        rendered = []
        cell_size = None
        for entry in panels_msg:
            try:
                idx = int(entry.get("panel"))
                panel = self._all_panels[idx]
            except (TypeError, ValueError, IndexError):
                rendered.append(None)
                continue
            vp = entry.get("viewport")
            viewport = (tuple(float(v) for v in vp)
                        if vp and len(vp) == 4 else None)
            sz = entry.get("size")
            if cell_size is None and sz and len(sz) == 2:
                try:
                    w, h = int(sz[0]), int(sz[1])
                    if w > 0 and h > 0:
                        cell_size = (w, h)
                except (TypeError, ValueError):
                    pass
            try:
                rendered.append(panel.render_result(viewport))
            except Exception as exc:
                log.warning("render_result failed for panel %s: %s", idx, exc)
                rendered.append(None)

        if not any(r is not None for r in rendered):
            return {"status": "error", "message": "nothing to export"}

        try:
            # cell_size comes from the browser so the exported aspect
            # matches what is on screen.  It is NOT the canvas the image
            # was shaded at -- the layout resizes are client-side only --
            # so matplotlib scales the image into the box.  That trades
            # 1:1 pixels (tier 1, section 1) for matching the GUI, which
            # is the right trade for an export whose whole purpose is to
            # reproduce the view.  Pass plot_width/plot_height at
            # construction to get both.
            out = export_png(
                rendered, path,
                nrows=nrows, ncols=ncols,
                footer=self._status_text(),
                theme=self._theme,
                cell_size=cell_size,
            )
        except Exception as exc:
            log.warning("export_png failed: %s", exc)
            return {"status": "error", "message": str(exc)}

        log.info("exported %s", out)
        return {"status": "ok", "path": out,
                "name": os.path.basename(out)}

    def _default_plotfile(self) -> str:
        """Where an export lands when the user names no file.

        Server-side, beside the process's working directory.  There is no
        Bokeh server and therefore no save dialog, and in
        JupyterLab-over-SSH the Python process is on a different machine
        from the browser -- so the file cannot be written "where the user
        is".  The resolved absolute path is returned to the client and
        shown, which is the honest substitute.
        """
        import os
        import time
        stem = os.path.basename(str(self._source_path or "visplot")).rstrip("/")
        for ext in (".ms", ".zarr", ".ps"):
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
        return os.path.abspath(
            f"{stem}_{time.strftime('%Y%m%d-%H%M%S')}.png")

    async def _handle_theme(self, msg, context=None):
        """Adopt a theme change from the Light/Dark toggle and re-shade.

        The toggle restyles the chrome in JavaScript and tells Python
        here, because the *palettes* live on the Python side and a theme
        change makes the old ones illegible -- light ramps on a dark
        ground lose ~2.5x contrast (``palettes.py``), which is what the
        chrome-only toggle produced before this existed.

        This is a ``SHADE`` (``refresh.py``): no rows, aggregation or
        extent change, so it re-composites from cache and never touches
        the backend.  That is why it can run on a button click at all.
        """
        theme = str(msg.get("theme", "dark"))
        try:
            self.set_theme(theme)
        except Exception as exc:
            log.warning("set_theme(%r) failed: %s", theme, exc)
            return {"status": "error", "message": str(exc)}

        # The re-shaded images must be RETURNED, not just assigned.
        # There is no Bokeh server, so a Python-side
        # ``ColumnDataSource.data = ...`` never reaches the browser --
        # every other image-updating handler already works this way (see
        # VisibilityScatter._image_response and _handle_set_color_mode).
        # Without this the re-shade happened correctly and invisibly:
        # the raster appeared to update only because its own scaling
        # path pushes over the comm.
        return {
            "status": "ok",
            "theme":  self._theme,
            "images": self._panel_image_payloads(),
        }

    def _panel_image_payloads(self) -> list:
        """Current image + extent for each panel, for a j2p response.

        Ordered by ``_theme_img_panels`` -- the same index list the
        toolbar used when it captured the image sources -- so the JS
        side can zip the two together.  A panel whose source is missing
        contributes ``None`` and is skipped client-side rather than
        shifting everything after it.
        """
        out = []
        for i in getattr(self, "_theme_img_panels", []):
            panel = self._all_panels[i]
            src = getattr(panel, "_image_source", None)
            if src is None or not src.data.get("image"):
                out.append(None)
                continue
            d = src.data
            out.append({
                "image": d["image"][0],
                "x0": d["x"][0], "x1": d["x"][0] + d["dw"][0],
                "y0": d["y"][0], "y1": d["y"][0] + d["dh"][0],
            })
        return out

    async def _handle_plot(self, msg, context=None):
        # Validation-error auto-switch-to-tab (added 2026-08-03) — the
        # last piece of decision 9's originally-settled gear/Tabs design
        # never built. Every error return below now includes
        # "failed_slot": slot.id, tagging which slot actually caused the
        # failure so doPlot()'s response handler can switch focus to that
        # tab rather than just showing an error message somewhere the
        # user isn't necessarily looking. Checked before building this:
        # every current error path already runs inside a `for slot in
        # self._slots:` loop (the kind-mismatch guard, the per-slot
        # raster Y/X conflict check below, and both kind's `except
        # Exception` catches around update_axes()) — so `slot.id` was
        # already in scope at every site; no new plumbing needed, just
        # adding the field to responses that already existed.
        #
        # Found immediately during live testing (2026-08-03): the raster
        # Y/X conflict check below is normally never reached at all —
        # doPlot()'s own client-side guard already refuses to send a
        # conflicting request before ctrl.send() is even called (see that
        # guard's comment for the reasoning), calling switchToTab()
        # directly there instead. The check here remains a genuine
        # "server-side backstop" (its own existing description, still
        # accurate) for any caller that might reach _handle_plot() without
        # going through that client guard — failed_slot still gets tagged
        # correctly if that ever happens, it just isn't the path this
        # specific error takes in normal use.
        #
        # One category this can't help with regardless: a malformed
        # *global* selection value (field/spw/scan/antenna/time/UV range,
        # parsed by self._build_selection() below) isn't tied to any
        # single panel in the first place, so there's no tab to usefully
        # switch to — orthogonal to this feature, not a gap in it.
        #
        # Reload ↺ (as opposed to Plot ▶ or a preset) starts over — the
        # pending FlagDB is preview-scope, in-memory, never written
        # anywhere, so "reload" discarding it is the correct behaviour
        # rather than something to preserve across a fresh render.
        did_reload = bool(msg.get("reload", False))
        if did_reload:
            self._flag_db = FlagDB()
            log.debug("_handle_plot: reload requested — FlagDB cleared")

        if "field"       in msg: self._field_str   = msg["field"]
        if "spw_ids" in msg:
            # Identities straight from the table, no text round trip.
            # `_spw_str` is left alone so the constructor's spw= remains
            # the record of what was *asked for*, while these are what
            # was *chosen*; _build_selection prefers the latter.
            self._spw_ids = list(msg["spw_ids"])
        elif "spw" in msg:
            # Legacy string form (constructor, older clients).
            self._spw_str = msg["spw"]
            self._spw_ids = None
        if "correlation" in msg: self._corr_str     = msg["correlation"]
        if "datacolumn"  in msg: self._datacolumn   = msg["datacolumn"].upper()

        # Group 3 piece 3 / Chunk 1 (added 2026-07-31): request is now
        # per-slot (msg["panels"][slot.id] = {"kind": ..., ...}) instead
        # of flat raster_y/raster_x/scatter_x/scatter_y globals — see
        # decision 9's "Group 3 piece 3, Chunk 1" note in
        # visplot-grid-iteration-notes.md for the full design record.
        panels_msg = msg.get("panels", {})

        # ---- Chunk 2: perform an actual kind-switch when requested ----
        # Was Chunk 1's reject-on-mismatch guard; relaxed here to the
        # real behavior — the actual point of this whole rework (two
        # rasters or two scatters open at once). _activate_slot_kind()
        # (built back in Stage 1b, never had a caller until now) handles
        # the slot.kind flip and, if the target has genuinely never been
        # rendered, a first render using its own stored axis defaults.
        # This chunk's own axes-changed check further below then applies
        # THIS request's actual y/x/qty on top — meaning a slot being
        # activated for the first time *and* given different axes than
        # its stored defaults in the same request renders twice (once
        # here with stale defaults, once below with the real values).
        # Known, narrow, first-activation-only inefficiency — accepted
        # rather than complicating _activate_slot_kind()'s contract to
        # avoid a render that only ever happens once per panel's
        # lifetime; a repeated switch back to an already-rendered kind
        # correctly reuses the cached render with no recompute at all,
        # via the same axes-changed check.
        switched_kind_this_round = set()
        for slot in self._slots:
            panel_msg = panels_msg.get(slot.id)
            if panel_msg is None:
                continue
            requested_kind = panel_msg.get("kind")
            if requested_kind is not None and requested_kind != slot.kind:
                if requested_kind not in ("raster", "scatter"):
                    text = f"⚠ Panel {slot.id}: unknown kind {requested_kind!r}."
                    self._notify(text)
                    return {"status": "error", "status_text": text,
                            "notify_text": text, "notify_color": "#f38ba8",
                            "failed_slot": slot.id}
                self._activate_slot_kind(slot.id, requested_kind)
                switched_kind_this_round.add(slot.id)

        # ---- Per-slot raster Y/X conflict, server-side backstop -------
        # Same reasoning as the old single global check (still true: the
        # browser already refuses to send this — doPlot's own guard, plus
        # each raster panel's own live conflict_div from Group 3 piece 1
        # — but this handler is the single entry point for Plot ▶,
        # Reload ↺, every preset, and any future programmatic caller, so
        # it's checked again here rather than trusted from the client).
        # Now per-slot rather than global, since each slot has its own
        # independent raster Y/X pair.
        for slot in self._slots:
            panel_msg = panels_msg.get(slot.id)
            if panel_msg is None or panel_msg.get("kind") != "raster":
                continue
            if panel_msg.get("y") == panel_msg.get("x"):
                text = (f"⚠ Panel {slot.id}: Raster Y and X axes must be "
                        f"different (both set to {panel_msg.get('y')}).")
                self._notify(text)
                return {"status": "error", "status_text": text,
                        "notify_text": text, "notify_color": "#f38ba8",
                        "failed_slot": slot.id}

        self._selection = self._build_selection()
        pols      = self._selection.correlation or ["XX"]
        first_pol = pols[0]

        # Selection now applied to all four panel objects (Group 2
        # pattern), not just the two currently kind-active ones. A panel
        # object that's activated later (Chunk 2, via
        # _activate_slot_kind()) needs to already have current selection
        # rather than whatever was current when it was last constructed
        # or active — same "structural cost paid regardless of
        # visibility" reasoning as everywhere else in this class.
        for panel in self._all_panels:
            panel._selection = self._selection

        panels_response = {}
        for slot in self._slots:
            panel_msg = panels_msg.get(slot.id)
            if panel_msg is None:
                continue
            kind  = panel_msg["kind"]
            # The Chunk 2 loop above already activated this slot's kind
            # if it wasn't already active, so slot.active is
            # unambiguously the correct (and only) object to update here
            # — no fallback ambiguity, unlike the self._raster/
            # self._scatter compatibility properties elsewhere in this
            # class.
            panel = slot.active

            if kind == "raster":
                try:
                    y   = Axis[panel_msg["y"]]
                    x   = Axis[panel_msg["x"]]
                    qty = Axis[panel_msg["qty"]]
                except KeyError as exc:
                    log.warning("_handle_plot: unknown Axis %r for panel %s",
                                exc, slot.id)
                    continue

                # Force re-render when this slot's raster axes or
                # selection content actually changed — not just because a
                # new SelectionSpec object was created (which always
                # happens above) — OR when this slot's kind was switched
                # this round (checked explicitly, not just inferred from
                # the comparisons below).
                #
                # Bug fixed 2026-08-02 (found during testing): this used
                # to compare against self._last_raster_selection, a
                # single attribute shared across the whole app — correct
                # only as long as at most one slot could ever be raster.
                # With two slots both raster in the same _handle_plot()
                # call, processing slot A first would set that shared
                # tracker to match self._selection; slot B's own check,
                # running later in the same call, then read "selection
                # unchanged" as true even though slot B's raster object
                # had never had its range data sent to the client at
                # all — so x0/x1/y0/y1/title/state all came back null,
                # and the browser crashed trying to set a Bokeh
                # Range1d.start to null. Now keyed per slot
                # (self._last_raster_selection_by_slot), so slot B's
                # check is independent of whatever slot A just did in the
                # same call. The explicit switched_kind_this_round check
                # is a second, direct guarantee for the same underlying
                # case — belt and suspenders, not relying solely on the
                # selection comparison correctly inferring "this is
                # actually a first render" on its own.
                axes_changed = (
                    slot.id in switched_kind_this_round or
                    y   != panel._y_dim    or
                    x   != panel._x_dim    or
                    qty != panel._quantity or
                    self._selection.field_names  != getattr(self._last_raster_selection_by_slot.get(slot.id), 'field_names', None)  or
                    self._selection.spw          != getattr(self._last_raster_selection_by_slot.get(slot.id), 'spw', None)           or
                    self._selection.correlation  != getattr(self._last_raster_selection_by_slot.get(slot.id), 'correlation', None)   or
                    self._selection.data_column  != getattr(self._last_raster_selection_by_slot.get(slot.id), 'data_column', None)
                )
                try:
                    if axes_changed:
                        panel._y_dim    = None
                        panel._x_dim    = None
                        panel._quantity = None
                        panel.update_axes(
                            y_dim        = y,
                            x_dim        = x,
                            quantity     = qty,
                            polarization = first_pol,
                        )
                        self._last_raster_selection_by_slot[slot.id] = self._selection
                except Exception as exc:
                    log.error("_handle_plot: panel %s raster update_axes "
                              "failed: %s", slot.id, exc, exc_info=True)
                    text = f"⚠ Panel {slot.id} raster error: {exc}"
                    self._notify(text)
                    return {"status": "error", "status_text": text,
                            "notify_text": text, "notify_color": "#f38ba8",
                            "failed_slot": slot.id}

                img_data = panel._image_source.data
                x0, x1 = panel._x_range
                y0, y1 = panel._y_range
                panels_response[slot.id] = {
                    "kind":  "raster",
                    # Always sent (even when axes unchanged) to keep the
                    # hover tool renderer active — same reasoning as the
                    # original raster_image field.
                    "image": img_data["image"][0],
                    "x0":      float(x0)              if axes_changed else None,
                    "x1":      float(x1)              if axes_changed else None,
                    "y0":      float(y0)              if axes_changed else None,
                    "y1":      float(y1)              if axes_changed else None,
                    # panel.*, not _axis_label(x): a bare Axis has no
                    # selection context, so it cannot know that CHANNEL
                    # resolved to frequency, and no range, so it cannot
                    # carry the SI prefix.  It produced "Frequency [Hz]"
                    # under ticks the browser was already scaling to GHz.
                    # _effective_title() and _state_data() beside this
                    # already read the panel; the labels must too.
                    "x_label": panel._panel_spec().axis_label("x")
                               if axes_changed else None,
                    "y_label": panel._panel_spec().axis_label("y")
                               if axes_changed else None,
                    "title":   panel._effective_title() if axes_changed else None,
                    "state":   panel._state_data()      if axes_changed else None,
                }

            else:  # kind == "scatter"
                try:
                    x = Axis[panel_msg["x"]]
                    y = Axis[panel_msg["y"]]
                except KeyError as exc:
                    log.warning("_handle_plot: unknown Axis %r for panel %s",
                                exc, slot.id)
                    continue

                # Recompute-cost fix (added 2026-08-02): scatter used to
                # re-render unconditionally every call — see the note by
                # self._last_scatter_selection_by_slot's declaration in
                # __init__ for why this mattered enough to fix now, not
                # just flag. Mirrors raster's axes_changed check exactly:
                # scatter doesn't have a single _y_dim attribute the way
                # raster does (its Y axis lives inside each layer), so
                # the comparison reads it off the first existing layer —
                # _make_scatter_layers() always builds one layer per
                # polarization sharing the same y_axis, so any layer's
                # y_axis is representative. never_rendered mirrors
                # _activate_slot_kind()'s own check (all layer_dfs None)
                # for the same reason: a never-rendered panel's stored
                # dims could otherwise coincidentally match the request
                # and wrongly skip its first real render.
                current_y_axis = panel._layers[0].y_axis if panel._layers else None
                current_pols   = [lyr.polarization for lyr in panel._layers]
                never_rendered = (not panel._layers or
                                  all(df is None for df in panel._layer_dfs))
                axes_changed = (
                    slot.id in switched_kind_this_round or
                    never_rendered or
                    x    != panel._x_dim or
                    y    != current_y_axis or
                    pols != current_pols or
                    self._selection.field_names  != getattr(self._last_scatter_selection_by_slot.get(slot.id), 'field_names', None)  or
                    self._selection.spw          != getattr(self._last_scatter_selection_by_slot.get(slot.id), 'spw', None)           or
                    self._selection.correlation  != getattr(self._last_scatter_selection_by_slot.get(slot.id), 'correlation', None)   or
                    self._selection.data_column  != getattr(self._last_scatter_selection_by_slot.get(slot.id), 'data_column', None)
                )
                try:
                    if axes_changed:
                        layers = _make_scatter_layers(
                            y, pols, cmaps=self._scatter_ramps)
                        log.debug("_handle_plot: panel %s scatter update_axes "
                                  "x=%s layers=%s", slot.id, x,
                                  [(l.y_axis, l.polarization) for l in layers])
                        panel._x_dim = None
                        panel.update_axes(x_dim=x, layers=layers)
                        log.debug("_handle_plot: panel %s scatter _layer_aggs "
                                  "after render: %s", slot.id,
                                  [a is not None for a in panel._layer_aggs])
                        self._last_scatter_selection_by_slot[slot.id] = self._selection
                except Exception as exc:
                    log.error("_handle_plot: panel %s scatter update_axes "
                              "failed: %s", slot.id, exc, exc_info=True)
                    text = f"⚠ Panel {slot.id} scatter error: {exc}"
                    self._notify(text)
                    return {"status": "error", "status_text": text,
                            "notify_text": text, "notify_color": "#f38ba8",
                            "failed_slot": slot.id}

                img_data = panel._image_source.data
                panels_response[slot.id] = {
                    "kind":    "scatter",
                    # Always sent (even when axes unchanged) to keep the
                    # hover tool renderer active — same reasoning as
                    # raster's image field above. Reading from
                    # panel._image_source.data is correct even when
                    # axes_changed is False: nothing changed means
                    # whatever was rendered last time is still current.
                    "image":   img_data["image"][0],
                    "x0":      float(img_data["x"][0])                                if axes_changed else None,
                    "x1":      float(img_data["x"][0]) + float(img_data["dw"][0])      if axes_changed else None,
                    "y0":      float(img_data["y"][0])                                if axes_changed else None,
                    "y1":      float(img_data["y"][0]) + float(img_data["dh"][0])      if axes_changed else None,
                    # See the raster branch: labels come from the panel
                    # so they carry the resolved axis and its SI prefix.
                    "x_label": panel._panel_spec().axis_label("x")
                               if axes_changed else None,
                    "y_label": panel._panel_spec().axis_label("y")
                               if axes_changed else None,
                    "title":   panel._effective_title()    if axes_changed else None,
                    "state":   panel._state_data()         if axes_changed else None,
                }

        self._notify("")   # clear any previous warning
        return {
            "status":      "ok",
            "status_text": self._status_text(),
            "notify_text": "",
            "panels":      panels_response,
        }

    async def _handle_done(self, msg, context=None):
        self._stop()
        return {"result": "stopped"}

    async def _handle_box_select(self, msg: dict, panel: str) -> Optional[dict]:
        x0 = float(msg.get("x0", 0.0))
        x1 = float(msg.get("x1", 0.0))
        y0 = float(msg.get("y0", 0.0))
        y1 = float(msg.get("y1", 0.0))
        # FlagTool(flag=True) vs. FlagTool(flag=False) ("Unflag") — both
        # send through the same _msg_select channel and are distinguished
        # here. Default True only covers messages from something older
        # that didn't set the field.
        flag = bool(msg.get("flag", True))
        verb = "flag" if flag else "unflag"

        # No Bokeh server here, so self._notify()'s Python-side
        # `_notify_div.text = ...` assignment never reaches the browser
        # on its own — it's still called (keeps Python-side state
        # consistent, e.g. for a later full-page re-render), but the
        # thing that actually updates the browser is this handler's
        # *return value*: FlagTool's comm.send() callback (flag_tool.ts)
        # applies notify_text/notify_color/status_text directly to the
        # live notify_div/status_div models, the same p2j response
        # mechanism doPlot already uses for resp.status_text.
        color_warn = "#f38ba8"
        color_ok   = "#a6e3a1"

        # The box now draws at any zoom level (better UX feedback than a
        # silent no-op), but flagging is still only semantically valid at
        # or past 1:1 pixel resolution — averaged/decimated bins aren't
        # individual visibilities. Below that, tell the user why nothing
        # happened instead of just doing nothing.
        if not bool(msg.get("at_pixel_res", False)):
            text = (f"⚠ Zoom to ≥1:1 pixel resolution before you can {verb} "
                    f"({panel}) — nothing {verb}ged.")
            self._notify(text, color=color_warn)
            return {"notify_text": text, "notify_color": color_warn}

        if panel == "raster":
            delta = FlagDelta(
                flag       = flag,
                time_range = (min(x0, x1), max(x0, x1))
                             if self._raster_x == Axis.TIME else None,
                freq_range = (min(x0, x1), max(x0, x1))
                             if self._raster_x in (Axis.CHANNEL, Axis.FREQUENCY)
                             else None,
                source  = f"raster_box_{verb}",
                comment = f"raster {verb} box x=[{x0:.4g},{x1:.4g}] y=[{y0:.4g},{y1:.4g}]",
            )
        else:
            delta = FlagDelta(
                flag       = flag,
                time_range = (min(x0, x1), max(x0, x1))
                             if self._scatter_x == Axis.TIME else None,
                freq_range = (min(x0, x1), max(x0, x1))
                             if self._scatter_x in (Axis.CHANNEL, Axis.FREQUENCY)
                             else None,
                source  = f"scatter_box_{verb}",
                comment = f"scatter {verb} box x=[{x0:.4g},{x1:.4g}] y=[{y0:.4g},{y1:.4g}]",
            )

        self._flag_db.append(delta)
        count = self._flag_db.pending_count
        log.debug(
            "%s round trip delivered to Python: panel=%s "
            "x=[%.4g,%.4g] y=[%.4g,%.4g] count=%d",
            verb.capitalize(), panel, x0, x1, y0, y1, count,
        )
        self._render_flag_overlay()
        self._update_status_bar()
        text = (f"✓ {verb.capitalize()}ged box recorded ({panel}) — "
                f"preview only, stored — not yet committed. "
                f"Flag count: {count}.")
        self._notify(text, color=color_ok)
        return {
            "notify_text":  text,
            "notify_color": color_ok,
            "status_text":  self._status_text(),
        }

    # ====================================================================== #
    # Flag overlay rendering (stub — Phase 1 F-9/F-10)                        #
    # ====================================================================== #

    def _render_flag_overlay(self) -> None:
        pending = self._flag_db.overlay_deltas()
        log.debug("_render_flag_overlay: %d pending delta(s) — stub", len(pending))
        # TODO Phase 1: query backend for flagged rows, shade red, composite.

    # ====================================================================== #
    # SelectionSpec construction                                               #
    # ====================================================================== #

    def _build_selection(self) -> SelectionSpec:
        field_name = _parse_field_string(self._field_str, self._meta)
        # Explicit identities from the SPW table win over the string
        # form: they are already the values _spw_selected compares
        # against, and re-deriving them from text could only lose
        # information.
        chosen = getattr(self, "_spw_ids", None)
        spw_ids = (list(chosen) if chosen is not None
                   else _parse_spw_string(self._spw_str, self._meta))
        corrs      = _parse_correlation_string(self._corr_str, self._meta)
        return SelectionSpec(
            field_names = [field_name] if field_name else None,
            spw         = spw_ids or None,
            correlation = corrs or None,
            data_column = self._datacolumn,
            time_range  = self._time_range,
            freq_range  = self._freq_range,
        )

    def _notify(self, text: str, color: str = "#f38ba8") -> None:
        """Show a transient notification in the status bar.

        Parameters
        ----------
        text : str
            HTML message to display.  Empty string clears the notification.
        color : str
            CSS color for the text.  Default is red (#f38ba8) for errors
            and warnings.  Use ``#a6e3a1`` (green) for success messages.
        """
        if hasattr(self, "_notify_div") and self._notify_div is not None:
            self._notify_div.styles = dict(
                self._notify_div.styles,
                color=color,
            )
            self._notify_div.text = text

    # ====================================================================== #
    # Status bar                                                               #
    # ====================================================================== #

    def _status_text(self) -> str:
        fname        = os.path.basename(self._source_path)
        layout_label = {"one": "One panel", "side": "Side by Side",
                        "over": "Over / Under"}.get(self._layout, self._layout)

        # I-1 (Phase 2.5): once a single field/SPW is selected -- via
        # Prev/Next or a manual pick, the two are indistinguishable here
        # by design (see _field_iteration_position's docstring) -- show
        # its position, e.g. "Field 3/7: 0637-752". Falls back to the
        # pre-I-1 "Field: <str or 'all'>" display otherwise (unset, or,
        # for SPW, a genuine multi-selection).
        field_pos = _field_iteration_position(self._field_str, self._meta)
        if field_pos:
            pos, n_fields = field_pos
            field_name = _parse_field_string(self._field_str, self._meta)
            field = f"Field {pos}/{n_fields}: {field_name}"
        else:
            field = f"Field: {self._field_str or 'all'}"

        chosen  = getattr(self, "_spw_ids", None)
        spw_ids = (list(chosen) if chosen is not None
                   else _parse_spw_string(self._spw_str, self._meta))
        spw_pos = _spw_iteration_position(spw_ids, self._meta)
        if spw_pos:
            pos, n_spws = spw_pos
            spw_label = next(
                (s.name or str(s.spw_id) for s in self._meta.spws
                 if s.spw_id == spw_ids[0]), str(spw_ids[0]))
            spw = f"SPW {pos}/{n_spws}: {spw_label}"
        else:
            spw = f"SPW: {self._spw_str or 'all'}"

        col = self._datacolumn
        count = self._flag_db.pending_count
        flag_note = (f"  |  <b>Flag count:</b> {count}"
                     if count > 0 else "")
        return (
            f"<b>{fname}</b>  |  Layout: {layout_label}<br>"
            f"{field}  |  {spw}  |  Col: {col}{flag_note}"
        )

    def _update_status_bar(self) -> None:
        if hasattr(self, "_status_div") and self._status_div is not None:
            self._status_div.text = self._status_text()

    # ====================================================================== #
    # Layout construction                                                      #
    # ====================================================================== #

    def _build_layout(self):
        """Build the full layout.

        Build order is significant — each step creates attributes referenced
        by later steps:

        1. ``_pref_source``       — needed by toolbar layout_js and plot area
        2. ``_build_status_bar``  — creates ``_status_div`` (toolbar plot_js)
        3. ``_build_sidebar``     — creates all ``_*_select`` / ``_corr_cbg``
        4. ``_build_toolbar``     — references all of the above
        5. ``_build_plot_area``   — references ``_pref_source``; returns
                                    (side_container, over_container)
        """
        self._pref_source = ColumnDataSource(data={"prefs": ["{}"]})

        # Inject page-level dark background and tool sync via add_init_script.
        # This runs at app init time in JS before anything else renders.
        self._app_context.add_init_script(
            code="""
document.body.style.background            = '#181825';
document.documentElement.style.background = '#181825';
""",
            description="dark page background",
        )

        # CSS injection for light-mode sidebar widget overrides.
        # When document.body gets class "cv-light", these rules activate.
        _css_div = Div(
            text="""<style>
.cv-light .cv-sidebar { background: #f8f8f0 !important; border-right: 1px solid #ccc !important; }
.cv-light .cv-sidebar .bk-input { background: #ffffff !important; color: #222222 !important; border-color: #aaa !important; }
.cv-light .cv-sidebar select.bk-input option { background: #fff; color: #222; }
.cv-light .cv-sidebar .bk-input-group label,
.cv-light .cv-sidebar .bk-label,
.cv-light .cv-sidebar label { color: #222222 !important; }
.cv-light .cv-sidebar .bk-btn { background: #f0f0f0 !important; color: #222 !important; border-color: #aaa !important; }
</style>""",
            width=0, height=0,
            styles={"display": "none"},
        )

        status_bar               = self._build_status_bar()

        # FlagTool/Unflag instances are built earlier (during __init__, via
        # each panel's own VisibilityPlot._build() -> _add_flag_tools()),
        # before _notify_div/_status_div exist — so wire them here, now
        # that both divs are available. None when enable_flagging=False.
        # All four panel objects (self._all_panels, Group 2 rework
        # 2026-07-31) — previously only the two kind-active ones, leaving
        # an initially-inactive panel's flag/unflag tools unwired until
        # some other code path happened to fix them up.
        for panel in self._all_panels:
            for tool in (getattr(panel, "_flag_tool", None),
                         getattr(panel, "_unflag_tool", None)):
                if tool is not None:
                    tool.notify_div = self._notify_div
                    tool.status_div = self._status_div

        sidebar_col, toggle_btn  = self._build_sidebar()
        toolbar                  = self._build_toolbar(toggle_btn)
        side_container, over_container = self._build_plot_area()

        # Both containers always in the document; only one visible.
        side_container.visible = (self._layout == "side")
        over_container.visible = (self._layout == "over")

        # stretch_width lets containers fill the browser window as it resizes
        # and correctly reclaims space when the sidebar is collapsed.
        plot_area = column(
            side_container, over_container,
            sizing_mode="stretch_width",
        )
        body = row(
            sidebar_col, plot_area,
            sizing_mode="stretch_width",
        )
        return column(_css_div, toolbar, body, status_bar,
                      sizing_mode="stretch_width")

    def _style_cmap_column(self, cmap_col, dark_stylesheet) -> tuple:
        """Apply dark styling to every themeable element in a colormap column.

        ``colormap_controls()`` returns a Bokeh ``column`` whose children
        are ``Select``, ``TextInput``, ``Button`` (the histogram reset
        button), ``Div``, a histogram ``figure``, and ``row``/``column``
        containers. This method walks the tree and applies dark styling
        to each, returning three flat lists so the dark/light JS
        callback can keep them in sync when the user toggles later:

        - ``styled`` — ``Select``/``TextInput``/``Button`` widgets,
          updated the same way as the rest of the sidebar (swapping
          ``stylesheets[0].css``, the shared mechanism the toggle JS's
          `widgets` loop already handles).
        - ``styled_figs`` — the colormap histogram ``figure``, updated
          via its own dedicated toggle-JS block (NOT the same one the
          main panel figures use — the histogram intentionally uses a
          dimmer, not stark black/white, background so the plotted
          distribution stays legible; see colormap_controls' own
          comments).
        - ``styled_icons`` — the reset button's ``BuiltinIcon``, whose
          ``color`` doesn't live on the ``Button`` itself and so isn't
          reachable via `stylesheets` at all.

        Button was previously not handled here at all (a real gap,
        found via live light-mode testing) — its dark colors were
        instead hardcoded once at construction time in
        ``colormap_controls()`` itself, with no way to ever change them
        again. Fixed here by PREPENDING ``dark_stylesheet`` to whatever
        stylesheets the widget already has (rather than replacing them,
        as done for Select/TextInput) — the reset button also carries
        its own small stylesheet for icon padding that must survive
        this. Prepending specifically (not appending) matters: the
        toggle JS's widget loop unconditionally does
        ``w.stylesheets[0].css = widget_css``, so the shared,
        toggle-managed stylesheet has to stay at index 0 or the toggle
        would silently clobber the padding rule instead of actually
        updating the theme.
        """
        from bokeh.models import Select, TextInput, Div, Button, Plot

        styled = []
        styled_figs = []
        styled_icons = []

        def _walk(node):
            if isinstance(node, (Select, TextInput)):
                node.stylesheets = [dark_stylesheet]
                styled.append(node)
            elif isinstance(node, Button):
                node.stylesheets = [dark_stylesheet] + list(node.stylesheets)
                styled.append(node)
                if getattr(node, "icon", None) is not None:
                    styled_icons.append(node.icon)
            elif isinstance(node, Plot):
                # The histogram figure inside colormap_controls()'s
                # returned column -- distinct from the four main panel
                # figures (self._all_panels), which already have their
                # own recoloring via the `figs` arg below.
                styled_figs.append(node)
            elif isinstance(node, Div):
                # Style equation label Div text color
                if node.text and not node.text.startswith("<span"):
                    node.text = (
                        f"<span style='color:#a6adc8;font-size:11px'>"
                        f"{node.text}</span>"
                    )
            # Walk children of layout containers
            children = getattr(node, "children", None)
            if children:
                for child in children:
                    _walk(child)

        _walk(cmap_col)
        return styled, styled_figs, styled_icons

    # ---------------------------------------------------------------------- #
    # Sidebar                                                                  #
    # ---------------------------------------------------------------------- #

    def _dark(self):
        """Return an InlineStyleSheet applying the dark widget theme."""
        return InlineStyleSheet(css=_DARK_WIDGET_CSS)

    def _tt(self, html: str, position: str = "bottom") -> Tooltip:
        """Build a Tooltip for wrapping a widget with Tip(widget, tooltip=...).

        Promoted from a nested function local to ``_build_toolbar()``
        (added 2026-08-03) so ``_build_sidebar()``'s per-tab widgets
        (Raster/Scatter switch, Cancel, Swap) can use the exact same
        tooltip convention already established for the top toolbar,
        rather than a second, separately-defined helper doing the same
        thing.
        """
        return Tooltip(content=BokehHTML(html), position=position)

    # ---------------------------------------------------------------------- #
    # Group 3 piece 1 (added 2026-07-31): per-slot config panels            #
    # ---------------------------------------------------------------------- #
    #
    # One raster panel and one scatter panel per slot — mirrors decision
    # 11's own "structural cost paid regardless of visibility" precedent
    # for the plot objects themselves, one layer up. Deliberately two
    # separate builder methods, not one parameterized by kind: raster and
    # scatter genuinely have different fields (raster's min/max via
    # colormap_controls() vs. scatter's lack of them is the concrete
    # example that settled this), so a shared builder would need kind
    # branching internally anyway — clearer to just have two.
    #
    # Both are called in a loop over self._slots (not written as two
    # hardcoded per-slot calls) specifically so a third slot, whenever
    # that's supported, is a non-event here — build for N, ship for 2.

    def _build_raster_config_panel(self, slot: "_PanelSlot", dark) -> tuple:
        """Build one slot's raster config panel: Y/X/quantity + colormap.

        Wired to ``slot.raster`` — that slot's own, already-constructed
        ``VisibilityRaster`` object — via ``colormap_controls()``, so
        scaling/min/max controls are genuinely independent per slot, not
        shared. The Y/X/quantity ``Select`` widgets are plain pickers with
        no live wiring of their own; they're read at Plot-press time by
        ``_handle_plot()``'s rewrite (piece 3, not yet built), same as the
        old global controls were.

        Returns
        -------
        panel : Bokeh column
        widgets : dict
            ``y_sel``, ``x_sel``, ``q_sel``, ``conflict_div``,
            ``cmap_widgets``, ``cmap_figs``, ``cmap_icons`` — stored by
            the caller in ``self._panel_axis_widgets[slot.id]["raster"]``.
        """
        ry_sel = Select(
            title="Raster Y axis", value=self._raster_y.name,
            options=[(k, v) for k, v in _RASTER_Y_OPTIONS],
            width=_SIDEBAR_WIDTH, stylesheets=[dark],
        )
        rx_sel = Select(
            title="Raster X axis", value=self._raster_x.name,
            options=[(k, v) for k, v in _RASTER_X_OPTIONS],
            width=_SIDEBAR_WIDTH, stylesheets=[dark],
        )
        rq_sel = Select(
            title="Raster quantity", value=self._raster_qty.name,
            options=[(k, v) for k, v in _RASTER_QTY_OPTIONS],
            width=_SIDEBAR_WIDTH, stylesheets=[dark],
        )

        # Per-slot Y/X conflict indicator — an inline Div scoped to this
        # panel, not the shared self._notify_div the old single global
        # raster section used. A shared message can't say which tab it's
        # about now that both tabs can be open with independent raster
        # configs at once (flagged explicitly when this design was
        # discussed, not an oversight).
        conflict_div = Div(
            text="", width=_SIDEBAR_WIDTH,
            styles={"color": "#f38ba8", "font-size": "12px"},
        )
        conflict_msg = "⚠ Raster Y and X axes must be different."
        conflict_js = CustomJS(
            args={"ry_sel": ry_sel, "rx_sel": rx_sel,
                  "conflict_div": conflict_div, "msg": conflict_msg},
            code="""
const conflict = (ry_sel.value === rx_sel.value);
conflict_div.text = conflict ? msg : '';
""",
        )
        ry_sel.js_on_change("value", conflict_js)
        rx_sel.js_on_change("value", conflict_js)

        raster_cmap  = slot.raster.colormap_controls()
        cmap_widgets, cmap_figs, cmap_icons = self._style_cmap_column(raster_cmap, dark)

        panel = column(
            Div(text="<span style='color:#89b4fa;font-weight:bold'>"
                     "── Raster ──</span>", width=_SIDEBAR_WIDTH),
            ry_sel, rx_sel, rq_sel,
            conflict_div,
            raster_cmap,
        )
        widgets = {
            "y_sel": ry_sel, "x_sel": rx_sel, "q_sel": rq_sel,
            "conflict_div": conflict_div, "cmap_widgets": cmap_widgets,
            "cmap_figs": cmap_figs, "cmap_icons": cmap_icons,
        }
        return panel, widgets

    def _build_scatter_config_panel(self, slot: "_PanelSlot", dark) -> tuple:
        """Build one slot's scatter config panel: X/Y + colormap.

        Same reasoning as ``_build_raster_config_panel`` above — see that
        docstring. No Y/X conflict check here: scatter's X and Y axes
        aren't drawn from the same shared vocabulary the way raster's are,
        so the same-value conflict this class checks for raster doesn't
        apply.

        Returns
        -------
        panel : Bokeh column
        widgets : dict
            ``x_sel``, ``y_sel``, ``cmap_widgets``, ``cmap_figs``,
            ``cmap_icons`` — stored by the caller in
            ``self._panel_axis_widgets[slot.id]["scatter"]``.
        """
        sx_sel = Select(
            title="Scatter X axis", value=self._scatter_x.name,
            options=[(k, v) for k, v in _SCATTER_X_OPTIONS],
            width=_SIDEBAR_WIDTH, stylesheets=[dark],
        )
        sy_sel = Select(
            title="Scatter Y axis", value=self._scatter_y.name,
            options=[(k, v) for k, v in _SCATTER_Y_OPTIONS],
            width=_SIDEBAR_WIDTH, stylesheets=[dark],
        )
        scatter_cmap = slot.scatter.colormap_controls(layer_index=0)
        cmap_widgets, cmap_figs, cmap_icons = self._style_cmap_column(scatter_cmap, dark)

        panel = column(
            Div(text="<span style='color:#89b4fa;font-weight:bold'>"
                     "── Scatter ──</span>", width=_SIDEBAR_WIDTH),
            sx_sel, sy_sel,
            scatter_cmap,
        )
        widgets = {
            "x_sel": sx_sel, "y_sel": sy_sel, "cmap_widgets": cmap_widgets,
            "cmap_figs": cmap_figs, "cmap_icons": cmap_icons,
        }
        return panel, widgets

    def _build_sidebar(self):
        """Build sidebar column + collapse toggle button.

        Returns
        -------
        sidebar_col : Bokeh column
            The full sidebar, collapsible via ``visible`` toggle.
        toggle_btn : Button
            The ⟨ / ⟩ toggle button — placed in the toolbar row by
            ``_build_toolbar`` so it is always visible.
        """
        meta = self._meta
        dark = self._dark()
        # Shared by every Prev/Next icon button (Field and SPW) — see
        # _ICON_BTN_CSS's own comment for why this is a second,
        # untouched stylesheet slot rather than folded into `dark`.
        self._icon_btn_css = InlineStyleSheet(css=_ICON_BTN_CSS)

        # ---- Source path -------------------------------------------------- #
        path_div = Div(
            text=(
                f"<b style='color:#cdd6f4'>Source:</b> "
                f"<span style='font-family:monospace;font-size:11px;"
                f"color:#a6e3a1'>"
                f"{os.path.basename(self._source_path)}</span>"
            ),
            width=_SIDEBAR_WIDTH,
        )
        self._path_div = path_div

        # Section headings are collected so the theme restyle can reach
        # them.  They previously hardcoded color:#cdd6f4 inline at
        # construction with nothing to ever change it -- the same defect
        # the restyle body's own comments describe -- so they stayed
        # dark-blue on a light sidebar and read as disabled.
        self._section_divs = []

        def _section(text, width=_SIDEBAR_WIDTH, margin=None):
            kwargs = dict(text=f"<span style='color:{_SECTION_DARK};"
                               f"font-weight:bold'>{text}</span>",
                          width=width)
            if margin is not None:
                kwargs["margin"] = margin
            d = Div(**kwargs)
            d.tags = ["section-label", text]
            self._section_divs.append(d)
            return d

        # ---- Data column -------------------------------------------------- #
        col_options = list(meta.data_columns) or ["DATA"]
        self._col_select = Select(
            title       = "Data column",
            value       = self._datacolumn if self._datacolumn in col_options
                          else col_options[0],
            options     = col_options,
            width       = _SIDEBAR_WIDTH,
            stylesheets = [dark],
        )

        # ---- Field --------------------------------------------------------- #
        # Include an "All fields" sentinel so the widget can represent the
        # initial state (field_names=None) without auto-selecting a specific field.
        #
        # No title= on the Select itself, unlike every other Select in
        # this sidebar (col_select, the per-slot axis pickers, …) —
        # field_select uses an external _section("Field") label instead,
        # matching SPW's own heading-above-control shape. This isn't
        # just cosmetic: a Select's title renders as extra height
        # *inside* the widget, above its input, which the row below
        # measures as part of field_select's total height. The first
        # cut tried compensating for that with row(align="end") and
        # got the buttons wrong anyway — bottom-aligning against a
        # child whose true "control" (the input) doesn't sit at that
        # measured bottom edge either, just closer to it than the top.
        # Removing the title from the widget entirely removes the
        # mismatch instead of trying to compensate for it.
        field_options = [("", "All fields")] + [(f.name, f.name) for f in meta.fields]
        current_field = _parse_field_string(self._field_str, meta) or ""
        self._field_select = Select(
            value       = current_field if current_field in [v for v, _ in field_options]
                          else "",
            options     = field_options,
            width       = _IterButtons.LABEL_WIDTH,
            margin      = (0, 0, 0, 0),
            stylesheets = [dark],
        )
        # Prev/Next live beside the widget they step, not in the toolbar
        # (I-1 originally shipped a single toolbar "Animate: <axis>"
        # selector; reworked here per live-MS testing feedback — see
        # the implementation plan's Phase 2.5 design note). Built by
        # _IterButtons -- see that class's docstring for the three
        # rounds of sizing/theming/alignment/spacing feedback it exists
        # to not repeat -- and wired in _build_toolbar() once
        # self._do_plot_js/_plot_js_args exist (same split every other
        # sidebar control the toolbar's shared doPlot() reaches into
        # already uses). vertical_nudge=2: empirical, a <select>'s own
        # box metrics center a couple of pixels differently than the
        # plain <div> SPW's heading uses (its own row needs none) --
        # see _IterButtons' vertical_nudge docstring for the caveat
        # about whether this transfers to a future EvTextInput-backed
        # axis unchanged.
        self._field_iter = _IterButtons(
            axis_label="field", control=self._field_select,
            count=len(meta.fields), dark=dark,
            icon_btn_css=self._icon_btn_css, tt=self._tt,
            vertical_nudge=2,
        )
        self._field_prev_btn = self._field_iter.prev_btn
        self._field_next_btn = self._field_iter.next_btn
        field_col = column(
            _section("Field"),
            self._field_iter.row,
            width=_SIDEBAR_WIDTH,
            # Bottom margin only, not all-around: zeroing every margin
            # inside the row (the select, the buttons, the row itself --
            # all handled by _IterButtons) fixed the horizontal button-
            # alignment defect; this is a different margin, on the
            # *column wrapping* that row, governing space to the *next*
            # sidebar section (SPW) -- left at 0 the first time this was
            # written, which crowded SPW directly against Field with no
            # gap at all. 10px matches the ~10px gap every other
            # adjacent pair of sidebar sections gets for free from
            # Bokeh's default (5,5,5,5) margin on ordinary un-wrapped
            # widgets like self._col_select.
            margin=(0, 0, 10, 0),
        )

        # ---- SPW ----------------------------------------------------------- #
        # ---- Spectral windows ---------------------------------------- #
        #
        # A DataTable rather than a MultiSelect: an ASDM-imported MS can
        # carry 30 windows with non-contiguous ids, and the id alone does
        # not say which is the science window.  The frequency span and
        # channel count do -- a 1-channel window at 7 GHz is a WVR at a
        # glance.  DataTable also gives scrolling and checkbox selection
        # natively, where a CheckboxGroup would need both hand-built.
        #
        # The identity is carried in the source as-is, NOT stringified.
        # It may be an int or a spectral-window name (see
        # _partition_spw_ident), and the old MultiSelect path stringified
        # it into a comma-joined value only for _parse_spw_string to take
        # it apart again -- a round trip through text that a name
        # containing a comma would have broken.
        # Both stylesheets are built HERE, in Python, and the restyle
        # callback only swaps which one is attached.  Constructing one in
        # JS needs `Bokeh.InlineStyleSheet`, which is not a constructor in
        # that namespace -- and the resulting TypeError aborted the rest
        # of the restyle body, so a single bad line stopped the theme
        # changing anywhere at all.
        self._table_css_dark  = InlineStyleSheet(css=_DARK_TABLE_CSS)
        self._table_css_light = InlineStyleSheet(css=_LIGHT_TABLE_CSS)
        self._sidebar_css     = dark

        spws = list(meta.spws)
        preselected = _parse_spw_string(self._spw_str, meta)
        self._spw_source = ColumnDataSource(data=dict(
            ident   = [s.spw_id for s in spws],
            name    = [s.name or str(s.spw_id) for s in spws],
            freq    = [f"{(s.centre_freq_hz - s.bandwidth_hz / 2) / 1e9:.2f}"
                       f"–{(s.centre_freq_hz + s.bandwidth_hz / 2) / 1e9:.2f}"
                       if s.bandwidth_hz else "" for s in spws],
            nchan   = [str(s.n_channels) if s.n_channels else "" for s in spws],
        ))
        self._spw_source.selected.indices = [
            i for i, s in enumerate(spws) if s.spw_id in preselected
        ]
        self._spw_table = DataTable(
            source          = self._spw_source,
            columns         = [
                TableColumn(field="name",  title="Spectral window", width=180),
                TableColumn(field="freq",  title="GHz",             width=96),
                TableColumn(field="nchan", title="Ch",              width=44),
            ],
            selectable      = "checkbox",
            index_position  = None,
            width           = _SIDEBAR_WIDTH,
            height          = min(max(len(spws), 1) * 26 + 30, 170),
            stylesheets     = [dark, self._table_css_dark],
            sizing_mode     = "fixed",
        )
        # No All/None buttons: DataTable's checkbox column puts a
        # select-all toggle in the header row, which does both jobs.  The
        # separate buttons duplicated it and were the one sidebar control
        # the theme restyle still did not reach -- removing them is a
        # better answer than styling a redundant widget.
        #
        # The distinction the buttons were meant to carry survives
        # regardless: "none selected" is a real state, not a synonym for
        # "all", because Plot refuses an empty selection (see the doPlot
        # guard).  Unchecking everything is a transient step on the way to
        # checking two, so the empty case is only reachable by pressing
        # Plot deliberately.
        #
        # Prev/Next: built by _IterButtons, same as Field's -- see that
        # class's docstring. axis_label="spectral window", not "SPW",
        # since it's what the disabled-state tooltip reads out in full
        # ("This dataset has only 1 spectral window — nothing to
        # iterate"); the heading itself stays the short "SPW" via
        # _section() below, unrelated to axis_label. No vertical_nudge:
        # a plain Div centers predictably against a Button at an
        # identical declared height, unlike a <select> (see Field's
        # vertical_nudge=2 and _IterButtons' docstring on why) --
        # this row was in fact the reference point used to diagnose
        # that Field's own row needed one at all.
        spw_heading = _section("SPW", width=_IterButtons.LABEL_WIDTH,
                               margin=(0, 0, 0, 0))
        self._spw_iter = _IterButtons(
            axis_label="spectral window", control=spw_heading,
            count=len(meta.spws), dark=dark,
            icon_btn_css=self._icon_btn_css, tt=self._tt,
        )
        self._spw_prev_btn = self._spw_iter.prev_btn
        self._spw_next_btn = self._spw_iter.next_btn
        self._spw_select = column(
            self._spw_iter.row,
            self._spw_table,
            width=_SIDEBAR_WIDTH,
            # Same 10px-bottom-only fix as field_col, same reason: this
            # wrapper's all-zero margin fixed the row's internal
            # horizontal alignment but also zeroed the gap to whatever
            # sidebar section comes next (Correlation) — restored here
            # rather than left crowded the same way Field/SPW was.
            margin=(0, 0, 10, 0),
        )

        # ---- Correlations -------------------------------------------------- #
        all_corrs = (list(meta.spws[0].polarizations)
                     if meta.spws else ["XX", "YY"])
        sel_corrs = _parse_correlation_string(self._corr_str, meta)
        self._corr_cbg = CheckboxGroup(
            labels      = all_corrs,
            active      = [i for i, c in enumerate(all_corrs) if c in sel_corrs],
            width       = _SIDEBAR_WIDTH,
            stylesheets = [dark],
        )
        corr_label = _section("Correlation")

        # ---- Stub inputs for unwired selections ---------------------------- #
        # ---- Context-sensitive hint text from MS metadata ------------------- #
        import datetime
        def _mjd_to_iso(mjd_s: float) -> str:
            try:
                dt = datetime.datetime(1858, 11, 17) + datetime.timedelta(seconds=mjd_s)
                return dt.strftime("%Y/%m/%d/%H:%M:%S")
            except Exception:
                return str(mjd_s)

        scan_ids = sorted({s.scan_id for s in meta.scans})
        ant_names = sorted({a.name for a in meta.antennas})
        t0, t1 = meta.time_range

        field_names_str  = ", ".join(f.name for f in meta.fields)
        spw_ids_str      = ", ".join(str(s.spw_id) for s in meta.spws)
        corr_str         = ", ".join(meta.spws[0].polarizations) if meta.spws else "XX, YY"

        scan_hint    = (
            f"<b>Scan</b> — MSSelection integer list  "
            f"| Valid: {', '.join(str(s) for s in scan_ids[:8])}"
            + (f" … {scan_ids[-1]} ({len(scan_ids)} total)" if len(scan_ids) > 8 else "")
            + "  | e.g. <tt>1,3,7</tt>  or  <tt>1~7</tt>"
        )
        ant_hint     = (
            f"<b>Antenna</b> — name or index, MSSelection syntax  "
            f"| Antennas: {', '.join(ant_names[:6])}"
            + (f" … ({len(ant_names)} total)" if len(ant_names) > 6 else "")
            + "  | e.g. <tt>DA41</tt>  or  <tt>DA41&DV01</tt>  or  <tt>!DA42</tt>"
        )
        time_hint    = (
            f"<b>Time range</b> — YYYY/MM/DD/HH:MM:SS~YYYY/MM/DD/HH:MM:SS  "
            f"| Obs: {_mjd_to_iso(t0)} – {_mjd_to_iso(t1)}  "
            f"| e.g. <tt>{_mjd_to_iso(t0)}~{_mjd_to_iso(t1)}</tt>"
        )
        uvrange_hint = (
            "<b>UV range</b> — distance or wavelengths  "
            "| e.g. <tt>0~50klambda</tt>  or  <tt>100~300m</tt>  or  <tt>&gt;200m</tt>"
        )
        field_hint   = (
            f"<b>Field</b> — name or index  "
            f"| Fields: {field_names_str}  "
            "| e.g. <tt>TW Hya</tt>  or  <tt>0,2</tt>  or leave blank for all"
        )
        spw_hint     = (
            f"<b>SPW</b> — spectral window index (multi-select)  "
            f"| SPWs: {spw_ids_str}  "
            "| Select one or more; leave all unselected for all SPWs"
        )
        corr_hint    = (
            f"<b>Correlation</b> — tick to include  "
            f"| Available: {corr_str}  "
            "| Untick to exclude a polarisation from the display"
        )

        # Populate the pre-built hint divs (created in _build_status_bar)
        self._hint_field.text   = field_hint
        self._hint_spw.text     = spw_hint
        self._hint_corr.text    = corr_hint
        self._hint_scan.text    = scan_hint
        self._hint_antenna.text = ant_hint
        self._hint_time.text    = time_hint
        self._hint_uvrange.text = uvrange_hint

        def _focus_blur(widget, hint_div):
            """Wire MouseEnter→show hint (full width), MouseLeave→show
            status+notify row. Hints need the full status-bar width (some
            run long, e.g. the antenna-selection syntax hint), so both
            halves of the status row hide together rather than just
            _status_div — otherwise the red flag-notification half would
            keep showing alongside a hint, cramping it."""
            show = CustomJS(
                args={"hint": hint_div, "status_row": self._status_row},
                code="status_row.visible = false; hint.visible = true;",
            )
            hide = CustomJS(
                args={"hint": hint_div, "status_row": self._status_row},
                code="hint.visible = false; status_row.visible = true;",
            )
            widget.js_on_event(MouseEnter, show)
            widget.js_on_event(MouseLeave, hide)

        def _stub_input(title, placeholder, hint_div):
            inp = EvTextInput(
                title       = title,
                value       = placeholder,
                width       = _SIDEBAR_WIDTH,
                stylesheets = [dark],
            )
            _focus_blur(inp, hint_div)
            return inp

        scan_inp    = _stub_input("Scan",       self._scan_str,      self._hint_scan)
        antenna_inp = _stub_input("Antenna",    self._antenna_str,   self._hint_antenna)
        time_inp    = _stub_input("Time range", self._timerange_str, self._hint_time)
        uv_inp      = _stub_input("UV range",   self._uvrange_str,   self._hint_uvrange)

        # Wire focus/blur on the already-created select/checkbox widgets too
        _focus_blur(self._field_select, self._hint_field)
        # The table, not the column wrapper: _focus_blur attaches
        # MouseEnter/MouseLeave, which a layout container does not
        # emit, and the hint would silently never appear.
        _focus_blur(self._spw_table,    self._hint_spw)
        _focus_blur(self._corr_cbg,     self._hint_corr)

        # ---- Global raster/scatter axis sections removed (Group 3 piece
        # 2, added 2026-07-31). This sidebar section used to build
        # self._ry_select/_rx_select/_rq_select/_sx_select/_sy_select,
        # the old raster Y/X conflict check, and
        # self._raster_axis_section/_scatter_axis_section — all now dead
        # code since Chunk 1 (Group 3 piece 3) moved doPlot()/
        # _handle_plot() onto the per-slot config panels built in Group 3
        # piece 1 (_build_raster_config_panel()/_build_scatter_config_panel(),
        # called per slot further below). Confirmed dead via live test
        # before removal, not assumed: the global controls stopped having
        # any effect once Chunk 1 landed, while the gear-driven per-slot
        # panels correctly drove Plot ▶.

        # ---- Group 3 piece 1: gear/Tabs skeleton (built here, before
        # self._sidebar_col, so it can be included directly in that
        # column's children like every other sidebar section — no
        # post-construction .children mutation, matching this method's
        # existing pattern). One TabPanel per self._slots entry (placeholder
        # content only — no Kind selector, no wiring to
        # _activate_slot_kind() yet, that's a later Stage 1c increment).
        # Hidden by default; a gear click (added further below, once
        # toggle_btn exists) reveals it and activates that slot's tab. Per
        # decision 9 (see visplot-grid-iteration-notes.md): one TabPanel
        # pre-built per slot, not a shared retargeted drawer, so each
        # slot's in-progress config is preserved when switching away and
        # back, via Bokeh's native inactive-tab state retention.
        _tabs_dark = InlineStyleSheet(css=_DARK_TABS_CSS)
        # ---- Revised 2026-07-31: Tabs widget starts genuinely empty -------
        # (tabs=[]), not pre-populated with both slots' TabPanels. Per
        # feedback: pre-populating both meant the *header strip* always
        # showed both "Panel A"/"Panel B" labels the moment either gear was
        # clicked, even for the slot the user never asked to touch — the
        # gear tool wasn't actually narrowing what's revealed, just
        # toggling one shared widget's overall visibility. Each gear now
        # injects only its own (still singly-instantiated, never rebuilt)
        # TabPanel into tabs.tabs on click — see the gear CustomJS below.
        self._gear_tabs = Tabs(
            tabs=[],
            active=0,
            visible=False,
            width=_SIDEBAR_WIDTH_COL,
            stylesheets=[_tabs_dark],
        )

        # ---- Assemble sidebar column --------------------------------------- #
        # css_classes enables light-mode switching via a CSS class toggle
        # in the dark/light JS callback.
        self._sidebar_col = column(
            path_div,
            _section("Data"),
            self._col_select, field_col, self._spw_select,
            corr_label, self._corr_cbg,
            scan_inp, antenna_inp, time_inp, uv_inp,
            # "Axes" header removed (Group 3 piece 2, 2026-07-31) along
            # with self._raster_axis_section/_scatter_axis_section that
            # used to sit under it — axis controls now live inside each
            # tab's per-slot config panel instead. self._gear_tabs is
            # hidden by default, so a lone "Axes" label with nothing
            # visibly under it would have looked broken rather than just
            # collapsed.
            self._gear_tabs,
            width       = _SIDEBAR_WIDTH_COL,
            visible     = True,
            css_classes = ["cv-sidebar"],
            styles      = {
                "background":    "#1e1e2e",
                "padding":       "8px",
                "border-right":  "1px solid #45475a",
                "overflow-y":    "auto",
                "max-height":    f"{_PANEL_HEIGHT + 60}px",
            },
        )

        # ---- Collapse toggle button --------------------------------------- #
        toggle_btn = Button(
            label       = "⟨",
            button_type = "default",
            width       = 28,
            styles      = {"font-size": "14px", "padding": "0 4px"},
        )
        # Store on self so _build_plot_area can patch side_container /
        # over_container into the args after they are created.
        self._sidebar_toggle_js = CustomJS(
            args={
                "sidebar": self._sidebar_col,
                "btn":     toggle_btn,
                "side_container": None,   # patched in _build_plot_area
                "over_container": None,   # patched in _build_plot_area
            },
            code="""
const collapsing = sidebar.visible;
sidebar.visible  = !collapsing;
btn.label        = collapsing ? '⟩' : '⟨';
// sizing_mode="stretch_width" on figures and containers handles the
// reflow automatically — no explicit width manipulation needed.
""",
        )
        toggle_btn.js_on_click(self._sidebar_toggle_js)

        # ---- Stage 1c increment 1 (continued): gear/Tabs/Cancel ------------
        #
        # Revised 2026-07-31 (second pass), per feedback on the "[Panel X] "
        # prefix version: that prefix collided visually with this app's own
        # existing title convention, which already uses square brackets for
        # axis info (e.g. "Amplitude [Time vs Channel] pol=XX") — stacking
        # "[Panel A]" in front of that read as two unrelated uses of the
        # same bracket notation. It was also not attention-grabbing enough
        # given the title is otherwise unstyled black-on-dark text.
        #
        # Replaced with: on gear click, the figure's title is fully
        # *replaced* (not prefixed) with a plain "Panel A"/"Panel B" label
        # in a distinct red (_EDIT_TITLE_COLOR, reusing the same accent this
        # file already uses for the raster axis-conflict warning, so it's
        # not a new ad hoc color). Bokeh's Title annotation is plain
        # text — it does not support per-substring rich/HTML styling — so a
        # full-title color change is the most expressive signal actually
        # available here; a real risk (colorblind users, grayscale
        # screenshots) if this were the *only* signal, but it isn't: the
        # panel identity is still spelled out as literal text, not
        # color-only.
        #
        # A user who starts editing a panel is now explicitly in an
        # unfinished state for that panel until either a successful Plot ▶
        # (see doPlot() below) or the new Cancel button inside each tab,
        # which restores that slot's original title text+color and removes
        # its tab, "as if the gear had never been clicked."
        #
        # Cross-callback shared state: each slot gets its own small
        # ColumnDataSource (self._panel_title_state[slot.id]) holding the
        # pre-edit title text+color, written once by the gear click and
        # read by both Cancel and doPlot()'s success handler — same
        # cross-callback-state idiom already used elsewhere in this class
        # (e.g. _state_source for FlagTool). A plain Python dict would not
        # be shared live across separately-serialized CustomJS callbacks;
        # a ColumnDataSource is an actual client-side model instance, so
        # mutations by one callback are visible to the others.
        self._panel_title_state = {}
        self._panel_tabpanels   = {}
        # Group 3 (added 2026-07-31): per-slot, per-kind config panels.
        # self._panel_axis_widgets[slot.id][kind] -> dict of widget refs
        # (y_sel/x_sel/q_sel/conflict_div/cmap_widgets for raster;
        # x_sel/y_sel/cmap_widgets for scatter) — read at Plot-press time
        # by _handle_plot()'s rewrite (piece 3, not yet built). Keyed by
        # slot.id, not named _a/_b attributes, so this scales to more than
        # two slots without restructuring — same convention as
        # self._panel_title_state/self._panel_tabpanels above.
        # self._panel_kind_switch[slot.id] -> that slot's Raster/Scatter
        # RadioButtonGroup; its .active value at Plot-press time is the
        # slot's "pending kind" — no separate tracking variable needed,
        # since the switch only ever changes which config panel is
        # visible (client-side, no comm) until Plot ▶ reads it.
        self._panel_axis_widgets = {}
        self._panel_kind_switch  = {}
        # Swap buttons — one per tab (added 2026-08-03). A plain list, not
        # a dict, since neither swap_js's logic nor the later
        # side_container/over_container patching care which slot's tab a
        # given button lives in — both buttons do the identical thing.
        self._swap_btns = []
        self._swap_js_objects = []

        gear_click_js = """
const idx = tabs.tabs.indexOf(my_panel);
if (idx === -1) {
    // First time opening this round: remember the real title text+color
    // so Cancel can fully undo this. (A successful Plot needs only the
    // color restored — Python already resends fresh title *text* on
    // success, see doPlot() below.)
    //
    // Also remember WHICH KIND was actually clicked (added 2026-08-02,
    // fixing a bug found during testing): if the user changes the
    // Raster/Scatter switch within this same open tab before pressing
    // Plot, the switch takes effect at Plot time, but the figure that
    // was actually reddened here is whichever one *this* gear instance
    // is bound to — which may no longer match whatever kind ends up
    // active after the Plot press. The success handler needs to restore
    // color on the figure that was actually edited, not guess from the
    // post-Plot kind.
    orig_source.data['text']  = [fig.title != null ? fig.title.text : null];
    orig_source.data['color'] = [fig.title != null ? fig.title.text_color : null];
    orig_source.data['kind']  = [kind];
    orig_source.change.emit();

    if (fig.title != null) {
        fig.title.text       = panel_label;
        fig.title.text_color = edit_color;
        // Explicit emit — same idiom as r_img_src.change.emit() elsewhere
        // in this file: property assignment alone doesn't reliably force
        // a repaint in every case here (no Bokeh server driving this).
        fig.title.change.emit();
    }
    // Figure-level emit too, added alongside the Cancel-path fix attempt
    // for consistency — see cancel_click_js for the reasoning.
    fig.change.emit();

    tabs.tabs = tabs.tabs.concat([my_panel]);
    tabs.active = tabs.tabs.length - 1;
} else {
    // Already open — just bring it to front, don't rebuild/reset it.
    tabs.active = idx;
}
tabs.visible = true;

if (!sidebar.visible) {
    sidebar.visible  = true;
    toggle_btn.label = '⟨';
}

// Scroll the sidebar so the revealed Tabs widget is actually visible,
// rather than appearing off-screen at whatever scroll position the user
// happened to be at (reported: gear click while scrolled to the bottom
// of the global axis sections left only a sliver of the tab visible).
// The Tabs widget is the last child of the sidebar column, so scrolling
// to the container's full scrollHeight puts it in view. Deferred via
// setTimeout — same reasoning as the Cancel-button title fix earlier:
// the DOM hasn't reflowed to reflect tabs.visible=true yet in this same
// synchronous tick, so scrollHeight read right now would be stale.
//
// Revised: a plain document.querySelector('.cv-sidebar') reportedly found
// nothing (no visible effect at all, consistent with the `if (sidebarEl)`
// guard silently skipping). Bokeh 3.x commonly renders each root's content
// inside a Shadow DOM for style encapsulation, which a plain top-level
// querySelector cannot see across — it returns null rather than erroring,
// so the failure was silent. __cvFindEl recursively searches into every
// shadow root it finds instead of assuming everything is in the light DOM.
setTimeout(function() {
    function __cvFindEl(root, selector) {
        const found = root.querySelector(selector);
        if (found) return found;
        const all = root.querySelectorAll('*');
        for (let i = 0; i < all.length; i++) {
            if (all[i].shadowRoot) {
                const inner = __cvFindEl(all[i].shadowRoot, selector);
                if (inner) return inner;
            }
        }
        return null;
    }
    const sidebarEl = __cvFindEl(document, '.cv-sidebar');
    if (sidebarEl) {
        sidebarEl.scrollTop = sidebarEl.scrollHeight;
    }
}, 0);
"""
        cancel_click_js = """
if (fig.title != null) {
    fig.title.text       = orig_source.data['text'][0];
    fig.title.text_color = orig_source.data['color'][0];
    fig.title.change.emit();
}
fig.change.emit();

// Defer the tabs.tabs removal to the next tick (added after title-emit
// alone made no difference). Gear's title-set is immediately followed by
// tabs.tabs.concat(...) (adding a tab) and renders fine; Cancel's is
// immediately followed by tabs.tabs.filter(...) (removing one) and does
// not, until something else (mouse movement) forces a later repaint. The
// difference in symptoms points at the *removal* specifically — likely a
// heavier Tabs-view rebuild competing with the figure's own pending
// title-layout invalidation in the same synchronous tick, with one of
// the two invalidations getting dropped rather than both completing.
// Pushing the removal to a fresh tick via setTimeout(..., 0) is the
// standard workaround for two same-tick layout-invalidating updates
// racing like this; if this doesn't resolve it either, the same-tick
// collision theory is wrong and the next step is reconstructing a new
// Title object outright rather than mutating the existing one in place.
setTimeout(function() {
    tabs.tabs = tabs.tabs.filter(p => p !== my_panel);
    if (tabs.tabs.length === 0) {
        tabs.visible = false;
    } else {
        tabs.active = 0;
    }
}, 0);
"""
        # Swap button — added 2026-08-03. Clicking either slot's button
        # does the identical thing (swap is its own inverse, and with
        # exactly two positions there's only ever one possible action —
        # no "which target" question the way a future N-panel dropdown
        # will need), so this is one shared code string reused by two
        # separate Button/CustomJS instances (one per tab) below, not
        # duplicated logic. Genuinely zero-recompute: reorders
        # self._display_order_source (the live, client-side-mutable
        # tracker — self._slot_display_order itself is Python-side and
        # fixed, only ever read at construction) and rebuilds the
        # container's children from the two already-fully-rendered pairs
        # of layout objects — no comm round-trip, no re-render, matching
        # the recompute-cost concern this whole feature exists for.
        swap_js = """
const order   = display_order_source.data['order'][0].slice();
const swapped = [order[1], order[0]];
display_order_source.data = {order: [swapped]};

const slot0_pair = [slot0_raster_layout, slot0_scatter_layout];
const slot1_pair = [slot1_raster_layout, slot1_scatter_layout];
const first_pair  = (swapped[0] === 0) ? slot0_pair : slot1_pair;
const second_pair = (swapped[0] === 0) ? slot1_pair : slot0_pair;
const new_children = first_pair.concat(second_pair);

// Whole-array reassignment, not in-place mutation — same pattern
// already proven correct for tabs.tabs (Bokeh List-valued properties
// need reassignment, not .push()/.splice(), to notify the renderer).
side_container.children = new_children;
over_container.children = new_children;
"""
        # Fixed 2026-07-31 as part of the Group 1/2 rework: previously
        # paired via (self._slots[0], self._raster), (self._slots[1],
        # self._scatter) — documented at the time as a "positional shim"
        # that "stops being correct the moment kind-switching is wired
        # up," since self._raster/self._scatter resolve by kind across
        # *both* slots and could point at the wrong slot entirely once a
        # future Kind selector lets either slot hold either kind. That
        # shim is no longer needed: self._slots[i].active (Stage 1b.5)
        # already gives "whichever object *this specific slot* currently
        # shows," which is what gear/Cancel actually need — genuinely
        # correct per-slot resolution now, not a shim scoped to today's
        # fixed defaults.
        for slot in self._slots:
            orig_source = ColumnDataSource(data={"text": [""], "color": [None], "kind": [None]})
            self._panel_title_state[slot.id] = orig_source

            cancel_btn = Button(
                label       = "Cancel",
                button_type = "default",
                width       = 80,
                stylesheets = [dark],
            )

            # Group 3 piece 1: per-slot Raster/Scatter switch + both
            # config panels. RadioButtonGroup to match the visual language
            # already established by the Layout control (One/Side/Over) —
            # same kind of switch, same place a user would expect it.
            #
            # Purely a navigation control, not a trigger: switching only
            # toggles which of the two panels below is visible
            # (client-side, no comm), exactly like the "batched, not
            # immediate" reading confirmed when this was discussed —
            # actually switching the slot's kind only happens later, when
            # Plot ▶ is pressed and _handle_plot()'s rewrite (piece 3)
            # reads whichever kind this switch is showing at that moment.
            # No separate "pending kind" variable to keep in sync — the
            # switch's own .active value at Plot-press time *is* the
            # pending kind.
            raster_panel, raster_widgets = \
                self._build_raster_config_panel(slot, dark)
            scatter_panel, scatter_widgets = \
                self._build_scatter_config_panel(slot, dark)
            self._panel_axis_widgets[slot.id] = {
                "raster": raster_widgets, "scatter": scatter_widgets,
            }

            kind_switch = RadioButtonGroup(
                labels=["Raster", "Scatter"],
                active=(0 if slot.kind == "raster" else 1),
                width=120,
                stylesheets=[dark],
            )
            self._panel_kind_switch[slot.id] = kind_switch

            raster_panel.visible  = (slot.kind == "raster")
            scatter_panel.visible = (slot.kind == "scatter")
            kind_switch.js_on_change("active", CustomJS(
                args={"raster_panel": raster_panel,
                      "scatter_panel": scatter_panel},
                code="""
// Capture scroll position before toggling — reported: switching
// Raster<->Scatter shifted the visible sidebar content unexpectedly.
// Root cause: nothing here calls scrollTo() directly; the raster panel
// (min/max + extra fields) is taller than the scatter panel, so toggling
// which is .visible changes the sidebar column's total content height.
// When that happens while already scrolled near the bottom, the browser
// clamps scrollTop to the new (smaller) max on its own — an incidental
// side effect of the height change, not an intentional scroll, but it
// looks and feels like one. Explicitly restoring the pre-toggle
// scrollTop afterward cancels that clamp out, so the view only moves
// when the content still extends that far after the change (the
// unavoidable case: if the user was scrolled somewhere the shorter
// content genuinely doesn't reach anymore, the browser will still clamp
// — nothing can prevent that without leaving dead space in the layout).
//
// Revised: a plain document.querySelector('.cv-sidebar') reportedly found
// nothing here either (the original unmitigated clamp behavior showed
// through unchanged, consistent with the guard below silently skipping).
// Same Shadow DOM issue as the gear-click fix — see that comment.
function __cvFindEl(root, selector) {
    const found = root.querySelector(selector);
    if (found) return found;
    const all = root.querySelectorAll('*');
    for (let i = 0; i < all.length; i++) {
        if (all[i].shadowRoot) {
            const inner = __cvFindEl(all[i].shadowRoot, selector);
            if (inner) return inner;
        }
    }
    return null;
}
const sidebarEl = __cvFindEl(document, '.cv-sidebar');
const prevScrollTop    = sidebarEl ? sidebarEl.scrollTop    : null;
const prevScrollHeight = sidebarEl ? sidebarEl.scrollHeight : null;

raster_panel.visible  = (cb_obj.active === 0);
scatter_panel.visible = (cb_obj.active === 1);

if (sidebarEl && prevScrollTop !== null) {
    // Deferred by a tick — same reasoning as the gear-click scroll fix
    // above and the earlier Cancel-button title fix: the DOM hasn't
    // reflowed to reflect the new .visible state yet in this same
    // synchronous tick, so measuring/restoring scroll right now would be
    // fighting a reflow that hasn't happened.
    //
    // Asymmetric on purpose (added after further feedback): restoring the
    // exact prior scrollTop is correct when content *shrinks* (switching
    // to Scatter, fewer fields than Raster) — confirmed working. It's
    // wrong when content *grows* (switching to Raster): restoring the old
    // position leaves whatever newly appeared below it (e.g. min/max)
    // still off-screen, unrevealed. So: scroll to the bottom instead when
    // scrollHeight increased, same idea as the gear-click fix — reveal
    // what just appeared, rather than pin to where the view was before it
    // existed. Compares against the actual new scrollHeight after reflow,
    // not which kind was selected, so this stays correct even if a
    // future panel's relative heights change.
    setTimeout(function() {
        if (sidebarEl.scrollHeight > prevScrollHeight) {
            sidebarEl.scrollTop = sidebarEl.scrollHeight;
        } else {
            sidebarEl.scrollTop = prevScrollTop;
        }
    }, 0);
}
""",
            ))

            swap_btn = Button(
                label       = "Swap",
                button_type = "default",
                width       = 60,
                stylesheets = [dark],
            )
            swap_js_obj = CustomJS(
                args={
                    "display_order_source": self._display_order_source,
                    "slot0_raster_layout":  self._slots[0].raster.layout,
                    "slot0_scatter_layout": self._slots[0].scatter.layout,
                    "slot1_raster_layout":  self._slots[1].raster.layout,
                    "slot1_scatter_layout": self._slots[1].scatter.layout,
                    "side_container": None,   # patched in after _build_plot_area
                    "over_container": None,   # patched in after _build_plot_area
                },
                code=swap_js,
            )
            swap_btn.js_on_click(swap_js_obj)
            # Stored so side_container/over_container can be patched in
            # after _build_plot_area() creates them (same "construct now,
            # patch args in later" pattern already used for layout_js,
            # presets, and the sidebar toggle) — the button itself
            # (self._swap_btns) isn't otherwise needed after construction,
            # but kept too in case something later needs to reference it
            # (e.g. disabling it, should that ever become relevant).
            self._swap_btns.append(swap_btn)
            self._swap_js_objects.append(swap_js_obj)

            tab_panel = TabPanel(
                child=column(
                    # Split into two rows (added 2026-08-03, reported
                    # overflowing the sidebar as one row —
                    # 120+80+60=260px, exactly _SIDEBAR_WIDTH with no
                    # margin left for borders/padding). Grouped by
                    # function: mode selection on its own row, the two
                    # action buttons together below.
                    row(Tip(kind_switch,
                            tooltip=self._tt("Switch this panel between "
                                              "raster and scatter"))),
                    row(Tip(cancel_btn,
                            tooltip=self._tt("Discard changes to this "
                                              "panel and close its tab")),
                        Tip(swap_btn,
                            tooltip=self._tt("Swap this panel's screen "
                                              "position with the other "
                                              "panel — instant, no "
                                              "replot"))),
                    raster_panel,
                    scatter_panel,
                ),
                title=f"Panel {slot.id}",
            )
            self._panel_tabpanels[slot.id] = tab_panel

            # Fixed 2026-07-31: reported as a testing-blocking bug — after
            # switching a slot's kind via Chunk 2, the gear tool vanished
            # entirely rather than just mistargeting. Root cause: gear was
            # only ever added to slot.active.figure, evaluated once at
            # construction — so only whichever figure was active *then*
            # ever got a gear in its toolbar. The other figure (which
            # becomes the visible one after a switch) never had one added
            # at all; there was nothing to retarget, it was just missing.
            #
            # Fixed by adding one gear per KIND per slot (two, not one) —
            # each bound to its own figure, sharing the same tab_panel/
            # orig_source/cancel_btn (only one tab per slot, regardless of
            # which of its two gears opened it). Since only the currently
            # *visible* figure's toolbar is ever actually clickable, this
            # also incidentally resolves the earlier-flagged "gear title
            # targets the wrong (hidden) figure" gap — there is no wrong
            # figure to target anymore, each gear only ever fires from the
            # one it's actually attached to, which is only reachable when
            # visible.
            #
            # Cancel needed a matching fix: it used to bind to a single
            # fixed slot.active.figure too, which would restore the wrong
            # figure's title after a switch. Now determined dynamically at
            # click time from which of the slot's two layout objects is
            # currently .visible (Chunk 2 already keeps this correctly
            # up to date) — reuses existing state rather than adding a new
            # "which figure did the open gear session target" tracker.
            cancel_btn.js_on_click(CustomJS(
                args={
                    "tabs":           self._gear_tabs,
                    "my_panel":       tab_panel,
                    "raster_fig":     slot.raster.figure,
                    "scatter_fig":    slot.scatter.figure,
                    "orig_source":    orig_source,
                },
                # Determines the target figure from orig_source's tracked
                # 'kind' field (written by gear_click_js at capture time)
                # rather than checking layout visibility — same fix and
                # same reasoning as the doPlot() success handler below:
                # precise and direct rather than relying on "the visible
                # figure hasn't changed since gear was clicked" staying
                # true, which is more fragile to depend on implicitly.
                code="const fig = (orig_source.data['kind'][0] === 'scatter') "
                     "? scatter_fig : raster_fig;\n" + cancel_click_js,
            ))

            for kind, kind_panel in (("raster", slot.raster), ("scatter", slot.scatter)):
                gear = CustomAction(
                    icon=_GEAR_ICON_DATA_URI,
                    description=f"Configure Panel {slot.id}",
                    callback=CustomJS(
                        args={
                            "sidebar":     self._sidebar_col,
                            "toggle_btn":  toggle_btn,
                            "tabs":        self._gear_tabs,
                            "my_panel":    tab_panel,
                            "panel_label": f"Panel {slot.id}",
                            "fig":         kind_panel.figure,
                            "kind":        kind,
                            "orig_source": orig_source,
                            "edit_color":  _EDIT_TITLE_COLOR,
                        },
                        code=gear_click_js,
                    ),
                )
                kind_panel.figure.add_tools(gear)

        return self._sidebar_col, toggle_btn

    # ---------------------------------------------------------------------- #
    # Toolbar                                                                  #
    # ---------------------------------------------------------------------- #

    def _build_toolbar(self, sidebar_toggle_btn):
        """Build the toolbar row.

        Parameters
        ----------
        sidebar_toggle_btn : Button
            The ⟨/⟩ collapse button returned by ``_build_sidebar``.
        """
        ctrl        = self._pipe["control"]
        ids         = self._ids
        # raster_fig/scatter_fig locals removed (Group 3 piece 3, Chunk 2,
        # 2026-07-31) — their only use was the fixed r_fig/s_fig args in
        # _plot_js_args, replaced by per-slot, per-kind fig args since
        # either slot can now show either kind.

        # Group 3 piece 3, Chunk 2 follow-on fix (added 2026-08-02): the
        # slot currently in each screen position, used below by layout_js
        # and preset_js to reference both kinds' fig/layout objects
        # rather than the single fixed self._pos0.figure/.layout — those
        # are captured once at construction and go stale the instant a
        # kind switch actually happens (same root cause as the four bugs
        # fixed the same day in doPlot()'s response handler).
        _pos0_slot = self._slots[self._slot_display_order[0]]
        _pos1_slot = self._slots[self._slot_display_order[1]]

        side_w   = _PANEL_WIDTH_SIDE
        full_w   = _PANEL_WIDTH_FULL
        panel_h  = _PANEL_HEIGHT
        over_h   = _PANEL_HEIGHT_OVER

        # ---- Plot ▶ and Reload ↺ ----------------------------------------- #
        plot_btn   = Button(label="Plot ▶",   button_type="success", width=80)
        reload_btn = Button(label="Reload ↺", button_type="default", width=80)

        # Shared plot-send logic used by Plot ▶, Reload ↺, and all presets.
        _do_plot_js = """
// Shared by both the early client-side conflict guard inside doPlot()
// below and the server-error response handling further down (added
// 2026-08-03, factored out once it became clear both needed the exact
// same "open + focus this tab, expanding the sidebar if needed" logic —
// see the two call sites for why each one exists). Deliberately does
// NOT touch gear_tabs.tabs' other entries or hide/reset anything —
// whatever else is open stays open, exactly as decision 9 called for.
function switchToTab(target_tab, gear_tabs, sidebar, toggle_btn) {
    if (target_tab == null) return;
    const idx = gear_tabs.tabs.indexOf(target_tab);
    if (idx === -1) {
        gear_tabs.tabs = gear_tabs.tabs.concat([target_tab]);
        gear_tabs.active = gear_tabs.tabs.length - 1;
    } else {
        gear_tabs.active = idx;
    }
    gear_tabs.visible = true;
    if (!sidebar.visible) {
        sidebar.visible  = true;
        toggle_btn.label = '⟨';
    }
}

function doPlot(reload) {
    // Group 3 piece 3 / Chunk 1 (added 2026-07-31): request/response are
    // now per-slot (panels: {id: {...}}) instead of flat raster_y/
    // raster_x/scatter_x/scatter_y globals. See decision 9's "Group 3
    // piece 3, Chunk 1" note in visplot-grid-iteration-notes.md for the
    // full design record. panel0/panel1 below correspond to self._slots[0]
    // (still always raster in this chunk) and self._slots[1] (still
    // always scatter) — kind is still read from each slot's own switch
    // and sent honestly, but _handle_plot() rejects an actual mismatch
    // rather than this chunk attempting to render one (that's Chunk 2).
    function buildPanelPayload(kind_switch, ry_sel, rx_sel, rq_sel, sx_sel, sy_sel) {
        if (kind_switch.active === 0) {
            return {kind: 'raster', y: ry_sel.value, x: rx_sel.value, qty: rq_sel.value};
        } else {
            return {kind: 'scatter', x: sx_sel.value, y: sy_sel.value};
        }
    }
    function rasterConflict(kind_switch, ry_sel, rx_sel) {
        return kind_switch.active === 0 && ry_sel.value === rx_sel.value;
    }

    // Refuse to send rather than let the server round-trip reject it —
    // same enforcement point as before (Plot ▶, Reload ↺, every preset
    // all funnel through here). Each raster panel's own live conflict_div
    // (Group 3 piece 1) already shows the warning as soon as the conflict
    // appears — but only where it's visible, i.e. on that panel's own
    // tab. Reported gap (found 2026-08-03): if the *other* tab is the
    // one currently open, pressing Plot here used to silently do
    // nothing — correctly refusing to send, but with no visible sign of
    // why, and no way to discover it without guessing which tab to check.
    // This never went through _handle_plot() at all (the request is
    // refused right here, before ctrl.send()), so the server-side
    // failed_slot/switchToTab mechanism below never got a chance to run
    // for this specific error — same underlying UX problem as that
    // feature was built for, just reached via a different, client-only
    // path. Fixed by calling the same switchToTab() helper here too.
    if (rasterConflict(panel0_kind_switch, panel0_ry_sel, panel0_rx_sel)) {
        switchToTab(panel_a_tab, gear_tabs, sidebar, toggle_btn);
        return;
    }
    if (rasterConflict(panel1_kind_switch, panel1_ry_sel, panel1_rx_sel)) {
        switchToTab(panel_b_tab, gear_tabs, sidebar, toggle_btn);
        return;
    }

    const corr = corr_cbg.labels.filter((_, i) => corr_cbg.active.includes(i));
    const panels = {};
    panels[panel0_id] = buildPanelPayload(
        panel0_kind_switch, panel0_ry_sel, panel0_rx_sel, panel0_rq_sel,
        panel0_sx_sel, panel0_sy_sel);
    panels[panel1_id] = buildPanelPayload(
        panel1_kind_switch, panel1_ry_sel, panel1_rx_sel, panel1_rq_sel,
        panel1_sx_sel, panel1_sy_sel);

    console.log('[visplot doPlot] sending panels:', JSON.parse(JSON.stringify(panels)));

    // Identities as a LIST, in the source's own types.  Joining them
    // into a string only for Python to split it again is a round trip a
    // spectral-window name containing a comma would break -- and the
    // identity is deliberately opaque (int or name), so text is the
    // wrong carrier for it.
    const _spw_idx = spw_src.selected.indices || [];
    const spw_ids = _spw_idx.map(i => spw_src.data['ident'][i]);
    if (spw_src.data['ident'].length && spw_ids.length === 0) {
        // Empty is a real state, not a synonym for "all" -- which is
        // what makes the All and None buttons distinct.  Refusing here
        // costs nothing because Plot is an explicit commit: unchecking
        // everything is a transient step on the way to checking two.
        if (notify_div)
            notify_div.text = "<b>Select at least one spectral window.</b>";
        return;
    }

    ctrl.send(ids['plot'], {
        field:       field_sel.value,
        spw_ids:     spw_ids,
        correlation: corr.join(','),
        datacolumn:  col_sel.value,
        panels:      panels,
        reload:      !!reload,
    }, function(resp) {
        if (!resp) return;
        console.log('[visplot doPlot] received status:', resp.status,
                     'panels:', resp.panels ? JSON.parse(JSON.stringify(resp.panels)) : resp.panels);
        if (resp.status_text && status_div)
            status_div.text = resp.status_text;
        // No Bokeh server — resp.notify_text must be applied explicitly
        // the same way FlagTool's own comm.send() callback does, or a
        // Python-side self._notify("") clear never reaches the browser.
        if (resp.notify_text != null && notify_div) {
            notify_div.text = resp.notify_text;
            if (resp.notify_color != null) {
                notify_div.styles = {...notify_div.styles, color: resp.notify_color};
            }
        }

        const p0 = resp.panels ? resp.panels[panel0_id] : null;
        const p1 = resp.panels ? resp.panels[panel1_id] : null;

        // Group 3 piece 3, Chunk 2 (added 2026-07-31): pick the correct
        // kind-specific fig/img_src/state/layout for each slot at
        // runtime, from resp.panels[id].kind (what _handle_plot() says
        // actually rendered this round) — not a construction-time-fixed
        // binding, since either slot can now show either kind. Falls
        // back to the raster set if p0/p1 is null (nothing to update
        // regardless) purely so the destructuring below doesn't throw;
        // no fields get applied in that case since every read below is
        // already guarded on p0/p1 being present.
        const p0_kind  = (p0 && p0.kind === 'scatter') ? 'scatter' : 'raster';
        const p0_fig   = (p0_kind === 'raster') ? panel0_raster_fig   : panel0_scatter_fig;
        const p0_img   = (p0_kind === 'raster') ? panel0_raster_img_src : panel0_scatter_img_src;
        const p0_state = (p0_kind === 'raster') ? panel0_raster_state : panel0_scatter_state;

        const p1_kind  = (p1 && p1.kind === 'scatter') ? 'scatter' : 'raster';
        const p1_fig   = (p1_kind === 'raster') ? panel1_raster_fig   : panel1_scatter_fig;
        const p1_img   = (p1_kind === 'raster') ? panel1_raster_img_src : panel1_scatter_img_src;
        const p1_state = (p1_kind === 'raster') ? panel1_raster_state : panel1_scatter_state;

        // Same story for _state_source (full_x0/agg_n_x/...) — without
        // this, FlagTool keeps computing its 1:1 zoom target from
        // whatever full_x0/agg_n_x were current as of the last time this
        // ran, silently stale after any axis change.
        if (p0 && p0.state != null) { p0_state.data = p0.state; }
        if (p1 && p1.state != null) { p1_state.data = p1.state; }

        // Update panel 0's figure + axes — whichever kind actually
        // rendered this round (p0_fig/p0_img), not a fixed one.
        try {
            if (p0 && p0.image != null) {
                p0_img.data['image'] = [p0.image];
            }
            if (p0 && p0.x0 != null) {
                p0_img.data['x']  = [p0.x0];
                p0_img.data['y']  = [p0.y0];
                p0_img.data['dw'] = [p0.x1 - p0.x0];
                p0_img.data['dh'] = [p0.y1 - p0.y0];
                p0_fig.x_range.start = p0.x0; p0_fig.x_range.end = p0.x1;
                p0_fig.y_range.start = p0.y0; p0_fig.y_range.end = p0.y1;
                p0_fig.x_range.reset_start = p0.x0; p0_fig.x_range.reset_end = p0.x1;
                p0_fig.y_range.reset_start = p0.y0; p0_fig.y_range.reset_end = p0.y1;
            }
            // Single emit *after* image and x/y/dw/dh are both settled — a
            // ColumnDataSource.data mutation is a plain dict write and does
            // not itself notify the renderer (no Bokeh server here to sync
            // that automatically), so emitting between the two blocks above
            // redrew the glyph with the new image but the still-stale
            // x/y/dw/dh box from the previous axes, positioning the correct
            // pixels outside the new viewport (all black) even though
            // p0_fig's ranges/labels/title (driven by their own property
            // setters, not this CDS) updated correctly. Always emit here —
            // panel 0's image is always sent, even when axes are
            // unchanged, to keep the hover renderer active.
            if (p0 && p0.image != null) {
                p0_img.change.emit();
            }
            if (p0 && p0.x_label != null) p0_fig.below[0].axis_label = p0.x_label;
            if (p0 && p0.y_label != null) p0_fig.left[0].axis_label  = p0.y_label;
            if (p0 && p0.title   != null) p0_fig.title.text           = p0.title;

            // Reveal whichever of this slot's two layout objects matches
            // what actually rendered, hide the other — the visibility-
            // toggling mechanism _build_plot_area() sets up (both are
            // already children of the container; a hidden LayoutDOM
            // child takes no space, same as "One" mode's pos1 already
            // relied on before this chunk). Only touched when a panel
            // response actually arrived, so a request that only updated
            // the other slot doesn't needlessly re-toggle this one.
            if (p0) {
                panel0_raster_layout.visible  = (p0_kind === 'raster');
                panel0_scatter_layout.visible = (p0_kind === 'scatter');
            }
        } catch(e) { console.warn('panel 0 update failed:', e); }

        // Update panel 1's figure + axes — same pattern as panel 0 above.
        //
        // Bug fixed 2026-08-03 (found during testing): this block used to
        // combine the always-sent image update with the conditionally-
        // sent range update into a single "if (p1.image != null)" check —
        // unlike panel 0's block above, which correctly splits them into
        // two separate conditionals. Since p1.image is *always* sent
        // (even when axes didn't change, to keep the hover renderer
        // active), that combined condition was always true — meaning it
        // always tried to set p1_img.data['x'] = [p1.x0] etc even when
        // p1.x0/x1/y0/y1 were null (axes unchanged this round), corrupting
        // the image's position data to NaN and making it disappear. This
        // was a latent bug since Chunk 2: scatter previously always
        // re-rendered unconditionally, so x0 was always non-null and this
        // combined condition was never exercised with a null value —
        // only exposed once scatter's own change-detection (added the
        // same day, see self._last_scatter_selection_by_slot) started
        // correctly sending null range fields for an unchanged scatter
        // panel. Fixed by splitting into the same two-conditional
        // structure panel 0 already had correct.
        try {
            if (p1 && p1.image != null) {
                p1_img.data['image'] = [p1.image];
            }
            if (p1 && p1.x0 != null) {
                p1_img.data['x']  = [p1.x0];
                p1_img.data['y']  = [p1.y0];
                p1_img.data['dw'] = [p1.x1 - p1.x0];
                p1_img.data['dh'] = [p1.y1 - p1.y0];
                p1_fig.x_range.start = p1.x0; p1_fig.x_range.end = p1.x1;
                p1_fig.y_range.start = p1.y0; p1_fig.y_range.end = p1.y1;
                // Update reset bounds so the ResetTool returns to new data extents
                p1_fig.x_range.reset_start = p1.x0; p1_fig.x_range.reset_end = p1.x1;
                p1_fig.y_range.reset_start = p1.y0; p1_fig.y_range.reset_end = p1.y1;
            }
            // Same reasoning as panel 0's emit above: always emit when
            // image is sent, even if axes/range didn't change, to keep
            // the hover renderer active.
            if (p1 && p1.image != null) {
                p1_img.change.emit();
            }
            if (p1 && p1.x_label != null) p1_fig.below[0].axis_label = p1.x_label;
            if (p1 && p1.y_label != null) p1_fig.left[0].axis_label  = p1.y_label;
            if (p1 && p1.title   != null) p1_fig.title.text           = p1.title;

            if (p1) {
                panel1_raster_layout.visible  = (p1_kind === 'raster');
                panel1_scatter_layout.visible = (p1_kind === 'scatter');
            }
        } catch(e) { console.warn('panel 1 update failed:', e); }

        // Gear/Tabs: hide + fully reset on success (added 2026-07-31,
        // revised same day for the full-title-replacement version).
        // resp.status already came back from _handle_plot() as 'ok' or
        // 'error' — previously unused here.
        //
        // Text used to need no manual handling here, on the assumption
        // that p0.title/p1.title above always superseded whatever plain
        // "Panel A"/"Panel B" label a gear click had put there. That
        // assumption was wrong (bug found during testing, fixed
        // 2026-08-02): p0.title/p1.title are only sent when
        // _handle_plot() decides that panel's axes actually changed —
        // if a gear tab was open but nothing about that panel's own axes
        // changed before Plot was pressed, no fresh title arrives, and
        // text stays stuck on the placeholder even though color gets
        // unconditionally restored below. Now falls back to
        // panel_a_state/panel_b_state's captured original text whenever
        // a fresh title wasn't actually applied to the edited figure
        // this round — see the fuller comment at the fallback check
        // itself, below, for the exact condition (also covers the
        // related kind-switch-mid-session case). text_color still needs
        // the same explicit restore it always did: the gear click set it
        // to _EDIT_TITLE_COLOR and nothing else ever touches it,
        // so on success it must be explicitly restored from that slot's
        // own captured pre-edit color (self._panel_title_state[slot.id],
        // same source Cancel reads from) — but only for a slot that's
        // actually currently open (tabs.tabs membership), so a slot the
        // user never touched this round doesn't get its color needlessly
        // reset.
        //
        // Fixed 2026-08-02 (bug found during testing, not previously
        // reported): this used to target p0_fig/p1_fig — whichever kind
        // the response says actually rendered this round. That's wrong
        // if the user changed the Raster/Scatter switch *within* the
        // same open tab before pressing Plot: the figure that was
        // actually reddened is whichever the gear was originally clicked
        // on, which can differ from what's active after the switch takes
        // effect. Now reads panel_a_state/panel_b_state's tracked 'kind'
        // field (written by gear_click_js at capture time) to pick the
        // correct figure directly, independent of what this round's
        // response says rendered.
        //
        // Resetting tabs.tabs to [] (not just tabs.visible = false)
        // matters so the *next* gear click starts from "nothing open"
        // again rather than instantly re-revealing whatever was open last
        // round.
        if (resp.status === 'ok') {
            if (gear_tabs.tabs.indexOf(panel_a_tab) !== -1) {
                const edited_kind = panel_a_state.data['kind'][0];
                const edited_fig  = (edited_kind === 'scatter')
                    ? panel0_scatter_fig : panel0_raster_fig;
                if (edited_fig.title != null) {
                    // Fixed 2026-08-02 (bug found during testing): text
                    // was only ever restored as a side effect of the
                    // unconditional "if (p0.title != null) p0_fig.title.text
                    // = p0.title" line above — which only fires when
                    // _handle_plot() actually sent a fresh title, i.e.
                    // when this panel's own axes genuinely changed. If
                    // the gear tab was open but nothing about that
                    // panel's axes actually changed before Plot was
                    // pressed, no fresh title arrives — color still gets
                    // unconditionally restored below, but text was left
                    // stuck on the gear's placeholder ("Panel A") with
                    // nothing to replace it. Also covers the kind-switch
                    // case: if p0_kind (what actually rendered this
                    // round) differs from edited_kind (what was actually
                    // gear-clicked), the response describes a different
                    // figure entirely, so p0.title never applied to
                    // edited_fig regardless of whether it's non-null.
                    // Falls back to orig_source's captured original text
                    // in either case — correct because if nothing
                    // changed, the pre-edit title is still accurate.
                    const p0_title_applied = (p0 && p0.title != null && p0_kind === edited_kind);
                    if (!p0_title_applied) {
                        edited_fig.title.text = panel_a_state.data['text'][0];
                    }
                    edited_fig.title.text_color = panel_a_state.data['color'][0];
                    edited_fig.title.change.emit();
                }
            }
            if (gear_tabs.tabs.indexOf(panel_b_tab) !== -1) {
                const edited_kind = panel_b_state.data['kind'][0];
                const edited_fig  = (edited_kind === 'scatter')
                    ? panel1_scatter_fig : panel1_raster_fig;
                if (edited_fig.title != null) {
                    // Same fix as panel A above.
                    const p1_title_applied = (p1 && p1.title != null && p1_kind === edited_kind);
                    if (!p1_title_applied) {
                        edited_fig.title.text = panel_b_state.data['text'][0];
                    }
                    edited_fig.title.text_color = panel_b_state.data['color'][0];
                    edited_fig.title.change.emit();
                }
            }
            gear_tabs.visible = false;
            gear_tabs.tabs    = [];
        } else if (resp.status === 'error' && resp.failed_slot != null) {
            // Validation-error auto-switch-to-tab (added 2026-08-03) —
            // decision 9's originally-settled design, last piece built.
            // resp.failed_slot (tagged by every _handle_plot() error
            // path — the kind-mismatch guard, the raster Y/X conflict
            // check, and both kinds' exception handlers) tells us which
            // slot actually caused the failure. Deliberately does NOT
            // hide/reset gear_tabs the way the success branch does — the
            // open tabs stay open, exactly as decision 9 called for;
            // switchToTab() only makes sure the right one is in front.
            //
            // In practice this branch only fires for errors that
            // actually reach _handle_plot() — the raster Y/X conflict is
            // caught earlier, client-side, before any request is even
            // sent (see doPlot()'s own guard above, which calls
            // switchToTab() directly for that case) — but the
            // kind-mismatch guard and both kinds' exception handlers
            // still route through here.
            const failed_tab = (resp.failed_slot === panel0_id) ? panel_a_tab
                              : (resp.failed_slot === panel1_id) ? panel_b_tab
                              : null;
            switchToTab(failed_tab, gear_tabs, sidebar, toggle_btn);
        }
    });
}
"""
        _plot_js_args = {
            "ctrl":       ctrl,
            "ids":        ids,
            "field_sel":  self._field_select,
            "spw_src":    self._spw_source,
            "corr_cbg":   self._corr_cbg,
            "col_sel":    self._col_select,
            "status_div": self._status_div,
            "notify_div": self._notify_div,
            "gear_tabs":  self._gear_tabs,
            # Needed for validation-error auto-switch-to-tab (added
            # 2026-08-03): if the sidebar was collapsed when a failure
            # happens, gear_tabs.visible=true alone wouldn't be enough to
            # actually show it — the whole sidebar (gear_tabs' parent)
            # needs expanding too, same as gear_click_js already does.
            "sidebar":    self._sidebar_col,
            "toggle_btn": sidebar_toggle_btn,
            # Positional shim (same scope noted in _build_sidebar): slot A
            # is currently "the raster position", slot B "the scatter
            # position", per self._slots' construction order.
            "panel_a_tab":   self._panel_tabpanels[self._slots[0].id],
            "panel_b_tab":   self._panel_tabpanels[self._slots[1].id],
            "panel_a_state": self._panel_title_state[self._slots[0].id],
            "panel_b_state": self._panel_title_state[self._slots[1].id],
            # Group 3 piece 3 / Chunk 1 (added 2026-07-31): per-slot
            # request-building args, replacing the old flat ry_sel/
            # rx_sel/rq_sel/sx_sel/sy_sel/raster_axis_conflict_msg args
            # (removed above — no longer read by doPlot()). panel0/panel1
            # map to self._slots[0]/self._slots[1] — same positional
            # convention as panel_a_tab/panel_b_tab above, just named for
            # request-building rather than display-state. Each includes
            # both kinds' widgets (not just the currently-active one)
            # since the switch could be on either — doPlot() reads
            # whichever the switch currently shows.
            "panel0_id":          self._slots[0].id,
            "panel0_kind_switch": self._panel_kind_switch[self._slots[0].id],
            "panel0_ry_sel": self._panel_axis_widgets[self._slots[0].id]["raster"]["y_sel"],
            "panel0_rx_sel": self._panel_axis_widgets[self._slots[0].id]["raster"]["x_sel"],
            "panel0_rq_sel": self._panel_axis_widgets[self._slots[0].id]["raster"]["q_sel"],
            "panel0_sx_sel": self._panel_axis_widgets[self._slots[0].id]["scatter"]["x_sel"],
            "panel0_sy_sel": self._panel_axis_widgets[self._slots[0].id]["scatter"]["y_sel"],
            "panel1_id":          self._slots[1].id,
            "panel1_kind_switch": self._panel_kind_switch[self._slots[1].id],
            "panel1_ry_sel": self._panel_axis_widgets[self._slots[1].id]["raster"]["y_sel"],
            "panel1_rx_sel": self._panel_axis_widgets[self._slots[1].id]["raster"]["x_sel"],
            "panel1_rq_sel": self._panel_axis_widgets[self._slots[1].id]["raster"]["q_sel"],
            "panel1_sx_sel": self._panel_axis_widgets[self._slots[1].id]["scatter"]["x_sel"],
            "panel1_sy_sel": self._panel_axis_widgets[self._slots[1].id]["scatter"]["y_sel"],
            # Group 3 piece 3, Chunk 2 (added 2026-07-31): both kinds'
            # figure/image-source/state-source/layout per slot, replacing
            # the fixed r_fig/s_fig/r_img_src/s_img_src/r_state/s_state
            # args above — those assumed slot 0 is always raster and slot
            # 1 always scatter, which Chunk 2 finally allows to not be
            # true. doPlot()'s response handler picks the right one per
            # slot at runtime from resp.panels[id].kind (what actually
            # rendered), not from a construction-time-fixed binding.
            "panel0_raster_fig":      self._slots[0].raster.figure,
            "panel0_raster_img_src":  self._slots[0].raster._image_source,
            "panel0_raster_state":    self._slots[0].raster._state_source,
            "panel0_raster_layout":   self._slots[0].raster.layout,
            "panel0_scatter_fig":     self._slots[0].scatter.figure,
            "panel0_scatter_img_src": self._slots[0].scatter._image_source,
            "panel0_scatter_state":   self._slots[0].scatter._state_source,
            "panel0_scatter_layout":  self._slots[0].scatter.layout,
            "panel1_raster_fig":      self._slots[1].raster.figure,
            "panel1_raster_img_src":  self._slots[1].raster._image_source,
            "panel1_raster_state":    self._slots[1].raster._state_source,
            "panel1_raster_layout":   self._slots[1].raster.layout,
            "panel1_scatter_fig":     self._slots[1].scatter.figure,
            "panel1_scatter_img_src": self._slots[1].scatter._image_source,
            "panel1_scatter_state":   self._slots[1].scatter._state_source,
            "panel1_scatter_layout":  self._slots[1].scatter.layout,
        }

        plot_js = CustomJS(
            args=_plot_js_args,
            code=_do_plot_js + "doPlot(false);",
        )
        reload_js = CustomJS(
            args=_plot_js_args,
            code=_do_plot_js + "doPlot(true);",
        )
        plot_btn.js_on_click(plot_js)
        reload_btn.js_on_click(reload_js)

        # Store for use in _preset_js callbacks
        self._do_plot_js   = _do_plot_js
        self._plot_js_args = _plot_js_args

        # ---- Iteration: Prev/Next, wired to the sidebar's own Field/SPW
        # controls (I-1, Phase 2.5) ------------------------------------- #
        #
        # Placed here, after self._do_plot_js/_plot_js_args exist, for the
        # same reason _preset_js below is: Prev/Next reuse doPlot() itself
        # rather than duplicating the send logic a fourth time (kickoff
        # §3). The mechanism: mutate field_sel.value (Field) or
        # spw_src.selected.indices (SPW) to the stepped value, then call
        # the exact same doPlot(false) Plot ▶ uses. doPlot() always reads
        # field_sel.value and spw_src.selected.indices at send time
        # regardless of which one changed, so the *other*, unmutated axis
        # is resent unchanged -- the "omit the non-animated axis's key"
        # behaviour the kickoff describes falls out of reusing doPlot()
        # as-is, with no new "hold fixed" flag or partial-message shape
        # needed on either side (kickoff §3's "don't invent a parallel
        # one").
        #
        # Wraps at the ends (does not clamp) -- the design choice the
        # kickoff asked to be made and stated explicitly (§1). Index
        # arithmetic lives in iteration_step.py, shared with Python via a
        # golden-table parity test (test_iteration_step.py) rather than
        # written inline here a second time -- see that module's
        # docstring for why a Python copy exists even though only the JS
        # copy is on this feature's critical path.
        #
        # Two revisions since I-1's first cut, both from live-MS testing
        # feedback, documented in the implementation plan's Phase 2.5
        # section:
        #
        # 1. A single toolbar "Animate: <axis>" selector (first a
        #    RadioButtonGroup, briefly a Select) chose which of
        #    field_sel/spw_src Prev/Next acted on. Dropped entirely: the
        #    buttons now live beside field_row / the SPW section heading
        #    in the *sidebar* (self._field_prev_btn/_next_btn,
        #    self._spw_prev_btn/_next_btn -- constructed in
        #    _build_sidebar(), wired here once self._do_plot_js exists,
        #    the same split every other sidebar-reaching toolbar control
        #    already uses). Which widget a press acts on is no longer a
        #    mode to track or display anywhere -- it's simply whichever
        #    pair was clicked -- and the toolbar's iteration footprint is
        #    now zero rather than growing with every future axis (I-2
        #    Polarization, I-3 Antenna/Baseline/Scan/Time each just need
        #    their own pair beside their own sidebar control, exactly
        #    this shape, with no toolbar change at all). This also
        #    resolves the toolbar-width concern the Select revision only
        #    partially addressed.
        # 2. Per-axis Prev/Next are now disabled at construction (see
        #    _build_sidebar()) when that axis has ≤1 item, rather than
        #    only guarded at click time -- a real single-SPW MS (I-1's
        #    live testing) made "visibly disabled" clearly better than
        #    "silently does nothing when clicked".
        #
        # Consistency across panels (raised in live-MS testing): Field/
        # SPW are not per-panel state. _handle_plot() builds exactly one
        # SelectionSpec per request (self._selection = self._build_
        # selection(), ~L2514) and assigns the *same object* to every
        # panel (self._all_panels) before either panel re-renders — so a
        # Prev/Next press is structurally guaranteed to move both panels
        # together, raster and scatter alike, regardless of which kind
        # is active in which slot. There is no code path where one panel
        # could see a field/SPW change and the other not.
        _iterate_field_js = STEP_INDEX_JS + """
function doIterateField(delta) {
    // options[0] is the ("", "All fields") sentinel -- not itself a
    // steppable position, same exclusion _field_iteration_position()
    // applies on the Python side (status-bar readout) and meta.fields
    // applies for the count iteration_step.step_index() receives.
    const names = field_sel.options.slice(1).map(o => o[0]);""" + \
    _iter_guard_js("names.length", "field") + """
    const cur = names.indexOf(field_sel.value);
    const idx = stepIterationIndex(cur === -1 ? null : cur, names.length, delta, true);
    if (idx === null) return;
    field_sel.value = names[idx];
}
"""
        _iterate_spw_js = STEP_INDEX_JS + """
function doIterateSpw(delta) {
    // SPW is a DataTable (row-selection), not a dropdown -- see
    // _parse_spw_string's docstring for why (non-contiguous ids / bare
    // names). spw_src.data['ident'] is the full ordered meta.spws list,
    // already on the client with nothing further to fetch. Stepping
    // always narrows to exactly one selected row -- the same mechanism
    // a manual single-SPW pick already uses correctly (kickoff §3) --
    // even if multiple rows happen to be checked when Prev/Next is
    // pressed; the lowest checked index is used as the "current"
    // reference point in that case.
    const idents = spw_src.data['ident'] || [];""" + \
    _iter_guard_js("idents.length", "spectral window") + """
    const sel = spw_src.selected.indices || [];
    const cur = sel.length ? Math.min.apply(null, sel) : null;
    const idx = stepIterationIndex(cur, idents.length, delta, true);
    if (idx === null) return;
    spw_src.selected.indices = [idx];
}
"""
        # .wire() (see _IterButtons) assembles each button's CustomJS
        # from these bodies + self._do_plot_js exactly the way the
        # hand-written js_on_click() calls this replaced did -- the
        # only thing that moved is who writes the boilerplate.
        self._field_iter.wire(self._plot_js_args, self._do_plot_js,
                              _iterate_field_js, "doIterateField")
        self._spw_iter.wire(self._plot_js_args, self._do_plot_js,
                            _iterate_spw_js, "doIterateSpw")

        # Rule 3 (§3.1c of the plan): superseded by the sidebar move.
        # I-1's toolbar-resident controls (RadioButtonGroup, then
        # Select) correctly concluded no _THEME_RESTYLE_JS entry was
        # needed, because the *toolbar* area is never restyled either
        # way — Button/RadioButtonGroup there rely on Bokeh's own
        # button_type coloring and that was already fine in both
        # themes. That precedent stopped applying the moment these four
        # buttons moved into the *sidebar*: unthemed default Buttons
        # sitting directly beside themed Select/DataTable widgets is
        # exactly the light/dark mismatch reported from a real screen
        # (buttons staying pale in dark mode). Fixed at construction —
        # see field_prev_btn/field_next_btn/spw_prev_btn/spw_next_btn
        # above, each `stylesheets=[dark, self._icon_btn_css]` — and
        # all four are in self._theme_restyle_args["widgets"] below, so
        # stylesheets[0] (colour) follows the toggle exactly like every
        # other themed sidebar widget; stylesheets[1] (_ICON_BTN_CSS,
        # sizing only) is deliberately never touched by that loop — see
        # _ICON_BTN_CSS's own comment for why.


        # ---- Layout (unified — replaces the former separate mode +
        # layout toggles as of July 29 2026) -------------------------------- #
        # "One" reuses side_container (pos0_layout visible, pos1_layout
        # hidden within it) rather than a third container — a hidden
        # LayoutDOM child in a Bokeh row/column is removed from the render
        # flow, so no extra widget is needed for the single-panel case.
        layout_rbg = RadioButtonGroup(
            labels = ["One", "Side by Side", "Over / Under"],
            active = {"one": 0, "side": 1, "over": 2}.get(self._layout, 1),
            width  = 320,
        )
        self._layout_rbg = layout_rbg

        # --- Export PNG ------------------------------------------------ #
        export_btn = Button(label="Export PNG", button_type="default",
                            width=104)
        export_btn.js_on_click(CustomJS(
            args={
                "ctrl": ctrl,
                "ids":  ids,
                "slot0_raster_fig":     self._slots[0].raster.figure,
                "slot0_raster_layout":  self._slots[0].raster.layout,
                "slot0_scatter_fig":    self._slots[0].scatter.figure,
                "slot0_scatter_layout": self._slots[0].scatter.layout,
                "slot1_raster_fig":     self._slots[1].raster.figure,
                "slot1_raster_layout":  self._slots[1].raster.layout,
                "slot1_scatter_fig":    self._slots[1].scatter.figure,
                "slot1_scatter_layout": self._slots[1].scatter.layout,
                "display_order_source": self._display_order_source,
                "layout_rbg":  layout_rbg,
                "notify_div":  self._notify_div,
                # Index of each (slot, kind) panel in _all_panels, so the
                # message can name panels the way the handler indexes them.
                "panel_index": {
                    "0raster":  self._all_panels.index(self._slots[0].raster),
                    "0scatter": self._all_panels.index(self._slots[0].scatter),
                    "1raster":  self._all_panels.index(self._slots[1].raster),
                    "1scatter": self._all_panels.index(self._slots[1].scatter),
                },
            },
            code="""
// Collect the state only the browser has.  With no Bokeh server the
// figures' ranges, the layout radio and the panel order are all
// CustomJS-mutated and never reach Python, so an export driven purely
// from Python-side state would render the *unzoomed* full extent in the
// default layout.  Everything else -- axes, selection, scaling,
// palettes, cached aggregations -- Python already holds and is
// deliberately not sent.
const order = (display_order_source.data['order'] || [[0, 1]])[0];
const kinds = [
    slot0_raster_layout.visible ? 'raster' : 'scatter',
    slot1_raster_layout.visible ? 'raster' : 'scatter',
];
const figs = [
    kinds[0] === 'raster' ? slot0_raster_fig : slot0_scatter_fig,
    kinds[1] === 'raster' ? slot1_raster_fig : slot1_scatter_fig,
];
const shown = [
    slot0_raster_layout.visible || slot0_scatter_layout.visible,
    slot1_raster_layout.visible || slot1_scatter_layout.visible,
];

// Display order, so the exported grid matches what the user sees after
// a Swap rather than slot order.
const panels = [];
for (let k = 0; k < order.length; k++) {
    const slot = order[k];
    if (!shown[slot]) continue;               // "One" mode hides a slot
    const f = figs[slot];
    panels.push({
        panel: panel_index[String(slot) + kinds[slot]],
        viewport: [f.x_range.start, f.x_range.end,
                   f.y_range.start, f.y_range.end],
        // On-screen size.  The layout JS resizes the figures for One /
        // Side / Over-Under (full_w, side_w, panel_h, over_h) and those
        // assignments never reach Python, so without this the export
        // uses the construction-time canvas and a wide One-mode panel
        // comes out square.
        size: [f.inner_width || f.width, f.inner_height || f.height],
    });
}
if (panels.length === 0) { return; }

// Translate the layout vocabulary here, so no layout string crosses the
// wire -- the compositor takes only (nrows, ncols).
const active = layout_rbg.active;
let nrows = 1, ncols = panels.length;
if (panels.length > 1 && active === 2) { nrows = 2; ncols = 1; }

notify_div.text = "<i>Exporting…</i>";
ctrl.send(ids['export'], {panels: panels, nrows: nrows, ncols: ncols},
    function(resp) {
        if (!resp) { notify_div.text = ""; return; }
        if (resp.status !== 'ok') {
            notify_div.text = "<b>Export failed:</b> " +
                              (resp.message || 'unknown error');
            return;
        }
        // Report the absolute server-side path: there is no save dialog
        // without a Bokeh server, and in JupyterLab-over-SSH the file
        // lands on a different machine from the browser.
        notify_div.text = "Wrote <code>" + resp.path + "</code>";
    });
"""))
        self._export_btn = export_btn


        layout_js = CustomJS(
            args={
                # Swap follow-on fix (added 2026-08-03): all four
                # slot-kind fig/layout objects plus the live display-order
                # tracker, replacing the construction-time-fixed
                # _pos0_slot/_pos1_slot-derived args (correct only until
                # the swap feature — added the same day — could actually
                # move either slot into either position at runtime).
                # Mirrors the kind-resolution fix from 2026-08-02 exactly
                # (determine at click time, don't assume from
                # construction) — this closes the equivalent gap for
                # *position* instead of *kind*, the "One" mode sizing
                # issue flagged when the swap was first discussed.
                "slot0_raster_fig":     self._slots[0].raster.figure,
                "slot0_raster_layout":  self._slots[0].raster.layout,
                "slot0_scatter_fig":    self._slots[0].scatter.figure,
                "slot0_scatter_layout": self._slots[0].scatter.layout,
                "slot1_raster_fig":     self._slots[1].raster.figure,
                "slot1_raster_layout":  self._slots[1].raster.layout,
                "slot1_scatter_fig":    self._slots[1].scatter.figure,
                "slot1_scatter_layout": self._slots[1].scatter.layout,
                "display_order_source": self._display_order_source,
                "side_container": None,    # patched in after _build_plot_area
                "over_container": None,    # patched in after _build_plot_area
                "side_w":         side_w,
                "full_w":         full_w,
                "panel_h":        panel_h,
                "over_h":         over_h,
                "pref_src":       self._pref_source,
                # Group 3 piece 3 / Chunk 1 (added 2026-07-31): points at
                # the per-slot widgets now (panel0 = slot A = raster,
                # panel1 = slot B = scatter — same fixed assumption
                # presets use) rather than the old global ry_sel/rx_sel/
                # sx_sel/sy_sel, which piece 2 (2026-07-31) removed
                # entirely — nothing wrote to them anymore as of Chunk 1,
                # so reading them here would have silently built
                # preference keys from stale, frozen values.
                "ry_sel": self._panel_axis_widgets[self._slots[0].id]["raster"]["y_sel"],
                "rx_sel": self._panel_axis_widgets[self._slots[0].id]["raster"]["x_sel"],
                "sx_sel": self._panel_axis_widgets[self._slots[1].id]["scatter"]["x_sel"],
                "sy_sel": self._panel_axis_widgets[self._slots[1].id]["scatter"]["y_sel"],
            },
            code="""
const val  = cb_obj.active;   // 0=one, 1=side, 2=over
const one  = (val === 0);
const over = (val === 2);

// Which SLOT is currently in the primary/first screen position — read
// from the live swap tracker, not assumed from construction-time slot
// indices, since a swap (added 2026-08-03) can have moved either slot
// into either position since the page loaded. This is the "One" mode
// sizing gap flagged when the swap feature was first discussed, closed
// here as part of the same change that introduces the swap itself.
const order = display_order_source.data['order'][0];
const pos0_is_slot0 = (order[0] === 0);

const pos0_raster_fig     = pos0_is_slot0 ? slot0_raster_fig     : slot1_raster_fig;
const pos0_raster_layout  = pos0_is_slot0 ? slot0_raster_layout  : slot1_raster_layout;
const pos0_scatter_fig    = pos0_is_slot0 ? slot0_scatter_fig    : slot1_scatter_fig;
const pos0_scatter_layout = pos0_is_slot0 ? slot0_scatter_layout : slot1_scatter_layout;
const pos1_raster_fig     = pos0_is_slot0 ? slot1_raster_fig     : slot0_raster_fig;
const pos1_raster_layout  = pos0_is_slot0 ? slot1_raster_layout  : slot0_raster_layout;
const pos1_scatter_fig    = pos0_is_slot0 ? slot1_scatter_fig    : slot0_scatter_fig;
const pos1_scatter_layout = pos0_is_slot0 ? slot1_scatter_layout : slot0_scatter_layout;

// Which kind is CURRENTLY showing at each (now correctly resolved)
// position — same "check .visible" pattern already used by Cancel and
// doPlot()'s response handler, unchanged from the 2026-08-02 fix.
const pos0_fig    = pos0_raster_layout.visible ? pos0_raster_fig    : pos0_scatter_fig;
const pos0_layout = pos0_raster_layout.visible ? pos0_raster_layout : pos0_scatter_layout;
const pos1_fig    = pos1_raster_layout.visible ? pos1_raster_fig    : pos1_scatter_fig;
const pos1_layout = pos1_raster_layout.visible ? pos1_raster_layout : pos1_scatter_layout;

// "one" always shows whatever's in the primary/first screen position,
// not "the raster panel" specifically (positional, not kind-based).
pos0_layout.visible  = true;
pos1_layout.visible  = !one;

side_container.visible = !over;   // covers both "one" and "side"
over_container.visible =  over;

if (one) {
    pos0_fig.width  = full_w;
    pos0_fig.height = panel_h;
} else if (over) {
    pos0_fig.width   = full_w;
    pos1_fig.width  = full_w;
    pos0_fig.height  = over_h;
    pos1_fig.height = over_h;
} else {
    pos0_fig.width   = side_w;
    pos1_fig.width  = side_w;
    pos0_fig.height  = panel_h;
    pos1_fig.height = panel_h;
}

// Persist preference (side vs. over) — unchanged from before, "one"
// doesn't participate since there's nothing to arrange with one panel.
if (!one) {
    const key   = [ry_sel.value, rx_sel.value, 'AMPLITUDE',
                   sx_sel.value, sy_sel.value].join(':');
    const prefs = JSON.parse(pref_src.data['prefs'][0]);
    prefs[key]  = over ? 'over' : 'side';
    pref_src.data = {prefs: [JSON.stringify(prefs)]};
}
""",
        )
        layout_rbg.js_on_change("active", layout_js)

        # Store layout_js so _build_plot_area can patch container refs
        self._layout_js = layout_js

        # ---- Preset buttons ----------------------------------------------- #
        vplot_btn     = Button(label="vplot",     button_type="default", width=70)
        radplot_btn   = Button(label="radplot",   button_type="default", width=70)
        waterfall_btn = Button(label="Waterfall", button_type="default", width=80)

        def _preset_js(preset_name: str) -> CustomJS:
            ry, rx, rq, sx, sy, pl = _PRESETS[preset_name]
            # Presets always show both panels (never "one") — index 1=side,
            # 2=over on the unified layout_rbg (0 is reserved for "one").
            active_layout = 1 if pl == "side" else 2
            args = {
                **self._plot_js_args,
                "layout_rbg":      layout_rbg,
                "active_layout":   active_layout,
                # Group 3 piece 3, Chunk 2 follow-on fix (added
                # 2026-08-02): presets always force pos0=raster/
                # pos1=scatter (their own fixed assumption, unchanged —
                # see the kind_switch.active lines below), so unlike
                # layout_js above they can reference the target fig/
                # layout directly rather than needing to check which is
                # currently showing. But the *other* kind (whichever was
                # actually showing before this preset ran) also needs
                # explicit args now, to hide it — the old code only set
                # the target layout's .visible = true without touching
                # the other one, so if the user had switched a slot away
                # from the preset's assumed kind, both could briefly
                # appear stacked until doPlot()'s async response settled
                # things a moment later. Fixed to set the complete
                # correct end-state synchronously instead.
                "pos0_raster_fig":     _pos0_slot.raster.figure,
                "pos0_raster_layout":  _pos0_slot.raster.layout,
                "pos0_scatter_layout": _pos0_slot.scatter.layout,
                "pos1_scatter_fig":    _pos1_slot.scatter.figure,
                "pos1_scatter_layout": _pos1_slot.scatter.layout,
                "pos1_raster_layout":  _pos1_slot.raster.layout,
                "side_container":  None,
                "over_container":  None,
                "side_w":          side_w,
                "full_w":          full_w,
                "panel_h":         panel_h,
                "over_h":          over_h,
            }
            return CustomJS(
                args=args,
                code=self._do_plot_js + f"""
layout_rbg.active      = active_layout;

// Presets' fixed target: pos0 = raster, pos1 = scatter. Set the complete
// end-state for both positions synchronously (both the target kind
// visible AND the other kind hidden), rather than only setting the
// target visible and leaving whatever was previously showing untouched
// until doPlot()'s async response settles it a moment later.
pos0_raster_layout.visible  = true;
pos0_scatter_layout.visible = false;
pos1_scatter_layout.visible = true;
pos1_raster_layout.visible  = false;

// Group 3 piece 3 / Chunk 1 (added 2026-07-31): presets set the
// per-slot widgets directly now (panel0 = slot A = raster, panel1 =
// slot B = scatter — presets' existing fixed assumption, unchanged).
// Also force each slot's kind switch to match, in case the user had
// switched either away before clicking a preset — otherwise doPlot()
// would read a stale switch position and send the wrong kind (or the
// wrong, untouched widgets) for what the preset actually intends.
// Setting .active here fires the same js_on_change listener a real
// click would, so each tab's visible config panel updates too.
panel0_kind_switch.active = 0;
panel1_kind_switch.active = 1;

panel0_ry_sel.value = '{ry.name}';
panel0_rx_sel.value = '{rx.name}';
panel0_rq_sel.value = '{rq.name}';
panel1_sx_sel.value = '{sx.name}';
panel1_sy_sel.value = '{sy.name}';

const over = (active_layout === 2);
side_container.visible = !over;
over_container.visible =  over;
if (over) {{
    pos0_raster_fig.width   = full_w;  pos1_scatter_fig.width  = full_w;
    pos0_raster_fig.height  = over_h;  pos1_scatter_fig.height = over_h;
}} else {{
    pos0_raster_fig.width   = side_w;  pos1_scatter_fig.width  = side_w;
    pos0_raster_fig.height  = panel_h; pos1_scatter_fig.height = panel_h;
}}

doPlot();
""",
            )

        self._preset_js_objects = [
            _preset_js("vplot"),
            _preset_js("radplot"),
            _preset_js("waterfall"),
        ]
        vplot_btn.js_on_click(self._preset_js_objects[0])
        radplot_btn.js_on_click(self._preset_js_objects[1])
        waterfall_btn.js_on_click(self._preset_js_objects[2])

        # ---- Dark / Light mode toggle ------------------------------------- #
        # The toggle's initial state must follow the constructor's
        # ``theme``.  It did not, and the result was the worst possible
        # combination: ``theme="light"`` selected light-conditioned
        # palettes while the chrome stayed dark, so the scatter's
        # light-ramp sparse end sat against a dark ground -- the exact
        # mismatch that costs ~2.5x contrast (see palettes.py), and the
        # panel rendered nearly black.
        #
        # The label is inverted by design: it names the mode the button
        # switches *to*, matching the CustomJS below, so in light mode it
        # reads "Dark".
        _light = (self._theme == "light")   # effective, not requested
        dark_btn = Toggle(
            label       = "🌙 Dark" if _light else "☀ Light",
            active      = _light,       # False = currently dark
            button_type = "default",
            width       = 78,
        )
        # Group 3 piece 2 (added 2026-07-31): flattened list of every
        # per-slot, per-kind axis widget + colormap widget, replacing the
        # old self._ry_select/_rx_select/_rq_select/_sx_select/_sy_select
        # + self._raster_cmap_widgets/_scatter_cmap_widgets references
        # (removed along with the global sections they belonged to).
        # Built explicitly rather than assumed covered by the shared
        # `dark` InlineStyleSheet instance these widgets were constructed
        # with — the toggle's JS mutates each listed widget's own
        # stylesheets[0].css directly, so anything not listed here
        # wouldn't get re-themed regardless of what object it shares.
        _all_axis_widgets = []
        _all_cmap_figs = []
        _all_cmap_icons = []
        for slot in self._slots:
            for kind in ("raster", "scatter"):
                w = self._panel_axis_widgets[slot.id][kind]
                if kind == "raster":
                    _all_axis_widgets += [w["y_sel"], w["x_sel"], w["q_sel"]]
                else:
                    _all_axis_widgets += [w["x_sel"], w["y_sel"]]
                _all_axis_widgets += w["cmap_widgets"]
                _all_cmap_figs     += w["cmap_figs"]
                _all_cmap_icons    += w["cmap_icons"]

        # Panels that have an image source, and the sources themselves.
        # Both the toolbar args and _panel_image_payloads() iterate this
        # same index list, which is what keeps the two sides aligned.
        self._theme_img_panels = [
            i for i, pnl in enumerate(self._all_panels)
            if getattr(pnl, "_image_source", None) is not None
        ]
        _theme_img_srcs = [self._all_panels[i]._image_source
                           for i in self._theme_img_panels]

        # Captured so _apply_startup_theme() can hand the same models to
        # the same JS body -- one arg set, one code path.
        self._theme_restyle_args = {
                # All four panel figures/info-divs, not just the two
                # kind-active ones (Group 2 rework, 2026-07-31) — a
                # currently-inactive panel should already be in the
                # correct theme by the time it's shown, not caught in
                # Bokeh's raw default because the toggle never reached it.
                "figs":         [p.figure    for p in self._all_panels],
                "info_divs":    [p._info_div for p in self._all_panels],
                "sidebar":      self._sidebar_col,
                "status_div":   self._status_div,
                "notify_div":   self._notify_div,
                # Two light-mode gaps fixed 2026-08-03, found during
                # testing — neither was ever covered by any part of the
                # existing dark/light mechanism (the config-field hint
                # divs use plain .styles with a hardcoded dark-only
                # color, not stylesheets, so the generic `widgets` loop
                # below never reaches them; path_div's color is baked
                # into an inline HTML <span style=...> inside .text
                # itself, which no property-based recolor mechanism can
                # reach at all — it has to be regenerated, not restyled).
                "hint_divs":    [self._hint_field, self._hint_spw,
                                 self._hint_corr, self._hint_scan,
                                 self._hint_antenna, self._hint_time,
                                 self._hint_uvrange],
                "path_div":         self._path_div,
                "source_basename":  os.path.basename(self._source_path),
                # _spw_table, not _spw_select: the latter is now a column
                # wrapping the label, the All/None row and the table, and
                # the restyle walks widgets rather than containers.
                "widgets":      [self._col_select, self._field_select,
                                 self._spw_table, self._corr_cbg,
                                 self._field_prev_btn, self._field_next_btn,
                                 self._spw_prev_btn, self._spw_next_btn]
                                + _all_axis_widgets,
                # Colormap histogram figures + reset-button icons (added
                # to fix a reported light-mode gap: these previously had
                # dark colors hardcoded once at construction time in
                # colormap_controls() with nothing to ever change them).
                # Kept separate from `figs`/`widgets` above rather than
                # merged in, since the histogram deliberately uses a
                # dimmer background than the main panels' stark
                # black/white (so the plotted distribution stays
                # legible — see colormap_controls' own comments), and
                # icon color isn't reachable via `stylesheets` at all.
                "cmap_figs":    _all_cmap_figs,
                "cmap_icons":   _all_cmap_icons,
                # Gear tab strip ("Panel A"/"Panel B") -- previously not
                # included in the toggle at all, so it stayed dark
                # regardless of mode (reported directly: the header/tab
                # area, unlike everything else, never responded to the
                # toggle). Uses Bokeh's shadow-DOM tab classes
                # (.bk-header/.bk-tab), not the .bk-input/.bk-btn ones
                # widget_css targets, so it needs its own CSS strings
                # below rather than reusing widget_css.
                "tabs":         self._gear_tabs,
                # CSS content passed in from the Python-side constants
                # rather than duplicated as inline JS template literals
                # (which is what this replaced) -- single source of
                # truth, so a change to e.g. _DARK_WIDGET_CSS can't
                # silently drift out of sync with what the toggle
                # actually applies, and a new theme constant can't go
                # missing the way _LIGHT_TABS_CSS did before it existed.
                "dark_css":       _DARK_WIDGET_CSS,
                "light_css":      _LIGHT_WIDGET_CSS,
                "dark_tabs_css":  _DARK_TABS_CSS,
                # Section headings and the SPW table: both were styled
                # once at construction with no way to change them, so
                # they did not follow the theme (headings read as
                # disabled on light; the table stayed light-on-white on
                # dark).
                "section_divs":   self._section_divs,
                "section_dark":   _SECTION_DARK,
                "section_light":  _SECTION_LIGHT,
                "spw_table":       self._spw_table,
                "sidebar_css":     self._sidebar_css,
                "table_css_dark":  self._table_css_dark,
                "table_css_light": self._table_css_light,
                "light_tabs_css": _LIGHT_TABS_CSS,
                # For the p2j theme message appended to the restyle body.
                "ctrl":           ctrl,
                "ids":            ids,
                # Image sources, so the theme response can be applied.
                # Captured with their indices so _panel_image_payloads()
                # returns a list in exactly this order.
                "theme_img_srcs": _theme_img_srcs,
        }

        dark_btn.js_on_change("active", CustomJS(
            args=dict(self._theme_restyle_args),
            code="""
const light     = cb_obj.active;
""" + _THEME_RESTYLE_JS + """
cb_obj.label = light ? '\U0001f319 Dark' : '\u2600 Light';

// Tell Python, so the palettes follow.  The chrome above is JS-only;
// the ramps are resolved server-side and conditioned against the
// background, so without this the plot keeps ramps trimmed for the
// *previous* theme and the scatter becomes nearly unreadable.
// A SHADE-level refresh (refresh.py) -- no re-query, so this is fast
// enough to run on the click rather than deferring to Plot.
if (typeof ctrl !== 'undefined' && ctrl && ids && ids['theme']) {
    ctrl.send(ids['theme'], { theme: light ? 'light' : 'dark' },
        function(resp) {
            // Apply the re-shaded images.  Assigning image_source.data
            // in Python does nothing here -- no Bokeh server -- so the
            // handler returns them and the client installs them, the
            // same contract every other image-updating handler uses.
            if (!resp || resp.status !== 'ok' || !resp.images) return;
            if (typeof theme_img_srcs === 'undefined') return;
            var n = Math.min(resp.images.length, theme_img_srcs.length);
            for (var i = 0; i < n; i++) {
                var im = resp.images[i], src = theme_img_srcs[i];
                if (!im || !src) continue;      // deferred/blank panel
                src.data = {
                    image: [im.image],
                    x:     [im.x0],
                    y:     [im.y0],
                    dw:    [im.x1 - im.x0],
                    dh:    [im.y1 - im.y0],
                };
                src.change.emit();
            }
        });
}
""",
        ))

        # ---- Flag ⚑ / Unflag toolbar buttons removed -----------------------
        # Flagging now lives directly on each figure's own toolbar via the
        # FlagTool / FlagTool(flag=False) drag tools added in
        # VisibilityPlot._add_flag_tools() (see visibility_plot.py), rather
        # than as top-level stub buttons here. Pending-flag count is still
        # surfaced in the status bar via _update_status_bar()/_status_text().
        # When enable_flagging=False no such tools exist on either figure
        # and there is nothing here to disable/hide — this row simply has
        # no flagging-related controls in that case.
        # ---- Separators --------------------------------------------------- #
        def _sep():
            return Div(text="&nbsp;|&nbsp;", width=14,
                       styles={"line-height": "32px", "color": "#45475a"})

        return row(
            Tip(sidebar_toggle_btn,
                tooltip=self._tt("Show / hide the plot configuration panel", "right")),
            _sep(),
            Tip(plot_btn,   tooltip=self._tt("Replot both panels using the current configuration")),
            Tip(reload_btn, tooltip=self._tt("Reload data and replot (clears any pending flags)")),
            _sep(),
            Tip(layout_rbg, tooltip=self._tt("Show one panel, or both side by side / one above the other")),
            _sep(),
            Tip(vplot_btn,     tooltip=self._tt("Preset: Baseline vs Time (raster) + Amplitude vs Time (scatter)")),
            Tip(radplot_btn,   tooltip=self._tt("Preset: Baseline vs Time (raster) + Amplitude vs UV Distance (scatter)")),
            Tip(waterfall_btn, tooltip=self._tt("Preset: Amplitude vs Channel waterfall (over/under layout)")),
            _sep(),
            Tip(export_btn, tooltip=self._tt(
                "Write the current view to a PNG (server-side; the path is "
                "reported below the plots)")),
            _sep(),
            Tip(dark_btn, tooltip=self._tt("Toggle between dark and light background")),
        )

    # ---------------------------------------------------------------------- #
    # Plot area                                                                #
    # ---------------------------------------------------------------------- #

    def _build_plot_area(self):
        """Build both layout containers with linked cursor spans."""
        if self._raster_x == self._scatter_x:
            self._scatter.figure.x_range = self._raster.figure.x_range

        # Group 3 piece 3, Chunk 2 (added 2026-07-31): both layout
        # objects (raster and scatter) for BOTH slots are now children of
        # side_container/over_container from construction, not just
        # whichever is currently active — visibility toggling (below)
        # selects which one is shown per slot, the same mechanism this
        # method already used for "One" mode's pos1_layout (a hidden
        # LayoutDOM child in a Bokeh row/column takes no space — proven
        # working before this chunk, not new machinery). This replaces
        # needing to reassign which object occupies a container position
        # when a slot's kind actually changes (doPlot()'s response
        # handler, not this method, does that toggling at runtime) —
        # simpler than swapping container.children entries, since the
        # "different object each time" case tabs.tabs needed doesn't
        # apply here: exactly two pre-built alternatives per slot, not an
        # open-ended list.
        #
        # Positional (Group 1 rework, 2026-07-31, still applies): WHICH
        # SLOT'S pair is in the primary/first position vs the
        # secondary/second is a screen-position concept
        # (self._slot_display_order), independent of which kind either
        # slot currently shows. self._pos0/self._pos1 still resolve
        # correctly through a kind change (they read slot.active, which
        # follows slot.kind) — used below only to decide "One" mode's
        # sizing target, not container membership anymore.
        pos0_slot = self._slots[self._slot_display_order[0]]
        pos1_slot = self._slots[self._slot_display_order[1]]

        # Stage 1a fixed roles, now positional rather than kind-based:
        # position 0 is always shown; position 1 only when layout != "one".
        # Previously this initial state was never set in Python at all —
        # mode="raster"/"scatter" only took visual effect after a user
        # interaction fired the (now-removed) mode_js listener, never on
        # first page load. Closed here as part of the same edit rather
        # than left for a separate pass, since it's the same code this
        # rewrite already has to touch.
        pos0_slot.raster.layout.visible  = (pos0_slot.kind == "raster")
        pos0_slot.scatter.layout.visible = (pos0_slot.kind == "scatter")
        pos1_slot.raster.layout.visible  = (
            self._layout != "one" and pos1_slot.kind == "raster")
        pos1_slot.scatter.layout.visible = (
            self._layout != "one" and pos1_slot.kind == "scatter")
        if self._layout == "one":
            _full_w  = self._layout_js.args["full_w"]
            _panel_h = self._layout_js.args["panel_h"]
            self._pos0.figure.width  = _full_w
            self._pos0.figure.height = _panel_h

        # ---- Linked cursor Spans ----------------------------------------- #
        # Generalized to N panels (added 2026-07-31, closing the KNOWN GAP
        # flagged in Chunk 2 above). Previously kind-indexed
        # (self._raster/self._scatter, exactly 2 fixed roles) — degraded
        # once Chunk 2 allowed two rasters or two scatters at once, since
        # "the raster's span" stopped identifying a single panel.
        #
        # Revised design, confirmed in conversation: tracking is no
        # longer "raster syncs with scatter" but "any panel whose cursor
        # moved syncs with every *other* panel that shares at least one
        # matching axis dimension" — X-vs-X, X-vs-Y, Y-vs-X, and Y-vs-Y
        # are all independently checked, so a panel with two matching
        # dimensions correctly gets both its vertical and horizontal span
        # set, not just the first match found.
        #
        # Same "structural cost paid regardless of visibility" pattern as
        # decision 11/Group 2 (flag tools, register_select_callback, dark
        # styling — already applied to all four panel objects, not just
        # the two currently active): every panel gets its own span pair
        # unconditionally at construction, so a kind switch never needs
        # to create anything new at runtime, only decide whether to move
        # something that already exists.
        from bokeh.models import Span

        def _make_span(dim, color="#f38ba8"):
            return Span(location=float("nan"), dimension=dim,
                        line_color=color, line_width=1,
                        line_alpha=0.85, line_dash="dashed")

        # One descriptor per panel object (self._all_panels — all four,
        # not just the two currently active), built once here rather than
        # in __init__, since spans need a figure to attach to via
        # add_layout() and that's already guaranteed to exist by this
        # point regardless of construction order.
        _cursor_panels = []
        for panel in self._all_panels:
            vspan = _make_span("height")   # vertical span (tracks X)
            hspan = _make_span("width")    # horizontal span (tracks Y)
            panel.figure.add_layout(vspan)
            panel.figure.add_layout(hspan)
            _cursor_panels.append({
                "id": panel.vr_id, "fig": panel.figure,
                "vspan": vspan, "hspan": hspan,
            })

        cursor_src = self._cursor_source

        self._cursor_source.js_on_change("data", CustomJS(
            args={
                "cursor_src": cursor_src,
                "panels":     _cursor_panels,
            },
            code="""
const x      = cursor_src.data['x'][0];
const y      = cursor_src.data['y'][0];
const fig_id = cursor_src.data['fig'] ? cursor_src.data['fig'][0] : '';

// Reset every panel's spans first — same as before, just looped instead
// of four hardcoded lines.
for (const p of panels) {
    p.vspan.location = NaN;
    p.hspan.location = NaN;
}

if (x != null && !isNaN(x)) {
    let source = null;
    for (const p of panels) {
        if (p.id === fig_id) { source = p; break; }
    }

    if (source) {
        const sx_label = source.fig.below.length ? source.fig.below[0].axis_label : '';
        const sy_label = source.fig.left.length  ? source.fig.left[0].axis_label  : '';

        for (const p of panels) {
            if (p === source) {
                // Always show on the panel the cursor is actually in —
                // same as the old "always show vertical span on raster
                // at x" rule, generalized to whichever panel fired.
                p.vspan.location = x;
                if (y != null && !isNaN(y)) p.hspan.location = y;
                continue;
            }

            const p_x_label = p.fig.below.length ? p.fig.below[0].axis_label : '';
            const p_y_label = p.fig.left.length  ? p.fig.left[0].axis_label  : '';

            // This panel's vertical span (its X axis): match against the
            // source's X first, else the source's Y — independent of the
            // horizontal-span check below, so both can fire on the same
            // panel if it happens to match on both axes.
            if (sx_label && sx_label === p_x_label) {
                p.vspan.location = x;
            } else if (sy_label && sy_label === p_x_label && y != null && !isNaN(y)) {
                p.vspan.location = y;
            }

            // This panel's horizontal span (its Y axis): same two-way
            // check against the source's X and Y.
            if (sx_label && sx_label === p_y_label) {
                p.hspan.location = x;
            } else if (sy_label && sy_label === p_y_label && y != null && !isNaN(y)) {
                p.hspan.location = y;
            }

            console.log('[visplot cursor-span]', p.id,
                        'p_x_label:', p_x_label, 'p_y_label:', p_y_label,
                        'sx_label:', sx_label, 'sy_label:', sy_label,
                        'vspan.location:', p.vspan.location,
                        'hspan.location:', p.hspan.location);
        }
    }
}
""",
        ))

        # All four layout objects as children — see the Chunk 2 comment at
        # the top of this method for why (visibility toggling, not
        # container.children reassignment, selects which is shown).
        side_container = row(
            pos0_slot.raster.layout, pos0_slot.scatter.layout,
            pos1_slot.raster.layout, pos1_slot.scatter.layout,
            sizing_mode = "stretch_width",
            visible     = (self._layout in ("one", "side")),
        )
        over_container = column(
            pos0_slot.raster.layout, pos0_slot.scatter.layout,
            pos1_slot.raster.layout, pos1_slot.scatter.layout,
            sizing_mode = "stretch_width",
            visible     = (self._layout == "over"),
        )

        self._layout_js.args["side_container"] = side_container
        self._layout_js.args["over_container"] = over_container
        for pjs in self._preset_js_objects:
            pjs.args["side_container"] = side_container
            pjs.args["over_container"] = over_container
        self._sidebar_toggle_js.args["side_container"] = side_container
        self._sidebar_toggle_js.args["over_container"] = over_container
        for sjs in self._swap_js_objects:
            sjs.args["side_container"] = side_container
            sjs.args["over_container"] = over_container

        return side_container, over_container

    # ---------------------------------------------------------------------- #
    # Status bar                                                               #
    # ---------------------------------------------------------------------- #

    def _build_status_bar(self):
        _hint_style = {
            "font-size":   "12px",
            "font-family": "monospace",
            "padding":     "4px 10px",
            "background":  "#181825",
            "color":       "#89dceb",     # cyan — distinct from status green
            "border-top":  "1px solid #45475a",
        }
        _status_style = {
            "font-size":   "12px",
            "font-family": "monospace",
            "padding":     "4px 10px",
            "background":  "#181825",
            "color":       "#a6e3a1",
            "border-top":  "1px solid #45475a",
        }

        self._status_div = Div(
            text        = self._status_text(),
            sizing_mode = "stretch_width",
            visible     = True,
            styles      = _status_style,
        )
        self._notify_div = Div(
            text        = "",
            sizing_mode = "stretch_width",
            styles      = {
                "font-size":    "12px",
                "font-family":  "monospace",
                "padding":      "4px 10px",
                "background":   "#181825",
                "color":        "#f38ba8",
                "min-height":   "18px",
                "text-align":   "right",
                "border-top":   "1px solid #45475a",
                "border-left":  "1px solid #45475a",
            },
        )
        # Shared status-bar line: dataset/config summary (green) on the
        # left half, flagging feedback (red, via _notify()) on the right
        # half. Both halves hide together during a sidebar-field hint
        # (see _focus_blur in _build_sidebar), which needs the full row
        # width for itself.
        self._status_row = row(
            self._status_div, self._notify_div,
            sizing_mode="stretch_width",
        )

        # Pre-built hint divs — one per sidebar widget that benefits from help.
        # All start hidden; focus/blur JS toggles visibility.
        def _hint(html):
            return Div(text=html, sizing_mode="stretch_width",
                       visible=False, styles=_hint_style)

        self._hint_field    = _hint("")   # filled in _build_sidebar with MS data
        self._hint_spw      = _hint("")
        self._hint_corr     = _hint("")
        self._hint_scan     = _hint("")
        self._hint_antenna  = _hint("")
        self._hint_time     = _hint("")
        self._hint_uvrange  = _hint("")

        return column(
            self._status_row,
            self._hint_field,
            self._hint_spw,
            self._hint_corr,
            self._hint_scan,
            self._hint_antenna,
            self._hint_time,
            self._hint_uvrange,
            sizing_mode="stretch_width",
        )
