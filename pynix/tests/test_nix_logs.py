from __future__ import annotations

import contextlib
import io
from collections.abc import AsyncGenerator, Callable
from typing import Any, cast

import huggorm
import pytest
import structlog
from huggorm import Verbosity

from pynix._util import configure_logging, forward_state_logs

_CHATTY = int(Verbosity.CHATTY)
_INFO = int(Verbosity.INFO)
_BUILD_LOG_LINE = int(huggorm.ResultType.BUILD_LOG_LINE)
# `_from_parts` is left out of the stubs. It is the seam huggorm's own round
# trip goes through, so a record built here is a real one.
_LogField: Any = cast("Any", huggorm.LogField)
_LogRecord: Any = cast("Any", huggorm.LogRecord)


def _record(action: str, *, level: int = 0, kind: int = 0, text: str = "", fields: tuple[Any, ...] = ()) -> Any:
    parts = [
        _LogField._from_parts(
            isinstance(f, int), f if isinstance(f, int) else 0, b"" if isinstance(f, int) else f.encode()
        )
        for f in fields
    ]
    # action, level, id, parent, type, request, text, fields, error info,
    # payload.
    return _LogRecord._from_parts(action, level, 7, 0, kind, 0, text.encode(), parts, None, "")


def _start(level: int, text: str) -> Any:
    return _record("start", level=level, text=text)


class _Session:
    """Hands *records* to the process-wide callback, as huggorm's forward does."""

    def __init__(self, *records: Any) -> None:
        self.records = records

    @contextlib.asynccontextmanager
    async def forward_process(self, callback: Callable[[Any], None], *, level: int) -> AsyncGenerator[None]:
        for record in self.records:
            callback(record)
        yield


async def _forward(*records: Any, level: Verbosity = Verbosity.INFO, print_build_logs: bool = False) -> str:
    out = io.StringIO()
    async with forward_state_logs(
        _Session(*records),  # type: ignore[arg-type]  # duck-typed: forward_state_logs reads only forward_process here
        None,
        verbosity=level,
        print_build_logs=print_build_logs,
        log_file=out,
    ):
        pass
    return out.getvalue()


async def test_an_activity_prints_as_simple_logger_does_up_to_the_verbosity() -> None:
    printed = await _forward(
        _start(_CHATTY, "copying '/nix/store/x-source' to the store"),
        _start(_INFO, "building '/nix/store/y.drv'"),
        _start(_INFO, ""),
    )

    assert printed == "building '/nix/store/y.drv'...\n"


async def test_a_chatty_activity_prints_at_chatty() -> None:
    printed = await _forward(_start(_CHATTY, "copying 'x' to the store"), level=Verbosity.CHATTY)

    assert printed == "copying 'x' to the store...\n"


async def test_a_message_prints_as_its_text_and_stop_and_result_print_nothing() -> None:
    # A message arrives already gated at the verbosity of the thread that
    # raised it, so the forwarder prints it whatever the gate says.
    printed = await _forward(
        _record("stop"),
        _record("result", kind=1, fields=(123,)),
        _record("msg", level=_CHATTY, text="useful message"),
        level=Verbosity.ERROR,
    )

    assert printed == "useful message\n"


async def test_a_pipe_gets_no_escape_sequences() -> None:
    printed = await _forward(_record("msg", level=1, text="\x1b[35;1mwarning:\x1b[0m unknown setting 'x'"))

    assert printed == "warning: unknown setting 'x'\n"


async def test_a_build_log_line_prints_only_when_asked() -> None:
    line = _record("result", kind=_BUILD_LOG_LINE, fields=("hello> compiling",))

    assert await _forward(line) == ""
    assert await _forward(line, print_build_logs=True) == "hello> compiling\n"


@pytest.mark.parametrize(
    ("verbosity", "printed"),
    [
        (Verbosity.INFO, "warning: the registry is unreachable layer='global'\n"),
        (Verbosity.TALKATIVE, "pynix build finished\nwarning: the registry is unreachable layer='global'\n"),
    ],
)
def test_pynix_events_show_at_a_higher_verbosity_and_warnings_always(verbosity: Verbosity, printed: str) -> None:
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
