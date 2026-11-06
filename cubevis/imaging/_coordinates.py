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
'''The coordinate description of an image cube (design §2.5): plain,
JSON-safe data that does not depend on the image format, in place of a FITS
header (a CASA export format, which for example writes a non-linear
spectral axis as an approximate optical velocity) or a casatools
``coordsys``.

Every ``ImageStore`` provides one (``coordinates()``); the browser receives
it as ``ImagePipe.coords_json`` and gives ``coordtxl`` the keywords it reads
(``cubevisjs/src/bokeh/sources/coordinate_description.ts``); Python makes an
astropy ``WCS`` from it with ``astropy_wcs``.

Shape of a description (version 1)::

    {
      "version": 1,
      "shape": [nx, ny, nstokes, nchan],
      "pixel_origin": 0,                      # pixel indices are 0-based (FITS CRPIX is 1-based)
      "direction": {
          "frame": "FK5" | "ICRS" | "FK4" | "GALACTIC" | "ECLIPTIC" | "SUPERGALACTIC" | <other>,
          "equinox": 2000.0 | 1950.0 | None,
          "projection": "SIN", "projection_parameters": [...],
          "reference_value": [lon_deg, lat_deg],
          "reference_pixel": [x, y],          # 0-based
          "increment": [dlon_deg, dlat_deg],
          "pc": [[1, 0], [0, 1]],             # FITS convention: pc[i][j] = PC(i+1)_(j+1)
          "lonpole": deg | None, "latpole": deg | None,
          "native_frame": "J2000"             # the image's own name for the frame (informational)
      },
      "spectral": None | {
          "frame": "LSRK", "unit": "Hz",
          "values": [...],                    # one per channel
          "rest_frequency": Hz | None, "velocity_convention": "radio" | "optical" | ...
      },
      "stokes": ["I", ...],
      "beam": None | {"major": arcsec, "minor": arcsec, "pa": deg}
                   | {"per_plane": [[{"major":..,"minor":..,"pa":..} per stokes] per channel]},
      "brightness_unit": "Jy/beam",
      "extra": {"telescope": ..., "observer": ..., "obsdate": ...}
    }

Nothing here imports CASA; ``astropy_wcs`` imports astropy when called.
'''

import math
from typing import Any, Dict, List, Optional, Sequence

VERSION = 1

#: a CASA direction reference -> (frame, equinox) in the description
CASA_DIRECTION_FRAMES = {
    'J2000':     ('FK5', 2000.0),
    'JMEAN':     ('FK5', 2000.0),
    'JTRUE':     ('FK5', 2000.0),
    'ICRS':      ('ICRS', None),
    'B1950':     ('FK4', 1950.0),
    'B1950_VLA': ('FK4', 1950.0),
    'BMEAN':     ('FK4', 1950.0),
    'BTRUE':     ('FK4', 1950.0),
    'GALACTIC':  ('GALACTIC', None),
    'ECLIPTIC':  ('ECLIPTIC', None),
    'SUPERGAL':  ('SUPERGALACTIC', None),
}

#: FITS STOKES axis codes
STOKES_CODES = {'I': 1, 'Q': 2, 'U': 3, 'V': 4,
                'RR': -1, 'LL': -2, 'RL': -3, 'LR': -4,
                'XX': -5, 'YY': -6, 'XY': -7, 'YX': -8}

#: CASA MDoppler type numbers (the spectral record's velType)
VELOCITY_CONVENTIONS = {0: 'radio', 1: 'optical', 2: 'ratio', 3: 'beta', 4: 'gamma'}

_TO_DEG = {'rad': 180.0 / math.pi, 'deg': 1.0, 'arcmin': 1.0 / 60.0, 'arcsec': 1.0 / 3600.0}
_TO_ARCSEC = {'rad': 180.0 / math.pi * 3600.0, 'deg': 3600.0, 'arcmin': 60.0, 'arcsec': 1.0}


def _floats(values) -> List[float]:
    return [float(v) for v in values]


def _quantity(q, table) -> float:
    return float(q['value']) * table[q['unit']]


def _find(record: dict, prefix: str):
    '''the coordinate in a casatools coordsys record whose key starts with
    ``prefix`` ("direction", "spectral", "stokes"), and its index'''
    for key in sorted(record):
        if key.startswith(prefix) and key[len(prefix):].isdigit():
            return record[key], int(key[len(prefix):])
    return None, None


