"""nanopynix — nanobind-based Python bindings for Nix.

**This module builds its public surface on demand.** Every name below comes
from a submodule, and PEP 562 gives a module a ``__getattr__``, so the first
read of a name imports the one module that defines it. A caller that reads no
name imports nothing but this file.

One import, and what it loads
-----------------------------
The surface used to be eager, and it cost 0.848 s of a fresh interpreter,
which started in 0.017 s of that. 555 modules loaded, and 274 ms of them were
the three generated modules of ``nanopynix_proto``. Every consumer paid the
whole amount to read one name, and a planner of ``ddrn/examples/venv-graph``
spent 97% of its run on the import and 0.025 s on its work.
``import nanopynix`` costs 0.030 s now.

The cost did not come from any one module. ``exceptions``, ``models``,
``protocols``, ``verbosity`` and ``logging`` each import the generated
protocol, and ``inproc``, ``rpc`` and ``settings`` cost 190 ms, 183 ms and
113 ms. So no subset of the surface could stay eager and be cheap, and the
whole surface became lazy instead. Issue #122 holds the measurement.

**A consumer that reads a name from every module still pays for every
module.** ``pynix`` is one, and its start did not change. That is the right
answer and not a shortfall: the cost belongs to the caller that asks for the
name, and issue #107 covers what ``pynix`` itself asks for.

**A consumer of the store alone should say so**, and import
:mod:`nanopynix.libstore`. That module imports ``nanopynix_bindings`` and no
other module of nanopynix, so it needs no ``__getattr__`` and it cannot grow
one by accident.

The convention of this repository bans a lazy import inside a function, and
this is not one: a module ``__getattr__`` runs at the first *read of a name*
and not at each call, and it caches what it read. `CLAUDE.md` names this
exception.

**Every name stays a name of this module.** :data:`__all__` is unchanged,
``from nanopynix import X`` still works, and the block below tells a type
checker what each name is.
"""

from __future__ import annotations

import importlib
import typing

