"""What a kernel can run (``cubevis.setup.capabilities``; iclean design step
5b): the probe the launcher test runs in a kernel's context, and the
refusal before connecting when setup recorded that a kernel cannot run an
application.

    pytest tests/manual/setup/test_capabilities.py

The probe runs here in a subprocess: under this Python (CASA's packages
when it has them: the interactive clean cases skip without), and under a
Python with neither xarray-ms nor casatasks.  No kernel, no ssh.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys

import pytest

from cubevis.setup import capabilities as C
from cubevis.setup import launcher as L
from cubevis.setup import state as S
from cubevis.setup.steps import Ctx
from cubevis.setup.ui import UI

HAS_CASA = all(importlib.util.find_spec(m) for m in ("casatasks", "casatools", "casaconfig"))
HAS_CASADATA = importlib.util.find_spec("casadata") is not None
needs_casa = pytest.mark.skipif(not HAS_CASA, reason="casatasks, casatools and casaconfig")


def probe(python, cwd, env=None):
    """The probe's report, run as a launcher test runs it (stdin)."""
    r = subprocess.run([python, "-c", L.TEST_CODE], input=C.PROBE, capture_output=True,
                       text=True, cwd=str(cwd), env=env, timeout=120)
    rep = C.parse(r.stdout)
    assert rep is not None, r.stdout + r.stderr
    return rep


def clean_env(home, **more):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home)}
    env.update(more)
    return env


def test_parse_and_words():
    assert C.parse("noise\nCUBEVIS-CAPS {\"capabilities\": {\"iclean\": false}}\n") == {
        "capabilities": {"iclean": False}}
    assert C.parse("Traceback ...") is None and C.parse("CUBEVIS-CAPS {not json") is None
    assert C.describe(None) == "not known"
    assert C.describe({"visplot": True, "iclean": True}) == "visplot, interactive clean"
    assert C.describe({"visplot": False, "iclean": True}) == "interactive clean"
    assert C.describe({"visplot": False, "iclean": False}) == "neither visplot nor interactive clean"
    assert C.describe_data({"ready": True, "measurespath": "/d", "auto_update": True}) == \
        "in /d (auto-updated)"
    assert C.describe_data({"ready": False, "why": "/d does not exist"}) == "/d does not exist"


def test_a_python_without_either(tmp_path):
    """No xarray-ms, no casatasks: neither, each with why; no CASA data asked."""
    py = "/usr/bin/python3"
    if not os.path.exists(py) or subprocess.run(
            [py, "-c", "import importlib.util as u, sys; "
                       "sys.exit(any(u.find_spec(m) for m in ('xarray_ms', 'casatasks')))"]).returncode:
        pytest.skip("needs a Python without xarray-ms and casatasks")
    rep = probe(py, tmp_path, clean_env(tmp_path))
    assert rep["capabilities"] == {"visplot": False, "iclean": False}
    assert "casatasks is not installed" in rep["errors"]["iclean"]
    assert "xarray_ms" in rep["errors"]["visplot"] and rep["casa_data"] is None


@needs_casa
def test_no_casa_data_is_said_and_nothing_is_written(tmp_path):
    """A home without ~/.casa: interactive clean is there, its data is not --
    and the probe imports no casatools (no casa-*.log in the directory, no
    ~/.casa made)."""
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    rep = probe(sys.executable, work, clean_env(home))
    assert rep["capabilities"]["iclean"] is True and rep["versions"]["casatools"]
    d = rep["casa_data"]
    assert d["ready"] is False and d["measurespath"] == str(home / ".casa" / "data")
    assert "does not exist" in d["why"] and d["files"] == [] and d["auto_update"] is True
    assert os.listdir(work) == [] and os.listdir(home) == []


