"""The ssh files generated from the state: ``ssh/config`` and ``ssh/rules``.

``ssh/config`` has one ``Host`` per login host and per node, under the
aliases the state gives them, so the kernels (``sshpyk --ssh-config``) and
setup's checks reach every node through its network's login host with
shared connections (ControlMaster) -- the user's own ``~/.ssh/config`` is
neither needed nor changed.

``ssh/rules`` tells the wrapper (``data/ssh``) which real host names ask for
a password and where it is stored: each network's login host, and the
network's ``node_pattern`` (a glob, so a node added later needs no new
rule).  Login hosts come first: the wrapper takes the first rule that
matches, and a login host can match its own network's node pattern.

Both are regenerated from the state whenever it changes and rewritten only
when their text changes (``write`` says which did).  The format of the
rules is the wrapper's (its header, "Rules"); the keys per password store
are those of the bash setup's ``store_rule_keys``.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from . import config_dir, data_file
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
    return config_dir() / "ssh"


def config_path() -> Path:
    return ssh_dir() / "config"


def rules_path() -> Path:
    return ssh_dir() / "rules"


def cm_dir() -> Path:
    return config_dir() / "cm"


def bin_dir() -> Path:
    return config_dir() / "bin"


def wrapper_path() -> Path:
    return bin_dir() / "ssh"


def secrets_dir() -> Path:
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

def _host_block(alias: str, hostname: str, user: str, port: Optional[int],
                proxy_jump: Optional[str]) -> list:
    lines = [f"Host {alias}", f"    HostName {hostname}", f"    User {user}"]
    if port and int(port) != 22:
        lines.append(f"    Port {int(port)}")
    if proxy_jump:
        lines.append(f"    ProxyJump {proxy_jump}")
    lines += ["    ControlMaster auto",
              f"    ControlPath {cm_dir()}/%C",
              f"    ControlPersist {CONTROL_PERSIST}",
              f"    ServerAliveInterval {SERVER_ALIVE_INTERVAL}",
              f"    ServerAliveCountMax {SERVER_ALIVE_COUNT_MAX}"]
    return lines


def config_text(st: State) -> str:
    """``ssh/config`` for *st*: networks in name order, each login host
    then its nodes in name order."""
    out = ["# Written by cubevis setup from state.json; changes made here are",
           "# lost when it is written again (use python -m cubevis.setup).",
           "# The kernels use this file directly; your ~/.ssh/config includes it",
           "# only if you chose that."]
    for name in sorted(st.networks()):
        net = st.networks()[name]
        login = net.get("login")
        ranges = net.get("inside") or []
        out.append("")
        out.append(f"# network {name}" + ("" if login else " (nodes reached directly)"))
        if login:
            out += _host_block(login["alias"], login["host"], login["user"],
                               login.get("port"), None)
        # Through the login host always, never (no login host), or only
        # when this computer is outside the network (inside ranges): then
        # the ProxyJump goes in a Match block, decided at each connection.
        always = login["alias"] if (login and not ranges) else None
        aliases = []
        for nname in sorted(st.nodes(name)):
            node = st.nodes()[nname]
            aliases.append(node["alias"])
            out.append("")
            out += _host_block(node["alias"], node["hostname"], net["user"],
                               node.get("port"), always)
        if login and ranges and aliases:
            out.append("")
            out.append(f"# Outside {', '.join(ranges)}: through {login['alias']}"
                       " (python -m cubevis.setup.inside -v " + name + " says which)")
            out.append(f"Match originalhost {','.join(aliases)} !exec \"{inside_command(st, name)}\"")
            out.append(f"    ProxyJump {login['alias']}")
    # For the test suite: host keys go to a file of its own (a new one,
    # so new keys are accepted), not to the account's ~/.ssh/known_hosts.
    # Unset in normal use.
    kh = os.environ.get("CUBEVIS_SETUP_KNOWN_HOSTS")
    if kh:
        out += ["", "# CUBEVIS_SETUP_KNOWN_HOSTS (tests)", "Host *",
                f"    UserKnownHostsFile {kh}", "    StrictHostKeyChecking accept-new"]
    return "\n".join(out) + "\n"


def inside_command(st: State, network: str) -> str:
    """The command ssh runs (``Match exec``) to ask whether this computer
    is inside *network*: the local Python setup recorded, else this one."""
    import shlex
    import sys
    py = st.local.get("python") or sys.executable
    return f"{shlex.quote(py)} -m cubevis.setup.inside {shlex.quote(network)}"


def rules_text(st: State) -> str:
    """``ssh/rules`` for *st*: login hosts first (see module docstring),
    then each network's nodes -- by ``node_pattern``, or by name when the
    network has none."""
    out = ["# Written by cubevis setup from state.json: the hosts that ask for a",
           "# password, and where the wrapper finds it. Real host names, not aliases."]
    lines_login, lines_nodes = [], []
    for name in sorted(st.networks()):
        net = st.networks()[name]
        login = net.get("login") or {}
        if login.get("password"):
            svc = login.get("service") or net.get("service") or "cubevis-ssh"
            lines_login.append(f"{login['host']}  {rule_keys(st.local, svc, login['user'])}")
        if net.get("password"):
            svc = net.get("service") or login.get("service") or "cubevis-ssh"
            keys = rule_keys(st.local, svc, net["user"])
            if net.get("node_pattern"):
                lines_nodes.append(f"{net['node_pattern']}  {keys}")
            else:
                hosts = sorted(n["hostname"] for n in st.nodes(name).values())
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
    cm = str(cm_dir())
    if len(cm) + _SOCKET_EXTRA > _SOCKET_LIMIT:
        w.append(f"{cm} is a long path: ssh may fail to share connections (socket path "
                 "too long). A shorter XDG_CONFIG_HOME, or home directory, avoids that.")
    return w


def write(st: State) -> dict:
    """(Re)write ``ssh/config`` and ``ssh/rules`` from *st* and make the
    connection directory.  Returns ``{"config": changed, "rules": changed,
    "warnings": [...]}``."""
    ssh_dir().mkdir(parents=True, exist_ok=True)
    cm_dir().mkdir(parents=True, exist_ok=True)
    for d in (config_dir(), ssh_dir(), cm_dir()):
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    return {"config": _write_if_changed(config_path(), config_text(st)),
            "rules": _write_if_changed(rules_path(), rules_text(st)),
            "warnings": warnings_for(st)}


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
