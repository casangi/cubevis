# Handoff: iclean on remote kernels (planned 2026-11-04, not started)

Goal (Darrell, 2026-11-04): users who asked for it can run interactive
clean against data on a cluster, through the same kernels `python -m
cubevis.setup HOST` makes for visplot.  **`gclean` runs on the remote**:
it is the independence layer between cubevis and CASA6, so that an RADPS
`gclean` could later drive iclean the same way.  The GUI stays on the
laptop, as visplot's does.

Read with: `hrs_handoff_2026-11-04_sessions.md` (the remote kernels,
sessions and what is still missing there -- Scenario A applies to iclean
too, with more at stake: hours of cleaning); `devel/notes/drs/remote/
cubevis-remote-execution-chunk3-handoff.md` (the 2026-09 plan, "Chunk 3",
written before visplot's remote path existed; its call-site survey still
holds, its "blocking prerequisite" is solved -- below);
`remote_session_design.md`.  Conventions: as in the sessions handoff
(zip on the base commit, notes sections in `hrs_h8_consolidation_prep.md`).

## (a) What is wanted, and what is there

Wanted: `iclean(vis=..., imagename=..., ..., kernel_name="zuul06")` (or
the task interface's equivalent) runs tclean's major and minor cycles on
the kernel's host, on data there, writing the images there; the iclean
GUI on the laptop shows the residual cube and mask, edits the mask, sets
the iteration controls, and shows convergence -- as it does locally.

There today:

* **`gclean`** (`casatasks/private/imagerhelpers/_gclean.py`, casatasks
  6.7.6; selected by `iclean_backend`: "PROD" or a module `_gclean_NAME`,
  `_interactiveclean.py`).  Its surface, used by
  `_interactive_clean_ui.py`: `next(gclean)` (sync; the first, dirty
  image, from `_setup` before any event loop), `update(dict)` (sync,
  returns `(err, errmsg)`), `await gclean.__anext__()` (runs `__next__`
  in a thread; one major cycle and its minor cycles -- minutes),
  `image_products()` (a list of dicts per field: `name`, `imagename`,
  `imagepath` -- absolute, on gclean's host -- `maskname`,
  `residualname`), `_log(history=False)`, `restore()` (returns tclean's
  result dict), `has_next()`.  `__next__` returns `(stopdesc, stopcode,
  majordone, majorleft, iterleft, convergence)`; `convergence` is nested
  dicts of lists, per channel and Stokes.
* **The image side of the GUI**: `CubeMask` (`toolbox/_cube.py`) owns an
  `ImagePipe` (`bokeh/sources/_image_pipe.py`) per image, which **opens
  the residual and mask with casatools on this computer** and answers the
  browser: `channel` (a plane quantized to uint8, its mask, the mask0,
  statistics and a histogram, in one reply), `spectrum`, `adjust-colormap`;
  and CubeMask's mask edits: `mask(index, modify)`, `put_mask`,
  `set_mask_name`, `pixel_value`, `mask_value`, `statistics`,
  `coorddesc`, `fits_header`, `shape`, and `__get_chan`'s check that the
  residual was rewritten (`newest_ctime`).  Everything is
  `imagetool.getchunk/putchunk/statistics` on a path.  `convert_masks`
  (regions to a mask) uses `coorddesc()`.
* **The remote machinery visplot uses**: kernels from setup, a supervisor
  in the kernel, a worker process per context, objects created by class
  name from a registration module and called by handle
  (`create_object` / `call_method`, `dispatch_fast`; `dispatch_async` +
  `job_status` for long work), the sync bridge
  (`RemoteReductionContext._call` -> `SyncBridge.run`) -- which is exactly
  what the old Chunk 3 handoff called the blocking prerequisite (a call
  from code with no event loop): solved since.
* `tests/manual/iclean-remote/iclean_remote_webserver.py`: the older
  approach (all of iclean in the kernel, its web page forwarded); not
  the plan -- remove when this lands.

## (b) Design

One worker per iclean session holds **both** a `gclean` and an image
backend per field, so residual and mask are read and written where
tclean works, by one process: a mask edit is on disk before the next
`update()` / `__anext__()` reads it.

```
laptop                                        kernel's host (worker)
iclean GUI                                    RemoteGClean ---- gclean (casatasks, or RADPS later)
  InteractiveCleanUI --- GCleanProxy  ======>      |
  CubeMask --- ImagePipe --- ImageAccess  ===>  ImageAccess (casatools on the paths gclean names)
                 (Bokeh model, browser)
```

* **`ImageAccess`**: everything `ImagePipe` does to the image and mask
  files, moved out of it into a class with plain-data methods --
  `channel_reply(index, quant)` building the whole `channel` answer
  (quantization, mask, mask0, statistics, histogram: **one round trip
  per channel step**), `spectrum`, `mask`, `put_mask`, `set_mask_name`,
  `pixel_value`, `mask_value`, `statistics`, `coorddesc`, `fits_header`,
  `shape`, `stokes_labels`.  `ImagePipe` keeps the Bokeh model and the
  message handling and calls an `ImageAccess` -- local (today's
  behaviour, unchanged) or a proxy to the same class in the worker.
  `CubeMask` hardly changes: it already goes through
  `self._pipe['image']`.  Later, an `ImageAccess` for non-casatools
  images (xradio, RADPS) is the second independence layer next to
  `gclean`.
* **`RemoteGClean`** (in the worker): creates the backend gclean from
  iclean's arguments and `iclean_backend` (resolved on the kernel's
  host), and exposes `next`, `update`, `image_products`, `log`,
  `restore`, `has_next`.  Results are plain data (tuples, dicts, lists,
  numpy) -- check `convergence` and `restore()`'s dict serialize with
  `cubevis.utils.remote_serialize`.
