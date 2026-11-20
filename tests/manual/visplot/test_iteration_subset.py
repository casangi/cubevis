"""test_iteration_subset.py -- Prev / Next through the ticked rows.

Pre-preview feedback (2026-11-18): ticking SPWs 2, 3 and 4 and pressing
Next narrowed the ticks to one and then walked every SPW.  Now the ▶
(a per-axis current row, which Plot requests carry) always marks
iteration: with two or more ticked the steppers walk those rows and leave
the ticks alone; with one ticked the tick moves with the ▶; with none
ticked on Antenna / Baseline ("all") only the ▶ moves.  Plot ▶ and any
hand change of the ticks end it.  Grid Play follows the ▶ axis first.

The shipped JS strings run under ``node`` against plain-object stand-ins
for the Bokeh sources.  Checked live as well (headless Chrome, Bokeh
3.10, simulated MS): Antenna 2/6 -> 4/6 -> 6/6 -> 2/6 with 1, 3, 5
ticked, "3 selected" again after Plot ▶; Baseline likewise; none ticked keeps
the ticks empty while the ▶ moves; a single tick [2] steps to [3], [4].

Run:  python -m pytest test_iteration_subset.py -q
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from cubevis_test_paths import ensure_cubevis_importable

ensure_cubevis_importable()

from cubevis.toolbox.visplot import iteration_subset as its           # noqa: E402
from cubevis.toolbox.visplot import antenna_baseline_select as abs_   # noqa: E402
from cubevis.toolbox.visplot.iteration_step import STEP_INDEX_JS      # noqa: E402

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_HARNESS = r"""
const window = {};
function src(cols, ticked) {
    let emits = 0;
    return {data: cols, selected: {indices: ticked}, change: {emit() { emits++; }},
            get emits() { return emits; }};
}
"""


def _node(script: str):
    code = _HARNESS + STEP_INDEX_JS + its.ITER_SUBSET_JS + script
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


SPW_STEP = r"""
function doIterateSpw(delta) {
    const idents = spw_src.data['ident'];
    if (cvIterSubsetStep(spw_src, 'spw', idents.map((_, i) => i), delta)) return;
    spw_src.selected.indices = [stepIterationIndex(
        spw_src.selected.indices.length ? Math.min.apply(null, spw_src.selected.indices) : null,
        idents.length, delta, true)];
}
"""


class TestSubsetStep:
    def test_walks_only_the_ticked_rows_and_keeps_the_ticks(self):
        r = _node(SPW_STEP + r"""
const spw_src = src({ident: [0, 1, 2, 3, 4, 5]}, [2, 3, 4]);
const seen = [];
for (const d of [1, 1, 1, 1, -1]) { doIterateSpw(d); seen.push(cvIterEffective(spw_src, 'spw')[0]); }
console.log(JSON.stringify({seen, ticks: spw_src.selected.indices, marker: spw_src.data['iter_cur']}));
""")
        assert r["seen"] == [2, 3, 4, 2, 4]
        assert r["ticks"] == [2, 3, 4]
        assert r["marker"] == [0, 0, 0, 0, 1, 0]

    def test_first_press_starts_at_the_first_ticked_either_way(self):
        """iteration_step's rule for every stepper: nothing current yet,
        Prev or Next, starts at the first."""
        r = _node(SPW_STEP + r"""
const spw_src = src({ident: [0, 1, 2, 3]}, [3, 1]);
doIterateSpw(-1); const a = cvIterEffective(spw_src, 'spw');
doIterateSpw(-1); const b = cvIterEffective(spw_src, 'spw');
console.log(JSON.stringify({a, b}));""")
        assert r == {"a": [1], "b": [3]}

    def test_one_tick_moves_with_the_marker(self):
        """One tick: every row, the tick and the ▶ moving together."""
        r = _node(SPW_STEP + r"""
const spw_src = src({ident: [0, 1, 2, 3]}, [1]);
doIterateSpw(1); const a = [spw_src.selected.indices.slice(), window.__cvIterCur.spw];
doIterateSpw(1); const b = [spw_src.selected.indices.slice(), window.__cvIterCur.spw];
doIterateSpw(1); const c = [spw_src.selected.indices.slice(), window.__cvIterCur.spw];
console.log(JSON.stringify({a, b, c, eff: cvIterEffective(spw_src, 'spw'), own: window.__cvIterOwnTick.spw}));""")
        assert r["a"] == [[2], 2] and r["b"] == [[3], 3] and r["c"] == [[0], 0]
        assert r["eff"] == [0]
        assert r["own"] == "[0]"           # the stepper's own tick change, marked

    def test_spw_none_ticked_starts_at_the_first_and_ticks_it(self):
        r = _node(SPW_STEP + r"""
