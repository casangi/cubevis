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
   with ``CUBEVIS_ALLOW_MISMATCH=1``.  None usable: setup offers to make its
   own (``envmake``: a minimal conda environment named for this cubevis
   version, made in the background on HOST and polled; answering ``new``
   to the question makes one even when others are usable), and then offers
   to remove its own older ones that nothing here uses
   (``--cleanup HOST`` does that on request).  Where it cannot (a
   development install here, no conda there) it says what to do instead.
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
import time
from typing import Optional

from . import host_dir, host_id, launcher, envs, envmake
from . import remote as R
from . import state as S
from .steps import (Ctx, account_name, first_jump, hop_steps, inside_step, local_steps,
                    parent_domain, split_port, user_ssh)
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
    ui.say(f"Kernels on {host} are started from here over ssh, the way you log in to it. "
           "At some sites you first log in to another computer -- an intermediate host, "
           "often called a login host or bastion, like ssh.example.org -- and from there to "
           f"{host}.")
    import getpass
    me = getpass.getuser()
    acct = account_name()            # ssh's own default User
    # What the user's own ssh configuration says (ssh -G): defaults only.
    g = user_ssh(host)
    real = g.get("hostname") or host
    pj = g.get("proxyjump")
    if g.get("configured"):
        ui.say(f"Your ssh configuration has {host} as {g.get('user')}@{real}"
               + (f", port {g['port']}" if g.get("port", 22) != 22 else "")
               + (f", through {pj}" if pj else "") + "; those are the defaults below.")
    via = ui.ask_yn(f"Do you log in to an intermediate host before logging in to {host}?",
                    True, key="via_login")
    login, lport, luser = None, 22, None
    if via:
        jhost, juser, jport = first_jump(pj) if pj else (None, None, 22)
        dflt = (jhost + (f":{jport}" if jport != 22 else "")) if jhost else ""
        while True:
            text = ui.ask("The intermediate host's name (host, or host:port)", dflt,
                          key="login_host")
            if not text:
                return None
            login, lport = split_port(text)
            lg = user_ssh(login)
            if lg.get("hostname") and lg["hostname"].lower() != login.lower():
                ui.say(f"{login} is {lg['hostname']} (your ssh configuration).")
                login = lg["hostname"]
            if ":" not in text and lg.get("port", 22) != 22:
                lport = lg["port"]
            try:
                S.check_host(login)
                break
            except S.StateError as exc:
                ui.note(str(exc))
                if ui.yes or "login_host" in ui.answers:
                    return None
        cfg_user = juser or (lg.get("user") if lg.get("user") and lg.get("user") != acct else None)
        luser = ui.ask(f"Your user name on {login}", cfg_user or st.local.get("last_user") or me,
                       key="login_user")
    node_user = g.get("user") if g.get("user") and g.get("user") != acct else None
    user = ui.ask(f"Your user name on {host}", node_user or luser or st.local.get("last_user") or me,
                  key="user")
    st.local["last_user"] = user
    domain = None if is_address(real) else parent_domain(real)
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
    # The user's own ssh configuration (ssh -G): a short name's real one.
    g = user_ssh(host)
    real = g.get("hostname") or host
    dom = None if is_address(real) else parent_domain(real)
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
    # Its real name and port as the user's ssh configuration has them
    # (Host cvpost106 / HostName cvpost106.cv.nrao.edu).
    hostname = st.node_hostname(net, host)
    if g.get("hostname") and g["hostname"].lower() != host.lower():
        hostname = g["hostname"]
    if port == 22 and g.get("port", 22) != 22:
        port = g["port"]
    try:
        st.add_node(n, network=net, hostname=hostname, port=port)
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


