# Notebook state, reconnection and preferences (design, 2026-11-25)

Design for spin-off issues 1 (restore GUI state on reconnect), 2 (visplot in
notebooks) and 6 (user preferences) of `issue35_spinoffs.md`. Agreed with
Darrell on branch `44-visplot-in-notebooks`. Items marked **(verify)** are
assumptions to be checked with `devel/tools/jupyterlab_headless` before code
relies on them.

## Principles

* **The kernel is the source of truth.** A page that connects to a live
  session takes its view from the kernel; what the browser shows is never
  sent back as truth. Edits the kernel never received (a staged sidebar pick,
  an unsent cycleniter) are lost on reload. For visplot this is consistent
  with "stage, then plot".
* **Opening a notebook runs nothing.** As in Jupyter generally, outputs are
  snapshots until the user runs cells, in order. A GUI is never brought back
  to life on open: its data may depend on earlier cells that have not run.
* **Restore on run, with a check.** Running a cell may start the GUI from
  saved state, but data-dependent state is applied only if the inputs still
  match.
* **Preferences and state are different.** Preferences (how the user likes
  the tool) are data-independent, drawn from the portable vocabulary, and
  applied without checks. State (selection, iteration position, zoom, flags)
  depends on the data and is checked.

## Three cases

### A. Reload while the kernel lives (reattach)

Same session, so no fingerprint check. Saved notebook (restored output) and
unsaved notebook (`show(redisplay=True)`) take one path: when the page's
script connects and the session exists, it asks the kernel for the session's
state (or a fresh render) and brings its controls up to date. If the session
is gone, the GUI is shuttered (unchanged).

* **iclean first:** niter left, cycleniter, status / stop code, convergence
  data. Fixes the hazard of issue 1 (Continue after a reload resets the
  iteration budget to the niter given at `show()`).
* **visplot:** applied selection, iteration position, layout, panel kinds and
  axes, scaling, zoom -- through `view_state.py` units (only
  `RasterScalingUnit` exists; `apply_view_state` is not yet GUI-ready, see
  its docstring).

### B. Notebook opened (no session)

The saved output shows a **snapshot**: a PNG of the last view
(`png_export.py`) and "run this cell to resume", in place of a dead GUI.
Renders on GitHub / nbviewer too.

### C. Cell run in a new kernel (checked restore)

`show()` looks for saved state for this output and applies it:

| scope | restore | condition |
|---|---|---|
| `prefs` | yes | none |
| `display`, `layout`, `selection` (visplot) | yes | fingerprint matches |
| `data` (visplot pending flags) | no (explicit opt-in at most) | -- |
| iclean view (channel, colormap, ...) | yes | fingerprint matches |
| iclean iteration budget | **never** | niter comes from the cell's code |

Fingerprint: MS path plus modification time / size (maybe a table checksum);
for iclean, the image products too. On a mismatch the GUI starts fresh and
says why. Override: `show(state="auto" | "fresh" | <envelope>)`; an envelope
argument is also "state as code" (a button / `capture_view_state()` gives a
literal to paste into the cell).

## Where the notebook state is kept: a companion output

`show()` emits a second display, with its own `display_id`, carrying
`application/vnd.cubevis.state+json` (state + prefs), `image/png` (the
snapshot) and `text/plain`. The kernel refreshes it with `update_display`
on meaningful events (Plot, Done, layout change), throttled; the kernel
cannot see the user's save. It is saved with the notebook by the normal save.

* (verify) updating the companion does not touch the GUI output;
* (verify) a custom-mime output with `image/png` survives save and reload in
  JupyterLab 4.6 (and Notebook 7, VS Code, Colab later);
* reading back: running a cell clears its outputs in the browser before the
  kernel sees it, so the kernel reads the last-saved `.ipynb` from disk --
  (verify) that the notebook path is available in the kernel (`__session__`
  in recent ipykernel / jupyter_server); match by cell id or a key given to
  `show()`.
* State is per output (cell id + instance key), so several visplot panels in
  one notebook each keep their own; no notebook-wide layer for now.

