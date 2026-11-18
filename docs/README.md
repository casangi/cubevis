# cubevis documentation

The source of <https://casangi.github.io/cubevis/>.

```console
$ pip install -r docs/requirements.txt
$ cd docs
$ make html                      # result: build/html/index.html
```

| Path | Contents |
|---|---|
| `source/index.md` | the front page and the table of contents |
| `source/applications/` | the user guide |
| `source/historical/` | design and implementation documents from `casagui`, kept for reference |
| `source/contributing.md` | how to write pages, and how to include docstrings from the Python source |
| `source/extensions/pydoc_include.py` | the `task-doc`, `task-params` and `slide-decks` directives |
| `source/conf.py` | Sphinx configuration |
| `requirements.txt` | what the build needs |
| `publish_site.py` | places a build into the published site (latest, releases, `docs-*` previews); used by the workflow |
| `rtd_redirect.py` | what Read the Docs publishes: a page for each old page forwarding to the same page here |

Publishing is done by `.github/workflows/docs.yml`; the slide decks in
`devel/slides` are published with it. See *Publishing* in
`source/contributing.md` for the published copies (latest, releases and
`docs-*` branch previews).
