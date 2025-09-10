import {LayoutDOM, LayoutDOMView} from "@bokehjs/models/layouts/layout_dom"
import type {FullDisplay} from "@bokehjs/models/layouts/layout_dom"
import {UIElement} from "@bokehjs/models/ui/ui_element"
import type * as p from "@bokehjs/core/properties"

export class ShowableView extends LayoutDOMView {
  declare model: Showable

  // SIMPLIFIED: Implement required child_models but let parent handle everything else
  get child_models(): UIElement[] {
    return this.model.ui != null ? [this.model.ui] : []
  }

  // MINIMAL OVERRIDE: Let the parent class handle all the complex initialization
  // This ensures DataTable and other complex widgets initialize properly
  async lazy_initialize(): Promise<void> {
    // Just call parent - it will handle child view building through child_models
    await super.lazy_initialize()
  }

  // MINIMAL OVERRIDE: Let parent handle signals
  connect_signals(): void {
    super.connect_signals()
    // Parent will automatically handle child model changes through child_models
  }

  // MINIMAL OVERRIDE: Let parent handle layout
  _update_layout(): void {
    super._update_layout()
  }

  // MINIMAL OVERRIDE: Simple rendering that lets parent do the work
  render(): void {
    super.render()
    
    // The parent class should have already rendered our children
    // Just ensure we have proper styling/structure if needed
    if (this.child_views.length === 0 && this.model.ui == null) {
      this.el.innerHTML = `<div style="color: gray; padding: 10px; border: 1px dashed gray;">
        Showable: No UI element set
      </div>`
    }
  }

  // MINIMAL OVERRIDE: Let parent handle after_layout
  after_layout(): void {
    super.after_layout()
  }

  // MINIMAL OVERRIDE: Let parent handle sizing
  protected _intrinsic_display(): FullDisplay {
    return super._intrinsic_display()
  }

  // MINIMAL OVERRIDE: Let parent handle cleanup
  remove(): void {
    super.remove()
  }
}

export namespace Showable {
  export type Attrs = p.AttrsOf<Props>

  export type Props = LayoutDOM.Props & {
    ui: p.Property<UIElement>
  }
}

export interface Showable extends Showable.Attrs {}

export class Showable extends LayoutDOM {
  declare properties: Showable.Props
  declare __view_type__: ShowableView

  static __module__ = "cubevis.bokeh.models._showable"

  constructor(attrs?: Partial<Showable.Attrs>) {
    super(attrs)
  }

  static {
    this.prototype.default_view = ShowableView

    this.define<Showable.Props>(({Ref}) => ({
      ui: [ Ref(UIElement) ],
    }))
  }
}
