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
'''``ImageCube``: one field's residual, mask and mask0 as the interactive
GUIs use them, written once in numpy over an ``ImageStore``.  This is the
image logic ``ImagePipe`` held until 2026-11 (channel caching, quantization
to uint8, histograms, spectra, statistics, mask planes), moved here
unchanged, so that it can run where the images are (a remote worker) while
``ImagePipe`` keeps the Bokeh model and the browser messages.

Two differences from ``ImagePipe``:

* The colormap adjustment (bounds and transfer function) is display state
  and belongs to the caller: it is passed to ``channel``/``channel_reply``
  as ``quant``.
* Freshness of the cached plane.  ``track="ctime"`` (default, for a cube
  opened from a path, e.g. by createmask) asks the store for its newest
  file change time on each access, as before.  ``track="generation"`` (a
  cube served by an ``ImagingSession``) trusts ``generation``, which the
  session increments after each cycle of the imager: no file system
  polling.
'''

import sys
from typing import List, Optional

import numpy as np

from ._masks import MaskEditor


def partition(pred, iterable):
    '''Split ``iterable`` into two lists based on ``pred`` (as
    ``cubevis.utils.partition``; not imported from there because importing
    ``cubevis.utils`` loads Bokeh, astropy and regions, and this package
    imports neither Bokeh nor CASA).'''
    trues, falses = [], []
    for item in iterable:
        (trues if pred(item) else falses).append(item)
    return trues, falses

LINEAR = {'bounds': [[], []], 'transfer': {'scaling': 'linear'}}

QUANT_SCALING = {'log':    lambda chan, alpha: np.ma.log(alpha * chan + 1.0) / np.ma.log(alpha + 1.0),
                 'sqrt':   lambda chan:        np.ma.sqrt(chan),
                 'square': lambda chan:        np.square(chan),
                 'gamma':  lambda chan, gamma: np.ma.power(chan, gamma),
                 'power':  lambda chan, alpha: (np.ma.power(alpha, chan) - 1.0) / alpha}


def quantize(nptype, image_plane, quant):
    '''Scale one plane into ``nptype`` (uint8) bins for pseudocolor display.

    Note:
       (1) the histogram sent to GUI is ALWAYS be histogram based on the raw image (handled by ``histogram``)
       (2) the scaled portion of the matrix should be the non-cropped portion
       (3) the lower cropped portion should be set to the min scaled value
       (4) the upper cropped portion should be set to the max scaled value
       (5) a histogram should be created with the resulting (completely filled) array
       (6) then this histogram should be used with the (completely filled) array with numpy.digitize( ) to create
           the uint8 array
    '''
    exclude_below = None
    exclude_above = None
    included = None

    ### Sort out the relationship between channel min/max and user specified min/max
    amin = image_plane.min()           ## array min
    amax = image_plane.max()           ## array max

    ### Extract user bounds (use array bounds as fallback)
    umin = amin if len(quant['bounds'][0]) == 0 else quant['bounds'][0][0]
    umax = amax if len(quant['bounds'][1]) == 0 else quant['bounds'][1][0]

    ### Handle cropping (when user bounds are narrower than data)
    if umin > amin:
        exclude_below = image_plane < umin
    if umax < amax:
        exclude_above = image_plane > umax

    ### Set up access masks
    if exclude_below is not None and exclude_above is not None:
        included = np.logical_not(np.logical_or(exclude_below, exclude_above))
    elif exclude_below is not None:
        included = np.logical_not(exclude_below)
    elif exclude_above is not None:
        included = np.logical_not(exclude_above)

    ### Apply the scaling function to the included pixels
    selected_scaling = quant['transfer']['scaling']
    if selected_scaling != 'linear':
        if selected_scaling not in QUANT_SCALING:
            print(f'''error: ${selected_scaling} is not a known scaling...''', file=sys.stderr)
            result = image_plane
        else:
            normalize = 0 if umin > 0 else -umin
            result = np.ma.zeros(image_plane.shape, image_plane.dtype)

            if included is not None:

                result[included] = QUANT_SCALING[selected_scaling](
                    image_plane[included] + normalize,
                    **quant['transfer']['args']
                )

                # Set excluded regions to min/max of included values
                if exclude_below is not None:
                    result[exclude_below] = result[included].min()
                if exclude_above is not None:
                    result[exclude_above] = result[included].max()
            else:
                result = QUANT_SCALING[selected_scaling](
                    image_plane + normalize,
                    **quant['transfer']['args']
                )
    else:
        result = image_plane

    ### Histogram of the scaled
    edges = np.histogram_bin_edges(result, bins=254, range=(umin, umax))

    return np.digitize(result, edges, right=True).astype(nptype)


