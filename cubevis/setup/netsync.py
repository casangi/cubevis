"""The GUI side reading a network's node files (design section 7b).

Each node that ran ``node-here`` has ``hosts/<id>/node/<network>.json`` in
its configuration directory.  On a home shared with the nodes they are
already here; otherwise one ssh command to a node prints every node file
of the network in *its* configuration directory (all of them, when the
nodes share a home) together with that node's host name.

``sync`` then records, in ``state.json``:

* the nodes the files describe (those not known yet are added, under
  their host id and full name; the node ssh'd to is matched by its host
  name);
* their environments, by identity (network, interpreter path): one
  environment seen on several nodes is one record.  ``hosts`` is "all"
  when it is in a shared home (``shared_home`` and the prefix under the
  home), else the list of nodes whose files name it.  ``on`` keeps, per
  node, its launcher and its check (status, error, time);
* a kernel for each new environment (same name), since a kernel is an
  environment plus launch options and most need nothing more;
* the nodes' subnets (``ranges``), offered as inside ranges.

An environment a node's file no longer names is flagged on that node, not
removed (``prune`` removes what is flagged).
"""
from __future__ import annotations

import glob
import json
import os
import shlex
from typing import Optional

from . import config_dir
from . import state as S

BEGIN = "=== CUBEVIS-NODEFILE "
HOST = "=== CUBEVIS-HOST "
END = "=== CUBEVIS-END"
# Copied from a node file's environment to the state's.
ENV_FIELDS = ("kind", "prefix", "tool", "cubevis_via", "versions", "python_version", "visplot",
              "workdir", "env")


def remote_script(network: str) -> str:
    """Prints this node's host name, then every node file of *network* in
    its configuration directory, each after a marker line."""
    net = S.check_name("network", network)
    return ("echo \"" + HOST + "$(hostname)\"; "
            "for f in \"${XDG_CONFIG_HOME:-$HOME/.config}\"/cubevis/hosts/*/node/" + net + ".json; "
            "do [ -f \"$f\" ] && { echo \"" + BEGIN + "$f\"; cat \"$f\"; echo; }; done; "
            "echo \"" + END + "\"")


def remote_command(network: str) -> str:
    return "sh -c " + shlex.quote(remote_script(network))


def parse_dump(text: str) -> tuple:
    """``(hostname, [(path, data)], errors)`` from the remote script's output."""
    host, files, errors = None, [], []
    cur, buf = None, []

    def flush():
        if cur is None:
            return
        try:
            data = json.loads("\n".join(buf))
            if isinstance(data, dict) and isinstance(data.get("node"), dict):
                files.append((cur, data))
            else:
                errors.append(f"{cur}: not a node file")
        except ValueError as exc:
            errors.append(f"{cur}: {exc}")

    for line in (text or "").splitlines():
        if line.startswith(HOST):
            host = line[len(HOST):].strip()
        elif line.startswith(BEGIN):
            flush()
            cur, buf = line[len(BEGIN):].strip(), []
        elif line.startswith(END):
            flush()
            cur = None
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        flush()
    return host, files, errors


def read_local(network: str) -> list:
    """``[(path, data)]``: the network's node files in this configuration
    directory (a home shared with the nodes)."""
    out = []
    for p in sorted(glob.glob(str(config_dir() / "hosts" / "*" / "node" /
                                  f"{S.check_name('network', network)}.json"))):
        try:
            with open(p) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("node"), dict):
            out.append((p, data))
    return out


def _short(name: Optional[str]) -> str:
    return (name or "").split(".")[0].lower()


def _match_node(st: S.State, network: str, node: dict, reached: dict) -> Optional[str]:
    """The state's name for the node a file describes: the node ssh'd to
    when the file is its own (``reached``: {short host name: state name}),
    else one with the same full name or the same name as its host id."""
    for key in (_short(node.get("hostname")), _short(node.get("host"))):
        if key and key in reached:
            return reached[key]
    for name, rec in st.nodes(network).items():
        if rec.get("hostname", "").lower() == (node.get("hostname") or "").lower():
            return name
    host = node.get("host")
    if host in st.nodes(network):
        return host
    return None


def _env_name(st: S.State, network: str, name: str, host: str) -> str:
    taken = st.section("environments")
    if name not in taken:
        return name
    base = f"{name}-{host}"
    n, i = base, 2
    while n in taken:
        n, i = f"{base}-{i}", i + 1
    return n


