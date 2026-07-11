#!/usr/bin/env python3
"""sync_layers.py
=================
Build-time tool that scrapes Python source files and expands Jinja2 templates,
keeping parallel class layers (thin wrappers, script layers, CLI adapters, etc.)
in sync with a canonical source class.

Usage
-----
    python tools/sync_layers.py layers.yaml [--check] [--verbose]

``--check``  Diff-only mode: exit non-zero if any output file would change
             (suitable for CI).

Generated files are made read-only (``chmod 444``) after writing so that
accidental edits in an editor are immediately obvious.  The generator
restores write permission automatically before overwriting on the next run.
Note that ``git`` does not preserve file permissions across clones, so this
is a local safeguard only.
``--verbose`` Print variable bindings extracted from each source file.

YAML format
-----------
::

    sources:
      - file: cubevis/toolbox/visplot/visibility_plotter.py
        class: VisibilityPlotter
        methods:
          - __init__
          - __call__
          - show

    templates:
      - input:  templates/script_layer.py.j2
        output: cubevis/toolbox/visplot/script_layer.py

Template variables
------------------
For a source file declaring class ``Foo`` with methods ``bar`` and ``baz``:

    ``{{ docstring }}``            Class-level docstring (raw triple-quoted string).
    ``{{ methods.bar.docstring }}``  Method docstring.
    ``{{ methods.bar.arguments }}``  Full argument list as a source string,
                                     e.g. ``self, *, ms=None, backend='auto'``.
    ``{{ methods.bar.returntype }}`` Return annotation string, e.g. ``-> None``,
                                     or ``""`` if absent.
    ``{{ methods.bar.async }}``      ``"async "`` if the method is async, else ``""``.
    ``{{ methods.bar.signature }}``  Convenience: ``async def bar(<arguments>)<returntype>``

Each element of ``methods.bar.args`` also exposes:

    ``arg.short_desc``   First sentence of per-parameter docstring prose, or the
                         value from ``short_desc_overrides`` in the YAML if present.
    ``arg.is_hidden``    ``True`` when the parameter is listed in ``hidden_args``
                         in the YAML source entry.  Templates use this to suppress
                         parameters from ``inp``-style display.
    ``arg.is_layer``     ``True`` when the parameter is listed in ``layer_args``
                         in the YAML source entry.  The layer supplies the value
                         internally; templates exclude these from user-facing
                         signatures, global frame lookup, inp display, defaults,
                         and tget/tput, while still forwarding them to the
                         canonical interface.

Indentation
-----------
The indentation of a ``{{ ... }}`` tag in the template is automatically applied
to every line of the substituted value.  So if the tag is indented 8 spaces,
every line of the substituted block is indented 8 spaces (the first line
inherits the indent from the template itself; subsequent lines get it added).

This means you can write::

        def __init__(
            {{ methods.__init__.arguments }},
        ) {{ methods.__init__.returntype }}:
            {{ methods.__init__.docstring }}

and the docstring will be correctly indented at 12 spaces even though it was
stored at 8 in the source.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import re
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined, BaseLoader


# ---------------------------------------------------------------------------
# AST scraping
# ---------------------------------------------------------------------------

@dataclass
class ArgInfo:
    """Structured representation of a single parameter."""
    name:       str       # bare name, e.g. "ms"
    type:       str       # annotation string, e.g. "Optional[str]", or ""
    default:    str       # default value string, e.g. "None", or ""
    is_self:    bool      # True for the leading self/cls parameter
    is_star:    bool      # True for a bare * (keyword-only separator)
    is_args:    bool      # True for *args
    is_kwargs:  bool      # True for **kwargs
    short_desc:         str  = "" # first sentence of per-param docstring prose
    is_hidden:          bool = False  # True when listed in hidden_args in YAML
    validator_override: str  = "" # overrides type-annotation validator; e.g. "(tuple, list)"
    is_layer:           bool = False  # True when listed in layer_args in YAML


@dataclass
class MethodInfo:
    name:       str
    docstring:  str       # raw, dedented, NOT re-wrapped
    arguments:  str       # flat string e.g. "self, *, ms=None, backend='auto'"
    args:       list      # list[ArgInfo] -- structured form of the same
    returntype: str       # e.g. " -> None" or ""
    is_async:   bool


@dataclass
class ClassInfo:
    name:      str
    docstring: str
    methods:   dict[str, MethodInfo] = field(default_factory=dict)


def _unparse_annotation(node) -> str:
    """Turn an annotation AST node back into source text."""
    return ast.unparse(node) if node else ""


def _unparse_default(node) -> str:
    return ast.unparse(node) if node else ""


def _unparse_arguments(args: ast.arguments) -> str:
    """Reconstruct the full argument list string from ast.arguments.

    Handles positional, *args, keyword-only, **kwargs, and defaults.
    Produces the same style as the original source (annotations included).
    """
    parts: list[str] = []
    n_args    = len(args.args)
    n_defaults = len(args.defaults)
    # defaults align to the END of args.args
    defaults_offset = n_args - n_defaults

    def _fmt_arg(arg, default=None) -> str:
        s = arg.arg
        if arg.annotation:
            s += f": {_unparse_annotation(arg.annotation)}"
        if default is not None:
            s += f" = {_unparse_default(default)}"
        return s

    # positional-or-keyword
    for i, arg in enumerate(args.args):
        default_idx = i - defaults_offset
        default = args.defaults[default_idx] if default_idx >= 0 else None
        parts.append(_fmt_arg(arg, default))

    # *args or bare *
    if args.vararg:
        parts.append(f"*{_fmt_arg(args.vararg)}")
    elif args.kwonlyargs:
        parts.append("*")

    # keyword-only
    for i, arg in enumerate(args.kwonlyargs):
        default = args.kw_defaults[i]  # may be None
        parts.append(_fmt_arg(arg, default))

    # **kwargs
    if args.kwarg:
        parts.append(f"**{_fmt_arg(args.kwarg)}")

    return ", ".join(parts)



def _structured_arguments(args: ast.arguments) -> list:
    """Return a list of ArgInfo for every parameter in *args*.

    The list mirrors the flat argument string produced by
    ``_unparse_arguments`` but gives templates a structured object to
    loop over and filter, e.g.::

        {% for arg in methods['__init__'].args if not arg.is_self and not arg.is_star %}
            {{ arg.name }} = {{ arg.name }},
        {% endfor %}
    """
    result: list = []
    n_args         = len(args.args)
    n_defaults     = len(args.defaults)
    defaults_offset = n_args - n_defaults

    # positional-or-keyword (includes self/cls)
    for i, arg in enumerate(args.args):
        default_idx = i - defaults_offset
        default_node = args.defaults[default_idx] if default_idx >= 0 else None
        result.append(ArgInfo(
            name      = arg.arg,
            type      = _unparse_annotation(arg.annotation) if arg.annotation else "",
            default   = _unparse_default(default_node) if default_node is not None else "",
            is_self   = (i == 0 and arg.arg in ("self", "cls")),
            is_star   = False,
            is_args   = False,
            is_kwargs = False,
        ))

    # *args or bare *
    if args.vararg:
        result.append(ArgInfo(
            name      = args.vararg.arg,
            type      = _unparse_annotation(args.vararg.annotation) if args.vararg.annotation else "",
            default   = "",
            is_self   = False,
            is_star   = False,
            is_args   = True,
            is_kwargs = False,
        ))
    elif args.kwonlyargs:
        result.append(ArgInfo(
            name="", type="", default="",
            is_self=False, is_star=True, is_args=False, is_kwargs=False,
        ))

    # keyword-only args
    for i, arg in enumerate(args.kwonlyargs):
        default_node = args.kw_defaults[i]
        result.append(ArgInfo(
            name      = arg.arg,
            type      = _unparse_annotation(arg.annotation) if arg.annotation else "",
            default   = _unparse_default(default_node) if default_node is not None else "",
            is_self   = False,
            is_star   = False,
            is_args   = False,
            is_kwargs = False,
        ))

    # **kwargs
    if args.kwarg:
        result.append(ArgInfo(
            name      = args.kwarg.arg,
            type      = _unparse_annotation(args.kwarg.annotation) if args.kwarg.annotation else "",
            default   = "",
            is_self   = False,
            is_star   = False,
            is_args   = False,
            is_kwargs = True,
        ))

    return result


def _get_docstring(node) -> str:
    """Extract and dedent the docstring from a function or class node.

    ``ast.get_docstring(clean=True)`` removes the leading indentation that
    Python adds when the string literal is indented inside a class/function
    body, giving us a fully column-0 string.  We then re-wrap in triple
    quotes; the template's auto-indent machinery handles re-indentation at
    the call site.
    """
    raw = ast.get_docstring(node, clean=True)   # clean=True does the dedent
    if raw is None:
        return ""
    inner = raw.rstrip()
    if "\n" in inner:
        return f'"""{inner}\n"""'
    else:
        return f'"""{inner}"""'



def _parse_numpy_params(docstring: str) -> dict[str, str]:
    """Extract per-parameter short descriptions from a numpy-style docstring.

    Scans for the ``Parameters\n----------`` section and parses each entry of
    the form::

        name : type
            First sentence of description.  More prose that we ignore.

    Returns a dict mapping parameter name to its first sentence.  The first
    sentence is defined as the text up to the first ``.`` followed by
    whitespace or end-of-string, or the entire first line of prose if no
    period is found.

    This is used to populate ``ArgInfo.short_desc`` when no explicit override
    is provided in the YAML ``short_desc_overrides`` block.
    """
    result: dict[str, str] = {}
    if not docstring:
        return result

    # Strip wrapping triple-quotes if present (build_vars stores them that way)
    raw = docstring.strip()
    for q in ('"""', "'''"):
        if raw.startswith(q) and raw.endswith(q):
            raw = raw[3:-3]
            break

    lines = raw.splitlines()

    # Find the Parameters section header
    in_params = False
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped in ("Parameters", "Parameters:"):
            # Next non-empty line should be the dashes
            if i + 1 < len(lines) and re.match(r"^-{3,}", lines[i + 1].strip()):
                in_params = True
                i += 2
                break
        i += 1

    if not in_params:
        return result

    # Parse entries until we hit the next section (a line of dashes) or EOF
    while i < len(lines):
        line = lines[i]

        # A new section starts with a header followed by dashes
        if i + 1 < len(lines) and re.match(r"^-{3,}", lines[i + 1].strip()):
            break

        # Parameter name line: "name" or "name : type"
        # Must be at column 0 (no leading indent) and non-empty
        name_match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::.*)?$", line)
        if name_match and line == line.lstrip():
            param_name = name_match.group(1)
            i += 1
            # Collect indented prose lines
            prose_lines = []
            while i < len(lines):
                pl = lines[i]
                # Prose lines are indented; an un-indented non-empty line
                # starts the next parameter entry.
                if pl and not pl[0].isspace():
                    break
                prose_lines.append(pl.strip())
                i += 1
            # Join and extract first sentence
            prose = " ".join(p for p in prose_lines if p)
            # First sentence: up to first period+whitespace, or end
            m = re.search(r"\.(\s|$)", prose)
            if m:
                short = prose[: m.start() + 1].strip()
            else:
                # No period — take the whole first line of prose
                short = prose_lines[0].strip() if prose_lines else ""
            if param_name and short:
                result[param_name] = short
        else:
            i += 1

    return result


