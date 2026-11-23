"""The kernel-side fingerprint: what must be identical between the computer
the plots are on and a kernel's environment (remote session design 7c).

Not the cubevis version (every release would need a new environment on
the node, most of them for changes that never run there) and not a
hand-kept protocol number (it would have to cover the visplot backends
too, and a forgotten bump is a silent mismatch).  It is *computed*:

* ``ROOTS`` declares the entry points that run on the kernel side: the
  worker, and each registration module a worker loads by name.  That
  short list is the only thing kept by hand.
* ``closure`` follows ``import`` statements inside cubevis from them, in
  the source (nothing is imported, so a node needs none of the heavy
  dependencies to compute it): module-level and function-level imports,
  relative ones resolved, ``from X import Y`` counting ``X.Y`` when that
  is a module, and every parent package (its ``__init__`` runs on
  import) -- but not imports under ``if TYPE_CHECKING:``, which never run
  (the lazy package ``__init__``s keep their names there for editors).  An over-approximation, on purpose: a module that might run on
  the kernel side counts.
* ``normalize`` reduces each module to its tokens without comments, blank
  lines, docstrings and indentation width, so editing those does not
  change it; the tokenizer is the same from Python 3.12 (cubevis' minimum)
  on, unlike ``ast.dump``.
* ``GENERATED`` modules are left out: files the build writes into the
  package (``cubevis/__version__.py``, from ``pyproject.toml``'s
  ``write_to``), which hold only the version.  A wheel has one and a
  checkout does not, so with it every release differed from its own source
  and from every other release (found 2026-11-01: ``k1``).
* ``SENT``: this module itself, which is always run from the copy that is
  sent (below), is left out too.
* ``fingerprint`` hashes the modules, in name order, with ``ALGORITHM``.

**Computed with one copy of this code** (``source()``): setup sends this
module's text to the node and runs it there over the environment's files
(``envcheck``, the launcher test), rather than that environment's own copy
of it -- so a change to the rules here (like ``GENERATED``) applies to
environments made before it, and both sides always hash the same way.

The test ``tests/manual/setup/test_fingerprint.py`` imports the roots in a
fresh interpreter and fails if a cubevis module was imported that the
closure does not hold -- so the declared list cannot drift silently.

``compatible`` compares two fingerprints; ``CUBEVIS_ALLOW_MISMATCH=1``
(or a session's ``allow_mismatch=True``) lets a mismatch through, with a
warning each time.  Only the local side compares: the node just reports.

Standard library only; importing this module imports nothing else of
cubevis.
"""
from __future__ import annotations

import ast
import hashlib
import io
import os
import tokenize
from typing import Iterable, Optional

ALGORITHM = "k2"            # k1 (2026-10-23) counted cubevis/__version__.py
# Written by the build into the package; not code (see the module docstring).
GENERATED = frozenset({"cubevis.__version__"})
# This module: it is always run from the copy setup (or the laptop's
# connection) sends, never from the environment's own file, so that file
# does not decide anything.  ``RemoteObjectConnection.kernel_fingerprint``
# imports it, and the closure would otherwise count it (2026-11-09, step 5c).
SENT = frozenset({"cubevis._fingerprint"})
ROOTS = (
    "cubevis.remote.worker_main",
    "cubevis.toolbox.visplot.remote_registrations",
    "cubevis.imaging.remote_registrations",          # interactive clean (2026-11-07)
)
TOP = "cubevis"


