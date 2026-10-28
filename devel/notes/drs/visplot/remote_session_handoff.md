# Handoff: remote sessions and `python -m cubevis.setup` (2026-10-17, updated 2026-10-23)

Read first: `devel/notes/drs/visplot/remote_session_design.md` (the
design, agreed with Darrell 2026-10-17). Background: section 22 of
`hrs_h8_consolidation_prep.md` (the bash `setup/cubevis-setup`, committed
as ee125b6) and section 17 (the ssh wrapper).

## Where things stand

* main is at **e6f4fe9** (2c step 1 committed: the fingerprint, section 33,
  and the lazy package `__init__`s, section 34).  Delivered on top of it,
  not yet committed when this was written: **`remote_stage2c2_e6f4fe9.zip`**
  (2c step 2: `setup HOST`, section 35).  Check `git log` first.
* **Stage 2c step 2 done** (2026-10-23, base `e6f4fe9`): `python -m
  cubevis.setup HOST [--kernel NAME]` as the one path; clusters; kernels
  with their directory, variables and a launcher per kernel on the node
  (`~/.config/cubevis/launch/<gui-id>/<kernel>.sh`), tested by running it;
  the discovery script (`discover.py`, Python 3.6+, `python3 -` over ssh;
  by hand `python3 -m cubevis.setup.discover`); state schema 3;
  `node-here`, node files, netsync, hostinfo and the `node` command
  removed.  Section 35.  **Not yet run on the real network**: ask how
  Darrell's first `setup zuul06...` / `setup cvpost...` went (RHEL 8's
  python3 3.6, tcsh logins, the cluster question) before building on it.
* **Design changed 2026-10-20/21** (section 7b of the design): setup runs
  in one of two roles, GUI or node; the node side configures and tests
  environments (several per host, only configured ones used; an
  environment belongs to one network); the GUI side reads the nodes'
  `hosts/<id>/node/<network>.json` over ssh; the configuration directory may
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
* **Stage 2b part ii done** (2026-10-21, base `bbf9d27`): `node-here`
  (the node side), environments found / chosen / checked, the node file
  `hosts/<id>/node/<network>.json`; the walk-through asks the role.  Section 31.
  Darrell ran part i on zuul06 (RHEL 8): `status` correct.
* **Host identity** decided 2026-10-21: the short host name, no machine
  id / IOPlatformUUID (regenerated per boot on diskless nodes, cloned in
  images, changed by replacements that should keep the configuration);
  instead every per-computer value is re-checked on each run.
* **OpenSSH 8.0** (RHEL 8) is supported since 2026-10-19 (section 29).
* **Stage 2b part iii done** (2026-10-22, base `0a5b519`): launchers on the nodes
  (working directory, variables, activation; verified with a real sshpyk
  kernel), the GUI side reading node files into `state.json`,
  `--inside-here`, `CUBEVIS_ROUTE_<NET>`.  Section 32.
