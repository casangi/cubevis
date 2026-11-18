########################################################################
#
# Copyright (C) 2026
# Associated Universities, Inc. Washington DC, USA.
#
# This script is free software; you can redistribute it and/or modify it
# under the terms of the GNU Library General Public License as published by
# the Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.
#
# This library is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Library General Public
# License for more details.
#
# You should have received a copy of the GNU Library General Public License
# along with this library; if not, write to the Free Software Foundation,
# Inc., 675 Massachusetts Ave, Cambridge, MA 02139, USA.
#
# Correspondence concerning AIPS++ should be adressed as follows:
#        Internet email: casa-feedback@nrao.edu.
#        Postal address: AIPS++ Project Office
#                        National Radio Astronomy Observatory
#                        520 Edgemont Road
#                        Charlottesville, VA 22903-2475 USA
#
########################################################################
'''An image dataset whatever it came from: ``open_image`` opens a CASA
image, an image saved to Zarr (xradio's ``write_image``, or one of this
package's datasets saved with ``to_zarr``) or takes a dataset in hand;
``describe_dataset`` says what is in it in plain numbers, reading xradio's
attributes and this package's alike.

``describe_dataset`` is the one place that knows how the two differ: xradio
keeps the reference direction, projection and rotation in
``attrs["coordinate_system_info"]`` and leaves the reference pixel and
increment to be found from ``l`` and ``m`` (``l = (pixel - reference_pixel)
* increment``); quantities in attributes are small dicts (``{"data": ...,
"attrs": {"units": ...}}``).  The casa-image engine writes those too, so
one reading serves both.
'''

import os
from typing import Any, Dict, List, Optional

import numpy as np

from ._image import is_casa_image
from ._schema import DIM_FREQUENCY, DIM_L, DIM_M, DIM_POLARIZATION, DIM_TIME

IMAGE_DIMS = (DIM_TIME, DIM_FREQUENCY, DIM_POLARIZATION, DIM_L, DIM_M)


def source_format(source: Any) -> str:
    '''``"casa-image"``, ``"zarr"`` or ``"dataset"``; ValueError for a path
    that is neither kind of image.'''
    if hasattr(source, 'data_vars'):
        return 'dataset'
    try:
        path = os.fspath(source)
    except TypeError:
        raise ValueError(f'not an image: {source!r}') from None
    if is_casa_image(path):
        return 'casa-image'
    if os.path.isdir(path) and any(os.path.exists(os.path.join(path, f)) for f in ('zarr.json', '.zgroup', '.zmetadata')):
        return 'zarr'
    raise ValueError(f'{path}: neither a CASA image (paged or concatenated) nor a Zarr store')


def open_image(source: Any, **kwargs: Any):
    '''An image as an ``xarray.Dataset``: ``source`` is the path of a CASA
    image (``kwargs`` to ``open_casa_image``), the path of a Zarr store (to
    ``xarray.open_zarr``), or a dataset, returned as it is.'''
    kind = source_format(source)
    if kind == 'dataset':
        return source
    path = os.path.abspath(os.path.expanduser(os.fspath(source)))
    if kind == 'casa-image':
        from ._backend import open_casa_image            # pylint: disable=import-outside-toplevel
        return open_casa_image(path, **kwargs)
    import xarray as xr                                  # pylint: disable=import-outside-toplevel
    ds = xr.open_zarr(path, **kwargs)
    ds.encoding.setdefault('source', path)
    return ds


def _scalar(value: Any) -> Optional[float]:
    '''A number from a plain value or an xradio quantity dict.'''
    if isinstance(value, dict):
        value = value.get('data')
    try:
        value = float(np.atleast_1d(np.asarray(value, dtype=float))[0])
    except (TypeError, ValueError, IndexError):
        return None
    return value if np.isfinite(value) else None


def sky_variable(ds, variable: Optional[str] = None) -> str:
    '''The dataset's pixel variable: the one named, else ``data_groups``'
    ``sky``, else the only variable of type ``sky``, else ``SKY``, else
    the only variable over ``l`` and ``m``.'''
    if variable is not None:
        if variable not in ds.data_vars:
            raise KeyError(f'no variable {variable!r} (variables: {list(ds.data_vars)})')
        return variable
    groups = ds.attrs.get('data_groups') or {}
    for group in groups.values():
        if isinstance(group, dict) and group.get('sky') in ds.data_vars:
            return group['sky']
    typed = [n for n, v in ds.data_vars.items() if v.attrs.get('type') == 'sky']
    if len(typed) == 1:
        return typed[0]
    if 'SKY' in ds.data_vars:
        return 'SKY'
    planes = [n for n, v in ds.data_vars.items()
              if DIM_L in v.dims and DIM_M in v.dims and v.dtype.kind in 'fc']
    if len(planes) == 1:
        return planes[0]
    raise ValueError(f'which variable is the image? name one of {list(ds.data_vars)}')


def _flag_variable(ds, variable: str) -> Optional[str]:
    sky = ds[variable]
    names = [sky.attrs.get('flag')]
    for group in (ds.attrs.get('data_groups') or {}).values():
        if isinstance(group, dict) and group.get('sky', variable) == variable:
            names.append(group.get('flag'))
    names.append(f'FLAG_{variable}')
    for name in names:
        if name and name in ds.data_vars and ds[name].dtype == bool and set(ds[name].dims) == set(sky.dims):
            return name
    return None


def _beam_variable(ds, variable: str) -> Optional[str]:
    names = [ds[variable].attrs.get('beam_fit_params')]
    for group in (ds.attrs.get('data_groups') or {}).values():
        if isinstance(group, dict):
            names.extend(v for k, v in group.items() if k.startswith('beam_fit_params'))
    names.append(f'BEAM_FIT_PARAMS_{variable}')
    for name in names:
        if name and name in ds.data_vars and 'beam_params_label' in ds[name].dims:
            return name
    return None


