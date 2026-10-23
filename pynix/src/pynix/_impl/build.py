"""The implementation of the ``pynix build`` command.

``pynix.build`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import contextlib
import difflib
import functools
import shutil
import tempfile
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

# A real import, not a TYPE_CHECKING one: `libpynix` resolves the annotations
# of a command to build its parser, so `Path` has to exist as an object and not
# just as a lazy PEP 563 string.
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import anyio.to_thread
import huggorm
import structlog
from anyio import Path as AnyioPath
from huggorm.errors import NixError
from huggorm.fod import (
    FodSourceUpdateError,
    derivation_name_from_path,
    extract_fod_hash_mismatch,
    extract_unique_fod_hash_mismatch,
    find_fod_hash_literal,
    mismatch_is_target_fod,
    replace_fod_hash,
)
from nanopynix_helpers.build import FodBuildError, build_with_fod_update

import nanopynix
from nanopynix._typechecking import BEARTYPING
from pynix import _evaluate, _impl
from pynix._build_monitor import BuildPlan, MonitorState
from pynix._impl.copy import StoreEnds, closure_of, copy_verified
from pynix._util import (
    build_derivation,
    error_console,
    error_exit,
    huggorm_build_session,
    nix_session,
    print_json,
    record_message,
    report_and_exit,
)
from pynix.build import Build

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import AsyncGenerator

    from huggorm import AsyncEvalState, AsyncSession, AsyncStore, AsyncValue, CapturedLogs
    from huggorm.fod import FodHashMismatch

    # The protocol, and not one engine's class, for the reason
    # `nanopynix_helpers.build` gives. Issue #232.
    from nanopynix.protocols import AsyncValue as ValueProxy
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    base_attr_search,
    configuration_kind,
    configuration_message,
    evaluate_target,
)

logger = structlog.get_logger("pynix.build")
#: Explicitly the daemon, never "auto" -- see _promote_to_host_store.
_HOST_STORE_URI = "daemon"
_MAX_FOD_UPDATES = 10
#: Nix logs the cause of a failed dependency, such as a hash mismatch, as an error.
_CAUSE_LEVEL = int(nanopynix.LogLevel.WARN)


@asynccontextmanager
async def _overlay_namespace(
    enabled: bool,
    overlay_dir: Path | None,
) -> AsyncGenerator[nanopynix.OverlayNamespace | None]:
    """Yield the overlay layout for this build, and clean it up if it is ours.

    A directory the user named survives, which is the point of naming one. An
    unnamed one is temporary and goes away with the build.
    """
    if not enabled:
        yield None
        return

    if overlay_dir is not None:
        await AnyioPath(overlay_dir).mkdir(parents=True, exist_ok=True)
        yield nanopynix.OverlayNamespace.under(overlay_dir)
        return

    root = await anyio.to_thread.run_sync(functools.partial(tempfile.mkdtemp, prefix="pynix-overlay-"))
    try:
        yield nanopynix.OverlayNamespace.under(root)
    finally:
        await anyio.to_thread.run_sync(_force_rmtree, root)


def _force_rmtree(root: str) -> None:
    """Remove *root*, including the read-only directories a store leaves.

    Store directories are ``r-xr-xr-x``, and removing what is inside one needs
    the write bit on the directory itself. This process owns them, because the
    namespace mapped it to root, so it can put the bit back.
    """
    for dirpath, dirnames, _ in Path(root).walk():
        for name in dirnames:
            with contextlib.suppress(OSError):
                (dirpath / name).chmod(0o700)
    shutil.rmtree(root, ignore_errors=True)


async def _promote_to_host_store(nix: Any, store: Any, outputs: dict[str, str]) -> None:
    """Copy the outputs of a namespaced build into the host store.

    ``daemon`` by name rather than ``auto``. The worker is root inside its own
    user namespace, so ``auto`` would resolve to a *local* store at
    ``/nix/store`` -- which is the overlay mount, not the host store.

    Signature checking is off because these paths were built here a moment ago
    and nothing has signed them. The daemon still decides: it accepts unsigned
    paths from a trusted user and refuses them otherwise.
    """
    paths = sorted(set(outputs.values()))
    if not paths:
        return
    async with nix.store(_HOST_STORE_URI) as host:
        logger.info("pynix build promoting outputs to the host store", paths=len(paths))
        await store.copy_closure(paths, host, check_sigs=False)


async def _copy_outputs(session: AsyncSession, store: AsyncStore, outputs: dict[str, str], uri: str) -> dict[str, Any]:
    """Copy the closure of *outputs* from *store* to *uri*, and report what arrived.

    Signature checking is off for the reason ``_promote_to_host_store`` gives,
    and the destination still decides. A path that it refuses quietly is
    caught by :func:`pynix._impl.copy.copy_verified`.
    """
    requested = sorted(set(outputs.values()))
    closure = await closure_of(store, requested)
    ends = StoreEnds(store, session.store(uri), check_sigs=False)
    present, arrived = await copy_verified(ends, requested, closure, uri)
    return {"to": uri, "copied": arrived, "alreadyPresent": present}


@dataclass(frozen=True, slots=True)
class _NanopynixEnds:
    """:class:`pynix._impl.copy.StoreEnds` on nanopynix's stores, for ``--namespaced``."""

    source: Any
    destination: Any

    async def present(self, paths: list[str]) -> list[str]:
        return sorted([path for path in paths if await self.destination.is_valid_path(path)])

    async def copy(self, paths: list[str]) -> None:
        await self.source.copy_closure(paths, self.destination, check_sigs=False)


