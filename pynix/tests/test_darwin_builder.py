"""Unit tests for the macOS builder runner: what it reads and says, with no VM.

A VM needs Apple silicon, QEMU's vmapple fork and a saved guest, which no CI
runner has. The snapshot manifest, the QEMU command line and the vsock
protocol are testable against fake sockets, so these tests pin them.
"""

from __future__ import annotations

import json
import platform
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import anyio
import pytest
from anyio.abc import SocketListener, SocketStream

from pynix._impl import darwin_builder
from pynix._impl.darwin_builder import DarwinBuilderError, Snapshot


@pytest.fixture
def short_dir() -> Iterator[Path]:
    # A Unix socket path takes 104 bytes on macOS, and pytest's tmp_path there is longer.
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        yield Path(directory)


async def _write_snapshot(directory: Path, **overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "qemu": str(directory / "qemu-system-aarch64"),
        "qemuImg": str(directory / "qemu-img"),
        "args": ["-qmp", "unix:{run}/qmp.sock,server=on,wait=off", "-incoming", "defer"],
        "overlay": str(directory / "overlay.qcow2"),
        "aux": str(directory / "aux.img"),
        "state": str(directory / "state.bin"),
        "daemonVsockPort": 3030,
        "clockVsockPort": 3031,
        **overrides,
    }
    for key in ("qemu", "qemuImg", "overlay", "aux", "state"):
        await anyio.Path(str(data[key])).touch()
    await anyio.Path(directory / "snapshot.json").write_text(json.dumps(data))
    return data


async def test_the_run_directory_replaces_the_placeholder(tmp_path: Path) -> None:
    await _write_snapshot(tmp_path)
    snapshot = await Snapshot.read(str(tmp_path))

    argv = snapshot.qemu_argv(Path("/run/x"))

    assert argv == [str(tmp_path / "qemu-system-aarch64"), "-qmp", "unix:/run/x/qmp.sock,server=on,wait=off",
                    "-incoming", "defer"]  # fmt: skip


async def test_a_snapshot_without_a_key_is_a_darwin_builder_error(tmp_path: Path) -> None:
    await anyio.Path(tmp_path / "snapshot.json").write_text('{"qemu": "/x"}')

    with pytest.raises(DarwinBuilderError, match="cannot read the macOS builder snapshot"):
        await Snapshot.read(str(tmp_path))


async def test_a_snapshot_names_the_file_it_lost(tmp_path: Path) -> None:
    data = await _write_snapshot(tmp_path)
    await anyio.Path(str(data["state"])).unlink()

    with pytest.raises(DarwinBuilderError, match=f"needs {data['state']}, which is missing"):
        await Snapshot.read(str(tmp_path))


async def _vsock_device(listener: SocketListener, answers: dict[int, bytes], received: list[bytes]) -> None:
    """virtio-vsock-unix's host side: ``CONNECT <port>``, then the guest's bytes."""
    async with listener:
        while True:
            client: SocketStream = await listener.accept()
            async with client:
                line = b""
                while not line.endswith(b"\n"):
                    line += await client.receive(1)
                port = int(line.split()[1])
                if port not in answers:
                    continue
                await client.send(f"OK {1 << 30}\n".encode())
                data = b""
                with anyio.move_on_after(1):
                    while len(data) < 8:
                        data += await client.receive()
                received.append(data)
                await client.send(answers[port])


@pytest.mark.parametrize(
    ("reply", "answers"),
    [
        (darwin_builder._WORD.pack(darwin_builder._WORKER_MAGIC_2), True),
        (darwin_builder._WORD.pack(0), False),
    ],
)
async def test_only_the_worker_handshake_counts_as_a_daemon(short_dir: Path, reply: bytes, answers: bool) -> None:
    path = short_dir / "vsock.sock"
    received: list[bytes] = []
    listener = await anyio.create_unix_listener(path)
    async with anyio.create_task_group() as group:
        group.start_soon(_vsock_device, listener, {3030: reply}, received)
        assert await darwin_builder._daemon_answers(path, 3030) is answers
        group.cancel_scope.cancel()
    assert received == [darwin_builder._WORD.pack(darwin_builder._WORKER_MAGIC_1)]


