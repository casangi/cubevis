"""
help_text.py
============
Status-area help with an optional long form (pre-preview feedback,
2026-11-18).

The help shown while the pointer is over a control lives in a fixed,
five-line status area at the bottom of the window (``_STATUS_HEIGHT`` in
``visibility_plotter``): fixed so that showing help never moves anything.
Some texts had grown far past five lines -- "Baselines combined" and
"Flag reaches" to more than 1100 characters -- and could not be read,
since the help follows the pointer and vanishes when it moves to scroll.

Every help text is therefore a *spec*: either a plain string, or a tuple

    ``(tracking, long_html)``

* ``tracking`` -- what the status area shows on hover.  Kept within
  :data:`TRACKING_BUDGET` visible characters (a test checks).  When a
  long form exists it ends with :data:`MORE_CUE`, so the reader knows to
  press a key; when none exists it must not (the same test).
* ``long_html`` -- ``None``, or the HTML of the full explanation.
  Pressing **h** (or **?**) while the help is showing opens it in a help
  tab (one tab, reused), with every other long help after it.  The
  toolbar's **Help** button opens the same page at the top.

The long form is free HTML so that it can grow into real long-form help
(examples, tables, figures) without touching any of the plumbing here.
:func:`points_html` turns the existing " | "-separated hint style into a
heading and a list, which is what the first long forms are.

Keys: a key typed into a text field, text area or dropdown never opens
help.  Bokeh renders its widgets in shadow DOM, so at document level a
key typed in, say, the Scan box reports a ``<div>`` as its target; the
check therefore walks ``event.composedPath()`` (the same reason the
page-wide autocomplete listener does).  ``h`` was preferred to ``?`` (no
Shift); with the path check, both are safe and both are bound.
"""
from __future__ import annotations

import html as _html
import re
from typing import Optional, Tuple, Union

#: The cue a tracking string ends with when a long form exists.
MORE_CUE = "  | <i>press <b>h</b> for more</i>"

#: Most visible characters a tracking string may have.  Measured in a
#: browser (headless Chrome, Bokeh 3.10, 2026-11-18).  The status area
#: holds five lines of help, but only four while a warning is shown above
#: it (visibility_plotter._SHOW_HINT_JS); 450 characters fit four lines at
#: a 1100-px window (texts of 454-516 did not), with room for wrapping.
TRACKING_BUDGET = 450

HintSpec = Union[str, Tuple[str, Optional[str]]]


def split_spec(spec: HintSpec) -> Tuple[str, Optional[str]]:
    """``(tracking, long_html_or_None)`` from a hint spec."""
    if isinstance(spec, tuple):
        tracking, long_html = spec
        return tracking, (long_html or None)
    return spec, None


def visible_text(html_text: str) -> str:
    """*html_text* with tags removed and entities decoded: what the
    reader sees, for length checks."""
    return _html.unescape(re.sub(r"<[^>]+>", "", html_text or ""))


def points_html(text: str) -> str:
    """The " | "-separated hint style as long-form HTML: the first point
    as a lead paragraph, the rest as a list."""
    parts = [p.strip() for p in (text or "").split(" | ") if p.strip()]
    if not parts:
        return ""
    head, rest = parts[0], parts[1:]
    out = f"<p>{head}</p>"
    if rest:
        out += "<ul>" + "".join(f"<li>{p}</li>" for p in rest) + "</ul>"
    return out


def long_tag(long_html: Optional[str]) -> list:
    """The ``tags`` a hint Div carries: the long form, if any, where the
    key handler finds it."""
    return [{"cv_long_help": long_html}] if long_html else []


# ---------------------------------------------------------------------------
# Browser side
# ---------------------------------------------------------------------------

