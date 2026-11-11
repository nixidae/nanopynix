"""``pynix hash`` (nixidae/nixidae#117), held to ``nix hash``.

The oracle is the output of ``nix hash`` for the same input. A conversion also
round-trips: each format converts back to the hash it came from.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import huggorm
import pytest
from structlog.testing import capture_logs

from pynix import parse
from support.ansi import strip_ansi
from support.nix_oracle import require_matching_nix_cli
from test_support.subprocess_output import run_process

_FORMATS = ["base16", "nix32", "base64", "sri"]
_SRI = "sha256-" + base64.b64encode(hashlib.sha256(b"hello\n").digest()).decode()


async def _pynix(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """Exit code, stdout, and stderr with its lines joined: rich wraps a long message."""
    code, nix_error = 0, ""
    try:
        await parse(["hash", *argv]).run()
    except SystemExit as exc:
        code = 1 if isinstance(exc.code, str) else int(exc.code or 0)
    except huggorm.errors.NixError as exc:
        code, nix_error = 1, str(exc)
    captured = capsys.readouterr()
    return code, captured.out, " ".join(strip_ansi(captured.err + nix_error).split())


async def _nix(argv: list[str]) -> tuple[int, str, str]:
    result = await run_process(["nix", "--extra-experimental-features", "nix-command", "hash", *argv])
    return result.returncode, result.stdout, strip_ansi(result.stderr)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "f").write_bytes(b"hello\n")
    (root / "x").write_bytes(b"#!/bin/sh\n")
    (root / "x").chmod(0o755)
    (root / "sub" / "z").write_bytes(b"z")
    (root / "l").symlink_to("f")
    return root


@pytest.mark.parametrize("to", _FORMATS)
async def test_each_format_converts_back_to_the_hash(to: str, capsys: pytest.CaptureFixture[str]) -> None:
    code, converted, _ = await _pynix(["convert", "--to", to, _SRI], capsys)
    assert code == 0
    code, back, _ = await _pynix(["convert", "--algo", "sha256", converted.strip()], capsys)
    assert (code, back) == (0, _SRI + "\n")


async def test_convert_takes_every_hash_in_order(capsys: pytest.CaptureFixture[str]) -> None:
    sha1 = "sha1-" + base64.b64encode(hashlib.sha1(b"x", usedforsecurity=False).digest()).decode()
    code, out, _ = await _pynix(["convert", "--to", "base16", _SRI, sha1], capsys)
    assert code == 0
    assert out.splitlines() == [
        hashlib.sha256(b"hello\n").hexdigest(),
        hashlib.sha1(b"x", usedforsecurity=False).hexdigest(),
    ]


async def test_a_bare_digest_needs_an_algorithm(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = await _pynix(["convert", hashlib.sha256(b"").hexdigest()], capsys)
    assert code == 1
    assert "does not include a type, nor is the type otherwise known from context" in err


async def test_from_refuses_a_hash_in_another_format(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = await _pynix(["convert", "--from", "base16", _SRI], capsys)
    assert (code, out) == (1, "")
    assert f"input hash '{_SRI}' has format 'sri', but '--from base16' was specified" in err


async def test_base32_is_an_alias_with_nix_s_warning(capsys: pytest.CaptureFixture[str]) -> None:
    with capture_logs() as logs:
        code, alias, _ = await _pynix(["convert", "--to", "base32", _SRI], capsys)
    _, nix32, _ = await _pynix(["convert", "--to", "nix32", _SRI], capsys)
    assert (code, alias) == (0, nix32)
    assert [entry["event"] for entry in logs] == ['"base32" is a deprecated alias for hash format "nix32".']


async def test_the_file_hash_is_the_digest_of_its_contents(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = await _pynix(["file", "--base16", str(tree / "f"), str(tree / "l")], capsys)
    assert code == 0
    assert out.splitlines() == [hashlib.sha256(b"hello\n").hexdigest()] * 2


async def test_two_format_flags_are_refused(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = await _pynix(["path", "--base16", "--format", "sri", str(tree)], capsys)
    assert (code, out) == (1, "")


async def test_git_mode_needs_its_experimental_feature(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = await _pynix(["path", "--mode", "git", str(tree)], capsys)
    assert code == 1
    assert "experimental Nix feature 'git-hashing' is disabled" in err


_ORACLE_CASES = [
    ["path", "{tree}"],
    ["path", "--base16", "{tree}"],
    ["path", "--format", "nix32", "--algo", "sha512", "{tree}"],
    ["path", "--type", "md5", "--base64", "{tree}/x"],
    ["path", "{tree}/l"],
    ["path", "--mode", "flat", "{tree}/f"],
    ["file", "{tree}/f", "{tree}/x"],
    ["file", "--type", "sha1", "--base32", "{tree}/sub/z"],
    ["convert", "--to", "nix32", _SRI],
    ["convert", "--to", "base64", "--from", "sri", _SRI],
    ["to-base16", _SRI],
    ["to-sri", "--type", "sha256", hashlib.sha256(b"").hexdigest()],
]


@pytest.mark.parametrize("case", _ORACLE_CASES, ids=[" ".join(case) for case in _ORACLE_CASES])
async def test_the_output_is_the_one_nix_prints(
    case: list[str], tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await require_matching_nix_cli()
    argv = [arg.format(tree=tree) for arg in case]
    code, out, _ = await _pynix(argv, capsys)
    nix_code, nix_out, nix_err = await _nix(argv)
    assert nix_code == 0, nix_err
    assert (code, out) == (nix_code, nix_out)
