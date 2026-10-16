# Steps on your computer: what is here, conda, the cubevis environment,
# the ssh wrapper. (The password store is set up when a host first needs
# it: see store.sh.)

LOCAL_STEPS="platform conda env wrapper"

# --------------------------------------------------------------------------
platform_title() { echo "This computer: tools setup needs"; }
platform_check() {
    OS=$(uname -s); ARCH=$(uname -m)
    local miss="" v
    has perl || miss="$miss perl"
    has curl || miss="$miss curl"
    has ssh || miss="$miss ssh"
    SSH_VERSION=$(ssh -V 2>&1 | sed -n 's/^OpenSSH_\([0-9]*\.[0-9]*\).*/\1/p')
    v=${SSH_VERSION%%.*}
    if [ -z "$SSH_VERSION" ] || [ "$v" -lt 8 ] || { [ "$v" -eq 8 ] && [ "${SSH_VERSION#*.}" -lt 4 ]; }; then
        miss="$miss OpenSSH-8.4-or-later(have:${SSH_VERSION:-?})"
    fi
    if [ -n "$miss" ]; then
        CHECK_MSG="missing:$miss"
        return 1
    fi
    CHECK_MSG="$OS $ARCH, OpenSSH $SSH_VERSION, perl, curl"
    return 0
}
platform_do() {
    platform_check
    say "Setup needs perl, curl and OpenSSH 8.4 or later. They come with macOS and with most Linux systems; ask whoever runs this computer to install what is missing (this script does not use root)."
    return 1
}

# --------------------------------------------------------------------------
conda_title() { echo "conda (installs Python and the packages)"; }
conda_check() {
    CONDA=$(conf_get "$CV_CONF" conda)
    if [ -n "$CONDA" ] && [ -x "$CONDA" ] && run_quiet "$CONDA" --version; then
        CHECK_MSG="$(tilde "$CONDA") ($OUT)"
        return 0
    fi
    CHECK_MSG="no conda chosen yet"
    return 1
}

# Conda installations already here: one path per line, no repeats.
find_condas() {
    local c seen=""
    for c in "${CONDA_EXE:-}" "$(command -v conda 2>/dev/null)" \
        "$HOME/miniforge3/bin/conda" "$HOME/mambaforge/bin/conda" \
        "$HOME/miniconda3/bin/conda" "$HOME/anaconda3/bin/conda" \
        "/opt/miniforge3/bin/conda" "/opt/conda/bin/conda" \
        "/opt/homebrew/Caskroom/miniforge/base/bin/conda"; do
        [ -n "$c" ] && [ -x "$c" ] || continue
        case "$seen" in *"|$c|"*) continue ;; esac
        "$c" --version >/dev/null 2>&1 || continue
        seen="$seen|$c|"
        echo "$c"
    done
}

