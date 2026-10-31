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
import platform
import re
import shlex
import shutil
import socket
import struct
import sys
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import anyio
import structlog
from anyio import Path as AnyioPath
from anyio.abc import ByteStream, Process, SocketListener, SocketStream

from pynix._util import error_exit

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
#: Half the host's memory, at least 8 GiB. 8 GiB with 15 vCPUs ran out:
#: the guest's OOM killer took cc1plus in Nix's LTO build.
_MEMORY_MIB = max(8192, os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // 2 // 1024**2)
_HOST_ALIAS = "pynix-linux-builder"
#: The guest journal goes to console.log, and the work directory stays after exit.
_DEBUG = os.environ.get("PYNIX_LINUX_BUILDER_DEBUG") == "1"
#: "1" or "0" forces nested virtualization on or off instead of asking the host.
_NESTED = os.environ.get("PYNIX_LINUX_BUILDER_NESTED")
#: Apple's floor for nested virtualization: an M3 chip and macOS 15.
_NESTED_CHIP = 3
_NESTED_MACOS = 15
_APPLE_CHIP = re.compile(r"^Apple M(\d+)")


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
    ssh_vsock_port: int
    ssh_host_key: str

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
                ssh_vsock_port=int(data["sshVsockPort"]),
                ssh_host_key=data["sshHostKey"],
            )
        except (OSError, ValueError, KeyError) as exc:
            msg = f"cannot read the Linux builder manifest {path}: {exc}"
            raise LinuxBuilderError(msg) from exc


async def _nested_supported() -> bool:
    """Whether vzvm may ask for nested virtualization here; it refuses to start otherwise.

    Read from the chip name and the macOS version, because Virtualization.framework's
    own answer is an Objective-C class property that Python does not reach.
    """
    if _NESTED is not None:
        return _NESTED == "1"
    macos = platform.mac_ver()[0]
    if not macos or int(macos.split(".")[0]) < _NESTED_MACOS:
        return False
    brand = await anyio.run_process(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], check=False)
    chip = _APPLE_CHIP.match(brand.stdout.decode(errors="replace"))
    return chip is not None and int(chip.group(1)) >= _NESTED_CHIP


def _free_loopback_port() -> int:
    # vzvm binds the port itself. Another process can take it between this
    # close and vzvm's bind; vzvm then refuses to start, and says why.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@dataclass(frozen=True)
class _Plan:
    """One start of the VM: where its files are, and its loopback ports."""

    manifest: Manifest
    work: Path
    daemon_port: int
    rosetta: bool
    #: The guest then has /dev/kvm, and its daemon offers `kvm`.
    nested: bool
    #: The host directory to share at the same path, for ``pynix run``. With
    #: it the guest also lets the run's key log in over ssh.
    home: Path | None
    ssh_port: int

    @property
    def disk(self) -> Path:
        return self.work / "store.img"

    @property
    def keys(self) -> Path:
        return self.work / "keys"

    def vzvm_config(self) -> dict[str, object]:
        forwards = [{"listen": f"127.0.0.1:{self.daemon_port}", "vsockPort": self.manifest.daemon_vsock_port}]
        shares = [{"tag": "hostnix", "path": "/nix"}]
        cmdline = self.manifest.cmdline
        if _DEBUG:
            cmdline += " systemd.log_level=info systemd.journald.forward_to_console=1"
        if self.home is not None:
            forwards.append({"listen": f"127.0.0.1:{self.ssh_port}", "vsockPort": self.manifest.ssh_vsock_port})
            shares += [{"tag": "pynix-keys", "path": str(self.keys)}, {"tag": "pynix-home", "path": str(self.home)}]
            cmdline += f" pynix.home={self.home}"
        return {
            "cpuCount": os.cpu_count() or 1,
            "memorySizeMiB": _MEMORY_MIB,
            "kernel": self.manifest.kernel,
            "initrd": self.manifest.initrd,
            "cmdline": cmdline,
            # vzvm refuses to start when Rosetta is asked for and absent.
            # Without it the guest still builds aarch64-linux.
            "rosetta": self.rosetta,
            "nestedVirtualization": self.nested,
            "console": {"mode": "file", "path": str(self.work / "console.log")},
            "vsock": {"forwards": forwards},
            "disks": [{"path": str(self.disk), "readOnly": False}],
            "shares": shares,
        }


