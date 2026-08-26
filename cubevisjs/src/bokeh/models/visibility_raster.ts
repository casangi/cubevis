/**
 * visibility_raster.ts
 * ====================
 * TypeScript view for the Python ``VisibilityRaster`` Bokeh Model.
 *
 * Compiled into ``cubevisjs.min.js`` by the cubevis build system.
 * Registered under the name ``"VisibilityRaster"`` so that Bokeh's
 * JavaScript machinery can match it to the Python class.
 *
 * Role: controller, not renderer
 * --------------------------------
 * ``VisibilityRaster`` owns no DOM elements of its own.  The raster image
 * is displayed by a standard Bokeh ``Figure`` with an ``image_rgba`` glyph
 * and its own ``ColumnDataSource``.  This view observes Bokeh property
 * changes on the Model (``status_text``, ``is_rendering``) and propagates
 * them to the DOM elements that the figure's layout has already created.
 *
 * Comm-based interactivity
 * ------------------------
 * All hover probing and pan/zoom re-render requests are handled by
 * ``CustomJS`` callbacks on the Python side that call::
 *
 *     comm.send(messageId, payload, callback)
 *
 * The ``Comm`` object is passed directly as a Bokeh ``CustomJS`` arg on the
 * Python side, so Bokeh serialises it as a proper Model reference.
 * ``Comm.initialize()`` in ``comm_mgr.ts`` reconnects it to its ``CommMgr``
 * via ``document.get_model_by_name(comm_mgr_id)`` -- this works correctly
 * inside Colab iframes where ``window`` is not shared between output cells.
 *
 * This view therefore has NO direct interaction with the transport layer and
 * does NOT need to reference ``window.__cubevis_comm_mgr_*`` globals.
 */

import {Model} from "@bokehjs/model"
import type * as p from "@bokehjs/core/properties"

// ---------------------------------------------------------------------------
// Python Model property mirror
// ---------------------------------------------------------------------------

export namespace VisibilityRaster {
  export type Attrs = p.AttrsOf<Props>

  export type Props = Model.Props & {
    vr_id:         p.Property<string>
    canvas_width:  p.Property<number>
    canvas_height: p.Property<number>
    status_text:   p.Property<string>
    is_rendering:  p.Property<boolean>
  }
}

export interface VisibilityRaster extends VisibilityRaster.Attrs {}

// ---------------------------------------------------------------------------
// Model class
// ---------------------------------------------------------------------------

export class VisibilityRaster extends Model {
  declare properties: VisibilityRaster.Props

  static override __name__ = "VisibilityRaster"

  static {
    this.define<VisibilityRaster.Props>(({String, Int, Bool}) => ({
      vr_id:         [String,  ""   ],
      canvas_width:  [Int,     900  ],
      canvas_height: [Int,     600  ],
      status_text:   [String,  ""   ],
      is_rendering:  [Bool,    false],
    }))
  }

  // ------------------------------------------------------------------
  // Property observers
  //
  // ``status_text`` is updated by the Python side when it writes a new
  // hover label.  In normal Comm-wired mode the CustomJS callback writes
  // directly into the Bokeh Div, so this observer is only active in static
  // (no-Comm) mode where Python may set ``status_text`` from a cell.
  //
  // ``is_rendering`` drives a CSS ``data-rendering`` attribute on the
  // canvas container so a spinner overlay (defined in cubevis.css) can be
  // shown while a re-render request is in flight.
  // ------------------------------------------------------------------

  override connect_signals(): void {
    super.connect_signals()
    this.connect(this.properties.status_text.change, () => {
      this._on_status_text_change(this.status_text)
    })
    this.connect(this.properties.is_rendering.change, () => {
      this._on_is_rendering_change(this.is_rendering)
    })
  }

  protected _on_status_text_change(text: string): void {
    // The Python side sets a ``data-vr-info-id`` attribute on the info Div
    // element so it can be located here by vr_id without keeping a direct
    // cross-serialisation JS reference.
    const el = document.querySelector<HTMLElement>(
      `[data-vr-info-id="${this.vr_id}"]`
    )
    if (el != null) {
      el.innerHTML = text
    }
  }

  protected _on_is_rendering_change(rendering: boolean): void {
    const el = document.querySelector<HTMLElement>(
      `[data-vr-canvas-id="${this.vr_id}"]`
    )
    if (el != null) {
      el.setAttribute("data-rendering", rendering ? "true" : "false")
    }
  }
}
