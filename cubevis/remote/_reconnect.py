"""_reconnect.py
=============
Getting a session through a change of network (Scenario A, remote session
handoff of 2026-11-04, section (b)1): the laptop's Python and the browser
survive a sleep or a VPN that drops; only the ssh connections under the
kernel's tunnels die.  Two pieces, both for kernels started by sshpyk (a
kernel on this computer has no ssh and needs neither):

``make_nonblocking(km)`` -- A1, the reconnect must not block.  sshpyk
(1.25) reopens a missing shared connection inside an ``async`` method with
a blocking ``subprocess.run`` (``verify_ssh_connection``, up to three
tries of ``launch_timeout`` each) and reads ``ssh -O check``'s pipes
synchronously.  Both run on the event loop that carries every remote call
and the liveness watch, so while a network is down everything stops.  The
provisioner *instance* gets two replacements that do the same work off the
loop (``asyncio.to_thread``, ``asyncio.create_subprocess_exec``).  The
proper fix is an sshpyk PR (as Darrell's #12/#13); until a release has it,
this.  Nothing else of sshpyk changes.

``SshReconnector`` -- what the watch runs when the heartbeat has gone
quiet.  For each host the kernel's ssh goes through (sshpyk's
``all_hosts``: hops first, then the kernel's host):

1. a command through the shared connection (``ssh HOST true``, short
   timeout).  An answer means the connection is fine (the silence was
   something else, or has already ended);
2. no answer: the shared connection is stale -- its TCP connection went
   with the old network, and ssh's keep-alives (``ServerAliveInterval 30``
   x ``ServerAliveCountMax 6`` in setup's configuration) would end it only
   after three minutes.  ``ssh -O exit HOST`` ends it now;
3. ``ssh HOST true`` again opens a new one.  setup's ``Match exec`` route
   test runs again for it, so it takes whichever route works from here
   (the login host from home, the direct one on the internal network);

then sshpyk forwards the kernel's ports again over the new connection
(``ensure_tunnels``, the same local ports), and ZeroMQ, which reconnects
by itself, finds them.  Each step is a subprocess with a time limit,
awaited: the event loop keeps running.  When ssh cannot open a connection
by itself -- a login that asks for a second factor, a password not stored
-- its own message (setup's wrapper names the command to run in a
terminal) is what the failure carries.
"""
from __future__ import annotations

import asyncio
import logging
import os
import types
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

#: seconds for a command through an existing shared connection
CHECK_TIMEOUT = 8.0
#: seconds for opening a new shared connection (setup's own checks allow 30)
OPEN_TIMEOUT = 30.0


def _provisioner(km):
    prov = getattr(km, "provisioner", None)
    if prov is None or type(prov).__name__ != "SSHKernelProvisioner":
        return None
    if not getattr(prov, "ssh_base", None) or not getattr(prov, "all_hosts", None):
        return None
    return prov


