import { DataSource } from "@bokehjs/models/sources/data_source"
import * as p from "@bokehjs/core/properties"
import { serialize, deserialize } from "../util/conversions"
import { CallbackLike0 } from "@bokehjs/core/util/callbacks";
import {execute} from "@bokehjs/core/util/callbacks"
import {activeDataPipes} from "./active_data_pipes"

declare global {
    // extend document with our properties
    interface Document { shutdown_in_progress_: boolean }
}

// Data source where the data is defined column-wise, i.e. each key in the
// the data attribute is a column name, and its value is an array of scalars.
// Each column should be the same length.
export namespace DataPipe {
    export type Attrs = p.AttrsOf<Props>

    export type Props = DataSource.Props & {
        init_script: p.Property<CallbackLike0<DataPipe> | null>;
        address: p.Property<[string,number]>;
        instance_id: p.Property<string>;
        conflict_check: p.Property<boolean>
    }
}

export interface DataPipe extends DataPipe.Attrs {}

export class DataPipe extends DataSource {
    declare properties: DataPipe.Props

    static __module__ = "cubevis.bokeh.sources._data_pipe"

    websocket: any
    // used to queue up messages sent to a particular id which already has outstanding
    // messages for which a reply has not been received.
    send_queue: {[key: string]: any} = { }
    // used to queue up messages which are sent BEFORE the connection is completely
    // established. After the connection is established, these message are resent in order.
    connection_queue: [ object, [ string, {[key: string]: any}, (msg:{[key: string]: any}) => any ] ][ ] = [ ]
    pending: {[key: string]: any} = { }
    incoming_callbacks: {[key: string]: any} = { }

    // Session conflict detection properties
    private session_id: string
    private instance_key: string         // unique identifier for this DataPipe purpose
    private session_storage_key: string  // computed storage key
    private heartbeat_interval?: number

    constructor(attrs?: Partial<DataPipe.Attrs>) {
        super(attrs);
        this.session_id = casalib.object_id(this)
        /**********************************************************
        *** With Bokeh 3.0 properties are no longer initialized ***
        *** before the constructor is called...                 ***
        **********************************************************/
    }

    private isColab(): boolean {
        // Check if running in Colab environment
        return (
            typeof (window as any).google !== 'undefined' &&
            typeof (window as any).google.colab !== 'undefined' &&
            typeof (window as any).google.colab.kernel !== 'undefined'
        )
    }

    // @ts-expect-error: debugging colab websocket problems
    private async getWebSocketUrl(): Promise<string> {
        const [host, port] = this.address

        if (this.isColab()) {
            try {
                const google = (window as any).google

                // Get the proxy URL from Colab
                const httpsUrl = await google.colab.kernel.proxyPort(port, {'cache': true})

                // Convert https:// to wss://
                const wssUrl = httpsUrl.replace(/^https:/, 'wss:')

                console.log(`Colab proxy URL for port ${port}: ${wssUrl}`)
                return wssUrl
            } catch (e) {
                console.error('Error getting Colab proxy URL:', e)
                // Fallback to localhost (will fail, but provides error info)
                return `ws://${host}:${port}`
            }
        }

        // Standard WebSocket URL for non-Colab environments
        return `ws://${host}:${port}`
    }

    private checkSessionConflict(): boolean {
        try {
            if (typeof(Storage) === "undefined") {
                console.warn('localStorage not available, skipping session conflict detection')
                return true
            }

            const existing = localStorage.getItem(this.session_storage_key)
            if (existing) {
                const existingData = JSON.parse(existing)
                // Check if another session is active within the last 2 minutes
                if (existingData.sessionId !== this.session_id && 
                    Date.now() - existingData.timestamp < 120000) {

                    if (this.conflict_check) {
                        const message = `CubeVis DataPipe (${this.instance_key}) is already running in another browser window or tab.\n\n` +
                                        'Please close other instances and refresh this page, or\n' +
                                        'close this window to continue using the other instance.'

                        alert(message)

                        if (window.opener || window.history.length === 1) {
                            window.close()
                        } else {
                            window.location.href = 'about:blank'
                        }
                        return false
                    } else {
                        console.group(`DataPipe ${this.instance_key} conflict detected in Jupyter context`);
                        console.log('Current session ID:', this.session_id);
                        console.log('Existing session ID:', existingData.sessionId);
                        console.log('Existing timestamp:', new Date(existingData.timestamp).toISOString());
                        console.log('Age of existing session (ms):', Date.now() - existingData.timestamp);
                        console.log('Address:', this.address);
                        console.log('Instance key:', this.instance_key);
                        console.log('Storage key:', this.session_storage_key);
                        console.log('Existing data:', existingData);
                        console.log('All localStorage keys:', Object.keys(localStorage).filter(k => k.startsWith('cubevis_datapipe_')));
                        console.groupEnd();

                        // In Jupyter, we'll allow it but keep monitoring
                        // The existing session will be overwritten by updateSessionHeartbeat below
                    }
                }
            }

            this.updateSessionHeartbeat()
            return true

        } catch(e) {
            console.warn('Session conflict detection failed:', e)
            return true
        }
    }

