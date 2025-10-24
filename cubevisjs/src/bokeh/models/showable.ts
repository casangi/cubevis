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

  initialize() {
    super.initialize();

    // Add the attribute to suppress JupyterLab keyboard shortcuts
    // This is the standard way to prevent interference with embedded widgets
    this.el.setAttribute('data-lm-suppress-shortcuts', 'true');

    // Before finding the above attribute, typing a number in one of the text
    // entry widgets of interactive clean would cause that number of hash signs
    // to be inserted into the beginning of the cell that caused the interactive
    // clean GUI to be rendered (so entering '3' would cause '###' to be inserted).
    // It seems as though this was because Jupyter Lab has hotkeys, and it was
    // treating this cell as markdown. And '3' caused a "sub-sub-section" to be
    // created. This selective keydown handler prevented Jupyter shortcuts while
    // allowing other keys to reach child widgets. The above attribute seems to
    // have been introduced to fix this problem. This code does not SEEM to be
    // needed for "Clasic Notebooks" but it is left here for reference.
//  const rootEl = this.el;
//  rootEl.addEventListener('keydown', (e) => {
//    const conflictingKeys = ['1', '2', '3', '4', '5', '6'];
//
//    // Check if the event's target is within the Showable
//    if (rootEl.contains(e.target as Node)) {
//      if (e.key) { // Check if the key property is present
//        // Only block if the key is in our list of conflicting keys
//        if (conflictingKeys.includes(e.key)) {
//          e.stopPropagation();
//          console.log('🛑 BLOCKED keydown at view root:', e.key);
//        } else {
//          console.log('✅ ALLOWED keydown to propagate:', e.key);
//        }
//      }
//    }
//  }, true); // Use the capturing phase to ensure this runs first
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
