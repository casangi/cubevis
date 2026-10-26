# -*- coding: utf-8 -*-
"""What a host offers cubevis kernels -- the discovery script (remote
session design 7c).

Setup runs it on a node over ssh, as ``ssh NODE python3 -`` with this file
on stdin (``payload()``: this source with ``data/envcheck.py`` embedded), so
cubevis need not be installed there.  By hand on the node, read-only, it
prints what setup would receive:

    python3 -m cubevis.setup.discover          where cubevis is installed
    python3 discover.py                        the file setup leaves for that
                                               (python -m cubevis.setup.discover
                                               --bundle > discover.py, here)

It reports, changing nothing:

* the host: its name, full name and domain, the user, the home directory
  and the filesystem it is on, the configuration directory, the network
  addresses with their prefixes and subnets (candidate inside ranges),
  ``$SSH_CONNECTION``;
* the conda tools (conda, mamba, micromamba: on PATH, in ``$CONDA_EXE`` /
  ``$MAMBA_EXE``, in the usual install places) and where conda keeps
  environments (``envs_dirs``);
* the environments they list, the active ones, and the Python running this:
  each with its kind (conda, venv, python, from the files), whether cubevis
  is installed (its ``dist-info``, nothing run) and setup's marker file
  (``cubevis-setup.json``) if any; and, for those with cubevis (or given on
  the command line), ``envcheck``'s report from running its interpreter --
  versions, the kernel-side fingerprint, how cubevis was installed, the
  import error.

Options: paths of environments (a directory or an interpreter) to check as
well; ``--only`` checks just those; ``--no-check`` runs no interpreter;
``--json`` prints only the report line (``CUBEVIS-DISCOVER {json}``), which
is what setup reads.

It must run with whatever ``python3`` a node has -- RHEL 8's is 3.6 -- so:
standard library only, no ``from __future__ import annotations``, no
``subprocess.run(capture_output=, text=)`` (3.7), nothing newer than 3.6.
The interpretation of the reports (is it usable, how to upgrade it) is done
on the computer the plots are on (``envs``), not here.
"""
import glob
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile

VERSION = 1
MARKER = "CUBEVIS-DISCOVER "
SETUP_MARKER_FILE = "cubevis-setup.json"   # in an environment setup created (stage 2c step 3)
TOOLS = ("conda", "mamba", "micromamba")
CHECK_TIMEOUT = 300          # seconds: the first import from a cold NFS cache is slow
LIST_TIMEOUT = 60
COMMON_BASES = ("miniforge3", "mambaforge", "miniconda3", "anaconda3", "micromamba",
                ".local/share/mamba")

SHARED_FS = ("nfs", "nfs4", "lustre", "gpfs", "cifs", "smbfs", "smb3", "ceph", "cephfs",
             "beegfs", "glusterfs", "fuse.glusterfs", "fuse.sshfs", "afs", "panfs", "wekafs",
             "autofs")
# Bridges for containers and VMs: on every machine, never a site's network.
SKIP_RANGES = ("172.17.0.0/16", "192.168.122.0/24", "192.168.121.0/24")


def _run(cmd, timeout=10):
    """stdout of *cmd* ('' if it cannot run).  3.6: no capture_output/text."""
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           stdin=subprocess.DEVNULL, universal_newlines=True, timeout=timeout)
        return r.stdout
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


# --------------------------------------------------------------------------
# The host
# --------------------------------------------------------------------------

def fqdn():
    """The full host name (``socket.getfqdn``), as far as the system says."""
    name = socket.gethostname()
    try:
        full = socket.getfqdn(name)
    except OSError:
        full = name
    return full if "." in full else name


def domain_of(host):
    parts = (host or "").split(".")
    return ".".join(parts[1:]) if len(parts) > 1 else None


def interfaces():
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
                    ipaddress.ip_network("0.0.0.0/" + mask).prefixlen
                raw.append("%s/%d" % (m.group(1), bits))
            m6 = re.search(r"\binet6\s+([0-9a-f:]+)(?:%\S+)?\s+prefixlen\s+(\d+)", line)
            if m6:
                raw.append("%s/%s" % (m6.group(1), m6.group(2)))
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


def ranges_of(ifaces):
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


def home_fs(home=None):
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


def shared(fs):
    """True for a network filesystem, False for a local one, None if unknown."""
    if not fs:
        return None
    return fs.lower() in SHARED_FS or fs.lower().startswith(("nfs", "fuse.ceph"))


