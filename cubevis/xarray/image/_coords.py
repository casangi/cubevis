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
'''What a CASA image's coordinate system record says, in numpy: which
pixel axis is what, the frequency of each channel, the Stokes labels, the
direction axes, the beam.

The record is the image's ``coords`` keyword in casatools' layout
(``CasaImage.coords_record()``, the same as ``coordsys().torecord()``).
It holds one entry per coordinate, named by kind and number --
``direction0``, ``stokes1``, ``spectral2``, also ``linear<n>`` and
``tabular<n>`` -- and for coordinate ``n`` a ``pixelmap<n>``: the pixel
axis of each of its axes (-1 for an axis the image no longer has).

No CASA, no astropy except in ``sky_coordinates``.
'''

import math
from typing import Any, Dict, List, NamedTuple, Optional, Sequence

import numpy as np

COORDINATE_KINDS = ('direction', 'spectral', 'stokes', 'linear', 'tabular')

SPEED_OF_LIGHT = 299792458.0            # m/s

#: angle unit (as casacore writes it) -> radians
ANGLE_TO_RAD = {'rad': 1.0, 'deg': math.pi / 180.0,
                "'": math.pi / 10800.0, 'arcmin': math.pi / 10800.0,
                '"': math.pi / 648000.0, "''": math.pi / 648000.0, 'arcsec': math.pi / 648000.0,
                'mas': math.pi / 648000.0e3, 'marcsec': math.pi / 648000.0e3}

FREQUENCY_TO_HZ = {'Hz': 1.0, 'kHz': 1e3, 'MHz': 1e6, 'GHz': 1e9, 'THz': 1e12}

#: casacore MDoppler types (the spectral coordinate's ``velType``)
VELOCITY_CONVENTIONS = {0: 'radio', 1: 'optical', 2: 'ratio', 3: 'beta', 4: 'gamma'}


class Axis(NamedTuple):
    '''One pixel axis of an image.'''
    kind: str               # 'direction', 'spectral', 'stokes', 'linear', 'tabular' or 'unknown'
    key: Optional[str]      # the coordinate's key in the record ('direction0'), None when unknown
    index: int              # which axis of that coordinate (0 = longitude, 1 = latitude for a direction)
    name: str               # the axis name in the record ('Right Ascension', 'Frequency', ...)


def coordinate_keys(record: Dict[str, Any]) -> List[str]:
    '''The record's coordinates in order: ``['direction0', 'stokes1', ...]``.'''
    found = []
    for key in record:
        for kind in COORDINATE_KINDS:
            if key.startswith(kind) and key[len(kind):].isdigit():
                found.append((int(key[len(kind):]), key))
    return [key for _, key in sorted(found)]


def _kind(key: str) -> str:
    return key.rstrip('0123456789')


def _axis_names(key: str, coordinate: Dict[str, Any]) -> List[str]:
    if _kind(key) == 'spectral':
        return [str(coordinate.get('name', 'Frequency'))]
    names = coordinate.get('axes')
    if _kind(key) == 'stokes' and not names:
        return ['Stokes']
    return [str(n) for n in names or []]


def axes(record: Dict[str, Any], ndim: int) -> List[Axis]:
    '''What each of the image's ``ndim`` pixel axes is.'''
    out = [Axis('unknown', None, 0, f'axis{i}') for i in range(ndim)]
    for key in coordinate_keys(record):
        number = key[len(_kind(key)):]
        names = _axis_names(key, record[key])
        pixelmap = record.get(f'pixelmap{number}')
        if pixelmap is None:
            continue
        for index, pixel_axis in enumerate(np.atleast_1d(pixelmap)):
            pixel_axis = int(pixel_axis)
            if 0 <= pixel_axis < ndim:
                name = names[index] if index < len(names) else f'{key}_{index}'
                out[pixel_axis] = Axis(_kind(key), key, index, name)
    return out


def find_axis(image_axes: Sequence[Axis], kind: str, index: int = 0) -> Optional[int]:
    '''The pixel axis of the given kind (and index within its coordinate),
    None when the image has none.'''
    for pixel_axis, axis in enumerate(image_axes):
        if axis.kind == kind and axis.index == index:
            return pixel_axis
    return None


# ---- spectral ---------------------------------------------------------------

