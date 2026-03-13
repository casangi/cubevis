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
from enum import Enum
from os.path import dirname, join, basename, abspath
from urllib.parse import urlparse
from bokeh import resources
from bokeh.settings import settings
from ...utils import path_to_url, static_vars, have_network

logger = logging.getLogger(__name__)

_CUBEVIS_LIBS = {}
# Selector for which source to prefer for JavaScript files. Bokeh's Python
# package comes with static versions of the Bokeh JavaScript libraries.
# cubevis libraries are available both as static versions contained within
# the Python package and from an NRAO website.
#
# None = auto-detect, 'local' = prefer local, 'network' = prefer network
class JsLoading(Enum):
    AUTO = 'auto'
    LOCAL = 'local'
    NETWORK = 'network'

_JS_STRATEGY = JsLoading.AUTO

# Package-level registry
_JUPYTER_STATE = {
    'models_registered': set()
}

def get_cubevis_libs( ):
    """Get the package-level default cubevis paths"""
    return _CUBEVIS_LIBS

def set_cubevis_lib(**kw):
    """Set custom paths for cubevis JavaScript libraries"""
    libs = get_cubevis_libs()
    for lib in ['bokeh', 'bokeh_widgets', 'bokeh_tables', 'casalib', 'cubevisjs']:
        if lib in kw:
            libs[lib] = kw[lib]
    return libs

def get_js_loading( ):
    """Get JavaScript loading strategy.
    """
    return _JS_STRATEGY

def set_js_loading(strategy):
    """Set JavaScript loading strategy.

    Args:
        strategy (JsLoading or str):
            - 'auto': Smart selection based on environment
            - 'local': Always prefer local static files when available
            - 'network': Always prefer network CDN when available
    """
    global _JS_STRATEGY
    if isinstance(strategy, str):
        _JS_STRATEGY = JsLoading(strategy)
    elif isinstance(strategy, JsLoading):
        _JS_STRATEGY = strategy
    else:
        raise TypeError("'strategy' must be a valid JsLoading string or enum member")

def get_js_loading_selection( ):
    # Check for user override
    global _JS_STRATEGY

    prefer_network = False
    prefer_local = False

    in_jupyter = is_jupyter()
    use_network = have_network()

    if _JS_STRATEGY is JsLoading.LOCAL:
        prefer_network = False
        prefer_local = True
        logger.debug("Strategy: User override -> prefer local")
    elif _JS_STRATEGY is JsLoading.NETWORK:
        prefer_network = True
        prefer_local = False
        logger.debug("Strategy: User override -> prefer network")
    else:
        # Auto-detect optimal strategy
        if in_jupyter and use_network:
            # Jupyter notebooks work best with CDN versions
            prefer_network = True
            prefer_local = False
            logger.debug("Strategy: Jupyter + network -> prefer CDN")
        elif not use_network:
            # No choice - must use local
            prefer_network = False
            prefer_local = True
            logger.debug("Strategy: No network -> local only")
        else:
            # Standalone application with network -> prefer local for speed/reliability
            prefer_network = False
            prefer_local = True
            logger.debug("Strategy: Standalone + network -> prefer local static")

    return prefer_local, prefer_network

def is_jupyter():
    """Check if running in Jupyter environment"""
    try:
        from IPython import get_ipython
        return get_ipython() is not None
    except ImportError:
        return False

def resolve_js_library_paths():
    """Resolve paths to all required JavaScript libraries based on environment and availability.

    Strategy:
    - Jupyter notebooks: Prefer network CDN (required for proper functionality)
    - Standalone apps with network: Prefer local static (faster, more reliable)
    - No network: Use local static (only option)
    - Corporate/restricted networks: Prefer local static (firewall bypass)

    Returns:
        dict: Dictionary with resolved paths for 'casalib', 'bokeh', 'bokeh_widgets',
              'bokeh_tables', and 'cubevisjs'
    """
    ### These functions also select remote/local based on Jupyter or
    ### no network because these URLs also are included in the
    ### __javascript__ variables for Bokeh model derived member variables.
    from . import casalib_url as get_casalib_url
    from . import cubevisjs_url as get_cubevisjs_url

    # Start with user-defined overrides
    result = get_cubevis_libs().copy()

    # Handle {JSLIB}/ substitution for user paths
    for key, value in result.items():
        if value and value.startswith("{JSLIB}/"):
            result[key] = path_to_url(value[8:])

    prefer_local, prefer_network = get_js_loading_selection( )

    # Get available sources
    bokeh_static_js = {}
    bokeh_static_available = False

    try:
        bokeh_static_path = settings.bokehjs_path()
        js_dir = join(bokeh_static_path, 'js')
        logger.debug( f"Bokeh static pat: {js_dir}" )

        if path.exists(js_dir):
            for filename in os.listdir(js_dir):
                if filename.endswith('.js'):
                    file_path = f"file://{abspath(join(js_dir, filename))}"

                    ## js_dir seems to contain both minified and non minified versions of
                    ## the bokeh librarys. If we ever want to allow for non-minified
                    ## versions, e.g. for debugging, we'll need to look here
                    if re.match(r'.*bokeh(?:-\d+\.\d+\.\d+)?\.min\.js$', filename):
                        bokeh_static_js['bokeh'] = file_path
                    elif re.match(r'.*bokeh-widgets(?:-\d+\.\d+\.\d+)?\.min\.js$', filename):
                        bokeh_static_js['bokeh_widgets'] = file_path
                    elif re.match(r'.*bokeh-tables(?:-\d+\.\d+\.\d+)?\.min\.js$', filename):
                        bokeh_static_js['bokeh_tables'] = file_path

            bokeh_static_available = len(bokeh_static_js) > 0

    except Exception as e:
        logger.warning(f"Could not access Bokeh static files: {e}")

    # Network connection available
    use_network = have_network()

    # Fill in missing Bokeh libraries using the selected strategy
    for lib in ['bokeh', 'bokeh_widgets', 'bokeh_tables']:
        if lib not in result or result[lib] is None:
            if prefer_local and bokeh_static_available and lib in bokeh_static_js:
                result[lib] = bokeh_static_js[lib]
                logger.debug(f"Using local static for {lib}")
            elif prefer_network and use_network:
                # Let Bokeh handle CDN URLs - return None to use defaults
                result[lib] = None
                logger.debug(f"Using CDN for {lib}")
            elif bokeh_static_available and lib in bokeh_static_js:
                # Fallback to local if network preference fails
                result[lib] = bokeh_static_js[lib]
                logger.debug(f"Fallback to local static for {lib}")
            else:
                # Last resort - try local cache
                try:
                    cached_path = path_to_url(f"{lib}.min.js")
                    if cached_path != f"{lib}.min.js":  # Found in cache
                        result[lib] = cached_path
                        logger.debug(f"Using local cache for {lib}")
                    else:
                        result[lib] = None
                        logger.debug(f"No source found for {lib}")
                except:
                    result[lib] = None

    # Fill in cubevis-specific libraries
    if 'casalib' not in result or result['casalib'] is None:
        result['casalib'] = get_casalib_url( )

    if 'cubevisjs' not in result or result['cubevisjs'] is None:
        result['cubevisjs'] = get_cubevisjs_url( )

    logger.debug(f"Resolved JS library paths: {result}")
    logger.debug(f"Environment: jupyter={is_jupyter( )}, network={use_network}, strategy={'network' if prefer_network else 'local'}")

    return result

