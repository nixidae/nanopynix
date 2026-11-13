"""Nix's per-setting flags, hidden (``configuration.cc:224``). nixidae/nixidae#1320."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import huggorm
import pytest

from pynix import parse
from pynix._settings import option_pairs
from pynix.build import Build

_MAIN = "import sys; from pynix import main; sys.argv = ['pynix', *sys.argv[1:]]; main()"


def _pairs(*args: str) -> list[tuple[str, str]]:
    return option_pairs(parse(["config", "show", *args]))


@pytest.mark.parametrize(
    ("args", "pairs"),
    [
        (["--max-jobs", "7"], [("max-jobs", "7")]),
        (["--no-sandbox"], [("sandbox", "false")]),
        (["--sandbox"], [("sandbox", "true")]),
        (["--relaxed-sandbox"], [("sandbox", "relaxed")]),
        (["--build-use-sandbox"], [("sandbox", "true")]),
        (["--extra-substituters", "https://a.example"], [("extra-substituters", "https://a.example")]),
    ],
)
def test_a_setting_flag_is_a_pair(args: list[str], pairs: list[tuple[str, str]]) -> None:
    assert _pairs(*args) == pairs


def test_setting_flags_and_option_keep_the_order_of_the_line() -> None:
    assert _pairs("--option", "cores", "2", "--max-jobs", "3", "--no-sandbox") == [
        ("cores", "2"),
        ("max-jobs", "3"),
        ("sandbox", "false"),
    ]


def test_a_flag_of_the_command_stays_the_commands() -> None:
    command = parse(["build", "--file", "x.nix", "--keep-going", "--max-jobs", "2"])
    assert isinstance(command, Build)
    assert command.keep_going is True
    assert option_pairs(command) == [("max-jobs", "2")]


def test_a_line_without_one_never_reads_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> str:
        raise AssertionError("read the settings")

    monkeypatch.setattr(huggorm, "settings_json", refuse)
    assert _pairs("--setting", "cores") == []


def test_an_unknown_flag_is_still_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        parse(["config", "show", "--no-such-flag"])
    assert exited.value.code == 2
    assert "unrecognized arguments: --no-such-flag" in capsys.readouterr().err


def test_setting_flags_are_hidden_from_the_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        parse(["config", "show", "--max-jobs", "2", "--help"])
    assert "max-jobs" not in capsys.readouterr().out


def test_main_applies_a_setting_flag(child_env: dict[str, str]) -> None:
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [sys.executable, "-c", _MAIN, "config", "show", "--setting", "max-jobs", "--max-jobs", "37"],
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert (result.returncode, result.stderr) == (0, "")
    assert json.loads(result.stdout) == {"max-jobs": "37"}


@pytest.mark.parametrize(("flags", "code"), [([], 1), (["--extra-experimental-features", "blake3-hashes"], 0)])
def test_a_command_with_no_session_gets_the_setting_flags(
    flags: list[str], code: int, child_env: dict[str, str], tmp_path: Path
) -> None:
    """``hash`` opens no store or evaluator, and ``blake3`` needs ``blake3-hashes``."""
    data = tmp_path / "data"
    data.write_text("hello\n")
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [sys.executable, "-c", _MAIN, "hash", "path", "--algo", "blake3", str(data), *flags],
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == code, result.stderr
    if code:
        assert "blake3-hashes" in result.stderr
