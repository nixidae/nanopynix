"""The packages a compiled program names inside itself: Go build info and cargo-auditable data.

A Go binary holds the modules it was built from, in its ``.go.buildinfo``
section (``debug/buildinfo``). nixpkgs builds Rust with cargo-auditable,
which writes the crates and the edges between them, as zlib-compressed JSON,
to a ``.dep-v0`` section. Both name what is compiled in, from the artifact
itself: no lock file, no build and no network.
"""

from __future__ import annotations

import json
import os
import re
import stat
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol, cast, runtime_checkable
from urllib.parse import quote

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile

_ELF_MAGIC = b"\x7fELF"
_GO_MAGIC = b"\xff Go buildinf:"
_GO_ALIGN = 16
_GO_HEADER = 32
#: Go 1.18 and later write the strings inline; older Go wrote pointers, which nixpkgs no longer builds.
_GO_INLINE = 0x2
#: The 16-byte sentinels that ``cmd/go`` puts around the module lines.
_GO_SENTINEL = 16
#: cargo-auditable's own advice: no legitimate audit data is larger.
_AUDIT_LIMIT = 8 << 20
_GO_RELEASE = re.compile(r"go(\d+\.\d+(?:\.\d+)?(?:(?:rc|beta)\d+)?)$")


@runtime_checkable
class _Section(Protocol):
    def data(self) -> bytes: ...


@runtime_checkable
class _Elf(Protocol):
    """The part of pyelftools' ``ELFFile`` this module calls; pyelftools has no type hints."""

    def get_section_by_name(self, name: str) -> _Section | None: ...


class Ecosystem(StrEnum):
    """The package URL type of an embedded package."""

    GOLANG = "golang"
    CARGO = "cargo"


@dataclass(frozen=True, slots=True)
class Package:
    """One package compiled into the binaries at *files*."""

    ecosystem: Ecosystem
    name: str
    version: str | None
    #: The package URLs it depends on.
    dependencies: tuple[str, ...] = ()
    #: Whether *dependencies* names every package it depends on directly; ``None`` when the binary does not say.
    complete: bool | None = None
    #: The main module or root crate: the program itself.
    root: bool = False
    files: tuple[str, ...] = ()
    properties: tuple[tuple[str, str], ...] = ()

    @property
    def purl(self) -> str:
        name = "/".join(quote(part, safe="") for part in self.name.split("/"))
        return (
            f"pkg:{self.ecosystem}/{name}@{quote(self.version, safe='')}"
            if self.version
            else f"pkg:{self.ecosystem}/{name}"
        )


def _uvarint(data: bytes, offset: int) -> tuple[int, int]:
    """encoding/binary ``Uvarint``: the value, and the offset after it."""
    value = shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("truncated varint")


def _go_strings(section: bytes) -> tuple[str, str] | None:
    """The Go version and module lines of a ``.go.buildinfo`` section, as ``debug/buildinfo`` reads them."""
    start = 0
    while (start := section.find(_GO_MAGIC, start)) >= 0 and start % _GO_ALIGN:
        start += 1
    if start < 0 or len(section) < start + _GO_HEADER or not section[start + 15] & _GO_INLINE:
        return None
    try:
        length, offset = _uvarint(section, start + _GO_HEADER)
        version = section[offset : offset + length]
        length, offset = _uvarint(section, offset + length)
        framed = section[offset : offset + length]
        # The sentinels are not UTF-8: cut them off before decoding.
        framing = len(framed) >= 2 * _GO_SENTINEL + 1 and framed[-_GO_SENTINEL - 1] == ord("\n")
        modules = framed[_GO_SENTINEL:-_GO_SENTINEL].decode() if framing else ""
        return (version.decode(), modules) if version else None
    except (ValueError, UnicodeDecodeError):
        return None


def _go_module(fields: list[str], *, root: bool = False) -> Package:
    """A ``mod``, ``dep`` or ``=>`` line: the path, the version and the ``go.sum`` hash."""
    path, version, checksum = (*fields, "", "")[:3]
    properties = (("go:sum", checksum),) if checksum else ()
    return Package(
        Ecosystem.GOLANG, path, version if version not in ("", "(devel)") else None, root=root, properties=properties
    )


def parse_go(section: bytes) -> tuple[Package, ...] | None:
    """The modules of a ``.go.buildinfo`` section, the standard library first; ``None`` when it holds none.

    The main module depends on every other one. Go states no edges between
    the others, and does not say which of them the main module imports
    directly, so every module's dependencies are unknown.
    """
    found = _go_strings(section)
    if found is None:
        return None
    toolchain, lines = found
    main: Package | None = None
    deps: list[Package] = []
    for line in lines.splitlines():
        kind, _, rest = line.partition("\t")
        fields = rest.split("\t")
        if kind == "mod":
            main = _go_module(fields, root=True)
        elif kind == "dep":
            deps.append(_go_module(fields))
        elif kind == "=>" and (deps or main):
            # A replace line follows the module it replaces: the replacement is what was compiled in.
            replaced = deps[-1] if deps else cast("Package", main)
            replacement = _go_module(fields, root=replaced.root)
            if replacement.version is None:
                # A local directory: the module keeps its name, and has no version.
                replacement = replace(replacement, name=replaced.name)
            note = ("go:replaces", f"{replaced.name} {replaced.version or ''}".strip())
            replacement = replace(replacement, properties=(*replacement.properties, note))
            if deps:
                deps[-1] = replacement
            else:
                main = replacement
    release = _GO_RELEASE.match(toolchain.split(" ", 1)[0])
    stdlib = Package(Ecosystem.GOLANG, "stdlib", f"v{release[1]}") if release else None
    listed = [*([stdlib] if stdlib else []), *deps]
    if main is not None:
        # Every module of the build, not only the direct ones: still unknown.
        dependencies = tuple(sorted({p.purl for p in listed}))
        listed.append(
            replace(main, dependencies=dependencies, properties=(*main.properties, ("go:toolchain", toolchain)))
        )
    return tuple(listed) or None


