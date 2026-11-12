"""The implementation of ``pynix store verify``: ``CmdVerify`` of
``src/nix/verify.cc``.

The paths are what ``StorePathsCommand`` selects (``sigs.selected_paths``).
Nix checks them in a thread pool, so the order of its messages varies;
pynix checks them one at a time, dependencies first.

The exit code is Nix's: 1 when a path was modified, 2 when a path is
untrusted, 4 when a check failed, or a sum of them.
"""

from __future__ import annotations

import sys

from huggorm import AsyncStore, PathInfo, Signature, Verbosity, log_message
from huggorm.errors import InvalidPath, NixError

from pynix._impl.sigs import selected_paths
from pynix._util import huggorm_eval_session, print_nix_error
from pynix.store import Verify
from pynix.target import EvaluationTarget

#: ``ValidPathInfo::maxSigs``: what a content-addressed path counts as.
_MAX_SIGS = sys.maxsize

_CORRUPTED = 1
_UNTRUSTED = 2
_FAILED = 4


async def _modified(store: AsyncStore, info: PathInfo) -> bool:
    """Whether the NAR of *info*'s path has another hash than the registered one."""
    expected = info.nar_hash()
    got = await store.compute_nar_hash(info.path(), expected.algorithm())
    if got == expected:
        return False
    log_message(
        Verbosity.ERROR,
        f"path '{await store.print_store_path(info.path())}' was modified! "
        f"expected hash '{expected.to_string()}', got '{got.to_string()}'",
    )
    return True


async def _trusted(command: Verify, store: AsyncStore, substituters: list[AsyncStore], info: PathInfo) -> bool:
    """Whether *info* is ultimately trusted, content-addressed, or signed by enough trusted keys."""
    if info.ultimate() and not command.sigs_needed:
        return True
    needed = max(command.sigs_needed, 1)
    valid = _MAX_SIGS if await store.is_content_addressed(info) else 0
    seen: set[Signature] = set()

    async def count(sigs: list[Signature]) -> None:
        nonlocal valid
        for sig in sigs:
            if sig in seen:
                continue
            seen.add(sig)
            if valid < _MAX_SIGS and await store.check_signature(info, sig):
                valid += 1

    await count(info.sigs())
    for substituter in substituters:
        if valid >= needed:
            break
        other = await _lent(substituter, info)
        if other is None:
            continue
        if await store.is_content_addressed(other):
            valid = _MAX_SIGS
        await count(other.sigs())
    return valid >= needed


async def _lent(substituter: AsyncStore, info: PathInfo) -> PathInfo | None:
    """What *substituter* knows of *info*'s path, or None when it has nothing to lend."""
    try:
        return await substituter.query_path_info(info.path())
    except InvalidPath:
        return None
    except NixError as exc:
        print_nix_error(exc)
        return None


async def run_verify(command: Verify) -> None:
    """The body of :meth:`pynix.store.Verify.run`."""
    pure = EvaluationTarget.from_command(command).pure(impure=command.impure)
    async with huggorm_eval_session(command.store, pure=pure) as (session, store, state):
        substituters = [session.store(uri) for uri in command.substituter]
        corrupted = untrusted = failed = 0
        for path in await selected_paths(command, state, store):
            try:
                info = await store.query_path_info(path)
                printed = await store.print_store_path(info.path())
                # Nix's simple logger prints an activity's text with "..." after it.
                log_message(Verbosity.INFO, f"checking '{printed}'...")
                if not command.no_contents and await _modified(store, info):
                    corrupted += 1
                if not command.no_trust and not await _trusted(command, store, substituters, info):
                    untrusted += 1
                    log_message(Verbosity.ERROR, f"path '{printed}' is untrusted")
            except NixError as exc:
                print_nix_error(exc)
                failed += 1
    code = (_CORRUPTED if corrupted else 0) | (_UNTRUSTED if untrusted else 0) | (_FAILED if failed else 0)
    if code:
        raise SystemExit(code)
