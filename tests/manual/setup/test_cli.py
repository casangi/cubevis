"""``python -m cubevis.setup`` (remote session design, stage 2; ``setup
HOST``: design 7c).

    pytest tests/manual/setup/test_cli.py
    CUBEVIS_SSH_RIG=1 CUBEVIS_TEST_KERNEL_PYTHON=/path/to/python \\
        pytest tests/manual/setup/test_cli.py           # + the rig

The rig: ``sudo devel/tools/ssh_rig.sh`` (a disposable container or VM only):
a password login host, and a password node under two names
(``compute``, ``compute2``: one sshd), as the user alice.  The tests that
log in as alice give her, on the "node", a fake ``~/miniforge3/bin/conda``
listing ``envs/hrs`` (a venv with ``conda-meta`` that sees
``CUBEVIS_TEST_KERNEL_PYTHON``'s packages: the same cubevis as here),
``envs/plain`` (no cubevis) and ``envs/alt`` (a copy of cubevis whose
kernel-side code differs).

Without the rig: status, refusals, the earlier layout's files being left
alone, unattended runs never looping.  With it, ``setup HOST`` driven
through a real terminal (pexpect) as a user would answer it, then
``check`` and ``status``; a host reached directly, a second kernel, a
rerun; environments refused and the mismatch override; a cluster and a
second node of it; two computers sharing one configuration directory; a
csh login shell; a real kernel started through the launcher.

Each test plays the computer ``CUBEVIS_HOST_ID=gui1`` (``gui2`` too, where
two are needed), with its sockets in the test's own directory.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

import pytest

from cubevis.setup import sshgen as G
from cubevis.setup import state as S

needs_rig = pytest.mark.skipif(os.environ.get("CUBEVIS_SSH_RIG") != "1",
                               reason="set CUBEVIS_SSH_RIG=1 with the ssh test rig running (devel/tools/ssh_rig.sh)")
needs_ssh = pytest.mark.skipif(not shutil.which("ssh") or not shutil.which("perl"),
                               reason="needs ssh and perl")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
KERNEL_PY = os.environ.get("CUBEVIS_TEST_KERNEL_PYTHON")
needs_kernel_py = pytest.mark.skipif(not KERNEL_PY, reason="set CUBEVIS_TEST_KERNEL_PYTHON")

HOST = "compute.example.test:2202"
LOGIN = ["--answer", "via_login=y", "--answer", "login_host=login.example.test:2201",
         "--answer", "login_user=alice", "--answer", "user=alice"]
DIRECT = ["--answer", "via_login=n", "--answer", "user=alice"]
STORE = ["--answer", "store=command", "--answer", "store_command=echo 's3cret pw!'"]


@pytest.fixture
def home(monkeypatch):
    d = tempfile.mkdtemp(prefix="cvc", dir="/tmp")         # short: ControlPath sockets
    monkeypatch.setenv("XDG_CONFIG_HOME", d)
    monkeypatch.setenv("CUBEVIS_HOST_ID", "gui1")
    monkeypatch.setenv("CUBEVIS_RUNTIME_DIR", f"{d}/run")
    monkeypatch.setenv("CUBEVIS_SETUP_KNOWN_HOSTS", f"{d}/known_hosts")   # not ~/.ssh
    yield d
    for host in ("gui1", "gui2"):
        cfg = os.path.join(d, "cubevis", "hosts", host, "ssh", "config")
        if os.path.exists(cfg):
            env = dict(os.environ, CUBEVIS_RUNTIME_DIR=f"{d}/run" if host == "gui1"
                       else f"{d}/run-{host}")
            aliases = [l.split()[1] for l in open(cfg) if l.startswith("Host ") and "*" not in l]
            for a in aliases:
                subprocess.run(["ssh", "-F", cfg, "-O", "exit", a], capture_output=True, env=env)
    shutil.rmtree(d, ignore_errors=True)


def _env(home, **extra):
    env = dict(os.environ, XDG_CONFIG_HOME=home, NO_COLOR="1",
               PYTHONPATH=os.pathsep.join([ROOT] + [p for p in sys.path if p]))
    env.pop("SSH_WRAPPER_RULES", None)
    env.pop("SSH_WRAPPER_DEBUG", None)       # its log is in the real ~/.ssh
    env.pop("CUBEVIS_ALLOW_MISMATCH", None)
    env["CUBEVIS_SETUP_ALLOW_PLAIN_COMMAND"] = "1"      # the rig's password: echo
    env.update(extra)
    return env


def _run(home, *args, input=None, timeout=300, host=None, **extra):
    # Another computer: its own name and its own local disk for sockets (on
    # one machine, it would otherwise ride on the first one's connections).
    if host:
        extra.update(CUBEVIS_HOST_ID=host, CUBEVIS_RUNTIME_DIR=f"{home}/run-{host}")
    return subprocess.run([sys.executable, "-m", "cubevis.setup", *args], capture_output=True,
                          text=True, env=_env(home, **extra), timeout=timeout,
                          input=input if input is not None else "")


def _flat(r):
    return " ".join(r.stdout.split())             # messages are wrapped


def test_help_and_empty_status(home):
    r = _run(home, "--help")
    assert r.returncode == 0 and "network" in r.stdout and "check" in r.stdout
    assert "HOST" in r.stdout
    r = _run(home, "status")
    assert r.returncode == 0 and "nothing set up yet" in r.stdout
    assert not os.path.exists(os.path.join(home, "cubevis", "state.json"))


def test_import_is_light():
    """The ssh "inside" test runs at every connection: no visplot, no bokeh;
    setup itself imports none of them either."""
    code = ("import sys, cubevis.setup.inside, cubevis.setup.__main__; "
            "print(any(m.startswith(('bokeh', 'cubevis.toolbox', 'dask')) for m in sys.modules))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=dict(os.environ, PYTHONPATH=ROOT))
    assert r.stdout.strip() == "False", r.stderr


def test_unknown_network_bad_state_and_arguments(home):
    r = _run(home, "network", "nope")
    assert r.returncode == 1 and "no network 'nope'" in r.stdout
    r = _run(home, "--kernel", "k", "status")
    assert r.returncode == 2 and "--kernel goes with a host" in r.stderr
    r = _run(home, "--yes", "bad host!")
    assert r.returncode == 1 and "does not look like one" in r.stdout
    os.makedirs(os.path.join(home, "cubevis"), exist_ok=True)
    with open(os.path.join(home, "cubevis", "state.json"), "w") as f:
        f.write("{broken")
    r = _run(home, "status")
    assert r.returncode == 1 and "cannot be read" in r.stdout


@needs_ssh
def test_unattended_failure_skips_and_old_layout_is_left_alone(home):
    """--yes on a host that cannot be reached: the step fails once and is
    skipped (no retry loop); the node is kept, flagged.  The earlier
    layout's ssh/config (here the bash setup's) is mentioned, not touched:
    another computer sharing the directory may still use it."""
    sshd = os.path.join(home, "cubevis", "ssh")
    os.makedirs(sshd)
    old = "# Written by cubevis-setup from ~/.config/cubevis/hosts/*.conf\nHost old\n"
    with open(os.path.join(sshd, "config"), "w") as f:
        f.write(old)
    r = _run(home, "--yes", "--answer", "via_login=n", "--answer", "user=jo",
             "nosuch.example.invalid", timeout=120)
    out = _flat(r)
    assert "not used any more" in out and "cubevis/ssh/config" in out
    assert open(os.path.join(sshd, "config")).read() == old
    assert "FAIL" in out and "cannot find" in out
    assert r.returncode == 1
    st = S.load()
    assert st.nodes()["nosuch"]["status"] == "flagged"
    assert list(st.networks()) == ["example"]                 # named from the domain
    assert st.networks()["example"]["domain"] == "example.invalid"
    assert "example-nosuch" in open(G.config_path()).read()
    assert str(G.config_path()).endswith("cubevis/hosts/gui1/ssh/config")


def test_bad_names_are_refused(home):
    r = _run(home, "--yes", "--answer", "via_login=y", "--answer", "login_host=gw.example.org",
             "--answer", "login_user=j o", "node1.example.org", timeout=60)
    assert r.returncode == 1 and "user name 'j o'" in r.stdout
    assert not S.load().networks()


# ---------------------------------------------------------------------- #
# The rig: alice's environments on the "node"                              #
# ---------------------------------------------------------------------- #

def _alice_ok():
    import pwd
    try:
        pwd.getpwnam("alice")
    except KeyError:
        return False
    return os.geteuid() == 0


needs_alice = pytest.mark.skipif(not _alice_ok(), reason="needs root and the rig's user alice")


def _as_alice(cmd, timeout=300):
    return subprocess.run(["su", "-", "alice", "-c", cmd], capture_output=True, text=True,
                          timeout=timeout)


ALICE_ENVS = r'''
set -e
rm -rf ~/miniforge3 ~/.config/cubevis && mkdir -p ~/miniforge3/bin ~/miniforge3/envs ~/visplot-data
python3 -m venv --without-pip ~/miniforge3/envs/plain
cat > ~/miniforge3/bin/conda <<'C'
#!/bin/sh
case "$*" in
  "env list --json") printf '{"envs": ["%s/miniforge3"' "$HOME"
                     for e in "$HOME"/miniforge3/envs/*; do printf ', "%s"' "$e"; done; echo ']}';;
  "config --show envs_dirs --json") printf '{"envs_dirs": ["%s/miniforge3/envs"]}\n' "$HOME";;
  *) echo "fake conda: $*" >&2; exit 1;;
esac
C
chmod +x ~/miniforge3/bin/conda
if [ -n "$KPY" ]; then
  KSITE=$("$KPY" -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
  for e in hrs alt; do
    "$KPY" -m venv --without-pip ~/miniforge3/envs/$e
    mkdir -p ~/miniforge3/envs/$e/conda-meta
    SITE=$(~/miniforge3/envs/$e/bin/python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
    cp -r "$KSITE"/cubevis-*.dist-info "$SITE"/
    if [ $e = hrs ]; then src="$ROOT"; else
      src=~/alt-src; rm -rf $src; mkdir -p $src; cp -r "$ROOT/cubevis" $src/
      echo "ALT_CHANGE = 1" >> $src/cubevis/remote/worker_main.py
    fi
    printf '%s\n%s\n' "$src" "$KSITE" > "$SITE/kernel.pth"
    rm -f "$SITE/cubevis.pth"
  done
fi
'''


@pytest.fixture(scope="module")
def alice_envs():
    if not _alice_ok():
        pytest.skip("needs root and the rig's user alice")
    r = _as_alice(f"KPY={shlex.quote(KERNEL_PY or '')} ROOT={shlex.quote(ROOT)} "
                  f"bash -c {shlex.quote(ALICE_ENVS)}")
    assert r.returncode == 0, r.stderr
    return "/home/alice/miniforge3/envs"


def _host(home, *answers, host=HOST, kernel=None, route=LOGIN, who=None, **extra):
    args = ["--yes", *route, *STORE, "--answer", "inside=127.0.0.0/8", *answers, host]
    if kernel:
        args += ["--kernel", kernel]
    return _run(home, *args, host=who, **extra)


# ---------------------------------------------------------------------- #
# The rig, through a real terminal                                         #
# ---------------------------------------------------------------------- #

def _spawn(home, *args):
    pexpect = pytest.importorskip("pexpect")
    return pexpect.spawn(sys.executable, ["-m", "cubevis.setup", *args], env=_env(home),
                         encoding="utf-8", timeout=120, dimensions=(50, 200))


@needs_rig
@needs_ssh
@needs_kernel_py
def test_rig_interactive_host(home, alice_envs):
    import pexpect
    p = _spawn(home, HOST)
    script = [
        (r"Go ahead\?", ""),                                         # the wrapper
        (r"intermediate host before logging in to compute.example.test\? \[Y/n\]", "y"),
        (r"The intermediate host's name", "login.example.test:2201"),
        (r"Your user name on login.example.test", "alice"),
        (r"Your user name on compute.example.test", ""),             # alice (default)
        (r"Store your password for alice@login.example.test that way", "y"),
        (r"Which store\?", "command"),
        (r"Command", "echo 's3cret pw!'"),
        (r"Name for the stored entry", ""),
        (r"Press Enter when it is stored", ""),
        (r"Store your password for alice@compute.example.test that way", "y"),
        (r"same password as for login host example-login\?", "y"),
        (r"cluster's nodes\? \[y/N\]", ""),
        (r"\[u\]se these, \[c\]hange them", "c"),
        (r"Ranges \(CIDR", "127.0.0.0/8"),
        (r"Environment \[1\]", ""),
        (r"A name for the kernel \[compute\]", "tw-hya"),
        (r"Working directory \[~\]", "~/visplot-data"),
        (r"Variables \('none' for none\)", "OMP_NUM_THREADS=2"),
    ]
    for pat, ans in script:
        p.expect(pat)
        p.sendline(ans)
    p.expect(r"Changed in this run")
    p.expect(pexpect.EOF)
    out = " ".join(p.before.split())
    p.close()
    assert p.exitstatus == 0, out
    st = S.load()
    net = st.networks()["example"]
    assert net["login"]["password"] and net["password"] and net["inside"] == ["127.0.0.0/8"]
    assert st.nodes()["compute"]["status"] == "ok" and st.nodes()["compute"]["standalone"]
    k = st.section("kernels")["tw-hya"]
    assert k["status"] == "ok" and k["workdir"] == "/home/alice/visplot-data"
    assert k["env"] == {"OMP_NUM_THREADS": "2"}
    assert k["launcher"] == "/home/alice/.config/cubevis/launch/gui1/tw-hya.sh"
    e = st.section("environments")[k["environment"]]
    assert e["python"] == f"{alice_envs}/hrs/bin/python" and e["hosts"] == ["compute"]
    from cubevis._fingerprint import fingerprint
    assert e["fingerprint"] == k["fingerprint"] == fingerprint()
    r = _run(home, "check")
    assert r.returncode == 0 and "all checks passed" in r.stdout, r.stdout
    assert "kernel tw-hya on compute: starts in /home/alice/visplot-data" in _flat(r)
    r = _run(home, "status")
    assert "this computer is inside: direct" in r.stdout
    assert "kernel tw-hya: environment hrs-compute, starts in ~/visplot-data" in r.stdout


@needs_rig
@needs_ssh
@needs_kernel_py
def test_rig_direct_host_a_second_kernel_and_a_rerun(home, alice_envs):
    """No login host; then --kernel adds a kernel without the environment
    questions; a rerun checks both (and changes nothing)."""
    r = _host(home, "--answer", "workdir=~", "--answer", "env_vars=none", route=DIRECT)
    assert r.returncode == 0, r.stdout
    assert "Inside ranges" not in r.stdout                   # no login host: nothing to decide
    text = open(G.config_path()).read()
    assert "ProxyJump" not in text and "Host example-compute" in text
    r = _host(home, "--answer", "kernel=", "--answer", "workdir=~/visplot-data",
              "--answer", "env_vars=A=1", kernel="hrs-imaging", route=DIRECT)
    assert r.returncode == 0, r.stdout
    assert "The Python environment for kernels" not in r.stdout
    st = S.load()
    assert sorted(st.section("kernels")) == ["compute", "hrs-imaging"]
    assert st.section("kernels")["compute"]["environment"] == \
        st.section("kernels")["hrs-imaging"]["environment"]
    r = _run(home, "--yes", HOST)
    out = _flat(r)
    assert r.returncode == 0, r.stdout
    assert "kernel compute: starts in ~" in out and "kernel hrs-imaging: starts in ~/visplot-data" in out
    assert "Changed in this run" not in r.stdout


@needs_rig
@needs_ssh
@needs_kernel_py
def test_rig_environments_refused_and_the_mismatch_override(home, alice_envs):
    """Listed with the reason: alt's kernel-side code differs.  Chosen, it
    is refused; with CUBEVIS_ALLOW_MISMATCH=1 it is used, and said so.  A
    path without cubevis is refused too."""
    r = _host(home, "--answer", "environment=~/miniforge3/envs/plain", route=DIRECT)
    out = _flat(r)
    assert r.returncode == 1 and "cubevis is not installed in it" in out, r.stdout
    assert "its kernel-side code differs" in out and "same kernel-side code as here" in out
    rows = [l for l in r.stdout.splitlines() if "miniforge3/envs/" in l]
    assert "envs/hrs" in rows[0]                             # usable first
    alt_n = [l.split(".")[0].split()[-1] for l in rows if "envs/alt" in l][0]
    r = _host(home, "--answer", f"environment={alt_n}", route=DIRECT)
    assert r.returncode == 1 and "cannot be used" in _flat(r)
    assert not S.load().section("kernels")
    r = _host(home, "--answer", f"environment={alt_n}", "--answer", "workdir=~",
              "--answer", "env_vars=none", route=DIRECT, CUBEVIS_ALLOW_MISMATCH="1")
    out = _flat(r)
    assert r.returncode == 0, r.stdout
    assert "used anyway: CUBEVIS_ALLOW_MISMATCH is set" in out
    k = S.load().section("kernels")["compute"]
    assert k["status"] == "ok" and S.load().section("environments")[k["environment"]]["prefix"] \
        .endswith("/envs/alt")
    r = _run(home, "check")                                   # without the override: flagged
    assert r.returncode == 1 and "its kernel-side code differs" in _flat(r)
    assert S.load().section("kernels")["compute"]["status"] == "flagged"


@needs_rig
@needs_ssh
@needs_kernel_py
def test_rig_a_cluster_and_another_of_its_nodes(home, alice_envs):
    """compute is set up as a node of cluster 'comp' (compute*); compute2
    then joins it without a question about the route, and finds the
    cluster's kernel there."""
    r = _host(home, "--answer", "cluster=y", "--answer", "cluster_name=comp",
              "--answer", "cluster_pattern=compute*", "--answer", "kernel=tw-hya",
              "--answer", "workdir=~", "--answer", "env_vars=none")
    assert r.returncode == 0, r.stdout
    st = S.load()
    assert st.section("clusters") == {"comp": {"network": "example", "pattern": "compute*"}}
    e = st.section("environments")[st.section("kernels")["tw-hya"]["environment"]]
    assert e["cluster"] == "comp" and e["hosts"] == []
    r = _run(home, "--yes", "compute2.example.test:2202")
    out = _flat(r)
    assert r.returncode == 0, r.stdout
    assert "added node: compute2 to cluster comp" in out
    assert "intermediate host" not in out and "cluster's nodes?" not in out
    assert "kernel tw-hya: starts in ~" in out
    st = S.load()
    assert st.nodes()["compute2"]["cluster"] == "comp" and st.nodes()["compute2"]["status"] == "ok"
    assert st.section("kernels")["tw-hya"]["checked_on"] == "compute2"
    assert "cluster comp: nodes compute* (set up: compute, compute2)" in _run(home, "status").stdout
    rules = open(G.rules_path()).read()
    assert "*.example.test" in rules


