# How the cubevis documentation is published

The documentation in `docs/` (Sphinx) and the slide decks in
`devel/slides/` are published together to GitHub Pages:

<https://casangi.github.io/cubevis/>

This note explains what happens between a push and the page changing, how
the release copies (`/v1.0/`, `/v1.1/`, ...) are made, and how to use
`docs-*` branches to show documentation changes to others before they are
merged. How to *write* the documentation is in `docs/README.md` and in
*Contributing to the documentation* (`docs/source/contributing.md`).

The pieces:

| File | What it does |
|---|---|
| `.github/workflows/docs.yml` | the workflow: decides where a build goes, builds, publishes |
| `docs/publish_site.py` | places one build into the site and rewrites `versions.json` |
| `docs/source/conf.py` | the banner and search-engine settings for each kind of copy |
| `docs/source/_static/js/versions.js` | the *Version* menu under the sidebar title |
| `docs/rtd_redirect.py`, `.readthedocs.yaml` | what Read the Docs publishes: pages forwarding here |
| `cubevis/_docs.py` | `cubevis.docs()`: opens the copy that matches the installed cubevis |

## What is published

Several copies of the documentation sit side by side on one site:

| Copy | Address | Made by |
|---|---|---|
| latest | `https://casangi.github.io/cubevis/` | every documentation change pushed to `main` |
| a release | `https://casangi.github.io/cubevis/v1.2/` | the first `v1.2.x` tag; later `v1.2.x` tags replace it |
| a preview | `https://casangi.github.io/cubevis/dev/docs-<name>/` | creating or pushing a `docs-<name>` branch |

Each copy is a complete site, slide decks included
(`.../v1.2/architecture/slides.html`). Every copy has a *Version* menu
under the sidebar title listing all the others; choosing one opens the
same page there, or that copy's front page if the page does not exist in
it. Releases and previews show a banner at the top pointing to the latest
copy. Previews are marked `noindex`, so search engines leave them out, and
every copy names the latest page as its canonical address, so searches
lead to the latest copy.

## The `gh-pages` branch

`gh-pages` is an ordinary git branch of `casangi/cubevis`, next to `main`,
with two differences:

* **It holds only the built website**, laid out exactly as the site's
  addresses. No Python, no `docs/` sources:

  ```
  gh-pages                              address
  ├── index.html                        casangi.github.io/cubevis/
  ├── applications/visplot.html         .../cubevis/applications/visplot.html
  ├── architecture/slides.html          .../cubevis/architecture/slides.html
  ├── v1.0/ ...                         .../cubevis/v1.0/
  ├── dev/docs-foo/ ...                 .../cubevis/dev/docs-foo/
  ├── versions.json                     the list the Version menu reads
  └── .nojekyll                         serve the files as they are
  ```

* **It shares no history with `main`.** It is an *orphan* branch (its
  commit has no parent), and each publish replaces it with one new commit
  holding the whole site. It always has exactly one commit.

Look at it like any branch: <https://github.com/casangi/cubevis/tree/gh-pages>,
or `git fetch origin gh-pages && git switch gh-pages`. The name is only a
convention; Pages can serve any branch.

**Never edit it by hand**: the next publish replaces it. To change the
site, change the sources and push (or run the workflow, below).

### Why one commit

A plain `git clone` fetches every branch, so a `gh-pages` that kept every
past snapshot of the site would make every clone of cubevis heavier for
no benefit. The sources on `main` (and the tags) have the history; any
copy can be rebuilt from them.

### `versions.json` and `.build.json`

Each copy holds a small `.build.json` (what it is, the commit it was built
from, when). After every change `publish_site.py` rewrites `versions.json`
at the top of the site from the directories that exist and their
`.build.json`, newest release first:

