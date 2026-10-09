# HRS H5, slice 1: how far a flag reaches

*2026-10-07. Built against `main` at `22abed1`. Plan:
`hrs_visplot_plan.md` (H5; slice 2 is the filters).*

## What it is for

A box on a plot says *where* something is wrong. What is wrong is often
bigger than the box: an antenna (all its baselines), a scan, a whole
band. AIPS's flag editors have switches for this that apply to the
commands that follow; CASA has `flagdata` selections and `extend`.
Here it is a group of controls in the Flagging panel, **Flag reaches**.

## What the user sees

**Flag reaches** (Flagging panel, under *Preview each proposal*). All
start at "as drawn". They stay as set until changed, and apply to flag
and unflag boxes alike.

| Control | Effect on each box |
|---|---|
| Baselines: *As drawn* | the baselines in the box |
| Baselines: *All to the antenna they share* | every baseline of the one antenna common to the drawn baselines. Drag across two or more baselines of the bad antenna. Refused, with a message, when the box holds one baseline (which end?) or baselines without exactly one common antenna |
| Baselines: *All to every antenna drawn* | every baseline of every antenna that appears in the box |
| Baselines: *All baselines* | every baseline |
| All channels | the drawn times over the whole band (existed as "Extend to all channels") |
| All selected spectral windows | the same channel numbers, at the same times, in every selected window |
| Whole scan | every integration of the scans the box touches |
| All fields | drops the field selection; only matters on a plot without a Time axis |
| All correlations | not only the plotted one (existed as "Extend to all correlations") |

- A line under the controls reads "Flags cover what is drawn." and turns
  amber, listing what is on, while any setting is wider: the settings
  persist, so it must be visible at a glance that the next box is more
  than a box.
- The message after a box says how far it went, next to the count:
  "Flagged: 768 samples on 4 baselines -- reaching all baselines to
  ANTENNA-1; whole scan 2". The count is of what is really flagged.
  The preview dialog has a "Reaches" row. *Describe pending flags* shows
  it per flag ("reaches: ..." in Provenance) and the current settings.
- **Shift / Alt while dragging** (for one box, nothing to switch back):
  Shift stretches the box to the full height of the plot as now shown,
  Alt (Option on a Mac) to the full width; both, the whole view. The
  dashed box shows it while dragging, and pressing or letting go of a
  key without moving the pointer redraws it. What is sent is the box
  last drawn. "As now shown" is deliberate: the box never covers
  anything off screen. To take everything regardless of zoom, use the
  controls.
- **Esc** before letting go of the button drops the box; nothing is sent.
- **Amber outline**: while any Flag reaches setting is wider than the
  box, the box being drawn is outlined in solid amber instead of the
  dashed flag / unflag colour.
- **Reason**: a text box. Its text is stored with every flag made from
  then on, shown in the report and the preview, saved in the JSON, and
  written as `reason='...'` in exported flagdata commands (one line, at
  most 80 characters, quotes removed).
- **Help**: every control of the Flagging panel now shows help in the
  status area on hover, like the rest of the GUI. The tooltips on the
  export list and the filter parameters are gone; their text is in the
  status-area help (one per filter, built from the filter's own
  description and parameter help).
- Constructor / task arguments: `flag_reach` (comma-separated words:
  `shared-antenna`, `antennas`, `all-baselines`, `channels`, `spw`,
  `scan`, `fields`, `correlations`; empty = as drawn) and `flag_reason`.

## Corrected after Darrell's first test (2026-10-08, macOS)

- *Shift / Option boxes on a scatter flagged only the dragged box* while
  the stretched one was drawn. The tool recomputed the box at release
  from the pointer-up event; on macOS that event can arrive without its
  modifier keys. It now sends the box last drawn, and follows the keys
  through keyboard events while dragging. (Not reproducible in Chromium
  on Linux, where the release carries the keys; the new code does not
  depend on them.)
- *Ctrl+drag opened the context menu* on macOS (Ctrl+click is the
  secondary click). Ctrl is no longer a modifier; on Linux desktops that
  keep Alt+drag for moving windows, the Flag reaches controls remain.