@needs_rig
@needs_ssh
def test_rig_two_computers_share_one_directory(home, alice_envs):
    """gui1 sets up a host; gui2, sharing the directory, sees it, chooses
    its own password store, and gets its own ssh files and wrapper; gui1's
    stay as they were.  Then each checks, and state.json holds one
    network."""
    r = _host(home, "--answer", "environment=none", route=DIRECT)  # logins only: no kernel
    gui1 = os.path.join(home, "cubevis", "hosts", "gui1")
    cfg1 = open(os.path.join(gui1, "ssh", "config")).read()
    rules1 = open(os.path.join(gui1, "ssh", "rules")).read()
    assert "backend=command" in rules1, r.stdout

    r = _run(home, "status", host="gui2")
    assert "computer   gui2" in r.stdout and "network example" in r.stdout
    r = _run(home, "--yes", *STORE, "--answer", "environment=none", HOST, host="gui2")
    out = _flat(r)
    assert "no password store chosen yet" in out
    assert "Log in to the node compute" in out and "ok Log in to the node compute" in out, r.stdout
    gui2 = os.path.join(home, "cubevis", "hosts", "gui2")
    assert os.path.exists(os.path.join(gui2, "bin", "ssh"))
    assert "Host example-compute" in open(os.path.join(gui2, "ssh", "config")).read()
    assert "backend=command" in open(os.path.join(gui2, "ssh", "rules")).read()
    assert open(os.path.join(gui1, "ssh", "config")).read() == cfg1
    assert open(os.path.join(gui1, "ssh", "rules")).read() == rules1

    for host in (None, "gui2"):                       # None: gui1, the fixture's
        r = _run(home, "check", host=host)
        assert r.returncode == 0 and "all checks passed" in r.stdout, (host, r.stdout)
    shared = json.load(open(os.path.join(home, "cubevis", "state.json")))
    assert list(shared["networks"]) == ["example"] and "local" not in shared
    for host in ("gui1", "gui2"):
        loc = json.load(open(os.path.join(home, "cubevis", "hosts", host, "local.json")))
        assert loc["local"]["store"] == "command"


