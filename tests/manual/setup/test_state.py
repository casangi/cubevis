"""Tests for cubevis.setup.state (remote session design, stage 1; the
shared-directory layout, merging and locking: ``test_state_shared.py``).

    pytest tests/manual/setup/test_state.py

Standard library only; every test works in its own XDG_CONFIG_HOME.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from cubevis.setup import config_dir, data_file
from cubevis.setup import state as S


@pytest.fixture(autouse=True)
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CUBEVIS_HOST_ID", "here")
    return tmp_path / "xdg"


def _net(st, name="cv", **kw):
    args = dict(login_host="login.example.org", user="jo", login_password=True,
                password=True, domain="cv.example.org")
    args.update(kw)
    return st.add_network(name, **args)


def test_paths_follow_xdg(xdg, monkeypatch):
    assert config_dir() == xdg / "cubevis"
    assert S.state_path() == xdg / "cubevis" / "state.json"
    assert S.host_state_path() == xdg / "cubevis" / "hosts" / "here" / "local.json"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", "/home/someone")
    assert config_dir() == S.Path("/home/someone/.config/cubevis")


def test_data_files_are_in_the_package():
    for name in ("ssh", "probe.sh", "remote-setup.sh"):
        assert data_file(name).is_file()
    assert os.access(data_file("ssh"), os.X_OK)
    with pytest.raises(FileNotFoundError):
        data_file("nope")


def test_missing_file_is_empty_state():
    st = S.load()
    assert st.data == S.empty()
    assert not S.state_path().exists()          # loading writes nothing


def test_save_is_atomic_private_and_round_trips():
    st = S.load()
    _net(st)
    st.add_node("node140", network="cv")
    p = S.save(st)
    assert p == S.state_path()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    assert not [f for f in p.parent.iterdir() if f.name.startswith(".state.")]
    back = S.load()
    assert back.data == st.data
    assert json.loads(p.read_text())["schema"] == S.SCHEMA


def test_save_failure_leaves_old_file(monkeypatch):
    st = S.load()
    _net(st)
    S.save(st)
    before = S.state_path().read_text()
    st.add_node("n1", network="cv")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(S.os, "replace", boom)
    with pytest.raises(OSError):
        S.save(st)
    assert S.state_path().read_text() == before
    assert not [f for f in S.state_path().parent.iterdir() if f.name.startswith(".state.")]


def test_bad_files_are_refused_not_replaced():
    p = S.state_path()
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    with pytest.raises(S.StateError, match="cannot be read"):
        S.load()
    p.write_text(json.dumps({"networks": {}}))
    with pytest.raises(S.StateError, match="no schema"):
        S.load()
    p.write_text(json.dumps({"schema": S.SCHEMA + 1}))
    with pytest.raises(S.StateError, match="newer than this cubevis"):
        S.load()
    assert p.read_text() == json.dumps({"schema": S.SCHEMA + 1})


def test_migration_hook(monkeypatch):
    """A schema-0 file goes through MIGRATIONS[0]."""
    monkeypatch.setattr(S, "SCHEMA", 1)
    monkeypatch.setitem(S.MIGRATIONS, 0, lambda d: {**d, "networks": {"old": {"x": 1}}})
    st = S.State({"schema": 0})
    assert st.data["schema"] == 1 and st.networks() == {"old": {"x": 1}}
    assert all(k in st.data for k in S.SHARED_SECTIONS)


def test_network_defaults():
    st = S.load()
    net = _net(st)
    assert net["login"] == {"host": "login.example.org", "user": "jo", "port": 22,
                            "alias": "cv-login", "password": True, "service": "cubevis-ssh"}
    assert net["node_pattern"] == "*.cv.example.org"
    assert net["service"] == "cubevis-ssh" and net["shared_fs"] is True
    with pytest.raises(S.StateError, match="exists already"):
        _net(st)
    _net(st, replace=True, login_user="jlogin", login_port=2201)
    assert st.networks()["cv"]["login"]["user"] == "jlogin"
    assert st.networks()["cv"]["login"]["port"] == 2201


def test_names_and_aliases_are_checked():
    st = S.load()
    for bad in ("", "-x", "a b", "a/b", "ü"):
        with pytest.raises(S.StateError):
            _net(st, name=bad)
    with pytest.raises(S.StateError, match="must differ"):
        _net(st, login_alias="login.example.org")
    _net(st)
    with pytest.raises(S.StateError, match="must differ"):
        st.add_node("n1", network="cv", alias="n1.cv.example.org")
    st.add_node("n1", network="cv")
    with pytest.raises(S.StateError, match="already used"):
        st.add_node("n2", network="cv", alias="cv-n1")
    with pytest.raises(S.StateError, match="already used"):
        st.add_node("n3", network="cv", alias="cv-login")
    with pytest.raises(S.StateError, match="no network 'nope'"):
        st.add_node("n4", network="nope")
    with pytest.raises(S.StateError, match="query"):
        _net(st, name="q", query={"host": "head1"})


def test_nodes_complete_names_and_default():
    st = S.load()
    _net(st)
    n = st.add_node("node140", network="cv")
    assert n["hostname"] == "node140.cv.example.org" and n["alias"] == "cv-node140"
    assert st.networks()["cv"]["default_node"] == "node140"
    st.add_node("node141", network="cv", hostname="other.example.com", port=2202)
    assert st.nodes()["node141"]["port"] == 2202
    assert set(st.nodes("cv")) == {"node140", "node141"}
    st.remove_node("node140")
    assert st.networks()["cv"]["default_node"] == "node141"
    _net(st, name="nodomain", domain=None)
    assert st.add_node("x1", network="nodomain")["hostname"] == "x1"
    assert st.networks()["nodomain"]["node_pattern"] is None


def test_environments_kernels_and_removal_rules():
    st = S.load()
    _net(st)
    st.add_node("n1", network="cv")
    st.add_environment("cv-main", network="cv", python="/home/jo/mf/envs/cubevis/bin/python",
                       kind="conda", prefix="/home/jo/mf/envs/cubevis", tool="/home/jo/mf/bin/conda")
    st.add_kernel("hrs", environment="cv-main")
    assert st.kernel_network("hrs") == "cv"
    with pytest.raises(S.StateError, match="no environment"):
        st.add_kernel("k2", environment="nope")
    with pytest.raises(S.StateError, match="still used"):
        st.remove_network("cv")
    st.section("sessions")["b3f1"] = {"kernel": "hrs", "node": "n1"}
    with pytest.raises(S.StateError, match="recorded sessions"):
        st.remove_node("n1")


def test_flag_and_mark_ok():
    st = S.load()
    _net(st)
    st.add_node("n1", network="cv")
    st.add_environment("e", network="cv", python="/p")
    st.flag("nodes", "n1", "Could not resolve hostname")
    r = st.nodes()["n1"]
    assert r["status"] == "flagged" and "resolve" in r["last_error"] and r["last_checked"]
    assert [(k, n) for k, n, _ in st.flagged()] == [("nodes", "n1")]
    assert "n1" in st.nodes()                    # flagged, not deleted
    st.mark_ok("environments", "e", cubevis="1.0.97", checked_on="n1")
    e = st.section("environments")["e"]
    assert e["status"] == "ok" and e["cubevis"] == "1.0.97" and e["last_error"] is None
    st.mark_ok("nodes", "n1")
    assert st.flagged() == []
    with pytest.raises(S.StateError, match="only nodes"):
        st.flag("networks", "cv", "y")


def test_environment_identity_hosts_and_one_network():
    st = S.load()
    _net(st)
    _net(st, name="nm", login_host="login.nm.example.org", domain="nm.example.org")
    st.add_node("n1", network="cv")
    st.add_node("n2", network="cv")
    st.add_node("m1", network="nm")
    py = "/home/jo/mf/envs/cubevis/bin/python"
    st.add_environment("main", network="cv", python=py, kind="mamba")   # read as conda
    assert st.section("environments")["main"]["kind"] == "conda"
    assert st.find_environment("cv", py) == "main" and st.find_environment("nm", py) is None
    with pytest.raises(S.StateError, match="is environment 'main' of cv already"):
        st.add_environment("again", network="cv", python=py)
    # The same path seen from another network is another environment.
    st.add_environment("main-nm", network="nm", python=py)
    assert st.environment_on("main", "n2") and not st.environment_on("main", "m1")
    st.add_environment("only1", network="cv", python="/scratch/n1/env/bin/python", hosts=["n1"])
    assert st.environment_on("only1", "n1") and not st.environment_on("only1", "n2")
    with pytest.raises(S.StateError, match="belongs to one network"):
        st.add_environment("mixed", network="cv", python="/x/bin/python", hosts=["n1", "m1"])
    with pytest.raises(S.StateError, match="kind"):
        st.add_environment("k", network="cv", python="/y/bin/python", kind="pixi")
    with pytest.raises(S.StateError, match="absolute"):
        st.add_environment("rel", network="cv", python="bin/python")


def test_clusters_their_nodes_and_their_environments():
    """A cluster: nodes named alike in one network; a node matching its
    pattern is added without questions (``node_for``) and sees the
    cluster's environments; a node outside it does not."""
    st = S.load()
    _net(st)
    st.add_cluster("cvpost", network="cv", pattern="cvpost*")
    st.add_node("cvpost140", network="cv", cluster="cvpost")
    st.add_node("zuul06", network="cv")
    assert st.cluster_for("cvpost141") == "cvpost"
    assert st.cluster_for("cvpost141.cv.example.org") == "cvpost"
    assert st.cluster_for("zuul06") is None
    assert st.find_node("cvpost140.cv.example.org") == "cvpost140"
    assert st.find_node("cv-cvpost140") == "cvpost140" and st.find_node("nope") is None
    n = st.node_for("cvpost141")
    assert n == "cvpost141" and st.nodes()[n]["cluster"] == "cvpost"
    assert st.nodes()[n]["hostname"] == "cvpost141.cv.example.org"
    assert st.node_for("cvpost141") == "cvpost141"          # known now: not added twice
    assert st.node_for("elsewhere") is None
    st.add_environment("e1", network="cv", python="/lustre/cv/mf/envs/c/bin/python",
                       cluster="cvpost", hosts=[])
    assert st.environment_on("e1", "cvpost141") and not st.environment_on("e1", "zuul06")
    st.add_kernel("tw-hya", environment="e1", workdir="/lustre/jo/tw", env={"A": "1"},
                  launcher="/home/jo/.config/cubevis/launch/gui1/tw-hya.sh")
    assert st.kernels_on("cvpost141") == ["tw-hya"] and st.kernels_on("zuul06") == []
    k = st.section("kernels")["tw-hya"]
    assert k["status"] == "new" and k["env"] == {"A": "1"}
    st.flag("kernels", "tw-hya", "it failed")
    assert st.section("kernels")["tw-hya"]["status"] == "flagged"
    with pytest.raises(S.StateError, match="absolute"):
        st.add_kernel("rel", environment="e1", workdir="data")
    with pytest.raises(S.StateError, match="node pattern"):
        st.add_cluster("bad", network="cv", pattern="*")
    with pytest.raises(S.StateError, match="exists already"):
        st.add_cluster("cvpost", network="cv", pattern="x*")
    _net(st, name="nm", login_host="login.nm.example.org", domain="nm.example.org")
    with pytest.raises(S.StateError, match="is in network 'cv'"):
        st.add_node("m1", network="nm", cluster="cvpost")
    with pytest.raises(S.StateError, match="still used"):
        st.remove_network("cv")
    S.save(st)
    assert S.load().section("clusters") == {"cvpost": {"network": "cv", "pattern": "cvpost*"}}


