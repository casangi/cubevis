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
    the route from it (ssh -G), adopting its ProxyJump without questions."""
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
    assert ("Your ssh configuration reaches cvpost106 (cvpost106.cv.example.org) through "
            "dschieb@ssh; setup does the same: dschieb@ssh.cv.example.org is the login host") in out
    assert "intermediate host" not in out and "user name" not in out      # nothing asked
    assert net["login"]["ssh_name"] == "ssh" and node["ssh_name"] == "cvpost106"
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
    assert "development install" in out and "no package on PyPI reproduces" in out
    assert "create -y" not in out                                    # nothing to run for it
    # What makes it work there: an environment of the user's with the same
    # code, found by its fingerprint (copying was removed, notes section 50).
    assert "install it in an environment there (pip install -e DIR" in out
    assert "python -m cubevis.setup node1 again: setup finds the environment" in out
    assert "--sync-checkout" not in out and "--wheel" not in out


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
    # kernels there run interactive clean too unless asked not to (step 5b)
    assert start["tool"] == CONDA and start["spec"] == "cubevis[visplot,iclean]==1.0.97"
    assert start["version"] == "1.0.97" and start["setup"] == {"cubevis": "1.0.97", "host": "gui1"}
    assert start["python"] == "%d.%d" % __import__("sys").version_info[:2]
    assert "cannot start it on node1: stop here" in c.ui.out.getvalue()
    assert "Should kernels on node1 run interactive clean?" in c.ui.out.getvalue()
    c = ctx(via_login="n", user="jo", iclean="n")
    assert H.offer_creation(c, _node(c), _rep()) is None
    assert seen[-1]["spec"] == "cubevis[visplot]==1.0.97" and c.iclean_answer is False


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
    # What is offered after choosing: unused, not current, not the one just chosen --
    # from any computer (Darrell 2026-11-02), each saying which made it.
    removed = []
    c2 = ctx(via_login="n", user="jo", cleanup="y")
    c2.st = c.st
    monkeypatch.setattr(H, "remove_environments",
                        lambda c_, n_, r_, items: removed.extend(i["marker"]["name"] for i in items)
                        or len(items))
    H.offer_cleanup(c2, node, rep, "node1.example.org", keep_python="/opt/c/envs/cubevis-1.0.97/bin/python")
    assert removed == ["cubevis-0.8", "cubevis-0.7"]
    out = " ".join(c2.ui.out.getvalue().split())
    assert "cubevis-0.9" not in out and "cubevis-1.0.97" not in out
    assert "cubevis-0.8 (cubevis 0.8; made from this computer)" in out
    assert "cubevis-0.7 (cubevis 0.7; made from gui2)" in out


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


def test_asking_for_new_with_a_development_install_does_not_say_nothing_matches(ctx, monkeypatch):
    """'new' while an environment is usable: setup says it cannot make one
    for a development install, and does not claim no environment matches."""
    monkeypatch.delenv("CUBEVIS_SETUP_TEST_INSTALL", raising=False)
    monkeypatch.setattr(H, "local_is_development", lambda: (True, "version 1.0.97.dev3 is not a release"))
    monkeypatch.setattr(H, "local_fingerprint", lambda c: "FP-HERE")
    c = ctx(via_login="n", user="jo")
    assert H.offer_creation(c, _node(c), _rep(), forced=True) is None
    out = " ".join(c.ui.out.getvalue().split())
    assert "Setup cannot make an environment from PyPI for this cubevis" in out
    assert "No environment on" not in out and "choose one of the environments listed" in out


def _running(step="installing cubevis (pip)", **act):
    base = dict(cpu=1.0, io=1000, log_size=100, log_age=1, procs=["pip"], disk_wait=0, alive=2)
    base.update(act)
    return {"state": "running", "step": step, "activity": base,
            "log_tail": ["Collecting bokeh", "Downloading bokeh-3.10.0-py3-none-any.whl (6.6 MB)"]}


def test_pulse_moves_only_when_the_node_says_the_work_moved():
    p = H.Pulse()
    assert p.update(_running()) is False                          # the first answer: nothing to compare
    assert p.update(_running()) is False and p.spin == 0          # same counters: not moving
    assert p.update(_running(cpu=2.0)) is True and p.spin == 1    # CPU grew
    assert p.update(_running(cpu=2.0, io=5000)) is True           # data moved
    assert p.update(_running(cpu=2.0, io=5000, log_size=300)) is True      # the log grew
    assert p.update({"state": "running", "step": "x"}) is False   # no report (not Linux): no verdict
    assert p.words({"state": "running"}, "node1") == ""


def test_pulse_words_tell_quiet_from_waiting_on_the_disk_from_stuck():
    p = H.Pulse()
    st = _running()
    p.update(st)
    assert p.words(st, "node1").startswith("working (pip)")
    p.last_move -= 60
    assert p.words(st, "node1").startswith("quiet for 1:0")
    p.last_move -= 300
    assert "no output, CPU or data for 6:0" in p.words(st, "node1")
    assert "not stopped" in p.words(st, "node1")
    p.last_move += 300
    words = p.words(_running(disk_wait=1), "node1")
    assert words.startswith("waiting on node1's disk") and "slow shared filesystem" in words


def test_waiting_says_how_it_is_going_from_what_the_node_reports(ctx, release, monkeypatch):
    seq = iter([_running(), _running(cpu=2.0), _running(cpu=2.0, disk_wait=1),
                {"state": "ok", "step": "done"}])
    monkeypatch.setattr(H.envmake, "call", lambda *a, **k: {"ok": True, "status": next(seq)})
    c = ctx(via_login="n", user="jo")
    assert H.wait_for_job(c, _node(c), _rep(), "cubevis-1.0.97")["state"] == "ok"
    out = c.ui.out.getvalue()
    assert "installing cubevis (pip) ... working (pip)" in out
    assert "\r" not in out                                           # not a terminal: lines only


def test_a_node_that_stops_answering_is_said_to_be_slow_and_then_back(ctx, release, monkeypatch):
    seq = iter([H.R.RemoteError("timed out"), H.R.RemoteError("timed out"),
                {"ok": True, "status": _running()}, {"ok": True, "status": {"state": "ok", "step": "done"}}])

    def call(*a, **k):
        r = next(seq)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(H.envmake, "call", call)
    c = ctx(via_login="n", user="jo")
    assert H.wait_for_job(c, _node(c), _rep(), "cubevis-1.0.97")["state"] == "ok"
    out = " ".join(c.ui.out.getvalue().split())
    assert out.count("node1 is slow to answer") == 1 and "node1 answers again" in out


def test_the_live_line_is_rewritten_on_a_terminal_only(ctx, release, monkeypatch):
    class Tty(__import__("io").StringIO):
        def isatty(self):
            return True
    seq = iter([_running(), _running(cpu=3.0), {"state": "ok", "step": "done"}])
    monkeypatch.setattr(H.envmake, "call", lambda *a, **k: {"ok": True, "status": next(seq)})
    c = ctx(via_login="n", user="jo")
    c.ui.out = Tty()
    assert H.wait_for_job(c, _node(c), _rep(), "cubevis-1.0.97")["state"] == "ok"
    out = c.ui.out.getvalue()
    assert "\r\x1b[2K" in out and "installing cubevis (pip)" in out


def test_a_checkout_with_a_releases_kernel_code_gets_that_release(ctx, monkeypatch):
    """A checkout used without installing it, past its tag (or with local
    changes) whose kernel-side code is still that release's: setup makes the
    release's environment from PyPI -- no copy, and not "a development
    install no package reproduces" (2026-11-01)."""
    monkeypatch.delenv("CUBEVIS_SETUP_TEST_INSTALL", raising=False)
    monkeypatch.setattr(H, "local_is_development",
                        lambda: (True, "a checkout at version 1.0.104.dev2+gabc, not a release"))
    monkeypatch.setattr(H, "local_version", lambda: "1.0.104.dev2+gabc")
    monkeypatch.setattr(H, "local_fingerprint", lambda c: "FP-HERE")
    monkeypatch.setattr(H.checkout, "checkout_root", lambda: "/src/cubevis")
    monkeypatch.setattr(H.checkout, "release_with_same_code", lambda root, fp: "1.0.103")
    call, calls = _plan()
    monkeypatch.setattr(H.envmake, "call", call)
    c = ctx(via_login="n", user="jo", make_env="n")
    assert H.offer_creation(c, _node(c), _rep()) is None
    out = " ".join(c.ui.out.getvalue().split())
    assert "Making an environment for cubevis 1.0.103 on node1" in out
    assert ("cubevis 1.0.103 (cubevis[visplot]==1.0.103, from PyPI; this checkout "
            "(1.0.104.dev2+gabc) has the same kernel-side code)") in out
    assert calls[0]["names"][0] == "cubevis-1.0.103"
    assert "create -y -n cubevis-1.0.103" in out                   # declined: the commands
    assert "no package on PyPI reproduces" not in out


