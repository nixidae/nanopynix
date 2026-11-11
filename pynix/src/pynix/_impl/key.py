"""The implementation of ``pynix key``: ``CmdKeyGenerateSecret`` and
``CmdKeyConvertSecretToPublic`` of ``src/nix/sigs.cc``.

Each prints the key with no newline after it, as Nix writes it.
"""

from __future__ import annotations

import sys

import anyio
from huggorm import SecretKey, aio
from huggorm.errors import NixError

from pynix._util import error_exit
from pynix.key import ConvertSecretToPublic, GenerateSecret


async def run_generate_secret(command: GenerateSecret) -> None:
    """The body of :meth:`pynix.key.GenerateSecret.run`."""
    key = await aio.generate_secret_key(command.key_name)
    sys.stdout.write(key.to_string())


async def run_convert_secret_to_public(_command: ConvertSecretToPublic) -> None:
    """The body of :meth:`pynix.key.ConvertSecretToPublic.run`."""
    text: str = await anyio.to_thread.run_sync(sys.stdin.read)
    try:
        key = SecretKey(text)
    except NixError as exc:
        error_exit(str(exc), cause=exc)
    sys.stdout.write(key.to_public_key().to_string())
