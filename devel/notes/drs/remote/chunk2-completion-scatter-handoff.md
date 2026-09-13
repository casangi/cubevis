# Chunk 2 completion / scatter handoff

**Start here for the scatter work, not from Chunk 2's raster session
history.** This document is self-contained. Read alongside the
original `cubevis-remote-execution-chunk2-handoff.md` (§4 in
particular — still the right starting frame for scatter's design
question, though its scope has sharpened since it was written — see
"What changed since the original §4" below) and
`cubevis-remote-execution-developer-guide.md` for the framework's
general patterns.

---

## 1. Status: Chunk 2's raster path is done, confirmed end to end

`VisibilityPlotter(ms=..., backend="remote", kernel_name=...)` now
successfully constructs and renders both raster panels against a real
`sshpyk`-provisioned cluster kernel (`zuul06`) and a real ALMA MS
(`sis14_twhya_calibrated_flagged.ms`) — connect, `metadata()`,
`query_raster()`, and the full `_build_panels()` construction path all
confirmed working, not mocked. Scatter is the one remaining gap, and
it's now precisely scoped rather than mysterious — see §3.

### Files changed, and why

- **`cubevis/utils/_conversion.py`** — `CubevisSerializer`/
  `CubevisDeserializer` subclasses of Bokeh's own `Serializer`/
  `Deserializer`, fixing three confirmed gaps in Bokeh's stock
  serialization: no `Enum` support at all (`_encode_other` override,
  covers every `Enum` subclass generically); `Serializer` encodes
  arbitrary `@dataclass` instances natively but `Deserializer` never
  implements the matching reconstruction (`_decode_object` override);
  and tuples silently degrade to lists with no wire-level marker to
  reconstruct them (harmless for value comparisons, fatal the moment
  something uses a tuple as a dict key — which scatter's own
  `query_columns(xaxis, yaxes, ...)` does). All three are genuinely
  generic, Python-language-level fixes — not domain-specific — which
  is why they live in this shared module. `_resolve_class()` resolves
  a class from its dotted path via `sys.modules` first, falling back
  to `importlib.import_module()` only if not already loaded — avoids a
  real, confirmed import-lock deadlock risk when decoding happens on a
  background thread (`SyncBridge`) while something else in the process
  holds Python's import lock.

- **`cubevis/toolbox/visplot/_wire_types.py`** (new file) —
  visplot-specific wire types (`xr.DataArray`, `pd.DataFrame`),
  deliberately *not* in `_conversion.py`: these are domain-specific
  third-party types needed by exactly one application today, and the
  next one actually looked at (iclean, via `casatools.image`) doesn't
  even use xarray. Registered via the plain `Serializer.register(type,
  encoder)` / `Deserializer.register(tag, decoder)` API rather than a
  subclass override, since each is exactly one concrete type, not a
  family of subclasses the way `Enum` is. Composes with
  `_conversion.py`'s subclass with zero coordination needed in either
  direction — confirmed against real round trips, not just reasoned
  about. Must be imported on both ends of the wire before either type
  crosses it (both directions matter: `query_raster()`'s/
  `query_columns()`'s results flow worker → P_local; `probe_raster_pixel()`'s/
  `probe_scatter_pixel()`'s inputs flow P_local → worker) — and also
  needed by the supervisor process in the middle, which is why
  `create_context()`'s config carries a `wire_types` list (see
  `_supervisor.py` below) rather than this module being hardcoded
  anywhere.

