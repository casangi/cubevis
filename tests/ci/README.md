# Quick tests

Minutes-long tests that need no real network, cluster or downloaded data,
run on every push and pull request to main (`.github/workflows/quick-tests.yml`)
and by hand:

    pip install -e ".[visplot]" -r tests/ci/requirements.txt
    python tests/ci/run.py            # all; --list, --only, --skip, --lint, --strict

What runs is listed in `suites.toml`; adding tests means adding a
`[[suite]]` there (a pytest path, or any command) -- the workflow does not
change.  Today:

* **setup** -- `python -m cubevis.setup` without the ssh test rig (the
  rig's tests skip themselves).
* **remote** -- the remote layer against a kernel on this computer:
  transport, worker, objects, a dead worker reported.
* **tiny-ms**, **visplot-remote** -- a 3 MB MSv2 made by `make_tiny_ms.py`
  (xradio's generator, then given real times, UVWs, data and frequencies),
  and `tests/manual/visplot/remote_visplot_check.py` on it: visplot local
  and with `backend="remote"`, compared step by step through the GUI's
  handlers.  This is the guard against local changes breaking remote
  plotting.
* **syntax** (lint) -- `compileall` only.  A stricter checker can be
  another `[[lint]]` entry; JavaScript kept in Python strings is not
  checked by any of them.
