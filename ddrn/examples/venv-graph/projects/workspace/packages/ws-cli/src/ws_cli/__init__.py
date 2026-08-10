"""The application member of the workspace.

It reads `ws-core`, which is the other member, and `rich`, which is an
artefact of the index. One environment therefore holds two editable members
and the closure of one registry package.
"""

from __future__ import annotations

from rich.console import Console

# Only the graph puts the other member on the path, and it does that by
# installing both members of the workspace as PEP 660 editables.
from ws_core import banner  # pyright: ignore[reportMissingImports, reportUnknownVariableType] -- see above


def greet() -> str:
    """What this build of the member says, on top of the other member."""
    return f"ws-cli, on top of {banner()}"


def main() -> int:
    """The console script that `[project.scripts]` names."""
    Console(force_terminal=False, width=72).print(greet())
    return 0
