"""``python -m cubevis.setup HOST``: make cubevis kernels work on HOST, the
computer the data is on (remote session design 7c).

The user's task is "my data is on HOST, which I can ssh to"; networks,
routes and launchers are setup's own bookkeeping.  In order:

1. **Where HOST is.**  A node setup knows (by name, alias or full name); a
   node of a known cluster (its name matches the cluster's pattern: added
   without a question); a host in the domain of a known network (it joins
   it: same route); else a new network, named from the domain, whose route
   comes from the user's own ssh configuration (``ssh -G``; stage 2c step
   4): a ``ProxyJump`` there is adopted as the login host, with the users
   and real names configured, and nothing is asked.  Without one, a login
   with the user's ssh is tried (never asking): reached directly, the
   question about a login host defaults to no, else to yes.  The generated
   configuration copies the user's login options (``sshopts``).
2. **Logging in**, each hop: the login host, then HOST, with a stored
   password where one is asked for (``steps.hop_steps``), or -- a hop marked
   ``--2fa`` -- by the user, once, in a terminal.  When the login of a
   host placed in this run fails, nothing about it is kept -- but the
   answers are, as a draft that the next run offers (``forget_new``,
   ``draft_network``).
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

A development install here (a checkout, or a version not on PyPI) is served by
a release from PyPI when its kernel-side code is that release's
(``same_release``); otherwise setup says how to make an environment there
(``development_advice``) -- copying code to the node (``--sync-checkout``,
``--wheel``) was removed on 2026-11-02 (notes section 50).

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

from . import host_dir, host_id, launcher, envs, envmake, checkout, capabilities
from . import remote as R
from . import state as S
from .steps import (Ctx, account_name, first_jump, hop_steps, inside_step, local_steps,
                    network_steps, parent_domain, split_port, user_ssh)
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
    """This cubevis's version.  Imported from a checkout: the installed
    package's when it is an editable install of that checkout, else from git
    (``checkout.git_version``: a checkout used without installing it --
    ``PYTHONPATH``, a symbolic link beside a script); otherwise the installed
    package's.  ``unknown`` when none says."""
    from importlib.metadata import PackageNotFoundError, distribution
    root = checkout.checkout_root()
    try:
        dist = distribution("cubevis")
    except PackageNotFoundError:
        dist = None
    if root is not None and not (dist is not None and _dist_is_here(dist, root)):
        return checkout.git_version(root) or "unknown"
    return dist.version if dist is not None else "unknown"


def _dist_is_here(dist, root) -> bool:
    """Whether the installed cubevis *dist* is an editable install of the
    checkout at *root* (not another copy, found later on sys.path)."""
    try:
        import json
        from pathlib import Path
        url = json.loads(dist.read_text("direct_url.json") or "{}").get("url") or ""
        return url.startswith("file://") and Path(url[len("file://"):]).resolve() == root.resolve()
    except Exception:
        return False


def local_is_development() -> tuple:
    """``(dev, why)``: whether cubevis here cannot be installed elsewhere by
    its version from PyPI -- an editable or local-directory install, or a
    version that is not a release (``1.0.97.dev22+g0a5b519``)."""
    v = local_version()
    root = checkout.checkout_root()
    if root is not None:
        # A checkout (installed editable or not): only an exact, unchanged
        # release tag is that release.
        if not re.match(r"^\d+(\.\d+)*$", v):
            return True, f"a checkout at version {v}, not a release"
        return False, ""
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

def _route_from_jump(pj: str) -> tuple:
    """A ProxyJump value from the user's configuration as setup's route:
    ``(login host as named there, its port, its user or None, the hops
    before it or None)``.  Setup keeps one login host per network, the last
    hop (the one that reaches the nodes); earlier hops become its own
    ProxyJump."""
    hops = [h.strip() for h in pj.split(",") if h.strip()]
    host, user, port = first_jump(hops[-1])
    return host, port, user, (",".join(hops[:-1]) or None)


def new_network(c: Ctx, host: str, port: int = 22) -> Optional[str]:
    """Questions for a host setup knows nothing about: the route.  Returns
    the new network's name (made from the domain; the user sees clusters
    and kernels, not networks).

    The user's own ssh configuration decides first (design 7c, step 4): a
    ``ProxyJump`` there is adopted as the login host, its users and names as
    configured, without questions.  Otherwise a login with the user's ssh is
    tried (never asking, never recording anything): reached directly, the
    question about a login host defaults to no -- one is still worth giving
    when this computer is sometimes off site --, not reached, to yes."""
    ui, st = c.ui, c.st
    ui.heading(f"How this computer reaches {host}")
    import getpass
    me = getpass.getuser()
    acct = account_name()            # ssh's own default User
    g = user_ssh(host, port if port != 22 else None)
    real = g.get("hostname") or host
    pj = g.get("proxyjump")
    login, lport, luser, lname, via = None, 22, None, None, None
    if pj and "via_login" not in ui.answers:
        jname, lport, luser, via = _route_from_jump(pj)
        lg = user_ssh(jname, lport if lport != 22 else None)
        login, lname = lg.get("hostname") or jname, jname
        if lport == 22 and lg.get("port", 22) != 22:
            lport = lg["port"]
        luser = luser or lg.get("user") or acct
        ui.say(f"Your ssh configuration reaches {host} ({real}) through {pj}; setup does the "
               f"same: {luser}@{login}" + (f":{lport}" if lport != 22 else "")
               + (f" (itself reached through {via})" if via else "")
               + f" is the login host, and {host} is reached directly whenever this computer "
               "is inside the network (that is asked below).")
        try:
            S.check_host(login)
        except S.StateError as exc:
            ui.fail(f"the login host from your ssh configuration: {exc}")
            return None
    else:
        ui.say(f"Kernels on {host} are started from here over ssh, the way you log in to it. "
               "At some sites you first log in to another computer -- an intermediate host, "
               "often called a login host or bastion, like ssh.example.org -- and from there "
               f"to {host}.")
        if g.get("configured"):
            ui.say(f"Your ssh configuration has {host} as {g.get('user')}@{real}"
                   + (f", port {g['port']}" if g.get("port", 22) != 22 else "")
                   + ("" if not g.get("proxycommand") else
                      f", through the command {g['proxycommand']} (kept)")
                   + "; those are the defaults below.")
        direct = None
        if "via_login" not in ui.answers and not g.get("proxycommand"):
            ui.say(f"Trying ssh {host} from here (never asking for anything) ...")
            from . import sshopts
            verdict, why = sshopts.try_login(host, port if port != 22 else None)
            ui.log(f"direct login to {host}: {verdict} ({why})")
            if verdict in ("ok", "answers"):
                direct = True
                ui.ok(f"this computer reaches {host} directly"
                      + ("" if verdict == "ok" else f" ({why})"))
            elif verdict == "unreachable":
                direct = False
                ui.note(f"this computer does not reach {host} directly ({why})")
        if direct:
            ui.say(f"An intermediate host is used only when this computer is away from {host}'s "
                   "network; from inside it, kernels go straight to the node.")
            q = (f"When this computer is away from {host}'s network (off site, a visitor network), "
                 f"do you reach {host} through an intermediate host?")
        else:
            q = f"Do you log in to an intermediate host before logging in to {host}?"
        via_login = ui.ask_yn(q, direct is not True and not g.get("proxycommand"), key="via_login")
        if via_login:
            dflt = ""
            others = sorted({(n["login"]["host"], n["login"]["port"]) for n in st.networks().values()
                             if n.get("login")})
            if len(others) == 1:
                dflt = others[0][0] + (f":{others[0][1]}" if others[0][1] != 22 else "")
            while True:
                text = ui.ask("The intermediate host's name (host, or host:port)", dflt,
                              key="login_host")
                if not text:
                    return None
                login, lport = split_port(text)
                lname = login
                lg = user_ssh(login, lport if lport != 22 else None)
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
            cfg_user = lg.get("user") if lg.get("configured") or (lg.get("user") or acct) != acct else None
            luser = ui.ask(f"Your user name on {login}", cfg_user or st.local.get("last_user") or me,
                           key="login_user")
    node_user = g.get("user") if g.get("user") and (g.get("configured") or g["user"] != acct) else None
    if node_user and g.get("configured") and "user" not in ui.answers:
        user = node_user                  # the user's configuration says: not asked
    else:
        user = ui.ask(f"Your user name on {host}", node_user or luser or st.local.get("last_user")
                      or me, key="user")
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
    lrec = st.networks()[name].get("login")
    if lrec is not None:
        if lname and lname.lower() != login.lower():
            lrec["ssh_name"] = lname
        if via:
            lrec["via"] = via
    ui.record_change("added network", f"{name} ({'through ' + login if login else 'direct'}"
                     + (f", domain {domain}" if domain else "") + ")")
    return name


