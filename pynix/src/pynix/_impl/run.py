"""The implementation of the ``pynix run`` command.

``InstallableValue::toApp`` (``src/libcmd/installable-value.cc``) and
``CmdRun::run`` (``src/nix/run.cc``) are the model, rule for rule.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal

from anyio import Path as AnyioPath

from nanopynix import store_exec_prefix
from nanopynix._typechecking import BEARTYPING
from pynix._impl.develop import physical_path_mapper
from pynix._util import error_exit, eval_session, report_and_exit
from pynix.target import EvaluationTarget, EvaluationTargetError, app_attr_search, evaluate_target_selected

if TYPE_CHECKING or BEARTYPING:
    from nanopynix.protocols import AsyncStore, AsyncValue
    from pynix.run import Run

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


async def _optional_string(value: AsyncValue, name: str) -> str | None:
    if not await value.has_attr(name):
        return None
    result = await value.attr(name).to_python()
    if not isinstance(result, str):
        raise EvaluationTargetError(f"attribute '{name}' should be a string")
    return result


async def _program(value: AsyncValue, selected: str) -> str:
    """The realised program of *value*, as ``toApp`` and ``resolve`` give it."""
    shown = selected or "the selected value"
    expected = expected_type(selected)
    kind = await _optional_string(value, "type")
    if kind != expected:
        raise EvaluationTargetError(f"attribute '{shown}' should have type '{expected}'")
    if kind == "app":
        return await value.attr("program").realise_string()
    out_path = await value.attr("outPath").realise_string()
    meta = value.attr("meta") if await value.has_attr("meta") else None
    main = await _optional_string(meta, "mainProgram") if meta is not None else None
    name = await _optional_string(value, "name") or ""
    return f"{out_path}/bin/{main_program(main=main, pname=await _optional_string(value, 'pname'), name=name)}"


async def resolve_program(command: Run) -> ResolvedProgram:
    """Evaluate, build and check the program that *command* runs."""
    target = EvaluationTarget.from_command(command)
    try:
        async with eval_session(
            command.store, verbosity=command.verbosity, print_build_logs=command.print_build_logs
        ) as (_nix, store, session):
            value, locked, selected = await evaluate_target_selected(
                target, session, auto_call_file=True, attr_search=app_attr_search()
            )
            try:
                program = await _program(value, selected)
            finally:
                if locked is not None:
                    await locked.release()
            program = await _locate(store, program, command.bin)
            return ResolvedProgram(program=program, exec_prefix=await store_exec_prefix(store))
    except EvaluationTargetError as exc:
        report_and_exit(exc)


async def _locate(store: AsyncStore, program: str, wanted: str | None) -> str:
    """Refuse a *program* outside the store, as ``resolve`` does, and apply ``--bin``."""
    store_dir = (await store.store_dirs()).store_dir.rstrip("/")
    if not program.startswith(store_dir + "/"):
        raise EvaluationTargetError(f"app program '{program}' is not in the Nix store")
    if wanted is None:
        return program
    directory = AnyioPath((await physical_path_mapper(store))(str(PurePosixPath(program).parent)))
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
