# visplot save/restore ("view state"): design and handoff

Status: framework and the first unit (per-quantity raster scaling) are built and
tested; nothing in the GUI calls it yet. This document is the design record and
the plan for the rest, written so it can be picked up in a fresh chat.

**Revised priority (later in the same session):** save/restore should be pulled
FORWARD, ahead of the HRS waterfall work, because it lets internal users author and
save "plot modes" as JSON instead of waiting for code changes. That idea is sound
but narrower than "modes are just JSON"; see section 7A for what it can and cannot
do and section 8 for the revised order. The HRS roadmap (`HRS_VISPLOT_PLAN.md`,
milestone M1b) reflects the same change.

Suggested first message for a new chat:
> Continuing the visplot view-state (save/restore) work. Read VIEW_STATE_DESIGN.md.
> Start with the milestone in section 8 that I name (default: V2, then V3).
> Latest files are in view_state_step1.zip (see section 10).

---

## 1. Goal and the shape of the solution

Goal: be able to save and restore *pieces* of the GUI (one panel's color scaling,
two panels, five things, or the whole layout, with or without data references),
and build user-visible features on that (Back/Forward, named views, sharing).

Shape: an internal **registry of self-describing state units**. Each independently
restorable piece of the GUI is a small object (`key`, `version`, `order`, `scopes`,
`capture()`, `apply(state)`). You register as many as you like; saving one piece
or the whole GUI is just choosing which units to capture. This is the "register a
unit, accrete more" design that was asked for. It is a reasonable path: it is
small, testable in isolation, and every later feature (history, named views,
presets, code export) reduces to "capture some units / apply some units".

## 2. What exists now

| File | What |
|---|---|
| `view_state.py` | The framework (no Bokeh/backend imports): `StateUnit` protocol, `CallableUnit`, `StateRegistry`, `ApplyReport`, envelope format, JSON helpers |
| `scaling_memory.py` | `ScalingSettings`, `ScalingMemory`, `default_scaling_settings`, and `RasterScalingUnit` (the first concrete unit) |
| `visibility_raster.py` | Per-quantity scaling memory wired into `update_axes`; `capture_scaling_state()` / `apply_scaling_state()`; `_reshade_image()` factored out of `update_scaling` |
| `visibility_plotter.py` | `self._view_state = StateRegistry()`; one `RasterScalingUnit` per slot under `panel.<id>.raster.scaling`; thin `capture_view_state()` / `apply_view_state()` (not GUI-wired) |
| tests | `test_view_state.py` (framework), `test_scaling_memory.py` (pure pieces), `test_zscore_threshold_scaling.py` (behavior on a raster, incl. the plotter's real call pattern) |

### 2.1 Step 1: per-quantity scaling memory (behavior)
Each raster panel remembers its scaling settings (function, alpha, gamma, min/max,
"cutoff is automatic") **per quantity**. Leaving a quantity stores its settings;
returning restores them; the first visit uses that quantity's default. Only
Z-Score has a special default today (threshold at the n-aware cutoff);
`_QUANTITY_DEFAULTS` in `scaling_memory.py` is where to add others. Phase/Flag
were deliberately *not* given new defaults (a visual decision for the user).

Behavior changes vs before, all intended:
- Amplitude -> Phase no longer inherits Amplitude's range/scaling (it used to; a
  latent bug).
- Whatever you tuned on Z-Score is kept when you come back to it (the old code
  discarded it on leaving).
- Removed: `_pre_zscore_scaling` and `_init_zscore_scaling_state` (replaced by
  `_init_scaling_state`, `_switch_scaling_owner`, and friends).

## 3. The framework (view_state.py)

**Unit contract**
- `key`: unique, dotted; the prefix is ownership (`panel.A.raster.scaling`).
  **Keys are API**: they end up in saved files, so renaming one breaks old files.
- `version`: integer; bump when the shape of `capture()` changes.
- `order`: apply order, ascending (ties by key). Convention: 10-40 layout/axes,
  50 display (scaling), 60+ overlays/data.
- `scopes`: free-form tags (`display`, `selection`, `layout`, `data`). "Without
  data" = `exclude_scopes={"data"}`.
- `capture() -> dict` (JSON-serializable, checked at capture time) and
  `apply(dict) -> None`. Optional `migrate(from_version, state) -> state`.

