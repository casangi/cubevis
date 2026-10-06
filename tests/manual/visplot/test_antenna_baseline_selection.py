"""
test_antenna_baseline_selection.py
====================================
The sidebar's Antenna and Baseline selection tables (2026-10, HRS H3):
the selection rule, the "tick from text" interpreter, the shipped
JavaScript, the baseline metadata, and the whole path through a real
``VisibilityPlotter`` on both backends.

Location in repository:
    cubevis/tests/manual/visplot/test_antenna_baseline_selection.py

Run:
    pytest cubevis/tests/manual/visplot/test_antenna_baseline_selection.py -v

Sections
--------
1. parse_selection_text         the interpreter, in Python
2. Python / JavaScript parity   the same cases through the shipped JS (node)
3. cvApplySelectionText         all-or-nothing, box cleared, switch set (node)
4. cvAntennaBaselineSelection   what a Plot sends, incl. Both ends (node)
5. doIterateAntenna / Baseline  the shipped stepping functions (node)
6. initial_state, resolve, status   constructor string -> ticks -> selection
7. Baseline metadata            collect_baselines, the metadata contract
8. Real backends                one baseline selects one baseline (sim MS/PS)
9. Real plotter                 messages in, selection and re-query out

Sections 1-7 need no data.  8-9 build a small simulated MSv2 with
xarray-ms's simulator (and its MSv4 zarr twin) and are skipped without
it.  What is NOT covered: anything that only happens in a browser --
that the tables render, that a tick shows, that typing fires the box's
change event.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import warnings
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from cubevis.toolbox.visplot import antenna_baseline_select as abs_
from cubevis.toolbox.visplot.iteration_step import STEP_INDEX_JS
from cubevis.toolbox.visplot.reduction_context import (
    AntennaInfo, BaselineInfo, ObservationMetadata,
)
from cubevis.toolbox.visplot.visibility_plotter import _iter_guard_js

warnings.filterwarnings("ignore")
needs_node = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not on PATH")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _meta(names=("DV03", "DA41", "DV01", "DA42"), autos=False):
    """Antennas in the given (deliberately non-alphabetical) order and
    every baseline between them, lower index first."""
    ants = tuple(AntennaInfo(antenna_id=i, name=n) for i, n in enumerate(names))
    bls, k = [], 0
    for i in range(len(names)):
        for j in range(i if autos else i + 1, len(names)):
            bls.append(BaselineInfo(baseline_id=k, ant1=names[i], ant2=names[j]))
            k += 1
    return ObservationMetadata(
        fields=(), spws=(), antennas=ants, scans=(), data_columns=("DATA",),
        time_range=(0.0, 1.0), freq_range_hz=(0.0, 1.0),
        n_baselines=len(bls), baselines=tuple(bls))


def _node(script: str):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "harness.js"
        path.write_text(script)
        res = subprocess.run(["node", str(path)], capture_output=True,
                             text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


M = _meta()
AK = abs_.antenna_keys(M)
BK = abs_.baseline_keys(M)
BP = abs_.baseline_pairs(M)
# Baselines of M, by row: 0 DV03&DA41, 1 DV03&DV01, 2 DV03&DA42,
#                         3 DA41&DV01, 4 DA41&DA42, 5 DV01&DA42


# ---------------------------------------------------------------------------
# 1. The interpreter
# ---------------------------------------------------------------------------

class TestParse:

    def _rows(self, text, keys=AK, pairs=None):
        r = abs_.parse_selection_text(text, keys, pairs)
        assert r["error"] is None, r["error"]
        return r["rows"], r["exclude"]

    def test_name_and_number(self):
        assert self._rows("DA41") == ([1], False)
        assert self._rows("1") == ([1], False)
        assert self._rows(" DV01 , 0 ") == ([0, 2], False)

    def test_semicolon_separates_too(self):
        assert self._rows("DA41;DA42") == ([1, 3], False)

    def test_range_is_rows_between_in_table_order(self):
        # DV03..DV01 are rows 0..2: table order, not alphabetical.
        assert self._rows("DV03~DV01") == ([0, 1, 2], False)
        assert self._rows("1~3") == ([1, 2, 3], False)
        assert self._rows("3~1") == ([1, 2, 3], False)      # either direction

    def test_exclusion_alone_means_all_but(self):
        assert self._rows("!DA42") == ([0, 1, 2], True)
        assert self._rows("!0,!3") == ([1, 2], True)

    def test_exclusion_with_inclusions_subtracts(self):
        assert self._rows("0~3,!DA41") == ([0, 2, 3], True)

    def test_result_is_sorted_and_unique(self):
        assert self._rows("3,0,DA42,0~1") == ([0, 1, 3], False)

    @pytest.mark.parametrize("text", ["nope", "DA41,nope", "0~9", "!", "!nope",
                                      "DA41~", "~DA41", "DA41&DV01"])
    def test_any_unmatched_part_is_an_error(self, text):
        r = abs_.parse_selection_text(text, AK)
        assert r["error"] and r["rows"] == []

    @pytest.mark.parametrize("text", ["", "   ", " , ; ", None])
    def test_nothing_entered(self, text):
        assert abs_.parse_selection_text(text, AK)["error"] == "Nothing entered."

    def test_error_names_the_part(self):
        assert '"nope"' in abs_.parse_selection_text("DA41,nope", AK)["error"]

    def test_baseline_pair_either_order_names_or_numbers(self):
        assert self._rows("DA41&DV01", BK, BP) == ([3], False)
        assert self._rows("DV01&DA41", BK, BP) == ([3], False)
        assert self._rows("1&2", BK, BP) == ([3], False)
        assert self._rows("DA41&&DV01", BK, BP) == ([3], False)

    def test_baseline_numbers_ranges_exclusion(self):
        assert self._rows("0,4~5", BK, BP) == ([0, 4, 5], False)
        assert self._rows("!2", BK, BP) == ([0, 1, 3, 4, 5], True)
        assert self._rows("DV03&DA41, 5", BK, BP) == ([0, 5], False)

    @pytest.mark.parametrize("text", ["DA41&", "&DA41", "DA41&nope", "DA41&DA41",
                                      "DA41&DV01&DA42", "DA41"])
    def test_baseline_unmatched(self, text):
        assert abs_.parse_selection_text(text, BK, BP)["error"]

    def test_duplicate_names_tick_every_match(self):
        keys = [["0", "Subband:0"], ["1", "Subband:1"], ["8", "Subband:0"]]
        assert self._rows("Subband:0", keys) == ([0, 2], False)
        assert self._rows("8", keys) == ([2], False)

    def test_exact_name_beats_range_and_pair_syntax(self):
        keys = [["A~B"], ["A"], ["B"], ["C&D"]]
        assert self._rows("A~B", keys) == ([0], False)
        assert self._rows("C&D", keys, dict(a1=[], a2=[], ant_keys=[])) == ([3], False)

    def test_lenient_skips_and_reports(self):
        r = abs_.parse_selection_text("DA41,nope", AK, lenient=True)
        assert r["rows"] == [1] and r["skipped"] == ["nope"] and not r["error"]
        r = abs_.parse_selection_text("nope", AK, lenient=True)
        assert r["error"] and r["rows"] == []        # not "everything"

    def test_spw_keys(self):
        from types import SimpleNamespace as NS
        meta = NS(spws=[NS(spw_id=0, name="X"), NS(spw_id=5, name=""),
                        NS(spw_id="A", name="A")])
        assert abs_.spw_keys(meta) == [["0", "X"], ["5"], ["A"]]


# ---------------------------------------------------------------------------
# 2. Python / JavaScript parity
# ---------------------------------------------------------------------------

PARITY_CASES = [
    ("DA41", AK, None), ("1,3", AK, None), ("DV03~DV01", AK, None),
    ("3~1", AK, None), ("!DA42", AK, None), ("0~3,!DA41", AK, None),
    ("nope", AK, None), ("DA41;2~0", AK, None), ("", AK, None),
    (" , ", AK, None), ("0~9", AK, None), ("!", AK, None), ("DA41~", AK, None),
    ("DA41&DV01", AK, None), ("  DA41  ,,DA42 ", AK, None),
    ("DV01&DA41", BK, BP), ("1&2, 0", BK, BP), ("!1", BK, BP),
    ("DV03&nope", BK, BP), ("DA41&DA41", BK, BP), ("4~5;DV03&DA41", BK, BP),
    ("DA41&&DV01", BK, BP), ("DA41&DV01&DA42", BK, BP),
    ("Subband:0", [["0", "Subband:0"], ["1", "S1"], ["8", "Subband:0"]], None),
]


@needs_node
def test_js_interpreter_matches_python():
    js = _node(abs_.PARSE_SELECTION_TEXT_JS
               + "\nconst cases = " + json.dumps(PARITY_CASES) + ";\n"
               + "process.stdout.write(JSON.stringify(cases.map("
               + "c => cvParseSelectionText(c[0], c[1], c[2]))));")
    for (text, keys, pairs), got in zip(PARITY_CASES, js):
        want = abs_.parse_selection_text(text, keys, pairs)
        assert (got["rows"], got["exclude"], got["error"]) == \
            (want["rows"], want["exclude"], want["error"]), text


# ---------------------------------------------------------------------------
# 3. cvApplySelectionText
# ---------------------------------------------------------------------------

@needs_node
class TestApplyTextJS:

    @staticmethod
    def _run(text, keys=AK, pairs=None, start=(2,), mode=0, other=(4,),
             with_mode=True, with_clear=True):
        return _node(abs_.APPLY_SELECTION_TEXT_JS + f"""
