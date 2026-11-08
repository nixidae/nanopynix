"""A macOS builder VM for the life of one pynix command (nixidae/nixidae#1168).

QEMU's ``vmapple`` machine runs the guest under Hypervisor.framework, from a
snapshot that ``darwin_builder.py save`` wrote: the guest booted, its
nix-daemon listening on vsock. A start loads the snapshot instead of booting,
so the daemon answers in a fraction of a second.

``snapshot.json`` names QEMU, its arguments (``{run}`` stands for the run
directory), the read-only disk overlay, the device state and the vsock ports.
Guest RAM is a file the arguments map private and copy-on-write, so each start
sees the saved RAM and none changes it. Each start gets a new qcow2 overlay
over the saved one, so every build starts from the same guest.

The guest's daemon runs as root for each vsock connection
(``nix-daemon --stdio``). virtio-vsock-unix reaches a guest port through a
handshake on one Unix socket, so pynix serves the daemon port on a plain
socket of its own, in a 0700 temporary directory.

The store has no files the host can read, so its URI carries
``local-fs=false``, as the Linux builder's does.
"""

from __future__ import annotations

import contextlib
import json
import platform
import shutil
import struct
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import anyio
import structlog
from anyio import Path as AnyioPath
from anyio.abc import ByteStream, Process, SocketStream, TaskStatus

from pynix._util import error_exit

logger = structlog.get_logger("pynix.darwin_builder")

_WORD = struct.Struct("<Q")
_WORKER_MAGIC_1 = 0x6E697863
_WORKER_MAGIC_2 = 0x6478696F
_START_TIMEOUT = 60.0
_STOP_TIMEOUT = 5.0
_QMP_TIMEOUT = 10.0
_HANDSHAKE_TIMEOUT = 2.0
_X_IGNORE_SHARED = [{"capability": "x-ignore-shared", "state": True}]
#: Longer than ``OK <port>\n`` and ``ok\n``: anything past it is not virtio-vsock-unix or the clock.
_LINE_MAX = 32


class DarwinBuilderError(Exception):
    """The macOS builder VM did not start, or its snapshot is unusable."""