def _axis(values: np.ndarray):
    '''(reference pixel, increment) of an ``l`` or ``m`` coordinate.'''
    values = np.asarray(values, dtype=np.float64)
    if values.size < 2 or values[1] == values[0]:
        return 0.0, None
    increment = float(values[-1] - values[0]) / (values.size - 1)
    return float(-values[0] / increment), increment


def describe_dataset(ds, variable: Optional[str] = None) -> Dict[str, Any]:
    '''What an image dataset holds, in plain values:

    ``variable``, ``dims`` (of the variable), ``sizes`` (``l``, ``m``,
    ``polarization``, ``frequency``; 1 for a dim it does not have),
    ``units``, ``polarization`` (labels), ``frequency`` (Hz, an array, or
    None), ``spectral`` (``frame``, ``rest_frequency`` Hz or None,
    ``velocity_convention``), ``direction`` (``frame``, ``equinox`` --
    xradio's names, ``fk5`` and ``j2000.0`` --, ``projection``,
    ``projection_parameters``, ``reference_value`` (rad),
    ``reference_pixel``, ``increment`` (rad), ``pc`` (as FITS ``PCi_j``),
    ``lonpole`` and ``latpole`` (deg)) or None, ``beam`` (the variable's
    name or None), ``flag`` (the default flag variable's name or None),
    ``time`` (MJD days or None), ``telescope``, ``observer``.'''
    variable = sky_variable(ds, variable)
    sky = ds[variable]
    for dim in (DIM_L, DIM_M):
        if dim not in sky.dims:
            raise ValueError(f'{variable} has no {dim!r} dim (dims: {sky.dims}): not a sky image')
    extra = [d for d in sky.dims if d not in IMAGE_DIMS]
    if extra:
        raise ValueError(f'{variable} has dims {extra}, which a sky image here does not have')
    sizes = {d: int(sky.sizes.get(d, 1)) for d in (DIM_L, DIM_M, DIM_POLARIZATION, DIM_FREQUENCY, DIM_TIME)}

    labels: List[str] = []
    if DIM_POLARIZATION in sky.dims and DIM_POLARIZATION in ds.coords:
        labels = [str(v) for v in ds[DIM_POLARIZATION].values]

    frequency, spectral = None, None
    if DIM_FREQUENCY in sky.dims and DIM_FREQUENCY in ds.coords:
        fc = ds[DIM_FREQUENCY]
        frequency = np.asarray(fc.values, dtype=np.float64)
        convention = fc.attrs.get('velocity_convention')
        if convention is None and 'velocity' in ds.coords:
            convention = ds['velocity'].attrs.get('doppler_type')
        spectral = {'frame': str(fc.attrs.get('frame', '')),
                    'rest_frequency': _scalar(fc.attrs.get('rest_frequency')),
                    'velocity_convention': str(convention or 'radio')}
        if spectral['rest_frequency'] is not None and spectral['rest_frequency'] <= 0:
            spectral['rest_frequency'] = None

    direction = None
    info = ds.attrs.get('coordinate_system_info')
    if isinstance(info, dict) and 'reference_direction' in info and DIM_L in ds.coords and DIM_M in ds.coords:
        reference = info['reference_direction']
        ref_attrs = reference.get('attrs', {})
        scale = {'rad': 1.0, 'deg': np.pi / 180.0}.get(str(ref_attrs.get('units', 'rad')), 1.0)
        crpix_l, cdelt_l = _axis(ds[DIM_L].values)
        crpix_m, cdelt_m = _axis(ds[DIM_M].values)
        mine = ds.attrs.get('direction') or {}                    # the casa-image engine's exact values
        pole = (info.get('native_pole_direction') or {}).get('data') or [np.pi, 0.0]
        pc = info.get('pixel_coordinate_transformation_matrix') or [[1.0, 0.0], [0.0, 1.0]]
        direction = {
            'frame': str(ref_attrs.get('frame', 'fk5')).lower(),
            'equinox': str(ref_attrs['equinox']).lower() if ref_attrs.get('equinox') else None,
            'projection': str(info.get('projection', 'SIN')),
            'projection_parameters': [float(v) for v in info.get('projection_parameters') or []],
            'reference_value': [float(v) * scale for v in reference['data'][:2]],
            'reference_pixel': [float(v) for v in mine.get('reference_pixel', [crpix_l, crpix_m])],
            'increment': [float(v) if v is not None else None
                          for v in mine.get('increment', [cdelt_l, cdelt_m])],
            'pc': [[float(v) for v in row] for row in pc],
            'lonpole': float(np.degrees(pole[0])),
            'latpole': float(np.degrees(pole[1])),
        }

    time = None
    if DIM_TIME in ds.coords and ds[DIM_TIME].size:
        time = _scalar(ds[DIM_TIME].values)

    telescope = ds.attrs.get('telescope')
    if telescope is None and isinstance(sky.attrs.get('telescope'), dict):
        telescope = sky.attrs['telescope'].get('name')
    return {
        'variable': variable,
        'dims': tuple(sky.dims),
        'sizes': sizes,
        'dtype': str(sky.dtype),
        'units': str(sky.attrs.get('units', '') or ''),
        'polarization': labels,
        'frequency': frequency,
        'spectral': spectral,
        'direction': direction,
        'beam': _beam_variable(ds, variable),
        'flag': _flag_variable(ds, variable),
        'time': time,
        'telescope': str(telescope) if telescope else None,
        'observer': str(ds.attrs['observer']) if ds.attrs.get('observer') else None,
    }
