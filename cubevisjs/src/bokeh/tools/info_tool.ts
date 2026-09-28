import * as p from "@bokehjs/core/properties"
import {BoxAnnotation} from "@bokehjs/models/annotations/box_annotation"
import {PanEvent} from "@bokehjs/core/ui_events"
import {DragTool, DragToolView} from "./drag_tool"
import {px_from_sx, py_from_sy, dx_from_px, dy_from_py} from "../util/find"
import {Comm} from "../transport/comm_mgr"

// Screen-pixel movement below this threshold counts as a "click" (send
// the point) rather than a drag (send the box). Same value and same
// rationale as FlagTool's own DRAG_THRESHOLD_PX (flag_tool.ts) -- kept
// as a separate local constant rather than imported from there, since
// InfoTool deliberately doesn't depend on FlagTool at all (see
// _info_tool.py's module docstring for why duplicating this one small
// piece of bookkeeping was preferred over adding a shared dependency).
const DRAG_THRESHOLD_PX = 3

function _placeholderHtml(): string {
  return (
    "<html><head><title>Fetching\u2026</title></head>" +
    "<body style=\"background:#1e1e2e;color:#cdd6f4;" +
    "font-family:-apple-system,Helvetica,Arial,sans-serif;padding:24px;\">" +
    "Fetching exact identity\u2026</body></html>"
  )
}

// Opens a brand-new tab (an empty/"_blank" target name always creates a
// new browsing context rather than reusing one -- unlike an earlier
// version of this tool, which intentionally reused one fixed-name tab.
// Changed on request: multiple clicks/drags are a natural way to
// compare several selections side by side, and a reused tab overwrote
// the previous answer on every new click, which worked against that.
// Each tab's *title* still carries the clicked rectangle (see
// VisibilityScatter._handle_probe_region) so a pile of open tabs stays
// distinguishable in the browser's tab strip/window list.
//
// Deliberately passes NO window-features string to window.open(). A
// features string (even just "width=...,height=...") is what tells
// Chrome/Firefox/Safari to open a separate chromeless popup *window*
// instead of a tab in the current window -- confirmed the hard way: an
// earlier version passed "width=560,height=680" specifically to size
// the placeholder nicely, and every click opened its own floating
// window rather than a tab, which piled up into exactly the clutter
// multiple open tabs were supposed to avoid. Omitting the third
// argument (equivalently, calling with no features at all) is what
// actually gets ordinary tab behavior.
//
// Immediately writes a placeholder into the new tab, synchronously,
// while still inside the click/drag-release event that triggered this
// -- i.e. still within a browser-trusted user gesture. Filling in the
// real content has to wait for comm.send()'s response (a real backend
// round trip, not a free local lookup -- see
// VisibilityScatter._handle_probe_region), which is asynchronous; by
// the time that resolves the browser no longer considers it part of
// the original gesture, and would very likely block a *new*
// window.open() call made from inside that callback instead. Opening
// up front, before the async wait, sidesteps that entirely.
function _openInfoTab(): Window | null {
  let w: Window | null = null
  try {
    w = window.open("", "_blank")
  } catch (e) {
    console.warn("[InfoTool] window.open failed", e)
    return null
  }
  if (w == null) {
    console.warn("[InfoTool] window.open returned null -- popup blocked?")
    return null
  }
  try {
    w.document.open()
    w.document.write(_placeholderHtml())
    w.document.close()
  } catch (e) {
    // Non-fatal: the real content still arrives via the comm.send()
    // callback below and gets the same write() treatment there.
    console.warn("[InfoTool] could not write tab placeholder", e)
  }
  return w
}

export class InfoToolView extends DragToolView {
  declare model: InfoTool

  private _start_sx = 0
  private _start_sy = 0
  private _dragging = false

