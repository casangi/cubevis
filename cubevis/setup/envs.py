"""Python environments on this computer, for kernels: finding candidates,
telling their kind, and checking that one can run cubevis kernels.

Kinds (design section 7b):

    conda   a conda-format environment; ``tool`` is the command the user
            installs and upgrades packages with: conda, mamba or micromamba
    venv    a virtual environment (``python -m venv``)
    python  a plain interpreter (packages installed for it, perhaps --user)

(Until 2026-10-21 there was a fourth, ``mamba``: the environments are the
same, so only the tool differs; old records read as ``conda``.)

Setup *asks* which environments to use; what is found here only pre-fills
the list.  Finding is cheap and changes nothing: the conda tools' own
``env list --json``, the active virtual environment, the Python running
setup, and a look for an installed cubevis (its ``dist-info`` directory)
without running anything.  ``check`` runs the interpreter with
``data/envcheck.py`` in an empty directory.

An environment is identified by its interpreter's path, absolute but not
with symbolic links resolved (a venv's ``bin/python`` is a link to the
base interpreter; resolving it would lose the venv).
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from . import data_file

KINDS = ("conda", "venv", "python")
TOOLS = ("conda", "mamba", "micromamba")
CHECK_TIMEOUT = 300          # seconds: the first import from a cold NFS cache is slow
LIST_TIMEOUT = 60

_COMMON_BASES = ("miniforge3", "mambaforge", "miniconda3", "anaconda3", "micromamba",
                 ".local/share/mamba")


def interpreter(prefix: str) -> Optional[str]:
    """``<prefix>/bin/python`` (or ``python3``) if it exists."""
    for name in ("python", "python3"):
        p = os.path.join(prefix, "bin", name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def norm(path: str) -> str:
    """Absolute and normalised, links kept (see the module docstring)."""
    return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


def cubevis_installed(prefix: str) -> Optional[str]:
    """The version of cubevis installed in *prefix*, from its dist-info
    directory's name (nothing is run), or None."""
    for d in sorted(glob.glob(os.path.join(prefix, "lib", "python3*", "site-packages",
                                           "cubevis-*.dist-info"))):
        m = re.match(r"cubevis-(.+)\.dist-info$", os.path.basename(d))
        if m:
            return m.group(1)
    return None


def kind_of(prefix: str, listed_by: Optional[str] = None) -> str:
    """The likely kind of the environment at *prefix* (the user is asked)."""
    if os.path.isdir(os.path.join(prefix, "conda-meta")):
        return "conda"
    if os.path.isfile(os.path.join(prefix, "pyvenv.cfg")):
        return "venv"
    return "python"


def conda_tools() -> list:
    """conda, mamba and micromamba executables found here: on PATH, named
    by ``$CONDA_EXE`` / ``$MAMBA_EXE``, or in the usual install places."""
    found = []
    for env in ("CONDA_EXE", "MAMBA_EXE"):
        if os.environ.get(env):
            found.append(os.environ[env])
    for name in ("conda", "mamba", "micromamba"):
        w = shutil.which(name)
        if w:
            found.append(w)
    home = os.path.expanduser("~")
    for base in _COMMON_BASES + ("/opt/conda",):
        root = base if base.startswith("/") else os.path.join(home, base)
        for name in ("conda", "mamba", "micromamba"):
            found.append(os.path.join(root, "bin", name))
    out, seen = [], set()
    for f in found:
        if not (os.path.isfile(f) and os.access(f, os.X_OK)):
            continue
        real = os.path.realpath(f)
        if real in seen:
            continue
        seen.add(real)
        out.append(norm(f))
    return out


def conda_envs(tool: str) -> list:
    """Prefixes ``<tool> env list --json`` reports ([] if it fails)."""
    try:
        r = subprocess.run([tool, "env", "list", "--json"], capture_output=True, text=True,
                           timeout=LIST_TIMEOUT, stdin=subprocess.DEVNULL)
        data = json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    envs = data.get("envs") if isinstance(data, dict) else None
    return [norm(e) for e in envs or [] if isinstance(e, str)]


