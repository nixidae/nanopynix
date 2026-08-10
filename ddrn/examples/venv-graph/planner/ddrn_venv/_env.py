"""Reading what the Nix half set.

This module holds no import of nanopynix, so `_resolve` stays a module about
packaging and nothing else.
"""

from __future__ import annotations

import os


def env(name: str) -> str:
    """Read one environment variable that the Nix half must set."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is empty: the Nix half must set it")
    return value
