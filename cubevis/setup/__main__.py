"""``python -m cubevis.setup``: set up remote kernels for cubevis.

    python -m cubevis.setup                       walk through what is not done yet
    python -m cubevis.setup status                what is set up
    python -m cubevis.setup check [NETWORK|NODE]  test logins; change nothing
    python -m cubevis.setup network [NAME]        add a network (or go through one again)
    python -m cubevis.setup node NETWORK [NODE..] add nodes to a network
    python -m cubevis.setup node-here [NETWORK]   this computer as a node: its environments

Stage 2 of ``devel/notes/drs/visplot/remote_session_design.md``: this
computer, networks, nodes and logging in.  Environments, kernels, versions
and the node query follow (stage 2b, section 7b).

Everything it keeps is in ``~/.config/cubevis`` (``$XDG_CONFIG_HOME``), which
may be shared by several computers: ``state.json`` (networks, nodes:
shared), and per computer in ``hosts/<id>/``: ``local.json``, the generated
``ssh/config`` and ``ssh/rules``, the wrapper in ``bin/``, ``changes.log``
(every change) and ``setup.log`` (every command).  ssh's connection
sockets are on local disk (``/tmp/cubevis-<uid>``).
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
import shutil
import subprocess
import sys
from typing import Optional

from . import RuntimeDirError, host_id, nodefile, sshgen, state as S, stores
from .node_here import cmd_node_here
from .ui import UI, FuncStep, Quit, run_step, tilde

PRIVATE_SKIP = ("172.17.0.0/16", "192.168.122.0/24", "192.168.121.0/24")  # docker, libvirt


# ==========================================================================
# Shared helpers
# ==========================================================================

class Ctx:
    """What a run carries: the UI, the state, and options."""

    def __init__(self, ui: UI, st: S.State, check_only: bool = False, redo: bool = False):
        self.ui, self.st, self.check_only, self.redo = ui, st, check_only, redo
        self.step_n = 0
        self.last_out = ""
        self.warned: set = set()

    def save(self) -> None:
        """Save the state and regenerate the ssh files from it."""
        if self.check_only:
            S.save(self.st)          # flags only
            return
        S.save(self.st)
        try:
            res = sshgen.write(self.st)
        except RuntimeDirError as exc:
            raise S.StateError(str(exc)) from None
        for what in ("config", "rules"):
            if res[what]:
                p = sshgen.config_path() if what == "config" else sshgen.rules_path()
                self.ui.record_change("wrote", tilde(p))
        for w in res["warnings"]:
            if w not in self.warned:
                self.warned.add(w)
                self.ui.note(w)

    def step(self, title, check, do, look_only=False) -> bool:
        self.step_n += 1
        return run_step(self.ui, FuncStep(title, check, do, look_only),
                        check_only=self.check_only, redo=self.redo, number=self.step_n)

    def ssh(self, alias: str, *remote, timeout: float = 60) -> subprocess.CompletedProcess:
        """The wrapper with the generated config -- exactly what kernels use.
        BatchMode: ssh itself never asks; the wrapper logs in with the stored
        password when the rules list the host."""
        cmd = [str(sshgen.wrapper_path()), "-F", str(sshgen.config_path()),
               "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
               "-o", "ConnectTimeout=30", alias] + (list(remote) or ["true"])
        r = self.ui.run(cmd, show=False, timeout=timeout)
        self.last_out = (r.stdout or "") + (r.stderr or "")
        return r


def split_port(text: str) -> tuple:
    if text.count(":") == 1:
        h, p = text.split(":")
        if p.isdigit():
            return h, int(p)
    return text, 22


def parent_domain(host: str) -> Optional[str]:
    parts = host.split(".")
    return ".".join(parts[1:]) if len(parts) > 2 else None


def last_line(out: str) -> str:
    lines = [l for l in out.strip().split("\n") if l and not l.startswith("[ssh-wrapper]")]
    if lines:
        return lines[-1]
    lines = [l for l in out.strip().split("\n") if l]
    return lines[-1] if lines else ""


# ==========================================================================
# This computer
# ==========================================================================

def openssh_version() -> Optional[tuple]:
    if not shutil.which("ssh"):
        return None
    r = subprocess.run(["ssh", "-V"], capture_output=True, text=True)
    m = re.search(r"OpenSSH_(\d+)\.(\d+)", r.stdout + r.stderr)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def local_steps(c: Ctx) -> bool:
    ui, st = c.ui, c.st

    def tools_check():
        v = openssh_version()
        missing = []
        if v is None:
            missing.append("ssh")
        # 8.0 is RHEL 8's.  8.4 added SSH_ASKPASS_REQUIRE=force; before it ssh
        # still uses askpass when there is no terminal and DISPLAY is set,
        # which the wrapper arranges (tested with 8.0p1, 2026-10-19).  The
        # generated config needs 7.6 (StrictHostKeyChecking accept-new).
        elif v < (8, 0):
            missing.append(f"OpenSSH 8.0 or later (this is {v[0]}.{v[1]})")
        if not shutil.which("perl"):
            missing.append("perl (the wrapper is a perl script)")
        if missing:
            return False, "missing: " + "; ".join(missing)
        return True, f"OpenSSH {v[0]}.{v[1]}, perl"

    def tools_do():
        ui.say("These come with macOS and every Linux distribution; on Linux install them "
               "with the package manager (openssh-client, perl). Then run this again.")
        return False

    if not c.step("Tools: ssh and perl", tools_check, tools_do, look_only=True):
        return False

    def wrapper_check():
        w = sshgen.wrapper_path()
        if w.exists() and w.read_bytes() == sshgen.data_file("ssh").read_bytes():
            return True, tilde(w)
        return False, "not installed, or older than the one in this cubevis"

    def wrapper_do():
        ui.say("The ssh wrapper logs in once per host with a stored password, for hosts "
               "that ask for one even with a key, so that kernels (which have no terminal) "
               "can connect. It is not put on your PATH: your own ssh, scp and git are "
               "unchanged; only the kernels and setup use it.")
        ui.changes_list(f"copies the wrapper to {tilde(sshgen.wrapper_path())} and links "
                        "scp to it")
        if not ui.ask_yn("Go ahead?", True):
            return False
        sshgen.install_wrapper()
        ui.record_change("installed", tilde(sshgen.wrapper_path()))
        return True

    if not c.step("The ssh wrapper", wrapper_check, wrapper_do):
        return False

    old = sshgen.old_layout_files()
    if old and not c.check_only:
        ui.note("not used any more (an earlier cubevis setup, or the bash one, wrote them; "
                "remove them once no computer sharing this directory runs those): "
                + ", ".join(tilde(p) for p in old))

    # The Python that ssh's "inside" test runs (Match exec): this one.
    if not c.check_only and st.local.get("python") != sys.executable:
        st.local["python"] = sys.executable
        c.save()
    return True


# ==========================================================================
# Networks and nodes
# ==========================================================================

def ask_network(c: Ctx, name: Optional[str]) -> Optional[str]:
    """Questions for a new network; returns its name."""
    ui, st = c.ui, c.st
    ui.heading("A network")
    ui.say("A network is a group of computers (nodes) where your data and kernels can "
           "be: a cluster, or the machines of one site. Often they are reached through a "
           "login host (a bastion), as in 'ssh -J login node'.")
    via = ui.ask_yn("Do you reach these nodes through a login host?", True, key="via_login")
    login, lport, luser = None, 22, None
    me = os.environ.get("USER") or ""
    if via:
        text = ui.ask("Login host, its real name (host[:port])", "", key="login_host")
        if not text:
            return None
        login, lport = split_port(text)
        luser = ui.ask(f"Your user name on {login}", st.local.get("last_user") or me,
                       key="login_user")
        st.local["last_user"] = luser
    user = ui.ask("Your user name on the nodes", luser or st.local.get("last_user") or me,
                  key="user")
    dom_default = parent_domain(login) if login else ""
    ui.say("The nodes' domain completes short names (node140 -> node140.<domain>) and "
           "gives the pattern the wrapper's rules use for all of them.")
    domain = ui.ask("Domain of the nodes (empty: none)", dom_default or "", key="domain") or None
    default_name = (domain or login or "net").split(".")[0]
    while True:
        name = name or ui.ask("A short name for this network", default_name, key="network")
        try:
            S.check_name("network", name)
        except S.StateError as exc:
            ui.note(str(exc))
            name = None
            continue
        if name in st.networks():
            ui.note(f"there is a network {name!r} already")
            name = None
            continue
        break
    try:
        st.add_network(name, login_host=login, login_port=lport, login_user=luser, user=user,
                       domain=domain)
    except S.StateError as exc:
        ui.fail(str(exc))
        return None
    ui.record_change("added network", f"{name} ({'via ' + login if login else 'direct'})")
    c.save()
    return name


def ask_nodes(c: Ctx, network: str, names=()) -> list:
    """Add nodes (typed); returns their names."""
    ui, st = c.ui, c.st
    added = []
    if not names:
        ui.say("Nodes: give one or more names, separated by spaces or commas (short names "
               "are completed with the network's domain).")
        text = ui.ask(f"Nodes of {network}", "", key="nodes")
        names = [n for n in re.split(r"[,\s]+", text) if n]
    for n in names:
        host, port = split_port(n)
        short = host.split(".")[0] if "." in host else host
        try:
            st.add_node(short, network=network,
                        hostname=st.node_hostname(network, host), port=port)
        except S.StateError as exc:
            ui.note(str(exc))
            continue
        node = st.nodes()[short]
        ui.record_change("added node", f"{short} ({node['hostname']}, ssh name {node['alias']})")
        added.append(short)
    if added:
        c.save()
    return added


# -- logging in, one hop at a time ------------------------------------------

def hop_info(st: S.State, network: str, node: Optional[str]) -> dict:
    """alias / host / user / password flag / service record for a hop: the
    login host (node None) or a node."""
    net = st.networks()[network]
    if node is None:
        lg = net["login"]
        return {"alias": lg["alias"], "host": lg["host"], "user": lg["user"],
                "rec": lg, "label": f"login host {lg['alias']}"}
    n = st.nodes()[node]
    return {"alias": n["alias"], "host": n["hostname"], "user": net["user"],
            "rec": net, "label": f"node {node}"}


def hop_steps(c: Ctx, network: str, node: Optional[str]) -> bool:
    ui, st = c.ui, c.st
    h = hop_info(st, network, node)

    def check():
        h.update(hop_info(st, network, node))
        rec = h["rec"]
        if rec.get("password") and sys.stdin.isatty():
            stores.unlock(ui, st, rec.get("service") or "cubevis-ssh", h["user"])
        r = c.ssh(h["alias"])
        if r.returncode == 0:
            if node:
                st.mark_ok("nodes", node)
                S.save(st)
            if rec.get("password"):
                return True, "password from " + stores.where(st, rec.get("service"), h["user"])
            return True, "with an ssh key"
        why = last_line(c.last_out) or f"ssh exit {r.returncode}"
        if node:
            why = diagnose_behind_login(c, network, node, why)
            st.flag("nodes", node, why)
            S.save(st)
        return False, why

    def do():
        out = c.last_out
        rec = h["rec"]
        why = last_line(out) or "it refused the login"
        if re.search(r"bad configuration option|terminating, \d+ bad", out):
            ui.say(f"ssh cannot read the generated configuration ({why}). This is a setup "
                   f"bug; {tilde(sshgen.config_path())} and {tilde(ui.log_path)} say more.")
            return False
        if re.search(r"Could not resolve|Name or service not known|nodename nor servname", out):
            ui.say(f"ssh cannot find '{h['host']}' ({why}). Check the name; a host behind a "
                   "login host is often known only there.")
            return False
        if re.search(r"timed out|Connection refused|No route|Operation timed out", out):
            ui.say(f"No connection to {h['host']}: {why}. A VPN, a firewall, or a login host "
                   "may be needed.")
            return False
        if re.search(r"HOST IDENTIFICATION HAS CHANGED|Host key verification failed", out):
            ui.say(f"The host key of {h['host']} does not match the one saved before "
                   "(~/.ssh/known_hosts). Ask whoever runs the host whether it changed; "
                   "setup does not override that.")
            return False
        if rec.get("password") and st.local.get("store"):
            ui.say(f"Logging in with the stored password failed: {why}")
            ui.say("The wrapper tries a stored password once per login, so a wrong one does "
                   "not lock your account at once; but sites count the failures. Store it "
                   "again only when you are sure of it.")
            if not ui.ask_yn("Type the password again?", True):
                return False
            return stores.put(ui, st, rec["service"], h["user"], h["host"])
        if rec.get("password"):
            ui.say(f"{h['host']} needs your password, and this computer has no password "
                   "store chosen yet (the network was set up from another computer sharing "
                   "this directory).")
        else:
            ui.say(f"{h['host']} does not accept a key from here ({why}), so it needs your "
                   "password.")
        if not ui.ask_yn(f"Store your password for {h['user']}@{h['host']}, for the wrapper "
                         "to use?", True, key="store_password"):
            return False
        if not stores.choose(ui, st):
            return False
        svc, need_put = None, True
        if rec.get("password") and rec.get("service"):
            # Set up elsewhere: the entry's name is known; it may already be in
            # this computer's store too (a store shared with the other one).
            svc = rec["service"]
            need_put = not stores.has_entry(st, svc, h["user"])
        # The same user's password stored for another hop is often the same.
        for other in ([] if svc else _password_entries(st)):
            if other[1] == h["user"]:
                if ui.ask_yn(f"Is it the same password as for {other[2]}?", True,
                             key="same_password"):
                    svc, need_put = other[0], False
                break
        if svc is None:
            svc = ui.ask("Name for the stored entry (hosts with the same password can "
                         "share one)", "cubevis-ssh", key="service")
            if st.local.get("store") != "command" and stores.has_entry(st, svc, h["user"]):
                if ui.ask_yn(f"There is a stored password '{svc}' for {h['user']} already. "
                             "Use it?", True):
                    need_put = False
        if need_put and not stores.put(ui, st, svc, h["user"], h["host"]):
            return False
        rec["password"] = True
        rec["service"] = svc
        c.save()
        ui.say("Trying the login (one attempt) ...")
        return True

    return c.step(f"Log in to the {h['label']} ({h['user']}@{h['host']}) without typing",
                  check, do)


def diagnose_behind_login(c: Ctx, network: str, node: str, why: str) -> str:
    """A node behind a login host that fails with ssh's bare "Connection
    closed by UNKNOWN": the login host's own reason (most often, it cannot
    resolve the node's name) went to the shared connection, not to us.  Ask
    the login host directly."""
    st = c.st
    net = st.networks()[network]
    lg = net.get("login")
    if not lg or "Connection closed" not in why:
        return why
    host = st.nodes()[node]["hostname"]
    out = c.last_out
    r = c.ssh(lg["alias"], "sh", "-c",
              f"'getent hosts {host} || host {host} || nslookup {host}' >/dev/null 2>&1; echo rc=$?")
    c.last_out = out
    if r.returncode == 0 and "rc=0" not in r.stdout:
        return f"the login host {lg['host']} cannot resolve {host}: check the node's name"
    if r.returncode == 0:
        return (f"{why} -- the login host resolves {host}, so the node refused or did not "
                "answer (is it up? is ssh on the given port?)")
    return why


def _password_entries(st: S.State) -> list:
    """(service, user, label) of the stored passwords setup knows of."""
    out = []
    for name, net in st.networks().items():
        lg = net.get("login") or {}
        if lg.get("password"):
            out.append((lg.get("service"), lg.get("user"), f"login host {lg.get('alias')}"))
        if net.get("password"):
            out.append((net.get("service"), net.get("user"), f"the nodes of {name}"))
    return out


def private(addr: str) -> bool:
    try:
        a = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return a.is_private and not a.is_loopback and not any(
        a in ipaddress.ip_network(r) for r in PRIVATE_SKIP)


def suggest_ranges(addrs) -> list:
    """/16 of each private address (IPv4), in order, without repeats."""
    out = []
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.version == 4 and private(a):
            r = str(ipaddress.ip_network(f"{a}/16", strict=False))
            if r not in out:
                out.append(r)
    return out


def inside_step(c: Ctx, network: str, node: str) -> bool:
    """Ask once for the site's address ranges, suggested from the
    addresses a node reports for this very connection ($SSH_CONNECTION:
    the address it was reached from -- the login host's inside one, or
    this computer's -- and its own)."""
    ui, st = c.ui, c.st
    net = st.networks()[network]

    def check():
        if not net.get("login"):
            return True, "no login host: always direct"
        r = net.get("inside")
        if r is None:
            return False, "not decided yet"
        return True, (f"direct from inside {', '.join(r)}" if r
                      else "always through the login host")

    def do():
        alias = st.nodes()[node]["alias"]
        r = c.ssh(alias, "echo", "$SSH_CONNECTION")
        seen = r.stdout.split() if r.returncode == 0 else []
        addrs = [seen[0], seen[2]] if len(seen) >= 4 else []
        sug = suggest_ranges(addrs)
        ui.say("From inside the site (an office, a VPN that puts you on the site's "
               "network), ssh can go to the nodes directly instead of through the login "
               "host. Setup decides at each connection: this computer is inside when it "
               "would reach a node from an address in the site's ranges.")
        if addrs:
            ui.say(f"{node} reports this connection as from {addrs[0]} to {addrs[1]}.")
        ui.say("Give the site's address ranges (CIDR, separated by spaces), or 'none' to "
               "always use the login host. Include every range you work from inside, e.g. "
               "the desktops' as well as the cluster's.")
        while True:
            text = ui.ask("Inside ranges", " ".join(sug) or "none", key="inside")
            if text.strip().lower() in ("none", "-", ""):
                ranges = []
                break
            try:
                ranges = S.normalize_ranges(re.split(r"[,\s]+", text.strip()))
                break
            except S.StateError as exc:
                ui.note(str(exc))
        net["inside"] = ranges
        ui.record_change("set inside ranges", f"{network}: {', '.join(ranges) or 'none'}")
        c.save()
        return True

    return c.step(f"Direct connections from inside {network}", check, do)


def network_steps(c: Ctx, network: str, nodes=None) -> bool:
    """Log in to each hop of *network* (its login host, then each node), and
    settle the inside ranges."""
    st = c.st
    net = st.networks()[network]
    c.ui.heading(f"Network {network}" + (f" (login host {net['login']['host']})"
                                          if net.get("login") else " (direct)"))
    if not c.check_only:
        c.save()
    ok = True
    if net.get("login"):
        ok = hop_steps(c, network, None)
        if not ok and not c.check_only:
            return False
    names = sorted(st.nodes(network)) if nodes is None else list(nodes)
    if not names:
        c.ui.note(f"{network} has no nodes yet: python -m cubevis.setup node {network} NAME")
    first_ok = None
    for n in names:
        if hop_steps(c, network, n):
            first_ok = first_ok or n
        else:
            ok = False
    if first_ok and not c.check_only:
        inside_step(c, network, first_ok)
    elif net.get("login") and c.check_only:
        r = net.get("inside")
        c.ui.ok(f"Direct connections from inside {network} -- "
                + ("not decided yet" if r is None else
                   (f"inside {', '.join(r)}" if r else "always through the login host")))
    return ok


# ==========================================================================
# Commands
# ==========================================================================

def cmd_status(c: Ctx) -> int:
    ui, st = c.ui, c.st
    ui.heading(f"cubevis setup in {tilde(S.state_path().parent)}")
    loc = st.local
    ui._p(f"    {'computer':<10} {host_id()} ({tilde(S.host_state_path().parent)})")
    for k in ("python", "store"):
        ui._p(f"    {k:<10} {loc.get(k) or '-'}")
    from . import inside as _in
    if not st.networks():
        ui._p("    no networks yet")
    for name, net in sorted(st.networks().items()):
        lg = net.get("login")
        ui._p("")
        ui._p(f"    network {name}: " + (f"through {lg['user']}@{lg['host']}"
                                          f"{':' + str(lg['port']) if lg['port'] != 22 else ''}"
                                          f" ({lg['alias']})" if lg else "direct"))
        r = net.get("inside")
        if lg:
            if r:
                now = _in.decide(_in.network_hosts(net, st.nodes(name)), r)[0]
                ui._p(f"      inside {', '.join(r)} -- this computer is "
                      f"{'inside: direct' if now else 'outside: through the login host'}")
            else:
                ui._p("      inside ranges: " + ("not decided" if r is None else "none"))
        for n, node in sorted(st.nodes(name).items()):
            flag = node.get("status") or "-"
            if flag == "flagged":
                flag += f" ({node.get('last_error')})"
            mark = " (default)" if net.get("default_node") == n else ""
            ui._p(f"      node {n}{mark}: {node['hostname']} as {net['user']}, "
                  f"ssh {node['alias']}, {flag}"
                  + (f", checked {node['last_checked']}" if node.get("last_checked") else ""))
    for k in ("environments", "kernels", "sessions"):
        if st.section(k):
            ui._p(f"    {k}: {', '.join(sorted(st.section(k)))}")
    for net in nodefile.mine():
        try:
            nf = nodefile.load(net)
        except S.StateError as exc:
            ui._p(f"    this computer as a node of {net}: {exc}")
            continue
        ui._p("")
        ui._p(f"    this computer as a node of {net} ({tilde(nf.path)}):")
        if not nf.environments:
            ui._p("      no environments enabled")
        for name, e in sorted(nf.environments.items()):
            v = (e.get("versions") or {}).get("cubevis")
            how = (f"ok, cubevis {v}" if e.get("status") == "ok"
                   else f"{e.get('status')}: {e.get('last_error')}")
            ui._p(f"      environment {name}: {e['kind']} {tilde(e['python'])} -- {how}")
    ui._p("")
    ui._p(f"    ssh config  {tilde(sshgen.config_path())}")
    ui._p(f"    wrapper     {tilde(sshgen.wrapper_path())}")
    ui._p(f"    changes     {tilde(ui.changes_path)}")
    ui._p(f"    sockets     {sshgen.cm_dir()}")
    old = sshgen.old_layout_files()
    if old:
        ui._p("    not used any more (earlier layout): " + ", ".join(tilde(p) for p in old))
    return 0


def cmd_check(c: Ctx, target: Optional[str]) -> int:
    ui, st = c.ui, c.st
    ui.heading("cubevis setup: checking (nothing is changed but the nodes' status)")
    local_steps(c)
    if target and target in st.nodes():
        network_steps(c, st.nodes()[target]["network"], [target])
    elif target:
        st.get("networks", target)
        network_steps(c, target)
    else:
        for name in sorted(st.networks()):
            network_steps(c, name)
    ui._p()
    if ui.failed:
        ui.fail(f"{ui.failed} check(s) failed")
        return 1
    ui.ok("all checks passed")
    return 0


def cmd_network(c: Ctx, name: Optional[str]) -> int:
    if not local_steps(c):
        return 1
    if not name or name not in c.st.networks():
        name = ask_network(c, name)
        if not name:
            return 1
        ask_nodes(c, name)
    return 0 if network_steps(c, name) else 1


def cmd_node(c: Ctx, network: str, names) -> int:
    c.st.get("networks", network)
    if not local_steps(c):
        return 1
    added = ask_nodes(c, network, names)
    if not added:
        return 1
    return 0 if network_steps(c, network, added) else 1


def cmd_walk(c: Ctx) -> int:
    ui, st = c.ui, c.st
    ui.heading("cubevis setup")
    ui.say("This sets up the networks where visplot's kernels can run next to your data: "
           "how to log in to their nodes (through a login host when there is one), with a "
           f"stored password where a host asks for one. It asks before each change; "
           f"everything it keeps is in {tilde(S.state_path().parent)}, and every change is "
           f"listed in {tilde(ui.changes_path)}. Enter accepts the answer in [brackets].")
    in_ssh = bool(os.environ.get("SSH_CONNECTION"))
    ui.say("A computer takes part in two ways: where the plots appear (it starts kernels), "
           "or as a node where kernels run (it offers Python environments for them). One "
           "computer can be both: run this once for each.")
    role = ui.ask_choice("Set this computer up as [p]lots-here or [n]ode?", "n" if in_ssh else "p",
                         ("p", "n"), key="role")
    if role == "n":
        return cmd_node_here(c)
    if not local_steps(c):
        return 1
    while True:
        nets = sorted(st.networks())
        if not nets:
            ui.heading("Networks")
            ui.say("None yet.")
            if not ui.ask_yn("Add one now?", True, key="add_network"):
                return 0
            choice = "a"
        else:
            ui.heading("Networks")
            for n in nets:
                net = st.networks()[n]
                bad = [k for k, v in st.nodes(n).items() if v.get("status") != "ok"]
                ui._p(f"    {n}: {len(st.nodes(n))} node(s)"
                      + (f", not checked or failing: {', '.join(sorted(bad))}" if bad else ""))
            choice = ui.ask_choice("[a]dd a network, add a [n]ode, [c]heck them all, or "
                                   "[d]one?", "d", ("a", "n", "c", "d"), key="menu")
        if choice == "d":
            return 0
        if choice == "a":
            name = ask_network(c, None)
            if name:
                ask_nodes(c, name)
                network_steps(c, name)
        elif choice == "n":
            net = nets[0] if len(nets) == 1 else ui.ask_choice("Which network?", nets[0], nets)
            added = ask_nodes(c, net)
            if added:
                network_steps(c, net, added)
        elif choice == "c":
            for n in nets:
                network_steps(c, n)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m cubevis.setup",
                                description="Set up remote kernels for cubevis (networks, "
                                            "nodes, logins).")
    p.add_argument("--yes", action="store_true", help="take every default (for tests)")
    p.add_argument("--redo", action="store_true", help="ask again even for steps that pass")
    p.add_argument("--answer", action="append", default=[], metavar="KEY=VALUE",
                   help="pre-fill one question (for scripts and tests)")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status", help="what is set up")
    pc = sub.add_parser("check", help="test logins; change nothing")
    pc.add_argument("target", nargs="?", help="a network or node (default: all)")
    pn = sub.add_parser("network", help="add a network, or go through one again")
    pn.add_argument("name", nargs="?")
    pd = sub.add_parser("node", help="add nodes to a network")
    pd.add_argument("network")
    pd.add_argument("names", nargs="*")
    ph = sub.add_parser("node-here", help="this computer as a node: where kernels run, and "
                                          "its Python environments for them")
    ph.add_argument("network", nargs="?")
    a = p.parse_args(argv)
    answers = dict(kv.split("=", 1) for kv in a.answer if "=" in kv)
    ui = UI(yes=a.yes, answers=answers)
    try:
        st = S.load()
    except S.StateError as exc:
        ui.fail(str(exc))
        return 1
    c = Ctx(ui, st, check_only=(a.cmd == "check"), redo=a.redo)
    ui.log(f"---- cubevis.setup {' '.join(sys.argv[1:] if argv is None else argv)}")
    # gpg (pass, gpg-file) asks for its passphrase on the terminal named here.
    if sys.stdin.isatty() and "GPG_TTY" not in os.environ:
        os.environ["GPG_TTY"] = os.ttyname(0)
    try:
        if a.cmd == "status":
            return cmd_status(c)
        if a.cmd == "check":
            return cmd_check(c, a.target)
        if a.cmd == "network":
            rc = cmd_network(c, a.name)
        elif a.cmd == "node":
            rc = cmd_node(c, a.network, a.names)
        elif a.cmd == "node-here":
            rc = cmd_node_here(c, a.network)
        else:
            rc = cmd_walk(c)
        ui.show_changes()
        return rc
    except Quit:
        ui._p()
        ui.show_changes()
        return 1
    except S.StateError as exc:
        ui.fail(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