async def _copy_outputs_on_nanopynix(nix: Any, store: Any, outputs: dict[str, str], uri: str) -> dict[str, Any]:
    """:func:`_copy_outputs` on nanopynix's stores."""
    requested = sorted(set(outputs.values()))
    closure = sorted({str(member) for path in requested for member in await store.compute_fs_closure(path)})
    async with nix.store(uri) as destination:
        present, arrived = await copy_verified(_NanopynixEnds(store, destination), requested, closure, uri)
    return {"to": uri, "copied": arrived, "alreadyPresent": present}


async def _require_a_local_file(target: EvaluationTarget) -> None:
    """Refuse ``--update-fod`` unless ``--file`` names a local file.

    ``--update-fod`` writes the new hash back into the source. A fetched tree
    lives in the store, which is read-only, and a lookup path resolves to one.
    """
    try:
        reference = await target.file_reference()
    except EvaluationTargetError as exc:
        report_and_exit(exc)
    if reference is None or reference.local_path is None:
        error_exit("--update-fod requires --file to name a local file")


async def _evaluate_build_target_on_nanopynix(target: EvaluationTarget, session: Any) -> ValueProxy:
    try:
        root = await evaluate_target(target, session, auto_call_file=True, attr_search=base_attr_search())
    except EvaluationTargetError as exc:
        raise BuildTargetError(str(exc)) from exc
    # **Before the build, and not after it.** Nix answers a configuration with
    # `selected value is not a derivation`, which is true and is the end of the
    # road: it says neither what the value is nor which path would work. The
    # test costs two attribute reads, and only a value that is an attribute set
    # reaches even the first one.
    kind = await configuration_kind(root)
    if kind is not None:
        raise BuildTargetError(configuration_message(kind, target.selected_attr()))
    return root


class BuildTargetError(EvaluationTargetError):
    pass


@dataclass(frozen=True, slots=True)
class BuildSession:
    """A huggorm evaluator, and the two stores that a build reads and writes."""

    session: AsyncSession
    build_store: AsyncStore
    evaluation_store: AsyncStore
    state: AsyncEvalState


async def _evaluate_build_target(target: EvaluationTarget, state: AsyncEvalState) -> AsyncValue:
    try:
        root = await _evaluate.evaluate_target(
            target, state, auto_call_file=True, attr_search=_evaluate.base_attr_search()
        )
    except EvaluationTargetError as exc:
        raise BuildTargetError(str(exc)) from exc
    # Before the build, for the reason `_evaluate_build_target_on_nanopynix` gives.
    kind = await _evaluate.configuration_kind(state, root)
    if kind is not None:
        raise BuildTargetError(configuration_message(kind, target.selected_attr()))
    return root


