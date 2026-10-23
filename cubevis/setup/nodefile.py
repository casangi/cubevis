"""``hosts/<id>/node/<network>.json``: this computer as a node of
*network* -- what it is, and the Python environments enabled on it for
kernels.

In this computer's own directory, with everything else it writes
(``hosts/<id>/``: one writer per directory; one computer's files are one
directory to look at or remove).  Written only by ``python -m
cubevis.setup node-here`` run on that computer (design section 7b);
reruns are merged like the state files (``state.merge``: lock, re-read,
apply only this run's changes).  The GUI side reads them -- directly when
it shares the home directory (``hosts/*/node/<network>.json``), otherwise
over ssh -- and records the environments in ``state.json`` (stage 2b
part iii).

    {"schema": 2,
     "node": {"network", "host" (the id), "hostname" (full name), "user",
              "domain", "home", "home_fs", "shared_home", "addresses",
              "ranges", "query": {"command", "names", "tested", "error"}
              or null, "cubevis" (the setup that wrote it), "updated"},
     "environments": {NAME: {"kind" (conda, venv, python), "python",
                             "prefix",
                             "cubevis_via": {"method", "source"}: how
                               cubevis was installed -- conda, checkout,
                               vcs, pip, uv or unknown (envs.install_method),
                             "tool": the conda/mamba/micromamba that
                               upgrades it, for method conda only,
                             "listed_by": the tool that listed it, if any,
                             "workdir": the kernels' working directory
                               (null: the home directory),
                             "env": {NAME: value} set before Python starts,
                             "launcher": the script a kernel's ssh runs
                               instead of python (``launcher``),
                             "versions": {dist: version}, "python_version",
                             "visplot", "status", "last_checked",
                             "last_error"}}}

The network ``local`` is this computer by itself: environments for kernels
started here, for plots shown here (no ssh).
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Optional

from . import config_dir, host_id
from . import state as S

LOCAL = "local"
SECTIONS = ("environments",)
FIELD_DICTS = ("node",)


def node_dir(host: Optional[str] = None) -> Path:
    """``hosts/<id>/node``: this computer's node files."""
    return config_dir() / "hosts" / (host or host_id()) / "node"


def path(network: str, host: Optional[str] = None) -> Path:
    return node_dir(host) / f"{S.check_name('network', network)}.json"


def launcher_path(network: str, env: str, host: Optional[str] = None) -> Path:
    """``hosts/<id>/node/<network>/<env>.sh``: the environment's launcher
    (``launcher``)."""
    return node_dir(host) / S.check_name("network", network) / f"{S.check_name('environment', env)}.sh"


def mine() -> list:
    """Networks this computer has a node file for."""
    d = node_dir()
    return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []


def all_files() -> list:
    """``(network, host, path)`` of every node file in this directory (a
    shared home holds the other nodes' too)."""
    root = config_dir() / "hosts"
    if not root.is_dir():
        return []
    return sorted((p.stem, p.parent.parent.name, p) for p in root.glob("*/node/*.json"))


def empty(network: str) -> dict:
    return {"schema": S.SCHEMA, "node": {"network": network, "host": host_id()},
            "environments": {}}


class NodeFile:
    """One node file, loaded; ``save`` merges and writes it."""

    def __init__(self, network: str, data: Optional[dict] = None, base: Optional[dict] = None,
                 p: Optional[Path] = None):
        self.network = network
        self.path = p or path(network)
        self.data = data if data is not None else empty(network)
        self._base = base

    @property
    def node(self) -> dict:
        return self.data["node"]

    @property
    def environments(self) -> dict:
        return self.data["environments"]

    def environment_by_python(self, python: str) -> Optional[str]:
        for name, e in self.environments.items():
            if e.get("python") == python:
                return name
        return None


def _migrate(raw: dict, where: str) -> dict:
    v = raw.get("schema")
    if not isinstance(v, int):
        raise S.StateError(f"{where} has no schema number; it was not written by cubevis setup")
    if v > S.SCHEMA:
        raise S.StateError(f"{where} is schema {v}, newer than this cubevis understands "
                           f"({S.SCHEMA}): upgrade cubevis here, or use the newer one")
    raw.setdefault("node", {})
    raw.setdefault("environments", {})
    raw["schema"] = S.SCHEMA
    return raw


def load(network: str, host: Optional[str] = None) -> NodeFile:
    p = path(network, host)
    raw = S._read_json(p)
    if raw is None:
        return NodeFile(network, p=p)
    data = _migrate(raw, str(p))
    return NodeFile(network, data, copy.deepcopy(data), p)


def save(nf: NodeFile) -> Path:
    with S.FileLock(nf.path):
        raw = S._read_json(nf.path)
        theirs = _migrate(raw, str(nf.path)) if raw is not None else None
        merged = S.merge(nf._base, nf.data, theirs, SECTIONS, FIELD_DICTS)
        S._write_json(nf.path, merged)
    S._apply_in_place(nf.data, merged, SECTIONS)
    nf._base = copy.deepcopy(merged)
    return nf.path