- **`cubevis/toolbox/visplot/remote_reduction_context.py`** —
  `RemoteReductionContext`, satisfying both `ReductionContext` and
  `VisibilityReader`. Read-only Chunk-2 scope (calibration/flag-writing
  raise `NotImplementedError`). Owns its own `SyncBridge` and uses it
  to *run* `RemoteAppLink.open()` itself (not `link.sync_bridge`, which
  is a different loop — see the developer guide §6). Bypasses
  `call_method`/`create_object`'s convenience wrappers in favor of
  `dispatch_fast` + explicit `"error"` checking, wrapped in
  `RemoteBackendError` (developer guide §3's documented footgun: the
  convenience wrappers don't check for errors themselves).
  `create_context()`'s config carries both `register_function` and
  `wire_types`. Per-phase connect timing logged at INFO
  (`start_kernel()`/`RemoteAppLink.open()`/`create_context()`/
  `create_object()` each timed separately) — kept permanently, not
  diagnostic-only: cheap, and the exact tool that found the
  `PYTHONHOME` issue below.

- **`cubevis/toolbox/visplot/remote_registrations.py`** — worker-side
  `register_function`. Thin wrapper (`VisplotRemoteBackend`) around a
  real `MSv2Backend`/`MSv4Backend` + `LocalVisibilityReader`, forwarding
  every `VisibilityReader` method verbatim. Imports `_wire_types`.

- **`cubevis/remote/_supervisor.py`** — `_handle_create_context` now
  reads a `wire_types` key from `config` (a list of dotted module
  paths) and `importlib.import_module()`s each one. This is the
  supervisor's *only* touchpoint with anything application-specific,
  and it's data flowing through the existing `register_function`-style
  config mechanism, not a hardcoded import — the supervisor itself
  stays fully application-agnostic; a future iclean would pass its own
  `wire_types` list the same way. Necessary because the supervisor
  relays every message between P_local and the worker and must
  `deserialize()` each one to do so — confirmed by direct
  investigation that an unregistered custom type tag makes that
  `deserialize()` call raise inside the supervisor's own background
  read-loop task, which nothing awaits or checks, silently killing the
  relay for the rest of the session (manifests as P_local's request
  timing out with no other symptom at all).

- **`sshpyk`'s `provisioning.py`** (Darrell's own package, not
  `cubevis`) — `launch_remote_kernel()`'s constructed shell command now
  sets `PYTHONHOME` explicitly (`PurePosixPath(self.remote_python).parent.parent`)
  before the final `exec`. Root cause: with `PYTHONHOME` unset, CPython's
  own interpreter-startup path-search algorithm walks upward from the
  executable's directory looking for landmark files, and on zuul06 one
  of the candidate paths it probes lands on a literal `/home/lib` —
  which is very likely hitting a broken/slow autofs automount for a key
  that doesn't correspond to any real mount target, confirmed via
  `strace -T` showing one `stat()` call alone costing 49.27 of a ~49.6
  second total. Fixed connect time from ~154s to ~7s against a real
  cluster kernel — unrelated to any wire-protocol work above, found
  purely from the per-phase timing already mentioned.

### Key architectural principles confirmed, worth not re-deriving

- **Generic language-level wire gaps (Enum/dataclass/tuple) belong in
  `cubevis.utils._conversion`; domain-specific types (DataArray,
  DataFrame, whatever iclean eventually needs) belong in an
  application-owned `_wire_types.py`-style module, registered via the
  plain `.register()` API.** The dividing line is whether the *next*
  actual consumer would need it too — checked against iclean concretely,
  not assumed.
- **`create_context()`'s `config` dict is the generic extension point**
  for anything an application needs the worker *or* supervisor to know
  about (`register_function` for the worker, `wire_types` for both) —
  reach for this before ever hardcoding an application-specific import
  into shared framework code.
- **Raster's output is mathematically bounded to `max_cells`, for any
  MS/PS size, for either backend, regardless of how asymmetric the two
  display dimensions are** — verified directly from `_decimate_agg`'s
  actual stride formula (`stride = n / sqrt(max_cells)`, which cancels
  the other axis's size out of each axis's own stride calculation).
  Raster's existing local-shading design is sound and doesn't need
  reconsideration for scatter's fix. The one real, different-in-kind
  caveat: partitions are decimated independently and then concatenated
  *before* the final global decimation pass, so intermediate
  remote-worker memory can transiently reach `(partition count) ×
  max_cells` — real for an MS with many scan/intent partitions, but a
  local memory-pressure question on the worker, never a wire-transfer
  one.
- **Per-call remote latency has a real, fixed ~600-800ms floor**
  (dispatch/round-trip/backend-compute), essentially independent of
  transferred payload size within the ranges tested — confirmed via a
  real benchmark sweep, not assumed. Worth keeping in mind when scatter
  returns images instead of rows: the floor doesn't go away just
  because the payload gets small.

---

## 2. What triggered this handoff: scatter has no bound at all

The headless `VisibilityPlotter` smoke test's *default* (unrestricted)
selection made `query_columns()` return **30,913,392 raw visibility
rows** — confirmed directly via diagnostic logging, not inferred. At
~495MB after base64 inflation per column set, and `_query_all_layers`
requesting every polarization layer in one call (so realistically
~1GB for a 2-layer scatter panel), this is not a serialization bug —
every wire-protocol fix above is confirmed working correctly at this
scale. It's `MSv2Backend`/`MSv4Backend`'s `query_columns()` having no
output bound whatsoever, unlike `query_raster()`'s `max_cells`. This is
exactly the risk the original Chunk 2 handoff's §4 flagged from the
start, now hit concretely rather than hypothetically.

