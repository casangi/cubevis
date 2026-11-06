/**
 * The coordinate description of an image cube (cubevis.imaging._coordinates,
 * design §2.5): plain data that does not depend on the image format, sent by
 * ImagePipe as `coords_json`.  It replaces the image's FITS header
 * (`fits_header_json`, until 2026-11-06).
 *
 * `description_keywords` gives coordtxl's WCSTransform the keywords it reads,
 * through its existing MapKeywordProvider, so that coordtxl itself is
 * unchanged.  A coordtxl that takes the description directly (with ICRS and a
 * spectral axis) can replace this later.
 *
 * Kept free of imports so that tests can compile it alone and run it under
 * node with the coordtxl package (tests/manual/iclean-imaging/coordtxl_parity.mjs).
 */

export type Direction = {
    frame: string
    equinox: number | null
    projection: string
    projection_parameters: number[]
    reference_value: [number, number]          // degrees
    reference_pixel: [number, number]          // pixel_origin based
    increment: [number, number]                // degrees
    pc: [[number, number], [number, number]]   // FITS convention: pc[i][j] = PC(i+1)_(j+1)
    lonpole: number | null
    latpole: number | null
    native_frame?: string
}

export type Spectral = {
    frame: string
    unit: string
    values: number[]
    rest_frequency: number | null
    velocity_convention: string
}

export type CoordinateDescription = {
    version: number
    shape: number[]
    pixel_origin: number
    direction: Direction | null
    spectral: Spectral | null
    stokes: string[]
    beam: {[key: string]: any} | null
    brightness_unit: string
    extra: {[key: string]: any}
}

/** FITS longitude/latitude axis names for a frame */
export function celestial_axis_names( frame: string ): [string, string] {
    const f = (frame || "").toUpperCase( )
    if ( f.startsWith("GAL") ) return [ "GLON", "GLAT" ]
    if ( f.startsWith("ECL") ) return [ "ELON", "ELAT" ]
    if ( f.startsWith("SUPERGAL") ) return [ "SLON", "SLAT" ]
    return [ "RA", "DEC" ]
}

/** "RA---SIN", "DEC--SIN", "GLON-SIN" */
export function fits_ctype( name: string, projection: string ): string {
    let padded = name
    while ( padded.length < 4 ) padded += "-"
    return `${padded}-${projection}`
}

/**
 * The keywords coordtxl's WCSTransform reads, from a description:
 * NAXIS1/2, CTYPE1/2, CRVAL1/2, CRPIX1/2 (1-based), EQUINOX, and either
 * CDELT1/2 (no rotation) or the CD matrix (CD = diag(CDELT) * PC).  The
 * reference frame is given by EQUINOX, as the FITS header did (coordtxl reads
 * RADECSYS, which CASA's header does not write: it writes RADESYS): 2000 ->
 * FK5 (ICRS is treated as FK5/J2000 by coordtxl), 1950 -> FK4.
 */
export function description_keywords( desc: CoordinateDescription ): {[key: string]: number | string} {
    const d = desc.direction
    if ( d == null ) return { }
    const [ lon, lat ] = celestial_axis_names( d.frame )
    const origin = desc.pixel_origin ?? 0
    const result: {[key: string]: number | string} = {
        NAXIS1: desc.shape[0],
        NAXIS2: desc.shape[1],
        CTYPE1: fits_ctype( lon, d.projection ),
        CTYPE2: fits_ctype( lat, d.projection ),
        CRVAL1: d.reference_value[0],
        CRVAL2: d.reference_value[1],
        CRPIX1: d.reference_pixel[0] + 1 - origin,
        CRPIX2: d.reference_pixel[1] + 1 - origin,
        EQUINOX: d.equinox ?? 2000.0,
    }
    const pc = d.pc
    const identity = pc[0][0] === 1 && pc[0][1] === 0 && pc[1][0] === 0 && pc[1][1] === 1
    if ( identity ) {
        result.CDELT1 = d.increment[0]
        result.CDELT2 = d.increment[1]
    } else {
        result.CD1_1 = d.increment[0] * pc[0][0]
        result.CD1_2 = d.increment[0] * pc[0][1]
        result.CD2_1 = d.increment[1] * pc[1][0]
        result.CD2_2 = d.increment[1] * pc[1][1]
    }
    return result
}

/** the spectral value (in `spectral.unit`) of a channel, or null */
export function spectral_value( desc: CoordinateDescription, chan: number ): number | null {
    const s = desc.spectral
    if ( s == null || chan < 0 || chan >= s.values.length ) return null
    return s.values[chan]
}
