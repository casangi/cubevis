# HRS visplot: handoff, 2026-10-07

For whoever (person or Claude session) picks up the HRS work on `visplot`
next. Everything needed to start is in this directory; this note says
where, what state things are in, and what was learned the hard way.

## Read first

| File | What it is |
|---|---|
| `hrs_requirements.md` | The requirement (CASR-385) |
| `hrs_commissioning_workflow_survey.md` | What AIPS / CASA users do when commissioning, and what visplot had |
| `hrs_visplot_plan.md` | The plan (revision 2), H1 to H8, with a dated tracking table at the end |
| `hrs_h1_raster_averaging.md` | Vector / scalar averaging |
| `hrs_h2_phase_statistics.md` | Phase RMS, Coherence, windows, slope removal, DIFF quantities |
| `hrs_h3_antenna_baseline_selection.md` | Antenna / Baseline / SPW tables and text entry |

## State

`main` at `4869088` plus the delivery of 2026-10-07 (see the plan's last
table row).

| Item | State |
|---|---|
| H1 raster averaging | Done; confirmed in the browser |
| H1b phase-safe resample | Done; tests only |
| H2 phase rms, coherence, windows, scatter forms | Done; confirmed in the browser on TW Hya |
| H2 presets, DIFF quantities | Done; tests and a headless static render only |
| H3 selection tables, baseline stepping | Done; confirmed in the browser |
| H3 phase waterfall, cyclic colormap | Done; headless static render only |
| H3 sort Baseline raster by length | **Not done**; see below |
| H4 all-baseline waterfall (FTFLG view) | Not started |
| H5 flag scope extensions | Not started |
| H6 averaged line plots | Not started |
| H7 antenna x antenna matrix | Not started |
| H8 static output, plot summary dialog | Not started; dialog design agreed (below) |

## Next, in suggested order

1. **H4 + baseline-length ordering.** Both need the baseline axis to stop
   being "numeric `baseline_id`, in id order". Today that assumption is
   in the raster coordinate, the tick labels, the cursor probe
   (`visibility_raster._probe_raster_pixel_local`), flag regions and
   their half-cell snapping (`flag_engine.py`, search `Axis.BASELINE`),
   and the flag overlays. Introduce one position <-> baseline mapping and
   route all of them through it; lengths come from antenna positions (or
   median UVW).
2. **H5 flag scopes**, on the views H3/H4 provide.
3. **H8 plot summary dialog.** Agreed with Darrell: a dialog with the
   full description of a plot (selection, axes, averaging, windows,
   counts), opened by a hotkey scoped to the plot and a toolbar button.
   `casalib.hotkeys` (hotkeys-js) is already in the bundle; `Showable`
   in `cubevis.bokeh.models` manages key event delivery in notebooks.
4. H6, H7, then Correlation as a raster axis (it is offered in the axis
   lists and raises a clear "not implemented").

## Known open issues

- Two-line title: a clipped fragment of text sometimes shows above it
  after a replot (Darrell's screenshot, 2026-10-06). Not reproduced in a
  headless Chromium with Bokeh 3.10 by changing a one-line title to a
  two-line one in either order of text / font size. Worth knowing the
  browser and Bokeh version where it shows.
- Multi-SPW Baseline x Time shows the first SPW only (`_raster_merge`
  keeps the first non-NaN value).
- The Baseline table does not narrow to the ticked antennas.
- Whether `cvNoAutofill` stops browser autofill history: unconfirmed.
- Short noisy windows read somewhat low in Phase RMS.
- `PHASE_DIFF` reads above 90 deg where a window has no coherent mean
  (see the H2 note).
- No VLBI-style test dataset yet. TW Hya (ALMA) is the real-data check.
- `data/reader.py` still has a legacy path that returns Phase in
  radians; the two live backends return degrees and `Axis.PHASE` now
  says so.

## Working conventions (Darrell's)

- Deliveries are zip files of complete files, unzipped at the root of a
  cubevis checkout. He commits to `main`; record the hash each time,
  because he sometimes forgets to say it moved.
- MSv2 is primary; everything must also work on MSv4.
- Flagging is to be web-native, not a copy of AIPS keystrokes.
- One kind of help only: text in the status area while the pointer is
  over a control (`VisibilityPlotter._hover`, the `EvHover` model). No
  tooltips. Text boxes and tick boxes must never hold competing
  selections.
- Fix problems where they are found rather than working round them.
- He is not an astronomer; say what a view is for, not only what it is.

## Practical notes

- Tests: `export CUBEVIS_SRC=$PWD; python -m pytest tests/manual/visplot`.
  About five minutes. In the cloud sandbox: 11 failures and 13 errors
  that need packages or data it lacks (remote / reconnection / lifecycle
  tests), about 600 skips that need the TW Hya MS. Compare the *set* of
  failures before and after, not the count.
- Simulated data for tests: `xarray_ms.testing.simulator.MSStructureSimulator`
  (see the `sim_paths` fixtures); convert to MSv4 with
  `xr.open_datatree(ms, engine="xarray-ms:msv2").to_zarr(...)`.
- `scripts/sync_layers` regenerates the task layers from
  `VisibilityPlotter.__init__`; constructor defaults must be literals.
  Run it after any constructor or docstring change; `--check` verifies.
- cubevisjs: TypeScript in `cubevisjs/src/bokeh`, built with
  `bokeh build`, bundle copied to `cubevis/__js__/bokeh-3.{6..10}/`. The
  five committed bundles are byte-identical. Nothing in the 2026-10-07
  delivery needed a rebuild.
- Stock Bokeh widgets emit no pointer enter / leave events; wrap in
  `EvHover`.
- A hidden gear tab's `Select` throws when its value is set before the
  tab was ever opened; preset JS wraps such assignments in `try`.
- `_do_plot_js` and the JS string constants it is built from must stay
  bare module-level names: `test_checkbox_guard.py` resolves them by AST.
- **Headless look at the GUI** without a notebook: build the plotter,
  `bokeh.embed.file_html(vp._build_layout(), INLINE)`, insert
  `cubevis/__js__/casalib.min.js` and the matching `cubevisjs.min.js` as
  inline scripts before the document JSON, open with Playwright. There
  is no Python connection, so Plot does nothing, but layout, titles,
  colours, the initial images and status-area help can all be seen and
  driven through `Bokeh.documents[0]`.
