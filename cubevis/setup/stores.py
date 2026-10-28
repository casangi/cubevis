"""Password stores: where the ssh wrapper finds the password for a host that
asks for one.  Chosen once (``local.store`` in the state), when the first
such host is set up.  Ported from the bash setup's ``lib/store.sh``.

    keychain     macOS Keychain (the ``security`` command)
    secret-tool  the desktop keyring on Linux (GNOME Keyring, KWallet)
    pass         the ``pass`` password store (gpg-encrypted files)
    gpg-file     one gpg-encrypted file per password, kept by setup
    command      your own command that prints the password

Passwords never pass through the state or the logs: the store's own
command asks for them on the terminal (``security``, ``secret-tool``,
``pass``), or setup reads them without echo and hands them to gpg.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .sshgen import secrets_dir
from .state import State
from .ui import UI, tilde

ALL = ("keychain", "secret-tool", "pass", "gpg-file", "command")


def has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _pass_dir() -> Path:
    return Path(os.environ.get("PASSWORD_STORE_DIR") or Path.home() / ".password-store")


def available(store: str) -> tuple:
    """``(usable, why)`` for *store* on this computer."""
    darwin = platform.system() == "Darwin"
    if store == "keychain":
        if not (darwin and has("security")):
            return False, "macOS only"
        why = keychain_problem()
        if why:
            return False, why
        return True, "macOS Keychain: encrypted, unlocked by your login"
    if store == "secret-tool":
        if not has("secret-tool"):
            return False, "secret-tool is not installed"
        r = subprocess.run(["secret-tool", "lookup", "service", "cubevis-setup-probe"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if not (r.stdout + r.stderr).strip():
            return True, "your desktop keyring (encrypted) is running"
        return False, ("no desktop keyring in this session (works only where you log "
                       "in to a desktop)")
    if store == "pass":
        if not has("pass"):
            return False, "pass is not installed"
        if (_pass_dir() / ".gpg-id").is_file():
            return True, "you already use pass (gpg-encrypted)"
        return (True, "pass is installed (gpg-encrypted; setup will initialise it)") \
            if has("gpg") else (False, "pass needs gpg")
    if store == "gpg-file":
        return ((True, "one gpg-encrypted file per password, kept by setup")
                if has("gpg") else (False, "gpg is not installed"))
    if store == "command":
        return True, ("your password manager's command that prints it (op, bw, ...); "
                      "how safe that is, is up to it")
    return False, "unknown"


def keychain_problem() -> Optional[str]:
    """Why this account's default keychain cannot take a password, or None.
    An account whose first login did not complete (or whose home is not its
    own) has none: ``security`` then fails, and macOS shows "Keychain Not
    Found" -- so setup asks first instead (2026-10-28, a test account)."""
    try:
        r = subprocess.run(["security", "default-keychain", "-d", "user"], capture_output=True,
                           text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"security cannot be run ({exc})"
    path = r.stdout.strip().strip('"')
    if r.returncode != 0 or not path:
        return "this account has no default keychain"
    if not os.path.exists(path):
        return f"this account's default keychain is missing ({tilde(path)})"
    if not os.access(path, os.W_OK):
        return f"this account cannot write its default keychain ({tilde(path)})"
    return None


def default() -> str:
    """The store most likely already in use here."""
    if platform.system() == "Darwin":
        return "keychain"
    if has("pass") and (_pass_dir() / ".gpg-id").is_file():
        return "pass"
    for s in ("secret-tool", "pass", "gpg-file"):
        if available(s)[0]:
            return s
    return "command"


def where(st: State, service: str, account: str) -> str:
    """Where an entry lives, for messages."""
    s = st.local.get("store")
    return {"keychain": f"Keychain item '{service}', account '{account}'",
            "secret-tool": f"keyring item service={service} account={account}",
            "pass": f"pass entry {service}/{account}",
            "gpg-file": tilde(secrets_dir() / f"{service}-{account}.gpg"),
            "command": f"your command ({service}, {account})"}.get(s, "?")


def has_entry(st: State, service: str, account: str) -> bool:
    s = st.local.get("store")
    if s == "keychain":
        return subprocess.run(["security", "find-generic-password", "-a", account, "-s",
                               service], capture_output=True).returncode == 0
    if s == "secret-tool":
        r = subprocess.run(["secret-tool", "lookup", "service", service, "account", account],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return bool(r.stdout.strip())
    if s == "pass":
        return (_pass_dir() / service / f"{account}.gpg").is_file()
    if s == "gpg-file":
        return (secrets_dir() / f"{service}-{account}.gpg").is_file()
    return s == "command"


def choose(ui: UI, st: State, exclude=()) -> bool:
    """Choose the store (once).  False if none can be used.  *exclude*:
    stores that just failed (offered again only if nothing else works)."""
    s = st.local.get("store")
    if s and s not in exclude and available(s)[0]:
        return True
    ui.say("The password is never written to setup's files or logs. It is kept in an "
           "encrypted password store, from which the wrapper reads it only to log in. "
           "Stores that work here:")
    usable = []
    for name in ALL:
        ok, why = available(name)
        if ok and name in exclude:
            ok, why = False, "failed just now"
        if ok:
            ui.say(f"  {name:<12} {why}")
            usable.append(name)
        elif name == "keychain" and platform.system() == "Darwin":
            ui.say(f"  ({name:<10} not usable: {why}. Keychain Access can make one, or log "
                   "in to this account at the Mac once)")
    if not usable:
        ui.fail("no password store can be used here")
        return False
    if platform.system() != "Darwin":
        ui.say("On Linux: if you use pass already, keep using it. secret-tool suits a "
               "desktop you log in to; it cannot be used over ssh or without a desktop "
               "session. gpg-file works anywhere gpg does.")
    d = default() if default() in usable else usable[0]
    s = ui.ask_choice("Which store?", d, usable, key="store")
    if s == "pass" and not (_pass_dir() / ".gpg-id").is_file():
        if not _pass_init(ui, st):
            return False
    if s == "command":
        ui.say("Give your password manager's command that prints the password on its first "
               "line. It runs with /bin/sh; @SERVICE@ and @ACCOUNT@ are replaced by the "
               "entry's names.")
        while True:
            cmd = ui.ask("Command", st.local.get("store_command") or "", key="store_command")
            if not cmd:
                return False
            why = plain_text_command(cmd)
            if not why:
                break
            ui.note(why)
            if ui.yes or "store_command" in ui.answers:
                return False
        st.local["store_command"] = cmd
    st.local["store"] = s
    ui.record_change("chose password store", s)
    return True


# Commands that would hold the password in plain text -- in the command
# itself, which setup records in its own files, or in a file read as is.
_PLAIN = ("echo", "printf", "cat", "head", "tail", "less", "more", "print")


def plain_text_command(cmd: str) -> Optional[str]:
    """Why *cmd* is refused as a password store, or None.  The test suite
    (an ``echo`` of the rig's password) sets
    ``CUBEVIS_SETUP_ALLOW_PLAIN_COMMAND=1``."""
    if os.environ.get("CUBEVIS_SETUP_ALLOW_PLAIN_COMMAND") == "1":
        return None
    import shlex
    try:
        first = shlex.split(cmd)[0] if cmd.strip() else ""
    except ValueError:
        first = cmd.split()[0] if cmd.split() else ""
    if os.path.basename(first) in _PLAIN:
        return (f"'{first} ...' would keep the password in plain text (setup records the "
                "command in its own files): give a password manager's command, or choose "
                "an encrypted store")
    return None


def _gpg_key(ui: UI, st: State) -> Optional[str]:
    uid = st.local.get("gpg_uid")
    if uid and subprocess.run(["gpg", "--list-secret-keys", uid],
                              capture_output=True).returncode == 0:
        return uid
    r = subprocess.run(["gpg", "--list-secret-keys", "--with-colons"],
                       capture_output=True, text=True)
    uid = "new"
    if any(l.startswith("sec") for l in r.stdout.splitlines()):
        uids = [l.split(":")[9] for l in r.stdout.splitlines() if l.startswith("uid")]
        ui.say("Your gpg keys: " + "; ".join(uids))
        uid = ui.ask("Encrypt with which key (a name or e-mail above), or 'new'", "new",
                     key="gpg_uid")
    if uid == "new":
        uid = f"{os.environ.get('USER', 'me')} cubevis passwords"
        ui.say(f"Creating a gpg key '{uid}'. gpg asks for a passphrase that protects the "
               "stored passwords; gpg-agent remembers it for a while after you type it.")
        ui.changes_list("creates a gpg key in ~/.gnupg")
        if not ui.ask_yn("Go ahead?", True):
            return None
        if ui.run(["gpg", "--quick-generate-key", uid, "default", "default", "never"],
                  tty=True).returncode != 0:
            return None
        ui.record_change("created gpg key", uid)
    st.local["gpg_uid"] = uid
    return uid


def _pass_init(ui: UI, st: State) -> bool:
    uid = _gpg_key(ui, st)
    if not uid:
        return False
    ui.changes_list(f"creates the password store {tilde(_pass_dir())} (pass init)")
    if not ui.ask_yn("Go ahead?", True):
        return False
    if ui.run(["pass", "init", uid]).returncode != 0:
        return False
    ui.record_change("initialised pass", str(_pass_dir()))
    return True


def put(ui: UI, st: State, service: str, account: str, host: str) -> bool:
    """Ask for the password of *account* on *host* and store it."""
    s = st.local.get("store")
    twice = {"keychain": " (twice)", "secret-tool": "", "pass": " (twice)", "gpg-file": " (twice)"}
    if s != "command":
        ui.say(f"Type your password for {account} on {host} when asked{twice.get(s, '')}; "
               "nothing is shown as you type. It goes to "
               f"{where(st, service, account)}, encrypted"
               + ("" if s == "gpg-file" else ", without passing through setup") + ".")
    if s == "keychain":
        r = ui.run(["security", "add-generic-password", "-U", "-a", account, "-s", service,
                    "-l", f"ssh password ({service})", "-w"], tty=True)
    elif s == "secret-tool":
        r = ui.run(["secret-tool", "store", f"--label=ssh password ({service}, {account})",
                    "service", service, "account", account], tty=True)
    elif s == "pass":
        r = ui.run(["pass", "insert", "-f", f"{service}/{account}"], tty=True)
    elif s == "gpg-file":
        uid = _gpg_key(ui, st)
        if not uid:
            return False
        pw = ui.secret("Password")
        if pw != ui.secret("Again"):
            ui.note("they differ")
            return False
        d = secrets_dir()
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        out = d / f"{service}-{account}.gpg"
        r = subprocess.run(["gpg", "-q", "--batch", "--yes", "-e", "-r", uid, "-o", str(out)],
                           input=pw + "\n", text=True, capture_output=True)
        pw = ""
        ui.log(f"run: gpg -e -r {uid} -o {out} -> exit {r.returncode}")
    else:   # command
        ui.say("With your own command, store the password your usual way; setup cannot "
               "do it for you.")
        ui.ask("Press Enter when it is stored", "")
        return True
    if r.returncode != 0:
        ui.fail(f"the password was not stored in {where(st, service, account)} (the "
                "message above is the store's)"
                + (f": {keychain_problem()}" if s == "keychain" and keychain_problem() else ""))
        return False
    ui.record_change("stored a password", where(st, service, account))
    return True


def put_or_switch(ui: UI, st: State, service: str, account: str, host: str) -> bool:
    """``put``; when the store fails, offer another one (the Keychain of an
    account that has none, say) instead of the same failure again."""
    if put(ui, st, service, account, host):
        return True
    failed = st.local.get("store")
    others = [n for n in ALL if n != failed and available(n)[0]]
    if not others:
        return False
    if not ui.ask_yn(f"Use another password store ({', '.join(others)})?", True,
                     key="switch_store"):
        return False
    st.local.pop("store", None)
    if not choose(ui, st, exclude=(failed,)):
        return False
    return put(ui, st, service, account, host)


def unlock(ui: UI, st: State, service: str, account: str) -> bool:
    """gpg-based stores: have gpg-agent hold the passphrase now (gpg asks on
    the terminal if it needs to), so the wrapper -- which has no terminal --
    can decrypt later."""
    s = st.local.get("store")
    if s == "pass":
        cmd = ["pass", "show", f"{service}/{account}"]
    elif s == "gpg-file":
        cmd = ["gpg", "-q", "-d", str(secrets_dir() / f"{service}-{account}.gpg")]
    else:
        return True
    return subprocess.run(cmd, stdout=subprocess.DEVNULL).returncode == 0