# **`typing.TYPE_CHECKING` and not a bare `TYPE_CHECKING`.** A bare import
# binds a public name on this package, and this package publishes what it
# binds. `tests/meta/test_public_surface.py` refuses that name.
if typing.TYPE_CHECKING:
    from nanopynix_bindings.expr import (
        EvalState as EvalState,
        PrimopError as PrimopError,
        Value as Value,
        eval_file as eval_file,
        init_libexpr as init_libexpr,
        register_primop as register_primop,
    )
    from nanopynix_bindings.fetchers import (
        input_from_attrs as input_from_attrs,
        input_from_url as input_from_url,
    )
    from nanopynix_bindings.flake import (
        get_flake as get_flake,
        lock_flake as lock_flake,
        parse_flake_ref as parse_flake_ref,
    )
    from nanopynix_bindings.store import (
        BuildMode as BuildMode,
        open_store as open_store,
        process_connection as process_connection,
        register_store_implementation as register_store_implementation,
    )
    from nanopynix_bindings.util import (
        build_info as build_info,  # type: ignore[reportUnknownVariableType] -- C++ extension without type stubs
        current_system as current_system,
        enable_experimental_feature as enable_experimental_feature,
        get_verbosity as get_verbosity,
        install_logger as install_logger,
        list_settings as list_settings,
        remove_logger as remove_logger,
        set_verbosity as set_verbosity,
    )
    from nanopynix_proto.nix.common import LogLevel as LogLevel
    from nanopynix_proto.nix.store import GcAction as GcAction

    from nanopynix import inproc as inproc, libstore as libstore, rpc as rpc, stores as stores
    from nanopynix._ansi import strip_ansi as strip_ansi
    from nanopynix._process_title import set_manager_title as set_manager_title
    from nanopynix.exceptions import (
        BadStorePathError as BadStorePathError,
        BuildError as BuildError,
        BuildHashMismatchError as BuildHashMismatchError,
        BuildTimedOutError as BuildTimedOutError,
        CachedBuildFailureError as CachedBuildFailureError,
        DependencyFailedError as DependencyFailedError,
        EngineError as EngineError,
        EvalError as EvalError,
        EvalHashMismatchError as EvalHashMismatchError,
        EvalSessionClosedError as EvalSessionClosedError,
        EvaluatorAbandonedError as EvaluatorAbandonedError,
        ForeignValueError as ForeignValueError,
        ForkedSessionError as ForkedSessionError,
        HashMismatchError as HashMismatchError,
        InfiniteRecursionError as InfiniteRecursionError,
        InputRejectedError as InputRejectedError,
        InvalidPathError as InvalidPathError,
        ListIndexError as ListIndexError,
        LockedFlakeReleasedError as LockedFlakeReleasedError,
        LogLimitExceededError as LogLimitExceededError,
        MiscBuildError as MiscBuildError,
        MissingArgumentError as MissingArgumentError,
        MissingAttributeError as MissingAttributeError,
        NixAssertionError as NixAssertionError,
        NixError as NixError,
        NixSysError as NixSysError,
        NixTypeError as NixTypeError,
        NoSubstitutersError as NoSubstitutersError,
        NotDeterministicError as NotDeterministicError,
        ObjectLifetimeError as ObjectLifetimeError,
        ObjectMisuseError as ObjectMisuseError,
        OutputRejectedError as OutputRejectedError,
        ParseError as ParseError,
        PermanentBuildError as PermanentBuildError,
        RestrictedPathError as RestrictedPathError,
        SessionClosedError as SessionClosedError,
        SettingNotLiveError as SettingNotLiveError,
        SettingOutOfScopeError as SettingOutOfScopeError,
        StoreClosedError as StoreClosedError,
        StoreError as StoreError,
        ThrownError as ThrownError,
        TransientBuildError as TransientBuildError,
        UndefinedVarError as UndefinedVarError,
        UnimplementedError as UnimplementedError,
        UnresolvedValueError as UnresolvedValueError,
        UnsupportedError as UnsupportedError,
        UsageError as UsageError,
        ValueReleasedError as ValueReleasedError,
        WorkerDiedError as WorkerDiedError,
        WorkerSignaledError as WorkerSignaledError,
        WrongNixTypeError as WrongNixTypeError,
    )
    from nanopynix.libstore import (
        DEFAULT_EXPERIMENTAL_FEATURES as DEFAULT_EXPERIMENTAL_FEATURES,
        init_libstore as init_libstore,
    )
    from nanopynix.logging import (
        LogCapture as LogCapture,
        LogCollector as LogCollector,
    )
    from nanopynix.models import (
        BuildResult as BuildResult,
        Derivation as Derivation,
        DerivationOutput as DerivationOutput,
        DerivationOutputs as DerivationOutputs,
        DerivedPath as DerivedPath,
        FlakeRef as FlakeRef,
        GcResult as GcResult,
        GcRoot as GcRoot,
        Input as Input,
        LockedFlake as LockedFlake,
        LockedNode as LockedNode,
        LogEvent as LogEvent,
        MissingInfo as MissingInfo,
        NixType as NixType,
        PathInfo as PathInfo,
        PrimOpSpec as PrimOpSpec,
        ResultType as ResultType,
        StorePath as StorePath,
    )
    from nanopynix.namespace import (
        NamespaceSupport as NamespaceSupport,
        OverlayNamespace as OverlayNamespace,
        enter_overlay_namespace as enter_overlay_namespace,
        probe_namespace_support as probe_namespace_support,
    )
    from nanopynix.primops import (
        from_yaml as from_yaml,
        from_yaml11 as from_yaml11,
        from_yaml11_stream as from_yaml11_stream,
        from_yaml_stream as from_yaml_stream,
        to_yaml as to_yaml,
        yaml_primops as yaml_primops,
    )
    from nanopynix.protocols import (
        AsyncEvalSession as AsyncEvalSession,
        AsyncLockedFlake as AsyncLockedFlake,
        AsyncReplSession as AsyncReplSession,
        AsyncSession as AsyncSession,
        AsyncStore as AsyncStore,
        AsyncValue as AsyncValue,
        AsyncVerbosityController as AsyncVerbosityController,
    )
    from nanopynix.settings import (
        NanopynixSettings as NanopynixSettings,
        NixEvalSettings as NixEvalSettings,
        NixEvaluatorSettings as NixEvaluatorSettings,
        NixFetchSettings as NixFetchSettings,
        NixFlakeSettings as NixFlakeSettings,
        NixGlobalSettings as NixGlobalSettings,
        NixSettingMetadata as NixSettingMetadata,
        NixSettings as NixSettings,
        NixSettingsEnv as NixSettingsEnv,
        NixStoreDefaults as NixStoreDefaults,
        PrefixedEnvSettingsSource as PrefixedEnvSettingsSource,
        SettingsDrift as SettingsDrift,
        SettingsProvenance as SettingsProvenance,
        check_all_settings_model_drift as check_all_settings_model_drift,
        check_settings_model_drift as check_settings_model_drift,
        list_eval_settings_metadata as list_eval_settings_metadata,
        list_fetch_settings_metadata as list_fetch_settings_metadata,
        list_flake_settings_metadata as list_flake_settings_metadata,
        list_settings_metadata as list_settings_metadata,
    )
    from nanopynix.store_exec import (
        STORE_EXEC_TOOL as STORE_EXEC_TOOL,
        store_exec_prefix as store_exec_prefix,
    )
    from nanopynix.store_impl import (
        DISPATCHABLE_METHODS as DISPATCHABLE_METHODS,
        StoreImpl as StoreImpl,
    )
    from nanopynix.verbosity import (
        LogLevelInput as LogLevelInput,
        normalize_log_level as normalize_log_level,
    )

