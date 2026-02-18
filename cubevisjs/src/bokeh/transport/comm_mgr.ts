/**
 * TypeScript implementation for Comm and CommMgr Bokeh models
 * 
 * These are the __implementation__ TypeScript code for the Python models.
 * They provide full communications functionality in the browser.
 */

// ============================================================================
// CommMgr TypeScript Implementation
// ============================================================================

import {Model} from "@bokehjs/model"
import * as p from "@bokehjs/core/properties"

// Import transport implementations
import {TransportBase, WebSocketTransport, ColabCommsTransport, JupyterCommsTransport} from "./low_level_transport"

// Enums
enum AppState {
    INITIALIZING = "initializing",
    RUNNING = "running",
    SHUTTING_DOWN = "shutting_down",
    STOPPED = "stopped",
    ERROR = "error"
}

/**
 * Handler context passed to message callbacks
 */
class HandlerContext {
    constructor(private commMgr: CommMgr) {}
    
    requestShutdown(reason: string = "Handler requested shutdown"): void {
        this.commMgr.requestShutdown(reason)
    }
    
    reportError(error: Error, fatal: boolean = false): void {
        this.commMgr.reportError(error, fatal)
    }
    
    getState(): AppState {
        return this.commMgr.state
    }
    
    setSharedState(key: string, value: any): void {
        this.commMgr.setSharedState(key, value)
    }
    
    getSharedState(key: string, defaultValue?: any): any {
        return this.commMgr.getSharedState(key, defaultValue)
    }
}

/**
 * Message queued for sending
 */
interface QueuedMessage {
    messageId: string
    message: any
    callback?: (response: any) => void
}

/**
 * Pending request tracking
 */
interface PendingRequest {
    commId: string
    messageId: string
    callback?: (response: any) => void
}

/**
 * CommMgr namespace for Bokeh properties
 */
export namespace CommMgr {
    export type Attrs = p.AttrsOf<Props>
    
    export type Props = Model.Props & {
        transport_type: p.Property<string>
        address: p.Property<[string, number] | null>
        comm_mgr_id: p.Property<string>
    }
}

export interface CommMgr extends CommMgr.Attrs {}

/**
 * Communications Manager - Bokeh Model
 * 
 * Manages all communication with the Python backend.
 * Stored as property of BokehAppContext for automatic reconstruction.
 */
export class CommMgr extends Model {
    declare properties: CommMgr.Props

    static __module__ = "cubevis.bokeh.transport._comm_mgr"

    // Internal state (not Bokeh properties)
    private transport?: TransportBase
    private comms: Map<string, Comm> = new Map()
    private handlers: Map<string, Map<string, (msg: any, ctx: HandlerContext) => any>> = new Map()
    private pending: Map<string, string> = new Map()  // comm_id => request_id
    private pendingRequests: Map<string, PendingRequest> = new Map()  // request_id => info
    private sendQueue: Map<string, QueuedMessage[]> = new Map()  // comm_id => queue
    private sharedState: Map<string, any> = new Map()
    private context: HandlerContext
    private _state: AppState = AppState.INITIALIZING
    private onShutdown?: (reason: string) => void
    private onError?: (error: Error) => void
    private initialized: boolean = false
    private shutdownRequested: boolean = false
    private runTask?: Promise<void>
    
    constructor(attrs?: Partial<CommMgr.Attrs>) {
        super(attrs)
        this.context = new HandlerContext(this)
    }
    
    initialize(): void {
        super.initialize()
        
        console.log(`CommMgr initializing: ${this.comm_mgr_id}`)
        
        // Initialize transport based on properties
        this.initializeTransport()
    }
    
    get state(): AppState {
        return this._state
    }
    
    set state(newState: AppState) {
        const oldState = this._state
        this._state = newState
        console.log(`CommMgr state: ${oldState} -> ${newState}`)
    }
    
    /**
     * Initialize the transport based on properties
     */
    private async initializeTransport(): Promise<void> {
        if (this.initialized) {
            return
        }
        
        this.state = AppState.INITIALIZING
        
        try {
            // Determine transport type
            let transportType = this.transport_type
            if (transportType === 'auto') {
                transportType = this.detectTransport()
            }
            
            // Create appropriate transport
            // Note: WebSocket created in run() when we have the actual connection
            if (transportType === 'websocket') {
                if (!this.address) {
                    throw new Error("WebSocket transport requires address")
                }
                this.transport = new WebSocketTransport(
                    this,
                    this.address
                )
            } else if (transportType === 'colab') {
                this.transport = new ColabCommsTransport(this)
            } else if (transportType === 'jupyter') {
                this.transport = new JupyterCommsTransport(this)
            } else {
                throw new Error(`Unknown transport type: ${transportType}`)
            }
            
            // ALL TRANSPORTS USE SAME PATTERN NOW!
            // 1. Set message callback
            this.transport.setMessageCallback((msg: any) => {
                this.routeMessage(msg)
            })
            
            // 2. Connect (performs handshake if needed)
            await this.transport.connect()
            
            this.initialized = true
            this.state = AppState.RUNNING
            console.log(`CommMgr initialized with ${transportType} transport`)
            
            // 3. Start event loop
            this.runTransport()
            
        } catch (e) {
            console.error("Error initializing CommMgr transport:", e)
            this.state = AppState.ERROR
            throw e
        }
    }

