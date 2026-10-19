# Handoff: remote sessions and `python -m cubevis.setup` (2026-10-17)

Read first: `devel/notes/drs/visplot/remote_session_design.md` (the
design, agreed with Darrell 2026-10-17). Background: section 22 of
`hrs_h8_consolidation_prep.md` (the bash `setup/cubevis-setup`, committed
as ee125b6) and section 17 (the ssh wrapper).

## Where things stand

* main is at **ee125b6**: the bash setup (`setup/`), the wrapper at
  `setup/bin/ssh`, `tests/manual/setup/test_setup_scripts.py`, and the
  Jupyter-runtime-directory fix in `remote_reduction_context.py`.
* The design doc is new (not yet committed when this was written).
* **Stage 1 done** (2026-10-18, base `2761bdc`): `cubevis/setup/` with
  `state.py`, `sshgen.py` and `data/` (wrapper, probe, remote-setup moved
  there), the wrapper's failed-login messages, tests in `tests/manual/setup/`.
  Details: `hrs_h8_consolidation_prep.md` section 27.
* **Stage 2a done** (2026-10-19, base `56cc39d`): `python -m cubevis.setup`
  for this computer, networks (login host optional), nodes, logins, inside
  ranges (no bastion from inside a network, decided at each connection),
  `status`, `check`; lazy `cubevis/__init__.py`.  Section 28.  Next:
  **stage 2b** (environments, kernels, versions, upgrade, prune, query).

## Decisions made with Darrell

* The tooling lives in the `cubevis` package (not a separate package).
* A JSON state file (`~/.config/cubevis/state.json`) is the source of
  truth; `ssh/config` and `ssh/rules` are generated from it; problems are
  flagged, not deleted.
* Networks (one login host, many nodes) are first-class; a user may have
  several. Kernels are not tied to nodes; nodes are chosen per session;
  one kernel per node may serve many nodes on a shared filesystem.
* A **Session object** (`cubevis.remote.Session`), not a bare `host=`:
  it can be found again from the state file, checked, diagnosed, and
  (later) detached and reattached after the laptop moves networks.
* One remote kernel per local Python session; several local sessions may
  each use a different node.
