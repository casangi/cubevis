########################################################################
#
# Copyright (C) 2026
# Associated Universities, Inc. Washington DC, USA.
#
# This script is free software; you can redistribute it and/or modify it
# under the terms of the GNU Library General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# This library is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Library General Public
# License for more details.
#
# You should have received a copy of the GNU Library General Public License
# along with this library; if not, write to the Free Software Foundation,
# Inc., 675 Massachusetts Ave, Cambridge, MA 02139, USA.
#
# Correspondence concerning AIPS++ should be adressed as follows:
#        Internet email: casa-feedback@nrao.edu.
#        Postal address: AIPS++ Project Office
#                        National Radio Astronomy Observatory
#                        520 Edgemont Road
#                        Charlottesville, VA 22903-2475 USA
#
########################################################################
'''Implementation of ``FlagTool`` — a custom Bokeh drag tool that combines
two behaviours needed by ``VisibilityPlotter``:

1. **Click** (drag with ~zero movement) zooms the *local* figure to the
   point where one screen pixel corresponds to one Datashader aggregation
   cell — i.e. the resolution at which box-select flagging is semantically
   meaningful, since flagging must operate on individual visibilities
   rather than averaged/decimated bins.
2. **Drag** draws a rubber-band box (a ``BoxAnnotation`` overlay). On
   release, if the figure is at/past pixel resolution
   (``at_pixel_res=True``) the box extent is sent to Python via the
   panel's dedicated flagging ``Comm`` as a flag/unflag delta. Below pixel
   resolution the box is drawn for visual feedback only and nothing is
   sent — this preview-scope box-drawing-without-effect mirrors the
   original box-select stub behaviour until the caller is actually zoomed
   in far enough for the operation to be well defined.

``FlagTool`` is deliberately *not unselectable* the way ordinary Bokeh
gesture tools are: re-clicking the toolbar button while it is already the
active drag tool does not deactivate it. Instead the re-click is
intercepted (in the TypeScript view's ``_active_change``) and treated as
another "zoom to 1:1" click. This lets a single button serve as both the
persistent flagging mode and the 1:1 zoom shortcut, without a separate
deactivated state that would silently stop flagging from working.

Two instances are normally created per figure — one with ``flag=True``
(solid-flag icon) and one with ``flag=False`` (flag-outline icon,
"Unflag"). Both share the figure's existing ``Comm``/``msg_id`` pair
(the *same* handler distinguishes flag vs. unflag via the ``flag`` field
in the j2p payload), and both reference the figure's ``image_source`` /
``state_source`` so the TypeScript view can compute the 1:1 zoom extent
and keep ``at_pixel_res`` current across ordinary pan/zoom too.

See ``visplot_preview_handoff.md`` / the "custom FlagTool" design
discussion for the full rationale.
'''

from os.path import join, dirname

from bokeh.core.properties import Bool, String, Instance, Nullable
from bokeh.models import Tool, ColumnDataSource, Div

from cubevis.bokeh.transport import Comm

from cubevis.data import casaimage

from ._drag_tool import DragTool

# cubevis/bokeh/tools/_flag_tool.py -> cubevis/__icons__
_ICONS_DIR = join(dirname(dirname(dirname(__file__))), "__icons__")
_ICON_FLAG_SOLID   = join(_ICONS_DIR, "flag-data.png")
_ICON_FLAG_OUTLINE = join(_ICONS_DIR, "unflag-data.png")


