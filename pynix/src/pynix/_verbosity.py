"""Nix's verbosity, from what a user types."""

from __future__ import annotations

from huggorm import Verbosity

type VerbosityInput = Verbosity | int | str
"""A :class:`huggorm.Verbosity`, an int 0-7, or a level name such as ``"warn"``,
``"info"`` or ``"lvlDebug"`` (case-insensitive)."""

_ALIASES = {"err": Verbosity.ERROR, "warning": Verbosity.WARN}


def verbosity_of(value: VerbosityInput) -> Verbosity:
    """*value* as a :class:`huggorm.Verbosity`.

    Raises:
        TypeError: *value* is a ``bool``.
        ValueError: *value* is an int outside 0-7, or no level's name.
    """
    if isinstance(value, Verbosity):
        return value
    if isinstance(value, bool):
        raise TypeError("verbosity must be a log level name or integer from 0 through 7")
    if isinstance(value, int):
        return _from_int(value)
    raw = value.strip()
    if not raw:
        raise ValueError("verbosity cannot be empty")
    if raw.isdecimal():
        return _from_int(int(raw))
    key = raw.replace("-", "_").lower().removeprefix("log_level_").removeprefix("lvl").removeprefix("_")
    if key in _ALIASES:
        return _ALIASES[key]
    try:
        return Verbosity[key.upper()]
    except KeyError as exc:
        valid = ", ".join(sorted([*(level.name.lower() for level in Verbosity), *_ALIASES]))
        raise ValueError(f"unknown verbosity {value!r}; expected 0-7 or one of: {valid}") from exc


def _from_int(value: int) -> Verbosity:
    try:
        return Verbosity(value)
    except ValueError as exc:
        raise ValueError(f"verbosity must be between 0 and 7, got {value}") from exc