async def _plan(build: BuildSession, drv_path: str) -> BuildPlan:
    """:func:`plan_build` on huggorm's stores."""
    missing = await build.build_store.query_missing([await build.build_store.parse_derived_path(f"{drv_path}^*")])
    builds = frozenset([await build.build_store.print_store_path(p) for p in missing.will_build()])
    downloads = frozenset([await build.build_store.print_store_path(p) for p in missing.will_substitute()])
    store_dir = drv_path.rsplit("/", 1)[0]
    paths: dict[str, frozenset[str]] = {}

    async def input_drvs(path: str) -> list[str]:
        derivation = await build.evaluation_store.read_derivation(await build.evaluation_store.parse_store_path(path))
        try:
            return [f"{store_dir}/{base}" for base in await derivation.input_drvs()]
        finally:
            await derivation.aclose()

    async def output_paths(path: str) -> frozenset[str]:
        # The static map: a floating content-addressed output has no path
        # yet, and leaves its node without one, as nanopynix's plan does.
        if path not in paths:
            try:
                found = await build.evaluation_store.query_derivation_output_map(
                    await build.evaluation_store.parse_store_path(path)
                )
            except NixError:
                found = {}
            paths[path] = frozenset([await build.evaluation_store.print_store_path(p) for p in found.values()])
        return paths[path]

    children: dict[str, tuple[str, ...]] = {}
    outputs: dict[str, frozenset[str]] = {}
    for node in (drv_path, *sorted(builds - {drv_path})):
        outputs[node] = await output_paths(node)
        kids: list[str] = []
        for input_drv in await input_drvs(node):
            if input_drv in builds:
                kids.append(input_drv)
            elif downloads and (input_outputs := await output_paths(input_drv)) & downloads:
                outputs[input_drv] = input_outputs
                kids.append(input_drv)
        children[node] = tuple(kids)
    return BuildPlan(roots=(drv_path,), children=children, outputs=outputs, builds=builds, downloads=downloads)


async def _drv_path(build: BuildSession, root: AsyncValue) -> str:
    """The ``.drv`` of *root*. ``drv_path`` is Nix's own test of a
    derivation, and raises Nix's message for any other value."""
    return await build.evaluation_store.print_store_path(await root.drv_path())


def _messages(logs: CapturedLogs) -> list[str]:
    return [nanopynix.strip_ansi(record_message(record)) for record in logs.records if record.action() == "msg"]


async def _updated_source(source_file: Path, mismatch: FodHashMismatch) -> str:
    """The text of *source_file* with the hash of *mismatch* replaced, and its diff printed."""
    try:
        source = await AnyioPath(source_file).read_text()
        literal = find_fod_hash_literal(
            source,
            mismatch.specified,
            derivation_name=derivation_name_from_path(mismatch.drv_path),
        )
        updated = replace_fod_hash(source, literal, mismatch.got)
    except (OSError, FodSourceUpdateError) as exc:
        raise BuildTargetError(f"cannot update fixed-output hash: {exc}") from exc
    _print_diff(source_file, source, updated)
    return updated


