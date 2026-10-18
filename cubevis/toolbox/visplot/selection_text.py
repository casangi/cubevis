"""selection_text.py
==================
The sidebar's Scan, Time range and UV range boxes (and the ``scan=``,
``timerange=``, ``uvrange=`` arguments) turned into ``SelectionSpec``
values.  HRS A1, 2026-10-11: until then the text was stored, shown and
ignored.

Field (2026-10-18; ``field=``) is parsed here too: until then only one
name or id was understood and anything else matched nothing, silently.

The syntax is CASA's (MSSelection), as plotms and ``flagdata`` take it,
with the forms a commissioning user types; anything else is refused with
a message that says what is accepted, never silently ignored.

Field
    ``3c279``  ``0``  ``0~2``  ``3c279,J0522-364``  ``J05*`` (glob)
    ``!Ceres`` (all but); names or FIELD_IDs, mixed

Scan
    ``1,3,7``  ``1~7``  ``>30``  ``<10``  ``!5`` (all but)

Time range (UTC; one range)
    ``2012/11/19/07:37:00~2012/11/19/08:00:00``  full dates
    ``07:37:00~08:00``  times of day: the day of the data (or the day
    within the observation that contains them)
    ``1/00:10:00``  AIPS-style day number from the first day of the data,
    as the time axis labels it when the data cross midnight
    ``>07:50``  ``<08:10``  open ranges
    ``07:37:00+00:05:00``  a start and a length
    ``07:37:03``  one time: the integration around it

UV range (metres unless a unit is given; several ranges with commas)
    ``0~50klambda``  ``100~300m``  ``>200m``  ``<1.5km``  ``0~50kl``
    Units: m, km, lambda (l, λ), klambda (kl, kλ), Mlambda (Ml, Mλ).
    In wavelengths, a row's distance is taken at the centre frequency of
    its spectral window, as one value per row (time, baseline): a row is
    either in or out, as with CASA's row selection.

All times are UNIX seconds (UTC), the unit the backends' time
coordinates use.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/selection_text.py``
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import math
import re
from typing import Optional, Sequence

_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
_DAY = 86400.0


class SelectionTextError(ValueError):
    """Text that does not parse, or matches nothing; the message is for
    the user (status area or exception)."""


# --------------------------------------------------------------------------
# Field
# --------------------------------------------------------------------------

def parse_field_text(text: Optional[str], fields: Sequence) -> Optional[list[str]]:
    """Field names selected by *text*, in the data's order.

    ``None`` for empty text (all fields).  *fields* are ``(field_id,
    name)`` pairs (``field_id`` may be ``None`` when the backend does not
    know it).  Tokens, separated by commas: an exact name, a FIELD_ID, an
    id range ``a~b``, ``>n`` / ``<n``, a glob (``*``, ``?``, ``[..]``)
    on names; ``!`` in front excludes.  A name is tried before an id, so
    a field named ``"3"`` is still reachable.  Raises
    ``SelectionTextError`` for a token that matches no field, or a
    selection that leaves none.
    """
    text = (text or "").strip()
    if not text:
        return None
    order = [str(n) for _, n in fields]
    by_id: dict[int, str] = {}
    for fid, name in fields:
        if fid is not None:
            try:
                by_id[int(fid)] = str(name)
            except (TypeError, ValueError):
                pass
    shown = ", ".join(order[:8]) + (" …" if len(order) > 8 else "")
    keep: set[str] = set()
    drop: set[str] = set()
    positive = False
    for tok in [t.strip() for t in text.split(",") if t.strip()]:
        neg = tok.startswith("!")
        body = tok[1:].strip() if neg else tok
        m_rng = re.fullmatch(r"(\d+)\s*~\s*(\d+)", body)
        m_cmp = re.fullmatch(r"([<>])\s*(\d+)", body)
        if body in order:
            got = {body}
        elif m_rng:
            lo, hi = sorted((int(m_rng.group(1)), int(m_rng.group(2))))
            got = {n for i, n in by_id.items() if lo <= i <= hi}
        elif m_cmp:
            k = int(m_cmp.group(2))
            got = {n for i, n in by_id.items() if (i > k if m_cmp.group(1) == ">" else i < k)}
        elif re.fullmatch(r"\d+", body):
            got = {by_id[int(body)]} if int(body) in by_id else set()
        elif any(c in body for c in "*?["):
            got = set(fnmatch.filter(order, body))
        else:
            got = set()
        if not got:
            raise SelectionTextError(
                f"Field: {tok!r} matches no field in the data (fields: "
                f"{shown}; give names, FIELD_IDs, a~b, globs such as "
                f"J05*, or !name)")
        if neg:
            drop |= got
        else:
            positive = True
            keep |= got
    chosen = (keep if positive else set(order)) - drop
    if not chosen:
        raise SelectionTextError("Field: the selection leaves no field")
    return [n for n in order if n in chosen]


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------

def parse_scan_text(text: Optional[str], scans: Sequence) -> Optional[list[str]]:
    """Scan names selected by *text*, from the data's *scans*.

    ``None`` for empty text (all scans).  *scans* are the data's scan
    names (strings of integers, e.g. ``["4", "7", ...]``).  Raises
    ``SelectionTextError`` for text that does not parse or selects no
    scan of the data.
    """
    text = (text or "").strip()
    if not text:
        return None
    avail = {}
    for s in scans:
        try:
            avail[int(str(s).strip())] = str(s)
        except ValueError:
            continue
    ids = sorted(avail)
    shown = ", ".join(str(i) for i in ids[:12]) + (" …" if len(ids) > 12 else "")
    keep: set[int] = set()
    drop: set[int] = set()
    positive = False
    for tok in [t.strip() for t in text.split(",") if t.strip()]:
        neg = tok.startswith("!")
        body = tok[1:].strip() if neg else tok
        m_rng = re.fullmatch(r"(\d+)\s*~\s*(\d+)", body)
        m_cmp = re.fullmatch(r"([<>])\s*(\d+)", body)
        if m_rng:
            lo, hi = sorted((int(m_rng.group(1)), int(m_rng.group(2))))
            got = {i for i in ids if lo <= i <= hi}
        elif m_cmp:
            n = int(m_cmp.group(2))
            got = {i for i in ids if (i > n if m_cmp.group(1) == ">" else i < n)}
        elif re.fullmatch(r"\d+", body):
            got = {int(body)} & set(ids)
        else:
            raise SelectionTextError(
                f"Scan: {tok!r} is not a scan number, range (a~b), "
                f">n, <n or !n")
        if not got:
            raise SelectionTextError(
                f"Scan: {tok!r} matches no scan in the data (scans: {shown})")
        if neg:
            drop |= got
        else:
            positive = True
            keep |= got
    chosen = (keep if positive else set(ids)) - drop
    if not chosen:
        raise SelectionTextError("Scan: the selection leaves no scan")
    return [avail[i] for i in sorted(chosen)]


# --------------------------------------------------------------------------
# Time range
# --------------------------------------------------------------------------

_TOD = r"(\d{1,2}):(\d{1,2})(?::(\d{1,2}(?:\.\d*)?))?"
_RE_FULL = re.compile(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:[/T ]" + _TOD + r")?$")
_RE_DAYN = re.compile(r"(\d{1,3})/" + _TOD + r"$")
_RE_TOD = re.compile(_TOD + r"$")


def _tod_seconds(h, m, s) -> float:
    h, m = int(h), int(m)
    s = float(s) if s else 0.0
    if h > 24 or m > 59 or s >= 60.0:
        raise SelectionTextError("Time range: hours, minutes or seconds out of range")
    return h * 3600.0 + m * 60.0 + s


def _day_start(t: float) -> float:
    return math.floor(t / _DAY) * _DAY


def _parse_instant(tok: str, obs: tuple, anchor: Optional[float]) -> float:
    """One time; *anchor* (UNIX s) gives the date of a time of day that
    follows a dated one (``A~B``)."""
    tok = tok.strip()
    m = _RE_FULL.match(tok)
    if m:
        y, mo, d = (int(m.group(i)) for i in (1, 2, 3))
        try:
            day = (_dt.datetime(y, mo, d, tzinfo=_dt.timezone.utc) - _EPOCH).total_seconds()
        except ValueError as exc:
            raise SelectionTextError(f"Time range: {tok!r}: {exc}") from None
        tod = _tod_seconds(m.group(4), m.group(5), m.group(6)) if m.group(4) else 0.0
        return day + tod
    m = _RE_DAYN.match(tok)
    if m:
        return (_day_start(obs[0]) + int(m.group(1)) * _DAY
                + _tod_seconds(m.group(2), m.group(3), m.group(4)))
    m = _RE_TOD.match(tok)
    if m:
        tod = _tod_seconds(m.group(1), m.group(2), m.group(3))
        if anchor is not None:
            t = _day_start(anchor) + tod
            return t if t >= anchor else t + _DAY
        # The day of the data that contains this time of day, if any;
        # else the first day.
        d0 = _day_start(obs[0])
        n_days = int((_day_start(obs[1]) - d0) // _DAY) + 1
        for k in range(max(n_days, 1)):
            t = d0 + k * _DAY + tod
            if obs[0] - 1.0 <= t <= obs[1] + 1.0:
                return t
        return d0 + tod
    raise SelectionTextError(
        f"Time range: {tok!r} is not a time; use YYYY/MM/DD/hh:mm:ss, "
        f"hh:mm:ss, or day/hh:mm:ss")


def _fmt(t: float) -> str:
    return (_EPOCH + _dt.timedelta(seconds=t)).strftime("%Y/%m/%d/%H:%M:%S")


def format_timerange(tr) -> str:
    """``(t0, t1)`` UNIX seconds as Time range box text, to the
    millisecond so it parses back to the same range."""
    def f(ms):
        d = _EPOCH + _dt.timedelta(milliseconds=ms)
        return d.strftime("%Y/%m/%d/%H:%M:%S") + f".{d.microsecond // 1000:03d}"
    # Start rounded down and end up, so no sample at an end is lost.
    return (f"{f(math.floor(float(tr[0]) * 1000.0))}~"
            f"{f(math.ceil(float(tr[1]) * 1000.0))}")


def parse_timerange_text(text: Optional[str], obs: tuple,
                         half_integration: float = 0.5
                         ) -> Optional[tuple[float, float]]:
    """``(t0, t1)`` in UNIX seconds for *text*, or ``None`` (all times).

    *obs* is the data's ``(first, last)`` time.  Raises
    ``SelectionTextError`` for text that does not parse, an empty range,
    or a range that misses the data entirely.
    """
    text = (text or "").strip()
    if not text:
        return None
    if "," in text:
        raise SelectionTextError(
            "Time range: one range at a time (a~b); several are not supported yet")
    obs = (float(obs[0]), float(obs[1]))
    if text.startswith(">"):
        t0, t1 = _parse_instant(text[1:], obs, None), obs[1]
    elif text.startswith("<"):
        t0, t1 = obs[0], _parse_instant(text[1:], obs, None)
    elif "~" in text:
        a, b = text.split("~", 1)
        t0 = _parse_instant(a, obs, None)
        b = b.strip()
        dated = bool(_RE_FULL.match(b) or _RE_DAYN.match(b))
        t1 = _parse_instant(b, obs, None if dated else t0)
    elif "+" in text:
        a, b = text.split("+", 1)
        t0 = _parse_instant(a, obs, None)
        b = b.strip()
        m = _RE_TOD.match(b)
        if m:
            dur = _tod_seconds(m.group(1), m.group(2), m.group(3))
        else:
            try:
                dur = float(b.rstrip("s"))
            except ValueError:
                raise SelectionTextError(
                    f"Time range: {b!r} is not a length (hh:mm:ss or seconds)") from None
        t1 = t0 + dur
    else:
        t = _parse_instant(text, obs, None)
        t0, t1 = t - half_integration, t + half_integration
    if t1 < t0:
        raise SelectionTextError(
            f"Time range: the end ({_fmt(t1)}) is before the start ({_fmt(t0)})")
    if t1 < obs[0] - half_integration or t0 > obs[1] + half_integration:
        raise SelectionTextError(
            f"Time range: {_fmt(t0)}–{_fmt(t1)} is outside the data "
            f"({_fmt(obs[0])} – {_fmt(obs[1])})")
    return (t0, t1)


# --------------------------------------------------------------------------
# UV range
# --------------------------------------------------------------------------

_UV_UNITS = {
    "": (1.0, "m"), "m": (1.0, "m"), "km": (1e3, "m"),
    "lambda": (1.0, "lambda"), "l": (1.0, "lambda"), "λ": (1.0, "lambda"),
    "klambda": (1e3, "lambda"), "kl": (1e3, "lambda"), "kλ": (1e3, "lambda"),
    "mlambda": (1e6, "lambda"), "ml": (1e6, "lambda"), "mλ": (1e6, "lambda"),
}
_NUM = r"(\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def _uv_unit(u: str) -> tuple[float, str]:
    key = u.strip()
    # "Mlambda" / "Ml": capital M is mega; lower-case "ml" is accepted
    # for it too (no one means milli-lambda).
    key = key.lower()
    if key not in _UV_UNITS:
        raise SelectionTextError(
            f"UV range: unknown unit {u!r} (m, km, lambda, klambda, Mlambda)")
    return _UV_UNITS[key]


def parse_uvrange_text(text: Optional[str]) -> Optional[list]:
    """``[[lo, hi, unit], ...]`` for *text*, or ``None`` (all).

    *unit* is ``"m"`` or ``"lambda"``; *lo* / *hi* are in that unit, with
    ``None`` for an open end.  Lists (not tuples, not infinities) so the
    value survives the remote wire unchanged.
    """
    text = (text or "").strip()
    if not text:
        return None
    out = []
    num = re.compile(r"\s*(" + _NUM + r")\s*([A-Za-zλ]*)\s*$")
    for tok in [t.strip() for t in text.split(",") if t.strip()]:
        try:
            if tok[0] in "<>":
                m = num.match(tok[1:])
                if not m:
                    raise ValueError
                f, unit = _uv_unit(m.group(3))
                v = float(m.group(1)) * f
                out.append([v, None, unit] if tok[0] == ">" else [None, v, unit])
            elif "~" in tok:
                a, b = tok.split("~", 1)
                ma, mb = num.match(a), num.match(b)
                if not (ma and mb):
                    raise ValueError
                fb, ub = _uv_unit(mb.group(3))
                fa, ua = _uv_unit(ma.group(3)) if ma.group(3) else (fb, ub)
                if ua != ub:
                    raise SelectionTextError(
                        f"UV range: {tok!r} mixes metres and wavelengths")
                lo, hi = float(ma.group(1)) * fa, float(mb.group(1)) * fb
                if hi < lo:
                    raise SelectionTextError(f"UV range: {tok!r} ends below its start")
                out.append([lo, hi, ub])
            else:
                raise ValueError
        except SelectionTextError:
            raise
        except (ValueError, IndexError):
            raise SelectionTextError(
                f"UV range: {tok!r} is not a range; use a~b, >a or <b with "
                f"a unit, e.g. 0~50klambda, 100~300m, >200m") from None
    return out


def uv_range_text(uv: Optional[list]) -> str:
    """Short description of a parsed UV range, for titles and reports."""
    if not uv:
        return ""
    parts = []
    for lo, hi, unit in uv:
        u = "λ" if unit == "lambda" else "m"
        if lo is None:
            parts.append(f"<{hi:g}{u}")
        elif hi is None:
            parts.append(f">{lo:g}{u}")
        else:
            parts.append(f"{lo:g}~{hi:g}{u}")
    return ",".join(parts)
