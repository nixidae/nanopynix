from __future__ import annotations

from typing import override

from libpynix import opt
from pynix import _impl
from pynix._settings import PynixCommand


class GenerateSecret(PynixCommand):
    """Generate a secret key for signing store paths"""

    key_name: str = opt(
        None,
        required=True,
        metavar="name",
        help="Identifier of the key (e.g. `cache.example.org-1`).",
    )

    @override
    async def run(self) -> None:
        await _impl.key.run_generate_secret(self)


class ConvertSecretToPublic(PynixCommand):
    """Generate a public key for verifying store paths from a secret key read from standard input"""

    @override
    async def run(self) -> None:
        await _impl.key.run_convert_secret_to_public(self)


class Key(PynixCommand):
    """Generate and convert Nix signing keys"""

    subcommands = (GenerateSecret, ConvertSecretToPublic)