async def _build_target(
    target: EvaluationTarget,
    build: BuildSession,
    *,
    update_fod: bool,
    dry_run: bool,
    monitor: MonitorState | None = None,
) -> tuple[dict[str, str], int]:
    """Build a target, applying only unambiguous plain-string FOD updates.

    Each attempt evaluates again, after the evaluator forgets every file it
    read, so it reads its own edit. A mismatch that names a derivation must be
    a fixed-output member of the target's closure before the source changes.
    """
    reference = await target.file_reference()
    source_file = reference.local_path if reference is not None else None
    updates = 0
    while True:
        root: AsyncValue | None = None
        # A failed dependency's cause is not in the error of the target that
        # needed it. It arrives on the process stream, and only a capture has
        # all of it when the build returns.
        async with build.session.capture_process(level=_CAUSE_LEVEL) as logs:
            try:
                root = await _evaluate_build_target(target, build.state)
                drv_path = await _drv_path(build, root)
                if monitor is not None:
                    monitor.plan = await _plan_or_none(build, drv_path)
                return await build_derivation(build.build_store, drv_path, eval_store=build.evaluation_store), updates
            except NixError as exc:
                error = exc
        mismatch = extract_fod_hash_mismatch(str(error)) or extract_unique_fod_hash_mismatch(_messages(logs))
        if not update_fod or mismatch is None:
            raise error
        if mismatch.drv_path is not None and (
            root is None or not await mismatch_is_target_fod(build.evaluation_store, build.state, root, mismatch)
        ):
            raise BuildTargetError(
                "fixed-output hash mismatch was not found in the evaluated target derivation closure"
            ) from error
        if source_file is None:
            raise BuildTargetError("--update-fod currently requires --file") from error
        if updates >= _MAX_FOD_UPDATES:
            raise BuildTargetError(f"stopped after {_MAX_FOD_UPDATES} fixed-output hash updates") from error
        updated = await _updated_source(source_file, mismatch)
        updates += 1
        if dry_run:
            return {}, updates
        await AnyioPath(source_file).write_text(updated)
        for path in await build.state.cached_files():
            await build.state.forget_file(path)


async def _build_on_huggorm(
    command: Build, target: EvaluationTarget, settings: dict[str, str]
) -> tuple[dict[str, str], int, dict[str, Any] | None]:
    monitor = MonitorState(started=time.monotonic(), print_build_logs=command.print_build_logs) if command.nom else None
    async with huggorm_build_session(
        command.store,
        eval_store_uri=command.eval_store,
        settings=settings,
        verbosity=command.verbosity,
        print_build_logs=command.print_build_logs,
        monitor=monitor,
    ) as (session, build_store, eval_store, state):
        logger.info("pynix build evaluating target")
        outputs, updates = await _build_target(
            target,
            BuildSession(session, build_store, eval_store, state),
            update_fod=command.update_fod,
            dry_run=command.dry_run,
            monitor=monitor,
        )
        logger.info("pynix build finished")
        copied = None
        if command.copy is not None and outputs:
            copied = await _copy_outputs(session, build_store, outputs, command.copy)
    return outputs, updates, copied


async def _build_target_on_nanopynix(  # noqa: PLR0913 -- tracked complexity/arg-count debt, see TODO.md
    target: EvaluationTarget,
    session: Any,
    *,
    nix: Any,
    evaluation_store: Any,
    build_store: Any = None,
    update_fod: bool,
    dry_run: bool,
    monitor: MonitorState | None = None,
) -> tuple[dict[str, str], int]:
    """Build a target, applying only unambiguous plain-string FOD updates."""

    async def _evaluate() -> ValueProxy:
        root = await _evaluate_build_target_on_nanopynix(target, session)
        logger.info("pynix build target evaluated")
        if monitor is not None:
            monitor.plan = await _plan_or_none_on_nanopynix(evaluation_store, build_store or evaluation_store, root)
        return root

    # The local file, and not the raw argument. --update-fod rewrites the hash
    # in the source, so a fetched tree in the store and a lookup path have no
    # file to rewrite. `run()` refuses both before it reaches this point.
    reference = await target.file_reference()
    source_file = reference.local_path if reference is not None else None

    try:
        return await build_with_fod_update(
            _evaluate,
            nix=nix,
            eval_session=session,
            evaluation_store=evaluation_store,
            build_store=build_store,
            update_fod=update_fod,
            source_file=source_file,
            dry_run=dry_run,
            on_hash_update=_print_diff,
        )
    except FodBuildError as exc:
        raise BuildTargetError(str(exc)) from exc


