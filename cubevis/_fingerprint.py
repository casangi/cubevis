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
* ``fingerprint`` hashes the modules, in name order, with ``ALGORITHM``.

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

ALGORITHM = "k1"
ROOTS = (
    "cubevis.remote.worker_main",
    "cubevis.toolbox.visplot.remote_registrations",
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
        if mod in seen or _path_of(root, mod) is None:
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
    """``k1-<20 hex digits>`` for the kernel-side code installed at *root*
    (default: this cubevis)."""
    root = root or package_root()
    h = hashlib.sha256(ALGORITHM.encode())
    for mod in closure(roots, root):
        with open(_path_of(root, mod), encoding="utf-8") as f:
            text = normalize(f.read())
        h.update(b"\0" + mod.encode() + b"\0" + text.encode())
    return f"{ALGORITHM}-{h.hexdigest()[:20]}"


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
    else:
        why = f"its kernel-side code differs (there {remote}, here {local})"
    if allow:
        return True, why + "; used anyway: CUBEVIS_ALLOW_MISMATCH is set"
    return False, why
