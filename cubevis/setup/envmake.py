"""Setup's own environments on a node (remote session design 7c, stage 2c
step 3).

When no environment on a node has the same kernel-side code as this
computer, setup makes one there: a minimal conda environment named for the
cubevis version, ``<tool> create -y -p PREFIX python=X.Y pip`` and then
``PREFIX/bin/python -m pip install "cubevis[visplot]==VERSION"``.  Setup
never modifies it afterwards (a new cubevis version gets a new one) and
never installs into an environment of the user's.

Making one takes minutes, over a connection that may be a laptop's, so the
work runs **detached on the node** and setup only polls it:

* ``start`` writes a job and a status file under ``<config>/make/`` and
  spawns a worker (a session of its own, output to a log) that runs the
  steps; each finished step is recorded, so a worker started again for the
  same environment (after a failure, or a node restart) skips what is done;
* the **marker** (``<prefix>/cubevis-setup.json``: name, version, spec,
  Python, creating tool, which setup made it) is written *last*: an
  environment has one only when it is complete, and discovery
  (``discover.setup_marker``) recognises setup's own by it;
* ``status`` reads the status file, and says ``died`` when a worker that
  should be running is not (the node restarted, the process was killed);
* ``plan`` says, before anything is made, where an environment could go
  (writable? free space? names taken?) and what earlier jobs left;
* ``remove`` removes an environment **that has the marker**, with the
  tool's own ``env remove`` -- never anything else, never by deleting
  files itself.

Everything on the node is one standard-library Python program (3.6 and
later, like ``discover``), sent as ``python3 -`` with the request filled in
(``payload``); its answer is one line, ``CUBEVIS-ENVMAKE {json}``
(``call``).  Nothing needs cubevis there.

Test hooks (the environment of the setup run, never set in normal use):
``CUBEVIS_SETUP_TEST_INSTALL`` -- a shell command that replaces the pip
install (``$PREFIX`` is the new environment) and allows a development
version here; ``CUBEVIS_SETUP_POLL_SECONDS`` -- the polling interval.
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Optional

from .remote import RemoteError

MARKER_FILE = "cubevis-setup.json"      # discover.SETUP_MARKER_FILE
ANSWER = "CUBEVIS-ENVMAKE "
EXPECTED_SIZE = "about 1.5 GB"          # pip's part measured 2026-10-28: 965 MB (Linux, Python 3.12)
MIN_FREE = 3 * 1024 ** 3                # below this, setup warns before making one there
CALL_TIMEOUT = 120
TOOL_ORDER = ("conda", "mamba", "micromamba")


def base_name(version: str) -> str:
    """The environment's name for cubevis *version*: ``cubevis-1.0.97``."""
    return "cubevis-" + re.sub(r"[^A-Za-z0-9_.-]", "-", version)


def candidate_names(version: str) -> list:
    """The names tried, in order: ``cubevis-1.0.97``, ``-2``, ... (a name
    that is taken -- by anything -- is not reused)."""
    b = base_name(version)
    return [b] + [f"{b}-{i}" for i in range(2, 10)]


def spec(version: str) -> str:
    return f"cubevis[visplot]=={version}"


def pick_tool(tools) -> Optional[dict]:
    """The conda tool to make environments with: conda, mamba, then
    micromamba (discovery's order within each)."""
    for name in TOOL_ORDER:
        for t in tools or []:
            if t.get("name") == name:
                return t
    return None


def human_bytes(n) -> str:
    if n is None:
        return "unknown"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB", "MB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# --------------------------------------------------------------------------
# The detached worker (its own interpreter: ``python3 -c WORKER JOBFILE``)
# --------------------------------------------------------------------------

