"""The libstore surface, with no engine behind it.

**This module imports ``nanopynix_bindings`` and nothing else of nanopynix.**
A consumer that opens a store, initialises Nix and reads a path needs no RPC
engine, no settings model and no generated protocol. It imports this module,
and it pays for the C++ extension alone.

``nanopynix`` itself gives every name of this module, and it costs more to
reach them there. The package builds its whole public surface on demand, so
the first name that a caller reads decides what loads. A caller that wants the
store alone says so here:

    from nanopynix.libstore import init_libstore, open_store

Measured on 2026-08-12, as the best of five runs of a fresh interpreter:

| import | seconds |
| --- | --- |
| ``nanopynix.libstore`` | 0.049 |
| ``nanopynix.rpc`` | 0.834 |

``nanopynix/__init__.py`` gives the whole reason, under "One import, and what
it loads". Issue #122 holds the measurement that started this file.

**libexpr is not here.** The evaluator lives in ``nanopynix_bindings.expr``,
which is as cheap and is already public, so a caller that evaluates imports
that module directly.
"""

from __future__ import annotations

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
    init_libstore as _init_libstore_raw,
    list_settings as list_settings,
)

#: The experimental features that :func:`init_libstore` turns on.
#:
#: **This tuple lives here and not in :mod:`nanopynix.settings`**, because
#: :func:`init_libstore` is the one function that every consumer calls and
#: that module costs a pydantic model tree. ``nanopynix.settings`` imports the
#: tuple from here.
DEFAULT_EXPERIMENTAL_FEATURES = (
    "flakes",
    "nix-command",
    "ca-derivations",
    "dynamic-derivations",
    "recursive-nix",
)


def init_libstore(load_config: bool = True) -> None:
    """Initialize libstore, then enable nanopynix's default experimental features.

    The one Nix initialisation entry point nanopynix offers. There used to be a
    second, ``init_nix``, wrapping ``nix::initNix``; it is gone because
    everything ``initNix`` adds over ``initLibStore`` is a process-wide side
    effect a library has no business imposing on its host -- a signal-handler
    thread, ``SIGCHLD`` reset to ``SIG_DFL``, a ``SIGSEGV`` handler, an
    ``NIX_SIG_MULTI_INT`` handler, ``umask(0022)``, a ``RLIMIT_NOFILE`` bump
    and a static buffer installed on ``std::cerr``. Python has its own signal
    machinery, and nothing in nanopynix ever called it.

    Enabling the features here, rather than leaving it to whoever opens a
    store, is load-bearing: Nix latches some of them at store *construction*
    but re-checks them at *query* time. ``LocalStore`` prepares its realisation
    SQL statements only when ``ca-derivations`` is on at construction
    (``local-store.cc:356``), while ``queryRealisationUncached`` re-tests the
    flag and dereferences those statements (``:1563``). A store built before
    the feature was enabled, then queried after it was turned on, therefore
    trips ``assert(stmt.stmt)`` and aborts the process -- SIGABRT, not an
    exception, so there is nothing a caller could have caught.

    Since libstore has to be initialised before any libstore call anyway, doing
    it here means every store nanopynix can open is constructed with the
    defaults already in force. ``Session`` enables the same features again
    through ``runtime.initialize``, which calls
    :func:`enable_experimental_feature` at the same point of its own sequence;
    that is additive and harmless.
    """
    _init_libstore_raw(load_config=load_config)
    _enable_default_experimental_features()


def _enable_default_experimental_features() -> None:
    for feature in DEFAULT_EXPERIMENTAL_FEATURES:
        enable_experimental_feature(feature)


__all__ = [
    "DEFAULT_EXPERIMENTAL_FEATURES",
    "BuildMode",
    "build_info",
    "current_system",
    "enable_experimental_feature",
    "init_libstore",
    "list_settings",
    "open_store",
    "process_connection",
    "register_store_implementation",
]
