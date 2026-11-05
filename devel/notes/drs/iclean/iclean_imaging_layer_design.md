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

### 2.3 `ImageStore` (primitives; this is what a new image format implements)

```python
shape; coordinates()                            # §2.5; the store builds it from its own metadata
read_plane(stokes, chan) -> np.ndarray          # float
read_spectrum(x, y, stokes) -> np.ndarray
read_mask_plane(stokes, chan); write_mask_plane(stokes, chan, plane)
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

## 3. Remote shape (reusing visplot's machinery)

* Registration module `cubevis.toolbox.iclean.remote_registrations` registers
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

## 4. Steps (revised)

| Step | Slices |
|---|---|
| 1. Protocols; `LocalImagingSession` = gclean + `CasatoolsImageStore` + `ImageCube` extracted from `ImagePipe`; UI and `CubeMask` go through it (field ids, `stop_descriptions`, generation instead of ctime). Local only, no behaviour change; tests against today. **Done: notes §60** | 1.5 |
| 1b. Coordinate description (§2.5): casatools store builds it; `coords_json` + `DescriptionKeywordProvider` in cubevisjs; `astropy_wcs()` replaces the two coordsys→WCS helpers; parity against the FITS-header path | 0.5–1 |
| 2. `edit_mask` operations replace `CubeMask`'s per-channel loops | 0.5 |
| 3. Registration + `RemoteImagingSession`; remote display (channel, spectrum, stats, histogram) and mask edits | 1–2 |
| 4. `run_cycle` as an async job; stop and progress semantics | 1 |
| 5. `iclean(kernel_name=)`, the shared connection base, setup environments with casatasks (`cubevis[visplot,iclean]`, discovery reports it), fingerprint roots, release | 1 |
| 6. Parity tests (local kernel, small MS, local vs remote), quick tests, rounds with Darrell | 1 + 2–3 rounds |
| Later: coordtxl taking the description natively, with a spectral axis (§2.5); `XarrayImageStore` over arcae; notebook variant; remove `tests/manual/iclean-remote/` and `_gen_port_fwd_cmd` | — |

Steps 1–2 are useful without any remote work: the CASA dependency in the GUI
becomes explicit, and mask editing becomes faster for cubes.

## 5. Things to verify / open questions

* **Laptop without CASA.** §2.5 removes the `coordsys` uses. Also audit the
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
* **Stop during a cycle**: can gclean stop mid-cycle, or is "stop after this
  cycle" the contract? (From the handoff.)
* **Progress during a cycle**: push minor-cycle convergence from the worker,
  or only after each cycle? (From the handoff.)
* **Reattach** after a disconnect (hours of cleaning): `on_reconnect` pushes a
  full state snapshot (generation, last `CycleResult`) rather than replaying
  queued messages.
* **Multi-field**: one `ImageCube` per field in the one session. Check the
  per-field loops in `_interactive_clean_ui.py`.
* **`cubevis/private/_gclean.py`** is an older, unused copy (it has no
  `image_products`/`_log`). Delete it, or turn it into the stub backend for
  tests.
* **Naming**: `ImagingSession` / `ImageCube` / `ImageStore` / `Deconvolver`
  are placeholders.
