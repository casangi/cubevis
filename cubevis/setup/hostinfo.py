"""Facts a node knows about itself, for its node file: its full name, its
network addresses (with their subnets: candidate inside ranges for the GUI
side, which the user confirms there), and the filesystem its home
directory is on (a shared one suggests the other nodes see the same
environments).  Standard library and the system's own commands; nothing
is changed.
"""
from __future__ import annotations

import ipaddress
import os
import platform
import re
import socket
import subprocess
from typing import Optional

SHARED_FS = ("nfs", "nfs4", "lustre", "gpfs", "cifs", "smbfs", "smb3", "ceph", "cephfs",
             "beegfs", "glusterfs", "fuse.glusterfs", "fuse.sshfs", "afs", "panfs", "wekafs",
             "autofs")
# Bridges for containers and VMs: on every machine, never a site's network.
SKIP_RANGES = ("172.17.0.0/16", "192.168.122.0/24", "192.168.121.0/24")


def fqdn() -> str:
    """The full host name (``socket.getfqdn``), as far as the system says."""
    name = socket.gethostname()
    try:
        full = socket.getfqdn(name)
    except OSError:
        full = name
    return full if "." in full else name


def domain_of(host: str) -> Optional[str]:
    parts = host.split(".")
    return ".".join(parts[1:]) if len(parts) > 1 else None


def _run(cmd) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                              stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def interfaces() -> list:
    """This computer's addresses as interface CIDRs (``10.7.1.20/24``), from
    ``ip -o addr`` (Linux) or ``ifconfig`` (macOS); loopback, link-local and
    container/VM bridges left out."""
    raw = []
    if platform.system() == "Linux":
        for line in _run(["ip", "-o", "addr", "show"]).splitlines():
            m = re.search(r"\binet6?\s+(\S+)", line)
            if m:
                raw.append(m.group(1))
        if not raw:
            raw = [a + "/32" for a in _run(["hostname", "-I"]).split()]
    else:
        for line in _run(["ifconfig"]).splitlines():
            m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)\s+netmask\s+(0x[0-9a-f]+|\S+)", line)
            if m:
                mask = m.group(2)
                bits = bin(int(mask, 16)).count("1") if mask.startswith("0x") else \
                    ipaddress.ip_network(f"0.0.0.0/{mask}").prefixlen
                raw.append(f"{m.group(1)}/{bits}")
            m6 = re.search(r"\binet6\s+([0-9a-f:]+)(?:%\S+)?\s+prefixlen\s+(\d+)", line)
            if m6:
                raw.append(f"{m6.group(1)}/{m6.group(2)}")
    out = []
    for r in raw:
        try:
            iface = ipaddress.ip_interface(r)
        except ValueError:
            continue
        a = iface.ip
        if a.is_loopback or a.is_link_local or a.is_multicast:
            continue
        if any(a in ipaddress.ip_network(s) for s in SKIP_RANGES):
            continue
        if str(iface) not in out:
            out.append(str(iface))
    return out


def ranges_of(ifaces) -> list:
    """The subnets of *ifaces* (``10.7.1.0/24``), IPv4 first, no repeats."""
    nets = []
    for i in ifaces:
        try:
            n = str(ipaddress.ip_interface(i).network)
        except ValueError:
            continue
        if n not in nets:
            nets.append(n)
    return sorted(nets, key=lambda n: (":" in n, nets.index(n)))


def home_fs(home: Optional[str] = None) -> Optional[str]:
    """The type of the filesystem holding the home directory (``nfs4``,
    ``xfs``, ...), or None if it cannot be told."""
    home = os.path.realpath(home or os.path.expanduser("~"))
    if os.path.exists("/proc/self/mounts"):
        best, kind = "", None
        try:
            with open("/proc/self/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    mnt = parts[1].replace("\\040", " ")
                    if (home == mnt or home.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
                        best, kind = mnt, parts[2]
        except OSError:
            return None
        return kind
    out = _run(["df", "-P", home]).splitlines()
    if len(out) < 2:
        return None
    mnt = out[-1].split()[-1]
    for line in _run(["mount"]).splitlines():
        m = re.search(r" on (.+?) \(([^,)]+)", line)
        if m and m.group(1) == mnt:
            return m.group(2)
    return None


def shared(fs: Optional[str]) -> Optional[bool]:
    """True for a network filesystem, False for a local one, None if unknown."""
    if not fs:
        return None
    return fs.lower() in SHARED_FS or fs.lower().startswith(("nfs", "fuse.ceph"))
