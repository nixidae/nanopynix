"""The interactive Nix REPL: the prompt, the commands and the session.

``pynix/repl.py`` holds the command that starts this. The two are
separate modules because ``prompt_toolkit`` and the Nix grammar cost 91.8 ms,
and the parser loads every subcommand module on every start. ``pynix._impl`` says
why, and how the two halves reach each other.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import signal
import sys
import textwrap
from contextlib import asynccontextmanager
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

import anyio
import anyio.abc
import huggorm
from huggorm import AsyncValue
from huggorm.errors import NixError
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples
from prompt_toolkit.history import FileHistory
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.table import Table
from tree_sitter import Query, QueryCursor

from nanopynix._typechecking import BEARTYPING
from pynix import _evaluate
from pynix._completion import completion_prefix_at
from pynix._markdown import render_markdown
from pynix._nix_syntax import NIX_GRAMMAR_PATH, NIX_LANGUAGE, parse_nix
from pynix._util import DEFAULT_VERBOSITY, build_derivation, forward_state_logs, init_nix
from pynix._value_render import format_json
from pynix._verbosity import VerbosityInput, verbosity_of
from pynix.repl import Repl
from pynix.target import EvaluationTarget, EvaluationTargetError

if TYPE_CHECKING or BEARTYPING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Iterable

    from huggorm import AsyncEvalState, AsyncRepl, AsyncSession, AsyncStore
    from prompt_toolkit.document import Document

_PROMPT = "pynix> "
# Nix's `editorFor` passes `+LINE` to these, and to no other editor.
_LINE_EDITORS = ("emacs", "nano", "vim", "kak", "hx")
_FEATURES = ("flakes", "nix-command")
_COMMANDS = {
    ":a": "Add an attribute set to scope",
    ":add": "Add an attribute set to scope",
    ":b": "Build a derivation",
    ":build": "Build a derivation",
    ":d": "Show documentation of an expression",
    ":doc": "Show documentation of an expression",
    ":e": "Open a value's source in $EDITOR",
    ":edit": "Open a value's source in $EDITOR",
    ":exec": "Realise a Nix argv list and execute it",
    ":help": "Show this help",
    ":l": "Load a Nix file into scope",
    ":lf": "Load flake outputs into scope",
    ":ll": "Show the most recently loaded names",
    ":load": "Load a Nix file into scope",
    ":load-flake": "Load flake outputs into scope",
    ":p": "Evaluate and print an expression",
    ":print": "Evaluate and print an expression",
    ":q": "Exit the REPL",
    ":quit": "Exit the REPL",
    ":r": "Reload files and flakes",
    ":reload": "Reload files and flakes",
    ":run": "Build a derivation and run its main program",
    ":shell": "Realise a Nix string and execute it with the shell",
    ":t": "Show an expression's Nix type",
    ":type": "Show an expression's Nix type",
    ":verbosity": "Show or set Nix log verbosity",
    ":?": "Show this help",
}
_NIX_EXPRESSION_COMMANDS = frozenset(
    {
        ":a",
        ":add",
        ":b",
        ":build",
        ":d",
        ":doc",
        ":e",
        ":edit",
        ":exec",
        ":p",
        ":print",
        ":run",
        ":shell",
        ":t",
        ":type",
    }
)
_NIX_HIGHLIGHTS = Query(
    NIX_LANGUAGE,
    (Path(NIX_GRAMMAR_PATH) / "queries" / "highlights.scm").read_text(),
)
_NIX_HIGHLIGHT_STYLES = {
    # tree-sitter-nix 0.5.0 (numtide fork) renamed several capture groups to
    # align with nvim-treesitter conventions -- see its docs/highlight-groups.md
    # "Renames from v0.4.x and earlier" table. Both the surviving old names and
    # their v0.5.0 replacements are mapped here so this doesn't silently regress
    # on the next grammar bump either.
    "comment": "class:nix.comment",
    "comment.documentation": "class:nix.comment",
    "function": "class:nix.function",
    "function.call": "class:nix.function",
    "function.builtin": "class:nix.builtin",
    "keyword": "class:nix.keyword",
    "keyword.conditional": "class:nix.keyword",
    "keyword.operator": "class:nix.keyword",
    "keyword.import": "class:nix.keyword",
    "keyword.exception": "class:nix.keyword",
    "number": "class:nix.number",
    "number.float": "class:nix.number",
    "boolean": "class:nix.builtin",
    "constant.builtin": "class:nix.builtin",
    "operator": "class:nix.operator",
    # v0.5.0 dropped the old blanket "any identifier" fallback that used to
    # ride along under @property -- @variable is now the only thing covering
    # plain variable references, so it needs its own style to avoid going dark.
    "variable": "class:nix.variable",
    "variable.member": "class:nix.property",
    "punctuation.bracket": "class:nix.punctuation",
    "punctuation.delimiter": "class:nix.punctuation",
    "punctuation.special": "class:nix.punctuation",
    "string": "class:nix.string",
    "string.escape": "class:nix.escape",
    "string.special.path": "class:nix.path",
    "string.special.url": "class:nix.path",
    "variable.parameter": "class:nix.parameter",
    "variable.parameter.builtin": "class:nix.parameter",
}
_REPL_STYLE = Style.from_dict(
    {
        "nix.builtin": "ansicyan",
        "nix.comment": "ansibrightblack italic",
        "nix.escape": "ansiyellow",
        "nix.function": "ansiblue",
        "nix.keyword": "ansimagenta bold",
        "nix.number": "ansicyan",
        "nix.operator": "ansimagenta",
        "nix.parameter": "ansiyellow",
        "nix.path": "underline ansiblue",
        "nix.property": "ansiblue",
        "nix.punctuation": "ansibrightblack",
        "nix.string": "ansigreen",
        "nix.variable": "ansiblue",
    },
)
_HELP_ROWS = (
    ("<expr>", "Evaluate and print an expression"),
    ("<name> = <expr>", "Bind an expression to a name"),
    (":a, :add <expr>", "Add an attrset's attributes to scope"),
    (":b, :build <expr>", "Build an evaluated derivation"),
    (":d, :doc <expr>", "Show documentation of an expression"),
    (":e, :edit <expr>", "Open a package, function, path, or string in $EDITOR"),
    (":l, :load <path>", "Load a Nix file and add its attributes to scope"),
    (":lf, :load-flake <ref>", "Load flake outputs into scope"),
    (":ll, :last-loaded", "Show names from the most recent load"),
    (":p, :print <expr>", "Evaluate and print an expression"),
    (":run <expr>", "Build a derivation and run its main program"),
    (":exec <list>", "Realise a Nix argv list and execute it"),
    (":shell <expr>", "Realise a Nix string and execute it with the shell"),
    (":r, :reload", "Reload files and flakes"),
    (":t, :type <expr>", "Show an expression's Nix type"),
    (":verbosity [level]", "Show or set Nix log verbosity (0-7 or a level name)"),
    (":q, :quit", "Exit the REPL"),
    (":?, :help", "Show this help"),
)


def _render_help() -> str:
    """Render the REPL help with Rich's terminal-aware column layout."""
    output = StringIO()
    console = Console(file=output, color_system=None)
    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1))
    table.add_column(no_wrap=True)
    table.add_column()
    for command, description in _HELP_ROWS:
        table.add_row(command, description)
    console.print("Commands:")
    console.print(table)
    return "\n".join(line.rstrip() for line in output.getvalue().splitlines())


