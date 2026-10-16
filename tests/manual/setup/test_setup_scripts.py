"""Tests for setup/ (cubevis-setup and the ssh wrapper) that need no network
and no remote host: syntax, the state-file helpers, quoting, and the
wrapper's --explain report.

    pytest tests/manual/setup/test_setup_scripts.py

Set CUBEVIS_TEST_BASH to another bash (for example a bash 3.2 build, the
one in macOS) to run the shell parts with it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SETUP = ROOT / "setup"
BASH = os.environ.get("CUBEVIS_TEST_BASH", shutil.which("bash") or "/bin/bash")
SHELL_FILES = [SETUP / "cubevis-setup"] + sorted((SETUP / "lib").glob("*.sh"))


def bash(script: str, home: Path, **env) -> subprocess.CompletedProcess:
    """Run SCRIPT with lib/common.sh loaded, HOME set to HOME."""
    e = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), **env)
    pre = f'. "{SETUP}/lib/common.sh"; . "{SETUP}/lib/versions.sh"\n'
    return subprocess.run([BASH, "-c", pre + script], capture_output=True, text=True,
                          env=e, stdin=subprocess.DEVNULL, timeout=60)


@pytest.mark.parametrize("path", SHELL_FILES, ids=lambda p: p.name)
def test_shell_syntax(path):
    r = subprocess.run([BASH, "-n", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_no_bash4_features():
    """macOS has bash 3.2: none of the bash 4 forms that break there."""
    bad = ["declare -A", "local -A", "mapfile", "readarray", ",,}", "^^}", "|&", ";&"]
    for path in SHELL_FILES:
        text = "\n".join(l for l in path.read_text().splitlines()
                         if not l.lstrip().startswith("#"))
        for b in bad:
            assert b not in text, f"{path.name} uses {b!r}"


def test_wrapper_compiles():
    if not shutil.which("perl"):
        pytest.skip("no perl")
    r = subprocess.run(["perl", "-c", str(SETUP / "bin" / "ssh")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_conf_roundtrip(tmp_path):
    r = bash('f="$CV_HOME/t.conf"; conf_set "$f" a "x = y"; conf_set "$f" b 2; '
             'conf_set "$f" a "new value"; echo "[$(conf_get "$f" a)][$(conf_get "$f" b)]'
             '[$(conf_get "$f" c dflt)]"', tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[new value][2][dflt]"


def test_sq_quotes_for_a_remote_shell(tmp_path):
    awkward = "it's a $HOME `x` \"q\" path"
    r = subprocess.run([BASH, "-c", f'. "{SETUP}/lib/common.sh"; v=$(sq "$1"); eval "printf %s $v"',
                        "_", awkward], capture_output=True, text=True,
                       env=dict(os.environ, HOME=str(tmp_path)))
    assert r.stdout == awkward, (r.stdout, r.stderr)


def test_record_change_and_backup(tmp_path):
    (tmp_path / "file").write_text("mine\n")
    r = bash(f'b=$(backup_file "{tmp_path}/file"); record_change "changed" "{tmp_path}/file"; '
             'cat "$b"; cat "$CV_CHANGES"', tmp_path)
    assert r.returncode == 0, r.stderr
    assert "mine" in r.stdout and f"changed: {tmp_path}/file" in r.stdout


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
    r = subprocess.run([str(SETUP / "bin" / "ssh"), "--explain", "-F", str(cfg), "dh"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "hop 1: gw -> login.example.test" in out
    assert "hop 2: dh -> data.example.test" in out
    assert "password: stored" in out                         # the gw rule
    assert "rule 'data'" in out and "not the whole host name" in out
    # The old "user,host" form is pointed out for the hop it was meant for.
    env = _wrapper_env(tmp_path, "jo,login backend=command command=echo pw\n")
    r = subprocess.run([str(SETUP / "bin" / "ssh"), "--explain", "-F", str(cfg), "gw"],
                       capture_output=True, text=True, env=env, timeout=60)
    assert "starts with your user name" in r.stdout


@pytest.mark.skipif(not shutil.which("ssh") or not shutil.which("perl"), reason="needs ssh and perl")
def test_wrapper_passes_through_without_rules(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "none"))
    env.pop("SSH_WRAPPER_RULES", None)
    r = subprocess.run([str(SETUP / "bin" / "ssh"), "-V"], capture_output=True, text=True, env=env)
    assert "OpenSSH" in r.stderr
