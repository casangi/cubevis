"""flag_db.py
============
``FlagDB`` -- the ordered store of *accepted* pending flag operations.

What it holds
-------------
An ordered list of ``FlagDelta`` objects (see ``flag_model.py``).  The
effective flag state of any sample is the on-disk flag with these deltas
applied **in order** (``flag_model.fold_deltas``): a later unflag overrides
an earlier flag and vice versa.  Nothing here ever touches disk; pending
flags become permanent only through ``commit(context)`` ->
``ReductionContext.commit_flags()``.

Only accepted deltas live here.  Proposals (a box plus a filter, awaiting
review) are held by the caller until the reviewer accepts them, so undo
never has to step over rejected work.

Operations and history
----------------------
Flag, Unflag, Undo, Redo and Clear all operate on this DB:

* ``add(delta)``   -- append (Flag and Unflag both add a delta); clears redo.
* ``undo()``       -- reverse the most recent operation (an add or a clear).
* ``redo()``       -- re-apply the most recently undone operation.
* ``clear()``      -- drop every pending delta; undoable.
* ``commit(ctx)``  -- hand the ordered list to the reduction context; on
  success the DB and its history are emptied.

``version`` increases on every change.  It travels to the data backend
(``SelectionSpec.pending_version``) so cached frames built under an older
pending state are never reused.

Thread safety
-------------
All mutators take an internal re-entrant lock; ``deltas()`` returns an
immutable snapshot.  Listeners (``add_listener``) are called after the lock
is released, with the new version.

Persistence
-----------
``to_jsonl()`` / ``from_jsonl()`` write and read the ordered deltas (with
provenance) as JSON Lines -- the audit/persistence form, also used by the
view-state ``data.flags.pending`` unit.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/flag_db.py``
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING, Callable, Iterable, Optional

from .flag_model import FlagDelta

if TYPE_CHECKING:
    from .reduction_context import FlagSummary, ReductionContext

log = logging.getLogger(__name__)

JSONL_FORMAT = "cubevis.visplot.flagdb"
JSONL_VERSION = 2


class FlagDB:
    """Ordered, undoable store of accepted pending flag deltas.

    Parameters
    ----------
    max_undo : int
        Maximum number of operations kept for undo (``0`` = unlimited).
        Older history is forgotten; the deltas themselves stay.
    """

    def __init__(self, max_undo: int = 0) -> None:
        self._deltas: list[FlagDelta] = []
        self._undo: list[tuple] = []
        self._redo: list[tuple] = []
        self._max_undo = int(max_undo)
        self._seq = 0
        self._version = 0
        self._lock = threading.RLock()
        self._listeners: list[Callable[[int], None]] = []

    # ------------------------------------------------------------------ #
    # Listeners / versioning                                               #
    # ------------------------------------------------------------------ #

    @property
    def version(self) -> int:
        return self._version

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def add_listener(self, fn: Callable[[int], None]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn) -> None:
        try:
            self._listeners.remove(fn)
        except ValueError:
            pass

    def _changed(self) -> int:
        # Called with the lock held; returns the new version.
        self._version += 1
        return self._version

    def _notify(self, version: int) -> None:
        for fn in list(self._listeners):
            try:
                fn(version)
            except Exception:
                log.exception("FlagDB listener failed")

    def _push_undo(self, op: tuple) -> None:
        self._undo.append(op)
        if self._max_undo > 0 and len(self._undo) > self._max_undo:
            del self._undo[0]

    # ------------------------------------------------------------------ #
    # Mutators                                                             #
    # ------------------------------------------------------------------ #

    def add(self, delta: FlagDelta) -> FlagDelta:
        """Append *delta* (assigning its sequence number); returns it."""
        with self._lock:
            self._seq += 1
            delta = delta.with_seq(self._seq)
            self._deltas.append(delta)
            self._push_undo(("add", delta))
            self._redo.clear()
            v = self._changed()
        log.debug("FlagDB.add: %s (%d pending)", delta.describe(), len(self._deltas))
        self._notify(v)
        return delta

    # v1 name
    def append(self, delta: FlagDelta) -> FlagDelta:
        return self.add(delta)

    def undo(self) -> Optional[tuple]:
        """Reverse the most recent operation.  Returns ``(kind, payload)``
        or ``None`` if there is nothing to undo."""
        with self._lock:
            if not self._undo:
                return None
            op = self._undo.pop()
            kind, payload = op
            if kind == "add":
                # An add is always the newest delta unless history was
                # rewritten; search from the end to be safe.
                for i in range(len(self._deltas) - 1, -1, -1):
                    if self._deltas[i].delta_id == payload.delta_id:
                        del self._deltas[i]
                        break
            elif kind == "clear":
                self._deltas = list(payload) + self._deltas
            self._redo.append(op)
            v = self._changed()
        self._notify(v)
        return op

    def redo(self) -> Optional[tuple]:
        with self._lock:
            if not self._redo:
                return None
            op = self._redo.pop()
            kind, payload = op
            if kind == "add":
                self._deltas.append(payload)
            elif kind == "clear":
                ids = {d.delta_id for d in payload}
                self._deltas = [d for d in self._deltas if d.delta_id not in ids]
            self._push_undo(op)
            v = self._changed()
        self._notify(v)
        return op

    def pop(self) -> FlagDelta:
        """v1 compatibility: undo the most recent *add* and return its delta."""
        with self._lock:
            if not self._deltas:
                raise IndexError("FlagDB.pop(): no pending deltas to undo")
            if self._undo and self._undo[-1][0] == "add":
                return self.undo()[1]
            delta = self._deltas.pop()
            self._redo.append(("add", delta))
            v = self._changed()
        self._notify(v)
        return delta

    def clear(self, *, record: bool = True) -> int:
        """Drop all pending deltas.  Undoable when *record* (default).
        ``record=False`` also forgets history (used after data reload)."""
        with self._lock:
            n = len(self._deltas)
            if record and n:
                self._push_undo(("clear", tuple(self._deltas)))
                self._redo.clear()
            if not record:
                self._undo.clear()
                self._redo.clear()
            self._deltas = []
            v = self._changed()
        self._notify(v)
        return n

    def replace_all(self, deltas: Iterable[FlagDelta]) -> None:
        """Load *deltas* (e.g. restored pending state); forgets history."""
        with self._lock:
            self._deltas = []
            for d in deltas:
                self._seq = max(self._seq, d.seq) if d.seq else self._seq + 1
                self._deltas.append(d if d.seq else d.with_seq(self._seq))
            self._undo.clear()
            self._redo.clear()
            v = self._changed()
        self._notify(v)

    # ------------------------------------------------------------------ #
    # Commit                                                               #
    # ------------------------------------------------------------------ #

    def commit(self, context: "ReductionContext") -> "FlagSummary":
        """Hand the ordered pending deltas to ``context.commit_flags()``.

        The DB is emptied (and its history forgotten) only after the
        context returns successfully; if it raises, everything stays
        pending and the user can retry.
        """
        with self._lock:
            if not self._deltas:
                raise RuntimeError("FlagDB.commit(): no pending deltas to commit.")
            snapshot = list(self._deltas)
        summary = context.commit_flags(snapshot)
        with self._lock:
            ids = {d.delta_id for d in snapshot}
            self._deltas = [d for d in self._deltas if d.delta_id not in ids]
            self._undo.clear()
            self._redo.clear()
            v = self._changed()
        self._notify(v)
        return summary

    # ------------------------------------------------------------------ #
    # Queries                                                              #
    # ------------------------------------------------------------------ #

    def deltas(self) -> tuple:
        """Immutable snapshot of the ordered pending deltas."""
        with self._lock:
            return tuple(self._deltas)

    def snapshot(self) -> tuple:
        """``(version, deltas)`` taken atomically."""
        with self._lock:
            return self._version, tuple(self._deltas)

    def overlay_deltas(self) -> list:
        return list(self.deltas())

    def peek(self, index: int = -1) -> FlagDelta:
        with self._lock:
            if not self._deltas:
                raise IndexError("FlagDB.peek(): no pending deltas")
            return self._deltas[index]

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def has_pending(self) -> bool:
        return bool(self._deltas)

    @property
    def pending_count(self) -> int:
        return len(self._deltas)

    def __bool__(self) -> bool:
        return bool(self._deltas)

    def __len__(self) -> int:
        return len(self._deltas)

    def __iter__(self):
        return iter(self.deltas())

    def __repr__(self) -> str:  # pragma: no cover
        return (f"FlagDB(pending={len(self._deltas)}, undo={len(self._undo)}, "
                f"redo={len(self._redo)}, version={self._version})")

    # ------------------------------------------------------------------ #
    # Persistence                                                          #
    # ------------------------------------------------------------------ #

    def to_jsonl(self, header: Optional[dict] = None) -> str:
        lines = [json.dumps({"format": JSONL_FORMAT, "version": JSONL_VERSION,
                             **(header or {})})]
        for d in self.deltas():
            lines.append(json.dumps(d.to_dict(json_safe=True)))
        return "\n".join(lines) + "\n"

    @staticmethod
    def parse_jsonl(text: str) -> tuple:
        """``(header, [FlagDelta, ...])`` from ``to_jsonl`` output."""
        header, deltas = {}, []
        for i, line in enumerate(l for l in text.splitlines() if l.strip()):
            obj = json.loads(line)
            if i == 0 and obj.get("format") == JSONL_FORMAT:
                header = obj
                continue
            deltas.append(FlagDelta.from_dict(obj))
        return header, deltas

    @classmethod
    def from_jsonl(cls, text: str, max_undo: int = 0) -> "FlagDB":
        db = cls(max_undo=max_undo)
        _hdr, deltas = cls.parse_jsonl(text)
        db.replace_all(deltas)
        return db
