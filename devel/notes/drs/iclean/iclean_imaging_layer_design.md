# iclean on remote kernels: an imaging layer (design proposal, 2026-11-04)

Revises section (b) of `devel/notes/drs/visplot/hrs_handoff_2026-11-04_iclean_remote.md`.
The handoff puts two separate objects in the worker, a `gclean` and an
`ImageAccess` per field, and has the GUI talk to each of them. This proposal
puts one **imaging layer** between the GUI and everything that runs on the
data's host. That layer combines running tclean (gclean) with access to the
residual and mask images. It is the boundary that gets replaced, whether by a
remote proxy or by an RADPS/xradio implementation.

## 1. Why one layer and not two objects

The code shows that the GUI's view of "the images" and its view of "the clean"
are not independent of each other:

* **Freshness.** `ImagePipe.__get_chan` finds out whether tclean rewrote the
  residual by polling directory ctimes (`newest_ctime`). An object that runs
  the cycle *and* serves the residual knows this directly: it increments a
  generation counter when `run_cycle()` returns. No filesystem polling is
  needed, local or remote.
* **Mask lifecycle.** For the "no mask cube yet" (breadcrumbs) path,
  `clean_handler` (`_interactive_clean_ui.py` ~604) does
  `shutil.rmtree("<imagename>.mask")` **on the laptop's filesystem**. It then
  converts the drawn regions using `coorddesc()` (a casatools `coordsys`) and
  passes them to `gclean.update(mask=...)`. Remotely, the rmtree would point
  at the wrong machine. Deleting and recreating the mask belongs to the
  imaging side: `set_mask_regions(field, regions)`.
  *Found in step 1 (2026-11-04, notes §60):* that branch never ran -- the
  browser sends `masks`, not `mask`, and the branch used attributes that
  were never set (`_imagename`, `_mask_history`).  It was removed;
  `set_mask_regions` waits until masks drawn without a mask cube are
  wanted (it is not in step 1's protocol).
* **Convergence vocabulary.** The UI imports
  `casatasks...imager_return_dict.ImagingDict` (lines 409, 586) only for
  `get_summaryminor_stopdesc`. That is backend knowledge and should be served
  by the backend (`stop_descriptions()`).
* **Field naming.** The UI gets paths from `image_products()` and joins them
  itself (`imagepath`/`residualname`/`maskname`). With a layer, the UI deals
  only in **field ids**. Paths become information shown to the user, and stay
  on the host.
* **Independence.** gclean was meant to be the API that RADPS/xradio could
  implement. In practice, an alternative imager also produces images in its own
  format (zarr, xarray), so replacing gclean alone isn't enough. The image
  reader has to be replaced too. The layer is the unit that gets implemented.

Once those four things move behind the layer, **the laptop side needs neither
casatasks nor casatools**, the same as visplot's GUI. This is still to be
verified: see §5.

## 2. Layers

```
laptop (GUI, Bokeh, no CASA)                         data's host (or same process, when local)
------------------------------------------           -----------------------------------------------
InteractiveCleanUI ---+
CubeMask -- ImagePipe-+-- ImagingSession  ========>  LocalImagingSession
             (Bokeh model,    (protocol)    remote:     |-- Deconvolver   : casatasks gclean | RADPS | stub
              quant state)                   proxy      |-- ImageCube x field (shared numpy logic)
                                                        |      `-- ImageStore : casatools  | (later) xarray via arcae
```

The local case uses the **same** `LocalImagingSession` in-process. "Remote"
only means that a proxy stands in front of it, as in visplot. Local and remote
then run the same image code, and the parity test compares like with like.

### 2.1 `ImagingSession` (the wire boundary; one remote handle per iclean)

```python
class ImagingSession(Protocol):
    # iteration -- today's gclean surface, as plain methods with plain-data results
    def start(self) -> CycleResult            # next(gclean): the dirty image
    def update(self, params: dict) -> tuple[int, str]
    def run_cycle(self) -> CycleResult         # one major cycle + its minor cycles (minutes)
    def has_next(self) -> bool
    def log(self, history: bool = False) -> list[str]
    def restore(self) -> dict                  # tclean's return dict, wire-safe
    def stop_descriptions(self) -> dict        # replaces the ImagingDict import in the UI
    # images
    def fields(self) -> list[dict]             # id, imagename, host paths (informational)
    def cube(self, field: str) -> "ImageCube"  # local: the object; remote: a proxy bound to field
    def set_mask_regions(self, field: str, regions) -> None   # deferred (see §1, mask lifecycle)
    def close(self) -> None
```

`CycleResult` holds `stopdesc, stopcode, majordone, majorleft, iterleft,
convergence`, all plain data. Check that it and `restore()` round-trip
through `remote_serialize`.

### 2.2 `ImageCube` (per field: residual + mask + mask0; written once, pure numpy)

```python
shape; generation                          # generation bumps after each run_cycle
coordinates()                              # the coordinate description, §2.5 (replaces fits_header/coorddesc)
channel_reply(index, quant, *, stats: bool, hist_bins: int|None) -> dict
     # uint8 plane, msk, msk0, statistics, histogram: ONE round trip per channel step
pixel_value(index, xy); mask_value(index, xy)
spectrum(xy_stokes, with_mask: bool)
statistics(index, use_mask: bool)
edit_mask(op, scope, index, *, polygon=None, src=None) -> None
     # op: add|subtract|clear|invert; scope: chan|cube
has_mask(); has_mask0(); reopen_mask(path)
```

Two changes compared with what `ImagePipe` does today:

* **Quantization state moves out.** The `adjust-colormap` bounds and transfer
  function describe what one viewer sees. `ImagePipe` keeps them and passes
  them in `channel_reply(quant=...)`. The cube stays stateless with respect to
  display.
* **Mask edits become operations.** `CubeMask.mod_mask` (`_cube.py`
  ~383–500) loops `mask()`/`put_mask()` over every channel for `scope ==
  'cube'`. Remotely, that is 2×nchan round trips for a single click.
  `edit_mask` does the whole operation where the mask is stored, and it ships
  the polygon rather than the pixel indices (`polygon_indexes` needs only the
  shape). This is the largest change on the GUI side, and it's worth making
  even for local use.

#### 2.2.1 As done (step 2, 2026-11-07, notes §65)

* `ImageCube.edit_mask(op, scope, index, *, polygon=None, src=None) -> dict`,
  in `cubevis/imaging/_masks.py`: ops `add` / `subtract` (a region's
  vertices, or `src`, another channel's mask), `clear`, `fill`, `invert`;
  scopes `chan` / `cube` (every channel of `index`'s stokes).  Returns
  `{'clear_region': True}` when a channel's own mask was subtracted from it.
  The polygon is rasterized where the mask is (`polygon_pixels`,
  vectorized; `cubevis.utils.polygon_indexes` calls it).
* Cube scope uses the optional store primitives `read_mask_chans` /
  `write_mask_chans` (casatools: getchunk/putchunk over a channel range) in
  chunks of at most 64 MB, else plane by plane.
* `CubeMask`'s `mask-mod` handler maps the browser's messages onto it;
  the browser is unchanged.  Before/after: identical mask cubes and replies.
* Pixel selection and the mask outline follow §2.5.2's grid (step 2b,
  notes §66): a region selects the pixels whose **centre** it holds (every
  shape, the last column/row included; a region holding no centre selects the
  pixel under its middle), and the browser's outline (d3-contour at threshold
  0.5) runs along the pixel edges.  Before, polygons tested pixel corners and
  the outline went through the edge pixels' centres: together, the
  one-sided off-by-one-pixel outline.

### 2.3 `ImageStore` (primitives; this is what a new image format implements)

```python
shape; coordinates()                            # §2.5; the store builds it from its own metadata
read_plane(stokes, chan) -> np.ndarray          # float
read_spectrum(x, y, stokes) -> np.ndarray
read_mask_plane(stokes, chan); write_mask_plane(stokes, chan, plane)
read_mask_chans(stokes, start, count); write_mask_chans(stokes, start, planes)   # optional (§2.2.1)
read_default_mask() -> np.ndarray | None        # mask0
open_mask(path) / close()
statistics(stokes, chan, mask_path|None) -> dict   # optional; ImageCube has a numpy default
```

`CasatoolsImageStore` is lifted from `ImagePipe` (getchunk/putchunk, the
CAS-13625 one-use `ia.statistics`, the mask0 cache). The later
**xarray store over arcae** (the xarray-ms analogue for CASA images) only has
to provide these primitives. Statistics then fall back to numpy; flux needs
the beam, which comes from the coordinate description. The image-side
dependency on casatools leaves with that store; tclean's doesn't.
`fits_header()` is kept as an *optional* extra on stores that have one,
for display and export only. Nothing in the GUI depends on it.

### 2.4 `Deconvolver`

Today's gclean surface, unchanged, resolved from `iclean_backend` ("PROD" →
`casatasks.private.imagerhelpers._gclean`, or `_gclean_NAME`) on the host.
`LocalImagingSession` builds a `CasatoolsImageStore` per entry in
`image_products()`.

An RADPS/xradio imager can plug in at either level:
* **Deconvolver only**, if its products can be read by an existing store.
* **A whole `ImagingSession`**, if it has its own image format, or if it wants
  to do mask edits itself (for example, masks kept as regions rather than
  as a cube).

### 2.5 Coordinates: a description, not a FITS header

Today the world coordinates have three separate sources, and each of them is
specific to casatools:

* **Browser.** `ImagePipe.fits_header_json` ← `ia.fitsheader()`. In
  `image_pipe.ts` it goes `JSON.parse` → `coordtxl.MapKeywordProvider` →
  `coordtxl.WCSTransform`, which the axis ticks (`wcs_ticks.ts`), the
  cursor/pixel tooltip and region coordinates (`_cube.py` ~1603, ~3028) use.
  coordtxl reads only the celestial FITS keywords (`NAXIS1/2`, `CTYPE`,
  `CRVAL`, `CRPIX`, `CDELT`, `CROTA`/`CD`, `EQUINOX`, `EPOCH`, `RADECSYS`), and the
  spectrum plot's x axis is the channel *index* (`_image_pipe.py`
  `spectrum`: "should return spectral freq etc.").
* **Python, regions.** `coorddesc()` → casatools `coordsys` →
  `convert_masks(coord='world')` → `__get_wcs_object`, an astropy `WCS`
  built from `csys.referencepixel/increment/referencevalue`, with
  `RA---SIN` hard-coded.
* **Python, axes.** `bokeh/utils/_axes_labels.get_world_coordinates_wcs`
  does the same thing again.

A FITS header is a CASA export format. xradio/RADPS images carry their
coordinates as xarray coordinates and attributes (`l`, `m`, `frequency`,
`polarization`, plus direction reference and projection attributes). So
`ImageStore.coordinates()` returns a **coordinate description**: plain data
(JSON-safe) that doesn't depend on the backend, versioned, and built once per
cube:

```python
{
  "version": 1,
  "shape": [nx, ny, nstokes, nchan],          # display order, as today
  "pixel_origin": 0,                          # explicit: FITS CRPIX is 1-based
  "direction": {
      "frame": "ICRS" | "FK5" | "FK4" | "GALACTIC" | ...,
      "equinox": 2000.0 | None,
      "projection": "SIN", "projection_parameters": [...],
      "reference_value": [lon_deg, lat_deg],
      "reference_pixel": [x, y],
      "increment": [dlon_deg, dlat_deg],
      "pc": [[1, 0], [0, 1]],                   # rotation / skew, general form
      "lonpole": None, "latpole": None,
  },
  "spectral": {                                # None for an image with no spectral axis
      "frame": "LSRK", "unit": "Hz",
      "values": [...],                         # per channel: covers tabular axes and xradio's coordinate arrays
      "rest_frequency": None | Hz, "velocity_convention": "radio",
  },
  "stokes": ["I", "Q", "U", "V"],              # replaces stokes_labels()
  "beam": {"major": arcsec, "minor": arcsec, "pa": deg} | {"per_plane": [...]} | None,
  "brightness_unit": "Jy/beam",
  "extra": {...},                              # backend-specific, informational (telescope, date, ...)
}
```

How each store builds it:
* **casatools**: from `ia.coordsys()` (reference value, pixel and
  increment, projection, direction reference) and `cs.toworld` over the
  spectral axis, plus `ia.restoringbeam()` and `ia.brightnessunit()`.
* **xradio / arcae**: from the image's `l`/`m` coordinates and direction
  attributes (increment from neighbouring values, reference pixel where
  `l = m = 0`), with `frequency.values` and `polarization.values` used
  directly.

How it is used:
* **Browser.** `ImagePipe.coords_json` replaces `fits_header_json`.
  `image_pipe.ts` builds the `WCSTransform` through a new
  `DescriptionKeywordProvider` that implements coordtxl's existing
  `WCSKeywordProvider` interface (description → the keywords coordtxl
  reads; 0-based → 1-based pixels; ICRS → FK5/J2000). That step needs **no
  change to coordtxl itself**. The "modified coordtxl" can then come in two
  steps:
  1. A constructor that takes the description directly, plus the general
     `pc` matrix and ICRS as a frame of its own.
  2. A spectral axis: channel ↔ frequency/velocity by lookup on
     `spectral.values`. Done in cubevisjs or coordtxl, this gives the
     spectrum plot and the channel label real units.
* **Python.** One helper, `astropy_wcs(description)`, replaces both
  `__get_wcs_object` and `get_world_coordinates_wcs` (and removes the
  hard-coded SIN). `convert_masks(..., cdesc=)` takes the description in
  place of a `coordsys`. Writing CRTF uses the description's frame names,
  so a backend that doesn't use CASA regions still gets sensible world
  regions.

Check: for the test images, the browser world coordinates from description →
provider → coordtxl must equal today's results from FITS header → coordtxl
(ticks, cursor readout, region corners), and the astropy WCS must match the
current one. This comparison is part of step 1's tests.

#### 2.5.1 As done (step 1b, 2026-11-06, notes §63)

* `cubevis/imaging/_coordinates.py`: `from_casa_coordsys(record, shape,
  spectral_values=, beam=, brightness_unit=)` and `astropy_wcs(desc,
  naxis=4)`; `CasatoolsImageStore.coordinates()` (spectral values from
  `toworldmany`: exact for tabular/non-linear axes), `ImageCube.coordinates()`
  (made once).  `pc` follows FITS (`pc[i][j]` = `PC(i+1)_(j+1)`, i the world
  axis): **the casatools record's `pc` is the transpose** (column-major).
  Frames: J2000/JMEAN/JTRUE → FK5 2000, ICRS → ICRS, B1950(_VLA)/BMEAN/BTRUE
  → FK4 1950, GALACTIC, ECLIPTIC, SUPERGAL → SUPERGALACTIC, others by name.
* Browser: `ImagePipe.coords_json` replaces `fits_header_json`;
  `cubevisjs/src/bokeh/util/coordinate_description.ts` (no imports)
  `description_keywords(desc)` gives coordtxl's `MapKeywordProvider`:
  `NAXIS1/2`, `CTYPE1/2`, `CRVAL1/2`, `CRPIX1/2` (+1), `EQUINOX` (null → 2000),
  and `CDELT1/2` when `pc` is the identity, else the `CD` matrix
  (`diag(CDELT)·PC`; coordtxl ignores `PC`, and reads `CD` only without
  `CDELT1`).  No `RADECSYS`: the header had `RADESYS`, which coordtxl does not
  read, so the frame came from `EQUINOX` and still does.
  `ImagePipe.coordinates()` (TS) gives the parsed description (spectral
  values for a later spectral axis).  coordtxl unchanged.
* Python: `astropy_wcs` replaces `__get_wcs_object` (`convert_masks`,
  `coord='world'`) and the dead `bokeh/utils/_axes_labels.py` (deleted);
  createregion's `RegionList` gets `astropy_wcs(cube.coordinates())`.
  `ImageCube.coorddesc()` keeps `csys` (the casatools `coordsys`, which
  `format='region'` needs) and adds `coordinates`.
* Findings about coordtxl 0.1.4/0.1.5, the same before and after (for the
  "modified coordtxl"):
  1. `WCSTransform` takes the projection from `CTYPE1` with
     `wcstemp.substring(3, 7)` -- `"--SI"` for `RA---SIN` -- which matches
     none of `-SIN`, `-TAN`, ...; every image is treated as **linear** (no
     1/cos(dec) in RA: ~190" at the corners of the 128×128, 12"/pixel test
     image at dec +40.7°).  **Fixed in coordtxl 0.1.6 (§2.5.2).**
  2. FK4/B1950 (seen only once 1 is fixed): ~82° off, both ways --
     coordtxl's FK4 conversion.  **Open** (with the coordtxl feature work).
  3. Pixels are 1-based (FITS); the GUI passed 0-based pixel indices.
     **Fixed (§2.5.2).**
  4. The FITS header's `NAXIS` array became `NAXIS1 = 4` (the number of
     axes) in `MapKeywordProvider`; the description gives the true size.
     coordtxl's pixel → world does not depend on it.

#### 2.5.2 Coordinate defects fixed (2026-11-06, notes §64)

* **coordtxl 0.1.6** (`WCSTransform.java`): the projection is the three
  letters after the run of dashes that follows the axis name (`RA---SIN`,
  `DEC--SIN`, `GLON-SIN`, `RA---TAN-SIP` → `-TAN`), not characters 3..6.
  `ProjectionTypeTest` (new) and the expected label in `CasaImageLabelTest`
  / `tstest.ts` (now the SIN value, checked with astropy).  Published;
  casalib takes `coordtxl ^0.1.6`, and its bundle
  (`cubevis/__js__/casalib.min.js`) is built from it.
* **Pixel convention**: the image glyph is drawn at `x=0, y=0` with
  `dw, dh` = shape, so pixel index i covers [i, i+1] in Bokeh data space and
  its centre is i + 0.5.  Axis ticks (`wcs_ticks.ts`) and region centres
  (`to_world`/`from_world` in `_cube.py`) convert data-space positions; the
  cursor readout converted the (floored) pixel index.  So the browser's
  transform now works in data space: `description_keywords(desc,
  pixel_center)` with `BOKEH_PIXEL_CENTER` = 0.5 (CRPIX = reference pixel +
  0.5; `FITS_PIXEL_CENTER` = 1 gives a FITS CRPIX and is the default), and
  the cursor readout converts the pixel centre, index + 0.5.
* Result: the page's coordinates equal casatools' to ~3e-10" for SIN, TAN,
  galactic, ICRS, rotated and off-centre images (before: up to 190" from
  1, plus one pixel at the cursor and half a pixel on the axes from 3).
* casalib's `scripts/build-browser.js` never defined `__VERSION__` (the
  bundle would throw on load) and copied a file the build does not write;
  both fixed.

## 3. Remote shape (reusing visplot's machinery)

* Registration module `cubevis.imaging.remote_registrations` (§6) registers
  `LocalImagingSession`. It is added to `_fingerprint.ROOTS`, so every
  fingerprint changes and a release follows.
* `RemoteImagingSession` (laptop) uses one handle. Cube proxies call
  `cube_call(field, method, **kw)` on that same handle, so there's one worker
  and one handle per iclean. Fast calls go through the sync bridge.
  `run_cycle` goes through `dispatch_async` + `job_status` so the GUI stays
  responsive.
* The connection (kernel resolution, fingerprint, `_watched` liveness,
  dead-worker handling, session records with `app="iclean"`) is factored out
  of `RemoteReductionContext` into a shared base. It is not copied.
  Restarting a dead worker is *not* transparent for iclean: tclean's state
  would be lost. It should fail clearly, with the products left on disk.
* `iclean(..., kernel_name=)`: `vis`/`imagename` are interpreted on the host,
  relative to the kernel's working directory, as visplot's `ms=` is.

### 3.1 As done (step 3, 2026-11-07, notes §67)

* `cubevis.remote.RemoteObjectConnection` (`_remote_object.py`): the generic
  connection -- one bridge loop, kernel, link, context, one object; `call`,
  `acall`, `submit`; liveness, remote errors with tracebacks, a dead worker
  (not restarted for iclean), the session record.  Lifted from visplot's
  `RemoteReductionContext`, which moves onto it in step 5.
* Worker: `cubevis.imaging.remote_registrations.ImagingSessionBackend` over a
  `LocalImagingSession`; cycles run in a thread (`start_cycle` /
  `cycle_status`) so the worker keeps answering the GUI; cube methods by name
  (`cube_call`, the GUI's set only).  Laptop: `RemoteImagingSession` and
  `RemoteImageCube` with `LocalImagingSession`'s and `ImageCube`'s surface;
  `arun_cycle` polls on the connection's loop and is awaited without blocking
  Bokeh's.  One worker and one object per interactive clean; cube calls name
  the field.
* Found: the worker's stdout is the wire and tclean prints to it
  (`worker_main.protect_wire` moves the wire to its own descriptor); an
  `imagename` with a directory never worked (convergence re-keyed to field
  ids); a cubevis without the new root now fingerprints as different rather
  than failing.
* Verified: real tclean through a local kernel gives the same handler
  records as locally; the page from a remote session in headless Chrome.
* Step 4 (§68, §69): stop is after the running cycle (gclean cannot
  interrupt one); any failure is an 'error' the GUI shows; progress
  (`progress()`, read from tclean's log in the worker) rides on the cycle
  polls and is pushed to the status line.

## 4. Steps (revised)

| Step | Slices |
|---|---|
| 1. Protocols; `LocalImagingSession` = gclean + `CasatoolsImageStore` + `ImageCube` extracted from `ImagePipe`; UI and `CubeMask` go through it (field ids, `stop_descriptions`, generation instead of ctime). Local only, no behaviour change; tests against today. **Done: notes §60** | 1.5 |
| 1b. Coordinate description (§2.5): casatools store builds it; `coords_json` + `DescriptionKeywordProvider` in cubevisjs; `astropy_wcs()` replaces the two coordsys→WCS helpers; parity against the FITS-header path. **Done: notes §63, §2.5.1** | 0.5–1 |
| 1d. Coordinate defects (§2.5.2): coordtxl projection parsing (0.1.6), the GUI's pixel convention. FK4 stays for the coordtxl feature work. **Done: notes §64** | 0.5 |
| 2. `edit_mask` operations replace `CubeMask`'s per-channel loops. **Done: notes §65, §2.2.1** | 0.5 |
| 2b. Region selection by pixel centre, the mask outline on pixel edges (§2.2.1). **Done: notes §66** | 0.5 |
| 3. Registration + `RemoteImagingSession`; remote display (channel, spectrum, stats, histogram) and mask edits. **Done: notes §67, §3.1** | 1–2 |
| 4. `run_cycle` as an async job; stop and progress semantics. **Done: notes §68** (the job in step 3; stop after the cycle; failures shown in the GUI; progress and reattach open, §5) | 1 |
| 4b. Progress of a running cycle, local and remote: tclean's log followed where the imager runs (`CycleProgress`), kept as session state (`progress()`), pushed to the status line about once a second. **Done: notes §69** | 0.5 |
| 5. `iclean(kernel_name=)`, the shared connection base, setup environments with casatasks (`cubevis[visplot,iclean]`, discovery reports it), fingerprint roots, release | 1 |
| 6. Parity tests (local kernel, small MS, local vs remote), quick tests, rounds with Darrell | 1 + 2–3 rounds |
| 1c. Package organization (§6): `toolbox/iclean/`, one app template, dependency rule. **Done: notes §62** | 1 |
| Later: coordtxl taking the description natively, with ICRS, a spectral axis and correct FK4 (§2.5); `XarrayImageStore` over arcae; notebook variant; remove `tests/manual/iclean-remote/` and `_gen_port_fwd_cmd` | — |

Steps 1–2 are useful without any remote work: the CASA dependency in the GUI
becomes explicit, and mask editing becomes faster for cubes.

## 5. Things to verify / open questions

* **Laptop without CASA.** §2.5 removes the `coordsys` uses (1b: the GUI
  needs only the description; `coorddesc()['csys']` is still the casatools
  `coordsys` locally, for `convert_masks(format='region')`). Also audit the
  imports in `_cube.py` and `ImageDataSource`.
* **Coordinate description scope.** Does a per-plane beam belong in v1?
  Should `spectral.values` be required, or may it be linear (crval/cdelt)
  for very large cubes? Outside iclean, `createregion`'s `result()` passes
  `CubeMask.fits_header()`'s string to `RegionList`, which turns it into an
  astropy `WCS` (`_region_list.py`, cached by hash). `RegionList` already
  accepts a `WCS` object, so `astropy_wcs(description)` fits in. That WCS
  has stokes and frequency axes, though, so the helper has to build all 4
  axes, not just 2. An irregular spectral axis means `-TAB` or an
  approximation; check what `RegionList` actually uses from it.
* **Table locks.** The store holds the mask open while tclean (same process)
  rewrites it. casacore auto-locking handles this today. An arcae-based store
  must flush and release around `run_cycle`, which is another reason the
  session should own both sides.
* ~~**Stop during a cycle**~~: "stop after this cycle" is the contract
  (step 4, notes §68): gclean cannot interrupt a cycle, and the GUI's orange
  stop already waits for the running one; the same remotely.
* ~~**Progress during a cycle**~~ (done, step 4b, notes §69): tclean's log
  is followed where the imager runs (`CycleProgress`: phase, channels done,
  iterations, peak residual, elapsed time, how long tclean has been quiet);
  `ImagingSession.progress()` gives it as state, any time; the GUI pushes it
  to the status line about once a second during a cycle, local and remote.
* **Reattach** after a disconnect (hours of cleaning; for the session-
  management work): a lost kernel now ends the session with a message in the
  GUI (§68).  In place (§69): the cycle's state and progress live in the
  worker and can be asked for at any time (`cycle_status()`, `progress()`).
  To do: adopt a running worker from the session record (`app="iclean"`),
  rebuild the GUI's state from it (convergence history, last `CycleResult`,
  controls), register the browser's progress handler at page load.
* **Failures** (done, §68): any failure of a cycle -- the imager, a value it
  refuses, the worker or kernel gone -- is an 'error' reply the browser shows
  with the controls enabled again.
* **Multi-field**: one `ImageCube` per field in the one session. Check the
  per-field loops in `toolbox/iclean/_ui.py`.
* ~~`cubevis/private/_gclean.py`~~: deleted in 1c (notes §62).
* **Naming**: `ImagingSession` / `ImageCube` / `ImageStore` / `Deconvolver`
  are placeholders.

## 6. Package organization (2026-11-05; done, notes §62)

Step 1 added `cubevis.imaging` but left interactive clean spread where it
grew: the application classes are generated into `cubevis/private/apps/`,
the GUI and its widgets are in `cubevis/toolbox/`, and the casatasks
wrappers are in `cubevis/private/casatasks/`.  visplot, the newest
application, keeps everything of its own under `cubevis/toolbox/visplot/`
(GUI, readers, local and remote reduction context, registrations), with a
thin task wrapper in `private/casatasks/visplot.py`.  The proposal is to
give iclean the same shape, with one rule for dependencies.

### 6.1 What is there now

| What | Where | Note |
|---|---|---|
| task wrappers `iclean`, `iclean_notebook` | `private/casatasks/iclean{,_notebook}.{mustache,py}` | generated from tclean's XML; the two templates differ in 36 lines |
| casashell binding | `private/casashell/iclean.{mustache,py}` | generated; also named by `cubevis.mustache_interface_templates()` |
| `InteractiveClean`, `InteractiveCleanNotebook` | `private/apps/_interactiveclean{,notebook}.{mustache,py}` | generated; 84-line templates, identical except the notebook's display (`DisplayContext`, `__call__`, `get_future`) |
| `InteractiveCleanUI` | `toolbox/_interactive_clean_ui.{mustache,py}` | the `.mustache` has **no tags**: it is a hand-kept copy of the `.py` (one header line different). Step 1 edited only the `.py`; fixed in §61 |
| `SharedWidgets` | `toolbox/_interactiveclean_wrappers.py` | used only by the UI |
| `CubeMask` | `toolbox/_cube.py` (3.6k lines) | shared with createmask and createregion |
| `ImagePipe` | `bokeh/sources/_image_pipe.py` | since step 1, Bokeh model + messages only |
| imaging layer | `cubevis/imaging/` | step 1 |
| old copy of gclean | `private/_gclean.py` | unused, pre-`image_products` |
| pre-multiplexing remote test | `tests/manual/iclean-remote/` | the ssh-port webserver approach |

### 6.2 Proposed layout

```
cubevis/imaging/                    the imaging layer: no Bokeh, no CASA at import
    __init__.py                     ImagingSession, Deconvolver, ImageCube, ImageStore, CycleResult, ...
    _protocols.py
    _image_cube.py                  ImageCube (numpy)
    _casatools_store.py             CasatoolsImageStore      (later _xarray_store.py: arcae)
    _coordinates.py                 the coordinate description, astropy_wcs()        (step 1b)
    _session.py                     LocalImagingSession
    _remote_session.py              RemoteImagingSession (laptop)                    (step 3)
    remote_registrations.py         worker side; a _fingerprint root                 (step 3)

cubevis/toolbox/iclean/             the application, like cubevis/toolbox/visplot/
    __init__.py                     InteractiveClean, InteractiveCleanNotebook, InteractiveCleanUI
    _app.mustache -> _app.py        ONE template: InteractiveClean, and
                                    InteractiveCleanNotebook(InteractiveClean) adding only its display
    _ui.py                          InteractiveCleanUI       (from toolbox/_interactive_clean_ui.py;
                                                              plain source, the tag-free .mustache dropped)
    _widgets.py                     SharedWidgets            (from toolbox/_interactiveclean_wrappers.py)

cubevis/private/casatasks/iclean{,_notebook}.*   unchanged (task wrappers, generated)
cubevis/toolbox/_cube.py, bokeh/sources/_image_pipe.py   stay: shared with createmask/createregion
```

Dependency rule (checked by a quick test): `cubevis.imaging` imports
neither Bokeh nor CASA at import time, so a remote worker, an RADPS
implementation or a test can use it without the GUI;
`toolbox/iclean` and `bokeh/sources` depend on `imaging`, never the reverse.

Names that keep working: `cubevis.iclean`, `cubevis.toolbox.InteractiveCleanUI`,
`cubevis.private.apps.InteractiveClean` / `InteractiveCleanNotebook` /
`iclean_notebook` (the lazy tables point to the new modules).  The
underscore module paths (`toolbox._interactive_clean_ui`,
`private.apps._interactiveclean`) go away; three tests and
`toolbox/visplot/visibility_plot.py` (a comment) refer to them.

Removed: `private/_gclean.py`; `tests/manual/iclean-remote/` once step 3
works (the design already planned this).

### 6.3 Why the imaging layer stays a top-level package

visplot keeps its reduction context inside `toolbox/visplot/`, so the
mirror image would be `toolbox/iclean/session.py`.  It is kept separate
because it is the API outside imagers implement (an RADPS deconvolver, an
xradio store), because the remote worker loads it without the GUI (and
`toolbox/iclean/__init__` would pull the GUI in), and because `ImageCube`
already serves createmask and createregion through `CubeMask`.

### 6.4 When

As its own slice, now, before 1b/2/3, so that those land in their final
places: moves and import changes only, verified by the before/after
handler records, the notebook run (`nb` check, §61), and the quick tests.
About one slice.

### 6.5 As done (notes §62)

As §6.2, plus:
* The dependency rule needed one change: importing anything from
  `cubevis.utils` runs its `__init__` (Bokeh, astropy, regions: ~0.6 s), so
  `cubevis.imaging` has its own `partition` and imports `cubevis.utils._casa`
  only when a store first opens an image.  Tested: importing
  `cubevis.imaging` loads no other `cubevis` module and none of Bokeh,
  casatools, casatasks, astropy, regions; importing `cubevis.toolbox.iclean`
  loads none of its modules.
* The task wrappers (`private/casatasks/iclean{,_notebook}.mustache`,
  `private/casashell/iclean.mustache`) import from `cubevis.toolbox.iclean`
  (visplot's import from `cubevis.toolbox.visplot` the same way).
* Two kinds of template, for the record: the `.mustache` templates carry
  tclean's parameters, types and documentation from its task XML
  (`scripts/expand_templates`); `scripts/sync_layers` expands Jinja2
  templates from `VisibilityPlotter.__call__`'s signature and docstrings
  for visplot's task and casashell layers (`inp`/`go`).  iclean's
  application keeps the first kind, now one template (`toolbox/iclean/
  _app.mustache`); its UI is plain source.
* Kernel side: `cubevis/toolbox/__init__.py` is in the fingerprint closure
  (the worker imports `cubevis.toolbox.visplot`), and its lazy table names
  the UI's module, so this slice changes the fingerprint; the bridge-error
  patch (§61) is in the same release.
