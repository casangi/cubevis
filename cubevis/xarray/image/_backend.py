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
'''The ``casa-image`` xarray engine: a CASA image as an ``xarray.Dataset``
whose pixels are read when they are used::

    import xarray as xr
    ds = xr.open_dataset("ngc5921.image", engine="casa-image")
    ds.SKY.isel(frequency=12, polarization=0).values          # reads one plane
    ds.SKY.sel(l=0, m=0, method="nearest").values             # reads one spectrum
    xr.open_dataset("big.image", engine="casa-image", chunks={})   # dask, a plane per chunk

Layout: xradio's image dataset (``xradio.image``, schema 0.0.3 as written
by xradio 1.3.1), so that code reading one reads the other; ``_schema.py``
lists where this one differs.

* dims ``time`` (length 1), ``frequency``, ``polarization``, ``l``, ``m``;
  an axis the image does not have is not a dim.  Any other axis keeps its
  own name (``linear`` coordinates: ``uu``, ``vv``, ``offset``, ...).
* ``SKY(time, frequency, polarization, l, m)`` -- the pixels;
* ``FLAG_SKY`` -- the image's default pixel mask, **True where a pixel is
  not valid** (the reverse of CASA's masks, as in xradio); any other pixel
  mask of the image is ``MASK_<NAME>``, the same way round;
* ``BEAM_FIT_PARAMS_SKY(time, frequency, polarization, beam_params_label)``
  in radians, when the image has a beam;
* coords: ``frequency`` (Hz), ``velocity(frequency)`` (m/s, when there is a
  rest frequency), ``polarization`` (labels), ``l`` and ``m`` (radians
  from the reference pixel), ``time`` (the observation date, MJD days);
  ``right_ascension(l, m)`` and ``declination(l, m)`` with
  ``sky_coords=True``;
* ``attrs["coordinate_system_info"]`` and ``attrs["data_groups"]`` as xradio
  writes them; ``attrs["coordinate_system"]`` -- the image's whole CASA
  coordinate system record (as ``coordsys().torecord()``), so nothing of it
  is lost.

Read-only, like every xarray backend; ``CasaImage`` writes pixels, and
``to_casa_image`` writes a dataset back as an image.
'''

import os
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from . import _coords as co
from ._image import CasaImage, IndexedArray, is_casa_image
from ._schema import (BEAM_PARAMS_LABELS, DEVIATIONS, DIM_FREQUENCY, DIM_L, DIM_M, DIM_POLARIZATION,
                      DIM_TIME, SCHEMA_VERSION, XRADIO_FRAMES, mask_variable_name, quantity)

try:
    from xarray.backends import BackendArray, BackendEntrypoint
except ImportError:                                  # the rest of the package works without xarray
    BackendArray = object                            # type: ignore
    BackendEntrypoint = object                       # type: ignore


class LazyPixels(BackendArray):
    '''The pixels (or one mask) of an image for xarray: indexed in the
    dataset's dim order, read from the table in CASA's axis order.

    ``order[i]`` is the CASA pixel axis of the array's axis ``i``; -1 is the
    ``time`` axis, which the image does not have.  ``invert`` turns a CASA
    mask (True = good) into a flag (True = not valid).'''

    def __init__(self, array: IndexedArray, order: Sequence[int], invert: bool = False):
        self.array = array
        self.order = tuple(int(a) for a in order)
        self.invert = invert
        self.shape = tuple(1 if a < 0 else array.shape[a] for a in self.order)
        self.dtype = np.dtype(bool) if invert else array.dtype

    def __getitem__(self, key):
        from xarray.core import indexing             # pylint: disable=import-outside-toplevel
        return indexing.explicit_indexing_adapter(key, self.shape, indexing.IndexingSupport.BASIC, self._read)

    def _read(self, key: tuple) -> np.ndarray:
        casa_key: List[Any] = [slice(None)] * self.array.ndim
        kept = []                                    # (casa axis, position in the result) of sliced axes
        out_shape = []
        position = 0
        for axis, k in zip(self.order, key):
            if isinstance(k, slice):
                n = len(range(*k.indices(1 if axis < 0 else self.array.shape[axis])))
                out_shape.append(n)
                if axis >= 0:
                    kept.append((axis, position))
                position += 1
            elif axis < 0 and int(k) not in (0, -1):
                raise IndexError(f'index {k} is out of bounds for the time axis of size 1')
            if axis >= 0:
                casa_key[axis] = k
        block = self.array[tuple(casa_key)]          # sliced axes, in CASA order
        ### CASA order -> the order asked for
        casa_sorted = sorted(kept)
        block = block.transpose([casa_sorted.index(pair) for pair in sorted(kept, key=lambda p: p[1])])
        if self.invert:
            block = ~block
        return np.ascontiguousarray(block).reshape(out_shape)