# window.__cvOpenHelp(entries, anchor, light): write the help page into
# the (reused) help tab.  entries: [{id, title, long}], anchor: an entry
# id or null.  Must run inside a user gesture (key press, click), which
# is what lets window.open through pop-up blockers.
OPEN_HELP_JS = r"""
window.__cvOpenHelp = window.__cvOpenHelp || function(entries, anchor, light) {
    const esc = (s) => String(s).replace(/[&<>]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;'}[c]));
    const fg = light ? '#1e293b' : '#cdd6f4', bg = light ? '#f8fafc' : '#1e1e2e';
    const mute = light ? '#475569' : '#a6adc8', acc = light ? '#1d4ed8' : '#89b4fa';
    const hi = light ? '#e0e7ff' : '#313244', rule = light ? '#cbd5e1' : '#45475a';
    // The requested entry first, then the rest in their usual order.
    const order = entries.slice();
    const k = order.findIndex(e => e.id === anchor);
    if (k > 0) order.unshift(order.splice(k, 1)[0]);
    const toc = order.map(e => "<li><a href='#" + e.id + "'>" + esc(e.title) + "</a></li>").join('');
    const body = order.map(e =>
        "<section id='" + e.id + "'" + (e.id === anchor ? " class='cur'" : "") + ">" +
        "<h2>" + esc(e.title) + "</h2>" + e.long + "</section>").join('');
    const page = "<!DOCTYPE html><html><head><meta charset='utf-8'><title>visplot help</title>" +
        "<style>body{font:15px/1.5 system-ui,sans-serif;max-width:52em;margin:2em auto;padding:0 1.5em;" +
        "color:" + fg + ";background:" + bg + "}h1{font-size:1.4em}h2{font-size:1.15em;margin:0 0 .4em}" +
        "section{padding:.8em 1em;margin:0 -1em 1em;border-top:1px solid " + rule + "}" +
        "section.cur{background:" + hi + ";border-radius:6px;border-top-color:transparent}" +
        "a{color:" + acc + "}nav{color:" + mute + ";font-size:.9em}nav ul{columns:2;padding-left:1.2em}" +
        "li{margin:.25em 0}</style></head><body>" +
        "<h1>visplot help</h1><nav><p>Press <b>h</b> over any control whose help ends in " +
        "“press h for more” to come back here at its entry.</p><ul>" + toc + "</ul></nav>" +
        body + "</body></html>";
    let w = null;
    try { w = window.open('', 'visplot-help'); } catch (e) { console.warn('[visplot] help: window.open failed', e); }
    if (!w) { console.warn('[visplot] help: pop-up blocked'); return false; }
    w.document.open(); w.document.write(page); w.document.close();
    try { if (anchor) { const el = w.document.getElementById(anchor); if (el) el.scrollIntoView(); } w.focus(); } catch (e) {}
    return true;
};
"""

# Installed once per page at load.  Args: entries (as for __cvOpenHelp,
# plus ``div``: the hint Div), theme_btn (its ``active`` is "light").
# KEY_HANDLER_JS is defined after LEAVE_FIELD_JS, below, which it includes.
_KEY_HANDLER_BODY = r"""
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
        if (e.key !== 'h' && e.key !== '?') return;
        if (typing(e)) return;
        // The help showing now -- not merely the last control hovered:
        // the pointer may have moved on to a plot since.
        const cur = entries.find(x => x.div && x.div.visible);
        if (!cur) return;
        e.preventDefault();
        window.__cvOpenHelp(entries, cur.id, !!(theme_btn && theme_btn.active));
    }, true);
}
"""

# Leaving a text field (2026-11-18).  A field keeps the keyboard focus
# while the pointer moves on: scroll down the sidebar to another control,
# whose help says "press h for more", press h -- and the h goes into the
# field (correctly: it has the focus), which the browser then scrolls back
# into view.  Typing a letter cannot safely mean help instead (names and
# reasons contain h), so the field is left at the moments the user has
# plainly moved on:
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

KEY_HANDLER_JS = OPEN_HELP_JS + LEAVE_FIELD_JS + _KEY_HANDLER_BODY

# Toolbar Help button.  Same args as KEY_HANDLER_JS.
HELP_BUTTON_JS = OPEN_HELP_JS + r"""
window.__cvOpenHelp(entries, null, !!(theme_btn && theme_btn.active));
"""


def title_of(tracking: str, fallback: str) -> str:
    """The entry's title: the first bold run of its tracking text (every
    hint starts ``<b>Name</b> — ...``), else *fallback*."""
    m = re.search(r"<b>(.*?)</b>", tracking or "")
    return visible_text(m.group(1)).strip() if m else fallback
