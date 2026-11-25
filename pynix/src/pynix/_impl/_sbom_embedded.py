"""The packages a store path names inside itself: Go build info, cargo-auditable data, Python, npm and Composer metadata.

A Go binary holds the modules it was built from, in its ``.go.buildinfo``
section (``debug/buildinfo``). nixpkgs builds Rust with cargo-auditable,
which writes the crates and the edges between them, as zlib-compressed JSON,
to a ``.dep-v0`` section. An installed Python distribution has a
``METADATA`` file in its ``.dist-info`` directory (``PKG-INFO`` for the
older ``.egg-info``), and a package that vendors others has theirs too.
npm installs each package at ``node_modules/<name>`` (``node_modules/@scope/<name>``)
with its ``package.json``; a ``package.json`` anywhere else is a
project's own, or a file inside a package. Composer lists every package
it installed into a vendor directory in ``vendor/composer/installed.json``
(the directory name is a project setting: Nextcloud uses ``3rdparty``).
All of them name what is in the artifact, from the artifact itself: no
lock file, no build and no network.

A Python distribution's or an npm package's name is not proof that the
code came from PyPI or the npm registry: a package built from a Git
checkout, or one never published, names itself the same way.
"""

from __future__ import annotations

import io
import json
import os
import re
import stat
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from email.parser import BytesHeaderParser
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol, cast, runtime_checkable
from urllib.parse import quote

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile
from huggorm import FileType, SourceAccessor

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
#: The ``build`` settings govulncheck matches a vulnerability's platforms against, as properties.
_GO_PLATFORM = {"GOOS": "go:goos", "GOARCH": "go:goarch"}


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
    PYPI = "pypi"
    NPM = "npm"
    COMPOSER = "composer"


