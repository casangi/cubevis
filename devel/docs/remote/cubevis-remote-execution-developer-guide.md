# Using `cubevis.remote` for a distributed application — a developer's guide

This is not a design document and not a chunk-status record — those are
`cubevis-remote-execution-design.md` and
`cubevis-remote-execution-implementation.md` respectively, and this guide
assumes the reader has, or can get, both. This document is instead aimed
at someone who wants to *use* the `cubevis.remote` framework to build a
distributed application of their own — inside `cubevis` (Chunk 2, Chunk
3) or otherwise — and wants the practical advice that doesn't fit neatly
into either of those two documents: what the pieces are for, when to
reach for which one, and the mistakes that are easy to make the first
time.

If you haven't yet, run `examples/try_worker_process.py`,
`examples/try_remote_object.py`, and `examples/try_remote_eval.py`
against a local kernel (`--kernel-name python3`) before reading further.
Each is short, heavily commented, and demonstrates one layer of what
follows end to end. Reading this guide first and the examples never is
the wrong order.

---

## 1. The three tiers, and which one your code runs in

Every piece of code you write against this framework runs in exactly one
of three places, and knowing which one matters more than almost anything
else here:

- **`P_local`** — the process closest to the user (their laptop, or
  wherever the browser-facing side runs). This is the only tier that
  knows "remote" exists at all. It holds a `RemoteAppLink`, creates
  `ExecutionContext`s, and issues `call_method`/`eval_code`/`exec_code`
  calls. Async, driven by `asyncio` — every call from here is
  `await`-based unless you go through a `SyncBridge` (§6).
- **The supervisor kernel** — a real Jupyter kernel running on the
  cluster node, reached via `sshpyk`. You will rarely write application
  code that runs *here* directly; its job is to host the
  `ExecutionContextPool` (`_supervisor.py`) and proxy requests through to
  worker subprocesses. The one thing you do put here is a
  `register_function` — see §3 — because that function's import path
  must be resolvable in whatever environment the supervisor kernel itself
  runs in.
- **A worker subprocess** — a plain OS process, spawned by the supervisor,
  one per execution context. This is where your actual application
  objects live: an opened measurement set, a `gclean` instance, a
  Datashader canvas. From this object's own point of view, it is running
  completely locally — it does not know it's being driven remotely, and
  it should not need to. If you find yourself importing anything from
  `cubevis.remote` *inside* a class meant to run here, that's usually a
  sign the abstraction has leaked; the whole point of the object registry
  (§3) is that ordinary application classes stay ordinary.

A useful discipline when adding a new capability: before writing a line
of code, say out loud which of the three tiers it runs in. Most bugs in
this kind of system come from code that silently assumed it was running
somewhere it wasn't (see §8's cross-loop hazard for the sharpest example).

---

## 2. Picking the right primitive: four ways to reach a worker

`ExecutionContext` (what `link.create_context()` gives you) exposes four
different ways to get work done, and the right choice depends on two
independent questions: *is this a pre-built operation or ad hoc code?*
and *does it need to return quickly?*

| | fast / must return quickly | may run for a while |
|---|---|---|
| **structured (object + method)** | `ctx.call_method(handle, name, args, kwargs)` | wrap the same call in `dispatch_async` + `job_status` polling |
| **unstructured (a code string)** | `ctx.eval_code(expr)` | `ctx.exec_code(code)` (same caveat) |

In practice, two of these four cells cover almost everything:

- **`call_method`** is the default choice for anything that maps cleanly
  onto "invoke one method on one object" — smaller blast radius than a
  raw code string, no ambiguity about namespace, and it's what
  `create_object`/`ObjectRegistry` (§3) are built around. Prefer this over
  `eval_code`/`exec_code` whenever the operation *has* a natural
  method-call shape, even if reaching for `eval_code("obj.method(...)")`
  would technically work — `call_method` is what a future reader expects
  to see, and it's what the isolation and error-reporting conventions
  below assume you're using.
- **`eval_code`/`exec_code`** are the escape hatch for ad hoc composition,
  debugging, or genuinely dynamic code that doesn't have a fixed method
  name known in advance — `try_remote_eval.py`'s `--interactive` mode is
  the clearest example of where this is the right tool, not a shortcut
  around building a proper class.