def linear_spectral_values(crval: float, cdelt: float, crpix: float, nchan: int) -> List[float]:
    '''channel values of a linear spectral axis (``crpix`` 0-based)'''
    return [float(crval + (c - crpix) * cdelt) for c in range(nchan)]


def beam_from_casa(beam: Optional[dict]) -> Optional[dict]:
    '''``ia.restoringbeam()`` -> the description's ``beam``'''
    if not beam:
        return None

    def one(b):
        return {'major': _quantity(b['major'], _TO_ARCSEC),
                'minor': _quantity(b['minor'], _TO_ARCSEC),
                'pa': _quantity(b['positionangle'], _TO_DEG)}

    if 'major' in beam:
        return one(beam)
    if 'beams' in beam:
        nchan, nstokes = int(beam['nChannels']), int(beam['nStokes'])
        return {'per_plane': [[one(beam['beams'][f'*{c}'][f'*{s}']) for s in range(nstokes)]
                              for c in range(nchan)]}
    return None


def from_casa_coordsys(record: dict, shape: Sequence[int], *,
                       spectral_values: Optional[Sequence[float]] = None,
                       beam: Optional[dict] = None, brightness_unit: str = '') -> Dict[str, Any]:
    '''The description of a CASA image from its coordinate system record
    (``coordsys().torecord()``).

    ``spectral_values`` (Hz, one per channel) should come from the coordinate
    system itself (``toworldmany``), which handles tabular and non-linear
    axes; without them the record's linear ``wcs`` is used.  ``beam`` is
    ``ia.restoringbeam()``.'''
    shape = [int(s) for s in shape]
    nchan = shape[3] if len(shape) > 3 else 1

    direction = None
    dc, _ = _find(record, 'direction')
    if dc is not None:
        units = list(dc.get('units', ['rad', 'rad']))
        native = str(dc.get('system', ''))
        frame, equinox = CASA_DIRECTION_FRAMES.get(native, (native, None))
        # the record's pc is the transpose of FITS PCi_j (CASA arrays are column-major);
        # the description follows FITS: pc[i][j] = PC(i+1)_(j+1), i the world axis
        rpc = dc.get('pc', [[1.0, 0.0], [0.0, 1.0]])
        pc = [[float(rpc[j][i]) for j in range(2)] for i in range(2)]
        direction = {
            'frame': frame,
            'equinox': equinox,
            'projection': str(dc.get('projection', 'SIN')),
            'projection_parameters': _floats(dc.get('projection_parameters', [])),
            'reference_value': [float(dc['crval'][i]) * _TO_DEG[units[i]] for i in range(2)],
            'reference_pixel': _floats(dc['crpix'][:2]),
            'increment': [float(dc['cdelt'][i]) * _TO_DEG[units[i]] for i in range(2)],
            'pc': pc,
            'lonpole': float(dc['longpole']) if 'longpole' in dc else None,
            'latpole': float(dc['latpole']) if 'latpole' in dc else None,
            'native_frame': native,
        }

    spectral = None
    sc, _ = _find(record, 'spectral')
    if sc is not None:
        if spectral_values is None:
            wcs = sc.get('wcs', {})
            spectral_values = linear_spectral_values(float(wcs.get('crval', 0.0)), float(wcs.get('cdelt', 1.0)),
                                                     float(wcs.get('crpix', 0.0)), nchan)
        restfreq = sc.get('restfreq')
        spectral = {
            'frame': str(sc.get('system', '')),
            'unit': str(sc.get('unit', 'Hz')),
            'values': _floats(spectral_values),
            'rest_frequency': float(restfreq) if restfreq else None,
            'velocity_convention': VELOCITY_CONVENTIONS.get(int(sc.get('velType', 0)), 'radio'),
        }

    stc, _ = _find(record, 'stokes')
    stokes = [str(s) for s in stc['stokes']] if stc is not None else []

    extra = {}
    for key in ('telescope', 'observer'):
        if record.get(key):
            extra[key] = str(record[key])
    obsdate = record.get('obsdate')
    if isinstance(obsdate, dict) and 'm0' in obsdate:
        extra['obsdate'] = {'value': float(obsdate['m0']['value']), 'unit': str(obsdate['m0']['unit']),
                            'refer': str(obsdate.get('refer', ''))}

    return {
        'version': VERSION,
        'shape': shape,
        'pixel_origin': 0,
        'direction': direction,
        'spectral': spectral,
        'stokes': stokes,
        'beam': beam_from_casa(beam),
        'brightness_unit': str(brightness_unit or ''),
        'extra': extra,
    }


