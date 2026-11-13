from __future__ import annotations

import json
from pathlib import Path

import anyio
import huggorm
import pytest
from huggorm.testing.environment import NixTestEnvironment, with_nixpkgs

from pynix import parse
from support.ansi import strip_ansi


async def test_show_file(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text(
        with_nixpkgs(
            """
    let
      pkgs = import <nixpkgs> {};
    in
    pkgs.stdenvNoCC.mkDerivation {
      pname = "test-drv";
      version = "1";
      dontUnpack = true;
      installPhase = ''
        echo hi > "$out"
      '';
    }
    """,
            nixpkgs_path,
        )
    )
    cmd = parse(
        ["derivation", "show", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()],
    )
    await cmd.run()
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    # The document `nix derivation show` prints: one key per derivation, by
    # base name, and the format version beside them.
    assert document["version"] == 4
    result = document["derivations"]

    drv_path = next(iter(result))
    assert drv_path.endswith("-test-drv-1.drv")
    assert "/" not in drv_path
    drv = result[drv_path]
    assert drv["name"] == "test-drv-1"
    # `stdenvNoCC` builds for the host, so the system of this derivation is the
    # system of the machine. Naming one host made this fail on macOS with
    # `aarch64-darwin`, and the subject is that `derivation show` reports the
    # field, not what the field holds.
    assert drv["system"] == huggorm.current_system()
    assert "out" in drv["outputs"]


async def test_show_file_with_attr(
    shared_nix_environment: NixTestEnvironment,
    nixpkgs_path: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text(
        with_nixpkgs(
            """
    let
      pkgs = import <nixpkgs> {};
    in
    {
      hello = pkgs.stdenvNoCC.mkDerivation {
        pname = "nested-hello";
        version = "1";
        dontUnpack = true;
        installPhase = ''
          echo hi > "$out"
        '';
      };
    }
    """,
            nixpkgs_path,
        )
    )
    cmd = parse(
        ["derivation", "show", "--file", str(nix_file), "--attr", "hello", *shared_nix_environment.pynix_store_args()],
    )
    await cmd.run()
    captured = capsys.readouterr()
    result = json.loads(captured.out)["derivations"]
    drv_path = next(iter(result))
    assert result[drv_path]["name"] == "nested-hello-1"


async def test_show_flake(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    git_flake: Path,
) -> None:
    cmd = parse(["derivation", "show", "--flake", f"{git_flake}#hello", *shared_nix_environment.pynix_store_args()])
    await cmd.run()
    captured = capsys.readouterr()
    result = json.loads(captured.out)["derivations"]
    drv_path = next(iter(result))
    drv = result[drv_path]
    assert drv["name"] == "test-hello-1"
    assert "out" in drv["outputs"]


async def test_show_flake_greeting_is_not_derivation(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    git_flake: Path,
) -> None:
    cmd = parse(
        ["derivation", "show", "--flake", f"{git_flake}#greeting", *shared_nix_environment.pynix_store_args()],
    )
    with pytest.raises(SystemExit):
        await cmd.run()
    captured = capsys.readouterr()
    # `getDerivations`' message. Nix tries the string as a store path instead, and says it
    # has no context.
    assert "expression does not evaluate to a derivation (or a set or list of those)" in " ".join(captured.err.split())


async def test_show_missing_both_errors(capsys: pytest.CaptureFixture[str]) -> None:
    cmd = parse(["derivation", "show"])
    with pytest.raises(SystemExit):
        await cmd.run()
    captured = capsys.readouterr()
    assert "either --file or --flake is required" in captured.err


async def test_show_both_file_and_flake_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{}")
    cmd = parse(["derivation", "show", "--file", str(nix_file), "--flake", ".#hello"])
    with pytest.raises(SystemExit):
        await cmd.run()
    captured = capsys.readouterr()
    assert "--file and --flake are mutually exclusive" in captured.err


async def test_show_file_missing_attr_errors(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text("{ present = 1; }")
    cmd = parse(
        [
            "derivation",
            "show",
            "--file",
            str(nix_file),
            "--attr",
            "missing",
            *shared_nix_environment.pynix_store_args(),
        ],
    )
    with pytest.raises(SystemExit):
        await cmd.run()
    captured = capsys.readouterr()
    assert "attribute 'missing' not found" in strip_ansi(captured.err)


@pytest.mark.parametrize("expression", ['{ type = "not-a-derivation"; }', "{ x = 1; }"])
async def test_a_set_with_no_derivation_shows_an_empty_document(
    expression: str,
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``nix derivation show`` prints ``{"derivations":{},"version":4}`` for both."""
    nix_file = tmp_path / "test.nix"
    await anyio.Path(nix_file).write_text(expression)

    await parse(["derivation", "show", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()]).run()

    assert json.loads(capsys.readouterr().out) == {"derivations": {}, "version": 4}


async def test_a_set_shows_each_derivation_it_holds(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``getDerivations``: each member that is a derivation, and a member set only with ``recurseForDerivations``."""
    nix_file = tmp_path / "test.nix"
    await anyio.Path(nix_file).write_text(
        'let d = name: derivation { inherit name; system = builtins.currentSystem; builder = "/bin/sh"; };\n'
        'in { a = d "show-a"; b = d "show-b"; hidden = { c = d "show-c"; };'
        ' shown = { recurseForDerivations = true; e = d "show-e"; }; }\n'
    )

    await parse(["derivation", "show", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()]).run()

    shown = json.loads(capsys.readouterr().out)["derivations"]
    assert sorted(drv["name"] for drv in shown.values()) == ["show-a", "show-b", "show-e"]


async def test_show_file_non_string_drv_path_errors(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    nix_file = tmp_path / "test.nix"
    nix_file.write_text('{ type = "derivation"; drvPath = 123; }')
    cmd = parse(["derivation", "show", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()])
    with pytest.raises(SystemExit):
        await cmd.run()
    captured = capsys.readouterr()
    assert "failed to get derivation path" in captured.err