async def test_a_port_the_guest_refuses_is_no_daemon(short_dir: Path) -> None:
    path = short_dir / "vsock.sock"
    listener = await anyio.create_unix_listener(path)
    async with anyio.create_task_group() as group:
        group.start_soon(_vsock_device, listener, {}, [])
        assert await darwin_builder._daemon_answers(path, 3030) is False
        group.cancel_scope.cancel()


async def test_no_socket_is_no_daemon(short_dir: Path) -> None:
    assert await darwin_builder._daemon_answers(short_dir / "absent.sock", 3030) is False


async def _accept_and_stay_silent(listener: SocketListener) -> None:
    async with listener, await listener.accept():
        await anyio.sleep_forever()


async def test_a_handshake_nobody_answers_times_out(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A first boot of macOS 26 left one request unanswered: with no limit, the
    # whole wait for the daemon went to that one attempt.
    monkeypatch.setattr(darwin_builder, "_HANDSHAKE_TIMEOUT", 0.1)
    path = short_dir / "vsock.sock"
    listener = await anyio.create_unix_listener(path)
    async with anyio.create_task_group() as group:
        group.start_soon(_accept_and_stay_silent, listener)
        with anyio.fail_after(5):
            assert await darwin_builder._daemon_answers(path, 3030) is False
        group.cancel_scope.cancel()


@pytest.mark.parametrize("error", [anyio.BrokenResourceError, anyio.EndOfStream, ConnectionRefusedError])
async def test_a_refused_client_leaves_the_forward_serving(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    # A reset while the device writes the handshake reaches the sender as
    # EPIPE, which anyio raises as BrokenResourceError; that is a race a fake
    # device cannot force, so the first handshake raises it directly.
    real = darwin_builder.connect_guest
    calls: list[int] = []

    async def refuse_first(vsock: Path, port: int) -> SocketStream:
        calls.append(port)
        if len(calls) == 1:
            raise error
        return await real(vsock, port)

    monkeypatch.setattr(darwin_builder, "connect_guest", refuse_first)
    vsock, listen = short_dir / "vsock.sock", short_dir / "daemon.sock"
    device = await anyio.create_unix_listener(vsock)
    reply = darwin_builder._WORD.pack(darwin_builder._WORKER_MAGIC_2)
    async with anyio.create_task_group() as group:
        group.start_soon(_vsock_device, device, {3030: reply}, [])
        await group.start(darwin_builder._forward, vsock, 3030, listen)
        async with await anyio.connect_unix(listen) as refused:
            with pytest.raises((anyio.EndOfStream, anyio.BrokenResourceError)):
                await refused.receive()
        async with await anyio.connect_unix(listen) as client:
            await client.send(darwin_builder._WORD.pack(darwin_builder._WORKER_MAGIC_1))
            with anyio.fail_after(5):
                assert await client.receive(8) == darwin_builder._WORD.pack(darwin_builder._WORKER_MAGIC_2)
        group.cancel_scope.cancel()


@pytest.mark.parametrize(("reply", "sets"), [(b"ok\n", True), (b"date: illegal time\n", False)])
async def test_the_clock_is_set_only_when_the_guest_says_ok(short_dir: Path, reply: bytes, sets: bool) -> None:
    path = short_dir / "vsock.sock"
    received: list[bytes] = []
    listener = await anyio.create_unix_listener(path)
    async with anyio.create_task_group() as group:
        group.start_soon(_vsock_device, listener, {3031: reply}, received)
        if sets:
            await darwin_builder._set_clock(path, 3031)
        else:
            with pytest.raises(DarwinBuilderError, match="did not set its clock"):
                await darwin_builder._set_clock(path, 3031)
        group.cancel_scope.cancel()
    assert received[0].strip().isdigit()


def test_the_store_is_refused_next_to_the_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")

    with pytest.raises(SystemExit):
        darwin_builder.requested_snapshot(snapshot="/s", store_named=True)
    with pytest.raises(SystemExit):
        darwin_builder.requested_snapshot(snapshot=None, store_named=False)
    assert darwin_builder.requested_snapshot(snapshot="/s", store_named=False) == "/s"
