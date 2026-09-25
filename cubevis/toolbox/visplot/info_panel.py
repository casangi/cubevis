"""Per-panel info display: cursor readout, legend, colorbar -- and the
gear-tab controls that choose which of them shows, and in what order.

Where this lives and why (2026-09, after three redesigns)
-------------------------------------------------------------
The three widgets are built once, in ``VisibilityPlot._build()``, and stay
in THAT panel's own layout for good -- under its figure, never moved.  An
earlier pass in this series moved them into ``VisibilityPlotter``'s
permanent sidebar instead (one block per (slot, kind)).  Reverted: it
stops scaling past two panels, separates the colorbar/cursor readout from
the plot they describe, and the sidebar isn't wide enough for a
comfortably-read colorbar.

What changed in the second redesign (checkbox list + rotate)
------------------------------------------------------------------
The first under-the-plot version used a Color-key radio (Auto / Legend /
Colorbar / None) plus a Cursor-first / Key-first order radio, wrapped in a
generously-sized (40vh), scrolling container.  Two problems in practice:
an unbounded cursor-readout Div (a real regression -- the pre-existing
design had always capped it) let a long hover readout grow tall enough to
push the page's status bar out of the viewport, and the scrolling
container's own CSS (``overflow`` on a flex item) is suspected of
interacting badly with the flex width-stretch that keeps these Divs
matching the figure's actual width, though this is not confirmed without
a browser.  Both point the same direction: no per-panel scrolling
wrapper, and every widget individually height-bounded (never open-ended)
-- "visible area only" applied at the panel level, not just the page
level.

Replaced with: one ``Checkbox`` labelled "All" plus one ``CheckboxGroup``
(item_keys, e.g. ``["cursor", "legend", "colorbar"]`` -- fewer entries for
a raster, which has no legend) that independently shows or hides each
piece (AND'd with "does it have content" -- an unchecked-but-empty item
was always going to be invisible anyway, and a checked-but-empty one
still doesn't show), plus one small rotate button that cycles display
order by one step per click.  No more mutual exclusivity between legend
and colorbar: the mixed-layer case (one categorical layer, one
continuous) legitimately wants both simultaneously, and independent
checkboxes handle that for free where a radio could not.

How it stays correct with no Bokeh server
------------------------------------------
Python setting ``widget.visible`` does nothing in the browser after the
first paint.  Everything dynamic is therefore driven from the client:

* ``doPlot()``'s response (and the live scaling responses) write
  ``legend.text`` / ``colorbar.text`` on the panel's own Divs directly --
  there is no separate copy to keep in sync.  Those Divs carry a
  ``js_on_change("text", ...)`` hook (``wire_info_display()``) that
  re-runs ``INFO_APPLY_JS``, which derives every ``visible`` from the
  checklist plus "does this Div have content".
* Checking/unchecking an item runs ``INFO_APPLY_JS`` (already wired to
  the checklist's own change), which -- among its other duties --
  recomputes whether "All" should now read as checked and writes that
  directly, only when it actually differs. Clicking "All" itself runs a
  small, separate script that sets the checklist's ``active`` (also only
  when it would actually change). Two SEPARATE listeners each
  unconditionally writing back to the other was the original design,
  and it had a real bug: unchecking one item cascaded into unchecking
  all of them. Even with the redesign, ``INFO_APPLY_JS``'s own write to
  "All" is a real property change and synchronously fires "All"'s own
  listener -- so that listener is guarded (``window.__cvAllCbReflecting``,
  keyed by the widget's own Bokeh model id) against treating a
  reflective update from ``INFO_APPLY_JS`` as if it were the user's own
  click, which would otherwise rebuild the checklist from scratch and
  reintroduce the same bug through a different path. Found via two
  separate rounds of live testing -- this class of bug does not show up
  from testing either script's own logic in isolation, only from wiring
  them together exactly as the real page does.
* The rotate button reads each visible Div's *current* CSS ``order``
  (defaulted at construction to its natural 1, 2, 3.. position) and
  reassigns the next permutation directly -- no separate order model is
  needed; the Divs' own ``styles`` already hold the state.

Python's ``apply_info_defaults`` mirrors the visibility half of the
script for the initial paint and for tests; ``test_info_panel.py`` runs
both over the whole state space and checks they agree.  The rotate
button's logic is JS-only (a client-side interaction with no Python-side
twin to keep in sync, since nothing needs to know the display order at
construction time beyond the natural 1..N default).
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional

from bokeh.layouts import column, row
from bokeh.models import Button, Checkbox, CheckboxGroup, CustomJS, Div

__all__ = [
    "ITEM_LABELS", "ITEM_KEYS", "INFO_APPLY_JS", "ROTATE_JS",
    "colorbar_html",
    "InfoSelectors", "build_info_selectors",
    "wire_info_display", "apply_info_defaults",
    "ITEM_HEIGHTS",
]

#: Checklist entries per kind.  Scatter has a categorical legend, raster
#: never does (no colorize-by-axis), so its checklist is two items, not
#: three -- rather than a permanently-inert "Legend" checkbox that would
#: look broken when checked and nothing appears.
ITEM_LABELS = {
    "scatter": ["Cursor tracking", "Legend", "Colorbar"],
    "raster":  ["Cursor tracking", "Colorbar"],
}
ITEM_KEYS = {
    "scatter": ["cursor", "legend", "colorbar"],
    "raster":  ["cursor", "colorbar"],
}
#: Fixed per-item pixel height (each item's own overflow-y:auto is a
#: small internal scrollbar within THIS box, same pattern the original
#: cursor-readout strip always used -- never an outer, open-ended one).
#: Sized so "All" checked, everything with real content, still totals
#: well under a typical panel's own height budget.
ITEM_HEIGHTS = {"cursor": 60, "legend": 90, "colorbar": 100}


# --------------------------------------------------------------------------- #
# Client-side logic (mirrored in Python for the initial paint / tests)         #
# --------------------------------------------------------------------------- #

#: One-liner, duplicated in both scripts below rather than factored into a
#: shared JS function (this codebase's established pattern for small
#: snippets that need to run identically in more than one CustomJS -- see
#: __cvInstallSelectViewGuard existing in two places): which keys are
#: CURRENTLY relevant. Cursor and colorbar always are; legend only when
#: ``divs.legend`` both exists (a raster's ``divs`` simply has no "legend"
#: key at all -- the raster/scatter distinction falls out of this same
#: rule for free) and has content (a scatter panel that hasn't been
#: colorized -- or was, then switched back to continuous -- has an empty
#: legend Div). This is "(colorbar and tracking for raster), (colorbar and
#: tracking for Continuous), (colorbar, tracking and legend for
#: Categorical)" (2026-09, on request).
_APPLICABLE_JS = (
    "const legend_ok = divs.legend && ((divs.legend.text || '') !== '');\n"
    "const applicable = Object.keys(divs).filter(function(k) { "
    "return k !== 'legend' || legend_ok; });\n"
)

#: Also duplicated: given items.labels' CURRENT order (which the rotate
#: button reorders -- see ROTATE_JS) rather than any fixed key list,
#: recover the SET of currently-checked keys. item_label (a CustomJS arg,
#: {key: display text}) is inverted here rather than passed pre-inverted,
#: since it is tiny and this keeps only one dict needed on the Python side.
_CHECKED_KEYS_JS = (
    "const label_to_key = {};\n"
    "for (const k in item_label) label_to_key[item_label[k]] = k;\n"
    "const checked_keys = new Set(items.active.map(function(i) { "
    "return label_to_key[items.labels[i]]; }));\n"
)

#: The full display-logic script: keeps the checklist's own ROW SET in
#: sync with what is currently applicable (adding "Legend" back the moment
#: it has content, removing it the moment it doesn't -- preserving every
#: surviving item's position and checked state, defaulting a
#: newly-applicable item to checked), then derives each Div's `visible`
#: from "checked AND has content". Runs on every checklist change and
#: every LEGEND/COLORBAR Div's text change (how doPlot()'s and the live
#: scaling responses reach it) -- deliberately NOT the cursor Div's own
#: text (see wire_info_display()): cursor is always applicable and always
#: has content in practice, so reacting to its every-mousemove text update
#: would rerun this whole checklist-rebuild on every hover event for no
#: benefit. Also recomputes and writes ``all_cb.active`` as one of its
#: own side effects (see this module's docstring for why that replaced a
#: separate, bidirectional listener). Free variables (CustomJS args):
#: divs, items, item_label, all_cb. No backslashes in the ACTUAL script
#: text (only in this Python string building it) -- the rest of this
#: app's embedded JS follows the same rule, and
#: test_shipped_js_has_no_backslashes() pins the assembled result, not
#: this source.
INFO_APPLY_JS = _APPLICABLE_JS + _CHECKED_KEYS_JS + """
const was_present = new Set(items.labels.map(function(l) { return label_to_key[l]; }));
const still_here = items.labels.map(function(l) { return label_to_key[l]; })
                                .filter(function(k) { return applicable.includes(k); });
