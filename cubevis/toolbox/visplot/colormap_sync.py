"""
colormap_sync.py
================
Keep a display's color-scaling controls (histogram, min/max lines and
fields, scaling dropdown) in step with what the display last drew.

Why this exists (pre-preview feedback, 2026-11-18)
--------------------------------------------------
``VisibilityRaster.colormap_controls()`` and
``VisibilityScatter.colormap_controls()`` build their histogram once, when
the sidebar is built, from whatever the display held at that moment.  With
no Bokeh server, nothing Python does afterwards reaches those widgets, so:

* the histogram kept showing the first quantity drawn (usually Amplitude)
  after Plot drew Imaginary, Phase, another SPW, ...;
* Reset moved the lines and fields to the histogram edges *as built*,
  frozen into its CustomJS as constants;
* the per-quantity scaling memory (``scaling_memory.py``) restored, say,
  Phase's -180..180 on the server while the fields still showed the
  Amplitude range.

The fix is a one-row ``ColumnDataSource`` per set of controls (the *sync
source*).  Whatever reply carries a new picture -- the Plot response
(``panels[id].cmap``) and the live ``update_scaling`` reply (``cmap``) --
writes :func:`colormap_state`'s dict into it, and :data:`APPLY_JS`
(attached with ``js_on_change("data", ...)``) moves every control to
match.  Reset reads the edges from the histogram source as it now is,
not from constants.

Threshold scaling
-----------------
``threshold`` uses only a lower cutoff (``vmin``).  :data:`THRESHOLD_JS`
hides the max line and field and retitles min "cutoff" while it is
chosen, so it is clear which value the cutoff is.  It is shared by the
scaling dropdown's own callback and :data:`APPLY_JS`.

Writing a control from here never sends anything back: span ``location``
changes only mirror into the paired field (the comm send is on
``dragging``), ``TextInput.value`` assignments raise no ``ValueSubmit``,
and the dropdown is changed through :func:`quiet_set_js`.

Quiet dropdown changes
----------------------
BokehJS runs ``CustomJS`` callbacks asynchronously (``execute`` awaits the
compiled function), so a flag set around ``select.value = v`` and cleared
in ``finally`` -- the earlier ``window.__cvQuietScaling`` -- is already
clear when the dropdown's own callback runs, and the change was sent back
as a live ``update_scaling`` (seen in headless testing, 2026-11-18).
:func:`quiet_set_js` instead records the expected value under the
dropdown's id in ``window.__cvQuietScalingFor``; :data:`QUIET_GUARD_JS`,
at the top of the dropdown's callback, consumes exactly that one change
whenever it runs.
"""
from __future__ import annotations

from typing import Optional

import numpy as np


def colormap_state(counts, edges, *, scaling: str, alpha, gamma,
                   vmin: Optional[float], vmax: Optional[float]) -> dict:
    """The JSON-able dict :data:`APPLY_JS` applies.

    *counts* / *edges* as ``numpy.histogram`` returns them (empty when
    nothing is drawn: the state then carries no bars and the controls'
    histogram is left as it is).  ``vmin`` / ``vmax`` ``None`` mean
    automatic: the fields then show the histogram's edges.
    """
    counts = np.asarray(counts)
    edges = np.asarray(edges, dtype=float)
    ok = counts.size > 0 and edges.size == counts.size + 1 and np.all(np.isfinite(edges))

    def _num(v):
        if v is None:
            return None
        v = float(v)
        return v if np.isfinite(v) else None

    return {
        "left":    edges[:-1].tolist() if ok else [],
        "right":   edges[1:].tolist() if ok else [],
        "top":     counts.astype(float).tolist() if ok else [],
        "scaling": scaling,
        "alpha":   _num(alpha),
        "gamma":   _num(gamma),
        "vmin":    _num(vmin),
        "vmax":    _num(vmax),
    }


