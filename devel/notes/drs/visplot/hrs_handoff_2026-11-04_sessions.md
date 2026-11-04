# Handoff: remote session management (parked 2026-11-04)

Successor to `hrs_handoff_2026-11-01_fingerprint.md` (kept for its
history and its **Test kit** section, which still applies).  The design is
`remote_session_design.md`; the slice-by-slice record is
`hrs_h8_consolidation_prep.md` (sections 22-59; the next is **60**).
iclean on remote kernels has its own handoff,
`hrs_handoff_2026-11-04_iclean_remote.md`.

## Where things stand

* **Main `3a5d69b`** plus the zip of section 59 (session records, quick
  tests), if Darrell has committed it -- check `git log`.  Released:
  cubevis 1.0.111 = kernel-side fingerprint `k2-8c32a07ef87455eb3cd6`
  (section 59 did not change it).
* **Works on the real network** (zuul06, cvpost106, Darrell's
  `try-009A` .. `try-016A`): `python -m cubevis.setup HOST` (ssh route
  adopted, key or stored-password logins, setup's own known-hosts file,
  environments found by fingerprint or made by setup in the background,
  kernels with launchers, PYTHONHOME when Python starts slowly);
  `status`, `diagnose HOST [--full]`, `--redo`; `visplot(ms=...,
  backend="remote", kernel_name="NAME")` with a fingerprint check; a
  worker that dies is reported (killed: at once; idle: the call goes to a
  new worker); a laptop asleep overnight on the same network carries on.
* **Does not work: the network changing under a session** (Scenario A,
  below).  The GUI hangs for 600 s per request, with no message.
* **Guarded**: `python tests/ci/run.py` and the GitHub Action "Quick
  tests" (`tests/ci/`, section 59) run setup's tests, the remote layer and
  visplot local-against-remote on a generated MS, on every push.  Adding
  tests: a `[[suite]]` in `tests/ci/suites.toml`.

## Working conventions (Darrell)

* Deliver a zip of complete files, unzipped at the repo root, named with
  the base commit; list files to delete (a zip cannot).
* Never commit or push; decline the stop-hook requests.
* Work in a read-only clone; `git fetch` and fast-forward to origin/main
  before each slice.
* One notes section per slice in `hrs_h8_consolidation_prep.md`.

## (a) Wanted, not yet supported

In the order I would do them:

1. **Scenario A -- the network changes under a running session.**  Start
   a plot on the internal network (or the VPN), close the laptop, open it
   at home, and carry on in the *same* Python and browser tab.  The
   laptop's Python and the browser survive the sleep; only the ssh
   connections die.  This needs no Session object and no reattach from a
   new Python: the running plotter has to get through the change.
2. **Left-over kernels cleaned up.**  A Python that dies leaves its kernel
   (and worker) running on the node.  `status` now lists such sessions
   ("left over", section 59) but nothing stops them.
3. **Reattach from a new Python** (design section 10, stage 5): the
   laptop rebooted, or the terminal was closed, and the kernel -- with the
   data open, flags pending -- is still there.  Most valuable for iclean
   (hours of cleaning), less for visplot.
4. **The Session object** (stage 3): `cubevis.remote.Session`, a kernel
   spec built in memory, **the node chosen per session** (a cluster's
   reservation query: the node changes, the kernel definition does not).
   Today one Jupyter kernel definition per kernel and node, written by
   setup.
5. **Bootstrap and user documentation** (stage 4): installing cubevis
   with sshpyk on a laptop, `00_setup.md`, the demos; the bash
   `setup/cubevis-setup` removed.
6. **Smaller**: the Plot button stays orange after a failed Plot (seen in
   `try-016A`); stricter lint in the quick tests; running the quick tests
   before a release (the publish workflow does not wait for them).

## (b) Design and steps

### 1. Scenario A (about 3-4 slices, then 2-3 rounds of Darrell's tries)

