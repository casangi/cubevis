"""Set up visplot with a remote kernel: your commands, and checks.

    python 00_remote_setup.py            # ask, then print the steps with your values
    python 00_remote_setup.py --check    # test each step (changes nothing)
    python 00_remote_setup.py --check --kernel   # also start the kernel and plot

Follows ``00_setup.md`` (read it first). Answers are kept in
``remote-setup/answers.json`` next to this file, with the files to copy into
place: ``ssh_config.txt`` (for ``~/.ssh/config``), ``rules.txt`` (for
``~/.config/ssh-wrapper/rules``) and ``steps.md`` (the guide with your names).
This script never edits your ssh configuration or your keychain itself.

Uses only the standard library, so it runs before the environment exists.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "remote-setup")
ANSWERS = os.path.join(OUT, "answers.json")
CUBEVIS_RELEASE = "1.0.95"
DATA_URL = ("https://casa.nrao.edu/download/devel/casavis/data/"
            "sis14_twhya_calibrated_flagged.ms.tar.gz")
DATA_MS = "sis14_twhya_calibrated_flagged.ms"
CONDA_PKGS = "python=3.12 pip ipykernel jupyter_client scipy regions 'zarr>=3' datashader certifi"

QUESTIONS = [
    ("login_host", "Login host (the gateway you reach first), real name", "login.example.org"),
    ("remote_host", "Remote host (where the data and the kernel live), real name", "compute1.example.org"),
    ("user", "Your user name on them", os.environ.get("USER", "")),
    ("password_hosts", "Which ask for a password: login, remote, or both", "both"),
    ("service", "Keychain service name for the password", "remote_ssh_pw"),
    ("login_alias", "ssh alias for the login host", "vp-login"),
    ("remote_alias", "ssh alias for the remote host", "vp-remote"),
    ("kernel", "Jupyter kernel name", "vp_remote"),
    ("data_dir", "Data directory on the remote host (absolute path)", ""),
    ("remote_python", "The visplot environment's python on the remote host "
                      "(blank if not created yet)", ""),
]


# --------------------------------------------------------------------------- #
# answers                                                                       #
# --------------------------------------------------------------------------- #

def load() -> dict:
    try:
        with open(ANSWERS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def ask(old: dict) -> dict:
    print(textwrap.fill(
        "Answer each question; Enter keeps the value in [brackets]. Use the real "
        "host names (as in a terminal: ssh you@host), not aliases.", 78))
    print()
    a = dict(old)
    for key, prompt, default in QUESTIONS:
        cur = a.get(key) or default
        if key == "data_dir" and not cur:
            cur = f"/home/{a.get('user') or 'you'}/visplot-data"
        v = input(f"{prompt} [{cur}]: ").strip()
        a[key] = v or cur
    a["password_hosts"] = a["password_hosts"].lower()
    if a["password_hosts"] not in ("login", "remote", "both"):
        a["password_hosts"] = "both"
    os.makedirs(OUT, exist_ok=True)
    with open(ANSWERS, "w") as f:
        json.dump(a, f, indent=1)
    return a


def password_hosts(a: dict) -> list:
    return {"login": [a["login_host"]], "remote": [a["remote_host"]],
            "both": [a["login_host"], a["remote_host"]]}[a["password_hosts"]]


# --------------------------------------------------------------------------- #
# the steps, with the answers filled in                                         #
# --------------------------------------------------------------------------- #

def ssh_config(a: dict) -> str:
    return textwrap.dedent(f"""\
        Host {a['login_alias']}
            HostName {a['login_host']}
            User {a['user']}

        Host {a['remote_alias']}
            HostName {a['remote_host']}
            User {a['user']}
            ProxyJump {a['login_alias']}

        Host {a['login_alias']} {a['remote_alias']}
            ControlMaster auto
            ControlPath ~/.ssh/cm_%r@%h_%p
            ControlPersist 8h
            ServerAliveInterval 30
            ServerAliveCountMax 6
        """)


def rules(a: dict) -> str:
    return (f"# hosts that ask for a password (real host names)\n"
            f"{','.join(password_hosts(a))}   service={a['service']}\n")


def steps(a: dict) -> str:
    rp = a.get("remote_python") or "<the python printed in step 2>"
    ms = f"{a['data_dir'].rstrip('/')}/{DATA_MS}"
    store_mac = f"security add-generic-password -a {a['user']} -s {a['service']} -w"
    store_linux = (f"secret-tool store --label='remote ssh password' "
                   f"service {a['service']} account {a['user']}")
    return textwrap.dedent(f"""\
    # Your setup steps (from 00_setup.md)

    Login host `{a['login_host']}` (alias `{a['login_alias']}`), remote host
    `{a['remote_host']}` (alias `{a['remote_alias']}`), user `{a['user']}`.
    Password asked by: {', '.join(password_hosts(a))}.

    ## 1. Miniforge -- ON YOUR COMPUTER, then ON THE REMOTE HOST
    (skip if you already have conda/mamba using conda-forge; on the remote
    host, log in by hand: ssh -J {a['user']}@{a['login_host']} {a['user']}@{a['remote_host']})

        curl -L -O "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh"
        bash "Miniforge3-$(uname)-$(uname -m).sh" -b -p "$HOME/miniforge3"
        "$HOME/miniforge3/bin/conda" init "$(basename "$SHELL")"
        # open a new terminal / log in again

    ## 2. The visplot environment -- ON BOTH

        mamba create -y -n visplot {CONDA_PKGS}
        conda activate visplot
        pip install "cubevis[visplot]=={CUBEVIS_RELEASE}"

    ON YOUR COMPUTER also:

        pip install sshpyk
        git clone https://github.com/casangi/cubevis.git ~/cubevis

    ON THE REMOTE HOST, note the python (answer it when you run this script again):

        conda activate visplot && python -c "import sys; print(sys.executable)"

    ## 3. The ssh wrapper -- ON YOUR COMPUTER

        mkdir -p ~/bin && cp ~/cubevis/demos/visplot-remote/bin/ssh ~/bin/ssh
        chmod +x ~/bin/ssh && ln -sf ssh ~/bin/scp
        # at the END of ~/.zshrc (macOS) or ~/.bashrc (Linux):
        export PATH="$HOME/bin:$PATH"

    ## 4. Your password -- ON YOUR COMPUTER

        {store_mac}        # macOS
        {store_linux}      # Linux desktop
        mkdir -p ~/.config/ssh-wrapper
        cp {os.path.join(OUT, 'rules.txt')} ~/.config/ssh-wrapper/rules

    ## 5. ssh configuration -- ON YOUR COMPUTER
    Add {os.path.join(OUT, 'ssh_config.txt')} to ~/.ssh/config:

    @@SSH_CONFIG@@
    ## 6. Check ssh -- ON YOUR COMPUTER

        ssh {a['remote_alias']} hostname        # twice: the second time, no [ssh-wrapper] lines

    ## 7. Kernel definition -- ON YOUR COMPUTER

        sshpyk add --ssh-host-alias {a['remote_alias']} --kernel-name {a['kernel']} \\
                   --display-name "visplot on {a['remote_host']}" \\
                   --remote-python {rp} \\
                   --remote-kernel-name python3 --language python
        sshpyk list --remote

    ## 8. Test data -- ON THE REMOTE HOST

        mkdir -p {a['data_dir']} && cd {a['data_dir']}
        curl -L -O {DATA_URL}
        tar xzf {DATA_URL.rsplit('/', 1)[1]}

    ## 9. visplot -- ON YOUR COMPUTER

        python 00_remote_setup.py --check --kernel
        cd ~/cubevis/demos/visplot-remote
        python 03_remote_gui.py --remote-ms {ms} --kernel {a['kernel']} --field 3c279
    """).replace("@@SSH_CONFIG@@\n", textwrap.indent(ssh_config(a), "    ") + "\n")


def write_files(a: dict) -> None:
    os.makedirs(OUT, exist_ok=True)
    for name, text in (("ssh_config.txt", ssh_config(a)), ("rules.txt", rules(a)),
                       ("steps.md", steps(a))):
        with open(os.path.join(OUT, name), "w") as f:
            f.write(text)


# --------------------------------------------------------------------------- #
# checks                                                                        #
# --------------------------------------------------------------------------- #

class Checks:
    def __init__(self):
        self.failed = 0

    def report(self, ok, what, detail="", fix=""):
        tag = {True: "PASS", False: "FAIL", None: "WARN"}[ok]
        print(f"[{tag}] {what}" + (f": {detail}" if detail else ""))
        if ok is not True and fix:
            print(textwrap.indent(textwrap.fill(fix, 72), "       "))
        if ok is False:
            self.failed += 1
        return ok


def run(cmd, timeout=60, **kw):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, **kw)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as e:
        return 127, "", str(e)


def ssh_g(alias):
    rc, out, _ = run(["ssh", "-G", alias], timeout=15)
    cfg = {}
    for line in out.splitlines():
        k, _, v = line.partition(" ")
        cfg.setdefault(k.lower(), v)
    return cfg


def read_rules(path):
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                pats, *kv = line.split()
                r = {"patterns": pats.split(",")}
                for item in kv:
                    k, _, v = item.partition("=")
                    r[k] = v
                m = re.search(r"\bcommand=(.*)$", line)
                if m:
                    r["command"] = m.group(1)
                out.append(r)
    except OSError:
        pass
    return out


def lookup_password(rule, user) -> bool:
    """True if the password store has an entry (the password is not shown)."""
    service = rule.get("service", "ssh-wrapper")
    account = rule.get("account", user)
    backend = rule.get("backend") or ("command" if rule.get("command") else
                                      "security" if sys.platform == "darwin" else
                                      "secret-tool" if shutil.which("secret-tool") else "pass")
    if backend == "security":
        cmd = ["security", "find-generic-password", "-a", account, "-s", service, "-w"]
    elif backend == "secret-tool":
        cmd = ["secret-tool", "lookup", "service", service, "account", account]
    elif backend == "pass":
        cmd = ["pass", "show", rule.get("pass_name", service)]
    else:
        cmd = ["/bin/sh", "-c", rule.get("command", "false")]
    rc, out, _ = run(cmd, timeout=60)
    return rc == 0 and bool(out.strip()), f"{backend}, service '{service}', account '{account}'"


def check(a: dict, kernel: bool) -> int:
    c = Checks()
    print(f"Checking the setup for {a['remote_alias']} ({a['remote_host']}) "
          f"through {a['login_alias']} ({a['login_host']})\n")

    # --- 3. wrapper first on PATH, OpenSSH version, perl
    ssh = shutil.which("ssh") or ""
    is_wrapper = False
    try:
        with open(ssh, "rb") as f:
            is_wrapper = b"ssh-wrapper" in f.read(4000)
    except OSError:
        pass
    c.report(is_wrapper, "ssh on your PATH is the wrapper", ssh or "no ssh found",
             "Step 3: copy bin/ssh to ~/bin/ssh and put ~/bin first in PATH (after the "
             "conda lines in your shell start-up file); open a new terminal.")
    scp = shutil.which("scp") or ""
    c.report(os.path.realpath(scp) == os.path.realpath(ssh) if is_wrapper else None,
             "scp is the wrapper too", scp, "Step 3: ln -sf ssh ~/bin/scp")
    c.report(bool(shutil.which("perl")), "perl is installed", "", "Install perl (it comes with macOS and most Linux).")
    rc, _, err = run(["ssh", "-V"], timeout=10)
    m = re.search(r"OpenSSH_(\d+)\.(\d+)", err)
    ver = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    c.report(ver >= (8, 4), "OpenSSH 8.4 or later", err.strip().split(",")[0],
             "The wrapper needs SSH_ASKPASS_REQUIRE (OpenSSH 8.4+).")

    # --- 4. rules and password
    rules_path = os.environ.get("SSH_WRAPPER_RULES") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "ssh-wrapper", "rules")
    rl = read_rules(rules_path)
    c.report(bool(rl), "rules file", rules_path,
             f"Step 4: cp {os.path.join(OUT, 'rules.txt')} {rules_path}")
    for host in password_hosts(a):
        rule = next((r for r in rl if any(fnmatch.fnmatch(host.lower(), p.lower())
                                          for p in r["patterns"])), None)
        if c.report(rule is not None, f"a rule covers {host}", "",
                    "Step 4: list the real host name in the rules file."):
            ok, where = lookup_password(rule, a["user"])
            c.report(ok, f"password stored for {host}", where,
                     "Step 4: store the password with that service and account "
                     "(the account defaults to User in ~/.ssh/config).")

    # --- 5. ssh configuration
    for alias, host in ((a["login_alias"], a["login_host"]), (a["remote_alias"], a["remote_host"])):
        g = ssh_g(alias)
        c.report(g.get("hostname", "").lower() == host.lower(), f"ssh alias {alias} -> {host}",
                 g.get("hostname", "?"), f"Step 5: add {os.path.join(OUT, 'ssh_config.txt')} to ~/.ssh/config")
        cp = g.get("controlpath", "none")
        c.report(cp != "none", f"{alias} has a ControlPath", cp, "Step 5: the 'Host ... ControlPath' section.")
        persist = g.get("controlpersist", "no")
        c.report(persist not in ("no", "false", "0"), f"{alias} has ControlPersist", persist,
                 "Step 5: ControlPersist 8h")
        if alias == a["remote_alias"]:
            pj = g.get("proxyjump", "")
            c.report(pj == a["login_alias"], f"{alias} goes through {a['login_alias']}", pj or "no ProxyJump",
                     f"Step 5: ProxyJump {a['login_alias']}")
        fx = g.get("forwardx11", "no")
        if fx == "yes":
            c.report(None, f"{alias} forwards X11", "",
                     "Remove ForwardX11 yes from Host * in ~/.ssh/config: it slows down "
                     "non-interactive connections (sshpyk) and prints warnings.")

    # --- 6. connection
    rc, out, err = run(["ssh", a["remote_alias"], "hostname"], timeout=120)
    last_err = (err.strip().splitlines() or [""])[-1]
    ok = c.report(rc == 0, f"ssh {a['remote_alias']} hostname", out.strip() if rc == 0 else last_err,
                  "Step 6: run it in a terminal to see the [ssh-wrapper] messages; "
                  "SSH_WRAPPER_DEBUG=1 logs to ~/.ssh/ssh-wrapper.log.")
    if ok:
        rc, _, err = run(["ssh", "-O", "check", a["remote_alias"]], timeout=15)
        c.report(rc == 0, "shared connection is up", err.strip(),
                 "Step 5: ControlPath / ControlPersist for the remote alias.")

    # --- 2. remote environment
    rp = a.get("remote_python")
    if not rp and ok:
        # The usual place (00_setup.md, steps 1-2), expanded on the remote host.
        rc, out, _ = run(["ssh", a["remote_alias"],
                          'p="$HOME/miniforge3/envs/visplot/bin/python"; test -x "$p" && echo "$p"'],
                         timeout=60)
        rp = out.strip()
    if c.report(bool(rp), "remote python known", rp or "",
                "Step 2 on the remote host, then run this script again without --check "
                "and give the python path."):
        code = ("import sys, importlib.metadata as m; import cubevis, ipykernel, jupyter_client, xarray_ms; "
                "print(sys.version.split()[0], m.version('cubevis'))")
        rc, out, err = run(["ssh", a["remote_alias"], f"{rp} -c \"{code}\""], timeout=120)
        c.report(rc == 0, "remote environment has cubevis, ipykernel, xarray-ms",
                 out.strip() or (err.strip().splitlines() or [""])[-1],
                 f'Step 2 on the remote host: pip install "cubevis[visplot]=={CUBEVIS_RELEASE}"')
        if rc == 0:
            remote_v = out.split()[-1]
            try:
                import importlib.metadata as md
                local_v = md.version("cubevis")
            except Exception:
                local_v = "not installed"
            c.report(local_v == remote_v, "same cubevis on both sides",
                     f"here {local_v}, remote {remote_v}",
                     f'Install the same release on both: pip install "cubevis[visplot]=={CUBEVIS_RELEASE}"')

    # --- 7. kernel definition
    try:
        from jupyter_client.kernelspec import KernelSpecManager
        spec = KernelSpecManager().get_kernel_spec(a["kernel"])
        argv = " ".join(spec.argv)
        meta = json.dumps(spec.metadata)
        c.report(a["remote_alias"] in argv or a["remote_alias"] in meta,
                 f"kernel '{a['kernel']}' uses {a['remote_alias']}", spec.resource_dir,
                 "Step 7: sshpyk add --ssh-host-alias ...")
    except ImportError:
        c.report(False, "jupyter_client / sshpyk installed here", "",
                 "Step 2 on your computer: conda activate visplot; pip install sshpyk")
    except Exception as e:
        c.report(False, f"kernel '{a['kernel']}' defined", str(e).splitlines()[0], "Step 7: sshpyk add ...")
    if not shutil.which("sshpyk"):
        c.report(False, "sshpyk installed here", "", "pip install sshpyk (in the visplot environment)")

    # --- 8. data
    ms = f"{a['data_dir'].rstrip('/')}/{DATA_MS}"
    if ok:
        rc, _, _ = run(["ssh", a["remote_alias"], f"test -d {ms}"], timeout=60)
        c.report(rc == 0, "test data on the remote host", ms, "Step 8.")

    # --- 9. kernel and plot
    if kernel and c.failed == 0:
        print("\nStarting the kernel and drawing a test plot (a minute or so) ...")
        try:
            # The installed release (not this checkout): that is what the
            # version check compares with the remote side.
            import warnings
            warnings.filterwarnings("ignore")
            from cubevis.toolbox.visplot import VisibilityPlotter
            vp = VisibilityPlotter(ms=ms, field="3c279", headless=True,
                                   backend="remote", kernel_name=a["kernel"])
            out_png = os.path.join(OUT, "remote_test.png")
            path = vp(plotfile=out_png, theme="light")
            warn = vp._version_warning
            vp.close()
            c.report(True, "remote kernel drew a plot", path)
            if warn:
                c.report(None, "version check", warn, "Install the same cubevis release on both sides.")
        except Exception as e:
            c.report(False, "remote kernel drew a plot", f"{type(e).__name__}: {e}",
                     "See the messages above; SSH_WRAPPER_DEBUG=1 and CUBEVIS_DEBUG=1 give logs.")
    elif kernel:
        print("\n(not starting the kernel: fix the failures above first)")

    print(f"\n{'All checks passed.' if c.failed == 0 else f'{c.failed} check(s) failed.'}")
    return 1 if c.failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="test each step (changes nothing)")
    ap.add_argument("--kernel", action="store_true", help="with --check: also start the kernel and plot")
    ap.add_argument("--reset", action="store_true", help="forget the saved answers")
    args = ap.parse_args()
    if args.reset and os.path.exists(ANSWERS):
        os.remove(ANSWERS)
    a = load()
    if args.check:
        if not a:
            sys.exit("No answers yet: run without --check first.")
        sys.exit(check(a, args.kernel))
    a = ask(a)
    write_files(a)
    print("\n" + steps(a))
    print(f"These steps, and the files to copy, are in {OUT}/ "
          "(steps.md, ssh_config.txt, rules.txt).")
    print("When they are done: python 00_remote_setup.py --check --kernel")


if __name__ == "__main__":
    main()
