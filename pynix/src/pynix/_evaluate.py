"""Evaluate a command-line target on a huggorm evaluator.

:mod:`pynix.target` keeps what reads no evaluator --
:class:`~pynix.target.FileReference`, its resolution and
:class:`~pynix.target.EvaluationTarget` -- and this module does the evaluation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Final

import huggorm
import structlog
from huggorm import AsyncEvalState, AsyncLockedFlake, AsyncRepl, AsyncValue
from huggorm.attrpath import (
    AttrPathError,
    AttrPathSearch,
    auto_call,
    flake_outputs,
    select_attr,
    select_flake_attr,
)
from huggorm.errors import ThrownError

from pynix._script import base_argument
from pynix.target import (
    CONFIGURATION_KINDS,
    AutoArg,
    AutoArgKind,
    EvaluationTarget,
    EvaluationTargetError,
    FileReference,
    configuration_message,
    names_stdin,
    resolve_file_reference,
)

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


async def load_repl_target(
    target: EvaluationTarget, state: AsyncEvalState, repl: AsyncRepl, args: AsyncValue
) -> AsyncValue:
    """Evaluate *target* as ``nix repl`` loads it: a file as ``:load`` does,
    called with *args*, the set that ``--arg`` and ``--argstr`` build."""
    target.validate(required=True)
    if names_stdin(target.file):
        raise EvaluationTargetError("--file - reads standard input, and the REPL reads its commands there")
    if target.file is None:
        return await evaluate_target(target, state, attr_search=repl_attr_search())
    reference = await resolve_file_reference(target.file)
    value = await open_file_reference(reference, lambda candidate: repl.load_file(candidate, args))
    try:
        if reference.fragment:
            value = await select_attr(state, value, reference.fragment)
        if target.attr:
            value = await select_attr(state, value, target.attr)
    except AttrPathError as exc:
        raise EvaluationTargetError(str(exc)) from exc
    return value


async def _auto_arg_value(state: AsyncEvalState, arg: AutoArg) -> AsyncValue:
    match arg.kind:
        case AutoArgKind.EXPR:
            return await state.parse_expr(arg.text, base_argument())
        case AutoArgKind.STRING:
            return await state.make_string(arg.text)
        case AutoArgKind.JSON:
            # The JSON goes in as a string value, never as Nix source.
            from_json = await state.parse_expr("builtins.fromJSON")
            return await from_json(await state.make_string(arg.text))


async def auto_arg_set(state: AsyncEvalState, args: tuple[AutoArg, ...]) -> AsyncValue:
    """The attribute set ``getAutoArgs`` builds: an ``--arg`` stays an
    unforced thunk, parsed against the working directory, so an argument
    the function does not take is never evaluated."""
    made = await state.make_attrs()
    for arg in args:
        await state.attrs_set(made, arg.name, await _auto_arg_value(state, arg))
    return made


async def _evaluate_selected(
    target: EvaluationTarget,
    state: AsyncEvalState,
    *,
    auto_call_file: bool,
    attr_search: AttrPathSearch | None,
) -> tuple[AsyncValue, AsyncLockedFlake | None, str]:
    selected = ""
    locked: AsyncLockedFlake | None = None
    reference = await target.file_reference()
    if reference is not None:
        value = (
            await open_file_reference(reference, state.eval_file)
            if reference.expression is None
            # `--file -` reads from the working directory (`parseStdin`), and
            # `--expr` from the base directory (`installables.cc:472`).
            else await state.eval_expr(reference.expression, base_argument() if target.expr is not None else None)
        )
        # Auto-call first, then the fragment. The root of a fetched tree is
        # usually a function, and its attributes only exist after the call.
        if auto_call_file:
            value = await auto_call(state, value, await auto_arg_set(state, await target.arguments.resolve()))
        if reference.fragment:
            value = await select_attr(state, value, reference.fragment)
            selected = reference.fragment
    else:
        if target.flake is None:
            raise EvaluationTargetError("either --file or --flake is required")
        ref, _, flake_attr = target.flake.partition("#")
        # `InstallableFlake::getLockedFlake` applies the flake's `nixConfig`.
        locked = await state.lock_flake(huggorm.parse_flake_ref(ref, base_argument()), apply_nix_config=True)
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
    parsed: JsonValue = json.loads(await value.to_json())
    return parsed


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


async def is_derivation(state: AsyncEvalState, value: AsyncValue) -> bool:
    """libexpr's ``isDerivation``: an attribute set whose ``type`` is ``"derivation"``."""
    await state.force(value)
    return (
        await value.type_name() == "attrs"
        and await value.has("type")
        and await _attr_json(value, "type") == "derivation"
    )


_ATTR_PATH_COMPONENT: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_+-]*")


def _join_attr_path(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


async def derivations(state: AsyncEvalState, value: AsyncValue, prefix: str = "") -> list[tuple[str, AsyncValue]]:
    """The derivations *value* holds, each with its attribute path, as
    ``nix build`` expands a value that is not a derivation.

    ``getDerivations`` (``src/libexpr/get-drvs.cc``): a derivation is itself.
    A set gives each member that is a derivation, in sorted order, and goes
    into a member set only when it carries ``recurseForDerivations = true``;
    a name that is not ``[A-Za-z_][A-Za-z0-9_+-]*`` is skipped. A list gives
    each element, and goes into every element that is not a derivation.
    Anything else is the type error Nix raises.
    """
    if await is_derivation(state, value):
        return [(prefix, value)]
    kind = await value.type_name()
    found: list[tuple[str, AsyncValue]] = []
    if kind == "attrs":
        for name in sorted(await value.names()):
            if not _ATTR_PATH_COMPONENT.fullmatch(name):
                continue
            member = await value.get(name)
            path = _join_attr_path(prefix, name)
            if await is_derivation(state, member):
                found.append((path, member))
            elif await optional_attr(state, member, "recurseForDerivations") is True:
                found.extend(await derivations(state, member, path))
    elif kind == "list":
        for index in range(await value.length()):
            element = await value.at(index)
            await state.force(element)
            found.extend(await derivations(state, element, _join_attr_path(prefix, str(index))))
    else:
        raise EvaluationTargetError("expression does not evaluate to a derivation (or a set or list of those)")
    return found


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
