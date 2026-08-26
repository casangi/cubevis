"""
iteration_step.py
==================
Pure index arithmetic for stepping through an iteration axis (Field, SPW —
I-1, Phase 2.5 — and, per the kickoff's explicit forward-compatibility
note, future axes and future grid-mode pagination).

Why this module exists
-----------------------
Duo mode's "Animate: Field | SPW" Prev/Next buttons compute the next index
client-side, in JavaScript — a server round trip is not needed just to
decide *what* the next value is, only to actually re-render once it is
chosen, and that re-render already goes through the existing ``doPlot()``
/ ``_handle_plot()`` path (see ``visibility_plotter.py``). That leaves the
same "one algorithm, two runtimes" problem ``tick_format.py`` already
solved for axis tick labels, so this module follows the same shape:
``_JS_CORE`` is the single copy of the algorithm as JavaScript;
``step_index`` is the Python mirror; ``test_iteration_step.py`` runs both
against ``GOLDEN_CASES`` under ``node`` and fails if they diverge.

``step_index`` has no caller in ``visibility_plotter.py`` at run time —
I-1's Prev/Next buttons embed ``STEP_INDEX_JS`` directly and never round-
trip through Python to compute an index. It exists in Python, tested
standalone, so the algorithm has one precise and directly testable
definition, and so a *server-side* stepper can call it directly later
rather than re-deriving it from the JS:

* Grid-mode pagination (implementation plan, Phase 4 punch-list item X-1)
  turns a page by a stride equal to the number of visible cells —
  ``delta=page_size`` instead of duo mode's ``delta=1``. X-1's own notes
  describe page turns as re-selecting through the existing
  ``update_axes()`` path (server-side), unlike I-1's client-only step —
  so grid mode is the expected first Python caller of this function.
* A future headless/scripted iteration entry point (extending E-1's
  generator API) would run entirely in Python, with no browser to compute
  the next index in the first place.

Both prospective call sites pass ``current_index=None`` for "nothing
selected yet / the sentinel is active" rather than ``-1`` — see
``step_index``'s docstring for why, and for the wrap/clamp behaviour.

Package location
----------------
``cubevis/cubevis/toolbox/visplot/iteration_step.py``
"""

from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# The algorithm, as JavaScript — the single copy
# ---------------------------------------------------------------------------
#
# Operates on four variables the caller must already have in scope:
# ``current_index`` (number or null), ``count`` (number), ``delta``
# (number, possibly negative, possibly |delta| > 1 for a future grid-mode
# page stride), ``wrap`` (bool). Returns a number or null. Kept free of
# Bokeh references so the test harness can wrap it in a plain function and
# run it under node; STEP_INDEX_JS below adds the named-function wrapper
# visibility_plotter.py's Prev/Next CustomJS embeds directly.
_JS_CORE = r"""
if (!count || count <= 0) {
    // Nothing to iterate over -- e.g. metadata with zero fields. Callers
    // must treat null as "no-op", not as index 0.
    return null;
}
if (current_index === null || current_index === undefined) {
    // No current selection (duo mode's "All fields" sentinel, or SPW's
    // DataTable with nothing checked). Both Prev and Next land on the
    // first item -- simpler and less surprising than making Prev wrap
    // "backwards" from a position that was never really -1, and it is
    // the one rule that needs no wrap/clamp branch of its own.
    return 0;
}
// Defensive normalisation: a caller-supplied index outside [0, count) is
// folded back in before stepping, rather than propagating an
// out-of-range result.
const idx = ((current_index % count) + count) % count;
if (wrap) {
    return ((idx + delta) % count + count) % count;
}
const next = idx + delta;
if (next < 0) return 0;
if (next > count - 1) return count - 1;
return next;
"""

# Named-function wrapper embedded directly in visibility_plotter.py's
# Prev/Next CustomJS (see _build_toolbar's ``_iterate_js``).
STEP_INDEX_JS = """
function stepIterationIndex(current_index, count, delta, wrap) {
""" + _JS_CORE + """
}
"""


# ---------------------------------------------------------------------------
# The algorithm, as Python
# ---------------------------------------------------------------------------

