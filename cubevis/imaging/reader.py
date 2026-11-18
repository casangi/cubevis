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
'''``ImageReader``: an image cube as planes, spectra and plain values,
whatever holds it -- a CASA image, an xradio image in Zarr, or an
``xarray.Dataset`` in hand.  No casatools, no Bokeh.

    reader = ImageReader.open("ngc5921.residual")      # or "sky.img.zarr", or a dataset
    reader.info()                                      # ImageInfo: shape, Stokes, frequencies, beam ...
    reader.plane(0, 12)                                # [x, y] of Stokes 0, channel 12
    reader.spectrum(128, 128, 0)                       # every channel of one pixel
    reader.write_plane(0, 12, pixels)                  # where the source can be written

It is to images what ``CalTableReader`` is to calibration tables: code above
it (``XarrayImageStore``, and through that ``ImageCube`` and interactive
clean) is written against this class only, so the dataset below can follow
xradio's schema as that settles.  Below it is ``cubevis.xarray.image``:
``open_image`` gives the dataset -- xradio's image layout for both kinds of
source -- and ``describe_dataset`` what it holds.  A CASA image may be a
paged image or a concatenation of images (``ia.imageconcat``).

Indices are CASA's, as interactive clean uses them: ``x``, ``y`` (the
dataset's ``l``, ``m``), then Stokes, then channel.  An image without a
polarization or frequency dim has one plane there.

Pixels are read when asked for, each time: nothing is cached here, so
what the imager wrote a moment ago is what comes back.  What *is* kept is
the description (shape, coordinates, beam); it is read again when the
source's files show a rewrite (a CASA image's ``table.dat``, a Zarr
store's metadata), which is how a restoring beam added by a later cycle is
seen.
'''
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

_DIMS = ('l', 'm', 'polarization', 'frequency')


def _image_package():
    from ..xarray import image                       # pylint: disable=import-outside-toplevel
    return image


@dataclass(frozen=True)
class ImageInfo:
    '''What an image is.'''
    source: str                        # a path, or "<dataset>"
    source_format: str                 # "casa-image", "zarr" or "dataset"
    variable: str                      # the dataset's pixel variable ("SKY")
    shape: tuple[int, int, int, int]   # x, y, Stokes, channels
    dtype: str
    units: str                         # brightness unit, "" when not known
    stokes: tuple[str, ...]            # labels; () without a polarization axis
    frequency: Optional[tuple[float, ...]]   # Hz, a value per channel; None without a spectral axis
    spectral_frame: str
    rest_frequency: Optional[float]    # Hz
    has_direction: bool                # direction axes with a known coordinate system
    has_beam: bool
    has_default_mask: bool             # a pixel mask (CASA) / flag (xradio) of the image itself
    writable: bool                     # write_plane and write_chans work


