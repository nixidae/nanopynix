"""The implementation of the ``pynix build`` command.

``pynix.build`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import dataclasses
import difflib
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import huggorm
import structlog
from anyio import Path as AnyioPath
from huggorm import (
    TEMPORARY,
    ActivityType,
    AsyncEvalState,
    AsyncRemoteSession,
    AsyncSession,
    AsyncStore,
    AsyncValue,
    LogRecord,
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
    build_derivations,
    configured_nix_settings,
    create_out_links,
    error_console,
    error_exit,
    evaluator_settings,
    filter_log_escapes,
    forward_state_logs,
    huggorm_build_session,
    huggorm_store_session,
    log_text,
    print_json,
    record_message,
    report_and_exit,
)
from pynix.build import Build
from pynix.target import (
    AutoArgOptions,
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


@dataclass(frozen=True, slots=True)
class BuiltTarget:
    """One derivation that a build built, and the attribute path that named it."""

    attr: str
    drv_path: str
    outputs: dict[str, str]


def output_report(built: Sequence[BuiltTarget]) -> dict[str, str] | dict[str, dict[str, str]]:
    """The ``outputs`` of the report: one target's outputs as they are, and
    several keyed by attribute path, because two targets can each have an
    ``out`` (nixidae/nixidae#891)."""
    if len(built) == 1:
        return dict(built[0].outputs)
    return {target.attr: dict(target.outputs) for target in built}


def _output_paths(built: Sequence[BuiltTarget]) -> list[str]:
    return [path for target in built for path in target.outputs.values()]


async def _copy_outputs(store: AsyncStore, destination: AsyncStore, outputs: Sequence[str], uri: str) -> dict[str, Any]:
    """Copy the closure of *outputs* from *store* to *destination*, which *uri* names, and report what arrived.

    Signature checking is off because these paths were built here a moment
    ago and nothing has signed them. The destination still decides: a daemon
    accepts unsigned paths from a trusted user and refuses them otherwise. A
    path that it refuses quietly is caught by
    :func:`pynix._impl.copy.copy_verified`.
    """
    requested = sorted(set(outputs))
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


async def _evaluate_build_target(target: EvaluationTarget, state: AsyncEvalState) -> list[tuple[str, AsyncValue]]:
    """The derivations *target* names, each with its attribute path."""
    try:
        root = await _evaluate.evaluate_target(
            target, state, auto_call_file=True, attr_search=_evaluate.base_attr_search()
        )
    except EvaluationTargetError as exc:
        raise BuildTargetError(str(exc)) from exc
    # **Before the build, and not after it.** Nix builds nothing for a
    # configuration and says nothing: it holds no derivation at the top level.
    # That says neither what the value is nor which path would work. The test
    # costs two attribute reads, and only a value that is an attribute set
    # reaches even the first one.
    kind = await _evaluate.configuration_kind(state, root)
    if kind is not None:
        raise BuildTargetError(configuration_message(kind, target.selected_attr()))
    try:
        return await _evaluate.derivations(state, root, target.selected_attr() or "")
    except EvaluationTargetError as exc:
        raise BuildTargetError(str(exc)) from exc


async def _plan(build: BuildSession, drv_paths: Sequence[str]) -> BuildPlan:
    """:func:`plan_build` on huggorm's stores."""
    missing = await build.build_store.query_missing(
        [await build.build_store.parse_derived_path(f"{drv_path}^*") for drv_path in drv_paths]
    )
    builds = frozenset([await build.build_store.print_store_path(p) for p in missing.will_build()])
    downloads = frozenset([await build.build_store.print_store_path(p) for p in missing.will_substitute()])
    store_dir = drv_paths[0].rsplit("/", 1)[0]
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
    for node in (*drv_paths, *sorted(builds - set(drv_paths))):
        outputs[node] = await output_paths(node)
        kids: list[str] = []
        for input_drv in await input_drvs(node):
            if input_drv in builds:
                kids.append(input_drv)
            elif downloads and (input_outputs := await output_paths(input_drv)) & downloads:
                outputs[input_drv] = input_outputs
                kids.append(input_drv)
        children[node] = tuple(kids)
    return BuildPlan(roots=tuple(drv_paths), children=children, outputs=outputs, builds=builds, downloads=downloads)


async def _drv_path(build: BuildSession, root: AsyncValue) -> str:
    """The ``.drv`` of *root*. ``drv_path`` is Nix's own test of a
    derivation, and raises Nix's message for any other value."""
    return await build.evaluation_store.print_store_path(await root.drv_path())


@dataclass
class _BuildLog:
    """The messages of a build, which hold the cause of a failed dependency.

    A callback on the process stream, and not a capture: a capture holds a
    fixed number of messages and results together, so a long build log fills
    it, and the mismatch that comes last is dropped. This keeps the messages
    alone, so no amount of build progress pushes one out.
    """

    messages: list[str] = field(default_factory=list)
    #: Each derivation that Nix built resolved, by the path of the resolved
    #: copy. Nix names the copy in a failure, and the copy is in no closure of
    #: the target. Only the resolution activity carries two fields
    #: (`derivation-resolution-goal.cc:138`): the original and the copy.
    originals: dict[str, str] = field(default_factory=dict)

    def keep(self, record: LogRecord) -> None:
        if record.action() == "msg":
            self.messages.append(filter_log_escapes(record_message(record)))
        elif record.action() == "start" and record.type() == ActivityType.BUILD_WAITING:
            match [log_text(entry) for entry in record.fields() if not entry.is_int()]:
                case [original, resolved]:
                    self.originals[resolved] = original
                case _:
                    pass

    def attributed(self, mismatch: FodHashMismatch) -> FodHashMismatch:
        """*mismatch*, named by the derivation the target holds."""
        if mismatch.drv_path is None or mismatch.drv_path not in self.originals:
            return mismatch
        return dataclasses.replace(mismatch, drv_path=self.originals[mismatch.drv_path])


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


async def _build_all(
    build: BuildSession,
    found: Sequence[tuple[str, AsyncValue]],
    monitor: MonitorState | None,
    mode: huggorm.BuildMode,
) -> list[BuiltTarget]:
    """Build every derivation in *found* in one request, as ``nix build a b`` does.

    A derivation that two attribute paths name builds once, and each path
    reports it.
    """
    named = [(attr, await _drv_path(build, value)) for attr, value in dict(found).items()]
    if not named:
        return []
    drv_paths = list(dict.fromkeys(drv_path for _, drv_path in named))
    if monitor is not None:
        monitor.plan = await _plan_or_none(build, drv_paths)
    outputs = await build_derivations(build.build_store, drv_paths, eval_store=build.evaluation_store, mode=mode)
    return [BuiltTarget(attr, drv_path, outputs[drv_path]) for attr, drv_path in named]


async def _names_the_fod(
    build: BuildSession, found: Sequence[tuple[str, AsyncValue]], mismatch: FodHashMismatch
) -> bool:
    for _, value in found:
        if await mismatch_is_target_fod(build.evaluation_store, build.state, value, mismatch):
            return True
    return False


async def _build_target(  # noqa: PLR0913 -- keyword-only, one per build option; a bundle would hide which ones a caller sets
    targets: Sequence[EvaluationTarget],
    build: BuildSession,
    *,
    update_fod: bool,
    dry_run: bool,
    mode: huggorm.BuildMode,
    monitor: MonitorState | None = None,
) -> tuple[list[BuiltTarget], int]:
    """Build the targets, applying only unambiguous plain-string FOD updates.

    Each attempt evaluates again, after the evaluator forgets every file it
    read, so it reads its own edit. A mismatch that names a derivation must be
    a fixed-output member of a target's closure before the source changes.
    Every target names the same ``--file``, so the first one's is the source.
    """
    reference = await targets[0].file_reference()
    source_file = reference.local_path if reference is not None else None
    updates = 0
    while True:
        found: list[tuple[str, AsyncValue]] = []
        # A failed dependency's cause is not in the error of the target that
        # needed it. It arrives on the process stream.
        log = _BuildLog()
        async with build.session.forward_process(log.keep, level=_CAUSE_LEVEL):
            try:
                for target in targets:
                    found.extend(await _evaluate_build_target(target, build.state))
                return await _build_all(build, found, monitor, mode), updates
            except NixError as exc:
                error = exc
        mismatch = extract_fod_hash_mismatch(str(error)) or extract_unique_fod_hash_mismatch(log.messages)
        if not update_fod or mismatch is None:
            raise error
        mismatch = log.attributed(mismatch)
        if mismatch.drv_path is not None and not await _names_the_fod(build, found, mismatch):
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
            return [], updates
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
    command: Build, targets: Sequence[EvaluationTarget], settings: dict[str, str], uris: _StoreUris
) -> tuple[list[BuiltTarget], int, dict[str, Any] | None]:
    monitor = MonitorState(started=time.monotonic(), print_build_logs=command.print_build_logs) if command.nom else None
    async with huggorm_build_session(
        uris.build,
        eval_store_uri=uris.evaluation,
        settings=settings,
        verbosity=command.verbosity,
        print_build_logs=command.print_build_logs,
        monitor=monitor,
        pure=targets[0].pure(impure=command.impure),
    ) as (session, build_store, eval_store, state):
        logger.info("pynix build evaluating target")
        built, updates = await _build_target(
            targets,
            BuildSession(session, build_store, eval_store, state),
            update_fod=command.update_fod,
            dry_run=command.dry_run,
            mode=_build_mode(command),
            monitor=monitor,
        )
        logger.info("pynix build finished")
        outputs = _output_paths(built)
        if uris.copy_back is not None and outputs:
            await _copy_outputs(build_store, session.store(uris.copy_back), outputs, uris.copy_back)
        copied = None
        if command.copy is not None and outputs:
            copied = await _copy_outputs(build_store, session.store(command.copy), outputs, command.copy)
    return built, updates, copied


