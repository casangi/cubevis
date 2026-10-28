"""``python -m cubevis.setup``: set up remote kernels for cubevis.

    python -m cubevis.setup HOST                  make kernels work on HOST (the computer
                                                  your data is on); again: check them
    python -m cubevis.setup HOST --kernel NAME    add (or change) a kernel on HOST
    python -m cubevis.setup                       the same, asking for the host
    python -m cubevis.setup status                what is set up
    python -m cubevis.setup check [NAME]          test logins and kernels; change nothing
    python -m cubevis.setup network NAME          go through a network's logins again
    python -m cubevis.setup network NAME --inside-here
                                                  go directly from where this computer is
    python -m cubevis.setup jupyter KERNEL [NODE] a Jupyter kernel for it, for
                                                  VisibilityPlotter(kernel_name=...) until
                                                  sessions (stage 3) start kernels themselves:
                                                  cubevis-KERNEL (its usual node), or
                                                  cubevis-KERNEL-NODE (on NODE)

Remote session design, section 7c: the user names a host; networks (the
route to it), clusters and launchers are setup's own bookkeeping
(``hostsetup``).

Everything it keeps is in ``~/.config/cubevis`` (``$XDG_CONFIG_HOME``), which
may be shared by several computers: ``state.json`` (networks, clusters,
nodes, environments, kernels: shared), and per computer in ``hosts/<id>/``:
``local.json``, the generated ``ssh/config`` and ``ssh/rules``, the wrapper
in ``bin/``, ``changes.log`` (every change) and ``setup.log`` (every
command).  ssh's connection sockets are on local disk
(``/tmp/cubevis-<uid>``).  On the nodes it writes only the kernels'
launchers (``~/.config/cubevis/launch/<id>/``).
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

from . import host_id, sshgen, state as S
from . import jupyterspec as J
from .hostsetup import check_kernel, cmd_host, rtilde
from .steps import Ctx, last_line, local_steps, network_steps
from .ui import UI, Quit, tilde

COMMANDS = ("status", "check", "network", "jupyter")


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
        ui._p("    nothing set up yet: python -m cubevis.setup HOST")
    envs_ = st.section("environments")
    for name, net in sorted(st.networks().items()):
        lg = net.get("login")
        ui._p("")
        ui._p(f"    network {name}" + (f" ({net['domain']})" if net.get("domain") else "") + ": "
              + (f"through {lg['user']}@{lg['host']}"
                 f"{':' + str(lg['port']) if lg['port'] != 22 else ''} ({lg['alias']})"
                 if lg else "direct"))
        r = net.get("inside")
        forced, why = _in.override(name)
        if lg and forced is not None:
            ui._p(f"      {why} is set here: {'direct' if forced else 'through the login host'}, "
                  "whatever the ranges")
        elif lg and why:
            ui._p(f"      {why}")
        if lg:
            if r and forced is not None:
                ui._p(f"      inside {', '.join(r)}")
            elif r:
                now = _in.decide(_in.network_hosts(net, st.nodes(name)), r)[0]
                ui._p(f"      inside {', '.join(r)} -- this computer is "
                      f"{'inside: direct' if now else 'outside: through the login host'}")
            else:
                ui._p("      inside ranges: " + ("not decided" if r is None else "none"))
        for cl, cr in sorted(st.section("clusters").items()):
            if cr.get("network") == name:
                members = sorted(k for k, v in st.nodes(name).items() if v.get("cluster") == cl)
                ui._p(f"      cluster {cl}: nodes {cr['pattern']}"
                      + (f" (set up: {', '.join(members)})" if members else ""))
                if "workdir" in cr or "env" in cr:
                    home = next((v.get("home") for v in st.nodes(name).values() if v.get("home")), None)
                    ui._p(f"        new kernels start from: directory "
                          f"{rtilde(cr.get('workdir'), home) if cr.get('workdir') else '~'}"
                          + (", sets " + ", ".join(sorted(cr["env"])) if cr.get("env") else ""))
        for n, node in sorted(st.nodes(name).items()):
            flag = node.get("status") or "-"
            if flag == "flagged":
                flag += f" ({node.get('last_error')})"
            ui._p(f"      node {n}: {node['hostname']}"
                  + (f":{node['port']}" if node.get("port") else "")
                  + f" as {net['user']}, ssh {node['alias']}"
                  + (f", cluster {node['cluster']}" if node.get("cluster") else "")
                  + f", {flag}"
                  + (f", checked {node['last_checked']}" if node.get("last_checked") else ""))
        for en, e in sorted(envs_.items()):
            if e.get("network") != name:
                continue
            where = (f"cluster {e['cluster']}" if e.get("cluster") else
                     "every node" if e.get("hosts") == "all" else ", ".join(e.get("hosts") or []))
            state_ = "ok" if e.get("status") == "ok" else f"{e.get('status')} ({e.get('last_error')})"
            ui._p(f"      environment {en}: {e.get('kind')} {e.get('python')} on {where}, "
                  + (f"cubevis {e['cubevis']}, " if e.get("cubevis") else "") + state_)
        for kn, k in sorted(st.section("kernels").items()):
            e = envs_.get(k.get("environment")) or {}
            if e.get("network") != name:
                continue
            home = None
            for nn in st.nodes(name).values():
                home = home or nn.get("home")
            state_ = "ok" if k.get("status") == "ok" else f"{k.get('status')} ({k.get('last_error')})"
            ui._p(f"      kernel {kn}: environment {k.get('environment')}, starts in "
                  f"{rtilde(k.get('workdir'), home) if k.get('workdir') else '~'}"
                  + (", sets " + ", ".join(sorted(k["env"])) if k.get("env") else "")
                  + f", {state_}"
                  + (f" (on {k['checked_on']}, {k['last_checked']})" if k.get("checked_on") else ""))
            specs = J.specs_for(st, kn)
            if specs:
                for jn, jnode in specs:
                    shown = kn if jn == J.jupyter_name(kn) else jn
                    ui._p(f"        visplot(..., backend=\"remote\", kernel_name=\"{shown}\")"
                          f"   runs it on {jnode}")
            else:
                ui._p(f"        no Jupyter kernel for it here: python -m cubevis.setup "
                      f"jupyter {kn}")
    if st.section("sessions"):
        ui._p(f"    sessions: {', '.join(sorted(st.section('sessions')))}")
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
    ui.heading("cubevis setup: checking (nothing is changed but the status of what is checked)")
    local_steps(c)
    kernels = sorted(st.section("kernels"))
    if target and target in st.section("kernels"):
        kernels = [target]
        net = st.kernel_network(target)
        network_steps(c, net)
    elif target and (st.find_node(target) or target in st.section("clusters")):
        nodes = ([st.find_node(target)] if st.find_node(target) else
                 sorted(k for k, v in st.nodes().items() if v.get("cluster") == target))
        net = st.nodes()[nodes[0]]["network"] if nodes else st.section("clusters")[target]["network"]
        network_steps(c, net, nodes)
        kernels = [k for k in kernels if any(k in st.kernels_on(n) for n in nodes)]
    elif target:
        st.get("networks", target)
        network_steps(c, target)
        kernels = [k for k in kernels if st.kernel_network(k) == target]
    else:
        for name in sorted(st.networks()):
            network_steps(c, name)
    for k in kernels:
        if not check_kernel(c, k):
            ui.failed += 1
    S.save(st)
    ui._p()
    if ui.failed:
        ui.fail(f"{ui.failed} check(s) failed")
        return 1
    ui.ok("all checks passed")
    return 0


def cmd_network(c: Ctx, name: str) -> int:
    c.st.get("networks", name)
    if not local_steps(c):
        return 1
    return 0 if network_steps(c, name) else 1


def cmd_inside_here(c: Ctx, name: str) -> int:
    """``network NAME --inside-here``: add where this computer is now to the
    network's inside ranges -- only after a direct connection to a node,
    without the login host, has worked from here (at setup time; nothing
    is probed when kernels start)."""
    from . import discover as _disc, inside as _in
    ui, st = c.ui, c.st
    net = st.get("networks", name)
    if not local_steps(c):
        return 1
    ui.heading(f"Inside {name}, from here")
    if not net.get("login"):
        ui.ok(f"{name} has no login host: its nodes are always reached directly")
        return 0
    nodes = _in.network_hosts(net, st.nodes(name))
    names = {st.nodes()[n]["hostname"]: n for n in st.nodes(name)}
    if not nodes:
        ui.fail(f"{name} has no nodes yet")
        return 1
    host = nodes[0]
    addrs = _in.resolve(host)
    if not addrs:
        ui.fail(f"{host} does not resolve here: from here, {name} needs its login host. "
                "Nothing recorded.")
        return 1
    src = _in.route_source(addrs[0])
    ranges = net.get("inside") or []
    if src and _in.in_ranges(src, ranges):
        ui.ok(f"this computer ({src}) is inside {', '.join(ranges)} already")
        return 0
    node = names[host]
    ui.say(f"Trying {node} ({host}) directly, without the login host ...")
    r = c.ssh(st.nodes()[node]["alias"], opts=("-o", "ProxyJump=none"))
    if r.returncode != 0:
        ui.fail(f"no direct connection from here ({last_line(c.last_out) or 'ssh exit ' + str(r.returncode)}): "
                f"from here, {name} needs its login host. Nothing recorded.")
        return 1
    ui.ok(f"{node} answers directly from here ({src})")
    import ipaddress
    default = f"{src}/32" if src and ":" not in src else f"{src}/128"
    for iface in _disc.interfaces():
        if ipaddress.ip_address(src) in ipaddress.ip_interface(iface).network:
            default = str(ipaddress.ip_interface(iface).network)
            break
    ui.say(f"This computer reaches {name} from {src}. Recording a range makes every "
           "connection from it go directly; a narrower one is safer when the same network "
           "also has guests (they would need the login host).")
    while True:
        text = ui.ask("Range to add", default, key="inside_here")
        try:
            new = S.normalize_ranges([text])
            break
        except S.StateError as exc:
            ui.note(str(exc))
            if ui.yes:
                return 1
    net["inside"] = ranges + [r_ for r_ in new if r_ not in ranges]
    ui.record_change("added inside range", f"{name}: {', '.join(new)}")
    c.save()
    return 0


def cmd_jupyter(c: Ctx, kernel: str, node: Optional[str]) -> int:
    """``setup jupyter KERNEL [NODE]``: the Jupyter kernel for a setup kernel
    (``jupyterspec``): ``cubevis-KERNEL`` on its usual node -- ``setup HOST``
    makes that one too -- or ``cubevis-KERNEL-NODE`` on another node of its
    cluster, alongside."""
    ui, st = c.ui, c.st
    k = st.get("kernels", kernel)
    # What cannot work is said before anything is installed or asked.
    if node and not (st.find_node(node) or st.cluster_for(node)):
        ui.fail(f"{node} is not a node setup knows, nor one of a cluster's: "
                f"python -m cubevis.setup {node} first")
        return 1
    if not k.get("launcher"):
        ui.fail(f"kernel {kernel} has no launcher yet: python -m cubevis.setup HOST --kernel {kernel}")
        return 1
    if k.get("status") != "ok":
        ui.note(f"kernel {kernel} is {k.get('status')} ({k.get('last_error')}); "
                f"python -m cubevis.setup check {kernel} says whether it works now")
    if not local_steps(c):
        return 1
    name = J.make(c, kernel, node)
    if not name:
        return 1
    where = node or J.default_node(st, kernel)
    ui.ok(f"Jupyter kernel {name}: kernel {kernel} on {where}")
    ui.say("For example, with a measurement set in the kernel's working directory there:")
    J.show_use(ui, kernel if not node else name)
    if not node:
        ui.say(f"On another node of it, alongside this one: python -m cubevis.setup jupyter "
               f"{kernel} NODE makes cubevis-{kernel}-NODE.")
    return 0


def cmd_walk(c: Ctx) -> int:
    ui, st = c.ui, c.st
    ui.heading("cubevis setup")
    ui.say("This makes cubevis kernels work on the computers your data is on: how to log in "
           "to them from here (through a login host when there is one, with a stored password "
           "where a host asks for one), which Python environment there runs the kernels, and "
           "where they start. It asks before each change; everything it keeps is in "
           f"{tilde(S.state_path().parent)}, and every change is listed in "
           f"{tilde(ui.changes_path)}. Enter accepts the answer in [brackets].")
    rc = 0
    while True:
        if st.section("kernels"):
            ui.heading("Kernels")
            for kn, k in sorted(st.section("kernels").items()):
                e = st.section("environments").get(k.get("environment")) or {}
                where = e.get("cluster") or ", ".join(e.get("hosts") or []) or e.get("network")
                ui._p(f"    {kn}: on {where}, {k.get('status')}")
        host = ui.ask("The host your data is on (as you would ssh to it; empty: done)", "",
                      key="host").strip()
        if not host:
            return rc
        rc = cmd_host(c, host) or rc
        if ui.yes or "host" in ui.answers:
            return rc


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(
        prog="python -m cubevis.setup",
        description="Set up cubevis kernels on the computers your data is on.",
        epilog="Anything that is not a command is a host: python -m cubevis.setup HOST "
               "[--kernel NAME]. Without one, setup asks for it.")
    p.add_argument("--yes", action="store_true", help="take every default (for tests)")
    p.add_argument("--redo", action="store_true", help="ask again even for steps that pass")
    p.add_argument("--answer", action="append", default=[], metavar="KEY=VALUE",
                   help="pre-fill one question (for scripts and tests)")
    p.add_argument("--kernel", metavar="NAME", help="with HOST: add or change this kernel there")
    sub = p.add_subparsers(dest="cmd")
    ph = sub.add_parser("host", help="make kernels work on HOST (the default command)")
    ph.add_argument("host")
    ph.add_argument("--kernel", dest="kernel_after", metavar="NAME",
                    help="add or change this kernel there")
    sub.add_parser("status", help="what is set up")
    pc = sub.add_parser("check", help="test logins and kernels; change nothing")
    pc.add_argument("target", nargs="?", help="a kernel, node, cluster or network (default: all)")
    pn = sub.add_parser("network", help="go through a network's logins and inside ranges again")
    pn.add_argument("name")
    pj = sub.add_parser("jupyter", help="a Jupyter kernel for a setup kernel, for "
                                        "VisibilityPlotter(kernel_name=...)")
    pj.add_argument("jkernel", metavar="kernel")
    pj.add_argument("node", nargs="?", help="where it runs (default: where it was last checked)")
    pn.add_argument("--inside-here", action="store_true",
                    help="add where this computer is now to the network's inside ranges, "
                         "after checking that its nodes answer directly from here")
    # Anything that is not a command (or an option) is a host.
    takes_value = {"--answer", "--kernel"}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in takes_value:
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        if a not in COMMANDS and a != "host":
            argv.insert(i, "host")
        break
    a = p.parse_args(argv)
    a.kernel = getattr(a, "kernel_after", None) or a.kernel
    if a.kernel and a.cmd != "host":
        p.error("--kernel goes with a host: python -m cubevis.setup HOST --kernel NAME")
    answers = dict(kv.split("=", 1) for kv in a.answer if "=" in kv)
    ui = UI(yes=a.yes, answers=answers)
    try:
        st = S.load()
    except S.StateError as exc:
        ui.fail(str(exc))
        return 1
    c = Ctx(ui, st, check_only=(a.cmd == "check"), redo=a.redo)
    ui.log(f"---- cubevis.setup {' '.join(argv)}")
    # gpg (pass, gpg-file) asks for its passphrase on the terminal named here.
    if sys.stdin.isatty() and "GPG_TTY" not in os.environ:
        os.environ["GPG_TTY"] = os.ttyname(0)
    try:
        if a.cmd == "status":
            return cmd_status(c)
        if a.cmd == "check":
            return cmd_check(c, a.target)
        if a.cmd == "network":
            rc = cmd_inside_here(c, a.name) if a.inside_here else cmd_network(c, a.name)
        elif a.cmd == "jupyter":
            rc = cmd_jupyter(c, a.jkernel, a.node)
        elif a.cmd == "host":
            rc = cmd_host(c, a.host, a.kernel)
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
