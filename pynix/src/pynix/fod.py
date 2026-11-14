"""The hash of every fixed-output derivation that does not match, from one build.

A library API, not a command: a caller that pins hashes in its own files
(a lock file, a JSON database) builds every candidate at once and reads
back each ``got``. Writing the hash anywhere is the caller's job.

Nix reports a mismatch as text only. The message of a failed target holds
its own mismatch; a failed dependency's, and a target Nix built resolved,
arrive on the process log. Both are read, with ``huggorm.fod``'s parser,
and each mismatch is named by the derivation evaluation produced.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import huggorm
from huggorm import AsyncRemoteSession, AsyncSession, AsyncStore
from huggorm.fod import FodHashMismatch, extract_fod_hash_mismatch

from pynix._build_log import CAUSE_LEVEL, BuildLog


class FodBuildError(RuntimeError):
    """The build cannot report every mismatch."""


@dataclass(frozen=True, slots=True)
class FodBuild:
    """What one build of *drv_paths* gave.

    ``built``: each requested derivation that built, its output paths by name.
    ``failed``: each requested derivation that failed, with Nix's message.
    ``mismatches``: each fixed-output derivation whose hash did not match,
    requested or a dependency, by its path as evaluated.
    """

    built: dict[str, dict[str, str]]
    failed: dict[str, str]
    mismatches: dict[str, FodHashMismatch]


async def build_fixed_outputs(
    session: AsyncSession | AsyncRemoteSession,
    build_store: AsyncStore,
    drv_paths: Sequence[str],
    *,
    eval_store: AsyncStore | None = None,
) -> FodBuild:
    """Build every output of each of *drv_paths* in one request, and report each mismatch.

    ``keep-going`` must be on before *build_store* connects: without it the
    first failure stops every other build, and their mismatches go unseen.
    A daemon reads the setting when the connection opens.
    """
    if huggorm.get_setting("keep-going") != "true":
        raise FodBuildError("keep-going is off, so the first failure would hide every other mismatch")
    log = BuildLog()
    derived = [await build_store.parse_derived_path(f"{drv_path}^*") for drv_path in drv_paths]
    other_eval_store = None if eval_store is build_store else eval_store
    async with session.forward_process(log.keep, level=CAUSE_LEVEL):
        results = await build_store.build_paths_with_results(
            derived, huggorm.BuildMode.NORMAL, eval_store=other_eval_store
        )
    built: dict[str, dict[str, str]] = {}
    failed: dict[str, str] = {}
    for result in results:
        drv_path = drv_paths[derived.index(result.path())]
        error = result.error()
        if error is not None:
            failed[drv_path] = str(error)
            continue
        success = result.success()
        if success is None:
            raise FodBuildError(f"the build of {drv_path} reported neither success nor failure")
        built[drv_path] = {
            name: await build_store.print_store_path(realisation.out_path())
            for name, realisation in success.built_outputs().items()
        }
    if missing := [drv_path for drv_path in drv_paths if drv_path not in built and drv_path not in failed]:
        raise FodBuildError(f"the build gave no result for {', '.join(missing)}")
    return FodBuild(built=built, failed=failed, mismatches=mismatches_in([*failed.values(), *log.messages], log))


def mismatches_in(messages: Sequence[str], log: BuildLog) -> dict[str, FodHashMismatch]:
    """Each mismatch in *messages*, by the derivation evaluation produced.

    Nix may say one mismatch twice, in the error and on the log. Two that
    disagree for one derivation mean the text was misread, so this raises.
    """
    found: dict[str, FodHashMismatch] = {}
    for message in messages:
        mismatch = extract_fod_hash_mismatch(message)
        if mismatch is None or mismatch.drv_path is None:
            continue
        mismatch = log.attributed(mismatch)
        drv_path = mismatch.drv_path or ""
        known = found.setdefault(drv_path, mismatch)
        if known != mismatch:
            raise FodBuildError(f"two different mismatches for {drv_path}: {known} and {mismatch}")
    return found
