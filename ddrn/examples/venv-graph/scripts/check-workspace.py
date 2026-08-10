"""Report what the environment of one selection of a uv workspace holds.

`check-workspace.nix` runs this with the `bin/python` of the environment that
the graph built, and then probes each member that the selection installs. This
script therefore imports no member: a selection decides which members exist,
and an import at the top of this file would name a package that is absent.

The two together prove what a workspace adds over a single project:

1. **One lock file resolves to more than one environment.** `uv.lock` records
   no selection, so the caller names the members to install.
   `check-workspace.nix` builds two selections of this one workspace.
2. **Two local members install together.** Each one is a PEP 660 editable of
   its own directory, so `uvEditables` of `uv.nix` carries two entries and the
   graph holds a node for each.
3. **A member reads the other member.** `ws-cli` imports `ws_core`, and both
   redirect at a directory outside the store.

`DDRN_EXPECT` names the distributions that this selection must hold, as PEP 503
names, separated by a space.
"""

from __future__ import annotations

import os
import re
import sys
from importlib import metadata

#: In the `ddrn` dependency group of the workspace root, which no selection
#: enables. Every one of these builds the environment, and none is in it.
EXCLUDED = {"hatchling", "installer", "editables", "packaging", "pathspec", "pluggy", "trove-classifiers"}


def canonical(name: str) -> str:
    """The name, as PEP 503 normalises it.

    `uv.lock` calls a project `pygments`, and the `Name` of its metadata is
    `Pygments`. `packaging` is not in this environment, so this is the rule of
    PEP 503 itself.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def main() -> int:
    expected = {canonical(name) for name in os.environ["DDRN_EXPECT"].split()}

    print(f"prefix       {sys.prefix}")
    print(f"selection    {' '.join(sorted(expected))}")

    print("distributions")
    found: set[str] = set()
    for dist in sorted(metadata.distributions(), key=lambda item: item.name or ""):
        name = dist.name or "?"
        found.add(canonical(name))
        print(f"  {name}=={dist.version}")

    if found != expected:
        print(f"the environment holds {sorted(found)}, and this selection resolves {sorted(expected)}")
        return 1
    leaked = sorted(found & EXCLUDED)
    if leaked:
        print(f"{leaked} builds this environment and must not be in it")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
