"""Where a store's paths are, and where its bytes are.

A store opened with a root reports logical ``/nix/store/...`` paths while the
bytes live under that root. Reading a reported path finds nothing, and running
one needs nanopynix's store-exec helper. :mod:`nanopynix.store_exec` gives the
reason at length.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nanopynix._typechecking import BEARTYPING
from nanopynix.store_exec import STORE_EXEC_TOOL

if TYPE_CHECKING or BEARTYPING:
    from huggorm import AsyncStore


@dataclass(frozen=True, slots=True)
class StoreLayout:
    """The logical store directory, and the real one, or None for a store
    with no filesystem."""

    store_dir: str
    real_store_dir: str | None

    @classmethod
    async def of(cls, store: AsyncStore) -> StoreLayout:
        real = await store.real_store_dir()
        return cls((await store.store_dir()).rstrip("/"), None if real is None else str(real).rstrip("/"))

    @property
    def relocated(self) -> bool:
        return self.real_store_dir is not None and self.real_store_dir != self.store_dir

    def to_disk(self, path: str) -> str:
        """Where the bytes of *path* really are. *path* itself for an ordinary store."""
        if not self.relocated or not path.startswith(self.store_dir) or self.real_store_dir is None:
            return path
        return self.real_store_dir + path[len(self.store_dir) :]

    def exec_prefix(self) -> list[str]:
        """An argv prefix that makes the store's paths runnable, as
        ``nanopynix.store_exec_prefix`` gives it. Empty for an ordinary store.

        Raises ``RuntimeError`` for a relocated store when the helper is not on
        ``PATH``: an empty prefix there fails later, on a valid store path.
        """
        if not self.relocated or self.real_store_dir is None:
            return []
        tool = shutil.which(STORE_EXEC_TOOL)
        if tool is None:
            raise RuntimeError(
                f"{STORE_EXEC_TOOL} is not on PATH, so programs cannot be run from the relocated store "
                f"at {self.real_store_dir} (its paths are reported as {self.store_dir}/...)."
            )
        return [tool, self.store_dir, self.real_store_dir]