const input = {{value: {json.dumps(text)}}};
const src = {{selected: {{indices: {json.dumps(list(start))}}}}};
const notify = {{text: ''}};
const mode_switch = {'{active: ' + str(mode) + '}' if with_mode else 'null'};
const clear_src = {'{selected: {indices: ' + json.dumps(list(other)) + '}}' if with_clear else 'null'};
const res = cvApplySelectionText(input, src, {json.dumps(keys)},
                                 {json.dumps(pairs)}, 'Antenna', notify,
                                 mode_switch, clear_src);
process.stdout.write(JSON.stringify({{
    value: input.value, rows: src.selected.indices, notify: notify.text,
    mode: mode_switch ? mode_switch.active : null,
    other: clear_src ? clear_src.selected.indices : null,
    returned: res === null ? null : res.error}}));
""")

    def test_success_replaces_ticks_and_clears_the_box(self):
        r = self._run("DA41,DA42")
        assert r["rows"] == [1, 3] and r["value"] == ""
        assert "2 ticked" in r["notify"]

    def test_error_changes_nothing(self):
        r = self._run("DA41,nope", start=(2,), mode=1, other=(4,))
        assert r["rows"] == [2]              # ticks kept
        assert r["value"] == "DA41,nope"     # text kept, to be corrected
        assert r["mode"] == 1 and r["other"] == [4]
        assert "nope" in r["notify"] and "unchanged" in r["notify"]

    def test_empty_box_does_nothing(self):
        r = self._run("   ", start=(2,))
        assert r["rows"] == [2] and r["returned"] is None and r["notify"] == ""

    def test_exclusion_sets_both_ends_inclusion_sets_either(self):
        assert self._run("!DA42", mode=0)["mode"] == 1
        assert self._run("DA41", mode=1)["mode"] == 0

    def test_antenna_text_clears_ticked_baselines(self):
        assert self._run("DA41", other=(4, 5))["other"] == []

    def test_works_without_switch_or_other_table(self):
        r = self._run("0~1", with_mode=False, with_clear=False)
        assert r["rows"] == [0, 1] and r["mode"] is None

    def test_message_is_html_escaped(self):
        r = self._run("<b>x</b>")
        assert "<b>x</b>" not in r["notify"] and "&lt;b&gt;" in r["notify"]


# ---------------------------------------------------------------------------
# 4. cvAntennaBaselineSelection
# ---------------------------------------------------------------------------

def _payload_js(ant_sel, bl_sel, both, meta=M):
    return _node(abs_.SELECTION_PAYLOAD_JS + f"""
