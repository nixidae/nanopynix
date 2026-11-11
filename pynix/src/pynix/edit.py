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
from pynix._settings import ConfiguredCommand, store_option


class Edit(ConfiguredCommand):
    """Open the Nix expression of a Nix package in $EDITOR

    Opens the file of meta.position at its line, as nix edit does. $EDITOR
    defaults to cat. emacs, nano, vim and kak get the line as +LINE.

    Examples:
      pynix edit --flake nixpkgs#hello
      pynix edit --file '<nixpkgs>' --attr hello"""

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
        await _impl.edit.run_edit(self)
