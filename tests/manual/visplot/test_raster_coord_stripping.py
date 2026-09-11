"""
test_raster_coord_stripping.py
===============================
Regression test for ``_drop_non_raster_coords`` in ``msv2_backend.py`` /
``msv4_backend.py``.

Why this module exists
-----------------------
Live testing of I-1 (duo-mode Field/SPW iteration) against a real MS hit::

    Panel A raster error: ufunc 'minimum' did not contain a loop with
    signature matching types (dtype('<U...')...) -> None

on a Field change — not on SPW. Traced (``visibility_plotter.py``'s
``log.error(..., exc_info=True)`` at the ``panel.update_axes()`` call
site) to ``VisibilityRaster._render()`` → ``query_raster()`` →
``_raster_2d()``, whose AMPLITUDE/PHASE branches build::

    coords={k: v for k, v in vis_pol.coords.items()}

which copies *every* coordinate on the source partition — including
auxiliary display-label coordinates such as ``baseline_antenna1_name`` /
``baseline_antenna2_name`` (string dtype, riding on the ``baseline_id``
dimension) that have no role in a 2D raster array. A ``.mean(dim=...)``
reduction only drops coordinates *along the reduced dimensions*, so these
survive untouched into the 2D array ``query_raster()`` later concatenates
across partitions with ``xr.concat(..., join="outer")`` — and partitions
for different fields commonly disagree on which baselines/antennas are
present, so that concat has to reconcile a *string* coordinate on the
non-concat dimension.

I could not reproduce the exact ``ufunc 'minimum'`` failure directly
against xarray 2026.7.0 / datashader 0.19.1 in this environment despite
several attempts with differing-baseline-coverage partitions (see the
session's chat for what was tried) — the exact internal call may be
version-dependent, or need a scenario not yet hit. What this module
*does* verify, directly and unambiguously, is the fix itself:
``_drop_non_raster_coords`` removes exactly the coordinates it claims to
and nothing else, for both backends, which independently of the precise
failure mechanism removes the only string-typed data anywhere in this
pipeline that could plausibly reach a numeric alignment/ufunc call. If
the original error recurs, that is evidence this diagnosis was
incomplete, not that the fix is wrong -- see the chat response for what
to send next (the full server-side traceback, and exact xarray/
datashader/numpy versions).

Both backends' copies are pure functions (``xr.DataArray`` in,
``xr.DataArray`` out — no MS, no Dask, no backend instance), so they are
lifted by AST rather than importing the modules, matching
``test_spw_selection.py``'s established pattern for the same reason
(``msv2_backend.py`` / ``msv4_backend.py`` import dask/xarray-ms at
module scope).

Test location
-------------
``cubevis/tests/manual/visplot/test_raster_coord_stripping.py``
"""

import ast
import pathlib
import textwrap
import types

import numpy as np
import pytest

xr = pytest.importorskip("xarray")


# ---------------------------------------------------------------------------
# Lifting the function under test
# ---------------------------------------------------------------------------

def _find_visplot() -> pathlib.Path:
    """Locate ``cubevis/toolbox/visplot`` by walking up from this file.

    Same rationale as ``test_spw_selection.py``'s copy of this helper.
    """
    for base in pathlib.Path(__file__).resolve().parents:
        cand = base / "cubevis" / "toolbox" / "visplot"
        if (cand / "visibility_plotter.py").is_file():
            return cand
    raise RuntimeError(
        "could not locate cubevis/toolbox/visplot above "
        f"{pathlib.Path(__file__).resolve()}"
    )


_PKG = _find_visplot()


def _lift(path, *names, extra=None):
    src = pathlib.Path(path).read_text()
    tree = ast.parse(src)
    ns = {"log": types.SimpleNamespace(warning=lambda *a, **k: None,
                                       debug=lambda *a, **k: None),
          "xr": xr}
    ns.update(extra or {})
    for name in names:
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        body = textwrap.dedent(ast.get_source_segment(src, fn))
        for dec in reversed(fn.decorator_list):
            body = f"@{getattr(dec, 'id', 'staticmethod')}\n" + body
        exec(body, ns)
    return ns


@pytest.fixture(scope="module", params=["msv2_backend.py", "msv4_backend.py"])
def drop_coords(request):
    """``_drop_non_raster_coords`` from each backend.

    Parameterised over both, same rationale as ``test_spw_selection.py``'s
    ``ident`` fixture: this exact defect (a fix needed in one backend but
    not mirrored in the other) is §3.1c Rule 4's named failure mode, so a
    fix landing in only one backend must fail this test, not pass it.
    """
    ns = _lift(_PKG / "data" / request.param, "_drop_non_raster_coords")  # backends live under data/
    return ns["_drop_non_raster_coords"]


# ---------------------------------------------------------------------------
# Fixtures — a 2D raster-shaped array carrying the auxiliary coordinates
# that triggered the defect
# ---------------------------------------------------------------------------