class FlagTool(DragTool):
    '''Box-select flag/unflag tool, gated on 1:1 pixel-to-data-cell zoom.

    Parameters
    ----------
    flag : bool
        ``True`` — this instance flags (solid-flag icon, sends
        ``flag: true`` in the j2p payload). ``False`` — this instance
        unflags (flag-outline icon, sends ``flag: false``). Two separate
        ``FlagTool`` instances (one of each) are normally added to a
        figure's toolbar.
    comm :
        The panel's *dedicated* flagging Comm (``VisibilityPlot._flag_comm``)
        — deliberately separate from the panel's main ``_comm`` (probe,
        rerender, ...), which uses ``squash_queue=True`` and would risk a
        rapid second flag/unflag box silently squashing an earlier one
        still queued. Both FlagTool instances (flag + unflag) on a given
        panel share this same Comm.
    msg_id : str
        Message id the box extent is sent under on release, e.g. the
        panel's ``_msg_select`` (registered Python-side via
        ``register_select_callback``).
    panel : str
        ``"raster"`` or ``"scatter"`` — included in the j2p payload so a
        single Python handler can route the delta using the correct axis
        (time vs. frequency) for that panel, exactly as
        ``VisibilityPlotter._handle_box_select`` already does for the
        prior ``BoxSelectTool``-based flow.
    image_source :
        The panel's ``_image_source``. Not currently read by the TS view
        (reserved for a future overlay-based visual "already flagged"
        indication) but wired through for that purpose.
    state_source :
        The panel's ``_state_source``. Supplies ``full_x0/x1/y0/y1``
        (full data extent) and ``agg_n_x``/``agg_n_y`` (aggregation grid
        size) used to compute the 1:1 zoom-to-pixel-resolution extent.
    notify_div :
        ``VisibilityPlotter._notify_div`` — the red half of the shared
        status-bar row. There is no Bokeh server, so a plain Python-side
        ``self._notify_div.text = ...`` assignment never reaches the
        browser; this reference lets the TS view apply the *response* of
        the ``comm.send()`` round-trip directly to the live browser-side
        Div, the same way ``doPlot``'s ``resp.status_text`` handling
        already does for ``status_div``. Shared by all four flag/unflag
        tool instances (raster + scatter, flag + unflag) since only one
        message needs showing at a time.
    status_div :
        ``VisibilityPlotter._status_div`` — the green half of the same
        row, used to refresh the pending-flag count after a successful
        flag/unflag, via the same response-callback mechanism.
    '''

    flag = Bool(default=True, help="""
    True: flagging instance (solid-flag icon, payload flag=true).
    False: unflagging instance (flag-outline icon, payload flag=false).
    """)

    comm = Nullable(Instance(Comm), help="""
    The panel's dedicated flagging Comm (VisibilityPlot._flag_comm, NOT
    the panel's main _comm — see class docstring), used to send the
    finalized box extent to Python on drag-release and receive back
    notify_text/notify_color/status_text to apply to notify_div/status_div.
    """)

    msg_id = String(default="", help="""
    Message id the box extent is sent under (shared with the panel's
    box-select handler registration).
    """)

    panel = String(default="", help="""
    'raster' or 'scatter' — identifies which panel this tool instance is
    attached to, included in the j2p payload.
    """)

    image_source = Nullable(Instance(ColumnDataSource), help="""
    The panel's _image_source. Reserved for future use (e.g. rendering a
    "pending flag" indication directly from the tool).
    """)

    state_source = Nullable(Instance(ColumnDataSource), help="""
    The panel's _state_source. Supplies full_x0/x1/y0/y1 and
    agg_n_x/agg_n_y, used client-side to compute the 1:1 zoom-to-pixel-
    resolution extent and to keep at_pixel_res current after any pan or
    zoom (not just clicks on this tool).
    """)

    notify_div = Nullable(Instance(Div), help="""
    VisibilityPlotter._notify_div (red half of the status row). The TS
    view writes to this directly from the comm.send() response callback
    — there is no Bokeh server, so nothing set on the Python side syncs
    to the browser on its own.
    """)

    status_div = Nullable(Instance(Div), help="""
    VisibilityPlotter._status_div (green half of the status row). Same
    response-callback mechanism as notify_div, used to refresh the
    pending-flag count after a successful flag/unflag.
    """)

    at_pixel_res = Bool(default=False, help="""
    True once the figure's viewport is zoomed to (or past) one screen
    pixel per aggregation cell. Recomputed client-side on every x_range /
    y_range change. Watched by CustomJS elsewhere to toggle the toolbar
    button's colour (red -> green) as a "you're at flagging resolution
    now" signal. The box is sent to Python on every release regardless of
    this value (included in the payload) — Python decides whether to
    record a FlagDelta and reports back via notify_div either way, rather
    than the browser silently discarding below-resolution boxes.
    """)

    # explicit __init__ to support Init signatures — mirrors DragTool, but
    # the icon depends on `flag` so it can't be set via the DragTool
    # superclass's hardcoded icon kwarg; set it after construction instead.
    def __init__(self, *args, flag: bool = True, **kwargs) -> None:
        kwargs.setdefault("flag", flag)
        super().__init__(*args, **kwargs)
        self.icon = casaimage.as_mime(
            _ICON_FLAG_SOLID if self.flag else _ICON_FLAG_OUTLINE
        )
        self.description = "Flag" if self.flag else "Unflag"


Tool.register_alias("flag",   lambda: FlagTool(flag=True))
Tool.register_alias("unflag", lambda: FlagTool(flag=False))