def quiet_set_js(select: str, value: str) -> str:
    """JS setting dropdown *select* to *value* (both JS expressions)
    without its callback sending the change (see the module docstring)."""
    return (f"{{ const _qs = {select}, _qv = {value};"
            f" if (_qs && _qs.value !== _qv) {{"
            f" window.__cvQuietScalingFor = window.__cvQuietScalingFor || {{}};"
            f" window.__cvQuietScalingFor[_qs.id] = _qv; _qs.value = _qv; }} }}")


# Top of a scaling dropdown's callback, after ``const s = cb_obj.value``
# and the purely visual updates: a change made by quiet_set_js ends here.
QUIET_GUARD_JS = """
{
    const _q = window.__cvQuietScalingFor;
    if (_q && _q[cb_obj.id] === s) { delete _q[cb_obj.id]; return; }
}
"""


def sync_source():
    """A new, empty sync source (one per set of controls)."""
    from bokeh.models import ColumnDataSource
    return ColumnDataSource(data={"state": [None]})


# Needs ``min_span``, ``max_span``, ``min_input``, ``max_input`` in
# scope and a scaling name in ``s``.
THRESHOLD_JS = """
{
    const _t = (s === 'threshold');
    max_span.visible  = !_t;
    max_input.visible = !_t;
    min_input.title   = _t ? 'cutoff' : 'min';
}
"""

# CustomJS body for ``sync.js_on_change("data", ...)``.  Args:
# hist_source, min_span, max_span, min_input, max_input, scaling_select,
# alpha_input, gamma_input, equation, equations.
APPLY_JS = """
const st = ((cb_obj.data && cb_obj.data['state']) || [null])[0];
if (!st) return;
if (st.left && st.left.length) {
    hist_source.data = {left: st.left, right: st.right, top: st.top,
                        bottom: st.top.map(() => 0)};
}
const L = hist_source.data['left'] || [], R = hist_source.data['right'] || [];
const lo = L.length ? L[0] : 0.0;
const hi = R.length ? R[R.length - 1] : 1.0;
const vmin = (st.vmin == null) ? lo : st.vmin;
const vmax = (st.vmax == null) ? hi : st.vmax;
const fmt = v => String(parseFloat(Number(v).toPrecision(6)));
// The spans' location callbacks mirror into the fields in the same
// format (FMT6_JS), whichever order the asynchronous callbacks run in.
min_span.location = vmin;
max_span.location = vmax;
min_input.value = fmt(vmin);
max_input.value = fmt(vmax);
if (st.alpha != null) alpha_input.value = fmt(st.alpha);
if (st.gamma != null) gamma_input.value = fmt(st.gamma);
const s = st.scaling || scaling_select.value;
// Show the scaling the display uses without sending it back.
if (st.scaling) """ + quiet_set_js("scaling_select", "st.scaling") + """
alpha_input.visible = (s === 'log' || s === 'power');
gamma_input.visible = (s === 'gamma');
equation.text = equations[s] || s;
""" + THRESHOLD_JS

# Reset: the edges of the histogram as it is now.  Args: hist_source,
# min_span, max_span, min_input, max_input.  Leaves ``lo`` / ``hi``
# defined for the caller's code that follows.
RESET_EDGES_JS = """
const _L = hist_source.data['left'] || [], _R = hist_source.data['right'] || [];
const lo = _L.length ? _L[0] : 0.0;
const hi = _R.length ? _R[_R.length - 1] : 1.0;
min_span.location = lo;
max_span.location = hi;
min_input.value = String(parseFloat(Number(lo).toPrecision(6)));
max_input.value = String(parseFloat(Number(hi).toPrecision(6)));
"""

# Appended to a live update_scaling reply handler (``resp`` in scope,
# ``cmap_sync`` an arg): a reply that carries the state applies it.
APPLY_REPLY_JS = """
if (resp && resp.cmap && cmap_sync) { cmap_sync.data = {state: [resp.cmap]}; }
"""

# How every control here writes a number into a field: six significant
# digits, no trailing zeros ("227", "0.25", "1.41421").  The span
# location mirrors use it too, so a field never flips between "227" and
# "227.000000" depending on which asynchronous callback ran last.
FMT6_JS = "String(parseFloat(Number(%s).toPrecision(6)))"
