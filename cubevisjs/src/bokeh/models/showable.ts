import {LayoutDOM, LayoutDOMView} from "@bokehjs/models/layouts/layout_dom"
import type {FullDisplay} from "@bokehjs/models/layouts/layout_dom"
import {UIElement} from "@bokehjs/models/ui/ui_element"
import type * as p from "@bokehjs/core/properties"

export class ShowableView extends LayoutDOMView {
  declare model: Showable
  private _overlay_el: HTMLDivElement | null = null

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

  connect_signals(): void {
    super.connect_signals()
    // Parent will automatically handle child model changes through child_models

    // Listen for changes to the disabled properties
    this.connect(this.model.properties.disabled.change, () => {
      this._update_disabled_state()
    })

    // update message if it changes while disabled
    this.connect(this.model.properties.disabled_message.change, () => {
      if (this.model.disabled && this._overlay_el != null) {
        // Update the message content
        this._update_overlay_message()
      }
    })
  }

  // MINIMAL OVERRIDE: Let parent handle layout
  _update_layout(): void {
    super._update_layout()
  }

  // MINIMAL OVERRIDE: Simple rendering that lets parent do the work
  render(): void {
    super.render()

    console.log('Showable render() - disabled:', this.model.disabled, 'shadow_el:', this.shadow_el != null)
    
    // The parent class should have already rendered our children
    // Just ensure we have proper styling/structure if needed
    if (this.child_views.length === 0 && this.model.ui == null) {
      this.el.innerHTML = `<div style="color: gray; padding: 10px; border: 1px dashed gray;">
        Showable: No UI element set
      </div>`
    }

    // Apply disabled state if needed
    this._update_disabled_state()
  }

  // MINIMAL OVERRIDE: Let parent handle after_layout
  after_layout(): void {
    super.after_layout()
    // CRITICAL: Re-apply disabled state after layout is complete
    // This ensures the overlay appears correctly on notebook reload
    console.log('Showable after_layout() - disabled:', this.model.disabled, 'shadow_el:', this.shadow_el != null)
    if (this.model.disabled) {
      this._update_disabled_state()
    }
  }

  // MINIMAL OVERRIDE: Let parent handle sizing
  protected _intrinsic_display(): FullDisplay {
    return super._intrinsic_display()
  }

  private _update_disabled_state(): void {
    console.log('_update_disabled_state called, disabled:', this.model.disabled)
    if (this.model.disabled) {
      this._show_disabled_overlay()
      // Apply grayscale to the content
      this.el.style.filter = 'grayscale(50%)'
    } else {
      this._hide_disabled_overlay()
      this.el.style.filter = ''
    }
  }

  private _disable_interactive_elements(): void {
    // Find and disable all Bokeh toolbars within this.el
    const toolbars = this.el.querySelectorAll('.bk-toolbar, .bk-toolbar-button')
    toolbars.forEach((toolbar) => {
      (toolbar as HTMLElement).style.pointerEvents = 'none';
      (toolbar as HTMLElement).style.opacity = '0.5'
    })

    // Find and disable all canvases (where figures are rendered)
    const canvases = this.el.querySelectorAll('canvas')
    canvases.forEach((canvas) => {
      canvas.style.pointerEvents = 'none'
    })

    // Disable any button elements
    const buttons = this.el.querySelectorAll('button, .bk-btn')
    buttons.forEach((button) => {
      (button as HTMLButtonElement).disabled = true
    })
  }

  private _enable_interactive_elements(): void {
    // Re-enable all the elements that were disabled
    const shadowRoot = this.shadow_el
    if (!shadowRoot) return

    // Re-enable Bokeh toolbars within shadow root
    const toolbars = shadowRoot.querySelectorAll('.bk-toolbar, .bk-toolbar-button')
    toolbars.forEach((toolbar) => {
      (toolbar as HTMLElement).style.pointerEvents = '';
      (toolbar as HTMLElement).style.opacity = ''
    })

    // Re-enable canvases
    const canvases = shadowRoot.querySelectorAll('canvas')
    canvases.forEach((canvas) => {
      canvas.style.pointerEvents = ''
    })

    // Re-enable buttons
    const buttons = shadowRoot.querySelectorAll('button, .bk-btn')
    buttons.forEach((button) => {
      (button as HTMLButtonElement).disabled = false
    })

    console.log('Interactive elements re-enabled')
  }