def parse_cargo(section: bytes) -> tuple[Package, ...] | None:
    """The crates of a ``.dep-v0`` section that are compiled in; ``None`` when it cannot be read.

    A ``build`` crate (a build script's or a procedural macro's) runs at build
    time only. It is left out, and a crate with an edge to one is incomplete.
    """
    try:
        inflater = zlib.decompressobj()
        text = inflater.decompress(section, _AUDIT_LIMIT)
        if inflater.unconsumed_tail:
            return None
        loaded: object = json.loads(text)
    except (zlib.error, ValueError):
        return None
    raw = cast("dict[str, object]", loaded).get("packages") if isinstance(loaded, dict) else None
    if not isinstance(raw, list):
        return None
    entries = [cast("dict[str, object]", e) for e in cast("list[object]", raw) if isinstance(e, dict)]
    if len(entries) != len(cast("list[object]", raw)):
        return None
    shells = [
        Package(Ecosystem.CARGO, str(e.get("name", "")), str(e["version"]) if e.get("version") else None)
        for e in entries
    ]
    result: list[Package] = []
    for entry, shell in zip(entries, shells, strict=True):
        if entry.get("kind") == "build" or not shell.name:
            continue
        indices = [i for i in cast("list[object]", entry.get("dependencies") or []) if isinstance(i, int)]
        targets = [entries[i] for i in indices if 0 <= i < len(entries)]
        runtime = [shells[i].purl for i in indices if 0 <= i < len(entries) and entries[i].get("kind") != "build"]
        result.append(
            Package(
                Ecosystem.CARGO,
                shell.name,
                shell.version,
                dependencies=tuple(sorted(set(runtime))),
                complete=len(targets) == len(indices) and len(runtime) == len(targets),
                root=entry.get("root") is True,
                properties=(("cargo:source", str(entry.get("source", ""))),),
            )
        )
    return tuple(result) or None


def read_binary(stream: BinaryIO) -> tuple[Package, ...] | None:
    """The packages an ELF file names inside itself; ``None`` for any other file, or one that names none."""
    try:
        elf = cast("_Elf", ELFFile(stream))
        go = elf.get_section_by_name(".go.buildinfo")
        if go is not None:
            return parse_go(go.data())
        audit = elf.get_section_by_name(".dep-v0")
        if audit is not None:
            return parse_cargo(audit.data())
    except ELFError:
        return None
    return None


def _executables(root: Path) -> Iterator[Path]:
    """Every regular file under *root* that some user may run; *root* itself when it is one."""
    if root.is_symlink() or not root.exists():
        return
    if root.is_file():
        candidates: Iterable[Path] = [root]
    else:
        candidates = (Path(top, name) for top, _, names in os.walk(root) for name in names)
    for path in candidates:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_mode & 0o111:
            yield path


def scan(disk: Path, store_path: str) -> tuple[Package, ...]:
    """The packages each executable of the store path *store_path*, at *disk* on this host, names.

    Each package carries the store path of the files that hold it. One
    package in two files is one package.
    """
    found: list[Package] = []
    for path in _executables(disk):
        with path.open("rb") as stream:
            if stream.read(len(_ELF_MAGIC)) != _ELF_MAGIC:
                continue
            stream.seek(0)
            packages = read_binary(stream)
        if packages:
            name = str(PurePosixPath(store_path, path.relative_to(disk).as_posix())) if path != disk else store_path
            found.extend(replace(p, files=(name,)) for p in packages)
    return merge(found)


def merge(packages: Iterable[Package]) -> tuple[Package, ...]:
    """One package for each package URL, with the files, edges and properties of each copy, by package URL."""
    by_purl: dict[str, Package] = {}
    for package in packages:
        seen = by_purl.get(package.purl)
        if seen is None:
            by_purl[package.purl] = package
            continue
        complete = None if seen.complete is None or package.complete is None else seen.complete and package.complete
        by_purl[package.purl] = replace(
            seen,
            dependencies=tuple(sorted({*seen.dependencies, *package.dependencies})),
            complete=complete,
            root=seen.root or package.root,
            files=tuple(sorted({*seen.files, *package.files})),
            properties=tuple(sorted({*seen.properties, *package.properties})),
        )
    return tuple(by_purl[purl] for purl in sorted(by_purl))