- *The gear tabs were drawn over the export controls* in a window
  shorter than the sidebar's content. Bokeh caps each child of a column
  at `max-height: 100%`; the Flagging panel, taller since this slice,
  was cut to the window's height and the next section drawn over the
  rest. Each sidebar section now keeps its own height and the sidebar
  scrolls. (Latent before: any section taller than the window.)
- A scatter box dropped the reason and did not say that baselines /
  windows / scans / fields were not widened. Both fixed.

## Limits

- Baselines, spectral windows, scans and fields are widened for a
  **raster box with the "All selected" filter**. A scatter box, or a box
  with another filter, flags a list of samples, not a region; there
  these four are not applied and the message says so. (All channels and
  All correlations work everywhere, as before.) Slice 2 can lift this
  for the filters where it makes sense.
- "All selected spectral windows" uses channel *numbers*. For windows of
  the same shape (the usual case, and AIPS's meaning of "all IFs") that
  is the same part of each band. Channels a narrower window does not
  have are left out.
- On a Frequency axis shared by windows that overlap in frequency, the
  channel numbers are taken from every window the box covers.

## How it works

The reach is resolved when the box is proposed, into the flag record's
own fields (`flag_engine.widen_region`): antenna names, scan names with
`extend_scan`, channel ranges per window. Nothing new is stored as a
switch to be interpreted later, so the report, the JSON, the commit to
MSv2 / MSv4 and the flagdata export need no new cases, and a saved flag
means the same when loaded under another selection. The words go into
the record's provenance.

Order: the box is first verified as a region exactly as before (so a
region never reaches data the box did not address); only then widened;
then counted on the whole store (`region_counts_everywhere`), because a
widened region reaches beyond the plotted selection.

The request carries `scope` (`baselines`, `spw`, `scan`, `field`),
`reason`, and the tool's `span` ("x", "y", "xy" or absent). `scope` and
`reason` are plain data, so the remote path needs nothing new.

Flag tool: `cubevisjs/src/bokeh/tools/flag_tool.ts` (`_span_of`,
`_draw_box`); the five bundles under `cubevis/__js__/bokeh-3.*` were
rebuilt with `bokeh build` and are byte-identical to each other.

In-application "Write flags with CASA flagdata" sends the same commands
as before, without reasons (they select nothing, and that command set is
the one checked against CASA). Reasons are in the exported command file.

## Fixed on the way

`import cubevis` failed without network access: `cubevis/utils/__init__.py`
has `from socket import socket` and then names `socket.gaierror` in an
`except` clause of `have_network()`. That attribute lookup only runs when
the connectivity check raises, and then raises `AttributeError` itself,
which surfaced as "cannot import name 'InteractiveClean'". Seen here
when the sandbox's proxy was briefly unavailable. Now the exception
classes come from the `socket` module.

## Verified

- `tests/manual/visplot/test_flag_reach.py`, 48 tests, both backends,
  on a simulated MS (5 antennas, 3 scans, 2 windows of 16 and 8
  channels) and its MSv4 twin. What is flagged is read back from the
  flags, not from the record:
  - each Baselines setting; both refusals; whole scan, also across a
    scan boundary; scan + antennas + correlations together; unflag;
  - all windows: same channel numbers, clipping to a narrower window,
    with All channels;
  - undo / redo, JSON save and load (reason and reach kept), commit to
    a scratch copy and the flags read back from disk (the plan's exit
    test);
  - reason in records, report, preview and flagdata lines;
  - the controls, their start-up values from the constructor, every
    Flagging control wrapped for status-area help, none with a tooltip.
- Live, in headless Chromium against a running plotter (the task's own
  websocket server; `BROWSER` set to record the page URL): on both
  panels, a free box, Shift, Alt, Shift pressed after moving, and Esc
  send exactly the box drawn (or nothing for Esc); a Shift box on the
  scatter flags its whole column; the amber outline follows the
  controls; with a 700-pixel window the opened gear tab sits below the
  export controls.
- `scripts/sync_layers --check` clean after regeneration.

## Not verified

- In a browser with a live kernel: that the amber line updates as the
  controls change, that the help reads well at the sidebar's width, how
  Alt+drag behaves on the desktops in use.
- That CASA's flagdata accepts a quoted `reason` containing spaces in a
  list file (no CASA in the sandbox). If it does not, replace spaces
  with underscores in `flag_casa._line`.
