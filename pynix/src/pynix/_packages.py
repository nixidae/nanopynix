"""Bulk package extraction, for ``pynix search``.

This is the package half of what ``pynix/_options.py`` does for NixOS options,
and it follows the same two rules: run the whole walk inside the evaluator, in
one round trip, and force nothing that a package can define as an expression
over something a walk cannot resolve.

**The walk reads the nixpkgs the caller pinned, and not a channel release.**
Issue #85 measured the other source: Hydra publishes ``packages.json.br``,
which holds 148 251 packages of one release. Two things follow. That file
describes a release the caller may not use, and the channel serves it in
Brotli alone -- ``packages.json``, ``.gz`` and ``.xz`` each answer 404 -- so
reading it means a dependency that nothing else here needs. This walk answers
"what is in *my* nixpkgs", which is the set the caller will really build.

Measured on nixpkgs unstable: 24 571 packages, 17.6 s, 2.08 GB at peak, and
16 069 of them name a ``meta.mainProgram``.

**``mainProgram`` is one binary, and a package gives many.** So this walk does
not answer "which package gives me ``ssh-keygen``". ``programs.sqlite``, which
Hydra ships inside ``nixexprs.tar.xz``, holds 161 511 rows of binary to
package and does answer it. Issue #256 adds that source beside this one; the
two join on the package name.

**The walk enters a set that carries ``recurseForDerivations = true``, at any
depth, and no other set.** That is the rule ``nix search`` follows under a
flake's ``legacyPackages`` (``search.cc:177``), and it finds the same 113 630
records on nixpkgs. ``pkgsCross``, ``pkgsStatic`` and ``python3Packages`` carry
no marker, so neither walk enters them. Nix keeps a ``--file`` target at the
top level; pynix enters the sets for every target (nixidae/nixidae#320).

**Each group of sets reads a fresh package set, when the caller can make one.**
A forced set stays reachable from the fixpoint that holds it, so only a fresh
``import nixpkgs { }`` lets the collector free a group once it is read.
Measured on one nixpkgs: 3.45 GB at peak for 20.7 s, against 4.78 GB for
18.4 s over one shared package set.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import structlog
from huggorm import AsyncEvalState, AsyncValue
from huggorm.errors import NixError

from pynix._evaluate import JsonValue, call, to_python
from pynix._nix_helpers import helper
from pynix._util import muted_warnings

logger = structlog.get_logger("pynix.packages")

#: Names per group of sets. A set larger than this is a group of its own.
#: The peak follows the largest group, and each group costs one evaluation of
#: the target: `haskellPackages` alone holds 19 446 names.
_GROUP_NAMES = 10_000

#: How the walk makes a fresh package set for a group: the target evaluated
#: again, and the package set selected from it.
Rebuild = Callable[[], Awaitable[AsyncValue]]


@dataclass(frozen=True, slots=True)
class _Set:
    """A top-level set the walk enters, and how many names it holds."""

    name: str
    size: int


def _groups(sets: Sequence[_Set]) -> list[list[str]]:
    """*sets* in groups of about `_GROUP_NAMES` names, in their own order."""
    groups: list[list[str]] = []
    names = 0
    for found in sets:
        if not groups or names + found.size > _GROUP_NAMES:
            groups.append([])
            names = 0
        groups[-1].append(found.name)
        names += found.size
    return groups


@dataclass(frozen=True)
class PackageRecord:
    """One package, as a search reads it.

    There is no store path and no output here. Resolving a path means
    instantiating the derivation, and a search that answers in milliseconds
    cannot do that for 24 571 packages. Issue #85 says to resolve the results
    that the caller can see, and only those.
    """

    #: The attribute path under the top level, dotted: `ripgrep`, or
    #: `python314Packages.requests` for a package in a set the walk enters.
    attr: str
    #: `meta.pname`, or the name when the package states no `pname`.
    pname: str
    #: The version, or an empty string when the package states none.
    version: str
    #: `meta.description`, or `None`.
    description: str | None
    #: `meta.mainProgram`: the one binary the package means the caller to run.
    #: `None` for the 8 502 of 24 571 packages that name none.
    main_program: str | None
    #: `meta.broken`.
    broken: bool
    #: Whether the licence is unfree, or the package is otherwise unavailable.
    unfree: bool


async def fetch_package_list(
    state: AsyncEvalState,
    pkgs_value: AsyncValue,
    lib_value: AsyncValue,
    *,
    rebuild: Rebuild | None = None,
) -> list[PackageRecord]:
    """Extract every package of *pkgs_value*, in the sets it marks too.

    *lib_value* must be a nixpkgs `lib`, and ordinarily the one that
    *pkgs_value* itself carries. *rebuild* makes a fresh package set for each
    group of sets; without it, every group reads *pkgs_value*.

    Each walk runs inside the evaluator and returns one list, so this costs a
    few round trips per group whatever the size of nixpkgs. When an attribute
    raises an error that `tryEval` does not catch, the walk reads each
    attribute from here and skips that one, where `nix search` skips it only
    in a flake's `legacyPackages` (`search.cc:183`).
    """
    collector = await call(await helper(state, "packages"), lib_value)
    entry_at = await collector.get("entryAt")
    raw: list[JsonValue] = []
    with muted_warnings(state):
        # The top level too, because the caller holds *pkgs_value* past the
        # walk, and with it everything the walk forced.
        pkgs = pkgs_value if rebuild is None else await rebuild()
        try:
            top = _object(await to_python(await call(await collector.get("top"), pkgs)))
            raw += _list(top["packages"])
            sets = [_parse_set(found) for found in _list(top["sets"])]
        except NixError:
            sets = await _each_entry(state, entry_at, pkgs, [], raw)
        for names in _groups(sets):
            pkgs = pkgs_value if rebuild is None else await rebuild()
            try:
                raw += _list(
                    await to_python(
                        await call(await collector.get("group"), pkgs, await state.make_string(json.dumps(names)))
                    )
                )
            except NixError:
                for name in names:
                    await _each_entry(state, entry_at, await pkgs.get(name), [name], raw)
    return [_parse_record(_object(entry)) for entry in raw]


async def _each_entry(
    state: AsyncEvalState, entry_at: AsyncValue, value: AsyncValue, path: list[str], packages: list[JsonValue]
) -> list[_Set]:
    """Add each package under *value* to *packages*, without the attributes
    that raise, and return the sets at the top that a group should read.

    Below the top level a set is entered here at once, so the list is empty.
    """
    sets: list[_Set] = []
    for name in await value.names():
        here = [*path, name]
        try:
            child = await value.get(name)
            probed = await to_python(await call(entry_at, await state.make_string(json.dumps(here)), child))
        except NixError as exc:
            logger.info("skipped an attribute that fails to evaluate", attr=".".join(here), error=exc.message)
            continue
        if probed is None:
            continue
        found = _object(probed)
        if "package" in found:
            packages.append(found["package"])
        elif not path:
            sets.append(_Set(name=name, size=_count(found["set"])))
        else:
            await _each_entry(state, entry_at, child, here, packages)
    return sets


def _object(value: object) -> Mapping[str, JsonValue]:
    if not isinstance(value, dict):
        raise TypeError(f"the package walk must return an object here, got {type(value).__name__}")
    return cast("Mapping[str, JsonValue]", value)


def _list(value: object) -> list[JsonValue]:
    if not isinstance(value, list):
        raise TypeError(f"the package walk must return a list here, got {type(value).__name__}")
    return cast("list[JsonValue]", value)


def _count(value: object) -> int:
    if not isinstance(value, int):
        raise TypeError(f"the package walk must count with an integer, got {type(value).__name__}")
    return value


def _parse_set(value: object) -> _Set:
    found = _object(value)
    return _Set(name=str(found["name"]), size=_count(found["size"]))


def _record_fields(record: PackageRecord) -> dict[str, object]:
    """One record, in the shape the walk itself produces.

    **Not `dataclasses.asdict`.** That writes the field names of the class, and
    `_parse_record` reads the names the Nix expression uses: `mainProgram`
    against `main_program`. The two disagreed, so every cached package lost its
    main program and `rg` stopped finding `ripgrep` from a warm cache. Writing
    the walk's own shape keeps one parser for both paths, which is the only
    thing that makes them agree by construction.
    """
    return {
        "attr": record.attr,
        "pname": record.pname,
        "version": record.version,
        "description": record.description,
        "mainProgram": record.main_program,
        "broken": record.broken,
        "unfree": record.unfree,
    }


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _parse_record(entry: Mapping[str, object]) -> PackageRecord:
    return PackageRecord(
        attr=str(entry["attr"]),
        pname=str(entry.get("pname", "")),
        version=str(entry.get("version", "")),
        description=_optional_str(entry.get("description")),
        main_program=_optional_str(entry.get("mainProgram")),
        broken=bool(entry.get("broken", False)),
        unfree=bool(entry.get("unfree", False)),
    )


#: Bump this when `PackageRecord` gains or loses a field, or the walk reads
#: other packages. A cache written by an older `pynix` is then ignored rather
#: than read into the wrong shape or answered with the old set.
_CACHE_VERSION = 2


def cache_path(identity: str) -> Path:
    """Where the walk of the nixpkgs named by *identity* is kept.

    **The identity is a store path, so the cache needs no expiry.** `pkgs.path`
    is the source of nixpkgs in the store, and a store path is the hash of what
    is under it. Evaluation is pure, so the same path gives the same walk for
    ever: a hit is exactly right, and a different pin simply has a different
    file. `search` keys its own cache by the target it was given, and has to
    trust that; this one cannot be stale.
    """
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    directory = cache_home / "pynix" / "packages"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{Path(identity).name}.json"


def _cached_entries(path: Path) -> list[object] | None:
    """The package entries in the cache at *path*, or `None` for anything else.

    A cache is a convenience, so a file that is missing, truncated or written
    by another version is not an error: the caller walks nixpkgs again.
    """
    if not path.is_file():
        return None
    try:
        payload: object = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    fields = cast("dict[str, object]", payload)
    if fields.get("version") != _CACHE_VERSION:
        return None
    packages = fields.get("packages")
    return cast("list[object]", packages) if isinstance(packages, list) else None


def load_cache(path: Path) -> list[PackageRecord] | None:
    """Read a cached walk, or `None` when there is none this version can read.

    The entries go through `_parse_record`, the same function the walk itself
    uses, so a cache and a fresh walk cannot disagree about the shape.
    """
    entries = _cached_entries(path)
    if entries is None:
        return None
    records: list[PackageRecord] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        records.append(_parse_record(cast("Mapping[str, object]", entry)))
    return records


def save_cache(path: Path, identity: str, records: Sequence[PackageRecord]) -> None:
    """Write the walk to *path*, whole or not at all.

    The write goes to a neighbouring file and is then renamed, because two
    `pynix` processes can index at once and a reader must never see half a
    file. A rename within one directory is atomic.
    """
    payload = {
        "version": _CACHE_VERSION,
        "identity": identity,
        "packages": [_record_fields(record) for record in records],
    }
    partial = path.with_suffix(f".{os.getpid()}.partial")
    partial.write_text(json.dumps(payload))
    partial.replace(path)


#: How much of the digest joins the identity. Sixteen hexadecimal characters
#: is 64 bits, which no set of package sets on one machine will collide in,
#: and it keeps the file name readable.
_DIGEST_LENGTH = 16


async def package_identity(state: AsyncEvalState, pkgs_value: AsyncValue) -> str:
    """What names the walk of *pkgs_value*: its source, and what shapes it.

    `pkgs.path` is what nixpkgs calls its own source, and a store path is the
    hash of what is under it. That alone was the key until issue #260, and two
    package sets built from one source shared it: `import <nixpkgs> { }` and
    `import <nixpkgs> { config.allowUnfree = true; }` have the same `path`, so
    the second read the walk of the first.

    `nix/identity.nix` says what else joins the key, and why it cannot simply
    be `pkgs.config`. Measured on real nixpkgs, 27 918 attributes: the facts
    cost 0.043 s against a walk of 15 s.
    """
    await state.force(pkgs_value)
    path = str(await to_python(await pkgs_value.get("path")))
    facts = str(await to_python(await call(await helper(state, "identity"), pkgs_value)))
    digest = hashlib.sha256(facts.encode()).hexdigest()[:_DIGEST_LENGTH]
    return f"{Path(path).name}-{digest}"


async def indexed_packages(
    state: AsyncEvalState,
    pkgs_value: AsyncValue,
    lib_value: AsyncValue,
    *,
    refresh: bool = False,
    rebuild: Rebuild | None = None,
) -> list[PackageRecord]:
    """Return the packages of *pkgs_value*, from the cache when there is one.

    Measured on nixpkgs unstable: the walk costs 15 s and 2.08 GB, and reading
    the cache costs 0.1 s. Pass *refresh* to walk again and overwrite.
    """
    identity = await package_identity(state, pkgs_value)
    path = cache_path(identity)
    if not refresh:
        cached = load_cache(path)
        if cached is not None:
            return cached
    records = await fetch_package_list(state, pkgs_value, lib_value, rebuild=rebuild)
    save_cache(path, identity, records)
    return records
