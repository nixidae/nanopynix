"""``pynix fmt`` (nixidae/nixidae#118), as ``CmdFormatterRun`` runs a formatter.

``fmt`` ends in ``exec``, so each test runs ``pynix`` in a subprocess.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import huggorm
from huggorm.testing.environment import NixTestEnvironment

from test_support.git_fixtures import commit_files, init_repo

#: A formatter that records its arguments and ``PRJ_ROOT``, and fails with 7.
_FORMATTER = """pkgs.writeShellScriptBin "fmt" ''
  echo "$PRJ_ROOT" "$@" > "$PRJ_ROOT/marker"
  exit 7
''"""


def _flake(path: Path, nixpkgs_path: str, outputs: str) -> Path:
    (path / "sub").mkdir(parents=True)
    system = huggorm.current_system()
    (path / "flake.nix").write_text(
        "{\n"
        f'  inputs.nixpkgs = {{ url = "path:{nixpkgs_path}"; flake = false; }};\n'
        "  outputs = { nixpkgs, ... }:\n"
        f'    let pkgs = import nixpkgs {{ system = "{system}"; config = {{ }}; overlays = [ ]; }};\n'
        f"    in {{ {outputs.replace('SYSTEM', system)} }};\n"
        "}\n",
        encoding="utf-8",
    )
    commit_files(init_repo(path), path / "flake.nix")
    return path


def _fmt(store_uri: str, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        "-c",
        "import sys; from pynix import main; sys.argv = ['pynix', *sys.argv[1:]]; main()",
        "fmt",
        "--store",
        store_uri,
        *args,
    ]
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, every path from the test itself
        argv, capture_output=True, text=True, check=False, cwd=cwd, env=os.environ.copy()
    )


def test_fmt_runs_the_formatter_with_the_arguments_and_the_root(
    shared_nix_environment: NixTestEnvironment, nixpkgs_path: str, tmp_path: Path
) -> None:
    flake = _flake(tmp_path / "flake", nixpkgs_path, f"formatter.SYSTEM = {_FORMATTER};")
    result = _fmt(shared_nix_environment.store_uri, flake / "sub", "--", "a.nix", "--check")
    assert result.returncode == 7, result.stderr
    assert (flake / "marker").read_text(encoding="utf-8") == f"{flake} a.nix --check\n"


def test_fmt_names_the_attribute_a_flake_lacks(
    shared_nix_environment: NixTestEnvironment, nixpkgs_path: str, tmp_path: Path
) -> None:
    flake = _flake(tmp_path / "flake", nixpkgs_path, "")
    result = _fmt(shared_nix_environment.store_uri, flake)
    assert result.returncode == 1
    assert f"formatter.{huggorm.current_system()}" in result.stderr
    assert not (flake / "marker").exists()
