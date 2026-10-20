# cubevis-setup

Sets up cubevis on your computer and, if you like, on remote hosts: the
machines that hold your data, where visplot's data side runs in a Jupyter
kernel (`backend="remote"`). It walks you through each step, says what it is
about to change, asks, makes the change, and tests it before going on.

```
bash setup/cubevis-setup             # set up, or carry on where it stopped
bash setup/cubevis-setup --check     # test everything; change nothing
bash setup/cubevis-setup --status    # what is set up; every change made
bash setup/cubevis-setup --help      # the other options
```

It runs from a cubevis checkout, wherever that is, with the bash that comes
with macOS (3.2) or Linux. It needs `perl`, `curl` and OpenSSH 8.0 or later
(all part of macOS and of most Linux systems), and no administrator rights.
Python is not needed beforehand: setup installs it (with conda) if there is
none.

Run it again whenever you like. Steps that pass are not repeated; a remote
host whose setup stopped part way is picked up where it stopped; and you can
add more remote hosts.

## What it does

On your computer:

| Step | What happens | What changes |
|---|---|---|
| tools | checks for perl, curl, OpenSSH 8.0+ | nothing |
| conda | uses a conda you have (Miniforge, Miniconda, Anaconda), or installs Miniforge in a directory you choose | that directory only: **no** changes to your shell start-up files, so no `(base)` in your prompt |
| environment | creates the conda environment `cubevis` (conda-forge packages), then `pip install cubevis[visplot]==<release> sshpyk` | the environment; `~/.config/cubevis/activate` |
| ssh wrapper | copies `cubevis/setup/data/ssh` to `~/.config/cubevis/bin/ssh` | that file. It is **not** put on your PATH: your own `ssh`, `scp` and `git` are unchanged |

For each remote host:

| Step | What happens | What changes |
|---|---|---|
| names | the host's real name, your user name, the login host (gateway) it is reached through, if any; the ssh names (aliases) setup gives them | `~/.config/cubevis/hosts/<alias>.conf`; the generated `ssh/config` and `ssh/rules` |
| log in | tries an ssh key first; if the host wants a password, stores it in a password store (below) and logs in once with it | the password store; `ssh/rules` |
| terminal (once, optional) | adds `Include ~/.config/cubevis/ssh/config` at the top of `~/.ssh/config`, so `ssh <alias>` works in a terminal too | two marked lines in `~/.ssh/config` (backed up first). Not needed for visplot |
| look | runs a script on the host that changes nothing: system, home directory, conda, whether it can download, whether your home directory is shared with this computer | nothing |
| environment there | writes a script for that host (`hosts/<alias>/remote-setup.sh`) and runs it there over ssh, or copies it for you to run | on the host: Miniforge (if no conda) and the `cubevis` environment; nothing else |
| data | downloads a small test measurement set on the host, or uses one you name (or skip) | on the host: that directory |
| kernel | `sshpyk add`: a Jupyter kernel definition that starts Python on the host through the wrapper | a directory in your Jupyter kernels (`sshpyk list` shows it) |
| test | starts the kernel, and draws a plot with it | `hosts/<alias>/test.png` |

