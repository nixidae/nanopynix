"""The implementation of ``pynix store sign`` and ``pynix store copy-sigs``:
``CmdSign`` and ``CmdCopySigs`` of ``src/nix/sigs.cc``.

The paths are what ``StorePathsCommand`` selects: the named paths and the
outputs of ``--file`` or ``--flake``, their closure with ``--recursive``, or
every valid path with ``--all``. Nix builds a missing output first; pynix
builds nothing, as ``pynix copy`` does, and refuses an output that is not
there.

``copy-sigs`` asks the substituters one path at a time, where Nix asks in a
thread pool.
"""

from __future__ import annotations

import anyio
from huggorm import AsyncEvalState, AsyncStore, PathInfo, SecretKey, Signature, StorePath, Verbosity, log_message
from huggorm.errors import InvalidPath, NixError

from pynix._impl.copy import evaluated_paths
from pynix._util import error_exit, huggorm_eval_session
from pynix.store import CopySigs, Sign, StorePaths
from pynix.target import EvaluationTarget


async def selected_paths(command: StorePaths, state: AsyncEvalState, store: AsyncStore) -> list[StorePath]:
    """``BuiltPathsCommand::run`` and ``StorePathsCommand::run``: dependencies first."""
    target = EvaluationTarget.from_command(command)
    if command.all:
        if command.paths or target.file is not None or target.flake is not None:
            error_exit("'--all' does not expect arguments")
        roots = await store.query_all_valid_paths()
    else:
        named = sorted({*command.paths, *await evaluated_paths(target, state, store, command.store)})
        if not named:
            error_exit("name a store path, or name --file, --flake or --all")
        try:
            roots = [await store.parse_store_path(path) for path in named]
            if command.recursive:
                roots = await store.compute_fs_closure(roots)
        except NixError as exc:
            error_exit(str(exc), cause=exc)
    return list(reversed(await store.topo_sort_paths(roots)))


def _info(message: str) -> None:
    """``printInfo``: Nix's logger, so ``--quiet`` hides it as it hides Nix's."""
    log_message(Verbosity.INFO, message)


async def run_sign(command: Sign) -> None:
    """The body of :meth:`pynix.store.Sign.run`."""
    try:
        key = SecretKey(await anyio.Path(command.key_file).read_text())
    except OSError as exc:
        error_exit(f"reading file '{command.key_file}': {exc.strerror}", cause=exc)
    except NixError as exc:
        error_exit(str(exc), cause=exc)
    pure = EvaluationTarget.from_command(command).pure(impure=command.impure)
    async with huggorm_eval_session(command.store, pure=pure) as (_session, store, state):
        added = 0
        for path in await selected_paths(command, state, store):
            info = await store.query_path_info(path)
            sig = key.sign_detached(await store.fingerprint(info))
            if sig not in info.sigs():
                await store.add_signatures(path, [sig])
                added += 1
        _info(f"added {added} signatures")


def _same_object(info: PathInfo, other: PathInfo) -> bool:
    """Whether two stores describe one object: a signature of one fits the other."""
    return (
        info.nar_hash() == other.nar_hash()
        and info.nar_size() == other.nar_size()
        and info.references() == other.references()
    )


async def run_copy_sigs(command: CopySigs) -> None:
    """The body of :meth:`pynix.store.CopySigs.run`."""
    if not command.substituter:
        error_exit("you must specify at least one substituter using '-s'")
    pure = EvaluationTarget.from_command(command).pure(impure=command.impure)
    async with huggorm_eval_session(command.store, pure=pure) as (session, store, state):
        substituters = [session.store(uri) for uri in command.substituter]
        added = 0
        for path in await selected_paths(command, state, store):
            info = await store.query_path_info(path)
            have = set(info.sigs())
            new: set[Signature] = set()
            for substituter in substituters:
                try:
                    other = await substituter.query_path_info(path)
                except InvalidPath:
                    continue
                if _same_object(info, other):
                    new.update(sig for sig in other.sigs() if sig not in have)
            if new:
                await store.add_signatures(path, sorted(new))
                added += len(new)
        _info(f"imported {added} signatures")
