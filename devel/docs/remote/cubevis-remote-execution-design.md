# cubevis Remote Execution — Architecture

**Status:** Architecture settled through Chunk 1c. Chunk 1, Chunk 1b, and
Chunk 1c implemented and tested; Chunks 2 and 3 designed at the
architecture level, not yet implemented, and are committed to building on
Chunk 1c from the start rather than being retrofitted onto it later.

**Relationship to the implementation document:** this document describes
the *shape* of the system and why it's shaped that way — it should change
rarely, mostly when a genuinely new architectural decision gets made (as
Chunk 1b was, and Chunk 1c was). Chunk-by-chunk technical status,
code-level detail, line number references, open questions being worked
through, and what's been verified versus assumed all live in the
separate, actively-updated **`cubevis-remote-execution-implementation.md`**
instead. If you're looking for "what does Chunk 2 actually need to do to
`msv2_backend.py`," that's the other document; if you're looking for "why
does this system have three tiers instead of two," it's this one.

**Scope:** How `iclean` and `visplot` (both non-Bokeh-server `cubevis`
applications, using `CommMgr`/`Comm` over a multiplexed websocket) support
a user starting the app on a cluster node and viewing/interacting with it
from a laptop, possibly with a large intervening dataset (MSv2 up to MSv4
at terabyte scale), a slow or intermittent link, and — as of Chunk 1b —
long-running C++-backed work (`gclean`/`tclean` cycles) that must not
freeze the connection used to monitor it. As of Chunk 1c, the scope
widens deliberately: rather than one hand-written wire handler per
application-level operation, the system provides a general-purpose remote
execution and object-management framework — arbitrary Python evaluation
and object creation/method-invocation on a remote worker — that Chunk 2
and Chunk 3 are two *applications* of, not two independent designs.

---

## 1. Rejected approaches, and why

Two earlier directions were seriously considered and dropped for the
overall connectivity model. Recording the reasoning so they aren't
re-litigated from scratch:

**Tunneling `CommMgr`'s existing `WebSocketTransport` (SSH `-L`, or an
in-process `asyncssh`-managed tunnel).** Reuses `WebSocketTransport`/
`CommMgr` completely unmodified, which is attractive, but leaves real
problems unsolved: getting the initial static HTML from cluster node to
laptop (no notebook display channel to carry it), address
bind-vs-advertise splitting, and — once the "start on-site, reconnect from
home" requirement came up — a custom session-registry/discovery mechanism
to reinvent something Jupyter kernels already solve. Dropped in favor of
§2.

**A literal Jupyter-protocol frontend on the laptop** (laptop runs a real
Jupyter frontend — e.g. JupyterLab — connected via `sshpyk` to a remote
kernel, using Jupyter's own `display_data`/comm machinery to carry the
Bokeh HTML). Reuses `CommsTransport`'s existing `jupyter` transport path
almost for free, but requires the laptop side to actually be a running
notebook session, not "start a standalone app, get a plain browser tab" —
which is what was actually wanted. Superseded by §2.