def _draft_key(host: str) -> str:
    return host.lower()


def save_draft(c: Ctx, host: str, network: str) -> None:
    """Keep the answers about *network* (its record: route, users, login
    options) as a draft for *host*, in this computer's file -- not a
    network setup uses."""
    rec = c.st.networks().get(network)
    if not rec:
        return
    import copy
    drafts = dict(c.st.local.get("drafts") or {})
    drafts[_draft_key(host)] = {"saved": S.now(), "name": network, "network": copy.deepcopy(rec)}
    c.st.local["drafts"] = drafts


def drop_draft(c: Ctx, host: str) -> None:
    drafts = dict(c.st.local.get("drafts") or {})
    if drafts.pop(_draft_key(host), None) is not None:
        c.st.local["drafts"] = drafts


def draft_network(c: Ctx, host: str) -> Optional[str]:
    """A host whose first login failed in an earlier run: offer its answers
    from then (default yes), instead of asking them all again.  Returns the
    network made from them, or None (no draft, declined, or its name since
    taken: the questions are asked)."""
    ui, st = c.ui, c.st
    d = (st.local.get("drafts") or {}).get(_draft_key(host))
    if not d or not isinstance(d.get("network"), dict):
        return None
    name, rec = d.get("name"), d["network"]
    login = rec.get("login")
    route = (f"through {(login.get('user') + '@') if login.get('user') else ''}{login.get('host')}"
             + (f" (port {login['port']})" if int(login.get('port') or 22) != 22 else "")
             if login else "directly")
    ui.heading(f"How this computer reaches {host}")
    ui.say(f"Logging in to {host} did not work when it was set up on "
           f"{str(d.get('saved') or '')[:10] or 'an earlier run'}; nothing was kept but the "
           f"answers: {route}, user {rec.get('user')} on {host}.")
    use = ui.ask_yn("Use those answers again?", True, key="use_draft")
    drop_draft(c, host)
    if not use:
        return None
    if not name or name in st.networks():
        ui.note(f"network {name} exists now: the questions are asked again")
        return None
    import copy
    st.networks()[name] = copy.deepcopy(rec)
    ui.record_change("added network", f"{name} ({route}; the answers of "
                     f"{str(d.get('saved') or '')[:10]})")
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
    g = user_ssh(host, port if port != 22 else None)
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
        net = draft_network(c, host) or new_network(c, host, port)
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
    if host.lower() != node["hostname"].lower():
        node["ssh_name"] = host          # what the user's ssh configuration knows it by
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
            ui.say(f"Setup cannot make an environment from PyPI for this cubevis: it is a "
                   f"development install ({why}). " + development_advice(c, node)
                   + f" Or choose one of the environments listed on {node}.")
        else:
            ui.say(f"Setup cannot make an environment on {node}: there is no conda there. "
                   "Choose one of the environments listed.")
        return
    if not declined:
        ui.say(f"No environment on {node} has the same kernel-side code as this computer "
               f"({here}): the code that runs in a kernel must be identical on both sides.")
    dev, why = local_is_development()
    same = same_release(c) if dev else None
    if dev and not same and not os.environ.get("CUBEVIS_SETUP_TEST_INSTALL"):
        ui.say(f"cubevis here is a development install ({why}), which no package on PyPI "
               "reproduces. " + development_advice(c, node))
        return
    v = same or local_version()
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
    ui.say("(\"cubevis[visplot,iclean]\" for interactive clean too) and run this again.")


def same_release(c: Ctx) -> Optional[str]:
    """For a development install here: a published release whose kernel-side
    code is the same, whose PyPI package then serves its kernels; None
    otherwise.  First what releases say they hold (their stamped description
    on PyPI, ``releases``), then -- for releases from before the stamp, or no
    network -- a checkout's nearest tag, from git (``checkout.release_with_same_code``).
    Once per run."""
    if not hasattr(c, "_same_release"):
        from . import releases
        here = local_fingerprint(c)
        found, _n = releases.release_with_same_code(here)
        if found is None:
            root = checkout.checkout_root()
            found = checkout.release_with_same_code(root, here) if root is not None else None
        c._same_release = found
    return c._same_release


