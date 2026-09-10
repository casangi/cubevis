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
'''Implementation of ``InfoTool`` -- a custom Bokeh drag tool for the
hover-probe redesign's piece 3 ("click-to-exact"), added only to
``VisibilityScatter`` figures -- see
``VisibilityScatter._add_info_tool``. ``VisibilityRaster`` has no
equivalent: piece 1 already made raster's hover exact and fully local,
so there is nothing for a click/drag tool to add there.

Two behaviours, both sent to Python over the same dedicated Comm:

1. **Click** (drag with ~zero movement) -- sends the clicked point's
   data-space ``{x, y}``. Python
   (``VisibilityScatter._handle_probe_region``, via ``_click_window``)
   collapses this to a small rectangle -- about one displayed canvas
   pixel wide -- before querying the backend, since an exact float
   point would very likely match nothing at all.
2. **Drag** -- draws a rubber-band box (a ``BoxAnnotation`` overlay,
   visually distinct from ``FlagTool``'s -- info blue rather than
   flag pink/green), and on release sends the box's data-space extent
   directly.

Both cases open a **brand-new** browser tab **synchronously**, at
click/drag-release time -- i.e. still inside the browser's click/drag
event, which it still considers a trusted user gesture -- with a small
"fetching" placeholder, then fill it in once the ``comm.send()``
response (Python-built HTML -- see
``VisibilityScatter._probe_region_page``) arrives. Opening the tab
*after* that response arrives instead would very likely be blocked as
a pop-up: the round trip is a real backend query (unlike the coarse,
free hover-probe grid), and for a remote session in particular it can
take long enough that the browser no longer considers the response
callback part of the original gesture. See ``info_tool.ts`` for the
actual open/fill logic.

A new tab every time, deliberately (an earlier version of this tool
reused one fixed-name tab, but that meant every new click overwrote
the previous answer -- multiple clicks/drags are a natural way to
compare several selections at once, which a reused tab worked
against). Each page's ``<title>`` carries the clicked rectangle (see
``VisibilityScatter._handle_probe_region``) so a pile of open tabs
stays identifiable in the browser's tab strip/window list.

Deliberately does **not** extend ``FlagTool`` or modify ``DragTool``.
``FlagTool``'s click behaviour (zoom to 1:1 pixel resolution) has
nothing to do with what a click should do here (send a point probe),
so there is no shared behaviour to inherit from it. Its click-vs-drag
threshold bookkeeping *would* be reusable in principle, but lifting it
into the shared ``DragTool`` base for one more caller was judged not
worth touching already-working, already-tested code for -- see
``info_tool.ts``, which duplicates that small piece of logic locally
instead, the same way ``FlagTool`` itself doesn't inherit it from
anywhere either.

Only one instance is added per figure (no flag/unflag-style pair --
there is only one kind of "look this up exactly").
'''

from os.path import join, dirname

from bokeh.core.properties import Instance, Nullable, String
from bokeh.models import Tool

from cubevis.bokeh.transport import Comm

from cubevis.data import casaimage

from ._drag_tool import DragTool

# cubevis/bokeh/tools/_info_tool.py -> cubevis/__icons__
_ICONS_DIR = join(dirname(dirname(dirname(__file__))), "__icons__")
_ICON_INFO = join(_ICONS_DIR, "info-probe.png")


class InfoTool(DragTool):
    '''Click/drag exact-identity probe tool, scatter figures only.

    Parameters
    ----------
    comm :
        The panel's dedicated info-probe Comm
        (``VisibilityScatter._info_comm``) -- deliberately separate
        from both the panel's main ``_comm`` (``squash_queue=True``;
        a rapid second click could otherwise squash an in-flight first
        one before Python has answered it) and the flagging
        ``_flag_comm`` (a different, unrelated concern that happens to
        also be a drag tool).
    msg_id : str
        Message id the click point or box extent is sent under
        (registered Python-side via ``VisibilityScatter._add_info_tool``).

    Note on the ``overlay`` (rubber-band box) property
    ----------------------------------------------------
    Declared only in ``info_tool.ts``'s ``define()`` block, not here --
    matching ``FlagTool``'s own asymmetric pattern (its ``overlay`` is
    likewise TS-only in ``flag_tool.ts``). Python never reads or writes
    it, so there is nothing for a Python-side property to do beyond
    duplicating what the TS side already declares.
    '''

    comm = Nullable(Instance(Comm), help="""
    The panel's dedicated info-probe Comm.
    """)

    msg_id = String(default="", help="""
    Message id the click point or box extent is sent under.
    """)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.icon = casaimage.as_mime(_ICON_INFO)
        self.description = "Exact identity (click, or drag a box)"


Tool.register_alias("info_probe", lambda: InfoTool())
