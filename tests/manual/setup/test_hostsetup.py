"""``setup HOST`` without ssh (remote session design 7c): where a host is
placed, paths on the node, the cluster question, the hint after a failed
look.  The flow over ssh is in ``test_cli.py`` (the rig).

    pytest tests/manual/setup/test_hostsetup.py
"""
from __future__ import annotations

import io

import pytest

from cubevis.setup import hostsetup as H
from cubevis.setup import state as S
from cubevis.setup.steps import Ctx
from cubevis.setup.ui import UI


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("CUBEVIS_HOST_ID", "gui1")
    monkeypatch.setenv("USER", "jo")

    def make(**answers):
        ui = UI(yes=True, answers=answers, out=io.StringIO(), inp=io.StringIO())
        return Ctx(ui, S.load())
    return make


def test_paths_on_the_node():
    assert H.expand_remote("~", "/home/jo") == "/home/jo"
    assert H.expand_remote("~/data/../tw", "/home/jo") == "/home/jo/tw"
    assert H.expand_remote("/lustre/x/", "/home/jo") == "/lustre/x"
    assert H.expand_remote("scratch", "/home/jo") == "/home/jo/scratch"
    assert H.rtilde("/home/jo/tw", "/home/jo") == "~/tw" and H.rtilde("/home/jo", "/home/jo/") == "~"
    assert H.rtilde("/home/joe/x", "/home/jo") == "/home/joe/x"


def test_where_a_host_goes(ctx):
    """New: the route is asked and the network named from the domain.  Then
    a host of the same domain joins it, a known one is found by any of its
    names, and an address is a node of its own name."""
    c = ctx(via_login="y", login_host="login.cv.example.org", login_user="jo", user="jo")
    n = H.locate(c, "zuul06.cv.example.org", 22)
    st = c.st
    assert n == "zuul06" and st.nodes()[n]["network"] == "cv"
    net = st.networks()["cv"]
    assert net["login"]["host"] == "login.cv.example.org" and net["domain"] == "cv.example.org"
    c2 = Ctx(UI(yes=True, out=io.StringIO()), st)             # nothing asked
    assert H.locate(c2, "cvpost140.cv.example.org", 22) == "cvpost140"
    assert st.nodes()["cvpost140"]["network"] == "cv"
    assert "reached the same way" in " ".join(c2.ui.out.getvalue().split())
    assert H.locate(c2, "cv-zuul06", 22) == "zuul06"
    assert H.locate(c2, "zuul06", 2222) == "zuul06" and st.nodes()["zuul06"]["port"] == 2222
    c3 = Ctx(UI(yes=True, answers={"via_login": "n", "user": "jo"}, out=io.StringIO()), st)
    n = H.locate(c3, "10.1.2.3", 22)
    assert n == "10.1.2.3" and st.nodes()[n]["network"] not in ("cv",)


def test_the_cluster_question_and_its_nodes(ctx):
    c = ctx(via_login="n", user="jo", cluster="y")
    n = H.locate(c, "cvpost140.cv.example.org", 22)
    H.locate(c, "cvpost139.cv.example.org", 22)               # known before the cluster
    H.cluster_step(c, n)
    st = c.st
    assert st.section("clusters") == {"cvpost": {"network": "cv", "pattern": "cvpost*"}}
    assert st.nodes()["cvpost140"]["cluster"] == st.nodes()["cvpost139"]["cluster"] == "cvpost"
    # Another node of it: added by its name alone.
    c2 = Ctx(UI(yes=True, out=io.StringIO()), st)
    assert H.locate(c2, "cvpost141", 22) == "cvpost141"
    assert st.nodes()["cvpost141"]["cluster"] == "cvpost"
    # No digits, the default is no; asked once.
    c3 = ctx(via_login="n", user="jo")
    m = H.locate(c3, "gateway.other.org", 22)
    H.cluster_step(c3, m)
    assert c3.st.nodes()[m]["standalone"] is True and not c3.st.section("clusters")
    out = c3.ui.out.getvalue()
    H.cluster_step(c3, m)
    assert c3.ui.out.getvalue() == out


def test_a_pattern_that_misses_the_host_is_refused(ctx):
    c = ctx(via_login="n", user="jo", cluster="y", cluster_name="cv", cluster_pattern="nm*")
    n = H.locate(c, "cvpost140.cv.example.org", 22)
    H.cluster_step(c, n)
    assert not c.st.section("clusters") and not c.st.nodes()[n].get("cluster")
    assert "does not match cvpost140" in c.ui.out.getvalue()