* **Direction changed 2026-10-23** (Darrell, design section 7c): the user's
  path is `python -m cubevis.setup HOST`.  Route from `ssh -G HOST` and a
  login test (a bastion in the user's own ssh config is adopted); the
  network inferred from the node's domain; environments found over ssh by
  a stdlib discovery script; default: setup's own minimal environment
  `cubevis-<version>` (marker file; created by setup, never modified),
  also listing the user's environments with the same or newer cubevis;
  conda only (no venv; no conda -> ask the user to install Miniforge);
  `--cleanup HOST` removes setup's environments there; a `--2fa` flag for
  hosts whose login needs a second factor (not yet the case at NRAO).
* **Compatibility = a kernel-side code fingerprint** (Darrell 2026-10-23,
  design 7c): a declared list of kernel-side modules hashed by syntax tree
  on each side, compared exactly; an import-tracing test keeps the list
  complete.  Not the cubevis version, not a hand-kept protocol number.
* **Kernels carry the settings** (Darrell 2026-10-23, design 7c 4a): a
  kernel = a user-chosen name + an environment + working directory +
  variables + launch options; several kernels per environment; one
  launcher per kernel; `setup HOST --kernel NAME` adds one.  2b part iii's
  per-environment directory/variables move to kernels in 2c.
* **Refinements 2026-10-23** (design 7c, "Refinements" and "Diagnostics"):
  one path (`node-here`, node files and their sync removed; `setup
  --local` for the plotting computer's own kernels); clusters (name,
  node pattern, route, kernels; any matching node usable without setup);
  environment location from conda's `envs_dirs`, shown with size; detached,
  resumable creation; `CUBEVIS_ALLOW_MISMATCH` / `--sync-checkout`; the
  user's ssh config as reference, a minimal generated one carrying the
  login options, tested before saving; 2FA via the shared connection;
  diagnostics: `python3 -m cubevis.setup.discover` on the node,
  `setup diagnose HOST` here.
* **Stage 2c step 1 done** (2026-10-23): `cubevis/_fingerprint.py`, reported by
  `envcheck`.  Lazy `__init__`s for `cubevis.toolbox` and
  `cubevis.toolbox.visplot` (agreed; 127 -> 82 fingerprinted, 116 -> 42
  imported on the kernel side; section 34).
* **Darrell's first real run** (2026-10-26, `setup cvpost106` from a fresh
  macOS account): worked end to end.  Since then (section 36, base
  `9f21e53`): the inside ranges are offered as found ([u]se / [c]hange /
  [n]one), and `python -m cubevis.setup jupyter KERNEL [NODE]` makes a
  Jupyter kernel definition so `visplot(..., backend="remote",
  kernel_name="cubevis-KERNEL")` works until the Session exists.  Found,
  not fixed: a dead worker leaves the plotter waiting forever.
* **Second clean-account run** (2026-10-26, section 37): CASA is never
  imported to see whether to use it (`cubevis/utils/_casa.py`); an unknown
  `kernel_name=` names the fix; the login-host and password wording; plain
  `echo` password commands refused (tests: `CUBEVIS_SETUP_ALLOW_PLAIN_COMMAND=1`).
  For step 3: setup's environments from a release (PyPI), a local wheel
  (copied to the node) or a checkout (synced, 2d).
* **Third clean-account run** (2026-10-27, section 38): `setup HOST` now
  writes the Jupyter kernel and prints the visplot call; `kernel_name=`
  takes the setup kernel's name; `status` lists the Jupyter kernels; a
  `remote` extra (sshpyk), offered by setup when missing.
* **Safari** (2026-10-27, section 39): pages now load their scripts from
  `js/` beside them; Safari refused the `file://` scripts in site-packages
  (blank window).  To be confirmed by Darrell in Safari.
* **2026-10-28** (section 40): the Keychain checked before it is offered,
  another store offered when one fails; defaults (real names, users,
  ProxyJump) from the user's `ssh -G` -- the first part of step 4.
* **Darrell's run of 2026-10-28 worked end to end** (remote kernel, plot in
  Safari).  Then (section 41): kernels named for their working directory,
  never a node; cluster defaults from the first kernel.  Agreed for stage
  3: the node chosen per session (given, else the cluster's reservation
  query -- moved into stage 3 -- else the last that answered), the
  Jupyter-kernel bridge gone with the Session.
* **Next: stage 2c step 3** (design 7c "Refinements", section 13): setup's
  own environments -- `envs_dirs` (discovery reports it) shown with the
  expected size, another place allowed; `<tool> create -y -n cubevis-<v>
  python=<here> pip` + `pip install "cubevis[visplot]==<v>"`, detached on
  the node (`nohup`, a log), polled, the marker `cubevis-setup.json` written
  last (resumable); reuse by marker; offer to remove superseded ones;
  `--cleanup HOST`.  Hook: `hostsetup.no_environment_advice` prints the
  commands today; discovery already reads markers and sorts setup's own
  environments first.  Then step 4 (route from `ssh -G`, `diagnose HOST`,
  `--2fa`), then 2d.  Part iv as planned below is folded into 2c/2d.

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
* Notes per slice in `hrs_h8_consolidation_prep.md` (next section: 42).

## Test kit (rebuild after a sandbox restart)

The sandbox loses `/etc/hosts` edits and running daemons on restart.

