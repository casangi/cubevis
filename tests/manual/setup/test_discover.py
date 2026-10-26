"""The discovery script (remote session design 7c): what a host offers
kernels, found by ``cubevis/setup/discover.py`` -- run here directly, as
the payload setup sends to a node's ``python3 -``, and under Python 3.6
(RHEL 8's) -- and what its reports mean (``envs``).

    pytest tests/manual/setup/test_discover.py
    CUBEVIS_TEST_KERNEL_PYTHON=/path/to/python pytest ...   # + working environments
    CUBEVIS_TEST_PY36=/opt/py36/bin/python3.6 pytest ...    # + the oldest python3

Environments are small venvs made here (``--without-pip``); a conda one is a
venv with a ``conda-meta`` directory, listed by a fake ``conda``.  An
environment that can run kernels needs cubevis, ipykernel and bokeh: with
``CUBEVIS_TEST_KERNEL_PYTHON`` naming a Python that has them, the test
environments see its packages through a ``.pth`` file (and a copy of its
cubevis ``dist-info``, which discovery looks for); without it those tests
are skipped.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys

import pytest

from cubevis.setup import discover as D
from cubevis.setup import envs, remote

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
KERNEL_PY = os.environ.get("CUBEVIS_TEST_KERNEL_PYTHON")
PY36 = os.environ.get("CUBEVIS_TEST_PY36") or ("/opt/py36/bin/python3.6"
                                               if os.path.exists("/opt/py36/bin/python3.6") else None)
needs_kernel_py = pytest.mark.skipif(not KERNEL_PY, reason="set CUBEVIS_TEST_KERNEL_PYTHON to a "
                                     "Python with cubevis, ipykernel and bokeh")
needs_py36 = pytest.mark.skipif(not PY36, reason="set CUBEVIS_TEST_PY36 to a Python 3.6")


def _purelib(python):
    return subprocess.run([python, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                          capture_output=True, text=True, check=True).stdout.strip()


def make_env(prefix, kind="venv", kernel=False):
    # From the kernel Python when there is one: its packages are seen through
    # the .pth file, so the versions must match.
    subprocess.run([KERNEL_PY or sys.executable, "-m", "venv", "--without-pip", str(prefix)],
                   check=True)
    if kind == "conda":
        os.makedirs(os.path.join(prefix, "conda-meta"))
    if kernel:
        site = _purelib(os.path.join(prefix, "bin", "python"))
        ksite = _purelib(KERNEL_PY)
        with open(os.path.join(site, "kernel.pth"), "w") as f:
            f.write(f"{ROOT}\n{ksite}\n")
        for d in glob.glob(os.path.join(ksite, "cubevis-*.dist-info")):
            shutil.copytree(d, os.path.join(site, os.path.basename(d)))
    return str(prefix)


def fake_tool(bindir, name, prefixes, envs_dirs=()):
    os.makedirs(bindir, exist_ok=True)
    p = os.path.join(bindir, name)
    with open(p, "w") as f:
        f.write("#!/bin/sh\ncase \"$*\" in\n"
                f"  'env list --json') cat <<'EOF'\n{json.dumps({'envs': [str(x) for x in prefixes]})}\nEOF\n;;\n"
                f"  'config --show envs_dirs --json') cat <<'EOF'\n{json.dumps({'envs_dirs': list(envs_dirs)})}\nEOF\n;;\n"
                "  *) exit 1;;\nesac\n")
    os.chmod(p, 0o755)
    return p


def fake_cubevis(prefix, version="1.0.97", installer="pip", direct_url=None, conda=False):
    """Packaging records of a cubevis install, without the package."""
    site = glob.glob(os.path.join(prefix, "lib", "python3*", "site-packages"))[0]
    d = os.path.join(site, f"cubevis-{version}.dist-info")
    os.makedirs(d)
    with open(os.path.join(d, "METADATA"), "w") as f:
        f.write(f"Metadata-Version: 2.1\nName: cubevis\nVersion: {version}\n")
    with open(os.path.join(d, "INSTALLER"), "w") as f:
        f.write(installer + "\n")
    if direct_url:
        with open(os.path.join(d, "direct_url.json"), "w") as f:
            json.dump(direct_url, f)
    if conda:
        with open(os.path.join(prefix, "conda-meta", f"cubevis-{version}-py_0.json"), "w") as f:
            f.write("{}")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A home of its own (so ~/miniforge3 and friends are not found)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    for v in ("CONDA_EXE", "MAMBA_EXE", "VIRTUAL_ENV", "CONDA_PREFIX"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("PATH", f"{tmp_path}/bin:/usr/bin:/bin")
    return tmp_path


def _run_payload(python, *args, home=None, cwd=None):
    """The payload on stdin, as setup sends it: ``python - ARGS``."""
    env = dict(os.environ)
    if home is not None:
        env.update(HOME=str(home), PATH=f"{home}/bin:/usr/bin:/bin")
        env.pop("XDG_CONFIG_HOME", None)
    env.pop("PYTHONPATH", None)
    r = subprocess.run([python, "-", *args], input=D.payload(), capture_output=True, text=True,
                       env=env, cwd=cwd or "/", timeout=600)
    return r


def _report(r):
    for line in r.stdout.splitlines():
        if line.startswith(D.MARKER):
            return json.loads(line[len(D.MARKER):])
    raise AssertionError(r.stdout + r.stderr)


# -- what a host knows about itself -------------------------------------------

def test_ranges_and_shared_filesystems():
    assert D.ranges_of(["10.7.1.20/24", "10.7.1.21/24", "fd00::5/64", "10.8.0.3/16"]) == \
        ["10.7.1.0/24", "10.8.0.0/16", "fd00::/64"]
    assert D.shared("nfs4") and D.shared("lustre") and D.shared("NFS")
    assert D.shared("ext4") is False and D.shared(None) is None
    assert D.domain_of("zuul06.cv.nrao.edu") == "cv.nrao.edu"
    assert D.domain_of("zuul06") is None


def test_interfaces_parsing(monkeypatch):
    ip = ("1: lo    inet 127.0.0.1/8 scope host lo\n"
          "2: eth0    inet 10.7.1.20/24 brd 10.7.1.255 scope global eth0\n"
          "2: eth0    inet6 fe80::1/64 scope link\n"
          "3: docker0    inet 172.17.0.1/16 scope global docker0\n"
          "4: ib0    inet6 fd00::5/64 scope global\n")
    monkeypatch.setattr(D.platform, "system", lambda: "Linux")
    monkeypatch.setattr(D, "_run", lambda cmd, timeout=10: ip if cmd[0] == "ip" else "")
    assert D.interfaces() == ["10.7.1.20/24", "fd00::5/64"]
    mac = ("lo0: flags=8049\n\tinet 127.0.0.1 netmask 0xff000000\n"
           "en0: flags=8863\n\tinet 192.168.1.23 netmask 0xffffff00 broadcast 192.168.1.255\n"
           "\tinet6 fe80::1c%en0 prefixlen 64 scopeid 0x4\n")
    monkeypatch.setattr(D.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(D, "_run", lambda cmd, timeout=10: mac if cmd[0] == "ifconfig" else "")
    assert D.interfaces() == ["192.168.1.23/24"]


def test_home_fs_and_host_facts():
    assert D.home_fs("/") is not None
    h = D.host_facts()
    assert h["home"] == os.path.expanduser("~") and h["user"]
    assert h["config_dir"].endswith("/cubevis")


# -- finding environments ----------------------------------------------------

def test_kinds_names_and_installed_cubevis(tmp_path):
    c = make_env(tmp_path / "mf" / "envs" / "hrs", "conda")
    v = make_env(tmp_path / "proj" / ".venv")
    assert D.kind_of(c) == "conda" and D.kind_of(v) == "venv"
    assert D.kind_of(str(tmp_path)) == "python"
    assert D.cubevis_installed(c) is None
    fake_cubevis(c)
    assert D.cubevis_installed(c) == "1.0.97"
    assert envs.default_name(D.describe_path(c)) == "hrs"
    assert envs.default_name(D.describe_path(v)) == "proj-venv"
    d = D.describe_path(os.path.join(v, "bin", "python"))
    assert d["kind"] == "venv" and d["python"] == os.path.join(v, "bin", "python")
    assert envs.default_name({"kind": "python", "python": "/usr/bin/python3.12",
                              "prefix": "/usr"}) == "python3.12"
    assert D.describe_path(str(tmp_path / "nothing")) is None


def test_candidates_tools_envs_dirs_and_markers(home, monkeypatch):
    a = make_env(home / "miniforge3" / "envs" / "a", "conda")
    b = make_env(home / "mm" / "envs" / "b", "conda")
    v = make_env(home / "v")
    with open(os.path.join(a, D.SETUP_MARKER_FILE), "w") as f:
        json.dump({"cubevis": "1.0.97", "fingerprint": "k1-x"}, f)
    conda = fake_tool(home / "miniforge3" / "bin", "conda", [a], [str(home / "miniforge3" / "envs")])
    fake_tool(home / "bin", "micromamba", [b, a])                # on PATH, sees a too
    monkeypatch.setenv("VIRTUAL_ENV", v)
    found = D.candidates()
    by = {c["prefix"]: c for c in found}
    assert by[a]["kind"] == "conda" and by[a]["tool"].endswith("miniforge3/bin/conda")
    assert by[a]["marker"] == {"cubevis": "1.0.97", "fingerprint": "k1-x"}
    assert by[b]["kind"] == "conda" and by[b]["tool"].endswith("bin/micromamba")
    assert by[v]["kind"] == "venv" and by[v]["how"] == "the active virtual environment"
    assert by[v]["marker"] is None
    assert found[0]["how"] == "running this"
    assert len([c for c in found if c["prefix"] == a]) == 1     # listed twice, kept once
    assert D.envs_dirs(conda) == [str(home / "miniforge3" / "envs")]


def test_check_reports_why_not(tmp_path):
    e = make_env(tmp_path / "plain")
    r = envs.check(os.path.join(e, "bin", "python"))
    assert not r["ok"] and r["error"] == "cubevis is not installed in it"
    assert r["python"].startswith("3.") and r["versions"]["cubevis"] is None
    r = envs.check(str(tmp_path / "missing" / "python"))
    assert not r["ok"] and "does not exist" in r["error"]
    r = envs.check("/bin/true")
    assert not r["ok"] and "is it Python 3?" in r["error"]
    assert envs.interpret(None)["ok"] is False


@needs_kernel_py
def test_check_passes_for_a_kernel_environment(tmp_path):
    e = make_env(tmp_path / "k", kernel=True)
    r = envs.check(os.path.join(e, "bin", "python"))
    assert r["ok"], r
    assert r["versions"]["cubevis"] and r["versions"]["ipykernel"] and r["kernel"] is True
    assert r["prefix"] == e and r["fingerprint"].startswith("k1-")
    assert "cubevis" in envs.summary(r) and "visplot data" in envs.summary(r)


def test_install_method_from_the_records():
    m = envs.install_method
    assert m({"install": {"conda": True, "installer": "conda"}})["method"] == "conda"
    assert m({"install": {"installer": "pip"}}) == {"method": "pip", "source": None}
    assert m({"install": {"installer": "uv"}})["method"] == "uv"
    c = m({"install": {"installer": "pip", "url": "file:///users/jo/src/cube%20vis",
                       "editable": True}})
    assert c == {"method": "checkout", "source": "/users/jo/src/cube vis", "editable": True}
    assert envs.describe_method(c) == "from the checkout /users/jo/src/cube vis"
    g = m({"install": {"installer": "pip", "url": "https://github.com/casangi/cubevis",
                       "vcs": "git"}})
    assert g == {"method": "vcs", "source": "https://github.com/casangi/cubevis"}
    assert m({"install": {}})["method"] == "unknown" and m({})["method"] == "unknown"
    w = m({"install": {"installer": "pip", "archive": True,
                       "url": "file:///tmp/cubevis-1.0.97.dev22%2Bg0a5b519-py3-none-any.whl"}})
    assert w == {"method": "file", "source": "/tmp/cubevis-1.0.97.dev22+g0a5b519-py3-none-any.whl"}
    assert m({"install": {"url": "file:///x/cubevis-1.0.tar.gz"}})["method"] == "file"


def test_the_kind_is_the_interpreters_and_paths_are_the_nodes():
    k = envs.kind_from_report
    assert k({"prefix": "/e", "conda": True, "base_prefix": "/b"}) == "conda"
    assert k({"prefix": "/v", "conda": False, "base_prefix": "/opt/cf"}) == "venv"
    assert k({"prefix": "/usr", "conda": False, "base_prefix": "/usr/"}) == "python"
    assert k({}) is None


def test_the_check_reports_how_cubevis_was_installed(tmp_path):
    e = make_env(tmp_path / "e")
    fake_cubevis(e, direct_url={"url": "file:///src/cubevis", "dir_info": {"editable": True}})
    r = envs.check(os.path.join(e, "bin", "python"))
    assert r["install"] == {"installer": "pip", "conda": False, "url": "file:///src/cubevis",
                            "editable": True, "vcs": None, "archive": False}
    assert not r["ok"] and "does not import" in r["error"]      # records only, no package


# -- the script as setup runs it ---------------------------------------------

def test_payload_on_stdin_reports_and_checks(home):
    """``python3 - --json`` with the payload, in a home with a fake conda:
    only environments with cubevis (or setup's marker, or given) are run."""
    a = make_env(home / "miniforge3" / "envs" / "withcv", "conda")
    b = make_env(home / "miniforge3" / "envs" / "without", "conda")
    fake_cubevis(a)
    fake_tool(home / "miniforge3" / "bin", "conda", [a, b], [str(home / "miniforge3" / "envs")])
    r = _run_payload(sys.executable, "--json", home=home)
    rep = _report(r)
    assert rep["discover"] == D.VERSION and rep["host"]["home"] == str(home)
    assert [t["name"] for t in rep["tools"]] == ["conda"]
    by = {e["prefix"]: e for e in rep["environments"]}
    assert by[a]["check"]["versions"]["cubevis"] == "1.0.97"
    assert by[b]["check"] is None and by[b]["cubevis"] is None
    assert not rep["errors"]
    # --only PATH: just that one, checked though it has no cubevis.
    rep = _report(_run_payload(sys.executable, "--json", "--only", b, home=home))
    assert [e["prefix"] for e in rep["environments"]] == [b]
    assert rep["environments"][0]["check"]["versions"]["cubevis"] is None
    rep = _report(_run_payload(sys.executable, "--json", "--only", "/no/such", home=home))
    assert rep["environments"] == [] and "not an environment" in rep["errors"][0]


def test_by_hand_bundle_and_without_envcheck(home, tmp_path):
    """The forms a person runs on a node: the module (readable text), the
    bundle (a file with envcheck in it), and the bare file, which says it
    cannot check without envcheck.py."""
    env = dict(os.environ, PYTHONPATH=ROOT, HOME=str(home))
    r = subprocess.run([sys.executable, "-m", "cubevis.setup.discover"], capture_output=True,
                       text=True, env=env, timeout=300)
    assert r.returncode == 0 and "environments:" in r.stdout, r.stderr
    b = subprocess.run([sys.executable, "-m", "cubevis.setup.discover", "--bundle"],
                       capture_output=True, text=True, env=env).stdout
    assert b.startswith("# cubevis setup's discovery script") and "ENVCHECK_SOURCE = " in b
    p = tmp_path / "discover.py"
    p.write_text(b)
    r = subprocess.run([sys.executable, str(p), "--json"], capture_output=True, text=True,
                       env=dict(env, PYTHONPATH=""), cwd=str(tmp_path))
    assert not _report(r)["errors"]
    bare = tmp_path / "bare" / "discover.py"
    bare.parent.mkdir()
    shutil.copy(D.__file__, bare)
    r = subprocess.run([sys.executable, str(bare), "--json"], capture_output=True, text=True,
                       env=dict(env, PYTHONPATH=""), cwd=str(bare.parent))
    assert any("envcheck.py is not here" in e for e in _report(r)["errors"])


@needs_py36
def test_the_payload_runs_under_python_3_6(home):
    """RHEL 8's python3 is 3.6: the script and envcheck must run there."""
    a = make_env(home / "miniforge3" / "envs" / "a", "conda")
    fake_cubevis(a)
    fake_tool(home / "miniforge3" / "bin", "conda", [a])
    r = _run_payload(PY36, "--json", "/usr/bin/python3", home=home)
    rep = _report(r)
    assert rep["python"].startswith("3.6."), rep
    assert {e["prefix"] for e in rep["environments"]} >= {a}
    assert not rep["errors"], rep["errors"]
    # envcheck itself under 3.6 (an environment that old cannot run cubevis).
    r = _run_payload(PY36, "--json", "--only", PY36, home=home)
    ch = _report(r)["environments"][0]["check"]
    assert ch["python"].startswith("3.6.") and ch["versions"]["cubevis"] is None
    assert "cubevis needs 3.12" in envs.interpret(ch)["error"]
    r = _run_payload(PY36, home=home)                              # readable form
    assert r.returncode == 0 and "environments:" in r.stdout, r.stderr


@pytest.mark.skipif(not shutil.which("tcsh"), reason="needs tcsh")
def test_the_remote_command_lines_survive_csh(tmp_path):
    """ssh hands the remote command to the login shell, csh at some sites:
    the python finder and the launcher test pass through it unchanged."""
    line = remote._python_finder() + " --json --no-check"
    r = subprocess.run(["tcsh", "-f", "-c", line], input=D.payload(), capture_output=True,
                       text=True, timeout=120)
    assert _report(r)["discover"] == D.VERSION, r.stderr
    from cubevis.setup import launcher
    import shlex
    lp = tmp_path / "l.sh"
    lp.write_text(f"#!/bin/bash\nexec {shlex.quote(sys.executable)} \"$@\"\n")
    lp.chmod(0o700)
    code = launcher.TEST_CODE.replace("import os,ipykernel,cubevis.remote.worker_main;", "import os;")
    code = code.replace("from cubevis._fingerprint import fingerprint as f;", "f=lambda: 'k1-t';")
    r = subprocess.run(["tcsh", "-f", "-c", f"{shlex.quote(str(lp))} -c {shlex.quote(code)}"],
                       capture_output=True, text=True, cwd=str(tmp_path))
    assert launcher.parse_test(r.stdout) == ("k1-t", str(tmp_path)), r.stdout + r.stderr
