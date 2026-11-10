# Release notes: interactive clean on remote kernels (`remote-iclean` -> `main`, after v1.0.113)

Draft for the merge of `remote-iclean` (notes §60–§79,
`devel/notes/drs/visplot/hrs_h8_consolidation_prep.md`; design:
`devel/notes/drs/iclean/iclean_imaging_layer_design.md`).

## For users

### Interactive clean on the data's computer

* `iclean(..., kernel_name="KERNEL")` and `iclean.notebook(..., kernel_name=...)`
  run `tclean` and the image access in a Jupyter kernel on the computer that
  holds the data; the window stays on your computer, which needs no CASA.
  The casashell `iclean` takes `kernel_name=` (and `iclean_backend=`) too.
* Paths are the kernel's: relative `vis`/`imagename` are found in, and
  written to, the kernel's working directory; the parameters are checked
  there, as `tclean`'s task checks them.
* While a cycle runs (here or remotely), the status line shows its progress
  from `tclean`'s log: elapsed time, minor/major cycle, channels,
  iterations, peak residual (the full text as a tooltip).
* Stop takes effect after the running cycle.  Any failure of a cycle is
  shown in the window, with the controls enabled again.
* Closing the window, or Python ending, closes the remote session (kernel
  and worker); the images stay on that computer.
* Docs: `docs/source/applications/remote_kernels.rst` (new),
  `interactive_clean.rst` (section "Running on the data's computer").

### Interactive clean, also locally

* Mask edits over a whole cube are done in one operation where the mask is
  (much faster for cubes; identical results).
* A region selects the pixels whose centre it holds, and the mask outline
  runs along pixel edges: the one-sided off-by-one-pixel outline is gone.
* World coordinates (axes, cursor readout, regions) match casatools to
  ~3e-10" for SIN/TAN/galactic/ICRS/rotated images (coordtxl 0.1.6: every
  image used to be treated as linear, ~190" off at the corners of a test
  image; the cursor was one pixel off).
* The status line keeps its height, so the image no longer resizes as the
  status text changes.

### `python -m cubevis.setup`

* Kernels have capabilities -- visplot, interactive clean -- checked in the
  kernel's own environment (every package of `cubevis[visplot]` /
  `cubevis[iclean]`), shown by `setup status`, and corrected by each
  connection (a package installed later is noticed).  A kernel that cannot
  run an application says why and what to install.
* Making an environment asks whether its kernels should run interactive
  clean (`cubevis[visplot,iclean]`); a rerun offers to add it to an
  environment setup made.  CASA's data: the user's, or a private copy for
  the environment (`casadata`, given to the kernels as `CASASITECONFIG`).
* A rerun of `setup HOST` offers another kernel (another environment,
  directory or variables); `HOST --kernel NEW` always asks for its
  environment.
* `setup kernel NAME [--workdir DIR] [--env VARS] [--environment ENV]`
  changes one kernel; a cluster's defaults can follow.
* `setup --help` lists every command.
* Setup's kernel names work everywhere (`kernel_name="NAME"`, also for a
  kernel on another node of a cluster, `cubevis-NAME-NODE`).

### visplot

* Uses the same remote connection as interactive clean (one code path:
  kernel start, liveness, dead worker, session records); behaviour
  unchanged.  Each connection phase's time is logged.

## Compatibility

* **Kernel-side fingerprint** changes: a kernel's environment needs this
  release (or the same development install) -- setup reports a mismatch.
* `cubevis[iclean]` now includes the `remote` packages.
* CI no longer needs python-casacore (xradio >= 1.3 reads tables through
  arcae).
* Removed: `tests/manual/iclean-remote/` (the ssh-port webserver prototype);
  the bash setup (`setup/cubevis-setup`, `setup/lib/`) and its guide
  `demos/visplot-remote/00_setup.md` -- `python -m cubevis.setup` replaces
  them, and still recognizes the files the bash setup left
  (`~/.config/cubevis`).  The ssh wrapper's tests moved to
  `tests/manual/setup/test_ssh_wrapper.py`.
* CI: a suite may say `optional = true` (tests/ci/suites.toml): skipped
  when its modules are missing even under `--strict`.  `iclean-parity`
  (needs CASA) is optional; the GitHub workflow has no CASA.

## Known, outside cubevis

* casatasks' gclean `_update_peakres_masksum_multifield` reads each
  field's residual and mask by base name in the current directory (not
  `imagepath`): an `imagename` outside the working directory fails at the
  first cycle, locally too.  Fix in casatasks (join `imagepath`): a ticket
  after this release; until then keep `imagename` relative to the working
  directory.

## Not in this release

* Reattaching to a running remote clean after a disconnect (session
  management): the worker keeps the cycle's state and progress for it.
* coordtxl's FK4 conversion (~82° off), a spectral axis in world units.