async def run_ssh(argv: List[str], timeout: float) -> Tuple[Optional[int], str]:
    """Run *argv* (an ssh command) with stdin closed and a time limit;
    ``(returncode, output)``, returncode None when it ran out of time (the
    process is killed).  Never blocks the event loop."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    except OSError as exc:
        return 127, str(exc)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass
        return None, f"no answer within {timeout:.0f} s"
    return proc.returncode, (out or b"").decode(errors="replace").strip()


def _last_lines(text: str, n: int = 2) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()
             and not ln.startswith("Warning: Permanently added")]
    return " ".join(lines[-n:])


def make_nonblocking(km) -> bool:
    """Give *km*'s sshpyk provisioner a ``check_control_sockets`` (and the
    ``has_control_socket`` it uses) that keep the event loop running.
    True when installed; False for a kernel that is not sshpyk's, or an
    sshpyk whose shape this does not recognise (left as it is)."""
    prov = _provisioner(km)
    if prov is None:
        return False
    if getattr(prov, "_cubevis_nonblocking", False):
        return True
    try:
        from sshpyk.utils import verify_ssh_connection
    except Exception:
        return False
    for name in ("check_control_sockets", "has_control_socket", "launch_timeout",
                 "log", "log_prefix", "independent_local_processes"):
        if not hasattr(prov, name):
            log.debug("sshpyk provisioner has no %s; its reconnect is left as it is", name)
            return False

    async def has_control_socket(self, alias: str, log_error: bool = False) -> bool:
        rc, out = await run_ssh([*self.ssh_base, "-O", "check", alias], CHECK_TIMEOUT)
        ok = rc == 0
        if not ok:
            (self.le if log_error else self.ld)(
                f"Host '{alias}' does not have a control socket ({_last_lines(out, 1)}).")
        return ok

    async def check_control_sockets(self, raise_on_failed_open: bool = False):
        # sshpyk's own logic and messages, with the blocking calls off the loop
        for host in self.all_hosts:
            if not isinstance(host, str):
                raise RuntimeError(f"Invalid host alias: {host!r}")
            if await self.has_control_socket(host):
                continue
            ok = False
            for _ in range(3):
                ok, _uname = await asyncio.to_thread(
                    verify_ssh_connection, ssh=self.ssh_base, host_alias=host, log=self.log,
                    lp=self.log_prefix, start_new_session=self.independent_local_processes,
                    timeout=self.launch_timeout)
                if ok:
                    await self.has_control_socket(host)
                    break
                await asyncio.sleep(0.2)
            if ok:
                continue
            cmd = [*getattr(self, "ssh_base_help", self.ssh_base), host]
            msg = (f"Failed to open control socket for {host!r}. Make sure you can connect "
                   f"manually (w/out any passwords) to {host!r}: '{' '.join(cmd)}'")
            if not raise_on_failed_open:
                self.lw(msg)
                continue
            self.le(msg)
            raise RuntimeError(msg)

    prov.has_control_socket = types.MethodType(has_control_socket, prov)
    prov.check_control_sockets = types.MethodType(check_control_sockets, prov)
    prov._cubevis_nonblocking = True
    log.debug("sshpyk provisioner: control-socket checks moved off the event loop")
    return True


class SshReconnector:
    """Reopens the shared ssh connections under an sshpyk kernel and its
    tunnels (see the module docstring).  ``for_kernel(km)`` is None for a
    kernel that does not go through ssh."""

    def __init__(self, prov) -> None:
        self._prov = prov
        self.last_error = ""
        self.attempts = 0
        # host -> the shared connection's socket (ControlPath) while it worked
        self._paths: dict = {}

    @classmethod
    def for_kernel(cls, km) -> Optional["SshReconnector"]:
        prov = _provisioner(km)
        return cls(prov) if prov is not None else None

    @property
    def host(self) -> str:
        '''the kernel's own ssh host (alias)'''
        return str(getattr(self._prov, "ssh_host_alias", "") or self._prov.all_hosts[-1])

    def _ssh(self, *args: str) -> List[str]:
        return [*self._prov.ssh_base, *args]

    async def control_path(self, host: str) -> Optional[str]:
        '''the shared connection's socket for *host* as ssh resolves it now
        (``ssh -G``: setup's ``Match exec`` route test runs for it)'''
        rc, out = await run_ssh(self._ssh("-G", host), CHECK_TIMEOUT)
        if rc != 0:
            return None
        for line in out.splitlines():
            key, _, value = line.partition(" ")
            if key.lower() == "controlpath" and value.strip() and value.strip() != "none":
                return value.strip()
        return None

    async def remember(self) -> None:
        '''Record each host's socket while the connection works: after a
        change of network, the route -- and with it the socket's name (its
        ``%C`` hash covers port and hops) -- can differ, and the old shared
        connection, which still holds the kernel's forwarded ports, can then
        no longer be reached by the host's name.'''
        for host in list(self._prov.all_hosts):
            path = await self.control_path(host)
            if path:
                self._paths[host] = path

    async def reconnect(self) -> bool:
        """One attempt; True when every host's shared connection answers
        and the tunnels were forwarded again.  ``last_error`` says why not."""
        self.attempts += 1
        for host in list(self._prov.all_hosts):
            old, now = self._paths.get(host), await self.control_path(host)
            if old and now and old != now and os.path.exists(old):
                # Another route from here: end the old route's connection,
                # which holds the kernel's local ports, by its socket.
                log.info("ssh to %s: the route changed; ending the old shared connection",
                         host)
                await run_ssh(self._ssh("-S", old, "-O", "exit", host), CHECK_TIMEOUT)
            rc, out = await run_ssh(self._ssh(host, "true"), CHECK_TIMEOUT)
            if rc == 0:
                continue
            why = _last_lines(out, 1) or f"exit {rc}"
            log.info("ssh to %s: no answer through the shared connection (%s); "
                     "opening a new one", host, why)
            # what the failure says if time runs out before the next step
            self.last_error = f"ssh to {host} gave no answer ({why}); opening a new connection"
            await run_ssh(self._ssh("-O", "exit", host), CHECK_TIMEOUT)
            rc, out = await run_ssh(self._ssh("-o", f"ConnectTimeout={int(OPEN_TIMEOUT)}",
                                              host, "true"), OPEN_TIMEOUT + 5)
            if rc != 0:
                self.last_error = (f"ssh to {host} failed: "
                                   f"{_last_lines(out) or ('exit status %s' % rc)}")
                log.info("%s", self.last_error)
                return False
            log.info("ssh to %s: new shared connection open", host)
        try:
            await asyncio.wait_for(self._prov.ensure_tunnels(), timeout=OPEN_TIMEOUT)
        except Exception as exc:                       # noqa: BLE001 -- said, then retried
            self.last_error = f"forwarding the kernel's ports again failed: {exc}"
            log.info("%s", self.last_error)
            return False
        self.last_error = ""
        await self.remember()
        return True


def reconnect_budget() -> float:
    '''``CUBEVIS_REMOTE_RECONNECT`` (seconds): how long a silent kernel is
    reconnected to before a call fails (default 120).'''
    v = os.environ.get("CUBEVIS_REMOTE_RECONNECT", "").strip()
    try:
        return max(1.0, float(v)) if v else 120.0
    except ValueError:
        return 120.0
