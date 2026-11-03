"""The ssh files generated from the state: ``ssh/config`` and ``ssh/rules``,
in this computer's directory (``hosts/<id>/ssh``).

``ssh/config`` has one ``Host`` per login host, per hop before it (the
user's ``ProxyJump gw,ssh``: ``<login alias>-hop1`` for gw), and per node,
under the aliases the state gives them, so the kernels (``sshpyk
--ssh-config``) and setup's checks reach every node through its network's
login host with shared connections (ControlMaster) -- the user's own
``~/.ssh/config`` is neither needed nor changed.  Each block carries the
login options recorded for it (``sshopts``, notes section 55), ``BatchMode
yes`` (nothing in a kernel's ssh can ask; the wrapper logs in with
``BatchMode=no`` itself where it may), and the known-hosts files: setup's
own first (``known_hosts_path``, shared by the computers sharing the
configuration directory, so a key recorded from one is known to all), then
the user's, with ``StrictHostKeyChecking accept-new`` -- a host never seen
is recorded on first contact (a cluster's node reached without setup, say),
a changed key is refused.

``ssh/rules`` tells the wrapper (``data/ssh``) which real host names ask for
a password and where it is stored: each network's login host, and the
network's ``node_pattern`` (a glob, so a node added later needs no new
rule).  Login hosts come first: the wrapper takes the first rule that
matches, and a login host can match its own network's node pattern.

Both are regenerated from the state whenever it changes and rewritten only
when their text changes (``write`` says which did).  They are per computer
(the rules name this computer's password store; the inside test runs its
Python), as is the wrapper copy in ``hosts/<id>/bin``: computers sharing a
home directory may run different cubevis versions.  The connection
sockets (``ControlPath``) are on local disk (``runtime_dir``).  The format of the
rules is the wrapper's (its header, "Rules"); the keys per password store
are those of the bash setup's ``store_rule_keys``.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from . import config_dir, data_file, host_dir, runtime_dir, runtime_path
from .state import State, StateError

# Shared-connection options every generated Host gets.
CONTROL_PERSIST = "8h"
SERVER_ALIVE_INTERVAL = 30
SERVER_ALIVE_COUNT_MAX = 6

# A Unix socket path is limited to 104 bytes on macOS (108 on Linux):
# the directory, "/", 40 for %C, and up to 17 that ssh adds while creating it.
_SOCKET_LIMIT = 104
_SOCKET_EXTRA = 58


def ssh_dir() -> Path:
    return host_dir() / "ssh"


def config_path() -> Path:
    return ssh_dir() / "config"


def rules_path() -> Path:
    return ssh_dir() / "rules"


def cm_dir() -> Path:
    """ssh's shared-connection sockets: local disk, per user."""
    return runtime_path() / "cm"


def bin_dir() -> Path:
    return host_dir() / "bin"


def wrapper_path() -> Path:
    return bin_dir() / "ssh"


def known_hosts_path() -> Path:
    """Setup's own known-hosts file (shared; first in every block, so new
    keys go there).  ``CUBEVIS_SETUP_KNOWN_HOSTS``: another (the tests)."""
    kh = os.environ.get("CUBEVIS_SETUP_KNOWN_HOSTS")
    return Path(kh) if kh else config_dir() / "ssh_known_hosts"


def secrets_dir() -> Path:
    """gpg-file passwords: shared (with a shared home, so is ~/.gnupg)."""
    return config_dir() / "secrets"


# --------------------------------------------------------------------------
# Password store -> rule keys
# --------------------------------------------------------------------------

def rule_keys(local: dict, service: str, account: str) -> str:
    """The wrapper's keys for one stored password (wrapper header, "Keys").
    ``command=`` takes the rest of the line, so it comes last."""
    store = local.get("store")
    if not store:
        raise StateError("no password store chosen yet (local.store)")
    base = f"service={service} account={account}"
    if store == "keychain":
        return f"{base} backend=security"
    if store == "secret-tool":
        return f"{base} backend=secret-tool"
    if store == "pass":
        return f"{base} backend=pass pass_name={service}/{account}"
    if store == "gpg-file":
        return f"{base} backend=command command=gpg -q -d {secrets_dir()}/{service}-{account}.gpg"
    if store == "command":
        cmd = (local.get("store_command") or "").strip()
        if not cmd:
            raise StateError("store 'command' needs local.store_command")
        cmd = cmd.replace("@SERVICE@", service).replace("@ACCOUNT@", account)
        return f"{base} backend=command command={cmd}"
    raise StateError(f"unknown password store {store!r}")


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------

