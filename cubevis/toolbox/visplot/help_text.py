"""
help_text.py
============
Status-area help, and the **?** key that opens the documentation at the
entry for it (2026-11-18, reworked 2026-11-19).

The help shown while the pointer is over a control lives in a fixed,
five-line status area at the bottom of the window (``_STATUS_HEIGHT`` in
``visibility_plotter``): fixed so that showing help never moves anything.
The long form of each help is in the documentation, in
``docs/source/applications/visplot_help.md``, not in the code.

Every help text is a *spec*: a plain string, or a tuple

    ``(tracking, label)``

* ``tracking`` -- what the status area shows.  Without a label it must
  fit the area (:data:`TRACKING_BUDGET`, a test checks), since there is
  nowhere else to read the rest.
* ``label`` -- ``None``, or the documentation entry: a MyST target in the
  docs such as ``(vp-flag-reach)=``.  ``tests/manual/visplot/
  test_help_labels.py`` checks every label the code uses is defined there.

**ⓘ marks what has more** (2026-11-19, after trying it: most status
messages have no entry, and a key that silently did nothing over them
was confusing; an entry for every one would mostly repeat its line).
Whatever the status area shows that has an entry carries a ⓘ: at the
bottom right of a help text (over its last line, so it stays visible
when the text is cut off), at the end of a one-line message (``CUE_JS``).
No ⓘ, nothing more.  Clicking the ⓘ opens the entry; for a hover help
the key is the way, since moving the pointer to the ⓘ leaves the control.

**? opens it.**  Pressing **?** anywhere in the
window, except while typing in a field, opens the entry of the first
thing the status area shows that has one, in one browser tab that is
reused:

* the help Div showing (its label rides in the Div's ``tags``,
  :func:`help_tag`), else
* the status row's warning or status text, while the row shows, when it
  holds a :func:`marker` -- so a message from anywhere (a Plot reply, a
  check in the browser, the flagging code) points at its entry by
  including one; :func:`with_selection_label` adds it to a selection-text
  error.

**?**, not a letter: it is the usual help key on the web and clashes
with no navigation scheme, while **h** is "left" in vi-style h/j/k/l keys
and **i** "up" in I/J/K/L (and, twice, interrupts the kernel in a Jupyter
notebook).  **h** worked too for a day (2026-11-18/19) and was removed
rather than kept undocumented.  A key that opens help is kept from the
page around visplot; with nothing marked it is left alone.

The address is ``<docs>/help/<label>.html``: a stub the documentation
build writes for every ``vp-`` label, which forwards to the entry
wherever it is (``docs/source/extensions/vp_help.py``); ``<docs>`` is the
copy for this cubevis' version (``cubevis._docs.base``).  The toolbar's
**Help** button opens ``<docs>/help/index.html``, the top of the page.

Typing: a key typed into a text field, text area or dropdown never opens
help.  Bokeh renders its widgets in shadow DOM, so at document level a
key typed in, say, the Scan box reports a ``<div>`` as its target; the
check therefore walks ``event.composedPath()``.
"""
from __future__ import annotations

import html as _html
import re
from typing import Optional, Tuple, Union

#: Most visible characters a tracking string *without a label* may have.
#: Measured in a browser (headless Chrome, Bokeh 3.10, 2026-11-18): the
#: status area holds five lines of help, but only four while a warning is
#: shown above it (visibility_plotter._SHOW_HINT_JS); 450 characters fit
#: four lines at a 1100-px window, with room for wrapping.
TRACKING_BUDGET = 450

#: Documentation labels for visplot help start with this.
LABEL_PREFIX = "vp-"

HintSpec = Union[str, Tuple[str, Optional[str]]]

_MARKER_RE = re.compile(r'data-cv-help="([^"]+)"')


def split_spec(spec: HintSpec) -> Tuple[str, Optional[str]]:
    """``(tracking, label_or_None)`` from a hint spec."""
    if isinstance(spec, tuple):
        tracking, label = spec
        return tracking, (label or None)
    return spec, None


def visible_text(html_text: str) -> str:
    """*html_text* with tags removed and entities decoded: what the
    reader sees, for length checks."""
    return _html.unescape(re.sub(r"<[^>]+>", "", html_text or ""))


def help_tag(label: Optional[str]) -> list:
    """The ``tags`` a help Div carries: its label, if any, where the key
    handler finds it.  In tags rather than the text, because some help
    texts are filled in later (from the data) and replace it."""
    return [{"cv_help": label}] if label else []


def marker(label: Optional[str]) -> str:
    """An invisible mark that makes a message's documentation entry
    *label*: for the warning and status texts of the status row."""
    return f'<span data-cv-help="{_html.escape(label, quote=True)}"></span>' if label else ""


