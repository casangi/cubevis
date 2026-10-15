# Setting up visplot with a remote kernel

This is the one setup we support for running visplot's data side on another
machine: the machine that holds the data, reached through a login host. It
works when the login host asks for your password every time, even with an
SSH key installed, and when the machine behind it does too. It needs no
administrator rights on either side.

`00_remote_setup.py` (next to this file) asks for your host names and prints
every command below with your values filled in, and `--check` tests each step.
Read this page once first; then use whichever you prefer.

## The pieces

```
 your computer                     login host            remote host
 ─────────────                     ──────────            ───────────
 visplot (browser window)  ──ssh──▶  (password)  ──ssh──▶  Jupyter kernel
 sshpyk  (starts the kernel)                               visplot data side
 ssh wrapper (types the password, once)                    your measurement sets
```

* **Your computer** (macOS or Linux) runs the visplot window and `sshpyk`,
  which starts a Jupyter kernel on the remote host over ssh.
* **The login host** (also called a bastion or gateway) is the only machine
  you can reach from outside. You never run anything there.
* **The remote host** holds the data and runs the kernel.

sshpyk runs ssh with no terminal, so a password prompt has nobody to answer
it. The **ssh wrapper** (`bin/ssh`) logs in to each host that asks for a
password once, using the password stored in your keychain, and leaves a
*shared connection* open that every later ssh command reuses.

## What you need to know first

| | Example | Yours |
|---|---|---|
| Login host | `login.example.org` | |
| Remote host | `compute1.example.org` | |
| Your user name there | `jsmith` | |
| Which of them ask for a password | both | |
| Where the data will live on the remote host | `/home/jsmith/visplot-data` | |

Below, `<login>`, `<remote>` and `<user>` stand for these. The ssh *aliases*
used below are short names you choose in step 5: `vp-login` and `vp-remote`.

**Two home directories.** Your home directory on the remote host is normally
not the one on your computer: different machines, different files, often a
different path (`/Users/jsmith` on a Mac, `/home/jsmith` there). Each step
says where it runs, and a path printed on the remote host is a path *there*;
`~` in a command means the home directory of the machine it runs on. Some
sites share one home directory between your workstation and their compute
hosts; see the note in step 1.

## 1. conda, on your computer and on the remote host

You need conda with the conda-forge channel on both machines. **If you
already have conda** (Miniforge, Miniconda, Anaconda, or mamba), skip this
step: step 2 asks for conda-forge explicitly.

Otherwise install Miniforge, a conda installation in your home directory that
uses conda-forge by default and needs no administrator rights. On **your
computer**, and again on **the remote host** (log in by hand: `ssh -J
<user>@<login> <user>@<remote>`):

```
curl -L -O "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh"
bash "Miniforge3-$(uname)-$(uname -m).sh" -b -p "$HOME/miniforge3"
"$HOME/miniforge3/bin/conda" init "$(basename "$SHELL")"
```

Open a new terminal (or log in again) so that `conda` is on your path. This
page uses `conda` throughout; `mamba` (which Miniforge also installs) takes
the same commands, but needs its own `mamba shell init` before `mamba
activate` works, so there is no need for both.

**If your computer and the remote host share your home directory** (check:
a file you create in `~` on one appears in `~` on the other), install once,
from the machine whose system is the remote host's (for example both Linux
x86_64): one installation and one environment then serve both sides, and
step 2 is done once. If the two systems differ (a Mac and a Linux host),
give the remote host's installation its own directory, e.g.
`-p "$HOME/miniforge3-$(uname -s)-$(uname -m)"` in the second command, and
use that path instead of `$HOME/miniforge3` on the remote host.

## 2. The visplot environment, on both sides

The same environment on both sides, with the same cubevis release: visplot
compares the two versions when it connects and warns if they differ.

On **your computer** and on **the remote host**:

```
conda create -y -n visplot -c conda-forge python=3.12 pip ipykernel \
      jupyter_client scipy regions 'zarr>=3' datashader certifi
conda activate visplot
pip install "cubevis[visplot]==1.0.96"
```

On **your computer** only, add sshpyk and get the demos (this also gives
you the ssh wrapper):

```
pip install sshpyk
git clone --depth 1 --branch v1.0.96 https://github.com/casangi/cubevis.git ~/cubevis
```

On **the remote host**, print the environment's Python and keep the path:
step 7 tells your computer where to find it.

