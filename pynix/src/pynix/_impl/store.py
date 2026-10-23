"""The implementation of the ``pynix store`` command.

``pynix.store`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from huggorm import ContentAddressMethod, GCAction, GCOptions, HashAlgorithm
from huggorm.errors import BadStorePath

from nanopynix._typechecking import BEARTYPING
from pynix._util import huggorm_local_store_path, huggorm_store_session, print_json
from pynix.store import (
    Add,
    AddFile,
    AddIndirectRoot,
    AddPath,
    AddPermRoot,
    AddTempRoot,
    Cat,
    ComputeFsClosure,
    DiffClosures,
    Dirs,
    EnsurePath,
    FollowLinksToStorePath,
    Info,
    IsValidPath,
    ListValidPaths,
    Ls,
    Optimise,
    PathFromHashPart,
    PrintAlive,
    PrintDead,
    PrintRoots,
    QueryDerivationOutputs,
    QueryMissing,
    QueryReferrers,
    QuerySubstitutablePaths,
    QueryValidDerivers,
    Verify,
)

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import Iterable

    from huggorm import AsyncStore, DerivedPathBuilt, StorePath
logger = structlog.get_logger("pynix.store")


def _print_paths(paths: Iterable[str]) -> None:
    print_json({"paths": list(paths)})


async def _store_path(store: AsyncStore, path: str) -> StorePath:
    """*path* as a store path. A base name is a path in this store."""
    if path and not path.startswith("/"):
        path = f"{await store.store_dir()}/{path}"
    return await store.parse_store_path(path)


async def _derived_path(store: AsyncStore, path: str) -> StorePath | DerivedPathBuilt:
    """*path* as Nix reads a derived path, except that a bare ``.drv`` selects every output.

    Nix reads a bare ``.drv`` as the file alone, and plans no build for it.
    """
    if not path.startswith("/"):
        path = f"{await store.store_dir()}/{path}"
    if path.endswith(".drv") and "^" not in path:
        path = f"{path}^*"
    return await store.parse_derived_path(path)


async def _printed(store: AsyncStore, paths: Iterable[StorePath]) -> list[str]:
    return [await store.print_store_path(path) for path in paths]


def _text(path: os.PathLike[str] | None) -> str | None:
    return None if path is None else os.fspath(path)


async def _store_dirs_to_json(store: AsyncStore) -> dict[str, str | None]:
    return {
        "storeDir": await store.store_dir(),
        "uri": await store.get_uri(),
        "rootDir": _text(await store.root_dir()),
        "stateDir": _text(await store.state_dir()),
        "logDir": _text(await store.log_dir()),
        "realStoreDir": _text(await store.real_store_dir()),
        "buildDir": _text(await store.build_dir()),
    }


def _filesystem_path(path: str, method: str) -> str:
    """Nix resolves the empty string against the working directory, so refuse it."""
    if path == "":
        raise BadStorePath(f"{method}: the empty string is not a path")
    return path


async def _add_to_store(  # noqa: PLR0913 -- tracked complexity/arg-count debt, see TODO.md
    *,
    path: str,
    name: str | None,
    method: str,
    hash_algo: str,
    dry_run: bool,
    store_uri: str,
) -> None:
    # os.path.basename and not Path.name: a trailing slash leaves no name, and Nix refuses the empty one.
    added_name = os.path.basename(path) if name is None else name  # noqa: PTH119 -- see above
    arguments = (
        added_name,
        _filesystem_path(path, "compute_store_path" if dry_run else "add_to_store"),
        ContentAddressMethod(method),
        HashAlgorithm(hash_algo),
    )
    async with huggorm_store_session(store_uri) as (_session, store):
        if dry_run:
            result_path = await store.compute_store_path(*arguments)
        else:
            result_path = await store.add_path_to_store(*arguments)
        print_json({"path": await store.print_store_path(result_path)})


def _directory_entry_to_json(path: Path) -> dict[str, object]:
    result: dict[str, object] = {"name": path.name}
    if path.is_symlink():
        result["type"] = "symlink"
        result["target"] = str(path.readlink())
    elif path.is_dir():
        result["type"] = "directory"
    elif path.is_file():
        result["type"] = "regular"
        result["size"] = path.stat().st_size
    else:
        result["type"] = "unknown"
    return result


async def _closure_path_infos(store: AsyncStore, path: str) -> dict[str, int]:
    paths = await store.compute_fs_closure([await _store_path(store, path)])
    return {await store.print_store_path(p): (await store.query_path_info(p)).nar_size() for p in paths}


async def run_print_roots(command: PrintRoots) -> None:
    """The body of :meth:`pynix.store.PrintRoots.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        roots = [
            {"link": root.link(), "path": await store.print_store_path(root.path())}
            for root in await store.find_roots(censor=False)
        ]
        print_json({"roots": roots})


async def run_print_dead(command: PrintDead) -> None:
    """The body of :meth:`pynix.store.PrintDead.run`."""
    action = GCAction.DELETE_DEAD if command.rip else GCAction.RETURN_DEAD
    async with huggorm_store_session(command.store) as (_session, store):
        result = await store.collect_garbage(GCOptions(action))
        print_json({"paths": result.paths(), "bytesFreed": result.bytes_freed()})


async def run_print_alive(command: PrintAlive) -> None:
    """The body of :meth:`pynix.store.PrintAlive.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        result = await store.collect_garbage(GCOptions(GCAction.RETURN_LIVE))
        print_json({"paths": result.paths()})


