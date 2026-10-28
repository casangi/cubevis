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


# ---------------------------------------------------------------------- #
# Setup's own environments (stage 2c step 3), with the node's side mocked  #
# ---------------------------------------------------------------------- #

CONDA = "/opt/c/bin/conda"


def _rep(tools=((("conda", CONDA)),), dirs=("/opt/c/envs",), envs=()):
    return {"python_exe": "/usr/bin/python3", "python": "3.9.1",
            "host": {"home": "/home/jo", "config_dir": "/home/jo/.config/cubevis"},
            "tools": [{"name": n, "path": p} for n, p in tools],
            "envs_dirs": {p: list(dirs) for _n, p in tools}, "environments": list(envs)}


@pytest.fixture
def release(monkeypatch):
    """This computer has a release cubevis 1.0.97 with a known fingerprint."""
    monkeypatch.delenv("CUBEVIS_SETUP_TEST_INSTALL", raising=False)
    monkeypatch.setattr(H, "local_is_development", lambda: (False, ""))
    monkeypatch.setattr(H, "local_version", lambda: "1.0.97")
    monkeypatch.setattr(H, "local_fingerprint", lambda c: "FP-HERE")
    monkeypatch.setattr(H, "poll_seconds", lambda: 0.01)


def _node(c):
    return H.locate(c, "node1.example.org", 22)


def _plan(free=50 * 1024 ** 3, writable=True, taken=(), jobs=None):
    calls = []

    def call(c, alias, python3, req, timeout=120):
        calls.append(req)
        assert req["action"] == "plan", req
        return {"ok": True, "jobs": jobs or {}, "dirs": [
            {"dir": d, "exists": True, "free": free, "writable": writable, "taken": list(taken)}
            for d in req["dirs"]]}
    return call, calls


def test_a_development_install_is_not_made_elsewhere(ctx, monkeypatch):
    monkeypatch.delenv("CUBEVIS_SETUP_TEST_INSTALL", raising=False)
    monkeypatch.setattr(H, "local_is_development", lambda: (True, "version 1.0.97.dev3 is not a release"))
    monkeypatch.setattr(H, "local_fingerprint", lambda c: "FP-HERE")
    c = ctx(via_login="n", user="jo")
    node = _node(c)
    assert H.offer_creation(c, node, _rep()) is None
    out = " ".join(c.ui.out.getvalue().split())
    assert "development install" in out and "no package reproduces" in out
    assert "create -y" not in out                                    # nothing to run for it


def test_no_conda_says_how_to_get_one(ctx, release):
    c = ctx(via_login="n", user="jo")
    node = _node(c)
    assert H.offer_creation(c, node, _rep(tools=())) is None
    out = " ".join(c.ui.out.getvalue().split())
    assert "There is no conda on node1" in out and "Miniforge" in out and "make the environment" in out


def test_the_question_shows_where_and_how_big_and_declining_gives_the_commands(ctx, release, monkeypatch):
    call, calls = _plan(free=7 * 1024 ** 3)
    monkeypatch.setattr(H.envmake, "call", call)
    c = ctx(via_login="n", user="jo", make_env="n")
    node = _node(c)
    assert H.offer_creation(c, node, _rep()) is None
    out = " ".join(c.ui.out.getvalue().split())
    assert "Making an environment for cubevis 1.0.97 on node1" in out
    assert "about 1.5 GB" in out and "(7.0 GB free there)" in out and "PyPI" in out
    assert "cubevis[visplot]==1.0.97" in out and "(FP-HERE)" in out
    assert "To make it yourself" in out and f"{H.rtilde(CONDA, '/home/jo')} create -y -n cubevis-1.0.97" in out
    assert [r["action"] for r in calls] == ["plan"]                  # nothing was started
    assert calls[0]["dirs"] == ["/opt/c/envs"] and calls[0]["names"][0] == "cubevis-1.0.97"


