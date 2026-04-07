declare global {
    interface Window {
        // The Jupyter or Colab Comm object is injected from Python using an `anywidget` and
        // passed along (whenever possible) as `cubevis_${comm_id}`. In Colab, the `window` is
        // unique to each cell.
        [key: `cubevis_${string}`]: { [key: string]: any } | undefined;
        // Catch-all to satisfy the compiler for dynamic string indexing
        [key: string]: any;
    };
    var casalib: {
        debounce: (func: () => void, delay: number) => { (): void; cancel(): void };
        object_id: (obj: { [key: string]: any }) => string;
        ReconnectState: () => { timeout: number; retries: number; connected: boolean; backoff: () => void };
        coordtxl: any;
        d3: any;
    };
    namespace google {
        namespace colab {
            namespace kernel {
                const comms: any;
            }
        }
    }
}

export {};