class ImageCube:
    '''See the module docstring.  ``store`` is an ``ImageStore``.'''

    def __init__(self, store, *, track: str = 'ctime'):
        if track not in ('ctime', 'generation'):
            raise ValueError(f"track must be 'ctime' or 'generation', not {track!r}")
        self._store = store
        self._track = track
        self.generation = 0
        self._shape = None
        self._chan_shape = None
        self._stokes_labels = None
        self._coordinates = None
        self._mask0_cache = None
        self._mask_statistics = False
        ###
        ### the last channel retrieved is kept around for pixel retrieval
        ###
        self._cached_chan = None
        self._cached_chan_index = None
        self._cached_chan_stamp = None

    # ---- identity -----------------------------------------------------

    @property
    def image_path(self) -> Optional[str]:
        return self._store.image_path

    @property
    def mask_path(self) -> Optional[str]:
        return self._store.mask_path

    @property
    def store(self):
        return self._store

    @property
    def shape(self) -> List[int]:
        if self._shape is None:
            self._shape = list(self._store.shape())
            self._chan_shape = self._shape[0:2]
        return self._shape

    def touch(self) -> None:
        '''The imager rewrote the residual (called by the session).'''
        self.generation += 1

    # ---- the residual ---------------------------------------------------

    def _stamp(self):
        return self._store.modified() if self._track == 'ctime' else self.generation

    def _get_chan(self, index):
        stamp = self._stamp()
        if self._cached_chan is None or \
           stamp != self._cached_chan_stamp or \
           self._cached_chan_index[0] != index[0] or \
           self._cached_chan_index[1] != index[1]:
            chan_shape = self.shape[0:2]
            ###
            ### ensure that the channel index is within cube shape
            ###
            index = list(index)     # index is potentially a python tuple
            index[0] = min(index[0], chan_shape[0] - 1)
            index[1] = min(index[1], chan_shape[1] - 1)
            index[0] = max(index[0], 0)
            index[1] = max(index[1], 0)
            self._cached_chan_index = index
            self._cached_chan_stamp = stamp
            self._cached_chan = self._store.read_plane(index)
        return self._cached_chan

    def pixel_value(self, chan, index):
        channel = self._get_chan(chan)
        index[0] = min(index[0], channel.shape[0] - 1)
        index[1] = min(index[1], channel.shape[1] - 1)
        index[0] = max(index[0], 0)
        index[1] = max(index[1], 0)
        return np.squeeze(channel[index[0], index[1]])

    def stokes_labels(self):
        """Returns stokes plane labels"""
        if self._stokes_labels is None:
            self._stokes_labels = self._store.stokes_labels()
        return self._stokes_labels

    ### ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ----
    ### the element type of image pixels retrieved from the CASA image are float64, but it
    ### seems like 256 is the greatest number of colors in the colormaps currrently used
    ### for pseudo color within interactive clean...
    ### ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ----
    def channel(self, index, pixel_type, quant=None):
        """Retrieve one channel from the image cube. The `index` should be a
        two element list of integers. The first integer is the ''stokes'' axis
        in the image cube. The second integer is the ''channel'' axis in the
        image cube.

        Parameters
        ----------
        index: [ int, int ]
            list containing first the ''stokes'' index and second the ''channel'' index
        pixel_type: numpy type
            the numpy type for the pixel elements of the returned channel
        quant: dict
            colormap adjustment ``{'bounds': [[min],[max]], 'transfer': {'scaling': ..., 'args': {...}}}``;
            linear over the whole range when ``None``
        """
        if np.issubdtype(pixel_type, np.integer):
            return quantize(pixel_type, np.squeeze(self._get_chan(index)),
                            LINEAR if quant is None else quant).transpose()
        return np.squeeze(self._get_chan(index)).astype(pixel_type).transpose()

    def channel_reply(self, index, quant=None, *, stats=False, hist_bins=None):
        '''Everything the browser needs for one channel step, in one call
        (one round trip when remote): the uint8 plane, the mask plane, mask0,
        statistics and the histogram, as plain numpy (the caller packs them
        for Bokeh).'''
        reply = {'img': self.channel(index, np.uint8, quant)}
        if self.have_mask():
            reply['msk'] = self.mask(index)
        mask0 = self.mask0(index)
        if mask0 is not None:
            reply['msk0'] = mask0
        reply['hist'] = self.histogram(index, hist_bins) if hist_bins else {}
        if stats:
            reply['stats'] = self.statistics(index)
        return reply

    def histogram(self, index, nbins):
        """Calculate histogram (Bokeh Quad) extents for update of colormap adjuster (or anything
        else that wants a histogram of image intensities.

        Parameters
        ----------
        index: [ int, int ]
            list containing first the ''stokes'' index and second the ''channel'' index
        nbins: int
            number of histogram bins
        """
        chan = self._get_chan(index)
        bins = np.linspace(chan.min(), chan.max(), nbins + 1)
        hist, edges = np.histogram(chan, density=False, bins=bins)
        return dict(left=list(edges[:-1]), right=list(edges[1:]), top=list(hist), bottom=[0] * len(hist))

    def spectrum(self, index, mask=False):
        """Retrieve one spectrum from the image cube. The `index` should be a
        three element list of integers. The first integer is the ''right
        ascension'' axis, the second integer is the ''declination'' axis,
        and the third integer is the ''stokes'' axis.

        Parameters
        ----------
        index: [ int, int, int ]
            list containing first the ''right ascension'', the ''declination'' and
            the ''stokes'' axis
        """
        shape = self.shape
        index = list(map(lambda i: 0 if i is None else i, index))
        if index[0] >= shape[0]:
            index[0] = shape[0] - 1
        if index[1] >= shape[1]:
            index[1] = shape[1] - 1
        result_mask = np.squeeze(self._store.read_mask_spectrum(index)) if self.have_mask() and mask else None
        result = np.squeeze(self._store.read_spectrum(index))
        ### should return spectral freq etc.
        ### here for X rather than just the index
        try:
            if mask:
                return {'chan': list(range(len(result))), 'pixel': list(result)}, None if result_mask is None else list(result_mask.astype(bool))
            else:
                return {'chan': list(range(len(result))), 'pixel': list(result)}
        except Exception:
            ## In this case, result is not iterable (e.g.) only one channel in the cube.
            ## A zero length numpy ndarray has no shape and looks like a float but it is
            ## an ndarray.
            if mask:
                return {'chan': [0], 'pixel': [float(result)]}, None if result_mask is None else [bool(result_mask)]
            else:
                return {'chan': [0], 'pixel': [float(result)]}

    def statistics_config(self, use_mask=None):
        '''Configure the behavior of the statistics function.
        use_mask indicates that if a mask is available, the statistics should
        be based upon the portion of the image included in the mask instead of
        the whole channel.'''
        if self.mask_path and use_mask is not None:
            self._mask_statistics = bool(use_mask)

    def statistics(self, index):
        """Retrieve statistics for one channel from the image cube. The `index`
        should be a two element list of integers. The first integer is the
        ''stokes'' axis in the image cube. The second integer is the ''channel''
        axis in the image cube.

        Parameters
        ----------
        index: [ int, int ]
            list containing first the ''stokes'' index and second the ''channel'' index
        """
        def singleton(potential_nonlist):
            # convert a list of a single element to the element
            return potential_nonlist if len(potential_nonlist) != 1 else potential_nonlist[0]

        def sort_result(unsorted_dictionary):
            part = partition(lambda s: (s.startswith('trc') or s.startswith('blc')), sorted(unsorted_dictionary.keys()))
            return {k: unsorted_dictionary[k] for k in part[1] + part[0]}

        rawstats = self._store.statistics(index, self.mask_path if self._mask_statistics else None)
        return sort_result({k: singleton([x.item() for x in v]) if isinstance(v, np.ndarray) else v for k, v in rawstats.items()})

    # ---- masks ----------------------------------------------------------

    def have_mask0(self):
        """Check to see if the synthesis imaging 'mask0' mask exists

        Returns
        -------
        bool:
            ''True'' if the cube contains an internal ''mask0'' mask otherwise ''False''
        """
        return self._store.has_default_mask()

    def mask0(self, index):
        """Within the image, there can be an arbitrary number of INTERNAL masks. They can
        have arbitrary names. The synthesis imaging module uses a mask named 'mask0'. This
        mask is used in processing (it MAY represent the beam).

        Parameters
        ----------
        index: [ int, int ]
            list containing first the ''stokes'' index and second the ''channel'' index
        """
        ### ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ----
        ### tclean does not maintain mask0. Instead, calls to tclean can result in the
        ### internal, mask0 being lost. Because of this, once a good copy of this internal
        ### mask is retrieved it is reused. Urvashi says that reusing one copy throughout
        ### should be fine (Fri Mar 31 13:54:17 EDT 2023)
        ### ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ----
        if self._mask0_cache is None and self.have_mask0():
            self._mask0_cache = self._store.read_default_mask()
        if self._mask0_cache is not None:
            return self._mask0_cache[:, :, index[0], index[1]]
        return None

    def have_mask(self):
        """Check to see if a mask exists.

        Returns
        -------
        bool:
            ''True'' if a mask cube is available otherwise ''False''
        """
        return self._store.has_mask()

    def mask(self, index, modify=False):
        """Retrieve one channel mask from the mask cube. The `index` should be a
        two element list of integers. The first integer is the ''stokes'' axis
        in the image cube. The second integer is the ''channel'' axis in the
        image cube.

        Parameters
        ----------
        index: [ int, int ]
            list containing first the ''stokes'' index and second the ''channel'' index
        modify: boolean
            If true, it implies that the channel mask is being retrieved for modification
            and updating the channel on disk. If false, it implies that the channel mask
            is being retrieved for display.
        """
        if not self.have_mask():
            raise RuntimeError(f'cannot retrieve mask at {repr(index)} because no mask cube exists')
        plane = np.squeeze(self._store.read_mask_plane(index))
        return plane.astype(np.bool_).transpose() if modify == False else plane

    def mask_value(self, chan, index):
        if not self.have_mask():
            raise RuntimeError(f'cannot retrieve mask at {repr(index)} because no mask cube exists')
        try:
            return self._store.mask_pixel(chan, index)
        except Exception:
            pass
        return -1

    def set_mask_name(self, new_mask_path):
        self._store.close_mask()
        self._store.open_mask(new_mask_path)

    def put_mask(self, index, mask):
        """Replace one channel mask with the mask specified as the second parameter.
        The `index` should be a two element list of integers. The first integer is the
        ''stokes'' axis in the image cube. The second integer is the ''channel'' axis
        in the image cube. The assumption is that the :code:`mask` parameter was retrieved
        from the mask cube using the :code:`mask(...)` function with the :code:`modify`
        parameter set to :code:`True`.

        Parameters
        ----------
        index: [ int, int ]
            list containing first the ''stokes'' index and second the ''channel'' index
        mask: numpy.ndarray
            two dimensional array to replace the existing mask for the channel specified
            by :code:`index`
        """
        if not self.have_mask():
            raise RuntimeError(f'cannot replace mask at {repr(index)} because no mask cube exists')
        if mask.dtype == bool:
            ### cannot put bools with putchunk
            self._store.write_mask_plane(index, mask.astype(np.uint8))
        else:
            self._store.write_mask_plane(index, mask)

    def edit_mask(self, op, scope, index, *, polygon=None, src=None):
        '''One mask edit, done where the mask is stored (design §2.2, step 2).

        Parameters
        ----------
        op: str
            ``'add'`` or ``'subtract'`` (a region or another channel's mask),
            ``'clear'``, ``'fill'`` or ``'invert'``
        scope: str
            ``'chan'`` (the channel ``index``) or ``'cube'`` (every channel of
            ``index``'s stokes plane)
        index: [ int, int ]
            ``[stokes, channel]``
        polygon: dict
            ``{'xs': [...], 'ys': [...]}``, the region's vertices in Bokeh data
            coordinates (``_masks.polygon_pixels`` gives its pixels)
        src: [ int, int ]
            ``[stokes, channel]`` whose mask is added or subtracted

        Returns
        -------
        dict:
            what the browser should update besides the mask: ``{'clear_region': True}``
            when a channel's own mask was subtracted from it, else empty
        '''
        if not self.have_mask():
            raise RuntimeError(f'cannot edit the mask at {repr(index)} because no mask cube exists')
        return MaskEditor(self._store, self.shape).edit(op, scope, index, polygon=polygon, src=src)

    # ---- description ----------------------------------------------------

    def coordinates(self):
        '''The coordinate description (design §2.5; ``cubevis.imaging._coordinates``),
        made once.'''
        if self._coordinates is None:
            self._coordinates = self._store.coordinates()
        return self._coordinates

    def fits_header(self):
        '''``(dict, str)``: the store's FITS header, for stores that have one
        (display and export only; nothing in the GUI depends on it).'''
        return self._store.fits_header()

    def coorddesc(self):
        '''``{'csys': ..., 'shape': ..., 'coordinates': ...}`` for
        ``convert_masks``.  ``coordinates`` is the coordinate description,
        which ``convert_masks`` uses for world coordinates; ``csys`` is the
        casatools ``coordsys`` when the store has one (as before: CASA
        regions, ``format='region'``, need it), else the description.'''
        desc = self.coordinates()
        csys = self._store.coordsys() if hasattr(self._store, 'coordsys') else desc
        return {'csys': csys, 'shape': tuple(self.shape), 'coordinates': desc}

    def close(self):
        self._stokes_labels = None
        self._store.close()
