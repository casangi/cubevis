"""``python -m cubevis.setup`` (remote session design, stage 2a).

    pytest tests/manual/setup/test_cli.py
    CUBEVIS_SSH_RIG=1 pytest tests/manual/setup/test_cli.py   # + the rig

The rig: ``sudo devel/tools/ssh_rig.sh`` (a disposable container or VM only).

Without the rig: status, refusals, the old bash configuration being kept,
unattended runs never looping.  With ``CUBEVIS_SSH_RIG=1`` the interactive
run is driven through a real terminal (pexpect), as a user would answer it:
a network behind a password login host, its node with the same password,
the inside ranges; then ``check`` and ``status``; and a network reached
directly.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

from cubevis.setup import sshgen as G
from cubevis.setup import state as S

needs_rig = pytest.mark.skipif(os.environ.get("CUBEVIS_SSH_RIG") != "1",
                               reason="set CUBEVIS_SSH_RIG=1 with the ssh test rig running (devel/tools/ssh_rig.sh)")
needs_ssh = pytest.mark.skipif(not shutil.which("ssh") or not shutil.which("perl"),
                               reason="needs ssh and perl")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


@pytest.fixture
def home(monkeypatch):
    d = tempfile.mkdtemp(prefix="cvc", dir="/tmp")         # short: ControlPath sockets
    monkeypatch.setenv("XDG_CONFIG_HOME", d)
    monkeypatch.setenv("CUBEVIS_SETUP_KNOWN_HOSTS", f"{d}/known_hosts")   # not ~/.ssh
    yield d
    cfg = os.path.join(d, "cubevis", "ssh", "config")
    if os.path.exists(cfg):
        for a in ("rig-compute", "rig-login", "lan-compute"):
            subprocess.run(["ssh", "-F", cfg, "-O", "exit", a], capture_output=True)
    shutil.rmtree(d, ignore_errors=True)


def _env(home):
    env = dict(os.environ, XDG_CONFIG_HOME=home, NO_COLOR="1",
               PYTHONPATH=os.pathsep.join([ROOT] + [p for p in sys.path if p]))
    env.pop("SSH_WRAPPER_RULES", None)
    env.pop("SSH_WRAPPER_DEBUG", None)       # its log is in the real ~/.ssh
    return env


def _run(home, *args, input=None, timeout=180):
    return subprocess.run([sys.executable, "-m", "cubevis.setup", *args], capture_output=True,
                          text=True, env=_env(home), timeout=timeout,
                          input=input if input is not None else "")


def test_help_and_empty_status(home):
    r = _run(home, "--help")
    assert r.returncode == 0 and "network" in r.stdout and "check" in r.stdout
    r = _run(home, "status")
    assert r.returncode == 0 and "no networks yet" in r.stdout
    assert not os.path.exists(os.path.join(home, "cubevis", "state.json"))


def test_import_is_light():
    """The ssh "inside" test runs at every connection: no visplot, no bokeh."""
    code = ("import sys, cubevis.setup.inside, cubevis.setup.__main__; "
            "print(any(m.startswith(('bokeh', 'cubevis.toolbox', 'dask')) for m in sys.modules))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=dict(os.environ, PYTHONPATH=ROOT))
    assert r.stdout.strip() == "False", r.stderr


def test_unknown_network_and_bad_state(home):
    r = _run(home, "node", "nope", "x1")
    assert r.returncode == 1 and "no network 'nope'" in r.stdout
    os.makedirs(os.path.join(home, "cubevis"), exist_ok=True)
    with open(os.path.join(home, "cubevis", "state.json"), "w") as f:
        f.write("{broken")
    r = _run(home, "status")
    assert r.returncode == 1 and "cannot be read" in r.stdout


@needs_ssh
def test_unattended_failure_skips_and_bash_config_is_kept(home):
    """--yes on a node that cannot be reached: the step fails once and is
    skipped (no retry loop); the old bash setup's ssh/config is copied to
    backups/ before being replaced; the node is kept, flagged."""
    sshd = os.path.join(home, "cubevis", "ssh")
    os.makedirs(sshd)
    with open(os.path.join(sshd, "config"), "w") as f:
        f.write("# Written by cubevis-setup from ~/.config/cubevis/hosts/*.conf\nHost old\n")
    r = _run(home, "--yes", "--answer", "via_login=n", "--answer", "user=jo",
             "--answer", "domain=", "--answer", "network=lan",
             "--answer", "nodes=nosuch.example.invalid", "network", timeout=120)
    out = " ".join(r.stdout.split())             # messages are wrapped
    assert "older bash setup" in out
    backups = os.listdir(os.path.join(home, "cubevis", "backups"))
    assert any(b.startswith("config.") for b in backups)
    assert "FAIL" in out and "cannot find" in out
    assert r.returncode == 1
    st = S.load()
    assert st.nodes()["nosuch"]["status"] == "flagged"
    assert "lan-nosuch" in open(os.path.join(sshd, "config")).read()


def test_bad_names_are_refused(home):
    r = _run(home, "--yes", "--answer", "via_login=y", "--answer", "login_host=gw.example.org",
             "--answer", "login_user=j o", "network", timeout=60)
    assert r.returncode == 1 and "user name 'j o'" in r.stdout
    assert not S.load().networks()


# ---------------------------------------------------------------------- #
# The rig, through a real terminal                                         #
# ---------------------------------------------------------------------- #

def _spawn(home, *args):
    pexpect = pytest.importorskip("pexpect")
    return pexpect.spawn(sys.executable, ["-m", "cubevis.setup", *args], env=_env(home),
                         encoding="utf-8", timeout=90, dimensions=(50, 200))


@needs_rig
@needs_ssh
def test_rig_interactive_network(home):
    p = _spawn(home, "network")
    script = [
        (r"Go ahead\?", ""),                                         # the wrapper
        (r"through a login host\?", "y"),
        (r"Login host, its real name", "login.example.test:2201"),
        (r"Your user name on login.example.test", "alice"),
        (r"Your user name on the nodes", ""),                        # alice (default)
        (r"Domain of the nodes", ""),                                # example.test
        (r"A short name for this network", "rig"),
        (r"Nodes of rig", "compute:2202"),
        (r"Store your password for alice@login.example.test", "y"),
        (r"Which store\?", "command"),
        (r"Command", "echo 's3cret pw!'"),
        (r"Name for the stored entry", ""),
        (r"Press Enter when it is stored", ""),
        (r"Store your password for alice@compute.example.test", "y"),
        (r"same password as for login host rig-login\?", "y"),
        (r"Inside ranges", "127.0.0.0/8"),
    ]
    for pat, ans in script:
        p.expect(pat)
        p.sendline(ans)
    p.expect(r"Changed in this run")
    p.expect(pexpect_eof())
    out = p.before
    p.close()
    assert p.exitstatus == 0, out
    st = S.load()
    net = st.networks()["rig"]
    assert net["login"]["password"] and net["password"] and net["inside"] == ["127.0.0.0/8"]
    assert st.nodes()["compute"]["status"] == "ok"
    r = _run(home, "check")
    assert r.returncode == 0, r.stdout
    assert "all checks passed" in r.stdout
    r = _run(home, "status")
    assert "this computer is inside: direct" in r.stdout
    assert "node compute (default): compute.example.test" in r.stdout


def pexpect_eof():
    import pexpect
    return pexpect.EOF


@needs_rig
@needs_ssh
def test_rig_direct_network(home):
    """A network without a login host: the node is reached directly."""
    r = _run(home, "--yes", "--answer", "via_login=n", "--answer", "user=alice",
             "--answer", "domain=example.test", "--answer", "network=lan",
             "--answer", "nodes=compute:2202", "--answer", "store=command",
             "--answer", "store_command=echo 's3cret pw!'", "network")
    assert r.returncode == 0, r.stdout
    assert "no login host: always direct" in r.stdout
    text = open(G.config_path()).read()
    assert "ProxyJump" not in text and "Host lan-compute" in text
