########################################################################
#
# Copyright (C) 2023
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
from ...utils import static_vars
from ._initialize import initialize_bokeh
from os import path

@static_vars( library_path=None )
def casalib_url( ):
    if casalib_url.library_path is None:
        casalib_url.library_path = path.join(path.dirname(path.dirname(path.dirname(__file__))), '__js__', 'casalib.min.js')
    if not path.isfile(casalib_url.library_path):
        raise RuntimeError( f''''casalib' JavaScript library not found at '{casalib_url.library_path}\'''' )
    if not initialize_bokeh.initialized:
        initialize_bokeh( )
    return f'''file://{casalib_url.library_path}'''

@static_vars( library_path=None )
def cubevisjs_url( ):
    if cubevisjs_url.library_path is None:
        cubevisjs_url.library_path = path.join(path.dirname(path.dirname(path.dirname(__file__))), '__js__', 'cubevisjs.min.js')
    if not path.isfile(cubevisjs_url.library_path):
        raise RuntimeError( f''''cubevisjs' JavaScript library not found at '{cubevisjs_url.library_path}\'''' )
    if not initialize_bokeh.initialized:
        initialize_bokeh( )
    return f'''file://{cubevisjs_url.library_path}'''
