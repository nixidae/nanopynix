"""``--file -`` reads the Nix expression from standard input, as ``nix --file -`` does.

A shell heredoc then carries a Nix program with no temporary file. A relative
path in it names from the working directory. Issue nixidae/nixidae#862.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from anyio import Path as AnyioPath
from huggorm.testing.environment import LINUX_CHROOT_BUILD, NixTestEnvironment

from pynix import parse


def _stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


async def _eval(argv: list[str], capsys: pytest.CaptureFixture[str], env: NixTestEnvironment) -> object:
    await parse(["eval", *argv, *env.pynix_store_args()]).run()
    return json.loads(capsys.readouterr().out)


async def test_eval_reads_a_file_from_stdin_and_names_paths_from_the_working_directory(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _stdin(monkeypatch, "{ path = toString ./x; sum = 1 + 2; }\n")

    assert await _eval(["--file", "-"], capsys, shared_nix_environment) == {"path": f"{tmp_path}/x", "sum": 3}


async def test_a_fragment_selects_from_the_expression_on_stdin(
    shared_nix_environment: NixTestEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--file=``: argparse reads a separate ``-#a.b`` as an option, as it reads any word that starts with ``-``."""
    _stdin(monkeypatch, "{ a.b = 42; }")

    assert await _eval(["--file=-#a.b"], capsys, shared_nix_environment) == 42


async def test_a_function_on_stdin_is_called_as_a_file_is(
    shared_nix_environment: NixTestEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _stdin(monkeypatch, "{ x ? 7 }: x")

    assert await _eval(["--file", "-"], capsys, shared_nix_environment) == 7


async def test_empty_stdin_is_refused(
    shared_nix_environment: NixTestEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stdin(monkeypatch, "  \n")

    with pytest.raises(SystemExit):
        await parse(["eval", "--file", "-", *shared_nix_environment.pynix_store_args()]).run()


@pytest.mark.parametrize("argv", [["--expr", "toString ./x"], []])
async def test_an_expression_names_paths_from_the_working_directory(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    """``--expr`` and a bare stdin answered ``$PWD/<string>/x`` before."""
    monkeypatch.chdir(tmp_path)
    _stdin(monkeypatch, "toString ./x")

    assert await _eval(argv, capsys, shared_nix_environment) == f"{tmp_path}/x"


@LINUX_CHROOT_BUILD
async def test_build_reads_a_derivation_from_stdin(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``build`` resolves ``--file`` twice, so this also proves stdin is read once."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "input").write_text(tmp_path.name)
    _stdin(
        monkeypatch,
        """
derivation {
  name = "pynix-stdin-build";
  system = builtins.currentSystem;
  builder = "/bin/sh";
  args = [ "-c" "read x < ${./input}; echo $x > $out" ];
}
""",
    )

    await parse(["build", "--file", "-", *shared_nix_environment.pynix_store_args()]).run()

    out = json.loads(capsys.readouterr().out)["outputs"]["out"]
    assert "pynix-stdin-build" in out
    assert await AnyioPath(shared_nix_environment.physical_path(out)).read_text() == f"{tmp_path.name}\n"