  private _show_disabled_overlay(): void {
    console.log('_show_disabled_overlay called, _overlay_el exists:', this._overlay_el != null)
    console.log('disabled_message:', this.model.disabled_message)

    if (this._overlay_el == null) {
      console.log('Creating new overlay element')

      // Find the shadow root
      const shadowRoot = this.shadow_el
      console.log('Shadow root:', shadowRoot)

      if (!shadowRoot) {
        console.error('No shadow root found!')
        return
      }

      // Create the message box FIRST
      const messageBox = document.createElement('div')
      messageBox.className = 'showable-disabled-message'
      messageBox.style.cssText = `
        background: white;
        padding: 30px 40px;
        border-radius: 10px;
        box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
        text-align: center;
        border: 2px solid #4CAF50;
        max-width: 90%;
        word-wrap: break-word;
        pointer-events: auto;
        cursor: default;
      `

      messageBox.innerHTML = `
        <div style="font-size: 24px; font-weight: bold; color: #4CAF50; margin-bottom: 10px;">
          ${this.model.disabled_message}
        </div>
      `

      console.log('Message box created')

      // Now create overlay
      this._overlay_el = document.createElement('div')

      // Add message box
      this._overlay_el.appendChild(messageBox)

      // Use absolute positioning and cover entire shadow root content
      this._overlay_el.style.setProperty('position', 'absolute', 'important')
      this._overlay_el.style.setProperty('top', '0', 'important')
      this._overlay_el.style.setProperty('left', '0', 'important')
      this._overlay_el.style.setProperty('right', '0', 'important')
      this._overlay_el.style.setProperty('bottom', '0', 'important')
      this._overlay_el.style.setProperty('width', '100%', 'important')
      this._overlay_el.style.setProperty('height', '100%', 'important')
      this._overlay_el.style.setProperty('background-color', 'rgba(220, 220, 220, 0.85)', 'important')
      this._overlay_el.style.setProperty('display', 'flex', 'important')
      this._overlay_el.style.setProperty('justify-content', 'center', 'important')
      this._overlay_el.style.setProperty('align-items', 'center', 'important')
      this._overlay_el.style.setProperty('z-index', '2147483647', 'important')
      this._overlay_el.style.setProperty('pointer-events', 'auto', 'important')
      this._overlay_el.style.setProperty('cursor', 'not-allowed', 'important')

      console.log('Overlay element styles set')

      // Find the first child of shadow root (the actual content container)
      const contentContainer = shadowRoot.firstElementChild as HTMLElement
      if (contentContainer) {
        contentContainer.style.position = 'relative'
        console.log('Set content container to relative positioning')
      }

      // Append to shadow root
      shadowRoot.appendChild(this._overlay_el)
      console.log('Overlay appended to shadow root')

      // DEBUG: Check dimensions
      setTimeout(() => {
        const rect = this._overlay_el!.getBoundingClientRect()
        const messageRect = messageBox.getBoundingClientRect()

        console.log('Overlay bounding rect:', {
          width: rect.width,
          height: rect.height,
          top: rect.top,
          left: rect.left
        })
        console.log('Message box bounding rect:', {
          width: messageRect.width,
          height: messageRect.height
        })
      }, 100)

      // Also explicitly disable interactive elements
      this._disable_interactive_elements()

      // Block interaction events but ALLOW wheel/scroll events
      const interactionEvents = [
        'mousedown', 'mouseup', 'click', 'dblclick', 'contextmenu',
        'touchstart', 'touchmove', 'touchend',
        'keydown', 'keyup', 'keypress',
        'pointerdown', 'pointerup', 'pointermove',
        'dragstart', 'drag', 'dragend'
      ]

      interactionEvents.forEach(eventType => {
        this._overlay_el!.addEventListener(eventType, (e) => {
          // Only allow events on the message box itself
          if (e.target === messageBox || messageBox.contains(e.target as Node)) {
            return
          }
          // Block everything else
          e.stopPropagation()
          e.preventDefault()
          e.stopImmediatePropagation()
          return false
        }, { capture: true, passive: false })
      })

      // For wheel events, don't block them - let them propagate for scrolling
      // But we need to ensure they reach the scrollable container
      this._overlay_el.addEventListener('wheel', (e) => {
        // Don't preventDefault - allow scrolling
        // But stop propagation to GUI elements under the overlay
        // The event will still scroll the parent container

        // Find the scrollable parent (outside shadow DOM)
        let scrollableParent = (shadowRoot.host as HTMLElement).parentElement
        while (scrollableParent) {
          const overflow = window.getComputedStyle(scrollableParent).overflow
          const overflowY = window.getComputedStyle(scrollableParent).overflowY
          if (overflow === 'auto' || overflow === 'scroll' || overflowY === 'auto' || overflowY === 'scroll') {
            break
          }
          scrollableParent = scrollableParent.parentElement
        }

        if (scrollableParent) {
          // Manually scroll the parent
          scrollableParent.scrollTop += (e as WheelEvent).deltaY
          scrollableParent.scrollLeft += (e as WheelEvent).deltaX
        }

        // Prevent the event from reaching GUI elements
        e.stopPropagation()
        e.preventDefault()
      }, { capture: true, passive: false })

    } else {
      console.log('Overlay already exists, showing it')
      this._overlay_el.style.display = 'flex'
    }

    console.log('_show_disabled_overlay complete')
  }

  private _update_overlay_message(): void {
    if (this._overlay_el != null) {
      const messageBox = this._overlay_el.querySelector('.showable-disabled-message')
      if (messageBox) {
        messageBox.innerHTML = `
          <div style="font-size: 24px; font-weight: bold; color: #4CAF50; margin-bottom: 10px;">
            ${this.model.disabled_message}
          </div>
          <div style="font-size: 14px; color: #666;">
            You can now close this GUI or continue working in your notebook
          </div>
        `
      }
    }
  }

  private _hide_disabled_overlay(): void {
    if (this._overlay_el != null) {
      this._overlay_el.style.display = 'none'

      // Re-enable interactive elements when hiding overlay
      this._enable_interactive_elements()
    }
  }

  remove(): void {
    if (this._overlay_el != null) {
      this._overlay_el.remove()
      this._overlay_el = null
    }
    super.remove()
  }

}

export namespace Showable {
  export type Attrs = p.AttrsOf<Props>

  export type Props = LayoutDOM.Props & {
    ui: p.Property<UIElement>
    disabled_message: p.Property<string>
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

    this.define<Showable.Props>(({Ref, String}) => ({
      ui: [ Ref(UIElement) ],
      disabled_message: [ String, "Interaction Complete ✓" ],
    }))
  }
}
