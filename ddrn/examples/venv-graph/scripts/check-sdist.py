"""Report what the environment holds when the project ships as an sdist.

`check-sdist.nix` runs this with the `bin/python` of the environment that the
graph built. `scripts/check-packaged.py` reports the same project installed as
a PEP 660 editable, and the two reports differ in one line that matters.

**This is the form that reaches a user who does not have the tree.** `uv build`
writes a source distribution, an index serves it, and the consumer runs the
PEP 517 `build_wheel` hook over what the archive holds. So the environment
reads the store, and no directory outside the store answers for the code:

    editable   loaded from $DDRN_EDITABLE_ROOT/src/packaged_app/__init__.py
    sdist      loaded from <the environment>/lib/python3.14/site-packages/...

The second one cannot follow an edit, and that is the point of a release.
"""

from __future__ import annotations

import os
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import cast

import packaged_app  # pyright: ignore[reportMissingImports] -- only the graph installs this, from the tree of the project

#: What `uv.lock` resolves for the default selection. The form of the install
#: does not change this: a source distribution and a PEP 660 editable both end
#: as a wheel, and `pypa/installer` writes the same `.dist-info` for either.
EXPECTED = {"packaged-app", "rich", "markdown-it-py", "mdurl", "pygments"}

#: In the `ddrn` dependency group, which the selection does not enable. The
#: backend built the archive and then built the wheel, and it is in neither.
EXCLUDED = {"hatchling", "installer", "editables", "flit-core", "packaging", "pathspec", "pluggy"}


def canonical(name: str) -> str:
    """The name, as PEP 503 normalises it.

    `uv.lock` calls a project `pygments`, and the `Name` of its metadata is
    `Pygments`. `packaging` is not in this environment, so this is the rule of
    PEP 503 itself.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def main() -> int:
    print(f"interpreter  {sys.executable}")
    print(f"prefix       {sys.prefix}")

    print("distributions")
    found: set[str] = set()
    for dist in sorted(metadata.distributions(), key=lambda item: item.name or ""):
        name = dist.name or "?"
        found.add(canonical(name))
        print(f"  {name}=={dist.version}")

    if found != EXPECTED:
        print(f"the environment holds {sorted(found)}, and uv.lock resolves {sorted(EXPECTED)}")
        return 1
    leaked = sorted(found & EXCLUDED)
    if leaked:
        print(f"{leaked} builds this environment and must not be in it")
        return 1

    module = Path(cast("str", packaged_app.__file__))
    greeting = cast("str", packaged_app.greet())  # pyright: ignore[reportUnknownMemberType] -- the import above is unresolved, so this call has no type
    print("release")
    print(f"  {greeting}")
    print(f"  loaded from {module}")

    # **The archive holds the code, so nothing outside the store answers for
    # it.** The check derivation still sets this variable, and an editable
    # install of the same project would read the tree that it names. This one
    # must not, whatever the variable says.
    root = os.environ.get("DDRN_EDITABLE_ROOT", "")
    print(f"  DDRN_EDITABLE_ROOT names {root}")
    if root and module.is_relative_to(root):
        print(f"{module} is under {root}, so this install still reads a tree and is not a release")
        return 1
    if not module.is_relative_to(sys.prefix):
        print(f"{module} is outside {sys.prefix}, so the environment does not hold the code")
        return 1
    print("  the code is in the environment, and no tree of the user answers for it")

    # A console script exists only because an installer wrote it. The wheel
    # came out of the archive, and it goes through the same installer as one
    # that the index served.
    entry_points = metadata.distribution("packaged-app").entry_points
    print(f"entry points {sorted(f'{ep.name}={ep.value}' for ep in entry_points)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