@dataclass(frozen=True, slots=True)
class Package:
    """One package compiled into, or installed at, the *files*."""

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
        # The purl spec's PyPI rule, as packageurl-python applies it: PEP 503 also folds ".", the spec does not.
        if self.ecosystem is Ecosystem.PYPI:
            own = self.name.lower().replace("_", "-")
        elif self.ecosystem in (Ecosystem.NPM, Ecosystem.COMPOSER):
            own = self.name.lower()
        else:
            own = self.name
        name = "/".join(quote(part, safe="") for part in own.split("/"))
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

    Each module carries the ``GOOS`` and ``GOARCH`` it was compiled for
    (``go:goos``, ``go:goarch``): a Go vulnerability can name the platforms
    it affects.
    """
    found = _go_strings(section)
    if found is None:
        return None
    toolchain, lines = found
    main: Package | None = None
    deps: list[Package] = []
    platform: list[tuple[str, str]] = []
    for line in lines.splitlines():
        kind, _, rest = line.partition("\t")
        fields = rest.split("\t")
        setting, _, value = rest.partition("=")
        if kind == "build" and setting in _GO_PLATFORM and value:
            platform.append((_GO_PLATFORM[setting], value))
        elif kind == "mod":
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
    return tuple(replace(p, properties=(*p.properties, *platform)) for p in listed) or None


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


def read_binary(stream: io.BufferedIOBase) -> tuple[Package, ...] | None:
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


def parse_python(metadata: bytes) -> Package | None:
    """The distribution a core metadata file (``METADATA``, ``PKG-INFO``) describes; ``None`` without a name."""
    headers = BytesHeaderParser().parsebytes(metadata)
    name, version = (headers.get(field, "").strip() for field in ("Name", "Version"))
    return Package(Ecosystem.PYPI, name, version or None) if name else None


def parse_npm(manifest: bytes) -> Package | None:
    """The package an installed ``package.json`` describes; ``None`` without a name, or for a file that is no JSON object."""
    try:
        data = json.loads(manifest)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    name, version = data.get("name"), data.get("version")
    if not isinstance(name, str) or not name:
        return None
    return Package(Ecosystem.NPM, name, version if isinstance(version, str) and version else None)


def parse_composer(installed: bytes) -> tuple[Package, ...] | None:
    """The packages a Composer ``installed.json`` lists; ``None`` for a file that is no such list.

    An edge goes to the package of that name in the same file, or to the
    one that replaces or provides it. A requirement with no ``/`` in its
    name (``php``, ``ext-json``) is the platform, not a package. A package
    is complete when every other requirement resolves in the file.
    """
    try:
        data: object = json.loads(installed)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    # Composer 2 writes {"packages": [...]}; Composer 1 wrote the list alone.
    raw = cast("dict[str, object]", data).get("packages") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return None
    entries = [
        cast("dict[str, object]", e)
        for e in cast("list[object]", raw)
        if isinstance(e, dict) and isinstance(e.get("name"), str) and e.get("name")
    ]
    shells = {
        str(e["name"]).lower(): Package(
            Ecosystem.COMPOSER,
            str(e["name"]),
            str(e["version"]) if isinstance(e.get("version"), str) and e.get("version") else None,
        )
        for e in entries
    }
    targets = dict(shells)
    for entry in entries:
        for field in ("replace", "provide"):
            links = entry.get(field)
            for name in cast("dict[str, object]", links) if isinstance(links, dict) else ():
                targets.setdefault(name.lower(), shells[str(entry["name"]).lower()])
    result: list[Package] = []
    for entry in entries:
        shell = shells[str(entry["name"]).lower()]
        require = entry.get("require")
        wanted = [
            n.lower() for n in (cast("dict[str, object]", require) if isinstance(require, dict) else ()) if "/" in n
        ]
        found = [targets[n].purl for n in wanted if n in targets and targets[n] is not shell]
        result.append(
            replace(
                shell,
                dependencies=tuple(sorted(set(found))),
                complete=all(n in targets for n in wanted),
            )
        )
    return tuple(result) or None


def package_json(package: Package) -> dict[str, object]:
    """*package* as JSON, for a cache of scans; :func:`package_of_json` reads it back."""
    return {
        "ecosystem": package.ecosystem.value,
        "name": package.name,
        "version": package.version,
        "dependencies": list(package.dependencies),
        "complete": package.complete,
        "root": package.root,
        "files": list(package.files),
        "properties": [list(p) for p in package.properties],
    }


def package_of_json(data: dict[str, object]) -> Package:
    """The package :func:`package_json` wrote."""
    version, complete = data["version"], data["complete"]
    return Package(
        Ecosystem(str(data["ecosystem"])),
        str(data["name"]),
        str(version) if version is not None else None,
        dependencies=tuple(str(d) for d in cast("list[object]", data["dependencies"])),
        complete=bool(complete) if complete is not None else None,
        root=bool(data["root"]),
        files=tuple(str(f) for f in cast("list[object]", data["files"])),
        properties=tuple((str(k), str(v)) for k, v in cast("list[list[object]]", data["properties"])),
    )


class Tree(Protocol):
    """The files of one store path, named by their store paths."""

    def regular_files(self) -> Iterator[tuple[PurePosixPath, bool]]:
        """Each regular file, and whether it is executable. A symbolic link is not followed."""
        ...

    def read(self, path: PurePosixPath) -> bytes: ...

    def open(self, path: PurePosixPath) -> io.BufferedIOBase: ...


@dataclass(frozen=True, slots=True)
class DiskTree:
    """A store path whose files are at *disk* on this host."""

    disk: Path
    store_path: str

    def _on_disk(self, path: PurePosixPath) -> Path:
        relative = path.relative_to(self.store_path)
        return self.disk / relative if relative.parts else self.disk

    def regular_files(self) -> Iterator[tuple[PurePosixPath, bool]]:
        for path, mode in _regular_files(self.disk):
            relative = path.relative_to(self.disk).as_posix() if path != self.disk else ""
            yield PurePosixPath(self.store_path, relative), bool(mode & 0o111)

    def read(self, path: PurePosixPath) -> bytes:
        return self._on_disk(path).read_bytes()

    def open(self, path: PurePosixPath) -> io.BufferedIOBase:
        return self._on_disk(path).open("rb")


@dataclass(frozen=True, slots=True)
class AccessorTree:
    """A store path read through its store's accessor: on a remote store,
    the path's NAR, fetched once and held in memory."""

    accessor: SourceAccessor
    store_path: str

    def _inside(self, path: PurePosixPath) -> str:
        relative = path.relative_to(self.store_path)
        return "/" + relative.as_posix() if relative.parts else "/"

    def regular_files(self) -> Iterator[tuple[PurePosixPath, bool]]:
        pending = [""]
        while pending:
            relative = pending.pop()
            current = "/" + relative
            entry = self.accessor.lstat(current)
            if entry.type() == FileType.REGULAR:
                yield PurePosixPath(self.store_path, relative), entry.is_executable()
            elif entry.type() == FileType.DIRECTORY:
                pending.extend(
                    f"{relative}/{name}".lstrip("/") for name in sorted(self.accessor.read_directory(current))
                )

    def read(self, path: PurePosixPath) -> bytes:
        return self.accessor.read_file(self._inside(path))

    def open(self, path: PurePosixPath) -> io.BufferedIOBase:
        return io.BytesIO(self.read(path))


