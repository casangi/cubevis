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
'''Making ``casa-image`` an xarray engine by name when this package is
imported from a checkout, where no entry point is installed.

xarray finds engines through the ``xarray.backends`` entry points of
*installed* packages.  Run from a source tree (no ``pip install``),
``pyproject.toml``'s entry point does not exist, and
``xr.open_dataset(path, engine="casa-image")`` fails with "unrecognized
engine".  ``register()`` adds the engine to xarray's table then; where the
entry point is installed it does nothing.

This is the same remedy as ``cubevis/xarray/_register.py`` on the calplot
branch, kept as a copy inside this package so that the package still
imports nothing from outside itself.

Registering the *name* does not disturb xradio's own engine for CASA
images (``xradio_casa_image``): the two have different names, and this one
never claims a path that no engine was named for (``guess_can_open`` is
False).
'''

import logging

log = logging.getLogger(__name__)

ENGINE = 'casa-image'
ENTRY_POINT_GROUP = 'xarray.backends'


def entry_point_installed(name: str = ENGINE) -> bool:
    '''Does an installed package give xarray the engine ``name``?'''
    try:
        from importlib.metadata import entry_points      # pylint: disable=import-outside-toplevel
        return any(e.name == name for e in entry_points(group=ENTRY_POINT_GROUP))
    except Exception:
        return False


def register(*, force: bool = False) -> bool:
    '''Make ``CasaImageBackendEntrypoint`` xarray's engine ``casa-image``,
    unless an installed entry point already does.

    True when the engine was added here; False when there was nothing to do
    or it could not be done.  It never raises: the package must import
    whatever state xarray is in (or without xarray).  ``force`` adds it even
    when the entry point is installed.'''
    if not force and entry_point_installed():
        return False
    try:
        from xarray.backends import plugins              # pylint: disable=import-outside-toplevel
        from ._backend import CasaImageBackendEntrypoint  # pylint: disable=import-outside-toplevel
        table = plugins.BACKEND_ENTRYPOINTS
        if ENGINE in table:
            return False
        table[ENGINE] = (None, CasaImageBackendEntrypoint)
        clear = getattr(plugins.list_engines, 'cache_clear', None)
        if clear is not None:                            # xarray was used before us
            clear()
        return True
    except Exception as exc:                             # no xarray, or its table moved
        log.debug('xarray engine %r not registered: %s: %s', ENGINE, type(exc).__name__, exc)
        return False
