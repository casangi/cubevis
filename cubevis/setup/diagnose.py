"""``python -m cubevis.setup diagnose HOST``: how this computer reaches HOST,
side by side with how setup's configuration does (remote session design 7c,
"Diagnostics, on both sides"; stage 2c step 4).

ssh is intricate, and a kernel that does not start says little.  This puts
in one place what is otherwise spread over ``ssh -G``, the generated
``ssh/config``, the wrapper's ``--explain``, the route decision and the node
itself:

1. what setup knows about HOST (node, network, cluster, login host, how each
   hop logs in, the last error);
2. what the user's own ssh makes of HOST (``ssh -G``) and what setup's
   configuration makes of it (``ssh -F ssh/config -G ALIAS``, with the route
   decided now), and the login options in the first that the second lacks;
3. the route: inside or outside the network, and why;
4. the wrapper's view of each hop (``--explain``: rules, stored password,
   shared connection);
5. a login: with the user's ssh (never asking), then with setup's -- and on
   failure the end of ssh's verbose output;
6. the kernel-side code here and in the environments of the kernels there,
   and how long their Python takes to start, with and without
   ``PYTHONHOME`` (``--pythonhome``: slow on automounted filesystems).

It changes nothing setup keeps: the state, the generated files and the
node are left as they are.  The logins may open a shared connection, as any
use of the host would.  The output is meant to be sent as it is to whoever
helps (nothing secret is in it: passwords are never shown, only whether
one is stored).
"""
from __future__ import annotations

import subprocess
import sys
from typing import Optional

from . import sshgen, sshopts, state as S
from .steps import Ctx, hop_info, last_line, split_port
from .ui import tilde

SHOWN = ("hostname", "user", "port", "proxyjump", "proxycommand")


def _mark(ui, good: bool, text: str) -> None:
    """A result line, indented like the rest (``ui.ok``/``ui.fail`` would
    count it as a step)."""
    ui._p(f"      {'ok:' if good else 'PROBLEM:'} {text}")


def _indent(ui, text: str, n: int = 6) -> None:
    for line in (text or "").rstrip().splitlines():
        ui._p(" " * n + line)


def _summary(v: dict) -> str:
    one = sshopts.one
    route = (f", through {one(v, 'proxyjump')}" if one(v, "proxyjump") else
             f", through the command {one(v, 'proxycommand')}" if one(v, "proxycommand") else
             ", directly")
    return f"{one(v, 'user')}@{one(v, 'hostname')}:{one(v, 'port') or 22}{route}"


