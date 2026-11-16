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
'''The names the ``casa-image`` engine uses, in one place, and where its
datasets leave xradio's image dataset (``xradio.image``; schema 0.0.3 as
xradio 1.3.1 writes it, compared 2026-11-16 on the same CASA images:
``SKY``, ``FLAG_SKY`` and ``BEAM_FIT_PARAMS_SKY`` values, ``frequency``,
``l``, ``m`` and ``coordinate_system_info`` are the same).

The layout is xradio's so that code written for one reads the other
(``describe_dataset`` reads both).  This backend does not claim to conform:
it needs only arcae, numpy and xarray, and keeps every image readable rather
than refusing what the schema has no place for.
'''

import re
from typing import Any, Dict

SCHEMA_VERSION = 2

#: the xradio schema this layout was compared with
XRADIO_SCHEMA_VERSION = '0.0.3'

DIM_TIME = 'time'
DIM_FREQUENCY = 'frequency'
DIM_POLARIZATION = 'polarization'
DIM_L = 'l'
DIM_M = 'm'

BEAM_PARAMS_LABELS = ('major', 'minor', 'pa')

#: CASA direction reference -> (xradio frame, equinox), as xradio converts them
XRADIO_FRAMES = {'J2000': ('fk5', 'j2000.0'), 'B1950': ('fk4', 'b1950.0'), 'ICRS': ('icrs', None),
                 'GALACTIC': ('galactic', None), 'SUPERGAL': ('supergalactic', None)}
#: and back
CASA_FRAMES = {('fk5', 'j2000.0'): 'J2000', ('fk4', 'b1950.0'): 'B1950', ('icrs', None): 'ICRS',
               ('galactic', None): 'GALACTIC', ('supergalactic', None): 'SUPERGAL'}

#: recorded in every dataset (``attrs["schema"]["deviations"]``)
DEVIATIONS = (
    'an axis the image does not have (frequency, polarization) is not a dim; xradio always has five',
    'the whole CASA coordinate system record is kept in attrs["coordinate_system"], with attrs["casa_image"], '
    'attrs["imageinfo"] and attrs["direction"] (reference pixel and increment, which xradio leaves to l and m)',
    'the pixel variable is named by the caller (default SKY), not from the image file name',
    'right_ascension and declination are added only on request (sky_coords=True)',
    'axes that are neither direction, spectral nor Stokes keep their CASA names as dims',
    'a pixel mask other than the default one is MASK_<NAME> (True = not valid)',
    'SKY.attrs has units, type and names of its flag and beam variables; no telescope or pointing measures',
    'frequency.attrs has no reference_frequency or wave_units; time is the observation date',
)


def quantity(value: float, units: str) -> Dict[str, Any]:
    '''A scalar as xradio writes quantities in attributes.'''
    return {'data': float(value), 'dims': [], 'attrs': {'units': units, 'type': 'quantity'}}


def mask_variable_name(casa_name: str) -> str:
    '''The variable of a pixel mask that is not the default one: ``mask1``
    -> ``MASK_1``; other names upper-cased after ``MASK_``, with anything
    but letters, digits and ``_`` replaced.'''
    name = re.sub(r'[^0-9A-Za-z_]+', '_', casa_name).upper()
    if re.match(r'^MASK\d+$', name):
        return re.sub(r'^MASK(\d+)$', r'MASK_\1', name)
    return name if name.startswith('MASK_') else f'MASK_{name}'