const newly_here = applicable.filter(function(k) { return !was_present.has(k); });
const new_keys = still_here.concat(newly_here);
const changed = (new_keys.length !== items.labels.length) ||
    new_keys.some(function(k, i) { return item_label[k] !== items.labels[i]; });
if (changed) {
    items.labels = new_keys.map(function(k) { return item_label[k]; });
    items.active = new_keys
        .map(function(k, i) { return (checked_keys.has(k) || newly_here.includes(k)) ? i : -1; })
        .filter(function(i) { return i >= 0; });
}

// Give any applicable item that doesn't have an explicit CSS order yet
// one now -- at construction, NOTHING has one, so this assigns the
// natural 1..N; later, when legend first becomes applicable, only IT
// lacks one, so it is appended after whatever already does (at the
// visual bottom, rather than disrupting a user's prior rotation).
const has_order = function(k) { return divs[k] && (divs[k].styles || {})['order'] !== undefined; };
const with_order = new_keys.filter(has_order);
const without_order = new_keys.filter(function(k) { return !has_order(k); });
let next_order = with_order.length
    ? Math.max.apply(null, with_order.map(function(k) { return parseInt(divs[k].styles['order'], 10) || 0; }))
    : 0;
without_order.forEach(function(k) {
    next_order += 1;
    divs[k].styles = Object.assign({}, divs[k].styles, {order: String(next_order)});
});

