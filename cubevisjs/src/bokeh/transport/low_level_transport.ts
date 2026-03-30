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
/**
 * TypeScript implementation for Google Colab communication.
 * This acts as the frontend counterpart to the Python ColabCommsTransport.
 */
/**
 * TypeScript implementation for Google Colab communication.
 * This acts as the frontend counterpart to the Python ColabCommsTransport.
 */
export class ColabCommsTransport implements TransportBase {
    private comm?: any;
    private targetName: string;
    private isRegistered: boolean = false;
    private onMessageCallback?: (msg: any) => void;
    private shouldRun: boolean = false;

    constructor(private comm_mgr: CommMgr) {
        // This ID must match the string passed to the Python register_target
        this.targetName = this.comm_mgr.comm_mgr_id;
    }

    setMessageCallback(callback: (msg: any) => void): void {
        this.onMessageCallback = callback;
    }

    /**
     * Satisfies the TransportBase interface requirement.
     */
    isConnected(): boolean {
        return this.isRegistered && this.comm !== undefined;
    }

    async connect(): Promise<void> {
        console.log(`Connecting to Colab Comm target: ${this.targetName}`);

        // Verify Colab environment
        if (!window.google?.colab?.kernel) {
            throw new Error("Colab environment or kernel not detected.");
        }

        const kernel = window.google.colab.kernel;

        if (!kernel.comms) {
            throw new Error("Colab kernel.comms is not available in this environment.");
        }

        try {
            // Establishes the channel back to the Python backend
            this.comm = await kernel.comms.open(this.targetName, {
                comm_mgr_id: this.comm_mgr.comm_mgr_id,
                origin: 'frontend'
            });
            
            if (!this.comm) {
                throw new Error(`Failed to establish channel for ${this.targetName}`);
            }

            this.comm.onMsg = (msg: any) => this.handleIncoming(msg);
            this.comm.onClose = () => this.handleClose();

            this.isRegistered = true;
            console.log("Colab Comm connection established.");

        } catch (error) {
            console.error("Colab Comm connection failed:", error);
            throw error;
        }
    }

    private handleIncoming(msg: any): void {
        // Colab usually nests user data in a 'data' property
        const data = msg.data || msg;
        if (this.onMessageCallback) {
            this.onMessageCallback(data);
        }
    }

    private handleClose(): void {
        console.warn("Colab Comm channel closed.");
        this.isRegistered = false;
        this.shouldRun = false;
        this.comm = undefined;
    }

    send(message: any): void {
        if (this.isConnected()) {
            this.comm.send(message);
        } else {
            console.warn("Colab Comm not connected; message dropped.");
        }
    }

    async run(): Promise<void> {
        this.shouldRun = true;
        while (this.shouldRun && this.isRegistered) {
            await new Promise(resolve => setTimeout(resolve, 1000));
        }
    }

    async close(): Promise<void> {
        this.shouldRun = false;
        if (this.comm) {
            this.comm.close();
        }
        this.isRegistered = false;
        this.comm = undefined;
    }
}

// ============================================================================
// Jupyter Comms Transport
// ============================================================================

/**
 * Jupyter Comms transport for Classic Notebook and JupyterLab.
 * 
 * Enables connection to Jupyter kernels for persistent, reconnectable sessions.
 * 
 * Key features:
 * - Works with Classic Notebook 6.x AND JupyterLab 3.x / 4.x
 * - Session persistence across browser sessions
 * - Reconnection support
 * - Multi-client kernel access
 * - Efficient data transfer via Bokeh serialization
 * 
 * Environment support matrix:
 *   Classic Notebook 6  — comm via Jupyter.notebook.kernel.comm_manager.new_comm()
 *   JupyterLab 3        — comm via @jupyter-widgets/base CommManager.new_comm()
 *   JupyterLab 4        — comm via kernel.createComm() (@jupyter/services kernel)
 */
export class JupyterCommsTransport implements TransportBase {
    private comm?: any
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
            // Register handlers BEFORE opening the comm so we
            // never miss a fast reply from the Python kernel.
            // We create the comm object first, wire up handlers, then open it.
            this.comm = await this.createComm()

            if (!this.comm) {
                throw new Error("Could not create Jupyter comm")
            }

            // handlers wired before any open/send call
            this.comm.on_msg((msg: any) => {
                this.handleJupyterMessage(msg)
            })

            this.comm.on_close((msg: any) => {
                this.handleCommClose(msg)
            })

            // Now open the comm — this sends the comm_open message to the kernel
            if (typeof this.comm.open === 'function') {
                // @jupyter/services IComm (JupyterLab 4) and ipywidgets Comm both
                // expose open(). Classic Notebook's new_comm() opens implicitly.
                this.comm.open({
                    comm_mgr_id: this.comm_mgr.comm_mgr_id,
                    type: 'initialization',
                    frontend_ready: true
                })
            }

            this.isOpen = true
            console.log(`Jupyter comm opened: ${this.targetName}`)

            // Send explicit handshake so the backend knows we're ready
            this.comm.send({
                type: 'cubevis_message',
                comm_mgr_id: this.comm_mgr.comm_mgr_id,
                data: serialize({
                    type: 'comm_opened',
                    comm_mgr_id: this.comm_mgr.comm_mgr_id,
                    frontend_ready: true
                })
            })

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
    
    // --------------------------------------------------------------------------
    // Comm creation — three-path strategy for environment compatibility
    // --------------------------------------------------------------------------