```json
[
 {"name": "latest", "title": "latest (main)", "path": "", "commit": "...", "built": "..."},
 {"name": "1.1", "title": "1.1", "kind": "release", "path": "v1.1/", ...},
 {"name": "1.0", "title": "1.0", "kind": "release", "path": "v1.0/", ...},
 {"name": "docs-foo", "title": "preview: docs-foo", "kind": "dev", "path": "dev/docs-foo/", ...}
]
```

The Version menu and `cubevis.docs()` both read it.

## From a push to the page

```
push to main
   │
   ▼
GitHub Actions: "Publish documentation" (.github/workflows/docs.yml)
   │  target   where does this build go?  main → the top of the site
   │  build    on a fresh Ubuntu machine: pip install -r docs/requirements.txt,
   │           sphinx-build docs/source → _site/, kept with the run as "docs-site"
   │  publish  fetch gh-pages, docs/publish_site.py puts _site/ in place
   │           (keeping v*/ and dev/), commit, push gh-pages
   ▼
gh-pages updated
   │
   ▼
GitHub Pages sees the push and runs its own workflow, "pages-build-deployment"
   │  (shown in the Actions tab; GitHub provides it) and copies the
   │  branch's files to its web servers
   ▼
https://casangi.github.io/cubevis/   (a minute or so later)
```

The three jobs of `docs.yml`:

* **target** maps the event to a *kind* and a *name*
  (`python docs/publish_site.py target REF_TYPE REF_NAME`):

  | Event | kind | name | goes to |
  |---|---|---|---|
  | push to `main` | `latest` | `main` | `/` |
  | tag `v1.2.3` created | `release` | `1.2` | `/v1.2/` |
  | `docs-foo` created or pushed | `dev` | `docs-foo` | `/dev/docs-foo/` |
  | `docs-foo` deleted | `remove` | `docs-foo` | `/dev/docs-foo/` removed |
  | pull request | `pr` | the PR's branch | not published |
  | anything else | `none` | | nothing happens |

* **build** runs Sphinx with `CUBEVIS_DOCS_KIND` and `CUBEVIS_DOCS_NAME`
  set, which `conf.py` uses for the banner, the title (`cubevis 1.2`) and
  `noindex`, and `versions.js` to know which copy it is in. The result is
  attached to the run as the `docs-site` artifact (kept 7 days).

* **publish** (not for pull requests) checks out the current `gh-pages`
  into a fresh directory, runs `publish_site.py add` (or `remove`), commits
  the whole result as a single new commit and pushes it over `gh-pages`.

What `publish_site.py add` replaces:

* **latest**: everything at the top of the site *except* the release
  directories (`v<major>.<minor>/`), `dev/`, `versions.json`, `.nojekyll`
  and `CNAME`;
* **release** and **dev**: only their own directory.

### The Pages setting

Settings → Pages → *Build and deployment* is set to **Deploy from a
branch**, branch **`gh-pages`**, folder **`/ (root)`**: whenever `gh-pages`
changes, serve its files. `.nojekyll` (written by every publish) stops
Pages from running the files through Jekyll, which would drop Sphinx's
`_static/` and `_images/` (Jekyll ignores names starting with `_`).

The address comes from the repository: `casangi` is the account, so its
site is `casangi.github.io`, and `/cubevis/` is this repository's part.

This replaced the earlier setting, *GitHub Actions*, under which a
workflow hands Pages a finished bundle and the whole site is that bundle:
whichever workflow ran last owned all of it. That is how the slides
workflow once replaced the whole documentation site with the slides. A
branch is storage that persists between runs, so each run changes only
its own part.

### Permissions and simultaneous runs

* The publish job pushes with the workflow's own `GITHUB_TOKEN`, allowed to
  write by `permissions: contents: write` on that job only. Pushes made
  with that token start no other workflows (so publishing cannot loop),
  except GitHub's own Pages deployment.
* Two runs can publish at once (a tag and a push to `main`, say). Each
  pushes with `--force-with-lease`: the push succeeds only if `gh-pages` is
  still the commit that run started from. Otherwise the run fetches the
  new `gh-pages`, places its build again and retries (up to six times), so
  one run never erases another's copy.
