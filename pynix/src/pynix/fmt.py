from __future__ import annotations

from typing import override

from libpynix import pos
from pynix import _impl
from pynix._nix_options import impure_option
from pynix._settings import ConfiguredCommand, print_build_logs_option, store_option, verbosity_option


class Fmt(ConfiguredCommand):
    """Reformat your code in the standard style

    Runs formatter.<system> of the flake in the current directory, as nix fmt
    does. The formatter gets the arguments, and PRJ_ROOT names the top of the
    flake. Its exit status is the exit status of pynix.

    Examples:
      pynix fmt
      pynix fmt -- --check ."""

    impure: bool = impure_option()

    store: str = store_option("Store URI to build with.")

    verbosity: str | None = verbosity_option()

    print_build_logs: bool = print_build_logs_option()

    #: Everything after ``--``.
    args: list[str] = pos(help="Arguments for the formatter, after --.")

    @override
    async def run(self) -> None:
        await _impl.fmt.run_fmt(self)
