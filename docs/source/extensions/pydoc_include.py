# -*- coding: utf-8 -*-
"""Sphinx directives that pull text out of the Python source.

The text comes from the same scraper that generates the CASA task and
casashell layers (``scripts/sync_layers``), configured by the same
``project_layers.yaml``.  The source is parsed, never imported, so the
documentation builds without cubevis or its dependencies installed.

In a Markdown page::

    ```{task-doc} visplot
    ```

    ```{task-params} visplot
    :only: vis, ms, ps, kernel_name
    ```

    ```{slide-decks}
    ```

``task-doc``
    The description at the top of the task's docstring (everything before
    the first numpy-style section such as ``Parameters``).  With
    ``:section: NAME`` it renders that section of the docstring instead.

``task-params``
    The task's parameters -- name, type, default and the full description
    from the ``Parameters`` section of the docstring -- in the order of
    the signature.  ``:only:`` and ``:exclude:`` take comma-separated
    names and let a page present the parameters in groups.  Parameters
    the layers supply themselves (``layer_args`` in the YAML) are never
    listed.  ``:all:`` lists what is left after every other
    ``task-params`` block *on the same page* has taken its share, so a
    page can end with "everything else" without repeating names.

``slide-decks``
    A list of the slide decks in ``devel/slides`` (each folder with a
    ``slides.html``), linked to where the build publishes them.

Both ``task-*`` directives draw a bordered, lightly tinted box labelled
with the Python source it came from (``_static/css/cubevis-docs.css``).

The argument is a task name (``vars.task_name`` of a template in
``project_layers.yaml``, or ``documentation[].task`` there) or a class name
(``sources[].class``).

Two docstring layouts are understood, and told apart by their content:

* numpy style (``Parameters`` underlined with dashes), as sync_layers reads
  it.  The descriptions are reStructuredText.
* the layout xml-casa generates from a CASA task's XML (``--------- parameter
  descriptions ---------``), as in ``iclean``, whose text is tclean's.  The
  descriptions are preformatted text: the first lines are shown, the rest
  behind a "more" control.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml
from docutils import nodes
from docutils.parsers.rst import Directive, Parser, directives
from docutils.utils import new_document
from sphinx.util import logging

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
LAYERS_YAML = REPO_ROOT / "scripts" / "sync_layers" / "project_layers.yaml"
SLIDES_DIR = REPO_ROOT / "devel" / "slides"

# numpy-style section: a title line followed by a line of dashes
_SECTION_RE = re.compile(r"^(?P<title>\S.*)\n-{3,}[ \t]*$", re.MULTILINE)
_PARAM_RE = re.compile(r"^(?P<name>\*{0,2}\w+(?:\s*,\s*\*{0,2}\w+)*)\s*(?::\s*(?P<type>.*))?$")


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def _scraper():
    """``scrape_class`` from scripts/sync_layers (imported on first use)."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from scripts.sync_layers.__main__ import scrape_class
    return scrape_class


def _find_source(config: dict, name: str) -> dict:
    """The ``sources`` entry for a task name or a class name."""
    sources = config.get("sources") or []
    for doc in config.get("documentation") or []:
        if doc.get("task") == name:
            return doc
    for src in sources:
        if src.get("class") == name:
            return src
    for tpl in config.get("templates") or []:
        v = tpl.get("vars") or {}
        if v.get("task_name") != name:
            continue
        module = (v.get("app_module") or "").replace(".", "/") + ".py"
        for src in sources:
            if src.get("file") == module:
                return src
    known = sorted({(t.get("vars") or {}).get("task_name") for t in config.get("templates") or []}
                   - {None}) + [d.get("task") for d in config.get("documentation") or []] \
        + [s.get("class") for s in sources]
    raise KeyError(f"{name!r} is not a task or class in {LAYERS_YAML.name} "
                   f"(known: {', '.join(known)})")


def split_sections(docstring: str) -> tuple[str, dict[str, str]]:
    """``(description, {section title: body})`` of a numpy-style docstring."""
    marks = list(_SECTION_RE.finditer(docstring))
    if not marks:
        return docstring.strip(), {}
    sections = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(docstring)
        sections[m.group("title").strip()] = docstring[m.end():end].strip("\n")
    return docstring[:marks[0].start()].strip(), sections