def test_a_published_stamp_comes_before_the_git_check(ctx, monkeypatch):
    """same_release: a release whose PyPI description is stamped with this
    computer's fingerprint wins; git is asked only when none is."""
    from cubevis.setup import releases
    monkeypatch.setattr(H, "local_fingerprint", lambda c: "FP-HERE")
    asked = []
    monkeypatch.setattr(H.checkout, "checkout_root", lambda: "/src/cubevis")
    monkeypatch.setattr(H.checkout, "release_with_same_code", lambda root, fp: asked.append(fp) or "1.0.103")
    monkeypatch.setattr(releases, "release_with_same_code", lambda fp: ("1.0.105", 2))
    c = ctx()
    assert H.same_release(c) == "1.0.105" and asked == []
    monkeypatch.setattr(releases, "release_with_same_code", lambda fp: (None, 0))
    c = ctx()
    assert H.same_release(c) == "1.0.103" and asked == ["FP-HERE"]
    H.same_release(c)
    assert asked == ["FP-HERE"]                                     # once per run


class _Clock:
    """``time.monotonic`` for ``write_and_test``: each launcher test takes
    the next of *seconds*."""
    def __init__(self, seconds):
        self.seconds, self.t, self.started = list(seconds), 0.0, False

    def monotonic(self):
        if self.started:
            self.t += self.seconds.pop(0)
        self.started = not self.started
        return self.t


