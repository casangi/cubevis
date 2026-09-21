# CommMgr concurrency mechanism — implementation notes

Companion to `PART4_IMPLEMENTATION_NOTES.md`. Covers a separate, later bug: a
"WebSocket declared dead" false positive during legitimate slow renders
(colorize-by-axis, scaling changes, axis changes), traced to the transport
layer rather than anything in Part 4's UI code, and the mechanism landed to
fix it.

## The bug, in one paragraph

`WebSocketTransport.run()` awaited `self._message_callback(msg)` inline,
inside its `async for message in self.websocket:` loop. A slow, fully
synchronous handler (a real backend render) therefore blocked that same
loop from even reading the *next* incoming frame — including a `__ping__`
— for the handler's entire duration. The client's heartbeat watchdog
(`low_level_transport.ts`, 15s ping / 10s pong timeout) can't tell that
apart from a genuinely dead socket, so it declared the connection dead and
tried to reconnect mid-render.

## The fix, in one paragraph

Two changes, needed together: (1) `run()` now dispatches each message via
`asyncio.create_task()` instead of awaiting it inline, so the loop can
always see and answer the next frame regardless of how long any dispatched
processing takes; (2) the handlers that actually do slow backend work are
now `async def` and internally `await asyncio.to_thread(...)` for that
work, so they yield the event loop instead of occupying it. Neither change
alone is sufficient — see the git history / prior conversation for why.

## Why a lock was needed, and where it lives

Making handler dispatch non-blocking means two different messages can now
genuinely interleave in a way they never could before (previously,
*everything* was accidentally serialized by simply blocking the one
shared event loop). Traced against both apps' actual `Comm` usage:

- **visplot**: `VisibilityPlot.__init__` opens two Comms per instance —
  `self._comm` (squash_queue=True, scaling/colorize/probe traffic) and
  `self._flag_comm` (squash_queue=False, flag/unflag traffic) — both
  touching the same instance's render/selection state. Separately,
  `VisibilityPlotter._handle_plot` (`doPlot`, on its own comm) calls
  `panel.update_axes()` directly on whichever panels changed.
- **iclean**: `Cube` opens a "cube mask control" comm (mask-mod, done,
  config-statistics, palette, fetch-spectrum — already squash_queue=True,
  already multiple message types sharing one Comm) while the `ImagePipe`
  it owns separately opens an "image cube updates" comm. Both read/write
  the same underlying image/mask data.

In both cases, **Comm boundaries don't align with actual shared-state
boundaries** — confirmed by tracing the real code, not assumed. The
existing per-`comm_id` send-side throttle (`CommMgr.send`/`comm_mgr.ts`'s
`this.pending.has(commId)` check — verified identical on both ends) already
guarantees no two messages *on the same comm_id* can be in flight at once,
regardless of message type. It does nothing for two *different* comm_ids
that happen to share an underlying Python object.

The lock therefore lives on the **shared object**, not on any one `Comm`:
`CommMgr.open(..., lock=None)` accepts an existing `asyncio.Lock`; when
omitted, a fresh comm-private lock is created (matching today's behavior,
effectively free since a single comm's own send-side throttle already
orders it). `_handle_request` holds whichever lock applies for the full
handler-call-through-reply-send critical section. Passing the *same* lock
to multiple `open()` calls is how the caller — the only party that
actually knows the relationship — declares "these comms touch the same
object."

## What changed, by file

### `_comm_mgr.py`
- `CommMgr.__init__`: new `self._comm_locks: Dict[str, asyncio.Lock]`.
- `CommMgr.open(..., lock=None)`: populates `_comm_locks[comm_id]`, either
  the caller-supplied lock or a fresh one.
- `_register_comm` (the direct-construction fallback path) and `close()`
  keep `_comm_locks` consistent with `_comms`/`_handlers`/`_send_queue`.
- `_handle_request`: the handler-call-through-reply-send block (including
  both the success and error-reply paths) now runs inside
  `async with lock:`.

### `_low_level_transport.py`
- `WebSocketTransport.__init__`: new `self._pending_message_tasks: set`
  (holds strong references so asyncio never GCs an in-flight
  fire-and-forget task).
- `WebSocketTransport.run()`: `await self._message_callback(msg)` →
  `asyncio.create_task(...)` + `add_done_callback` for error logging
  (mirrors `Task._run_coroutine_sync`'s existing Jupyter-branch pattern in
  `cubevis/exe/_task.py` — not a new idiom for this codebase).

### `visibility_plot.py` (visplot)
- `self._render_lock = asyncio.Lock()` created once; passed via the new
  `lock=` argument to both the `self._comm` and `self._flag_comm`
  `open()` calls.

