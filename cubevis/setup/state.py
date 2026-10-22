"""What setup knows: ``state.json`` (shared) and ``hosts/<id>/local.json``
(this computer's).

Everything else setup writes (``ssh/config``, ``ssh/rules``) is generated
from these (``sshgen``).  Plain JSON, standard library only, readable and
diffable; written to a temporary file and renamed, mode 600.  Passwords are
never in them -- they stay in the password stores; the state only names the
store and the entry.

Two files, because the configuration directory may be a home directory
shared by several computers (design section 7b):

* ``state.json`` -- networks, nodes, environments, kernels: shared on
  purpose (a GUI on any of those computers sees the same networks).
* ``hosts/<id>/local.json`` -- this computer as the GUI side: its Python,
  its password store, the sessions it started.

**Saving never overwrites what another run wrote.**  ``save`` takes a lock
file next to the file (created with ``O_EXCL``, which is atomic on local
disks and NFSv3+; ``flock`` is not reliable on older NFS), reads the file
again, and applies only what this run changed since it loaded -- record by
record, field by field (``merge``) -- then writes atomically.  Two runs
that change different nodes, or different fields of one, both keep their
changes; a field both changed takes this run's value.  A removed record
stays removed unless the other run changed more than its health fields.

Problems are *flagged*, not deleted: a node or environment that fails a
check gets ``status: "flagged"``, ``last_error`` and ``last_checked``;
``mark_ok`` clears them.  Removing what is flagged is the user's choice
(``setup prune``).

Layout (schema 2)::

    state.json
    {"schema": 2,
     "networks":     {NAME: {"login": {"host", "user", "port", "alias",
                                       "password", "service"},
                             "inside", "user", "password", "service",
                             "node_pattern", "domain", "shared_fs",
                             "default_node", "query": {"host", "command"}}},
     "nodes":        {NAME: {"network", "hostname", "alias", "port", "status",
                             "last_checked", "last_error"}},
     "environments": {NAME: {"network", "kind", "python", "prefix", "tool",
                             "hosts", "cubevis", "versions",
                             "python_version", "visplot", "checked_on",
                             "status", "last_checked", "last_error"}},
     "kernels":      {NAME: {"environment", "launch_timeout"}}}

An environment belongs to one network (design 7b) and is identified by
(network, ``python``); ``hosts`` is ``"all"`` (the network's nodes see it
on a shared filesystem) or a list of nodes.  ``kind``: conda, venv or
python; ``tool``: for conda, the command the user installs and upgrades
packages with (conda, mamba or micromamba).

    hosts/<id>/local.json
    {"schema": 2,
     "local":        {"python", "store", "store_command", "gpg_uid",
                      "last_user"},
     "sessions":     {ID:   {"kernel", "node", "local_pid", "started",
                             "persistent_file"}}}

Schema 1 (stage 2a) was one ``state.json`` with ``local`` and
``sessions`` in it; ``load`` moves them to this computer's file (written at
the first save; loading writes nothing).

A network's ``login.password`` / ``password`` say whether the login host /
the nodes ask for a password (then the wrapper logs in with the stored
one, from ``service``); ``user`` is the user name on the nodes.

Use::

    st = state.load()                      # missing files -> empty
    st.add_network("cv", login_host="login.example.org", user="jo", ...)
    st.add_node("node140", network="cv")
    state.save(st)                         # locked, merged, atomic
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import re
import socket
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from . import config_dir, host_dir

SCHEMA = 2
SHARED_SECTIONS = ("networks", "nodes", "environments", "kernels")
HOST_SECTIONS = ("sessions",)
SECTIONS = SHARED_SECTIONS + HOST_SECTIONS
FLAGGABLE = ("nodes", "environments")
STORES = ("keychain", "secret-tool", "pass", "gpg-file", "command")
# Fields a check writes.  A change to these alone neither keeps a record
# another run removed nor stops this run's removal of it (``merge``).
HEALTH_FIELDS = {"status", "last_checked", "last_error", "checked_on", "cubevis",
                 "versions", "python_version", "visplot", "cubevis_via", "tool"}
KINDS = ("conda", "venv", "python")

LOCK_TIMEOUT = 30.0      # seconds to wait for another run's lock
LOCK_STALE = 120.0       # a lock older than this is a crashed run's (held for milliseconds)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@-]*$")
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$|^[0-9a-fA-F:]+$")


class StateError(ValueError):
    """Bad input to a state operation; the message is for the user."""


def state_path() -> Path:
    """The shared file."""
    return config_dir() / "state.json"


def host_state_path() -> Path:
    """This computer's file."""
    return host_dir() / "local.json"