def test_schema_2_kernels_take_their_environments_settings(xdg):
    """Stage 2b part iii kept the working directory and variables per
    environment, with launchers per node in ``on``: in schema 3 they are
    the kernel's, which has no launcher until setup HOST writes one."""
    v2 = {"schema": 2,
          "networks": {"cv": {"login": None, "user": "jo", "domain": "cv.example.org"}},
          "nodes": {"n1": {"network": "cv", "hostname": "n1.cv.example.org", "alias": "cv-n1"}},
          "environments": {"hrs": {"network": "cv", "python": "/e/bin/python", "kind": "conda",
                                   "hosts": ["n1"], "workdir": "/data", "env": {"X": "1"},
                                   "on": {"n1": {"launcher": "/old.sh"}}}},
          "kernels": {"hrs": {"environment": "hrs", "launch_timeout": 120}}}
    (xdg / "cubevis").mkdir(parents=True)
    (xdg / "cubevis" / "state.json").write_text(json.dumps(v2))
    st = S.load()
    k = st.section("kernels")["hrs"]
    assert k["workdir"] == "/data" and k["env"] == {"X": "1"} and k["launcher"] is None
    assert "setup HOST again" in k["last_error"]
    e = st.section("environments")["hrs"]
    assert "on" not in e and "workdir" not in e and e["cluster"] is None
    assert st.section("clusters") == {}
    S.save(st)
    assert json.loads((xdg / "cubevis" / "state.json").read_text())["schema"] == S.SCHEMA == 3


def test_a_node_added_by_a_cluster_pattern_takes_the_port_its_nodes_share():
    st = S.load()
    _net(st)
    st.add_cluster("c", network="cv", pattern="c*")
    st.add_node("c1", network="cv", cluster="c", port=2202)
    assert st.nodes()[st.node_for("c2")]["port"] == 2202
    st.add_cluster("d", network="cv", pattern="d*")
    st.add_node("d1", network="cv", cluster="d")
    assert "port" not in st.nodes()[st.node_for("d2")]