def config_dir():
    """``$XDG_CONFIG_HOME/cubevis``, or ``~/.config/cubevis`` (as setup's)."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "cubevis")


def host_facts():
    import getpass
    home = os.path.expanduser("~")
    full = fqdn()
    fs = home_fs(home)
    ifaces = interfaces()
    return {"hostname": socket.gethostname(), "fqdn": full, "domain": domain_of(full),
            "user": getpass.getuser(), "home": home, "home_fs": fs, "shared_home": shared(fs),
            "config_dir": config_dir(), "addresses": ifaces, "ranges": ranges_of(ifaces),
            "ssh_connection": os.environ.get("SSH_CONNECTION"),
            "system": platform.system(), "machine": platform.machine()}


# --------------------------------------------------------------------------
# Environments
# --------------------------------------------------------------------------

def norm(path):
    """Absolute and normalised, symbolic links kept: an environment is known
    by its interpreter's path, and a venv's ``bin/python`` is a link to the
    base interpreter (resolving it would lose the venv)."""
    return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


def interpreter(prefix):
    """``<prefix>/bin/python`` (or ``python3``) if it exists."""
    for name in ("python", "python3"):
        p = os.path.join(prefix, "bin", name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def cubevis_installed(prefix):
    """The version of cubevis installed in *prefix*, from its dist-info
    directory's name (nothing is run), or None."""
    for d in sorted(glob.glob(os.path.join(prefix, "lib", "python3*", "site-packages",
                                           "cubevis-*.dist-info"))):
        m = re.match(r"cubevis-(.+)\.dist-info$", os.path.basename(d))
        if m:
            return m.group(1)
    return None


def kind_of(prefix, listed_by=None):
    """The kind from the files (the check's own report decides, ``envs``)."""
    if os.path.isdir(os.path.join(prefix, "conda-meta")):
        return "conda"
    if os.path.isfile(os.path.join(prefix, "pyvenv.cfg")):
        return "venv"
    return "python"


def setup_marker(prefix):
    """Setup's marker file in an environment it created, or None."""
    p = os.path.join(prefix, SETUP_MARKER_FILE)
    try:
        with open(p) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"error": "not a JSON object"}
    except (OSError, ValueError):
        return None if not os.path.exists(p) else {"error": "unreadable"}


def conda_tools():
    """conda, mamba and micromamba executables found here: on PATH, named
    by ``$CONDA_EXE`` / ``$MAMBA_EXE``, or in the usual install places."""
    found = []
    for env in ("CONDA_EXE", "MAMBA_EXE"):
        if os.environ.get(env):
            found.append(os.environ[env])
    for name in TOOLS:
        w = shutil.which(name)
        if w:
            found.append(w)
    home = os.path.expanduser("~")
    for base in COMMON_BASES + ("/opt/conda",):
        root = base if base.startswith("/") else os.path.join(home, base)
        for name in TOOLS:
            found.append(os.path.join(root, "bin", name))
    out, seen = [], set()
    for f in found:
        if not (os.path.isfile(f) and os.access(f, os.X_OK)):
            continue
        real = os.path.realpath(f)
        if real in seen:
            continue
        seen.add(real)
        out.append(norm(f))
    return out


def _json_of(cmd, timeout=LIST_TIMEOUT):
    try:
        return json.loads(_run(cmd, timeout=timeout) or "{}")
    except ValueError:
        return {}


def conda_envs(tool):
    """Prefixes ``<tool> env list --json`` reports ([] if it fails)."""
    data = _json_of([tool, "env", "list", "--json"])
    envs = data.get("envs") if isinstance(data, dict) else None
    return [norm(e) for e in envs or [] if isinstance(e, str)]


def envs_dirs(tool):
    """Where *tool* creates named environments, first one first ([] if it
    cannot say).  conda and mamba: ``config --show envs_dirs``; micromamba
    keeps them in ``<root prefix>/envs``."""
    name = os.path.basename(tool)
    if name == "micromamba":
        data = _json_of([tool, "info", "--json"])
        root = data.get("base environment") or data.get("root_prefix") if isinstance(data, dict) else None
        dirs = data.get("envs directories") if isinstance(data, dict) else None
        if isinstance(dirs, list) and dirs:
            return [norm(d) for d in dirs if isinstance(d, str)]
        return [os.path.join(norm(root), "envs")] if isinstance(root, str) else []
    data = _json_of([tool, "config", "--show", "envs_dirs", "--json"])
    dirs = data.get("envs_dirs") if isinstance(data, dict) else None
    return [norm(d) for d in dirs or [] if isinstance(d, str)]