def now() -> str:
    """Local time to the minute, as recorded in ``last_checked``."""
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M")


def check_name(kind: str, name: str) -> str:
    """Names are what users type and what ends up in ssh's ``Host``
    lines: letters, digits, ``. _ -``, not starting with punctuation."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise StateError(f"{kind} name {name!r}: use letters, digits, '.', '_' and '-' "
                         "(starting with a letter or digit)")
    return name


def normalize_ranges(ranges) -> list:
    """Address ranges as canonical CIDR strings ("10.7.0.0/16"); a bare
    address is its own /32 (/128).  Refuses what does not parse."""
    import ipaddress
    out = []
    for r in ranges or []:
        try:
            out.append(str(ipaddress.ip_network(str(r).strip(), strict=False)))
        except ValueError:
            raise StateError(f"{r!r} is not an address range (like 10.7.0.0/16)") from None
    return out


def check_user(user: str) -> str:
    if not isinstance(user, str) or not _USER_RE.match(user):
        raise StateError(f"user name {user!r}: letters, digits, '.', '_', '-', '@' only")
    return user


def check_host(host: str) -> str:
    if not isinstance(host, str) or not _HOST_RE.match(host):
        raise StateError(f"host name {host!r} does not look like one (letters, digits, "
                         "'.', '-'; or an address)")
    return host


def empty() -> dict:
    """An empty shared state."""
    return {"schema": SCHEMA, **{s: {} for s in SHARED_SECTIONS}}


def empty_host() -> dict:
    """An empty state for this computer."""
    return {"schema": SCHEMA, "local": {}, **{s: {} for s in HOST_SECTIONS}}


def _migrate_1(data: dict) -> dict:
    """Schema 1 -> 2: ``local`` and ``sessions`` leave the shared file.  They
    are kept under ``_host`` for ``migrate`` to hand to the caller."""
    data = dict(data)
    data["_host"] = {"local": data.pop("local", None) or {},
                     "sessions": data.pop("sessions", None) or {}}
    return data


# MIGRATIONS[n] turns a schema-n shared dict into schema n+1.
MIGRATIONS: dict = {1: _migrate_1}


def migrate(data: dict, what: str = "state.json") -> tuple:
    """``(shared, host_part)``: *data* brought up to ``SCHEMA``, and what a
    migration moved out of it for this computer's file (or None).  Refuses
    a newer file (written by a newer cubevis) rather than guess at it."""
    v = data.get("schema")
    if not isinstance(v, int):
        raise StateError(f"{what} has no schema number; it was not written by cubevis setup")
    if v > SCHEMA:
        raise StateError(f"{what} is schema {v}, newer than this cubevis understands "
                         f"({SCHEMA}): upgrade cubevis here, or use the newer one")
    host_part = None
    while v < SCHEMA:
        data = MIGRATIONS[v](data)
        host_part = data.pop("_host", None) or host_part
        v = data["schema"] = v + 1
    for s in SHARED_SECTIONS:
        data.setdefault(s, {})
    return data, host_part


def migrate_host(data: dict, what: str = "local.json") -> dict:
    v = data.get("schema")
    if not isinstance(v, int):
        raise StateError(f"{what} has no schema number; it was not written by cubevis setup")
    if v > SCHEMA:
        raise StateError(f"{what} is schema {v}, newer than this cubevis understands "
                         f"({SCHEMA}): upgrade cubevis here, or use the newer one")
    data["schema"] = SCHEMA
    data.setdefault("local", {})
    for s in HOST_SECTIONS:
        data.setdefault(s, {})
    return data


class State:
    """The state as two plain dicts -- ``.data`` (shared) and ``.host``
    (this computer) -- with accessors.  Nothing is written until ``save``.

    ``State(data)`` takes a dict of either schema (a schema-1 dict's
    ``local`` and ``sessions`` become this computer's, unless *host* is
    given).  Everything in a State built this way counts as changed by
    this run; ``load`` records what was on disk, so that ``save`` writes
    only this run's changes."""

    def __init__(self, data: Optional[dict] = None, path: Optional[Path] = None,
                 host: Optional[dict] = None, host_path: Optional[Path] = None):
        moved = None
        if data is not None:
            self.data, moved = migrate(copy.deepcopy(data))
        else:
            self.data = empty()
        if host is not None:
            self.host = migrate_host(copy.deepcopy(host))
        else:
            self.host = empty_host()
            if moved:
                self.host.update(copy.deepcopy(moved))
        self.path = Path(path) if path is not None else state_path()
        self.host_path = Path(host_path) if host_path is not None else None
        # What each file held when loaded (None: count everything as ours).
        self._base: Optional[dict] = None
        self._base_host: Optional[dict] = None

    # -- sections ---------------------------------------------------------
    @property
    def local(self) -> dict:
        return self.host["local"]

    def section(self, kind: str) -> dict:
        if kind in SHARED_SECTIONS:
            return self.data[kind]
        if kind in HOST_SECTIONS:
            return self.host[kind]
        raise StateError(f"no such section {kind!r}")

    def get(self, kind: str, name: str) -> dict:
        try:
            return self.section(kind)[name]
        except KeyError:
            known = ", ".join(sorted(self.section(kind))) or "none yet"
            raise StateError(f"no {kind[:-1]} {name!r} (known: {known})") from None

    def networks(self) -> dict:
        return self.data["networks"]

    def nodes(self, network: Optional[str] = None) -> dict:
        n = self.data["nodes"]
        return n if network is None else {k: v for k, v in n.items() if v.get("network") == network}

    # -- networks -----------------------------------------------------------
    def add_network(self, name: str, *, login_host: Optional[str], user: str,
                    login_user: Optional[str] = None, login_port: int = 22,
                    login_alias: Optional[str] = None,
                    login_password: bool = False, password: bool = False,
                    service: Optional[str] = None, login_service: Optional[str] = None,
                    node_pattern: Optional[str] = None, domain: Optional[str] = None,
                    shared_fs: bool = True, default_node: Optional[str] = None,
                    query: Optional[dict] = None, inside: Optional[list] = None,
                    replace: bool = False) -> dict:
        """A network: its nodes, and the login host (bastion) in front of
        them -- or none, when the nodes are reached directly.

        ``node_pattern`` (an ssh glob of the nodes' real names) is what the
        wrapper's rules match, so new nodes need no new rule; it defaults to
        ``*.<domain>`` when a domain is given.  The login host's alias in
        the generated ssh config defaults to ``<name>-login``.

        ``inside``: address ranges (CIDR) of the site.  When this computer
        would reach a node from an address in them it is inside the network
        and goes to the node directly; outside, through the login host.
        Decided at each connection (``inside.py``), so a laptop that moves
        does the right thing.  Without ranges the login host is always used.
        """
        check_name("network", name)
        if name in self.networks() and not replace:
            raise StateError(f"network {name!r} exists already")
        check_user(user)
        if login_user:
            check_user(login_user)
        if domain:
            check_host(domain)
        alias = None
        if login_host:
            check_host(login_host)
            alias = check_name("ssh alias", login_alias or f"{name}-login")
            if alias.lower() == login_host.lower():
                raise StateError(f"the login host's alias must differ from its name "
                                 f"{login_host!r} (sshpyk needs that)")
        # None: not asked yet; []: asked, none (always the login host).
        ranges = normalize_ranges(inside) if inside is not None else None
        if (login_password or password) and not (service or login_service):
            service = "cubevis-ssh"
        if query is not None and not (isinstance(query, dict) and query.get("command")):
            raise StateError("query must be {'host': ..., 'command': ...}")
        net = {
            "login": ({"host": login_host, "user": login_user or user,
                       "port": int(login_port or 22), "alias": alias,
                       "password": bool(login_password),
                       "service": login_service or service}
                      if login_host else None),
            "inside": ranges,
            "user": user, "password": bool(password), "service": service,
            "node_pattern": node_pattern or (f"*.{domain}" if domain else None),
            "domain": domain, "shared_fs": bool(shared_fs),
            "default_node": default_node, "query": query,
        }
        self.networks()[name] = net
        return net

    def remove_network(self, name: str) -> None:
        self.get("networks", name)
        users = [n for n, v in self.nodes(name).items()]
        users += [e for e, v in self.section("environments").items() if v.get("network") == name]
        if users:
            raise StateError(f"network {name!r} is still used by {', '.join(sorted(users))}")
        del self.networks()[name]

    # -- nodes --------------------------------------------------------------
    def node_hostname(self, network: str, name: str) -> str:
        """A short name completed with the network's domain."""
        net = self.get("networks", network)
        dom = net.get("domain")
        if dom and "." not in name:
            return f"{name}.{dom}"
        return name

    def add_node(self, name: str, *, network: str, hostname: Optional[str] = None,
                 alias: Optional[str] = None, port: Optional[int] = None,
                 replace: bool = False) -> dict:
        """A node of *network*.  ``hostname`` defaults to *name* completed
        with the network's domain; ``alias`` (its ssh name in the generated
        config) to ``<network>-<name>``."""
        check_name("node", name)
        net = self.get("networks", network)
        if name in self.nodes() and not replace:
            raise StateError(f"node {name!r} exists already "
                             f"(in network {self.nodes()[name].get('network')!r})")
        hn = check_host(hostname or self.node_hostname(network, name))
        al = check_name("ssh alias", alias or f"{network}-{name}")
        if al.lower() == hn.lower():
            raise StateError(f"node {name!r}: the alias must differ from the host name {hn!r} "
                             "(sshpyk needs that)")
        taken = {v.get("alias") for k, v in self.nodes().items() if k != name}
        taken |= {n["login"].get("alias") for n in self.networks().values() if n.get("login")}
        if al in taken:
            raise StateError(f"ssh alias {al!r} is already used")
        node = {"network": network, "hostname": hn, "alias": al,
                "status": "new", "last_checked": None, "last_error": None}
        if port and int(port) != 22:
            node["port"] = int(port)
        self.nodes()[name] = node
        if not net.get("default_node"):
            net["default_node"] = name
        return node

    def remove_node(self, name: str) -> None:
        node = self.get("nodes", name)
        used = [s for s, v in self.section("sessions").items() if v.get("node") == name]
        if used:
            raise StateError(f"node {name!r} has recorded sessions: {', '.join(used)}")
        del self.nodes()[name]
        net = self.networks().get(node.get("network"), {})
        if net.get("default_node") == name:
            rest = sorted(self.nodes(node.get("network")))
            net["default_node"] = rest[0] if rest else None

    # -- environments and kernels ------------------------------------------
    def add_environment(self, name: str, *, network: str, python: str,
                        kind: str = "python", prefix: Optional[str] = None,
                        tool: Optional[str] = None, hosts="all",
                        replace: bool = False) -> dict:
        """An environment of *network*: the interpreter *python* (absolute;
        links kept), on every node (``hosts="all"``) or on the listed ones."""
        check_name("environment", name)
        self.get("networks", network)
        if kind == "mamba":              # a kind until 2026-10-21: the same environments
            kind = "conda"
        if kind not in KINDS:
            raise StateError(f"environment kind {kind!r}: one of {', '.join(KINDS)}")
        if not python.startswith("/"):
            raise StateError(f"environment {name!r}: the interpreter must be an absolute path")
        if name in self.section("environments") and not replace:
            raise StateError(f"environment {name!r} exists already")
        same = self.find_environment(network, python)
        if same and same != name:
            raise StateError(f"{python} is environment {same!r} of {network} already")
        if hosts != "all":
            hosts = sorted(set(hosts))
            for h in hosts:
                node = self.get("nodes", h)
                if node.get("network") != network:
                    raise StateError(f"node {h!r} is not in network {network!r} (an environment "
                                     "belongs to one network)")
        e = {"network": network, "kind": kind, "python": python, "prefix": prefix,
             "tool": tool, "hosts": hosts, "cubevis": None, "versions": {},
             "python_version": None, "visplot": None, "checked_on": None,
             "status": "new", "last_checked": None, "last_error": None}
        self.section("environments")[name] = e
        return e

    def find_environment(self, network: str, python: str) -> Optional[str]:
        """The name of *network*'s environment with interpreter *python*."""
        for n, e in self.section("environments").items():
            if e.get("network") == network and e.get("python") == python:
                return n
        return None

    def environment_on(self, name: str, node: str) -> bool:
        """Whether environment *name* is available on *node*: same network,
        and all of its nodes or this one listed."""
        e = self.get("environments", name)
        n = self.get("nodes", node)
        if n.get("network") != e.get("network"):
            return False
        return e.get("hosts") == "all" or node in (e.get("hosts") or [])

    def add_kernel(self, name: str, *, environment: str, launch_timeout: int = 120,
                   replace: bool = False) -> dict:
        check_name("kernel", name)
        self.get("environments", environment)
        if name in self.section("kernels") and not replace:
            raise StateError(f"kernel {name!r} exists already")
        k = {"environment": environment, "launch_timeout": int(launch_timeout)}
        self.section("kernels")[name] = k
        return k

    def kernel_network(self, kernel: str) -> str:
        env = self.get("environments", self.get("kernels", kernel)["environment"])
        return env["network"]

    # -- health -------------------------------------------------------------
    def flag(self, kind: str, name: str, error: str) -> dict:
        """Record a failed check on a node or environment (kept, not
        deleted)."""
        if kind not in FLAGGABLE:
            raise StateError(f"only {' and '.join(FLAGGABLE)} are flagged")
        rec = self.get(kind, name)
        rec.update(status="flagged", last_error=str(error), last_checked=now())
        return rec

    def mark_ok(self, kind: str, name: str, **fields: Any) -> dict:
        """Record a passed check (and anything it found, e.g. the
        environment's ``cubevis`` version and ``checked_on`` node)."""
        if kind not in FLAGGABLE:
            raise StateError(f"only {' and '.join(FLAGGABLE)} are checked")
        rec = self.get(kind, name)
        rec.update(fields)
        rec.update(status="ok", last_error=None, last_checked=now())
        return rec

    def flagged(self) -> list:
        """``(kind, name, record)`` for everything flagged."""
        return [(k, n, r) for k in FLAGGABLE for n, r in self.section(k).items()
                if r.get("status") == "flagged"]


