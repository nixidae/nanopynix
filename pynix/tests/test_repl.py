"""Tests for the interactive pynix REPL loop."""

# pyright: reportPrivateUsage=false
# Tests intentionally access private symbols from pynix._impl.repl

from __future__ import annotations

import signal
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from huggorm import AsyncValue, Verbosity
from huggorm.errors import NixError
from prompt_toolkit.completion import CompleteEvent, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI, FormattedText

from pynix import _util as util_module, parse
from pynix._impl.repl import (  # type: ignore[reportPrivateUsage] -- tests intentionally access private symbols
    _HELP,
    ReplRunError,
    _AttrDoc,
    _derivation_name_part,
    _edit,
    _editor_argv,
    _exec_argv,
    _forwarding,
    _interruptible,
    _load_initial_target,
    _main_program,
    _nix_input,
    _NixLexer,
    _primary_output,
    _print_error,
    _repl_history,
    _ReplCompleter,
    _ReplEngine,
    _run_derivation,
    _run_repl_loop,
    _shell,
    _ValueDoc,
)
from pynix._verbosity import VerbosityInput, verbosity_of
from pynix.repl import Repl
from pynix.target import EvaluationTarget

if TYPE_CHECKING:
    from huggorm.testing.environment import NixTestEnvironment


class _Prompt:
    def __init__(self, lines: list[str]) -> None:
        self._lines = deque(lines)

    async def prompt_async(self, _prompt: str) -> str:
        return self._lines.popleft()


class _EOFPrompt:
    """A prompt that hangs up immediately, as if stdin were closed."""

    async def prompt_async(self, _prompt: str) -> str:
        raise EOFError


class _InterruptOncePrompt:
    """A prompt whose first read is interrupted (e.g. by Ctrl-C), then behaves normally."""

    def __init__(self) -> None:
        self._interrupted = False

    async def prompt_async(self, _prompt: str) -> str:
        if not self._interrupted:
            self._interrupted = True
            raise KeyboardInterrupt
        return ":quit"