async def run_info(command: Info) -> None:
    """The body of :meth:`pynix.store.Info.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        dirs = await _store_dirs_to_json(store)
        print_json({"uri": dirs["uri"], "storeDir": dirs["storeDir"], "dirs": dirs})


async def run_dirs(command: Dirs) -> None:
    """The body of :meth:`pynix.store.Dirs.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        print_json(await _store_dirs_to_json(store))


async def run_is_valid_path(command: IsValidPath) -> None:
    """The body of :meth:`pynix.store.IsValidPath.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        valid = await store.is_valid_path(await _store_path(store, command.path))
        print_json({"path": command.path, "valid": valid})


async def run_follow_links_to_store_path(command: FollowLinksToStorePath) -> None:
    """The body of :meth:`pynix.store.FollowLinksToStorePath.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        path = await store.follow_links_to_store_path(_filesystem_path(command.path, "follow_links_to_store_path"))
        print_json({"path": await store.print_store_path(path)})


async def run_compute_fs_closure(command: ComputeFsClosure) -> None:
    """The body of :meth:`pynix.store.ComputeFsClosure.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        paths = await store.compute_fs_closure(
            [await _store_path(store, command.path)],
            flip_direction=command.flip_direction,
            include_outputs=command.include_outputs,
            include_derivers=command.include_derivers,
        )
        _print_paths(await _printed(store, paths))


async def run_query_missing(command: QueryMissing) -> None:
    """The body of :meth:`pynix.store.QueryMissing.run`."""
    if not command.paths:
        raise SystemExit("query-missing requires at least one path")

    async with huggorm_store_session(command.store) as (_session, store):
        resp = await store.query_missing([await _derived_path(store, path) for path in command.paths])
        print_json(
            {
                "willBuild": await _printed(store, resp.will_build()),
                "willSubstitute": await _printed(store, resp.will_substitute()),
                "unknown": await _printed(store, resp.unknown()),
                "downloadSize": resp.download_size(),
                "narSize": resp.nar_size(),
            },
        )


async def run_query_derivation_outputs(command: QueryDerivationOutputs) -> None:
    """The body of :meth:`pynix.store.QueryDerivationOutputs.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        outputs = await store.query_derivation_output_map(await _store_path(store, command.path))
        _print_paths(await _printed(store, outputs.values()))


async def run_query_valid_derivers(command: QueryValidDerivers) -> None:
    """The body of :meth:`pynix.store.QueryValidDerivers.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        paths = await store.query_valid_derivers(await _store_path(store, command.path))
        _print_paths(await _printed(store, paths))


async def run_list_valid_paths(command: ListValidPaths) -> None:
    """The body of :meth:`pynix.store.ListValidPaths.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        _print_paths(await _printed(store, await store.query_all_valid_paths()))


async def run_query_referrers(command: QueryReferrers) -> None:
    """The body of :meth:`pynix.store.QueryReferrers.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        paths = await store.query_referrers(await _store_path(store, command.path))
        _print_paths(await _printed(store, paths))


async def run_query_substitutable_paths(command: QuerySubstitutablePaths) -> None:
    """The body of :meth:`pynix.store.QuerySubstitutablePaths.run`."""
    if not command.paths:
        raise SystemExit("query-substitutable-paths requires at least one path")

    async with huggorm_store_session(command.store) as (_session, store):
        paths = await store.query_substitutable_paths([await _store_path(store, path) for path in command.paths])
        _print_paths(await _printed(store, paths))