def spectral_values(coordinate: Dict[str, Any], nchan: int) -> np.ndarray:
    '''The frequency of each channel, in Hz.

    A linear axis is ``wcs``: ``crval + (channel - crpix) * cdelt`` (casacore
    keeps it in Hz).  A tabular axis is ``tabular``: world values at pixel
    values, interpolated (and extrapolated from the end pairs, as casacore
    does).'''
    channels = np.arange(nchan, dtype=np.float64)
    table = coordinate.get('tabular')
    if table and len(np.atleast_1d(table.get('worldvalues', []))) > 0:
        pixels = np.atleast_1d(np.asarray(table['pixelvalues'], dtype=np.float64))
        world = np.atleast_1d(np.asarray(table['worldvalues'], dtype=np.float64))
        unit = str(np.atleast_1d(table.get('units', ['Hz']))[0])
        if len(world) == 1:
            cdelt = float(np.atleast_1d(table.get('cdelt', [0.0]))[0])
            values = world[0] + (channels - pixels[0]) * cdelt
        else:
            values = np.interp(channels, pixels, world)
            below, above = channels < pixels[0], channels > pixels[-1]
            if below.any():
                slope = (world[1] - world[0]) / (pixels[1] - pixels[0])
                values[below] = world[0] + (channels[below] - pixels[0]) * slope
            if above.any():
                slope = (world[-1] - world[-2]) / (pixels[-1] - pixels[-2])
                values[above] = world[-1] + (channels[above] - pixels[-1]) * slope
        return values * FREQUENCY_TO_HZ.get(unit, 1.0)
    wcs = coordinate.get('wcs', {})
    crval, cdelt = float(wcs.get('crval', 0.0)), float(wcs.get('cdelt', 1.0))
    crpix, pc = float(wcs.get('crpix', 0.0)), float(wcs.get('pc', 1.0))
    return crval + (channels - crpix) * cdelt * pc


def rest_frequency(coordinate: Dict[str, Any]) -> Optional[float]:
    '''Hz; None when the image has none (casacore writes 0 or less).'''
    value = coordinate.get('restfreq')
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def velocity_convention(coordinate: Dict[str, Any]) -> str:
    return VELOCITY_CONVENTIONS.get(int(coordinate.get('velType', 0)), 'radio')


def velocity_values(frequency: np.ndarray, rest: float, convention: str = 'radio') -> np.ndarray:
    '''Velocity (m/s) of each frequency for a rest frequency, in the image's
    Doppler convention.'''
    frequency = np.asarray(frequency, dtype=np.float64)
    with np.errstate(divide='ignore', invalid='ignore'):
        if convention == 'optical':
            return SPEED_OF_LIGHT * (rest / frequency - 1.0)
        if convention == 'ratio':
            return SPEED_OF_LIGHT * (frequency / rest)
        if convention == 'beta':
            return SPEED_OF_LIGHT * (rest ** 2 - frequency ** 2) / (rest ** 2 + frequency ** 2)
        if convention == 'gamma':
            return SPEED_OF_LIGHT * (rest ** 2 + frequency ** 2) / (2.0 * rest * frequency)
        return SPEED_OF_LIGHT * (1.0 - frequency / rest)


# ---- stokes -------------------------------------------------------------------

def stokes_labels(coordinate: Dict[str, Any]) -> List[str]:
    return [str(s) for s in coordinate.get('stokes', [])]


# ---- direction ----------------------------------------------------------------

def direction(coordinate: Dict[str, Any]) -> Dict[str, Any]:
    '''The direction coordinate in radians: ``frame`` (CASA's name: J2000,
    ICRS, GALACTIC, ...), ``projection``, ``projection_parameters``,
    ``reference_value``, ``reference_pixel`` (0-based), ``increment``,
    ``pc`` (``pc[i][j]``: world axis i, pixel axis j, as FITS ``PCi_j``),
    ``lonpole`` and ``latpole`` (degrees), ``axis_names``.'''
    units = [str(u) for u in coordinate.get('units', ['rad', 'rad'])]
    scale = [ANGLE_TO_RAD.get(u) for u in units]
    if None in scale:
        raise ValueError(f'direction axes in unknown units {units}')
    ### casatools' layout of the record has pc transposed relative to FITS
    rpc = coordinate.get('pc', [[1.0, 0.0], [0.0, 1.0]])
    pc = [[float(rpc[j][i]) for j in range(2)] for i in range(2)]
    return {
        'frame': str(coordinate.get('system', '')),
        'projection': str(coordinate.get('projection', 'SIN')),
        'projection_parameters': [float(v) for v in np.atleast_1d(coordinate.get('projection_parameters', []))],
        'reference_value': [float(coordinate['crval'][i]) * scale[i] for i in range(2)],
        'reference_pixel': [float(coordinate['crpix'][i]) for i in range(2)],
        'increment': [float(coordinate['cdelt'][i]) * scale[i] for i in range(2)],
        'pc': pc,
        'lonpole': float(coordinate['longpole']) if 'longpole' in coordinate else None,
        'latpole': float(coordinate['latpole']) if 'latpole' in coordinate else None,
        'axis_names': [str(n) for n in coordinate.get('axes', ['Right Ascension', 'Declination'])],
    }


def offset_values(info: Dict[str, Any], index: int, n: int) -> np.ndarray:
    '''Offsets of the pixels along direction axis ``index`` from the
    reference pixel, in radians: ``(pixel - reference_pixel) * increment``.
    These are xradio's ``l`` and ``m`` coordinates; they ignore rotation
    (``pc``), which ``sky_coordinates`` does not.'''
    return (np.arange(n, dtype=np.float64) - info['reference_pixel'][index]) * info['increment'][index]


_FITS_AXES = {'GALACTIC': ('GLON', 'GLAT'), 'ECLIPTIC': ('ELON', 'ELAT'), 'SUPERGAL': ('SLON', 'SLAT')}


