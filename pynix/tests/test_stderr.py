"""stdout holds the result, stderr everything else, and stderr says only what is news.

A caller can filter stderr only because nothing else is on stdout. A caller
stops filtering it only if it stays quiet: a build that changes nothing prints
nothing, as ``nix build`` prints nothing. Issue nixidae/nixidae#863.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import TYPE_CHECKING

import anyio
import pytest
from huggorm.testing.environment import LINUX_CHROOT_BUILD

from pynix import parse

if TYPE_CHECKING:
    from pathlib import Path

    from huggorm.testing.environment import NixTestEnvironment

_CLI = "import sys, pynix; sys.argv[0] = 'pynix'; pynix.main()"


def _derivation(tmp_path: Path) -> Path:
    # `${./input}` copies a file at every evaluation, which Nix reports as a
    # chatty activity: 34 of the 36 lines that #863 counted were these.
    (tmp_path / "input").write_text(tmp_path.name)
    nix_file = tmp_path / "quiet.nix"
    nix_file.write_text(
        """
derivation {
  name = "pynix-quiet-stderr";
  system = builtins.currentSystem;
  builder = "/bin/sh";
  args = [ "-c" "read x < ${./input}; echo $x > $out" ];
}
"""
    )
    return nix_file


@LINUX_CHROOT_BUILD
async def test_a_build_that_changes_nothing_prints_only_the_result(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["build", "--file", str(_derivation(tmp_path)), *shared_nix_environment.pynix_store_args()]
    await parse(argv).run()
    capsys.readouterr()

    await parse(argv).run()

    captured = capsys.readouterr()
    assert "pynix-quiet-stderr" in json.loads(captured.out)["outputs"]["out"]
    assert captured.err == ""


@LINUX_CHROOT_BUILD
async def test_a_higher_verbosity_shows_the_activities(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The control for the case above: the copy is there to hide."""
    argv = ["build", "--verbosity", "chatty", "--file", str(_derivation(tmp_path))]
    await parse([*argv, *shared_nix_environment.pynix_store_args()]).run()

    assert "to the store...\n" in capsys.readouterr().err


@LINUX_CHROOT_BUILD
@pytest.mark.parametrize("print_build_logs", [False, True], ids=["quiet", "logs"])
async def test_the_command_prints_a_build_log_only_when_asked(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    print_build_logs: bool,
) -> None:
    """The real command, in a process of its own, with nothing shared.

    A build logs on huggorm's process stream alone. Unread, that stream goes to
    a fallback logger at CHATTY, which prints the builder's own output whether
    or not the caller asked for it. Issue nixidae/nixidae#1053.
    """
    # A name for each case, because the cases share a store.
    name = f"pynix-build-log-{print_build_logs}".lower()
    nix_file = tmp_path / "loud.nix"
    nix_file.write_text(
        f"""
derivation {{
  name = "{name}";
  system = builtins.currentSystem;
  builder = "/bin/sh";
  args = [ "-c" "echo from-the-builder; echo > $out" ];
}}
"""
    )
    # No configuration of the host or of the user: as `load_config=False`
    # does for the tests in this process.
    env = {
        **os.environ,
        "NIX_CONF_DIR": str(tmp_path),
        "NIX_USER_CONF_FILES": str(tmp_path / "absent.conf"),
        "PYNIX_CONFIG": str(tmp_path / "absent.toml"),
    }
    argv = [sys.executable, "-c", _CLI, "build", "--file", str(nix_file), *shared_nix_environment.pynix_store_args()]
    if print_build_logs:
        argv.append("--print-build-logs")

    result = await anyio.run_process(argv, env=env, check=False)

    err = result.stderr.decode()
    assert result.returncode == 0, err
    assert name in json.loads(result.stdout)["outputs"]["out"]
    assert re.search(rf"^building '/\S+-{name}\.drv'\.\.\.$", err, re.MULTILINE), err
    assert ("from-the-builder" in err) is print_build_logs, err
