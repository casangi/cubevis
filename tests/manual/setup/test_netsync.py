"""The GUI side reading node files (remote session design 7b, stage 2b part
iii): parsing what a node prints, and recording nodes, environments and
kernels in the state.

    pytest tests/manual/setup/test_netsync.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from cubevis.setup import netsync as N
from cubevis.setup import state as S

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


@pytest.fixture(autouse=True)
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CUBEVIS_HOST_ID", "gui1")
    return tmp_path / "xdg" / "cubevis"


def nodefile(host, hostname, envs, shared=True, home="/users/jo", ranges=("10.7.7.0/24",)):
    return {"schema": 2,
            "node": {"network": "cv", "host": host, "hostname": hostname, "home": home,
                     "shared_home": shared, "ranges": list(ranges), "updated": "x"},
            "environments": envs}


def env(python, status="ok", **kw):
    e = {"kind": "conda", "python": python, "prefix": os.path.dirname(os.path.dirname(python)),
         "status": status, "versions": {"cubevis": "1.0.97"}, "last_error": None,
         "launcher": f"/users/jo/.config/cubevis/hosts/x/node/cv/{os.path.basename(python)}.sh",
         "workdir": "/users/jo/data", "env": {"OMP_NUM_THREADS": "4"},
         "cubevis_via": {"method": "pip", "source": None}}
    e.update(kw)
    return e


def _state():
    st = S.load()
    st.add_network("cv", login_host="login.cv.example.org", user="jo", domain="cv.example.org")
    st.add_node("zuul06", network="cv")
    return st


def test_parse_dump():
    data = nodefile("zuul06", "zuul06.cv.example.org", {})
    text = (f"{N.HOST}zuul06.cv.example.org\n{N.BEGIN}/u/a.json\n{json.dumps(data, indent=2)}\n\n"
            f"{N.BEGIN}/u/b.json\n{{broken\n{N.BEGIN}/u/c.json\n[1]\n{N.END}\n")
    host, files, errors = N.parse_dump(text)
    assert host == "zuul06.cv.example.org"
    assert [p for p, _ in files] == ["/u/a.json"] and files[0][1] == data
    assert len(errors) == 2 and "/u/b.json" in errors[0] and "not a node file" in errors[1]
    assert "cv.json" in N.remote_script("cv") and N.remote_command("cv").startswith("sh -c '")
    with pytest.raises(S.StateError):
        N.remote_script("bad name")


def test_the_remote_script_finds_the_files(tmp_path, xdg):
    d = tmp_path / "home" / ".config" / "cubevis" / "hosts" / "n1" / "node"
    d.mkdir(parents=True)
    (d / "cv.json").write_text(json.dumps(nodefile("n1", "n1.x", {})))
    r = subprocess.run(["sh", "-c", N.remote_script("cv")], capture_output=True, text=True,
                       env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"})
    host, files, errors = N.parse_dump(r.stdout)
    assert host and len(files) == 1 and not errors


def test_a_shared_environment_is_one_record_on_every_node():
    st = _state()
    py = "/users/jo/mf/envs/hrs/bin/python"
    files = [("a", nodefile("zuul06", "zuul06.cv.example.org", {"hrs": env(py)})),
             ("b", nodefile("zuul07", "zuul07.cv.example.org", {"hrs": env(py)}))]
    changes = N.sync(st, "cv", files)
    assert ("added node", "zuul07 (zuul07.cv.example.org, from its node file)") in changes
    e = st.section("environments")["hrs"]
    assert e["hosts"] == "all" and set(e["on"]) == {"zuul06", "zuul07"}
    assert e["workdir"] == "/users/jo/data" and e["env"] == {"OMP_NUM_THREADS": "4"}
    assert e["cubevis"] == "1.0.97" and e["status"] == "ok" and e["kind"] == "conda"
    assert st.section("kernels")["hrs"] == {"environment": "hrs", "launch_timeout": 120}
    assert N.suggested_ranges(st, "cv") == ["10.7.7.0/24"]
    assert N.sync(st, "cv", files) == []                     # again: nothing new


def test_environments_outside_a_shared_home_are_per_node_and_names_do_not_clash():
    st = _state()
    st.add_node("zuul07", network="cv")
    files = [("a", nodefile("zuul06", "zuul06.cv.example.org",
                            {"scratch": env("/scratch/zuul06/env/bin/python")}, shared=False)),
             ("b", nodefile("zuul07", "zuul07.cv.example.org",
                            {"scratch": env("/scratch/zuul07/env/bin/python")}, shared=False))]
    N.sync(st, "cv", files)
    envs = st.section("environments")
    assert envs["scratch"]["hosts"] == ["zuul06"] and envs["scratch-zuul07"]["hosts"] == ["zuul07"]
    assert set(st.section("kernels")) == {"scratch", "scratch-zuul07"}
    # The same path on another node later, without a shared home: added there.
    files.append(("c", nodefile("zuul08", "zuul08.cv.example.org",
                                {"s": env("/scratch/zuul06/env/bin/python")}, shared=False)))
    N.sync(st, "cv", files)
    assert envs["scratch"]["hosts"] == ["zuul06", "zuul08"]


def test_the_node_reached_is_matched_by_its_host_name():
    st = S.load()
    st.add_network("lan", login_host=None, user="alice", domain="example.test")
    st.add_node("compute", network="lan")
    f = nodefile("vm", "vm", {"k": env("/opt/k/bin/python")}, shared=False)
    f["node"]["network"] = "lan"
    N.sync(st, "lan", [("x", f)], reached={"vm": "compute"})
    assert set(st.nodes("lan")) == {"compute"}
    assert st.section("environments")["k"]["hosts"] == ["compute"]


def test_an_environment_no_longer_offered_is_flagged_not_removed():
    st = _state()
    py = "/users/jo/mf/envs/hrs/bin/python"
    N.sync(st, "cv", [("a", nodefile("zuul06", "zuul06.cv.example.org", {"hrs": env(py)}))])
    changes = N.sync(st, "cv", [("a", nodefile("zuul06", "zuul06.cv.example.org", {}))])
    assert ("flagged environment", "hrs: no longer enabled on zuul06") in changes
    e = st.section("environments")["hrs"]
    assert e["status"] == "flagged" and e["on"]["zuul06"]["status"] == "gone"
    assert "hrs" in st.section("kernels")


def test_a_failed_check_on_the_node_shows_on_the_gui_side():
    st = _state()
    N.sync(st, "cv", [("a", nodefile("zuul06", "zuul06.cv.example.org",
                                     {"bad": env("/x/bin/python", status="flagged",
                                                 last_error="cubevis is not installed in it")}))])
    e = st.section("environments")["bad"]
    assert e["status"] == "flagged" and e["last_error"] == "cubevis is not installed in it"


def test_network_reads_the_files_of_a_shared_home(tmp_path, xdg):
    """The GUI shares the home with the nodes: the files are read here, and
    the nodes they name are added (their logins then fail here: no such hosts)."""
    for h in ("n1", "n2"):
        d = xdg / "hosts" / h / "node"
        d.mkdir(parents=True)
        f = nodefile(h, f"{h}.example.invalid", {"hrs": env("/users/jo/mf/envs/hrs/bin/python")})
        (d / "cv.json").write_text(json.dumps(f))
    env_ = dict(os.environ, PYTHONPATH=ROOT, NO_COLOR="1", CUBEVIS_RUNTIME_DIR=str(tmp_path / "run"))
    r = subprocess.run([sys.executable, "-m", "cubevis.setup", "--yes", "--answer", "via_login=n",
                        "--answer", "user=jo", "--answer", "domain=example.invalid",
                        "--answer", "network=cv", "--answer", "nodes=", "network"],
                       capture_output=True, text=True, env=env_, timeout=300, stdin=subprocess.DEVNULL)
    out = " ".join(r.stdout.split())
    assert "read 2 node file(s) here (a home shared with the nodes)" in out, r.stdout
    st = S.load()
    assert set(st.nodes("cv")) == {"n1", "n2"}
    assert st.section("environments")["hrs"]["hosts"] == "all"
    assert "kernel hrs" in subprocess.run([sys.executable, "-m", "cubevis.setup", "status"],
                                          capture_output=True, text=True, env=env_).stdout
