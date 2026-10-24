from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Any, NoReturn, TextIO

import anyio
import huggorm
import structlog
from huggorm import AsyncEvalState, LogRecord
from rich.console import Console
from rich.live import Live
from rich.text import Text
from structlog.typing import EventDict, WrappedLogger

from libpynix._typecheck import no_runtime_type_check
from pynix._build_monitor import MonitorState, RecordEvent, render
from pynix._impl.settings import SettingError, nix_settings
from pynix._store_layout import StoreLayout
from pynix._verbosity import VerbosityInput, verbosity_of
from pynix.target import EvaluationTargetError

_LOG_LINE_RESULTS = frozenset({huggorm.ResultType.BUILD_LOG_LINE, huggorm.ResultType.POST_BUILD_LOG_LINE})
# nom redraws on every event; four times a second shows the same thing.
_MONITOR_REDRAW_SECONDS = 0.25
#: What `nix` logs at when no flag names a level (`nix::verbosity`, lvlInfo).
DEFAULT_VERBOSITY = huggorm.Verbosity.INFO

_config_loaded = False

# Build-progress/error messages go here, not stdout -- so a command's
# print_json() output stays clean, machine-parseable JSON even when the same
# invocation also fails partway through (e.g. `pynix derivation show ... |
# jq` must never see "Error: ..." text mixed into its stdin).
error_console = Console(stderr=True)


def print_json(obj: object) -> None:
    sys.stdout.write(json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False))
    sys.stdout.write("\n")


def error_exit(message: str | Text, *, cause: BaseException | None = None) -> NoReturn:
    """Print ``Error: message`` to stderr and exit(1).

    **The message arrives as a `Text`, and never inside a markup string.**
    Most of these messages come from Nix, which colours its own output, and
    an interpolated escape reaches the highlighter of rich as literal text.
    Measured, with ``ESC`` for the escape byte, on ``ESC[35;1mvalue ...``::

        f"...{message}"           ->  ESC ESC[1m [ ESC[0m ESC[1;36m 35 ESC[0m ;1mvalue ...
        Text.from_ansi(message)   ->  ESC[1;35m value is a string ESC[0m

    ``ReprHighlighter`` matches the ``[`` and the ``35`` and styles each one,
    which leaves the escape byte of Nix with nothing after it. The markup
    parser is not the cause: ``RE_TAGS`` starts a tag at ``[a-z#/@]``, and a
    digit does not match. ``Text.from_ansi`` turns the escape into a style of
    rich before rich sees the text, so a terminal keeps the colour and a pipe
    drops it. A ``Text`` is also never highlighted, which is what makes this
    safe for a message that pynix did not write.

    Pass *cause* to chain the exception, so that a ``--debug`` traceback keeps
    the real reason.
    """
    error_console.print("[red]Error:[/red]", Text.from_ansi(message) if isinstance(message, str) else message)
    raise SystemExit(1) from cause


def report_and_exit(exc: EvaluationTargetError) -> NoReturn:
    """Like `error_exit`, but for an EvaluationTargetError (or subclass, e.g.
    build.py's BuildTargetError) caught from `target.validate()`/
    `evaluate_target()` -- chains `from exc` so --debug tracebacks keep the
    real cause."""
    error_exit(str(exc), cause=exc)


class LogGate:
    """The verbosity up to which :func:`forward_state_logs` prints an activity.

    Mutable, because a REPL's ``:verbosity`` moves the level after the
    forwarder starts.
    """

    __slots__ = ("level",)

    def __init__(self, level: huggorm.Verbosity) -> None:
        self.level = level


def _python_level(verbosity: huggorm.Verbosity) -> int:
    """The level of pynix's own events that Nix's *verbosity* shows.

    At Nix's default, only a warning or an error: a progress event of pynix is
    not news. ``talkative`` shows them, and ``debug`` shows everything.
    """
    if verbosity <= huggorm.Verbosity.INFO:
        return logging.WARNING
    if verbosity <= huggorm.Verbosity.CHATTY:
        return logging.INFO
    return logging.DEBUG


def _render(_logger: WrappedLogger, method_name: str, event_dict: EventDict) -> str:
    """``warning: the event key=value``, in the shape Nix gives its own lines."""
    event = event_dict.pop("event")
    fields = "".join(f" {key}={value!r}" for key, value in sorted(event_dict.items()))
    prefix = f"{method_name}: " if method_name in {"warning", "error", "critical"} else ""
    return f"{prefix}{event}{fields}"


