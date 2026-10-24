# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportPrivateUsage=false, reportUnknownLambdaType=false, reportUnknownArgumentType=false
# structlog and the huggorm test environment give untyped values, and the
# scenario reads private pynix state on purpose, as conftest.py does.
"""The store scenario of the pynix suite, and the log it writes.

``conftest.py`` builds these as fixtures; a test imports the types from here.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import uuid
from collections.abc import Generator
from contextlib import redirect_stderr, redirect_stdout
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

import structlog

# Session scope, and anyio's own `anyio_backend` is module scope. Both are
# global plugins now that `pytest.ini` registers ours with `-p`, and anyio
# wins that tie -- every session-scoped fixture that requests it then fails
# with `ScopeMismatch`. A conftest beats any plugin, so importing the fixture
# here settles it. `nanopynix/tests/conftest.py` carries the same import and
# the full account.
from huggorm.testing.environment import NixTestEnvironment, anyio_backend as anyio_backend
from structlog.exceptions import DropEvent
from structlog.typing import EventDict, WrappedLogger

import pynix._util as pynix_util
from pynix import parse

CURRENT_PYNIX_TEST: ContextVar[str] = ContextVar("CURRENT_PYNIX_TEST", default="unknown")


@dataclass
class PynixLiveLog:
    path: Path
    stack_path: Path

    def __post_init__(self) -> None:
        self.entries_by_test: dict[str, list[dict[str, object]]] = {}

    def append(self, record: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(record, sort_keys=True, ensure_ascii=False))
            f.write("\n")

    def entries_for(self, test_name: str) -> list[dict[str, object]]:
        return self.entries_by_test.setdefault(test_name, [])

    def capture(self, _logger: WrappedLogger, method_name: str, event_dict: EventDict) -> NoReturn:
        test_name = CURRENT_PYNIX_TEST.get()
        entry = dict(event_dict)
        entry["log_level"] = _structlog_method_name_to_level(method_name)
        self.entries_for(test_name).append(entry)
        self.append({"test": test_name, **entry})
        raise DropEvent


@dataclass
class PynixStoreScenario:
    store_url: str
    environment: NixTestEnvironment
    work_root: Path
    repo_root: Path
    nixpkgs_path: str
    live_log: PynixLiveLog
    last_stdout: str = ""
    last_stderr: str = ""
    last_logs: list[dict[str, object]] | None = None
    current_system: str | None = None
    hello_path: str | None = None
    text_path: str | None = None
    local_log_path: str | None = None
    local_log_stderr: str = ""
    nixpkgs_hello_path: str | None = None
    nixpkgs_hello_unfree_path: str | None = None
    flake_hello_path: str | None = None

    @property
    def store_root(self) -> Path:
        return self.environment.root

    @property
    def log_path(self) -> Path:
        return self.live_log.path

    async def run_pynix(self, args: list[str], *, test_name: str = "unknown") -> tuple[str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        self._append_log_record({"event": "pynix command start", "test": test_name, "args": args})
        before = len(self.live_log.entries_for(test_name))
        with (
            _patched_environ({"NIX_PATH": f"nixpkgs={self.nixpkgs_path}"}),
            pynix_configure_logging_noop(),
            _pynix_test_context(test_name),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            cmd = parse(args)
            # Parsing may configure structlog through Pynix's public re-export.
            # Reinstall the test collector before the command starts forwarding
            # Nix logs, so JSON command output remains machine-readable.
            structlog.configure(processors=[self.live_log.capture])
            await cmd.run()
        self.last_stdout = stdout.getvalue()
        self.last_stderr = stderr.getvalue()
        self.last_logs = [dict(entry) for entry in self.live_log.entries_for(test_name)[before:]]
        self._append_log_record(
            {
                "event": "pynix command finish",
                "test": test_name,
                "args": args,
                "stdout_bytes": len(self.last_stdout),
                "stderr_bytes": len(self.last_stderr),
            },
        )
        return self.last_stdout, self.last_stderr

    async def run_pynix_json(self, args: list[str], *, test_name: str = "unknown") -> object:
        stdout, _stderr = await self.run_pynix(args, test_name=test_name)
        return json.loads(stdout)

    async def get_store_dirs(self, *, test_name: str = "unknown") -> dict[str, object]:
        data = await self.run_pynix_json(["store", "dirs", "--store", self.store_url], test_name=test_name)
        if not isinstance(data, dict):
            raise TypeError("store dirs must produce an object")
        return data

    async def assert_temp_store_dirs(self, *, test_name: str = "unknown") -> dict[str, object]:
        dirs = await self.get_store_dirs(test_name=test_name)
        if not self.environment.relocated:
            # `NANOPYNIX_TEST_SYSTEM_STORE` opened the store of the machine, so
            # there is no temporary layout to name. What stays worth asserting
            # is the property that mode exists for: the store is not diverted,
            # so `storeDir` is the directory on disk and a build there needs no
            # chroot. The layout of that store belongs to the machine, and this
            # suite must not assert what it is.
            if dirs.get("rootDir") is not None:
                raise AssertionError(f"the store of the machine has no root directory; got {dirs.get('rootDir')!r}")
            if dirs.get("realStoreDir") != dirs.get("storeDir"):
                raise AssertionError(
                    f"expected an undiverted store, got storeDir={dirs.get('storeDir')!r} "
                    f"realStoreDir={dirs.get('realStoreDir')!r}"
                )
            return dirs
        expected = {
            "storeDir": "/nix/store",
            "rootDir": str(self.store_root),
            "stateDir": str(self.store_root / "nix" / "var" / "nix"),
            "logDir": str(self.store_root / "nix" / "var" / "log" / "nix"),
            "realStoreDir": str(self.store_root / "nix" / "store"),
            # A native daemon owns builds in its connection process, so its
            # RemoteStore does not expose a local build directory.
            "buildDir": (
                str(self.store_root / "nix" / "var" / "nix" / "builds") if self.environment.backend == "local" else None
            ),
        }
        for key, value in expected.items():
            if dirs.get(key) != value:
                raise AssertionError(f"expected store dir {key}={value!r}, got {dirs.get(key)!r}; all dirs={dirs!r}")
        uri = dirs.get("uri")
        if self.environment.backend == "local":
            uri_ok = isinstance(uri, str) and uri.startswith("local://")
        else:
            # An open unix:// store may report back as the bare "daemon"
            # shorthand (Nix's own collapse for a store whose socket matches
            # the process's default) or drop its query params on older Nix
            # versions -- see NixTestEnvironment.store_uri_matches.
            uri_ok = isinstance(uri, str) and (uri == "daemon" or uri.startswith(("daemon?", "unix://")))
        if not uri_ok:
            raise AssertionError(f"expected a {self.environment.backend} store uri, got {uri!r}; all dirs={dirs!r}")
        return dirs

    async def add_text_file(self, contents: str = "temporary-store-message\n", *, test_name: str = "unknown") -> str:
        source = self.work_root / "message.txt"
        source.write_text(contents)
        data = await self.run_pynix_json(
            ["store", "add-file", str(source), "--store", self.store_url],
            test_name=test_name,
        )
        if not isinstance(data, dict):
            raise TypeError("store add-file must produce an object")
        self.text_path = _require_str(data, "path")
        return self.text_path

    async def get_current_system(self, *, test_name: str = "unknown") -> str:
        data = await self.run_pynix_json(["config", "current-system"], test_name=test_name)
        if not isinstance(data, dict):
            raise TypeError("config current-system must produce an object")
        self.current_system = _require_str(data, "currentSystem")
        return self.current_system

    async def build_hello(self, *, test_name: str = "unknown") -> str:
        source = self.work_root / "hello-source"
        bin_dir = source / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        hello = bin_dir / "hello"
        hello.write_text("#!/bin/sh\necho hello\n")
        hello.chmod(0o755)
        data = await self.run_pynix_json(
            [
                "store",
                "add-path",
                str(source),
                "--name",
                "pynix-scenario-hello",
                "--store",
                self.store_url,
            ],
            test_name=test_name,
        )
        if not isinstance(data, dict):
            raise TypeError("store add-path must produce an object")
        self.hello_path = _require_str(data, "path")
        return self.hello_path

    async def build_local_log_derivation(self, *, test_name: str = "unknown") -> str:
        # Nonce keeps this derivation's store path unique per call. The shared
        # session-scoped store means an earlier test/fixture may have already
        # realized an identical derivation, which would make this build a
        # cache hit with no build log to assert on.
        nonce = uuid.uuid4().hex
        log_nix_file = self.work_root / "log-test.nix"
        log_nix_file.write_text(f"""
        let
          pkgs = import <nixpkgs> {{}};
        in
        pkgs.stdenvNoCC.mkDerivation {{
          pname = "pynix-log-test";
          version = "1";
          dontUnpack = true;
          NANOPYNIX_TEST_NONCE = "{nonce}";
          installPhase = ''
            echo pynix-log-line >&2
            echo log-output > "$out"
          '';
        }}
        """)
        data = await self.run_pynix_json(
            [
                "build",
                "--file",
                str(log_nix_file),
                "--store",
                self.store_url,
                "--verbosity",
                "6",
                "--print-build-logs",
            ],
            test_name=test_name,
        )
        if not isinstance(data, dict):
            raise TypeError("build must produce an object")
        self.local_log_path = _require_output(data, "out")
        self.local_log_stderr = self.last_stderr
        return self.local_log_path

    async def build_nixpkgs_package(
        self,
        attrpath: str,
        *,
        test_name: str = "unknown",
        verbosity: str = "0",
    ) -> str:
        if attrpath == "hello-unfree":
            pkgs_nix_file = self.work_root / "pkgs-allow-unfree-default.nix"
            pkgs_nix_file.write_text("""
            { pkgs ? import <nixpkgs> { config.allowUnfree = true; } }:
            pkgs
            """)
            build_attrpath = attrpath
        else:
            pkgs_nix_file = self.repo_root / "default.nix"
            build_attrpath = f"pkgs.{attrpath}"
        data = await self.run_pynix_json(
            [
                "build",
                "--file",
                str(pkgs_nix_file),
                "--attr",
                build_attrpath,
                "--store",
                self.store_url,
                "--verbosity",
                verbosity,
                "--print-build-logs",
            ],
            test_name=test_name,
        )
        if not isinstance(data, dict):
            raise TypeError("build must produce an object")
        output = _require_output(data, "out")
        if attrpath == "hello":
            self.nixpkgs_hello_path = output
        elif attrpath == "hello-unfree":
            self.nixpkgs_hello_unfree_path = output
        return output

    async def build_flake_hello(self, *, test_name: str = "unknown") -> str:
        # **The pinned nixpkgs, and not this repository.** The subject is
        # `pynix build --flake`, and the reference named `repo_root` only
        # because the old flake re-exported every nixpkgs attribute as
        # `legacyPackages`. The curated surface dropped that, so the reference
        # asked for an attribute no output holds. It also asked a read-only
        # store path to lock a flake that has two inputs and no committed
        # lock, which failed before the attribute was ever looked up. nixpkgs
        # has no input to lock, `base_attr_search` finds `hello` under its
        # `legacyPackages`, and the path is realised already.
        flake_ref = f"{self.nixpkgs_path}#hello"
        data = await self.run_pynix_json(
            [
                "build",
                "--flake",
                flake_ref,
                "--store",
                self.store_url,
                "--verbosity",
                "0",
                "--print-build-logs",
            ],
            test_name=test_name,
        )
        if not isinstance(data, dict):
            raise TypeError("build must produce an object")
        self.flake_hello_path = _require_output(data, "out")
        return self.flake_hello_path

    def require_hello_path(self) -> str:
        return _require_present(self.hello_path, "hello_path")

    def require_text_path(self) -> str:
        return _require_present(self.text_path, "text_path")

    def require_local_log_path(self) -> str:
        return _require_present(self.local_log_path, "local_log_path")

    def require_nixpkgs_hello_path(self) -> str:
        return _require_present(self.nixpkgs_hello_path, "nixpkgs_hello_path")

    def require_nixpkgs_hello_unfree_path(self) -> str:
        return _require_present(self.nixpkgs_hello_unfree_path, "nixpkgs_hello_unfree_path")

    def require_flake_hello_path(self) -> str:
        return _require_present(self.flake_hello_path, "flake_hello_path")

    def physical_path(self, store_path: str) -> Path:
        return self.environment.physical_path(store_path)

    def _append_log_record(self, record: dict[str, object]) -> None:
        self.live_log.append(record)


@contextlib.contextmanager
def _patched_environ(values: dict[str, str]) -> Generator[None]:
    old_values: dict[str, str | None] = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old_value in old_values.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


@contextlib.contextmanager
def pynix_configure_logging_noop() -> Generator[None]:
    # One name, not two. `pynix/__init__.py` used to import `configure_logging`
    # so that `main` could call it before `parse()`; it now calls
    # `pynix._impl.main.prepare()` after the parse instead, and the package no
    # longer binds the name. Issue #123.
    old_configure_logging = pynix_util.configure_logging
    pynix_util.configure_logging = lambda *, file=None, verbosity=None: None  # noqa: ARG005 -- matching configure_logging's real signature so callers like forward_state_logs(log_file=...) don't break
    try:
        yield
    finally:
        pynix_util.configure_logging = old_configure_logging


@contextlib.contextmanager
def _pynix_test_context(test_name: str) -> Generator[None]:
    token = CURRENT_PYNIX_TEST.set(test_name)
    try:
        yield
    finally:
        CURRENT_PYNIX_TEST.reset(token)


def _structlog_method_name_to_level(method_name: str) -> str:
    if method_name == "exception":
        return "error"
    if method_name == "warn":
        return "warning"
    return method_name


def _require_str(data: dict[str, object], key: str) -> str:
    value = data[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _require_output(data: dict[str, object], output_name: str) -> str:
    outputs = data["outputs"]
    if not isinstance(outputs, dict):
        raise TypeError("outputs must be an object")
    value = outputs[output_name]
    if not isinstance(value, str):
        raise TypeError(f"outputs.{output_name} must be a string")
    return value


def _require_present(value: str | None, field_name: str) -> str:
    if value is None:
        raise AssertionError(f"scenario field {field_name} was not populated by its dependency")
    return value
