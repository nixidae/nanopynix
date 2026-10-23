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

from nanopynix._typechecking import BEARTYPING
from pynix import _evaluate
from pynix._store_layout import StoreLayout
from pynix._util import error_exit, huggorm_build_session, report_and_exit
from pynix.target import EvaluationTarget, EvaluationTargetError

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Awaitable, Callable

    from huggorm import AsyncEvalState, AsyncValue

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


async def output_paths(state: AsyncEvalState, value: AsyncValue) -> list[str]:
    """Build the default outputs of the derivation *value*, and return their paths."""
    if await _evaluate.optional_attr(state, value, "type") != "derivation":
        raise EvaluationTargetError("value is not a derivation")
    meta = await _evaluate.optional_set(state, value, "meta")
    to_install = await _evaluate.optional_attr(state, meta, "outputsToInstall")
    name = await _evaluate.optional_attr(state, value, "outputName")
    outputs = default_outputs(
        output_specified=await _evaluate.optional_attr(state, value, "outputSpecified") is True,
        output_name=name if isinstance(name, str) else None,
        outputs_to_install=[str(item) for item in cast("list[object]", to_install)]
        if isinstance(to_install, list)
        else [],
    )
    return [await _evaluate.realised_attr(state, await value.get(output), "outPath") for output in outputs]


def _targets(command: Shell) -> list[EvaluationTarget]:
    if command.file is not None:
        return [EvaluationTarget(file=command.file, attr=attr, flake=None) for attr in command.installables]
    return [EvaluationTarget(file=None, attr=None, flake=ref) for ref in command.installables]


async def prepare_shell(command: Shell) -> ShellEnvironment:
    """Build every installable of *command*, and compute the ``PATH`` it gets."""
    if not command.installables:
        error_exit("name at least one installable")
    try:
        async with huggorm_build_session(
            command.store, verbosity=command.verbosity, print_build_logs=command.print_build_logs
        ) as (_session, store, _eval_store, state):
            roots: list[str] = []
            for target in _targets(command):
                value = await _evaluate.evaluate_target(
                    target, state, auto_call_file=True, attr_search=_evaluate.base_attr_search()
                )
                roots.extend(sorted(await output_paths(state, value), key=_store_path_order))
            layout = await StoreLayout.of(store)

            async def read_propagated(path: str) -> list[str]:
                propagated = AnyioPath(layout.to_disk(f"{path}/{_PROPAGATED}"))
                return (await propagated.read_text()).split() if await propagated.is_file() else []

            directories = await bin_directories(roots, read_propagated)
            inherited = os.environ.get("PATH", "")
            return ShellEnvironment(
                path=os.pathsep.join([*directories, inherited]) if inherited else os.pathsep.join(directories),
                exec_prefix=layout.exec_prefix(),
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
