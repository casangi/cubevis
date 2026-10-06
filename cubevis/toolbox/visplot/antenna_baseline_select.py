"""Antenna and baseline selection: the logic behind the sidebar's two
checkbox tables (added 2026-10, HRS H3).

What the user sees
------------------
Two scrolling tables in the sidebar's Data section, built like the SPW
table: **Antenna** (one row per antenna) and **Baseline** (one row per
baseline in the data, with the number the Baseline axis of a raster
shows for it).  Each has Prev / Next buttons.

The rule, in one place
----------------------
The ticked rows are the selection.  Nothing else is: the text boxes
under the tables only *set ticks* (see "Text entry" below), so there is
never a second, competing statement of what is selected.

* Nothing ticked in either table: everything.
* Antennas ticked, switch at **Either end** (the default): every
  baseline that has a ticked antenna at either end.  One ticked antenna
  is "that antenna against all the others", which is what Prev / Next on
  the Antenna table steps through.  ``SelectionSpec.antenna_names``;
  CASA's ``antenna='A,B'``.
* Antennas ticked, switch at **Both ends**: only baselines whose two
  antennas are both ticked.  Tick all (the header checkbox), untick one,
  and that antenna is excluded; tick a few and you have a sub-array.
  Sent as the explicit list of those baselines.
* Baselines ticked: exactly those baselines, whatever the Antenna table
  says.  ``SelectionSpec.baselines``, which both backends already give
  precedence over ``antenna_names``.

Two meanings of "these antennas" need the switch because no single rule
serves both: under "both ends" one ticked antenna selects nothing but
its autocorrelation, and under "either end" unticking one antenna out of
many removes almost nothing.

"Nothing ticked means all" differs from the SPW table, where an empty
selection is refused.  It is the right rule here because these are
filters layered on a selection that already exists.  Ticking every
antenna is treated the same as ticking none.

Text entry
----------
Each table (SPW included) has a one-line box that turns a typed string
into ticks -- convenient when there are many rows.  On Enter the string
is interpreted, the table's ticks are *replaced* by the result, and the
box is cleared, so what remains on screen is only the ticks.  If any
part of the string matches nothing, nothing changes and a message names
the part: a half-applied string would be exactly the skew this design
exists to prevent.

Accepted, in every box: names or numbers as the table shows them;
lists separated by ``,`` or ``;``; ``a~b`` for every row from a to b;
a leading ``!`` for "not" (alone: all rows except these; with other
entries: those entries minus these).  In the Baseline box also
``NAME&NAME`` in either order.  In the Antenna box ``!`` also sets the
switch to Both ends, since "not this antenna" only means something
there; anything else sets it to Either end.

The same interpreter exists in Python (``parse_selection_text``) for
the constructor's ``antenna=`` string and in JavaScript
(``PARSE_SELECTION_TEXT_JS``) for the boxes; a test runs both against
the same cases.

Stepping
--------
Antenna Prev / Next ticks exactly one antenna, and clears any ticked
baselines: they would override the antenna just stepped to, and the
button would appear to do nothing.  Baseline Prev / Next ticks exactly
one baseline; when some (not all) antennas are ticked it steps only
through those antennas' baselines -- "the next baseline to this
antenna", the AIPS SPFLG / EDITR way of walking an array -- and
otherwise through every baseline in id order.

Why the baseline list comes from the data
-----------------------------------------
The backends match ``SelectionSpec.baselines`` as *ordered* pairs
against the data's own antenna-name coordinates.  So the pairs offered
here are the data's (``ObservationMetadata.baselines``, collected by
``data/_baseline_meta.py``), and a pair typed in either order
(``antenna="DV01&DA41"``) is looked up and turned into the data's
orientation before it is used.

This module is deliberately free of Bokeh and of the plotter: plain
functions and JavaScript source strings, so all of it is testable
without a browser (the JavaScript under ``node``, as
``test_antenna_iteration.py`` already does for the stepping code).
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Sequence

log = logging.getLogger(__name__)

#: Rows shown before each table scrolls.
ANTENNA_MAX_ROWS = 6
BASELINE_MAX_ROWS = 8


# ---------------------------------------------------------------------------
# Parsing the constructor / task string
# ---------------------------------------------------------------------------

def _antenna_name(token: str, meta) -> Optional[str]:
    """Antenna name for *token* (a name, or a numeric antenna_id), or
    ``None``.  Same matching rule as ``_parse_antenna_string``."""
    token = token.strip()
    for a in meta.antennas:
        if str(a.antenna_id) == token or a.name == token:
            return a.name
    return None


def split_antenna_tokens(antenna_str: Optional[str]) -> list:
    """Tokens of an antenna string, split on ``,`` and ``;``, stripped,
    empties dropped.  CASA separates baseline specifications with ``;``
    and antennas with ``,``; both are accepted for either."""
    if not antenna_str:
        return []
    return [t.strip() for t in re.split(r"[;,]", antenna_str) if t.strip()]


def parse_baseline_string(antenna_str: Optional[str], meta) -> Optional[list]:
    """Baselines named in *antenna_str* as ``A&B`` tokens.

    Returns the pairs in the data's own orientation (see the module
    docstring), in the order given, without duplicates; ``None`` when
    the string names no baseline.  Each side of the ``&`` is one antenna
    name or numeric id.  A token that does not resolve to a baseline
    present in the data is logged and skipped, matching how
    ``_parse_antenna_string`` treats an unknown antenna.

    Only the plain pair form is supported: ``A&B``.  ``A&&B`` is read as
    the same pair; the open-ended forms (``A&``, ``A&&&``) are not
    supported and are skipped with a warning.
    """
    known = {}
    for b in getattr(meta, "baselines", ()) or ():
        known[(b.ant1, b.ant2)] = (b.ant1, b.ant2)
        known.setdefault((b.ant2, b.ant1), (b.ant1, b.ant2))
    out: list = []
    for tok in split_antenna_tokens(antenna_str):
        if "&" not in tok:
            continue
        sides = [s.strip() for s in re.split(r"&+", tok.lstrip("!"))]
        if len(sides) != 2 or not sides[0] or not sides[1] or tok.startswith("!"):
            log.warning("antenna=%r: baseline form %r is not supported "
                        "(use NAME&NAME); ignoring it", antenna_str, tok)
            continue
        n1, n2 = _antenna_name(sides[0], meta), _antenna_name(sides[1], meta)
        pair = known.get((n1, n2)) if n1 and n2 else None
        if pair is None:
            log.warning("antenna=%r: %r matches no baseline in the data; "
                        "ignoring it", antenna_str, tok)
            continue
        if pair not in out:
            out.append(pair)
    return out or None


# ---------------------------------------------------------------------------
# Interpreting typed text (Python twin of PARSE_SELECTION_TEXT_JS)
# ---------------------------------------------------------------------------

def parse_selection_text(text, keys: Sequence[Sequence[str]],
                         pairs: Optional[dict] = None,
                         lenient: bool = False) -> dict:
    """Interpret *text* against a table's rows.

    Parameters
    ----------
    text :
        What was typed (or the constructor's string).
    keys :
        For each row, the strings that name it (an antenna's name and
        number; a baseline's number; a spectral window's id and name).
    pairs :
        Baseline table only: ``{"a1": [...], "a2": [...], "ant_keys":
        [...]}`` -- each row's two antenna names, and the Antenna
        table's ``keys`` so each side of ``A&B`` may be a name or a
        number.
    lenient :
        ``False`` (the text boxes): the first part that matches nothing
        makes the whole result an error.  ``True`` (constructor string):
        such a part is skipped and reported in ``skipped``.

    Returns
    -------
    dict
        ``rows`` (sorted row indices), ``exclude`` (a ``!`` was used),
        ``error`` (message or ``None``), ``skipped`` (lenient only).
        Must stay in step with ``PARSE_SELECTION_TEXT_JS``.
    """
    n = len(keys)

    def rows_for(tok):
        return [i for i in range(n) if tok in keys[i]]

    def ant_name(tok):
        for k in (pairs or {}).get("ant_keys", []):
            if tok in k:
                return k[0]
        return None

    def pair_rows(tok):
        sides = [x.strip() for x in re.split(r"&+", tok)]
        if len(sides) != 2 or not sides[0] or not sides[1]:
            return None
        p, q = ant_name(sides[0]), ant_name(sides[1])
        if p is None or q is None:
            return None
        a1, a2 = pairs["a1"], pairs["a2"]
        out = [i for i in range(n)
               if (a1[i] == p and a2[i] == q) or (a1[i] == q and a2[i] == p)]
        return out or None

    def resolve(tok):
        r = rows_for(tok)
        if r:
            return r
        if pairs is not None and "&" in tok:
            return pair_rows(tok)
        k = tok.find("~")
        if 0 < k < len(tok) - 1:
            lo, hi = rows_for(tok[:k].strip()), rows_for(tok[k + 1:].strip())
            if not lo or not hi:
                return None
            a, b = min(lo[0], hi[0]), max(lo[-1], hi[-1])
            return list(range(a, b + 1))
        return None

    toks = [t.strip() for t in re.split(r"[;,]", "" if text is None else str(text))]
    toks = [t for t in toks if t]
    out = dict(rows=[], exclude=False, error=None, skipped=[])
    if not toks:
        out["error"] = "Nothing entered."
        return out
    inc, exc = set(), set()
    any_inc = any_exc = False
    for raw in toks:
        neg = raw.startswith("!")
        tok = raw[1:].strip() if neg else raw
        r = resolve(tok) if tok else None
        if r is None:
            if lenient:
                out["skipped"].append(raw)
                continue
            out["error"] = f'No match for "{raw}".'
            return out
        if neg:
            any_exc = True
            exc.update(r)
        else:
            any_inc = True
            inc.update(r)
    if lenient and not (any_inc or any_exc):
        out["error"] = "Nothing matched."
        return out
    out["rows"] = [i for i in range(n)
                   if (i in inc if any_inc else True) and i not in exc]
    out["exclude"] = any_exc
    return out


def antenna_keys(meta) -> list:
    """``keys`` for the Antenna table: each antenna's name and number."""
    return [[a.name, str(a.antenna_id)] for a in meta.antennas]


def baseline_keys(meta) -> list:
    """``keys`` for the Baseline table: each baseline's number."""
    return [[str(b.baseline_id)] for b in getattr(meta, "baselines", ()) or ()]


def baseline_pairs(meta) -> dict:
    """``pairs`` for the Baseline table (see ``parse_selection_text``)."""
    bls = list(getattr(meta, "baselines", ()) or ())
    return dict(a1=[b.ant1 for b in bls], a2=[b.ant2 for b in bls],
                ant_keys=antenna_keys(meta))


def spw_keys(meta) -> list:
    """``keys`` for the SPW table: each window's id and, if it has one,
    its name.  Names are not unique on some telescopes; a name then
    ticks every window that has it."""
    out = []
    for s in meta.spws:
        k = [str(s.spw_id)]
        name = str(getattr(s, "name", "") or "")
        if name and name not in k:
            k.append(name)
        out.append(k)
    return out


def pairs_between(antenna_names: Sequence[str], meta) -> list:
    """Baselines whose two antennas are both in *antenna_names* -- the
    "Both ends" rule.  Python twin of the same step in
    ``SELECTION_PAYLOAD_JS``."""
    want = set(antenna_names)
    return [b.pair for b in getattr(meta, "baselines", ()) or ()
            if b.ant1 in want and b.ant2 in want]


def initial_state(antenna_str: Optional[str], meta) -> dict:
    """What the constructor's ``antenna=`` string ticks.

    Returns ``antenna_rows`` / ``baseline_rows`` (rows to tick),
    ``both`` (the switch), and ``antenna_names`` / ``baselines`` -- the
    same two lists the browser would send for those ticks, so the
    selection before the first Plot and after it are computed alike.

    Parts with ``&`` are baselines, the rest antennas.  A part that
    matches nothing is skipped with a warning (lenient, as the
    constructor has always been).  ``!`` on an antenna sets Both ends.
    """
    toks = split_antenna_tokens(antenna_str)
    akeys = antenna_keys(meta)
    flat = {k for ks in akeys for k in ks}
    is_bl = lambda t: "&" in t and t.lstrip("!").strip() not in flat
    a_text = ",".join(t for t in toks if not is_bl(t))
    b_text = ",".join(t for t in toks if is_bl(t))
    a_rows, b_rows, both = [], [], False
    if a_text:
        res = parse_selection_text(a_text, akeys, lenient=True)
        for bad in res["skipped"]:
            log.warning("antenna=%r: %r matches no antenna; ignoring it "
                        "(available: %s)", antenna_str, bad,
                        ", ".join(a.name for a in meta.antennas) or "none")
        if not res["error"]:
            a_rows, both = res["rows"], res["exclude"]
    if b_text:
        res = parse_selection_text(b_text, baseline_keys(meta),
                                   baseline_pairs(meta), lenient=True)
        for bad in res["skipped"]:
            log.warning("antenna=%r: %r matches no baseline in the data; "
                        "ignoring it", antenna_str, bad)
        if not res["error"]:
            b_rows = res["rows"]
    names = [meta.antennas[i].name for i in a_rows]
    bls = list(getattr(meta, "baselines", ()) or ())
    pairs = [bls[i].pair for i in b_rows]
    send_names, send_pairs = names, pairs
    if not pairs and both and 0 < len(names) < len(meta.antennas):
        send_names, send_pairs = [], pairs_between(names, meta)
    return dict(antenna_rows=a_rows, baseline_rows=b_rows, both=both,
                antenna_names=send_names or None, baselines=send_pairs or None)


# ---------------------------------------------------------------------------
# Resolving what is ticked into a selection
# ---------------------------------------------------------------------------

def resolve_antenna_baseline_selection(
    antenna_names_from_string: Optional[Sequence[str]],
    baselines_from_string: Optional[Sequence[tuple]],
    ticked_antennas: Optional[Sequence[str]],
    ticked_baselines: Optional[Sequence[Sequence[str]]],
    meta,
) -> tuple:
    """``(antenna_names, baselines)`` for ``SelectionSpec``.

    *ticked_antennas* / *ticked_baselines* are what the browser sent
    with the last Plot (``None`` = it has sent nothing yet, so the
    constructor's string still decides: the ``*_from_string`` values).
    Applies the rule in the module docstring.  Names and pairs the data
    do not contain are dropped, so a stale or malformed message can only
    select less, never something that matches nothing by accident; if
    that leaves a list empty it counts as "nothing ticked".
    """
    if ticked_baselines is None:
        baselines = list(baselines_from_string) if baselines_from_string else None
    else:
        known = {b.pair for b in getattr(meta, "baselines", ()) or ()}
        baselines = []
        for item in ticked_baselines:
            try:
                pair = (str(item[0]), str(item[1]))
            except (TypeError, IndexError, KeyError):
                continue
            if pair in known and pair not in baselines:
                baselines.append(pair)
        baselines = baselines or None

    if baselines:
        # Ticked baselines are the whole answer.  antenna_names is left
        # out rather than passed along to be ignored: several consumers
        # read "exactly one antenna name" as "antenna iteration is
        # active", which it is not while a baseline is ticked.
        return None, baselines

    if ticked_antennas is None:
        names = list(antenna_names_from_string) if antenna_names_from_string else None
    else:
        all_names = [a.name for a in meta.antennas]
        names = [n for n in all_names if n in {str(t) for t in ticked_antennas}]
        if not names or len(names) == len(all_names):
            names = None
    return names, None


# ---------------------------------------------------------------------------
# Status-bar positions
# ---------------------------------------------------------------------------

def antenna_position(antenna_names: Optional[Sequence[str]], meta) -> Optional[tuple]:
    """1-based ``(position, count)`` when exactly one antenna is selected,
    else ``None``.  Order is ``meta.antennas``' own (the table's)."""
    if not antenna_names or len(antenna_names) != 1:
        return None
    ordered = [a.name for a in meta.antennas]
    if antenna_names[0] not in ordered:
        return None
    return ordered.index(antenna_names[0]) + 1, len(ordered)


def baseline_position(baselines: Optional[Sequence[tuple]], meta) -> Optional[tuple]:
    """1-based ``(position, count)`` when exactly one baseline is
    selected, else ``None``.  Order is ``meta.baselines``' own."""
    if not baselines or len(baselines) != 1:
        return None
    ordered = [b.pair for b in getattr(meta, "baselines", ()) or ()]
    pair = tuple(baselines[0])
    if pair not in ordered:
        return None
    return ordered.index(pair) + 1, len(ordered)


def selection_status(antenna_names: Optional[Sequence[str]],
                     baselines: Optional[Sequence[tuple]], meta,
                     both_ends_antennas: Optional[int] = None) -> str:
    """The antenna / baseline part of the status bar.

    ``"Baseline 67/325: DA44&DV19"``, ``"Baselines: 3 selected"``,
    ``"Antenna 2/26: DA41"``, ``"Antennas: 4 selected"``, or
    ``"Antenna: all"``.  *both_ends_antennas* is the number of ticked
    antennas when the baselines came from the Both ends rule rather
    than from ticked baselines: ``"Antennas: 25 of 26, both ends (300
    baselines)"``.
    """
    if baselines and both_ends_antennas:
        return (f"Antennas: {both_ends_antennas} of {len(meta.antennas)}, "
                f"both ends ({len(baselines)} baselines)")
    if baselines:
        pos = baseline_position(baselines, meta)
        if pos:
            return f"Baseline {pos[0]}/{pos[1]}: {baselines[0][0]}&{baselines[0][1]}"
        return f"Baselines: {len(baselines)} selected"
    pos = antenna_position(antenna_names, meta)
    if pos:
        return f"Antenna {pos[0]}/{pos[1]}: {antenna_names[0]}"
    if antenna_names:
        return f"Antennas: {len(antenna_names)} selected"
    return "Antenna: all"


# ---------------------------------------------------------------------------
# Table contents
# ---------------------------------------------------------------------------

def antenna_table_data(meta) -> dict:
    """ColumnDataSource data for the Antenna table, in ``meta.antennas``
    order.  ``name`` is both what is shown and what is sent."""
    ants = list(meta.antennas)
    return dict(name=[a.name for a in ants],
                ident=[str(a.antenna_id) for a in ants])


def baseline_table_data(meta) -> dict:
    """ColumnDataSource data for the Baseline table, in id order.

    ``ant1`` / ``ant2`` are what is sent (the data's orientation);
    ``name`` and ``ident`` are what is shown.  An autocorrelation is
    marked, since ``DA41&DA41`` is easy to misread in a long list.
    """
    bls = list(getattr(meta, "baselines", ()) or ())
    return dict(
        ant1=[b.ant1 for b in bls],
        ant2=[b.ant2 for b in bls],
        name=[f"{b.name} (auto)" if b.is_auto else b.name for b in bls],
        ident=[str(b.baseline_id) for b in bls],
    )


def preselected_rows(names: Optional[Sequence], column: Sequence) -> list:
    """Row indices of *column* whose value is in *names* (``None`` or
    empty: no rows -- "nothing ticked")."""
    if not names:
        return []
    want = set(names)
    return [i for i, v in enumerate(column) if v in want]


# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------
#
# Source strings spliced into the plotter's CustomJS code.  They refer to
# two names that the plotter puts in every plot-related CustomJS's args:
#   ant_src   the Antenna table's ColumnDataSource
#   bl_src    the Baseline table's ColumnDataSource
# and, for the stepping functions, to stepIterationIndex() from
# iteration_step.STEP_INDEX_JS and to notify_div.

SELECTION_PAYLOAD_JS = """
function cvAntennaBaselineSelection(ant_src, bl_src, both) {
    // The ticked rows of the sidebar's Antenna and Baseline tables, as
    // the plot request carries them: names, and [antenna1, antenna2]
    // pairs in the data's own orientation.  Lists, not a joined string,
    // for the reason spw_ids is a list -- no text round trip to break.
    // Empty means "nothing ticked", which the server reads as "all".
    //
    // `both` is the Antenna table's switch.  With Both ends, and no
    // baseline ticked, the ticked antennas are turned into the explicit
    // list of baselines lying between them HERE, so the server needs no
    // third kind of selection.  `none` reports that this list came out
    // empty (one antenna ticked, no autocorrelations): there is nothing
    // to plot and the caller says so instead of sending.
    function ticked(src) {
        const sel = (src && src.selected && src.selected.indices) || [];
        return Array.from(sel).sort(function(a, b) { return a - b; });
    }
    const names = (ant_src && ant_src.data && ant_src.data['name']) || [];
    const a1 = (bl_src && bl_src.data && bl_src.data['ant1']) || [];
    const a2 = (bl_src && bl_src.data && bl_src.data['ant2']) || [];
    const ants = ticked(ant_src).filter(function(i) { return i < names.length; })
                                .map(function(i) { return names[i]; });
    const bls  = ticked(bl_src).filter(function(i) { return i < a1.length; })
                               .map(function(i) { return [a1[i], a2[i]]; });
    if (!bls.length && both && ants.length > 0 && ants.length < names.length) {
        const want = new Set(ants);
        const between = [];
        for (let i = 0; i < a1.length; i++)
            if (want.has(a1[i]) && want.has(a2[i])) between.push([a1[i], a2[i]]);
        return {antenna_names: [], baselines: between,
                both_ends_antennas: ants.length, none: between.length === 0};
    }
    return {antenna_names: ants, baselines: bls, both_ends_antennas: 0, none: false};
}
"""


PARSE_SELECTION_TEXT_JS = """
function cvParseSelectionText(text, keys, pairs) {
    // Interpret typed text against a table's rows.  JavaScript twin of
    // antenna_baseline_select.parse_selection_text (strict form) -- see
    // that function for the rules; a test runs both on the same cases.
    const n = keys.length;
    function rowsFor(tok) {
        const out = [];
        for (let i = 0; i < n; i++) if (keys[i].indexOf(tok) !== -1) out.push(i);
        return out;
    }
    function antName(tok) {
        const ak = (pairs && pairs.ant_keys) || [];
        for (let i = 0; i < ak.length; i++) if (ak[i].indexOf(tok) !== -1) return ak[i][0];
        return null;
    }
    function pairRows(tok) {
        const sides = tok.split(/&+/).map(function(x) { return x.trim(); });
        if (sides.length !== 2 || !sides[0] || !sides[1]) return null;
        const p = antName(sides[0]), q = antName(sides[1]);
        if (p === null || q === null) return null;
        const out = [];
        for (let i = 0; i < n; i++)
            if ((pairs.a1[i] === p && pairs.a2[i] === q) ||
                (pairs.a1[i] === q && pairs.a2[i] === p)) out.push(i);
        return out.length ? out : null;
    }
    function resolve(tok) {
        const r = rowsFor(tok);
        if (r.length) return r;
        if (pairs && tok.indexOf('&') !== -1) return pairRows(tok);
        const k = tok.indexOf('~');
        if (k > 0 && k < tok.length - 1) {
            const lo = rowsFor(tok.slice(0, k).trim());
            const hi = rowsFor(tok.slice(k + 1).trim());
            if (!lo.length || !hi.length) return null;
            const a = Math.min(lo[0], hi[0]);
            const b = Math.max(lo[lo.length - 1], hi[hi.length - 1]);
            const out = [];
            for (let i = a; i <= b; i++) out.push(i);
            return out;
        }
        return null;
    }
    const toks = String(text === null || text === undefined ? '' : text)
        .split(/[;,]/).map(function(x) { return x.trim(); })
        .filter(function(x) { return x.length > 0; });
    if (!toks.length) return {rows: [], exclude: false, error: 'Nothing entered.'};
    const inc = new Set(), exc = new Set();
    let anyInc = false, anyExc = false;
    for (let t = 0; t < toks.length; t++) {
        const raw = toks[t];
        const neg = raw.charAt(0) === '!';
        const tok = neg ? raw.slice(1).trim() : raw;
        const r = tok ? resolve(tok) : null;
        if (r === null)
            return {rows: [], exclude: false, error: 'No match for "' + raw + '".'};
        if (neg) { anyExc = true; r.forEach(function(i) { exc.add(i); }); }
        else     { anyInc = true; r.forEach(function(i) { inc.add(i); }); }
    }
    const rows = [];
    for (let i = 0; i < n; i++)
        if ((anyInc ? inc.has(i) : true) && !exc.has(i)) rows.push(i);
    return {rows: rows, exclude: anyExc, error: null};
}
"""


APPLY_SELECTION_TEXT_JS = PARSE_SELECTION_TEXT_JS + """
function cvApplySelectionText(input, src, keys, pairs, label, notify, mode_switch, clear_src) {
    // Turn what was typed into ticks: all or nothing.  On success the
    // table's ticks are REPLACED, the box is cleared, and (Antenna
    // table) the switch is set from whether "!" was used.  On any
    // unmatched part nothing changes and the message names the part.
    // Returns the parse result, or null when the box was empty.
    const text = String(input.value === null || input.value === undefined
                        ? '' : input.value).trim();
    if (!text) return null;
    const res = cvParseSelectionText(text, keys, pairs);
    function esc(x) {
        return String(x).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
    if (res.error) {
        if (notify) notify.text = '<b>' + esc(label) + ':</b> ' + esc(res.error)
                                  + ' Ticks unchanged.';
        return res;
    }
    src.selected.indices = res.rows;
    if (mode_switch) mode_switch.active = res.exclude ? 1 : 0;
    // Ticked baselines override the antenna selection: typing antennas
    // while some are ticked would otherwise appear to do nothing.
    if (clear_src && clear_src.selected && (clear_src.selected.indices || []).length)
        clear_src.selected.indices = [];
    if (notify) notify.text = '<b>' + esc(label) + ':</b> ' + res.rows.length
                              + ' ticked. Press Plot to apply.';
    input.value = '';
    return res;
}
"""


NO_AUTOFILL_JS = """
function cvNoAutofill(model) {
    // Ask the browser not to offer its autofill history in this text
    // box.  Bokeh's TextInput has no property for the <input>'s
    // autocomplete attribute, so the element is found through the
    // model's view and the attribute set directly.  The view lookup is
    // not a stable public API (it has differed between Bokeh 3.x
    // releases), so every step is optional and the whole thing is
    // wrapped: if nothing is found, nothing happens and nothing throws.
    // Returns true when the attribute was set.
    try {
        const B = (typeof Bokeh !== 'undefined') ? Bokeh : null;
        const index = B && B.index;
        if (!index || !model) return false;
        let view = null;
        if (typeof index.find_one === 'function') view = index.find_one(model);
        if (!view && typeof index.find_one_by_id === 'function')
            view = index.find_one_by_id(model.id);
        if (!view && typeof index.get_one === 'function') view = index.get_one(model);
        if (!view) return false;
        let el = view.input_el || null;
        if (!el && view.shadow_el && view.shadow_el.querySelector)
            el = view.shadow_el.querySelector('input');
        if (!el && view.el && view.el.querySelector)
            el = view.el.querySelector('input');
        if (!el || typeof el.setAttribute !== 'function') return false;
        el.setAttribute('autocomplete', 'off');
        return true;
    } catch (e) {
        return false;
    }
}
"""


def iterate_antenna_js(guard_js: str) -> str:
    """``function doIterateAntenna(delta)`` -- Antenna Prev / Next.

    *guard_js* is ``_iter_guard_js("names.length", "antenna")`` from the
    plotter (passed in rather than imported: the plotter imports this
    module).  Needs ``STEP_INDEX_JS`` prepended by the caller.
    """
    return """
function doIterateAntenna(delta) {
    // The Antenna table's rows, in meta.antennas' own order -- already
    // on the client, like the SPW table's.
    const names = (ant_src.data && ant_src.data['name']) || [];""" + guard_js + """
    // Only EXACTLY ONE ticked antenna has a position to step from.  No
    // tick, or several, starts fresh at the first antenna -- the rule
    // the free-text antenna box followed before this table replaced it.
    const sel = ant_src.selected.indices || [];
    const cur = sel.length === 1 ? sel[0] : null;
    const idx = stepIterationIndex(cur, names.length, delta, true);
    if (idx === null) return;
    ant_src.selected.indices = [idx];
    // Ticked baselines override the antenna selection, so leaving them
    // ticked would make this button appear to do nothing.
    if (bl_src && bl_src.selected && (bl_src.selected.indices || []).length)
        bl_src.selected.indices = [];
}
"""


def iterate_baseline_js(guard_js: str) -> str:
    """``function doIterateBaseline(delta)`` -- Baseline Prev / Next.

    *guard_js* is ``_iter_guard_js("cand.length", "baseline")``.  Needs
    ``STEP_INDEX_JS`` prepended by the caller.
    """
    return """
function doIterateBaseline(delta) {
    const a1 = (bl_src.data && bl_src.data['ant1']) || [];
    const a2 = (bl_src.data && bl_src.data['ant2']) || [];
    // What to step through: every baseline, in id order -- or, when
    // some but not all antennas are ticked, only the baselines that
    // have a ticked antenna at either end ("the next baseline to this
    // antenna").
    const names = (ant_src && ant_src.data && ant_src.data['name']) || [];
    const asel  = (ant_src && ant_src.selected && ant_src.selected.indices) || [];
    const cand  = [];
    if (asel.length > 0 && asel.length < names.length) {
        const want = new Set(Array.from(asel).map(function(i) { return names[i]; }));
        for (let i = 0; i < a1.length; i++)
            if (want.has(a1[i]) || want.has(a2[i])) cand.push(i);
    } else {
        for (let i = 0; i < a1.length; i++) cand.push(i);
    }""" + guard_js + """
    // Exactly one ticked baseline, and one of the candidates, has a
    // position to step from; anything else starts at the first.
    const sel = bl_src.selected.indices || [];
    const pos = sel.length === 1 ? cand.indexOf(sel[0]) : -1;
    const idx = stepIterationIndex(pos === -1 ? null : pos, cand.length, delta, true);
    if (idx === null) return;
    bl_src.selected.indices = [cand[idx]];
}
"""
