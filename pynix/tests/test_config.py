from __future__ import annotations

import json
import os
import sys

import pytest
from anyio import run_process

from pynix import parse


async def test_config_show(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = parse(["config", "show"])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data, dict)
    assert "store" in data


async def test_config_show_one_setting(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = parse(["config", "show", "--setting", "store"])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert set(data) == {"store"}
    assert isinstance(data["store"], str)


async def test_config_show_reports_the_nix_path_from_the_environment() -> None:
    """`nix-path` holds what `NIX_PATH` says, as `nix config show` prints it.

    Nix copies `NIX_PATH` into the setting when libexpr starts, once per
    process, so a child process with its own `NIX_PATH` asks.
    """
    script = (
        "import sys; from pynix import main; sys.argv = ['pynix', 'config', 'show', '--setting', 'nix-path']; main()"
    )
    result = await run_process(
        [sys.executable, "-c", script],
        env={**os.environ, "NIX_PATH": "alpha=/a:beta=/b"},
    )

    assert json.loads(result.stdout) == {"nix-path": "alpha=/a beta=/b"}


async def test_config_check(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = parse(["config", "check"])
    await cmd.run()
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ok": True}


async def test_config_current_system(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = parse(["config", "current-system"])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert set(data) == {"currentSystem"}
    assert isinstance(data["currentSystem"], str)
    assert data["currentSystem"]