async def run_add_temp_root(command: AddTempRoot) -> None:
    """The body of :meth:`pynix.store.AddTempRoot.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        await store.add_temp_root(await _store_path(store, command.path))
        print_json({"path": command.path, "added": True})


async def run_add_perm_root(command: AddPermRoot) -> None:
    """The body of :meth:`pynix.store.AddPermRoot.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        gc_root = await store.add_perm_root(await _store_path(store, command.path), command.gc_root)
        print_json({"path": command.path, "gcRoot": str(gc_root)})


async def run_add_indirect_root(command: AddIndirectRoot) -> None:
    """The body of :meth:`pynix.store.AddIndirectRoot.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        await store.add_indirect_root(command.path)
        print_json({"path": command.path, "added": True})


async def run_path_from_hash_part(command: PathFromHashPart) -> None:
    """The body of :meth:`pynix.store.PathFromHashPart.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        path = await store.query_path_from_hash_part(command.hash_part)
        print_json({"path": None if path is None else await store.print_store_path(path)})


async def run_ensure_path(command: EnsurePath) -> None:
    """The body of :meth:`pynix.store.EnsurePath.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        await store.ensure_path(await _store_path(store, command.path))
        print_json({"path": command.path, "valid": True})


async def run_cat(command: Cat) -> None:
    """The body of :meth:`pynix.store.Cat.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        resolved = await huggorm_local_store_path(store, command.path)

    if not resolved.is_file():
        raise SystemExit(f"{command.path} is not a regular file")
    with resolved.open("rb") as f:
        sys.stdout.buffer.write(f.read())


async def run_ls(command: Ls) -> None:
    """The body of :meth:`pynix.store.Ls.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        resolved = await huggorm_local_store_path(store, command.path)

    if resolved.is_dir():
        entries = sorted(resolved.iterdir(), key=lambda path: path.name)
    elif resolved.exists():
        entries = [resolved]
    else:
        raise SystemExit(f"{command.path} does not exist")

    if command.json:
        print_json({"entries": [_directory_entry_to_json(entry) for entry in entries]})
        return
    for entry in entries:
        sys.stdout.write(entry.name)
        sys.stdout.write("\n")


async def run_add(command: Add) -> None:
    """The body of :meth:`pynix.store.Add.run`."""
    await _add_to_store(
        path=command.path,
        name=command.name,
        method=command.mode,
        hash_algo=command.hash_algo,
        dry_run=command.dry_run,
        store_uri=command.store,
    )


async def run_add_file(command: AddFile) -> None:
    """The body of :meth:`pynix.store.AddFile.run`."""
    await _add_to_store(
        path=command.path,
        name=command.name,
        method="flat",
        hash_algo=command.hash_algo,
        dry_run=command.dry_run,
        store_uri=command.store,
    )


async def run_add_path(command: AddPath) -> None:
    """The body of :meth:`pynix.store.AddPath.run`."""
    await _add_to_store(
        path=command.path,
        name=command.name,
        method="nar",
        hash_algo=command.hash_algo,
        dry_run=command.dry_run,
        store_uri=command.store,
    )


async def run_diff_closures(command: DiffClosures) -> None:
    """The body of :meth:`pynix.store.DiffClosures.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        before = await _closure_path_infos(store, command.before)
        after = await _closure_path_infos(store, command.after)

    before_paths = set(before)
    after_paths = set(after)
    added = sorted(after_paths - before_paths)
    removed = sorted(before_paths - after_paths)
    before_size = sum(before.values())
    after_size = sum(after.values())
    print_json(
        {
            "before": command.before,
            "after": command.after,
            "added": [{"path": path, "narSize": after[path]} for path in added],
            "removed": [{"path": path, "narSize": before[path]} for path in removed],
            "beforeNarSize": before_size,
            "afterNarSize": after_size,
            "narSizeDelta": after_size - before_size,
        },
    )


async def run_optimise(command: Optimise) -> None:
    """The body of :meth:`pynix.store.Optimise.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        await store.optimise_store()
        print_json({"optimised": True})


async def run_verify(command: Verify) -> None:
    """The body of :meth:`pynix.store.Verify.run`."""
    async with huggorm_store_session(command.store) as (_session, store):
        errors = await store.verify_store(command.check_contents, repair=command.repair)
        print_json({"errors": errors})
