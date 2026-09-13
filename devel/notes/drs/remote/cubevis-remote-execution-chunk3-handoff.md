# Chunk 3 handoff — `gclean` remote data path (`tclean`/`deconvolve` CASA6 tasks as a remote object)

**Start here, not from prior chat history.** Read alongside
`cubevis-remote-execution-design.md` (§2c for why a separate OS process
is required at all here — this is the chunk that originally motivated
that requirement — and §2f for the execution-context/object model) and
`cubevis-remote-execution-implementation.md` (Chunk 1c's exact wire
schemas). `cubevis-remote-execution-developer-guide.md` covers general
usage patterns; this document is `gclean`/`iclean`-specific detail on
top of it, and assumes the reader has both.

**Status: designed at the call-site level, not yet implemented, and
genuinely less far along than Chunk 2.** Unlike Chunk 2, where the
target interfaces (`VisibilityReader`, `ReductionContext`) have been read
directly, **`gclean`'s own source has not been part of this project's
reviewed material** — only its call sites, in `_interactive_clean_ui.py`.
Treat everything below that depends on `gclean`'s internal behavior as a
plan to validate against real source, not a confirmed design. Lower
near-term priority than Chunk 2 per explicit direction; revisit once
Chunk 2's pattern is proven out in practice — the object-registry/
execution-context pattern this chunk uses is the same one, so a working
Chunk 2 is real evidence this chunk's mechanics will also work, even
though `gclean`'s specific surface is different from `visplot`'s.

---

## 1. What exists today, and the one prerequisite that blocks this chunk before anything else

`iclean` already has a processing-isolation object, `gclean`
(`InteractiveCleanUI.__init__(self, gclean, user_args)`), used through a
narrow surface in `_interactive_clean_ui.py`:

- `next(gclean)` — plain sync, called from `initialize_tclean()` inside
  `_setup()` (line 1206) — **before `_task_server`'s event loop
  exists.**
- `gclean.update(dict(...))` — sync, returns `(err, errmsg)` directly
  (line 624).
- `await gclean.__anext__()` — already async (line 628).
- `.image_products()`, `._log()`, `.restore()` — sync, called from
  `__init__` and elsewhere.

Three different calling conventions on one object, confirmed by grepping
every call site — not a simplification for this document's sake. A
remote proxy for `gclean` has to honor all three.

**The blocking prerequisite:** `next(gclean)`'s call site runs *before
any event loop exists at all* — this is exactly the shape
`RemoteAppLink.sync_bridge` was left unwired for (see the design/
implementation docs' Chunk 1b sections, and the developer guide's §6).
`SyncBridge` itself works and is tested; what's missing is driving a
`RemoteAppLink`-backed `request()` call from a genuinely no-ambient-loop
construction-time call site. **Resolve this first, before writing any
`gclean`-specific proxy code** — it's a real, first-time design task
(either route such calls through `asyncio.run_coroutine_threadsafe`
against the link's own ambient loop, or construct the whole link from
inside `bridge.run()` so everything shares one loop from the start; both
options are named, neither is chosen, in the implementation doc's Chunk
1b writeup), and every other piece of this chunk assumes it's solved.

**The other prerequisite, non-blocking but do it before designing method
signatures:** read `gclean`'s actual source. Everything about `tclean`'s
return dictionary shape, what `deconvolve` needs as arguments, how the
residual image cube is actually produced and stored, and what
`.update()`/`.image_products()`/`.restore()` actually do internally is
currently known only from the outside (call sites, not implementation).
The plan below is written to be robust to some uncertainty here, but it
is not a substitute for reading the real code.

## 2. The object model: `gclean` as a Chunk 1c remote object

`gclean` gets created once, via `create_object`, in an
`iclean`-dedicated execution context — its own OS process, isolated from
anything else the same supervisor kernel might be running (a `visplot`
session, an ad hoc eval context). This is not incidental: `gclean`'s
`tclean`/`deconvolve` major/minor cycles are exactly the long-running,
potentially non-GIL-releasing C++-backed work that motivated requiring a
separate process at all (design doc §2c) — this chunk is that
requirement's original concrete case, not a hypothetical one.

Map each calling convention onto Chunk 1c's primitives:

- **`next(gclean)`** → once §1's `SyncBridge`/ambient-loop prerequisite
  is resolved, a `SyncBridge.run(ctx.call_method(handle, "__next__"))` or
  equivalent. This is the one call site that cannot proceed at all until
  that prerequisite is done — don't attempt to design around it with a
  workaround specific to this call site; solve the general problem once.
- **`gclean.update(dict(...))`** → `SyncBridge`-wrapped `call_method`,
  matching today's sync return of `(err, errmsg)` — or relax the call
  site to `async` if that turns out to be an acceptable, small change to
  `_interactive_clean_ui.py`. Not decided; whichever is less disruptive
  to the existing call site should win, and that's a judgment call to
  make once the call site is actually in front of you, not from this
  document alone.
