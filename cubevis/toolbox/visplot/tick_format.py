"""
tick_format.py
==============
Axis tick formatting, shared by the Bokeh chrome and the matplotlib
export chrome.

Why this module exists
----------------------
With no Bokeh server, axis tick labels are produced in the browser by a
``CustomJSTickFormatter``.  The matplotlib export path must produce the
same strings from Python, and there is no way to derive one
implementation from the other — JS runs in the browser, Python runs in
the pipeline.  What *can* be done is to keep them adjacent, share one
copy of the algorithm's source text, and pin their agreement with a
golden table that both runtimes are tested against.  That converts silent
drift into a failing test, which is the best available outcome.

``_JS_CORE`` below is the single copy of the algorithm as JavaScript.
``TICK_FORMATTER_JS`` wraps it with the ``_state_source`` lookups Bokeh
needs; ``tests/.../test_tick_format.py`` wraps the *same* string in a
bare function and runs it under ``node`` against ``GOLDEN_CASES``.  There
is therefore one JS implementation, not two.

Numeric formatting is not portable
----------------------------------
JavaScript's ``Number.prototype.toFixed`` and Python's ``format(x, '.Nf')``
disagree on ties.  ``toFixed`` operates on ``|x|`` and picks the larger
candidate — ties away from zero — while Python's formatter rounds
half-to-even::

    (2.5).toFixed(0)   === "3"      f"{2.5:.0f}"   == "2"
    (0.25).toFixed(1)  === "0.3"    f"{0.25:.1f}"  == "0.2"
    (0.125).toFixed(2) === "0.13"   f"{0.125:.2f}" == "0.12"

The browser is what users see, so Python matches JS rather than the
reverse.  ``_to_fixed`` below reimplements ``toFixed`` semantics exactly,
using ``Decimal`` on the float's true binary value so the tie detection
matches the spec's "closest to zero, ties to larger" rule rather than
operating on a decimal approximation.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/tick_format.py``
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

# ---------------------------------------------------------------------------
# The algorithm, as JavaScript — the single copy
# ---------------------------------------------------------------------------
#
# Operates on three variables the caller must already have in scope:
# ``tick`` (float), ``is_time`` (truthy), ``t0`` (float).  Returns a
# string.  Kept free of Bokeh references so the test harness can wrap it
# in a plain function and run it under node; TICK_FORMATTER_JS below adds
# the Bokeh-specific preamble.
_JS_CORE = r"""
if (typeof labels !== 'undefined' && labels) {
    // An axis drawn in whole positions (a raster's Baseline axis): the
    // tick at position i reads labels[i]; a tick between positions, or
    // beyond the last, reads nothing.
    const i = Math.round(tick);
    if (Math.abs(tick - i) > 1e-6 || i < 0 || i >= labels.length) return '';
    return String(labels[i]);
}
if (!is_time) {
    // Scale is fixed from the full extent and supplied by _state_source,
    // so a pan or zoom never changes the axis units mid-interaction.
    if (typeof scale === 'number' && scale > 0 && scale !== 1) {
        tick = tick / scale;
    }
    // Trailing zeros are noise on a channel or distance axis: "10.0000"
    // reads worse than "10" and eats horizontal room that a crowded grid
    // export does not have.  Trim them, but never all the way to "0" for
    // a value that is not zero -- fall back to more decimals instead, so
    // a sub-milli axis still shows distinct ticks rather than a column
    // of zeros.
    let s = tick.toFixed(4);
    if (s.indexOf('.') >= 0)
        s = s.replace(/0+$/, '').replace(/\.$/, '');
    if (tick !== 0 && (s === '0' || s === '-0')) {
        s = tick.toFixed(9);
        if (s.indexOf('.') >= 0)
            s = s.replace(/0+$/, '').replace(/\.$/, '');
    }
    return s;
}
if (typeof mode !== 'undefined' && mode === 'utc') {
    // UTC time of day (2026-10-11, HRS A0).  Times are UNIX seconds
    // (xarray-ms / MSv4 "format: unix, scale: utc"), so a whole day is
    // a multiple of 86400 and no calendar is needed here.  Decimals
    // follow the tick spacing; the arithmetic is done in integer units
    // of the last decimal so Python reproduces it exactly.
    const st  = (typeof step !== 'undefined' && step) ? step : 1;
    const dec = st >= 1 ? 0 : (st >= 0.1 ? 1 : 2);
    const p   = Math.pow(10, dec);
    const u   = Math.floor(tick * p + 0.5);
    const per = 86400 * p;
    const day = Math.floor(u / per);
    let   r   = u - day * per;
    const fr  = r % p;  r = (r - fr) / p;
    const hh  = Math.floor(r / 3600), mm = Math.floor((r % 3600) / 60), ss = r % 60;
    const two = (n) => (n < 10 ? '0' : '') + n;
    let txt = two(hh) + ':' + two(mm) + ':' + two(ss);
    if (dec > 0) txt += '.' + String(fr).padStart(dec, '0');
    if (typeof day0 !== 'undefined' && day0 !== null) txt = (day - day0) + '/' + txt;
    return txt;
}
const elapsed = tick - t0;
if (Math.abs(elapsed) < 60)
    return elapsed.toFixed(1) + ' s';
