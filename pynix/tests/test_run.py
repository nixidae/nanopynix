"""``pynix run``.

The rules of ``InstallableValue::toApp`` are unit tests. The end-to-end tests
run ``pynix`` in a subprocess, because ``run`` ends in ``exec`` and would
replace the pytest interpreter.
"""

from __future__ import annotations

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
# nanopynix.current_system comes from a C++ nanobind extension without type stubs.
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

import nanopynix
from pynix._impl.run import choose_bin, expected_type, main_program
from pynix.target import EvaluationTargetError
from test_support.git_fixtures import commit_files, init_repo

if TYPE_CHECKING:
    from pathlib import Path

    from nanopynix_testing.nix_environment import NixTestEnvironment


@pytest.mark.parametrize(
    ("selected", "expected"),
    [
        ("apps.x86_64-linux.default", "app"),
        ("defaultApp.x86_64-linux", "app"),
        ("packages.x86_64-linux.hello", "derivation"),
        ("hello", "derivation"),
        ("", "derivation"),
    ],
)
def test_an_app_is_expected_under_apps_alone(selected: str, expected: str) -> None:
    assert expected_type(selected) == expected


@pytest.mark.parametrize(
    ("main", "pname", "name", "expected"),
    [
        ("hi", "hello", "hello-2.12", "hi"),
        (None, "hello", "hello-2.12", "hello"),
        (None, None, "hello-2.12", "hello"),
        (None, None, "git-lfs-3.4.0", "git-lfs"),
        (None, None, "plain", "plain"),
    ],
)
def test_the_program_name_follows_nix(main: str | None, pname: str | None, name: str, expected: str) -> None:
    assert main_program(main=main, pname=pname, name=name) == expected


def test_bin_selects_from_the_directory_of_the_program() -> None:
    assert choose_bin("/nix/store/x-p/bin/one", "two", ["one", "two"]) == "/nix/store/x-p/bin/two"
    assert choose_bin("/nix/store/x-p/bin/one", None, []) == "/nix/store/x-p/bin/one"


def test_an_unknown_bin_lists_the_names_that_exist() -> None:
    with pytest.raises(EvaluationTargetError, match="it holds: one, two"):
        choose_bin("/nix/store/x-p/bin/one", "three", ["two", "one"])


# --- end to end, in a subprocess --------------------------------------------

#: A package with two programs. ``meta.mainProgram`` names the second, so a
#: run that reached ``pname`` instead would print the wrong marker.
_TOOLS = """
  tools = pkgs.runCommand "tools-1.0" { meta.mainProgram = "second"; } ''
    mkdir -p $out/bin
    printf '#!${pkgs.runtimeShell}\\necho first "$@"\\n' > $out/bin/first
    printf '#!${pkgs.runtimeShell}\\necho second "$@"\\n' > $out/bin/second
    printf '#!${pkgs.runtimeShell}\\nexit 42\\n' > $out/bin/fails
    chmod +x $out/bin/*
  '';
"""


def _flake(path: Path, nixpkgs_path: str, outputs: str) -> Path:
    path.mkdir()
    system = nanopynix.current_system()
    (path / "flake.nix").write_text(
        "{\n"
        f'  inputs.nixpkgs = {{ url = "path:{nixpkgs_path}"; flake = false; }};\n'
        "  outputs = { nixpkgs, ... }:\n"
        "    let\n"
        f'      pkgs = import nixpkgs {{ system = "{system}"; config = {{ }}; overlays = [ ]; }};\n'
        f"{_TOOLS}"
        f"    in {{ {outputs.replace('SYSTEM', system)} }};\n"
        "}\n",
        encoding="utf-8",
    )
    commit_files(init_repo(path), path / "flake.nix")
    return path


def _run(store_uri: str, flake: str, *args: str) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        "-c",
        "import sys; from pynix import main; sys.argv = ['pynix', *sys.argv[1:]]; main()",
        "run",
        "--flake",
        flake,
        "--store",
        store_uri,
        *args,
    ]
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, every path from the test itself
        argv, capture_output=True, text=True, check=False, env=os.environ.copy()
    )


def test_run_prefers_the_default_app(
    shared_nix_environment: NixTestEnvironment, nixpkgs_path: str, tmp_path: Path
) -> None:
    flake = _flake(
        tmp_path / "app",
        nixpkgs_path,
        'apps.SYSTEM.default = { type = "app"; program = "${tools}/bin/first"; }; packages.SYSTEM.default = tools;',
    )
    result = _run(shared_nix_environment.store_uri, f"{flake}#", "--", "a", "b")
    assert (result.returncode, result.stdout) == (0, "first a b\n"), result.stderr


def test_run_uses_main_program_without_apps(
    shared_nix_environment: NixTestEnvironment, nixpkgs_path: str, tmp_path: Path
) -> None:
    flake = _flake(tmp_path / "package", nixpkgs_path, "packages.SYSTEM.default = tools;")
    result = _run(shared_nix_environment.store_uri, f"{flake}#")
    assert (result.returncode, result.stdout) == (0, "second\n"), result.stderr


def test_run_returns_the_exit_status_of_the_program(
    shared_nix_environment: NixTestEnvironment, nixpkgs_path: str, tmp_path: Path
) -> None:
    flake = _flake(tmp_path / "package", nixpkgs_path, "packages.SYSTEM.default = tools;")
    result = _run(shared_nix_environment.store_uri, f"{flake}#", "--bin", "fails")
    assert result.returncode == 42, result.stderr


def test_run_bin_selects_another_program(
    shared_nix_environment: NixTestEnvironment, nixpkgs_path: str, tmp_path: Path
) -> None:
    flake = _flake(tmp_path / "package", nixpkgs_path, "packages.SYSTEM.default = tools;")
    store_uri = shared_nix_environment.store_uri
    chosen = _run(store_uri, f"{flake}#", "--bin", "first")
    unknown = _run(store_uri, f"{flake}#", "--bin", "third")
    assert (chosen.returncode, chosen.stdout) == (0, "first\n"), chosen.stderr
    assert unknown.returncode == 1
    assert "it holds: fails, first, second" in " ".join(unknown.stderr.split())


def test_an_app_must_be_an_app(shared_nix_environment: NixTestEnvironment, nixpkgs_path: str, tmp_path: Path) -> None:
    """``toApp`` refuses a derivation under ``apps``, and so does ``pynix run``."""
    flake = _flake(tmp_path / "wrong", nixpkgs_path, "apps.SYSTEM.default = tools;")
    result = _run(shared_nix_environment.store_uri, f"{flake}#")
    assert result.returncode == 1
    assert f"attribute 'apps.{nanopynix.current_system()}.default' should have type 'app'" in result.stderr
