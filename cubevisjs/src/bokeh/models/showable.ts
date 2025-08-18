import {LayoutDOM, LayoutDOMView} from "@bokehjs/models/layouts/layout_dom"
import type {FullDisplay} from "@bokehjs/models/layouts/layout_dom"
import {UIElement, UIElementView} from "@bokehjs/models/ui/ui_element"
import type * as p from "@bokehjs/core/properties"
import {build_view} from "@bokehjs/core/build_views"

export class ShowableView extends LayoutDOMView {
  declare model: Showable

  ui_view: UIElementView

  get child_models(): UIElement[] {
    return [this.model.ui]
  }

  async lazy_initialize(): Promise<void> {
    await super.lazy_initialize()
    // Build the view for the wrapped UI element
    this.ui_view = await build_view(this.model.ui, {parent: this}) as UIElementView
  }

  _update_layout(): void {
    // Let the parent handle basic layout setup
    super._update_layout()
    
    // Update the wrapped UI's layout if it supports it
    if (this.ui_view != null && 'update_layout' in this.ui_view) {
      (this.ui_view as any).update_layout()
    }
  }

  render(): void {
    super.render()
    
    // Clear any existing content
    this.el.innerHTML = ""
    
    // Render and append the wrapped UI
    if (this.ui_view != null) {
      this.ui_view.render()
      this.el.appendChild(this.ui_view.el)
    }
  }

  after_layout(): void {
    super.after_layout()
    
    // Ensure the wrapped UI gets proper after_layout handling if it supports it
    if (this.ui_view != null && 'after_layout' in this.ui_view) {
      (this.ui_view as any).after_layout()
    }
  }

  // Override sizing to delegate to the wrapped UI
  protected _intrinsic_display(): FullDisplay {
    if (this.ui_view != null) {
      // Try to get display info from the wrapped view
      const ui_display = (this.ui_view as any)._intrinsic_display?.()
      if (ui_display != null) {
        // If the wrapped view returns a FullDisplay, use it
        if ('inner' in ui_display && 'outer' in ui_display) {
          return ui_display as FullDisplay
        }
        // If it returns something else, we still need to return a FullDisplay
        // so fall back to the parent's implementation
      }
    }
    return super._intrinsic_display()
  }

  // Ensure proper cleanup
  remove(): void {
    if (this.ui_view != null) {
      this.ui_view.remove()
    }
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
