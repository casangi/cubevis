"""``python -m cubevis.setup HOST``: make cubevis kernels work on HOST, the
computer the data is on (remote session design 7c).

The user's task is "my data is on HOST, which I can ssh to"; networks,
routes and launchers are setup's own bookkeeping.  In order:

1. **Where HOST is.**  A node setup knows (by name, alias or full name); a
   node of a known cluster (its name matches the cluster's pattern: added
   without a question); a host in the domain of a known network (it joins
   it: same route); else a new network, for which setup asks the route --
   directly, or through a login host -- and names it from the domain.
   (Stage 2c step 4 takes the route from the user's own ssh configuration
   instead of asking.)
2. **Logging in**, each hop: the login host, then HOST, with a stored
   password where one is asked for (``steps.hop_steps``).
3. **Looking around** (``discover``, over ssh, read-only): HOST's full
   name, addresses and home; its conda tools; its environments, each with
   cubevis checked by running it.  The node's domain fills in the
   network's, its subnets are offered as inside ranges.
4. **A cluster?**  Asked once per node: name and pattern suggested from the
   name (``cvpost140`` -> ``cvpost``, ``cvpost*``).
5. **The environment**: one whose kernel-side code is the same as here
   (``cubevis._fingerprint``), setup's own first; the user's are listed
   with whether they can be used and why not.  A mismatch is usable only
   with ``CUBEVIS_ALLOW_MISMATCH=1``.  None usable: setup says how to make
   one (creating it is stage 2c step 3).
6. **The kernel**: a name (``Session("tw-hya", node=...)`` chooses it), its
   working directory on HOST and its environment variables; setup writes
   its launcher on HOST and runs it once (directory, activation, imports,
   fingerprint) before recording it.

``--kernel NAME`` adds (or changes) one kernel for a host already set up,
without the environment questions.  Run again without it, setup checks the
host's kernels (and rewrites their launchers when they changed); ``--redo``
asks their settings again.
"""
from __future__ import annotations

import ipaddress
import os
import posixpath
import re
from typing import Optional

from . import host_dir, host_id, launcher, envs
from . import remote as R
from . import state as S
from .steps import (Ctx, hop_steps, inside_step, local_steps, parent_domain,
                    split_port)
from .ui import tilde


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def rtilde(path: Optional[str], home: Optional[str]) -> str:
    """*path* on the node, with its home as ``~``."""
    p = str(path or "")
    h = (home or "").rstrip("/")
    if h and (p.rstrip("/") == h or p.startswith(h + "/")):
        return "~" + p.rstrip("/")[len(h):]
    return p


def expand_remote(text: str, home: str) -> str:
    """A directory on the node as typed (``~``, ``~/x``, absolute, or
    relative to the home) -> absolute and normalised."""
    t = text.strip()
    if t == "~" or t == "":
        return home
    if t.startswith("~/"):
        return posixpath.normpath(home.rstrip("/") + t[1:])
    if t.startswith("/"):
        return posixpath.normpath(t)
    return posixpath.normpath(posixpath.join(home, t))


def is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def short_name(host: str) -> str:
    return host if is_address(host) else host.split(".")[0]


def local_fingerprint(c: Ctx) -> str:
    """This computer's kernel-side fingerprint (once per run: ~1 s)."""
    fp = getattr(c, "_fingerprint", None)
    if fp is None:
        from .. import _fingerprint
        fp = c._fingerprint = _fingerprint.fingerprint()
    return fp


def local_version() -> str:
    try:
        from importlib.metadata import version
        return version("cubevis")
    except Exception:
        return "unknown"


def local_is_development() -> tuple:
    """``(dev, why)``: whether cubevis here cannot be installed elsewhere by
    its version from PyPI -- an editable or local-directory install, or a
    version that is not a release (``1.0.97.dev22+g0a5b519``)."""
    v = local_version()
    try:
        import json
        from importlib.metadata import distribution
        du = distribution("cubevis").read_text("direct_url.json")
        d = json.loads(du) if du else {}
    except Exception:
        d = {}
    if (d.get("url") or "").startswith("file://") and "archive_info" not in d:
        return True, f"installed from the directory {d['url'][len('file://'):]}"
    if not re.match(r"^\d+(\.\d+)*$", v):
        return True, f"version {v} is not a release"
    return False, ""


def _unique(base: str, taken) -> str:
    name, i = base, 2
    while name in taken:
        name, i = f"{base}-{i}", i + 1
    return name


