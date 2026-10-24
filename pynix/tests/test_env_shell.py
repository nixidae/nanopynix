"""``pynix env shell``.

The output and ``PATH`` rules of ``CmdShell::run`` are unit tests. The
end-to-end tests run ``pynix`` in a subprocess, because the command ends in
``exec``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from huggorm.testing.environment import NixTestEnvironment, with_nixpkgs

from pynix._impl.env import bin_directories, default_outputs


@pytest.mark.parametrize(
    ("specified", "name", "to_install", "expected"),
    [
        (True, "dev", ["bin", "man"], ["dev"]),
        (False, "out", ["man", "bin"], ["bin", "man"]),
        (False, "out", [], ["out"]),
        (True, None, [], ["out"]),
    ],
)
def test_default_outputs_follow_nix(
    specified: bool, name: str | None, to_install: list[str], expected: list[str]
) -> None:
    assert default_outputs(output_specified=specified, output_name=name, outputs_to_install=to_install) == expected


async def test_propagated_packages_follow_their_roots_once() -> None:
    graph = {"/s/a": ["/s/c"], "/s/b": ["/s/c", "/s/a"], "/s/c": []}

    async def read(path: str) -> list[str]:
        return graph[path]

    assert await bin_directories(["/s/a", "/s/b"], read) == ["/s/a/bin", "/s/b/bin", "/s/c/bin"]


# --- end to end, in a subprocess --------------------------------------------

#: `tool` propagates `helper`, so `helper` is on PATH without being named.
_PACKAGES = """
with import <nixpkgs> { };
rec {
  helper = writeShellScriptBin "pynix-test-helper" "echo helper";
  tool = runCommand "pynix-test-tool" { } ''
    mkdir -p $out/bin $out/nix-support
    printf '#!${runtimeShell}\\necho tool "$@"\\n' > $out/bin/pynix-test-tool
    chmod +x $out/bin/pynix-test-tool
    echo ${helper} > $out/nix-support/propagated-user-env-packages
  '';
}
"""


def _shell(store_uri: str, nix_file: Path, *args: str) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        "-c",
        "import sys; from pynix import main; sys.argv = ['pynix', *sys.argv[1:]]; main()",
        "env",
        "shell",
        "--file",
        str(nix_file),
        "--store",
        store_uri,
        *args,
    ]
    return subprocess.run(  # noqa: S603 -- fixed argv, no shell, every path from the test itself
        argv, capture_output=True, text=True, check=False, env=os.environ.copy()
    )


@pytest.fixture
def packages(tmp_path: Path, nixpkgs_path: str) -> Path:
    path = tmp_path / "packages.nix"
    path.write_text(with_nixpkgs(_PACKAGES, nixpkgs_path), encoding="utf-8")
    return path


def test_the_command_finds_the_package_and_what_it_propagates(
    shared_nix_environment: NixTestEnvironment, packages: Path
) -> None:
    result = _shell(
        shared_nix_environment.store_uri,
        packages,
        "tool",
        "--command",
        "sh",
        "-c",
        "pynix-test-tool a -- b; pynix-test-helper",
    )
    assert (result.returncode, result.stdout) == (0, "tool a -- b\nhelper\n"), result.stderr


def test_a_double_dash_in_the_command_reaches_the_command(
    shared_nix_environment: NixTestEnvironment, packages: Path
) -> None:
    result = _shell(shared_nix_environment.store_uri, packages, "tool", "--command", "pynix-test-tool", "--", "x")
    assert (result.returncode, result.stdout) == (0, "tool -- x\n"), result.stderr


def test_the_exit_status_of_the_command_reaches_the_caller(
    shared_nix_environment: NixTestEnvironment, packages: Path
) -> None:
    result = _shell(shared_nix_environment.store_uri, packages, "tool", "-c", "sh", "-c", "exit 42")
    assert result.returncode == 42, result.stderr