def development_advice(c: Ctx, node: str) -> str:
    """What makes a development install's code usable on *node*: an
    environment there with the same code installed, which setup then finds
    by its fingerprint like any other (notes section 50)."""
    host_text = c.st.nodes()[node].get("ssh_name") or node
    root = checkout.checkout_root()
    what = (f"this checkout ({root}) -- a clone of the same commit, or a copy --" if root
            else "the same cubevis")
    return (f"To use it on {node}, put {what} where {node} sees it, install it in an "
            f"environment there (pip install -e DIR in a conda environment of yours), and run "
            f"python -m cubevis.setup {host_text} again: setup finds the environment by its "
            "kernel-side code.  To use a different environment anyway, set "
            "CUBEVIS_ALLOW_MISMATCH=1 (kernels may fail at any message).")


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
    why)``, checked, or None when none was made (the reason has been said).

    From PyPI: this cubevis's version, or -- for a development install here
    -- the release with the same kernel-side code (``same_release``)."""
    ui = c.ui
    test_install = os.environ.get("CUBEVIS_SETUP_TEST_INSTALL")
    dev, _why = local_is_development()
    tool = envmake.pick_tool(rep.get("tools"))
    # A checkout whose kernel-side code is that of its nearest release: the
    # release from PyPI is the same code (no copy needed).
    same = same_release(c) if dev and not test_install else None
    if (dev and not test_install and not same) or tool is None:
        no_environment_advice(c, node, rep, asked=forced)
        return None
    import sys
    h = rep.get("host") or {}
    home = h.get("home") or "/"
    v = local_version()
    py = f"{sys.version_info[0]}.{sys.version_info[1]}"
    v = same or v
    names = envmake.candidate_names(v)

    def what(spec_text):
        return (f"cubevis {v} ({spec_text}, from PyPI"
                + (f"; this checkout ({local_version()}) has the same kernel-side code"
                   if same else "") + ")")

    spec_text = envmake.spec(v)
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
        spec_text = j.get("spec") or spec_text          # as it was started
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
               f"environment with Python {py} and {what(spec_text)}, {envmake.EXPECTED_SIZE}. "
               f"It is made in the background on {node}, so closing the laptop does not lose it; "
               "setup only adds to it afterwards, when you ask (a later cubevis gets a new one), "
               "and never touches your own environments.")
        if ask_iclean(c, node):
            spec_text = envmake.spec(v, iclean=True)
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
                ui.note(f"every name {names[0]}... is taken in {rtilde(d, home)}")
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
                              python=py, spec=spec_text, version=v,
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
# 5b. Interactive clean: a capability of the environment, CASA's data
# --------------------------------------------------------------------------

ICLEAN_SIZE = "about 300 MB more"        # casatools + casatasks + casaconfig wheels, installed
CASA_DATA_SIZE = "about 850 MB"          # casadata 2025.9.22, installed


def ask_iclean(c: Ctx, node: str) -> bool:
    """Should kernels on *node* run interactive clean (casatasks in their
    environment)?"""
    c.ui.say(f"Kernels on {node} can run interactive clean (iclean) too: their environment then "
             f"also holds casatasks and casatools ({ICLEAN_SIZE}).")
    c.iclean_answer = c.ui.ask_yn(f"Should kernels on {node} run interactive clean?", True,
                                  key="iclean")
    return c.iclean_answer


def environment_iclean(st, env: str) -> Optional[bool]:
    """Can environment *env* run interactive clean: its check's answer, else
    its kernels' (their launcher tests); None when nothing says."""
    e = st.section("environments")[env]
    if e.get("iclean") is not None:
        return e["iclean"]
    said = [(k.get("capabilities") or {}).get("iclean") for k in st.section("kernels").values()
            if k.get("environment") == env and k.get("capabilities")]
    return None if not said else all(said)


def made_by_setup(e: dict) -> bool:
    """An environment setup made (its marker): the only kind it adds to."""
    m = e.get("marker")
    return isinstance(m, dict) and m.get("schema") == 1


def iclean_step(c: Ctx, node: str, env: str, rep: dict) -> None:
    """Environment *env* cannot run interactive clean: for one of setup's
    own, offer to add it (once: a no is remembered until ``--redo``); for
    the user's own, say how."""
    ui, st = c.ui, c.st
    e = st.section("environments")[env]
    if environment_iclean(st, env) is not False:
        return                          # can, or nothing says (asked once it is checked)
    if getattr(c, "iclean_answer", None) is False:
        e["iclean_declined"] = True     # answered no when it was made, in this run
        return
    if e.get("iclean_declined") and not c.redo:
        return
    home = (rep.get("host") or {}).get("home")
    where = rtilde(e.get("prefix"), home)
    ui.heading(f"Interactive clean on {node}")
    if not made_by_setup(e):
        ui.say(f"Kernels using {where} cannot run interactive clean: it has no casatasks. It is "
               f"your environment, so setup does not install into it. To add it: "
               f"{rtilde(e['python'], home)} -m pip install \"cubevis[iclean]=={e.get('cubevis')}\" "
               "there, then run this again (--redo says this again).")
        e["iclean_declined"] = True
        return
    ui.say(f"Kernels using {where} cannot run interactive clean: it has no casatasks. Setup "
           f"made that environment, so it can add casatasks and casatools ({ICLEAN_SIZE}), "
           "in the background like making it.")
    if not ui.ask_yn("Add interactive clean to it?", True, key="add_iclean"):
        e["iclean_declined"] = True
        ui.say("Not added; python -m cubevis.setup --redo HOST asks again.")
        return
    e.pop("iclean_declined", None)
    add_to_environment(c, node, rep, env, iclean=True)