- **`await gclean.__anext__()`** → a direct `request()`/`call_method`
  call — already async, no bridge needed, the simplest of the four.
- **`.image_products()` / `._log()` / `.restore()`** → `SyncBridge`-
  wrapped `call_method`, same pattern as `.update()`.

`gclean` itself is otherwise unmodified — every one of these becomes a
proxy call against the handle Chunk 1c's `create_object` returned, not a
rewrite of `gclean`'s own logic.

## 3. New in this handoff: `tclean`/`deconvolve` as the actual work `gclean` drives, and getting their outputs back

This section captures what's new since the design doc's original Chunk 3
sketch: the specific requirement that `gclean`, running remotely, is
what actually invokes CASA6's `tclean` and `deconvolve` tasks, and that
two specific things need to come back to `P_local`: **`tclean`'s own
return dictionary**, and **individual planes from the residual image
cube `tclean` is producing as it iterates.** Both are real payload-shape
questions this framework hasn't had to answer yet — Chunk 2's raster/
scatter payloads are numeric arrays and dicts of scalars; these are
similar in kind but larger, and one of them (the residual cube) is
explicitly *not* meant to travel across the wire in full.

**The `tclean` return dictionary.** CASA6's `tclean` task, when it
returns a summary (rather than `None`), gives back a plain dict of
mostly scalars and small arrays — convergence history, cycle counts,
per-channel/per-stokes summary statistics, and similar. This is a
schema-free bag of values, closer in shape to `eval_code`'s return
convention than to a schema you'd want to hand-declare in this
framework — the developer guide's §3 already establishes that a real
dict of scalars round-trips through this framework's existing
serializer with no special handling needed. **Treat the return
dictionary as a normal `call_method` return value** — the method that
wraps a `tclean`/`deconvolve` invocation (on `gclean`, or on whatever
object `gclean` itself delegates to internally — unconfirmed until its
source is read, per §1) simply returns that dict, and it travels through
Chunk 1c's existing wire path unchanged. **Verify one concrete thing
before assuming this is entirely free:** whether the dict CASA6 actually
returns contains anything that isn't already a plain Python/numpy value
this project's serializer handles today (a CASA-specific type, a
`taskinit`-managed object reference, or similar) — if so, that's a real
new serialization case to design, not a free pass; check this against
real `tclean` output before writing the wrapping method, not after.

**Residual image cube planes — this is the one genuinely new design
problem in this chunk, and it should be modeled on Chunk 2's raster
pattern, not invented from scratch.** The residual cube itself must
**stay on the remote side, in the worker's own memory or on the remote
filesystem** — exactly the same reasoning Chunk 2 already worked through
for a large MSv4: shipping the whole cube over the wire on every update,
or even once, is both a bandwidth problem and (for a cube at realistic
imaging resolution and channel count) potentially a memory-safety
problem on the `P_local` side too. What should cross the wire is **one
bounded, requested plane at a time** — a single channel/Stokes slice, a
2D numpy array — fetched via a `call_method` shaped like Chunk 2's
`query_raster()`: the caller names which plane it wants (channel index,
Stokes index, and — worth designing in from the start rather than
retrofitting — an optional downsampling/stride parameter mirroring
raster's own `max_cells`, since a single residual plane can itself be
large at full imaging resolution), and gets back exactly that plane,
nothing more. A method like `get_residual_plane(chan, stokes, max_cells=...)`
returning one 2D array is the shape to design toward; the exact name and
signature should follow from what `gclean`'s real source actually
exposes for accessing intermediate residual state (§1 — this is
precisely the kind of detail that can't be finalized without reading it).

**How planes reach `P_local` during an active clean, not just on
request, is a genuinely open design question — two shapes are plausible
and the choice should be made deliberately, not defaulted into:**

1. **Poll-style, matching this framework's existing `job_status` pattern**
   — `P_local` requests a specific plane whenever its UI needs to
   redraw (e.g., after each major cycle it's been told, via a status
   poll or a push, has completed). Simple, reuses existing mechanisms
   entirely, no new push-message type needed for the image data itself
   (though one is still needed for progress/convergence — see below).
2. **Push-style** — the worker proactively sends a plane (or a
   downsampled preview of one) as each major cycle completes, without
   `P_local` having to ask. Better for a live "watch it converge" UI
   experience, but requires registering a new push-message handler on
   the supervisor's `gclean`-context-facing comm (see below), and needs
   its own decimation discipline (don't push a full-resolution plane on
   every single minor-cycle iteration if the UI can't consume them that
   fast — this is a real instance of the framework's existing "no
   payload chunking yet" limitation, worth resolving here rather than
   discovering it under load).

Recommend starting with (1) for the first working version — it needs no
new push-handler plumbing and directly reuses Chunk 1c's existing
primitives — and treating (2) as a follow-on enhancement once the basic
remote `gclean` proxy is proven, rather than building both at once.

## 4. Progress/convergence push traffic (distinct from the residual-plane question above)

Push traffic for convergence/progress updates (major cycle N of M,
residual peak, etc.) is a straight relay: the worker sends a push, a
handler registered on the supervisor's `gclean`-context-facing comm
relays it onward to `P_local`. **Confirmed by hand, not assumed, in
Chunk 1b's own demo work: this relay does not exist generically and must
be registered explicitly for whichever specific push message type
`gclean` actually sends** — Chunk 1c's generic infrastructure does not
provide this automatically, by design (a wildcard/catch-all handler
would defeat the point of `CommMgr`'s explicit routing). This chunk
needs to register that relay for its own message type(s).

How Chunk 1b's generic status vocabulary (`working`/`stuck`/`completed`/
`died`) maps onto `gclean`'s own convergence/progress concepts is not
decided — that vocabulary is a floor, not necessarily everything
`iclean`'s UI wants to show (major cycle number, residual peak value, and
similar `gclean`-specific concepts likely need their own push message
shape layered on top, not a replacement for the generic vocabulary).