# --------------------------------------------------------------------------
# Merging: what this run changed, applied to what is on disk now
# --------------------------------------------------------------------------

_GONE = object()     # a record or field that is not there


def _health_only(before, after) -> bool:
    """*after* differs from *before* in health fields only."""
    if not (isinstance(before, dict) and isinstance(after, dict)):
        return False
    keys = set(before) | set(after)
    return all(before.get(k, _GONE) == after.get(k, _GONE) for k in keys - HEALTH_FIELDS)


def _merge_fields(base: dict, ours: dict, theirs: dict) -> dict:
    out = {}
    for k in list(theirs) + [k for k in ours if k not in theirs] + \
            [k for k in base if k not in theirs and k not in ours]:
        b, o, t = base.get(k, _GONE), ours.get(k, _GONE), theirs.get(k, _GONE)
        v = t if o == b else o          # ours wins where we changed it
        if v is not _GONE:
            out[k] = v
    return out


def _merge_record(b, o, t):
    if o == b:
        return t                       # untouched here: whatever is on disk now
    if t == b:
        return o                       # untouched there: ours
    if o is _GONE:                     # removed here, changed there
        return _GONE if _health_only(b, t) else t
    if t is _GONE:                     # removed there, changed here
        return _GONE if _health_only(b, o) else o
    if isinstance(b, dict) or b is _GONE:
        if isinstance(o, dict) and isinstance(t, dict):
            return _merge_fields(b if isinstance(b, dict) else {}, o, t)
    return o


