# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportMissingParameterType=false, reportUnknownParameterType=false
# nanopynix / pynix are C++ nanobind extensions without type stubs.
# Variable types cascade from unknown member access; capsys fixture lacks stubs.

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from nanopynix_proto.nix.store import GetBuildLogRequest

from nanopynix_testing.nix_markers import LINUX_CHROOT_BUILD
from pynix import parse

if TYPE_CHECKING:
    from nanopynix_testing.nix_environment import NixTestEnvironment


@LINUX_CHROOT_BUILD
async def test_nanopynix_store_get_build_log_from_populated_store(
    populated_store: dict[str, str],
    shared_nix_environment: NixTestEnvironment,
) -> None:
    async with shared_nix_environment.rpc_session() as nix, nix.store() as store:
        response = await store.rpc.get_build_log(GetBuildLogRequest(path=populated_store["log_path"]))

    assert response.log is not None
    assert "pynix-log-line" in response.log


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
