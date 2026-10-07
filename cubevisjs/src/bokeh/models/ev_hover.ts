import {LayoutDOM, LayoutDOMView} from "@bokehjs/models/layouts/layout_dom"
import {UIElement} from "@bokehjs/models/ui/ui_element"
import type {StyleSheetLike} from "@bokehjs/core/dom"
import {fieldset} from "@bokehjs/core/dom"
import type * as p from "@bokehjs/core/properties"
import {MouseEnter,MouseLeave} from "@bokehjs/core/bokeh_events"

// EvHover -- a wrapper that reports the pointer entering and leaving.
//
// Wraps one child (a widget, or a column holding a widget and its
// title) and triggers the standard MouseEnter / MouseLeave model events
// when the pointer crosses the wrapper's own element.  Stock Bokeh
// widgets do not emit these; EvTextInput does, for a text input only.
// This gives ANY control the same thing, so that
//
//     wrapper.js_on_event(MouseEnter, CustomJS(...))
//
// works for a Select, a DataTable, a CheckboxGroup, a RadioButtonGroup,
// and for a region made of several of them.  Added 2026-10 so that
// visplot's status-area help covers every control, not only its text
// inputs.
//
// The layout handling is Tip's (models/tip.ts), minus the tooltip: the
// child's element is placed inside a border-less fieldset in the shadow
// root, so the wrapper adds no space of its own.

export class EvHoverView extends LayoutDOMView {
  declare model: EvHover

  fieldset_el: HTMLFieldSetElement

  override stylesheets(): StyleSheetLike[] {
      return [...super.stylesheets(), "*, *:before, *:after { box-sizing: border-box;  } fieldset { border: 0px; margin: 0px; padding: 0px; min-width: 0px; }"]
  }

  override async lazy_initialize(): Promise<void> {
    await super.lazy_initialize()
    await this.build_child_views()
  }

  override connect_signals(): void {
    super.connect_signals()
    const {child} = this.model.properties
    this.on_change(child, () => this.update_children())

    // Listeners on the host element: mouseenter / mouseleave do not
    // bubble, so moving between the child's own parts (title, input,
    // an open dropdown inside the shadow root) does not re-trigger.
    this.el.addEventListener("mouseenter", (event) => {
      this.model.trigger_event( new MouseEnter( event.screenX,
                                                event.screenY,
                                                event.x,
                                                event.y,
                                                { shift: event.shiftKey,
                                                  ctrl: event.ctrlKey,
                                                  alt: event.altKey } ) )
    })

    this.el.addEventListener("mouseleave", (event) => {
      this.model.trigger_event( new MouseLeave( event.screenX,
                                                event.screenY,
                                                event.x,
                                                event.y,
                                                { shift: event.shiftKey,
                                                  ctrl: event.ctrlKey,
                                                  alt: event.altKey } ) )
    })
  }

  get child_models(): UIElement[] {
    return [this.model.child]
  }

  override render(): void {
    super.render()
    const child_els = this.child_views.map((child) => child.el)
    this.fieldset_el = fieldset({}, ...child_els)
    this.shadow_el.appendChild(this.fieldset_el)
  }

  protected override _update_children(): void {
    const child_els = this.child_views.map((child) => child.el)
    this.fieldset_el.append(...child_els)
  }
}

export namespace EvHover {
  export type Attrs = p.AttrsOf<Props>

  export type Props = LayoutDOM.Props & {
    child: p.Property<UIElement>
  }
}

export interface EvHover extends EvHover.Attrs {}

export class EvHover extends LayoutDOM {
  declare properties: EvHover.Props
  declare __view_type__: EvHoverView

  static __module__ = "cubevis.bokeh.models._ev_hover"

  constructor(attrs?: Partial<EvHover.Attrs>) {
    super(attrs)
  }

  static {
    this.prototype.default_view = EvHoverView

    this.define<EvHover.Props>(({Ref}) => ({
      child: [ Ref(UIElement) ],
    }))
  }
}
