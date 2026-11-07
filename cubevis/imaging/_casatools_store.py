########################################################################
#
# Copyright (C) 2021,2022,2023,2024,2026
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
'''``CasatoolsImageStore``: an ``ImageStore`` for CASA images through
``casatools.image``.  The casatools calls ``ImagePipe`` made until 2026-11,
moved here unchanged (getchunk/putchunk, the one-use image tool for
statistics -- CAS-13625 -- and for the coordinate system).

The image is opened on first use, not when the store is made: interactive
clean makes its cubes before gclean has written the residual.'''

import os
from typing import List, Optional


def _casa():
    '''casatools, imported when an image is first opened: importing
    ``cubevis.utils`` loads Bokeh, and this package imports neither Bokeh
    nor CASA when it is imported.'''
    from ..utils._casa import casatools_available, imagetool, regionmanager
    return casatools_available, imagetool, regionmanager


class CasatoolsImageStore:
    '''Residual (or any CASA image) and an optional mask image.'''

    def __init__(self, image_path: str, mask_path: Optional[str] = None):
        self.image_path = image_path
        self.mask_path = mask_path
        self._img = None
        self._msk = None
        self._rgn = None
        self._chan_shape = None
        self._mask_opened = False

    # ---- opening ------------------------------------------------------

    def _image(self):
        if self._img is None:
            casatools_available, imagetool, regionmanager = _casa()
            if not casatools_available():
                raise RuntimeError('cannot open an image because casatools is not available')
            img = imagetool()
            try:
                img.open(self.image_path)
            except Exception as ex:
                raise RuntimeError(f'could not open image: {self.image_path}') from ex
            self._img = img
            self._rgn = regionmanager()
            imshape = img.shape()
            if self._chan_shape is None:
                self._chan_shape = list(imshape[0:2])
            if not self._mask_opened:
                self._mask_opened = True
                self.open_mask(self.mask_path)
        return self._img

    def open_mask(self, path: Optional[str]) -> None:
        if path is None:
            self.mask_path = None
            return
        if self._msk is not None:
            self._msk.close()
        _, imagetool, _ = _casa()
        self._msk = imagetool()
        try:
            self._msk.open(path)
            self.mask_path = path
        except Exception as ex:
            self._msk = None
            self.mask_path = None
            raise RuntimeError(f'could not open mask: {path}') from ex
        mskshape = self._msk.shape()
        if self._img is not None and all(self._img.shape() != mskshape):
            raise RuntimeError(f'mismatch between image shape ({self._img.shape()}) and mask shape ({mskshape})')
        if self._chan_shape is None:
            self._chan_shape = list(mskshape[0:2])

    def close_mask(self) -> None:
        if self._msk is not None:
            self._msk.close()
            self._msk = None

    def _mask(self):
        self._image()
        return self._msk

    # ---- reading and writing -----------------------------------------

    def shape(self) -> List[int]:
        return list(self._image().shape())

    def modified(self) -> float:
        '''Newest change time of the files making up the image (tclean
        rewrites the residual in place).'''
        paths = [os.path.join(self.image_path, f) for f in os.listdir(self.image_path)]
        return max(map(os.path.getctime, paths))

    def read_plane(self, index: List[int]):
        return self._image().getchunk(blc=[0, 0] + index, trc=self._chan_shape + index)

    def read_spectrum(self, index: List[int]):
        nchan = self.shape()[-1]
        return self._image().getchunk(blc=index + [0], trc=index + [nchan])

    def has_mask(self) -> bool:
        return self._mask() is not None

    def read_mask_plane(self, index: List[int]):
        return self._mask().getchunk(blc=[0, 0] + index, trc=self._chan_shape + index)

    def write_mask_plane(self, index: List[int], pixels) -> None:
        self._mask().putchunk(blc=[0, 0] + index, pixels=pixels)

    def read_mask_chans(self, stokes: int, start: int, count: int):
        '''Mask channels ``start .. start+count-1`` of ``stokes``, ``[x, y, 1, count]``.'''
        nx, ny = self._chan_shape
        return self._mask().getchunk(blc=[0, 0, stokes, start], trc=[nx - 1, ny - 1, stokes, start + count - 1])

    def write_mask_chans(self, stokes: int, start: int, pixels) -> None:
        '''Write ``[x, y, count]`` (or ``[x, y, 1, count]``) from channel ``start``.'''
        if pixels.ndim == 3:
            pixels = pixels[:, :, None, :]
        self._mask().putchunk(blc=[0, 0, stokes, start], pixels=pixels)

    def read_mask_spectrum(self, index: List[int]):
        nchan = self.shape()[-1]
        return self._mask().getchunk(blc=index + [0], trc=index + [nchan])

    def mask_pixel(self, chan: List[int], index: List[int]) -> int:
        pv = self._mask().pixelvalue(index + chan)
        return int(pv['value']['value'])

    def has_default_mask(self) -> bool:
        return 'mask0' in self._image().maskhandler('get')

    def read_default_mask(self):
        return self._image().getregion(getmask=True)

    def statistics(self, index: List[int], mask_path: Optional[str] = None) -> dict:
        self._image()
        reg = self._rgn.box([0, 0] + index, self._chan_shape + index)
        ###
        ### This seems like it should work:
        ###
        #      rawstats = self._img.statistics( region=reg )
        ###
        ### but it does not so we have to create a one-use image tool (see CAS-13625)
        ###
        _, imagetool, _ = _casa()
        ia = imagetool()
        ia.open(self.image_path)
        try:
            if mask_path:
                ### mask is an LEL expression and quotes prevents a name containing
                ### numbers from being interpreted as an expression
                return ia.statistics(region=reg, mask=f'''"{mask_path}"''')
            return ia.statistics(region=reg)
        finally:
            ia.close()

    def stokes_labels(self) -> List[str]:
        return self._image().coordsys().stokes()

    def fits_header(self):
        img = self._image()
        return (img.fitsheader(exclude="HISTORY"),
                img.fitsheader(retstr=True, exclude="HISTORY"))

    def coordsys(self):
        '''The casatools ``coordsys`` (a one-use image tool, as before).'''
        _, imagetool, _ = _casa()
        ia = imagetool()
        ia.open(self.image_path)
        try:
            return ia.coordsys()
        finally:
            ia.close()

    def coordsys_record(self) -> dict:
        return self.coordsys().torecord()

    def coordinates(self) -> dict:
        '''The coordinate description (``cubevis.imaging._coordinates``):
        from the coordinate system record, with the spectral values of
        every channel from the coordinate system itself (exact for tabular
        and non-linear axes), the restoring beam and the brightness unit.'''
        from ._coordinates import from_casa_coordsys
        img = self._image()
        shape = list(img.shape())
        csys = img.coordsys()
        try:
            record = csys.torecord()
            spectral_values = None
            found = csys.findcoordinate('spectral')
            if found.get('return') and len(found['pixel']) == 1 and len(found['world']) == 1:
                pix_axis, world_axis = int(found['pixel'][0]), int(found['world'][0])
                nchan = shape[pix_axis]
                refpix = list(csys.referencepixel()['numeric'])
                import numpy as np                  # pylint: disable=import-outside-toplevel
                pixels = np.array([refpix] * nchan, dtype=float).transpose()
                pixels[pix_axis, :] = np.arange(nchan, dtype=float)
                world = csys.toworldmany(pixels)['numeric']
                spectral_values = [float(v) for v in world[world_axis]]
            return from_casa_coordsys(record, shape, spectral_values=spectral_values,
                                      beam=img.restoringbeam(), brightness_unit=img.brightnessunit())
        finally:
            csys.done()

    def close(self) -> None:
        if self._rgn is not None:
            self._rgn.done()
            self._rgn = None
        self.close_mask()
        if self._img is not None:
            self._img.close()
            self._img.done()
            self._img = None
        # reopened on next use, mask included (mask_path is kept)
        self._mask_opened = False
