from __future__ import annotations

from pathlib import Path
from typing import override

from libpynix import opt, pos
from pynix import _impl
from pynix._settings import ConfiguredCommand, PynixCommand


class Pack(ConfiguredCommand):
    """Serialise a path to stdout in NAR format"""

    path: Path = pos(help="Path to serialise.")

    @override
    async def run(self) -> None:
        await _impl.nar.run_pack(self)


class DumpPath(Pack):
    """Serialise a path to stdout in NAR format (deprecated alias for `nar pack`)"""

    @override
    async def run(self) -> None:
        await _impl.nar.run_dump_path(self)


class Ls(ConfiguredCommand):
    """Show information about a path inside a NAR file"""

    nar: Path = pos(help="NAR file to read.")

    path: str = pos(help="Path inside the NAR.")

    recursive: bool = opt(False, short="R", help="List subdirectories recursively.")

    long: bool = opt(False, short="l", help="Show detailed file information.")

    directory: bool = opt(False, short="d", help="Show directories rather than their contents.")

    json: bool = opt(False, help="Produce output in JSON format.")

    @override
    async def run(self) -> None:
        await _impl.nar.run_ls(self)


class Cat(ConfiguredCommand):
    """Print the contents of a file inside a NAR file on stdout"""

    nar: Path = pos(help="NAR file to read.")

    path: str = pos(help="Path of the file inside the NAR.")

    @override
    async def run(self) -> None:
        await _impl.nar.run_cat(self)


class Nar(PynixCommand):
    """Create or inspect NAR files"""

    subcommands = (Cat, DumpPath, Ls, Pack)
