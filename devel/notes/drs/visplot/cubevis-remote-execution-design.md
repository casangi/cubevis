# cubevis Remote Execution — Design Reference

**Status:** Design settled at the architecture level; several implementation
details flagged as open and unverified (see each chunk's "Open questions").
Written to hand off to focused, per-chunk implementation chats — each chunk
below is meant to be workable mostly on its own, given this document as
context.

**Scope:** How `iclean` and `visplot` (both non-Bokeh-server `cubevis`
applications, using `CommMgr`/`Comm` over a multiplexed websocket) support a
user starting the app on a cluster node and viewing/interacting with it from
a laptop, possibly with a large intervening dataset (MSv2 up to MSv4 at
terabyte scale) and a slow or intermittent link.

---

## 1. Rejected approaches, and why

Two earlier directions were seriously considered and dropped. Recording the
reasoning so they aren't re-litigated from scratch:

**Tunneling `CommMgr`'s existing `WebSocketTransport` (SSH `-L`, or an
in-process `asyncssh`-managed tunnel).** Reuses `WebSocketTransport`/`CommMgr`
completely unmodified, which is attractive, but leaves real problems
unsolved: getting the initial static HTML from cluster node to laptop (no
notebook display channel to carry it), address bind-vs-advertise splitting,
and — once the "start on-site, reconnect from home" requirement came up — a
custom session-registry/discovery mechanism to reinvent something
Jupyter kernels already solve. Dropped in favor of §2.

**A literal Jupyter-protocol frontend on the laptop** (laptop runs a real
Jupyter frontend — e.g. JupyterLab — connected via `sshpyk` to a remote
kernel, using Jupyter's own `display_data`/comm machinery to carry the
Bokeh HTML). Reuses `CommsTransport`'s existing `jupyter` transport path
almost for free, but requires the laptop side to actually be a running
notebook session, not "start a standalone app, get a plain browser tab" —
which is what was actually wanted. Superseded by §2, which uses `sshpyk`
for a different purpose (see below).

---

## 2. Chosen architecture

**Two processes on the laptop, one kernel on the cluster node:**

- **Browser** — unchanged. Talks only to `localhost`, via `CommMgr`'s
  existing `WebSocketTransport`, exactly as today. No tunnel, no address
  rewriting, no mixed-content concerns.