# --------------------------------------------------------------------------
# 1. Where HOST is
# --------------------------------------------------------------------------

def new_network(c: Ctx, host: str) -> Optional[str]:
    """Questions for a host setup knows nothing about: the route.  Returns
    the new network's name (made from the domain; the user sees clusters
    and kernels, not networks)."""
    ui, st = c.ui, c.st
    ui.heading(f"How this computer reaches {host}")
    ui.say(f"Kernels on {host} are started from here over ssh. Some sites put their "
           "computers behind a login host (a bastion), reached first, as in "
           f"'ssh -J login.example.org {host}'.")
    via = ui.ask_yn(f"Do you reach {host} through a login host?", True, key="via_login")
    login, lport, luser = None, 22, None
    me = os.environ.get("USER") or ""
    if via:
        while True:
            text = ui.ask("Login host, its real name (host[:port])", "", key="login_host")
            if not text:
                return None
            login, lport = split_port(text)
            try:
                S.check_host(login)
                break
            except S.StateError as exc:
                ui.note(str(exc))
                if ui.yes or "login_host" in ui.answers:
                    return None
        luser = ui.ask(f"Your user name on {login}", st.local.get("last_user") or me,
                       key="login_user")
    user = ui.ask(f"Your user name on {host}", luser or st.local.get("last_user") or me,
                  key="user")
    st.local["last_user"] = user
    domain = None if is_address(host) else parent_domain(host)
    base = (domain or (parent_domain(login) if login else None)
            or re.sub(r"[-_.]?\d+$", "", short_name(host)) or "net").split(".")[0]
    base = re.sub(r"[^A-Za-z0-9_.-]", "-", base).strip("-._") or "net"
    name = _unique(base, st.networks())
    try:
        st.add_network(name, login_host=login, login_port=lport, login_user=luser, user=user,
                       domain=domain)
    except S.StateError as exc:
        ui.fail(str(exc))
        return None
    ui.record_change("added network", f"{name} ({'through ' + login if login else 'direct'}"
                     + (f", domain {domain}" if domain else "") + ")")
    return name


def locate(c: Ctx, host: str, port: int) -> Optional[str]:
    """The node for *host*, added when setup does not know it yet."""
    ui, st = c.ui, c.st
    n = st.find_node(host)
    if n:
        node = st.nodes()[n]
        if port != 22 and int(node.get("port") or 22) != port:
            node["port"] = port
            ui.record_change("changed port", f"node {n}: {port}")
        return n
    cl = st.cluster_for(host)
    if cl:
        n = st.node_for(host)
        if port != 22:
            st.nodes()[n]["port"] = port
        ui.record_change("added node", f"{n} to cluster {cl} (it matches "
                         f"{st.section('clusters')[cl]['pattern']})")
        return n
    dom = None if is_address(host) else parent_domain(host)
    same = [k for k, v in sorted(st.networks().items())
            if dom and (v.get("domain") or "").lower() == dom.lower()]
    if same:
        net = same[0]
        lg = st.networks()[net].get("login")
        ui.say(f"{host} is in {dom}, as the nodes setup knows in network {net} are: it is "
               "reached the same way" + (f" (through {lg['host']})." if lg else " (directly)."))
    else:
        net = new_network(c, host)
        if not net:
            return None
    n = short_name(host)
    if n in st.nodes() or not re.match(r"^[A-Za-z0-9]", n):
        n = _unique(f"{n}-{net}", st.nodes())
    try:
        st.add_node(n, network=net, hostname=st.node_hostname(net, host), port=port)
    except S.StateError as exc:
        ui.fail(str(exc))
        return None
    node = st.nodes()[n]
    ui.record_change("added node", f"{n} ({node['hostname']}, ssh name {node['alias']})")
    return n


# --------------------------------------------------------------------------
# 3. Looking around
# --------------------------------------------------------------------------

def discover_hint(c: Ctx, node: str) -> None:
    """After a failure on a node: how to see what setup sees, there."""
    p = host_dir() / "discover.py"
    try:
        from . import discover as D
        p.write_text(D.payload())
        os.chmod(p, 0o644)
        where = f"or copy {tilde(p)} there and run python3 discover.py"
    except OSError:
        where = "or python3 discover.py with the file python -m cubevis.setup.discover --bundle writes"
    c.ui.say(f"To see what setup sees, on {node}: python3 -m cubevis.setup.discover (where "
             f"cubevis is installed), {where}. Read-only.")