* **ssh rig**: `sudo devel/tools/ssh_rig.sh` (idempotent; `stop` to stop).
  It adds `login.example.test` / `compute.example.test` /
  `compute2.example.test` (127.0.0.1; compute2 is a second name for the
  same sshd, a second node of a cluster) to `/etc/hosts`, the user `alice` ("s3cret pw!"), and password-only sshd's
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
* **Kernel-capable test environments**: `/home/claude/venv-kernel`
  (python3.12 venv: `pip install ipykernel sshpyk==1.23 pytest pexpect pytest-asyncio pytest-xdist -e "/home/claude/cubevis[visplot]"`;
  sshpyk for the launcher kernel test; `mkdir -p ~/.local/share/jupyter/runtime`);
  `CUBEVIS_TEST_KERNEL_PYTHON=/home/claude/venv-kernel/bin/python`; run the
  suite with that Python.  alice's node side is made by `test_cli.py`'s
  `alice_envs` fixture (nothing by hand): a fake `~/miniforge3/bin/conda`
  listing `envs/hrs` (venv + `conda-meta` + venv-kernel's cubevis
  `dist-info` + a `.pth` to the checkout and venv-kernel's site-packages),
  `envs/plain`, `envs/alt` (a copy of cubevis with a changed
  `worker_main`: another fingerprint).
* **Python 3.6** (RHEL 8's python3), for `test_discover.py`: built from
  `github.com/python/cpython` tag `v3.6.15` (`./configure --prefix=/opt/py36
  && make && make altinstall`), found at `/opt/py36/bin/python3.6` or
  `CUBEVIS_TEST_PY36`.  **tcsh** (`apt-get install tcsh`) for the csh
  tests.
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
  `/home/alice/visplot-data/` (from Darrell's upload: `cat ...part0* | tar
  -xz`; casa.nrao.edu is blocked here); the launcher kernel test opens it
  when it is there.
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

* **Reconnection files on a shared home** (2026-10-21, for stage 3/5).
  sshpyk writes its persistence file to Jupyter's runtime directory
  (`<jupyter_data_dir>/runtime/sshpyk-...-<uuid>.json`: `~/Library/Jupyter/
  runtime` on macOS, `~/.local/share/jupyter/runtime` on Linux; on disk,
  kept across logout and reboot -- jupyter_core 5.9 no longer uses
  `$XDG_RUNTIME_DIR`).  cubevis's `KernelPersistenceManifest` is one file
  there, `cubevis-kernel-manifest.json`, written atomically but without a
  lock or re-read: on a shared home two GUI computers can drop each other's
  entries.  Plan: the Session passes `persistent_file` under
  `hosts/<id>/sessions/` and keeps its record in `hosts/<id>/local.json`
  (locked, merged), instead of the shared manifest.  The sockets in
  `/tmp/cubevis-<uid>/cm` are only live connections: losing them (logout,
  reboot) costs one wrapper login per hop, nothing else.

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
2. ~~**Part ii: environments and the node-side command.**~~ (section 31)  Environment
   records: `kind` (conda, mamba, venv, python), `python` (resolved; with
   the network, the identity), activation and upgrade information,
   `hosts` (every node, or a list).  The node-side command: role
   question (suggest *node* when `$SSH_CONNECTION` is set), network name
   and domain, user, optional node query (tested there); environments
   asked for, found ones only offered; each tested by running its
   interpreter with a small script (Python version; cubevis, ipykernel
   import and versions); failures recorded flagged; writes
   `hosts/<id>/node/<network>.json` (single writer: that computer).  The
   GUI host's own environments: the network *this host*.
3. ~~**Part iii**~~ (section 32) also writes, on the node, a launcher per environment
   (working directory, node-set environment variables; section 31a).
   **The GUI side** reads `hosts/*/node/<network>.json` (directly
   when the home is shared with the nodes, else over ssh), shows and
   records them (environments into `state.json` via `add_environment`,
   merged by identity: same python on several nodes of a shared home ->
   one environment, `hosts` "all" or the list; `local` ones into
   `hosts/<id>/local.json`); offers the node's `ranges` as inside ranges; `network NET --inside-here` (checks a direct
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
