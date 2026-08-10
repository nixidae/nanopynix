"""Report what the environment of a uv project holds, from inside it.

`check-uv.nix` runs this with the `bin/python` of the environment that the
graph built. Nothing here adds a path: an import that works proves that the
environment finds its own `site-packages`.

**Two assertions carry the proof, and both are about `uv.lock`.** The lock file
resolves `rich`, and it also pins `installer` in the `ddrn` dependency group.
The selection enabled no group, so the planner must install the first and must
not install the second. A resolver that installed every `[[package]]` of the
lock file would fail the second assertion.
"""

from __future__ import annotations

import re
import sys
from importlib import metadata

from rich.console import Console

#: What `uv.lock` resolves for the selection that `uv.nix` makes: `rich`, and
#: the three packages that the edges of `rich` reach.
EXPECTED = {"rich", "markdown-it-py", "mdurl", "pygments"}

#: In the same lock file, in the `ddrn` dependency group. The graph installs
#: its wheels with this tool, and the environment must not hold it.
EXCLUDED = "installer"


def canonical(name: str) -> str:
    """The name, as PEP 503 normalises it.

    **A distribution does not have to spell its name the way the index does.**
    `uv.lock` calls this project `pygments`, and the `Name` of its metadata is
    `Pygments`. `packaging.utils.canonicalize_name` is the reference for this,
    and it is not in the environment that the graph built, so this is the rule
    of PEP 503 itself.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def main() -> int:
    print(f"interpreter  {sys.executable}")
    print(f"prefix       {sys.prefix}")
    # `rich` records no `__version__`, so ask the metadata that the installer
    # wrote. That is the same file which proves the install happened.
    print(f"rich         {metadata.version('rich')}")

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

    # **A console script exists only because an installer wrote it.** An
    # unpacked wheel carries `entry_points.txt` and no `bin/` entry, so this
    # line proves that the graph bootstrapped `pypa/installer` out of the same
    # lock file and then ran it.
    entry_points = metadata.distribution("pygments").entry_points
    print(f"entry points {sorted(f'{ep.name}={ep.value}' for ep in entry_points)}")

    # `rich` renders, so the whole dependency chain imports:
    # rich -> markdown-it-py -> mdurl, and rich -> pygments.
    console = Console(file=sys.stdout, force_terminal=False, width=40)
    console.print("[bold]rich renders[/bold], through markdown-it-py and pygments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