def discover_step(c: Ctx, node: str, paths=(), only=False, python3=None) -> Optional[dict]:
    ui, st = c.ui, c.st
    alias = st.nodes()[node]["alias"]
    if not only:
        ui.say(f"Looking at {node}: its Python environments, and what each holds "
               "(read-only; environments with cubevis are started once to check them) ...")
    try:
        rep = R.discover(c, alias, paths, only=only, python3=python3)
    except R.RemoteError as exc:
        ui.fail(f"Looking at {node} failed: {exc}")
        discover_hint(c, node)
        return None
    if not only:
        h = rep.get("host") or {}
        tools = ", ".join(t["name"] for t in rep.get("tools") or []) or "no conda tools"
        n_cv = len([e for e in rep.get("environments") or [] if e.get("cubevis")])
        ui.ok(f"{node} is {h.get('fqdn') or h.get('hostname')}: python3 {rep.get('python')}, "
              f"{tools}, {len(rep.get('environments') or [])} environment(s), "
              f"{n_cv} with cubevis")
    for e in rep.get("errors") or []:
        ui.note(f"{node}: {e}")
    return rep


def record_facts(c: Ctx, node: str, rep: dict) -> None:
    """What the node said about itself, into its record and its network."""
    ui, st = c.ui, c.st
    h = rep.get("host") or {}
    n = st.nodes()[node]
    n.update(fqdn=h.get("fqdn"), ranges=h.get("ranges") or [], home=h.get("home"))
    net = st.networks()[n["network"]]
    dom = h.get("domain")
    if dom and not net.get("domain"):
        try:
            S.check_host(dom)
        except S.StateError:
            return
        net["domain"] = dom
        if not net.get("node_pattern"):
            net["node_pattern"] = f"*.{dom}"
        ui.record_change("set domain", f"network {n['network']}: {dom} (from {node})")


# --------------------------------------------------------------------------
# 4. A cluster?
# --------------------------------------------------------------------------

def cluster_step(c: Ctx, node: str) -> None:
    ui, st = c.ui, c.st
    n = st.nodes()[node]
    if n.get("cluster") or n.get("standalone"):
        return
    # The name the user gave (a node may call itself something else).
    short = short_name(n["hostname"])
    m = re.match(r"^(.*?[A-Za-z])[-_]?\d+$", short)
    guess = m.group(1) if m else None
    ui.heading(f"Is {short} one of a cluster's nodes?")
    ui.say("A cluster is a group of computers named alike (cvpost001, cvpost002, ...) that "
           "share a filesystem and are reached the same way. Set up one of them, and kernels "
           "can run on any of them -- the node is chosen when a session starts -- without "
           "running setup for each.")
    if not ui.ask_yn(f"Is {short} one of a cluster's nodes?", bool(guess), key="cluster"):
        n["standalone"] = True
        return
    while True:
        name = ui.ask("The cluster's name", guess or short, key="cluster_name")
        pattern = ui.ask("Its nodes' names, with * for what varies", f"{guess or short}*",
                         key="cluster_pattern")
        other, old_pattern = None, None
        try:
            S.check_name("cluster", name)
            other = st.section("clusters").get(name)
            if other and other.get("network") != n["network"]:
                raise S.StateError(f"there is a cluster {name!r} elsewhere already")
            if other:
                old_pattern = other["pattern"]
                other["pattern"] = pattern
            else:
                st.add_cluster(name, network=n["network"], pattern=pattern)
            if st.cluster_for(short) != name:
                raise S.StateError(f"{pattern} does not match {short}")
            break
        except S.StateError as exc:
            if old_pattern is not None:
                other["pattern"] = old_pattern          # put back what it was
            elif other is None and name in st.section("clusters"):
                del st.section("clusters")[name]        # the one just added
            ui.note(str(exc))
            if ui.yes or "cluster_name" in ui.answers or "cluster_pattern" in ui.answers:
                return
    n["cluster"] = name
    ui.record_change("joined cluster" if other else "added cluster",
                     f"{name} (nodes {pattern}, network {n['network']})")
    for k, v in st.nodes(n["network"]).items():          # nodes known before
        if k != node and not v.get("cluster") and st.cluster_for(v["hostname"]) == name:
            v["cluster"] = name
            v.pop("standalone", None)
            ui.record_change("added node to cluster", f"{k} to {name}")