const now_checked = changed
    ? new Set(items.active.map(function(i) { return label_to_key[items.labels[i]]; }))
    : checked_keys;
for (const key of applicable) {
    const div = divs[key];
    const vis = now_checked.has(key) && ((div.text || '') !== '');
    if (div.visible !== vis) div.visible = vis;
}
for (const key in divs) {
    if (!applicable.includes(key) && divs[key].visible) divs[key].visible = false;
}

// "All" reflects whether every CURRENTLY-applicable item is checked --
// computed and written HERE, as a side effect of the same script that
// already runs on every items.active change, rather than by a SEPARATE
// CustomJS listening on items.active that writes back to all_cb (which
// in turn had its own listener writing back to items.active). That
// two-listener design is what caused a real bug (2026-09, found via
// live testing): unchecking a single item cascaded into unchecking
// ALL of them.
//
// The write below is itself guarded (2026-09, found via FURTHER live
// testing after the fix above): assigning all_cb.active here is a
// real property change, and all_cb's own listener (build_info_
// selectors()'s "All -> items" script) fires synchronously in
// response to ANY change of all_cb.active -- including this
// reflective one -- and would otherwise treat it exactly like a
// user's own click on "All", rebuilding items.active from scratch and
// clobbering the very state this script just correctly computed.
// window.__cvAllCbReflecting (keyed by all_cb.id, a real per-instance
// Bokeh model id) tells that listener "this change came from me, not
// a click" for the brief synchronous duration of this one write.
const want_all = applicable.length > 0 && now_checked.size === applicable.length;
if (all_cb.active !== want_all) {
    window.__cvAllCbReflecting = window.__cvAllCbReflecting || {};
    window.__cvAllCbReflecting[all_cb.id] = true;
    all_cb.active = want_all;
    window.__cvAllCbReflecting[all_cb.id] = false;
}
"""

#: Rotate button: shifts every currently-applicable Div's CSS `order` by
#: one step, wrapping N back to 1 -- "bottom becomes top, top becomes
#: second" -- reading the CURRENT order directly off each Div's own
#: styles (defaulted at construction to its natural 1..N position by
#: apply_info_defaults()), so no separate order model is needed. THEN
#: (2026-09, on request -- the checklist's own row order previously
#: stayed fixed while only the Divs moved) reorders the checklist itself
#: to match, so it always mirrors what is currently on top under the
#: plot. Free variables: divs, items, item_label.
ROTATE_JS = _APPLICABLE_JS + """
const n = applicable.length;
const cur = applicable.map(function(k) { return parseInt((divs[k].styles || {})['order'] || '1', 10) || 1; });
applicable.forEach(function(k, i) {
    const next = (cur[i] % n) + 1;
    divs[k].styles = Object.assign({}, divs[k].styles, {order: String(next)});
});
""" + _CHECKED_KEYS_JS + """
const order_now = applicable.map(function(k) {
    return {key: k, order: parseInt((divs[k].styles || {})['order'] || '1', 10) || 1};
});
order_now.sort(function(a, b) { return a.order - b.order; });
const new_keys = order_now.map(function(x) { return x.key; });
items.labels = new_keys.map(function(k) { return item_label[k]; });
items.active = new_keys
    .map(function(k, i) { return checked_keys.has(k) ? i : -1; })
    .filter(function(i) { return i >= 0; });
