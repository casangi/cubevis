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

Without a table: ``synthetic_caltab("Fringe", nant=27)`` is a tree of the
same shape with values that follow a stated rule, and
``write_caltab(tree, path)`` makes a CASA table of any tree, with arcae
alone (for tests; see ``_write``).

Flags are written back with ``commit_flags(tree)``: edit the tree's
``FLAG``, and the table is made to say the same, with a backup of what
it said before and a read-back (``_flags``).

Importing this package is enough for ``engine="casa-caltab"``: where the
entry point is not installed (a checkout on ``PYTHONPATH``), the import
registers the engine with xarray (``cubevis.xarray._register``).

No Bokeh and no CASA are imported, and arcae only when a table is opened.
Installed, xarray is not imported either until it is needed; from a
checkout, registering the engine imports it.
"""
from __future__ import annotations

__all__ = ["open_caltab", "caltab_info", "NotACalTable", "CaltabBackendEntrypoint",
           "synthetic_caltab", "synthetic_value", "write_caltab", "register_backend",
           "commit_flags", "restore_flags", "list_flag_backups", "FlagCommitError"]

_WHERE = {"open_caltab": "_reader", "caltab_info": "_reader", "NotACalTable": "_reader",
          "CaltabBackendEntrypoint": "_backend",
          "synthetic_caltab": "_synthetic", "synthetic_value": "_synthetic",
          "write_caltab": "_write",
          "commit_flags": "_flags", "restore_flags": "_flags", "list_flag_backups": "_flags",
          "FlagCommitError": "_flags"}


ENGINE = "casa-caltab"


def register_backend(force: bool = False) -> bool:
    """Make ``casa-caltab`` an xarray engine where no installed entry point
    does (done on import; True when it was added by this call)."""
    from .._register import register
    return register(ENGINE, f"{__name__}._backend", "CaltabBackendEntrypoint", force=force)


register_backend()


def __getattr__(name):
    if name in _WHERE:
        from importlib import import_module
        return getattr(import_module(f"{__name__}.{_WHERE[name]}"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