@dataclass(frozen=True)
class Snapshot:
    """What ``snapshot.json`` says about one saved guest."""

    qemu: str
    qemu_img: str
    args: tuple[str, ...]
    overlay: str
    aux: str
    state: str
    daemon_vsock_port: int
    clock_vsock_port: int

    @classmethod
    async def read(cls, directory: str) -> Snapshot:
        path = Path(directory) / "snapshot.json"
        try:
            data = json.loads(await AnyioPath(path).read_text())
            snapshot = cls(
                qemu=data["qemu"],
                qemu_img=data["qemuImg"],
                args=tuple(str(arg) for arg in data["args"]),
                overlay=data["overlay"],
                aux=data["aux"],
                state=data["state"],
                daemon_vsock_port=int(data["daemonVsockPort"]),
                clock_vsock_port=int(data["clockVsockPort"]),
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            msg = f"cannot read the macOS builder snapshot {path}: {exc}"
            raise DarwinBuilderError(msg) from exc
        for needed in (snapshot.qemu, snapshot.qemu_img, snapshot.overlay, snapshot.aux, snapshot.state):
            if not await AnyioPath(needed).exists():
                msg = f"the macOS builder snapshot {directory} needs {needed}, which is missing"
                raise DarwinBuilderError(msg)
        return snapshot

    def qemu_argv(self, run: Path) -> list[str]:
        return [self.qemu, *(arg.replace("{run}", str(run)) for arg in self.args)]


class _Qmp:
    """Just enough QMP to load a snapshot: one command at a time, events skipped."""

    def __init__(self, stream: SocketStream) -> None:
        self._stream = stream
        self._buffer = b""

    @classmethod
    async def connect(cls, path: Path, vm: Process) -> _Qmp:
        with anyio.fail_after(_QMP_TIMEOUT):
            while True:
                try:
                    qmp = cls(await anyio.connect_unix(path))
                    break
                except OSError:
                    if vm.returncode is not None:
                        msg = f"QEMU exited with status {vm.returncode} before it took QMP"
                        raise DarwinBuilderError(msg) from None
                    await anyio.sleep(0.01)
            await qmp._line()
            await qmp.call("qmp_capabilities")
        return qmp

    async def _line(self) -> dict[str, object]:
        while b"\n" not in self._buffer:
            self._buffer += await self._stream.receive()
        raw, self._buffer = self._buffer.split(b"\n", 1)
        message: dict[str, object] = json.loads(raw)
        return message

    async def call(self, execute: str, **arguments: object) -> object:
        message: dict[str, object] = {"execute": execute}
        if arguments:
            message["arguments"] = arguments
        await self._stream.send(json.dumps(message).encode() + b"\n")
        while True:
            reply = await self._line()
            if "error" in reply:
                msg = f"QEMU refused {execute}: {reply['error']}"
                raise DarwinBuilderError(msg)
            if "return" in reply:
                return reply["return"]

    async def aclose(self) -> None:
        await self._stream.aclose()


async def connect_guest(vsock: Path, port: int) -> SocketStream:
    """A stream to the guest's vsock *port*: virtio-vsock-unix's ``CONNECT`` handshake."""
    stream = await anyio.connect_unix(vsock)
    line = b""
    try:
        # A guest whose vsock driver ignores the request never answers at all.
        # fail_after raises TimeoutError, an OSError like a refusal.
        with anyio.fail_after(_HANDSHAKE_TIMEOUT):
            await stream.send(f"CONNECT {port}\n".encode())
            while not line.endswith(b"\n") and len(line) < _LINE_MAX:
                line += await stream.receive(1)
    except BaseException:
        await stream.aclose()
        raise
    if not line.startswith(b"OK "):
        await stream.aclose()
        msg = f"the guest refused vsock port {port}"
        raise OSError(msg)
    return stream


async def _daemon_answers(vsock: Path, port: int) -> bool:
    """True when the guest daemon answers the first word of the worker handshake."""
    try:
        async with await connect_guest(vsock, port) as stream:
            await stream.send(_WORD.pack(_WORKER_MAGIC_1))
            reply = b""
            with anyio.move_on_after(2):
                while len(reply) < _WORD.size:
                    reply += await stream.receive(_WORD.size - len(reply))
    except (OSError, anyio.EndOfStream, anyio.BrokenResourceError):
        return False
    return len(reply) == _WORD.size and _WORD.unpack(reply)[0] == _WORKER_MAGIC_2


async def _set_clock(vsock: Path, port: int) -> None:
    """A loaded guest wakes at the time of the save; it takes the host's epoch."""
    async with await connect_guest(vsock, port) as stream:
        await stream.send(f"{int(time.time())}\n".encode())
        await stream.send_eof()
        reply = b""
        with contextlib.suppress(anyio.EndOfStream):
            while len(reply) < _LINE_MAX:
                reply += await stream.receive()
    if reply.strip() != b"ok":
        msg = f"the guest did not set its clock: {reply!r}"
        raise DarwinBuilderError(msg)


async def _pump(source: ByteStream, sink: ByteStream) -> None:
    with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError, anyio.EndOfStream):
        async for chunk in source:
            await sink.send(chunk)
    # Half-close: the daemon protocol's far side still answers after an EOF.
    with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
        await sink.send_eof()


async def _forward(vsock: Path, port: int, listen: Path, *, task_status: TaskStatus[None]) -> None:
    """Serve the guest's daemon port on *listen*, a socket Nix can open."""
    listener = await anyio.create_unix_listener(listen)

    async def handle(client: SocketStream) -> None:
        async with client:
            try:
                guest = await connect_guest(vsock, port)
            except (OSError, anyio.EndOfStream) as exc:
                logger.warning("pynix darwin builder cannot reach the guest daemon", error=str(exc))
                return
            async with guest, anyio.create_task_group() as tg:
                tg.start_soon(_pump, client, guest)
                tg.start_soon(_pump, guest, client)

    task_status.started()
    await listener.serve(handle)


async def _load(qmp: _Qmp, vm: Process, snapshot: Snapshot) -> None:
    await qmp.call("migrate-set-capabilities", capabilities=_X_IGNORE_SHARED)
    await qmp.call("migrate-incoming", uri=f"file:{snapshot.state}")
    while True:
        status = await qmp.call("query-status")
        if not isinstance(status, dict):
            msg = f"QEMU answered query-status with {status!r}"
            raise DarwinBuilderError(msg)
        if status.get("status") != "inmigrate":
            break
        if vm.returncode is not None:
            msg = f"QEMU exited with status {vm.returncode} while it loaded the snapshot"
            raise DarwinBuilderError(msg)
        await anyio.sleep(0.01)
    if status.get("status") != "paused":
        msg = f"QEMU did not load the snapshot: {status}"
        raise DarwinBuilderError(msg)
    await qmp.call("cont")