async def plan_build(evaluation_store: Any, build_store: Any, drv_path: str) -> BuildPlan:
    """What building ``drv_path`` will do, as the tree nom draws.

    A node is a derivation to build, or one whose outputs will be downloaded.
    Its children are its inputs that are nodes too. This reads the ``.drv`` of
    each build, and of each input a build does not build, which is how a
    download is tied to the derivation that produced it. nom reads the same
    files as the builds start; reading them first also gives the totals.
    """
    missing = await build_store.query_missing([f"{drv_path}^*"])
    builds = frozenset(str(p) for p in missing.will_build)
    downloads = frozenset(str(p) for p in missing.will_substitute)
    derivations: dict[str, Any] = {}

    async def read(path: str) -> Any:
        if path not in derivations:
            derivations[path] = await evaluation_store.read_derivation(path)
        return derivations[path]

    def output_paths(derivation: Any) -> frozenset[str]:
        return frozenset(o.path for o in derivation.outputs.values() if o.path)

    children: dict[str, tuple[str, ...]] = {}
    outputs: dict[str, frozenset[str]] = {}
    for node in (drv_path, *sorted(builds - {drv_path})):
        derivation = await read(node)
        outputs[node] = output_paths(derivation)
        kids: list[str] = []
        for input_drv in derivation.input_drvs:
            if input_drv in builds:
                kids.append(input_drv)
            elif downloads and (input_outputs := output_paths(await read(input_drv))) & downloads:
                outputs[input_drv] = input_outputs
                kids.append(input_drv)
        children[node] = tuple(kids)
    return BuildPlan(roots=(drv_path,), children=children, outputs=outputs, builds=builds, downloads=downloads)


async def _plan_or_none_on_nanopynix(evaluation_store: Any, build_store: Any, root: ValueProxy) -> BuildPlan | None:
    """The plan of ``root``, or None, which leaves the monitor a flat list.

    A missing plan costs the tree and nothing else, so an error here must not
    stop the build.
    """
    try:
        drv_path = await root.attr("drvPath").as_string()
        return await plan_build(evaluation_store, build_store, drv_path)
    except nanopynix.NixError as exc:
        logger.warning("pynix build could not plan the dependency graph", error=str(exc))
        return None


async def _plan_or_none(build: BuildSession, drv_path: str) -> BuildPlan | None:
    """:func:`_plan_or_none_on_nanopynix` on huggorm."""
    try:
        return await _plan(build, drv_path)
    except NixError as exc:
        logger.warning("pynix build could not plan the dependency graph", error=str(exc))
        return None


def _print_diff(path: Path, before: str, after: str) -> None:
    error_console.print(
        "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=str(path),
                tofile=str(path),
            ),
        ),
        markup=False,
        highlight=False,
        end="",
    )


async def _add_sandbox_paths(nix: Any, paths: list[str]) -> None:
    """Add *paths* to the worker's ``sandbox-paths``, after what nix.conf set.

    Added, never replacing: nix.conf carries ``/bin/sh=<busybox>`` there,
    and a build with any ``--sandbox-path`` had no ``/bin/sh``. Measured: a
    kernel's Kconfig runs ``sh`` for every ``$(shell)`` and failed on it.
    Written before a store opens, because a store reads its settings once.
    """
    configured = (await nix.settings()).get("sandbox-paths", "").split()
    await nix.set_settings(
        nanopynix.NixGlobalSettings(sandbox_paths=[*configured, *(p for p in paths if p not in configured)])
    )


async def run_build(command: Build) -> None:
    """The body of :meth:`pynix.build.Build.run`."""
    target = EvaluationTarget.from_command(command)
    try:
        target.validate(required=True)
    except EvaluationTargetError as exc:
        report_and_exit(exc)
    if command.update_fod:
        await _require_a_local_file(target)
    if command.dry_run and not command.update_fod:
        error_exit("--dry-run requires --update-fod")
    if command.dry_run and command.copy is not None:
        error_exit("--dry-run builds nothing, so --copy has nothing to copy")

    namespaced = _resolve_namespaced(command)

    try:
        settings = _impl.settings.nix_settings(
            huggorm.list_settings(),
            substituters=command.substituters,
            trusted_public_keys=command.trusted_public_keys,
        )
    except _impl.settings.SettingError as exc:
        error_exit(str(exc))

    # Outside the session, so the error passes through its log forwarding:
    # --nom counts it in the last frame, and the message prints after that.
    try:
        # A namespaced build still runs on nanopynix: issue #1051.
        if namespaced:
            outputs, updates, copied = await _build_in_session(
                command, target, namespaced=namespaced, settings=settings
            )
        else:
            outputs, updates, copied = await _build_on_huggorm(command, target, settings)
    except BuildTargetError as exc:
        report_and_exit(exc)

    report: dict[str, Any] = {"outputs": outputs, "updatedFods": updates, "dryRun": command.dry_run}
    if copied is not None:
        report["copy"] = copied
    print_json(report)


