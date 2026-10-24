# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportPrivateUsage=false, reportUnknownLambdaType=false, reportUnknownArgumentType=false
# conftest.py interacts heavily with nanopynix/pynix C++ nanobind extensions
# that lack type stubs.  Member types, variable types, private imports,
# unknown argument types, and lambdas without type context are inherent to the
# test infrastructure.
"""What this suite needs, and the scenario objects that only it uses.

The stores come from ``huggorm.testing``. This file adds what only this suite
uses: ``repo_root``, ``nixpkgs_path`` and the shared sessions.

``support/`` is a package inside the suite because nothing outside the suite
reads it. The language-server fixtures that used to be re-exported here moved
to ``pynix-lsp/tests/conftest.py`` with the server itself, in issue #107.

Every other plugin, beartype's import hook included, is named with ``-p`` in
``pynix/pytest.ini``. That file gives the reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import io
import os
import signal
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import structlog

# Session scope, and anyio's own `anyio_backend` is module scope. Both are
# global plugins now that `pytest.ini` registers ours with `-p`, and anyio
# wins that tie -- every session-scoped fixture that requests it then fails
# with `ScopeMismatch`. A conftest beats any plugin, so importing the fixture
# here settles it.
from huggorm.testing.environment import NixTestEnvironment, anyio_backend as anyio_backend

import pynix._util as pynix_util
from _shared_sessions import FAITHFUL_SESSIONS_ENV_VAR, SharedSessions
from support.store_scenario import (
    CURRENT_PYNIX_TEST,
    PynixLiveLog,
    PynixStoreScenario,
    pynix_configure_logging_noop,
)
from test_support.subprocess_output import run_process


@pytest.fixture(scope="session")
def pynix_live_log(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[PynixLiveLog]:
    log_dir = tmp_path_factory.mktemp("pynix-live-logs")
    stack_file: io.TextIOWrapper = (log_dir / "pynix-manager-stacks.log").open("a")
    with contextlib.suppress(RuntimeError, ValueError):
        faulthandler.register(signal.SIGUSR2, file=stack_file, all_threads=True)
    log = PynixLiveLog(path=log_dir / "pynix-structlog.jsonl", stack_path=log_dir / "pynix-manager-stacks.log")
    terminal = request.config.pluginmanager.get_plugin("terminalreporter")
    if terminal is not None:
        terminal.write_line("")
        terminal.write_line("================================================================")
        terminal.write_line(f"PYNIX LIVE STRUCTLOG JSONL: {log.path}")
        terminal.write_line(f"tail -f {log.path}")
        terminal.write_line(f"PYNIX MANAGER PID: {os.getpid()}")
        terminal.write_line(f"PYNIX MANAGER STACKS: {log.stack_path}")
        terminal.write_line(f"kill -USR2 {os.getpid()}")
        terminal.write_line("================================================================")
    yield log
    with contextlib.suppress(RuntimeError, ValueError):
        faulthandler.unregister(signal.SIGUSR2)
    stack_file.close()


@pytest.fixture(scope="module", autouse=True)
def _host_nix_conf_stays_out() -> Iterator[None]:  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    """Keep the host's ``nix.conf`` out of the pytest process.

    Every pynix command loads it, and these tests run the commands in this
    process, so the first one put the host's substituters, sandbox and
    builders into every test after it. Module scope, so a module-scoped
    fixture that runs a command is covered as well.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pynix_util, "_config_loaded", True)
        yield


@pytest.fixture(scope="module", autouse=True)
async def _shared_pynix_sessions(  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    anyio_backend: str,  # noqa: ARG001 -- see below; requested for its side effect on the fixture closure
) -> AsyncIterator[None]:
    """Share one Nix session/store/evaluator across pynix commands, unless CI
    asked for the faithful per-command path -- see _shared_sessions.py beside this file.

    `anyio_backend` is unused in the body and load-bearing anyway. anyio's
    plugin only wraps an async fixture when `anyio_backend` is in the
    *requesting test's* fixture closure (`pytest_fixture_setup`:
    `if "anyio_backend" in request.fixturenames`). An async autouse fixture at
    session scope is therefore fine right up until the first test to reach it
    is a sync one, at which point pytest is handed a raw async generator and
    errors out -- and every later test in the run collapses behind it with an
    internal `assert not self._finalizers`.

    Which test comes first depends on selection, so this passed for `pytest
    tests` and failed for `pytest pynix/tests/test_repl.py`, whose first test
    is sync: exactly backwards from what makes a bug easy to find. Naming the
    fixture here pulls it into every test's closure and settles it.
    """
    if os.environ.get(FAITHFUL_SESSIONS_ENV_VAR):
        yield
        return
    shared = SharedSessions()
    with pytest.MonkeyPatch.context() as monkeypatch:
        shared.install(monkeypatch)
        try:
            yield
        finally:
            await shared.aclose()


