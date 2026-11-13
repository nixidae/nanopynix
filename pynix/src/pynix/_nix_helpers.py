"""The Nix programs pynix runs, as files in ``pynix/nix/``.

**Imported from a store path, so a helper reads as Nix code everywhere.** An
error names the file and the line, and one helper imports another with
``import ./other.nix``.

**Mounted, not copied.** :meth:`AsyncEvalState.mount` shows the directory to
the evaluator at its content store path and allows that path, as Nix does for
a flake input. Pure evaluation refuses ``import`` of any other absolute path,
a store path included. Nothing is written to the store, and the source tree of
a dev shell works as an installed ``pynix`` does.
"""

from __future__ import annotations

from pathlib import Path

import huggorm
from huggorm import AsyncEvalState, AsyncValue

from pynix._evaluate import to_python

#: Where the helpers are: inside the package, so they ship in ``$out``.
HELPERS = Path(__file__).parent / "nix"

#: The store name of the mounted directory.
_NAME = "pynix-nix-helpers"


async def helper(state: AsyncEvalState, name: str) -> AsyncValue:
    """The value of ``nix/<name>.nix``, imported through *state*."""
    mounted = await state.mount(huggorm.filesystem_accessor(HELPERS), _NAME)
    store_dir = await to_python(await state.eval_expr("builtins.storeDir"))
    return await state.eval_expr(f"import {store_dir}/{mounted}/{name}.nix")