    /**
     * Run the transport event loop.
     * 
     * ALL TRANSPORTS NOW USE THE SAME PATTERN!
     */
    private async runTransport(): Promise<void> {
        if (!this.transport) {
            return
        }
        
        try {
            // Flush any queued messages
            await this.flushAllQueues()
            
            // Run transport event loop (blocks until shutdown/close)
            this.runTask = this.transport.run()
            await this.runTask
            
            console.log("Transport event loop completed")
            
        } catch (e) {
            console.error("Error in transport event loop:", e)
            this.reportError(e as Error, true)
        } finally {
            await this.shutdown()
        }
    }

    /**
     * Auto-detect appropriate transport
     */
    private detectTransport(): string {
        if ((window as any).google?.colab) {
            return 'colab'
        }
        
        if ((window as any).Jupyter) {
            return 'jupyter'
        }
        
        return 'websocket'
    }

    /**
     * Register a Comm with this manager
     * Called by Comm.initialize()
     */
    registerComm(comm: Comm): void {
        const commId = comm.comm_id
        
        if (!this.comms.has(commId)) {
            this.comms.set(commId, comm)
            this.handlers.set(commId, new Map())
            this.sendQueue.set(commId, [])
            console.log(`Registered comm: ${commId}`)
        }
    }
    
    /**
     * Register a handler for messages
     */
    register(comm: Comm, messageId: string, callback: (msg: any, ctx: HandlerContext) => any): void {
        const commId = comm.comm_id
        
        if (!this.handlers.has(commId)) {
            this.handlers.set(commId, new Map())
        }
        
        const commHandlers = this.handlers.get(commId)!
        
        if (commHandlers.has(messageId)) {
            console.warn(`Replacing handler for ${commId}.${messageId}`)
        }
        
        commHandlers.set(messageId, callback)
        console.debug(`Registered handler: ${commId}.${messageId}`)
    }
    
    /**
     * Unregister a handler
     */
    unregister(comm: Comm, messageId: string): void {
        const commId = comm.comm_id
        
        if (this.handlers.has(commId)) {
            this.handlers.get(commId)!.delete(messageId)
            console.debug(`Unregistered handler: ${commId}.${messageId}`)
        }
    }
    
    /**
     * Send a message through a comm
     */
    send(comm: Comm, messageId: string, message: any, callback?: (response: any) => void): void {
        const commId = comm.comm_id
        const requestId = this.generateId()
        
        // Check if this comm has a pending request
        if (this.pending.has(commId)) {
            // Queue this message
            if (comm.squash_queue) {
                // Squash mode: remove any queued message with same message_id
                const queue = this.sendQueue.get(commId)!
                this.sendQueue.set(
                    commId,
                    queue.filter(item => item.messageId !== messageId)
                )
            }
            
            // Add to queue
            this.sendQueue.get(commId)!.push({
                messageId,
                message,
                callback
            })
            
            console.debug(
                `Queued message for ${commId}.${messageId} ` +
                `(queue size: ${this.sendQueue.get(commId)!.length})`
            )
        } else {
            // Send immediately
            this.sendImmediate(commId, messageId, message, requestId, callback)
        }
    }
    
    /**
     * Send a message immediately
     */
private sendImmediate(
        commId: string,
        messageId: string,
        message: any,
        requestId: string,
        callback?: (response: any) => void
    ): void {
        const msg = {
            comm_id: commId,
            message_id: messageId,
            message: message,
            direction: 'j2p',
            request_id: requestId
        }
        
        // Mark as pending
        this.pending.set(commId, requestId)
        this.pendingRequests.set(requestId, {
            commId,
            messageId,
            callback
        })
        
        // Send through transport
        if (this.transport && this.transport.isConnected()) {
            this.transport.send(msg)
            console.debug(`Sent message: ${commId}.${messageId} (request_id=${requestId})`)
        } else {
            console.warn(`Transport not ready, cannot send ${commId}.${messageId}`)
        }
    }
    
