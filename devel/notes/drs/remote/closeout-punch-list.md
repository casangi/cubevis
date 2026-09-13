# Chunk 2 closeout — `visplot` remote data path

**Status: Chunk 2 (2a raster, 2b scatter, 2c hover-probe/`InfoTool`, 2d
remote-path validation) is complete for what each was scoped to do.**
This document is not a re-statement of that work — the implementation
doc's Chunk 2 sections have the full technical account, correction
notes included where the shipped shape differs from what was originally
designed. This document's job is narrower and more important for what
comes next: **what still stands between today's state and a basic,
functioning `visplot` app with a real `sshpyk`/Jupyter-kernel remote
backend for MSv2/MSv4 data access** — the gap between "the remote
execution framework works" and "a user can actually run `visplot`
against a remote host and get a good experience."

Every item below is sourced either from direct verification this
chunk, from the actual git history / prior handoff documents in
`devel/notes/drs/remote/`, or is marked as genuinely unconfirmed rather
than assumed. Where an earlier handoff already flagged something as an
open checklist item, this document says so explicitly and states
whether it was actually resolved since — several were not.

---

## 1. The one item that should be first, in Darrell's own words

The commit that consolidated this project's development notes for the
branch merge (`2075fbf`, the same commit `main` will fast-forward to)
says, verbatim: **"the fundamental remote execution... framework is
complete. Still have not tested display of a remote MSv2/MSv4
measurement dataset with this framework."** That statement is more
recent than any of the partial GUI confirmations described below, and
should be treated as the current, authoritative status — not
superseded by them.

This is genuinely in tension with two earlier, narrower claims worth
being aware of rather than ignoring:

- `chunk2-completion-scatter-handoff.md` (Chunk 2a's own completion
  note): `VisibilityPlotter(ms=..., backend="remote", kernel_name=...)`
  "successfully constructs and renders both raster panels against a
  real `sshpyk`-provisioned cluster kernel... and the full
  `_build_panels()` construction path all confirmed working."
- `chunk-2c-handoff.md` §1: hover-probe piece 1 (raster's exact,
  per-hover lookup) "**confirmed working via live GUI test**."

Both of those are real, specific, narrower confirmations (construction
+ render succeeding; one interaction succeeding), not a comprehensive
"I opened the app and used it" pass — and neither covers scatter's
panels at all. The most recent, broadest statement from the person who
wrote all of this is that a real end-to-end display pass hasn't
happened. **Reconcile this explicitly before assuming raster's GUI
story is settled, and treat scatter's GUI story as fully open.**

**What "confirm real end-to-end GUI display" should mean, concretely,
so it's not another partial pass:**
1. Open a real `VisibilityPlotter` session against a real `sshpyk`
   kernel and a real MSv2 *and* a real MSv4 dataset (four combinations:
   raster×MSv2, raster×MSv4, scatter×MSv2, scatter×MSv4).
2. For each: confirm the panel actually renders correct-looking data,
   not just that construction didn't raise.
3. For each: exercise hover, at least one pan, at least one zoom, and
   (scatter only) at least one `InfoTool` click and one drag.
4. For scatter specifically: confirm colormap and scaling controls
   still work and produce a visibly-updated plot (§3 below explains why
   this is a real, separate round trip now, not a free local
   operation).

## 2. Scatter's remote wire path: tested at the wrong level, and only against a local kernel

`chunk-2c-handoff.md`'s own next-session checklist (§6) had this as
item 4: *"Confirm scatter's remote path end-to-end including hover on
an actual remote session... the remote relay path
(`RemoteReductionContext`/`remote_registrations.py`) has the fix
applied but hasn't been exercised live yet."* And item 2: *"Confirm
`MSv4Backend`'s scatter path against real data via GUI."*

**Neither was resolved by this chunk, and it's worth being precise
about why, since real progress was made adjacent to both:**

- `test_query_columns_matches_local`/`test_probe_scatter_region_matches_local`
  were added to `test_remote_reduction_context.py` this chunk —
  genuinely new coverage of scatter's remote wire path that didn't
  exist before at any level. But they exercise `RemoteReductionContext`
  directly, via pytest, never through `VisibilityPlotter`/`InfoTool`
  itself — and they were only ever run against a **local** kernel.
  The last confirmed-clean run against real `zuul06` (670 passed, 3
  skipped, 0 failed) happened *before* these two tests were added, so
  scatter's remote wire path has never actually been exercised against
  a real cluster kernel, local-kernel-as-proxy testing notwithstanding.
- MSv4 parity was confirmed at the same pytest level (both new tests
  pass against `PS`), which is real progress over "not reviewed at
  all" but is still not what item 2 above asked for.