// Round to whole seconds *before* splitting into minutes and seconds.
// Doing it after (Math.round(|elapsed| % 60)) yields "1m 60s" for an
// elapsed time of 119.6 s, because the remainder rounds up to a full
// minute with nowhere to carry.  Fixed 2026-08; GOLDEN_CASES pins it.
const sign  = elapsed < 0 ? '-' : '';
const total = Math.round(Math.abs(elapsed));
const m     = Math.floor(total / 60);
const s     = total % 60;
return sign + m + 'm ' + s.toString().padStart(2, '0') + 's';
"""

# The CustomJSTickFormatter body.  Requires args={"state": <source>,
# "axis_key": "x_is_time"|"y_is_time", "t0_key": "x_t0"|"y_t0",
# "scale_key": "x_scale"|"y_scale", "cat_key": "x_cat"|"y_cat"}.
# All four columns come from PanelSpec.to_state_data(); ``cat_key`` holds
# null for an ordinary numeric axis.
TICK_FORMATTER_JS = """
const is_time = state.data[axis_key][0];
const t0      = state.data[t0_key][0];
const scale   = (state.data[scale_key] || [1])[0];
const labels  = (typeof cat_key !== 'undefined' && state.data[cat_key])
                ? state.data[cat_key][0] : null;
// UTC or elapsed labels on a time axis (HRS A0); the day number of the
// first sample when the axis crosses a UTC midnight, else null; the
// tick spacing, which decides how many decimals a UTC label shows.
const mode    = (state.data['time_labels'] || ['elapsed'])[0];
const day0    = (typeof day_key !== 'undefined' && state.data[day_key])
                ? state.data[day_key][0] : null;
const step    = (typeof ticks !== 'undefined' && ticks && ticks.length > 1)
                ? Math.abs(ticks[1] - ticks[0]) : null;