#: Each public name, and the module that defines it. **This mapping is the
#: surface**, and the ``if TYPE_CHECKING`` block above states the same thing
#: for a type checker. ``tests/meta/test_public_surface.py`` compares the two,
#: so the pair cannot drift.
_EXPORTS: dict[str, str] = {
    "AsyncEvalSession": "nanopynix.protocols",
    "AsyncLockedFlake": "nanopynix.protocols",
    "AsyncReplSession": "nanopynix.protocols",
    "AsyncSession": "nanopynix.protocols",
    "AsyncStore": "nanopynix.protocols",
    "AsyncValue": "nanopynix.protocols",
    "AsyncVerbosityController": "nanopynix.protocols",
    "BadStorePathError": "nanopynix.exceptions",
    "BuildError": "nanopynix.exceptions",
    "BuildHashMismatchError": "nanopynix.exceptions",
    "BuildMode": "nanopynix_bindings.store",
    "BuildResult": "nanopynix.models",
    "BuildTimedOutError": "nanopynix.exceptions",
    "CachedBuildFailureError": "nanopynix.exceptions",
    "DEFAULT_EXPERIMENTAL_FEATURES": "nanopynix.libstore",
    "DISPATCHABLE_METHODS": "nanopynix.store_impl",
    "DependencyFailedError": "nanopynix.exceptions",
    "Derivation": "nanopynix.models",
    "DerivationOutput": "nanopynix.models",
    "DerivationOutputs": "nanopynix.models",
    "DerivedPath": "nanopynix.models",
    "EngineError": "nanopynix.exceptions",
    "EvalError": "nanopynix.exceptions",
    "EvalHashMismatchError": "nanopynix.exceptions",
    "EvalSessionClosedError": "nanopynix.exceptions",
    "EvalState": "nanopynix_bindings.expr",
    "EvaluatorAbandonedError": "nanopynix.exceptions",
    "FlakeRef": "nanopynix.models",
    "ForeignValueError": "nanopynix.exceptions",
    "ForkedSessionError": "nanopynix.exceptions",
    "GcAction": "nanopynix_proto.nix.store",
    "GcResult": "nanopynix.models",
    "GcRoot": "nanopynix.models",
    "HashMismatchError": "nanopynix.exceptions",
    "InfiniteRecursionError": "nanopynix.exceptions",
    "Input": "nanopynix.models",
    "InputRejectedError": "nanopynix.exceptions",
    "InvalidPathError": "nanopynix.exceptions",
    "ListIndexError": "nanopynix.exceptions",
    "LockedFlake": "nanopynix.models",
    "LockedFlakeReleasedError": "nanopynix.exceptions",
    "LockedNode": "nanopynix.models",
    "LogCapture": "nanopynix.logging",
    "LogCollector": "nanopynix.logging",
    "LogEvent": "nanopynix.models",
    "LogLevel": "nanopynix_proto.nix.common",
    "LogLevelInput": "nanopynix.verbosity",
    "LogLimitExceededError": "nanopynix.exceptions",
    "MiscBuildError": "nanopynix.exceptions",
    "MissingArgumentError": "nanopynix.exceptions",
    "MissingAttributeError": "nanopynix.exceptions",
    "MissingInfo": "nanopynix.models",
    "NamespaceSupport": "nanopynix.namespace",
    "NanopynixSettings": "nanopynix.settings",
    "NixAssertionError": "nanopynix.exceptions",
    "NixError": "nanopynix.exceptions",
    "NixEvalSettings": "nanopynix.settings",
    "NixEvaluatorSettings": "nanopynix.settings",
    "NixFetchSettings": "nanopynix.settings",
    "NixFlakeSettings": "nanopynix.settings",
    "NixGlobalSettings": "nanopynix.settings",
    "NixSettingMetadata": "nanopynix.settings",
    "NixSettings": "nanopynix.settings",
    "NixSettingsEnv": "nanopynix.settings",
    "NixStoreDefaults": "nanopynix.settings",
    "NixSysError": "nanopynix.exceptions",
    "NixType": "nanopynix.models",
    "NixTypeError": "nanopynix.exceptions",
    "NoSubstitutersError": "nanopynix.exceptions",
    "NotDeterministicError": "nanopynix.exceptions",
    "ObjectLifetimeError": "nanopynix.exceptions",
    "ObjectMisuseError": "nanopynix.exceptions",
    "OutputRejectedError": "nanopynix.exceptions",
    "OverlayNamespace": "nanopynix.namespace",
    "ParseError": "nanopynix.exceptions",
    "PathInfo": "nanopynix.models",
    "PermanentBuildError": "nanopynix.exceptions",
    "PrefixedEnvSettingsSource": "nanopynix.settings",
    "PrimOpSpec": "nanopynix.models",
    "PrimopError": "nanopynix_bindings.expr",
    "RestrictedPathError": "nanopynix.exceptions",
    "ResultType": "nanopynix.models",
    "STORE_EXEC_TOOL": "nanopynix.store_exec",
    "SessionClosedError": "nanopynix.exceptions",
    "SettingNotLiveError": "nanopynix.exceptions",
    "SettingOutOfScopeError": "nanopynix.exceptions",
    "SettingsDrift": "nanopynix.settings",
    "SettingsProvenance": "nanopynix.settings",
    "StoreClosedError": "nanopynix.exceptions",
    "StoreError": "nanopynix.exceptions",
    "StoreImpl": "nanopynix.store_impl",
    "StorePath": "nanopynix.models",
    "ThrownError": "nanopynix.exceptions",
    "TransientBuildError": "nanopynix.exceptions",
    "UndefinedVarError": "nanopynix.exceptions",
    "UnimplementedError": "nanopynix.exceptions",
    "UnresolvedValueError": "nanopynix.exceptions",
    "UnsupportedError": "nanopynix.exceptions",
    "UsageError": "nanopynix.exceptions",
    "Value": "nanopynix_bindings.expr",
    "ValueReleasedError": "nanopynix.exceptions",
    "WorkerDiedError": "nanopynix.exceptions",
    "WorkerSignaledError": "nanopynix.exceptions",
    "WrongNixTypeError": "nanopynix.exceptions",
    "build_info": "nanopynix_bindings.util",
    "check_all_settings_model_drift": "nanopynix.settings",
    "check_settings_model_drift": "nanopynix.settings",
    "current_system": "nanopynix_bindings.util",
    "enable_experimental_feature": "nanopynix_bindings.util",
    "enter_overlay_namespace": "nanopynix.namespace",
    "eval_file": "nanopynix_bindings.expr",
    "from_yaml": "nanopynix.primops",
    "from_yaml11": "nanopynix.primops",
    "from_yaml11_stream": "nanopynix.primops",
    "from_yaml_stream": "nanopynix.primops",
    "get_flake": "nanopynix_bindings.flake",
    "get_verbosity": "nanopynix_bindings.util",
    "init_libexpr": "nanopynix_bindings.expr",
    "init_libstore": "nanopynix.libstore",
    "input_from_attrs": "nanopynix_bindings.fetchers",
    "input_from_url": "nanopynix_bindings.fetchers",
    "install_logger": "nanopynix_bindings.util",
    "list_eval_settings_metadata": "nanopynix.settings",
    "list_fetch_settings_metadata": "nanopynix.settings",
    "list_flake_settings_metadata": "nanopynix.settings",
    "list_settings": "nanopynix_bindings.util",
    "list_settings_metadata": "nanopynix.settings",
    "lock_flake": "nanopynix_bindings.flake",
    "normalize_log_level": "nanopynix.verbosity",
    "open_store": "nanopynix_bindings.store",
    "parse_flake_ref": "nanopynix_bindings.flake",
    "probe_namespace_support": "nanopynix.namespace",
    "process_connection": "nanopynix_bindings.store",
    "register_primop": "nanopynix_bindings.expr",
    "register_store_implementation": "nanopynix_bindings.store",
    "remove_logger": "nanopynix_bindings.util",
    "set_manager_title": "nanopynix._process_title",
    "set_verbosity": "nanopynix_bindings.util",
    "store_exec_prefix": "nanopynix.store_exec",
    "strip_ansi": "nanopynix._ansi",
    "to_yaml": "nanopynix.primops",
    "yaml_primops": "nanopynix.primops",
}

