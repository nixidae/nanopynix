"""The three options that name what to evaluate.

One declaration for both programs, which is the half of issue #222 that the
issue calls the part that matters most: the help of ``--attr`` was the same
string in ``pynix`` and in ``easykubenix``, character for character.
"""

from __future__ import annotations

import pytest

from libpynix import (
    Command,
    arg_option,
    argjson_option,
    args_json_option,
    argstr_option,
    attr_option,
    attrs_option,
    build_parser,
    dispatch,
    file_option,
    flake_option,
    impure_option,
)


class Evaluating(Command):
    """A command that names what to evaluate."""

    file: str | None = file_option()
    attr: str | None = attr_option()
    flake: str | None = flake_option()
    impure: bool = impure_option()


class Calling(Command):
    """A command that passes arguments to a file."""

    file: str | None = file_option()
    arg: list[tuple[str, str]] = arg_option()
    argstr: list[tuple[str, str]] = argstr_option()
    argjson: list[tuple[str, str]] = argjson_option()
    args_json: str | None = args_json_option()


def parse(*arguments: str) -> Evaluating:
    parser = build_parser(Evaluating)
    command = dispatch(parser, parser.parse_args(arguments))
    assert isinstance(command, Evaluating)
    return command


def test_the_three_options_take_their_long_spelling() -> None:
    command = parse("--file", "./x.nix", "--attr", "a.b", "--flake", "nixpkgs#hello")
    assert command.file == "./x.nix"
    assert command.attr == "a.b"
    assert command.flake == "nixpkgs#hello"


def test_impure_is_off_unless_named() -> None:
    assert parse("--flake", "f").impure is False
    assert parse("--flake", "f", "--impure").impure is True


def test_file_and_attr_have_the_short_spelling_that_nix_uses() -> None:
    command = parse("-f", "./x.nix", "-A", "a.b")
    assert command.file == "./x.nix"
    assert command.attr == "a.b"


def test_a_flake_reference_is_a_string_and_keeps_every_separator() -> None:
    """``PurePath`` collapses a repeated separator, and a URL is not a path.

    ``https://example.com/x.tar.gz`` reached the evaluator as
    ``https:/example.com/x.tar.gz`` and failed.
    """
    assert parse("--file", "https://example.com/x.tar.gz").file == "https://example.com/x.tar.gz"


def test_attrs_option_collects_each_attr_in_order() -> None:
    class Building(Command):
        """A command that takes several targets."""

        attr: list[str] = attrs_option()

    parser = build_parser(Building)
    command = dispatch(parser, parser.parse_args(["--attr", "a", "-A", "b"]))
    assert isinstance(command, Building)
    assert command.attr == ["a", "b"]


def test_arg_and_argstr_collect_each_use_in_order() -> None:
    parser = build_parser(Calling)
    command = dispatch(parser, parser.parse_args(["--arg", "a", "1", "--argstr", "b", "x", "--arg", "a", "2"]))
    assert isinstance(command, Calling)
    assert command.arg == [("a", "1"), ("a", "2")]
    assert command.argstr == [("b", "x")]


def test_argjson_collects_each_use_and_args_json_takes_one() -> None:
    parser = build_parser(Calling)
    arguments = ["--argjson", "a", "[1]", "--argjson", "b", "{}", "--args-json", "@x.json"]
    command = dispatch(parser, parser.parse_args(arguments))
    assert isinstance(command, Calling)
    assert command.argjson == [("a", "[1]"), ("b", "{}")]
    assert command.args_json == "@x.json"


def test_a_second_args_json_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser(Calling)
    with pytest.raises(SystemExit):
        parser.parse_args(["--args-json", "{}", "--args-json", "{}"])
    assert "--args-json may be given once" in capsys.readouterr().err


def test_none_of_the_three_is_required() -> None:
    command = parse()
    assert (command.file, command.attr, command.flake) == (None, None, None)


@pytest.mark.parametrize("flag", ["--file", "-f", "--attr", "-A", "--flake"])
def test_a_second_use_is_refused(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    """argparse kept the last of two and said nothing, so `--attr a --attr b`
    built `b` alone with exit 0 (nixidae/nixidae#1050)."""
    with pytest.raises(SystemExit) as raised:
        parse(flag, "a", flag, "b")
    assert raised.value.code == 2
    assert f"{flag} may be given once" in capsys.readouterr().err


def test_the_long_and_the_short_spelling_are_one_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        parse("--attr", "a", "-A", "b")
    assert "may be given once" in capsys.readouterr().err


def test_each_option_carries_a_help_line() -> None:
    """The guard: a declaration with no help is a Tab that says nothing."""
    for field in ("file", "attr", "flake"):
        assert Evaluating.specs[field].help.endswith(".")