def _quote(path: str) -> str:
    return f'"{path}"' if any(ch.isspace() for ch in path) and not path.startswith('"') else path


def _known_files(user_files) -> list:
    """Setup's known-hosts file, then the user's that are not the same."""
    own = str(known_hosts_path())
    out = [own]
    for p in user_files or []:
        if os.path.expanduser(p) != os.path.expanduser(own) and p not in out:
            out.append(p)
    return out


def _host_block(alias: str, hostname: str, user: Optional[str], port: Optional[int],
                proxy_jump: Optional[str], extra=(), source: Optional[str] = None,
                known=(), saved: Optional[str] = None) -> list:
    """One ``Host`` block.  *extra*: ``(Option, value)`` pairs from the
    user's ssh configuration for *source* (``sshopts.login_options``, as
    recorded on *saved*); *known*: the user's known-hosts files."""
    lines = [f"Host {alias}", f"    HostName {hostname}"]
    if user:
        lines.append(f"    User {user}")
    if port and int(port) != 22:
        lines.append(f"    Port {int(port)}")
    if proxy_jump:
        lines.append(f"    ProxyJump {proxy_jump}")
    if extra:
        lines.append(f"    # from your ssh configuration (ssh -G {source}"
                     + (f", recorded {saved[:10]}" if saved else "") + ")")
        lines += [f"    {k} {v}" for k, v in extra]
    lines += ["    BatchMode yes",
              "    UserKnownHostsFile " + " ".join(_quote(p) for p in _known_files(known)),
              "    StrictHostKeyChecking accept-new",
              "    ControlMaster auto",
              f"    ControlPath {cm_dir()}/%C",
              f"    ControlPersist {CONTROL_PERSIST}",
              f"    ServerAliveInterval {SERVER_ALIVE_INTERVAL}",
              f"    ServerAliveCountMax {SERVER_ALIVE_COUNT_MAX}"]
    return lines


def ssh_name(rec: dict, hostname_key: str = "hostname") -> tuple:
    """``(name, port)`` to ask the user's ssh about for a login host or
    node record: the name the user gave setup (``ssh_name``: ``cvpost106``,
    matched by their ``Host cvpost*``; its port too), else its real name and
    setup's port."""
    if rec.get("ssh_name"):
        return rec["ssh_name"], None          # the user's configuration has its port
    port = rec.get("port")
    return rec[hostname_key], (int(port) if port and int(port) != 22 else None)


def live_hops(login: dict) -> list:
    """The hops before a login host, from the user's ssh now: its ``via``
    (the earlier hops of the user's ``ProxyJump``), else its own
    ``ProxyJump`` in the user's configuration (``sshopts.parse_hops``)."""
    from . import sshopts
    via = login.get("via")
    if not via:
        name, port = ssh_name(login, "host")
        via = sshopts.one(sshopts.view(name, port), "proxyjump")
    return sshopts.parse_hops(via) if via else []


def hops_of(login: dict) -> list:
    """The hops recorded for a login host (``hops``, notes section 55); a
    record from before that: as the user's ssh gives them now."""
    return login["hops"] if "hops" in login else live_hops(login)


