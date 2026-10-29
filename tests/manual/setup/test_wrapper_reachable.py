"""The wrapper's ``--reachable HOST [PORT]`` (2026-10-29): for a ``Match
exec`` line in ~/.ssh/config, so ssh goes straight to a node when this
computer is on the site's network and through the login host when not.

The answer is remembered per host:port and per local source address (the
address this computer would send from to reach the host), so a change of
network is never missed and a known network costs nothing.

    pytest tests/manual/setup/test_wrapper_reachable.py

No ssh test rig needed: a listening socket on 127.0.0.1 is "inside", a
refused port, 192.0.2.1 (TEST-NET-1) and an unresolvable name are not.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time

import pytest

from cubevis.setup import data_file

WRAPPER = str(data_file("ssh"))

needs_perl = pytest.mark.skipif(not shutil.which("perl"), reason="needs perl")
needs_ssh = pytest.mark.skipif(not shutil.which("ssh"), reason="needs ssh")
pytestmark = needs_perl


@pytest.fixture
def listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    yield s.getsockname()[1]
    s.close()


@pytest.fixture
def refused():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                          # nothing listens there now
    return port


@pytest.fixture
def env(tmp_path):
    e = dict(os.environ, XDG_CACHE_HOME=str(tmp_path / "cache"))
    for k in ("SSH_WRAPPER_ROUTE", "SSH_WRAPPER_DEBUG", "SSH_WRAPPER_REACHABLE_TTL"):
        e.pop(k, None)
    return e


def cache_file(env):
    return os.path.join(env["XDG_CACHE_HOME"], "ssh-wrapper", "reachable")


def reach(env, *args):
    return subprocess.run([WRAPPER, "--reachable", "-v", *args], capture_output=True,
                          text=True, env=env, timeout=30, stdin=subprocess.DEVNULL)


def test_answering_host_is_direct_and_remembered(env, listener):
    r = reach(env, "127.0.0.1", str(listener))
    assert r.returncode == 0, r.stderr
    assert "now remembered" in r.stderr
    r = reach(env, "127.0.0.1", str(listener))
    assert r.returncode == 0
    assert "remembered for this network" in r.stderr
    line, = open(cache_file(env)).read().splitlines()
    assert line.startswith(f"127.0.0.1:{listener} 127.0.0.1 direct ")


def test_ports_are_remembered_separately(env, listener, refused):
    assert reach(env, "127.0.0.1", str(listener)).returncode == 0
    r = reach(env, "127.0.0.1", str(refused))
    assert r.returncode == 1, r.stderr
    assert "jump" in r.stderr and "now remembered" in r.stderr


def test_unresolvable_name_is_not_reachable_and_not_remembered(env):
    r = reach(env, "no-such-host.invalid")
    assert r.returncode == 1
    assert "does not resolve" in r.stderr
    assert not os.path.exists(cache_file(env))


def test_unanswering_address_is_jump(env):
    t = time.time()
    r = reach(env, "192.0.2.1", "22")
    assert r.returncode == 1, r.stderr
    assert time.time() - t < 10        # the timeout is 1s (plus perl's start)


def test_another_networks_answer_is_not_used(env, listener):
    """A remembered answer counts only for the same local source address:
    a different one means a different network (home, VPN, ...)."""
    os.makedirs(os.path.dirname(cache_file(env)))
    now = int(time.time())
    with open(cache_file(env), "w") as f:
        f.write(f"127.0.0.1:{listener} 203.0.113.9 jump {now}\n")
    r = reach(env, "127.0.0.1", str(listener))
    assert r.returncode == 0, r.stderr
    assert "now remembered" in r.stderr
    text = open(cache_file(env)).read()
    assert "203.0.113.9 jump" in text and "127.0.0.1 direct" in text


def test_this_networks_answer_is_trusted_until_it_expires(env, listener):
    os.makedirs(os.path.dirname(cache_file(env)))
    now = int(time.time())
    with open(cache_file(env), "w") as f:
        f.write(f"127.0.0.1:{listener} 127.0.0.1 jump {now}\n")
    r = reach(env, "127.0.0.1", str(listener))
    assert r.returncode == 1 and "remembered" in r.stderr     # not tested again
    with open(cache_file(env), "w") as f:
        f.write(f"127.0.0.1:{listener} 127.0.0.1 jump {now - 100}\n")
    r = reach(dict(env, SSH_WRAPPER_REACHABLE_TTL="50"), "127.0.0.1", str(listener))
    assert r.returncode == 0 and "now remembered" in r.stderr  # expired: tested


def test_route_override(env, listener):
    r = reach(dict(env, SSH_WRAPPER_ROUTE="jump"), "127.0.0.1", str(listener))
    assert r.returncode == 1 and "SSH_WRAPPER_ROUTE" in r.stderr
    r = reach(dict(env, SSH_WRAPPER_ROUTE="direct"), "no-such-host.invalid")
    assert r.returncode == 0
    assert not os.path.exists(cache_file(env))


def test_forget(env, listener):
    reach(env, "127.0.0.1", str(listener))
    assert os.path.exists(cache_file(env))
    r = subprocess.run([WRAPPER, "--reachable", "--forget"], capture_output=True, text=True,
                       env=env)
    assert r.returncode == 0
    assert not os.path.exists(cache_file(env))


def test_usage(env):
    r = subprocess.run([WRAPPER, "--reachable"], capture_output=True, text=True, env=env)
    assert r.returncode == 2 and "usage" in r.stderr


@needs_ssh
@pytest.mark.parametrize("which", ["listener", "refused"])
def test_ssh_match_exec_picks_the_route(env, tmp_path, listener, refused, which):
    """As in ~/.ssh/config: the ProxyJump applies only when not reachable.
    `ssh -G` evaluates Match exec, which is also how the wrapper finds its
    hops."""
    port = listener if which == "listener" else refused
    cfg = tmp_path / "config"
    cfg.write_text(f"""Host node
    HostName 127.0.0.1
    Port {port}
Match host 127.0.0.1 !exec "{WRAPPER} --reachable %h %p"
    ProxyJump gw.example.test
""")
    r = subprocess.run(["ssh", "-F", str(cfg), "-G", "node"], capture_output=True, text=True,
                       env=env, timeout=30, stdin=subprocess.DEVNULL)
    jump = [l for l in r.stdout.splitlines() if l.startswith("proxyjump ")]
    if which == "listener":
        assert not jump or jump == ["proxyjump none"], r.stdout
    else:
        assert jump == ["proxyjump gw.example.test"], r.stdout


@pytest.mark.skipif(os.geteuid() != 0 or not shutil.which("iptables"),
                    reason="needs root and iptables (a disposable container)")
def test_silent_address_times_out(env):
    """A firewall that drops (the off-site case): no answer within the
    timeout, so jump -- in about SSH_WRAPPER_REACHABLE_TIMEOUT seconds."""
    rule = ["OUTPUT", "-d", "127.0.0.2", "-p", "tcp", "--dport", "2999", "-j", "DROP"]
    if subprocess.run(["iptables", "-A", *rule], capture_output=True).returncode:
        pytest.skip("iptables refused")
    try:
        t = time.time()
        r = reach(env, "127.0.0.2", "2999")
        took = time.time() - t
    finally:
        subprocess.run(["iptables", "-D", *rule], capture_output=True)
    assert r.returncode == 1 and "within 1s" in r.stderr, r.stderr
    assert 0.9 < took < 5