@needs_rig
@needs_ssh
def test_rig_socket_directory_cleaned_away(home, alice_envs):
    """/tmp emptied (a reboot, a tmp cleaner) while setup is not running: the
    wrapper makes the socket directory again, closed to others, at the next
    login that needs a password."""
    _host(home, "--answer", "environment=none", route=DIRECT)
    assert S.load().nodes()["compute"]["status"] == "ok"
    subprocess.run(["ssh", "-F", str(G.config_path()), "-O", "exit", "example-compute"],
                   capture_output=True)
    shutil.rmtree(os.path.join(home, "run"))
    r = _run(home, "check")
    assert r.returncode == 0 and "all checks passed" in r.stdout, r.stdout
    import stat
    for d in ("run", "run/cm"):
        assert stat.S_IMODE(os.stat(os.path.join(home, d)).st_mode) == 0o700


@needs_rig
@needs_ssh
@needs_kernel_py
def test_rig_a_csh_login_shell(home, alice_envs):
    """Some accounts log in to tcsh: discovery, the launcher's installation
    and its test run go through it unchanged."""
    if not shutil.which("tcsh"):
        pytest.skip("needs tcsh")
    subprocess.run(["chsh", "-s", shutil.which("tcsh"), "alice"], check=True)
    try:
        r = _host(home, "--answer", "workdir=~", "--answer", "env_vars=X='a b'", route=DIRECT)
        assert r.returncode == 0, r.stdout
        assert "kernel compute on compute: starts in ~" in _flat(r)
        assert _run(home, "check").returncode == 0
    finally:
        subprocess.run(["chsh", "-s", "/bin/bash", "alice"], check=True)


