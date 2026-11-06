// Build coordtxl WCSTransforms for each case in CASES.json two ways -- from the
// FITS header keyword map the browser received until 2026-11-06 (the old
// `fits_header_json`) and from the coordinate description through
// `description_keywords` (the compiled coordinate_description.ts) -- and
// write pixel -> world for each case's pixels, and the formatted strings
// the GUI shows, to OUT.json.
//
//   node coordtxl_parity.cjs COORDTXL_DIR COMPILED_DESCRIPTION_JS CASES.json OUT.json
//
// Run by coordinates_parity.py, which makes the cases with casatools and
// compares the results with casatools' own pixel -> world.
const fs = require('fs')
const [ coordtxlDir, describeJs, casesPath, outPath ] = process.argv.slice(2)
const coordtxl = require(coordtxlDir + '/coordtxl.js')
const { description_keywords } = require(describeJs)

function transform( keywords ) {
    try {
        return new coordtxl.WCSTransform( new coordtxl.MapKeywordProvider( keywords ) )
    } catch ( e ) {
        return { error: String(e) }
    }
}

function world( wcs, pixels ) {
    if ( wcs.error ) return { error: wcs.error }
    const out = [], text = []
    for ( const [ x, y ] of pixels ) {
        const pt = new coordtxl.Point2D( x, y )
        wcs.imageToWorldCoords( pt, false )
        out.push( [ pt.getX( ), pt.getY( ) ] )
        text.push( new coordtxl.WorldCoords( pt.getX( ), pt.getY( ) ).format( 2000.0 ) )
    }
    return { world: out, text }
}

const cases = JSON.parse( fs.readFileSync( casesPath, 'utf8' ) )
const result = { }
for ( const [ name, c ] of Object.entries( cases ) ) {
    const keywords = description_keywords( c.description )
    result[name] = {
        keywords,
        header: world( transform( c.header_map ), c.pixels ),
        description: world( transform( keywords ), c.pixels ),
    }
}
fs.writeFileSync( outPath, JSON.stringify( result ) )
