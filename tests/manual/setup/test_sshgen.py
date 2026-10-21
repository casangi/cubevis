"""Tests for cubevis.setup.sshgen (remote session design, stage 1).

    pytest tests/manual/setup/test_sshgen.py
    CUBEVIS_SSH_RIG=1 pytest tests/manual/setup/test_sshgen.py   # + the rig

The generated ssh/config is read back with ``ssh -F <file> -G <alias>``
(what ssh itself makes of it) and the rules with the wrapper's
``--explain``; both log in nowhere.  With ``CUBEVIS_SSH_RIG=1`` a two-hop
password login goes through the ssh test rig (login.example.test:2201 and
compute.example.test:2202, user alice): ``sudo devel/tools/ssh_rig.sh``
in a disposable container or VM.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess

import pytest

from cubevis.setup import data_file
from cubevis.setup import sshgen as G
from cubevis.setup import state as S

needs_ssh = pytest.mark.skipif(not shutil.which("ssh") or not shutil.which("perl"),
                               reason="needs ssh and perl")


@pytest.fixture(autouse=True)
def xdg(monkeypatch):
    """A short directory for the sockets: pytest's own temporary paths are
    too long for an ssh ControlPath socket (104 bytes on macOS, 108 on
    Linux) -- the very thing warnings_for() warns about."""
    import tempfile
    d = tempfile.mkdtemp(prefix="cvx", dir="/tmp")
    monkeypatch.setenv("XDG_CONFIG_HOME", d)
    monkeypatch.setenv("CUBEVIS_HOST_ID", "gui1")
    monkeypatch.setenv("CUBEVIS_RUNTIME_DIR", f"{d}/run")
    monkeypatch.setenv("CUBEVIS_SETUP_KNOWN_HOSTS", f"{d}/known_hosts")   # not ~/.ssh
    yield S.Path(d)
    shutil.rmtree(d, ignore_errors=True)


def _state(store="keychain", **local):
    st = S.load()
    st.local.update(store=store, **local)
    st.add_network("cv", login_host="login.example.org", user="jo", login_user="jlogin",
                   login_port=2201, login_password=True, password=True,
                   domain="cv.example.org", login_service="cv-login-pw", service="cv-pw")
    st.add_node("node140", network="cv")
    st.add_node("node141", network="cv", port=2222)
    st.add_network("keyonly", login_host="gw.other.org", user="jo")
    st.add_node("k1", network="keyonly", hostname="k1.inside.other.org")
    return st


def _G(alias, cfg):
    r = subprocess.run(["ssh", "-F", str(cfg), "-G", alias], capture_output=True,
                       text=True, check=True)
    out = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition(" ")
        out.setdefault(k, v)
    return out


@pytest.mark.parametrize("store,expect", [
    ("keychain", "service=s account=a backend=security"),
    ("secret-tool", "service=s account=a backend=secret-tool"),
    ("pass", "service=s account=a backend=pass pass_name=s/a"),
])
def test_rule_keys(store, expect):
    assert G.rule_keys({"store": store}, "s", "a") == expect


def test_rule_keys_gpg_and_command(xdg):
    k = G.rule_keys({"store": "gpg-file"}, "s", "a")
    assert k == f"service=s account=a backend=command command=gpg -q -d {xdg}/cubevis/secrets/s-a.gpg"
    k = G.rule_keys({"store": "command", "store_command": "op read op://v/@SERVICE@/@ACCOUNT@"},
                    "s", "a")
    assert k.endswith("command=op read op://v/s/a")
    with pytest.raises(S.StateError, match="store_command"):
        G.rule_keys({"store": "command"}, "s", "a")
    with pytest.raises(S.StateError, match="no password store"):
        G.rule_keys({}, "s", "a")


def test_rules_text_order_and_content():
    text = G.rules_text(_state())
    rules = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert rules == [
        "login.example.org  service=cv-login-pw account=jlogin backend=security",
        "*.cv.example.org  service=cv-pw account=jo backend=security",
    ]


def test_rules_without_pattern_list_the_nodes():
    st = S.load()
    st.local["store"] = "pass"
    st.add_network("n", login_host="gw.x.org", user="u", password=True)
    st.add_node("a", network="n", hostname="a.x.org")
    st.add_node("b", network="n", hostname="b.x.org")
    rules = [l for l in G.rules_text(st).splitlines() if l and not l.startswith("#")]
    assert rules == ["a.x.org,b.x.org  service=cubevis-ssh account=u backend=pass "
                     "pass_name=cubevis-ssh/u"]


@needs_ssh
def test_config_as_ssh_reads_it(xdg):
    st = _state()
    res = G.write(st)
    assert res["config"] and res["rules"]
    cfg = G.config_path()
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    login = _G("cv-login", cfg)
    assert (login["hostname"], login["user"], login["port"]) == ("login.example.org", "jlogin", "2201")
    assert login["controlmaster"] == "auto" and login["controlpersist"] == "28800"
    assert login["controlpath"].startswith(str(xdg / "run" / "cm"))
    assert str(cfg) == str(xdg / "cubevis" / "hosts" / "gui1" / "ssh" / "config")
    assert login.get("proxyjump", "none") == "none"
    n = _G("cv-node140", cfg)
    assert (n["hostname"], n["user"], n["port"], n["proxyjump"]) == \
        ("node140.cv.example.org", "jo", "22", "cv-login")
    assert n["serveraliveinterval"] == "30" and n["serveralivecountmax"] == "6"
    assert _G("cv-node141", cfg)["port"] == "2222"
    k1 = _G("keyonly-k1", cfg)
    assert (k1["hostname"], k1["proxyjump"]) == ("k1.inside.other.org", "keyonly-login")
    assert stat.S_IMODE((xdg / "run" / "cm").stat().st_mode) == 0o700


def test_write_only_when_changed():
    st = _state()
    G.write(st)
    m1 = G.config_path().stat().st_mtime_ns
    assert G.write(st) == {"config": False, "rules": False, "warnings": []}
    assert G.config_path().stat().st_mtime_ns == m1
    st.add_node("node150", network="cv")
    res = G.write(st)
    assert res["config"] and not res["rules"]      # the pattern covers the new node


def test_long_socket_dir_warns_long_config_dir_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ("d" * 80)))
    assert G.write(_state())["warnings"] == []           # the sockets are not there
    monkeypatch.setenv("CUBEVIS_RUNTIME_DIR", str(tmp_path / ("r" * 80)))
    assert "socket path" in G.write(_state())["warnings"][0]


def test_install_wrapper(xdg):
    assert G.install_wrapper()
    w = G.wrapper_path()
    assert w.read_bytes() == data_file("ssh").read_bytes()
    assert os.access(w, os.X_OK)
    assert os.readlink(w.with_name("scp")) == "ssh"
    assert not G.install_wrapper()
    w.write_text("old")
    assert G.install_wrapper() and w.read_bytes() == data_file("ssh").read_bytes()


@needs_ssh
def test_wrapper_explain_reads_generated_files(xdg):
    """The installed wrapper finds the generated rules by itself (beside its
    bin/ directory) and matches the right hops: the node through the login
    host, both password hosts, the key-only network neither."""
    st = _state(store="command", store_command="echo pw-@SERVICE@-@ACCOUNT@")
    G.write(st)
    G.install_wrapper()
    env = dict(os.environ)
    env.pop("SSH_WRAPPER_RULES", None)
    r = subprocess.run([str(G.wrapper_path()), "--explain", "-F", str(G.config_path()),
                        "cv-node140"], capture_output=True, text=True, env=env, timeout=60)
    out = r.stdout
    assert str(G.rules_path()) in out.splitlines()[0]
    assert "hop 1: cv-login -> login.example.org (user jlogin, port 2201)" in out
    assert "hop 2: cv-node140 -> node140.cv.example.org (user jo, port 22)" in out
    assert out.count("password: stored") == 2
    assert "rule: login.example.org" in out and "rule: *.cv.example.org" in out
    r = subprocess.run([str(G.wrapper_path()), "--explain", "-F", str(G.config_path()),
                        "keyonly-k1"], capture_output=True, text=True, env=env, timeout=60)
    assert r.stdout.count("rule: none") == 2


# ---------------------------------------------------------------------- #
# The rig: a real two-hop password login through the generated files       #
# ---------------------------------------------------------------------- #

@pytest.mark.skipif(os.environ.get("CUBEVIS_SSH_RIG") != "1",
                    reason="set CUBEVIS_SSH_RIG=1 with the ssh test rig running (devel/tools/ssh_rig.sh)")
@needs_ssh
def test_rig_two_hop_password_login(xdg):
    st = S.load()
    st.local.update(store="command", store_command="echo 's3cret pw!'")
    st.add_network("rig", login_host="login.example.test", user="alice", login_port=2201,
                   login_password=True, password=True, domain="example.test",
                   node_pattern="compute.example.test")
    st.add_node("compute", network="rig", port=2202)
    G.write(st)
    G.install_wrapper()
    env = dict(os.environ)
    env.pop("SSH_WRAPPER_RULES", None)
    cmd = [str(G.wrapper_path()), "-F", str(G.config_path()), "-o", "BatchMode=yes",
           "-o", "StrictHostKeyChecking=accept-new", "rig-compute", "echo", "hello-from-node"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120,
                           stdin=subprocess.DEVNULL)
        assert r.returncode == 0, r.stderr
        assert "hello-from-node" in r.stdout
        # Both hops now have shared connections.
        for alias in ("rig-login", "rig-compute"):
            c = subprocess.run(["ssh", "-F", str(G.config_path()), "-O", "check", alias],
                               capture_output=True, text=True)
            assert c.returncode == 0, (alias, c.stderr)
    finally:
        for alias in ("rig-compute", "rig-login"):
            subprocess.run(["ssh", "-F", str(G.config_path()), "-O", "exit", alias],
                           capture_output=True)


# ---------------------------------------------------------------------- #
# Networks without a login host; inside / outside (2026-10-19)            #
# ---------------------------------------------------------------------- #

def test_network_without_login_host(xdg):
    st = S.load()
    st.local["store"] = "keychain"
    st.add_network("lan", login_host=None, user="jo", password=True, domain="lan.example.org")
    st.add_node("ws1", network="lan")
    text = G.config_text(st)
    assert "lan-login" not in text and "ProxyJump" not in text
    assert "(nodes reached directly)" in text
    rules = [l for l in G.rules_text(st).splitlines() if l and not l.startswith("#")]
    assert rules == ["*.lan.example.org  service=cubevis-ssh account=jo backend=security"]


def test_inside_ranges_put_proxyjump_in_a_match_block(xdg):
    st = _state()
    st.networks()["cv"]["inside"] = ["10.7.0.0/16", "10.2.0.0/16"]
    st.local["python"] = "/opt/my env/bin/python"
    text = G.config_text(st)
    cv = text[text.index("# network cv"):text.index("# network keyonly")]
    assert "ProxyJump" in cv
    head, match = cv.split("Match ", 1)
    assert "ProxyJump" not in head                 # not in the Host blocks
    assert match.startswith("originalhost cv-node140,cv-node141 !exec "
                            "\"'/opt/my env/bin/python' -m cubevis.setup.inside cv\"")
    assert "    ProxyJump cv-login" in match
    # keyonly has no ranges: always through its login host, as before.
    assert "ProxyJump keyonly-login" in text[text.index("# network keyonly"):]


def _rig_state(ranges):
    st = S.load()
    st.local.update(store="command", store_command="echo 's3cret pw!'")
    st.add_network("rig", login_host="login.example.test", user="alice", login_port=2201,
                   login_password=True, password=True, node_pattern="compute.example.test",
                   inside=ranges)
    st.add_node("compute", network="rig", hostname="compute.example.test", port=2202)
    S.save(st)                    # the inside test reads state.json
    G.write(st)
    G.install_wrapper()
    return st


@needs_ssh
@pytest.mark.skipif(os.environ.get("CUBEVIS_SSH_RIG") != "1",
                    reason="set CUBEVIS_SSH_RIG=1 with the ssh test rig running (devel/tools/ssh_rig.sh)")
@pytest.mark.parametrize("ranges,hops", [(["127.0.0.0/8"], 1), (["10.99.0.0/16"], 2), ([], 2)])
def test_rig_inside_goes_direct(xdg, ranges, hops):
    """The rig's node resolves to 127.0.0.1: with 127.0.0.0/8 as the inside
    range this computer is inside and ssh goes to the node directly (one
    hop); with another range, or none, through the login host (two)."""
    _rig_state(ranges)
    env = dict(os.environ)
    env.pop("SSH_WRAPPER_RULES", None)
    import sys
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)   # the Match exec's python
    cfg = str(G.config_path())
    ex = subprocess.run([str(G.wrapper_path()), "--explain", "-F", cfg, "rig-compute"],
                        capture_output=True, text=True, env=env, timeout=60,
                        stdin=subprocess.DEVNULL)
    assert ex.stdout.count("\nhop ") == hops, ex.stdout
    try:
        r = subprocess.run([str(G.wrapper_path()), "-F", cfg, "-o", "BatchMode=yes",
                            "-o", "StrictHostKeyChecking=accept-new", "rig-compute",
                            "echo", "reached"], capture_output=True, text=True, env=env,
                           timeout=120, stdin=subprocess.DEVNULL)
        assert r.returncode == 0, r.stderr
        assert "reached" in r.stdout
    finally:
        for a in ("rig-compute", "rig-login"):
            subprocess.run(["ssh", "-F", cfg, "-O", "exit", a], capture_output=True, env=env)
