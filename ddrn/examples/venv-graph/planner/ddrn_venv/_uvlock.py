"""Read `uv.lock`, and resolve it for one target.

**This module invents nothing about packaging.** `packaging` answers every
question that a PEP defines, and this module answers only the questions that
the format of `uv.lock` defines:

- PEP 503 normalises each name, so `flit_core` and `flit-core` are one name.
- PEP 440 compares each version, so an edge that pins `2.0.1` finds the
  package that the lock calls `2.0.1`.
- PEP 508 evaluates each marker, and the environment decides.
- PEP 621 names `optional-dependencies`, which are extras.
- PEP 735 names the dependency groups, which uv writes as `dev-dependencies`.

`uv2nix` resolves the same file, in the Nix language. It re-implements PEP 508
and PEP 440 to do that (`pyproject.nix/lib/pep508.nix`, `lib/pep440.nix`).
This module calls the reference implementation instead, so a disagreement with
uv is a defect of `packaging` and not of this lab.

**uv reads the same file, and `crates/uv-resolver/src/lock/mod.rs` is the
authority for the format.** `PackageIdForDependency::unwire` of that file says
what identifies one entry, and `Installable::to_resolution` of
`lock/installable.rs` says how uv walks the edges. This module follows both,
and it names the function beside each decision that it takes from them.

**The lock file records no build system**, so a source distribution of the
lock becomes a node of kind `sdist`, which is a planner that reads the archive.
`ddrn/PACKAGING-FACTS.md` gives the measurement and the consequence.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

    #: Reads a list of marker texts, and gives back the parsed markers. One
    #: parse of a lock file shares one of these, so a text that repeats across
    #: the file is parsed once.
    MarkerParser = Callable[[Iterable[str]], tuple[Marker, ...]]

#: The only `version` that this reader accepts. uv writes `1` in every lock
#: file that this lab measured, and `uv2nix` asserts the same value.
LOCK_VERSION = 1

#: The keys of a source that mean "this package is a local directory, and not
#: an artefact of an index". `Source::is_source_tree` of uv answers with the
#: same three, and no other kind.
LOCAL_SOURCES = ("editable", "directory", "virtual")

#: What uv writes into the marker of an edge when a `conflicts` declaration
#: reaches that edge.
#:
#: **Such a marker is not a PEP 508 marker.** uv says so itself, in the doc
#: comment of `UniversalMarker` (`crates/uv-resolver/src/universal_marker.rs`):
#: "we technically preserve PEP 508 compatibility but abuse the `extra`
#: attribute to encode conflicts". The value of the comparison is
#: `extra-<n>-<package>-<extra>`, `group-<n>-<package>-<group>` or
#: `project-<n>-<package>`, and `<n>` is the length of the package name in
#: bytes, so that a reader can find where the package name ends.
CONFLICT_VALUE = re.compile(r"'(extra|group|project)-(\d+)-([^']*)'")


class LockError(Exception):
    """The lock file, or the resolution of it, is not usable."""


@dataclass(frozen=True, slots=True)
class Known:
    """What one entry of an unambiguous name gives to an edge that omits it."""

    version: str | None
    source: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class File:
    """One artefact of a package: a wheel, or the source distribution.

    uv writes the location three ways, and exactly one of them appears:
    `url` for an index, `path` for a local registry, and `filename` for a
    package whose registry is a directory.
    """

    url: str = ""
    path: str = ""
    filename: str = ""
    hash: str = ""
    size: int | None = None
    subdirectory: str | None = None

    @classmethod
    def parse(cls, entry: Mapping[str, Any]) -> File:
        return cls(
            url=entry.get("url", ""),
            path=entry.get("path", ""),
            filename=entry.get("filename", ""),
            hash=entry.get("hash", ""),
            size=entry.get("size"),
            subdirectory=entry.get("subdirectory"),
        )

    @property
    def name(self) -> str:
        """The file name that the index gave this artefact.

        `installer` reads the name of a wheel, and the store path of a fetch
        carries a hash in front of it, so the name has to travel separately.
        """
        if self.filename:
            return self.filename
        return (self.url or self.path).rsplit("/", 1)[-1]

    @property
    def key(self) -> str:
        """What identifies this artefact to the Nix half of the example.

        `uv.nix` makes one `fetchurl` for each artefact of the lock file, and
        it keys the menu by this string. A URL is what uv itself uses to identify
        an artefact, so the two halves agree by construction, and no translation
        sits between them.
        """
        return self.url or self.path or self.filename


@dataclass(frozen=True, slots=True)
class PackageId:
    """What identifies one `[[package]]` entry, and one edge that names it.

    **The name alone does not identify a package, and the version does not
    either.** uv calls this triple a `PackageId`, and the source is a part of
    it. Two entries of one lock file may carry the same name and the same
    version and come from two places, and uv installs a different file for
    each one. The lock file of a project that pins `torch` to one index for
    the CPU and to another index for CUDA is that shape, and uv writes it in
    its own tests (`crates/uv/tests/lock/lock.rs`).

    `source` is the source table of the lock file as canonical text, because
    uv compares the whole table and this reader must compare the same thing.

    `version` is a parsed version, so `2.0.1` and `2.0.1.0` are one version.
    PEP 440 says so, and uv parses the version for the same reason. It is
    `None` for a package whose version is dynamic.
    """

    name: str
    version: Version | None
    source: str

    @property
    def label(self) -> str:
        """The three parts, for a message that a reader sees."""
        version = f" {self.version}" if self.version else ""
        return f"{self.name}{version} from {self.source}"


@dataclass(frozen=True, slots=True)
class Edge:
    """One dependency of a package, as uv resolved it.

    **The edge names exactly one entry of the lock file, and never a choice.**
    uv writes the version and the source on an edge whenever the name is
    ambiguous, and it omits both when the name is not
    (`PackageIdForDependency::unwire`). `_target` puts back what the file omits,
    so `target` is complete here even when the entry of the file was short.

    `extras` names the extras of the target that this edge asks for, which is
    `name[extra]` in the notation of PEP 508.
    """

    target: PackageId
    extras: tuple[str, ...] = ()
    marker: Marker | None = None

    @property
    def name(self) -> str:
        return self.target.name

    def holds(self, environment: Mapping[str, str]) -> bool:
        """Whether this edge exists for the given PEP 508 environment."""
        return self.marker is None or self.marker.evaluate(dict(environment))


@dataclass(frozen=True, slots=True)
class Package:
    """One `[[package]]` entry of the lock file."""

    name: str
    #: The name as the lock file writes it, before PEP 503 normalisation.
    raw_name: str
    #: `None` for a project whose version is dynamic. Six packages of the 22
    #: lock files that `ddrn/PACKAGING-FACTS.md` measures have no version, and
    #: each one is a local project, so the name alone identifies it.
    version: str | None
    source: Mapping[str, Any]
    resolution_markers: tuple[Marker, ...] = ()
    dependencies: tuple[Edge, ...] = ()
    # A subscripted alias is callable and gives an empty container, and it
    # also tells the type checker what that container holds. A bare `dict`
    # here leaves both parameters unknown.
    optional_dependencies: Mapping[str, tuple[Edge, ...]] = field(default_factory=dict[str, tuple["Edge", ...]])
    dev_dependencies: Mapping[str, tuple[Edge, ...]] = field(default_factory=dict[str, tuple["Edge", ...]])
    wheels: tuple[File, ...] = ()
    sdist: File | None = None
    #: The `[package.metadata]` table, which holds the declaration that this
    #: entry was resolved from. uv writes it so that `Lock::satisfies` can tell
    #: a stale lock file from a current one, and `ddrn_venv._freshness` reads
    #: it for the same purpose. **No resolution reads it**: the edges above
    #: already hold what the resolution decided.
    metadata: Mapping[str, Any] = field(default_factory=dict[str, Any])

    @property
    def id(self) -> PackageId:
        """What identifies this entry, and what an edge names to reach it."""
        return PackageId(
            name=self.name,
            version=_version(self.version, self.name),
            source=_source_key(self.source),
        )

    @property
    def label(self) -> str:
        """The name and the version, for a message that a reader sees."""
        return f"{self.raw_name}-{self.version}" if self.version else self.raw_name

    @property
    def is_local(self) -> bool:
        """Whether this package is a directory, and not an artefact."""
        return any(kind in self.source for kind in LOCAL_SOURCES)

    @property
    def is_git(self) -> bool:
        """Whether this package is a git checkout, and not an artefact.

        **A git source has no `wheels` and no `sdist` at all.** uv records the
        whole location in one string, and the fragment of it is the commit
        that the resolution pinned:

            source = { git = "https://github.com/pypa/hatch.git\
        ?subdirectory=backend&rev=hatchling-v1.27.0#cbf6598e5cbce3ba…" }

        So a build of this package reads a directory, the same way a build of
        a `directory` source does, and `uv.nix` supplies that directory.
        """
        return "git" in self.source

    @property
    def is_url(self) -> bool:
        """Whether this package is an archive that a URL names directly.

        **The `sdist` entry of such a package carries the hash and no URL.**
        uv keeps the location in `source`, beside the subdirectory:

            source = { url = "…/hatchling-v1.27.0.tar.gz", subdirectory = "backend" }
            sdist = { hash = "sha256:c5d576a0…" }

        `uv.nix` joins the two, so what reaches the planner is an ordinary
        source distribution that happens to carry a subdirectory.
        """
        return "url" in self.source

    @property
    def is_path(self) -> bool:
        """Whether this package is a file that this machine already holds.

        **The location is on the source, and the row of the artefact carries
        no address.** uv writes a path relative to the project, and the row
        gives the file name and the hash alone:

            source = { path = "vendor/vendored-1.0.0-py3-none-any.whl" }
            wheels = [ { filename = "vendored-1.0.0-py3-none-any.whl", hash = "sha256:7353…" } ]

        A file of either shape reaches the graph the same way that an artefact
        of an index reaches it. `uv.nix` copies the file into the store with
        `builtins.path` instead of a fetch, so a wheel installs and an archive
        builds through its backend.

        uv calls the source kind `Source::Path`. A registry that is a
        directory writes the same key on the row of the artefact, which uv
        calls `WheelWireSource::Path`; `ddrn/PACKAGING-FACTS.md` gives the
        three shapes.
        """
        return "path" in self.source

    @property
    def is_virtual(self) -> bool:
        """Whether this package carries edges and installs nothing.

        `tool.uv.package = false` makes a project virtual. It takes part in
        the resolution, and no node of the graph builds it.
        """
        return "virtual" in self.source

    def reachable(self, environment: Mapping[str, str]) -> bool:
        """Whether this entry is the one that this environment resolves to.

        A package of a forked resolution carries `resolution-markers`, and the
        fork that this environment takes is the one whose marker holds. A
        package with no such marker takes part in every fork.

        **This decides a root, and it decides nothing else.** uv uses these
        markers in `Lock::find_by_markers`, which answers a request that names
        a package and no version. Every other choice of uv comes from the edge,
        which already names one entry. uv itself keeps these markers for a
        different purpose as well, and the field says so: it sets "the correct
        preferences in the next resolution".
        """
        if not self.resolution_markers:
            return True
        return any(marker.evaluate(dict(environment)) for marker in self.resolution_markers)

    def edges(self, extras: Iterable[str], groups: Iterable[str]) -> Iterator[Edge]:
        """The edges that this package contributes, for the given selection.

        **The selection matters, and `uv2nix` does not use one.** Its resolver
        follows every extra and every group of every package
        (`lib/lock1.nix:133`), which is why its own tests for "with optionals"
        and "no optionals" expect the same answer. This follows an extra only
        when an edge asked for it, and a group only for a package that the
        caller named.
        """
        yield from self.dependencies
        for extra in extras:
            yield from self.optional_dependencies.get(extra, ())
        for group in groups:
            yield from self.dev_dependencies.get(group, ())


@dataclass(frozen=True, slots=True)
class Lock:
    """A parsed `uv.lock`."""

    version: int
    revision: int | None
    requires_python: SpecifierSet
    packages: tuple[Package, ...]
    resolution_markers: tuple[Marker, ...] = ()
    supported_markers: tuple[Marker, ...] = ()
    required_markers: tuple[Marker, ...] = ()
    #: Each entry is one declaration: a list of `{package, extra}` or
    #: `{package, group}` tables that may not be enabled together.
    conflicts: tuple[tuple[Mapping[str, str], ...], ...] = ()
    #: The workspace members, from `[manifest]`.
    members: tuple[str, ...] = ()

    @classmethod
    def read(cls, path: str | Path) -> Lock:
        """Parse the lock file at `path`."""
        with Path(path).open("rb") as handle:
            return cls.parse(tomllib.load(handle))

    @classmethod
    def parse(cls, document: Mapping[str, Any]) -> Lock:
        """Parse an unmarshalled lock file.

        A marker text repeats across a lock file, so this parses each distinct
        text once and shares the result. `uv2nix` does the same, for the same
        reason (`lib/lock1.nix:342`).
        """
        version = document.get("version")
        if version != LOCK_VERSION:
            raise LockError(f"uv.lock says version {version!r}, and this reader accepts {LOCK_VERSION}")

        cache: dict[str, Marker] = {}
        markers = _markers(cache)

        # **An edge omits the version and the source when the name of the
        # target is unambiguous.** uv counts the entries of each name first,
        # for exactly this reason, and its comment gives it: the two fields are
        # "strictly redundant" for a name that one entry holds. `_target` reads
        # this map, and it refuses an edge that omits a field it needs.
        entries = document.get("package", [])
        unambiguous = _unambiguous(entries)

        return cls(
            version=version,
            # `revision` is absent from one of the 22 lock files that this lab
            # measured, and it is 1, 2 or 3 in the others. Nothing reads it.
            revision=document.get("revision"),
            requires_python=SpecifierSet(document.get("requires-python", "")),
            packages=tuple(_package(entry, markers, unambiguous) for entry in entries),
            resolution_markers=markers(document.get("resolution-markers", [])),
            supported_markers=markers(document.get("supported-markers", [])),
            required_markers=markers(document.get("required-markers", [])),
            conflicts=tuple(tuple(declaration) for declaration in document.get("conflicts", [])),
            members=tuple(document.get("manifest", {}).get("members", [])),
        )

    def find(self, name: str, environment: Mapping[str, str]) -> Package:
        """The one entry of `name` that this environment takes.

        This is `Lock::find_by_markers` of uv. One entry of a name is the
        answer at once. More than one is a forked resolution, and the
        `resolution-markers` of the fork decide which one this environment
        takes. uv raises when more than one survives that, and so does this.
        """
        matching = [package for package in self.packages if package.name == name]
        if not matching:
            raise LockError(f"the selection names '{name}', and the lock file holds no such package")
        if len(matching) == 1:
            return matching[0]
        live = [package for package in matching if package.reachable(environment)]
        if len(live) != 1:
            found = ", ".join(package.label for package in (live or matching))
            raise LockError(
                f"the lock file holds more than one package named '{name}', and the resolution "
                f"markers of this environment do not decide between them: {found}"
            )
        return live[0]

    def check_environment(self, environment: Mapping[str, str]) -> None:
        """Raise unless this lock file covers the given environment.

        Three assertions, and `uv2nix` makes the same three
        (`lib/overlays.nix:126`). `requires-python` is the constraint of the
        project. `supported-markers` and `required-markers` come from
        `tool.uv.environments` and `tool.uv.required-environments`, which name
        what the resolution covers.

        **`resolve` calls this before it evaluates one marker, and the order is
        load bearing.** uv simplifies each marker of a lock file against
        `requires-python` before it writes that marker, and it says what such a
        marker needs: "A simplified marker should only exist in contexts where a
        `requires-python` setting can be assumed"
        (`SimplifiedMarkerTree`, `crates/uv-distribution-types/src/requires_python.rs`).
        uv puts the constraint back with `into_marker`. This module asserts the
        constraint instead, and it therefore reads each marker as written.
        """
        full_version = environment["python_full_version"]
        if full_version not in self.requires_python:
            raise LockError(
                f"the lock file needs a Python of '{self.requires_python}', and the target is {full_version}"
            )
        for label, group in (("supported", self.supported_markers), ("required", self.required_markers)):
            if group and not any(marker.evaluate(dict(environment)) for marker in group):
                raise LockError(f"no {label} environment of the lock file holds for this target")


@dataclass(frozen=True, slots=True)
class Resolution:
    """One package for each name that the target needs."""

    packages: tuple[Package, ...]

    def by_name(self) -> dict[str, Package]:
        return {package.name: package for package in self.packages}

    @property
    def members(self) -> tuple[Package, ...]:
        """The packages that a node of the graph builds.

        A virtual package takes part in the resolution and installs nothing,
        so it is not a member.
        """
        return tuple(package for package in self.packages if not package.is_virtual)


def filter_conflicts(lock: Lock, spec: Mapping[str, Sequence[str]]) -> Lock:
    """Remove each extra and each group that this selection did not take.

    A `conflicts` declaration names extras and groups that may not be enabled
    together. The caller enables one of them, and this removes the others from
    the lock file. The resolver then reads a lock file that has no conflict,
    which is the same order that `uv2nix` uses (`lib/lock1.nix:250`).
    """
    if not lock.conflicts:
        return lock

    dropped: dict[str, set[tuple[str, str]]] = {}
    for declaration in lock.conflicts:
        taken = [item for item in declaration if _conflict_name(item) in _selection(spec, item["package"])]
        if len(taken) > 1:
            names = ", ".join(f"{item['package']}:{_conflict_name(item)}" for item in taken)
            raise LockError(f"the selection takes more than one branch of one conflict declaration: {names}")
        if not taken:
            # The declaration is about extras that this resolution does not
            # take at all, so it constrains nothing.
            continue
        for item in declaration:
            if item in taken:
                continue
            key = canonicalize_name(item["package"])
            dropped.setdefault(key, set()).add(("extra" if "extra" in item else "group", _conflict_name(item)))

    def prune(package: Package) -> Package:
        removals = dropped.get(package.name)
        if not removals:
            return package
        extras = {
            name: edges for name, edges in package.optional_dependencies.items() if ("extra", name) not in removals
        }
        groups = {name: edges for name, edges in package.dev_dependencies.items() if ("group", name) not in removals}
        return replace(package, optional_dependencies=extras, dev_dependencies=groups)

    return replace(lock, packages=tuple(prune(package) for package in lock.packages), conflicts=())


def resolve(
    lock: Lock,
    environment: Mapping[str, str],
    spec: Mapping[str, Sequence[str]],
) -> Resolution:
    """The one package for each name that this target and selection need.

    `spec` maps the name of a root package to the extras and the groups to
    enable for it. The roots are the workspace members, and `lock.members`
    names them.

    Three steps, and `Installable::to_resolution` of uv takes the same three:

    1. Remove the conflicting extras that the selection did not take.
    2. Find the entry of each root.
    3. Walk the live edges from the roots, and follow each one to the entry
       that it names.

    **There is no fourth step, and `uv2nix` needs one.** Its resolver reads the
    version of an edge and not the source, so a name that two entries carry
    stays a choice, and it runs a fixed point over the versions to settle that
    choice (`lib/lock1.nix:146`). uv writes the source on such an edge, so the
    edge already names one entry. This module reads that source.
    """
    lock = filter_conflicts(lock, spec)
    lock.check_environment(environment)

    live = _live(lock, environment)
    # `canonicalize_name` gives a `NormalizedName`, which is a `str` under a
    # new name. The annotation keeps the key type plain, because a `Mapping`
    # is invariant in its key.
    groups: dict[str, set[str]] = {canonicalize_name(name): set(choices) for name, choices in spec.items()}
    roots = {lock.find(name, environment).id: choices for name, choices in groups.items()}

    reached = _walk(live, roots)
    if not reached:
        raise LockError("the selection reached no package at all")

    return Resolution(packages=_one_of_each_name(reached))


def build_requirements(
    lock: Lock,
    environment: Mapping[str, str],
    requirements: Iterable[Requirement],
) -> list[Requirement]:
    """The closure of a `build-system.requires`, as one exact pin for each name.

    **A backend needs its own dependencies, and PEP 517 says so.** The frontend
    installs `requires` *and what those need* into the build environment.
    `build-system.requires = ["hatchling"]` therefore names one package of five:
    `hatchling` also needs `packaging`, `pathspec`, `pluggy` and
    `trove-classifiers` to import at all. `flit_core` needs nothing, which is
    why the `lock.json` example never met this rule.

    **`uv.lock` records no build system**, so nothing here can come from the
    `[build-system]` table alone. The lock file does hold the edges of any
    package that it lists, so a backend that a dependency group pins resolves
    the same way every other package does. `projects/README.md` gives the
    `uv add --group` command that puts one there.
    """
    wanted = {canonicalize_name(requirement.name) for requirement in requirements}
    if not wanted:
        return []

    try:
        resolution = resolve(lock, environment, {name: [] for name in wanted})
    except LockError as error:
        raise LockError(
            f"the lock file does not hold the build system {sorted(wanted)}. **uv.lock records no "
            "build system**, so a backend reaches it only as an ordinary package. Pin each one in "
            "a dependency group, with `uv add --group ddrn <name>`, and lock the project again. "
            f"({error})"
        ) from error

    return [
        Requirement(f"{package.raw_name}=={package.version}")
        for package in sorted(resolution.members, key=lambda package: package.name)
    ]


def _live(lock: Lock, environment: Mapping[str, str]) -> dict[PackageId, Package]:
    """Every entry of the lock file, with the edges that this environment takes.

    **One filter, and it is the marker of each edge.** uv evaluates that marker
    and nothing else during the walk (`Installable::to_resolution`). A package
    that no live edge reaches drops out because nothing reaches it, and not
    because a filter removed it first.
    """
    return {
        package.id: replace(
            package,
            dependencies=_holding(package.dependencies, environment),
            optional_dependencies={
                name: _holding(edges, environment) for name, edges in package.optional_dependencies.items()
            },
            dev_dependencies={name: _holding(edges, environment) for name, edges in package.dev_dependencies.items()},
        )
        for package in lock.packages
    }


def _walk(live: Mapping[PackageId, Package], roots: Mapping[PackageId, set[str]]) -> dict[PackageId, Package]:
    """Every entry that the roots reach, by the edges that hold.

    `roots` maps the entry of each root to the extras and the groups that the
    caller enabled for it. A group is a property of a root only, and uv reads
    the groups of the roots outside its own walk for the same reason.
    """
    requested: dict[PackageId, set[str]] = {}
    reached: dict[PackageId, Package] = {}
    queue: deque[PackageId] = deque()

    def want(target: PackageId, extras: Iterable[str]) -> None:
        package = live.get(target)
        if package is None:
            raise LockError(f"an edge names {target.label}, and the lock file holds no such package")
        before = requested.get(target, set())
        requested[target] = before | set(extras)
        if target not in reached or requested[target] != before:
            reached[target] = package
            queue.append(target)

    for target, choices in roots.items():
        want(target, choices)

    while queue:
        target = queue.popleft()
        groups = roots.get(target, set())
        for edge in _edges_for(reached[target], requested[target], groups):
            want(edge.target, edge.extras)

    return reached


def _one_of_each_name(reached: Mapping[PackageId, Package]) -> tuple[Package, ...]:
    """The reached entries, unless two of them carry one name.

    **An environment installs one distribution for each name.** Two entries of
    one name are two forks, and a selection that reaches both asks for a
    directory that cannot exist. The edges decide which fork this environment
    takes, so this states what they decided rather than choose for them.
    """
    grouped: dict[str, list[Package]] = {}
    for package in reached.values():
        grouped.setdefault(package.name, []).append(package)

    clashing = {name: [package.label for package in group] for name, group in grouped.items() if len(group) > 1}
    if clashing:
        raise LockError(f"the selection reaches more than one entry of one name: {clashing}")
    return tuple(package for group in grouped.values() for package in group)


def _edges_for(package: Package, extras: set[str], groups: set[str]) -> Iterator[Edge]:
    """The edges of `package`, for the extras and the groups that were asked.

    An extra that this package does not have is not an error. A root of a
    workspace names its own extras and its own groups in one list, so a name
    of that list may be either.
    """
    return package.edges(extras & set(package.optional_dependencies), groups)


def _unambiguous(entries: Iterable[Mapping[str, Any]]) -> dict[str, Known]:
    """The entry of each name that only one entry carries.

    This is the first loop of `TryFrom<LockWire> for Lock` in uv, and the
    comment of that loop gives the reason: the version and the source of an
    edge are "strictly redundant" when one entry holds the name, so uv leaves
    both out. A name that a second entry carries is ambiguous, and it stays
    out of this map even if a third entry carries it as well.
    """
    seen: dict[str, Known] = {}
    ambiguous: set[str] = set()
    for entry in entries:
        name = canonicalize_name(entry["name"])
        if name in ambiguous:
            continue
        if name in seen:
            del seen[name]
            ambiguous.add(name)
            continue
        seen[name] = Known(version=entry.get("version"), source=entry.get("source", {}))
    return seen


def _package(entry: Mapping[str, Any], markers: MarkerParser, unambiguous: Mapping[str, Known]) -> Package:
    raw_name = entry["name"]
    return Package(
        name=canonicalize_name(raw_name),
        raw_name=raw_name,
        version=entry.get("version"),
        source=entry.get("source", {}),
        resolution_markers=markers(entry.get("resolution-markers", [])),
        dependencies=_edges(entry.get("dependencies", []), markers, unambiguous),
        optional_dependencies={
            name: _edges(edges, markers, unambiguous) for name, edges in entry.get("optional-dependencies", {}).items()
        },
        dev_dependencies={
            name: _edges(edges, markers, unambiguous) for name, edges in entry.get("dev-dependencies", {}).items()
        },
        wheels=tuple(File.parse(wheel) for wheel in entry.get("wheels", [])),
        sdist=File.parse(entry["sdist"]) if entry.get("sdist") else None,
        metadata=entry.get("metadata", {}),
    )


def _edges(
    entries: Iterable[Mapping[str, Any]],
    markers: MarkerParser,
    unambiguous: Mapping[str, Known],
) -> tuple[Edge, ...]:
    return tuple(
        Edge(
            target=_target(entry, unambiguous),
            extras=tuple(entry.get("extra", [])),
            marker=_marker(entry, markers),
        )
        for entry in entries
    )


def _target(entry: Mapping[str, Any], unambiguous: Mapping[str, Known]) -> PackageId:
    """The one entry that this edge names, with what the file leaves out.

    `PackageIdForDependency::unwire` of uv does this, and it raises the same
    two errors. An edge that omits a field which the map cannot supply names
    more than one entry, and uv refuses to read such a lock file rather than
    choose an entry.
    """
    name = canonicalize_name(entry["name"])
    known = unambiguous.get(name)

    source = entry.get("source")
    if source is None:
        if known is None:
            raise LockError(
                f"the dependency '{name}' names no source, and the lock file holds more than one "
                "package of that name. uv writes the source of an edge whenever the name is "
                "ambiguous, so a file that omits it here names no one package."
            )
        source = known.source

    version = entry.get("version")
    if version is None:
        if known is not None:
            version = known.version
        elif not _is_source_tree(source):
            raise LockError(
                f"the dependency '{name}' names no version, and the lock file holds more than one "
                "package of that name. Only a directory may omit the version, because the version "
                "of a project is dynamic and this source is not a directory."
            )

    return PackageId(name=name, version=_version(version, name), source=_source_key(source))


def _marker(entry: Mapping[str, Any], markers: MarkerParser) -> Marker | None:
    """The marker of one edge, unless uv encoded a conflict into it.

    **A conflict marker is not a PEP 508 marker, and `packaging` reads it as
    one.** uv writes `extra == 'extra-7-example-foo'` to mean "the extra `foo`
    of the package `example` is enabled", and it evaluates that against the
    extras that the caller enabled. `packaging` compares it against the `extra`
    of the environment, which is empty here, so every such edge would quietly
    disappear, and one `extra !=` would quietly stay. Neither answer is the
    answer of uv.
    """
    text = entry.get("marker")
    if not text:
        return None
    encoded = CONFLICT_VALUE.findall(text)
    if encoded:
        raise LockError(
            f"the dependency '{entry['name']}' carries the marker {text!r}, and that marker holds a "
            f"conflict of uv: {', '.join(_decode_conflict(*item) for item in encoded)}. uv encodes a "
            "conflict as an `extra` comparison, and `packaging` reads such a comparison as PEP 508. "
            "This reader answers no conflict that reaches an edge, and it refuses rather than drop "
            "the edge. Resolve the project with no `conflicts` declaration."
        )
    return markers([text])[0]


def _decode_conflict(kind: str, length: str, rest: str) -> str:
    """One encoded conflict of uv, in words.

    The number is the length of the package name in bytes, and uv puts it
    there so that a reader can find where that name ends. `encode_package_extra`
    of `crates/uv-resolver/src/universal_marker.rs` writes it.
    """
    package = rest[: int(length)]
    name = rest[int(length) + 1 :]
    if kind == "project":
        return f"the package '{package}'"
    return f"the {kind} '{name}' of the package '{package}'"


def _source_key(source: Mapping[str, Any]) -> str:
    """The source table as canonical text, so that two of them compare.

    uv compares the whole `Source`, and a registry, a git repository, a URL and
    a directory are each a table of the lock file. The text of that table with
    its keys in order is the same comparison.
    """
    return json.dumps(source, sort_keys=True, separators=(",", ":"))


def _is_source_tree(source: Mapping[str, Any]) -> bool:
    """Whether this source is a directory, which `Source::is_source_tree` asks."""
    return any(kind in source for kind in LOCAL_SOURCES)


def _version(text: str | None, name: str) -> Version | None:
    """One version of the lock file, as PEP 440 reads it."""
    if text is None:
        return None
    try:
        return Version(text)
    except InvalidVersion as error:
        raise LockError(f"'{name}' names the version {text!r}, and PEP 440 does not read it") from error


def _markers(cache: dict[str, Marker]) -> MarkerParser:
    """A parser that reads each distinct marker text once."""

    def parse(texts: Iterable[str]) -> tuple[Marker, ...]:
        result: list[Marker] = []
        for text in texts:
            if text not in cache:
                cache[text] = Marker(text)
            result.append(cache[text])
        return tuple(result)

    return parse


def _holding(edges: Iterable[Edge], environment: Mapping[str, str]) -> tuple[Edge, ...]:
    return tuple(edge for edge in edges if edge.holds(environment))


def _conflict_name(item: Mapping[str, str]) -> str:
    """The extra or the group that one item of a conflict declaration names."""
    name = item.get("extra") or item.get("group")
    if name is None:
        raise LockError(f"a conflict declaration names neither an extra nor a group: {dict(item)}")
    return name


def _selection(spec: Mapping[str, Sequence[str]], package: str) -> set[str]:
    """What the selection enables for `package`, and nothing when it omits it.

    **A package that the selection does not name enables no extra and no
    group**, so a conflict declaration about that package constrains nothing.
    This is the same answer as a selection that names the package and takes no
    branch of the declaration, and `filter_conflicts` leaves both alone.

    A workspace is the case that needs it. `select` names the members to
    install, and a member that it leaves out may still declare conflicting
    extras of its own. Those extras reach no environment, so they can decide
    nothing about the members that `select` did name.
    """
    wanted = canonicalize_name(package)
    for name, choices in spec.items():
        if canonicalize_name(name) == wanted:
            return set(choices)
    return set()
