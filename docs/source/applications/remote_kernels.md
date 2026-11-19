# Setting up remote execution

`visplot` and interactive clean can do their work on the data — reading
the measurement set, running `tclean`, reading and writing images and
masks — in a Jupyter kernel on the computer that holds the data, while the
window stays on your own computer. Only what the window shows crosses the
network.

This chapter covers setting that up with `python -m cubevis.setup`. Once a
kernel exists, using it is one argument:

```python
from cubevis import visplot, iclean

visplot(vis="sis14_twhya_calibrated_flagged.ms", kernel_name="cvpost106-visplot")
iclean(vis="sis14_twhya_calibrated_flagged.ms", imagename="twhya", ...,
       kernel_name="cvpost106-iclean")
```

Paths (`vis`, `imagename`) are the kernel's: relative paths are found in,
and images are written to, the kernel's working directory on that
computer.

## What you need

- On your computer: `pip install "cubevis[remote]"` (the `visplot` and
  `iclean` extras include it).
- A computer that holds the data and that you can reach with `ssh`,
  directly or through a login host.

Nothing needs to be installed on the remote computer beforehand. Setup
finds the Python environments there that have `cubevis` and makes one when
there is none.

## First-time setup

Run setup from your own computer, naming the computer the data is on as
you would for `ssh`:

```console
$ python -m cubevis.setup cvpost106
```

Without a host name, setup asks for one. It asks before each change, says
what it will change, and tests each step before going on. Pressing Enter
accepts the answer shown in `[brackets]`.

Setup works through these steps:

1. **How to reach the host.** The route is taken from your own ssh
   configuration. A `ProxyJump` there is used as the login host; otherwise
   setup tries a direct login and asks whether a login host is needed.
2. **Logging in**, to the login host and then to the host itself. Where a
   host asks for a password, setup stores it in a password store on your
   computer (the macOS Keychain, the Linux desktop keyring, `pass`, or a
   gpg-encrypted file) so that kernels can be started without typing it.
   Passwords are never written to setup's own files or logs.
3. **Looking around** the host, read-only: its name and home directory,
   its conda tools, and its Python environments, each checked for
   `cubevis`.
4. **A cluster?** If the host looks like one node of a cluster
   (`cvpost106` suggests `cvpost*`), setup asks once. A cluster is set up
   once for all its nodes.
5. **The environment** the kernel runs in. Setup lists the environments
   that can be used and, when there is none, offers to make one: a conda
   environment with `cubevis[visplot,iclean]`. Setup never installs into
   an environment of yours.
6. **The kernel**: its name, the directory it starts in, and any
   environment variables. Setup writes the kernel's launcher on the host
   and runs it once before recording it.

At the end setup prints how to use the kernel:

```text
kernel cvpost106-iclean (on cvpost106) runs visplot, interactive clean:

  from cubevis import iclean
  iclean(vis="...", imagename="...", ..., kernel_name="cvpost106-iclean")
```

:::{note}
Screenshots of a complete setup session will be added here.
:::

### Logins that ask for a second factor

If a host's login asks for a code or a push notification, say so:

```console
$ python -m cubevis.setup cvpost106 --2fa
```

No password is stored for such a host. You log in once in a terminal and
the connection is shared by what follows.

## What a kernel can run

Each application needs its own packages in the kernel's environment:

- `visplot` needs `cubevis[visplot]`;
- interactive clean needs `cubevis[iclean]` and CASA's data (below).

Setup checks this when it tests a kernel and records the result
(`python -m cubevis.setup status` shows it). Each connection checks again:
a kernel whose environment lacks an application's packages says so when
you start the application, with the `pip install` that adds them. An
environment that setup made can have interactive clean added to it; a
rerun of `python -m cubevis.setup HOST` offers that.

### CASA's data

Interactive clean needs CASA's data (the measures). When the kernel's
environment finds it — the `measurespath` in your `~/.casa/config.py`, a
site configuration, or `~/.casa/data` — setup asks whether to use it or
to give the environment a private copy (the `casadata` package, about
850 MB, never updated), so that cleaning there does not change when your
own data is updated. When none is found, an environment that setup made
gets the private copy; for an environment of yours setup says what to do.