@needs_rig
@needs_ssh
@needs_kernel_py
def test_rig_a_kernel_runs_through_the_launcher(home, alice_envs):
    """The design's claim (7b, 7c 4a): sshpyk takes the kernel's launcher as
    remote_python, so the kernel starts in its working directory with its
    variables -- here next to the test MS, opened by a relative path when
    it is there."""
    r = _host(home, "--answer", "kernel=tw-hya", "--answer", "workdir=~/visplot-data",
              "--answer", "env_vars=CUBEVIS_TRY=node", route=DIRECT)
    assert r.returncode == 0, r.stdout
    lp = S.load().section("kernels")["tw-hya"]["launcher"]
    os.makedirs(os.path.expanduser("~/.local/share/jupyter/runtime"), exist_ok=True)
    code = f'''
import asyncio, sys
from jupyter_client.kernelspec import KernelSpec, KernelSpecManager
from jupyter_client.manager import AsyncKernelManager
class Specs(KernelSpecManager):
    def get_kernel_spec(self, name):
        return KernelSpec(argv=[sys.executable, "-c", "pass"], display_name="t", language="python",
            metadata={{"kernel_provisioner": {{"provisioner_name": "sshpyk-provisioner", "config": {{
                "ssh": {str(G.wrapper_path())!r}, "ssh_config": {str(G.config_path())!r},
                "ssh_host_alias": "example-compute", "remote_python": {lp!r},
                "remote_kernel_name": "python3", "launch_timeout": 90}}}}}})
CODE = """import os
ms = 'sis14_twhya_calibrated_flagged.ms'
n = None
if os.path.isdir(ms):
    import xarray_ms, xarray as xr
    n = len(xr.open_datatree(ms, engine='xarray-ms:msv2').children)
print(os.getcwd(), os.environ.get('CUBEVIS_TRY'), n)"""
async def main():
    km = AsyncKernelManager(kernel_name="not-on-disk", kernel_spec_manager=Specs())
    await km.start_kernel()
    kc = km.client(); kc.start_channels(); await kc.wait_for_ready(timeout=60)
    mid = kc.execute(CODE)
    while True:
        m = await kc.get_iopub_msg(timeout=120)
        if m["parent_header"].get("msg_id") != mid:
            continue
        if m["msg_type"] == "stream" and m["content"].get("name") == "stdout":
            print("KERNEL:", m["content"]["text"].strip()); break
        if m["msg_type"] == "error":
            print("KERNEL-ERROR:", m["content"]["evalue"]); break
    kc.stop_channels(); await km.shutdown_kernel(now=True)
asyncio.run(main())
'''
    kenv_vars = {k: v for k, v in _env(home).items() if k != "PYTHONPATH"}   # its own Python
    r = subprocess.run([KERNEL_PY, "-c", code], capture_output=True, text=True, timeout=300,
                       env=kenv_vars, cwd="/tmp")
    assert "KERNEL: /home/alice/visplot-data node" in r.stdout, r.stdout[-2000:] + r.stderr[-3000:]
    if os.path.isdir("/home/alice/visplot-data/sis14_twhya_calibrated_flagged.ms"):
        n = r.stdout.split("KERNEL: /home/alice/visplot-data node")[1].split()[0]
        assert n.isdigit() and int(n) > 0, r.stdout


