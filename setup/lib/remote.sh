# Steps for a remote host (HOST = its alias): look around, set up the
# environment there, test data, the kernel definition, and the last test.

REMOTE_STEPS="probe renv data kernel final"

host_dir() { echo "$CV_HOSTS/$HOST"; }

# Run a bash script (given as text) on $HOST: bash there, whatever your
# login shell is.
remote_bash() { printf '%s\n' "$1" | cv_ssh -o BatchMode=yes "$HOST" bash -s; }

# --------------------------------------------------------------------------
probe_title() { echo "Look at $HOST (changes nothing there)"; }

# P_<key> from the saved probe.
probe_get() { conf_get "$(host_dir)/probe.txt" "$1"; }

probe_check() {
    local d
    d=$(host_dir); mkdir -p "$d"
    if ! run_quiet remote_bash "set -- $(sq "$(cat "$CV_HOME/.home-id")") \
$(sq "$(conf_get "$CV_CONF" env "$CV_ENV_DEFAULT")") $(sq "$(conf_get "$CV_CONF" python)")
$(cat "$CV_SETUP/lib/remote-probe.sh")"; then
        CHECK_MSG=$(printf '%s\n' "$OUT" | tail -1)
        return 1
    fi
    printf '%s\n' "$OUT" | grep '^[a-z_]*=' >"$d/probe.txt"
    local conda shared net=""
    conda=$(probe_get conda)
    shared=$(probe_get shared_home)
    [ "$(probe_get net_conda)" = yes ] || net="$net conda-forge"
    [ "$(probe_get net_pypi)" = yes ] || net="$net PyPI"
    [ -z "$net" ] || net=", cannot reach:$net"
    CHECK_MSG="$(probe_get os) $(probe_get arch), home $(probe_get home)$([ "$shared" = yes ] && echo ' (shared with this computer)'), conda: $([ -n "$conda" ] && tilde "$conda" || echo none)$net"
    host_set "$HOST" shared_home "$shared"
    return 0
}
probe_do() {
    say "Setup could not run its look-around script on $HOST: $CHECK_MSG"
    say "It needs bash there. The full output is in $(tilde "$CV_LOG")."
    return 1
}

# --------------------------------------------------------------------------
renv_title() { echo "The cubevis environment on $HOST"; }
renv_check() {
    host_load "$HOST"
    RELEASE=$(conf_get "$CV_CONF" release)
    if [ -z "$H_python" ]; then CHECK_MSG="not set up yet"; return 1; fi
    if ! run_quiet remote_bash "$(sq "$H_python") -c 'import cubevis, ipykernel, jupyter_client, xarray_ms, importlib.metadata as m; print(m.version(\"cubevis\"))'"; then
        CHECK_MSG="$H_python cannot import cubevis"
        return 1
    fi
    local v
    v=$(printf '%s\n' "$OUT" | tail -1)
    if [ "$v" != "$RELEASE" ]; then CHECK_MSG="cubevis $v there, $RELEASE here"; return 1; fi
    CHECK_MSG="cubevis $v ($H_python)"
    return 0
}