def _slow_kernel(ctx, monkeypatch, seconds, **record):
    """A kernel whose launcher tests take *seconds* in turn; the launcher
    texts written, in order."""
    monkeypatch.setattr(H, "local_fingerprint", lambda c: "FP")
    monkeypatch.setattr(H, "time", _Clock(seconds))
    written = []
    monkeypatch.setattr(H.R, "install_launcher", lambda c, a, py, path, text: written.append(text) or "written")
    monkeypatch.setattr(H.R, "test_launcher", lambda c, a, path: ("FP", "/home/jo", None))
    c = ctx(via_login="n", user="jo")
    node = _node(c)
    net = c.st.nodes()[node]["network"]
    c.st.add_environment("e", network=net, python="/c/envs/x/bin/python", kind="conda",
                         prefix="/c/envs/x", hosts=[node])
    k = c.st.add_kernel("k", environment="e")
    k.update(record)
    return c, node, written


def test_a_slow_start_is_tried_with_pythonhome_and_kept_when_it_helps(ctx, monkeypatch):
    c, node, written = _slow_kernel(ctx, monkeypatch, [52.0, 0.6])
    ok, msg = H.write_and_test(c, node, "k", _rep(), "zuul06")
    assert ok, msg
    k = c.st.section("kernels")["k"]
    assert k["pythonhome"] is True and k["pythonhome_by"] == "setup"
    assert k["pythonhome_times"] == [52.0, 0.6]                  # diagnose shows them
    assert "PYTHONHOME" not in written[0] and "export PYTHONHOME=/c/envs/x" in written[1]
    out = " ".join(c.ui.out.getvalue().split())
    assert "Starting it took 52 s; trying again with PYTHONHOME=/c/envs/x" in out
    assert "with PYTHONHOME it starts in 0.6 s: kernel k sets it" in out
    assert "--no-pythonhome takes it out" in out


def test_pythonhome_is_dropped_when_it_does_not_help(ctx, monkeypatch):
    c, node, written = _slow_kernel(ctx, monkeypatch, [30.0, 29.0, 30.0])
    ok, msg = H.write_and_test(c, node, "k", _rep(), "zuul06")
    assert ok, msg
    k = c.st.section("kernels")["k"]
    assert not k["pythonhome"] and len(written) == 3 and "PYTHONHOME" not in written[2]
    assert "PYTHONHOME does not help here (29 s): not set" in " ".join(c.ui.out.getvalue().split())


def test_your_no_pythonhome_is_not_overridden(ctx, monkeypatch):
    c, node, written = _slow_kernel(ctx, monkeypatch, [52.0], pythonhome=False, pythonhome_by="you")
    ok, msg = H.write_and_test(c, node, "k", _rep(), "zuul06")
    assert ok and len(written) == 1 and not c.st.section("kernels")["k"]["pythonhome"]
    # and a fast start is left alone
    c, node, written = _slow_kernel(ctx, monkeypatch, [1.0])
    assert H.write_and_test(c, node, "k", _rep(), "zuul06")[0] and len(written) == 1


