"""Configure-style interaction for ``python -m cubevis.setup``: messages,
questions, the logs, and the step runner.

The same conventions as the bash ``setup/cubevis-setup`` (``lib/common.sh``):
each step *checks* (asks nothing, changes nothing), and only when the check
fails *explains, lists its changes, asks, makes them*, and checks again;
a failure offers retry / skip / quit.  Every change goes to
``changes.log``, every command (with its output) to ``setup.log``, both in
this computer's directory (``hosts/<id>``: with a shared home, each
computer keeps its own).  Enter takes the answer in [brackets].

``Answers`` pre-fills questions from command-line flags; ``--yes`` takes
every default (for tests and scripts).
"""
from __future__ import annotations

import datetime as _dt
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import config_dir, host_dir

WIDTH = 76


def _tty_colours() -> dict:
    if sys.stdout.isatty() and os.environ.get("NO_COLOR") is None:
        return {"b": "\033[1m", "dim": "\033[2m", "r": "\033[31m", "g": "\033[32m",
                "y": "\033[33m", "0": "\033[0m"}
    return {k: "" for k in ("b", "dim", "r", "g", "y", "0")}


C = _tty_colours()


class Quit(Exception):
    """The user chose [q]uit (or input ended)."""


def tilde(p) -> str:
    p = str(p)
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p == home or p.startswith(home + os.sep) else p


def quote_cmd(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(c)) for c in cmd)


