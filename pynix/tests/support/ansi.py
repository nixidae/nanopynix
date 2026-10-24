"""Text with Nix's escape sequences taken out, as the `nix` CLI shows it to a pipe."""

from __future__ import annotations

import huggorm


def strip_ansi(text: str) -> str:
    return huggorm.filter_ansi_escapes(text, filter_all=True)
