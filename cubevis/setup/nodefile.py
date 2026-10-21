"""``remote/<network>/<id>.json``: this computer as a node of *network* --
what it is, and the Python environments enabled on it for kernels.

Written only by ``python -m cubevis.setup node-here`` run on that computer
(design section 7b), so each file has one writer; reruns are merged like
the state files (``state.merge``: lock, re-read, apply only this run's
changes).  The GUI side reads these files -- directly when it shares the
home directory, otherwise over ssh -- and records the environments in
``state.json`` (stage 2b part iii).

    {"schema": 2,
     "node": {"network", "host" (the id), "hostname" (full name), "user",
              "domain", "home", "home_fs", "shared_home", "addresses",
              "ranges", "query": {"command", "names", "tested"} or null,
              "cubevis" (the setup that wrote it), "updated"},
     "environments": {NAME: {"kind", "python", "prefix", "tool",
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


def remote_dir() -> Path:
    return config_dir() / "remote"


def path(network: str, host: Optional[str] = None) -> Path:
    return remote_dir() / S.check_name("network", network) / f"{host or host_id()}.json"


def mine() -> list:
    """Networks this computer has a node file for."""
    d = remote_dir()
    if not d.is_dir():
        return []
    return sorted(p.parent.name for p in d.glob(f"*/{host_id()}.json"))


def all_files() -> list:
    """``(network, host, path)`` of every node file in this directory (a
    shared home holds the other nodes' too)."""
    d = remote_dir()
    if not d.is_dir():
        return []
    return sorted((p.parent.name, p.stem, p) for p in d.glob("*/*.json"))


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
