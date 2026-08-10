"""The project that a git source of `uv.lock` stands for.

**A git checkout is a directory, so no archive stands between the tree and the
wheel.** One PEP 517 `build_wheel` call over `backend/` gives the install, and
that install holds this file. So the environment follows no tree of the user,
which is the difference between this project and `demo-app` as an editable.
"""

from __future__ import annotations


def greet() -> str:
    return "hello from the project below the top of its tree"


def main() -> None:
    print(greet())
