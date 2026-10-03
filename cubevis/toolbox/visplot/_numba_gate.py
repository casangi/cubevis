"""Serialise datashader's numba-parallel kernels across threads.

The crash this prevents
-----------------------
::

    Numba workqueue threading layer is terminating: Concurrent access has
    been detected.
    zsh: abort      ipython

Several datashader kernels (``tf.shade``'s colour/eq_hist helpers,
``Canvas.raster``'s resampling) are ``parallel=True`` + ``nogil=True``.
visplot renders each panel in a worker thread (``asyncio.to_thread``)
under that panel's own ``asyncio.Lock`` -- which orders work *within* a
panel, not *between* panels.  Two panels (or a preset's two panels plus a
pan/zoom re-shade) can therefore enter a parallel kernel at once.  Numba's
``workqueue`` threading layer -- the fallback when neither TBB nor OpenMP
is installed -- is not thread-safe, and answers concurrent entry by
aborting the whole process, which no ``try/except`` can catch.

The fix is a single process-wide re-entrant lock around the datashader
entry points visplot uses.  It is correct under every threading layer
(TBB/OpenMP merely tolerate the overlap; workqueue cannot), costs little
(the kernels already use all cores, so overlapping them bought nothing),
and the lock is held only for the in-memory kernel call -- never across
data reads, which stay concurrent.

``install()`` is idempotent.  Set ``CUBEVIS_NO_NUMBA_GATE=1`` to disable
it (e.g. to measure its effect).
"""
from __future__ import annotations

import functools
import logging
import os
import threading

log = logging.getLogger(__name__)

#: Re-entrant: ``tf.shade`` may call helpers that are themselves wrapped.
numba_gate = threading.RLock()

_installed = False
_install_lock = threading.Lock()


def _gated(fn):
    if getattr(fn, "_cv_gated", False):
        return fn

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with numba_gate:
            return fn(*args, **kwargs)

    wrapper._cv_gated = True
    return wrapper


def install() -> bool:
    """Wrap the datashader entry points visplot uses.  Idempotent.

    Returns ``True`` if the gate is active, ``False`` if it was disabled
    or datashader is unavailable.
    """
    global _installed
    if os.environ.get("CUBEVIS_NO_NUMBA_GATE"):
        return False
    with _install_lock:
        if _installed:
            return True
        try:
            import datashader as ds
            import datashader.transfer_functions as tf
        except ImportError:
            return False
        for owner, names in (
            (ds.Canvas, ("points", "raster")),
            (tf, ("shade", "stack")),
        ):
            for name in names:
                fn = getattr(owner, name, None)
                if fn is not None:
                    setattr(owner, name, _gated(fn))
        _installed = True
        log.debug("numba gate installed around datashader kernels")
        return True
