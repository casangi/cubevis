########################################################################
#
# Copyright (C) 2021,2022
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


def set_log_level(level):
    """Set the logging level for cubevis.

    Args:
        level: Logging level (e.g., logging.DEBUG, logging.INFO, 'DEBUG', 'INFO')
    """
    if isinstance(level, str):
        level = getattr(_logging, level.upper())
    logger.setLevel(level)

try:
    from .__version__ import __version__
except ModuleNotFoundError:
    ###
    ### __version__.py is generated as part of the build, but if the source tree
    ### for cubevis is used directly for development, no __version__.py will be
    ### available so set it to a default value...
    ###
    __version__ = {}


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
