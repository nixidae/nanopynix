"""``pynix key``, ``pynix store sign`` and ``pynix store copy-sigs``
(nixidae/nixidae#115), held to ``nix key`` and ``nix store verify``.

The stores are chroot stores under ``tmp_path``, so no daemon and no network
take part.

``nix store verify --sigs-needed 1`` cannot be the oracle: it trusts a
content-addressed path with no signature (``ValidPathInfo::checkSignatures``),
and ``store add-file`` makes only those. So ``nix path-info --sigs`` reads what
the store holds, and Nix's ``PublicKey::verifyDetached`` checks it against the
path's fingerprint.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import anyio
import huggorm
import pytest
from huggorm import PublicKey

from pynix import parse
from support.nix_oracle import require_matching_nix_cli


async def _pynix(argv: list[str]) -> int:
    try:
        await parse(argv).run()
    except SystemExit as exc:
        return 1 if isinstance(exc.code, str) else int(exc.code or 0)
    return 0


async def _out(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    capsys.readouterr()
    assert await _pynix(argv) == 0, capsys.readouterr().err
    return capsys.readouterr().out


@pytest.fixture
async def secret(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    key = tmp_path / "secret"
    await anyio.Path(key).write_text(
        await _out(["key", "generate-secret", "--key-name", "cache-1"], capsys), encoding="utf-8"
    )
    return key


async def _public(secret: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(await anyio.Path(secret).read_text(encoding="utf-8")))
    return await _out(["key", "convert-secret-to-public"], capsys)


async def _add(store: Path, content: bytes, capsys: pytest.CaptureFixture[str]) -> str:
    source = store.parent / "greeting"
    await anyio.Path(source).write_bytes(content)
    path: str = json.loads(await _out(["store", "add-file", "--store", str(store), str(source)], capsys))["path"]
    return path


async def _verify(store: Path, public: str, path: str) -> bool:
    """Whether the store holds a signature of *path* that *public* verifies."""
    listed = await anyio.run_process(
        ["nix", "--extra-experimental-features", "nix-command", "path-info", "--store", str(store), "--sigs", path]
    )
    held = set(listed.stdout.decode().split()[1:])
    async with huggorm.AsyncSession(str(store)) as session:
        opened = session.store()
        info = await opened.query_path_info(await opened.parse_store_path(path))
        fingerprint = await opened.fingerprint(info)
    key = PublicKey(public)
    return any(key.verify_detached(fingerprint, sig) for sig in info.sigs() if str(sig) in held)


async def test_the_secret_key_has_its_name_and_no_newline(secret: Path) -> None:
    text = await anyio.Path(secret).read_text(encoding="utf-8")
    assert text.startswith("cache-1:")
    assert not text.endswith("\n")


async def test_the_public_key_is_the_one_nix_derives(
    secret: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    await require_matching_nix_cli()
    ours = await _public(secret, capsys, monkeypatch)
    theirs = await anyio.run_process(
        ["nix", "--extra-experimental-features", "nix-command", "key", "convert-secret-to-public"],
        input=await anyio.Path(secret).read_bytes(),
    )
    assert ours == theirs.stdout.decode()
    assert ours.startswith("cache-1:")


async def test_a_corrupt_secret_key_is_refused(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("cache-1"))
    assert await _pynix(["key", "convert-secret-to-public"]) == 1
    assert "key is corrupt" in capsys.readouterr().err


async def test_a_signed_path_verifies_with_the_public_key_only(
    tmp_path: Path, secret: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    await require_matching_nix_cli()
    store = tmp_path / "store"
    path = await _add(store, b"hello\n", capsys)
    public = await _public(secret, capsys, monkeypatch)
    assert not await _verify(store, public, path)

    assert await _pynix(["store", "sign", "--store", str(store), "--key-file", str(secret), path]) == 0
    assert "added 1 signatures" in capsys.readouterr().err
    assert await _verify(store, public, path)

    # A second sign adds nothing: the signature of one key over one path is one.
    assert await _pynix(["store", "sign", "--store", str(store), "--key-file", str(secret), path]) == 0
    assert "added 0 signatures" in capsys.readouterr().err

    other = tmp_path / "other"
    await anyio.Path(other).write_text(
        await _out(["key", "generate-secret", "--key-name", "cache-1"], capsys), encoding="utf-8"
    )
    assert not await _verify(store, await _public(other, capsys, monkeypatch), path)


async def test_copy_sigs_imports_what_the_other_store_signed(
    tmp_path: Path, secret: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    await require_matching_nix_cli()
    ours = tmp_path / "ours" / "store"
    theirs = tmp_path / "theirs" / "store"
    await anyio.Path(ours.parent).mkdir()
    await anyio.Path(theirs.parent).mkdir()
    path = await _add(ours, b"hello\n", capsys)
    assert await _add(theirs, b"hello\n", capsys) == path
    assert await _pynix(["store", "sign", "--store", str(theirs), "--key-file", str(secret), path]) == 0
    public = await _public(secret, capsys, monkeypatch)
    assert not await _verify(ours, public, path)

    assert await _pynix(["store", "copy-sigs", "--store", str(ours), "--substituter", str(theirs), path]) == 0
    assert "imported 1 signatures" in capsys.readouterr().err
    assert await _verify(ours, public, path)


async def test_copy_sigs_needs_a_substituter(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert await _pynix(["store", "copy-sigs", "--store", str(tmp_path / "store"), "/nix/store/x"]) == 1
    assert "you must specify at least one substituter using '-s'" in capsys.readouterr().err


async def test_all_takes_no_paths(tmp_path: Path, secret: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store = tmp_path / "store"
    path = await _add(store, b"hello\n", capsys)
    argv = ["store", "sign", "--store", str(store), "--key-file", str(secret), "--all", path]
    assert await _pynix(argv) == 1
    assert "'--all' does not expect arguments" in capsys.readouterr().err