def scrape_class(source_path: Path, class_name: str,
                 method_names: list[str],
                 hidden_args: list[str] | None = None,
                 layer_args: dict[str, str] | list[str] | None = None,
                 short_desc_overrides: dict[str, str] | None = None,
                 validator_overrides: dict[str, str] | None = None,
                 ) -> ClassInfo:
    """Parse *source_path* and extract info for *class_name*.

    Parameters
    ----------
    source_path :
        Path to the Python source file.
    class_name :
        Name of the class to scrape.
    method_names :
        List of method names to extract from the class.
    hidden_args :
        Parameter names that should be marked ``is_hidden=True`` in the
        resulting ``ArgInfo`` dicts.  Templates use this to suppress
        parameters from ``inp``-style display without removing them from
        the signature.
    layer_args :
        Parameter names that should be marked ``is_layer=True`` in the
        resulting ``ArgInfo`` dicts.  These are arguments whose values are
        supplied by the layer itself (e.g. execution context) rather than
        by the user.  Templates exclude them from the user-facing signature,
        global frame lookup, ``inp`` display, ``_arg_default``, and
        ``tget``/``tput``, while still passing them through to the
        canonical interface call.
    short_desc_overrides :
        Mapping of parameter name → short description string.  When
        present for a parameter, overrides the first-sentence heuristic
        applied to the class docstring.
    """
    hidden_set    = set(hidden_args or [])
    # Normalise layer_args to dict if a list was passed
    if isinstance(layer_args, list):
        layer_args = {name: 'None' for name in (layer_args or [])}
    layer_args    = layer_args or {}
    layer_set     = set(layer_args.keys())
    overrides     = short_desc_overrides or {}
    val_overrides = validator_overrides or {}

    source = source_path.read_text(encoding="utf-8")
    tree   = ast.parse(source, filename=str(source_path))

    # Find the class
    cls_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            cls_node = node
            break
    if cls_node is None:
        raise ValueError(
            f"Class {class_name!r} not found in {source_path}"
        )

    class_doc = _get_docstring(cls_node)

    # Parse per-parameter short descriptions from the class docstring,
    # then apply any overrides from the YAML.
    scraped_descs = _parse_numpy_params(class_doc)
    short_descs   = {**scraped_descs, **overrides}

    # Find requested methods (direct children only — no walk into nested classes)
    info = ClassInfo(name=class_name, docstring=class_doc)
    wanted = set(method_names)

    for node in cls_node.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in wanted:
            continue
        ret_annotation = (
            f" -> {_unparse_annotation(node.returns)}"
            if node.returns else ""
        )
        structured = _structured_arguments(node.args)
        # Stamp short_desc and is_hidden onto each ArgInfo
        for a in structured:
            a.short_desc         = short_descs.get(a.name, "")
            a.is_hidden          = a.name in hidden_set
            a.is_layer           = a.name in layer_set
            a.validator_override = val_overrides.get(a.name, "")
        info.methods[node.name] = MethodInfo(
            name       = node.name,
            docstring  = _get_docstring(node),
            arguments  = _unparse_arguments(node.args),
            args       = structured,
            returntype = ret_annotation,
            is_async   = isinstance(node, ast.AsyncFunctionDef),
        )

    missing = wanted - set(info.methods)
    if missing:
        raise ValueError(
            f"Methods not found in {class_name}: {sorted(missing)}"
        )

    # Stash layer_values on info so build_vars can expose it to templates.
    info._layer_values = layer_args   # type: ignore[attr-defined]
    return info


