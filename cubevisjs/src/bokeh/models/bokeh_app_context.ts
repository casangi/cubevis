import {LayoutDOM, LayoutDOMView} from "@bokehjs/models/layouts/layout_dom"
import {UIElement} from "@bokehjs/models/ui/ui_element"
import {CustomJS} from "@bokehjs/models/callbacks/index"
import type * as p from "@bokehjs/core/properties"
import {object_id} from "../util/find"

import { CommMgr } from "../transport/comm_mgr"

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
        session.applications[this.model.app_id].state = {
          ...this.model.app_state,
          app_id: this.model.app_id
        }
      }
    })
  }
}

export namespace BokehAppContext {
  export type Attrs = p.AttrsOf<Props>
  export type Props = LayoutDOM.Props & {
    ui: p.Property<UIElement | null>
    app_id: p.Property<string>
    comm_mgr: p.Property<CommMgr | null>
    backend_id: p.Property<string>
    frontend_id: p.Property<string | null>
    app_state: p.Property<any>
    init_scripts: p.Property<[CustomJS, string, string][]>
  }
}

export interface BokehAppContext extends BokehAppContext.Attrs {}

export class BokehAppContext extends LayoutDOM {
  declare properties: BokehAppContext.Props
  declare __view_type__: BokehAppContextView

  static __module__ = "cubevis.bokeh.models._bokeh_app_context"

  constructor(attrs?: Partial<BokehAppContext.Attrs>) {
    console.log("BokehAppContext.constructor <A>")
    super(attrs)
  }

  override initialize(): void {
    super.initialize()
    
    try {
      console.log("BokehAppContext.initialize <B>",this.init_scripts,this.app_state)
    } catch (error) {
      console.error("An error occurred:", error);
    }

    // frontend_id must be set by the frontend not preset by the backend
    const frontend_identifier = object_id(this)
    if ( this.frontend_id !== null && this.frontend_id !== frontend_identifier ) {
      console.warn(
        `BokehAppContext [${this.app_id}]: 'frontend_id' was incorrectly initialized as '${this.frontend_id}', ` +
          `instead of '${frontend_identifier}', resetting it.`
      )
    }
    if ( this.frontend_id !== frontend_identifier )
      this.frontend_id = frontend_identifier

    // Initialize session-level data structure if it doesn't exist
    if (!(window as any).cubevisAppSession) {
      (window as any).cubevisAppSession = {
        sessionId: this.backend_id,
        applications: {}
      }
      console.log(`Initialized Bokeh session: ${this.backend_id}:${this.frontend_id}`)
    }
    
    const session = (window as any).cubevisAppSession
    
    // Register this application in the session
    if (!session.applications[this.app_id]) {
      session.applications[this.app_id] = {
        appId: this.app_id,
        state: { ...this.app_state, app_id: this.app_id },
        createdAt: new Date().toISOString()
      }
      console.log(`Registered application: ${this.app_id}`)
    }
    //
    // Run any initialization script
    //
    const _execute = () => {
        console.group( "BokehAppContext init script execution" )
        this.init_scripts.forEach(
            ([script, id, description], i) => {
                // Pass the current loop index 'i' into the cb_data object
                if ( description === null || description === undefined || description.trim().length === 0 )
                    console.log(id)
                else
                    console.log(description)
                script.execute( this, { index: i, id, description } )
            } )
        console.groupEnd( )
    }
    _execute( )
  }

  static {
    this.prototype.default_view = BokehAppContextView

    this.define<BokehAppContext.Props>(({Ref, Tuple, Nullable, Array, Dict, String, Unknown}) => ({
      ui: [ Nullable(Ref(UIElement)), null ],
      app_id: [String, ""],
      comm_mgr: [ Nullable(Ref(CommMgr)), null ],
      backend_id: [String, ""],
      frontend_id: [Nullable(String), null],
      app_state: [ Dict(Unknown), {} ],
      init_scripts:   [ Array(Tuple(Ref(CustomJS), String, String)), [] ],
    }))
  }
}