async def _plan_or_none(build: BuildSession, drv_paths: Sequence[str]) -> BuildPlan | None:
    """The plan of *drv_paths*, or None, which leaves the monitor a flat list.

    A missing plan costs the tree and nothing else, so an error here must not
    stop the build.
    """
    try:
        return await _plan(build, drv_paths)
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
    targets = _targets(command)
    if command.update_fod:
        await _require_a_local_file(targets[0])
    _refuse_contradicting_flags(command)

    namespaced = _resolve_namespaced(command)
    guest = _resolve_linux_builder(command, namespaced=namespaced)
    _refuse_an_out_link_with_nothing_to_link(command, elsewhere=namespaced or guest is not None)

    settings = configured_nix_settings(
        substituters=command.substituters,
        trusted_public_keys=command.trusted_public_keys,
        keep_going="true" if command.keep_going else None,
    )

    # Outside the session, so the error passes through its log forwarding:
    # --nom counts it in the last frame, and the message prints after that.
    try:
        if namespaced:
            built, updates, copied = await _build_namespaced(command, targets, settings)
        elif guest is not None:
            built, updates, copied = await _build_on_linux_builder(command, targets, settings, guest)
        else:
            built, updates, copied = await _build_on_huggorm(
                command, targets, settings, _StoreUris(command.store, command.eval_store)
            )
    except BuildTargetError as exc:
        report_and_exit(exc)
    except LinuxBuilderError as exc:
        error_exit(str(exc))

    if command.out_link is not None and built:
        # The store the outputs are in now: a namespaced or VM build copied them to the host's.
        linked = _HOST_STORE_URI if namespaced or guest is not None else command.store
        async with huggorm_store_session(linked) as (_session, store):
            await create_out_links(store, command.out_link, [target.outputs for target in built])

    _print_result(command, built, updates, copied)