def _is_composer_manifest(path: PurePosixPath) -> bool:
    """Whether *path* is the list of packages Composer installed into a vendor directory."""
    return path.name == "installed.json" and path.parent.name == "composer"


def _is_npm_manifest(path: PurePosixPath) -> bool:
    """Whether *path* is the ``package.json`` of a package npm installed: ``node_modules/<name>`` or ``node_modules/@scope/<name>``."""
    if path.name != "package.json":
        return False
    above = path.parent.parent
    return above.name == "node_modules" or (above.name.startswith("@") and above.parent.name == "node_modules")


def _is_python_metadata(path: PurePosixPath) -> bool:
    """Whether *path* is the core metadata of an installed distribution, in any of the layouts pip and setuptools write."""
    parent = path.parent.name
    return (
        (parent.endswith(".dist-info") and path.name == "METADATA")
        or (parent.endswith(".egg-info") and path.name == "PKG-INFO")
        or path.name.endswith(".egg-info")
    )


def _regular_files(root: Path) -> Iterator[tuple[Path, int]]:
    """Every regular file under *root*, with its mode; *root* itself when it is one.

    A symbolic link is not followed: a Python environment links each
    distribution of another store path, which that store path names itself.
    """
    if root.is_symlink() or not root.exists():
        return
    if root.is_file():
        candidates: Iterable[Path] = [root]
    else:
        candidates = (Path(top, name) for top, _, names in os.walk(root) for name in names)
    for path in candidates:
        mode = path.lstat().st_mode
        if stat.S_ISREG(mode):
            yield path, mode


def _read(tree: Tree, path: PurePosixPath, executable: bool) -> tuple[Package, ...] | None:
    """The packages the file *path* names: an executable's compiled-in ones, a Python distribution's or npm package's own, or Composer's list."""
    if _is_python_metadata(path):
        package = parse_python(tree.read(path))
        return (package,) if package else None
    if _is_npm_manifest(path):
        package = parse_npm(tree.read(path))
        return (package,) if package else None
    if _is_composer_manifest(path):
        return parse_composer(tree.read(path))
    if not executable:
        return None
    with tree.open(path) as stream:
        if stream.read(len(_ELF_MAGIC)) != _ELF_MAGIC:
            return None
        stream.seek(0)
        return read_binary(stream)


def scan_tree(tree: Tree) -> tuple[Package, ...]:
    """The packages the files of *tree* name.

    Each package carries the store path of the files that hold it. One
    package in two files is one package.
    """
    found: list[Package] = []
    for path, executable in tree.regular_files():
        if packages := _read(tree, path, executable):
            found.extend(replace(p, files=(str(path),)) for p in packages)
    return merge(found)


def scan(disk: Path, store_path: str) -> tuple[Package, ...]:
    """The packages the files of the store path *store_path*, at *disk* on this host, name."""
    return scan_tree(DiskTree(disk, store_path))


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