def candidates() -> list:
    """Environments found here, as dicts ``{python, prefix, kind, tool,
    cubevis, how}``: one per interpreter, in the order found."""
    out, seen = [], set()

    def add(prefix, how, tool=None):
        py = interpreter(prefix)
        if not py:
            return
        py = norm(py)
        if py in seen:
            return
        seen.add(py)
        k = kind_of(prefix, tool)
        out.append({"python": py, "prefix": norm(prefix), "kind": k,
                    "tool": tool if k == "conda" else None,
                    "cubevis": cubevis_installed(prefix), "how": how})

    if sys.prefix:
        add(sys.prefix, "running this setup")
        if out and not out[0]["cubevis"]:
            try:                       # it runs cubevis, installed or on a path
                from importlib.metadata import version
                out[0]["cubevis"] = version("cubevis")
            except Exception:
                pass
    if os.environ.get("VIRTUAL_ENV"):
        add(os.environ["VIRTUAL_ENV"], "the active virtual environment")
    if os.environ.get("CONDA_PREFIX"):
        add(os.environ["CONDA_PREFIX"], "the active conda environment")
    # An environment several tools list goes with conda when conda lists it
    # (the usual way to manage one), else with the first tool that did.
    listed: dict = {}
    for tool in conda_tools():
        for prefix in conda_envs(tool):
            prev = listed.get(prefix)
            if prev is None or (os.path.basename(tool) == "conda"
                                and os.path.basename(prev) != "conda"):
                listed[prefix] = tool
    for prefix, tool in listed.items():
        add(prefix, f"{os.path.basename(tool)} env list", tool)
    return out


def describe_path(text: str) -> Optional[dict]:
    """A candidate from a path the user typed: an environment's directory
    or an interpreter.  None if it is neither."""
    p = norm(text)
    if os.path.isdir(p):
        py = interpreter(p)
        if not py:
            return None
        return {"python": norm(py), "prefix": p, "kind": kind_of(p), "tool": None,
                "cubevis": cubevis_installed(p), "how": "typed"}
    if os.path.isfile(p) and os.access(p, os.X_OK):
        prefix = os.path.dirname(os.path.dirname(p))
        return {"python": p, "prefix": prefix, "kind": kind_of(prefix), "tool": None,
                "cubevis": cubevis_installed(prefix), "how": "typed"}
    return None


def default_name(c: dict) -> str:
    """A name for an environment: its directory's name (``envs/<name>``,
    a venv's directory), or ``python3.12`` for a plain interpreter."""
    if c["kind"] == "python":
        return re.sub(r"[^A-Za-z0-9_.-]", "-", os.path.basename(c["python"])) or "python"
    base = os.path.basename(c["prefix"].rstrip("/")) or "env"
    if base in ("venv", ".venv", "env", ".env"):
        base = os.path.basename(os.path.dirname(c["prefix"].rstrip("/"))) + "-" + base.lstrip(".")
    return re.sub(r"[^A-Za-z0-9_.-]", "-", base).lstrip("-._") or "env"


