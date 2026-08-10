"""Report what an environment built out of two local files holds.

`check-vendored.nix` runs this with the `bin/python` of the environment that
the graph built. This script imports no package of that environment, for the
reason that `scripts/check-git.py` gives: only the graph installs them.

**The proof is that no artefact of these two packages has an address.**
`uv.lock` names each one by a path relative to the project:

    [[package]]
    name = "vendored"
    source = { path = "vendor/vendored-1.0.0-py3-none-any.whl" }
    wheels = [ { filename = "vendored-1.0.0-py3-none-any.whl", hash = "sha256:7353…" } ]

So nothing of either package can be downloaded. `vendored` is in the
environment because `uv.nix` copied the wheel of `vendor/` into the store, and
`archived` is there because the graph ran the PEP 517 backend over the archive
of `vendor/`.

Two facts show up here:

1. Both distributions are present, at the version that each file carries.
2. `flit_core` is not. It builds `archived`, and `uv.lock` pins it in the
   `ddrn` dependency group that the selection does not enable. `installer` is
   in that same group.
"""

from __future__ import annotations

import re
import sys
from importlib import metadata

#: What `uv.lock` resolves for the selection that `uv.nix` makes. Neither
#: distribution declares a dependency, so the closure is these two.
EXPECTED = {"vendored", "archived"}

#: In the same lock file, in the `ddrn` dependency group. `flit-core` builds
#: `archived`, and the environment must not hold it.
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
            print(f"{name} says {dist.version}, and both files of vendor/ carry {VERSION}")
            return 1

    if found != EXPECTED:
        print(f"the environment holds {sorted(found)}, and uv.lock resolves {sorted(EXPECTED)}")
        return 1

    # **The wheel arrived whole, and the archive was built.** One package came
    # out of a file that no build read, and the other came out of a PEP 517
    # `build_wheel` call over an archive. Both must import the same way.
    return 0


if __name__ == "__main__":
    sys.exit(main())
