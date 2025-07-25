
//export function map<T>( func: (arg: [string,any] | any) => void, container: T | T[] ): void {
export function map<T>( func: ((key: string, val: any, index:number) => void) | ((val: T,index: number,arr: T[]) => void), container: T | T[] ): void {
    if ( Array.isArray(container) )
        // @ts-ignore : who knows how to check if this is a function of type: (val: T,index: number,arr: T[]) => void
        container.map( func )
    else if ( typeof container === 'object' && container !== null )
        // @ts-ignore : who knows how to check if this is a function of type: (key: string, val: T) => void
        Object.entries(container).map( (e) => func(e[0],e[1]) )
    else
        throw new Error( `casalib.map applied to ${typeof(container)}` )
}

export function reduce<T,U>( func: ((acc: U, key: string, val: any, index:number) => void) | ((acc: U, val: T,index: number, arr: T[]) => void), container: T | T[], initial: U ): void {
    if ( Array.isArray(container) )
        // @ts-ignore : who knows how to check if this is a function of type: (val: T,index: number, arr: T[]) => void
        return container.reduce( func, initial )
    else if ( container instanceof Set ) {
        // @ts-ignore : who knows how to check if this is a function of type: (val: T,index: number, arr: T[]) => void
        return [...container].reduce( func, initial )
    } else if ( typeof container === 'object' && container !== null )
        // @ts-ignore : who knows how to check if this is a function of type: (key: string, val: T) => void
        return Object.entries(container).reduce( (accum,e,i) => func(accum,e[0],e[1],i), initial)
    else
        throw new Error( `casalib.reduce applied to ${typeof(container)}` )
}

export function debounce(func: () => void, delay: number) {
  let timeoutId: number | null = null;

  // The main debounced function
  const debouncedFunction = function() {
    // Cancel any pending execution
    if (timeoutId !== null) {
      clearTimeout(timeoutId);
    }

    // Schedule new execution
    timeoutId = window.setTimeout(() => {
      func();
      timeoutId = null;
    }, delay);
  };

  // Add a cancel method to the function
  debouncedFunction.cancel = function() {
    if (timeoutId !== null) {
      clearTimeout(timeoutId);
      timeoutId = null;
    }
  };

  return debouncedFunction;
}