""" + _JS_CORE


# ---------------------------------------------------------------------------
# The algorithm, as Python
# ---------------------------------------------------------------------------

def _to_fixed(x: float, digits: int) -> str:
    """Reproduce JavaScript ``Number.prototype.toFixed`` exactly.

    Per ECMA-262: the sign is stripped first, then an integer ``n`` is
    chosen so that ``n / 10**digits`` is closest to ``|x|``, ties going to
    the *larger* ``n``.  That is round-half-away-from-zero on the
    magnitude, which Python's own formatter does not do.

    ``Decimal(abs(x))`` is exact — it converts the float's true binary
    value, not its shortest decimal representation — so a value that only
    looks like a tie (1.005, which is really 1.00499999...) is correctly
    not treated as one, matching the browser.
    """
    if not math.isfinite(x):
        return "NaN" if math.isnan(x) else ("Infinity" if x > 0 else "-Infinity")
    neg = math.copysign(1.0, x) < 0
    q   = Decimal(1).scaleb(-digits)
    d   = Decimal(abs(x)).quantize(q, rounding=ROUND_HALF_UP)
    s   = f"{d:.{digits}f}"
    # JS keeps the sign even when the rounded magnitude is zero:
    # (-0.04).toFixed(1) === "-0.0".
    return f"-{s}" if neg else s


def _js_round(x: float) -> int:
    """``Math.round`` for non-negative *x*: ties toward +infinity.

    Only ever called on a magnitude, so the spec's negative-zero and
    negative-tie cases (``Math.round(-0.5) === -0``) cannot arise.
    """
    return math.floor(x + 0.5)


def _js_round_signed(x: float) -> int:
    """``Math.round`` for any finite *x*: ties toward +infinity."""
    return int(math.floor(x + 0.5))


def _trim_zeros(s: str) -> str:
    """Strip trailing fractional zeros, then a bare trailing point.

    Mirrors the regex pair in ``_JS_CORE``.  Only touches strings that
    contain a decimal point, so an integer-valued label is left alone.
    """
    if "." not in s:
        return s
    return s.rstrip("0").rstrip(".")


def _utc_decimals(step) -> int:
    """Decimals of a UTC tick label for a tick spacing of *step* s."""
    st = step if step else 1
    return 0 if st >= 1 else (1 if st >= 0.1 else 2)


def format_utc(t: float, decimals: int = 0, day0=None) -> str:
    """UTC time of day of UNIX seconds *t*: ``"07:37:03"``.

    *decimals* digits of seconds; with *day0* (a day number,
    ``floor(t / 86400)`` of the axis start) the label is prefixed with
    the day count from it, AIPS style: ``"1/00:10:00"``.  Mirrors the
    UTC branch of ``_JS_CORE`` in integer arithmetic, so both give the
    same text for the same float.
    """
    p   = 10 ** decimals
    u   = math.floor(t * p + 0.5)
    per = 86400 * p
    day = u // per
    r   = u - day * per
    fr  = r % p
    r   = (r - fr) // p
    hh, mm, ss = r // 3600, (r % 3600) // 60, r % 60
    txt = f"{hh:02d}:{mm:02d}:{ss:02d}"
    if decimals > 0:
        txt += "." + str(fr).rjust(decimals, "0")
    if day0 is not None:
        txt = f"{day - int(day0)}/{txt}"
    return txt


def utc_date(t: float) -> str:
    """UTC calendar date of UNIX seconds *t*: ``"2012-11-19"``."""
    import datetime as _dt
    return (_dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
            + _dt.timedelta(seconds=float(t))).strftime("%Y-%m-%d")


def utc_datetime_text(t: float, decimals: int = 1) -> str:
    """``"2012-11-19 07:37:03.0"`` -- the cursor readout's UTC form."""
    return f"{utc_date(math.floor(t))} {format_utc(t, decimals)}"


def time_day0(lo: float, hi: float):
    """Day number for the AIPS-style day prefix, or ``None``.

    ``None`` (no prefix) when *lo* and *hi* fall on the same UTC day,
    which is the usual case; otherwise ``floor(lo / 86400)``.
    """
    if not (math.isfinite(lo) and math.isfinite(hi)):
        return None
    d0, d1 = math.floor(lo / 86400.0), math.floor(hi / 86400.0)
    return int(d0) if d1 != d0 else None


# Tick spacings for a UTC axis: AdaptiveTicker with base 60 gives
# 1, 2, 5, 10, 15, 20, 30 s, then the same in minutes and hours, so
# ticks fall on whole minutes and hours.  Used by the browser (the
# axis ticker is switched to these when an axis shows UTC) and by the
# PNG export's locator, which picks the same spacing.
UTC_TICK_BASE      = 60
UTC_TICK_MANTISSAS = (1, 2, 5, 10, 15, 20, 30)


