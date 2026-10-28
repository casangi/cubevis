"""The program setup sends to a node to make, check and remove its own
environments (``cubevis.setup.envmake``; design 7c, stage 2c step 3).

    pytest tests/manual/setup/test_envmake.py

No ssh: the program runs here under ``python3 -`` exactly as setup sends it,
with a fake ``conda`` (a shell script: ``create -y -p PREFIX ...`` makes a
venv with ``conda-meta``; ``env remove -y -p PREFIX`` removes it).  The
fake is steered by ``FAKE_CONDA_FAIL=create|remove`` in the environment of
the program (the detached worker inherits it).

Every test runs the node program under this Python and, when there is one,
under a real Python 3.6 (RHEL 8's python3; ``CUBEVIS_TEST_PY36`` or
``/opt/py36/bin/python3.6``), so the worker and the actions are checked on
the oldest interpreter a node may have.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from cubevis.setup import envmake as E

FAKE_CONDA = r'''#!/bin/sh
case "$1 $2" in
  "create -y")
      [ "$FAKE_CONDA_FAIL" = create ] && { mkdir -p "$4"; echo "fake conda: solver failed" >&2; exit 1; }
      "%(py)s" -m venv --without-pip "$4" && mkdir -p "$4/conda-meta" ;;
  "env remove")
      [ "$FAKE_CONDA_FAIL" = remove ] && { echo "fake conda: cannot remove" >&2; exit 1; }
      rm -rf "$5" ;;
  *) echo "fake conda: $*" >&2; exit 1;;
esac
'''


PY36 = os.environ.get("CUBEVIS_TEST_PY36") or "/opt/py36/bin/python3.6"
NODE_PYTHONS = [pytest.param(sys.executable, id="this-python"),
                pytest.param(PY36, id="python36", marks=pytest.mark.skipif(
                    not os.path.exists(PY36), reason="no Python 3.6 (CUBEVIS_TEST_PY36)"))]


@pytest.fixture(params=NODE_PYTHONS)
def node(tmp_path, request):
    node_python = request.param
    conda = tmp_path / "bin" / "conda"
    conda.parent.mkdir()
    conda.write_text(FAKE_CONDA % {"py": sys.executable})
    conda.chmod(0o755)
    cfg = tmp_path / "config"
    envs = tmp_path / "envs"
    envs.mkdir()

    class Node:
        tool, config_dir, envs_dir = str(conda), str(cfg), str(envs)
        tmp = tmp_path
        python = node_python

        def call(self, action, env=None, **req):
            req = dict(action=action, config_dir=self.config_dir, marker=E.MARKER_FILE, **req)
            r = subprocess.run([self.python, "-"], input=E.payload(req), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True,
                               env=dict(os.environ, **(env or {})), timeout=60)
            lines = [l for l in r.stdout.splitlines() if l.startswith(E.ANSWER)]
            assert lines, (r.stdout, r.stderr)
            return json.loads(lines[-1][len(E.ANSWER):])

        def start(self, name="cubevis-1.0.97", env=None, **extra):
            req = dict(name=name, prefix=f"{self.envs_dir}/{name}", tool=self.tool, python="3.12",
                       spec=E.spec("1.0.97"), version="1.0.97", setup={"host": "gui1"})
            req.update(extra)
            return self.call("start", env=env, **req)

        def wait(self, name="cubevis-1.0.97", states=("ok", "failed", "died")):
            for _ in range(300):
                st = self.call("status", name=name)["status"]
                if st and st["state"] in states:
                    return st
                time.sleep(0.1)
            pytest.fail(f"still {st}")

    return Node()


def test_names_and_the_spec():
    assert E.base_name("1.0.97") == "cubevis-1.0.97"
    assert E.base_name("1.0.97.dev3+gabc") == "cubevis-1.0.97.dev3-gabc"
    assert E.candidate_names("1.0.97")[:3] == ["cubevis-1.0.97", "cubevis-1.0.97-2", "cubevis-1.0.97-3"]
    assert E.spec("1.0.97") == "cubevis[visplot]==1.0.97"
    assert E.pick_tool([{"name": "micromamba"}, {"name": "mamba"}, {"name": "conda"}])["name"] == "conda"
    assert E.pick_tool([{"name": "micromamba"}, {"name": "mamba"}])["name"] == "mamba"
    assert E.pick_tool([]) is None
    assert E.human_bytes(1536 * 1024 ** 2) == "1.5 GB" and E.human_bytes(None) == "unknown"


def test_plan_says_where_and_what_is_taken(node):
    (node.tmp / "envs" / "cubevis-1.0.97").mkdir()
    r = node.call("plan", dirs=[node.envs_dir, str(node.tmp / "new" / "deeper"), "/proc/nothing-here"],
                  names=E.candidate_names("1.0.97"))
    assert r["ok"] and r["jobs"] == {}
    a, b, c = r["dirs"]
    assert a["writable"] and a["exists"] and a["taken"] == ["cubevis-1.0.97"] and a["free"] > 0
    assert b["writable"] and not b["exists"] and b["taken"] == []      # would be made
    if os.geteuid() != 0:                    # root can write anywhere: nothing to see
        assert not c["writable"]


def test_the_default_install_is_pip_in_the_new_environment_and_no_marker_until_it_works(node):
    """No install override: the worker runs ``PREFIX/bin/python -m pip
    install SPEC``.  The fake's venv has no pip, so it fails -- which shows
    that the marker is written only after the install, and that the step
    reached is recorded."""
    r = node.start()
    assert r["ok"] and r["started"], r
    st = node.wait()
    assert st["state"] == "failed" and st["done"] == ["create"], st
    assert "pip install cubevis[visplot]==1.0.97 failed" in st["error"]
    assert not os.path.exists(f"{node.envs_dir}/cubevis-1.0.97/{E.MARKER_FILE}")
    log = open(st["log"]).read()
    assert f"$ {node.envs_dir}/cubevis-1.0.97/bin/python -m pip install cubevis[visplot]==1.0.97" in log
    assert f"$ {node.tool} create -y -p {node.envs_dir}/cubevis-1.0.97 python=3.12 pip" in log


def test_a_complete_environment_has_its_marker_and_is_listed(node):
    r = node.start(install_shell='echo installing into "$PREFIX"; touch "$PREFIX/installed"')
    assert r["ok"] and r["started"]
    st = node.wait()
    assert st["state"] == "ok", st
    prefix = f"{node.envs_dir}/cubevis-1.0.97"
    marker = json.load(open(f"{prefix}/{E.MARKER_FILE}"))
    assert marker["schema"] == 1 and marker["name"] == "cubevis-1.0.97" and marker["version"] == "1.0.97"
    assert marker["spec"] == "cubevis[visplot]==1.0.97" and marker["python"] == "3.12"
    assert marker["setup"] == {"host": "gui1"} and marker["tool"] == node.tool
    assert os.path.exists(f"{prefix}/installed") and os.path.isdir(f"{prefix}/conda-meta")
    from cubevis.setup import discover as D
    assert D.setup_marker(prefix)["name"] == "cubevis-1.0.97"         # what discovery reads
    r = node.call("plan", dirs=[node.envs_dir], names=E.candidate_names("1.0.97"))
    assert r["dirs"][0]["taken"] == ["cubevis-1.0.97"] and r["jobs"]["cubevis-1.0.97"]["state"] == "ok"


def test_the_worker_is_detached_from_the_connection(node):
    """The call returns at once, with the worker still running: nothing
    holds the connection's pipes (ssh would wait for them)."""
    t0 = time.time()
    r = node.start(install_shell="sleep 2")
    assert time.time() - t0 < 1.5, "the call waited for the worker"
    assert r["started"] and r["status"]["state"] in ("starting", "running")
    assert node.wait(states=("running",))["state"] == "running"
    assert node.wait()["state"] == "ok"