* Builds of the same ref cancel each other (`concurrency`, per ref): two
  quick pushes to `main` build only the second. A publish is never
  cancelled half way.

### When the workflow runs

On a push to `main` or a `docs-*` branch, only if the push changes one of
the `paths` listed in `docs.yml`: `docs/`, `devel/slides/`,
`scripts/sync_layers/`, the workflow itself, and the source files whose
docstrings the documentation includes. When the documentation starts
including docstrings from another source file, add it to **both** `paths`
lists (push and pull request).

Creating a branch or a tag (the `create` event) is not filtered by paths,
nor is a branch deletion (the `delete` event).

To run it by hand: Actions → *Publish documentation* → **Run workflow**,
and choose `main`, a `docs-*` branch or a tag. That is also the fix when a
publish seems not to have happened.

## Versioned (release) documentation

Releases keep one copy per **major.minor** version, never per patch:

| Tag pushed | Effect |
|---|---|
| `v1.0.116`, first tag since this workflow exists | creates `/v1.0/` |
| `v1.0.117` | replaces `/v1.0/` with v1.0.117's documentation |
| `v1.1.0` | creates `/v1.1/`; `/v1.0/` stays as it is |
| `v2.0.0` | creates `/v2.0/` |

So the 1.0 documentation is always that of the newest 1.0.x release. To
keep instead the copy the *first* tag of a major.minor made, set
`CUBEVIS_DOCS_PATCH_UPDATES` to `"0"` in the publish step of `docs.yml`.

Things to know:

* **The tag is what counts, not the version in a file.** The copy is made
  from the tagged commit, with that commit's `docs.yml`. Tags made before
  this workflow existed (`v1.0.115` and earlier) cannot be built.
* **The tag must reach GitHub.** A tag that exists only locally does
  nothing until `git push origin v1.2.0`. Making a release in the GitHub
  web interface also creates the tag.
* **Push tags one or two at a time.** GitHub sends no `create` event when
  more than three tags are pushed at once, so none of them would be built.
* **A tag is a release.** Pushing a `v*` tag also runs `publish.yml`, which
  uploads to PyPI. Do not tag only to rebuild the documentation; use *Run
  workflow* on the tag instead.
* **Deleting a tag does not remove its copy.** To remove a release copy,
  do it by hand (below).
* Version numbers that do not look like `vX.Y` or `vX.Y.Z` (for example
  `v1.2.0rc1`) are ignored.

### Removing a release copy by hand

```sh
git clone --branch gh-pages --depth 1 https://github.com/casangi/cubevis.git site
python docs/publish_site.py remove site release 1.0     # from a cubevis checkout
cd site && git add -A && git commit -m "Remove the 1.0 documentation" && git push
```

## Previewing changes: `docs-*` branches

A branch whose name starts with `docs-` is published under `/dev/` while
it exists, so people can read the rendered documentation before it is
merged:

```sh
git switch -c docs-grid-guide          # from main, or wherever the work starts
# ... edit docs/ ...
git push -u origin docs-grid-guide
```

* **Creating** the branch on GitHub builds it at once, whatever it
  changes (the `create` event). Its address is
  `https://casangi.github.io/cubevis/dev/docs-grid-guide/`, a minute or
  two after the push. It is also in every copy's Version menu, as
  *preview: docs-grid-guide*.
* **Each push** rebuilds it, if the push changes one of the documentation
  paths (above); otherwise use *Run workflow* on the branch.
* **Deleting** the branch (by hand, or with *Delete branch* after its pull
  request is merged) removes the preview and its menu entry. The removal
  runs the workflow file on `main`, so it only works for branches deleted
  after this workflow reached `main`.
* A `/` in the branch name becomes `-` in the address: `docs-gs/remote`
  is published at `/dev/docs-gs-remote/`.
