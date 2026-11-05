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
'''Implementation of ``ImagePipe`` class which provides a ``websockets``
implementation for CASA images which allows for interacitve display
of image cube channels in response to user input.

Since 2026-11 the image itself is read and written by an ``ImageCube``
(``cubevis.imaging``): ``ImagePipe`` keeps the Bokeh model, the messages
from the browser and the colormap adjustment, and asks the cube for
everything else -- so the cube can be one served by an ``ImagingSession``
(interactive clean, later a remote one) or one opened from a path.'''

import json
from uuid import uuid4

from bokeh.model import Model
from bokeh.core.properties import Tuple, String, Int, Instance, Nullable
from bokeh.plotting import ColumnDataSource
from .. import BokehInit
from ..state import casalib_url, cubevisjs_url   # noqa: F401 (imported here since before 2026-11)
from ..transport import Comm

import numpy as np

from ...utils import pack_arrays, resource_manager, strip_arrays

class ImagePipe( Model ):
    """The `ImagePipe` allows for updates to Bokeh plots from a CASA or CNGI

    image. This is done using a `websocket`. A `ImagePipe` is created with
    the path to the image (or with an ``ImageCube``), and then it is used as
    the input to an `ImageDataSource` or a `SpectraDataSource`. This allows a
    single CASA or CNGI imge to be opened once and shared among multiple Bokeh
    plots, for example ploting an image channel and a plot of a spectrum from
    the image cube.
    """

    comm = Instance(Comm)
    shape = Tuple( Int, Int, Int, Int, help="shape: [ RA, DEC, Stokes, Spectral ]" )
    dataid = String( )
    fits_header_json = Nullable( String, help="""JSON representation of image FITS header for world coordinate labeling""" )
    _histogram_source = Nullable(Instance(ColumnDataSource), help='''
    data source for (raw) image channel histogram of intensities used with a "figure.quad(...)"
    ''')

    ###################################################################
    ### filled from cubevis.bokeh.state._initialize._order_bokeh_js ###
    ###################################################################
    #__javascript__ = [ casalib_url( ), cubevisjs_url( ) ]

    def __init__( self, image=None, *args, mask=None, stats=False, lock=None, cube=None, **kwargs ):

        self._comm_mgr = BokehInit.get_app_context( ).comm_mgr
        kwargs['dataid'] = str(uuid4( ))
        # `lock` (2026-09): shares handler-execution serialization with
        # another Comm known to touch the same underlying data -- e.g.
        # a Cube that owns this ImagePipe also opens its own "cube mask
        # control" comm, and both ultimately read/write the same image/
        # mask arrays. The per-comm_id send-side throttle (CommMgr.send)
        # only ever protects one comm_id against itself, not two comms
        # against each other, so without this two different comms'
        # handlers could run concurrently against this same ImagePipe's
        # state once either is made async (see CommMgr.open()'s `lock`
        # parameter docstring for the full rationale). None (the
        # default, e.g. for an ImagePipe used standalone, not through a
        # Cube) falls back to CommMgr.open()'s own default of a fresh,
        # private-to-this-comm Lock.
        kwargs['comm'] = self._comm_mgr.open( description="image cube updates", lock=lock )

        super( ).__init__( *args, **kwargs, )

        if cube is None:
            if image is None:
                raise RuntimeError('ImagePipe requires an image path or an ImageCube')
            from ...imaging import open_image_cube
            cube = open_image_cube( image, mask )
        self.__cube = cube

        self.__fits_header = None
        self.__fits_header_str = ''
        resource_manager( ).reg_at_exit( self, '__del__' )
        self._stats = stats
        self.shape = list(self.__cube.shape)
        if not self.fits_header_json:
            self.__fits_header, self.__fits_header_str = self.__cube.fits_header( )
            if self.__fits_header:
                self.fits_header_json = json.dumps(strip_arrays(self.__fits_header))

        ###
        ### quantization controls to affect how pseudo colors are displayed
        ### (display state: kept here, passed to the cube)
        ###
        self.__quant_adjustments = { 'bounds': [ [ ], [ ] ],
                                     'transfer': {'scaling': 'linear'} }

        self.comm.register( self.dataid, self._image_message_handler )

    @property
    def cube( self ):
        '''the ``ImageCube`` this pipe displays'''
        return self.__cube

    def pixel_value( self, chan, index ):
        return self.__cube.pixel_value( chan, index )

    def stokes_labels( self ):
        """Returns stokes plane labels"""
        return self.__cube.stokes_labels( )

    def channel( self, index, pixel_type ):
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
        """
        return self.__cube.channel( index, pixel_type, self.__quant_adjustments )

    def have_mask0( self ):
        """Check to see if the synthesis imaging 'mask0' mask exists"""
        return self.__cube.have_mask0( )

    def mask0( self, index ):
        """The synthesis imaging 'mask0' internal mask for one channel (or None)"""
        return self.__cube.mask0( index )

    def have_mask( self ):
        """Check to see if a mask exists."""
        return self.__cube.have_mask( )

    def mask( self, index, modify=False ):
        """Retrieve one channel mask from the mask cube (see ``ImageCube.mask``)"""
        return self.__cube.mask( index, modify )

    def mask_value( self, chan, index ):
        return self.__cube.mask_value( chan, index )

    def set_mask_name( self, new_mask_path ):
        self.__cube.set_mask_name( new_mask_path )

    def put_mask( self, index, mask ):
        """Replace one channel mask (see ``ImageCube.put_mask``)"""
        self.__cube.put_mask( index, mask )

    def spectrum( self, index, mask=False ):
        """Retrieve one spectrum from the image cube (see ``ImageCube.spectrum``)"""
        return self.__cube.spectrum( index, mask )

    def histogram_source( self, data ):
        if not self._histogram_source:
            self._histogram_source = ColumnDataSource( data=data )
        return self._histogram_source

    async def _image_message_handler( self, cmd ):
        if cmd['action'] == 'channel':
            reply = self.__cube.channel_reply( cmd['index'], self.__quant_adjustments, stats=bool(self._stats),
                                               hist_bins=len(self._histogram_source.data['top']) if self._histogram_source else None )
            mask = { } if 'msk' not in reply else { 'msk': [ pack_arrays( reply['msk'] ) ] }
            mask0 = { } if 'msk0' not in reply else { 'msk0': [ pack_arrays( reply['msk0'] ) ] }
            if self._stats:
                #statistics for the displayed plane of the image cubea
                statistics = reply['stats']
                return { 'chan': { 'img': [ pack_arrays( reply['img'] ) ],
                                   **mask0,
                                   **mask },
                         'stats': { 'labels': list(statistics.keys( )), 'values': pack_arrays(list(statistics.values( ))) },
                         'hist': reply['hist'],
                         'id': cmd['id'] }
            else:
                return { 'chan': { 'img': [ pack_arrays( reply['img'] ) ],
                                   **mask0,
                                   **mask },
                         'hist': reply['hist'],
                         'id': cmd['id'] }

        elif cmd['action'] == 'spectrum':
            return { 'spectrum': pack_arrays( self.spectrum(cmd['index']) ), 'id': cmd['id'] }
        elif cmd['action'] == 'adjust-colormap':
            if cmd['bounds'] == "reset":
                self.__quant_adjustments = { 'bounds': [ [ ], [ ] ],
                                             'transfer': {'scaling': 'linear'} }
            else:
                ### later a function should be provided for setting the quantization transfer function
                self.__quant_adjustments = { 'bounds': cmd['bounds'], 'transfer': cmd['transfer'] }
            return { 'result': 'OK', 'id': cmd['id'] }

    def add_init_script(self, code, args=None, description=''):
        self.comm.add_init_script( code=code, args=args, description=f"ImagePipe: {description}" )

    def __del__(self):
        cube = getattr( self, '_ImagePipe__cube', None )
        if cube is not None:
            try:
                cube.close( )
            except Exception:
                pass
            self.__cube = None

    def fits_header( self ):
        return ( self.__fits_header, self.__fits_header_str )

    def coorddesc( self ):
        return self.__cube.coorddesc( )

    def statistics_config( self, use_mask=None ):
        '''Configure the behavior of the statistics function.
        use_mask indicates that if a mask is available, the statistics should
        be based upon the portion of the image included in the mask instead of
        the whole channel.'''
        self.__cube.statistics_config( use_mask )

    def statistics( self, index ):
        """Retrieve statistics for one channel from the image cube (see ``ImageCube.statistics``)"""
        return self.__cube.statistics( index )

    def histogram( self, index ):
        """Calculate histogram (Bokeh Quad) extents for update of colormap adjuster (or anything
        else that wants a histogram of image intensities.

        Parameters
        ----------
        index: [ int, int ]
            list containing first the ''stokes'' index and second the ''channel'' index
        """
        if not self._histogram_source:
            return { }
        return self.__cube.histogram( index, len(self._histogram_source.data['top']) )
