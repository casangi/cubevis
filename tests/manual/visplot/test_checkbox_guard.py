"""
test_checkbox_guard.py
======================
The orphaned-twin guard for ``CheckboxGroup`` (``__cvInstallSelectViewGuard``
in ``visibility_plotter.py``), exercised under node against a model of the
Bokeh 3.10 view it patches.

What is being guarded
---------------------
With no Bokeh server, the first time a widget is added to a dynamically
shown container Bokeh can build TWO views for one model; the second is
never rendered but still receives property changes.  ``Select``,
``RadioButtonGroup`` and ``DataTable`` each crashed on that (see the guard's
own comments).  ``CheckboxGroup`` -- now used for the colorize checklists
and the info-display selectors -- has the same defect, but a different
shape: its ``active`` handler is an *arrow function created inside
``connect_signals()``* that iterates ``enumerate(this._inputs)``, and
``_inputs`` is only assigned in ``render()``.  So a prototype method cannot
be wrapped the way the other guards do; instead ``connect_signals`` is
wrapped to give every view an empty ``_inputs`` first.

The model below reproduces exactly those three properties of Bokeh's
source (class field ``_inputs``, arrow handler in ``connect_signals``,
assignment in ``render``); it is NOT Bokeh itself, and this test says
nothing about whether an orphan actually occurs -- only that IF one does,
the patch stops it throwing without disturbing a real view.  The
authoritative check remains the browser.

Test location
-------------
``cubevis/tests/manual/visplot/test_checkbox_guard.py``
"""

import ast
import json
import pathlib
import shutil
import subprocess

import pytest

_NODE = shutil.which("node")


def _find_plotter() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    for base in here.parents:
        for cand in (base / "cubevis" / "toolbox" / "visplot" / "visibility_plotter.py",
                     base / "visibility_plotter.py"):
            if cand.is_file():
                return cand
    raise RuntimeError("could not locate visibility_plotter.py")


def _find_module(name: str) -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    for base in here.parents:
        for cand in (base / "cubevis" / "toolbox" / "visplot" / name,
                     base / name):
            if cand.is_file():
                return cand
    raise RuntimeError(f"could not locate {name}")


def _module_level_str(path: pathlib.Path, name: str) -> str:
    """A plain ``NAME = "..."`` (or ``f"..."``, or a concatenation of
    those) at module level in the given file, as its runtime string
    value. Used to resolve constants like _CV_SET_BUSY_JS that
    _do_plot_js() may reference rather than inline, per _eval_str_expr's
    own rules -- see that function for what node shapes are supported.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == name
                        for t in node.targets)):
            return _eval_str_expr(node.value, {})
    raise RuntimeError(f"{name} not found in {path}")


def _eval_str_expr(node: ast.AST, known: dict) -> str:
    """Resolve a module-level string-valued expression to its actual
    runtime value: a plain string/f-string constant, string
    concatenation via ``+`` (either side may itself be a NAME already
    resolved into ``known``), or a bare NAME already in ``known``.
    Deliberately narrow -- just enough to follow how _do_plot_js is
    actually built (a NAME plus a triple-quoted string, joined with +)
    without needing a full expression evaluator; raises on anything else.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):  # an f-string with no {} left unresolved is fine
        return ast.literal_eval(node)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _eval_str_expr(node.left, known) + _eval_str_expr(node.right, known)
    if isinstance(node, ast.Name) and node.id in known:
        return known[node.id]
    raise ValueError(f"unsupported node in string expression: {ast.dump(node)}")


def _do_plot_js() -> str:
    cv_set_busy_js = _module_level_str(_find_module("visibility_plot.py"), "_CV_SET_BUSY_JS")
    tree = ast.parse(_find_plotter().read_text())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "_do_plot_js"
                        for t in node.targets)):
            return _eval_str_expr(node.value, {"_CV_SET_BUSY_JS": cv_set_busy_js})
    raise RuntimeError("_do_plot_js not found")


