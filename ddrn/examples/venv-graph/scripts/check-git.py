"""Report what an environment built out of a git checkout holds.

`check-git.nix` runs this with the `bin/python` of the environment that the
graph built, and then imports `flit_core` from that same environment. This
script therefore imports no package of the environment: `flit_core` resolves
for no type checker here, because only the graph installs it.
`scripts/check-binary.py` leaves `zmq` alone for the same reason.

**The proof is that `uv.lock` names no file for this package.** A git source
carries no `wheels` table and no `sdist` table at all, so the menu that
`uv.nix` builds holds nothing to download for it:

    [[package]]
    name = "flit-core"
    version = "3.12.0"
    source = { git = "https://github.com/pypa/flit?subdirectory=flit_core&rev=3.12.0#1c8141…" }

So this environment can only hold `flit_core` because `uv.nix` fetched the
repository, and one PEP 517 `build_wheel` call ran over the `flit_core`
directory of it.

Two facts of that build show up here, and `check-git.nix` adds two more:

1. The version is `3.12.0`, which is the tag that `uv.lock` resolved. The
   metadata comes from the checkout, and no index answered for it.
2. The environment holds this one distribution and nothing else. `installer`
   is in the same lock file, in the `ddrn` dependency group, and the selection
   did not enable that group.
"""

from __future__ import annotations

import re
import sys
from importlib import metadata

#: What `uv.lock` resolves for the selection that `uv.nix` makes. `flit_core`
#: declares `dependencies = []`, so the closure is the one package.
EXPECTED = {"flit-core"}

#: In the same lock file, in the `ddrn` dependency group.
EXCLUDED = "installer"

#: The tag that `uv add` resolved, and the commit that it wrote beside it.
VERSION = "3.12.0"


def canonical(name: str) -> str:
    """The name, as PEP 503 normalises it.

    `uv.lock` says `flit-core`, and the metadata of the distribution says
    `flit_core`. `scripts/check-uv.py` gives the whole reason.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def main() -> int:
    print(f"interpreter  {sys.executable}")
    print(f"prefix       {sys.prefix}")

    # **The version comes out of the checkout, and not out of an index.** The
    # metadata that the installer wrote is the file that the build produced.
    version = metadata.version("flit_core")
    print(f"flit_core    {version}")
    if version != VERSION:
        print(f"the metadata says {version}, and uv.lock resolved the tag {VERSION}")
        return 1

    print("distributions")
    found: set[str] = set()
    for dist in sorted(metadata.distributions(), key=lambda item: item.name or ""):
        name = dist.name or "?"
        found.add(canonical(name))
        print(f"  {name}=={dist.version}")

    if found != EXPECTED:
        print(f"the environment holds {sorted(found)}, and uv.lock resolves {sorted(EXPECTED)}")
        return 1
    if EXCLUDED in found:
        print(f"'{EXCLUDED}' is a build tool of the 'ddrn' group, and the selection did not enable it")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
