"""The packages a binary names inside itself: Go build info and cargo-auditable data.

The binaries are nixpkgs builds on the suite's PATH (``nix/suite-runtime.nix``).
cargo-auditable's own reader, ``rust-audit-info``, is the oracle for Rust.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import zlib
from pathlib import Path
from typing import cast

from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonStrictValidator

from pynix._impl._sbom_cyclonedx import document
from pynix._impl._sbom_embedded import Ecosystem, Package, parse_cargo, parse_go, read_binary, scan
from pynix._impl._sbom_model import inventory, parse_walk, with_embedded

#: ``cmd/go/internal/modload``'s ``infoStart`` and ``infoEnd``.
_GO_START = bytes.fromhex("3077af0c9274080241e1c107e6d618e6")
_GO_END = bytes.fromhex("f932433186182072008242104116d8f2")


def _program(name: str) -> Path:
    found = shutil.which(name)
    assert found is not None, f"{name} is not on PATH: nix/suite-runtime.nix provides it"
    return Path(found).resolve()


def _read(path: Path) -> tuple[Package, ...] | None:
    with path.open("rb") as stream:
        return read_binary(stream)


def _varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _go_section(version: str, lines: str) -> bytes:
    """A ``.go.buildinfo`` section as Go 1.18 and later lay it out."""
    header = b"\xff Go buildinf:" + bytes([8, 2]) + bytes(16)
    modules = _GO_START + lines.encode() + _GO_END
    return header + _varint(len(version)) + version.encode() + _varint(len(modules)) + modules


def test_a_rust_binary_names_the_crates_cargo_auditable_reads() -> None:
    binary = _program("rust-audit-info")
    oracle = json.loads(
        subprocess.run([binary, binary], capture_output=True, text=True, check=True).stdout  # noqa: S603 -- a nixpkgs program on itself
    )
    raw = cast("list[dict[str, object]]", oracle["packages"])
    runtime = {(str(p["name"]), str(p["version"])) for p in raw if p.get("kind") != "build"}
    found = _read(binary)
    assert found is not None
    assert {(p.name, p.version) for p in found} == runtime
    root = next(p for p in found if p.root)
    edges = cast("list[int]", next(p for p in raw if p.get("root")).get("dependencies") or [])
    assert set(root.dependencies) == {f"pkg:cargo/{raw[i]['name']}@{raw[i]['version']}" for i in edges}


def test_a_go_binary_names_its_modules_and_its_standard_library() -> None:
    found = _read(_program("go-md2man"))
    assert found is not None
    by_name = {p.name: p for p in found}
    assert by_name["github.com/russross/blackfriday/v2"].purl == "pkg:golang/github.com/russross/blackfriday/v2@v2.1.0"
    assert by_name["stdlib"].version is not None
    assert by_name["stdlib"].version.startswith("v1.")
    main = by_name["github.com/cpuguy83/go-md2man/v2"]
    assert main.root
    assert set(main.dependencies) == {p.purl for p in found if not p.root}
    # Go states no edges between modules, nor which ones the main module imports directly.
    assert {p.complete for p in found} == {None}


def test_a_c_binary_names_nothing() -> None:
    assert _read(_program("bash")) is None


def test_a_replace_line_names_what_was_compiled_in() -> None:
    lines = (
        "path\texample.org/app\n"
        "mod\texample.org/app\t(devel)\t\n"
        "dep\texample.org/old\tv1.0.0\th1:abc=\n"
        "=>\texample.org/fork\tv1.0.1\th1:def=\n"
        "dep\texample.org/local\tv0.1.0\th1:ghi=\n"
        "=>\t../local\t\t\n"
        "build\tCGO_ENABLED=0\n"
    )
    found = parse_go(_go_section("go1.26.8", lines))
    assert found is not None
    by_name = {p.name: p for p in found}
    assert "example.org/old" not in by_name
    assert by_name["example.org/fork"].version == "v1.0.1"
    assert ("go:replaces", "example.org/old v1.0.0") in by_name["example.org/fork"].properties
    # A local directory keeps the module's name, and has no version.
    assert by_name["example.org/local"].version is None
    assert by_name["example.org/app"].version is None
    assert by_name["stdlib"].version == "v1.26.8"


def test_go_build_info_without_its_sentinels_names_no_module() -> None:
    header = b"\xff Go buildinf:" + bytes([8, 2]) + bytes(16)
    section = header + _varint(8) + b"go1.26.8" + _varint(4) + b"mod\t"
    assert parse_go(section) == (Package(Ecosystem.GOLANG, "stdlib", "v1.26.8"),)


def _audit(*packages: dict[str, object]) -> bytes:
    return zlib.compress(json.dumps({"packages": list(packages)}).encode())


def test_a_build_dependency_is_not_compiled_in_and_leaves_its_dependent_incomplete() -> None:
    found = parse_cargo(
        _audit(
            {"name": "app", "version": "1.0.0", "source": "local", "root": True, "dependencies": [1, 2]},
            {"name": "serde", "version": "1.0.0", "source": "crates.io", "dependencies": [2]},
            {"name": "serde_derive", "version": "1.0.0", "source": "crates.io", "kind": "build"},
        )
    )
    assert found is not None
    by_name = {p.name: p for p in found}
    assert set(by_name) == {"app", "serde"}
    assert by_name["app"].dependencies == ("pkg:cargo/serde@1.0.0",)
    assert by_name["app"].complete is False
    assert by_name["serde"].complete is False
    assert parse_cargo(_audit({"name": "leaf", "version": "1.0.0", "source": "crates.io"})) == (
        Package(Ecosystem.CARGO, "leaf", "1.0.0", complete=True, properties=(("cargo:source", "crates.io"),)),
    )


def test_audit_data_that_is_not_zlib_names_nothing() -> None:
    assert parse_cargo(b"not zlib") is None


def test_a_scan_finds_the_executables_of_a_store_path(tmp_path: Path) -> None:
    store_path = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-tools-1.0"
    (tmp_path / "bin").mkdir()
    shutil.copy(_program("go-md2man"), tmp_path / "bin" / "go-md2man")
    # Not executable: a store file with no execute bit is no program.
    shutil.copy(_program("rust-audit-info"), tmp_path / "rust-audit-info")
    (tmp_path / "rust-audit-info").chmod(0o444)
    found = scan(tmp_path, store_path)
    assert {p.ecosystem for p in found} == {Ecosystem.GOLANG}
    assert {file for p in found for file in p.files} == {f"{store_path}/bin/go-md2man"}


_APP = "/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-app-1.0"
_LIB = "/nix/store/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-lib-2.0"


def test_the_document_nests_each_package_in_the_component_that_holds_it() -> None:
    records = parse_walk(
        json.dumps(
            [
                {"path": _APP, "pname": "app", "version": "1.0", "outputName": "out"},
                {"path": _LIB, "pname": "lib", "version": "2.0", "outputName": "out"},
            ]
        )
    )
    listed = inventory(records, _APP, {_APP: (_LIB,), _LIB: ()}, buildtime=False)
    found = parse_cargo(
        _audit(
            {"name": "lib", "version": "2.0.0", "source": "local", "root": True, "dependencies": [1]},
            {"name": "itoa", "version": "1.0.0", "source": "crates.io"},
        )
    )
    assert found is not None
    listed = with_embedded(listed, {_LIB: found, "/nix/store/cccccccccccccccccccccccccccccccc-unlisted": found})
    assert set(listed.embedded) == {_LIB}
    bom = document(
        listed, serial_number="urn:uuid:" + "0" * 8 + "-0000-4000-8000-" + "0" * 12, tool_version="0", patches={}
    )
    assert JsonStrictValidator(SchemaVersion.V1_7).validate_str(json.dumps(bom)) is None
    components = cast("list[dict[str, object]]", bom["components"])
    lib = next(c for c in components if c["name"] == "lib")
    inside = cast("list[dict[str, object]]", lib["components"])
    refs = {str(c["bom-ref"]) for c in inside}
    assert refs == {f"{lib['bom-ref']}#pkg:cargo/lib@2.0.0", f"{lib['bom-ref']}#pkg:cargo/itoa@1.0.0"}
    edges = {str(d["ref"]): d["dependsOn"] for d in cast("list[dict[str, object]]", bom["dependencies"])}
    assert edges[f"{lib['bom-ref']}#pkg:cargo/lib@2.0.0"] == [f"{lib['bom-ref']}#pkg:cargo/itoa@1.0.0"]
    claims = {
        (str(c["aggregate"]), kind): set(cast("list[str]", c[kind]))
        for c in cast("list[dict[str, object]]", bom["compositions"])
        for kind in ("assemblies", "dependencies")
        if kind in c
    }
    assert claims[("incomplete", "assemblies")] == {lib["bom-ref"]}
    assert refs <= claims[("complete", "dependencies")]
