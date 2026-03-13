declare global {
    var casalib: {
        debounce: (func: () => void, delay: number) => { (): void; cancel(): void };
        object_id: ( obj?: { [key: string]: any } | null ) => string;
        ReconnectState: () => { timeout: number; retries: number; connected: boolean; backoff: () => void };
        coordtxl: any;
        d3: any;
    };
}

export {};
