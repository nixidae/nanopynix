"""``pynix sbom``: a Software Bill of Materials for a package."""

from __future__ import annotations

from typing import Literal, override

from libpynix import opt
from pynix import _impl
from pynix._nix_options import attr_option, file_option, flake_option
from pynix._settings import ConfiguredCommand, print_build_logs_option, store_option, verbosity_option


class Sbom(ConfiguredCommand):
    """Build a package, and print a Software Bill of Materials for it

    The SBOM lists the runtime closure of the default outputs, with the
    license, source, hashes and patches of each package. --buildtime adds
    the build closure. The JSON goes to stdout.

    Examples:
      pynix sbom --flake nixpkgs#hello
      pynix sbom --file '<nixpkgs>' --attr hello --format spdx
      pynix sbom --flake .#default --buildtime"""

    file: str | None = file_option()

    attr: str | None = attr_option()

    flake: str | None = flake_option()

    store: str = store_option("Store URI to build with.")

    verbosity: str | None = verbosity_option()

    print_build_logs: bool = print_build_logs_option()

    format: Literal["cyclonedx", "spdx"] = opt(
        "cyclonedx",
        help="CycloneDX 1.7 JSON, or SPDX 2.3 JSON. SPDX takes its creation time from SOURCE_DATE_EPOCH when it is set.",
    )

    buildtime: bool = opt(False, help="Also list the build closure: compilers, build tools and sources.")

    @override
    async def run(self) -> None:
        await _impl.sbom.run_sbom(self)