# ---------------------------------------------------------------------------
# Template variable dict
# ---------------------------------------------------------------------------

def build_vars(info: ClassInfo) -> dict[str, Any]:
    """Convert a ClassInfo into the flat variable dict exposed to templates.

    Each method entry contains both the flat ``arguments`` string (for use
    in signature reconstruction) and a structured ``args`` list of dicts
    (for Jinja2 ``{% for %}`` loops).  Each element of ``args`` has the keys:

        name      -- bare parameter name, e.g. ``"ms"``
        type      -- annotation string, e.g. ``"Optional[str]"``, or ``""``
        default   -- default value string, e.g. ``"None"``, or ``""``
        is_self   -- True for the leading self/cls parameter
        is_star   -- True for a bare ``*`` (keyword-only separator)
        is_args   -- True for ``*args``
        is_kwargs -- True for ``**kwargs``

    Example template loop::

        {% for arg in methods['__init__'].args if not arg.is_self and not arg.is_star %}
            {{ arg.name }} = {{ arg.name }},
        {% endfor %}
    """
    methods = {}
    for name, m in info.methods.items():
        async_kw = "async " if m.is_async else ""
        methods[name] = {
            "docstring":  m.docstring,
            "arguments":  m.arguments,
            "args":       [
                {
                    "name":       a.name,
                    "type":       a.type,
                    "default":    a.default,
                    "is_self":    a.is_self,
                    "is_star":    a.is_star,
                    "is_args":    a.is_args,
                    "is_kwargs":  a.is_kwargs,
                    "short_desc":         a.short_desc,
                    "is_hidden":          a.is_hidden,
                    "is_layer":           a.is_layer,
                    "validator_override":  a.validator_override,
                }
                for a in m.args
            ],
            "returntype": m.returntype,
            "async":      async_kw,
            "signature":  (
                f"{async_kw}def {name}({m.arguments}){m.returntype}"
            ),
        }
    # layer_values: dict of name → expression for the _t shim injection.
    # Populated from the source entry's layer_args dict; empty if no layer_args.
    # Access in templates as e.g.: {{ layer_values.remote_endpoint }}
    layer_values = getattr(info, '_layer_values', {})
    return {
        "class_name":   info.name,
        "docstring":    info.docstring,
        "methods":      methods,
        "layer_values": layer_values,
    }