async def _wait_for_daemon(vm: Process, vsock: Path, port: int) -> None:
    with anyio.move_on_after(_START_TIMEOUT) as scope:
        while not await _daemon_answers(vsock, port):
            if vm.returncode is not None:
                msg = f"QEMU exited with status {vm.returncode} before the guest daemon answered"
                raise DarwinBuilderError(msg)
            await anyio.sleep(0.02)
    if scope.cancelled_caught:
        msg = f"the guest daemon did not answer within {_START_TIMEOUT:.0f} s"
        raise DarwinBuilderError(msg)


async def _stop(vm: Process, qmp: _Qmp | None) -> None:
    with anyio.CancelScope(shield=True):
        if vm.returncode is None and qmp is not None:
            with (
                anyio.move_on_after(_STOP_TIMEOUT),
                contextlib.suppress(DarwinBuilderError, OSError, anyio.EndOfStream),
            ):
                await qmp.call("quit")
        with anyio.move_on_after(_STOP_TIMEOUT):
            await vm.wait()
            return
        vm.kill()
        await vm.wait()


def requested_snapshot(*, snapshot: str | None, store_named: bool) -> str:
    """The snapshot of a ``--darwin-builder`` command, or exit with what makes it impossible."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        error_exit("--darwin-builder runs on Apple silicon Macs only")
    if store_named:
        error_exit("--darwin-builder builds in the VM's store, so it cannot be combined with --store")
    if snapshot is None:
        error_exit(
            "--darwin-builder needs --darwin-builder-snapshot or PYNIX_DARWIN_BUILDER_SNAPSHOT: "
            "a directory that `darwin_builder.py save` wrote"
        )
    return snapshot


@dataclass(frozen=True)
class DarwinBuilder:
    """A running guest: its daemon as a store URI."""

    store: str
    work: Path


@contextlib.asynccontextmanager
async def darwin_builder(directory: str) -> AsyncIterator[DarwinBuilder]:
    """Start a guest from the snapshot in *directory*, and yield it once its daemon answers."""
    snapshot = await Snapshot.read(directory)
    started = time.monotonic()
    work = Path(await anyio.to_thread.run_sync(lambda: tempfile.mkdtemp(prefix="pynix-db-")))
    try:
        cloned = await anyio.run_process(["/bin/cp", "-c", snapshot.aux, str(work / "aux.img")], check=False)
        if cloned.returncode != 0:
            msg = f"cannot clone {snapshot.aux}: {cloned.stderr.decode(errors='replace').strip()}"
            raise DarwinBuilderError(msg)
        overlay = await anyio.run_process(
            [snapshot.qemu_img, "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", snapshot.overlay,
             str(work / "overlay.qcow2")],
            check=False,
        )  # fmt: skip
        if overlay.returncode != 0:
            msg = f"qemu-img cannot make the overlay: {overlay.stderr.decode(errors='replace').strip()}"
            raise DarwinBuilderError(msg)
        log = await AnyioPath(work / "qemu.log").open("wb")
        vsock = work / "vsock.sock"
        async with (
            log,
            await anyio.open_process(snapshot.qemu_argv(work), stdout=log.wrapped, stderr=log.wrapped) as vm,
        ):
            qmp = None
            try:
                qmp = await _Qmp.connect(work / "qmp.sock", vm)
                await _load(qmp, vm, snapshot)
                await _wait_for_daemon(vm, vsock, snapshot.daemon_vsock_port)
                await _set_clock(vsock, snapshot.clock_vsock_port)
                logger.info(
                    "pynix darwin builder is ready", seconds=round(time.monotonic() - started, 3), work=str(work)
                )
                async with anyio.create_task_group() as tg:
                    socket = work / "daemon.sock"
                    await tg.start(_forward, vsock, snapshot.daemon_vsock_port, socket)
                    yield DarwinBuilder(f"unix://{socket}?local-fs=false", work)
                    tg.cancel_scope.cancel()
            except DarwinBuilderError as exc:
                tail = await _log_tail(work / "qemu.log")
                raise DarwinBuilderError(f"{exc}\n{tail}") from exc
            finally:
                await _stop(vm, qmp)
                if qmp is not None:
                    with anyio.CancelScope(shield=True):
                        await qmp.aclose()
    finally:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(lambda: shutil.rmtree(work, onexc=_log_rmtree_failure))


async def _log_tail(path: Path) -> str:
    try:
        lines = (await AnyioPath(path).read_text(errors="replace")).splitlines()
    except OSError:
        return "(no QEMU log)"
    return "\n".join(lines[-20:])


def _log_rmtree_failure(_function: object, path: str, exc: BaseException) -> None:
    # A leftover file must not replace the error that is on its way out.
    logger.warning("pynix darwin builder cannot remove a file", path=path, error=str(exc))
