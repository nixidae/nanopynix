"""``pynix run``, the counterpart of ``nix run``.

Everything after ``--`` goes to the program, as in ``pynix develop``.
"""

from __future__ import annotations

from typing import override

from libpynix import opt, pos
from pynix import _impl
from pynix._nix_options import attr_option, file_option, flake_option
from pynix._settings import ConfiguredCommand, print_build_logs_option, store_option, verbosity_option


class Run(ConfiguredCommand):
    """Build an app or a package, and run it

    A flake fragment selects as nix run does: apps.<system> first, then
    packages.<system> and legacyPackages.<system>. A package runs
    bin/<meta.mainProgram>, or bin/<pname> when it sets none.

    Examples:
      pynix run --flake nixpkgs#hello
      pynix run --flake .# -- --version
      pynix run --file '<nixpkgs>' --attr coreutils --bin ls -- -l"""

    file: str | None = file_option()

    attr: str | None = attr_option()

    flake: str | None = flake_option(search="app")

    store: str = store_option("Store URI to build with.")

    verbosity: str | None = verbosity_option()

    print_build_logs: bool = print_build_logs_option()

    bin: str | None = opt(
        None,
        help=(
            "Run this program from the directory of the resolved program, instead of the resolved program. "
            "A name that is not there fails with the names that are."
        ),
    )

    #: Everything after ``--``.
    args: list[str] = pos(help="Arguments for the program, after --.")

    @override
    async def run(self) -> None:
        await _impl.run.run_run(self)