def test_a_second_start_while_running_does_not_start_another(node):
    node.start(install_shell="sleep 2")
    node.wait(states=("running",))
    r = node.start(install_shell="true")
    assert r["ok"] and r["started"] is False and r["status"]["state"] == "running"
    assert node.wait()["state"] == "ok"
    log = open(f"{node.config_dir}/make/cubevis-1.0.97.log").read()
    assert log.count("$ ") == 2                                  # one create, one install


def test_a_failed_create_leaves_a_partial_prefix_and_the_next_try_clears_it(node):
    node.start(env={"FAKE_CONDA_FAIL": "create"}, install_shell="true")
    st = node.wait()
    assert st["state"] == "failed" and "create failed" in st["error"] and st["done"] == []
    assert os.path.isdir(f"{node.envs_dir}/cubevis-1.0.97")             # the fake's leftover
    assert any("solver failed" in l for l in st["log_tail"])
    r = node.start(install_shell="true")                                # resumes: removes, makes again
    assert r["ok"] and r["started"]
    st = node.wait()
    assert st["state"] == "ok", st
    assert os.path.exists(f"{node.envs_dir}/cubevis-1.0.97/{E.MARKER_FILE}")


def test_a_failed_install_resumes_after_the_create_step(node):
    node.start(install_shell="exit 3")
    st = node.wait()
    assert st["state"] == "failed" and st["done"] == ["create"]
    node.start(install_shell="true")
    assert node.wait()["state"] == "ok"
    log = open(f"{node.config_dir}/make/cubevis-1.0.97.log").read()
    assert log.count("create -y -p") == 1                                # not made again
    assert log.count("$ exit 3") == 1 and log.count("$ true") == 1


