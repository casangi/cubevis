# HRS H3 (part): antenna and baseline selection tables

*2026-10-05. Built against `main` at `452ed8f`, on top of H2 slice 2
(statistic windows), which was not yet in `main`. Plan:
`hrs_visplot_plan.md` (milestone H3).*

## Why

A single baseline could not be selected from the GUI: the free-text antenna
box took antenna names only and selected every baseline of each. The
per-baseline waterfall (the AIPS SPFLG view) and per-baseline phase
statistics need exactly that.

## What the user sees

In the sidebar's Data section, built like the SPW table (scrolling,
checkboxes, select-all in the header row, Prev / Next):

- **Antenna** table, with a switch under it: *Either end* / *Both ends*.
- **Baseline** table: every baseline in the data, with the number the
  Baseline axis of a raster (and the cursor readout) shows for it. Hidden
  for single-dish data.
- A one-line **text box** under each of the SPW, Antenna and Baseline
  tables.

The free-text antenna box is gone.

## The rule

The ticked rows are the selection; nothing else is.

| Ticked | Selects |
|---|---|
| Nothing | Everything |
| Antennas, *Either end* | Every baseline with a ticked antenna at either end (one antenna = that antenna against all others) |
| Antennas, *Both ends* | Only baselines whose two antennas are both ticked (tick all, untick one = exclude it; tick a few = sub-array) |
| Baselines | Exactly those, whatever the Antenna table says |

Ticking every antenna is the same as ticking none. With *Both ends* and
antennas that share no baseline (one antenna, no autocorrelations), Plot
is refused with a message rather than drawing nothing.

"Nothing ticked means all" differs from the SPW table, where an empty
selection is refused. A note under each table says so.

## Text boxes

Typing in a box and pressing Enter (or leaving the box) turns the text
into ticks: the table's ticks are replaced, the box is cleared, and a
message says how many rows were ticked. The box never holds a selection,
so it cannot disagree with the table. If any part matches nothing,
nothing changes, the text stays for correction, and the message names
the part. Ticks are staged like any other tick: press Plot to apply.

| Form | Meaning |
|---|---|
| `DA41`, `3` | a row by name or number, as the table shows them |
| `a, b; c` | several (`,` or `;`) |
| `a~b` | every row from a to b, in table order, either direction |
| `!a` | not a. Alone: all rows except these. With other entries: those minus these |
| `A&B` | Baseline box only: that baseline, either order, names or numbers |

In the Antenna box, `!` also sets the switch to *Both ends* (anything else
sets *Either end*), and applying text clears any ticked baselines, which
would otherwise override it. A spectral-window name shared by several
windows ticks all of them.

While the pointer is over a box, the status area shows what may be typed,
with examples using this data set's own antenna names (as Time range and
UV range do).

The boxes also ask the browser not to offer its autofill history
(unrelated values from other pages dropping down on click). Bokeh has no
property for this, so the attribute is set on the input element through a
view lookup that has varied between Bokeh 3.x releases; it is written to
do nothing, silently, if the lookup fails. **Not verified in a browser.**

## Stepping

- Antenna Prev / Next ticks one antenna and clears ticked baselines.
- Baseline Prev / Next ticks one baseline. When some (not all) antennas are
  ticked it steps only through their baselines; otherwise through all, in
  number order.

## Constructor and task

`antenna=` is unchanged in form and now sets the initial ticks:
`"DA41"`, `"DA41,DA43"`, `"!DA42"`, `"DA44&DV19"`, mixed with `;` or `,`.

**Behaviour change:** `antenna="!DA42"` now excludes DA42 (ticks the
others, *Both ends*). Before, it resolved to "every other antenna" under
the either-end rule, which kept all of DA42's baselines to those antennas.
A part that matches nothing is still skipped with a warning.

## How it is built

- `data/_baseline_meta.py`: both backends report `baselines` in
  `metadata()` (`[id, antenna1, antenna2]`, the data's own orientation,
  which is what `SelectionSpec.baselines` must carry: the backends match
  ordered pairs). Added to `reader.METADATA_KEYS`.
  `ObservationMetadata.baselines` / `BaselineInfo`.
- `antenna_baseline_select.py`: the rule, the text interpreter in Python
  (`parse_selection_text`, used for the constructor string) and JavaScript
  (`PARSE_SELECTION_TEXT_JS`, used by the boxes), the plot-request payload,
  the stepping functions, status text. No Bokeh, so all of it is testable.
- `visibility_plotter.py`: the widgets; the plot request carries
  `antenna_names` and `baselines` as lists (the *Both ends* rule is turned
  into an explicit baseline list in the browser, so the server needs no
  third kind of selection); `_handle_plot` stores them; the status bar
  reads "Baseline 5/15: A&B", "Antennas: 5 of 6, both ends (10
  baselines)".
- **Bug fixed on the way:** the comparison that decides whether a panel
  re-queries looked at `antenna_names` but not `baselines`, in both the
  raster and scatter branches. A change of ticked baseline alone would not
  have replotted.

## Verified in the browser (Darrell, 2026-10-05)

Tables, switch and text boxes working as far as tested. Reported: the
text boxes showed no help in the status area; added (above).

## Verified

`xarray-ms` installs in the sandbox and its simulator builds an MS, so
from this delivery on the real backends and a real headless
`VisibilityPlotter` are exercised (on simulated data, both MSv2 and its
MSv4 zarr twin).

- `test_antenna_baseline_selection.py`: 123 passed.
  - interpreter rules; Python and JavaScript agree on 24 shared cases
    (JavaScript run under node);
  - the shipped apply, payload and stepping JavaScript under node;
  - real backends: 15 baselines listed for 6 antennas; one listed baseline
    selects one baseline, numbered as listed; a reversed pair matches
    nothing; single-baseline Time x Channel Phase RMS is blank without a
    window and reads the simulated 10 deg with a channel window;
  - real plotter: tables built from the data; each text box wired once;
    messages for a ticked antenna, a ticked baseline, *Both ends*, nothing,
    junk, and the legacy string form give the right selection and status;
    changing only the baseline re-queries raster and scatter; the
    constructor strings above set the right ticks.
- `test_antenna_iteration.py` (24) now runs the shipped table-based
  stepping function instead of a copy of the old text-box one.
- `test_checkbox_guard.py` updated to resolve the one new name in the
  `_do_plot_js` expression it reads from source.
- Whole `tests/manual/visplot` with the simulator available: clean
  `452ed8f` 1312 passed, this tree 1524 passed; the same 11 failures and 13
  collection errors on both (packages or data absent in the sandbox); 613
  skipped (need the TW Hya MS).
- `scripts/sync_layers --check` clean after regeneration.

## Not verified

Anything that only happens in a browser: that the tables and boxes render
and theme correctly, that ticks display, that the header checkbox toggles
all, that Enter in a box fires its change event, and that the sidebar is
still comfortable with the added height.

## Not built

- The Baseline table does not narrow to the ticked antennas' baselines
  (Baseline Prev / Next does).
- Baseline length is not shown, and the Time x Baseline raster is not
  sortable by length (remaining H3 items).

## Found while testing, not changed

`colormap_scaling.equalize_histogram` raises "Too many bins for data
range" for float32 values that differ by less than about 0.8% without
being identical (spreads of 1e-3 and 1e-4 fail; 1e-2, 0, and any float64
input pass). Hit when building a plotter on unit-amplitude synthetic data;
a model column of nearly constant amplitude could do the same. Casting to
float64 before `np.histogram` should fix it.
