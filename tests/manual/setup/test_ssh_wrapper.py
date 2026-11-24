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


# A stand-in for ssh: answers -V and -G, says the shared connection is up
# once a login has run, and records every other call with its DISPLAY.
_FAKE_SSH = """#!/bin/sh
D="$FAKE_SSH_DIR"
for a in "$@"; do
  case "$a" in
    -V) echo "OpenSSH_${FAKE_SSH_VERSION}, fake" >&2; exit 0 ;;
    -G) printf 'hostname login.example.test\\nuser jo\\nport 22\\ncontrolpath %s/cm/s\\ncontrolpersist 60\\n' "$D"
        exit 0 ;;
    -O) [ -e "$D/cm/s" ]; exit $? ;;
  esac
done
echo "DISPLAY=${DISPLAY-<unset>} $*" >> "$D/calls"
mkdir -p "$D/cm" && touch "$D/cm/s"
exit 0
"""


def _login_call(tmp_path, version, display):
    """The wrapper's own login (the first recorded call), as one line."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "ssh"
    fake.write_text(_FAKE_SSH)
    fake.chmod(0o755)
    env = _wrapper_env(tmp_path, "login.example.test backend=command command=echo pw\n")
    env.update(PATH=f"{bindir}:{env['PATH']}", FAKE_SSH_DIR=str(tmp_path),
               FAKE_SSH_VERSION=version, TMPDIR=str(tmp_path))
    env.pop("DISPLAY", None)
    if display is not None:
        env["DISPLAY"] = display
    r = subprocess.run([str(WRAPPER), "gw", "echo", "hi"], capture_output=True, text=True,
                       env=env, stdin=subprocess.DEVNULL, timeout=60)
    assert r.returncode == 0, r.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert len(calls) == 2, calls                      # the login, then the command itself
    assert "BatchMode=no" in calls[0] and calls[0].endswith(" gw true"), calls[0]
    assert calls[1].endswith(" gw echo hi"), calls[1]
    return calls[0]


@pytest.mark.skipif(not shutil.which("perl"), reason="needs perl")
def test_login_leaves_forwarding_to_the_config(tmp_path):
    """The master's options are what every session sharing it can get: the
    wrapper's login must not turn X11 (or the agent) off (2026-11-23: -x
    left every later session without DISPLAY), and on OpenSSH 8.4+ must not
    give the master a made-up DISPLAY."""
    login = _login_call(tmp_path, "10.3p1", None)
    words = login.split()
    assert "-x" not in words and "-a" not in words, login
    assert login.startswith("DISPLAY=<unset> "), login
    # A real DISPLAY is passed on untouched.
    other = tmp_path / "real"
    other.mkdir()
    assert _login_call(other, "10.3p1", "/tmp/xq:0").startswith("DISPLAY=/tmp/xq:0 ")


@pytest.mark.skipif(not shutil.which("perl"), reason="needs perl")
def test_login_on_old_ssh_still_gets_a_display(tmp_path):
    """Before OpenSSH 8.4 askpass is used only with DISPLAY set."""
    assert _login_call(tmp_path, "8.0p1", None).startswith("DISPLAY=:0 ")
