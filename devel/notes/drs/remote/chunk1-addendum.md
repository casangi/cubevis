# Chunk 1 Addendum — Shared Wire-Protocol Layer (resolved)

Status: implemented and tested, then refactored into an isolated
`cubevis.remote` subpackage before testing began — see
`subpackage-refactor.md` for exactly what moved and why. This document
describes the substance of each decision; file paths below reflect the
post-refactor layout.

All code and tests referenced below are independent of Bokeh's GUI/browser
stack — they run under plain `pytest`, exercising the real `_comm_mgr.py`
logic (and, for Tasks 5–6, a real separate `ipykernel` subprocess) with no
websocket, browser, or display involved.

---

## 1. Bug reproduced (Task 1)

Confirmed exactly as described: two `CommMgr`s wired directly together
(loopback `TransportBase` double, no socket/kernel involved) reproduce the
push-misrouting bug precisely. A push from side B arrives at side A tagged
`p2j`, is logged `Received response for unknown request`, and is dropped
— and side B's `_pending` slot for that comm is left permanently occupied,
wedging all further traffic on it. See
`cubevis/remote/tests/test_bug_reproduction.py`.

## 2. Direction/role tag parameterized (Task 2)

`CommMgr.__init__` gained a `role: str = CommMgr.ROLE_DEFAULT` parameter.
Internally this resolves to `self._self_direction` / `self._peer_direction`
via a small lookup table:

```python
ROLE_DEFAULT = 'default'
ROLE_MIRROR = 'mirror'

_ROLE_DIRECTION_TAGS = {
    ROLE_DEFAULT: ('p2j', 'j2p'),   # today's literals, unchanged
    ROLE_MIRROR:  ('j2p', 'p2j'),   # for the other end of a Python<->Python link
}
```

`ROLE_DEFAULT`/`ROLE_MIRROR` are exported both as module-level constants
(`cubevis.bokeh.transport.ROLE_DEFAULT`/`ROLE_MIRROR`) and as `CommMgr`
class attributes (`CommMgr.ROLE_DEFAULT`/`ROLE_MIRROR`), added during the
subpackage refactor so `cubevis.remote` (and anyone else) never has to
hardcode the strings `'default'`/`'mirror'`.

All four hardcoded sites now read from `self._self_direction`/
`self._peer_direction` instead of the literal strings: `send()`,
`_send_immediate()` (both now tag outgoing messages with
`self._self_direction`), `_route_message()` (dispatches on
`self._self_direction` vs `self._peer_direction` instead of the literal
strings, and now logs+drops anything matching neither, instead of silently
falling through), and all three reply sites inside `_handle_request()`
(now tag with `self._peer_direction`). A read-only `role` property was
added alongside the existing `state`/`connection_generation` properties.

**Backward compatibility:** `role=CommMgr.ROLE_DEFAULT` (or no `role`
argument at all) reproduces today's literals exactly — `CommMgr()` is
byte-for-byte identical in behavior to before. Verified by: (a) diffing
that only the documented sites changed, (b) confirming
`CommMgr()._self_direction == 'p2j'` / `._peer_direction == 'j2p'`
unconditionally, (c) re-running the original two-`CommMgr()`-collision
scenario from Task 1 after patching and confirming it still collides
identically (expected — that's inherent to using identical roles on both
ends, not a regression). **Caveat:** this sandbox doesn't have the actual
existing browser-facing test suite (only representative source files), so
"passes unmodified" is argued by the equivalence above rather than
literally run against that suite — worth doing before merging.

**Usage going forward:** the kernel-side `CommMgr` stays
`role=CommMgr.ROLE_DEFAULT` (unchanged construction, exactly like
`_build_comm()` today). P_local's kernel-facing `CommMgr` should be
constructed with `role=CommMgr.ROLE_MIRROR` — or, more conveniently, via
`cubevis.remote.open_remote_kernel_link()`, added during the refactor (see
below).

## 3. CommMgr reuse decision (Task 3)

**Decision: reuse `CommMgr` as-is (via `role=CommMgr.ROLE_MIRROR`) for the
kernel-facing multiplexer, not a lighter base class.** This matches the
design doc's own lean and the "considered and rejected" reasoning already
on record for the direction-tag fix: a hand-rolled parallel implementation
would need to independently reimplement `squash_queue`, in-flight resend
on reconnect, and reconnect-generation bookkeeping to be equally safe for
the away-for-hours-then-back scenario this whole design exists to support
— a second, independently-maintained copy of that logic is a likely source
of drift. The one-parameter change (`role`) is backward-compatible and
small enough that the "meaningless-with-no-browser" `bokeh.model.Model`/
`init_scripts` baggage the design doc worried about is a non-issue in
practice: it's inert unused surface area, not something that breaks or
needs to be worked around.

