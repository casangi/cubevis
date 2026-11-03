"""What the user's own ssh makes of a host, and what of it setup's generated
configuration must carry (remote session design 7c, "The user's ssh
configuration is the reference"; stage 2c step 4).

``ssh -G NAME`` prints every option ssh would use for NAME -- the user's
``~/.ssh/config`` *and* the system's ``/etc/ssh/ssh_config`` (at NRAO, say,
``GSSAPIAuthentication yes``, or RHEL's crypto policies) -- without
connecting.  ``ssh -F /dev/null -G NAME`` prints ssh's built-in defaults.
The options that differ, among those that affect *logging in*, are what the
generated ``ssh/config`` (read with ``-F``, so it sees neither file) copies
into the host's block: identity and certificate files, ``IdentitiesOnly``,
the agent, GSSAPI (Kerberos), PKCS#11 and security keys, the
authentication methods, the algorithms a server may insist on, host-key
files and aliases, the address family -- and ``ProxyCommand`` where the
route is the user's own.  Host name, user and port are setup's own fields;
``ProxyJump`` becomes setup's login host (``hostsetup.new_network``);
connection sharing and keep-alives are setup's.

**Recorded per host, changes reported** (Darrell 2026-11-02, notes section
55): what ``ssh -G`` gave when a host was set up is kept in this computer's
file (``local.json``, ``ssh_options``, by setup's alias: ``snapshot``), and
the generated file is written from that -- not from whatever the user's
configuration says today, which may be a slip.  Each run compares
(``changes``): a difference is logged, shown by ``status`` and
``diagnose``, and adopted only by ``setup HOST`` (or ``network NAME``),
after asking (``hostsetup.adopt_step``).  A host recorded before this is
recorded at the next save, as it is then (``fill_missing``).  Each computer
sharing a configuration directory keeps its own (the option names are
those of its ssh: OpenSSH 8.0 prints ``pubkeyacceptedkeytypes`` where 9.x
prints ``pubkeyacceptedalgorithms``).

``CUBEVIS_SETUP_USER_SSH_CONFIG`` names another file than ``~/.ssh/config``
(and leaves out the system's: ``-F``) -- ssh reads the account's home, not
``$HOME``, so the tests need it.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Optional

# Options (as ``ssh -G`` names them, lower case) that change whether or how
# a login works.  Anything starting with ``gssapi`` too (Red Hat's builds add
# gssapikeyexchange, gssapitrustdns, ...).
LOGIN_KEYS = (
    "identityfile", "certificatefile", "identitiesonly", "identityagent",
    "pubkeyauthentication", "passwordauthentication", "kbdinteractiveauthentication",
    "challengeresponseauthentication", "kbdinteractivedevices", "preferredauthentications",
    "hostbasedauthentication", "numberofpasswordprompts",
    "pkcs11provider", "securitykeyprovider",
    "pubkeyacceptedalgorithms", "pubkeyacceptedkeytypes", "hostkeyalgorithms",
    "hostbasedacceptedalgorithms", "hostbasedkeytypes", "casignaturealgorithms",
    "kexalgorithms", "ciphers", "macs", "requiredrsasize",
    "hostkeyalias", "userknownhostsfile", "globalknownhostsfile", "checkhostip",
    "verifyhostkeydns", "addressfamily",
    "usekeychain",          # Apple's ssh: a key's passphrase from the Keychain (no agent needed)
)
# One value that may hold spaces (a path): quoted when written.
_ONE_PATH = ("identityfile", "certificatefile", "identityagent", "pkcs11provider",
             "securitykeyprovider")
# Fields setup keeps itself, and never copies.
OWN_KEYS = ("hostname", "user", "port", "proxyjump", "controlmaster", "controlpath",
            "controlpersist", "serveraliveinterval", "serveralivecountmax", "batchmode",
            # setup's own known-hosts file first, the user's after it (``known_hosts``)
            "userknownhostsfile", "stricthostkeychecking")
# How they are written (ssh does not mind the case; people reading the file do).
_PRETTY = {k: k for k in LOGIN_KEYS}
_PRETTY.update({
    "identityfile": "IdentityFile", "certificatefile": "CertificateFile",
    "identitiesonly": "IdentitiesOnly", "identityagent": "IdentityAgent",
    "pubkeyauthentication": "PubkeyAuthentication",
    "passwordauthentication": "PasswordAuthentication",
    "kbdinteractiveauthentication": "KbdInteractiveAuthentication",
    "challengeresponseauthentication": "ChallengeResponseAuthentication",
    "kbdinteractivedevices": "KbdInteractiveDevices",
    "preferredauthentications": "PreferredAuthentications",
    "hostbasedauthentication": "HostbasedAuthentication",
    "numberofpasswordprompts": "NumberOfPasswordPrompts",
    "pkcs11provider": "PKCS11Provider", "securitykeyprovider": "SecurityKeyProvider",
    "pubkeyacceptedalgorithms": "PubkeyAcceptedAlgorithms",
    "pubkeyacceptedkeytypes": "PubkeyAcceptedKeyTypes",
    "hostkeyalgorithms": "HostKeyAlgorithms",
    "hostbasedacceptedalgorithms": "HostbasedAcceptedAlgorithms",
    "hostbasedkeytypes": "HostbasedKeyTypes",
    "casignaturealgorithms": "CASignatureAlgorithms",
    "kexalgorithms": "KexAlgorithms", "ciphers": "Ciphers", "macs": "MACs",
    "requiredrsasize": "RequiredRSASize", "hostkeyalias": "HostKeyAlias",
    "userknownhostsfile": "UserKnownHostsFile",
    "globalknownhostsfile": "GlobalKnownHostsFile", "checkhostip": "CheckHostIP",
    "verifyhostkeydns": "VerifyHostKeyDNS", "addressfamily": "AddressFamily",
    "usekeychain": "UseKeychain",
    "proxycommand": "ProxyCommand", "proxyjump": "ProxyJump",
    "gssapiauthentication": "GSSAPIAuthentication",
    "gssapidelegatecredentials": "GSSAPIDelegateCredentials",
})

_cache: dict = {}


def user_config() -> Optional[str]:
    return os.environ.get("CUBEVIS_SETUP_USER_SSH_CONFIG") or None


def _stamp() -> tuple:
    """What a cached answer depends on: the configuration files' times."""
    alt = user_config()
    files = [alt] if alt else [os.path.expanduser("~/.ssh/config"), "/etc/ssh/ssh_config"]
    out = [alt, shutil.which("ssh")]
    for f in files:
        try:
            out.append(os.stat(f).st_mtime_ns)
        except OSError:
            out.append(None)
    return tuple(out)


