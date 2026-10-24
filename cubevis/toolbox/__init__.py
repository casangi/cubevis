########################################################################
#
# Copyright (C) 2022,2024,2025
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
'''Common tools used in creating applications.'''

# Lazy package (PEP 562).  ``CubeMask``, ``RegionList`` and
# ``InteractiveCleanUI`` are imported on first use, not when the package is:
# every ``cubevis.toolbox.*`` import (a remote kernel's visplot backends
# among them) runs this file, and with eager imports it loaded the image
# and clean applications on the node for nothing -- slower, and part of
# the kernel-side fingerprint (``cubevis/_fingerprint.py``, remote session
# design 7c) for code that never runs there.  ``from cubevis.toolbox import
# CubeMask`` works as before; ``tests/manual/setup/test_lazy_packages.py``
# checks every name.  Keep new public names in ``_LAZY``.
import importlib as _importlib
from typing import TYPE_CHECKING

_LAZY = {
    "CubeMask": "._cube",
    "RegionList": "._region_list",
    "InteractiveCleanUI": "._interactive_clean_ui",
}

__all__ = ["CubeMask", "RegionList", "InteractiveCleanUI"]


def __getattr__(name):
    if name in _LAZY:
        value = getattr(_importlib.import_module(_LAZY[name], __name__), name)
        globals()[name] = value          # once imported, an ordinary attribute
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_LAZY))


if TYPE_CHECKING:                        # for type checkers and editors only
    from ._cube import CubeMask
    from ._region_list import RegionList
    from ._interactive_clean_ui import InteractiveCleanUI
