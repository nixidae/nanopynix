# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportMissingParameterType=false, reportUnknownParameterType=false
# nanopynix / pynix are C++ nanobind extensions without type stubs.
# Variable types cascade from unknown member access; capsys fixture lacks stubs.

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from huggorm.testing.environment import LINUX_CHROOT_BUILD

from pynix import parse

if TYPE_CHECKING:
    from huggorm.testing.environment import NixTestEnvironment


@LINUX_CHROOT_BUILD
async def test_pynix_log_prints_build_log_from_populated_store(
    populated_store: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    cmd = parse(["log", populated_store["log_path"], "--store", populated_store["store_url"]])
    await cmd.run()
    captured = capsys.readouterr()
    assert "pynix-log-line" in captured.out


async def test_pynix_log_errors_when_build_log_unavailable(shared_nix_environment: NixTestEnvironment) -> None:
    path = "/nix/store/00000000000000000000000000000000-no-log"
    cmd = parse(["log", path, *shared_nix_environment.pynix_store_args()])
    with pytest.raises(SystemExit, match=r"build log of .* is not available"):
        await cmd.run()
