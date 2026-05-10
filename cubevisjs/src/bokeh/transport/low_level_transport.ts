/**
 * Transport Implementations for TypeScript
 * 
 * These are imported and used by CommMgr in comm_bokeh_models.ts
 * They handle the actual communication with Python backend.
 */
import { CommMgr } from "./comm_mgr"
import { serialize, deserialize } from "../util/conversions"
import * as find from "../util/find"

/**
 * Global window extensions for Colab and Jupyter
 */
declare global {
    interface Window {
        google?: {
            colab?: {
                kernel?: {
                    comms?: {
                        open: (target: string, data: any) => Promise<any>
                        registerTarget: (target: string, handler: (comm: any, msg: any) => void) => void
                    }
                }
            }
        }
        Jupyter?: any
        jupyterapp?: any
        IPython?: any
    }
}

// ============================================================================
// Transport Base Interface
// ============================================================================

/**
 * Abstract base interface for communication transports.
 */
export interface TransportBase {
    /**
     * Connect and initialize the transport.
     * Performs any necessary handshaking.
     */
    connect(): Promise<void>
    
    /**
     * Send a message through this transport.
     */
    send(message: any): void
    
    /**
     * Set callback for incoming messages.
     */
    setMessageCallback(callback: (msg: any) => void): void
    
    /**
     * Run the transport event loop.
     * 
     * For WebSocket: processes incoming messages via iteration
     * For Colab/Jupyter: keeps event loop alive for callbacks
     * 
     * Blocks until connection closes or shutdown requested.
     */
    run(): Promise<void>

    /**
     * Close the transport connection.
     */
    close(): void
    
    /**
     * Check if transport is currently connected.
     */
    isConnected(): boolean
}

// ============================================================================
// WebSocket Transport
// ============================================================================

/**
 * WebSocket-based transport with unified interface.
 * 
 * This transport handles:
 * - Initial handshaking (validate frontend/backend)
 * - Event loop (listening for messages)
 * - Connection lifecycle
 * 
 * Usage (same as Colab/Jupyter):
 *     const transport = new WebSocketTransport(comm_mgr_id, address)
 *     transport.setMessageCallback(routeMessage)
 *     await transport.connect()  // Performs handshake
 *     await transport.run()      // Runs until connection closes
 */
export class WebSocketTransport implements TransportBase {
    private websocket?: WebSocket
    private onMessageCallback?: (msg: any) => void
    // @ts-expect-error: not yet used
    private connected: boolean = false
    private initialized: boolean = false
  
    constructor(
        private comm_mgr: CommMgr,
        private address: [string, number]
    ) { }
    
    setMessageCallback(callback: (msg: any) => void): void {
        this.onMessageCallback = callback
    }
    
    async connect(): Promise<void> {
        const [host, port] = this.address
        const ws_address = `ws://${host}:${port}`
        
        return new Promise((resolve, reject) => {
            if (this.websocket !== undefined) {
                this.websocket.close()
            }
            
            this.websocket = new WebSocket(ws_address)
            this.websocket.binaryType = "arraybuffer"
            
            this.websocket.addEventListener("error", (e: Event) => {
                console.error('WebSocket error encountered:', e)
                reject(new Error('WebSocket connection failed'))
            })
            
            // Don't set onmessage here - that's for run()
            
            this.websocket.onopen = async () => {
                console.debug("WebSocket connected, performing handshake...")
                this.connected = true
                
                try {
                    await this.performHandshake()
                    console.log("WebSocket handshake complete")
                    resolve()
                } catch (e) {
                    console.error("WebSocket handshake failed:", e)
                    reject(e)
                }
            }
        })
    }
    
