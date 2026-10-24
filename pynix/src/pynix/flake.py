from __future__ import annotations

from typing import override

from libpynix import opt, pos
from pynix import _impl
from pynix._settings import ConfiguredCommand, PynixCommand, store_option


class Show(ConfiguredCommand):
    """Show the outputs provided by a flake"""

    flake_ref: str = pos(help="Flake reference (e.g. '.#' or 'nixpkgs#').")

    attrpath: str | None = opt(
        None,
        short="A",
        help="Dot-separated attribute path within the flake outputs to start from.",
    )

    store: str = store_option("Store URI to evaluate with.")

    @override
    async def run(self) -> None:
        await _impl.flake.run_show(self)


class Metadata(ConfiguredCommand):
    """Show locked flake metadata"""

    flake_ref: str = pos(help="Flake reference (e.g. '.' or 'nixpkgs').")

    store: str = store_option("Store URI to evaluate with.")

    @override
    async def run(self) -> None:
        await _impl.flake.run_metadata(self)


class Info(ConfiguredCommand):
    """Alias for flake metadata"""

    flake_ref: str = pos(help="Flake reference (e.g. '.' or 'nixpkgs').")

    store: str = store_option("Store URI to evaluate with.")

    @override
    async def run(self) -> None:
        await _impl.flake.run_info(self)


class Lock(ConfiguredCommand):
    """Create the missing entries of a flake's lock file

    An entry that is in the lock file already stays as it is. Nix reports
    each entry that it adds on stderr.

    Examples:
      pynix flake lock
      pynix flake lock ./path/to/flake"""

    flake_ref: str = pos(default=".", help="Flake reference (default: '.').")

    store: str = store_option("Store URI to fetch the inputs into.")

    @override
    async def run(self) -> None:
        await _impl.flake.run_lock(self)


class Update(ConfiguredCommand):
    """Update the inputs of a flake's lock file to their newest revisions

    With no input named, this updates every input. Nix reports each input
    that moves on stderr, with the old and the new revision.

    Examples:
      pynix flake update
      pynix flake update nixpkgs
      pynix flake update mid/leaf --flake ./path/to/flake"""

    inputs: list[str] = pos(help="Inputs to update, such as 'nixpkgs' or 'a/b'. Omit to update every input.")

    flake: str = opt(".", help="Flake reference whose lock file to update (default: '.').")

    store: str = store_option("Store URI to fetch the inputs into.")

    @override
    async def run(self) -> None:
        await _impl.flake.run_update(self)


class Flake(PynixCommand):
    """Inspect and manage Nix flakes"""

    subcommands = (Show, Metadata, Info, Lock, Update)
