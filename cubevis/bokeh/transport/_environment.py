########################################################################
# Shared IPython/Jupyter kernel-detection helper
#
# Single source of truth for "are we running inside a real Jupyter/Colab
# kernel session" (as opposed to a plain `ipython` terminal REPL, or a
# vanilla `python` interpreter with no IPython at all).
#
# `get_ipython() is not None` is NOT sufficient to detect a notebook: it
# is also true in a plain terminal `ipython` REPL. The distinguishing
# feature is the `.kernel` attribute, which is present only on
# kernel-backed shells (`ZMQInteractiveShell` — JupyterLab, Classic
# Notebook, Colab) and absent on `TerminalInteractiveShell`.
#
# Before this module existed, this same `hasattr(shell, 'kernel')` check
# was duplicated independently at six call sites across _comm_mgr.py and
# _low_level_transport.py. One of the six (the comm-target registration
# in CommsTransport.display_bridge()) was missing the guard, and
# CommMgr._detect_transport() never had it at all — which is what caused
# plain `ipython` sessions to be misclassified as 'jupyter', leading to
# "Could not create Jupyter comm" errors. Route all such checks through
# this module so they can't drift out of sync again.
########################################################################

import logging

logger = logging.getLogger(__name__)

__all__ = ["get_ipython_kernel_shell", "is_jupyter_kernel"]


def get_ipython_kernel_shell():
    """
    Return the active IPython shell *only* if it is backed by a real kernel
    (i.e. a JupyterLab / Classic Notebook / Colab session) — otherwise None.

    Returns None for:
      - IPython not installed
      - no active IPython shell (plain `python` interpreter)
      - a plain terminal `ipython` REPL (TerminalInteractiveShell — has no
        `.kernel` attribute)

    Callers that only need a yes/no answer should use `is_jupyter_kernel()`.
    Callers that need to reach into `shell.kernel` (e.g. for `_parent_header`
    / `_parents` manipulation) can use the returned shell directly, e.g.:

        shell = get_ipython_kernel_shell()
        if shell is not None:
            kernel = shell.kernel
            ...
    """
    try:
        from IPython import get_ipython
    except ImportError:
        return None

    try:
        shell = get_ipython()
    except Exception:
        logger.exception("get_ipython_kernel_shell: get_ipython() raised")
        return None

    if shell is not None and hasattr(shell, "kernel"):
        return shell

    return None


def is_jupyter_kernel() -> bool:
    """True iff running inside a real Jupyter/Colab kernel session."""
    return get_ipython_kernel_shell() is not None