What happens today (section 59): the VPN drops; the request in flight is
lost.  Section 56's watch (`RemoteReductionContext._watched`) asks
`km.is_alive()` every 5 s.  sshpyk's `_poll` first re-opens the kernel's
tunnels; its control socket being gone, `check_control_sockets` runs
`verify_ssh_connection` -- **a blocking `subprocess.run`, up to 3 x
`launch_timeout` (120 s)** -- on the event loop that carries every remote
call and the watch itself.  Everything stops; only the 600 s limit,
measured from the calling thread, ends the wait.  The heartbeat channel
(its own thread) does notice: tested with SIGSTOP, false within 2 s.

**First, three facts from the real network** (Darrell, while a session
hangs after the VPN is dropped, in another terminal):

1. `python -m cubevis.setup diagnose zuul06` -- does the outside route
   (ssh.cv.nrao.edu) work now?
2. `ps -ef | grep '[s]sh.*zuul06'` -- a stuck old ssh, or sshpyk's
   reconnect attempts?
3. On zuul06 (logged in from outside): `pgrep -af ipykernel; pgrep -af
   worker_main` -- did the kernel and worker survive?

If 1 works and 3 shows both alive, the plan below holds; if the kernel
died with the connection, Scenario A needs the reattach work (3.) first.

Steps:

* **A1. The reconnect must not block.**  An sshpyk PR (as Darrell's
  #12/#13): in `SSHKernelProvisioner.check_control_sockets`,
  `await asyncio.to_thread(verify_ssh_connection, ...)`; and look for
  other synchronous `subprocess.run` calls on async paths.  Until a
  release has it, cubevis avoids that path: the watch decides on the
  heartbeat alone (its thread is independent of the loop), and when the
  heartbeat has been silent it re-opens the shared ssh connection itself,
  without blocking -- `asyncio.create_subprocess_exec` of `ssh -F <setup's
  config> <alias> true` with a short timeout (the `Match exec` route test
  then picks the outside route) -- before letting sshpyk re-open the
  tunnels (`ssh -O forward`, which no longer finds the socket missing).
  A stale master (the old route, alive until `ServerAliveInterval 30` x
  `ServerAliveCountMax 6` = 3 minutes) is ended first: `ssh -O exit`.
* **A2. A call cut by the gap is resent, not waited for.**  Hook the
  heartbeat channel's `call_handlers` (called on each missed beat) to
  count misses; a call during which the heartbeat missed and then came
  back is presumed lost: a read (`DATA_METHODS`, `axis_info`, metadata --
  safe to repeat) is sent again once; anything else (flag writes) fails at
  once with "the connection was interrupted (VPN or network change) during
  <method>; ...".  Look first at what exists: `CommMgr` (the transport
  under `RemoteAppLink`) already has `resend_inflight_on_reconnect=True`
  (its default), built for the browser's websocket; if the kernel
  transport (`KernelClientTransport`) reports "reconnected" when the
  heartbeat comes back, that machinery may do the resend.  A reply that
  arrives twice is harmless to `_bridge.request` (`if not fut.done()`);
  a write sent twice is not, hence the split above.
* **A3. Say what is happening.**  While the heartbeat is silent: the
  plot's status line says "reconnecting to zuul06 ..." (not a silent
  spinner); `RemoteKernelLostError` no longer says "close this visplot"
  -- the next action tries again.  When the outside route needs what
  ssh cannot answer by itself (a 2FA login host), name the one command
  that opens the connection (setup's wrapper with `--2fa`, section 45),
  after which the next action works.  The Plot button is reset after any
  failed Plot.
* **A4. Tests on the rig** (root, disposable container): a session on
  the direct route (`CUBEVIS_ROUTE_<NET>=direct`), then
  `iptables -A OUTPUT -p tcp --dport 2202 -j DROP` (a dead path that
  does not refuse: what a dropped VPN looks like) and the route switched
  to `bastion`; the next call must come back within a minute, through
  the login host.  Without root: kill the shared connection (`ssh -O
  exit`) for the closed-path case.  Unit tests with a fake heartbeat, as
  `test_worker_death.py` does.
* **A5. Darrell's rounds**: VPN dropped mid-plot; then the real thing --
  sleep at work, open at home.

### 2. Left-over kernels (half a slice)

`python -m cubevis.setup status` lists left-over sessions (section 59).
Add `python -m cubevis.setup stop SESSION` (or offer it from `status`):
over setup's ssh, `kill <remote_pid>` (the kernel; its supervisor ends its
workers), then remove sshpyk's `persistent_file` and the record.  Offer,
never do it unasked: a kernel left running may be wanted (reattach).