@static_vars(initialized=False)
def order_bokeh_js():
    """Initialize `bokeh` for use with the ``cubevisjs`` extensions.

    This function injects the cubevis JavaScript libraries into Bokeh's resource
    loading system in the proper order: casalib → bokeh core → bokeh widgets →
    bokeh tables → cubevisjs.

    The function automatically determines the best source for JavaScript libraries
    based on the current environment (Jupyter vs standalone, network availability, etc.).
    """

    if order_bokeh_js.initialized:
        return

    # Get all resolved library paths
    js_paths = resolve_js_library_paths()

    logger.debug(f"order_bokeh_js() initializing with paths: {js_paths}")
    order_bokeh_js.initialized = True

    # Store original Bokeh js_files function
    resources.Resources._old_js_files = resources.Resources.js_files

    def js_files(self):
        """Replacement function that returns JavaScript files in the correct order"""

        # Get original Bokeh URLs as fallback
        original_urls = resources.Resources._old_js_files.fget(self)
        logger.debug( f"Original Bokeh JavaScript: {original_urls}" )

        def get_url_or_fallback(lib_key, fallback_pattern=None):
            """Get resolved URL or find fallback from original Bokeh URLs"""
            resolved_path = js_paths.get(lib_key)

            if resolved_path:
                return resolved_path

            # Look for fallback in original URLs
            if fallback_pattern:
                for url in original_urls:
                    if re.match(fallback_pattern, url):
                        return url

            return None

        # Build the ordered list
        ordered_js = []

        # 1. casalib (always first)
        casalib_url = get_url_or_fallback('casalib')
        if casalib_url:
            ordered_js.append(casalib_url)

        # 2. bokeh core
        bokeh_url = get_url_or_fallback('bokeh', r'.*/bokeh-\d+\.\d+\.\d+(?:\.min)?\.js$')
        if bokeh_url:
            ordered_js.append(bokeh_url)

        # 3. bokeh widgets
        widgets_url = get_url_or_fallback('bokeh_widgets', r'.*/bokeh-widgets-\d+\.\d+\.\d+(?:\.min)?\.js$')
        if widgets_url:
            ordered_js.append(widgets_url)

        # 4. bokeh tables
        tables_url = get_url_or_fallback('bokeh_tables', r'.*/bokeh-tables-\d+\.\d+\.\d+(?:\.min)?\.js$')
        if tables_url:
            ordered_js.append(tables_url)

        # 5. cubevisjs (always last)
        cubevisjs_url = get_url_or_fallback('cubevisjs')
        if cubevisjs_url:
            ordered_js.append(cubevisjs_url)

        logger.debug(f"Final JS load order: {ordered_js}")
        return ordered_js

    # Replace Bokeh's js_files property
    resources.Resources.js_files = property(js_files)
    return

#def get_bokeh_js_paths( ):
#    modes = ['cdn','inline','server','server-dev','relative','relative-dev','absolute','absolute-dev']
#    return { 'new': { mode: resources.Resources(mode=mode).js_files for mode in modes },
#             'old': { mode: resources.Resources(mode=mode)._old_js_files for mode in modes } }
def get_bokeh_js_paths( ):
    return resources.Resources(mode='cdn').js_files

def get_jupyter_state( ):
    """Get the package-level Jupyter state"""
    return _JUPYTER_STATE

def register_model(model_class):
    """Register a model class that needs dependencies"""
    _JUPYTER_STATE['models_registered'].add(model_class.__name__)
