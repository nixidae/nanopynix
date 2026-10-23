"""The implementation of the ``pynix copy`` command.

``pynix.copy`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand
module on every start, and none of these imports is needed to list an option.

**Two stores, one session.** The command opens the source store and its
evaluator with :func:`pynix._util.huggorm_eval_session`, and the destination on
the same session.

**``--substitute-on-destination`` is not here.** ``copy_closure`` takes the
keyword, so adding the flag is one line. Issue #80 puts the substituter
protocol out of scope, and a flag that no test drives is worse than an absent
one. Add it with the test that proves it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import structlog

from nanopynix._typechecking import BEARTYPING
from pynix import _evaluate
from pynix._util import error_exit, huggorm_eval_session, print_json, report_and_exit
from pynix.copy import Copy
from pynix.target import EvaluationTarget, EvaluationTargetError

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Iterable

    from huggorm import AsyncEvalState, AsyncStore, StorePath
logger = structlog.get_logger("pynix.copy")

#: How many stranded paths the failure message names before it says "...".
_NAMED_IN_A_FAILURE = 3


def _endpoints(command: Copy) -> tuple[str, str]:
    """The store to read from and the store to write to, in that order.

    ``--store`` is the side that the caller did not name, which is how ``nix
    copy`` resolves the same pair. Naming both means ``--store`` takes no part.
    """
    if command.to is None and command.from_ is None:
        error_exit("name --to or --from: pynix copy needs a second store")
    source = command.from_ if command.from_ is not None else command.store
    destination = command.to if command.to is not None else command.store
    if source == destination:
        error_exit(f"--from and --to name the same store ({source})")
    return source, destination


async def _evaluated_paths(command: Copy, state: AsyncEvalState, source: AsyncStore, source_uri: str) -> list[str]:
    """The output paths of the derivation that ``--file`` or ``--flake`` names.

    Empty when the caller named neither. ``pynix copy`` builds nothing, so an
    output that the source store does not hold is an error and not a build.
    """
    target = EvaluationTarget.from_command(command)
    try:
        target.validate()
    except EvaluationTargetError as exc:
        report_and_exit(exc)
    if target.file is None and target.flake is None:
        return []

    try:
        root = await _evaluate.evaluate_target(
            target, state, auto_call_file=True, attr_search=_evaluate.base_attr_search()
        )
        drv_path = await _evaluate.derivation_path(state, root, selected=target.selected_attr())
    except EvaluationTargetError as exc:
        report_and_exit(exc)

    outputs = await source.query_derivation_output_map(await source.parse_store_path(drv_path))
    valid = await _printed(source, await source.query_valid_paths(list(outputs.values())))
    if not valid:
        error_exit(f"{drv_path} has no output in {source_uri}: build it before you copy it")
    return valid


async def _parsed(store: AsyncStore, paths: Iterable[str]) -> list[StorePath]:
    return [await store.parse_store_path(path) for path in paths]


async def _printed(store: AsyncStore, paths: Iterable[StorePath]) -> list[str]:
    return [await store.print_store_path(path) for path in paths]


async def closure_of(source: AsyncStore, paths: Iterable[str]) -> list[str]:
    """Every path that the copy will carry, sorted.

    Nix walks the references itself, which is why the command does not.
    """
    return sorted(await _printed(source, await source.compute_fs_closure(await _parsed(source, paths))))


class CopyEnds(Protocol):
    """The two stores of a copy, for :func:`copy_verified`."""

    async def present(self, paths: list[str]) -> list[str]:
        """The subset of *paths* that the destination holds, sorted."""
        ...

    async def copy(self, paths: list[str]) -> None:
        """Copy *paths* and their closure from the source to the destination."""
        ...


@dataclass(frozen=True, slots=True)
class StoreEnds:
    source: AsyncStore
    destination: AsyncStore
    check_sigs: bool

    async def present(self, paths: list[str]) -> list[str]:
        # Nothing is substituted.
        valid = await self.destination.query_valid_paths(await _parsed(self.destination, paths))
        return sorted(await _printed(self.destination, valid))

    async def copy(self, paths: list[str]) -> None:
        await self.source.copy_closure(self.destination, await _parsed(self.source, paths), check_sigs=self.check_sigs)


async def copy_verified(
    ends: CopyEnds,
    requested: list[str],
    closure: list[str],
    destination_uri: str,
) -> tuple[list[str], list[str]]:
    """Copy *requested* and its *closure* to the destination, and prove that it arrived.

    Returns the paths that were present before, and the paths that arrived.
    Exits with an error when a path that was missing is still missing.
    """
    # **Before the copy.** Nix reports nothing about what it wrote, so
    # "what did this command copy" has to be the difference between the
    # two stores. `query_missing` does not answer it: that one asks what
    # a *build* would still have to do, over derived paths and
    # substituters.
    present = await ends.present(closure)
    missing = [path for path in closure if path not in set(present)]
    logger.info(
        "pynix copy starting",
        destination=destination_uri,
        requested=len(requested),
        closure=len(closure),
        missing=len(missing),
    )
    # Nix logs `copying 0 paths` for a copy with nothing to do; `nix copy`
    # does not start one.
    if missing:
        await ends.copy(requested)

    # **And again afterwards, because a copy can end quietly.**
    # `Store::addMultipleToStore` (`src/libstore/store-api.cc`) catches
    # the failure of one path when `keep-going` is on: it counts it in
    # `nrFailed`, logs it, and returns. `copyPaths` returns void and
    # never reads `nrFailed`, so no caller of `copyClosure` can learn
    # that a path failed.
    #
    # Measured on 2.34, 2.35 and git alike: a copy of an unsigned path
    # into a store that requires a signature raised nothing, wrote
    # nothing, and this command reported both paths as copied. That
    # report was the difference computed above, which is what the
    # command *meant* to copy. `arrived` is what it did copy.
    arrived = await ends.present(missing)

    stranded = [path for path in missing if path not in set(arrived)]
    if stranded:
        error_exit(
            f"{len(stranded)} of {len(missing)} path(s) did not reach {destination_uri}, "
            f"and Nix reported no error: {', '.join(stranded[:_NAMED_IN_A_FAILURE])}"
            + (" ..." if len(stranded) > _NAMED_IN_A_FAILURE else ""),
        )
    return present, arrived


async def run_copy(command: Copy) -> None:
    """The body of :meth:`pynix.copy.Copy.run`."""
    source_uri, destination_uri = _endpoints(command)

    async with huggorm_eval_session(source_uri) as (session, source, state):
        requested = sorted({*command.paths, *await _evaluated_paths(command, state, source, source_uri)})
        if not requested:
            error_exit("name a store path, or name --file or --flake")

        try:
            closure = await closure_of(source, requested)
        except Exception as exc:
            # stderr, and not stdout: the output of this command is JSON, and
            # `pynix copy ... | jq` must not read this instead. `pynix.path_info`
            # carries the full account of `error_exit`.
            error_exit(str(exc), cause=exc)

        ends = StoreEnds(source, session.store(destination_uri), check_sigs=command.check_sigs)
        present, arrived = await copy_verified(ends, requested, closure, destination_uri)

    print_json(
        {
            "from": source_uri,
            "to": destination_uri,
            "requested": requested,
            "copied": arrived,
            "alreadyPresent": present,
        },
    )
