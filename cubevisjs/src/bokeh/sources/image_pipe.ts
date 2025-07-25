import { ColumnDataSource } from "@bokehjs/models/sources/column_data_source"
import { DataPipe } from "./data_pipe"
import * as p from "@bokehjs/core/properties"

// Data source where the data is defined column-wise, i.e. each key in the
// the data attribute is a column name, and its value is an array of scalars.
// Each column should be the same length.
export namespace ImagePipe {
    export type Attrs = p.AttrsOf<Props>

    export type Props = DataPipe.Props & {
        dataid: p.Property<string>
        shape: p.Property<[number,number,number,number]>
        fits_header_json: p.Property<string | null>
        _histogram_source: p.Property<ColumnDataSource | null>     // source for histogram updates
        channel: p.Property<( index: [number,number], cb: (msg:{[key: string]: any}) => any, id: string ) => void>
        spectra: p.Property<( index: [number,number,number], cb: (msg:{[key: string]: any}) => any, id: string ) => void>
        refresh: p.Property<( cb: (msg:{[key: string]: any}) => any, id: string, default_index: [number] ) => void>
    }
}

export interface ImagePipe extends ImagePipe.Attrs {}

export class ImagePipe extends DataPipe {
    declare properties: ImagePipe.Props

    static __module__ = "cubevis.bokeh.sources._image_pipe"

    position: {[key: string]: any} = { }
    _wcs: {[key: string]: any} | null = null

    constructor(attrs?: Partial<ImagePipe.Attrs>) {
        super(attrs)
        /**********************************************************
        *** With Bokeh 3.0 properties are no longer initialized ***
        *** before the constructor is called...                 ***
        **********************************************************/
    }

    initialize(): void {
        super.initialize();
        if ( this.fits_header_json ) {
            this._wcs = new casalib.coordtxl.WCSTransform( new casalib.coordtxl.MapKeywordProvider(JSON.parse(this.fits_header_json)) )
        }
    }

    // fetch channel
    //    index: [ stokes index, spectral plane ]
    // RETURNED MESSAGE SHOULD HAVE { id: string, message: any }
    channel( index: [number, number], cb: (msg:{[key: string]: any}) => any, id: string ): void {
        this.position[id] = { index }
        let message = { action: 'channel', index, id }
        super.send( this.dataid, message,
                    (msg:{[key: string]: any}) => {
                        // update histogram (for colormap adjust etc.)
                        if ( this._histogram_source != null && 'hist' in msg &&
                             'top' in msg.hist && 'bottom' in msg.hist &&
                             'left' in msg.hist && 'right' in msg.hist ) {
                            this._histogram_source.data = msg.hist
                        }
                        cb(msg) } )
    }
    // fetch spectra
    //    index: [ RA index, DEC index, stokes index ]
    // RETURNED MESSAGE SHOULD HAVE { id: string, message: any }
    spectrum( index: [number, number, number], cb: (msg:{[key: string]: any}) => any, id: string, squash_queue: boolean | ((msg:{[key: string]: any}) => boolean) = false ) {
        let message = { action: 'spectrum', index, id }
        super.send( this.dataid, message, cb, squash_queue )
    }

    adjust_colormap( bounds: [ number[], number[] ] | string, transfer: {[key: string]: any},
                     cb: (msg:{[key: string]: any}) => any, id: string, squash_queue: boolean | ((msg:{[key: string]: any}) => boolean) = false  ) {
        const message = { action: 'adjust-colormap', bounds, transfer, id }
        super.send( this.dataid, message, cb, squash_queue )
    }

    refresh( cb: (msg:{[key: string]: any}) => any, id: string, default_index=[ 0, 0 ] as number[] ): void {
        let { index } = id in this.position ? this.position[id] : { index: default_index }
        if ( index.length === 2 ) {
            // refreshing channel
            let message = { action: 'channel', index, id }
            super.send( this.dataid, message, cb )

        } else if ( index.length === 3 ) {
            // refreshing spectrum
            let message = { action: 'spectrum', index, id }
            super.send( this.dataid, message, cb )
        }
    }

    wcs( ): {[key: string]: any} | null {
        return this._wcs
    }

    static {
        this.define<ImagePipe.Props>(({Number, Nullable, String, Tuple, Ref }) => ({
            dataid: [ String ],
            shape: [ Tuple(Number,Number,Number,Number) ],
            fits_header_json: [ Nullable(String), null ],
            _histogram_source: [ Nullable(Ref(ColumnDataSource)), null ]
        }));
    }
}
