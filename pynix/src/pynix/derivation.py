from __future__ import annotations

from typing import override

from pynix import _impl
from pynix._nix_options import (
    arg_option,
    argjson_option,
    args_json_option,
    argstr_option,
    attr_option,
    file_option,
    flake_option,
    impure_option,
)
from pynix._settings import ConfiguredCommand, PynixCommand, store_option


class Show(ConfiguredCommand):
    """Show the contents of a Nix derivation

    Examples:
      pynix derivation show --file default.nix --attr hello
      pynix derivation show --flake .#hello
      pynix derivation show --flake nixpkgs#python3Packages.requests"""

    file: str | None = file_option()

    arg: list[tuple[str, str]] = arg_option()

    argstr: list[tuple[str, str]] = argstr_option()

    argjson: list[tuple[str, str]] = argjson_option()

    args_json: str | None = args_json_option()

    attr: str | None = attr_option()

    flake: str | None = flake_option()

    impure: bool = impure_option()

    store: str = store_option("Store URI to use.")

    @override
    async def run(self) -> None:
        await _impl.derivation.run_show(self)


class Derivation(PynixCommand):
    """Inspect and manipulate Nix derivations"""

    subcommands = (Show,)