def label_in(text: Optional[str]) -> Optional[str]:
    """The label of a :func:`marker` in *text*, or ``None``."""
    m = _MARKER_RE.search(text or "")
    return m.group(1) if m else None


#: The selection boxes' error messages (selection_text.SelectionTextError)
#: start with the box's name; the entry each points at.
SELECTION_LABELS = {
    "Scan:": "vp-scan",
    "Time range:": "vp-time",
    "UV range:": "vp-uvrange",
}


def with_selection_label(text: str) -> str:
    """*text* (a selection-text error, possibly after "⚠ ") with the
    :func:`marker` of its box's entry, when it has one."""
    body = (text or "").lstrip("\u26a0 ").lstrip()
    for prefix, label in SELECTION_LABELS.items():
        if body.startswith(prefix):
            return text + marker(label)
    return text


def docs_base() -> str:
    """The documentation copy for this cubevis (``cubevis._docs.base``)."""
    from cubevis._docs import base
    return base()


def help_url(label: Optional[str], base: Optional[str] = None) -> str:
    """Where **?** goes for *label*: ``<base>help/<label>.html``, or the
    help index for ``None``."""
    base = docs_base() if base is None else base
    return f"{base}help/{label or 'index'}.html"


# ---------------------------------------------------------------------------
# Browser side
# ---------------------------------------------------------------------------

# window.__cvHelpLabel(sources, status_row): the label of what the status
# area displays.  sources: the help Divs, then the status row's Divs
# (warning, status).  A help Div counts while visible (one at a time); a
# status-row Div while the row is.
# window.__cvOpenHelp(base, label): open <base>help/<label>.html in the
# reused tab.  Must run inside a user gesture (key press, click), which
# is what lets window.open through pop-up blockers.
# window.__cvHelpCue(div): give *div* its ⓘ when it has an entry (and
# remove a stale one); called whenever a help is shown, a status-row text
# changes, or the area is laid out again.
OPEN_HELP_JS = r"""
window.__cvHelpLabel = window.__cvHelpLabel || function(sources, status_row) {
    const tagLabel = (d) => {
        for (const t of (d.tags || [])) if (t && t.cv_help) return t.cv_help;
        return null;
    };
    const textLabel = (d) => {
        const m = /data-cv-help="([^"]+)"/.exec(d.text || '');
        return m ? m[1] : null;
    };
    for (const d of sources) {
        if (!d || !d.visible) continue;
        const inRow = !!(d.tags || []).find(t => t && t.cv_status_row);
        if (inRow && status_row && !status_row.visible) continue;
        const l = tagLabel(d) || textLabel(d);
        if (l) return l;                  // the first thing shown with an entry
    }
    return null;
};
window.__cvOpenHelp = window.__cvOpenHelp || function(base, label) {
    const url = base + 'help/' + (label || 'index') + '.html';
    let w = null;
    try { w = window.open(url, 'visplot-help'); } catch (e) { console.warn('[visplot] help: window.open failed', e); }
    if (!w) { console.warn('[visplot] help: pop-up blocked:', url); return false; }
    try { w.focus(); } catch (e) {}
    return true;
};
"""

CUE_JS = r"""
window.__cvHelpCue = window.__cvHelpCue || function(div) {
    if (typeof Bokeh === 'undefined' || !Bokeh.index || !div) return;
    const tag = (div.tags || []).find(t => t && t.cv_help);
    const m = /data-cv-help="([^"]+)"/.exec(div.text || '');
    const label = tag ? tag.cv_help : (m ? m[1] : null);
    const inRow = !!(div.tags || []).find(t => t && t.cv_status_row);
    const apply = () => {
        try {
            let view = null;
            const visit = (v) => {
                if (!v || view) return;
                if (v.model === div) { view = v; return; }
                if (v._child_views) for (const c of v._child_views.values()) visit(c);
            };
            for (const e of Object.values(Bokeh.index)) visit(e.model_view || e);
            if (!view || !view.el || !view.shadow_el) return;
            for (const old of view.shadow_el.querySelectorAll('.cv-info')) old.remove();
            if (!label || !div.visible) return;
            const mark = document.createElement('span');
            mark.className = 'cv-info';
            mark.textContent = 'ⓘ';
            mark.title = 'More about this: press ?, or click';
            Object.assign(mark.style, {cursor: 'pointer', fontStyle: 'normal', fontWeight: 'bold',
                                       color: '#89b4fa', padding: '0 4px'});
            mark.addEventListener('click', (e) => {
                e.stopPropagation();
                if (window.__cvOpenHelp && window.__cvHelpBase) window.__cvOpenHelp(window.__cvHelpBase, label);
            });
            if (inRow) {
                // A one-line message: the mark follows its text.
                (view.markup_el || view.shadow_el).appendChild(mark);
                return;
            }
            // A help text, which may be cut off: the mark sits at the
            // area's bottom right, over the last line, while it scrolls.
            const host = view.el;
            const bg = getComputedStyle(host).backgroundColor || '#181825';
            const box = document.createElement('div');
            box.className = 'cv-info';
            Object.assign(box.style, {position: 'sticky', bottom: '0', display: 'flex',
                justifyContent: 'flex-end', pointerEvents: 'none', marginTop: '-1.4em',
                height: '1.4em', alignItems: 'flex-end'});
            mark.className = '';
            Object.assign(mark.style, {pointerEvents: 'auto', paddingLeft: '18px',
                background: 'linear-gradient(to right, transparent, ' + bg + ' 14px)'});
            box.appendChild(mark);
            view.shadow_el.appendChild(box);
        } catch (e) { console.warn('[visplot] help mark failed:', e); }
    };
    // After the visibility / text change has been laid out.
    requestAnimationFrame(() => requestAnimationFrame(apply));
};
"""

