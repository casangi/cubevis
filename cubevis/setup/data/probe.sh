# Runs ON THE REMOTE HOST (bash -s), changing nothing: prints what setup
# needs to know as key=value lines.
#   $1  this computer's home-directory id (to see whether home is shared)
#   $2  the environment name
#   $3  the environment's python on this computer (used if home is shared)
home_id=$1 env=$2 local_py=$3

echo "os=$(uname -s)"
echo "arch=$(uname -m)"
echo "home=$HOME"
echo "bash=$BASH_VERSION"
f="${XDG_CONFIG_HOME:-$HOME/.config}/cubevis/.home-id"
if [ -f "$f" ] && [ "$(cat "$f")" = "$home_id" ]; then echo "shared_home=yes"; else echo "shared_home=no"; fi
echo "free_kb=$(df -Pk "$HOME" 2>/dev/null | awk 'NR == 2 { print $4 }')"
for t in curl wget tar; do command -v "$t" >/dev/null 2>&1 && echo "has_$t=yes"; done

# Can it download? (github.com: Miniforge; conda-forge; PyPI)
# (Some servers and proxies refuse a HEAD request: then a 1-byte GET.)
net() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSIL --max-time 15 "$1" >/dev/null 2>&1 \
            || curl -fsSL --max-time 15 -r 0-0 -o /dev/null "$1" >/dev/null 2>&1
    else
        wget -q --spider --timeout=15 "$1" >/dev/null 2>&1
    fi
}
net "https://github.com/conda-forge/miniforge/releases/latest" && echo "net_github=yes"
net "https://conda.anaconda.org/conda-forge/noarch/repodata.json" && echo "net_conda=yes"
net "https://pypi.org/simple/pip/" && echo "net_pypi=yes"

# conda installations that run here (a shared home may hold another
# system's: those do not run).
seen=""
for c in "${CONDA_EXE:-}" "$(command -v conda 2>/dev/null)" \
    "$HOME/miniforge3/bin/conda" "$HOME/mambaforge/bin/conda" \
    "$HOME/miniconda3/bin/conda" "$HOME/anaconda3/bin/conda" \
    "$HOME/miniforge3-$(uname -s)-$(uname -m)/bin/conda" \
    /opt/miniforge3/bin/conda /opt/conda/bin/conda; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    case "$seen" in *"|$c|"*) continue ;; esac
    seen="$seen|$c|"
    "$c" --version >/dev/null 2>&1 || { echo "conda_broken=$c"; continue; }
    echo "conda=$c"
    p=$("$c" env list 2>/dev/null | awk -v n="$env" '$1 == n { print $NF; exit }')
    if [ -n "$p" ] && [ -x "$p/bin/python" ]; then
        echo "env_python=$p/bin/python"
        v=$("$p/bin/python" -c 'import importlib.metadata as m; print(m.version("cubevis"))' 2>/dev/null)
        [ -n "$v" ] && echo "env_cubevis=$v"
    fi
done
if [ -n "$local_py" ] && [ -x "$local_py" ] && "$local_py" -c 'import sys' 2>/dev/null; then
    echo "local_python_runs=yes"
fi
exit 0