def _build_mode(command: Build) -> huggorm.BuildMode:
    """``bmCheck`` for ``--rebuild``, as ``nix build`` sets it (``build.cc:118``)."""
    return huggorm.BuildMode.CHECK if command.rebuild else huggorm.BuildMode.NORMAL


def _refuse_contradicting_flags(command: Build) -> None:
    if command.dry_run and not command.update_fod:
        error_exit("--dry-run requires --update-fod")
    if command.dry_run and command.copy is not None:
        error_exit("--dry-run builds nothing, so --copy has nothing to copy")
    # In Nix the later of the two wins; pynix refuses the pair, so no order decides quietly.
    if command.no_link and command.out_link is not None:
        error_exit("--no-link and --out-link contradict each other")


def _print_result(command: Build, built: list[BuiltTarget], updates: int, copied: dict[str, Any] | None) -> None:
    if command.print_out_paths:
        # `printOutPathsMaybe`: each output of each target, in output-name order.
        for target in built:
            for output in sorted(target.outputs):
                sys.stdout.write(f"{target.outputs[output]}\n")
        return
    report: dict[str, Any] = {"outputs": output_report(built), "updatedFods": updates, "dryRun": command.dry_run}
    if copied is not None:
        report["copy"] = copied
    print_json(report)


def _refuse_an_out_link_with_nothing_to_link(command: Build, *, elsewhere: bool) -> None:
    """*elsewhere* is a build outside the host's store: namespaced, or in a VM."""
    if command.out_link is None:
        return
    if command.dry_run:
        error_exit("--dry-run builds nothing, so --out-link has nothing to link")
    if elsewhere and not command.copy_back:
        error_exit("--out-link needs --copy-back: without it the outputs are gone when the build ends")


