"""The implementation of ``pynix edit``: ``CmdEdit::run`` of ``src/nix/edit.cc``.

The file and the line come from huggorm's ``edit_location``, which calls
``findPackageFilename`` for a package. The editor is ``editorFor`` of
``src/libcmd/editor-for.cc``.

Two differences from Nix. pynix ``exec``s the editor, so the exit status of
the editor is the exit status of ``pynix``; Nix waits for it and ignores its
status. And Nix's message for a package with no position lacks its closing
quote; this one has it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from huggorm.errors import NoPositionInfo

from pynix._evaluate import base_attr_search, evaluate_target
from pynix._util import error_exit, huggorm_eval_session, report_and_exit
from pynix.edit import Edit
from pynix.target import EvaluationTarget, EvaluationTargetError

#: ``editorFor`` passes ``+LINE`` to an editor whose command holds one of these.
LINE_EDITORS = ("emacs", "nano", "vim", "kak")


def editor_argv(path: str, line: int, environ: Mapping[str, str]) -> list[str]:
    """``editorFor``: ``$EDITOR`` split at whitespace, ``cat`` when it is empty."""
    editor = environ.get("EDITOR") or "cat"
    argv = editor.split()
    if line > 0 and any(name in editor for name in LINE_EDITORS):
        argv.append(f"+{line}")
    argv.append(path)
    return argv


def _what(target: EvaluationTarget) -> str:
    """The installable as the caller named it, as ``Installable::what`` gives it."""
    if target.flake is not None:
        return target.flake
    return target.attr or target.file or ""


async def run_edit(command: Edit) -> None:
    """The body of :meth:`pynix.edit.Edit.run`."""
    target = EvaluationTarget.from_command(command)
    try:
        target.validate(required=True)
    except EvaluationTargetError as exc:
        report_and_exit(exc)
    async with huggorm_eval_session(command.store, pure=target.pure(impure=command.impure)) as (
        _session,
        _store,
        state,
    ):
        try:
            value = await evaluate_target(target, state, auto_call_file=True, attr_search=base_attr_search())
            location = await value.edit_location()
        except EvaluationTargetError as exc:
            report_and_exit(exc)
        except NoPositionInfo as exc:
            error_exit(f"cannot find position information for '{_what(target)}'", cause=exc)
    argv = editor_argv(location.path(), location.line(), os.environ)
    try:
        os.execvp(argv[0], argv)  # noqa: S606 -- the editor the caller names in $EDITOR, as nix edit runs it
    except OSError as exc:
        error_exit(f"unable to execute '{argv[0]}': {exc.strerror}", cause=exc)