def sky_coordinates(info: Dict[str, Any], nx: int, ny: int):
    '''Longitude and latitude (radians, each ``[x, y]``) of every pixel of
    the direction plane, in the image's own frame, with astropy's WCS.'''
    from astropy.wcs import WCS                         # pylint: disable=import-outside-toplevel
    w = WCS(naxis=2)
    lon, lat = _FITS_AXES.get(info['frame'].upper(), ('RA', 'DEC'))
    w.wcs.ctype = [f'{lon:-<4}-{info["projection"]}', f'{lat:-<4}-{info["projection"]}']
    w.wcs.crval = [math.degrees(v) for v in info['reference_value']]
    w.wcs.crpix = [p + 1.0 for p in info['reference_pixel']]
    w.wcs.cdelt = [math.degrees(v) for v in info['increment']]
    w.wcs.cunit = ['deg', 'deg']
    w.wcs.pc = np.array(info['pc'], dtype=float)
    if info.get('lonpole') is not None:
        w.wcs.lonpole = info['lonpole']
    if info.get('latpole') is not None:
        w.wcs.latpole = info['latpole']
    pv = [(2, m, v) for m, v in enumerate(info.get('projection_parameters') or []) if v]
    if pv:
        w.wcs.set_pv(pv)
    w.wcs.set()
    x, y = np.meshgrid(np.arange(nx, dtype=np.float64), np.arange(ny, dtype=np.float64), indexing='ij')
    lon_deg, lat_deg = w.wcs_pix2world(x, y, 0)
    return np.radians(lon_deg), np.radians(lat_deg)


# ---- linear and other axes ----------------------------------------------------

def linear_values(coordinate: Dict[str, Any], index: int, n: int) -> np.ndarray:
    '''World values along axis ``index`` of a linear (or tabular) coordinate,
    in the record's units, ignoring any coupling between its axes.'''
    table_pix = coordinate.get('pixelvalues')
    if table_pix is not None and len(np.atleast_1d(table_pix)) > 1:
        return np.interp(np.arange(n, dtype=np.float64),
                         np.asarray(table_pix, dtype=np.float64),
                         np.asarray(coordinate['worldvalues'], dtype=np.float64))
    crval = float(np.atleast_1d(coordinate.get('crval', [0.0] * (index + 1)))[index])
    crpix = float(np.atleast_1d(coordinate.get('crpix', [0.0] * (index + 1)))[index])
    cdelt = float(np.atleast_1d(coordinate.get('cdelt', [1.0] * (index + 1)))[index])
    return crval + (np.arange(n, dtype=np.float64) - crpix) * cdelt


# ---- beam -----------------------------------------------------------------------

def restoring_beam(imageinfo: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    '''The beam as ``ia.restoringbeam()`` gives it: ``{'major': {'value',
    'unit'}, 'minor': ..., 'positionangle': ...}`` for one beam; for a beam
    per plane ``{'nChannels', 'nStokes', 'beams': {'*<chan>': {'*<stokes>':
    beam}}}``; None when the image has none.

    The keyword keeps per-plane beams flat, ``*0 ... *N``, channel varying
    fastest.'''
    if not imageinfo:
        return None
    if imageinfo.get('restoringbeam'):
        return dict(imageinfo['restoringbeam'])
    flat = imageinfo.get('perplanebeams')
    if not flat:
        return None
    nchan, nstokes = int(flat.get('nChannels', 1)), int(flat.get('nStokes', 1))
    beams: Dict[str, Dict[str, Any]] = {}
    for chan in range(nchan):
        beams[f'*{chan}'] = {f'*{s}': dict(flat[f'*{chan + nchan * s}']) for s in range(nstokes)}
    return {'nChannels': nchan, 'nStokes': nstokes, 'beams': beams}


def _to_rad(quantity: Dict[str, Any]) -> float:
    return float(quantity['value']) * ANGLE_TO_RAD[str(quantity['unit'])]


def beam_array(imageinfo: Dict[str, Any], nchan: int, nstokes: int) -> Optional[np.ndarray]:
    '''``[chan, stokes, 3]``: major axis, minor axis and position angle of
    the beam of each plane, in radians; None without a beam.'''
    beam = restoring_beam(imageinfo)
    if beam is None:
        return None
    out = np.empty((nchan, nstokes, 3), dtype=np.float64)

    def one(b):
        return [_to_rad(b['major']), _to_rad(b['minor']), _to_rad(b['positionangle'])]

    if 'major' in beam:
        out[:, :] = one(beam)
        return out
    for chan in range(nchan):
        row = beam['beams'][f'*{min(chan, beam["nChannels"] - 1)}']
        for s in range(nstokes):
            out[chan, s] = one(row[f'*{min(s, beam["nStokes"] - 1)}'])
    return out


def beam_area_pixels(beam: Optional[np.ndarray], increment: Sequence[float]) -> Optional[float]:
    '''Pixels per beam for one beam ``[major, minor, pa]`` (radians) and the
    direction increments (radians): what turns a sum in Jy/beam into Jy.'''
    if beam is None:
        return None
    pixel = abs(increment[0] * increment[1])
    if pixel == 0:
        return None
    return math.pi / (4.0 * math.log(2.0)) * float(beam[0]) * float(beam[1]) / pixel