def test_a_directory_that_cannot_be_written_is_refused_and_a_full_one_warned(ctx, release, monkeypatch):
    call, calls = _plan(writable=False)
    monkeypatch.setattr(H.envmake, "call", call)
    c = ctx(via_login="n", user="jo", env_dir="/nowhere")
    assert H.offer_creation(c, _node(c), _rep()) is None
    assert "cannot write in /nowhere" in c.ui.out.getvalue()
    call, calls = _plan(free=1 * 1024 ** 3)
    monkeypatch.setattr(H.envmake, "call", call)
    c = ctx(via_login="n", user="jo", make_env="n")
    H.offer_creation(c, _node(c), _rep())
    assert "only 1.0 GB free in /opt/c/envs" in c.ui.out.getvalue()


def test_every_name_taken_is_said(ctx, release, monkeypatch):
    call, _ = _plan(taken=envmake_names())
    monkeypatch.setattr(H.envmake, "call", call)
    c = ctx(via_login="n", user="jo")
    assert H.offer_creation(c, _node(c), _rep()) is None
    assert "every name cubevis-1.0.97... is taken" in c.ui.out.getvalue()


def envmake_names():
    return H.envmake.candidate_names("1.0.97")


def test_a_taken_name_gets_the_next_one(ctx, release, monkeypatch):
    seen = []

    def call(c, alias, python3, req, timeout=120):
        seen.append(req)
        if req["action"] == "plan":
            return {"ok": True, "jobs": {}, "dirs": [{"dir": d, "exists": True, "free": 9 * 1024 ** 3,
                                                       "writable": True, "taken": ["cubevis-1.0.97"]}
                                                      for d in req["dirs"]]}
        assert req["action"] == "start"
        return {"ok": False, "error": "stop here"}
    monkeypatch.setattr(H.envmake, "call", call)
    c = ctx(via_login="n", user="jo")
    assert H.offer_creation(c, _node(c), _rep()) is None
    start = seen[-1]
    assert start["name"] == "cubevis-1.0.97-2" and start["prefix"] == "/opt/c/envs/cubevis-1.0.97-2"
    assert start["tool"] == CONDA and start["spec"] == "cubevis[visplot]==1.0.97"
    assert start["version"] == "1.0.97" and start["setup"] == {"cubevis": "1.0.97", "host": "gui1"}
    assert start["python"] == "%d.%d" % __import__("sys").version_info[:2]
    assert "cannot start it on node1: stop here" in c.ui.out.getvalue()


def test_waiting_follows_the_steps_and_ends_on_the_state(ctx, release, monkeypatch):
    seq = iter([{"state": "running", "step": "creating the environment (conda)"},
                {"state": "running", "step": "creating the environment (conda)"},
                {"state": "running", "step": "installing cubevis (pip)"},
                {"state": "ok", "step": "done"}])
    monkeypatch.setattr(H.envmake, "call", lambda *a, **k: {"ok": True, "status": next(seq)})
    c = ctx(via_login="n", user="jo")
    st = H.wait_for_job(c, _node(c), _rep(), "cubevis-1.0.97")
    assert st["state"] == "ok"
    out = c.ui.out.getvalue()
    assert out.count("creating the environment (conda) ...") == 1       # said once, not per poll
    assert "installing cubevis (pip) ..." in out


def test_waiting_gives_up_on_an_unreachable_node_without_stopping_the_work(ctx, release, monkeypatch):
    def call(*a, **k):
        raise H.R.RemoteError("ssh exit 255")
    monkeypatch.setattr(H.envmake, "call", call)
    c = ctx(via_login="n", user="jo")
    assert H.wait_for_job(c, _node(c), _rep(), "cubevis-1.0.97") is None
    out = " ".join(c.ui.out.getvalue().split())
    assert "cannot reach node1" in out and "it goes on there" in out and "run this again" in out


