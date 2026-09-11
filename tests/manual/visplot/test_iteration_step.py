"""
test_iteration_step.py
=======================
Regression tests for ``iteration_step`` — the Prev/Next step-index
arithmetic shared by duo mode's client-side "Animate: Field | SPW"
buttons (JavaScript, in the browser) and any future server-side stepper
(Python — see the module docstring for the expected first caller,
grid-mode pagination).

The point of this module is ``test_js_matches_python``: it extracts
``iteration_step.STEP_INDEX_JS`` — the *same string* duo mode's Prev/Next
``CustomJS`` embeds — wraps it in a bare harness, executes it under
``node``, and diffs the result against the Python implementation across
``GOLDEN_CASES``. Nothing else in the codebase can catch the two drifting
apart.

Requires ``node`` on PATH; skips cleanly without it, so the suite still
runs in a bare pipeline environment. If it is skipping in CI, that is a
coverage hole worth closing rather than an acceptable outcome (see
``test_tick_format.py``, which established this convention).

Test location
-------------
``cubevis/tests/manual/visplot/test_iteration_step.py``
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from cubevis.toolbox.visplot.iteration_step import (
    GOLDEN_CASES,
    STEP_INDEX_JS,
    _JS_CORE,
    check_golden,
    step_index,
)


# ---------------------------------------------------------------------------
# Python side
# ---------------------------------------------------------------------------

class TestGoldenTable:

    def test_python_matches_golden_table(self):
        """Every golden case holds against the Python implementation."""
        bad = check_golden()
        assert not bad, "\n".join(bad)


class TestStepIndex:
    """Cases spelled out individually, for a clearer failure than a
    table diff when one breaks."""

    def test_sentinel_next_lands_on_first_item(self):
        """Duo mode's Field axis starts on the "All fields" sentinel,
        which is not itself index 0 of anything -- Next from there must
        still land on the first real field."""
        assert step_index(None, 7, 1) == 0

    def test_sentinel_prev_also_lands_on_first_item(self):
        """Prev from the sentinel is defined to match Next, not to wrap
        "backwards" from a position that was never really -1 -- see the
        function's docstring for why this is the chosen rule."""
        assert step_index(None, 7, -1) == 0

    def test_no_items_returns_none(self):
        """count == 0 means there is nothing to land on at all --
        distinct from "nothing selected yet" (current_index=None with
        count > 0), which returns 0."""
        assert step_index(None, 0, 1) is None
        assert step_index(0, 0, 1) is None

    def test_wrap_forward_past_the_end(self):
        assert step_index(6, 7, 1, wrap=True) == 0

    def test_wrap_backward_past_the_start(self):
        assert step_index(0, 7, -1, wrap=True) == 6

    def test_single_item_wraps_to_itself(self):
        assert step_index(0, 1, 1, wrap=True) == 0
        assert step_index(0, 1, -1, wrap=True) == 0

    def test_clamp_forward_stops_at_the_end(self):
        """Not reachable from the shipped UI (I-1 always wraps), but
        implemented and tested for a future mode that wants it -- see
        the module docstring's grid-mode note."""
        assert step_index(6, 7, 1, wrap=False) == 6

    def test_clamp_backward_stops_at_the_start(self):
        assert step_index(0, 7, -1, wrap=False) == 0

    def test_large_stride_for_future_grid_mode_paging(self):
        """A grid-mode page turn steps by the page size, not by 1 --
        this must resolve the same way a smaller delta would, just
        further, with no special-casing for |delta| > count."""
        assert step_index(2, 4, 5, wrap=True) == 3
        assert step_index(0, 5, -7, wrap=True) == 3

    def test_out_of_range_current_index_is_normalised(self):
        """Defensive: a caller-supplied index outside [0, count) is
        folded back in before stepping, rather than propagating an
        out-of-range result."""
        assert step_index(9, 7, 1, wrap=True) == 3   # 9 -> 2, +1 -> 3


# ---------------------------------------------------------------------------
# Cross-runtime parity
# ---------------------------------------------------------------------------