Independently: **`dispatch_fast` (which `call_method`/`eval_code`/
`exec_code` all use underneath) blocks the caller until the worker
replies — fine for anything that answers in well under a second.**
Anything that might run for seconds or more should go through
`dispatch_async` instead, which returns a `job_id` immediately, with
`job_status(job_id)` polled afterward. The reason this matters is not
really about your own caller being blocked (an `await` blocking one
coroutine is often fine) — it's that the *supervisor's* receive loop
processes one worker-facing request at a time per context; a slow
`dispatch_fast` call queues up every other request against that same
worker behind it, including status polls for unrelated work. `gclean`'s
major/minor cycles are the canonical example this matters for: a caller
that used `dispatch_fast` for a whole cycle would make its own status
polling impossible until the cycle finished, defeating the entire point
of polling. When in doubt, if an operation's duration isn't bounded and
known-short, use `dispatch_async`.

---

## 3. The object registry: how to expose your own classes

An execution context starts out with nothing but the generic wire
handlers (`create_object`, `call_method`, `dispose_object`, `eval_code`,
`exec_code`) — no application classes registered. You give it some via
the `config` argument to `create_context()`:

```python
ctx = await link.create_context(
    config={"register_function": "your_package.remote_registrations:register"}
)
```

`register_function` is a dotted or colon-separated import path (both
forms work — see `worker_main.py`'s `_load_dotted`), resolved **inside
the worker subprocess**, which inherits the supervisor kernel's own
Python environment. That function receives the context's `ObjectRegistry`
and is expected to call `registry.register_class("SomeName", SomeClass)`
for each class you want constructible in that context. From there:

```python
handle = await ctx.create_object("SomeName", args=[...], kwargs={...})
result = await ctx.call_method(handle, "some_method", args=[...])
await ctx.dispose_object(handle)
```

A few things worth internalizing before writing your own registration
function:

- **Write the registration module so it's importable from a fresh
  interpreter with no other setup.** It's imported cold, inside a
  subprocess that only just started — don't rely on any state your own
  `P_local` process happens to have set up first.
- **One `ObjectRegistry` per execution context, and that's the isolation
  boundary — use it deliberately.** Two unrelated pieces of work (say, a
  `visplot` session and an ad hoc debugging context) that don't need to
  share memory should get *two* execution contexts, not one shared one
  with both sets of classes registered. This isn't just tidiness: two
  independent long-running calls in the same worker process can still
  block each other (one process, one GIL), so "give it its own context"
  is the actual mechanism for real isolation, not just a naming
  convention. See the design doc's §2f for why this needed a whole extra
  layer (execution contexts) rather than being solved by the object
  registry alone.
- **`call_method`'s return value is raw and unwrapped — including
  `None`.** A method that legitimately returns `None` and a call that
  failed both come back looking unremarkable unless you check for it
  explicitly. A failure is a reply dict with an `"error"` key (and a
  `"traceback"` string) — `CommMgr`'s own exception-to-error-reply
  conversion produces this automatically for anything your method raises,
  so you never need to catch exceptions yourself just to report them; you
  do need to check `"error" in reply` on the caller side if you need to
  tell a real failure apart from a genuine `None`. `dispatch_fast`
  directly (bypassing the `call_method` convenience wrapper) is the
  right way to inspect a reply for this, since the wrapper methods assume
  success and will raise an unrelated `KeyError` on an error-shaped reply
  instead of surfacing the real one.
- **Real payloads travel through the same serializer Bokeh already
  uses** (`cubevis.utils.serialize`/`deserialize`) — a numpy array, a
  plain dict, a scalar, all round-trip with no special handling on your
  part. This has been verified against a real 2D numpy array, not just
  scalars — see `test_object_registry_e2e.py`. Very large results are the
  one thing not yet handled specially (no chunking) — see §7.

---

## 4. Fast dispatch vs. background jobs, in practice

`dispatch_async`'s status vocabulary is deliberately small: `working`,
`stuck`, `completed`, `died`. Poll `job_status(job_id)` on whatever cadence
makes sense for your UI (0.5s in the demo scripts; there's nothing
special about that number). A few practical notes:

- Polling itself stays fast — milliseconds — even while the underlying
  worker call is genuinely still running, because the supervisor never
  has to choose between answering a status poll and waiting on the
  worker call itself. If you ever see a status poll taking as long as the
  work it's polling, something has regressed this property — it should
  not happen by design.
- `stuck` exists as a status distinct from `working` for operations that
  have a way to self-report unusual slowness; if your worker-side
  operation has no such self-reporting, don't expect `stuck` to appear
  spontaneously — it isn't a generic hang detector.
- Push messages (a worker proactively sending something to `P_local`
  without being asked, e.g. a progress update mid-cycle) are a
  **different mechanism** from `dispatch_async`/`job_status`, and they
  are not wired up generically — `CommMgr` has no wildcard/catch-all
  handler. If your application needs this (Chunk 3's `gclean` convergence
  updates are the concrete example), you must register a handler for that
  *specific* message type yourself, on the supervisor's worker-facing
  comm. This was found by trying to build it, not assumed — see the
  Chunk 1b/1c writeups for the exact demo that surfaced it.

---

## 5. Timeouts, and what real remote latency actually looks like

Every wire operation in this framework has a timeout, and getting the
*budget*, not just the *value*, right is the single most consequential
piece of advice in this document — it is also the one mistake that
already happened once in this project (see the implementation doc's
"Post-delivery finding" section for the full story) and is easy to
reintroduce in a new application built on top of this framework.

**The rule: any client-side timeout wrapping an operation that itself
waits on a nested server-side timeout must be strictly larger than that
server-side budget** — never equal, and not by an arbitrary small margin.
If a client waits exactly as long as the server's own internal budget for
the same round trip, the client can time out at the exact instant the
server is still doing legitimate, non-hung work, because the server's
internal wait is entirely *nested inside* the client's. This project's
own bug was exactly this: a client-side `create_context()` wait and a
server-side `_CONFIGURE_TIMEOUT` both hardcoded to 30 seconds. It never
showed up against a local kernel (subprocess spawn there takes well under
a second) and showed up immediately against a real cluster kernel (worker
subprocess spawn there took closer to 50 seconds). If you add your own
operation with a similar nested-wait shape, give the outer timeout
comfortable, explicit headroom over the inner one, and say so in a
comment — don't let them drift into equality by both starting from the
same "seems reasonable" default.

**What real remote latency actually looks like, from one measured run
against a live `sshpyk`-provisioned cluster kernel** (full breakdown in
the implementation doc's timing section) — roughly two and a half
minutes end to end, broken into: about 50 seconds for the remote host to
start a fresh Python process at all (conda activation, interpreter
startup, on a networked home filesystem); about another 50 seconds
waiting for that freshly-started remote kernel to become responsive
(during which `sshpyk`'s own SSH-based liveness polling runs, roughly
every 1.25 seconds); and about 50 more seconds for the actual worker
subprocess this framework spawns to come up and complete its opening
`configure` handshake. **None of this is specific to one unlucky run** —
a fresh interpreter starting on a cold, networked filesystem is a
structural cost, not a fluke, and any application built on this framework
should assume something in this range (tens of seconds to a couple of
minutes) for the *first* connection to a given remote host, with
everything after that (once channels are open, once a worker exists)
running at ordinary low-latency RPC speed. Size your own UI's "connecting…"
states, spinners, and any timeout of your own around this reality, not
around how fast things go against a local kernel in a test suite.

**Logging is off by default, on purpose, and that's a lever you have too.**
`sshpyk`/`jupyter_client` are `traitlets`-based and produce zero visible
output unless something has called `logging.basicConfig()` in your
process — this is not a bug, and it surprises everyone the first time.
If you're debugging a slow or hung remote connection, configure logging
at `DEBUG` (see any of this chunk's three demo scripts' `--log-level`
flag for the pattern); for routine runs, `INFO` gives you the useful
narrative (kernel launched, PID, shutdown) without the very high-volume
`DEBUG`-level detail (every SSH invocation, every liveness check). Also
worth knowing before it costs you a debugging session: a script's own
`print()` output is buffered when redirected to a file, while `logging`'s
output to `stderr` is not — the two can appear wildly out of chronological
order in a captured log even though nothing hung. `python -u` (or
`PYTHONUNBUFFERED=1`) fixes this if you need your own print statements to
interleave correctly with a debug log for timing analysis.

---

## 6. `SyncBridge`: when your call site has no `await` to give you

Almost everything in this framework assumes an `asyncio` event loop is
already running wherever you're calling from. Sometimes it isn't — a
constructor, a synchronous callback, a call site in existing
(non-`cubevis.remote`-aware) code that you don't want to make `async` —
and that's what `SyncBridge` is for: it runs its own dedicated event loop
on a separate thread and lets synchronous code hand it a coroutine to run
to completion.

Two things to get right:

- **Every `mgr`/transport a given `SyncBridge` drives must have been
  constructed on — and must only ever be driven from — the same asyncio
  loop.** `CommMgr`'s internal locks and futures are bound to the loop
  they were created on; calling into them from a different loop or thread
  doesn't raise an error, it **deadlocks silently**, because
  `asyncio.Lock` isn't thread-safe across loops. This was found the hard
  way once already (see the implementation doc's Chunk 1b writeup) and is
  exactly the kind of bug that looks like "the remote call just hangs
  forever" with no exception, no traceback, nothing — so if you ever see
  that specific symptom, check for a cross-loop `SyncBridge` misuse before
  anything else.
- **`RemoteAppLink.sync_bridge` exists but is not yet wired up to drive
  request calls from a genuinely no-ambient-loop call site.** If your
  application has a call site that runs *before* any event loop exists at
  all (Chunk 3's `gclean` is the concrete case this framework will need
  to solve first), that wiring is still a real, open design task — not
  something you can assume already works because the attribute exists.
  See the Chunk 3 handoff document for the specifics.

---

## 7. Known limits, stated plainly

Worth knowing up front rather than discovering mid-project:

- **No payload chunking.** A `call_method` result that's very large (a
  large image cube, an enormous DataFrame) currently travels as one
  message. This project's own Colab transport already has a chunking
  pattern for a different constraint (message-size limits in that
  environment) that could be adapted here if a real payload turns out to
  need it — nothing is built yet, and nothing should be built speculatively
  ahead of an actual payload shape that needs it.
- **No execution-context lifecycle/cleanup.** Nothing today notices or
  cleans up an execution context nobody ever reconnects to. If your
  application creates contexts that might legitimately be abandoned (a
  browser tab closed without a clean shutdown, say), that's currently
  your own application's problem to solve, not something this framework
  handles for you.
- **Reconnecting to a specific execution context assumes `P_local`'s own
  process stayed alive** (network dropped and came back — laptop sleep,
  a wifi blip, even an evening's absence — but the process holding the
  `execution_context_id` in memory never restarted). If your application
  needs `P_local` itself to restart from scratch and still find a
  specific pre-existing context, that's a harder problem this framework
  does not yet solve — `list_contexts()` exists for introspection, not as
  a "guess which one was mine" discovery mechanism.
- **The real `sshpyk`/SSH/cluster path is validated by real use, not by an
  automated test suite running against one.** Every automated test in
  this framework runs against a real local kernel (never mocked), which
  is a meaningfully different regime from a real cross-host SSH tunnel —
  see §5's timing numbers for exactly how different. Treat "passes the
  test suite" and "confirmed against a real cluster kernel" as two
  different, both-worth-having kinds of confidence, not one substituting
  for the other.

---

## 8. Testing philosophy, if you're extending this framework

This project's test suite deliberately never mocks the transport,
subprocess, or kernel layers — every test that claims to prove something
about real subprocess or real kernel behavior does so against an actual
subprocess or an actual local Jupyter kernel, not a stand-in. This is
slower (the full suite takes on the order of 30-40 seconds) and has
found real bugs a mocked equivalent would have hidden (the cross-loop
`SyncBridge` deadlock in §6, the client/server timeout equality bug in
§5 — neither would reproduce against a mock that always replies
instantly). If you add a new capability to this framework, hold the same
line: a test that exercises a real subprocess or a real local kernel is
worth more than a faster test that exercises a mock standing in for one,
even though it costs more wall-clock time to run. Where something
genuinely can't be exercised in a given sandbox (a real SSH/cluster
connection, so far), say so plainly in the test's own docstring and in
whatever document tracks status, rather than letting a mocked
stand-in quietly imply more confidence than actually exists.
