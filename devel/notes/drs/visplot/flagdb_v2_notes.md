# FlagDB v2 — implementation notes

Status (part 2, 2026-09-28): implemented end to end and tested
(`tests/manual/visplot/test_flagdb_v2.py`, 35 tests on a simulated MSv2, incl.
the real box handler); smoke-tested on sis14_twhya (Ceres) in the sandbox.
Browser-side behaviour (FlagTool response callback, Flagging sidebar section,
review dialog, panel refresh) still needs a live GUI check.

## Part 2 (GUI)
- `flag_controls.FlagController` owns the FlagDB, the filter registry,
  preview/display/colour/extend settings, proposals, export and the widgets.
- `VisibilityPlotter(flag_filters=, flag_preview=False, flag_display="hide",
  flag_color="#ff00ff")`; `plotter.flag_db`, `plotter.flags`,
  `plotter.export_flags(path, fmt="flagdata"|"jsonl")`.
- Box handler: per-panel-object callbacks; box resolved on the displayed
  (union) grid of the plotted axis; a Channel axis that the backend did not
  relabel (windows differ) is treated as Frequency (the aggregate is the truth).
- Flag views (`SelectionSpec.flag_view`): effective | disk | pending | proposal,
  applied by `LocalVisibilityReader` via a contextvar read in `_flag_mask`.
  "hide" = effective; "color" = disk + pending overlay; a proposal under
  review is painted orange in both modes.
- Panels: `_flag_stale` -> next pan/zoom re-render re-queries at full extent
  first; overlays composited in raster `_render`/`_shade_viewport` and scatter
  `_push_image`. Per-figure rerender debounce timers (shared timer bug fixed).
- FlagTool: new `response_callback` property (Python + TS); tool activation no
  longer zooms to 1:1 (gate removed; a plain click still zooms).
- Pre-existing bug fixed: Flag-fraction raster masked padding after reducing,
  which re-broadcast baseline (3-D result for Time x Frequency) and let padded
  slots inflate the fraction.

## Decisions (agreed 2026-09-28)
- Flag, Unflag, Undo, Redo, Clear all operate on the DB (ordered fold).
- Display is user selectable: default "hide" (pending-flagged points vanish,
  AIPS-like); alternative shows pending points in a user-selected colour.
  Backend support: `set_pending_flags(..., apply=False)` + `pending_flag_mask()`.
- The 1:1 zoom gate is removed (part 2); filters replace it.
- Constructor parameter: `flag_filters=`.
- Z-Score filter reference matches the colouring of the panel being flagged:
  scatter = whole selection per baseline+correlation (`reference="selection"`),
  raster = per spectral window (`reference="spw"`); `granularity="cell"`
  reproduces the raster's per-cell max with the Sidak-corrected cutoff.
- Scatter boxes flag what is displayed (visible layers, hidden categories excluded).

## Findings that shaped the design
- `time` is UNIX seconds in both xarray-ms (MSv2) and xradio (MSv4)
  (`time.attrs["format"] == "unix"`), not MJD seconds as older docstrings say.
  Deltas carry `time_format`; export converts.
- SPW identity may be a non-unique name (simulated data: every window
  `<Unknown>`). `SpwKey` = identity + frequency span + channel count.
  MSv2 export maps windows to SPECTRAL_WINDOW rows via arcae (`spw_casa_ids`);
  otherwise `name:f0~f1Hz`, else `*:f0~f1Hz` with a WARNING comment when ambiguous.
- xarray-ms pads missing (time, baseline) slots as flagged with NaN
  EFFECTIVE_INTEGRATION_TIME. Such samples are "invalid" and never changed
  (an unflag box cannot expose padding).
- Every render path in both backends gets flags from `_flag_mask(ds)`; the
  backends now implement `_disk_flag_mask` and `XArrayReader._flag_mask`
  folds pending deltas lazily (`dask.map_blocks`).
- Scatter frame cache generation is now `(cache_generation, pending_version)`.

## Representation
- Raster box + identity filter -> region delta, verified against every
  partition's coordinates; falls back to explicit samples if not exact.
- Scatter box or any non-identity filter -> sample-set delta materialized at
  proposal time (frozen selection), compact encoding (indices or bits).
- Export never uses `clip` for value conditions: materialized `manual` lines.

## Open / later
- Remote: built-in filters run in the worker; user filters are local-only
  (clear error remotely).
- Scatter flag evaluation re-reads the selection (exact but not cached);
  optimise later if slow on large data.
- Frame cache re-reads on every pending change; later: apply pending mask to
  cached frames instead.