def test_redo_does_not_redo_a_working_key_login(ctx, monkeypatch):
    """``--redo`` (Darrell 2026-11-03, zuul06): a node logged in to by key
    was put through the repair of a failed login -- "Your own ssh logs in
    ... but setup's configuration does not" -- and reported FAIL, though
    the login worked.  Logins are checked under --redo, not redone."""
    import subprocess
    from cubevis.setup import steps
    c = ctx(via_login="n", user="jo")
    node = _node(c)
    c.redo = True
    calls = []

    def ssh(alias, *remote, **kw):
        calls.append(alias)
        c.last_out = ""
        return subprocess.CompletedProcess(["ssh"], 0, "", "")
    monkeypatch.setattr(c, "ssh", ssh)
    from cubevis.setup import sshopts
    monkeypatch.setattr(sshopts, "try_login",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("repair ran")))
    net = c.st.nodes()[node]["network"]
    assert steps.hop_steps(c, net, node)
    out = " ".join(c.ui.out.getvalue().split())
    assert "FAIL" not in out and "ok Log in to the node node1" in out and "with an ssh key" in out
    assert calls                                          # checked


def test_a_step_whose_check_passes_after_a_declined_do_is_done(ctx):
    from cubevis.setup.ui import FuncStep, run_step
    c = ctx()
    assert run_step(c.ui, FuncStep("Thing", lambda: (True, "fine"), lambda: False), redo=True)
    out = c.ui.out.getvalue()
    assert "ok    Thing -- fine" in out and "FAIL" not in out


# --------------------------------------------------------------------------
# Interactive clean and CASA's data (iclean design step 5b)
# --------------------------------------------------------------------------

MARKER = {"schema": 1, "name": "cubevis-1.0.97", "version": "1.0.97", "tool": CONDA}


def _env(c, node, marker=MARKER, **fields):
    net = c.st.nodes()[node]["network"]
    e = c.st.add_environment("e", network=net, python="/opt/c/envs/cubevis-1.0.97/bin/python",
                             kind="conda", prefix="/opt/c/envs/cubevis-1.0.97", hosts=[node])
    e.update(cubevis="1.0.97", marker=marker, **fields)
    return e


def test_setups_environment_is_offered_interactive_clean_once(ctx, release, monkeypatch):
    added = []
    monkeypatch.setattr(H, "add_to_environment", lambda c, node, rep, env, **kw: added.append((env, kw)) or True)
    c = ctx(via_login="n", user="jo")
    node = _node(c)
    e = _env(c, node, iclean=False)
    H.iclean_step(c, node, "e", _rep())
    assert added == [("e", {"iclean": True})]
    out = " ".join(c.ui.out.getvalue().split())
    assert "cannot run interactive clean: it has no casatasks. Setup made that environment" in out
    # no: remembered, not asked again ...
    added.clear()
    c = ctx(via_login="n", user="jo", add_iclean="n")
    c.st.section("environments")["e"] = e
    H.iclean_step(c, node, "e", _rep())
    assert added == [] and e["iclean_declined"] is True
    c.ui.out = __import__("io").StringIO()
    H.iclean_step(c, node, "e", _rep())
    assert c.ui.out.getvalue() == ""
    # ... until --redo
    c = ctx(via_login="n", user="jo")
    c.st.section("environments")["e"] = e
    c.redo = True
    H.iclean_step(c, node, "e", _rep())
    assert added == [("e", {"iclean": True})] and "iclean_declined" not in e


def test_your_environment_is_told_how_not_installed_into(ctx, release, monkeypatch):
    monkeypatch.setattr(H, "add_to_environment", lambda *a, **k: pytest.fail("installed into yours"))
    c = ctx(via_login="n", user="jo")
    node = _node(c)
    _env(c, node, marker=None, iclean=False)
    H.iclean_step(c, node, "e", _rep())
    out = " ".join(c.ui.out.getvalue().split())
    assert "It is your environment, so setup does not install into it" in out
    assert '-m pip install "cubevis[iclean]==1.0.97"' in out


def test_nothing_is_offered_when_it_can_or_nobody_knows(ctx, release, monkeypatch):
    monkeypatch.setattr(H, "add_to_environment", lambda *a, **k: pytest.fail("offered"))
    c = ctx(via_login="n", user="jo")
    node = _node(c)
    c.ui.out = __import__("io").StringIO()
    e = _env(c, node, iclean=True)
    H.iclean_step(c, node, "e", _rep())
    e["iclean"] = None                                  # checked before step 5b
    H.iclean_step(c, node, "e", _rep())
    c.iclean_answer = False                             # said no when it was made, this run
    e["iclean"] = False
    H.iclean_step(c, node, "e", _rep())
    assert c.ui.out.getvalue() == "" and e["iclean_declined"] is True


