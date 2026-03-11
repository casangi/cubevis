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
import {Showable} from "../models/showable"

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

function find_parent<T extends Model>(
    type_string: string
): (model: Model) => T | undefined {
    return (model: Model): T | undefined => {
        // with Bokeh 3.6 there is a roots( ) function...
        // with Bokeh 3.8 there is a all_roots property...
        const roots = model?.document?.all_roots ?? model?.document?.roots();
        if (!roots) return undefined;

        const potentialChildrenProps = ['children', 'items', 'panes', 'tabs', 'child', 'ui'];

        // Recursively get the first matching type in the tree (or empty array if none)
        const findParent = (node: HasProps): T[] => {
            const nodeModel = node as Model;

            // If current node matches the type string, return it (stop searching deeper)
            if (nodeModel?.type === type_string) {
                return [nodeModel as T];
            } else {
                // Otherwise, search children
                return potentialChildrenProps.flatMap(prop => {
                    const children = (node as any)[prop];
                    if (!children) return [];

                    // Handle both single child and array of children
                    const childArray = Array.isArray(children) ? children : [children];
                    return childArray.flatMap(findParent);
                });
            }
        };

        // Get first parent from each root and find the one containing our model
        const parents = roots.flatMap(findParent);

        return parents.find(parent =>
            Boolean(find_model(parent as Model,
                (candidate: Model) => {
                    return candidate.id === model.id;
                }
            ))
        );
    };
}

// "children" was created (and is visible as "Bokeh.find.children(...)") to allow
// the DataPipe object to be found so that they can be closed. Unfortunately, the
// design of the cubevisjs library is not so consistent which means that the
// properties traversed as branch forks to attempt to root out DataPipe objects.
// This usage discovers some, BUT NOT ALL of the DataPipes (where ctrl is a
// DataPipe object):
//
//     const data_pipes = Bokeh.find.children(
//                            ctrl.constructor,
//                            [ 'children', 'items', 'panes', 'tabs', 'child', 'ui',
//                              'source', 'data_source', 'renderers', 'image_source' ]
//                        )(showable)
//
export function children<T extends Model>(
    type_identifier: string | (new (...args: any[]) => Model),
    properties: string[] = [ 'children', 'items', 'panes', 'tabs', 'child', 'ui' ]
): (model: Model) => T[] {
    return (root) => {
        if (!root) return [];

        const matches_type = (node: Model): boolean => {
            if (typeof type_identifier === 'string') {
                // String: exact type match only
                return node.type === type_identifier;
            } else {
                // Class: use instanceof for inheritance checking
                return node instanceof type_identifier;
            }
        };

        const child_finder = (node: HasProps): T[] => {
            const nodeModel = node as Model;

            const search_children = (prop: string): T[] => {
                const children = (node as any)[prop];
                if (!children) return [];
                const childArray = Array.isArray(children) ? children : [children];
                return childArray.flatMap(child_finder);
            };

            const matchingNode = matches_type(nodeModel) ? [nodeModel as T] : [];
            return [...matchingNode, ...properties.flatMap(search_children)];
        };

        return child_finder(root);
    };
}

export function context(model: Model): BokehAppContext | undefined {
    const result = model.document?.get_model_by_name("_GLOBAL_APP_CONTEXT_") as BokehAppContext | undefined
    if ( result && ! result.frontend_id ) { object_id(result) }
    return result
}

export const showable = find_parent<Showable>(
    "cubevis.bokeh.models._showable.Showable"
);

export function appState(model: Model): object | undefined {
    const ctx = context(model)
    if ( ctx ) {
        // @ts-ignore: defined on startup
        const apps_state = window?.cubevisAppSession?.applications
        return apps_state[ctx.app_id]?.state
    }
    return
}

// This is a cubevisjs specialization of the casalib object_id. This version
// uses the casalib object_id (barring bugs) to generate the object ID and
// then if the obj being checked is a BokehAppContext it adds the ID to the
// app_state.
//
// Other Models will be initialized BEFORE the BokehAppContext. This will
// mean that the app_state.frontend_id may be filled in through earlier
// calls to this function BEFORE the initialize function of BokehAppContext
// is executed.
export function object_id(obj: any): string {

    if (!obj) return "invalid-object-provided";

    // We check for the function globally or via a known registry
    const globalCasalib = (globalThis as any).casalib;
    // 1. Generate ID: Call casalib object_id if available to generate ID
    //.   otherwise generate a unique string...
    const identifier = globalCasalib && typeof globalCasalib.object_id === 'function' ?
        globalCasalib.object_id(obj) : undefined

    // Check for 'app_state' without importing the class and if so, add id to app_state
    if ('app_state' in obj && obj.type === "cubevis.bokeh.models._bokeh_app_context.BokehAppContext") {

        if (!obj.app_state) obj.app_state = { }

        // Use the casalib ID or a locally generated ID
        if ( identifier ) {
            if (!obj.frontend_id) obj.frontend_id = identifier

            if ( ! obj.app_state.frontend_id || obj.app_state.frontend_id != identifier ) {
                obj.app_state.frontend_id = identifier
            }
        } else {
            if ( ! obj.app_state.frontend_id ) {
                obj.app_state.frontend_id = `cube-${Math.random().toString(36).substr(2, 9)}`
            }
        }

        return obj.app_state.frontend_id
    }


    return identifier ?? "casalib-not-found-fallback-id";
}
