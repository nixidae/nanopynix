"""``pynix env shell``, the counterpart of ``nix env shell`` and ``nix shell``."""

from __future__ import annotations

from typing import override

from libpynix import opt, pos
from pynix import _impl
from pynix._nix_options import file_option
from pynix._settings import ConfiguredCommand, PynixCommand, print_build_logs_option, store_option, verbosity_option


class Shell(ConfiguredCommand):
    """Run a shell in which the named packages are available

    Each package's bin directory goes on PATH, with the packages it
    propagates. Without --file, each installable is a flake reference;
    with --file, each is an attribute path in that file.

    Examples:
      pynix env shell nixpkgs#jq nixpkgs#hello
      pynix env shell nixpkgs#git --command git log -- README.md
      pynix env shell --file '<nixpkgs>' jq --command jq --version"""

    installables: list[str] = pos(help="Flake references, or attribute paths of --file.")

    file: str | None = file_option()

    store: str = store_option("Store URI to build with.")

    verbosity: str | None = verbosity_option()

    print_build_logs: bool = print_build_logs_option()

    command: list[str] = opt(
        short="c",
        remainder=True,
        help="Command and arguments to run, instead of $SHELL. Takes every argument after it.",
    )

    @override
    async def run(self) -> None:
        await _impl.env.run_shell(self)


class Env(PynixCommand):
    """Run programs in an environment of packages"""

    subcommands = (Shell,)