_HELP = _render_help()


def _repl_history() -> FileHistory:
    """Create persistent REPL history in the XDG state directory."""
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    path = state_home / "pynix" / "repl_history"
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileHistory(str(path))


def _nix_input(text: str) -> tuple[str, int] | None:
    """Return Nix source and its character offset within one REPL input line."""
    command, separator, argument = text.partition(" ")
    if command.startswith(":"):
        if command not in _NIX_EXPRESSION_COMMANDS or not separator:
            return None
        return argument, len(command) + len(separator)
    return text, 0


class _NixLexer(Lexer):
    """Color Nix expressions with the tree-sitter-nix highlight query."""

    @override
    def lex_document(self, document: Document) -> Callable[[int], StyleAndTextTuples]:
        source_and_offset = _nix_input(document.text)
        styles = [""] * len(document.text)
        if source_and_offset is not None:
            source, offset = source_and_offset
            encoded = source.encode()
            captures = QueryCursor(_NIX_HIGHLIGHTS).captures(parse_nix(source).root_node)
            for capture, nodes in captures.items():
                style = _NIX_HIGHLIGHT_STYLES.get(capture)
                if style is None:
                    continue
                for node in nodes:
                    start = offset + len(encoded[: node.start_byte].decode())
                    end = offset + len(encoded[: node.end_byte].decode())
                    styles[start:end] = [style] * (end - start)

        def get_line(line_number: int) -> StyleAndTextTuples:
            line = document.lines[line_number]
            start = sum(len(previous) + 1 for previous in document.lines[:line_number])
            fragments: StyleAndTextTuples = []
            for character, style in zip(line, styles[start : start + len(line)], strict=True):
                if fragments and fragments[-1][0] == style:
                    fragments[-1] = (style, fragments[-1][1] + character)
                else:
                    fragments.append((style, character))
            return fragments

        return get_line


