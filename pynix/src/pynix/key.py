from __future__ import annotations

from typing import override

from libpynix import opt
from pynix import _impl
from pynix._settings import ConfiguredCommand, PynixCommand


class GenerateSecret(ConfiguredCommand):
    """Generate a secret key for signing store paths"""

    key_name: str = opt(
        None,
        required=True,
        metavar="name",
        help="Identifier of the key (e.g. `cache.example.org-1`).",
    )

    key_type: str = opt(
        "ed25519",
        metavar="type",
        help="Type of key: one of the types this Nix has. NixOS/nix has ed25519 only; Determinate Nix also has ml-dsa-44, ml-dsa-65, ml-dsa-87 and ecdsa-p384, which need the cnsa experimental feature.",
    )

    @override
    async def run(self) -> None:
        await _impl.key.run_generate_secret(self)


class ConvertSecretToPublic(ConfiguredCommand):
    """Generate a public key for verifying store paths from a secret key read from standard input"""

    @override
    async def run(self) -> None:
        await _impl.key.run_convert_secret_to_public(self)


class ConvertSecretToPem(ConfiguredCommand):
    """Convert a secret key read from standard input to PEM PKCS#8 format"""

    @override
    async def run(self) -> None:
        await _impl.key.run_convert_secret_to_pem(self)


class ConvertPublicToPem(ConfiguredCommand):
    """Convert a public key read from standard input to PEM SubjectPublicKeyInfo format"""

    @override
    async def run(self) -> None:
        await _impl.key.run_convert_public_to_pem(self)


class Key(PynixCommand):
    """Generate and convert Nix signing keys"""

    subcommands = (GenerateSecret, ConvertSecretToPublic, ConvertSecretToPem, ConvertPublicToPem)