def utc_tick_step(lo: float, hi: float, desired: int = 6) -> float:
    """The spacing Bokeh's ``AdaptiveTicker`` picks for a UTC axis.

    Same rule as BokehJS 3.x ``AdaptiveTicker.get_interval`` with
    ``min_interval`` 0: ideal = span / desired; magnitude = base ** floor
    (log_base ideal); of the extended mantissas (last / base, the
    mantissas, first * base) the one giving the tick count nearest
    *desired*.
    """
    span = abs(hi - lo)
    if not math.isfinite(span) or span <= 0:
        return 1.0
    b = UTC_TICK_BASE
    ideal = span / max(desired, 1)
    mag = b ** math.floor(math.log(ideal) / math.log(b))
    ext = ([UTC_TICK_MANTISSAS[-1] / b] + list(UTC_TICK_MANTISSAS)
           + [UTC_TICK_MANTISSAS[0] * b])
    best = min(ext, key=lambda m: abs(desired - span / (m * mag)))
    return float(best * mag)


def format_tick(tick: float, is_time: bool, t0: float = 0.0,
                scale: float = 1.0, labels=None, *,
                time_mode: str = "elapsed", step=None, day0=None) -> str:
    """Format one axis tick, matching the browser byte for byte.

    Parameters
    ----------
    labels : sequence of str, optional
        For an axis drawn in whole positions (a raster's Baseline axis):
        the text of the tick at each position.  A tick between
        positions, or outside them, is empty.  Takes precedence over
        everything else.
    tick : float
        The tick value in data units.
    is_time : bool
        Whether this axis is ``Axis.TIME``.  When ``False`` the value is
        shown directly to 4 decimals; when ``True`` it is shown as
        elapsed time from *t0*.
    t0 : float
        Origin for elapsed time — the axis's ``full_x0``/``full_y0``,
        i.e. the start of the *full* data extent, not of the current
        viewport.  Elapsed labels therefore stay stable under pan and
        zoom, which is why the browser reads it from ``_state_source``
        rather than from the axis range.

    Returns
    -------
    str
        e.g. ``"1234.5678"``, ``"42.5 s"``, ``"2m 05s"``, ``"-1m 30s"``.
    """
    if labels:
        i = _js_round_signed(tick)
        if abs(tick - i) > 1e-6 or i < 0 or i >= len(labels):
            return ""
        return str(labels[i])
    if not is_time:
        if scale and scale > 0 and scale != 1.0:
            tick = tick / scale
        s = _trim_zeros(_to_fixed(tick, 4))
        if tick != 0 and s in ("0", "-0"):
            s = _trim_zeros(_to_fixed(tick, 9))
        return s
    if time_mode == "utc":
        return format_utc(tick, _utc_decimals(step), day0)
    elapsed = tick - t0
    if abs(elapsed) < 60:
        return _to_fixed(elapsed, 1) + " s"
    sign  = "-" if elapsed < 0 else ""
    total = _js_round(abs(elapsed))
    m, s  = divmod(total, 60)
    return f"{sign}{m}m {s:02d}s"


_SI_PREFIXES = (
    (1e24, "Y"), (1e21, "Z"), (1e18, "E"), (1e15, "P"), (1e12, "T"),
    (1e9,  "G"), (1e6,  "M"), (1e3,  "k"), (1.0,  ""),  (1e-3, "m"),
    (1e-6, "\u00b5"), (1e-9, "n"), (1e-12, "p"), (1e-15, "f"),
)


# Prefixes usable on an axis.  Deliberately coarser than si_scale's set:
# an axis label reading "TB" or "fm" is more confusing than a slightly
# large number, and the astronomy-relevant span is k..G with m/u below.
_AXIS_PREFIXES = (
    (1e9,  "G"), (1e6,  "M"), (1e3,  "k"), (1.0, ""),
    (1e-3, "m"), (1e-6, "\u00b5"),
)