def _gear_click_js_guard_block() -> str:
    """The OTHER copy of the installer (gear_click_js), as text."""
    src = _find_plotter().read_text()
    return src


_HARNESS = r"""
globalThis.window = globalThis;
function* enumerate(seq) { let i = 0; for (const x of seq) yield [x, i++]; }

// --- a model of Bokeh 3.10's CheckboxGroupView (see module docstring) ----
class ToggleInputGroupView {
    _inputs;                                   // class field: own, undefined
    constructor(model) { this.model = model; this.handlers = []; }
    on_change(prop, fn) { this.handlers.push(fn); }
    connect_signals() {}
}
class CheckboxGroupView extends ToggleInputGroupView {
    connect_signals() {
        super.connect_signals();
        this.on_change('active', () => {
            for (const [el, i] of enumerate(this._inputs)) {
                el.checked = this.model.active.includes(i);
            }
        });
    }
    render() { this._inputs = this.model.labels.map(() => ({checked: false})); }
}
const model = () => ({type: 'CheckboxGroup', labels: ['a', 'b'], active: [0]});
const fire = (v) => v.handlers.forEach(h => h());

const result = {};

// 1. unguarded: the orphan throws (this is the defect)
{
    const orphan = new CheckboxGroupView(model());
    orphan.connect_signals();
    try { fire(orphan); result.unguarded_orphan_throws = false; }
    catch (e) { result.unguarded_orphan_throws = true; }
}

// 2. install the REAL guard code from the plotter, reaching the prototype
//    through an existing rendered view, as in the app (corr_cbg)
const permanent = new CheckboxGroupView(model());
permanent.connect_signals(); permanent.render();
permanent.model.type = 'CheckboxGroup';
const root = {model: {type: 'Row'}, _child_views: new Map([[1, permanent]])};
window.Bokeh = {index: {r0: root}};

%(code)s
window.__cvInstallSelectViewGuard();
result.flag_set = !!window.__cvCheckboxGroupViewGuarded;

// 3. guarded: orphan no longer throws
{
    const orphan = new CheckboxGroupView(model());
    orphan.connect_signals();
    try { fire(orphan); result.guarded_orphan_throws = false; }
    catch (e) { result.guarded_orphan_throws = true; result.err = String(e); }
}

// 4. guarded: a REAL view (connect_signals -> render) still tracks `active`
{
    const m = model();
    const real = new CheckboxGroupView(m);
    real.connect_signals(); real.render();
    m.active = [1];
    fire(real);
    result.real_view_synced = (real._inputs[0].checked === false && real._inputs[1].checked === true);
}

// 5. idempotent: running the installer again must not double-wrap
{
    const before = CheckboxGroupView.prototype.connect_signals;
    window.__cvInstallSelectViewGuard();
    result.idempotent = (CheckboxGroupView.prototype.connect_signals === before);
}
console.log(JSON.stringify(result));
"""


@pytest.mark.skipif(_NODE is None, reason="node not available")
def test_checkbox_orphan_guard():
    code = _do_plot_js()
    # only the top-level definitions matter; doPlot() is never called here
    harness = _HARNESS % {"code": code}
    res = subprocess.run([_NODE, "-e", harness], capture_output=True, text=True,
                         timeout=60)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["unguarded_orphan_throws"] is True, "model no longer reproduces the defect"
    assert out["flag_set"] is True
    assert out["guarded_orphan_throws"] is False, out.get("err")
    assert out["real_view_synced"] is True
    assert out["idempotent"] is True


def test_both_copies_of_the_installer_carry_the_checkbox_guard():
    # __cvInstallSelectViewGuard is defined in gear_click_js AND _do_plot_js
    # (whichever runs first wins); an edit to one must not miss the other.
    src = _find_plotter().read_text()
    assert src.count("window.__cvInstallSelectViewGuard = function()") == 2
    assert src.count("window.__cvCheckboxGroupViewGuarded = true;") == 2
    assert src.count("cproto.connect_signals = function()") == 2


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