def setup_view(alias: str) -> dict:
    """``ssh -F <setup's config> -G ALIAS`` (the route decided now: the
    ``Match exec`` runs)."""
    cfg = sshgen.config_path()
    if not cfg.exists():
        return {}
    try:
        r = subprocess.run(["ssh", "-F", str(cfg), "-G", alias], capture_output=True, text=True,
                           timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return {}
    out = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition(" ")
        if k:
            out.setdefault(k.lower(), []).append(v.strip())
    return out


def compare(ui, label: str, name: str, port: Optional[int], alias: Optional[str],
            route_is_users: bool) -> list:
    """The user's ssh and setup's for one hop; returns the login options
    setup's configuration lacks (``[(option, value)]``)."""
    mine = sshopts.view(name, port)
    ui._p(f"    {label}")
    if not mine:
        ui._p(f"      your ssh (ssh -G {name}): cannot say (is ssh installed? is your "
              "~/.ssh/config valid?)")
        return []
    ui._p(f"      your ssh      (ssh -G {name}): {_summary(mine)}")
    opts = sshopts.login_options(name, port, route=True)
    for k, v in opts:
        ui._p(f"          {k} {v}")
    if not alias:
        return []
    theirs = setup_view(alias)
    if not theirs:
        ui._p(f"      setup's       (ssh -F {tilde(sshgen.config_path())} -G {alias}): "
              "cannot say (no generated configuration yet?)")
        return []
    ui._p(f"      setup's       (ssh -F ... -G {alias}): {_summary(theirs)}")
    missing = []
    for k, v in opts:
        key = k.lower()
        if key == "proxycommand" and not route_is_users:
            continue
        got = theirs.get(key) or []
        if v.strip('"') not in [g.strip('"') for g in got]:
            missing.append((k, v))
    for what in ("hostname", "user", "port"):
        a, b = sshopts.one(mine, what), sshopts.one(theirs, what)
        if a and b and a.lower() != b.lower():
            ui._p(f"      differs: {what} is {a} in yours, {b} in setup's"
                  + (" (setup asked you for it)" if what == "user" else ""))
    for k, v in missing:
        _mark(ui, False, f"setup's lacks {k} {v}")
    if opts and not missing:
        ui._p("      setup's carries every login option yours has")
    return missing


def cmd_diagnose(c: Ctx, text: str, full: bool = False) -> int:
    ui, st = c.ui, c.st
    host, port = split_port(text)
    p = port if port != 22 else None
    ui.heading(f"Diagnosing {text} (read-only: nothing setup keeps is changed)")
    ui._p(f"    cubevis setup in {tilde(S.state_path().parent)}; this computer {sys.platform}, "
          f"python {sys.version.split()[0]}")
    v = subprocess.run(["ssh", "-V"], capture_output=True, text=True) if _has("ssh") else None
    ui._p(f"    ssh: {((v.stderr or v.stdout).strip() if v else 'not found')}")
    # 1. What setup knows.
    ui._p("")
    ui._p("    1. What setup knows")
    node = st.find_node(host)
    net = None
    if node:
        n = st.nodes()[node]
        net = st.networks()[n["network"]]
        lg = net.get("login")
        ui._p(f"      node {node} of network {n['network']}"
              + (f", cluster {n['cluster']}" if n.get("cluster") else "")
              + f": {net['user']}@{n['hostname']}" + (f":{n['port']}" if n.get("port") else "")
              + f", ssh name {n['alias']}" + (f", your name for it {n['ssh_name']}"
                                              if n.get("ssh_name") else ""))
        ui._p(f"      status {n.get('status')}" + (f" ({n['last_error']})" if n.get("last_error") else "")
              + (f", checked {n['last_checked']}" if n.get("last_checked") else ""))
        if lg:
            ui._p(f"      login host {lg['user']}@{lg['host']}" + (f":{lg['port']}" if lg.get("port", 22) != 22 else "")
                  + f" ({lg['alias']}" + (f", your name for it {lg['ssh_name']}" if lg.get("ssh_name") else "")
                  + (f", itself through {lg['via']}" if lg.get("via") else "") + "): "
                  + _how(st, lg, lg["user"]))
        ui._p(f"      the nodes: {_how(st, net, net['user'])}")
    else:
        cl = st.cluster_for(host)
        ui._p(f"      setup does not know {host}" + (f"; it matches cluster {cl}" if cl else "")
              + f": python -m cubevis.setup {text} sets it up. Below, only your ssh's view.")
    # 2. The two configurations.
    ui._p("")
    ui._p("    2. Your ssh configuration and setup's")
    missing = []
    if net and net.get("login"):
        h = hop_info(st, n["network"], None)
        missing += compare(ui, f"login host {h['host']}", h["name"], h["port"], h["alias"], True)
    if node:
        h = hop_info(st, n["network"], node)
        missing += compare(ui, f"node {node}", h["name"], h["port"], h["alias"], not net.get("login"))
    else:
        compare(ui, host, host, p, None, True)
    if missing:
        ui.say("Setup writes its configuration from your ssh's (ssh -G) each time it saves: "
               f"your configuration has changed since, or this is a bug. python -m cubevis.setup "
               f"{text} writes it again.")
    # 3. The route.
    if net and net.get("login"):
        from . import inside as _in
        ui._p("")
        ui._p("    3. The route")
        forced, why = _in.override(n["network"])
        if forced is not None:
            ui._p(f"      {why}: {'directly' if forced else 'through the login host'}")
        else:
            ok, why = _in.decide(_in.network_hosts(net, st.nodes(n["network"])), net.get("inside") or [])
            ui._p(f"      {'inside: directly' if ok else 'outside: through the login host'} -- {why}")
            ui._p(f"      inside ranges: {', '.join(S.collapse_ranges(net.get('inside'))) or 'none'}; "
                  f"{_in.route_var(n['network'])}=direct or =jump decides instead")
    # 4. The wrapper.
    ok = None
    if node:
        ui._p("")
        ui._p("    4. The wrapper (--explain: rules, stored passwords, shared connections)")
        w = sshgen.wrapper_path()
        if not w.exists():
            ui._p(f"      not installed here ({tilde(w)}): python -m cubevis.setup {text}")
        else:
            r = subprocess.run([str(w), "--explain", "-F", str(sshgen.config_path()), n["alias"]],
                               capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            _indent(ui, (r.stdout or "") + (r.stderr or ""))
    # 5. Logging in.
    ui._p("")
    ui._p("    5. Logging in")
    if node:
        h = hop_info(st, n["network"], node)
        name, hp = h["name"], h["port"]
    else:
        name, hp = host, p
    verdict, why = sshopts.try_login(name, hp)
    words = {"ok": "logs in without asking (a key, Kerberos)",
             "answers": "reaches it, but would ask for something (a password, a code) or "
                        "does not know its host key yet",
             "unreachable": "does not reach it from here", "unknown": "cannot tell"}
    ui._p(f"      your ssh (ssh {name}, never asking): {words[verdict]}" + (f" -- {why}" if why else ""))
    if node and sshgen.wrapper_path().exists():
        r = c.ssh(n["alias"], timeout=120)
        ok = r.returncode == 0
        if ok:
            _mark(ui, True, "setup's (the wrapper and its configuration, as kernels do): logged in")
        else:
            _mark(ui, False, "setup's (the wrapper and its configuration, as kernels do): "
                  f"{last_line(c.last_out) or 'ssh exit ' + str(r.returncode)}")
            _indent(ui, "\n".join(l for l in c.last_out.splitlines() if l.startswith("[ssh-wrapper]")))
            r = subprocess.run([str(sshgen.wrapper_path()), "-F", str(sshgen.config_path()), "-v",
                                "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", n["alias"], "true"],
                               capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            tail = [l for l in (r.stderr or "").splitlines() if l.startswith("debug1:")
                    and not any(x in l for x in ("Reading configuration", "kex_", "Applying options",
                                                 "channel", "client_input", "rekey", "Sending env"))]
            ui._p("      the end of ssh -v:")
            _indent(ui, "\n".join(tail[-15:]), 8)
            if verdict == "ok":
                _mark(ui, False, "your ssh logs in without asking and setup's does not: "
                      "compare the two in section 2")
    # 6. Kernel-side code.
    if node and ok:
        ui._p("")
        ui._p("    6. The kernel-side code (it must be identical here and there)")
        from .hostsetup import local_fingerprint, local_is_development, local_version
        from .. import _fingerprint
        here = local_fingerprint(c)
        dev, dwhy = local_is_development()
        ui._p(f"      here: {here} (cubevis {local_version()}" + (f", {dwhy}" if dev else "") + ")")
        envs_ = [(e, rec) for e, rec in sorted(st.section("environments").items())
                 if st.environment_on(e, node)]
        if not envs_:
            ui._p(f"      no environment is recorded on {node}")
        prefixes = [rec.get("prefix") or rec["python"] for _e, rec in envs_]
        rep = None
        if prefixes:
            from . import remote as R
            try:
                rep = R.discover(c, n["alias"], prefixes, only=True)
            except R.RemoteError as exc:
                _mark(ui, False, f"looking at {node} failed: {exc}")
        got = {}
        for e in (rep or {}).get("environments") or []:
            got[e.get("prefix")] = e
            got[e.get("python")] = e
        for e, rec in envs_:
            kernels = [k for k, kr in sorted(st.section("kernels").items()) if kr.get("environment") == e]
            cand = got.get(rec.get("prefix")) or got.get(rec.get("python")) or {}
            if cand.get("check") is None:
                _mark(ui, False, f"{e} ({rec.get('prefix') or rec['python']}"
                      + (f"; kernels {', '.join(kernels)}" if kernels else "")
                      + f"): not found on {node} -- removed?"
                      + (f" python -m cubevis.setup {text} offers to give "
                         + ("them" if len(kernels) > 1 else "it") + " another" if kernels
                         else " no kernel uses it"))
                continue
            from . import envs as E
            r = E.interpret(cand["check"])
            fp = r.get("fingerprint")
            usable, why = (_fingerprint.compatible(here, fp) if r["ok"]
                           else (False, r.get("error") or "it cannot run kernels"))
            _mark(ui, usable, f"{e} ({rec.get('prefix') or rec['python']}"
                  + (f"; kernels {', '.join(kernels)}" if kernels else "") + "): "
                  + (f"{fp}, " if fp else "") + why)
            if kernels:
                startup(c, n["alias"], dict(rec, base_prefix=(cand.get("check") or {}).get("base_prefix")
                                            or rec.get("base_prefix")), kernels, text, full)
    ui._p("")
    ui.say("On the node itself, python3 -m cubevis.setup.discover (where cubevis is installed) "
           "shows what setup sees there; read-only.")
    return 0 if (ok or ok is None and verdict in ("ok", "answers")) else 1


# Run by the node's python3 (3.6+): how long the environment's python takes
# to start, without PYTHONHOME and with it.
_STARTUP = r'''
import json, os, subprocess, sys, time
py, home = sys.argv[1], sys.argv[2]
out = {}
for label, extra in (("plain", {}), ("home", {"PYTHONHOME": home})):
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.update(extra)
    t = time.time()
    r = subprocess.run([py, "-c", "pass"], env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    out[label] = [round(time.time() - t, 2), r.returncode]
print("CUBEVIS-STARTUP " + json.dumps(out))
'''
SLOW = 5.0          # seconds: worth --pythonhome


def startup(c, alias: str, rec: dict, kernels: list, text: str, full: bool = False) -> None:
    """How long environment *rec*'s python takes to start on the node,
    without ``PYTHONHOME`` and with it (``launcher.home_of``).  Not timed
    again when every kernel of it sets ``PYTHONHOME`` already -- the slow
    start, a minute, is what was diagnosed -- unless *full*."""
    import json
    import shlex
    from . import launcher
    ui, st = c.ui, c.st
    home = launcher.home_of(rec)
    if not home:
        return
    ks = st.section("kernels")
    if not full and all(ks[k].get("pythonhome") for k in kernels):
        why = []
        for k in kernels:
            t = ks[k].get("pythonhome_times")
            why.append(f"{k}: " + ("your choice" if ks[k].get("pythonhome_by") == "you" else
                                   "set by setup, the start being slow")
                       + (f", {t[0]:.0f} s -> {t[1]:.1f} s" if t else ""))
        ui._p(f"        kernel{'s' if len(kernels) > 1 else ''} {', '.join(kernels)} "
              f"set{'' if len(kernels) > 1 else 's'} PYTHONHOME={home} ({'; '.join(why)}); "
              f"Python's start not timed again (diagnose {text} --full does)")
        return
    ui.live(f"        timing Python's start there, without PYTHONHOME and with it "
            "(a slow start takes up to a minute) ...")
    r = c.ssh(alias, "python3", "-", shlex.quote(rec["python"]), shlex.quote(home),
              timeout=300, input=_STARTUP)
    line = [l for l in (r.stdout or "").splitlines() if l.startswith("CUBEVIS-STARTUP ")]
    if not line:
        _mark(ui, False, f"timing Python's start there failed: {last_line(c.last_out) or 'no answer'}")
        return
    got = json.loads(line[-1].split(" ", 1)[1])
    (plain, prc), (withh, hrc) = got["plain"], got["home"]
    using = [k for k in kernels if st.section("kernels")[k].get("pythonhome")]
    ui._p(f"        Python starts in {plain:.1f} s there; with PYTHONHOME={home} in {withh:.1f} s"
          + ("" if hrc == 0 else " (but it fails then)")
          + (f"; kernel {', '.join(using)} set{'s' if len(using) == 1 else ''} it" if using else "")
          + "".join(f" ({k}: {'your choice' if st.section('kernels')[k].get('pythonhome_by') == 'you' else 'set by setup, the start being slow'})"
                    for k in using))
    if plain > SLOW and hrc == 0 and withh < plain / 3 and len(using) < len(kernels):
        _mark(ui, False, f"slow without PYTHONHOME: python -m cubevis.setup {text} --pythonhome")


def _how(st, rec: dict, user: str) -> str:
    if rec.get("twofactor"):
        return "second factor: you log in, the connection is shared"
    if rec.get("password"):
        from . import stores
        try:
            where = stores.where(st, rec.get("service"), user)
        except Exception:
            where = f"entry {rec.get('service')}"
        return "stored password (" + where + ")"
    return "a key or Kerberos (nothing stored)"


def _has(prog: str) -> bool:
    import shutil
    return bool(shutil.which(prog))
