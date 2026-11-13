"""``pynix`` as the interpreter of a script, as ``nix`` is (``src/libutil/args.cc:283``).

A script that starts with ``#!/usr/bin/env pynix`` reaches ``pynix`` as its
first argument. Its ``#! pynix`` lines are the command, and the script and its
arguments follow them. Relative paths of the command name files from the
directory of the script (``commandBaseDir``). nixidae/nixidae#1039.

Standard library only: ``main`` calls this before it imports what a command
needs.
"""

from __future__ import annotations

import contextvars
import os
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

#: The characters that start a line Nix reads past the first (``args.cc:300``).
_COMMENT_CHARS = "#/\\%@*-("
# `(:? |$)` as Nix writes it (`args.cc:308`): an optional ':' and a space, or the end.
_DIRECTIVE = re.compile(r"#!\s*pynix(:? |$)(.*)")
_RESERVED = "$*~<>|;()[]{}'\"\\"
_WHITESPACE = " \t\n\r"

_base_dir: contextvars.ContextVar[Path | None] = contextvars.ContextVar("pynix_command_base_dir", default=None)


class ShebangError(ValueError):
    """A ``#! pynix`` line that Nix's parser refuses."""


@dataclass(frozen=True, slots=True)
class ScriptArgv:
    """The command line after the script's ``#! pynix`` lines are read."""

    argv: list[str]
    base_dir: Path | None
    """The directory of the script, or ``None`` when no script was given."""


def command_base_dir() -> Path | None:
    """The directory that relative paths of the command name files from.

    ``None`` is the working directory, outside a script.
    """
    return _base_dir.get()


@contextmanager
def command_base_dir_of(base_dir: Path | None) -> Iterator[None]:
    """Name files of the command from *base_dir* while the block runs."""
    token = _base_dir.set(base_dir)
    try:
        yield
    finally:
        _base_dir.reset(token)


def base_argument() -> str | None:
    """The ``base`` that huggorm's parsers take: the base directory, or ``None`` for the working one."""
    base = command_base_dir()
    return None if base is None else str(base)


def rebased(path: str) -> str:
    """*path* from the command's base directory, as ``absPath(path, baseDir)`` gives it."""
    base = command_base_dir()
    if base is None or Path(path).is_absolute() or path.startswith("~"):
        return path
    return str(base / path)


def expand_script(argv: Sequence[str]) -> ScriptArgv:
    """*argv* with the command of the script in ``argv[0]``, when it names one.

    A first argument that cannot be read, or that does not start with ``#!``,
    leaves *argv* as it is.
    """
    if not argv:
        return ScriptArgv(list(argv), None)
    script = argv[0]
    try:
        with Path(script).open("rb") as stream:
            if stream.read(2) != b"#!":
                return ScriptArgv(list(argv), None)
            lines = stream.read().split(b"\n")[1:]
    except OSError:
        return ScriptArgv(list(argv), None)
    content = ""
    for raw in lines:
        line = os.fsdecode(raw)
        if not line or line[0] not in _COMMENT_CHARS:
            break
        match = _DIRECTIVE.fullmatch(line.rstrip(_WHITESPACE))
        if match is not None:
            content += f"{match[2]}\n"
    words = parse_shebang_content(content)
    return ScriptArgv([*words, script, *argv[1:]], Path(script).parent.absolute())


def parse_shebang_content(text: str) -> list[str]:
    """The arguments of the ``#! pynix`` lines, as ``parseShebangContent`` reads them.

    Whitespace separates words. A double backtick quotes a word, and inside it
    three or more backticks are one backtick fewer (``args.cc:107``).
    """
    words: list[str] = []
    index = 0
    while index < len(text):
        index = _unquoted(text, index, words)
    return words


def _unquoted(text: str, start: int, words: list[str]) -> int:
    acc = ""
    index = start
    while index < len(text):
        char = text[index]
        if char in _WHITESPACE:
            break
        if char == "`":
            if text.startswith("``", index):
                # Nix drops *acc*: foo``bar`` gives the one word bar.
                return _quoted(text, index + 2, words)
            raise ShebangError("single backtick is not a supported syntax in the nix shebang.")
        if char in _RESERVED:
            raise ShebangError(
                f"unsupported unquoted character in nix shebang: {char}. Use double backticks to escape?"
            )
        if char == "#" and not acc:
            raise ShebangError("unquoted nix shebang argument cannot start with #. Use double backticks to escape?")
        acc += char
        index += 1
    if acc:
        words.append(acc)
    return index + 1


def _quoted(text: str, start: int, words: list[str]) -> int:
    quoted = ""
    index = start
    while True:
        rest = text[index:]
        if not rest:
            raise ShebangError("unterminated quoted string in nix shebang")
        if rest.startswith(" ``") and (len(rest) == 3 or rest[3] != "`"):  # noqa: PLR2004 -- a space and two backticks
            words.append(quoted)
            return index + 3
        if rest.startswith("``") and (len(rest) == 2 or rest[2] != "`"):  # noqa: PLR2004 -- two backticks
            words.append(quoted)
            return index + 2
        if rest.startswith("```"):
            index += 1
            while index < len(text) and text[index] == "`":
                quoted += "`"
                index += 1
            continue
        quoted += rest[0]
        index += 1