    /**
     * Process next queued message for a comm
     */
    private processNextQueued(commId: string): void {
        const queue = this.sendQueue.get(commId)
        
        if (!queue || queue.length === 0) {
            return
        }
        
        // Get next message
        const item = queue.shift()!
        const requestId = this.generateId()
        
        console.debug(
            `Processing queued message for ${commId}.${item.messageId} ` +
            `(${queue.length} remaining in queue)`
        )
        
        // Send it
        this.sendImmediate(commId, item.messageId, item.message, requestId, item.callback)
    }
    
    /**
     * Route incoming message to appropriate handler
     */
    private routeMessage(msg: any): void {
        const direction = msg.direction
        
        if (direction === 'j2p') {
            // Response to our request
            this.handleResponse(msg)
        } else if (direction === 'p2j') {
            // Request from Python
            this.handleRequest(msg)
        }
    }

    /**
     * Handle response from Python
     */
    private handleResponse(msg: any): void {
        const requestId = msg.request_id
        
        if (!requestId || !this.pendingRequests.has(requestId)) {
            console.warn(`Received response for unknown request: ${requestId}`)
            return
        }
        
        // Get request info
        const request = this.pendingRequests.get(requestId)!
        this.pendingRequests.delete(requestId)
        
        const {commId, messageId, callback} = request
        
        // Clear pending state
        if (this.pending.get(commId) === requestId) {
            this.pending.delete(commId)
        }
        
        // Call callback
        if (callback) {
            try {
                callback(msg.message)
            } catch (e) {
                console.error(`Error in response callback for ${commId}.${messageId}:`, e)
                this.reportError(e as Error, false)
            }
        }
        
        // Process next queued message
        this.processNextQueued(commId)
    }
    
    /**
     * Handle request from Python
     */
    private handleRequest(msg: any): void {
        const commId = msg.comm_id
        const messageId = msg.message_id
        const requestId = msg.request_id
        
        if (!commId || !messageId) {
            console.warn("Request missing comm_id or message_id:", msg)
            return
        }
        
        // Find handler
        if (!this.handlers.has(commId) || !this.handlers.get(commId)!.has(messageId)) {
            console.warn(`No handler for ${commId}.${messageId}`)
            
            // Send error response
            if (requestId && this.transport) {
                this.transport.send({
                    comm_id: commId,
                    message_id: messageId,
                    request_id: requestId,
                    message: {error: `No handler for ${commId}.${messageId}`},
                    direction: 'p2j'
                })
            }
            return
        }
        
        // Call handler
        const handler = this.handlers.get(commId)!.get(messageId)!
        
        try {
            const result = handler(msg.message, this.context)
            
            // Send response if there's a request_id
            if (requestId && this.transport) {
                this.transport.send({
                    comm_id: commId,
                    message_id: messageId,
                    request_id: requestId,
                    message: result,
                    direction: 'p2j'
                })
            }
        } catch (e) {
            console.error(`Error in handler ${commId}.${messageId}:`, e)
            this.reportError(e as Error, false)
            
            if (requestId && this.transport) {
                this.transport.send({
                    comm_id: commId,
                    message_id: messageId,
                    request_id: requestId,
                    message: {error: (e as Error).message},
                    direction: 'p2j'
                })
            }
        }
    }
    
    requestShutdown(reason: string = "Shutdown requested"): void {
        if (this.shutdownRequested) {
            return
        }
        
        console.log(`Shutdown requested: ${reason}`)
        this.shutdownRequested = true
        
        if (this.onShutdown) {
            this.onShutdown(reason)
        }
        
        this.shutdown()
    }

    reportError(error: Error, fatal: boolean = false): void {
        console.error(`Error reported (${fatal ? 'fatal' : 'non-fatal'}):`, error)
        
        if (this.onError) {
            this.onError(error)
        }
        
        if (fatal) {
            this.state = AppState.ERROR
            this.requestShutdown(`Fatal error: ${error.message}`)
        }
    }

    setSharedState(key: string, value: any): void {
        this.sharedState.set(key, value)
        console.debug(`Shared state set: ${key}`)
    }
    
    getSharedState(key: string, defaultValue?: any): any {
        return this.sharedState.has(key) ? this.sharedState.get(key) : defaultValue
    }

    async shutdown(): Promise<void> {
        if (this.state === AppState.STOPPED) {
            return
        }
        
        console.log("Shutting down CommMgr")
        this.state = AppState.SHUTTING_DOWN
        
        if (this.transport) {
            this.transport.close()
        }
        
        this.handlers.clear()
        this.pending.clear()
        this.pendingRequests.clear()
        this.sendQueue.clear()
        this.comms.clear()
        
        this.state = AppState.STOPPED
    }

