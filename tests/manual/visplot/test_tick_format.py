"""
test_tick_format.py
===================
Regression tests for ``tick_format`` — the axis tick labels shared by the
Bokeh chrome (JavaScript, in the browser) and the matplotlib export
chrome (Python, in a pipeline).

The point of this module is ``test_js_matches_python``: it extracts
``tick_format._JS_CORE`` — the *same string* the ``CustomJSTickFormatter``
runs — wraps it in a bare function, executes it under ``node``, and diffs
the result against the Python implementation across the golden table.
Nothing else in the codebase can catch the two drifting apart.

Requires ``node`` on PATH; skips cleanly without it, so the suite still
runs in a bare pipeline environment.  If it is skipping in CI, that is a
coverage hole worth closing rather than an acceptable outcome.

Test location
-------------
``cubevis/tests/manual/visplot/test_tick_format.py``
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from cubevis.toolbox.visplot.tick_format import (
    GOLDEN_CASES,
    TICK_FORMATTER_JS,
    _JS_CORE,
    _to_fixed,
    check_golden,
    format_tick,
)


# ---------------------------------------------------------------------------
# Python side
# ---------------------------------------------------------------------------

class TestGoldenTable:

    def test_python_matches_golden_table(self):
        """Every golden case holds against the Python implementation."""
        bad = check_golden()
        assert not bad, "\n".join(bad)

    def test_minute_boundary_carries(self):
        """119.6 s is 2m 00s, not 1m 60s.

        The pre-2026-08 formatter rounded the seconds remainder after
        splitting, so a remainder that rounded up to a full minute had
        nowhere to carry.  Cosmetic, but it appeared on any time axis
        whose ticks landed near a minute boundary.
        """
        assert format_tick(119.6, True, 0.0) == "2m 00s"
        assert format_tick(-119.6, True, 0.0) == "-2m 00s"
        assert format_tick(3599.4, True, 0.0) == "59m 59s"

    def test_sub_minute_stays_in_seconds(self):
        """The <60 test uses the unrounded value, so 59.95 s stays seconds."""
        assert format_tick(59.95, True, 0.0) == "60.0 s"
        assert format_tick(60.0, True, 0.0) == "1m 00s"

    def test_negative_zero_keeps_sign(self):
        """(-0.04).toFixed(1) === "-0.0" — the sign survives rounding."""
        assert format_tick(99.96, True, 100.0) == "-0.0 s"

    def test_t0_is_full_extent_not_viewport(self):
        """Elapsed labels are relative to the supplied origin, unchanged
        by where the viewport happens to be.  This is why the browser
        reads t0 from _state_source's full_x0/full_y0 rather than from
        the axis range: pan and zoom must not relabel the ticks."""
        assert format_tick(1000.0, True, 100.0) == "15m 00s"
        assert format_tick(1000.0, True, 0.0) == "16m 40s"


class TestToFixed:
    """``_to_fixed`` must match JS ``toFixed``, not Python's formatter."""

    @pytest.mark.parametrize("value,digits,expected", [
        (2.5,    0, "3"),        # Python's f"{2.5:.0f}" gives "2"
        (0.25,   1, "0.3"),      # Python gives "0.2"
        (-0.25,  1, "-0.3"),     # Python gives "-0.2"
        (0.125,  2, "0.13"),     # Python gives "0.12"
        (1.005,  2, "1.00"),     # not really a tie: 1.00499999...
        (-0.04,  1, "-0.0"),
        (59.95,  1, "60.0"),
    ])
    def test_ties_round_away_from_zero(self, value, digits, expected):
        assert _to_fixed(value, digits) == expected

    def test_differs_from_python_default_where_expected(self):
        """Guard against someone 'simplifying' _to_fixed to an f-string.

        If these ever agree, the reimplementation has been replaced by
        the naive version and browser/PNG labels will silently diverge on
        tie values.
        """
        assert _to_fixed(0.25, 1) != f"{0.25:.1f}"
        assert _to_fixed(2.5, 0) != f"{2.5:.0f}"


# ---------------------------------------------------------------------------
# Cross-runtime parity
# ---------------------------------------------------------------------------

