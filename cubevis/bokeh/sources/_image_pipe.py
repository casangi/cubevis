########################################################################
#
# Copyright (C) 2021,2022,2023,2024
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
of image cube channels in response to user input.'''

import os
import sys
import json
import asyncio
from uuid import uuid4

from . import DataPipe
from bokeh.util.compiler import TypeScript
from bokeh.core.properties import Tuple, String, Int, Instance, Nullable
from bokeh.models.callbacks import Callback
from bokeh.plotting import ColumnDataSource
from ..state import casalib_url, cubevisjs_url

import numpy as np
try:
    import casatools as ct
    from casatools import regionmanager
    from casatools import image as imagetool
except:
    ct = None
    from cubevis.utils import warn_import
    warn_import('casatools')

from ...utils import pack_arrays, partition, resource_manager, strip_arrays

class ImagePipe(DataPipe):
    """The `ImagePipe` allows for updates to Bokeh plots from a CASA or CNGI

    image. This is done using a `websocket`. A `ImagePipe` is created with
    the path to the image, and then it is used as the input to an
    `ImageDataSource` or a `SpectraDataSource`. This allows a single CASA
    or CNGI imge to be opened once and shared among multiple Bokeh plots,
    for example ploting an image channel and a plot of a spectrum from the
    image cube.

    Attributes
    ----------
    address: tuple of string and int
        the string is the IP address for the network that should be used and the
        integer is the port number, see ``cubevis.utils.find_ws_address``
    init_script: JavaScript
        this javascript is run when this DataPipe object is initialized. init_script
        is used to run caller JavaScript which needs to be run at initialization time.
        This is optional and does not need to be set.
    """
    __im_path = None
    __im = None
    __chan_shape = None

    shape = Tuple( Int, Int, Int, Int, help="shape: [ RA, DEC, Stokes, Spectral ]" )
    dataid = String( )
    fits_header_json = Nullable( String, help="""JSON representation of image FITS header for world coordinate labeling""" )
    _histogram_source = Nullable(Instance(ColumnDataSource), help='''
    data source for (raw) image channel histogram of intensities used with a "figure.quad(...)"
    ''')

    __javascript__ = [ casalib_url( ), cubevisjs_url( ) ]

    def __open_image( self, image ):
        if self.__img is not None:
            self.__img.close( )
            self.__stokes_labels = None
        self.__img = imagetool( )
        self.__rgn = regionmanager( )
        try:
            self.__img.open(image)
            self.__image_path = image
        except Exception as ex:
            self.__img = None
            self.__image_path = None
            raise RuntimeError(f'could not open image: {image}') from ex
        imshape = self.__img.shape( )
        if self.__msk is not None and all(self.__msk.shape( ) != imshape):
            raise RuntimeError(f'mismatch between image shape ({imshape}) and mask shape ({self.__msk.shape( )})')
        if self.__chan_shape is None: self.__chan_shape = list(imshape[0:2])

    def __open_mask( self, mask ):
        if mask is None:
            self.__mask_path = None
            return
        if self.__msk is not None:
            self.__msk.close( )
        self.__msk = imagetool( )
        try:
            self.__msk.open(mask)
            self.__mask_path = mask
        except Exception as ex:
            self.__msk = None
            self.__mask_path = None
            raise RuntimeError(f'could not open mask: {mask}') from ex
        mskshape = self.__msk.shape( )
        if self.__img is not None and all(self.__img.shape( ) != mskshape):
            raise RuntimeError(f'mismatch between image shape ({self.__img.shape( )}) and mask shape ({mskshape})')
        if self.__chan_shape is None: self.__chan_shape = list(mskshape[0:2])

    def __close_mask( self ):
        if self.__msk is not None:
            self.__msk.close( )
            self.__msk = None

    def pixel_value( self, chan, index ):
        channel = self.__get_chan(chan)
        index[0] = min( index[0], channel.shape[0] - 1 )
        index[1] = min( index[1], channel.shape[1] - 1 )
        index[0] = max( index[0], 0 )
        index[1] = max( index[1], 0 )
        return np.squeeze(channel[index[0],index[1]])

    def stokes_labels( self ):
        """Returns stokes plane labels"""
        if self.__stokes_labels is None:
            self.__stokes_labels = self.__img.coordsys( ).stokes( )
        return self.__stokes_labels

    def __get_chan( self, index ):
        def newest_ctime( path ):
            files = os.listdir(path)
            paths = [os.path.join(path, basename) for basename in files]
            return max( map( os.path.getctime, paths ) )

        image_ctime = newest_ctime( self.__image_path )
        if image_ctime > self.__cached_chan_ctime or \
           self.__cached_chan_index[0] != index[0] or \
           self.__cached_chan_index[1] != index[1] or \
           self.__cached_chan is None :
            if self.__img is None:
                raise RuntimeError('no image is available')
            ###
            ### ensure that the channel index is within cube shape
            ###
            index = list(index)     # index is potentially a python tuple
            index[0] = min( index[0], self.__chan_shape[0] - 1 )
            index[1] = min( index[1], self.__chan_shape[1] - 1 )
            index[0] = max( index[0], 0 )
            index[1] = max( index[1], 0 )
            self.__cached_chan_index = index
            self.__cached_chan_ctime = image_ctime
            self.__cached_chan = self.__img.getchunk( blc=[0,0] + index,
                                                      trc=self.__chan_shape + index )
        return self.__cached_chan

    ### ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ----
    ### the element type of image pixels retrieved from the CASA image are float64, but it
    ### seems like 256 is the greatest number of colors in the colormaps currrently used
    ### for pseudo color within interactive clean...
    ### ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ---- ----
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
        def quantize( nptype, image_plane ):
            ### --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---
            ### Note:
            ###    (1) the histogram sent to GUI is ALWAYS be histogram based on the raw image (THIS IS HANDLED ABOVE)
            ###    (2) the scaled portion of the matrix should be the non-cropped portion
            ###    (3) the lower cropped portion should be set to the min scaled value
            ###    (4) the upper cropped portion should be set to the max scaled value
            ###    (5) a histogram should be created with the resulting (completely filled) array
            ###    (6) then this histogram should be used with the (completely filled) array with numpy.digitize( ) to create
            ###        the uint8 array
            ### --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---
            exclude_below = None
            exclude_above = None
            included = None

            ### --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---
            ### Sort out the relationship between channel min/max and user specified min/max
            ### --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---
            amin = image_plane.min( )           ## array min
            amax = image_plane.max( )           ## array max

            ### --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---
            ### Extract user bounds (use array bounds as fallback)
            ### --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---
            umin = amin if len(self.__quant_adjustments['bounds'][0]) == 0 else self.__quant_adjustments['bounds'][0][0]
            umax = amax if len(self.__quant_adjustments['bounds'][1]) == 0 else self.__quant_adjustments['bounds'][1][0]

            ### --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---
            ### Handle cropping (when user bounds are narrower than data)
            ### --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---
            if umin > amin:
                exclude_below = image_plane < umin
            if umax < amax:
                exclude_above = image_plane > umax

            ### --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---
            ### Set up access masks
            ### --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---
            if exclude_below is not None and exclude_above is not None:
                included = np.logical_not( np.logical_or( exclude_below, exclude_above ) )
            elif exclude_below is not None:
                included = np.logical_not( exclude_below )
            elif exclude_above is not None:
                included = np.logical_not( exclude_above )

            ### --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---
            ### Apply the scaling function to the included pixels
            ### --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---
            selected_scaling = self.__quant_adjustments['transfer']['scaling']
            if selected_scaling != 'linear':
                if selected_scaling not in self.__quant_scaling:
                    print( f'''error: ${selected_scaling} is not a known scaling...''', file=sys.stderr )
                    result = image_plane
                else:
                    normalize = 0 if umin > 0 else -umin
                    result = np.ma.zeros(image_plane.shape,image_plane.dtype)

                    if included is not None:

                        result[included] = self.__quant_scaling[selected_scaling](
                            image_plane[included] + normalize,
                            **self.__quant_adjustments['transfer']['args']
                        )

                        # Set excluded regions to min/max of included values
                        if exclude_below is not None:
                            result[exclude_below] = result[included].min( )
                        if exclude_above is not None:
                            result[exclude_above] = result[included].max( )
                    else:
                        result = self.__quant_scaling[selected_scaling](
                            image_plane + normalize,
                            **self.__quant_adjustments['transfer']['args']
                        )
            else:

                result = image_plane

            ### --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---
            ### Histogram of the scaled
            ### --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---
            edges = np.histogram_bin_edges( result, bins=254, range=( umin, umax ) )

            return np.digitize( result, edges, right=True ).astype(nptype)

        if self.__img is None:
            raise RuntimeError('no image is available')
        if np.issubdtype( pixel_type, np.integer ):
            return quantize( pixel_type,
                             np.squeeze( self.__get_chan(index) ) ).transpose( )
        else:
            return np.squeeze( self.__get_chan(index) ).astype(pixel_type).transpose( )

    def have_mask0( self ):
        """Check to see if the synthesis imaging 'mask0' mask exists

        Returns
        -------
        bool:
            ''True'' if the cube contains an internal ''mask0'' mask otherwise ''False''
        """
        if self.__img is None:
            raise RuntimeError('no image is available')
        return 'mask0' in self.__img.maskhandler('get')


    def mask0( self, index ):
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
        if self.__img is None:
            raise RuntimeError('no image is available')
        if self.__mask0_cache is None and self.have_mask0( ):
            self.__mask0_cache = self.__img.getregion(getmask=True)
        if self.__mask0_cache is not None:
            return self.__mask0_cache[:,:,index[0],index[1]]
        return None

    def have_mask( self ):
        """Check to see if a mask exists.

        Returns
        -------
        bool:
            ''True'' if a mask cube is available otherwise ''False''
        """
        return self.__msk is not None

    def mask( self, index, modify=False ):
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
        if self.__msk is None:
            raise RuntimeError(f'cannot retrieve mask at {repr(index)} because no mask cube exists')
        return ( np.squeeze( self.__msk.getchunk( blc=[0,0] + index,
                                                trc=self.__chan_shape + index) ).astype(np.bool_).transpose( )
                 if modify == False else
                 np.squeeze( self.__msk.getchunk( blc=[0,0] + index,
                                                trc=self.__chan_shape + index) ) )

    def mask_value( self, chan, index ):
        if self.__msk is None:
            raise RuntimeError(f'cannot retrieve mask at {repr(index)} because no mask cube exists')
        try:
            pv = self.__msk.pixelvalue( index + chan )
            return int(pv['value']['value'])
        except:
            pass
        return -1

    def set_mask_name( self, new_mask_path ):
        self.__close_mask( )
        self.__open_mask( new_mask_path )

    def put_mask( self, index, mask ):
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
        if self.__msk is None:
            raise RuntimeError(f'cannot replace mask at {repr(index)} because no mask cube exists')
        if mask.dtype == bool:
            ### cannot put bools with putchunk
            self.__msk.putchunk( blc=[0,0] + index, pixels=mask.astype(np.uint8) )
        else:
            self.__msk.putchunk( blc=[0,0] + index, pixels=mask )

    def spectrum( self, index, mask=False ):
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
        index = list(map( lambda i: 0 if i is None else i, index ))
        if index[0] >= self.shape[0]:
            index[0] = self.shape[0] - 1
        if index[1] >= self.shape[1]:
            index[1] = self.shape[1] - 1
        if self.__img is None:
            raise RuntimeError('no image is available')
        result_mask = np.squeeze( self.__msk.getchunk( blc=index + [0],
                                                       trc=index + [self.shape[-1]] ) ) if self.__msk and mask else None
        result = np.squeeze( self.__img.getchunk( blc=index + [0],
                                                 trc=index + [self.shape[-1]] ) )
        ### should return spectral freq etc.
        ### here for X rather than just the index
        try:
            if mask:
                return { 'chan': list(range(len(result))), 'pixel': list(result) }, None if result_mask is None else list(result_mask.astype(bool))
            else:
                return { 'chan': list(range(len(result))), 'pixel': list(result) }
        except Exception as e:
            ## In this case, result is not iterable (e.g.) only one channel in the cube.
            ## A zero length numpy ndarray has no shape and looks like a float but it is
            ## an ndarray.
            if mask:
                return { 'chan': [0], 'pixel': [float(result)] }, None if result_mask is None else [ bool(result_mask) ]
            else:
                return { 'chan': [0], 'pixel': [float(result)] }

    def histogram_source( self, data ):
        if not self._histogram_source:
            self._histogram_source = ColumnDataSource( data=data )
        return self._histogram_source

    async def _image_message_handler( self, cmd ):
        if cmd['action'] == 'channel':
            chan = self.channel(cmd['index'],np.uint8)
            mask = { } if self.__msk is None else { 'msk': [ pack_arrays( self.mask(cmd['index']) ) ] }
            _mask0 = self.mask0(cmd['index'])
            mask0 = { } if _mask0 is None else { 'msk0': [ pack_arrays(_mask0) ] }
            histogram = self.histogram( cmd['index'] ) if self._histogram_source else { }
            if self._stats:
                #statistics for the displayed plane of the image cubea
                statistics = self.statistics( cmd['index'] )
                return { 'chan': { 'img': [ pack_arrays(chan) ],
                                   **mask0,
                                   **mask },
                         'stats': { 'labels': list(statistics.keys( )), 'values': pack_arrays(list(statistics.values( ))) },
                         'hist': histogram,
                         'id': cmd['id'] }
            else:
                return { 'chan': { 'img': [ pack_arrays(chan) ],
                                   **mask0,
                                   **mask },
                         'hist': histogram,
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
                ### ensure that the cached channel is not used...
                self.__cached_chan = None
            return { 'result': 'OK', 'id': cmd['id'] }

    def __init__( self, image, *args, mask=None, stats=False, **kwargs ):
        super( ).__init__( *args, **kwargs, )

        self.dataid = str(uuid4( ))

        if ct is None:
            raise RuntimeError('cannot open an image because casatools is not available')

        self.__img = None
        self.__msk = None
        self.__fits_header = None
        self.__fits_header_str = ''
        resource_manager( ).reg_at_exit( self, '__del__' )
        self._stats = stats
        self.__open_image( image )
        self.__open_mask( mask )
        self.__mask0_cache = None
        self.shape = list(self.__img.shape( ))
        if not self.fits_header_json:
            self.__fits_header = self.__img.fitsheader(exclude="HISTORY")
            self.__fits_header_str = self.__img.fitsheader(retstr=True,exclude="HISTORY")
            if self.__fits_header:
                self.fits_header_json = json.dumps(strip_arrays(self.__fits_header))
        self.__session = None
        self.__stokes_labels = None
        self.__mask_statistics = False

        ###
        ### the last channel retrieved is kept around for pixel retrieval
        ###
        self.__cached_chan = None
        self.__cached_chan_index = None
        self.__cached_chan_ctime = 0

        ###
        ### quantization controls to affect how pseudo colors are displayed
        ###
        self.__quant_adjustments = { 'bounds': [ [ ], [ ] ],
                                     'transfer': {'scaling': 'linear'} }
        self.__quant_scaling = { 'log':    lambda chan,alpha: np.ma.log(alpha * chan + 1.0) / np.ma.log(alpha + 1.0),
                                 'sqrt':   lambda chan:       np.ma.sqrt(chan),
                                 'square': lambda chan:       np.square(chan),
                                 'gamma':  lambda chan,gamma: np.ma.power(chan,gamma),
                                 'power':  lambda chan,alpha: (np.ma.power(alpha,chan) - 1.0) / alpha }

        super( ).register( self.dataid, self._image_message_handler )

    def __del__(self):
        if self.__rgn:
            self.__rgn.done( )
        if self.__img != None:
            self.__img.close()
            self.__img.done()
            self.__img = None
            self.__stokes_labels = None

    def fits_header( self ):
        return ( self.__fits_header, self.__fits_header_str )

    def coorddesc( self ):
        ia = imagetool( )
        ia.open(self.__image_path)
        csys = ia.coordsys( )
        ia.close( )
        return { 'csys': csys, 'shape': tuple(self.shape) }

    def statistics_config( self, use_mask=None ):
        '''Configure the behavior of the statistics function.
        use_mask indicates that if a mask is available, the statistics should
        be based upon the portion of the image included in the mask instead of
        the whole channel.'''
        if self.__mask_path and use_mask is not None:
            self.__mask_statistics = bool(use_mask)

    def statistics( self, index ):
        """Retrieve statistics for one channel from the image cube. The `index`
        should be a two element list of integers. The first integer is the
        ''stokes'' axis in the image cube. The second integer is the ''channel''
        axis in the image cube.

        Parameters
        ----------
        index: [ int, int ]
            list containing first the ''stokes'' index and second the ''channel'' index
        """
        def singleton( potential_nonlist ):
            # convert a list of a single element to the element
            return potential_nonlist if len(potential_nonlist) != 1 else potential_nonlist[0]
        def sort_result( unsorted_dictionary ):
            part = partition( lambda s: (s.startswith('trc') or s.startswith('blc')), sorted(unsorted_dictionary.keys( )) )
            return { k: unsorted_dictionary[k] for k in part[1] + part[0] }

        reg = self.__rgn.box( [0,0] + index, self.__chan_shape + index )
        ###
        ### This seems like it should work:
        ###
        #      rawstats = self.__img.statistics( region=reg )
        ###
        ### but it does not so we have to create a one-use image tool (see CAS-13625)
        ###
        ia = imagetool( )
        ia.open(self.__image_path)
        if self.__mask_statistics:
            ### mask is an LEL expression and quotes prevents a name containing
            ### numbers from being interpreted as an expression
            rawstats = ia.statistics( region=reg, mask=f'''"{self.__mask_path}"''' )
        else:
            rawstats = ia.statistics( region=reg )
        ia.close( )
        return sort_result( { k: singleton([ x.item( ) for x in v ]) if isinstance(v,np.ndarray) else v for k,v in rawstats.items( ) } )

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

        chan = self.__get_chan(index)
        bins = np.linspace( chan.min( ), chan.max( ), len(self._histogram_source.data['top'])+1 )
        hist, edges = np.histogram( chan, density=False, bins=bins )
        return dict( left=list(edges[:-1]), right=list(edges[1:]), top=list(hist), bottom=[0]*len(hist) )
