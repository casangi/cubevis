"""CASA, only when it is already there.

visplot does not need CASA.  Importing ``casatasks`` (or ``casatools``) is
slow, prints, and on an account whose CASA data are not set up raises at
import -- ``casaconfig``'s ``AutoUpdatesNotAllowed`` when ``~/.casa/data``
does not exist, an ``Exception`` rather than an ``ImportError`` -- which,
caught as ``ImportError`` before 2026-10-26, broke ``from cubevis import
visplot`` for anyone with casatasks installed but not configured.

So cubevis never imports CASA to find out whether to use it:

* ``loaded()`` -- the ``casatasks`` module if this process imported it
  already (inside CASA, or the user did), else None.  Logging to casalog
  and the task log lines follow this: in CASA they appear as before; in
  plain Python nothing of CASA is touched.
* ``usable()`` -- for code that needs CASA to do its job (committing flags
  with ``flagdata``/``flagmanager``, ``backend="casa6"``): import it, and
  report any failure as ``(None, reason)`` instead of raising.
* ``task_logging()`` -- casatasks' ``start_log`` / ``end_log`` /
  ``except_log`` when it is loaded, else quiet stand-ins with the same
  signatures.
"""
import sys


def loaded():
    """The ``casatasks`` module if it was imported already, else None."""
    return sys.modules.get("casatasks")


def usable():
    """``(casatasks module or None, reason)``: import it if need be; any
    failure (missing, a broken install, CASA data not set up) is a reason,
    not an exception."""
    mod = loaded()
    if mod is not None:
        return mod, ""
    try:
        import casatasks
        return casatasks, ""
    except Exception as exc:          # ImportError, AutoUpdatesNotAllowed, a broken binary
        return None, f"casatasks not usable here ({type(exc).__name__}: {exc})"


def _start_log(tname, arguments):
    return ()


def _end_log(state, tname, result):
    return result


def _except_log(tname, exc):
    return None


def task_logging():
    """``(start_log, end_log, except_log)``: casatasks' when CASA is loaded,
    else stand-ins that log nothing."""
    if loaded() is not None:
        try:
            from casatasks.private.task_logging import start_log, end_log, except_log
            return start_log, end_log, except_log
        except Exception:
            pass
    return _start_log, _end_log, _except_log


# casatools for the image code (iclean, createmask, createregion): imported
# when an image is first opened, not when cubevis is imported -- visplot
# loads those modules too and needs none of it.

def casatools_available() -> bool:
    try:
        import casatools  # noqa: F401
        return True
    except Exception:                 # ImportError, AutoUpdatesNotAllowed, ...
        return False


def imagetool(*args, **kwargs):
    """``casatools.image(...)``, importing casatools on first use."""
    from casatools import image
    return image(*args, **kwargs)


def regionmanager(*args, **kwargs):
    """``casatools.regionmanager(...)``, importing casatools on first use."""
    from casatools import regionmanager as _rm
    return _rm(*args, **kwargs)
