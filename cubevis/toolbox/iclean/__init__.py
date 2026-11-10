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
'''Interactive clean, the application (as ``cubevis.toolbox.visplot`` is
visplot's):

* ``_app.py``     ``InteractiveClean`` and ``InteractiveCleanNotebook``,
                  GENERATED from ``_app.mustache`` and tclean's task XML by
                  ``scripts/expand_templates`` (edit the template)
* ``_ui.py``      ``InteractiveCleanUI``: the Bokeh GUI
* ``_widgets.py`` ``SharedWidgets``: the iteration and automask inputs
                  shared by the GUI's tabs

The imager and its images are reached only through the imaging layer,
``cubevis.imaging``.  The task wrappers (``iclean``, ``iclean.notebook``,
casashell's ``iclean``) are generated into ``cubevis/private/casatasks`` and
``cubevis/private/casashell``.

Each name is imported on first use (as in ``cubevis.toolbox``).
'''

import importlib as _importlib
from typing import TYPE_CHECKING

_LAZY = {
    "InteractiveClean":         "._app",
    "InteractiveCleanNotebook": "._app",
    "InteractiveCleanUI":       "._ui",
    "SharedWidgets":            "._widgets",
}

__all__ = sorted(_LAZY)


def __getattr__(name):
    if name in _LAZY:
        value = getattr(_importlib.import_module(_LAZY[name], __name__), name)
        globals()[name] = value          # once imported, an ordinary attribute
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_LAZY))


if TYPE_CHECKING:                        # for type checkers and editors only
    from ._app import InteractiveClean, InteractiveCleanNotebook
    from ._ui import InteractiveCleanUI
    from ._widgets import SharedWidgets
