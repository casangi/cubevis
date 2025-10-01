import subprocess
from os import path
from typing import Optional
from packaging import version

_VERSION = None
def max_git_version() -> Optional[str]:
    """Get maximum version from all git tags"""
    global _VERSION
    package_parent = path.dirname(path.dirname(path.dirname(path.realpath(__file__))))
    if _VERSION is None and path.basename( package_parent ) == 'cubevis' and \
       path.isdir(path.join( package_parent, '.git' )):
        result = subprocess.run(
            ['git', 'tag'],
            cwd=package_parent,
            capture_output=True,
            text=True,
            check=True
        )
    
        # Filter tags matching version pattern v#.#.#
        tags = [
            tag[1:] for tag in result.stdout.strip().split('\n')
                if tag.startswith('v') and all(part.isdigit() for part in tag[1:].split('.'))
        ]

        if not tags:
            return None
    
        # Sort by version number and get maximum
        try:
            max_tag = max(tags, key=lambda v: version.parse(v))
            _VERSION = max_tag
        except: pass

    return _VERSION
