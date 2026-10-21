"""``python -m cubevis.setup`` (remote session design, stage 2).

    pytest tests/manual/setup/test_cli.py
    CUBEVIS_SSH_RIG=1 pytest tests/manual/setup/test_cli.py   # + the rig

The rig: ``sudo devel/tools/ssh_rig.sh`` (a disposable container or VM only).

Without the rig: status, refusals, the earlier layout's files being left
alone, unattended runs never looping.  With ``CUBEVIS_SSH_RIG=1`` the interactive
run is driven through a real terminal (pexpect), as a user would answer it:
a network behind a password login host, its node with the same password,
the inside ranges; then ``check`` and ``status``; a network reached
directly; and two computers sharing one configuration directory.

Each test plays the computer ``CUBEVIS_HOST_ID=gui1`` (``gui2`` too, where
two are needed), with its sockets in the test's own directory.
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
    monkeypatch.setenv("CUBEVIS_HOST_ID", "gui1")
    monkeypatch.setenv("CUBEVIS_RUNTIME_DIR", f"{d}/run")
    monkeypatch.setenv("CUBEVIS_SETUP_KNOWN_HOSTS", f"{d}/known_hosts")   # not ~/.ssh
    yield d
    for host in ("gui1", "gui2"):
        cfg = os.path.join(d, "cubevis", "hosts", host, "ssh", "config")
        if os.path.exists(cfg):
            env = dict(os.environ, CUBEVIS_RUNTIME_DIR=f"{d}/run" if host == "gui1"
                       else f"{d}/run-{host}")
            for a in ("rig-compute", "rig-login", "lan-compute"):
                subprocess.run(["ssh", "-F", cfg, "-O", "exit", a], capture_output=True, env=env)
    shutil.rmtree(d, ignore_errors=True)


def _env(home, **extra):
    env = dict(os.environ, XDG_CONFIG_HOME=home, NO_COLOR="1", **extra,
               PYTHONPATH=os.pathsep.join([ROOT] + [p for p in sys.path if p]))
    env.pop("SSH_WRAPPER_RULES", None)
    env.pop("SSH_WRAPPER_DEBUG", None)       # its log is in the real ~/.ssh
    return env


def _run(home, *args, input=None, timeout=180, host=None):
    # Another computer: its own name and its own local disk for sockets (on
    # one machine, it would otherwise ride on the first one's connections).
    extra = {"CUBEVIS_HOST_ID": host, "CUBEVIS_RUNTIME_DIR": f"{home}/run-{host}"} if host else {}
    return subprocess.run([sys.executable, "-m", "cubevis.setup", *args], capture_output=True,
                          text=True, env=_env(home, **extra), timeout=timeout,
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
def test_unattended_failure_skips_and_old_layout_is_left_alone(home):
    """--yes on a node that cannot be reached: the step fails once and is
    skipped (no retry loop); the node is kept, flagged.  The earlier
    layout's ssh/config (here the bash setup's) is mentioned, not touched:
    another computer sharing the directory may still use it."""
    sshd = os.path.join(home, "cubevis", "ssh")
    os.makedirs(sshd)
    old = "# Written by cubevis-setup from ~/.config/cubevis/hosts/*.conf\nHost old\n"
    with open(os.path.join(sshd, "config"), "w") as f:
        f.write(old)
    r = _run(home, "--yes", "--answer", "via_login=n", "--answer", "user=jo",
             "--answer", "domain=", "--answer", "network=lan",
             "--answer", "nodes=nosuch.example.invalid", "network", timeout=120)
    out = " ".join(r.stdout.split())             # messages are wrapped
    assert "not used any more" in out and "cubevis/ssh/config" in out
    assert open(os.path.join(sshd, "config")).read() == old
    assert "FAIL" in out and "cannot find" in out
    assert r.returncode == 1
    st = S.load()
    assert st.nodes()["nosuch"]["status"] == "flagged"
    assert "lan-nosuch" in open(G.config_path()).read()
    assert str(G.config_path()).endswith("cubevis/hosts/gui1/ssh/config")
    assert "Host lan-nosuch" not in open(os.path.join(sshd, "config")).read()


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


@needs_rig
@needs_ssh
def test_rig_two_computers_share_one_directory(home):
    """gui1 sets up a network; gui2, sharing the directory, sees it, chooses
    its own password store, and gets its own ssh files and wrapper; gui1's
    stay as they were.  Then each checks, and state.json holds one network."""
    answers = ["--answer", "store=command", "--answer", "store_command=echo 's3cret pw!'"]
    r = _run(home, "--yes", "--answer", "via_login=n", "--answer", "user=alice",
             "--answer", "domain=example.test", "--answer", "network=lan",
             "--answer", "nodes=compute:2202", *answers, "network")
    assert r.returncode == 0, r.stdout
    gui1 = os.path.join(home, "cubevis", "hosts", "gui1")
    cfg1 = open(os.path.join(gui1, "ssh", "config")).read()
    rules1 = open(os.path.join(gui1, "ssh", "rules")).read()

    r = _run(home, "status", host="gui2")
    assert "computer   gui2" in r.stdout and "network lan" in r.stdout
    r = _run(home, "--yes", *answers, "network", "lan", host="gui2")
    out = " ".join(r.stdout.split())
    assert r.returncode == 0, r.stdout
    assert "no password store chosen yet" in out
    gui2 = os.path.join(home, "cubevis", "hosts", "gui2")
    assert os.path.exists(os.path.join(gui2, "bin", "ssh"))
    assert "Host lan-compute" in open(os.path.join(gui2, "ssh", "config")).read()
    assert "backend=command" in open(os.path.join(gui2, "ssh", "rules")).read()
    assert open(os.path.join(gui1, "ssh", "config")).read() == cfg1
    assert open(os.path.join(gui1, "ssh", "rules")).read() == rules1

    for host in (None, "gui2"):                       # None: gui1, the fixture's
        r = _run(home, "check", host=host)
        assert r.returncode == 0 and "all checks passed" in r.stdout, (host, r.stdout)
    import json
    shared = json.load(open(os.path.join(home, "cubevis", "state.json")))
    assert list(shared["networks"]) == ["lan"] and "local" not in shared
    for host in ("gui1", "gui2"):
        loc = json.load(open(os.path.join(home, "cubevis", "hosts", host, "local.json")))
        assert loc["local"]["store"] == "command"


@needs_rig
@needs_ssh
def test_rig_socket_directory_cleaned_away(home):
    """/tmp emptied (a reboot, a tmp cleaner) while setup is not running: the
    wrapper makes the socket directory again, closed to others, at the next
    login that needs a password."""
    r = _run(home, "--yes", "--answer", "via_login=n", "--answer", "user=alice",
             "--answer", "domain=example.test", "--answer", "network=lan",
             "--answer", "nodes=compute:2202", "--answer", "store=command",
             "--answer", "store_command=echo 's3cret pw!'", "network")
    assert r.returncode == 0, r.stdout
    subprocess.run(["ssh", "-F", str(G.config_path()), "-O", "exit", "lan-compute"],
                   capture_output=True)
    shutil.rmtree(os.path.join(home, "run"))
    r = _run(home, "check")
    assert r.returncode == 0 and "all checks passed" in r.stdout, r.stdout
    import stat
    for d in ("run", "run/cm"):
        assert stat.S_IMODE(os.stat(os.path.join(home, d)).st_mode) == 0o700
