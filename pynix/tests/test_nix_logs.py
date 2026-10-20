from __future__ import annotations

import io
from typing import TYPE_CHECKING, Any

import pytest
import structlog

from nanopynix import LogEvent, LogLevel
from pynix._util import LogGate, _forward_nix_logs, _LogActivity, configure_logging

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_CHATTY = int(LogLevel.CHATTY)
_INFO = int(LogLevel.INFO)


class _Session:
    def __init__(self, *events: LogEvent) -> None:
        self.events = events

    async def log_stream(self) -> AsyncIterator[LogEvent]:
        for event in self.events:
            yield event


def _start(level: int, text: str) -> LogEvent:
    # `[id, level, type, text, fields, parent]`, what `_core/_logs.py` sends.
    return LogEvent(action="start", args=[7, level, 0, text, [], 0])


async def _forward(*events: LogEvent, level: LogLevel = LogLevel.INFO, print_build_logs: bool = False) -> str:
    out = io.StringIO()
    await _forward_nix_logs(
        _Session(*events),
        print_build_logs=print_build_logs,
        activity=_LogActivity(),
        gate=LogGate(level),
        file=out,
    )
    return out.getvalue()


async def test_an_activity_prints_as_simple_logger_does_up_to_the_verbosity() -> None:
    printed = await _forward(
        _start(_CHATTY, "copying '/nix/store/x-source' to the store"),
        _start(_INFO, "building '/nix/store/y.drv'"),
        _start(_INFO, ""),
    )

    assert printed == "building '/nix/store/y.drv'...\n"


async def test_a_chatty_activity_prints_at_chatty() -> None:
    printed = await _forward(_start(_CHATTY, "copying 'x' to the store"), level=LogLevel.CHATTY)

    assert printed == "copying 'x' to the store...\n"


async def test_a_message_prints_as_its_text_and_stop_and_result_print_nothing() -> None:
    # `[level, text]`, which is what `PyLogger::log` sends. A message arrives
    # already gated at the verbosity of the thread that raised it, so the
    # forwarder prints it whatever the gate says.
    printed = await _forward(
        LogEvent(action="stop", args=[42]),
        LogEvent(action="result", args=[42, 1, [123]]),
        LogEvent(action="msg", args=[_CHATTY, "useful message"]),
        level=LogLevel.ERROR,
    )

    assert printed == "useful message\n"


async def test_a_pipe_gets_no_escape_sequences() -> None:
    printed = await _forward(LogEvent(action="msg", args=[1, "\x1b[35;1mwarning:\x1b[0m unknown setting 'x'"]))

    assert printed == "warning: unknown setting 'x'\n"


async def test_a_build_log_line_prints_only_when_asked() -> None:
    line = LogEvent(action="result", args=[42, 101, ["hello> compiling"]])

    assert await _forward(line) == ""
    assert await _forward(line, print_build_logs=True) == "hello> compiling\n"


async def test_the_drain_counts_every_event_including_skipped_ones() -> None:
    # The exit drain stops once this counter stops moving, so it has to count
    # every event the stream produced -- including the ones this forwarder
    # filters out. Counting only printed events would let a burst of skipped
    # ones look like a quiet stream and cut the drain short.
    activity = _LogActivity()
    await _forward_nix_logs(
        _Session(LogEvent(action="stop", args=[42]), _start(_CHATTY, "x"), LogEvent(action="msg", args=[3, "m"])),
        print_build_logs=False,
        activity=activity,
        gate=LogGate(LogLevel.INFO),
        file=io.StringIO(),
    )

    assert activity.count == 3


@pytest.mark.parametrize(
    ("verbosity", "printed"),
    [
        (LogLevel.INFO, "warning: the registry is unreachable layer='global'\n"),
        (LogLevel.TALKATIVE, "pynix build finished\nwarning: the registry is unreachable layer='global'\n"),
    ],
)
def test_pynix_events_show_at_a_higher_verbosity_and_warnings_always(verbosity: LogLevel, printed: str) -> None:
    old = structlog.get_config()
    out = io.StringIO()
    try:
        configure_logging(file=out, verbosity=verbosity)
        logger: Any = structlog.get_logger("pynix.test")
        logger.info("pynix build finished")
        logger.warning("the registry is unreachable", layer="global")
    finally:
        structlog.configure(**old)

    assert out.getvalue() == printed
