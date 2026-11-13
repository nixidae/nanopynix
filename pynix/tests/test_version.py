"""``pynix --version``, in the shape of ``nix --version`` (``shared.cc:341``)."""

from __future__ import annotations

import importlib.metadata
import json
import re

import huggorm
import pytest

from pynix import parse


def _version(capsys: pytest.CaptureFixture[str], *args: str) -> str:
    with pytest.raises(SystemExit) as exited:
        parse(["--version", *args])
    assert exited.value.code == 0
    return capsys.readouterr().out


def test_version_is_the_line_of_nix_with_the_version_of_pynix(capsys: pytest.CaptureFixture[str]) -> None:
    line = _version(capsys)
    assert line == f"pynix {importlib.metadata.version('pynix')} (Nix) {huggorm.nix_version()}\n"
    # What a script that reads `nix --version` looks for.
    assert re.search(r"\(Nix\) (\S+)", line)[1] == huggorm.nix_version()  # type: ignore[index]  # the line above holds it


def test_version_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert json.loads(_version(capsys, "--json")) == {
        "pynix": importlib.metadata.version("pynix"),
        "nix": huggorm.nix_version(),
    }


def test_version_wins_over_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert _version(capsys, "build", "--file", "x.nix").startswith("pynix ")


def test_json_alone_before_the_command_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        parse(["--json", "build", "--file", "x.nix"])
    assert exited.value.code == 2
    assert "--json before the command needs --version" in capsys.readouterr().err
