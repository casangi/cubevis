#!/bin/bash
# Used as $BROWSER by live.sh: record the URL instead of opening it.
echo "$1" > "$(cd "$(dirname "$0")" && pwd)/url.txt"