def parse_params(body: str) -> dict[str, tuple[str, str, tuple]]:
    """``{name: (type, description, names)}`` from a ``Parameters`` section
    body: the whole description, not only its first sentence.  An entry
    that documents several parameters at once (``raster_y, raster_x : str``)
    is filed under each, with *names* listing all of them."""
    out: dict[str, tuple[str, str, tuple]] = {}
    name = None
    typ = ""
    lines: list[str] = []

    def flush():
        if name:
            # dedent the description as a block
            text = [ln for ln in lines]
            indents = [len(ln) - len(ln.lstrip()) for ln in text if ln.strip()]
            cut = min(indents) if indents else 0
            desc = "\n".join(ln[cut:] for ln in text).strip("\n")
            group = tuple(n.strip().lstrip("*") for n in name.split(","))
            for n in group:
                out[n] = (typ, desc, group)

    for line in body.splitlines():
        if line.strip() and not line[0].isspace():
            m = _PARAM_RE.match(line.strip())
            if m:
                flush()
                name = m.group("name")
                typ = (m.group("type") or "").strip()
                lines = []
                continue
        lines.append(line)
    flush()
    return out


# xml-casa layout:  "--------- parameter descriptions -------------"
_CASA_HEAD_RE = re.compile(r"^-{5,}\s*(?P<title>.*?)\s*-{5,}\s*$", re.MULTILINE)
_CASA_PARAM_RE = re.compile(r"^(?P<name>\w+)(?:\s+(?P<text>\S.*))?$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def is_casa_layout(docstring: str) -> bool:
    return any("parameter" in m.group("title") for m in _CASA_HEAD_RE.finditer(docstring))


def parse_casa(docstring: str, names: set) -> tuple[str, dict[str, tuple[str, str, tuple]]]:
    """``(description, {name: ("", text, (name,))})`` of an xml-casa docstring.

    Every block whose heading mentions "parameter" is read.  A parameter
    starts at a line beginning with its name; *names* (the signature's)
    tells such a line from a continuation line that happens to start at
    the margin."""
    docstring = _ANSI_RE.sub("", docstring)
    marks = list(_CASA_HEAD_RE.finditer(docstring))
    description = docstring[:marks[0].start()].strip()
    out: dict[str, tuple[str, str, tuple]] = {}
    for i, m in enumerate(marks):
        if "parameter" not in m.group("title"):
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(docstring)
        name, lines = None, []

        def flush():
            if name:
                first, rest = lines[0], lines[1:]
                indents = [len(ln) - len(ln.lstrip()) for ln in rest if ln.strip()]
                cut = min(indents) if indents else 0
                text = "\n".join([first] + [ln[cut:] for ln in rest]).strip("\n")
                out[name] = ("", text, (name,))

        for line in docstring[m.end():end].splitlines():
            pm = _CASA_PARAM_RE.match(line)
            if pm and pm.group("name") in names:
                flush()
                name, lines = pm.group("name"), [pm.group("text") or ""]
            elif pm and pm.group("name") == "RETURNS":
                flush()
                name = None
            elif name:
                lines.append(line.expandtabs())
        flush()
    return description, out


def tidy_default(default: str) -> str:
    """xml-casa writes ``int(100)``, ``float(0.2)``, ``[  ]``."""
    default = re.sub(r"\b(?:int|float)\(\s*([^()]*?)\s*\)", r"\1", default)
    default = re.sub(r"\[\s+\]", "[]", default)
    return re.sub(r"\[\s+(.*?)\s+\]", r"[\1]", default)


class TaskInfo:
    """What the directives need to know about one task."""

    def __init__(self, name: str):
        config = yaml.safe_load(LAYERS_YAML.read_text(encoding="utf-8"))
        src = _find_source(config, name)
        self.task = name
        self.class_name = src["class"]
        self.file = src["file"]
        self.path = REPO_ROOT / src["file"]
        method = src.get("method", "__init__")
        info = _scraper()(self.path, self.class_name, [method],
                          hidden_args=src.get("hidden_args"),
                          layer_args=src.get("layer_args"))
        doc = info.docstring or ""
        for q in ('"""', "'''"):         # the scraper keeps the quotes for its templates
            if doc.startswith(q) and doc.endswith(q):
                doc = doc[3:-3]
        init = info.methods.get(method)
        self.args = [a for a in (init.args if init else [])
                     if not (a.is_self or a.is_star or a.is_args or a.is_kwargs or a.is_layer)]
        doc = doc.strip("\n")
        self.casa = is_casa_layout(doc)
        if self.casa:
            self.sections = {}
            self.description, self.documented = parse_casa(doc, {a.name for a in self.args})
        else:
            self.description, self.sections = split_sections(doc)
            self.documented = parse_params(self.sections.get("Parameters", ""))


def _task(env, name: str) -> TaskInfo:
    cache = env.temp_data.setdefault("pydoc_include_tasks", {})
    if name not in cache:
        cache[name] = TaskInfo(name)
    return cache[name]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _rst_nodes(text: str, directive: Directive, where: str) -> list:
    """Docstrings are reStructuredText whatever the page is written in."""
    if not text.strip():
        return []
    doc = new_document(where, directive.state.document.settings)
    doc.reporter.report_level = 5          # collected below, not printed twice
    Parser().parse(text, doc)
    out = []
    for child in doc.children:
        if isinstance(child, nodes.system_message):
            logger.warning("docstring markup (%s): %s", where, child.astext().splitlines()[-1],
                           location=directive.state_machine.get_source_and_line(directive.lineno))
            continue
        out.append(child)
    for bad in [n for o in out for n in o.findall(nodes.system_message)]:
        bad.parent.remove(bad)
    for prob in [n for o in out for n in o.findall(nodes.problematic)]:
        prob.parent.replace(prob, nodes.Text(prob.astext()))
    return out


def _plain_nodes(text: str) -> list:
    """Preformatted text (xml-casa docstrings): its first line as a
    paragraph, the rest as it is, behind a "more" control."""
    lines = text.split("\n")
    head = [lines.pop(0).strip()] if lines and lines[0].strip() else []
    while lines and not lines[0].strip():
        lines.pop(0)
    out = [nodes.paragraph(text=" ".join(head))] if head else []
    if lines:
        indents = [len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()]
        cut = min(indents) if indents else 0
        body = "\n".join(ln[cut:] for ln in lines).rstrip()
        out.append(nodes.raw("", '<details class="pydoc-more"><summary>more</summary>', format="html"))
        out.append(nodes.literal_block(body, body, language="none"))
        out.append(nodes.raw("", "</details>", format="html"))
    return out


def _box(info: TaskInfo, what: str, env) -> nodes.container:
    box = nodes.container(classes=["pydoc-box"])
    label = nodes.paragraph(classes=["pydoc-source"])
    label += nodes.Text(f"{what} · from the Python source: ")
    repo = env.config.pydoc_source_url
    text = nodes.literal(text=f"{info.class_name}")
    if repo:
        label += nodes.reference("", "", text, refuri=f"{repo.rstrip('/')}/{info.file}")
    else:
        label += text
    box += label
    return box


def _names(value: str) -> list[str]:
    return [n.strip() for n in (value or "").replace("\n", ",").split(",") if n.strip()]


class _TaskDirective(Directive):
    required_arguments = 1
    has_content = False

    def _info(self):
        env = self.state.document.settings.env
        try:
            info = _task(env, self.arguments[0])
        except (KeyError, OSError, SyntaxError) as exc:
            raise self.error(f"{self.name}: {exc}")
        env.note_dependency(str(info.path))
        env.note_dependency(str(LAYERS_YAML))
        return env, info


class TaskDoc(_TaskDirective):
    option_spec = {"section": directives.unchanged}

    def run(self):
        env, info = self._info()
        section = self.options.get("section")
        if section:
            if section not in info.sections:
                raise self.error(f"task-doc: {info.class_name} has no section {section!r} "
                                 f"(has: {', '.join(info.sections) or 'none'})")
            text, what = info.sections[section], f"{info.task}: {section}"
        else:
            text, what = info.description, f"{info.task}"
        box = _box(info, what, env)
        if info.casa:
            for para in re.split(r"\n\s*\n", text):
                if para.strip():
                    line = " ".join(ln.strip() for ln in para.splitlines())
                    box += nodes.paragraph(text=re.sub(r"\s-{2,}\s", " \u2014 ", line))
        else:
            box += _rst_nodes(text, self, f"{info.class_name} docstring")
        return [box]


class TaskParams(_TaskDirective):
    option_spec = {"only": directives.unchanged, "exclude": directives.unchanged,
                   "all": directives.flag}

    def run(self):
        env, info = self._info()
        only, exclude = _names(self.options.get("only")), set(_names(self.options.get("exclude")))
        known = {a.name: a for a in info.args}
        for n in list(only) + sorted(exclude):
            if n not in known:
                logger.warning("task-params: %s has no parameter %r", info.task, n,
                               location=self.state_machine.get_source_and_line(self.lineno))
        shown = env.temp_data.setdefault(("pydoc_include_shown", env.docname, info.task), set())
        if only:
            args = [known[n] for n in only if n in known]
        else:
            args = [a for a in info.args if a.name not in exclude]
            if "all" in self.options:
                args = [a for a in args if a.name not in shown]
        shown.update(a.name for a in args)

        box = _box(info, f"{info.task} parameters", env)
        dl = nodes.definition_list(classes=["pydoc-params"])
        listed = {a.name for a in args}
        done: set = set()
        for a in args:
            if a.name in done:
                continue
            typ, desc, group = info.documented.get(a.name, ("", "", (a.name,)))
            # parameters documented together are shown together
            together = [known[n] for n in group if n in listed] or [a]
            done.update(t.name for t in together)
            term = nodes.term()
            for i, t in enumerate(together):
                if i:
                    term += nodes.Text(", ")
                term += nodes.literal(text=t.name, classes=["pydoc-name"])
            sig = typ or a.type
            if sig:
                term += nodes.literal(text=f" : {sig}", classes=["pydoc-type"])
            defaults = {t.default for t in together}
            if len(defaults) == 1 and a.default != "":
                term += nodes.literal(text=f" = {tidy_default(a.default)}", classes=["pydoc-default"])
            item = nodes.definition_list_item("", term)
            definition = nodes.definition()
            body = (_plain_nodes(desc) if info.casa
                    else _rst_nodes(desc, self, f"{info.class_name}.{a.name}"))
            if not body:
                body = [nodes.paragraph(text="(not documented in the docstring)",
                                        classes=["pydoc-missing"])]
                logger.info("task-params: %s.%s has no description in the docstring",
                            info.class_name, a.name)
            definition += body
            item += definition
            # stable anchors: #visplot-param-vis
            for t in together:
                term["ids"].append(nodes.make_id(f"{info.task}-param-{t.name}"))
            dl += item
        box += dl
        return [box]


def slide_decks() -> list[tuple[str, str]]:
    """``[(folder, title)]`` for each deck in devel/slides."""
    decks = []
    if SLIDES_DIR.is_dir():
        for html in sorted(SLIDES_DIR.glob("*/slides.html")):
            m = re.search(r"<title>(.*?)</title>", html.read_text(encoding="utf-8", errors="replace"),
                          re.S)
            decks.append((html.parent.name, (m.group(1).strip() if m else html.parent.name)))
    return decks


class SlideDecks(Directive):
    has_content = False

    def run(self):
        env = self.state.document.settings.env
        up = "../" * env.docname.count("/")
        repo = env.config.pydoc_source_url
        lst = nodes.bullet_list()
        for folder, title in slide_decks():
            env.note_dependency(str(SLIDES_DIR / folder / "slides.html"))
            par = nodes.paragraph()
            par += nodes.reference("", title, refuri=f"{up}{folder}/slides.html")
            if repo:
                par += nodes.Text(" (")
                par += nodes.reference("", "source", refuri=f"{repo.rstrip('/').replace('/blob/', '/tree/', 1)}/devel/slides/{folder}")
                par += nodes.Text(")")
            lst += nodes.list_item("", par)
        if not lst.children:
            return [nodes.paragraph(text="No slide decks found in devel/slides.")]
        return [lst]


def setup(app):
    app.add_config_value("pydoc_source_url", "", "env")
    app.add_directive("task-doc", TaskDoc)
    app.add_directive("task-params", TaskParams)
    app.add_directive("slide-decks", SlideDecks)
    return {"version": "1.0", "parallel_read_safe": True, "parallel_write_safe": True}
