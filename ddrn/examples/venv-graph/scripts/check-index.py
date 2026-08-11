"""Report what an environment built out of a registry of this machine holds.

`check-index.nix` runs this with the `bin/python` of the environment that the
graph built. This script imports no package of that environment, for the reason
that `scripts/check-git.py` gives: only the graph installs them.

**The proof is the shape of the location.** `uv.lock` puts the directory of the
registry on the source, and the name of the file on the row:

    [[package]]
    name = "stocked"
    source = { registry = "index" }
    wheels = [ { path = "stocked-1.0.0-py3-none-any.whl" } ]

So the file is `index/stocked-1.0.0-py3-none-any.whl`, and it is neither
`stocked-1.0.0-py3-none-any.whl` of the project nor an address that a fetch
could reach. uv joins the same three parts in `to_registry_wheel`, and
`ddrn/PACKAGING-FACTS.md` names the line.

Two facts show up here:

1. Both distributions are present, at the version that each file carries.
2. `flit_core` is not. It builds `crated`, and `uv.lock` pins it in the `ddrn`
   dependency group that the selection does not enable. `installer` is in that
   same group.
"""

from __future__ import annotations

import re
import sys
from importlib import metadata

#: What `uv.lock` resolves for the selection that `uv.nix` makes. Neither
#: distribution declares a dependency, so the closure is these two.
EXPECTED = {"stocked", "crated"}

#: In the same lock file, in the `ddrn` dependency group. `flit-core` builds
#: `crated`, and the environment must not hold it.
EXCLUDED = {"flit-core", "installer"}

VERSION = "1.0.0"


def canonical(name: str) -> str:
    """The name, as PEP 503 normalises it."""
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
        if dist.version != VERSION:
            print(f"{name} says {dist.version}, and both files of index/ carry {VERSION}")
            return 1

    if found != EXPECTED:
        print(f"the environment holds {sorted(found)}, and uv.lock resolves {sorted(EXPECTED)}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
