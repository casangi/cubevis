"""Tests of the ssh wrapper (cubevis/setup/data/ssh) that need no network
and no remote host: it compiles, its --explain report, and passing through
without rules.

    pytest tests/manual/setup/test_ssh_wrapper.py

(The bash setup these tests once covered too was removed before release;
``python -m cubevis.setup`` replaced it.)
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
# The wrapper is package data since 2026-10-18.
WRAPPER = ROOT / "cubevis" / "setup" / "data" / "ssh"


def test_wrapper_compiles():
    if not shutil.which("perl"):
        pytest.skip("no perl")
    r = subprocess.run(["perl", "-c", str(WRAPPER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _wrapper_env(tmp_path, rules):
    rf = tmp_path / "rules"
    rf.write_text(rules)
    return dict(os.environ, HOME=str(tmp_path), SSH_WRAPPER_RULES=str(rf))


@pytest.mark.skipif(not shutil.which("ssh") or not shutil.which("perl"), reason="needs ssh and perl")
def test_explain_reports_rule_and_near_misses(tmp_path):
    cfg = tmp_path / "ssh_config"
    cfg.write_text("Host gw\n    HostName login.example.test\n    User jo\n"
                   "Host dh\n    HostName data.example.test\n    User jo\n    ProxyJump gw\n"
                   "    ControlPath none\n")
    env = _wrapper_env(tmp_path, "jo,login backend=command command=echo pw\n"
                                 "data backend=command command=echo pw\n"
                                 "login.example.test backend=command command=echo pw\n")
    r = subprocess.run([str(WRAPPER), "--explain", "-F", str(cfg), "dh"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "hop 1: gw -> login.example.test" in out
    assert "hop 2: dh -> data.example.test" in out
    assert "password: stored" in out                         # the gw rule
    assert "rule 'data'" in out and "not the whole host name" in out
    # The old "user,host" form is pointed out for the hop it was meant for.
    env = _wrapper_env(tmp_path, "jo,login backend=command command=echo pw\n")
    r = subprocess.run([str(WRAPPER), "--explain", "-F", str(cfg), "gw"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert "starts with your user name" in r.stdout


@pytest.mark.skipif(not shutil.which("ssh") or not shutil.which("perl"), reason="needs ssh and perl")
def test_wrapper_passes_through_without_rules(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "none"))
    env.pop("SSH_WRAPPER_RULES", None)
    r = subprocess.run([str(WRAPPER), "-V"], capture_output=True, text=True, env=env)
    assert "OpenSSH" in r.stderr
