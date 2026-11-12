"""``pynix store verify`` (nixidae/nixidae#1296), held to ``nix store verify``.

Each case runs both programs on the same store and compares the exit code and
stderr. A built path is ``ultimate`` in the store that built it, and a copy of
it into a second store is not: there only a signature can make it trusted.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
from huggorm.testing.environment import LINUX_CHROOT_BUILD, NixTestEnvironment, force_rmtree, with_nixpkgs

from pynix import parse
from support.ansi import strip_ansi
from support.nix_oracle import require_matching_nix_cli

_LEAF = 'with import <nixpkgs> {}; runCommand "pynix-verify-leaf" {} "printf %s leaf > $out"\n'


async def _pynix(argv: list[str]) -> int:
    try:
        await parse(argv).run()
    except SystemExit as exc:
        return 1 if isinstance(exc.code, str) else int(exc.code or 0)
    return 0


async def _verify(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    capsys.readouterr()
    code = await _pynix(["store", "verify", *argv])
    return code, strip_ansi(capsys.readouterr().err)


async def _nix_verify(argv: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str]:
    done = await anyio.run_process(
        ["nix", "--extra-experimental-features", "nix-command", "store", "verify", *argv],
        check=False,
        env={**os.environ, **(env or {})},
    )
    return done.returncode, strip_ansi(done.stderr.decode())


async def _both(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """The exit code and stderr of both programs, which must agree."""
    ours = await _verify(argv, capsys)
    assert ours == await _nix_verify(argv)
    return ours


@pytest.fixture
async def copied_store(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[str]:
    """A second chroot store, outside every ``tmp_path`` for the reason
    ``test_copy.destination_store`` gives."""
    root = tmp_path_factory.mktemp("pynix-verify-copy")
    try:
        yield f"local://?root={root}"
    finally:
        force_rmtree(root)


@pytest.fixture
async def built(
    shared_nix_environment: NixTestEnvironment, nixpkgs_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> str:
    leaf = tmp_path / "leaf.nix"
    await anyio.Path(leaf).write_text(with_nixpkgs(_LEAF, nixpkgs_path))
    await parse(["build", "--file", str(leaf), *shared_nix_environment.pynix_store_args()]).run()
    return str(json.loads(capsys.readouterr().out)["outputs"]["out"])


@dataclass(frozen=True)
class _Copy:
    path: str
    #: The store that holds the copy.
    store: str
    #: The store that built it.
    source: str


@pytest.fixture
async def copied(
    shared_nix_environment: NixTestEnvironment, built: str, copied_store: str, capsys: pytest.CaptureFixture[str]
) -> _Copy:
    source = shared_nix_environment.store_uri
    await parse(["copy", built, "--to", copied_store, "--no-check-sigs", "--store", source]).run()
    capsys.readouterr()
    return _Copy(built, copied_store, source)


@dataclass(frozen=True)
class _Key:
    secret: Path
    public: str

    def nix_config(self) -> dict[str, str]:
        return {"NIX_CONFIG": f"trusted-public-keys = {self.public}"}


@pytest.fixture
async def key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> _Key:
    secret = tmp_path / "secret"
    await parse(["key", "generate-secret", "--key-name", "cache-1"]).run()
    await anyio.Path(secret).write_text(capsys.readouterr().out)
    done = await anyio.run_process(
        ["nix", "--extra-experimental-features", "nix-command", "key", "convert-secret-to-public"],
        input=await anyio.Path(secret).read_bytes(),
    )
    return _Key(secret, done.stdout.decode().strip())


async def _added(store: Path, capsys: pytest.CaptureFixture[str]) -> str:
    source = store.parent / "greeting"
    await anyio.Path(source).write_bytes(b"hello\n")
    await parse(["store", "add-file", "--store", str(store), str(source)]).run()
    return str(json.loads(capsys.readouterr().out)["path"])


async def test_a_content_addressed_path_is_trusted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Even with ``--sigs-needed``: its address is its proof."""
    await require_matching_nix_cli()
    store = tmp_path / "store"
    path = await _added(store, capsys)
    for flags in ([], ["--sigs-needed", "2"]):
        code, err = await _both(["--store", str(store), *flags, path], capsys)
        assert code == 0
        assert err == f"checking '{path}'...\n"


