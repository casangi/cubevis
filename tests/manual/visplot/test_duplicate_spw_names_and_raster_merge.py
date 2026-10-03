"""Regression tests: duplicate SPW names, multi-SPW raster merge, numba gate.

Self-contained (no MS needed): loads the small helper modules directly.
"""
import importlib.util
import pathlib
import threading

import numpy as np
import pytest
import xarray as xr


def _load(name):
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "cubevis" / "toolbox" / "visplot"
        if (cand / "visibility_plotter.py").is_file():
            path = cand / (name if name.endswith(".py") else name + ".py")
            spec = importlib.util.spec_from_file_location(path.stem, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError("visplot package not found")


# ---------------------------------------------------------------- SPW ident
class _Coord:
    def __init__(self, **attrs):
        self.attrs = attrs


class FakeDS:
    def __init__(self, name, ref_hz):
        self.attrs = {}
        self.coords = {"frequency": _Coord(
            spectral_window_name=name,
            reference_frequency={"attrs": {}, "data": ref_hz})}


def _raw(ds):
    name = ds.coords["frequency"].attrs.get("spectral_window_name")
    return (str(name), "name") if name else (None, "none")


@pytest.fixture(scope="module")
def ident_mod():
    return _load("data/_spw_identity.py")


def test_duplicate_names_resolve_to_distinct_rows(ident_mod):
    # EVLA: Subband:0 appears in two basebands with different ref freqs.
    amb = {("Subband:0", 1_636_000_000): 0, ("Subband:0", 945_000_000): 8}
    f = ident_mod.make_disambiguating_ident(_raw, amb)
    assert f(FakeDS("Subband:0", 1.636e9)) == (0, "spw")
    assert f(FakeDS("Subband:0", 0.945e9)) == (8, "spw")


def test_unique_names_untouched(ident_mod):
    amb = {("Subband:0", 1_636_000_000): 0, ("Subband:0", 945_000_000): 8}
    f = ident_mod.make_disambiguating_ident(_raw, amb)
    assert f(FakeDS("ALMA_RB_07#BB_2", 1.0e11)) == ("ALMA_RB_07#BB_2", "name")


def test_no_ambiguity_returns_raw_function(ident_mod):
    assert ident_mod.make_disambiguating_ident(_raw, {}) is _raw


def test_missing_ref_freq_falls_back_to_name(ident_mod):
    amb = {("Subband:0", 1_636_000_000): 0}
    ds = FakeDS("Subband:0", None)
    ds.coords["frequency"].attrs.pop("reference_frequency")
    f = ident_mod.make_disambiguating_ident(_raw, amb)
    assert f(ds) == ("Subband:0", "name")


# ------------------------------------------------------------- raster merge
@pytest.fixture(scope="module")
def merge():
    return _load("data/_raster_merge.py").merge_raster_partitions


def _mk(v, y, x, yn, xn):
    return xr.DataArray(np.full((len(y), len(x)), v, np.float32),
                        dims=(yn, xn), coords={yn: y, xn: x})


def test_time_by_frequency_spws_share_time_rows(merge):
    t = np.arange(179.0)
    parts = [_mk(s + 1, t, np.arange(64) + s * 64.0, "time", "frequency")
             for s in range(16)]
    agg = merge(parts, "time", "frequency")
    assert agg.shape == (179, 1024)          # NOT (179*16, 1024)
    assert not np.isnan(agg.values).any()
    for s in range(16):
        assert (agg.values[:, s * 64:(s + 1) * 64] == s + 1).all()


def test_baseline_by_time_split_by_scan(merge):
    bl = np.arange(6)
    agg = merge([_mk(1, bl, np.arange(0, 100.0), "baseline_id", "time"),
                 _mk(2, bl, np.arange(100, 180.0), "baseline_id", "time")],
                "baseline_id", "time")
    assert agg.shape == (6, 180) and not np.isnan(agg.values).any()


def test_overlap_first_wins_and_sorted(merge):
    a = _mk(1, np.array([5.0, 1.0, 3.0]), np.arange(4.0), "time", "frequency")
    b = _mk(2, np.array([3.0, 4.0]), np.arange(4.0), "time", "frequency")
    agg = merge([a, b], "time", "frequency")
    assert agg.time.values.tolist() == [1.0, 3.0, 4.0, 5.0]
    assert (agg.sel(time=3.0).values == 1).all()


# --------------------------------------------------------------- numba gate
def test_gate_wraps_once_and_is_reentrant():
    pytest.importorskip("datashader")
    gate = _load("_numba_gate.py")
    import datashader.transfer_functions as tf
    assert gate.install()
    first = tf.shade
    assert getattr(first, "_cv_gated", False)
    assert gate.install() and tf.shade is first      # idempotent
    with gate.numba_gate:
        with gate.numba_gate:                          # re-entrant
            pass


# ------------------------------------------------- degenerate scatter range
@pytest.fixture(scope="module")
def nonzero_span():
    """``_scatter_render.nonzero_span``, lifted by source (the module needs
    datashader/pandas and package-relative imports to load whole)."""
    import ast
    import math
    import textwrap
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "cubevis" / "toolbox" / "visplot" / "data" / "_scatter_render.py"
        if cand.is_file():
            src = cand.read_text()
            fn = next(n for n in ast.walk(ast.parse(src))
                      if isinstance(n, ast.FunctionDef) and n.name == "nonzero_span")
            ns = {"math": math}
            exec(textwrap.dedent(ast.get_source_segment(src, fn)), ns)
            return ns["nonzero_span"]
    raise RuntimeError("_scatter_render.py not found")


def test_real_range_unchanged(nonzero_span):
    assert nonzero_span(0.0, 1.0) == (0.0, 1.0)
    assert nonzero_span(3.0, 1.0) == (1.0, 3.0)          # order normalised


@pytest.mark.parametrize("v", [0.0, 0.339, 45.0, -2.5, 1.287e9, 1.6e9, 1e-15])
def test_constant_axis_gets_nonzero_width(nonzero_span, v):
    lo, hi = nonzero_span(v, v)
    assert hi > lo and lo < v < hi                       # datashader divides by hi-lo


@pytest.mark.parametrize("bad", [(float("nan"), 1.0), (0.0, float("inf"))])
def test_non_finite_falls_back(nonzero_span, bad):
    assert nonzero_span(*bad) == (0.0, 1.0)