**Registry operations**: `register` (dup key is an error unless `replace=True`),
`unregister`, `unregister_prefix` (drop a panel's units when it is destroyed),
`keys/capture/apply` with selection by explicit keys, key prefix, scope, and
excluded scope (combined with AND); `dumps/loads` for JSON.

**Envelope (schema 1)**: `{"format": "cubevis.visplot.viewstate", "schema": 1,
"units": {key: {"version": v, "state": {...}}}}`.

**Policies (each was a deliberate choice)**
- *Capture is strict, apply is tolerant.* A unit that raises or returns non-JSON at
  capture time raises immediately (it is the author's bug, better to see it where it
  was written). `apply` never raises for unit-level problems: it applies what it can
  and returns an `ApplyReport` (applied / skipped with reason / failed with the
  exception). KeyboardInterrupt/SystemExit always propagate.
- *Unknown keys in a file are skipped, not errors* (a unit that no longer exists, or
  a panel that is not present).
- *Version skew:* saved by a **newer** unit version -> skipped and reported (never
  guessed at). **Older** -> `migrate` hook if present (a failing migration is a
  reported failure), else skipped.
- *Partial restore is normal:* units not in the file are left alone; the caller can
  restrict what is restored (e.g. only the `display` scope of a file that holds more).
- *A restore is not atomic.* No rollback today (see open questions).

## 4. Design decisions in Step 1 worth remembering

- **Ownership tracking.** The plotter sets `panel._quantity = None` before every
  `update_axes(...)`, so `update_axes` can never learn the previous quantity. The
  panel therefore tracks `_scaling_owner` (which quantity the live settings belong
  to) and all switching keys on it. Two earlier attempts keyed on the previous
  quantity and silently never fired in the real GUI while their tests passed.
  **Any future unit that reacts to a quantity/axis change must do the same.**
- **"Live + remembered".** The live fields (`_scaling`, `_scaling_vmin`, ...) are
  the source of truth for the current owner; the memory table holds the others.
  `capture` merges live into a *copy* of the table (never mutates); nothing
  hooks `update_scaling`.
- **`apply` restores the memory table and loads the settings of the panel's
  CURRENT quantity, not of the saved "owner".** The quantity itself belongs to
  whichever unit owns the axes (ordered earlier). The saved `owner` is
  informational.
- **First-visit defaults are seeded from the constructor's alpha/gamma, not the
  live values** (a test caught the live-value version leaking Amplitude's tuning
  into Phase).
- Tolerant parsing: an unusable entry (unknown scaling function from a newer build,
  quantity that no longer exists) is dropped, not fatal.

## 5. The hard part: where GUI state actually lives

Registering a unit for *server-held* state is easy (scaling memory is one). Much
of the GUI is **not** held on the server:
- Most sidebar controls (axis selects, field/spw/antenna, correlation, colorize
  mode/axis/checklists, layout radio, info-display checkboxes) are **client-side
  Bokeh widgets**. The server sees them only in the payload of a Plot press
  ("staged" model), and there is **no Bokeh server** (a custom comm channel is used),
  so Python cannot simply assign a widget value.
- Server-side state exists but is partial: `self._selection`,
  `_last_raster_selection_by_slot`, each panel's `_x_dim/_y_dim/_quantity/_layers`,
  `_antenna_str`, etc.

Options for capturing/restoring UI-held state:
1. **Server mirror ("last applied Plot payload").** Capture what the server last
   received/applied (it already has most of it). Restore = push that payload to the
   client, which sets the widgets and calls `doPlot()`, exactly as the preset
   buttons do today (the preset JS is the working precedent). *Recommended first.*
2. **Client round trip.** `capture()` asks the browser for widget values over the
   comm (async). More exact but makes `capture` asynchronous and couples it to the
   browser being connected.
3. **Client-owned units** (JS objects implementing the same contract) with a small
   protocol for the server to request/apply. Cleanest long-term; largest change.

Whichever is chosen: **applying UI state should be one batched operation** (set all
widgets, then one `doPlot`), not many independent re-renders; and it must run like
`_handle_plot` runs `update_axes` (`async with panel._render_lock` +
`asyncio.to_thread`). `apply_view_state()` on the plotter is currently synchronous and
lock-free and is documented as not GUI-ready.

## 6. Inventory of state and proposed units

Existing = built. Sources are where the state lives today.

| Key (proposed) | Scope | Order | Source today | Notes |
|---|---|---|---|---|
| `panel.<id>.raster.scaling` | display | 50 | **built** | per-quantity memory |
| `panel.<id>.raster.viewport` | display | 55 | panel `_x_range/_y_range`, current viewport | server-held; easy; decide if saved by default |
| `panel.<id>.scatter.layer.<i>.scaling` | display | 50 | `ScatterLayer` fields | layers are rebuilt on Plot; key by polarization not index |
| `panel.<id>.scatter.colorize.<layer>` | display | 45 | client `colorize_handles` widgets | mode (0/1/2), axis, excluded, priority, display; needs section 5 |
| `theme`, `palette.raster`, `palette.scatter` | display | 20 | plotter `_theme`, `_raster_cmap_name`, `_scatter_cmap_name` | server-held; today only via constructor/theme toggle |
| `session.layout` | layout | 10 | client radio + `_layout` | one / side / over |
| `panel.<id>.kind`, `.raster.axes`, `.scatter.axes` | layout | 15-30 | client selects + panel dims | quantity change interacts with scaling ownership |
| `session.selection` | selection | 35 | `SelectionSpec` + widgets | field, spw, correlation, antenna, scan, time/uv ranges, data column |
| `session.info_display` | display | 60 | info selectors | cursor tracking, colorbar checkboxes |
| `data.source` | data | 5 | plotter path/backend/data group | identity for the dataset check (section 7) |
| `data.flags.pending` | data | 70 | `FlagDB` deltas | when flagging lands |

Not saved by design: frame caches, rendered images, sidebar collapse state (UI
ephemera; revisit).

## 7. "With data" vs "without data"

- **Without data** (default for sharing views): every scope except `data`. Display,
  selection, layout only; portable across datasets.
- **With data**: adds `data`-scope units: the dataset identity (path, backend kind,
  data group) and anything that only makes sense against that data (pending flags).
  **Never embed visibilities.**
- **Dataset identity check on restore:** a saved view carries a light fingerprint
  (e.g. antenna names, SPW ids, channel counts). Restoring onto a different dataset
  should warn and skip units that reference things that do not exist (a field, an
  antenna) rather than failing; the framework already reports skips.
- **Portability:** absolute paths in `data.source` are advisory only; never
  auto-open a path from a file without user confirmation.

## 7A. Plot modes (JSON) built on view state

Idea: an internal user tunes the GUI, saves the result, and that file *is* a
"plot mode" others can load, so satisfying a requirement means shipping a JSON
file, not a code change. Today's four presets (vplot, radplot, Waterfall,
Z-Score) are exactly this, hardcoded as a tuple plus JavaScript patches. This is
worth building, with the following limits understood up front.

