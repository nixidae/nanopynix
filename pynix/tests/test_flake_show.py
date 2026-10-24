from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from pynix import parse

if TYPE_CHECKING:
    from pathlib import Path

    from huggorm.testing.environment import NixTestEnvironment


async def test_flake_show_root(
    shared_nix_environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str], git_flake: Path
) -> None:
    cmd = parse(["flake", "show", str(git_flake), *shared_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    assert "hello" in captured.out
    assert "greeting" in captured.out


async def test_flake_show_with_hash_attrpath(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    git_flake: Path,
) -> None:
    cmd = parse(["flake", "show", f"{git_flake}#hello", *shared_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    assert "hello" in captured.out
    assert "greeting" not in captured.out


@pytest.mark.parametrize("args", [["#nope"], ["", "--attrpath", "hello.nope"]], ids=["fragment", "attrpath"])
async def test_flake_show_reports_a_missing_attribute_as_one_error(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    git_flake: Path,
    args: list[str],
) -> None:
    fragment, *rest = args
    cmd = parse(["flake", "show", f"{git_flake}{fragment}", *rest, *shared_nix_environment.pynix_store_args()])
    with pytest.raises(SystemExit) as raised:
        await cmd.run()
    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "attribute 'nope' not found" in captured.err
    assert "Traceback" not in captured.err


async def test_flake_show_with_a_separate_attrpath_flag_navigates_further(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    git_flake: Path,
) -> None:
    """--attrpath narrows further than the flake_ref's own '#' fragment,
    a distinct code path from resolving the fragment alone (see the other
    test above)."""
    cmd = parse(
        ["flake", "show", f"{git_flake}#hello", "--attrpath", "pname", *shared_nix_environment.pynix_store_args()]
    )
    await cmd.run()
    captured = capsys.readouterr()
    assert "greeting" not in captured.out
    assert "drvPath" not in captured.out


async def test_flake_metadata_json_does_not_write_lock_file(
    shared_nix_environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str], git_flake: Path
) -> None:
    lock_file = git_flake / "flake.lock"
    assert not lock_file.exists()

    cmd = parse(["flake", "metadata", str(git_flake), *shared_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    # `resolvedUrl` and `locks` are Nix's own key names. This object used to be
    # a bespoke one keyed `resolvedRef`/`inputs`; see test_flake_metadata.py for
    # the oracle that now holds it to `nix flake metadata --json`.
    assert str(git_flake) in data["resolvedUrl"]
    # No `description` key: this fixture's flake declares none, and Nix omits
    # the key rather than reporting an empty string. The bespoke object this
    # replaced always emitted one.
    assert "description" not in data
    assert isinstance(data["locks"], dict)
    assert not lock_file.exists()


async def test_flake_info_aliases_metadata(
    shared_nix_environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str], git_flake: Path
) -> None:
    cmd = parse(["flake", "info", str(git_flake), *shared_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert str(git_flake) in data["resolvedUrl"]