**Concrete next step:** re-run the full suite (including these two new
tests) against real `zuul06` once more, and separately do the GUI-level
scatter×MSv4 pass named in §1.

## 3. Scatter's remote interactivity has no caching, debouncing, or overscan

Covered in full in the standalone `remote-viewport-responsiveness-
assessment.md` (2026-09) — summarized here because it's real, user-
facing, and unresolved, not because the detail belongs in this
document. Raster keeps almost all pan/zoom local via a cache-and-crop
scheme; scatter has no equivalent tier at all post-redesign — every
pan, zoom, *and* colormap/scaling change is a full server-side
`query_columns()` round trip, a cost the redesign's own source comments
already named (debouncing / stale-while-revalidate / overscan) without
implementing any of them.

**What's new since that assessment was written:** `chunk2-completion-
scatter-handoff.md` independently named the colormap/scaling
consequence at the time shading moved server-side, and a real
per-call latency floor is now measured (~600-800ms, independent of
payload size) — so a real remote scatter session today should be
expected to feel noticeably less responsive than raster, not just
theoretically.

**Not resolved by this chunk.** The assessment's own recommendation:
debouncing first (cheapest, lowest-risk, helps both plot types), then
decide overscan/prefetch for scatter specifically once real remote-
storage-latency numbers exist (still not measured — see §7).

## 4. No kernel/execution-context reuse across sessions — confirmed, not assumed

Checked directly against the real source: `RemoteReductionContext`
never references `KernelPersistenceManifest` anywhere. Every
`RemoteReductionContext(...)` construction spins up a fully fresh
`AsyncKernelManager` and worker subprocess from scratch — there is no
`existing=` reattachment, no manifest lookup, nothing reused.

This matters because the underlying framework has a working, tested
mechanism for exactly the opposite (Chunk 1c's `KernelPersistenceManifest`
— record/reattach/forget, confirmed tested, see
`chunk1c-persistence-manifest-flaky-heartbeat.md` for its one known
flaky-under-load edge case, itself unrelated to the manifest logic
itself). The whole "start on-site, disconnect, reconnect from home
hours later" scenario the design doc's reconnection section (§2e/§2f)
is built around is a real, designed capability — `visplot`'s own
integration just doesn't use it yet. For a "basic functioning app," the
practical consequence is: every time a user opens a new `visplot`
session against a remote host, they pay the full connect cost again
(now ~7-11s on a healthy host, not the ~150s the `PYTHONHOME` bug used
to cost — see the implementation doc's raster-completion section — but
still real, and still worth avoiding on repeat use if the same remote
host/kernel is still alive).

**Not designed or built.** Worth a real decision: does `visplot` want
this for its first "basic functioning" release, or is paying connect
cost per session acceptable for now?

## 5. `RemoteReductionContext.__init__` blocks synchronously for the full connect

Confirmed by construction (not a bug — a design property worth
surfacing): opening the MS/PS happens eagerly, inside `__init__`, via
`create_object()` — resolved in the implementation doc's "Open
questions" as the deliberate choice, but its UX consequence for a
*basic functioning app* was never separately assessed. A caller
constructing `VisibilityPlotter(backend="remote", ...)` blocks for
however long connect + kernel launch + worker spawn + MS open takes —
on a healthy `zuul06`-like host today that's roughly 7-11s (§4), on an
unhealthy one it could still be much longer (the `PYTHONHOME` class of
bug is fixed on the one host it was found on; a different remote host
could have its own undiscovered slow-path quirk — see the developer
guide §5's own caution about this).

**Not designed:** should this become async with a real loading
indicator in the GUI, or is a blocking construction with some "please
wait" affordance acceptable for a first basic release? No answer
exists yet either way.

## 6. Scope exclusions worth explicitly re-confirming, not just inheriting

- **Read-only.** `RemoteReductionContext` raises `NotImplementedError`
  for calibration/flag-writing (`chunk2-completion-scatter-handoff.md`'s
  own stated scope). If "basic functioning" is read-only plotting, this
  is fine as-is. If flagging over a remote session is expected to work
  for a first release, it's entirely unbuilt.
- **`ReductionContext.submit()`'s `Future`-bridge** — still explicitly
  undesigned, per both the original Chunk 2 handoff and the
  implementation doc's own carried-forward note. Worth a quick check of
  whether any current `visplot` call site actually invokes `.submit()`
  at all; if none do, this stays moot for "basic functioning" and can
  keep waiting.

## 7. Framework-level limitations that land on `visplot`, unchanged since the developer guide's own §7

Named there in full; listed here only because a "basic functioning
app" inherits all of them:

- **No execution-context cleanup.** A `visplot` session abandoned
  uncleanly (browser tab closed without a clean shutdown) leaves its
  worker subprocess — with an open MS/PS — running on the remote host
  indefinitely. On a shared/reserved cluster allocation, this is a real
  resource-leak risk, not a cosmetic one.
- **No `P_local`-restart reattachment to a still-live remote session** —
  only a network blip (process stays alive, connection drops and
  returns) is handled; `P_local` itself restarting and finding its way
  back to a specific prior session is unsolved (related to, but
  distinct from, §4's kernel-reuse gap — this one is about
  *`visplot`'s own session state*, not just the kernel process).
- **No `dask.distributed`.** Confirmed by direct grep (implementation
  doc, Chunk 2b correction): every remote computation runs single-host,
  thread-parallel at most, regardless of whether a real multi-node
  cluster sits behind the remote host. Real, but explicitly a separate,
  later goal per both the original handoff and this chunk's own
  correction — not a "basic functioning" blocker.
- **No payload chunking.** Probably a non-issue given scatter's image
  output and raster's `max_cells` are both bounded to a few MB, but
  named for completeness, matching the developer guide's own honesty
  about it.

## 8. Minor, low-priority loose ends

- **`test_persistence_manifest.py` flakiness under full-suite load** —
  documented in `chunk1c-persistence-manifest-flaky-heartbeat.md`:
  intermittent (~1 in 4), only under full-suite load, working diagnosis
  is transient OS scheduling contention from many real kernel
  start/stop cycles in one pytest session, not a logic bug. No code
  change recommended there; re-check the three files that document
  flagged as not-yet-reviewed for cleanup discipline
  (`test_bridge.py`, `test_worker_process_transport.py`,
  `test_worker_start_reattach_real_kernel.py`) if it recurs or worsens.
- **The "scatter remote-execution design notes" document** referenced
  by name in two source comments (`visibility_scatter.py`,
  `data/msv2_backend.py`) does not exist anywhere in the repository —
  searched directly, confirmed absent (see the viewport-responsiveness
  assessment's §2b for the full account). Either write it for real, or
  update those two comments to stop pointing at a document that isn't
  there.
- **The `devel/notes/drs/remote/` archive copies of `design.md`/
  `developer-guide.md`/`implementation.md`** are stale snapshots of an
  intermediate state of this chunk's own doc updates (confirmed
  directly against git history) — not wrong, just superseded by what's
  now in `devel/docs/remote/`. No action needed beyond knowing not to
  treat them as current if anyone opens them later.
- **The `CommMgr` reply/close-race fix's second symptom** — one
  duplicate "Error processing message" log line from the original field
  report was never fully explained (the retried-error-reply mechanism
  that explains the first is confirmed; a second, independent in-flight
  request hitting the same race was the working guess for the second,
  never confirmed). Deferred by explicit choice last chunk; still
  deferred.
- **The local scatter pipeline's cold/warm timing variance**
  (`TestTiming`) — real, reproducible, mechanism still not pinned down
  (tried and failed to explain it via OS page cache and via `numba` JIT
  warmup, both this chunk). Orthogonal to remote execution specifically;
  not blocking anything above.

---

## 9. Suggested priority order

1. **§1 — the real end-to-end GUI pass**, all four combinations (raster/
   scatter × MSv2/MSv4), including interactive hover/click/pan/zoom.
   This is the one item stated most recently and most plainly by the
   project owner as not done; everything else is easier to prioritize
   correctly once this exists.
2. **§2 — re-run the full suite against real `zuul06`** once more,
   picking up the two new scatter-remote tests that were never run
   against a real cluster kernel.
3. **§4/§5 — decide the kernel-reuse and construction-blocking
   questions explicitly.** Both are real UX properties of "opening
   `visplot` against a remote host" that no one has decided are
   acceptable or not for a first basic release; deciding costs little,
   and building either fix is more work than deciding whether it's
   needed at all.
4. **§3 — debouncing**, the cheapest of the viewport-responsiveness
   options, as a first concrete step once §1/§2 confirm there's a real
   interactive session to make responsive.
5. **§6 — confirm the read-only/`submit()` scope exclusions are still
   acceptable** for a first release (cheap to check, expensive to
   discover late that they weren't).
6. Everything in §7/§8 — real, worth tracking, none of it blocking a
   first basic release.

## 10. Definition of done, suggested, for "basic, functioning `visplot` with a remote backend"

- §1's four-combination GUI pass, done and confirmed, not just
  constructed/rendered once.
- §2's real-`zuul06` re-run, clean.
- §4 and §5 each have an explicit answer (built, or explicitly deferred
  with a stated reason) rather than an unexamined default.
- §6's scope exclusions confirmed still correct for what "basic" is
  meant to cover.
- Whatever remains genuinely unresolved after that stated plainly in
  an updated status document — matching this project's own established
  convention throughout every handoff that led here — rather than
  silently dropped.