def test_after_a_failed_look_the_bundle_is_left_here(ctx):
    c = ctx()
    H.discover_hint(c, "zuul06")
    from cubevis.setup import host_dir
    p = host_dir() / "discover.py"
    assert p.read_text().startswith("# cubevis setup's discovery script")
    assert "python3 -m cubevis.setup.discover" in c.ui.out.getvalue()


def test_the_walk_asks_for_a_host_and_stops_on_none(tmp_path, monkeypatch):
    import subprocess, sys, os
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    env = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path), CUBEVIS_HOST_ID="gui1", NO_COLOR="1",
               PYTHONPATH=root)
    r = subprocess.run([sys.executable, "-m", "cubevis.setup"], input="\n", capture_output=True,
                       text=True, env=env, timeout=60)
    assert r.returncode == 0 and "The host your data is on" in r.stdout, r.stdout + r.stderr


def test_a_password_command_that_holds_it_in_plain_text_is_refused(monkeypatch):
    from cubevis.setup import stores
    monkeypatch.delenv("CUBEVIS_SETUP_ALLOW_PLAIN_COMMAND", raising=False)
    for cmd in ("echo 's3cret pw!'", "printf %s pw", "/bin/cat ~/pw.txt", "head -1 pw"):
        assert "plain text" in stores.plain_text_command(cmd), cmd
    for cmd in ("op read op://vault/@SERVICE@/@ACCOUNT@", "bw get password ssh", "security x"):
        assert stores.plain_text_command(cmd) is None, cmd
    monkeypatch.setenv("CUBEVIS_SETUP_ALLOW_PLAIN_COMMAND", "1")      # the test rig's
    assert stores.plain_text_command("echo pw") is None


def test_without_sshpyk_setup_offers_it_and_unattended_does_not_install(ctx, monkeypatch):
    from cubevis.setup import jupyterspec as J
    monkeypatch.setattr(J, "have_sshpyk", lambda: False)
    c = ctx()
    assert J.ensure_sshpyk(c) is False
    out = " ".join(c.ui.out.getvalue().split())
    assert "sshpyk starts kernels on other computers" in out and "not installed (unattended" in out
    assert "-m pip install" in out


