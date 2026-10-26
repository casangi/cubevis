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
