"""The implementation of the ``pynix run`` command.

``InstallableValue::toApp`` (``src/libcmd/installable-value.cc``) and
``CmdRun::run`` (``src/nix/run.cc``) are the model, rule for rule.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from anyio import Path as AnyioPath
from huggorm import AsyncEvalState, AsyncValue

from pynix import _evaluate
from pynix._store_layout import StoreLayout
from pynix._util import error_exit, huggorm_build_session, report_and_exit
from pynix.run import Run
from pynix.target import EvaluationTarget, EvaluationTargetError

#: ``DrvName`` ends the name at the first dash that a non-letter follows.
_VERSION_DASH = re.compile(r"-(?![A-Za-z])")


def expected_type(selected: str) -> Literal["app", "derivation"]:
    """What ``toApp`` requires of the value at *selected*.

    An app under ``apps`` or ``defaultApp``, and a derivation anywhere else.
    """
    first = selected.split(".", 1)[0]
    return "app" if first in ("apps", "defaultApp") else "derivation"


def main_program(*, main: str | None, pname: str | None, name: str) -> str:
    """``meta.mainProgram``, else ``pname``, else the name without its version."""
    if main is not None:
        return main
    if pname is not None:
        return pname
    return _VERSION_DASH.split(name, maxsplit=1)[0]


def choose_bin(program: str, wanted: str | None, names: list[str]) -> str:
    """The program that ``--bin`` names, from the directory that holds *program*.

    Raises :class:`EvaluationTargetError` that lists *names* when *wanted* is
    not one of them.
    """
    if wanted is None:
        return program
    if wanted not in names:
        listed = ", ".join(sorted(names)) or "none"
        raise EvaluationTargetError(f"--bin {wanted!r} is not in {PurePosixPath(program).parent}; it holds: {listed}")
    return str(PurePosixPath(program).parent / wanted)


@dataclass(frozen=True)
class ResolvedProgram:
    """The program to execute, and what makes its store path runnable."""

    program: str
    exec_prefix: list[str]


async def _optional_string(state: AsyncEvalState, value: AsyncValue | None, name: str) -> str | None:
    result = await _evaluate.optional_attr(state, value, name)
    if result is not None and not isinstance(result, str):
        raise EvaluationTargetError(f"attribute '{name}' should be a string")
    return result


async def _program(state: AsyncEvalState, value: AsyncValue, selected: str) -> str:
    """The realised program of *value*, as ``toApp`` and ``resolve`` give it."""
    shown = selected or "the selected value"
    expected = expected_type(selected)
    kind = await _optional_string(state, value, "type")
    if kind != expected:
        raise EvaluationTargetError(f"attribute '{shown}' should have type '{expected}'")
    if kind == "app":
        return await _evaluate.realised_attr(state, value, "program")
    out_path = await _evaluate.realised_attr(state, value, "outPath")
    meta = await _evaluate.optional_set(state, value, "meta")
    main = await _optional_string(state, meta, "mainProgram")
    name = await _optional_string(state, value, "name") or ""
    pname = await _optional_string(state, value, "pname")
    return f"{out_path}/bin/{main_program(main=main, pname=pname, name=name)}"


async def resolve_program(command: Run) -> ResolvedProgram:
    """Evaluate, build and check the program that *command* runs."""
    target = EvaluationTarget.from_command(command)
    try:
        async with huggorm_build_session(
            command.store, verbosity=command.verbosity, print_build_logs=command.print_build_logs
        ) as (_session, store, _eval_store, state):
            value, locked, selected = await _evaluate.evaluate_target_selected(
                target, state, auto_call_file=True, attr_search=_evaluate.app_attr_search()
            )
            try:
                program = await _program(state, value, selected)
            finally:
                if locked is not None:
                    await locked.aclose()
            layout = await StoreLayout.of(store)
            program = await _locate(layout, program, command.bin)
            return ResolvedProgram(program=program, exec_prefix=layout.exec_prefix())
    except EvaluationTargetError as exc:
        report_and_exit(exc)


async def _locate(layout: StoreLayout, program: str, wanted: str | None) -> str:
    """Refuse a *program* outside the store, as ``resolve`` does, and apply ``--bin``."""
    if not program.startswith(layout.store_dir + "/"):
        raise EvaluationTargetError(f"app program '{program}' is not in the Nix store")
    if wanted is None:
        return program
    directory = AnyioPath(layout.to_disk(str(PurePosixPath(program).parent)))
    names = [entry.name async for entry in directory.iterdir()] if await directory.is_dir() else []
    return choose_bin(program, wanted, names)


async def run_run(command: Run) -> None:
    """The body of :meth:`pynix.run.Run.run`.

    ``exec``, so the exit status of the program is the exit status of
    ``pynix``. ``argv[0]`` is the program path, as ``CmdRun`` passes it.
    """
    resolved = await resolve_program(command)
    if resolved.exec_prefix:
        argv = [*resolved.exec_prefix, resolved.program, *command.args]
        os.execvp(argv[0], argv)  # noqa: S606 -- the helper that nanopynix ships; see nanopynix.store_exec
        return
    try:
        os.execv(resolved.program, [resolved.program, *command.args])  # noqa: S606 -- a store path this process just built
    except OSError as exc:
        error_exit(f"unable to execute '{resolved.program}': {exc.strerror}", cause=exc)