# ---------------------------------------------------------------------------
# Auto-indent Jinja2 preprocessing
# ---------------------------------------------------------------------------

# Matches {{ ... }} tags that are the first non-whitespace on their line,
# capturing the leading whitespace as group 1 and the expression as group 2.
_TAG_RE = re.compile(r'^(?P<indent>[ \t]*)(?P<tag>\{\{[^}]+\}\})[ \t]*$',
                     re.MULTILINE)


def _preprocess_template(source: str) -> str:
    """Rewrite indented ``{{ expr }}`` tags to ``{{ expr | _indent(N) }}``.

    A tag is only rewritten when it appears alone on its line (possibly with
    trailing whitespace).  Inline tags (mixed with other text) are left alone
    because they don't need whole-block indentation treatment.

    The first line of a substituted block keeps the template indentation
    naturally (it occupies the same column as the ``{{`` tag).  The
    ``_indent`` filter adds the same indent to all *subsequent* lines, so
    multi-line substitutions are correctly aligned throughout.
    """
    def replacer(m: re.Match) -> str:
        indent = m.group("indent")
        tag    = m.group("tag")
        n      = len(indent.expandtabs(4))
        if n == 0:
            # No indentation — no filter needed, just return the tag as-is
            # (still on its own line, so the indent prefix is empty)
            return tag
        # Strip the expression of whitespace and inject the filter.
        # e.g. {{ methods.__init__.docstring }} →
        #      {{ methods.__init__.docstring | _indent(8) }}
        inner = tag[2:-2].strip()
        return f"{indent}{{{{ {inner} | _indent({n}) }}}}"

    return _TAG_RE.sub(replacer, source)