@pytest.fixture(autouse=True)
def _capture_pynix_test_structlog(  # type: ignore[reportUnusedFunction] -- pytest autouse fixture, wired by pytest
    request: pytest.FixtureRequest,
    pynix_live_log: PynixLiveLog,
) -> Iterator[None]:
    test_name = request.node.nodeid
    old_config = structlog.get_config()
    token = CURRENT_PYNIX_TEST.set(test_name)
    pynix_live_log.append({"event": "pytest test start", "test": test_name})
    with pynix_configure_logging_noop():
        structlog.configure(processors=[pynix_live_log.capture])
        try:
            yield
        finally:
            pynix_live_log.append({"event": "pytest test finish", "test": test_name})
            structlog.configure(**old_config)
            CURRENT_PYNIX_TEST.reset(token)


@pytest.fixture(scope="session")
def repo_root(pytestconfig: pytest.Config) -> Path:
    """The checkout root, found from the rootdir: this file may run from a venv copy."""
    for candidate in (pytestconfig.rootpath, *pytestconfig.rootpath.parents):
        if (candidate / "flake.nix").is_file():
            return candidate
    raise RuntimeError(f"no flake.nix at or above the pytest rootdir {pytestconfig.rootpath}")


@pytest.fixture(scope="module")
async def nixpkgs_path(repo_root: Path) -> str:
    """The pinned nixpkgs, realised once per module."""
    result = await run_process(["nix", "eval", "--impure", "--raw", "--file", str(repo_root), "pkgs.path"])
    if result.returncode != 0:
        raise RuntimeError(f"nix eval of pkgs.path {result.describe()}")
    return result.stdout.strip()


@pytest.fixture(scope="module")
async def pynix_store_scenario(  # noqa: PLR0913 -- tracked complexity/arg-count debt, see TODO.md
    request: pytest.FixtureRequest,
    repo_root: Path,
    nixpkgs_path: str,
    pynix_live_log: PynixLiveLog,
    shared_nix_environment: NixTestEnvironment,
    tmp_path_factory: pytest.TempPathFactory,
) -> PynixStoreScenario:
    work_root = tmp_path_factory.mktemp("pynix-work")
    scenario = PynixStoreScenario(
        store_url=shared_nix_environment.store_uri,
        environment=shared_nix_environment,
        work_root=work_root,
        repo_root=repo_root,
        nixpkgs_path=nixpkgs_path,
        live_log=pynix_live_log,
    )
    terminal = request.config.pluginmanager.get_plugin("terminalreporter")
    if terminal is not None:
        terminal.write_line(f"pynix scenario structlog: {scenario.log_path}")
    # **The work root stays, and that is what stops the next module reusing its
    # name.** `mktemp` numbers from the highest directory that exists, so a
    # `shutil.rmtree` here handed `pynix-work0` straight back to the next
    # module. Nix 2.35 keeps a path cache for the life of the process, so the
    # second module wrote a new file at the old path and the evaluation
    # answered "path ... does not exist" for a file that was on disk. Issue
    # #274 measured it: `pytest pynix/tests/test_log.py
    # pynix/tests/test_real_store_scenario.py` failed on 2.35 and passed on
    # 2.34, and the probe reported `exists=true` for the path Nix denied.
    #
    # `tmp_path_factory` removes all but the last few roots of earlier runs, so
    # nothing accumulates. The directory holds a handful of small files.
    return scenario


@pytest.fixture(scope="module")
async def populated_store(pynix_store_scenario: PynixStoreScenario) -> dict[str, str]:
    scenario = pynix_store_scenario
    if scenario.hello_path is None:
        await scenario.build_hello(test_name="populated_store:build_hello")
    if scenario.text_path is None:
        await scenario.add_text_file(test_name="populated_store:add_text_file")
    if scenario.local_log_path is None:
        await scenario.build_local_log_derivation(test_name="populated_store:build_local_log_derivation")
    return {
        "store_url": scenario.store_url,
        "hello_path": scenario.require_hello_path(),
        "text_path": scenario.require_text_path(),
        "log_path": scenario.require_local_log_path(),
    }


@pytest.fixture
async def git_flake(nixpkgs_path: str) -> AsyncIterator[Path]:
    with tempfile.TemporaryDirectory() as d:
        flake_dir = Path(d)
        (flake_dir / "flake.nix").write_text(f"""
        {{
          inputs.nixpkgs.url = "path:{nixpkgs_path}";
          outputs = {{ nixpkgs, ... }}:
          let
            system = builtins.currentSystem;
            pkgs = nixpkgs.legacyPackages.${{system}};
          in
          {{
            hello = pkgs.stdenvNoCC.mkDerivation {{
              pname = "test-hello";
              version = "1";
              dontUnpack = true;
              installPhase = ''
                echo hi > "$out"
              '';
            }};
            greeting = "hi";
          }};
        }}
        """)
        for args in (
            ["git", "init"],
            ["git", "add", "flake.nix"],
            ["git", "commit", "-m", "init"],
        ):
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=flake_dir,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        yield flake_dir