def add_to_environment(c: Ctx, node: str, rep: dict, env: str, iclean: bool = False,
                       casa_data: bool = False) -> bool:
    """Add interactive clean (*iclean*) and/or a private copy of CASA's
    data (*casa_data*) to setup's own environment *env* (``envmake`` add),
    wait for it, and check it again; True when it was added."""
    ui, st = c.ui, c.st
    e = st.section("environments")[env]
    home = (rep.get("host") or {}).get("home")
    prefix = e["prefix"]
    alias, python3, config_dir = _node_access(c, node, rep)
    v = e.get("cubevis")
    site = posixpath.join(prefix, capabilities.SITE_CONFIG_FILE)
    update = {}
    if iclean:
        update["iclean"] = True
    if casa_data:
        update["casa_site_config"] = site
    req = envmake.request(
        "add", config_dir, prefix=prefix, spec=envmake.add_spec(v, iclean, casa_data), version=v,
        step="installing " + " and ".join(
            (["interactive clean (pip)"] if iclean else []) + (["CASA's data (pip)"] if casa_data else [])),
        site_config=({"file": capabilities.SITE_CONFIG_FILE, "text": capabilities.SITE_CONFIG_TEXT}
                     if casa_data else None),
        marker_update=update, setup={"cubevis": local_version(), "host": host_id()},
        install_shell=os.environ.get("CUBEVIS_SETUP_TEST_ADD"))
    try:
        res = envmake.call(c, alias, python3, req)
    except R.RemoteError as exc:
        res = {"ok": False, "error": str(exc)}
    if not res.get("ok"):
        ui.fail(f"cannot add to {rtilde(prefix, home)} on {node}: {res.get('error')}")
        return False
    what = " and ".join((["interactive clean"] if iclean else [])
                        + (["a private copy of CASA's data"] if casa_data else []))
    ui.record_change("adding to environment", f"{node}:{rtilde(prefix, home)}: {what} (in the "
                     f"background; log {rtilde(envmake_log(config_dir, posixpath.basename(prefix)), home)})")
    ui.say("Started; setup waits for it (Ctrl-C stops waiting, not the work).")
    js = wait_for_job(c, node, rep, posixpath.basename(prefix.rstrip("/")))
    if js is None:
        return False
    if js["state"] != "ok":
        ui.fail(f"{what} could not be added to {rtilde(prefix, home)}: {js.get('error')}")
        for line in ([js["reason"]] if js.get("reason") else (js.get("log_tail") or [])[-8:]):
            ui._p("    " + line)
        ui.say(f"Its log is {rtilde(js.get('log'), home)} on {node}. Run this again to go on from "
               "the step that failed.")
        return False
    more = discover_step(c, node, [prefix], only=True, python3=rep.get("python_exe"))
    got = _rows(c, more) if more else []
    if not got:
        ui.fail(f"{what} was added to {rtilde(prefix, home)}, but it cannot be checked")
        return False
    record_environment(c, node, got[0][0], got[0][1])
    ui.ok(f"{rtilde(prefix, home)} now has {what}")
    return True


def _private_data_in_use(e: dict, data: dict) -> bool:
    return bool(e.get("casa_site_config") and data.get("ready")
                and str(data.get("measurespath") or "").startswith(e["prefix"].rstrip("/") + "/"))


def casa_data_step(c: Ctx, node: str, name: str, rep: dict, host_text: str) -> None:
    """Kernel *name* runs interactive clean: where it finds CASA's data.
    Found (your configuration or the site's): use it, or give the
    environment a private copy -- asked once per environment
    (``casa_data_choice``; ``--redo`` asks again).  Not found: a private
    copy, for setup's own environment; what to do, for yours.  A private
    copy is the ``casadata`` package with a site configuration pointing at
    it, which the kernels get as ``CASASITECONFIG`` (``launcher.text``)."""
    ui, st = c.ui, c.st
    k = st.section("kernels")[name]
    if not (k.get("capabilities") or {}).get("iclean"):
        return
    env = k["environment"]
    e = st.section("environments")[env]
    data = k.get("casa_data") or {}
    home = (rep.get("host") or {}).get("home")
    if e.get("casa_site_config"):
        if _private_data_in_use(e, data):
            ui.ok(f"kernel {name}: CASA's data is the environment's own copy")
        else:
            ui.note(f"kernel {name}: CASA's data is {capabilities.describe_data(data, home)}, not the "
                    f"environment's own copy"
                    + (f" -- {', '.join(rtilde(f, home) for f in data.get('files') or [])} "
                       "sets it (your own configuration overrides the site's)"
                       if data.get("files") else ""))
        return
    if data.get("ready") and (not made_by_setup(e)
                              or e.get("casa_data_choice") == "own" and not c.redo):
        ui.ok(f"kernel {name}: CASA's data {capabilities.describe_data(data, home)}")
        return
    ui.heading(f"CASA's data for interactive clean on {node}")
    if data.get("ready"):
        by = data.get("files") or []
        ui.say(f"Interactive clean in kernel {name} finds CASA's data in "
               f"{rtilde(data.get('measurespath'), home)} ("
               + (f"set by {', '.join(rtilde(f, home) for f in by)}" if by else "casaconfig's default")
               + ("; CASA updates it itself" if data.get("auto_update") else "; not updated by CASA")
               + "). The environment can have a private copy instead: the casadata package "
               f"({CASA_DATA_SIZE}), which nothing updates -- so cleaning there never changes "
               "with your data.")
        default = "u"
    else:
        ui.say(f"Interactive clean in kernel {name} finds no CASA data: "
               f"{data.get('why') or 'not known why'}.")
        if not made_by_setup(e):
            ui.say("Its environment is yours: install casadata there (pip install casadata) and "
                   "point CASA at it -- measurespath in ~/.casa/config.py, or a site "
                   "configuration given as CASASITECONFIG among the kernel's variables -- or create "
                   "~/.casa/data for CASA to fill, then run this again.")
            return
        ui.say(f"Setup can give its environment a private copy: the casadata package "
               f"({CASA_DATA_SIZE}), which nothing updates.")
        default = "p"
    a = ui.ask_choice("[u]se " + ("it" if data.get("ready") else "nothing (decide later)")
                      + ", or give the environment a [p]rivate copy?", default, ("u", "p"),
                      key="casa_data")
    if a == "u":
        if data.get("ready"):
            e["casa_data_choice"] = "own"
        return
    if not add_to_environment(c, node, rep, env, casa_data=True):
        return
    e = st.section("environments")[env]
    e["casa_data_choice"] = "private"
    for kn in st.kernels_on(node):
        if st.section("kernels")[kn].get("environment") == env:
            ok, msg = write_and_test(c, node, kn, rep, host_text)
            (ui.ok if ok else ui.fail)(f"kernel {kn}: {msg}")
            if ok:
                d = st.section("kernels")[kn].get("casa_data") or {}
                if not _private_data_in_use(e, d):
                    ui.note(f"kernel {kn}: CASA's data is {capabilities.describe_data(d, home)}"
                            + (f" -- {', '.join(rtilde(f, home) for f in d.get('files') or [])} "
                               "overrides the environment's copy" if d.get("files") else ""))
    c.save()


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


