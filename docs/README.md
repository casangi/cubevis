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
| `source/design/` | design documents |
| `source/contributing.md` | how to write pages, and how to include docstrings from the Python source |
| `source/extensions/pydoc_include.py` | the `task-doc`, `task-params` and `slide-decks` directives |
| `source/conf.py` | Sphinx configuration |
| `requirements.txt` | what the build needs |
| `readthedocs-redirect.yaml`, `rtd_redirect.py` | for switching Read the Docs over to redirects (see the comments in each) |

Publishing is done by `.github/workflows/docs.yml`; the slide decks in
`devel/slides` are published with it. See `source/contributing.md`.