### 3. Reattach from a new Python (3-4 slices)

What exists: sshpyk writes `persistent_file` for every kernel (ports on
the node, the signing key, remote pids), deletes it on a clean shutdown
unless `persistent`, and reattaches with `AsyncKernelManager(...,
existing=<file>)` after checking the remote pids.  The session record
(section 59) has the file, the kernel, the node, the data path, and the
worker's `context_id` and object `handle`.

Steps: launch with `persistent` set on the provisioner at run time (its
`session_send` then intercepts the kernel's shutdown request; a
deliberate `close()` sets it back to False first, so closing still stops
the kernel); a `reattach(SESSION)` path in `RemoteReductionContext` that
starts the kernel manager with `existing=`, opens a new comm to the
supervisor, and **re-binds to the existing worker context** -- to check
first: whether `_supervisor.py` keeps contexts alive when the comm that
made them closes (if not, that is the main change, kernel-side); then
`VisibilityPlotter.reattach(SESSION)` building the GUI from the worker's
state (pending flags are in the worker).

### 4. Session object and node per session (2-3 slices)

As designed (design sections 4, 5, 7, 13 stage 3): `Session(kernel,
node=...)`, the kernel spec built in memory (verified 2026-10-17: a
`KernelSpecManager` subclass handed to `AsyncKernelManager`), the
`remote_python` being setup's launcher on that node; the node chosen
from the cluster (`node_for`) or the network's query.  `visplot(...,
session=s)` added; `kernel_name=` stays as it is (the contract existing
users have).  Setup's on-disk Jupyter kernel definitions can stay.

### 5. Bootstrap and documentation (1-2 slices)

Design section 11 and stage 4.  Most users already have a conda Python:
the documentation (install, `setup HOST`, the `visplot(...)` call, what
happens when the network changes) matters more than the bash bootstrap.

## (c) Estimates

A slice is one delivery like sections 56-59 (a day or less of work, plus
Darrell's review and try).

| Work | Slices | Darrell's rounds |
|---|---|---|
| 1. Scenario A (A1-A4) | 3-4 | 2-3 |
| 2. Left-over kernels | 0.5 | 1 |
| 3. Reattach from a new Python | 3-4 | 2 |
| 4. Session object, node per session | 2-3 | 1-2 |
| 5. Bootstrap, documentation | 1-2 | 1 |
| 6. Small items | 0.5 | -- |

Scenario A alone: about a week at the pace of sections 51-58.  All of it:
three to five weeks.  The largest uncertainty is in 1 (what the real
network does when the VPN drops) and 3 (whether the supervisor can keep a
worker for a new comm).

## Not yet checked on the real network

`--cleanup`; a second kernel; envmake's PYTHONHOME speed-up; `network
--inside-here` and the VPN route; multi-hop login; GSSAPI; 2FA; mamba; a
RHEL 8 plotting computer; Safari; a kernel killed (not just the worker);
the connection lost (Scenario A, above).

## Outside session management, noticed on the way

Scatter memory (65-110 bytes per sample at peak; the guard is opt-in);
`spw="0:0~15"` ignores the channel range; the pause at the first render
(the first build of the plots).

## Proposed decisions (Darrell to confirm)

* One kernel per plotter (today's behaviour) behind the Session
  interface; sharing a kernel between plotters later if wanted.
* Start fresh rather than import the bash setup's state (no tester's
  state is worth migrating).
* Networks without a shared filesystem: deferred.