def step_index(current_index: Optional[int], count: int, delta: int,
               wrap: bool = True) -> Optional[int]:
    """Compute the index Prev/Next (or a future stepper) should land on.

    Parameters
    ----------
    current_index : int or None
        The currently-selected position in ``[0, count)``, or ``None`` if
        nothing is currently selected (duo mode's "All fields" sentinel,
        or an SPW table with no row checked).
    count : int
        Number of steppable items -- e.g. ``len(meta.fields)``, already
        excluding the Field ``Select``'s ``("", "All fields")`` sentinel
        entry, which is not itself a steppable item. See
        ``_field_iteration_position`` in ``visibility_plotter.py`` for
        where that exclusion happens on the Python side, and
        ``_iterate_js``'s ``field_sel.options.slice(1)`` for the
        equivalent on the client.
    delta : int
        Step size and direction. Duo mode's Prev/Next always pass ``-1``
        / ``+1``; a future grid-mode page turn would pass ``±page_size``
        (see module docstring) -- any nonzero integer is valid, and
        ``abs(delta) > count`` is handled the same as any other value
        (it wraps/clamps like a smaller delta would, just further).
    wrap : bool
        ``True`` (the default, and what I-1's shipped UI uses): stepping
        past either end wraps to the other. ``False``: stepping past
        either end clamps to that end instead. Both are implemented and
        tested even though only wrap-around is currently reachable from
        the GUI (the kickoff calls for picking one shipped behaviour,
        not for leaving the other unbuilt) -- a later mode may want the
        other, and grid-mode paging in particular may prefer clamping so
        a page turn cannot silently jump back to page 1.

    Returns
    -------
    int or None
        The new index, or ``None`` if ``count <= 0`` -- nothing to
        iterate over at all, distinct from "nothing selected yet", which
        is the ``current_index=None`` input case and always returns 0.
    """
    if not count or count <= 0:
        return None
    if current_index is None:
        return 0
    idx = current_index % count
    if wrap:
        return (idx + delta) % count
    nxt = idx + delta
    if nxt < 0:
        return 0
    if nxt > count - 1:
        return count - 1
    return nxt


# ---------------------------------------------------------------------------
# Golden table
# ---------------------------------------------------------------------------
#
# ``(current_index, count, delta, wrap, expected)``. Asserted against the
# Python implementation and, under node, against _JS_CORE.
GOLDEN_CASES: tuple[tuple[Optional[int], int, int, bool, Optional[int]], ...] = (
    # --- sentinel / nothing selected -------------------------------------
    (None, 7, 1,  True,  0),   # Next from "All fields" -> first field
    (None, 7, -1, True,  0),   # Prev from "All fields" -> also first field
    (None, 7, 1,  False, 0),
    (None, 7, -1, False, 0),
    (None, 0, 1,  True,  None),  # nothing selected AND nothing to select

    # --- wrap-around (I-1's shipped behaviour) ----------------------------
    (0, 7, 1,  True, 1),
    (3, 7, 1,  True, 4),
    (3, 7, -1, True, 2),
    (6, 7, 1,  True, 0),   # forward past the last item wraps to the first
    (0, 7, -1, True, 6),   # backward past the first item wraps to the last
    (0, 1, 1,  True, 0),   # a single item wraps to itself
    (0, 1, -1, True, 0),

    # --- stride > 1 (future grid-mode page turn; see module docstring) ----
    (2, 4, 5,   True, 3),
    (0, 5, -7,  True, 3),

    # --- clamp (not reachable from the shipped GUI; kept for future use) --
    (0, 7, 1,   False, 1),
    (6, 7, 1,   False, 6),   # clamps at the last item
    (0, 7, -1,  False, 0),   # clamps at the first item
    (3, 7, -10, False, 0),
    (3, 7, 10,  False, 6),

    # --- nothing to iterate over -------------------------------------------
    (0, 0, 1, True, None),
)


def check_golden() -> list[str]:
    """Return a list of mismatch descriptions; empty means all pass.

    Exposed as a function rather than living only in the test module so
    it can be called from a REPL or a smoke check without pytest.
    """
    bad = []
    for current_index, count, delta, wrap, want in GOLDEN_CASES:
        got = step_index(current_index, count, delta, wrap)
        if got != want:
            bad.append(
                f"step_index({current_index!r}, {count!r}, {delta!r}, "
                f"wrap={wrap!r}) -> {got!r}, expected {want!r}"
            )
    return bad
