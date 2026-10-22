"""``python -m cubevis.setup node-here [NETWORK]``: set this computer up as
a node, where kernels run (design section 7b, the node side).

Run it logged in on the node, with a terminal.  It asks only what the node
can answer -- the network's name and domain, whether the home directory is
shared, an optional command listing the nodes you may use (run here, at
once) -- records what it can see for itself (full name, addresses and
their subnets, the home's filesystem), and then the Python environments:
the ones found here are offered, the user chooses, may type others, and
says what kind each is; each is checked by running it (``envs.check``).
Only the environments chosen here are ever used for kernels.

Everything goes to this computer's own directory, ``hosts/<id>/``: the
node file ``node/<network>.json`` (``nodefile``), and the logs.
``state.json`` is not touched.  The network
``local`` is this computer by itself (kernels for plots shown here).
"""
from __future__ import annotations

import getpass
import os
import re
import subprocess
from typing import Optional

from . import envs, hostinfo, host_id, nodefile
from . import state as S
from .ui import tilde

QUERY_TIMEOUT = 60
SLURM_EXAMPLE = "squeue -h --me -o %N | xargs -r scontrol show hostnames"


def _cubevis_version() -> Optional[str]:
    try:
        from importlib.metadata import version
        return version("cubevis")
    except Exception:
        return None


def run_query(command: str) -> tuple:
    """``(names, error)`` from running *command* here with /bin/sh."""
    try:
        r = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True,
                           timeout=QUERY_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return [], f"no answer within {QUERY_TIMEOUT} s"
    except OSError as exc:
        return [], str(exc)
    if r.returncode != 0:
        last = [l for l in r.stderr.strip().splitlines() if l]
        return [], (last[-1] if last else f"exit {r.returncode}")
    names = []
    for n in re.split(r"[\s,]+", r.stdout.strip()):
        if n and n not in names:
            names.append(n)
    return names, None


# --------------------------------------------------------------------------
# The network, and what this computer is in it
# --------------------------------------------------------------------------

def ask_network(c, network: Optional[str]) -> Optional[str]:
    ui = c.ui
    mine = nodefile.mine()
    if network:
        return S.check_name("network", network)
    in_ssh = bool(os.environ.get("SSH_CONNECTION"))
    dom = hostinfo.domain_of(hostinfo.fqdn())
    if mine:
        ui.say(f"This computer is a node of: {', '.join(mine)}.")
        default = mine[0]
    elif not in_ssh:
        default = nodefile.LOCAL
    else:
        # A cluster's name: its domain's first label, or the host name
        # without its number (zuul06 -> zuul).
        default = (dom.split(".")[0] if dom
                   else re.sub(r"[-_.]?\d+$", "", host_id()) or "cluster")
    ui.say("Kernels here are used either from another computer, through ssh -- then this "
           "computer is a node of a network (a cluster, or the machines of a site) -- or from "
           f"this computer itself: the network '{nodefile.LOCAL}'."
           + ("" if in_ssh else " You are not logged in over ssh, so 'local' is suggested."))
    while True:
        name = ui.ask("Network of this computer", default, key="network")
        try:
            return S.check_name("network", name)
        except S.StateError as exc:
            ui.note(str(exc))
            if ui.yes:
                return None


def node_facts(c, nf: nodefile.NodeFile) -> None:
    """Fill in and ask for the node's facts."""
    ui = c.ui
    node = nf.node
    full = hostinfo.fqdn()
    home = os.path.expanduser("~")
    fs = hostinfo.home_fs(home)
    ifaces = hostinfo.interfaces()
    node.update(network=nf.network, host=host_id(), hostname=full, user=getpass.getuser(),
                home=home, home_fs=fs, addresses=ifaces, ranges=hostinfo.ranges_of(ifaces),
                cubevis=_cubevis_version(), updated=S.now())
    if nf.network == nodefile.LOCAL:
        node.update(domain=None, shared_home=None, query=None)
        return
    ui.say(f"{full}: home {home}" + (f" ({fs})" if fs else "")
           + (f", addresses {', '.join(ifaces)}" if ifaces else ""))
    ui.say("The domain completes the nodes' short names (node140 -> node140.<domain>).")
    node["domain"] = ui.ask("Domain of the nodes (empty: none)",
                            node.get("domain") or hostinfo.domain_of(full) or "",
                            key="domain") or None
    guess = hostinfo.shared(fs)
    prev = node.get("shared_home")
    default = prev if prev is not None else (guess if guess is not None else True)
    ui.say(f"When your home directory ({home}) is on a filesystem the other nodes of "
           f"{nf.network} share, an environment in it serves them all.")
    node["shared_home"] = ui.ask_yn("Is your home directory shared by the other nodes?",
                                    default, key="shared_home")
    q = node.get("query") or {}
    ui.say("If a command lists the nodes you may use (a scheduler's reservation, say), give it "
           f"and the computer you plot from can offer them. Slurm, for one: {SLURM_EXAMPLE}")
    cmd = ui.ask("Command printing node names (empty: none)", q.get("command") or "",
                 key="query").strip()
    if not cmd:
        node["query"] = None
        return
    names, err = run_query(cmd)
    if err:
        ui.note(f"it failed here ({err}); kept, to try again later")
    else:
        ui.ok(f"it prints {len(names)} name(s)" + (f": {', '.join(names[:8])}"
                                                    + (" ..." if len(names) > 8 else "")
                                                    if names else ""))
    node["query"] = {"command": cmd, "names": names, "tested": S.now(), "error": err}