renv_do() {
    local d script conda prefix home os arch env pip c missing=""
    host_load "$HOST"
    d=$(host_dir); script="$d/remote-setup.sh"
    env=$(conf_get "$CV_CONF" env "$CV_ENV_DEFAULT")
    RELEASE=$(conf_get "$CV_CONF" release)
    home=$(probe_get home); os=$(probe_get os); arch=$(probe_get arch)

    # Shared home, same kind of computer: the environment made here runs there.
    if [ "$(probe_get local_python_runs)" = yes ]; then
        say "$HOST shares your home directory and can run the environment set up on this computer ($(conf_get "$CV_CONF" python)). Nothing needs installing there."
        if ask_yn "Use it?" y; then
            host_set "$HOST" python "$(conf_get "$CV_CONF" python)"
            return 0
        fi
    fi

    conda=$(probe_get conda)
    if [ -n "$conda" ]; then
        say "$HOST has conda ($conda): setup adds the environment '$env' to it."
    else
        prefix="$home/miniforge3"
        # A shared home may hold this computer's Miniforge, which does not run there.
        if [ -n "$(probe_get conda_broken)" ] || [ "$(probe_get shared_home)" = yes ]; then
            prefix="$home/miniforge3-$os-$arch"
        fi
        say "$HOST has no conda: setup installs Miniforge there, in $prefix."
        [ "$(probe_get net_github)" = yes ] || missing="$missing github.com"
    fi
    [ "$(probe_get net_conda)" = yes ] || missing="$missing conda.anaconda.org"
    [ "$(probe_get net_pypi)" = yes ] || missing="$missing pypi.org"
    if [ -n "$missing" ]; then
        warn "$HOST could not reach:$missing. The install downloads from there; it will fail unless a proxy is set up in your login there."
    fi
    if [ "$(probe_get shared_home)" = yes ]; then
        say "Your home directory there is the one on this computer (shared), so what is installed there is also visible here."
    fi
    pip=$(printf '%s' "$CV_PIP_COMMON" | sed "s/@RELEASE@/$RELEASE/")
    {
        echo "#!/bin/bash"
        echo "# cubevis remote setup, written by cubevis-setup for one host:"
        echo "#     bash remote-setup.sh        (on the host)"
        echo "CV_ALIAS='$HOST'"
        echo "CV_GENERATED='$(date '+%Y-%m-%d %H:%M') on $(hostname)'"
        echo "CV_CONDA='$conda'"
        echo "CV_PREFIX='$prefix'"
        echo "CV_ENV='$env'"
        echo "CV_CONDA_PKGS='$CV_CONDA_PKGS'"
        echo "CV_PIP_PKGS='$pip'"
        echo "CV_MINIFORGE_URL='$CV_MINIFORGE_URL'"
        echo
        cat "$CV_SETUP/lib/remote-setup.sh"
    } >"$script"
    changes_list "on $HOST: $([ -n "$conda" ] && echo "adds the environment '$env' to $conda" || echo "installs Miniforge in $prefix, then creates the environment '$env'")" \
        "on $HOST: pip install $pip into it" \
        "nothing else there: no shell start-up files, no (base) in your prompt"
    say "Setup wrote this as one script, $(tilde "$script"); it can run it there for you, or you can copy it and run it yourself."
    while :; do
        ask_choice c "[r]un it on $HOST now, [v]iew it first, [c]opy it there to run yourself, or [s]kip?" r "r v c s"
        case $c in
            v) "${PAGER:-less}" "$script" ;;
            s) return 1 ;;
            r)
                run_remote_script "$script" && return 0
                return 1 ;;
            c)
                run "$CV_BIN/scp" -F "$CV_SSH_CONFIG" "$script" "$HOST:cubevis-remote-setup.sh" || return 1
                say "On $HOST (ssh $H_alias, or however you usually log in), run:"
                echo "      bash ~/cubevis-remote-setup.sh"
                pause "Press Enter here when it has finished."
                probe_check >/dev/null
                c=$(probe_get env_python)
                [ -n "$c" ] || { warn "setup does not see the environment '$env' on $HOST yet"; return 1; }
                host_set "$HOST" python "$c"
                record_change "on $HOST: ran remote-setup.sh by hand" "$c"
                return 0 ;;
        esac
    done
}