### `visibility_scatter.py` (visplot)
Converted to `async def` + `await asyncio.to_thread(...)` for the actual
backend-touching call: `_handle_set_color_mode`, `_handle_set_alpha`,
`_handle_update_scaling`, `_handle_colorize`, `_handle_update_axes_scatter`,
`_handle_probe_region` (a real `probe_scatter_region()` backend round
trip — also wrapped in `async with self._render_lock:`, since it reads
`self._x_dim`/`self._selection`/`self._layers`, the same state a
concurrent colorize/scaling/axis-change handler mutates). `_handle_probe`
deliberately left untouched — its own docstring confirms it's resolved
entirely from a local cache, no backend call.

### `visibility_raster.py` (visplot)
Same treatment for the three analogous handlers:
`_handle_set_color_mode_raster`, `_handle_update_scaling_raster`,
`_handle_update_axes_raster`. Raster's own `_handle_probe` is also
local-only (post-2026-09 redesign removed its old backend round trip) —
left untouched, no raster equivalent of `_handle_probe_region` exists.

### `visibility_plotter.py` (visplot)
- `_handle_plot` (`doPlot`'s handler, already `async def`): both direct
  `panel.update_axes(...)` call sites (raster branch, scatter branch) now
  do `async with panel._render_lock: await asyncio.to_thread(panel.update_axes, ...)`.
- `_activate_slot_kind` converted to `async def` (its own first-render
  `update_axes()` calls get the same lock+to_thread treatment); its one
  call site in `_handle_plot` updated to `await` it.

### `_cube.py` / `_image_pipe.py` (iclean)
- `Cube.__init__`: `self._render_lock = asyncio.Lock()` created once,
  passed into `ImagePipe(..., lock=self._render_lock)` and into
  `self._comm_mgr.open(..., lock=self._render_lock)` for the "control"
  comm.
- `ImagePipe.__init__`: new `lock=None` parameter, passed through to its
  own `comm_mgr.open(..., lock=lock)` call. `None` (the default, e.g. an
  `ImagePipe` used standalone) falls back to `CommMgr.open()`'s own
  default of a fresh, private lock.
- **No handler conversion** — per explicit scoping, iclean's own handlers
  (`mod_mask`, `receive_return_value`, `config_statistics`, `fetch_palette`,
  `fetch_spectrum`) stay fully synchronous for now. The lock plumbing is
  purely preparatory: as long as every handler touching `Cube`/`ImagePipe`
  state stays synchronous, nothing here changes iclean's runtime behavior
  at all (the lock is never contended, since nothing else can run
  concurrently with a synchronous handler regardless of locking) — it's
  exactly the mechanism needed the moment iclean's own maintainers convert
  a slow handler (`fetch_spectrum` is the likely first candidate) the same
  way visplot's were.

### Deliberately not touched
- `comm_mgr.ts` / `low_level_transport.ts` (the JS mirror): this app is
  overwhelmingly j2p (confirmed), and the actual heavy computation is
  entirely server-side — there's no evidence of, or reason to expect, a
  slow *browser-side* handler blocking anything. `comm_mgr.ts` does have
  a mirrored `handleRequest` (p2j direction) that would need the same
  treatment if that ever became necessary, but it isn't part of this fix.
- `data/reader.py`, `_scatter_render.py`, `palettes.py`, `msv2_backend.py`,
  `msv4_backend.py` — untouched, as in the Part 4 delivery.

## Verification status

**Executed and passing, against the real patched code:**
1. Two independent comms (no shared lock) interleave correctly — a slow
   handler on one doesn't block a fast handler on the other from
   completing first.
2. Two comms explicitly sharing one lock fully serialize, in dispatch
   order (FIFO, matching `asyncio.Lock` semantics).
3. `run()`'s fire-and-forget dispatch: a real `__pong__` is sent in
   ~2ms following a message that triggers a simulated 0.2s-slow handler,
   vs. the old (unpatched) code, which — verified directly, as a negative
   control — blocks the pong for the full 0.2s.
4. Full integration, real `VisibilityScatter` against the real backend and
   the real extracted MS: constructs correctly with a shared
   `_render_lock` between `self._comm`/`self._flag_comm`; all six
   converted handlers are genuine coroutine functions; a real colorize
   request round-trips through `CommMgr._handle_request` (real lock, real
   async handler, real backend query) and produces the correct result;
   and — the key property — five concurrent "tick" coroutines all
   complete *while* a real backend colorize call is still running in its
   `asyncio.to_thread` worker, confirming the event loop genuinely stays
   free during real (not simulated) slow work.

**Reviewed but not executed:**
- `visibility_plotter.py`'s `_handle_plot`/`_activate_slot_kind` changes —
  reviewed by hand (the lock+to_thread pattern is identical to what's
  verified elsewhere), but not exercised against a real multi-panel
  `VisibilityPlotter` instance, which needs more scaffolding than this
  session built out.
- `_cube.py`/`_image_pipe.py` — reviewed by hand only; iclean's fuller
  dependency chain (casatools, the mustache UI template) wasn't
  reconstructed for execution in this session.
- Whether `iclean`'s `Cube`/`ImagePipe` construction order (ImagePipe is
  constructed *before* the control comm is opened, per the existing code)
  causes any issue with the lock being created early enough — reviewed by
  reading the exact code path, not executed.

