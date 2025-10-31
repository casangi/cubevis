import {HasProps} from "@bokehjs/core/has_props"
import {CoordinateMapper} from "@bokehjs/core/util/bbox"
import {Scale} from "@bokehjs/models/scales/scale"
import {CoordinateUnits} from "@bokehjs/core/enums"
import {PlotView} from "@bokehjs/models/plots/plot"
// @ts-ignore: All imports in import declaration are unused.
import {Span, SpanView} from "@bokehjs/models/annotations/span"
import {View} from "@bokehjs/core/view"
import {Model} from "@bokehjs/model"
import {BokehAppContext} from "../models/bokeh_app_context"

declare global {
    var Bokeh: {
        index: {
            [key: string]: View;
        }
    }
}

export function view( model: Model ): View | null {

    function find_view(v: View): View | null {
        if ( v.model === model ) {
            return v
        }
        for (const child of v.children( )) {
            const result = find_view(child)
            if (result) return result
        }
        return null
    }

    const document = model.document;
    if ( ! document ) {        //model unattached to document
        return null
    }

    // @ts-ignore: views_manager is internal to Document
    const view_manager = document.views_manager
    if ( ! view_manager ) {
        return null
    }

    const root_views = view_manager.roots
    for ( const v of root_views ) {
        const found = find_view(v)
        if ( found ) {
            return found
        }
    }

    return null
}

export function span_coords( span: SpanView ) {
    // find span screen coordinates
    function compute( value: number | null, units: CoordinateUnits, scale: Scale, view: CoordinateMapper, canvas: CoordinateMapper ): number {
        if ( value != null )
            switch (units) {
                case "canvas": return canvas.compute(value)
                case "screen": return view.compute(value)
                case "data":   return scale.compute(value)
            }
        return NaN
    }
    const {frame, canvas} = span.plot_view
    const {x_scale, y_scale} = span.coordinates
    let height, sleft, stop, width, orientation = span.model.dimension
    if (span.model.dimension == "width") {
        stop = compute(span.model.location, span.model.location_units, y_scale, frame.bbox.yview, canvas.bbox.y_screen)
        sleft = frame.bbox.left
        width = frame.bbox.width
        height = span.model.line_width
    } else {
        stop = frame.bbox.top
        sleft = compute(span.model.location, span.model.location_units, x_scale, frame.bbox.xview, canvas.bbox.y_screen)
        width = span.model.line_width
        height = frame.bbox.height
    }
    return { stop, sleft, width, height, orientation }
}


//****************************************************************
//*** scalar coordinate functions                              ***
//****************************************************************
export function px_from_sx( view: PlotView, x: number ) {
    // map the screen x coordinates supplied as sx in cb_data for mouse
    // movement to the screen coordinate used within a plot
    return view.frame.bbox.x_view.invert(x)
}
export function py_from_sy( view: PlotView, y: number ) {
    // map the screen y coordinates supplied as sy in cb_data for mouse
    // movement to the screen coordinate used within a plot
    return view.frame.bbox.y_view.invert(y)
}

export function dx_from_px( view: PlotView, sx: number ) {
    // map the (plot) screen x coordinate supplied as sx in cb_data for mouse
    // movement to the data coordinate. sx is screen coordinates WITHIN the plot
    const fig_sx = view.frame.bbox.x_view.compute(sx)
    return view.frame.x_scale.invert(fig_sx)
}

export function dy_from_py( view: PlotView, sy: number ) {
    // map the (plot) screen y coordinate supplied as sy in cb_data for mouse
    // movement to the data coordinate. sy is screen coordinates WITHIN the plot
    const fig_sy = view.frame.bbox.y_view.compute(sy)
    return view.frame.y_scale.invert(fig_sy)
}

export function sx_from_dx( view: PlotView, dx: number ) {
    // map the data x coordinate supplied as x in cb_data for mouse
    // movement to the screen coordinate
    return view.frame.x_scale.compute(dx)
}

export function sy_from_dy( view: PlotView, dy: number ) {
    // map the data y coordinate supplied as y in cb_data for mouse
    // movement to the data coordinate used within the plot
    return view.frame.y_scale.compute(dy)
}

