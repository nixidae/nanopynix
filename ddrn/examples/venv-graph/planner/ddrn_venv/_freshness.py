"""Tell a stale `uv.lock` from a current one.

**A lock file records the declaration it was resolved from, and a project can
grow past it.** A user who adds a dependency to `pyproject.toml` and does not
run `uv lock` has a lock file that resolves the old declaration. uv refuses
such a file: `Lock::satisfies` compares the two and gives one of 21
`SatisfiesResult` values. This lab would otherwise build the old environment
and say nothing, which is the worst answer of the three.

    [package.metadata]
    requires-dist = [{ name = "rich", specifier = ">=13" }]

    [package.metadata.requires-dev]
    ddrn = [{ name = "installer", specifier = ">=1.0.1" }]

This module makes the part of that comparison that is exact, and it refuses to
guess at the rest. `ddrn/PACKAGING-FACTS.md` records what each part costs.

## What this compares, and why only this

`normalize_requirement` of `crates/uv-resolver/src/lock/mod.rs` says what uv
rewrites before it compares. It sorts the extras and the groups, it rebuilds a
git or a path source from its fields, and it calls
`requires_python.simplify_markers` on the marker. **It does not touch the
specifier.**

So the name, the extras and the specifier are exactly comparable, and this
module compares those three. It ignores:

- **the marker**, because uv simplifies it against `requires-python`. A
  declaration of `; python_version >= "3.13"` under `requires-python =
  ">=3.13"` is stored with no marker at all, and a reader that compared the
  text would call every such lock file stale.
- **the source**, because a `tool.uv.sources` entry moves the location out of
  the requirement and into another table. The two sides then spell one
  dependency two ways, and neither spelling is wrong.
- **a dynamic `dependencies` table**, because the file does not hold it. uv
  builds the package to learn such a list, and `_tables` gives the reason.

**A marker-only edit therefore passes this check.** That is a gap, and it is
the honest boundary of a comparison that never raises a false alarm. uv itself
is the tool for the whole answer, and `uv lock --check` is the command. The
`uvCheck` argument of `uv.nix` runs that command, in a derivation that the
planner waits for, and it closes this gap and the eighteen others of
`Lock::satisfies` that this module does not implement.

**A new member of a workspace needs a second comparison**, and `check_members`
makes it. `uv.nix` builds one declaration for each package that `uv.lock`
holds, so a member that the lock file never saw reaches no comparison of
`[package.metadata]`. `workspace.nix` walks the tree during the evaluation, and
`ddrn_venv._workspace` turns that listing into the member set. uv compares the
same two sets, and it calls the difference
`SatisfiesResult::MismatchedMembers`.

**`ddrn_venv._workspace` answers `None` for a rule that reaches further than
it does**, and the comparison is then off. That module names each such case. A
false alarm would stop a build that is correct, which is worse than the gap.

`groups_of` and `_tables` each carry the rule of uv that they follow, with the
file of uv that states it. Every one of those rules came from that source, and
not from the output of a command.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

from ddrn_venv._uvlock import LockError
from ddrn_venv._workspace import members as members_of

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from packaging.utils import NormalizedName

    from ddrn_venv._uvlock import Lock, Package


@dataclass(frozen=True, slots=True, order=True)
class Pin:
    """The part of one requirement that both sides spell the same way."""

    name: str
    extras: tuple[str, ...]
    specifier: str

    @property
    def text(self) -> str:
        """The pin, as a reader of a message wants to see it."""
        extras = f"[{','.join(self.extras)}]" if self.extras else ""
        return f"{self.name}{extras}{self.specifier}"


def from_declaration(text: str) -> Pin:
    """One PEP 508 string of `pyproject.toml`, as a pin."""
    try:
        requirement = Requirement(text)
    except InvalidRequirement as error:
        raise LockError(f"pyproject.toml declares {text!r}, and PEP 508 does not read it") from error
    return Pin(
        name=canonicalize_name(requirement.name),
        extras=tuple(sorted(canonicalize_name(extra) for extra in requirement.extras)),
        specifier=str(SpecifierSet(str(requirement.specifier))),
    )


def from_metadata(entry: Mapping[str, Any]) -> Pin:
    """One row of `[package.metadata]`, as a pin.

    uv writes the requirement as a table, and not as a PEP 508 string, so this
    reads the three fields rather than parse anything.
    """
    return Pin(
        name=canonicalize_name(entry["name"]),
        extras=tuple(sorted(canonicalize_name(extra) for extra in entry.get("extras", []))),
        specifier=str(SpecifierSet(entry.get("specifier", ""))),
    )


def pins(entries: Iterable[Mapping[str, Any]]) -> set[Pin]:
    return {from_metadata(entry) for entry in entries}


def declared(entries: Iterable[str]) -> set[Pin]:
    return {from_declaration(text) for text in entries}


def _difference(locked: set[Pin], stated: set[Pin]) -> str:
    """What changed, as one line for a message, or the empty string."""
    added = sorted(pin.text for pin in stated - locked)
    removed = sorted(pin.text for pin in locked - stated)
    parts: list[str] = []
    if added:
        parts.append(f"pyproject.toml adds {added}")
    if removed:
        parts.append(f"uv.lock still holds {removed}")
    return ", and ".join(parts)


def _include_group(entry: object) -> str | None:
    """The group that an `include-group` entry names, or `None` for any other entry.

    `tomllib` reads a file that the user wrote, so this takes an `object` and
    it tests every step. A PEP 735 Dependency Object Specifier is a table with
    no `include-group` key, and uv refuses that one as well.
    """
    if not isinstance(entry, dict):
        return None
    # `dict[Unknown, Unknown]` is what an `isinstance` test gives, and the keys
    # of a TOML table are strings.
    table = cast("dict[str, object]", entry)
    included = table.get("include-group")
    return included if isinstance(included, str) else None


def groups_of(document: Mapping[str, Any]) -> dict[str, list[str]]:
    """Every dependency group of `pyproject.toml`, as uv records it.

    **A group of the lock file is not the table that the user wrote.**
    `FlatDependencyGroups::from_pyproject_toml`
    (`crates/uv-workspace/src/dependency_groups.rs`) does two things to the
    tables before uv writes `[package.metadata.requires-dev]`, and this
    function does the same two things:

    1. It resolves each `include-group` entry, and it resolves one that names a
       group which itself includes another. The lock file holds the flat list,
       so a comparison against the written table reports a difference that is
       not one.
    2. It adds `tool.uv.dev-dependencies` to the group `dev`. That table is the
       older spelling of a group, and a project that uses it has no
       `[dependency-groups]` at all. A comparison that read the standard table
       alone would call every such project stale.
    """
    tables: Mapping[str, Sequence[Any]] = document.get("dependency-groups", {})
    written: dict[str, Sequence[Any]] = {canonicalize_name(name): entries for name, entries in tables.items()}

    flat: dict[str, list[str]] = {}

    def resolve(name: str, parents: tuple[str, ...]) -> list[str]:
        if name in flat:
            return flat[name]
        if name in parents:
            raise LockError(
                f"pyproject.toml has a cycle in dependency-groups: {' -> '.join([*parents, name])}. "
                "uv refuses such a file as well."
            )
        requirements: list[str] = []
        # An entry is a requirement string or an `include-group` table. uv reads
        # a third form, the PEP 735 Dependency Object Specifier, and refuses it.
        entries: Sequence[Any] = written.get(name, [])
        for entry in entries:
            if isinstance(entry, str):
                requirements.append(entry)
                continue
            included = _include_group(entry)
            if included is None:
                # uv gives `DependencyObjectSpecifierNotSupported` here, for a
                # PEP 735 Dependency Object Specifier and for anything else.
                raise LockError(
                    f"pyproject.toml gives {entry!r} in dependency-groups '{name}'. "
                    "uv supports a requirement string and an include-group entry only."
                )
            # **A group that names an absent group is not this reader's error
            # to report.** uv refuses that file, and this check runs to tell a
            # fresh lock file from a stale one.
            requirements.extend(resolve(canonicalize_name(included), (*parents, name)))
        flat[name] = requirements
        return requirements

    for name in written:
        resolve(name, ())

    legacy: Sequence[str] = document.get("tool", {}).get("uv", {}).get("dev-dependencies", [])
    if legacy:
        flat.setdefault("dev", []).extend(legacy)

    return flat


def _tables(document: Mapping[str, Any], metadata: Mapping[str, Any]) -> list[tuple[str, set[Pin], set[Pin]]]:
    """Every list of requirements that both sides state, paired up.

    The name of each pair is what a message calls it, so a reader knows which
    table of `pyproject.toml` to edit.
    """
    project: Mapping[str, Any] = document.get("project", {})
    pairs: list[tuple[str, set[Pin], set[Pin]]] = []

    # **A dynamic field is not in the file, so no reader can compare it.** uv
    # reads the two tables below out of `pyproject.toml` only while neither is
    # dynamic. `RequiresDist::from_project_maybe_dynamic`
    # (`crates/uv-pypi-types/src/metadata/requires_dist.rs`) gives
    # `MetadataError::DynamicField` for each of them, and uv then builds the
    # package to learn what it needs. The lock file therefore holds a list that
    # the declaration never stated. A dynamic *version* does not do this, and
    # uv keeps the static path for it.
    dynamic: Sequence[str] = project.get("dynamic", [])
    if not ({"dependencies", "optional-dependencies"} & set(dynamic)):
        # **An extra is a row of `requires-dist`, and not a table of its own.**
        # The same function builds that one list, and it puts the extra in the
        # marker of each row that came from an extra:
        #
        #     requires-dist = [{ name = "arpeggio", marker = "extra == 'haxx'" }]
        #     provides-extras = ["haxx"]
        #
        # This module drops every marker, so the two sides compare only when
        # the declared side is the union as well. A comparison against
        # `[project].dependencies` alone would call every project that has an
        # extra stale.
        optional: Mapping[str, Sequence[str]] = project.get("optional-dependencies", {})
        stated = list(project.get("dependencies", []))
        for extra in sorted(optional):
            stated.extend(optional[extra])
        pairs.append(("dependencies", pins(metadata.get("requires-dist", [])), declared(stated)))

    groups = groups_of(document)
    raw_locked: Mapping[str, Sequence[Mapping[str, Any]]] = metadata.get("requires-dev", {})
    locked_groups: dict[str, Sequence[Mapping[str, Any]]] = {
        canonicalize_name(name): entries for name, entries in raw_locked.items()
    }
    pairs.extend(
        (
            f"dependency-groups.{group}",
            pins(locked_groups.get(group, [])),
            declared(groups.get(group, [])),
        )
        for group in sorted(set(groups) | set(locked_groups))
    )
    return pairs


def check_members(lock: Lock, listing: Mapping[str, str] | None) -> None:
    """Refuse a lock file that the workspace has grown a member past.

    `listing` is what `workspace.nix` walked: the `pyproject.toml` of every
    directory under the workspace, keyed by the path of that directory.
    `ddrn_venv._workspace` holds every rule that turns the listing into a
    member set, and it answers `None` for a rule that reaches further than it
    does. `None` on either side turns the comparison off.

    **uv writes an empty `[manifest] members` for a workspace of one member
    that is a project.** `LockTarget::members` of
    `crates/uv/src/commands/project/lock_target.rs` clears the list in that one
    case, and it says why: a workspace of one member mismatches anyway, on the
    member itself. So this reader clears the same list before it compares, and
    a single project therefore compares an empty set with an empty set.
    """
    if not listing:
        return

    found = members_of(listing)
    if found is None:
        return

    expected: set[NormalizedName] = {canonicalize_name(name) for name in found.names}
    if len(expected) == 1 and found.root is not None:
        expected = set()

    recorded = {canonicalize_name(name) for name in lock.members}
    if expected == recorded:
        return

    parts: list[str] = []
    added = sorted(expected - recorded)
    removed = sorted(recorded - expected)
    if added:
        parts.append(f"the workspace adds {added}")
    if removed:
        parts.append(f"the lock file still holds {removed}")
    raise LockError(
        f"uv.lock is stale: it resolved a different set of workspace members. "
        f"{', and '.join(parts)}. Run `uv lock` and build again."
    )


def check(
    lock: Lock,
    declarations: Mapping[str, str],
    listing: Mapping[str, str] | None = None,
) -> None:
    """Refuse a lock file that the projects have grown past.

    `declarations` maps the name of a member to the store path of its
    `pyproject.toml`. An empty mapping turns the check off, which is what a
    lock document with no project behind it gives.

    `listing` is what `check_members` takes, and it answers the half that
    `declarations` cannot reach: a member directory that the lock file never
    saw is in no declaration, because `uv.nix` builds one declaration for each
    package that the lock file holds.
    """
    check_members(lock, listing)

    by_name: dict[str, Package] = {package.name: package for package in lock.packages}

    for raw_name, path in sorted(declarations.items()):
        name = canonicalize_name(raw_name)
        package = by_name.get(name)
        if package is None:
            raise LockError(
                f"pyproject.toml of '{raw_name}' is a member of this workspace, "
                f"and uv.lock holds no package of that name. Run `uv lock`."
            )

        # **A lock file of an older uv writes no metadata**, and a reader
        # cannot tell that from a member that declares nothing. uv has the same
        # case, and it calls it `allow_missing_package_metadata`.
        if not package.metadata:
            continue

        document = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        for table, locked, stated in _tables(document, package.metadata):
            if locked == stated:
                continue
            difference = _difference(locked, stated)
            if not difference:
                continue
            raise LockError(
                f"uv.lock is stale: '{raw_name}' declares different {table} than the lock file "
                f"resolved. {difference}. Run `uv lock` and build again."
            )
