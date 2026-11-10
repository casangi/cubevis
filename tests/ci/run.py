#!/usr/bin/env python3
"""Run the quick tests listed in ``tests/ci/suites.toml``: each suite on
its own, then a summary; the exit status is 0 only if none failed.

    python tests/ci/run.py                 # everything
    python tests/ci/run.py --list          # what there is
    python tests/ci/run.py --only remote,setup
    python tests/ci/run.py --skip visplot-remote
    python tests/ci/run.py --lint          # only the [[lint]] entries
    python tests/ci/run.py --strict        # a suite missing a module fails (CI),
                                           # unless the suite says optional = true

What a suite needs is installed by ``pip install -e ".[visplot]"`` plus
``tests/ci/requirements.txt``.  Standard library only (Python 3.11+, for
``tomllib``).
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
import tempfile
import time

try:
    import tomllib
except ImportError:                                   # pragma: no cover
    sys.exit("tests/ci/run.py needs Python 3.11 or later (tomllib)")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CONFIG = os.path.join(ROOT, "tests", "ci", "suites.toml")


def load(path: str = CONFIG) -> list:
    """The entries, suites first then lint, each a dict with ``kind``."""
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    out = []
    for kind in ("suite", "lint"):
        for e in cfg.get(kind, []):
            if "name" not in e or ("pytest" in e) == ("command" in e):
                raise SystemExit(f"{path}: each [[{kind}]] needs a name and one of pytest/command: {e}")
            out.append(dict(e, kind=kind))
    names = [e["name"] for e in out]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise SystemExit(f"{path}: names used twice: {', '.join(sorted(dup))}")
    return out


def missing(entry: dict) -> list:
    return [m for m in entry.get("requires", []) if importlib.util.find_spec(m) is None]


def argv_of(entry: dict, tmp: str) -> list:
    def fill(a):
        return str(a).format(tmp=tmp, python=sys.executable, root=ROOT)
    if "command" in entry:
        return [fill(a) for a in entry["command"]]
    return ([sys.executable, "-m", "pytest", "-p", "no:cacheprovider"]
            + [fill(a) for a in entry.get("args", [])]
            + [fill(p) for p in entry["pytest"]])


def run_one(entry: dict, tmp: str) -> tuple:
    """``(status, seconds, note)``; status PASS, FAIL or TIMEOUT."""
    env = dict(os.environ, **{k: str(v) for k, v in entry.get("env", {}).items()})
    env.setdefault("PYTHONPATH", ROOT)
    t0 = time.monotonic()
    try:
        r = subprocess.run(argv_of(entry, tmp), cwd=ROOT, env=env,
                           timeout=float(entry.get("timeout", 900)))
    except subprocess.TimeoutExpired:
        return "TIMEOUT", time.monotonic() - t0, f"after {entry.get('timeout', 900)} s"
    dt = time.monotonic() - t0
    return ("PASS" if r.returncode == 0 else "FAIL"), dt, ("" if r.returncode == 0
                                                          else f"exit status {r.returncode}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--list", action="store_true", help="list the suites and stop")
    ap.add_argument("--only", help="comma-separated names to run")
    ap.add_argument("--skip", help="comma-separated names not to run")
    ap.add_argument("--lint", action="store_true", help="run only the lint entries")
    ap.add_argument("--strict", action="store_true",
                    help="a suite whose modules are missing fails instead of being skipped")
    ap.add_argument("--config", default=CONFIG)
    a = ap.parse_args(argv)
    entries = load(a.config)
    if a.list:
        for e in entries:
            what = " ".join(e.get("pytest") or e.get("command"))
            miss = missing(e)
            print(f"{e['kind']:5} {e['name']:16} {what}"
                  + (f"   (missing here: {', '.join(miss)})" if miss else ""))
        return 0
    only = set(filter(None, (a.only or "").split(",")))
    skip = set(filter(None, (a.skip or "").split(",")))
    unknown = (only | skip) - {e["name"] for e in entries}
    if unknown:
        ap.error(f"no such suite: {', '.join(sorted(unknown))}")
    chosen = [e for e in entries
              if (not only or e["name"] in only) and e["name"] not in skip
              and (e["kind"] == "lint" or not a.lint)]
    results = {}
    with tempfile.TemporaryDirectory(prefix="cubevis-ci-") as tmp:
        for e in chosen:
            name = e["name"]
            print(f"\n=== {e['kind']} {name} ===", flush=True)
            miss = missing(e)
            dep = e.get("after")
            if dep and results.get(dep, ("PASS",))[0] != "PASS":
                results[name] = ("SKIP", 0.0, f"needs {dep}, which did not pass")
            elif miss:
                results[name] = ("FAIL" if a.strict and not e.get("optional") else "SKIP", 0.0,
                                 f"missing module(s): {', '.join(miss)}")
            else:
                results[name] = run_one(e, tmp)
            print(f"--- {name}: {results[name][0]}"
                  + (f" ({results[name][2]})" if results[name][2] else ""), flush=True)
    print("\nquick tests:")
    for name, (status, dt, note) in results.items():
        print(f"  {status:7} {dt:7.1f}s  {name}" + (f"  -- {note}" if note else ""))
    bad = [n for n, r in results.items() if r[0] in ("FAIL", "TIMEOUT")]
    print(f"\n{len(results) - len(bad)} of {len(results)} without failure"
          + (f"; failed: {', '.join(bad)}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
