"""``pynix build --namespaced`` in a huggorm server over an overlay of the
host store (nixidae/nixidae#1060).

The overlay's lower layer is the host store, because the server's own
closure must stay visible under /nix/store. So these tests need the host's
daemon, unprivileged user namespaces, and a daemon whose realisation queries
this build's Nix can make (nixidae/nixidae#1061). Each one skips, with the
reason, when the host lacks one.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import anyio
import anyio.to_thread
import huggorm
import pytest
from huggorm.errors import BuildError
from huggorm.namespace import remove_layers

from pynix import parse

PROBE = """
import sys
from huggorm.namespace import OverlayNamespace, enter_overlay_namespace
root = sys.argv[1]
enter_overlay_namespace(OverlayNamespace.under(root), mount_at=root + "/mnt")
"""

DAEMON_SOCKET = "/nix/var/nix/daemon-socket/socket"


def _minor(version: str) -> tuple[int, int]:
    major, minor = version.split(".")[:2]
    return int(major), int(minor)


async def _unsupported(tmp_path: Path) -> str:
    """Empty when this host can run a namespaced build, else the reason."""
    if not await anyio.Path(DAEMON_SOCKET).exists():
        return "no Nix daemon, and the overlay's lower store is the host's"
    probe = await anyio.run_process([sys.executable, "-c", PROBE, str(tmp_path / "probe")], check=False)
    if await anyio.Path(tmp_path / "probe").exists():
        await anyio.to_thread.run_sync(remove_layers, tmp_path / "probe")
    if probe.returncode:
        return probe.stderr.decode().strip().splitlines()[-1]
    nix = shutil.which("nix")
    if nix is None:
        return "no nix CLI to ask the daemon's version"
    info = await anyio.run_process([nix, "store", "info", "--store", "daemon", "--json"])
    daemon = json.loads(info.stdout)["version"]
    if _minor(huggorm.nix_version()) < (2, 35) <= _minor(daemon):
        return f"Nix {huggorm.nix_version()} cannot build over the {daemon} daemon (nixidae/nixidae#1061)"
    return ""


READ_EXTRA = "read x < /extra/marker; echo $x > $out"


async def _derivation(tmp_path: Path, unique: str, script: str) -> Path:
    """A derivation that runs *script* with nix.conf's ``/bin/sh``."""
    nix_file = tmp_path / "namespaced.nix"
    await anyio.Path(nix_file).write_text(
        f"""
        derivation {{
          name = "pynix-namespaced-{unique}";
          system = builtins.currentSystem;
          builder = "/bin/sh";
          args = [ "-c" "{script}" ];
        }}
        """
    )
    return nix_file


async def _build(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *extra: str,
    script: str = READ_EXTRA,
    overlay_dir: bool = True,
) -> tuple[str, str, Path]:
    if reason := await _unsupported(tmp_path):
        pytest.skip(reason)
    unique = uuid.uuid4().hex
    marker = tmp_path / "extra"
    await anyio.Path(marker).mkdir()
    await anyio.Path(marker / "marker").write_text(f"{unique}\n")
    root = tmp_path / "overlay"
    command = parse(
        [
            "build",
            "--namespaced",
            *(["--overlay-dir", str(root)] if overlay_dir else []),
            "--sandbox-path",
            f"/extra={marker}",
            "--file",
            str(await _derivation(tmp_path, unique, script)),
            *extra,
        ]
    )
    try:
        await command.run()
    finally:
        out = capsys.readouterr().out
    return json.loads(out)["outputs"]["out"], unique, root


async def test_a_namespaced_build_writes_its_upper_layer_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The output is in the overlay and not in the host store. Building it
    took nix.conf's ``/bin/sh`` and the ``--sandbox-path`` together."""
    try:
        out, unique, root = await _build(tmp_path, capsys, "--no-copy-back")
        name = anyio.Path(out).name
        assert await anyio.Path(root / "upper" / name).read_text() == f"{unique}\n"
        assert not await anyio.Path(out).exists()
    finally:
        await anyio.to_thread.run_sync(remove_layers, tmp_path)


async def test_a_build_without_an_overlay_dir_leaves_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker makes the layers in the temporary directory, and removes
    them when the build ends (nixidae/nixidae#1066)."""
    temporary = tmp_path / "tmp"
    await anyio.Path(temporary).mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    monkeypatch.setenv("TMPDIR", str(temporary))
    try:
        out, _, _ = await _build(tmp_path, capsys, "--no-copy-back", overlay_dir=False)
        assert not await anyio.Path(out).exists()
        assert [path async for path in anyio.Path(temporary).iterdir()] == []
    finally:
        await anyio.to_thread.run_sync(remove_layers, tmp_path)


async def test_copy_back_is_the_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    try:
        out, unique, _ = await _build(tmp_path, capsys)
        assert await anyio.Path(out).read_text() == f"{unique}\n"
    finally:
        await anyio.to_thread.run_sync(remove_layers, tmp_path)


async def test_a_failed_namespaced_build_raises_nix_s_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The error itself, as a build on the host store raises it: `main`
    turns a NixError into Nix's message, and would miss an ExceptionGroup."""
    try:
        with pytest.raises(BuildError, match="Cannot build"):
            await _build(tmp_path, capsys, "--no-copy-back", script="exit 1")
    finally:
        await anyio.to_thread.run_sync(remove_layers, tmp_path)