@needs_casa
def test_your_configuration_is_reported(tmp_path):
    home = tmp_path / "home"
    data = tmp_path / "data"
    (data / "geodetic").mkdir(parents=True)
    (home / ".casa").mkdir(parents=True)
    (home / ".casa" / "config.py").write_text(
        f"measurespath = {str(data)!r}\nmeasures_auto_update = False\ndata_auto_update = False\n")
    rep = probe(sys.executable, tmp_path, clean_env(home))
    d = rep["casa_data"]
    assert d["ready"] is True and d["measurespath"] == str(data) and d["auto_update"] is False
    assert d["files"] == [str(home / ".casa" / "config.py")]


@needs_casa
@pytest.mark.skipif(not HAS_CASADATA, reason="casadata")
def test_setups_site_configuration_gives_the_private_copy(tmp_path):
    """What setup writes for a private copy, as the kernel's CASASITECONFIG:
    casadata's directory, no updates."""
    import casadata
    home = tmp_path / "home"
    home.mkdir()
    site = tmp_path / C.SITE_CONFIG_FILE
    site.write_text(C.SITE_CONFIG_TEXT)
    rep = probe(sys.executable, tmp_path, clean_env(home, CASASITECONFIG=str(site)))
    d = rep["casa_data"]
    assert d["ready"] is True and d["measurespath"] == os.path.abspath(casadata.datapath)
    assert d["files"] == [str(site)] and d["site"] == str(site) and d["auto_update"] is False
    # Your own measurespath still wins over the site's (casaconfig's order).
    mine = tmp_path / "mine"
    (mine / "geodetic").mkdir(parents=True)
    (home / ".casa").mkdir()
    (home / ".casa" / "config.py").write_text(f"measurespath = {str(mine)!r}\n")
    d = probe(sys.executable, tmp_path, clean_env(home, CASASITECONFIG=str(site)))["casa_data"]
    assert d["measurespath"] == str(mine) and d["files"] == [str(site), str(home / ".casa" / "config.py")]


def test_the_launcher_sets_casasiteconfig_for_a_private_copy():
    e = {"python": "/c/envs/x/bin/python", "prefix": "/c/envs/x", "kind": "conda",
         "casa_site_config": "/c/envs/x/" + C.SITE_CONFIG_FILE}
    text = L.text("k", {}, e, "/home/jo", "gui1")
    assert f"export CASASITECONFIG=/c/envs/x/{C.SITE_CONFIG_FILE}" in text
    assert text.index("CASASITECONFIG") < text.index("exec ")
    # The kernel's own value wins: the launcher does not set it.
    text = L.text("k", {"env": {"CASASITECONFIG": "/site/mine.py"}}, e, "/home/jo", "gui1")
    assert text.count("CASASITECONFIG") == 1 and "export CASASITECONFIG=/site/mine.py" in text
    e.pop("casa_site_config")
    assert "CASASITECONFIG" not in L.text("k", {}, e, "/home/jo", "gui1")


# --------------------------------------------------------------------------
# The laptop side: refused before connecting
# --------------------------------------------------------------------------

