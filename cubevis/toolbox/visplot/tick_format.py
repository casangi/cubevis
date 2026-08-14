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
if (!is_time) {
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
# "axis_key": "x_is_time"|"y_is_time", "t0_key": "full_x0"|"full_y0"}.
TICK_FORMATTER_JS = """
const is_time = state.data[axis_key][0];
const t0      = state.data[t0_key][0];
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


def _trim_zeros(s: str) -> str:
    """Strip trailing fractional zeros, then a bare trailing point.

    Mirrors the regex pair in ``_JS_CORE``.  Only touches strings that
    contain a decimal point, so an integer-valued label is left alone.
    """
    if "." not in s:
        return s
    return s.rstrip("0").rstrip(".")


def format_tick(tick: float, is_time: bool, t0: float = 0.0) -> str:
    """Format one axis tick, matching the browser byte for byte.

    Parameters
    ----------
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
    if not is_time:
        s = _trim_zeros(_to_fixed(tick, 4))
        if tick != 0 and s in ("0", "-0"):
            s = _trim_zeros(_to_fixed(tick, 9))
        return s
    elapsed = tick - t0
    if abs(elapsed) < 60:
        return _to_fixed(elapsed, 1) + " s"
    sign  = "-" if elapsed < 0 else ""
    total = _js_round(abs(elapsed))
    m, s  = divmod(total, 60)
    return f"{sign}{m}m {s:02d}s"


def mpl_formatter(is_time: bool, t0: float = 0.0):
    """Return a ``matplotlib.ticker.FuncFormatter`` wrapping *format_tick*.

    matplotlib is imported lazily: this module is imported by
    ``visibility_plot``, which must stay importable in a GUI-only install
    that has no matplotlib.
    """
    from matplotlib.ticker import FuncFormatter
    return FuncFormatter(lambda v, _pos: format_tick(v, is_time, t0))


# ---------------------------------------------------------------------------
# Golden table
# ---------------------------------------------------------------------------
#
# ``(tick, t0, is_time, expected)``.  Asserted against the Python
# implementation and, under node, against _JS_CORE.  A case here is a
# claim about what the user sees in *both* the browser and an exported
# PNG; add to it whenever either implementation changes.
GOLDEN_CASES: tuple[tuple[float, float, bool, str], ...] = (
    # --- non-time axis: four decimals -----------------------------------
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
