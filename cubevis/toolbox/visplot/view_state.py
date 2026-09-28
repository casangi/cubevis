"""
view_state.py
=============
A small, dependency-free framework for saving and restoring pieces of the
visplot GUI state (Part 6 follow-up, 2026-09).

Design in one paragraph
-----------------------
Each independently restorable piece of the GUI is a **state unit**: an object
with a unique dotted ``key`` (``"panel.A.raster.scaling"``), a ``version``, an
``order`` (apply order), a set of ``scopes`` (what kind of state it is), and two
methods, ``capture() -> dict`` and ``apply(state: dict) -> None``.  A
:class:`StateRegistry` holds any number of units.  Capturing walks the
registered units (optionally filtered by key prefix, explicit keys or scope) and
returns one JSON-serializable *envelope*; applying feeds each unit its own slice
back, in order, isolating failures so one bad unit cannot block the rest.  Save
one piece, five pieces or the whole GUI by choosing which units to capture;
adding a new piece of GUI to the mechanism is "write a unit, register it".

This module knows nothing about Bokeh, backends or panels, so it is testable in
isolation and safe to import anywhere.  See ``VIEW_STATE_DESIGN.md`` for the
full design, the planned units and the open questions.

Envelope format (``schema`` 1)
------------------------------
::

    {"format": "cubevis.visplot.viewstate",
     "schema": 1,
     "units": {"panel.A.raster.scaling": {"version": 1, "state": {...}}, ...}}

Unit ``state`` values must be JSON-serializable (checked at capture time, so a
bad unit fails where it is written, not later when someone tries to save a
file).

Scopes
------
Free-form tags a unit declares so callers can select by *kind*.  Conventions
(see the design doc): ``"display"`` (scaling, palettes, zoom), ``"selection"``
(field/spw/antenna/...), ``"layout"`` (panel arrangement, kinds, axes),
``"data"`` (anything that embeds or depends on data, e.g. pending flags).
"Without data" = capture every scope except ``"data"``.

Versioning
----------
Every unit has an integer ``version``.  Restoring a state saved by a NEWER
version than the running unit supports is skipped (reported, never guessed at).
An OLDER saved version is passed through the unit's optional
``migrate(from_version, state)`` hook; without the hook it is skipped.  Unknown
keys in a restored envelope (a unit that no longer exists, or belongs to a panel
that is not present) are skipped and reported, not errors.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Tuple, runtime_checkable

log = logging.getLogger(__name__)

FORMAT = "cubevis.visplot.viewstate"
SCHEMA = 1


@runtime_checkable
class StateUnit(Protocol):
    """One independently restorable piece of GUI state."""

    key: str                    # unique, dotted; prefix = ownership ("panel.A....")
    version: int                # bump when the shape of ``capture()`` changes
    order: int                  # apply order, ascending (ties broken by key)
    scopes: frozenset           # e.g. frozenset({"display"})

    def capture(self) -> dict:
        """Return this unit's state as a JSON-serializable dict."""

    def apply(self, state: dict) -> None:
        """Make the GUI match *state* (as produced by ``capture``)."""


class CallableUnit:
    """Convenience: build a unit from two functions (handy for small pieces
    and for tests).  ``migrate`` is optional."""

    def __init__(
        self, key: str, capture: Callable[[], dict], apply: Callable[[dict], None],
        *, version: int = 1, order: int = 100, scopes: Iterable[str] = (),
        migrate: Optional[Callable[[int, dict], dict]] = None,
    ) -> None:
        self.key = key
        self.version = version
        self.order = order
        self.scopes = frozenset(scopes)
        self._capture, self._apply = capture, apply
        if migrate is not None:
            self.migrate = migrate

    def capture(self) -> dict:
        return self._capture()

    def apply(self, state: dict) -> None:
        self._apply(state)


