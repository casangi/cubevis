# Remote sessions and `python -m cubevis.setup` -- design

Status: proposal, 2026-10-17. Supersedes the bash `setup/cubevis-setup`
(section 22 of `hrs_h8_consolidation_prep.md`) once its stages land; that
script stays until stage 4.

## 1. Goals

* Setting up, extending and checking remote kernels without a cubevis
  checkout: the tooling ships in the `cubevis` package.
* The same release on both sides, by construction: setup installs (or
  tells you how to install) the release it is part of.
* **Networks** of nodes behind one login host, many nodes per network,
  several networks per user.
* **Kernels decoupled from nodes**: a kernel is an environment on a
  network's shared filesystem; the node is chosen per session. A cluster
  reservation that moves to new nodes changes nothing but the node.
* **One remote kernel per local Python session**; several local sessions
  may each use a different node at the same time.
* A **session object** that can be found again (from the state file),
  asked about its health, and diagnosed; later, detached and reattached
  after the laptop changes networks.
* Configure-style interaction (walk through, explain, ask, change, test);
  flags only pre-fill answers.

Not goals: anything specific to one site or scheduler (a generic query
hook instead, section 7); upgrading remote environments automatically
(setup checks and prints the commands, section 8); Windows.

## 2. The pieces

```
bootstrap (bash, once)      Miniforge + the cubevis environment + pip install cubevis
python -m cubevis.setup     Configure-style: networks, nodes, environments, kernels, checks
~/.config/cubevis/state.json        the source of truth (section 3)
~/.config/cubevis/ssh/{config,rules}   generated from it on every change
~/.config/cubevis/bin/ssh           the wrapper (package data, copied)
cubevis.remote.Session      where a kernel runs; starts it; health; diagnostics
VisibilityPlotter(..., session=s)   (and the iclean demos)
```

Package layout (new):

```
cubevis/setup/__main__.py      python -m cubevis.setup
cubevis/setup/state.py         load/save/migrate state.json (atomic, schema version)
cubevis/setup/sshgen.py        state -> ssh/config, ssh/rules
cubevis/setup/ui.py            say/ask/confirm/changes/record (Configure style)
cubevis/setup/steps/*.py       local, network, node, environment, kernel, check
cubevis/setup/remote.py        run a bash script on a node (bash -s through the wrapper)
cubevis/setup/data/ssh         the wrapper (perl)          } package data
cubevis/setup/data/probe.sh    read-only look at a node    } (from setup/lib/)
cubevis/setup/data/remote-setup.sh   conda + environment  }
cubevis/remote/session.py      Session
```

## 3. State: `~/.config/cubevis/state.json`

One JSON file (readable, diffable, standard library only), written to a
temporary file and renamed. `schema` allows migrations. Passwords are
never in it (they stay in the password stores). Problems are *flagged*,
not deleted: `status`, `last_checked`, `last_error`; `setup prune` offers
to remove what is flagged.

```json
{
  "schema": 1,
  "local": {
    "conda": "/Users/jo/miniforge3/bin/conda", "env": "cubevis",
    "python": "/Users/jo/miniforge3/envs/cubevis/bin/python",
    "store": "keychain", "ssh_include": false
  },
  "networks": {
    "cv": {
      "login": {"host": "login.example.org", "user": "jo", "port": 22,
                "password": true, "service": "cubevis-ssh"},
      "user": "jo", "password": true, "service": "cubevis-ssh",
      "node_pattern": "*.cv.example.org", "domain": "cv.example.org",
      "shared_fs": true, "default_node": "node140",
      "query": {"host": "head1", "command": "squeue -h --me -o %N | xargs -r scontrol show hostnames"}
    }
  },
  "nodes": {
    "node140": {"network": "cv", "hostname": "node140.cv.example.org",
                "alias": "cv-node140", "status": "ok",
                "last_checked": "2026-10-17T16:30", "last_error": null}
  },
  "environments": {
    "cv-main": {"network": "cv",
                "python": "/home/jo/miniforge3/envs/cubevis/bin/python",
                "conda": "/home/jo/miniforge3/bin/conda", "env": "cubevis",
                "cubevis": "1.0.97", "checked_on": "node140",
                "status": "ok", "last_checked": "2026-10-17T16:31"}
  },
  "kernels": {
    "hrs": {"environment": "cv-main", "launch_timeout": 120}
  },
  "sessions": {
    "b3f1": {"kernel": "hrs", "node": "node140", "local_pid": 4242,
             "started": "2026-10-17T16:40", "persistent_file": null}
  }
}
```