def _make_raster_array(extra_coords=True):
    """A (time, baseline_id) array shaped like _raster_2d's return value.

    With auxiliary string coordinates on baseline_id, matching what
    ``coords={k: v for k, v in vis_pol.coords.items()}`` actually copies
    from a real MSv2/MSv4 partition.
    """
    coords = {
        "time":        [100.0, 101.0, 102.0],
        "baseline_id": [0, 1, 2],
    }
    if extra_coords:
        coords["baseline_antenna1_name"] = ("baseline_id",
                                             ["DV01", "DV01", "DV02"])
        coords["baseline_antenna2_name"] = ("baseline_id",
                                             ["DV02", "DV03", "DV03"])
        # Also exercise a string coordinate riding on the OTHER kept
        # dimension (time), and a numeric-but-unwanted one, so the test
        # isn't just checking "strings get dropped" -- ANY non-(y,x)
        # coordinate must go, regardless of dtype or which dimension.
        coords["field_name"] = ("time", ["3C286", "3C286", "3C286"])
        coords["scan_number"] = ("time", [1, 1, 2])
    return xr.DataArray(
        np.zeros((3, 3)), dims=["time", "baseline_id"], coords=coords,
    )


class TestDropNonRasterCoords:

    def test_auxiliary_string_coords_are_dropped(self, drop_coords):
        arr = _make_raster_array()
        out = drop_coords(arr, "time", "baseline_id")
        assert "baseline_antenna1_name" not in out.coords
        assert "baseline_antenna2_name" not in out.coords

    def test_auxiliary_coords_on_the_other_kept_dimension_are_also_dropped(self, drop_coords):
        """Not just baseline_id -- any coordinate outside {y_name, x_name}
        must go, including ones riding on the dimension that IS kept
        (e.g. field_name / scan_number on time)."""
        arr = _make_raster_array()
        out = drop_coords(arr, "time", "baseline_id")
        assert "field_name" not in out.coords
        assert "scan_number" not in out.coords

    def test_dimension_coordinates_survive(self, drop_coords):
        """The two coordinates Canvas.raster() and query_raster()'s
        extent computation actually need must not be touched."""
        arr = _make_raster_array()
        out = drop_coords(arr, "time", "baseline_id")
        assert list(out.coords["time"].values) == [100.0, 101.0, 102.0]
        assert list(out.coords["baseline_id"].values) == [0, 1, 2]

    def test_data_values_and_shape_are_unchanged(self, drop_coords):
        """This is a coordinate-only operation -- the actual raster
        values and array shape must be untouched."""
        arr = _make_raster_array()
        out = drop_coords(arr, "time", "baseline_id")
        assert out.shape == arr.shape
        assert out.dims == arr.dims
        np.testing.assert_array_equal(out.values, arr.values)

    def test_no_op_when_nothing_extra_to_drop(self, drop_coords):
        """An array that only ever had the two needed coordinates must
        pass through unchanged (also exercises the `if extra` branch
        that skips calling drop_vars at all)."""
        arr = _make_raster_array(extra_coords=False)
        out = drop_coords(arr, "time", "baseline_id")
        assert set(out.coords.keys()) == {"time", "baseline_id"}

    def test_result_is_concat_safe_across_differing_baseline_coverage(self, drop_coords):
        """The actual scenario this exists for: two partitions (e.g. two
        fields) whose baseline_id coverage differs. Concatenating the
        RAW arrays (auxiliary coords still attached) is what the defect
        report traced to; concatenating the STRIPPED arrays must succeed
        cleanly with no coordinate reconciliation needed at all, because
        there is nothing left to reconcile except the numeric dimension
        coordinates themselves.
        """
        part_a = xr.DataArray(
            np.ones((2, 3)), dims=["time", "baseline_id"],
            coords={
                "time": [100.0, 101.0], "baseline_id": [0, 1, 2],
                "baseline_antenna1_name": ("baseline_id",
                                           ["DV01", "DV01", "DV02"]),
                "baseline_antenna2_name": ("baseline_id",
                                           ["DV02", "DV03", "DV03"]),
            },
        )
        part_b = xr.DataArray(
            np.ones((2, 2)) * 2, dims=["time", "baseline_id"],
            coords={
                "time": [200.0, 201.0], "baseline_id": [1, 2],
                "baseline_antenna1_name": ("baseline_id", ["DV01", "DV02"]),
                "baseline_antenna2_name": ("baseline_id", ["DV03", "DV03"]),
            },
        )
        stripped = [drop_coords(p, "time", "baseline_id")
                    for p in (part_a, part_b)]
        agg = xr.concat(
            stripped, dim="time", join="outer",
            coords="minimal", compat="override",
        )
        assert set(agg.coords.keys()) == {"time", "baseline_id"}
        assert agg.shape == (4, 3)


class TestBackendWiring:

    def test_raster_2d_calls_the_helper_at_both_return_points(self):
        """Both backends must route every _raster_2d return value
        (the FLAG early return and the shared AMPLITUDE/PHASE/REAL/
        IMAGINARY path) through _drop_non_raster_coords -- a fix that
        only covers one path would leave the other quantity silently
        exposed to the same defect again."""
        for fname in ("msv2_backend.py", "msv4_backend.py"):
            src = (_PKG / "data" / fname).read_text()  # backends live under data/
            start = src.index("def _raster_2d(")
            end = src.index("\n    def ", start + 1)
            body = src[start:end]
            occurrences = body.count("_drop_non_raster_coords(")
            assert occurrences == 2, (
                f"{fname}: expected 2 call sites (FLAG early return + "
                f"shared return) in _raster_2d, found {occurrences}"
            )
