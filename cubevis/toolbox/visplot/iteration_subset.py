"""
iteration_subset.py
===================
Prev / Next through the ticked rows of the SPW, Antenna and Baseline
tables (pre-preview feedback, 2026-11-18).

Before, a step narrowed the ticks to the one row it moved to.  Ticking
SPWs 2, 3 and 4 and pressing Next therefore lost the 2-3-4 choice at the
first press, and later presses walked every SPW.

The rule now (agreed 2026-11-18).  In every case the row shown is the
axis's *current row*, kept in the browser (``window.__cvIterCur[axis]``)
and marked ▶ in the table (the ``iter_cur`` column, drawn by the name
column's formatter); the table scrolls to keep it in view, and Plot
requests carry it.  Rows with no data in the selection are skipped.

==================================  ==========================  ===============
Ticked                              Prev / Next step through    The ticks
==================================  ==========================  ===============
two or more                         the ticked rows             unchanged
one                                 every row with data         move with the ▶
none (Antenna, Baseline: "all")     every row with data         stay empty
none (SPW: refused at Plot)         every row                   move with the ▶
==================================  ==========================  ===============

So the ticks are always the selection, and the ▶ always what is shown;
with one tick the two simply coincide.  (Antenna and Baseline keep their
"none ticked = all" convention; for Baseline, "all" is the baselines of
the antenna selection, which the stepper passes in as its candidates.)

**Iteration ends** -- the current row is dropped -- when the ticks change
(a click, the text box, ✱, a drill-down from the grid), and when Plot ▶
is pressed: Plot ▶ always plots what is ticked.  Reload keeps it.  A
stepper's own tick change (the one-tick case) is marked beforehand with
``cvIterSetTicks``, so the ticks listener lets it through: callbacks run
asynchronously, so a flag set and cleared around the change would not do
(the colormap_sync "quiet" lesson).

Several axes can be mid-iteration at once (stepping SPWs while sitting
on antenna 3 of 5), each with its own marker.  As before, a current
Antenna row sets aside ticked baselines, which would otherwise override
it, and a current Baseline row wins over the antennas.

``cvIterSubsetStep`` needs ``stepIterationIndex`` (iteration_step's
``STEP_INDEX_JS``) in the same script, as every stepper already has.
"""
from __future__ import annotations

