"""Interactive clean, local and remote, with real tclean (iclean design step
6, notes §79):

* the GUI's handlers give the same replies with the imaging layer in this
  process and on a kernel (``record_iclean_handlers.py`` twice,
  ``compare_records.py --remote``);
* a kernel that starts in another directory than this Python: relative
  paths are the kernel's -- found and written there, checked there (the
  validation Darrell hit, notes §72);
* ``iclean.notebook(..., kernel_name=)`` in a notebook kernel, against
  another kernel.

    pytest tests/manual/iclean-imaging/test_iclean_parity.py

Needs casatools and casatasks, jupyter_client and ipykernel (nbclient and
nbformat for the notebook); skipped without.  A small simulated MS is made
once (``make_sim_ms.py``, a few seconds).  The kernels are started on this
computer (``python3``, and one written here that starts elsewhere).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

import pytest

for _m in ("casatools", "casatasks", "jupyter_client", "ipykernel"):
    if importlib.util.find_spec(_m) is None:
        pytest.skip(f"needs {_m}", allow_module_level=True)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
ENV = dict(os.environ, BROWSER="true", PYTHONPATH=REPO + os.pathsep + os.environ.get("PYTHONPATH", ""))


@pytest.fixture(scope="module")
def ms(tmp_path_factory):
    d = tmp_path_factory.mktemp("ms")
    path = str(d / "sim.ms")
    r = subprocess.run([sys.executable, os.path.join(HERE, "make_sim_ms.py"), path], cwd=str(d),
                       capture_output=True, text=True, env=ENV, timeout=600)
    assert r.returncode == 0 and os.path.isdir(path), r.stdout + r.stderr
    return path


def _record(ms, work, out, *extra):
    r = subprocess.run([sys.executable, os.path.join(HERE, "record_iclean_handlers.py"), ms, out,
                        *extra], cwd=str(work), capture_output=True, text=True, env=ENV,
                       timeout=1200)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]


def test_the_handlers_reply_the_same_here_and_on_a_kernel(ms, tmp_path):
    """Same directory, the images removed between: what differs is only what
    differs by design (the casatools coordsys stays on the host; the
    connection's call counts)."""
    work = tmp_path / "work"
    work.mkdir()
    _record(ms, work, str(tmp_path / "local.pkl"), "--absolute")
    for p in work.iterdir():
        if p.name.startswith("bl."):
            subprocess.run(["rm", "-rf", str(p)], check=True)
    _record(ms, work, str(tmp_path / "remote.pkl"), "--kernel", "python3")
    r = subprocess.run([sys.executable, os.path.join(HERE, "compare_records.py"),
                        str(tmp_path / "local.pkl"), str(tmp_path / "remote.pkl"), "--remote"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
    assert "IDENTICAL" in r.stdout


@pytest.fixture
def elsewhere(tmp_path, monkeypatch, ms):
    """A kernel that starts in its own directory (holding the MS as sim.ms),
    found through JUPYTER_PATH; this Python runs in another directory."""
    host, here = tmp_path / "host", tmp_path / "here"
    host.mkdir()
    here.mkdir()
    os.symlink(ms, host / "sim.ms")
    kdir = tmp_path / "jupyter" / "kernels" / "elsewhere"
    kdir.mkdir(parents=True)
    (kdir / "kernel.json").write_text(json.dumps({
        "argv": ["/bin/sh", "-c", f'cd "{host}" && exec "{sys.executable}" -m ipykernel_launcher '
                                  '-f "$0"', "{connection_file}"],
        "display_name": "elsewhere (test)", "language": "python",
        "env": {"PYTHONPATH": ENV["PYTHONPATH"]}}))
    monkeypatch.setenv("JUPYTER_PATH", str(tmp_path / "jupyter"))
    monkeypatch.setenv("BROWSER", "true")
    monkeypatch.chdir(here)
    return host, here


def _args(**over):
    """iclean's arguments (every parameter, its default) as InteractiveClean
    gets them, with the test's values."""
    import inspect
    from cubevis.toolbox.iclean import InteractiveClean
    sig = inspect.signature(InteractiveClean.__init__)
    args = {k: p.default for k, p in sig.parameters.items()
            if k not in ("self", "vis", "imagename", "iclean_backend", "kernel_name")}
    args.update(imsize=64, cell="12.0arcsec", specmode="cube", interpolation="nearest", nchan=3,
                start="1.0GHz", width="0.2GHz", pblimit=-1e-05, deconvolver="hogbom",
                threshold="0.001Jy", cyclefactor=3, niter=20, cycleniter=10)
    args.update(over)
    return args


def test_relative_paths_are_the_kernels(elsewhere):
    host, here = elsewhere
    from cubevis.imaging import RemoteImagingSession
    s = RemoteImagingSession(_args(vis="sim.ms", imagename="img"), "elsewhere")
    try:
        assert s.args["vis"] == "sim.ms" and s.args["cyclefactor"] == 3.0     # checked there
        assert s.start().stopcode == (0, 0)
        r = s.run_cycle()
        assert r.stopcode[0] != -1, r.stopdesc
        assert s.connection.call("runtime_info")["cwd"] == str(host)
    finally:
        s.close()
    assert (host / "img.residual").is_dir() and not list(here.iterdir())


def test_a_path_not_on_the_kernels_side_is_refused_there(elsewhere):
    """The MS is here, not where the kernel runs: refused by the kernel's
    check, naming its directory (the laptop's check accepted it before)."""
    host, here = elsewhere
    os.symlink(host / "sim.ms", here / "only-here.ms")
    from cubevis import iclean
    with pytest.raises(ValueError) as exc:
        iclean(vis="only-here.ms", imagename="img", kernel_name="elsewhere")
    msg = str(exc.value)
    assert msg.startswith("kernel 'elsewhere': iclean's parameters (checked on ")
    assert f"in {host})" in msg and '"only-here.ms" does not exist' in msg


@pytest.mark.skipif(importlib.util.find_spec("nbclient") is None
                    or importlib.util.find_spec("nbformat") is None, reason="nbclient, nbformat")
def test_the_notebook_variant_on_a_kernel(elsewhere):
    """``iclean.notebook(..., kernel_name=)`` in a notebook kernel (python3),
    the imaging on another (elsewhere): the application is made with a
    remote session, the arguments checked over there (the page itself needs a
    browser, as notebook_check.py says)."""
    import nbformat
    from nbclient import NotebookClient
    host, here = elsewhere
    kw = ("vis='sim.ms', imagename='nb', imsize=64, cell='12.0arcsec', specmode='cube', nchan=3, "
          "start='1.0GHz', width='0.2GHz', niter=20, cycleniter=10, kernel_name='elsewhere'")
    cells = [
        f"import sys; sys.path.insert(0, {REPO!r})\nfrom cubevis import iclean",
        # the task: what a user types (it returns the page to show)
        f"w = iclean.notebook({kw})\nprint('TASK', type(w).__name__)",
        # the application under it, to look at its session
        "from cubevis.toolbox.iclean import InteractiveCleanNotebook\n"
        f"ic = InteractiveCleanNotebook({kw})\n"
        "print('SESSION', type(ic._session).__name__, ic._args['vis'], ic._args['niter'])\n"
        "print('CWD', ic._session.connection.call('runtime_info')['cwd'])\n"
        "ic._session.close()",
    ]
    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell(c) for c in cells])
    NotebookClient(nb, kernel_name="python3", timeout=600, allow_errors=True,
                   resources={"metadata": {"path": str(here)}}).execute()
    out = []
    for c in nb.cells:
        for o in c.get("outputs", []):
            out.append(o.get("text") or "")
            if o.get("output_type") == "error":
                pytest.fail(f"{o.get('ename')}: {o.get('evalue')}\n" + "\n".join(o.get("traceback", [])))
    text = "".join(out)
    assert "TASK Showable" in text, text
    assert "SESSION RemoteImagingSession sim.ms 20" in text, text
    assert f"CWD {host}" in text, text