const ant_src = {{data: {json.dumps(abs_.antenna_table_data(meta))},
                 selected: {{indices: {json.dumps(list(ant_sel))}}}}};
const bl_src = {{data: {json.dumps(abs_.baseline_table_data(meta))},
                selected: {{indices: {json.dumps(list(bl_sel))}}}}};
process.stdout.write(JSON.stringify(
    cvAntennaBaselineSelection(ant_src, bl_src, {json.dumps(bool(both))})));
""")


@needs_node
class TestPayloadJS:

    def test_nothing_ticked(self):
        r = _payload_js([], [], False)
        assert r["antenna_names"] == [] and r["baselines"] == [] and not r["none"]

    def test_antennas_either_end(self):
        r = _payload_js([3, 1], [], False)
        assert r["antenna_names"] == ["DA41", "DA42"] and r["baselines"] == []

    def test_ticked_baselines_in_data_orientation(self):
        r = _payload_js([0], [5, 3], False)
        assert r["baselines"] == [["DA41", "DV01"], ["DV01", "DA42"]]
        assert r["antenna_names"] == ["DV03"]      # sent; the server ignores it

    def test_both_ends_becomes_the_baselines_between(self):
        r = _payload_js([0, 1, 2], [], True)       # all but DA42
        assert r["antenna_names"] == []
        assert r["baselines"] == [["DV03", "DA41"], ["DV03", "DV01"], ["DA41", "DV01"]]
        assert r["both_ends_antennas"] == 3 and not r["none"]

    def test_both_ends_matches_python_twin(self):
        r = _payload_js([0, 2, 3], [], True)
        assert [tuple(p) for p in r["baselines"]] == \
            abs_.pairs_between(["DV03", "DV01", "DA42"], M)

    def test_both_ends_with_one_antenna_is_none(self):
        r = _payload_js([1], [], True)
        assert r["none"] is True and r["baselines"] == []

    def test_both_ends_with_one_antenna_and_autos_is_its_auto(self):
        r = _payload_js([1], [], True, meta=_meta(autos=True))
        assert r["baselines"] == [["DA41", "DA41"]] and not r["none"]

    def test_both_ends_with_all_or_none_ticked_is_everything(self):
        for sel in ([], [0, 1, 2, 3]):
            r = _payload_js(sel, [], True)
            assert r["baselines"] == [] and not r["none"]

    def test_ticked_baselines_win_over_both_ends(self):
        r = _payload_js([0, 1], [5], True)
        assert r["baselines"] == [["DV01", "DA42"]] and r["both_ends_antennas"] == 0

    def test_tolerates_missing_sources(self):
        r = _node(abs_.SELECTION_PAYLOAD_JS + "process.stdout.write(JSON.stringify("
                  "cvAntennaBaselineSelection(null, undefined, true)));")
        assert r == {"antenna_names": [], "baselines": [],
                     "both_ends_antennas": 0, "none": False}


# ---------------------------------------------------------------------------
# 5. Stepping
# ---------------------------------------------------------------------------

def _step_js(which, ant_sel, bl_sel, delta, meta=M):
    fn = (abs_.iterate_antenna_js(_iter_guard_js("names.length", "antenna"))
          if which == "antenna" else
          abs_.iterate_baseline_js(_iter_guard_js("cand.length", "baseline")))
    call = "doIterateAntenna" if which == "antenna" else "doIterateBaseline"
    return _node(STEP_INDEX_JS + fn + f"""
const ant_src = {{data: {json.dumps(abs_.antenna_table_data(meta))},
                 selected: {{indices: {json.dumps(list(ant_sel))}}}}};
const bl_src = {{data: {json.dumps(abs_.baseline_table_data(meta))},
                selected: {{indices: {json.dumps(list(bl_sel))}}}}};
const notify_div = {{text: ''}};
{call}({delta});
process.stdout.write(JSON.stringify(
    [ant_src.selected.indices, bl_src.selected.indices, notify_div.text]));
