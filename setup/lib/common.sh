# Shared helpers for cubevis-setup: messages, questions, state files, the
# record of changes, and the step runner.
#
# Written for bash 3.2 (the bash in macOS): no associative arrays, no
# ${var,,}, no mapfile, no ;& in case.

# --------------------------------------------------------------------------
# Where things live
# --------------------------------------------------------------------------
CV_HOME="${XDG_CONFIG_HOME:-$HOME/.config}/cubevis"
CV_CONF="$CV_HOME/setup.conf"          # answers about this computer
CV_HOSTS="$CV_HOME/hosts"              # one <alias>.conf per ssh host
CV_LOG="$CV_HOME/setup.log"            # every command run, with output
CV_CHANGES="$CV_HOME/changes.log"      # every change made, one per line
CV_BACKUPS="$CV_HOME/backups"
CV_BIN="$CV_HOME/bin"                  # the ssh wrapper
CV_SSH_CONFIG="$CV_HOME/ssh/config"    # generated ssh configuration
CV_SSH_RULES="$CV_HOME/ssh/rules"      # generated wrapper rules
CV_CM_DIR="$CV_HOME/cm"                # ssh shared-connection sockets

# gpg (pass, gpg-file) asks for its passphrase on the terminal named here.
if [ -t 0 ] && [ -z "${GPG_TTY:-}" ]; then GPG_TTY=$(tty); export GPG_TTY; fi

RUN_CHANGES=""      # changes made by this run (newline separated)
STEP_N=0
FAILED=0

# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------
if [ -t 1 ]; then
    C_B=$(printf '\033[1m'); C_DIM=$(printf '\033[2m'); C_R=$(printf '\033[31m')
    C_G=$(printf '\033[32m'); C_Y=$(printf '\033[33m'); C_0=$(printf '\033[0m')
else
    C_B=""; C_DIM=""; C_R=""; C_G=""; C_Y=""; C_0=""
fi

# A paragraph, wrapped.
say() { printf '%s\n' "$*" | fold -s -w 76 | sed 's/^/  /'; }
blank() { echo; }
heading() { echo; printf '%s\n' "${C_B}$*${C_0}"; }
# hang LEAD TEXT: TEXT wrapped, the first line after LEAD (printed as
# is) and the rest indented by WIDTH spaces (LEAD's visible width).
hang() {   # hang LEAD WIDTH TEXT
    local pad
    pad=$(printf '%*s' "$2" '')
    printf '%s\n' "$3" | fold -s -w $((78 - $2)) | sed -e "1s/^/$(printf '%s' "$1" | sed 's/[\/&]/\\&/g')/" -e "2,\$s/^/$pad/"
}
ok() { hang "  ${C_G}ok${C_0}    " 8 "$*"; }
warn() { hang "  ${C_Y}note${C_0}  " 8 "$*"; }
fail() { hang "  ${C_R}FAIL${C_0}  " 8 "$*"; }
die() { fail "$*"; exit 1; }

# "What this changes" list: changes_list "item" "item" ...
changes_list() {
    printf '%s\n' "  ${C_B}Changes:${C_0}"
    local c
    for c in "$@"; do [ -n "$c" ] && hang "    - " 6 "$c"; done
}

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$CV_LOG" 2>/dev/null; }

has() { command -v "$1" >/dev/null 2>&1; }

# Show a command, log it, run it; its output goes to the screen and the log.
run() {
    printf '%s\n' "  ${C_DIM}\$ $(quote_cmd "$@")${C_0}"
    log "run: $(quote_cmd "$@")"
    "$@" 2>&1 | tee -a "$CV_LOG"
    return "${PIPESTATUS[0]}"
}

# The same for a command that asks you something (a password): no tee, so
# it has the terminal to itself.
run_tty() {
    printf '%s\n' "  ${C_DIM}\$ $(quote_cmd "$@")${C_0}"
    log "run (interactive): $(quote_cmd "$@")"
    "$@"
}

# Run quietly, keeping the output in $OUT (and the log).
run_quiet() {
    log "run (quiet): $(quote_cmd "$@")"
    OUT=$("$@" 2>&1)
    local rc=$?
    printf '%s\n' "$OUT" >>"$CV_LOG" 2>/dev/null
    log "  -> exit $rc"
    return $rc
}

quote_cmd() {
    local out="" a
    for a in "$@"; do
        case $a in
            *[!A-Za-z0-9_./:=@%+,-]* | "") out="$out '$(printf '%s' "$a" | sed "s/'/'\\\\''/g")'" ;;
            *) out="$out $a" ;;
        esac
    done
    printf '%s' "${out# }"
}

# Single-quote a value for a remote shell command.
sq() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }

# Shorten $HOME to ~ for display.
tilde() { case $1 in "$HOME"/*) printf '~/%s' "${1#"$HOME"/}" ;; *) printf '%s' "$1" ;; esac; }

# --------------------------------------------------------------------------
# Questions. Enter takes the default in [brackets].
# --------------------------------------------------------------------------
# ask VAR "question" "default"
ask() {
    local __v __d="$3"
    if [ -n "$CV_YES" ]; then __v=""; else
        read -r -p "  $2${__d:+ [$__d]}: " __v || exit 1
    fi
    [ -z "$__v" ] && __v="$__d"
    printf -v "$1" '%s' "$__v"
}

# ask_yn "question" y|n  -> 0 for yes
ask_yn() {
    local __a __d="${2:-y}" __p="[Y/n]"
    [ "$__d" = n ] && __p="[y/N]"
    while :; do
        if [ -n "$CV_YES" ]; then __a=""; else
            read -r -p "  $1 $__p " __a || exit 1
        fi
        [ -z "$__a" ] && __a="$__d"
        case $__a in
            y | Y | yes | Yes) return 0 ;;
            n | N | no | No) return 1 ;;
        esac
    done
}

# ask_choice VAR "question" default "a b c"   (single letters or words)
ask_choice() {
    local __a
    while :; do
        if [ -n "$CV_YES" ]; then __a=""; else
            read -r -p "  $2 [$3] " __a || exit 1
        fi
        [ -z "$__a" ] && __a="$3"
        case " $4 " in *" $__a "*) printf -v "$1" '%s' "$__a"; return 0 ;; esac
        echo "  (one of: $4)"
    done
}

pause() { [ -n "$CV_YES" ] || read -r -p "  ${1:-Press Enter to continue.} " _ || exit 1; }

# --------------------------------------------------------------------------
# State files: plain "key=value" lines (no quoting, never sourced).
# --------------------------------------------------------------------------
conf_get() {   # conf_get FILE KEY [DEFAULT]
    local k v
    if [ -f "$1" ]; then
        while IFS='=' read -r k v; do
            if [ "$k" = "$2" ]; then printf '%s' "$v"; return 0; fi
        done <"$1"
    fi
    printf '%s' "${3:-}"
    return 1
}

conf_set() {   # conf_set FILE KEY VALUE
    local f="$1" tmp k v
    mkdir -p "$(dirname "$f")"
    tmp="$f.tmp.$$"
    : >"$tmp"
    if [ -f "$f" ]; then
        while IFS='=' read -r k v; do
            [ "$k" = "$2" ] || printf '%s=%s\n' "$k" "$v" >>"$tmp"
        done <"$f"
    fi
    printf '%s=%s\n' "$2" "$3" >>"$tmp"
    mv "$tmp" "$f"
}

# --------------------------------------------------------------------------
# The record of changes, and backups of files we did not create.
# --------------------------------------------------------------------------
record_change() {   # record_change "what" "path or name"
    local line
    line="$(date '+%Y-%m-%d %H:%M:%S')  $1: $2"
    mkdir -p "$CV_HOME"
    printf '%s\n' "$line" >>"$CV_CHANGES"
    RUN_CHANGES="$RUN_CHANGES$1: $2
"
}

# Copy a file to backups/ before changing it; prints the backup's path.
backup_file() {
    local f="$1" b
    [ -e "$f" ] || return 0
    mkdir -p "$CV_BACKUPS"
    b="$CV_BACKUPS/$(basename "$f").$(date '+%Y%m%d-%H%M%S')"
    cp -p "$f" "$b" && printf '%s' "$b"
}

# --------------------------------------------------------------------------
# Steps
#
# A step is three functions named after it:
#   <step>_title   prints a short title
#   <step>_check   returns 0 when the step is already done (no questions, no
#                  changes); leaves a one-line result in CHECK_MSG
#   <step>_do      explains, asks, makes the changes; returns 0 when done
# run_step runs _check, and _do only when _check fails (or with --redo);
# --check runs only the checks. Steps that only look (they ask nothing and
# change nothing) are listed in LOOK_STEPS: --redo does not repeat them.
# --------------------------------------------------------------------------
LOOK_STEPS="platform probe final"

run_step() {
    local s="$1" title c redo="$CV_REDO"
    STEP_N=$((STEP_N + 1))
    title=$("${s}_title")
    case " $LOOK_STEPS " in *" $s "*) redo="" ;; esac
    while :; do
        CHECK_MSG=""
        if [ -z "$redo" ] || [ -n "$CV_CHECK_ONLY" ]; then
            if "${s}_check"; then
                ok "$title${CHECK_MSG:+ -- $CHECK_MSG}"
                return 0
            fi
        fi
        if [ -n "$CV_CHECK_ONLY" ]; then
            fail "$title${CHECK_MSG:+ -- $CHECK_MSG}"
            FAILED=$((FAILED + 1))
            return 1
        fi
        heading "$STEP_N. $title"
        if "${s}_do"; then
            CHECK_MSG=""
            if "${s}_check"; then
                ok "$title${CHECK_MSG:+ -- $CHECK_MSG}"
                return 0
            fi
        fi
        fail "$title${CHECK_MSG:+ -- $CHECK_MSG}"
        ask_choice c "[r]etry this step, [s]kip it, or [q]uit?" r "r s q"
        case $c in
            r) continue ;;
            s) FAILED=$((FAILED + 1)); return 1 ;;
            q) show_run_changes; exit 1 ;;
        esac
    done
}

show_run_changes() {
    [ -n "$RUN_CHANGES" ] || return 0
    heading "Changed in this run (all runs: $(tilde "$CV_CHANGES"))"
    printf '%s' "$RUN_CHANGES" | sed 's/^/    /'
}