"""


# --------------------------------------------------------------------------- #
# Colorbar HTML                                                                #
# --------------------------------------------------------------------------- #

_COLOR_OK = re.compile(r"^(#[0-9a-fA-F]{3,8}|[A-Za-z]{3,20})$")


def _fmt(v: float) -> str:
    try:
        return f"{float(v):.3g}"
    except (TypeError, ValueError):
        return ""


def colorbar_html(bands: Iterable[Any], *, max_stops: int = 24,
                  n_ticks: int = 3) -> str:
    """HTML colorbar(s) for the live info block, from ``ColorBand``s.

    Draws exactly what ``png_export`` would draw for the same bands: the
    bar is coloured by *position* along the colormap and the labels are
    the data values at those positions (``ScalarMapping.ticks``), so a
    non-linear scaling shows its bunched ticks here as it does in an
    exported figure.  Skipped, as there: categorical bands (they get the
    legend), hidden bands, and bands with no ``mapping`` yet.

    Returns ``""`` when there is nothing to draw -- the caller uses that
    to hide the Div rather than show an empty box.

    Text colours are ``inherit`` so the bar follows the Div's own colour
    (set by the dark/light restyle) instead of baking in one theme.
    """
    bands = list(bands)
    # Whether to say WHICH layer a bar belongs to.  Decided from ALL the
    # panel's bands, not just the ones drawn: with one layer categorical
    # (it gets the legend) and one continuous, the single bar left is
    # still one layer's, and an unlabelled "Density" beside a legend
    # would not say which.
    multi = len(bands) > 1
    drawn = [b for b in bands
             if getattr(b, "kind", None) != "categorical"
             and getattr(b, "visible", True)
             and getattr(b, "mapping", None) is not None
             and getattr(b, "cmap", None)]
    if not drawn:
        return ""
    out: List[str] = []
    for b in drawn:
        cmap = [str(c) for c in b.cmap]
        k = max(2, min(max_stops, len(cmap)))
        picks = [cmap[round(i * (len(cmap) - 1) / (k - 1))] for i in range(k)]
        picks = [c for c in picks if _COLOR_OK.match(c)]
        if len(picks) < 2:
            continue
        try:
            vals = [_fmt(v) for v in b.mapping.ticks(n_ticks)]
        except Exception:                       # a mapping we cannot invert
            vals = [_fmt(getattr(b.mapping, "vmin", None)),
                    _fmt(getattr(b.mapping, "vmax", None))]
        label = b.bar_label() if hasattr(b, "bar_label") else str(getattr(b, "label", ""))
        if multi:
            label = f"{getattr(b, 'label', '')}: {label}"
        spans = "".join(f"<span>{_html.escape(v)}</span>" for v in vals)
        out.append(
            "<div style='margin:3px 0 6px 0'>"
            f"<div style='font-size:10px;opacity:0.8;white-space:nowrap;"
            f"overflow:hidden;text-overflow:ellipsis'>{_html.escape(label)}</div>"
            "<div style='height:10px;border-radius:2px;margin:2px 0;"
            f"background:linear-gradient(to right,{','.join(picks)})'></div>"
            "<div style='display:flex;justify-content:space-between;"
            f"font-size:10px;font-family:monospace'>{spans}</div>"
            "</div>"
        )
    return "".join(out)


# --------------------------------------------------------------------------- #
# Widgets                                                                      #
# --------------------------------------------------------------------------- #

@dataclass
class InfoSelectors:
    """The gear-tab widgets that choose what one panel's info area shows."""
    kind: str
    column: Any
    all_cb: Any            # Checkbox, label "All"
    items: Any              # CheckboxGroup -- ROW SET/order change at runtime
                            # (see INFO_APPLY_JS/ROTATE_JS): "Legend" comes
                            # and goes with whether it currently has content,
                            # and rotate reorders the rows to match the Divs.
    rotate_btn: Any
    item_keys: list         # ITEM_KEYS[kind] at CONSTRUCTION time -- the full
                            # possible set for this kind. items.labels/active
                            # at any later moment may be a SUBSET (legend
                            # absent) and/or reordered; item_keys itself never
                            # changes and is not a reliable current-order
                            # source after the first rotation -- item_label
                            # (below) plus items.labels is.
    item_label: dict        # {key: display text}, e.g. {"cursor": "Cursor
                            # tracking", ...} -- the CustomJS scripts invert
                            # this to map items.labels' CURRENT (possibly
                            # rotated/narrowed) order back to keys.

    def widgets(self) -> list:
        """Every widget a dark/light restyle must reach."""
        return [self.all_cb, self.items, self.rotate_btn]