run_remote_script() {
    local log py line
    log="$(host_dir)/remote-setup.log"
    say "Running it on $HOST (the output is also kept in $(tilde "$log")); installing takes a few minutes."
    printf '%s\n' "  ${C_DIM}\$ $(tilde "$CV_BIN")/ssh $HOST bash -s < $(tilde "$1")${C_0}"
    cv_ssh -o BatchMode=yes "$HOST" bash -s <"$1" 2>&1 | tee "$log" | sed 's/^/    | /'
    while IFS= read -r line; do
        case $line in "CUBEVIS_CHANGE "*) record_change "on $HOST" "${line#CUBEVIS_CHANGE }" ;; esac
    done <"$log"
    py=$(sed -n 's/^CUBEVIS_RESULT python=//p' "$log" | tail -1)
    if [ -z "$py" ]; then
        warn "it did not finish: $(grep '^CUBEVIS_ERROR' "$log" | tail -1 | sed 's/^CUBEVIS_ERROR //')"
        return 1
    fi
    host_set "$HOST" python "$py"
    return 0
}

# --------------------------------------------------------------------------
data_title() { echo "A measurement set on $HOST for the test"; }
data_check() {
    host_load "$HOST"
    if [ -z "$H_ms" ]; then CHECK_MSG="none chosen yet"; return 1; fi
    if [ "$H_ms" = none ]; then CHECK_MSG="skipped: no plot in the test"; return 0; fi
    if ! run_quiet remote_bash "test -d $(sq "$H_ms")"; then
        CHECK_MSG="$H_ms is not there"
        return 1
    fi
    CHECK_MSG="$H_ms"
    return 0
}
data_do() {
    local c dir url
    host_load "$HOST"
    say "The last test draws a plot through the kernel, so it needs a measurement set on $HOST. Setup can download a small test data set there ($CV_TEST_MS), or use one that is there already."
    ask_choice c "[d]ownload the test data, [p]ath of your own, or [s]kip the plot test?" d "d p s"
    case $c in
        s) host_set "$HOST" ms none; return 0 ;;
        p)
            ask H_ms "Path of the measurement set on $HOST" ""
            host_set "$HOST" ms "$H_ms"
            return 0 ;;
    esac
    ask dir "Directory for it on $HOST" "${H_data_dir:-$(probe_get home)/cubevis-data}"
    changes_list "on $HOST: creates $dir/$CV_TEST_MS (downloaded and unpacked; the archive is not kept)"
    ask_yn "Go ahead?" y || return 1
    url="$CV_TEST_DATA_URL"
    say "Downloading on $HOST ..."
    run remote_bash "set -e -o pipefail; mkdir -p $(sq "$dir"); cd $(sq "$dir")
if command -v curl >/dev/null; then curl -fL $(sq "$url"); else wget -O - $(sq "$url"); fi | tar xzf -" || return 1
    record_change "on $HOST: downloaded the test data" "$dir/$CV_TEST_MS"
    host_set "$HOST" data_dir "$dir"
    host_set "$HOST" ms "$dir/$CV_TEST_MS"
    return 0
}

# --------------------------------------------------------------------------
kernel_title() { echo "The kernel definition for $HOST"; }
kernel_check() {
    host_load "$HOST"
    local py
    py=$(conf_get "$CV_CONF" python)
    if [ -z "$H_kernel" ]; then CHECK_MSG="not defined yet"; return 1; fi
    if ! run_quiet "$py" - "$H_kernel" "$HOST" "$H_python" "$CV_SSH_CONFIG" "$CV_BIN/ssh" <<'EOF'
import sys
from jupyter_client.kernelspec import KernelSpecManager
name, alias, python, config, ssh = sys.argv[1:]
spec = KernelSpecManager().get_kernel_spec(name)
c = spec.metadata["kernel_provisioner"]["config"]
want = {"ssh_host_alias": alias, "remote_python": python, "ssh_config": config, "ssh": ssh}
bad = [f"{k}={c.get(k)!r}" for k, v in want.items() if c.get(k) != v]
print(spec.resource_dir)
sys.exit("differs: " + ", ".join(bad) if bad else 0)
EOF
    then
        CHECK_MSG="'$H_kernel': $(printf '%s\n' "$OUT" | tail -1)"
        return 1
    fi
    CHECK_MSG="'$H_kernel' ($(tilde "$(printf '%s\n' "$OUT" | tail -1)"))"
    return 0
}
# Is kernel name $1 used by another host? (OWNER = that host)
kernel_owner() {
    local a
    for a in $(host_list remote); do
        [ "$a" = "$HOST" ] && continue
        if [ "$(conf_get "$(host_file "$a")" kernel)" = "$1" ]; then OWNER=$a; return 0; fi
    done
    return 1
}