  // Same reasoning as FlagToolView's identical override: without this,
  // the BoxAnnotation built as this.model.overlay is mutated correctly
  // in _pan() below but never actually painted -- ToolView.overlays
  // defaults to empty, and that's what PlotView uses to decide which
  // annotations get a renderer built at all.
  override get overlays() {
    return [...super.overlays, this.model.overlay]
  }

  // -------------------------------------------------------------------
  // Drag gesture: click -> point probe; drag -> box probe. Unlike
  // FlagTool, BOTH branches send a message -- there is no "click does
  // something purely local" case here.
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
    ov.visible = true
  }

  override _pan_end(ev: PanEvent): void {
    const {comm, msg_id} = this.model
    if (comm == null || !msg_id) {
      this.model.overlay.visible = false
      this._dragging = false
      return
    }

    if (!this._dragging) {
      this.model.overlay.visible = false
      const px = px_from_sx(this.plot_view, ev.sx)
      const py = py_from_sy(this.plot_view, ev.sy)
      const x = dx_from_px(this.plot_view, px)
      const y = dy_from_py(this.plot_view, py)

      const w = _openInfoTab()
      comm.send(msg_id, {tool: "info_click", x, y}, (resp: any) => {
        this._applyResponse(w, resp)
      })
      return
    }

    this._dragging = false
    const ov = this.model.overlay
    const {left, right, bottom, top} = ov
    ov.visible = false

    if (left == null || right == null || bottom == null || top == null) return
    if (!isFinite(left as number) || !isFinite(right as number) ||
        !isFinite(bottom as number) || !isFinite(top as number)) return

    const w = _openInfoTab()
    comm.send(msg_id, {
      tool: "info_box", x0: left, x1: right, y0: bottom, y1: top,
    }, (resp: any) => {
      this._applyResponse(w, resp)
    })
  }

  // Applies the Python-built HTML page (see
  // VisibilityScatter._probe_region_page) to the tab opened at
  // click/drag-release time. `w` may be null (popup blocked) or
  // already closed by the user before the round trip finished --
  // both are silently ignored rather than surfaced, since there is no
  // useful recovery action for either case beyond what the console
  // warnings already logged at open time.
  private _applyResponse(w: Window | null, resp: any): void {
    if (w == null || w.closed) return
    if (resp == null || resp.info_html == null) return
    try {
      w.document.open()
      w.document.write(resp.info_html)
      w.document.close()
    } catch (e) {
      console.warn("[InfoTool] could not write response into tab", e)
    }
  }
}

export namespace InfoTool {
  export type Attrs = p.AttrsOf<Props>

  export type Props = DragTool.Props & {
    comm:    p.Property<Comm | null>
    msg_id:  p.Property<string>
    overlay: p.Property<BoxAnnotation>
  }
}

export interface InfoTool extends InfoTool.Attrs {}

export class InfoTool extends DragTool {
  declare properties: InfoTool.Props
  declare __view_type__: InfoToolView

  static __module__ = "cubevis.bokeh.tools._info_tool"

  constructor(attrs?: Partial<InfoTool.Attrs>) {
    super(attrs)
  }

  static {
    this.prototype.default_view = InfoToolView
    this.define<InfoTool.Props>(({Nullable, Ref, String}) => ({
      comm:    [ Nullable(Ref(Comm)), null ],
      msg_id:  [ String, "" ],
      overlay: [ Ref(BoxAnnotation), () => new BoxAnnotation({
        syncable:        false,
        propagate_hover: false,
        level:           "overlay",
        visible:         false,
        left_units:      "data",
        right_units:     "data",
        top_units:       "data",
        bottom_units:    "data",
        fill_color:      "#89b4fa",
        fill_alpha:      0.25,
        line_color:      "#89b4fa",
        line_alpha:      0.9,
        line_width:      2,
        line_dash:       [2, 2],
      }) ],
    }))
  }

  override tool_name = "Exact identity"
  override event_type = "pan" as "pan"
  override default_order = 12
}