WORKER = r'''
import fcntl, json, os, re, shutil, subprocess, sys, tempfile, time

class Failed(Exception):
    pass

job = json.load(open(sys.argv[1]))
STATUS, PREFIX, TOOL = job["status"], job["prefix"], job["tool"]
PY = os.path.join(PREFIX, "bin", "python")
log = open(job["log"], "ab")

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")

def put(**kw):
    try:
        with open(STATUS) as f:
            cur = json.load(f)
    except (OSError, ValueError):
        cur = {}
    cur.update(kw)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATUS), prefix=".status.")
    with os.fdopen(fd, "w") as f:
        json.dump(cur, f)
    os.replace(tmp, STATUS)

def say(text):
    log.write((text + "\n").encode())
    log.flush()

env = dict(os.environ)
env.update(PYTHONNOUSERSITE="1", PIP_NO_INPUT="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PREFIX=PREFIX)
for k in ("PIP_REQUIRE_VIRTUALENV", "PYTHONHOME", "PYTHONPATH"):
    env.pop(k, None)

def run(step, cmd, shell=False, extra=None):
    """Run *cmd*; its exit code.  ``LAST`` is what it printed."""
    global LAST
    put(step=step)
    say("$ " + (cmd if shell else " ".join(cmd)))
    start = log.tell()
    e = dict(env)
    e.update(extra or {})
    rc = subprocess.call(cmd, shell=shell, stdin=subprocess.DEVNULL, stdout=log,
                         stderr=subprocess.STDOUT, env=e)
    say("-> exit %d" % rc)
    try:
        with open(job["log"], "rb") as f:
            f.seek(start)
            LAST = f.read().decode("utf-8", "replace")
    except OSError:
        LAST = ""
    return rc

LAST = ""

def job_reason():
    try:
        with open(STATUS) as f:
            return json.load(f).get("reason")
    except (OSError, ValueError):
        return None

def reason(text):
    """The line that says why: the last error line of what a command printed."""
    found = [l.strip() for l in text.splitlines()
             if re.match(r"\s*(ERROR:|([A-Za-z_.]+\.)?[A-Za-z]*(Error|Exception)\b)", l)]
    return found[-1][:300] if found else None

def locks_work(d):
    """Can a file in *d* be locked?  Some shared filesystems (Lustre mounted
    without flock, NFS without a lock daemon) answer "No locks available";
    conda locks its package cache and gives up."""
    try:
        with open(os.path.join(d, ".lock-test"), "w") as f:
            fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.unlink(os.path.join(d, ".lock-test"))
        return True
    except OSError:
        return False

def scratch_pkgs():
    """A fresh package cache where locking works and there is room, or None."""
    for base in (os.environ.get("TMPDIR"), "/tmp", "/var/tmp", "/dev/shm"):
        try:
            if not base or not os.path.isdir(base):
                continue
            st = os.statvfs(base)
            if st.f_bavail * st.f_frsize < 2 * 1024 ** 3:
                continue
            d = tempfile.mkdtemp(prefix="cubevis-pkgs-", dir=base)
        except OSError:
            continue
        if locks_work(d):
            return d
        shutil.rmtree(d, ignore_errors=True)
    return None

done = list(job.get("done") or [])
try:
    put(state="running", pid=os.getpid(), step="starting", started=now(), error=None, reason=None)
    if "create" not in done or not os.path.exists(PY):
        if os.path.lexists(PREFIX):           # what an unfinished earlier try left
            if run("removing an unfinished environment", [TOOL, "env", "remove", "-y", "-p", PREFIX]) \
                    or os.path.lexists(PREFIX):
                raise Failed("cannot remove the unfinished environment %s" % PREFIX)
            done = []
        cmd = [TOOL, "create", "-y", "-p", PREFIX, "python=" + job["python"], "pip"]
        step = "creating the environment (conda)"
        rc = run(step, cmd)
        if rc and re.search(r"No locks available|Errno 37|ENOLCK", LAST):
            # The package cache is on a filesystem without file locking: the
            # packages for this one environment go through a scratch cache.
            pkgs = scratch_pkgs()
            if pkgs:
                say("the package cache cannot be locked here; using %s for this" % pkgs)
                put(step=step + ", package cache in " + pkgs)
                if os.path.lexists(PREFIX):
                    run("removing an unfinished environment", [TOOL, "env", "remove", "-y", "-p", PREFIX])
                try:
                    rc = run(step, cmd, extra={"CONDA_PKGS_DIRS": pkgs})
                finally:
                    shutil.rmtree(pkgs, ignore_errors=True)
            else:
                put(reason="its package cache is on a filesystem that cannot lock files (No locks "
                           "available), and there is no local scratch directory with room for another")
        if rc:
            if not job_reason():
                put(reason=reason(LAST))
            raise Failed("%s create failed" % os.path.basename(TOOL))
        done.append("create")
        put(done=done)
    if "install" not in done:
        if job.get("install_shell"):
            rc = run("installing cubevis (pip)", job["install_shell"], shell=True)
        else:
            rc = run("installing cubevis (pip)", [PY, "-m", "pip", "install", job["spec"]])
        if rc:
            put(reason=reason(LAST))
            raise Failed("pip install %s failed" % job["spec"])
        done.append("install")
        put(done=done)
    marker = {"schema": 1, "name": job["name"], "version": job["version"], "spec": job["spec"],
              "python": job["python"], "tool": TOOL, "created": now(), "setup": job.get("setup") or {}}
    fd, tmp = tempfile.mkstemp(dir=PREFIX, prefix=".marker.")
    with os.fdopen(fd, "w") as f:
        json.dump(marker, f, indent=1)
    os.replace(tmp, os.path.join(PREFIX, job["marker"]))          # last
    put(state="ok", step="done", finished=now())
except Failed as exc:
    say("FAILED: %s" % exc)
    put(state="failed", error=str(exc), finished=now())
except Exception as exc:
    say("FAILED: %s: %s" % (type(exc).__name__, exc))
    put(state="failed", error="%s: %s" % (type(exc).__name__, exc), finished=now())
'''


