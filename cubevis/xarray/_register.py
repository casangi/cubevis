"""Make a backend known to xarray when its entry point is not installed.

xarray finds a backend by its entry point (``pyproject.toml``,
``[project.entry-points."xarray.backends"]``), which exists only for an
installed package.  Run from a checkout on ``PYTHONPATH``, there is none,
and ``xr.open_datatree(path, engine="casa-caltab")`` does not know the
engine.  ``register`` covers that case: importing a backend's package
(``from cubevis.xarray.caltab import open_caltab``) is then enough.

* Installed (``pip install .`` or ``pip install -e .``): the entry point
  is there, ``register`` does nothing, and xarray is not imported.
* Not installed: xarray is imported and the backend added to its table
  of engines, and xarray's cached list is cleared so the engine shows
  even if xarray was used first.

The table (``xarray.backends.plugins.BACKEND_ENTRYPOINTS``) is not part
of xarray's documented interface.  If a release moves it, ``register``
returns False and nothing else changes: the entry point, ``open_caltab``
and ``engine=<the class>`` do not depend on it.  A test watches for that.
"""
from __future__ import annotations

import logging
from importlib import import_module

log = logging.getLogger("cubevis.xarray")

ENTRY_POINT_GROUP = "xarray.backends"


def entry_point_installed(name: str) -> bool:
    """Does an installed package give xarray the engine *name*?"""
    try:
        from importlib.metadata import entry_points
        return any(e.name == name for e in entry_points(group=ENTRY_POINT_GROUP))
    except Exception:
        return False


def register(name: str, module: str, attr: str, *, force: bool = False) -> bool:
    """Make ``module.attr`` (a ``BackendEntrypoint`` class) xarray's engine
    *name*, unless an installed entry point already does.

    Returns True when the engine was added here, False when there was
    nothing to do or it could not be done.  It never raises: a backend's
    package must import whatever state xarray is in.  *force* adds it
    even when the entry point is installed.
    """
    if not force and entry_point_installed(name):
        return False
    try:
        from xarray.backends import plugins
        table = plugins.BACKEND_ENTRYPOINTS
        if name in table:
            return False
        table[name] = (None, getattr(import_module(module), attr))
        clear = getattr(plugins.list_engines, "cache_clear", None)
        if clear is not None:                                        # xarray was used before us
            clear()
        return True
    except Exception as exc:                                         # no xarray, or its table moved
        log.debug("xarray engine %r not registered: %s: %s", name, type(exc).__name__, exc)
        return False
