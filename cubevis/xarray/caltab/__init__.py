"""``cubevis.xarray.caltab`` -- CASA calibration tables as xarray, with arcae.

    import xarray as xr
    dt = xr.open_datatree("tl016b.gff", engine="casa-caltab")
    ds = dt["spw_0"].ds
    ds.CALPARAM_DELAY.sel(antenna_name="BR")          # ns; see its "units"

or ``open_caltab(path)``, the same without xarray's entry point.

One node per spectral window.  Each is a Dataset with the dimensions
``direction, time, antenna_name, frequency, receptor_label`` and the
variables ``CALPARAM_<name>``, ``CALERROR_<name>``, ``SNR`` and ``FLAG``,
following xradio's draft calibration schema where it says something
(``_schema`` lists what is the draft's and what is this backend's).  A
type the backend does not know is still read: its parameters are
``CALPARAM`` over a ``parameter`` dimension.

Nothing is imported until it is asked for: no Bokeh, no CASA, and arcae
and xarray only when a table is opened.
"""
from __future__ import annotations

__all__ = ["open_caltab", "caltab_info", "NotACalTable", "CaltabBackendEntrypoint"]


def __getattr__(name):
    if name in ("open_caltab", "caltab_info", "NotACalTable"):
        from . import _reader
        return getattr(_reader, name)
    if name == "CaltabBackendEntrypoint":
        from ._backend import CaltabBackendEntrypoint
        return CaltabBackendEntrypoint
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
