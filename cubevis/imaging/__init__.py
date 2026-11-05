########################################################################
#
# Copyright (C) 2026
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
'''The imaging layer: what interactive clean's GUI uses to run the imager
and to read and edit its images (``devel/notes/drs/iclean/
iclean_imaging_layer_design.md``).

* ``ImagingSession`` (protocol) -- the imager and the images of each field;
  ``LocalImagingSession`` runs them in this process.
* ``ImageCube`` -- one field's residual, mask and mask0 for display and
  mask editing, in numpy, over an ``ImageStore``.
* ``ImageStore`` (protocol) -- primitive image access for one format;
  ``CasatoolsImageStore`` for CASA images.
* ``Deconvolver`` (protocol) -- the gclean surface.

Importing this package imports no CASA module; casatools is imported when
a ``CasatoolsImageStore`` first opens an image, casatasks when a
``LocalImagingSession`` is made from iclean's arguments.
'''

from ._protocols import CycleResult, Deconvolver, ImageStore, ImagingSession
from ._protocols import ImageCube as ImageCubeProtocol
from ._image_cube import ImageCube, quantize
from ._casatools_store import CasatoolsImageStore
from ._session import LocalImagingSession, resolve_deconvolver, as_imaging_session, open_image_cube

__all__ = ['CycleResult', 'Deconvolver', 'ImageStore', 'ImagingSession', 'ImageCubeProtocol',
           'ImageCube', 'quantize', 'CasatoolsImageStore', 'LocalImagingSession',
           'resolve_deconvolver', 'as_imaging_session', 'open_image_cube']
