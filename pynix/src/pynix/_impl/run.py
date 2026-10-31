"""The implementation of the ``pynix run`` command.

``InstallableValue::toApp`` (``src/libcmd/installable-value.cc``) and
``CmdRun::run`` (``src/nix/run.cc``) are the model, rule for rule.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import anyio
import structlog
from anyio import Path as AnyioPath
from huggorm import AsyncEvalState, AsyncValue

from pynix import _evaluate
from pynix._impl.linux_builder import LinuxBuilderError, linux_builder, requested_guest
from pynix._store_layout import StoreLayout
from pynix._util import error_exit, huggorm_build_session, report_and_exit
from pynix.run import Run
from pynix.target import EvaluationTarget, EvaluationTargetError

#: ``DrvName`` ends the name at the first dash that a non-letter follows.
_VERSION_DASH = re.compile(r"-(?![A-Za-z])")
_HOST_STORE_URI = "daemon"
#: ssh's own failure status, which a program can also return.
_SSH_FAILED = 255

logger = structlog.get_logger("pynix.run")


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


@dataclass(frozen=True)
class _Where:
    """Where the program builds and evaluates, and whether its files are on this machine."""

    store: str
    eval_store: str | None = None
    settings: Mapping[str, str] | None = None
    on_host: bool = True


async def resolve_program(command: Run, where: _Where | None = None) -> ResolvedProgram:
    """Evaluate, build and check the program that *command* runs."""
    where = where or _Where(command.store)
    target = EvaluationTarget.from_command(command)
    try:
        async with huggorm_build_session(
            where.store,
            eval_store_uri=where.eval_store,
            settings=where.settings,
            verbosity=command.verbosity,
            print_build_logs=command.print_build_logs,
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
            program = await _locate(layout, program, command.bin, on_host=where.on_host)
            return ResolvedProgram(program=program, exec_prefix=layout.exec_prefix())
    except EvaluationTargetError as exc:
        report_and_exit(exc)


async def _locate(layout: StoreLayout, program: str, wanted: str | None, *, on_host: bool) -> str:
    """Refuse a *program* outside the store, as ``resolve`` does, and apply ``--bin``."""
    if not program.startswith(layout.store_dir + "/"):
        raise EvaluationTargetError(f"app program '{program}' is not in the Nix store")
    if wanted is None:
        return program
    if not on_host:
        # The directory is in the guest's store, so this machine cannot list
        # it. A missing name fails in the guest instead.
        return str(PurePosixPath(program).parent / wanted)
    directory = AnyioPath(layout.to_disk(str(PurePosixPath(program).parent)))
    names = [entry.name async for entry in directory.iterdir()] if await directory.is_dir() else []
    return choose_bin(program, wanted, names)


async def _run_on_linux_builder(command: Run, guest: str) -> int:
    """Build the program in the VM for ``--linux-system``, run it there over ssh, and return its status.

    Not ``exec``: the VM must stop after the program. ``eval-system`` sets
    ``builtins.currentSystem``, so ``nixpkgs#hello`` selects the Linux package,
    and the host store still builds only for the host.
    """
    async with linux_builder(guest, home=Path.home()) as vm:
        where = _Where(vm.store, _HOST_STORE_URI, {"eval-system": command.linux_system}, on_host=False)
        resolved = await resolve_program(command, where)
        tty = sys.stdin.isatty() and sys.stdout.isatty()
        argv = vm.ssh_argv([resolved.program, *command.args], cwd=str(Path.cwd()), tty=tty)
        async with await anyio.open_process(argv, stdin=None, stdout=None, stderr=None) as process:
            status = await process.wait()
    if status == _SSH_FAILED:
        logger.warning("pynix run: ssh into the Linux builder failed, or the program exited with 255")
    # A negative status is a signal; a shell reports that as 128 + its number.
    return 128 - status if status < 0 else status


async def run_run(command: Run) -> None:
    """The body of :meth:`pynix.run.Run.run`.

    ``exec``, so the exit status of the program is the exit status of
    ``pynix``. ``argv[0]`` is the program path, as ``CmdRun`` passes it.
    """
    if command.linux_builder:
        guest = requested_guest(guest=command.linux_builder_guest, store_named="store" in command.explicit_options)
        try:
            status = await _run_on_linux_builder(command, guest)
        except LinuxBuilderError as exc:
            error_exit(str(exc))
        sys.exit(status)
    resolved = await resolve_program(command)
    if resolved.exec_prefix:
        argv = [*resolved.exec_prefix, resolved.program, *command.args]
        os.execvp(argv[0], argv)  # noqa: S606 -- the helper that nanopynix ships; see nanopynix.store_exec
        return
    try:
        os.execv(resolved.program, [resolved.program, *command.args])  # noqa: S606 -- a store path this process just built
    except OSError as exc:
        error_exit(f"unable to execute '{resolved.program}': {exc.strerror}", cause=exc)
