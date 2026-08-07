import {Span,SpanView} from "@bokehjs/models/annotations/span"
import type {PanEvent} from "@bokehjs/core/ui_events"
import {LODStart,LODEnd} from "@bokehjs/core/bokeh_events"
import {dist_to_segment} from "@bokehjs/core/hittest"
import type * as p from "@bokehjs/core/properties"
//
// It is not clear that it is possible to create and register new events:
// ---  ---  ---  ---  ---  ---  ---  ---  ---  ---  ---  ---  ---  ---  ---
//import {EditStart,EditEnd} from "../events"
// ---  ---  ---  ---  ---  ---  ---  ---  ---  ---  ---  ---  ---  ---  ---
// so instead we were using the LODStart and LODEnd because they have no
// properties and are PlotEvents.
//
// UPDATE: LODStart/LODEnd are Bokeh's own PlotEvent subclasses,
// documented (see bokeh.events.PlotEvent/LODEnd on the Python side) as
// applying to Plot models specifically. In practice, triggering them
// from a non-Plot origin (this Span) never reached any js_on_event
// listener at all -- confirmed with live console output across two
// separate debugging rounds (listening on the span itself, then on the
// containing figure instead): zero LOD-related log entries, no errors,
// nothing. Whether that's BokehJS silently refusing to route a
// Plot-scoped event from a non-Plot origin, or something else, the
// practical result was the same: it's not a usable signal here.
//
// Replaced with a genuine model property (`dragging`) instead of a
// custom Event. This uses the exact same property-change dispatch
// machinery Bokeh already uses for every other property (already
// confirmed reliable via `location`'s own js_on_change firing
// correctly during drag) rather than a special-purpose Event class
// whose applicability to non-Plot origins is unclear. Consumers should
// listen via `span.js_on_change('dragging', callback)` and act when
// `cb_obj.dragging` becomes `false` (drag just ended), ignoring the
// `true` (drag just started) transition.
//
// The trigger_event(...) calls below are left in place -- harmless if
// unused, and consumers relying on the old LODStart/LODEnd behavior
// (if any exist) aren't broken by this change -- but `dragging` is now
// the recommended, verified-working signal.
//
export class EditSpanView extends SpanView {
  declare model: EditSpan

  // Bokeh's UIEventBus (ui_events.ts) dispatches a pan gesture to the
  // FIRST Pannable candidate -- among ALL renderers whose
  // interactive_hit() succeeds at the click point -- in reversed
  // add-order (most-recently-added first). There is no distance
  // comparison anywhere in that dispatch: hit_test_renderers() just
  // collects every matching candidate, and __trigger()'s pan:start
  // loop takes the first one whose on_pan_start() returns true.
  //
  // With two EditSpans (min/max) on the same figure, whichever was
  // added LAST (typically max_span, added after min_span) always wins
  // any tie whenever both spans' hit-test tolerance zones overlap at
  // the click point -- regardless of which one is actually closer to
  // where the user clicked. This is the confirmed cause of needing
  // multiple attempts specifically when grabbing whichever span wasn't
  // added last, worked out from reading Bokeh's actual dispatch source
  // (span.ts + ui_events.ts) rather than guessed at.
  //
  // Fix: override interactive_hit (which hit_test_renderers() calls to
  // build its candidate list in the first place) to do the distance
  // comparison Bokeh's own dispatcher doesn't. Only claim the hit if
  // this span's segment is at least as close to the click point as its
  // sibling's -- set sibling on both spans from Python (see
  // colormap_controls() in visibility_raster.py/visibility_scatter.py).
  // `_hit_test`/`Line`/`EDGE_TOLERANCE` in span.ts are all private/
  // unexported, so the tolerance check here is a duplicate of that
  // logic (EDGE_TOLERANCE hardcoded to 2.5 to match) rather than a
  // reused call -- if span.ts's own tolerance ever changes upstream,
  // this needs updating to match.
  override interactive_hit(sx: number, sy: number): boolean {
    if (!this.model.visible || !this.model.editable) {
      return false
    }

    const EDGE_TOLERANCE = 2.5  // must track span.ts's own (private, unexported) constant
    const tolerance = Math.max(EDGE_TOLERANCE, this.model.line_width/2)
    const own_dist = dist_to_segment({x: sx, y: sy}, this.line.p0, this.line.p1)
    if (own_dist >= tolerance) {
      return false
    }

    const sibling = this.model.sibling
    if (sibling != null) {
      let sibling_view: EditSpanView | undefined
      for (const view of this.plot_view.all_renderer_views) {
        if (view.model === sibling) {
          sibling_view = view as EditSpanView
          break
        }
      }
      if (sibling_view?.model.visible && sibling_view.model.editable) {
        const sib_dist = dist_to_segment({x: sx, y: sy}, sibling_view.line.p0, sibling_view.line.p1)
        if (sib_dist < own_dist) {
          return false  // sibling is strictly closer -- let it claim the hit instead
        }
      }
    }

    return true
  }

  override on_pan_start(ev: PanEvent): boolean {
    const result = super.on_pan_start( ev )
    if (result) {
      // Only mark dragging when the hit-test in super.on_pan_start
      // actually succeeded (result === true) -- previously this ran
      // unconditionally, which left `dragging` stuck `true` after a
      // missed click (a click near, but not close enough to, the span:
      // super.on_pan_start returns false in that case, and Bokeh will
      // not call on_pan_end for this view for that gesture, so nothing
      // would ever reset it back to false). A real correctness bug
      // regardless of the interactive_hit fix above.
      this.model.trigger_event( new LODStart( ) )
      this.model.dragging = true
    }
    return result
  }

  override on_pan(ev: PanEvent): void {
    super.on_pan( ev )
  }

  override on_pan_end(ev: PanEvent): void {
    super.on_pan_end( ev )
    this.model.trigger_event( new LODEnd( ) )
    this.model.dragging = false
  }

}

export namespace EditSpan {
  export type Attrs = p.AttrsOf<Props>
  export type Props = Span.Props & {
    dragging: p.Property<boolean>
    sibling: p.Property<EditSpan | null>
  }
}

export interface EditSpan extends EditSpan.Attrs {}

export class EditSpan extends Span {
  declare properties: EditSpan.Props
  declare __view_type__: EditSpanView
    
  // cubevis.bokeh.models._edit_span.EditSpan
  static __module__ = "cubevis.bokeh.models._edit_span"


  constructor(attrs?: Partial<EditSpan.Attrs>) {
    super(attrs)
  }

  static {
    this.prototype.default_view = EditSpanView

    this.define<EditSpan.Props>(({Boolean, Nullable, Ref}) => ({
      dragging: [ Boolean, false ],
      // Reference to a paired EditSpan (e.g. min_span <-> max_span) for
      // the interactive_hit tie-break above. Self-referential model
      // reference -- EditSpan is already fully declared by the time
      // this static block runs, so the direct (non-string) reference
      // resolves fine. CONFIRMED: this needed to be Ref, not Instance
      // (which is what the corresponding Python-side property uses,
      // via bokeh.core.properties.Instance) -- Ref is the correct
      // TS-side Kind factory for "reference to another model", Instance
      // is the Python-side one; the two aren't interchangeable across
      // languages despite the similar-sounding name.
      sibling: [ Nullable(Ref(EditSpan)), null ],
    }))
  }

}
