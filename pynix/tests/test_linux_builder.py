"""Unit tests for the Linux builder runner: what it hands vzvm and ssh, with no VM.

A VM needs Apple silicon and a built guest, which no CI runner has. What the
runner decides before vzvm starts is pure, so these tests pin it.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import anyio
import pytest
from anyio.abc import SocketListener, SocketStream

from pynix._impl import linux_builder
from pynix._impl.linux_builder import LinuxBuilder, LinuxBuilderError, Manifest, _Plan

_GIB = 1024


def _manifest(tmp: Path) -> Manifest:
    return Manifest(
        vzvm=str(tmp / "vzvm"),
        kernel=str(tmp / "Image"),
        initrd=str(tmp / "initrd"),
        cmdline="init=/nix/store/x/init",
        daemon_vsock_port=3030,
        ssh_vsock_port=22,
        ssh_host_key="ssh-ed25519 AAAA test",
    )


def _plan(tmp: Path, home: Path | None = None) -> _Plan:
    return _Plan(_manifest(tmp), tmp / "work", rosetta=True, nested=False, home=home)


@pytest.fixture
def short_dir() -> Iterator[Path]:
    # A Unix socket path takes 104 bytes on macOS, and pytest's tmp_path there is longer.
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        yield Path(directory)


def test_a_build_forwards_only_the_daemon_and_shares_the_store_read_only(tmp_path: Path) -> None:
    config = _plan(tmp_path).vzvm_config()

    assert config["vsock"] == {"forwards": [{"listen": str(tmp_path / "work/daemon.sock"), "vsockPort": 3030}]}
    assert config["shares"] == [{"tag": "hostnix", "path": "/nix", "readOnly": True}]
    assert "pynix.home=" not in str(config["cmdline"])


def test_a_run_forwards_ssh_and_shares_home_writable_and_keys_read_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = _plan(tmp_path, home).vzvm_config()

    forwards = config["vsock"]["forwards"]  # type: ignore[index]  # vzvm_config returns JSON
    assert {"listen": str(tmp_path / "work/ssh.sock"), "vsockPort": 22} in forwards
    assert config["shares"] == [
        {"tag": "hostnix", "path": "/nix", "readOnly": True},
        {"tag": "pynix-keys", "path": str(tmp_path / "work/keys"), "readOnly": True},
        {"tag": "pynix-home", "path": str(home)},
    ]
    assert str(config["cmdline"]).endswith(f" pynix.home={home}")


def test_ssh_goes_through_the_socket_and_checks_the_pinned_host_key(tmp_path: Path) -> None:
    home = tmp_path / "home"
    builder = LinuxBuilder("unix://x", _plan(tmp_path, home))

    argv = builder.ssh_argv(["echo", "a b"], cwd="/some dir", tty=False)

    assert f"ProxyCommand=/usr/bin/nc -U {tmp_path / 'work/ssh.sock'}" in argv
    assert "-p" not in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert argv[-3:-1] == ["root@pynix-linux-builder", "--"]
    assert argv[-1] == f"cd '/some dir' 2>/dev/null || cd {home}; export HOME={home}; exec echo 'a b'"


def test_ssh_needs_a_shared_home(tmp_path: Path) -> None:
    with pytest.raises(LinuxBuilderError, match="no home directory"):
        LinuxBuilder("unix://x", _plan(tmp_path)).ssh_argv(["true"], cwd="/", tty=False)


@pytest.mark.parametrize(
    ("host_gib", "vm_mib"),
    [
        (8, 4 * _GIB),  # the floor would take it all: the host keeps its reserve
        (16, 8 * _GIB),  # the floor
        (24, 12 * _GIB),  # half
        (64, 32 * _GIB),
    ],
)
def test_the_vm_takes_half_the_host_memory_within_the_floor_and_the_reserve(
    monkeypatch: pytest.MonkeyPatch, host_gib: int, vm_mib: int
) -> None:
    page = 16384
    sizes = {"SC_PAGE_SIZE": page, "SC_PHYS_PAGES": host_gib * 1024**3 // page}
    monkeypatch.setattr(os, "sysconf", sizes.__getitem__)

    assert linux_builder._memory_mib() == vm_mib


async def test_a_manifest_without_a_key_is_a_linux_builder_error(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    await anyio.Path(path).write_text('{"vzvm": "/x"}')

    with pytest.raises(LinuxBuilderError, match="cannot read the Linux builder manifest"):
        await Manifest.read(str(path))


async def test_a_collected_guest_names_the_missing_path(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    for present in (manifest.vzvm, manifest.kernel):
        await anyio.Path(present).touch()

    with pytest.raises(LinuxBuilderError, match=f"{manifest.initrd} is missing"):
        await manifest.check_present("m.json")


async def _serve_once(listener: SocketListener, reply: bytes) -> None:
    async with listener:
        client: SocketStream = await listener.accept()
        async with client:
            await client.receive(8)
            if reply:
                await client.send(reply)


@pytest.mark.parametrize(
    ("reply", "answers"),
    [
        (linux_builder._WORD.pack(linux_builder._WORKER_MAGIC_2), True),
        # vzvm accepts before the guest listens, and then closes the connection.
        (b"", False),
        (linux_builder._WORD.pack(0), False),
    ],
)
async def test_only_the_worker_handshake_counts_as_a_daemon(short_dir: Path, reply: bytes, answers: bool) -> None:
    path = short_dir / "daemon.sock"
    listener = await anyio.create_unix_listener(path)
    async with anyio.create_task_group() as group:
        group.start_soon(_serve_once, listener, reply)
        assert await linux_builder._daemon_answers(path) is answers


async def test_no_socket_is_no_daemon(short_dir: Path) -> None:
    assert await linux_builder._daemon_answers(short_dir / "absent.sock") is False
