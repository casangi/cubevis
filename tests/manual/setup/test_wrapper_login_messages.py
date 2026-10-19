"""The wrapper's message when its own login fails (stage 1 fix, 2026-10-18).

It used to print a hint about gpg-agent whatever the cause -- for example
when the host name did not resolve.  Now it shows the last line ssh wrote,
and says why from what its askpass call reported: ssh never asked (name,
network, host key), the lookup failed, nothing was stored (the gpg hint
only here, and only for a gpg-backed store), or the stored password was
refused.

    pytest tests/manual/setup/test_wrapper_login_messages.py
    CUBEVIS_SSH_RIG=1 pytest ...    # the password cases, on the ssh test rig
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

from cubevis.setup import data_file

needs_ssh = pytest.mark.skipif(not shutil.which("ssh") or not shutil.which("perl"),
                               reason="needs ssh and perl")
needs_rig = pytest.mark.skipif(os.environ.get("CUBEVIS_SSH_RIG") != "1",
                               reason="set CUBEVIS_SSH_RIG=1 with the ssh test rig running")


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
