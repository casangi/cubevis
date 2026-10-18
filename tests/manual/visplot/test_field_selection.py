"""
test_field_selection.py
=======================
``field=`` in CASA syntax (2026-10-18).  Until then only one name or one
FIELD_ID was understood; ``field="3c279,J0522-364"`` was taken as one
literal name, matched nothing, and the plot said "no unflagged data".

1. ``selection_text.parse_field_text``: names, ids, ranges, ``>n``,
   globs, ``!``, refusals.
2. The plotter: several fields reach the backends; text matching nothing
   is refused at construction; the Field menu gets one entry for the
   several fields and Field ◀ ▶ steps only through single fields.

Part 2 uses the TW Hya MS (``MS=``; fields 3c279, J0522-364 (one
partition), Ceres, J1037-295, TW Hya with FIELD_IDs 0, 2, 3, 5, 6).
"""
from __future__ import annotations

import os
import warnings

import numpy as np
import pytest

from cubevis.toolbox.visplot.selection_text import SelectionTextError, parse_field_text

warnings.filterwarnings("ignore")

FIELDS = [(0, "J0522-364"), (2, "3c279"), (3, "Ceres"), (5, "J1037-295"), (6, "TW Hya")]


@pytest.mark.parametrize("text,want", [
    ("", None),
    ("   ", None),
    ("3c279", ["3c279"]),
    ("2", ["3c279"]),                         # FIELD_ID, not a position
    ("3c279, Ceres", ["3c279", "Ceres"]),
    ("Ceres,3c279", ["3c279", "Ceres"]),      # data order, not text order
    ("0~3", ["J0522-364", "3c279", "Ceres"]),
    ("3~0", ["J0522-364", "3c279", "Ceres"]),
    (">3", ["J1037-295", "TW Hya"]),
    ("<2", ["J0522-364"]),
    ("J*", ["J0522-364", "J1037-295"]),
    ("TW?Hya", ["TW Hya"]),
    ("!Ceres", ["J0522-364", "3c279", "J1037-295", "TW Hya"]),
    ("J*,!J1037-295", ["J0522-364"]),
    ("TW Hya,5", ["J1037-295", "TW Hya"]),
])
def test_parse(text, want):
    assert parse_field_text(text, FIELDS) == want


def test_name_before_id():
    # A field called "5" is reachable by name even though 5 is also an id.
    fields = [(0, "5"), (5, "other")]
    assert parse_field_text("5", fields) == ["5"]


@pytest.mark.parametrize("text,match", [
    ("3c273", "matches no field"),
    ("3c279,Nope", "'Nope' matches no field"),
    ("4", "matches no field"),               # no FIELD_ID 4
    ("10~12", "matches no field"),
    ("X*", "matches no field"),
    ("!3c279,!J0522-364,!Ceres,!J1037-295,!TW Hya", "leaves no field"),
])
def test_refused(text, match):
    with pytest.raises(SelectionTextError, match=match):
        parse_field_text(text, FIELDS)


def test_unknown_ids_only_names():
    # A backend without FIELD_IDs: names and globs still work, ids do not.
    fields = [(None, "A"), (None, "B")]
    assert parse_field_text("A,B", fields) == ["A", "B"]
    with pytest.raises(SelectionTextError):
        parse_field_text("0", fields)


# ---------------------------------------------------------------------- #
# Plotter                                                                  #
# ---------------------------------------------------------------------- #

MS = os.environ.get("MS", "")
needs_ms = pytest.mark.skipif(not os.path.isdir(MS), reason="TW Hya MS not available (MS=)")


@needs_ms
def test_plotter_several_fields_reach_the_data():
    from cubevis.toolbox.visplot import VisibilityPlotter
    from cubevis.toolbox.visplot.axes import Axis
    vp = VisibilityPlotter(ms=MS, field="3c279,Ceres", headless=True,
                           layout="one", kind="raster")
    try:
        sel = vp._build_selection()
        assert sel.field_names == ["3c279", "Ceres"]
        one = vp._reader.query_raster(Axis.BASELINE, Axis.TIME, Axis.AMPLITUDE,
                                      vp._build_selection().copy(), polarization="XX")
        a = np.asarray(one[0] if isinstance(one, tuple) else one, float)
        assert np.isfinite(a).any()
    finally:
        vp.close()


@needs_ms
def test_plotter_refuses_unmatched_field():
    from cubevis.toolbox.visplot import VisibilityPlotter
    with pytest.raises(ValueError, match="matches no field"):
        VisibilityPlotter(ms=MS, field="3c279,J9999+999", headless=True)


@needs_ms
def test_field_menu_entry_and_stepping_skip_it():
    from cubevis.toolbox.visplot import VisibilityPlotter
    vp = VisibilityPlotter(ms=MS, field="Ceres, 3c279")
    try:
        vp._build_layout()
        sel = vp._field_select
        multi = [o for o in sel.options if "," in o[0]]
        assert len(multi) == 1 and multi[0][0] == sel.value
        assert multi[0][1].startswith("2 fields:")
        # Field ◀ ▶ steps through single fields only.
        from cubevis.toolbox.visplot import visibility_plotter as vpm
        import inspect
        src = inspect.getsource(vpm)
        assert 'filter(v => v.indexOf(",") < 0)' in src
        # One field: no extra entry.
    finally:
        vp.close()
    vp = VisibilityPlotter(ms=MS, field="3c279")
    try:
        vp._build_layout()
        assert vp._field_select.value == "3c279"
        assert not [o for o in vp._field_select.options if "," in o[0]]
    finally:
        vp.close()