## Suggested next steps

1. Run this against a real multi-panel `VisibilityPlotter` (both a raster
   and a scatter slot, or two of either) with real `doPlot` traffic
   overlapping real colorize/scaling traffic on one of the panels, to
   exercise `_handle_plot`'s lock acquisition against a live per-panel
   comm — the one path this session couldn't reach for real.
2. A live iclean smoke test: open a cube, edit a mask region while
   simultaneously navigating channels, confirm no behavior change (since
   nothing there is async yet, none should be observable) and no new
   errors from the added `lock=` plumbing.
3. When iclean's maintainers do want the ping/pong fix for their own slow
   handlers, the pattern to follow is `visibility_scatter.py`'s
   conversions here — `async def` + `await asyncio.to_thread(sync_call, ...)`,
   already covered by the lock this session put in place.

## Addendum: a second, independent transport-lifecycle bug (also fixed)

After the above landed, live testing surfaced a *different* crash during
a real "box zoom" / info-tool drag: `AttributeError: 'NoneType' object
has no attribute 'run'` in `process_messages()`, plus a related
`RuntimeError: WebSocket not connected` escaping from `_handle_request`.
Traced to the actual code (not guessed at) — this is a **separate** bug
from the ping/pong one above, present since before any of this session's
changes (confirmed: it's the exact same crash, at the exact same line, as
the very first traceback shared at the start of this whole investigation).

**Root cause 1 — a race on `self._transport` across overlapping
connections.** `process_messages()` is invoked once per incoming
WebSocket connection. It creates a transport, assigns it to
`self._transport`, `await`s `.connect()` (a suspension point), and later
does `asyncio.create_task(self._transport.run())` — re-reading the shared
attribute rather than using a local reference. If a *different* incoming
connection's own `process_messages()` call runs during that suspension
and retires "the old transport" (calling `_reset_for_reconnect()`, which
sets `self._transport = None`), the first invocation's later
`self._transport.run()` finds `None`. The frontend's own aggressive
reconnect-on-declared-dead behavior (see `low_level_transport.ts`'s
`declareDead()`) is exactly what produces the overlapping connection
attempts needed to trigger this.

**Fix:** `process_messages()` now captures `transport = self._transport`
immediately after creating (or, for colab/jupyter, after `initialize()`
sets) it, and uses that local variable for `set_message_callback`,
`connect()`, `create_task(transport.run())`, and the `finally` block's
close-status query and cleanup — all immune to `self._transport` being
mutated by a different, concurrent invocation. `self._transport` itself
is untouched otherwise; other code (`_handle_request`'s `send_message`
calls) still sees "the current transport" as before.

**Root cause 2, found while fixing the above — the same class of bug at
the cleanup end.** The `finally` block's `else` branch (non-fatal
disconnect) called `self._reset_for_reconnect()` unconditionally. If a
newer connection had already replaced `self._transport` with its own,
live transport by the time this invocation reached its own cleanup,
this would null out that *other*, active connection's transport and
bump `self._connection_generation` out from under it — for no reason
related to that other connection.

**Fix:** guarded as `if self._transport is transport:
self._reset_for_reconnect(...)` — only resets if `self._transport`
still refers to *this* invocation's own transport. `_on_connection_closed`
still fires unconditionally (it's informational about this invocation's
own connection ending, which is true either way).

**Root cause 3 — `TransportNotConnectedError` wasn't in the "benign,
don't report" set.** `send_message()` raises a plain `RuntimeError`
(different message text in `WebSocketTransport` vs. `CommsTransport`)
when it finds `self._connected` already `False` — a deliberate, distinct
signal from the underlying `websockets` library's `ConnectionClosedError`/
`ConnectionClosedOK` (which fire when a send is actively attempted
against a failing socket), but semantically the same "peer is gone, not
a handler bug" case. `_handle_request`'s except clauses only caught the
two `websockets` exceptions, so this fell through to the generic
`except Exception` branch — reported as a real error, and (if the
*error-reply* send also hit the same "not connected" condition) escaped
`_handle_request` entirely, only becoming visible via this session's own
`_on_message_task_done` logging in `run()`.

