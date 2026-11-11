from __future__ import annotations

from typing import ClassVar, Literal, override

from libpynix import opt, pos
from pynix import _impl
from pynix._settings import PynixCommand

#: The words of ``nix hash --type``, ``--algo`` and ``--format``.
#: ``base32`` is Nix's deprecated alias of ``nix32``.
HashAlgo = Literal["blake3", "md5", "sha1", "sha256", "sha512"]
HashFormatName = Literal["base16", "nix32", "base32", "base64", "sri"]


class HashPrinting(PynixCommand):
    """The four format flags that ``nix hash path`` and ``nix hash file`` share."""

    paths: list[str] = pos(help="Paths to hash.")

    type: HashAlgo = opt("sha256", help="Hash algorithm (`blake3`, `md5`, `sha1`, `sha256`, or `sha512`).")

    sri: bool = opt(False, help="Print the hash in SRI format.")

    base64: bool = opt(False, help="Print the hash in base-64 format.")

    base32: bool = opt(False, help="Print the hash in base-32 (Nix-specific) format.")

    base16: bool = opt(False, help="Print the hash in base-16 format.")


class Path(HashPrinting):
    """Print cryptographic hash of the NAR serialisation of a path"""

    algo: HashAlgo | None = opt(None, help="Hash algorithm (`blake3`, `md5`, `sha1`, `sha256`, or `sha512`).")

    mode: Literal["flat", "nar", "git"] = opt("nar", help="How to compute the hash of the input.")

    format: HashFormatName | None = opt(None, help="Hash format (`base16`, `nix32`, `base64`, `sri`). Default: `sri`.")

    @override
    async def run(self) -> None:
        await _impl.hash.run_path(self)


class File(HashPrinting):
    """Print cryptographic hash of a regular file"""

    @override
    async def run(self) -> None:
        await _impl.hash.run_file(self)


class Convert(PynixCommand):
    """Convert between hash formats"""

    hashes: list[str] = pos(help="Hashes to convert.")

    from_: HashFormatName | None = opt(None, help="Hash format (`base16`, `nix32`, `base64`, `sri`).")

    to: HashFormatName = opt("sri", help="Hash format (`base16`, `nix32`, `base64`, `sri`). Default: `sri`.")

    algo: HashAlgo | None = opt(
        None,
        help="Hash algorithm (`blake3`, `md5`, `sha1`, `sha256`, or `sha512`). Can be omitted for SRI hashes.",
    )

    @override
    async def run(self) -> None:
        await _impl.hash.run_convert(self)


class ToBase(PynixCommand):
    """``nix hash to-*``: deprecated, and ``nix hash convert`` replaces them."""

    #: The format each subcommand prints.
    target: ClassVar[HashFormatName] = "sri"

    strings: list[str] = pos(help="Hashes to convert.")

    type: HashAlgo | None = opt(None, help="Hash algorithm (`blake3`, `md5`, `sha1`, `sha256`, or `sha512`).")

    @override
    async def run(self) -> None:
        await _impl.hash.run_to_base(self)


class ToBase16(ToBase):
    """Convert a hash to base-16 representation (deprecated, use `nix hash convert` instead)"""

    cli_name = "to-base16"
    target: ClassVar[HashFormatName] = "base16"


class ToBase32(ToBase):
    """Convert a hash to base-32 representation (deprecated, use `nix hash convert` instead)"""

    cli_name = "to-base32"
    target: ClassVar[HashFormatName] = "nix32"


class ToBase64(ToBase):
    """Convert a hash to base-64 representation (deprecated, use `nix hash convert` instead)"""

    cli_name = "to-base64"
    target: ClassVar[HashFormatName] = "base64"


class ToSri(ToBase):
    """Convert a hash to SRI representation (deprecated, use `nix hash convert` instead)"""

    cli_name = "to-sri"
    target: ClassVar[HashFormatName] = "sri"


class Hash(PynixCommand):
    """Compute and convert cryptographic hashes"""

    subcommands = (Convert, Path, File, ToBase16, ToBase32, ToBase64, ToSri)
