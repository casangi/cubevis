"""xarray's entry point for CASA calibration tables: engine ``"casa-caltab"``.

Registered in ``pyproject.toml`` under ``[project.entry-points."xarray.backends"]``.
xarray is imported here, so this module is loaded only by xarray (through
the entry point) or by a caller that already has it.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from xarray.backends import BackendEntrypoint

from .. import _arcae_util as au
from ._reader import open_caltab


class CaltabBackendEntrypoint(BackendEntrypoint):
    """``xr.open_datatree(path, engine="casa-caltab")``: one node per
    spectral window (``cubevis.xarray.caltab.open_caltab``).

    ``xr.open_dataset(path, engine="casa-caltab", group="spw_0")`` gives one
    window; without ``group`` the table must have only one.
    """
    description = "CASA calibration tables, read with arcae (cubevis)"
    url = "https://github.com/casangi/cubevis"
    supports_groups = True       # without it xr.open_datatree does not guess this engine
    open_dataset_parameters = ("filename_or_obj", "drop_variables", "group", "spw", "receptor_labels")

    def guess_can_open(self, filename_or_obj) -> bool:
        try:
            path = str(filename_or_obj)
        except Exception:
            return False
        return au.is_table(path) and au.table_info(path).get("type") == "Calibration"

    def open_datatree(self, filename_or_obj, *, drop_variables: Optional[Iterable[str]] = None,
                      spw: Optional[Iterable[int]] = None,
                      receptor_labels: Optional[list[str]] = None, **kwargs: Any):
        tree = open_caltab(str(filename_or_obj), spw=spw, receptor_labels=receptor_labels)
        if drop_variables:
            drop = [drop_variables] if isinstance(drop_variables, str) else list(drop_variables)
            tree = tree.map_over_datasets(lambda ds: ds.drop_vars(drop, errors="ignore"))
        return tree

    def open_groups_as_dict(self, filename_or_obj, **kwargs: Any) -> dict[str, Any]:
        tree = self.open_datatree(filename_or_obj, **kwargs)
        return {node.path: node.to_dataset(inherit=False) for node in tree.subtree}

    def open_dataset(self, filename_or_obj, *, drop_variables: Optional[Iterable[str]] = None,
                     group: Optional[str] = None, spw: Optional[Iterable[int]] = None,
                     receptor_labels: Optional[list[str]] = None, **kwargs: Any):
        if group is not None and spw is None:
            name = str(group).strip("/")
            if not name.startswith("spw_") or not name[4:].isdigit():
                raise ValueError(f"group {group!r}: a calibration table's groups are spw_<id>")
            spw = [int(name[4:])]
        tree = self.open_datatree(filename_or_obj, drop_variables=drop_variables,
                                  spw=spw, receptor_labels=receptor_labels)
        names = list(tree.children)
        if len(names) != 1:
            raise ValueError(f"{filename_or_obj}: {len(names)} spectral windows ({', '.join(names)}); "
                             "name one with group='spw_<id>', or use xr.open_datatree")
        ds = tree[names[0]].to_dataset(inherit=False)
        ds.attrs = {**tree.attrs, **ds.attrs}
        return ds
