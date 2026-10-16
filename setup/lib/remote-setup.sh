# --- The rest is the same for every host (setup/lib/remote-setup.sh). ---
#
# Installs conda if needed, creates the environment, installs cubevis, and
# tests the import. Safe to run again: it skips what is there already.
# Lines starting CUBEVIS_ are read by cubevis-setup.

set -u
step() { echo; echo "== $*"; }
result() { echo "CUBEVIS_RESULT $*"; }
change() { echo "CUBEVIS_CHANGE $*"; }
die() { echo "CUBEVIS_ERROR $*"; exit 1; }
download() {   # download URL FILE
    if command -v curl >/dev/null 2>&1; then curl -fL -o "$2" "$1"
    else wget -O "$2" "$1"; fi
}

echo "cubevis remote setup on $(hostname) for '$CV_ALIAS' ($CV_GENERATED)"

if [ -z "$CV_CONDA" ]; then
    if [ -x "$CV_PREFIX/bin/conda" ] && "$CV_PREFIX/bin/conda" --version >/dev/null 2>&1; then
        CV_CONDA="$CV_PREFIX/bin/conda"
    else
        step "Installing Miniforge in $CV_PREFIX"
        [ -e "$CV_PREFIX" ] && die "$CV_PREFIX exists but has no working conda"
        f="Miniforge3-$(uname -s)-$(uname -m).sh"
        tmp=$(mktemp -d "${TMPDIR:-/tmp}/cubevis.XXXXXX") || die "mktemp failed"
        download "$CV_MINIFORGE_URL/$f" "$tmp/$f" || die "could not download $f"
        bash "$tmp/$f" -b -p "$CV_PREFIX" || die "the Miniforge installer failed"
        rm -rf "$tmp"
        change "installed Miniforge: $CV_PREFIX"
        CV_CONDA="$CV_PREFIX/bin/conda"
    fi
fi
echo "conda: $CV_CONDA"

env_path() { "$CV_CONDA" env list 2>/dev/null | awk -v n="$CV_ENV" '$1 == n { print $NF; exit }'; }
p=$(env_path)
if [ -z "$p" ]; then
    step "Creating the conda environment '$CV_ENV'"
    set -f
    # shellcheck disable=SC2086  # the package list is meant to split
    "$CV_CONDA" create -y -n "$CV_ENV" --override-channels -c conda-forge $CV_CONDA_PKGS \
        || die "conda create failed"
    set +f
    p=$(env_path)
    [ -n "$p" ] || die "conda created no environment '$CV_ENV'"
    change "created conda environment: $p"
else
    echo "environment: $p (exists)"
fi

step "Installing $CV_PIP_PKGS"
set -f
# shellcheck disable=SC2086
"$p/bin/python" -m pip install -q $CV_PIP_PKGS || die "pip install failed"
set +f
change "pip installed $CV_PIP_PKGS: $p"

step "Testing"
v=$("$p/bin/python" -c 'import cubevis, ipykernel, jupyter_client, xarray_ms
import importlib.metadata as m; print(m.version("cubevis"))') || die "the environment cannot import cubevis"
echo "cubevis $v in $p"
result "python=$p/bin/python"
result "cubevis=$v"
