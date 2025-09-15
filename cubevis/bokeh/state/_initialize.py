########################################################################
#
# Copyright (C) 2021,2022,2023,2025
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
'''This contains functions to inject the ``cubevisjs`` library into the
generated HTML that is used to display the Bokeh plots that ``casagui``'s
applications produce'''
import os
import re
import logging
from os import path
from os.path import dirname, join, basename, abspath
from urllib.parse import urlparse
from bokeh import resources
from IPython.display import Javascript, display
from ...utils import path_to_url, static_vars, have_network

logger = logging.getLogger(__name__)

# Package-level registry
_JUPYTER_STATE = {
    'casalib_loaded': False,
    'cubevisjs_loaded': False,
    'models_registered': set()
}

def get_jupyter_state( ):
    """Get the package-level Jupyter state"""
    return _JUPYTER_STATE

_CUBEVIS_LIBS = { }

def get_cubevis_libs( ):
    """Get the package-level default cubevis paths"""
    return _CUBEVIS_LIBS

def set_cubevis_lib( **kw ):
    libs = get_cubevis_libs( )
    for lib in [ 'bokeh', 'bokeh_widgets', 'bokeh_tables', 'casalib', 'cubevisjs' ]:
        if lib in kw:
            libs[lib] = kw[lib]
    return libs

def is_jupyter():
    """Check if running in Jupyter"""
    try:
        from IPython import get_ipython
        return get_ipython() is not None
    except ImportError:
        return False

@static_vars( initialized=False,
              do_local_subst=not have_network( ) )
def order_bokeh_js( ):
    """Initialize `bokeh` for use with the ``cubevisjs`` extensions.

    The ``cubevisjs`` extensions for Bokeh are built into a stand-alone
    external JavaScript file constructed for the specific version of
    Bokeh that is being used. Previously the CASA minified JavaScript
    libraries were loaded from the network. Now these libraries are
    included in a ``__js__`` directory within the cubevis Python package.
    If users need to debug JavaScript problems, non-minified versions
    can be copied into the ``__js__`` directory to replace the existing
    libraries. To debug within the bokehjs library, a URL or a path
    can be supplied by the ``bokehjs_subst`` parameter and it will be
    substituted for the standard Bokeh library.

    Setting defaults
    ----------------
    The default paths for the cubevis libs can be controlled by fetching
    the default JavaScript libraries with get_cubevis_libs( ) and setting
    the path to the desired libraries.

    Bokeh dependent javascript library which is loaded after all
    other Bokeh libraries have been loaded. The dictionary keys
    which are checked are:

         *  'bokeh'
         *  'bokeh_widgets'
         *  'bokeh_tables'
         *  'casalib'
         *  'cubevisjs'

    The value for each of these keys should be the path to the
    corresponding Bokeh library that should be used. This path
    could be a local path, a URL or None. None is the default
    in which case it loads the published library for the current
    version of ``cubevisjs`` and ``bokeh``

    If paths provided as the dictionary values contain
    "{JSLIB}" at the start of the string, then it will be
    replaced with the path to the "cubevis/__js__" directory.
    """

    js_libraries=get_cubevis_libs( )

    def include_all_bokehjs( urls ):
        result = urls.copy( )
        bokeh_path=None
        has_bokeh_widgets=False
        has_bokeh_tables=False
        for url in urls:
            if re.match( r'.*/bokeh-\d+\.\d+\.\d+(?:\.min)?\.js$', url ):
                bokeh_path = url
            if re.match( r'.*/bokeh-widgets-\d+\.\d+\.\d+(?:\.min)?\.js$', url ):
                has_bokeh_widgets = True
            if re.match( r'.*/bokeh-tables-\d+\.\d+\.\d+(?:\.min)?\.js$', url ):
                has_bokeh_tables = True

        if bokeh_path:
            if not has_bokeh_widgets:
                result.append( bokeh_path.replace('/bokeh-','/bokeh-widgets-') )
            if not has_bokeh_tables:
                result.append( bokeh_path.replace('/bokeh-','/bokeh-tables-') )

        return result

    def retrieve_local_cache( url ):
        pieces = urlparse(url)
        if pieces.scheme != 'file':
            lib = basename(pieces.path)
            url = path_to_url(lib)
            if lib != url:
                return url
            elif not have_network( ):
                raise RuntimeError( '''Cannot confirm internet access and could not find local cached versions of Bokeh's libraries''' )
        return url

    def fill_default_urls( jspaths ):
        sys_paths = include_all_bokehjs( jspaths )
        result = js_libraries.copy( )
        for url in sys_paths:
            if re.match( r'.*/bokeh-\d+\.\d+\.\d+(?:\.min)?\.js$', url ):
                if 'bokeh' not in result:
                    result['bokeh'] = retrieve_local_cache(url) if order_bokeh_js.do_local_subst else url
            if re.match( r'.*/bokeh-widgets-\d+\.\d+\.\d+(?:\.min)?\.js$', url ):
                if 'bokeh_widgets' not in result:
                    result['bokeh_widgets'] = retrieve_local_cache(url) if order_bokeh_js.do_local_subst else url
            if re.match( r'.*/bokeh-tables-\d+\.\d+\.\d+(?:\.min)?\.js$', url ):
                if 'bokeh_tables' not in result:
                    result['bokeh_tables'] = retrieve_local_cache(url) if order_bokeh_js.do_local_subst else url
            if re.match( r'.*/cubevisjs.*(?:\.min)?\.js$', url ):
                if 'cubevisjs' not in result:
                    result['cubevisjs'] =  retrieve_local_cache(url) if order_bokeh_js.do_local_subst else url
            if re.match( r'.*/casalib.*(?:\.min)?\.js$', url ):
                if 'casalib' not in result:
                    result['casalib'] =  retrieve_local_cache(url) if order_bokeh_js.do_local_subst else url
        return result

    if order_bokeh_js.initialized:
        ### only initialize once...
        return

    ###
    ### Substitute "{JSLIB}/..." {CACHEDIR}
    ###
    for key, value in js_libraries.items():
        if value.startswith("{JSLIB}/"):
            js_libraries[key] = path_to_url(value[8:])

    ###
    ### NOTE that for this log message to be printed the user MUST use the
    ### environment variable approach to setting logging level to DEBUG:
    ###
    ###    CUBEVIS_DEBUG=1
    ###
    state = get_jupyter_state()
    logger.debug(f"\torder_bokeh_js( ) w/ {len(state['models_registered'])} model types and {js_libraries} defaults")
    order_bokeh_js.initialized = True
    resources.Resources._old_js_files = resources.Resources.js_files

    def js_files( self ):
        ########################################################################
        ### Function that will replace the member function within Bokeh that ###
        ### returns the URLs for Bokeh initialization...                     ###
        ########################################################################
        def expand_paths( replacement ):
            ### turn all paths into URLs
            ### turn a single string into a list with one string
            if replacement is None:
                return [ ]
            if type(replacement) == str:
                if path.isfile(replacement):
                    return [ f'''file://{abspath(replacement)}''' ]
                elif replacement.startswith('http'):
                    return [ replacement ]
                else:
                    raise RuntimeError( f'''debugging bokehjs substitution ('{replacement}') does not exist''' )
            if type(replacement) == list:
                result = [ ]
                for u in replacement:
                    if path.isfile(u):
                        result.append( f'''file://{abspath(u)}''' )
                    elif u.startswith('http'):
                        result.append( u )
                    else:
                        raise RuntimeError( f'''debugging bokehjs substitution ('{u}') does not exist''' )
                return result
            return [ ]

        user_bokehjs_replacement = expand_paths(js_libraries)
        sys_urls = fill_default_urls( resources.Resources._old_js_files.fget(self) )

        return [ sys_urls['casalib'], sys_urls['bokeh'], sys_urls['bokeh_widgets'], sys_urls['bokeh_tables'], sys_urls['cubevisjs'] ]

    resources.Resources.js_files = property(js_files)
    return