kernel_do() {
    local k py
    host_load "$HOST"
    py=$(conf_get "$CV_CONF" python)
    say "A Jupyter kernel definition tells visplot (backend=\"remote\", kernel_name=...) how to start Python on $HOST: through ssh, with the wrapper and setup's ssh configuration."
    while :; do
        # Default: the alias, with _ for - (cubevis-zuul06 -> cubevis_zuul06).
        ask k "Kernel name" "${H_kernel:-$(printf '%s' "$HOST" | tr -c 'A-Za-z0-9_' '_')}"
        case $k in *[!A-Za-z0-9_.-]* | "") warn "letters, digits, . _ - only"; continue ;; esac
        if kernel_owner "$k"; then warn "'$k' is the kernel for $OWNER already"; continue; fi
        break
    done
    changes_list "adds the kernel definition '$k' to your Jupyter kernels (in your home directory; 'sshpyk list' shows it)"
    ask_yn "Go ahead?" y || return 1
    # Jupyter's runtime directory: sshpyk writes connection files there but
    # does not create it (a new account has none). cubevis releases after
    # 1.0.96 create it too.
    "$py" -c 'import os; from jupyter_core.paths import jupyter_runtime_dir as d; os.makedirs(d(), mode=0o700, exist_ok=True)'
    run "$py" -m sshpyk.cli add --replace --kernel-name "$k" \
        --display-name "cubevis on $H_hostname" --language python \
        --ssh-host-alias "$HOST" --ssh-config "$CV_SSH_CONFIG" --ssh "$CV_BIN/ssh" \
        --remote-python "$H_python" --remote-kernel-name python3 || return 1
    host_set "$HOST" kernel "$k"
    record_change "added kernel" "$k (for $HOST)"
    return 0
}

# --------------------------------------------------------------------------
final_title() { echo "Start the kernel on $HOST and draw a plot"; }
final_check() {
    local py png
    host_load "$HOST"
    py=$(conf_get "$CV_CONF" python)
    png="$(host_dir)/test.png"
    set -- "$H_kernel"
    if [ -n "$H_ms" ] && [ "$H_ms" != none ]; then
        set -- "$@" --ms "$H_ms" --png "$png"
        case $H_ms in */"$CV_TEST_MS") set -- "$@" --field "$CV_TEST_FIELD" ;; esac
    fi
    echo "  testing (a minute or so) ..."
    # From the state directory: the installed cubevis, not this checkout.
    log "run (quiet): $py check_kernel.py $*"
    OUT=$(cd "$CV_HOME" && "$py" "$CV_SETUP/lib/check_kernel.py" "$@" 2>&1)
    local rc=$?
    printf '%s\n' "$OUT" >>"$CV_LOG"
    if [ $rc -ne 0 ]; then
        printf '%s\n' "$OUT" | grep -E '^(ok|FAIL|note)' | sed 's/^/    /'
        CHECK_MSG="see above"
        return 1
    fi
    printf '%s\n' "$OUT" | grep -E '^(ok|note)' | sed 's/^/    /'
    CHECK_MSG="kernel '$H_kernel' works"
    host_set "$HOST" tested "$(date '+%Y-%m-%d %H:%M')"
    return 0
}
final_do() {
    say "The test failed. To see more, run the setup's check with logging:"
    echo "      SSH_WRAPPER_DEBUG=1 CUBEVIS_DEBUG=1 bash $(tilde "$CV_SETUP")/cubevis-setup --check $HOST"
    say "(the wrapper then logs to ~/.ssh/ssh-wrapper.log)."
    return 1
}