def view(name: str, port: Optional[int] = None, *, defaults: bool = False) -> dict:
    """``ssh -G`` for *name*: ``{option: [values]}`` (lower-case names; most
    have one value, ``identityfile`` and the like several).  *defaults*:
    ssh's own, with no configuration file.  {} when ssh cannot say."""
    cfg = "/dev/null" if defaults else user_config()
    key = (name, port, cfg, _stamp())
    if key in _cache:
        return _cache[key]
    cmd = ["ssh"] + (["-F", cfg] if cfg else []) + (["-p", str(port)] if port else []) + ["-G", name]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                           stdin=subprocess.DEVNULL)
        out = {}
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                k, _, v = line.partition(" ")
                if k:
                    out.setdefault(k.lower(), []).append(v.strip())
    except (OSError, subprocess.SubprocessError):
        out = {}
    _cache[key] = out
    return out


def one(v: dict, key: str) -> Optional[str]:
    """The (first) value of *key*, None when absent or ``none``."""
    x = (v.get(key) or [None])[0]
    return None if x is None or x.lower() == "none" else x


def _wanted(key: str, route: bool) -> bool:
    if key in OWN_KEYS:
        return False
    if key == "proxycommand":
        return route
    return key in LOGIN_KEYS or key.startswith("gssapi")


def login_options(name: str, port: Optional[int] = None, *, route: bool = False) -> list:
    """``[(Option, value), ...]``: the login options the user's ssh uses for
    *name* that differ from ssh's defaults, in ``ssh -G``'s order.
    *route*: also ``ProxyCommand`` (when the route to the host is the
    user's, not a login host of setup's)."""
    mine, base = view(name, port), view(name, port, defaults=True)
    if not mine or not base:
        return []
    out = []
    for k, vals in mine.items():
        if not _wanted(k, route) or vals == base.get(k):
            continue
        if k == "proxycommand" and one(mine, k) is None:
            continue
        for v in vals:
            if k in _ONE_PATH and re.search(r"\s", v) and not v.startswith('"'):
                v = f'"{v}"'
            out.append((_PRETTY.get(k, k), v))
    return out


def known_hosts(name: str, port: Optional[int] = None) -> list:
    """The user's known-hosts files for *name* (``ssh -G``: by default
    ``~/.ssh/known_hosts ~/.ssh/known_hosts2``), listed after setup's own
    in the generated file.  With ``CUBEVIS_SETUP_KNOWN_HOSTS`` (the tests)
    only files the user configured -- not ssh's defaults, the account's."""
    mine = " ".join(view(name, port).get("userknownhostsfile") or []).split()
    if os.environ.get("CUBEVIS_SETUP_KNOWN_HOSTS"):
        base = " ".join(view(name, port, defaults=True).get("userknownhostsfile") or []).split()
        if mine == base:
            return []
    return mine