def made_by(x: dict) -> str:
    """Which computer's setup made environment *x* (``own_environments``),
    and when: ``this computer, 2026-10-28`` / ``zuul06, 2026-10-28``."""
    m = x["marker"]
    who = "this computer" if x["mine"] else ((m.get("setup") or {}).get("host") or "another computer")
    when = str(m.get("created") or "")[:10]
    return who + (f", {when}" if when else "")


def offer_cleanup(c: Ctx, node: str, rep: dict, host_text: str, keep_python: str) -> None:
    """After an environment is chosen: setup's own older ones on *node* --
    made from any computer (their marker says), since an environment can
    vanish anyway (removed by hand, or from another computer) and kernels
    are ready for that (Darrell 2026-11-02) -- that differ from this
    computer's kernel-side code and that no kernel here uses: offered for
    removal (not by default)."""
    ui = c.ui
    home = (rep.get("host") or {}).get("home")
    net = c.st.nodes()[node]["network"]
    old = [x for x in own_environments(c, rep, net)
           if not x["used_by"] and not x["current"] and x["cand"]["python"] != keep_python]
    if not old:
        return
    ui.heading(f"Older environments setup made on {node}")
    ui.say("These differ from this computer's kernel-side code, and no kernel here uses them:")
    for x in old:
        ui._p(f"    {rtilde(x['cand']['prefix'], home)}  (cubevis {x['marker'].get('version')}; "
              f"made from {made_by(x)})")
    if ui.ask_yn("Remove them?", False, key="cleanup"):
        remove_environments(c, node, rep, old)
    else:
        ui.say(f"python -m cubevis.setup --cleanup {host_text} removes them whenever you like.")


def cmd_cleanup(c: Ctx, text: str) -> int:
    """``--cleanup HOST``: list the environments setup made on HOST (from
    any computer), with what uses them here, and remove the ones chosen.
    The user's own are never listed; one a kernel here uses is never
    removed."""
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
        what += f"; made from {made_by(x)}"
        (ui.ok if good else ui.note)(f"{i:>2}. {rtilde(x['cand']['prefix'], home)}  (cubevis {v}): {what}")
        if not x["used_by"] and not x["current"]:
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
             visplot=r.get("visplot"), iclean=r.get("iclean"), cubevis_via=via, tool=tool,
             fingerprint=r.get("fingerprint"), marker=cand.get("marker"), checked_on=node,
             base_prefix=r.get("base_prefix"))
    if (cand.get("marker") or {}).get("casa_site_config"):
        e["casa_site_config"] = cand["marker"]["casa_site_config"]
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

    def write_test():
        """Write the launcher (when its text changed) and run it once:
        ``(fp, cwd, report, seconds)``; RemoteError."""
        text = launcher.text(name, k, e, home, host_id(), host=host_text)
        try:
            did = R.install_launcher(c, alias, rep.get("python_exe") or "python3", path, text)
        except R.RemoteError as exc:
            raise R.RemoteError(f"cannot write its launcher on {node}: {exc}") from None
        if did == "written":
            ui.record_change("wrote launcher", f"{node}:{rtilde(path, home)}")
        k["launcher"] = path
        t0 = time.monotonic()
        try:
            fp_, cwd_, caps_ = R.test_launcher(c, alias, path)
        except R.RemoteError as exc:
            raise R.RemoteError(f"on {node}: {exc}") from None
        took_ = time.monotonic() - t0
        ui.log(f"kernel {name}: the launcher test took {took_:.1f} s"
               + (" (PYTHONHOME set)" if k.get("pythonhome") else ""))
        return fp_, cwd_, caps_, took_

    try:
        fp, cwd, caps, took = write_test()
    except R.RemoteError as exc:
        st.flag("kernels", name, str(exc))
        return False, st.section("kernels")[name]["last_error"]
    if (took > SLOW_START and not k.get("pythonhome") and launcher.home_of(e)
            and k.get("pythonhome_by") != "you"):
        # Slow: most likely Python's search for its own installation on an
        # automounted filesystem (notes sections 52, 53).  Try PYTHONHOME;
        # keep it only when it helps.
        ui.say(f"Starting it took {took:.0f} s; trying again with PYTHONHOME="
               f"{launcher.home_of(e)} (it spares Python the search for its own installation, "
               "slow on automounted filesystems) ...")
        k["pythonhome"], k["pythonhome_by"] = True, "setup"
        try:
            fp2, cwd2, caps2, took2 = write_test()
        except R.RemoteError as exc:
            fp2, took2 = None, None
            ui.log(f"kernel {name}: with PYTHONHOME: {exc}")
        if took2 is not None and took2 < took / 2:
            fp, cwd, caps = fp2, cwd2, caps2
            k["pythonhome_times"] = [round(took, 1), round(took2, 1)]
            ui.ok(f"with PYTHONHOME it starts in {took2:.1f} s: kernel {name} sets it "
                  f"(python -m cubevis.setup {host_text} --no-pythonhome takes it out)")
            ui.record_change("PYTHONHOME", f"kernel {name}: set ({launcher.home_of(e)}; "
                             f"start {took:.0f} s -> {took2:.1f} s)")
        else:
            ui.note("PYTHONHOME does not help here" + (f" ({took2:.0f} s)" if took2 else "")
                    + ": not set")
            k["pythonhome"], k["pythonhome_by"] = False, None
            try:
                fp, cwd, caps, _t = write_test()
            except R.RemoteError as exc:
                st.flag("kernels", name, str(exc))
                return False, st.section("kernels")[name]["last_error"]
    usable, why = _fingerprint.compatible(local_fingerprint(c), fp)
    if not usable:
        st.flag("kernels", name, f"on {node}: {why}")
        return False, st.section("kernels")[name]["last_error"]
    st.mark_ok("kernels", name, fingerprint=fp, checked_on=node, **capability_fields(caps))
    return True, (f"starts in {rtilde(cwd, home)}" + (f"; {why}" if fp != local_fingerprint(c) else "")
                  + (f"; runs {capabilities.describe(caps.get('capabilities'))}" if caps else ""))


def capability_fields(report: Optional[dict]) -> dict:
    """A kernel record's fields from what its launcher test reported
    (``capabilities.parse``): nothing from a kernel that did not say."""
    if not report:
        return {}
    return {"capabilities": report.get("capabilities") or {},
            "capability_errors": report.get("errors") or {},
            "casa_data": report.get("casa_data")}


