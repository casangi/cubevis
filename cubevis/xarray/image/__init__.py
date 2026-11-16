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
'''``cubevis.xarray.image`` -- radio astronomy images as xarray datasets:
CASA images read and written with arcae (engine ``"casa-image"``; no
casatools), laid out as xradio's image datasets, and one way to open and
describe either.

* ``CasaImage(path)`` -- pixels by index (``im[x, y, stokes, chan]``,
  reads and writes of part of an image), keywords, the coordinate system
  record, pixel masks.  Needs arcae and numpy.
* ``xr.open_dataset(path, engine="casa-image")`` / ``open_casa_image(path)``
  -- a lazily read dataset in xradio's image layout.  Needs xarray too.
* ``open_image(source)`` -- a CASA image, an image in Zarr (xradio's, or
  one saved from here) or a dataset in hand; ``describe_dataset(ds)`` --
  what it holds in plain values, whichever of the two wrote it.
* ``write_image``, ``create_image``, ``default_coords``, ``to_casa_image``
  -- CASA images written with arcae alone, which casatools opens.

This package stands alone: it imports nothing else of cubevis (not
``cubevis.xarray.caltab`` either), so it can be moved out as it is.
``cubevis.imaging.ImageReader`` is cubevis's use of it.

Importing this package makes ``engine="casa-image"`` work by name from a
checkout too, where no entry point is installed (``_register.py``).  The
engine is only ever used when named: it does not compete with xradio's
``xradio_casa_image`` for ``xr.open_dataset(path)`` with no engine.

Record: ``devel/notes/drs/xarray-image/xarray_image_notes.md``.
'''

from ._image import CasaImage, PagedArray, is_casa_image, table_info
from ._write import create_image, write_image, default_coords, beam_record, put_keywords
from ._backend import CasaImageBackendEntrypoint, LazyPixels, open_casa_image, to_casa_image, describe
from ._dataset import open_image, describe_dataset, sky_variable, source_format
from ._register import register
from . import _coords as coords
from . import _schema as schema

register()

__all__ = ['CasaImage', 'PagedArray', 'is_casa_image', 'table_info',
           'create_image', 'write_image', 'default_coords', 'beam_record', 'put_keywords',
           'CasaImageBackendEntrypoint', 'LazyPixels', 'open_casa_image', 'to_casa_image', 'describe',
           'open_image', 'describe_dataset', 'sky_variable', 'source_format',
           'register', 'coords', 'schema']