class UI:
    """Messages, questions, logs.  One per run."""

    def __init__(self, *, yes: bool = False, answers: Optional[dict] = None,
                 out=None, inp=None):
        self.yes = yes
        self.answers = dict(answers or {})   # question key -> pre-filled answer
        self.out = out or sys.stdout
        self.inp = inp or sys.stdin
        self._live = False                   # a line written in place is showing
        self.changes: list = []              # made in this run
        self.failed = 0
        self.home = host_dir()
        self.home.mkdir(parents=True, exist_ok=True)
        for d in (config_dir(), self.home):
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass
        self.log_path = self.home / "setup.log"
        self.changes_path = self.home / "changes.log"

    # -- messages -----------------------------------------------------------
    def _p(self, s: str = "") -> None:
        self.live_clear()
        print(s, file=self.out, flush=True)

    def live(self, text: str) -> bool:
        """Show *text* as one line rewritten in place (a terminal only; False
        elsewhere, where nothing is written).  The next message replaces it."""
        if not getattr(self.out, "isatty", lambda: False)():
            return False
        width = max(20, shutil.get_terminal_size((80, 24)).columns - 1)
        self.out.write("\r\x1b[2K" + text[:width])
        self.out.flush()
        self._live = True
        return True

    def live_clear(self) -> None:
        if self._live:
            self.out.write("\r\x1b[2K")
            self.out.flush()
            self._live = False

    def say(self, text: str) -> None:
        for para in str(text).split("\n"):
            self._p(textwrap.fill(para, WIDTH, initial_indent="  ",
                                  subsequent_indent="  ") if para else "")

    def heading(self, text: str) -> None:
        self._p()
        self._p(f"{C['b']}{text}{C['0']}")

    def _hang(self, lead: str, text: str) -> None:
        lines = textwrap.wrap(str(text), WIDTH - 6) or [""]
        self._p(lead + lines[0])
        for l in lines[1:]:
            self._p(" " * 8 + l)

    def ok(self, text: str) -> None:
        self._hang(f"  {C['g']}ok{C['0']}    ", text)

    def note(self, text: str) -> None:
        self._hang(f"  {C['y']}note{C['0']}  ", text)

    def fail(self, text: str) -> None:
        self._hang(f"  {C['r']}FAIL{C['0']}  ", text)

    def changes_list(self, *items: str) -> None:
        self._p(f"  {C['b']}Changes:{C['0']}")
        for c in items:
            if c:
                lines = textwrap.wrap(c, WIDTH - 6)
                self._p("    - " + lines[0])
                for l in lines[1:]:
                    self._p("      " + l)

    # -- questions ----------------------------------------------------------
    def _read(self, prompt: str, key: Optional[str]) -> str:
        if key and key in self.answers:
            v = str(self.answers[key])
            self._p(f"{prompt}{v}")
            return v
        if self.yes:
            self._p(prompt)
            return ""
        self.out.write(prompt)
        self.out.flush()
        line = self.inp.readline()
        if not line:
            raise Quit()
        return line.rstrip("\n")

    def ask(self, question: str, default: str = "", key: Optional[str] = None) -> str:
        d = f" [{default}]" if default else ""
        v = self._read(f"  {question}{d}: ", key).strip()
        return v or default

    def ask_yn(self, question: str, default: bool = True, key: Optional[str] = None) -> bool:
        p = "[Y/n]" if default else "[y/N]"
        while True:
            a = self._read(f"  {question} {p} ", key).strip().lower()
            if not a:
                return default
            if a in ("y", "yes"):
                return True
            if a in ("n", "no"):
                return False
            if key:
                key = None          # a bad pre-filled answer: ask after all

    def ask_choice(self, question: str, default: str, choices: Sequence[str],
                   key: Optional[str] = None) -> str:
        while True:
            a = self._read(f"  {question} [{default}] ", key).strip() or default
            if a in choices:
                return a
            self._p(f"  (one of: {' '.join(choices)})")
            key = None

    def secret(self, question: str) -> str:
        """A password typed without echo (never logged, never pre-filled)."""
        import getpass
        if self.inp is not sys.stdin:
            return self._read(f"  {question}: ", None)
        return getpass.getpass(f"  {question}: ")

    # -- logs and commands --------------------------------------------------
    def log(self, text: str) -> None:
        try:
            with open(self.log_path, "a") as f:
                f.write(f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
        except OSError:
            pass

    def record_change(self, what: str, where: str) -> None:
        line = f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S}  {what}: {where}"
        try:
            with open(self.changes_path, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
        self.changes.append(f"{what}: {where}")
        self.log(f"change: {what}: {where}")

    def run(self, cmd: Sequence[str], *, show: bool = True, tty: bool = False,
            timeout: Optional[float] = None, input: Optional[str] = None,
            env: Optional[dict] = None) -> subprocess.CompletedProcess:
        """Run *cmd*; its output is logged (and shown when *show*).  With
        *tty* the command has the terminal to itself (it asks something)."""
        if show:
            self._p(f"  {C['dim']}$ {quote_cmd(cmd)}{C['0']}")
        self.log(f"run: {quote_cmd(cmd)}")
        try:
            if tty:
                r = subprocess.run(list(map(str, cmd)), timeout=timeout, env=env)
                r.stdout = r.stderr = ""
            else:
                r = subprocess.run(list(map(str, cmd)), capture_output=True, text=True,
                                   timeout=timeout, input=input,
                                   stdin=None if input is not None else subprocess.DEVNULL,
                                   env=env)
        except subprocess.TimeoutExpired as exc:
            self.log(f"  -> timed out after {timeout} s")
            r = subprocess.CompletedProcess(cmd, 124, exc.stdout or "",
                                            (exc.stderr or "") + f"\ntimed out after {timeout} s")
            if isinstance(r.stdout, bytes):
                r.stdout = r.stdout.decode(errors="replace")
            if isinstance(r.stderr, bytes):
                r.stderr = r.stderr.decode(errors="replace")
        except FileNotFoundError as exc:
            r = subprocess.CompletedProcess(cmd, 127, "", str(exc))
        out = (r.stdout or "") + (r.stderr or "")
        if out:
            try:
                with open(self.log_path, "a") as f:
                    f.write(out if out.endswith("\n") else out + "\n")
            except OSError:
                pass
            if show and not tty:
                for l in out.rstrip("\n").split("\n"):
                    self._p("    " + l)
        self.log(f"  -> exit {r.returncode}")
        return r

    def backup(self, path: Path) -> Optional[Path]:
        """Copy a file we are about to replace to ``backups/``."""
        path = Path(path)
        if not path.exists():
            return None
        d = self.home / "backups"
        d.mkdir(parents=True, exist_ok=True)
        b = d / f"{path.name}.{_dt.datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(path, b)
        return b

    def show_changes(self) -> None:
        if self.changes:
            self.heading(f"Changed in this run (all runs: {tilde(self.changes_path)})")
            for c in self.changes:
                self._p("    " + c)


class Step:
    """A step: ``title``, ``check() -> (done, message)``, ``do() -> bool``.
    ``look_only`` steps ask and change nothing; ``--redo`` does not repeat
    them."""
    look_only = False

    def title(self) -> str:
        raise NotImplementedError

    def check(self) -> tuple:
        raise NotImplementedError

    def do(self) -> bool:
        raise NotImplementedError


def run_step(ui: UI, step: Step, *, check_only: bool = False, redo: bool = False,
             number: Optional[int] = None) -> bool:
    """Check, and do only when the check fails (or *redo*); retry / skip /
    quit on failure.  Returns whether the step ends done."""
    if step.look_only:
        redo = False
    title = step.title()
    while True:
        if not redo or check_only:
            done, msg = step.check()
            if done:
                ui.ok(title + (f" -- {msg}" if msg else ""))
                return True
            if check_only:
                ui.fail(title + (f" -- {msg}" if msg else ""))
                ui.failed += 1
                return False
        ui.heading((f"{number}. " if number else "") + title)
        if step.do():
            done, msg = step.check()
            if done:
                ui.ok(title + (f" -- {msg}" if msg else ""))
                return True
        else:
            done, msg = step.check()
        ui.fail(title + (f" -- {msg}" if msg else ""))
        # Unattended (--yes), a retry would only fail again: skip.
        c = ui.ask_choice("[r]etry this step, [s]kip it, or [q]uit?",
                          "s" if ui.yes else "r", ("r", "s", "q"))
        if c == "s":
            ui.failed += 1
            return False
        if c == "q":
            raise Quit()
        redo = False


class FuncStep(Step):
    """A step from three functions."""

    def __init__(self, title: str, check: Callable, do: Callable, look_only: bool = False):
        self._title, self._check, self._do = title, check, do
        self.look_only = look_only

    def title(self) -> str:
        return self._title

    def check(self) -> tuple:
        return self._check()

    def do(self) -> bool:
        return self._do()