_HARNESS = """
const cases = %s;
function step(current_index, count, delta, wrap) {
%s
}
const out = cases.map(c => step(c[0], c[1], c[2], c[3]));
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node not on PATH")
class TestJavaScriptParity:

    @staticmethod
    def _run_js(cases):
        """Execute _JS_CORE under node against *cases*, return the indices.

        Wraps the exact string STEP_INDEX_JS ships (and duo mode's
        Prev/Next CustomJS embeds), so this exercises the shipped
        implementation rather than a transcription of it.
        """
        script = _HARNESS % (
            json.dumps([list(c[:4]) for c in cases]), _JS_CORE,
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "harness.js"
            path.write_text(script)
            res = subprocess.run(["node", str(path)],
                                 capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stderr
        return json.loads(res.stdout)

    def test_js_matches_golden_table(self):
        """The browser produces the golden indices."""
        got = self._run_js(GOLDEN_CASES)
        bad = [
            f"current_index={c[0]!r} count={c[1]!r} delta={c[2]!r} "
            f"wrap={c[3]!r}: js={g!r} expected={c[4]!r}"
            for c, g in zip(GOLDEN_CASES, got) if g != c[4]
        ]
        assert not bad, "\n".join(bad)

    def test_js_matches_python(self):
        """The two runtimes agree index-for-index.

        The test this module exists for. A change to either
        implementation that is not mirrored in the other fails here.
        """
        got = self._run_js(GOLDEN_CASES)
        bad = [
            f"current_index={c[0]!r} count={c[1]!r} delta={c[2]!r} "
            f"wrap={c[3]!r}: js={g!r} "
            f"python={step_index(c[0], c[1], c[2], c[3])!r}"
            for c, g in zip(GOLDEN_CASES, got)
            if g != step_index(c[0], c[1], c[2], c[3])
        ]
        assert not bad, "\n".join(bad)

    def test_js_matches_python_on_fuzzed_values(self):
        """Beyond the golden table: pseudo-random steps in both modes.

        Deterministic seed so a failure is reproducible. Catches
        divergence in combinations nobody thought to enumerate --
        in particular strides larger than count, which is where a
        naive JS ``%`` (remainder, not floor-mod) most easily diverges
        from Python's ``%`` (always floor-mod for a positive divisor).
        """
        import random
        rng = random.Random(20260819)
        cases = []
        for _ in range(300):
            count = rng.randint(1, 12)
            current = rng.choice([None] + list(range(count)))
            delta = rng.randint(-3 * count, 3 * count) or 1
            wrap = rng.choice([True, False])
            cases.append((current, count, delta, wrap, None))
        # count == 0 edge case, deliberately separate from the randint(1, 12)
        # loop above (randint's bounds cannot produce it).
        for current in (None, 0, 5):
            cases.append((current, 0, 1, True, None))

        got = self._run_js(cases)
        bad = [
            f"current_index={c[0]!r} count={c[1]!r} delta={c[2]!r} "
            f"wrap={c[3]!r}: js={g!r} "
            f"python={step_index(c[0], c[1], c[2], c[3])!r}"
            for c, g in zip(cases, got)
            if g != step_index(c[0], c[1], c[2], c[3])
        ]
        assert not bad, f"{len(bad)} of {len(cases)} diverged:\n" + \
                        "\n".join(bad[:20])


class TestModuleWiring:

    def test_step_index_js_embeds_the_shared_core(self):
        """STEP_INDEX_JS must wrap _JS_CORE, not duplicate it.

        If someone inlines a copy back into visibility_plotter.py's
        ``_build_toolbar``, the parity tests above go on passing while
        the shipped Prev/Next handler drifts. This is the assertion
        that catches that.
        """
        assert _JS_CORE in STEP_INDEX_JS
        assert "function stepIterationIndex(" in STEP_INDEX_JS

    def test_visibility_plotter_uses_the_shared_string(self):
        """visibility_plotter must import STEP_INDEX_JS, not define its
        own copy of the step arithmetic."""
        from cubevis.toolbox.visplot import visibility_plotter
        src = Path(visibility_plotter.__file__).read_text()
        assert "STEP_INDEX_JS" in src
        assert "from .iteration_step import" in src