# ---------------------------------------------------------------------------
# Jinja2 environment + custom filter
# ---------------------------------------------------------------------------

# Module-level set of layer-arg names, populated by process_config before
# each template render so that _strip_layer_filter can access it without
# needing to thread state through the Jinja2 context.
_current_layer_names: set[str] = set()


def _indent_filter(value: str, width: int) -> str:
    """Jinja2 filter: indent all lines *after the first* by *width* spaces.

    The first line is already at the correct column because the ``{{ tag }}``
    itself carried the indentation.  We only need to fix up subsequent lines.
    """
    if not isinstance(value, str):
        value = str(value)
    lines = value.splitlines()
    if len(lines) <= 1:
        return value
    pad = " " * width
    return lines[0] + "\n" + "\n".join(
        (pad + ln) if ln.strip() else ln   # don't pad blank lines
        for ln in lines[1:]
    )


def _wrap_args_filter(value: str, indent: int = 8, width: int = 88) -> str:
    """Jinja2 filter: wrap a flat argument string onto multiple lines.

    Each argument is placed on its own line, indented by *indent* spaces,
    so the result fits within *width* columns.  The closing paren is left
    to the template.

    Example (indent=8)::

        self, *, ms: Optional[str] = None, backend: str = 'auto'

    becomes::

        self,
        *,
        ms: Optional[str] = None,
        backend: str = 'auto'

    with each continuation line at column *indent*.
    """
    if not isinstance(value, str):
        value = str(value)
    # Split on commas, but only top-level ones (not inside brackets).
    args = _split_args(value)
    if len(args) <= 1:
        return value
    pad = " " * indent
    lines = [args[0]]
    for arg in args[1:]:
        lines.append(pad + arg)
    return ",\n".join(lines)


def _strip_self_filter(value: str) -> str:
    """Jinja2 filter: remove leading ``self`` (or ``cls``) from an argument string."""
    if not isinstance(value, str):
        return value
    args = _split_args(value)
    if args and args[0].strip() in ("self", "cls"):
        args = args[1:]
    return ", ".join(a.strip() for a in args)


def _strip_layer_filter(value: str) -> str:
    """Jinja2 filter: remove layer-supplied arguments from a flat argument string.

    Works on the flat ``arguments`` string rather than the structured ``args``
    list, so it can be composed with ``strip_self`` and ``wrap_args`` in the
    same pipeline::

        {{ methods['__init__'].arguments | strip_self | strip_layer | wrap_args(8) }}

    Layer arg names are identified by scanning the current template variable
    context via a thread-local set populated during rendering.  Because Jinja2
    filters don't have access to the render context, the filter relies on a
    module-level set ``_current_layer_names`` that ``process_config`` populates
    before each template render.
    """
    if not isinstance(value, str):
        return value
    args = _split_args(value)
    result = []
    for a in args:
        stripped = a.strip()
        if stripped in ("self", "cls", "*"):
            result.append(stripped)
            continue
        # Extract bare name (before any : or =)
        import re as _re
        name_m = _re.match(r'\*{0,2}(\w+)', stripped)
        name = name_m.group(1) if name_m else ""
        if name not in _current_layer_names:
            result.append(stripped)
    return ", ".join(result)


