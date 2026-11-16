"""``cubevis.toolbox.calplot`` -- plotting calibration tables.

The application is to come (plan stages C1 and C2,
``devel/docs/gridplot/grid_and_calplot_plan.md``).  What is here is what it
stands on:

``CalTableReader``
    A calibration table as calplot uses one: what it is (``info``), its
    solutions by selection as flat arrays (``read``), a delay table as the
    phase it applies (``evaluate``), and flags set by selection and written
    back (``set_flags``, ``commit_flags``)::

        from cubevis.toolbox.calplot import CalTableReader
        reader = CalTableReader.open("cal.G")
        s = reader.read("phase", antenna="ea05")

    It reads CASA calibration tables (with arcae: no CASA needed), and
    trees saved to Zarr; the data layer is ``cubevis.xarray.caltab``.

Nothing here imports Bokeh or CASA.
"""
from __future__ import annotations

__all__ = ["CalTableReader", "CalInfo", "CalSamples", "QuantityInfo", "SpwInfo"]


def __getattr__(name):
    if name in __all__:
        from . import reader
        return getattr(reader, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
