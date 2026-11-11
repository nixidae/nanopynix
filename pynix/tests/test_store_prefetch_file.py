"""``pynix store prefetch-file``, against ``file://`` URLs so no test needs a network.

Each expected hash was also checked once against ``nix store prefetch-file
--json`` on the same input (nixidae/nixidae#1042).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest
from huggorm.errors import NixError
from huggorm.testing.environment import NixTestEnvironment

from pynix import parse


async def _prefetch(
    environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
    url: str,
    *arguments: str,
) -> dict[str, str]:
    await parse(["store", "prefetch-file", url, *arguments, *environment.pynix_store_args()]).run()
    result: dict[str, str] = json.loads(capsys.readouterr().out)
    return result


async def _add(environment: NixTestEnvironment, capsys: pytest.CaptureFixture[str], path: Path, name: str) -> str:
    """The path ``store add`` gives the same tree by NAR, to compare against."""
    await parse(["store", "add", str(path), "--name", name, *environment.pynix_store_args()]).run()
    added: str = json.loads(capsys.readouterr().out)["path"]
    return added


def _sri(data: bytes) -> str:
    return "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode()


def _tarball(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


async def test_a_file_is_added_flat_under_the_last_segment_of_the_url(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served = tmp_path / "message.txt"
    served.write_bytes(b"prefetched\n")
    got = await _prefetch(shared_nix_environment, capsys, served.as_uri())
    assert set(got) == {"storePath", "hash"}
    assert got["hash"] == _sri(b"prefetched\n")
    assert got["storePath"].endswith("-message.txt")
    assert shared_nix_environment.physical_path(got["storePath"]).read_bytes() == b"prefetched\n"


async def test_name_overrides_the_name_from_the_url(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served = tmp_path / "message.txt"
    served.write_bytes(b"named\n")
    got = await _prefetch(shared_nix_environment, capsys, served.as_uri(), "--name", "other")
    assert got["storePath"].endswith("-other")


async def test_hash_type_chooses_the_algorithm(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served = tmp_path / "message.txt"
    served.write_bytes(b"sha512\n")
    got = await _prefetch(shared_nix_environment, capsys, served.as_uri(), "--hash-type", "sha512")
    assert got["hash"] == "sha512-" + base64.b64encode(hashlib.sha512(b"sha512\n").digest()).decode()


async def test_unpack_makes_a_single_top_level_directory_the_root(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = _tarball(tmp_path / "source.tar.gz", {"source-1.0/README": b"read me\n"})
    got = await _prefetch(shared_nix_environment, capsys, archive.as_uri(), "--unpack", "--name", "source")
    assert (shared_nix_environment.physical_path(got["storePath"]) / "README").read_bytes() == b"read me\n"
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "README").write_bytes(b"read me\n")
    assert got["storePath"] == await _add(shared_nix_environment, capsys, tree, "source")


async def test_unpack_keeps_several_top_level_entries_under_one_root(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = _tarball(tmp_path / "two.tar.gz", {"one": b"1", "two": b"2"})
    got = await _prefetch(shared_nix_environment, capsys, archive.as_uri(), "--unpack")
    root = shared_nix_environment.physical_path(got["storePath"])
    assert sorted(p.name for p in root.iterdir()) == ["one", "two"]
    assert got["storePath"].endswith("-two.tar.gz")


async def test_an_empty_archive_is_refused(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    archive = _tarball(tmp_path / "empty.tar.gz", {})
    with pytest.raises(SystemExit):
        await _prefetch(shared_nix_environment, capsys, archive.as_uri(), "--unpack")
    assert "is empty" in capsys.readouterr().err


async def test_executable_adds_an_executable_file_by_nar(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served = tmp_path / "tool"
    served.write_bytes(b"#!/bin/sh\n")
    got = await _prefetch(shared_nix_environment, capsys, served.as_uri(), "--executable")
    assert os.access(shared_nix_environment.physical_path(got["storePath"]), os.X_OK)
    served.chmod(0o755)
    assert got["storePath"] == await _add(shared_nix_environment, capsys, served, "tool")


async def test_an_expected_hash_the_store_holds_needs_no_download(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served = tmp_path / "held.txt"
    served.write_bytes(b"held\n")
    first = await _prefetch(shared_nix_environment, capsys, served.as_uri())
    served.unlink()
    again = await _prefetch(shared_nix_environment, capsys, served.as_uri(), "--expected-hash", first["hash"])
    assert again == first


async def test_an_expected_hash_that_differs_is_refused(
    shared_nix_environment: NixTestEnvironment,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    served = tmp_path / "message.txt"
    served.write_bytes(b"real\n")
    with pytest.raises(NixError, match="hash mismatch"):
        await _prefetch(shared_nix_environment, capsys, served.as_uri(), "--expected-hash", _sri(b"other\n"))


async def test_a_url_with_no_path_segment_needs_a_name(
    shared_nix_environment: NixTestEnvironment,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        await _prefetch(shared_nix_environment, capsys, "https://example.com/")
    assert "cannot figure out file name for 'https://example.com/'" in capsys.readouterr().err
