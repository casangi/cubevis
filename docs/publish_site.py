#!/usr/bin/env python3
"""Place one documentation build into the published site (the gh-pages branch).

    python docs/publish_site.py target REF_TYPE REF_NAME
    python docs/publish_site.py add    SITE BUILD KIND NAME [COMMIT]
    python docs/publish_site.py remove SITE KIND NAME

The site (a checkout of the gh-pages branch) holds every published copy of
the documentation side by side:

    /                    latest: the docs on main
    /v<major>.<minor>/   a release, one per major.minor version
    /dev/<branch>/       a preview of a docs-* branch, until it is deleted
    /versions.json       the list of all of them, read by the version menu

``target`` prints, for the ref a workflow runs on, ``kind=...`` and
``name=...`` lines (for $GITHUB_OUTPUT) saying where its build goes:

    branch main          kind=latest   name=main
    branch docs-<x>      kind=dev      name=docs-<x>
    tag v1.2.3           kind=release  name=1.2
    anything else        kind=none

A tag makes the v<major>.<minor> copy the first time that major.minor is
seen.  Later patch tags (v1.2.4, ...) replace it, so the 1.2 documentation
is always that of the newest 1.2.x release; set CUBEVIS_DOCS_PATCH_UPDATES=0
in the environment to keep the copy made by the first tag instead.

``add`` copies the Sphinx output BUILD into place: ``latest`` replaces
everything at the top of the site except the release and preview
directories; ``release`` and ``dev`` replace only their own directory.
``remove`` deletes a release or preview directory.  Both rewrite
versions.json.

Standard library only.
"""
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

DEV_PREFIX = "docs-"
DEV_DIR = "dev"
RELEASE = re.compile(r"^v(\d+)\.(\d+)$")          # a release directory
TAG = re.compile(r"^v(\d+)\.(\d+)(?:\.(\d+))?$")  # a release tag
INFO = ".build.json"                              # what is in a directory
# Kept at the top of the site when latest is replaced (besides the
# release directories).
KEEP = {".git", ".nojekyll", "CNAME", DEV_DIR, "versions.json"}


def dev_dir(branch):
    """docs-a/b -> docs-a-b: one directory level per preview."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", branch)


def target(ref_type, ref_name):
    if ref_type == "branch" and ref_name == "main":
        return "latest", "main"
    if ref_type == "branch" and ref_name.startswith(DEV_PREFIX):
        return "dev", ref_name
    if ref_type == "tag":
        m = TAG.match(ref_name)
        if m:
            return "release", f"{m.group(1)}.{m.group(2)}"
    return "none", ""


def where(site, kind, name):
    if kind == "latest":
        return site
    if kind == "release":
        return site / f"v{name}"
    if kind == "dev":
        return site / DEV_DIR / dev_dir(name)
    sys.exit(f"unknown kind {kind!r}")


def _clear_latest(site):
    """Remove the latest copy: everything at the top but releases and previews."""
    for p in site.iterdir():
        if p.name in KEEP or (p.is_dir() and RELEASE.match(p.name)):
            continue
        shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()


def add(site, build, kind, name, commit=""):
    dest = where(site, kind, name)
    if kind == "release" and dest.exists() and os.environ.get("CUBEVIS_DOCS_PATCH_UPDATES", "1") == "0":
        print(f"{dest.name} exists and CUBEVIS_DOCS_PATCH_UPDATES=0: left as it is")
        return False
    if kind == "latest":
        _clear_latest(dest)
    elif dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(build, dest, dirs_exist_ok=True)
    (dest / INFO).write_text(json.dumps({
        "kind": kind, "name": name, "commit": commit,
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }, indent=1) + "\n")
    (site / ".nojekyll").touch()          # serve _static/ and friends as is
    write_versions(site)
    print(f"{kind} {name} -> {dest.relative_to(site).as_posix() or '/'}")
    return True


def remove(site, kind, name):
    if kind == "latest":
        sys.exit("latest is not removed")
    dest = where(site, kind, name)
    if dest.exists():
        shutil.rmtree(dest)
        print(f"removed {dest.relative_to(site).as_posix()}")
    else:
        print(f"{dest.relative_to(site).as_posix()}: not there")
    write_versions(site)


def _info(path):
    try:
        return json.loads((path / INFO).read_text())
    except (OSError, ValueError):
        return {}


def write_versions(site):
    """versions.json: what the version menu offers, newest release first."""
    entries = [dict(_info(site), name="latest", title="latest (main)", path="")]
    releases = sorted((p for p in site.iterdir() if p.is_dir() and RELEASE.match(p.name)),
                      key=lambda p: tuple(map(int, RELEASE.match(p.name).groups())),
                      reverse=True)
    for p in releases:
        entries.append(dict(_info(p), name=p.name[1:], title=p.name[1:],
                            kind="release", path=f"{p.name}/"))
    dev = site / DEV_DIR
    for p in sorted(dev.iterdir()) if dev.is_dir() else ():
        if p.is_dir():
            info = _info(p)
            entries.append(dict(info, title=f"preview: {info.get('name', p.name)}",
                                kind="dev", path=f"{DEV_DIR}/{p.name}/"))
    (site / "versions.json").write_text(json.dumps(entries, indent=1) + "\n")


def main(argv):
    cmd, args = (argv[1], argv[2:]) if len(argv) > 1 else ("", [])
    if cmd == "target" and len(args) == 2:
        kind, name = target(*args)
        print(f"kind={kind}\nname={name}")
    elif cmd == "add" and len(args) in (4, 5):
        add(Path(args[0]), Path(args[1]), *args[2:])
    elif cmd == "remove" and len(args) == 3:
        remove(Path(args[0]), *args[1:])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