**Fix:** introduced `TransportNotConnectedError(RuntimeError)` in
`_low_level_transport.py`; both `send_message()` implementations
(`WebSocketTransport`, `CommsTransport`) now raise it instead of a bare
`RuntimeError`, keeping their existing descriptive messages. `_comm_mgr.py`'s
three relevant except clauses (`_send_immediate`, and `_handle_request`'s
two reply-send paths) now catch it alongside `ConnectionClosedError`/
`ConnectionClosedOK`. A bare `RuntimeError` for an unrelated reason is
unaffected — this is a precise, dedicated subclass, not a broadened
`except RuntimeError`.

### Verification (this addendum)

Executed against the real patched code:
- Simulated the exact race: a "concurrent" invocation replaces
  `self._transport` between this invocation's transport creation and its
  `create_task(transport.run())` call — confirmed the local `transport`
  reference is used correctly (no `AttributeError`), and the newer
  transport is left running untouched.
- Confirmed the `_reset_for_reconnect()` guard: when `self._transport`
  still matches this invocation's own transport, the reset fires
  normally; when it doesn't (a newer one has taken over), the reset is
  correctly skipped and the newer transport is left alone.
- Confirmed `TransportNotConnectedError` during a reply send is treated
  as benign (no entry added to `self._errors`) — and, as a control, that
  a genuine handler bug (`ValueError`) is still reported normally, so
  the widened except clauses don't swallow real errors.
- Re-ran the full real-backend integration test (real `VisibilityScatter`,
  real `CommMgr`, real colorize round trip) after all of the above —
  still passes with no regressions.

Not executed: a true concurrent-connection scenario against a real
`websockets` server (would need real network infrastructure this sandbox
doesn't have) — the race itself was verified at the level of the specific
logic that was actually broken (the local-reference capture and the
reset guard), not via spinning up two real overlapping WebSocket
connections.

## Addendum 2: inner exception handler swallowing the outer handler's own connection-closed case

Reported separately: repeated identical `ERROR`-level log lines during
laptop sleep/wake --

```
[cubevis.bokeh.transport._low_level_transport] ERROR: Error processing
message: received 4000 (private use) stale connection; then sent 4000
(private use) stale connection
```

**Root cause.** `WebSocketTransport.run()` has two layers of exception
handling around its `async for message in self.websocket:` loop:

- An **outer** `except (ConnectionClosedError, ConnectionClosedOK)`
  wrapping the whole loop, already correctly designed for exactly this
  case (logs at `debug`: *"Normal close - don't treat as error"* --
  matching the method's own docstring: *"ConnectionClosedError can
  happen when laptop sleeps and is NOT re-raised"*).
- An **inner**, per-message `try/except Exception` inside the loop body
  (wrapping `deserialize()` and the `__pong__` reply send), added to log
  and skip a single bad message without killing the whole loop.

Since `ConnectionClosedError`/`ConnectionClosedOK` are `Exception`
subclasses, the inner catch-all intercepts them *before* they can ever
reach the outer, already-correct handler -- logs them at `ERROR`, and
lets the loop continue (its own comment: *"Continue processing other
messages"*), so the outer handler never runs at all. The connection
closing (4000/"stale connection" -- the client's own heartbeat watchdog
deliberately closing what it's decided is a dead socket; see
`low_level_transport.ts`'s `declareDead()`) most plausibly surfaces here
via the `__pong__` reply's `await self.websocket.send(...)` racing the
close, and recurs once per frame still in flight around the closing
handshake -- matching the repeated identical lines.

This predates this session's other changes -- both the outer handler and
its docstring were already there; the inner one just sat in the way of
it.

**Fix:** the inner `except` now has its own
`except (ConnectionClosedError, ConnectionClosedOK): raise` clause,
positioned before the generic `except Exception`, so these two
specifically re-propagate to the outer handler instead of being
absorbed and mislogged.

**Verification:** built a fake WebSocket whose `send()` raises a real
`ConnectionClosedError(Close(4000, "stale connection"), ...)` -- its
`str()` renders as *exactly* the reported log text, confirming the
diagnosis precisely, not just plausibly. Confirmed the fix: no
`ERROR`-level log record is produced, and the close is correctly logged
at `debug` by the outer handler. Confirmed as a real regression test (not
a tautology) by also running the *old* inner-handler logic directly
against the same simulated exception and observing it does produce the
`ERROR`-level result the reported bug showed.
