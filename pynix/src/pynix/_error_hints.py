"""A line after an error of Nix that says how to get past it.

pynix prints the error of Nix unchanged and adds the hint on its own line.
"""

from __future__ import annotations

import re

#: `linux-derivation-builder.cc:498`: `SysError("creating cgroup %s", ...)`.
#: A SysError of huggorm carries no errno, so the text is the only signal.
_BUILD_CGROUP = re.compile(r'error: creating cgroup "[^"]*/nix-build-[^"]*": ')

USE_CGROUPS_HINT = (
    "hint: Nix runs this build in a cgroup, because `use-cgroups` is true or the derivation needs `uid-range`. "
    "This user cannot create a cgroup here. Build through a daemon that runs as root, "
    "or set `NIX_CONFIG='use-cgroups = false'`."
)


def hint_for(message: str) -> str | None:
    """The hint for the plain *message* of a ``SysError``, or ``None``. nixidae/nixidae#1037."""
    if _BUILD_CGROUP.match(message):
        return USE_CGROUPS_HINT
    return None