@dataclass(frozen=True)
class LinuxBuilder:
    """A running guest: its daemon as a store URI, and ssh into it for ``pynix run``."""

    store: str
    _plan: _Plan

    def ssh_argv(self, argv: list[str], *, cwd: str, tty: bool) -> list[str]:
        """The ssh command that runs *argv* as root in the guest, in *cwd* when the guest has it."""
        home = self._plan.home
        if home is None:
            msg = "this Linux builder shares no home directory, so it takes no ssh session"
            raise LinuxBuilderError(msg)
        remote = (
            f"cd {shlex.quote(cwd)} 2>/dev/null || cd {shlex.quote(str(home))}; "
            f"export HOME={shlex.quote(str(home))}; exec {shlex.join(argv)}"
        )
        return [
            "/usr/bin/ssh",
            "-i", str(self._plan.keys / "id_ed25519"),
            "-p", str(self._plan.ssh_port),
            "-o", "IdentitiesOnly=yes",
            "-o", f"HostKeyAlias={_HOST_ALIAS}",
            "-o", f"UserKnownHostsFile={self._plan.keys / 'known_hosts'}",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "LogLevel=ERROR",
            "-t" if tty else "-T",
            "root@127.0.0.1",
            "--",
            remote,
        ]  # fmt: skip


async def _stage_keys(plan: _Plan) -> None:
    """A key pair for this run only, and the files each side needs to trust the other."""
    await AnyioPath(plan.keys).mkdir()
    key = plan.keys / "id_ed25519"
    made = await anyio.run_process(
        ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "pynix-linux-builder", "-f", str(key)],
        check=False,
    )
    if made.returncode != 0:
        msg = f"ssh-keygen cannot make {key}: {made.stderr.decode(errors='replace').strip()}"
        raise LinuxBuilderError(msg)
    await AnyioPath(plan.keys / "authorized_keys").write_text(await AnyioPath(f"{key}.pub").read_text())
    await AnyioPath(plan.keys / "known_hosts").write_text(f"{_HOST_ALIAS} {plan.manifest.ssh_host_key}\n")


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


def requested_guest(*, guest: str | None, store_named: bool) -> str:
    """The guest manifest of a ``--linux-builder`` command, or exit with what makes it impossible."""
    if sys.platform != "darwin":
        error_exit("--linux-builder runs on macOS only")
    if store_named:
        error_exit("--linux-builder builds in the VM's store, so it cannot be combined with --store")
    if guest is None:
        error_exit(
            "--linux-builder needs --linux-builder-guest or PYNIX_LINUX_BUILDER_GUEST: "
            "the path that `nix build --file nanopynix internal.linuxBuilder.manifest` prints"
        )
    return guest


@contextlib.asynccontextmanager
async def linux_builder(manifest_path: str, *, home: Path | None = None) -> AsyncIterator[LinuxBuilder]:
    """Start the guest that *manifest_path* names, and yield it once its daemon answers.

    With *home*, the guest mounts that directory at the same path and takes
    ssh sessions. The VM and its directory exist until the context exits.
    """
    manifest = await Manifest.read(manifest_path)
    rosetta = await AnyioPath(_ROSETTA).is_dir()
    if not rosetta:
        logger.warning("pynix linux builder: Rosetta is not installed, so x86_64-linux will not build")
    work = Path(await anyio.to_thread.run_sync(lambda: tempfile.mkdtemp(prefix="pynix-lb-")))
    try:
        plan = _Plan(
            manifest, work, _free_loopback_port(), rosetta, await _nested_supported(), home, _free_loopback_port()
        )
        # ASIF, Apple's sparse format: measured in croshome, 2.5x the random
        # write IOPS of a raw sparse file.
        created = await anyio.run_process(
            ["/usr/sbin/diskutil", "image", "create", "blank", "--fs", "none", "--format", "ASIF",
             "--size", str(_DISK_BYTES), str(plan.disk)],
            check=False,
        )  # fmt: skip
        if created.returncode != 0:
            msg = f"diskutil cannot create {plan.disk}: {created.stderr.decode(errors='replace').strip()}"
            raise LinuxBuilderError(msg)
        if home is not None:
            await _stage_keys(plan)

        config = work / "vzvm.json"
        await AnyioPath(config).write_text(json.dumps(plan.vzvm_config()))

        async def forward(client: SocketStream) -> None:
            await _forward(client, plan.daemon_port)

        async def serve(listener: SocketListener) -> None:
            await listener.serve(forward)

        log = await AnyioPath(work / "vzvm.log").open("wb")
        command = [manifest.vzvm, str(config)]
        async with log, await anyio.open_process(command, stdout=log.wrapped, stderr=log.wrapped) as vm:
            try:
                await _wait_for_daemon(vm, plan.daemon_port, work)
                socket_path = work / "daemon.sock"
                listener = await anyio.create_unix_listener(socket_path)
                async with listener, anyio.create_task_group() as group:
                    group.start_soon(serve, listener)
                    logger.info("pynix linux builder is ready", socket=str(socket_path))
                    yield LinuxBuilder(f"unix://{socket_path}?local-fs=false", plan)
                    group.cancel_scope.cancel()
            finally:
                await _stop(vm)
    finally:
        if _DEBUG:
            logger.warning("pynix linux builder keeps its directory", directory=str(work))
        else:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(lambda: shutil.rmtree(work))