def check(python: str, timeout: float = CHECK_TIMEOUT) -> dict:
    """Run *python* with ``envcheck.py``: its report (envcheck's header),
    plus ``ok`` (it can run kernels) and ``error`` (why not, one line)."""
    script = data_file("envcheck.py").read_text()
    with tempfile.TemporaryDirectory(prefix="cubevis-envcheck-") as d:
        env = dict(os.environ)
        env.pop("PYTHONHOME", None)
        try:
            r = subprocess.run([python, "-c", script], cwd=d, capture_output=True, text=True,
                               timeout=timeout, env=env, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            return {"ok": False, "error": f"{python} does not exist"}
        except PermissionError:
            return {"ok": False, "error": f"{python} cannot be run (permission denied)"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"no answer within {int(timeout)} s"}
        except OSError as exc:
            return {"ok": False, "error": f"{python} cannot be run: {exc}"}
    rep = None
    for line in (r.stdout or "").splitlines():
        if line.startswith("CUBEVIS-ENVCHECK "):
            try:
                rep = json.loads(line[len("CUBEVIS-ENVCHECK "):])
            except ValueError:
                rep = None
    if rep is None:
        last = [l for l in (r.stderr or "").strip().splitlines() if l]
        return {"ok": False, "error": (last[-1] if last else f"exit {r.returncode}")
                + " (is it Python 3?)"}
    errs = rep.get("errors") or {}
    if rep.get("kernel"):
        rep["ok"], rep["error"] = True, None
    elif "cubevis" in errs and not (rep.get("versions") or {}).get("cubevis"):
        rep["ok"], rep["error"] = False, "cubevis is not installed in it"
    elif "cubevis" in errs:
        rep["ok"], rep["error"] = False, f"cubevis does not import: {errs['cubevis']}"
    else:
        rep["ok"], rep["error"] = False, f"ipykernel is missing: {errs.get('ipykernel', '?')}"
    major_minor = tuple(int(x) for x in rep.get("python", "0.0").split(".")[:2])
    if major_minor < (3, 12) and not rep["ok"]:
        rep["error"] += f" (Python {rep['python']}; cubevis needs 3.12 or later)"
    return rep


def kind_from_report(rep: dict) -> Optional[str]:
    """The kind, as the interpreter itself reports it (``envcheck``): conda
    when its prefix has ``conda-meta``; venv when its prefix is not its base
    prefix; else python.  Not "conda" in ``sys.version``: a venv made from a
    conda-forge Python says that too, but its packages are the venv's.
    None when the check could not run."""
    if not rep.get("prefix"):
        return None
    if rep.get("conda"):
        return "conda"
    if rep.get("base_prefix") and os.path.realpath(rep["prefix"]) != os.path.realpath(rep["base_prefix"]):
        return "venv"
    return "python"


def install_method(rep: dict) -> dict:
    """How to upgrade cubevis in an environment, from a check's ``install``
    report: ``{"method", "source"}`` with method one of

        conda     installed as a conda package (the environment's tool)
        checkout  from a local directory, editable or not (source: the path)
        vcs       from a git (or other VCS) URL (source: the URL)
        pip, uv   from a package index, by that installer
        unknown   installed, but nothing says how
    """
    inst = rep.get("install") or {}
    if inst.get("conda"):
        return {"method": "conda", "source": None}
    url = inst.get("url") or ""
    if inst.get("vcs"):
        return {"method": "vcs", "source": url}
    if url.startswith("file://"):
        from urllib.parse import unquote, urlparse
        return {"method": "checkout", "source": unquote(urlparse(url).path),
                "editable": bool(inst.get("editable"))}
    who = (inst.get("installer") or "").lower()
    if who in ("pip", "uv"):
        return {"method": who, "source": None}
    if who == "conda":                 # conda-built wheel record without conda-meta
        return {"method": "conda", "source": None}
    return {"method": "unknown", "source": None}


def describe_method(m: dict) -> str:
    """For messages: how cubevis is installed there."""
    k = m.get("method")
    if k == "checkout":
        return ("from the checkout " if m.get("editable") else "from the directory ") \
            + str(m.get("source"))
    if k == "vcs":
        return f"from {m.get('source')}"
    if k in ("pip", "uv", "conda"):
        return f"by {k}"
    return "by an installer that left no record"


def summary(rep: dict) -> str:
    """One line for a check result."""
    v = rep.get("versions") or {}
    if not rep.get("ok"):
        why = rep.get("error") or "failed"
        if v.get("cubevis"):           # installed but not working: how may say why
            why += f" (cubevis {v['cubevis']}, installed {describe_method(install_method(rep))})"
        return why
    parts = [f"cubevis {v.get('cubevis') or '?'}", f"Python {rep.get('python')}",
             f"ipykernel {v.get('ipykernel') or '?'}"]
    parts.append("visplot data: yes" if rep.get("visplot")
                 else "visplot data: no (xarray-ms missing)")
    parts.append("installed " + describe_method(install_method(rep)))
    return ", ".join(parts)
