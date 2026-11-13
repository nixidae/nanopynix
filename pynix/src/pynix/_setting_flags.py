"""Nix's per-setting flags: ``--max-jobs 4``, ``--no-sandbox``, ``--extra-substituters X``.

``Config::convertToArgs`` (``configuration.cc:224``) gives each setting a flag:
``--NAME VALUE``, and ``--extra-NAME VALUE`` when the value is a list or a map
(``config-impl.hh:95``); ``--NAME`` and ``--no-NAME`` for a bool
(``configuration.cc:320``); and ``--relaxed-sandbox`` besides
(``globals.cc:341``). Each one here appends a pair to ``--option``'s list,
so the two apply in command-line order.

Hidden, as every compatibility spelling is, and declared only when a first
parse leaves an option over (:func:`libpynix.parse_args`): reading the
settings needs Nix, and ``--help`` or a completion needs none.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, override

import huggorm

#: The field of every command that runs which ``--option`` fills.
_OPTION_FIELD = "option"


@dataclass(frozen=True)
class SettingFlag:
    """One flag, and the pair it appends: *value* None takes the value from the line."""

    flags: tuple[str, ...]
    setting: str
    value: str | None


def setting_flags(
    settings: dict[str, Any],
) -> Iterator[SettingFlag]:  # Any: Nix's `settings_json`, one object per setting
    """The flags of every setting in *settings*, as ``huggorm.settings_json`` gives them."""
    for name, info in settings.items():
        default = info["defaultValue"]
        aliases = tuple(f"--{alias}" for alias in info["aliases"])
        if isinstance(default, bool):
            yield SettingFlag((f"--{name}", *aliases), name, "true")
            yield SettingFlag((f"--no-{name}",), name, "false")
            if name == "sandbox":
                yield SettingFlag(("--relaxed-sandbox",), name, "relaxed")
            continue
        yield SettingFlag((f"--{name}", *aliases), name, None)
        if isinstance(default, list | dict):
            yield SettingFlag((f"--extra-{name}",), f"extra-{name}", None)


class _AppendSetting(argparse.Action):
    """Append ``(setting, value)`` to the ``--option`` list."""

    def __init__(self, *args: Any, setting: str, fixed: str | None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.setting = setting
        self.fixed = fixed

    @override
    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        value = self.fixed if self.fixed is not None else str(values)
        pairs = [*vars(namespace).get(_OPTION_FIELD, []), (self.setting, value)]
        vars(namespace)[_OPTION_FIELD] = pairs


def add_setting_flags(parsers: Sequence[argparse.ArgumentParser]) -> None:
    """Give each of *parsers* the flags of Nix's settings, hidden.

    A flag the command already declares stays the command's: ``--store`` and
    ``--keep-going`` of ``pynix build`` mean what pynix says.
    """
    flags = list(setting_flags(json.loads(huggorm.settings_json())))
    for parser in parsers:
        taken = set(parser._option_string_actions)  # noqa: SLF001 -- argparse names no public way to ask which flags a parser has
        for flag in flags:
            free = tuple(spelling for spelling in flag.flags if spelling not in taken)
            if not free:
                continue
            parser.add_argument(
                *free,
                action=_AppendSetting,
                setting=flag.setting,
                fixed=flag.value,
                nargs=0 if flag.value is not None else None,
                dest=argparse.SUPPRESS,
                default=argparse.SUPPRESS,
                help=argparse.SUPPRESS,
            )
            taken.update(free)
