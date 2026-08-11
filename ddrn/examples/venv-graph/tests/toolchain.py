r"""The patched Nix, the private store, and how a test drives a build.

**Every test of this directory drives Nix through nanopynix, and no test runs
the `nix` command.** One :class:`Toolchain` holds one RPC session. That session
evaluates each expression, builds each derivation, and reads each result back,
so a test writes Python where it used to write an argument vector.

**The process that runs pytest has to be the patched build.**
`nanopynix/src/nanopynix/rpc/_worker_argv.py` starts a worker as
`[sys.executable, "-m", nanopynix.rpc.worker]`, so the worker is the build of
the client and the two cannot differ. `nanopynixMaster.shell` is the
development shell of this repository, built against the patched Nix:

```console
$ nix develop --impure --file . nanopynixMaster.shell \\
    --command pytest ddrn/examples/venv-graph/tests
```

`check_toolchain` below refuses a plain `pytest` with that command in the
message.

**The build goes into a private chroot store, and not into the store of the
machine.** The system daemon does not offer `builder-rpc-v0`, so a build that
the daemon runs never reaches the new code path. This session builds in a
directory of its own, and the store directory stays `/nix/store`, so every
dependency substitutes from the binary cache rather than building.

**A chroot store is a plain directory tree, so a test reads it directly.**
`<work>/store/nix/store/<hash>-<name>` is the store object, and
:meth:`Toolchain.cat` opens that file. The `nix store cat` of the old harness
was a second process for something that is one `read_text`.

`../run-uv.sh` sets the same things up for a reader with the command line, and
`../README.md` gives each command.

**The first run builds the patched Nix, and that takes about 20 minutes.** The
work directory survives the run, so a second run builds nothing. `DDRN_WORK`
names that directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from nanopynix.rpc import Session
from nanopynix.settings import NixEvalSettings, NixSettings

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nanopynix.rpc.client._session import EvalSession
    from nanopynix.rpc.client.store import Store

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parent
REPO = EXAMPLE.parents[2]

#: The features that the patched Nix needs to evaluate and to build a graph.
FEATURES = ["nix-command", "flakes", "ca-derivations", "dynamic-derivations"]

#: The feature that a **build** of a planner needs. Nix compares it in
#: `DerivationBuildingGoal::tryToBuild`, which runs only after substitution
#: failed, so a consumer that substitutes every output needs no such feature.
#: These tests build everything, because the store is private and no cache has
#: ever served it.
SYSTEM_FEATURES = ["builder-rpc-v0", "recursive-nix"]

#: How many lines of a failed build Nix puts in its message. The default is
#: 25, and a diagnostic of this example is longer than that. The old command
#: line passed `--log-lines 100` for the same reason.
LOG_LINES = 1000

#: What `builtins.nixVersion` reads on the patched build. `nix/nix-master.nix`
#: gives the suffix, and no released Nix carries it.
PATCH_MARK = "ddnix"

#: The expression that builds the environment of the planner. It is the only
#: thing the private store needs from outside: the client is the patched
#: nanopynix itself, so no `nix` command has to live in that store.
PLANNER_ENV = f'(import {REPO} {{ }}).nanopynixMaster.pythonSet.mkVirtualEnv "ddrn-planner-env" {{ nanopynix = [ ]; }}'

#: `nix/compat.nix` overrides `self` with the local checkout unless this is
#: set, and every CI workflow sets it. A test builds what CI builds.
BUILD_ENV = {"FLAKE_COMPATISH_DISABLE_OVERRIDES": "1"}


class WrongInterpreterError(RuntimeError):
    """The interpreter that runs pytest is not the patched build."""


def nix_string(value: object) -> str:
    """`value` as a Nix string literal.

    A path of this machine reaches an expression as a string, and a string
    needs quotation marks around it. Nothing here escapes a quotation mark,
    because every value is a store path or a path of this repository.
    """
    return f'"{value}"'


class Toolchain:
    """One RPC session over the private store, and the planner environment.

    `run.sh` sets the same things up with the command line, and this class is
    that script as a fixture. The work directory survives the run, so a second
    run of this test builds nothing.
    """

    def __init__(self, work: Path) -> None:
        self.work = work
        self.store_root = work / "store"
        #: The planner environment, as a path of the store of this machine.
        self.env = ""
        self._session: Session | None = None
        self._store: Store | None = None
        self._eval: EvalSession | None = None
        self._host: Store | None = None
        self._host_eval: EvalSession | None = None

    # -- the session ------------------------------------------------------

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("the toolchain is not open; use `async with`")
        return self._session

    @property
    def store(self) -> Store:
        if self._store is None:
            raise RuntimeError("the toolchain is not open; use `async with`")
        return self._store

    @property
    def evaluator(self) -> EvalSession:
        if self._eval is None:
            raise RuntimeError("the toolchain is not open; use `async with`")
        return self._eval

    @property
    def host(self) -> Store:
        """The store of this machine.

        **One check of this example needs no patched Nix, and no private
        store.** `../uv.nix` builds `lockCheck` with `pkgs.uv` alone, so it
        builds here and substitutes what it needs.
        """
        if self._host is None:
            raise RuntimeError("the toolchain is not open; use `async with`")
        return self._host

    @property
    def host_evaluator(self) -> EvalSession:
        if self._host_eval is None:
            raise RuntimeError("the toolchain is not open; use `async with`")
        return self._host_eval

    async def __aenter__(self) -> Toolchain:
        await self.prepare()
        return self

    async def __aexit__(self, *arguments: object) -> None:
        await self.close()

    def _session_for(self, uri: str, *, env: Mapping[str, str] | None = None) -> Session:
        return Session(
            store_uri=uri,
            experimental_features=FEATURES,
            settings=NixSettings(system_features=SYSTEM_FEATURES, log_lines=LOG_LINES),
            env=dict(env) if env else None,
        )

    async def prepare(self) -> None:
        """Build the planner environment, and open the private store.

        **The environment builds against the store of this machine, and the
        graph builds in the private one.** The environment is an ordinary
        derivation, so the daemon of the machine substitutes most of it. The
        graph is not, so nothing but this session can build it.
        """
        self.work.mkdir(parents=True, exist_ok=True)
        self.store_root.mkdir(parents=True, exist_ok=True)

        # A session of its own, because `env` is a property of the worker and
        # the graph session must not carry a variable that changes an
        # evaluation of `../uv.nix`.
        async with (
            self._session_for("auto", env=BUILD_ENV) as builder,
            builder.store() as host,
            builder.eval(host, eval_settings=NixEvalSettings(pure_eval=False)) as evaluator,
        ):
            value = await evaluator.string(PLANNER_ENV)
            drv = await value.attr("drvPath").as_string()
            failure = await self._realise(builder, host, f"{drv}^*")
            if failure:
                raise RuntimeError(f"the planner environment did not build:\n{failure}")
            self.env = (await host.query_derivation_outputs(drv))[0]

            # `check_sigs=False` is needed and is safe. The environment was
            # built on this machine, so no cache signed it, and a chroot store
            # refuses an unsigned path. The store is a private directory of
            # this test.
            async with builder.store(str(self.store_root)) as private:
                await host.copy_closure([self.env], private, check_sigs=False)

        self._session = self._session_for(str(self.store_root))
        await self._session.open()
        self._store = self._session.store()
        await self._store.open()
        self._eval = self._session.eval(self._store, eval_settings=NixEvalSettings(pure_eval=False))
        await self._eval.__aenter__()

        # The second store of the same session, for the one check that needs
        # no patched Nix. Its evaluator writes into that store, so nothing of
        # `pkgs.uv` reaches the private one.
        self._host = self._session.store("auto")
        await self._host.open()
        self._host_eval = self._session.eval(self._host, eval_settings=NixEvalSettings(pure_eval=False))
        await self._host_eval.__aenter__()

    async def close(self) -> None:
        for evaluator in (self._host_eval, self._eval):
            if evaluator is not None:
                await evaluator.__aexit__(None, None, None)
        self._host_eval = None
        self._eval = None
        for store in (self._host, self._store):
            if store is not None:
                await store.close()
        self._host = None
        self._store = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    # -- expressions ------------------------------------------------------

    def source(self, expression: Path, attribute: str | None = None, /, **arguments: str) -> str:
        """One file of the example, applied to `arguments`.

        **Each value is Nix source, and not a Python value.** A test states
        `application='["ws-cli"]'` the way it stated `--arg application
        '["ws-cli"]'`, and :func:`nix_string` makes a path into a literal.

        `nanopynixEnv` goes on every call, because every expression of this
        directory takes it.
        """
        given = {"nanopynixEnv": nix_string(self.env), **arguments}
        body = " ".join(f"{name} = {value};" for name, value in given.items())
        applied = f"(import {expression} {{ {body} }})"
        return applied if attribute is None else f"{applied}.{attribute}"

    async def evaluate(self, expression: str, *, evaluator: EvalSession | None = None) -> str:
        """Evaluate `expression`, and give back the string it names."""
        chosen = evaluator if evaluator is not None else self.evaluator
        return await (await chosen.string(expression)).as_string()

    async def evaluate_failing(self, expression: str) -> str:
        """Evaluate `expression`, which must fail, and give back the message."""
        try:
            await self.evaluate(expression)
        except Exception as error:
            return str(error)
        raise AssertionError(f"this expression was expected to fail, and it evaluated:\n{expression}")

    # -- builds -----------------------------------------------------------

    async def _realise(self, session: Session, store: Store, derived: str) -> str:
        """Build `derived`, and give back what went wrong.

        An empty string means that the build succeeded. **A failed build
        raises nothing here**: `build_paths_with_results` reports the failure
        in the result, so this reads `success` rather than catch an exception.

        **The session records every log event of the build.** That is what
        `--print-build-logs` gave the old command line, and it is the only way
        to read the output of a derivation that a planner submitted: such a
        derivation has no name that a caller knows, so nothing can ask
        `get_build_log` for it. `log_lines` of :data:`NIX_SETTINGS` decides how
        much of the log Nix puts in the message.
        """
        async with session.capture_logs() as capture:
            results = await store.build_paths_with_results([derived])
        broken = [result for result in results if not result.success]
        if not broken:
            return ""
        spoken = [event.message_without_ansi for event in capture.events if event.message_without_ansi]
        return "\n".join([*(result.error_msg for result in broken), *spoken])

    async def drv_of(
        self,
        expression: Path,
        attribute: str | None = None,
        /,
        *,
        evaluator: EvalSession | None = None,
        **arguments: str,
    ) -> str:
        """The derivation that `expression` evaluates to, and no build."""
        source = f"{self.source(expression, attribute, **arguments)}.drvPath"
        return await self.evaluate(source, evaluator=evaluator)

    async def build(self, expression: Path, attribute: str | None = None, /, **arguments: str) -> str:
        """Build `expression` in the private store, and give back its output."""
        return await self._build(self.store, self.evaluator, expression, attribute, **arguments)

    async def build_on_host(self, expression: Path, attribute: str | None = None, /, **arguments: str) -> str:
        """Build `expression` in the store of this machine."""
        return await self._build(self.host, self.host_evaluator, expression, attribute, **arguments)

    async def build_failing(self, expression: Path, attribute: str | None = None, /, **arguments: str) -> str:
        """Build `expression` in the private store, which must fail."""
        return await self._build_failing(self.store, self.evaluator, expression, attribute, **arguments)

    async def build_on_host_failing(self, expression: Path, attribute: str | None = None, /, **arguments: str) -> str:
        """Build `expression` in the store of this machine, which must fail."""
        return await self._build_failing(self.host, self.host_evaluator, expression, attribute, **arguments)

    async def _build(
        self,
        store: Store,
        evaluator: EvalSession,
        expression: Path,
        attribute: str | None,
        /,
        **arguments: str,
    ) -> str:
        drv = await self.drv_of(expression, attribute, evaluator=evaluator, **arguments)
        failure = await self._realise(self.session, store, f"{drv}^*")
        if failure:
            raise AssertionError(f"{drv} was expected to build, and it failed:\n{failure}")
        return (await store.query_derivation_outputs(drv))[0]

    async def _build_failing(
        self,
        store: Store,
        evaluator: EvalSession,
        expression: Path,
        attribute: str | None,
        /,
        **arguments: str,
    ) -> str:
        """Build what must fail, and give back what it printed.

        **The message of the failure is the thing under test.** A refusal that
        happens during evaluation raises, and a refusal that happens during the
        build does not, so this reads both.
        """
        try:
            drv = await self.drv_of(expression, attribute, evaluator=evaluator, **arguments)
        except Exception as error:
            return str(error)
        failure = await self._realise(self.session, store, f"{drv}^*")
        if not failure:
            raise AssertionError(f"{drv} was expected to fail, and it built")
        return failure

    async def build_graph_failing(self, expression: Path, planner: str, /, **arguments: str) -> str:
        """Realise the graph that `planner` submits, which must fail.

        **`builtins.outputOf` gives no derivation to evaluate.** It gives a
        string that holds a placeholder, so `.drvPath` of it is a type error
        and :meth:`build_failing` cannot take it. The deriving path says the
        same thing, and Nix reads it: `<planner>^out` is the derivation that
        the planner wrote, and `<planner>^out^out` is the output of **that**
        derivation. `ddrn/NIX-FACTS.md` records the syntax.
        """
        source = f"{self.source(expression, planner, **arguments)}.drvPath"
        drv = await self.evaluate(source)
        failure = await self._realise(self.session, self.store, f"{drv}^out^out")
        if not failure:
            raise AssertionError(f"the graph of {drv} was expected to fail, and it built")
        return failure

    # -- reading the private store ----------------------------------------

    def path_in_store(self, path: str) -> Path:
        """Where the private store keeps the object that `path` names.

        A chroot store puts every object under its own prefix, and the logical
        path stays `/nix/store/...`. So this joins the two.
        """
        return self.store_root / path.lstrip("/")

    def cat(self, path: str) -> str:
        """The content of the store object that `path` names."""
        return self.path_in_store(path).read_text(encoding="utf-8")

    def list_files(self, path: str) -> list[str]:
        """Every entry under `path`, relative to it, sorted.

        A symbolic link is one entry and this does not follow it, so an
        application that holds one link gives one entry.
        """
        root = self.path_in_store(path)
        if root.is_symlink() or root.is_file():
            return []
        found = [entry.relative_to(root).as_posix() for entry in root.rglob("*")]
        return sorted(found)

    def link_target(self, path: str) -> str:
        """What the store object that `path` names points at."""
        return str(self.path_in_store(path).readlink())


async def check_toolchain() -> None:
    """Refuse a run whose interpreter is not the patched build.

    A plain `pytest` runs the development shell of the pinned Nix, and that
    build has no `builder-rpc-v0` and no `dynamic-derivations`. It fails deep
    inside a build, so this reads the version first and names the command.
    """
    async with (
        Session(experimental_features=FEATURES) as session,
        session.store() as store,
        session.eval(store) as evaluator,
    ):
        version = await (await evaluator.string("builtins.nixVersion")).as_string()
    if PATCH_MARK not in version:
        raise WrongInterpreterError(
            f"nanopynix here links Nix {version}, and these tests need the patched build. "
            "Run them in the development shell of that build:\n"
            "  nix develop --impure --file . nanopynixMaster.shell "
            "--command pytest ddrn/examples/venv-graph/tests"
        )


def environment_of(report: str) -> str:
    """The store path that a report names under its `environment` heading.

    `../tests/sealed.nix` and `../check-application.nix` write that heading,
    and the path is the line under it.
    """
    lines = report.splitlines()
    return lines[lines.index("environment") + 1].strip()


def prefix_of(report: str) -> str:
    """The store path that a report names on its `prefix` line.

    `../check-uv.nix`, `../check-packaged.nix` and `../check-workspace.nix`
    write one labelled line instead of a heading. The label is the name of the
    environment that the check ran, and it is the same store path.
    """
    for line in report.splitlines():
        if line.startswith("prefix"):
            return line.split(maxsplit=1)[1].strip()
    raise AssertionError(f"the report names no prefix:\n{report}")


def work_directory(name: str) -> Path:
    """Where the patched Nix and the private store live, between runs.

    `DDRN_WORK` names one directory for every module, so a machine that has
    built the patched Nix once never builds it again.

    **This does not read `TMPDIR`, and an earlier version did.** `nix develop`
    sets `TMPDIR` to a directory of its own for each invocation, so every run
    under it took an empty store, copied the environment again, and built every
    graph from nothing. It also left 1.6 GB behind for each run. The default
    below is one directory, whatever starts the run.
    """
    named = os.environ.get("DDRN_WORK")
    if named:
        return Path(named)
    return Path("/tmp") / name
