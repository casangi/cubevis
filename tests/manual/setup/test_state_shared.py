"""A configuration directory shared by several computers (remote session
design, section 7b): this computer's own file, the schema 1 -> 2
migration, merging saves, the lock, and the socket directory.

    pytest tests/manual/setup/test_state_shared.py

Standard library only.  Computers are played with ``CUBEVIS_HOST_ID``.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import socket
import stat
import time

import pytest

from cubevis import setup as SETUP
from cubevis.setup import state as S


@pytest.fixture(autouse=True)
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CUBEVIS_HOST_ID", "gui1")
    return tmp_path / "xdg" / "cubevis"


def _net(st, name="cv"):
    return st.add_network(name, login_host="login.example.org", user="jo", password=True,
                          domain="cv.example.org")


# -- this computer ---------------------------------------------------------

def test_host_id(monkeypatch):
    monkeypatch.setenv("CUBEVIS_HOST_ID", "Zuul06.CV.nrao.edu")
    assert SETUP.host_id() == "zuul06"
    monkeypatch.setenv("CUBEVIS_HOST_ID", "my mac!")
    assert SETUP.host_id() == "my-mac"
    monkeypatch.delenv("CUBEVIS_HOST_ID")
    SETUP._host_id_cache.clear()
    hid = SETUP.host_id()
    assert hid and hid == SETUP._clean_id(socket.gethostname()) or os.uname().sysname == "Darwin"


def test_local_and_sessions_are_this_computers(xdg):
    st = S.load()
    _net(st)
    st.local["store"] = "pass"
    st.section("sessions")["s1"] = {"kernel": "k", "node": "n"}
    S.save(st)
    shared = json.loads((xdg / "state.json").read_text())
    assert "local" not in shared and "sessions" not in shared and "cv" in shared["networks"]
    mine = json.loads((xdg / "hosts" / "gui1" / "local.json").read_text())
    assert mine["local"]["store"] == "pass" and "s1" in mine["sessions"]
    assert stat.S_IMODE((xdg / "hosts" / "gui1" / "local.json").stat().st_mode) == 0o600


def test_another_computer_sees_the_networks_not_the_local(xdg, monkeypatch):
    st = S.load()
    _net(st)
    st.local["store"] = "pass"
    S.save(st)
    monkeypatch.setenv("CUBEVIS_HOST_ID", "gui2")
    other = S.load()
    assert "cv" in other.networks() and other.local == {}
    other.local["store"] = "keychain"
    S.save(other)
    monkeypatch.setenv("CUBEVIS_HOST_ID", "gui1")
    assert S.load().local["store"] == "pass"


# -- schema 1 ----------------------------------------------------------------

V1 = {"schema": 1, "local": {"python": "/old/python", "store": "pass"},
      "networks": {"cv": {"login": None, "user": "jo", "password": False, "domain": None,
                          "node_pattern": None, "shared_fs": True, "default_node": None,
                          "query": None, "inside": None, "service": None}},
      "nodes": {}, "environments": {}, "kernels": {},
      "sessions": {"s1": {"kernel": "k", "node": "n"}}}


def test_schema_1_moves_to_this_computer_at_the_first_save(xdg, monkeypatch):
    xdg.mkdir(parents=True)
    (xdg / "state.json").write_text(json.dumps(V1))
    st = S.load()
    assert st.local["store"] == "pass" and "s1" in st.section("sessions")
    assert "cv" in st.networks()
    assert json.loads((xdg / "state.json").read_text())["schema"] == 1   # load wrote nothing
    assert not (xdg / "hosts").exists()
    S.save(st)
    shared = json.loads((xdg / "state.json").read_text())
    assert shared["schema"] == 2 and "local" not in shared and "cv" in shared["networks"]
    mine = json.loads((xdg / "hosts" / "gui1" / "local.json").read_text())
    assert mine["local"]["python"] == "/old/python"
    # A second computer sharing the directory starts with its own, empty.
    monkeypatch.setenv("CUBEVIS_HOST_ID", "gui2")
    assert S.load().local == {}


def test_a_newer_file_is_refused(xdg):
    (xdg / "hosts" / "gui1").mkdir(parents=True)
    (xdg / "hosts" / "gui1" / "local.json").write_text(json.dumps({"schema": S.SCHEMA + 1}))
    with pytest.raises(S.StateError, match="newer than this cubevis"):
        S.load()


# -- merging saves -----------------------------------------------------------

def _two_runs():
    st = S.load()
    if "cv" not in st.networks():
        _net(st)
    if "n0" not in st.nodes():
        st.add_node("n0", network="cv")
    S.save(st)
    return S.load(), S.load()


def test_two_runs_adding_different_nodes_keep_both():
    a, b = _two_runs()
    a.add_node("n1", network="cv")
    b.add_node("n2", network="cv")
    b.networks()["cv"]["shared_fs"] = False
    net_b = b.networks()["cv"]
    S.save(a)
    S.save(b)
    back = S.load()
    assert set(back.nodes()) == {"n0", "n1", "n2"}
    assert back.networks()["cv"]["shared_fs"] is False
    # b now holds the merged state, in the dicts it held before.
    assert "n1" in b.nodes() and b.networks()["cv"] is net_b
    net_b["default_node"] = "n2"
    S.save(b)
    assert S.load().networks()["cv"]["default_node"] == "n2"


def test_a_field_both_changed_takes_this_runs_value_other_fields_kept():
    a, b = _two_runs()
    a.networks()["cv"]["user"] = "ann"
    a.networks()["cv"]["domain"] = "a.example.org"
    b.networks()["cv"]["user"] = "bob"
    S.save(a)
    S.save(b)
    net = S.load().networks()["cv"]
    assert net["user"] == "bob" and net["domain"] == "a.example.org"


def test_a_removal_beats_a_check_but_not_an_edit():
    a, b = _two_runs()
    a.remove_node("n0")
    b.flag("nodes", "n0", "no route")            # health only
    S.save(a)
    S.save(b)
    assert "n0" not in S.load().nodes()

    a, b = _two_runs()                            # n0 again
    b.flag("nodes", "n0", "no route")
    S.save(b)
    a.remove_node("n0")
    S.save(a)
    assert "n0" not in S.load().nodes()

    a, b = _two_runs()
    a.remove_node("n0")
    b.nodes()["n0"]["port"] = 2202                # a real change: kept
    S.save(a)
    S.save(b)
    assert S.load().nodes()["n0"]["port"] == 2202


def test_unchanged_files_are_not_rewritten(xdg):
    st = S.load()
    _net(st)
    S.save(st)
    m = (xdg / "state.json").stat().st_mtime_ns
    time.sleep(0.01)
    S.save(st)
    assert (xdg / "state.json").stat().st_mtime_ns == m


def _adder(args):
    xdg, host, i = args
    os.environ["XDG_CONFIG_HOME"] = xdg
    os.environ["CUBEVIS_HOST_ID"] = host
    for k in range(5):
        st = S.load()
        st.add_node(f"{host}-{i}-{k}", network="cv")
        st.local[f"k{i}{k}"] = 1
        S.save(st)


def test_many_processes_saving_at_once_lose_nothing(xdg):
    st = S.load()
    _net(st)
    S.save(st)
    jobs = [(str(xdg.parent), f"h{i % 2}", i) for i in range(8)]
    with multiprocessing.get_context("spawn").Pool(8) as pool:
        pool.map(_adder, jobs)
    nodes = S.load().nodes()
    assert len(nodes) == 40, sorted(nodes)
    for h in ("h0", "h1"):
        loc = json.loads((xdg / "hosts" / h / "local.json").read_text())["local"]
        assert len(loc) == 20
    assert not list(xdg.glob("*.lock")) and not list(xdg.glob("hosts/*/*.lock"))


# -- the lock ----------------------------------------------------------------

def test_a_held_lock_times_out_with_who_holds_it(xdg):
    xdg.mkdir(parents=True)
    lock = xdg / "state.json.lock"
    lock.write_text(f"{socket.gethostname()} {os.getpid()} {time.time():.0f}\n")
    with pytest.raises(S.StateError, match="locked by another setup run"):
        with S.FileLock(xdg / "state.json", timeout=0.3):
            pass
    assert lock.exists()                         # a live run's lock is left alone


def _nothing():
    pass


def test_stale_locks_are_broken(xdg):
    xdg.mkdir(parents=True)
    lock = xdg / "state.json.lock"
    p = multiprocessing.get_context("spawn").Process(target=_nothing)
    p.start()
    p.join()
    lock.write_text(f"{socket.gethostname()} {p.pid} 0\n")       # its process has ended
    with S.FileLock(xdg / "state.json", timeout=1):
        pass
    lock.write_text("elsewhere 1 0\n")                            # another computer's, old
    old = time.time() - S.LOCK_STALE - 10
    os.utime(lock, (old, old))
    with S.FileLock(xdg / "state.json", timeout=1):
        pass
    assert not lock.exists()


# -- sockets -----------------------------------------------------------------

def test_runtime_dir_is_private_and_checked(tmp_path, monkeypatch):
    d = tmp_path / "run"
    monkeypatch.setenv("CUBEVIS_RUNTIME_DIR", str(d))
    assert SETUP.runtime_dir() == d and stat.S_IMODE(d.stat().st_mode) == 0o700
    os.chmod(d, 0o777)
    SETUP.runtime_dir()
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    f = tmp_path / "file"
    f.write_text("")
    monkeypatch.setenv("CUBEVIS_RUNTIME_DIR", str(f))
    with pytest.raises(SETUP.RuntimeDirError, match="not a directory"):
        SETUP.runtime_dir()
    if os.getuid() == 0:                          # can give it away
        o = tmp_path / "theirs"
        o.mkdir()
        os.chown(o, 12345, 12345)
        monkeypatch.setenv("CUBEVIS_RUNTIME_DIR", str(o))
        with pytest.raises(SETUP.RuntimeDirError, match="belongs to someone else"):
            SETUP.runtime_dir()


def test_default_runtime_dir_is_on_local_disk(monkeypatch):
    monkeypatch.delenv("CUBEVIS_RUNTIME_DIR", raising=False)
    assert str(SETUP.runtime_path()) == f"/tmp/cubevis-{os.getuid()}"
    from cubevis.setup import sshgen as G
    assert str(G.cm_dir()).startswith("/tmp/") and "xdg" not in str(G.cm_dir())
