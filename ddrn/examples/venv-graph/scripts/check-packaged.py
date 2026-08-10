"""Report what the environment of a packaged uv project holds.

`check-packaged.nix` runs this with the `bin/python` of the environment that
the graph built. It proves the three paths that `projects/packaged-app`
exercises and `projects/pure-app` does not:

1. **A PEP 660 editable of the project itself.** `uv init --package` writes
   `source = { editable = "." }`, so the lock file says the project installs
   from its own tree. `hatchling` builds it, and the install redirects at a
   directory outside the store.
2. **A backend with a dependency closure.** `build-system.requires` names
   `hatchling` alone, and `hatchling` needs four more packages to import.
   `uv.lock` holds those edges, and `ddrn_venv.build_requirements` walks them.
3. **A source distribution that a wheel could have satisfied.**
   `tool.uv.no-binary-package = ["mdurl"]` names one, so the planner takes the
   sdist and a PEP 517 build runs for a package that ships a perfectly good
   wheel.
"""

from __future__ import annotations

import re
import sys
from importlib import metadata
from typing import cast

import packaged_app  # pyright: ignore[reportMissingImports] -- only the graph installs this, from the tree of the project

#: What `uv.lock` resolves for the default selection. `packaged-app` is here
#: too, because a PEP 660 install writes a `.dist-info` like any other wheel.
EXPECTED = {"packaged-app", "rich", "markdown-it-py", "mdurl", "pygments"}

#: In the `ddrn` dependency group, which the selection does not enable. Every
#: one of these builds the environment, and none of them is in it.
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

    # **The editable reads a tree that is not in this environment.** The `.pth`
    # that `hatchling` wrote expands `$DDRN_EDITABLE_ROOT` when the interpreter
    # starts, so this line reports whichever tree the check derivation named.
    print("editable")
    greeting = cast("str", packaged_app.greet())  # pyright: ignore[reportUnknownMemberType] -- the import above is unresolved, so this call has no type
    print(f"  {greeting}")
    print(f"  loaded from {packaged_app.__file__}")

    # A console script exists only because an installer wrote it, and a PEP 660
    # wheel is a wheel: PEP 376 does not treat it apart.
    entry_points = metadata.distribution("packaged-app").entry_points
    print(f"entry points {sorted(f'{ep.name}={ep.value}' for ep in entry_points)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
