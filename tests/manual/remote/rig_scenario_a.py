"""Scenario A on a rig: a real ssh route that dies and another that works.

Needs root (iptables, an sshd of its own) and a disposable machine or
container: it runs ``sshd`` on 127.0.0.1:2202 (the "direct" route) and
:2203 (the "outside" one), writes an ssh configuration in setup's shape --
a ``Match exec`` route test, shared connections, ``BatchMode``, setup's
keep-alives -- and an sshpyk kernel ``cubevis-rig-a`` that reaches this
same computer through it.  Each scenario then cuts the direct route the
way a dropped VPN does (packets dropped both ways, nothing refused) and
switches the route test to the outside one, as a laptop moving from work
to home does:

    between    cut between two calls; the next call must answer
    read       cut during a slow read; it is sent again and answers
    write      cut during a slow write; RemoteCallInterruptedError
    exit       the shared connection ended (ssh -O exit), between calls
    down       no route at all for a while: the call fails within the
               reconnect budget with the reason, the bridge loop never
               stalls, and the first call after the network is back answers

    sudo python tests/manual/remote/rig_scenario_a.py [SCENARIO ...] [--keep]

All scenarios by default.  Prints one line per scenario and exits non-zero
when one failed.  Removes its kernel, firewall rules and sshd on exit
(``--keep`` leaves the rig in place for poking at).  Notes section 82.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

KERNEL = "cubevis-rig-a"
DIRECT, OUTSIDE = 2202, 2203
SCENARIOS = ("between", "read", "write", "exit", "down")


def sh(*argv, check=True, **kw):
    return subprocess.run(list(argv), check=check, capture_output=True, text=True, **kw)


class Rig:
    def __init__(self, keep: bool = False):
        self.keep = keep
        self.dir = tempfile.mkdtemp(prefix="cubevis-rig-a-")
        self.cm = os.path.join(self.dir, "cm")
        self.config = os.path.join(self.dir, "config")
        self.route = os.path.join(self.dir, "route_outside")
        self.sshd_pid = None

    # -- setup ------------------------------------------------------------
    def __enter__(self):
        if os.geteuid() != 0:
            sys.exit("rig_scenario_a: needs root (iptables, sshd)")
        for tool in ("sshd", "ssh", "ssh-keygen", "iptables", "sshpyk"):
            if not (shutil.which(tool) or os.path.exists(f"/usr/sbin/{tool}")):
                sys.exit(f"rig_scenario_a: {tool} not found")
        os.makedirs(self.cm, mode=0o700)
        os.makedirs("/run/sshd", exist_ok=True)
        sh("ssh-keygen", "-A")
        key = os.path.join(self.dir, "id")
        sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key)
        auth = os.path.join(self.dir, "authorized_keys")
        shutil.copy(key + ".pub", auth)
        os.chmod(auth, 0o600)
        cfg = os.path.join(self.dir, "sshd_config")
        with open(cfg, "w") as f:
            f.write(f"Port {DIRECT}\nPort {OUTSIDE}\nListenAddress 127.0.0.1\n"
                    "PermitRootLogin prohibit-password\nPasswordAuthentication no\nStrictModes no\n"
                    f"AuthorizedKeysFile {auth}\nPidFile {self.dir}/sshd.pid\n")
        sshd = shutil.which("sshd") or "/usr/sbin/sshd"
        sh(sshd, "-f", cfg)
        for _ in range(50):
            if os.path.exists(f"{self.dir}/sshd.pid"):
                break
            time.sleep(0.1)
        self.sshd_pid = int(open(f"{self.dir}/sshd.pid").read())
        with open(self.config, "w") as f:
            # setup's shape (cubevis/setup/sshgen.py): shared connections,
            # BatchMode, keep-alives; the route chosen by a Match exec test
            f.write(f'Match host rig exec "test -f {self.route}"\n    Port {OUTSIDE}\n'
                    f"Host rig\n    HostName 127.0.0.1\n    Port {DIRECT}\n    User root\n"
                    f"    IdentityFile {key}\n    IdentitiesOnly yes\n    BatchMode yes\n"
                    "    StrictHostKeyChecking accept-new\n"
                    f"    UserKnownHostsFile {self.dir}/known_hosts\n"
                    f"    ControlMaster auto\n    ControlPath {self.cm}/%C\n"
                    "    ControlPersist 8h\n    ServerAliveInterval 30\n"
                    "    ServerAliveCountMax 6\n")
        sh("sshpyk", "add", "--kernel-name", KERNEL, "--display-name", KERNEL,
           "--language", "python", "--ssh-host-alias", "rig", "--ssh-config", self.config,
           "--remote-python", sys.executable, "--remote-kernel-name", "python3",
           "--launch-timeout", "60", "--replace")
        return self

    def __exit__(self, *exc):
        self.network(True)
        self.end_connections()
        if self.keep:
            print(f"rig left in {self.dir} (kernel {KERNEL}, sshd pid {self.sshd_pid})")
            return
        sh("jupyter", "kernelspec", "remove", "-f", KERNEL, check=False)
        if self.sshd_pid:
            try:
                os.kill(self.sshd_pid, 15)
            except ProcessLookupError:
                pass
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- the network ------------------------------------------------------
    def _rules(self, ports):
        return [["-p", "tcp", f"--{d}", str(p), "-j", "DROP", chain]
                for p in ports for chain, d in (("OUTPUT", "dport"), ("INPUT", "sport"))]

    def network(self, up: bool, ports=(DIRECT,)):
        if up:
            for rule in self._rules((DIRECT, OUTSIDE)):
                *r, chain = rule
                while sh("iptables", "-D", chain, *r, check=False).returncode == 0:
                    pass
            if os.path.exists(self.route):
                os.unlink(self.route)
            return
        for rule in self._rules(ports):
            *r, chain = rule
            sh("iptables", "-A", chain, *r)

    def move(self):
        '''the direct route dies; the route test now picks the outside one'''
        self.network(False, (DIRECT,))
        open(self.route, "w").close()

    def end_connections(self):
        for sock in os.listdir(self.cm):
            sh("ssh", "-F", self.config, "-S", os.path.join(self.cm, sock), "-O", "exit", "rig",
               check=False)


def connect():
    from cubevis.remote import RemoteObjectConnection
    return RemoteObjectConnection(
        KERNEL, "Counter", {"start": 1},
        register_function="cubevis.remote._test_registrations:register_basic",
        app="rig", data_methods=("slow_get", "slow_increment"),
        repeatable_methods=("get", "slow_get"))


def timed(fn):
    t0 = time.monotonic()
    try:
        return fn(), None, time.monotonic() - t0
    except Exception as exc:                          # noqa: BLE001 -- reported
        return None, exc, time.monotonic() - t0


def scenario(rig: Rig, name: str) -> str:
    from cubevis.remote import RemoteCallInterruptedError, RemoteKernelLostError
    rig.network(True)
    rig.end_connections()
    if name == "down":
        os.environ["CUBEVIS_REMOTE_RECONNECT"] = "30"
    else:
        os.environ.pop("CUBEVIS_REMOTE_RECONNECT", None)
    conn = connect()
    try:
        assert conn.call("get") == 1
        if name == "between":
            rig.move()
            time.sleep(2)
            v, exc, dt = timed(lambda: conn.call("get"))
            ok = v == 1 and dt < 60
            return f"{'PASS' if ok else 'FAIL'}  next call {v!r} after {dt:.0f} s {exc or ''}"
        if name in ("read", "write"):
            threading.Timer(3.0, rig.move).start()
            method = "slow_get" if name == "read" else "slow_increment"
            v, exc, dt = timed(lambda: conn.call(method, seconds=8))
            after, exc2, _ = timed(lambda: conn.call("get"))
            if name == "read":
                ok = v == 1 and after == 1
            else:
                ok = isinstance(exc, RemoteCallInterruptedError) and after in (1, 2)
            return (f"{'PASS' if ok else 'FAIL'}  {method} -> "
                    f"{type(exc).__name__ if exc else v!r} after {dt:.0f} s; then get -> {after!r}")
        if name == "exit":
            sh("ssh", "-F", rig.config, "-O", "exit", "rig", check=False)
            time.sleep(1)
            v, exc, dt = timed(lambda: conn.call("get"))
            ok = v == 1 and dt < 60
            return f"{'PASS' if ok else 'FAIL'}  next call {v!r} after {dt:.0f} s {exc or ''}"
        if name == "down":
            gaps, stop = [], False

            async def ticker():
                last = time.monotonic()
                while not stop:
                    await asyncio.sleep(0.2)
                    now = time.monotonic()
                    gaps.append(now - last)
                    last = now
            tick = conn.submit(ticker())
            rig.network(False, (DIRECT, OUTSIDE))
            v, exc, dt = timed(lambda: conn.call("get"))
            rig.network(True)
            after, exc2, dt2 = timed(lambda: conn.call("get"))
            stop = True
            tick.result(timeout=5)
            ok = (isinstance(exc, RemoteKernelLostError) and "next action" in str(exc)
                  and dt < 60 and after == 1 and max(gaps) < 1.0)
            return (f"{'PASS' if ok else 'FAIL'}  failed after {dt:.0f} s "
                    f"({type(exc).__name__}); loop stall {max(gaps):.1f} s; after: {after!r} "
                    f"in {dt2:.1f} s")
        raise ValueError(name)
    finally:
        rig.network(True)
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("scenarios", nargs="*", metavar="SCENARIO",
                    help=f"any of {', '.join(SCENARIOS)} (default: all)")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING,
                        format="  [%(name)s] %(levelname)s: %(message)s")
    logging.getLogger("traitlets").setLevel(logging.ERROR)
    bad = [n for n in a.scenarios if n not in SCENARIOS]
    if bad:
        ap.error(f"unknown scenario {', '.join(bad)}; choose from {', '.join(SCENARIOS)}")
    failed = 0
    with Rig(keep=a.keep) as rig:
        for name in a.scenarios or SCENARIOS:
            line = scenario(rig, name)
            failed += line.startswith("FAIL")
            print(f"{name:8s} {line}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