""")


@needs_node
class TestSteppingJS:

    def test_antenna_starts_at_first(self):
        assert _step_js("antenna", [], [], 1)[0] == [0]

    def test_antenna_steps_and_wraps(self):
        assert _step_js("antenna", [1], [], 1)[0] == [2]
        assert _step_js("antenna", [3], [], 1)[0] == [0]
        assert _step_js("antenna", [0], [], -1)[0] == [3]

    def test_antenna_multi_selection_starts_fresh(self):
        assert _step_js("antenna", [1, 2], [], 1)[0] == [0]

    def test_antenna_step_clears_ticked_baselines(self):
        ants, bls, _ = _step_js("antenna", [1], [4, 5], 1)
        assert ants == [2] and bls == []

    def test_antenna_single_antenna_dataset(self):
        ants, _, note = _step_js("antenna", [], [], 1, meta=_meta(("A",)))
        assert ants == [] and "nothing to iterate" in note

    def test_baseline_all_in_id_order(self):
        assert _step_js("baseline", [], [], 1)[1] == [0]
        assert _step_js("baseline", [], [0], 1)[1] == [1]
        assert _step_js("baseline", [], [5], 1)[1] == [0]
        assert _step_js("baseline", [], [0], -1)[1] == [5]

    def test_baseline_stays_within_ticked_antenna(self):
        # DA41 (row 1) is in baselines 0, 3, 4.
        assert _step_js("baseline", [1], [], 1)[1] == [0]
        assert _step_js("baseline", [1], [0], 1)[1] == [3]
        assert _step_js("baseline", [1], [3], 1)[1] == [4]
        assert _step_js("baseline", [1], [4], 1)[1] == [0]

    def test_baseline_outside_the_antennas_starts_fresh(self):
        # Row 5 (DV01&DA42) is not one of DA41's baselines.
        assert _step_js("baseline", [1], [5], 1)[1] == [0]

    def test_baseline_all_antennas_ticked_is_no_restriction(self):
        assert _step_js("baseline", [0, 1, 2, 3], [1], 1)[1] == [2]

    def test_baseline_step_leaves_antenna_ticks(self):
        assert _step_js("baseline", [1], [0], 1)[0] == [1]

    def test_no_baselines(self):
        from dataclasses import replace
        ants, bls, note = _step_js("baseline", [], [], 1,
                                   meta=replace(M, baselines=()))
        assert bls == [] and "No baselines" in note


# ---------------------------------------------------------------------------
# 6. Constructor string -> ticks -> selection -> status
# ---------------------------------------------------------------------------

class TestInitialStateAndResolve:

    def test_empty(self):
        s = abs_.initial_state("", M)
        assert s == dict(antenna_rows=[], baseline_rows=[], both=False,
                         antenna_names=None, baselines=None)

    def test_antenna(self):
        s = abs_.initial_state("DA41", M)
        assert s["antenna_rows"] == [1] and s["antenna_names"] == ["DA41"]
        assert not s["both"] and s["baselines"] is None

    def test_exclusion_really_excludes(self):
        # Before 2026-10 "!DA42" resolved to "every other antenna" under
        # the either-end rule, which kept all of DA42's baselines.
        s = abs_.initial_state("!DA42", M)
        assert s["antenna_rows"] == [0, 1, 2] and s["both"] is True
        assert s["antenna_names"] is None
        assert s["baselines"] == [("DV03", "DA41"), ("DV03", "DV01"), ("DA41", "DV01")]
        assert not any("DA42" in p for p in s["baselines"])

    def test_baseline_either_order_gives_data_orientation(self):
        for text in ("DA41&DV01", "DV01&DA41", "1&2"):
            s = abs_.initial_state(text, M)
            assert s["baseline_rows"] == [3] and s["baselines"] == [("DA41", "DV01")]

    def test_antennas_and_baselines_together(self):
        s = abs_.initial_state("DV03; DA41&DV01", M)
        assert s["antenna_rows"] == [0] and s["baseline_rows"] == [3]
        assert s["baselines"] == [("DA41", "DV01")]

    def test_unmatched_parts_are_skipped_not_fatal(self, caplog):
        s = abs_.initial_state("DA41,nope,DA41&nope", M)
        assert s["antenna_rows"] == [1] and s["baseline_rows"] == []
        assert "nope" in caplog.text

    def test_all_unmatched_is_all_not_nothing(self):
        s = abs_.initial_state("nope", M)
        assert s["antenna_names"] is None and s["baselines"] is None

    def _resolve(self, ants, bls, names=None, pairs=None):
        return abs_.resolve_antenna_baseline_selection(names, pairs, ants, bls, M)

    def test_browser_silent_uses_the_string(self):
        assert self._resolve(None, None, ["DA41"], None) == (["DA41"], None)
        assert self._resolve(None, None, ["DA41"], [("DV03", "DA41")]) == \
            (None, [("DV03", "DA41")])

    def test_nothing_ticked_is_all_even_if_the_string_said_otherwise(self):
        assert self._resolve([], [], ["DA41"], [("DV03", "DA41")]) == (None, None)

    def test_all_antennas_ticked_is_all(self):
        assert self._resolve(["DV03", "DA41", "DV01", "DA42"], []) == (None, None)

    def test_antennas_come_back_in_table_order(self):
        assert self._resolve(["DA42", "DV03"], []) == (["DV03", "DA42"], None)

    def test_baselines_win_and_antenna_names_are_dropped(self):
        assert self._resolve(["DA41"], [["DV01", "DA42"]]) == (None, [("DV01", "DA42")])

    def test_unknown_names_and_pairs_are_dropped(self):
        assert self._resolve(["nope", "DA41"], []) == (["DA41"], None)
        assert self._resolve([], [["DA42", "DV01"], ["x"], None, ["DV01", "DA42"]]) == \
            (None, [("DV01", "DA42")])
        assert self._resolve(["nope"], [["a", "b"]]) == (None, None)

    def test_status(self):
        st = abs_.selection_status
        assert st(None, None, M) == "Antenna: all"
        assert st(["DA41"], None, M) == "Antenna 2/4: DA41"
        assert st(["DV03", "DA41"], None, M) == "Antennas: 2 selected"
        assert st(None, [("DA41", "DV01")], M) == "Baseline 4/6: DA41&DV01"
        assert st(None, [("DA41", "DV01"), ("DV03", "DA41")], M) == "Baselines: 2 selected"
        assert st(None, [("DV03", "DA41")] * 3, M, both_ends_antennas=3) == \
            "Antennas: 3 of 4, both ends (3 baselines)"

    def test_table_data(self):
        a = abs_.antenna_table_data(M)
        assert a["name"] == ["DV03", "DA41", "DV01", "DA42"] and a["ident"][2] == "2"
        b = abs_.baseline_table_data(_meta(("A", "B"), autos=True))
        assert b["name"] == ["A&A (auto)", "A&B", "B&B (auto)"]
        assert b["ant1"] == ["A", "A", "B"] and b["ident"] == ["0", "1", "2"]


# ---------------------------------------------------------------------------
# 7. Baseline metadata
# ---------------------------------------------------------------------------

class TestBaselineMetadata:

    def _ds(self, ids, a1, a2, with_coord=True):
        coords = {"baseline_antenna1_name": ("baseline_id", a1),
                  "baseline_antenna2_name": ("baseline_id", a2)}
        if with_coord:
            coords["baseline_id"] = ids
        return xr.Dataset(coords=coords)

    def test_collect_and_order(self):
        from cubevis.toolbox.visplot.data._baseline_meta import (
            baselines_to_meta, collect_baselines)
        t = {}
        collect_baselines(self._ds([2, 0, 1], ["B", "A", "A"], ["C", "B", "C"]), t)
        assert baselines_to_meta(t) == [[0, "A", "B"], [1, "A", "C"], [2, "B", "C"]]

    def test_partitions_merge_without_duplicates(self):
        from cubevis.toolbox.visplot.data._baseline_meta import (
            baselines_to_meta, collect_baselines)
        t = {}
        collect_baselines(self._ds([0, 1], ["A", "A"], ["B", "C"]), t)
        collect_baselines(self._ds([0, 2], ["A", "B"], ["B", "C"]), t)
        assert baselines_to_meta(t) == [[0, "A", "B"], [1, "A", "C"], [2, "B", "C"]]

    def test_no_coordinate_uses_position_and_no_names_is_ignored(self):
        from cubevis.toolbox.visplot.data._baseline_meta import (
            baselines_to_meta, collect_baselines)
        t = {}
        collect_baselines(self._ds(None, ["A", ""], ["B", "C"], with_coord=False), t)
        collect_baselines(xr.Dataset(coords={"antenna_name": ["A"]}), t)
        assert baselines_to_meta(t) == [[0, "A", "B"]]

    def test_contract_and_dto(self):
        from cubevis.toolbox.visplot.data.reader import METADATA_KEYS
        assert "baselines" in METADATA_KEYS
        m = ObservationMetadata.from_backend_metadata(
            {"antenna_names": ["A", "B"], "baselines": [[0, "A", "B"]]})
        assert m.baselines == (BaselineInfo(0, "A", "B"),)
        assert m.baselines[0].name == "A&B" and not m.baselines[0].is_auto
        # A backend that does not report them: empty, not an error.
        assert ObservationMetadata.from_backend_metadata({}).baselines == ()


# ---------------------------------------------------------------------------
# 8-9. Real backends and a real plotter, on simulated data
# ---------------------------------------------------------------------------

def _transform(desc, data):
    ddid = int(desc.DATA_DESC_ID.item())
    rng = np.random.default_rng(1000 + ddid * 17 + int(desc.chunk_id))
    dims, vis = data["DATA"]
    ph = (np.deg2rad(rng.normal(0, 10.0, vis.shape))
          + rng.uniform(-3, 3, (vis.shape[0],) + (1,) * (vis.ndim - 1)))
    amp = 1.0 + 0.1 * rng.standard_normal(vis.shape)
    data["DATA"] = (dims, (amp * np.exp(1j * ph)).astype(np.complex64))
    fdims, _ = data["FLAG"]
    data["FLAG"] = (fdims, np.zeros(vis.shape, dtype=bool))
    return data


@pytest.fixture(scope="module")
def sim_ms(tmp_path_factory):
    sim = pytest.importorskip("xarray_ms.testing.simulator")
    path = str(tmp_path_factory.mktemp("absel") / "s.ms")
    sim.MSStructureSimulator(
        ntime=24, nantenna=6, auto_corrs=False,
        data_description=[(16, ["XX", "YY"])],
        simulate_data=True, transform_data=_transform).simulate_ms(path)
    return path


@pytest.fixture(scope="module")
def sim_ps(sim_ms, tmp_path_factory):
    out = str(tmp_path_factory.mktemp("abselps") / "s.ps.zarr")
    dt = xr.open_datatree(sim_ms, engine="xarray-ms:msv2",
                          partition_schema=["FIELD_ID"])
    dt.to_zarr(out, mode="w", compute=True)
    return out


@pytest.fixture(params=["msv2", "msv4"])
def backend(request, sim_ms, sim_ps):
    if request.param == "msv2":
        from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
        b = MSv2Backend(sim_ms)
    else:
        from cubevis.toolbox.visplot.data.msv4_backend import MSv4Backend
        b = MSv4Backend(sim_ps)
    b.open()
    yield b
    b.close()


class TestRealBackends:

    def test_metadata_lists_every_baseline(self, backend):
        bls = backend.metadata()["baselines"]
        assert len(bls) == 15                       # 6 antennas, no autos
        assert [b[0] for b in bls] == list(range(15))
        assert bls[0][1:] == ["ANTENNA-0", "ANTENNA-1"]
        assert len({(b[1], b[2]) for b in bls}) == 15

    def test_one_listed_baseline_selects_one_baseline(self, backend):
        from cubevis.toolbox.visplot.axes import Axis
        from cubevis.toolbox.visplot.selection import SelectionSpec
        bl = backend.metadata()["baselines"][7]
        agg, *_ = backend.query_raster(
            Axis.BASELINE, Axis.TIME, Axis.AMPLITUDE,
            SelectionSpec(baselines=[(bl[1], bl[2])]), polarization="XX")
        assert agg.sizes[agg.dims[0]] == 1
        assert int(agg.coords[agg.dims[0]].values[0]) == bl[0]   # axis number == "#"
        assert np.isfinite(agg.values).all()

    def test_every_listed_pair_matches_in_the_listed_orientation(self, backend):
        from cubevis.toolbox.visplot.axes import Axis
        from cubevis.toolbox.visplot.selection import SelectionSpec
        bls = backend.metadata()["baselines"]
        agg, *_ = backend.query_raster(
            Axis.BASELINE, Axis.TIME, Axis.AMPLITUDE,
            SelectionSpec(baselines=[(b[1], b[2]) for b in bls]), polarization="XX")
        assert agg.sizes[agg.dims[0]] == len(bls)
        # ...and reversed pairs match nothing: orientation matters, which
        # is why the table takes its pairs from the data.
        rev = SelectionSpec(baselines=[(bls[0][2], bls[0][1])])
        try:
            agg, *_ = backend.query_raster(Axis.BASELINE, Axis.TIME,
                                           Axis.AMPLITUDE, rev, polarization="XX")
            n = 0 if agg is None else agg.sizes.get(agg.dims[0], 0)
        except Exception:
            n = 0
        assert n == 0

    def test_single_baseline_waterfall_phase_rms(self, backend):
        # The SPFLG view this work was needed for: one baseline, Time x
        # Channel, Phase RMS with a window.  The simulated data have 10
        # deg of phase noise across the band, on a phase that jumps at
        # random from one integration to the next (_transform draws one
        # offset per row).  So a channel window reads the 10 deg, and a
        # time window correctly reads the jumps instead.
        from cubevis.toolbox.visplot.axes import Axis
        from cubevis.toolbox.visplot.selection import SelectionSpec
        import dataclasses
        bl = backend.metadata()["baselines"][3]
        sel = SelectionSpec(baselines=[(bl[1], bl[2])])
        blank, *_ = backend.query_raster(Axis.TIME, Axis.CHANNEL, Axis.PHASE_RMS,
                                         sel, polarization="XX")
        assert np.isnan(blank.values).all()
        chan, *_ = backend.query_raster(
            Axis.TIME, Axis.CHANNEL, Axis.PHASE_RMS,
            dataclasses.replace(sel, stat_chan_window=8), polarization="XX")
        assert chan.shape == blank.shape
        assert np.nanmean(chan.values) == pytest.approx(10.0, rel=0.15)
        scan, *_ = backend.query_raster(
            Axis.TIME, Axis.CHANNEL, Axis.PHASE_RMS,
            dataclasses.replace(sel, stat_time_window="scan"), polarization="XX")
        assert np.nanmean(scan.values) > 50.0


def _plot_msg(vp, **over):
    W = vp._panel_axis_widgets
    msg = {"field": "", "correlation": "XX,YY", "datacolumn": "data",
           "reload": False,
           "spw_ids": [s.spw_id for s in vp._meta.spws],
           "antenna_names": [], "baselines": [], "both_ends_antennas": 0,
           "panels": {
               "A": {"kind": "raster",
                     "y": W["A"]["raster"]["y_sel"].value,
                     "x": W["A"]["raster"]["x_sel"].value,
                     "qty": W["A"]["raster"]["q_sel"].value},
               "B": {"kind": "scatter",
                     "x": W["B"]["scatter"]["x_sel"].value,
                     "y": W["B"]["scatter"]["y_sel"].value,
                     "colorize": [None, None]}}}
    msg.update(over)
    return msg


@pytest.fixture(params=["msv2", "msv4"])
def plotter(request, sim_ms, sim_ps):
    from cubevis.toolbox.visplot import VisibilityPlotter
    kw = dict(ms=sim_ms) if request.param == "msv2" else dict(ps=sim_ps)
    vp = VisibilityPlotter(layout="side", correlation="XX,YY", **kw)
    yield vp
    vp.close()


class TestRealPlotter:

    def test_tables_are_built_from_the_data(self, plotter):
        assert len(plotter._antenna_source.data["name"]) == 6
        assert len(plotter._baseline_source.data["name"]) == 15
        assert plotter._antenna_source.selected.indices == []
        assert plotter._baseline_source.selected.indices == []
        assert plotter._antenna_mode.active == 0
        assert plotter._build_selection().antenna_names is None
        assert plotter._build_selection().baselines is None

    def test_every_text_box_is_wired_once(self, plotter):
        for box in (plotter._spw_text, plotter._antenna_text, plotter._baseline_text):
            cbs = box.js_property_callbacks.get("change:value", [])
            assert len(cbs) == 1
            assert "cvApplySelectionText" in cbs[0].code
            assert box.value == ""                       # never holds a selection
        args = plotter._antenna_text.js_property_callbacks["change:value"][0].args
        assert args["mode_switch"] is plotter._antenna_mode
        assert args["clear_src"] is plotter._baseline_source
        assert args["keys"] == abs_.antenna_keys(plotter._meta)

    def test_text_boxes_show_a_hint_like_time_range_does(self, plotter):
        # Time range / UV range show help in the status area while the
        # pointer is over them; the tick-from-text boxes did not at
        # first (reported 2026-10-05).
        from bokeh.events import MouseEnter, MouseLeave
        for box, hint, word in (
                (plotter._spw_text, plotter._hint_spw_text, "SPW"),
                (plotter._antenna_text, plotter._hint_antenna_text, "Antennas"),
                (plotter._baseline_text, plotter._hint_baseline_text, "Baselines")):
            assert word in hint.text and "Enter" in hint.text
            assert hint.visible is False
            enter = box.js_event_callbacks[MouseEnter.event_name]
            leave = box.js_event_callbacks[MouseLeave.event_name]
            assert any(cb.args.get("hint") is hint and "hint.visible = true" in cb.code
                       for cb in enter)
            assert any(cb.args.get("hint") is hint and "hint.visible = false" in cb.code
                       for cb in leave)
            assert any("cvNoAutofill" in cb.code for cb in enter)
        # The examples use this data set's own antenna names.
        first = plotter._meta.antennas[0].name
        assert first in plotter._hint_antenna_text.text
        assert first in plotter._hint_baseline_text.text

    def test_plot_code_reads_the_tables(self, plotter):
        a = plotter._plot_js_args
        assert a["ant_src"] is plotter._antenna_source
        assert a["bl_src"] is plotter._baseline_source
        assert a["ant_mode"] is plotter._antenna_mode
        assert "antenna_input" not in a
        code = plotter._do_plot_js
        assert "function cvAntennaBaselineSelection" in code
        assert "antenna_names: _ab.antenna_names" in code
        assert "antenna_input" not in code
        for btn in (plotter._baseline_prev_btn, plotter._baseline_next_btn):
            assert "doIterateBaseline" in btn.js_event_callbacks["button_click"][0].code

    def test_ticked_antenna(self, plotter):
        resp = asyncio.run(plotter._handle_plot(
            _plot_msg(plotter, antenna_names=["ANTENNA-2"])))
        assert resp.get("status") != "error"
        sel = plotter._selection
        assert sel.antenna_names == ["ANTENNA-2"] and sel.baselines is None
        assert "Antenna 3/6: ANTENNA-2" in plotter._status_text()

    def test_ticked_baseline_replots_with_one_baseline(self, plotter):
        def msg(**kw):
            m = _plot_msg(plotter, **kw)
            m["panels"]["A"].update(y="BASELINE", x="TIME")   # Baseline on an axis
            return m
        asyncio.run(plotter._handle_plot(msg()))
        from cubevis.toolbox.visplot.axes import Axis as _A
        r0 = plotter._slots[0].raster
        assert r0._agg.shape[0 if r0._y_dim is _A.BASELINE else 1] == 15
        bl = plotter._meta.baselines[4]
        resp = asyncio.run(plotter._handle_plot(msg(
            antenna_names=["ANTENNA-5"], baselines=[list(bl.pair)])))
        assert resp.get("status") != "error"
        sel = plotter._selection
        assert sel.baselines == [bl.pair] and sel.antenna_names is None
        assert f"Baseline 5/15: {bl.name}" in plotter._status_text()
        # The raster really re-queried: a title is only sent when it did,
        # and its aggregate now holds a single baseline.
        assert resp["panels"]["A"]["title"] is not None
        raster = plotter._slots[0].raster
        from cubevis.toolbox.visplot.axes import Axis
        axis = 0 if raster._y_dim is Axis.BASELINE else 1
        assert Axis.BASELINE in (raster._y_dim, raster._x_dim)
        assert raster._agg.shape[axis] == 1

    def test_changing_only_the_baseline_requeries(self, plotter):
        # The comparison that decides whether a panel re-queries did not
        # look at baselines at all before 2026-10.
        b0, b1 = plotter._meta.baselines[0], plotter._meta.baselines[9]
        asyncio.run(plotter._handle_plot(_plot_msg(plotter, baselines=[list(b0.pair)])))
        r_same = asyncio.run(plotter._handle_plot(
            _plot_msg(plotter, baselines=[list(b0.pair)])))
        r_new = asyncio.run(plotter._handle_plot(
            _plot_msg(plotter, baselines=[list(b1.pair)])))
        assert r_same["panels"]["A"]["title"] is None        # nothing changed
        assert r_new["panels"]["A"]["title"] is not None     # raster
        assert r_new["panels"]["B"]["title"] is not None     # scatter

    def test_both_ends_payload(self, plotter):
        names = [a.name for a in plotter._meta.antennas if a.name != "ANTENNA-2"]
        pairs = abs_.pairs_between(names, plotter._meta)
        resp = asyncio.run(plotter._handle_plot(_plot_msg(
            plotter, baselines=[list(p) for p in pairs], both_ends_antennas=5)))
        assert resp.get("status") != "error"
        assert len(plotter._selection.baselines) == 10
        assert "Antennas: 5 of 6, both ends (10 baselines)" in plotter._status_text()

    def test_back_to_nothing_ticked_is_everything(self, plotter):
        asyncio.run(plotter._handle_plot(_plot_msg(plotter, antenna_names=["ANTENNA-1"])))
        asyncio.run(plotter._handle_plot(_plot_msg(plotter)))
        assert plotter._selection.antenna_names is None
        assert plotter._selection.baselines is None
        assert "Antenna: all" in plotter._status_text()

    def test_junk_from_the_browser_cannot_select_nothing(self, plotter):
        resp = asyncio.run(plotter._handle_plot(_plot_msg(
            plotter, antenna_names=["nope"], baselines=[["x", "y"], "junk", None])))
        assert resp.get("status") != "error"
        assert plotter._selection.antenna_names is None
        assert plotter._selection.baselines is None

    def test_legacy_string_message_still_works(self, plotter):
        msg = _plot_msg(plotter)
        for k in ("antenna_names", "baselines", "both_ends_antennas"):
            del msg[k]
        msg["antenna"] = "ANTENNA-3"
        asyncio.run(plotter._handle_plot(msg))
        assert plotter._selection.antenna_names == ["ANTENNA-3"]


@pytest.mark.parametrize("text, ants, bls, mode", [
    ("ANTENNA-2", [2], [], 0),
    ("!ANTENNA-2", [0, 1, 3, 4, 5], [], 1),
    ("ANTENNA-1&ANTENNA-0, 3", [3], [0], 0),
    ("bogus,4", [4], [], 0),
])
def test_constructor_string_sets_the_ticks(sim_ms, text, ants, bls, mode):
    from cubevis.toolbox.visplot import VisibilityPlotter
    vp = VisibilityPlotter(ms=sim_ms, layout="side", correlation="XX,YY",
                           antenna=text)
    try:
        assert vp._antenna_source.selected.indices == ants
        assert vp._baseline_source.selected.indices == bls
        assert vp._antenna_mode.active == mode
        sel = vp._build_selection()
        if text == "!ANTENNA-2":
            assert sel.antenna_names is None and len(sel.baselines) == 10
            assert not any("ANTENNA-2" in p for p in sel.baselines)
            assert "5 of 6, both ends" in vp._status_text()
    finally:
        vp.close()


# ---------------------------------------------------------------------------
# 10. cvNoAutofill: best effort, never throws
# ---------------------------------------------------------------------------

@needs_node
class TestNoAutofillJS:

    @staticmethod
    def _run(setup):
        return _node(abs_.NO_AUTOFILL_JS + setup + """
