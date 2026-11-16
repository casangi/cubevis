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
'''``XarrayImageStore``: the ``ImageStore`` interactive clean uses, over
``ImageReader`` -- CASA images through arcae, xradio images in Zarr, no
casatools.  The same surface and, for CASA images, the same results as
``CasatoolsImageStore`` (compared call by call in
``tests/manual/xarray-image/test_xarray_store.py``), so ``ImageCube`` and the
GUI above it do not know which one they have.

It only shapes what the reader gives into what ``ImageCube`` expects
(casatools' ``getchunk`` shapes, float64).  Differences that are meant:

* Nothing stays open: each call reads through a handle of its own.  The
  imager (tclean, through casatools) rewrites the residual and the mask in
  this same process between calls, and an arcae handle held across that keeps
  returning the old pixels (``cubevis/xarray/image/_image.py``).  It also
  means the store never holds a lock the imager waits for.
* ``statistics`` is numpy over the plane (``ImageReader.statistics``).
* ``coordsys()`` gives the coordinate system *record* of a CASA image (what
  casatools' ``coordsys().torecord()`` gives; ``convert_masks`` takes
  either), and the coordinate description for any other source.
* ``fits_header()`` is made from the coordinate description (astropy), for
  display only.

The image is opened on first use, not when the store is made: interactive
clean makes its cubes before the imager has written the residual.
'''

from typing import List, Optional

import numpy as np

from .reader import ImageReader


def missing_modules() -> List[str]:
    '''Which of arcae and xarray, which reading a CASA image this way
    needs, cannot be imported ([] when both can).'''
    import importlib                                 # pylint: disable=import-outside-toplevel
    missing = []
    for module in ('arcae', 'xarray'):
        try:
            importlib.import_module(module)
        except Exception:
            missing.append(module)
    return missing


def xarray_store_available() -> bool:
    return not missing_modules()


class XarrayImageStore:
    '''Residual (or any image an ``ImageReader`` opens) and an optional mask image.'''

    def __init__(self, image_path: str, mask_path: Optional[str] = None):
        self.image_path = image_path
        self.mask_path = mask_path
        self._img: Optional[ImageReader] = None
        self._msk: Optional[ImageReader] = None
        self._mask_opened = False

    # ---- opening ------------------------------------------------------

    def _image(self) -> ImageReader:
        if self._img is None:
            try:
                img = ImageReader.open(self.image_path)
            except ImportError as ex:
                raise RuntimeError(f'cannot open an image because {ex.name or "a module"} is not available') from ex
            except Exception as ex:
                raise RuntimeError(f'could not open image: {self.image_path}') from ex
            self._img = img
            if not self._mask_opened:
                self._mask_opened = True
                self.open_mask(self.mask_path)
        return self._img

    def open_mask(self, path: Optional[str]) -> None:
        if path is None:
            self.mask_path = None
            self._msk = None
            return
        try:
            msk = ImageReader.open(path)
        except Exception as ex:
            self._msk = None
            self.mask_path = None
            raise RuntimeError(f'could not open mask: {path}') from ex
        if self._img is not None and tuple(self._img.shape) != tuple(msk.shape):
            raise RuntimeError(f'mismatch between image shape ({list(self._img.shape)}) and mask shape ({list(msk.shape)})')
        self._msk, self.mask_path = msk, path

    def close_mask(self) -> None:
        self._msk = None

    def _mask(self) -> Optional[ImageReader]:
        self._image()
        if self._msk is None and self.mask_path is not None and self._mask_opened:
            self.open_mask(self.mask_path)          # after close()
        return self._msk

    @property
    def reader(self) -> ImageReader:
        '''The image's ``ImageReader``.'''
        return self._image()

    # ---- reading and writing -----------------------------------------

    def shape(self) -> List[int]:
        return [int(n) for n in self._image().shape]

    def modified(self) -> float:
        '''Newest change time of the files making up the image (tclean
        rewrites the residual in place).'''
        return self._image().modified()

    def read_plane(self, index: List[int]):
        return self._image().plane(index[0], index[1]).astype(np.float64)[:, :, None, None]

    def read_spectrum(self, index: List[int]):
        return self._image().spectrum(index[0], index[1], index[2]).astype(np.float64)[None, None, None, :]

    def has_mask(self) -> bool:
        return self._mask() is not None

    def read_mask_plane(self, index: List[int]):
        return self._mask().plane(index[0], index[1]).astype(np.float64)[:, :, None, None]

    def write_mask_plane(self, index: List[int], pixels) -> None:
        self._mask().write_plane(index[0], index[1], pixels)

    def read_mask_chans(self, stokes: int, start: int, count: int):
        '''Mask channels ``start .. start+count-1`` of ``stokes``, ``[x, y, 1, count]``.'''
        return self._mask().chans(stokes, start, count).astype(np.float64)[:, :, None, :]

    def write_mask_chans(self, stokes: int, start: int, pixels) -> None:
        '''Write ``[x, y, count]`` (or ``[x, y, 1, count]``) from channel ``start``.'''
        self._mask().write_chans(stokes, start, pixels)

    def read_mask_spectrum(self, index: List[int]):
        return self._mask().spectrum(index[0], index[1], index[2]).astype(np.float64)[None, None, None, :]

    def mask_pixel(self, chan: List[int], index: List[int]) -> int:
        return int(self._mask().pixel(index[0], index[1], chan[0], chan[1]))

    def has_default_mask(self) -> bool:
        return self._image().info().has_default_mask

    def read_default_mask(self):
        '''The image's own pixel mask for the whole cube, True where a pixel
        is good (all True without one), as ``ia.getregion(getmask=True)``.'''
        good = self._image().default_mask()
        return np.ones(self._image().shape, dtype=bool) if good is None else good

    def statistics(self, index: List[int], mask_path: Optional[str] = None) -> dict:
        '''Statistics of one plane with ``ia.statistics``' keys; with
        ``mask_path``, only where that image is 0.5 or more (as casatools
        reads a mask image).'''
        good = None
        if mask_path:
            mask = self._msk if self._msk is not None and mask_path == self.mask_path else ImageReader.open(mask_path)
            good = mask.plane(index[0], index[1]) >= 0.5
        return self._image().statistics(index[0], index[1], good)

    # ---- coordinates -----------------------------------------------------

    def stokes_labels(self) -> List[str]:
        return list(self._image().info().stokes)

    def coordinates(self) -> dict:
        return self._image().coordinates()

    def coordsys(self) -> dict:
        '''The coordinate system *record* of a CASA image (casatools'
        ``coordsys().torecord()``); the coordinate description otherwise.'''
        record = self._image().casa_record()
        return record if record is not None else self.coordinates()

    def fits_header(self):
        '''``(dict, str)`` from the coordinate description: for display only.'''
        try:
            from ._coordinates import astropy_wcs          # pylint: disable=import-outside-toplevel
            header = astropy_wcs(self.coordinates(), naxis=4).to_header()
        except Exception:
            return (None, '')
        shape = self.shape()
        header.insert(0, ('NAXIS', len(shape)))
        for i, n in enumerate(shape):
            header.insert(i + 1, (f'NAXIS{i + 1}', n))
        unit = self._image().info().units
        if unit:
            header['BUNIT'] = unit
        return ({k: header[k] for k in header}, header.tostring(sep='\n'))

    def close(self) -> None:
        for reader in (self._img, self._msk):
            if reader is not None:
                reader.close()
        self._img = None
        self._msk = None
        # read again on next use, mask included (mask_path is kept)
        self._mask_opened = False
