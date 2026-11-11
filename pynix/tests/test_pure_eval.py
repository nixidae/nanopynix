"""A flake evaluates in pure mode, as ``nix`` evaluates it (nixidae/nixidae#1109).

``nix`` sets ``pure-eval`` for every command (``main.cc``), ``--impure`` turns it
off, and ``--file`` and an expression evaluate impurely always
(``installables.cc``). ``builtins ? currentSystem`` tells the two modes apart.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from huggorm.testing.environment import NixTestEnvironment

from pynix import parse

_PROBE = "builtins ? currentSystem"


@pytest.fixture
def flake(tmp_path: Path) -> Path:
    directory = tmp_path / "pure-flake"
    directory.mkdir()
    (directory / "flake.nix").write_text(f"{{ outputs = _: {{ impure = {_PROBE}; }}; }}\n", encoding="utf-8")
    return directory


async def _eval(environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    await parse(["eval", *argv, *environment.pynix_store_args()]).run()
    return capsys.readouterr().out.strip()


@pytest.mark.parametrize(("flags", "expected"), [([], "false"), (["--impure"], "true")], ids=["pure", "impure"])
async def test_a_flake_is_pure_unless_impure(
    shared_nix_environment: NixTestEnvironment,
    flake: Path,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    expected: str,
) -> None:
    assert await _eval(shared_nix_environment, capsys, *flags, "--flake", f"path:{flake}#impure") == expected


async def test_a_file_is_impure(
    shared_nix_environment: NixTestEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    probe = tmp_path / "probe.nix"
    probe.write_text(f"{_PROBE}\n", encoding="utf-8")
    assert await _eval(shared_nix_environment, capsys, "--file", str(probe)) == "true"


async def test_an_expression_is_impure(
    shared_nix_environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await _eval(shared_nix_environment, capsys, "--expr", _PROBE) == "true"
