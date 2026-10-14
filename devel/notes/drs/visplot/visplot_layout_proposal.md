# visplot GUI layout: proposal for after the first astronomer testing

*2026-10-11. Status: proposal, deferred. Darrell decided to have the
project astronomers test the current layout first and refactor
afterwards, so their feedback can shape it. Base: `main` at `bae9852`.*

## 1. The problem

visplot grew one requirement at a time (H1 to H8), and each control went
where there was room:

- **Top toolbar:** sidebar toggle, Plot, Reload, three layout buttons,
  twelve preset buttons, Export PNG and the theme switch. The presets
  overflow narrow windows and wrap, and nothing says what they are for
  until you hover over them.
- **Left configuration panel:** one long scrolling column.
  - *Data*: column, field, SPW table, correlations, autocorrelations,
    scan, the antenna and baseline tables, time range, UV range.
  - *Flagging*: filters, preview, reaches, reason, display, undo / redo,
    report, export / commit / backups.
  - *Panels*: Pin layout, Time labels, and the transient gear tabs, each
    with a Pin checkbox.
- **Stepping** (Field / SPW / Antenna / Baseline ◀ ▶) is part of the Data
  section, so while flagging you have to scroll up to it.

The full control list is in appendix A of `hrs_handoff_2026-10-11_h8.md`.

## 2. How the capabilities divide

A commissioning pass (user guide, *A commissioning workflow*) does three
things in order:

1. **Choose the data:** which rows are read.
2. **Choose how to look at it:** a view, and the settings of each panel.
3. **Flag it**, then commit.

One loop cuts across all three and is used constantly: **stepping**. You
go to the next baseline (or antenna, SPW or field), look, flag, and step
again. That is AIPS SPFLG, and it is also how *Spectrum* and *Time
series* are meant to be read.

## 3. Proposed layout

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ ⟨  Plot ▶  Reload │ Field 3c279 ◀▶ │ SPW all ◀▶ │ Baseline BR&FD ◀▶ │ One│Side│Over │ PNG ☀ │
├────────────────────────┬─────────────────────────────────────────────────────────┤
│ [ Data ][ View ][ Flag ]│                                                        │
│                        │                     panels                              │
│   (tab contents)       │                                                         │
│                        │                                                         │
├────────────────────────┴─────────────────────────────────────────────────────────┤
│ status line / help for the control under the pointer                             │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Top bar: what you use while looking at plots

- Plot ▶, Reload, the layout buttons (One / Side by Side / Over /
  Under), Export PNG, Light / Dark, the sidebar toggle.
- **Stepping strip.** Field, SPW and Antenna / Baseline, each showing
  what is current, with ◀ ▶. Because it is always visible, you can step
  while the Flag tab is open. The tables in the Data tab still set the
  selection; the strip steps through it.
- The preset buttons leave the top bar (section 3.3).
- Optional: Plot ▶ highlighted when something has been changed but not
  yet plotted. Changes waiting for Plot are what new users trip over.

### 3.2 Left panel: three tabs, in workflow order

**Data** has everything that decides which rows are read:

- data column, field, SPW table, correlations, autocorrelations
- Scan, Time range, UV range
- Antenna and Baseline tables, with their tick-from-text boxes and the
  Either end / Both ends switch

**View** has three parts:

- **Views** (today's presets) as a grouped list (section 3.3).
- **Panel A / Panel B**: the content of today's gear tabs, made
  permanent. The gear icon on a plot opens View with that panel
  selected. This removes the transient-tab machinery and the per-tab Pin
  checkbox.
- **Display**: Pin layout, Time labels, colormap choices that apply to
  both panels.

**Flag** has the same content as the Flagging section today:

- filters (with Preview), Flag reaches, reason
- how pending and flagged data are shown
- undo / redo / clear, Describe pending flags
- export JSON, write to MS, backups

### 3.3 Views as a grouped list

Each view shows its name and a one-line description all the time.
Hovering still gives the longer help in the status area. The groups
follow the commissioning workflow:

| Group | Views | What the group is for |
|---|---|---|
| Survey | Ant×Ant, Z-Score, vplot, φrms·t, φrms·uv | Which antenna, baseline or time is wrong |
| One baseline | Waterfall, φ Waterfall, Spectrum, Time series | Look closely, stepping with ◀ ▶ |
| Interference and band | All-BL, φrms·ν, radplot | Problems shared by many baselines, or across the band |

This scales: a new view is one more row in a group, not another toolbar
button. The grouping also says what each view is for.

## 4. Decisions before building

1. **Stepping strip on the top bar.** This is the main change in
   behaviour. The alternative is to keep ◀ ▶ only in the Data tab, which
   means switching tabs while flagging baseline by baseline.
2. **Panel settings inside View, or a fourth "Panels" tab?** Proposed:
   inside View, since choosing a view and adjusting it are one activity.
3. **Views as a grouped list in View, or a dropdown on the top bar?**
   Proposed: the list, because it teaches what each view is for. A
   dropdown keeps a one-click switch visible at all times.

## 5. Technical notes

- Status-area help comes from `EvHover` wrappers (`_hover`), which work
  anywhere in the layout, so it carries over unchanged.
- **Hidden tabs.** A Select in a tab that has never been opened throws
  when it is set (handoff, practical notes). Presets already wrap their
  gear-tab assignments in `try`; with three permanent tabs this should be
  fixed once, generally, rather than with more `try` blocks.
- **Every figure is laid out twice**, in the Side by Side and the Over /
  Under containers. A toolbar tool therefore has two views, and one click
  runs a `CustomAction` callback once per view. Found 2026-10-11 as a
  blocked second pop-up from "Describe this plot"; that callback now
  ignores a repeat within a second. The gear callback runs twice too but
  is idempotent. A refactor that gives each figure one container removes
  the cause.
- **Code that will move:** `_build_sidebar`, `_build_toolbar`,
  `_build_layout`, the gear-tab code and `gear_click_js`, the preset JS
  (`_preset_js`, `_pair_preset_js`, `_PRESET_SETS`), and the iteration
  rows (`_IterButtons`).
- **Tests that check structure and will need updating:**
  `test_build_layout_visibility`, `test_info_block_integration`,
  `test_checkbox_guard` (finds `_do_plot_js` by AST), and the preset
  tests.
- **Arguments.** The constructor and `visplot()` arguments do not change.
  The refactor only reorganises the GUI.
- **Sizes and a 2 x 2 grid (2026-10-13).** Sizes are already modelled
  on a grid: `row_height` / `column_width` (number, `"fit"`, or one value
  per row / column; `panel_sizes.py`), held in the browser in
  `_size_source` and applied by one function, `window.__cvApplySizes`
  (`_SIZES_JS`), which the layout buttons, presets, Swap and the Height
  menu call. A 2 x 2 layout adds rows and columns to the same arguments
  (`row_height=[X, Y]`). What it still needs is the four-panel grid
  itself: `MAX_PANELS`, two fixed screen positions, and the two
  containers above (one grid container whose children are rearranged
  would replace both). Drag handles between rows / columns, when added,
  should set the same values so the reproducing call keeps working.

## 6. What to ask the testers

- Which views did you use, and in what order? Was anything hard to
  find?
- How often did you step (baseline / antenna / SPW / field), and from
  where?
- Did you open the panel configuration (gear)? What did you change?
- Did Plot ▶ ever not do what you expected, for example a change that
  did not appear until you pressed it?