* **`GCleanProxy`** (laptop): the object `InteractiveCleanUI` is given in
  place of a gclean -- `__next__`, `update`, `image_products`, `_log`,
  `restore` through the sync bridge; `__anext__` through
  `dispatch_async` + `job_status` (a major cycle outlasts any sensible
  call timeout, and the GUI must stay responsive and able to stop).
  Define the surface as a `Protocol` (`CleanBackend`) that both gclean
  and the proxy satisfy: the independence layer made explicit.
* **The connection**: a `RemoteReductionContext`-like object
  (`RemoteCleanContext`) sharing its machinery -- kernel resolution
  (`_resolve_kernel_name`), the fingerprint check, the liveness watch,
  dead-worker handling, session records (`app="iclean"`).  Factor the
  common part out of `RemoteReductionContext` rather than copy it.
* **Kernel-side code**: a registration module
  (`toolbox/iclean_remote_registrations.py`, registering `RemoteGClean`
  and `ImageAccess`) becomes another root of the fingerprint
  (`_fingerprint.ROOTS`) -- every fingerprint changes once; a release
  follows.
* **The environment** needs casatasks/casatools as well as cubevis.
  Setup's own environments are `cubevis[visplot]` today: offer
  `cubevis[visplot,iclean]` (a choice when making one, or a flag), and let
  discovery report whether casatasks imports, so `setup HOST` can say
  which environments iclean can use.
* **Paths**: `vis` and `imagename` are on the kernel's host, relative to
  the kernel's working directory, as visplot's `ms=` is.  The products
  stay there.

## (c) Steps and estimates

A slice is one delivery like visplot's sections 56-59.

| Step | Slices |
|---|---|
| 1. `ImageAccess` split out of `ImagePipe`, local only, no behaviour change; tests against today's behaviour | 1 |
| 2. `ImageAccess` in a worker: display (channel, spectrum, statistics, histogram) on a remote image; registration, proxy | 1-2 |
| 3. Mask editing remote (`put_mask`, `set_mask_name`, `convert_masks`) | 0.5-1 |
| 4. `RemoteGClean` / `GCleanProxy`, `__anext__` as an async job; `CleanBackend` protocol | 1-2 |
| 5. `iclean(..., kernel_name=)`, the shared connection class, setup's environments with casatasks, fingerprint roots, a release | 1 |
| 6. Tests (a local kernel, a tiny MS: local and remote iclean compared, as `remote_visplot_check.py` does), the quick tests, Darrell's rounds | 1 + 2-3 rounds |

About 5-7 slices plus rounds: two to three weeks.  Steps 1-3 are useful
on their own (the image backend), and step 1 can land without anything
remote.

## Open questions

* The browser's "stop" during a major cycle: today the cycle runs to its
  end in a thread; remote, the job runs in the worker -- can `gclean`
  stop mid-cycle at all, or is "stop after this cycle" the contract?
* Progress during a cycle (minor-cycle convergence pushed to the GUI):
  today only after each `__anext__`; keep that, or push from the worker
  (the old Chunk 3 handoff, section 4)?
* Multi-field (outlier) imaging: one `ImageAccess` per field, all in the
  one worker; check `_interactive_clean_ui.py`'s per-field loops.
* `restore()` and the final products: return paths only (the images stay
  remote), or offer a copy back?
* iclean's notebook variant (`_interactivecleannotebook.py`): same
  proxy, later.
