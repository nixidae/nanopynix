"""The implementation of ``pynix flake check``.

A port of ``CmdFlakeCheck`` in ``src/nix/flake.cc`` (2.34): the same check
for each output, the same messages, and the same two error modes. Without
``keep-going`` the first failure ends the command, with each enclosing
"while checking" frame on it. With ``keep-going`` each failure prints with
its innermost frame, and the command fails at the end.

Two parts of Nix's output are absent. A frame names no position, because no
huggorm value answers the position of an attribute. The outputs come in
name order, and Nix's come in symbol order, so without ``keep-going`` the
first failure can be another one than Nix's.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Final

import anyio
import huggorm
import structlog
from huggorm import AsyncEvalState, AsyncStore, AsyncValue
from huggorm.attrpath import AttrPathError, flake_outputs, select_attr
from huggorm.errors import NixError
from rich.text import Text

from pynix._evaluate import is_derivation
from pynix._util import configured_nix_settings, error_console, huggorm_build_session, read_only_mode
from pynix.flake import Check

logger = structlog.get_logger("pynix.flake")

_ERROR: Final = "\x1b[31;1merror:\x1b[0m"
_INDENT: Final = " " * 7
_IFD: Final = "allow-import-from-derivation"

_DEPRECATED: Final = {
    "defaultPackage": "packages.<system>.default",
    "defaultApp": "apps.<system>.default",
    "defaultTemplate": "templates.default",
    "defaultBundler": "bundlers.<system>.default",
    "overlay": "overlays.default",
    "devShell": "devShells.<system>.default",
    "nixosModule": "nixosModules.default",
}

#: Known community outputs that Nix checks nothing in.
_UNCHECKED: Final = frozenset(
    {
        "lib",
        "darwinConfigurations",
        "darwinModules",
        "flakeModule",
        "flakeModules",
        "herculesCI",
        "homeConfigurations",
        "homeModule",
        "homeModules",
        "nixopsConfigurations",
    }
)

#: ``showType`` of ``src/libexpr/eval.cc``, by ``AsyncValue.type_name``.
_SHOW_TYPE: Final = {
    "int": "an integer",
    "float": "a float",
    "bool": "a Boolean",
    "string": "a string",
    "path": "a path",
    "null": "null",
    "attrs": "a set",
    "list": "a list",
    "function": "a function",
    "external": "an external value",
}


def _quote(name: str) -> str:
    """*name* as Nix's ``HintFmt`` puts an argument: quoted and magenta."""
    return f"'\x1b[35;1m{name}\x1b[0m'"


class CheckError(Exception):
    """A failed check, in Nix's words, with the frames around it."""

    def __init__(self, colored: str) -> None:
        super().__init__(huggorm.filter_ansi_escapes(colored, filter_all=True))
        self.colored = colored
        self.hints: list[str] = []

    @classmethod
    def raised(cls, message: str) -> CheckError:
        """An error that ``CmdFlakeCheck`` itself raises."""
        return cls(f"{_ERROR} {message}")

    @classmethod
    def of(cls, exc: NixError | CheckError) -> CheckError:
        return exc if isinstance(exc, CheckError) else cls(exc.colored)

    def add_trace(self, hint: str) -> None:
        """Put *hint* outside the frames this error has, as ``addTrace`` does."""
        self.hints.insert(0, hint)

    def render(self) -> str:
        """The error as Nix prints it: ``error:``, the frames, then the message."""
        if not self.hints:
            return self.colored
        frames = "".join(f"{_INDENT}… {hint}\n\n" for hint in self.hints)
        head, _, rest = self.colored.partition("\n")
        if huggorm.filter_ansi_escapes(head, filter_all=True) == "error:":
            # Nix's own frames follow a bare head: ours go in front of them.
            return f"{head}\n{frames}{rest}"
        return f"{_ERROR}\n{frames}{_INDENT}{self.colored}"


def _print_error(error: CheckError) -> None:
    _print(error.render())


def _print(colored: str) -> None:
    # `soft_wrap`: a build log in the message keeps Nix's own line breaks.
    error_console.print(Text.from_ansi(colored), soft_wrap=True)


def _arg_has_name(name: str, expected: str) -> bool:
    return name in (expected, "_") or (name.startswith("_") and name[1:] == expected)


async def _show_type(value: AsyncValue) -> str:
    kind = await value.type_name()
    if kind == "function" and await value.is_primop():
        return "a built-in function"
    if kind == "function" and await value.is_primop_app():
        return "a partially applied built-in function"
    return _SHOW_TYPE.get(kind, kind)


