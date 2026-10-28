"""Shared tree-sitter-nix grammar setup for pynix's REPL and language server."""

from __future__ import annotations

from typing import Protocol, cast

import tree_sitter_nix
from tree_sitter import Language, Parser, Tree


class _Grammar(Protocol):
    path: str | None


class _GrammarConfig(Protocol):
    grammars: list[_Grammar]


NIX_LANGUAGE = Language(cast("int", tree_sitter_nix.language()))


def _grammar_directory() -> str:
    config: _GrammarConfig | None = tree_sitter_nix.config()
    if config is None:
        raise RuntimeError("tree-sitter-nix did not provide its grammar configuration")
    path: str | None = config.grammars[0].path
    if path is None:
        raise RuntimeError("tree-sitter-nix did not provide its grammar path")
    return path


NIX_GRAMMAR_PATH: str = _grammar_directory()


def parse_nix(source: str) -> Tree:
    """Parse one Nix source string with the shared tree-sitter-nix grammar."""
    return Parser(NIX_LANGUAGE).parse(source.encode())
