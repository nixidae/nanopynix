"""The implementation of the ``pynix develop`` command.

``pynix develop`` and ``pynix print-dev-env``, the counterparts of ``nix``'s.

``pynix.develop`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import anyio.to_thread
import huggorm
import structlog
from anyio import Path as AnyioPath
from huggorm import AsyncEvalState, AsyncStore
from huggorm.attrpath import flake_outputs, select_attr
from huggorm.devshell import awrite_dev_shell_derivation
from huggorm.errors import NixError
from rich.text import Text

from pynix import _evaluate
from pynix._dev_env import GET_ENV_SH, BuildEnvironment, DevEnvError, make_rc_script, quote
from pynix._store_layout import StoreLayout
from pynix._util import error_exit, huggorm_build_session, print_json, report_and_exit
from pynix.develop import Develop, PrintDevEnv
from pynix.target import EvaluationTarget, EvaluationTargetError

logger = structlog.get_logger("pynix.develop")
#: What Nix puts in front of an error message of its own.
_NIX_ERROR_PREFIX = "error: "
#: Where an output path is rewritten to. ``develop.cc:351`` uses the same
#: directory, and it is relative to the working directory rather than to a
#: temporary one, so that a phase which writes to ``$out`` writes somewhere the
#: caller can find.
_OUTPUTS_DIR_NAME = "outputs"
#: Wrapped around the environment for an interactive shell, mirroring
#: ``develop.cc:625``. ``~/.bashrc`` runs first so that a prompt survives, and
#: alias expansion is off while the environment is applied because an alias
#: would otherwise rewrite what the environment defines.
_INTERACTIVE_PROLOGUE = '[ -n "$PS1" ] && [ -e ~/.bashrc ] && source ~/.bashrc;\nshopt -u expand_aliases\n'
_INTERACTIVE_EPILOGUE = "\nshopt -s expand_aliases\n"
#: Where the interactive bash comes from. ``defaultNixpkgsFlakeRef()``, an
#: indirect reference that the registry resolves.
_NIXPKGS_FLAKE_REF = "nixpkgs"
_INTERACTIVE_BASH_ATTR = "bashInteractive"


@runtime_checkable
class _FlakeRef(Protocol):
    def to_string(self) -> str: ...


@runtime_checkable
class _LockedInput(Protocol):
    def is_flake(self) -> bool: ...
    def locked_ref(self) -> _FlakeRef: ...


@runtime_checkable
class NixpkgsLock(Protocol):
    """What :func:`_nixpkgs_flake_ref` asks of a flake lock: huggorm's
    ``AsyncLockedFlake``, or a test's double."""

    async def find_input(self, path: list[str]) -> _LockedInput | None: ...


@dataclass(frozen=True)
class InteractiveShell:
    """The bash that an interactive dev shell runs.

    *from_nixpkgs* separates the two cases that ``develop.cc`` also separates:
    a shell it chose, whose directory it prepends to ``PATH``, and the bare
    ``bash`` it falls back to, which is on ``PATH`` already.
    """

    path: str
    from_nixpkgs: bool
    exec_prefix: list[str]


def compose_shell_script(
    environment: BuildEnvironment,
    *,
    command: list[str],
    outputs_dir: Path,
    cleanup: Path | None = None,
    shell: InteractiveShell | None = None,
) -> str:
    """Build the bash that ``develop`` hands to the shell.

    A command ends the script with ``exec``, which is what gives the command
    the exit status of the shell, and therefore of ``pynix``. Without one the
    script is wrapped for interactive use.

    *cleanup* is removed by the script itself, before the command runs.
    ``exec`` never returns, so a line after it would never run -- which is why
    develop.cc:599 also puts this line before the branch.

    *shell* adds the two lines of ``develop.cc:688``, and only for an
    interactive shell. Nix appends them after its own ``exec`` line, where they
    never run, so a command sees no ``SHELL`` of Nix's either.
    """
    script = make_rc_script(environment, outputs_dir=outputs_dir)
    if cleanup is not None:
        script += f"command rm -rf {quote(str(cleanup))}\n"
    if command:
        # quote(), not shlex.join(): develop.cc:620 quotes every word, and a
        # word that shlex leaves bare would be re-split by the shell.
        return script + "exec {}\n".format(" ".join(quote(word) for word in command))
    script = _INTERACTIVE_PROLOGUE + script + _INTERACTIVE_EPILOGUE
    if shell is not None:
        # The build's own bash is not interactive: it is built without
        # readline. Leaving it in SHELL hands it to everything that spawns the
        # user's shell -- vim's :sh, a git editor -- from inside the dev shell.
        script += f'SHELL="{shell.path}"\n'
        if shell.from_nixpkgs:
            # Only when the lookup succeeded, as at develop.cc:689. The
            # fallback shell is already on PATH by definition.
            script += f'PATH="{Path(shell.path).parent}${{PATH:+:$PATH}}"\n'
    return script