@dataclass
class _Checker:
    state: AsyncEvalState
    store: AsyncStore
    keep_going: bool
    all_systems: bool
    local_system: str
    has_errors: bool = False
    omitted_systems: set[str] = field(default_factory=set)
    #: Each check to build on this system, with every attribute path that names it.
    attr_paths_by_drv: dict[str, list[str]] = field(default_factory=dict)

    def report(self, error: CheckError) -> None:
        """``reportError``: raise, or under ``keep-going`` print and go on."""
        if not self.keep_going:
            raise error
        _print_error(error)
        self.has_errors = True

    @contextmanager
    def checking(self, hint: str) -> Iterator[None]:
        """Report what fails inside, with *hint* as its outermost frame."""
        try:
            yield
        except (NixError, CheckError) as exc:
            error = CheckError.of(exc)
            error.add_trace(hint)
            self.report(error)

    async def force_attrs(self, value: AsyncValue) -> None:
        await self.state.force(value)
        if await value.type_name() != "attrs":
            raise CheckError.raised(f"expected a set but found {await _show_type(value)}")

    async def force_string_no_context(self, value: AsyncValue) -> None:
        await self.state.force(value)
        if await value.type_name() != "string":
            raise CheckError.raised(f"expected a string but found {await _show_type(value)}")
        context = await value.string_context()
        if context:
            raise CheckError.raised(
                f"the string {_quote(str(await value.string_value()))} is not allowed to refer to a store path"
                f" (such as {_quote(context[0])})"
            )

    async def members(self, value: AsyncValue) -> list[tuple[str, AsyncValue]]:
        await self.force_attrs(value)
        return [(name, await value.get(name)) for name in await value.names()]

    def check_system_name(self, system: str) -> None:
        # Nix adds ", at <position>"; huggorm answers no attribute's position.
        if "-" not in system:
            self.report(CheckError.raised(f"{_quote(system)} is not a valid system type"))

    def check_system_type(self, system: str) -> bool:
        if not self.all_systems and system != self.local_system:
            self.omitted_systems.add(system)
            return False
        return True

    async def systems(self, value: AsyncValue) -> list[tuple[str, AsyncValue]]:
        """The members of a per-system output that this run checks."""
        checked: list[tuple[str, AsyncValue]] = []
        for system, member in await self.members(value):
            self.check_system_name(system)
            if self.check_system_type(system):
                checked.append((system, member))
        return checked

    async def derivation(self, attr_path: str, value: AsyncValue) -> str | None:
        logger.info("pynix flake check: checking derivation", attr=attr_path)
        with self.checking(f"while checking the derivation {_quote(attr_path)}"):
            if not await is_derivation(self.state, value):
                raise CheckError.raised(f"flake attribute {_quote(attr_path)} is not a derivation")
            drv_path = await self.store.print_store_path(await value.drv_path())
            logger.info("pynix flake check: derivation evaluated", drv_path=drv_path)
            return drv_path
        return None

    async def app(self, attr_path: str, value: AsyncValue) -> None:
        logger.info("pynix flake check: checking app", attr=attr_path)
        with self.checking(f"while checking the app definition {_quote(attr_path)}"):
            await self.force_attrs(value)
            if not await value.has("type"):
                raise CheckError.raised(f"app {_quote(attr_path)} lacks attribute {_quote('type')}")
            await self.force_string_no_context(await value.get("type"))
            if not await value.has("program"):
                raise CheckError.raised(f"app {_quote(attr_path)} lacks attribute {_quote('program')}")
            program = await value.get("program")
            await self.state.force(program)
            if await program.type_name() != "string":
                raise CheckError.raised(f"expected a string but found {await _show_type(program)}")
            if await value.has("meta"):
                meta = await value.get("meta")
                await self.force_attrs(meta)
                if await meta.has("description"):
                    await self.force_string_no_context(await meta.get("description"))
                else:
                    logger.warning("app lacks attribute 'meta.description'", app=attr_path)
            else:
                logger.warning("app lacks attribute 'meta'", app=attr_path)
            for name in await value.names():
                if name not in ("type", "program", "meta"):
                    raise CheckError.raised(f"app {_quote(attr_path)} has unsupported attribute {_quote(name)}")

    async def overlay(self, attr_path: str, value: AsyncValue) -> None:
        logger.info("pynix flake check: checking overlay", attr=attr_path)
        with self.checking(f"while checking the overlay {_quote(attr_path)}"):
            await self.state.force(value)
            if await value.type_name() != "function" or not await value.is_lambda():
                raise CheckError.raised(f"overlay is not a function, but {await _show_type(value)} instead")
            if await value.has_formals() or not _arg_has_name(await value.lambda_arg(), "final"):
                raise CheckError.raised("overlay does not take an argument named 'final'")

    async def module(self, attr_path: str, value: AsyncValue) -> None:
        logger.info("pynix flake check: checking NixOS module", attr=attr_path)
        with self.checking(f"while checking the NixOS module {_quote(attr_path)}"):
            await self.state.force(value)

    async def hydra_jobs(self, attr_path: str, value: AsyncValue) -> None:
        logger.info("pynix flake check: checking Hydra job", attr=attr_path)
        with self.checking(f"while checking the Hydra jobset {_quote(attr_path)}"):
            await self.force_attrs(value)
            if await is_derivation(self.state, value):
                raise CheckError.raised("jobset should not be a derivation at top-level")
            for name, member in await self.members(value):
                await self.force_attrs(member)
                child_path = f"{attr_path}.{name}"
                if await is_derivation(self.state, member):
                    await self.derivation(child_path, member)
                else:
                    await self.hydra_jobs(child_path, member)

    async def nixos_configuration(self, attr_path: str, value: AsyncValue) -> None:
        logger.info("pynix flake check: checking NixOS configuration", attr=attr_path)
        with self.checking(f"while checking the NixOS configuration {_quote(attr_path)}"):
            try:
                toplevel = await select_attr(self.state, value, "config.system.build.toplevel")
            except AttrPathError as exc:
                raise CheckError.raised(str(exc)) from exc
            if not await is_derivation(self.state, toplevel):
                raise CheckError.raised("attribute 'config.system.build.toplevel' is not a derivation")

    async def template(self, attr_path: str, value: AsyncValue) -> None:
        logger.info("pynix flake check: checking template", attr=attr_path)
        with self.checking(f"while checking the template {_quote(attr_path)}"):
            await self.force_attrs(value)
            if not await value.has("path"):
                raise CheckError.raised(f"template {_quote(attr_path)} lacks attribute {_quote('path')}")
            path = await self.template_path(await value.get("path"))
            if not await anyio.Path(path).exists():
                raise CheckError.raised(f"template {_quote(attr_path)} refers to a non-existent path {_quote(path)}")
            if not await value.has("description"):
                raise CheckError.raised(f"template {_quote(attr_path)} lacks attribute {_quote('description')}")
            await self.force_string_no_context(await value.get("description"))
            for name in await value.names():
                if name not in ("path", "description", "welcomeText"):
                    raise CheckError.raised(f"template {_quote(attr_path)} has unsupported attribute {_quote(name)}")

    async def template_path(self, value: AsyncValue) -> str:
        """Where a template's ``path`` points, copied nowhere, as ``coerceToPath`` reads it."""
        await self.state.force(value)
        if await value.type_name() == "path":
            path: str = json.loads(await value.to_json())
            return path
        return await value.coerce_to_string()

    async def bundler(self, attr_path: str, value: AsyncValue) -> None:
        logger.info("pynix flake check: checking bundler", attr=attr_path)
        # Nix's frame says "the template" here (flake.cc:574), a copy of
        # the frame above; this one names what it checks.
        with self.checking(f"while checking the bundler {_quote(attr_path)}"):
            await self.state.force(value)
            if await value.type_name() != "function" or not await value.is_lambda():
                raise CheckError.raised("bundler must be a function")

    async def output(self, name: str, value: AsyncValue) -> None:  # noqa: C901, PLR0912 -- one branch per output, as flake.cc has
        logger.info("pynix flake check: checking flake output", attr=name)
        with self.checking(f"while checking flake output {_quote(name)}"):
            await self.state.force(value)
            if name in _DEPRECATED:
                logger.warning("flake output attribute is deprecated", output=name, replacement=_DEPRECATED[name])
            if name == "checks":
                for system, per_system in await self.systems(value):
                    for check, member in await self.members(per_system):
                        attr_path = f"{name}.{system}.{check}"
                        drv_path = await self.derivation(attr_path, member)
                        if drv_path is not None and system == self.local_system:
                            self.attr_paths_by_drv.setdefault(drv_path, []).append(attr_path)
            elif name in ("formatter", "defaultPackage", "devShell"):
                for system, member in await self.systems(value):
                    await self.derivation(f"{name}.{system}", member)
            elif name in ("packages", "devShells"):
                for system, per_system in await self.systems(value):
                    for package, member in await self.members(per_system):
                        await self.derivation(f"{name}.{system}.{package}", member)
            elif name == "apps":
                for system, per_system in await self.systems(value):
                    for app, member in await self.members(per_system):
                        await self.app(f"{name}.{system}.{app}", member)
            elif name == "defaultApp":
                for system, member in await self.systems(value):
                    await self.app(f"{name}.{system}", member)
            elif name == "legacyPackages":
                for system, _member in await self.members(value):
                    self.check_system_name(system)
                    self.check_system_type(system)
            elif name == "overlay":
                await self.overlay(name, value)
            elif name == "overlays":
                for overlay, member in await self.members(value):
                    await self.overlay(f"{name}.{overlay}", member)
            elif name == "nixosModule":
                await self.module(name, value)
            elif name == "nixosModules":
                for module, member in await self.members(value):
                    await self.module(f"{name}.{module}", member)
            elif name == "nixosConfigurations":
                for configuration, member in await self.members(value):
                    await self.nixos_configuration(f"{name}.{configuration}", member)
            elif name == "hydraJobs":
                await self.hydra_jobs(name, value)
            elif name == "defaultTemplate":
                await self.template(name, value)
            elif name == "templates":
                for template, member in await self.members(value):
                    await self.template(f"{name}.{template}", member)
            elif name == "defaultBundler":
                for system, member in await self.systems(value):
                    await self.bundler(f"{name}.{system}", member)
            elif name == "bundlers":
                for system, per_system in await self.systems(value):
                    for bundler, member in await self.members(per_system):
                        await self.bundler(f"{name}.{system}.{bundler}", member)
            elif name not in _UNCHECKED:
                logger.warning("unknown flake output", output=name)

    async def outputs(self, outputs: AsyncValue) -> None:
        """Each output, ``hydraJobs`` first, as ``enumerateOutputs`` orders them."""
        names = await outputs.names()
        # `enableImportFromDerivation.setDefault(name != "hydraJobs")`
        # (flake.cc:600): a default, so a setting someone gave wins.
        ifd_given = _IFD in huggorm.list_settings(overridden_only=True)
        for name in sorted(names, key=lambda name: name != "hydraJobs"):
            if not ifd_given:
                await self.state.set_setting(_IFD, "true" if name != "hydraJobs" else "false")
            await self.output(name, await outputs.get(name))

    async def build(self) -> None:
        """Build what the checks need that no substituter has, and report each failure."""
        store = self.store
        targets = [await store.parse_derived_path(f"{drv}^*") for drv in self.attr_paths_by_drv]
        missing = await store.query_missing(targets)
        to_build = sorted([await store.print_store_path(path) for path in missing.will_build()])
        logger.info("pynix flake check: running flake checks", count=len(to_build))
        if not to_build:
            return
        results = await store.build_paths_with_results([await store.parse_derived_path(f"{drv}^*") for drv in to_build])
        for drv_path, result in zip(to_build, results, strict=True):
            failure = result.error()
            if failure is None:
                continue
            built = f"{drv_path}^*"
            # libstore renders the failure with Nix's "error: " head, and the
            # message goes inside ours.
            message = failure.message.removeprefix("error: ")
            attr_paths = self.attr_paths_by_drv.get(drv_path)
            if not attr_paths:
                self.report(CheckError.raised(f"build of {_quote(built)} failed: {message}"))
            for attr_path in attr_paths or ():
                self.report(
                    CheckError.raised(
                        f"failed to build attribute {_quote(attr_path)}, build of {_quote(built)} failed: {message}"
                    )
                )


