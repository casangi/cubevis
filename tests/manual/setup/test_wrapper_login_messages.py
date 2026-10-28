"""The wrapper's message when its own login fails (stage 1 fix, 2026-10-18).

It used to print a hint about gpg-agent whatever the cause -- for example
when the host name did not resolve.  Now it shows the last line ssh wrote,
and says why from what its askpass call reported: ssh never asked (name,
network, host key), the lookup failed, nothing was stored (the gpg hint
only here, and only for a gpg-backed store), or the stored password was
refused.

    pytest tests/manual/setup/test_wrapper_login_messages.py
    CUBEVIS_SSH_RIG=1 pytest ...    # the password cases, on the ssh test rig

The rig: ``sudo devel/tools/ssh_rig.sh`` (a disposable container or VM only).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

from cubevis.setup import data_file
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

needs_ssh = pytest.mark.skipif(not shutil.which("ssh") or not shutil.which("perl"),
                               reason="needs ssh and perl")
needs_rig = pytest.mark.skipif(os.environ.get("CUBEVIS_SSH_RIG") != "1",
                               reason="set CUBEVIS_SSH_RIG=1 with the ssh test rig running (devel/tools/ssh_rig.sh)")


@pytest.fixture
def d():
    p = tempfile.mkdtemp(prefix="cvw", dir="/tmp")     # short: a ControlPath lives here
    os.makedirs(f"{p}/cm")
    with open(f"{p}/config", "w") as f:
        f.write(f"""Host gw
    HostName login.example.test
    Port 2201
    User alice
    ControlMaster auto
    ControlPath {p}/cm/%C
    ControlPersist 60
    StrictHostKeyChecking accept-new
    UserKnownHostsFile {p}/known_hosts
Host nowhere
    HostName no-such-host.example.invalid
    User alice
    ControlPath {p}/cm/%C
""")
    yield p
    subprocess.run(["ssh", "-F", f"{p}/config", "-O", "exit", "gw"], capture_output=True)
    shutil.rmtree(p, ignore_errors=True)


def _run(d, rule, host):
    with open(f"{d}/rules", "w") as f:
        f.write(rule + "\n")
    env = dict(os.environ, SSH_WRAPPER_RULES=f"{d}/rules", TMPDIR=d)
    env.pop("SSH_WRAPPER_DEBUG", None)
    r = subprocess.run([str(data_file("ssh")), "-F", f"{d}/config", "-o", "BatchMode=yes",
                        host, "echo", "ran"], capture_output=True, text=True, env=env,
                       timeout=90, stdin=subprocess.DEVNULL)
    # The scratch directory is gone afterwards (exec skips END blocks).
    assert not [n for n in os.listdir(d) if n.startswith("ssh-wrapper-")]
    return r


@needs_ssh
def test_unresolvable_name_says_so_without_gpg_hint(d):
    r = _run(d, "no-such-host.example.invalid service=t account=alice backend=command "
                "command=gpg -q -d /x.gpg", "nowhere")
    assert r.returncode == 255
    assert "Could not resolve hostname" in r.stderr
    assert "failed before asking for a password" in r.stderr
    assert "gpg" not in r.stderr.replace("command=gpg", "")


@needs_ssh
def test_another_wrapper_copy_on_path_is_not_the_real_ssh(d, monkeypatch):
    """2026-10-19, macOS: the bash setup's older wrapper was on PATH and was
    taken for the real ssh -- one wrapper ran inside the other, logged in
    twice, and its old message came out instead of this one's."""
    old = os.path.join(ROOT, "setup", "bin", "ssh")
    copy = os.path.join(d, "oldbin", "ssh")
    os.makedirs(os.path.dirname(copy))
    with open(old if os.path.exists(old) else str(data_file("ssh"))) as f, open(copy, "w") as g:
        g.write(f.read())
    os.chmod(copy, 0o755)
    monkeypatch.setenv("PATH", os.path.dirname(copy) + os.pathsep + os.environ["PATH"])
    r = _run(d, "no-such-host.example.invalid service=t account=alice backend=command "
                "command=true", "nowhere")
    assert r.returncode == 255
    assert r.stderr.count("logging in to 'nowhere'") == 1, r.stderr
    assert "Could not resolve hostname" in r.stderr
    assert "failed before asking for a password" in r.stderr