def _exec_bash(rc_path: Path, *, interactive: bool, shell: InteractiveShell | None = None) -> None:
    """Replace this process with the bash that reads *rc_path*.

    ``exec``, so the shell owns the terminal and its exit status is the exit
    status of ``pynix``.

    A command runs the file as a script, and an interactive shell reads it as
    an rc file. ``develop.cc:698`` gives the reason for the difference: with
    ``--rcfile``, Ctrl-C would leave an interactive shell behind after the
    command it interrupted.

    ``argv[0]`` stays ``bash`` whatever binary runs, which is what
    ``shell.filename()`` does at develop.cc:698.
    """
    tail = ["--rcfile", str(rc_path)] if interactive else [str(rc_path)]
    if shell is None or not shell.from_nixpkgs:
        os.execvp("bash", ["bash", *tail])  # noqa: S606, S607 -- the bare name is the point: it is what `nix develop` falls back to at develop.cc:642
        return
    argv = [*shell.exec_prefix, shell.path, *tail]
    if shell.exec_prefix:
        # The helper takes the program as its own argument, so there is no
        # argv[0] of ours to set. See nanopynix.store_exec.
        os.execvp(argv[0], argv)  # noqa: S606 -- the helper that nanopynix ships, resolved off PATH like the rest of its tools
        return
    os.execv(shell.path, ["bash", *tail])  # noqa: S606 -- an absolute store path that this process just built, and argv[0] is bash as at develop.cc:698


async def build_dev_env(
    command: Any, *, resolve_shell: bool = False
) -> tuple[BuildEnvironment, InteractiveShell | None]:
    """Compute the build environment of *command*'s evaluation target.

    *resolve_shell* also picks the interactive bash, in the same session, which
    is where ``CmdDevelop::run`` picks it.
    """
    target = EvaluationTarget.from_command(command)
    try:
        target.validate(required=True)
    except EvaluationTargetError as exc:
        report_and_exit(exc)

    get_env_script = await AnyioPath(GET_ENV_SH).read_bytes()

    # One store unless the caller named a second one, as huggorm_build_session
    # gives it: two Store objects for one URI are two stores to Nix.
    async with huggorm_build_session(
        command.store,
        eval_store_uri=command.eval_store,
        verbosity=command.verbosity,
        print_build_logs=command.print_build_logs,
    ) as (_session, build_store, eval_store, state):
        shell: InteractiveShell | None = None
        # The lock is kept past the evaluation because the interactive shell
        # needs it: it decides which nixpkgs `bashInteractive` comes from. One
        # `finally` covers every way out, including `report_and_exit`, which
        # raises.
        locked: huggorm.AsyncLockedFlake | None = None
        try:
            try:
                root, locked = await _evaluate.evaluate_target_locked(
                    target, state, auto_call_file=True, attr_search=_evaluate.dev_shell_attr_search()
                )
                drv_path = await _evaluate.derivation_path(state, root, selected=target.selected_attr())
            except EvaluationTargetError as exc:
                report_and_exit(exc)
            if resolve_shell:
                shell = await _resolve_interactive_shell(state, eval_store, build_store, locked)
        finally:
            if locked is not None:
                await locked.aclose()

        raw = await _read_dev_env(
            eval_store,
            build_store,
            drv_path=drv_path,
            get_env_script=get_env_script,
        )

    try:
        return BuildEnvironment.from_json(raw), shell
    except DevEnvError as exc:
        error_exit(str(exc), cause=exc)


