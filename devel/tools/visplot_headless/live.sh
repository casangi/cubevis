#!/bin/bash
# usage: live.sh <steps.py> <out-prefix> [VisibilityPlotter key=value ...]
# Starts serve.py in the background, waits for its URL, runs drive.py with
# the steps file, then stops the server.  Server output goes to serve.log
# in this directory (the "visplot timing:" lines are useful).
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${CUBEVIS_ROOT:-$(cd "$HERE/../../.." && pwd)}"
STEPS="$(realpath "$1")"; OUT="$2"; shift 2
cd "$HERE"; rm -f url.txt
BROWSER="$HERE/record_url.sh" BOKEH_RESOURCES=inline CUBEVIS_SRC="$ROOT/cubevis" CUBEVIS_ROOT="$ROOT" \
  python -u serve.py "$@" > serve.log 2>&1 &
PID=$!
for i in $(seq 1 90); do [ -s url.txt ] && break; sleep 1; done
[ -s url.txt ] || { echo "server did not start; see $HERE/serve.log"; kill $PID 2>/dev/null; exit 1; }
STEPS="$STEPS" timeout "${DRIVE_TIMEOUT:-300}" python drive.py "$(cat url.txt)" "$OUT"
kill $PID 2>/dev/null; sleep 1; kill -9 $PID 2>/dev/null
