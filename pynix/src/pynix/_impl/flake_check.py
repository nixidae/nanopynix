"""The implementation of ``pynix flake check``.

A port of ``CmdFlakeCheck`` in ``src/nix/flake.cc`` (2.34): the same check
for each output, the same messages, and the same two error modes. Without
``keep-going`` the first failure ends the command, with each enclosing
"while checking" frame on it. With ``keep-going`` each failure prints with
its innermost frame, and the command fails at the end.

Each frame goes on Nix's own error, at the position Nix gives it, and Nix
renders the result, so a failure prints as Nix prints it. Each set is
walked in symbol order, as Nix's loops walk it, so without ``keep-going``
the check stops at Nix's first failure.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final

import anyio
import huggorm
import structlog
from huggorm import AsyncEvalState, AsyncNativeError, AsyncSourcePos, AsyncStore, AsyncValue, Position
from huggorm.attrpath import AttrPathError, flake_outputs, select_attr
from huggorm.errors import NixError
from rich.text import Text

from pynix._evaluate import is_derivation
from pynix._util import (
    configured_nix_settings,
    create_out_links,
    error_console,
    error_exit,
    huggorm_build_session,
    read_only_mode,
)
from pynix.flake import Check

logger = structlog.get_logger("pynix.flake")

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


def _literal(text: str) -> str:
    """*text* as a format string that formats to itself."""
    return text.replace("%", "%%")


class CheckError(Exception):
    """A failed check: Nix's own error, with the frames around it."""

    def __init__(self, native: AsyncNativeError) -> None:
        super().__init__("a flake check failed")
        self.native = native

    @classmethod
    def raised(cls, format_string: str, *args: str) -> CheckError:
        """``Error(format_string, args...)``, as ``CmdFlakeCheck`` raises one."""
        return cls(AsyncNativeError(format_string, list(args)))

    @classmethod
    def of(cls, exc: NixError | CheckError) -> CheckError:
        if isinstance(exc, CheckError):
            return exc
        # An error has no handle only when it crossed a wire, and a check
        # evaluates in this process.
        if not isinstance(native := exc.native, AsyncNativeError):
            raise TypeError(f"a Nix error came without Nix's own error: {exc}") from exc
        return cls(native)

    async def add_trace(self, pos: AsyncSourcePos | None, format_string: str, *args: str) -> None:
        """Put a frame outside the frames this error has, as ``addTrace`` does."""
        self.native = await self.native.add_trace(pos, format_string, list(args))


@dataclass(frozen=True)
class _At:
    """Where a checked value is: its name in the set that holds it. Nix
    puts a frame at ``attr.pos``, and a position is looked up only for a
    frame."""

    parent: AsyncValue
    name: str

    async def pos(self) -> AsyncSourcePos | None:
        return await self.parent.attr_source_pos(self.name)


def _print(colored: str) -> None:
    # `soft_wrap`: a build log in the message keeps Nix's own line breaks.
    error_console.print(Text.from_ansi(colored), soft_wrap=True)


