import {LayoutDOM, LayoutDOMView} from "@bokehjs/models/layouts/layout_dom"
import {UIElement} from "@bokehjs/models/ui/ui_element"
import type * as p from "@bokehjs/core/properties"
import {object_id} from "../util/find"

export class BokehAppContextView extends LayoutDOMView {
  declare model: BokehAppContext

  // Tell Bokeh about our child
  override get child_models(): UIElement[] {
    return this.model.ui != null ? [this.model.ui] : []
  }

  override initialize(): void {
    super.initialize()

    // Make the wrapper transparent/minimal
    if (this.el) {
      this.el.style.padding = '0'
      this.el.style.margin = '0'
      this.el.style.border = 'none'
      this.el.style.background = 'transparent'
      // Let content determine size
      this.el.style.display = 'contents' // CSS magic for transparent wrapper
    }

    // Set up property change listener
    this.connect(this.model.properties.app_state.change, () => {
      const session = (window as any).cubevisAppSession
      if (session?.applications[this.model.app_id]) {
        session.applications[this.model.app_id].state = this.model.app_state
      }
    })
  }
}

export namespace BokehAppContext {
  export type Attrs = p.AttrsOf<Props>
  export type Props = LayoutDOM.Props & {
    ui: p.Property<UIElement | null>
    app_id: p.Property<string>
    backend_id: p.Property<string>
    frontend_id: p.Property<string | null>
    app_state: p.Property<any>
  }
}

export interface BokehAppContext extends BokehAppContext.Attrs {}

export class BokehAppContext extends LayoutDOM {
  declare properties: BokehAppContext.Props
  declare __view_type__: BokehAppContextView

  static __module__ = "cubevis.bokeh.models._bokeh_app_context"

  constructor(attrs?: Partial<BokehAppContext.Attrs>) {
    super(attrs)
  }

  override initialize(): void {
    super.initialize()

    // frontend_id must be set by the frontend not preset by the backend
    if (this.frontend_id !== null) {
      console.warn(
        `BokehAppContext [${this.app_id}]: 'frontend_id' was initialized as '${this.frontend_id}', ` +
        `but it must be NULL upon initialization. Forcing to NULL.`
      )
      this.frontend_id = null
    }

    // Initialize session-level data structure if it doesn't exist
    if (!(window as any).cubevisAppSession) {
      (window as any).cubevisAppSession = {
        sessionId: this.backend_id,
        applications: {}
      }

      this.frontend_id = object_id(this)
      console.log(`Initialized Bokeh session: ${this.backend_id}:${this.frontend_id}`)
    }
    
    const session = (window as any).cubevisAppSession
    
    // Register this application in the session
    if (!session.applications[this.app_id]) {
      session.applications[this.app_id] = {
        appId: this.app_id,
        state: this.app_state,
        createdAt: new Date().toISOString()
      }
      console.log(`Registered application: ${this.app_id}`)
    }
  }

  static {
    this.prototype.default_view = BokehAppContextView

    this.define<BokehAppContext.Props>(({Ref, Nullable, Dict, String, Unknown}) => ({
      ui: [ Nullable(Ref(UIElement)), null ],
      app_id: [String, ""],
      backend_id: [String, ""],
      frontend_id: [ Nullable(String), null ],
      app_state: [ Dict(Unknown), {} ],
    }))
  }
}
