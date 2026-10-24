"""The implementation of the ``pynix flake`` command.

``pynix.flake`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass

import huggorm
import structlog
from huggorm import AsyncEvalState, AsyncValue
from huggorm.attrpath import AttrPathError, flake_outputs, select_attr
from rich.console import Console
from rich.tree import Tree

from pynix._util import (
    configured_nix_settings,
    huggorm_build_session,
    huggorm_eval_session,
    print_json,
    report_and_exit,
)
from pynix.flake import Info, Lock, Metadata, Show, Update
from pynix.target import EvaluationTargetError

logger = structlog.get_logger("pynix.flake")
console = Console()
_MAX_TREE_NODES = 200
_MAX_TREE_DEPTH = 6
_MAX_LIST_PREVIEW_ITEMS = 10
#: The label of a value whose evaluation fails.
_UNSPECIFIED = "unspecified"


@dataclass
class _TreeBudget:
    remaining: int = _MAX_TREE_NODES


async def _kind(state: AsyncEvalState, value: AsyncValue) -> str | None:
    """What *value* is once forced, or ``None`` when forcing it fails."""
    try:
        await state.force(value)
        return await value.type_name()
    except Exception:
        return None


async def _build_tree(
    tree: Tree,
    state: AsyncEvalState,
    value: AsyncValue,
    *,
    depth: int = 0,
    budget: _TreeBudget,
) -> None:
    if depth > _MAX_TREE_DEPTH:
        tree.add("[dim]<...>[/dim]")
        return
    kind = await _kind(state, value)
    if kind is None:
        tree.add("[dim]<unresolved>[/dim]")
        return
    children: list[tuple[str, AsyncValue]] = []
    length = 0
    if kind == "attrs":
        names: list[str] = []
        with contextlib.suppress(Exception):
            names = await value.names()
        if "type" in names and "drvPath" in names:
            tree.add("[dim]<derivation>[/dim]")
            return
        children = [(name, await value.get(name)) for name in names]
    elif kind == "list":
        with contextlib.suppress(Exception):
            length = await value.length()
        children = [(f"[{i}]", await value.at(i)) for i in range(min(length, _MAX_LIST_PREVIEW_ITEMS))]
    elif kind == "thunk":
        tree.add(f"[dim]{kind}[/dim]")
        return
    for name, child in children:
        if budget.remaining == 0:
            tree.add("[dim]<...>[/dim]")
            break
        budget.remaining -= 1
        branch = tree.add(_format_attr(name, await _kind(state, child) or _UNSPECIFIED))
        await _build_tree(branch, state, child, depth=depth + 1, budget=budget)
    if length > _MAX_LIST_PREVIEW_ITEMS:
        tree.add(f"[dim]... {length - _MAX_LIST_PREVIEW_ITEMS} more items[/dim]")


def _format_attr(name: str, kind: str) -> str:
    if kind in ("attrs", "list"):
        return f"[cyan]{name}[/cyan]"
    if kind == "function":
        return f"[yellow]{name}[/yellow]: [dim]<function>[/dim]"
    return f"[green]{name}[/green]: [dim]<{kind}>[/dim]"


async def _print_flake_metadata(flake_ref: str, *, store_uri: str) -> None:
    """Print what ``nix flake metadata --json`` prints for *flake_ref*.

    Nix writes the whole object itself, in C++, so nothing here assembles it.
    That is what makes the two commands agree, ``locks`` included: the lock
    graph is a graph, and a flat map of the top level could never carry a
    transitive node or a ``follows`` edge.
    """
    async with huggorm_eval_session(store_uri) as (_session, _store, state):
        locked = await state.lock_flake(huggorm.parse_flake_ref(flake_ref), write_lock_file=False)
        try:
            print_json(json.loads(await state.flake_metadata_json(locked)))
        finally:
            await locked.aclose()


async def run_show(command: Show) -> None:
    """The body of :meth:`pynix.flake.Show.run`."""
    base_ref, _, flake_attr = command.flake_ref.partition("#")
    flake_attr = flake_attr or None

    async with huggorm_eval_session(command.store) as (_session, _store, state):
        locked = await state.lock_flake(huggorm.parse_flake_ref(base_ref), write_lock_file=False)
        try:
            outputs = await flake_outputs(state, await state.call_flake(locked))
        finally:
            await locked.aclose()
        # `select_attr`, and no candidate search. `nix flake show` starts
        # at the root of the outputs on purpose: it shows what the flake
        # provides, so a prefix that hid the other outputs would defeat
        # the command.
        try:
            if flake_attr:
                outputs = await select_attr(state, outputs, flake_attr)
            if command.attrpath:
                outputs = await select_attr(state, outputs, command.attrpath)
        except AttrPathError as exc:
            report_and_exit(EvaluationTargetError(str(exc)))

        tree = Tree(f"[bold]{command.flake_ref}[/bold]")
        await _build_tree(tree, state, outputs, budget=_TreeBudget())
        console.print(tree)


async def run_metadata(command: Metadata) -> None:
    """The body of :meth:`pynix.flake.Metadata.run`."""
    await _print_flake_metadata(command.flake_ref, store_uri=command.store)


async def run_info(command: Info) -> None:
    """The body of :meth:`pynix.flake.Info.run`."""
    await _print_flake_metadata(command.flake_ref, store_uri=command.store)


async def _lock(flake_ref: str, *, store_uri: str, update_inputs: bool | list[str]) -> None:
    """Lock *flake_ref* and write its lock file, as ``nix flake lock`` does.

    ``lockFlake`` itself writes the file, and it reports each added or moved
    input as a warning, so nothing here renders a diff. An unchanged lock
    file is not written, and Nix reports nothing about it.

    ``tarball-ttl`` is 0, as both commands of Nix set it: a cached tarball
    of a moving reference would otherwise lock the old revision again.
    """
    async with huggorm_build_session(store_uri, settings=configured_nix_settings(tarball_ttl="0")) as (
        _session,
        _store,
        _eval,
        state,
    ):
        locked = await state.lock_flake(
            huggorm.parse_flake_ref(flake_ref),
            recreate=update_inputs is True,
            update=update_inputs if isinstance(update_inputs, list) else None,
            write_lock_file=True,
        )
        await locked.aclose()


async def run_lock(command: Lock) -> None:
    """The body of :meth:`pynix.flake.Lock.run`."""
    await _lock(command.flake_ref, store_uri=command.store, update_inputs=False)


async def run_update(command: Update) -> None:
    """The body of :meth:`pynix.flake.Update.run`.

    ``nix flake update`` with no input recreates the lock file, and with
    inputs it updates those alone (``CmdFlakeUpdate``). ``update_inputs``
    carries the same two cases.
    """
    await _lock(command.flake, store_uri=command.store, update_inputs=command.inputs or True)