conda_do() {
    local found n i pick prefix
    found=$(find_condas)
    if [ -n "$found" ]; then
        say "conda is already installed here:"
        i=0
        while IFS= read -r c; do i=$((i + 1)); echo "    $i) $(tilde "$c")"; done <<EOF
$found
EOF
        n=$i
        say "Setup can use it (it adds an environment of its own and changes nothing else in it), or install a separate Miniforge."
        ask pick "Use which (1-$n), or 'new' for a separate Miniforge" 1
        if [ "$pick" != new ]; then
            CONDA=$(printf '%s\n' "$found" | sed -n "${pick}p")
            [ -n "$CONDA" ] || { warn "no such choice: $pick"; return 1; }
            conf_set "$CV_CONF" conda "$CONDA"
            return 0
        fi
    else
        say "There is no conda here yet. Setup installs Miniforge: conda and Python from the community-run conda-forge channel, in a directory of your own (no root, nothing outside that directory)."
    fi
    ask prefix "Install Miniforge in" "$HOME/miniforge3"
    case $prefix in "~"/*) prefix="$HOME/${prefix#\~/}" ;; esac
    if [ -e "$prefix" ]; then
        warn "$(tilde "$prefix") already exists; choose another directory, or remove it first."
        return 1
    fi
    local file
    file="Miniforge3-$(uname -s)-$(uname -m).sh"
    changes_list "downloads $file into $(tilde "$CV_HOME")/downloads" \
        "creates $(tilde "$prefix") (a few hundred MB)" \
        "does NOT change your shell start-up files: conda is not active in new terminals, and there is no (base) in your prompt; setup writes $(tilde "$CV_HOME")/activate for when you want it"
    ask_yn "Go ahead?" y || return 1
    mkdir -p "$CV_HOME/downloads"
    run curl -fL -o "$CV_HOME/downloads/$file" "$CV_MINIFORGE_URL/$file" || return 1
    run bash "$CV_HOME/downloads/$file" -b -p "$prefix" || return 1
    record_change "installed Miniforge" "$prefix"
    rm -f "$CV_HOME/downloads/$file"
    CONDA="$prefix/bin/conda"
    conf_set "$CV_CONF" conda "$CONDA"
    conf_set "$CV_CONF" conda_installed_by_setup "$prefix"
    return 0
}

# --------------------------------------------------------------------------
env_title() { echo "The cubevis environment on this computer"; }

# The path of a named environment of $CONDA (empty if there is none).
env_path() {
    "$CONDA" env list 2>/dev/null | awk -v n="$1" '$1 == n { print $NF; exit }'
}

env_check() {
    CONDA=$(conf_get "$CV_CONF" conda)
    ENV_NAME=$(conf_get "$CV_CONF" env "$CV_ENV_DEFAULT")
    RELEASE=$(conf_get "$CV_CONF" release "$(default_release)")
    LOCAL_PY=$(conf_get "$CV_CONF" python)
    if [ -z "$LOCAL_PY" ] || [ ! -x "$LOCAL_PY" ]; then
        CHECK_MSG="no environment yet"
        return 1
    fi
    if ! run_quiet "$LOCAL_PY" -c 'import importlib.metadata as m, sshpyk, jupyter_client; print(m.version("cubevis"))'; then
        CHECK_MSG="$(tilde "$LOCAL_PY") cannot import cubevis / sshpyk"
        return 1
    fi
    LOCAL_CUBEVIS=$(printf '%s\n' "$OUT" | tail -1)
    if [ "$LOCAL_CUBEVIS" != "$RELEASE" ]; then
        CHECK_MSG="cubevis $LOCAL_CUBEVIS installed, $RELEASE wanted"
        return 1
    fi
    CHECK_MSG="'$ENV_NAME', cubevis $RELEASE ($(tilde "$LOCAL_PY"))"
    return 0
}

# The release matching this checkout (tag vX.Y.Z), else the default.
default_release() {
    local t
    t=$(git -C "$CV_ROOT" describe --tags --exact-match 2>/dev/null)
    case $t in v[0-9]*) echo "${t#v}" ;; *) echo "$CV_RELEASE_DEFAULT" ;; esac
}

env_do() {
    local path pip_pkgs
    CONDA=$(conf_get "$CV_CONF" conda)
    ask ENV_NAME "Name of the environment" "$(conf_get "$CV_CONF" env "$CV_ENV_DEFAULT")"
    say "cubevis is installed as a release from PyPI, the same one on this computer and on each remote host. The default matches this checkout when it is at a release tag."
    ask RELEASE "cubevis release" "$(conf_get "$CV_CONF" release "$(default_release)")"
    pip_pkgs="$(printf '%s' "$CV_PIP_COMMON" | sed "s/@RELEASE@/$RELEASE/") $CV_PIP_LOCAL"
    path=$(env_path "$ENV_NAME")
    if [ -n "$path" ]; then
        say "The environment '$ENV_NAME' exists ($(tilde "$path")): setup installs the packages below into it with pip."
        changes_list "pip install $pip_pkgs into $(tilde "$path")" \
            "writes $(tilde "$CV_HOME")/activate"
    else
        changes_list "creates the conda environment '$ENV_NAME' with: $CV_CONDA_PKGS" \
            "then: pip install $pip_pkgs" \
            "writes $(tilde "$CV_HOME")/activate"
    fi
    ask_yn "Go ahead?" y || return 1
    if [ -z "$path" ]; then
        # shellcheck disable=SC2086  # the package list is meant to split
        run "$CONDA" create -y -n "$ENV_NAME" --override-channels -c conda-forge $CV_CONDA_PKGS || return 1
        path=$(env_path "$ENV_NAME")
        [ -n "$path" ] || { warn "conda created no environment '$ENV_NAME'"; return 1; }
        record_change "created conda environment" "$path"
    fi
    # shellcheck disable=SC2086
    run "$path/bin/python" -m pip install -q $pip_pkgs || return 1
    record_change "pip installed $pip_pkgs" "$path"
    conf_set "$CV_CONF" env "$ENV_NAME"
    conf_set "$CV_CONF" release "$RELEASE"
    conf_set "$CV_CONF" python "$path/bin/python"
    write_activate "$path"
    return 0
}

write_activate() {
    local base
    base=$("$CONDA" info --base 2>/dev/null)
    cat >"$CV_HOME/activate" <<EOF
# Use the cubevis environment in this shell (bash or zsh):
#     . $(tilde "$CV_HOME")/activate
# Written by cubevis-setup.
. "$base/etc/profile.d/conda.sh" && conda activate "$1"
EOF
    record_change "wrote" "$CV_HOME/activate"
}

# --------------------------------------------------------------------------
wrapper_title() { echo "The ssh wrapper (logs in with a stored password)"; }
wrapper_check() {
    if [ ! -x "$CV_BIN/ssh" ] || ! cmp -s "$CV_SETUP/bin/ssh" "$CV_BIN/ssh"; then
        CHECK_MSG="not installed, or older than $(tilde "$CV_SETUP")/bin/ssh"
        return 1
    fi
    if [ ! -L "$CV_BIN/scp" ]; then CHECK_MSG="no scp link"; return 1; fi
    if ! run_quiet perl -c "$CV_BIN/ssh"; then CHECK_MSG="perl cannot run it: $OUT"; return 1; fi
    CHECK_MSG="$(tilde "$CV_BIN")/ssh"
    return 0
}
wrapper_do() {
    say "The remote kernels connect with ssh, without a terminal to type a password in. The wrapper runs in front of ssh: for hosts listed in its rules it logs in once with the password from your password store and lets ssh share that connection; for every other host it simply runs ssh."
    say "It is NOT put on your PATH: only the kernels (and this setup) use it, so your own ssh, git and scp are unchanged."
    changes_list "copies $(tilde "$CV_SETUP")/bin/ssh to $(tilde "$CV_BIN")/ssh, and links scp to it"
    ask_yn "Go ahead?" y || return 1
    mkdir -p "$CV_BIN"
    run cp "$CV_SETUP/bin/ssh" "$CV_BIN/ssh" && chmod 755 "$CV_BIN/ssh" && ln -sf ssh "$CV_BIN/scp" || return 1
    record_change "installed the ssh wrapper" "$CV_BIN/ssh"
    return 0
}