const spw_src = src({ident: [0, 1, 2]}, []);
doIterateSpw(1); console.log(JSON.stringify([spw_src.selected.indices, window.__cvIterCur.spw]));""")
        assert r == [[0], 0]

    def test_none_ticked_on_an_all_axis_moves_only_the_marker(self):
        """Antenna / Baseline: none ticked means all; it stays so."""
        r = _node(r"""
const s = src({name: ['a', 'b', 'c'], has_data: [1, 1, 1]}, []);
const seen = [];
for (let k = 0; k < 4; k++) { cvIterSubsetStep(s, 'antenna', [0, 1, 2], 1);
    seen.push([s.selected.indices.slice(), window.__cvIterCur.antenna]); }
console.log(JSON.stringify({seen, eff: cvIterEffective(s, 'antenna')}));""")
        assert r["seen"] == [[[], 0], [[], 1], [[], 2], [[], 0]]
        assert r["eff"] == [0]

    def test_one_tick_on_a_row_without_data_steps_to_a_neighbour(self):
        r = _node(r"""
const s = src({name: ['a', 'b', 'c', 'd'], has_data: [1, 0, 1, 1]}, [1]);
cvIterSubsetStep(s, 'antenna', [0, 2, 3], 1); const n = window.__cvIterCur.antenna;
s.selected.indices = [1]; window.__cvIterCur.antenna = null;
cvIterSubsetStep(s, 'antenna', [0, 2, 3], -1); const p = window.__cvIterCur.antenna;
console.log(JSON.stringify([n, p]));""")
        assert r == [2, 0]

    def test_own_tick_change_does_not_end_the_iteration(self):
        r = _node(r"""
const s = src({ident: [0, 1, 2]}, [0]);
cvIterSetTicks(s, 'spw', [1]); cvIterMark(s, 'spw', 1);
const end = (new Function('window', 'src', 'axis', 'stepIterationIndex', %s));
end(window, s, 'spw', stepIterationIndex);          // the listener, for that change
const kept = window.__cvIterCur.spw;
s.selected.indices = [2];                            // then a hand change
end(window, s, 'spw', stepIterationIndex);
console.log(JSON.stringify([kept, window.__cvIterCur.spw]));""" % json.dumps(its.END_ON_TICK_CHANGE_JS))
        assert r == [1, None]

    def test_rows_without_data_are_skipped(self):
        r = _node(r"""
const s = src({name: ['a', 'b', 'c', 'd'], has_data: [1, 0, 1, 1]}, [0, 1, 3]);
const cand = [0, 2, 3];
const seen = [];
for (let k = 0; k < 3; k++) { cvIterSubsetStep(s, 'antenna', cand, 1); seen.push(window.__cvIterCur.antenna); }
console.log(JSON.stringify(seen));""")
        assert r == [0, 3, 0]

    def test_effective_falls_back_to_ticks_when_stale(self):
        """A current row that disagrees with the ticks does not count --
        the ticks are what is plotted."""
        r = _node(r"""
const s = src({ident: [0, 1, 2]}, [0, 2]);
cvIterMark(s, 'spw', 2);
const a = cvIterEffective(s, 'spw');
s.selected.indices = [0, 1];
const b = cvIterEffective(s, 'spw');
s.selected.indices = [0];
const c = cvIterEffective(s, 'spw');
s.selected.indices = [];
const d = cvIterEffective(s, 'spw');
console.log(JSON.stringify({a, b, c, d}));""")
        assert r == {"a": [2], "b": [0, 1], "c": [0], "d": []}

    def test_clear_all_and_end_on_tick_change(self):
        r = _node(r"""
const s = src({ident: [0, 1, 2]}, [0, 2]), a = src({name: ['x', 'y']}, [0, 1]),
      b = src({ant1: ['x'], ant2: ['y']}, []);
cvIterMark(s, 'spw', 2); cvIterMark(a, 'antenna', 1);
cvIterClearAll(s, a, b);
const after = [window.__cvIterCur.spw, window.__cvIterCur.antenna, s.data.iter_cur, a.data.iter_cur];
cvIterMark(s, 'spw', 0);
(new Function('window', 'src', 'axis', 'stepIterationIndex', %s))(window, s, 'spw', stepIterationIndex);
console.log(JSON.stringify({after, end: window.__cvIterCur.spw}));""" % json.dumps(its.END_ON_TICK_CHANGE_JS))
        assert r == {"after": [None, None, [0, 0, 0], [0, 0]], "end": None}


class TestAntennaBaseline:
    def test_current_antenna_sets_ticked_baselines_aside(self):
        r = _node(r"""
