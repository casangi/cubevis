# visplot_headless: drive a live visplot in headless Chromium

Runs `VisibilityPlotter` the way the `visplot` task does (browser GUI with
a live Python kernel behind it, not a static page), points headless
Chromium at it with Playwright, and runs a small Python "steps" file
against the page. Pressing Plot, flagging, panning and the busy cursor all
behave as they do for a user, so this is how GUI fixes are checked when
no person is at a browser (e.g. in a cloud session).

The older static check (`bokeh.embed.file_html` of `_build_layout()`)
still works for layout only; it has no kernel, so Plot does nothing.

## Needs

- cubevis importable from this checkout (found as `../../..`, or set
  `CUBEVIS_ROOT`)
- `playwright` with Chromium (`PLAYWRIGHT_BROWSERS_PATH` if preinstalled)
- a measurement set; the TW Hya test MS works. A full plotter on all TW Hya
  fields needs more than 8 GB, so pass `field=3c279` there.

## Files

| File | What it does |
|---|---|
| `live.sh` | `live.sh <steps.py> <out-prefix> [key=value ...]`: start the server, run the steps, stop the server. `key=value` go to `VisibilityPlotter` (`ms=`, `field=`, `kind=`, `scatter_x=`, `layout=`, ...) |
| `serve.py` | The server: `VisibilityPlotter(headless=False, enable_flagging=True, ...)` under `exe.Context(SYNC)`. Output in `serve.log`, including the `visplot timing:` lines |
| `record_url.sh` | Used as `$BROWSER`; writes the page URL to `url.txt` |
| `drive.py` | Opens the URL (`VW` x `VH` px, default 1700 x 1100, 8 s settle) and `exec`s the steps file with `pg` (the page), `out`, `FIND`, `os`, `json`, `time`. Prints console errors / warnings, plus lines containing `SHOWLOG` |
| `find.js` | `pg.evaluate(FIND, None)`: visible flag-tool figures with their screen boxes, panel name, tool and figure ids |
| `runtests.sh` | `runtests.sh <outdir> [glob]`: the visplot tests, one file per process (the whole suite in one process runs out of memory with the TW Hya MS). `MS` / `PS` env vars name the MSv2 / MSv4 data. Compare `summary.txt` before and after |

`examples/`: `shot.py` (screenshot and notification text),
`busy_timeline.py` (busy indicator against image updates after Plot),
`raster_flag_drag.py` (toolbar button click, drags, pixel reads).

## Useful handles inside a steps file

- Every model: `Bokeh.documents[0]._all_models.values()`; find widgets by
  `title` or `label`, set `.value`.
- A button's JS callbacks: `for (const cb of b.js_event_callbacks['button_click']) cb.execute(b)`.
- A figure's view (for screen coordinates): `Bokeh.index.find_one_by_id(fig.id)`;
  `view.frame.bbox` is the plot area within `view.el`.
- `window.__cvSetBusy`, `window.__cvBusyN`: the busy indicator and its count;
  wrap `__cvSetBusy` to log calls.
- Real mouse / keys: `pg.mouse.move/down/up`, `pg.keyboard.down('Shift')`.
  Synthetic events miss Bokeh's gesture handling; use the real ones.

## Practical

- `SLOW_PLOT=<seconds>` in the environment of `live.sh` delays every Plot
  reply by that much, e.g. to check that the busy indicator holds through a
  plot longer than 30 s (`SLOW_PLOT=45 WAIT=60000 DRIVE_TIMEOUT=200
  ./live.sh examples/busy_timeline.py ...`).

- Long runs: start with `setsid nohup ... & disown` and poll for a marker;
  a plain long `sleep` in a tool call has been seen to return early.
- `DRIVE_TIMEOUT` (default 300 s) bounds the browser part.
