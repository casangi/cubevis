"""The launcher a kernel's ssh runs instead of python (remote session
design 7b, stage 2b part iii): working directory, activation, variables.

    pytest tests/manual/setup/test_launcher.py
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


def _env(tmp_path, kind, **kw):
    prefix = tmp_path / "env"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(prefix)], check=True)
    if kind == "conda":
        (prefix / "conda-meta").mkdir()
    e = {"kind": kind, "python": str(prefix / "bin" / "python"), "prefix": str(prefix)}
    e.update(kw)
    return e


def _launch(tmp_path, e, code):
    p = tmp_path / "launch.sh"
    assert L.write(p, L.text("hrs", "cv", "zuul06", e, str(tmp_path)))
    assert not L.write(p, L.text("hrs", "cv", "zuul06", e, str(tmp_path)))   # unchanged
    assert stat.S_IMODE(p.stat().st_mode) == 0o700
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    return subprocess.run([str(p), "-c", code], capture_output=True, text=True, env=env,
                          cwd="/")


def test_venv_working_directory_and_variables(tmp_path):
    work = tmp_path / "data with space"
    work.mkdir()
    e = _env(tmp_path, "venv", workdir=str(work), env={"MSG": "it's here", "OMP_NUM_THREADS": "4"})
    r = _launch(tmp_path, e, "import os, sys; print(os.getcwd()); print(os.environ['MSG']); "
                             "print(os.environ['OMP_NUM_THREADS']); print(sys.prefix); "
                             "print(os.environ['VIRTUAL_ENV']); print(os.environ['PATH'].split(':')[0])")
    assert r.returncode == 0, r.stderr
    out = r.stdout.splitlines()
    assert out[:3] == [str(work), "it's here", "4"]
    assert out[3] == out[4] == e["prefix"] and out[5] == e["prefix"] + "/bin"


def test_home_is_the_default_and_a_missing_directory_stops_it(tmp_path):
    e = _env(tmp_path, "python")
    r = _launch(tmp_path, e, "import os; print(os.getcwd())")
    assert r.stdout.strip() == str(tmp_path)                # home (as given to text())
    e["workdir"] = str(tmp_path / "gone")
    r = _launch(tmp_path, e, "print('ran')")
    assert r.returncode == 1 and "ran" not in r.stdout
    assert "cannot change to" in r.stderr and "--redo node-here cv" in r.stderr


def test_conda_activation_scripts_run_and_their_output_goes_to_stderr(tmp_path):
    e = _env(tmp_path, "conda", env={"MSG": "mine"})
    d = tmp_path / "env" / "etc" / "conda" / "activate.d"
    d.mkdir(parents=True)
    (d / "proj.sh").write_text('echo "activating" ; export PROJ_DATA="$CONDA_PREFIX/share/proj"\n'
                               'export MSG=theirs\n')
    r = _launch(tmp_path, e, "import os; print(os.environ['PROJ_DATA']); "
                             "print(os.environ['CONDA_PREFIX']); print(os.environ['CONDA_DEFAULT_ENV']); "
                             "print(os.environ['MSG'])")
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [e["prefix"] + "/share/proj", e["prefix"], "env", "mine"]
    assert "activating" in r.stderr and "activating" not in r.stdout   # sshpyk reads stdout