    private async flushAllQueues(): Promise<void> {
        for (const commId of Array.from(this.sendQueue.keys())) {
            if (this.sendQueue.get(commId)!.length > 0 && !this.pending.has(commId)) {
                this.processNextQueued(commId)
            }
        }
    }

    private generateId(): string {
        return `${Date.now()}_${Math.random().toString(36).substr(2, 9)}`
    }

    static {
        this.define<CommMgr.Props>(({String, Tuple, Number, Nullable}) => ({
            transport_type: [String, 'auto'],
            address: [Nullable(Tuple(String, Number)), null],
            comm_mgr_id: [String],
        }))
    }
}

// ============================================================================
// Comm TypeScript Implementation
// ============================================================================

/**
 * Comm namespace for Bokeh properties
 */
export namespace Comm {
    export type Attrs = p.AttrsOf<Props>
    
    export type Props = Model.Props & {
        comm_id: p.Property<string>
        comm_mgr_id: p.Property<string>
        squash_queue: p.Property<boolean>
    }
}

export interface Comm extends Comm.Attrs {}

/**
 * Communication channel - Bokeh Model
 * 
 * Represents a logical communication category.
 * Finds its CommMgr via document search using comm_mgr_id.
 * Can be passed directly to CustomJS callbacks.
 */
export class Comm extends Model {
    declare properties: Comm.Props

    static __module__ = "cubevis.bokeh.transport._comm_mgr"

    private _mgr?: CommMgr
    
    constructor(attrs?: Partial<Comm.Attrs>) {
        super(attrs)
    }
    
    initialize(): void {
        super.initialize()
        
        console.log(`Comm initializing: ${this.comm_id}`)
        
        // Find CommMgr
        this._mgr = this.findCommMgr()
        
        if (this._mgr) {
            // Register with CommMgr
            this._mgr.registerComm(this)
            console.log(`Comm ${this.comm_id} found CommMgr ${this._mgr.comm_mgr_id}`)
        } else {
            console.warn(`Comm ${this.comm_id} could not find CommMgr ${this.comm_mgr_id}`)
        }
    }
    
    /**
     * Find the CommMgr for this Comm
     * 
     * Searches the document starting from root (BokehAppContext)
     * which should have comm_mgr property.
     */
    private findCommMgr(): CommMgr | undefined {
        const roots = this.document?.roots()
        if (!roots || roots.length === 0) {
            console.warn("Comm: No document roots found")
            return undefined
        }
        
        // The root should be BokehAppContext with comm_mgr property
        const appContext = roots[0]
        
        // Check if appContext has comm_mgr property
        if (appContext && 'comm_mgr' in appContext) {
            const mgr = (appContext as any).comm_mgr
            
            if (mgr instanceof CommMgr && mgr.comm_mgr_id === this.comm_mgr_id) {
                return mgr
            }
        }
        
        // Fallback: search all models in document
        console.warn("Comm: comm_mgr not found in root, searching document...")
        return this.searchDocumentForCommMgr()
    }
    
    /**
     * Fallback method to search entire document for CommMgr
     */
    private searchDocumentForCommMgr(): CommMgr | undefined {
        if (!this.document) {
            return undefined
        }
        
        // Search all models in document
        for (const model of this.document.roots()) {
            const found = this.searchModelForCommMgr(model)
            if (found) {
                return found
            }
        }
        
        return undefined
    }
    
    /**
     * Recursively search a model and its references for CommMgr
     */
    private searchModelForCommMgr(model: any): CommMgr | undefined {
        if (model instanceof CommMgr && model.comm_mgr_id === this.comm_mgr_id) {
            return model
        }
        
        // Search references
        for (const ref of model.references()) {
            const found = this.searchModelForCommMgr(ref)
            if (found) {
                return found
            }
        }
        
        return undefined
    }
    
    /**
     * Register a handler for messages with this ID
     */
    register(messageId: string, callback: (msg: any, ctx: HandlerContext) => any): void {
        if (this._mgr) {
            this._mgr.register(this, messageId, callback)
        } else {
            console.error(`Comm ${this.comm_id}: Cannot register, CommMgr not found`)
        }
    }
    
    /**
     * Unregister a handler
     */
    unregister(messageId: string): void {
        if (this._mgr) {
            this._mgr.unregister(this, messageId)
        }
    }
    
    /**
     * Send a message through this comm
     */
    send(messageId: string, message: any, callback?: (response: any) => void): void {
        if (this._mgr) {
            this._mgr.send(this, messageId, message, callback)
        } else {
            console.error(`Comm ${this.comm_id}: Cannot send, CommMgr not found`)
        }
    }
    
    static {
        this.define<Comm.Props>(({String, Boolean}) => ({
            comm_id: [String],
            comm_mgr_id: [String],
            squash_queue: [Boolean, false]
        }))
    }
}