@pytest.fixture
def recorded(tmp_path, monkeypatch):
    """Setup's state with kernels: *name* -> capabilities (None: never said)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("CUBEVIS_HOST_ID", "gui1")

    def make(**kernels):
        st = S.load()
        st.add_network("n", login_host=None, user="jo")
        st.add_environment("e", network="n", python="/c/x/bin/python", kind="conda",
                           prefix="/c/x", hosts="all")
        for name, caps in kernels.items():
            k = st.add_kernel(name, environment="e")
            if caps is not None:
                k["capabilities"] = caps
                k["capability_errors"] = {a: "casatasks is not installed"
                                          for a, v in caps.items() if not v}
        S.save(st)
    return make


def test_require_refuses_only_what_setup_recorded(recorded):
    recorded(both={"visplot": True, "iclean": True}, plots={"visplot": True, "iclean": False},
             old=None)
    for name in ("both", "cubevis-both"):                       # setup's or Jupyter's name
        C.require(name, "iclean")
        C.require(name, "visplot")
    with pytest.raises(C.MissingCapability) as exc:
        C.require("cubevis-plots", "iclean")
    assert "cannot run interactive clean: casatasks is not installed" in str(exc.value)
    assert "python -m cubevis.setup HOST" in str(exc.value)
    C.require("plots", "visplot")
    C.require("old", "iclean")                                  # never checked: not refused
    C.require("somebody-elses", "iclean")                       # not setup's: not refused


def test_no_setup_state_refuses_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "none"))
    C.require("anything", "iclean")


def test_iclean_refuses_the_kernel_before_starting_it(recorded, monkeypatch):
    """``RemoteImagingSession`` (``iclean(kernel_name=)``): no kernel manager
    is made for a kernel that cannot run interactive clean."""
    pytest.importorskip("jupyter_client")
    import jupyter_client
    made = []
    monkeypatch.setattr(jupyter_client, "AsyncKernelManager", lambda **kw: made.append(kw))
    recorded(plots={"visplot": True, "iclean": False})
    from cubevis.imaging import RemoteImagingSession
    with pytest.raises(C.MissingCapability):
        RemoteImagingSession({"vis": "x.ms", "imagename": "x"}, "cubevis-plots")
    assert made == []


def test_iclean_takes_a_setup_kernels_own_name(tmp_path, recorded, monkeypatch):
    """Setup prints ``kernel_name="NAME"``; Jupyter knows ``cubevis-NAME``.
    Every remote connection resolves it, as visplot always did (Darrell,
    2026-11-09: iclean failed with NoSuchKernel)."""
    pytest.importorskip("jupyter_client")
    import json as _json
    import jupyter_client
    monkeypatch.setenv("JUPYTER_DATA_DIR", str(tmp_path / "jd"))
    monkeypatch.setenv("JUPYTER_PATH", str(tmp_path / "jd"))
    d = tmp_path / "jd" / "kernels" / "cubevis-cvpost106-iclean"
    d.mkdir(parents=True)
    (d / "kernel.json").write_text(_json.dumps({"argv": ["python", "-c", "pass"],
                                                "display_name": "x", "language": "python"}))
    recorded(**{"cvpost106-iclean": {"visplot": True, "iclean": True},
                "plots": {"visplot": True, "iclean": False}})
    started = []

    class Stop(Exception):
        pass

    def manager(kernel_name):
        started.append(kernel_name)
        raise Stop
    monkeypatch.setattr(jupyter_client, "AsyncKernelManager", manager)
    from cubevis.imaging import RemoteImagingSession
    with pytest.raises(Stop):
        RemoteImagingSession({"vis": "x.ms", "imagename": "x"}, "cvpost106-iclean")
    assert started == ["cubevis-cvpost106-iclean"]
    with pytest.raises(ValueError, match="no Jupyter kernel named 'nope' here"):
        RemoteImagingSession({"vis": "x.ms", "imagename": "x"}, "nope")
    assert started == ["cubevis-cvpost106-iclean"]


def test_a_kernel_on_another_node_of_a_cluster_is_setups_kernel(recorded):
    """``python -m cubevis.setup jupyter NAME NODE`` makes ``cubevis-NAME-NODE``:
    the capability check knows it as NAME (before 5c it knew nothing of it, so
    never refused it)."""
    recorded(plots={"visplot": True, "iclean": False})
    st = S.load()
    st.add_node("cvpost107", network="n", hostname="cvpost107.example.org")
    S.save(st)
    from cubevis.setup.sessions import setup_kernel_name
    assert setup_kernel_name("cubevis-plots-cvpost107", S.load()) == "plots"
    assert setup_kernel_name("cubevis-plots-cvpost107") == "plots-cvpost107"   # no state: as before
    assert setup_kernel_name("cubevis-other-cvpost107", S.load()) == "other-cvpost107"
    with pytest.raises(C.MissingCapability):
        C.require("cubevis-plots-cvpost107", "iclean")
    C.require("cubevis-plots-cvpost107", "visplot")
