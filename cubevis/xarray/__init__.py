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
"""``cubevis.xarray`` -- xarray backends for CASA tables, read with arcae.

Nothing here imports Bokeh or CASA (the ``cubevis.imaging`` rule), so a
remote worker, a test or another package can use it without the GUI.
Each backend is a package of its own that imports nothing from the others,
so that any of them can leave cubevis and be installed by itself.

* ``image`` -- engine ``"casa-image"``: a CASA image as an
  ``xarray.Dataset`` read lazily, in xradio's image layout; ``CasaImage``
  for reading and writing pixels and masks directly; ``open_image`` and
  ``describe_dataset`` for CASA and xradio (Zarr) images alike::

      import xarray as xr
      ds = xr.open_dataset("ngc5921.image", engine="casa-image")

Record: ``devel/notes/drs/xarray-image/xarray_image_notes.md``.
"""
