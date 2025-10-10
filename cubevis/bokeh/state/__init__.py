########################################################################
#
# Copyright (C) 2022,2025
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
'''Bokeh state management functions (both within the Bokeh distribution
and with the Bokeh extensions found in ``cubevis.bokeh``.'''

from ._initialize import order_bokeh_js, register_model, set_cubevis_lib, get_js_loading, set_js_loading, get_js_loading_selection, JsLoading
from ._session import setup_session as initialize_session
from ._palette import available_palettes, find_palette, default_palette
from ._javascript import casalib_path, casalib_url, cubevisjs_path, cubevisjs_url
from ._current import CurrentBokehState as current_bokeh_state
