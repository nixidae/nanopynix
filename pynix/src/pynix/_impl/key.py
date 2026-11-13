"""The implementation of ``pynix key``: ``CmdKeyGenerateSecret`` and
``CmdKeyConvertSecretToPublic`` of ``src/nix/sigs.cc``.

Each prints the key with no newline after it, as Nix writes it.
"""

from __future__ import annotations

import sys

import anyio
from huggorm import KeyType, SecretKey, aio
from huggorm.errors import NixError

from pynix._util import error_exit
from pynix.key import ConvertSecretToPublic, GenerateSecret


def key_type(word: str) -> KeyType:
    """*word* as a type this Nix has, or Determinate Nix's refusal (``parseKeyType``).

    ``list(KeyType)`` answers which types the linked Nix has: one on NixOS/nix.
    """
    try:
        return KeyType(word)
    except ValueError:
        error_exit(f"unknown key type '{word}'; valid key types are {', '.join(sorted(KeyType))}")


async def run_generate_secret(command: GenerateSecret) -> None:
    """The body of :meth:`pynix.key.GenerateSecret.run`."""
    try:
        key = await aio.generate_secret_key(command.key_name, key_type(command.key_type))
    except NixError as exc:
        error_exit(str(exc), cause=exc)
    sys.stdout.write(key.to_string())


async def run_convert_secret_to_public(_command: ConvertSecretToPublic) -> None:
    """The body of :meth:`pynix.key.ConvertSecretToPublic.run`."""
    text: str = await anyio.to_thread.run_sync(sys.stdin.read)
    try:
        key = SecretKey(text)
    except NixError as exc:
        error_exit(str(exc), cause=exc)
    sys.stdout.write(key.to_public_key().to_string())
