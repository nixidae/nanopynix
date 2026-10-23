"""The implementation of the ``pynix log`` command.

``pynix.log`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import sys

from pynix._util import huggorm_store_session
from pynix.log import Log


async def run_log(command: Log) -> None:
    """The body of :meth:`pynix.log.Log.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        log = await store.get_build_log(await store.parse_store_path(command.path))

    if log is None:
        raise SystemExit(f"build log of '{command.path}' is not available")
    sys.stdout.write(log)