@needs_ssh
@needs_rig
@pytest.mark.parametrize("command,expect,gpg", [
    ("echo nope", "the stored password was refused", False),
    ("true", "no stored password was found", False),
    ("gpg -q -d /nonexistent.gpg", "It is gpg-encrypted", True),
])
def test_password_cases(d, command, expect, gpg):
    r = _run(d, f"login.example.test service=t account=alice backend=command command={command}",
             "gw")
    assert r.returncode == 255
    assert "Permission denied" in r.stderr
    assert expect in r.stderr
    assert ("gpg-agent" in r.stderr) == gpg
    assert "ran" not in r.stdout


@needs_ssh
@needs_rig
def test_good_password_runs_the_command(d):
    r = _run(d, "login.example.test service=t account=alice backend=command "
                "command=echo 's3cret pw!'", "gw")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ran"


# --- Stale shared connections (2026-10-28, from Darrell's ~/bin copy) -------

GOOD = "login.example.test service=t account=alice backend=command command=echo 's3cret pw!'"


def _run_logged(d, **extra):
    """_run with the debug log on; returns (result, log text of this run)."""
    log = f"{d}/wrapper.log"
    open(log, "w").close()
    with open(f"{d}/rules", "w") as f:
        f.write(GOOD + "\n")
    env = dict(os.environ, SSH_WRAPPER_RULES=f"{d}/rules", TMPDIR=d,
               SSH_WRAPPER_DEBUG="1", SSH_WRAPPER_LOG=log, **extra)
    r = subprocess.run([str(data_file("ssh")), "-F", f"{d}/config", "-o", "BatchMode=yes",
                        "gw", "echo", "ran"], capture_output=True, text=True, env=env,
                       timeout=90, stdin=subprocess.DEVNULL)
    return r, open(log).read()


def _session_pids():
    """alice's sshd session processes on the rig (the remote end of a master)."""
    r = subprocess.run(["pgrep", "-u", "alice", "-f", "^sshd: alice"],
                       capture_output=True, text=True)
    return {int(p) for p in r.stdout.split()}


@needs_ssh
@needs_rig
def test_fresh_master_is_trusted_without_a_probe(d):
    r, _ = _run_logged(d)
    assert r.returncode == 0, r.stderr
    r, log = _run_logged(d)
    assert r.returncode == 0 and r.stdout.strip() == "ran", r.stderr
    assert "confirmed" in log and "probed" not in log, log
    assert "logging in" not in r.stderr


@needs_ssh
@needs_rig
def test_unconfirmed_master_is_probed_and_kept(d):
    _run_logged(d)
    before = _session_pids()
    r, log = _run_logged(d, SSH_WRAPPER_STALE_AFTER="0")
    assert r.returncode == 0 and r.stdout.strip() == "ran", r.stderr
    assert "probed" in log, log
    assert "stale" not in r.stderr
    assert _session_pids() >= before           # the same master, not a new login


@needs_ssh
@needs_rig
def test_stale_master_is_replaced(d):
    """The remote session frozen: the socket still answers -O check, nothing
    answers a command -- as after sleep or a dropped link."""
    r, _ = _run_logged(d)
    assert r.returncode == 0, r.stderr
    frozen = _session_pids()
    assert frozen
    for p in frozen:
        os.kill(p, 19)                          # SIGSTOP
    try:
        r, log = _run_logged(d, SSH_WRAPPER_STALE_AFTER="0", SSH_WRAPPER_PROBE_TIMEOUT="2")
        assert "is stale" in r.stderr, r.stderr
        assert "shared connection to 'gw' is up" in r.stderr, r.stderr
        assert r.returncode == 0 and r.stdout.strip() == "ran", r.stderr
        assert _session_pids() - frozen          # a new remote session
    finally:
        for p in frozen:
            try:
                os.kill(p, 18)                  # SIGCONT, then gone
                os.kill(p, 15)
            except ProcessLookupError:
                pass