const ant = src({name: ['A', 'B', 'C']}, [0, 2]), bl = src({ant1: ['A'], ant2: ['B']}, [0]);
cvIterMark(ant, 'antenna', 2);
const [ae, be] = cvIterAntBl(ant, bl);
console.log(JSON.stringify({a: ae.selected.indices, b: be.selected.indices, ticks: bl.selected.indices}));""")
        assert r == {"a": [2], "b": [], "ticks": [0]}

    def test_current_baseline_wins(self):
        r = _node(r"""
const ant = src({name: ['A', 'B', 'C']}, [0, 2]), bl = src({ant1: ['A', 'A'], ant2: ['B', 'C']}, [0, 1]);
cvIterMark(ant, 'antenna', 2); cvIterMark(bl, 'baseline', 1);
const [ae, be] = cvIterAntBl(ant, bl);
console.log(JSON.stringify({a: ae.selected.indices, b: be.selected.indices}));""")
        assert r == {"a": [], "b": [1]}

    def test_nothing_current_passes_the_sources_through(self):
        r = _node(r"""
const ant = src({name: ['A']}, [0]), bl = src({ant1: [], ant2: []}, []);
const [ae, be] = cvIterAntBl(ant, bl);
console.log(JSON.stringify(ae === ant && be === bl));""")
        assert r is True

    def test_shipped_baseline_stepper_none_ticked_follows_the_antennas(self):
        """No baseline ticked: the ▶ walks the baselines of the antenna
        selection, ticks staying empty."""
        guard = "if (cand.length < 2) return;"
        r = _node(abs_.iterate_baseline_js(guard) + r"""
const ant_src = src({name: ['A', 'B', 'C']}, [0, 1]);
const bl_src = src({ant1: ['A', 'A', 'B'], ant2: ['B', 'C', 'C'], has_data: [1, 1, 1]}, []);
const seen = [];
for (let k = 0; k < 4; k++) { doIterateBaseline(1); seen.push(window.__cvIterCur.baseline); }
console.log(JSON.stringify({seen, ticks: bl_src.selected.indices}));""")
        # antennas A, B ticked (some, not all): every baseline touches one
        assert r == {"seen": [0, 1, 2, 0], "ticks": []}

    def test_shipped_baseline_stepper_one_tick_moves_it(self):
        guard = "if (cand.length < 2) return;"
        r = _node(abs_.iterate_baseline_js(guard) + r"""
const ant_src = src({name: ['A', 'B', 'C']}, []);
const bl_src = src({ant1: ['A', 'A', 'B'], ant2: ['B', 'C', 'C'], has_data: [1, 1, 1]}, [1]);
doIterateBaseline(1);
console.log(JSON.stringify([bl_src.selected.indices, window.__cvIterCur.baseline]));""")
        assert r == [[2], 2]

    def test_shipped_antenna_stepper_uses_the_subset(self):
        guard = "if (names.length < 2) return;"
        r = _node(abs_.iterate_antenna_js(guard) + r"""
const ant_src = src({name: ['A', 'B', 'C', 'D'], has_data: [1, 1, 1, 1]}, [1, 3]);
const bl_src = src({ant1: ['A'], ant2: ['B']}, [0]);
const seen = [];
for (let k = 0; k < 3; k++) { doIterateAntenna(1); seen.push(window.__cvIterCur.antenna); }
console.log(JSON.stringify({seen, ticks: ant_src.selected.indices, bl: bl_src.selected.indices}));""")
        # ticks and ticked baselines untouched; doPlot sets the baselines aside
        assert r == {"seen": [1, 3, 1], "ticks": [1, 3], "bl": [0]}

    def test_shipped_baseline_stepper_uses_the_subset(self):
        guard = "if (cand.length < 2) return;"
        r = _node(abs_.iterate_baseline_js(guard) + r"""
const ant_src = src({name: ['A', 'B', 'C']}, []);
const bl_src = src({ant1: ['A', 'A', 'B'], ant2: ['B', 'C', 'C'], has_data: [1, 1, 1]}, [0, 2]);
const seen = [];
for (let k = 0; k < 3; k++) { doIterateBaseline(1); seen.push(window.__cvIterCur.baseline); }
console.log(JSON.stringify({seen, ticks: bl_src.selected.indices}));""")
        assert r == {"seen": [0, 2, 0], "ticks": [0, 2]}

    def test_shipped_steppers_still_run_without_the_helpers(self):
        """antenna_baseline_select's own tests run these functions alone:
        the subset calls are guarded."""
        guard = "if (names.length < 2) return;"
        code = (_HARNESS + STEP_INDEX_JS + abs_.iterate_antenna_js(guard) + r"""
