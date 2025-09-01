import {View} from "@bokehjs/core/view"
import {Model} from "@bokehjs/model"
import type * as p from "@bokehjs/core/properties"

export class SharedDictView extends View {
  declare model: SharedDict

  override initialize(): void {
    super.initialize()
  }

  override connect_signals(): void {
    super.connect_signals()
    const {values} = this.model.properties
    this.on_change(values, () => this.values_changed())
  }

  private values_changed(): void {
    // Handle values property changes if needed
    // This method is called when the values dictionary is updated
  }
}

export namespace SharedDict {
  export type Attrs = p.AttrsOf<Props>

  export type Props = Model.Props & {
    values: p.Property<{[key: string]: any}>
  }
}

export interface SharedDict extends SharedDict.Attrs {}

export class SharedDict extends Model {
  declare properties: SharedDict.Props
  declare __view_type__: SharedDictView

  static __module__ = "cubevis.bokeh.models._shared_dict"

  constructor(attrs?: Partial<SharedDict.Attrs>) {
    super(attrs)
  }

  static {
    this.prototype.default_view = SharedDictView

    this.define<SharedDict.Props>(({Dict, Unknown}) => ({
        values: [ Dict(Unknown), {} ]
    }))
  }
}