    /**
     * Perform WebSocket handshake.
     * 
     * Sends initialization message and waits for acknowledgment.
     */
    private async performHandshake(): Promise<void> {
        return new Promise((resolve, reject) => {
            // Get app context for IDs
            const appContext = this.getAppContext()
            let settled = false

            // Set up one-time listener for initialization response
            const initHandler = (event: MessageEvent) => {
                if (typeof event.data === 'string' || event.data instanceof String) {
                    try {
                        const data = deserialize(event.data as string)
                        
                        if (data.type === 'initialized') {
                            console.debug('WebSocket initialized:', data)
                            this.initialized = true
                            
                            // Remove this handler
                            if (this.websocket) {
                                this.websocket.removeEventListener('message', initHandler)
                            }
                            
                            // Show warnings if any
                            if (Array.isArray(data.warnings) && data.warnings.length > 0) {
                                for (const warning of data.warnings) {
                                    console.warn('Backend warning:', warning)
                                }
                            }
                            
                            settled = true
                            resolve()
                        } else if (data.type === 'warning') {
                            console.warn('Backend warning:', data.message)
                            // Don't resolve yet — wait for 'initialized'
                        }
                    } catch (e) {
                        console.error('Error parsing initialization response:', e)
                        if (this.websocket) {
                            this.websocket.removeEventListener('message', initHandler)
                        }
                        settled = true
                        reject(e)
                    }
                }
            }
            
            // Add handler
            if (this.websocket) {
                this.websocket.addEventListener('message', initHandler)
            }
            
            // Send initialization message
            this.send({
                id: 'initialize',
                direction: 'j2p',
                frontend_id: appContext?.frontend_id || null,
                backend_id: appContext?.backend_id || null,
                comm_mgr_id: this.comm_mgr.comm_mgr_id
            })
            
            // Timeout after 5 seconds
            // Remove the listener before rejecting so a late 'initialized'
            // message can't set this.initialized = true on a connection
            // that was already declared failed.
            setTimeout(() => {
                if (!settled) {
                    if (this.websocket) {
                        this.websocket.removeEventListener('message', initHandler)
                    }
                    reject(new Error('WebSocket handshake timeout'))
                }
            }, 5000)
        })
    }

    /**
     * Run the WebSocket event loop.
     * 
     * Listens for messages and calls the callback.
     * Blocks until connection closes.
     */
    async run(): Promise<void> {
        
        return new Promise((resolve, reject) => {
            if (!this.websocket) {
                reject(new Error("WebSocket not initialized"))
                return
            }
            
            // Set up message handler
            this.websocket.onmessage = (event: MessageEvent) => {
                if (typeof event.data === 'string' || event.data instanceof String) {
                    try {
                        const data = deserialize(event.data as string)
                        
                        if (this.onMessageCallback) {
                            this.onMessageCallback(data)
                        }
                    } catch (e) {
                        console.error("Error processing WebSocket message:", e)
                    }
                }
            }
            
            // Set up close handler
            this.websocket.onclose = (event: CloseEvent) => {
                console.debug(
                    `WebSocket closed: code=${event.code}, ` +
                    `reason=${event.reason || 'none'}, ` +
                    `clean=${event.wasClean}`
                )
                this.connected = false
                this.initialized = false
                resolve()
            }
            
            // Set up error handler
            this.websocket.onerror = (event: Event) => {
                console.error("WebSocket error:", event)
                // Don't reject here — let onclose handle it
            }
        })
    }

    private getAppContext(): any {
        return find.context(this.comm_mgr)
    }

    send(message: any): void {
        if (this.websocket && this.websocket.readyState === WebSocket.OPEN) {
            this.websocket.send(serialize(message))
        } else {
            console.warn("WebSocket not ready, message not sent:", message)
        }
    }
    
    close(): void {
        if (this.websocket) {
            this.websocket.close()
            this.websocket = undefined
            this.connected = false
            this.initialized = false
        }
    }
    
    isConnected(): boolean {
        return this.websocket !== undefined &&
               this.websocket.readyState === WebSocket.OPEN &&
               this.initialized
    }
}

// ============================================================================
// Jupyter and Colab Comms Transport
// ============================================================================