* **network**: the login host (one per network; a user can have several
  networks), how nodes are named (`node_pattern` for the wrapper's rules;
  `domain` to complete short names), whether nodes share a filesystem,
  the default node, the optional node query (section 7).
* **node**: a real host in a network; `alias` is its ssh name in the
  generated config (sshpyk requires alias != host name).
* **environment**: a Python on a network's shared filesystem (or on one
  node, when `shared_fs` is false), with what is needed to write upgrade
  commands (`conda`, `env`) and the cubevis version last seen.
* **kernel**: an environment plus launch options. Users name kernels;
  most networks will have one environment and one kernel.
* **session**: written when a Session starts a kernel, removed on clean
  shutdown; what is left over is found by `Session.list()` /
  `setup status` (and later reattached, section 10).

`ssh/config` (one `Host` per node and login host) and `ssh/rules`
(`node_pattern` and the login host per network, so new nodes need no new
rule) are regenerated from this on every change. `~/.ssh/config` is never
needed (optional `Include`, as in the bash setup).

## 4. Session

```python
from cubevis.remote import Session

s = Session("hrs", node="node140")   # kernel name; node optional (default node, or asks)
s                                     # repr: kernel, network, node, state, versions
s.check()                             # health report (section 9), returns a result object
s.start()                             # start the kernel now (else on first use)
vp = VisibilityPlotter(ms="/data/x.ms", session=s)
s.close()                             # shut the kernel down, forget the session record
Session.list()                        # sessions recorded in state.json (live or left over)
Session.attach("b3f1")                # later (section 10)
```

* One Session = one remote kernel = one node. Several local Python
  processes may each have a Session on a different node.
* `VisibilityPlotter(backend="remote", kernel_name=...)` keeps working
  (a plain Jupyter kernel definition, local or sshpyk-made);
  `session=` is the new way and implies `backend="remote"`.
* Whether several plotters in one Python session share one kernel (one
  worker each) is an open question (section 12); stage 3 keeps today's
  one-kernel-per-plotter behaviour behind the Session interface so that
  can change without changing callers.

## 5. Starting a kernel for a session

No kernel definition file per node. The Session builds one in memory and
gives it to jupyter_client through its public trait:

```python
class _SessionSpecs(KernelSpecManager):          # jupyter_client.kernelspec
    def get_kernel_spec(self, name):             # only the session's name
        return KernelSpec(argv=[local_python, "-m", "sshpyk.kernelapp", ...],
                          metadata={"kernel_provisioner": {
                              "provisioner_name": "sshpyk-provisioner",
                              "config": {"ssh": wrapper, "ssh_config": generated_config,
                                         "ssh_host_alias": node.alias,
                                         "remote_python": env.python,
                                         "remote_kernel_name": "python3",
                                         "launch_timeout": ...}}})

AsyncKernelManager(kernel_name=..., kernel_spec_manager=_SessionSpecs(...))
```

**Verified (2026-10-17, ssh test rig, two hops with passwords):** a
remote kernel started and answered from a spec that exists only in
memory (`kernel_name="not-on-disk"`), with the wrapper and the generated
ssh config. In-process, the provisioner named in the metadata does the
launch; the spec's `argv` is not run (it matters only when sshpyk runs as
a standalone kernel app), so the Session can give a harmless one. `RemoteReductionContext`
takes a kernel-manager factory from the Session instead of a bare
`kernel_name`. Before starting: Jupyter's runtime directory exists
(already fixed), the wrapper and generated config exist, the node is not
flagged.

## 6. `python -m cubevis.setup`

Configure style, as in the bash script: each step checks, explains,
lists its changes, asks, makes them, tests; failure offers retry / skip /
quit; every change goes to `changes.log`, every command to `setup.log`.
Plain `python -m cubevis.setup` walks through whatever is not done yet;
sub-commands go straight to one part, with flags pre-filling answers:

```
python -m cubevis.setup                        # walk through what is missing
python -m cubevis.setup status                 # networks, nodes, kernels, sessions, flags
python -m cubevis.setup check [KERNEL|NODE]    # read-only; flags what fails
python -m cubevis.setup network [NAME]         # add/change a network (login host, naming, query)
python -m cubevis.setup node NETWORK [NAME...] # add nodes (typed, or from the query)
python -m cubevis.setup kernel [NAME]          # environment + kernel on a network
python -m cubevis.setup upgrade [KERNEL]       # versions; prints the commands for the node
python -m cubevis.setup prune                  # offer to remove flagged nodes / sessions
```