- **`P_local`** — a local Python process running the existing `CommMgr`
  browser-facing side unchanged, plus a *new* kernel-facing side that
  connects to a remote Jupyter kernel via `sshpyk`. `P_local`'s role shifts
  from *doing* the processing (today's local case) to *forwarding* requests
  to the remote kernel and relaying results back to the browser.
- **Remote kernel** (cluster node, reached via `sshpyk`, itself connecting
  over SSH through a jump host — confirmed as the assumed connectivity
  model) — does the actual work: opens the MS/Processing Set, runs
  `tclean`/`gclean` cycles, runs Datashader aggregation. From its own point
  of view it is a completely ordinary local session — it uses
  `LocalVisibilityReader`/`gclean` exactly as today, with no awareness that
  it's being driven remotely. **"Remote" is entirely a `P_local`-side
  concept.**

**Why this is better than tunneling the browser-facing socket:** the hard
"reach a machine across the internet" problem moves from the browser↔backend
layer (fussy: address rewriting, tunnels, mixed content) down to a
Python↔Python layer, where `sshpyk`/`jupyter_client` already own kernel
discovery and reconnection as mature, tested capabilities. Kernel
connection files are already the "how do I find and reattach to a specific
running session" mechanism — no custom `cubevis` session registry is
needed. "Start on-site, leave, reconnect from home" reduces to: start a
fresh `P_local`, hand it the same kernel's connection info, reattach.

**Confirmed environment assumptions** (from conversation, not to be
re-derived): this is a reserved-node system, not a job-submission HPC
center — sessions are inherently interactive, no scheduler/queue
complexity to design around. SSH connectivity to the cluster node is
assumed to go through a jump host.

**Reconnection is mostly already solved on the browser-facing side.**
`CommMgr` already has `reconnect_timeout` (default `None` = wait
indefinitely), `reconnect_grace_period`, in-flight request resend on
reconnect, and an `on_reconnect` callback — all built originally for
laptop-sleep/browser-reload scenarios, and directly reusable here without
modification. One thing worth being deliberate about: an unbounded
`reconnect_timeout` is fine on a personal workstation but holds a reserved
node's walltime open on this system's shared/reserved allocations — a
policy value to set consciously once this runs on cluster time, not a
design gap.

---

## 3. Chunk 1 — Shared wire-protocol layer (foundational; blocks Chunks 2 and 3)

**Goal:** give `P_local`↔remote-kernel a multiplexed, request/response- and
push-capable channel with the same reliability properties `CommMgr` already
has on the browser leg — without duplicating that machinery.

**Multiplexing discipline:** exactly **one** Jupyter comm per kernel,
carrying every message category (control, per-query traffic, progress
pushes, whatever else) multiplexed by `comm_id`/`message_id` inside it —
mirroring how the browser leg already collapsed from many websockets down
to one. `sshpyk` itself tunnels the kernel's five fixed ZMQ sockets
(shell/iopub/stdin/control/heartbeat) once, per kernel — that's a
protocol-level given, orthogonal to this app-level multiplexing decision,
not something to add more of.

**Explicit anti-pattern, already in the codebase as a fossil:**
`_interactive_clean_ui.py`'s `_gen_port_fwd_cmd()` (currently dead —
`self._is_remote` is hardcoded `False`) forwards one port *per Comm
category* (`self._pipe['control'].address[1]`, one more per cube's
image/control pipe, etc.) and calls `.address` on `Comm` objects that no
longer have that attribute. This predates `CommMgr`'s multiplexing and must
not be revived or used as a reference — it's exactly the model this design
replaces.

**Reusing `CommMgr` on the kernel leg needs one real fix.** `Comm.send()`
and `CommMgr.send()`/`_send_immediate()` hardcode the outgoing
`'direction': 'p2j'` tag; `_route_message()` dispatches purely on that
literal string (`'p2j'` in → treated as a response to *our* pending
request; `'j2p'` in → treated as a fresh request needing a handler); and
`_handle_request()`'s auto-reply hardcodes `'j2p'` on the way back out.
This is fine for the browser leg (genuinely asymmetric: Python always
originates `p2j`, JS always originates `j2p`), but breaks if two
unmodified `CommMgr` instances talk to each other — both are Python, both
tag outgoing messages `p2j`, so an unsolicited push from the kernel side
(e.g. a progress update) arrives at `P_local` tagged `p2j`, is treated as
"a response to something we asked for," finds no matching `request_id`,
and is silently dropped.

*Fix:* parameterize the direction/role tag in those four locations
(`_comm_mgr.py`), defaulting to today's literal strings so the browser leg
is untouched. Run the kernel-side `CommMgr` in the default role (identical
to how `_build_comm()` is written today for any local app); run
`P_local`'s kernel-facing `CommMgr` in the mirror role.

*Considered and rejected:* a hand-written mirror class instead of touching
`_comm_mgr.py`, to avoid changing working code. Rejected because it would
need to reimplement `squash_queue`, in-flight resend on reconnect, and
reconnect-generation bookkeeping to be equally safe for the
away-for-hours-then-back scenario this whole design exists to support — a
second, independently maintained copy of that logic is a likely source of
drift. The one-parameter, backward-compatible change was preferred.

**Two calling-convention primitives, needed by both Chunk 2 and Chunk 3:**

