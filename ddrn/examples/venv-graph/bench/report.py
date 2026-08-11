"""Report what the menu of one planner derivation holds.

`measure.sh` pipes `nix derivation show` into this script. The menu travels in
`DDRN_INPUTS`, which is one JSON document, so its size in bytes is the size of
the menu that the planner reads.

**The count of `.drv` files in the closure is not the count of artefacts.**
Each source distribution of the lock file also gets a planner of its own, at
every level of the recursion that `sdistPlannerDepth` allows. This script
reports both numbers, so a reader can tell the two apart.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def one_derivation(document: dict[str, Any]) -> dict[str, Any]:
    """The one derivation of a `nix derivation show` document.

    **Nix writes two shapes, and this reads both.** Nix 2.34.8 writes
    `{"version": 4, "derivations": {"<name>.drv": {...}}}`, and an older Nix
    writes `{"<store path>": {...}}` with no wrapper. A reader that assumed
    either one alone breaks on the other Nix, and this lab runs two.
    """
    derivations = document.get("derivations", document)
    return next(iter(derivations.values()))


def report(document: dict[str, Any]) -> None:
    """Print one line for each number that the menu gives."""
    drv = one_derivation(document)
    menu: str = drv["env"]["DDRN_INPUTS"]
    print(f"    {len(menu)} bytes of DDRN_INPUTS")

    inputs = json.loads(menu)
    artifacts: list[dict[str, Any]] = inputs["artifacts"]
    kinds: dict[str, int] = {}
    for artifact in artifacts:
        kinds[artifact["kind"]] = kinds.get(artifact["kind"], 0) + 1

    print(f"    {len(artifacts)} artefact rows")
    for kind, count in sorted(kinds.items()):
        print(f"      {count} {kind}")
    print(f"    {sum(1 for a in artifacts if a.get('plannerDrv'))} of them carry a planner")
    print(f"    {sum(1 for a in artifacts if a.get('path'))} of them name a file of this machine")


if __name__ == "__main__":
    report(json.load(sys.stdin))
