# cubevis Remote Execution — Implementation Record

**Purpose:** technical status, code-level detail, and open questions,
chunk by chunk, updated as each chunk is implemented. The companion
`cubevis-remote-execution-design.md` describes the stable architecture
this all builds toward; this document tracks the actual, current state of
building it. When the two disagree, this one is more current — the
architecture doc changes only when a genuinely new architectural decision
gets made (Chunk 1b was one; Chunk 1c is another).

**Chunk status at a glance:**

| Chunk | Status |
|---|---|
| 1 — shared wire-protocol layer | **Implemented, tested, isolated into `cubevis.remote` subpackage** |
| 1b — compute worker process infrastructure | **Implemented, tested against real subprocesses/kernels** |
| 1c — remote execution and object framework | **Implemented, tested against real subprocesses/kernels** |
| 2a — `visplot` raster | Designed, not yet implemented |
| 2b — `visplot` scatter | Designed, not yet implemented |
| 3 — `iclean`/`gclean` | Designed, not yet implemented |

---

## Chunk 1 — Shared wire-protocol layer

**Status: implemented and tested.** Isolated into `cubevis.remote` (see
"Subpackage isolation," below) before real-environment testing began.
Verified against real infrastructure where practical (a real, separate
local `ipykernel` subprocess for every kernel-facing test; real `sshpyk`
kernels for the hand-run demo, once dependency/logging issues in that
environment were resolved — see "Real-environment issues found," below).

### What it delivers

- `cubevis/bokeh/transport/_comm_mgr.py` — the **only** existing `cubevis`
  file modified by Chunk 1 itself (Chunk 1b's Task 6 later added one more
  small, explicitly-scoped change to `_bokeh_app_context.py` — see that
  section). Added:
  - `role: str = CommMgr.ROLE_DEFAULT` constructor parameter, resolving
    to `self._self_direction`/`self._peer_direction` via a small lookup
    table (`ROLE_DEFAULT: ('p2j', 'j2p')`, `ROLE_MIRROR: ('j2p', 'p2j')`).
    All four previously-hardcoded direction-tag sites (`send()`,
    `_send_immediate()`, `_route_message()`, `_handle_request()`'s three
    reply sites) now read from these instead of literal strings.
    `role=ROLE_DEFAULT` (the default) reproduces today's literals
    exactly — the browser-facing path is unaffected.
  - `ROLE_DEFAULT`/`ROLE_MIRROR` named constants, both module-level and
    as `CommMgr` class attributes.
  - `CommMgr.initialize(transport=None)` — optional parameter; if given,
    assigned to `self._transport` before the existing logic runs. Lets
    external callers avoid touching `self._transport` directly.
  - `transport_type == 'remote_kernel'`, recognized by `initialize()`
    identically to `'colab'`/`'jupyter'` — a separate label so it doesn't
    collide with `'auto'` autodetection used elsewhere for the browser
    leg. Chunk 1b's worker-process hop reuses this same label rather than
    introducing a fourth (`'worker_process'`) — see Chunk 1b, "Known
    deviations from the original sketch."