async def run_check(command: Check) -> None:
    """The body of :meth:`pynix.flake.Check.run`."""
    settings = configured_nix_settings(keep_going="true" if command.keep_going else None)
    async with huggorm_build_session(command.store, settings=settings, verbosity=command.verbosity, pure=True) as (
        _session,
        store,
        _eval_store,
        state,
    ):
        with read_only_mode(command.no_build):
            await _check(command, store, state)


async def _check(command: Check, store: AsyncStore, state: AsyncEvalState) -> None:
    system = huggorm.get_setting("system")
    if system is None:
        raise RuntimeError("Nix has no 'system' setting")
    checker = _Checker(
        state=state,
        store=store,
        keep_going=huggorm.get_setting("keep-going") == "true",
        all_systems=command.all_systems,
        local_system=system,
    )
    # `CmdFlakeCheck` sets `applyNixConfig`, and keeps the default
    # `writeLockFile` of every flake command.
    locked = await state.lock_flake(huggorm.parse_flake_ref(command.flake_ref), apply_nix_config=True)
    try:
        logger.info("pynix flake check: evaluating flake")
        outputs = await flake_outputs(state, await state.call_flake(locked))
    finally:
        await locked.aclose()
    try:
        await checker.outputs(outputs)
        if not command.no_build and checker.attr_paths_by_drv:
            await checker.build()
    except CheckError as error:
        _print_error(error)
        raise SystemExit(1) from error
    if checker.has_errors:
        _print(f"{_ERROR} some errors were encountered during the evaluation")
        raise SystemExit(1)
    logger.info("pynix flake check: all checks passed")
    if checker.omitted_systems:
        logger.warning(
            "the check omitted these incompatible systems; use --all-systems to check all",
            systems=sorted(checker.omitted_systems),
        )