```python
async def request(comm, message_id, payload):
    """Async request/response over a Comm — wraps Comm.send()'s existing
    callback mechanism in a Future. Use from any call site that already
    has a running event loop (e.g. a j2p handler)."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    def _on_reply(msg):
        if not fut.done():
            fut.set_result(msg)
    await comm.send(message_id, payload, callback=_on_reply)
    return await fut
```

For call sites with **no running loop** (construction-time calls — see
`gclean`'s `next(gclean)` in `_setup()`, and `VisibilityPlotter`'s
`_build_panels()` at `visibility_plotter.py:1176`, which runs immediately
after `_build_comm()` at line 1175, before `_task_server`'s loop exists):
run the kernel-facing `CommMgr`/transport on its own dedicated background
thread with its own event loop, started at proxy-construction time, and
block via `asyncio.run_coroutine_threadsafe(coro, bg_loop).result()`. This
mirrors existing prior art in this codebase — `Context`'s `Mode.THREAD`
(`_context.py`) already uses the identical "separate thread does the real
work, caller blocks on a `concurrent.futures.Future`" shape — rather than
inventing a new pattern.

Push traffic (progress/convergence updates) needs neither primitive — it's
a straight relay: a handler registered on `P_local`'s kernel-facing comm
fires, and its body just calls `.send()` on the corresponding
browser-facing comm to forward the same payload onward.

**Open questions (unverified — resolve during implementation):**

- The exact `jupyter_client`/`sshpyk` API calls for the frontend-role
  transport (`KernelClientTransport`'s `connect()`/`send_message()`/`run()`
  against a `BlockingKernelClient` or async client) were asserted at a
  conceptual level only, not checked against the real API surface.
- **Start vs. reattach.** First launch must bootstrap the worker
  (construct `gclean`/open the MS, start the comm); reattachment must not
  re-run that and blow away live state. Needs an idempotent check on the
  kernel side (a namespace marker, or an `execute_request` safe to run
  repeatedly that reports "already running, here's the comm target" if
  so) — not designed yet, and worth checking what `sshpyk` already exposes
  for kernel discovery before inventing something.
- Whether to keep the kernel-side multiplexer as a literal `CommMgr`
  (fastest path, but drags in `bokeh.model.Model`/`init_scripts` machinery
  that's meaningless with no browser attached) vs. factoring the
  request/response/queueing core into a lighter shared base class both
  legs build on. Leaning toward reusing `CommMgr` as-is for a first
  version, revisiting only if the Bokeh dependency turns out to matter —
  not decided.

---

## 4. Chunk 2 — `visplot` remote data path (near-term priority)

Prioritized over Chunk 3 per explicit direction: `iclean` is already
released (lower near-term appetite for change); `visplot` is still in
active development and remote execution is valuable to its own developers
now.

**visplot already has an isolation boundary — it just wasn't recognized as
filling that role.** `VisibilityPlot` never touches the MS directly; it
goes through `self._backend`, typed as `VisibilityReader`
(`visibility_plot.py:85,266`). `reduction_context.py`'s own docstring
(lines 27–46) already names the intended remote implementation —
`RemoteReductionContext` — satisfying **both** `ReductionContext` and
`VisibilityReader` at once. Nothing about `VisibilityPlot`/
`VisibilityPlotter` needs restructuring; they already treat the reader as
swappable.

`VisibilityReader` (`visibility_reader.py`) is a `@runtime_checkable
Protocol` with exactly four methods: `query_raster`, `query_columns`,
`probe_raster_pixel`, `probe_scatter_pixel`. Two more methods are needed
beyond the formal protocol, evidenced by `LocalVisibilityReader`
(`local_visibility_reader.py`): `metadata()` (used by `open_ms`/`open_ps`
to build `ObservationMetadata`) and `axis_info()` (used for axis
labeling — its own comment warns a missing implementation silently
produces "correct-looking output, wrong label"). `RemoteReductionContext`
must implement both, not just the four protocol methods.

**Method-to-primitive mapping**, based on where each is called from:
`query_raster`/`query_columns`/`probe_*` are called from j2p handlers with
a running loop already active → plain `async def` + `request()`.
`metadata()`/`axis_info()` are called at construction time, before
`_task_server`'s loop exists (same shape as `next(gclean)`) → the
sync-bridge from Chunk 1. `ReductionContext.submit()` already returns a
`Future` by contract (`reduction_context.py:679`) — maps onto
`run_coroutine_threadsafe` almost exactly as designed; `commit_flags`,
`bandpass`, etc. become one-line `request()` calls against the
already-wire-shaped `ReductionOperation`/`ReductionResult` DTOs.

### 4a. Raster — no wire-contract change needed

**`query_raster()` already correctly bounds itself against an
arbitrarily large MSv4, and this was verified in the actual implementation,
not assumed:**

- `MSv2Backend._raster_2d` (`msv2_backend.py:1233-1310`) builds the
  quantity array via dask-array primitives (`da.absolute`, `da.angle`) and
  reduces over non-display dimensions with `.mean(...)` on that
  dask-backed array — which stays **lazy**.
- `_decimate_agg` strides that still-lazy graph *before* any
  materialization.
- The single `.compute()` call (`msv2_backend.py:1134`,
  `partitions_2d.append(arr.compute())`) happens **after** striding — so
  Dask only ever reads the strided cells from disk, regardless of the true
  size of the underlying MS/Processing Set.
- Output is capped at `max_cells` (default 2,000,000 cells, "≈16 MB at
  float64" per the docstring at `reader.py:738`) — a fixed bound
  independent of dataset size.

So `RemoteReductionContext.query_raster()` can be close to a mechanical
relay of the existing contract. The only tuning worth doing is passing a
smaller `max_cells` by default when dispatching remotely than the local
2M default, trading resolution for bandwidth using a parameter the
interface already exposes for exactly this purpose.

**Local recompositing must stay local — do not route it through the wire.**
`VisibilityRaster._shade_viewport()` (`visibility_raster.py:1380-1409`)
operates purely on the cached `self._agg` — zero backend calls. Pan/zoom
within the cached agg's resolution, color-mode toggles
(`_handle_set_color_mode_raster`), and scaling changes
(`_handle_update_scaling_raster`) all resolve to this same free, local
recomposite today. An earlier version of this design proposed shipping a
fully-rendered image over the wire on every render call — **rejected**:
it would turn every currently-free pan/zoom drag into a network round
trip. The wire boundary is `query_raster()` itself (already correct, see
above), not the per-viewport render call.

### 4b. Scatter — the real gap, and the fix is about correctness, not just bandwidth

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

**Important clarification on data fidelity (raised and resolved in
conversation, worth preserving precisely):** this fix does not exclude any
matching points from the result. Every sample that falls within the
selection still contributes to whichever pixel-bin it lands in — the bin's
aggregate is genuine, not a subset. What changes is *where* the binning
happens (near the data, before the network hop) versus *where* it happens
today (client-side, in `VisibilityScatter._shade_all_layers`, against
`self._layer_dfs` cached from a `query_columns()` call that already
shipped every raw row across whatever transport was in use). This is
explicitly **not** the same kind of trade-off as raster's `max_cells`
stride, which *is* real, visible decimation with a defined recovery path
(`is_decimated` + re-query at higher resolution on zoom-in) — no analogous
recovery path is needed here because nothing is being dropped.

**This fix is also the enabler for genuine distributed cluster execution**
of the aggregation itself, which was named as a goal independent of the
bandwidth concern: a lazy Dask graph is exactly what a `dask.distributed`
scheduler can spread across multiple worker nodes, each partition read and
reduced in parallel, results combined into the one bounded output. Today's
eager `.compute()`-into-a-single-process's-pandas-DataFrames implementation
is not neutral with respect to that goal — it caps out at one process's
memory regardless of how many nodes sit behind it, so this fix is required
for that goal, not merely compatible with it.

**Open questions (unverified — resolve during implementation):**

- `Canvas.points()` generally wants a Dask **DataFrame**, not an `xr.Dataset`
  directly — some conversion (e.g. `.to_dask_dataframe()`) is the likely
  missing glue between what the backend currently produces and what
  Datashader consumes lazily. Standard, well-supported territory in the
  Dask/xarray ecosystem in general, but **not verified against this
  codebase's actual partition/backend code** (`_iter_visibility_partitions`,
  `_apply_selection`, etc. in `msv2_backend.py`) — may not drop in
  cleanly.
- Whether/how this generalizes to `MSv4Backend` (not reviewed in this
  conversation — `msv4_backend.py` exists in the project but wasn't read).
- Local recompositing for scatter (`_shade_all_layers` currently rebins
  from cached raw rows on every viewport change) — once the remote path
  returns a bounded aggregate instead of raw rows, does the *local* path's
  probe logic (`_agg_pixel`, which currently indexes into
  `self._layer_aggs`, themselves derived from raw-row rebinning) need any
  adjustment for consistency between local and remote sessions? Not
  analyzed.

---

## 5. Chunk 3 — `iclean` remote data path (lower near-term priority; revisit once `visplot`'s pattern is proven out)

`iclean` already has a processing-isolation object, `gclean`
(`InteractiveCleanUI.__init__(self, gclean, user_args)`), used through a
narrow surface: `gclean.update(...)`, `gclean.__anext__()`,
`gclean.image_products()`, `gclean._log()`, `gclean.restore()`
(`_interactive_clean_ui.py`). A proxy standing in for a remote `gclean`
needs to honor **three different calling conventions on the same object**,
confirmed by grepping every call site:

- `next(gclean)` — plain sync, called from `initialize_tclean()` inside
  `_setup()` (line 1206) — **before** `_task_server`'s event loop exists.
  → sync-bridge (Chunk 1).
- `gclean.update(dict(...))` — sync, returns `(err, errmsg)` directly
  (line 624). → sync-bridge, or possibly relax to async if the call site
  can be changed — not decided.
- `await gclean.__anext__()` — already async (line 628). → `request()`
  directly.
- `.image_products()`, `._log()`, `.restore()` — sync, called from
  `__init__` and elsewhere. → sync-bridge.

Push traffic (convergence/progress updates) is a straight relay, same
pattern as Chunk 2 — no new primitive needed.

`_gen_port_fwd_cmd()`/`self._is_remote` (currently dead code, see §3) is a
fossil of the pre-multiplexing architecture and should be **removed**, not
extended, when this chunk is implemented.

**Long-term note, not for this chunk specifically:** `iclean` is the app
that most benefits from the "start on-site, disconnect, reconnect from
home hours later" workflow, since it has genuinely long-running background
work (major/minor cycles) that continues whether or not anyone is
watching. Once Chunk 3 exists, the `on_reconnect` callback should push a
fresh full-state snapshot (current cycle, convergence state) rather than
relying solely on queued-message replay to catch a reconnecting client up
after a multi-hour absence — the convergence pipe should be opened with
`squash_queue=True` regardless, so a fallback to the queue holds only
latest state, not a backlog.

---

## 6. Suggested chunk ordering for implementation chats

1. **Chunk 1** (shared wire layer) — blocks everything else; do first.
2. **Chunk 2a** (raster) — smallest real change (parameter tuning only,
   contract already correct); good validation of Chunk 1 before tackling
   the harder scatter case.
3. **Chunk 2b** (scatter) — the genuinely new design work (lazy
   `Canvas.points()` pipeline); do after 2a proves the wire layer works
   end-to-end.
4. **Chunk 3** (`iclean`/`gclean`) — revisit once the pattern from Chunk 2
   is proven; lower near-term priority per explicit direction.

Each implementation chat should start from this document rather than this
conversation's history.