    /**
     * Create a Jupyter comm object using the best available API for the
     * current environment.
     *
     * Resolution order:
     *   1. Classic Notebook 6 / JupyterLab 3 — commManager.new_comm()
     *   2. JupyterLab 4                       — kernel.createComm()
     *   3. ipywidgets Comm constructor         — last-resort fallback
     */
    private async createComm(): Promise<any> {
        console.group('JupyterCommsTransport.createComm')
        // Path 1: Classic Notebook 6 / JupyterLab 3 via @jupyter-widgets/base
        try {
            const commManager = await this.findCommManager()
            console.log( 'Path 1', commManager )
            if (commManager && typeof commManager.new_comm === 'function') {
                // new_comm() on these managers creates but does NOT immediately
                // send the comm_open — we call open() ourselves after wiring handlers.
                const comm = commManager.new_comm(this.targetName, {})
                console.log("Created comm via commManager.new_comm() (Classic/JupyterLab 3)")
                return comm
            }
        } catch (e) {
            console.debug("commManager.new_comm path failed:", e)
        }

        // Path 2: JupyterLab 4 — kernel object exposed by the lab extension system
        // JupyterLab 4 removed CommManager from @jupyter-widgets/base.
        // The live kernel is available via jupyterapp.serviceManager or the
        // global `kernel` object injected by the notebook widget.
        try {
            const kernel = await this.findJupyterLabKernel()
            console.log( 'Path 2', kernel )
            if (kernel && typeof kernel.createComm === 'function') {
                const comm = kernel.createComm(this.targetName)
                console.log("Created comm via kernel.createComm() (JupyterLab 4)")
                return comm
            }
        } catch (e) {
            console.debug("kernel.createComm path failed:", e)
        }

        // Path 3: ipywidgets Comm constructor loaded via RequireJS (fallback)
        try {
            const WidgetsComm = await this.loadWidgetsComm()
            console.log( 'Path 3', WidgetsComm )
            if (WidgetsComm) {
                const comm = new WidgetsComm({ target_name: this.targetName })
                console.log("Created comm via ipywidgets Comm constructor (fallback)")
                return comm
            }
        } catch (e) {
            console.debug("ipywidgets Comm fallback failed:", e)
        }

        console.log( 'Failure', window )
        console.groupEnd( )
        throw new Error(
            `Could not create a Jupyter comm for target '${this.targetName}'. ` +
            "Ensure @jupyter-widgets/base or @jupyter/services is available."
        )
    }


    /**
     * Find the comm manager for Classic Notebook 6 / JupyterLab 3.
     */
    private async findCommManager(): Promise<any> {
        // Classic Notebook 6
        if ((window as any).Jupyter?.notebook?.kernel?.comm_manager) {
            console.debug("Found comm_manager via window.Jupyter (Classic Notebook)")
            return (window as any).Jupyter.notebook.kernel.comm_manager
        }

        // Some JupyterLab 3 builds expose the kernel on window.kernel
        if ((window as any).kernel?.comm_manager) {
            console.debug("Found comm_manager via window.kernel")
            return (window as any).kernel.comm_manager
        }

        // JupyterLab 3 via @jupyter-widgets/base CommManager
        // Try RequireJS (present in JupyterLab 3).
        if (typeof (window as any).require !== 'undefined') {
            try {
                const widgets: any = await new Promise((resolve, reject) => {
                    (window as any).require(
                        ['@jupyter-widgets/base'],
                        (base: any) => resolve(base),
                        (err: any) => reject(err)
                    )
                })
                if (widgets?.CommManager) {
                    const mgr = widgets.CommManager.get_comm_manager?.()
                    if (mgr) {
                        console.debug("Found comm_manager via RequireJS @jupyter-widgets/base")
                        return mgr
                    }
                }
            } catch (e) {
                console.debug("RequireJS @jupyter-widgets/base failed:", e)
            }
        }

        return null
    }

    /**
     * Find the live kernel object in JupyterLab 4.
     *
     * JupyterLab 4 exposes the kernel through the application's
     * service manager rather than through @jupyter-widgets/base.
     */
    private async findJupyterLabKernel(): Promise<any> {
        // JupyterLab 4: window.jupyterapp is the JupyterFrontEnd application
        const jupyterapp = (window as any).jupyterapp
        if (jupyterapp) {
            // Active session's kernel via the sessions manager
            try {
                const sessions = jupyterapp.serviceManager?.sessions
                if (sessions) {
                    const running = [...sessions.running()]
                    if (running.length > 0) {
                        const connection = await sessions.connectTo({ model: running[0] })
                        const kernel = connection?.kernel
                        if (kernel && typeof kernel.createComm === 'function') {
                            console.debug("Found kernel via jupyterapp.serviceManager.sessions")
                            return kernel
                        }
                    }
                }
            } catch (e) {
                console.debug("jupyterapp.serviceManager path failed:", e)
            }
        }

        // JupyterLab 4 also often exposes the kernel directly on window
        if ((window as any).kernel?.createComm) {
            console.debug("Found kernel via window.kernel.createComm")
            return (window as any).kernel
        }

        // IPython kernel shim (some JupyterLab 3 / extension-provided kernels)
        if ((window as any).IPython?.kernel?.createComm) {
            console.debug("Found kernel via window.IPython.kernel")
            return (window as any).IPython.kernel
        }

        return null
    }
    

    /**
     * Load the ipywidgets Comm constructor as a last-resort fallback.
     */
    private async loadWidgetsComm(): Promise<any> {
        if (typeof (window as any).require !== 'undefined') {
            try {
                const widgets: any = await new Promise((resolve, reject) => {
                    (window as any).require(
                        ['@jupyter-widgets/base'],
                        (base: any) => resolve(base),
                        (err: any) => reject(err)
                    )
                })
                if (widgets?.Comm) {
                    return widgets.Comm
                }
            } catch (e) {
                console.debug("loadWidgetsComm RequireJS failed:", e)
            }
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
        this.stopHeartbeat()
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

    // --------------------------------------------------------------------------
    // Heartbeat
    // --------------------------------------------------------------------------

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
