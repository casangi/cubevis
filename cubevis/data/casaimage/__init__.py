########################################################################
#
# Copyright (C) 2023, 2024
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
'''image utilities'''

from os.path import isfile, exists
from io import BytesIO
from pathlib import Path
import PIL.Image
import base64

# casatools on first use, not at import (cubevis.utils._casa).
from cubevis.utils._casa import casatools_available, imagetool

def as_mime( path ):

    value = path
    if isinstance( value, str ) and isfile(value):
        value = Path(value)

    if isinstance( value, Path ):
        value = PIL.Image.open(value)

    if isinstance( value, PIL.Image.Image ):
        out = BytesIO()
        fmt = value.format or "PNG"
        value.save(out, fmt)
        encoded = base64.b64encode( out.getvalue() ).decode( 'ascii' )
        return f"data:image/{fmt.lower()};base64,{encoded}"

    raise RuntimeError( f'''could not load {path}''' )

def _arcae_image( ):
    '''``cubevis.xarray.image`` when arcae is installed (CASA images without
    casatools), else None.'''
    try:
        import arcae  # noqa: F401                    # pylint: disable=import-outside-toplevel,unused-import
        from cubevis.xarray import image              # pylint: disable=import-outside-toplevel
        return image
    except Exception:
        return None

def new( pattern, path, overwrite=False ):
    '''Create a new image using ``pattern`` as a pattern. ``pattern`` supplies the
    shape for the new image as well as the coordinate system for the new image.
    The new image is filled with zeros.

    With arcae installed no casatools is needed (``cubevis.xarray.image``);
    without it, casatools makes the image as before.

    Parameters
    ----------
    path: str
        path to the image to be created

    pattern: str
        path to the image on which the new image should be based

    overwrite: bool
        overwrite any existing image, directory or file
    '''
    if exists(path) and not overwrite:
        raise RuntimeError( '''casaimage.new: image already exists (and 'overwrite=False')''' )
    if not exists(pattern):
        raise RuntimeError( 'casaimage.new: an original image is required' )
    image = _arcae_image( )
    if image is not None:
        import numpy as np                            # pylint: disable=import-outside-toplevel
        src = image.CasaImage( pattern )
        made = image.create_image( path, src.shape, dtype=np.float32, coords=src.coords_record( ), overwrite=overwrite )
        ### a plane at a time: a cube need not fit in memory
        with made.hold( readonly=False ):
            if made.ndim <= 2:
                made[...] = 0
            else:
                for index in np.ndindex( *made.shape[2:] ):
                    made[(slice(None), slice(None)) + index] = 0
        return made.path
    if not casatools_available():
        raise RuntimeError( 'casaimage.new: neither arcae nor casatools is available' )
    im = imagetool( )
    im.open(pattern)
    newim = im.newimagefromshape( path, shape=im.shape( ), csys=im.coordsys( ).torecord( ), overwrite=overwrite )
    im.close( )
    im.done( )
    result = newim.name( )
    newim.close( )
    newim.done( )
    return result

def shape( path ):
    '''Return the shape of an image.

    Parameters
    ----------
    path: str
        path to the image

    Returns
    -------
    numpy array of int
        shape of image
    '''
    if not exists(path):
        raise RuntimeError( '''casaimage.shape: image does not exist''' )
    image = _arcae_image( )
    if image is not None:
        import numpy as np                            # pylint: disable=import-outside-toplevel
        return np.array( image.CasaImage( path ).shape )
    im = imagetool( )
    im.open(path)
    result = im.shape( )
    im.close( )
    im.done( )
    return result
