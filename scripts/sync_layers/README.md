# `sync_layers` — Python layer synchronisation tool

`scripts/sync_layers` is a build-time code-generation tool that keeps parallel
Python class layers (CASA task wrappers, shell interface layers, script-layer
entry points, etc.) in sync with a single canonical source class.  It scrapes
Python source files using the AST, extracts signatures and docstrings, and
expands Jinja2 templates — producing generated files that always reflect the
current interface without manual copying.

---

## Contents

- [Motivation](#motivation)
- [Quick start](#quick-start)
- [Directory layout](#directory-layout)
- [Configuration — `project_layers.yaml`](#configuration--project_layersyaml)
  - [Sources](#sources)
  - [Templates](#templates)
- [Template variables](#template-variables)
  - [Class-level](#class-level)
  - [Method-level](#method-level)
  - [Argument-level](#argument-level)
  - [`layer_values`](#layer_values)
- [Jinja2 filters](#jinja2-filters)
- [Argument modifiers](#argument-modifiers)
  - [`hidden_args`](#hidden_args)
  - [`layer_args`](#layer_args)
- [Overrides](#overrides)
  - [`short_desc_overrides`](#short_desc_overrides)
  - [`validator_overrides`](#validator_overrides)
- [CI drift detection](#ci-drift-detection)
- [Adding a new parameter](#adding-a-new-parameter)
- [Adding a new layer](#adding-a-new-layer)

---

## Motivation

`cubevis` exposes each tool (e.g. `VisibilityPlotter`) through several parallel
layers:

| Layer | File | Purpose |
|---|---|---|
| Canonical interface | `visibility_plotter.py` | Full implementation; source of truth |
| Script layer | `script_layer.py` | Module-level function + thin app adapter |
| CASA task layer | `iclean_task.py` | `casatasks`-compatible callable class |
| CASA shell layer | `iclean_shell.py` | `inp`/`go` interactive shell interface |

Every layer must expose the same parameters with the same defaults and
docstrings as the canonical interface.  Keeping these in sync by hand is
error-prone: a new parameter added to `VisibilityPlotter.__init__` must be
propagated to three other files.

`sync_layers` solves this by making the canonical class the single source of
truth and generating the other layers from it.

---

## Quick start

```bash
# From the project root:
python3 -m scripts.sync_layers            # generate all layers
python3 -m scripts.sync_layers --check    # CI mode: exit 1 if any file would change
python3 -m scripts.sync_layers --verbose  # show extracted variable bindings
```

---

## Directory layout

```
scripts/
└── sync_layers/
    ├── __main__.py           # tool implementation
    ├── project_layers.yaml   # configuration
    └── templates/
        ├── script_layer.py.j2
        ├── casatask_layer.py.j2
        └── casashell_layer.py.j2
```

All source and output paths in `project_layers.yaml` are resolved relative to
the **current working directory** (expected: project root).  Template paths are
resolved relative to the `scripts/sync_layers/` directory first, then the
project root.

---

## Configuration — `project_layers.yaml`

```yaml
sources:
  - file:    cubevis/toolbox/visplot/visibility_plotter.py
    class:   VisibilityPlotter
    methods:
      - __init__
      - __call__
      - show
    hidden_args:
      - backend
    layer_args:
      remote_endpoint: None
    short_desc_overrides:
      mode: 'Display mode: both, raster, or scatter.'
    validator_overrides:
      some_param: '(tuple, list)'

templates:
  - input:  templates/casashell_layer.py.j2
    output: cubevis/toolbox/visplot/iclean_shell.py
    vars:
      task_name:  iclean
      app_module: cubevis.private.apps
      info_group: imaging
      info_desc:  Radio Interferometric Image Reconstruction
```

### Sources

Each entry under `sources` identifies a canonical class to scrape.

| Key | Type | Description |
|---|---|---|
| `file` | `str` | Path to the source file, relative to the project root |
| `class` | `str` | Class name to scrape |
| `methods` | `list[str]` | Methods to extract (signatures + docstrings) |
| `hidden_args` | `list[str]` | Parameters excluded from `inp()` display only |
| `layer_args` | `dict[str, str]` | Parameters supplied by the layer, not the user (see [below](#layer_args)) |
| `short_desc_overrides` | `dict[str, str]` | Per-parameter short description overrides |
| `validator_overrides` | `dict[str, str]` | Per-parameter validator type expression overrides |

### Templates

Each entry under `templates` specifies one output file to generate.

| Key | Type | Description |
|---|---|---|
| `input` | `str` | Jinja2 template path (relative to `scripts/sync_layers/`) |
| `output` | `str` | Output file path (relative to project root) |
| `vars` | `dict` | Extra variables merged into the template render context |

`vars` entries override scraped values if keys collide, making them suitable
for layer-specific metadata such as `task_name`, `app_module`, `info_group`,
and `info_desc` that have no Python-source equivalent.

---

## Template variables

### Class-level

| Variable | Type | Description |
|---|---|---|
| `class_name` | `str` | Class name, e.g. `"VisibilityPlotter"` |
| `docstring` | `str` | Class docstring wrapped in `"""..."""` |
| `layer_values` | `dict[str, str]` | Layer-arg name → injection expression (see [`layer_args`](#layer_args)) |

### Method-level

Accessed as `methods['method_name'].field`.  Dunder methods require bracket
notation: `methods['__init__']` not `methods.__init__`.

| Field | Type | Description |
|---|---|---|
| `docstring` | `str` | Method docstring wrapped in `"""..."""` |
| `arguments` | `str` | Full argument list string, e.g. `"self, *, ms: Optional[str] = None, ..."` |
| `args` | `list[dict]` | Structured argument list (see [Argument-level](#argument-level)) |
| `returntype` | `str` | Return annotation string, e.g. `" -> None"`, or `""` |
| `async` | `str` | `"async "` if the method is async, else `""` |
| `signature` | `str` | Convenience: `"async def name(arguments)returntype"` |

### Argument-level

Each element of `methods['method_name'].args` is a dict with the following
keys.

| Key | Type | Description |
|---|---|---|
| `name` | `str` | Bare parameter name, e.g. `"ms"` |
| `type` | `str` | Annotation string, e.g. `"Optional[str]"`, or `""` |
| `default` | `str` | Default value string, e.g. `"None"`, or `""` |
| `is_self` | `bool` | `True` for the leading `self`/`cls` parameter |
| `is_star` | `bool` | `True` for a bare `*` keyword-only separator |
| `is_args` | `bool` | `True` for `*args` |
| `is_kwargs` | `bool` | `True` for `**kwargs` |
| `short_desc` | `str` | First sentence of per-parameter docstring prose, or override |
| `is_hidden` | `bool` | `True` when listed in `hidden_args` |
| `is_layer` | `bool` | `True` when listed in `layer_args` |
| `validator_override` | `str` | Type expression override for shell-layer validator, or `""` |

### `layer_values`

A dict mapping each `layer_args` parameter name to its injection expression
string.  Use this in the `_t` shim to supply layer-provided values to the
canonical constructor:

```jinja
    _app = {{ class_name }}(
{%- for arg in methods['__init__'].args if not arg.is_self and not arg.is_star and not arg.is_layer %}
        {{ arg.name }} = {{ arg.name }},
{%- endfor %}
{%- if layer_values %}
{%- for name, expr in layer_values.items() %}
        {{ name }} = {{ expr }},
{%- endfor %}
{%- endif %}
    )
```

---

## Jinja2 filters

| Filter | Signature | Description |
|---|---|---|
| `strip_self` | `(value: str) -> str` | Remove leading `self`/`cls` from a flat argument string |
| `strip_layer` | `(value: str) -> str` | Remove `layer_args` parameters from a flat argument string |
| `wrap_args` | `(value: str, indent: int = 8) -> str` | Wrap a flat argument string onto one-argument-per-line, indented at `indent` spaces |
| `strip_quotes` | `(value: str) -> str` | Strip wrapping `"""..."""` from a docstring variable (for use when the template owns the delimiters) |
| `_indent` | `(value: str, width: int) -> str` | Applied automatically by the preprocessor; indent lines 2..N of a substitution block |

Filters compose naturally:

```jinja
{{ methods['__init__'].arguments | strip_self | strip_layer | wrap_args(8) }}
```

### Auto-indentation

A `{{ expr }}` tag that appears alone on a line inherits its column offset as
an indent applied to every line of the substituted value after the first.  This
means multi-line docstrings and argument lists are correctly indented without
explicit filter calls:

```jinja
    def __init__(
            {{ methods['__init__'].arguments | strip_self | wrap_args(12) }},
    ):
        {{ methods['__init__'].docstring }}
```

---

## Argument modifiers

### `hidden_args`

Parameters listed in `hidden_args` are marked `is_hidden = True`.  They remain
in the user-facing signature, `_arg_default`, `tget`/`tput`, and logging, but
are suppressed from `inp()` display.  Use this for expert or internal
parameters that are settable but not shown by default.

```yaml
hidden_args:
  - backend
```

Template usage:

```jinja
{%- for arg in methods['__init__'].args
    if not arg.is_self and not arg.is_star
    and not arg.is_layer and not arg.is_hidden %}
    self.__{{ arg.name }}_inp()
{%- endfor %}
```

### `layer_args`

Parameters listed in `layer_args` are marked `is_layer = True`.  They are
**owned by the layer**, not the user.  The layer supplies the value internally
(e.g. an execution context, a fixed endpoint, a session token).

`layer_args` is a `dict[str, str]` mapping parameter name to the Python
expression the layer injects:

```yaml
layer_args:
  remote_endpoint: None
  exec_context: _get_exec_context()
```

Layer args are excluded from:

- User-facing function signatures
- `inp()` display
- `_arg_description` and `_arg_default` dicts
- Global frame lookup
- `tget`/`tput` save/restore
- Logging

They are **included** in the canonical constructor call via `layer_values`,
where the injection expression is substituted directly.

**Semantic summary**

| | `inp()` | User signature | Global frame | `_arg_default` | `tput`/`tget` | Logging | Canonical call |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Normal arg | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `hidden_args` | ✗ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `layer_args` | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✓ (injected) |

---

## Overrides

### `short_desc_overrides`

The tool extracts per-parameter short descriptions by taking the first sentence
of each parameter's prose block in the numpy-style class docstring.  When the
heuristic produces an unsatisfactory result, override it:

```yaml
short_desc_overrides:
  mode:   'Display mode: both, raster, or scatter.'
  preset: 'Named startup preset (vplot, radplot, waterfall).'
```

### `validator_overrides`

The `casashell_layer` template generates a `__validate_` method that checks
parameter values against their type annotations for `inp()` colorization.  When
the annotation cannot express the full set of types the shell should accept as
valid, override it:

```yaml
validator_overrides:
  some_param: '(tuple, list)'
```

The expression is a type name or parenthesised tuple of type names accepted by
`isinstance`.  The annotation in the source file remains authoritative for type
checkers; this is purely a shell-layer UX affordance.

> **Note:** prefer tightening the source annotation over using
> `validator_overrides`.  For example, `Optional[tuple]` → `tuple[float, float]
> | list[float] | None` accurately reflects both the accepted types and the
> expected shape, making `validator_overrides` unnecessary.

---

## CI drift detection

```bash
python3 -m scripts.sync_layers --check
```

`--check` mode compares each output file against what would be generated and
exits non-zero if any file is out of date, printing a unified diff.  Add it as
a CI step to catch layers that were not regenerated after a signature change:

```yaml
# .github/workflows/ci.yml (excerpt)
- name: Check generated layers are up to date
  run: python3 -m scripts.sync_layers --check
```

---

## Adding a new parameter

1. Add the parameter to the canonical class `__init__` signature and docstring.
2. Run `python3 -m scripts.sync_layers`.
3. All generated layers are updated automatically.

If the parameter should be hidden from `inp()`, add it to `hidden_args` in
`project_layers.yaml`.  If its short description doesn't read well from the
first-sentence heuristic, add it to `short_desc_overrides`.

---

## Adding a new layer

1. Write a Jinja2 template in `scripts/sync_layers/templates/`.
2. Add a `templates` entry to `project_layers.yaml`:

```yaml
templates:
  - input:  templates/my_new_layer.py.j2
    output: cubevis/toolbox/visplot/my_new_layer.py
    vars:
      task_name:  my_task
      app_module: cubevis.private.apps
```

3. Run `python3 -m scripts.sync_layers`.

Any key/value pairs under `vars` are merged into the Jinja2 render context and
override scraped values, so they are safe to use for layer-specific metadata.