## 5. Cleanup while you're in this code

`_gen_port_fwd_cmd()`/`self._is_remote`, wherever they currently live in
`iclean`'s code, are dead code — a fossil of the pre-`CommMgr`-
multiplexing architecture: they forward one port *per Comm category*
(obsolete once multiplexing existed) and call `.address` on `Comm`
objects that no longer have that attribute. **Remove, don't extend,
when implementing this chunk** — there's no reason to carry this forward
into the remote-`gclean` design.

## 6. Long-term note for whoever builds `on_reconnect` handling here (not blocking, but worth knowing up front)

`iclean` is the application that most benefits from the "start on-site,
disconnect, reconnect from home hours later" workflow the design doc's
§2f is built around, precisely because `gclean`'s major/minor cycles
keep running whether or not anyone's watching. Once this chunk exists,
the `on_reconnect` callback should push a fresh full-state snapshot
(current cycle, convergence state, and probably a fresh residual plane
at whatever the UI's current channel/stokes selection is) rather than
relying solely on queued-message replay to catch a reconnecting client
up after a multi-hour absence — replaying a multi-hour backlog of
minor-cycle push messages is neither useful nor bounded. The convergence
push channel should be opened with `squash_queue=True` regardless, so a
fallback to the queue holds only latest state, not a backlog. Not
required for a first working version, but design the push-message
handling in §4 with this in mind rather than bolting it on afterward.

## 7. Open questions, carried forward and new

- **`gclean`'s actual source has not been reviewed** — the single
  biggest gap in this handoff. Nearly everything in §3 (the residual
  plane access method's real signature, whether `tclean`'s return dict
  contains anything non-trivial to serialize, how `deconvolve` fits in
  relative to `tclean`) needs validating against real code before
  implementation starts, not assumed from this document.
- **`RemoteAppLink.sync_bridge`'s no-ambient-loop wiring** (§1) — the
  actual blocking prerequisite; resolve this before any `gclean`-specific
  proxy code, since `next(gclean)`'s call site cannot work without it.
- **`gclean.update(...)`'s calling convention** — `SyncBridge` vs.
  relaxing the call site to `async` — depends on how disruptive changing
  that call site would be in practice; not assessed.
- **Residual-plane delivery: poll vs. push** (§3) — recommend starting
  with poll-style for the first version; push-style is real follow-on
  work with its own decimation discipline to design.
- **Whether `tclean`'s return dictionary contains anything outside this
  framework's existing serializer's coverage** (§3) — check against
  real CASA6 output before assuming this is free.
- **How Chunk 1b's generic status vocabulary maps onto `gclean`'s own
  progress concepts** (§4) — not decided; likely needs its own
  message shape layered on top of, not replacing, the generic one.

## 8. Suggested implementation order

1. Read `gclean`'s actual source (§1) — everything else depends on this.
2. Resolve `RemoteAppLink.sync_bridge`'s ambient-loop wiring (§1) — the
   hard blocking prerequisite; this is genuinely new design work, budget
   real time for it.
3. Get `gclean` constructible via `create_object` in a dedicated
   execution context, with `.update()`/`__anext__()`/`.image_products()`/
   `._log()`/`.restore()` proxied per §2 — validate against the simplest
   possible real clean run before adding anything else.
4. Add the residual-plane access method (§3), poll-style first.
5. Add the tclean-return-dictionary path (§3) — verify its serialization
   against real output.
6. Register the convergence/progress push relay (§4).
7. Remove the dead `_gen_port_fwd_cmd()`/`self._is_remote` code (§5).
8. Only once all of the above works: consider push-style residual-plane
   delivery (§3) and the `on_reconnect` full-state-snapshot behavior
   (§6) as follow-on enhancements.