Steps carried over from `setup/lib/*.sh`: tools check, wrapper install,
password stores (keychain, secret-tool, pass, gpg-file, command; gpg
unlocking in the terminal), login per hop (key first, then password,
shared entries), the optional `Include`, the read-only probe, the
generated per-node `remote-setup.sh` (run over ssh or copied), test data,
and the final kernel + plot test. Conda and the local environment move to
the bootstrap (section 11).

## 7. Finding nodes: an optional query per network

A network may have a query: a host to run it on (reached through the
login host like a node) and a shell command printing node names, one or
more per line. `setup node NETWORK` runs it and offers the names; new
names are added, missing ones are not deleted (flagged only if a check
fails). Nothing about any scheduler is built in; the example in the
prompt and docs is Slurm's
`squeue -h --me -o %N | xargs -r scontrol show hostnames`. Without a
query, names are typed (short names completed with the network's
domain).

## 7a. Login host optional; inside a network, no bastion (2026-10-19)

A network may have no login host: its nodes are reached directly.  When it
has one and also `inside` address ranges, this computer goes to the nodes
directly whenever it is inside the network, decided at each connection by
an OpenSSH `Match ... !exec "<python> -m cubevis.setup.inside NET"` block
that adds the ProxyJump only outside.  Inside means: the local address the
system would use to reach a node (resolved here) is in the ranges -- not
any local address (bridges, home networks).  Details and reasons:
`hrs_h8_consolidation_prep.md` section 28.

## 7b. Roles, hosts and environments (agreed with Darrell 2026-10-20)

Supersedes the parts of sections 3, 6 and 13 (stage 2) that assume setup
runs only where the plots appear and learns about nodes from outside
(`probe.sh`, a generated `remote-setup.sh`).

**Roles.**  Each run of setup is in one role: *GUI* (plots appear here;
kernels are started from here) or *node* (kernels run here).  The role
belongs to the run, not the host: a host may be both (two runs), and a
node of one network may be the GUI host for another.  Setup asks; when
`$SSH_CONNECTION` is set it suggests *node*.

**Node side** (`python -m cubevis.setup node-here [NETWORK]`; the walk-through
asks the role first): run logged in on the node, with a terminal.
Built in stage 2b part ii (`hrs_h8_consolidation_prep.md` section 31).
* Asks only what the node can answer: the network's name and domain, the
  user, an optional node query (tested on the spot).
* Environments: setup *asks* which to use; what it finds (`conda env
  list`, `mamba env list`, an active venv) only pre-fills the choices,
  and the user may type paths or give names.  Kinds: `conda`, `venv`,
  `python` (2026-10-21: `mamba` dropped -- the environments are the same).
  How cubevis was installed in each is *detected* from its packaging
  records (2026-10-22), since that, not the environment's kind, decides
  how it is upgraded: `conda-meta/cubevis-*.json` (a conda package), the
  dist-info's `direct_url.json` (a local checkout, editable or not, or a
  VCS URL) and `INSTALLER` (pip, uv).  Only for a conda package is a
  conda/mamba/micromamba `tool` recorded -- the one that listed the
  environment, else one found; asked only when none is found.
* Each chosen environment is tested by running its interpreter
  with a small script (Python version; cubevis and ipykernel import, and
  their versions); the cubevis running setup need not be in it.  Failures
  are recorded flagged, with the reason.
* **Only configured environments are used.**  A conda environment created
  later is invisible until setup on that host enables it.
* Records the host's addresses (candidate `inside` ranges for the GUI
  side) and writes `hosts/<id>/node/<network>.json`.
* Replaces the generated `remote-setup.sh` and its password plumbing.

**GUI side.**  `network` asks how this computer reaches the network
(directly, or through login host X; password and store), then reads the
nodes' `hosts/*/node/<network>.json` over ssh (one connection reads them all
on a shared home), shows them, and records them.  The probe stays, as the
GUI side's read-only check later (versions, section 8; health, section 9).

**Environments in the state.**  Each environment has `kind`, `python`
(resolved interpreter path; with the host, its identity), what is needed
to activate and upgrade it, and `hosts`: every node (on a shared
filesystem) or a list.  Several per host.  A kernel stays an environment
plus launch options; `Session(kernel, node=)` refuses a node the
environment is not on.  The GUI host's own configured environments are
kernels too, started locally with no ssh.