def test_repl_history_uses_xdg_state_home(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    history = _repl_history()
    history.append_string("pkgs.hello")

    assert (tmp_path / "state" / "pynix" / "repl_history").is_file()
    assert list(_repl_history().load_history_strings()) == ["pkgs.hello"]


# Every value double subclasses `AsyncValue`, and every engine double
# `_ReplEngine`, so that beartype's isinstance checks on annotated parameters
# accept them. The engine doubles hand each value operation to the value.
class _Value(AsyncValue):
    def __init__(self) -> None:
        pass

    async def as_python(self) -> object:
        return {"answer": 42}


class _RunValue(AsyncValue):
    def __init__(self, attrs: dict[str, object], outputs: dict[str, str]) -> None:
        self._attrs = attrs
        self._outputs = outputs

    def attr(self, name: str) -> _RunValue | None:
        if name not in self._attrs:
            return None
        value = self._attrs[name]
        return value if isinstance(value, _RunValue) else _RunValue({"value": value}, {})

    async def as_python(self) -> object:
        return self._attrs["value"]

    async def build(self) -> dict[str, str]:
        return self._outputs


class _CommandValue(AsyncValue):
    def __init__(self, *, argv: list[str] | None = None, command: str | None = None) -> None:
        self._argv = argv
        self._command = command

    async def realise_argv(self) -> list[str]:
        if self._argv is None:
            raise AssertionError("unexpected :exec value")
        return self._argv

    async def realise_string(self) -> str:
        if self._command is None:
            raise AssertionError("unexpected :shell value")
        return self._command


class _EditValue(AsyncValue):
    def __init__(self) -> None:
        pass

    async def location(self) -> tuple[str, int]:
        return "/tmp/source.nix", 42


class _TypedValue(AsyncValue):
    def __init__(self) -> None:
        pass

    async def type_name(self) -> str:
        return "int"


class _DocValue(AsyncValue):
    def __init__(self, doc: _ValueDoc | None) -> None:
        self.value_doc = doc


class _AttrDocValue(AsyncValue):
    def __init__(self, attr_doc: _AttrDoc | None) -> None:
        self.value_doc = None
        self.found = attr_doc


type _AnyValue = _Value | _RunValue | _CommandValue | _EditValue | _TypedValue | _DocValue | _AttrDocValue


class _Repl(_ReplEngine):
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.loaded_files: list[str] = []
        self.loaded_flakes: list[str] = []
        self.file_cache_resets = 0
        self.value: _AnyValue = _Value()
        self.verbosity = Verbosity.NOTICE
        self.raise_on_line: str | None = None

    async def line(self, text: str) -> _Value | None:
        if text == self.raise_on_line:
            raise NixError("boom failed")
        self.lines.append(text)
        return None if text == "answer = 42" else _Value()

    async def load_file(self, path: str) -> _Value:
        self.loaded_files.append(path)
        return _Value()

    async def load_target(self, target: EvaluationTarget) -> _Value:
        assert target.file is not None
        return await self.load_file(target.file)

    async def eval_flake(self, ref: str) -> _Value:
        self.loaded_flakes.append(ref)
        return _Value()

    async def add_attrs(self, value: AsyncValue) -> list[str]:
        return ["answer"]

    async def reset_file_cache(self) -> None:
        self.file_cache_resets += 1

    async def get_verbosity(self) -> Verbosity:
        return self.verbosity

    async def set_verbosity(self, level: VerbosityInput) -> Verbosity:
        self.verbosity = verbosity_of(level)
        return self.verbosity

    async def repl_select(self, expr: str) -> tuple[str, AsyncValue] | None:
        if "." in expr:
            _prefix, _, name = expr.rpartition(".")
            return name, self.value
        return None

    async def string(self, expr: str) -> _AnyValue:
        return self.value

    async def to_python(self, value: Any) -> object:
        return await value.as_python()

    async def type_name(self, value: Any) -> str:
        return await value.type_name()

    async def attr(self, value: Any, name: str) -> AsyncValue | None:
        return value.attr(name)

    async def build(self, value: Any) -> dict[str, str]:
        return await value.build()

    async def doc(self, value: Any) -> _ValueDoc | None:
        return value.value_doc

    async def attr_doc(self, value: Any, name: str) -> _AttrDoc | None:
        return value.found

    async def edit_location(self, value: Any) -> tuple[str, int]:
        return await value.location()

    async def realise_argv(self, value: Any) -> list[str]:
        return await value.realise_argv()

    async def realise_string(self, value: Any) -> str:
        return await value.realise_string()


class _CompletionValue(AsyncValue):
    def __init__(self, names: list[str]) -> None:
        self.attr_names = names
        self.released = False


class _CompletionRepl(_ReplEngine):
    def __init__(self) -> None:
        self.value = _CompletionValue(["hello", "hello-unfree", "world"])

    async def scope_names(self) -> list[str]:
        return ["answer", "builtins", "pkgs"]

    async def string(self, expr: str) -> _CompletionValue:
        if expr == "badexpr":
            raise NixError("badexpr is undefined")
        assert expr == "pkgs"
        return self.value

    async def attr_names(self, value: Any) -> list[str]:
        return value.attr_names

    async def release(self, value: Any) -> None:
        value.released = True


async def test_repl_loop_keeps_bindings_and_prints_expression_values(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    repl = _Repl()

    await _run_repl_loop(repl, _Prompt(["answer = 42", "{ inherit answer; }", ":quit"]))

    assert repl.lines == ["answer = 42", "{ inherit answer; }"]
    assert output == [
        _HELP,
        '{\n  "answer": 42\n}',
    ]


async def test_repl_load_command_uses_repl_load_file(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    repl = _Repl()

    await _run_repl_loop(repl, _Prompt([":load .", ":quit"]))

    assert repl.loaded_files == ["."]
    assert output == [_HELP, "Added 1 variables: answer"]


async def test_repl_loads_file_target_into_initial_scope(monkeypatch: Any, tmp_path: Path) -> None:
    (tmp_path / "default.nix").write_text("{ answer = 42; }\n")
    monkeypatch.chdir(tmp_path)
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    repl = _Repl()

    names = await _load_initial_target(repl, EvaluationTarget(file="default.nix", attr=None, flake=None))

    assert names == ["answer"]
    assert repl.loaded_files == ["default.nix"]
    assert output == ["Added 1 variables: answer"]


async def test_repl_last_loaded_includes_initial_target(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    repl = _Repl()

    await _run_repl_loop(repl, _Prompt([":ll", ":quit"]), initial_loaded=["flake", "pkgs"])

    assert output == [_HELP, "flake pkgs"]


async def test_repl_verbosity_shows_and_updates_nix_log_level(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    repl = _Repl()

    await _run_repl_loop(repl, _Prompt([":verbosity", ":verbosity debug", ":verbosity", ":quit"]))

    assert output == [_HELP, "notice (2)", "debug (6)", "debug (6)"]


def test_repl_preserves_nix_error_ansi(monkeypatch: Any) -> None:
    output: list[object] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)

    error = NixError("\x1b[31;1merror:\x1b[0m undefined variable 'll'")
    _print_error(error)

    assert len(output) == 1
    assert isinstance(output[0], ANSI)
    assert output[0].value == error.colored


def test_editor_argv_only_includes_line_for_line_aware_editors(monkeypatch: Any) -> None:
    monkeypatch.setenv("EDITOR", "hx --health")
    assert _editor_argv("/tmp/source.nix", 42, ("hx",)) == ["hx", "--health", "+42", "/tmp/source.nix"]

    monkeypatch.setenv("EDITOR", "cat")
    assert _editor_argv("/tmp/source.nix", 42, ("hx",)) == ["cat", "/tmp/source.nix"]


async def test_repl_edit_reloads_initial_sources(monkeypatch: Any) -> None:
    output: list[str] = []
    edited: list[_EditValue] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)

    async def edit(_repl: _Repl, value: _EditValue, line_editors: tuple[str, ...]) -> int:
        edited.append(value)
        assert line_editors == ("hx",)
        return 0

    monkeypatch.setattr("pynix._impl.repl._edit", edit)
    repl = _Repl()
    value = _EditValue()
    repl.value = value

    await _run_repl_loop(
        repl,
        _Prompt([":edit target", ":quit"]),
        initial_sources=[(":load", "default.nix")],
        line_editors=("hx",),
    )

    assert edited == [value]
    assert repl.file_cache_resets == 1
    assert repl.loaded_files == ["default.nix"]
    assert output == [_HELP, "Added 1 variables: answer"]


async def test_repl_run_prefers_meta_main_program(tmp_path: Path, monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    out = tmp_path / "out"
    program = out / "bin" / "actual-program"
    program.parent.mkdir(parents=True)
    marker = tmp_path / "ran"
    program.write_text(f"#!/bin/sh\necho ran > {marker}\n")
    program.chmod(0o755)
    value = _RunValue({"meta": _RunValue({"mainProgram": "actual-program"}, {})}, {"out": str(out)})

    assert await _run_derivation(_Repl(), value) == 0
    assert marker.read_text() == "ran\n"
    assert output == []


async def test_repl_run_command_runs_evaluated_derivation(tmp_path: Path, monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    out = tmp_path / "out"
    program = out / "bin" / "run-me"
    program.parent.mkdir(parents=True)
    marker = tmp_path / "ran"
    program.write_text(f"#!/bin/sh\necho ran > {marker}\n")
    program.chmod(0o755)
    repl = _Repl()
    repl.value = _RunValue({"meta": _RunValue({"mainProgram": "run-me"}, {})}, {"out": str(out)})

    await _run_repl_loop(repl, _Prompt([":run package", ":quit"]))

    assert marker.read_text() == "ran\n"
    assert output == [_HELP]


async def test_repl_exec_runs_realised_argv_list(tmp_path: Path, monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    marker = tmp_path / "exec-ran"
    repl = _Repl()
    repl.value = _CommandValue(argv=["/bin/sh", "-c", f"echo exec > {marker}"])

    await _run_repl_loop(repl, _Prompt([":exec [ command ]", ":quit"]))

    assert marker.read_text() == "exec\n"
    assert output == [_HELP]


async def test_repl_shell_runs_realised_string(tmp_path: Path, monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    marker = tmp_path / "shell-ran"
    repl = _Repl()
    repl.value = _CommandValue(command=f"echo shell > {marker}")

    await _run_repl_loop(repl, _Prompt([":shell command", ":quit"]))

    assert marker.read_text() == "shell\n"
    assert output == [_HELP]


async def test_repl_run_warns_when_using_pname(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    value = _RunValue({"pname": "fallback-program"}, {})

    assert await _main_program(_Repl(), value) == (
        "fallback-program",
        "derivation has no meta.mainProgram; using pname 'fallback-program'",
    )
    assert _derivation_name_part("apache-httpd-2.0.48") == "apache-httpd"


async def test_repl_completion_uses_commands_scope_and_attrsets() -> None:
    completer = _ReplCompleter(_CompletionRepl())

    async def complete(text: str) -> list[str]:
        document = Document(text, cursor_position=len(text))
        return [item.text async for item in completer.get_completions_async(document, CompleteEvent())]

    assert await complete(":lo") == [":load", ":load-flake"]
    assert await complete("ans") == ["answer"]
    assert await complete(':shell "${') == ["answer", "builtins", "pkgs"]
    assert await complete("pkgs.") == ["hello", "hello-unfree", "world"]
    assert await complete("pkgs.hel") == ["hello", "hello-unfree"]
    assert await complete(':shell "${pk') == ["pkgs"]
    assert await complete(':shell "${pkgs.hel') == ["hello", "hello-unfree"]
    assert await complete(':shell "${pkgs.') == ["hello", "hello-unfree", "world"]
    assert await complete(":e p") == ["pkgs"]
    assert await complete(":e pkgs.") == ["hello", "hello-unfree", "world"]
    assert await complete(":e pkgs.hel") == ["hello", "hello-unfree"]
    assert completer._repl.value.released  # type: ignore[reportPrivateUsage] -- verifies temporary value lifetime


async def test_repl_completion_on_an_empty_line_waits_for_a_keypress() -> None:
    """Tab on an empty line offers every root binding, and typing offers none.

    The shell convention is that Tab on an empty word offers everything that
    can go there. `complete_while_typing` is on, so a completer that answered
    an empty line unasked would open the menu as soon as the prompt appeared.
    """
    completer = _ReplCompleter(_CompletionRepl())
    document = Document("")

    async def complete(event: CompleteEvent) -> list[Completion]:
        return [item async for item in completer.get_completions_async(document, event)]

    offered = await complete(CompleteEvent(completion_requested=True))
    assert [item.text for item in offered] == ["answer", "builtins", "pkgs"]
    # Zero, so that the name is added and no typed character is replaced.
    assert {item.start_position for item in offered} == {0}
    assert await complete(CompleteEvent()) == []


async def test_repl_completion_after_an_expression_command_waits_for_a_keypress() -> None:
    """Tab after ":e " offers every root binding, and typing offers none.

    An expression command with nothing typed after the space is a bare
    position, like an empty line. `complete_while_typing` is on, so answering
    the space itself would pop the menu open the moment it is typed.
    """
    completer = _ReplCompleter(_CompletionRepl())
    document = Document(":e ", cursor_position=len(":e "))

    async def complete(event: CompleteEvent) -> list[Completion]:
        return [item async for item in completer.get_completions_async(document, event)]

    offered = await complete(CompleteEvent(completion_requested=True))
    assert [item.text for item in offered] == ["answer", "builtins", "pkgs"]
    # Zero, so that the name is added after the space and nothing is replaced.
    assert {item.start_position for item in offered} == {0}
    assert await complete(CompleteEvent()) == []


async def test_repl_completer_sync_hook_returns_no_completions() -> None:
    """prompt_toolkit calls the sync hook first; it must yield nothing so the async hook takes over."""
    completer = _ReplCompleter(_CompletionRepl())
    document = Document("pkgs.")

    assert list(completer.get_completions(document, CompleteEvent())) == []


async def test_repl_completer_returns_nothing_for_a_non_expression_command() -> None:
    completer = _ReplCompleter(_CompletionRepl())
    document = Document(":quit ", cursor_position=len(":quit "))

    completions = [item.text async for item in completer.get_completions_async(document, CompleteEvent())]

    assert completions == []


async def test_repl_completer_returns_nothing_when_cursor_has_no_completable_target() -> None:
    completer = _ReplCompleter(_CompletionRepl())
    document = Document("1 + 1 ", cursor_position=len("1 + 1 "))

    completions = [item.text async for item in completer.get_completions_async(document, CompleteEvent())]

    assert completions == []


async def test_repl_completer_swallows_nix_errors_from_attr_lookup() -> None:
    """A NixError while resolving attrs for completion must not blow up the prompt."""
    completer = _ReplCompleter(_CompletionRepl())
    document = Document("badexpr.", cursor_position=len("badexpr."))

    completions = [item.text async for item in completer.get_completions_async(document, CompleteEvent())]

    assert completions == []


def test_repl_tree_sitter_lexer_highlights_nix_expression() -> None:
    document = Document(':shell let x = 1; in "${x}"')

    fragments = _NixLexer().lex_document(document)(0)

    assert ("class:nix.keyword", "let") in fragments
    assert ("class:nix.number", "1") in fragments
    assert ("class:nix.punctuation", "${") in fragments
    assert ("class:nix.string", '"') in fragments


def test_repl_tree_sitter_lexer_highlights_plain_variable_references() -> None:
    """Regression test: tree-sitter-nix 0.5.0 dropped the blanket @property
    fallback that used to cover every identifier, so plain variable references
    (as opposed to binding names or formals) need their own explicit capture
    mapping or they silently stop being highlighted at all.
    """
    document = Document(":shell let x = 1; in x")

    fragments = _NixLexer().lex_document(document)(0)

    assert ("class:nix.property", "x") in fragments  # the `x = 1` binding name
    assert ("class:nix.variable", "x") in fragments  # the `in x` reference


def test_repl_tree_sitter_lexer_skips_highlighting_for_non_expression_input() -> None:
    """A bare, argument-less command (e.g. ":quit") has no Nix source to highlight."""
    document = Document(":quit")

    fragments = _NixLexer().lex_document(document)(0)

    assert fragments == [("", ":quit")]


def test_repl_is_a_pynix_subcommand() -> None:
    assert isinstance(parse(["repl"]), Repl)


def test_repl_accepts_file_and_attr_options() -> None:
    command = parse(["repl", "--file", "default.nix", "--attr", "pkgs"])

    assert isinstance(command, Repl)
    assert command.file == "default.nix"
    assert command.attr == "pkgs"


# ── _nix_input: dumb coverage tests ─────────────────────────────────────
# _NixLexer and _ReplCompleter only ever feed _nix_input full, well-formed
# REPL lines, so the integration-style tests above never reach its
# argument-less-command and unrecognized-command branches. These pin each
# branch down directly.


def test_nix_input_plain_expression_has_zero_offset() -> None:
    assert _nix_input("1 + 1") == ("1 + 1", 0)


def test_nix_input_returns_none_for_command_without_argument() -> None:
    assert _nix_input(":build") is None


def test_nix_input_returns_none_for_non_expression_command() -> None:
    assert _nix_input(":quit extra") is None


def test_nix_input_returns_argument_and_offset_for_expression_command() -> None:
    assert _nix_input(":print 1 + 1") == ("1 + 1", len(":print "))


# ── _run_repl_loop: prompt lifecycle and command dispatch ───────────────


async def test_repl_loop_returns_quietly_on_eof(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)

    await _run_repl_loop(_Repl(), _EOFPrompt())

    assert output == [_HELP, ""]


async def test_repl_loop_recovers_from_keyboard_interrupt(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)

    await _run_repl_loop(_Repl(), _InterruptOncePrompt())

    assert output == [_HELP, "^C"]


async def test_repl_loop_skips_blank_lines(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    repl = _Repl()

    await _run_repl_loop(repl, _Prompt(["   ", ":quit"]))

    assert repl.lines == []
    assert output == [_HELP]


async def test_repl_loop_help_command_reprints_help(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)

    await _run_repl_loop(_Repl(), _Prompt([":help", ":quit"]))

    assert output == [_HELP, _HELP]


async def test_repl_loop_unknown_command_reports_error(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)

    await _run_repl_loop(_Repl(), _Prompt([":bogus", ":quit"]))

    assert output == [_HELP, "unknown command: :bogus; try :help"]


async def test_repl_loop_print_command_evaluates_expression(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)

    await _run_repl_loop(_Repl(), _Prompt([":print 1 + 1", ":quit"]))

    assert output == [_HELP, '{\n  "answer": 42\n}']


async def test_repl_loop_type_command_shows_nix_type(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    repl = _Repl()
    repl.value = _TypedValue()

    await _run_repl_loop(repl, _Prompt([":type 1", ":quit"]))

    assert output == [_HELP, "int"]


async def test_repl_loop_build_command_prints_outputs(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    repl = _Repl()
    repl.value = _RunValue({}, {"out": "/nix/store/xxx-out"})

    await _run_repl_loop(repl, _Prompt([":build pkgs.hello", ":quit"]))

    assert output == [_HELP, '{\n  "out": "/nix/store/xxx-out"\n}']


async def test_repl_loop_add_command_adds_attrs(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)

    await _run_repl_loop(_Repl(), _Prompt([":add pkgs", ":quit"]))

    assert output == [_HELP, "Added 1 variables: answer"]


async def test_repl_loop_load_flake_command_uses_repl_eval_flake(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    repl = _Repl()

    await _run_repl_loop(repl, _Prompt([":load-flake .#hello", ":quit"]))

    assert repl.loaded_flakes == [".#hello"]
    assert output == [_HELP, "Added 1 variables: answer"]


async def test_repl_loop_reload_command_reruns_loaded_sources(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    repl = _Repl()

    await _run_repl_loop(repl, _Prompt([":reload", ":quit"]), initial_sources=[(":load", "default.nix")])

    assert repl.file_cache_resets == 1
    assert repl.loaded_files == ["default.nix"]
    assert output == [_HELP, "Added 1 variables: answer"]


async def test_repl_loop_verbosity_reports_invalid_level(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)

    await _run_repl_loop(_Repl(), _Prompt([":verbosity bogus-level", ":quit"]))

    assert len(output) == 2
    assert output[0] == _HELP
    assert output[1].startswith("error: unknown verbosity 'bogus-level'")


async def test_repl_loop_reports_nix_error_from_a_line_expression(monkeypatch: Any) -> None:
    """The loop's except clause must also fire for plain-expression evaluation, not just commands."""
    output: list[object] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    repl = _Repl()
    repl.raise_on_line = "boom"

    await _run_repl_loop(repl, _Prompt(["boom", ":quit"]))

    assert len(output) == 2
    assert isinstance(output[1], ANSI)


def test_print_error_formats_repl_run_error_as_plain_text(monkeypatch: Any) -> None:
    """Unlike NixError, a ReplRunError has no ANSI diagnostics to preserve."""
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)

    _print_error(ReplRunError("boom"))

    assert output == ["error: boom"]


async def test_load_initial_target_returns_empty_when_no_file_or_flake_given() -> None:
    names = await _load_initial_target(_Repl(), EvaluationTarget(file=None, attr=None, flake=None))

    assert names == []


# ── _run_derivation: build/run success, warnings, and failures ──────────


async def test_run_derivation_prints_main_program_fallback_warning(tmp_path: Path, monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    out = tmp_path / "out"
    program = out / "bin" / "fallback-program"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\ntrue\n")
    program.chmod(0o755)
    value = _RunValue({"pname": "fallback-program"}, {"out": str(out)})

    assert await _run_derivation(_Repl(), value) == 0
    assert output == ["warning: derivation has no meta.mainProgram; using pname 'fallback-program'"]


async def test_run_derivation_reports_exec_failure(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    value = _RunValue({"meta": _RunValue({"mainProgram": "missing-program"}, {})}, {"out": str(out)})

    with pytest.raises(ReplRunError, match="cannot run"):
        await _run_derivation(_Repl(), value)


async def test_run_derivation_warns_on_nonzero_exit(tmp_path: Path, monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    out = tmp_path / "out"
    program = out / "bin" / "failing"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\nexit 1\n")
    program.chmod(0o755)
    value = _RunValue({"meta": _RunValue({"mainProgram": "failing"}, {})}, {"out": str(out)})

    assert await _run_derivation(_Repl(), value) == 1
    assert output == [f"warning: {program} exited with status 1"]


# ── _editor_argv / _edit: editor launch edge cases ───────────────────────


def test_editor_argv_rejects_unparsable_editor(monkeypatch: Any) -> None:
    monkeypatch.setenv("EDITOR", 'hx "unterminated')

    with pytest.raises(ReplRunError, match=r"cannot parse \$EDITOR"):
        _editor_argv("/tmp/source.nix", 1, ())


def test_editor_argv_rejects_empty_editor(monkeypatch: Any) -> None:
    monkeypatch.setenv("EDITOR", "   ")

    with pytest.raises(ReplRunError, match=r"\$EDITOR is empty"):
        _editor_argv("/tmp/source.nix", 1, ())


async def test_edit_runs_editor_and_returns_code(tmp_path: Path, monkeypatch: Any) -> None:
    marker = tmp_path / "edited"
    editor = tmp_path / "editor.sh"
    editor.write_text(f"#!/bin/sh\necho edited > {marker}\n")
    editor.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(editor))

    assert await _edit(_Repl(), _EditValue(), ()) == 0
    assert marker.read_text() == "edited\n"


async def test_edit_reports_launch_failure(monkeypatch: Any) -> None:
    monkeypatch.setenv("EDITOR", "/nonexistent/editor-binary")

    with pytest.raises(ReplRunError, match="cannot start editor"):
        await _edit(_Repl(), _EditValue(), ())


async def test_edit_warns_on_nonzero_exit(tmp_path: Path, monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    editor = tmp_path / "editor.sh"
    editor.write_text("#!/bin/sh\nexit 3\n")
    editor.chmod(0o755)
    monkeypatch.setenv("EDITOR", str(editor))

    assert await _edit(_Repl(), _EditValue(), ()) == 3
    assert output == ["warning: editor exited with status 3"]


# ── _exec_argv / _shell / _primary_output: dumb coverage tests ──────────
# These are only reached today through the ":exec"/":shell"/":build" REPL
# commands with hand-picked happy-path fixtures elsewhere in this file, so
# their empty-input, launch-failure, and nonzero-exit branches are
# otherwise untested.


async def test_exec_argv_rejects_empty_list() -> None:
    with pytest.raises(ReplRunError, match="command list is empty"):
        await _exec_argv([])


async def test_exec_argv_reports_launch_failure() -> None:
    with pytest.raises(ReplRunError, match="cannot run"):
        await _exec_argv(["/nonexistent/binary-xyz"])


async def test_exec_argv_warns_on_nonzero_exit(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)

    return_code = await _exec_argv(["/bin/sh", "-c", "exit 7"])

    assert return_code == 7
    assert output == ["warning: /bin/sh exited with status 7"]


def test_primary_output_rejects_missing_out() -> None:
    with pytest.raises(ReplRunError, match="no 'out' output"):
        _primary_output({"lib": "/nix/store/xxx"})


async def test_shell_rejects_empty_command() -> None:
    with pytest.raises(ReplRunError, match="command is empty"):
        await _shell("")


async def test_shell_reports_launch_failure(monkeypatch: Any) -> None:
    # Patches `anyio.open_process`, which is what `_shell` actually calls.
    #
    # This used to patch `asyncio.create_subprocess_shell` -- an implementation
    # detail of anyio's asyncio backend, and one it stopped using in 4.14.2 in
    # favour of `loop.subprocess_shell()`. The patch then intercepted nothing,
    # the test really ran `echo hi`, and it failed on DID NOT RAISE rather than
    # on anything about pynix. Patching the boundary the code under test calls
    # is both the correct scope and immune to that happening again.
    async def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError("boom")

    monkeypatch.setattr(anyio, "open_process", _raise)

    with pytest.raises(ReplRunError, match="cannot start shell"):
        await _shell("echo hi")


async def test_shell_warns_on_nonzero_exit(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)

    return_code = await _shell("exit 5")

    assert return_code == 5
    assert output == ["warning: shell exited with status 5"]


# ── _main_program / _derivation_name_part: remaining branches ───────────


async def test_main_program_falls_back_to_pname_when_meta_lacks_main_program() -> None:
    value = _RunValue({"meta": _RunValue({}, {}), "pname": "fallback"}, {})

    assert await _main_program(_Repl(), value) == (
        "fallback",
        "derivation has no meta.mainProgram; using pname 'fallback'",
    )


async def test_main_program_rejects_non_string_meta_main_program() -> None:
    value = _RunValue({"meta": _RunValue({"mainProgram": 123}, {})}, {})

    with pytest.raises(ReplRunError, match=r"meta\.mainProgram is not a string"):
        await _main_program(_Repl(), value)


async def test_main_program_rejects_non_string_pname() -> None:
    value = _RunValue({"pname": 123}, {})

    with pytest.raises(ReplRunError, match="pname is not a string"):
        await _main_program(_Repl(), value)


async def test_main_program_rejects_derivation_with_no_identifying_attrs() -> None:
    value = _RunValue({}, {})

    with pytest.raises(ReplRunError, match=r"has neither meta\.mainProgram, pname, nor name"):
        await _main_program(_Repl(), value)


async def test_main_program_rejects_non_string_name() -> None:
    value = _RunValue({"name": 123}, {})

    with pytest.raises(ReplRunError, match="name is not a string"):
        await _main_program(_Repl(), value)


async def test_main_program_falls_back_to_derivation_name() -> None:
    value = _RunValue({"name": "hello-1.2.3"}, {})

    assert await _main_program(_Repl(), value) == (
        "hello",
        "derivation has no meta.mainProgram; using name 'hello'",
    )


def test_derivation_name_part_keeps_dashless_names_whole() -> None:
    assert _derivation_name_part("hello") == "hello"


# ── Repl.run(): full interactive session, end to end ─────────────────────


class _ScriptedPromptSession:
    """Stand-in for prompt_toolkit's PromptSession, feeding scripted lines then EOF."""

    def __init__(self, lines: list[str], **_kwargs: object) -> None:
        self._lines = deque(lines)

    async def prompt_async(self, _prompt: str) -> str:
        if not self._lines:
            raise EOFError
        return self._lines.popleft()


async def test_repl_run_executes_a_full_interactive_session(
    shared_nix_environment: NixTestEnvironment,
    monkeypatch: Any,
) -> None:
    """Exercises Repl.run() itself against a real store/session with a scripted prompt.

    Everything else in this file drives _run_repl_loop directly with fakes;
    nothing else builds and runs the real command end to end, so Repl.run()
    (PromptSession/store/repl-session wiring) has no coverage otherwise.
    """

    def _fake_prompt_session(**kwargs: object) -> _ScriptedPromptSession:
        return _ScriptedPromptSession(["1 + 1", ":quit"], **kwargs)

    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    monkeypatch.setattr("pynix._impl.repl.PromptSession", _fake_prompt_session)

    cmd = parse(["repl", *shared_nix_environment.pynix_store_args()])
    await cmd.run()

    assert output[0] == _HELP
    assert "2" in output[1]


async def test_repl_run_rejects_mutually_exclusive_file_and_flake(tmp_path: Path, monkeypatch: Any) -> None:
    """Exercises Repl.run()'s own target.validate() error handling before any store is opened."""
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{}")

    cmd = parse(["repl", "--file", str(nix_file), "--flake", ".#hello"])

    with pytest.raises(SystemExit):
        await cmd.run()
    assert output == ["error: --file and --flake are mutually exclusive"]


async def test_repl_run_loads_a_file_target_into_initial_scope(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Covers Repl.run()'s --file initial_sources branch, not exercised by the plain-session test above."""

    def _fake_prompt_session(**kwargs: object) -> _ScriptedPromptSession:
        return _ScriptedPromptSession([":quit"], **kwargs)

    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    monkeypatch.setattr("pynix._impl.repl.PromptSession", _fake_prompt_session)
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ answer = 42; }")

    cmd = parse(["repl", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()])
    await cmd.run()

    assert output == ["Added 1 variables: answer", _HELP]


async def test_repl_run_loads_a_flake_target_into_initial_scope(
    shared_nix_environment: NixTestEnvironment,
    git_flake: Path,
    monkeypatch: Any,
) -> None:
    """Covers Repl.run()'s --flake initial_sources branch (the --file variant is covered above)."""

    def _fake_prompt_session(**kwargs: object) -> _ScriptedPromptSession:
        return _ScriptedPromptSession([":quit"], **kwargs)

    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    monkeypatch.setattr("pynix._impl.repl.PromptSession", _fake_prompt_session)

    cmd = parse(["repl", "--flake", str(git_flake), *shared_nix_environment.pynix_store_args()])
    await cmd.run()

    assert output[0].startswith("Added ")
    assert "hello" in output[0].split()
    assert "greeting" in output[0].split()
    assert output[1] == _HELP


async def test_repl_run_reports_missing_attr_in_initial_target(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Covers Repl.run()'s EvaluationTargetError handling around _load_initial_target."""

    def _fake_prompt_session(**kwargs: object) -> _ScriptedPromptSession:
        return _ScriptedPromptSession([":quit"], **kwargs)

    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    monkeypatch.setattr("pynix._impl.repl.PromptSession", _fake_prompt_session)
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ answer = 42; }")

    cmd = parse(
        ["repl", "--file", str(nix_file), "--attr", "missing", *shared_nix_environment.pynix_store_args()],
    )

    with pytest.raises(SystemExit):
        await cmd.run()
    assert len(output) == 1
    assert output[0].startswith("error: attribute 'missing' not found")


class _HangingRepl(_Repl):
    """An evaluation that never ends on its own, and raises SIGINT once inside it.

    Stands in for `let x = x; in x` at the prompt. The signal has to be raised
    from here rather than from the test body: `_interruptible` installs its
    SIGINT handler only for the duration of the dispatch, and a signal raised
    outside that window would reach Python's default handler instead.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered = 0

    async def line(self, text: str) -> _Value | None:
        self.entered += 1
        signal.raise_signal(signal.SIGINT)
        # Gives the loop a chance to run the signal callback, which cancels the
        # scope around this call.
        await anyio.sleep_forever()
        raise AssertionError("the evaluation was not cancelled")


async def test_repl_loop_returns_to_the_prompt_when_ctrl_c_stops_an_evaluation(
    monkeypatch: Any,
) -> None:
    """Ctrl-C during an evaluation cancels it; it does not end the REPL.

    Before #37, the loop caught KeyboardInterrupt around `prompt_async` only.
    Ctrl-C while Nix was working cancelled the main task, the REPL exited, and
    the evaluator thread ran on unwatched.
    """
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    repl = _HangingRepl()

    with anyio.fail_after(10):
        await _run_repl_loop(repl, _Prompt(["1 + 1", "2 + 2", ":quit"]))

    # Both expressions were entered, so the first Ctrl-C returned to the prompt
    # rather than leaving the loop.
    assert repl.entered == 2
    assert output == [_HELP, "^C", "^C"]


async def test_a_second_ctrl_c_ends_pynix_when_nix_ignores_the_first() -> None:
    """After the first Ctrl-C, SIGINT has the kernel's default action again.

    huggorm waits for the evaluator thread to stop, and a pure evaluation never
    polls for the interrupt. A handler on the event loop could only cancel
    again, so the second Ctrl-C would do nothing until the evaluation ends.
    """
    with _interruptible() as scope:
        signal.raise_signal(signal.SIGINT)
        # Shielded, so the loop runs the signal callback and this sleep ends.
        with anyio.CancelScope(shield=True):
            await anyio.sleep(0.1)
        assert scope.cancel_called
        assert signal.getsignal(signal.SIGINT) is signal.SIG_DFL
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


async def test_ctrl_c_stops_a_build_and_returns_to_the_prompt(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """A real build polls for the interrupt, so Ctrl-C ends it and the loop goes on.

    The interrupt comes after a delay, and not on a sign from the builder: the
    sandbox lets the builder write nothing the test can see. The evaluation
    takes milliseconds, so the delay lands inside the build, which never ends.

    The sandbox has no `sleep`, so the builder counts in the shell. It writes a
    line on each round: Nix polls for the interrupt only when a child's output
    wakes the build loop (nixidae/nixidae#1058).
    """
    output: list[object] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    spin = "while :; do echo tick; i=0; while [ $i -lt 20000 ]; do i=$((i+1)); done; done"
    build = (
        f'derivation {{ name = "repl-interrupt-{tmp_path.name}"; system = builtins.currentSystem; '
        f'builder = "/bin/sh"; args = [ "-c" "{spin}" ]; }}'
    )

    async def interrupt_the_build() -> None:
        await anyio.sleep(3)
        signal.raise_signal(signal.SIGINT)

    async with (
        util_module.huggorm_eval_session(shared_nix_environment.store_uri) as (session, store, state),
        _forwarding(session, state) as logs,
        anyio.create_task_group() as tg,
    ):
        engine = _ReplEngine(store, state, await state.repl(), logs)
        tg.start_soon(interrupt_the_build)
        with anyio.fail_after(30):
            await _run_repl_loop(engine, _Prompt([f":b {build}", "1 + 1", ":quit"]))

    assert output == [_HELP, "^C", "2"]


async def test_the_repl_leaves_no_sigint_handler_behind() -> None:
    """The handler belongs to one evaluation, not to the process.

    Inside the scope, SIGINT reaches asyncio and cancels the evaluation.
    Outside it, Ctrl-C must raise KeyboardInterrupt again -- that is what the
    arm around `prompt_async` catches, and what a caller of pynix expects of
    its own process afterwards.
    """
    with _interruptible():
        assert signal.getsignal(signal.SIGINT) is not signal.default_int_handler
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def _doc_text(printed: object) -> str:
    """The visible text of what `:doc` printed.

    `render_markdown` gives `FormattedText` since issue #255, and no longer an
    `ANSI` object with escape bytes inside it. A `NixError` still reaches the
    screen as `ANSI`, so the type is what tells the two apart.
    """
    assert isinstance(printed, FormattedText)
    return "".join(fragment[1] for fragment in printed)


async def test_repl_doc_builtin(monkeypatch: Any) -> None:
    output: list[object] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    repl = _Repl()
    repl.value = _DocValue(_ValueDoc(name="add", args=("e1", "e2"), doc="Return the sum of the numbers e1 and e2."))

    await _run_repl_loop(repl, _Prompt([":doc builtins.add", ":quit"]))
    assert len(output) == 2
    assert output[0] == _HELP
    drawn = _doc_text(output[1])
    assert "builtins.add" in drawn
    assert "Return the sum of the numbers" in drawn


async def test_repl_doc_lambda(monkeypatch: Any) -> None:
    output: list[object] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    repl = _Repl()
    repl.value = _DocValue(_ValueDoc(name=None, args=(), doc="Function `f`\n  … defined at /tmp/f.nix:1\n\nAdds one."))

    await _run_repl_loop(repl, _Prompt([":doc f", ":quit"]))
    assert len(output) == 2
    assert output[0] == _HELP
    drawn = _doc_text(output[1])
    assert "Function" in drawn
    assert "/tmp/f.nix:1" in drawn
    assert "Adds one." in drawn


async def test_repl_doc_attr_selection(monkeypatch: Any) -> None:
    output: list[object] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    repl = _Repl()
    repl.value = _AttrDocValue(_AttrDoc(path="/tmp/pkgs.nix", line=42, doc="Hello package."))

    await _run_repl_loop(repl, _Prompt([":doc pkgs.hello", ":quit"]))
    assert len(output) == 2
    assert output[0] == _HELP
    drawn = _doc_text(output[1])
    assert "Attribute" in drawn
    assert "hello" in drawn
    assert "/tmp/pkgs.nix:42" in drawn
    assert "Hello package." in drawn


async def test_repl_doc_attr_selection_no_doc(monkeypatch: Any) -> None:
    output: list[object] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    repl = _Repl()
    repl.value = _AttrDocValue(_AttrDoc(path="/tmp/pkgs.nix", line=42, doc=None))

    await _run_repl_loop(repl, _Prompt([":doc pkgs.hello", ":quit"]))
    assert len(output) == 2
    assert output[0] == _HELP
    drawn = _doc_text(output[1])
    assert "Attribute" in drawn
    assert "hello" in drawn
    assert "/tmp/pkgs.nix:42" in drawn
    assert "No documentation found." in drawn


async def test_repl_doc_value_without_doc_prints_error(monkeypatch: Any) -> None:
    output: list[str] = []
    monkeypatch.setattr("pynix._impl.repl.print_formatted_text", output.append)
    repl = _Repl()
    repl.value = _DocValue(None)

    await _run_repl_loop(repl, _Prompt([":doc 42", ":quit"]))
    assert output == [
        _HELP,
        "error: value does not have documentation",
    ]


def test_repl_help_includes_doc_command() -> None:
    assert ":d, :doc <expr>" in _HELP
