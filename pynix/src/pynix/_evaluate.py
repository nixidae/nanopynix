"""Evaluate a command-line target on a huggorm evaluator.

The huggorm half of :mod:`pynix.target`. That module keeps what reads no
evaluator -- :class:`~pynix.target.FileReference`, its resolution and
:class:`~pynix.target.EvaluationTarget` -- and this one does the evaluation.
Issue #1051 moves each command here, one at a time.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import huggorm
import structlog
from huggorm.attrpath import (
    AttrPathError,
    AttrPathSearch,
    auto_call,
    flake_outputs,
    select_attr,
    select_flake_attr,
)
from huggorm.errors import ThrownError

from nanopynix._typechecking import BEARTYPING
from pynix.target import (
    CONFIGURATION_KINDS,
    EvaluationTargetError,
    configuration_message,
    names_stdin,
    resolve_file_reference,
)

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Awaitable, Callable

    from huggorm import AsyncEvalState, AsyncLockedFlake, AsyncRepl, AsyncValue

    from pynix.target import EvaluationTarget, FileReference

logger = structlog.get_logger("pynix.target")

# The two messages that `EvalState::findFile` raises when the lookup path
# holds no such name (`src/libexpr/eval.cc`). The second is the pure-evaluation
# form, and it means the same thing here: the lookup path cannot answer.
_SEARCH_PATH_MISS = (
    "was not found in the Nix search path",
    "in pure evaluation mode",
)


def _is_a_search_path_miss(exc: ThrownError) -> bool:
    """Report whether *exc* says the lookup path holds no such name.

    The class alone is not enough. `builtins.throw` raises the same class, so a
    `default.nix` that was found and then rejected its arguments would look
    identical, and the fallback would report a missing flake instead of the
    real failure.
    """
    return any(marker in str(exc) for marker in _SEARCH_PATH_MISS)


async def open_file_reference(
    reference: FileReference,
    opener: Callable[[str], Awaitable[AsyncValue]],
) -> AsyncValue:
    """Open the first candidate of *reference* that the evaluator can resolve.

    *opener* is `state.eval_file` or `repl.load_file`, so a REPL keeps its own
    ``:load`` semantics.

    **Only a miss in the lookup path moves to the next candidate.** A name that
    the lookup path holds, whose file then fails to evaluate, raises that
    failure. Falling back there would answer a broken `<nixpkgs>` with a
    message about a flake, which is a worse error and a wrong one.
    """
    last: ThrownError | None = None
    for candidate in reference.arguments:
        try:
            return await opener(candidate)
        except ThrownError as exc:
            if not _is_a_search_path_miss(exc):
                raise
            logger.debug("pynix file reference not in the lookup path", candidate=candidate)
            last = exc
    if last is None:  # pragma: no cover -- a reference always holds a candidate
        raise EvaluationTargetError("--file resolved to no candidate")
    raise last


# --- the attribute-path search of each command -------------------------------
#
# `nix` gives every installable command two lists, and a fragment such as
# `#hello` is resolved against them rather than read as one path.
# `SourceExprCommand` in `src/libcmd/installables.cc` holds the base pair, and
# each command overrides it. These functions are that table, and each one names
# the file of the `nix` source that decides it.


def base_attr_search() -> AttrPathSearch:
    """The pair of `SourceExprCommand`, which `build` and `eval` use."""
    system = huggorm.current_system()
    return AttrPathSearch(
        prefixes=(f"packages.{system}.", f"legacyPackages.{system}."),
        defaults=(f"packages.{system}.default", f"defaultPackage.{system}"),
    )


def dev_shell_attr_search() -> AttrPathSearch:
    """The pair of `nix develop`, from `src/nix/develop.cc`."""
    system = huggorm.current_system()
    base = base_attr_search()
    return AttrPathSearch(
        prefixes=(f"devShells.{system}.", *base.prefixes),
        defaults=(f"devShells.{system}.default", f"devShell.{system}", *base.defaults),
    )


def repl_attr_search() -> AttrPathSearch:
    """The pair of `nix repl`, from `src/nix/repl.cc`.

    `CmdRepl` overrides the defaults to one empty path, and it leaves the
    prefixes alone. An empty path selects the value itself.
    """
    return AttrPathSearch(prefixes=base_attr_search().prefixes, defaults=("",))


def app_attr_search() -> AttrPathSearch:
    """The pair of `nix run` and `nix bundle`, from `src/nix/run.cc`."""
    system = huggorm.current_system()
    base = base_attr_search()
    return AttrPathSearch(
        prefixes=(f"apps.{system}.", *base.prefixes),
        defaults=(f"apps.{system}.default", f"defaultApp.{system}", *base.defaults),
    )


def formatter_attr_search() -> AttrPathSearch:
    """The pair of `nix fmt`, from `src/nix/formatter.cc`. It has no prefix."""
    return AttrPathSearch(prefixes=(), defaults=(f"formatter.{huggorm.current_system()}",))


async def evaluate_target(
    target: EvaluationTarget,
    state: AsyncEvalState,
    *,
    auto_call_file: bool = False,
    attr_search: AttrPathSearch | None = None,
) -> AsyncValue:
    """Evaluate *target* in *state* and apply its attribute selectors."""
    value, locked = await evaluate_target_locked(target, state, auto_call_file=auto_call_file, attr_search=attr_search)
    if locked is not None:
        await locked.aclose()
    return value


async def evaluate_target_locked(
    target: EvaluationTarget,
    state: AsyncEvalState,
    *,
    auto_call_file: bool = False,
    attr_search: AttrPathSearch | None = None,
) -> tuple[AsyncValue, AsyncLockedFlake | None]:
    """Evaluate *target*, and also hand back the flake lock when there is one.

    ``pynix develop`` asks the lock which ``nixpkgs`` the flake uses, as
    ``InstallableFlake::nixpkgsFlakeRef`` does. The lock is ``None`` for a
    ``--file`` target.
    """
    value, locked, _path = await evaluate_target_selected(
        target, state, auto_call_file=auto_call_file, attr_search=attr_search
    )
    return value, locked


async def evaluate_target_selected(
    target: EvaluationTarget,
    state: AsyncEvalState,
    *,
    auto_call_file: bool = False,
    attr_search: AttrPathSearch | None = None,
) -> tuple[AsyncValue, AsyncLockedFlake | None, str]:
    """:func:`evaluate_target_locked`, and also the attribute path it selected.

    *attr_search* names the candidates that the fragment of ``--flake``
    resolves against. ``None`` reads the fragment as one exact path. The search
    reaches ``--flake`` only: ``nix`` applies no prefix to a ``--file`` target.

    The path is the candidate that resolved, with ``--attr`` after it.
    """
    target.validate(required=True)
    try:
        return await _evaluate_selected(target, state, auto_call_file=auto_call_file, attr_search=attr_search)
    except AttrPathError as exc:
        raise EvaluationTargetError(str(exc)) from exc


async def load_repl_target(target: EvaluationTarget, state: AsyncEvalState, repl: AsyncRepl) -> AsyncValue:
    """Evaluate *target* as ``nix repl`` loads it: a file as ``:load`` does."""
    target.validate(required=True)
    if names_stdin(target.file):
        raise EvaluationTargetError("--file - reads standard input, and the REPL reads its commands there")
    if target.file is None:
        return await evaluate_target(target, state, attr_search=repl_attr_search())
    reference = await resolve_file_reference(target.file)
    value = await open_file_reference(reference, repl.load_file)
    try:
        if reference.fragment:
            value = await select_attr(state, value, reference.fragment)
        if target.attr:
            value = await select_attr(state, value, target.attr)
    except AttrPathError as exc:
        raise EvaluationTargetError(str(exc)) from exc
    return value


async def _evaluate_selected(
    target: EvaluationTarget,
    state: AsyncEvalState,
    *,
    auto_call_file: bool,
    attr_search: AttrPathSearch | None,
) -> tuple[AsyncValue, AsyncLockedFlake | None, str]:
    selected = ""
    locked: AsyncLockedFlake | None = None
    if target.file is not None:
        reference = await resolve_file_reference(target.file)
        value = (
            await open_file_reference(reference, state.eval_file)
            if reference.expression is None
            else await state.eval_expr(reference.expression)
        )
        # Auto-call first, then the fragment. The root of a fetched tree is
        # usually a function, and its attributes only exist after the call.
        if auto_call_file:
            value = await auto_call(state, value)
        if reference.fragment:
            value = await select_attr(state, value, reference.fragment)
            selected = reference.fragment
    else:
        if target.flake is None:
            raise EvaluationTargetError("either --file or --flake is required")
        ref, _, flake_attr = target.flake.partition("#")
        locked = await state.lock_flake(huggorm.parse_flake_ref(ref))
        # `outputs`, and not the value `callFlake` returns: `openEvalCache` of
        # `src/libflake/flake.cc` selects them before it resolves anything.
        value = await flake_outputs(state, await state.call_flake(locked))
        if attr_search is not None:
            value, selected = await select_flake_attr(state, value, attr_search, flake_attr or None, flake_ref=ref)
        elif flake_attr:
            value = await select_attr(state, value, flake_attr)
            selected = flake_attr

    if target.attr:
        value = await select_attr(state, value, target.attr)
        selected = f"{selected}.{target.attr}" if selected else target.attr
    return value, locked, selected


async def _attr_json(value: AsyncValue, name: str) -> object:
    return await to_python(await value.get(name))


type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


async def to_python(value: AsyncValue) -> JsonValue:
    """*value*, forced all the way down, as JSON data. A path stays a path."""
    return json.loads(await value.to_json())


async def call(function: AsyncValue, *arguments: AsyncValue) -> AsyncValue:
    """*function* applied to each of *arguments* in turn, as Nix curries."""
    for argument in arguments:
        function = await function(argument)
    return function


async def optional_attr(state: AsyncEvalState, value: AsyncValue | None, name: str) -> object:
    """The attribute *name* of the attribute set *value*, as JSON data.

    ``None`` when *value* is ``None``, is not an attribute set, or lacks *name*.
    """
    if value is None:
        return None
    await state.force(value)
    if await value.type_name() != "attrs" or not await value.has(name):
        return None
    return await _attr_json(value, name)


async def optional_set(state: AsyncEvalState, value: AsyncValue, name: str) -> AsyncValue | None:
    """The attribute *name* of *value*, or ``None`` when *value* lacks it."""
    await state.force(value)
    if await value.type_name() != "attrs" or not await value.has(name):
        return None
    return await value.get(name)


async def realised_attr(state: AsyncEvalState, value: AsyncValue, name: str) -> str:
    """The string at *name*, with each store path that it names built."""
    await state.force(value)
    attr = await value.get(name)
    await state.force(attr)
    return await attr.realise_string()


async def configuration_kind(state: AsyncEvalState, value: AsyncValue) -> tuple[str, str] | None:
    """The name of *value* and its derivation attribute, when it is a configuration.

    ``None`` for every other value, so a caller keeps the message it has.
    """
    await state.force(value)
    if await value.type_name() != "attrs":
        return None
    if not await value.has("_type") or not await value.has("class"):
        return None
    if await _attr_json(value, "_type") != "configuration":
        return None
    class_name = await _attr_json(value, "class")
    if not isinstance(class_name, str):
        return None
    return CONFIGURATION_KINDS.get(class_name)


async def derivation_path(state: AsyncEvalState, value: AsyncValue, *, selected: str | None = None) -> str:
    """Return the ``drvPath`` of *value*, which must be a derivation.

    Raises :class:`EvaluationTargetError` when it is not one, so a caller
    reports it the same way it reports a bad ``--attr``.

    *selected* is the attribute path that the caller named, which the message
    for a configuration puts in front of the path it suggests.

    ``drvPath`` and not ``AsyncValue.drv_path``: that one gives the base name,
    and it does not say which of the checks below failed.
    """
    await state.force(value)
    if await value.type_name() != "attrs" or not await value.has("type"):
        kind = await configuration_kind(state, value)
        if kind is not None:
            raise EvaluationTargetError(configuration_message(kind, selected))
        raise EvaluationTargetError("value is not a derivation")
    value_type = await _attr_json(value, "type")
    if value_type != "derivation":
        raise EvaluationTargetError(f"value at attribute path is not a derivation (got {value_type!r})")
    path = await _attr_json(value, "drvPath")
    if not isinstance(path, str):
        raise EvaluationTargetError("failed to get derivation path")
    return path