def _show_position(pos: Position | None) -> str:
    """A position as ``Pos::print`` writes it with its origin (``position.cc:64``)."""
    if pos is None:
        return "«none»:0"
    column = f":{pos.column()}" if pos.column() > 0 else ""
    return f"{pos.file()}:{pos.line()}{column}"


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
    #: Build every check and keep its outputs, for ``--out-link``.
    keep_outputs: bool = False
    #: The outputs of each built check, by output name, in derivation path order.
    built: list[dict[str, str]] = field(default_factory=list)

    async def print_error(self, error: CheckError) -> None:
        """``logError(e.info())``: Nix renders the error."""
        _print(await self.state.render_error(error.native, show_trace=huggorm.get_setting("show-trace") == "true"))

    async def report(self, error: CheckError) -> None:
        """``reportError``: raise, or under ``keep-going`` print and go on."""
        if not self.keep_going:
            raise error
        await self.print_error(error)
        self.has_errors = True

    @asynccontextmanager
    async def checking(self, at: _At, format_string: str, *args: str) -> AsyncIterator[None]:
        """Report what fails inside, with a frame at *at* as its outermost."""
        try:
            yield
        except (NixError, CheckError) as exc:
            error = CheckError.of(exc)
            await error.add_trace(await at.pos(), format_string, *args)
            await self.report(error)

    async def force_attrs(self, value: AsyncValue) -> None:
        await self.state.force(value)
        if await value.type_name() != "attrs":
            raise CheckError.raised("expected a set but found %s", await _show_type(value))

    async def force_string_no_context(self, value: AsyncValue) -> None:
        await self.state.force(value)
        if await value.type_name() != "string":
            raise CheckError.raised("expected a string but found %s", await _show_type(value))
        context = await value.string_context()
        if context:
            raise CheckError.raised(
                "the string '%s' is not allowed to refer to a store path (such as '%s')",
                str(await value.string_value()),
                context[0],
            )

    async def members(self, value: AsyncValue) -> list[tuple[str, AsyncValue]]:
        await self.force_attrs(value)
        return [(name, await value.get(name)) for name in await value.names_in_symbol_order()]

    async def check_system_name(self, parent: AsyncValue, system: str) -> None:
        if "-" not in system:
            where = _show_position(await parent.attr_pos(system))
            await self.report(CheckError.raised("'%s' is not a valid system type, at %s", system, where))

    def check_system_type(self, system: str) -> bool:
        if not self.all_systems and system != self.local_system:
            self.omitted_systems.add(system)
            return False
        return True

    async def systems(self, value: AsyncValue) -> list[tuple[str, AsyncValue]]:
        """The members of a per-system output that this run checks."""
        checked: list[tuple[str, AsyncValue]] = []
        for system, member in await self.members(value):
            await self.check_system_name(value, system)
            if self.check_system_type(system):
                checked.append((system, member))
        return checked

    async def derivation(self, attr_path: str, value: AsyncValue, at: _At) -> str | None:
        logger.info("pynix flake check: checking derivation", attr=attr_path)
        async with self.checking(at, "while checking the derivation '%s'", attr_path):
            if not await is_derivation(self.state, value):
                raise CheckError.raised("flake attribute '%s' is not a derivation", attr_path)
            drv_path = await self.store.print_store_path(await value.drv_path())
            logger.info("pynix flake check: derivation evaluated", drv_path=drv_path)
            return drv_path
        return None

    async def app(self, attr_path: str, value: AsyncValue, at: _At) -> None:
        logger.info("pynix flake check: checking app", attr=attr_path)
        async with self.checking(at, "while checking the app definition '%s'", attr_path):
            await self.force_attrs(value)
            if not await value.has("type"):
                raise CheckError.raised("app '%s' lacks attribute 'type'", attr_path)
            await self.force_string_no_context(await value.get("type"))
            if not await value.has("program"):
                raise CheckError.raised("app '%s' lacks attribute 'program'", attr_path)
            program = await value.get("program")
            await self.state.force(program)
            if await program.type_name() != "string":
                raise CheckError.raised("expected a string but found %s", await _show_type(program))
            if await value.has("meta"):
                meta = await value.get("meta")
                await self.force_attrs(meta)
                if await meta.has("description"):
                    await self.force_string_no_context(await meta.get("description"))
                else:
                    logger.warning("app lacks attribute 'meta.description'", app=attr_path)
            else:
                logger.warning("app lacks attribute 'meta'", app=attr_path)
            for name in await value.names_in_symbol_order():
                if name not in ("type", "program", "meta"):
                    raise CheckError.raised("app '%s' has unsupported attribute '%s'", attr_path, name)

    async def overlay(self, attr_path: str, value: AsyncValue, at: _At) -> None:
        logger.info("pynix flake check: checking overlay", attr=attr_path)
        async with self.checking(at, "while checking the overlay '%s'", attr_path):
            await self.state.force(value)
            if await value.type_name() != "function" or not await value.is_lambda():
                raise CheckError.raised("overlay is not a function, but %s instead", await _show_type(value))
            if await value.has_formals() or not _arg_has_name(await value.lambda_arg(), "final"):
                raise CheckError.raised("overlay does not take an argument named 'final'")

    async def module(self, attr_path: str, value: AsyncValue, at: _At) -> None:
        logger.info("pynix flake check: checking NixOS module", attr=attr_path)
        async with self.checking(at, "while checking the NixOS module '%s'", attr_path):
            await self.state.force(value)

    async def hydra_jobs(self, attr_path: str, value: AsyncValue, at: _At) -> None:
        logger.info("pynix flake check: checking Hydra job", attr=attr_path)
        async with self.checking(at, "while checking the Hydra jobset '%s'", attr_path):
            await self.force_attrs(value)
            if await is_derivation(self.state, value):
                raise CheckError.raised("jobset should not be a derivation at top-level")
            for name, member in await self.members(value):
                await self.force_attrs(member)
                child_path = f"{attr_path}.{name}"
                if await is_derivation(self.state, member):
                    await self.derivation(child_path, member, _At(value, name))
                else:
                    await self.hydra_jobs(child_path, member, _At(value, name))

    async def nixos_configuration(self, attr_path: str, value: AsyncValue, at: _At) -> None:
        logger.info("pynix flake check: checking NixOS configuration", attr=attr_path)
        async with self.checking(at, "while checking the NixOS configuration '%s'", attr_path):
            try:
                toplevel = await select_attr(self.state, value, "config.system.build.toplevel")
            except AttrPathError as exc:
                raise CheckError.raised(_literal(str(exc))) from exc
            if not await is_derivation(self.state, toplevel):
                raise CheckError.raised("attribute 'config.system.build.toplevel' is not a derivation")

    async def template(self, attr_path: str, value: AsyncValue, at: _At) -> None:
        logger.info("pynix flake check: checking template", attr=attr_path)
        async with self.checking(at, "while checking the template '%s'", attr_path):
            await self.force_attrs(value)
            if not await value.has("path"):
                raise CheckError.raised("template '%s' lacks attribute 'path'", attr_path)
            path = await self.template_path(await value.get("path"))
            if not await anyio.Path(path).exists():
                raise CheckError.raised("template '%s' refers to a non-existent path '%s'", attr_path, path)
            if not await value.has("description"):
                raise CheckError.raised("template '%s' lacks attribute 'description'", attr_path)
            await self.force_string_no_context(await value.get("description"))
            for name in await value.names_in_symbol_order():
                if name not in ("path", "description", "welcomeText"):
                    raise CheckError.raised("template '%s' has unsupported attribute '%s'", attr_path, name)

    async def template_path(self, value: AsyncValue) -> str:
        """Where a template's ``path`` points, copied nowhere, as ``coerceToPath`` reads it."""
        await self.state.force(value)
        if await value.type_name() == "path":
            path: str = json.loads(await value.to_json())
            return path
        return await value.coerce_to_string()

    async def bundler(self, attr_path: str, value: AsyncValue, at: _At) -> None:
        logger.info("pynix flake check: checking bundler", attr=attr_path)
        # Nix's frame says "the template" here (flake.cc:574), a copy of
        # the frame above; this one names what it checks.
        async with self.checking(at, "while checking the bundler '%s'", attr_path):
            await self.state.force(value)
            if await value.type_name() != "function" or not await value.is_lambda():
                raise CheckError.raised("bundler must be a function")

    async def output(self, name: str, value: AsyncValue, at: _At) -> None:  # noqa: C901, PLR0912 -- one branch per output, as flake.cc has
        logger.info("pynix flake check: checking flake output", attr=name)
        async with self.checking(at, "while checking flake output '%s'", name):
            await self.state.force(value)
            if name in _DEPRECATED:
                logger.warning("flake output attribute is deprecated", output=name, replacement=_DEPRECATED[name])
            if name == "checks":
                for system, per_system in await self.systems(value):
                    for check, member in await self.members(per_system):
                        attr_path = f"{name}.{system}.{check}"
                        drv_path = await self.derivation(attr_path, member, _At(per_system, check))
                        if drv_path is not None and system == self.local_system:
                            self.attr_paths_by_drv.setdefault(drv_path, []).append(attr_path)
            elif name in ("formatter", "defaultPackage", "devShell"):
                for system, member in await self.systems(value):
                    await self.derivation(f"{name}.{system}", member, _At(value, system))
            elif name in ("packages", "devShells"):
                for system, per_system in await self.systems(value):
                    for package, member in await self.members(per_system):
                        await self.derivation(f"{name}.{system}.{package}", member, _At(per_system, package))
            elif name == "apps":
                for system, per_system in await self.systems(value):
                    for app, member in await self.members(per_system):
                        await self.app(f"{name}.{system}.{app}", member, _At(per_system, app))
            elif name == "defaultApp":
                for system, member in await self.systems(value):
                    await self.app(f"{name}.{system}", member, _At(value, system))
            elif name == "legacyPackages":
                for system, _member in await self.members(value):
                    await self.check_system_name(value, system)
                    self.check_system_type(system)
            elif name == "overlay":
                await self.overlay(name, value, at)
            elif name == "overlays":
                for overlay, member in await self.members(value):
                    await self.overlay(f"{name}.{overlay}", member, _At(value, overlay))
            elif name == "nixosModule":
                await self.module(name, value, at)
            elif name == "nixosModules":
                for module, member in await self.members(value):
                    await self.module(f"{name}.{module}", member, _At(value, module))
            elif name == "nixosConfigurations":
                for configuration, member in await self.members(value):
                    await self.nixos_configuration(f"{name}.{configuration}", member, _At(value, configuration))
            elif name == "hydraJobs":
                await self.hydra_jobs(name, value, at)
            elif name == "defaultTemplate":
                await self.template(name, value, at)
            elif name == "templates":
                for template, member in await self.members(value):
                    await self.template(f"{name}.{template}", member, _At(value, template))
            elif name == "defaultBundler":
                for system, member in await self.systems(value):
                    await self.bundler(f"{name}.{system}", member, _At(value, system))
            elif name == "bundlers":
                for system, per_system in await self.systems(value):
                    for bundler, member in await self.members(per_system):
                        await self.bundler(f"{name}.{system}.{bundler}", member, _At(per_system, bundler))
            elif name not in _UNCHECKED:
                logger.warning("unknown flake output", output=name)

    async def outputs(self, outputs: AsyncValue) -> None:
        """Each output, ``hydraJobs`` first, as ``enumerateOutputs`` orders them.

        ``sorted`` is stable, so the rest keep their symbol order."""
        names = await outputs.names_in_symbol_order()
        # `enableImportFromDerivation.setDefault(name != "hydraJobs")`
        # (flake.cc:600): a default, so a setting someone gave wins.
        ifd_given = _IFD in huggorm.list_settings(overridden_only=True)
        for name in sorted(names, key=lambda name: name != "hydraJobs"):
            if not ifd_given:
                await self.state.set_setting(_IFD, "true" if name != "hydraJobs" else "false")
            await self.output(name, await outputs.get(name), _At(outputs, name))

    async def build(self) -> None:
        """Build what the checks need that no substituter has, and report each failure."""
        store = self.store
        targets = [await store.parse_derived_path(f"{drv}^*") for drv in self.attr_paths_by_drv]
        missing = await store.query_missing(targets)
        to_build = sorted([await store.print_store_path(path) for path in missing.will_build()])
        logger.info("pynix flake check: running flake checks", count=len(to_build))
        # Every check when its outputs are linked, as `CmdFlakeCheck` builds `drvPaths` then.
        if self.keep_outputs:
            to_build = sorted(self.attr_paths_by_drv)
        if not to_build:
            return
        results = await store.build_paths_with_results([await store.parse_derived_path(f"{drv}^*") for drv in to_build])
        for drv_path, result in zip(to_build, results, strict=True):
            failure = result.error()
            if failure is None:
                if self.keep_outputs and (success := result.success()) is not None:
                    self.built.append(
                        {
                            name: await store.print_store_path(realisation.out_path())
                            for name, realisation in success.built_outputs().items()
                        }
                    )
                continue
            built = f"{drv_path}^*"
            # libstore renders the failure with Nix's "error: " head, and the
            # message goes inside ours.
            message = failure.message.removeprefix("error: ")
            attr_paths = self.attr_paths_by_drv.get(drv_path)
            if not attr_paths:
                await self.report(CheckError.raised("build of '%s' failed: %s", built, message))
            for attr_path in attr_paths or ():
                await self.report(
                    CheckError.raised(
                        "failed to build attribute '%s', build of '%s' failed: %s", attr_path, built, message
                    )
                )


async def run_check(command: Check) -> None:
    """The body of :meth:`pynix.flake.Check.run`."""
    # `nix flake check --no-build` with either flag does nothing and says nothing; `pynix build --dry-run -o` refuses too.
    if command.no_build and command.out_link is not None:
        error_exit("--no-build builds nothing, so --out-link has nothing to link")
    if command.no_build and command.print_out_paths:
        error_exit("--no-build builds nothing, so --print-out-paths has nothing to print")
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
        keep_outputs=command.out_link is not None or command.print_out_paths,
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
        await checker.print_error(error)
        raise SystemExit(1) from error
    if checker.has_errors:
        await checker.print_error(CheckError.raised("some errors were encountered during the evaluation"))
        raise SystemExit(1)
    logger.info("pynix flake check: all checks passed")
    if checker.omitted_systems:
        logger.warning(
            "the check omitted these incompatible systems; use --all-systems to check all",
            systems=sorted(checker.omitted_systems),
        )
    if command.print_out_paths:
        for paths in checker.built:
            for output in sorted(paths):
                sys.stdout.write(f"{paths[output]}\n")
    if command.out_link is not None:
        await create_out_links(store, command.out_link, checker.built)