**An environment belongs to one network** (Darrell, 2026-10-21).  It may be
shared by that network's hosts through their common filesystem; it is not
shared across networks.  So:
* Identity is (network, interpreter path); `hosts` is every node of that
  network or a subset of them.  A kernel inherits its environment's
  network, so a kernel is single-network too.
* `Session(kernel, node=)` refuses a node of another network, before any
  ssh.
* Version checks, upgrade scripts and `prune` work per network: one check
  on any node covers a shared environment.
* The GUI host's own environments belong to a network of their own, *this
  host* (no ssh, no login host).
* Unhappy path, allowed but not helped: the same path visible from two
  networks (a home mounted at both sites) is two environment records, one
  per network, each configured and tested from its own side.  Setup does
  not link them; a version difference shows up in each network's check.

**On-site and off-site** (amends 7a).  The `inside` ranges are a positive
list: only from them does this computer go direct; from any other address
(a visitor network included) it uses the bastion.  Ranges come from the
node files (as candidates, confirmed by the user) and from
`network NET --inside-here`, run on-site, which first checks that a node
can be reached directly from here (at setup time only) and records the
current range only if it can.  No probing when a kernel starts.
Override, read by the inside test before the ranges:
`CUBEVIS_ROUTE_<network>=direct|bastion` (unset: the ranges decide).  ssh
inherits it from the Python process, so setting it before a session
starts is enough; it also applies to a plain `ssh` from that shell.

**Shared `~/.config/cubevis`** (a home on NFS shared by the nodes and
possibly a GUI host).  Files are split by writer, so most have one
(built in stage 2b part i, 2026-10-21; `hrs_h8_consolidation_prep.md`
section 30):

```
state.json                     networks, nodes, environments, kernels (shared; locked, merged)
secrets/                       gpg-file passwords (shared, as ~/.gnupg is)
hosts/<id>/local.json          this computer as GUI: python, store, sessions
hosts/<id>/ssh/{config,rules}  generated per computer (its store; its python runs the inside test)
hosts/<id>/bin/ssh, scp        the wrapper, per computer (computers may run different cubevis)
hosts/<id>/{setup,changes}.log, backups/
hosts/<id>/node/<network>.json this computer as a node: environments, addresses (part ii)
```

* `<id>` is the short host name (first label, lower case; on macOS the
  `LocalHostName`, since `hostname` follows the network);
  `$CUBEVIS_HOST_ID` overrides it.
* ControlMaster sockets cannot live on NFS: they are in
  `/tmp/cubevis-<uid>/cm` (mode 700, owner checked), or
  `$CUBEVIS_RUNTIME_DIR`.  Not `$XDG_RUNTIME_DIR` as first planned: it is
  removed when the last login session ends, under running kernels.  The
  wrapper makes the directory again if a reboot or tmp cleaner removed it.
* Saves take a lock file (`O_EXCL`, holding host, pid, time; stale when
  its process is gone on this computer, or after 120 s -- not `flock`,
  unreliable on older NFS), re-read the file, and apply only what this run
  changed since it loaded: record by record, field by field; a field both
  changed takes this run's value; a removal beats a change to health
  fields only (`status`, `last_checked`, ...), loses to any other change.
* Records are keyed by stable names, so a rerun updates in place.
* Schema 1 (2a: `state.json` with `local` and `sessions`) is migrated on
  load; this computer's part is written to `hosts/<id>/local.json` at the
  first save; loading writes nothing.  The earlier layout's files
  (`ssh/`, `bin/ssh`, `cm/` in the directory itself, also the bash
  setup's) are left alone and named by setup: another computer sharing
  the directory may still use them.
* A computer that finds networks set up by another has no password store
  yet: its rules leave those hosts out (with a note), and the first login
  that needs one asks for the store, keeping the entry's name.

## 8. Versions and upgrades

`check` compares the local cubevis with each environment's (one command
on any node of the network). On a difference it does not change the
remote side; it prints what to run there and writes it as a script beside
the environment's state:

```
# on node140 (or any node of cv):
bash ~/cubevis-upgrade.sh
# which is:
source /home/jo/miniforge3/etc/profile.d/conda.sh && conda activate cubevis
python -m pip install "cubevis[visplot]==1.0.97"
```