process.stdout.write(JSON.stringify([cvNoAutofill(model), attrs]));
""")

    def test_sets_the_attribute_via_find_one(self):
        ok, attrs = self._run("""
const attrs = {};
const el = {setAttribute: function(k, v) { attrs[k] = v; }};
const model = {id: 'm1'};
globalThis.Bokeh = {index: {find_one: function(m) { return m === model ? {input_el: el} : null; }}};
""")
        assert ok is True and attrs == {"autocomplete": "off"}

    def test_falls_back_to_id_lookup_and_shadow_root(self):
        ok, attrs = self._run("""
const attrs = {};
const el = {setAttribute: function(k, v) { attrs[k] = v; }};
const model = {id: 'm1'};
globalThis.Bokeh = {index: {find_one_by_id: function(id) {
    return id === 'm1' ? {shadow_el: {querySelector: function(q) { return q === 'input' ? el : null; }}} : null; }}};
""")
        assert ok is True and attrs == {"autocomplete": "off"}

    @pytest.mark.parametrize("setup", [
        "const attrs = {}; const model = {id: 'm'};",                       # no Bokeh
        "const attrs = {}; const model = {id: 'm'}; globalThis.Bokeh = {};",
        "const attrs = {}; const model = {id: 'm'}; globalThis.Bokeh = {index: {}};",
        "const attrs = {}; const model = {id: 'm'}; globalThis.Bokeh = {index: {find_one: function() { return null; }}};",
        "const attrs = {}; const model = {id: 'm'}; globalThis.Bokeh = {index: {find_one: function() { return {}; }}};",
        "const attrs = {}; const model = {id: 'm'}; globalThis.Bokeh = {index: {find_one: function() { throw new Error('x'); }}};",
        "const attrs = {}; const model = null; globalThis.Bokeh = {index: {find_one: function() { return {}; }}};",
    ])
    def test_does_nothing_and_never_throws(self, setup):
        ok, attrs = self._run(setup)
        assert ok is False and attrs == {}
