########################################################################
#
# Copyright (C) 2021, 2022
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
'''This module contains the extensions and additions to the functionality
provided by Bokeh'''

from .state import order_bokeh_js as _order_bokeh_js
from .state import register_model as _register_model
from .state._initialize import get_bokeh_js_paths
from .state import set_cubevis_lib

class BokehInit:
    """Mixin for all cubevis models"""

    def __init_subclass__(cls, **kwargs):
        """Auto-register subclasses"""
        _order_bokeh_js()
        super().__init_subclass__(**kwargs)
        _register_model(cls)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
