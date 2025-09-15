########################################################################
#
# Copyright (C) 2023,2025
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
'''This contains functions which return the URLs to the ``cubevis``
JavaScript libraries. The ``casalib`` library has Bokeh independent
functions while the `cubevisjs` library has the Bokeh extensions'''
from os import path

_library_path = None

def casalib_path( ):
    global _library_path
    if _library_path is None:
        _library_path = path.join( path.dirname(path.dirname(path.dirname(__file__))), '__js__' )
    casalib_path = path.join( _library_path, 'casalib.min.js' )
    if not path.isfile( casalib_path ):
        raise RuntimeError( f''''casalib' JavaScript library not found at '{casalib_path}\'''' )
    return casalib_path

def casalib_url( ):
    return f'''file://{casalib_path( )}'''

def cubevisjs_path( ):
    global _library_path
    if _library_path is None:
        _library_path = path.join( path.dirname(path.dirname(path.dirname(__file__))), '__js__' )
    cubevisjs_path = path.join( _library_path, 'cubevisjs.min.js' )
    if not path.isfile(cubevisjs_path):
        raise RuntimeError( f''''cubevisjs' JavaScript library not found at '{cubevisjs_path}\'''' )
    return cubevisjs_path

def cubevisjs_url( ):
    return f'''file://{cubevisjs_path( )}'''