def merge(base: Optional[dict], ours: dict, theirs: Optional[dict], sections,
          field_dicts=("local",)) -> dict:
    """Three-way merge of one file: *base* (as loaded; None: nothing),
    *ours* (now, in this run), *theirs* (on disk now; None: no file).
    Sections merge record by record and, for a record both changed, field
    by field; *field_dicts* (``local``) merge field by field."""
    base = base or {}
    theirs = theirs or {}
    out = {"schema": SCHEMA}
    for fd in field_dicts:                # first: they describe the file
        if fd in ours or fd in theirs:
            out[fd] = None
    for sec in sections:
        b, o, t = base.get(sec) or {}, ours.get(sec) or {}, theirs.get(sec) or {}
        res = {}
        for name in list(t) + [n for n in o if n not in t] + \
                [n for n in b if n not in t and n not in o]:
            r = _merge_record(b.get(name, _GONE), o.get(name, _GONE), t.get(name, _GONE))
            if r is not _GONE:
                res[name] = r
        out[sec] = res
    for fd in field_dicts:
        if fd in ours or fd in theirs:
            out[fd] = _merge_fields(base.get(fd) or {}, ours.get(fd) or {},
                                    theirs.get(fd) or {})
    return out


def _apply_in_place(target: dict, result: dict, sections=SECTIONS) -> None:
    """Make *target* equal to *result*, keeping the identity of the
    section and record dicts that stay (callers hold references to them,
    e.g. a network's record across several saves)."""
    result = copy.deepcopy(result)
    for k in list(target):
        if k not in result:
            del target[k]
    for k, v in result.items():
        cur = target.get(k)
        if isinstance(cur, dict) and isinstance(v, dict) and k != "schema":
            if k not in sections:            # a field dict (``local``)
                cur.clear()
                cur.update(v)
                continue
            for name in list(cur):
                if name not in v:
                    del cur[name]
            for name, rec in v.items():
                old = cur.get(name)
                if isinstance(old, dict) and isinstance(rec, dict):
                    old.clear()
                    old.update(rec)
                else:
                    cur[name] = rec
        else:
            target[k] = v


