"""Where iclean's parameters are checked (2026-11-09, notes §72): in the
user's Python for a local iclean, as before; on the kernel's host with
``kernel_name=`` -- the paths are the host's (relative ones relative to the
kernel's working directory), and the computer running the GUI needs no
CASA for it.

    pytest tests/manual/iclean-imaging/test_task_params.py

The wrapper's remote branch is checked with casatools and casatasks made
unimportable; the check itself and the worker's use of it need casatools
(skipped without); the refusal through a kernel needs jupyter_client and a
``python3`` kernel with casatools.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap

import pytest

HAS_CASATOOLS = importlib.util.find_spec('casatools') is not None
needs_casatools = pytest.mark.skipif(not HAS_CASATOOLS, reason='casatools')
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def test_with_a_kernel_the_wrapper_checks_nothing_here(tmp_path):
    """``iclean(..., kernel_name=)``: no casatools, no casatasks imported, the
    arguments go to the application as given (a path not here is fine)."""
    code = textwrap.dedent('''
        import sys, importlib
        for m in ("casatools", "casatasks"):
            sys.modules[m] = None                      # import fails
        for name in ("iclean", "iclean_notebook"):
            mod = importlib.import_module("cubevis.private.casatasks." + name)
            got = {}
            setattr(mod, "_%s_t" % name, lambda **kw: got.update(kw) or "ran")
            task = getattr(mod, name)
            assert task(vis="only-on-the-host.ms", imagename="x", niter=7,
                        kernel_name="k") == "ran", name
            assert got["vis"] == "only-on-the-host.ms" and got["niter"] == 7
            assert got["kernel_name"] == "k" and got["iclean_backend"] == "PROD"
            assert "cyclefactor" in got                # every parameter, as given
        print("OK")
    ''')
    r = subprocess.run([sys.executable, '-c', code], cwd=str(tmp_path), capture_output=True,
                       text=True, env=dict(os.environ, PYTHONPATH=REPO), timeout=120)
    assert r.stdout.strip().endswith('OK'), r.stdout + r.stderr
    assert os.listdir(tmp_path) == []                   # no CASA log written here either


@needs_casatools
def test_the_check_is_tcleans(tmp_path, monkeypatch):
    from cubevis.imaging import _tclean_params as P
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'here.ms').mkdir()
    doc = P.check({'vis': 'here.ms', 'imagename': 'x', 'imsize': 64, 'cyclefactor': 3,
                   'not-a-parameter': 1})
    assert doc['vis'] == 'here.ms' and doc['imsize'] == 64
    assert doc['cyclefactor'] == 3.0 and isinstance(doc['cyclefactor'], float)   # coerced
    assert 'not-a-parameter' not in doc
    assert P.names()[:2] == ['vis', 'selectdata'] and 'kernel_name' not in P.names()
    with pytest.raises(AssertionError, match='"nope.ms" does not exist'):
        P.check({'vis': 'nope.ms', 'imagename': 'x'})


@needs_casatools
def test_the_worker_checks_in_its_own_directory(tmp_path, monkeypatch):
    """``ImagingSessionBackend(check=True)``: checked where it runs; refused
    with what is wrong and where; ``args()`` the coerced arguments."""
    from cubevis.imaging.remote_registrations import ImagingSessionBackend, PARAMS_ERROR
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'here.ms').mkdir()
    b = ImagingSessionBackend({'vis': 'here.ms', 'imagename': 'x', 'imsize': [20, 14],
                               'cyclefactor': 2},
                              deconvolver='cubevis.imaging.testing:ScriptedDeconvolver',
                              store='cubevis.imaging.testing:numpy_store', check=True)
    try:
        args = b.args()
        assert args['cyclefactor'] == 2.0 and args['imsize'] == [20, 14]
    finally:
        b.close()
    with pytest.raises(ValueError) as exc:
        ImagingSessionBackend({'vis': 'nope.ms', 'imagename': 'x'}, check=True)
    assert str(exc.value).startswith(PARAMS_ERROR) and f'in {tmp_path}' in str(exc.value)
    assert '"nope.ms" does not exist' in str(exc.value)


def _kernel_has_casatools(name):
    try:
        from jupyter_client.kernelspec import KernelSpecManager
        argv = KernelSpecManager().get_kernel_spec(name).argv
    except Exception:
        return False
    return subprocess.run([argv[0], '-c', 'import casatools'], capture_output=True).returncode == 0


@pytest.mark.skipif(not (importlib.util.find_spec('jupyter_client') and _kernel_has_casatools('python3')),
                    reason='a python3 kernel with casatools')
def test_a_path_not_on_the_host_is_refused_plainly():
    """Through a kernel: a ValueError naming the kernel, the host, its
    directory and what is wrong -- not the worker's traceback -- and no
    kernel left running."""
    from cubevis.imaging import RemoteImagingSession
    with pytest.raises(ValueError) as exc:
        RemoteImagingSession({'vis': 'nowhere-to-be-found.ms', 'imagename': 'x'}, 'python3')
    msg = str(exc.value)
    assert msg.startswith("kernel 'python3': iclean's parameters (checked on ")
    assert '"nowhere-to-be-found.ms" does not exist' in msg and 'Traceback' not in msg


@needs_casatools
def test_the_casashell_binding_passes_a_kernel_through(tmp_path, monkeypatch):
    """casashell's ``iclean`` (inp/go): with ``kernel_name=`` nothing is
    checked here and the arguments -- defaults filled in -- go to the
    application with the kernel (notes §79).  casashell is stood in for when
    it is not installed: only its stack helpers are used."""
    import types
    if importlib.util.find_spec("casashell") is None:
        sm = types.ModuleType("casashell.private.stack_manip")
        sm.find_local = lambda *a, **k: (lambda f: f)
        sm.find_frame = lambda *a, **k: {}
        for name, mod in (("casashell", types.ModuleType("casashell")),
                          ("casashell.private", types.ModuleType("casashell.private")),
                          ("casashell.private.stack_manip", sm)):
            monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.chdir(tmp_path)
    mod = importlib.import_module("cubevis.private.casashell.iclean")
    got = {}
    monkeypatch.setattr(mod, "_iclean_t", lambda **kw: got.update(kw) or "ran")
    assert mod.iclean(vis="only-on-the-host.ms", imagename="x", niter=7, kernel_name="k") == "ran"
    assert got["kernel_name"] == "k" and got["iclean_backend"] == "PROD"
    assert got["vis"] == "only-on-the-host.ms" and got["niter"] == 7 and "cyclefactor" in got
