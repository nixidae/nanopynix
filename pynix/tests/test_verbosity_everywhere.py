"""``--verbosity`` on every command that runs, as ``nix`` takes ``--verbose`` on every command. nixidae/nixidae#1324."""

from __future__ import annotations

import subprocess
import sys

import pytest

from libpynix import Command, command_name
from pynix import Pynix, parse
from pynix._settings import ConfiguredCommand

_MAIN = "import sys; from pynix import main; sys.argv = ['pynix', *sys.argv[1:]]; main()"
_WARN = 'builtins.warn "hello" 1'


def _leaves(command: type[Command], path: str = "") -> list[tuple[str, type[Command]]]:
    if not command.subcommands:
        return [(path, command)]
    return [leaf for sub in command.subcommands for leaf in _leaves(sub, f"{path} {command_name(sub)}".strip())]


def _eval(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [sys.executable, "-c", _MAIN, "eval", "--expr", _WARN, *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )


def test_every_command_that_runs_takes_verbosity() -> None:
    leaves = _leaves(Pynix)
    assert len(leaves) > 60, "the walk found too few commands"
    lacking = [
        path for path, leaf in leaves if not issubclass(leaf, ConfiguredCommand) or "verbosity" not in leaf.specs
    ]
    assert lacking == []


def test_eval_shows_a_warning_by_default(child_env: dict[str, str]) -> None:
    assert "hello" in _eval(env=child_env).stderr


def test_eval_at_error_hides_the_warning(child_env: dict[str, str]) -> None:
    result = _eval("--verbosity", "error", env=child_env)
    assert (result.stdout, result.stderr) == ("1\n", "")


def test_eval_at_talkative_shows_more_of_nix(child_env: dict[str, str]) -> None:
    assert "reading expression from argument" in _eval("--verbosity", "talkative", env=child_env).stderr


@pytest.mark.parametrize(
    ("flags", "level"),
    [
        (["-v"], "4"),
        (["-v", "-v", "--verbose"], "6"),
        (["-vvvvvvv"], "7"),
        (["--quiet", "--quiet", "--quiet", "--quiet"], "0"),
        (["--debug", "--quiet"], "5"),
        (["--quiet", "--debug"], "6"),
        (["--verbosity", "error", "-v"], "1"),
        (["-v", "--verbosity", "error"], "1"),
    ],
)
def test_nix_verbosity_flags_step_in_order(flags: list[str], level: str) -> None:
    """``common-args.cc:17``: from info, `-v` up to vomit, `--quiet` down to error, `--debug` sets debug."""
    command = parse(["eval", "--expr", "1", *flags])
    assert isinstance(command, ConfiguredCommand)
    assert command.verbosity == level


def test_no_flag_leaves_nix_its_default() -> None:
    command = parse(["eval", "--expr", "1"])
    assert isinstance(command, ConfiguredCommand)
    assert command.verbosity is None


def test_nix_verbosity_flags_are_hidden_from_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        parse(["eval", "--help"])
    shown = capsys.readouterr().out
    assert "--verbosity" in shown
    assert not {"--verbose", "--quiet", "--debug", "-v,"} & set(shown.split())
