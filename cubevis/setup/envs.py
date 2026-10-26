"""Python environments for kernels: what a check's report means.

The finding and the checking run where the environment is -- on a node,
through the discovery script (``discover``, sent over ssh); here, the
same functions directly -- and give ``envcheck`` reports.  This module
interprets them on the computer the plots are on:

* ``interpret`` -- ``ok`` (it can run kernels) and ``error`` (why not, one
  line), from a raw report;
* ``kind_from_report`` -- conda, venv or python, as the interpreter itself
  reports it;
* ``install_method`` / ``describe_method`` -- how cubevis got there, which
  decides how it is upgraded (design section 8);
* ``summary`` -- one line for messages.

Kinds (design section 7b): ``conda`` (a conda-format environment), ``venv``
(``python -m venv``), ``python`` (a plain interpreter).

An environment is identified by its interpreter's path, absolute but not
with symbolic links resolved (a venv's ``bin/python`` is a link to the
base interpreter; resolving it would lose the venv).
"""
from __future__ import annotations

import os
import re
from typing import Optional

from . import data_file
from .discover import (TOOLS, candidates, conda_envs, conda_tools, cubevis_installed,  # noqa: F401
                       describe_path, interpreter, kind_of, norm)

KINDS = ("conda", "venv", "python")
CHECK_TIMEOUT = 300          # seconds: the first import from a cold NFS cache is slow


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
    """Run *python* here with ``envcheck.py``: its report, interpreted."""
    from .discover import check as _check
    return interpret(_check(python, data_file("envcheck.py").read_text(), timeout=timeout))


def interpret(rep: Optional[dict]) -> dict:
    """A copy of envcheck's report *rep* with ``ok`` (it can run kernels)
    and ``error`` (why not, one line)."""
    rep = dict(rep or {"error": "not checked"})
    if "versions" not in rep:                  # the check gave no report
        rep["ok"] = False
        rep.setdefault("error", "not checked")
        return rep
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
    # normpath, not realpath: the paths are the node's, not this computer's.
    if rep.get("base_prefix") and os.path.normpath(rep["prefix"]) != os.path.normpath(rep["base_prefix"]):
        return "venv"
    return "python"


def install_method(rep: dict) -> dict:
    """How to upgrade cubevis in an environment, from a check's ``install``
    report: ``{"method", "source"}`` with method one of

        conda     installed as a conda package (the environment's tool)
        checkout  from a local directory, editable or not (source: the path)
        file      from a local wheel or sdist file (source: the file)
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
        path = unquote(urlparse(url).path)
        if inst.get("archive") or path.endswith((".whl", ".tar.gz", ".zip")):
            return {"method": "file", "source": path}
        return {"method": "checkout", "source": path, "editable": bool(inst.get("editable"))}
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
    if k == "file":
        return f"from the file {m.get('source')}"
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
