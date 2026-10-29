# The ssh wrapper

`ssh` in this directory is a small Perl script that stands in for `ssh` and
`scp`.  It is for sites whose login host -- and sometimes the hosts behind it
-- ask for a typed password even when you have an ssh key installed.  It
does two things:

* **Passwords from your keychain.**  The first time you connect to such a
  host, the wrapper logs in once with the password stored in your keychain
  (or another password store) and leaves that login open as a *shared
  connection* (ssh's ControlMaster).  Every later `ssh`, `scp`, `rsync`,
  `git` or editor connection to that host rides on it: no prompt, for hours.
  Tools that run ssh with no terminal -- a remote Jupyter kernel, a script,
  a cron job -- work too, instead of hanging at a prompt nobody can answer.
* **Inside or outside, decided for you.**  Optionally, ssh can go straight
  to a cluster node when your computer is on the site's network (or its
  VPN), and through the login host when it is not -- the laptop that moves
  between office, home and travel does the right thing with no change of
  configuration (see [Inside and outside](#inside-and-outside)).

For hosts not listed in its rules it does nothing at all: it runs the real
`ssh`/`scp` unchanged.  It never alters the command you typed.

cubevis uses its own private copy (`python -m cubevis.setup` installs it
under `~/.config/cubevis/`); this page is about using it yourself, as your
`ssh` and `scp`, with or without cubevis.

## What you need

* macOS or Linux, with `perl` (present on both) and OpenSSH 8.0 or later
  (`ssh -V`).
* A password store: the macOS Keychain; on Linux the desktop keyring
  (`secret-tool`, from libsecret) or `pass`; or any command that prints the
  password (a password manager's CLI).

## Set up

The example is NRAO's: the login host `ssh.cv.nrao.edu`, cluster nodes
`*.cv.nrao.edu`.  Use your own site's names.

**1. Install the wrapper as `ssh` and `scp` in `~/bin`:**

```sh
mkdir -p ~/bin ~/.config/ssh-wrapper ~/.ssh/cm && chmod 700 ~/.ssh/cm
curl -fsSL https://raw.githubusercontent.com/casangi/cubevis/main/cubevis/setup/data/ssh -o ~/bin/ssh
chmod 755 ~/bin/ssh && ln -sf ssh ~/bin/scp
```

`~/bin` must come before `/usr/bin` in your `PATH` -- for zsh (macOS) add
`export PATH="$HOME/bin:$PATH"` to `~/.zshrc`, for bash to `~/.bashrc`, and
open a new terminal.  `command -v ssh` should then print `.../bin/ssh`.

**2. Say which hosts ask for the password** (one line per site):

```sh
echo 'ssh.cv.nrao.edu,*.cv.nrao.edu   service=nrao_ssh_pw' > ~/.config/ssh-wrapper/rules
```

**3. Store the password once**, under that `service` name and your user name
on those hosts:

```sh
# macOS (prompts for the password; add -U to replace one stored before)
security add-generic-password -a YOUR_USER -s nrao_ssh_pw -w
# Linux, desktop keyring
secret-tool store --label='NRAO ssh' service nrao_ssh_pw account YOUR_USER
```

**4. Let ssh share the login**, in `~/.ssh/config` (skip if you already set
a `ControlPath` for these hosts):

```
Host *.cv.nrao.edu
    ControlPath ~/.ssh/cm/%C
```

**5. Check:** `ssh --explain ssh.cv.nrao.edu` -- it logs in nowhere, and
prints each hop, the rule it matches, whether the password was found,
whether a `ControlPath` is set and whether a shared connection is up.  Then
`ssh ssh.cv.nrao.edu` logs in without asking:

```
[ssh-wrapper] logging in to 'ssh.cv.nrao.edu' (ssh.cv.nrao.edu) for a shared connection
[ssh-wrapper] shared connection to 'ssh.cv.nrao.edu' is up
```

Later connections print nothing extra.

## Inside and outside

Cluster nodes usually answer only from the site's own network.  From
anywhere else ssh has to go through the login host (`ProxyJump`); from
inside, going through it is a needless detour (and a second password hop).
`ssh --reachable HOST PORT` answers "can I reach this host directly from
here?", and ssh's `Match exec` uses the answer.  Add at the **end** of
`~/.ssh/config`:

```
Match host *.cv.nrao.edu,!ssh.cv.nrao.edu !exec "~/bin/ssh --reachable %h %p"
    ProxyJump ssh.cv.nrao.edu
```

Read it as: for a node (not the login host itself) that is *not* reachable
directly, jump through the login host.  `ssh cvpost001.cv.nrao.edu` then
works the same in the office, at home and on the VPN.

How it decides, and why it is cheap: ssh runs that test before every
connection, so the wrapper remembers each answer -- per host and per *local
source address*, the address your computer would send from to reach the
host.  The system reports that address without sending anything, and it
changes whenever your network does (office, home, VPN up or down, a hotel).
So a move is never missed, a network seen before costs nothing, and a new
one costs a single connection attempt of at most a second.  A host name
that does not resolve counts as "not reachable" straight away.  Answers are
kept a day, in `~/.cache/ssh-wrapper/reachable`.

* `ssh --reachable -v cvpost001.cv.nrao.edu` says what it decided and why.
* `ssh --reachable --forget` forgets everything learned (say, after the
  site's firewall changed).
* `SSH_WRAPPER_ROUTE=direct` or `=jump` in the environment decides without
  testing.

Two cautions: nothing earlier in `~/.ssh/config` may set `ProxyJump` for the
same hosts (ssh keeps the first value it finds -- hence "at the end"), and
keep the login host out of the `Match` (it must never jump through itself).
Host aliases are fine as long as their `Host` block, with its `HostName`,
comes before the `Match`.

## The rules file

`~/.config/ssh-wrapper/rules` (or the file named by `SSH_WRAPPER_RULES`):
one rule per line, `#` starts a comment.

```
<host pattern>[,<pattern>...]   [key=value ...]
```

Patterns are ssh-style globs (`*`, `?`), matched case-insensitively against
each hop's **real host name** -- what `ssh -G HOST` reports as `hostname` --
not against your `~/.ssh/config` aliases.  List only hosts that ask for a
password; key-only hosts (a git server) need no rule.  Keys, all optional:

| key | meaning | default |
|---|---|---|
| `service=` | the keychain service name | `ssh-wrapper` |
| `account=` | the keychain account name | the hop's `User` from `~/.ssh/config`, else your login name |
| `backend=` | `security` (macOS), `secret-tool`, `pass` or `command` | Keychain on macOS; `secret-tool` if installed, else `pass` |
| `pass_name=` | the entry name for `pass` | the service |
| `command=` | for `backend=command`: a shell command that prints the password; the rest of the line | |

`account=` matters when the user name you stored the password under is not
the one ssh logs in as.

## Day to day

* **Passwords are tried once.**  A wrong or outdated stored password is
  refused by the host once, and the wrapper stops -- it cannot lock your
  account by retrying.  Store the new one (`security ... -U` on macOS) after
  a password change.
* **No terminal, no prompt.**  If the wrapper's own login fails and nothing
  can type a password (a kernel, a script), it stops with a message saying
  why: the name or network, no stored password, or a refused one.  In a
  terminal it hands over to ssh, which asks you.
* **Stale connections are replaced.**  After sleep, a network drop or a
  macOS user switch, a shared connection can look alive locally while the
  far end is gone.  One not confirmed for five minutes is tested first (a
  no-op over the shared connection, killed after ten seconds), and replaced
  by a fresh login if it does not answer:
  `[ssh-wrapper] the shared connection to '...' is stale ...; logging in again`.
* **Ending a shared connection** by hand: `ssh -O exit HOST`.
* **Updating:** run the `curl` line of step 1 again.  **Removing:** delete
  `~/bin/ssh` and `~/bin/scp`; nothing else depends on them.

## Settings

Environment variables, all optional:

| variable | effect | default |
|---|---|---|
| `SSH_WRAPPER_RULES` | the rules file (only this one) | `~/.config/ssh-wrapper/rules` |
| `SSH_WRAPPER_BACKEND` | the password store, for rules without `backend=` | see above |
| `SSH_WRAPPER_PERSIST` | how long a shared login stays open, if `~/.ssh/config` sets no `ControlPersist` | `8h` |
| `SSH_WRAPPER_STALE_AFTER` | seconds a shared connection is trusted before it is tested; `0` tests every time | `300` |
| `SSH_WRAPPER_PROBE_TIMEOUT` | seconds that test may take | `10` |
| `SSH_WRAPPER_ROUTE` | `direct` or `jump`: `--reachable` answers without testing | |
| `SSH_WRAPPER_REACHABLE_TIMEOUT` | seconds `--reachable` waits for a host to answer | `1` |
| `SSH_WRAPPER_REACHABLE_TTL` | seconds a learned answer is kept | `86400` |
| `SSH_WRAPPER_DEBUG` | `1` logs every step | |
| `SSH_WRAPPER_LOG` | where | `~/.ssh/ssh-wrapper.log` |

## When something is wrong

* Start with `ssh --explain HOST`.  "rule: none" with a note usually means
  the rule names an alias or a short name instead of the real host name.
* "NOT FOUND" for the password: the `service`/`account` pair differs from
  what you stored -- compare with `security find-generic-password -s SERVICE`
  (macOS) or add `account=` to the rule.
* "no ControlPath": step 4 is missing for that host; without it the login
  cannot be shared and you would be asked every time.
* Going the wrong way (straight to a node from outside, or through the
  login host from inside): `ssh --reachable -v NODE`, then
  `ssh --reachable --forget` if the site changed.
* Everything else: `SSH_WRAPPER_DEBUG=1 ssh HOST`, then read
  `~/.ssh/ssh-wrapper.log`.

The full details are in the comments at the top of the script.