# --------------------------------------------------------------------------
# The program setup sends to the node (``python3 -``)
# --------------------------------------------------------------------------

NODE_PROGRAM = r'''
import json, os, subprocess, sys, tempfile, time

def answer(**kw):
    sys.stdout.write("CUBEVIS-ENVMAKE " + json.dumps(kw) + "\n")
    sys.stdout.flush()

def make_dir():
    return os.path.join(REQUEST["config_dir"], "make")

def paths(name):
    d = make_dir()
    return {"status": os.path.join(d, name + ".json"), "job": os.path.join(d, name + ".job.json"),
            "log": os.path.join(d, name + ".log")}

def read_json(p):
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None

def alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, TypeError, ValueError):
        return True if pid else False
    return True

def tail(p, n=12):
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return lines[-n:]
    except OSError:
        return []

def activity_of(pid, log):
    """What the worker and what it started are doing now, from the system
    itself (Linux ``/proc``; anything missing is left out): the log's size
    and age; and, over the worker's live descendants, CPU seconds used,
    bytes read and written, process names, and how many are in
    uninterruptible disk wait (state D -- what a stalled shared filesystem
    looks like).  Counters only grow while the work does, so setup compares
    two answers; they are not a measure of progress, only of life."""
    out = {}
    try:
        st = os.stat(log)
        out["log_size"], out["log_age"] = st.st_size, max(0, int(time.time() - st.st_mtime))
    except OSError:
        pass
    try:
        tick = float(os.sysconf("SC_CLK_TCK"))
        procs, kids = {}, {}
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % d) as f:
                    raw = f.read()
                rest = raw[raw.rindex(")") + 2:].split()
                procs[int(d)] = (raw[raw.index("(") + 1:raw.rindex(")")], rest[0],
                                 sum(int(x) for x in rest[11:15]) / tick)
                kids.setdefault(int(rest[1]), []).append(int(d))
            except (OSError, ValueError, IndexError):
                continue
        mine, todo = [], [int(pid)]
        while todo:
            q = todo.pop()
            if q in procs:
                mine.append(q)
            todo.extend(kids.get(q, []))
        io = 0
        for q in mine:
            try:
                with open("/proc/%d/io" % q) as f:
                    for line in f:
                        if line.startswith(("rchar:", "wchar:")):
                            io += int(line.split()[1])
            except (OSError, ValueError):
                pass
        names = []
        for q in mine:
            if q != int(pid) and procs[q][0] not in ("sh", "bash", "csh", "tcsh", "zsh") \
                    and procs[q][0] not in names:
                names.append(procs[q][0])
        out.update(cpu=round(sum(procs[q][2] for q in mine), 2), io=io, procs=names[:4],
                   disk_wait=sum(1 for q in mine if procs[q][1] == "D"), alive=len(mine))
    except (OSError, ValueError, AttributeError):
        pass
    return out

def status_of(name):
    p = paths(name)
    st = read_json(p["status"])
    if st is None:
        return None
    if st.get("state") in ("running", "starting"):
        age = time.time() - os.path.getmtime(p["status"])
        if st.get("pid"):
            if not alive(st["pid"]):
                st["state"], st["error"] = "died", "the process making it is gone (the node restarted?)"
        elif age > 60:
            st["state"], st["error"] = "died", "it never started"
    st["name"] = name
    st["log"] = p["log"]
    st["log_tail"] = tail(p["log"])
    if st["state"] in ("running", "starting") and st.get("pid"):
        st["activity"] = activity_of(st["pid"], p["log"])
    return st

def nearest(d):
    d = os.path.abspath(d)
    while d and not os.path.exists(d):
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return d

def dir_facts(d):
    e = nearest(d)
    try:
        sv = os.statvfs(e)
        free = sv.f_bavail * sv.f_frsize
    except OSError:
        free = None
    return {"dir": d, "exists": os.path.isdir(d), "free": free,
            "writable": os.path.isdir(e) and os.access(e, os.W_OK | os.X_OK),
            "taken": [n for n in REQUEST.get("names") or [] if os.path.lexists(os.path.join(d, n))]}

def act_plan():
    jobs = {}
    for n in REQUEST.get("names") or []:
        st = status_of(n)
        if st:
            jobs[n] = st
    answer(ok=True, dirs=[dir_facts(d) for d in REQUEST.get("dirs") or []], jobs=jobs,
           python=sys.executable)

def act_status():
    answer(ok=True, status=status_of(REQUEST["name"]))

def act_start():
    name, prefix, tool = REQUEST["name"], REQUEST["prefix"], REQUEST["tool"]
    if not os.path.isabs(prefix) or os.path.basename(prefix.rstrip("/")) != name:
        return answer(ok=False, error="the prefix must be an absolute path ending in the name")
    if not (os.path.isfile(tool) and os.access(tool, os.X_OK)):
        return answer(ok=False, error="%s is not an executable there" % tool)
    if not os.access(nearest(os.path.dirname(prefix)), os.W_OK | os.X_OK):
        return answer(ok=False, error="cannot write in %s" % os.path.dirname(prefix))
    old = status_of(name)
    done = []
    if old and old.get("state") in ("running", "starting"):
        return answer(ok=True, started=False, status=old)             # already on its way
    if os.path.lexists(prefix):
        # Only what an earlier try of setup's own left may be continued (or
        # removed by the worker); anything else there is not ours.
        if not (old and old.get("prefix") == prefix and old.get("state") in ("failed", "died")):
            return answer(ok=False, error="%s exists already" % prefix)
        done = old.get("done") or []
    p = paths(name)
    os.makedirs(make_dir(), mode=0o700, exist_ok=True)
    job = {"name": name, "prefix": prefix, "tool": tool, "python": REQUEST["python"],
           "spec": REQUEST["spec"], "version": REQUEST["version"], "marker": REQUEST["marker"],
           "setup": REQUEST.get("setup") or {}, "install_shell": REQUEST.get("install_shell"),
           "done": done, "status": p["status"], "log": p["log"]}
    with open(p["job"], "w") as f:
        json.dump(job, f)
    with open(p["status"], "w") as f:
        json.dump({"state": "starting", "step": "starting", "prefix": prefix, "tool": tool,
                   "version": REQUEST["version"], "done": done, "queued": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, f)
    with open(p["log"], "ab") as f:
        f.write(("---- %s\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode())
    log = open(p["log"], "ab")
    subprocess.Popen([sys.executable, "-c", WORKER, p["job"]], stdin=subprocess.DEVNULL,
                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    answer(ok=True, started=True, status=status_of(name))

def act_remove():
    prefix, tool = REQUEST["prefix"], REQUEST["tool"]
    marker = read_json(os.path.join(prefix, REQUEST["marker"]))
    if not isinstance(marker, dict) or marker.get("schema") != 1:
        return answer(ok=False, error="%s was not made by cubevis setup (no marker): not removed" % prefix)
    if os.path.basename(prefix.rstrip("/")) != marker.get("name"):
        return answer(ok=False, error="%s is not named as its marker says: not removed" % prefix)
    if not (os.path.isfile(tool) and os.access(tool, os.X_OK)):
        return answer(ok=False, error="%s is not an executable there" % tool)
    r = subprocess.run([tool, "env", "remove", "-y", "-p", prefix], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = r.stdout.decode("utf-8", "replace").strip().splitlines()[-6:]
    gone = not os.path.lexists(prefix)
    if gone:
        for n in (marker["name"],):
            for p in paths(n).values():
                try:
                    os.unlink(p)
                except OSError:
                    pass
    answer(ok=gone, removed=gone, rc=r.returncode, output=out,
           error=None if gone else "%s is still there after %s env remove" % (prefix, os.path.basename(tool)))

try:
    {"plan": act_plan, "status": act_status, "start": act_start, "remove": act_remove}[REQUEST["action"]]()
except Exception as exc:
    answer(ok=False, error="%s: %s" % (type(exc).__name__, exc))
'''


def request(action: str, config_dir: str, **fields) -> dict:
    """A request for ``call``: *action* (plan, start, status, remove), the
    node's configuration directory (where jobs and logs live), its fields."""
    return dict(action=action, config_dir=config_dir, marker=MARKER_FILE, **fields)


def payload(request: dict) -> str:
    """The program for the node: *request* (``action`` and its fields)
    filled in, with the worker's source."""
    return (f"import json\nREQUEST = json.loads({json.dumps(request)!r})\n"
            f"WORKER = {WORKER!r}\n" + NODE_PROGRAM)


def call(c, alias: str, python3: Optional[str], request: dict,
         timeout: float = CALL_TIMEOUT) -> dict:
    """Run *request* on the node *alias* through setup's ssh; the answer's
    dict (``ok`` and the action's fields).  RemoteError when there is none."""
    py = shlex.quote(python3) if python3 else "python3"
    r = c.ssh(alias, py, "-", timeout=timeout, input=payload(request))
    for line in (r.stdout or "").splitlines():
        if line.startswith(ANSWER):
            try:
                return json.loads(line[len(ANSWER):])
            except ValueError:
                break
    from .steps import last_line
    raise RemoteError(last_line(c.last_out) or f"ssh exit {r.returncode}")