def snapshot(name: str, port: Optional[int] = None, *, route: bool = False) -> dict:
    """What setup records for a host (module docstring): its login options
    and the user's known-hosts files, as the user's ssh gives them now."""
    import datetime as _dt
    return {"from": name + (f":{port}" if port else ""),
            "options": [list(x) for x in login_options(name, port, route=route)],
            "known_hosts": known_hosts(name, port),
            "saved": _dt.datetime.now().astimezone().isoformat(timespec="minutes")}


def differ(old: dict, new: dict) -> Optional[dict]:
    """``{"added": [...], "removed": [...]}`` between two snapshots (options
    as ``"Option value"``, known-hosts files as ``UserKnownHostsFile ...``),
    None when they agree."""
    def items(s):
        out = [f"{k} {v}" for k, v in s.get("options") or []]
        if s.get("known_hosts"):
            out.append("UserKnownHostsFile " + " ".join(s["known_hosts"]))
        return out
    a, b = items(old), items(new)
    added = [x for x in b if x not in a]
    removed = [x for x in a if x not in b]
    return {"added": added, "removed": removed} if added or removed else None


def parse_hops(spec: str) -> list:
    """A ProxyJump value as hops, each ``{"ssh_name", "ssh_port", "host",
    "user", "port"}`` -- the name (and port) as the user wrote it (their
    alias), the real name, user and port from their ssh (``user`` None:
    ssh's own default)."""
    hops = []
    for hop in spec.split(","):
        hop = hop.strip()
        if not hop:
            continue
        if hop.startswith("ssh://"):
            hop = hop[len("ssh://"):]
        user, _, hp = hop.rpartition("@")
        host, port = hp, None
        if hp.count(":") == 1 and hp.split(":")[1].isdigit():
            host, port = hp.split(":")[0], int(hp.split(":")[1])
        v = view(host, port)
        real = one(v, "hostname") or host
        if not user and one(v, "user") != one(view(host, port, defaults=True), "user"):
            user = one(v, "user")             # only when not ssh's own default
        p = port or int(one(v, "port") or 22)
        hops.append({"ssh_name": host, "ssh_port": port, "host": real,
                     "user": user or None, "port": p})
    return hops


def resolve_jump(spec: str) -> str:
    """A ProxyJump value whose hops may be the user's aliases, as real
    ``user@host:port`` -- the generated configuration (``-F``) knows none of
    the user's names."""
    return ",".join((f"{h['user']}@" if h["user"] else "") + h["host"]
                    + (f":{h['port']}" if h["port"] != 22 else "") for h in parse_hops(spec))


# What a direct, non-interactive login with the user's own ssh says.
UNREACHABLE = re.compile(r"Could not resolve|Name or service not known|nodename nor servname|"
                         r"timed out|Connection refused|No route to host|Network is unreachable|"
                         r"Connection closed by UNKNOWN|Temporary failure in name resolution")
ANSWERS = re.compile(r"Permission denied|Host key verification failed|Too many authentication|"
                     r"REMOTE HOST IDENTIFICATION|Received disconnect|Authentication failed|"
                     r"No more authentication methods")


def try_login(name: str, port: Optional[int] = None, timeout: int = 8) -> tuple:
    """A login with the user's own ssh, never asking (``BatchMode``), never
    becoming a shared connection's master (an existing one is used) and
    never recording a host key: ``(verdict, last line)``, the verdict one
    of ``ok`` (logged in, by key or Kerberos), ``answers`` (the host is
    reachable from here but wants something typed, or its key is not known
    yet), ``unreachable`` (no route, no name, no answer), ``unknown``."""
    cfg = user_config()
    cmd = (["ssh"] + (["-F", cfg] if cfg else []) + (["-p", str(port)] if port else [])
           + ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={timeout}",
              "-o", "ControlMaster=no", "-o", "StrictHostKeyChecking=yes",
              "-o", "LogLevel=ERROR", name, "true"])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return "unreachable", f"no answer within {timeout + 10} s"
    except (OSError, subprocess.SubprocessError) as exc:
        return "unknown", str(exc)
    lines = [l for l in (r.stderr or "").strip().splitlines() if l.strip()]
    last = lines[-1] if lines else ""
    text = r.stderr or ""
    if r.returncode == 0:
        return "ok", last
    if UNREACHABLE.search(text):
        return "unreachable", last
    if ANSWERS.search(text):
        return "answers", last
    if r.returncode != 255:          # logged in; the command itself failed
        return "ok", last
    return "unknown", last or f"ssh exit {r.returncode}"
