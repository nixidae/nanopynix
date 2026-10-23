"""The implementation of the ``pynix path-info`` command.

``pynix.path_info`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import structlog

from pynix._util import error_exit, huggorm_store_session, print_json
from pynix.path_info import PathInfo

logger = structlog.get_logger("pynix.path_info")


async def run_path_info(command: PathInfo) -> None:
    """The body of :meth:`pynix.path_info.PathInfo.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        try:
            info = await store.query_path_info(await store.parse_store_path(command.path))
        except Exception as exc:
            # stderr, and not stdout: the output of this command is JSON,
            # and `pynix path-info ... | jq` must not read this instead.
            #
            # `error_exit` turns the message into a `Text`, which is what
            # keeps the colour of Nix. See that function for the reason
            # that interpolation loses it.
            error_exit(str(exc), cause=exc)
            raise SystemExit(1) from exc
        ca = info.ca()
        deriver = info.deriver()
        result = {
            "path": await store.print_store_path(info.path()),
            "narHash": info.nar_hash().sri(),
            "narSize": info.nar_size(),
            "references": [await store.print_store_path(ref) for ref in info.references()],
            "registrationTime": info.registration_time(),
            "ultimate": info.ultimate(),
            "ca": None if ca is None else ca.render(),
        }
        if deriver is not None:
            result["deriver"] = await store.print_store_path(deriver)
        print_json(result)