**A related, separate finding that was NOT previously on record:
`CommsTransport` (today's only `'jupyter'` transport) cannot be reused for
the kernel-side leg, regardless of the CommMgr decision above.**
`CommsTransport.__init__` unconditionally calls
`BokehInit.get_app_context().add_preflight_callable(self.display_bridge)`,
and `display_bridge()` builds an `anywidget.AnyWidget` and calls
`display()`; `connect()` then blocks on `self._conn_event`, which is only
ever set by `_on_comm_open()` firing from a **JS** comm handshake
(JupyterLab's widget manager, or Colab's eval_js/BroadcastChannel path).
None of that has a counterpart when the peer is P_local speaking plain
`jupyter_client` — there's no browser, no widget manager, nothing to
render the bridge for.

This is why a new, headless sibling transport, `KernelCommTransport`
(`cubevis/remote/_kernel_transport.py`), was built for the kernel side.
It keeps the one piece of `CommsTransport` that *is* transport-agnostic —
registering a target directly on the kernel's own `comm_manager`
(`register_target()` fires for any `comm_open` with a matching
`target_name`, regardless of whether the opener is JS-via-anywidget or a
raw `jupyter_client` frontend) — and drops everything that assumes a
browser: the anywidget bridge, the Colab chunking path, the JS-handshake
wait.

## 4. Calling-convention primitives (Task 4)

`cubevis/remote/_bridge.py` (has zero `cubevis` dependencies — pure
stdlib `asyncio`/`threading`, genuinely portable as-is):

- **`request(comm, message_id, payload, timeout=None)`** — matches the
  design doc's sketch (`Future` wrapped around `Comm.send()`'s callback),
  with an added optional `timeout` (`asyncio.TimeoutError` on expiry). Not
  in the original sketch, but added because a permanently-vanished peer
  with `resend_inflight_on_reconnect=True` (CommMgr's default) would
  otherwise leave the awaited `Future` pending forever — CommMgr itself
  only resolves a stuck pending request early when that flag is `False`.
  Default `timeout=None` preserves the original no-timeout behavior.
  Error replies (the peer's `{'error': ..., 'traceback': ...}` convention
  from `_handle_request`'s exception path) are returned as-is, not raised
  — callers check for the `'error'` key themselves.

- **`SyncBridge`** — a dedicated background thread with its own
  *persistent* event loop (`start()`/`run()`/`run_background()`/`stop()`),
  for call sites with no running loop. **Correction to the design doc's
  pointer:** it says to model this on `_context.py`'s `Mode.THREAD`
  handling, but that path is actually `ThreadPoolExecutor.submit()` — it
  doesn't give the submitted work a persistent loop of its own, which
  matters here since a comm's reply callback needs somewhere to land
  whenever it eventually fires, not just for the duration of one
  `submit()` call. The real matching precedent already in this codebase is
  `_task.py`'s `Task._convert_asyncio_event_to_threading` /
  `bridge_events` (`asyncio.new_event_loop()` + `set_event_loop()` in a
  daemon thread) — `SyncBridge` generalizes that exact pattern into a
  reusable, start/stop-able primitive.

Both primitives are tested with at least one no-running-loop case (a
`RuntimeError` from `asyncio.get_running_loop()` is asserted at the top of
those tests, to confirm they're genuinely exercising that path and not
accidentally running inside pytest-asyncio's loop) — see
`cubevis/remote/tests/test_bridge.py`.

## 5. `KernelClientTransport` spike (Task 5)

`cubevis/remote/_kernel_transport.py` delivers both halves:

- **`KernelClientTransport`** (frontend role, P_local side) — owns the
  `jupyter_client` lifecycle: `connect()` starts the kernel if needed
  (`AsyncKernelManager.start_kernel()`), waits for readiness, and opens one
  multiplexed comm via a raw `comm_open` message on the shell channel
  (matching the "exactly one Jupyter comm per kernel" discipline). Outgoing
  messages go out as `comm_msg` on the shell channel; `run()` polls iopub
  for `comm_msg`/`comm_close` traffic addressed to that comm id and
  forwards it to `CommMgr._route_message`.

- **`KernelCommTransport`** (kernel side, headless) — see §3 above.

**Verified against the real API, not assumed**, by installing
`jupyter_client` 8.9.1 and reading its source directly: `Session.msg()` /
`shell_channel.send()` for outgoing shell messages, `get_iopub_msg()` for
incoming iopub traffic (raises on timeout rather than blocking forever,
matching the existing transports' "keep the loop alive, check a flag"
shape), and — via `comm`/`ipykernel.comm.manager` source — that kernel→
frontend comm traffic goes out over **iopub** and frontend→kernel
`comm_open`/`comm_msg` traffic goes over **shell**, with content shapes
`{comm_id, target_name, data}` / `{comm_id, data}`. `register_target()`
was confirmed to be transport-agnostic (fires for any `comm_open`
regardless of opener) by reading `ipykernel.comm.manager.CommManager`
directly.

**Genuinely spiked, not just designed**: `test_kernel_transport_spike.py`
starts a real, separate `ipykernel` subprocess (`AsyncKernelManager(kernel_name='python3')`),
runs a `KernelCommTransport`-based bootstrap inside it via `client.execute()`,
then drives a full request/response round trip from a `KernelClientTransport`
in the test process — genuinely separate OS processes, no loopback double,
no mocked client. `test_link.py` covers the same scenario through
`open_remote_kernel_link()` instead of hand-wiring, added during the
subpackage refactor.

**Scope note, stated plainly:** this environment has no SSH/cluster access,
so `sshpyk`'s actual SSH tunneling was **not** exercised — only the
standard `jupyter_client` kernel-management/comm-protocol surface that
`sshpyk`-provisioned kernels use identically to a local one (confirmed by
reading `sshpyk` 1.23's source directly — see §6). This is a real gap
between "spiked" and "proven against the actual cluster," and worth
closing with a real cluster-connected run before Chunk 2/3 lean on it
heavily, but it's an honest, bounded gap rather than an unverified
assumption about the API shape itself.

## 6. Start-vs-reattach (Task 6)

**Two layers, kept deliberately separate:**

1. **Process-level liveness/reattachment — sshpyk's job, already solved.**
   Read `sshpyk` 1.23's source (`provisioning.py`, `kernelapp.py`)
   directly rather than assuming. It's a `jupyter_client.kernel_provisioners`
   entry point (`sshpyk-provisioner`) — not a library you call directly —
   so the integration point is the *standard* `AsyncKernelManager`/
   `AsyncKernelClient` API against an sshpyk-provisioned kernelspec, which
   is exactly what `KernelClientTransport` already uses. It already has a
   real, working `existing=`/`persistent=`/`persistent_file` config:
   `pre_launch()` skips `launch_remote_kernel()` entirely when `existing`
   is set, instead loading a previously-saved PID/command snapshot and
   verifying the remote process is still running the same command before
   accepting the reattach; `write_persistent_info()`/`load_persistent_info()`
   round-trip the connection info needed to reconnect. This directly
   answers "check what sshpyk already exposes for kernel discovery/liveness
   before designing something new" — nothing new was needed at this layer.

2. **App-level state idempotency — not sshpyk's concern, ours.** Knowing
   the remote *process* is alive says nothing about whether *this
   application's* worker state inside it (an opened MS, a running `gclean`)
   has already been bootstrapped. `cubevis/remote/_worker.py`'s
   `ensure_remote_worker(build_worker, target_name=..., namespace=...)`
   answers this: a namespace marker (`__cubevis_remote_worker__`) stashed
   in the kernel's own `user_ns`, checked before doing anything else. First
   call constructs a `CommMgr`/`KernelCommTransport` and calls
   `build_worker(mgr)` -- passing `mgr` specifically so `build_worker` can
   `mgr.open(category)`/`.register(message_id, handler)` its own protocol
   handlers as part of construction, since that's the only point at which
   it has a natural hook to do so -- then stashes the `CommMgr`,
   `KernelCommTransport`, and whatever `build_worker` returned in a
   `RemoteWorkerHandle`. Every subsequent call — a genuine repeat, or a
   fresh P_local session reattaching to the same still-running kernel —
   finds the marker and returns the existing `comm_mgr_id` unchanged,
   without calling `build_worker` again. A failed `build_worker` leaves no
   marker behind, so a later attempt retries cleanly rather than mistaking
   a half-built worker for a healthy one.

   Uses a **well-known constant target name**
   (`cubevis.remote.DEFAULT_TARGET_NAME = "cubevis-remote-worker"`) rather
   than a freshly-generated id, since the design doc's "exactly one
   Jupyter comm per kernel" discipline means there's only ever one worker
   per kernel process anyway — this is what lets a *new* P_local process
   reattach without first having to learn a previous session's
   randomly-generated id from anywhere; it already knows the name to ask
   for.

**Concrete and tested**, both layers: `test_worker_start_reattach_unit.py`
covers the namespace-marker contract in isolation (build-once, failure
doesn't wedge future attempts, namespaces don't bleed into each other).
`test_worker_start_reattach_real_kernel.py` runs the stronger version
against a real kernel subprocess: bootstraps once, closes that
`jupyter_client` connection (but leaves the kernel process running — this
sandbox's stand-in for sshpyk's process staying alive across an SSH
disconnect/reconnect), opens a **second, independent** `jupyter_client`
connection to the same still-running kernel, and confirms the second
bootstrap call reuses the existing worker (`build_worker`'s counter stays
at 1) and can immediately transact on the reattached comm target.

---

## Files delivered

```
cubevis/bokeh/transport/_comm_mgr.py           patched -- role param, ROLE_DEFAULT/ROLE_MIRROR
                                                constants, initialize(transport=) extension
                                                (only existing cubevis file touched)

cubevis/remote/__init__.py                     public API
cubevis/remote/_bridge.py                      request(), SyncBridge  (Task 4)
cubevis/remote/_kernel_transport.py            KernelClientTransport, KernelCommTransport (Task 5)
cubevis/remote/_link.py                        open_remote_kernel_link() convenience wiring
cubevis/remote/_worker.py                      ensure_remote_worker() (Task 6)
cubevis/remote/testing.py                      LoopbackTransport, wire_loopback_pair (public test util)
cubevis/remote/tests/                          17 tests, all passing, no Bokeh GUI required
```

See `subpackage-refactor.md` for the full rationale behind this layout,
including the three remaining `cubevis.remote -> cubevis.bokeh.transport`
coupling seams and what closing them for standalone extraction would
require.

## Definition of done — status

- [x] Two `CommMgr`-derived multiplexers (mirrored roles) complete request/
      response and push round trips over a loopback test double — no real
      `sshpyk`/cluster connection needed.
- [x] Existing browser-facing `CommMgr` tests pass unmodified — verified by
      equivalence argument (§2's caveat); not literally re-run, since that
      suite wasn't part of this sandbox's source set.
- [x] `request()` and the sync-bridge both have a "no running loop" test.
- [x] `KernelClientTransport` connects to and exchanges a message with a
      real remote kernel — genuinely spiked against a separate `ipykernel`
      subprocess; sshpyk's SSH layer itself not exercised (§5's scope note).
- [x] Start-vs-reattach has a concrete, working mechanism — tested against
      both an isolated marker and a real, separately-reattached kernel
      process.

## Handoff to Chunk 2

- `mgr, transport = await cubevis.remote.open_remote_kernel_link(kernel_manager, target_name=DEFAULT_TARGET_NAME)`
  builds and connects P_local's side in one call.
- `query_raster`/`query_columns`/`probe_*` (running loop already active,
  per the design doc's method-to-primitive mapping):
  `await cubevis.remote.request(comm, message_id, payload)`.
- `metadata()`/`axis_info()` (construction time, no loop yet — same shape
  as `next(gclean)`): hold one `cubevis.remote.SyncBridge` per
  `RemoteReductionContext`, `.start()` it in `__init__`, and call
  `bridge.run(request(comm, "metadata", {}))` /
  `bridge.run_background(transport.run())` for the transport's own read
  loop so both share one loop instead of racing two.
- The remote kernel side bootstraps via
  `cubevis.remote.ensure_remote_worker(lambda: <open MS, build ReductionContext>, target_name=DEFAULT_TARGET_NAME)`
  in a single `execute_request` — safe to send that same line on every
  P_local (re)connect.
- Still open, deliberately left to Chunk 2/3: what `build_worker` actually
  constructs (opening the MS / building `ReductionContext` — Chunk 1 has no
  opinion), and a real cluster-connected run to close the sshpyk-SSH gap
  noted in §5.