@no_runtime_type_check  # file is duck-typed against TextIO -- under prompt_toolkit's
# patch_stdout(), sys.stdout is swapped for a StdoutProxy that implements the
# file-like write interface without being a real TextIO instance.
def configure_logging(*, file: TextIO | None = None, verbosity: huggorm.Verbosity = DEFAULT_VERBOSITY) -> None:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(_python_level(verbosity)),
        logger_factory=structlog.PrintLoggerFactory(file=file or sys.stderr),
        processors=[_render],
    )


async def huggorm_local_store_path(store: huggorm.AsyncStore, path: str) -> Path:
    """Where *path* really is on this filesystem, for a store that has one.

    A chroot store reports the logical ``/nix/store`` and keeps the files
    somewhere else, so the string a caller types is not a path ``open`` takes.

    Raises ``SystemExit`` when the store exposes no local path, which is what
    an ssh or an http store does.
    """
    layout = await StoreLayout.of(store)
    store_path, suffix = _split_store_path(path, layout.store_dir)
    await store.query_path_info(await store.parse_store_path(store_path))
    if layout.real_store_dir is None:
        raise SystemExit("store does not expose a local filesystem path")
    return Path(layout.to_disk(store_path)) / suffix


def _split_store_path(path: str, store_dir: str) -> tuple[str, Path]:
    prefix = f"{store_dir}/"
    if not path.startswith(prefix):
        raise SystemExit(f"{path} is not inside {store_dir}")
    rest = path.removeprefix(prefix)
    base_name, separator, suffix = rest.partition("/")
    if not base_name:
        raise SystemExit(f"{path} is not a store path")
    suffix_path = Path(suffix) if separator else Path()
    if suffix_path.is_absolute() or ".." in suffix_path.parts:
        raise SystemExit(f"{path} escapes {store_dir}/{base_name}")
    return f"{store_dir}/{base_name}", suffix_path


def init_nix(*, load_config: bool = True) -> None:
    """Set up Nix for this process: ``nix.conf``, the plugins, the evaluator.

    The configuration loads once: a second load of ``nix.conf`` overwrites
    what a caller set after the first. Nix loads the plugins once per
    process, and ``huggorm.init_plugins`` keeps that count for every caller.
    ``fetch-tree`` is on here, before the first store opens, so ``NIX_PATH``
    reaches ``nix-path`` as a session starts.
    """
    global _config_loaded  # noqa: PLW0603 -- process-wide, like the Nix configuration
    if load_config and not _config_loaded:
        huggorm.load_config()
        _config_loaded = True
    huggorm.init_plugins()
    huggorm.start_collector()
    huggorm.enable_experimental_feature("fetch-tree")


def configured_nix_settings(**flags: str | None) -> dict[str, str]:
    """The Nix settings that *flags*, ``PYNIX_NIX_*`` and ``[nix]`` name.

    Exits with the error when a layer names a setting Nix does not have.
    """
    try:
        return nix_settings(huggorm.list_settings(), **flags)
    except SettingError as exc:
        error_exit(str(exc))


def apply_nix_settings(settings: Mapping[str, str] | None = None) -> None:
    """Set up Nix, then apply *settings*, or the configured ones when None.

    Before the first store opens, because a store reads the globals once.
    """
    init_nix()
    for name, value in (configured_nix_settings() if settings is None else settings).items():
        huggorm.set_setting(name, value)


@asynccontextmanager
async def huggorm_eval_session(
    store_uri: str,
    *,
    print_build_logs: bool = False,
) -> AsyncGenerator[tuple[huggorm.AsyncSession, huggorm.AsyncStore, AsyncEvalState]]:
    """Open a huggorm session, store and evaluator, forwarding logs for the duration of the block."""
    apply_nix_settings()
    async with huggorm.AsyncSession(store_uri) as session:
        store = session.store()
        state = session.eval(store)
        async with forward_state_logs(session, state, print_build_logs=print_build_logs):
            yield session, store, state


@asynccontextmanager
async def huggorm_store_session(
    store_uri: str,
    *,
    verbosity: VerbosityInput | None = None,
) -> AsyncGenerator[tuple[huggorm.AsyncSession, huggorm.AsyncStore]]:
    """Open a huggorm session and store with no evaluator, forwarding logs for the duration of the block."""
    apply_nix_settings()
    async with huggorm.AsyncSession(store_uri) as session:
        store = session.store()
        async with forward_state_logs(session, None, verbosity=verbosity):
            yield session, store


