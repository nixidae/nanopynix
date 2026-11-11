"""The implementation of ``pynix fmt``: ``CmdFormatterRun::run`` of ``src/nix/formatter.cc``.

The formatter is ``formatter.<system>`` of the flake ``.``, with no prefix, and
it resolves as ``pynix run`` resolves a package.
"""

from __future__ import annotations

import os

from huggorm import aio

from pynix import _evaluate
from pynix._impl.run import ProgramRequest, resolve_program
from pynix._util import error_exit
from pynix.fmt import Fmt
from pynix.target import EvaluationTarget

_FLAKE = "."


async def run_fmt(command: Fmt) -> None:
    """The body of :meth:`pynix.fmt.Fmt.run`.

    ``exec``, so the exit status of the formatter is the exit status of
    ``pynix``.
    """
    source = (await aio.parse_flake_ref(_FLAKE)).source_path()
    if source is None:
        error_exit("the flake in the current directory is not on this machine")
    request = ProgramRequest(
        target=EvaluationTarget(file=None, attr=None, flake=_FLAKE),
        attr_search=_evaluate.formatter_attr_search(),
        store=command.store,
        impure=command.impure,
        verbosity=command.verbosity,
        print_build_logs=command.print_build_logs,
    )
    resolved = await resolve_program(request)
    argv = [*resolved.exec_prefix, resolved.program, *command.args]
    env = {**os.environ, "PRJ_ROOT": str(source)}
    try:
        os.execve(argv[0], argv, env)  # noqa: S606 -- a store path this process just built
    except OSError as exc:
        error_exit(f"unable to execute '{argv[0]}': {exc.strerror}", cause=exc)