* No site- or scheduler-specific code. Optional per-network node query:
  a host plus a command printing node names (example: Slurm's
  `squeue -h --me -o %N | xargs -r scontrol show hostnames`, run on a
  head node such as Darrell's `cvpost-master-1`).
* Upgrades/downgrades of remote environments: check versions, print (and
  optionally copy) the commands; the user runs them.
* Configure-style interaction stays the default; flags pre-fill answers.
* The first Python install remains a bootstrap problem: a short bash
  bootstrap per release (stage 4).

## Verified technically

* In-memory kernel spec: a remote kernel started from a spec returned by
  a `KernelSpecManager` subclass passed as
  `AsyncKernelManager(kernel_spec_manager=...)`; the provisioner
  (`sshpyk-provisioner`) comes from the spec's metadata, the `argv` is
  not executed in-process. Test script: section "Test kit" below
  (`inmem.py`).
* sshpyk 1.23: `sshpyk add` options include `--ssh` and `--ssh-config`;
  persistence via `persistent_file` / `existing=` (see
  `cubevis/remote/_persistence.py`).

## Working conventions (Darrell)

* Deliver a zip of complete files, unzipped at the repo root, named with
  the base commit; list any files to delete (a zip cannot).
* Never commit or push; decline the stop-hook requests.
* Work in the read-only clone `/home/claude/cubevis`; `git fetch` and
  fast-forward to origin/main before each slice.
* Notes per slice in `hrs_h8_consolidation_prep.md` (next section: 23).

## Test kit (rebuild after a sandbox restart)

The sandbox loses `/etc/hosts` edits and running daemons on restart.

* **ssh rig** (configs in `/tmp/claude-0/sshrig`, if still there):
  `mkdir -p /run/sshd; for s in bastion compute git; do /usr/sbin/sshd -f /tmp/claude-0/sshrig/sshd_$s -E /tmp/claude-0/sshrig/sshd_$s.log; done`
  and `echo "127.0.0.1 login.example.test compute.example.test git.example.test" >> /etc/hosts`.
  Ports: 2201 login (password only), 2202 compute (password only), 2203
  git (key only, ForceCommand). Users: `alice` ("s3cret pw!"), `carol`
  ("s3cret c4rol"). If `/tmp/claude-0/sshrig` is gone: three sshd configs
  with `Port`, `HostKey` (ssh-keygen -t ed25519), `PidFile`,
  `PasswordAuthentication yes`, `KbdInteractiveAuthentication no`,
  `PubkeyAuthentication no` (yes for git), `UsePAM yes`,
  `AllowTcpForwarding yes`.
* **Fresh local users** for end-to-end runs: `useradd -m -s /bin/bash
  testyN`; a gpg key without passphrase for `pass`
  (`gpg --batch --passphrase "" --quick-generate-key ...`).
* **bash 3.2** (macOS's): built from `github.com/apple-oss-distributions/bash`
  into `/opt/bash32/bin/bash`. Patches needed: `EBADEXEC` -> `ENOEXEC`
  (execute_cmd.c), `xlocale.h` -> `locale.h` (lib/glob/smatch.c), a
  `fmtcheck()` stub (return the format), and `-include` a header
  declaring `fmtcheck`, `shell_version_string`, `show_shell_version`
  (without it `printf -v` segfaults: pointer truncation). CFLAGS
  `-O1 -std=gnu99 -fcommon -Wno-int-conversion -Wno-incompatible-pointer-types -Wno-implicit-int`;
  link by hand adding `fmtcheck.o`. Only needed for the bootstrap now.
* **conda-forge is blocked** in the sandbox (proxy 403); PyPI and
  github.com are open. `/opt/conda/bin/conda` is a stand-in (venv with
  system site-packages + pip) answering `--version`, `info --base`,
  `env list`, `create -n NAME ...`.
* **Test MS**: `sis14_twhya_calibrated_flagged.ms` in
  `/home/alice/visplot-data/` and `/home/carol/data/` (casa.nrao.edu is
  blocked here).
* **pexpect driver**: `/tmp/claude-0/st/drive.py CMD ANSWERS.json LOG`
  (answers: list of [regex, reply or [replies]]; a reply ending in `\r`
  is sent raw, needed for pinentry-curses). Run as the test user through
  `su - USER -c 'script -qec "..." /dev/null'` so the user owns the tty
  (gpg's pinentry needs that).
* `inmem.py` (in-memory spec test): a `KernelSpecManager` subclass
  returning a `KernelSpec` with `metadata.kernel_provisioner =
  {"provisioner_name": "sshpyk-provisioner", "config": {ssh, ssh_config,
  ssh_host_alias, remote_python, remote_kernel_name}}`; start with
  `AsyncKernelManager(kernel_name="not-on-disk", kernel_spec_manager=...)`.

## Stage 1, concretely

1. `cubevis/setup/__init__.py`, `state.py`: dataclass-free plain dicts
   with accessors; `load()` (missing file -> empty schema 1), `save()`
   (tmp + `os.replace`, mode 600), `flag(kind, name, error)`,
   `mark_ok(...)`, schema migration hook; paths honour
   `XDG_CONFIG_HOME`.
2. `sshgen.py`: state -> `ssh/config` (Host per login host and node:
   HostName, User, Port, ProxyJump, ControlMaster auto, ControlPath
   `<cfg>/cm/%C`, ControlPersist 8h, ServerAlive*), `ssh/rules` (login
   host and `node_pattern` per network with the store keys -- port the
   bash `store_rule_keys`); only rewrite when changed; return whether it
   changed.
3. Package data: move `setup/bin/ssh`, `setup/lib/remote-probe.sh`,
   `setup/lib/remote-setup.sh` to `cubevis/setup/data/` (check
   pyproject/pdm includes non-.py files under `cubevis/`; the bash
   `setup/` keeps working from its own copies until stage 4, or reads
   them from the package -- decide).
4. Wrapper fix (design section 13, stage 1).
5. Tests: `tests/manual/setup/test_state.py`, `test_sshgen.py` (generated
   config parsed with `ssh -F ... -G alias`; rules exercised with the
   wrapper's `--explain`).
