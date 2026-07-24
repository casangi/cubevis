import * as p from "@bokehjs/core/properties"
import {ColumnDataSource} from "@bokehjs/models/sources/column_data_source"
import {BoxAnnotation} from "@bokehjs/models/annotations/box_annotation"
import {PanEvent} from "@bokehjs/core/ui_events"
import {DragTool, DragToolView} from "./drag_tool"
import {px_from_sx, py_from_sy, dx_from_px, dy_from_py} from "../util/find"
import {Comm} from "../transport/comm_mgr"

// Screen-pixel movement below this threshold counts as a "click" (zoom to
// 1:1) rather than a drag (rubber-band box).
const DRAG_THRESHOLD_PX = 3

// Tolerance for the at_pixel_res comparison — floating point range math
// means an exact zoom-to-1:1 rarely lands on the identical value on the
// next range 'end' event, so treat "close enough or finer" as at-res.
const PIXEL_RES_TOLERANCE = 1.01

// Guards against a second, redundant activate() call landing within this
// window of a real one — observed in practice as a duplicate tap/activation
// racing the re-render this tool's own zoom triggers, occasionally landing
// in a transient 0x0 measurement gap while the canvas is mid-rebuild.
// Already harmless (the guards below just no-op on bad geometry), this
// just avoids the redundant attempt and its console noise.
const ACTIVATE_DEBOUNCE_MS = 250

export class FlagToolView extends DragToolView {
  declare model: FlagTool

  private _start_sx = 0
  private _start_sy = 0
  private _dragging = false
  private _last_activate_zoom_ts = 0

  override connect_signals(): void {
    super.connect_signals()
    // Keep at_pixel_res current across *any* pan/zoom of this figure, not
    // just clicks on this tool — e.g. the standard box-zoom tool or wheel
    // zoom can also bring the view to/past 1:1.
    const {x_range, y_range} = this.plot_view.model
    this.connect(x_range.change, () => this._update_at_pixel_res())
    this.connect(y_range.change, () => this._update_at_pixel_res())
  }

  // Without this, the BoxAnnotation built as this.model.overlay is mutated
  // correctly in _pan() below but is never actually painted — the base
  // ToolView.overlays getter (models/tools/tool.ts) defaults to an empty
  // array, and that's what PlotView uses to decide which annotations get
  // a renderer built at all. Bokeh's own BoxZoomTool/BoxSelectTool both
  // override this the same way for the same reason.
  override get overlays() {
    return [...super.overlays, this.model.overlay]
  }

  // -------------------------------------------------------------------
  // Drag gesture: click -> zoom to 1:1; drag -> rubber-band box
  // -------------------------------------------------------------------

  override _pan_start(ev: PanEvent): void {
    this._start_sx = ev.sx
    this._start_sy = ev.sy
    this._dragging = false
    this.model.overlay.visible = false
  }

  override _pan(ev: PanEvent): void {
    const moved = Math.hypot(ev.sx - this._start_sx, ev.sy - this._start_sy)
    if (!this._dragging && moved < DRAG_THRESHOLD_PX) return
    this._dragging = true

    const sx0 = px_from_sx(this.plot_view, this._start_sx)
    const sy0 = py_from_sy(this.plot_view, this._start_sy)
    const sx1 = px_from_sx(this.plot_view, ev.sx)
    const sy1 = py_from_sy(this.plot_view, ev.sy)

    const x0 = dx_from_px(this.plot_view, sx0)
    const x1 = dx_from_px(this.plot_view, sx1)
    const y0 = dy_from_py(this.plot_view, sy0)
    const y1 = dy_from_py(this.plot_view, sy1)

    const ov = this.model.overlay
    ov.left    = Math.min(x0, x1)
    ov.right   = Math.max(x0, x1)
    ov.bottom  = Math.min(y0, y1)
    ov.top     = Math.max(y0, y1)
    // Flag vs. unflag reads visually distinct even mid-drag, before the
    // toolbar-button colour swap (which lives outside this view).
    ov.fill_color = this.model.flag ? "#f38ba8" : "#a6e3a1"
    ov.visible = true
  }

