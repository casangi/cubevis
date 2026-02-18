/**
 * Transport Implementations for TypeScript
 * 
 * These are imported and used by CommMgr in comm_bokeh_models.ts
 * They handle the actual communication with Python backend.
 */
import { CommMgr } from "./comm_mgr"
import { serialize, deserialize } from "../util/conversions"
import *  as find from "../util/find"

/**
 * Global window extensions for Colab and Jupyter
 */
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
 * This transport now handles:
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
    private shouldRun: boolean = true
  
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
            
            this.websocket.onclose = () => {
                console.log("WebSocket closed")
                this.connected = false
                this.initialized = false
                this.shouldRun = false
            }
            
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
                            
                            resolve()
                        } else if (data.type === 'warning') {
                            console.warn('Backend warning:', data.message)
                            // Don't resolve yet - wait for initialized
                        }
                    } catch (e) {
                        console.error('Error parsing initialization response:', e)
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
            setTimeout(() => {
                if (!this.initialized) {
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
        if (!this.initialized) {
            throw new Error("Must call connect() before run()")
        }
        
        if (!this.onMessageCallback) {
            throw new Error("Must call setMessageCallback() before run()")
        }
        
        console.log(`WebSocket event loop starting for ${this.comm_mgr.comm_mgr_id}`)
        
        return new Promise((resolve) => {
            if (!this.websocket) {
                resolve()
                return
            }
            
            // Set up message handler
            this.websocket.onmessage = (event: MessageEvent) => {
                if (typeof event.data === 'string' || event.data instanceof String) {
                    try {
                        const data = deserialize(event.data as string)
                        
                        // Call the callback (set by CommMgr)
                        if (this.onMessageCallback) {
                            this.onMessageCallback(data)
                        }
                    } catch (e) {
                        console.error("Error processing WebSocket message:", e)
                    }
                } else {
                    console.log("WebSocket received binary data", event.data.byteLength, "bytes")
                }
            }
            
            // Set up close handler
            this.websocket.onclose = () => {
                console.log(`WebSocket event loop ended for ${this.comm_mgr.comm_mgr_id}`)
                this.connected = false
                this.initialized = false
                this.shouldRun = false
                resolve()
            }
            
            // Also resolve if shouldRun becomes false
            const checkInterval = setInterval(() => {
                if (!this.shouldRun) {
                    clearInterval(checkInterval)
                    resolve()
                }
            }, 100)
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
        this.shouldRun = false
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
// Colab Comms Transport
// ============================================================================

/**
 * Colab Comms-based transport for Google Colab environment.
 * 
 * Uses Colab's native comm protocol for efficient bidirectional communication.
 * Handles large data like images and arrays efficiently.
 * 
 * Key features:
 * - Native Colab comm protocol (not eval_js)
 * - Efficient large data transfer
 * - Automatic Bokeh serialization support
 * - Bidirectional message passing
 */
export class ColabCommsTransport implements TransportBase {
    private comm?: any
    private targetName: string
    private registered: boolean = false
    private onMessageCallback?: (msg: any) => void
    private shouldRun: boolean = true
    
    constructor(private comm_mgr: CommMgr) {
        this.targetName = `cubevis_comm_mgr_${comm_mgr.comm_mgr_id}`
    }
    
    setMessageCallback(callback: (msg: any) => void): void {
        this.onMessageCallback = callback
    }
    
    async connect(): Promise<void> {
        console.log("Colab Comms connecting for comm_mgr:", this.comm_mgr.comm_mgr_id)
        
        // Verify Colab environment
        if (!window.google || !window.google.colab || !window.google.colab.kernel) {
            throw new Error("Colab environment not detected")
        }
        
        const kernel = window.google.colab.kernel
        if (!kernel.comms) {
            throw new Error("Colab kernel.comms not available")
        }
        
        try {
            // Register comm target to receive comms from backend
            kernel.comms.registerTarget(
                this.targetName,
                (comm: any, openMsg: any) => {
                    this.handleCommOpen(comm, openMsg)
                }
            )
            
            console.log(`Registered Colab comm target: ${this.targetName}`)
            
            // Open a comm to the backend
            this.comm = await kernel.comms.open(
                this.targetName,
                {
                    data: serialize({
                        type: 'initialization',
                        comm_mgr_id: this.comm_mgr.comm_mgr_id,
                        frontend_ready: true
                    })
                }
            )
            
            if (!this.comm) {
                throw new Error(`Failed to open Colab comm to ${this.targetName}`)
            }
            
            // Register message handler
            this.comm.onMsg = (msg: any) => {
                this.handleMessage(msg)
            }
            
            // Register close handler
            this.comm.onClose = (msg: any) => {
                this.handleClose(msg)
            }
            
            this.registered = true
            
            console.log(`Colab comm opened: ${this.targetName}`)
            
        } catch (e) {
            console.error("Error initializing Colab Comms:", e)
            throw e
        }
    }
    
    async run(): Promise<void> {
        console.log(`Colab Comms event loop starting for ${this.comm_mgr.comm_mgr_id}`)
        
        // Keep alive until shutdown
        while (this.shouldRun && this.registered) {
            await new Promise(resolve => setTimeout(resolve, 100))
        }
        
        console.log(`Colab Comms event loop ended for ${this.comm_mgr.comm_mgr_id}`)
    }
    
    private handleCommOpen(comm: any, _openMsg: any): void {
        console.log(`Backend opened Colab comm to ${this.targetName}`)
        
        if (this.comm && this.comm !== comm) {
            console.log("Replacing existing comm with new one from backend")
            try {
                this.comm.close()
            } catch (e) {
                console.warn("Error closing old comm:", e)
            }
        }
        
        this.comm = comm
        this.registered = true
        
        this.comm.onMsg = (msg: any) => {
            this.handleMessage(msg)
        }
        
        this.comm.onClose = (msg: any) => {
            this.handleClose(msg)
        }
        
        this.comm.send({
            data: serialize({
                type: 'comm_opened',
                comm_mgr_id: this.comm_mgr.comm_mgr_id,
                frontend_ready: true
            })
        })
    }
    
    private handleMessage(msg: any): void {
        try {
            const msgData = msg.data || msg
            
            let data
            if (typeof msgData === 'string') {
                data = deserialize(msgData)
            } else {
                data = msgData
            }
            
            console.debug("Received Colab comm message:", data.type || typeof data)
            
            // Handle special message types
            if (typeof data === 'object' && data !== null) {
                const msgType = data.type
                
                if (msgType === 'ping') {
                    if (this.comm) {
                        this.comm.send({
                            data: serialize({
                                type: 'pong',
                                comm_mgr_id: this.comm_mgr.comm_mgr_id,
                                timestamp: Date.now()
                            })
                        })
                    }
                    return
                }
                
                if (msgType === 'heartbeat') {
                    console.debug(`Heartbeat received for ${this.comm_mgr.comm_mgr_id}`)
                    return
                }
                
                if (msgType === 'comm_opened' || msgType === 'closing') {
                    console.log(`Backend message: ${msgType}`)
                    return
                }
            }
            
            // Call the message callback
            if (this.onMessageCallback) {
                this.onMessageCallback(data)
            }
            
        } catch (e) {
            console.error("Error handling Colab comm message:", e)
        }
    }
    
    private handleClose(_msg: any): void {
        console.log(`Colab comm closed for ${this.targetName}`)
        this.registered = false
        this.shouldRun = false
        this.comm = undefined
    }
    
    send(message: any): void {
        if (!this.registered || !this.comm) {
            console.warn("Colab Comm not initialized, message not sent:", message)
            return
        }
        
        try {
            const serialized = serialize(message)
            this.comm.send({ data: serialized })
            console.debug(`Sent message via Colab comm ${this.targetName}`)
        } catch (e) {
            console.error("Error sending message via Colab Comm:", e)
        }
    }
    
    close(): void {
        this.shouldRun = false
        if (this.comm && this.registered) {
            try {
                this.comm.send({
                    data: serialize({
                        type: 'closing',
                        comm_mgr_id: this.comm_mgr.comm_mgr_id,
                        message: 'Frontend closing comm'
                    })
                })
                
                this.comm.close()
                console.log(`Closed Colab comm for ${this.targetName}`)
            } catch (e) {
                console.error("Error closing Colab comm:", e)
            } finally {
                this.comm = undefined
                this.registered = false
            }
        }
    }
    
    isConnected(): boolean {
        return this.registered && this.comm !== undefined
    }
}

// ============================================================================
// Jupyter Comms Transport
// ============================================================================

/**
 * Jupyter Comms transport for remote kernel execution.
 * 
 * Enables connection to Jupyter kernels for persistent, reconnectable sessions.
 * 
 * Key features:
 * - Connect to remote Jupyter kernels
 * - Session persistence across browser sessions
 * - Reconnection support
 * - Multi-client kernel access
 * - Efficient data transfer via Bokeh serialization
 * 
 * Requires @jupyter-widgets/base package:
 *     npm install @jupyter-widgets/base
 */
export class JupyterCommsTransport implements TransportBase {
    private comm?: any
    private commManager?: any
    private targetName: string
    private isOpen: boolean = false
    private onMessageCallback?: (msg: any) => void
    private heartbeatInterval?: number
    private shouldRun: boolean = true
    
    constructor(private comm_mgr: CommMgr) {
        this.targetName = `cubevis_comm_mgr_${comm_mgr.comm_mgr_id}`
    }
    
    setMessageCallback(callback: (msg: any) => void): void {
        this.onMessageCallback = callback
    }
    
    async connect(): Promise<void> {
        console.log("Jupyter Comms connecting for comm_mgr:", this.comm_mgr.comm_mgr_id)
        
        try {
            // Load Jupyter widgets
            const widgets = await this.loadJupyterWidgets()
            
            if (!widgets) {
                throw new Error("Could not load @jupyter-widgets/base")
            }
            
            // Get comm manager
            this.commManager = await this.getCommManager(widgets)
            
            if (!this.commManager) {
                throw new Error("Could not get Jupyter comm manager")
            }
            
            // Open comm to kernel
            this.comm = this.commManager.new_comm(
                this.targetName,
                {
                    comm_mgr_id: this.comm_mgr.comm_mgr_id,
                    type: 'initialization',
                    frontend_ready: true
                }
            )
            
            // Register handlers
            this.comm.on_msg((msg: any) => {
                this.handleJupyterMessage(msg)
            })
            
            this.comm.on_close((msg: any) => {
                this.handleCommClose(msg)
            })
            
            this.isOpen = true
            
            console.log(`Jupyter comm opened: ${this.targetName}`)
            
            // Send handshake
            this.comm.send({
                type: 'cubevis_message',
                comm_mgr_id: this.comm_mgr.comm_mgr_id,
                data: serialize({
                    type: 'comm_opened',
                    comm_mgr_id: this.comm_mgr.comm_mgr_id,
                    frontend_ready: true
                })
            })
            
            // Start heartbeat
            this.startHeartbeat()
            
        } catch (e) {
            console.error("Error initializing Jupyter Comms:", e)
            throw e
        }
    }
    
    async run(): Promise<void> {
        console.log(`Jupyter Comms event loop starting for ${this.comm_mgr.comm_mgr_id}`)
        
        // Keep alive until shutdown
        while (this.shouldRun && this.isOpen) {
            await new Promise(resolve => setTimeout(resolve, 100))
        }
        
        console.log(`Jupyter Comms event loop ended for ${this.comm_mgr.comm_mgr_id}`)
    }
    
    private async loadJupyterWidgets(): Promise<any> {
        // Try RequireJS
        if (typeof (window as any).require !== 'undefined') {
            try {
                const widgets = await new Promise((resolve, reject) => {
                    (window as any).require(
                        ['@jupyter-widgets/base'],
                        (base: any) => resolve(base),
                        (err: any) => reject(err)
                    )
                })
                console.log("Loaded @jupyter-widgets/base via RequireJS")
                return widgets
            } catch (e) {
                console.debug("RequireJS failed:", e)
            }
        }
        
        // Try global
        if ((window as any).jupyter?.widgets?.base) {
            console.log("Found @jupyter-widgets/base in global scope")
            return (window as any).jupyter.widgets.base
        }
        
        // Try dynamic import
        try {
            const dynamicImport = new Function('specifier', 'return import(specifier)')
            const widgets = await dynamicImport('@jupyter-widgets/base')
            console.log("Loaded @jupyter-widgets/base via dynamic import")
            return widgets
        } catch (e) {
            console.debug("Dynamic import failed:", e)
        }
        
        return null
    }
    
    private async getCommManager(widgets: any): Promise<any> {
        if (widgets.CommManager) {
            try {
                return widgets.CommManager.get_comm_manager()
            } catch (e) {
                console.debug("CommManager.get_comm_manager failed:", e)
            }
        }
        
        if ((window as any).Jupyter?.notebook?.kernel?.comm_manager) {
            return (window as any).Jupyter.notebook.kernel.comm_manager
        }
        
        if ((window as any).kernel?.comm_manager) {
            return (window as any).kernel.comm_manager
        }
        
        throw new Error("Could not find Jupyter comm manager")
    }
    
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
            
            // Call callback
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
        this.stopHeartbeat()
    }
    
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
        this.stopHeartbeat()
        
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
    
    private startHeartbeat(intervalMs: number = 30000): void {
        if (this.heartbeatInterval) {
            clearInterval(this.heartbeatInterval)
        }
        
        this.heartbeatInterval = window.setInterval(() => {
            if (this.isConnected()) {
                this.send({
                    type: 'heartbeat',
                    comm_mgr_id: this.comm_mgr.comm_mgr_id,
                    timestamp: Date.now()
                })
            }
        }, intervalMs)
    }
    
    private stopHeartbeat(): void {
        if (this.heartbeatInterval) {
            clearInterval(this.heartbeatInterval)
            this.heartbeatInterval = undefined
        }
    }
}