def test_visplot_takes_a_setup_kernels_own_name(tmp_path, monkeypatch):
    """kernel_name="cvpost" (the name setup gave) starts cubevis-cvpost; a
    setup kernel without its Jupyter kernel says how to make it."""
    import json as _json
    from cubevis.toolbox.visplot import visibility_plotter as V
    monkeypatch.setenv("JUPYTER_DATA_DIR", str(tmp_path / "jd"))
    monkeypatch.setenv("JUPYTER_PATH", str(tmp_path / "jd"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    d = tmp_path / "jd" / "kernels" / "cubevis-cvpost"
    d.mkdir(parents=True)
    (d / "kernel.json").write_text(_json.dumps({"argv": ["python", "-c", "pass"],
                                                "display_name": "x", "language": "python"}))
    assert V._resolve_kernel_name("cvpost") == "cubevis-cvpost"
    assert V._resolve_kernel_name("cubevis-cvpost") == "cubevis-cvpost"
    st = S.load()
    st.add_network("n", login_host=None, user="jo")
    st.add_node("a", network="n", hostname="a.example.org")
    st.add_environment("e", network="n", python="/e/bin/python", hosts=["a"])
    st.add_kernel("tw", environment="e")
    S.save(st)
    with pytest.raises(ValueError, match="'tw' is a kernel set up with python -m cubevis.setup"):
        V._resolve_kernel_name("tw")
    with pytest.raises(ValueError, match="set up with cubevis: tw"):
        V._resolve_kernel_name("nope")


def test_defaults_come_from_the_users_ssh_configuration(ctx, tmp_path, monkeypatch):
    """Darrell's ~/.ssh/config names the login host 'ssh' and the node by
    short names, with his user: setup takes the real names, the user and
    the route from it (ssh -G), asking only to confirm."""
    import shutil as _sh
    if not _sh.which("ssh"):
        pytest.skip("needs ssh")
    cfg = tmp_path / "ssh_config"
    cfg.write_text("Host ssh\n    User dschieb\n    HostName ssh.cv.example.org\n"
                   "Host cvpost106\n    User dschieb\n    HostName cvpost106.cv.example.org\n"
                   "    ProxyJump dschieb@ssh\n")
    monkeypatch.setenv("CUBEVIS_SETUP_USER_SSH_CONFIG", str(cfg))
    c = ctx()                                   # every default taken
    n = H.locate(c, "cvpost106", 22)
    st = c.st
    node = st.nodes()[n]
    net = st.networks()[node["network"]]
    assert node["hostname"] == "cvpost106.cv.example.org"
    assert net["login"]["host"] == "ssh.cv.example.org" and net["login"]["user"] == "dschieb"
    assert net["user"] == "dschieb" and net["domain"] == "cv.example.org"
    out = " ".join(c.ui.out.getvalue().split())
    assert "Your ssh configuration has cvpost106 as dschieb@cvpost106.cv.example.org" in out
    assert "ssh is ssh.cv.example.org (your ssh configuration)" in out
    # Another node typed by its short name joins the same network, no questions.
    cfg.write_text(cfg.read_text() + "Host cvpost107\n    HostName cvpost107.cv.example.org\n")
    c2 = Ctx(UI(yes=True, out=io.StringIO()), st)
    m = H.locate(c2, "cvpost107", 22)
    assert st.nodes()[m]["network"] == node["network"]
    assert "reached the same way" in " ".join(c2.ui.out.getvalue().split())


def test_without_a_configuration_nothing_is_made_up(ctx, monkeypatch):
    from cubevis.setup.steps import user_ssh, first_jump
    import shutil as _sh
    if not _sh.which("ssh"):
        pytest.skip("needs ssh")
    monkeypatch.setenv("CUBEVIS_SETUP_USER_SSH_CONFIG", "/dev/null")
    g = user_ssh("node7.example.org")
    assert g["hostname"] == "node7.example.org" and g["port"] == 22 and g["proxyjump"] is None
    assert g["configured"] is False
    assert first_jump("jo@gw.example.org:2222,inner") == ("gw.example.org", "jo", 2222)
    assert first_jump("gw") == ("gw", None, 22)


def test_a_store_that_fails_offers_another(ctx, monkeypatch):
    """A Keychain that cannot store (an account without one) is not tried
    again and again: setup says it failed and offers the other stores."""
    from cubevis.setup import stores
    c = ctx(store="command", store_command="op read op://v/@SERVICE@/@ACCOUNT@")
    c.st.local["store"] = "keychain"
    tried = []

    def fake_put(ui, st, service, account, host):
        tried.append(st.local.get("store"))
        return st.local.get("store") == "command"
    monkeypatch.setattr(stores, "put", fake_put)
    monkeypatch.setattr(stores, "available", lambda name: (name in ("keychain", "command"), "x"))
    assert stores.put_or_switch(c.ui, c.st, "cubevis-ssh", "jo", "gw") is True
    assert tried == ["keychain", "command"] and c.st.local["store"] == "command"
    out = c.ui.out.getvalue()
    assert "Use another password store (command)?" in out and "failed just now" not in out


def test_the_keychain_is_checked_before_it_is_offered(monkeypatch):
    from cubevis.setup import stores
    import subprocess as sp

    def fake_run(cmd, **kw):
        return sp.CompletedProcess(cmd, 0, '    "/Users/t/Library/Keychains/login.keychain-db"\n', "")
    monkeypatch.setattr(stores.subprocess, "run", fake_run)
    assert "missing" in stores.keychain_problem()
    monkeypatch.setattr(stores.subprocess, "run",
                        lambda cmd, **kw: sp.CompletedProcess(cmd, 1, "", "no default"))
    assert stores.keychain_problem() == "this account has no default keychain"


def test_a_kernel_is_named_for_its_directory_not_a_node(ctx):
    """2026-10-28: the working directory's name; in the home directory, the
    cluster's name (never one of its nodes: reservations move) or a lone
    host's; a taken name gets -2."""
    c = ctx(via_login="n", user="jo")
    st = c.st
    n = H.locate(c, "cvpost140.cv.example.org", 22)
    node = st.nodes()[n]
    assert H.default_kernel_name(st, node, n, "/home/jo/casa/visplot/") == "visplot"
    assert H.default_kernel_name(st, node, n, None) == "cvpost140"          # alone
    st.add_cluster("cvpost", network=node["network"], pattern="cvpost*")
    node["cluster"] = "cvpost"
    assert H.default_kernel_name(st, node, n, None) == "cvpost"
    assert H.default_kernel_name(st, node, n, "/lustre/aoc/my data!") == "my-data"
    st.add_environment("e", network=node["network"], python="/e/bin/python", cluster="cvpost",
                       hosts=[])
    st.add_kernel("visplot", environment="e")
    assert H.default_kernel_name(st, node, n, "/x/visplot") == "visplot-2"