//****************************************************************
//*** vector coordinate functions                              ***
//****************************************************************
export function v_px_from_sx( view: PlotView, x: [ number ]) {
    // map the screen x coordinates supplied as sx in cb_data for mouse
    // movement to the screen coordinate used within a plot
    return view.frame.bbox.x_view.v_invert(x)
}
export function v_py_from_sy( view: PlotView, y: [ number ] ) {
    // map the screen y coordinates supplied as sy in cb_data for mouse
    // movement to the screen coordinate used within a plot
    return view.frame.bbox.y_view.v_invert(y)
}

export function v_dx_from_px( view: PlotView, sx: [ number ] ) {
    // map the (plot) screen x coordinate supplied as sx in cb_data for mouse
    // movement to the data coordinate
    const fig_sx = view.frame.bbox.x_view.v_compute(sx)
    return view.frame.x_scale.v_invert(fig_sx)
}

export function v_dy_from_py( view: PlotView, sy: [ number ] ) {
    // map the (plot) screen y coordinate supplied as sy in cb_data for mouse
    // movement to the data coordinate
    const fig_sy = view.frame.bbox.y_view.v_compute(sy)
    return view.frame.y_scale.v_invert(fig_sy)
}

export function v_sx_from_dx( view: PlotView, dx: [ number ] ) {
    // map the data x coordinate supplied as x in cb_data for mouse
    // movement to the screen coordinate
    return view.frame.x_scale.v_compute(dx)
}

export function v_sy_from_dy( view: PlotView, dy: [ number ] ) {
    // map the data y coordinate supplied as y in cb_data for mouse
    // movement to the data coordinate used within the plot
    return view.frame.y_scale.v_compute(dy)
}

// The BokehAppContext is used to provide session storage and information
// for a Bokeh app. It can be used in two ways:
//
//   (1) as a mix in:      column( myBokehAppContext, widget01, widget02 )
//       in this mode, the app state is available, but the app must manage
//       must manage finding its app context based on the `app_id` that is
//       available from the BokehAppContext in both Python and JavaScript
//
//   (2) as a parent:      BokehAppContext( column( widget01, widget02 ),
//                                          app_state={ 'key': "value" } )
//       In this mode, the context for a specific application can be found
//       by searching up the widget hierarchy to find a BokehAppContext
//       above which will supply the `app_id` for this particular app
//
// This function can ONLY BE USED FOR THE SECOND CASE. Note that these
// Models do not seem to necessarily be tied into the hierarchy:
//
//    * DataSources
//

//*********************************************************************************
//*** Use a type guard like this with filter to satisfy the TypeScript compiler ***
//*********************************************************************************
//function isNotNullish<T>(value: T | null | undefined): value is T {
//    return value !== null && value !== undefined;
//}
function find_model(
    model: Model,
    predicate: (m: Model) => boolean,
    visited: Set<Model> = new Set()
): Model | undefined {
    if (visited.has(model)) return;
    visited.add(model);

    if (predicate(model)) return model;

    // 'child' is used cubevis' Tip but also some Bokeh containers
    // 'ui' is used by cubevis' Showable and BokehAppContext
    const potentialChildrenProps = ['children', 'items', 'panes', 'tabs', 'child', 'ui'];

    for (const propName of potentialChildrenProps) {
        const children = (model as any)[propName];

        if (children) {
            // Unify handling for single children and arrays of children
            const childrenArray = Array.isArray(children) ? children : [children];

            for (const child of childrenArray) {
                // Ensure the child is a Model instance before recursing
                if (child instanceof Model && !visited.has(child)) {
                    const result = find_model(child, predicate, visited);
                    if (result) return result;
                }
            }
        }
    }

    return;
}

export function context(model: Model): BokehAppContext | undefined {
    const roots = model?.document?.all_roots
    if ( roots ) {
      const cl: BokehAppContext[] = roots.flatMap(
          (value: HasProps) => {
              const model = value as Model;
              return ( model && model.type === "cubevis.bokeh.models._bokeh_app_context.BokehAppContext" )
                    ? [model as BokehAppContext]  : []
          } )
      const result = cl.find( (root) => { return Boolean(find_model(root as Model, (candidate: Model) => candidate.id === model.id ) ) } )
      return result
    }
    return;
}

export function appState(model: Model): object | undefined {
    const ctx = context(model)
    if ( ctx ) {
        // @ts-ignore: defined on startup
        const apps_state = window?.cubevisAppSession?.applications
        return apps_state[ctx.app_id] ?? undefined
    }
    return
}