_HARNESS = """
const cases = %s;
function fmt(tick, is_time, t0) {
%s
}
const out = cases.map(c => fmt(c[0], c[2], c[1]));
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node not on PATH")
class TestJavaScriptParity:

    @staticmethod
    def _run_js(cases):
        """Execute _JS_CORE under node against *cases*, return the labels.

        Wraps the exact string the CustomJSTickFormatter uses, so this
        exercises the shipped implementation rather than a transcription
        of it.
        """
        script = _HARNESS % (json.dumps([list(c[:3]) for c in cases]), _JS_CORE)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "harness.js"
            path.write_text(script)
            res = subprocess.run(["node", str(path)],
                                 capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stderr
        return json.loads(res.stdout)

    def test_js_matches_golden_table(self):
        """The browser produces the golden labels."""
        got = self._run_js(GOLDEN_CASES)
        bad = [
            f"tick={c[0]!r} t0={c[1]!r} is_time={c[2]!r}: "
            f"js={g!r} expected={c[3]!r}"
            for c, g in zip(GOLDEN_CASES, got) if g != c[3]
        ]
        assert not bad, "\n".join(bad)

    def test_js_matches_python(self):
        """The two runtimes agree label-for-label.

        The test this module exists for.  A change to either
        implementation that is not mirrored in the other fails here.
        """
        got = self._run_js(GOLDEN_CASES)
        bad = [
            f"tick={c[0]!r} t0={c[1]!r} is_time={c[2]!r}: "
            f"js={g!r} python={format_tick(c[0], c[2], c[1])!r}"
            for c, g in zip(GOLDEN_CASES, got)
            if g != format_tick(c[0], c[2], c[1])
        ]
        assert not bad, "\n".join(bad)

    def test_js_matches_python_on_fuzzed_values(self):
        """Beyond the golden table: pseudo-random ticks in both modes.

        Deterministic seed so a failure is reproducible.  Catches
        divergence in ranges nobody thought to enumerate — in particular
        tie values, which is where JS and Python formatting differ.
        """
        import random
        rng = random.Random(20260813)
        cases = []
        for _ in range(400):
            t0 = rng.choice([0.0, 100.0, 4.8e9])
            cases.append((t0 + rng.uniform(-5000, 5000), t0, True, None))
            cases.append((rng.uniform(-1e6, 1e6), 0.0, False, None))
        # Tie values specifically, at both formatter precisions.
        for n in range(-40, 41):
            cases.append((n + 0.05, 0.0, True, None))
            cases.append((n / 8.0, 0.0, False, None))

        got = self._run_js(cases)
        bad = [
            f"tick={c[0]!r} t0={c[1]!r} is_time={c[2]!r}: "
            f"js={g!r} python={format_tick(c[0], c[2], c[1])!r}"
            for c, g in zip(cases, got)
            if g != format_tick(c[0], c[2], c[1])
        ]
        assert not bad, f"{len(bad)} of {len(cases)} diverged:\n" + \
                        "\n".join(bad[:20])


class TestFormatterWiring:

    def test_bokeh_formatter_embeds_the_shared_core(self):
        """TICK_FORMATTER_JS must wrap _JS_CORE, not duplicate it.

        If someone inlines a copy back into visibility_plot._build(), the
        parity tests above go on passing while the shipped formatter
        drifts.  This is the assertion that catches that.
        """
        assert _JS_CORE in TICK_FORMATTER_JS
        assert "state.data[axis_key][0]" in TICK_FORMATTER_JS
        assert "state.data[t0_key][0]" in TICK_FORMATTER_JS

    def test_visibility_plot_uses_the_shared_string(self):
        """visibility_plot must import the formatter, not define its own."""
        from cubevis.toolbox.visplot import visibility_plot
        src = Path(visibility_plot.__file__).read_text()
        assert "_time_fmt_code = \"\"\"" not in src, (
            "visibility_plot defines its own tick formatter again; it "
            "should use tick_format.TICK_FORMATTER_JS"
        )


class TestMatplotlibFormatter:

    def test_mpl_formatter_produces_golden_labels(self):
        """The matplotlib FuncFormatter routes through format_tick."""
        pytest.importorskip("matplotlib")
        from cubevis.toolbox.visplot.tick_format import mpl_formatter
        f = mpl_formatter(True, 100.0)
        assert f(142.5, 0) == "42.5 s"
        assert f(1000.0, 0) == "15m 00s"
        g = mpl_formatter(False)
        assert g(1234.5678, 0) == "1234.5678"
