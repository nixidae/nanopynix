"""A line after an error of Nix that says how to get past it.

pynix prints the error of Nix unchanged and adds the hint on its own line.
An error of huggorm carries no errno, so the text of Nix is the only signal.
"""

from __future__ import annotations

import re

import huggorm.errors

#: `linux-derivation-builder.cc:498`: `SysError("creating cgroup %s", ...)`.
_BUILD_CGROUP = re.compile(r'error: creating cgroup "[^"]*/nix-build-[^"]*": ')

#: `linux-derivation-builder.cc:360`, in the child that sets up the sandbox.
#: `derivation-builder.cc:684` adds the trace in the parent, and `readError`
#: there drops the type, so the error is a plain `NixError`.
_SANDBOX_PATH = re.compile(
    r'… while setting up the build environment\n\s*\n\s*error: getting attributes of path "([^"]*)": Permission denied$'
)

USE_CGROUPS_HINT = (
    "hint: Nix runs this build in a cgroup, because `use-cgroups` is true or the derivation needs `uid-range`. "
    "This user cannot create a cgroup here. Build through a daemon that runs as root, "
    "or set `NIX_CONFIG='use-cgroups = false'`."
)


def sandbox_path_hint(path: str) -> str:
    # Not `path?`: an optional path skips only ENOENT and ENOTDIR (`:356`).
    return (
        f"hint: {path} is in `sandbox-paths` or `extra-sandbox-paths`, and this user cannot read it. "
        "Remove it from that setting, or build through a daemon that runs as root."
    )


def hint_for(exc: huggorm.errors.NixError) -> str | None:
    """The hint for *exc*, or ``None``. nixidae/nixidae#1037, nixidae/nixidae#1072."""
    if isinstance(exc, huggorm.errors.SysError) and _BUILD_CGROUP.match(exc.message):
        return USE_CGROUPS_HINT
    if (match := _SANDBOX_PATH.search(exc.message)) is not None:
        return sandbox_path_hint(match[1])
    return None
