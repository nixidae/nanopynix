"""The implementation of the ``pynix build`` command.

``pynix.build`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import difflib
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import huggorm
import structlog
from anyio import Path as AnyioPath
from huggorm import (
    TEMPORARY,
    AsyncEvalState,
    AsyncRemoteSession,
    AsyncSession,
    AsyncStore,
    AsyncValue,
    CapturedLogs,
    ServerOptions,
    worker,
)
from huggorm.errors import NixError
from huggorm.fod import (
    FodHashMismatch,
    FodSourceUpdateError,
    derivation_name_from_path,
    extract_fod_hash_mismatch,
    extract_unique_fod_hash_mismatch,
    find_fod_hash_literal,
    mismatch_is_target_fod,
    replace_fod_hash,
)
from huggorm.namespace import Temporary

from pynix import _evaluate
from pynix._build_monitor import BuildPlan, MonitorState
from pynix._impl.copy import StoreEnds, closure_of, copy_verified
from pynix._impl.linux_builder import LinuxBuilderError, linux_builder, requested_guest
from pynix._util import (
    build_derivation,
    configured_nix_settings,
    error_console,
    error_exit,
    forward_state_logs,
    huggorm_build_session,
    print_json,
    record_message,
    report_and_exit,
)
from pynix.build import Build
from pynix.target import (
    EvaluationTarget,
    EvaluationTargetError,
    configuration_message,
)

logger = structlog.get_logger("pynix.build")
_HOST_STORE_URI = "daemon"
_MAX_FOD_UPDATES = 10
#: Nix logs the cause of a failed dependency, such as a hash mismatch, as an error.
_CAUSE_LEVEL = int(huggorm.Verbosity.WARN)


def _server_options(overlay: Path | Temporary, settings: Mapping[str, str], sandbox_paths: list[str]) -> ServerOptions:
    """The huggorm worker's setup for a build in an overlay under *overlay*.

    The server reads nix.conf, then *settings*. A sandbox path goes to
    ``extra-sandbox-paths``, which appends: nix.conf carries
    ``/bin/sh=<busybox>`` in ``sandbox-paths``, and a build with any
    ``--sandbox-path`` had no ``/bin/sh`` when the list was replaced.
    Measured: a kernel's Kconfig runs ``sh`` for every ``$(shell)``.
    """
    options = dict(settings)
    if sandbox_paths:
        given = [options["extra-sandbox-paths"]] if "extra-sandbox-paths" in options else []
        options["extra-sandbox-paths"] = " ".join([*given, *sandbox_paths])
    return ServerOptions(settings=options, load_config=True, overlay=overlay)


async def _copy_outputs(
    store: AsyncStore, destination: AsyncStore, outputs: dict[str, str], uri: str
) -> dict[str, Any]:
    """Copy the closure of *outputs* from *store* to *destination*, which *uri* names, and report what arrived.

    Signature checking is off because these paths were built here a moment
    ago and nothing has signed them. The destination still decides: a daemon
    accepts unsigned paths from a trusted user and refuses them otherwise. A
    path that it refuses quietly is caught by
    :func:`pynix._impl.copy.copy_verified`.
    """
    requested = sorted(set(outputs.values()))
    closure = await closure_of(store, requested)
    ends = StoreEnds(store, destination, check_sigs=False)
    present, arrived = await copy_verified(ends, requested, closure, uri)
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


class BuildTargetError(EvaluationTargetError):
    pass


@dataclass(frozen=True, slots=True)
class BuildSession:
    """A huggorm evaluator, and the two stores that a build reads and writes."""

    session: AsyncSession | AsyncRemoteSession
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
    # **Before the build, and not after it.** Nix answers a configuration with
    # `selected value is not a derivation`, which is true and is the end of the
    # road: it says neither what the value is nor which path would work. The
    # test costs two attribute reads, and only a value that is an attribute set
    # reaches even the first one.
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
        # yet, and leaves its node without one.
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
    return [
        huggorm.filter_ansi_escapes(record_message(record), filter_all=True)
        for record in logs.records
        if record.action() == "msg"
    ]


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


@dataclass(frozen=True)
class _StoreUris:
    """Where a build runs, where it evaluates, and where its outputs go when it succeeds."""

    build: str
    evaluation: str | None
    copy_back: str | None = None


async def _build_on_huggorm(
    command: Build, target: EvaluationTarget, settings: dict[str, str], uris: _StoreUris
) -> tuple[dict[str, str], int, dict[str, Any] | None]:
    monitor = MonitorState(started=time.monotonic(), print_build_logs=command.print_build_logs) if command.nom else None
    async with huggorm_build_session(
        uris.build,
        eval_store_uri=uris.evaluation,
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
        if uris.copy_back is not None and outputs:
            await _copy_outputs(build_store, session.store(uris.copy_back), outputs, uris.copy_back)
        copied = None
        if command.copy is not None and outputs:
            copied = await _copy_outputs(build_store, session.store(command.copy), outputs, command.copy)
    return outputs, updates, copied


async def _plan_or_none(build: BuildSession, drv_path: str) -> BuildPlan | None:
    """The plan of *drv_path*, or None, which leaves the monitor a flat list.

    A missing plan costs the tree and nothing else, so an error here must not
    stop the build.
    """
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
    guest = _resolve_linux_builder(command, namespaced=namespaced)

    settings = configured_nix_settings(
        substituters=command.substituters,
        trusted_public_keys=command.trusted_public_keys,
    )

    # Outside the session, so the error passes through its log forwarding:
    # --nom counts it in the last frame, and the message prints after that.
    try:
        if namespaced:
            outputs, updates, copied = await _build_namespaced(command, target, settings)
        elif guest is not None:
            outputs, updates, copied = await _build_on_linux_builder(command, target, settings, guest)
        else:
            outputs, updates, copied = await _build_on_huggorm(
                command, target, settings, _StoreUris(command.store, command.eval_store)
            )
    except BuildTargetError as exc:
        report_and_exit(exc)
    except LinuxBuilderError as exc:
        error_exit(str(exc))

    report: dict[str, Any] = {"outputs": outputs, "updatedFods": updates, "dryRun": command.dry_run}
    if copied is not None:
        report["copy"] = copied
    print_json(report)


async def _build_namespaced(
    command: Build, target: EvaluationTarget, settings: dict[str, str]
) -> tuple[dict[str, str], int, dict[str, Any] | None]:
    """Build in a huggorm server in its own user namespace, over an overlay store.

    The server is a process of its own because ``unshare`` needs a
    single-threaded process, and this one has threads. Its stderr is this
    command's, so the reason it fails to start reaches the user.
    """
    monitor = MonitorState(started=time.monotonic(), print_build_logs=command.print_build_logs) if command.nom else None
    copied: dict[str, Any] | None = None
    # A directory the user named survives, which is the point of naming one.
    overlay = command.overlay_dir if command.overlay_dir is not None else TEMPORARY
    options = _server_options(overlay, settings, command.sandbox_path or [])
    async with worker(options) as session:
        build_store = await session.store()
        if command.eval_store is None:
            eval_store = build_store
            state = await session.eval(eval_store)
        else:
            eval_store = await session.store(command.eval_store)
            state = await session.eval(eval_store, None, build_store)
        async with forward_state_logs(
            session,
            state,
            verbosity=command.verbosity,
            print_build_logs=command.print_build_logs,
            monitor=monitor,
        ):
            logger.info("pynix build evaluating target")
            outputs, updates = await _build_target(
                target,
                BuildSession(session, build_store, eval_store, state),
                update_fod=command.update_fod,
                dry_run=command.dry_run,
                monitor=monitor,
            )
            logger.info("pynix build finished")
            # The host store by name: in the namespace the server is root,
            # so "auto" would open the overlay at /nix/store instead.
            if command.copy_back and outputs:
                host = await session.store(_HOST_STORE_URI)
                await _copy_outputs(build_store, host, outputs, _HOST_STORE_URI)
            if command.copy is not None and outputs:
                destination = await session.store(command.copy)
                copied = await _copy_outputs(build_store, destination, outputs, command.copy)
    return outputs, updates, copied


async def _build_on_linux_builder(
    command: Build, target: EvaluationTarget, settings: dict[str, str], guest: str
) -> tuple[dict[str, str], int, dict[str, Any] | None]:
    """Build in a Linux VM, evaluating against the host store.

    The host store evaluates, so the derivations land in it and Nix copies
    their closure to the guest; the overlay already holds every host path,
    so that copy only registers them.
    """
    async with linux_builder(guest) as vm:
        uris = _StoreUris(
            vm.store,
            command.eval_store or _HOST_STORE_URI,
            _HOST_STORE_URI if command.copy_back else None,
        )
        return await _build_on_huggorm(command, target, settings, uris)


def _resolve_linux_builder(command: Build, *, namespaced: bool) -> str | None:
    """The guest manifest of a --linux-builder build, or None for any other build."""
    if not command.linux_builder:
        return None
    if namespaced:
        error_exit("--linux-builder and --namespaced each build in a store of their own")
    return requested_guest(guest=command.linux_builder_guest, store_named="store" in command.explicit_options)


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