@needs_rig
@needs_ssh
def test_rig_inside_here_records_only_after_a_direct_connection(home, alice_envs):
    r = _run(home, "--yes", *LOGIN, *STORE, "--answer", "inside=none", "--answer", "environment=none",
             HOST)
    assert S.load().networks()["example"]["inside"] == [], r.stdout
    r = _run(home, "--yes", "network", "example", "--inside-here")
    assert r.returncode == 0 and "answers directly from here" in r.stdout, r.stdout
    assert S.load().networks()["example"]["inside"] == ["127.0.0.1/32"]
    assert "inside 127.0.0.1/32 -- this computer is inside: direct" in _run(home, "status").stdout
    # A node that does not answer directly: nothing recorded.
    st = S.load()
    st.add_node("far", network="example", hostname="compute.example.test", port=2299)
    st.networks()["example"]["inside"] = []
    st.networks()["example"]["default_node"] = "far"
    S.save(st)
    G.write(S.load())
    r = _run(home, "--yes", "network", "example", "--inside-here")
    out = _flat(r)
    assert r.returncode == 1 and "no direct connection from here" in out, r.stdout
    assert "Nothing recorded" in out and S.load().networks()["example"]["inside"] == []


@needs_rig
@needs_ssh
def test_rig_found_ranges_are_the_default_answer(home, alice_envs):
    """Users rarely know their site's numbers: what setup finds (the node's
    own subnets) is offered and taken by default -- nothing to type."""
    r = _run(home, "--yes", *LOGIN, *STORE, "--answer", "environment=none", HOST)
    from cubevis.setup.steps import node_ranges
    st = S.load()
    want = node_ranges(st, "example")             # the rig's: loopback, so no /16 guess
    out = _flat(r)
    if want:
        assert "Found, from compute:" in out and "[u]se these" in out, r.stdout
        assert st.networks()["example"]["inside"] == want
    else:
        assert "Nothing found to recognise the site by" in out, r.stdout
    assert "Give the site's address ranges" not in out