# Installed once per page at load.  Args: sources and status_row (as for
# __cvHelpLabel), base (the documentation copy).
# KEY_HANDLER_JS is defined after LEAVE_FIELD_JS, below, which it includes.
_KEY_HANDLER_BODY = r"""
window.__cvHelpBase = base;            // for the marks' clicks (CUE_JS)
if (!window.__cvHelpKeys) {
    window.__cvHelpKeys = true;
    const typing = (e) => {
        const path = e.composedPath ? e.composedPath() : [e.target];
        for (const el of path) {
            if (!el || !el.tagName) continue;
            const t = el.tagName;
            if (t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT' || el.isContentEditable) return true;
        }
        return false;
    };
    document.addEventListener('keydown', (e) => {
        if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
        if (e.key !== '?') return;
        if (typing(e)) return;
        // What the status area shows now -- not merely the last control
        // hovered, and not only help: a warning can carry an entry too.
        const label = window.__cvHelpLabel(sources, status_row);
        if (!label) return;              // nothing marked: the key is left alone
        // Handled: keep it from the page around visplot as well (in a
        // Jupyter notebook, which has its own keyboard shortcuts).
        e.preventDefault();
        e.stopPropagation();
        if (e.stopImmediatePropagation) e.stopImmediatePropagation();
        window.__cvOpenHelp(base, label);
    }, true);
}
// A status-row message already showing at load (a start-up notice).
for (const d of sources)
    if (d && (d.tags || []).some(t => t && t.cv_status_row)) window.__cvHelpCue(d);
"""

# Leaving a text field (2026-11-18).  A field keeps the keyboard focus
# while the pointer moves on: scroll down the sidebar to another control,
# whose help has a ⓘ, press ? -- and the ? goes into the field
# (correctly: it has the focus), which the browser then scrolls back into
# view.  A key typed there cannot safely mean help instead (a reason or a
# file name may contain it), so the field is left at the moments the user
# has plainly moved on:
#   * scrolling (wheel or touch) anywhere but over the field itself;
#   * Esc in the field.
# Leaving commits the field's value exactly as a click elsewhere does.
LEAVE_FIELD_JS = r"""
if (!window.__cvLeaveField) {
    window.__cvLeaveField = true;
    const TEXTUAL = ['', 'text', 'search', 'number', 'password'];
    // The focused element, through open shadow roots.
    const focused = () => {
        let a = document.activeElement;
        while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
        return a;
    };
    const isField = (el) => !!el && ((el.tagName === 'INPUT' &&
        TEXTUAL.indexOf((el.getAttribute('type') || '').toLowerCase()) >= 0) || el.tagName === 'TEXTAREA');
    const onScroll = (e) => {
        const f = focused();
        if (!isField(f)) return;
        const path = e.composedPath ? e.composedPath() : [e.target];
        if (path.indexOf(f) >= 0) return;           // scrolling the field itself
        f.blur();
    };
    document.addEventListener('wheel', onScroll, {capture: true, passive: true});
    document.addEventListener('touchmove', onScroll, {capture: true, passive: true});
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape' || e.defaultPrevented) return;
        const path = e.composedPath ? e.composedPath() : [e.target];
        const el = path[0];
        if (isField(el)) el.blur();
    }, true);
}
"""

KEY_HANDLER_JS = OPEN_HELP_JS + CUE_JS + LEAVE_FIELD_JS + _KEY_HANDLER_BODY

# Toolbar Help button.  Args: base.
HELP_BUTTON_JS = OPEN_HELP_JS + r"""
window.__cvOpenHelp(base, null);
"""