class _ReplCompleter(Completer):
    """Complete REPL commands and names from the current Nix lexical scope."""

    def __init__(self, repl: _ReplEngine) -> None:
        self._repl = repl

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterable[Completion]:
        """Prompt-toolkit requires this synchronous hook even for async completion."""
        del document, complete_event
        return ()

    async def get_completions_async(
        self,
        document: Document,
        complete_event: CompleteEvent,
    ) -> AsyncGenerator[Completion]:
        before_cursor = document.text_before_cursor
        if not before_cursor:
            # **Tab, and not typing.** The shell convention is that Tab on an
            # empty word offers everything that can go there, and `nix repl`
            # follows it. `complete_while_typing` is on, so a completer that
            # answers an empty line unasked would open the menu as soon as the
            # prompt appeared. `completion_requested` is true for a keypress
            # and false for typing, which is exactly that difference.
            #
            # No cap on the number of names. The menu of prompt-toolkit
            # scrolls, so a large scope fills a bounded box rather than the
            # terminal, and the branch below already yields every name that
            # starts with one letter without a cap of its own.
            if complete_event.completion_requested:
                for name in await self._repl.scope_names():
                    yield Completion(name, start_position=0)
            return
        command, separator, _argument = before_cursor.partition(" ")
        if not separator and command.startswith(":"):
            for name, description in _COMMANDS.items():
                if name.startswith(command):
                    yield Completion(name, start_position=-len(command), display_meta=description)
            return

        async for item in self._name_completions(before_cursor, complete_event):
            yield item

    async def _name_completions(
        self,
        before_cursor: str,
        complete_event: CompleteEvent,
    ) -> AsyncGenerator[Completion]:
        """Each name of the scope, or of an attribute set, that continues the last word.

        A branch of its own, and not of the caller: the three cases together
        cost more complexity than the strict lint allows one function.
        """
        source_and_offset = _nix_input(before_cursor)
        if source_and_offset is None:
            return
        source, _offset = source_and_offset
        if not source:
            # An expression command with nothing typed after the space (":e ")
            # is a bare position, like an empty line. Complete from the ambient
            # scope on a keypress, and not while typing: `complete_while_typing`
            # is on, so answering the space itself would pop the menu open the
            # moment it is typed.
            if complete_event.completion_requested:
                for name in await self._repl.scope_names():
                    yield Completion(name, start_position=0)
            return
        target = completion_prefix_at(source, len(source.encode()))
        if target is None:
            return
        prefix, partial = target
        candidates = await self._repl.scope_names() if prefix is None else await self._attr_names(prefix)
        for name in candidates:
            if name.startswith(partial):
                yield Completion(name, start_position=-len(partial))

    async def _attr_names(self, expression: str) -> list[str]:
        try:
            value = await self._repl.string(expression)
            try:
                return await self._repl.attr_names(value)
            finally:
                await self._repl.release(value)
        except NixError:
            return []


@dataclass(frozen=True, slots=True)
class _ValueDoc:
    """What `EvalState::getDoc` says of a value."""

    name: str | None
    args: tuple[str, ...]
    doc: str


@dataclass(frozen=True, slots=True)
class _AttrDoc:
    """Where a set defines an attribute, and the comment there."""

    path: str
    line: int
    doc: str | None