def test_jupyter_refuses_what_it_cannot_make(home):
    r = _run(home, "jupyter", "nope")
    assert r.returncode == 1 and "no kernel 'nope'" in r.stdout
    st = S.load()
    st.add_network("n", login_host=None, user="jo")
    st.add_node("a", network="n", hostname="a.example.org")
    st.add_environment("e", network="n", python="/e/bin/python", hosts=["a"])
    st.add_kernel("k", environment="e")
    S.save(st)
    r = _run(home, "jupyter", "k")
    assert r.returncode == 1 and "has no launcher yet" in _flat(r)
    r = _run(home, "jupyter", "k", "elsewhere")
    assert r.returncode == 1 and "not a node setup knows" in _flat(r)


@needs_rig
@needs_ssh
@needs_kernel_py
def test_rig_jupyter_kernel_and_a_remote_plotter(home, alice_envs, tmp_path):
    """The bridge until sessions: a Jupyter kernel definition for a setup
    kernel, and the call it prints -- the MS by its name in the kernel's
    working directory -- run headless (field Ceres: the whole MS's first
    scatter needs more memory than a small test machine has)."""
    r = _host(home, "--answer", "cluster=y", "--answer", "cluster_name=comp",
              "--answer", "cluster_pattern=compute*", "--answer", "kernel=tw-hya",
              "--answer", "workdir=~/visplot-data", "--answer", "env_vars=none", route=LOGIN)
    assert r.returncode == 0, r.stdout
    jd = str(tmp_path / "jupyter")
    r = _run(home, "jupyter", "tw-hya", JUPYTER_DATA_DIR=jd)
    assert r.returncode == 0, r.stdout
    assert 'kernel_name="cubevis-tw-hya"' in r.stdout and "visplot(ms=" in r.stdout
    spec = json.load(open(os.path.join(jd, "kernels", "cubevis-tw-hya", "kernel.json")))
    cfg = spec["metadata"]["kernel_provisioner"]["config"]
    assert cfg["ssh_host_alias"] == "example-compute" and cfg["ssh"] == str(G.wrapper_path())
    assert cfg["remote_python"] == "/home/alice/.config/cubevis/launch/gui1/tw-hya.sh"
    # Another node of the cluster, alongside: its own Jupyter kernel, the
    # node added on the way (with the port the cluster's nodes share).
    r = _run(home, "jupyter", "tw-hya", "compute2", JUPYTER_DATA_DIR=jd)
    assert r.returncode == 0 and 'kernel_name="cubevis-tw-hya-compute2"' in r.stdout, r.stdout
    two = json.load(open(os.path.join(jd, "kernels", "cubevis-tw-hya-compute2", "kernel.json")))
    assert two["metadata"]["kernel_provisioner"]["config"]["ssh_host_alias"] == "example-compute2"
    assert os.path.exists(os.path.join(jd, "kernels", "cubevis-tw-hya", "kernel.json"))
    assert S.load().nodes()["compute2"]["port"] == 2202
    if not os.path.isdir("/home/alice/visplot-data/sis14_twhya_calibrated_flagged.ms"):
        pytest.skip("the test MS is not in alice's ~/visplot-data")
    code = ("from cubevis.toolbox.visplot import VisibilityPlotter\n"
            "vp = VisibilityPlotter(ms='sis14_twhya_calibrated_flagged.ms', field='Ceres', "
            "backend='remote', kernel_name='cubevis-tw-hya', headless=True)\n"
            "print('PLOTTER', type(vp._reader).__name__)\n")
    env = {k: v for k, v in _env(home, JUPYTER_DATA_DIR=jd).items() if k != "PYTHONPATH"}
    r = subprocess.run([KERNEL_PY, "-c", code], capture_output=True, text=True, timeout=600,
                       env=env, cwd="/tmp")
    assert "PLOTTER RemoteReductionContext" in r.stdout, r.stdout[-2000:] + r.stderr[-3000:]
