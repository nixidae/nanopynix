"""The implementation of ``pynix env shell``.

``CmdShell::run`` (``src/nix/env.cc``) and the default outputs of
``InstallableFlake::toDerivedPaths`` are the model.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast

from anyio import Path as AnyioPath

from nanopynix import store_exec_prefix
from nanopynix._typechecking import BEARTYPING
from pynix._impl.develop import physical_path_mapper
from pynix._util import error_exit, eval_session, report_and_exit
from pynix.target import EvaluationTarget, EvaluationTargetError, base_attr_search, evaluate_target

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Awaitable, Callable

    from nanopynix.protocols import AsyncValue
    from pynix.env import Shell

#: The file whose store paths ``nix shell`` also puts on ``PATH``.
_PROPAGATED = "nix-support/propagated-user-env-packages"


def default_outputs(*, output_specified: bool, output_name: str | None, outputs_to_install: list[str]) -> list[str]:
    """The outputs a package contributes when the caller names none.

    ``outputName`` when the value says its output was chosen, else
    ``meta.outputsToInstall``, else ``out``. Sorted, as Nix holds them in a set.
    """
    chosen = [output_name] if output_specified and output_name else outputs_to_install
    return sorted(set(chosen)) or ["out"]


async def bin_directories(roots: list[str], read_propagated: Callable[[str], Awaitable[list[str]]]) -> list[str]:
    """``<path>/bin`` of each root, then of what each one propagates, in that order.

    A breadth-first walk that visits each path once, as ``CmdShell::run``
    does. *roots* arrive in the order of the installables, and the outputs of
    one installable in store-path order. Measured against ``nix shell
    nixpkgs#jq nixpkgs#hello``: jq-man, jq-bin, hello.
    """
    done: set[str] = set()
    todo = deque(roots)
    found: list[str] = []
    while todo:
        path = todo.popleft()
        if path in done:
            continue
        done.add(path)
        found.append(f"{path}/bin")
        todo.extend(await read_propagated(path))
    return found


def _store_path_order(path: str) -> str:
    """The key that orders store paths as ``StorePath`` does: by their base name."""
    return PurePosixPath(path).name


@dataclass(frozen=True)
class ShellEnvironment:
    """What ``exec`` needs: the new ``PATH`` and the store's exec prefix."""

    path: str
    exec_prefix: list[str]


async def _optional(value: AsyncValue, name: str) -> object:
    return await value.attr(name).to_python() if await value.has_attr(name) else None


async def _output_paths(value: AsyncValue) -> list[str]:
    """Build the default outputs of the derivation *value*, and return their paths."""
    if await _optional(value, "type") != "derivation":
        raise EvaluationTargetError("value is not a derivation")
    meta = value.attr("meta") if await value.has_attr("meta") else None
    to_install = await _optional(meta, "outputsToInstall") if meta is not None else None
    name = await _optional(value, "outputName")
    outputs = default_outputs(
        output_specified=await _optional(value, "outputSpecified") is True,
        output_name=name if isinstance(name, str) else None,
        outputs_to_install=[str(item) for item in cast("list[object]", to_install)]
        if isinstance(to_install, list)
        else [],
    )
    return [await value.attr(output).attr("outPath").realise_string() for output in outputs]


def _targets(command: Shell) -> list[EvaluationTarget]:
    if command.file is not None:
        return [EvaluationTarget(file=command.file, attr=attr, flake=None) for attr in command.installables]
    return [EvaluationTarget(file=None, attr=None, flake=ref) for ref in command.installables]


async def prepare_shell(command: Shell) -> ShellEnvironment:
    """Build every installable of *command*, and compute the ``PATH`` it gets."""
    if not command.installables:
        error_exit("name at least one installable")
    try:
        async with eval_session(
            command.store, verbosity=command.verbosity, print_build_logs=command.print_build_logs
        ) as (_nix, store, session):
            roots: list[str] = []
            for target in _targets(command):
                value = await evaluate_target(target, session, auto_call_file=True, attr_search=base_attr_search())
                roots.extend(sorted(await _output_paths(value), key=_store_path_order))
            to_disk = await physical_path_mapper(store)

            async def read_propagated(path: str) -> list[str]:
                propagated = AnyioPath(to_disk(f"{path}/{_PROPAGATED}"))
                return (await propagated.read_text()).split() if await propagated.is_file() else []

            directories = await bin_directories(roots, read_propagated)
            inherited = os.environ.get("PATH", "")
            return ShellEnvironment(
                path=os.pathsep.join([*directories, inherited]) if inherited else os.pathsep.join(directories),
                exec_prefix=await store_exec_prefix(store),
            )
    except EvaluationTargetError as exc:
        report_and_exit(exc)


async def run_shell(command: Shell) -> None:
    """The body of :meth:`pynix.env.Shell.run`.

    ``exec`` through a ``PATH`` lookup, as ``UseLookupPath::Use`` does, so a
    bare program name finds the packages.
    """
    environment = await prepare_shell(command)
    argv = command.command or [os.environ.get("SHELL", "bash")]
    os.environ["PATH"] = environment.path
    try:
        if environment.exec_prefix:
            prefixed = [*environment.exec_prefix, *argv]
            os.execvp(prefixed[0], prefixed)  # noqa: S606 -- the helper that nanopynix ships; see nanopynix.store_exec
        os.execvp(argv[0], argv)  # noqa: S606 -- the caller's own command, found on the PATH built above
    except OSError as exc:
        error_exit(f"unable to execute '{argv[0]}': {exc.strerror}", cause=exc)