@asynccontextmanager
async def huggorm_build_session(  # noqa: PLR0913 -- keyword-only, one per session option; a bundle would hide which ones a command sets
    store_uri: str,
    *,
    eval_store_uri: str | None = None,
    settings: Mapping[str, str] | None = None,
    verbosity: VerbosityInput | None = None,
    print_build_logs: bool = False,
    monitor: MonitorState | None = None,
) -> AsyncGenerator[tuple[huggorm.AsyncSession, huggorm.AsyncStore, huggorm.AsyncStore, AsyncEvalState]]:
    """Open a huggorm session for a build, forwarding logs for the duration of the block.

    Yields the session, the store that builds, the store that evaluates, and
    the evaluator. The two stores are one store unless *eval_store_uri* names
    another. *settings* is in ``nix.conf`` spelling, and None means the
    configured ones (:func:`apply_nix_settings`).
    """
    apply_nix_settings(settings)
    async with huggorm.AsyncSession(store_uri) as session:
        build_store = session.store()
        if eval_store_uri is None:
            eval_store = build_store
            state = session.eval(eval_store)
        else:
            eval_store = session.store(eval_store_uri)
            state = session.eval(eval_store, None, build_store)
        async with forward_state_logs(
            session, state, verbosity=verbosity, print_build_logs=print_build_logs, monitor=monitor
        ):
            yield session, build_store, eval_store, state


async def build_derivation(
    build_store: huggorm.AsyncStore,
    drv_path: str,
    *,
    eval_store: huggorm.AsyncStore | None = None,
) -> dict[str, str]:
    """Build every output of *drv_path*, and give the path of each one."""
    derived = await build_store.parse_derived_path(f"{drv_path}^*")
    other_eval_store = None if eval_store is build_store else eval_store
    [result] = await build_store.build_paths_with_results([derived], eval_store=other_eval_store)
    error = result.error()
    if error is not None:
        raise error
    success = result.success()
    if success is None:
        raise RuntimeError("a build reported neither success nor failure")
    return {
        name: await build_store.print_store_path(realisation.out_path())
        for name, realisation in success.built_outputs().items()
    }


@asynccontextmanager
@no_runtime_type_check  # log_file is duck-typed against TextIO -- see configure_logging above.
async def forward_state_logs(  # noqa: PLR0913 -- keyword-only, one per forwarding option
    session: huggorm.AsyncSession | huggorm.AsyncRemoteSession,
    state: AsyncEvalState | None,
    *,
    verbosity: VerbosityInput | None = None,
    print_build_logs: bool = False,
    log_file: TextIO | None = None,
    monitor: MonitorState | None = None,
) -> AsyncGenerator[LogGate]:
    """Print what *state* logs while the block runs, as Nix prints it.

    Also what no evaluator claims: a fetch, a download, a build. Unforwarded,
    those reach huggorm's fallback logger, which prints at CHATTY.

    *verbosity* gates the activities and pynix's own events; Nix gates its
    messages before they arrive. The block receives the :class:`LogGate`, to
    move the level later.

    With ``monitor``, fold the records into it and draw nom's status at the
    bottom of stderr instead, with the log above it. The caller keeps the
    state, so it can hand the monitor a plan.
    """
    gate = LogGate(DEFAULT_VERBOSITY if verbosity is None else verbosity_of(verbosity))
    old_config = structlog.get_config()
    live = _start_monitor() if monitor is not None else None
    # After the monitor starts: `Live` swaps `sys.stderr` for a proxy that
    # prints above it, and structlog binds whatever `sys.stderr` is now.
    configure_logging(file=log_file, verbosity=gate.level)
    # Bound once, as structlog binds it: `quiet_terminal` swaps `sys.stderr`
    # before a command that runs under it opens its session.
    out = log_file if log_file is not None else sys.stderr
    colour = _wants_colour(out)

    def write(record: LogRecord) -> None:
        line = _record_line(record, print_build_logs=print_build_logs, gate=gate)
        if line is not None:
            out.write(f"{line if colour else huggorm.filter_ansi_escapes(line, filter_all=True)}\n")

    def fold(record: LogRecord) -> None:
        event = monitor_event(record)
        if event is None or live is None or monitor is None:
            return
        for line in monitor.apply(event, time.monotonic()):
            # Unwrapped, as Nix prints it: a store path broken across lines
            # cannot be copied.
            live.console.print(line, soft_wrap=True)

    callback = write if monitor is None else fold
    try:
        async with (
            session.forward(state, callback, level=int(gate.level)) if state is not None else nullcontext(),
            session.forward_process(callback, level=int(gate.level)),
            _redrawing(live, monitor),
        ):
            yield gate
    except Exception as exc:
        # The monitor's last frame counts it. The caller prints it, after
        # that frame, where nom prints its errors too.
        if monitor is not None:
            monitor.note_error(str(exc))
        raise
    finally:
        if live is not None and monitor is not None:
            _stop_monitor(live, monitor)
        structlog.configure(**old_config)