def _strip_quotes_filter(value: str) -> str:
    """Jinja2 filter: strip wrapping triple-quotes from a docstring variable.

    ``build_vars`` stores docstrings as ``\'\'\'text\'\'\'`` so they can be
    dropped directly into a ``{{ docstring }}`` tag that stands alone.  In the
    casatask template the class body owns the ``r\'\'\'`` / ``\'\'\'`` delimiters,
    so we need the raw text without the outer quotes.
    """
    v = value.strip()
    for quote in ('"""', "'''"):
        if v.startswith(quote) and v.endswith(quote):
            return v[3:-3]
    return value


def _split_args(argstr: str) -> list[str]:
    """Split a comma-separated argument string, respecting bracket nesting."""
    parts = []
    depth = 0
    current: list[str] = []
    for ch in argstr:
        if ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


class _StringLoader(BaseLoader):
    """Jinja2 loader that accepts a pre-processed template string directly."""
    def __init__(self, source: str, name: str = "<template>"):
        self._source = source
        self._name   = name

    def get_source(self, environment, template):
        return self._source, self._name, lambda: True


def make_env(template_string: str, template_name: str = "<template>") -> tuple:
    """Return (env, template) for a pre-processed template string."""
    env = Environment(
        loader        = _StringLoader(template_string, template_name),
        undefined     = StrictUndefined,
        keep_trailing_newline = True,
    )
    env.filters["_indent"]      = _indent_filter
    env.filters["wrap_args"]    = _wrap_args_filter
    env.filters["strip_self"]   = _strip_self_filter
    env.filters["strip_layer"]  = _strip_layer_filter
    env.filters["strip_quotes"] = _strip_quotes_filter
    tmpl = env.get_template(template_name)
    return env, tmpl


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def process_config(config_path: Path, project_root: Path,
                   check: bool = False, verbose: bool = False) -> bool:
    """Process one YAML config file.  Returns True if all outputs are up-to-date.

    All relative paths in the YAML (``sources[].file``, ``templates[].input``,
    ``templates[].output``) are resolved against *project_root*, which is the
    directory from which the tool was invoked — not the directory containing
    the YAML file.  This allows the YAML and its templates to live inside
    ``scripts/sync-layers/`` while the source and output paths refer to files
    anywhere in the project tree.

    Template paths (``templates[].input``) that are not absolute are resolved
    relative to the YAML file's own directory first, then project_root, so you
    can write either ``templates/foo.j2`` (resolved inside ``sync-layers/``) or
    a project-rooted path like ``cubevis/toolbox/visplot/templates/foo.j2``.
    """
    config      = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    yaml_dir    = config_path.parent   # scripts/sync-layers/
    root        = project_root.resolve()

    def _resolve_project(rel: str) -> Path:
        """Resolve a path relative to the project root (cwd)."""
        p = Path(rel)
        return p if p.is_absolute() else root / p

    def _resolve_template(rel: str) -> Path:
        """Resolve a template path: prefer yaml_dir-relative, fall back to root."""
        p = Path(rel)
        if p.is_absolute():
            return p
        candidate = yaml_dir / p
        if candidate.exists():
            return candidate
        return root / p

    # --- Build variable dicts for all sources ---
    all_vars: dict[str, Any] = {}
    source_entries = config.get("sources", [])
    if not source_entries:
        print("WARNING: no sources defined in config.", file=sys.stderr)

    for src in source_entries:
        src_path            = _resolve_project(src["file"])
        class_name          = src["class"]
        method_names        = src.get("methods", [])
        hidden_args          = src.get("hidden_args", [])
        # layer_args may be a list (names only, value=None) or a dict
        # (name → expression string injected by the _t shim).  Normalise to dict.
        _raw_layer = src.get("layer_args", [])
        if isinstance(_raw_layer, list):
            layer_args = {name: 'None' for name in _raw_layer}
        else:
            layer_args = dict(_raw_layer)
        short_desc_overrides = src.get("short_desc_overrides", {})
        validator_overrides  = src.get("validator_overrides", {})

        if verbose:
            print(f"  Scraping {src_path.relative_to(root)} "
                  f":: {class_name}  methods={method_names}")
            if hidden_args:
                print(f"    hidden_args={hidden_args}")
            if layer_args:
                print(f"    layer_args={layer_args}")
            if short_desc_overrides:
                print(f"    short_desc_overrides={list(short_desc_overrides)}")
            if validator_overrides:
                print(f"    validator_overrides={list(validator_overrides)}")

        class_info = scrape_class(
            src_path, class_name, method_names,
            hidden_args          = hidden_args,
            layer_args           = layer_args,
            short_desc_overrides = short_desc_overrides,
            validator_overrides  = validator_overrides,
        )
        vars_      = build_vars(class_info)

        if verbose:
            import pprint
            trimmed = {
                k: (v[:80] + "…" if isinstance(v, str) and len(v) > 80 else v)
                for k, v in vars_.items()
                if k != "methods"
            }
            for mname, minfo in vars_["methods"].items():
                trimmed[f"methods.{mname}.arguments"] = minfo["arguments"]
                trimmed[f"methods.{mname}.returntype"] = minfo["returntype"]
                trimmed[f"methods.{mname}.async"]      = minfo["async"]
            pprint.pprint(trimmed, width=100)

        # Expose under class name (multi-source) and also at top level
        # (single-source convenience; last writer wins on conflict).
        all_vars[class_name] = vars_
        all_vars.update(vars_)

    # --- Expand templates ---
    all_ok = True
    for tmpl_entry in config.get("templates", []):
        tmpl_path = _resolve_template(tmpl_entry["input"])
        out_path  = _resolve_project(tmpl_entry["output"])

        raw_source       = tmpl_path.read_text(encoding="utf-8")
        processed_source = _preprocess_template(raw_source)

        _, tmpl  = make_env(processed_source, str(tmpl_path))
        # Per-template vars (from YAML ``vars:`` block) override scraped vars.
        tmpl_vars = {**all_vars, **tmpl_entry.get("vars", {})}
        # Populate module-level layer-name set for _strip_layer_filter.
        global _current_layer_names
        _current_layer_names = {
            a["name"]
            for src_vars in all_vars.values()
            if isinstance(src_vars, dict) and "methods" in src_vars
            for method in src_vars["methods"].values()
            for a in method.get("args", [])
            if a.get("is_layer")
        }
        try:
            rendered = tmpl.render(**tmpl_vars)
        finally:
            _current_layer_names = set()

        if check:
            existing = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
            if rendered != existing:
                all_ok = False
                diff = difflib.unified_diff(
                    existing.splitlines(keepends=True),
                    rendered.splitlines(keepends=True),
                    fromfile=str(out_path) + " (current)",
                    tofile=str(out_path) + " (would generate)",
                )
                print(f"\n--- {out_path.relative_to(root)} is out of date ---")
                sys.stdout.writelines(diff)
            else:
                print(f"  OK  {out_path.relative_to(root)}")
        else:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            # If the file already exists and is read-only (from a previous
            # generation run), temporarily restore write permission so we
            # can overwrite it, then re-apply read-only after writing.
            if out_path.exists():
                out_path.chmod(0o644)
            out_path.write_text(rendered, encoding="utf-8")
            out_path.chmod(0o444)
            print(f"  wrote  {out_path.relative_to(root)}  (read-only)")

    return all_ok


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    """Entry point when invoked as ``python3 scripts/sync-layers``.

    The YAML config is always ``project_layers.yaml`` in the same directory
    as this file (i.e. ``scripts/sync-layers/project_layers.yaml``).

    All source and output paths in the YAML are resolved relative to the
    current working directory, which should be the project root::

        cd /path/to/cubevis
        python3 scripts/sync-layers            # generate
        python3 scripts/sync-layers --check    # CI drift check
    """
    parser = argparse.ArgumentParser(
        prog        = "python3 scripts/sync-layers",
        description = "Sync parallel Python class layers from a canonical source.",
    )
    parser.add_argument("--check", action="store_true",
                        help="Diff mode: exit 1 if any output would change.")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print extracted variable bindings.")
    args = parser.parse_args()

    # __file__ is .../scripts/sync-layers/__main__.py
    config_path  = Path(__file__).parent / "project_layers.yaml"
    project_root = Path.cwd()

    if not config_path.exists():
        print(f"ERROR: config not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    ok = process_config(config_path, project_root,
                        check=args.check, verbose=args.verbose)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
