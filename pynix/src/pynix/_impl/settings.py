"""Where a repeated ``pynix`` option gets its default from.

Four layers, and the first one that names a value wins:

1. the flag on the command line
2. the environment, as ``PYNIX_<OPTION>`` and ``PYNIX_NIX_<SETTING>``
3. ``$XDG_CONFIG_HOME/pynix/config.toml``
4. the built-in default

An option the caller did not name is absent from the namespace until
:class:`~pynix._settings.ConfiguredCommand` resolves it, which is how "absent"
is told apart from "explicitly false".

The file holds two tables. ``[defaults]`` holds the options that cross the
commands, and ``[nix]`` holds the Nix settings::

    [defaults]
    store = "daemon"
    verbosity = "notice"
    print-build-logs = true

    [nix]
    substituters = ["https://cache.nixos.org/", "https://mine.example/"]
    max-jobs = 8

A Nix setting stays in the ``nix.conf`` spelling: a string, with a list
joined by spaces. Nix parses it, so pynix keeps no model of the settings, and
only checks that each name is one Nix knows.

**The blocking file read is deliberate.** The read happens in
:meth:`~pynix._settings.ConfiguredCommand.__init__`, which ``Pynix.parse()``
calls, and ``main()`` calls ``parse()`` before it starts any event loop. There
is no loop to block, and ``anyio.Path`` would have nowhere to run.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Collection
from pathlib import Path
from typing import Any, Final, cast

from libpynix import Command

#: What every ``--store`` defaults to.
DEFAULT_STORE = "auto"

#: The table of the configuration file that each layer reads.
DEFAULTS_TABLE = "defaults"

NIX_TABLE = "nix"

#: Names another configuration file, for a test and for a user who keeps more
#: than one profile.
CONFIG_PATH_VARIABLE = "PYNIX_CONFIG"

#: The prefix of a ``[defaults]`` option in the environment.
DEFAULTS_PREFIX: Final = "PYNIX_"

#: The prefix of a Nix setting in the environment.
NIX_PREFIX: Final = "PYNIX_NIX_"

#: The ``pynix`` options that repeat across the commands, and the built-in
#: default of each. These are not Nix settings: ``--store`` names which store
#: a command opens, and Nix has a global of the same name that says something
#: else.
DEFAULTS: Final[dict[str, str | bool | None]] = {
    "store": DEFAULT_STORE,
    "eval_store": None,
    "verbosity": None,
    "print_build_logs": False,
    "linux_builder_guest": None,
}

# The spellings of true and false that the environment takes.
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def config_path() -> Path:
    """The configuration file this process reads, whether or not it exists."""
    override = os.environ.get(CONFIG_PATH_VARIABLE)
    if override:
        return Path(override)
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "pynix" / "config.toml"


class ConfigFileError(Exception):
    """The configuration file exists and cannot be read."""


class SettingError(ValueError):
    """A layer names an option or a Nix setting that does not exist, or a value of the wrong kind."""


def _read_table(table: str) -> dict[str, Any]:
    """Read one table out of the configuration file.

    A missing file is not an error: a user who never wrote one is the ordinary
    case. A file that exists and does not parse is an error, and the message
    names the file, because the alternative is a silently ignored setting.
    """
    path = config_path()
    try:
        text = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigFileError(f"cannot read {path}: {exc}") from exc
    try:
        document = tomllib.loads(text.decode())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigFileError(f"cannot parse {path}: {exc}") from exc
    section = document.get(table)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigFileError(f"[{table}] in {path} must be a table, not {type(section).__name__}")
    # A TOML key is a string by the grammar, so the isinstance check above is
    # the whole of the narrowing that this needs.
    return cast("dict[str, Any]", section)


def _option(name: str, value: object, where: str) -> str | bool | None:
    """*value* as the kind of the option *name*, or a `SettingError` that names *where*."""
    if isinstance(DEFAULTS[name], bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in _TRUE | _FALSE:
            return value.lower() in _TRUE
        raise SettingError(f"{where} must be true or false, not {value!r}")
    if not isinstance(value, str):
        raise SettingError(f"{where} must be a string, not {value!r}")
    return value


def pynix_defaults(**named: Any) -> dict[str, str | bool | None]:
    """Every option in :data:`DEFAULTS`, resolved: *named*, then the environment, then the file."""
    table = _read_table(DEFAULTS_TABLE)
    unknown = sorted(set(table) - {name.replace("_", "-") for name in DEFAULTS})
    if unknown:
        raise SettingError(f"[{DEFAULTS_TABLE}] in {config_path()} names no pynix option: {', '.join(unknown)}")
    resolved: dict[str, str | bool | None] = {}
    for name, default in DEFAULTS.items():
        variable = f"{DEFAULTS_PREFIX}{name.upper()}"
        key = name.replace("_", "-")
        if name in named:
            resolved[name] = named[name]
        elif variable in os.environ:
            resolved[name] = _option(name, os.environ[variable], variable)
        elif key in table:
            resolved[name] = _option(name, table[key], f"{key} in [{DEFAULTS_TABLE}]")
        else:
            resolved[name] = default
    return resolved


def _nix_conf_value(value: object, where: str) -> str:
    """A TOML value in the spelling ``nix.conf`` gives it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | int | float):
        return str(value)
    if isinstance(value, list) and all(isinstance(item, str | int | float) for item in cast("list[object]", value)):
        return " ".join(str(item) for item in cast("list[object]", value))
    raise SettingError(f"{where} must be a string, a number, a boolean or a list of strings, not {value!r}")


def nix_settings(known: Collection[str], **flags: str | None) -> dict[str, str]:
    """The Nix settings that a command names, in ``nix.conf`` spelling.

    A flag wins, then ``PYNIX_NIX_<SETTING>``, then ``[nix]``. A ``None`` flag
    is one the caller did not give. A setting that no layer names is absent,
    so what ``nix.conf`` says stands, as it does for ``nix build``.

    *known* is every setting name Nix has, ``huggorm.list_settings()``. A name
    outside it raises, because a typo that pynix passes on is a setting that a
    user believes is applied.
    """
    settings: dict[str, str] = {}
    sources: dict[str, str] = {}
    for key, value in _read_table(NIX_TABLE).items():
        settings[key] = _nix_conf_value(value, f"{key} in [{NIX_TABLE}]")
        sources[key] = f"[{NIX_TABLE}] in {config_path()}"
    for variable, value in os.environ.items():
        if variable.startswith(NIX_PREFIX):
            key = variable.removeprefix(NIX_PREFIX).lower().replace("_", "-")
            settings[key] = value
            sources[key] = variable
    for name, value in flags.items():
        if value is not None:
            key = name.replace("_", "-")
            settings[key] = value
            sources[key] = f"--{key}"
    unknown = sorted(f"{key} ({sources[key]})" for key in settings if key not in known)
    if unknown:
        raise SettingError(f"no such Nix setting: {', '.join(unknown)}")
    return settings


def configured_fields(command: type[Command]) -> list[str]:
    """The options of *command* that :func:`~pynix._settings.option` declared.

    The declaration is the whole test: an option that ``option`` declared
    carries ``configured``, and every other option is an ordinary flag with an
    ordinary default. Reading the declaration keeps a name that a Nix setting
    happens to share, such as ``substituters``, out of this.
    """
    fields: list[str] = []
    for field, spec in command.specs.items():
        if not spec.configured:
            continue
        if field not in DEFAULTS:
            raise TypeError(f"{command.__name__}.{field} uses option(), and DEFAULTS does not name it")
        fields.append(field)
    return fields