/**
 * Jupyter and Colab Comms transport created from Python with `anywidget`.
 */
export class CommsTransport implements TransportBase {
    private comm?: any
    private targetName: string
    private isOpen: boolean = false
    private onMessageCallback?: (msg: any) => void
    private shouldRun: boolean = true
    
    constructor(private comm_mgr: CommMgr) {
        this.targetName = `comm_${comm_mgr.comm_mgr_id}`
    }
    
    setMessageCallback(callback: (msg: any) => void): void {
        this.onMessageCallback = callback
    }
    
    private getAppContext(): any {
        return find.context(this.comm_mgr)
    }

    async connect(): Promise<void> {

        try {
            // Register handlers BEFORE opening the comm so we
            // never miss a fast reply from the Python kernel.
            // We create the comm object first, wire up handlers, then open it.
            this.comm = await this.retrieveComm()

            if (!this.comm) {
                throw new Error("Could not create Jupyter comm")
            }

            // handlers wired before any open/send call
            this.comm.onMsg = (msg: any) => {
                this.handleJupyterMessage(msg)
            }

            this.comm.onClose = (msg: any) => {
                this.handleCommClose(msg)
            }

//          // Now open the comm — this sends the comm_open message to the kernel
//          if (typeof this.comm.open === 'function') {
//              // @jupyter/services IComm (JupyterLab 4) and ipywidgets Comm both
//              // expose open(). Classic Notebook's new_comm() opens implicitly.
//              this.comm.open({
//                  comm_mgr_id: this.comm_mgr.comm_mgr_id,
//                  type: 'initialization',
//                  frontend_ready: true
//              })
//          }

            this.isOpen = true

            // Send explicit handshake so the backend knows we're ready
            const envelope = {
                type: 'cubevis_message',
                comm_mgr_id: this.comm_mgr.comm_mgr_id,
                data: serialize({
                    type: 'comm_opened',
                    comm_mgr_id: this.comm_mgr.comm_mgr_id,
                    frontend_ready: true
                })
            }

            console.group( '------------------------------connect------------------------------' )
            console.log( this.getAppContext( ) )
            console.groupEnd( )
            this.comm.send(envelope)

        } catch (e) {
            console.error("Error initializing Comms:", e)
            throw e
        }
    }
    
    async run(): Promise<void> {

        // Keep alive until shutdown
        while (this.shouldRun && this.isOpen) {
            await new Promise(resolve => setTimeout(resolve, 100))
        }

    }
    
    // --------------------------------------------------------------------------
    // Comm creation — three-path strategy for environment compatibility
    // --------------------------------------------------------------------------