def test_a_kernels_test_says_what_its_environment_lacks(ctx, release, monkeypatch):
    """An environment checked before 5b (``iclean`` unknown): its kernels'
    launcher tests decide."""
    c = ctx(via_login="n", user="jo")
    node = _node(c)
    _env(c, node)
    assert H.environment_iclean(c.st, "e") is None
    c.st.add_kernel("k", environment="e")["capabilities"] = {"visplot": True, "iclean": False}
    assert H.environment_iclean(c.st, "e") is False


def test_adding_waits_and_checks_the_environment_again(ctx, release, monkeypatch):
    seen = []

    def call(c, alias, python3, req, timeout=120):
        seen.append(req)
        if req["action"] == "add":
            return {"ok": True, "started": True}
        return {"ok": True, "status": {"state": "ok", "step": "done"}}
    monkeypatch.setattr(H.envmake, "call", call)
    prefix = "/opt/c/envs/cubevis-1.0.97"
    marker = dict(MARKER, iclean=True, casa_site_config=f"{prefix}/{H.capabilities.SITE_CONFIG_FILE}")
    check = {"python": "3.12.3", "prefix": prefix, "versions": {"cubevis": "1.0.97", "ipykernel": "6"},
             "kernel": True, "visplot": True, "iclean": True, "errors": {}, "fingerprint": "FP-HERE"}
    monkeypatch.setattr(H, "discover_step", lambda c, node, paths, only=False, python3=None: {
        "environments": [{"python": f"{prefix}/bin/python", "prefix": prefix, "kind": "conda",
                          "marker": marker, "check": check}]})
    c = ctx(via_login="n", user="jo")
    node = _node(c)
    _env(c, node, iclean=False)
    assert H.add_to_environment(c, node, _rep(), "e", iclean=True, casa_data=True)
    add = seen[0]
    assert add["action"] == "add" and add["prefix"] == prefix
    assert add["spec"] == "cubevis[visplot,iclean]==1.0.97 casadata"
    assert add["site_config"]["file"] == H.capabilities.SITE_CONFIG_FILE
    assert add["marker_update"] == {"iclean": True, "casa_site_config": marker["casa_site_config"]}
    assert seen[1] == dict(seen[1], action="status", name="cubevis-1.0.97")
    e = c.st.section("environments")["e"]
    assert e["iclean"] is True and e["casa_site_config"] == marker["casa_site_config"]


def _kernel(c, node, data, **env_fields):
    _env(c, node, iclean=True, **env_fields)
    k = c.st.add_kernel("k", environment="e")
    k.update(capabilities={"visplot": True, "iclean": True}, casa_data=data, status="ok")
    return k


def test_found_data_is_used_or_replaced_by_a_private_copy(ctx, release, monkeypatch):
    added = []
    monkeypatch.setattr(H, "add_to_environment", lambda c, node, rep, env, **kw: added.append(kw) or True)
    tested = []
    monkeypatch.setattr(H, "write_and_test", lambda c, node, name, rep, h: tested.append(name) or (True, "ok"))
    data = {"ready": True, "measurespath": "/home/jo/.casa/data", "files": ["/home/jo/.casa/config.py"],
            "auto_update": True}
    c = ctx(via_login="n", user="jo")                      # the default: use it
    node = _node(c)
    _kernel(c, node, data)
    H.casa_data_step(c, node, "k", _rep(), "node1")
    out = " ".join(c.ui.out.getvalue().split())
    assert "finds CASA's data in ~/.casa/data (set by ~/.casa/config.py; CASA updates it itself)" in out
    assert added == [] and c.st.section("environments")["e"]["casa_data_choice"] == "own"
    c.ui.out = __import__("io").StringIO()
    H.casa_data_step(c, node, "k", _rep(), "node1")          # decided: not asked again
    assert "kernel k: CASA's data in ~/.casa/data (auto-updated)" in c.ui.out.getvalue()
    c.st.section("environments")["e"].pop("casa_data_choice")
    c.ui.answers["casa_data"] = "p"
    H.casa_data_step(c, node, "k", _rep(), "node1")
    assert added == [{"casa_data": True}] and tested == ["k"]
    assert c.st.section("environments")["e"]["casa_data_choice"] == "private"