def entries(st: State) -> list:
    """Every ``Host`` block of the generated file, in order: per network
    (name order) the hops before its login host, the login host, its nodes
    (name order).  Each a dict: ``alias``, ``hostname``, ``user``, ``port``,
    ``jump`` (the alias before it), ``name``/``qport`` (what to ask the
    user's ssh about), ``route`` (the user's ProxyCommand is the route),
    ``network``, ``label``."""
    out = []
    for name in sorted(st.networks()):
        net = st.networks()[name]
        login = net.get("login")
        if login:
            prev = None
            hops = hops_of(login)
            for i, h in enumerate(hops, 1):
                a = f"{login['alias']}-hop{i}"
                out.append({"alias": a, "hostname": h["host"], "user": h.get("user"),
                            "port": h.get("port"), "jump": prev, "name": h.get("ssh_name") or h["host"],
                            "qport": h.get("ssh_port"), "route": False, "network": name,
                            "label": f"hop {i} to {login['host']} ({h['host']})"})
                prev = a
            n_, p_ = ssh_name(login, "host")
            out.append({"alias": login["alias"], "hostname": login["host"], "user": login["user"],
                        "port": login.get("port"), "jump": prev, "name": n_, "qport": p_,
                        "route": not hops, "network": name,
                        "label": f"login host {login['host']}"})
        for nname in sorted(st.nodes(name)):
            node = st.nodes()[nname]
            n_, p_ = ssh_name(node)
            out.append({"alias": node["alias"], "hostname": node["hostname"], "user": net["user"],
                        "port": node.get("port"), "jump": None, "name": n_, "qport": p_,
                        "route": not login, "network": name, "label": f"node {nname}"})
    return out


def recorded(st: State, alias: str) -> Optional[dict]:
    return (st.local.get("ssh_options") or {}).get(alias)


def options_for(st: State, e: dict) -> dict:
    """What an entry's block carries: as recorded (``sshopts.snapshot``),
    else -- not recorded yet -- the user's ssh now."""
    from . import sshopts
    return recorded(st, e["alias"]) or sshopts.snapshot(e["name"], e["qport"], route=e["route"])


def fill_missing(st: State) -> list:
    """Record what is not recorded yet: the hops of each login host
    (``hops``, in the shared state) and each entry's options (this
    computer's).  Returns the aliases recorded."""
    from . import sshopts
    for net in st.networks().values():
        login = net.get("login")
        if login and "hops" not in login:
            login["hops"] = live_hops(login)
    rec = dict(st.local.get("ssh_options") or {})
    done = []
    for e in entries(st):
        if e["alias"] not in rec:
            rec[e["alias"]] = sshopts.snapshot(e["name"], e["qport"], route=e["route"])
            done.append(e["alias"])
    if done:
        st.local["ssh_options"] = rec
    return done


def changes(st: State, aliases=None) -> list:
    """How the user's ssh differs now from what was recorded: per entry
    (all, or *aliases*) ``{"alias", "label", "name", "added", "removed"}``,
    and per login host whose hops changed ``{..., "hops": (old, new)}``."""
    from . import sshopts
    out = []
    for e in entries(st):
        if aliases is not None and e["alias"] not in aliases:
            continue
        r = recorded(st, e["alias"])
        if r is None:
            continue
        d = sshopts.differ(r, sshopts.snapshot(e["name"], e["qport"], route=e["route"]))
        if d:
            out.append({"alias": e["alias"], "label": e["label"], "name": e["name"], **d})
    for nname, net in sorted(st.networks().items()):
        login = net.get("login")
        if not login or "hops" not in login:
            continue
        if aliases is not None and login["alias"] not in aliases:
            continue
        def show(hs):
            return ",".join(((h.get("user") + "@") if h.get("user") else "") + h["host"]
                            + (f":{h['port']}" if int(h.get("port") or 22) != 22 else "") for h in hs)
        now = live_hops(login)
        if show(now) != show(login["hops"]):
            out.append({"alias": login["alias"], "label": f"login host {login['host']}",
                        "name": ssh_name(login, "host")[0], "added": [], "removed": [],
                        "hops": (show(login["hops"]) or "directly", show(now) or "directly")})
    return out


def adopt(st: State, aliases) -> None:
    """Record the user's ssh as it is now for *aliases* (and the hops of a
    login host among them)."""
    from . import sshopts
    for net in st.networks().values():
        login = net.get("login")
        if login and login["alias"] in aliases:
            login["hops"] = live_hops(login)
    rec = dict(st.local.get("ssh_options") or {})
    for e in entries(st):
        if e["alias"] in aliases:
            rec[e["alias"]] = sshopts.snapshot(e["name"], e["qport"], route=e["route"])
    st.local["ssh_options"] = rec