`setup upgrade` can copy that script to the node; running it is the
user's step. Downgrades are the same with an older version.  The commands
follow how cubevis was installed there (`cubevis_via`, detected by
`node-here`): a conda package -> the environment's `tool`; pip -> `<python>
-m pip install -U "cubevis[visplot]==V"`; uv -> `uv pip install --python
<python> ...`; a checkout -> update that checkout (setup names the
directory; no package command); a VCS URL -> pip from that URL; unknown ->
say so.  pip-installed cubevis in a conda environment is the common case
and is upgraded with pip.

Open (Darrell, 2026-10-21): changing an environment while a kernel runs in
it can break that kernel.  Setup will at least say so -- before printing
upgrade commands, it lists the sessions it knows of using that
environment (this computer's records; other computers' are not visible).
Whether to go further -- a lock that cubevis holds on an environment while
kernels use it, which upgrades respect -- is a slippery slope (who
releases it after a crash; other installers ignore it) and is not planned.

## 9. Health and diagnostics

`Session.check()` and `setup check` share one checker, returning
structured results (and printing PASS/FAIL lines in setup):

1. the generated files and wrapper exist; the node is known and not
   flagged;
2. each hop: the shared connection is up, or a login works (stored
   password, or key) -- the wrapper's `--explain` gives the detail;
3. the environment's Python runs on the node and imports cubevis; the
   versions match (or a note);
4. for a started session: the kernel answers, round-trip time, the
   remote host name.

Failures update the node's or environment's `status` / `last_error`.

## 10. Detach and reattach (later stage)

sshpyk keeps a `persistent_file` per kernel and can reattach with
`AsyncKernelManager(kernel_name=..., existing=...)`;
`cubevis/remote/_persistence.py` already records such kernels by label.
The session record (section 3) is that label plus node and kernel. After
the laptop moves networks the ssh connections are gone but the remote
kernel can live on: `Session.attach(id)` logs in again (the wrapper) and
reattaches. Needs: `persistent=True` launches, the session record kept on
`detach()`, and a visplot plotter that can re-bind to a reattached kernel.

## 11. Bootstrap

The local half of the bash script becomes a short bash bootstrap,
published with each release (no checkout): find or install Miniforge
(no `conda init`), create the `cubevis` environment, `pip install
"cubevis[visplot]==<release>" sshpyk`, write `~/.config/cubevis/activate`,
then run `python -m cubevis.setup`. Someone with a suitable Python
already just does the pip install.

## 12. Open questions

* Several plotters in one Python session: one kernel with a worker each
  (needs a look at how `RemoteReductionContext` spawns its worker), or a
  kernel each as today?
* Name the user sees: "kernel" (familiar from Jupyter) for what is really
  environment + options -- keep environments mostly hidden, or show both?
* Networks without a shared filesystem: an environment per node (allowed
  by `shared_fs: false`), set up node by node.
* The bash setup's testers' state (`~/.config/cubevis/hosts/*.conf`):
  import it once, or start fresh.

## 13. Stages

Each stage is one delivery (zip on the then-current main), with tests.

1. **State and generation** (`cubevis/setup/state.py`, `sshgen.py`, data
   files moved into the package). Unit tests: schema, atomic save,
   migration hook, flagging, ssh config/rules output (checked with
   `ssh -G`), wrapper `--explain` against generated files. Wrapper fix
   found while verifying section 5: when its own login fails it prints a
   gpg hint whatever the cause (here the host name did not resolve); keep
   the throwaway login's stderr and show its last line, and give the gpg
   hint only when the password lookup itself failed.
2. **`python -m cubevis.setup`**: 2a (done): `ui.py`, this computer,
   networks, nodes, logins, inside ranges, `status`, `check`.  2b
   (revised 2026-10-20, section 7b): (i) the file layout, locking and
   migration, `cm/` on local disk; (ii) environment records (kinds,
   hosts) and the node-side command; (iii) the GUI side reading
   `remote/` files, `--inside-here`, `CUBEVIS_ROUTE_<net>`; (iv) versions
   and upgrade scripts, the node query, `prune`, the final kernel + plot
   test.  pexpect-driven tests on the ssh test rig, including two
   "hosts" sharing one config directory.
3. **Session**: `cubevis/remote/session.py`, in-memory kernel spec,
   `RemoteReductionContext` factory, `VisibilityPlotter(session=)`,
   `check()`. Tests: local kernel; the rig with two nodes from one
   environment at once, from two Python processes.
4. **Bootstrap and docs**: the bash bootstrap; `00_setup.md`, demos with
   `--session` / `--node`; remove `setup/` (bash) and its references.
5. **Detach / reattach** (section 10), when wanted.