**A second full Jupyter kernel as the compute-worker tier (§2c, considered
during Chunk 1b's design).** Recursively reusing Chunk 1's own
`KernelClientTransport`/`KernelCommTransport`/`ensure_remote_worker` for
the supervisor↔worker hop — the supervisor plays "P_local" toward a
second, locally-spawned kernel — was attractive because it costs nothing
new to write: every primitive already exists and is tested. Rejected as
needlessly heavy for what is, on the supervisor↔worker hop, a same-host,
supervisor-owned relationship with no need for Jupyter's own
connection-file/ZMQ/`kernel_info` protocol machinery (that machinery earns
its cost on the P_local↔supervisor hop specifically because it crosses a
network and needs independent reattachability — neither applies one hop
further in). See §2c below for what replaced it.

**Multiple Jupyter comm targets per kernel, one per worker (considered and
rejected during Chunk 1c's design).** When the need for more than one
compute worker per supervisor kernel became concrete (§2f), the first
instinct was to give each worker its own `target_name` — cheap to build,
since `ensure_remote_worker()` already takes one. Rejected, but **not**
for the reason an earlier version of this document gave (that a fixed
name is what lets a zero-prior-knowledge `P_local` process reconnect —
see the correction in §2e; reconnection turns out not to depend on a
fixed name at all). The real reason: one Jupyter comm target means one
`CommMgr`/`KernelClientTransport` pair per kernel, which is what lets
Chunk 1's own multiplexed request/response-and-push channel actually do
its job — sub-addressing "which logical destination" via ordinary
`Comm`/`comm_id` categories *within* that one channel, the mechanism it
already has. N comm targets means N independent `AsyncKernelClient`s
each running their own `get_iopub_msg()` polling loop against the same
kernel's iopub stream, each with its own `reconnect_timeout`/
`reconnect_grace_period`/`on_reconnect` bookkeeping to keep in sync —
real duplicated machinery and resource cost for a problem `CommMgr`'s
existing sub-multiplexing already solves for free. The correct place for
"more than one worker" is one level deeper: a pool of execution
contexts, addressed by an id handed back at creation time and *tracked
by* the one Jupyter-level worker, multiplexed through its single comm
rather than requiring their own top-level Jupyter comm each. See §2f —
this is now `_supervisor.py`'s `ExecutionContextPool`, implemented and
tested in Chunk 1c.

---

## 2. Chosen architecture

### 2a. Overall shape: three tiers

```
Browser  <--CommMgr/WebSocketTransport, unchanged-->  P_local
P_local  <--sshpyk/jupyter_client, Jupyter protocol-->  Supervisor kernel (cluster node)
Supervisor kernel  <--asyncio.subprocess, lightweight framing-->  Compute worker process(es) (same host)
```

- **Browser** — unchanged. Talks only to `localhost`, via `CommMgr`'s
  existing `WebSocketTransport`, exactly as today. No tunnel, no address
  rewriting, no mixed-content concerns.
- **`P_local`** — a local Python process running the existing `CommMgr`
  browser-facing side unchanged, plus a kernel-facing side (Chunk 1) that
  connects to a remote Jupyter kernel via `sshpyk`. `P_local`'s role
  shifts from *doing* the processing (today's local case) to *forwarding*
  requests to the remote kernel and relaying results back to the browser.
- **Supervisor kernel** (cluster node, reached via `sshpyk`, itself
  connecting over SSH through a jump host) — the remote Jupyter kernel
  process Chunk 1 talks to. Owns the app-level protocol handlers
  (`query_raster`, `gclean.update`, etc.) and, as of Chunk 1b, delegates
  any work that would risk holding the GIL for an extended period to a
  compute worker it spawns and supervises, rather than running that work
  inline. Stays continuously responsive to `P_local` regardless of what
  a worker is doing — this is the whole reason tier 2c (below) exists. As
  of Chunk 1c, the supervisor is also the single point that tracks *which*
  compute workers currently exist — see §2f.
- **Compute worker process(es)** (same host as the supervisor, spawned by
  it) — where the actual long-running, potentially GIL-holding work
  happens: opening the MS/Processing Set, `tclean`/`gclean` cycles,
  Datashader aggregation, or an arbitrary evaluated Python snippet. A
  genuinely separate OS process per execution context — separate memory,
  separate GIL — so nothing one does can block the supervisor's own event
  loop, or another worker's execution, no matter how long a single call
  takes or whether the C++ code underneath ever yields the GIL. From its
  own point of view each worker is a completely ordinary local session,
  using `LocalVisibilityReader`/`gclean` exactly as today, with no
  awareness that it's being driven remotely. **"Remote" is entirely a
  `P_local`-side concept; "delegated to a worker" is entirely a
  supervisor-side concept — neither a worker's own code nor the browser
  needs to know the other tiers exist.**

### 2b. Why this is better than tunneling the browser-facing socket

The hard "reach a machine across the internet" problem moves from the
browser↔backend layer (fussy: address rewriting, tunnels, mixed content)
down to a Python↔Python layer, where `sshpyk`/`jupyter_client` already own
kernel discovery and reconnection as mature, tested capabilities. Kernel
connection files, plus `sshpyk`'s own `persistent_file` (written on every
launch, kept on disk rather than deleted at shutdown when the kernel is
meant to survive `P_local` going away — see §2e), are the low-level
mechanism for "how do I find and reattach to a specific running kernel
process." **Correction to an earlier version of this document:** that
mechanism alone does not amount to "no custom `cubevis` session registry
is needed" — `sshpyk` will happily reattach to *a* kernel process given
its `persistent_file`, but nothing below `cubevis` remembers, across a
full restart of `P_local` itself, *which* `persistent_file` corresponds to
which of `cubevis`'s own sessions. That bookkeeping is `cubevis`'s to
keep, and Chunk 1c builds it (§2e). "Start on-site, leave, reconnect from
home" is still architecturally cheap — a small manifest lookup plus
`sshpyk`'s existing `existing=` reattachment — just not literally free.

### 2c. Why a third tier: the GIL problem

Some of the work the supervisor kernel needs to do on request —
specifically `gclean`/`tclean`'s C++-backed major/minor cycles — is
long-running and cannot be assumed to release the GIL. A Python `asyncio`
event loop and a blocking, GIL-holding C extension call share the same
GIL if they're in the same process; no amount of `asyncio` structure
changes that, since `asyncio` only ever governs *which Python code* the
loop hands the GIL to next; it can't make a currently-running C call give
it back early. A second Python *thread* in the same process doesn't
help either, for the same reason — CPython threads share one GIL. The
only thing that actually isolates one Python call from blocking another
is a separate OS **process**, since each process gets its own
interpreter and its own GIL.

Given that, the supervisor kernel's own handlers never run this work
inline — they delegate to a worker process (spawned via
`asyncio.create_subprocess_exec`, **not** `multiprocessing`'s default
`fork()`, which inherits the *entire* memory image of a multi-threaded
parent — including whatever some other thread's lock happened to be
holding at fork time — a well-documented hazard for processes like an
ipykernel that already run more than one thread; `exec()` discards that
inherited state by starting a genuinely fresh interpreter, which
`multiprocessing.get_context('spawn')` also does if `multiprocessing`'s
richer object-marshaling is ever preferred over hand-rolled framing).

The worker communicates back over a lightweight, `asyncio`-native pipe
protocol — not the Jupyter protocol (see §1's "rejected" note above) —
using the *same* `CommMgr`/mirrored-role/`request()` machinery Chunk 1
already built for the P_local↔supervisor hop, just with a new, cheaper
transport underneath it. The supervisor's own P_local-facing handlers, for
anything that needs a worker, become thin async proxies: call `request()`
against the target worker, return what comes back — or, for anything
long-running enough that the supervisor's own receive loop must not block
on it, schedule the call as a background job and return a job id
immediately instead (Chunk 1b's `dispatch_async`/`JobRegistry` — see the
implementation doc). No new dispatch logic is needed for either case —
`CommMgr._handle_request` already awaits a handler's result if it's
awaitable, so a proxy handler is exactly as simple as a handler that
computes the answer itself; the background-job case is one more
`asyncio.Task`, not a new primitive.

Because a worker is a separate process, the supervisor's own event loop
is never blocked by it, at any point — including while a single, fully
synchronous, no-GIL-release C++ call is in flight inside it. That is what
keeps the supervisor "lively": it can always answer `P_local` regardless
of what a worker is doing, even if that worker itself can't say much more
than "still there" during a single long call.

### 2d. Confirmed environment assumptions

(From conversation, not to be re-derived.) This is a reserved-node
system, not a job-submission HPC center — sessions are inherently
interactive, no scheduler/queue complexity to design around. SSH
connectivity to the cluster node is assumed to go through a jump host.

### 2e. Reconnection

Two genuinely separate mechanisms are stacked here, and keeping them
distinct is what makes the rest of this section (and §2f) coherent:

**Kernel-process-level reconnection** — whether the remote kernel
*process* is still running, and how to get `jupyter_client` reattached to
it if so. This is entirely `sshpyk`'s job, and it already has a real,
verified answer: every kernel launch writes a `persistent_file` (JSON:
connection info, remote PIDs, a `kernel_id` — see the implementation doc
for the exact fields, confirmed by reading `sshpyk`'s
`provisioning.py`/`get_persistent_info()` directly), which is *deleted* on
clean shutdown unless the provisioner's `persistent` flag is set — which
`cubevis` controls, and should set whenever a kernel is meant to survive
`P_local` going away. Reattaching is `AsyncKernelManager(kernel_name=...,
existing=<name or path>)`; `sshpyk` resolves the file, verifies the
remembered PIDs are still real processes on the remote host (not stale),
and hands back a working connection. Nothing in `cubevis` reimplements
any of this.

That mechanism has one gap `cubevis` has to fill itself, though: it
provides no way to *discover* which `persistent_file` — if any —
corresponds to a session `cubevis` cares about, and no automatic listing
of "what's currently dangling." `sshpyk` only resolves a name you already
know; it doesn't remember what that name meant to *you*. So `cubevis`
keeps a small manifest of its own — separate from and referencing
`sshpyk`'s file, not duplicating its contents — mapping a caller-chosen
label to the `sshpyk` `persistent_file`/kernel name last used for it. On
`P_local` startup, `cubevis` checks this manifest for anything
outstanding and surfaces it — reuse vs. shut down is the user's/app's
decision, not automatic (a kernel legitimately reserving cluster time
shouldn't be silently killed, nor silently left running forever by
default). This manifest — `KernelPersistenceManifest` — is implemented
and tested as of Chunk 1c; see the implementation doc for its exact shape
and write/read timing, and for what is and isn't verified about its
liveness-checking story in a sandbox with no real `sshpyk`/SSH access.

**`cubevis`-level reconnection** — once `P_local` is already talking to
the *same* kernel process (the above, already done), how does it find its
own app-level state inside it? This is `target_name`'s job — but it's
worth being precise about what problem `target_name` actually solves,
since an earlier version of this document conflated it with kernel-level
reconnection above and got the mechanism wrong as a result. `target_name`
is not a session identifier; it's the Jupyter Comm protocol's own
required routing field — "once connected to a kernel, which registered
service inside it do I want to open a channel to." It answers a real but
narrower question than "how do I get back into a specific session," and
does not need to be a fixed, globally-known string to do it.

Corrected mechanism, matching `ensure_remote_worker`'s actual behavior:
`target_name`, when not explicitly supplied, is generated fresh — from
the newly-constructed `CommMgr`'s own `comm_mgr_id` — on whichever call
first bootstraps a given kernel. Every *later* call against that same
kernel (a reattaching `P_local` session, or the same session's own retry)
finds the namespace marker already present and returns the **existing**
`comm_mgr_id` unconditionally, regardless of what `target_name` argument
that later call happened to pass — `build_worker` never runs a second
time, and the value it originally generated is what comes back. The
practical consequence: a reattaching `P_local` process does not need to
know the original `target_name`/`comm_mgr_id` *before* reconnecting. It
re-runs the same (idempotent) bootstrap cell it would run on a first
connection, and the cell's own output hands back the identifier to use —
exactly the mechanism Chunk 1 already demonstrated (`build_count`
staying at `1` across a reattach; the same test, extended slightly, shows
`comm_mgr_id` staying stable across it too). No fixed constant, and no
manifest entry for this specific identifier, is needed at all — the
manifest (above) only needs to get `P_local` as far as "reattached to the
right kernel process"; rediscovering *this* kernel's worker is then a
property of the bootstrap mechanism itself, not something requiring its
own persisted record. As of Chunk 1c, what gets rediscovered this way is
the execution-context *pool*, not a single worker — see §2f.

There is no meaningful collision risk this way either, which a
previously-considered fixed well-known constant genuinely did carry (two
independent `cubevis` deployments — different versions, different
installs — bootstrapping into the same kernel would have silently shared
one `ensure_remote_worker` marker under a fixed name): a freshly generated
id, by construction, doesn't collide with anything.

**Reconnecting to a specific execution context** (§2f) is a related but
distinct question the mechanism above does not, by itself, answer — see
§2f for why a generated, `P_local`-remembered id (the same shape as
`target_name`/`comm_mgr_id` itself, one layer deeper, not a second
persisted record) is the right mechanism, and why it's sufficient for the
actual scenarios this system needs to support (a `P_local` process, and
its in-memory state, surviving network interruptions ranging from a
brief wifi drop to an evening's commute — not surviving `P_local`'s own
process restarting from scratch).

`CommMgr` already has `reconnect_timeout` (default `None` = wait
indefinitely), `reconnect_grace_period`, in-flight request resend on
reconnect, and an `on_reconnect` callback — all built originally for
laptop-sleep/browser-reload scenarios, and directly reusable on the
P_local↔supervisor hop without modification. One thing worth being
deliberate about: an unbounded `reconnect_timeout` is fine on a personal
workstation but holds a reserved node's walltime open on this system's
shared/reserved allocations — a policy value to set consciously once this
runs on cluster time, not a design gap.

The supervisor↔worker hop still does not need `CommMgr`'s own
reconnection machinery (`reconnect_timeout` etc.) — a worker process is a
child of the *supervisor kernel process*, not of `P_local`, and the
supervisor kernel process already survives `P_local` disconnecting (the
paragraph above). A worker subprocess surviving a `P_local` network blip
is therefore a free consequence of process parentage, not something that
needs its own reattach protocol. What Chunk 1b's original text got too
narrow was concluding from this that there is only ever *one* worker per
kernel, full stop — that conflated "the inner hop doesn't need its own
independent reconnect mechanism" (still true) with "there can only be one
execution context" (not implied by it, and not what's wanted — see §2f).

### 2f. The execution-context and object model (Chunk 1c)

Three deliberately recursive layers, each solving the same shape of
problem — "given an id, find the right thing" — at a different scope:

```
target_name/comm_mgr_id (generated on first bootstrap, rediscovered via
                          idempotent re-bootstrap -- see §2e)
  --> the ONE Jupyter comm per supervisor kernel
      --> execution_context_id (generated, P_local-remembered)
          --> a specific compute worker subprocess, from a pool
              --> object_id (generated, scoped to one execution context)
                  --> a specific object living in that worker's memory
```

- **Layer 1 — the kernel-level worker.** Unchanged from Chunk 1/1b: one
  `CommMgr`/`KernelCommTransport` per supervisor kernel. `target_name`
  (and the `comm_mgr_id` it's generated from) is not a fixed constant —
  see §2e for why it doesn't need to be, and how a reattaching `P_local`
  process finds its way back to the same one without having memorized it
  in advance.
- **Layer 2 — execution contexts.** A pool of compute worker subprocesses,
  *owned and tracked by* the Layer-1 worker (part of what
  `ensure_remote_worker`'s `build_worker(mgr)` constructs and returns —
  opaque to Chunk 1's bootstrap mechanism, which only needs *something*
  kept alive as "the worker"). Created on demand via a wire operation
  (`create_context`, implemented in Chunk 1c — see the implementation doc
  for its exact schema), which spawns a fresh `WorkerProcessTransport`
  -backed subprocess and returns a generated `execution_context_id`. Not
  a second Jupyter comm target — see §1 for why that was rejected.
  Configuration for what a given context should have available (which
  classes it can construct, what pre-built objects it starts with)
  travels as part of *that* creation request's payload rather than as
  command-line arguments to the spawned process, since different
  contexts under the same kernel may legitimately want different
  configurations, and a request payload composes with that far better
  than argv does.
- **Layer 3 — objects within a context.** Within one worker subprocess, a
  registry mapping caller-supplied names (registered once, at worker
  configuration time, by whichever application — `visplot`, `iclean` —
  is using this framework) to actual classes, plus a handle table mapping
  a generated `object_id` to a live instance. `create_object` constructs
  one (by registered class name plus constructor args) and returns a
  handle; `call_method` invokes a method on an already-created object by
  handle; `eval_code`/`exec_code` is the unstructured escape hatch — an
  arbitrary string, evaluated against that context's own namespace, for
  ad hoc composition and debugging.

**Why isolation requires Layer 2, not just Layer 3.** A single worker
process is a single Python namespace and a single GIL. Two independent
uses of raw `eval`/`exec` — or two unrelated long-running operations —
in the *same* process can still interfere with each other (name
collisions, one call blocking the other) even though each individually
satisfies Chunk 1b's original "one worker never blocks the supervisor"
property. Genuine isolation between two unrelated pieces of work
requires two separate OS processes, i.e., two Layer-2 execution
contexts — which is also directly why `visplot`'s Datashader
raster/scatter work and `iclean`'s `gclean` object are expected to run in
*different* execution contexts even when driven from the same supervisor
kernel, not because they need different `target_name`s, but because they
want independent processes. Confirmed by test, not just by construction,
in Chunk 1c — two execution contexts configured with disjoint class
registrations, each shown to lack access to the other's classes.

**Why eval/exec is in scope at all.** A Jupyter kernel is already
arbitrary Python code execution — that's the mechanism `sshpyk` and
`ensure_remote_worker`'s own bootstrap already rely on. Layering a
constrained eval/exec capability on top of a worker process that's
already reachable only through an authenticated SSH+kernel connection
does not introduce a new category of exposure; it reuses the trust
boundary that already exists. `call_method` (structured: target object,
method name, args/kwargs) is preferred by default for anything that maps
cleanly onto "invoke one method" — smaller blast radius than a raw
string, no ambiguity about which namespace it runs in — with raw
`eval_code`/`exec_code` reserved for composition that doesn't fit that
shape.

**Reconnecting to a specific execution context.** The scenarios this
needs to support (confirmed, not assumed): a `P_local` process starts a
`gclean` execution context, begins interactive cleaning, kicks off a long
batch of iterations, and the user's laptop goes home for the evening —
`P_local`'s own process stays alive throughout, only its network
connection to the supervisor drops and later reconnects (laptop sleep is
architecturally the same case as a brief wifi interruption, just longer).
In neither case has `P_local` actually forgotten its `execution_context_id`
— it was sitting in `P_local`'s own memory the whole time, since the
*process* never restarted. That means the simple design is the sufficient
one: a server-generated id, held by `P_local` for the life of its own
process, addressed directly on reconnect. No caller-supplied label, and
no "list what's alive and guess which one is mine" discovery path, is
needed for these scenarios (Chunk 1c's `list_contexts` exists for
introspection/debugging, not as this discovery mechanism). If a future
scenario genuinely requires `P_local`'s own process to restart from
scratch and still find a specific pre-existing execution context, that
needs its own design pass (a label alongside the generated id, and
today's `list_contexts` query reused as the discovery step) — flagged
here as explicitly out of scope for what's been built, not silently
assumed away.

**Lifecycle.** An execution context nobody ever reconnects to needs
eventual cleanup, or abandoned worker subprocesses accumulate on the
remote host indefinitely. Not solved as of Chunk 1c — carried forward as
a real, related open item in the implementation document rather than
left implicit.

---

## 3. Chunk breakdown

Each chunk below is a short summary of *what it's responsible for* and
*why it's sequenced where it is*. Technical detail, status, and open
questions live in the implementation document.

- **Chunk 1 — Shared wire-protocol layer.** Gives `P_local`↔supervisor a
  multiplexed request/response and push channel with `CommMgr`'s existing
  reliability properties. Foundational; every other chunk builds on it.
  **Status: implemented and tested.**

- **Chunk 1b — Compute worker process infrastructure.** Gives the
  supervisor kernel a way to delegate long-running, potentially
  GIL-holding work to a separate process it spawns and supervises,
  without ever blocking its own responsiveness to `P_local` — see §2c.
  Built on Chunk 1's `CommMgr`/`TransportBase`/role-mirroring.
  **Status: implemented and tested** — real-subprocess round trips
  (request/response and push), stderr relay, the died/working/stuck/
  completed status vocabulary with a non-blocking background-dispatch
  primitive, and `RemoteAppLink`/`BokehAppContext` integration are all
  built and verified. Its original API shape (one `RemoteAppLink` per
  caller-chosen `target_name`) predated the corrected one-worker-per-kernel
  understanding in §2e/§2f and needed restructuring around the execution-
  context pool — that restructuring is Chunk 1c, now done.

- **Chunk 1c — Remote execution and object framework.** Gives the
  supervisor's one worker a pool of execution contexts (§2f) instead of a
  single 1:1 worker process, plus the object-creation/method-invocation
  and eval/exec capabilities that make the pool useful, plus the
  `cubevis`-level kernel-persistence manifest (§2e). Deliberately
  independent of both `visplot` and `iclean` — a general-purpose remote
  execution framework, not something either application's design demands
  in an application-specific shape. Blocks Chunk 2 and Chunk 3, both of
  which are expected to be built as applications of this framework rather
  than hand-rolled wire protocols of their own. **Status: implemented and
  tested** — real subprocess/real local-kernel testing throughout (39
  tests passing); the actual `sshpyk`-provisioned, cross-host path
  remains unverified in this sandbox (no SSH/cluster access), same
  limitation as Chunk 1/1b's own real-kernel tests — see the
  implementation doc's "What is and isn't verified" section.

- **Chunk 2 (`visplot` remote data path) — 2a raster, 2b scatter.** Gives
  `visplot` a `RemoteReductionContext` satisfying its existing
  `VisibilityReader`/`ReductionContext` interfaces, dispatching through
  Chunk 1c's `create_object`/`call_method` against objects living in a
  dedicated execution context. Near-term priority: `visplot` is under
  active development and remote execution is valuable to its own
  developers now, unlike `iclean` (already released, lower near-term
  appetite for change). **Status: designed, not yet implemented.**

- **Chunk 3 (`iclean`/`gclean` remote data path).** Gives `iclean` a
  proxy for its existing `gclean` processing-isolation object, honoring
  its three different calling conventions, with `gclean` itself created
  and driven through Chunk 1c's object protocol in its own dedicated
  execution context — this is the case that originally motivated Chunk
  1b's existence. Lower near-term priority than Chunk 2 per explicit
  direction; revisit once Chunk 2's pattern is proven out. **Status:
  designed, not yet implemented.**

---

## 4. Suggested chunk ordering for implementation chats

1. **Chunk 1** (shared wire layer) — done.
2. **Chunk 1b** (compute worker process infrastructure) — done.
3. **Chunk 1c** (remote execution and object framework) — done.
4. **Chunk 2a** (raster) — smallest real change on top of 1c (routing
   through `create_object`/`call_method`; contract already correct); good
   validation of the whole stack before tackling the harder scatter case.
   Do next.
5. **Chunk 2b** (scatter) — the genuinely new design work (lazy
   `Canvas.points()` pipeline); do after 2a proves the wire+worker+object
   layers work end-to-end.
6. **Chunk 3** (`iclean`/`gclean`) — revisit once the pattern from Chunk 2
   is proven; lower near-term priority per explicit direction, but no
   longer blocked on anything since Chunk 1c is done.

Each implementation chat should start from this document and the
implementation document, rather than from prior chat history. Two more
documents exist alongside these: `cubevis-remote-execution-developer-guide.md`
(general-audience usage advice for anyone building on this framework —
not chunk-specific) and dedicated handoff documents for Chunk 2
(`cubevis-remote-execution-chunk2-handoff.md`) and Chunk 3
(`cubevis-remote-execution-chunk3-handoff.md`), each self-contained
enough to start an implementation chat from directly rather than from
this document's own brief chunk-breakdown summary above.
