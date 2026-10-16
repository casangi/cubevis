# Password stores: where the wrapper finds the password for a host that
# asks for one. Chosen once (setup.conf: store=...), when the first such
# host is set up.
#
#   keychain     macOS Keychain (the 'security' command)
#   secret-tool  the desktop keyring on Linux (GNOME Keyring, KWallet)
#   pass         the 'pass' password store (gpg-encrypted files)
#   gpg-file     one gpg-encrypted file per password, kept by setup
#   command      your own command that prints the password

STORES_ALL="keychain secret-tool pass gpg-file command"

# Is store $1 usable here? STORE_WHY says why (not).
store_available() {
    local out
    case $1 in
        keychain)
            [ "$(uname -s)" = Darwin ] && has security && { STORE_WHY="macOS Keychain"; return 0; }
            STORE_WHY="macOS only"; return 1 ;;
        secret-tool)
            has secret-tool || { STORE_WHY="secret-tool is not installed"; return 1; }
            out=$(secret-tool lookup service cubevis-setup-probe 2>&1 </dev/null)
            [ -z "$out" ] && { STORE_WHY="your desktop keyring is running"; return 0; }
            STORE_WHY="no desktop keyring in this session (works only where you log in to a desktop)"; return 1 ;;
        pass)
            has pass || { STORE_WHY="pass is not installed"; return 1; }
            [ -f "${PASSWORD_STORE_DIR:-$HOME/.password-store}/.gpg-id" ] && { STORE_WHY="you already use pass"; return 0; }
            has gpg && { STORE_WHY="pass is installed (setup will initialise it)"; return 0; }
            STORE_WHY="pass needs gpg"; return 1 ;;
        gpg-file)
            has gpg && { STORE_WHY="gpg is installed"; return 0; }
            STORE_WHY="gpg is not installed"; return 1 ;;
        command)
            STORE_WHY="any command that prints the password (a password manager's CLI)"; return 0 ;;
    esac
    return 1
}

# The store to suggest: the one most likely already in use here.
store_default() {
    if [ "$(uname -s)" = Darwin ]; then echo keychain; return; fi
    if has pass && [ -f "${PASSWORD_STORE_DIR:-$HOME/.password-store}/.gpg-id" ]; then echo pass; return; fi
    if store_available secret-tool; then echo secret-tool; return; fi
    if store_available pass; then echo pass; return; fi
    if store_available gpg-file; then echo gpg-file; return; fi
    echo command
}

# Choose the store (once). Returns 1 if none can be used.
store_choose() {
    STORE=$(conf_get "$CV_CONF" store)
    [ -n "$STORE" ] && store_available "$STORE" && return 0
    local s avail="" def
    say "The password is kept in a password store and read by the wrapper only when it logs in. Stores that work here:"
    for s in $STORES_ALL; do
        if store_available "$s"; then
            printf '    %-12s %s\n' "$s" "$STORE_WHY"
            avail="$avail $s"
        fi
    done
    def=$(store_default)
    if [ "$(uname -s)" != Darwin ]; then
        say "On Linux: if you use pass already, keep using it. secret-tool suits a desktop you log in to; it cannot be used over ssh or without a desktop session. gpg-file works anywhere gpg does."
    fi
    ask_choice STORE "Which store?" "$def" "${avail# }"
    if [ "$STORE" = pass ] && [ ! -f "${PASSWORD_STORE_DIR:-$HOME/.password-store}/.gpg-id" ]; then
        pass_init || return 1
    fi
    if [ "$STORE" = command ]; then
        say "Give a command that prints the password on its first line. It runs with /bin/sh; @SERVICE@ and @ACCOUNT@ are replaced by the entry's names."
        ask STORE_COMMAND "Command" "$(conf_get "$CV_CONF" store_command)"
        [ -n "$STORE_COMMAND" ] || return 1
        conf_set "$CV_CONF" store_command "$STORE_COMMAND"
    fi
    conf_set "$CV_CONF" store "$STORE"
    return 0
}

# A gpg key for setup's encrypted passwords (pass, gpg-file): sets GPG_UID.
gpg_key() {
    local uid
    uid=$(conf_get "$CV_CONF" gpg_uid)
    if [ -n "$uid" ] && gpg --list-secret-keys "$uid" >/dev/null 2>&1; then GPG_UID="$uid"; return 0; fi
    if gpg --list-secret-keys --with-colons 2>/dev/null | grep -q '^sec'; then
        say "Your gpg keys:"
        gpg --list-secret-keys --keyid-format short 2>/dev/null | sed -n 's/^uid *\[[^]]*\] */      /p'
        ask uid "Encrypt with which key (a name or e-mail above), or 'new'" new
    else
        uid=new
    fi
    if [ "$uid" = new ]; then
        uid="$(id -un) cubevis passwords"
        say "Creating a gpg key '$uid'. gpg asks for a passphrase that protects the stored passwords; gpg-agent remembers it for a while after you type it."
        changes_list "creates a gpg key in ~/.gnupg"
        ask_yn "Go ahead?" y || return 1
        run_tty gpg --quick-generate-key "$uid" default default never || return 1
        record_change "created gpg key" "$uid"
    fi
    conf_set "$CV_CONF" gpg_uid "$uid"
    GPG_UID="$uid"
}