def candidates(tools=None):
    """Environments found here, as dicts ``{python, prefix, kind, tool,
    cubevis, how, marker}``: one per interpreter, in the order found."""
    out, seen = [], set()

    def add(prefix, how, tool=None):
        py = interpreter(prefix)
        if not py:
            return
        py = norm(py)
        if py in seen:
            return
        seen.add(py)
        k = kind_of(prefix, tool)
        out.append({"python": py, "prefix": norm(prefix), "kind": k,
                    "tool": tool if k == "conda" else None,
                    "cubevis": cubevis_installed(prefix), "how": how,
                    "marker": setup_marker(prefix)})

    if sys.prefix and sys.executable:
        add(sys.prefix, "running this")
    if os.environ.get("VIRTUAL_ENV"):
        add(os.environ["VIRTUAL_ENV"], "the active virtual environment")
    if os.environ.get("CONDA_PREFIX"):
        add(os.environ["CONDA_PREFIX"], "the active conda environment")
    # An environment several tools list goes with conda when conda lists it
    # (the usual way to manage one), else with the first tool that did.
    listed = {}
    order = []
    for tool in (conda_tools() if tools is None else tools):
        for prefix in conda_envs(tool):
            prev = listed.get(prefix)
            if prev is None:
                order.append(prefix)
            if prev is None or (os.path.basename(tool) == "conda"
                                and os.path.basename(prev) != "conda"):
                listed[prefix] = tool
    for prefix in order:
        add(prefix, os.path.basename(listed[prefix]) + " env list", listed[prefix])
    return out


def describe_path(text):
    """A candidate from a path given: an environment's directory or an
    interpreter.  None if it is neither."""
    p = norm(text)
    if os.path.isdir(p):
        py = interpreter(p)
        if not py:
            return None
        return {"python": norm(py), "prefix": p, "kind": kind_of(p), "tool": None,
                "cubevis": cubevis_installed(p), "how": "given", "marker": setup_marker(p)}
    if os.path.isfile(p) and os.access(p, os.X_OK):
        prefix = os.path.dirname(os.path.dirname(p))
        return {"python": p, "prefix": prefix, "kind": kind_of(prefix), "tool": None,
                "cubevis": cubevis_installed(prefix), "how": "given",
                "marker": setup_marker(prefix)}
    return None


# --------------------------------------------------------------------------
# Checking an environment: envcheck.py run with its interpreter
# --------------------------------------------------------------------------

def envcheck_source():
    """``envcheck.py``'s text: embedded by ``payload()`` (``ENVCHECK_SOURCE``),
    else next to this file in ``data/``; None if neither."""
    src = globals().get("ENVCHECK_SOURCE")
    if src:
        return src
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:                      # read from stdin without the payload
        return None
    for p in (os.path.join(here, "data", "envcheck.py"), os.path.join(here, "envcheck.py")):
        if os.path.isfile(p):
            with open(p) as f:
                return f.read()
    return None


def check(python, script, timeout=CHECK_TIMEOUT):
    """envcheck's report for *python*, or ``{"error": ...}`` when it gives
    none.  Run in an empty temporary directory (no planted modules), without
    ``PYTHONHOME``."""
    d = tempfile.mkdtemp(prefix="cubevis-envcheck-")
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    try:
        try:
            r = subprocess.run([python, "-c", script], cwd=d, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                               universal_newlines=True, timeout=timeout, env=env)
        except (FileNotFoundError, NotADirectoryError):
            return {"error": python + " does not exist", "error_kind": "missing"}
        except PermissionError:
            return {"error": python + " cannot be run (permission denied)",
                    "error_kind": "permission"}
        except subprocess.TimeoutExpired:
            return {"error": "no answer within %d s" % int(timeout), "error_kind": "timeout"}
        except OSError as exc:
            return {"error": "%s cannot be run: %s" % (python, exc), "error_kind": "oserror"}
    finally:
        shutil.rmtree(d, ignore_errors=True)
    for line in (r.stdout or "").splitlines():
        if line.startswith("CUBEVIS-ENVCHECK "):
            try:
                return json.loads(line[len("CUBEVIS-ENVCHECK "):])
            except ValueError:
                break
    last = [l for l in (r.stderr or "").strip().splitlines() if l]
    return {"error": (last[-1] if last else "exit %d" % r.returncode) + " (is it Python 3?)",
            "error_kind": "no-report"}


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------

