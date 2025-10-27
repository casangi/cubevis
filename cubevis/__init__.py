########################################################################
#
# Copyright (C) 2021,2022,2025
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
'''cubevis provides a number of python command line tools which can be
used to build GUI applications for astronomy. It also contains some
applications turn-key applications'''

###
### Useful for debugging -- the default libraries can be set with
###     'set_cubevis_lib(...)' which accepts only keyword parameters.
###     The parameters represent the JavaScript libraries used by
###     cubevis. The parameters are 'bokeh', 'bokeh_widgets',
###     'bokeh_tables', 'casalib', 'cubevisjs'. "{JSLIB}" is substituted
###     with the actual path to "cubvis/__js__", so for example:
###
#from cubevis.bokeh import set_cubevis_lib
#set_cubevis_lib( bokeh_tables="{JSLIB}/bokeh-tables-3.6.1.js", cubevisjs=... )

import os as _os
import logging as _logging

logger = _logging.getLogger('cubevis')
_handler = _logging.StreamHandler()
_formatter = _logging.Formatter('[%(name)s] %(levelname)s: %(message)s')
_handler.setFormatter(_formatter)
logger.addHandler(_handler)

if _os.getenv('CUBEVIS_DEBUG', '').lower() in ('1', 'true', 'yes', 'on'):
    logger.setLevel(_logging.DEBUG)
else:
    logger.setLevel(_logging.INFO)

from .private.apps import iclean

def xml_interface_defs( ):
    '''This function may eventually return XML files for use in generating casashell bindings. An
       indentically named function provided by casatasks allows cubevis to generate an
       interactive clean task interface using the tclean XML file from casatasks.
    '''
    return { }

__mustache_interface_templates__ = { 'iclean': _os.path.join( _os.path.dirname(__file__), "private", "casashell", "iclean.mustache" ) }
def mustache_interface_templates( ):
    '''This provides a list of mustache files provided by cubevis. It may eventually allow
       casashell to generate all of its bindings at startup time. This would allow casashell
       to be consistent with any version of casatasks that is availale.
    '''
    return __mustache_interface_templates__
