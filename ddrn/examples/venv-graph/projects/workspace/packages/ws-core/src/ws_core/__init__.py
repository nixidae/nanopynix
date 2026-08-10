"""The library member of the workspace, which depends on nothing."""

from __future__ import annotations

#: The tree that this module was read from. `run-uv.sh` writes a second tree
#: with a different value, and the same environment reads it back, because a
#: PEP 660 install redirects at a directory outside the store.
WHICH_TREE = "the tree that this repository holds"


def banner() -> str:
    """What this build of the member says."""
    return f"ws-core, from {WHICH_TREE}"