def test_no_data_gets_a_private_copy_in_setups_environment_only(ctx, release, monkeypatch):
    added = []
    monkeypatch.setattr(H, "add_to_environment", lambda c, node, rep, env, **kw: added.append(kw) or True)
    monkeypatch.setattr(H, "write_and_test", lambda *a: (True, "ok"))
    data = {"ready": False, "measurespath": "/home/jo/.casa/data",
            "why": "/home/jo/.casa/data does not exist"}
    c = ctx(via_login="n", user="jo")
    node = _node(c)
    _kernel(c, node, data)
    H.casa_data_step(c, node, "k", _rep(), "node1")
    assert added == [{"casa_data": True}]                       # the default when there is none
    assert "finds no CASA data: /home/jo/.casa/data does not exist" in " ".join(c.ui.out.getvalue().split())
    added.clear()
    c.ui.out = __import__("io").StringIO()
    c.st.section("environments")["e"]["marker"] = None
    H.casa_data_step(c, node, "k", _rep(), "node1")
    out = " ".join(c.ui.out.getvalue().split())
    assert added == [] and "Its environment is yours: install casadata there" in out


def test_a_private_copy_overridden_by_your_configuration_is_said(ctx, release, monkeypatch):
    prefix = "/opt/c/envs/cubevis-1.0.97"
    site = f"{prefix}/{H.capabilities.SITE_CONFIG_FILE}"
    c = ctx(via_login="n", user="jo")
    node = _node(c)
    _kernel(c, node, {"ready": True, "measurespath": f"{prefix}/lib/python3.12/site-packages/casadata/__data__",
                      "files": [site]}, casa_site_config=site)
    H.casa_data_step(c, node, "k", _rep(), "node1")
    assert "kernel k: CASA's data is the environment's own copy" in c.ui.out.getvalue()
    c.st.section("kernels")["k"]["casa_data"] = {
        "ready": True, "measurespath": "/home/jo/data", "files": [site, "/home/jo/.casa/config.py"]}
    H.casa_data_step(c, node, "k", _rep(), "node1")
    out = " ".join(c.ui.out.getvalue().split())
    assert "not the environment's own copy" in out and "~/.casa/config.py sets it" in out


def test_the_launcher_test_records_what_the_kernel_runs(ctx, monkeypatch):
    monkeypatch.setattr(H, "local_fingerprint", lambda c: "FP")
    monkeypatch.setattr(H.R, "install_launcher", lambda *a: "written")
    report = {"capabilities": {"visplot": False, "iclean": True}, "errors": {"visplot": "no xarray_ms"},
              "casa_data": {"ready": True, "measurespath": "/d"}}
    monkeypatch.setattr(H.R, "test_launcher", lambda c, a, path: ("FP", "/home/jo", report))
    c = ctx(via_login="n", user="jo")
    node = _node(c)
    _env(c, node)
    c.st.add_kernel("k", environment="e")
    ok, msg = H.write_and_test(c, node, "k", _rep(), "node1")
    assert ok and msg.endswith("runs interactive clean"), msg
    k = c.st.section("kernels")["k"]
    assert k["capabilities"] == report["capabilities"] and k["capability_errors"] == report["errors"]
    assert k["casa_data"] == report["casa_data"]


def test_a_rerun_offers_interactive_clean_and_tests_the_kernels_again(ctx, release, monkeypatch):
    """A host set up before 5b: its kernel's test says no interactive clean;
    setup offers to add it to its own environment, then tests again."""
    reports = iter([{"capabilities": {"visplot": True, "iclean": False}, "errors": {}, "casa_data": None},
                    {"capabilities": {"visplot": True, "iclean": True}, "errors": {},
                     "casa_data": {"ready": True, "measurespath": "/home/jo/.casa/data", "files": []}}])
    monkeypatch.setattr(H, "local_fingerprint", lambda c: "FP")
    monkeypatch.setattr(H.R, "install_launcher", lambda *a: "same")
    monkeypatch.setattr(H.R, "test_launcher", lambda c, a, path: ("FP", "/home/jo", next(reports)))
    monkeypatch.setattr(H, "use_kernel", lambda c, name, h: True)

    def add(c, node, rep, env, **kw):
        c.st.section("environments")[env]["iclean"] = True       # as the check after it says
        return True
    monkeypatch.setattr(H, "add_to_environment", add)
    c = ctx(via_login="n", user="jo")
    node = _node(c)
    _env(c, node)                                                # iclean: never checked
    c.st.add_kernel("k", environment="e")
    assert H.check_kernels(c, node, _rep(), "node1")
    k = c.st.section("kernels")["k"]
    assert k["capabilities"]["iclean"] is True and k["casa_data"]["ready"]
    out = " ".join(c.ui.out.getvalue().split())
    assert "Add interactive clean to it? [Y/n] ok kernel k: starts in ~; runs visplot, interactive clean" in out
    assert "finds CASA's data in ~/.casa/data" in out


