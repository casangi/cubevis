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
        return (True, "macOS Keychain") if darwin and has("security") else (False, "macOS only")
    if store == "secret-tool":
        if not has("secret-tool"):
            return False, "secret-tool is not installed"
        r = subprocess.run(["secret-tool", "lookup", "service", "cubevis-setup-probe"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if not (r.stdout + r.stderr).strip():
            return True, "your desktop keyring is running"
        return False, ("no desktop keyring in this session (works only where you log "
                       "in to a desktop)")
    if store == "pass":
        if not has("pass"):
            return False, "pass is not installed"
        if (_pass_dir() / ".gpg-id").is_file():
            return True, "you already use pass"
        return (True, "pass is installed (setup will initialise it)") if has("gpg") \
            else (False, "pass needs gpg")
    if store == "gpg-file":
        return (True, "gpg is installed") if has("gpg") else (False, "gpg is not installed")
    if store == "command":
        return True, "any command that prints the password (a password manager's CLI)"
    return False, "unknown"


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


def choose(ui: UI, st: State) -> bool:
    """Choose the store (once).  False if none can be used."""
    s = st.local.get("store")
    if s and available(s)[0]:
        return True
    ui.say("The password is kept in a password store and read by the wrapper only "
           "when it logs in. Stores that work here:")
    usable = []
    for name in ALL:
        ok, why = available(name)
        if ok:
            ui.say(f"  {name:<12} {why}")
            usable.append(name)
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
        ui.say("Give a command that prints the password on its first line. It runs with "
               "/bin/sh; @SERVICE@ and @ACCOUNT@ are replaced by the entry's names.")
        cmd = ui.ask("Command", st.local.get("store_command") or "", key="store_command")
        if not cmd:
            return False
        st.local["store_command"] = cmd
    st.local["store"] = s
    ui.record_change("chose password store", s)
    return True


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
    ui.say(f"Type your password for {account} on {host} when asked. "
           f"It goes to: {where(st, service, account)}.")
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
        return False
    ui.record_change("stored a password", where(st, service, account))
    return True


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
