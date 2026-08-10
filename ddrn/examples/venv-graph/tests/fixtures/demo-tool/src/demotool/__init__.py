"""A fixture built from a source distribution, by a backend of the same lock."""

from __future__ import annotations

BUILT_BY = "demoback"


def main() -> None:
    print(f"demo-tool was built by {BUILT_BY}")