const ant_src = src({name: ['A', 'B', 'C'], has_data: [1, 1, 1]}, [0, 1]);
const bl_src = src({ant1: [], ant2: []}, []);
doIterateAntenna(1); console.log(JSON.stringify(ant_src.selected.indices));""")
        out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout.strip().splitlines()[-1]) == [0]


def test_plotter_wiring():
    import inspect
    from cubevis.toolbox.visplot import visibility_plotter as vp
    src = inspect.getsource(vp)
    assert "const _spw_idx = cvIterEffective(spw_src, 'spw');" in src
    assert "const [_ant_eff, _bl_eff] = cvIterAntBl(ant_src, bl_src);" in src
    assert 'code=_do_plot_js + "cvIterClearAll(spw_src, ant_src, bl_src); cvScanEnd(); doPlot(false);"' in src
    assert "if (cvIterSubsetStep(spw_src, 'spw', idents.map((_, i) => i), delta)) return;" in src
    assert src.count("_itsub.END_ON_TICK_CHANGE_JS") == 1
    assert "_itsub.MARKER_TEMPLATE" in src


# --------------------------------------------------------------------------
# Grid Play follows the ▶ (2026-11-18)
# --------------------------------------------------------------------------

def _play(setup: str, actions: str):
    from cubevis.toolbox.visplot import visibility_plotter as vp
    steppers = (abs_.iterate_antenna_js("if (names.length < 2) return;")
                + abs_.iterate_baseline_js("if (cand.length < 2) return;")
                + SPW_STEP + "function doIterateField(d) {}\n")
    script = setup + steppers + r"""
const field_sel = null;
function doPlot() { window.__cvPlotSent = true; const r = window.__cvPlayStepDone;
                    window.__cvPlayStepDone = null; window.__cvQuietPlot = false; if (r) r(true); }
const cb_obj = {};
""" + vp._GRID_PLAY_JS + "(async () => {" + actions + "})();"
    return _node(script)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
class TestGridPlay:
    TABLES = r"""
const ant_src = src({name: ['A', 'B', 'C', 'D'], has_data: [1, 1, 1, 1]}, %s);
const bl_src = src({ant1: ['A', 'A', 'B'], ant2: ['B', 'C', 'C'], has_data: [1, 1, 1]}, %s);
const spw_src = src({ident: [0]}, [0]);
"""

    def test_one_antenna_ticked_plays_antennas_moving_the_tick(self):
        r = _play(self.TABLES % ("[1]", "[]"), r"""
const label = cb_obj.outer.label();
const seen = [];
for (let k = 0; k < 2; k++) { await cb_obj.outer.step(false);
    seen.push([ant_src.selected.indices.slice(), window.__cvIterCur.antenna]); }
console.log(JSON.stringify({label, seen}));""")
        assert r == {"label": "antennas", "seen": [[[2], 2], [[3], 3]]}

    def test_marker_axis_continues_and_stops_on_the_last(self):
        r = _play(self.TABLES % ("[0, 2, 3]", "[]"), r"""
cvIterSubsetStep(ant_src, 'antenna', [0, 1, 2, 3], 1);          // a Next: ▶ on A
const label = cb_obj.outer.label();
const res = [];
for (let k = 0; k < 3; k++) res.push([await cb_obj.outer.step(false), window.__cvIterCur.antenna]);
console.log(JSON.stringify({label, res, ticks: ant_src.selected.indices}));""")
        # steps to 2, then 3; the third would wrap: stops, ▶ back on 3
        assert r == {"label": "antennas", "res": [[True, 2], [True, 3], [False, 3]],
                     "ticks": [0, 2, 3]}

    def test_with_loop_it_wraps(self):
        r = _play(self.TABLES % ("[0, 2]", "[]"), r"""
cvIterSubsetStep(ant_src, 'antenna', [0, 1, 2, 3], 1);
const res = [];
for (let k = 0; k < 2; k++) res.push([await cb_obj.outer.step(true), window.__cvIterCur.antenna]);
console.log(JSON.stringify(res));""")
        assert r == [[True, 2], [True, 0]]

    def test_nothing_being_stepped_plays_the_most_specific_axis(self):
        r = _play(self.TABLES % ("[]", "[]"), r"""
const label = cb_obj.outer.label();
await cb_obj.outer.step(false);
console.log(JSON.stringify({label, bl: window.__cvIterCur.baseline, ticks: bl_src.selected.indices}));""")
        assert r == {"label": "baselines", "bl": 0, "ticks": []}


def test_block_can_be_included_twice_in_one_script():
    """Each stepper's script and doPlot's carry ITER_SUBSET_JS, so one
    CustomJS contains it twice: no top-level const / let in it.  (A
    `const` here broke every Prev / Next button, 2026-11-18.)"""
    code = "(function() {" + STEP_INDEX_JS + its.ITER_SUBSET_JS + its.ITER_SUBSET_JS + \
           "console.log(JSON.stringify(typeof cvIterSubsetStep));})();"
    out = subprocess.run([NODE, "-e", "const window = {};" + code], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip()) == "function"
