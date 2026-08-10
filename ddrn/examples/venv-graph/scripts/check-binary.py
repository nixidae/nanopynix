"""Report what the environment of a binary-wheel uv project holds.

`check-binary.nix` runs this with the `bin/python` of the environment that the
graph built, and then probes the compiled extension. This script therefore
imports no package of the environment: `zmq` resolves for no type checker
here, because only the graph installs it.

The two together prove the one path that every other project of `../projects`
leaves untouched:

**A wheel that holds compiled code needs `auto-patchelf`.** A manylinux wheel
links against the loader and the libraries of a generic Linux, and NixOS has
neither at the path that the wheel names. `graph.nix` gives a node whose
platform tag is not `any` the `patchelfInputs` attributes, and the install
script runs `auto-patchelf` over the output. An import of the extension is the
proof, because an unpatched `.so` resolves no `libstdc++` and raises
`ImportError` before any code of the package runs.

`DDRN_EXPECT` names the distributions that this environment must hold, as PEP
503 names, separated by a space.
"""

from __future__ import annotations

import os
import re
import sys
from importlib import metadata


def canonical(name: str) -> str:
    """The name, as PEP 503 normalises it."""
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
