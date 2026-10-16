# ssh hosts: what setup knows about each one (hosts/<alias>.conf), the ssh
# configuration and wrapper rules generated from those files, and the
# connection steps.
#
# A host file has these keys:
#   alias      the ssh name setup gives it (Host in the generated config)
#   hostname   the real name (HostName); port: when not 22
#   user       your user name there
#   role       login (a gateway) or remote (runs the kernel)
#   via        the login host's alias, when it is reached through one
#   password   yes when it asks for a password; service: the stored entry
#   kernel, python, ms, data_dir   (remote hosts) the kernel and its data
#   tested     when the last test (kernel and plot) last passed

HOST_KEYS="alias hostname port user role via password service kernel python env data_dir ms shared_home tested"

host_file() { echo "$CV_HOSTS/$1.conf"; }

# Load host $1 into H_<key> variables.
host_load() {
    local k f
    f=$(host_file "$1")
    for k in $HOST_KEYS; do printf -v "H_$k" '%s' "$(conf_get "$f" "$k")"; done
    H_alias="$1"
}

host_set() {   # host_set ALIAS KEY VALUE
    conf_set "$(host_file "$1")" "$2" "$3"
}

host_list() {   # host_list [role]
    local f a
    for f in "$CV_HOSTS"/*.conf; do
        [ -f "$f" ] || continue
        a=$(basename "$f" .conf)
        if [ -z "$1" ] || [ "$(conf_get "$f" role)" = "$1" ]; then echo "$a"; fi
    done
}

# What setup runs ssh as: the wrapper with the generated configuration --
# exactly what the kernels use.
cv_ssh() { "$CV_BIN/ssh" -F "$CV_SSH_CONFIG" "$@"; }

# --------------------------------------------------------------------------
# Generated files
# --------------------------------------------------------------------------
write_ssh_files() {
    local a f tmp keys keep="${H_alias:-}"
    mkdir -p "$CV_HOME/ssh" "$CV_CM_DIR"
    chmod 700 "$CV_CM_DIR"
    # A socket path is limited to 104 bytes on macOS (108 on Linux): this
    # directory, '/', 40 for %C, and up to 17 that ssh adds while creating it.
    if [ $((${#CV_CM_DIR} + 58)) -gt 104 ]; then
        warn "$CV_CM_DIR is a long path: ssh may fail to share connections (socket path too long). A shorter XDG_CONFIG_HOME, or home directory, avoids that."
    fi
    tmp="$CV_SSH_CONFIG.tmp.$$"
    {
        echo "# Written by cubevis-setup from $(tilde "$CV_HOSTS")/*.conf; changes made"
        echo "# here are lost on the next run (run the setup again instead)."
        echo "# The kernels use this file (sshpyk --ssh-config); your ~/.ssh/config"
        echo "# includes it only if you said so."
        for a in $(host_list); do
            host_load "$a"
            echo
            echo "Host $H_alias"
            echo "    HostName $H_hostname"
            echo "    User $H_user"
            [ -n "$H_port" ] && [ "$H_port" != 22 ] && echo "    Port $H_port"
            [ -n "$H_via" ] && echo "    ProxyJump $H_via"
            echo "    ControlMaster auto"
            echo "    ControlPath $CV_CM_DIR/%C"
            echo "    ControlPersist 8h"
            echo "    ServerAliveInterval 30"
            echo "    ServerAliveCountMax 6"
        done
    } >"$tmp"
    if ! cmp -s "$tmp" "$CV_SSH_CONFIG"; then
        mv "$tmp" "$CV_SSH_CONFIG"
        record_change "wrote" "$CV_SSH_CONFIG"
    else
        rm -f "$tmp"
    fi

    tmp="$CV_SSH_RULES.tmp.$$"
    {
        echo "# Written by cubevis-setup: the hosts that ask for a password, and where"
        echo "# the wrapper ($(tilde "$CV_BIN")/ssh) finds it. Real host names, not aliases."
        for a in $(host_list); do
            host_load "$a"
            [ "$H_password" = yes ] || continue
            keys=$(store_rule_keys "$H_service" "$H_user")
            echo "$H_hostname  $keys"
        done
    } >"$tmp"
    if ! cmp -s "$tmp" "$CV_SSH_RULES"; then
        mv "$tmp" "$CV_SSH_RULES"
        record_change "wrote" "$CV_SSH_RULES"
    else
        rm -f "$tmp"
    fi
    # host_load above replaced the caller's H_* variables: put them back.
    [ -n "$keep" ] && [ -f "$(host_file "$keep")" ] && host_load "$keep"
    return 0
}

# --------------------------------------------------------------------------
# Step: names (always asked when adding a host)
# --------------------------------------------------------------------------
# Short name: first label of a host name.
short_name() { printf '%s' "${1%%.*}"; }

# Is ALIAS free (not one of ours, not defined in your own ssh config)?
alias_free() {
    local hn
    [ -f "$(host_file "$1")" ] && return 1
    hn=$(ssh -G "$1" 2>/dev/null | awk '$1 == "hostname" { print $2; exit }')
    [ "$hn" = "$1" ] || [ -z "$hn" ]
}

pick_alias() {   # pick_alias VAR HOSTNAME
    local a d
    d="cubevis-$(short_name "$2")"
    while :; do
        ask a "ssh name (alias) for $2" "$d"
        if [ "$a" = "$2" ]; then warn "the alias must differ from the real name (sshpyk needs that)"; continue; fi
        case $a in *[!A-Za-z0-9_.-]* | "") warn "letters, digits, . _ - only"; continue ;; esac
        if ! alias_free "$a"; then
            warn "'$a' is already defined (by setup or in your ~/.ssh/config); choose another"
            continue
        fi
        printf -v "$1" '%s' "$a"
        return 0
    done
}

split_port() {   # split_port "host[:port]" -> HP_HOST HP_PORT
    case $1 in
        *:*) HP_HOST=${1%:*}; HP_PORT=${1##*:} ;;
        *) HP_HOST=$1; HP_PORT="" ;;
    esac
}

# Ask for a new remote host (and its login host). Sets HOST.
ask_new_host() {
    local name user via login lname luser lalias logins i n
    heading "A remote host"
    say "The remote host is the computer that has the data and runs the kernel. Give its real name, as you would type it in 'ssh you@name' (add :port if it is not 22)."
    ask name "Remote host" ""
    [ -n "$name" ] || return 1
    split_port "$name"
    ask user "Your user name on $HP_HOST" "$(conf_get "$CV_CONF" last_user "$(id -un)")"
    conf_set "$CV_CONF" last_user "$user"
    local rhost="$HP_HOST" rport="$HP_PORT"

    say "Do you reach $rhost through a login host (a gateway you log in to first, as in 'ssh -J gateway $rhost')?"
    logins=$(host_list login)
    if [ -n "$logins" ]; then
        say "Login hosts set up already:"
        i=0
        for a in $logins; do i=$((i + 1)); host_load "$a"; echo "    $i) $a ($H_user@$H_hostname)"; done
        n=$i
        ask via "Use which (1-$n), 'new' for another one, or 'none'" 1
    else
        ask_yn "Through a login host?" y && via=new || via=none
    fi
    case $via in
        none) via="" ;;
        new)
            ask lname "Login host, real name (host[:port])" ""
            [ -n "$lname" ] || return 1
            split_port "$lname"
            ask luser "Your user name on $HP_HOST" "$user"
            pick_alias lalias "$HP_HOST"
            host_set "$lalias" hostname "$HP_HOST"
            host_set "$lalias" port "$HP_PORT"
            host_set "$lalias" user "$luser"
            host_set "$lalias" role login
            record_change "added login host" "$lalias ($luser@$HP_HOST)"
            via="$lalias" ;;
        *)
            via=$(printf '%s\n' "$logins" | sed -n "${via}p")
            [ -n "$via" ] || return 1 ;;
    esac

    pick_alias HOST "$rhost"
    host_set "$HOST" hostname "$rhost"
    host_set "$HOST" port "$rport"
    host_set "$HOST" user "$user"
    host_set "$HOST" role remote
    host_set "$HOST" via "$via"
    record_change "added remote host" "$HOST ($user@$rhost${via:+ via $via})"
    write_ssh_files
    return 0
}

# --------------------------------------------------------------------------
# Step: the optional Include in ~/.ssh/config
# --------------------------------------------------------------------------
INCLUDE_MARK="# cubevis-setup: the hosts it set up (see $(tilde "$CV_HOME")/changes.log)"

include_title() { echo "Use these hosts from a terminal too (~/.ssh/config)"; }
include_check() {
    local want
    want=$(conf_get "$CV_CONF" ssh_include)
    if [ "$want" = no ]; then CHECK_MSG="not wanted (the kernels do not need it)"; return 0; fi
    if [ -f "$HOME/.ssh/config" ] && grep -qF "Include $CV_SSH_CONFIG" "$HOME/.ssh/config"; then
        CHECK_MSG="~/.ssh/config includes $(tilde "$CV_SSH_CONFIG")"
        return 0
    fi
    CHECK_MSG="not decided yet"
    return 1
}
include_do() {
    local f="$HOME/.ssh/config" b tmp
    if [ -f "$f" ] && grep -qF "Include $CV_SSH_CONFIG" "$f"; then
        say "~/.ssh/config includes $(tilde "$CV_SSH_CONFIG") already. (To stop that, delete the two lines marked 'cubevis-setup' at its top.)"
        return 0
    fi
    say "The kernels read $(tilde "$CV_SSH_CONFIG") directly, so nothing in your own ~/.ssh/config is needed or changed for them."
    say "If you also want 'ssh $H_alias' to work in a terminal, setup can add one line at the top of ~/.ssh/config that includes that file (your own entries stay as they are; the line is marked so it is easy to find). With a password host, a terminal ssh still asks for the password unless the wrapper is used: $(tilde "$CV_BIN")/ssh $H_alias."
    if ! ask_yn "Add the line?" n; then
        conf_set "$CV_CONF" ssh_include no
        return 0
    fi
    changes_list "adds 2 lines at the top of ~/.ssh/config (a copy of the old file goes to $(tilde "$CV_BACKUPS"))"
    mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
    tmp="$CV_HOME/ssh/config.new.$$"
    { echo "$INCLUDE_MARK"; echo "Include $CV_SSH_CONFIG"; echo; [ -f "$f" ] && cat "$f"; } >"$tmp"
    if [ -f "$f" ]; then
        b=$(backup_file "$f")
        # Rewrite in place (cat, not mv): keeps a symbolic link and the mode.
        cat "$tmp" >"$f" || { rm -f "$tmp"; return 1; }
        record_change "added an Include line (backup: $(tilde "$b"))" "$f"
    else
        cat "$tmp" >"$f" && chmod 600 "$f"
        record_change "created" "$f"
    fi
    rm -f "$tmp"
    if ! ssh -G "$H_alias" >/dev/null 2>&1; then
        warn "ssh cannot read ~/.ssh/config now; putting the old one back"
        [ -n "$b" ] && cat "$b" >"$f"
        return 1
    fi
    conf_set "$CV_CONF" ssh_include yes
    return 0
}

# --------------------------------------------------------------------------
# Step: log in to a hop (HOP = alias), with a key or a stored password
# --------------------------------------------------------------------------
hop_title() { host_load "$HOP"; echo "Log in to $HOP ($H_user@$H_hostname) without typing"; }

# One test login: BatchMode, so ssh itself never asks; the wrapper logs in
# with the stored password if the rules list the host.
hop_try() {
    run_quiet cv_ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=30 "$HOP" true </dev/null
}

hop_check() {
    host_load "$HOP"
    # A gpg-encrypted password: let gpg ask for its passphrase here, where
    # there is a terminal, rather than fail inside the wrapper.
    if [ "$H_password" = yes ] && [ -t 0 ]; then store_unlock "$H_service" "$H_user"; fi
    if hop_try; then
        if [ "$H_password" = yes ]; then CHECK_MSG="password from $(store_where "$H_service" "$H_user")"
        else CHECK_MSG="with an ssh key"; fi
        return 0
    fi
    CHECK_MSG=$(printf '%s\n' "$OUT" | grep -v '^\[ssh-wrapper\]' | tail -1)
    [ -n "$CHECK_MSG" ] || CHECK_MSG=$(printf '%s\n' "$OUT" | tail -1)
    return 1
}

hop_do() {
    local why svc a other need_put=yes
    if hop_check >/dev/null 2>&1; then
        # --redo, and the login works already.
        [ "$H_password" = yes ] || { say "$HOP takes your ssh key: nothing to do."; return 0; }
        ask_yn "Logging in to $HOP works. Store a new password for it (after a change)?" n || return 0
        store_put "$H_service" "$H_user" "$H_hostname"
        return
    fi
    host_load "$HOP"
    why=$(printf '%s\n' "$OUT" | grep -v '^\[ssh-wrapper\]' | tail -1)
    case $OUT in
        *"Could not resolve"* | *"Name or service not known"* | *"nodename nor servname"*)
            say "ssh cannot find '$H_hostname' ($why). Check the name; a host behind a login host is often known only there."
            return 1 ;;
        *"Connection timed out"* | *"Connection refused"* | *"No route"* | *"Operation timed out"*)
            say "No connection to $H_hostname: $why. A VPN, a firewall, or a login host may be needed."
            return 1 ;;
        *"REMOTE HOST IDENTIFICATION HAS CHANGED"* | *"Host key verification failed"*)
            say "The host key of $H_hostname does not match the one saved before (in ~/.ssh/known_hosts). Ask whoever runs the host whether it changed; setup does not override that."
            return 1 ;;
    esac
    [ -n "$why" ] || why="it refused the login"
    if [ "$H_password" = yes ]; then
        say "Logging in with the stored password failed: $why"
        say "The wrapper tries a stored password once per login, so a wrong one does not lock your account at once; but sites count the failures. Store it again only when you are sure of it."
        ask_yn "Type the password again?" y || return 1
        store_put "$H_service" "$H_user" "$H_hostname" || return 1
    else
        say "$H_hostname does not accept a key from here ($why), so it needs your password."
        ask_yn "Store your password for $H_user@$H_hostname, for the wrapper to use?" y || return 1
        store_choose || return 1
        svc=""
        # The same user's password stored for another host: often the same.
        for a in $(host_list); do
            [ "$a" = "$HOP" ] && continue
            other=$(conf_get "$(host_file "$a")" service)
            [ -n "$other" ] && [ "$(conf_get "$(host_file "$a")" user)" = "$H_user" ] || continue
            if ask_yn "Is it the same password as for $a?" y; then svc="$other"; need_put=no; fi
            break
        done
        if [ -z "$svc" ]; then
            ask svc "Name for the stored entry (hosts with the same password can share one)" "cubevis-ssh"
            if store_has "$svc" "$H_user" && [ "$(conf_get "$CV_CONF" store)" != command ]; then
                ask_yn "There is a stored password '$svc' for $H_user already. Use it?" y && need_put=no
            fi
        fi
        if [ "$need_put" = yes ]; then store_put "$svc" "$H_user" "$H_hostname" || return 1; fi
        host_set "$HOP" password yes
        host_set "$HOP" service "$svc"
        write_ssh_files
    fi
    host_load "$HOP"
    store_unlock "$H_service" "$H_user" || warn "could not read the stored password back"
    say "Trying the login (one attempt) ..."
    return 0
}
