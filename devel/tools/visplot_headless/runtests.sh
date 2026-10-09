#!/bin/bash
# usage: runtests.sh <outdir> [file-glob]
# Runs each tests/manual/visplot test file in its OWN process: the whole
# suite in one process exceeds an 8 GB sandbox once the TW Hya MS is
# present.  Writes <outdir>/<file>.log and <outdir>/summary.txt, which
# ends with DONE.  Compare summary.txt before and after a change.
# MS / PS point at the TW Hya MSv2 and its MSv4 conversion.
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${CUBEVIS_ROOT:-$(cd "$HERE/../../.." && pwd)}"
OUT="$(mkdir -p "$1" && cd "$1" && pwd)"; GLOB="${2:-test_*.py}"
cd "$ROOT"
export CUBEVIS_SRC="$ROOT/cubevis"
export MS="${MS:-$ROOT/../data/sis14_twhya_calibrated_flagged.ms}"
export PS="${PS:-$ROOT/../data/sis14_twhya_calibrated_flagged.ps.zarr}"
ulimit -n 4096
: > "$OUT/summary.txt"
for f in tests/manual/visplot/$GLOB; do
  b=$(basename "$f" .py)
  timeout 1800 python -m pytest "$f" -q -p no:cacheprovider --timeout=900 -rfE > "$OUT/$b.log" 2>&1
  echo "$b rc=$? $(tail -1 "$OUT/$b.log")" >> "$OUT/summary.txt"
done
echo DONE >> "$OUT/summary.txt"