    /**
     * Retrieve a Colab or Jupyter comm object injected with `anywidget` from Python.
     * Colab.
     *
     * Resolution order:
     *   1. See if injected comm is available directly (Jupyter Lab and Colab when
     *      retrieved from within the cell where the injection occurred)
     *   2. Colab open second comm using the same `comm_mgr_id` (Colab when retrieved
     *      from a cell that is not the cell where the injection occurred). Messages
     *      are delivered to all comms opened to the same identifier.
     */
    private async retrieveComm(): Promise<any> {
        const target_id = this.comm_mgr.comm_mgr_id
        const cachedComm = window["cubevis_" + target_id]?.comm
        console.log(`retrieveComm: target_id=${target_id} window keys=`,
          Object.keys(window).filter(k => k.startsWith('cubevis_')))

        if ( cachedComm ) {
            console.log(`CommsTransport.retrieveComm: retrieved comm for ${target_id}`, cachedComm)
            const el = window["cubevis_" + target_id].dbg_el
            if ( el ) {
                el.insertAdjacentHTML( 'beforeend',
                                       `<div style="padding:5px;background:#ccf">` +
                                         `✅ Comm Retrieved (${target_id})</div>` )
            }
            return cachedComm
        }

        const isColab = typeof google !== "undefined" && google?.colab?.kernel?.comms
        if (isColab) {

            // Colab isolates each cell output in its own iframe so window is not shared.
            // BroadcastChannel is same-origin and works across all Colab output iframes.
            //
            // The widget bridge ESM holds the single kernel comm and acts as relay:
            //
            //   JS -> Python:
            //     CommsTransport posts to bc_tx ("cubevis_tx_<id>")
            //     Widget bridge bc_tx.onmessage → channel.send() → Python _recv
            //
            //   Python -> JS:
            //     Python calls self._bridge.send(envelope) → anywidget model →
            //     model.on("msg:custom") in widget ESM → bc_rx.postMessage(envelope)
            //     CommsTransport bc_rx.onmessage → colabComm.onMsg → handleJupyterMessage
            //
            // Kernel comm.send() does NOT deliver to JS channel.messages in Colab
            // (confirmed by diagnostic testing). The anywidget model channel is the
            // only reliable Python->JS path in Colab.

            const bc_tx = new BroadcastChannel(`cubevis_tx_${target_id}`)
            const bc_rx = new BroadcastChannel(`cubevis_rx_${target_id}`)

            const colabComm: any = {
                onMsg:    null as any,
                onClose:  null as any,
                // JS->Python: post to tx bus; widget bridge relays to kernel
                send: (data: any) => { bc_tx.postMessage(data) },
                on_msg:   (cb: Function) => { colabComm.onMsg = cb },
                on_close: (cb: Function) => { colabComm.onClose = cb },
                close:    () => {
                    bc_tx.close()
                    bc_rx.close()
                    delete (window as any)[`cubevis_rx_cb_${target_id}`]
                }
            }

            // Python->JS delivery handler — called by either path:
            const onRx = (msg: any) => {
                if (colabComm.onMsg) {
                    colabComm.onMsg({ content: { data: msg }, buffers: [] })
                    // After each message, check if any deferred messages are now ready
                    // (binary tokens may have arrived since the envelope was deferred)
                    this.checkDeferredMessages()
                }
            }

            // Path 1: same-iframe — widget bridge calls window callback directly
            // (BroadcastChannel does NOT fire in the sender's own context)
            ;(window as any)[`cubevis_rx_cb_${target_id}`] = onRx
            // Called by bridge ESM after storing a binary token,
            // so deferred envelopes waiting for that token can be dispatched
            ;(window as any)[`cubevis_binary_arrived_${target_id}`] = () => {
                this.checkDeferredMessages()
            }

            // Path 2: cross-iframe — widget bridge posts on bc_rx
            bc_rx.onmessage = (event: MessageEvent) => onRx(event.data)

            return colabComm
        }

        return null
    }

    // --------------------------------------------------------------------------
    // Message handling
    // --------------------------------------------------------------------------

    /** Recursively substitute __binary__ tokens with typed arrays from window._cubevis_bin_* */
    private substituteBinary(obj: any): any {
        // Never recurse into typed arrays or ArrayBuffers - pass them through unchanged
        if (obj instanceof Uint8Array || obj instanceof Float32Array ||
            obj instanceof Float64Array || obj instanceof Int32Array ||
            obj instanceof Uint16Array || obj instanceof Int16Array ||
            obj instanceof ArrayBuffer) {
            return obj
        }
        if (obj && typeof obj === 'object' && '__binary__' in obj) {
            const token = obj['__binary__']
            const stored = (window as any)[`_cubevis_bin_${token}`]
            if (stored) {
                delete (window as any)[`_cubevis_bin_${token}`]
                return stored
            }
            console.warn("CUBEVIS: missing binary token:", token)
            return obj
        }
        if (Array.isArray(obj)) return obj.map((v: any) => this.substituteBinary(v))
        if (obj && typeof obj === 'object') {
            // Only recurse into plain objects — leave class instances (Bokeh models,
            // typed arrays, etc.) completely untouched to preserve their prototypes
            // and methods (e.g. .get() on Bokeh models).
            if (ArrayBuffer.isView(obj)) return obj
            if (Object.getPrototypeOf(obj) !== Object.prototype) return obj
            const out: any = {}
            for (const k of Object.keys(obj)) out[k] = this.substituteBinary(obj[k])
            return out
        }
        return obj
    }