# --------------------------------------------------------------------------
# python -m cubevis.setup kernel NAME: one kernel changed (2026-11-09)
# --------------------------------------------------------------------------

def _kernel_rig(ctx, monkeypatch, **answers):
    """A kernel 'k' on node1 (environment e), with no ssh: the logins pass, the
    node says where home is, and the launcher's test is recorded."""
    tested = []
    monkeypatch.setattr(H, "local_steps", lambda c: True)
    monkeypatch.setattr(H, "network_steps", lambda c, net, nodes=None, inside=True: True)
    monkeypatch.setattr(H, "discover_step", lambda c, node, paths=(), only=False, python3=None: _rep())
    monkeypatch.setattr(H, "write_and_test", lambda c, node, name, rep, h: tested.append(
        (node, name, dict(c.st.section("kernels")[name]))) or (True, "starts in ..."))
    c = ctx(via_login="n", user="jo", **answers)
    node = _node(c)
    _env(c, node)
    k = c.st.add_kernel("k", environment="e", workdir="/home/jo/old", env={"A": "1"})
    k["checked_on"] = node
    return c, node, tested


def test_a_kernels_working_directory_alone(ctx, release, monkeypatch):
    c, node, tested = _kernel_rig(ctx, monkeypatch)
    assert H.cmd_kernel(c, "k", workdir="~/casa/iclean") == 0
    k = c.st.section("kernels")["k"]
    assert k["workdir"] == "/home/jo/casa/iclean" and k["env"] == {"A": "1"}
    assert tested and tested[0][:2] == (node, "k") and tested[0][2]["workdir"] == "/home/jo/casa/iclean"
    c.ui.show_changes()
    out = " ".join(c.ui.out.getvalue().split())
    assert "changed kernel: k: starts in ~/casa/iclean (was ~/old)" in out
    # ~ alone is the home directory: no workdir kept
    assert H.cmd_kernel(c, "k", workdir="~") == 0 and c.st.section("kernels")["k"]["workdir"] is None


def test_variables_alone_and_nothing_to_change(ctx, release, monkeypatch):
    c, node, tested = _kernel_rig(ctx, monkeypatch)
    assert H.cmd_kernel(c, "k", env_vars="OMP_NUM_THREADS=8 B=2") == 0
    assert c.st.section("kernels")["k"]["env"] == {"OMP_NUM_THREADS": "8", "B": "2"}
    assert H.cmd_kernel(c, "k", env_vars="none") == 0 and c.st.section("kernels")["k"]["env"] == {}
    n = len(tested)
    assert H.cmd_kernel(c, "k", env_vars="none") == 0 and len(tested) == n       # not written again
    assert "nothing to change" in c.ui.out.getvalue()
    assert H.cmd_kernel(c, "k", env_vars="not a variable") == 1


def test_without_options_its_settings_are_asked(ctx, release, monkeypatch):
    c, node, tested = _kernel_rig(ctx, monkeypatch, workdir="/data/new", env_vars="none")
    assert H.cmd_kernel(c, "k") == 0
    k = c.st.section("kernels")["k"]
    assert (k["workdir"], k["env"]) == ("/data/new", {})
    assert "Working directory [~/old]" in c.ui.out.getvalue()


def test_the_clusters_defaults_can_follow(ctx, release, monkeypatch):
    c, node, tested = _kernel_rig(ctx, monkeypatch)
    c.st.add_cluster("cv", network=c.st.nodes()[node]["network"], pattern="node*")
    c.st.nodes()[node]["cluster"] = "cv"
    cl = c.st.section("clusters")["cv"]
    cl["workdir"], cl["env"] = "/home/jo/old", {"A": "1"}
    assert H.cmd_kernel(c, "k", workdir="/home/jo/new") == 0
    assert cl["workdir"] == "/home/jo/new" and cl["env"] == {"A": "1"}
    assert "Make its new settings cluster cv's defaults? [Y/n]" in c.ui.out.getvalue()
    # Defaults that were not this kernel's are left alone, unasked.
    cl["workdir"] = "/elsewhere"
    c.ui.out = __import__("io").StringIO()
    assert H.cmd_kernel(c, "k", workdir="/home/jo/newer") == 0
    assert cl["workdir"] == "/elsewhere" and "defaults" not in c.ui.out.getvalue()