class _LogForwarder:
    """Print what the evaluator logs, at a level that `:verbosity` moves.

    A level reaches Nix when a subscription opens, so a new level reopens the
    forwarding. One task owns it: a cancel scope that `:verbosity` entered
    would otherwise close in a different scope than the one that opened it.
    """

    def __init__(self, session: AsyncSession, state: AsyncEvalState) -> None:
        self._session = session
        self._state = state
        self.level = DEFAULT_VERBOSITY
        self._changed = anyio.Event()
        self._ready = anyio.Event()

    async def run(self, *, task_status: anyio.abc.TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
        started = False
        while True:
            async with forward_state_logs(self._session, self._state, verbosity=self.level, log_file=sys.stdout):
                self._ready.set()
                if not started:
                    started = True
                    task_status.started()
                await self._changed.wait()
            self._changed = anyio.Event()

    async def set_level(self, level: huggorm.Verbosity) -> None:
        self._ready = anyio.Event()
        self.level = level
        self._changed.set()
        await self._ready.wait()


@asynccontextmanager
async def _forwarding(session: AsyncSession, state: AsyncEvalState) -> AsyncGenerator[_LogForwarder]:
    forwarder = _LogForwarder(session, state)
    try:
        async with anyio.create_task_group() as tg:
            await tg.start(forwarder.run)
            try:
                yield forwarder
            finally:
                tg.cancel_scope.cancel()
    except* BaseException as eg:
        # The body's own exception, unwrapped, as `_util._redrawing` does.
        if len(eg.exceptions) == 1:
            raise eg.exceptions[0] from None
        raise


class _ReplEngine:
    """One `nix repl` scope on a huggorm evaluator, as the commands read it."""

    line_editors = _LINE_EDITORS

    def __init__(self, store: AsyncStore, state: AsyncEvalState, repl: AsyncRepl, logs: _LogForwarder) -> None:
        self._store = store
        self._state = state
        self._repl = repl
        self._logs = logs

    async def scope_names(self) -> list[str]:
        return await self._repl.names()

    async def string(self, expr: str) -> AsyncValue:
        return await self._repl.eval_expr(expr)

    async def line(self, text: str) -> AsyncValue | None:
        return await self._repl.process_line(text)

    async def load_file(self, path: str) -> AsyncValue:
        return await self._repl.load_file(path)

    async def load_target(self, target: EvaluationTarget) -> AsyncValue:
        return await _evaluate.load_repl_target(target, self._state, self._repl)

    async def eval_flake(self, ref: str) -> AsyncValue:
        """The value of `callFlake`, as `:load-flake` adds it: the outputs and the metadata."""
        locked = await self._state.lock_flake(huggorm.parse_flake_ref(ref))
        try:
            return await self._state.call_flake(locked)
        finally:
            await locked.aclose()

    async def add_attrs(self, value: AsyncValue) -> list[str]:
        return await self._repl.add_attrs(value)

    async def reset_file_cache(self) -> None:
        # `«nix-internal»/derivation-internal.nix` names no file to forget.
        for path in await self._state.cached_files():
            if path.startswith("/"):
                await self._state.forget_file(path)

    async def repl_select(self, expr: str) -> tuple[str, AsyncValue] | None:
        selection = await self._repl.select(expr)
        if selection is None:
            return None
        return await selection.name(), await selection.attrs()

    async def get_verbosity(self) -> huggorm.Verbosity:
        return self._logs.level

    async def set_verbosity(self, level: VerbosityInput) -> huggorm.Verbosity:
        verbosity = verbosity_of(level)
        await self._logs.set_level(verbosity)
        return verbosity

    async def to_python(self, value: AsyncValue) -> object:
        return await _evaluate.to_python(value)

    async def type_name(self, value: AsyncValue) -> str:
        await self._state.force(value)
        return await value.type_name()

    async def attr(self, value: AsyncValue, name: str) -> AsyncValue | None:
        return await _evaluate.optional_set(self._state, value, name)

    async def attr_names(self, value: AsyncValue) -> list[str]:
        await self._state.force(value)
        return await value.names()

    async def release(self, value: AsyncValue) -> None:
        await value.aclose()

    async def build(self, value: AsyncValue) -> dict[str, str]:
        await self._state.force(value)
        return await build_derivation(self._store, await self._store.print_store_path(await value.drv_path()))

    async def doc(self, value: AsyncValue) -> _ValueDoc | None:
        await self._state.force(value)
        doc = await value.doc()
        if doc is None:
            return None
        return _ValueDoc(name=doc.name(), args=tuple(doc.args()), doc=doc.doc())

    async def attr_doc(self, value: AsyncValue, name: str) -> _AttrDoc | None:
        await self._state.force(value)
        found = await value.attr_doc(name)
        if found is None:
            return None
        return _AttrDoc(path=found.path(), line=found.line(), doc=found.doc())

    async def edit_location(self, value: AsyncValue) -> tuple[str, int]:
        location = await value.edit_location()
        return location.path(), location.line()

    async def realise_argv(self, value: AsyncValue) -> list[str]:
        return await value.realise_argv()

    async def realise_string(self, value: AsyncValue) -> str:
        return await value.realise_string()


class _ReplState:
    """Mutable state threaded through one REPL session's command handlers."""

    def __init__(
        self,
        repl: _ReplEngine,
        line_editors: tuple[str, ...],
        loaded: list[tuple[str, str]],
        last_loaded: list[str],
    ) -> None:
        self.repl = repl
        self.line_editors = line_editors
        self.loaded = loaded
        self.last_loaded = last_loaded

    async def add_attrs(self, value: AsyncValue) -> list[str]:
        self.last_loaded = await self.repl.add_attrs(value)
        print_formatted_text(f"Added {len(self.last_loaded)} variables: {' '.join(self.last_loaded)}")
        return self.last_loaded

    async def evaluate(self, expr: str) -> AsyncValue:
        value = await self.repl.string(expr)
        print_formatted_text(format_json(await self.repl.to_python(value)))
        return value

    async def reload_sources(self) -> None:
        await self.repl.reset_file_cache()
        for load_command, source in self.loaded:
            value = await (self.repl.load_file(source) if load_command == ":load" else self.repl.eval_flake(source))
            await self.add_attrs(value)


async def _cmd_print(state: _ReplState, argument: str) -> None:
    await state.evaluate(argument)


async def _cmd_type(state: _ReplState, argument: str) -> None:
    print_formatted_text(await state.repl.type_name(await state.repl.string(argument)))


async def _cmd_build(state: _ReplState, argument: str) -> None:
    outputs = await state.repl.build(await state.repl.string(argument))
    print_formatted_text(format_json(outputs))


async def _cmd_doc(state: _ReplState, argument: str) -> None:
    doc = await state.repl.doc(await state.repl.string(argument))
    if doc is not None:
        doc_text = textwrap.dedent(doc.doc).strip()
        if doc.args and doc.name:
            args = " ".join(f"*{arg}*" for arg in doc.args)
            md = f"**Synopsis:** `builtins.{doc.name}` {args}\n\n{doc_text}"
        else:
            md = doc_text
        print_formatted_text(render_markdown(md))
        return

    sel = await state.repl.repl_select(argument)
    if sel is not None:
        name, parent_attrs = sel
        attr = await state.repl.attr_doc(parent_attrs, name)
        if attr is not None:
            pos_str = f"{attr.path}:{attr.line}"
            comment = attr.doc if attr.doc is not None else "No documentation found."
            md = f"Attribute `{name}`\n\n  … defined at {pos_str}\n\n{comment}".strip()
            print_formatted_text(render_markdown(md))
            return

    raise ReplRunError("value does not have documentation")


async def _cmd_edit(state: _ReplState, argument: str) -> None:
    await _edit(state.repl, await state.repl.string(argument), state.line_editors)
    await state.reload_sources()


async def _cmd_run(state: _ReplState, argument: str) -> None:
    await _run_derivation(state.repl, await state.repl.string(argument))


async def _cmd_exec(state: _ReplState, argument: str) -> None:
    await _exec_argv(await state.repl.realise_argv(await state.repl.string(argument)))


async def _cmd_shell(state: _ReplState, argument: str) -> None:
    await _shell(await state.repl.realise_string(await state.repl.string(argument)))


async def _cmd_add(state: _ReplState, argument: str) -> None:
    await state.add_attrs(await state.repl.string(argument))


async def _cmd_load(state: _ReplState, argument: str) -> None:
    await state.add_attrs(await state.repl.load_file(argument))
    state.loaded.append((":load", argument))


async def _cmd_load_flake(state: _ReplState, argument: str) -> None:
    await state.add_attrs(await state.repl.eval_flake(argument))
    state.loaded.append((":load-flake", argument))


async def _cmd_last_loaded(state: _ReplState, argument: str) -> None:
    del argument
    print_formatted_text(" ".join(state.last_loaded) if state.last_loaded else "nothing has been loaded")


async def _cmd_reload(state: _ReplState, argument: str) -> None:
    del argument
    await state.reload_sources()


async def _cmd_help(state: _ReplState, argument: str) -> None:
    del state, argument
    print_formatted_text(_HELP)


async def _cmd_verbosity(state: _ReplState, argument: str) -> None:
    """Show or set the level this repl logs at.

    The level covers the evaluator and the work it starts, such as a fetch or
    a build.
    """
    try:
        verbosity = await (state.repl.get_verbosity() if not argument else state.repl.set_verbosity(argument))
    except (TypeError, ValueError) as exc:
        print_formatted_text(f"error: {exc}")
    else:
        print_formatted_text(f"{verbosity.name.lower()} ({int(verbosity)})")


_COMMAND_HANDLERS: dict[str, Callable[[_ReplState, str], Awaitable[None]]] = {
    ":p": _cmd_print,
    ":print": _cmd_print,
    ":t": _cmd_type,
    ":type": _cmd_type,
    ":b": _cmd_build,
    ":build": _cmd_build,
    ":d": _cmd_doc,
    ":doc": _cmd_doc,
    ":e": _cmd_edit,
    ":edit": _cmd_edit,
    ":run": _cmd_run,
    ":exec": _cmd_exec,
    ":shell": _cmd_shell,
    ":a": _cmd_add,
    ":add": _cmd_add,
    ":l": _cmd_load,
    ":load": _cmd_load,
    ":lf": _cmd_load_flake,
    ":load-flake": _cmd_load_flake,
    ":ll": _cmd_last_loaded,
    ":last-loaded": _cmd_last_loaded,
    ":r": _cmd_reload,
    ":reload": _cmd_reload,
    ":help": _cmd_help,
    ":?": _cmd_help,
    ":verbosity": _cmd_verbosity,
}


async def _run_repl_loop(
    repl: _ReplEngine,
    prompt: Any,
    *,
    initial_loaded: list[str] | None = None,
    initial_sources: list[tuple[str, str]] | None = None,
    line_editors: tuple[str, ...] = (),
) -> None:
    """Read and evaluate lines until the user exits the REPL."""
    state = _ReplState(
        repl=repl,
        line_editors=line_editors,
        loaded=list(initial_sources or []),
        last_loaded=list(initial_loaded or []),
    )

    print_formatted_text(_HELP)
    while True:
        try:
            line = await prompt.prompt_async(_PROMPT)
        except EOFError:
            print_formatted_text("")
            return
        except KeyboardInterrupt:
            print_formatted_text("^C")
            continue

        text = line.strip()
        if not text:
            continue
        command, _, argument = text.partition(" ")
        if command in {":quit", ":q"}:
            return

        try:
            with _interruptible() as scope:
                await _dispatch(state, repl, command, argument, line)
            if scope.cancel_called:
                print_formatted_text("^C")
        except (NixError, ReplRunError) as exc:
            _print_error(exc)


async def _dispatch(
    state: _ReplState,
    repl: _ReplEngine,
    command: str,
    argument: str,
    line: str,
) -> None:
    """Run one REPL input: a command, or an expression to evaluate and print."""
    handler = _COMMAND_HANDLERS.get(command)
    if handler is not None:
        await handler(state, argument)
    elif command.startswith(":"):
        print_formatted_text(f"unknown command: {command}; try :help")
    else:
        value = await repl.line(line)
        if value is not None:
            print_formatted_text(format_json(await repl.to_python(value)))


@contextlib.contextmanager
def _interruptible() -> Generator[anyio.CancelScope]:
    """Let Ctrl-C stop the running evaluation and return to the prompt.

    huggorm turns the cancellation into Nix's interrupt of that one call, and
    waits for the evaluator thread to stop. Nix answers only where it polls
    ``checkInterrupt()``: a store operation stops, and so does a build that
    writes output. A pure evaluation does not poll, and a download and a silent
    build do not reach a poll (nixidae/nixidae#1058), so the wait lasts until
    they end. So the first Ctrl-C also gives SIGINT back to the kernel, and a
    second one ends pynix.

    ``loop.add_signal_handler`` rather than ``signal.signal``, because the
    handler must run on the event loop and cancel a scope. The arm at
    ``prompt_async`` above stays: that one covers Ctrl-C while the user types,
    where prompt_toolkit owns the terminal and no evaluation is in flight.
    """
    loop = asyncio.get_running_loop()
    with anyio.CancelScope() as scope:

        def interrupt() -> None:
            scope.cancel()
            loop.remove_signal_handler(signal.SIGINT)
            signal.signal(signal.SIGINT, signal.SIG_DFL)

        try:
            loop.add_signal_handler(signal.SIGINT, interrupt)
        except NotImplementedError:
            # No signal handlers on this loop (Windows, or a non-main thread).
            # The REPL still works; Ctrl-C just keeps its old behaviour.
            yield scope
            return
        try:
            yield scope
        finally:
            loop.remove_signal_handler(signal.SIGINT)
            signal.signal(signal.SIGINT, signal.default_int_handler)


def _print_error(exc: NixError | ReplRunError) -> None:
    """Print a REPL error, preserving Nix's ANSI diagnostics."""
    if isinstance(exc, NixError):
        print_formatted_text(ANSI(exc.colored))
    else:
        print_formatted_text(f"error: {exc}")


async def _load_initial_target(repl: _ReplEngine, target: EvaluationTarget) -> list[str]:
    """Load a command-line target into the persistent REPL scope."""
    if target.file is None and target.flake is None:
        return []
    names = await repl.add_attrs(await repl.load_target(target))
    print_formatted_text(f"Added {len(names)} variables: {' '.join(names)}")
    return names


class ReplRunError(RuntimeError):
    """A command could not be selected or started by the REPL."""


async def _run_derivation(repl: _ReplEngine, value: AsyncValue) -> int:
    """Build *value* and run it using Nix's derivation executable convention."""
    main_program, warning = await _main_program(repl, value)
    if warning is not None:
        print_formatted_text(f"warning: {warning}")

    outputs = await repl.build(value)
    out_path = _primary_output(outputs)
    program = Path(out_path) / "bin" / main_program
    try:
        process = await anyio.open_process([str(program)], stdin=None, stdout=None, stderr=None)
    except OSError as exc:
        raise ReplRunError(f"cannot run {program}: {exc.strerror}") from exc
    return_code = await process.wait()
    if return_code != 0:
        print_formatted_text(f"warning: {program} exited with status {return_code}")
    return return_code


def _editor_argv(path: str, line: int, line_editors: tuple[str, ...]) -> list[str]:
    """Match Nix's ``editorFor`` argument construction with configured editors."""
    editor = os.environ.get("EDITOR", "cat")
    try:
        argv = shlex.split(editor)
    except ValueError as exc:
        raise ReplRunError(f"cannot parse $EDITOR: {exc}") from exc
    if not argv:
        raise ReplRunError("$EDITOR is empty")
    if line > 0 and any(name in editor for name in line_editors):
        argv.append(f"+{line}")
    argv.append(path)
    return argv


async def _edit(repl: _ReplEngine, value: AsyncValue, line_editors: tuple[str, ...]) -> int:
    """Open the Nix-selected source location for *value* in the user's editor."""
    path, line = await repl.edit_location(value)
    argv = _editor_argv(path, line, line_editors)
    try:
        process = await anyio.open_process(argv, stdin=None, stdout=None, stderr=None)
    except OSError as exc:
        raise ReplRunError(f"cannot start editor {argv[0]!r}: {exc.strerror}") from exc
    return_code = await process.wait()
    if return_code != 0:
        print_formatted_text(f"warning: editor exited with status {return_code}")
    return return_code


async def _exec_argv(argv: list[str]) -> int:
    """Execute a realised Nix argv list without shell parsing."""
    if not argv:
        raise ReplRunError(":exec command list is empty")
    try:
        process = await anyio.open_process(argv, stdin=None, stdout=None, stderr=None)
    except OSError as exc:
        raise ReplRunError(f"cannot run {argv[0]!r}: {exc.strerror}") from exc
    return_code = await process.wait()
    if return_code != 0:
        print_formatted_text(f"warning: {argv[0]} exited with status {return_code}")
    return return_code


def _primary_output(outputs: dict[str, str]) -> str:
    out_path = outputs.get("out")
    if out_path is None:
        raise ReplRunError("derivation has no 'out' output")
    return out_path


async def _shell(command: str) -> int:
    """Execute a realised Nix string with the user's shell."""
    if not command:
        raise ReplRunError(":shell command is empty")
    try:
        process = await anyio.open_process(command, stdin=None, stdout=None, stderr=None)
    except OSError as exc:
        raise ReplRunError(f"cannot start shell: {exc.strerror}") from exc
    return_code = await process.wait()
    if return_code != 0:
        print_formatted_text(f"warning: shell exited with status {return_code}")
    return return_code


async def _string_attr(repl: _ReplEngine, value: AsyncValue | None, name: str) -> str | None:
    """The attribute *name* of *value* when it is there; a non-string raises."""
    found = None if value is None else await repl.attr(value, name)
    if found is None:
        return None
    text = await repl.to_python(found)
    if not isinstance(text, str):
        raise ReplRunError(f"derivation {name} is not a string")
    return text


async def _main_program(repl: _ReplEngine, value: AsyncValue) -> tuple[str, str | None]:
    """Select a derivation executable using the same order as ``nix run``."""
    try:
        main_program = await _string_attr(repl, await repl.attr(value, "meta"), "mainProgram")
    except ReplRunError as exc:
        raise ReplRunError("derivation meta.mainProgram is not a string") from exc
    if main_program is not None:
        return main_program, None

    pname = await _string_attr(repl, value, "pname")
    if pname is not None:
        return pname, f"derivation has no meta.mainProgram; using pname {pname!r}"

    name = await _string_attr(repl, value, "name")
    if name is None:
        raise ReplRunError("derivation has neither meta.mainProgram, pname, nor name")
    main_program = _derivation_name_part(name)
    return main_program, f"derivation has no meta.mainProgram; using name {main_program!r}"


def _derivation_name_part(name: str) -> str:
    """Match Nix's ``DrvName(name).name`` split rule."""
    for index, char in enumerate(name[:-1]):
        if char == "-" and not name[index + 1].isalpha():
            return name[:index]
    return name


async def run_repl(command: Repl) -> None:
    """Open the session, and drive the prompt until the user leaves it.

    The body of ``Repl.run``. It lives here, and not beside the command class,
    because ``prompt_toolkit`` and the Nix grammar are 91.8 ms that every
    ``pynix`` start paid. See the docstring of ``pynix._impl``.
    """
    target = EvaluationTarget.from_command(command)
    try:
        target.validate()
    except EvaluationTargetError as exc:
        print_formatted_text(f"error: {exc}")
        raise SystemExit(1) from exc

    init_nix()
    for feature in _FEATURES:
        huggorm.enable_experimental_feature(feature)
    async with huggorm.AsyncSession(command.store) as session:
        store = session.store()
        state = session.eval(store)
        with patch_stdout():
            async with _forwarding(session, state) as logs:
                repl = _ReplEngine(store, state, await state.repl(), logs)
                prompt: PromptSession[str] = PromptSession(
                    completer=_ReplCompleter(repl),
                    complete_while_typing=True,
                    history=_repl_history(),
                    lexer=_NixLexer(),
                    style=_REPL_STYLE,
                )
                try:
                    initial_loaded = await _load_initial_target(repl, target)
                except EvaluationTargetError as exc:
                    print_formatted_text(f"error: {exc}")
                    raise SystemExit(1) from exc
                initial_sources: list[tuple[str, str]] = []
                if target.file is not None:
                    initial_sources.append((":load", target.file))
                elif target.flake is not None:
                    initial_sources.append((":load-flake", target.flake))
                await _run_repl_loop(
                    repl,
                    prompt,
                    initial_loaded=initial_loaded,
                    initial_sources=initial_sources,
                    line_editors=repl.line_editors,
                )