def build_info_selectors(kind: str, *, width: int,
                         stylesheets: Optional[list] = None,
                         icon_stylesheets: Optional[list] = None) -> InfoSelectors:
    """Build the "All" checkbox, item checklist and rotate button for one
    (slot, kind) gear tab.

    *icon_stylesheets*, if given, styles ONLY the rotate button --
    typically the caller's compact icon-button CSS (the same one the
    "\u2715" close button and the iteration prev/next buttons use), so a
    single glyph doesn't sit inside oversized default button padding.
    Falls back to *stylesheets* when omitted.

    "All" is wired here to set items.active on the user's own click; the
    reverse (items -> does "All" now read as checked) is computed by
    ``INFO_APPLY_JS`` itself, as a side effect of the same script that
    already runs on every items.active change -- not by a second,
    separate listener. An earlier two-listener design (each writing back
    to the other) caused a real bug: unchecking one item cascaded into
    unchecking all of them. Even so, INFO_APPLY_JS's write to all_cb is a
    real property change that fires ITS listener (below) synchronously,
    so that listener is guarded against reacting to its OWN reflected
    value as if it were a fresh user click -- see ``window.
    __cvAllCbReflecting`` below and ``INFO_APPLY_JS``'s own comment.
    """
    if kind not in ITEM_LABELS:
        raise ValueError(f"unknown kind {kind!r}")
    ss = list(stylesheets or [])
    ics = list(icon_stylesheets) if icon_stylesheets is not None else ss
    item_keys = list(ITEM_KEYS[kind])
    item_label = dict(zip(item_keys, ITEM_LABELS[kind]))
    n = len(item_keys)

    all_cb = Checkbox(label="All", active=True, stylesheets=list(ss))
    items = CheckboxGroup(labels=list(ITEM_LABELS[kind]), active=list(range(n)),
                          stylesheets=list(ss))
    rotate_btn = Button(label="\u21bb", width=24, height=24,
                        button_type="default", margin=(0, 0, 0, 0),
                        stylesheets=ics)

    # "All" -> items: check/uncheck everything CURRENTLY in the list (which
    # may be narrower than item_keys if legend isn't applicable right now).
    # Only assigns when the result actually differs, so a click that
    # wouldn't change anything doesn't fire items' own change needlessly.
    #
    # Guarded against a real cascade found via live testing (2026-09):
    # INFO_APPLY_JS (wired to items' own change) writes all_cb.active as
    # a REFLECTIVE side effect -- "does this now read as fully checked" --
    # and that write, being a real property change, synchronously fires
    # THIS listener too, which would otherwise treat it exactly like a
    # user's own click on "All" and rebuild items.active from scratch,
    # clobbering whatever partial check/uncheck INFO_APPLY_JS had just
    # correctly computed. window.__cvAllCbReflecting, keyed by all_cb's
    # own Bokeh model id (unique per instance already -- no separate
    # per-panel id needed), is set for the DURATION of that reflective
    # write only (INFO_APPLY_JS sets it immediately before, clears it
    # immediately after, all synchronously), so this listener can tell
    # "all_cb just changed because *I* set it to match reality" from
    # "the user actually clicked it" and only acts on the latter.
    all_cb.js_on_change("active", CustomJS(
        args=dict(all_cb=all_cb, items=items),
        code="""
if (window.__cvAllCbReflecting && window.__cvAllCbReflecting[all_cb.id]) {
    /* INFO_APPLY_JS's own reflective write; not a user click. Skip. */
} else {
    const want = all_cb.active
        ? Array.from({length: items.labels.length}, function(_, i) { return i; })
        : [];
    const same = (items.active.length === want.length) &&
        items.active.every(function(v, i) { return v === want[i]; });
    if (!same) items.active = want;
}
""",
    ))

    col = column(
        Div(text="<span style='color:#89b4fa;font-weight:bold'>"
                 "\u2500\u2500 Info display \u2500\u2500</span>", width=width),
        row(
            column(all_cb, items, width=width - 32),
            rotate_btn,
            styles={"align-items": "flex-start", "gap": "4px"},
        ),
        width=width,
    )
    return InfoSelectors(kind=kind, column=col, all_cb=all_cb, items=items,
                         rotate_btn=rotate_btn, item_keys=item_keys,
                         item_label=item_label)


