"""The implementation of the ``pynix eval`` command.

``pynix.eval`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace

import structlog

from pynix._evaluate import base_attr_search, call, evaluate_target
from pynix._util import error_exit, huggorm_eval_session, print_json, read_only_mode, report_and_exit
from pynix.eval import Eval
from pynix.target import EvaluationTarget, EvaluationTargetError

logger = structlog.get_logger("pynix.eval")


async def run_eval(command: Eval) -> None:
    """The body of :meth:`pynix.eval.Eval.run`."""
    if command.json and command.raw:
        error_exit("--json and --raw contradict each other")
    target = replace(EvaluationTarget.from_command(command), expr=command.expr)
    try:
        target.validate()
    except EvaluationTargetError as exc:
        report_and_exit(exc)
    stdin_expr: str | None = None
    if target.file is None and target.flake is None and target.expr is None:
        stdin_expr = sys.stdin.read()
        logger.info("reading expression from stdin")

    # An expression on standard input is `nix eval --file -`, so it is a file,
    # and impure (`installables.cc:457`).
    pure = target.pure(impure=command.impure) if stdin_expr is None else False
    async with huggorm_eval_session(command.store, pure=pure) as (_session, _store, state):
        with read_only_mode(command.read_only):
            try:
                root = (
                    await evaluate_target(target, state, auto_call_file=True, attr_search=base_attr_search())
                    if stdin_expr is None
                    else await state.eval_expr(stdin_expr, None)
                )
            except EvaluationTargetError as exc:
                report_and_exit(exc)
            if command.apply is not None:
                root = await call(await state.eval_expr(command.apply), root)
            if command.raw:
                sys.stdout.write(await root.coerce_to_string())
                return
            # Nix's `printValueAsJSON`, as `nix eval --json` prints it.
            rendered = await root.to_json()
            if command.json:
                sys.stdout.write(f"{rendered}\n")
                return
            print_json(json.loads(rendered))