#: The submodules that a caller reaches as an attribute of this package.
_SUBMODULES = ("inproc", "libstore", "rpc", "stores")


def __getattr__(name: str) -> object:
    """Import the module that defines `name`, and give back the name.

    PEP 562 calls this for a name that the module does not already hold. The
    value goes into ``globals()``, so a second read is an ordinary attribute
    lookup and this function runs one time for each name.
    """
    if name in _SUBMODULES:
        value: object = importlib.import_module(f"{__name__}.{name}")
    else:
        module = _EXPORTS.get(name)
        if module is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Every public name, whether or not the module that defines it is loaded."""
    return sorted(__all__)


__all__ = [
    "DEFAULT_EXPERIMENTAL_FEATURES",
    "DISPATCHABLE_METHODS",
    "STORE_EXEC_TOOL",
    "AsyncEvalSession",
    "AsyncLockedFlake",
    "AsyncReplSession",
    "AsyncSession",
    "AsyncStore",
    "AsyncValue",
    "AsyncVerbosityController",
    "BadStorePathError",
    "BuildError",
    "BuildHashMismatchError",
    "BuildMode",
    "BuildResult",
    "BuildTimedOutError",
    "CachedBuildFailureError",
    "DependencyFailedError",
    "Derivation",
    "DerivationOutput",
    "DerivationOutputs",
    "DerivedPath",
    "EngineError",
    "EvalError",
    "EvalHashMismatchError",
    "EvalSessionClosedError",
    "EvalState",
    "EvaluatorAbandonedError",
    "FlakeRef",
    "ForeignValueError",
    "ForkedSessionError",
    "GcAction",
    "GcResult",
    "GcRoot",
    "HashMismatchError",
    "InfiniteRecursionError",
    "Input",
    "InputRejectedError",
    "InvalidPathError",
    "ListIndexError",
    "LockedFlake",
    "LockedFlakeReleasedError",
    "LockedNode",
    "LogCapture",
    "LogCollector",
    "LogEvent",
    "LogLevel",
    "LogLevelInput",
    "LogLimitExceededError",
    "MiscBuildError",
    "MissingArgumentError",
    "MissingAttributeError",
    "MissingInfo",
    "NamespaceSupport",
    "NanopynixSettings",
    "NixAssertionError",
    "NixError",
    "NixEvalSettings",
    "NixEvaluatorSettings",
    "NixFetchSettings",
    "NixFlakeSettings",
    "NixGlobalSettings",
    "NixSettingMetadata",
    "NixSettings",
    "NixSettingsEnv",
    "NixStoreDefaults",
    "NixSysError",
    "NixType",
    "NixTypeError",
    "NoSubstitutersError",
    "NotDeterministicError",
    "ObjectLifetimeError",
    "ObjectMisuseError",
    "OutputRejectedError",
    "OverlayNamespace",
    "ParseError",
    "PathInfo",
    "PermanentBuildError",
    "PrefixedEnvSettingsSource",
    "PrimOpSpec",
    "PrimopError",
    "RestrictedPathError",
    "ResultType",
    "SessionClosedError",
    "SettingNotLiveError",
    "SettingOutOfScopeError",
    "SettingsDrift",
    "SettingsProvenance",
    "StoreClosedError",
    "StoreError",
    "StoreImpl",
    "StorePath",
    "ThrownError",
    "TransientBuildError",
    "UndefinedVarError",
    "UnimplementedError",
    "UnresolvedValueError",
    "UnsupportedError",
    "UsageError",
    "Value",
    "ValueReleasedError",
    "WorkerDiedError",
    "WorkerSignaledError",
    "WrongNixTypeError",
    "build_info",
    "check_all_settings_model_drift",
    "check_settings_model_drift",
    "current_system",
    "enable_experimental_feature",
    "enter_overlay_namespace",
    "eval_file",
    "from_yaml",
    "from_yaml11",
    "from_yaml11_stream",
    "from_yaml_stream",
    "get_flake",
    "get_verbosity",
    "init_libexpr",
    "init_libstore",
    "inproc",
    "input_from_attrs",
    "input_from_url",
    "install_logger",
    "libstore",
    "list_eval_settings_metadata",
    "list_fetch_settings_metadata",
    "list_flake_settings_metadata",
    "list_settings",
    "list_settings_metadata",
    "lock_flake",
    "normalize_log_level",
    "open_store",
    "parse_flake_ref",
    "probe_namespace_support",
    "process_connection",
    "register_primop",
    "register_store_implementation",
    "remove_logger",
    "rpc",
    "set_manager_title",
    "set_verbosity",
    "store_exec_prefix",
    "stores",
    "strip_ansi",
    "to_yaml",
    "yaml_primops",
]
