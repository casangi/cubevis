"""The node side (remote session design 7b, stage 2b part ii): environments
found, classified and checked; the node file; ``python -m cubevis.setup
node-here``.

    pytest tests/manual/setup/test_node_here.py
    CUBEVIS_TEST_KERNEL_PYTHON=/path/to/python pytest ...   # + working environments

Environments are small venvs made here (``--without-pip``); a conda one
is a venv with a ``conda-meta`` directory, listed by a fake ``conda`` on
PATH.  An environment that can run kernels needs cubevis, ipykernel and
bokeh: with ``CUBEVIS_TEST_KERNEL_PYTHON`` naming a Python that has them,
the test environments see its packages through a ``.pth`` file;
without it those tests are skipped.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from cubevis.setup import envs, hostinfo, nodefile
from cubevis.setup import state as S

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
KERNEL_PY = os.environ.get("CUBEVIS_TEST_KERNEL_PYTHON")
needs_kernel_py = pytest.mark.skipif(not KERNEL_PY, reason="set CUBEVIS_TEST_KERNEL_PYTHON to a "
                                     "Python with cubevis, ipykernel and bokeh")


def _kernel_paths() -> list:
    r = subprocess.run([KERNEL_PY, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                       capture_output=True, text=True, check=True)
    return [ROOT, r.stdout.strip()]


def make_env(prefix, kind="venv", kernel=False):
    # From the kernel Python when there is one: its packages are seen through
    # the .pth file, so the versions must match.
    subprocess.run([KERNEL_PY or sys.executable, "-m", "venv", "--without-pip", str(prefix)],
                   check=True)
    if kind in ("conda", "mamba"):
        os.makedirs(os.path.join(prefix, "conda-meta"))
    if kernel:
        site = subprocess.run([os.path.join(prefix, "bin", "python"), "-c",
                               "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                              capture_output=True, text=True, check=True).stdout.strip()
        with open(os.path.join(site, "kernel.pth"), "w") as f:
            f.write("\n".join(_kernel_paths()) + "\n")
    return str(prefix)


def fake_tool(bindir, name, prefixes):
    os.makedirs(bindir, exist_ok=True)
    p = os.path.join(bindir, name)
    with open(p, "w") as f:
        f.write("#!/bin/sh\ncat <<'EOF'\n" + json.dumps({"envs": [str(x) for x in prefixes]})
                + "\nEOF\n")
    os.chmod(p, 0o755)
    return p


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A home of its own (so ~/miniforge3 and friends are not found), a
    config directory, and this computer as zuul06."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("CUBEVIS_HOST_ID", "zuul06")
    for v in ("CONDA_EXE", "MAMBA_EXE", "VIRTUAL_ENV", "CONDA_PREFIX"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("PATH", f"{tmp_path}/bin:/usr/bin:/bin")
    return tmp_path


# -- what a node knows about itself ------------------------------------------

def test_ranges_and_shared_filesystems():
    assert hostinfo.ranges_of(["10.7.1.20/24", "10.7.1.21/24", "fd00::5/64", "10.8.0.3/16"]) == \
        ["10.7.1.0/24", "10.8.0.0/16", "fd00::/64"]
    assert hostinfo.shared("nfs4") and hostinfo.shared("lustre") and hostinfo.shared("NFS")
    assert hostinfo.shared("ext4") is False and hostinfo.shared(None) is None
    assert hostinfo.domain_of("zuul06.cv.nrao.edu") == "cv.nrao.edu"
    assert hostinfo.domain_of("zuul06") is None


def test_interfaces_parsing(monkeypatch):
    ip = ("1: lo    inet 127.0.0.1/8 scope host lo\n"
          "2: eth0    inet 10.7.1.20/24 brd 10.7.1.255 scope global eth0\n"
          "2: eth0    inet6 fe80::1/64 scope link\n"
          "3: docker0    inet 172.17.0.1/16 scope global docker0\n"
          "4: ib0    inet6 fd00::5/64 scope global\n")
    monkeypatch.setattr(hostinfo.platform, "system", lambda: "Linux")
    monkeypatch.setattr(hostinfo, "_run", lambda cmd: ip if cmd[0] == "ip" else "")
    assert hostinfo.interfaces() == ["10.7.1.20/24", "fd00::5/64"]
    mac = ("lo0: flags=8049\n\tinet 127.0.0.1 netmask 0xff000000\n"
           "en0: flags=8863\n\tinet 192.168.1.23 netmask 0xffffff00 broadcast 192.168.1.255\n"
           "\tinet6 fe80::1c%en0 prefixlen 64 scopeid 0x4\n")
    monkeypatch.setattr(hostinfo.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(hostinfo, "_run", lambda cmd: mac if cmd[0] == "ifconfig" else "")
    assert hostinfo.interfaces() == ["192.168.1.23/24"]


def test_home_fs_is_found():
    assert hostinfo.home_fs("/") is not None


# -- finding environments ----------------------------------------------------

def test_kinds_names_and_installed_cubevis(tmp_path):
    c = make_env(tmp_path / "mf" / "envs" / "hrs", "conda")
    v = make_env(tmp_path / "proj" / ".venv")
    assert envs.kind_of(c) == "conda" and envs.kind_of(c, "/x/micromamba") == "mamba"
    assert envs.kind_of(v) == "venv" and envs.kind_of(str(tmp_path)) == "python"
    assert envs.cubevis_installed(c) is None
    site = [d for d in (tmp_path / "mf" / "envs" / "hrs" / "lib").glob("python3*")][0]
    (site / "site-packages" / "cubevis-1.0.97.dist-info").mkdir()
    assert envs.cubevis_installed(c) == "1.0.97"
    assert envs.default_name(envs.describe_path(c)) == "hrs"
    assert envs.default_name(envs.describe_path(v)) == "proj-venv"
    d = envs.describe_path(os.path.join(v, "bin", "python"))
    assert d["kind"] == "venv" and d["python"] == os.path.join(v, "bin", "python")
    assert envs.default_name({"kind": "python", "python": "/usr/bin/python3.12",
                              "prefix": "/usr"}) == "python3.12"
    assert envs.describe_path(str(tmp_path / "nothing")) is None


def test_candidates_from_tools_venv_and_setup(home, monkeypatch):
    a = make_env(home / "miniforge3" / "envs" / "a", "conda")
    b = make_env(home / "mm" / "envs" / "b", "conda")
    v = make_env(home / "v")
    fake_tool(home / "miniforge3" / "bin", "conda", [a])         # a usual install place
    fake_tool(home / "bin", "micromamba", [b, a])                # on PATH, sees a too
    monkeypatch.setenv("VIRTUAL_ENV", v)
    found = envs.candidates()
    by = {c["prefix"]: c for c in found}
    assert by[a]["kind"] == "conda" and by[a]["tool"].endswith("miniforge3/bin/conda")
    assert by[b]["kind"] == "mamba" and by[b]["tool"].endswith("bin/micromamba")
    assert by[v]["kind"] == "venv" and by[v]["how"] == "the active virtual environment"
    assert found[0]["how"] == "running this setup"
    assert len([c for c in found if c["prefix"] == a]) == 1     # listed twice, kept once


def test_check_reports_why_not(tmp_path):
    e = make_env(tmp_path / "plain")
    r = envs.check(os.path.join(e, "bin", "python"))
    assert not r["ok"] and r["error"] == "cubevis is not installed in it"
    assert r["python"].startswith("3.") and r["versions"]["cubevis"] is None
    r = envs.check(str(tmp_path / "missing" / "python"))
    assert not r["ok"] and "does not exist" in r["error"]
    r = envs.check("/bin/true")
    assert not r["ok"] and "is it Python 3?" in r["error"]


@needs_kernel_py
def test_check_passes_for_a_kernel_environment(tmp_path):
    e = make_env(tmp_path / "k", kernel=True)
    r = envs.check(os.path.join(e, "bin", "python"))
    assert r["ok"], r
    assert r["versions"]["cubevis"] and r["versions"]["ipykernel"] and r["kernel"] is True
    assert r["prefix"] == e
    assert "cubevis" in envs.summary(r) and "visplot data" in envs.summary(r)


# -- the node file -----------------------------------------------------------

def test_node_file_round_trip_and_reruns(home):
    nf = nodefile.load("cv")
    assert not nf.path.exists() and nf.path == home / ".config/cubevis/remote/cv/zuul06.json"
    nf.node["domain"] = "cv.example.org"
    nf.environments["main"] = {"kind": "conda", "python": "/p/bin/python", "status": "ok"}
    nodefile.save(nf)
    assert nodefile.mine() == ["cv"]
    a, b = nodefile.load("cv"), nodefile.load("cv")              # two runs at once
    a.environments["second"] = {"kind": "venv", "python": "/q/bin/python", "status": "new"}
    b.environments["main"]["status"] = "flagged"
    nodefile.save(a)
    nodefile.save(b)
    back = nodefile.load("cv")
    assert set(back.environments) == {"main", "second"}
    assert back.environments["main"]["status"] == "flagged"
    assert back.node["domain"] == "cv.example.org"
    assert list(json.loads(back.path.read_text())) == ["schema", "node", "environments"]
    assert [(n, h) for n, h, _ in nodefile.all_files()] == [("cv", "zuul06")]


# -- the command -------------------------------------------------------------

def _run(home, *args, ssh=True, timeout=600):
    env = dict(os.environ, NO_COLOR="1", PYTHONPATH=ROOT)
    if ssh:
        env["SSH_CONNECTION"] = "10.7.1.5 50000 10.7.1.20 22"
    else:
        env.pop("SSH_CONNECTION", None)
    return subprocess.run([sys.executable, "-m", "cubevis.setup", "--yes", *args],
                          capture_output=True, text=True, env=env, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def _node_file(home, net="cv"):
    return json.loads((home / ".config/cubevis/remote" / net / "zuul06.json").read_text())


def test_node_here_records_the_node_and_environments(home):
    good = make_env(home / "mf" / "envs" / "hrs", "conda", kernel=bool(KERNEL_PY))
    bare = make_env(home / "mf" / "envs" / "bare", "conda")
    fake_tool(home / "bin", "conda", [good, bare])
    r = _run(home, "--answer", "network=cv", "--answer", "domain=cv.example.org",
             "--answer", "query=echo node140 node141,node140",
             "--answer", "environments=2 3", "node-here")
    out = " ".join(r.stdout.split())
    assert "conda env list" in out
    f = _node_file(home)
    node = f["node"]
    assert node["network"] == "cv" and node["host"] == "zuul06"
    assert node["domain"] == "cv.example.org" and node["home"] == str(home)
    assert node["query"]["names"] == ["node140", "node141"] and node["query"]["error"] is None
    assert set(f["environments"]) == {"hrs", "bare"}
    hrs, b = f["environments"]["hrs"], f["environments"]["bare"]
    assert hrs["kind"] == "conda" and hrs["tool"].endswith("/bin/conda")
    assert hrs["python"] == os.path.join(good, "bin", "python")
    assert b["status"] == "flagged" and b["last_error"] == "cubevis is not installed in it"
    if KERNEL_PY:
        assert hrs["status"] == "ok" and hrs["versions"]["cubevis"], r.stdout
        assert r.returncode == 0
    assert not (home / ".config/cubevis/state.json").exists()     # the node side only

    # Again, keeping only the first: the other is no longer used.
    r = _run(home, "--answer", f"environments={good}", "node-here", "cv")
    f = _node_file(home)
    assert set(f["environments"]) == {"hrs"} and f["node"]["domain"] == "cv.example.org"
    assert "stopped using environment: bare" in " ".join(r.stdout.split())
    r = _run(home, "status")
    assert "this computer as a node of cv" in r.stdout and "environment hrs" in r.stdout


def test_node_here_without_ssh_is_local_and_a_failing_query_is_kept(home):
    e = make_env(home / "v")
    r = _run(home, "--answer", f"environments={e}", "node-here", ssh=False)
    f = _node_file(home, "local")
    assert f["node"]["network"] == "local" and f["node"]["query"] is None
    assert list(f["environments"]) == ["v"] and f["environments"]["v"]["kind"] == "venv"
    assert r.returncode == 1                       # it cannot run kernels: no cubevis
    r = _run(home, "--answer", "domain=", "--answer", "query=false",
             "--answer", "environments=none", "node-here", "cv")
    q = _node_file(home)["node"]["query"]
    assert q["command"] == "false" and q["names"] == [] and q["error"]
    assert "no environments enabled" in r.stdout


def test_walk_asks_the_role(home):
    fake_tool(home / "bin", "conda", [])
    r = _run(home, "--answer", "network=cv", "--answer", "domain=",
             "--answer", "environments=none")
    assert "[p]lots-here or [n]ode? [n]" in r.stdout
    assert "This computer as a node" in r.stdout
    r = _run(home, "--answer", "add_network=n", ssh=False)
    assert "[p]lots-here or [n]ode? [p]" in r.stdout and "Tools: ssh and perl" in r.stdout


def test_bad_typed_paths_are_refused(home):
    r = _run(home, "--answer", "environments=/no/such/env 99", "node-here", "cv")
    assert "not a number in the list, nor an environment or a python" in r.stdout
    assert _node_file(home)["environments"] == {}


def test_the_managing_tool_matches_the_kind(home):
    from cubevis.setup.node_here import _tool_for
    fake_tool(home / "bin", "mamba", [])
    assert _tool_for("conda", "/x/bin/conda") == "/x/bin/conda"
    assert _tool_for("mamba", "/x/bin/conda") == str(home / "bin" / "mamba")   # not conda's
    assert _tool_for("conda", None) is None                                    # none here