```
conda activate visplot && python -c "import sys; print(sys.executable)"
# e.g. /home/jsmith/miniforge3/envs/visplot/bin/python  (a path on the remote host)
```

## 3. The ssh wrapper, on your computer

```
mkdir -p ~/bin
cp ~/cubevis/demos/visplot-remote/bin/ssh ~/bin/ssh
chmod +x ~/bin/ssh
ln -sf ssh ~/bin/scp
```

`~/bin` must come before the system directories in your `PATH`. Add this
line at the **end** of `~/.zshrc` (macOS) or `~/.bashrc` (Linux), so it also
comes before the conda environment's directory:

```
export PATH="$HOME/bin:$PATH"
```

Open a new terminal, activate the environment, and check:

```
conda activate visplot
command -v ssh          # must print /Users/<you>/bin/ssh or /home/<you>/bin/ssh
ssh -V                  # OpenSSH 8.4 or later
```

## 4. Your password, on your computer

The wrapper reads the password from a password store; it is never kept in a
plain-text file. Pick a name for the entry, e.g. `remote_ssh_pw`, and one
store:

| Store | Use it on | Rules-file keys (below) |
|---|---|---|
| keychain | macOS | `service=remote_ssh_pw backend=security` |
| secret-tool | Linux with a desktop keyring (GNOME, KDE) | `service=remote_ssh_pw backend=secret-tool` |
| pass | Linux, any (also without a desktop) | `service=remote_ssh_pw backend=pass` |
| gpg file | Linux, when neither of the above is installed | `command=gpg -q -d ~/.ssh/remote_pw.gpg` |

**keychain** (macOS; asks for the password; `-U` replaces an existing entry):
```
security add-generic-password -a <user> -s remote_ssh_pw -w
```

**secret-tool** (asks for the password):
```
secret-tool store --label='remote ssh password' service remote_ssh_pw account <user>
```

**pass**, the standard Unix password manager: one gpg-encrypted file per
entry, under `~/.password-store`. Install it with your system's package
manager (`pass` on Debian/Ubuntu and Fedora, and in Homebrew), or without
administrator rights from its source:
```
git clone https://git.zx2c4.com/password-store ~/src/password-store
make -C ~/src/password-store install PREFIX="$HOME/.local"   # then ~/.local/bin on PATH
```
Then, once, a gpg key and a password store, and the entry:
```
gpg --quick-generate-key "<user> ssh password"     # once, if you have no gpg key
pass init "<user> ssh password"                    # once, if you have no password store
pass insert remote_ssh_pw                          # asks for the password
```

**gpg file** (a single encrypted file, no extra tools):
```
gpg --quick-generate-key "<user> ssh password"     # once, if you have no gpg key
gpg -q -e -r "<user> ssh password" -o ~/.ssh/remote_pw.gpg
# type the password, then Enter and Ctrl-D (so it is not in your shell history)
```

With **pass** and the **gpg file**, the gpg key's passphrase protects the
stored password. gpg-agent remembers it for a while after you first type it
(e.g. after `pass show remote_ssh_pw` in a terminal), and the wrapper can
read the password only while it does. To make that last a working day, add
`default-cache-ttl 28800` and `max-cache-ttl 28800` to
`~/.gnupg/gpg-agent.conf`.

Then tell the wrapper which hosts need the password. Create
`~/.config/ssh-wrapper/rules` with the **real host names** (not the aliases),
listing only hosts that ask for a password, and the keys for your store from
the table:

```
mkdir -p ~/.config/ssh-wrapper
cat > ~/.config/ssh-wrapper/rules <<'EOF'
<login>,<remote>   service=remote_ssh_pw backend=security
EOF
```

A pattern such as `*.example.org` covers several hosts; make sure it does not
cover a git server or other host you reach with a key only.

## 5. Your ssh configuration, on your computer

Add to `~/.ssh/config` (create it if needed; `chmod 600 ~/.ssh/config`):

```
Host vp-login
    HostName <login>
    User <user>

Host vp-remote
    HostName <remote>
    User <user>
    ProxyJump vp-login

Host vp-login vp-remote
    ControlMaster auto
    ControlPath ~/.ssh/cm_%r@%h_%p
    ControlPersist 8h
    ServerAliveInterval 30
    ServerAliveCountMax 6
```

`ControlPath` and `ControlPersist` are what keep the shared connection open;
without them every command would need the password again. Keep settings such
as `ForwardX11 yes` out of a `Host *` section: they slow down or break
non-interactive connections (sshpyk, git).

