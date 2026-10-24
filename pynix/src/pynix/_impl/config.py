"""The implementation of the ``pynix config`` command.

``pynix.config`` holds the command class and its options, and this module holds
what ``run`` needs. ``pynix._impl`` says why: the parser loads every subcommand module
on every start, and none of these imports is needed to list an option.
"""

from __future__ import annotations

import huggorm

from pynix._util import apply_nix_settings, print_json
from pynix.config import Check, CurrentSystem, Show


async def run_show(command: Show) -> None:
    """The body of :meth:`pynix.config.Show.run`."""
    apply_nix_settings()
    settings = huggorm.list_settings()
    if command.setting is not None:
        print_json({command.setting: settings.get(command.setting)})
        return
    print_json(settings)


async def run_check(command: Check) -> None:  # noqa: ARG001 -- every runner takes its command, so the shape does not depend on which options one reads
    """The body of :meth:`pynix.config.Check.run`."""
    apply_nix_settings()
    print_json({"ok": True})


async def run_current_system(command: CurrentSystem) -> None:  # noqa: ARG001 -- see run_check
    """The body of :meth:`pynix.config.CurrentSystem.run`."""
    apply_nix_settings()
    print_json({"currentSystem": huggorm.current_system()})