- `cubevis/remote/` — new, isolated subpackage:
  - `_bridge.py` — `request(comm, message_id, payload, timeout=None)`
    (`Future`-wrapped `Comm.send()`, optional timeout not in the original
    sketch — added because a permanently-vanished peer with
    `resend_inflight_on_reconnect=True`, CommMgr's default, would
    otherwise leave the awaited `Future` pending forever) and
    `SyncBridge` (dedicated background thread + persistent event loop;
    `start()`/`run()`/`run_background()`/`stop()`). Zero `cubevis`
    dependencies — pure `asyncio`/`threading`.
  - `_kernel_transport.py` — `KernelClientTransport` (P_local side,
    frontend role, owns the `jupyter_client` `AsyncKernelManager`/
    `AsyncKernelClient` lifecycle, `ready_timeout` constructor parameter)
    and `KernelCommTransport` (kernel side, headless — see "Why not
    `CommsTransport`," below).
  - `_link.py` — `open_remote_kernel_link(kernel_manager, target_name=...,
    comm_mgr_id=None, ready_timeout=60.0)`, one-call convenience wiring a
    mirrored `CommMgr` + `KernelClientTransport` pair via
    `CommMgr.initialize(transport=...)`, avoiding private-attribute
    access from calling code. Deliberately does not start
    `transport.run()` itself — see its docstring for why that's the
    caller's choice (a bare `asyncio.ensure_future(...)` in an
    already-async context vs. a `SyncBridge`-driven background task when
    the caller has one; conflating the two is a real hazard — see Chunk
    1b's "cross-loop `SyncBridge` hazard" finding, below).
  - `_worker.py` — `ensure_remote_worker(build_worker, *, target_name=...,
    namespace=None)`: idempotent kernel-side bootstrap. `build_worker(mgr)`
    receives the freshly-constructed `CommMgr` so it can `mgr.open(category)`/
    `.register(message_id, handler)` its own protocol handlers as part of
    construction — this is what lets a real backend (Chunk 1c/2/3) register
    its actual message handlers, and returns whatever object should be kept
    alive as "the worker" (opaque to this bootstrap mechanism — Chunk 1c's
    pool object is exactly such a return value, one layer richer than
    Chunk 1b's). A namespace marker (`__cubevis_remote_worker__`, stored in
    the active IPython kernel shell's `user_ns` by default) makes repeated
    bootstrap calls — including from a reattaching `P_local` session —
    idempotent: `build_worker` only runs once per kernel process, and every
    later call returns the **existing** `comm_mgr_id` unconditionally,
    regardless of what `target_name` argument that later call happened to
    pass.

    **Correction to an earlier version of this document, and a revision
    this implied — already applied and verified, not left as a pending
    task:** `target_name` was originally given a fixed, well-known
    default (`DEFAULT_TARGET_NAME = "cubevis-remote-worker"`), on the
    theory that a fixed name was what let a freshly-restarted `P_local`
    process reconnect without prior knowledge. On inspection that
    reasoning doesn't hold — `target_name` is the Jupyter Comm protocol's
    own routing field ("which registered service inside an
    already-connected kernel"), not a session identifier, and rediscovery
    of the *right* one doesn't need it to be globally fixed: since a later
    bootstrap call returns the existing `comm_mgr_id` regardless of what
    `target_name` it was given, a reattaching `P_local` process can simply
    re-run the same idempotent bootstrap cell and read the identifier back
    from its own output, exactly as it already does for `comm_mgr_id`
    itself (see the architecture doc §2e for the full argument, and why a
    generated value also removes a real collision risk a global constant
    carried). `target_name` now defaults to `None` and is generated from
    the newly-constructed `mgr.comm_mgr_id` when not explicitly supplied.
    Applied directly to the real file and re-verified against the full
    test suite (23/23 still passing, no other code depended on the old
    default — `RemoteAppLink`/`_supervisor.py` already always pass
    `target_name` explicitly) — genuinely isolated, not deferred to Chunk
    1c despite being closely related to that chunk's pool restructuring.
    `DEFAULT_TARGET_NAME` remains defined and importable for any caller
    who wants a fixed, predictable name for a specific reason (e.g. manual
    debugging); it's simply no longer the function's own default.
  - `testing.py` — `LoopbackTransport`/`wire_loopback_pair(side_a, side_b,
    name_a=..., name_b=...)`, an in-process `TransportBase` double for
    testing mirrored-`CommMgr` wiring without any real kernel/socket —
    public, not just a test helper, since it's independently useful to
    anyone testing against this subpackage. `name_a`/`name_b` are purely
    cosmetic log labels.
  - `examples/try_local_or_remote_kernel.py` — hand-run, step-by-step
    script: start a kernel (local or a real `sshpyk`-provisioned remote
    one, chosen purely via `--kernel-name`), bootstrap a toy worker, run
    two demo commands, shut down. Verified working end-to-end against
    both.
  - `tests/` — real, passing suite including `test_bug_reproduction.py`
    (pins down the pre-fix misrouting failure mode described below,
    using two same-role `CommMgr()`s deliberately) and
    `test_kernel_transport_spike.py` (a lower-level protocol validation,
    predating the polished `KernelClientTransport`/`KernelCommTransport`,
    kept as its own slower/heavier file so it's easy to deselect).

### Why the direction-tag bug existed, and the fix

`Comm.send()`/`CommMgr.send()`/`_send_immediate()` hardcoded the outgoing
`'direction': 'p2j'` tag; `_route_message()` dispatched purely on that
literal string (`'p2j'` in → treated as a response to *our* pending
request; `'j2p'` in → treated as a fresh request needing a handler); and
`_handle_request()`'s auto-reply hardcoded `'j2p'` on the way back out.
Fine for the browser leg (genuinely asymmetric: Python always originates
`p2j`, JS always originates `j2p`), but breaks if two unmodified `CommMgr`
instances talk to each other — both Python, both tagging their own
outgoing traffic `p2j`, so an unsolicited push from the kernel side (e.g.
a progress update) arrives at `P_local` tagged `p2j`, is treated as "a
response to something we asked for," finds no matching `request_id`, and
is silently dropped — while the sender's own `_pending` slot for that comm
is left permanently occupied, wedging all further traffic on it.
Reproduced concretely (`test_bug_reproduction.py`, a loopback double, no
real kernel) before being fixed.

*Considered and rejected:* a hand-written mirror class instead of
touching `_comm_mgr.py`, to avoid changing working code. Rejected because
it would need to independently reimplement `squash_queue`, in-flight
resend on reconnect, and reconnect-generation bookkeeping to be equally
safe for the away-for-hours-then-back scenario this whole design exists
to support — a second, independently maintained copy of that logic is a
likely source of drift. The one-parameter, backward-compatible `role`
change was preferred.

### Why not `CommsTransport` for the kernel side

`CommsTransport` (the only existing `'jupyter'` transport) is hard-coupled
to an anywidget/browser bridge: `__init__` unconditionally calls
`BokehInit.get_app_context().add_preflight_callable(self.display_bridge)`,
and `display_bridge()` builds an `anywidget.AnyWidget` and calls
`display()`; `connect()` then blocks on `self._conn_event`, set only by a
**JS** comm handshake (JupyterLab's widget manager, or Colab's
eval_js/BroadcastChannel path). None of that has a counterpart when the
peer is `KernelClientTransport` speaking plain `jupyter_client` — no
browser, no widget manager, nothing to render the bridge for.

`KernelCommTransport` keeps the one piece of `CommsTransport` that *is*
transport-agnostic — registering a target directly on the kernel's own
comm manager (`comm.get_comm_manager().register_target(target_name,
self._on_comm_open)`, `_on_comm_open(comm, open_msg)` matching the
standard `comm` package's `CommTargetCallback` signature) — and drops
everything that assumes a browser. **The direction matters and is worth
stating explicitly, since it's easy to get backwards:** the kernel side
is *passive* — it registers and waits. It's `KernelClientTransport` (the
`P_local` side) that *actively* sends the `comm_open` (a `session.msg()`
built by hand and sent on the shell channel, with a freshly-generated
`comm_id`), which is what triggers the kernel's registered callback. This
also makes reconnection simpler than an alternative "wait for a live
`comm_open` broadcast and hope you didn't miss it" design would be:
`register_target()` is a *persistent* registration, so a second `P_local`
session reattaching just sends its own fresh `comm_open` at the same
`target_name`, and the kernel's still-registered handler fires again,
handing `KernelCommTransport` a new server-side `Comm` for that session —
no discovery/timing race on either side.

### Real-API verification (not assumed)

Checked against `jupyter_client` 8.9.1 and `sshpyk`'s actual source, not
asserted at a conceptual level:

- `Session.msg()` to build a message, `shell_channel.send(msg)` to send
  it — the low-level pair actually used for hand-built `comm_open`/
  `comm_msg` traffic on the `P_local` side; `get_iopub_msg(timeout=...)`
  for incoming iopub traffic (raises on timeout rather than blocking
  forever — matches the existing transports' "keep the loop alive, check
  a flag" shape).
- Kernel→frontend comm traffic goes out over **iopub**; frontend→kernel
  `comm_open`/`comm_msg` traffic goes over **shell** — content shapes
  `{comm_id, target_name, data}` / `{comm_id, data}` (via `comm`/
  `ipykernel.comm.manager` source).
- `register_target()` is transport-agnostic — confirmed by reading
  `ipykernel.comm.manager.CommManager`/the standalone `comm` package's
  `CommManager`/`BaseComm` directly (`comm.base_comm`).
- `sshpyk` isn't a client library called directly — it registers as a
  `jupyter_client.kernel_provisioners` entry point (`sshpyk-provisioner`,
  confirmed via its `entry_points.txt`). The integration point is the
  *standard* `AsyncKernelManager`/`AsyncKernelClient` API against an
  sshpyk-provisioned kernelspec — exactly what `KernelClientTransport`
  already uses, with **zero** `sshpyk`-specific code anywhere in the
  implementation (confirmed by grep: the string `sshpyk` appears only in
  comments/docstrings). This is what makes `--kernel-name python3` vs.
  `--kernel-name zuul06_python312` a one-flag swap in the demo script —
  the kernel name is the only thing that determines which provisioner
  (`LocalProvisioner` vs. `sshpyk-provisioner`) actually launches it, and
  the code never asks which.
- `sshpyk`'s persistence mechanism, confirmed by reading
  `provisioning.py` directly (not just its own comments): every kernel
  launch calls `write_persistent_info()` unconditionally, inside
  `launch_kernel()`, writing `get_persistent_info()`'s dict (`kernel_id`,
  `rem_sys_name`, `rem_conn_info`, `rem_pid_k`, `rem_pid_ka`,
  `rem_conn_fp`, `rem_proc_cmds`) as JSON to `persistent_file` (default
  location: `jupyter_runtime_dir()`, named `sshpyk-kernel-<uuid>.json`).
  The `persistent` flag controls only whether `_cleanup()` **deletes**
  that file on shutdown (`if not self.persistent and self.persistent_file
  and not restart: fp.unlink()`) — it does not control whether the file
  gets written in the first place. Reattaching
  (`AsyncKernelManager(kernel_name=..., existing=<name-or-path>)`)
  resolves the file via `find_persistent_file()` (a thin wrapper around
  `jupyter_client.connect.find_connection_file`), loads it, and
  `load_persistent_info()` restores every field via plain `setattr` —
  including the remote PIDs later re-verified against the live remote
  process before the reattach is trusted. **There is no API to enumerate
  *all* currently-dangling persistent files** — `find_persistent_file`
  resolves one name/pattern you already know, it doesn't list what
  exists. This is exactly the gap Chunk 1c's kernel-persistence manifest
  exists to fill — see that section.

### Subpackage isolation

Refactored (before real-environment testing began, at the requester's
explicit request) from living inside `cubevis/bokeh/transport/` into its
own `cubevis/remote/` subpackage — two motivations: avoid interfering
with concurrent, unrelated `cubevis` work in the same directory, and keep
the code in reasonable shape to be extracted as a standalone package
later, if useful outside `cubevis`.

`cubevis/bokeh/transport/_comm_mgr.py` remains the only Chunk-1-touched
existing `cubevis` file — the `role` mechanism genuinely has to live where
message routing happens; there's no clean way to inject it from outside
without fragile subclassing of private methods. Three narrow, explicit
coupling seams remain between `cubevis.remote` and `cubevis.bokeh.transport`:

1. `CommMgr`/`Comm`/`TransportBase`/`AppState` (public surface) —
   permanent and fundamental; this is the thing being bridged.
2. `cubevis.utils.serialize`/`deserialize` — public module, kept for wire
   consistency with `WebSocketTransport`/`CommsTransport`'s existing
   encoding. Confirmed (Chunk 1c) to wrap Bokeh's own serialization
   machinery (`bokeh.core.serialization.Serializer(deferred=False)`/
   `Deserializer`, via `cubevis/utils/_conversion.py`) rather than plain
   JSON — see Chunk 1c's "Serialization" section for what that means for
   large payloads.
3. `cubevis.bokeh.transport._environment.get_ipython_kernel_shell` — a
   *private* module, despite being generic "am I in a Jupyter kernel"
   detection with nothing Bokeh-specific about it. The one real seam:
   worth promoting to a public location or vendoring, if/when standalone
   extraction actually happens. Deliberately not pre-emptively duplicated
   now, before there's a second consumer.

### Real-environment issues found (all resolved)

Testing against a real environment (rather than just the sandbox this was
built in) surfaced three genuine issues, none in the core wire protocol
itself:

1. **`ensure_remote_worker`'s `build_worker` signature.** Originally
   `Callable[[], Any]` — no way to register comm handlers, since no
   `Comm` exists until something calls `mgr.open(...)`, and `mgr` wasn't
   passed in. Found while writing the hand-run demo (which needed to
   register two toy commands). Fixed to `Callable[[CommMgr], Any]`;
   `build_worker(mgr)` now does the registration itself. Existing tests
   updated; this needed to be caught before Chunk 1c/2/3 wrote code
   against the old signature.
2. **Swallowed `sshpyk` diagnostics in plain scripts.** `sshpyk`'s
   provisioner (like `AsyncKernelManager` itself) is a traitlets
   `LoggingConfigurable`; its `.log` falls back to
   `traitlets.log.get_logger()`, which returns a logger with only a
   `NullHandler` attached when no real `Application` (`jupyter console`,
   `jupyter lab`, ...) is running — confirmed by direct test. A bare
   script gets **zero** log output from either library, not even
   warnings/errors — which is why a real failure ("kernel died before
   replying to `kernel_info`") produced no visible explanation. `sshpyk`
   explicitly relays the remote process's own stdout/stderr at debug
   level, which was being silently dropped. Fixed by having the demo
   script call `logging.basicConfig(level=DEBUG)` (default; `--log-level`
   to turn down). **Applies to any bare script using `jupyter_client`
   directly, not just this demo** — Chunk 1b's own `WorkerProcessTransport`
   stderr relay (below) is the same lesson applied one hop deeper.
3. **`pytest-asyncio` not installed in the target environment** (only
   `anyio`'s pytest plugin was present, which provides a *different*
   marker, `@pytest.mark.anyio`, not `@pytest.mark.asyncio`). Not a code
   issue; resolved via installing `pytest-asyncio` (pytest 9.1.1 +
   pytest-asyncio 1.4.0 confirmed compatible, used throughout this
   project's testing).

### Known limitations, honestly stated

- "Existing browser-facing `CommMgr` tests pass unmodified" is verified
  by equivalence argument (role defaults reproduce the old literals
  exactly; only the four documented sites changed), not by literally
  running that suite, which was never part of this project's available
  source set.
- The automated test suite's real-kernel tests exercise a local `python3`
  kernel (fast, no SSH/cluster dependency for CI). The `sshpyk` path is
  proven working by the hand-run demo against a real cluster, not by an
  automated test against one.

---

## Chunk 1b — Compute worker process infrastructure

**Status: implemented and tested.** Real-subprocess round trips (request/
response and push), stderr relay, the died/working/stuck/completed status
vocabulary with a non-blocking background-dispatch primitive, and
`RemoteAppLink`/`BokehAppContext` integration are all built and verified
against real subprocesses and real kernels — no mocks, matching Chunk 1's
own bar. Its current API shape (one `RemoteAppLink` per caller-chosen
`target_name`) needed restructuring around Chunk 1c's execution-context
pool — see "Known limitation," below — which Chunk 1c has now done;
everything at the transport and dispatch level it proved is unaffected by
that restructuring and is directly reused by Chunk 1c.

**Motivation:** `gclean`/`tclean`'s C++-backed major/minor cycles are
long-running and cannot be assumed to release the GIL. If that work ran
inline in the supervisor kernel's own process, it would block the same
event loop that's supposed to keep answering `P_local` — freezing exactly
the connection meant to report on the task's status. See the architecture
doc §2c for the full reasoning on why only a separate OS process actually
solves this (not a thread, not `asyncio` structure alone).

### Architecture, as built

```
Supervisor kernel  <--asyncio.subprocess, length-prefixed frames-->  Compute worker process
```

Reuses `CommMgr`, mirrored roles, and `request()` from Chunk 1 completely
unchanged — only the transport is new. The supervisor plays
`ROLE_MIRROR` toward the worker (which plays `ROLE_DEFAULT`, identical in
spirit to how a kernel-side `CommMgr` is constructed today), so this is
architecturally the *same* pattern as the P_local↔supervisor hop, one
level deeper.

**`WorkerProcessTransport`** (supervisor side, in `cubevis/remote/
_worker_transport.py`): spawns via `asyncio.create_subprocess_exec(
sys.executable, "-m", "cubevis.remote.worker_main", "--comm-mgr-id",
comm_mgr_id, ...)`, `stdin`/`stdout`/`stderr` all piped. Length-prefixed
framing — 4-byte big-endian length + UTF-8-encoded `cubevis.utils.serialize()`
output — matching Chunk 1's own framing convention. `close()` closes
`stdin`, waits for the process to exit with a timeout, escalating to
`terminate()` then `kill()` if it doesn't, and only returns once
`self._proc.returncode` is genuinely set — not merely once local Python
references are dropped. `WorkerCommTransport` (inside the worker, in the
same file) is the mirror image, using `loop.connect_read_pipe`/
`connect_write_pipe` against `sys.stdin.buffer`/`sys.stdout.buffer`.

**Stderr relay**, on the supervisor side: a background task
(`_relay_stderr`) reads the worker's stderr line by line for as long as
the process lives, logs each line at `WARNING` (so it surfaces by
default, not just at `DEBUG`), and keeps the last ~200 lines in a ring
buffer for inclusion in error messages if the pipe closes unexpectedly.
Verified concretely: a worker module that fails to even import (a
guaranteed startup failure, no comm ever established) produces a
`ModuleNotFoundError` traceback visible in the supervisor's own log
output — the same "don't let a real failure look like silence" lesson
Chunk 1 learned about `sshpyk`, applied one hop deeper, and tested
explicitly rather than merely asserted.

**Spawned via `asyncio.create_subprocess_exec`, deliberately not
`multiprocessing`'s default.** `multiprocessing.Process()` on Linux
defaults to `fork()` **without** an immediate `exec()` — the child
continues as a copy of the parent's entire memory image, inheriting every
thread's state at the moment of the fork (only the forking thread
continues running). A well-documented hazard when the parent is
multi-threaded — an ipykernel process is (at minimum a heartbeat thread,
often more) — and libraries like ZMQ are known to be fork-unsafe in this
scenario: a lock held by some other thread at fork time can end up
permanently, silently held in the child. `create_subprocess_exec` does
`fork()`+`exec()` together, so the `exec()` immediately discards the
inherited memory image and starts a fresh interpreter — no inherited
thread state.

**Known deviation from the original sketch:** `initialize()`'s real,
patched `_comm_mgr.py` recognizes `transport_type in ('colab', 'jupyter',
'remote_kernel')` — no fourth `'worker_process'` value. Rather than touch
that shared file a second time for this hop, the supervisor↔worker leg
reuses `'remote_kernel'` too (functionally identical: "a pre-wired
non-websocket `TransportBase`"). Worth a dedicated value if the two hops
ever need to be told apart at the `CommMgr` level — not needed for
anything built so far.

### Dispatch: two shapes, not one, and why

The original sketch for this section assumed a single shape — the
supervisor's P_local-facing handlers, for anything that needs a worker,
as thin async proxies:

```python
async def proxy_to_worker(msg):
    return await request(worker_comm, message_id, msg)

comm.register(message_id, proxy_to_worker)
```

This works, unmodified, because `CommMgr._handle_request` already does
`if inspect.isawaitable(result): result = await result` — a proxy handler
is exactly as simple as a handler that computes the answer itself. **But
it is only safe for commands that return promptly.** Tracing through
`_comm_mgr.py`'s actual receive-loop shape (confirmed, not assumed):
every `TransportBase.run()` implementation `await`s its message callback
inline, in a single coroutine, before reading the next incoming message —
`WebSocketTransport.run()`'s `async for message in self.websocket: ...
await self._message_callback(msg)` is the clearest example, and the same
shape holds for `WorkerProcessTransport.run()`. `_handle_request()` in
turn awaits the registered handler's full result before sending a reply
and returning control to that loop. Chain those two facts together: a
P_local-facing handler that does `return await request(worker_comm, ...)`
directly, for a command that takes any real time, stalls **that CommMgr's
entire receive loop** for the command's full duration — a concurrent
status-check request from `P_local` would sit unread on the socket until
the slow command finally finished, which is exactly backwards from what
"the supervisor stays lively" is supposed to mean. Splitting the two
kinds of traffic across different `Comm` categories does not fix this —
there's still one transport/socket per `CommMgr` feeding one receive
loop, regardless of which `comm_id` a message targets.

The fix, built and tested (`_async_dispatch.py`):

- **Fast commands** (metadata, anything that returns in well under a
  second): the direct-await proxy above, unchanged. No liveness problem
  for these — the receive loop resumes essentially immediately.
- **Long/unbounded commands**: `JobRegistry.dispatch(comm, message_id,
  payload, job_id)` schedules the `request()` call as a background
  `asyncio.Task` and returns a `JobRecord` **immediately** — the
  triggering P_local request gets back a `job_id`, and the receive loop
  is free again in milliseconds regardless of how long the actual worker
  call takes. Status/result retrieval is then `JobRegistry.status(job_id)`
  — a synchronous dict lookup, never itself touching the worker, so it
  stays fast (verified: sub-5ms polls, repeatedly, while a real worker
  subprocess was genuinely still inside a multi-second blocking call).

**What "liveness" actually means here, precisely, since it's easy to
overstate:** once a command is dispatched to a specific worker, *that*
worker is legitimately busy until it replies — it's one process, one
GIL; a concurrent request routed to the *same* worker correctly waits.
The property `JobRegistry` buys is narrower and more useful: the
*supervisor's own* handling of an unrelated request — in particular, a
status check on the job that's running — never itself blocks on the
worker, no matter how long that job takes.

### Status vocabulary (died / working / stuck / completed)

`_async_dispatch.py`'s `JobStatus` enum, backed by `JobRegistry`.
Combines **two signals**, because a worker mid-C++-call may not be able
to send anything:

1. **The dispatched `request()`'s own resolution** — `COMPLETED` (with
   `result`) if it returns normally, `DIED` (with `error`) if it raises.
2. **`is_worker_alive()`** (typically `transport.is_connected`, or
   equivalently `transport.returncode is None`) as the fallback signal
   for a job whose worker vanished mid-flight without ever replying —
   verified end-to-end by killing a real worker subprocess out from under
   an in-flight job and confirming `status()` reports `DIED` rather than
   hanging forever waiting on a reply that will never come.

`STUCK` is honestly a heuristic — time-since-last-update while the worker
process is still alive, nothing more — and is tested as exactly that: a
deliberately tiny `stuck_after` threshold makes a perfectly healthy,
still-running job report `STUCK`, proving the mechanism measures elapsed
time, not genuine wedged-ness, which (per the design doc) isn't provable
without cooperation from whatever's running inside the worker.

### `RemoteAppLink` and `BokehAppContext` integration

`RemoteAppLink` (`cubevis/remote/_link.py`) owns `(mgr, transport,
sync_bridge, worker_target_name)`. `RemoteAppLink.open(kernel_manager,
worker_target_name=..., worker_module=..., timeout=...)` runs a bootstrap
cell in the kernel (`ensure_remote_worker(build_worker_process_delegate(
worker_module), target_name=worker_target_name)` — idempotent, same
namespace-marker mechanism as Chunk 1, reused unchanged) and then connects
via `open_remote_kernel_link`. `close()` calls the supervisor's own
`shutdown_worker` handler (which itself awaits `WorkerProcessTransport
.close()` before replying) and only then reports success — confirming the
worker subprocess's actual OS exit (checked independently via `os.kill(
pid, 0)` in tests, not by trusting `cubevis`'s own self-report) rather
than merely that `P_local`'s references to it were dropped.

*(Chunk 1c note: `RemoteAppLink`'s shape described in this paragraph is
Chunk 1b's original, single-worker version. Chunk 1c's "Known limitation"
below explains why it needed to change, and Chunk 1c's own section
describes the pool-aware replacement — `open()` no longer spawns a
worker; `create_context()`/multi-context `close()` do instead.)*

`_bokeh_integration.py`'s `new_comm_mgr_with_remote_teardown()` wires
`remote_link.close()` into the browser-facing `comm_mgr`'s `on_shutdown`
callback. **Investigation finding, not assumption:** `BokehAppContext
.show()`'s call to `BokehInit.clear_app_context()` is *not* an
app-closed signal — reading `_bokeh_app_context.py` directly shows it
fires immediately after HTML generation/serialization, unrelated to
whether the app instance is still running. The real teardown signal is
`CommMgr.shutdown()`, which fires the constructor's `on_shutdown`
callback exactly once, whether triggered explicitly or by the reconnect
watchdog after `reconnect_grace_period`/`reconnect_timeout` elapses.
Worth stating plainly rather than glossing over: `on_shutdown` is a
**synchronous** callback, but confirming subprocess exit is inherently
async, so the cascade schedules `link.close()` as a background task
rather than awaiting it — `CommMgr.shutdown()` itself can return before
worker teardown is fully complete. There is no other awaited hook inside
`shutdown()` to use instead without a more invasive change to
`_comm_mgr.py`, which was out of this chunk's scope.

A second, purely mechanical finding from the same investigation: a bare
`app_context.remote_link = link` does not work as written, because
`BokehAppContext` is a Bokeh `HasProps` subclass and `HasProps
.__setattr__` rejects any attribute name that isn't a declared Bokeh
`Property` — the same restriction `CommMgr`'s own `role` attribute (Chunk
1) hit. Fixed with a small, explicitly-scoped addition to
`_bokeh_app_context.py`: a `_remote_link = None` class attribute plus a
`remote_link` property/setter. This is the one place Chunk 1b touched an
existing file beyond what Chunk 1 already changed — the kickoff for this
chunk explicitly scoped `_bokeh_app_context.py` as touchable for this
reason.

Tested without any GUI/browser, matching Chunk 1's own standard:
`BokehAppContext` instances constructed directly, `remote_link` confirmed
per-instance (not shared globally) with **two concurrent** `RemoteAppLink`s
against two separate kernels, each with its own live worker subprocess
(confirmed by distinct pids), and the `on_shutdown` → `remote_link.close()`
cascade confirmed by killing a worker process and observing the cascade
actually run.

### A cross-loop hazard, found and fixed during this chunk

Worth recording since it's a real, easy-to-reintroduce mistake: an early
version of `RemoteAppLink.open()` ran `transport.run()` via a
`SyncBridge`'s own dedicated background thread+loop
(`bridge.run_background(transport.run())`), while `mgr` — and its
internal `asyncio.Lock`, used by every `send()` call — had been
constructed on the *caller's* ambient loop. Mixing the two is a genuine
cross-event-loop hazard, not a style preference: `asyncio.Lock` isn't
thread-safe across loops, and the failure mode is a **silent deadlock**,
not an exception — the first couple of messages succeed (the lock happens
to start unheld), and it wedges only once real contention is possible,
which made it easy to miss in a quick smoke test and only surface once a
`RemoteAppLink`-driven demo tried a longer sequence of calls. Fixed by
scheduling `transport.run()` via a plain `asyncio.ensure_future()` on the
same ambient loop `mgr`/`transport` were built on. **Consequence, flagged
explicitly rather than silently left unresolved:** `RemoteAppLink`'s own
`sync_bridge` attribute, kept on the object per the original design
sketch's suggested shape, is not yet wired up to anything — safely
letting a genuinely synchronous caller (no ambient loop at all, the same
shape `next(gclean)` will need in Chunk 3) drive `request()` calls
against this same `mgr` requires either routing such calls through
`asyncio.run_coroutine_threadsafe` against the *ambient* loop specifically
(not the bridge's own loop), or constructing the whole link from inside
`bridge.run()` in the first place so everything shares one loop from the
start. Still unresolved as of Chunk 1c (that chunk's own `RemoteAppLink`
rewrite carries `sync_bridge` forward unchanged, for the same reason) —
flagged for Chunk 3, whichever needs a synchronous call site against a
`RemoteAppLink`-backed `mgr` first.

### Known limitation: the one-worker-per-kernel model, and what it actually constrains

**Resolved by Chunk 1c — recorded here for history, not as a live gap.**
`ensure_remote_worker`'s `_NAMESPACE_KEY` marker is a single scalar, not
keyed by `target_name` — **this is deliberate, not a bug**, and an
earlier draft of this document incorrectly described it as one. The
reason is multiplexing simplicity, not (as a still-earlier draft
claimed) a fixed name being required for reconnection — see the
architecture doc §2e/§1 for the corrected reasoning: one Jupyter comm
target per kernel is what lets `CommMgr`'s own sub-multiplexing
(ordinary `Comm`/`comm_id` categories within one channel) do the work,
rather than needing N independent `AsyncKernelClient`s each polling the
same kernel's iopub stream with their own reconnect bookkeeping.
`target_name` itself, meanwhile, is generated per bootstrap, not fixed —
see this document's Chunk 1 section for the small revision that implies
to `_worker.py`. What the one-comm-per-kernel discipline constrains is
Layer 1 specifically. It does **not** constrain how many OS subprocesses
that one Layer-1 worker chooses to manage internally, since
`build_worker(mgr)`'s return value is opaque to `ensure_remote_worker`.

Chunk 1b's actual delivered code did not yet take advantage of that
distinction, though: `RemoteAppLink.open()`'s `worker_target_name`
parameter was caller-supplied, and `_supervisor.py`'s
`build_worker_process_delegate()` built exactly one subprocess per
bootstrap — workable for one app, one kernel, but not for two apps (or
an app plus an ad hoc eval session) sharing a single kernel, which would
require a second Jupyter-level bootstrap against the same kernel process
— quietly reintroducing the multiple-Jupyter-comms problem the
architecture doc's §1 explicitly rejects, independent of whether the two
calls' `target_name`s happen to collide or not (a generated `target_name`
removes the *collision* risk specifically, but doesn't by itself make a
second Jupyter-level bootstrap the right design). Restructuring
`_supervisor.py` around a pool of execution contexts — keeping the
single Layer-1 comm and moving "more than one worker" one layer deeper,
where it belongs — was Chunk 1c's first task; see that section for the
`ExecutionContextPool` that now replaces `build_worker_process_delegate()`.

### Definition of done — checklist, resolved

- ✅ `WorkerProcessTransport`/`WorkerCommTransport` request/response
  **and** push round trips, against a real subprocess.
- ✅ Stderr relay: a worker that fails on startup produces a diagnosable
  message in the supervisor's log.
- ✅ `RemoteAppLink` constructed/torn down cleanly; worker subprocess
  confirmed gone (checked via the OS directly) after `close()`.
- ✅ "died" and "completed" demonstrated end-to-end against a real
  subprocess; "stuck" tested as, and documented as, a heuristic.
- ✅ `remote_link` confirmed per-`BokehAppContext`-instance with two
  concurrent instances, no GUI required.

One thing found via a hand-run demo script, worth recording as a
deliberate non-goal rather than a gap: **worker→`P_local` push relay is
not automatic.** `WorkerProcessTransport`'s push capability is proven
directly (the transport carries an unsolicited message from worker to
supervisor correctly), but relaying an arbitrary push *on to* `P_local`
needs a handler registered for that specific `message_id` on the
supervisor's worker-facing comm — `CommMgr` has no wildcard/catch-all
handler — which is exactly the kind of application-specific wiring (a
`visplot` progress update, an `iclean` convergence update) that belongs
to whichever chunk has an actual push type to relay, not to this chunk's
generic `_supervisor.py`.

---

## Chunk 1c — Remote execution and object framework

**Status: implemented and tested.** Real subprocess/real local-kernel
testing throughout (39 tests, all passing, including every carried-forward
Chunk 1/1b test plus this chunk's own new ones), matching Chunk 1/1b's own
bar. The one genuine gap, stated plainly rather than glossed over: this
sandbox has no SSH/cluster access, so the actual `sshpyk`-provisioned,
cross-host path is unverified here — see "What is and isn't verified,"
below, for exactly what that does and doesn't mean for confidence in the
result.

### Why this chunk exists, briefly

Two things came into focus once real usage was discussed concretely
(rather than only Chunk 2/3's specific, already-known method sets):
first, that "evaluate an arbitrary Python statement/expression on a
specific remote worker, with pre-configured objects available" is a
generic capability independent of `visplot` or `iclean`, not something
either application's design demands in an application-specific shape;
second, that supporting it safely — multiple genuinely independent
evaluation contexts that can't interfere with each other — requires the
same "more than one worker per kernel" capability Chunk 2's Datashader
work and Chunk 3's `gclean` object both independently want, for the
unrelated reason of GIL/process isolation. Building the pool once, here,
serves both motivations.

### The execution-context pool

`_supervisor.py`'s Chunk 1b `build_worker_process_delegate()`/
`_spawn_and_wire_worker()` (build exactly one `WorkerProcessTransport`
-backed subprocess per bootstrap) is replaced by `ExecutionContextPool`,
a `Dict[execution_context_id, WorkerDelegate]` owned by the (still
singular) Layer-1 `mgr`. `build_worker_pool()` is the `build_worker`
callable `ensure_remote_worker()` invokes — the pool object itself is
what gets kept alive across a reattach by Chunk 1's existing
namespace-marker mechanism, unchanged. `ensure_remote_worker()` itself
was not touched by this chunk at all beyond the `target_name`-generation
revision this document's Chunk 1 section already records.

New P_local-facing handlers, registered on the (still singular) Layer-1
`mgr`'s `"worker"` comm — the exact wire schema, as implemented and
tested:

- **`create_context`** — request payload `{"worker_module": <str,
  optional, default "cubevis.remote.worker_main">, "config": <dict or
  null>}`; reply `{"context_id": <generated UUID str>, "pid": <int>}`.
  Spawns a fresh `WorkerProcessTransport`, generates the `context_id`,
  adds a `WorkerDelegate` (its own `CommMgr`/`Comm`/`JobRegistry`, not
  shared with any other context) to the pool. `config` is never
  interpreted at this layer — it is forwarded unopened to the freshly
  spawned worker as its opening `configure` message (see "Worker
  configuration," below), which is **always** sent, even when the caller
  passed no `config` at all (an empty dict is sent in that case) —
  `create_context`'s own reply is only returned once that opening
  round trip completes, which is what guarantees the `configure` message
  really is the first thing the worker processes, not merely intended to
  be.
- **`dispatch_fast`** — payload `{"context_id": <str>, "message_id":
  <str>, "payload": <dict>}`; reply is whatever the worker's own handler
  for `message_id` returns, unwrapped (Chunk 1b's existing contract,
  unchanged — see that chunk's dispatch section for why this must only
  be used for promptly-returning commands).
- **`dispatch_async`** — same payload shape as `dispatch_fast`; reply
  `{"job_id": <generated UUID str>}`, returned immediately regardless of
  how long the dispatched command actually takes.
- **`job_status`** — payload `{"context_id": <str>, "job_id": <str>}`;
  reply is `JobRegistry.status()`'s existing dict (`status` one of
  `working`/`completed`/`died`/`stuck`, plus `result`/`error` once
  resolved) — unchanged from Chunk 1b, now looked up in the matching
  context's own `JobRegistry` rather than one shared instance.
- **`shutdown_context`** — payload `{"context_id": <str>}`; reply
  `{"closed": true, "returncode": <int>, "context_id": <str>}` on
  success, or `{"closed": false, "error": <str>}` if `context_id` is
  unknown. Removes the entry from the pool only after
  `WorkerProcessTransport.close()` confirms the subprocess's actual OS
  exit — the same standard Chunk 1b held `shutdown_worker` to, now
  applied per context.
- **`list_contexts`** — payload `{}`; reply `{"contexts": [{"context_id":
  ..., "pid": ..., "alive": <bool>, "worker_module": ...}, ...]}`. Not
  named in the kickoff doc's task list, added for the same "cheap given
  the pool already exists, and useful for the demo/tests" reasoning as
  `supervisor_info`, below — exercised directly in
  `test_remote_app_link.py::test_two_execution_contexts_are_genuinely_separate_subprocesses`.
- **`supervisor_info`** — payload `{}`; reply `{"pid": <int>, "hostname":
  <str>}` for the supervisor kernel process itself. Added, not requested
  by the kickoff doc, specifically so a caller/demo can print "process,
  kernel, host" information for all three tiers (`P_local`, supervisor,
  worker) from one place — used by both new `try_*.py` demos' startup
  banner.

**`RemoteAppLink` becomes pool-aware.** `RemoteAppLink.open(kernel_manager,
worker_target_name=..., timeout=...)` no longer spawns any worker
subprocess itself — it only bootstraps/connects to the kernel's Layer-1
pool (`open()` dropped Chunk 1b's `worker_module` parameter entirely,
since there is no longer a single worker to configure at open-time). A
link with no contexts created against it is a normal, if useless, state.
`link.create_context(worker_module=..., config=...)` wraps the
`create_context` wire operation and returns an `ExecutionContext` — the
object application code actually holds — carrying `context_id` and `pid`
plus `dispatch_fast`/`dispatch_async`/`job_status`/`create_object`/
`call_method`/`dispose_object`/`eval_code`/`exec_code`/`worker_info`/
`shutdown` convenience methods (each a thin wrapper over `dispatch_fast`/
`dispatch_async`, described in "Object registry" and "Eval/exec," below).
`link.close(timeout=20.0)` now tears down **every** context the link
created (via each context's own `shutdown`, catching per-context
exceptions so one unreachable context doesn't prevent tearing down the
rest) and returns `Dict[context_id, result]` rather than one bare result
— Chunk 1b's `close()` returned a single dict; every caller of the old
single-worker shape had to be updated for this (see "Test suite
reconciliation," below).

### Worker configuration via an opening wire message

`worker_main.py` no longer hardcodes `_register_toy_handlers` at import
time. Instead, a small, fixed set of generic wire handlers — `configure`,
`worker_info`, `create_object`, `call_method`, `dispose_object`,
`eval_code`, `exec_code` — are registered unconditionally at worker
startup: these are the reusable framework surface, never
application-specific, so there's nothing to defer them for. `configure`
is the opening configuration message `create_context` always sends
(described above); its payload is `{"register_function": <dotted
import path, optional>, "kwargs": <dict, optional>}`. `_load_dotted()`
resolves either colon form (`"package.module:attr"`, unambiguous, the
same convention Python entry points use) or plain dotted form
(`"package.module.attr"`, split on the last `.`). If `register_function`
is given, it's imported and called as `register_function(comm, registry,
**kwargs)` — mirroring `build_worker(mgr)`'s own shape from Chunk 1: handed
the worker's own `Comm` and `ObjectRegistry`, free to register whatever
classes/handlers it wants. If no `register_function` is given (including
the `config or {}` case `create_context` always sends when the caller
passed nothing), the *default* registration — Chunk 1b's original toy
handlers (`ping`/`add`/`slow_echo`/`crash`/`trigger_push`) — is installed
instead, so Chunk 1b's existing tests and demo keep passing with only the
one small handshake addition described below, not a rewrite of their
actual assertions.

**Definitely-worth-flagging consequence, found while writing this
chunk's own tests, not anticipated by the kickoff doc:**
`test_worker_process_transport.py` and `test_async_dispatch.py` both talk
to `WorkerProcessTransport` directly, one layer below `_supervisor.py`'s
pool — they never send `create_context`'s implicit `configure` handshake,
because there is no `_supervisor.py` in their call path at all. Against
the old worker_main.py (toy handlers registered unconditionally at
import) this didn't matter; against the new one (toy handlers only
installed on `configure`) their original bodies would have hung waiting
on replies from handlers that were never registered. Fixed with the
smallest possible change: their shared `_spawn_supervisor_side()` helper
now sends one `request(comm, "configure", {})` immediately after opening
the comm, before returning — exactly the handshake `create_context`
performs for any real caller. Nothing else in either file changed; both
still pass unmodified past that one addition.

### Object registry and handle table

`_object_registry.py`'s `ObjectRegistry` — deliberately a plain,
dependency-free class (no `cubevis` imports at all) so it has its own
fast unit test (`test_object_registry_unit.py`) independent of any
subprocess/transport. One instance per worker process (so per execution
context — see "Isolation," below), holding two tables: `_classes`
(name → class, populated only via `register_class()`, called from a
`register_function` at configure time) and `_objects` (generated handle
→ live instance, populated by `create_object`). Worker-side wire
handlers, registered unconditionally alongside `configure` (see above):

- **`create_object`** — payload `{"class_name": <str>, "args": <list>,
  "kwargs": <dict>}`; reply `{"handle": <generated UUID str>}`, or an
  `{"error": ...}` reply (via `CommMgr._handle_request`'s existing
  exception-to-error-reply conversion — Chunk 1b's own crash-
  diagnosability convention, unchanged) if `class_name` was never
  registered in this context.
- **`call_method`** — payload `{"handle": <str>, "method": <str>,
  "args": <list>, "kwargs": <dict>}`; reply is the method's **raw**
  return value, not wrapped in an envelope — a numpy array, a plain
  scalar, `None`, whatever the method returns, travels through
  `cubevis.utils.serialize`/`deserialize` exactly as any other message
  payload already does (Chunk 1b never required handler return values to
  have a specific shape either). One real consequence of "raw, unwrapped"
  worth stating plainly: a method that itself legitimately returns
  `None` and a call that failed (surfaced as `{"error": ..., "traceback":
  ...}`) are not distinguishable from the reply's shape alone in the
  fully generic case — `ExecutionContext.call_method()`'s docstring
  states this rather than hiding it; a caller that needs to tell the two
  apart reliably should check for an `"error"` key, which is exactly what
  this chunk's own tests do at the negative-path assertions (see
  `test_object_registry_e2e.py`).
- **`dispose_object`** — payload `{"handle": <str>}`; reply
  `{"disposed": <bool>}` — `true` if a live object was actually removed,
  `false` if the handle was already gone (disposing twice is a no-op, not
  an error).
- **`eval_code`**/**`exec_code`** — see "Eval/exec," below.

### Eval/exec — the convention actually chosen

- **`eval_code`** — payload `{"code": <str>}`; reply is
  `eval(code, namespace)`'s result, exactly matching plain Python `eval()`
  semantics for a single expression.
- **`exec_code`** — payload `{"code": <str>}`; reply is read back from a
  designated variable, `_result`, in the worker's persistent namespace
  **after** `exec(code, namespace)` runs — `_result` is popped from the
  namespace immediately before every `exec_code` call (not merely left
  unset), so a stale value from an earlier call can never be silently
  mistaken for the current one's result; a call that never assigns
  `_result` gets back `None`. This was chosen over mirroring a notebook
  cell's last-expression display hook — the mechanism `client.execute()`
  itself already relies on for this project's own bootstrap cells, and
  the tempting first instinct — specifically because "the last line" is
  not reliably "the interesting value" once a caller is composing several
  statements together, where an explicit convention composes better than
  an implicit one. Verified end-to-end in `test_eval_exec.py`, including
  that `_result` does not leak between calls and that the namespace
  itself (ordinary variables, not just `_result`) persists across calls
  on the same execution context, since it's one worker process's one
  dict for its whole lifetime.

Both handlers share one persistent `namespace: Dict[str, Any]`, seeded at
worker startup with `{"__builtins__": __builtins__, "_registry":
registry}` — the `_registry` name is what lets an eval/exec snippet reach
an object created via `create_object` (e.g. `_registry.get_object(handle)
.some_attr`), so the two Task 4 use cases compose rather than living in
separate silos; verified directly in
`test_eval_exec.py::test_eval_exec_can_reach_registry_created_objects`.

**Why `call_method` is preferred by default over `eval_code`,** unchanged
from the pre-implementation design and confirmed still true: a Jupyter
kernel is already arbitrary Python code execution, so adding a
constrained eval/exec capability on top of a worker process reachable
only through an authenticated SSH+kernel connection reuses an existing
trust boundary rather than introducing a new one. The preference is about
blast radius and clarity — a structured target/method/args call can't
accidentally do anything beyond invoking one method — not about safety in
the sense of a new exposure.

### Isolation, and why it needs Layer 2, not just Layer 3's handle table

Confirmed by test, not just by construction:
`test_worker_configuration_isolation.py` configures two execution
contexts under one supervisor kernel with two disjoint
`register_function`s (`cubevis/remote/_test_registrations.py`'s
`register_basic` — `Counter`/`NumpyEcho` — and `register_alt` —
`OnlyInAlt`, registered nowhere else), and confirms each context can only
`create_object` the classes *it* was configured with — attempting the
other context's class fails with an `{"error": ...}` reply naming the
missing class, at both the `create_object` wire operation and (redundant
but cheap to check) the `eval_code("_registry.registered_class_names()")`
level. This holds "for free," in the strongest sense: each execution
context is a genuinely separate OS process (confirmed by distinct pids in
the same test), so there is no shared Python-level state to leak between
them even by accident — the test proves this in practice, it doesn't
just assert it by construction.

### Serialization — confirmed against real payloads, not just scalars

Chunk 1's finding (`cubevis.utils.serialize`/`deserialize` = Bokeh's own
`Serializer(deferred=False)`/`Deserializer`, encoding inline rather than
via a separate buffer channel) is exercised in this chunk against a real
numpy array, not just JSON-native values:
`test_object_registry_e2e.py::test_create_object_call_method_dispose_against_real_worker`
round-trips a 2×3 `float64` array as a `call_method` argument **and**
return value through the real wire (a genuine worker subprocess, real
length-prefixed framing, real `serialize`/`deserialize` on both ends) and
confirms the result is numerically identical to computing the same thing
in-process — the risk flagged in this document's Chunk 1c design section
("Consequence for `create_object`/`call_method` results that carry real
array data") is resolved for the case actually tested; the payload-
chunking question for very large results (raised in that same section)
remains unbuilt, exactly as flagged, and is left for whichever of Chunk
2b's payloads turns out to need it in practice.

### The `cubevis`-level kernel-persistence manifest

`_persistence.py`'s `KernelPersistenceManifest` — a single JSON file
(default location `<jupyter_runtime_dir()>/cubevis-kernel-manifest.json`,
next to where `sshpyk`'s own `persistent_file`s live by default, but a
genuinely separate file, never duplicating their contents), one entry per
caller-chosen label:

```json
{
  "<label>": {
    "persistent_file": "<sshpyk persistent_file path or name>",
    "kernel_name": "<kernelspec name, for AsyncKernelManager(kernel_name=...)>",
    "created_at": "<ISO-8601 UTC timestamp>"
  }
}
```

Three operations, each re-reading the file (the class is stateless
between calls, deliberately — see below): `record(label, persistent_file,
kernel_name=None)` (overwrites any previous entry under the same label —
superseding, not appending), `outstanding()` (everything currently
recorded), `forget(label)` (removes an entry; returns `False`, not an
error, if there was nothing to remove). Writes are atomic
(`tempfile.mkstemp` + `os.replace`) so a crash mid-write can't leave a
half-written manifest behind. Layered strictly on top of `sshpyk`'s own
mechanism, not a reimplementation of any part of it — in particular,
**no liveness check or reattach logic lives in this class at all**; the
module docstring states this as a deliberate boundary (reimplementing
even a lightweight reattach probe here would duplicate logic `sshpyk`
already owns), and `test_persistence_manifest.py` demonstrates the
liveness check as the caller's own responsibility instead.

**What is real vs. realistically faked in the test, stated as plainly as
Chunk 1c's other sections state their own limits:** this sandbox has no
SSH/cluster access, so a real `sshpyk` `persistent_file` was not
available to test against. `test_persistence_manifest.py` uses a real
`AsyncKernelManager`-started local kernel's own connection file as the
stand-in `persistent_file` — same role (a JSON file that lets a
*separate* process reconnect to an already-running kernel without
starting a new one), same generic `jupyter_client`-level mechanism
`sshpyk`-provisioned kernels use identically underneath their own
SSH-specific layer. The test: records the connection file under a label
via one `KernelPersistenceManifest` instance; constructs a **second,
independent instance pointed at the same path** (simulating `P_local`
itself having restarted) and confirms it sees the entry — proving it is
neither silently reused from in-memory state (there is none held between
calls) nor silently discarded; performs the actual liveness/reattach
proof by loading the recorded connection file into a fresh
`AsyncKernelManager` and completing a real `kernel_info_request` round
trip against the still-running kernel; then `forget()`s the label and
confirms a third fresh instance no longer sees it. What this does **not**
exercise, named explicitly rather than left implicit: `sshpyk`'s own
remote-PID reverification during a real reattach — that remains `sshpyk`'s
job, unverified here for the same reason every other Chunk 1c real-kernel
test's `sshpyk` path is unverified (no SSH/cluster access in this
sandbox), not because `_persistence.py` does anything differently in that
case.

### Interface correction: `RemoteReductionContext`'s method-to-primitive mapping

Unchanged from the pre-implementation version of this section — carried
forward verbatim, since it concerns Chunk 2's future implementation, not
anything this chunk needed to revisit: `query_raster`/`query_columns`/
`probe_raster_pixel`/`probe_scatter_pixel` are plain `def`s in
`VisibilityReader`/`LocalVisibilityReader`, confirmed by direct
inspection of `visibility_raster.py:943`'s `_render()`, which calls
`self._backend.query_raster(...)` and immediately unpacks/indexes the
return value with no `await` anywhere on the call stack — an `async def`
implementation would hand it a coroutine object instead and break at
runtime. The corrected mapping (`SyncBridge.run(request(...))`-wrapped
`call_method` calls, not bare `async def`) stands as designed; with this
chunk's object protocol now actually built and tested, each of these
becomes a real `call_method` against a pre-created backend object living
in a dedicated execution context, wrapped in `SyncBridge.run(...)` on the
`RemoteReductionContext` side — the DTO marshaling this chunk's
serialization section covers, not a bespoke per-method wire contract.
`ReductionContext.submit()`'s `Future`-bridge remains exactly as flagged
before: undesigned, a concrete task for whichever chunk implements
`submit()`, not solved by anything built here.

### Test suite reconciliation

Every Chunk 1/1b test file was carried forward; two needed rewriting for
the pool-aware API, two needed the one-line `configure` handshake
addition described above, the rest needed nothing:

- **Unchanged, verbatim:** `test_bridge.py`, `test_bug_reproduction.py`,
  `test_kernel_transport_spike.py`, `test_role_mirroring.py`,
  `test_worker_start_reattach_unit.py`,
  `test_worker_start_reattach_real_kernel.py` — none of these touch
  `_supervisor.py`/`_link.py`/`worker_main.py`'s changed surface at all.
- **One-line addition (the `configure` handshake):**
  `test_worker_process_transport.py`, `test_async_dispatch.py` — see
  "Worker configuration," above.
- **Rewritten for the pool-aware API, same assertions/intent preserved:**
  `test_remote_app_link.py` (now `link.create_context()` +
  `ctx.dispatch_fast(...)`/`ctx.pid`, and `close()`'s
  `Dict[context_id, result]` shape, checked by `ctx.context_id`; a second
  test added for the pool guarantee itself — two concurrent contexts,
  confirmed separate pids, one torn down without affecting the other),
  `test_bokeh_app_context_integration.py` (same substitution:
  `ctx.remote_link.create_context()` then `exec_ctx.dispatch_fast(...)`
  in place of the old hand-built `request(comm, "dispatch_fast",
  {"message_id": ..., "payload": ...})` envelope against
  `link.mgr.open("worker")` directly).
- **New for this chunk:** `test_object_registry_unit.py` (fast, no
  subprocess), `test_object_registry_e2e.py` (real subprocess, including
  the numpy round trip), `test_eval_exec.py`, `test_worker_configuration_isolation.py`,
  `test_persistence_manifest.py`.

`examples/try_local_or_remote_kernel.py` needed no changes (it bootstraps
its own custom `build_worker`, bypassing `_supervisor.py` entirely).
`examples/try_worker_process.py` was rewritten for `create_context()`,
preserving its original nine-step structure. Two new demos were added:
`examples/try_remote_object.py` (generalized remote object creation/
invocation, using the same `register_basic`/`Counter`/`NumpyEcho`
fixtures the tests use) and `examples/try_remote_eval.py` (generalized
remote eval/exec, plus a genuine `--interactive` read-eval-print-loop
mode against the live remote worker). Both print `P_local`/supervisor/
worker pid+hostname once at startup, per the kickoff doc's explicit
requirement, using `supervisor_info()`/`worker_info()`/`create_context`'s
returned `pid`. All four demo scripts were run against a real local
`python3` kernel and produce the expected output end-to-end (not merely
written and assumed correct).

### Definition of done — checklist, resolved

- ✅ A real subprocess-backed pool: at least two `execution_context_id`s
  live concurrently under one supervisor kernel, each independently
  reachable, each confirmed a genuinely separate OS process by pid
  (`test_remote_app_link.py::test_two_execution_contexts_are_genuinely_separate_subprocesses`).
- ✅ `create_object`/`call_method`/`dispose_object` tested end-to-end
  against a real subprocess, including a numpy-array-typed value, through
  the real serializer (`test_object_registry_e2e.py`).
- ✅ `eval_code`/`exec_code` tested end-to-end, including the documented
  `_result` multi-statement convention (`test_eval_exec.py`).
- ✅ Worker configuration via the opening message tested: two contexts
  under the same kernel, configured with two different registration
  functions, each only having access to what it was configured with
  (`test_worker_configuration_isolation.py`).
- ✅ `RemoteAppLink.create_context()`/multi-context `close()` tested to
  Chunk 1b's own standard — no GUI, real subprocess exit confirmed per
  context (`test_remote_app_link.py`, `test_bokeh_app_context_integration.py`).
- ✅ The persistence manifest tested against a real (local-kernel-backed)
  persistent-file lifecycle: written on creation, found and surfaced on a
  simulated fresh-process restart via an independent
  `KernelPersistenceManifest` instance, liveness proven via a real
  jupyter_client reattach, and `forget()` confirmed removed
  (`test_persistence_manifest.py`).
- ✅ Two `try_*.py` demos, one per use case, each printing process/
  kernel/host information for `P_local`/supervisor/worker once at
  startup, both run and confirmed working end-to-end.

### What is and isn't verified — stated honestly, matching Chunk 1/1b's own standard

**Verified, for real, in this sandbox:** every wire operation above,
against a real `ipykernel` subprocess supervisor and real
`asyncio.subprocess` worker processes — no mocks anywhere in the test
suite, matching this project's stated testing philosophy throughout.
39 tests pass, including every carried-forward Chunk 1/1b test. All four
`try_*.py` demo scripts were actually executed against a real local
`python3` kernel, not merely written.

**Not verified here, same limitation as every other chunk's real-kernel
tests:** the actual `sshpyk`-provisioned, cross-host path. This sandbox
has no SSH/cluster access. Nothing in this chunk's code is
`sshpyk`-specific (confirmed by the same `grep`-for-the-string check
Chunk 1's own verification used — `sshpyk` appears only in comments/
docstrings), and every real-kernel test here goes through the identical
standard `AsyncKernelManager`/`AsyncKernelClient` API surface an
`sshpyk`-provisioned kernel uses underneath its own SSH layer, so the
same "swap `--kernel-name`" argument Chunk 1's own demo relies on applies
unchanged to both new demos in this chunk. But that argument is
inference from a shared API surface, not a substitute for actually
running it against a real cluster kernel — stated as such, not implied
to be equivalent.

### Open questions carried forward

- Execution-context lifecycle: nothing yet cleans up a context nobody
  ever reconnects to. Related to, but not solved by, the persistence
  manifest (which tracks *kernels*, not the contexts inside them).
- `submit()`'s `Future`-bridge — the mechanism, not just the mapping, is
  undesigned.
- Payload chunking for large `call_method` results — a real direction
  (the same pattern Chunk 1's Colab transport already uses for a
  different constraint), not built in this chunk; flagged for whichever
  of Chunk 2b's payloads turns out to need it.
- The harder reconnection scenario named in the design doc §2f
  (`P_local`'s own process restarting from scratch and rediscovering a
  specific pre-existing execution context by something other than a
  remembered id) — explicitly out of scope until a real scenario demands
  it.
- The real `sshpyk`/SSH/cluster path for this chunk's own new demos and
  tests, for the reason stated above.

---

## Chunk 2 — `visplot` remote data path

**Status: designed, not yet implemented.** Committed to building on
Chunk 1c's object framework from the start (per explicit direction) —
`RemoteReductionContext`'s methods are `call_method` calls against
objects living in a dedicated execution context, not hand-rolled proxies
of their own, even though Chunk 2's own currently-known bottleneck
(bandwidth/materialization risk, not confirmed GIL-blocking) doesn't by
itself demand a separate process. Prioritized over Chunk 3: `iclean` is
already released (lower near-term appetite for change); `visplot` is
still in active development and remote execution is valuable to its own
developers now.

### Isolation boundary already exists

`VisibilityPlot` never touches the MS directly; it goes through
`self._backend`, typed as `VisibilityReader` (`visibility_plot.py:85,266`).
`reduction_context.py`'s own docstring (lines 27–46) already names the
intended remote implementation — `RemoteReductionContext` — satisfying
**both** `ReductionContext` and `VisibilityReader` at once. Nothing about
`VisibilityPlot`/`VisibilityPlotter` needs restructuring; they already
treat the reader as swappable — which is exactly the property Chunk 1c's
interface correction (above) preserves and an `async def` implementation
would have quietly broken.

`VisibilityReader` (`visibility_reader.py`) is a `@runtime_checkable
Protocol` with exactly four methods: `query_raster`, `query_columns`,
`probe_raster_pixel`, `probe_scatter_pixel`. Two more are needed beyond
the formal protocol, evidenced by `LocalVisibilityReader`
(`local_visibility_reader.py`): `metadata()` (used by `open_ms`/`open_ps`
to build `ObservationMetadata`) and `axis_info()` (used for axis
labeling — its own comment warns a missing implementation silently
produces "correct-looking output, wrong label"). `RemoteReductionContext`
must implement both, not just the four protocol methods.

### Method-to-primitive mapping (corrected — see Chunk 1c)

All six of `query_raster`/`query_columns`/`probe_raster_pixel`/
`probe_scatter_pixel`/`metadata`/`axis_info` are `SyncBridge`-wrapped
`call_method` calls against a backend object living in a dedicated
execution context, matching `LocalVisibilityReader`'s own calling
convention exactly, so `VisibilityPlot`/`VisibilityPlotter` never need to
know which implementation they're talking to. `ReductionContext.submit()`
is the one exception — see Chunk 1c's `submit()`/`Future`-bridge
discussion.

**With Chunk 1c:** the backend object (an opened MS/`ReductionContext`)
is created once, via `create_object`, in a `visplot`-dedicated execution
context — not per-call, and not sharing a process with anything else's
work. `RemoteReductionContext`'s own methods hold that object's handle
and issue `call_method` against it.

### 2a. Raster — no wire-contract change needed

**`query_raster()` already correctly bounds itself against an arbitrarily
large MSv4, verified in the actual implementation, not assumed:**

- `MSv2Backend._raster_2d` (`msv2_backend.py:1233-1310`) builds the
  quantity array via dask-array primitives (`da.absolute`, `da.angle`)
  and reduces over non-display dimensions with `.mean(...)` on that
  dask-backed array — which stays **lazy**.
- `_decimate_agg` strides that still-lazy graph *before* any
  materialization.
- The single `.compute()` call (`msv2_backend.py:1134`,
  `partitions_2d.append(arr.compute())`) happens **after** striding — so
  Dask only ever reads the strided cells from disk, regardless of the
  true size of the underlying MS/Processing Set.
- Output is capped at `max_cells` (default 2,000,000 cells, "≈16 MB at
  float64" per the docstring at `reader.py:738`) — a fixed bound
  independent of dataset size.

So `RemoteReductionContext.query_raster()` can be close to a mechanical
relay of the existing contract — a `call_method` whose result is exactly
what `LocalVisibilityReader.query_raster()` already returns, marshaled
through Chunk 1c's serialization. The only tuning worth doing is passing
a smaller `max_cells` by default when dispatching remotely than the local
2M default, trading resolution for bandwidth using a parameter the
interface already exposes for exactly this purpose.

**Local recompositing must stay local — do not route it through the
wire.** `VisibilityRaster._shade_viewport()`
(`visibility_raster.py:1380-1409`) operates purely on the cached
`self._agg` — zero backend calls. Pan/zoom within the cached agg's
resolution, color-mode toggles (`_handle_set_color_mode_raster`), and
scaling changes (`_handle_update_scaling_raster`) all resolve to this
same free, local recomposite today. An earlier version of this design
proposed shipping a fully-rendered image over the wire on every render
call — **rejected**: it would turn every currently-free pan/zoom drag
into a network round trip. The wire boundary is `query_raster()` itself
(already correct, see above), not the per-viewport render call.

### 2b. Scatter — the real gap, and the fix is about correctness, not just bandwidth

**`query_columns()` has no equivalent bound, and this is a pre-existing
risk independent of remote execution.** `MSv2Backend.query_columns`'s
"adaptive pipeline" (`msv2_backend.py:843-846` — serial stack under 500K
samples, "fused `dask.compute()` + numpy ravel" from 500K–5M, "+ parallel
Datashader-ready DataFrames" above 5M) materializes every matching row at
every tier; the tiers change *how* it computes, never *whether* the full
match set gets pulled into memory. A broad scatter selection against a
terabyte-scale MSv4 could try to materialize the entire matched slice —
**a memory-safety problem even in today's local, non-remote case**, not
only a network-bandwidth one.

**The fix already has a documented blueprint, currently unimplemented.**
`reader.py`'s abstract `query_columns` docstring (lines 651, 669-670,
predating the concrete implementation's deviation from it) already
specifies the correct shape: *"Datashader consumes this Dataset directly
via `Canvas.points()`. No pre-averaging is performed... Call `.compute()`
only inside Datashader (never materialise the full array in Python)."*
Datashader's `Canvas.points()` genuinely accepts a Dask-backed input and
performs the pixel-binning via Dask's own chunked reduction, without ever
fully materializing the input.

**No `max_cells`-style cap is needed for scatter, unlike raster.** Raster
needs its stride because it has an intermediate reduction stage (averaging
over non-display dimensions to produce a 2D grid) that can be enormous
*before* any canvas-resolution binning happens to it. Scatter has no such
intermediate — each sample maps directly to one `(x, y)` point, and
`Canvas.points()`'s binning *is* the reduction, not a second pass over an
already-reduced grid. Run directly against the lazy data, its output is
always exactly `canvas_width × canvas_height` by construction, regardless
of whether the selection matches ten rows or ten billion — the wire
payload is bounded automatically, no separate decimation/`is_decimated`
concept to design.

**Important clarification on data fidelity, worth preserving precisely:**
this fix does not exclude any matching points from the result. Every
sample that falls within the selection still contributes to whichever
pixel-bin it lands in — the bin's aggregate is genuine, not a subset.
What changes is *where* the binning happens (near the data, before the
network hop) versus *where* it happens today (client-side, in
`VisibilityScatter._shade_all_layers`, against `self._layer_dfs` cached
from a `query_columns()` call that already shipped every raw row across
whatever transport was in use). Explicitly **not** the same kind of
trade-off as raster's `max_cells` stride, which *is* real, visible
decimation with a defined recovery path (`is_decimated` + re-query at
higher resolution on zoom-in) — no analogous recovery path is needed here
because nothing is being dropped.

**This fix is also the enabler for genuine distributed cluster execution**
of the aggregation itself, named as a goal independent of the bandwidth
concern: a lazy Dask graph is exactly what a `dask.distributed` scheduler
can spread across multiple worker nodes, each partition read and reduced
in parallel, results combined into the one bounded output. Today's eager
`.compute()`-into-a-single-process's-pandas-DataFrames implementation is
not neutral with respect to that goal — it caps out at one process's
memory regardless of how many nodes sit behind it — so this fix is
required for that goal, not merely compatible with it. This is also
where Chunk 1c's "encode once" note (serialization section) may matter
most in practice, once a real payload shape exists to measure against.

### Open questions (unverified — resolve during implementation)

- `Canvas.points()` generally wants a Dask **DataFrame**, not an
  `xr.Dataset` directly — some conversion (e.g. `.to_dask_dataframe()`)
  is the likely missing glue between what the backend currently produces
  and what Datashader consumes lazily. Standard, well-supported territory
  in the Dask/xarray ecosystem in general, but **not verified against
  this codebase's actual partition/backend code**
  (`_iter_visibility_partitions`, `_apply_selection`, etc. in
  `msv2_backend.py`) — may not drop in cleanly.
- Whether/how this generalizes to `MSv4Backend` (not reviewed — this
  project's source set includes `msv4_backend.py` but it hasn't been
  read).
- Local recompositing for scatter (`_shade_all_layers` currently rebins
  from cached raw rows on every viewport change) — once the remote path
  returns a bounded aggregate instead of raw rows, does the *local*
  path's probe logic (`_agg_pixel`, which currently indexes into
  `self._layer_aggs`, themselves derived from raw-row rebinning) need any
  adjustment for consistency between local and remote sessions? Not
  analyzed.
- Which execution-context configuration `visplot`'s worker registration
  function should build (what gets `create_object`'d at context-creation
  time vs. lazily) — not worked through against Chunk 2's specific method
  set yet.

---

## Chunk 3 — `iclean` remote data path

**Status: designed, not yet implemented.** Lower near-term priority than
Chunk 2 per explicit direction; revisit once Chunk 2's pattern is proven
out. Committed to building on Chunk 1c's object framework from the start
— this chunk's `gclean` major/minor cycles are, in fact, the original
motivating case for Chunk 1b existing at all.

`iclean` already has a processing-isolation object, `gclean`
(`InteractiveCleanUI.__init__(self, gclean, user_args)`), used through a
narrow surface: `gclean.update(...)`, `gclean.__anext__()`,
`gclean.image_products()`, `gclean._log()`, `gclean.restore()`
(`_interactive_clean_ui.py`). A proxy standing in for a remote `gclean`
needs to honor **three different calling conventions on the same object**,
confirmed by grepping every call site:

- `next(gclean)` — plain sync, called from `initialize_tclean()` inside
  `_setup()` (line 1206) — **before** `_task_server`'s event loop exists.
  → `SyncBridge` (Chunk 1) — noting the cross-loop hazard Chunk 1b found
  and the currently-unwired `sync_bridge` on `RemoteAppLink` (see that
  chunk's section): this call site is the first concrete need for
  resolving that, not merely a hypothetical one.
- `gclean.update(dict(...))` — sync, returns `(err, errmsg)` directly
  (line 624). → `SyncBridge`, or possibly relax to async if the call site
  can be changed — not decided.
- `await gclean.__anext__()` — already async (line 628). → `request()`
  directly.
- `.image_products()`, `._log()`, `.restore()` — sync, called from
  `__init__` and elsewhere. → `SyncBridge`.

**With Chunk 1c:** `gclean` itself is created once, via `create_object`,
in an `iclean`-dedicated execution context — its own OS process,
isolated from anything else the same kernel might be running (a
`visplot` session, an ad hoc eval context) — and every one of the four
calling shapes above becomes a `call_method`/`request()` call against
that handle rather than a hand-rolled proxy. This is what keeps the
supervisor able to report status (per Chunk 1b's status vocabulary)
throughout a long major/minor-cycle run, instead of freezing for its
duration. `gclean` itself is otherwise unmodified.

Push traffic (convergence/progress updates) is a straight relay — the
worker sends a push, a handler registered on the supervisor's
`gclean`-context-facing comm relays it onward to `P_local`. **Confirmed
by hand (Chunk 1b's demo script) that this relay does not exist
generically and must be registered explicitly for whichever specific
push message type `gclean` actually sends** — see Chunk 1b's "Definition
of done" section. This chunk needs to register that relay for its own
convergence/progress message type(s); it is not something Chunk 1c's
generic infrastructure provides automatically, by design.

`_gen_port_fwd_cmd()`/`self._is_remote` (currently dead code — forwards
one port *per Comm category*, predating `CommMgr`'s multiplexing, and
calls `.address` on `Comm` objects that no longer have that attribute) is
a fossil of the pre-multiplexing architecture and should be **removed**,
not extended, when this chunk is implemented.

**Long-term note, not for this chunk specifically:** `iclean` is the app
that most benefits from the "start on-site, disconnect, reconnect from
home hours later" workflow — it's the concrete scenario the design doc's
§2f reconnection discussion is built around — since it has genuinely
long-running background work (major/minor cycles) that continues whether
or not anyone is watching. Once Chunk 3 exists, the `on_reconnect`
callback should push a fresh full-state snapshot (current cycle,
convergence state) rather than relying solely on queued-message replay to
catch a reconnecting client up after a multi-hour absence — the
convergence pipe should be opened with `squash_queue=True` regardless, so
a fallback to the queue holds only latest state, not a backlog.

### Open questions

- `gclean`'s actual source/interface hasn't been reviewed as part of this
  project's source set (only its call sites in `_interactive_clean_ui.py`)
  — needed before this chunk can be implemented in detail.
- `gclean.update(...)`'s calling convention (`SyncBridge` vs. relaxing the
  call site to async) — not decided; depends on how disruptive changing
  that specific call site would be, not yet assessed.
- How Chunk 1b's status vocabulary maps onto `gclean`'s own convergence/
  progress reporting concepts specifically (major cycle N of M, residual
  peak, etc.) — the generic died/working/stuck/completed vocabulary is a
  floor, not necessarily everything `iclean`'s UI wants to show.
- Resolving `RemoteAppLink.sync_bridge`'s currently-unwired state (Chunk
  1b) is a likely prerequisite for `next(gclean)`'s calling convention
  specifically, given that call site's construction-time, no-ambient-loop
  shape — flagged here as a probable blocker to check early in this
  chunk's implementation, not assumed resolved by then.
