"""
scan_iteration.py
=================
Prev / ✱ / Next for Scan (pre-preview feedback #24, debug-0003).

Testers asked for PlotMS's scan iteration.  Scan stays a text box -- a
table of scans would swamp the sidebar: 30-100 scans are usual, and VLBI
can have many more -- and gets the same three buttons as the other axes,
with a readout under the box while a scan is current:

    ▶ Scan 7 · 3 of 5

**What is stepped through:** the scans the box selects (CASA's syntax,
``cvParseScanText`` from ``selection_text``: ``4, 7~9, >12, !5``), or
every scan when it is empty -- in either case only the scans of the
field shown, when one field is chosen.  Scans are known by their
*names*, as MSv4 has them (``scan_name``, a string; MSv2's scan numbers
arrive the same way): the steppable ones are those whose name is a whole
number, which is what the box's syntax can name.

**The current scan** is held apart from the box, as a table keeps its
current row apart from its ticks (``iteration_subset``): the box keeps
saying which scans are being stepped through, and Plot requests carry
the current scan instead of the box's text (``cvScanPlotText``).  Typing
in the box (or a drill-down from the grid, which writes it) ends the
stepping; so does **Plot ▶**, which plots what the box says, and ✱,
which goes back to all of the box's scans.

**Scans without data are skipped** (issue #37, debug-0004): the reader's
``scans_with_rows`` names the scans with rows in the chosen field, SPWs,
time range, UV range and autocorrelations.  The answer comes with the
first page (``scan_table()["has"]``), with every Plot reply and with the
Antenna / Baseline tables' "has data" refresh when the Data inputs
change (``scan_has_data``), and is kept in ``window.__cvScanHas``.  The
Antenna / Baseline *ticks* are deliberately not part of it, as for those
tables' own flags: a tick change does not ask the reader again (stage,
then plot), so the list cannot go stale between Plots, and it never
hides a scan that has data -- but with only some antennas ticked, a scan
one of them sat out can still plot empty.  ``null`` (the reader cannot
tell) narrows nothing.

Everything is ``var`` or ``function`` at top level: this block is
included in several scripts and twice in some (each stepper's and
doPlot's), where a repeated ``const`` is a SyntaxError
(``iteration_subset``, 2026-11-18).
"""
from __future__ import annotations


def scan_table(scans) -> dict:
    """``{"nos": [int, ...], "fields": {no: [field, ...]}}``: the
    steppable scans in increasing order, and the fields each one
    observed.  *scans* are objects with a scan name (``scan_name``, or
    ``name``) and a ``field_name``: the reader's identity tables
    (``IdentityTables.scans``), which know each scan's field, or the
    metadata's ``ScanInfo``, which may not."""
    fields: dict = {}
    for sc in scans or ():
        name = getattr(sc, "scan_name", None)
        if name is None:
            name = getattr(sc, "name", None)
        try:
            no = int(str(name).strip())
        except (TypeError, ValueError):
            continue
        fields.setdefault(no, set()).add(str(getattr(sc, "field_name", "") or ""))
    nos = sorted(fields)
    return {"nos": nos, "fields": {str(n): sorted(f for f in fields[n] if f) for n in nos}}


# Args: scan_inp, scan_note (the readout Div), scan_tab (scan_table()),
# field_sel, notify_div, scan_marker (help_text.marker("vp-scan")).
# Needs SCAN_TEXT_JS and STEP_INDEX_JS before it.
SCAN_ITER_JS = r"""
window.__cvScanCur = window.__cvScanCur || {scan: null};
// The scans with data (#37): the first page's list until a Plot reply or
// the rows refresh brings a newer one.  Looked up lazily, and scan_tab
// tested with typeof: not every script that includes this block has it.
function cvScanHasNames() {
    if (!window.__cvScanHas) {
        const t = (typeof scan_tab !== 'undefined') ? scan_tab : null;
        window.__cvScanHas = {names: (t && t.has != null) ? t.has : null};
    }
    return window.__cvScanHas.names;
}
function cvScanCandidates() {
    const all = (scan_tab && scan_tab.nos) || [];
    const r = cvParseScanText(scan_inp ? scan_inp.value : '', all);
    if (!r.ok) return {error: r.error};
    let c = r.scans || all.slice();
    const f = field_sel ? String(field_sel.value || '') : '';
    if (f && f.indexOf(',') < 0) {
        const ff = (scan_tab && scan_tab.fields) || {};
        c = c.filter(n => !ff[String(n)] || !ff[String(n)].length || ff[String(n)].indexOf(f) >= 0);
    }
    // Only the scans with data in this selection (#37).
    const has = cvScanHasNames();
    const before = c.length;
    if (has) {
        const hs = new Set(has.map(String));
        c = c.filter(n => hs.has(String(n)));
    }
    return {scans: c, narrowed: c.length < before};
}
function cvScanShow() {
    if (!scan_note) return;
    const cur = window.__cvScanCur.scan;
    if (cur == null) { scan_note.text = ''; scan_note.visible = false; return; }
    const c = cvScanCandidates().scans || [];
    const i = c.indexOf(cur);
    scan_note.text = '▶ Scan ' + cur + (i >= 0 ? ' · ' + (i + 1) + ' of ' + c.length : '');
    scan_note.visible = true;
}
function cvScanEnd() {
    window.__cvScanCur.scan = null;
    cvScanShow();
}
// The scan text a Plot request carries: the current scan while stepping,
// else what the box says.
function cvScanPlotText() {
    const cur = window.__cvScanCur && window.__cvScanCur.scan;
    return cur != null ? String(cur) : (scan_inp ? scan_inp.value : '');
}
function cvScanPos() {
    const cur = window.__cvScanCur.scan;
    if (cur == null) return -1;
    return (cvScanCandidates().scans || []).indexOf(cur);
}
function doIterateScan(delta) {
    const r = cvScanCandidates();
    if (r.error) {
        if (notify_div) notify_div.text = '⚠ ' + r.error + (scan_marker || '');
        return;
    }
    const c = r.scans;
    if (c.length <= 1) {
        if (notify_div) notify_div.text = c.length === 0
            ? (r.narrowed ? '<b>No scans with data in this selection to step through.</b>'
                          : '<b>No scans of this field to step through.</b>')
            : (r.narrowed ? '<b>Only one scan with data in this selection — nothing to step through.</b>'
                          : '<b>Only one scan selected — nothing to step through.</b>');
        return;
    }
    const cur = window.__cvScanCur.scan;
    let pos = cur == null ? null : c.indexOf(cur);
    if (pos === -1) {
        // The current scan is no longer among them (the field or the box
        // changed): continue from where it would sort.
        const after = c.findIndex(n => n > cur);
        pos = delta > 0 ? (after === -1 ? c.length - 1 : after - 1)
                        : (after === -1 ? 0 : after);
        if (pos < 0) pos = c.length - 1;
    }
    const idx = stepIterationIndex(pos, c.length, delta, true);
    if (idx === null) return;
    window.__cvScanCur.scan = c[idx];
    cvScanShow();
}
"""
