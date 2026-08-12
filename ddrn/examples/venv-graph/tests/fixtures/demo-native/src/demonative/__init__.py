"""A project that ships C code, for the `buildTools` argument of the example.

`_speedup.c` beside this file gives the whole reason. The import below fails
when the wheel holds no compiled module, so a build that ran no compiler
cannot pass for one that did.
"""

from __future__ import annotations

from demonative._speedup import answer as answer, library as library


def greet() -> str:
    """What the test reads back out of the environment."""
    return f"hello from C, which gives {answer()} and links zlib {library()}"
