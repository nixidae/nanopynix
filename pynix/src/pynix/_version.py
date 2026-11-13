"""What ``pynix --version`` prints."""

from __future__ import annotations

import importlib.metadata

import huggorm


def version_document() -> dict[str, str]:
    """The version of pynix and of the Nix it links."""
    return {"pynix": importlib.metadata.version("pynix"), "nix": huggorm.nix_version()}


def version_line() -> str:
    """``nix --version`` prints ``nix (Nix) 2.35.2`` (``shared.cc:341``); pynix adds its own version."""
    document = version_document()
    return f"pynix {document['pynix']} (Nix) {document['nix']}"
