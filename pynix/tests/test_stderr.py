"""stdout holds the result, stderr everything else, and stderr says only what is news.

A caller can filter stderr only because nothing else is on stdout. A caller
stops filtering it only if it stays quiet: a build that changes nothing prints
nothing, as ``nix build`` prints nothing. Issue nixidae/nixidae#863.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from nanopynix_testing.nix_markers import LINUX_CHROOT_BUILD
from pynix import parse

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from nanopynix_testing.nix_environment import NixTestEnvironment


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