Every change is written to `~/.config/cubevis/changes.log` (with the
backup's name for any file of yours it changed); `--status` prints it.

### Your ~/.ssh/config

Setup never needs it. The kernels are given setup's own file
(`sshpyk add --ssh-config ~/.config/cubevis/ssh/config`), which holds only
the hosts setup made, under names nobody else uses (setup checks), so your
own configuration can neither break them nor be changed by them.

If you want the same names in a terminal, say yes to the one optional step:
it adds two lines at the top of `~/.ssh/config`, marked `cubevis-setup`.
(It rewrites the file in place, so a symbolic link to a dotfiles copy stays
a link.) To undo it, delete those two lines.

### Passwords

Some sites ask for a password at their login host, and sometimes at the
hosts behind it, even when you have an ssh key. A kernel is started by ssh
with no terminal, so nobody could type it. The wrapper (`bin/ssh`) logs in
once per host with the password from a password store, and ssh then shares
that connection (ControlMaster) with everything that follows, for 8 hours.
A stored password is tried once per login, so a wrong one cannot lock your
account by itself. Hosts that take your ssh key need none of this.

The password store is chosen the first time a host needs one:

| Store | Where | Notes |
|---|---|---|
| `keychain` | macOS | the login keychain (`security`) |
| `secret-tool` | Linux desktop | GNOME Keyring or KWallet; needs a desktop session (not over plain ssh) |
| `pass` | Linux | if you use `pass` already, keep using it; setup can create the store |
| `gpg-file` | anywhere gpg is | one encrypted file per password in `~/.config/cubevis/secrets` |
| `command` | anywhere | your own command that prints the password (a password manager's CLI) |

On Linux, setup suggests what is already in use: `pass` if you have a
password store, otherwise `secret-tool` when a desktop keyring is running,
otherwise `pass` or `gpg-file`. With the two gpg-based stores the wrapper
can read a password only while gpg-agent holds your gpg passphrase; on a
desktop gpg asks in a window when needed, otherwise run
`bash setup/cubevis-setup --check` in a terminal first (it asks).

Hosts with the same password share one stored entry (setup asks).

## Where things are

Everything setup keeps is in `~/.config/cubevis` (`$XDG_CONFIG_HOME/cubevis`):

```
setup.conf          answers about this computer (conda, environment, store)
hosts/<alias>.conf  one per ssh host (login hosts too)
hosts/<alias>/      that host's look-around result, its remote-setup.sh
                    and log, the test plot
ssh/config          the generated ssh configuration (do not edit: re-run)
ssh/rules           the generated wrapper rules (do not edit: re-run)
bin/ssh, bin/scp    the wrapper
cm/                 ssh's shared-connection sockets
activate            . ~/.config/cubevis/activate  -- the environment
changes.log         every change, with backups' names
backups/            copies of files of yours before setup changed them
setup.log           every command setup ran, with its output
```

## Using it

```
. ~/.config/cubevis/activate
cd demos/visplot-remote
python 03_remote_gui.py --kernel <kernel> --remote-ms /path/on/the/host/data.ms
```

or in your own code, `VisibilityPlotter(ms=..., backend="remote",
kernel_name="<kernel>")`. setup prints the right line at the end.

## Taking it away

* a remote host: `bash setup/cubevis-setup --remove-host <alias>` (its ssh
  entries and kernel definition; the stored password and anything on the
  host stay);
* everything else: `changes.log` lists it -- the conda environment
  (`conda env remove -n cubevis`), Miniforge if setup installed it (its
  directory), the two lines in `~/.ssh/config`, the stored passwords, and
  `~/.config/cubevis`.

## When something fails

Each step says what failed and offers to retry, skip, or quit. More detail:

* `~/.config/cubevis/setup.log`: every command and its output;
* `~/.config/cubevis/bin/ssh --explain -F ~/.config/cubevis/ssh/config <alias>`:
  each hop, the rule it matches (or why none does), whether a password is
  stored, whether a shared connection is up;
* `SSH_WRAPPER_DEBUG=1` logs the wrapper to `~/.ssh/ssh-wrapper.log`;
* `CUBEVIS_DEBUG=1` logs visplot's side.

A host that asks for something other than a password (a one-time code, for
example) cannot be logged in to by the wrapper.

## For developers

`cubevis-setup` is the order of the steps; the steps are in `lib/`:

```
lib/common.sh        messages, questions, state files, the change log, run_step
lib/versions.sh      what is installed: release, conda and pip packages, URLs
lib/local.sh         steps on this computer
lib/store.sh         password stores
lib/hosts.sh         ssh hosts, the generated ssh files, logging in
lib/remote.sh        steps for a remote host
lib/check_kernel.py  the last test
```

Since 2026-10-18 three files are package data, shared with
`python -m cubevis.setup` (which replaces this script):

```
../cubevis/setup/data/probe.sh         runs on a remote host: looks, changes nothing
../cubevis/setup/data/remote-setup.sh  runs on a remote host: conda and the environment
../cubevis/setup/data/ssh              the ssh wrapper (perl)
```

A step `foo` is three functions: `foo_title` (one line), `foo_check`
(returns 0 when the step is done; asks nothing, changes nothing; leaves a
one-line result in `CHECK_MSG`) and `foo_do` (explains, lists the changes
with `changes_list`, asks, makes them, calls `record_change` for each).
`run_step foo` runs the check, and `_do` only if it fails, then the check
again; `--check` runs only checks, `--redo` runs every `_do`. To add a step,
write the three functions and add its name to `LOCAL_STEPS` (local.sh) or
`REMOTE_STEPS` (remote.sh). A step that only looks goes in `LOOK_STEPS`
(common.sh).

New release: `CV_RELEASE_DEFAULT` in `lib/versions.sh` (a checkout at tag
`vX.Y.Z` installs `X.Y.Z` anyway).

Keep it bash 3.2: no associative arrays, `${x,,}`, `mapfile`, `|&`, `;&`.
The state files are `key=value` lines read by `conf_get`, never sourced.
Remote commands go through `remote_bash`, which runs bash there whatever the
login shell is, with values quoted by `sq`.