# --------------------------------------------------------------------------
# 5. The environment
# --------------------------------------------------------------------------

def _rows(c: Ctx, rep: dict) -> list:
    """The environments with cubevis (or setup's marker, or given), with
    what decides whether they can be used: ``(cand, report, usable, why)``."""
    from .. import _fingerprint
    here = local_fingerprint(c)
    out = []
    for cand in rep.get("environments") or []:
        if cand.get("check") is None:
            continue
        r = envs.interpret(cand["check"])
        if not r["ok"]:
            out.append((cand, r, False, r.get("error") or "it cannot run kernels"))
            continue
        usable, why = _fingerprint.compatible(here, r.get("fingerprint"))
        exact = here == r.get("fingerprint")
        out.append((cand, r, usable, "same kernel-side code as here" if exact else why))
    # Usable first; of those, setup's own, then exact matches (not overridden).
    out.sort(key=lambda x: (not x[2], not x[0].get("marker"), here != x[1].get("fingerprint")))
    return out


def no_environment_advice(c: Ctx, node: str, rep: dict) -> None:
    ui = c.ui
    here = local_fingerprint(c)
    ui.say(f"No environment on {node} has the same kernel-side code as this computer "
           f"({here}): the code that runs in a kernel must be identical on both sides.")
    dev, why = local_is_development()
    if dev:
        ui.say(f"cubevis here is a development install ({why}), which no package "
               f"reproduces. Install the same checkout in an environment on {node} (pip "
               "install -e on a checkout both computers see), then run this again; or, to "
               "use a different one anyway, set CUBEVIS_ALLOW_MISMATCH=1 (kernels may fail at "
               "any message).")
        return
    v = local_version()
    tools = [t for t in rep.get("tools") or [] if t["name"] in ("conda", "mamba")]
    import sys
    py = f"{sys.version_info[0]}.{sys.version_info[1]}"      # the same Python as here
    if not tools:
        ui.say(f"There is no conda on {node}. Install Miniforge (conda-forge's installer, "
               "https://conda-forge.org/download) on a fast filesystem every node sees -- at "
               "NRAO a project or Lustre area rather than your home, which is slow and has a "
               f"quota -- then run this again.")
        return
    t = rtilde(tools[0]["path"], (rep.get("host") or {}).get("home"))
    ui.say("Setup will make its own environment for this (a later step of setup); until "
           f"then, on {node}:")
    ui._p(f"    {t} create -y -n cubevis-{v} python={py} pip")
    ui._p(f"    {t} run -n cubevis-{v} python -m pip install \"cubevis[visplot]=={v}\"")
    ui.say("and run this again.")


def environment_step(c: Ctx, node: str, rep: dict, reuse: bool) -> Optional[str]:
    """Choose the environment for the node's kernel; its record's name."""
    ui, st = c.ui, c.st
    n = st.nodes()[node]
    net = n["network"]
    home = (rep.get("host") or {}).get("home")
    used = [e for e in sorted(st.section("environments"))
            if st.environment_on(e, node)
            and any(k.get("environment") == e for k in st.section("kernels").values())]
    if reuse and len(used) == 1:
        return used[0]
    ui.heading(f"The Python environment for kernels on {node}")
    rows = _rows(c, rep)
    if not rows:
        ui.note(f"no environment on {node} has cubevis")
    for i, (cand, r, usable, why) in enumerate(rows, 1):
        v = (r.get("versions") or {}).get("cubevis")
        mark = "made by setup, " if cand.get("marker") else ""
        line = (f"{i:>2}. {rtilde(cand['prefix'], home)}  ({mark}cubevis {v or '-'}"
                f"{', Python ' + r['python'] if r.get('python') else ''}): {why}")
        (ui.ok if usable else ui.fail)(line)
    usable = [i for i, x in enumerate(rows, 1) if x[2]]
    if not usable:
        no_environment_advice(c, node, rep)
        ans = ui.ask(f"Or give the path of an environment on {node} to check (empty: stop)",
                     "", key="environment")
        if not ans:
            return None
    else:
        prev = [i for i, x in enumerate(rows, 1)
                if x[2] and st.find_environment(net, x[0]["python"]) in used]
        default = str((prev or usable)[0])
        ui.say("Choose one by its number, or give the path of another environment there "
               "(its directory or its python); 'none' to stop here.")
        ans = ui.ask("Environment", default, key="environment")
    while True:
        ans = ans.strip()
        if ans.lower() in ("none", "-"):
            return None
        pick = None
        if ans.isdigit() and 1 <= int(ans) <= len(rows):
            pick = rows[int(ans) - 1]
        elif ans and not ans.isdigit():
            path = ans if not ans.startswith("~") else (home or "~") + ans[1:]
            more = discover_step(c, node, [path], only=True, python3=rep.get("python_exe"))
            got = _rows(c, more) if more else []
            if got:
                pick = got[0]
                rows.append(pick)
            elif more is not None:
                ui.note(f"{ans} is not an environment on {node}")
        if pick and pick[2]:
            break
        if pick:
            ui.note(f"{rtilde(pick[0]['prefix'], home)} cannot be used: {pick[3]}")
        elif ans.isdigit():
            ui.note(f"no environment {ans} in the list")
        if ui.yes or "environment" in ui.answers:
            return None
        ans = ui.ask("Environment (empty: stop)", "")
        if not ans:
            return None
    cand, r, _ok, why = pick
    if "CUBEVIS_ALLOW_MISMATCH" in why:
        ui.note(f"using {rtilde(cand['prefix'], home)} although {why}")
    return record_environment(c, node, cand, r)