# --------------------------------------------------------------------------
# Environments
# --------------------------------------------------------------------------

def _listing(nf: nodefile.NodeFile) -> list:
    """Candidates: the environments enabled already, then the ones found."""
    out, seen = [], set()
    for name, e in sorted(nf.environments.items()):
        out.append({"python": e["python"], "prefix": e.get("prefix") or "", "kind": e["kind"],
                    "tool": e.get("tool"), "cubevis": (e.get("versions") or {}).get("cubevis"),
                    "how": "enabled", "name": name})
        seen.add(e["python"])
    for cand in envs.candidates():
        if cand["python"] not in seen:
            seen.add(cand["python"])
            out.append(cand)
    return out


def _names(cand: dict) -> set:
    """What a candidate can be chosen by, besides its number: the name it
    is enabled under, and its directory's name (``envs/<name>``)."""
    out = {envs.default_name(cand)}
    if cand.get("name"):
        out.add(cand["name"])
    if cand.get("prefix"):
        out.add(cand["prefix"].rstrip("/").rsplit("/", 1)[-1])
    return {n for n in out if n}


def _pick(c, listing: list) -> Optional[list]:
    """The candidates the user chooses: by number, by name (unless two
    share it), or by a path (others added)."""
    ui = c.ui
    ui.say("Python environments for kernels here. Found (or enabled before):")
    for i, cand in enumerate(listing, 1):
        cv = f"cubevis {cand['cubevis']}" if cand.get("cubevis") else "no cubevis"
        label = f"'{cand['name']}', enabled" if cand.get("name") else cand["how"]
        ui._p(f"    {i:>2}. {cand['kind']:<6} {tilde(cand['prefix'] or cand['python'])}"
              f"  ({cv}; {label})")
    if not listing:
        ui._p("    (none found)")
    enabled = [str(i) for i, x in enumerate(listing, 1) if x.get("name")]
    default = " ".join(enabled or [str(i) for i, x in enumerate(listing, 1) if x.get("cubevis")])
    ui.say("Give the ones to use by number or name, and others by path (an environment's "
           "directory or its python), separated by spaces; 'none' for none. Only these are "
           "used.")
    while True:
        text = ui.ask("Use", default or "none", key="environments").strip()
        if text.lower() in ("none", "-", ""):
            return []
        chosen, bad, ambiguous = [], [], []
        for tok in re.split(r"[\s,]+", text):
            by_name = [x for x in listing if tok in _names(x)]
            if tok.isdigit() and 1 <= int(tok) <= len(listing):
                cand = listing[int(tok) - 1]
            elif tok.isdigit() and not by_name:
                bad.append(tok)
                continue
            elif "/" not in tok and not tok.startswith("~") and len(by_name) == 1:
                cand = by_name[0]
            elif "/" not in tok and not tok.startswith("~") and len(by_name) > 1:
                ambiguous.append(f"{tok} ({', '.join(str(listing.index(x) + 1) for x in by_name)})")
                continue
            else:
                cand = envs.describe_path(tok)
                if cand is None:
                    bad.append(tok)
                    continue
            if cand["python"] not in [x["python"] for x in chosen]:
                chosen.append(cand)
        if not bad and not ambiguous:
            return chosen
        if ambiguous:
            ui.note("more than one has that name, give the number instead: " + ", ".join(ambiguous))
        if bad:
            ui.note("not a number or name in the list, nor an environment or a python: "
                    + ", ".join(bad))
        if ui.yes:
            return None


def resolve_tool(answer: str, found: list, near: Optional[str] = None) -> Optional[str]:
    """A tool from what the user typed: ``conda`` / ``mamba`` /
    ``micromamba`` (the one found here, preferring the directory of *near*),
    another name on PATH, or a path.  None if it is not an executable."""
    import shutil
    a = answer.strip()
    if not a:
        return None
    if "/" not in a and not a.startswith("~"):
        same = [t for t in found if os.path.basename(t) == a]
        if near:
            same.sort(key=lambda t: os.path.dirname(t) != os.path.dirname(near))
        if same:
            return same[0]
        w = shutil.which(a)
        return envs.norm(w) if w else None
    p = envs.norm(a)
    return p if os.path.isfile(p) and os.access(p, os.X_OK) else None


def conda_tool_for(e: dict) -> Optional[str]:
    """For cubevis installed as a conda package: the tool to upgrade it
    with -- the one that listed the environment, else the first conda,
    mamba or micromamba found here (preferring conda).  None if none is."""
    lister = e.get("listed_by")
    if lister and os.path.basename(lister) in envs.TOOLS and os.access(lister, os.X_OK):
        return lister
    found = envs.conda_tools()
    return next((t for t in found if os.path.basename(t) == "conda"), found[0] if found else None)