  override _pan_end(ev: PanEvent): void {
    const ov = this.model.overlay

    if (!this._dragging) {
      // Pure click: zoom this figure to 1:1 pixel resolution, centred on
      // the click location. No j2p message — this is a local view change.
      ov.visible = false
      const px = px_from_sx(this.plot_view, ev.sx)
      const py = py_from_sy(this.plot_view, ev.sy)
      const cx = dx_from_px(this.plot_view, px)
      const cy = dy_from_py(this.plot_view, py)
      this._zoom_to_pixel_res(cx, cy)
      return
    }

    this._dragging = false
    const {left, right, bottom, top} = ov
    ov.visible = false

    if (left == null || right == null || bottom == null || top == null) return
    if (!isFinite(left as number) || !isFinite(right as number)) return

    const {comm, msg_id, flag, panel, at_pixel_res} = this.model
    if (comm == null || !msg_id) return

    // Always send — even below flagging resolution. Python decides
    // whether to actually record a FlagDelta, and reports back through
    // the response callback below. There is no Bokeh server here, so a
    // Python-side `self._notify_div.text = ...` assignment on its own
    // never reaches the browser — applying resp fields to the live
    // Div models directly is the only thing that actually updates the
    // status bar, mirroring how doPlot's resp.status_text already works.
    comm.send(msg_id, {
      x0: left, x1: right, y0: bottom, y1: top,
      flag, panel, at_pixel_res,
      tool: flag ? "flag_box" : "unflag_box",
    }, (resp: any) => {
      if (resp == null) return
      const {notify_div, status_div} = this.model
      if (notify_div != null && resp.notify_text != null) {
        notify_div.text = resp.notify_text
        if (resp.notify_color != null) {
          notify_div.styles = {...notify_div.styles, color: resp.notify_color}
        }
      }
      if (status_div != null && resp.status_text != null) {
        status_div.text = resp.status_text
      }
    })
  }

  // -------------------------------------------------------------------
  // activate(): every time this tool becomes the active drag tool —
  // whether by a first click on its toolbar button, or by winning back
  // control from deactivate() below — zoom this figure to 1:1. This
  // covers the "first selection of the tool" case, not just re-clicks.
  //
  // deactivate(): "not unselectable" guard. A re-click of THIS tool's
  // own toolbar button while it's the sole active drag tool must not
  // turn it off — that click should zoom instead. But Bokeh's Toolbar
  // (models/tools/toolbar.ts, Toolbar._active_change) *also* deactivates
  // this tool any time a *different* drag tool (the flag/unflag sibling,
  // box_zoom, pan, ...) takes over — and that hand-off must be allowed
  // to proceed normally, or two drag tools end up stuck active at once.
  //
  // The two cases are distinguished by checking sibling drag tools
  // (same event_role, e.g. "pan") for one that's already active: the
  // tool taking over always has active=true *before* this deactivate()
  // runs (the assignment that triggered the whole cascade), whereas a
  // genuine self re-click never sets any other tool's active=true.
  // -------------------------------------------------------------------

  override activate(): void {
    const now = Date.now()
    if (now - this._last_activate_zoom_ts < ACTIVATE_DEBOUNCE_MS) {
      return
    }
    this._last_activate_zoom_ts = now
    this._zoom_to_pixel_res_at_view_center()
  }

  override deactivate(): void {
    const {toolbar} = this.plot_view.model
    // gestures is keyed by an unexported "GestureType" in toolbar.ts;
    // index defensively via `any` rather than fight that typing.
    const siblings = (toolbar.gestures as any)[this.model.event_role]?.tools ?? []
    const other_tool_taking_over = siblings.some((t: {active: boolean}) => t !== this.model && t.active)
    if (other_tool_taking_over) {
      // A different drag tool is becoming active — let this one turn off.
      return
    }
    // Self re-click while the sole active drag tool: refuse, and zoom
    // instead. Setting active back to true re-triggers activate() above,
    // which does the actual zoom — no need to duplicate that call here.
    this.model.active = true
  }

  // -------------------------------------------------------------------
  // Frame pixel size. Two Bokeh-internal candidates (frame.bbox and
  // Plot.inner_width/height) have both read 0 at the moment activate()
  // fires, even after a retry — so rather than guess at a third internal
  // property, measure the actual rendered DOM element directly via
  // getBoundingClientRect(), which every Bokeh view exposes via the
  // standard `.el` root element and can't be affected by whichever
  // internal Bokeh property happens to be synced or not at this moment.
  //
  // Caveat: this.plot_view.el is the *outer* plot container, which on
  // this app includes the right-side toolbar and axis label margins
  // (toolbar_location="right"), so it overestimates the true inner frame
  // width by roughly the toolbar + y-axis label width. That means the
  // "1:1" zoom will be very slightly looser than exact until we can
  // narrow this to the actual canvas/frame element specifically — worth
  // revisiting once the zoom is actually firing, but not a blocker now.
  // -------------------------------------------------------------------