pass_init() {
    gpg_key || return 1
    changes_list "creates the password store ${PASSWORD_STORE_DIR:-~/.password-store} (pass init)"
    ask_yn "Go ahead?" y || return 1
    run pass init "$GPG_UID" || return 1
    record_change "initialised pass" "${PASSWORD_STORE_DIR:-$HOME/.password-store}"
}

# Where an entry lives, for messages.
store_where() {   # store_where SERVICE ACCOUNT
    STORE=$(conf_get "$CV_CONF" store)
    case $STORE in
        keychain) echo "Keychain item '$1', account '$2'" ;;
        secret-tool) echo "keyring item service=$1 account=$2" ;;
        pass) echo "pass entry $1/$2" ;;
        gpg-file) echo "$(tilde "$CV_HOME")/secrets/$1-$2.gpg" ;;
        command) echo "your command ($1, $2)" ;;
    esac
}

# Is there an entry? (Does not show the password.)
store_has() {   # store_has SERVICE ACCOUNT
    STORE=$(conf_get "$CV_CONF" store)
    case $STORE in
        keychain) security find-generic-password -a "$2" -s "$1" >/dev/null 2>&1 ;;
        secret-tool) [ -n "$(secret-tool lookup service "$1" account "$2" 2>/dev/null </dev/null)" ] ;;
        pass) [ -f "${PASSWORD_STORE_DIR:-$HOME/.password-store}/$1/$2.gpg" ] ;;
        gpg-file) [ -f "$CV_HOME/secrets/$1-$2.gpg" ] ;;
        command) return 0 ;;
        *) return 1 ;;
    esac
}

# Ask for the password and store it.
store_put() {   # store_put SERVICE ACCOUNT HOST
    local pw pw2
    STORE=$(conf_get "$CV_CONF" store)
    [ "$STORE" = gpg-file ] && { gpg_key || return 1; }
    say "Type your password for $2 on $3 when asked. It goes to: $(store_where "$1" "$2")."
    case $STORE in
        keychain)
            run_tty security add-generic-password -U -a "$2" -s "$1" -l "ssh password ($1)" -w || return 1 ;;
        secret-tool)
            run_tty secret-tool store --label="ssh password ($1, $2)" service "$1" account "$2" || return 1 ;;
        pass)
            run_tty pass insert -f "$1/$2" || return 1 ;;
        gpg-file)
            read -r -s -p "  Password: " pw; echo
            read -r -s -p "  Again: " pw2; echo
            [ "$pw" = "$pw2" ] || { warn "they differ"; return 1; }
            mkdir -p "$CV_HOME/secrets" && chmod 700 "$CV_HOME/secrets"
            printf '%s\n' "$pw" | gpg -q --batch --yes -e -r "$GPG_UID" -o "$CV_HOME/secrets/$1-$2.gpg" || return 1
            pw=""; pw2="" ;;
        command)
            say "With your own command, store the password your usual way; setup cannot do it for you."
            pause "Press Enter when it is stored."
            return 0 ;;
    esac
    record_change "stored a password" "$(store_where "$1" "$2")"
}

# The wrapper's rule keys for an entry (bin/ssh, "Keys").
store_rule_keys() {   # store_rule_keys SERVICE ACCOUNT
    local cmd
    STORE=$(conf_get "$CV_CONF" store)
    case $STORE in
        keychain) echo "service=$1 account=$2 backend=security" ;;
        secret-tool) echo "service=$1 account=$2 backend=secret-tool" ;;
        pass) echo "service=$1 account=$2 backend=pass pass_name=$1/$2" ;;
        gpg-file) echo "service=$1 account=$2 backend=command command=gpg -q -d $CV_HOME/secrets/$1-$2.gpg" ;;
        command)
            cmd=$(conf_get "$CV_CONF" store_command | sed -e "s/@SERVICE@/$1/g" -e "s/@ACCOUNT@/$2/g")
            echo "service=$1 account=$2 backend=command command=$cmd" ;;
    esac
}

# gpg-based stores: have gpg-agent hold the key's passphrase, so the
# wrapper (which runs without a terminal) can decrypt. gpg asks here, in
# the terminal, if it needs to; on a desktop it may ask in a window later.
store_unlock() {   # store_unlock SERVICE ACCOUNT
    STORE=$(conf_get "$CV_CONF" store)
    case $STORE in
        pass) pass show "$1/$2" >/dev/null || return 1 ;;
        gpg-file) gpg -q -d "$CV_HOME/secrets/$1-$2.gpg" >/dev/null || return 1 ;;
    esac
    return 0
}