def discover(paths=(), only=False, run_checks=True):
    """The report (module docstring)."""
    rep = {"discover": VERSION, "python": "%d.%d.%d" % tuple(sys.version_info[:3]),
           "python_exe": sys.executable, "errors": []}
    try:
        rep["host"] = host_facts()
    except Exception as exc:                    # never lose the rest for it
        rep["host"] = {}
        rep["errors"].append("host facts: %s" % exc)
    tools = conda_tools()
    rep["tools"] = [{"path": t, "name": os.path.basename(t)} for t in tools]
    rep["envs_dirs"] = {}
    for t in tools:
        if os.path.basename(t) in ("conda", "mamba", "micromamba"):
            rep["envs_dirs"][t] = envs_dirs(t)
    cands = [] if only else candidates(tools)
    seen = {c["python"] for c in cands}
    for p in paths:
        c = describe_path(p)
        if c is None:
            rep["errors"].append("%s: not an environment or a python here" % p)
            continue
        if c["python"] in seen:
            for x in cands:
                if x["python"] == c["python"]:
                    x["given"] = True
            continue
        c["given"] = True
        seen.add(c["python"])
        cands.append(c)
    script = envcheck_source() if run_checks else None
    if run_checks and script is None:
        rep["errors"].append("envcheck.py is not here: environments were not checked "
                             "(use python -m cubevis.setup.discover --bundle for a "
                             "file that has it)")
    for c in cands:
        c["check"] = None
        if script and (c.get("cubevis") or c.get("given") or c.get("marker")):
            c["check"] = check(c["python"], script)
    rep["environments"] = cands
    return rep


def _tilde(p, home):
    p = str(p or "")
    return "~" + p[len(home):] if home and (p == home or p.startswith(home + "/")) else p


def describe(rep):
    """The report for a person (``python3 -m cubevis.setup.discover``)."""
    h = rep.get("host") or {}
    home = h.get("home") or ""
    out = ["%s (%s): user %s, home %s%s" % (
        h.get("fqdn") or h.get("hostname") or "?", h.get("system") or "?", h.get("user") or "?",
        home, " (%s%s)" % (h.get("home_fs"), ", shared" if h.get("shared_home") else "")
        if h.get("home_fs") else "")]
    if h.get("addresses"):
        out.append("  addresses " + ", ".join(h["addresses"]))
    out.append("  python3 here: %s (%s)" % (rep.get("python_exe"), rep.get("python")))
    if rep.get("tools"):
        for t in rep["tools"]:
            dirs = (rep.get("envs_dirs") or {}).get(t["path"]) or []
            out.append("  %s %s" % (t["name"], _tilde(t["path"], home))
                       + (", environments in " + ", ".join(_tilde(d, home) for d in dirs)
                          if dirs else ""))
    else:
        out.append("  no conda, mamba or micromamba found")
    out.append("environments:")
    if not rep.get("environments"):
        out.append("  none found")
    for c in rep.get("environments") or []:
        line = "  %-6s %s" % (c["kind"], _tilde(c["prefix"], home))
        bits = [c.get("how") or ""]
        if c.get("marker"):
            bits.append("made by cubevis setup")
        ch = c.get("check")
        if ch is None:
            bits.append("cubevis %s" % c["cubevis"] if c.get("cubevis") else "no cubevis")
        elif ch.get("error") and "versions" not in ch:
            bits.append("check failed: " + ch["error"])
        else:
            v = ch.get("versions") or {}
            bits.append("cubevis %s, Python %s" % (v.get("cubevis") or "-", ch.get("python")))
            bits.append("kernel side imports" if ch.get("kernel") else
                        "kernel side does not import: " + "; ".join(
                            "%s: %s" % kv for kv in sorted((ch.get("errors") or {}).items())))
            if ch.get("fingerprint"):
                bits.append("fingerprint " + ch["fingerprint"])
            inst = ch.get("install") or {}
            if inst.get("url"):
                bits.append("from " + inst["url"])
            elif inst.get("installer"):
                bits.append("installed by " + inst["installer"])
        out.append(line + "  (" + "; ".join(b for b in bits if b) + ")")
    for e in rep.get("errors") or []:
        out.append("note: " + e)
    return "\n".join(out)


def payload():
    """This script with ``envcheck.py`` embedded: what setup sends to a
    node's ``python3 -`` (and ``--bundle`` writes)."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "data", "envcheck.py")) as f:
        env_src = f.read()
    with open(os.path.abspath(__file__)) as f:
        src = f.read()
    head = ("# cubevis setup's discovery script (cubevis/setup/discover.py), with\n"
            "# envcheck.py embedded.  Read-only: python3 THIS-FILE [--json] [PATHS]\n"
            "ENVCHECK_SOURCE = %r\n" % env_src)
    return head + src


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if "--bundle" in args:
        sys.stdout.write(payload())
        return 0
    as_json = "--json" in args
    only = "--only" in args
    run_checks = "--no-check" not in args
    paths = [a for a in args if not a.startswith("--")]
    if not as_json:
        sys.stderr.write("looking at this computer (read-only) ...\n")
    rep = discover(paths, only=only, run_checks=run_checks)
    if as_json:
        sys.stdout.write(MARKER + json.dumps(rep) + "\n")
    else:
        sys.stdout.write(describe(rep) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
