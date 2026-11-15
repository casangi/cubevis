#!/usr/bin/env python3
"""Write redirect pages for Read the Docs (see docs/readthedocs-redirect.yaml).

    python docs/rtd_redirect.py OUTDIR [SITE]

For every page of the documentation (each .md or .rst under docs/source
that Sphinx builds) this writes OUTDIR/<page>.html, a page that forwards
to SITE/<page>.html.  SITE defaults to https://casangi.github.io/cubevis.
Pages that Read the Docs published before the documentation was
reorganised are listed in MOVED, so their links keep working too.

Standard library only.
"""
import sys
from pathlib import Path

SITE = "https://casangi.github.io/cubevis"
SOURCE = Path(__file__).resolve().parent / "source"
SKIP = ("python", "notebooks", "_static", "_templates", "extensions", "links")

# old page -> where it is now (both without .html)
MOVED = {
    "applications/intro": "index",
}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>cubevis documentation has moved</title>
<link rel="canonical" href="{url}">
<meta http-equiv="refresh" content="0; url={url}">
<script>location.replace("{url}" + location.hash);</script>
</head><body>
<p>The cubevis documentation has moved to <a href="{url}">{url}</a>.</p>
</body></html>
"""


def pages():
    for path in sorted(SOURCE.rglob("*")):
        rel = path.relative_to(SOURCE)
        if path.suffix in (".md", ".rst") and rel.parts[0] not in SKIP:
            yield rel.with_suffix("").as_posix()


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    out, site = Path(argv[1]), (argv[2] if len(argv) > 2 else SITE).rstrip("/")
    targets = {p: p for p in pages()}
    targets.update(MOVED)
    for page, target in sorted(targets.items()):
        dest = out / f"{page}.html"
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = f"{site}/" if target == "index" else f"{site}/{target}.html"
        dest.write_text(PAGE.format(url=url), encoding="utf-8")
    print(f"{len(targets)} redirect pages written to {out}")


if __name__ == "__main__":
    main(sys.argv)
