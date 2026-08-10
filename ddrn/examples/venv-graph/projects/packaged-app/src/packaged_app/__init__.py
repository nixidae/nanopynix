"""A library that the graph installs as a PEP 660 editable."""

from __future__ import annotations

from rich.console import Console

#: The tree that this module was read from. `run-uv.sh` writes a second tree
#: with a different value, and the same environment reads it back, because a
#: PEP 660 install redirects at a directory outside the store.
WHICH_TREE = "the tree that this repository holds"


def greet() -> str:
    """What this build of the project says."""
    return f"packaged-app, from {WHICH_TREE}"


def main() -> int:
    """The console script that `[project.scripts]` names.

    An entry point exists only because an installer wrote it, so this proves
    that the PEP 660 wheel went through `pypa/installer` and not through a
    plain unzip.
    """
    Console(force_terminal=False, width=60).print(greet())
    return 0