    private updateSessionHeartbeat(): void {
        try {
            if (typeof(Storage) !== "undefined") {
                localStorage.setItem(this.session_storage_key, JSON.stringify({
                    sessionId: this.session_id,
                    timestamp: Date.now(),
                    instanceKey: this.instance_key
                }))
            }
        } catch(e) {
            console.warn('Session heartbeat update failed:', e)
        }
    }

    private startHeartbeat(): void {
        // Update timestamp every 30 seconds to keep session "alive"
        this.heartbeat_interval = window.setInterval(() => {
            this.updateSessionHeartbeat()
        }, 30000)
    }

    private stopHeartbeat(): void {
        if (this.heartbeat_interval) {
            clearInterval(this.heartbeat_interval)
            this.heartbeat_interval = undefined
        }
    }

    private cleanupSession(): void {
        try {
            if (typeof(Storage) !== "undefined") {
                const current = localStorage.getItem(this.session_storage_key)
                if (current) {
                    const currentData = JSON.parse(current)
                    if (currentData.sessionId === this.session_id) {
                        localStorage.removeItem(this.session_storage_key)
                    }
                }
            }
        } catch(e) {
            console.warn('Session cleanup failed:', e)
        }
        this.stopHeartbeat()
    }

    private handleSessionConflictMessage(message: any): void {
        console.error('Session conflict detected by server:', message)

        let alertMessage = 'Session conflict detected by server.'

        if (message.type === 'session_conflict') {
            alertMessage = message.error || alertMessage
        } else if (message.type === 'session_corruption') {
            alertMessage = `Session corruption detected.\nExpected: ${message.expected}\nReceived: ${message.received}`
        }

        alert(alertMessage + '\n\nThis window will be closed to prevent data corruption.')

        // Clean up session data
        this.cleanupSession()

        // Trigger custom event for any additional cleanup
        const event = new CustomEvent('cubevis_session_conflict', {
            detail: { message, sessionId: this.session_id }
        })
        window.dispatchEvent(event)

        // Close the window after a brief delay
        setTimeout(() => {
            if (window.opener || window.history.length === 1) {
                window.close()
            } else {
                window.location.href = 'about:blank'
            }
        }, 2000)
    }

    private generateInstanceKey(): string {
        // Create unique key based on address and optional purpose
        const addressKey = `${this.address[0]}_${this.address[1]}`

        // Could extend this to include purpose/context if needed
        // For example, if you pass a 'purpose' property in attrs:
        // const purpose = this.attrs.purpose || 'default'
        // return `${addressKey}_${purpose}`

        return `${this.instance_id}_${addressKey}`
    }

