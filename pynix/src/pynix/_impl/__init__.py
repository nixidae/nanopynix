"""The heavy half of a subcommand, imported when that subcommand runs.

**The parser needs every command class before it can parse one argument.**
``pynix/__init__.py`` lists every subcommand, and ``libpynix`` reads
that annotation while the class body of ``Pynix`` runs. So every subcommand
module loads on every start, including ``pynix --help`` and every keypress of
a shell completion.

A subcommand module therefore holds the command class and its options, and
nothing else. What ``run`` needs lives here. deferimport makes the imports
below lazy, so a module loads at the first attribute read -- which happens
inside ``run``, after the parser decided that this is the subcommand to run.

Issue #123 measured what this saves, on the release build:

- ``pynix.repl`` was 91.8 ms, nearly all of it ``prompt_toolkit``;
- ``pynix.build`` was 112.2 ms, nearly all of it ``tree_sitter_nix``, which
  pulls ``tree_sitter_config`` and through it ``email_validator``.

``options_tui`` is not a subcommand. It is the second mode of one, and it
costs 115 ``prompt_toolkit`` modules and 69 Markdown ones, which
``pynix search <query>`` does not need.
"""

from __future__ import annotations

from pynix._impl import (
    build as build,
    config as config,
    copy as copy,
    derivation as derivation,
    develop as develop,
    env as env,
    eval as eval,
    flake as flake,
    log as log,
    main as main,
    merged_tui as merged_tui,
    options_tui as options_tui,
    package_tui as package_tui,
    path_info as path_info,
    registry as registry,
    repl as repl,
    run as run,
    sbom as sbom,
    search as search,
    settings as settings,
    store as store,
    why_depends as why_depends,
)