## Checking and changing a setup

Setup can be run again at any time. Nothing that passes is repeated.

| Command | What it does |
|---|---|
| `python -m cubevis.setup status` | Show what is set up: networks, nodes, environments, kernels, and what each kernel can run |
| `python -m cubevis.setup check [NAME]` | Test the logins and kernels and change nothing. `NAME` is a kernel, node, cluster or network; without it everything is tested |
| `python -m cubevis.setup HOST` | Check the host's kernels and offer another one |
| `python -m cubevis.setup HOST --kernel NAME` | Add kernel `NAME` on the host, or change it |
| `python -m cubevis.setup kernel NAME --workdir DIR` | Change the directory a kernel starts in (`~` is your home there) |
| `python -m cubevis.setup kernel NAME --env "OMP_NUM_THREADS=8"` | Set a kernel's environment variables; `none` clears them |
| `python -m cubevis.setup kernel NAME --environment ENV` | Move a kernel to another environment |
| `python -m cubevis.setup HOST --cleanup` | List the environments setup made on the host and remove the ones you choose. Your own environments are never listed, and one a kernel uses is never removed |
| `python -m cubevis.setup --help` | Every command and option |

Adding `--redo` asks again even for steps that pass.

## When something does not work

Start with `check`, which tests each login and kernel and reports the
first thing that fails:

```console
$ python -m cubevis.setup check
```

If the login itself is the problem, `diagnose` shows how ssh reaches the
host next to what setup has configured. It makes one login attempt and
changes nothing:

```console
$ python -m cubevis.setup diagnose cvpost106
```

Other situations:

- **Your ssh configuration changed** since setup recorded it. `status`
  says so, and `python -m cubevis.setup HOST` (or `network NAME`) asks
  whether to take the change over.
- **A laptop that is sometimes inside the network** and sometimes outside.
  Setup records which address ranges count as inside, and goes through
  the login host only from outside. To add where the computer is now,
  after checking that the nodes answer directly from there:

  ```console
  $ python -m cubevis.setup network NAME --inside-here
  ```

- **Python starts slowly on the host** (an automounted filesystem):
  `python -m cubevis.setup HOST --pythonhome` makes its kernels set
  `PYTHONHOME`, so that Python skips the search for its installation.
  `diagnose HOST --full` times the start.

## What setup writes, and where

On your computer everything is under `~/.config/cubevis` (or
`$XDG_CONFIG_HOME/cubevis`):

| File | Contents |
|---|---|
| `state.json` | Networks, clusters, nodes, environments and kernels. This directory may be a home directory shared by several computers |
| `hosts/<computer>/` | What belongs to one computer: the generated ssh configuration, the ssh wrapper, `changes.log` (every change setup made) and `setup.log` (every command) |

The sockets of shared ssh connections are kept on local disk, in
`/tmp/cubevis-<uid>`.

On the remote computer setup writes only the kernels' launchers
(`~/.config/cubevis/launch/`) and, when it makes one, its own Python
environment, with its build logs in `~/.config/cubevis/make/`.

## Interactive clean on a remote kernel

- The parameters are checked on the kernel's computer, as `tclean`'s task
  checks them: a path that is not there (relative to the kernel's working
  directory) is reported with that computer's name and directory.
- The images are best written in the kernel's working directory (a
  relative `imagename`). With current casatasks, `tclean`'s interactive
  driver looks for the residual and mask there, and an `imagename`
  elsewhere fails at the first cycle.
- While a cycle runs, the status line shows its progress, read from
  `tclean`'s log on the kernel's computer: the elapsed time, the minor or
  major cycle, channels, iterations and the peak residual. The whole text
  is its tooltip.
- Stop takes effect after the running cycle; `tclean` cannot be
  interrupted within one.
- When the window is closed, or Python ends, the remote session is
  closed: the kernel and its worker stop. The images stay on that
  computer.