  private _frame_size(): [number, number] {
    const el = this.plot_view.el as HTMLElement | undefined
    const rect = el?.getBoundingClientRect()
    const model = this.plot_view.model
    const frame_bbox = this.plot_view.frame.bbox

    if (rect != null && rect.width > 0 && rect.height > 0) {
      return [rect.width, rect.height]
    }
    if (model.inner_width > 0 && model.inner_height > 0) {
      return [model.inner_width, model.inner_height]
    }
    return [frame_bbox.width, frame_bbox.height]
  }

  private _zoom_to_pixel_res_at_view_center(allow_retry = true): void {
    const [frame_w, frame_h] = this._frame_size()
    if (frame_w === 0 || frame_h === 0) {
      if (allow_retry) {
        // Belt-and-braces: retry once more on the next frame before
        // giving up, in case even inner_width/inner_height hasn't been
        // set yet (e.g. genuinely the very first paint).
        console.debug("[FlagTool] frame not yet measured (0x0), retrying next frame", {frame_w, frame_h})
        requestAnimationFrame(() => this._zoom_to_pixel_res_at_view_center(false))
      } else {
        console.warn("[FlagTool] aborting zoom: frame still unmeasured after retry", {frame_w, frame_h})
      }
      return
    }
    const x_range = this.plot_view.model.x_range
    const y_range = this.plot_view.model.y_range
    const cx = ((x_range.start as number) + (x_range.end as number)) / 2
    const cy = ((y_range.start as number) + (y_range.end as number)) / 2
    this._zoom_to_pixel_res(cx, cy)
  }

  private _zoom_to_pixel_res(cx: number, cy: number): void {
    const {state_source} = this.model
    if (state_source == null) return
    const d = state_source.data as {[key: string]: number[]}

    const full_x0 = d["full_x0"]?.[0]
    const full_x1 = d["full_x1"]?.[0]
    const full_y0 = d["full_y0"]?.[0]
    const full_y1 = d["full_y1"]?.[0]
    const agg_n_x = d["agg_n_x"]?.[0]
    const agg_n_y = d["agg_n_y"]?.[0]
    const [frame_w, frame_h] = this._frame_size()

    // Diagnostic (debug-level, hidden unless verbose logging is enabled) —
    // left in place since it's cheap and useful if zoom targeting ever
    // looks wrong again.
    console.debug("[FlagTool] zoom inputs", {
      cx, cy, full_x0, full_x1, full_y0, full_y1, agg_n_x, agg_n_y,
      frame_w, frame_h,
    })

    if (!isFinite(full_x0) || !isFinite(full_x1) || !isFinite(full_y0) || !isFinite(full_y1)) {
      console.warn("[FlagTool] aborting zoom: non-finite full_x0/x1/y0/y1", {full_x0, full_x1, full_y0, full_y1})
      return
    }
    if (!isFinite(agg_n_x) || !isFinite(agg_n_y) || agg_n_x <= 0 || agg_n_y <= 0) {
      console.warn("[FlagTool] aborting zoom: invalid agg_n_x/agg_n_y", {agg_n_x, agg_n_y})
      return
    }

    // Data span represented by one aggregation cell at the full extent —
    // this is what "one screen pixel = one data cell" means client-side.
    const data_per_px_x = (full_x1 - full_x0) / agg_n_x
    const data_per_px_y = (full_y1 - full_y0) / agg_n_y

    const target_w = frame_w * data_per_px_x
    const target_h = frame_h * data_per_px_y

    if (!isFinite(target_w) || !isFinite(target_h) || target_w <= 0 || target_h <= 0) {
      console.warn("[FlagTool] aborting zoom: invalid target_w/target_h", {target_w, target_h})
      return
    }

    // "Zoom to 1:1" means "ensure at least 1:1" — if the view is already
    // at or past that resolution (e.g. the user manually zoomed in
    // further, or this is a second activation after already zooming),
    // leave it alone rather than snapping back out to exactly 1:1.
    const cur_x_range = this.plot_view.model.x_range
    const cur_w = Math.abs((cur_x_range.end as number) - (cur_x_range.start as number))
    if (cur_w <= target_w * PIXEL_RES_TOLERANCE) {
      this._update_at_pixel_res()
      return
    }

    // If the click/current-view centre is nowhere near the actual data
    // (e.g. the user was zoomed way out, off to one side of the real
    // extent), clamp the centre back inside the full data range rather
    // than zooming to an empty, off-data window.
    const clamped_cx = Math.min(Math.max(cx, full_x0), full_x1)
    const clamped_cy = Math.min(Math.max(cy, Math.min(full_y0, full_y1)), Math.max(full_y0, full_y1))
    if (clamped_cx !== cx || clamped_cy !== cy) {
      console.debug("[FlagTool] clamped zoom centre into full data range", {cx, cy, clamped_cx, clamped_cy})
    }

    const new_x0 = clamped_cx - target_w / 2
    const new_x1 = clamped_cx + target_w / 2
    const new_y0 = clamped_cy - target_h / 2
    const new_y1 = clamped_cy + target_h / 2

    if (!isFinite(new_x0) || !isFinite(new_x1) || !isFinite(new_y0) || !isFinite(new_y1) || new_x0 === new_x1 || new_y0 === new_y1) {
      console.warn("[FlagTool] aborting zoom: degenerate resulting range", {new_x0, new_x1, new_y0, new_y1})
      return
    }

    // Local to this figure only — the other panel's zoom state is left
    // alone, per the design discussion (forcing it to change would be
    // surprising to the user).
    const x_range = this.plot_view.model.x_range
    const y_range = this.plot_view.model.y_range
    x_range.setv({start: new_x0, end: new_x1})
    y_range.setv({start: new_y0, end: new_y1})

    this._update_at_pixel_res()
  }

