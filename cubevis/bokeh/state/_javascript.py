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
from os import path, environ
from pathlib import Path
from packaging import version
from bokeh import __version__ as bokeh_version
from . import get_js_loading_selection
from ...utils import max_git_version as _max_git_version

_local_library_path = None
_bokeh_major_minor = None

_CUBEVIS_RELEASE_VERSION = None
_CUBEVIS_GITHUB_VERSION = None
_BOKEH_MAJOR_MINOR = None
_CUBEVIS_JS_TAG = None

def bokeh_major_minor( ):
    global _BOKEH_MAJOR_MINOR
    if _BOKEH_MAJOR_MINOR is None:
        from bokeh import __version__ as bokeh_version
        v = version.parse(bokeh_version)
        _BOKEH_MAJOR_MINOR = f"{v.major}.{v.minor}"
    return _BOKEH_MAJOR_MINOR

def github_js_tag( ):
    global _CUBEVIS_JS_TAG
    if _CUBEVIS_JS_TAG is None:
        release = cubevis_release_version( )
        if release is not None:
            _CUBEVIS_JS_TAG = f"v{release}"
        else:
            if 'CUBEVIS_JS_TAG' in environ:
                _CUBEVIS_JS_TAG = environ['CUBEVIS_JS_TAG']
            else:
                _CUBEVIS_JS_TAG = f"v{cubevis_github_version( )}"
    return _CUBEVIS_JS_TAG

def cubevis_github_version( ):
    global _CUBEVIS_GITHUB_VERSION
    if _CUBEVIS_GITHUB_VERSION is None:
        _CUBEVIS_GITHUB_VERSION = _max_git_version( )
    return _CUBEVIS_GITHUB_VERSION

def cubevis_release_version( ):
    global _CUBEVIS_RELEASE_VERSION
    if _CUBEVIS_RELEASE_VERSION is None:
        try:
            ###
            ### __version__.py is generated as part of the build, but if the source tree
            ### for cubevis is used directly for development, no __version__.py will be
            ### available...
            ###
            from ...__version__ import __version__ as package_version
            _CUBEVIS_RELEASE_VERSION = package_version
        except ModuleNotFoundError: pass
    return _CUBEVIS_RELEASE_VERSION

def casalib_path( ):
    global _local_library_path
    if _local_library_path is None:
        _local_library_path = path.join( path.dirname(path.dirname(path.dirname(__file__))), '__js__' )
    casalib_path = path.join( _local_library_path, 'casalib.min.js' )
    if not path.isfile( casalib_path ):
        raise RuntimeError( f''''casalib' JavaScript library not found at '{casalib_path}\'''' )
    return casalib_path

def cubevisjs_path( ):
    global _local_library_path
    global _bokeh_major_minor
    if _local_library_path is None:
        _local_library_path = path.join( path.dirname(path.dirname(path.dirname(__file__))), '__js__' )
    if _bokeh_major_minor is None:
        version = bokeh_version.split('.')
        _bokeh_major_minor = f"{version[0]}.{version[1]}"
    cubevisjs_path = path.join( _local_library_path, f"bokeh-{_bokeh_major_minor}", 'cubevisjs.min.js' )
    if not path.isfile(cubevisjs_path):
        raise RuntimeError( f''''cubevisjs' JavaScript library not found at '{cubevisjs_path}\'''' )
    return cubevisjs_path

### These functions MUST also select remote/local based on Jupyter or
### no network because these URLs also are included in the
### __javascript__ variables for Bokeh model derived member variables.
### These paths are then included in the Bokeh list of JavaScript
### libraries loaded when the GUI is loaded. However, the ordering is
### not correct because cubevisjs.min.js must appear AFTER all of the
### Bokeh files.
def casalib_url( ):
    prefer_local, prefer_network = get_js_loading_selection( )
    if prefer_network:
        return f"https://cdn.jsdelivr.net/gh/casangi/cubevis@{github_js_tag( )}/cubevis/__js__/casalib.min.js"
    else:
        return f"file://{casalib_path( )}"
def cubevisjs_url( ):
    prefer_local, prefer_network = get_js_loading_selection( )
    if prefer_network:
        return f"https://cdn.jsdelivr.net/gh/casangi/cubevis@{github_js_tag( )}/cubevis/__js__/bokeh-{bokeh_major_minor( )}/cubevisjs.min.js"
    else:
        return f"file://{cubevisjs_path( )}"
