"""The JupyterLab embed script waits for BokehJS and cubevisjs
(``_showable._wait_for_bokehjs``; 2026-11-24).

``components()`` assumes BokehJS is already loaded.  A notebook output that
JupyterLab restores (page reload, notebook reopened) runs before the restored
loader has loaded it ("Bokeh is not defined"), so the visplot output stayed
empty; with the wrapper it comes back and, while the kernel still runs the
session, carries it on (checked in JupyterLab 4.6 with
devel/tools/jupyterlab_headless).

Runs the wrapped script under node with a fake window.
"""
import json
import shutil
import subprocess
import textwrap

import pytest

from cubevis.bokeh.models._showable import _wait_for_bokehjs, Showable

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

SCRIPT = '<script type="text/javascript">\n  window.__runs = (window.__runs || 0) + 1;\n</script>'
MODEL = "cubevis.bokeh.models._showable.Showable"


def _run(setup_js: str, ms: int = 400) -> dict:
    """Run the wrapped SCRIPT after *setup_js*; report runs at 0 ms and at the end."""
    wrapped = _wait_for_bokehjs(SCRIPT, Showable)
    body = wrapped.split(">", 1)[1].rsplit("</script>", 1)[0]
    js = textwrap.dedent(f"""
        const window = globalThis.window = {{}};
        const console = globalThis.console;
        {setup_js}
        {body}
        const at0 = window.__runs || 0;
        setTimeout(() => {{
          process.stdout.write(JSON.stringify({{at0, end: window.__runs || 0}}));
          process.exit(0);
        }}, {ms});
    """)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _bokeh(models):
    return ("{embed: {}, Models: {get: (n) => (" + json.dumps(models) + ").includes(n) ? function(){} : undefined}}")


def test_runs_at_once_when_everything_is_loaded():
    assert _run(f"window.Bokeh = {_bokeh([MODEL])};") == {"at0": 1, "end": 1}


def test_waits_for_bokehjs():
    r = _run(f"setTimeout(() => {{ window.Bokeh = {_bokeh([MODEL])}; }}, 150);")
    assert r == {"at0": 0, "end": 1}


def test_waits_for_the_cubevis_models():
    # BokehJS first, cubevisjs (which registers the model) later.
    r = _run(f"window.Bokeh = {_bokeh([])};"
             f" setTimeout(() => {{ window.Bokeh = {_bokeh([MODEL])}; }}, 150);")
    assert r == {"at0": 0, "end": 1}


def test_never_runs_without_bokehjs():
    assert _run("") == {"at0": 0, "end": 0}


def test_the_model_name_is_the_qualified_one():
    assert f"'{MODEL}'" in _wait_for_bokehjs(SCRIPT, Showable)


def test_a_script_without_a_script_tag_is_left_alone():
    assert _wait_for_bokehjs("not a script", Showable) == "not a script"