  private _update_at_pixel_res(): void {
    const {state_source} = this.model
    if (state_source == null) return
    const d = state_source.data as {[key: string]: number[]}

    const full_x0 = d["full_x0"]?.[0]
    const full_x1 = d["full_x1"]?.[0]
    const agg_n_x = d["agg_n_x"]?.[0]
    if (!isFinite(full_x0) || !isFinite(full_x1) || !isFinite(agg_n_x) || agg_n_x <= 0) return

    const data_per_px_x = (full_x1 - full_x0) / agg_n_x
    const [frame_w] = this._frame_size()
    const target_w = frame_w * data_per_px_x
    if (!isFinite(target_w) || target_w <= 0) return

    const x_range = this.plot_view.model.x_range
    const cur_w = Math.abs((x_range.end as number) - (x_range.start as number))

    // At or past 1:1 means zoomed in at least as far as the target width
    // (a *smaller* current width is finer than 1:1, which is still valid
    // for flagging individual points).
    this.model.at_pixel_res = cur_w <= target_w * PIXEL_RES_TOLERANCE
  }
}

export namespace FlagTool {
  export type Attrs = p.AttrsOf<Props>

  export type Props = DragTool.Props & {
    flag:          p.Property<boolean>
    comm:          p.Property<Comm | null>
    msg_id:        p.Property<string>
    panel:         p.Property<string>
    image_source:  p.Property<ColumnDataSource | null>
    state_source:  p.Property<ColumnDataSource | null>
    overlay:       p.Property<BoxAnnotation>
    at_pixel_res:  p.Property<boolean>
    notify_div:    p.Property<any>
    status_div:    p.Property<any>
  }
}

export interface FlagTool extends FlagTool.Attrs {}

export class FlagTool extends DragTool {
  declare properties: FlagTool.Props
  declare __view_type__: FlagToolView

  static __module__ = "cubevis.bokeh.tools._flag_tool"

  constructor(attrs?: Partial<FlagTool.Attrs>) {
    super(attrs)
  }

  static {
    this.prototype.default_view = FlagToolView
    this.define<FlagTool.Props>(({Boolean, String, Nullable, Ref, Any}) => ({
      flag:          [ Boolean, true ],
      comm:          [ Nullable(Ref(Comm)), null ],
      msg_id:        [ String, "" ],
      panel:         [ String, "" ],
      image_source:  [ Nullable(Ref(ColumnDataSource)), null ],
      state_source:  [ Nullable(Ref(ColumnDataSource)), null ],
      overlay:       [ Ref(BoxAnnotation), () => new BoxAnnotation({
        syncable:     false,
        propagate_hover: false,
        level:        "overlay",
        visible:      false,
        left_units:   "data",
        right_units:  "data",
        top_units:    "data",
        bottom_units: "data",
        fill_color:   "#f38ba8",
        fill_alpha:   0.3,
        line_color:   "#f38ba8",
        line_alpha:   0.8,
        line_width:   2,
        line_dash:    [4, 4],
      }) ],
      at_pixel_res:  [ Boolean, false ],
      notify_div:    [ Nullable(Any), null ],
      status_div:    [ Nullable(Any), null ],
    }))
  }

  override tool_name = "Flag"
  override event_type = "pan" as "pan"
  override default_order = 11
}
