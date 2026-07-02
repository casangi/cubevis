"""flag_db.py
============
``FlagDB`` — in-memory accumulation layer for interactive flag operations.

``FlagDB`` is the sole owner of pending flag state between the moment the
user draws a box-select region and the moment they press the **Flag ⚑**
button.  It is deliberately thin:

* **Append** — each box-close or point-click adds one ``FlagDelta``.
* **Undo** — ``pop()`` removes the most recent delta (stack discipline).
* **Commit** — ``commit()`` drains the list into
  ``ReductionContext.commit_flags()`` and clears the pending queue.
* **Overlay query** — ``flag_selection()`` returns a ``SelectionSpec``
  that covers all pending deltas, used by the rendering pipeline to
  produce the red flagged-data overlay without touching the on-disk
  flag column.

Design constraints
------------------
Flags accumulate in Python, not JavaScript.  This is a hard requirement:
displaying newly flagged data as a red overlay means re-running the
Datashader pipeline (``query_raster`` / ``query_columns`` → ``tf.shade``
→ Porter-Duff composite).  JavaScript has no access to the numpy/xarray
pipeline, so the only role JS plays is firing the j2p message when a
box-select region closes.  Everything after that point — ``FlagDelta``
creation, re-render, overlay composite — runs in Python.

``FlagDB`` itself holds no Bokeh objects and no backend references.  It
is constructed once by ``VisibilityPlotter.__init__`` and passed into the
j2p box-select handler via closure.  The handler calls
``flag_db.append(delta)`` and then triggers the re-render.

Relationship to ``ReductionContext``
-------------------------------------
``FlagDB`` does **not** call ``ReductionContext`` directly on every
``append`` — doing so would write to disk on every box-close, making
undo impossible.  The disk write happens only when
``FlagDB.commit(context)`` is called, which is wired to the **Flag ⚑**
button in ``VisibilityPlotter``.

Overlay rendering
-----------------
The re-render pipeline in ``VisibilityPlotter`` calls
``flag_db.overlay_deltas()`` to obtain the list of pending deltas and
constructs a masked boolean array for the Datashader composite step.
Specifically:

1. For each pending ``FlagDelta`` the raster/scatter backend is queried
   for the set of visibility rows matching the delta's coordinate ranges.
2. Those rows are rendered in red via a separate ``tf.shade`` pass.
3. The red layer is composited over the main image with
   ``tf.stack(main, red_overlay, how='over')``.

``FlagDB`` does not perform this rendering itself — it is the
``VisibilityPlotter`` re-render path that does — but the ``FlagDelta``
objects it holds carry all coordinate information required.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/flag_db.py``
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .reduction_context import FlagDelta, FlagSummary, ReductionContext

log = logging.getLogger(__name__)


class FlagDB:
    """In-memory accumulation layer for pending interactive flag operations.

    Each entry is a ``FlagDelta`` — a coordinate-range description of one
    flag (or unflag) operation.  The list grows on every box-select close
    and shrinks by one on every Undo press.  It is drained to disk (via
    ``ReductionContext.commit_flags()``) only when the **Flag ⚑** button
    is pressed.

    Parameters
    ----------
    max_undo : int
        Maximum number of deltas retained for undo.  Oldest entries are
        silently dropped when the limit is exceeded.  ``0`` means
        unlimited (the default).  In practice the preview does not impose
        a limit — the user can undo every delta in the current session.

    Examples
    --------
    Typical usage inside ``VisibilityPlotter``'s box-select j2p handler::

        delta = FlagDelta(
            flag=True,
            time_range=(t0, t1),
            freq_range=(f0, f1),
            source="raster_box",
        )
        flag_db.append(delta)
        # re-render with overlay …

    Undo (Undo ⟲ button)::

        if flag_db:
            flag_db.pop()
            # re-render without that delta …

    Commit (Flag ⚑ button)::

        summary = flag_db.commit(reduction_context)
        # summary.n_flagged, summary.fraction_flagged, …
    """

    def __init__(self, max_undo: int = 0) -> None:
        self._deltas: list["FlagDelta"] = []
        self._max_undo = max_undo

    # ------------------------------------------------------------------ #
    # Core list operations                                                 #
    # ------------------------------------------------------------------ #

    def append(self, delta: "FlagDelta") -> None:
        """Add a new ``FlagDelta`` to the pending queue.

        If ``max_undo > 0`` and the list is at capacity, the oldest entry
        is dropped before the new one is appended.

        Parameters
        ----------
        delta : FlagDelta
            The coordinate-range flag operation to accumulate.
        """
        if self._max_undo > 0 and len(self._deltas) >= self._max_undo:
            dropped = self._deltas.pop(0)
            log.debug(
                "FlagDB.append: max_undo=%d reached; dropped oldest delta "
                "(source=%r, time_range=%s)",
                self._max_undo, dropped.source, dropped.time_range,
            )
        self._deltas.append(delta)
        log.debug(
            "FlagDB.append: %d pending delta(s); latest source=%r",
            len(self._deltas), delta.source,
        )

    def pop(self) -> "FlagDelta":
        """Remove and return the most recent ``FlagDelta`` (undo one step).

        Raises
        ------
        IndexError
            If the queue is empty (caller should check ``bool(flag_db)``
            or ``flag_db.pending_count`` before calling).
        """
        if not self._deltas:
            raise IndexError("FlagDB.pop(): no pending deltas to undo")
        delta = self._deltas.pop()
        log.debug(
            "FlagDB.pop: undo — removed source=%r; %d delta(s) remaining",
            delta.source, len(self._deltas),
        )
        return delta

    def clear(self) -> None:
        """Discard all pending deltas without writing to disk.

        Called by ``VisibilityPlotter`` if the user closes the tool or
        reloads the data without committing.
        """
        n = len(self._deltas)
        self._deltas.clear()
        log.debug("FlagDB.clear: discarded %d pending delta(s)", n)

    # ------------------------------------------------------------------ #
    # Commit                                                               #
    # ------------------------------------------------------------------ #

    def commit(self, context: "ReductionContext") -> "FlagSummary":
        """Write all pending deltas to disk via ``context.commit_flags()``.

        The pending list is passed to the context **as a copy** so that
        the context implementation can iterate it freely.  The list is
        cleared from ``FlagDB`` only after a successful return — if the
        context raises, the deltas remain pending and the user can retry.

        Parameters
        ----------
        context : ReductionContext
            The active reduction context (``Casa6ReductionContext``,
            ``RadpsReductionContext``, etc.).  Must not be a
            ``NullReductionContext`` — the caller (``VisibilityPlotter``)
            is responsible for gating the **Flag ⚑** button on
            ``context.supports_calibration()``… or more precisely on
            whether ``commit_flags`` is meaningfully implemented; for the
            preview the button is simply disabled in the toolbar.

        Returns
        -------
        FlagSummary
            Counts and fractions returned by the context implementation.

        Raises
        ------
        NotImplementedError
            Re-raised from ``NullReductionContext.commit_flags()`` if
            called against the null context (should not happen in normal
            use since the Flag button is disabled in that case).
        RuntimeError
            If the pending list is empty — committing zero deltas is
            almost certainly a caller bug.
        """
        if not self._deltas:
            raise RuntimeError(
                "FlagDB.commit(): no pending deltas to commit. "
                "Check 'if flag_db:' before calling commit()."
            )

        snapshot = list(self._deltas)   # copy; context may iterate multiple times
        log.debug(
            "FlagDB.commit: writing %d delta(s) via %s",
            len(snapshot), type(context).__name__,
        )

        summary = context.commit_flags(snapshot)

        # Clear only after a successful commit
        self._deltas.clear()
        log.debug(
            "FlagDB.commit: done — n_flagged=%d  fraction=%.4f",
            summary.n_flagged, summary.fraction_flagged,
        )
        return summary

    # ------------------------------------------------------------------ #
    # Overlay query                                                        #
    # ------------------------------------------------------------------ #

    def overlay_deltas(self) -> list["FlagDelta"]:
        """Return a shallow copy of the pending delta list.

        Used by the ``VisibilityPlotter`` re-render pipeline to construct
        the red flagged-data overlay without the risk of the list changing
        mid-render (which could happen if a j2p handler fires concurrently
        in a future async implementation).

        Returns an empty list when nothing is pending — callers can skip
        the overlay composite step entirely in that case.
        """
        return list(self._deltas)

    def peek(self, index: int = -1) -> "FlagDelta":
        """Return the delta at *index* without removing it.

        Default ``index=-1`` returns the most recently appended delta.
        Positive indices count from the oldest delta (``index=0``).

        Used by ``VisibilityPlotter`` to implement a step-through cursor
        so the user can inspect accumulated flag operations one by one
        (e.g. via ← / → toolbar buttons or hotkeys) before committing.

        Parameters
        ----------
        index : int
            List index into the pending delta queue.

        Returns
        -------
        FlagDelta
            The delta at *index* — not removed from the queue.

        Raises
        ------
        IndexError
            If the queue is empty or *index* is out of range.
        """
        if not self._deltas:
            raise IndexError("FlagDB.peek(): no pending deltas")
        return self._deltas[index]

    def has_pending(self) -> bool:
        """Return ``True`` if there are uncommitted deltas.

        Convenience alias for ``bool(flag_db)``; use whichever reads more
        clearly at the call site.
        """
        return bool(self._deltas)

    # ------------------------------------------------------------------ #
    # Convenience / introspection                                          #
    # ------------------------------------------------------------------ #

    @property
    def pending_count(self) -> int:
        """Number of uncommitted deltas currently in the queue."""
        return len(self._deltas)

    def __bool__(self) -> bool:
        """``True`` if there are any pending deltas."""
        return bool(self._deltas)

    def __len__(self) -> int:
        return len(self._deltas)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"FlagDB(pending={len(self._deltas)}, "
            f"max_undo={self._max_undo!r})"
        )
