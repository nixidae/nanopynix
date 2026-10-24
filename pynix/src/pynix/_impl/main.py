"""What ``pynix`` sets up once the parser has decided that a command runs.

**Nothing here is needed to print help or to answer a completion.**
``main`` used to call all three of these before it parsed anything, so a
completion callback configured a logger, installed a traceback handler and
set a process title, and then exited during the parse. ``configure_logging``
alone pulled ``structlog``, which is 195 ms and brings ``rich``, ``asyncio``
and ``attr`` with it. Issue #123.

The parse now runs first. A usage error still reads well, because argparse
formats those itself and writes them to stderr. A genuine defect during the
parse prints a plain traceback rather than a rich one, and that is the whole
of the cost.

**The event loop starts here as well, and nowhere earlier.** Every ``run`` is a
coroutine function, and ``anyio`` is 60 modules that ``import pynix`` must not
load -- ``tests/meta/test_import_budget.py`` states that. This module is
reached through ``pynix._impl``, which imports it when a command runs.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine

import anyio
import huggorm.errors
import rich.traceback
from rich.text import Text

from pynix._util import configure_logging, error_console


def prepare() -> None:
    """Install the traceback handler and configure logging."""
    rich.traceback.install(show_locals=True)
    configure_logging()


def run(body: Callable[[], Coroutine[object, object, None]]) -> None:
    """Run the body of a command, and report a failure of Nix as one line.

    One call, so that the choice of an async backend is written down once.
    clypi owned this call and started asyncio; anyio is what the rest of this
    repository uses, and `AGENTS.md` says why.

    **The `except` is new, and clypi never had it.** clypi wrapped every
    `run()` in `except get_config().nice_errors`, and that setting defaults to
    `(ClypiException, ClypiExceptionGroup)` -- so a failure of Nix was
    re-raised, reached the handler that `prepare` installs, and
    `rich.traceback(show_locals=True)` printed a panel of internal objects with
    the escape sequences of Nix inside them. Measured on `pynix build --file
    <dir>#<attr>` against an attribute that is not a derivation: 20 lines of
    `EvalError(...)` repr where one line of "selected value is not a
    derivation" belongs. `error_exit` is what the rest of this CLI already uses
    for exactly that, and its docstring holds the measurement about the escape
    sequences.

    `NixError` and nothing wider. A `TypeError` in this repository is a defect,
    and a defect deserves the traceback.

    **The message of Nix, in the words of Nix, with no prefix of our own.**
    A reader saw three markers for one failure: `error_exit` wrote `Error:`,
    `NixError` wrote the class as `[EvalError]`, and Nix writes `error:`
    inside the message. This function prints `exc.colored`, which is the line
    the `nix` CLI prints for the same failure, so a reader who knows Nix reads
    it unchanged.

    A failure of pynix itself still goes through `error_exit` and still says
    `Error:`, because that one is not a failure of Nix.
    """
    try:
        anyio.run(body)
    except huggorm.errors.NixError as exc:
        # `Text.from_ansi`, and not an interpolation: Nix colours its own
        # output, and `error_exit` holds the measurement of what an escape
        # sequence does to the highlighter of rich. `colored` is the line Nix
        # prints, escape sequences and all.
        error_console.print(Text.from_ansi(exc.colored))
        raise SystemExit(1) from exc