def _in_shared_home(node: dict, prefix: Optional[str]) -> bool:
    home = (node.get("home") or "").rstrip("/")
    return bool(node.get("shared_home") and home and prefix
                and (prefix == home or prefix.startswith(home + "/")))


def sync(st: S.State, network: str, files: list, reached: Optional[dict] = None) -> list:
    """Record what *files* (``[(path, data)]``) say about *network*.  Returns
    the changes, as ``(what, where)`` for ``ui.record_change``."""
    S.check_name("network", network)
    st.get("networks", network)
    reached = reached or {}
    changes = []
    for _path, data in files:
        node = data.get("node") or {}
        if node.get("network") not in (None, network):
            continue
        nname = _match_node(st, network, node, reached)
        if nname is None:
            host = node.get("host") or _short(node.get("hostname"))
            nname = S.check_name("node", host)
            st.add_node(nname, network=network, hostname=node.get("hostname") or host)
            changes.append(("added node", f"{nname} ({node.get('hostname')}, from its node file)"))
        nrec = st.nodes()[nname]
        nrec["ranges"] = list(node.get("ranges") or [])
        nrec["node_file"] = {"updated": node.get("updated"), "cubevis": node.get("cubevis"),
                             "shared_home": node.get("shared_home"), "home": node.get("home")}
        seen = set()
        for ename, e in sorted((data.get("environments") or {}).items()):
            python = e.get("python")
            if not python or not python.startswith("/"):
                continue
            seen.add(python)
            shared = _in_shared_home(node, e.get("prefix"))
            name = st.find_environment(network, python)
            if name is None:
                name = _env_name(st, network, ename, nname)
                st.add_environment(name, network=network, python=python,
                                   kind=e.get("kind") or "python", prefix=e.get("prefix"),
                                   tool=e.get("tool"), hosts="all" if shared else [nname])
                changes.append(("added environment", f"{name} ({python}, "
                                + ("every node of " + network if shared else "on " + nname) + ")"))
                if not any(k.get("environment") == name for k in st.section("kernels").values()):
                    kname = name if name not in st.section("kernels") else f"{name}-kernel"
                    st.add_kernel(kname, environment=name)
                    changes.append(("added kernel", f"{kname} (environment {name})"))
            rec = st.section("environments")[name]
            if rec.get("hosts") != "all":
                rec["hosts"] = "all" if shared else sorted(set(rec.get("hosts") or []) | {nname})
            for f in ENV_FIELDS:
                if f in e:
                    rec[f] = e[f]
            rec["cubevis"] = (e.get("versions") or {}).get("cubevis")
            on = rec.setdefault("on", {})
            on[nname] = {"launcher": e.get("launcher"), "status": e.get("status"),
                         "last_error": e.get("last_error"), "last_checked": e.get("last_checked"),
                         "name_there": ename}
            oks = [v for v in on.values() if v.get("status") == "ok"]
            rec["status"] = "ok" if oks else (e.get("status") or "new")
            rec["last_error"] = None if oks else e.get("last_error")
            rec["last_checked"] = e.get("last_checked")
            rec["checked_on"] = nname
        # Environments this node used to offer and no longer names: flagged there.
        for name, rec in st.section("environments").items():
            if rec.get("network") != network or rec.get("python") in seen:
                continue
            on = rec.get("on") or {}
            if nname in on and on[nname].get("status") != "gone":
                on[nname].update(status="gone", last_error=f"no longer enabled on {nname}")
                if not any(v.get("status") == "ok" for v in on.values()):
                    rec.update(status="flagged", last_error=f"no longer enabled on {nname}")
                changes.append(("flagged environment", f"{name}: no longer enabled on {nname}"))
    return changes


def suggested_ranges(st: S.State, network: str) -> list:
    """The private IPv4 subnets the network's nodes reported, in order."""
    import ipaddress
    out = []
    for rec in st.nodes(network).values():
        for r in rec.get("ranges") or []:
            try:
                n = ipaddress.ip_network(r, strict=False)
            except ValueError:
                continue
            if n.version == 4 and n.is_private and not n.is_loopback and str(n) not in out:
                out.append(str(n))
    return out