def _dim_name(axis: co.Axis, used: Iterable[str]) -> str:
    name = re.sub(r'[^0-9a-zA-Z]+', '_', axis.name).strip('_').lower() or axis.kind
    base, n = name, 1
    while name in used:
        n += 1
        name = f'{base}_{n}'
    return name


def describe(image: CasaImage) -> Dict[str, Any]:
    '''What ``open_casa_image`` needs to know of an image, without xarray:
    ``dims`` (dataset order), ``order`` (the CASA pixel axis of each, -1 for
    time), ``coords`` (name -> (dims, values, attrs)), ``direction``.'''
    record = image.coords_record()
    image_axes = co.axes(record, image.ndim)
    spectral_axis = co.find_axis(image_axes, 'spectral')
    stokes_axis = co.find_axis(image_axes, 'stokes')
    lon_axis = co.find_axis(image_axes, 'direction', 0)
    lat_axis = co.find_axis(image_axes, 'direction', 1)

    coords: Dict[str, Tuple[Tuple[str, ...], Any, Dict[str, Any]]] = {}
    dims: List[str] = [DIM_TIME]
    order: List[int] = [-1]

    obsdate = record.get('obsdate')
    if isinstance(obsdate, dict) and 'm0' in obsdate:
        coords[DIM_TIME] = ((DIM_TIME,), np.array([float(obsdate['m0']['value'])]),
                            {'type': 'time', 'units': str(obsdate['m0'].get('unit', 'd')),
                             'scale': str(obsdate.get('refer', 'UTC')).lower(), 'format': 'mjd'})

    if spectral_axis is not None:
        sc = record[image_axes[spectral_axis].key]
        freq = co.spectral_values(sc, image.shape[spectral_axis])
        rest = co.rest_frequency(sc)
        attrs = {'type': 'spectral_coord', 'units': 'Hz', 'frame': str(sc.get('system', '')),
                 'velocity_convention': co.velocity_convention(sc)}
        if rest is not None:
            attrs['rest_frequency'] = quantity(rest, 'Hz')
        if len(freq) > 1:
            attrs['channel_width'] = quantity(float(abs(freq[1] - freq[0])), 'Hz')
        coords[DIM_FREQUENCY] = ((DIM_FREQUENCY,), freq, attrs)
        if rest is not None:
            coords['velocity'] = ((DIM_FREQUENCY,), co.velocity_values(freq, rest, attrs['velocity_convention']),
                                  {'type': 'doppler', 'units': 'm/s', 'doppler_type': attrs['velocity_convention']})
        dims.append(DIM_FREQUENCY)
        order.append(spectral_axis)

    if stokes_axis is not None:
        labels = co.stokes_labels(record[image_axes[stokes_axis].key])
        if len(labels) != image.shape[stokes_axis]:
            labels = (labels + [f'stokes_{i}' for i in range(image.shape[stokes_axis])])[:image.shape[stokes_axis]]
        coords[DIM_POLARIZATION] = ((DIM_POLARIZATION,), np.array(labels, dtype=str), {})
        dims.append(DIM_POLARIZATION)
        order.append(stokes_axis)

    for pixel_axis, axis in enumerate(image_axes):
        if pixel_axis in (spectral_axis, stokes_axis, lon_axis, lat_axis):
            continue
        name = _dim_name(axis, dims + [DIM_L, DIM_M])
        if axis.key is not None and axis.kind in ('linear', 'tabular'):
            units = record[axis.key].get('units', [])
            unit = str(units[axis.index]) if axis.index < len(units) else ''
            coords[name] = ((name,), co.linear_values(record[axis.key], axis.index, image.shape[pixel_axis]),
                            {'units': unit, 'casa_axis': axis.name})
        dims.append(name)
        order.append(pixel_axis)

    direction = None
    if lon_axis is not None and lat_axis is not None:
        direction = co.direction(record[image_axes[lon_axis].key])
        for dim, index, pixel_axis in ((DIM_L, 0, lon_axis), (DIM_M, 1, lat_axis)):
            coords[dim] = ((dim,), co.offset_values(direction, index, image.shape[pixel_axis]),
                           {'units': 'rad', 'casa_axis': direction['axis_names'][index],
                            'note': 'offset from the reference pixel: (pixel - reference_pixel) * increment'})
            dims.append(dim)
            order.append(pixel_axis)
    else:                                            # half a direction coordinate: keep the axis, unnamed
        for pixel_axis in (lon_axis, lat_axis):
            if pixel_axis is not None:
                dims.append(_dim_name(image_axes[pixel_axis], dims))
                order.append(pixel_axis)

    return {'record': record, 'axes': image_axes, 'dims': dims, 'order': order, 'coords': coords,
            'direction': direction, 'spectral_axis': spectral_axis, 'stokes_axis': stokes_axis,
            'lon_axis': lon_axis, 'lat_axis': lat_axis}


