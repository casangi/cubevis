# `cubevis.xarray.image`: CASA images with arcae, as xarray datasets

Record of the work for [casangi/cubevis issue 2](https://github.com/casangi/cubevis/issues/2):
interactive clean reading and writing images without casatools.  One
section per slice.

Confidence markers, as in the gridplot plan: **[code]** read in the
repository named, **[measured]** run in the session, **[doc]** from
documentation.

---

## 1. First slice (2026-11-16, base `gridplot` `3ef1106`)

### 1.1 What was built

```
cubevis/xarray/image/        stands alone: arcae, numpy, xarray (astropy only for sky_coords=True)
    _image.py                CasaImage, PagedArray: pixels by index, keywords, masks; reads and writes
    _coords.py               the coordinate system record in numpy: axes, frequencies, direction, beams
    _backend.py              engine "casa-image": open_casa_image, LazyPixels, to_casa_image
    _dataset.py              open_image (CASA | Zarr | dataset), describe_dataset
    _write.py                create_image, write_image, default_coords: CASA images made with arcae
    _schema.py               names, and where the layout leaves xradio's
cubevis/imaging/reader.py    ImageReader, ImageInfo: planes, spectra, plain values; any source
cubevis/imaging/_xarray_store.py   XarrayImageStore: iclean's ImageStore over ImageReader
```

The layers, top down:

| Layer | What it knows | Written against |
|---|---|---|
| `ImageCube`, the GUI | channels, masks, statistics | `ImageStore` (unchanged) |
| `XarrayImageStore` | casatools' array shapes | `ImageReader` only |
| `ImageReader` | x, y, Stokes, channel; plain values | a dataset in xradio's image layout |
| `cubevis.xarray.image` | CASA tables; xradio's attributes | arcae, xarray |

`ImageReader` is to images what `CalTableReader` is to calibration
tables: nothing above it sees the dataset, so the dataset can follow
xradio's schema as that settles.

### 1.2 Decisions (Darrell, 2026-11-16)

1. **The package is `cubevis/xarray/image/`**, independent of
   `cubevis/xarray/caltab/`: it imports nothing else of cubevis (a test
   parses its imports), so either can leave cubevis by itself.
2. **Both CASA images and xradio images**, behind a reader, as calplot
   does for calibration tables.
3. **`XarrayImageStore` is the default** image store when this merges.
   **`CUBEVIS_IMAGE_STORE=casatools`** puts `CasatoolsImageStore` back
   (read where the images are: a remote kernel's environment).
   `image_store("casatools")` and `open_image_cube(..., store=)` do the
   same from code.  Where arcae or xarray is not installed the default is
   casatools, as before.
4. **casatasks stays.**  tclean runs through casatasks' `gclean`, which
   needs casatools.  What this removes is cubevis's own use of
   casatools for images: cubevis now depends on CASA's task level only.

### 1.3 What a CASA image is on disk [measured, arcae 0.5.9]

- A casacore table of **one row and one column, `map`**, whose cell is the
  whole pixel array in a `TiledCellStMan`.  `table.info` says
  `Type = Image`.
- Everything else is table keywords: `coords` (the coordinate system
  record), `imageinfo` (beam, object name, image type), `units`,
  `miscinfo`, `masks`, `Image_defaultmask`, `logtable`.
- **The `coords` keyword is `coordsys().torecord()`**, field for field,
  except that **arcae gives 2-D arrays transposed** (the `pc` matrices)
  and pads FITS strings with NULs.  `CasaImage.coords_record()` undoes
  both; it is compared with casatools on rotated, tabular and galactic
  images.
- A pixel mask is a subtable with one boolean cell, column `PagedArray`,
  True where a pixel is good.  arcae returns it as `uint8`.
- A beam per plane is `imageinfo.perplanebeams`: `*0 ... *N`, channel
  varying fastest.
- **arcae reads and writes part of the cell** with slices: only the tiles
  touched are read.  Opening a table takes about 2 ms.

### 1.4 The stale-handle finding [measured]

**An arcae handle held open does not see what casatools writes to the
same image in the same process**, even after casatools closes it.  A new
handle does.  The two libraries each carry their own casacore, and the
file locks that keep two processes in step do not exist within one.

This is exactly interactive clean's situation: tclean rewrites the
residual and the mask in the worker's process every cycle.  So:

- `CasaImage` opens the table for each read or write and closes it.
  `hold()` keeps a handle for a burst where nothing else writes.
- `ImageReader` caches the description only (shape, coordinates, beam)
  and reads it again when `table.dat` (or a Zarr store's metadata)
  changes: that is how a restoring beam added by a later cycle is seen.
- A test (`test_a_held_handle_is_stale_after_casatools_writes`) fails the
  day arcae or casacore learns to notice, when holding handles could be
  reconsidered.

Not protected: a mask written through arcae *while* tclean is writing the
same mask.  With casatools the table lock serialized that; here nothing
does.  Whether the GUI can send a mask edit while a cycle runs was not
checked; a guard in the session would settle it (open, 1.9).

### 1.5 The dataset: xradio's image layout

`xr.open_dataset(path, engine="casa-image")` gives what xradio's own
`open_image` gives for the same CASA image **[measured, xradio 1.3.1,
schema 0.0.3]**: `SKY(time, frequency, polarization, l, m)`, `FLAG_SKY`
(True = not valid), `BEAM_FIT_PARAMS_SKY` (radians), `frequency`,
`velocity`, `l`, `m`, and `attrs["coordinate_system_info"]` and
`["data_groups"]`.  The pixel, flag and beam values and `describe_dataset`'s
answer are identical for a plain image, a rotated one with a beam per
plane, and a tabular spectral axis.

Where it differs (each dataset lists these in `attrs["schema"]`):

- an axis the image lacks is not a dim (xradio always has five);
- the whole CASA record is kept in `attrs["coordinate_system"]`, so a
  dataset can be written back as the same image;
- `right_ascension` / `declination` only with `sky_coords=True`;
- other axes (linear) keep their CASA names; other masks are
  `MASK_<NAME>`;
- no telescope or pointing measures in `SKY.attrs`.

**xradio registers its own engine for CASA images, `xradio_casa_image`**
(xradio PR 606, over arcae).  The two overlap.  This one is lighter (no
dask, toolviper or astropy needed), writes, and makes images.  So that
`xr.open_dataset(path)` with no engine keeps meaning what it meant, **this
engine never claims a path by guessing**: name it.

Run from a checkout no entry point is registered; importing the package
registers the engine itself (section 2).

### 1.6 Writing CASA images with arcae alone [measured]

`write_image`, `create_image`, `default_coords`, `to_casa_image`.
casatools opens the result: pixels, masks, coordinates, beam, units,
statistics.  This is what lets the tests run in CI without CASA, and a
dataset (from Zarr, say) be turned into a CASA image.

- The table is made with TaQL `CREATE TABLE ... DMINFO
  [TYPE='TiledCellStMan']`.
- casatools refuses an image with no `logtable` keyword; an empty log
  subtable is made.
- arcae's `putkeywords` (casacore's JSON reader) takes nested records but
  **not matrices or empty arrays**; those are set with TaQL `ALTER TABLE
  ... SET KEYWORD`.

### 1.7 Checked

| What | Result |
|---|---|
| `tests/manual/xarray-image` (60 tests; CI suite `xarray-image`) | pass; no CASA needed |
| the same with casatools installed: records, pixels, masks, beams, frequencies, sky positions, both directions | equal |
| `XarrayImageStore` v `CasatoolsImageStore`, every call; `ImageCube` replies through six mask edits | equal (rms and sumsq to 1e-9: casacore sums float32 squares differently) |
| `tests/manual/iclean-imaging` with the new default (real tclean, local and on a kernel) | 118 pass, 2 skipped as before |
| a recorded interactive clean with real tclean, casatools store v the default (`record_iclean_handlers.py`, `compare_records.py`) | 84 entries; differences only the working directory in command strings and the FITS header (below) |
| CASA image, its Zarr copy, an in-memory dataset, an image made by xradio | the same answers from `ImageReader`; writes read back |

Timings, 1024 x 1024 x 1 x 256 float32 image, warm disk cache, the cloud
sandbox **[measured]**:

| Call | casatools | xarray / arcae |
|---|---|---|
| a new plane | 99 ms | 31 ms |
| a spectrum | 166 ms | 2 ms |
| statistics of a plane | 176 ms | 46 ms |

### 1.8 Differences that are meant

- **`fits_header()`** is made from the coordinate description with
  astropy: 36 cards where casatools writes 77, and `CUNIT4` is `Hz`.  It
  is for display only; nothing in the GUI computes from it.
- **`coordsys()`** returns the record, not a casatools object.
  `convert_masks` takes either; for a CASA image the record is the one
  casatools gives.
- **Statistics** are numpy.  Keys and values match casatools, including
  the formatted positions on the images tested and empty arrays for a
  plane with nothing unmasked.  A mask image counts at 0.5 and above, as
  casatools reads one.

### 1.9 Not done, and open

- **Only paged images.**  FITS, MIRIAD, and concatenated or expression
  images (`imageconcat`) are refused with a message.  If parallel tclean
  ever hands iclean a concatenated residual, that needs handling.
- **A real ASTROviper image** has not been seen.  The xradio tests use
  images xradio itself wrote here (`make_empty_sky_image`, `write_image`).
  Its schema is 0.0.3 and may move; the differences live in
  `describe_dataset` and `_schema.py`.
- **An xradio mask for cleaning**: interactive clean's mask is a second
  image.  Where an xradio imager keeps its clean mask is not known.
- **A guard against mask writes during a cycle** (1.4).
- **arcae below 0.5.9** was not tried; the `image` extra asks for
  `>=0.5.9,<0.6.0`, inside xarray-ms's range.
- **Merging with `calplot`**: both branches add `cubevis/xarray/__init__.py`
  (a docstring) and the `xarray.backends` entry-point table in
  `pyproject.toml`.  Keep both sides.
- `CasatoolsImageStore.read_mask_chans` fails if called before anything
  opened its image (`_chan_shape` is None).  Not reached by `ImageCube`;
  left alone.
- `cubevis/imaging/_coordinates.py` now knows casacore's `'` and `"` unit
  spellings (an image from `ia.fromshape` has them; tclean's are radians).

### 1.10 Files

New: `cubevis/xarray/__init__.py`, `cubevis/xarray/image/` (7 files),
`cubevis/imaging/reader.py`, `cubevis/imaging/_xarray_store.py`,
`tests/manual/xarray-image/` (3 files), this note.

Changed: `cubevis/imaging/__init__.py`, `_session.py` (`image_store`, the
default), `_coordinates.py` (`from_image_info`, unit spellings),
`pyproject.toml` (entry point; extra `image`, added to `iclean`),
`tests/ci/suites.toml` (suite `xarray-image`).

---

## 2. Which store is in use; the engine by name (2026-11-16, base `xarray-image` `53fdea5`)

Darrell ran his interactive clean test on the first slice: it works.  Two
things followed.

### 2.1 Saying which store reads the images

The default falls back to casatools when arcae or xarray is missing, and
nothing said so.  Now:

- **`CUBEVIS_DEBUG=1`** logs, once per choice,
  `[cubevis.imaging._session] DEBUG: images are read with XarrayImageStore
  (arcae, through cubevis.xarray.image; no casatools): the default` -- or
  `CasatoolsImageStore (casatools): CUBEVIS_IMAGE_STORE=casatools`.  For a
  remote session the line is in the kernel's output, where the store is.
- **The fallback is said at INFO**, without `CUBEVIS_DEBUG`: `images are
  read with CasatoolsImageStore (casatools): arcae not installed; pip
  install "cubevis[image]" for the arcae store`.
- **`cubevis.imaging.image_store_name()`** returns `"xarray"` or
  `"casatools"`; `cube.store` is the store object itself.

### 2.2 `engine="casa-image"` from a checkout

`cubevis/xarray/image/_register.py`, called when the package is imported,
adds the engine to xarray's table where no installed entry point does:
the remedy of calplot's `cubevis/xarray/_register.py`.

- **It is a copy inside the package**, not an import of calplot's file:
  the package imports nothing from outside itself (decision 1.2.1), and
  `gridplot` does not have that file.  About 30 lines.
- **It does not disturb xradio's engine.**  The overlap noted in 1.5 is
  about `xr.open_dataset(path)` with *no* engine, which this engine never
  answers (`guess_can_open` is False).  Registering the name `casa-image`
  beside `xradio_casa_image` changes nothing for xradio: both were listed,
  and each opened the same image, in one process **[measured]**.
- The package has to have been imported (`import cubevis.xarray.image`;
  `ImageReader` does it on first use).  `import cubevis` alone does not.

Seen on the way **[measured]**: with xarray-ms installed,
`xr.open_dataset(<a CASA image>)` with no engine is claimed by xarray-ms's
`msv2` engine, which then fails on a missing `SCAN_NUMBER` column.  It
does not come from cubevis (this engine does not guess), and is another
reason to name the engine.

### 2.3 Checked

`tests/manual/xarray-image`: 62 tests pass (two new: the log, the engine by
name).  `tests/manual/iclean-imaging` (real tclean, local and on a kernel):
118 pass, 2 skipped, as before.

### 2.4 Files

New: `cubevis/xarray/image/_register.py`.  Changed:
`cubevis/xarray/image/__init__.py`, `cubevis/imaging/_session.py`,
`_xarray_store.py`, `__init__.py`, two test files, this note.