def network_aliases(st: State, network: str, node: Optional[str] = None) -> list:
    """The aliases of *network*'s hops and login host, and of *node* (all its
    nodes when None)."""
    out = []
    for e in entries(st):
        if e["network"] != network:
            continue
        if e["label"].startswith("node ") and node is not None and \
                e["alias"] != st.nodes()[node]["alias"]:
            continue
        out.append(e["alias"])
    return out


def config_text(st: State) -> str:
    """``ssh/config`` for *st*: networks in name order, each its hops, login
    host and nodes (``entries``).  Each block carries the login options
    recorded for that host (``options_for``)."""
    out = ["# Written by cubevis setup from state.json; changes made here are",
           "# lost when it is written again (use python -m cubevis.setup).",
           "# The kernels use this file directly; your ~/.ssh/config includes it",
           "# only if you chose that."]
    ents = entries(st)
    for name in sorted(st.networks()):
        net = st.networks()[name]
        login = net.get("login")
        ranges = net.get("inside") or []
        out.append("")
        out.append(f"# network {name}" + ("" if login else " (nodes reached directly)"))
        # Never through the login host (none), or through it when this
        # computer is outside the network: a Match block decided at each
        # connection -- also without inside ranges (then "outside" unless
        # CUBEVIS_ROUTE_<NET> says direct; inside.py).
        aliases, first = [], True
        for e in ents:
            if e["network"] != name:
                continue
            if e["label"].startswith("node "):
                aliases.append(e["alias"])
            o = options_for(st, e)
            if not first or e["label"].startswith("node "):
                out.append("")
            first = False
            out += _host_block(e["alias"], e["hostname"], e["user"], e["port"], e["jump"],
                               [tuple(x) for x in o.get("options") or []], e["name"],
                               known=o.get("known_hosts") or [], saved=o.get("saved")
                               if recorded(st, e["alias"]) else None)
        if login and aliases:
            from .inside import route_var
            out.append("")
            out.append((f"# Outside {', '.join(ranges)}" if ranges else "# Always (no inside ranges)")
                       + f": through {login['alias']}, unless {route_var(name)}=direct"
                       " (python -m cubevis.setup.inside -v " + name + " says which)")
            out.append(f"Match originalhost {','.join(aliases)} !exec \"{inside_command(st, name)}\"")
            out.append(f"    ProxyJump {login['alias']}")
    return "\n".join(out) + "\n"


def inside_command(st: State, network: str) -> str:
    """The command ssh runs (``Match exec``) to ask whether this computer
    is inside *network*: the local Python setup recorded, else this one."""
    import shlex
    import sys
    py = st.local.get("python") or sys.executable
    return f"{shlex.quote(py)} -m cubevis.setup.inside {shlex.quote(network)}"


def _password_hosts(st: State) -> list:
    """Names of the networks with a host that asks for a (stored) password."""
    return [n for n, net in sorted(st.networks().items())
            if (net.get("password") and not net.get("twofactor"))
            or ((net.get("login") or {}).get("password")
                and not (net.get("login") or {}).get("twofactor"))]