**What it can do:** express *presentation choices* composed from behavior that
already exists: layout, panel kinds, axes and quantities, selection, colorize
mode, scaling, palette, zoom. It also lets us test the framework on real use.

**What it cannot do (needs code first):** anything that is a new capability. Of
the HRS bullets: per-baseline waterfall selection and averaging, phase rms, and
flagging all need code; "faster and more reliable" is not a view. A mode can only
reference features that exist.

**Snapshot vs template.** A snapshot is an exact saved state (this field, these
antennas). A *mode* is meant to be reused across datasets, so it must be able to
say "automatic" instead of a number (the Z-Score cutoff is already stored as
`auto_cutoff`, not as 4.9) and to omit selection units entirely. Decide the
parameter/rule syntax (for example "all antennas", "first N", "the SPW with the
most channels") before authors depend on it.

**Mode wrapper.** A mode file is an envelope plus metadata, so it can be listed,
explained and rejected cleanly:
```
{"mode": {"name": "Z-Score overview", "description": "...", "author": "...",
          "requires": ["quantity:Z_SCORE", "scatter.coloring:statistical"],
          "schema": 1},
 "state": { ...a view-state envelope... }}
```
`requires` names capabilities the running build must have; a mode that needs one
it lacks fails with a clear message ("this mode needs PHASE_RMS, not available in
this build") instead of half-applying. Needs a small capability registry.

**Validation.** Users will write invalid combinations (a waterfall of the Flag
quantity, a Time x Frequency raster with several baselines). Add a validate step
before apply that explains the problem; the per-unit `ApplyReport` (applied /
skipped-with-reason / failed) is the starting point.

**File-format contract.** Once internal users author files, unit keys, scope
names and schema versions are public: freeze the naming scheme first (open
decision 4), keep a saved-envelope fixture for each released schema, and keep old
files loadable for an agreed period.

**Loading.** A modes directory (shipped modes plus a user directory), selectable
as `visplot(mode="zscore")` and from a menu. Applying a mode is one batched
operation (section 5): set all widgets, then one Plot.

**Acceptance test that proves the design:** load each of the four existing presets
from a JSON mode file and confirm the resulting panel state (kinds, axes,
quantity, layout, scaling, colorize modes) equals what the hardcoded preset
produces today; then retire the JS-patched preset code paths.

**Authoring:** a "Save current view as mode..." action, so modes are made by
tuning the GUI rather than by hand-writing JSON.

**Dependency to be honest about:** the pieces that define a mode (layout, axes,
selection, colorize) are client-side widgets, so a useful mode needs the
browser-side restore path (V3). Until then a mode could carry only server-held
units (scaling, zoom, palette), which is not enough.

## 8. Roadmap

| # | Milestone | Effort* | Notes |
|---|---|---|---|
| V1 | Framework + scaling unit | done | this delivery |
| V2 | More server-held units: viewport, scatter layer scaling, palettes/theme | S-M | no client work needed; same patterns; add a "restore triggers one re-render" helper |
| V3 | Browser-side restore path (section 5 option 1) + units for layout, axes, selection, colorize; async/locked, batched apply | M-L | the real work and now the critical path; needs live GUI testing |
| V4 | Mode loader: wrapper + `requires` capability check, validation, modes directory, `visplot(mode=...)` / menu; the four presets converted to JSON with the parity test (section 7A) | M | first user-visible payoff; unblocks internal authoring |
| V5 | "Save current view as mode" export | S-M | modes authored by tuning the GUI |
| V6 | Back/Forward history; named views + sharing + dataset identity check; presets fully retired to modes; "Copy as Python"; pending flags as a `data` unit | M-L | after modes prove the framework |

**Order rationale (revised):** V2-V4 come before further HRS feature work (see the
HRS plan, M1b) because authored modes reduce the code needed for every later view
requirement. Back/Forward, sharing and code export are deliberately after modes:
they matter less than being able to author and ship modes.

\*S < 1 day, M 1-3 days, L 1-2 weeks; rough.

Suggested UX (later): Back/Forward buttons in the toolbar; a "Views" menu (Save
view..., list, Export/Import); presets shown in the same list.

## 9. Testing strategy

- Every unit: capture -> apply -> capture equality (round trip); tolerant parsing of
  bad/unknown entries; JSON-serializable state; no mutation on capture.
- **Drive units the way the plotter does** (quantity reset to `None` before
  `update_axes`), not by setting attributes directly; see
  `_plotter_style_update` in `test_zscore_threshold_scaling.py`.
- Registry-level: selection by key/prefix/scope, ordering, isolation of failures,
  version skew and migration (keep saved-envelope fixtures for each released schema
  so an old file is always tested against new code).
- Mutation-check new tests (disable the behavior; confirm failures). In this work it
  caught a real design flaw (live vs constructor alpha/gamma) and proved the tests
  catch the earlier "keyed on previous quantity" bug.
- Anything that touches widgets needs a live GUI check; the sandbox cannot run
  real-MS tests (`test_visibility_raster.py` etc.), so ask for a full-suite run.

## 10. File inventory (this delivery: `view_state_step1.zip`)

New: `view_state.py`, `scaling_memory.py` (both belong in
`cubevis/toolbox/visplot/`), `test_view_state.py`, `test_scaling_memory.py`.
Changed: `visibility_raster.py`, `visibility_plotter.py`, and the rewritten
`test_zscore_threshold_scaling.py`. Other files are unchanged from
`ZSCORE_OPTIMIZATION_HANDOFF.md` section 8. Local regression: 418 passed, 20
skipped (real-MS only).

## 11. Open decisions

1. UI-state approach (section 5): server mirror first, or invest in client-owned
   units up front?
2. Should a restore be atomic (validate everything, then apply; rollback on
   failure)? Cheap for pure server units, hard once widgets and renders are involved.
3. History: how many entries, what counts as a new entry (every Plot press? only
   when state changed?), and is zoom part of it?
4. Key/version policy: freeze the naming scheme now; decide how long old schema
   versions must remain loadable.
5. Which quantities should get their own scaling defaults (Phase, Flag) now that
   there is a place for them?
6. Security: files are JSON only and every unit parses defensively, but decide
   whether loading a view may ever trigger opening a dataset path.
7. Mode parameter/rule syntax (section 7A): how a mode says "all antennas",
   "first N", "widest SPW" without naming specific ones.
8. Capability registry: what strings a mode's `requires` may contain, and who owns
   the list as features are added.
9. Where mode files live (shipped directory, per-user directory, per-project) and
   precedence when names collide.
10. How strict validation should be (refuse to apply vs warn and skip).

## 12. Hazards (read before extending)

- The plotter's reset-to-`None` pattern (section 4); the same trick applies to
  `_y_dim` and `_x_dim`.
- Staged vs live: most controls reach Python only at Plot press.
- No Bokeh server: server code cannot set widget values; use the comm/JS route the
  presets use.
- `VisibilityRaster._comm` is a different channel object from the plotter's `ctrl`.
- Renders must be serialized with the panel's `_render_lock`; do not re-render from
  `capture`.
- Real-MS tests are not runnable in the sandbox.
- Also see `ZSCORE_OPTIMIZATION_HANDOFF.md` section 7.