def wire_info_display(divs: dict, sel: InfoSelectors) -> tuple:
    """Attach the client-side hooks to one panel's own widgets.

    *divs* maps some subset of ``sel.item_keys`` to that panel's actual Div
    (e.g. ``{"cursor": panel._info_div, "colorbar": panel._colorbar_content}``
    for a raster, plus ``"legend"`` for a scatter). Returns the
    ``(visibility_cb, rotate_cb)`` CustomJS pair.
    """
    args = dict(divs=divs, items=sel.items, item_label=sel.item_label,
               all_cb=sel.all_cb)
    visibility_cb = CustomJS(args=args, code=INFO_APPLY_JS)
    sel.items.js_on_change("active", visibility_cb)
    # Legend/colorbar only -- NOT cursor: cursor is always applicable and
    # (in practice) never empty, so its own text changing (every mouse
    # move, via the hover callback) has nothing new for this script to
    # decide; wiring it too would just rerun the whole checklist-rebuild
    # on every hover event for no benefit.
    for key in ("legend", "colorbar"):
        div = divs.get(key)
        if div is not None:
            div.js_on_change("text", visibility_cb)

    rotate_cb = CustomJS(args=dict(divs=divs, items=sel.items,
                                   item_label=sel.item_label), code=ROTATE_JS)
    sel.rotate_btn.js_on_click(rotate_cb)
    return visibility_cb, rotate_cb


