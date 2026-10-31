"""A Linux builder VM for the life of one pynix command (nixidae/nixidae#1138).

vzvm boots the guest of ``nix/linux-builder`` on Apple's Virtualization.framework,
which gives the guest Rosetta, so the guest builds x86_64-linux as well as
aarch64-linux. The guest's daemon listens on vsock, vzvm forwards a loopback TCP
port to it, and this module serves a Unix socket in a temporary directory that
forwards each connection to that port.

The store URI carries ``local-fs=false``: the guest's store is an overlay that
the host cannot see, and the default ``unix://`` reads store objects from the
host's own /nix/store. huggorm's Nix carries the patch that adds the setting.

Kept apart from the build command on purpose, so the VM code can move to
another home.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import struct
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import anyio
import structlog
from anyio import Path as AnyioPath
from anyio.abc import ByteStream, Process, SocketStream

logger = structlog.get_logger("pynix.linux_builder")

#: The daemon protocol's unit: a little-endian 64-bit word.
_WORD = struct.Struct("<Q")
_WORKER_MAGIC_1 = 0x6E697863
_WORKER_MAGIC_2 = 0x6478696F
_BOOT_TIMEOUT = 120.0
_STOP_TIMEOUT = 5.0
_ROSETTA = Path("/Library/Apple/usr/libexec/oah")
#: Sparse: an empty ASIF image takes 4 MiB on the host.
_DISK_BYTES = 128 * 1024**3
_MEMORY_MIB = 8192


class LinuxBuilderError(Exception):
    """The Linux builder VM did not start, or its guest does not exist."""


@dataclass(frozen=True)
class Manifest:
    """What ``nix/linux-builder`` writes for one guest."""

    vzvm: str
    kernel: str
    initrd: str
    cmdline: str
    daemon_vsock_port: int

    @classmethod
    async def read(cls, path: str) -> Manifest:
        try:
            data = json.loads(await AnyioPath(path).read_text())
            return cls(
                vzvm=data["vzvm"],
                kernel=data["kernel"],
                initrd=data["initrd"],
                cmdline=data["cmdline"],
                daemon_vsock_port=int(data["daemonVsockPort"]),
            )
        except (OSError, ValueError, KeyError) as exc:
            msg = f"cannot read the Linux builder manifest {path}: {exc}"
            raise LinuxBuilderError(msg) from exc


def _free_loopback_port() -> int:
    # vzvm binds the port itself. Another process can take it between this
    # close and vzvm's bind; vzvm then refuses to start, and says why.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _vzvm_config(manifest: Manifest, work: Path, port: int, disk: Path, *, rosetta: bool) -> dict[str, object]:
    return {
        "cpuCount": os.cpu_count() or 1,
        "memorySizeMiB": _MEMORY_MIB,
        "kernel": manifest.kernel,
        "initrd": manifest.initrd,
        "cmdline": manifest.cmdline,
        # vzvm refuses to start when Rosetta is asked for and absent. Without
        # it the guest still builds aarch64-linux.
        "rosetta": rosetta,
        "console": {"mode": "file", "path": str(work / "console.log")},
        "vsock": {"forwards": [{"listen": f"127.0.0.1:{port}", "vsockPort": manifest.daemon_vsock_port}]},
        "disks": [{"path": str(disk), "readOnly": False}],
        "shares": [{"tag": "hostnix", "path": "/nix"}],
    }


async def _daemon_answers(port: int) -> bool:
    """True when the guest daemon answers the first word of the worker handshake.

    vzvm accepts on the loopback port before the guest listens, and closes the
    connection when the vsock connect fails, so an accepted connection proves
    nothing.
    """
    try:
        async with await anyio.connect_tcp("127.0.0.1", port) as stream:
            await stream.send(_WORD.pack(_WORKER_MAGIC_1))
            reply = b""
            with anyio.move_on_after(2):
                while len(reply) < _WORD.size:
                    reply += await stream.receive(_WORD.size - len(reply))
    except (OSError, anyio.EndOfStream, anyio.BrokenResourceError):
        return False
    return len(reply) == _WORD.size and _WORD.unpack(reply)[0] == _WORKER_MAGIC_2


async def _console_tail(work: Path) -> str:
    try:
        lines = (await AnyioPath(work / "console.log").read_text(errors="replace")).splitlines()
    except OSError:
        return "(no console log)"
    return "\n".join(lines[-20:])


async def _wait_for_daemon(vm: Process, port: int, work: Path) -> None:
    with anyio.move_on_after(_BOOT_TIMEOUT) as scope:
        while not await _daemon_answers(port):
            if vm.returncode is not None:
                msg = f"vzvm exited with status {vm.returncode}\n{await _console_tail(work)}"
                raise LinuxBuilderError(msg)
            await anyio.sleep(0.2)
    if scope.cancelled_caught:
        msg = f"the guest daemon did not answer within {_BOOT_TIMEOUT:.0f} s\n{await _console_tail(work)}"
        raise LinuxBuilderError(msg)


async def _pump(source: ByteStream, sink: ByteStream) -> None:
    # A peer that goes away ends this direction; the other one ends on its own.
    with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
        async for chunk in source:
            await sink.send(chunk)
        await sink.send_eof()


async def _forward(client: SocketStream, port: int) -> None:
    async with client:
        try:
            upstream = await anyio.connect_tcp("127.0.0.1", port)
        except OSError as exc:
            logger.warning("pynix linux builder cannot reach vzvm", error=str(exc))
            return
        async with upstream, anyio.create_task_group() as group:
            group.start_soon(_pump, client, upstream)
            group.start_soon(_pump, upstream, client)


async def _stop(vm: Process) -> None:
    if vm.returncode is not None:
        return
    # SIGTERM makes vzvm power the guest off. The guest keeps nothing.
    vm.terminate()
    with anyio.move_on_after(_STOP_TIMEOUT, shield=True):
        await vm.wait()
        return
    vm.kill()
    with anyio.CancelScope(shield=True):
        await vm.wait()


@contextlib.asynccontextmanager
async def linux_builder(manifest_path: str) -> AsyncIterator[str]:
    """Start the guest that *manifest_path* names, and yield a store URI for its daemon.

    The VM and its directory exist until the context exits.
    """
    manifest = await Manifest.read(manifest_path)
    rosetta = await AnyioPath(_ROSETTA).is_dir()
    if not rosetta:
        logger.warning("pynix linux builder: Rosetta is not installed, so x86_64-linux will not build")
    work = Path(await anyio.to_thread.run_sync(lambda: tempfile.mkdtemp(prefix="pynix-lb-")))
    try:
        disk = work / "store.img"
        # ASIF, Apple's sparse format: measured in croshome, 2.5x the random
        # write IOPS of a raw sparse file.
        created = await anyio.run_process(
            ["/usr/sbin/diskutil", "image", "create", "blank", "--fs", "none", "--format", "ASIF",
             "--size", str(_DISK_BYTES), str(disk)],
            check=False,
        )  # fmt: skip
        if created.returncode != 0:
            msg = f"diskutil cannot create {disk}: {created.stderr.decode(errors='replace').strip()}"
            raise LinuxBuilderError(msg)

        port = _free_loopback_port()
        config = work / "vzvm.json"
        await AnyioPath(config).write_text(json.dumps(_vzvm_config(manifest, work, port, disk, rosetta=rosetta)))

        async def forward(client: SocketStream) -> None:
            await _forward(client, port)

        log = await AnyioPath(work / "vzvm.log").open("wb")
        command = [manifest.vzvm, str(config)]
        async with log, await anyio.open_process(command, stdout=log.wrapped, stderr=log.wrapped) as vm:
            try:
                await _wait_for_daemon(vm, port, work)
                socket_path = work / "daemon.sock"
                listener = await anyio.create_unix_listener(socket_path)
                async with listener, anyio.create_task_group() as group:
                    group.start_soon(listener.serve, forward)
                    logger.info("pynix linux builder is ready", socket=str(socket_path))
                    yield f"unix://{socket_path}?local-fs=false"
                    group.cancel_scope.cancel()
            finally:
                await _stop(vm)
    finally:
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(lambda: shutil.rmtree(work))