async def _read_dev_env(
    eval_store: AsyncStore,
    build_store: AsyncStore,
    *,
    drv_path: str,
    get_env_script: bytes,
) -> str:
    """Rewrite the derivation, build it, and read the JSON that it wrote."""
    try:
        shell_drv = await awrite_dev_shell_derivation(
            eval_store, await eval_store.parse_store_path(drv_path), get_env_script
        )
    except NixError as exc:
        # The one Nix error this command expects: a derivation whose builder is
        # not bash cannot dump its environment. A traceback would say the same
        # thing at ten times the length, so report the reason and stop.
        # `Text.from_ansi` first, and `removeprefix` after: the "error: " of
        # Nix carries its own colour, so the prefix is not at the start of
        # `exc.msg` and it is at the start of the plain text of the `Text`.
        # "Error:" comes from `error_exit`, so this drops the second one.
        text = Text.from_ansi(str(exc))
        error_exit(text[len(_NIX_ERROR_PREFIX) :] if text.plain.startswith(_NIX_ERROR_PREFIX) else text, cause=exc)
    shell_drv_path = await eval_store.print_store_path(shell_drv)
    logger.info("pynix develop building the environment", drv_path=shell_drv_path)
    if (error := await _build_error(eval_store, build_store, shell_drv_path)) is not None:
        error_exit(f"failed to build the development environment: {error}", cause=error)

    # `get-env.sh` writes to whichever output it is given, so take the first
    # output that has anything in it. develop.cc:303 does the same.
    layout = await StoreLayout.of(build_store)
    for output in (await eval_store.query_derivation_output_map(shell_drv)).values():
        candidate = AnyioPath(layout.to_disk(await eval_store.print_store_path(output)))
        if await candidate.is_file() and (await candidate.stat()).st_size:
            return await candidate.read_text()
    # `return`, though error_exit never returns: NoReturn is assignable to
    # `str`, and it is what tells ruff that this branch ends the function.
    return error_exit(f"the build environment of {drv_path} is empty")


async def _build_error(eval_store: AsyncStore, build_store: AsyncStore, drv_path: str) -> NixError | None:
    """Build every output of *drv_path*, and give the first failure, or None."""
    derived = await build_store.parse_derived_path(f"{drv_path}^*")
    separate_eval_store = eval_store if eval_store is not build_store else None
    results = await build_store.build_paths_with_results([derived], eval_store=separate_eval_store)
    return next((error for result in results if (error := result.error()) is not None), None)


async def _nixpkgs_flake_ref(locked: NixpkgsLock | None) -> str:
    """Return the flake reference that ``bashInteractive`` comes from.

    ``InstallableFlake::nixpkgsFlakeRef`` (``installable-flake.cc:194``): the
    ``nixpkgs`` that the target flake locks, when the target is a flake that
    locks one and that input is itself a flake. That is the reference the
    ``flake.lock`` of the target pins, so the dev shell of a flake gets the
    bash of the same nixpkgs the flake was built against, whatever the registry
    of the machine says.

    Otherwise the indirect ``nixpkgs``, which is exactly
    ``defaultNixpkgsFlakeRef()`` (``installable-flake.hh:85``). A ``--file``
    target has no lock, and a flake need not declare ``nixpkgs`` at all.
    """
    if locked is None:
        return _NIXPKGS_FLAKE_REF
    node = await locked.find_input([_NIXPKGS_FLAKE_REF])
    if node is None or not node.is_flake():
        return _NIXPKGS_FLAKE_REF
    return node.locked_ref().to_string()


async def _resolve_interactive_shell(
    state: AsyncEvalState,
    eval_store: AsyncStore,
    build_store: AsyncStore,
    locked: huggorm.AsyncLockedFlake | None,
) -> InteractiveShell:
    """Pick the bash an interactive dev shell runs, as ``develop.cc:645`` does.

    ``bashInteractive``, built, because the bash of the build environment is
    stdenv's and carries no readline. :func:`_nixpkgs_flake_ref` decides which
    nixpkgs it comes from. Nix wraps the whole lookup in a ``try`` and falls
    back to the bare word ``bash`` on failure (``develop.cc:679``), so an
    evaluation error, a missing registry entry or an offline machine costs the
    readline, not the shell.
    """
    try:
        found = await _nixpkgs_bash(state, eval_store, build_store, locked)
    except (NixError, EvaluationTargetError, RuntimeError) as exc:
        # Never fatal, as at develop.cc:679: the shell still starts, and only
        # its line editing is the poorer for it. RuntimeError covers the
        # relocated store with no store-exec helper -- see
        # StoreLayout.exec_prefix, which raises rather than hand back a prefix
        # that cannot work.
        logger.info("pynix develop is falling back to the bash on PATH", reason=str(exc))
        return _fallback_shell()
    if found is None:
        logger.info("pynix develop found no bin/bash in nixpkgs, so it is falling back to PATH")
        return _fallback_shell()
    return found


