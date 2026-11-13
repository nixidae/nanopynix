"""``--option NAME VALUE``, as ``nix`` takes it (``common-args.cc:42``). nixidae/nixidae#1320."""

from __future__ import annotations

import json
import subprocess
import sys

import huggorm
import pytest
from structlog.testing import capture_logs

from pynix import parse
from pynix._impl.settings import command_options_of
from pynix._settings import option_pairs
from pynix._util import applied_nix_settings, init_nix
from pynix.store import Gc, Store

_MAIN = "import sys; from pynix import main; sys.argv = ['pynix', *sys.argv[1:]]; main()"


def _pynix(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [sys.executable, "-c", _MAIN, *args], env=env, capture_output=True, text=True, check=False, timeout=120
    )


async def _shown(capsys: pytest.CaptureFixture[str], *args: str) -> object:
    command = parse(["config", "show", *args])
    with command_options_of(option_pairs(command)):
        await command.run()
    return json.loads(capsys.readouterr().out)


def test_every_command_takes_option_pairs_in_order() -> None:
    command = parse(["build", "--file", "x.nix", "--option", "max-jobs", "3", "--option", "cores", "2"])
    assert option_pairs(command) == [("max-jobs", "3"), ("cores", "2")]


@pytest.mark.parametrize("group", [Store, Gc])
def test_a_group_takes_no_option(group: type[Store | Gc]) -> None:
    assert "option" not in group.specs


def test_main_applies_the_pairs(child_env: dict[str, str]) -> None:
    result = _pynix("config", "show", "--setting", "max-jobs", "--option", "max-jobs", "37", env=child_env)
    assert (result.returncode, result.stderr) == (0, "")
    assert json.loads(result.stdout) == {"max-jobs": "37"}


def test_main_warns_about_an_unknown_name_and_goes_on(child_env: dict[str, str]) -> None:
    result = _pynix("config", "show", "--setting", "cores", "--option", "no-such-setting", "1", env=child_env)
    assert (result.returncode, result.stderr) == (0, "warning: unknown setting 'no-such-setting'\n")


async def test_the_command_puts_the_setting_back(capsys: pytest.CaptureFixture[str]) -> None:
    init_nix()
    before = huggorm.get_setting("max-jobs")
    assert before != "37"
    assert await _shown(capsys, "--setting", "max-jobs", "--option", "max-jobs", "37") == {"max-jobs": "37"}
    assert huggorm.get_setting("max-jobs") == before


async def test_option_wins_over_the_pynix_layers(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYNIX_NIX_MAX_JOBS", "5")
    assert await _shown(capsys, "--setting", "max-jobs", "--option", "max-jobs", "6") == {"max-jobs": "6"}


def test_a_bad_value_is_a_warning_as_in_nix() -> None:
    init_nix()
    before = huggorm.get_setting("use-cgroups")
    with command_options_of([("use-cgroups", "maybe")]), capture_logs() as logs, applied_nix_settings():
        assert huggorm.get_setting("use-cgroups") == before
    assert [(entry["log_level"], entry["event"]) for entry in logs] == [
        ("warning", "Boolean setting 'use-cgroups' has invalid value 'maybe'")
    ]


def test_an_extra_setting_is_put_back() -> None:
    init_nix()
    before = huggorm.get_setting("substituters")
    with command_options_of([("extra-substituters", "https://extra.example")]), applied_nix_settings():
        assert "https://extra.example" in str(huggorm.get_setting("substituters"))
    assert huggorm.get_setting("substituters") == before