def no_environment_advice(c: Ctx, node: str, rep: dict, declined: bool = False,
                          asked: bool = False) -> None:
    """Why setup cannot (or was asked not to) make an environment, and what
    to do instead.  *declined*: it could have, and the user said no (or it
    failed): the commands setup would have run.  *asked*: the user asked for
    a new one although others are usable (so they are not "none")."""
    ui = c.ui
    here = local_fingerprint(c)
    if asked:
        dev, why = local_is_development()
        if dev and not os.environ.get("CUBEVIS_SETUP_TEST_INSTALL"):
            ui.say(f"Setup cannot make an environment for this cubevis: it is a development "
                   f"install ({why}), which no package reproduces. Choose one of the "
                   f"environments listed on {node}.")
        else:
            ui.say(f"Setup cannot make an environment on {node}: there is no conda there. "
                   "Choose one of the environments listed.")
        return
    if not declined:
        ui.say(f"No environment on {node} has the same kernel-side code as this computer "
               f"({here}): the code that runs in a kernel must be identical on both sides.")
    dev, why = local_is_development()
    if dev and not os.environ.get("CUBEVIS_SETUP_TEST_INSTALL"):
        ui.say(f"cubevis here is a development install ({why}), which no package "
               f"reproduces. Install the same checkout in an environment on {node} (pip "
               "install -e on a checkout both computers see), then run this again; or, to "
               "use a different one anyway, set CUBEVIS_ALLOW_MISMATCH=1 (kernels may fail at "
               "any message).")
        return
    v = local_version()
    tool = envmake.pick_tool(rep.get("tools"))
    import sys
    py = f"{sys.version_info[0]}.{sys.version_info[1]}"      # the same Python as here
    if tool is None:
        ui.say(f"There is no conda on {node}. Install Miniforge (conda-forge's installer, "
               "https://conda-forge.org/download) on a fast filesystem every node sees -- at "
               "NRAO a project or Lustre area rather than your home, which is slow and has a "
               f"quota -- then run this again, and setup will make the environment.")
        return
    t = rtilde(tool["path"], (rep.get("host") or {}).get("home"))
    ui.say(("To make it yourself, on " if declined else "Setup can make its own environment: "
            "run this again and answer yes; or, on ") + f"{node}:")
    ui._p(f"    {t} create -y -n cubevis-{v} python={py} pip")
    ui._p(f"    {t} run -n cubevis-{v} python -m pip install \"cubevis[visplot]=={v}\"")
    ui.say("and run this again.")


def _node_access(c: Ctx, node: str, rep: dict) -> tuple:
    """``(alias, python3, config_dir)`` for requests to *node*."""
    h = rep.get("host") or {}
    home = h.get("home") or "/"
    return (c.st.nodes()[node]["alias"], rep.get("python_exe") or "python3",
            h.get("config_dir") or posixpath.join(home, ".config", "cubevis"))


def poll_seconds() -> float:
    try:
        return max(0.05, float(os.environ.get("CUBEVIS_SETUP_POLL_SECONDS", "5")))
    except ValueError:
        return 5.0


SPINNER = "|/-\\"      # turns only when the node reports the work moved


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _last_output(st: dict) -> str:
    for line in reversed(st.get("log_tail") or []):
        line = line.strip()
        if line and not line.startswith(("$ ", "-> exit", "----")):
            return line[:70]
    return ""


class Pulse:
    """What the node says about the work, as plain words.  Only what the
    node reports is used: the worker's log growing, CPU time or data moved
    by it and what it started (``envmake.activity_of``).  ``moving``: any of
    that grew since the last answer.  Without that report (not Linux) there
    is no verdict, only the step and the time."""

    def __init__(self):
        self.prev = None
        self.last_move = time.time()
        self.spin = 0

    def update(self, st: dict) -> bool:
        a = st.get("activity") or {}
        moving = False
        if a and self.prev is not None:
            moving = (a.get("cpu", 0) - self.prev.get("cpu", 0) > 0.05
                      or a.get("io", 0) > self.prev.get("io", 0)
                      or a.get("log_size", 0) > self.prev.get("log_size", 0))
        if a:
            self.prev = a
        if moving or self.prev is None or not a:
            self.last_move = time.time()
        if moving:
            self.spin += 1
        return moving

    def quiet(self) -> float:
        return time.time() - self.last_move

    def words(self, st: dict, node: str) -> str:
        a = st.get("activity") or {}
        if not a:
            return ""
        what = "/".join(a.get("procs") or []) or "setup's worker"
        q = self.quiet()
        if q < 20:
            return f"working ({what})"
        if a.get("disk_wait"):
            return (f"waiting on {node}'s disk for {_clock(q)} ({what} cannot proceed until it "
                    "answers; a slow shared filesystem looks like this)")
        if q < 180:
            return f"quiet for {_clock(q)} ({what} is running)"
        return (f"no output, CPU or data for {_clock(q)}, though {what} is still running -- slow "
                "or stuck; it is not stopped")