Rejected for now: a JupyterLab labextension (most flexible, but JupyterLab /
Notebook 7 only and a TypeScript extension to maintain against JupyterLab
releases); a sidecar file (not saved with the notebook, lost on copy, and out
of step with the user's saves). Revisit the extension only if the companion
output fails.

## Preferences

Layers, lowest first: built-in defaults < user file
`~/.config/cubevis/prefs.json` < override file < the output's companion <
explicit arguments.

* Override file: `prefs=<path>` (on `VisibilityPlotter`, where the GUI is
  built in `__init__`; on the tasks) or `CUBEVIS_PREFS`. Override files are
  partial: only the keys they set, merged over the user file, so changes to
  the user file carry through. No automatic discovery of a file in the
  working directory ("no hidden functionality").
* Saving is explicit: "Save as my default" writes the file in use and says
  which. The notebook companion records each output's settings as part of
  its state (that is not a save of defaults).
* Which files were applied is logged / shown.
* **Not a `view_state.py` scope (revised 2026-11-27):** preferences are
  applied before the GUI exists (they resolve constructor arguments ahead of
  `_build_gui()`), and saving them reads the *browser* (a Pin click or a
  colormap pick never reaches the kernel's models), so they are a module of
  their own, `cubevis/toolbox/visplot/prefs.py`: the known keys (built-in
  default, check, the argument each supplies), load / merge / save.
* **Two groups (2026-11-27):** `app` -- `theme`, `layout`, `pin_layout`,
  `gridrows`, `gridcols`, `gridcolor`, `compact_toolbar`, `sidebar_open`;
  `panel`, the starting values of every panel (Panels A and B, and a panel
  shown on its own once that exists) -- `time_labels`, `time_gaps`,
  `freq_gaps`, `raster_cmap`, `scatter_cmap`, `flag_display`, `flag_color`,
  `flag_show_flagged`, `flag_flagged_color`, `gear_pin`. Not split by raster
  and scatter: a panel can switch kind, and most settings mean the same for
  both; kind-specific settings are named by kind (`raster_cmap`). Not
  preferences: what changes the data shown or how it is computed
  (`averaging`, `datacolumn`, `detrend`, `autocorr`, `baseline_combine`, the
  selection, the stats windows); for now also `baseline_order`, the panel
  sizes. Gear Pin is one value for every panel (A pinned, B not, is a
  session's choice).
* **Saving (step 2, agreed 2026-11-27):** in app mode, a gear tab's "Save as
  default" and an app-level one (in the sidebar) both write both groups; the
  panel group comes from that gear tab's panel, or, for the app-level
  button, the panel last plotted that is visible (a hidden panel never
  supplies it). A panel shown on its own saves only the panel group. Saving
  merges into the file (`save_prefs`), so the group not saved and the keys
  not given stay as they were. A colormap is saved only if the user picked
  one; otherwise it stays `null`, following the theme.
* **Constructor defaults:** most preferences are defaults of existing
  `VisibilityPlotter` arguments. `sync_layers` copies defaults verbatim into
  the task layers, which then pass every argument explicitly, so `__init__`
  cannot tell an explicit value from a default. Preference-backed arguments
  default to `None`, resolved through the layers, in `VisibilityPlotter`
  and in the generated layers alike. The casashell `inp()` shows such an
  argument as the value it comes to (`layout = 'over'`, default colour; the
  template's `prefs_module`, with the `prefs` global if set); `None` stays
  what is passed and written to `visplot.last`, so `tget()` does not pin
  today's preference as a user-set value.
* **Tests:** `CUBEVIS_PREFS=none` (built-in defaults only) is set by
  `tests/manual/visplot/conftest.py` and `tests/ci/run.py`, so a
  developer's own preference file does not change what tests see.
* **Remote kernels:** one preference set, read where the GUI is built (the
  user's host). No kernel-specific preferences: per-project needs are covered
  by notebook companions and override files. (verify) that GUI-side
  preferences never need to reach a remote kernel; if one affects kernel-side
  work, the local side sends the merged preferences at startup.
* A notebook on a remote Jupyter server builds the GUI in the kernel, so
  "user file" is the server's `~/.config/cubevis`; the companion covers
  per-notebook settings.

## visplot in notebooks: single panels

The notebook idiom for visplot is a single configured panel: the cell's code
is the sidebar (change an argument, run the cell again), configured from the
portable `config=` vocabulary, with a small state (zoom, scaling, iteration
position). `VisibilityPlot.show()` exists but uses `bk_show` without the
CommMgr / `Showable` path, so re-render on pan/zoom and hover will not work in
a notebook. Open: a separate entry point, or a minimal-chrome mode of
`VisibilityPlotter`. The full app in a notebook stays supported (issue 2's
list: `visplot.notebook()` via a `visplot_notebook` layer, toolbar width,
Output View sharing the comm, stray "Bokeh is not defined").

## Order

1. iclean reattach: niter etc. on reconnect (case A, iclean). **Done 2026-11-25.**
2. Preferences: loading (`prefs.py`, user file, override file, `None`
   defaults, `inp()`) **done 2026-11-27**; saving (the buttons) next.
3. Companion-output prototype in the headless harness (verify items).
4. visplot reattach and checked restore (cases A and C); snapshot (case B).
5. Remaining issue 2 items; single-panel entry point.
