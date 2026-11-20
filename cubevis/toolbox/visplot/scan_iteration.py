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

**Not done here**, deliberately: skipping scans that have no data for the
chosen SPWs, antennas or time range (the Antenna / Baseline tables' "has
data" refresh asks the reader; a per-scan answer needs a reader method,
which changes the remote kernel's fingerprint -- a separate step).  Such
a scan plots empty, and the next step moves on.

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
    return {scans: c};
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
            ? '<b>No scans of this field to step through.</b>'
            : '<b>Only one scan selected — nothing to step through.</b>';
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