def wait_for_job(c: Ctx, node: str, rep: dict, name: str) -> Optional[dict]:
    """Poll the environment being made on *node* until it is done: its
    status (``state`` ok, failed or died).  None when setup stops waiting
    (interrupted, or the node cannot be reached): the work goes on there,
    and running setup again picks it up.

    Shows how it is going from what the node reports (``Pulse``): a line
    each minute, one when the verdict changes, and on a terminal a line
    rewritten at each poll whose spinner turns only when the work moved."""
    ui = c.ui
    alias, python3, config_dir = _node_access(c, node, rep)
    home = (rep.get("host") or {}).get("home")
    interval, t0, last_say, step, errors = poll_seconds(), time.time(), time.time(), None, 0
    pulse, stuck, slow_since, st = Pulse(), False, None, {}
    try:
        while True:
            try:
                res = envmake.call(c, alias, python3, envmake.request("status", config_dir, name=name))
                errors = 0
                if slow_since is not None:
                    ui.say(f"  {node} answers again (after {_clock(time.time() - slow_since)}).")
                    slow_since = None
            except R.RemoteError as exc:
                errors += 1
                if slow_since is None:
                    slow_since = time.time()
                    ui.say(f"  {node} is slow to answer ({exc}); the work goes on there.")
                if errors >= 6:
                    ui.fail(f"cannot reach {node} to see how it is going ({exc}); it goes on "
                            f"there -- run this again to pick it up")
                    return None
                ui.live(f"  {step or 'waiting'}   {_clock(time.time() - t0)}   "
                        f"no answer from {node} for {_clock(time.time() - slow_since)}")
                time.sleep(interval)
                continue
            st = res.get("status")
            if not st:
                ui.fail(f"{node} has no record of {name} any more")
                return None
            if st["state"] in ("ok", "failed", "died"):
                return st
            moving = pulse.update(st)
            words = pulse.words(st, node)
            now_stuck = words.startswith(("waiting", "no output"))
            if st.get("step") != step or time.time() - last_say > 60 or now_stuck != stuck:
                step, last_say, stuck = st.get("step"), time.time(), now_stuck
                mins = int((time.time() - t0) // 60)
                last = _last_output(st)
                ui.say(f"  {step} ..." + (f" ({mins} min so far)" if mins else "")
                       + (f" {words}." if words else "")
                       + (f" Last output: {last}" if last and not words.startswith("working") else ""))
            ui.live(f"  {SPINNER[pulse.spin % 4] if pulse.spin else '.'} {step}   "
                    f"{_clock(time.time() - t0)}   {words}"
                    + (f"   {_last_output(st)}" if moving else ""))
            time.sleep(interval)
    except KeyboardInterrupt:
        ui._p()
        ui.say(f"Stopped waiting. The environment goes on being made on {node} (its log: "
               f"{rtilde(envmake_log(config_dir, name), home)}); run this again to pick it up.")
        return None
    finally:
        ui.live_clear()


def envmake_log(config_dir: str, name: str) -> str:
    return posixpath.join(config_dir, "make", name + ".log")


def offer_creation(c: Ctx, node: str, rep: dict, forced: bool = False) -> Optional[tuple]:
    """Make setup's own environment on *node* (``envmake``): where, how big,
    asked once; then made in the background and waited for.  Continues one
    already being made, or one that stopped (it goes on from the step it
    reached).  Returns the new environment's row ``(cand, report, usable,
    why)``, checked, or None when none was made (the reason has been said)."""
    ui = c.ui
    test_install = os.environ.get("CUBEVIS_SETUP_TEST_INSTALL")
    dev, _why = local_is_development()
    tool = envmake.pick_tool(rep.get("tools"))
    if (dev and not test_install) or tool is None:
        no_environment_advice(c, node, rep, asked=forced)
        return None
    import sys
    h = rep.get("host") or {}
    home = h.get("home") or "/"
    v = local_version()
    py = f"{sys.version_info[0]}.{sys.version_info[1]}"
    names = envmake.candidate_names(v)
    alias, python3, config_dir = _node_access(c, node, rep)
    dirs = list((rep.get("envs_dirs") or {}).get(tool["path"]) or [])
    if not dirs:        # the tool would not say: next to its own, where conda keeps them
        dirs = [posixpath.join(posixpath.dirname(posixpath.dirname(tool["path"])), "envs")]
    try:
        plan = envmake.call(c, alias, python3, envmake.request("plan", config_dir, dirs=dirs,
                                                               names=names))
    except R.RemoteError as exc:
        ui.fail(f"cannot look at where an environment would go on {node}: {exc}")
        return None
    if not plan.get("ok"):
        ui.fail(f"cannot look at where an environment would go on {node}: {plan.get('error')}")
        return None
    jobs = plan.get("jobs") or {}
    live = [n for n in names if (jobs.get(n) or {}).get("state") in ("starting", "running")]
    stopped = [n for n in names if (jobs.get(n) or {}).get("state") in ("failed", "died")]
    name = prefix = None
    if live:
        name, prefix = live[0], jobs[live[0]]["prefix"]
        ui.heading(f"An environment is being made on {node}")
        ui.say(f"{rtilde(prefix, home)} (cubevis {v}) was started on {node} earlier and is not "
               "finished: waiting for it.")
    elif stopped:
        name = stopped[0]
        j = jobs[name]
        prefix = j["prefix"]
        ui.heading(f"An environment stopped on {node}")
        ui.note(f"An earlier try to make {rtilde(prefix, home)} stopped at "
                f"{j.get('step') or 'the start'}: {j.get('error') or 'no reason recorded'}")
        for line in ([j["reason"]] if j.get("reason") else (j.get("log_tail") or [])[-6:]):
            ui._p("    " + line)
        ui.say(f"(its log: {rtilde(j.get('log'), home)} on {node})")
        if not ui.ask_yn("Try again? It goes on from the step it stopped at.", True, key="make_env"):
            no_environment_advice(c, node, rep, declined=True)
            return None
    else:
        facts = {d["dir"]: d for d in plan.get("dirs") or []}
        first = facts.get(dirs[0]) or {}
        ui.heading(f"Making an environment for cubevis {v} on {node}")
        ui.say(("You asked for one of setup's own. " if forced else
                f"No environment on {node} has the same kernel-side code as this computer "
                f"({local_fingerprint(c)}), so ") + "setup makes its own there: a minimal conda "
               f"environment with Python {py} and cubevis {v} (cubevis[visplot]=={v}, from "
               f"PyPI), {envmake.EXPECTED_SIZE}. It is made in the background on {node}, so "
               "closing the laptop does not lose it; setup never changes it afterwards (a "
               "later cubevis gets a new one) and never touches your own environments.")
        default = next((d for d in dirs if (facts.get(d) or {}).get("writable")), dirs[0])
        ui.say(f"{os.path.basename(tool['path'])} keeps environments in {rtilde(dirs[0], home)}"
               f" ({envmake.human_bytes(first.get('free'))} free there). Another directory is fine"
               " -- a fast filesystem every node sees (at NRAO a project or Lustre area), "
               "rather than a home directory with a quota.")
        while True:
            where = ui.ask(f"Directory for it on {node}", rtilde(default, home), key="env_dir")
            d = expand_remote(where, home)
            if d not in facts:
                more = envmake.call(c, alias, python3, envmake.request("plan", config_dir,
                                                                       dirs=[d], names=names))
                facts[d] = (more.get("dirs") or [{"dir": d}])[0]
            f = facts[d]
            free_name = next((n for n in names if n not in f.get("taken", [])), None)
            if not f.get("writable"):
                ui.note(f"cannot write in {rtilde(d, home)} on {node}")
            elif free_name is None:
                ui.note(f"every name cubevis-{v}... is taken in {rtilde(d, home)}")
            else:
                break
            if ui.yes or "env_dir" in ui.answers:
                return None
        if f.get("free") is not None and f["free"] < envmake.MIN_FREE:
            ui.note(f"only {envmake.human_bytes(f['free'])} free in {rtilde(d, home)}")
        name, prefix = free_name, posixpath.join(d, free_name)
        if not ui.ask_yn(f"Make {rtilde(prefix, home)} now?", True, key="make_env"):
            no_environment_advice(c, node, rep, declined=True)
            return None
    if not live:
        req = envmake.request("start", config_dir, name=name, prefix=prefix, tool=tool["path"],
                              python=py, spec=envmake.spec(v), version=v,
                              setup={"cubevis": v, "host": host_id()}, install_shell=test_install)
        try:
            res = envmake.call(c, alias, python3, req)
        except R.RemoteError as exc:
            ui.fail(f"cannot start it on {node}: {exc}")
            return None
        if not res.get("ok"):
            ui.fail(f"cannot start it on {node}: {res.get('error')}")
            return None
        ui.record_change("started environment", f"{node}:{rtilde(prefix, home)} (cubevis {v}, "
                         f"made in the background; log {rtilde(envmake_log(config_dir, name), home)})")
        ui.say("Started; setup waits for it (Ctrl-C stops waiting, not the work).")
    st = wait_for_job(c, node, rep, name)
    if st is None:
        return None
    if st["state"] != "ok":
        ui.fail(f"{rtilde(prefix, home)} could not be made: {st.get('error')}")
        if st.get("reason"):
            ui._p("    " + st["reason"])
        else:
            for line in (st.get("log_tail") or [])[-8:]:
                ui._p("    " + line)
        ui.say(f"Its log is {rtilde(st.get('log'), home)} on {node}. Run this again to try again "
               "from the step that failed (nothing done is repeated).")
        return None
    ui.ok(f"{rtilde(prefix, home)} is made")
    more = discover_step(c, node, [prefix], only=True, python3=rep.get("python_exe"))
    got = _rows(c, more) if more else []
    if not got:
        ui.fail(f"{rtilde(prefix, home)} was made, but it cannot be checked")
        return None
    return got[0]


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
    created, ans = None, ""
    if not usable:
        created = offer_creation(c, node, rep)
        if created is None:
            ans = ui.ask(f"Or give the path of an environment on {node} to check (empty: stop)",
                         "", key="environment")
            if not ans:
                return None
    else:
        prev = [i for i, x in enumerate(rows, 1)
                if x[2] and st.find_environment(net, x[0]["python"]) in used]
        default = str((prev or usable)[0])
        ui.say("Choose one by its number, or give the path of another environment there "
               "(its directory or its python); 'new' to have setup make its own; 'none' to "
               "stop here.")
        ans = ui.ask("Environment", default, key="environment")
    pick = created
    if created is not None and not created[2]:
        ui.note(f"{rtilde(created[0]['prefix'], home)} cannot be used: {created[3]}")
        return None
    while pick is None:
        ans = ans.strip()
        if ans.lower() in ("none", "-"):
            return None
        if ans.lower() == "new":
            pick = offer_creation(c, node, rep, forced=True)
            if pick is not None and not pick[2]:
                ui.note(f"{rtilde(pick[0]['prefix'], home)} cannot be used: {pick[3]}")
                return None
        elif ans.isdigit() and 1 <= int(ans) <= len(rows):
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
        pick = None
        if usable and not ans.isdigit() and ans.lower() == "new":     # could not: back to the list
            ans = ui.ask("Environment", str(usable[0]))
            continue
        ans = ui.ask("Environment (empty: stop)", "")
        if not ans:
            return None
    cand, r, _ok, why = pick
    if "CUBEVIS_ALLOW_MISMATCH" in why:
        ui.note(f"using {rtilde(cand['prefix'], home)} although {why}")
    return record_environment(c, node, cand, r)


# --------------------------------------------------------------------------
# Setup's own environments, afterwards: older ones, removed
# --------------------------------------------------------------------------

def own_environments(c: Ctx, rep: dict, net: str) -> list:
    """Setup's own environments in discovery's report *rep* (those with its
    marker) as dicts: ``cand``, ``r`` (its check), ``marker``, ``used_by``
    (kernels here whose environment it is), ``current`` (same kernel-side
    code as here), ``mine`` (made by this computer's setup: another
    computer's may be what its kernels use)."""
    st = c.st
    here = local_fingerprint(c)
    out = []
    for cand in rep.get("environments") or []:
        m = cand.get("marker")
        if not isinstance(m, dict) or m.get("schema") != 1 or not m.get("name"):
            continue                                  # unreadable: not ours to remove
        r = envs.interpret(cand["check"]) if cand.get("check") is not None else {"ok": False}
        used = sorted(k for k, kv in st.section("kernels").items()
                      if (st.section("environments").get(kv.get("environment")) or {}).get("python")
                      == cand["python"]
                      and (st.section("environments").get(kv.get("environment")) or {}).get("network") == net)
        out.append({"cand": cand, "r": r, "marker": m, "used_by": used,
                    "current": bool(r.get("ok")) and r.get("fingerprint") == here,
                    "mine": (m.get("setup") or {}).get("host") == host_id()})
    return out


def remove_environments(c: Ctx, node: str, rep: dict, items: list) -> int:
    """Remove setup's environments *items* (``own_environments``) from
    *node*, and their records here; how many went."""
    ui, st = c.ui, c.st
    alias, python3, config_dir = _node_access(c, node, rep)
    home = (rep.get("host") or {}).get("home")
    net = st.nodes()[node]["network"]
    tools = {t["path"] for t in rep.get("tools") or []}
    gone = 0
    for x in items:
        prefix = x["cand"]["prefix"]
        tool = x["marker"].get("tool")
        if tool not in tools:
            t = envmake.pick_tool(rep.get("tools"))
            tool = t["path"] if t else None
        if tool is None:
            ui.fail(f"no conda tool on {node} to remove {rtilde(prefix, home)} with")
            continue
        try:
            res = envmake.call(c, alias, python3,
                               envmake.request("remove", config_dir, prefix=prefix, tool=tool),
                               timeout=900)
        except R.RemoteError as exc:
            ui.fail(f"removing {rtilde(prefix, home)}: {exc}")
            continue
        if not res.get("ok"):
            ui.fail(f"{res.get('error')}")
            for line in res.get("output") or []:
                ui._p("    " + line)
            continue
        gone += 1
        ui.ok(f"removed {rtilde(prefix, home)}")
        ui.record_change("removed environment", f"{node}:{rtilde(prefix, home)}")
        name = st.find_environment(net, x["cand"]["python"])
        if name:
            try:
                st.remove_environment(name)
            except S.StateError as exc:
                ui.note(str(exc))
    c.save()
    return gone


def offer_cleanup(c: Ctx, node: str, rep: dict, host_text: str, keep_python: str) -> None:
    """After an environment is chosen: setup's own older ones on *node*,
    made from this computer, that differ from this computer's kernel-side
    code and that no kernel here uses -- offered for removal (not by
    default)."""
    ui = c.ui
    home = (rep.get("host") or {}).get("home")
    net = c.st.nodes()[node]["network"]
    old = [x for x in own_environments(c, rep, net)
           if x["mine"] and not x["used_by"] and not x["current"]
           and x["cand"]["python"] != keep_python]
    if not old:
        return
    ui.heading(f"Older environments setup made on {node}")
    ui.say("These differ from this computer's kernel-side code, and no kernel here uses them:")
    for x in old:
        ui._p(f"    {rtilde(x['cand']['prefix'], home)}  (cubevis {x['marker'].get('version')})")
    if ui.ask_yn("Remove them?", False, key="cleanup"):
        remove_environments(c, node, rep, old)
    else:
        ui.say(f"python -m cubevis.setup --cleanup {host_text} removes them whenever you like.")


def cmd_cleanup(c: Ctx, text: str) -> int:
    """``--cleanup HOST``: list the environments setup made on HOST, with
    what uses them, and remove the ones chosen.  The user's own are never
    listed."""
    ui, st = c.ui, c.st
    host, port = split_port(text)
    try:
        S.check_host(host)
    except S.StateError as exc:
        ui.fail(str(exc))
        return 1
    if not (st.find_node(host) or st.cluster_for(host)):
        ui.fail(f"setup does not know {host}: python -m cubevis.setup {host} sets it up first")
        return 1
    if not local_steps(c):
        return 1
    node = locate(c, host, port)
    if not node:
        return 1
    c.save()
    n = st.nodes()[node]
    net = st.networks()[n["network"]]
    ui.heading(f"{host}: the environments setup made there")
    if net.get("login") and not hop_steps(c, n["network"], None):
        return 1
    if not hop_steps(c, n["network"], node):
        return 1
    rep = discover_step(c, node)
    if rep is None:
        return 1
    home = (rep.get("host") or {}).get("home")
    own = own_environments(c, rep, n["network"])
    if not own:
        ui.ok(f"setup has made no environment on {node}")
        return 0
    default = []
    for i, x in enumerate(own, 1):
        v = x["marker"].get("version")
        if x["used_by"]:
            what, good = f"used by kernel {', '.join(x['used_by'])}", True
        elif x["current"]:
            what, good = "not used by a kernel here; same kernel-side code as here", True
        else:
            what, good = "not used by a kernel here; its kernel-side code differs from here", False
        if not x["mine"]:
            what += f"; made from {(x['marker'].get('setup') or {}).get('host') or 'another computer'}"
        (ui.ok if good else ui.note)(f"{i:>2}. {rtilde(x['cand']['prefix'], home)}  (cubevis {v}): {what}")
        if x["mine"] and not x["used_by"] and not x["current"]:
            default.append(str(i))
    ans = ui.ask("Remove which? (numbers separated by spaces; 'none' for none)",
                 " ".join(default), key="remove")
    chosen = []
    for tok in [] if ans.strip().lower() in ("none", "-") else ans.split():
        if not tok.isdigit() or not 1 <= int(tok) <= len(own):
            ui.note(f"{tok!r} is not in the list")
            return 1
        x = own[int(tok) - 1]
        if x["used_by"]:
            ui.note(f"{rtilde(x['cand']['prefix'], home)} is used by kernel "
                    f"{', '.join(x['used_by'])}: not removed")
            continue
        if not x["mine"] and not ui.ask_yn(
                f"{rtilde(x['cand']['prefix'], home)} was made from another computer, whose "
                "kernels may use it. Remove it anyway?", False):
            continue
        if x not in chosen:
            chosen.append(x)
    if not chosen:
        ui.say("Nothing removed.")
        return 0
    if not ui.ask_yn(f"Remove {len(chosen)} environment(s) from {node}?", True, key="confirm"):
        ui.say("Nothing removed.")
        return 0
    return 0 if remove_environments(c, node, rep, chosen) == len(chosen) else 1


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


def default_kernel_name(st, n: dict, node: str, workdir: Optional[str]) -> str:
    """A kernel is named for what it is for: its working directory's name
    (``.../casa/visplot`` -> ``visplot``); in the home directory, the
    cluster's name, or the host's for a host on its own.  Never a cluster
    node's own name: reservations move, the kernel stays (2026-10-28)."""
    base = None
    if workdir:
        base = re.sub(r"[^A-Za-z0-9_.-]", "-", posixpath.basename(workdir.rstrip("/")))
        base = base.strip("-._") or None
    base = base or n.get("cluster") or node
    return _unique(base, st.section("kernels"))


def kernel_step(c: Ctx, node: str, env: str, rep: dict, name: Optional[str],
                host_text: str) -> bool:
    """The kernel's working directory and variables, then its name (default
    from the directory), its launcher written and run once.  *name* given
    (``--kernel NAME``): that kernel, added or changed.  On a cluster, the
    settings start from the cluster's defaults -- its first kernel's."""
    ui, st = c.ui, c.st
    n = st.nodes()[node]
    home = (rep.get("host") or {}).get("home") or "/"
    kernels = st.section("kernels")
    cl_name = n.get("cluster")
    cl = st.section("clusters").get(cl_name) if cl_name else None
    ui.heading(f"A kernel on {cl_name and f'cluster {cl_name}' or node}")
    ui.say("A kernel is what you choose when you plot: the environment, the directory and "
           "variables it starts with, and a name for them. Several kernels can share one "
           "environment (say tw-hya and hrs-imaging, each in its own directory)"
           + ("; on a cluster a kernel can run on any of its nodes."
              if cl_name else "."))

    def network_clash(nm):
        old = kernels.get(nm)
        oe = st.section("environments").get((old or {}).get("environment")) or {}
        return old and oe and oe.get("network") != n["network"]

    if name and network_clash(name):
        ui.fail(f"kernel {name!r} is on another network already")
        return False
    k = dict(kernels.get(name) or {}) if name else {}
    if k and k.get("environment") != env:
        ui.note(f"kernel {name} now uses environment {env} (it used {k.get('environment')})")
    if not k and cl and (cl.get("workdir") or cl.get("env")):
        k = {"workdir": cl.get("workdir"), "env": dict(cl.get("env") or {})}
        ui.say(f"Cluster {cl_name}'s defaults are offered below (from its first kernel).")
    while True:
        _ask_settings(c, node, k, home)
        while not name:
            ans = ui.ask("A name for the kernel (you choose it by this name when you plot)",
                         default_kernel_name(st, n, node, k.get("workdir")), key="kernel")
            try:
                S.check_name("kernel", ans)
            except S.StateError as exc:
                ui.note(str(exc))
                if ui.yes or "kernel" in ui.answers:
                    return False
                continue
            if ans in kernels:
                ui.note(f"there is a kernel {ans!r} already (python -m cubevis.setup "
                        f"{host_text} --kernel {ans} changes it): another name")
                if ui.yes or "kernel" in ui.answers:
                    return False
                continue
            name = ans
        is_new = name not in kernels
        st.add_kernel(name, environment=env, workdir=k.get("workdir"), env=k.get("env"),
                      launcher=k.get("launcher"), launch_timeout=k.get("launch_timeout") or 120,
                      replace=True)
        ui.record_change("added kernel" if is_new else "changed kernel",
                         f"{name} ({env} on {cl_name or node}, starts in "
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
    if cl is not None and "workdir" not in cl and "env" not in cl:
        # The cluster's first kernel: its settings become the cluster's
        # defaults for the next ones (status shows them).
        kk = kernels[name]
        cl["workdir"], cl["env"] = kk.get("workdir"), dict(kk.get("env") or {})
        ui.record_change("cluster defaults", f"{cl_name}: starts in "
                         f"{rtilde(kk.get('workdir') or home, home)}"
                         + (", sets " + ", ".join(sorted(kk['env'])) if kk.get("env") else ""))
        c.save()
    use_kernel(c, name, host_text)
    return True


def use_kernel(c: Ctx, name: str, host_text: str) -> bool:
    """The Jupyter kernel for setup kernel *name* (``jupyterspec``), and
    how to plot with it."""
    from . import jupyterspec as J
    ui, st = c.ui, c.st
    if not J.make(c, name):
        ui.note(f"kernel {name} works, but visplot cannot start it from here yet: "
                f"python -m cubevis.setup jupyter {name} once sshpyk is installed")
        return False
    node = J.default_node(st, name)
    cl = st.nodes()[node].get("cluster")
    ui.ok(f"visplot can use kernel {name} (on {node}):")
    J.show_use(ui, name)
    ui.say("Relative paths are found in the kernel's working directory there. "
           + (f"On another node of cluster {cl}, alongside: python -m cubevis.setup jupyter "
              f"{name} NODE. " if cl else "")
           + f"Another kernel here: python -m cubevis.setup {host_text} --kernel NAME. "
           "python -m cubevis.setup status lists them all.")
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
    for name in names:
        if st.section("kernels")[name].get("status") == "ok":
            use_kernel(c, name, host_text)
    if not c.redo:
        ui.say(f"python -m cubevis.setup --redo {host_text} asks the kernels' settings again.")
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
    ok = kernel_step(c, node, env, rep, kernel, text)
    if ok:
        offer_cleanup(c, node, rep, text, st.section("environments")[env]["python"])
    return 0 if ok else 1


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