def record_environment(c: Ctx, node: str, cand: dict, r: dict) -> str:
    ui, st = c.ui, c.st
    n = st.nodes()[node]
    net = n["network"]
    name = st.find_environment(net, cand["python"])
    if name is None:
        base = envs.default_name({"kind": cand["kind"], "python": cand["python"],
                                  "prefix": cand["prefix"]})
        name = _unique(f"{base}-{n.get('cluster') or node}", st.section("environments"))
        st.add_environment(name, network=net, python=cand["python"], kind=cand["kind"],
                           prefix=cand["prefix"], hosts=[] if n.get("cluster") else [node],
                           cluster=n.get("cluster"))
        ui.record_change("added environment", f"{name} ({cand['python']} on "
                         f"{'cluster ' + n['cluster'] if n.get('cluster') else node})")
    e = st.section("environments")[name]
    if n.get("cluster") and not e.get("cluster"):
        # Recorded for this node before it joined its cluster: the cluster's
        # nodes share its filesystem, so the environment is theirs too.
        e["cluster"] = n["cluster"]
        ui.record_change("environment on cluster", f"{name}: every node of {n['cluster']}")
    elif not e.get("cluster") and e.get("hosts") != "all" and node not in (e.get("hosts") or []):
        e["hosts"] = sorted(set(e.get("hosts") or []) | {node})
    via = envs.install_method(r) if (r.get("versions") or {}).get("cubevis") else None
    tool = cand.get("tool") if via and via["method"] == "conda" else None
    e.update(kind=envs.kind_from_report(r) or cand["kind"],
             prefix=r.get("prefix") or cand["prefix"], versions=r.get("versions") or {},
             cubevis=(r.get("versions") or {}).get("cubevis"), python_version=r.get("python"),
             visplot=r.get("visplot"), cubevis_via=via, tool=tool,
             fingerprint=r.get("fingerprint"), marker=cand.get("marker"), checked_on=node)
    st.mark_ok("environments", name)
    return name


# --------------------------------------------------------------------------
# 6. The kernel
# --------------------------------------------------------------------------

def _ask_settings(c: Ctx, node: str, k: dict, home: str) -> None:
    ui = c.ui
    ui.say(f"Its kernels start in a working directory on {node}: relative paths "
           "(ms='x.ms') are found from there. ~ is your home directory there.")
    while True:
        ans = ui.ask("Working directory", rtilde(k.get("workdir") or home, home), key="workdir")
        p = expand_remote(ans, home)
        k["workdir"] = None if p == home else p
        break
    ui.say("Environment variables to set before Python starts, as NAME=value separated by "
           "spaces (quote values with spaces), e.g. HDF5_USE_FILE_LOCKING=FALSE "
           "OMP_NUM_THREADS=8. They are set on the node; nothing else is taken from here.")
    while True:
        ans = ui.ask("Variables ('none' for none)", launcher.format_vars(k.get("env")) or "none",
                     key="env_vars")
        try:
            k["env"] = {} if ans.strip().lower() in ("none", "-") else launcher.parse_vars(ans)
            break
        except ValueError as exc:
            ui.note(str(exc))
            if ui.yes or "env_vars" in ui.answers:
                break