def _targets(command: Build) -> list[EvaluationTarget]:
    """One target for each ``--attr``, or one for the whole value, each validated."""
    attrs: list[str | None] = [*command.attr] or [None]
    targets = [
        EvaluationTarget(
            file=command.file,
            attr=attr,
            flake=command.flake,
            arguments=AutoArgOptions.from_command(command),
            expr=command.expr,
        )
        for attr in attrs
    ]
    try:
        for target in targets:
            target.validate(required=True)
    except EvaluationTargetError as exc:
        report_and_exit(exc)
    return targets


async def _build_namespaced(
    command: Build, targets: Sequence[EvaluationTarget], settings: dict[str, str]
) -> tuple[list[BuiltTarget], int, dict[str, Any] | None]:
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
    own = evaluator_settings(settings, pure=targets[0].pure(impure=command.impure))
    async with worker(options) as session:
        build_store = await session.store()
        if command.eval_store is None:
            eval_store = build_store
            state = await session.eval(eval_store, own)
        else:
            eval_store = await session.store(command.eval_store)
            state = await session.eval(eval_store, own, build_store)
        async with forward_state_logs(
            session,
            state,
            verbosity=command.verbosity,
            print_build_logs=command.print_build_logs,
            monitor=monitor,
        ):
            logger.info("pynix build evaluating target")
            built, updates = await _build_target(
                targets,
                BuildSession(session, build_store, eval_store, state),
                update_fod=command.update_fod,
                dry_run=command.dry_run,
                mode=_build_mode(command),
                monitor=monitor,
            )
            logger.info("pynix build finished")
            outputs = _output_paths(built)
            # The host store by name: in the namespace the server is root,
            # so "auto" would open the overlay at /nix/store instead.
            if command.copy_back and outputs:
                host = await session.store(_HOST_STORE_URI)
                await _copy_outputs(build_store, host, outputs, _HOST_STORE_URI)
            if command.copy is not None and outputs:
                destination = await session.store(command.copy)
                copied = await _copy_outputs(build_store, destination, outputs, command.copy)
    return built, updates, copied


async def _build_on_linux_builder(
    command: Build, targets: Sequence[EvaluationTarget], settings: dict[str, str], guest: str
) -> tuple[list[BuiltTarget], int, dict[str, Any] | None]:
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
        return await _build_on_huggorm(command, targets, settings, uris)


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