async def _build_in_session(
    command: Build,
    target: EvaluationTarget,
    *,
    namespaced: bool,
    settings: dict[str, str],
) -> tuple[dict[str, str], int, dict[str, Any] | None]:
    copied: dict[str, Any] | None = None
    async with AsyncExitStack() as stack:
        namespace = await stack.enter_async_context(_overlay_namespace(namespaced, command.overlay_dir))
        # Passed only when there is one, which is the convention
        # nix_session documents: the test suite substitutes a double for
        # it, and a keyword that is always present makes every such double
        # wrong even for the ordinary build that never wanted a namespace.
        session_kwargs: dict[str, Any] = {} if namespace is None else {"namespace": namespace}
        monitor = (
            MonitorState(started=time.monotonic(), print_build_logs=command.print_build_logs) if command.nom else None
        )
        if monitor is not None:
            session_kwargs["monitor"] = monitor
        nix = await stack.enter_async_context(
            nix_session(
                settings=nanopynix.NixSettings.model_validate(settings),
                verbosity=command.verbosity,
                print_build_logs=command.print_build_logs,
                **session_kwargs,
            )
        )
        if command.sandbox_path:
            await _add_sandbox_paths(nix, command.sandbox_path)
        # None, not "auto", when namespaced: the session already defaults
        # to its overlay store, and naming "auto" here would open a
        # different store instead.
        store_uri = None if namespaced else command.store
        promote = namespaced and command.copy_back
        if command.eval_store is None:
            async with nix.store(store_uri) as store:
                async with nix.eval(store) as session:
                    logger.info("pynix build evaluating target")
                    outputs, updates = await _build_target_on_nanopynix(
                        target,
                        session,
                        nix=nix,
                        evaluation_store=store,
                        update_fod=command.update_fod,
                        dry_run=command.dry_run,
                        monitor=monitor,
                    )
                logger.info("pynix build finished")
                if promote:
                    await _promote_to_host_store(nix, store, outputs)
                if command.copy is not None and outputs:
                    copied = await _copy_outputs_on_nanopynix(nix, store, outputs, command.copy)
        else:
            async with (
                nix.store(command.eval_store) as eval_store,
                nix.store(store_uri) as build_store,
            ):
                async with nix.eval(eval_store) as session:
                    logger.info("pynix build evaluating target")
                    outputs, updates = await _build_target_on_nanopynix(
                        target,
                        session,
                        nix=nix,
                        evaluation_store=eval_store,
                        build_store=build_store,
                        update_fod=command.update_fod,
                        dry_run=command.dry_run,
                        monitor=monitor,
                    )
                logger.info("pynix build finished")
                if promote:
                    await _promote_to_host_store(nix, build_store, outputs)
                if command.copy is not None and outputs:
                    copied = await _copy_outputs_on_nanopynix(nix, build_store, outputs, command.copy)
    return outputs, updates, copied


def _resolve_namespaced(command: Build) -> bool:
    """Decide whether this build gets its own namespace, and reject the
    flag combinations that cannot mean anything."""
    namespaced = command.namespaced or command.overlay_dir is not None
    if command.sandbox_path and not namespaced:
        error_exit("--sandbox-path requires --namespaced: the daemon owns the sandbox of the host store")
    # What the caller typed, and not what `command.store` holds: a namespaced
    # build owns its store, so naming one on the command line is a
    # contradiction. A store that the environment or the configuration file
    # supplied is not a request about *this* build, and must not refuse it.
    if namespaced and "store" in command.explicit_options:
        error_exit("--namespaced builds in its own overlay store, so it cannot be combined with --store")
    return namespaced
