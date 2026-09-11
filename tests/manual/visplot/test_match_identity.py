"""
Unit test for the actual delivered `VisibilityPlot._match_identity`
(specifically the new `bl_ids` exact-match path), extracted directly
from the real source file rather than reimplemented -- importing the
whole module isn't practical here (it needs the real `bokeh` package
plus `cubevis.bokeh.tools._flag_tool.FlagTool`'s Comm-backed Bokeh
model machinery, none of which is available in this sandbox), but
`_match_identity` itself is pure data logic with no Bokeh dependency,
so extracting just its source and exec'ing it verbatim tests the real
code, not a paraphrase of it.
"""
import ast
import os
import sys
from dataclasses import dataclass, field

# Default assumes this test sits next to a checkout of the delivered
# files at the paths used during development; override with the env
# var if your layout differs.
SRC_PATH = os.environ.get(
    "CUBEVIS_VISIBILITY_PLOT_PY",
    "/home/claude/build/cubevis/toolbox/visplot/visibility_plot.py",
)

FAILURES = []


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


# ---------------------------------------------------------------------
# Prefer a real import (this sandbox has no `bokeh`, so it always falls
# back to source-extraction here -- but a real dev environment with
# bokeh installed should be able to import the module directly, which
# is more robust than the extraction fallback and needs no SRC_PATH at
# all).
# ---------------------------------------------------------------------
try:
    from cubevis.toolbox.visplot.visibility_plot import VisibilityPlot
    _match_identity = VisibilityPlot._match_identity
    print("Using a real import of VisibilityPlot._match_identity.\n")
except Exception as exc:
    print(f"Real import unavailable ({exc!r}); falling back to "
          f"source-extraction from {SRC_PATH}.\n")
    source = open(SRC_PATH).read()
    tree = ast.parse(source)

    method_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "VisibilityPlot":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "_match_identity":
                    method_node = item
                    break

    assert method_node is not None, "could not find VisibilityPlot._match_identity in source"
    method_src = ast.get_source_segment(source, method_node)
    lines = method_src.splitlines()
    indent = len(lines[0]) - len(lines[0].lstrip())
    method_src = "\n".join(l[indent:] if l.strip() else l for l in lines)

    namespace = {"Optional": __import__("typing").Optional}
    exec(compile(method_src, SRC_PATH, "exec"), namespace)
    _match_identity = namespace["_match_identity"]
    print(f"Extracted _match_identity: {len(method_src.splitlines())} lines of real source.\n")


# ---------------------------------------------------------------------
# Minimal stand-ins for IdentityTables / ScanInfo, matching the real
# shape _match_identity actually reads (tables.scans[i].t_start/.t_end/
# .field_name/.scan_name, tables.baseline_antennas: dict[int, tuple]).
# ---------------------------------------------------------------------
@dataclass
class FakeScan:
    t_start: float
    t_end: float
    field_name: str
    scan_name: str


@dataclass
class FakeTables:
    scans: list
    baseline_antennas: dict


TABLES = FakeTables(
    scans=[
        FakeScan(100.0, 102.0, "3c279", "scan1"),
        FakeScan(200.0, 202.0, "TW Hya", "scan2"),
    ],
    baseline_antennas={
        0: ("DA44", "DV05"),
        1: ("DV02", "DV16"),
        2: ("DV05", "DV16"),
        3: ("DV10", "DV15"),
        4: ("DV13", "DV17"),
    },
)


class FakeSelf:
    """Bound-method target: only needs _ensure_identity_tables, since
    that's the only self-method _match_identity's real body calls."""
    def _ensure_identity_tables(self, polarization):
        return TABLES


fake_self = FakeSelf()


def call(**kwargs):
    return _match_identity(fake_self, **kwargs)


# ---------------------------------------------------------------------
# The actual scenario this fix exists for: matched baseline_ids {0, 3}
# are non-contiguous. bl_range=(0,3) alone would (wrongly) also report
# antenna pairs for baselines 1 and 2. bl_ids=[0,3] should not.
# ---------------------------------------------------------------------
result_exact = call(bl_range=(0.0, 3.0), bl_ids=[0, 3])
check("bl_ids given: only baselines 0 and 3's antenna pairs are reported",
      set(result_exact["antenna_pairs"]) == {("DA44", "DV05"), ("DV10", "DV15")},
      result_exact["antenna_pairs"])
check("bl_ids given: baselines 1 and 2 (inside the bl_range but NOT in "
      "bl_ids) are correctly excluded",
      ("DV02", "DV16") not in result_exact["antenna_pairs"]
      and ("DV05", "DV16") not in result_exact["antenna_pairs"],
      result_exact["antenna_pairs"])
check("bl_ids given: exactly 2 pairs, not 4",
      len(result_exact["antenna_pairs"]) == 2, result_exact["antenna_pairs"])

# ---------------------------------------------------------------------
# Backward compatibility: bl_ids=None (pieces 1 and 2's only option)
# must reproduce the OLD contiguous-range behavior byte-for-byte --
# i.e. this fix must change nothing for existing callers.
# ---------------------------------------------------------------------
result_range_only = call(bl_range=(0.0, 3.0))  # bl_ids omitted -> None default
check("bl_ids omitted (pieces 1/2's existing call shape): falls back to "
      "the old contiguous bl_range scan, including baselines 1 and 2",
      set(result_range_only["antenna_pairs"]) ==
      {("DA44", "DV05"), ("DV02", "DV16"), ("DV05", "DV16"), ("DV10", "DV15")},
      result_range_only["antenna_pairs"])

# ---------------------------------------------------------------------
# t_range / field+scan resolution untouched by this change -- sanity
# check it still works alongside the bl_ids path in the same call.
# ---------------------------------------------------------------------
result_combined = call(t_range=(100.5, 101.5), bl_ids=[0])
check("t_range + bl_ids together: correct field/scan for the time window",
      result_combined["field_names"] == ["3c279"] and
      result_combined["scan_names"] == ["scan1"],
      (result_combined["field_names"], result_combined["scan_names"]))
check("t_range + bl_ids together: only baseline 0's pair",
      result_combined["antenna_pairs"] == [("DA44", "DV05")],
      result_combined["antenna_pairs"])

# ---------------------------------------------------------------------
# Empty bl_ids ([], not None) -- distinguish "given but nothing
# matched" from "not given at all". Should report no antenna pairs,
# not fall back to the range scan.
# ---------------------------------------------------------------------
result_empty = call(bl_range=(0.0, 3.0), bl_ids=[])
check("bl_ids=[] (given, but empty) reports zero antenna pairs, "
      "does NOT fall back to the bl_range scan",
      result_empty["antenna_pairs"] == [], result_empty["antenna_pairs"])

print("\n" + "=" * 60)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
else:
    print("All checks passed.")
