"""Link the programs of one package out of an environment, and nothing else.

`graph.nix` runs this as the builder of a node of kind `application`.

**An environment is not a program.** `make-venv.py` writes a real virtual
environment, so the result carries `bin/python`, `bin/activate` and
`pyvenv.cfg`, and it carries the console script of every package that the
environment holds. A NixOS configuration that puts such a path in
`environment.systemPackages` puts all of that on the PATH of the user, and
`bin/python` of the environment then shadows the interpreter of the system.

This node gives the other shape: **the tree of one package, pointing at the
environment**. The output holds `bin/<the program>` and whatever else that one
package installed outside `site-packages`, and each entry is a symbolic link
into the environment. The interpreter, the activation scripts and the programs
of every other package stay behind.

**The closure does not shrink, and that is not what this is for.** Each link
names a path inside the environment, so the environment, the interpreter and
every member stay in the runtime closure. What the output changes is the PATH
surface.

**The approach comes from `pyproject.nix`.** Its `build/util/mk-application.py`
reads the `$out` of one package as a template of the shape, and links the same
paths out of the environment. This is the same idea, and the two skip rules are
the same:

- `nix-support`, which is metadata of a build and not a part of the program.
- `lib/<python>/site-packages`, which is the code. The environment holds the
  code already, and a second copy of the tree of one package would shadow it.

The environment gives:
    out    the output path
    venv   the environment, which is the output of the node of kind `venv`
    base   the output of the node that installed the package, as the template
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: The one directory name to skip at the top of the tree.
METADATA = "nix-support"


def skipped(relative: Path, site: Path) -> bool:
    """Whether the template offers this path to the environment.

    Two rules, and `pyproject.nix` states both. `nix-support` describes the
    build, and `site-packages` holds the code that the environment already
    merged.
    """
    return relative == Path(METADATA) or relative == site


def link(base: Path, venv: Path, out: Path, site: Path) -> int:
    """Walk the template, and link each path of it out of the environment.

    A path of the template that the environment does not hold is left out.
    `make-venv.py` drops a `.dist-info` of a member, for one, and a link to a
    path that no build wrote would be a broken link in the output.
    """
    written = 0
    for root, directories, files in os.walk(base):
        here = Path(root).relative_to(base)

        # `os.walk` reads this list to decide where it descends, so a name
        # removed from it is a subtree that this walk never enters.
        directories[:] = [name for name in directories if not skipped(here / name, site)]

        for name in sorted(files):
            relative = here / name
            target = venv / relative
            if not target.exists():
                continue
            destination = out / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(target)
            written += 1
    return written


def main() -> int:
    out = Path(os.environ["out"])  # noqa: SIM112 -- Nix names a derivation attribute in lower case
    venv = Path(os.environ["venv"])  # noqa: SIM112 -- as above
    base = Path(os.environ["base"])  # noqa: SIM112 -- as above
    site = Path(os.environ["site"])  # noqa: SIM112 -- as above

    out.mkdir(parents=True, exist_ok=True)
    written = link(base, venv, out, site)

    # **An application with no program is a build that succeeded and gave
    # nothing**, which is worse than a refusal. A library is the usual reason,
    # and the message names it.
    if written == 0:
        print(
            f"{base.name} installed no file outside {site}, so this "
            "application would hold nothing. A library has no program to link, "
            "and only a package with a console script or a data file makes an "
            "application.",
            file=sys.stderr,
        )
        return 1

    print(f"{written} links, out of {venv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
