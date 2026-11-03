"""The user's ssh configuration as the reference (remote session design 7c,
stage 2c step 4): the login options the generated configuration copies
(``sshopts.login_options``), a ProxyJump adopted as the login host, real
names for the user's aliases, and the direct login that decides the
question about a login host.

    pytest tests/manual/setup/test_sshopts.py
    CUBEVIS_SSH_RIG=1 pytest ...      # also the rig's hosts (devel/tools/ssh_rig.sh)
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess

import pytest

from cubevis.setup import hostsetup as H
from cubevis.setup import sshgen, sshopts
from cubevis.setup import state as S
from cubevis.setup.steps import Ctx
from cubevis.setup.ui import UI

pytestmark = pytest.mark.skipif(not shutil.which("ssh"), reason="needs ssh")
RIG = os.environ.get("CUBEVIS_SSH_RIG") == "1"


def _has_gssapi() -> bool:
    """A client built without GSSAPI (this test kit's OpenSSH 8.0) neither
    reads nor prints its options: nothing to copy then."""
    r = subprocess.run(["ssh", "-F", "/dev/null", "-G", "x"], capture_output=True, text=True)
    return "gssapiauthentication" in r.stdout


GSSAPI = ["GSSAPIAuthentication"] if shutil.which("ssh") and _has_gssapi() else []

CONFIG = """\
Host gw
    HostName gw.example.org
    User jo
    Port 2222
Host ssh
    HostName ssh.cv.example.org
    User dschieb
    IdentityFile "~/.ssh/nrao key"
    IdentitiesOnly yes
Host cvpost*
    HostName %h.cv.example.org
    User dschieb
    ProxyJump ssh
    GSSAPIAuthentication yes
    PreferredAuthentications publickey,password
    ControlPath ~/.ssh/cm-%C
    ServerAliveInterval 5
Host far*
    HostName %h.far.example.org
    ProxyJump gw,ssh
Host tunnel
    HostName tunnel.example.net
    ProxyCommand nc -X connect -x proxy.example.net:3128 %h %p
    IdentityFile ~/.ssh/tunnel
"""


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    p = tmp_path / "ssh_config"
    p.write_text(CONFIG)
    monkeypatch.setenv("CUBEVIS_SETUP_USER_SSH_CONFIG", str(p))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CUBEVIS_HOST_ID", "gui1")
    monkeypatch.setenv("CUBEVIS_RUNTIME_DIR", str(tmp_path / "rt"))
    return p


def ctx(**answers):
    return Ctx(UI(yes=True, answers=answers, out=io.StringIO(), inp=io.StringIO()), S.load())


def parses(text: str, tmp_path, alias: str) -> dict:
    """``ssh -F TEXT -G ALIAS`` (route decision left out: no Match exec)."""
    f = tmp_path / "gen_config"
    f.write_text("\n".join(l for l in text.splitlines() if not l.startswith(("Match", "    ProxyJump cv")))
                 + "\n")
    r = subprocess.run(["ssh", "-F", str(f), "-G", alias], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition(" ")
        out.setdefault(k, []).append(v)
    return out


def test_login_options_differ_from_defaults_only(cfg):
    o = dict(sshopts.login_options("ssh"))
    assert o == {"IdentityFile": '"~/.ssh/nrao key"', "IdentitiesOnly": "yes"}
    node = sshopts.login_options("cvpost106")
    keys = [k for k, _ in node]
    # GSSAPI and the methods are login options; ControlPath,
    # ServerAliveInterval, User, HostName and ProxyJump are setup's own and
    # never copied.
    assert sorted(keys) == sorted(GSSAPI + ["PreferredAuthentications"])
    # A ProxyCommand only when the route is the user's.
    t = dict(sshopts.login_options("tunnel"))
    assert "ProxyCommand" not in t and t["IdentityFile"] == "~/.ssh/tunnel"
    t = dict(sshopts.login_options("tunnel", route=True))
    assert t["ProxyCommand"].startswith("nc -X connect")
    assert sshopts.login_options("unknown.example.org") == []


def test_nothing_from_an_empty_configuration(monkeypatch):
    monkeypatch.setenv("CUBEVIS_SETUP_USER_SSH_CONFIG", "/dev/null")
    assert sshopts.login_options("cvpost106") == []
    assert sshopts.view("cvpost106")["hostname"] == ["cvpost106"]


def test_jumps_become_real_names(cfg):
    assert sshopts.resolve_jump("ssh") == "dschieb@ssh.cv.example.org"
    assert sshopts.resolve_jump("gw,ssh") == "jo@gw.example.org:2222,dschieb@ssh.cv.example.org"
    assert sshopts.resolve_jump("al@gw:22") == "al@gw.example.org"
    assert sshopts.resolve_jump("x.example.org") == "x.example.org"


def test_a_proxyjump_is_adopted_and_the_generated_file_logs_in_the_same_way(cfg, tmp_path):
    c = ctx()
    n = H.locate(c, "cvpost106", 22)
    st = c.st
    net = st.networks()[st.nodes()[n]["network"]]
    assert net["login"]["host"] == "ssh.cv.example.org" and net["login"]["ssh_name"] == "ssh"
    out = " ".join(c.ui.out.getvalue().split())
    assert "setup does the same" in out and "Trying ssh" not in out
    text = sshgen.config_text(st)
    login = parses(text, tmp_path, net["login"]["alias"])
    assert login["identityfile"] == ["~/.ssh/nrao key"] and login["identitiesonly"] == ["yes"]
    assert login["user"] == ["dschieb"] and login["hostname"] == ["ssh.cv.example.org"]
    node = parses(text, tmp_path, st.nodes()[n]["alias"])
    assert node["preferredauthentications"] == ["publickey,password"]
    if GSSAPI:
        assert node["gssapiauthentication"] == ["yes"]
    assert "/rt/cm/" in node["controlpath"][0]                   # setup's, not the user's
    assert node["serveraliveinterval"] == [str(sshgen.SERVER_ALIVE_INTERVAL)]
    # The user's file changes: the next generated file follows (nothing stored).
    cfg.write_text(CONFIG.replace("    PreferredAuthentications publickey,password\n", ""))
    assert "PreferredAuthentications" not in sshgen.config_text(st)


def test_several_hops_keep_the_last_as_the_login_host(cfg, tmp_path):
    c = ctx()
    n = H.locate(c, "far1", 22)
    st = c.st
    lg = st.networks()[st.nodes()[n]["network"]]["login"]
    assert lg["host"] == "ssh.cv.example.org" and lg["via"] == "gw"
    text = sshgen.config_text(st)
    # Each hop its own entry (notes section 55): far-login-hop1 is gw, with
    # its real name, user and port; the login host jumps through it.
    hop = parses(text, tmp_path, "far-login-hop1")
    assert hop["hostname"] == ["gw.example.org"] and hop["user"] == ["jo"] and hop["port"] == ["2222"]
    assert "/rt/cm/" in hop["controlpath"][0] and hop["batchmode"] == ["yes"]
    login = parses(text, tmp_path, "far-login")
    assert login["proxyjump"] == ["far-login-hop1"]
    assert "(itself reached through gw)" in " ".join(c.ui.out.getvalue().split())
    # Recorded at the first save: then the hops no longer follow the user's file.
    c.save()
    assert st.networks()["far"]["login"]["hops"][0]["host"] == "gw.example.org"
    cfg.write_text(CONFIG.replace("    Port 2222\n", ""))
    assert "    Port 2222\n" in sshgen.config_text(st)
    ch = sshgen.changes(st)
    assert [x["hops"] for x in ch if "hops" in x] == [("jo@gw.example.org:2222", "jo@gw.example.org")]
    sshgen.adopt(st, ["far-login"])
    assert "    Port 2222\n" not in sshgen.config_text(st) and not sshgen.changes(st)


def test_options_are_recorded_and_changes_reported_not_followed(cfg, tmp_path):
    """Darrell 2026-11-02: what the user's ssh said when a host was set up
    is what the generated file carries; a change since is reported
    (``changes``) and adopted only on request (``adopt``)."""
    c = ctx()
    n = H.locate(c, "cvpost106", 22)
    st = c.st
    c.save()
    node = st.nodes()[n]["alias"]
    rec = sshgen.recorded(st, node)
    assert ["PreferredAuthentications", "publickey,password"] in rec["options"]
    assert rec["from"] == "cvpost106" and rec["saved"]
    assert not sshgen.changes(st)
    cfg.write_text(CONFIG.replace("    PreferredAuthentications publickey,password\n",
                                  "    PreferredAuthentications publickey\n"))
    text = sshgen.config_text(st)
    assert "PreferredAuthentications publickey,password" in text        # still the recorded one
    assert "recorded " in text                                          # the block says when
    ch = [x for x in sshgen.changes(st) if x["alias"] == node]
    assert ch and ch[0]["added"] == ["PreferredAuthentications publickey"]
    assert ch[0]["removed"] == ["PreferredAuthentications publickey,password"]
    sshgen.adopt(st, sshgen.network_aliases(st, st.nodes()[n]["network"], n))
    assert "PreferredAuthentications publickey\n" in sshgen.config_text(st)
    assert not sshgen.changes(st)


def test_known_hosts_and_batchmode_in_every_block(cfg, tmp_path, monkeypatch):
    """Setup's own known-hosts file first (shared by the computers sharing
    the configuration directory; new keys go there), then the user's;
    accept-new: a host never seen is recorded, a changed key refused.
    BatchMode yes: nothing in a kernel's ssh can ask (sshpyk's advice)."""
    monkeypatch.delenv("CUBEVIS_SETUP_KNOWN_HOSTS", raising=False)
    c = ctx()
    n = H.locate(c, "cvpost106", 22)
    text = sshgen.config_text(c.st)
    node = parses(text, tmp_path, c.st.nodes()[n]["alias"])
    files = node["userknownhostsfile"][0].split()
    assert files[0] == str(tmp_path / "xdg" / "cubevis" / "ssh_known_hosts")
    assert files[1:] == sshopts.known_hosts("cvpost106") and files[1:]
    assert node["stricthostkeychecking"] == ["accept-new"] and node["batchmode"] == ["yes"]
    # The tests' own file: only that one, unless the user configured others.
    monkeypatch.setenv("CUBEVIS_SETUP_KNOWN_HOSTS", str(tmp_path / "kh"))
    node = parses(sshgen.config_text(c.st), tmp_path, c.st.nodes()[n]["alias"])
    assert node["userknownhostsfile"] == [str(tmp_path / "kh")]


def test_a_proxycommand_route_is_kept_on_a_direct_network(cfg, tmp_path):
    c = ctx(via_login="n")
    n = H.locate(c, "tunnel", 22)
    st = c.st
    assert not st.networks()[st.nodes()[n]["network"]].get("login")
    out = " ".join(c.ui.out.getvalue().split())
    assert "through the command nc -X connect" in out
    node = parses(sshgen.config_text(st), tmp_path, st.nodes()[n]["alias"])
    assert node["proxycommand"][0].startswith("nc -X connect")
    assert node["identityfile"] == ["~/.ssh/tunnel"]


def test_try_login_tells_unreachable_from_answering():
    v, why = sshopts.try_login("127.0.0.1", 1, timeout=3)
    assert v == "unreachable", why
    v, why = sshopts.try_login("no-such-host.invalid", timeout=3)
    assert v == "unreachable", why


@pytest.mark.skipif(not RIG, reason="needs the ssh rig (CUBEVIS_SSH_RIG=1)")
def test_a_host_reached_directly_defaults_to_no_login_host(tmp_path, monkeypatch):
    """compute.example.test answers directly (it asks for a password): the
    login-host question is still asked -- one is needed off site -- but its
    default is no."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CUBEVIS_HOST_ID", "gui1")
    assert sshopts.try_login("compute.example.test", 2202, timeout=5)[0] == "answers"
    c = ctx(user="alice")
    n = H.locate(c, "compute.example.test", 2202)
    assert n and not c.st.networks()[c.st.nodes()[n]["network"]].get("login")
    out = " ".join(c.ui.out.getvalue().split())
    assert "this computer reaches compute.example.test directly" in out
    assert "When this computer is away from" in out and "through an intermediate host? [y/N]" in out
    assert "used only when this computer is away from" in out


def test_your_ssh_logs_in_but_setups_does_not(tmp_path, monkeypatch):
    """A host the user's own ssh logs in to by key, but not setup's file:
    setup does not offer to store a password, and names diagnose."""
    from cubevis.setup import steps
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CUBEVIS_HOST_ID", "gui1")
    c = ctx(user="jo")
    c.st.add_network("cv", login_host=None, user="jo", domain="cv.example.org")
    c.st.add_node("n1", network="cv")

    class R:
        returncode = 255
        stdout = ""
        stderr = ""

    def fake_ssh(alias, *a, **k):
        c.last_out = "jo@n1.cv.example.org: Permission denied (publickey)."
        return R()
    monkeypatch.setattr(c, "ssh", fake_ssh)
    monkeypatch.setattr(sshopts, "try_login", lambda name, port=None, timeout=8: ("ok", ""))
    assert steps.hop_steps(c, "cv", "n1") is False
    out = " ".join(c.ui.out.getvalue().split())
    assert "Your own ssh logs in to n1.cv.example.org without a password, but setup's" in out
    assert "python -m cubevis.setup diagnose n1.cv.example.org" in out
    assert "Store your password" not in out