def si_prefix(lo: float, hi: float, unit: str) -> tuple[float, str]:
    """Divisor and prefixed unit for an axis spanning ``[lo, hi]``.

    Returns ``(1.0, unit)`` unchanged when *unit* is empty (an index or
    dimensionless axis takes no prefix) or compound (``"m/s"``, where
    naive prefixing produces nonsense).  **Never infer a unit from
    magnitude alone** -- a bare float carries no dimension, and a
    magnitude-triggered "GHz" would mislabel a long baseline in metres as
    gigametres.

    Chosen from the *full data extent*, not the current viewport, and
    deliberately so: a unit that flips from GHz to MHz partway through a
    zoom is more disorienting than a large number, and holding it fixed
    means the axis label is computed once rather than on every pan.

    Uses the larger magnitude of the two endpoints, so an axis running
    from 0 to 372e9 is labelled GHz rather than being dragged to no
    prefix by its zero end.
    """
    if not unit or "/" in unit or unit in ("deg", "rad", "h", "K", "\u03bb"):
        return 1.0, unit
    mag = max(abs(lo), abs(hi))
    if not math.isfinite(mag) or mag == 0.0:
        return 1.0, unit
    for factor, prefix in _AXIS_PREFIXES:
        if mag >= factor:
            return factor, f"{prefix}{unit}"
    return 1.0, unit


def si_scale(value: float, unit: str) -> tuple[float, str]:
    """Scale *value* to an SI-prefixed *unit*: ``(372.764, "GHz")``.

    Returns *value* and *unit* unchanged when *unit* is empty (a
    dimensionless or index axis takes no prefix) or when the magnitude is
    already in range.  **Never infer a unit from magnitude alone** — a
    bare float carries no dimension, and a magnitude-triggered "GHz"
    would confidently mislabel a long baseline in metres as gigametres.

    Python-only, and deliberately so.  This is used by ``_format_probe``,
    whose output is rendered to HTML server-side, so unlike
    ``format_tick`` it has no JavaScript counterpart to stay in step with
    and needs no parity harness.  Axis *tick* text is the thing that must
    match the browser; a status-bar readout is not.

    Prefixes are not applied to units that already carry one or that are
    compound (``"m/s"``), where naive prefixing would produce nonsense
    like ``"km/s"`` from a value in m/s (correct) but ``"kdeg"`` from
    degrees (not).
    """
    if not unit or "/" in unit or unit in ("deg", "rad", "h", "K", "\u03bb"):
        return value, unit
    if value == 0 or not math.isfinite(value):
        return value, unit
    mag = abs(value)
    for factor, prefix in _SI_PREFIXES:
        if mag >= factor:
            return value / factor, f"{prefix}{unit}"
    return value, unit


def mpl_formatter(is_time: bool, t0: float = 0.0, scale: float = 1.0,
                  labels=None, *, time_mode: str = "elapsed", day0=None):
    """Return a matplotlib formatter wrapping *format_tick*.

    matplotlib is imported lazily: this module is imported by
    ``visibility_plot``, which must stay importable in a GUI-only install
    that has no matplotlib.

    The tick spacing (for a UTC label's decimals) is read from the
    formatter's own ``locs``, as the browser reads it from ``ticks``.
    """
    from matplotlib.ticker import Formatter

    class _F(Formatter):
        _cv_locs: list = []

        def set_locs(self, locs):
            self._cv_locs = list(locs)

        def __call__(self, v, pos=None):
            locs = self._cv_locs
            step = abs(locs[1] - locs[0]) if len(locs) > 1 else None
            return format_tick(v, is_time, t0, scale, labels,
                               time_mode=time_mode, step=step, day0=day0)
    return _F()