# --------------------------------------------------------------------------
# The lock
# --------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True              # exists, not ours
    return True


class FileLock:
    """``<file>.lock``, created with ``O_EXCL``; it holds ``host pid time``.

    A lock is stale -- a run that crashed -- when it is this computer's and
    its process is gone, or when it is older than ``LOCK_STALE`` (saves
    hold it for milliseconds).  A stale lock is renamed away first, so that
    two runs breaking it at once cannot both proceed."""

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT):
        self.path = Path(str(path) + ".lock")
        self.timeout = timeout
        self.me = f"{socket.gethostname()} {os.getpid()}"

    def _stale(self) -> Optional[str]:
        try:
            text = self.path.read_text()
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return None
        parts = text.split()
        host, pid = (parts[0], parts[1]) if len(parts) >= 2 else ("?", "0")
        if host == socket.gethostname() and pid.isdigit() and not _pid_alive(int(pid)):
            return f"its process {pid} has ended"
        if age > LOCK_STALE:
            return f"it is {int(age)} s old"
        return None

    def holder(self) -> str:
        try:
            return self.path.read_text().strip() or "?"
        except OSError:
            return "?"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        delay = 0.02
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if self._stale():
                    aside = Path(f"{self.path}.stale.{os.getpid()}")
                    try:
                        os.rename(self.path, aside)
                        os.unlink(aside)
                    except OSError:
                        pass
                    continue
                if time.monotonic() > deadline:
                    raise StateError(
                        f"{self.path.with_suffix('')} is locked by another setup run "
                        f"({self.holder()}). If none is running, remove {self.path}") from None
                time.sleep(delay)
                delay = min(delay * 2, 0.25)
                continue
            with os.fdopen(fd, "w") as f:
                f.write(f"{self.me} {time.time():.0f}\n")
            return self

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass
        return False


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def _read_json(p: Path) -> Optional[dict]:
    """The file's dict, or None if it does not exist.  A file that is not
    valid JSON raises ``StateError`` naming it (never silently replaced:
    it may be the only record)."""
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        raise StateError(f"{p} cannot be read ({exc}); fix or move it aside") from None
    if not isinstance(data, dict):
        raise StateError(f"{p} is not a cubevis state file")
    return data


