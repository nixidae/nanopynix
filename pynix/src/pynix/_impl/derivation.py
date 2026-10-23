"""The implementation of the ``pynix derivation`` command.

``pynix.derivation`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import json

import structlog

from pynix._evaluate import base_attr_search, derivation_path, evaluate_target
from pynix._util import huggorm_eval_session, print_json, report_and_exit
from pynix.derivation import Show
from pynix.target import EvaluationTarget, EvaluationTargetError

logger = structlog.get_logger("pynix.derivation")

#: The version of the document `nix derivation show` prints, and of each
#: derivation in it (`Derivation::toJSON`).
_FORMAT_VERSION = 4


async def run_show(command: Show) -> None:
    """The body of :meth:`pynix.derivation.Show.run`."""
    target = EvaluationTarget.from_command(command)
    try:
        target.validate(required=True)
    except EvaluationTargetError as exc:
        report_and_exit(exc)

    async with huggorm_eval_session(command.store) as (_session, store, state):
        try:
            root = await evaluate_target(target, state, auto_call_file=True, attr_search=base_attr_search())
            drv_path = await derivation_path(state, root, selected=target.selected_attr())
        except EvaluationTargetError as exc:
            report_and_exit(exc)

        path = await store.parse_store_path(drv_path)
        derivation = await store.read_derivation(path)
        # Nix's own JSON, keyed by base name, which is what `nix derivation
        # show` prints for the same path.
        print_json(
            {
                "derivations": {path.to_string(): json.loads(await derivation.to_json())},
                "version": _FORMAT_VERSION,
            }
        )
