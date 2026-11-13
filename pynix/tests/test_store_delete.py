"""`pynix store delete`, as `nix store delete` (`src/nix/store-delete.cc`)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import anyio
import huggorm
import pytest
from huggorm.errors import NixError, UsageError
from huggorm.testing.environment import NixTestEnvironment

from pynix import parse

# Nix holds a temporary root on each path a process adds or roots until that
# process exits, and `--ignore-liveness` skips only the permanent roots. So a
# child adds each path, and roots it when asked.
_ADD = """
import sys
import huggorm
uri, name, root, *references = sys.argv[1:]
store = huggorm.Store(uri)
path = store.add_to_store(name, name.encode(), references=[store.parse_store_path(r) for r in references])
if root:
    store.add_perm_root(path, root)
print(store.print_store_path(path))
"""


def _minor(version: str) -> tuple[int, int]:
    """Major and minor of `2.35.4`, and of the git lane's `2.36pre20260912_203f85b2`."""
    found = re.match(r"(\d+)\.(\d+)", version)
    if found is None:
        raise ValueError(f"not a Nix version: {version!r}")
    return int(found[1]), int(found[2])


async def _add(
    environment: NixTestEnvironment, name: str, *, references: list[str] | None = None, root: Path | None = None
) -> str:
    """A path that no living process holds, added by a child that has exited."""
    argv = [environment.store_uri, name, str(root or ""), *(references or [])]
    done = await anyio.run_process([sys.executable, "-c", _ADD, *argv])
    return done.stdout.decode().strip()


async def _delete(environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str], *argv: str) -> list[str]:
    """The paths `pynix store delete` says it deleted."""
    capsys.readouterr()
    await parse(["store", "delete", *argv, *environment.pynix_store_args()]).run()
    result = json.loads(capsys.readouterr().out)
    assert isinstance(result["bytesFreed"], int)
    return [str(path) for path in result["paths"]]


async def test_a_dead_path_is_deleted(
    isolated_nix_environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    path = await _add(isolated_nix_environment, "dead")

    assert await _delete(isolated_nix_environment, capsys, path) == [path]
    assert not isolated_nix_environment.physical_path(path).exists()


async def test_a_path_a_root_reaches_is_refused(
    isolated_nix_environment: NixTestEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = await _add(isolated_nix_environment, "rooted", root=tmp_path / "root")

    with pytest.raises(NixError, match=r"alive|in use"):
        await _delete(isolated_nix_environment, capsys, path)
    assert isolated_nix_environment.physical_path(path).exists()


async def test_skip_alive_keeps_a_rooted_path_and_deletes_a_dead_one(
    isolated_nix_environment: NixTestEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--skip-alive` is `gcDeleteDead` within the paths, which Nix 2.34
    cannot do without collecting the whole store; huggorm refuses it there."""
    rooted = await _add(isolated_nix_environment, "rooted", root=tmp_path / "root")
    dead = await _add(isolated_nix_environment, "dead")
    bystander = await _add(isolated_nix_environment, "bystander")

    if _minor(huggorm.nix_version()) < (2, 35):
        with pytest.raises(UsageError, match="only to delete them"):
            await _delete(isolated_nix_environment, capsys, "--skip-alive", rooted, dead)
        assert isolated_nix_environment.physical_path(dead).exists()
    else:
        assert await _delete(isolated_nix_environment, capsys, "--skip-alive", rooted, dead) == [dead]
        assert not isolated_nix_environment.physical_path(dead).exists()
    assert isolated_nix_environment.physical_path(rooted).exists()
    assert isolated_nix_environment.physical_path(bystander).exists()


async def test_ignore_liveness_deletes_a_rooted_path(
    isolated_nix_environment: NixTestEnvironment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = await _add(isolated_nix_environment, "rooted", root=tmp_path / "root")

    assert await _delete(isolated_nix_environment, capsys, "--ignore-liveness", path) == [path]
    assert not isolated_nix_environment.physical_path(path).exists()


async def test_a_path_another_path_refers_to_goes_only_with_also_referrers(
    isolated_nix_environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    target = await _add(isolated_nix_environment, "target")
    referrer = await _add(isolated_nix_environment, "referrer", references=[target])

    with pytest.raises(NixError, match=r"alive|in use"):
        await _delete(isolated_nix_environment, capsys, target)
    assert isolated_nix_environment.physical_path(target).exists()

    if _minor(huggorm.nix_version()) < (2, 35):
        with pytest.raises(UsageError, match="referrers"):
            await _delete(isolated_nix_environment, capsys, "--also-referrers", target)
        assert isolated_nix_environment.physical_path(target).exists()
    else:
        deleted = await _delete(isolated_nix_environment, capsys, "--also-referrers", target)
        assert sorted(deleted) == sorted([target, referrer])
        assert not isolated_nix_environment.physical_path(target).exists()
        assert not isolated_nix_environment.physical_path(referrer).exists()