    /** Check if all binary tokens for a deferred message have arrived, and if so dispatch it. */
    private checkDeferredMessages(): void {
        const deferred: any[] = (window as any)['_cubevis_deferred_msgs'] || []
        const remaining: any[] = []
        for (const item of deferred) {
            const tokens: string[] = item.tokens
            const allReady = tokens.every(t => (window as any)[`_cubevis_bin_${t}`] !== undefined)
            if (allReady) {
                this.dispatchMessage(item.msg)
            } else {
                remaining.push(item)
            }
        }
        ;(window as any)['_cubevis_deferred_msgs'] = remaining
    }

    private dispatchMessage(msg: any): void {
        try {
            const content = msg.content || {}
            const dataWrapper = content.data || {}
            const serializedData = dataWrapper.data

            let data
            if (serializedData && typeof serializedData === 'string') {
                data = deserialize(serializedData)
            } else if (dataWrapper.type) {
                data = dataWrapper
            } else {
                return
            }

            if (data.type === 'ping' || data.type === 'heartbeat' ||
                data.type === 'comm_opened' || data.type === 'closing') {
                return
            }



            data = this.substituteBinary(data)

            if (this.onMessageCallback) {
                this.onMessageCallback(data)
            }
        } catch (e) {
            console.error("Error dispatching message:", e)
        }
    }

    private handleJupyterMessage(msg: any): void {
        try {
            const content = msg.content || {}
            const dataWrapper = content.data || {}
            const tokens: string[] = dataWrapper.pending_binary_tokens || []

            if (tokens.length > 0) {
                // Some binary arrays not yet arrived - defer until all tokens present
                const allReady = tokens.every(t => (window as any)[`_cubevis_bin_${t}`] !== undefined)
                if (!allReady) {
                    if (!(window as any)['_cubevis_deferred_msgs']) {
                        (window as any)['_cubevis_deferred_msgs'] = []
                    }
                    ;(window as any)['_cubevis_deferred_msgs'].push({ msg, tokens })
                    return
                }
            }

            this.dispatchMessage(msg)

        } catch (e) {
            console.error("Error handling Jupyter comm message:", e)
        }
    }

    private handleCommClose(_msg: any): void {
        console.debug(`Jupyter comm closed for ${this.targetName}`)
        this.isOpen = false
        this.shouldRun = false
    }

    // --------------------------------------------------------------------------
    // Public send / close / isConnected
    // --------------------------------------------------------------------------

    send(message: any): void {
        if (!this.comm || !this.isOpen) {
            console.warn("Jupyter Comm not initialized, message not sent:", message)
            return
        }

        try {
            this.comm.send({
                type: 'cubevis_message',
                comm_mgr_id: this.comm_mgr.comm_mgr_id,
                data: serialize(message)
            })
        } catch (e) {
            console.error("Error sending message via Jupyter Comm:", e)
        }
    }

    close(): void {
        this.shouldRun = false

        if (this.comm && this.isOpen) {
            try {
                this.comm.send({
                    type: 'cubevis_message',
                    comm_mgr_id: this.comm_mgr.comm_mgr_id,
                    data: serialize({
                        type: 'closing',
                        comm_mgr_id: this.comm_mgr.comm_mgr_id
                    })
                })

                this.comm.close()
                console.debug(`Closed Jupyter comm for ${this.targetName}`)

            } catch (e) {
                console.error("Error closing Jupyter comm:", e)
            } finally {
                this.comm = undefined
                this.isOpen = false
            }
        }
    }

    isConnected(): boolean {
        return this.comm !== undefined && this.isOpen
    }
}