def test_ctrl_c_stops_waiting_not_the_work(ctx, release, monkeypatch):
    def call(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(H.envmake, "call", call)
    c = ctx(via_login="n", user="jo")
    assert H.wait_for_job(c, _node(c), _rep(), "cubevis-1.0.97") is None
    out = " ".join(c.ui.out.getvalue().split())
    assert "Stopped waiting" in out and "goes on being made on node1" in out
    assert "~/.config/cubevis/make/cubevis-1.0.97.log" in out


def _own(prefix, version, host="gui1", fp="FP-OLD", python=None):
    return {"python": python or f"{prefix}/bin/python", "prefix": prefix, "kind": "conda",
            "cubevis": version, "marker": {"schema": 1, "name": prefix.rsplit("/", 1)[1],
                                           "version": version, "setup": {"host": host}},
            "check": {"versions": {"cubevis": version}, "kernel": True, "python": "3.12.1",
                      "fingerprint": fp, "errors": {}}}


def test_which_environments_are_setups_and_which_may_go(ctx, release, monkeypatch):
    c = ctx(via_login="n", user="jo")
    node = _node(c)
    net = c.st.nodes()[node]["network"]
    c.st.add_environment("used", network=net, python="/opt/c/envs/cubevis-0.9/bin/python")
    c.st.add_kernel("tw-hya", environment="used")
    users_env = {"python": "/home/jo/mine/bin/python", "prefix": "/home/jo/mine", "kind": "conda",
                 "cubevis": "1.0.97", "marker": None, "check": None}
    rep = _rep(envs=[_own("/opt/c/envs/cubevis-0.9", "0.9"),                  # a kernel uses it
                     _own("/opt/c/envs/cubevis-0.8", "0.8"),                  # superseded, mine
                     _own("/opt/c/envs/cubevis-0.7", "0.7", host="gui2"),      # another computer's
                     _own("/opt/c/envs/cubevis-1.0.97", "1.0.97", fp="FP-HERE"),   # current
                     users_env,
                     dict(_own("/opt/c/envs/cubevis-bad", "x"), marker={"error": "unreadable"})])
    own = {x["marker"]["name"]: x for x in H.own_environments(c, rep, net)}
    assert sorted(own) == ["cubevis-0.7", "cubevis-0.8", "cubevis-0.9", "cubevis-1.0.97"]
    assert own["cubevis-0.9"]["used_by"] == ["tw-hya"] and not own["cubevis-0.8"]["used_by"]
    assert own["cubevis-1.0.97"]["current"] and not own["cubevis-0.8"]["current"]
    assert own["cubevis-0.8"]["mine"] and not own["cubevis-0.7"]["mine"]
    # What is offered after choosing: mine, unused, not current, not the one just chosen.
    removed = []
    c2 = ctx(via_login="n", user="jo", cleanup="y")
    c2.st = c.st
    monkeypatch.setattr(H, "remove_environments",
                        lambda c_, n_, r_, items: removed.extend(i["marker"]["name"] for i in items)
                        or len(items))
    H.offer_cleanup(c2, node, rep, "node1.example.org", keep_python="/opt/c/envs/cubevis-1.0.97/bin/python")
    assert removed == ["cubevis-0.8"]
    out = c2.ui.out.getvalue()
    assert "cubevis-0.8" in out and "cubevis-0.7" not in out and "cubevis-0.9" not in out


def test_older_environments_are_offered_not_removed_by_default(ctx, release):
    c = ctx(via_login="n", user="jo")                      # --yes: the default is no
    node = _node(c)
    rep = _rep(envs=[_own("/opt/c/envs/cubevis-0.8", "0.8")])
    H.offer_cleanup(c, node, rep, "node1.example.org", keep_python="/x/bin/python")
    out = " ".join(c.ui.out.getvalue().split())
    assert "Older environments setup made on node1" in out
    assert "python -m cubevis.setup --cleanup node1.example.org removes them" in out


def test_a_used_environment_cannot_be_forgotten(ctx):
    c = ctx(via_login="n", user="jo")
    node = _node(c)
    net = c.st.nodes()[node]["network"]
    c.st.add_environment("e", network=net, python="/e/bin/python")
    c.st.add_kernel("k", environment="e")
    with pytest.raises(S.StateError, match="used by kernel k"):
        c.st.remove_environment("e")
    del c.st.section("kernels")["k"]
    c.st.remove_environment("e")
    assert "e" not in c.st.section("environments")
    with pytest.raises(S.StateError):
        c.st.remove_environment("e")


def test_cleanup_needs_a_host_known_to_setup(ctx):
    c = ctx(via_login="n", user="jo")
    assert H.cmd_cleanup(c, "never-seen.example.org") == 1
    assert "setup does not know never-seen.example.org" in c.ui.out.getvalue()