def test_unknown_kernel_or_environment(ctx, release, monkeypatch):
    c, node, tested = _kernel_rig(ctx, monkeypatch)
    assert H.cmd_kernel(c, "nope", workdir="/x") == 1
    assert "there is no kernel 'nope' (kernels: k)" in c.ui.out.getvalue()
    assert H.cmd_kernel(c, "k", environment="nope") == 1
    assert "there is no environment 'nope'" in c.ui.out.getvalue()
    assert tested == []


def test_the_command_line(tmp_path, monkeypatch):
    from cubevis.setup import __main__ as M
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    seen = {}
    monkeypatch.setattr(H, "cmd_kernel", lambda c, name, **kw: seen.update(name=name, **kw) or 0)
    assert M.main(["kernel", "cvpost106-iclean", "--workdir", "~/casa/x"]) == 0
    assert seen == {"name": "cvpost106-iclean", "workdir": "~/casa/x", "env_vars": None,
                    "environment": None}


def _host_rig(ctx, monkeypatch, **answers):
    """cmd_host with the logins, discovery and the steps' insides stubbed:
    which steps run, with what."""
    calls = []
    c = ctx(via_login="n", user="jo", **answers)
    node = _node(c)
    if "e" not in c.st.section("environments"):
        _env(c, node)
    if "k" not in c.st.section("kernels"):
        c.st.add_kernel("k", environment="e")["checked_on"] = node
    for f in ("local_steps", "adopt_step", "record_facts", "cluster_step", "inside_step",
              "offer_cleanup"):
        monkeypatch.setattr(H, f, lambda *a, **k: True)
    monkeypatch.setattr(H, "locate", lambda c, host, port: node)
    monkeypatch.setattr(H, "hop_steps", lambda *a, **k: True)
    monkeypatch.setattr(H, "discover_step", lambda *a, **k: _rep())
    monkeypatch.setattr(H, "check_kernels", lambda c, node, rep, text: calls.append("check") or True)
    monkeypatch.setattr(H, "environment_step",
                        lambda c, node, rep, reuse: calls.append(("environment", reuse)) or "e")
    monkeypatch.setattr(H, "iclean_step", lambda c, node, env, rep: calls.append(("iclean", env)))
    monkeypatch.setattr(H, "kernel_step", lambda c, node, env, rep, name, text:
                        calls.append(("kernel", env, name)) or True)
    return c, node, calls


def test_a_host_set_up_before_offers_another_kernel(ctx, release, monkeypatch):
    """Darrell 2026-11-09: a rerun only checked the kernels; another one (another
    environment) needed --kernel NAME, which then took the environment in use
    without asking."""
    c, node, calls = _host_rig(ctx, monkeypatch)
    assert H.cmd_host(c, "node1.example.org") == 0
    assert calls == ["check"]                                     # the default: no
    assert "Add another kernel? [y/N]" in c.ui.out.getvalue()
    c, node, calls = _host_rig(ctx, monkeypatch, another_kernel="y")
    assert H.cmd_host(c, "node1.example.org") == 0
    assert calls == ["check", ("environment", False), ("iclean", "e"), ("kernel", "e", None)]


def test_kernel_option_chooses_a_new_kernels_environment(ctx, release, monkeypatch):
    c, node, calls = _host_rig(ctx, monkeypatch)
    assert H.cmd_host(c, "node1.example.org", kernel="other") == 0
    assert calls == [("environment", False), ("iclean", "e"), ("kernel", "e", "other")]
    # A kernel there is: its own environment, its settings asked again.
    c, node, calls = _host_rig(ctx, monkeypatch)
    assert H.cmd_host(c, "node1.example.org", kernel="k") == 0
    assert calls == [("iclean", "e"), ("kernel", "e", "k")]


def test_help_lists_every_command():
    from cubevis.setup import __main__ as M
    import contextlib
    import io as _io
    out = _io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit):
        M.main(["--help"])
    text = out.getvalue()
    for line in ("python -m cubevis.setup kernel NAME [--workdir DIR] [--env VARS] [--environment ENV]",
                 "python -m cubevis.setup HOST --kernel NAME", "python -m cubevis.setup status"):
        assert line in text, text
