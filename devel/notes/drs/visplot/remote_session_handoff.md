# Handoff: remote sessions and `python -m cubevis.setup` (2026-10-17, updated 2026-10-21)

Read first: `devel/notes/drs/visplot/remote_session_design.md` (the
design, agreed with Darrell 2026-10-17). Background: section 22 of
`hrs_h8_consolidation_prep.md` (the bash `setup/cubevis-setup`, committed
as ee125b6) and section 17 (the ssh wrapper).

## Where things stand

* main is at **24f5af8** (2a, OpenSSH 8.0, design 7b committed).
  Delivered on top of it, not yet committed when this was written:
  **`remote_stage2b1_24f5af8.zip`** (stage 2b part i, below).  Check
  `git log` first.
* **Design changed 2026-10-20/21** (section 7b of the design): setup runs
  in one of two roles, GUI or node; the node side configures and tests
  environments (several per host, only configured ones used; an
  environment belongs to one network); the GUI side reads the nodes'
  `remote/<network>/<id>.json` over ssh; the configuration directory may
  be shared by several computers.  Inside ranges are a positive list
  (visitor networks use the bastion); `--inside-here` checks before
  recording; `CUBEVIS_ROUTE_<net>=direct|bastion` overrides; no probing
  at kernel start.
* **Stage 2b part i done** (2026-10-21, base `24f5af8`): per-computer
  `hosts/<id>/` (local.json, ssh files, wrapper, logs), `state.json`
  schema 2 saved under a lock and merged field by field, migration from
  2a, sockets in `/tmp/cubevis-<uid>/cm`.  Section 30.
* **Stage 1 done** (2026-10-18, base `2761bdc`): `cubevis/setup/` with
  `state.py`, `sshgen.py` and `data/` (wrapper, probe, remote-setup moved
  there), the wrapper's failed-login messages, tests in `tests/manual/setup/`.
  Details: `hrs_h8_consolidation_prep.md` section 27.
* **Stage 2a done** (2026-10-19, base `56cc39d`): `python -m cubevis.setup`
  for this computer, networks (login host optional), nodes, logins, inside
  ranges (no bastion from inside a network, decided at each connection),
  `status`, `check`; lazy `cubevis/__init__.py`.  Section 28, including
  revision 2: the wrapper no longer takes another copy of itself on PATH
  for the real ssh (Darrell's macOS PATH had the bash setup's wrapper), and
  the tests no longer write `~/.ssh/known_hosts` or the wrapper log.
* **Not yet tried on the real network**: the inside-range suggestion (/16s
  of `$SSH_CONNECTION`) and the `Match exec` inside test.  Darrell's sample
  addresses (section 28) are in `test_inside.py`; his first real run is the
  check.  Ask how it went before building on it.
* **OpenSSH 8.0** (RHEL 8) is supported since 2026-10-19 (section 29).
* **Next: stage 2b part ii** -- below, "Stage 2b, concretely".

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
* Notes per slice in `hrs_h8_consolidation_prep.md` (next section: 31).

## Test kit (rebuild after a sandbox restart)

The sandbox loses `/etc/hosts` edits and running daemons on restart.

* **ssh rig**: `sudo devel/tools/ssh_rig.sh` (idempotent; `stop` to stop).
  It adds `login.example.test` / `compute.example.test` (127.0.0.1) to
  `/etc/hosts`, the user `alice` ("s3cret pw!"), and password-only sshd's
  on 2201 (login) and 2202 (compute) -- what `tests/manual/setup` needs
  with `CUBEVIS_SSH_RIG=1`.  Disposable container or VM only.  (The bash
  setup's older end-to-end runs also used a key-only git host on 2203 and
  the user `carol`; not needed by the Python tests, not in the script.)
* **Host keys in tests**: `CUBEVIS_SETUP_KNOWN_HOSTS=<file>` makes
  `sshgen.config_text` end with `Host *` / `UserKnownHostsFile <file>` /
  `StrictHostKeyChecking accept-new`.  The test fixtures set it; never set
  in normal use.  Without it the rig tests depend on keys already in
  `~/.ssh/known_hosts` (that is how they passed before 2026-10-19).
* **pytest here**: `/home/claude/venv/bin/python -m pytest` (the system
  python has none).
* **OpenSSH 8.0 client** (RHEL 8's): built from `openssh-portable` tag
  `V_8_0_P1` with `--without-openssl --without-zlib-version-check` into
  `/opt/openssh80/bin/ssh`; run the suite with `PATH=/opt/openssh80/bin:$PATH`
  as well as with the system's.  The rig's sshd is the distribution's
  (`apt-get install openssh-server`).
* **Several computers in one directory** (tests): `CUBEVIS_HOST_ID` names
  the computer, `CUBEVIS_RUNTIME_DIR` gives each its own socket directory
  (otherwise one rides on another's shared connections).
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

## Open points (collected 2026-10-19)

* **First contact with a login host, straight to a node.**  When the
  wrapper logs in to a password login host as a hop of its own, it passes
  only `-F` and the config's options, not the caller's `-o
  StrictHostKeyChecking=accept-new`; a login host ssh has never seen then
  fails with "Host key verification failed".  Setup is not affected (it
  checks the login host directly before any node, which records the key),
  but a kernel or a user going straight to a node on a new account would
  be.  Options: setup always visits the login host first (as now) and says
  so; or the generated config sets `StrictHostKeyChecking accept-new` for
  the hosts the user typed in.  Darrell's decision; raise it in 2b.
* **Scatter memory**: 65-110 bytes per sample at peak, unmeasured whether
  inherent, wasteful or a leak (also the growth seen in
  `test_visibility_scatter`).  The guard is opt-in
  (`CUBEVIS_VISPLOT_MEMORY_GUARD`).  Offered to Darrell, not requested.
* **The bash setup** (`setup/`, including `setup/bin/ssh`) stays until
  stage 4.  Copies of its wrapper on a PATH are now skipped by the
  packaged wrapper.
* Design open questions: `remote_session_design.md` section 12.

## Stage 2b, concretely

Design sections 7b and 13 (the plan), 8, 9.

1. ~~Part i: the file layout, locking and migration, sockets on local
   disk~~ (section 30).
2. **Part ii: environments and the node-side command.**  Environment
   records: `kind` (conda, mamba, venv, python), `python` (resolved; with
   the network, the identity), activation and upgrade information,
   `hosts` (every node, or a list).  The node-side command: role
   question (suggest *node* when `$SSH_CONNECTION` is set), network name
   and domain, user, optional node query (tested there); environments
   asked for, found ones only offered; each tested by running its
   interpreter with a small script (Python version; cubevis, ipykernel
   import and versions); failures recorded flagged; writes
   `remote/<network>/<id>.json` (single writer: that computer).  The
   GUI host's own environments: the network *this host*.
3. **Part iii: the GUI side** reads `remote/<network>/*.json` over ssh,
   shows and records them; `network NET --inside-here` (checks a direct
   connection first, records only then); `CUBEVIS_ROUTE_<net>` in
   `inside.py`, before the ranges.
4. **Part iv:** versions and upgrade scripts (section 8), the node query
   on the GUI side, `prune`, `check`/`status` for environments and
   kernels, the final kernel + plot test.
5. Tests: pexpect on the rig as before; a local python3 "remote"
   environment on the rig's compute host; version mismatch; a query
   command printing names; two computers on one directory (as in part i).

Then stage 3 (Session), 4 (bootstrap, docs, remove `setup/`), 5 (detach /
reattach): design section 13.
