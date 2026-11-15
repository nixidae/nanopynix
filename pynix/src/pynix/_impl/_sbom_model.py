"""The records of ``pynix sbom``, and the inventory that each format renders.

``nix/sbom.nix`` gives one record for each output in the build closure. The
store gives the runtime closure and its references. :func:`inventory` joins
the two as bombon's ``transform`` does (``rust/transformer/src/transform.rs``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import cast
from urllib.parse import quote

#: Outputs that hold documentation. bombon leaves them out of every SBOM.
_DOC_OUTPUTS = frozenset({"doc", "man"})

#: ``DrvName`` ends the name at the first dash that a non-letter follows.
_VERSION_DASH = re.compile(r"-(?![A-Za-z])")


@dataclass(frozen=True, slots=True)
class License:
    """One license of a package: an SPDX identifier, or a name when it has none."""

    spdx_id: str | None
    name: str | None


@dataclass(frozen=True, slots=True)
class Source:
    """Where the source of a package comes from, and its hash in SRI form."""

    #: Archive URLs.
    urls: tuple[str, ...]
    hash: str | None
    #: The repository a git fetch reads (``gitRepoUrl``), and the revision it checks out.
    repository: str | None = None
    rev: str | None = None


@dataclass(frozen=True, slots=True)
class Maintainer:
    """A nixpkgs maintainer of a package: ``meta.maintainers``."""

    name: str | None
    email: str | None
    github: str | None


class Origin(StrEnum):
    """What a record was read from, so a reader knows what it can lack."""

    #: An evaluated derivation value: ``meta`` included, with licenses and CPEs.
    VALUE = "value"
    #: The output's ``.drv``: name, version, the fetch behind ``src``, patches. No ``meta``.
    DERIVATION = "derivation"
    #: The store path's name alone.
    NAME = "name"


@dataclass(frozen=True, slots=True)
class Record:
    """One output of a derivation, with what Nix knows about its package."""

    path: str
    name: str
    version: str | None = None
    output_name: str | None = None
    output_hash: str | None = None
    licenses: tuple[License, ...] = ()
    #: An SPDX expression for a compound license (``AND``, ``WITH``, ``+``); it replaces *licenses*.
    license_expression: str | None = None
    homepages: tuple[str, ...] = ()
    changelogs: tuple[str, ...] = ()
    download_page: str | None = None
    description: str | None = None
    long_description: str | None = None
    main_program: str | None = None
    #: ``meta.position``: the file and line that define the package.
    position: str | None = None
    maintainers: tuple[Maintainer, ...] = ()
    unfree: bool | None = None
    insecure: bool | None = None
    broken: bool | None = None
    known_vulnerabilities: tuple[str, ...] = ()
    cpe: str | None = None
    possible_cpes: tuple[str, ...] = ()
    #: Package URLs that ``meta.identifiers`` declares, beside the ``pkg:nix`` one.
    purls: tuple[str, ...] = ()
    source: Source | None = None
    patches: tuple[str, ...] = ()
    build_references: tuple[str, ...] = ()
    drv_path: str | None = None
    #: The SRI SHA-256 of the output's NAR serialisation, from the store.
    nar_hash: str | None = None
    nar_size: int | None = None
    origin: Origin = Origin.VALUE

    @property
    def ref(self) -> str:
        """The store path without the store directory, as bombon names a component."""
        return PurePosixPath(self.path).name

    @property
    def purl(self) -> str:
        name = quote(self.name, safe="")
        return f"pkg:nix/{name}@{quote(self.version, safe='')}" if self.version else f"pkg:nix/{name}"


@dataclass(frozen=True, slots=True)
class Inventory:
    """What an SBOM lists: the target, its components and the edges between them.

    Every edge names a component, or the target, at both ends.
    """

    target: Record
    components: tuple[Record, ...]
    dependencies: dict[str, frozenset[str]] = field(default_factory=dict[str, frozenset[str]])
    #: The listed path that stands for each path of a listed package; a path with no version has none.
    aliases: dict[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, slots=True)
class Build:
    """One ``.drv`` of a build closure: what it runs, what it reads and what it writes."""

    drv_path: str
    name: str
    system: str
    builder: str
    args: tuple[str, ...]
    #: The output paths; a floating content-addressed output has none yet.
    outputs: tuple[str, ...]
    #: The outputs of input derivations and the sources the build reads.
    reads: tuple[str, ...]
    input_drvs: tuple[str, ...]
    #: A fixed-output derivation: a fetch, whose output hash is known before it runs.
    fixed_output: bool = False
    #: The revision a fixed-output git fetch checks out.
    rev: str | None = None


def _object(value: object) -> dict[str, object]:
    # JSON from `json.loads`: an object has string keys.
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _strings(value: object) -> tuple[str, ...]:
    return tuple(item for item in _list(value) if isinstance(item, str))


def _license(value: object) -> License | None:
    if isinstance(value, str):
        return License(spdx_id=None, name=value)
    fields = _object(value)
    spdx_id, name = _string(fields.get("spdxId")), _string(fields.get("fullName"))
    return License(spdx_id=spdx_id, name=name) if spdx_id or name else None


def _licenses(value: object) -> tuple[License, ...]:
    items = _list(value) or [value]
    return tuple(entry for entry in map(_license, items) if entry is not None)


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _source(value: object) -> Source | None:
    fields = _object(value)
    urls, repository = _strings(fields.get("urls")), _string(fields.get("repository"))
    if not urls and not repository:
        return None
    return Source(urls=urls, hash=_string(fields.get("hash")), repository=repository, rev=_string(fields.get("rev")))


def _maintainers(value: object) -> tuple[Maintainer, ...]:
    people = (_object(item) for item in _list(value))
    found = (
        Maintainer(name=_string(p.get("name")), email=_string(p.get("email")), github=_string(p.get("github")))
        for p in people
    )
    return tuple(m for m in found if m.name or m.email or m.github)


def _record(raw: dict[str, object]) -> Record:
    meta = _object(raw.get("meta"))
    path = str(raw["path"])
    return Record(
        path=path,
        name=_string(raw.get("pname")) or _string(raw.get("name")) or from_store_path(path).name,
        version=_string(raw.get("version")),
        output_name=_string(raw.get("outputName")),
        output_hash=_string(raw.get("outputHash")),
        licenses=_licenses(meta.get("license")),
        license_expression=_string(meta.get("licenseExpression")),
        homepages=_strings(meta.get("homepages")),
        changelogs=_strings(meta.get("changelogs")),
        download_page=_string(meta.get("downloadPage")),
        description=_string(meta.get("description")),
        long_description=_string(meta.get("longDescription")),
        main_program=_string(meta.get("mainProgram")),
        position=_string(meta.get("position")),
        maintainers=_maintainers(meta.get("maintainers")),
        unfree=_bool(meta.get("unfree")),
        insecure=_bool(meta.get("insecure")),
        broken=_bool(meta.get("broken")),
        known_vulnerabilities=_strings(meta.get("knownVulnerabilities")),
        cpe=_string(meta.get("cpe")),
        possible_cpes=_strings(meta.get("possibleCpes")),
        purls=_strings(meta.get("purls")),
        source=_source(raw.get("src")),
        patches=_strings(raw.get("patches")),
        build_references=_strings(raw.get("buildReferences")),
        drv_path=_string(raw.get("drvPath")),
    )


def parse_walk(text: str) -> dict[str, Record]:
    """The records of ``nix/sbom.nix``, by store path."""
    loaded: object = json.loads(text)
    if not isinstance(loaded, list):
        raise TypeError("the SBOM walk did not return a list")
    records = (_record(raw) for raw in map(_object, cast("list[object]", loaded)) if "path" in raw)
    return {record.path: record for record in records}


def from_store_path(path: str) -> Record:
    """A record from the name of a store path alone, for a path the walk did not reach.

    Nix's ``DrvName``: the name ends at the first dash that a non-letter
    follows, and the rest is the version.
    """
    _, _, rest = PurePosixPath(path).name.partition("-")
    parts = _VERSION_DASH.split(rest, maxsplit=1)
    version = parts[1] if len(parts) == 2 else None  # noqa: PLR2004 -- a name and a version
    return Record(path=path, name=parts[0], version=version, origin=Origin.NAME)


def _listed(record: Record) -> bool:
    return record.output_name not in _DOC_OUTPUTS and record.version is not None


def inventory(
    records: dict[str, Record],
    target: str,
    runtime: dict[str, tuple[str, ...]],
    *,
    buildtime: bool,
    always: frozenset[str] = frozenset(),
) -> Inventory:
    """Join the walk and the runtime closure into what an SBOM lists.

    *runtime* maps each path of the runtime closure to its references. With
    *buildtime*, the build closure joins it, with its build edges.

    One package URL is one component, as in bombon, so two outputs of one
    package (``glibc`` and ``glibc-bin``) are one component. An edge to the
    output that is left out goes to the one that stays, so the graph keeps it.
    A path in *always* is listed though it has no version.

    A path with no version is no component, but the graph passes through it:
    a NixOS system reaches its packages through ``etc`` and ``system-path``,
    which have none. Without that, most of a system hangs off nothing.
    """
    target_record = records.get(target) or from_store_path(target)
    candidates = [target_record, *(records.get(path) or from_store_path(path) for path in sorted(runtime))]
    if buildtime:
        candidates.extend(records[path] for path in sorted(records) if path not in runtime)
    # The `out` output names the package when it has one; then the path order.
    candidates[1:] = sorted(candidates[1:], key=lambda record: (record.output_name not in (None, "out"), record.path))
    kept: dict[str, Record] = {}
    alias: dict[str, str] = {}
    for record in candidates:
        if record.path == target or record.path in always or _listed(record):
            alias[record.path] = kept.setdefault(record.purl, record).path

    graph: dict[str, set[str]] = {path: set(references) for path, references in runtime.items()}
    if buildtime:
        for record in records.values():
            graph.setdefault(record.path, set()).update(record.build_references)

    # The listed paths each unlisted path reaches through unlisted paths alone.
    through: dict[str, frozenset[str]] = {}

    def reached(path: str) -> frozenset[str]:
        if path in through:
            return through[path]
        through[path] = frozenset()  # A path that refers to itself.
        found: set[str] = set()
        for ref in graph.get(path, ()):
            if ref in alias:
                found.add(alias[ref])
            elif ref != path:
                found |= reached(ref)
        through[path] = frozenset(found)
        return through[path]

    edges: dict[str, set[str]] = {path: set() for path in set(alias.values())}
    for path, references in graph.items():
        start = alias.get(path)
        if start is None:
            continue
        for ref in references:
            ends = {alias[ref]} if ref in alias else reached(ref)
            edges[start].update(ends - {start})
    return Inventory(
        target=target_record,
        components=tuple(kept.values()),
        dependencies={path: frozenset(refs) for path, refs in edges.items()},
        aliases=alias,
    )


_CVE = re.compile(r"CVE-\d{4}-\d{4,}", re.IGNORECASE)


def cve_ids(text: str) -> tuple[str, ...]:
    """The CVE identifiers *text* names, upper case, sorted, each once.

    nixpkgs names a security patch after the CVE it fixes
    (``fetchpatch { name = "CVE-2024-1234.patch"; }``), and writes the
    CVE into each ``meta.knownVulnerabilities`` entry it can.
    """
    return tuple(sorted({found.upper() for found in _CVE.findall(text)}))


def serial_uuid(seed: str) -> uuid.UUID:
    """A UUID that the same SBOM always gets: SHA-256 of *seed*, as bombon derives it."""
    return uuid.UUID(bytes=hashlib.sha256(seed.encode()).digest()[:16], version=4)


@dataclass(frozen=True, slots=True)
class Digest:
    """A hash in the form an SBOM states it."""

    algorithm: str
    hex: str


_SRI_ALGORITHMS = {"md5": "MD5", "sha1": "SHA-1", "sha256": "SHA-256", "sha512": "SHA-512"}


def sri_digest(sri: str | None) -> Digest | None:
    """The hex digest of an SRI hash, or ``None`` for any other form."""
    if sri is None or "-" not in sri:
        return None
    algorithm, _, encoded = sri.partition("-")
    name = _SRI_ALGORITHMS.get(algorithm)
    if name is None:
        return None
    try:
        return Digest(algorithm=name, hex=base64.b64decode(encoded, validate=True).hex())
    except ValueError:
        return None