def write_and_test(c: Ctx, node: str, name: str, rep: dict, host_text: str) -> tuple:
    """Write kernel *name*'s launcher on *node* and run it once.
    ``(ok, message)``; the kernel's record is updated either way."""
    ui, st = c.ui, c.st
    from .. import _fingerprint
    k = st.section("kernels")[name]
    e = st.section("environments")[k["environment"]]
    h = rep.get("host") or {}
    home = h.get("home") or "/"
    alias = st.nodes()[node]["alias"]
    path = posixpath.join(h.get("config_dir") or posixpath.join(home, ".config", "cubevis"),
                          launcher.relpath(host_id(), name))
    text = launcher.text(name, k, e, home, host_id(), host=host_text)
    try:
        did = R.install_launcher(c, alias, rep.get("python_exe") or "python3", path, text)
    except R.RemoteError as exc:
        st.flag("kernels", name, f"cannot write its launcher on {node}: {exc}")
        return False, st.section("kernels")[name]["last_error"]
    if did == "written":
        ui.record_change("wrote launcher", f"{node}:{rtilde(path, home)}")
    k["launcher"] = path
    try:
        fp, cwd = R.test_launcher(c, alias, path)
    except R.RemoteError as exc:
        st.flag("kernels", name, f"on {node}: {exc}")
        return False, st.section("kernels")[name]["last_error"]
    usable, why = _fingerprint.compatible(local_fingerprint(c), fp)
    if not usable:
        st.flag("kernels", name, f"on {node}: {why}")
        return False, st.section("kernels")[name]["last_error"]
    st.mark_ok("kernels", name, fingerprint=fp, checked_on=node)
    return True, f"starts in {rtilde(cwd, home)}" + (f"; {why}" if fp != local_fingerprint(c) else "")


def kernel_step(c: Ctx, node: str, env: str, rep: dict, name: Optional[str],
                host_text: str) -> bool:
    ui, st = c.ui, c.st
    n = st.nodes()[node]
    home = (rep.get("host") or {}).get("home") or "/"
    kernels = st.section("kernels")
    ui.heading(f"A kernel on {node}")
    ui.say("A kernel is what you choose when you plot: its name, the environment, and the "
           "directory and variables it starts with. Several kernels can share one "
           "environment (say tw-hya and hrs-imaging, each in its own directory).")
    while True:
        default = name or _unique(n.get("cluster") or node, kernels)
        name = ui.ask("A name for the kernel", default, key="kernel")
        try:
            S.check_name("kernel", name)
            old = kernels.get(name)
            if old and old.get("environment") in st.section("environments") \
                    and st.section("environments")[old["environment"]]["network"] != n["network"]:
                raise S.StateError(f"kernel {name!r} is on another network already")
            break
        except S.StateError as exc:
            ui.note(str(exc))
            name = None
            if ui.yes or "kernel" in ui.answers:
                return False
    k = dict(kernels.get(name) or {})
    if k.get("environment") != env and k:
        ui.note(f"kernel {name} now uses environment {env} (it used {k.get('environment')})")
    while True:
        _ask_settings(c, node, k, home)
        is_new = name not in kernels
        st.add_kernel(name, environment=env, workdir=k.get("workdir"), env=k.get("env"),
                      launcher=k.get("launcher"), launch_timeout=k.get("launch_timeout") or 120,
                      replace=True)
        ui.record_change("added kernel" if is_new else "changed kernel",
                         f"{name} ({env} on {n.get('cluster') or node}, starts in "
                         f"{rtilde(k.get('workdir') or home, home)}"
                         + (", sets " + ", ".join(sorted(k['env'])) if k.get("env") else "") + ")")
        ui.say(f"Writing its launcher on {node} and starting it once ...")
        ok, msg = write_and_test(c, node, name, rep, host_text)
        c.save()
        if ok:
            ui.ok(f"kernel {name} on {node}: {msg}")
            break
        ui.fail(f"kernel {name}: {msg}")
        if ui.yes or not ui.ask_yn("Change its settings and try again?", True):
            return False
        k = dict(kernels[name])
    where = (f"any node of cluster {n['cluster']}" if n.get("cluster") else node)
    ui.say(f"Kernel {name} is set up, on {where}. To plot with it now: python -m "
           f"cubevis.setup jupyter {name} makes a Jupyter kernel for "
           "VisibilityPlotter(kernel_name=...) and prints an example (sessions will "
           f"choose it by name, next stage). Another kernel here: python -m cubevis.setup "
           f"{host_text} --kernel NAME.")
    return True