@dataclass
class ApplyReport:
    """What :meth:`StateRegistry.apply` did.  Never raises for a unit-level
    problem; inspect this instead."""
    applied: List[str] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)     # (key, reason)
    failed: List[Tuple[str, BaseException]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing failed (skips are not failures)."""
        return not self.failed

    def summary(self) -> str:
        return (f"{len(self.applied)} applied, {len(self.skipped)} skipped, "
                f"{len(self.failed)} failed")


class StateRegistry:
    """Holds state units; captures and applies them in bulk."""

    def __init__(self) -> None:
        self._units: Dict[str, StateUnit] = {}

    # ---- registration ------------------------------------------------- #

    def register(self, unit: StateUnit, *, replace: bool = False) -> StateUnit:
        """Add *unit*.  A duplicate key is an error unless ``replace=True``
        (used when a panel is rebuilt)."""
        key = unit.key
        if not key or not isinstance(key, str):
            raise ValueError("a state unit needs a non-empty string key")
        if key in self._units and not replace:
            raise ValueError(f"state unit {key!r} is already registered")
        self._units[key] = unit
        return unit

    def unregister(self, key: str) -> bool:
        return self._units.pop(key, None) is not None

    def unregister_prefix(self, prefix: str) -> int:
        """Remove every unit whose key is *prefix* or starts with ``prefix.``
        (e.g. when a panel is destroyed).  Returns how many."""
        doomed = [k for k in self._units if _under(k, prefix)]
        for k in doomed:
            del self._units[k]
        return len(doomed)

    def get(self, key: str) -> Optional[StateUnit]:
        return self._units.get(key)

    def keys(self, prefix: Optional[str] = None,
             scopes: Optional[Iterable[str]] = None) -> List[str]:
        """Registered keys in apply order, optionally filtered."""
        return [u.key for u in self._select(None, prefix, scopes)]

    def __len__(self) -> int:
        return len(self._units)

    def __contains__(self, key: str) -> bool:
        return key in self._units

    # ---- capture -------------------------------------------------------- #

    def capture(
        self, keys: Optional[Iterable[str]] = None, *,
        prefix: Optional[str] = None,
        scopes: Optional[Iterable[str]] = None,
        exclude_scopes: Optional[Iterable[str]] = None,
    ) -> dict:
        """Capture selected units into one envelope.

        Selection (all optional, combined with AND): explicit *keys*; a key
        *prefix* (``"panel.A"`` = everything under panel A); *scopes* (unit must
        declare at least one); *exclude_scopes* (unit must declare none --
        ``exclude_scopes={"data"}`` is "without data").  A unit whose
        ``capture()`` raises, or returns something not JSON-serializable, raises
        here (capture is the author's bug to see immediately; unlike apply it
        does not swallow).
        """
        units = self._select(keys, prefix, scopes, exclude_scopes)
        out: Dict[str, dict] = {}
        for u in units:
            state = u.capture()
            try:
                json.dumps(state)
            except (TypeError, ValueError) as exc:
                raise TypeError(
                    f"state unit {u.key!r} captured a non-JSON-serializable "
                    f"value: {exc}") from exc
            out[u.key] = {"version": u.version, "state": state}
        return {"format": FORMAT, "schema": SCHEMA, "units": out}

    # ---- apply ---------------------------------------------------------- #

    def apply(
        self, envelope: dict, keys: Optional[Iterable[str]] = None, *,
        prefix: Optional[str] = None,
        scopes: Optional[Iterable[str]] = None,
        exclude_scopes: Optional[Iterable[str]] = None,
    ) -> ApplyReport:
        """Restore from *envelope*.  Same selection arguments as ``capture``
        (limit what is restored, e.g. restore only the display scope of a file
        that holds more).  Units are applied in ``order``; a failure in one is
        recorded and does not stop the others.  Raises only for an envelope that
        is not one of ours."""
        _check_envelope(envelope)
        saved = envelope["units"]
        report = ApplyReport()
        # Keys in the file that no registered unit will take.
        for key in saved:
            if key not in self._units:
                report.skipped.append((key, "no such unit registered"))
        for u in self._select(keys, prefix, scopes, exclude_scopes):
            entry = saved.get(u.key)
            if entry is None:
                continue                      # nothing saved for this unit: fine
            try:
                state = self._reconcile_version(u, entry)
            except _Skip as s:
                report.skipped.append((u.key, str(s)))
                continue
            except BaseException as exc:      # noqa: BLE001 -- a failing migrate()
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                log.warning("view state: unit %r migration failed: %s", u.key, exc)
                report.failed.append((u.key, exc))
                continue
            try:
                u.apply(state)
            except BaseException as exc:      # noqa: BLE001 -- isolate units
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                log.warning("view state: unit %r failed to apply: %s", u.key, exc)
                report.failed.append((u.key, exc))
            else:
                report.applied.append(u.key)
        # Keys that exist but were filtered out by the caller are not reported:
        # filtering is the caller's choice.
        return report

    # ---- JSON helpers --------------------------------------------------- #

    @staticmethod
    def dumps(envelope: dict, **kw) -> str:
        kw.setdefault("indent", 2)
        kw.setdefault("sort_keys", True)
        return json.dumps(envelope, **kw)

    @staticmethod
    def loads(text: str) -> dict:
        envelope = json.loads(text)
        _check_envelope(envelope)
        return envelope

    # ---- internals ------------------------------------------------------ #

    def _select(self, keys, prefix, scopes, exclude_scopes=None) -> List[StateUnit]:
        units = list(self._units.values())
        if keys is not None:
            wanted = set(keys)
            units = [u for u in units if u.key in wanted]
        if prefix is not None:
            units = [u for u in units if _under(u.key, prefix)]
        if scopes is not None:
            sc = set(scopes)
            units = [u for u in units if sc & set(u.scopes)]
        if exclude_scopes is not None:
            ex = set(exclude_scopes)
            units = [u for u in units if not (ex & set(u.scopes))]
        return sorted(units, key=lambda u: (u.order, u.key))

    @staticmethod
    def _reconcile_version(unit: StateUnit, entry: dict) -> dict:
        saved_v = entry.get("version")
        state = entry.get("state")
        if not isinstance(saved_v, int) or not isinstance(state, dict):
            raise _Skip("malformed entry")
        if saved_v == unit.version:
            return state
        if saved_v > unit.version:
            raise _Skip(f"saved by a newer version ({saved_v} > {unit.version})")
        migrate = getattr(unit, "migrate", None)
        if migrate is None:
            raise _Skip(f"older version ({saved_v} < {unit.version}) and no migration")
        return migrate(saved_v, state)


class _Skip(Exception):
    pass


def _under(key: str, prefix: str) -> bool:
    return key == prefix or key.startswith(prefix + ".")


def _check_envelope(envelope: Any) -> None:
    if (not isinstance(envelope, dict) or envelope.get("format") != FORMAT
            or not isinstance(envelope.get("units"), dict)):
        raise ValueError("not a cubevis visplot view-state envelope")
    if envelope.get("schema") != SCHEMA:
        raise ValueError(
            f"unsupported view-state schema {envelope.get('schema')!r} "
            f"(this build reads {SCHEMA})")