async def test_a_modified_path_fails_with_both_hashes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    await require_matching_nix_cli()
    store = tmp_path / "store"
    path = await _added(store, capsys)
    real = anyio.Path(f"{store}{path}")
    await real.chmod(0o644)
    await real.write_bytes(b"changed\n")
    code, err = await _both(["--store", str(store), path], capsys)
    assert code == 1
    assert f"path '{path}' was modified! expected hash 'sha256:" in err
    assert await _both(["--store", str(store), "--no-contents", path], capsys) == (0, f"checking '{path}'...\n")


async def test_an_invalid_path_fails_the_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Nix first says that it does not know how to build the path; pynix
    builds nothing, as ``pynix copy`` builds nothing."""
    await require_matching_nix_cli()
    store = tmp_path / "store"
    await _added(store, capsys)
    path = "/nix/store/00000000000000000000000000000000-x"
    argv = ["--store", str(store), path]
    code, err = await _verify(argv, capsys)
    nix_code, nix_err = await _nix_verify(argv)
    assert code == nix_code == 4
    assert err == f"error: path '{path}' is not valid\n"
    assert nix_err.endswith(err)


@LINUX_CHROOT_BUILD
async def test_a_built_path_is_trusted_where_it_was_built(
    shared_nix_environment: NixTestEnvironment, built: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """``ultimate``, unless the caller asks for signatures."""
    await require_matching_nix_cli()
    store = shared_nix_environment.pynix_store_args()
    assert await _both([*store, built], capsys) == (0, f"checking '{built}'...\n")
    assert await _both([*store, "--sigs-needed", "1", built], capsys) == (
        2,
        f"checking '{built}'...\npath '{built}' is untrusted\n",
    )


@LINUX_CHROOT_BUILD
async def test_a_copied_path_needs_a_trusted_signature(
    copied: _Copy, key: _Key, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    await require_matching_nix_cli()
    path, store = copied.path, ["--store", copied.store]
    untrusted = (2, f"checking '{path}'...\npath '{path}' is untrusted\n")
    assert await _both([*store, path], capsys) == untrusted
    assert await _both([*store, "--no-trust", path], capsys) == (0, f"checking '{path}'...\n")

    assert await _pynix(["store", "sign", *store, "--key-file", str(key.secret), path]) == 0
    # Signed, with a key that nothing trusts yet.
    assert await _both([*store, path], capsys) == untrusted

    monkeypatch.setenv("PYNIX_NIX_TRUSTED_PUBLIC_KEYS", key.public)
    trusted = (0, f"checking '{path}'...\n")
    assert await _verify([*store, path], capsys) == trusted
    assert await _nix_verify([*store, path], env=key.nix_config()) == trusted
    assert await _verify([*store, "--sigs-needed", "2", path], capsys) == untrusted


@LINUX_CHROOT_BUILD
async def test_a_substituter_lends_its_signatures(
    copied: _Copy, key: _Key, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy has no signature; the store it came from has one."""
    await require_matching_nix_cli()
    path = copied.path
    assert await _pynix(["store", "sign", "--store", copied.source, "--key-file", str(key.secret), path]) == 0
    monkeypatch.setenv("PYNIX_NIX_TRUSTED_PUBLIC_KEYS", key.public)
    argv = ["--store", copied.store, "--substituter", copied.source, path]
    expected = (0, f"checking '{path}'...\n")
    assert await _verify(argv, capsys) == expected
    assert await _nix_verify(argv, env=key.nix_config()) == expected