def package_root() -> str:
    """The directory holding the ``cubevis`` package (this file's parent's parent)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _path_of(root: str, mod: str) -> Optional[str]:
    p = os.path.join(root, *mod.split("."))
    init = os.path.join(p, "__init__.py")
    if os.path.isfile(init):
        return init
    if os.path.isfile(p + ".py"):
        return p + ".py"
    return None


def _type_checking(test) -> bool:
    return ((isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or
            (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"))


def _runtime_nodes(tree):
    """Every node, except the bodies of ``if TYPE_CHECKING:`` (their
    ``else`` branches run, so they are kept)."""
    stack = [tree]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.If) and _type_checking(node.test):
            stack.extend(node.orelse)
            continue
        stack.extend(ast.iter_child_nodes(node))


def _imports(root: str, mod: str) -> set:
    path = _path_of(root, mod)
    with open(path, "rb") as f:
        tree = ast.parse(f.read(), filename=path)
    pkg = mod if path.endswith("__init__.py") else mod.rpartition(".")[0]
    out = set()
    for node in _runtime_nodes(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = pkg.split(".")
                parts = parts[:len(parts) - (node.level - 1)] if node.level > 1 else parts
                base = ".".join(parts + ([base] if base else []))
            out.add(base)
            out.update(f"{base}.{a.name}" for a in node.names if a.name != "*")
    return {m for m in out if m.split(".")[0] == TOP}


def closure(roots: Iterable[str] = ROOTS, root: Optional[str] = None) -> list:
    """The cubevis modules the kernel side may run, sorted (see the module
    docstring).  A root that is not there raises ``FileNotFoundError``."""
    root = root or package_root()
    seen, todo = set(), []
    for r in roots:
        if _path_of(root, r) is None:
            raise FileNotFoundError(f"kernel-side root {r} is not in {root}")
        todo.append(r)
    while todo:
        mod = todo.pop()
        parts = mod.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[:i])
            if parent not in seen and _path_of(root, parent):
                todo.append(parent)
        if mod in seen or mod in GENERATED or mod in SENT or _path_of(root, mod) is None:
            continue
        seen.add(mod)
        todo.extend(_imports(root, mod) - seen)
    return sorted(seen)


def normalize(source: str) -> str:
    """The module's tokens, without what does not change its behaviour:
    comments, blank lines, line continuations, indentation width, and
    statements that are only a string (docstrings)."""
    keep = []
    line = []
    toks = tokenize.generate_tokens(io.StringIO(source).readline)
    skip = {tokenize.COMMENT, tokenize.NL, tokenize.ENCODING, tokenize.ENDMARKER}
    for tok in toks:
        if tok.type in skip:
            continue
        if tok.type in (tokenize.INDENT, tokenize.DEDENT):
            keep.append(tokenize.tok_name[tok.type])
            continue
        if tok.type == tokenize.NEWLINE:
            if line and not all(t[0] == tokenize.STRING for t in line):
                keep.extend(f"{tokenize.tok_name[t[0]]}:{t[1]}" for t in line)
                keep.append("NEWLINE")
            line = []
            continue
        line.append((tok.type, tok.string))
    if line and not all(t[0] == tokenize.STRING for t in line):
        keep.extend(f"{tokenize.tok_name[t[0]]}:{t[1]}" for t in line)
    return "\n".join(keep)


def fingerprint(roots: Iterable[str] = ROOTS, root: Optional[str] = None) -> str:
    """``k2-<20 hex digits>`` for the kernel-side code installed at *root*
    (the directory holding the ``cubevis`` package; default: this one's).

    A root that is not there counts as missing (a marker in the hash), so a
    cubevis older than one of the roots -- before interactive clean's,
    2026-11-07 -- has a fingerprint that differs, and is reported as kernel-side
    code that differs, rather than raising there (which reached the user as "no
    kernel-side fingerprint").  With every root present the value is what it
    was."""
    root = root or package_root()
    roots = list(roots)
    present = [r for r in roots if _path_of(root, r) is not None]
    h = hashlib.sha256(ALGORITHM.encode())
    for mod in closure(present, root):
        with open(_path_of(root, mod), encoding="utf-8") as f:
            text = normalize(f.read())
        h.update(b"\0" + mod.encode() + b"\0" + text.encode())
    for r in roots:
        if r not in present:
            h.update(b"\0missing root\0" + r.encode())
    return f"{ALGORITHM}-{h.hexdigest()[:20]}"


def source() -> str:
    """This module's text, to run elsewhere: ``exec`` it in a fresh
    namespace and call its ``fingerprint(root=...)`` with the directory
    holding that environment's ``cubevis`` (Python 3.7 and later)."""
    with open(os.path.abspath(__file__), encoding="utf-8") as f:
        return f.read()


def allow_mismatch(environ=None) -> bool:
    environ = os.environ if environ is None else environ
    return (environ.get("CUBEVIS_ALLOW_MISMATCH") or "").strip().lower() in ("1", "yes", "true", "on")


def compatible(local: Optional[str], remote: Optional[str], *, allow: Optional[bool] = None,
               environ=None) -> tuple:
    """``(usable, message)`` for a kernel environment whose fingerprint is
    *remote*, from here (*local*).  A mismatch is usable only when allowed
    (*allow*, else ``CUBEVIS_ALLOW_MISMATCH``), and the message says so."""
    if local and remote and local == remote:
        return True, f"kernel-side code identical ({local})"
    if allow is None:
        allow = allow_mismatch(environ)
    if not remote:
        why = "it has no kernel-side fingerprint (a cubevis from before 2026-10-23)"
    elif local and remote.split("-")[0] != local.split("-")[0]:
        why = (f"its kernel-side fingerprint was computed another way (there {remote}, here "
               f"{local}): run setup for this host again")
    else:
        why = f"its kernel-side code differs (there {remote}, here {local})"
    if allow:
        return True, why + "; used anyway: CUBEVIS_ALLOW_MISMATCH is set"
    return False, why


# ---------------------------------------------------------------------- #
# Command line (2026-11-20, debug-0004)                                    #
# ---------------------------------------------------------------------- #
# Whether a change needs a new kernel-side fingerprint (and so a cubevis
# release on the kernel side) used to be judged from a hand-kept list of
# modules.  The closure above already says it: ask it.
#
#   python -m cubevis._fingerprint                  the fingerprint
#   python -m cubevis._fingerprint --list           ... and the closure
#   python -m cubevis._fingerprint --changed REF    compared with git REF
#
# Standard library only, and Python 3.7-compatible like the rest of this
# module (it is also sent to nodes and run there).  This module is in SENT,
# so editing it changes no fingerprint.

def _present_closure(root: str) -> list:
    """``closure`` from the roots *root* has (``fingerprint``'s rule: a
    cubevis older than a root is compared, not refused)."""
    return closure([r for r in ROOTS if _path_of(root, r) is not None], root)


def _git(root: str, *args: str) -> str:
    import subprocess
    return subprocess.run(("git", "-C", root) + args, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True).stdout


def _export(root: str, ref: str, dest: str) -> str:
    """The ``cubevis`` package at git *ref* written under *dest*; returns
    the directory that holds it (for ``fingerprint(root=...)``)."""
    import io as _io
    import subprocess
    import tarfile
    top = _git(root, "rev-parse", "--show-toplevel").strip()
    prefix = os.path.relpath(os.path.join(root, TOP), top).replace(os.sep, "/")
    data = subprocess.run(("git", "-C", top, "archive", "--format=tar", ref, prefix),
                          check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    with tarfile.open(fileobj=_io.BytesIO(data)) as tf:
        if hasattr(tarfile, "data_filter"):          # 3.12+ (and 3.8.17+ ...)
            tf.extractall(dest, filter="data")
        else:
            tf.extractall(dest)
    return os.path.dirname(os.path.join(dest, prefix))


def changed(ref: str, root: Optional[str] = None) -> dict:
    """How the kernel-side code here differs from git *ref*:
    ``{"before", "after", "modules"}`` -- the two fingerprints, and the
    closure modules (of either side) whose normalized text differs, i.e.
    whose change is more than comments, docstrings or blank lines."""
    import tempfile
    root = root or package_root()
    with tempfile.TemporaryDirectory() as tmp:
        old_root = _export(root, ref, tmp)
        before, after = fingerprint(root=old_root), fingerprint(root=root)
        mods = []
        for mod in sorted(set(_present_closure(root)) | set(_present_closure(old_root))):
            texts = []
            for r in (old_root, root):
                p = _path_of(r, mod)
                if p is None:
                    texts.append(None)
                else:
                    with open(p, encoding="utf-8") as f:
                        texts.append(normalize(f.read()))
            if texts[0] != texts[1]:
                mods.append(mod)
    return {"before": before, "after": after, "modules": mods}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="python -m cubevis._fingerprint",
        description="The kernel-side fingerprint of this cubevis (remote session design 7c).")
    ap.add_argument("--list", action="store_true", help="also list the modules it covers")
    ap.add_argument("--changed", metavar="REF",
                    help="compare with git REF (e.g. origin/main): say whether the "
                         "kernel side needs a new fingerprint, and which modules changed")
    ap.add_argument("--root", help="directory holding the cubevis package (default: this one's)")
    a = ap.parse_args(argv)
    root = a.root or package_root()
    if a.changed:
        r = changed(a.changed, root)
        if r["before"] == r["after"]:
            print(f"kernel side unchanged since {a.changed}: {r['after']}")
        else:
            print(f"kernel side CHANGED since {a.changed}: {r['before']} -> {r['after']}")
            print("a new kernel-side fingerprint (a cubevis release on the kernel side) is needed")
            for m in r["modules"]:
                print(f"  {m}")
        return 0
    print(fingerprint(root=root))
    if a.list:
        for m in _present_closure(root):
            print(f"  {m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