# ---------------------------------------------------------------------------
# Golden table
# ---------------------------------------------------------------------------
#
# ``(tick, t0, is_time, expected)``.  Asserted against the Python
# implementation and, under node, against _JS_CORE.  A case here is a
# claim about what the user sees in *both* the browser and an exported
# PNG; add to it whenever either implementation changes.
GOLDEN_CASES: tuple[tuple[float, float, bool, str], ...] = (
    # --- non-time axis: trimmed to significant digits -------------------
    (0.0,          0.0, False, "0"),
    (10.0,         0.0, False, "10"),
    (24.0,         0.0, False, "24"),
    (1234.5678,    0.0, False, "1234.5678"),
    (-42.125,      0.0, False, "-42.125"),
    (0.5,          0.0, False, "0.5"),
    (0.00005,      0.0, False, "0.0001"),     # tie -> away from zero
    (-0.00005,     0.0, False, "-0.0001"),
    (1e6,          0.0, False, "1000000"),
    # Would trim to "0" but is not zero: fall back to more decimals.
    (1.2e-6,       0.0, False, "0.0000012"),
    (-3.4e-7,      0.0, False, "-0.00000034"),

    # --- time axis, under a minute: one decimal, " s" -------------------
    (100.0,      100.0, True,  "0.0 s"),
    (142.5,      100.0, True,  "42.5 s"),
    (100.25,     100.0, True,  "0.3 s"),      # JS: 0.3, naive Python: 0.2
    (59.95,        0.0, True,  "60.0 s"),     # rounds to 60 but stays "s"
    (95.0,       100.0, True,  "-5.0 s"),
    (99.96,      100.0, True,  "-0.0 s"),     # sign survives rounding

    # --- time axis, a minute or more ------------------------------------
    (60.0,         0.0, True,  "1m 00s"),
    (125.0,        0.0, True,  "2m 05s"),
    (119.6,        0.0, True,  "2m 00s"),     # was "1m 60s" before 2026-08
    (3599.4,       0.0, True,  "59m 59s"),
    (3600.0,       0.0, True,  "60m 00s"),    # no hour rollover by design
    (-125.0,       0.0, True,  "-2m 05s"),
    (-119.6,       0.0, True,  "-2m 00s"),
    (1000.0,     100.0, True,  "15m 00s"),
)

# Scaled cases: ``(tick, t0, is_time, scale, expected)``.  Separate from
# GOLDEN_CASES so the unscaled table stays a four-tuple and the existing
# harnesses need no change.
GOLDEN_SCALED: tuple[tuple[float, float, bool, float, str], ...] = (
    # Frequency in Hz displayed as GHz -- the case this exists for.
    (3.72649e11,   0.0, False, 1e9,  "372.649"),
    (3.7255e11,    0.0, False, 1e9,  "372.55"),
    (3.72766e11,   0.0, False, 1e9,  "372.766"),
    # A scale of 1 must be indistinguishable from no scale at all.
    (1234.5678,    0.0, False, 1.0,  "1234.5678"),
    (0.0,          0.0, False, 1.0,  "0"),
    # Sub-unit prefixes.
    (0.0123,       0.0, False, 1e-3, "12.3"),
    (-0.0005,      0.0, False, 1e-3, "-0.5"),
    # Trailing-zero trimming still applies after scaling.
    (3.72e11,      0.0, False, 1e9,  "372"),
    # Time ignores scale entirely: elapsed formatting is already
    # human-scaled, and dividing MJD seconds by 1e9 would be nonsense.
    (125.0,        0.0, True,  1e9,  "2m 05s"),
    (142.5,      100.0, True,  1e3,  "42.5 s"),
)


def check_golden() -> list[str]:
    """Return a list of mismatch descriptions; empty means all pass.

    Exposed as a function rather than living only in the test module so
    it can be called from a REPL or a smoke check without pytest.
    """
    bad = []
    for tick, t0, is_time, want in GOLDEN_CASES:
        got = format_tick(tick, is_time, t0)
        if got != want:
            bad.append(
                f"format_tick({tick!r}, {is_time!r}, {t0!r}) -> "
                f"{got!r}, expected {want!r}"
            )
    return bad


def set_time_ticker(ticker, utc: bool) -> None:
    """Point a Bokeh ``AdaptiveTicker`` at UTC spacings (base 60) or back
    to Bokeh's default decimal ones.  The browser twin is in the Plot
    response (``cvIntegerTicks``) and the time-labels toggle."""
    if utc:
        ticker.base, ticker.mantissas = float(UTC_TICK_BASE), list(UTC_TICK_MANTISSAS)
    else:
        ticker.base, ticker.mantissas = 10.0, [1, 2, 5]
