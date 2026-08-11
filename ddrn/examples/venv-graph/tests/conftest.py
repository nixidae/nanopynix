"""What every module of this directory shares.

**One toolchain serves the whole session, and not one for each module.** The
toolchain holds an RPC session over the private store, and the store is the
same directory whichever module asks for it. A second session would open the
same store again and build the same graphs.

`toolchain.py` gives the class, and it says why the process that runs pytest
has to be the patched build.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from toolchain import Toolchain, WrongInterpreterError, check_toolchain, work_directory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

#: Every module that builds shares one work directory, so a machine that has
#: built the patched Nix once never builds it again.
WORK = "ddrn-venv-graph"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """One event loop for the whole run, and not one for each module.

    anyio gives this fixture module scope, and a session fixture cannot ask a
    module fixture for anything. `toolchain` below is the session fixture that
    needs it, because one RPC session serves every module.
    """
    return "asyncio"


@pytest.fixture(scope="session")
async def toolchain() -> AsyncIterator[Toolchain]:
    """The patched Nix, the planner environment, and the private store.

    **The interpreter is checked before the environment is built.** A run in
    the wrong shell would otherwise spend twenty minutes and then fail inside a
    build, so this asks Nix its version first and names the right command.
    """
    try:
        await check_toolchain()
    except WrongInterpreterError as refusal:
        pytest.skip(str(refusal))

    async with Toolchain(work_directory(WORK)) as ready:
        yield ready
