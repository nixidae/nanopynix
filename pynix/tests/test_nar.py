"""``pynix nar`` (nixidae/nixidae#117), held to ``nix nar``.

The strong test is byte equality: the NAR ``pynix nar pack`` writes is the one
``nix nar pack`` writes for the same tree. The listings and ``cat`` read that
NAR and print what ``nix nar`` prints for it.
"""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest
from structlog.testing import capture_logs

from pynix import parse
from support.ansi import strip_ansi
from support.nix_oracle import require_matching_nix_cli


async def _pynix(argv: list[str]) -> int:
    try:
        await parse(["nar", *argv]).run()
    except SystemExit as exc:
        return 1 if isinstance(exc.code, str) else int(exc.code or 0)
    return 0


async def _nix(argv: list[str]) -> bytes:
    """What ``nix nar`` writes to stdout, as bytes: a NAR is not text."""
    result = await anyio.run_process(["nix", "--extra-experimental-features", "nix-command", "nar", *argv], check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    (root / "sub" / "deeper").mkdir(parents=True)
    (root / "f").write_bytes(b"hello\n")
    (root / "x").write_bytes(b"#!/bin/sh\n")
    (root / "x").chmod(0o755)
    (root / "sub" / "z").write_bytes(b"z")
    (root / "sub" / "deeper" / "empty").write_bytes(b"")
    (root / "l").symlink_to("f")
    return root


async def _pack(tree: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> bytes:
    assert await _pynix(["pack", str(tree)]) == 0
    return capsysbinary.readouterr().out


async def test_the_nar_is_byte_equal_to_nix_s(tree: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    await require_matching_nix_cli()
    ours = await _pack(tree, capsysbinary)
    assert ours == await _nix(["pack", str(tree)])
    assert ours.startswith(b"\x0d\x00\x00\x00\x00\x00\x00\x00nix-archive-1")


async def test_dump_path_warns_and_packs(tree: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    packed = await _pack(tree, capsysbinary)
    with capture_logs() as logs:
        assert await _pynix(["dump-path", str(tree)]) == 0
    assert capsysbinary.readouterr().out == packed
    assert [entry["event"] for entry in logs] == ["'nix nar dump-path' is a deprecated alias for 'nix nar pack'"]


@pytest.fixture
async def packed(tree: Path, tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> Path:
    out = tmp_path / "tree.nar"
    out.write_bytes(await _pack(tree, capsysbinary))
    return out


async def test_cat_prints_a_file(packed: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    assert await _pynix(["cat", str(packed), "/sub/z"]) == 0
    assert capsysbinary.readouterr().out == b"z"


@pytest.mark.parametrize("path", ["/sub", "/nope", "/l"])
async def test_cat_refuses_what_is_not_a_regular_file(
    packed: Path, path: str, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    assert await _pynix(["cat", str(packed), path]) == 1
    captured = capsysbinary.readouterr()
    assert captured.out == b""
    assert f"NAR does not contain regular file '{path}'" in " ".join(strip_ansi(captured.err.decode()).split())


async def test_json_refuses_directory(packed: Path) -> None:
    assert await _pynix(["ls", "--json", "-d", str(packed), "/"]) == 1


_LS_CASES = [
    ["/"],
    ["sub"],
    ["/f"],
    ["-l", "/"],
    ["-R", "/"],
    ["-lR", "/"],
    ["-d", "/sub"],
    ["-d", "/f"],
    ["-l", "/l"],
    ["--json", "/"],
    ["--json", "-R", "/"],
    ["--json", "/f"],
]


@pytest.mark.parametrize("case", _LS_CASES, ids=[" ".join(case) for case in _LS_CASES])
async def test_ls_prints_what_nix_prints(
    packed: Path, case: list[str], capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    await require_matching_nix_cli()
    *flags, path = case
    assert await _pynix(["ls", *flags, str(packed), path]) == 0
    ours = capsysbinary.readouterr().out
    theirs = await _nix(["ls", *flags, str(packed), path])
    assert ours == theirs


async def test_cat_prints_what_nix_prints(packed: Path, capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    await require_matching_nix_cli()
    assert await _pynix(["cat", str(packed), "/x"]) == 0
    theirs = await _nix(["cat", str(packed), "/x"])
    assert capsysbinary.readouterr().out == theirs
