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

// Import transport implementations
import {TransportBase, WebSocketTransport, CommsTransport} from "./low_level_transport"

// Enums
enum AppState {
    INITIALIZING = "initializing",
    RUNNING = "running",
    SHUTTING_DOWN = "shutting_down",
    STOPPED = "stopped",
    ERROR = "error",
    // Another page (tab) has the session; this one stops reconnecting
    // until its "Use this tab" button is pressed (transport review WS-2).
    SUPERSEDED = "superseded"
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
    requestId: string
    callback?: (response: any) => void
}

/**
 * Pending request tracking
 */
interface PendingRequest {
    commId: string
    messageId: string
    /**
     * Retained so a request that was in flight when the connection dropped can
     * be pushed back onto its comm's send queue and replayed after reconnection.
     */
    message: any
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
    private wakeRecoveryAttached: boolean = false
    // The connection attempt under way, if any (transport review JS-1).
    // Reconnects are single-flight: a wake, a send() or a timer that fires
    // while an attempt is pending waits for that attempt instead of closing
    // it mid-handshake and starting another (each abandoned attempt was a
    // failed handshake on the Python side).
    private connecting?: Promise<void>
    // Another page took the session (see onSuperseded()).
    private superseded: boolean = false
    // Identifies this page load to Python in the handshake (`frontend_id`)
    // when the BokehAppContext's own id cannot be found from here -- in
    // practice always: find.context() does not reach the context from the
    // CommMgr, and until 2026-11-20 every page sent null, so Python could
    // not tell a second tab from a reconnect (transport review WS-2).
    readonly pageId: string =
        `page-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
    // comm_id => the last request Python sent on that comm and the reply
    // this page gave.  Python replays a request that was in flight when a
    // connection ended with its first request_id; the reply is sent again
    // rather than the handler run twice (transport review RP-1).
    private lastRequest: Map<string, {requestId: string, reply: any}> = new Map()

    private comms: Map<string, Comm> = new Map()
    private handlers: Map<string, Map<string, (msg: any, ctx: HandlerContext) => any>> = new Map()
    private pending: Map<string, string> = new Map()  // comm_id => request_id
    private pendingRequests: Map<string, PendingRequest> = new Map()  // request_id => info
    private sendQueue: Map<string, QueuedMessage[]> = new Map()  // comm_id => queue
    private requestBorn: Map<string, number> = new Map()  // request_id => send() time (ms)
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
    }
    
    initialize(): void {
        super.initialize()

        // Page-wide registry of live managers (2026-10-09). The busy
        // indicator (visplot's window.__cvSetBusy) asks it whether any
        // request is still awaiting a reply before it gives up on a busy
        // period; see inFlight() and canReply().
        const w = window as any
        w.__cvCommMgrs = w.__cvCommMgrs || []
        if (w.__cvCommMgrs.indexOf(this) < 0) w.__cvCommMgrs.push(this)

        try {

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
        
        this.state = AppState.INITIALIZING
        
        try {
            // Determine transport type
            let transportType = this.transport_type
            if (transportType === 'auto') {
                transportType = this.detectTransport()
            }
            
            // Create appropriate transport
            if (transportType === 'websocket') {
                if (!this.address) {
                    throw new Error("WebSocket transport requires address")
                }
                this.attachWakeRecovery()
                const attempt = this.connectWebSocket()
                this.connecting = attempt
                try {
                    await attempt
                } finally {
                    if (this.connecting === attempt) this.connecting = undefined
                }
            } else if (transportType === 'colab' || transportType === 'jupyter') {
                this.transport = new CommsTransport(this)
                await this.setupTransport()
            } else {
                throw new Error(`Unknown transport type: ${transportType}`)
            }
            
            this.initialized = true
            this.state = AppState.RUNNING
            console.debug(`CommMgr initialized with ${transportType} transport`)

        } catch (e) {
            console.error("Error initializing CommMgr transport:", e)
            this.state = AppState.ERROR

            if (this.transport_type === 'websocket' || this.transport_type === 'auto') {
                // Attempt reconnection for WebSocket
                this.scheduleReconnect()
            } else {

                // Disable the GUI — no backend is available, but execution must be delayed
                //                   until the GUI has actually been initialized and the
                //                   Showable is available...
                setTimeout(() => {
                    // with Bokeh 3.6 there is a roots( ) function...
                    // with Bokeh 3.8 there is a all_roots property...
                    const roots = this.document?.all_roots ?? this.document?.roots() ?? []
                    for (const root of roots) {
                        if ((root as any).type === "cubevis.bokeh.models._showable.Showable") {
                            const showable = root as any
                            console.log("CommMgr: no backend available, disabling Showable")
                            showable.disabled_message = "No active session — re-run the cell to restart"
                            showable.disabled = true
                            return
                        }
                    }
                    // Fallback: try find.showable from CommMgr itself
                    console.warn("CommMgr: entered error state but could not find Showable to disable")
                }, 0)
                //throw e
            }
        }
    }

    /**
     * Recover promptly when the machine wakes, the tab is refocused, or the
     * network comes back.
     *
     * The transport's own heartbeat will eventually notice a dead socket, but
     * these events are a much better signal and arrive immediately. They also
     * clear the backoff, which matters because a laptop closed overnight will
     * otherwise be sitting at the 30s ceiling -- or past maxReconnectAttempts
     * entirely, where scheduleReconnect() has paused the retry loop and only a
     * backoff reset can restart it.
     */
    private attachWakeRecovery(): void {
        if (this.wakeRecoveryAttached) {
            return
        }
        this.wakeRecoveryAttached = true

        const onWake = () => {
            if (!this.shouldReconnect || this.state === AppState.STOPPED || this.superseded) {
                return
            }
            if (this.transport && this.transport.isConnected()) {
                return
            }
            if (this.connecting) {
                return   // an attempt is under way; let it finish (JS-1)
            }

            console.debug("Wake/online detected — retrying connection immediately")
            this.resetBackoff()

            if (this.reconnectTimer !== undefined) {
                clearTimeout(this.reconnectTimer)
                this.reconnectTimer = undefined
            }
            this.scheduleReconnect()
        }

        window.addEventListener("online", onWake)
        window.addEventListener("focus", onWake)
        window.addEventListener("pageshow", onWake)
        document.addEventListener("visibilitychange", () => {
            if (document.visibilityState === "visible") {
                onWake()
            }
        })
    }

    /**
     * Connect WebSocket with reconnection support.
     */
    private async connectWebSocket(claim: boolean = false): Promise<void> {
        console.debug(`Connecting WebSocket (attempt ${this.reconnectAttempts + 1})...`)

        try {
            // Held locally through setupTransport(): this.transport may be
            // replaced while an await is pending (JS-1).
            const transport = new WebSocketTransport(
                this,
                this.address!,
                claim
            )
            this.transport = transport

            await this.setupTransport(transport)

            // Reset reconnect counter on successful connection
            this.reconnectAttempts = 0
            this.reconnectDelay = 1000

            console.debug("WebSocket connected successfully")

        } catch (e) {
            console.error("WebSocket connection failed:", e)
            throw e
        }
    }

    /**
     * Set up transport callbacks and start event loop.
     */
    private async setupTransport(transport: TransportBase | undefined = this.transport): Promise<void> {
        if (!transport) {
            throw new Error("Transport not created")
        }

        // Set message callback
        transport.setMessageCallback((msg: any) => {
            this.routeMessage(msg)
        })

       // Connect
        await transport.connect()

        // A newer transport replaced this one while connect() was pending:
        // this one is not ours to use any more (JS-1).
        if (this.transport !== transport) {
            transport.close()
            throw new Error("Connection attempt superseded by a newer one")
        }
        if (this.superseded) {
            // Told during the handshake that another tab has the session.
            transport.close()
            throw new Error("Session is open in another tab")
        }

        // Flush any queued messages
        await this.flushAllQueues()

        // Start event loop (DON'T await - let it run in background)
        this.runTransportWithReconnection(transport)  // ← No await!
    }

    /**
     * Run transport event loop with automatic reconnection.
     */
    private async runTransportWithReconnection(transport: TransportBase | undefined = this.transport): Promise<void> {
        if (!transport) {
            return
        }

        try {

            // Run transport until it closes
            await transport.run()

            // A transport that was already replaced ending is not the
            // connection ending: the newer one carries on (JS-1).
            if (this.transport !== transport) {
                return
            }

            this.resetConnectionState()

            // Transport closed - attempt reconnection if still active
            if (this.superseded) {
                console.debug("Transport closed: the session is open in another tab")
            } else if (this.shouldReconnect && this.state !== AppState.STOPPED) {
                console.debug("Transport closed, attempting reconnection...")
                this.state = AppState.INITIALIZING
                this.scheduleReconnect()
            } else {
                console.log("Not reconnecting (shouldReconnect=" + this.shouldReconnect + ", state=" + this.state + ")")
            }

        } catch (e) {
            console.error("Transport error:", e)
            if (this.transport !== transport) {
                return
            }

            this.resetConnectionState()

            // Attempt reconnection on error
            if (!this.superseded && this.shouldReconnect && this.state !== AppState.STOPPED) {
                console.debug("Transport error, attempting reconnection...")
                this.state = AppState.INITIALIZING
                this.scheduleReconnect()
            }
        }
    }

    /**
     * Discard per-connection state after the transport goes away.
     *
     * Without this a comm that had a request in flight when the socket dropped
     * stays in `pending` forever: flushAllQueues() skips it and send() queues
     * behind it, so that comm never recovers even though the socket did.
     *
     * In-flight requests are pushed back onto the FRONT of their comm's queue
     * so their callbacks still fire once the backend replies after
     * reconnection. This mirrors CommMgr._reset_for_reconnect() in Python.
     */
    private resetConnectionState(): void {
        let requeued = 0

        for (const [requestId, req] of this.pendingRequests.entries()) {
            const queue = this.sendQueue.get(req.commId) ?? []
            queue.unshift({
                messageId: req.messageId,
                message:   req.message,
                requestId,
                callback:  req.callback,
            })
            this.sendQueue.set(req.commId, queue)
            requeued++
        }

        this.pendingRequests.clear()
        this.pending.clear()

        if (requeued > 0) {
            console.debug(`Re-queued ${requeued} in-flight request(s) for replay after reconnection`)
        }
    }

    /**
     * Reset the exponential backoff clock.
     *
     * Used when the machine wakes or the network returns: the backoff should
     * not keep counting down while the machine was suspended, and a wake is the
     * best possible moment to retry.
     */
    private resetBackoff(): void {
        this.reconnectAttempts = 0
        this.reconnectDelay = 1000
    }

    /**
     * Schedule a reconnection attempt with exponential backoff.
     */
    private scheduleReconnect(): void {
        // Don't reconnect if we're shutting down, or another tab has the
        // session (only "Use this tab" reconnects then: reclaim()).
        if (!this.shouldReconnect || this.state === AppState.STOPPED || this.superseded) {
            return
        }
        // Single-flight (JS-1): an attempt under way schedules the next
        // one itself if it fails.
        if (this.connecting) {
            return
        }

        // Check if we've exceeded max attempts
        if (this.reconnectAttempts >= this.maxReconnectAttempts) {
            // Stop the automatic retry loop but stay recoverable: a later wake,
            // refocus or network-up event calls resetBackoff() and tries again.
            //
            // Do NOT report this as fatal. reportError(..., true) calls
            // requestShutdown(), which sets shouldReconnect = false and makes
            // recovery impossible -- a laptop closed overnight will always blow
            // through the attempt cap.
            console.warn(
                `Pausing reconnection after ${this.maxReconnectAttempts} attempts; ` +
                `will retry when the tab regains focus or the network returns`
            )
            this.state = AppState.ERROR
            return
        }

        // Clear any existing timer
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer)
        }

        // Calculate delay with exponential backoff, plus +/-20% jitter so that
        // several tabs waking at once do not stampede the backend.
        const base = Math.min(
            this.reconnectDelay * Math.pow(2, this.reconnectAttempts),
            this.maxReconnectDelay
        )
        const delay = Math.round(base * (0.8 + Math.random() * 0.4))

        console.log(`Reconnecting in ${delay}ms...`)

        // Schedule reconnection
        this.reconnectTimer = window.setTimeout(() => {
            // Clear the handle first. send() gates its recovery path on
            // `!this.reconnectTimer`, and leaving it set makes that path dead
            // after the very first reconnect.
            this.reconnectTimer = undefined
            this.reconnectAttempts++
            this.startAttempt(false)
        }, delay)
    }

    /**
     * One reconnection attempt, recorded in `this.connecting` until it
     * settles (JS-1).  On failure the next one is scheduled -- after
     * `connecting` is cleared, or scheduleReconnect() would see this attempt
     * still pending and do nothing.
     */
    private startAttempt(claim: boolean): void {
        if (this.connecting) {
            return
        }
        const attempt = (async () => {
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
            await this.connectWebSocket(claim)
        })()
        this.connecting = attempt
        attempt.then(
            () => {
                if (this.connecting === attempt) this.connecting = undefined
            },
            (e) => {
                if (this.connecting === attempt) this.connecting = undefined
                console.error("Reconnection attempt failed:", e)
                // Try again
                this.scheduleReconnect()
            }
        )
    }

    // ------------------------------------------------------------------
    // Another tab took the session (transport review WS-2)
    // ------------------------------------------------------------------

    /**
     * Called by the transport when Python says another page has the session
     * (an `__superseded__` message or close code SUPERSEDED_CLOSE_CODE).
     * This page stops reconnecting -- including on focus or wake -- and says
     * so over the window; its plots stay visible underneath.  Requests that
     * were waiting stay queued and go out if the page takes the session back.
     */
    onSuperseded(): void {
        if (this.superseded || this.state === AppState.STOPPED) {
            return
        }
        console.log("CommMgr: this session is now open in another tab")
        this.superseded = true
        if (this.reconnectTimer !== undefined) {
            clearTimeout(this.reconnectTimer)
            this.reconnectTimer = undefined
        }
        this.state = AppState.SUPERSEDED
        this.showSupersededNotice()
    }

    /**
     * "Use this tab": connect again, telling Python this is deliberate
     * (`claim`), so this page takes the session back and the other one is
     * told in turn.
     */
    reclaim(): void {
        if (!this.superseded) {
            return
        }
        this.superseded = false
        this.hideSupersededNotice()
        this.state = AppState.INITIALIZING
        this.resetBackoff()
        if (this.reconnectTimer !== undefined) {
            clearTimeout(this.reconnectTimer)
            this.reconnectTimer = undefined
        }
        this.startAttempt(true)
    }

    private supersededNoticeId(): string {
        return `cv-superseded-${this.comm_mgr_id}`
    }

    /**
     * A full-window notice, deliberately unlike the small remote-kernel
     * "Reconnecting to ..." chip: nothing is wrong with the connection; this
     * tab simply is not the one in use.  Plain DOM on document.body so it
     * covers the whole page whatever the Bokeh layout is.
     */
    private showSupersededNotice(): void {
        if (typeof document === "undefined" || document.getElementById(this.supersededNoticeId())) {
            return
        }
        const veil = document.createElement("div")
        veil.id = this.supersededNoticeId()
        veil.className = "cv-superseded"
        veil.setAttribute("role", "alertdialog")
        veil.setAttribute("aria-modal", "true")
        veil.setAttribute("aria-labelledby", veil.id + "-title")
        veil.style.cssText = [
            "position:fixed", "inset:0", "z-index:2147483000",
            "display:flex", "align-items:center", "justify-content:center",
            "background:rgba(17,17,27,0.62)",
            "font-family:system-ui,-apple-system,'Segoe UI',sans-serif",
        ].join(";")

        const card = document.createElement("div")
        card.style.cssText = [
            "max-width:26rem", "margin:1rem", "padding:1.25rem 1.5rem",
            "background:#1e1e2e", "color:#cdd6f4",
            "border:1px solid #45475a", "border-radius:10px",
            "box-shadow:0 10px 30px rgba(0,0,0,0.45)",
            "line-height:1.45", "font-size:14px",
        ].join(";")

        const title = document.createElement("div")
        title.id = veil.id + "-title"
        title.textContent = "Open in another tab"
        title.style.cssText = "font-size:16px;font-weight:600;margin-bottom:0.5rem"

        const body = document.createElement("div")
        body.textContent =
            "This session is now open in another browser tab. This tab no " +
            "longer updates; what it shows stays as it was."
        body.style.cssText = "margin-bottom:1rem;color:#bac2de"

        const button = document.createElement("button")
        button.type = "button"
        button.textContent = "Use this tab"
        button.title = "Take the session back; the other tab stops updating"
        button.style.cssText = [
            "padding:0.45rem 1rem", "font:inherit", "font-weight:600",
            "color:#11111b", "background:#89b4fa", "border:0",
            "border-radius:6px", "cursor:pointer",
        ].join(";")
        button.addEventListener("click", () => this.reclaim())

        // Keys pressed while the notice is up go nowhere else: this tab's
        // shortcuts would only produce requests that cannot be answered.
        veil.addEventListener("keydown", (ev: KeyboardEvent) => {
            if (ev.key !== "Tab" && ev.key !== "Enter" && ev.key !== " ") ev.stopPropagation()
        })

        card.append(title, body, button)
        veil.append(card)
        document.body.append(veil)
        try { button.focus() } catch (_e) { /* not focusable yet */ }
    }

    private hideSupersededNotice(): void {
        if (typeof document === "undefined") {
            return
        }
        const veil = document.getElementById(this.supersededNoticeId())
        if (veil) veil.remove()
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
            console.debug(`Registered comm: ${logMsg}`)
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
        this.requestBorn.set(requestId, Date.now())

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
            if ( this.superseded ) {
                console.debug(`Session is open in another tab; dropping ${commId}.${messageId}`)
                if ( callback ) callback( { result: 'Error', error: 'session is open in another tab' } )
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
                requestId,
                callback
            })

            // Trigger reconnection if not already happening
            if (this.shouldReconnect && !this.reconnectTimer && !this.connecting) {
                console.debug("Triggering reconnection due to queued message")
                this.scheduleReconnect()
            }

            return
        }

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
                requestId,
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
     * Flush all queued messages after reconnection.
     */
    private async flushAllQueues(): Promise<void> {

        for (const [commId, queue] of this.sendQueue.entries()) {
            if (queue.length > 0 && !this.pending.has(commId)) {
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
            message,
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
        
        console.debug(
            `Processing queued message for ${commId}.${item.messageId} ` +
            `(${queue.length} remaining in queue)`
        )
        
        // Send it
        this.sendImmediate(commId, item.messageId, item.message, item.requestId, item.callback)
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
        this.requestBorn.delete(requestId)
        
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
        
        // A replay of the comm's last request (RP-1): Python keeps the
        // request_id when it re-sends after a reconnect, so the handler has
        // already run here; send the reply again.
        const last = requestId ? this.lastRequest.get(commId) : undefined
        if (last && last.requestId === requestId) {
            console.debug(`Replayed ${commId}.${messageId} (${requestId}); sending the reply again`)
            if (this.transport) this.transport.send(last.reply)
            return
        }

        // Call handler
        const handler = this.handlers.get(commId)!.get(messageId)!
        let reply: any
        
        try {
            const result = handler(msg.message, this.context)
            reply = {
                comm_id: commId,
                message_id: messageId,
                request_id: requestId,
                message: result,
                direction: 'p2j'
            }
        } catch (e) {
            console.error(`Error in handler ${commId}.${messageId}:`, e)
            this.reportError(e as Error, false)
            reply = {
                comm_id: commId,
                message_id: messageId,
                request_id: requestId,
                message: {error: (e as Error).message},
                direction: 'p2j'
            }
        }

        // Send response if there's a request_id
        if (requestId) {
            this.lastRequest.set(commId, {requestId, reply})
            if (this.transport) this.transport.send(reply)
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

    /**
     * Requests that have not had a reply yet: sent and awaiting one, or
     * queued (behind another request on the same comm, or for replay after
     * a reconnection). Used by the busy indicator to tell a long-running
     * request (keep showing busy) from a busy state nobody will clear.
     */
    inFlight(): number {
        let n = this.pendingRequests.size
        for (const q of this.sendQueue.values()) n += q.length
        return n
    }

    /**
     * True while replies to in-flight requests can still arrive: connected,
     * or connecting / reconnecting automatically. False once shut down, or
     * after reconnection has paused (ERROR) until the tab regains focus or
     * the network returns.
     */
    /**
     * How long (ms) the longest-waiting unanswered request has waited since
     * send() -- 0 if none.  Lets the busy indicator tell a long Plot or
     * redraw (one request waiting a long time) from a stream of short ones
     * such as hover readouts (2026-10-10).  Replays after a reconnection
     * keep their first send time.
     */
    oldestWaitMs(): number {
        const live = new Set<string>(this.pendingRequests.keys())
        for (const q of this.sendQueue.values()) for (const m of q) live.add(m.requestId)
        let oldest = Infinity
        for (const [id, t] of this.requestBorn) {
            if (!live.has(id)) { this.requestBorn.delete(id); continue }
            oldest = Math.min(oldest, t)
        }
        return oldest === Infinity ? 0 : Date.now() - oldest
    }

    canReply(): boolean {
        return this.state === AppState.RUNNING || this.state === AppState.INITIALIZING
    }

    setSharedState(key: string, value: any): void {
        this.sharedState.set(key, value)
        console.debug(`Shared state set: ${key}`)
    }
    
    getSharedState(key: string, defaultValue?: any): any {
        return this.sharedState.has(key) ? this.sharedState.get(key) : defaultValue
    }

    async shutdown(): Promise<void> {
        console.debug("Shutting down CommMgr")

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
        this.requestBorn.clear()
        this.lastRequest.clear()
        this.hideSupersededNotice()
        
        this.state = AppState.STOPPED

        const w = window as any
        if (Array.isArray(w.__cvCommMgrs)) {
            const i = w.__cvCommMgrs.indexOf(this)
            if (i >= 0) w.__cvCommMgrs.splice(i, 1)
        }
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

        console.debug(`Comm initializing: ${this.description?.trim() || this.comm_id} [squash:${this.squash_queue}]`);

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