# A launcher test slower than this (seconds): PYTHONHOME is tried.
SLOW_START = 15.0


def set_pythonhome(c: Ctx, names, on: bool) -> None:
    """``--pythonhome`` / ``--no-pythonhome``: kernels *names* set
    ``PYTHONHOME`` in their launchers (rewritten by the run), or not -- your
    choice (``pythonhome_by``), which setup's own trying when a start is
    slow (``write_and_test``) never overrides."""
    ui, st = c.ui, c.st
    for name in names:
        k = st.section("kernels")[name]
        e = st.section("environments").get(k.get("environment")) or {}
        if on and not launcher.home_of(e):
            ui.note(f"kernel {name}: its environment's Python installation is not known yet "
                    "(checked before 2026-11-02): run setup again to set PYTHONHOME")
        k["pythonhome_by"] = "you"
        if bool(k.get("pythonhome")) != on:
            k["pythonhome"] = on
            k.pop("pythonhome_times", None)
            ui.record_change("PYTHONHOME", f"kernel {name}: "
                             + (f"set ({launcher.home_of(e) or 'when known'})" if on else "not set"))


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
                      pythonhome=(k.get("pythonhome") if c.pythonhome is None else c.pythonhome),
                      replace=True)
        kernels[name]["pythonhome_by"] = k.get("pythonhome_by") if c.pythonhome is None else "you"
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
    casa_data_step(c, node, name, rep, host_text)
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
    caps = st.section("kernels")[name].get("capabilities")
    ui.ok(f"kernel {name} (on {node}) runs {capabilities.describe(caps)}:")
    J.show_use(ui, name, caps=caps)
    ui.say("Relative paths are found in the kernel's working directory there. "
           + (f"On another node of cluster {cl}, alongside: python -m cubevis.setup jupyter "
              f"{name} NODE. " if cl else "")
           + f"Another kernel here: python -m cubevis.setup {host_text} --kernel NAME. "
           f"Its working directory, variables or environment, later: python -m cubevis.setup "
           f"kernel {name} --workdir DIR (--env, --environment; no option asks). "
           "python -m cubevis.setup status lists them all.")
    return True


def forget_kernel(c: Ctx, name: str) -> None:
    """Forget kernel *name*: its record, the Jupyter kernels here that start
    it, and its environment's record when no other kernel uses it.  Nothing
    on the node is touched."""
    ui, st = c.ui, c.st
    from . import jupyterspec as J
    env = st.section("kernels")[name].get("environment")
    for jname, _node in J.specs_for(st, name):
        try:
            from jupyter_client.kernelspec import KernelSpecManager
            KernelSpecManager().remove_kernel_spec(jname)
            ui.record_change("removed jupyter kernel", jname)
        except Exception as exc:
            ui.note(f"Jupyter kernel {jname}: {exc}")
    st.remove_kernel(name)
    ui.record_change("forgot kernel", name)
    if env in st.section("environments"):
        try:
            st.remove_environment(env)
            ui.record_change("forgot environment", env)
        except S.StateError:
            pass


def environment_gone(c: Ctx, node: str, name: str, rep: dict, host_text: str,
                     gone: bool = True) -> bool:
    """Kernel *name*'s environment is gone from *node* (removed by hand, or
    by setup from another computer) -- or, *gone* false, its kernel-side
    code differs from this computer's (cubevis changed here, or there):
    give the kernel another environment (its name, working directory and
    variables stay), forget it, or keep it flagged.  True when the kernel
    works again."""
    ui, st = c.ui, c.st
    k = st.section("kernels")[name]
    old = k.get("environment")
    if not gone:
        ui.say(f"Kernel {name}'s environment has other kernel-side code than this computer's "
               "cubevis. Choosing another lists the environments there with theirs -- one you "
               "installed this cubevis in, or setup's own for a release.")
    a = ui.ask_choice(f"Kernel {name}: [r]eplace its environment (choose another; the kernel's "
                      "name, directory and variables stay), [f]orget the kernel, or [k]eep it "
                      "as it is?", "r", ("r", "f", "k"), key="env_gone" if gone else "env_differs")
    if a == "f":
        forget_kernel(c, name)
        ui.ok(f"kernel {name} forgotten")
        return True
    if a == "k":
        return False
    env = environment_step(c, node, rep, reuse=False)
    if env is None:
        return False
    k["environment"] = env
    ui.record_change("kernel environment", f"{name}: {env} (was {old}"
                     + (", gone)" if gone else ", other kernel-side code)"))
    if gone and old in st.section("environments") and old != env:
        try:
            st.remove_environment(old)
            ui.record_change("forgot environment", f"{old} (gone)")
        except S.StateError:
            pass
    ok, msg = write_and_test(c, node, name, rep, host_text)
    (ui.ok if ok else ui.fail)(f"kernel {name}: {msg}")
    return ok


def check_kernels(c: Ctx, node: str, rep: dict, host_text: str) -> bool:
    """A host set up before: rewrite its kernels' launchers when they
    changed (or were never written) and run each once."""
    ui, st = c.ui, c.st
    names = st.kernels_on(node)
    if c.pythonhome is not None:
        set_pythonhome(c, names, c.pythonhome)
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
        if not ok and launcher.ENV_GONE in (msg or ""):
            ok = environment_gone(c, node, name, rep, host_text)
        elif not ok and "kernel-side code differs" in (msg or ""):
            ok = environment_gone(c, node, name, rep, host_text, gone=False)
        good = good and ok
    c.save()
    # Interactive clean: offered for each environment the kernels use that
    # lacks it (added: their kernels are tested again), then CASA's data.
    for env in sorted({st.section("kernels")[n].get("environment") for n in names
                       if n in st.section("kernels")}):
        if env not in st.section("environments"):
            continue
        before = environment_iclean(st, env)
        iclean_step(c, node, env, rep)
        if before is False and st.section("environments")[env].get("iclean"):
            for name in names:
                if (st.section("kernels").get(name) or {}).get("environment") == env:
                    ok, msg = write_and_test(c, node, name, rep, host_text)
                    (ui.ok if ok else ui.fail)(f"kernel {name}: {msg}")
        c.save()
    for name in names:
        if (st.section("kernels").get(name) or {}).get("status") == "ok":
            casa_data_step(c, node, name, rep, host_text)
    c.save()
    for name in names:
        if name in st.section("kernels") and st.section("kernels")[name].get("status") == "ok":
            use_kernel(c, name, host_text)
    if not c.redo:
        ui.say(f"python -m cubevis.setup --redo {host_text} asks the kernels' settings again.")
    return good


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------