def test_what_is_not_setups_is_never_touched(node):
    mine = node.tmp / "envs" / "cubevis-1.0.97"
    mine.mkdir()
    (mine / "precious").write_text("x")
    r = node.start(install_shell="true")
    assert not r["ok"] and "exists already" in r["error"]
    assert (mine / "precious").exists()
    r = node.call("remove", prefix=str(mine), tool=node.tool)
    assert not r["ok"] and "not made by cubevis setup" in r["error"]
    assert (mine / "precious").exists()
    (mine / E.MARKER_FILE).write_text(json.dumps({"schema": 1, "name": "other-name"}))
    r = node.call("remove", prefix=str(mine), tool=node.tool)
    assert not r["ok"] and "not named as its marker says" in r["error"]
    assert (mine / "precious").exists()


def test_bad_requests_are_refused(node):
    assert "absolute path" in node.start(prefix="relative/cubevis-1.0.97")["error"]
    assert "ending in the name" in node.start(prefix=f"{node.envs_dir}/other")["error"]
    assert "not an executable" in node.start(tool="/nonexistent/conda")["error"]
    if os.geteuid() != 0:
        assert "cannot write" in node.start(name="cubevis-9", prefix="/proc/x/cubevis-9")["error"]


def test_a_worker_that_is_gone_is_said_to_have_died(node):
    os.makedirs(f"{node.config_dir}/make")
    with open(f"{node.config_dir}/make/cubevis-1.0.97.json", "w") as f:
        json.dump({"state": "running", "step": "installing", "pid": 2 ** 22 + 12345,
                   "prefix": "/x"}, f)
    st = node.call("status", name="cubevis-1.0.97")["status"]
    assert st["state"] == "died" and "gone" in st["error"]
    assert node.call("status", name="cubevis-nothing")["status"] is None


def test_remove_uses_the_tools_own_remove_and_clears_the_job(node):
    node.start(install_shell="true")
    assert node.wait()["state"] == "ok"
    prefix = f"{node.envs_dir}/cubevis-1.0.97"
    r = node.call("remove", prefix=prefix, tool=node.tool, env={"FAKE_CONDA_FAIL": "remove"})
    assert not r["ok"] and not r["removed"] and "still there" in r["error"]
    assert any("cannot remove" in l for l in r["output"])
    assert os.path.exists(prefix)                                        # never rm'd by setup
    r = node.call("remove", prefix=prefix, tool=node.tool)
    assert r["ok"] and r["removed"] and not os.path.exists(prefix)
    assert node.call("status", name="cubevis-1.0.97")["status"] is None