#: The current-row helpers.  Prepended to the steppers and to doPlot().
ITER_SUBSET_JS = r"""
window.__cvIterCur = window.__cvIterCur || {};
// Set (row) or clear (null) an axis's current row, and its ▶ marker.
function cvIterMark(src, axis, row) {
    window.__cvIterCur[axis] = (row == null ? null : row);
    if (!src || !src.data) return;
    const first = Object.keys(src.data)[0];
    const n = first ? (src.data[first] || []).length : 0;
    const col = new Array(n).fill(0);
    if (row != null && row >= 0 && row < n) col[row] = 1;
    const had = src.data['iter_cur'];
    if (!had || had.length !== n || had.some((v, i) => v !== col[i])) {
        src.data['iter_cur'] = col;
        src.change.emit();
    }
    if (row != null) cvIterScrollTo(src, row);
}
// Scroll every table showing `src` so `row` (a data row) is in view: the
// ▶ is no use below the table's fold (2026-11-18).  After the redraw the
// change above causes; a sorted table's rows are mapped through its
// data provider's index.
function cvIterScrollTo(src, row) {
    if (typeof Bokeh === 'undefined' || !Bokeh.index) return;
    setTimeout(function() {
        try {
            const visit = (v) => {
                if (!v) return;
                if (v.model && v.model.type === 'DataTable' && v.model.source === src && v.grid) {
                    let r = row;
                    try {
                        const ix = v.data && v.data.index;
                        if (ix && ix.indexOf) { const k = ix.indexOf(row); if (k >= 0) r = k; }
                    } catch (e) {}
                    v.grid.scrollRowIntoView(r, false);
                }
                if (v._child_views) for (const c of v._child_views.values()) visit(c);
            };
            for (const e of Object.values(Bokeh.index)) visit(e.model_view || e);
        } catch (e) { console.warn('[visplot] scrolling to the current row failed:', e); }
    }, 0);
}
// Axes whose "none ticked" means all rows (SPW's is refused at Plot).
// `var`, not `const`: this block is included more than once in a script
// (each stepper's and doPlot's), and only var / function may repeat.
var CV_ITER_NONE_IS_ALL = {antenna: true, baseline: true};
// The axis's current row, or null when it is not mid-iteration.  It
// counts while it agrees with the ticks: one of two or more ticked, the
// one ticked, or anything when none is ticked on an "all" axis.
function cvIterCurrent(src, axis) {
    const r = window.__cvIterCur[axis];
    if (r == null || !src || !src.selected) return null;
    const t = Array.from(src.selected.indices || []);
    if (t.length >= 2) return t.indexOf(r) >= 0 ? r : null;
    if (t.length === 1) return t[0] === r ? r : null;
    return CV_ITER_NONE_IS_ALL[axis] ? r : null;
}
// Change an axis's ticks from a stepper, without ending its iteration:
// the ticks listener (END_ON_TICK_CHANGE_JS) lets exactly this change
// through, whenever it runs.
function cvIterSetTicks(src, axis, indices) {
    window.__cvIterOwnTick = window.__cvIterOwnTick || {};
    window.__cvIterOwnTick[axis] = JSON.stringify(indices);
    src.selected.indices = indices;
}
// Prev / Next for an axis; `cand` are the rows it may step through when
// fewer than two are ticked (rows with data; for Baseline, those of the
// antenna selection).  Moves the ▶ -- and with one tick, the tick too.
// Returns true when it has handled the press (always, unless there is
// nothing at all to step through).
function cvIterSubsetStep(src, axis, cand, delta) {
    const t = Array.from((src && src.selected && src.selected.indices) || []);
    const many = t.length >= 2;
    const set = (many ? t.filter(i => cand.indexOf(i) >= 0) : cand.slice()).sort((a, b) => a - b);
    if (set.length === 0) return true;          // nothing with data
    const cur = cvIterCurrent(src, axis);
    let pos = null;
    if (cur != null && set.indexOf(cur) >= 0) {
        pos = set.indexOf(cur);
    } else if (t.length === 1) {
        // From the ticked row; a ticked row with no data steps to its
        // neighbour with data (Next lands on the one after, Prev before).
        pos = set.indexOf(t[0]);
        if (pos === -1) {
            let k = 0;
            while (k < set.length && set[k] < t[0]) k++;
            pos = delta > 0 ? k - 1 : k;
            if (pos < 0) pos = set.length - 1;
            if (pos >= set.length) pos = 0;
        }
    }
    const idx = stepIterationIndex(pos, set.length, delta, true);
    if (idx === null) return true;
    const row = set[idx];
    if (!many && (t.length === 1 || !CV_ITER_NONE_IS_ALL[axis]))
        cvIterSetTicks(src, axis, [row]);         // one tick: it moves with the ▶
    cvIterMark(src, axis, row);
    return true;
}
// The rows a Plot request carries: the current row, else the ticks.
function cvIterEffective(src, axis) {
    const r = cvIterCurrent(src, axis);
    return r == null ? Array.from((src && src.selected && src.selected.indices) || []) : [r];
}
// The Antenna / Baseline pair as cvAntennaBaselineSelection should see
// them: a current Baseline row is the selection; a current Antenna row
// is, with ticked baselines set aside (they would override it).
function cvIterAntBl(ant_src, bl_src) {
    const view = (src, idx) => ({data: src.data, selected: {indices: idx}});
    const b = cvIterCurrent(bl_src, 'baseline');
    if (b != null) return [view(ant_src, []), view(bl_src, [b])];
    const a = cvIterCurrent(ant_src, 'antenna');
    if (a != null) return [view(ant_src, [a]), view(bl_src, [])];
    return [ant_src, bl_src];
}
function cvIterClearAll(spw_src, ant_src, bl_src) {
    cvIterMark(spw_src, 'spw', null);
    cvIterMark(ant_src, 'antenna', null);
    cvIterMark(bl_src, 'baseline', null);
}
"""

#: On a table source's ``selected.indices`` change (args: src, axis): a
#: change of the ticks ends the axis's iteration -- unless it is the
#: stepper's own (cvIterSetTicks), which it lets through once.
END_ON_TICK_CHANGE_JS = ITER_SUBSET_JS + r"""
const _own = window.__cvIterOwnTick || {};
const _now = JSON.stringify(Array.from(src.selected.indices || []));
if (_own[axis] === _now) { delete _own[axis]; return; }
cvIterMark(src, axis, null);
"""

#: The ▶ marker, for a name column's HTMLTemplateFormatter.  Guarded:
#: the column exists only once an axis has been iterated.
MARKER_TEMPLATE = ("<% if (typeof iter_cur !== 'undefined' && iter_cur === 1) { %>"
                   "<b style=\"color:#f9e2af\">▶ </b><% } %>")
