"""The records of ``pynix sbom``, and the inventory that each format renders.

``sbom_walk.nix`` gives one record for each output in the build closure. The
store gives the runtime closure and its references. :func:`inventory` joins
the two as bombon's ``transform`` does (``rust/transformer/src/transform.rs``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import cast
from urllib.parse import quote

#: Outputs that hold documentation. bombon leaves them out of every SBOM.
_DOC_OUTPUTS = frozenset({"doc", "man"})


@dataclass(frozen=True, slots=True)
class License:
    """One license of a package: an SPDX identifier, or a name when it has none."""

    spdx_id: str | None
    name: str | None


@dataclass(frozen=True, slots=True)
class Source:
    """Where the source of a package comes from, and its hash in SRI form."""

    urls: tuple[str, ...]
    hash: str | None


@dataclass(frozen=True, slots=True)
class Record:
    """One output of a derivation, with what Nix knows about its package."""

    path: str
    name: str
    version: str | None = None
    output_name: str | None = None
    output_hash: str | None = None
    licenses: tuple[License, ...] = ()
    homepage: str | None = None
    description: str | None = None
    cpe: str | None = None
    possible_cpes: tuple[str, ...] = ()
    source: Source | None = None
    patches: tuple[str, ...] = ()
    build_references: tuple[str, ...] = ()

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


def _source(value: object) -> Source | None:
    fields = _object(value)
    urls = _strings(fields.get("urls"))
    return Source(urls=urls, hash=_string(fields.get("hash"))) if urls else None


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
        homepage=_string(meta.get("homepage")),
        description=_string(meta.get("description")),
        cpe=_string(meta.get("cpe")),
        possible_cpes=_strings(meta.get("possibleCpes")),
        source=_source(raw.get("src")),
        patches=_strings(raw.get("patches")),
        build_references=_strings(raw.get("buildReferences")),
    )


def parse_walk(text: str) -> dict[str, Record]:
    """The records of ``sbom_walk.nix``, by store path."""
    loaded: object = json.loads(text)
    if not isinstance(loaded, list):
        raise TypeError("the SBOM walk did not return a list")
    records = (_record(raw) for raw in map(_object, cast("list[object]", loaded)) if "path" in raw)
    return {record.path: record for record in records}


def from_store_path(path: str) -> Record:
    """A record from the name of a store path alone, for a path the walk did not reach.

    bombon's ``Derivation::from_store_path``: the last dash-separated part is
    the version when it starts with a digit.
    """
    base = PurePosixPath(path).name
    parts = base.split("-")[1:]
    if len(parts) > 1 and parts[-1][:1].isdigit():
        return Record(path=path, name="-".join(parts[:-1]), version=parts[-1])
    return Record(path=path, name="-".join(parts))


def _listed(record: Record) -> bool:
    return record.output_name not in _DOC_OUTPUTS and record.version is not None


def inventory(
    records: dict[str, Record],
    target: str,
    runtime: dict[str, tuple[str, ...]],
    *,
    buildtime: bool,
) -> Inventory:
    """Join the walk and the runtime closure into what an SBOM lists.

    *runtime* maps each path of the runtime closure to its references. With
    *buildtime*, the build closure joins it, with its build edges, once for
    each package name.
    """
    chosen = [records.get(path) or from_store_path(path) for path in sorted(runtime)]
    if buildtime:
        names = {(record.name, record.version) for record in chosen}
        for path in sorted(records):
            record = records[path]
            if path not in runtime and (record.name, record.version) not in names:
                names.add((record.name, record.version))
                chosen.append(record)
    by_purl: dict[str, Record] = {}
    for record in filter(_listed, chosen):
        by_purl.setdefault(record.purl, record)
    components = tuple(by_purl.values())
    target_record = records.get(target) or from_store_path(target)

    present = {record.path for record in components} | {target}
    edges: dict[str, set[str]] = {path: set() for path in present}
    for path, references in runtime.items():
        if path in present:
            edges[path].update(ref for ref in references if ref in present and ref != path)
    if buildtime:
        for record in components:
            edges[record.path].update(ref for ref in record.build_references if ref in present and ref != record.path)
    return Inventory(
        target=target_record,
        components=components,
        dependencies={path: frozenset(refs) for path, refs in edges.items()},
    )


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