class ImageReader:
    '''One image.  Make it with ``ImageReader.open``.'''

    def __init__(self, source: Any, *, variable: Optional[str] = None):
        self._source = source
        self._variable_asked = variable
        self._format = _image_package().source_format(source)
        self._path = None if self._format == 'dataset' else os.path.abspath(os.path.expanduser(os.fspath(source)))
        self._ds = None
        self._desc: Optional[dict] = None
        self._stamp = None
        self._files: Optional[list] = None
        self._coordinates: Optional[dict] = None

    @classmethod
    def open(cls, source: Any, *, variable: Optional[str] = None) -> 'ImageReader':
        '''``source``: the path of a CASA image or of a Zarr store holding an
        image dataset, or an ``xarray.Dataset`` in xradio's image layout.
        ``variable`` names the pixel variable when the dataset has several
        (``SKY``, ``SKY_RESIDUAL``, ``SKY_MODEL`` ...).'''
        reader = cls(source, variable=variable)
        reader._dataset()
        return reader

    # ---- the dataset, read again when the source was rewritten ------------

    def _source_stamp(self):
        if self._format == 'dataset':
            return None
        if self._format == 'casa-image':
            ### table.dat; for a concatenated image, its list and every part's table.dat
            files = self._files or [os.path.join(self._path, name) for name in ('table.dat', 'imageconcat.json')]
        else:
            files = [os.path.join(self._path, name) for name in ('zarr.json', '.zmetadata', '.zattrs', '.zgroup')]
        stamp = []
        for name in files:
            try:
                st = os.stat(name)
                stamp.append((name, st.st_mtime_ns, st.st_size, st.st_ino))
            except OSError:
                if self._files:
                    stamp.append((name, None))            # a part went away: read the description again
        if not stamp:
            raise FileNotFoundError(self._path)
        return tuple(stamp)

    def _dataset(self):
        stamp = self._source_stamp()
        if self._ds is None or stamp != self._stamp:
            image = _image_package()
            ds = image.open_image(self._source) if self._format != 'zarr' else image.open_image(self._path, chunks=None)
            self._desc = image.describe_dataset(ds, self._variable_asked)
            self._files = list((ds.attrs.get('casa_image') or {}).get('files') or []) if self._format == 'casa-image' else None
            self._ds, self._stamp, self._coordinates = ds, self._source_stamp(), None
        return self._ds

    def _description(self) -> dict:
        self._dataset()
        return self._desc

    def refresh(self) -> None:
        '''Read the description again on next use.'''
        if self._format != 'dataset':
            self._ds = None

    def close(self) -> None:
        '''Let go of the dataset (it is opened again on next use).  Nothing
        is held open between calls in any case.'''
        if self._format != 'dataset':
            if self._ds is not None:
                self._ds.close()
            self._ds = None

    # ---- what it is -------------------------------------------------------

    @property
    def path(self) -> Optional[str]:
        return self._path

    @property
    def source_format(self) -> str:
        return self._format

    @property
    def shape(self) -> tuple:
        sizes = self._description()['sizes']
        return tuple(sizes[d] for d in _DIMS)

    def info(self) -> ImageInfo:
        ds, desc = self._dataset(), self._description()
        frequency = desc['frequency']
        spectral = desc['spectral'] or {}
        return ImageInfo(
            source=self._path or '<dataset>', source_format=self._format, variable=desc['variable'],
            shape=self.shape, dtype=desc['dtype'], units=desc['units'],
            stokes=tuple(desc['polarization']),
            frequency=None if frequency is None else tuple(float(f) for f in frequency),
            spectral_frame=str(spectral.get('frame', '')), rest_frequency=spectral.get('rest_frequency'),
            has_direction=desc['direction'] is not None and None not in desc['direction']['increment'],
            has_beam=desc['beam'] is not None, has_default_mask=desc['flag'] is not None,
            writable=self._writable(ds))

    def modified(self) -> float:
        '''Newest change time of the source's files (0 for a dataset in hand).'''
        if self._path is None:
            return 0.0
        if self._format == 'casa-image':
            return _image_package().CasaImage(self._path).modified()
        newest = 0.0
        for root, _, files in os.walk(self._path):
            for name in files:
                try:
                    newest = max(newest, os.path.getctime(os.path.join(root, name)))
                except OSError:
                    pass
        return newest

    def beam(self, stokes: int = 0, chan: int = 0) -> Optional[dict]:
        '''``{'major': arcsec, 'minor': arcsec, 'pa': deg}`` of one plane;
        None without a beam.'''
        ds, desc = self._dataset(), self._description()
        if desc['beam'] is None:
            return None
        var = ds[desc['beam']]
        index = {'time': 0, 'polarization': stokes, 'frequency': chan}
        values = np.asarray(var.isel({d: i for d, i in index.items() if d in var.dims}).values, dtype=float)
        if not np.isfinite(values).all():
            return None
        scale = {'rad': 1.0, 'deg': math.pi / 180.0, 'arcsec': math.pi / 648000.0}.get(str(var.attrs.get('units', 'rad')), 1.0)
        major, minor, pa = (float(v) * scale for v in values)
        return {'major': math.degrees(major) * 3600.0, 'minor': math.degrees(minor) * 3600.0, 'pa': math.degrees(pa)}

    def casa_record(self) -> Optional[dict]:
        '''The CASA coordinate system record (``coordsys().torecord()``)
        when the source is a CASA image, else None.'''
        record = self._dataset().attrs.get('coordinate_system')
        return record if isinstance(record, dict) else None

    def casa_beam(self) -> Optional[dict]:
        '''``ia.restoringbeam()``'s record for a CASA image, else None.'''
        imageinfo = self._dataset().attrs.get('imageinfo')
        return _image_package().coords.restoring_beam(imageinfo) if isinstance(imageinfo, dict) else None

    def coordinates(self) -> dict:
        '''The coordinate description (``cubevis.imaging._coordinates``).
        From the CASA record for a CASA image -- what ``CasatoolsImageStore``
        gives, value for value --, else from the dataset's own coordinates.'''
        self._dataset()
        if self._coordinates is None:
            from ._coordinates import from_casa_coordsys, from_image_info     # pylint: disable=import-outside-toplevel
            desc = self._desc
            record = self.casa_record()
            if record is not None:
                values = None
                if desc['frequency'] is not None:
                    co = _image_package().coords
                    keys = [k for k in co.coordinate_keys(record) if k.startswith('spectral')]
                    unit = str(record[keys[0]].get('unit', 'Hz')) if keys else 'Hz'
                    ### the description gives the values in the record's unit, as toworld does
                    values = [float(v) for v in desc['frequency'] / co.FREQUENCY_TO_HZ.get(unit, 1.0)]
                self._coordinates = from_casa_coordsys(record, list(self.shape), spectral_values=values,
                                                       beam=self.casa_beam(), brightness_unit=desc['units'])
            else:
                self._coordinates = from_image_info(desc, beam=self.beam())
        return self._coordinates

    # ---- pixels -----------------------------------------------------------

    def _pick(self, var, x, y, stokes, chan) -> np.ndarray:
        '''``var`` indexed along l, m, polarization, frequency (each an int or
        a slice), as ``[x, y, stokes, chan]`` without the axes given as ints.'''
        index = {'l': x, 'm': y, 'polarization': stokes, 'frequency': chan}
        sel = {'time': 0} if 'time' in var.dims else {}
        order = []
        for dim in _DIMS:
            if dim in var.dims:
                sel[dim] = index[dim]
                if isinstance(index[dim], slice):
                    order.append(dim)
            elif isinstance(index[dim], slice):
                order.append(None)                    # an axis the image lacks, asked for as a slice
            elif index[dim] not in (0, -1):
                raise IndexError(f'{dim} index {index[dim]}: the image has no {dim} axis')
        out = var.isel(sel).transpose(*[d for d in order if d is not None]).values
        for position, dim in enumerate(order):
            if dim is None:
                out = np.expand_dims(out, position)
        return out

    def _sky(self):
        return self._dataset()[self._description()['variable']]

    def plane(self, stokes: int, chan: int) -> np.ndarray:
        '''``[x, y]``: one channel of one Stokes plane.'''
        return self._pick(self._sky(), slice(None), slice(None), int(stokes), int(chan))

    def spectrum(self, x: int, y: int, stokes: int) -> np.ndarray:
        '''``[chan]``: every channel at one pixel.'''
        return self._pick(self._sky(), int(x), int(y), int(stokes), slice(None))

    def chans(self, stokes: int, start: int, count: int) -> np.ndarray:
        '''``[x, y, count]``: channels ``start .. start+count-1`` of one Stokes plane.'''
        return self._pick(self._sky(), slice(None), slice(None), int(stokes), slice(int(start), int(start) + int(count)))

    def pixel(self, x: int, y: int, stokes: int, chan: int):
        return self._pick(self._sky(), int(x), int(y), int(stokes), int(chan))[()]

    def default_mask(self) -> Optional[np.ndarray]:
        '''``[x, y, stokes, chan]`` boolean, True where a pixel is **good**
        (CASA's sense), for the whole cube; None when the image has no mask
        of its own.'''
        ds, desc = self._dataset(), self._description()
        if desc['flag'] is None:
            return None
        return ~self._pick(ds[desc['flag']], slice(None), slice(None), slice(None), slice(None))

    def default_mask_plane(self, stokes: int, chan: int) -> Optional[np.ndarray]:
        ds, desc = self._dataset(), self._description()
        if desc['flag'] is None:
            return None
        return ~self._pick(ds[desc['flag']], slice(None), slice(None), int(stokes), int(chan))

    # ---- writing ------------------------------------------------------------

    def _writable(self, ds) -> bool:
        if self._format == 'casa-image':
            files = [f for f in (self._files or []) if f.endswith('table.dat')]
            return bool(files) and all(os.access(f, os.W_OK) for f in files)
        if self._format == 'zarr':
            return os.access(self._path, os.W_OK)
        return isinstance(ds[self._desc['variable']].variable._data, np.ndarray)      # pylint: disable=protected-access

    def _write(self, stokes: int, chan, pixels: np.ndarray) -> None:
        '''``pixels`` ``[x, y]`` (``chan`` an int) or ``[x, y, n]`` (a slice).'''
        ds, desc = self._dataset(), self._description()
        var = ds[desc['variable']]
        index = {'l': slice(None), 'm': slice(None), 'polarization': int(stokes), 'frequency': chan, 'time': 0}
        given = ['l', 'm'] + (['frequency'] if isinstance(chan, slice) else [])
        for dim in ('polarization', 'frequency'):
            if dim not in var.dims and index[dim] not in (0, slice(0, 1)):
                raise IndexError(f'the image has no {dim} axis')
        if 'frequency' not in var.dims and isinstance(chan, slice):
            pixels, given = pixels[:, :, 0], ['l', 'm']
        if self._format == 'casa-image':
            axis_of = ds.attrs['casa_image']['pixel_axis_of_dim']
            image = _image_package().CasaImage(self._path)
            key = [slice(None)] * image.ndim
            for dim, axis in axis_of.items():
                key[axis] = index[dim]
            kept = sorted((axis_of[d], d) for d in given)
            image[tuple(key)] = np.transpose(pixels, [given.index(d) for _, d in kept])
        elif self._format == 'zarr':
            import zarr                                   # pylint: disable=import-outside-toplevel
            array = zarr.open_group(self._path, mode='r+')[desc['variable']]
            kept = [d for d in var.dims if d in given]
            array[tuple(index[d] for d in var.dims)] = np.transpose(pixels, [given.index(d) for d in kept]).astype(var.dtype)
        else:
            if not self._writable(ds):
                raise PermissionError('this dataset is not held in memory: it cannot be written')
            kept = [d for d in var.dims if d in given]
            var.values[tuple(index[d] for d in var.dims)] = np.transpose(pixels, [given.index(d) for d in kept])

    def write_plane(self, stokes: int, chan: int, pixels) -> None:
        '''Write ``[x, y]`` to one channel of one Stokes plane.'''
        nx, ny = self.shape[0:2]
        self._write(stokes, int(chan), np.asarray(pixels).reshape(nx, ny))

    def write_chans(self, stokes: int, start: int, pixels) -> None:
        '''Write ``[x, y, count]`` from channel ``start``.'''
        pixels = np.asarray(pixels)
        if pixels.ndim == 4:
            pixels = pixels[:, :, 0, :]
        self._write(stokes, slice(int(start), int(start) + pixels.shape[2]), pixels)

    # ---- statistics -------------------------------------------------------------

    def _beam_pixels(self, stokes: int, chan: int) -> Optional[float]:
        beam, direction = self.beam(stokes, chan), self._description()['direction']
        if beam is None or direction is None or None in direction['increment']:
            return None
        pixel = abs(direction['increment'][0] * direction['increment'][1])
        to_rad = math.pi / 648000.0
        return math.pi / (4.0 * math.log(2.0)) * beam['major'] * to_rad * beam['minor'] * to_rad / pixel if pixel else None

    def statistics(self, stokes: int, chan: int, good: Optional[np.ndarray] = None) -> dict:
        '''Statistics of one plane with ``ia.statistics``' keys and values.
        Pixels the image's own mask excludes are left out, and those where
        ``good`` (``[x, y]`` boolean) is False.  The formatted positions
        (``blcf`` ...) need astropy and a direction coordinate system.'''
        s, c = int(stokes), int(chan)
        nx, ny = self.shape[0:2]
        plane = self.plane(s, c).astype(np.float64)
        ok = np.isfinite(plane)
        own = self.default_mask_plane(s, c)
        if own is not None:
            ok &= own
        if good is not None:
            ok &= np.asarray(good, dtype=bool)
        stats = {'blc': np.array([0, 0, s, c]), 'trc': np.array([nx - 1, ny - 1, s, c]),
                 'npts': np.array([float(ok.sum())])}
        values = plane[ok]
        unit = self._description()['units'].lower()
        per_beam = self._beam_pixels(s, c) if unit.endswith('/beam') else None
        if not values.size:
            ### nothing unmasked: casatools gives every number as an empty array
            for key in ('npts', 'sum', 'sumsq', 'mean', 'rms', 'sigma', 'min', 'max'):
                stats[key] = np.array([])
            if per_beam or unit.endswith('/pixel'):
                stats['flux'] = np.array([])
        else:
            total, sumsq = float(values.sum()), float(np.square(values).sum())
            flat = np.where(ok, plane, np.nan)
            maxpos = np.unravel_index(np.nanargmax(flat), flat.shape)
            minpos = np.unravel_index(np.nanargmin(flat), flat.shape)
            stats.update({
                'sum': np.array([total]), 'sumsq': np.array([sumsq]),
                'mean': np.array([total / values.size]),
                'rms': np.array([math.sqrt(sumsq / values.size)]),
                'sigma': np.array([float(values.std(ddof=1)) if values.size > 1 else 0.0]),
                'min': np.array([float(values.min())]), 'max': np.array([float(values.max())]),
                'minpos': np.array([int(minpos[0]), int(minpos[1]), s, c]),
                'maxpos': np.array([int(maxpos[0]), int(maxpos[1]), s, c]),
            })
            if per_beam:
                stats['flux'] = np.array([total / per_beam])
            elif unit.endswith('/pixel'):
                stats['flux'] = np.array([total])
        for key, pos in (('blcf', stats['blc']), ('trcf', stats['trc']),
                         ('minposf', stats.get('minpos')), ('maxposf', stats.get('maxpos'))):
            if pos is not None:
                text = self.format_position(pos)
                if text is not None:
                    stats[key] = text
        return stats

    def format_position(self, pixel) -> Optional[str]:
        '''"hh:mm:ss.sss, +dd.mm.ss.sss, I, 1.415e+09Hz" for a pixel ``[x, y,
        stokes, chan]``, as casatools writes positions; None without astropy
        or a direction coordinate system.'''
        try:
            from ._coordinates import astropy_wcs          # pylint: disable=import-outside-toplevel
            desc = self.coordinates()
            if desc.get('direction') is None:
                return None
            lon, lat = astropy_wcs(desc, naxis=2).wcs_pix2world(float(pixel[0]), float(pixel[1]), 0)
        except Exception:
            return None
        hours = (float(lon) % 360.0) / 15.0
        h = int(hours)
        m = int((hours - h) * 60)
        sec = (hours - h - m / 60.0) * 3600.0
        if round(sec, 3) >= 60.0:
            sec, m = 0.0, m + 1
        if m >= 60:
            m, h = m - 60, (h + 1) % 24
        sign = '-' if lat < 0 else '+'
        a = abs(float(lat))
        d = int(a)
        dm = int((a - d) * 60)
        ds = (a - d - dm / 60.0) * 3600.0
        if round(ds, 3) >= 60.0:
            ds, dm = 0.0, dm + 1
        if dm >= 60:
            dm, d = dm - 60, d + 1
        parts = [f'{h:02d}:{m:02d}:{sec:06.3f}', f'{sign}{d:02d}.{dm:02d}.{ds:06.3f}']
        stokes = desc.get('stokes') or []
        if int(pixel[2]) < len(stokes):
            parts.append(stokes[int(pixel[2])])
        spectral = desc.get('spectral')
        if spectral and int(pixel[3]) < len(spectral['values']):
            parts.append(f'{spectral["values"][int(pixel[3])]:.4g}' + (spectral.get('unit') or 'Hz'))
        return ', '.join(parts)

    def __repr__(self) -> str:
        return f'<ImageReader {self._path or "dataset"} ({self._format})>'