def load(path: Optional[Path] = None, host_path: Optional[Path] = None,
         host: bool = True) -> State:
    """The state from *path* (default ``state_path()``) and this computer's
    *host_path* (default ``host_state_path()``); missing files are empty.
    ``host=False`` skips this computer's file (the inside test, run at
    every ssh connection, needs only the networks).  Writes nothing."""
    p = Path(path) if path is not None else state_path()
    raw = _read_json(p)
    shared, moved = migrate(raw, p.name) if raw is not None else (empty(), None)
    st = State(path=p)
    st.data = shared
    st._base = copy.deepcopy(shared) if raw is not None else None
    if host:
        hp = Path(host_path) if host_path is not None else host_state_path()
        st.host_path = hp
        hraw = _read_json(hp)
        if hraw is not None:
            st.host = migrate_host(hraw, str(hp))
            st._base_host = copy.deepcopy(st.host)
        elif moved:
            # From a schema-1 file: this computer's from now on.  Counted as
            # this run's change, so the first save writes it.
            st.host.update(copy.deepcopy(moved))
    return st


def _write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except OSError:
        pass
    text = json.dumps(data, indent=2, sort_keys=False) + "\n"
    try:
        if p.read_text() == text:
            return
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=f".{p.stem}.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _save_one(p: Path, ours: dict, base: Optional[dict], sections, host: bool) -> dict:
    with FileLock(p):
        raw = _read_json(p)
        if raw is None:
            theirs = None
        elif host:
            theirs = migrate_host(raw, str(p))
        else:
            theirs = migrate(raw, p.name)[0]
        merged = merge(base, ours, theirs, sections)
        _write_json(p, merged)
    return merged


def save(st: State, path: Optional[Path] = None) -> Path:
    """Write the shared file (*path*, default ``st.path``) and this
    computer's (``st.host_path``, default ``host_state_path()``), each under
    its lock, merged with whatever another run wrote since this one loaded
    (module docstring).  Afterwards ``st`` holds the merged state, in the
    same dicts as before.  Returns the shared file's path."""
    p = Path(path) if path is not None else st.path
    merged = _save_one(p, st.data, st._base, SHARED_SECTIONS, host=False)
    _apply_in_place(st.data, merged)
    st._base = copy.deepcopy(merged)
    st.path = p
    hp = st.host_path or host_state_path()
    hmerged = _save_one(hp, st.host, st._base_host, HOST_SECTIONS, host=True)
    _apply_in_place(st.host, hmerged)
    st._base_host = copy.deepcopy(hmerged)
    st.host_path = hp
    return p
