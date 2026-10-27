########################################################################
#
# Copyright (C) 2022,2024
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
# pylint: disable=wrong-import-position
'''End user applications supplied by ``cubevis``.

Each is imported on first use (PEP 562 ``__getattr__``), so that using one
needs only that one's dependencies: ``visplot`` needs no CASA, ``iclean``
does.  (Until 2026-10-26 this was ``utils.ImportProtectedModule``, which
replaced this module's class to the same end.)  A new application goes in
``_LAZY``.
'''
import importlib as _importlib

_LAZY = {
    'plotants':                 '._plotants',
    'plotbandpass':             '._plotbandpass',
    'CreateMask':               '._createmask',
    'CreateRegion':             '._createregion',
    'InteractiveClean':         '._interactiveclean',
    'InteractiveCleanNotebook': '._interactivecleannotebook',
    'iclean':                   '..casatasks.iclean',
    'visplot':                  '..casatasks.visplot',
    'iclean_notebook':          '..casatasks.iclean_notebook',
    'createmask':               '..casatasks.createmask',
    'createregion':             '..casatasks.createregion',
}

__all__ = sorted(_LAZY)


def __getattr__(name):
    if name in _LAZY:
        value = getattr(_importlib.import_module(_LAZY[name], __name__), name)
        globals()[name] = value             # an ordinary attribute from now on
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