## 6. Check ssh, on your computer

```
ssh vp-remote hostname
```

The first time, expect:

```
[ssh-wrapper] logging in to 'vp-login' (<login>) for a shared connection
[ssh-wrapper] shared connection to 'vp-login' is up
[ssh-wrapper] logging in to 'vp-remote' (<remote>) for a shared connection
[ssh-wrapper] shared connection to 'vp-remote' is up
<remote's host name>
```

Run it again: only the host name, no `[ssh-wrapper]` lines. `ssh -O check
vp-remote` says `Master running`. If you are asked whether to trust a host's
key, answer `yes` once by hand (`ssh vp-login true`, then `ssh vp-remote
true`). If a login fails, see *Problems* below.

## 7. The kernel definition, on your computer

```
sshpyk add --ssh-host-alias vp-remote \
           --kernel-name vp_remote \
           --display-name "visplot on <remote>" \
           --remote-python <the python from step 2> \
           --remote-kernel-name python3 \
           --language python
sshpyk list --remote
```

In the `sshpyk list` output for `vp_remote`, look for `(v)` (verified) on
**SSH Path** (your `~/bin/ssh`), **SSH Connection**, **Remote Python** and
**Remote Kernel Name**, and `controlmaster: auto`, `controlpersist` and
`controlpath` on both host aliases. An `(!) identityfile` warning is expected
when you log in with a password and can be ignored.

## 8. Test data, on the remote host

```
mkdir -p ~/visplot-data && cd ~/visplot-data
curl -L -O https://casa.nrao.edu/download/devel/casavis/data/sis14_twhya_calibrated_flagged.ms.tar.gz
tar xzf sis14_twhya_calibrated_flagged.ms.tar.gz
pwd          # the absolute path, for step 9
```

(443 MB; it unpacks to `sis14_twhya_calibrated_flagged.ms`.)

## 9. visplot, on your computer

A file first, without a window:

```
conda activate visplot
python - <<'EOF'
from cubevis.toolbox.visplot import VisibilityPlotter
vp = VisibilityPlotter(ms="<path from step 8>/sis14_twhya_calibrated_flagged.ms",
                       field="3c279", headless=True,
                       backend="remote", kernel_name="vp_remote")
print("version warning:", vp._version_warning)     # None: both sides match
print(vp(plotfile="remote_test.png", theme="light"))
vp.close()
EOF
```

The MS path is the absolute path **on the remote host** (`~` is not
expanded). Open `remote_test.png`: a raster and a scatter of the 3c279
calibrator. `python 00_remote_setup.py --check --kernel` does the same and
checks every step before it.

Then the window, and the guided demos:

```
cd ~/cubevis/demos/visplot-remote
python 03_remote_gui.py --remote-ms <path from step 8>/sis14_twhya_calibrated_flagged.ms \
       --kernel vp_remote --field 3c279
```

`README.md` here describes demos 01–03.

## Problems

| What you see | What to do |
|---|---|
| `command -v ssh` is not `~/bin/ssh` | The `PATH` line must come after the conda lines in your shell start-up file; open a new terminal. |
| `login to 'vp-login' failed` | Check the stored password (step 4). The wrapper tries once only, so a wrong password cannot lock your account. Try `ssh vp-login true` in a terminal: ssh will ask you. |
| `no password found for ...` | The rules' `service=` and the store's entry must match (keychain and secret-tool: also the account, which defaults to `User` in `~/.ssh/config`). With pass or a gpg file: run `pass show remote_ssh_pw` (or `gpg -d ...`) in a terminal once, so gpg-agent has the key's passphrase. |
| `needs a password but has no ControlPath` | Step 5: the `Host vp-login vp-remote` section is missing or names different aliases. |
| `logged in ... but no shared connection stayed up` | `ControlPath`'s directory must exist (`~/.ssh`); `ControlPersist` must not be `no`. |
| sshpyk hangs or times out starting the kernel | Run step 6 again; then `sshpyk list --remote`. `SSH_WRAPPER_DEBUG=1` logs every step to `~/.ssh/ssh-wrapper.log`. |
| `version warning:` is not `None` | Install the same cubevis release on both sides (step 2). |
| `ModuleNotFoundError` in the kernel | The kernel definition points at a Python without the environment: check `--remote-python` (step 7). |

After a network interruption the shared connections close; the next ssh
command (or sshpyk) logs in again by itself.
