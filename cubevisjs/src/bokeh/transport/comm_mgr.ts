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
import {CustomJS} from "@bokehjs/models/callbacks/index"
import * as find from "../util/find"

// Import transport implementations
import {TransportBase, WebSocketTransport, CommsTransport} from "./low_level_transport"

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
        init_scripts: p.Property<[CustomJS, string, string][]>
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

    private reconnectAttempts: number = 0
    private maxReconnectAttempts: number = 10
    private reconnectDelay: number = 1000  // Start with 1 second
    private maxReconnectDelay: number = 30000  // Max 30 seconds
    private reconnectTimer?: number
    private shouldReconnect: boolean = true

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
    
    constructor(attrs?: Partial<CommMgr.Attrs>) {
        super(attrs)
        this.context = new HandlerContext(this)
        console.log("CommMgr constructor:", this)
    }
    
    initialize(): void {
        super.initialize()
        try {
            console.log("CommMgr initialize:", this)
            // Initialize transport based on properties
            this.initializeTransport()

            //
            // Run any initialization script
            //
            const _execute = () => {
                console.group( "CommMgr init script execution" )
                this.init_scripts.forEach(
                    ([script, id, description], i) => {
                        // Pass the current loop index 'i' into the cb_data object
                        if ( description === null || description === undefined || description.trim().length === 0 )
                            console.log(id)
                        else
                            console.log(description)
                        script.execute( this, { index: i, id, description } )
                    } )
                console.groupEnd( )
            }

            _execute( )

        } catch (error) {
            console.error("An error occurred:", error.message)
        }
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
     * Initialize the transport with automatic reconnection.
     */
    private async initializeTransport(): Promise<void> {
        if (this.initialized) {
            return
        }
        
        console.log("<1>transport_type is:", this.transport_type)
        this.state = AppState.INITIALIZING
        
        console.log("<2>transport_type is:", this.transport_type)
        try {
            // Determine transport type
            let transportType = this.transport_type
            if (transportType === 'auto') {
                transportType = this.detectTransport()
            }
            
            console.log("<3>transport_type is:", this.transport_type)
            // Create appropriate transport
            if (transportType === 'websocket') {
                if (!this.address) {
                    throw new Error("WebSocket transport requires address")
                }
                await this.connectWebSocket()
            } else if (transportType === 'colab' || transportType === 'jupyter') {
                console.log("<4>transport_type is:", this.transport_type)
                this.transport = new CommsTransport(this)
                await this.setupTransport()
            } else {
                throw new Error(`Unknown transport type: ${transportType}`)
            }
            
            this.initialized = true
            this.state = AppState.RUNNING
            console.log(`CommMgr initialized with ${transportType} transport`)

        } catch (e) {
            console.log("<5>transport_type is:", this.transport_type)
            console.error("Error initializing CommMgr transport:", e)
            console.log("<6>transport_type is:", this.transport_type)
            this.state = AppState.ERROR

            console.log("<7>transport_type is:", this.transport_type)
            if (this.transport_type === 'websocket' || this.transport_type === 'auto') {
                // Attempt reconnection for WebSocket
                this.scheduleReconnect()
            } else {
                console.log( "find showable #1:", find.showable(this) )
                // Disable the GUI — no backend is available, but execution must be delayed
                //                   until the GUI has actually been initialized and the
                //                   Showable is available...
                setTimeout(() => {
                    // with Bokeh 3.6 there is a roots( ) function...
                    // with Bokeh 3.8 there is a all_roots property...
                    console.log( "find showable #2:", find.showable(this) )
                    const roots = this.document?.all_roots ?? this.document?.roots() ?? []
                    console.log( 'Searching roots:', roots )
                    for (const root of roots) {
                        const comm_mgr = (root as any).comm_mgr
                        if (comm_mgr === this) {
                            // root is our BokehAppContext
                            const showable = (root as any).ui ?? find.showable(root as any)
                            if (showable) {
                                console.log("CommMgr: no backend available, disabling Showable")
                                showable.disabled_message = "No active session — re-run the cell to restart"
                                showable.disabled = true
                                return
                            }
                        }
                    }
                    console.warn("CommMgr: entered error state but could not find Showable to disable")
                }, 0)
                //throw e
            }
        }
    }

    /**
     * Connect WebSocket with reconnection support.
     */
    private async connectWebSocket(): Promise<void> {
        console.log(`Connecting WebSocket (attempt ${this.reconnectAttempts + 1})...`)

        try {
            this.transport = new WebSocketTransport(
                this,
                this.address!
            )

            await this.setupTransport()

            // Reset reconnect counter on successful connection
            this.reconnectAttempts = 0
            this.reconnectDelay = 1000

            console.log("WebSocket connected successfully")

        } catch (e) {
            console.error("WebSocket connection failed:", e)
            throw e
        }
    }

    /**
     * Set up transport callbacks and start event loop.
     */
    private async setupTransport(): Promise<void> {
        if (!this.transport) {
            throw new Error("Transport not created")
        }

        // Set message callback
        this.transport.setMessageCallback((msg: any) => {
            this.routeMessage(msg)
        })

       // Connect
        await this.transport.connect()

        // Flush any queued messages
        await this.flushAllQueues()

        // Start event loop (DON'T await - let it run in background)
        this.runTransportWithReconnection()  // ← No await!
    }

    /**
     * Run transport event loop with automatic reconnection.
     */
    private async runTransportWithReconnection(): Promise<void> {
        if (!this.transport) {
            return
        }

        try {
            console.log("Transport event loop starting...")

            // Run transport until it closes
            await this.transport.run()

            console.log("Transport event loop completed")

            // Transport closed - attempt reconnection if still active
            if (this.shouldReconnect && this.state !== AppState.STOPPED) {
                console.log("Transport closed, attempting reconnection...")
                this.scheduleReconnect()  // ← This should be called!
            } else {
                console.log("Not reconnecting (shouldReconnect=" + this.shouldReconnect + ", state=" + this.state + ")")
            }

        } catch (e) {
            console.error("Transport error:", e)

            // Attempt reconnection on error
            if (this.shouldReconnect && this.state !== AppState.STOPPED) {
                console.log("Transport error, attempting reconnection...")
                this.scheduleReconnect()
            }
        }
    }

    /**
     * Schedule a reconnection attempt with exponential backoff.
     */
    private scheduleReconnect(): void {
        // Don't reconnect if we're shutting down
        if (!this.shouldReconnect || this.state === AppState.STOPPED) {
            return
        }

        // Check if we've exceeded max attempts
        if (this.reconnectAttempts >= this.maxReconnectAttempts) {
            console.error(`Failed to reconnect after ${this.maxReconnectAttempts} attempts`)
            this.state = AppState.ERROR
            this.reportError(new Error("Max reconnection attempts exceeded"), true)
            return
        }

        // Clear any existing timer
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer)
        }

        // Calculate delay with exponential backoff
        const delay = Math.min(
            this.reconnectDelay * Math.pow(2, this.reconnectAttempts),
            this.maxReconnectDelay
        )

        console.log(`Reconnecting in ${delay}ms...`)

        // Schedule reconnection
        this.reconnectTimer = window.setTimeout(async () => {
            this.reconnectAttempts++

            try {
                // Close old transport
                if (this.transport) {
                    try {
                        this.transport.close()
                    } catch (e) {
                        console.error("Error closing old transport:", e)
                    }
                    this.transport = undefined
                }

                // Attempt to reconnect
                await this.connectWebSocket()

            } catch (e) {
                console.error("Reconnection attempt failed:", e)

                // Try again
                this.scheduleReconnect()
            }
        }, delay)
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
            const desc = comm.description?.trim()
            const logMsg = desc ? `${desc} (${commId})` : commId
            console.log(`Registered comm: ${logMsg}`)
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

        console.log( `CommMgr.send(messageId: ${messageId}, requestId: ${requestId}):`, message, callback )
        // Check if transport is connected
        if (!this.transport || !this.transport.isConnected()) {

            if ( ! this.shouldReconnect ) {
                console.warn(
                    `Attempted send of message for ${commId}.${messageId} ` +
                        `after shutdown (dropping message):`, message
                )
                if ( callback ) callback( { result: 'Error', error: 'transport has been shut down' } )
                return
            }

            console.warn(
                `Transport not ready, queuing message for ${commId}.${messageId} ` +
                `(will send when reconnected)`
            )

            // Queue the message
            if (!this.sendQueue.has(commId)) {
                this.sendQueue.set(commId, [])
            }

            this.sendQueue.get(commId)!.push({
                messageId,
                message,
                callback
            })

            // Trigger reconnection if not already happening
            if (this.shouldReconnect && !this.reconnectTimer) {
                console.log("Triggering reconnection due to queued message")
                this.scheduleReconnect()
            }

            return
        }

        // Check if this comm has a pending request
        if (this.pending.has(commId)) {
            // Queue this message
            console.log( 'CommMgr.send: queuing message' )
            if (comm.squash_queue) {
                // Squash mode: remove any queued message with same message_id
                const queue = this.sendQueue.get(commId)!
                console.log( 'CommMgr.send: squash queue' )
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
            console.log( 'CommMgr.send: sending message', message )
            this.sendImmediate(commId, messageId, message, requestId, callback)
        }
    }
    
    /**
     * Flush all queued messages after reconnection.
     */
    private async flushAllQueues(): Promise<void> {
        console.log("Flushing queued messages after reconnection...")

        for (const [commId, queue] of this.sendQueue.entries()) {
            if (queue.length > 0 && !this.pending.has(commId)) {
                console.log(`Flushing ${queue.length} messages for comm ${commId}`)
                this.processNextQueued(commId)
            }
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
        console.log( `CommMgr.sendImmediate(commId: ${commId}):`, message )
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
            console.log( `CommMgr.sendImmediate(commId: ${commId}): sending message` )
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

        if (msg['transport_control'] === 'SHUTDOWN-NOW') {
            this.shutdown( )
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
        console.log("Shutting down CommMgr")

        // Prevent reconnection
        this.shouldReconnect = false

        // Clear reconnect timer
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer)
            this.reconnectTimer = undefined
        }
        
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

    private generateId(): string {
        return `${Date.now()}_${Math.random().toString(36).substr(2, 9)}`
    }

    static {
      this.define<CommMgr.Props>(({String, Tuple, Number, Nullable, Array, Ref}) => ({
            transport_type: [String, 'auto'],
            address: [Nullable(Tuple(String, Number)), null],
            comm_mgr_id: [String],
            init_scripts:   [ Array(Tuple(Ref(CustomJS), String, String)), [] ]
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
        description: p.Property<string>
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

        console.log(`Comm initializing: ${this.description?.trim() || this.comm_id} [squash:${this.squash_queue}]`);

        // Find CommMgr
        this._mgr = this.findCommMgr()
        if ( this._mgr && this._mgr.comm_mgr_id ) console.log( `Comm ${this.description?.trim() || this.comm_id} registered with mgr ${this._mgr.comm_mgr_id}` )
        else console.warn( `Comm ${this.description?.trim() || this.comm_id} failed to register with mgr` )
        
        if (this._mgr) {
            // Register with CommMgr
            this._mgr.registerComm(this)
            console.log(`Comm ${this.description?.trim() || this.comm_id} found CommMgr ${this._mgr.comm_mgr_id}`)
        } else {
            console.warn(`Comm ${this.description?.trim() || this.comm_id} could not find CommMgr ${this.comm_mgr_id}`)
        }
    }
    
    /**
     * Find the CommMgr for this Comm
     * 
     * Searches the document starting from root (BokehAppContext)
     * which should have comm_mgr property.
     */
    private findCommMgr(): CommMgr | undefined {
        const mgr = this.document?.get_model_by_name(this.comm_mgr_id) as CommMgr | undefined
        if (!mgr) {
            console.warn(`Comm ${this.description?.trim() || this.comm_id}: no CommMgr named ${this.comm_mgr_id} found`)
        }
        return mgr
    }

    /**
     * Register a handler for messages with this ID
     */
    register(messageId: string, callback: (msg: any, ctx: HandlerContext) => any): void {
        if (this._mgr) {
            this._mgr.register(this, messageId, callback)
        } else {
            console.error(`Comm ${this.description?.trim() || this.comm_id}: Cannot register, CommMgr not found`)
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
            console.log( `Comm.send(messageId: ${messageId}):`, message, callback )
            this._mgr.send(this, messageId, message, callback)
        } else {
            console.error(`Comm ${this.description?.trim() || this.comm_id}: Cannot send, CommMgr not found`)
        }
    }
    
    static {
        this.define<Comm.Props>(({String, Boolean}) => ({
            comm_id: [String],
            description: [String,''],
            comm_mgr_id: [String],
            squash_queue: [Boolean, false]
        }))
    }
}
