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
        console.debug(`Message callback set for WebSocket ${this.comm_mgr.comm_mgr_id}`)
    }
    
    async connect(): Promise<void> {
        const [host, port] = this.address
        const ws_address = `ws://${host}:${port}`
        console.log("WebSocket connecting to:", ws_address)
        
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
                console.log("WebSocket connected, performing handshake...")
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
                            console.log('WebSocket initialized:', data)
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
        console.log(`WebSocket event loop starting for ${this.comm_mgr.comm_mgr_id}`)
        
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
                console.log(
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
    
    async connect(): Promise<void> {
        console.log("Comms connecting for comm_mgr:", this.comm_mgr.comm_mgr_id)
        
        try {
            // Register handlers BEFORE opening the comm so we
            // never miss a fast reply from the Python kernel.
            // We create the comm object first, wire up handlers, then open it.
            this.comm = await this.retrieveComm()
            console.log("CommsTransport.connect:", this.comm)

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
            console.log(`Jupyter comm opened: ${this.targetName}`)

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

            console.log(`CommsTransport.connect: sending handshake type=${envelope.type} comm_mgr_id=${envelope.comm_mgr_id}`)
            this.comm.send(envelope)

        } catch (e) {
            console.error("Error initializing Comms:", e)
            throw e
        }
    }
    
    async run(): Promise<void> {
        console.log(`Comms event loop starting for ${this.comm_mgr.comm_mgr_id}`)
        console.log(new Error().stack)

        // Keep alive until shutdown
        while (this.shouldRun && this.isOpen) {
            await new Promise(resolve => setTimeout(resolve, 100))
        }

        console.log(`Comms event loop ended for ${this.comm_mgr.comm_mgr_id}`)
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

        console.log(`CommsTransport.retrieveComm: starting for ${target_id}`, cachedComm)
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
        console.log(`CommsTransport.retrieveComm: colab for ${target_id}:`,  isColab )
        if (isColab) {
            console.log(`[Colab] Opening channels for ${target_id}`)

            // Colab isolates each cell output in its own iframe, so window is not
            // shared across cells. The kernel comm API IS shared (it goes through
            // the kernel websocket), so all cross-iframe communication must flow
            // through kernel comms.
            //
            // We use TWO channels to avoid the Colab routing ambiguity where only
            // the first channel opened per target reliably receives Python->JS:
            //
            //   channel_js2py  (target_id):         JS -> Python only
            //     JS calls channel_js2py.send(data)
            //     Python _recv fires on the matching comm
            //
            //   channel_py2js  (target_id + "_reply"): Python -> JS only
            //     Python calls reply_comm.send(envelope)
            //     Colab routes it to channel_py2js.messages here
            //     The pump calls colabComm.onMsg with the payload
            //
            // Python registers both targets. The widget bridge uses its own
            // channel on target_id for its own JS->Python traffic; that channel
            // is separate and does not interfere with channel_py2js here.

            const reply_target = target_id + "_reply"

            const colabComm: any = {
                _js2py: null as any,   // channel for JS->Python sends
                _py2js: null as any,   // channel for Python->JS receives
                onMsg: null as any,
                onClose: null as any,
                send: (data: any) => {
                    if (colabComm._js2py) {
                        colabComm._js2py.send(data)
                    } else {
                        console.error("[Colab] send() called before js2py channel was ready")
                    }
                },
                on_msg: (cb: Function) => { colabComm.onMsg = cb },
                on_close: (cb: Function) => { colabComm.onClose = cb },
                close: () => {
                    colabComm._js2py?.close()
                    colabComm._py2js?.close()
                }
            }

            // Open js2py channel: this triggers _on_comm_open on Python,
            // which wires _recv so Python can receive JS->Python messages.
            try {
                colabComm._js2py = await google.colab.kernel.comms.open(target_id, {})
                console.log("[Colab] js2py channel open")
            } catch (e) {
                console.error("[Colab] Failed to open js2py channel:", e)
                throw e
            }

            // Open py2js channel: Python registers this target and uses it
            // exclusively for sending replies back to this iframe.
            // We await this too so the pump is running before connect() sends
            // the handshake — preventing Python's reply from arriving before
            // the iterator is consuming channel.messages.
            try {
                colabComm._py2js = await google.colab.kernel.comms.open(reply_target, {})
                console.log("[Colab] py2js channel open, starting pump")

                // Fire-and-forget pump. The async IIFE body runs up to its first
                // await synchronously (establishing the iterator), then suspends.
                // This means the iterator is established before retrieveComm()
                // returns and before connect() assigns onMsg, so no messages
                // can be missed.
                ;(async () => {
                    console.log(`%c[Colab] Pump Starting on ${reply_target}`, "color: #4285f4; font-weight: bold")
                    try {
                        for await (const message of colabComm._py2js.messages) {
                            console.log("%c[Colab] Inbound:", "color: #34a853", message.data)
                            if (colabComm.onMsg) {
                                colabComm.onMsg({
                                    content: { data: message.data },
                                    buffers: message.buffers || []
                                })
                            }
                        }
                    } catch (e) {
                        console.error("[Colab] Pump loop failed:", e)
                    }
                    console.log("[Colab] Pump loop ended")
                })()

            } catch (e) {
                console.error("[Colab] Failed to open py2js channel:", e)
                throw e
            }

            return colabComm
        }

        return null
    }

    // --------------------------------------------------------------------------
    // Message handling
    // --------------------------------------------------------------------------

    private handleJupyterMessage(msg: any): void {
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

            // Handle special messages
            if (data.type === 'ping' || data.type === 'heartbeat' ||
                data.type === 'comm_opened' || data.type === 'closing') {
                return
            }

            if (this.onMessageCallback) {
                this.onMessageCallback(data)
            }

        } catch (e) {
            console.error("Error handling Jupyter comm message:", e)
        }
    }

    private handleCommClose(_msg: any): void {
        console.log(`Jupyter comm closed for ${this.targetName}`)
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
                console.log(`Closed Jupyter comm for ${this.targetName}`)
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