def open_casa_image(path: str, *, variable: str = 'SKY', masks: bool = True, sky_coords: bool = False,
                    drop_variables: Optional[Iterable[str]] = None):
    '''Open the CASA image at ``path`` as an ``xarray.Dataset`` (see the
    module docstring); what ``xr.open_dataset(path, engine="casa-image")``
    calls.

    variable:
        name of the pixels' variable (``SKY``; xradio also uses
        ``SKY_RESIDUAL``, ``SKY_MODEL``, ``POINT_SPREAD_FUNCTION``, ...)
    masks:
        include the image's pixel masks (lazily, like the pixels)
    sky_coords:
        add ``right_ascension`` and ``declination`` of every pixel (needs
        astropy; two float64 planes held in memory)
    '''
    import xarray as xr                              # pylint: disable=import-outside-toplevel
    from xarray.core import indexing                 # pylint: disable=import-outside-toplevel

    image = CasaImage(os.fspath(path))
    info = describe(image)
    dims, order = info['dims'], info['order']
    drop = {drop_variables} if isinstance(drop_variables, str) else set(drop_variables or ())

    def lazy(array: IndexedArray, invert: bool, attrs: Dict[str, Any]):
        backend = LazyPixels(array, order, invert=invert)
        var = xr.Variable(dims, indexing.LazilyIndexedArray(backend), attrs)
        ### what a dask user gets with chunks={}: a plane per chunk
        var.encoding['preferred_chunks'] = {d: 1 for d in dims if d in (DIM_FREQUENCY, DIM_POLARIZATION)}
        return var

    data_vars: Dict[str, Any] = {}
    sky_attrs: Dict[str, Any] = {'type': 'sky', 'units': image.units}
    if image.imageinfo.get('imagetype'):
        sky_attrs['sub_type'] = str(image.imageinfo['imagetype']).replace(' ', '')
    if image.imageinfo.get('objectname'):
        sky_attrs['object_name'] = str(image.imageinfo['objectname'])

    mask_vars: Dict[str, str] = {}
    if masks:
        for name in image.mask_names:
            try:
                array = image.mask(name)
            except (NotImplementedError, FileNotFoundError, ValueError):
                continue                             # a region mask, or a mask whose table is gone
            if array is None or array.shape != image.shape:
                continue
            mask_vars[name] = f'FLAG_{variable}' if name == image.default_mask else mask_variable_name(name)
            if mask_vars[name] not in drop:
                data_vars[mask_vars[name]] = lazy(array, True, {'type': 'flag', 'casa_mask': name})
        if image.default_mask in mask_vars and mask_vars[image.default_mask] not in drop:
            sky_attrs['flag'] = mask_vars[image.default_mask]

    coords = {name: xr.Variable(d, values, attrs) for name, (d, values, attrs) in info['coords'].items()}

    nchan = image.shape[info['spectral_axis']] if info['spectral_axis'] is not None else 1
    nstokes = image.shape[info['stokes_axis']] if info['stokes_axis'] is not None else 1
    beam = co.beam_array(image.imageinfo, nchan, nstokes)
    beam_name = f'BEAM_FIT_PARAMS_{variable}'
    if beam is not None and beam_name not in drop:
        beam_dims = [DIM_TIME]
        values = beam[None, ...]
        if info['spectral_axis'] is None:
            values = values[:, 0]
        else:
            beam_dims.append(DIM_FREQUENCY)
        if info['stokes_axis'] is None:
            values = values[..., 0, :]
        else:
            beam_dims.append(DIM_POLARIZATION)
        data_vars[beam_name] = xr.Variable(beam_dims + ['beam_params_label'], values,
                                           {'type': 'beam_fit_params', 'units': 'rad'})
        coords['beam_params_label'] = xr.Variable(('beam_params_label',), np.array(BEAM_PARAMS_LABELS, dtype=str))
        sky_attrs['beam_fit_params'] = beam_name

    direction = info['direction']
    if sky_coords and direction is not None:
        lon, lat = co.sky_coordinates(direction, image.shape[info['lon_axis']], image.shape[info['lat_axis']])
        frame_attrs = {'units': 'rad', 'frame': direction['frame']}
        coords['right_ascension'] = xr.Variable((DIM_L, DIM_M), lon, dict(frame_attrs))
        coords['declination'] = xr.Variable((DIM_L, DIM_M), lat, dict(frame_attrs))

    if variable not in drop:
        data_vars = {variable: lazy(image, False, sky_attrs), **data_vars}

    record = info['record']
    group = {'sky': variable} if variable not in drop else {}
    if 'beam_fit_params' in sky_attrs:
        group['beam_fit_params_sky'] = beam_name
    if 'flag' in sky_attrs:
        group['flag'] = sky_attrs['flag']
    attrs: Dict[str, Any] = {
        'type': 'image_dataset',
        'data_groups': {'base': group},
        'casa_image': {
            'path': image.path,
            'shape': list(image.shape),
            'axes': [axis.name for axis in info['axes']],
            'pixel_axis_of_dim': {d: a for d, a in zip(dims, order) if a >= 0},
            'dtype': str(image.dtype),
            'tile_shape': list(image.tile_shape) if image.tile_shape else None,
            'masks': image.mask_names,
            'default_mask': image.default_mask,
            'files': image.files(),
            'concatenated': [m.path for m in image.members] if hasattr(image, 'members') else None,
        },
        'coordinate_system': record,
        'imageinfo': image.imageinfo,
        'schema': {'name': 'cubevis casa-image', 'version': SCHEMA_VERSION, 'deviations': list(DEVIATIONS)},
    }
    if image.miscinfo:
        attrs['miscinfo'] = image.miscinfo
    if direction is not None:
        attrs['direction'] = dict(direction)
        frame, equinox = XRADIO_FRAMES.get(direction['frame'].upper(), (direction['frame'].lower(), None))
        reference = {'units': 'rad', 'frame': frame, 'type': 'sky_coord'}
        if equinox:
            reference['equinox'] = equinox
        attrs['coordinate_system_info'] = {
            'reference_direction': {'attrs': reference, 'data': list(direction['reference_value']),
                                    'dims': 'sky_dir_label',
                                    'coords': {'sky_dir_label': {'data': ['ra', 'dec'], 'dims': 'sky_dir_label'}}},
            'native_pole_direction': {
                'attrs': {'frame': 'NATIVE_PROJECTION', 'type': 'location', 'units': 'rad'},
                'data': [float(np.radians(direction['lonpole'] if direction['lonpole'] is not None else 180.0)),
                         float(np.radians(direction['latpole'] if direction['latpole'] is not None else 0.0))],
                'dims': 'ellipsoid_dir_label',
                'coords': {'ellipsoid_dir_label': {'data': ['lon', 'lat'], 'dims': 'ellipsoid_dir_label'}}},
            'pixel_coordinate_transformation_matrix': direction['pc'],
            'projection': direction['projection'],
            'projection_parameters': list(direction['projection_parameters']),
        }
    for key in ('telescope', 'observer'):
        if record.get(key):
            attrs[key] = str(record[key])
    ds = xr.Dataset(data_vars, coords=coords, attrs=attrs)
    ds.encoding['source'] = image.path
    return ds