def celestial_axis_names(frame: str):
    '''FITS longitude/latitude axis names for a frame'''
    frame = (frame or '').upper()
    if frame.startswith('GAL'):
        return 'GLON', 'GLAT'
    if frame.startswith('ECL'):
        return 'ELON', 'ELAT'
    if frame.startswith('SUPERGAL'):
        return 'SLON', 'SLAT'
    return 'RA', 'DEC'


def fits_ctype(name: str, projection: str) -> str:
    '''``RA---SIN``, ``DEC--SIN``, ``GLON-SIN``'''
    return f'{name:-<4}-{projection}'


def astropy_wcs(desc: Dict[str, Any], naxis: int = 4):
    '''An astropy ``WCS`` for a coordinate description: the two direction
    axes, then (``naxis`` 3 or 4) Stokes and the spectral axis, in the
    image's pixel axis order.  The spectral axis is linear (``FREQ``,
    first channel and the step to the second); for an irregular axis that
    is an approximation away from the first two channels -- the direction
    axes, which regions use, are exact.'''
    from astropy.wcs import WCS             # pylint: disable=import-outside-toplevel
    import numpy as np                      # pylint: disable=import-outside-toplevel

    d = desc['direction']
    if d is None:
        raise ValueError('the description has no direction coordinate')
    lon, lat = celestial_axis_names(d['frame'])
    w = WCS(naxis=naxis)
    ctype = [fits_ctype(lon, d['projection']), fits_ctype(lat, d['projection'])]
    crval = list(d['reference_value'])
    crpix = [p + 1 - desc.get('pixel_origin', 0) for p in d['reference_pixel']]    # FITS: 1-based
    cdelt = list(d['increment'])
    cunit = ['deg', 'deg']
    pc = np.identity(naxis)
    pc[0:2, 0:2] = np.array(d['pc'], dtype=float)

    if naxis >= 3:
        codes = [STOKES_CODES.get(s, 1) for s in desc.get('stokes') or ['I']]
        ctype.append('STOKES')
        crval.append(float(codes[0]))
        crpix.append(1.0)
        cdelt.append(float(codes[1] - codes[0]) if len(codes) > 1 else 1.0)
        cunit.append('')
    if naxis >= 4:
        s = desc.get('spectral') or {'values': [0.0], 'unit': 'Hz'}
        values = s['values'] or [0.0]
        ctype.append('FREQ')
        crval.append(float(values[0]))
        crpix.append(1.0)
        cdelt.append(float(values[1] - values[0]) if len(values) > 1 else 1.0)
        cunit.append(s.get('unit') or 'Hz')

    w.wcs.ctype = ctype
    w.wcs.crval = crval
    w.wcs.crpix = crpix
    w.wcs.cdelt = cdelt
    w.wcs.cunit = cunit
    w.wcs.pc = pc
    frame = (d['frame'] or '').upper()
    if frame in ('FK5', 'FK4', 'ICRS'):
        w.wcs.radesys = frame
    if d.get('equinox') is not None:
        w.wcs.equinox = float(d['equinox'])
    if d.get('lonpole') is not None:
        w.wcs.lonpole = float(d['lonpole'])
    if d.get('latpole') is not None:
        w.wcs.latpole = float(d['latpole'])
    pv = [(2, m, float(v)) for m, v in enumerate(d.get('projection_parameters') or [])]
    if pv:
        w.wcs.set_pv(pv)
    if naxis >= 4 and desc.get('spectral'):
        if desc['spectral'].get('frame'):
            w.wcs.specsys = desc['spectral']['frame']
        if desc['spectral'].get('rest_frequency'):
            w.wcs.restfrq = float(desc['spectral']['rest_frequency'])
    w.wcs.set()
    return w