def forget_new(c: Ctx, node: str, network: str, had_nodes: set, had_nets: set,
               host: str) -> None:
    """The login to a host placed in this run failed: nothing about it is
    kept (design 7c: the login is tested before anything is saved), so the
    next run asks again -- after the user has fixed what was wrong, or with
    other answers.  A stored password stays in its store."""
    ui, st = c.ui, c.st
    net = st.networks().get(network) or {}
    if net.get("twofactor") or (net.get("login") or {}).get("twofactor"):
        return          # the user logs in (a terminal, a command): kept for that
    gone = []
    if node not in had_nodes and node in st.nodes():
        st.remove_node(node)
        gone.append(f"node {node}")
    drafted = False
    if network not in had_nets and network in st.networks() and not st.nodes(network):
        try:
            save_draft(c, host, network)
            st.remove_network(network)
            gone.append(f"network {network}")
            drafted = True
        except S.StateError:
            drop_draft(c, host)
    if gone:
        c.save()
        ui.record_change("forgot", ", ".join(gone) + " (the login did not work)")
        ui.say(f"Nothing about {host} was kept, since logging in did not work"
               + ("; your answers were, and python -m cubevis.setup "
                  f"{host} offers them again." if drafted else
                  f"; python -m cubevis.setup {host} asks again."))


def describe_change(ch: dict) -> str:
    """One change of ``sshgen.changes`` in words."""
    if ch.get("hops"):
        return (f"{ch['label']}: your ssh now reaches it {('through ' + ch['hops'][1]) if ch['hops'][1] != 'directly' else 'directly'}"
                f" (setup recorded {('through ' + ch['hops'][0]) if ch['hops'][0] != 'directly' else 'directly'})")
    parts = [f"now {x}" for x in ch["added"]] + [f"no longer {x}" for x in ch["removed"]]
    return f"{ch['label']} (ssh -G {ch['name']}): " + "; ".join(parts)


def adopt_step(c: Ctx, network: str, node: Optional[str] = None) -> None:
    """``setup HOST`` (and ``network NAME``): what changed in the user's ssh
    configuration for this network's hosts since setup recorded it is shown,
    and taken over on a yes (default yes) -- the login tests that follow
    try it at once.  Never silently (notes section 55)."""
    from . import sshgen
    ui, st = c.ui, c.st
    ch = sshgen.changes(st, sshgen.network_aliases(st, network, node))
    if not ch:
        return
    ui.heading("Your ssh configuration changed")
    ui.say("Setup's ssh configuration carries what your own said when these hosts were set "
           "up; since then:")
    for x in ch:
        ui._p("    " + describe_change(x))
    if ui.ask_yn("Use your ssh configuration as it is now for them?", True, key="adopt_ssh"):
        sshgen.adopt(st, sorted({x["alias"] for x in ch}))
        for x in ch:
            ui.record_change("adopted ssh options", describe_change(x))
    else:
        ui.say("Kept as recorded; python -m cubevis.setup status lists the differences.")


def set_twofactor(c: Ctx, network: str, on: bool) -> None:
    """``--2fa`` / ``--no-2fa``: the hop of *network* whose login asks for a
    second factor -- its login host when it has one, else its nodes.  No
    stored password is used for it; its shared connection is opened by the
    user, in a terminal (``steps.hop_steps``)."""
    ui, st = c.ui, c.st
    net = st.networks()[network]
    rec = net.get("login") or net
    what = f"login host {rec['host']}" if net.get("login") else f"the nodes of network {network}"
    if bool(rec.get("twofactor")) == on:
        return
    rec["twofactor"] = on
    ui.record_change("second factor", f"{what}: {'asks for one' if on else 'does not ask for one'}")


def cmd_host(c: Ctx, text: str, kernel: Optional[str] = None,
             twofactor: Optional[bool] = None, pythonhome: Optional[bool] = None) -> int:
    ui, st = c.ui, c.st
    c.pythonhome = pythonhome
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
    from . import sshgen
    for x in sshgen.changes(st):
        ui.log(f"ssh configuration changed: {describe_change(x)}")
    had_nets, had_nodes = set(st.networks()), set(st.nodes())
    node = locate(c, host, port)
    if not node:
        return 1
    n = st.nodes()[node]
    if twofactor is not None:
        set_twofactor(c, n["network"], twofactor)
    if n["network"] in had_nets:
        adopt_step(c, n["network"], node)
    c.save()
    net = st.networks()[n["network"]]
    ui.heading(f"{host}: node {node}" + (f" of cluster {n['cluster']}" if n.get("cluster") else "")
               + (f", through {net['login']['host']}" if net.get("login") else ", reached directly"))
    if not ((not net.get("login") or hop_steps(c, n["network"], None))
            and hop_steps(c, n["network"], node)):
        forget_new(c, node, n["network"], had_nodes, had_nets, host)
        return 1
    if (st.local.get("drafts") or {}).get(_draft_key(host)) is not None:
        drop_draft(c, host)
        c.save()
    rep = discover_step(c, node)
    if rep is None:
        return 1
    record_facts(c, node, rep)
    cluster_step(c, node)
    c.save()
    if net.get("login"):
        inside_step(c, n["network"], node)
    if kernel is None and st.kernels_on(node):
        good = check_kernels(c, node, rep, text)
        # Another kernel here -- another environment (one with casatasks for
        # interactive clean, say), directory or variables: offered, not
        # only through --kernel NAME (Darrell, 2026-11-09).
        ui.heading(f"Another kernel on {node}")
        ui.say(f"Kernels here: {', '.join(st.kernels_on(node))}. Another one can use another "
               "environment (one that can run interactive clean, say), working directory or "
               "variables; python -m cubevis.setup kernel NAME changes one of these instead.")
        if not ui.ask_yn("Add another kernel?", False, key="another_kernel"):
            return 0 if good else 1
    elif kernel is not None and kernel in st.section("kernels"):
        # --kernel NAME of a kernel there is: its settings asked again, in
        # its own environment (python -m cubevis.setup kernel NAME
        # --environment ENV moves it to another).
        env = st.section("kernels")[kernel].get("environment")
        if env in st.section("environments") and st.environment_on(env, node):
            iclean_step(c, node, env, rep)
            c.save()
            return 0 if kernel_step(c, node, env, rep, kernel, text) else 1
    # A new kernel: its environment is always chosen (the one the host's
    # kernels use is the default) -- before, --kernel NEW took that one
    # without asking, so a second environment could not be chosen.
    env = environment_step(c, node, rep, reuse=False)
    c.save()
    if env is None:
        return 1
    iclean_step(c, node, env, rep)
    c.save()
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
        fp, cwd, caps = R.test_launcher(c, st.nodes()[node]["alias"], k["launcher"])
    except R.RemoteError as exc:
        st.flag("kernels", name, f"on {node}: {exc}")
        ui.fail(f"kernel {name} on {node}: {exc}")
        return False
    usable, why = _fingerprint.compatible(local_fingerprint(c), fp)
    if not usable:
        st.flag("kernels", name, f"on {node}: {why}")
        ui.fail(f"kernel {name} on {node}: {why}")
        return False
    st.mark_ok("kernels", name, fingerprint=fp, checked_on=node, **capability_fields(caps))
    ui.ok(f"kernel {name} on {node}: starts in {cwd}; {why}"
          + (f"; runs {capabilities.describe(caps.get('capabilities'))}" if caps else ""))
    return True