@asynccontextmanager
async def _redrawing(live: Live | None, state: MonitorState | None) -> AsyncGenerator[None]:
    """Redraw *live* from *state* while the block runs; nothing without a monitor."""
    if live is None or state is None:
        yield
        return
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_redraw, live, state)
            try:
                yield
            finally:
                tg.cancel_scope.cancel()
    except* BaseException as eg:
        # A task group wraps even the body's own lone exception in a group;
        # unwrapped, a caller still sees SystemExit and not a group.
        if len(eg.exceptions) == 1:
            raise eg.exceptions[0] from None
        raise


def _stop_monitor(live: Live, state: MonitorState) -> None:
    live.update(_monitor_view(live, state, finished_at=time.strftime("%H:%M:%S")), refresh=True)
    live.stop()
    # rich ends the last frame with a newline on a terminal only; without
    # one, the caller's error runs on after it.
    if not live.console.is_terminal:
        live.console.line()


def monitor_event(record: LogRecord) -> RecordEvent | None:
    """*record* as the event :meth:`MonitorState.apply` reads, or ``None``.

    A message with error info is an ``error``, and the fields of an activity
    are plain ints and strings.
    """
    match record.action():
        case "msg" if (info := record.info()) is not None:
            return RecordEvent("error", [record.level(), info.msg()], info.msg())
        case "msg":
            return RecordEvent("msg", [record.level(), record.text()], record.text())
        case "start":
            fields = _field_values(record)
            args = [record.id(), record.level(), record.type(), record.text(), fields, record.parent()]
            return RecordEvent("start", args)
        case "stop":
            return RecordEvent("stop", [record.id()])
        case "result":
            return RecordEvent("result", [record.id(), record.type(), _field_values(record)])
        case _:
            return None


def _field_values(record: LogRecord) -> list[int | str]:
    return [field.integer() if field.is_int() else field.text() for field in record.fields()]


def record_message(record: LogRecord) -> str:
    """The text of a ``msg`` record, as Nix prints it."""
    info = record.info()
    return record.text() if info is None else info.msg()


def _record_line(record: LogRecord, *, print_build_logs: bool, gate: LogGate) -> str | None:
    """The line ``SimpleLogger`` prints for *record*, or ``None`` for no line.

    A message arrives already gated at the verbosity of the thread that raised
    it, so a REPL evaluator at ``debug`` still shows its debug messages. An
    activity arrives ungated.
    """
    match record.action():
        case "msg":
            return record_message(record)
        case "start":
            text = record.text()
            if not text or record.level() > gate.level:
                return None
            return f"{text}..."
        case "result" if print_build_logs and record.type() in _LOG_LINE_RESULTS:
            texts = [field.text() for field in record.fields() if not field.is_int()]
            return texts[-1] if texts else None
        case _:
            return None


def _start_monitor() -> Live:
    # Not auto-refreshed: rich's refresh thread would read the state while the
    # event loop writes it. `_redraw` draws from the loop instead.
    live = Live(console=error_console, auto_refresh=False, transient=False)
    live.start()
    return live


def _monitor_view(live: Live, state: MonitorState, *, finished_at: str | None = None) -> Text:
    console = live.console
    height = console.size.height if console.is_terminal else None
    return render(state, time.monotonic(), width=console.size.width, height=height, finished_at=finished_at)


async def _redraw(live: Live, state: MonitorState) -> None:
    while True:
        live.update(_monitor_view(live, state), refresh=True)
        await anyio.sleep(_MONITOR_REDRAW_SECONDS)


def _wants_colour(out: Any) -> bool:
    """Nix's rule: escape sequences reach a terminal, and nothing else."""
    if "NO_COLOR" in os.environ:
        return False
    isatty = getattr(out, "isatty", None)
    return bool(isatty()) if callable(isatty) else False
