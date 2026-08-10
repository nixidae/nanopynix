"""The local project that the sealed graph installs, in two forms.

**As an editable, nothing here reaches the store.** `demoback` writes a PEP 660
wheel that carries a path and no code, and `scripts/build-local.py` rewrites
that path into `$DDRN_EDITABLE_ROOT`. So the environment reads the tree that
the check derivation names, and a second tree needs no rebuild.

**As a source distribution, this file reaches the store.** `build_sdist` puts
it in the archive, `build_wheel` puts it in a wheel, and the environment then
holds it. That install follows no tree, which is what a release is.
"""

from __future__ import annotations

WHICH_TREE = "the tree that the sealed check named"


def greet() -> str:
    return f"hello from {WHICH_TREE}"


def main() -> None:
    print(greet())
