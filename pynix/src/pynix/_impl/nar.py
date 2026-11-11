"""The implementation of ``pynix nar``: ``ls.cc``, ``cat.cc`` and ``dump-path.cc`` of Nix.

Nix writes and parses the NAR, and lists it as JSON, through huggorm. The text
listing of ``MixLs::listText`` is ported here. The accessor is a blocking
binding, so each call runs in a worker thread.

Two differences from Nix. ``nix nar ls`` reads a NAR lazily through its file
descriptor, and this reads the whole file into memory. A failure that is
pynix's own, such as ``nar cat`` of a directory, prints ``Error:``, where Nix
prints ``error:``.
"""

from __future__ import annotations

import sys
from typing import override

import anyio
import structlog
from huggorm import FileType, Sink, SourceAccessor, Stat, dump_path, nar_accessor

from pynix._util import error_exit
from pynix.nar import Cat, DumpPath, Ls, Pack

logger = structlog.get_logger("pynix.nar")


class _Stdout(Sink):
    """Nix's NAR, straight to standard output."""

    @override
    def write(self, data: bytes) -> None:
        sys.stdout.buffer.write(data)


def _canon(path: str) -> str:
    """``CanonPath``: absolute, with no empty, ``.`` or ``..`` component."""
    parts: list[str] = []
    for part in path.split("/"):
        if part == "..":
            if parts:
                parts.pop()
        elif part not in {"", "."}:
            parts.append(part)
    return "/" + "/".join(parts)


def _child(parent: str, name: str) -> str:
    return parent.rstrip("/") + "/" + name


def _mode(stat: Stat) -> str:
    if stat.type() == FileType.REGULAR:
        return "-r-xr-xr-x" if stat.is_executable() else "-r--r--r--"
    if stat.type() == FileType.SYMLINK:
        return "lrwxrwxrwx"
    return "dr-xr-xr-x"


def _list_text(accessor: SourceAccessor, path: str, *, recursive: bool, long: bool, directory: bool) -> list[str]:
    """``MixLs::listText``: the lines ``nix nar ls`` prints."""
    lines: list[str] = []

    def show_file(current: str, relative: str) -> None:
        stat = accessor.lstat(current)
        if long:
            line = f"{_mode(stat)} {stat.file_size() or 0:20d} {relative}"
            if stat.type() == FileType.SYMLINK:
                line += " -> " + accessor.read_link(current)
            lines.append(line)
        else:
            lines.append(relative)
        if recursive and stat.type() == FileType.DIRECTORY:
            visit(stat, current, relative, show_directory=False)

    def visit(stat: Stat, current: str, relative: str, *, show_directory: bool) -> None:
        if stat.type() == FileType.DIRECTORY and not show_directory:
            for name in sorted(accessor.read_directory(current)):
                show_file(_child(current, name), relative + "/" + name)
        else:
            show_file(current, relative)

    root = accessor.lstat(path)
    base = "." if root.type() == FileType.DIRECTORY else path.rsplit("/", 1)[-1]
    visit(root, path, base, show_directory=directory)
    return lines


async def run_pack(command: Pack) -> None:
    """The body of :meth:`pynix.nar.Pack.run`: ``CmdDumpPath2::run``."""
    await anyio.to_thread.run_sync(dump_path, command.path, _Stdout())
    sys.stdout.buffer.flush()


async def run_dump_path(command: DumpPath) -> None:
    """The body of :meth:`pynix.nar.DumpPath.run`: ``CmdNarDumpPath::run``."""
    logger.warning("'nix nar dump-path' is a deprecated alias for 'nix nar pack'")
    await run_pack(command)


async def run_ls(command: Ls) -> None:
    """The body of :meth:`pynix.nar.Ls.run`: ``MixLs::list``."""
    if command.json and command.directory:
        error_exit("'--directory' is useless with '--json'")
    accessor = await anyio.to_thread.run_sync(nar_accessor, command.nar)
    path = _canon(command.path)
    if command.json:
        sys.stdout.write(await anyio.to_thread.run_sync(accessor.list_nar, path, command.recursive))
        sys.stdout.write("\n")
        return
    lines = await anyio.to_thread.run_sync(
        lambda: _list_text(accessor, path, recursive=command.recursive, long=command.long, directory=command.directory)
    )
    for line in lines:
        sys.stdout.write(line)
        sys.stdout.write("\n")


async def run_cat(command: Cat) -> None:
    """The body of :meth:`pynix.nar.Cat.run`: ``CmdCatNar::run``."""
    accessor = await anyio.to_thread.run_sync(nar_accessor, command.nar)
    path = _canon(command.path)
    stat = await anyio.to_thread.run_sync(accessor.maybe_lstat, path)
    if stat is None or stat.type() != FileType.REGULAR:
        error_exit(f"NAR does not contain regular file '{command.path}'")
    sys.stdout.buffer.write(await anyio.to_thread.run_sync(accessor.read_file, path))
    sys.stdout.buffer.flush()
