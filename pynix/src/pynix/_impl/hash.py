"""The implementation of ``pynix hash``: ``src/nix/hash.cc`` of Nix.

Nix parses, hashes and prints; this module only picks the format and the
algorithm from the flags. Nix's ``--sri``, ``--base64``, ``--base32`` and
``--base16`` each overwrite one variable, so the last one named wins. argparse
does not keep that order, so a command that names two of them is refused.
"""

from __future__ import annotations

import sys
from pathlib import Path as FsPath

import structlog
from huggorm import FileIngestionMethod, HashAlgorithm, HashFormat, aio

from pynix._util import error_exit
from pynix.hash import Convert, File, HashFormatName, HashPrinting, Path, ToBase

logger = structlog.get_logger("pynix.hash")


def _format(name: HashFormatName) -> HashFormat:
    """``parseHashFormat``, with its warning for the deprecated alias."""
    if name == "base32":
        logger.warning('"base32" is a deprecated alias for hash format "nix32".')
        return HashFormat.NIX32
    return HashFormat(name)


def _printed_format(command: HashPrinting, chosen: HashFormatName | None) -> HashFormat:
    flags = [
        (command.sri, HashFormat.SRI),
        (command.base64, HashFormat.BASE64),
        (command.base32, HashFormat.NIX32),
        (command.base16, HashFormat.BASE16),
    ]
    named = [hash_format for given, hash_format in flags if given]
    if len(named) + (chosen is not None) > 1:
        error_exit("name one hash format, not several")
    if chosen is not None:
        return _format(chosen)
    return named[0] if named else HashFormat.SRI


def _print(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.write("\n")


async def _hash_paths(
    paths: list[str], method: FileIngestionMethod, algorithm: HashAlgorithm, hash_format: HashFormat
) -> None:
    for path in paths:
        digest = await aio.hash_path(FsPath(path), method, algorithm)
        _print(digest.render(hash_format))


async def run_path(command: Path) -> None:
    """The body of :meth:`pynix.hash.Path.run`. ``--algo`` and ``--type`` are one variable in Nix."""
    algorithm = HashAlgorithm(command.algo if command.algo is not None else command.type)
    hash_format = _printed_format(command, command.format)
    await _hash_paths(command.paths, FileIngestionMethod(command.mode), algorithm, hash_format)


async def run_file(command: File) -> None:
    """The body of :meth:`pynix.hash.File.run`."""
    hash_format = _printed_format(command, None)
    await _hash_paths(command.paths, FileIngestionMethod.FLAT, HashAlgorithm(command.type), hash_format)


async def run_convert(command: Convert) -> None:
    """The body of :meth:`pynix.hash.Convert.run`: ``CmdHashConvert::run``."""
    algorithm = None if command.algo is None else HashAlgorithm(command.algo)
    expected = None if command.from_ is None else _format(command.from_)
    to = _format(command.to)
    for text in command.hashes:
        digest = await aio.parse_hash(text, algorithm)
        if expected is not None:
            parsed = await aio.parse_hash_format(text, algorithm)
            if parsed != expected:
                error_exit(f"input hash '{text}' has format '{parsed}', but '--from {expected}' was specified")
        _print(digest.render(to))


async def run_to_base(command: ToBase) -> None:
    """The body of :meth:`pynix.hash.ToBase.run`: ``CmdToBase::run``."""
    logger.warning(
        "The old format conversion subcommands of `nix hash` were deprecated in favor of `nix hash convert`."
    )
    algorithm = None if command.type is None else HashAlgorithm(command.type)
    to = _format(command.target)
    for text in command.strings:
        _print((await aio.parse_hash(text, algorithm)).render(to))