## 3. What changed since the original §4 — read this before designing the fix

The original handoff's §4 scoped the fix as: keep the Dask graph lazy
through `Canvas.points()`, bin server-side, no pre-averaging. That was
written before remote execution's real wire costs were understood
concretely. **The confirmed, corrected requirement, per direct
instruction: `query_columns()`'s binning *and* the Datashader shading
step both run on the remote host. The only thing that should cross the
wire is the already-shaded image** — an RGBA array sized to the canvas
(e.g. 800×600×4 bytes, a few MB, fixed regardless of whether the
underlying selection is 10 rows or 10 billion) — not a bounded
DataFrame of binned counts, and never raw rows.

This is a *materially bigger* redesign than "make the aggregation
lazy," for a reason worth being explicit about: it moves the
`tf.shade()` / colormap step itself off of P_local, which is real,
new-in-kind design work, not a mechanical extension of the binning fix.
Concretely:

- `query_columns()`'s return type needs to change again — not to
  `dict[(Axis,pol), DataFrame]` (bounded bin counts), but to something
  like `dict[(Axis,pol), RGBA image + extent]`, with colormap/scaling
  parameters (`cmap`, `scaling`, `scaling_alpha`, ...) becoming *inputs*
  to `query_columns()` rather than something `VisibilityScatter`
  applies afterward locally.
- **`probe_scatter_pixel()`'s current design breaks harder under this
  than it would have under the original §4 plan.** It identifies
  individual MS rows within a hovered pixel by indexing into a
  client-held `scatter_df` — under the corrected design, P_local never
  holds *any* row-level data, not even a bounded aggregate. This has to
  become a genuine remote call, resolved against the raw data where it
  still lives, with its own design questions: does it re-run a
  targeted `query_columns()`-style call scoped to just the hovered
  pixel's region, or does the worker need to cache enough state from
  the last `query_columns()` call to answer probes without
  re-querying? Not designed yet — flagged in the original handoff as
  real, undesigned work even before this correction, and still is.
- **Colormap/scaling changes stop being free.** Today, recoloring a
  scatter plot is a local operation against cached data (mirroring
  raster). Once shading moves remote, every colormap or scaling
  adjustment becomes a network round trip against that same ~600-800ms
  floor. This is a real UX regression relative to today's local
  behavior and worth deciding how to handle explicitly (debounce
  aggressively? cache the last few renders client-side keyed by
  scaling params? accept the latency?) rather than discovering it after
  the fact — this is the same category of cost the iclean pre-notes
  flagged for `ImagePipe`'s existing server-side quantization, for the
  same underlying reason.
- **This does *not* apply to raster** — see §1's confirmed finding.
  Raster's local shading stays exactly as designed.

## 4. Dask distributed — noted, explicitly a separate, later goal

The eventual goal is genuine `dask.distributed` execution — spreading
the read-and-reduce across multiple reserved cluster nodes rather than
one worker process on one node, which is all that exists today.
`_raster_2d`'s reduction is already expressed as a real lazy Dask
graph, which is structurally what a distributed scheduler needs to
parallelize — but no distributed scheduler is deployed or configured
anywhere yet. This is real, but separate, future work: worth keeping
in mind that scatter's redesign should not accidentally foreclose it
(e.g., don't design the remote-side binning/shading step in a way that
assumes a single-process Dask scheduler if avoidable), but standing up
distributed execution itself is not in scope for the scatter fix.

## 5. Suggested first steps for the next session

1. Confirm the exact worker-side call shape for a shading-inclusive
   `query_columns()` — what `Canvas.points()` + `tf.shade()` call
   sequence produces the right image, using this session's confirmed
   MS as the test fixture.
2. Design `probe_scatter_pixel()`'s remote-resolution mechanism before
   writing code — this is the one piece with a real, currently-unclear
   design question (re-query vs. cached worker-side state), not just
   an implementation task.
3. Decide the colormap/scaling latency question explicitly, rather
   than shipping the mechanical fix and discovering the UX cost later.
4. Reuse `bench_remote_reduction_context.py`'s pattern (connect timed
   separately from per-call cost) to get real numbers for the new
   image-returning `query_columns()`, the same way raster's `max_cells`
   tuning got real numbers instead of guesses.