def rules_text(st: State) -> str:
    """``ssh/rules`` for *st*: login hosts first (see module docstring),
    then each network's nodes -- by ``node_pattern`` and the clusters'
    patterns, and by name the nodes none of them covers.

    A hop marked ``twofactor`` (``setup HOST --2fa``) gets ``twofactor=yes``
    instead of a stored password: the wrapper never logs in to it itself;
    with a terminal it lets the user log in once (shared for
    ``ControlPersist``), without one it stops, naming the command."""
    import fnmatch
    out = ["# Written by cubevis setup from state.json: the hosts that ask for a",
           "# password, and where the wrapper finds it. Real host names, not aliases."]
    store = bool(st.local.get("store"))
    if not store and _password_hosts(st):
        # Another computer sharing this directory set these networks up;
        # this one has not chosen its password store yet (setup asks at
        # the first login that needs it).
        out.append("# No password store chosen on this computer yet: "
                   + ", ".join(_password_hosts(st)) + " left out.")
    lines_login, lines_nodes = [], []
    for name in sorted(st.networks()):
        net = st.networks()[name]
        login = net.get("login") or {}
        if login.get("twofactor"):
            lines_login.append(f"{login['host']}  twofactor=yes")
        elif login.get("password") and store:
            svc = login.get("service") or net.get("service") or "cubevis-ssh"
            lines_login.append(f"{login['host']}  {rule_keys(st.local, svc, login['user'])}")
        if net.get("twofactor"):
            keys = "twofactor=yes"
        elif net.get("password") and store:
            svc = net.get("service") or login.get("service") or "cubevis-ssh"
            keys = rule_keys(st.local, svc, net["user"])
        else:
            continue
        # The network's pattern (``*.<domain>``) and its clusters' (a node
        # added later needs no new rule), then the nodes no pattern covers
        # -- one typed by its short name, say.
        pats = ([net["node_pattern"]] if net.get("node_pattern") else []) + sorted(
            {c["pattern"] for c in st.section("clusters").values() if c.get("network") == name}
            - {net.get("node_pattern")})
        hosts = sorted(n["hostname"] for n in st.nodes(name).values()
                       if not any(fnmatch.fnmatchcase(n["hostname"].lower(), p.lower())
                                  for p in pats))
        if pats:
            lines_nodes.append(f"{','.join(pats)}  {keys}")
        if hosts:
            lines_nodes.append(f"{','.join(hosts)}  {keys}")
    return "\n".join(out + lines_login + lines_nodes) + "\n"


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def _write_if_changed(path: Path, text: str, mode: int = 0o600) -> bool:
    try:
        if path.read_text() == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def warnings_for(st: State) -> list:
    """Things about the generated files the user should know (not errors)."""
    w = []
    if not st.local.get("store") and _password_hosts(st):
        w.append("no password store chosen on this computer yet, so the wrapper cannot log "
                 "in to " + ", ".join(_password_hosts(st)) + " from here: run python -m "
                 "cubevis.setup network NAME to choose one")
    cm = str(cm_dir())
    if len(cm) + _SOCKET_EXTRA > _SOCKET_LIMIT:
        w.append(f"{cm} is a long path: ssh may fail to share connections (socket path "
                 "too long). A shorter CUBEVIS_RUNTIME_DIR avoids that.")
    return w


def write(st: State) -> dict:
    """(Re)write ``ssh/config`` and ``ssh/rules`` from *st* and make the
    connection directory.  Returns ``{"config": changed, "rules": changed,
    "warnings": [...]}``."""
    ssh_dir().mkdir(parents=True, exist_ok=True)
    runtime_dir()
    cm_dir().mkdir(mode=0o700, exist_ok=True)
    for d in (config_dir(), host_dir(), ssh_dir(), cm_dir()):
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    return {"config": _write_if_changed(config_path(), config_text(st)),
            "rules": _write_if_changed(rules_path(), rules_text(st)),
            "warnings": warnings_for(st)}


def old_layout_files() -> list:
    """Files of the earlier layout (stage 2a, and the bash setup) in the
    configuration directory itself, which this cubevis no longer uses.  An
    older cubevis or the bash setup on another computer sharing this
    directory may still; so setup only mentions them."""
    d = config_dir()
    return [p for p in (d / "ssh" / "config", d / "ssh" / "rules", d / "bin" / "ssh", d / "cm")
            if p.exists() or p.is_symlink()]


def install_wrapper() -> bool:
    """Copy the packaged wrapper to ``bin/ssh`` (mode 755) and point
    ``bin/scp`` at it; True if anything changed.  The copy, not the
    package file, is what kernels run, so upgrading cubevis does not change
    ssh under a running session until setup installs it again."""
    src = data_file("ssh")
    dst = wrapper_path()
    dst.parent.mkdir(parents=True, exist_ok=True)
    changed = False
    if not dst.exists() or dst.read_bytes() != src.read_bytes():
        tmp = dst.with_name(".ssh.tmp")
        shutil.copyfile(src, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dst)
        changed = True
    scp = dst.with_name("scp")
    if not (scp.is_symlink() and os.readlink(scp) == "ssh"):
        if scp.exists() or scp.is_symlink():
            scp.unlink()
        scp.symlink_to("ssh")
        changed = True
    return changed