def to_casa_image(ds, path: str, *, variable: Optional[str] = None, overwrite: bool = False) -> CasaImage:
    '''Write a dataset from ``open_casa_image`` (or one shaped like it, with
    its ``coordinate_system`` attribute) as a CASA image, a plane at a time:
    pixels, the masks it has, the coordinate system, beam and units.'''
    from ._write import create_image                 # pylint: disable=import-outside-toplevel
    if variable is None:
        candidates = [n for n, v in ds.data_vars.items() if v.attrs.get('type') == 'sky']
        if len(candidates) != 1:
            raise ValueError(f'name the variable to write (sky variables: {candidates})')
        variable = candidates[0]
    sky = ds[variable]
    info = ds.attrs.get('casa_image') or {}
    axis_of = info.get('pixel_axis_of_dim')
    record = ds.attrs.get('coordinate_system')
    if not axis_of or not record:
        raise ValueError("the dataset needs the attributes 'coordinate_system' and "
                         "'casa_image' (pixel_axis_of_dim) that open_casa_image gives")
    casa_dims = [d for d, _ in sorted(axis_of.items(), key=lambda item: item[1])]
    if DIM_TIME in sky.dims:
        sky = sky.isel({DIM_TIME: 0})
    if set(casa_dims) != set(sky.dims):
        raise ValueError(f'{variable} has dims {sky.dims}; the coordinate system has {casa_dims}')
    sky = sky.transpose(*casa_dims)
    mask_names = {v.attrs['casa_mask']: n for n, v in ds.data_vars.items()
                  if v.attrs.get('type') == 'flag' and 'casa_mask' in v.attrs}
    default = next((m for m, n in mask_names.items() if n == ds[variable].attrs.get('flag')), None)
    dtype = sky.dtype if sky.dtype.kind in 'fc' else np.dtype(np.float32)
    image = create_image(path, sky.shape, dtype=dtype, coords=record, units=str(sky.attrs.get('units', '')),
                         imageinfo=ds.attrs.get('imageinfo'), miscinfo=ds.attrs.get('miscinfo'),
                         masks=list(mask_names), default_mask=default, overwrite=overwrite)
    targets = [(image, sky, False)]
    for casa_name, var_name in mask_names.items():
        flag = ds[var_name]
        if DIM_TIME in flag.dims:
            flag = flag.isel({DIM_TIME: 0})
        targets.append((image.mask(casa_name), flag.transpose(*casa_dims), True))
    plane_axes = sky.shape[2:]
    for target, source, invert in targets:
        with target.hold(readonly=False):
            for index in np.ndindex(*plane_axes):
                key = (slice(None), slice(None)) + index
                values = np.asarray(source[key].values)
                target[key] = ~values if invert else values
    return image


class CasaImageBackendEntrypoint(BackendEntrypoint):
    '''``xr.open_dataset(path, engine="casa-image")``.'''

    description = 'CASA images (casacore paged images), read with arcae'
    url = 'https://github.com/casangi/cubevis'
    open_dataset_parameters = ('filename_or_obj', 'drop_variables', 'variable', 'masks', 'sky_coords')

    def guess_can_open(self, filename_or_obj) -> bool:
        ### never by guessing: xradio registers an engine for CASA images too
        ### (xradio_casa_image), and xr.open_dataset(path) with no engine named
        ### should keep meaning what it meant before cubevis was installed
        return False

    def open_dataset(self, filename_or_obj, *, drop_variables=None, variable: str = 'SKY',
                     masks: bool = True, sky_coords: bool = False, **kwargs: Any):
        if kwargs:
            raise TypeError(f'casa-image: unexpected arguments {sorted(kwargs)}')
        if not is_casa_image(filename_or_obj):
            raise ValueError(f'{filename_or_obj!r} is not a CASA image (paged or concatenated)')
        return open_casa_image(os.fspath(filename_or_obj), variable=variable, masks=masks,
                               sky_coords=sky_coords, drop_variables=drop_variables)
