from __future__ import annotations

import json
from pathlib import Path

import pytest
from huggorm.testing.environment import NixTestEnvironment

from pynix import parse


async def test_pynix_store_add_file_imports_and_can_be_read(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    source = tmp_path / "message.txt"
    source.write_text("pynix-add-file\n")

    cmd = parse(["store", "add-file", str(source), *shared_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)

    assert data["path"].startswith("/nix/store/")
    assert shared_nix_environment.physical_path(data["path"]).read_text() == "pynix-add-file\n"


async def test_pynix_store_add_path_imports_directory(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source-dir"
    (source / "share").mkdir(parents=True)
    (source / "share" / "message").write_text("pynix-add-path\n")

    cmd = parse(
        ["store", "add-path", str(source), "--name", "custom-dir", *shared_nix_environment.pynix_store_args()],
    )
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)

    assert data["path"].endswith("-custom-dir")
    assert (shared_nix_environment.physical_path(data["path"]) / "share" / "message").read_text() == "pynix-add-path\n"


async def test_pynix_store_add_dry_run_does_not_import(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "message.txt"
    source.write_text("dry-run\n")

    cmd = parse(
        [
            "store",
            "add",
            str(source),
            "--mode",
            "flat",
            "--dry-run",
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)

    assert data["path"].startswith("/nix/store/")
    assert not shared_nix_environment.physical_path(data["path"]).exists()


@pytest.mark.parametrize("flag", ["--mode", "--hash-algo"])
def test_pynix_store_add_refuses_an_unknown_word(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse(["store", "add", "message.txt", flag, "bogus"])

    assert exc_info.value.code == 2
    assert "invalid choice: 'bogus'" in capsys.readouterr().err
