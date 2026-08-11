"""Report how much of a derivation closure is a copy of the menu.

`measure.sh` pipes the path of every `.drv` of the closure into this script,
one for each line, and gives the prefix of the store that holds them. Each
planner carries the menu in `DDRN_INPUTS`, and there is one planner for each
source distribution at each level of the recursion. So the same menu appears
many times, and this script says how many times and at what cost.

**The paths arrive on stdin, and not as arguments.** The closure of one lock
file of 111 packages holds 1811 `.drv` files, and a command line of that many
store paths is longer than the kernel accepts.

**A store under a prefix answers with the logical path.** `nix path-info
--store /tmp/x` prints `/nix/store/...`, and the file is at
`/tmp/x/nix/store/...`. So the prefix has to come separately, and this script
joins the two. A script that read the printed path would read the store of the
machine and report a number that the cold run never produced.

**This reads the bytes of the file, and not the derivation.** A `.drv` is
ATerm, and the environment of it is a plain string inside that text, so the
size of the file is what a store pays. `nix derivation show` would parse the
same file and answer a different question.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: The attribute that carries the menu. A `.drv` that names it is a planner.
MARKER = b"DDRN_INPUTS"


def main(prefix: str, lines: list[str]) -> int:
    total = 0
    menu_bytes = 0
    menus = 0
    count = 0
    for line in lines:
        name = line.strip()
        if not name:
            continue
        count += 1
        path = Path(prefix + name) if prefix else Path(name)
        try:
            data = path.read_bytes()
        except OSError as error:
            print(f"    {path}: {error}", file=sys.stderr)
            return 1
        total += len(data)
        if MARKER in data:
            menus += 1
            menu_bytes += len(data)

    print(f"    {count} .drv files, {total} bytes")
    print(f"    {menus} of them carry a menu, {menu_bytes} bytes")
    if total:
        print(f"    the menus are {100 * menu_bytes // total}% of the derivation text")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "", sys.stdin.readlines()))
