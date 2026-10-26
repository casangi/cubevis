"""The launcher a kernel's ssh runs instead of python (remote session
design 7b, 7c 4a): one per kernel -- working directory, activation,
variables -- written on the node by a small program for its python3, and
tested by running ``TEST_CODE`` through it.

    pytest tests/manual/setup/test_launcher.py
    CUBEVIS_TEST_KERNEL_PYTHON=/path/to/python pytest ...   # + the test run
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys

import pytest

from cubevis.setup import launcher as L


def test_variables_parse_quote_and_refuse():
    env = L.parse_vars("HDF5_USE_FILE_LOCKING=FALSE MSG='hello there' EMPTY=")
    assert env == {"HDF5_USE_FILE_LOCKING": "FALSE", "MSG": "hello there", "EMPTY": ""}
    assert L.parse_vars(L.format_vars(env)) == env
    assert L.parse_vars("") == {}
    for bad, why in (("NOEQUALS", "NAME=value"), ("1X=2", "not an environment variable"),
                     ("PATH=/x", "set by the launcher"), ("A-B=1", "not an environment")):
        with pytest.raises(ValueError, match=why):
            L.parse_vars(bad)


KERNEL_PY = os.environ.get("CUBEVIS_TEST_KERNEL_PYTHON")


def _env(tmp_path, kind, python=None):
    prefix = tmp_path / "env"
    subprocess.run([python or sys.executable, "-m", "venv", "--without-pip", str(prefix)],
                   check=True)
    if kind == "conda":
        (prefix / "conda-meta").mkdir()
    return {"kind": kind, "python": str(prefix / "bin" / "python"), "prefix": str(prefix)}


def _install(path, text, python=sys.executable):
    """The install program, run as setup runs it on a node: ``python3 -``."""
    r = subprocess.run([python, "-"], input=L.install_script(str(path), text),
                       capture_output=True, text=True)
    return L.parse_install(r.stdout)


def _launch(tmp_path, e, code, k=None):
    k = k or {}
    p = tmp_path / ".config" / "cubevis" / L.relpath("gui1", "hrs")
    text = L.text("hrs", k, e, str(tmp_path), "gui1", "zuul06")
    assert _install(p, text) == ("written", str(p))
    assert _install(p, text) == ("same", str(p))                   # unchanged
    assert stat.S_IMODE(p.stat().st_mode) == 0o700
    assert str(p).endswith(".config/cubevis/launch/gui1/hrs.sh")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    return subprocess.run([str(p), "-c", code], capture_output=True, text=True, env=env,
                          cwd="/")


def test_venv_working_directory_and_variables(tmp_path):
    work = tmp_path / "data with space"
    work.mkdir()
    e = _env(tmp_path, "venv")
    k = {"workdir": str(work), "env": {"MSG": "it's here", "OMP_NUM_THREADS": "4"}}
    r = _launch(tmp_path, e, "import os, sys; print(os.getcwd()); print(os.environ['MSG']); "
                             "print(os.environ['OMP_NUM_THREADS']); print(sys.prefix); "
                             "print(os.environ['VIRTUAL_ENV']); print(os.environ['PATH'].split(':')[0])",
                k)
    assert r.returncode == 0, r.stderr
    out = r.stdout.splitlines()
    assert out[:3] == [str(work), "it's here", "4"]
    assert out[3] == out[4] == e["prefix"] and out[5] == e["prefix"] + "/bin"


def test_home_is_the_default_and_a_missing_directory_stops_it(tmp_path):
    e = _env(tmp_path, "python")
    r = _launch(tmp_path, e, "import os; print(os.getcwd())")
    assert r.stdout.strip() == str(tmp_path)                # home (as given to text())
    r = _launch(tmp_path, e, "print('ran')", {"workdir": str(tmp_path / "gone")})
    assert r.returncode == 1 and "ran" not in r.stdout
    assert "cannot change to" in r.stderr
    assert "python -m cubevis.setup zuul06 --kernel hrs changes it" in r.stderr


def test_conda_activation_scripts_run_and_their_output_goes_to_stderr(tmp_path):
    e = _env(tmp_path, "conda")
    d = tmp_path / "env" / "etc" / "conda" / "activate.d"
    d.mkdir(parents=True)
    (d / "proj.sh").write_text('echo "activating" ; export PROJ_DATA="$CONDA_PREFIX/share/proj"\n'
                               'export MSG=theirs\n')
    r = _launch(tmp_path, e, "import os; print(os.environ['PROJ_DATA']); "
                             "print(os.environ['CONDA_PREFIX']); print(os.environ['CONDA_DEFAULT_ENV']); "
                             "print(os.environ['MSG'])", {"env": {"MSG": "mine"}})
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [e["prefix"] + "/share/proj", e["prefix"], "env", "mine"]
    assert "activating" in r.stderr and "activating" not in r.stdout   # sshpyk reads stdout


def test_install_refuses_quietly_and_parsing(tmp_path):
    """A launcher that cannot be written says why (a file where its
    directory should be); the parsers read only their marker lines."""
    (tmp_path / "launch").write_text("not a directory")
    st, why = _install(tmp_path / "launch" / "gui1" / "k.sh", "x")
    assert st == "error" and why
    assert L.parse_install("noise\nCUBEVIS-LAUNCHER same /a b/c.sh\n") == ("same", "/a b/c.sh")
    assert L.parse_install("nothing") == (None, "")
    assert L.parse_test("x\nCUBEVIS-LAUNCH-OK k1-abc /home/jo/my data\n") == ("k1-abc", "/home/jo/my data")
    assert L.parse_test("Traceback ...") is None
    assert "'" not in L.TEST_CODE and "$" not in L.TEST_CODE and "!" not in L.TEST_CODE


@pytest.mark.skipif(not KERNEL_PY, reason="set CUBEVIS_TEST_KERNEL_PYTHON")
def test_the_test_run_reports_the_fingerprint_and_directory(tmp_path):
    """What setup runs through a new launcher: imports, fingerprint, cwd."""
    from cubevis._fingerprint import fingerprint
    e = {"kind": "python", "python": KERNEL_PY, "prefix": os.path.dirname(os.path.dirname(KERNEL_PY))}
    r = _launch(tmp_path, e, L.TEST_CODE, {"workdir": str(tmp_path)})
    assert L.parse_test(r.stdout) == (fingerprint(), str(tmp_path)), r.stdout + r.stderr
