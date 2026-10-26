"""The steps ``python -m cubevis.setup`` is made of, shared by its
commands: what a run carries (``Ctx``), this computer's tools and the
wrapper, logging in to each hop of a network, and the inside ranges.
"""
from __future__ import annotations

import ipaddress
import re
import shutil
import subprocess
import sys
from typing import Optional

from . import RuntimeDirError, sshgen, state as S, stores
from .ui import UI, FuncStep, run_step, tilde

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

    def ssh(self, alias: str, *remote, timeout: float = 60, opts=(),
            input: Optional[str] = None) -> subprocess.CompletedProcess:
        """The wrapper with the generated config -- exactly what kernels use.
        BatchMode: ssh itself never asks; the wrapper logs in with the stored
        password when the rules list the host."""
        cmd = [str(sshgen.wrapper_path()), "-F", str(sshgen.config_path()),
               "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
               "-o", "ConnectTimeout=30", *opts, alias] + (list(remote) or ["true"])
        r = self.ui.run(cmd, show=False, timeout=timeout, input=input)
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


def node_ranges(st: S.State, network: str) -> list:
    """The private IPv4 subnets the network's nodes reported (discovery),
    in node order, without repeats: candidate inside ranges."""
    out = []
    for n in sorted(st.nodes(network)):
        for r in st.nodes()[n].get("ranges") or []:
            try:
                net = ipaddress.ip_network(r, strict=False)
            except ValueError:
                continue
            if net.version == 4 and private(str(net.network_address)) and r not in out:
                out.append(r)
    return out


def inside_step(c: Ctx, network: str, node: str) -> bool:
    """Ask once for the site's address ranges, suggested from the nodes'
    own subnets (discovery) and the addresses a node reports for this very
    connection ($SSH_CONNECTION: the address it was reached from -- the
    login host's inside one, or this computer's -- and its own)."""
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
        # The nodes' own subnets first, then the /16s of this connection's
        # addresses.
        own = node_ranges(st, network)
        sug = list(own)
        sug += [r for r in suggest_ranges(addrs) if r not in sug]
        ui.say("From inside the site (an office, a VPN that puts you on the site's "
               "network), ssh can go to the nodes directly instead of through the login "
               "host. Setup decides at each connection: this computer is inside when it "
               "would reach a node from an address in the site's ranges.")
        if addrs:
            ui.say(f"{node} reports this connection as from {addrs[0]} to {addrs[1]}.")
        if own:
            ui.say("The nodes' own subnets: " + ", ".join(own) + ".")
        ui.say("Only from these ranges does this computer go to the nodes directly; from "
               "anywhere else (a visitor network, say) it uses the login host. On site, "
               f"python -m cubevis.setup network {network} --inside-here adds where you are, "
               "after checking that a direct connection works from there.")
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


def network_steps(c: Ctx, network: str, nodes=None, inside: bool = True) -> bool:
    """Log in to each hop of *network* (its login host, then each node), and
    settle the inside ranges (unless *inside* is false: ``setup HOST`` asks
    after it has the node's subnets)."""
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
        c.ui.note(f"{network} has no nodes yet: python -m cubevis.setup HOST adds one")
    first_ok, ok_nodes = None, []
    for n in names:
        if hop_steps(c, network, n):
            first_ok = first_ok or n
            ok_nodes.append(n)
        else:
            ok = False
    if first_ok and not c.check_only and inside:
        inside_step(c, network, first_ok)
    elif net.get("login") and c.check_only:
        r = net.get("inside")
        c.ui.ok(f"Direct connections from inside {network} -- "
                + ("not decided yet" if r is None else
                   (f"inside {', '.join(r)}" if r else "always through the login host")))
    return ok