async def _nixpkgs_bash(
    state: AsyncEvalState,
    eval_store: AsyncStore,
    build_store: AsyncStore,
    locked: huggorm.AsyncLockedFlake | None,
) -> InteractiveShell | None:
    """Build ``bashInteractive`` from nixpkgs, and find its ``bin/bash``.

    ``None`` when the build produced no ``bin/bash``, which
    ``develop.cc:676`` treats the same as a failed lookup.
    """
    nixpkgs = await state.lock_flake(huggorm.parse_flake_ref(await _nixpkgs_flake_ref(locked)), write_lock_file=False)
    try:
        outputs = await flake_outputs(state, await state.call_flake(nixpkgs))
    finally:
        await nixpkgs.aclose()
    attrpath = f"legacyPackages.{huggorm.get_setting('system')}.{_INTERACTIVE_BASH_ATTR}"
    value = await select_attr(state, outputs, attrpath)
    drv_path = await _evaluate.derivation_path(state, value)
    logger.info("pynix develop is building the interactive shell", attrpath=attrpath)
    if await _build_error(eval_store, build_store, drv_path) is not None:
        return None

    layout = await StoreLayout.of(build_store)
    for output in (await eval_store.query_derivation_output_map(await eval_store.parse_store_path(drv_path))).values():
        candidate = f"{await eval_store.print_store_path(output)}/bin/bash"
        if await AnyioPath(layout.to_disk(candidate)).is_file():
            return InteractiveShell(
                path=candidate,
                from_nixpkgs=True,
                # Empty for an ordinary store. `nix develop` reaches the same
                # problem through execProgramInStore.
                exec_prefix=layout.exec_prefix(),
            )
    return None


def _fallback_shell() -> InteractiveShell:
    """The bash on PATH, which is what Nix falls back to at ``develop.cc:642``."""
    return InteractiveShell(path=shutil.which("bash") or "bash", from_nixpkgs=False, exec_prefix=[])


async def run_print_dev_env(command: PrintDevEnv) -> None:
    """The body of :meth:`pynix.develop.PrintDevEnv.run`."""
    environment, _ = await build_dev_env(command)
    if command.json:
        print_json(environment.to_json())
        return
    script = make_rc_script(environment, outputs_dir=Path.cwd() / _OUTPUTS_DIR_NAME)
    # The trailing newline is not part of the script. Nix's
    # `logger->writeToStdout` adds one, and this output is compared against
    # `nix print-dev-env` byte for byte.
    sys.stdout.write(script + "\n")


async def run_develop(command: Develop) -> None:
    """The body of :meth:`pynix.develop.Develop.run`."""
    words = command.command
    # Only an interactive shell needs one. develop.cc:688 appends its
    # `SHELL=` line after the `exec` of a command, where nothing runs it, so
    # resolving a shell for a command would cost a nixpkgs evaluation and
    # change nothing.
    environment, shell = await build_dev_env(command, resolve_shell=not words)

    # mkdtemp rather than TemporaryDirectory: execvp replaces this process,
    # so nothing of ours ever cleans up. The script removes the directory
    # itself, which is what develop.cc:599 does for the same reason.
    directory = await anyio.to_thread.run_sync(partial(tempfile.mkdtemp, suffix="", prefix="pynix-develop-"))
    script = compose_shell_script(
        environment,
        command=words,
        outputs_dir=Path.cwd() / _OUTPUTS_DIR_NAME,
        cleanup=Path(directory),
        shell=shell,
    )
    rc_path = Path(directory) / "rc"
    await AnyioPath(rc_path).write_text(script)

    logger.info("pynix develop entering the environment", command=words or None)
    _exec_bash(rc_path, interactive=not words, shell=shell)