# --------------------------------------------------------------------------
# One kernel's settings, changed on their own (2026-11-09, Darrell)
# --------------------------------------------------------------------------

def cmd_kernel(c: Ctx, name: str, workdir: Optional[str] = None, env_vars: Optional[str] = None,
               environment: Optional[str] = None) -> int:
    """``python -m cubevis.setup kernel NAME [--workdir DIR] [--env VARS]
    [--environment ENV]``: change one kernel without going through its host
    again.  With no option, its working directory and variables are asked
    (the answers default to what it has); with options, only those change.
    Its launcher is written again on the node it was last checked on and run
    once.  On a cluster, when the kernel's old settings were the cluster's
    defaults (for its next kernels), they can follow."""
    ui, st = c.ui, c.st
    kernels = st.section("kernels")
    if name not in kernels:
        ui.fail(f"there is no kernel {name!r}"
                + (f" (kernels: {', '.join(sorted(kernels))})" if kernels else
                   " (python -m cubevis.setup HOST sets one up)"))
        return 1
    k = kernels[name]
    envs_ = st.section("environments")
    new_env = environment or k.get("environment")
    if new_env not in envs_:
        ui.fail(f"there is no environment {new_env!r}"
                + (f" (environments: {', '.join(sorted(envs_))})" if envs_ else ""))
        return 1
    old_net = (envs_.get(k.get("environment")) or {}).get("network")
    if old_net and envs_[new_env].get("network") != old_net:
        ui.fail(f"environment {new_env} is on another network than kernel {name} ({old_net})")
        return 1
    nodes = [x for x in sorted(st.nodes()) if st.environment_on(new_env, x)]
    if k.get("checked_on") in nodes:
        nodes.remove(k["checked_on"])
        nodes.insert(0, k["checked_on"])
    if not nodes:
        ui.fail(f"no node has environment {new_env}: python -m cubevis.setup HOST first")
        return 1
    node = nodes[0]
    n = st.nodes()[node]
    if not local_steps(c):
        return 1
    if not network_steps(c, n["network"], [node]):
        return 1
    ui.heading(f"Kernel {name} on {n.get('cluster') and 'cluster ' + n['cluster'] or node}")
    rep = discover_step(c, node, only=True)
    if rep is None:
        return 1
    home = (rep.get("host") or {}).get("home") or n.get("home") or "/"
    old = {"workdir": k.get("workdir"), "env": dict(k.get("env") or {}),
           "environment": k.get("environment")}
    new = dict(old, env=dict(old["env"]))
    if workdir is None and env_vars is None and environment is None:
        _ask_settings(c, node, new, home)
    else:
        if workdir is not None:
            p = expand_remote(workdir, home)
            new["workdir"] = None if p == home else p
        if env_vars is not None:
            try:
                new["env"] = ({} if env_vars.strip().lower() in ("none", "-", "")
                              else launcher.parse_vars(env_vars))
            except ValueError as exc:
                ui.fail(f"--env: {exc}")
                return 1
        new["environment"] = new_env
    if new == old:
        ui.ok(f"kernel {name}: nothing to change (starts in {rtilde(old['workdir'] or home, home)}"
              + (f", {launcher.format_vars(old['env'])}" if old["env"] else "") + ")")
        return 0
    k.update(workdir=new["workdir"], env=new["env"], environment=new["environment"])
    said = []
    if new["workdir"] != old["workdir"]:
        said.append(f"starts in {rtilde(new['workdir'] or home, home)} "
                    f"(was {rtilde(old['workdir'] or home, home)})")
    if new["env"] != old["env"]:
        said.append(f"variables {launcher.format_vars(new['env']) or 'none'} "
                    f"(were {launcher.format_vars(old['env']) or 'none'})")
    if new["environment"] != old["environment"]:
        said.append(f"environment {new['environment']} (was {old['environment']})")
    ui.record_change("changed kernel", f"{name}: " + "; ".join(said))
    cl_name = n.get("cluster")
    cl = st.section("clusters").get(cl_name) if cl_name else None
    if (cl is not None and (cl.get("workdir"), dict(cl.get("env") or {})) == (old["workdir"], old["env"])
            and (new["workdir"], new["env"]) != (old["workdir"], old["env"])):
        ui.say(f"Cluster {cl_name}'s defaults for its next kernels are kernel {name}'s old "
               "settings.")
        if ui.ask_yn(f"Make its new settings cluster {cl_name}'s defaults?", True,
                     key="cluster_defaults"):
            cl["workdir"], cl["env"] = new["workdir"], dict(new["env"])
            ui.record_change("cluster defaults", f"{cl_name}: starts in "
                             f"{rtilde(new['workdir'] or home, home)}"
                             + (f", {launcher.format_vars(new['env'])}" if new["env"] else ""))
    c.save()
    ok, msg = write_and_test(c, node, name, rep, node)
    (ui.ok if ok else ui.fail)(f"kernel {name}: {msg}")
    c.save()
    if ok and new["environment"] != old["environment"]:
        casa_data_step(c, node, name, rep, node)
        c.save()
    return 0 if ok else 1
