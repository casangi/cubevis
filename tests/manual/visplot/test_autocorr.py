"""Autocorrelations: exclude (plotter default) / include / only (2026-10-11).

Found on the first VLBA data (TL016B subset): uncalibrated
autocorrelations sit ~600x above the cross-correlations, so any view of
all baselines that included them was flat at zero.  ``VLBA`` names that
MSv2 (10 stations with autocorrelations); ``MS`` (TW Hya) is used for the
plotter-level default.
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from cubevis.toolbox.visplot.selection import SelectionSpec

VLBA = os.environ.get("VLBA")
MS = os.environ.get("MS")
need_vlba = pytest.mark.skipif(not (VLBA and os.path.isdir(VLBA)),
                               reason="set VLBA to the TL016B subset MS")


def _pairs(ds):
    a1 = np.asarray(ds.coords["baseline_antenna1_name"].values).astype(str)
    a2 = np.asarray(ds.coords["baseline_antenna2_name"].values).astype(str)
    return list(zip(a1, a2))


@need_vlba
@pytest.mark.parametrize("mode,auto,cross", [
    (None, True, True), ("include", True, True),
    ("exclude", False, True), ("only", True, False)])
def test_modes(mode, auto, cross):
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    with MSv2Backend(VLBA) as b:
        raw = next(iter(b._iter_visibility_partitions(None)))
        prs = _pairs(b._apply_selection(raw, SelectionSpec(autocorr=mode)))
        assert any(x == y for x, y in prs) is auto
        assert any(x != y for x, y in prs) is cross


@need_vlba
def test_named_auto_baseline_survives_exclude():
    from cubevis.toolbox.visplot.data.msv2_backend import MSv2Backend
    with MSv2Backend(VLBA) as b:
        raw = next(iter(b._iter_visibility_partitions(None)))
        sel = SelectionSpec(autocorr="exclude", baselines=[("BR", "BR"), ("BR", "FD")])
        assert sorted(_pairs(b._apply_selection(raw, sel))) == [("BR", "BR"), ("BR", "FD")]


@need_vlba
def test_cross_amplitudes_no_longer_hidden():
    """The plotter default leaves only cross-correlations, whose
    amplitudes are far below the autocorrelations' ~1."""
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    p = VisibilityPlotter(ms=VLBA, field="4C39.25", headless=True,
                          raster_y="BASELINE", raster_x="TIME")
    assert p._selection.autocorr == "exclude"
    agg = np.asarray(p._slots[0].raster._agg, dtype=float)
    assert np.nanmax(agg) < 0.2


@pytest.mark.skipif(not (MS and os.path.isdir(MS)), reason="set MS")
def test_argument_validated():
    from cubevis.toolbox.visplot.visibility_plotter import VisibilityPlotter
    with pytest.raises(ValueError, match="autocorr"):
        VisibilityPlotter(ms=MS, field="3c279", headless=True, autocorr="some")