def check_kernels(c: Ctx, node: str, rep: dict, host_text: str) -> bool:
    """A host set up before: rewrite its kernels' launchers when they
    changed (or were never written) and run each once."""
    ui, st = c.ui, c.st
    names = st.kernels_on(node)
    ui.heading(f"Kernels on {node}")
    home = (rep.get("host") or {}).get("home") or "/"
    good = True
    for name in names:
        if c.redo:
            k = st.section("kernels")[name]
            ui.say(f"Kernel {name}:")
            _ask_settings(c, node, k, home)
        ok, msg = write_and_test(c, node, name, rep, host_text)
        (ui.ok if ok else ui.fail)(f"kernel {name}: {msg}")
        good = good and ok
    c.save()
    ui.say(f"Another kernel here: python -m cubevis.setup {host_text} --kernel NAME"
           + ("" if c.redo else "; --redo asks the kernels' settings again") + ".")
    return good


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------

def cmd_host(c: Ctx, text: str, kernel: Optional[str] = None) -> int:
    ui, st = c.ui, c.st
    if kernel is not None:
        try:
            S.check_name("kernel", kernel)
        except S.StateError as exc:
            ui.fail(str(exc))
            return 1
    host, port = split_port(text)
    try:
        S.check_host(host)
    except S.StateError as exc:
        ui.fail(str(exc))
        return 1
    if not local_steps(c):
        return 1
    node = locate(c, host, port)
    if not node:
        return 1
    c.save()
    n = st.nodes()[node]
    net = st.networks()[n["network"]]
    ui.heading(f"{host}: node {node}" + (f" of cluster {n['cluster']}" if n.get("cluster") else "")
               + (f", through {net['login']['host']}" if net.get("login") else ", reached directly"))
    if net.get("login") and not hop_steps(c, n["network"], None):
        return 1
    if not hop_steps(c, n["network"], node):
        return 1
    rep = discover_step(c, node)
    if rep is None:
        return 1
    record_facts(c, node, rep)
    cluster_step(c, node)
    c.save()
    if net.get("login"):
        inside_step(c, n["network"], node)
    if kernel is None and st.kernels_on(node):
        return 0 if check_kernels(c, node, rep, text) else 1
    env = environment_step(c, node, rep, reuse=kernel is not None)
    c.save()
    if env is None:
        return 1
    return 0 if kernel_step(c, node, env, rep, kernel, text) else 1


def check_kernel(c: Ctx, name: str) -> bool:
    """``setup check``: run kernel *name*'s launcher once on a node it is
    on (the one it was last checked on, if still so); flags what fails."""
    ui, st = c.ui, c.st
    from .. import _fingerprint
    k = st.section("kernels")[name]
    e = st.section("environments").get(k.get("environment"))
    if not e:
        st.flag("kernels", name, f"its environment {k.get('environment')} is gone")
        ui.fail(f"kernel {name}: {k['last_error']}")
        return False
    nodes = [x for x in sorted(st.nodes()) if st.environment_on(k["environment"], x)]
    if k.get("checked_on") in nodes:
        nodes.remove(k["checked_on"])
        nodes.insert(0, k["checked_on"])
    if not k.get("launcher") or not nodes:
        why = "no launcher yet" if not k.get("launcher") else "no node has its environment"
        st.flag("kernels", name, why + ": run python -m cubevis.setup HOST again")
        ui.fail(f"kernel {name}: {k['last_error']}")
        return False
    node = nodes[0]
    try:
        fp, cwd = R.test_launcher(c, st.nodes()[node]["alias"], k["launcher"])
    except R.RemoteError as exc:
        st.flag("kernels", name, f"on {node}: {exc}")
        ui.fail(f"kernel {name} on {node}: {exc}")
        return False
    usable, why = _fingerprint.compatible(local_fingerprint(c), fp)
    if not usable:
        st.flag("kernels", name, f"on {node}: {why}")
        ui.fail(f"kernel {name} on {node}: {why}")
        return False
    st.mark_ok("kernels", name, fingerprint=fp, checked_on=node)
    ui.ok(f"kernel {name} on {node}: starts in {cwd}; {why}")
    return True
