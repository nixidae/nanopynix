"""The local fast path: one Nix session shared by every pynix command.

Every pynix command opens its own session, store and evaluator -- that is
what the real CLI does, and running it that way is what makes these tests
end-to-end rather than unit tests. It is also the dominant cost of the suite:
measured on an evaluation of a flake importing nixpkgs, a fresh session per
call takes 9.04s, while a reused one takes 7.19s on the first call and
**0.01s** after. That time is re-importing nixpkgs into a brand-new
evaluator, over and over.

So this module rebinds the ``huggorm_*_session`` helpers of ``pynix._util`` to
versions that hand out a shared session, store and evaluator. Commands still
run their real code, still forward logs per invocation, and still see a real
Nix -- they just stop paying for a cold evaluator each time.

**This trades fidelity for speed, and that trade is deliberately not made in
CI.** Setting ``NANOPYNIX_TEST_FAITHFUL_SESSIONS`` restores the per-command
behaviour exactly, and CI sets it: the slow, faithful path is the one that
gates merges, while the fast path is for the local edit-run loop where a
four-minute wait is the thing actually being paid for. What the fast path
cannot catch, by construction, is anything that depends on a command getting a
*fresh* process, store handle or evaluator -- leaked global state, first-call
initialisation order, env read at worker spawn.

A build that names its own ``settings`` falls through to the real
implementation, and the settings it changed are put back after it.
"""

from __future__ import annotations

import contextlib
import pathlib
import sys
import urllib.parse
from collections.abc import AsyncGenerator, Mapping
from typing import Any

import anyio
import huggorm
import pytest

import pynix._util as pynix_util
from pynix._build_monitor import MonitorState
from pynix._verbosity import VerbosityInput

#: Set by CI to run the faithful path -- every command opens its own worker,
#: store and evaluator, exactly as the real CLI does. Unset (the local default)
#: shares them.
FAITHFUL_SESSIONS_ENV_VAR = "NANOPYNIX_TEST_FAITHFUL_SESSIONS"

_PATCHED_NAMES = (
    "huggorm_eval_session",
    "huggorm_store_session",
    "huggorm_build_session",
)