* A preview shows a banner saying it is not merged, and is hidden from
  search engines. Do not put preview addresses in documents or
  announcements; they stop working when the branch goes.
* Any other branch name is not published. Its pull request still builds
  the documentation, and the result is attached to the workflow run as the
  `docs-site` artifact (download it from the run's *Summary* page and open
  `index.html`). A pull request from a `docs-*` branch gets both.
* Forks cannot publish previews (their workflow runs cannot push to this
  repository); their pull requests get the artifact.

Typical use: open a `docs-*` branch, push, send the preview address to
the people reviewing, open a pull request when it is ready, merge, delete
the branch. Once merged, the change is in the latest copy.

## `cubevis.docs()`

`cubevis.docs()` opens the copy that matches the installed cubevis, using
`versions.json`:

* a release `1.2.x` opens `/v1.2/` when it is listed there;
* otherwise (a development version, a release older than the versioned
  documentation, or no network within 3 seconds) it opens the latest copy.

It opens a browser (the one `$BROWSER` names, else the system's) except
in a Jupyter kernel, where the browser would start on the kernel's
computer, which may not be the user's; there it shows the address as a
link instead. It always returns the address. `cubevis.docs('visplot')`
opens a page (`'install'`, `'visplot'`, `'iclean'`, `'remote'`, or a path
in the documentation); `launch=False` / `True` overrides the browser
choice. `cubevis/_docs.py` is loaded by name on first use and is not part
of the remote kernel fingerprint.

## Read the Docs

<https://cubevis.readthedocs.io/> held the documentation before it moved
here. Read the Docs builds its *latest* version from `main` using
`.readthedocs.yaml`, which no longer runs Sphinx: `docs/rtd_redirect.py`
writes, for every page of the documentation, a small page that forwards to
the same page on GitHub Pages (and the pages that moved during the
reorganisation, listed in `MOVED`, to their new places), plus a `404.html`
that forwards anything else to the front page. Links and bookmarks to the
old site keep working.

## Building locally

```sh
pip install -r docs/requirements.txt
python -m sphinx -b html docs/source _build/html
```

Open `_build/html/index.html`. A local build has no banner, and no Version
menu (there is no `versions.json` beside it). To see how a release or a
preview will look, set the variables the workflow sets:

```sh
CUBEVIS_DOCS_KIND=release CUBEVIS_DOCS_NAME=1.2 python -m sphinx -b html docs/source _build/v1.2
CUBEVIS_DOCS_KIND=dev CUBEVIS_DOCS_NAME=docs-foo python -m sphinx -b html docs/source _build/dev
```

To try the whole site with its menu, place builds into an empty directory
with `publish_site.py` and serve it under `/cubevis/`:

```sh
mkdir -p www/cubevis
python docs/publish_site.py add www/cubevis _build/html latest main
python docs/publish_site.py add www/cubevis _build/v1.2 release 1.2
python -m http.server --directory www 8000     # http://localhost:8000/cubevis/
```

## When something goes wrong

| Symptom | Look at |
|---|---|
| a push did not change the site | did it touch a documentation path? Actions → *Publish documentation*: did it run, did *build* fail? Then *Run workflow* |
| *build* failed | the Sphinx log in the run; reproduce with the local build above |
| *publish* failed after retries | many runs at once; *Run workflow* again |
| the workflow ran but the page is old | Actions → *pages-build-deployment* (GitHub's), and Settings → Pages still on `gh-pages` / `/ (root)`; the browser's cache |
| a tag made no release copy | was it pushed, alone, as `vX.Y.Z`, from a commit that has `docs.yml`? |
| a deleted `docs-*` branch is still listed | the delete event runs `main`'s workflow; remove it with `publish_site.py remove site dev <branch>` as for a release |
| the site shows no styling | `.nojekyll` missing from `gh-pages` (any publish restores it) |