def ask_tool(c, name: str) -> Optional[str]:
    """Only when cubevis was installed in environment *name* as a conda
    package and no conda tool is found here: the command that upgrades it.
    Setup only ever writes commands for the user to run (design section 8)."""
    ui = c.ui
    found = envs.conda_tools()
    default = ""
    ui.say(f"cubevis is installed in {name} as a conda package, and no conda, mamba or "
           "micromamba was found here. Which command upgrades packages in it? When cubevis "
           "there needs upgrading, setup writes the commands for you to run with it (setup "
           "never runs them).")
    while True:
        ans = ui.ask("  Command (conda, mamba, micromamba, or its path; empty: none)",
                     tilde(default) if default else "", key="env_tool")
        if not ans.strip():
            ui.note("no command recorded: upgrade instructions will name none")
            return None
        tool = resolve_tool(ans, found)
        if tool:
            return tool
        ui.note(f"{ans} is not a command here")
        if ui.yes:
            return None


def configure_environments(c, nf: nodefile.NodeFile) -> int:
    """Choose, name and check the environments; returns how many work."""
    ui = c.ui
    for e in nf.environments.values():
        if e.get("kind") == "mamba":         # a kind until 2026-10-21 (envs docstring)
            e["kind"] = "conda"
    listing = _listing(nf)
    chosen = _pick(c, listing)
    if chosen is None:
        return 0
    keep = {cand["python"] for cand in chosen}
    for name, e in list(nf.environments.items()):
        if e["python"] not in keep:
            del nf.environments[name]
            ui.record_change("stopped using environment", f"{name} ({tilde(e['python'])})")
    for cand in chosen:
        name = nf.environment_by_python(cand["python"])
        if name is None:
            ui.say(f"{tilde(cand['python'])}:")
            kind = ui.ask_choice("  What kind of environment is it (conda, venv, python)?",
                                 cand["kind"], envs.KINDS, key="env_kind")
            taken = set(nf.environments)
            base = envs.default_name(cand)
            dflt, i = base, 2
            while dflt in taken:
                dflt, i = f"{base}-{i}", i + 1
            while True:
                name = ui.ask("  A name for it", dflt, key="env_name")
                try:
                    S.check_name("environment", name)
                except S.StateError as exc:
                    ui.note(str(exc))
                    name = None
                if name and name in taken:
                    ui.note(f"'{name}' is used already")
                    name = None
                if name or ui.yes:
                    break
            if not name:
                continue
            nf.environments[name] = {
                "kind": kind, "python": cand["python"], "prefix": cand.get("prefix"),
                "tool": None, "listed_by": cand.get("tool"), "cubevis_via": None,
                "versions": {}, "python_version": None, "visplot": None,
                "status": "new", "last_checked": None, "last_error": None}
            ui.record_change("enabled environment", f"{name} ({kind}, {tilde(cand['python'])})")
    good = 0
    for name in sorted(nf.environments):
        e = nf.environments[name]
        rep = envs.check(e["python"])
        e.update(last_checked=S.now(), versions=rep.get("versions") or {},
                 python_version=rep.get("python"), visplot=rep.get("visplot"))
        if rep.get("prefix"):
            e["prefix"] = rep["prefix"]
        # How cubevis got there decides how it is upgraded (design section 8):
        # from the packaging metadata, not asked.
        via = envs.install_method(rep) if (rep.get("versions") or {}).get("cubevis") else None
        e["cubevis_via"] = via
        if via and via["method"] == "conda":
            e["tool"] = e.get("tool") or conda_tool_for(e) or ask_tool(c, name)
        else:
            e["tool"] = None
        line = f"Environment {name} ({e['kind']}, {tilde(e['python'])}) -- {envs.summary(rep)}"
        if rep["ok"]:
            e.update(status="ok", last_error=None)
            ui.ok(line)
            good += 1
        else:
            e.update(status="flagged", last_error=rep.get("error"))
            ui.fail(line)
    return good


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------

def cmd_node_here(c, network: Optional[str] = None) -> int:
    ui = c.ui
    ui.heading("This computer as a node: where kernels run")
    ui.say("This records what this computer offers to kernels: which network it is in and "
           "the Python environments to run them in. Only the environments you choose here "
           f"are used. It writes only to this computer's directory, "
           f"{tilde(nodefile.node_dir().parent)}.")
    name = ask_network(c, network)
    if not name:
        return 1
    nf = nodefile.load(name)
    new = not nf.path.exists()
    node_facts(c, nf)
    nodefile.save(nf)
    ui.record_change("added this computer to" if new else "updated this computer in",
                     f"network {name} ({tilde(nf.path)})")
    ui.heading(f"Environments for kernels on {host_id()}")
    good = configure_environments(c, nf)
    nodefile.save(nf)
    ui._p()
    if not nf.environments:
        ui.note("no environments enabled here: kernels cannot run on this computer")
        return 1
    if name == nodefile.LOCAL:
        ui.say("Kernels started on this computer can use these environments.")
    else:
        ui.say(f"On the computer where your plots appear, python -m cubevis.setup network "
               f"{name} reads this (stage 2b part iii).")
    return 0 if good else 1
