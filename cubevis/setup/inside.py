"""Is this computer inside a network?  ``python -m cubevis.setup.inside NET``
exits 0 if so, 1 if not.

The generated ssh configuration runs this in a ``Match ... exec`` line
before every connection to a node of a network that has both a login host
and ``inside`` address ranges: inside, ssh goes to the node directly;
outside, through the login host (``sshgen``).  Deciding at each connection
is what makes a laptop that moves (office, home, VPN) do the right thing.

The test is not "does this computer have an address in the ranges": a
Docker or libvirt bridge (172.17.0.1, 192.168.122.1) or a home router's
10.0.0.x would say yes anywhere.  It is the route: resolve a node's name
(a name that does not resolve here means outside), and ask the system which
local address it would send from to reach it (a UDP ``connect``, which
sends nothing).  Inside if that address is in the network's ranges.

Anything unexpected -- no state, an unknown network, a resolver that hangs
-- answers "outside": the login host works from anywhere, a direct
connection only from inside.  ``-v`` prints why.

Standard library only, and kept light: it runs at every ssh connection.
"""
from __future__ import annotations

import ipaddress
import socket
import sys
import threading
from typing import Iterable, Optional

RESOLVE_TIMEOUT = 1.0     # seconds; off-site, an internal name may hang
MAX_NODES = 3             # nodes tried (the default node first)


def resolve(host: str, timeout: float = RESOLVE_TIMEOUT) -> list:
    """The addresses *host* resolves to here, or [] (also on timeout).
    getaddrinfo has no timeout of its own, so it runs in a daemon thread."""
    out: list = []

    def run():
        try:
            for fam, _t, _p, _c, sa in socket.getaddrinfo(host, 22, proto=socket.IPPROTO_TCP):
                if fam in (socket.AF_INET, socket.AF_INET6) and sa[0] not in out:
                    out.append(sa[0])
        except Exception:          # anything at all: "does not resolve"
            pass
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return list(out) if not t.is_alive() else []


def route_source(addr: str, port: int = 22) -> Optional[str]:
    """The local address the system would send from to reach *addr*
    (nothing is sent), or None if there is no route."""
    fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
    try:
        with socket.socket(fam, socket.SOCK_DGRAM) as s:
            s.connect((addr, port))
            return s.getsockname()[0]
    except OSError:
        return None


def in_ranges(addr: str, ranges: Iterable[str]) -> bool:
    try:
        a = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    for r in ranges:
        try:
            if a in ipaddress.ip_network(r, strict=False):
                return True
        except ValueError:
            continue
    return False


def decide(hosts: Iterable[str], ranges: list, *, resolver=resolve, router=route_source):
    """``(inside, reason)`` for nodes named *hosts* and site *ranges*."""
    if not ranges:
        return False, "no inside ranges for this network"
    tried = []
    for h in hosts:
        addrs = resolver(h)
        if not addrs:
            tried.append(f"{h}: does not resolve here")
            continue
        for a in addrs:
            src = router(a)
            if src is None:
                tried.append(f"{h} ({a}): no route")
            elif in_ranges(src, ranges):
                return True, f"{h} ({a}) is reached from {src}, inside {', '.join(ranges)}"
            else:
                tried.append(f"{h} ({a}) is reached from {src}, not in {', '.join(ranges)}")
    return False, "; ".join(tried) or "no nodes to try"


def network_hosts(net: dict, nodes: dict) -> list:
    """Real names of up to MAX_NODES nodes of a network, default first."""
    names = sorted(nodes)
    d = net.get("default_node")
    if d in names:
        names.remove(d)
        names.insert(0, d)
    return [nodes[n]["hostname"] for n in names[:MAX_NODES]]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose = "-v" in argv
    args = [a for a in argv if a != "-v"]
    if len(args) != 1:
        print("usage: python -m cubevis.setup.inside [-v] NETWORK", file=sys.stderr)
        return 2
    name = args[0]
    try:
        from . import state as _state
        st = _state.load(host=False)      # the networks only: no lock, no write
        net = st.get("networks", name)
        ok, why = decide(network_hosts(net, st.nodes(name)), net.get("inside") or [])
    except Exception as exc:                       # never break ssh: say outside
        ok, why = False, f"cannot tell ({exc})"
    if verbose:
        print(f"{name}: {'inside' if ok else 'outside'} -- {why}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