def register_model(model_class):
    """Register a model class that needs dependencies"""
    _JUPYTER_STATE['models_registered'].add(model_class.__name__)


def ensure_jupyter_dependencies( ):
    """Ensure Jupyter dependencies using package state"""
    from . import casalib_path as get_casalib_path
    from . import cubevisjs_path as get_cubevisjs_path

    order_bokeh_js( )

    state = get_jupyter_state()

    if not is_jupyter():
        return

    if state['casalib_loaded'] and state['cubevisjs_loaded']:
        return

    js_libraries = get_cubevis_libs( )
    js_defaults = { 'casalib': get_casalib_path( ),
                    'cubevisjs': get_cubevisjs_path( ) }

    logger.debug( f"\tensure_jupyter_dependencies( ) w/ {len(state['models_registered'])} model types and {js_libraries} paths")

    def load_javascript( name ):
        nonlocal js_libraries
        nonlocal js_defaults

        try:
            lib_path = js_libraries[name] if name in js_libraries else None
            if lib_path is None:
                lib_path = js_defaults[name]
            else:
                if lib_path.startswith( "file://" ):
                    lib_path = casalib_path[7:]

            # Load casalib
            with open(lib_path, 'r') as f:
                display(Javascript(f"console.log('Loading {name}: {lib_path}'); {f.read()}"))

            logger.debug( f"\tensure_jupyter_dependencies( ) loaded {lib_path}" )
            return True

        except Exception as e:
            logger.warning( f"\tensure_jupyter_dependencies( ) FAILED load of {lib_path}" )
            return False

    if state['casalib_loaded'] == False and load_javascript( 'casalib' ):
        state['casalib_loaded'] = True

    if state['cubevisjs_loaded'] == False and load_javascript( 'cubevisjs' ):
        state['cubevisjs_loaded'] = True