    private async initializeWebSocket(): Promise<void> {

        let ws_address: string

        if (this.isColab()) {
            const [host, port] = this.address

            console.log('=== Colab WebSocket Debug ===')
            console.log('Port:', port)
            console.log('window.location.hostname:', window.location.hostname)
            console.log('window.location.href:', window.location.href)
            console.log('document.referrer:', document.referrer)
            console.log('window.location.ancestorOrigins:', window.location.ancestorOrigins)

            // Try to get the proxy URL
            try {
                const google = (window as any).google
                const httpsUrl = await google.colab.kernel.proxyPort(port, {'cache': true})
                console.log('proxyPort returned:', httpsUrl)

                // Try multiple patterns
                const patterns = [
                    httpsUrl.replace(/^https:/, 'wss:'),  // Direct WSS
                    httpsUrl.replace(/^https:/, 'wss:').replace(/\/$/, '') + '/ws',  // With /ws path
                    httpsUrl.replace(/^https:/, 'wss:').replace(/\/$/, '') + '/websocket',  // With /websocket path
                ]

                console.log('Will try these WebSocket URLs:', patterns)
                ws_address = patterns[0]  // Start with first one

            } catch (e) {
                console.error('Error in Colab proxy detection:', e)
                ws_address = `ws://${host}:${port}`
            }

            console.log('=== End Debug ===')
        } else {
            ws_address = `ws://${this.address[0]}:${this.address[1]}`
        }

        //const ws_address = await this.getWebSocketUrl()
        console.log("datapipe url:", ws_address)

        var reconnections: any | undefined = undefined
        document.shutdown_in_progress_ = false

        var connect_to_server = ( ) => {
            if ( this.websocket !== undefined ) {
                this.websocket.close( )
            }

            this.websocket = new WebSocket(ws_address)
            this.websocket.binaryType = "arraybuffer"

            this.websocket.addEventListener("error", (e: Event) => {
                console.log( 'error encountered:', e )
            })

            this.websocket.onmessage = (event: any) => {
                if (typeof event.data === 'string' || event.data instanceof String) {
                    let data = deserialize( event.data )
                    // @ts-ignore: 'data' is of type 'unknown'
                    if ( 'id' in data && 'direction' in data && 'message' in data ) {
                        // @ts-ignore: 'data' is of type 'unknown'
                        let { id, message, direction }: { id: string, message: any, direction: string} = data

                        // Handle session conflict/corruption messages from server
                        if (direction === 'error' && (id === 'session_conflict' || id === this.session_id)) {
                            if (message && (message.type === 'session_conflict' ||
                                          message.type === 'session_corruption' ||
                                          message.action === 'close_duplicate')) {
                                this.handleSessionConflictMessage(message)
                                return
                            }
                        }

                        if ( typeof message  === 'undefined' ) {
                            console.log( 'Error, event failure', data )
                        }
                        if ( direction == 'j2p' ) {
                            if ( id in this.pending ) {
                                let { cb }: { cb: (x:any) => any } = this.pending[id]
                                delete this.pending[id]
                                if ( id in this.send_queue && this.send_queue[id].length > 0 ) {
                                    // send next message queued by 'id'
                                    let {cb, msg} = this.send_queue[id].shift( )
                                    this.pending[id] = { cb }
                                    this.websocket.send(serialize(msg))
                                }
                                if ( typeof message === 'undefined' )
                                    console.log( 'DROPPING ERROR FOR NOW (maybe need error callbacks)', data )
                                else
                                    // post message
                                    cb( message )
                            } else {
                                console.log("message received but could not find id")
                            }
                        } else {
                            if ( id in this.incoming_callbacks ) {
                                let result = this.incoming_callbacks[id](message)
                                this.websocket.send( serialize({ id, direction, message: result, session: this.session_id }))
                            }
                        }
                    } else {
                        console.log( `datapipe received message without one of 'id', 'message' or 'direction': ${data}` )
                    }

                } else {
                    console.log("datapipe received binary data", event.data.byteLength, "bytes" )
                }
            }

            this.websocket.onopen = ( ) => {
                if ( ! reconnections ) {
                    this.websocket.send(serialize({ id: 'initialize', direction: 'j2p', session: this.session_id }))
                    // Start heartbeat after successful connection
                    this.startHeartbeat()
                } else if ( reconnections.connected == false ) {
                    console.log( `connection reestablished at ${new Date( )}` )
                }
                reconnections = new (casalib.ReconnectState as any)( )

                // if there were send events before the websocket was connected, resend them
                while ( this.connection_queue.length > 0 ) {
                    let state = this.connection_queue.shift( )!
                    this.send.apply( state[0], state[1] )
                }
            }

            this.websocket.onclose = ( ) => {
                if ( reconnections && reconnections.connected == true ) {
                    console.log( `connection lost at ${new Date( )}` )
                    reconnections.connected = false
                    if ( ! document.shutdown_in_progress_ ) {
                        console.log( `connection lost at ${new Date( )}` )
                        var recon = reconnections
                        function reconnect( tries: number ) {
                            if ( reconnections.connected == false ) {
                                console.log( `${tries+1}\treconnection attempt ${new Date( )}` )
                                connect_to_server( )
                                recon.backoff( )
                                if ( recon.retries > 0 ) {
                                    setTimeout(reconnect, recon.timeout, tries+1)
                                } else if ( reconnections.connected == false ) {
                                    console.log( `aborting reconnection after ${tries} attempts ${new Date()}` )
                                }
                            }
                        }
                        reconnect( 0 )
                    }
                }
            }
        }

        // Set up cleanup on page unload
        window.addEventListener('beforeunload', () => {
            this.cleanupSession()
        })

        // Set up cleanup on page visibility change (tab switching)
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') {
                // Don't clean up immediately on tab switch, just stop heartbeat temporarily
                this.stopHeartbeat()
            } else if (document.visibilityState === 'visible') {
                // Resume heartbeat when tab becomes visible again
                this.updateSessionHeartbeat()
                this.startHeartbeat()
            }
        })

        /**********************************************
        *** initial connection to python websocket  ***
        **********************************************/
        connect_to_server()
    }

    initialize(): void {
        super.initialize();
        activeDataPipes.register( this );

        // Generate instance key based on address and purpose
        // This allows multiple DataPipes for different purposes
        this.instance_key = this.generateInstanceKey()
        this.session_storage_key = `cubevis_datapipe_${this.instance_key}`

        // Check for session conflicts before initializing websocket
        if (!this.checkSessionConflict()) {
            return // Don't initialize if session conflict detected
        }

        // Start async WebSocket initialization
        this.initializeWebSocket()

        //
        // Run any initialization script
        //
        const _execute = () => {
            if ( this.init_script != null ) void execute( this.init_script, this )
        }
        _execute( )
    }

    destroy( ): void {
        activeDataPipes.unregister(this)
        super.destroy( )
    }

    register( id: string, cb: (msg:{[key: string]: any}) => any ): void {
        this.incoming_callbacks[id] = cb
    }

    send( id: string, message: {[key: string]: any}, cb: (msg:{[key: string]: any}) => any, squash_queue: boolean | ((msg:{[key: string]: any}) => boolean) = false ): void {
        let msg = { id, message, direction: 'j2p', session: this.session_id }
        // queue message if:
        //    (1) websocket is not yet initialized
        //    (2) a result indicated by id is pending
        if ( ! this.websocket || id in this.pending ) {
            if ( id in this.send_queue ) {
                if ( typeof squash_queue == 'boolean' && squash_queue && this.send_queue[id].length > 0 ) {
                    // throw away existing message if squash_queue is true
                    this.send_queue[id][0].msg = msg
                    this.send_queue[id][0].cb = cb
                } else if (typeof squash_queue == 'function' && this.send_queue[id].length > 0 ) {
                    // use predicate to attempt to find queued message to replace
                    let found = false
                    for ( const elem of this.send_queue[id] ) {
                        if ( squash_queue( elem.msg.message ) ) {
                            // throw away message selected by squash_queue predicate
                            elem.msg = msg
                            elem.cb = cb
                            found = true
                        }
                    }
                    if ( ! found ) {
                        // queue message
                        this.send_queue[id].push( { cb, msg } )
                    }
                } else {
                    // queue message
                    this.send_queue[id].push( { cb, msg } )
                }
            } else {
                this.send_queue[id] = [ { cb, msg } ]
            }
        } else {
            if ( this.websocket.readyState === WebSocket.CONNECTING ) {
                // connection not yet established yet...
                this.connection_queue.push( [ this, [ id, message, cb ] ] )
            } else if ( id in this.send_queue && this.send_queue[id].length > 0 ) {
                this.send_queue[id].push( { cb, msg } )
                {   // seemingly cannot reference wider 'cb' and the block-scoped
                    // 'cb' within the same block...
                    // src/bokeh/sources/data_pipe.ts:100:45 - error TS2448: Block-scoped variable 'cb' used before its declaration.
                    let { cb, msg } = this.send_queue[id].shift( )
                    this.pending[id] = { cb }
                    if ( this.websocket.readyState === WebSocket.OPEN )
                        this.websocket.send(serialize(msg))
                    else {
                        let countdown = 20
                        let pipe = this
                        function resend( ) {
                            if ( pipe.websocket.readyState === WebSocket.OPEN )
                                pipe.websocket.send(serialize(msg))
                            else {
                                countdown = countdown - 1
                                if ( countdown > 0 ) setTimeout( resend, 3000 )
                            }
                        }
                        setTimeout( resend, 3000 )
                    }
                }
            } else {
                if ( this.websocket.readyState === WebSocket.OPEN ) {
                    this.pending[id] = { cb }
                    this.websocket.send(serialize(msg))
                } else {
                    let countdown = 20
                    let pipe = this
                    function resend( ) {
                        if ( pipe.websocket.readyState === WebSocket.OPEN ) {
                            pipe.pending[id] = { cb }
                            pipe.websocket.send(serialize(msg))
                        } else {
                            countdown = countdown - 1
                            if ( countdown > 0 ) setTimeout( resend, 3000 )
                        }
                    }
                    setTimeout( resend, 3000 )
                }
            }
        }
    }

    static {
        this.define<DataPipe.Props>(({ Any, Tuple, String, Number, Bool }) => ({
            init_script: [ Any, null ],
            address: [Tuple(String,Number)],
            instance_id: [ String ],
            conflict_check: [ Bool, true ]
        }))
    }
}
