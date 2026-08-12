"""What `_speedup.c` gives, for a reader that compiles nothing.

A type checker reads no shared object, so the interface of a compiled module
lives in a stub. PEP 484 defines this file, and `build_wheel` of the fixture
backend ships it beside the module that it describes.
"""

from __future__ import annotations

def answer() -> int: ...
def library() -> str: ...