class SharedSessions:
    """Holds the shared session/store/evaluator and the patched helpers."""

    def __init__(self) -> None:
        self._stack = contextlib.AsyncExitStack()
        # Commands are driven sequentially by the tests, but the LSP tests
        # overlap operations, so opening the shared objects has to be atomic.
        self._lock = anyio.Lock()
        self._huggorm: huggorm.AsyncSession | None = None
        # Keyed by _store_key, not by URI alone -- see that function.
        self._huggorm_states: dict[tuple[str, tuple[int, int] | None], tuple[Any, Any]] = {}
        self._huggorm_builds: dict[tuple[Any, Any], tuple[Any, Any, Any]] = {}
        self._originals = {name: getattr(pynix_util, name) for name in _PATCHED_NAMES}

    # ── shared object construction ─────────────────────────────────────

    async def _shared_huggorm(self) -> huggorm.AsyncSession:
        if self._huggorm is None:
            # `load_config=False`, so the `nix.conf` of whoever runs the suite
            # reaches nothing here. `pynix` itself loads it, and must: the real
            # CLI takes what the host configured. A test states what it wants
            # and asserts on the result, and the substituters, the
            # experimental features and the sandbox of a developer machine are
            # none of its business.
            pynix_util.init_nix(load_config=False)
            self._huggorm = await self._stack.enter_async_context(huggorm.AsyncSession())
        return self._huggorm

    async def _shared_build(self, store_uri: str, eval_store_uri: str | None) -> tuple[Any, Any, Any, Any]:
        session = await self._shared_huggorm()
        key = (_store_key(store_uri), None if eval_store_uri is None else _store_key(eval_store_uri))
        triple = self._huggorm_builds.get(key)
        if triple is None:
            build_store = session.store(store_uri)
            if eval_store_uri is None:
                triple = (build_store, build_store, session.eval(build_store))
            else:
                eval_store = session.store(eval_store_uri)
                triple = (build_store, eval_store, session.eval(eval_store, None, build_store))
            self._huggorm_builds[key] = triple
        build_store, eval_store, state = triple
        return session, build_store, eval_store, state

    async def _shared_state(self, store_uri: str) -> tuple[huggorm.AsyncSession, Any, Any]:
        session = await self._shared_huggorm()
        key = _store_key(store_uri)
        pair = self._huggorm_states.get(key)
        if pair is None:
            store = session.store(store_uri)
            pair = (store, session.eval(store))
            self._huggorm_states[key] = pair
        store, state = pair
        return session, store, state

    # ── the patched helpers ────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def huggorm_eval_session(
        self,
        store_uri: str,
        *,
        print_build_logs: bool = False,
    ) -> AsyncGenerator[tuple[Any, Any, Any]]:
        async with self._lock:
            session, store, state = await self._shared_state(store_uri)
        async with pynix_util.forward_state_logs(session, state, print_build_logs=print_build_logs):
            yield session, store, state

    @contextlib.asynccontextmanager
    async def huggorm_store_session(
        self,
        store_uri: str,
        *,
        verbosity: VerbosityInput | None = None,
    ) -> AsyncGenerator[tuple[Any, Any]]:
        async with self._lock:
            session, store, _state = await self._shared_state(store_uri)
        async with pynix_util.forward_state_logs(session, None, verbosity=verbosity):
            yield session, store

    @contextlib.asynccontextmanager
    async def huggorm_build_session(  # noqa: PLR0913 -- mirrors pynix._util.huggorm_build_session
        self,
        store_uri: str,
        *,
        eval_store_uri: str | None = None,
        settings: Mapping[str, str] | None = None,
        verbosity: VerbosityInput | None = None,
        print_build_logs: bool = False,
        monitor: MonitorState | None = None,
    ) -> AsyncGenerator[tuple[Any, Any, Any, Any]]:
        if settings:
            # A setting is a process global, and a shared evaluator was built
            # before it. Put each one back, or every later test inherits it.
            before = {name: huggorm.get_setting(name) for name in settings}
            try:
                async with self._originals["huggorm_build_session"](
                    store_uri,
                    eval_store_uri=eval_store_uri,
                    settings=settings,
                    verbosity=verbosity,
                    print_build_logs=print_build_logs,
                    monitor=monitor,
                ) as quad:
                    yield quad
            finally:
                for name, value in before.items():
                    if value is not None:
                        huggorm.set_setting(name, value)
            return
        async with self._lock:
            session, build_store, eval_store, state = await self._shared_build(store_uri, eval_store_uri)
        async with pynix_util.forward_state_logs(
            session, state, verbosity=verbosity, print_build_logs=print_build_logs, monitor=monitor
        ):
            yield session, build_store, eval_store, state

    # ── installation ───────────────────────────────────────────────────

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rebind the helpers in every pynix module that imported them.

        The command modules use ``from pynix._util import huggorm_store_session``, so
        each holds its own reference and patching ``pynix._util`` alone would
        miss all of them. Sweeping the already-imported ``pynix.*`` modules for
        the original function object catches every current call site and any
        added later, without this file having to list them.
        """
        replacements = {name: getattr(self, name) for name in _PATCHED_NAMES}
        patched = 0
        for module_name, module in list(sys.modules.items()):
            if not module_name.startswith("pynix"):
                continue
            for name in _PATCHED_NAMES:
                current = getattr(module, name, None)
                if current is self._originals[name] or (
                    callable(current) and getattr(current, "__qualname__", "").startswith("SharedSessions.")
                ):
                    monkeypatch.setattr(module, name, replacements[name])
                    patched += 1
        if patched == 0:
            raise RuntimeError(
                "shared-session fast path patched nothing -- no pynix module holds a reference to "
                f"{_PATCHED_NAMES}. Either the command modules were not imported yet, or they stopped "
                f"importing these helpers; set {FAITHFUL_SESSIONS_ENV_VAR} to run without the fast path.",
            )

    async def aclose(self) -> None:
        await self._stack.aclose()


def _store_key(store_uri: str) -> tuple[str, tuple[int, int] | None]:
    """A cache key that survives pytest recycling a temporary root's name.

    ``tmp_path_factory.mktemp("nix-local")`` numbers its directories from the
    highest that currently exists, so a root removed at one test's teardown is
    handed straight back to the next test -- same path, same store URI, brand
    new empty directory. Keying the shared store on the URI alone therefore
    returned the previous test's store handle for a root that no longer had a
    state directory, which is what made ``store verify`` fail on a missing
    ``gc.lock``. The inode distinguishes the two incarnations.
    """
    query = urllib.parse.urlparse(store_uri).query
    roots = urllib.parse.parse_qs(query).get("root")
    if not roots:
        return (store_uri, None)
    try:
        stat = pathlib.Path(roots[0]).stat()
    except OSError:
        # No root on disk yet: the store is about to create it, and the next
        # lookup will see the real inode.
        return (store_uri, None)
    return (store_uri, (stat.st_dev, stat.st_ino))
