"""The implementation of the ``pynix derivation`` command.

``pynix.derivation`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import json

import structlog

from pynix._evaluate import (
    base_attr_search,
    configuration_kind,
    derivation_path,
    derivations,
    evaluate_target,
)
from pynix._util import huggorm_eval_session, print_json, report_and_exit
from pynix.derivation import Show
from pynix.target import EvaluationTarget, EvaluationTargetError, configuration_message

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

    async with huggorm_eval_session(command.store, pure=target.pure(impure=command.impure)) as (_session, store, state):
        try:
            root = await evaluate_target(target, state, auto_call_file=True, attr_search=base_attr_search())
        except EvaluationTargetError as exc:
            report_and_exit(exc)
        # A configuration holds no derivation at the top level, so Nix prints an empty
        # document for it; the message names the path that works instead.
        kind = await configuration_kind(state, root)
        if kind is not None:
            report_and_exit(EvaluationTargetError(configuration_message(kind, target.selected_attr())))
        try:
            # `getDerivations`, as `nix derivation show` expands a value that is not a derivation.
            found = await derivations(state, root, target.selected_attr() or "")
            drv_paths = [await derivation_path(state, value, selected=attr) for attr, value in found]
        except EvaluationTargetError as exc:
            report_and_exit(exc)

        shown: dict[str, object] = {}
        for drv_path in drv_paths:
            path = await store.parse_store_path(drv_path)
            derivation = await store.read_derivation(path)
            # Nix's own JSON, keyed by base name, which is what `nix derivation
            # show` prints for the same path.
            shown[path.to_string()] = json.loads(await derivation.to_json())
        print_json({"derivations": shown, "version": _FORMAT_VERSION})
