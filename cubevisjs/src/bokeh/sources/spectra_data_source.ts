import { ColumnDataSource } from "@bokehjs/models/sources/column_data_source"
import * as p from "@bokehjs/core/properties"
import {uuid4} from "@bokehjs/core/util/string"
import { ImagePipe } from "./image_pipe"

// Data source where the data is defined column-wise, i.e. each key in the
// the data attribute is a column name, and its value is an array of scalars.
// Each column should be the same length.
export namespace SpectraDataSource {
  export type Attrs = p.AttrsOf<Props>

  export type Props = ColumnDataSource.Props & {
      image_source: p.Property<ImagePipe>
  }
}

export interface SpectraDataSource extends SpectraDataSource.Attrs {}

export class SpectraDataSource extends ColumnDataSource {
    properties: SpectraDataSource.Props

    imid: string

    static __module__ = "cubevis.bokeh.sources._spectra_data_source"

    constructor(attrs?: Partial<SpectraDataSource.Attrs>) {
        super(attrs);
        this.imid = uuid4( )
    }
    initialize(): void {
        super.initialize();
    }
    spectra( r: number, d: number, s: number = 0, squash_queue: boolean | ((msg:{[key: string]: any}) => boolean) = false ): void {
        this.image_source.spectrum( [r, d, s], (data: any) => this.data = data.spectrum, this.imid, squash_queue )
    }
    refresh( ): void {
        // supply default index value because the ImagePipe will have no cached
        // index values for this.imid if there have been no updates yet...
        this.image_source.refresh( (data: any) => this.data = data.spectrum, this.imid, [ 0, 0, 0 ] )
    }

    static {
        this.define<SpectraDataSource.Props>(({ Ref }) => ({
            image_source: [ Ref(ImagePipe) ],
        }));
    }
}