def apply_info_defaults(divs: dict, sel: InfoSelectors) -> None:
    """Python twin of ``INFO_APPLY_JS``: narrows ``sel.items`` to whatever
    is applicable right now (mirroring ``_APPLICABLE_JS``), assigns a CSS
    ``order`` to anything that doesn't have one yet (at construction,
    nothing does, so this hands out the natural 1..N; called again later
    it only assigns one to a newly-applicable item, appending it after
    whatever already has one), sets each Div's ``visible``, and updates
    ``sel.all_cb`` to match, exactly as ``INFO_APPLY_JS`` does.

    Reads ``sel.items.labels``' CURRENT order to interpret
    ``sel.items.active`` (not the fixed ``sel.item_keys``), exactly like
    ``INFO_APPLY_JS`` -- so, like that script, this is safe to call more
    than once (a second call after a real Plot press correctly narrows
    or widens an already-narrowed checklist), even though the app itself
    only ever calls it once, at construction; ongoing updates run
    ``INFO_APPLY_JS`` client-side instead.
    """
    legend_div = divs.get("legend")
    legend_ok = bool(legend_div is not None and legend_div.text)
    applicable = [k for k in sel.item_keys if k != "legend" or legend_ok]

    label_to_key = {v: k for k, v in sel.item_label.items()}
    checked_keys = {label_to_key[sel.items.labels[i]] for i in sel.items.active}
    was_present = {label_to_key[l] for l in sel.items.labels}

    still_here = [label_to_key[l] for l in sel.items.labels if label_to_key[l] in applicable]
    newly_here = [k for k in applicable if k not in was_present]
    new_keys = still_here + newly_here

    new_labels = [sel.item_label[k] for k in new_keys]
    new_active = [i for i, k in enumerate(new_keys)
                 if k in checked_keys or k in newly_here]
    if new_labels != list(sel.items.labels):
        sel.items.labels = new_labels
    if new_active != list(sel.items.active):
        sel.items.active = new_active

    def _order_of(div):
        try:
            return int(div.styles.get("order", ""))
        except (TypeError, ValueError):
            return None

    with_order = [k for k in new_keys
                 if divs.get(k) is not None and _order_of(divs[k]) is not None]
    without_order = [k for k in new_keys if k not in with_order]
    next_order = max((_order_of(divs[k]) for k in with_order), default=0)
    for key in without_order:
        next_order += 1
        div = divs.get(key)
        if div is not None:
            div.styles = {**div.styles, "order": str(next_order)}

    # Filtered to new_keys (== applicable, just possibly reordered), NOT
    # checked_keys | newly_here directly -- checked_keys can still
    # contain a key that just became INAPPLICABLE (e.g. "legend" was
    # checked while categorical, then the panel went continuous), which
    # must not count towards "all applicable items are checked" any
    # more than it should keep the (now gone) Div visible.
    now_checked = {new_keys[i] for i in new_active}
    for key in new_keys:
        div = divs.get(key)
        if div is None:
            continue
        div.visible = (key in now_checked) and bool(div.text)
    # Anything not applicable (legend with no content, on a fresh scatter
    # panel) must be explicitly hidden -- Bokeh's own Div default for
    # `visible` is True, and the loop above only ever touches applicable
    # keys, so a Div newly constructed with no explicit `visible=False`
    # would otherwise keep that default indefinitely.
    for key, div in divs.items():
        if key not in applicable and div is not None:
            div.visible = False

    want_all = bool(applicable) and len(now_checked) == len(applicable)
    if sel.all_cb.active != want_all:
        sel.all_cb.active = want_all
