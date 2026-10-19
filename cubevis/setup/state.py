"""``~/.config/cubevis/state.json``: what setup knows, in one file.

Everything else setup writes (``ssh/config``, ``ssh/rules``) is generated
from it (``sshgen``).  Plain JSON, standard library only, readable and
diffable; written to a temporary file and renamed, mode 600.  Passwords are
never in it -- they stay in the password stores; the state only names the
store and the entry.

Problems are *flagged*, not deleted: a node or environment that fails a
check gets ``status: "flagged"``, ``last_error`` and ``last_checked``;
``mark_ok`` clears them.  Removing what is flagged is the user's choice
(``setup prune``, stage 2).

Layout (schema 1; design section 3)::

    {"schema": 1,
     "local":        {"conda", "env", "python", "store", "store_command",
                      "gpg_uid", "ssh_include"},
     "networks":     {NAME: {"login": {"host", "user", "port", "alias",
                                       "password", "service"},
                             "user", "password", "service",
                             "node_pattern", "domain", "shared_fs",
                             "default_node", "query": {"host", "command"}}},
     "nodes":        {NAME: {"network", "hostname", "alias", "port", "status",
                             "last_checked", "last_error"}},
     "environments": {NAME: {"network", "python", "conda", "env", "cubevis",
                             "checked_on", "status", "last_checked",
                             "last_error"}},
     "kernels":      {NAME: {"environment", "launch_timeout"}},
     "sessions":     {ID:   {"kernel", "node", "local_pid", "started",
                             "persistent_file"}}}

A network's ``login.password`` / ``password`` say whether the login host /
the nodes ask for a password (then the wrapper logs in with the stored
one, from ``service``); ``user`` is the user name on the nodes.

Use::

    st = state.load()                      # missing file -> empty schema 1
    st.add_network("cv", login_host="login.example.org", user="jo", ...)
    st.add_node("node140", network="cv")
    state.save(st)                         # atomic
"""
from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

from . import config_dir

SCHEMA = 1
SECTIONS = ("networks", "nodes", "environments", "kernels", "sessions")
FLAGGABLE = ("nodes", "environments")
STORES = ("keychain", "secret-tool", "pass", "gpg-file", "command")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class StateError(ValueError):
    """Bad input to a state operation; the message is for the user."""


def state_path() -> Path:
    return config_dir() / "state.json"


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


def empty() -> dict:
    return {"schema": SCHEMA, "local": {}, **{s: {} for s in SECTIONS}}


# Migrations: MIGRATIONS[n] turns a schema-n dict into schema n+1.  None
# yet; the hook is here so a later schema change is one function.
MIGRATIONS: dict = {}


def migrate(data: dict) -> dict:
    """Bring *data* up to ``SCHEMA``; refuse a newer file (written by a
    newer cubevis) rather than guess at it."""
    v = data.get("schema")
    if not isinstance(v, int):
        raise StateError("state.json has no schema number; it was not written by cubevis setup")
    if v > SCHEMA:
        raise StateError(f"state.json is schema {v}, newer than this cubevis understands "
                         f"({SCHEMA}): upgrade cubevis here, or use the newer one")
    while v < SCHEMA:
        data = MIGRATIONS[v](data)
        v = data["schema"] = v + 1
    for s in ("local",) + SECTIONS:
        data.setdefault(s, {})
    return data


class State:
    """The state as a plain dict (``.data``) with accessors.  Nothing is
    written until ``save``."""

    def __init__(self, data: Optional[dict] = None, path: Optional[Path] = None):
        self.data = migrate(copy.deepcopy(data)) if data is not None else empty()
        self.path = Path(path) if path is not None else state_path()

    # -- sections ---------------------------------------------------------
    @property
    def local(self) -> dict:
        return self.data["local"]

    def section(self, kind: str) -> dict:
        if kind not in SECTIONS:
            raise StateError(f"no such section {kind!r}")
        return self.data[kind]

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
    def add_network(self, name: str, *, login_host: str, user: str,
                    login_user: Optional[str] = None, login_port: int = 22,
                    login_alias: Optional[str] = None,
                    login_password: bool = False, password: bool = False,
                    service: Optional[str] = None, login_service: Optional[str] = None,
                    node_pattern: Optional[str] = None, domain: Optional[str] = None,
                    shared_fs: bool = True, default_node: Optional[str] = None,
                    query: Optional[dict] = None, replace: bool = False) -> dict:
        """A network: one login host and the nodes behind it.

        ``node_pattern`` (an ssh glob of the nodes' real names) is what the
        wrapper's rules match, so new nodes need no new rule; it defaults to
        ``*.<domain>`` when a domain is given.  The login host's alias in
        the generated ssh config defaults to ``<name>-login``.
        """
        check_name("network", name)
        if name in self.networks() and not replace:
            raise StateError(f"network {name!r} exists already")
        if not login_host:
            raise StateError(f"network {name!r}: a login host is needed")
        alias = check_name("ssh alias", login_alias or f"{name}-login")
        if alias.lower() == login_host.lower():
            raise StateError(f"the login host's alias must differ from its name {login_host!r} "
                             "(sshpyk needs that)")
        if (login_password or password) and not (service or login_service):
            service = "cubevis-ssh"
        if query is not None and not (isinstance(query, dict) and query.get("command")):
            raise StateError("query must be {'host': ..., 'command': ...}")
        net = {
            "login": {"host": login_host, "user": login_user or user,
                      "port": int(login_port or 22), "alias": alias,
                      "password": bool(login_password),
                      "service": login_service or service},
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
        hn = hostname or self.node_hostname(network, name)
        al = check_name("ssh alias", alias or f"{network}-{name}")
        if al.lower() == hn.lower():
            raise StateError(f"node {name!r}: the alias must differ from the host name {hn!r} "
                             "(sshpyk needs that)")
        taken = {v.get("alias") for k, v in self.nodes().items() if k != name}
        taken |= {n["login"].get("alias") for n in self.networks().values()}
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
                        conda: Optional[str] = None, env: Optional[str] = None,
                        replace: bool = False) -> dict:
        check_name("environment", name)
        self.get("networks", network)
        if name in self.section("environments") and not replace:
            raise StateError(f"environment {name!r} exists already")
        e = {"network": network, "python": python, "conda": conda, "env": env,
             "cubevis": None, "checked_on": None, "status": "new",
             "last_checked": None, "last_error": None}
        self.section("environments")[name] = e
        return e

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


def load(path: Optional[Path] = None) -> State:
    """The state from *path* (default ``state_path()``); a missing file is
    an empty state.  A file that is not valid JSON raises ``StateError``
    naming it (never silently replaced: it may be the only record)."""
    p = Path(path) if path is not None else state_path()
    if not p.exists():
        return State(path=p)
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        raise StateError(f"{p} cannot be read ({exc}); fix or move it aside") from None
    if not isinstance(data, dict):
        raise StateError(f"{p} is not a cubevis state file")
    return State(data, path=p)


def save(st: State, path: Optional[Path] = None) -> Path:
    """Write atomically (temporary file in the same directory, then
    ``os.replace``), mode 600, directory mode 700."""
    p = Path(path) if path is not None else st.path
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except OSError:
        pass
    text = json.dumps(st.data, indent=2, sort_keys=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=str(p.parent))
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
    st.path = p
    return p
