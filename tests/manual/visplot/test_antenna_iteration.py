"""
test_antenna_iteration.py
===========================
Tests for antenna iteration (I-3, 2026-09) -- the Prev/Next mechanism
mirroring Field/SPW's own (see ``_IterButtons``'s docstring, which
explicitly names Antenna as the next expected axis), built on top of
making the previously-unwired Antenna filter (``visibility_plotter.py``'s
own constructor docstring: "Stored; not yet wired in preview") actually
populate ``SelectionSpec.antenna_names``.

Location in repository:
    cubevis/tests/manual/visplot/test_antenna_iteration.py

Run:
    pytest cubevis/tests/manual/visplot/test_antenna_iteration.py -v

Sections
--------
1. _parse_antenna_string      the MSSelection-subset parser, in isolation
2. _antenna_iteration_position  the status-bar position lookup
3. doIterateAntenna JS         the real, shipped stepping logic, run
                              under node -- not a transcription of it
                              (mirrors test_iteration_step.py's own
                              stated principle for JS parity tests)

Scope note: this covers the parsing/position/stepping logic only.
Nothing here constructs a live VisibilityPlotter (sidebar/toolbar
widgets, the message round-trip, _status_text's rendered HTML) --
that needs a real backend connection this file doesn't have, the same
boundary test_iteration_step.py itself draws around STEP_INDEX_JS/
step_index rather than the full Prev/Next button wiring.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from cubevis.toolbox.visplot.iteration_step import STEP_INDEX_JS
from cubevis.toolbox.visplot.reduction_context import AntennaInfo, ObservationMetadata
from cubevis.toolbox.visplot.visibility_plotter import (
    _antenna_iteration_position,
    _iter_guard_js,
    _parse_antenna_string,
)


def _meta_with_antennas(*names_in_order: str) -> ObservationMetadata:
    """A minimal ObservationMetadata with antennas in the given order,
    antenna_id assigned by position -- antenna_id order is what
    iteration follows (see _antenna_iteration_position's own docstring
    for why this must NOT be alphabetical), so tests deliberately pass
    names in a non-alphabetical order to catch a regression to sorting.
    """
    antennas = tuple(
        AntennaInfo(antenna_id=i, name=n) for i, n in enumerate(names_in_order)
    )
    return ObservationMetadata(
        fields=(), spws=(), antennas=antennas, scans=(),
        data_columns=("DATA",), time_range=(0.0, 1.0), freq_range_hz=(0.0, 1.0),
        n_baselines=0,
    )


# ---------------------------------------------------------------------------
# 1. _parse_antenna_string
# ---------------------------------------------------------------------------

class TestParseAntennaString:
    def test_empty_or_none_means_all(self):
        meta = _meta_with_antennas("DV03", "DA41")
        assert _parse_antenna_string("", meta) is None
        assert _parse_antenna_string("   ", meta) is None
        assert _parse_antenna_string(None, meta) is None

    def test_single_antenna_by_name(self):
        meta = _meta_with_antennas("DV03", "DA41")
        assert _parse_antenna_string("DA41", meta) == ["DA41"]

    def test_single_antenna_by_id(self):
        meta = _meta_with_antennas("DV03", "DA41", "DV01")
        assert _parse_antenna_string("2", meta) == ["DV01"]

    def test_comma_separated_list_preserves_order(self):
        meta = _meta_with_antennas("DV03", "DA41", "DV01")
        assert _parse_antenna_string("DA41, DV01", meta) == ["DA41", "DV01"]

    def test_exclusion_returns_the_complement(self):
        meta = _meta_with_antennas("DV03", "DA41", "DV01", "DA42")
        result = _parse_antenna_string("!DA42", meta)
        assert set(result) == {"DV03", "DA41", "DV01"}

    def test_mixed_include_and_exclude_uses_only_the_exclusions(self):
        meta = _meta_with_antennas("DV03", "DA41", "DV01", "DA42")
        result = _parse_antenna_string("DA41,!DA42", meta)
        assert set(result) == {"DV03", "DA41", "DV01"}

    def test_wholly_unmatched_string_falls_back_to_all(self):
        """Mirrors _parse_spw_string's own `result or all_ids` fallback
        for the same reason: a string that matched nothing should not
        silently plot zero baselines."""
        meta = _meta_with_antennas("DV03", "DA41")
        assert _parse_antenna_string("NOTREAL", meta) is None

    def test_partially_unmatched_string_keeps_the_matches(self):
        meta = _meta_with_antennas("DV03", "DA41")
        assert _parse_antenna_string("DA41,NOTREAL", meta) == ["DA41"]

    def test_ampersand_baseline_pair_syntax_not_supported(self):
        """Deliberate scope boundary (see this parser's own docstring):
        '&' is not a recognized token separator, so a specific-baseline
        string resolves as a single unmatched (or accidentally
        substring-like) token, never as a baseline pair -- confirmed
        directly so a future reader doesn't assume this "just works"
        from the hint text's own example."""
        meta = _meta_with_antennas("DV03", "DA41")
        # Neither "DA41&DV01" nor any substring of it matches an antenna
        # name or id exactly, so the whole string is unmatched and falls
        # back to "all" -- not a baseline-pair selection.
        assert _parse_antenna_string("DA41&DV01", meta) is None


# ---------------------------------------------------------------------------
# 2. _antenna_iteration_position
# ---------------------------------------------------------------------------

class TestAntennaIterationPosition:
    def test_position_follows_antenna_id_order_not_alphabetical(self):
        """DA41 sorts before DV03 alphabetically, but is listed SECOND
        in antenna_id order here -- the position must reflect the
        dataset's own order, matching Field/SPW's own convention."""
        meta = _meta_with_antennas("DV03", "DA41", "DV01")
        assert _antenna_iteration_position("DA41", meta) == (2, 3)
        assert _antenna_iteration_position("DV03", meta) == (1, 3)
        assert _antenna_iteration_position("DV01", meta) == (3, 3)

    def test_multi_antenna_selection_has_no_position(self):
        meta = _meta_with_antennas("DV03", "DA41")
        assert _antenna_iteration_position("DV03,DA41", meta) is None

    def test_empty_selection_has_no_position(self):
        meta = _meta_with_antennas("DV03", "DA41")
        assert _antenna_iteration_position("", meta) is None

    def test_exclusion_has_no_position(self):
        """!DA41 resolves to every OTHER antenna -- a multi-antenna
        result even with only one exclusion token, so no position."""
        meta = _meta_with_antennas("DV03", "DA41", "DV01")
        assert _antenna_iteration_position("!DA41", meta) is None

    def test_unmatched_name_has_no_position(self):
        meta = _meta_with_antennas("DV03", "DA41")
        assert _antenna_iteration_position("NOTREAL", meta) is None


# ---------------------------------------------------------------------------
# 3. doIterateAntenna -- the real, shipped JS stepping logic
# ---------------------------------------------------------------------------

def _build_shipped_antenna_js() -> str:
    """The EXACT string _build_toolbar() ships for doIterateAntenna.

    Since 2026-10 the Antenna control is a checkbox table, not a text
    box, and the function body lives in ``antenna_baseline_select`` --
    so this is now the shipped builder itself rather than a
    reconstruction of its template, called the way _build_toolbar()
    calls it.
    """
    from cubevis.toolbox.visplot.antenna_baseline_select import iterate_antenna_js
    return STEP_INDEX_JS + iterate_antenna_js(
        _iter_guard_js("names.length", "antenna"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
class TestDoIterateAntennaJS:
    @staticmethod
    def _run(antenna_names: list, start_value: str, delta: int) -> tuple:
        """Runs doIterateAntenna(delta) against a mock Antenna table
        whose ticked rows are the antennas named in start_value (comma-
        separated; "" = none), returns (final_value, notify_text) with
        final_value the ticked antenna names joined the same way -- the
        shape these tests were written against when the control was a
        text box."""
        start = [antenna_names.index(n.strip())
                 for n in start_value.split(",") if n.strip() in antenna_names]
        script = _build_shipped_antenna_js() + f"""
const names = {json.dumps(antenna_names)};
let ant_src = {{ data: {{ name: names }}, selected: {{ indices: {json.dumps(start)} }} }};
let bl_src = {{ data: {{ ant1: [], ant2: [] }}, selected: {{ indices: [] }} }};
let notify_div = {{ text: "" }};
doIterateAntenna({delta});
process.stdout.write(JSON.stringify(
    [ant_src.selected.indices.map(i => names[i]).join(","), notify_div.text]));
"""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "harness.js"
            path.write_text(script)
            res = subprocess.run(["node", str(path)],
                                 capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stderr
        value, notify = json.loads(res.stdout)
        return value, notify

    def test_empty_starts_at_first_antenna(self):
        value, _ = self._run(["DV03", "DA41", "DV01"], "", 1)
        assert value == "DV03"

    def test_steps_forward(self):
        value, _ = self._run(["DV03", "DA41", "DV01"], "DA41", 1)
        assert value == "DV01"

    def test_steps_backward(self):
        value, _ = self._run(["DV03", "DA41", "DV01"], "DA41", -1)
        assert value == "DV03"

    def test_wraps_forward_past_the_end(self):
        value, _ = self._run(["DV03", "DA41", "DV01"], "DV01", 1)
        assert value == "DV03"

    def test_wraps_backward_past_the_start(self):
        value, _ = self._run(["DV03", "DA41", "DV01"], "DV03", -1)
        assert value == "DV01"

    def test_ambiguous_current_value_restarts_at_first_antenna(self):
        """A multi-name string, an exclusion, or anything else that
        doesn't literally equal one antenna name (indexOf returns -1)
        is treated the same as "nothing selected" -- lands on the
        first antenna, the same rule Field's own sentinel follows."""
        for start in ("DA41,DV01", "!DA41", "NOTREAL"):
            value, _ = self._run(["DV03", "DA41", "DV01"], start, 1)
            assert value == "DV03", f"start={start!r} got {value!r}"

    def test_zero_antennas_shows_notify_and_does_not_change_value(self):
        value, notify = self._run([], "", 1)
        assert value == ""
        assert "No antennas to iterate" in notify

    def test_single_antenna_shows_notify_and_does_not_change_value(self):
        value, notify = self._run(["DA41"], "", 1)
        assert value == ""
        assert "Only one antenna" in notify


# ---------------------------------------------------------------------------
# 4. Regression guard: antenna_names must gate axes_changed
# ---------------------------------------------------------------------------

class TestAntennaNamesGatesAxesChanged:
    """A critical bug found directly (not by any existing test) while
    verifying this feature end-to-end: _handle_plot()'s axes_changed
    condition -- the sole gate deciding whether a panel re-queries at
    all -- checked field_names/spw/correlation/data_column but not
    antenna_names, for BOTH the raster and scatter branches. Without
    this, pressing Antenna's Prev/Next would update the text field but
    the actual plot (and Slice 2's own per-antenna readout, which only
    ever refreshes inside query_columns) would never re-query at all --
    the feature would appear to do nothing.

    A full behavioral test needs a live panel with _render_lock/
    update_axes/etc. that this sandbox doesn't have (the same boundary
    every other _handle_plot()-adjacent test in this file draws) -- this
    is a structural guard instead: directly inspects the real source for
    both axes_changed blocks and asserts antenna_names is compared in
    each, so a future edit that drops it (e.g. refactoring one of these
    blocks without noticing the other, or a new SelectionSpec field
    added elsewhere without threading this same check through) fails a
    test immediately rather than shipping a UI control that silently
    does nothing.
    """

    @staticmethod
    def _handle_plot_source() -> str:
        import inspect
        import cubevis.toolbox.visplot.visibility_plotter as vp_mod
        return inspect.getsource(vp_mod.VisibilityPlotter._handle_plot)

    def test_raster_axes_changed_checks_antenna_names(self):
        src = self._handle_plot_source()
        start = src.index("axes_changed = (\n                    did_reload or\n                    slot.id in switched_kind_this_round or\n                    y   != panel._y_dim")
        # Not src.index(")", start): the block contains nested
        # getattr(...) calls whose own closing parens would end the
        # slice early. "try:" immediately follows both blocks' real
        # closing paren in the actual source (confirmed directly).
        end = src.index("\n                try:", start)
        block = src[start:end]
        assert "antenna_names" in block, (
            "raster's axes_changed no longer compares antenna_names -- "
            "Prev/Next on the Antenna field would silently stop "
            "re-querying raster panels"
        )

    def test_scatter_axes_changed_checks_antenna_names(self):
        src = self._handle_plot_source()
        start = src.index("axes_changed = (\n                    did_reload or\n                    slot.id in switched_kind_this_round or\n                    never_rendered")
        end = src.index("\n                try:", start)
        block = src[start:end]
        assert "antenna_names" in block, (
            "scatter's axes_changed no longer compares antenna_names -- "
            "Prev/Next on the Antenna field would silently stop "
            "re-querying scatter panels, and Slice 2's own per-antenna "
            "readout would never refresh either"
        )
